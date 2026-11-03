"""PhoneCluster command line interface, ``python -m phonecluster`` / ``pc``.

Every command in docs/SPEC.md section 13. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .client import (
    Cluster,
    ClusterError,
    Job,
    JobFailed,
    PhoneClusterError,
    Pipeline,
    discover,
    load_config,
    save_config,
)

__all__ = ["main", "build_parser", "parse_param_value"]


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------


def parse_param_value(text):
    """``--param k=v``: JSON when it parses, otherwise the raw string."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def parse_params(pairs):
    """Turn ``["k=v", ...]`` into a dict; a repeated key becomes a list."""
    params = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise PhoneClusterError("--param needs k=v, got %r" % pair)
        key, value = pair.split("=", 1)
        key = key.strip()
        parsed = parse_param_value(value)
        if key in params:
            if not isinstance(params[key], list):
                params[key] = [params[key]]
            params[key].append(parsed)
        else:
            params[key] = parsed
    return params


def print_table(headers, rows, out=None):
    """Print an aligned table. Empty rows print a dash line."""
    out = out or sys.stdout
    headers = [str(h) for h in headers]
    body = [[("" if c is None else str(c)) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in body:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))
    def line(cells):
        parts = []
        for i, cell in enumerate(cells):
            parts.append(cell.ljust(widths[i]) if i < len(cells) - 1 else cell)
        return "  ".join(parts).rstrip()
    out.write(line(headers) + "\n")
    out.write("  ".join("-" * w for w in widths).rstrip() + "\n")
    for row in body:
        out.write(line(row) + "\n")
    if not body:
        out.write("(none)\n")


def fmt_size(value):
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return ("%.0f%s" % (size, unit)) if unit == "B" else ("%.1f%s" % (size, unit))
        size /= 1024.0
    return str(value)


def fmt_time(value):
    if not value:
        return ""
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(value)))
    except (TypeError, ValueError):
        return str(value)


def fmt_age(value):
    if not value:
        return ""
    try:
        delta = time.time() - float(value)
    except (TypeError, ValueError):
        return str(value)
    if delta < 0:
        delta = 0
    if delta < 60:
        return "%ds" % int(delta)
    if delta < 3600:
        return "%dm" % int(delta / 60)
    if delta < 86400:
        return "%dh" % int(delta / 3600)
    return "%dd" % int(delta / 86400)


def fmt_num(value, digits=1):
    if value is None or value == "":
        return ""
    try:
        return ("%%.%df" % digits) % float(value)
    except (TypeError, ValueError):
        return str(value)


def dump(value, out=None):
    out = out or sys.stdout
    out.write(json.dumps(value, indent=2, sort_keys=False, default=str) + "\n")


def job_progress(data):
    total = data.get("total_tasks") or 0
    done = data.get("done_tasks") or 0
    failed = data.get("failed_tasks") or 0
    if not total:
        return ""
    text = "%d/%d" % (done, total)
    if failed:
        text += " (%d failed)" % failed
    return text


def load_json_file(path):
    if path == "-":
        return json.loads(sys.stdin.read())
    with open(path, "r") as fh:
        text = fh.read()
    try:
        return json.loads(text)
    except ValueError:
        rows = []
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rows.append(json.loads(raw))
            except ValueError:
                rows.append(raw)
        if not rows:
            raise
        return rows


def client(args):
    return Cluster(url=getattr(args, "url", None), pin=getattr(args, "pin", None),
                   timeout=getattr(args, "timeout_http", 30) or 30)


LIST_FILE_PARAMS = ("inputs", "files", "items_files")


def collect_files(cluster, pairs, list_keys=LIST_FILE_PARAMS):
    """``--file k=path``: upload each and return ``{k: file_id}``.

    Keys named in ``list_keys`` (or given more than once) always become a
    list of ids, matching plugin params of type ``files``.
    """
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise PhoneClusterError("--file needs k=path, got %r" % pair)
        key, path = pair.split("=", 1)
        key = key.strip()
        file_id = cluster.files.upload(path)
        sys.stdout.write("uploaded %s -> %s\n" % (path, file_id))
        if key in out:
            if not isinstance(out[key], list):
                out[key] = [out[key]]
            out[key].append(file_id)
        elif key in list_keys:
            out[key] = [file_id]
        else:
            out[key] = file_id
    return out


def wait_and_report(job, quiet=False):
    """Wait for a job; print its result. Returns a process exit code."""
    try:
        job.wait()
    except JobFailed as exc:
        sys.stderr.write("job %s %s: %s\n" % (job.id, job.status, exc.error))
        return 1
    if not quiet:
        dump(job.result())
    return 0


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_discover(args):
    """``pc discover``: list coordinators answering on the LAN."""
    replies = discover(timeout=args.timeout_disc, all_replies=True)
    if not replies:
        sys.stderr.write("no coordinator answered on the LAN (is it running? "
                         "is UDP %d allowed through the firewall?)\n" % 8766)
        return 1
    for reply in replies:
        sys.stdout.write("%s  %s\n" % (reply["url"], reply.get("name") or ""))
    return 0


def cmd_login(args):
    cfg = load_config()
    url = args.url_pos or args.url or os.environ.get("PC_URL") or cfg.get("url")
    pin = args.pin_pos or args.pin or os.environ.get("PC_PIN") or cfg.get("pin")
    if getattr(args, "discover", False) or (url and url.lower() == "auto"):
        sys.stdout.write("looking for a coordinator on the LAN ...\n")
        found = discover()
        if not found:
            sys.stderr.write("no coordinator answered; pass the URL instead\n")
            return 1
        sys.stdout.write("found %s\n" % found)
        url = found
    if not url:
        url = input("coordinator url [http://127.0.0.1:8764]: ").strip() \
            or "http://127.0.0.1:8764"
    if not pin:
        pin = input("pin: ").strip()
    cluster = Cluster(url=url, pin=pin)
    ok = True
    try:
        cluster.stats()
    except ClusterError as exc:
        ok = False
        sys.stderr.write("warning: could not verify (%s)\n" % exc)
    path = save_config(cluster.url, cluster.pin)
    sys.stdout.write("saved %s -> %s%s\n"
                     % (cluster.url, path, "" if ok else "  (unverified)"))
    return 0


def cmd_join(args):
    """A one-time join code for one new device (SPEC 3)."""
    cluster = client(args)
    info = cluster.join()
    minutes = int(info.get("minutes") or 10)
    sys.stdout.write("join code: %s   (works once, for %d minutes)\n" % (info.get("code"), minutes))
    sys.stdout.write("termux, linux, mac: %s\n" % info.get("install_cmd"))
    if info.get("windows_cmd"):
        sys.stdout.write("windows:            %s\n" % info.get("windows_cmd"))
    sys.stdout.write("browser:            %s\n" % info.get("browser_url"))
    sys.stdout.write("by hand:            %s\n" % info.get("manual_cmd"))
    sys.stdout.write("\n")
    if getattr(args, "termux", False):
        sys.stdout.write("scan on the phone, tap Copy, paste into Termux:\n")
        shown = print_qr(info.get("install_cmd") or "")
    else:
        sys.stdout.write("scan on the phone to open the browser worker (--termux: the Termux command):\n")
        shown = print_qr(info.get("browser_url") or "")
    if not shown:
        sys.stdout.write("open the Join page on the dashboard for the QR code\n")
    return 0


def print_qr(text):
    """ASCII QR via the optional ``qrcode`` package (present in the venv)."""
    try:
        import qrcode
    except Exception:
        return False
    try:
        code = qrcode.QRCode(border=1)
        code.add_data(text)
        code.make(fit=True)
        code.print_ascii(out=sys.stdout, invert=True)
        sys.stdout.write("\n")
        return True
    except Exception:
        return False


def cmd_devices(args):
    cluster = client(args)
    rows = []
    for device in cluster.devices():
        battery = device.get("battery")
        if battery is not None:
            battery = "%s%%%s" % (battery, "+" if device.get("charging") else "")
        status = device.get("status") or ""
        if device.get("paused"):
            status += " (paused)"
        rows.append([
            device.get("id"), device.get("name"), device.get("platform"), status,
            battery, fmt_num(device.get("temp")), device.get("slots"),
            fmt_num(device.get("bench_score"), 2),
            ",".join(device.get("caps") or []),
            fmt_age(device.get("last_seen")),
        ])
    print_table(["ID", "NAME", "PLATFORM", "STATUS", "BATT", "TEMP", "SLOTS",
                 "SCORE", "CAPS", "SEEN"], rows)
    return 0


def cmd_device(args):
    dump(client(args).device(args.device_id))
    return 0


def cmd_remove(args):
    client(args).remove_device(args.device_id)
    sys.stdout.write("removed %s\n" % args.device_id)
    return 0


def cmd_pause(args):
    paused = args.command == "pause"
    client(args).pause_device(args.device_id, paused)
    sys.stdout.write("%s %s\n" % ("paused" if paused else "resumed", args.device_id))
    return 0


def _onoff(value):
    if value is None:
        return None
    return str(value).lower() in ("on", "yes", "true", "1")


def cmd_device_set(args):
    """Roles and limits of one connected device."""
    cluster = client(args)
    device_id = cluster.llm.resolve([args.device])[0]
    on = True if args.on else (False if args.off else None)
    device = cluster.device_controls(device_id, on=on, jobs=_onoff(args.jobs), ai=_onoff(args.ai),
                                     storage=_onoff(args.storage), cpu_pct=args.cpu, mem_pct=args.mem,
                                     gpu_pct=args.gpu)
    _print_controls(device)
    return 0


def _print_controls(device):
    control = device.get("control") or {}
    roles = control.get("roles") or {}
    limits = control.get("limits") or {}
    safety = control.get("safety") or {}
    sys.stdout.write("%s: %s\n" % (device.get("name"), "OFF (used for nothing)" if device.get("paused") else "on"))
    sys.stdout.write("  roles : %s\n" % ", ".join("%s %s" % (k, "on" if v else "off") for k, v in sorted(roles.items())))
    sys.stdout.write("  limits: CPU %s%% (%s parallel tasks, %s model threads), memory %s%% (%s for models)"
                     % (limits.get("cpu_pct"), control.get("job_slots"), control.get("threads"),
                        limits.get("mem_pct"), _mb(control.get("usable_mem_mb"))))
    if control.get("gpu"):
        sys.stdout.write(", GPU %s%% (%s for models)" % (limits.get("gpu_pct"), _mb(control.get("gpu_budget_mb"))))
    sys.stdout.write("\n")
    for note in safety.get("notes") or []:
        sys.stdout.write("  note  : %s\n" % note)


def cmd_jobs(args):
    cluster = client(args)
    rows = []
    for job in cluster.jobs(status=args.status, limit=args.limit):
        data = job.data
        rows.append([
            data.get("id"), (data.get("name") or "")[:32], data.get("type"),
            data.get("status"), data.get("priority"), job_progress(data),
            fmt_time(data.get("created")),
        ])
    print_table(["ID", "NAME", "TYPE", "STATUS", "PRIO", "TASKS", "CREATED"], rows)
    return 0


def cmd_job(args):
    cluster = client(args)
    job = cluster.job(args.job_id)
    dump(job.data)
    tasks = job.tasks()
    if tasks:
        rows = []
        for task in sorted(tasks, key=lambda t: t.get("idx") or 0):
            rows.append([task.get("idx"), task.get("id"), task.get("status"),
                         task.get("device_id"), task.get("attempts"),
                         fmt_num(task.get("duration"), 2),
                         (task.get("error") or "")[:40]])
        sys.stdout.write("\n")
        print_table(["IDX", "TASK", "STATUS", "DEVICE", "TRY", "SECS", "ERROR"], rows)
    report = job.data.get("report")
    if report:
        sys.stdout.write("\n%s\n" % speed_line(report))
    return 0


def speed_line(report):
    """One sentence from a job's speed report (SPEC 9.5, 9.8, 9.9)."""
    if report.get("cached"):
        return "answered from the result of identical job %s (saved about %.1fs)" % (
            report.get("from_job"), report.get("saved_s") or 0)
    if not report.get("fastest_device"):
        return "all %d task(s) answered from earlier runs" % (report.get("reused_tasks") or 0)
    best = report.get("fastest_device") or {}
    text = "took %.1fs; %s alone would take about %.1fs" % (report.get("wall_s") or 0, best.get("name"),
                                                            best.get("alone_s") or 0)
    if report.get("single_device"):
        text += " (only %s worked on it)" % best.get("name")
    else:
        text += " -> %.2fx with the cluster" % (report.get("speedup") or 0)
    backups = report.get("backups") or {}
    if backups.get("started"):
        text += "; %d backup cop%s of slow tasks, %d finished first" % (
            backups["started"], "y" if backups["started"] == 1 else "ies", backups.get("won", 0))
    if report.get("reused_tasks"):
        text += "; %d task(s) answered from earlier runs" % report["reused_tasks"]
    checks = report.get("checks") or {}
    if checks.get("checked"):
        text += "; %d task(s) double-checked on another device" % checks["checked"]
        if checks.get("decided"):
            text += ", %d disagreement(s) settled by a third device" % checks["decided"]
        if checks.get("disputed"):
            text += ", %d unresolved" % checks["disputed"]
    return text


def cmd_submit(args):
    cluster = client(args)
    params = parse_params(args.param)
    params.update(collect_files(cluster, args.file))
    targets = args.targets
    if args.all:
        targets = "all"
    elif args.device:
        targets = list(args.device)
    job = cluster.submit(args.type, params, name=args.name,
                         priority=args.priority or 0, timeout=args.timeout,
                         max_attempts=args.max_attempts, targets=targets,
                         chunk_size=args.chunk, requires=args.requires or None,
                         cache=cache_choice(args))
    sys.stdout.write("%s\n" % job.id)
    if args.wait:
        return wait_and_report(job)
    return 0


def cmd_run(args):
    """``pc run <folder|zip>``: submit a job package (docs/PACKAGES.md)."""
    cluster = client(args)
    result = cluster.run_package(args.package, dry_run=args.dry_run)
    if args.dry_run:
        dump(result)
        return 0
    if isinstance(result, Pipeline):
        sys.stdout.write("pipeline %s\n" % result.id)
        for key in sorted(result.jobs):
            sys.stdout.write("  %s %s\n" % (key, result.jobs[key].id))
        if not args.wait:
            return 0
        code = 0
        for key in sorted(result.jobs):
            sys.stdout.write("--- %s ---\n" % key)
            code = wait_and_report(result.jobs[key]) or code
        return code
    sys.stdout.write("%s\n" % result.id)
    if args.wait:
        return wait_and_report(result)
    return 0


def cmd_services(args):
    services = client(args).services()
    if not services:
        sys.stdout.write("no services running (start one with: pc run <service package>)\n")
        return 0
    rows = []
    for svc in services:
        for inst in svc.get("instances") or []:
            rows.append([svc["name"], inst.get("device_name") or inst.get("device_id"),
                         inst.get("status"), str(inst.get("requests_done") or 0),
                         "" if inst.get("avg_ms") is None else "%d ms" % inst["avg_ms"],
                         svc.get("accepts") or "", svc.get("produces") or "", svc.get("description") or ""])
    print_table(["SERVICE", "DEVICE", "STATUS", "DONE", "AVG", "ACCEPTS", "PRODUCES", "DESCRIPTION"], rows)
    return 0


def cmd_call(args):
    cluster = client(args)
    params = parse_params(args.param)
    kwargs = {"params": params or None, "content_type": args.content_type,
              "timeout": args.timeout_req}

    def show_partial(value):
        sys.stderr.write("  partial: %s\n" % json.dumps(value))
        sys.stderr.flush()

    if args.follow:
        kwargs["on_partial"] = show_partial
    if args.text is not None:
        kwargs["text"] = args.text
    elif args.json_input is not None:
        kwargs["json_body"] = parse_param_value(args.json_input)
    elif args.input == "-":
        kwargs["data"] = sys.stdin.buffer.read()
    elif args.input:
        kwargs["path"] = args.input
    else:
        raise PhoneClusterError("give a file, '-' for stdin, --text or --json")
    try:
        result = cluster.call(args.service, **kwargs)
    except ClusterError as exc:
        sys.stderr.write("call failed: %s\n" % exc.message)
        return 1
    outputs = result.get("outputs") or []
    if args.output_dir and outputs:
        os.makedirs(args.output_dir, exist_ok=True)
        for item in outputs:
            dest = os.path.join(args.output_dir, item.get("name") or item.get("file_id"))
            cluster.files.download(item["file_id"], dest)
            sys.stderr.write("saved %s\n" % dest)
    dump(result.get("result"))
    if outputs and not args.output_dir:
        sys.stderr.write("%d output file(s); add -o <dir> to download them\n" % len(outputs))
    return 0


def cmd_service_stop(args):
    cluster = client(args)
    jobs = cluster.stop_service(args.name)
    if not jobs:
        sys.stderr.write("no running service named %r\n" % args.name)
        return 1
    sys.stdout.write("stopped %s (%d job%s)\n" % (args.name, len(jobs), "" if len(jobs) == 1 else "s"))
    return 0


def _mb(value):
    if value is None:
        return ""
    value = float(value)
    return "%.1f GB" % (value / 1024) if value >= 1024 else "%d MB" % value


def _weights(pairs):
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise PhoneClusterError("--weight needs device=number, got %r" % pair)
        key, value = pair.split("=", 1)
        out[key.strip()] = float(value)
    return out


def cmd_llm_models(args):
    rows = []
    for m in client(args).llm.models():
        if m.get("status") and m.get("status") != "done":
            rows.append([m["name"], "%s / %s" % (fmt_size(m.get("bytes")), fmt_size(m.get("total"))),
                         m.get("status"), "", "", m.get("error") or ""])
            continue
        rows.append([m["name"], fmt_size(m.get("size")), m.get("architecture") or "",
                     str(m.get("layers") or ""), str(m.get("context_length") or ""),
                     ", ".join(m.get("used_by") or [])])
    print_table(["MODEL", "SIZE", "ARCH", "LAYERS", "CONTEXT", "USED BY"], rows)
    return 0


def cmd_llm_fetch(args):
    started = client(args).llm.fetch_model(args.url, args.name)
    sys.stdout.write("downloading %s on the coordinator (see 'pc llm models')\n" % started["name"])
    return 0


def cmd_llm_upload(args):
    info = client(args).llm.upload_model(args.path)
    sys.stdout.write("uploaded %s (%s)\n" % (info["name"], fmt_size(info.get("size"))))
    return 0


def cmd_llm_devices(args):
    rows = []
    for d in client(args).llm.devices():
        gpu = ""
        if d.get("gpu"):
            gpu = "%s, %s for models" % ((d["gpu"].get("name") or "GPU")[:24], _mb(d.get("gpu_budget_mb")))
        rows.append([d["name"], d.get("platform") or "", d.get("status") or "", _mb(d.get("mem_free_mb")),
                     _mb(d.get("usable_mb")), gpu, d.get("llama_build") or "",
                     "yes" if d.get("eligible") else "no: %s" % d.get("reason")])
    print_table(["DEVICE", "PLATFORM", "STATUS", "FREE", "USABLE", "GPU", "LLAMA.CPP", "CAN TAKE A MODEL"], rows)
    return 0


def _print_plan(plan):
    rows = []
    for index, d in enumerate(plan["devices"]):
        role = {"host": " (alone)", "shard + host": " (host)"}.get(d["role"], "")
        parts = ", ".join("%s %d" % (p["kind"].upper(), p["layers"]) for p in d.get("parts") or [] if p["layers"])
        budget = sum(p["budget_mb"] for p in d.get("parts") or [])
        rows.append([str(index + 1), d["name"] + role, str(d.get("layers") if d.get("layers") is not None else ""),
                     parts, _mb(d["need_mb"]), _mb(budget), "fits" if d["fits"] else "TOO BIG"])
    print_table(["#", "DEVICE", "LAYERS", "PARTS", "NEEDS", "USABLE", ""], rows)
    if plan.get("summary"):
        sys.stdout.write("%s (%s)\n" % (plan["summary"], plan.get("mode")))
    sys.stdout.write("model %s, context %s; host %s" % (_mb(plan["model_mb"]), _mb(plan["kv_mb"]),
                                                        plan.get("host_name")))
    if plan.get("tokens_per_second"):
        sys.stdout.write("; roughly %s tokens/s" % plan["tokens_per_second"])
    sys.stdout.write("\n")
    for line in plan.get("errors") or []:
        sys.stdout.write("error: %s\n" % line)
    for line in plan.get("warnings") or []:
        sys.stdout.write("warning: %s\n" % line)


def _mode(args):
    if getattr(args, "manual", False):
        return "manual"
    if getattr(args, "auto", False):
        return "auto"
    return None


def cmd_llm_plan(args):
    plan = client(args).llm.plan(args.model, args.on, args.host, _weights(args.weight), args.ctx, _mode(args))
    _print_plan(plan)
    return 0 if plan["ok"] else 1


def cmd_llm_run(args):
    llm = client(args).llm
    dep = llm.deploy(args.name, args.model, args.on, host=args.host, weights=_weights(args.weight),
                     ctx=args.ctx, threads=args.threads, force=args.force, mode=_mode(args))
    sys.stdout.write("starting %s: %s\n" % (dep["name"], (dep.get("options") or {}).get("summary") or ""))
    if not args.wait:
        return 0
    last = None
    while True:
        dep = llm.deployment(dep["id"])
        if dep["status"] != last:
            last = dep["status"]
            sys.stdout.write("  %s%s\n" % (last, (": " + dep["error"]) if dep.get("error") else ""))
            sys.stdout.flush()
        if dep["status"] in ("ready", "failed", "stopped"):
            break
        time.sleep(2)
    if dep["status"] != "ready":
        return 1
    sys.stdout.write("ready. Try: pc llm chat %s\n" % dep["name"])
    return 0


def cmd_llm_list(args):
    rows = []
    for d in client(args).llm.deployments():
        shards = ", ".join("%s:%s" % (s.get("name") or s["device_id"][:8], s.get("layers") if s.get("layers") is not None else "?")
                           for s in d.get("shards") or [])
        note = d.get("error") or ("kept loaded" if not d.get("idle_minutes")
                                  else "unloads after %g min unused" % d["idle_minutes"])
        copies = len(d.get("copies") or [])
        rows.append([d["name"], d["status"], d["model"], d.get("host_name") or d["host_device"][:8],
                     shards or "alone", str(1 + copies) if d["status"] == "ready" else "-", note[:50]])
    print_table(["NAME", "STATUS", "MODEL", "HOST", "DEVICES:LAYERS", "COPIES", "NOTE"], rows)
    return 0


def cmd_llm_stop(args):
    dep = client(args).llm.stop(args.name)
    sys.stdout.write("%s %s\n" % (dep["name"], dep["status"]))
    return 0


def cmd_llm_restart(args):
    dep = client(args).llm.restart(args.name)
    sys.stdout.write("%s %s\n" % (dep["name"], dep["status"]))
    return 0


def cmd_llm_rm(args):
    client(args).llm.remove(args.name)
    sys.stdout.write("removed %s\n" % args.name)
    return 0


def cmd_llm_keep(args):
    dep = client(args).llm.keep(args.name, args.state == "on")
    if args.state == "on":
        sys.stdout.write("%s stays loaded\n" % dep["name"])
    else:
        sys.stdout.write("%s unloads after %g unused minutes\n" % (dep["name"], dep.get("idle_minutes") or 0))
    return 0


def cmd_llm_default(args):
    llm = client(args).llm
    info = llm.set_default(args.model) if args.model else llm.default()
    if info.get("model"):
        sys.stdout.write("'chat' reaches %s (%s)\n" % (info["name"], info["model"]))
    else:
        sys.stdout.write("no default model: set one with 'pc llm default <model.gguf>'\n")
    return 0


def cmd_speeds(args):
    rows = [[r["key"], r.get("device") or r["device_id"], "%g" % r["rate"], str(r["samples"]), fmt_age(r.get("updated"))]
            for r in client(args).speeds()]
    print_table(["KIND OF WORK", "DEVICE", "UNITS/S", "SAMPLES", "UPDATED"], rows)
    return 0


def _stream_token(text):
    sys.stdout.write(text)
    sys.stdout.flush()


def cmd_llm_ask(args):
    result = client(args).llm.chat(args.name, prompt=args.prompt, system=args.system,
                                   max_tokens=args.max_tokens, temperature=args.temperature,
                                   on_token=_stream_token)
    sys.stdout.write("\n")
    sys.stderr.write("(%s tokens, %s tok/s)\n" % (result.get("tokens"), result.get("tokens_per_second")))
    return 0


def cmd_llm_chat(args):
    """Interactive chat that keeps the conversation history."""
    llm = client(args).llm
    history = []
    if args.system:
        history.append({"role": "system", "content": args.system})
    sys.stdout.write("chatting with %s (empty line or Ctrl+C to quit, /new to forget)\n" % args.name)
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.stdout.write("\n")
            return 0
        if not line:
            return 0
        if line == "/new":
            history = history[:1] if args.system else []
            continue
        history.append({"role": "user", "content": line})
        sys.stdout.write("%s> " % args.name)
        sys.stdout.flush()
        try:
            result = llm.chat(args.name, messages=history, max_tokens=args.max_tokens,
                              temperature=args.temperature, on_token=_stream_token)
        except ClusterError as exc:
            sys.stdout.write("\n(error: %s)\n" % exc.message)
            history.pop()
            continue
        sys.stdout.write("\n")
        history.append({"role": "assistant", "content": result.get("text") or ""})


def cmd_requests(args):
    rows = []
    for req in client(args).requests(limit=args.limit):
        rows.append([req["id"], req.get("service") or "", req.get("status") or "",
                     "" if req.get("ms") is None else "%d ms" % req["ms"],
                     str(req.get("bytes_in") or 0), (req.get("error") or "")[:40]])
    print_table(["ID", "SERVICE", "STATUS", "TIME", "BYTES", "ERROR"], rows)
    return 0


def cmd_map(args):
    cluster = client(args)
    if ":" not in args.target:
        raise PhoneClusterError("map target must be <file.py>:<function>")
    path, fn_name = args.target.rsplit(":", 1)
    fn = import_function(path, fn_name)
    if args.lines:
        # every line of a big file is an item; results stream to JSONL
        job = cluster.map_lines(fn, args.items, lines_per_task=args.chunk or None,
                                line_format=args.line_format, name=args.name,
                                timeout=args.timeout or 600, stream_results=True)
        sys.stdout.write("%s\n" % job.id)
        if args.wait:
            try:
                job.wait()
            except JobFailed as exc:
                sys.stderr.write("job %s %s: %s\n" % (job.id, job.status, exc.error))
                return 1
            summary = job.result()
            sys.stdout.write("%s items -> results file %s (pc results %s)\n"
                             % (summary.get("count"), summary.get("results_file"), job.id))
        return 0
    items = load_json_file(args.items)
    if not isinstance(items, list):
        raise PhoneClusterError("items must be a JSON list, got %s"
                                % type(items).__name__)
    job = cluster.map(fn, items, chunk_size=args.chunk or None, name=args.name,
                      timeout=args.timeout or 600, stream_results=args.stream)
    sys.stdout.write("%s\n" % job.id)
    if args.wait:
        return wait_and_report(job)
    return 0


def import_function(path, name):
    import importlib.util

    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise PhoneClusterError("no such file: %s" % path)
    spec = importlib.util.spec_from_file_location(
        "pc_map_%d" % abs(hash(path)), path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    fn = getattr(module, name, None)
    if not callable(fn):
        raise PhoneClusterError("%s has no function %r" % (path, name))
    return fn


def cmd_shell(args):
    cluster = client(args)
    targets = "any"
    if args.all:
        targets = "all"
    elif args.device:
        targets = list(args.device)
    job = cluster.shell(args.cmd, targets=targets, timeout=args.timeout or 600)
    sys.stdout.write("%s\n" % job.id)
    if args.wait:
        try:
            job.wait()
        except JobFailed as exc:
            sys.stderr.write("job %s %s: %s\n" % (job.id, job.status, exc.error))
            return 1
        result = job.result()
        if isinstance(result, dict):
            for device_id, out in result.items():
                sys.stdout.write("--- %s ---\n" % device_id)
                if isinstance(out, dict):
                    sys.stdout.write(out.get("stdout") or "")
                    if out.get("stderr"):
                        sys.stderr.write(out["stderr"])
                else:
                    dump(out)
        else:
            dump(result)
    return 0


def cache_choice(args):
    """--fresh / --no-cache -> the job's ``cache`` option (SPEC 9.8)."""
    if getattr(args, "fresh", False):
        return "refresh"
    if getattr(args, "no_cache", False):
        return False
    return None


def cmd_wasm(args):
    """``pc wasm module.wasm [--call f] [--range a b] [--input f] ...``"""
    cluster = client(args)
    args_list = None
    if args.args:
        args_list = [json.loads(a) if a.strip().startswith("[") else [a] for a in args.args]
    stdin = None
    if args.stdin is not None:
        stdin = sys.stdin.read() if args.stdin == "-" else args.stdin
    job = cluster.wasm(args.module, call=args.call, range=args.range, args_list=args_list,
                       inputs=args.input or None, stdin=stdin, combine=args.combine,
                       grain=args.grain, memory_mb=args.memory, name=args.name,
                       timeout=args.timeout or 600, cache=cache_choice(args))
    sys.stdout.write("%s\n" % job.id)
    if args.wait:
        return wait_and_report(job)
    return 0


def cmd_rerun(args):
    job = client(args).rerun(args.job_id, fresh=args.fresh)
    sys.stdout.write("%s\n" % job.id)
    if args.wait:
        return wait_and_report(job)
    return 0


def cmd_cache(args):
    cluster = client(args)
    if args.action == "clear":
        removed = cluster.clear_cache().get("removed")
        sys.stdout.write("removed %s stored result(s)\n" % removed)
        return 0
    info = cluster.cache_info(limit=args.limit)
    stats = info.get("stats") or {}
    sys.stdout.write("%s stored result(s) (%s jobs, %s tasks), %s, reused %s time(s), saved about %ss%s\n" % (
        stats.get("entries"), stats.get("jobs"), stats.get("tasks"), fmt_size(stats.get("bytes")),
        stats.get("hits"), stats.get("saved_s"), "" if stats.get("enabled") else " (reuse is OFF)"))
    rows = [[e.get("key", "")[:12], e.get("kind"), e.get("job_type"), fmt_size(e.get("size")),
             e.get("hits"), e.get("source_job"), fmt_time(e.get("used"))]
            for e in info.get("entries") or []]
    if rows:
        print_table(["KEY", "KIND", "TYPE", "SIZE", "HITS", "FROM JOB", "LAST USED"], rows)
    return 0


def cmd_trust(args):
    cluster = client(args)
    if args.again:
        view = cluster.trust_again(args.again)
        sys.stdout.write("%s: %s (score %.2f)\n" % (args.again, view.get("state"), view.get("score") or 0))
        return 0
    rows = [[d.get("name"), d.get("state"), "%.2f" % (d.get("score") or 0), d.get("checks"),
             d.get("agreed"), d.get("wrong"), d.get("note") or ""] for d in cluster.trust()]
    print_table(["DEVICE", "STATE", "SCORE", "CHECKS", "AGREED", "WRONG", "NOTE"], rows)
    return 0


def cmd_cancel(args):
    job = client(args).job(args.job_id).cancel()
    sys.stdout.write("%s %s\n" % (job.id, job.status))
    return 0


def cmd_retry(args):
    job = client(args).job(args.job_id).retry()
    sys.stdout.write("%s %s\n" % (job.id, job.status))
    return 0


def cmd_result(args):
    dump(client(args).job(args.job_id).result())
    return 0


def cmd_results(args):
    """``pc results <job> [-o file]``: stream the job's items as JSON lines."""
    job = Job(client(args), {"id": args.job_id})
    if args.output:
        path = job.download_results(args.output, wait=args.wait)
        sys.stdout.write("%s\n" % path)
        return 0
    count = 0
    for item in job.iter_results(wait=args.wait):
        sys.stdout.write(json.dumps(item) + "\n")
        count += 1
    if not count:
        sys.stderr.write("(no results yet)\n")
    return 0


def cmd_export(args):
    cluster = client(args)
    job = Job(cluster, {"id": args.job_id})
    dest = args.output or ("%s.%s" % (args.job_id, args.format))
    path = job.export(dest, args.format)
    sys.stdout.write("%s\n" % path)
    return 0


def cmd_logs(args):
    cluster = client(args)
    is_job = True
    try:
        job = cluster.job(args.target)
    except ClusterError as exc:
        if exc.status not in (404, 400):
            raise
        is_job = False
    if not is_job:
        return follow_task_log(cluster, args.target, args.follow)
    seen = {}

    def flush():
        for task in sorted(job.tasks(), key=lambda t: t.get("idx") or 0):
            task_id = task.get("id")
            if args.task is not None and task.get("idx") != args.task:
                continue
            text = cluster.task_log(task_id)
            lines = text.splitlines()
            start = seen.get(task_id, 0)
            if start == 0 and lines:
                sys.stdout.write("--- task %s (%s) ---\n" % (task.get("idx"), task_id))
            for line in lines[start:]:
                sys.stdout.write(line + "\n")
            seen[task_id] = len(lines)
        sys.stdout.flush()

    flush()
    if args.follow:
        while not job.refresh().done:
            time.sleep(1.0)
            flush()
        flush()
    return 0


def follow_task_log(cluster, task_id, follow):
    seen = 0
    while True:
        text = cluster.task_log(task_id)
        lines = text.splitlines()
        for line in lines[seen:]:
            sys.stdout.write(line + "\n")
        seen = len(lines)
        sys.stdout.flush()
        if not follow:
            return 0
        task = cluster.task(task_id)
        if (task or {}).get("status") in ("done", "failed", "cancelled"):
            return 0
        time.sleep(1.0)


def cmd_files(args):
    cluster = client(args)
    if args.action == "list":
        rows = [[f.get("id"), f.get("name"), fmt_size(f.get("size")),
                 f.get("mime"), fmt_time(f.get("created"))]
                for f in cluster.files.list()]
        print_table(["ID", "NAME", "SIZE", "MIME", "CREATED"], rows)
    elif args.action == "upload":
        sys.stdout.write("%s\n" % cluster.files.upload(args.a))
    elif args.action == "download":
        sys.stdout.write("%s\n" % cluster.files.download(args.a, args.b or "."))
    elif args.action == "rm":
        cluster.files.delete(args.a)
        sys.stdout.write("deleted %s\n" % args.a)
    return 0


def cmd_storage(args):
    cluster = client(args)
    if args.action == "list":
        rows = [[b.get("id"), b.get("name"), fmt_size(b.get("size")),
                 b.get("n_chunks"), (b.get("protection") or {}).get("label") or b.get("replication"),
                 b.get("status"), fmt_time(b.get("created"))]
                for b in cluster.storage.list()]
        print_table(["ID", "NAME", "SIZE", "CHUNKS", "PROTECTION", "STATUS", "CREATED"], rows)
    elif args.action == "put":
        sys.stdout.write("%s\n" % cluster.storage.put(
            args.a, replication=args.replication, scheme=args.scheme))
    elif args.action == "get":
        sys.stdout.write("%s\n" % cluster.storage.get(args.a, args.b or "."))
    elif args.action == "rm":
        cluster.storage.delete(args.a)
        sys.stdout.write("deleted %s\n" % args.a)
    elif args.action == "status":
        dump(cluster.storage.status(args.a))
    return 0


def cmd_schedules(args):
    cluster = client(args)
    if args.action == "list":
        rows = [[s.get("id"), s.get("name"), s.get("cron"),
                 "yes" if s.get("enabled") else "no",
                 fmt_time(s.get("last_run")), fmt_time(s.get("next_run"))]
                for s in cluster.schedules.list()]
        print_table(["ID", "NAME", "CRON", "ON", "LAST", "NEXT"], rows)
    elif args.action == "add":
        spec = load_json_file(args.c)
        created = cluster.schedules.create(args.a, args.b, spec)
        sys.stdout.write("%s\n" % (created.get("id") if isinstance(created, dict)
                                   else created))
    elif args.action == "rm":
        cluster.schedules.delete(args.a)
        sys.stdout.write("deleted %s\n" % args.a)
    elif args.action == "run":
        dump(cluster.schedules.run(args.a))
    return 0


def cmd_templates(args):
    cluster = client(args)
    if args.action == "list":
        rows = []
        for tpl in cluster.templates.list():
            spec = tpl.get("job_spec") or {}
            if isinstance(spec, str):
                try:
                    spec = json.loads(spec)
                except ValueError:
                    spec = {}
            rows.append([tpl.get("id"), tpl.get("name"), spec.get("type"),
                         fmt_time(tpl.get("created"))])
        print_table(["ID", "NAME", "TYPE", "CREATED"], rows)
    elif args.action == "save":
        created = cluster.templates.create(args.a, load_json_file(args.b))
        sys.stdout.write("%s\n" % (created.get("id") if isinstance(created, dict)
                                   else created))
    elif args.action == "rm":
        cluster.templates.delete(args.a)
        sys.stdout.write("deleted %s\n" % args.a)
    return 0


def cmd_terminal(args):
    return client(args).terminal(args.device_id) or 0


def cmd_stats(args):
    cluster = client(args)
    stats = cluster.stats()
    devices = stats.get("devices") or {}
    jobs = stats.get("jobs") or {}
    sys.stdout.write("devices  online %s / %s   cores %s   free slots %s\n" % (
        devices.get("online", 0), devices.get("total", 0),
        devices.get("cores", 0), devices.get("slots_free", 0)))
    sys.stdout.write("jobs     running %s   pending %s   done(24h) %s   failed(24h) %s\n"
                     % (jobs.get("running", 0), jobs.get("pending", 0),
                        jobs.get("done_24h", 0), jobs.get("failed_24h", 0)))
    sys.stdout.write("tasks/min %s\n\n" % fmt_num(stats.get("tasks_per_min"), 2))
    rows = [[i + 1, e.get("name"), e.get("device_id"),
             fmt_num(e.get("bench_score"), 2), e.get("tasks_done")]
            for i, e in enumerate(stats.get("leaderboard") or [])]
    print_table(["#", "NAME", "DEVICE", "SCORE", "TASKS"], rows)
    return 0


def cmd_events(args):
    cluster = client(args)
    rows = [[fmt_time(e.get("ts")), e.get("kind"), e.get("device_id") or "",
             e.get("job_id") or "", (e.get("message") or "")[:60]]
            for e in cluster.events(limit=args.limit, kind=args.kind)]
    print_table(["TIME", "KIND", "DEVICE", "JOB", "MESSAGE"], rows)
    return 0


def cmd_plugins(args):
    cluster = client(args)
    rows = []
    for plugin in cluster.plugins():
        names = [p.get("name") for p in (plugin.get("params") or [])
                 if isinstance(p, dict)]
        rows.append([plugin.get("type"), (plugin.get("description") or "")[:44],
                     ", ".join(n for n in names if n)])
    print_table(["TYPE", "DESCRIPTION", "PARAMS"], rows)
    return 0


def cmd_bench(args):
    cluster = client(args)
    result = cluster.bench(args.device_id)
    if isinstance(result, Job):
        sys.stdout.write("%s\n" % result.id)
        if args.wait:
            return wait_and_report(result)
        return 0
    dump(result)
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pc", description="PhoneCluster CLI (see docs/SPEC.md section 13)")
    parser.add_argument("-u", "--url", help="coordinator url (env PC_URL)")
    parser.add_argument("-p", "--pin", help="dashboard pin (env PC_PIN)")
    parser.add_argument("--http-timeout", dest="timeout_http", type=float,
                        default=30, help="per-request timeout in seconds")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("login", help="save url + pin to data/cli.json")
    p.add_argument("url_pos", nargs="?", help="coordinator url (or 'auto')")
    p.add_argument("pin_pos", nargs="?", help="pin")
    p.add_argument("--discover", action="store_true",
                   help="find the coordinator by LAN broadcast instead of a url")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("discover", help="list coordinators answering on the LAN")
    p.add_argument("--timeout", dest="timeout_disc", type=float, default=2.0)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("join", help="a one-time join code for a new device, its commands and a QR")
    p.add_argument("--termux", action="store_true",
                   help="show the QR of the Termux command instead of the browser link")
    p.set_defaults(func=cmd_join)

    p = sub.add_parser("devices", help="list devices")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("device", help="show one device")
    p.add_argument("device_id")
    p.set_defaults(func=cmd_device)

    p = sub.add_parser("remove", help="remove a device")
    p.add_argument("device_id")
    p.set_defaults(func=cmd_remove)

    for name in ("pause", "resume"):
        p = sub.add_parser(name, help="%s a device" % name)
        p.add_argument("device_id")
        p.set_defaults(func=cmd_pause)

    p = sub.add_parser("device-set", help="what a device is used for and how hard it may work")
    p.add_argument("device", help="device name or id")
    p.add_argument("--on", action="store_true", help="use the device (master switch)")
    p.add_argument("--off", action="store_true", help="use the device for nothing, keep it connected")
    p.add_argument("--jobs", choices=["on", "off"], help="normal tasks")
    p.add_argument("--ai", choices=["on", "off"], help="AI models and services")
    p.add_argument("--storage", choices=["on", "off"], help="pooled storage chunks")
    p.add_argument("--cpu", type=int, metavar="PCT", help="share of the cores (10-100; one core always stays idle)")
    p.add_argument("--mem", type=int, metavar="PCT", help="share of free memory a model may use (10-70)")
    p.add_argument("--gpu", type=int, metavar="PCT", help="share of graphics memory a model may use (0-90)")
    p.set_defaults(func=cmd_device_set)

    p = sub.add_parser("speeds", help="what the cluster learned about each device's speed")
    p.set_defaults(func=cmd_speeds)

    p = sub.add_parser("jobs", help="list jobs")
    p.add_argument("--status", help="filter by status")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("job", help="show a job and its tasks")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_job)

    p = sub.add_parser("submit", help="submit any plugin job")
    p.add_argument("type")
    p.add_argument("--param", action="append", metavar="K=V",
                   help="plugin parameter, JSON-parsed when possible")
    p.add_argument("--file", action="append", metavar="K=PATH",
                   help="upload PATH and pass its file id as parameter K")
    p.add_argument("--name")
    p.add_argument("--priority", type=int, default=0)
    p.add_argument("--timeout", type=float)
    p.add_argument("--max-attempts", dest="max_attempts", type=int)
    p.add_argument("--chunk", type=int)
    p.add_argument("--requires", action="append")
    p.add_argument("--targets", help='"any", "all" or a device id')
    p.add_argument("--device", action="append", help="target this device id")
    p.add_argument("--all", action="store_true", help="target every online device")
    p.add_argument("--fresh", action="store_true", help="compute again and replace a stored result")
    p.add_argument("--no-cache", dest="no_cache", action="store_true", help="do not reuse or keep results")
    p.add_argument("--wait", action="store_true")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("services", help="list running services and their instances")
    p.set_defaults(func=cmd_services)

    p = sub.add_parser("call", help="send a request to a running service")
    p.add_argument("service")
    p.add_argument("input", nargs="?", metavar="file|-", help="file to send, or - for stdin")
    p.add_argument("--text", help="send this text instead of a file")
    p.add_argument("--json", dest="json_input", help="send this JSON instead of a file")
    p.add_argument("--param", action="append", metavar="K=V", help="request parameter")
    p.add_argument("--content-type", dest="content_type")
    p.add_argument("--timeout", dest="timeout_req", type=float, help="seconds to wait for the answer")
    p.add_argument("--follow", action="store_true", help="print partial results as they arrive")
    p.add_argument("-o", "--output-dir", dest="output_dir", help="download output files here")
    p.set_defaults(func=cmd_call)

    p = sub.add_parser("service", help="manage services (start one with 'pc run <package>')")
    ssub = p.add_subparsers(dest="service_cmd")
    sp = ssub.add_parser("stop", help="stop every instance of a service")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_service_stop)
    sp = ssub.add_parser("list", help="same as 'pc services'")
    sp.set_defaults(func=cmd_services)

    p = sub.add_parser("llm", help="run one language model across chosen devices")
    lsub = p.add_subparsers(dest="llm_cmd")
    lp = lsub.add_parser("models", help="GGUF models on the coordinator")
    lp.set_defaults(func=cmd_llm_models)
    lp = lsub.add_parser("fetch", help="download a .gguf from a URL onto the coordinator")
    lp.add_argument("url")
    lp.add_argument("--name")
    lp.set_defaults(func=cmd_llm_fetch)
    lp = lsub.add_parser("upload", help="upload a local .gguf to the coordinator")
    lp.add_argument("path")
    lp.set_defaults(func=cmd_llm_upload)
    lp = lsub.add_parser("devices", help="devices, their memory and llama.cpp build")
    lp.set_defaults(func=cmd_llm_devices)
    for cmd, func, text in (("plan", cmd_llm_plan, "preview where a model would run"),
                            ("run", cmd_llm_run, "start a model (automatic placement by default)")):
        lp = lsub.add_parser(cmd, help=text)
        lp.add_argument("model", help="model file name (see 'pc llm models')")
        lp.add_argument("--on", action="append", metavar="DEVICE",
                        help="automatic: a device it may use (default: any); manual: a device that "
                             "holds part of the model; repeat")
        lp.add_argument("--host", metavar="DEVICE", help="manual: device that keeps the model file and serves chats")
        lp.add_argument("--weight", action="append", metavar="DEVICE=N",
                        help="manual: share weight for a device (default: by usable memory)")
        lp.add_argument("--manual", action="store_true", help="use exactly the --on devices")
        lp.add_argument("--auto", action="store_true",
                        help="pick the devices (the default without --host/--weight)")
        lp.add_argument("--ctx", type=int, default=2048, help="context length in tokens")
        lp.set_defaults(func=func)
        if cmd == "run":
            lp.add_argument("--name", required=True, help="name callers use")
            lp.add_argument("--threads", type=int, default=0)
            lp.add_argument("--force", action="store_true", help="start even if the plan says it will not fit")
            lp.add_argument("--wait", action="store_true", help="wait until the model is ready")
    lp = lsub.add_parser("list", help="running and stopped models")
    lp.set_defaults(func=cmd_llm_list)
    for cmd, func in (("stop", cmd_llm_stop), ("restart", cmd_llm_restart), ("rm", cmd_llm_rm)):
        lp = lsub.add_parser(cmd)
        lp.add_argument("name")
        lp.set_defaults(func=func)
    lp = lsub.add_parser("keep", help="keep a model loaded (on) or let it unload when unused (off)")
    lp.add_argument("name")
    lp.add_argument("state", choices=["on", "off"])
    lp.set_defaults(func=cmd_llm_keep)
    lp = lsub.add_parser("default", help="show or set the model the name 'chat' reaches")
    lp.add_argument("model", nargs="?", help="model file name")
    lp.set_defaults(func=cmd_llm_default)
    for cmd, func in (("ask", cmd_llm_ask), ("chat", cmd_llm_chat)):
        lp = lsub.add_parser(cmd, help="one question" if cmd == "ask" else "interactive chat")
        lp.add_argument("name")
        if cmd == "ask":
            lp.add_argument("prompt")
        lp.add_argument("--system", help="system prompt")
        lp.add_argument("--max-tokens", dest="max_tokens", type=int, default=512)
        lp.add_argument("--temperature", type=float, default=None)
        lp.set_defaults(func=func)

    p = sub.add_parser("requests", help="recent service requests")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_requests)

    p = sub.add_parser("run", help="submit a job package (folder or .zip with job.yaml)")
    p.add_argument("package", metavar="folder|package.zip")
    p.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="show the resolved job spec without submitting")
    p.add_argument("--wait", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("map", help="map a python function over a JSON list (or the lines of a big file)")
    p.add_argument("target", metavar="file.py:fn")
    p.add_argument("items", metavar="items.json|-|bigfile.txt")
    p.add_argument("--chunk", type=int, default=None,
                   help="items (with --lines: lines) per task; default: sized to each device's speed")
    p.add_argument("--lines", action="store_true",
                   help="treat ITEMS as a big text/JSONL file: one item per line, streamed by byte range")
    p.add_argument("--line-format", dest="line_format", choices=["auto", "text", "json"], default="auto")
    p.add_argument("--stream", action="store_true",
                   help="write results to a JSONL file as tasks finish (see 'pc results')")
    p.add_argument("--name")
    p.add_argument("--timeout", type=float)
    p.add_argument("--wait", action="store_true")
    p.set_defaults(func=cmd_map)

    p = sub.add_parser("shell", help="run a shell command on the cluster")
    p.add_argument("cmd")
    p.add_argument("--all", action="store_true", help="every online device")
    p.add_argument("--device", action="append", help="target this device id")
    p.add_argument("--timeout", type=float)
    p.add_argument("--wait", action="store_true")
    p.set_defaults(func=cmd_shell)

    p = sub.add_parser("wasm", help="run one WebAssembly module on every device (PC, phones, browsers)")
    p.add_argument("module", metavar="module.wasm|file-id")
    p.add_argument("--call", help="exported function to call with numbers (else: run as a WASI program)")
    p.add_argument("--range", type=int, nargs=2, metavar=("START", "END"),
                   help="call f(lo, hi) over pieces of this range, sized to each device; results are added")
    p.add_argument("--grain", type=int, help="pieces of the range are multiples of this")
    p.add_argument("--args", action="append",
                   help="one task: numbers for --call, a command line for a program; JSON list or one value")
    p.add_argument("--input", action="append", help="program mode: one task per file (path or file id)")
    p.add_argument("--stdin", help="text for the program's stdin (- = read it from here)")
    p.add_argument("--combine", choices=["auto", "list", "sum", "text"], default="auto")
    p.add_argument("--memory", type=int, help="memory limit per task in MB (default 256)")
    p.add_argument("--name")
    p.add_argument("--timeout", type=float)
    p.add_argument("--fresh", action="store_true", help="compute again and replace a stored result")
    p.add_argument("--no-cache", dest="no_cache", action="store_true", help="do not reuse or keep results")
    p.add_argument("--wait", action="store_true")
    p.set_defaults(func=cmd_wasm)

    p = sub.add_parser("rerun", help="submit a job again (--fresh: ignore stored results)")
    p.add_argument("job_id")
    p.add_argument("--fresh", action="store_true")
    p.add_argument("--wait", action="store_true")
    p.set_defaults(func=cmd_rerun)

    p = sub.add_parser("cache", help="stored results that answer identical work (show|clear)")
    p.add_argument("action", nargs="?", choices=["show", "clear"], default="show")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_cache)

    p = sub.add_parser("trust", help="how each device's double-checked answers went")
    p.add_argument("--again", metavar="DEVICE", help="forgive a device that gave wrong answers")
    p.set_defaults(func=cmd_trust)

    p = sub.add_parser("cancel", help="cancel a job")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("retry", help="retry a job's failed tasks")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("logs", help="print logs of a job or task")
    p.add_argument("target", metavar="job|task")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--task", type=int, help="only this task index")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("result", help="print a job result")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_result)

    p = sub.add_parser("results", help="stream a job's items as JSON lines")
    p.add_argument("job_id")
    p.add_argument("-o", "--output", help="write to this file instead of stdout")
    p.add_argument("--wait", action="store_true", help="wait for the job to finish first")
    p.set_defaults(func=cmd_results)

    p = sub.add_parser("export", help="download a job export")
    p.add_argument("job_id")
    p.add_argument("--format", choices=["zip", "json", "csv"], default="zip")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("files", help="list|upload <path>|download <id> <dest>|rm <id>")
    p.add_argument("action", choices=["list", "upload", "download", "rm"])
    p.add_argument("a", nargs="?")
    p.add_argument("b", nargs="?")
    p.set_defaults(func=cmd_files)

    p = sub.add_parser("storage",
                       help="list|put <path>|get <id> <dest>|rm <id>|status <id>")
    p.add_argument("action", choices=["list", "put", "get", "rm", "status"])
    p.add_argument("a", nargs="?")
    p.add_argument("b", nargs="?")
    p.add_argument("--replication", type=int, help="this many full copies")
    p.add_argument("--scheme", choices=["auto", "copies", "ec"],
                   help="auto: parity pieces (erasure coding) from 3 storage phones on")
    p.set_defaults(func=cmd_storage)

    p = sub.add_parser("schedules",
                       help='list|add <name> "<cron>" <jobspec.json>|rm <id>|run <id>')
    p.add_argument("action", choices=["list", "add", "rm", "run"])
    p.add_argument("a", nargs="?")
    p.add_argument("b", nargs="?")
    p.add_argument("c", nargs="?")
    p.set_defaults(func=cmd_schedules)

    p = sub.add_parser("templates",
                       help="list|save <name> <jobspec.json>|rm <id>")
    p.add_argument("action", choices=["list", "save", "rm"])
    p.add_argument("a", nargs="?")
    p.add_argument("b", nargs="?")
    p.set_defaults(func=cmd_templates)

    p = sub.add_parser("terminal", help="interactive line shell on a device")
    p.add_argument("device_id")
    p.set_defaults(func=cmd_terminal)

    p = sub.add_parser("stats", help="cluster stats and leaderboard")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("events", help="recent events")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--kind")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("plugins", help="available job types")
    p.set_defaults(func=cmd_plugins)

    p = sub.add_parser("bench", help="benchmark one device or all of them")
    p.add_argument("device_id", nargs="?")
    p.add_argument("--wait", action="store_true")
    p.set_defaults(func=cmd_bench)

    return parser


REQUIRED_ARGS = {
    ("files", "upload"): ["a"], ("files", "download"): ["a"], ("files", "rm"): ["a"],
    ("storage", "put"): ["a"], ("storage", "get"): ["a"], ("storage", "rm"): ["a"],
    ("storage", "status"): ["a"],
    ("schedules", "add"): ["a", "b", "c"], ("schedules", "rm"): ["a"],
    ("schedules", "run"): ["a"],
    ("templates", "save"): ["a", "b"], ("templates", "rm"): ["a"],
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    required = REQUIRED_ARGS.get((args.command, getattr(args, "action", None)))
    if required and any(getattr(args, name, None) is None for name in required):
        parser.error("%s %s needs %d more argument(s)"
                     % (args.command, args.action, len(required)))
    if not hasattr(args, "func"):
        parser.error("%s needs a sub-command (see --help)" % args.command)
    try:
        return args.func(args) or 0
    except JobFailed as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    except ClusterError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    except PhoneClusterError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    except (OSError, ValueError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
