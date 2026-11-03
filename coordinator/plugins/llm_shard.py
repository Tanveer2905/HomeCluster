"""``llm_shard`` -- hold part of a distributed model on one device (SPEC 6.8).

Runs llama.cpp's ``ggml-rpc-server`` for as long as the deployment lives,
bound to the device's LAN address on a random port, and announces
``host:port`` so the coordinator can hand it to the head.  Created by the
LLM manager (``coordinator/llm.py``), not by hand.
"""

from __future__ import annotations

from typing import Any, Dict, List

TYPE = "llm_shard"
DESCRIPTION = "Internal: one device's share of a distributed LLM (use the Models page)"
HIDDEN = True

PARAMS = [
    {"name": "deployment", "type": "str", "default": "", "help": "Deployment id."},
    {"name": "device_id", "type": "str", "default": "", "help": "Device that holds this share."},
    {"name": "llama", "type": "str", "default": "", "help": "Pinned llama.cpp tag."},
    {"name": "threads", "type": "int", "default": 0, "help": "CPU threads (0 = all cores)."},
    {"name": "cache", "type": "bool", "default": True, "help": "Cache received weights on the device."},
    {"name": "devices", "type": "json", "default": [], "help": "Parts to offer, e.g. [\"CPU\", \"Vulkan0\"] (GPU build)."},
    {"name": "gpu", "type": "json", "default": None, "help": "{vk_index} of the graphics card to use."},
]

DEFAULT_OPTIONS: Dict[str, Any] = {"requires": ["service"], "timeout": 0, "max_attempts": 20}

SHARD_CODE = r'''
import os, re, socket, subprocess, sys, threading, time


def _binary(ctx, names):
    for name in names:
        for cand in ((name + ".exe", name) if os.name == "nt" else (name,)):
            path = os.path.join(ctx.runtime_dir or "", cand)
            if os.path.isfile(path):
                return path
    raise RuntimeError("no %s in %s" % ("/".join(names), ctx.runtime_dir))


def _free_port(ip):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((ip, 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def gpu_env(env, gpu):
    """Process-local settings for the GPU build (nothing system-wide changes):
    show llama.cpp only the chosen card, and skip AMD's switchable-graphics
    layer, which makes Vulkan device listing hang on hybrid laptops."""
    env["DISABLE_LAYER_AMD_SWITCHABLE_GRAPHICS_1"] = "1"
    if gpu and gpu.get("vk_index") is not None:
        env["GGML_VK_VISIBLE_DEVICES"] = str(int(gpu["vk_index"]))
    return env


def load(ctx):
    exe = _binary(ctx, ["ggml-rpc-server", "rpc-server"])
    ip = ctx.lan_ip()
    port = _free_port(ip)
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = ctx.runtime_dir + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    # received weights are cached next to the runtime, so a restart reloads fast
    env["LLAMA_CACHE"] = os.path.join(os.path.dirname(ctx.runtime_dir.rstrip("/\\")), "rpc-cache")
    args = [exe, "-H", ip, "-p", str(port)]
    parts = [str(d) for d in (ctx.params.get("devices") or [])]
    if parts:
        # one rpc server, several parts (CPU, then the graphics card): the
        # head sees one device per part, in this order
        args += ["-d", ",".join(parts)]
        gpu_env(env, ctx.params.get("gpu"))
    if ctx.params.get("cache", True):
        args.append("-c")
    if int(ctx.params.get("threads") or 0) > 0:
        args += ["-t", str(int(ctx.params["threads"]))]
    ctx.log("[shard] starting %s on %s:%d%s" % (os.path.basename(exe), ip, port,
                                                (" with " + ", ".join(parts)) if parts else ""))
    # ctx.spawn ties the rpc server to this service process (it dies with it)
    spawn = getattr(ctx, "spawn", None) or subprocess.Popen
    proc = spawn(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                 stdin=subprocess.DEVNULL, cwd=ctx.runtime_dir, env=env)
    facts = {"total_mb": None, "free_mb": None, "parts": []}
    banner = re.compile(r"\((\d+) MiB, (\d+) MiB free\)")

    def pump():
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            match = banner.search(line)
            if match:
                facts["total_mb"], facts["free_mb"] = int(match.group(1)), int(match.group(2))
                facts["parts"].append({"line": line.strip()[:120], "total_mb": facts["total_mb"],
                                       "free_mb": facts["free_mb"]})
            if line and not line.startswith("!!!") and "Client connection" not in line:
                ctx.log("[rpc] " + line)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.time() + 60
    while True:
        if proc.poll() is not None:
            raise RuntimeError("rpc server exited with code %s" % proc.returncode)
        try:
            socket.create_connection((ip, port), timeout=1).close()
            break
        except OSError:
            if time.time() > deadline:
                proc.kill()
                raise RuntimeError("rpc server did not start listening on %s:%d" % (ip, port))
            time.sleep(0.2)

    def watchdog():
        # if the rpc server dies, end this process so the worker restarts the shard
        proc.wait()
        ctx.log("[shard] rpc server exited (code %s); restarting" % proc.returncode)
        os._exit(3)

    threading.Thread(target=watchdog, daemon=True).start()
    time.sleep(0.3)  # let the banner with the memory figures arrive
    ctx.announce({"endpoint": "%s:%d" % (ip, port), "host": ip, "port": port,
                  "rpc_total_mb": facts["total_mb"], "rpc_free_mb": facts["free_mb"],
                  "parts": facts["parts"]})
    return {"proc": proc, "endpoint": "%s:%d" % (ip, port)}


def serve(request, ctx, model):
    return {"endpoint": model["endpoint"], "alive": model["proc"].poll() is None}


def unload(model, ctx):
    proc = model["proc"]
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
'''


def split(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> List[Dict[str, Any]]:
    device_id = str(params.get("device_id") or "")
    deployment = str(params.get("deployment") or "")
    tag = str(params.get("llama") or "")
    if not (device_id and deployment and tag):
        raise ValueError("llm_shard needs deployment, device_id and llama")
    service_params: Dict[str, Any] = {"threads": int(params.get("threads") or 0),
                                      "cache": bool(params.get("cache", True))}
    needs: Dict[str, Any] = {"llama": tag}
    if params.get("devices"):
        service_params["devices"] = [str(d) for d in params["devices"]]
        service_params["gpu"] = params.get("gpu") or {}
        needs["llama_variant"] = "gpu"   # the PC's Vulkan build (tools/llama.cpp/win-vulkan-x64)
    return [{
        "impls": {"python": {"code": SHARD_CODE, "entry": "serve", "args": {}}},
        "inputs": [],
        "timeout": 0,
        "requires": ["service"],
        "device_id": device_id,
        "service": {
            "name": "_llm.%s.%s" % (deployment, device_id),
            "request_timeout": 30,
            "params": service_params,
            "needs": needs,
            "meta": {"deployment": deployment, "role": "shard"},
        },
        "meta": {"deployment": deployment, "role": "shard"},
    }]


def reduce(params: Dict[str, Any], results: List[Dict[str, Any]], ctx: Any) -> Any:
    return {"stopped": True}
