"""``llm_head`` -- the front of a distributed model (SPEC 6.8).

Runs llama.cpp's ``llama-server`` on the chosen host device, pointed at the
shards' rpc servers (``--rpc``) with an explicit layer split (``-ts``), and
serves chat requests through the normal service relay: any device can call
the deployment by name and receives tokens as partial results.  Created by
the LLM manager (``coordinator/llm.py``), not by hand.
"""

from __future__ import annotations

from typing import Any, Dict, List

TYPE = "llm_head"
DESCRIPTION = "Internal: front end of a distributed LLM (use the Models page)"
HIDDEN = True

PARAMS = [
    {"name": "deployment", "type": "str", "default": ""},
    {"name": "name", "type": "str", "default": "", "help": "Service name callers use."},
    {"name": "device_id", "type": "str", "default": ""},
    {"name": "llama", "type": "str", "default": ""},
    {"name": "model", "type": "json", "default": {}, "help": "{name, size}"},
    {"name": "rpc", "type": "json", "default": [], "help": "ordered host:port list"},
    {"name": "tensor_split", "type": "json", "default": [], "help": "layers per rpc part, same order"},
    {"name": "gpu_layers", "type": "int", "default": 0,
     "help": "Alone on one device: layers on its graphics card (999 = all)."},
    {"name": "gpu", "type": "json", "default": None, "help": "{vk_index} of the graphics card."},
    {"name": "ctx", "type": "int", "default": 2048},
    {"name": "threads", "type": "int", "default": 0},
    {"name": "extra_args", "type": "json", "default": []},
    {"name": "request_timeout", "type": "int", "default": 900},
    {"name": "load_timeout", "type": "int", "default": 3600},
]

DEFAULT_OPTIONS: Dict[str, Any] = {"requires": ["service"], "timeout": 0, "max_attempts": 3}

HEAD_CODE = r'''
import json, os, socket, subprocess, sys, threading, time, urllib.request

TAIL = []


def _binary(ctx, names):
    for name in names:
        for cand in ((name + ".exe", name) if os.name == "nt" else (name,)):
            path = os.path.join(ctx.runtime_dir or "", cand)
            if os.path.isfile(path):
                return path
    raise RuntimeError("no %s in %s" % ("/".join(names), ctx.runtime_dir))


def _free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _get(port, path, timeout=5):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def load(ctx):
    p = ctx.params
    exe = _binary(ctx, ["llama-server"])
    port = _free_port()
    rpc = [str(e) for e in (p.get("rpc") or [])]
    args = [exe, "-m", ctx.model_path, "--host", "127.0.0.1", "--port", str(port),
            "-c", str(int(p.get("ctx") or 2048)), "-np", "1", "--no-ui"]
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = ctx.runtime_dir + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    gpu_layers = int(p.get("gpu_layers") or 0)
    if rpc:
        # every layer goes to the shards' parts, in the planned counts; llama.cpp's
        # own auto-fit keeps a 1 GB margin per device, far too much for phones
        args += ["--rpc", ",".join(rpc), "-ngl", "999", "--fit", "off"]
        split = [str(w) for w in (p.get("tensor_split") or [])]
        if split:
            args += ["-ts", ",".join(split)]
    elif gpu_layers:
        # alone on this device: the last layers on the graphics card, the rest on the CPU
        args += ["-ngl", str(gpu_layers), "--fit", "off"]
        # process-local only: the chosen card, and no AMD switchable-graphics layer
        # (it makes Vulkan device listing hang on hybrid laptops)
        env["DISABLE_LAYER_AMD_SWITCHABLE_GRAPHICS_1"] = "1"
        gpu = p.get("gpu") or {}
        if gpu.get("vk_index") is not None:
            env["GGML_VK_VISIBLE_DEVICES"] = str(int(gpu["vk_index"]))
    else:
        args += ["-ngl", "0"]
    if int(p.get("threads") or 0) > 0:
        args += ["-t", str(int(p["threads"]))]
    args += [str(a) for a in (p.get("extra_args") or [])]
    ctx.log("[head] %s" % " ".join(os.path.basename(a) if i == 0 else a for i, a in enumerate(args)))
    # ctx.spawn ties llama-server to this service process (it dies with it)
    spawn = getattr(ctx, "spawn", None) or subprocess.Popen
    proc = spawn(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                 stdin=subprocess.DEVNULL, cwd=ctx.runtime_dir, env=env)
    keep = ("load_tensors", "llama_kv_cache", "model loaded", "listening", "error", "failed",
            "RPC", "offload", "warn", "memory")

    def pump():
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            TAIL.append(line)
            del TAIL[:-40]
            if any(k.lower() in line.lower() for k in keep) and "slot" not in line:
                ctx.log("[llama] " + line[-300:])

    threading.Thread(target=pump, daemon=True).start()
    started = time.time()
    deadline = started + int(p.get("load_timeout") or 3600)
    last_note = started
    while True:
        if proc.poll() is not None:
            raise RuntimeError("llama-server exited with code %s while loading:\n%s"
                               % (proc.returncode, "\n".join(TAIL[-12:])))
        try:
            if _get(port, "/health", timeout=2).get("status") == "ok":
                break
        except Exception:
            pass
        if time.time() > deadline:
            proc.kill()
            raise RuntimeError("model did not load within %ss" % p.get("load_timeout"))
        if time.time() - last_note > 30:
            last_note = time.time()
            ctx.log("[head] still loading (%ds): weights are being sent to the shards"
                    % int(time.time() - started))
        time.sleep(0.5)

    def watchdog():
        proc.wait()
        ctx.log("[head] llama-server exited (code %s)" % proc.returncode)
        os._exit(3)

    threading.Thread(target=watchdog, daemon=True).start()
    loaded = int(time.time() - started)
    if rpc:
        ctx.log("[head] model loaded in %ds across %d shard(s)" % (loaded, len(rpc)))
    else:
        ctx.log("[head] model loaded in %ds on this device (%s)"
                % (loaded, "%s layers on the GPU" % ("all" if gpu_layers >= 999 else gpu_layers)
                   if gpu_layers else "CPU"))
    ctx.announce({"port": port, "model": os.path.basename(ctx.model_path or ""),
                  "rpc": rpc, "gpu_layers": gpu_layers, "load_seconds": loaded,
                  "ctx": int(p.get("ctx") or 2048)})
    return {"proc": proc, "port": port}


CHAT_KEYS = ("max_tokens", "temperature", "top_p", "top_k", "min_p", "stop", "seed",
             "repeat_penalty", "presence_penalty", "frequency_penalty")


def _build(request):
    """messages + options from JSON {messages|prompt, ...}, or plain text."""
    raw = request.text()
    body = None
    if raw.strip().startswith("{"):
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
    if not isinstance(body, dict):
        body = {"prompt": raw}
    messages = body.get("messages")
    if not messages:
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("send a prompt (text) or JSON {\"messages\": [...]}")
        messages = []
        if body.get("system"):
            messages.append({"role": "system", "content": str(body["system"])})
        messages.append({"role": "user", "content": prompt})
    options = {}
    for key in CHAT_KEYS:
        if key in body:
            options[key] = body[key]
        if key in request.params:
            options[key] = request.params[key]
    options.setdefault("max_tokens", 512)
    return messages, options


def serve(request, ctx, model):
    messages, options = _build(request)
    payload = dict(options, messages=messages, stream=True)
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % model["port"],
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    text = []
    pending = []
    last_emit = time.time()
    finish = None
    timings = {}
    usage = {}
    with urllib.request.urlopen(req, timeout=int(ctx.params.get("request_timeout") or 900)) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            for choice in chunk.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    text.append(piece)
                    pending.append(piece)
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
            timings = chunk.get("timings") or timings
            usage = chunk.get("usage") or usage
            # batch tokens so a fast model does not flood the network
            if pending and time.time() - last_emit > 0.25:
                ctx.emit({"token": "".join(pending)})
                pending = []
                last_emit = time.time()
    if pending:
        ctx.emit({"token": "".join(pending)})
    return {
        "text": "".join(text),
        "finish_reason": finish,
        "tokens": timings.get("predicted_n") or usage.get("completion_tokens"),
        "tokens_per_second": round(float(timings.get("predicted_per_second") or 0), 2),
        "prompt_tokens": timings.get("prompt_n") or usage.get("prompt_tokens"),
        "served_by": ctx.device_name,
    }


def unload(model, ctx):
    proc = model["proc"]
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
'''


def split(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> List[Dict[str, Any]]:
    device_id = str(params.get("device_id") or "")
    name = str(params.get("name") or "")
    model = params.get("model") or {}
    tag = str(params.get("llama") or "")
    if not (device_id and name and model.get("name") and tag):
        raise ValueError("llm_head needs device_id, name, model and llama")
    service_params = {
        "rpc": list(params.get("rpc") or []),
        "tensor_split": list(params.get("tensor_split") or []),
        "ctx": int(params.get("ctx") or 2048),
        "threads": int(params.get("threads") or 0),
        "extra_args": list(params.get("extra_args") or []),
        "request_timeout": int(params.get("request_timeout") or 900),
        "load_timeout": int(params.get("load_timeout") or 3600),
    }
    needs: Dict[str, Any] = {"llama": tag, "model": {"name": model["name"], "size": model.get("size")}}
    if int(params.get("gpu_layers") or 0) and not service_params["rpc"]:
        service_params["gpu_layers"] = int(params["gpu_layers"])
        service_params["gpu"] = params.get("gpu") or {}
        needs["llama_variant"] = "gpu"   # the PC's Vulkan build (tools/llama.cpp/win-vulkan-x64)
    return [{
        "impls": {"python": {"code": HEAD_CODE, "entry": "serve", "args": {}}},
        "inputs": [],
        "timeout": 0,
        "requires": ["service"],
        "device_id": device_id,
        "service": {
            "name": name,
            "request_timeout": service_params["request_timeout"],
            "params": service_params,
            "needs": needs,
            "meta": {"deployment": params.get("deployment"), "role": "head",
                     "accepts": "text or JSON {messages, max_tokens, temperature}",
                     "produces": "text (streamed tokens)", "streaming_output": True},
        },
        "meta": {"deployment": params.get("deployment"), "role": "head"},
    }]


def reduce(params: Dict[str, Any], results: List[Dict[str, Any]], ctx: Any) -> Any:
    return {"stopped": True}
