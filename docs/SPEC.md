# PhoneCluster — System Contract (SPEC)

Version 1.0 — 2026-09-10. Every component in this repo MUST follow this document.
If you need to deviate, add a note in `docs/DEVIATIONS.md` explaining why.

## 0. Goals and hard rules

- Turn old Android phones (Redmi 5A / Android 7, Redmi Note 10 / Android 11, Samsung J2 / Android 5.1 with 1 GB RAM)
  plus a Windows PC into a **zero-cost LAN compute + storage cluster**.
- **Adding a device = scan a QR / run one command. Removing = close the app or click Remove.** Nothing else.
- **No root, ever.** Everything must work on stock, unrooted Android and on a normal Windows user account.
- **Everything lives inside the project folder** (`<project>`: wherever it was put; `D:\cluster` on the author's PC). Every file, dependency, cache, log and temporary file of the cluster stays inside it, now and in every later change. The PC runs the cluster's own Python, `<project>\tools\python` (the official embeddable package, isolated by its `._pth` file); its deps are installed there with `PIP_CACHE_DIR=<project>\.pip-cache`.
  Data goes to `<project>\data`. Tools (ffmpeg) go to `<project>\tools`. No global installs, no global settings, no CDNs, no internet needed at runtime. Paths are found from the scripts' own location, never written in.
- Phones run a **single-file, stdlib-only Python worker** (Termux) or a **browser worker** (any Chrome). No pip on phones.
- The PC is both **coordinator** and **worker**.

## 1. Repository layout

```
<project>\
  tools\python\               the cluster's own Python 3.12 (embeddable; scripts/get-python.ps1; §16)
  .venv\                      older project venv, fallback only if tools\python is missing
  .pip-cache\                 pip cache                                [create on demand]
  data\                       runtime data (gitignored)
    config.yaml               coordinator config (auto-created on first start)
    cluster.db                SQLite database
    files\                    content-addressed file store: <sha256>
    storage\cache\            LRU cache of pooled-storage chunks
    logs\tasks\<task_id>.log  task log lines
    worker\                   PC worker state dir (device.json, cache\, store\)
  tools\ffmpeg\ffmpeg.exe     static ffmpeg (+ ffprobe.exe) for the PC worker
  tools\wasmtime\wasmtime.exe WebAssembly runtime for the PC worker (§6.9, scripts/get-wasmtime.ps1)
  tools\scrcpy\               scrcpy + its adb: a phone's screen on the PC (§10, scripts/get-scrcpy.ps1)
  docs\SPEC.md                this file
  coordinator\                Python package `coordinator` (FastAPI app)
    __init__.py  __main__.py  app.py  config.py  db.py  models.py  auth.py
    scheduler.py  files.py  storage.py  terminal.py  notify.py  schedules.py
    plugins\__init__.py       plugin loader + registry
    plugins\<type>.py         one built-in plugin per file (see §7)
  worker\worker.py            Termux / PC worker, single file, stdlib only, Python >= 3.8
  worker\browser\index.html   browser worker page (+ worker.js, runner.js, style.css)
  dashboard\index.html        dashboard SPA (+ app.js, style.css). Vanilla JS. No build step.
  sdk\phonecluster\           Python SDK + CLI package (stdlib only)
    __init__.py  client.py  cli.py  __main__.py
  sdk\pyproject.toml          `pip install -e sdk` (optional; also importable via sys.path)
  examples\*.py               runnable examples using the SDK
  examples\wasm\              example WebAssembly modules + the builder that makes them (§6.9)
  tests\                      pytest suite
  scripts\*.ps1               setup.ps1, get-python.ps1, start.ps1, worker.ps1, get-ffmpeg.ps1, test.ps1, pc.ps1
  requirements.txt
```

## 2. Runtime overview

```
 Phones (Termux worker.py)  ──HTTP long-poll──┐
 Phones (browser /w page)   ──HTTP long-poll──┤
 PC worker (worker.py)      ──HTTP long-poll──┼──>  Coordinator (FastAPI, port 8765)  <──WebSocket──  Dashboard / SDK / CLI
                                              │        SQLite  + data\files + scheduler
                                              └──────────────────────────────────────────
```

- **Two doors** (§3): `8765` (HTTPS, every interface) for phones; `8764` (plain HTTP, 127.0.0.1 only) for the dashboard, CLI, SDK and the PC worker. The same app answers both; the dashboard and its API answer only to the PC itself.

### 2.1 Auto-discovery (UDP)

The coordinator answers probes on UDP `discovery_port` (default `8766`, `discovery: false` disables it). A client sends the datagram `PHONECLUSTER_DISCOVER v1` to the LAN broadcast address (and the default-route subnet broadcast); the coordinator replies to the sender with `{"phonecluster": 1, "name": <host>, "port": 8765, "url": "http://<ip of the interface the probe arrived on>:8765", "urls": [...]}`. No PIN is ever included.

- `worker.py` with no `--url` (or `--url auto`) probes until a coordinator answers; it still needs a join code (`--join`) or a saved `device.json`. Replies carry the HTTPS URL; a worker only talks to it if the certificate comes from its cluster CA, so a fake reply leads nowhere. A worker whose saved URL fails 3 consecutive connections re-probes and, if the coordinator answers from a new address, switches to it and rewrites `device.json`.
- `pc discover` lists answering coordinators; `pc login --discover` / `Cluster(url="auto")` use the first one.
- The browser worker cannot send UDP; it keeps using the URL it was opened with.
- **Workers never accept inbound connections.** They only make outbound HTTP calls (long-poll). This is what makes unrooted phones and hotspots work.
- **Dashboard** gets live updates over WebSocket `/ws/dashboard`.
- Coordinator can spawn the **local PC worker** as a subprocess (`local_worker: true` in config).

## 3. Authentication and keeping outsiders out (`coordinator/security.py`, `auth.py`)

- **Two doors.** `port` (8765, all interfaces) speaks **only HTTPS**, with certificates the cluster makes for itself on first start in `data/tls/` (`ca.pem`/`ca.key`: its own CA, EC P-256, 20 years; `server.pem`/`server.key`: the coordinator, signed by that CA, SANs = localhost, the host name, loopback and the LAN addresses at creation). Nothing is added to any system certificate store. `local_port` (8764) is plain HTTP bound to **127.0.0.1 only**: the dashboard, CLI, SDK and the PC's own worker. Both serve the same app.
- **Only this PC manages the cluster.** A client counts as *the PC itself* when its address is loopback or one of the PC's own interface addresses. Everything except the worker side answers only to it: from anywhere else only `/w`, `/w/*`, `/worker.py`, `/install.sh`, `/install.ps1`, `/python-win.zip`, `/ca.pem`, `/api/info`, `/api/w/*`, `/api/services*` and `/api/requests*` are reachable (403 otherwise; `/` shows a short "only opens on the PC" page; the dashboard WebSocket is closed).
- **Host names**: a request whose `Host` is not localhost, a loopback address, one of the PC's addresses, its host name (`.local`, `.lan`, `.home`, `.localdomain`) or a name in `security.extra_hosts` gets **421** (stops DNS rebinding through a web page).
- **Dashboard / SDK / CLI** (on the PC): header `X-Pin: <pin>` on `/api/*` calls, or `POST /api/login {pin}` which sets cookie `pc_session` (a random token valid 12 h, HttpOnly, SameSite=Strict, Secure over HTTPS; never the PIN). WebSocket `/ws/dashboard` takes `?pin=` or the cookie. `data/config.yaml` has `pin: "NNNNNN"` (6 digits, generated on first start).
- **Joining**: a new device needs a **one-time join code** (`POST /api/join/code`, the Join page): 8 characters from `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` shown as `XXXX-XXXX`, valid **once** for `security.join_minutes` (10); only its SHA-256 is stored (table `join_codes`). `POST /api/w/register {join_code, name, platform, caps, info, slots}` uses it up; the PIN is accepted only from the PC itself (the PC's own worker, which the coordinator starts with a fresh code in `PC_JOIN_CODE`). The answer adds `tls = {ca_pem, cert_pem, key_pem}`: the cluster CA and a certificate for this device (signed by the CA, used for phone-to-phone transfers, §8.3). Afterwards every worker call sends `X-Device-Token: <token>` (random 32-byte hex; DB stores `sha256(token)`); re-attaching with `device_id + token` works from anywhere.
- **Join command** (shown on the PC): `curl -fsSk --pinnedpubkey "sha256//<coordinator public key>" "https://<ip>:8765/install.sh?code=<code>" | sh` — curl refuses any other key, so the script cannot be swapped on the Wi-Fi. `/install.sh?code=` (only for a valid, unused code) writes the CA to `cluster-ca.pem`, downloads `worker.py` with the same pin and runs `python worker.py --url https://<ip>:8765 --ca cluster-ca.pem --join <code>`. The script picks `python3`, else `python` (3.8+), and installs Python itself only on Termux (`pkg install python`), so the same command adds a Linux or Mac computer. **Windows PC or laptop**: `powershell -NoProfile -Command "iex ((curl.exe -fsSk --pinnedpubkey '<pin>' 'https://<ip>:8765/install.ps1?code=<code>' | Out-String) + ' ')"` (curl.exe ships with Windows 10 and 11); `/install.ps1?code=` (valid code only) puts everything into `%USERPROFILE%\phonecluster`: `cluster-ca.pem`, `worker.py`, the worker's state in `state\`, a `start-worker.cmd` that starts it again, and Python — the PC's own portable Python when it has one (`GET /python-win.zip?code=`, valid code only: the top-level files of `tools/python` with a `._pth` of their own, i.e. interpreter and standard library without the PC's libraries; packed into `data/tmp` and checked on arrival against the SHA-256 in the script), on 64-bit Windows; else a Python 3.8+ the computer already has. It uses only PowerShell's built-in commands and .NET, so Windows' default script policy (Restricted) is not in the way; no admin rights, nothing installed or changed outside that folder. By hand: `--fp <CA SHA-256>` makes the worker fetch `/ca.pem` and keep it only if its fingerprint matches. Browsers: `https://<ip>:8765/w?code=<code>` (the QR); the browser warns once about the unknown certificate.
- **Workers** trust exactly the cluster CA (stored as `<state>/tls/ca.pem`; host names unchecked because the PC's address changes) and use plain HTTP only to a loopback address. A worker without the CA refuses to connect.
- **Lockouts** (in memory): wrong PINs (only when a PIN was sent), wrong join codes, unknown device tokens and wrong saved credentials count per client address; 5 within 10 minutes lock that address for 1 minute, doubling up to 1 hour (429 `too many wrong attempts; try again in N s`); 30 failures from outside the PC within a minute pause every outside join/sign-in for 5 minutes. The PC itself is never locked by others' guesses.
- Wrong/missing auth → `401 {"error":"unauthorized"}`. Removed device token → `410 {"error":"removed"}` (worker must forget its token and exit).
- Not covered: the llama.cpp RPC servers of a model split across devices (§6.4d) still have no password and no encryption while that model runs (the user's choice: kept as before).

## 4. Data model (SQLite, `data/cluster.db`, WAL mode)

Timestamps are Unix floats (UTC). JSON columns are TEXT holding JSON. IDs are 12-char random hex unless stated.

| table | columns |
|---|---|
| `devices` | `id PK, name, platform ('termux'|'windows'|'linux'|'browser'), token_hash, caps JSON(list), info JSON, status ('online'|'offline'), paused INT, slots INT, battery INT NULL, charging INT NULL, temp REAL NULL, storage_free_mb INT NULL, bench_score REAL NULL, bench JSON NULL, settings JSON ({roles, cpu_pct, mem_pct, gpu_pct, min_battery, max_temp, resume_temp} or {}), mem_free_mb INT NULL, gpu JSON NULL (live card figures), trust JSON NULL ({score, checks, agreed, wrong, strikes, benched}, §9.9), last_seen REAL, created REAL, removed INT DEFAULT 0` |
| `jobs` | `id PK, name, type, params JSON, status ('waiting'|'pending'|'running'|'done'|'failed'|'cancelled'), priority INT DEFAULT 0, depends_on JSON(list of job ids), pipeline_id NULL, template_id NULL, created REAL, started REAL NULL, finished REAL NULL, total_tasks INT, done_tasks INT, failed_tasks INT, result JSON NULL, error TEXT NULL, options JSON ({timeout, max_attempts, requires, targets, chunk_size, fail_fast, backups}), report JSON NULL (speed report, §9.5)` |
| `tasks` | `id PK, job_id, idx INT, payload JSON, status ('queued'|'assigned'|'running'|'done'|'failed'|'cancelled'), device_id NULL, attempts INT DEFAULT 0, max_attempts INT, assigned_at REAL NULL, started_at REAL NULL, finished_at REAL NULL, lease_expires REAL NULL, result JSON NULL, outputs JSON NULL, error TEXT NULL, duration REAL NULL, history JSON(list of {device_id, attempt, error, ts}), fingerprint TEXT NULL (§9.8), reused INT (answered from the cache), role NULL|'check'|'decide' + check_of (§9.9), impl TEXT (the impl kind that ran), result_hash TEXT NULL, verified INT NULL (1 confirmed, -1 disputed)` |
| `events` | `id INTEGER PK AUTOINCREMENT, ts REAL, kind, device_id NULL, job_id NULL, task_id NULL, message TEXT, data JSON` |
| `files` | `id PK (sha256 hex of content), name, size INT, mime, created REAL, refs INT DEFAULT 0` — bytes live at `data/files/<id>` |
| `blobs` | `id PK, name, size INT, chunk_size INT, n_chunks INT, replication INT, scheme ('copies'|'ec'), ec_k INT, ec_m INT, status ('storing'|'ready'|'degraded'|'lost'|'deleting'), created REAL` |
| `blob_parity` | `blob_id, stripe INT, j INT, chunk_id (sha256), size INT, PRIMARY KEY(blob_id, stripe, j)` — parity pieces of erasure-coded blobs (§10) |
| `blob_chunks` | `blob_id, idx INT, chunk_id (sha256), size INT, PRIMARY KEY(blob_id, idx)` |
| `chunk_locations` | `chunk_id, device_id, stored_at REAL, checked_at REAL NULL (last scrub), PRIMARY KEY(chunk_id, device_id)` |
| `cache` | `key PK (fingerprint), kind ('task'|'job'), job_type, result JSON, outputs JSON, devices JSON, size INT, source_job, seconds REAL, created REAL, used REAL, hits INT` (§9.8) |
| `schedules` | `id PK, name, cron TEXT, job_spec JSON, enabled INT, last_run REAL NULL, next_run REAL NULL, created REAL` |
| `templates` | `id PK, name, job_spec JSON, created REAL` |
| `settings` | `key PK, value JSON` (mirrors editable parts of config; config.yaml is the source at startup, settings table overrides persist edits) |

Event kinds: `device_joined, device_offline, device_online, device_removed, device_paused, job_submitted, job_started, job_done, job_failed, job_cancelled, task_failed, task_requeued, task_late_result, blob_stored, blob_degraded, blob_scrubbed, schedule_fired, notify_sent, package_submitted, job_cached, task_checked, wrong_answer, task_disputed, work_not_repeatable, job_rechecked, error`.

## 5. Job specification (what clients submit)

```jsonc
{
  "name": "resize photos",            // optional, defaults to "<type> <id>"
  "type": "python_map",               // plugin type, see §7
  "params": { ... },                  // plugin-specific
  "priority": 0,                      // higher runs first
  "depends_on": ["<job_id>", ...],    // pipeline edges; job stays 'waiting' until all deps are 'done'
  "options": {
    "timeout": 600,                   // seconds per task (default from config)
    "max_attempts": 3,                // per task
    "requires": ["ffmpeg"],           // extra capabilities every task needs
    "targets": "any" | "all" | ["<device_id>", ...],   // "all" = one task per online device (used by shell/sensor/benchmark)
    "chunk_size": 10,                 // hint for plugin split
    "fail_fast": false,               // cancel remaining tasks on first permanent task failure
    "cache": true | false | "refresh",  // reuse stored results of identical work (default: per type, §9.8)
    "verify": false | 0.25            // share of tasks a second device runs again (default: per type, §9.9)
  }
}
```

Param values may reference an upstream job's result inside pipelines: `{"$from_job": "<job_id or pipeline key>", "path": "a.b"}`.
The coordinator resolves these when the job leaves `waiting`.

### 5.1 Pipeline submission

`POST /api/pipelines` body: `{"name": "...", "jobs": [ {"key": "a", ...jobspec}, {"key": "b", "depends_on": ["a"], ...} ]}`.
`depends_on` entries may be keys inside the same pipeline or existing job ids. Response: `{"pipeline_id", "jobs": {"a": "<job_id>", ...}}`.

## 6. Task payload (what a worker executes)

```jsonc
{
  "impls": {                                  // worker.py picks the FIRST impl whose kind is in its caps in this order: python, shell, js, wasm;
                                              // the browser: wasm, js, python
    "python": {"code": "<source>", "entry": "run", "args": {...}},
    "js":     {"code": "<source>", "entry": "run", "args": {...}},
    "shell":  {"cmd": "ffmpeg -i ...", "env": {"K": "V"}, "cwd": null},
    "wasm":   {"module": 0, "call": "f" | null, "args": [...], ...}   // §6.9
  },
  "inputs": ["<file_id>", ...],               // worker downloads these before running; available to code via ctx
  "timeout": 600,
  "requires": ["ffmpeg"]                      // extra caps beyond the impl kind
}
```

Scheduler eligibility: device is eligible if `requires ⊆ caps` AND at least one key of `impls` is in `caps` AND target constraints match.

### 6.1 Python impl contract (worker-side harness)

The worker runs a **subprocess** (`sys.executable -c HARNESS`) and passes JSON on stdin:
`{"code", "entry", "args", "inputs": {"<file_id>": "<local path>"}, "workdir", "task_id"}`.
The harness `exec`s `code` in a fresh module namespace and calls `entry(args, ctx)`.

`ctx` API available to user code:
- `ctx.log(msg)` — line is streamed to the coordinator (live logs).
- `ctx.input(file_id) -> path` — local path of a downloaded input.
- `ctx.inputs -> list[path]` — all inputs in order.
- `ctx.output(path, name=None)` — register a file to upload when the task finishes; returns nothing.
- `ctx.workdir` — a scratch directory unique to this task (deleted after upload).
- `ctx.task_id`, `ctx.device_name`.
- `ctx.progress(fraction)` — optional 0..1; sent as a log line `__PC_PROGRESS__ 0.5`.

Return value of `entry` must be JSON-serializable. Right before calling `entry` the harness prints `\n__PC_WORK__\n` (the worker times the task's own work from there: `work_duration`, §9.5). The harness prints `\n__PC_RESULT__<json>\n` as the last stdout line.
On exception the harness prints `\n__PC_ERROR__<json {error, traceback}>\n` and exits 1.
Everything else on stdout/stderr is treated as log lines.

### 6.2 Shell impl contract

`cmd` runs via `subprocess.Popen(cmd, shell=True, env=os.environ | env, cwd=cwd or task workdir)`.
Environment additions: `PC_TASK_ID`, `PC_WORKDIR`, `PC_INPUT_<n>` (path of input n, 0-based), `PC_INPUTS` (os.pathsep-joined), `PC_OUTPUT_DIR` (= workdir/out; every file left there is uploaded as an output).
Result: `{"exit_code": int, "stdout": str (last 1 MB), "stderr": str (last 256 KB)}`; `ok = exit_code == 0`.
On Windows `shell=True` uses cmd.exe; `impls.shell.cmd_windows` if present overrides `cmd` on Windows workers.

### 6.3 JS impl contract (browser worker)

Runs inside a dedicated Web Worker: `code` is evaluated, then `entry(args, ctx)` is awaited. `ctx.log(msg)`, `ctx.progress(f)`, `ctx.inputs` (array of `ArrayBuffer`s downloaded from the coordinator), `ctx.output(name, blobOrArrayBuffer)`. Return value JSON-serializable.

### 6.4a Checkpoints (resumable tasks)

- `ctx.checkpoint(state)` saves JSON-serialisable progress (≤ 256 KB). The worker throttles posts to one per 2 s per task (latest wins) and always flushes the last one before reporting a failure. `ctx.resume(default=None)` / `ctx.checkpoint_state` return what the previous attempt saved, or the default on a fresh start.
- Worker → coordinator: `POST /api/w/task/{id}/checkpoint {state, attempt}` (409 if the task is not running on that device / stale attempt, 413 if too large). Stored in `tasks.checkpoint`; kept across requeue and job retry; cleared when the task completes.
- The next attempt's `task` command carries `"checkpoint": <state>`, so any device (Termux, PC or browser) can resume.
- Shell impls: `PC_CHECKPOINT` holds the previous state as JSON; writing JSON to `$PC_CHECKPOINT_FILE` saves a new one (the worker watches the file once a second and on exit).
- `python_map` checkpoints automatically after every item, so a requeued chunk resumes after the last finished item.

### 6.4b Streaming inputs and outputs

- An `inputs` entry may be `{"file_id", "offset", "length"}`: the worker fetches only that byte range (`Range: bytes=a-b`, HTTP 206 from `/api/w/files/{id}`, also `/api/files/{id}/download`) and exposes it like any input; `ctx.input()` accepts the key `"<file_id>:<offset>+<length>"`.
- `python_map` params `lines_file` (file id), `lines_per_task` (default 1000) and `line_format` (`auto`|`text`|`json`; `.jsonl`/`.ndjson` → json) split a big text file into byte-range tasks without the coordinator loading it: it scans the file once for line offsets. The mapper receives one line (or one parsed JSON value) per item.
- `options.stream_results: true` writes every finished task's items to `data/results/<job>/<idx>.jsonl` as they arrive (`plugin.stream_lines(result)` may customise the items; default: list elements, or the value); the task row stores only `{"streamed": n}`. On completion the parts are joined in idx order into one file registered in the file store and the job result becomes `{"results_file", "name", "count", "tasks"}` (no `reduce`).
- `GET /api/jobs/{id}/results` streams JSON lines: the parts so far while running, the final file once done; for non-streamed jobs the final result one element per line (204 until done). SDK: `job.iter_results()`, `job.download_results(path)`, `c.map_lines(fn, path_or_id, ...)`; CLI: `pc map --lines`, `pc results <job>`.

### 6.4c Services (long-lived model hosts)

Job type `service` (params `name, description, code, inputs, setup_cmd, accepts, produces, streaming_output, request_timeout, params`; `DEFAULT_OPTIONS = {requires: [service], timeout: 0, max_attempts: 5}`). Its task payload carries `"service": {name, request_timeout, setup_cmd, params, meta}` and never completes on its own: the worker's `ServiceRun` executes `setup_cmd` once, runs the **service harness** as a child process (stdin frames `REQ <json>` / `DAT <n>` + bytes / `END` / `QUIT`; stdout sentinels `__PC_READY__`, `__PC_PARTIAL__`, `__PC_DONE__`), calls `load(ctx)` and then `serve(request, ctx[, model])` per request, restarting the child after a crash or a request timeout. Workers report the `service` capability; browsers do not host.

Worker protocol additions: `POST /api/w/service/{task_id}/ready {name, meta}`; commands `service_request {task_id, request:{id, name, params, content_type, size}}` and `service_cancel {request_id}`; `GET /api/w/requests/{id}/input` (the caller's body, streamed as it arrives), `POST /api/w/requests/{id}/partial {data}`, `POST /api/w/requests/{id}/done {ok, result, error, traceback, outputs, ms}`. A polling device's `status.running` list refreshes the lease of those tasks, so a service task never expires while its host keeps reporting it.

Coordinator (`coordinator/services.py`): a registry of ready instances (one per service task; dropped when the task leaves `assigned`/`running`), a per-request bounded chunk queue (32 chunks, backpressure to the uploader), least-recently-used instance choice, queueing while all instances are busy. **More copies** (`coordinator/autoscale.py`, every 2 s, setting `services.autoscale`): a service whose request waited `services.scale_up_after` s (default 2), or with two queued, gets another copy, up to `services.max_copies` (3), one at a time — a model running alone on one device gets a replica deployment (§6.4d), a `service` job one more task pinned to the fastest free device that does not host it (`payload.meta.autoscaled`); a copy the autoscaler added that stayed unused for `services.scale_down_minutes` (3) while nobody waited is stopped (events `llm_copy`, `service_copy`). Placement failures cool down for 60 s. Services no longer take a worker job slot (§9.3). `GET /api/services` also lists models that load on the first call (`on_demand: true`, no instances); calling a name that cannot load (does not fit, just failed) answers 503. Caller API (PIN **or** device token): `GET /api/services`, `POST /api/services/{name}/call` (raw body / multipart `file`+`params` / JSON; `?params=`, `?request_id=`, `?queue_timeout=`, `?timeout=`; 200/404/502/503/504 with the request dict), `GET /api/requests[?limit]`, `GET /api/requests/{id}`, `GET /api/requests/{id}/partials?since=&wait=` (long-poll), `POST /api/requests/{id}/cancel`. WebSocket events `services` (registry changed) and `request` (`request` dict plus `partial` when one arrived). Events: `service_ready`. Details and the code contract: `docs/SERVICES.md`.

### 6.4d Distributed LLMs (one model across chosen devices)

`coordinator/llm.py` (`LLMManager`), `coordinator/gguf.py`, plugins `llm_shard` / `llm_head` (hidden from the New job form), `api_llm.py`. Guide: `docs/LLM.md`.

- **Runtime**: one pinned llama.cpp release in `tools/llama.cpp/` (`VERSION`, the Windows CPU zip extracted to `win-x64/`, the Android arm64 tarball, and with `get-llama.ps1 -Vulkan` the Windows Vulkan build in `win-vulkan-x64/`). `GET /api/w/runtime/llama` → `{tag, assets:{win-x64|android-arm64: {file,size}}, gpu_build}`; `GET /api/w/runtime/llama/{file}`. A service payload's `service.needs.llama = <tag>` makes the worker's `Provisioner` install it under `<state>/runtime/llama-<tag>/` before `load()`: prebuilt for Windows x86_64 and Android arm64 API ≥ 28, else compiled from source in Termux (`GGML_RPC=ON`). The PC worker uses `tools/llama.cpp/win-x64` directly when `PC_ROOT` points at it; `needs.llama_variant = "gpu"` selects `win-vulkan-x64` (PC worker only).
- **GPU** (read-only, no system change): the PC worker with the Vulkan build and `nvidia-smi` reports `info.gpus = [{name, vram_mb, vk_index, nvidia_index}]` (cards llama.cpp lists, matched by name to cards whose temperature `nvidia-smi` reads) and `info.llama_gpu = "win-vulkan-x64"`; every poll carries `status.gpu = {name, vram_total_mb, vram_free_mb, temp, util, ts}` (refreshed in the background every 10 s; stored in `devices.gpu`). GPU processes get two process-local environment variables: `GGML_VK_VISIBLE_DEVICES=<vk_index>` and `DISABLE_LAYER_AMD_SWITCHABLE_GRAPHICS_1=1` (AMD's hybrid-graphics Vulkan layer makes device enumeration loop forever).
- **Models**: `.gguf` files in `settings llm.models_dir` (default `data/models`). `service.needs.model = {name, size}` downloads `GET /api/w/models/{name}` (Range, resumable) into `<state>/models/`, or uses `PC_ROOT/data/models` directly. Dashboard/SDK: `GET/PUT /api/llm/models`, `POST /api/llm/models/fetch {url}`, `DELETE /api/llm/models/{name}`. `gguf.py` reads the metadata **and the tensor directory** (sizes from offset gaps): `layer_bytes`, `embd_bytes`, `output_bytes` (tied embeddings count again), `kv_bytes_per_token`, `compute_bytes_base = 512 × n_ff × 16`, `compute_bytes_per_ctx = n_embd × 4.5`.
- **Devices**: workers report `info.abi`, `info.android_api`, `info.coordinator_local` and `status.mem_free_mb`. `GET /api/llm/devices` adds `usable_mb` (`policy.usable_mem_mb`: min(free − max(256 MB, 10 % RAM), free × mem_pct)), `gpu`, `gpu_budget_mb`, `threads`, `llama_build`, `eligible` + `reason` (browser, no `service` cap, offline, missing build, switched off, AI role off, safety notes).
- **Parts**: a device offers its CPU (`usable_mb`) and, with a GPU budget, its graphics card (`min(vram_free, vram_total × gpu_pct) − 300 MB`). Per part: layers × (layer + KV per layer) + compute buffer; the part holding the output layer also holds `output_bytes`; the host's CPU also holds the token embeddings + 32 MB.
- **Plan** (`POST /api/llm/plan {model, mode?, devices?, host?, weights?, ctx}`), `mode` defaults to `manual` when `host`/`weights` are given, else `auto`:
  - **auto**: candidates = `devices` (empty: all) that are `eligible`. Every candidate alone (`placement: single`, GPU part filled first, `-ngl` = layers on the card) and every combination of ≥ 2 of the 10 fastest (`placement: split`, fastest parts filled first to budget − one layer of slack) is scored by a rough time per token (layers × MB ÷ part speed + 6 ms per Wi-Fi part, 0.5 ms per part on the host; CPU speed ≈ 1.25 × bench score MB/ms, GPU 100); the fastest plan that fits wins, fewer devices on a tie. Host: a PC in the set (the one keeping the model file), else the fastest device with storage for the file.
  - **manual**: exactly `devices`; weights default to usable memory (CPU + GPU budget); layers ∝ weight; inside a device the GPU part first; one device that is also the host → `single`.
  - Order: devices by their fastest used part (ties: input order), parts slowest first inside a device, so the fastest part is last and holds the output layer. `tensor_split` = layers per part in that order. Result: `{mode, placement, host, devices[{…, role, parts[{kind, layers, need_mb, budget_mb, fits}], layers, need_mb, fits, share, weight}], rpc[{device_id, kind, rpc, layers}], tensor_split, gpu_layers, gpu_full, ms_per_token, tokens_per_second, summary, excluded, ok, errors, warnings}`.
- **Deployment** (`POST /api/llm/deployments {name, model, mode?, devices?, host?, weights?, ctx, threads, force}`; table `llm_deployments`, `options.request` keeps the original request): **single** → only an `llm_head` job on the host (`-ngl <gpu_layers or 999>` with the GPU build, else `-ngl 0`). **split** → one `llm_shard` job per device (service `_llm.<dep>.<device>`, `ggml-rpc-server -H <lan ip> -p <random> -c [-d CPU,Vulkan0] -t <threads>`, announces `endpoint` via `ctx.announce()`), then one `llm_head` job on the host (CPU build; `--rpc <endpoints> -ngl 999 -ts <layers per part> --fit off -c <ctx> -np 1 --host 127.0.0.1`). Threads = the device's core allowance (§9.3) unless `threads` is lower. States `starting → loading → ready`; a missing shard → `degraded` (head cancelled) → new head when every shard is back; a shard job ending → `failed`; `stop` cancels everything → `stopped`; `restart` plans again from `options.request` when it still fits. **Guard** (every reconcile): a device that went offline, was switched off, had AI switched off or crossed a safety limit (`ai_keep` false) → an **auto** deployment is placed again without it (`llm_moved`), or `degraded` while nothing else fits (retried every 20 s); a **manual** one waits (degraded). A graphics card past 85 °C → the deployment is planned again without that card (both modes). WebSocket event `llm`; events `llm_starting, llm_ready, llm_degraded, llm_moved, llm_failed, llm_stopped, llm_model_added`.
- **Chat**: a normal service call on the deployment name (§6.4c) with text or JSON `{messages | prompt, system?, max_tokens, temperature, ...}`; partials are `{"token": "..."}` (batched every 250 ms); result `{text, finish_reason, tokens, tokens_per_second, prompt_tokens, served_by}`.
- **Loading on call**: a call to a name with no running instance first asks `LLMManager.ensure(name)`: an active deployment of that name → wait; an inactive one (`idle`, `stopped`, `failed` more than 60 s ago) → `restart`; a model file whose name without `.gguf` (lowercase, other characters → `-`) is `name` → `deploy(name, model, mode="auto", on_demand=True)`. The caller waits up to `llm.load_wait` s (900) and gets a partial `{"status": "loading <name> (<summary>)"}`. `chat` / `llm` reach `llm.default_model` (`GET/PUT /api/llm/default {model}`), or the only model file when there is one; a `.gguf` file name reaches its model. Events `llm_on_demand`.
- **Unloading when unused**: a ready deployment whose instances were not used for `options.idle_minutes` (deploy parameter; None → `llm.idle_minutes`, default 10; 0 = keep loaded; `POST /api/llm/deployments/{key}/keep {keep}`) is stopped with status **`idle`** (event `llm_idle`); its row stays, so the next call restarts it with the same request. A new deployment of a name replaces inactive rows of that name.
- **Replicas** (autoscaling, §6.4c): rows with `options.replica_of = <parent id>` and the parent's `name`, placed `single` on a device the parent and its other replicas do not use; hidden from listings and name lookups, shown as `copies` in the parent's description, dropped when the parent stops, restarts or goes idle, and when their device has trouble (no re-placement).
- **Service harness additions**: `ctx.announce(dict)` (sent with the ready signal as `meta`), `ctx.lan_ip()`, `ctx.runtime_dir`, `ctx.model_path`, `ctx.spawn(args, **popen_kwargs)` (starts a helper that dies with the service process however it ends: a kill-on-close job object on Windows, `PR_SET_PDEATHSIG` on Linux/Android); `unload()` also runs at exit. Workers re-announce ready services after re-registering, so a restarted coordinator relearns them.
- Services whose name starts with `_` are hidden from `GET /api/services`.

### 6.9 WebAssembly impl contract

`{"wasm": {"module": <input index of the .wasm>, "call": <export> | null, "args": [...], "params": [types], "results": [types], "env": {}, "memory_mb": 256, "files": [input idx], "names": {idx: name}, "stdin_input": idx | null, "stdin": text | null}}`. The same bytes run everywhere:

- **worker.py**: `wasmtime run -W max-memory-size=<memory_mb MiB> ...` — from `PC_ROOT/tools/wasmtime` (the PC, `scripts/get-wasmtime.ps1`, pinned release, checksum verified) or `PATH`; on Termux the worker installs it with `pkg install -y wasmtime` (fallback `apt-get update` + `apt-get install -y wasmtime`) on the first WebAssembly task, logging into the task log; if that fails it stops reporting the `wasm` capability (re-registers). Termux workers report `wasm` when wasmtime is present or installable.
- **call**: `wasmtime run --invoke <call> <module> <args...>`; each printed value becomes the result (one value → scalar, several → list; a trailing number after printed text is taken as the value). wasmtime's "experimental --invoke" warning is not logged.
- **program** (`call` null, the module needs `_start`): inputs listed in `files` are copied to a task folder mounted as `/in`, `/out` is mounted for outputs (`--dir host::/in --dir host::/out`), `env` via `--env`, `args` as the command line, stdin from the input `stdin_input` or the text `stdin`. stdout (≤ 16 MB) is the result, parsed as JSON when it is a JSON value (NaN/Infinity stay text), else the text; stderr lines are task log lines; every file in `/out` is an output; a non-zero exit fails the task. No network, no other files.
- **browser** (`runner.js` `runWasm`, in a fresh Web Worker per task): `WebAssembly.Module/Instance` with a WASI preview1 layer (args, environ, clocks, random, fd_read/write/seek/tell/close, fdstat, filestat, prestat for fd 3 = `/in` and fd 4 = `/out`, path_open/filestat/unlink in those flat folders; anything else answers ENOSYS; imports from other modules throw when called). Calls convert `i64` arguments to BigInt and results back (unsafe integers become strings); `f32` results are printed as the shortest decimal of that f32, exactly like wasmtime, so results compare equal across devices. Same JSON-or-text rule for program output. The `wasm` capability is reported when `WebAssembly` exists (`?wasm=0` opts out).

### 6.4 Task result (worker → coordinator)

```jsonc
{"ok": true,  "result": <json>, "outputs": [{"file_id","name","size"}], "duration": 12.3, "work_duration": 12.1, "log_lines": 123, "impl": "python"}
{"ok": false, "error": "message", "traceback": "…", "duration": 1.0}
```

## 7. Built-in plugins (`coordinator/plugins/<type>.py`)

Plugin module interface:

```python
TYPE = "hash_search"
DESCRIPTION = "Find inputs whose hash starts with a prefix"
PARAMS = [  # drives the dashboard form and CLI --param validation
  {"name": "prefix", "type": "str", "default": "0000", "help": "..."},
  {"name": "count",  "type": "int", "default": 1_000_000},
  # types: str, int, float, bool, json, file (file_id), files (list of file_id), code (multiline), select (with "choices")
]
DEFAULT_OPTIONS = {"timeout": 600, "requires": []}   # merged under jobspec.options
def split(params: dict, options: dict, ctx) -> list[dict]:   # returns task payloads (§6); ctx.devices() lists online devices, ctx.file_meta(id)
def reduce(params: dict, results: list, ctx) -> Any:          # results[i] = {"idx", "device_id", "ok", "result", "outputs", "error"} ordered by idx
# optional (SPEC 9.5/9.6): cut the work while it runs instead of splitting up front
def work(params, options, ctx) -> dict | None:                 # {"units", "per_unit"?, "grain"?, "state"?}
def piece(params, options, ctx, state, start, end) -> dict:    # the payload for units [start, end); meta.units = its size
def speed_key(params) -> str:                                  # names the kind of work for the speed memory
CACHE = True     # same inputs -> same result: results may be reused (§9.8)
VERIFY = True    # results are exact: a second device may check them (§9.9)
```

Plugins may ship worker code as Python source strings (`WORKER_PY = '''...'''`) and JS strings (`WORKER_JS`). Use `inspect.getsource` on a nested function when convenient.
The loader imports every `coordinator/plugins/*.py` at startup and also `data/plugins/*.py` (user plugins). `GET /api/plugins` lists them.

Built-in types (all must exist):

| type | params | impls | notes |
|---|---|---|---|
| `python_map` | `code` (defines `fn(item, ctx)`), `items` (list) or `items_file` (file_id of JSON list), `chunk_size` (default 10), optional `js_code` (defines `fn(item, ctx)` for browser), `inputs` | python (+ js if `js_code`) | result = list of outputs in item order |
| `shell` | `cmd`, `cmd_windows` (optional), `timeout` | shell | default `targets: "any"`; `"all"` fans out; result = `{device_id: {exit_code, stdout, stderr}}` |
| `media` | `op` (`transcode`|`resize`|`thumbnail`|`extract_audio`|`custom`), `inputs` (file ids), `width`, `height`, `format`, `quality`, `ffmpeg_args`, `segment_seconds` (video only) | shell (`ffmpeg`) | requires `ffmpeg`. One task per input file; if `segment_seconds` set, one task per `-ss/-t` range and reduce concatenates on coordinator with `tools/ffmpeg` if present |
| `hash_search` | `algo` (`sha256`|`md5`), `prefix`, `start`, `count`, `chunk_size`, `salt`, `max_results` | python + js | result = `{"matches": [{"n", "hash"}], "searched": N}` |
| `monte_carlo_pi` | `samples`, `chunks` | python + js | result = `{"pi", "inside", "samples"}` |
| `prime_range` | `start`, `end`, `chunk_size`, `return_list` | python + js | result = `{"count", "primes"?}` |
| `sensor` | `sensor` (`photo`|`audio`|`location`|`battery`|`sensors`|`wifi`), `duration`, `camera` (0/1), `targets` | shell (`termux-*`) | requires `termux_api`; outputs uploaded; result per device. Uses `termux-camera-photo`, `termux-microphone-record`, `termux-location`, `termux-battery-status`, `termux-sensor -a -n 1`, `termux-wifi-connectioninfo` |
| `benchmark` | none | python + js | `targets: "all"`; result per device; coordinator writes `bench_score` |
| `custom` | `impls`, `args_list` (one task per element merged into every impl's args), `inputs`, `requires` | any | raw passthrough for power users |
| `service` | `name`, `code` (defines `load`/`serve`), `inputs`, `setup_cmd`, `accepts`, `produces`, `streaming_output`, `request_timeout`, `params` | python (`service` cap) | long-lived model host, see §6.4c; one task per target (`targets: "all"` = every capable device) |
| `wasm` | `module` (file id of a `.wasm`), `call`, `range` `[start, end]` + `grain`, `args_list`, `inputs`, `stdin`, `env`, `combine` (`auto`|`list`|`sum`|`text`), `memory_mb` | wasm | §6.9. The coordinator reads the module's exports first (unknown function, wrong argument count, a `.wat` text file or no `_start` fail the job before anything runs). `range` + `call`: automatic pieces, `f(lo, hi)` per piece, results summed; `args_list`: one task per entry; program mode with `inputs`: one task per file (stdin + `/in/<name>`), result `[{input, result, outputs}]` |

`CACHE` is set on `python_map`, `prime_range`, `hash_search`, `monte_carlo_pi`, `media`, `wasm`; `VERIFY` on the same minus `media` (ffmpeg builds differ byte-wise). `custom` may opt in per job with `options.cache` / `options.verify`; `custom` impls may include `wasm`.

Benchmark definition (identical in worker.py and browser worker so scores are comparable enough):
run the loop `x = (x * 1103515245 + 12345) & 0x7fffffff` with `x = 42` for as many iterations as fit in **2.0 s**; also SHA-256 of a 1 MiB buffer repeatedly for 1.0 s.
`score = iterations / 1e6` for Python; JS divides its iteration count by 25 (JIT calibration). Report `{"score", "loop_iters", "hash_mb_s", "kind"}`.

## 8. Worker protocol (HTTP long-poll)

All worker endpoints are under `/api/w/`. Bodies are JSON unless noted.

| method & path | body / params | response |
|---|---|---|
| `POST /api/w/register` | `{join_code, name, platform, caps:[...], info:{cpu_count, mem_mb, os, python, model, shell}, slots, device_id?, token?}` — a one-time join code (§3; the PIN only from the PC itself); if `device_id`+`token` valid, re-attaches keeping the id | `{device_id, token, config:{poll_timeout:25, offline_after:30, log_batch_ms:500, cache_mb:500}, tls:{ca_pem, cert_pem, key_pem}}` |
| `POST /api/w/poll` | `{status:{battery, charging, temp, free_slots, running:[task_ids], storage_free_mb, mem_free_mb, gpu?}}` — blocks up to `poll_timeout` s | `{commands:[...]}` (may be empty) |
| `POST /api/w/task/{id}/start` | `{}` | `{ok}` |
| `POST /api/w/task/{id}/log` | `{lines:["..."], ts}` | `{ok}` |
| `POST /api/w/task/{id}/done` | task result (§6.4) | `{ok}` (409 if task no longer assigned to this device/attempt) |
| `GET  /api/w/files/{file_id}` | | raw bytes, `Content-Disposition` filename |
| `PUT  /api/w/files?name=<name>` | raw bytes | `{file_id, size, name}` |
| `GET  /api/w/chunk/{chunk_id}` | | raw bytes |
| `PUT  /api/w/chunk/{chunk_id}?req_id=<id>` | raw bytes | `{ok}` |
| `POST /api/w/chunk_ack` | `{chunk_id, ok, error?}` | `{ok}` |
| `POST /api/w/storage/scrub` | `{checked:[ids], bad:[ids], missing:[ids]}` | `{ok, checked, rebuilding}` (§10) |
| `POST /api/w/peer/fetch` | `{file_id}` | `{file_id, size, piece_bytes, pieces:[sha256 per piece], sources:[{offer_id, device_id, name, url, token}]}` (§8.3) |
| `POST /api/w/peer/ready` | `{offer_id, ok, host?, port?, error?}` | `{ok}` |
| `POST /api/w/peer/result` | `{offer_id, ok, bytes, seconds, error?}` | `{ok}` |
| `POST /api/w/bench` | `{score, loop_iters, hash_mb_s, kind}` | `{ok}` |
| `POST /api/w/term/{session_id}/output` | `{data:"..."}` | `{ok}` |
| `POST /api/w/term/{session_id}/closed` | `{exit_code}` | `{ok}` |
| `POST /api/w/leave` | `{}` | `{ok}` — device goes offline immediately |

Commands the coordinator sends inside `poll` responses:

```jsonc
{"cmd":"task", "task":{"id","job_id","idx","attempt","payload":{...}, "peer_inputs"?:[file ids]}}   // §8.3
{"cmd":"cancel", "task_id"}
{"cmd":"store_chunk", "chunk_id", "size"}            // worker GETs /api/w/chunk/{id}, saves to <state>/store/<id>, POSTs chunk_ack
{"cmd":"send_chunk", "chunk_id", "req_id"}           // worker PUTs the chunk back to /api/w/chunk/{id}?req_id=
{"cmd":"del_chunk", "chunk_id"}
{"cmd":"scrub_chunks", "chunk_ids":[...]}            // re-hash these stored chunks, delete damaged ones, POST /api/w/storage/scrub
{"cmd":"peer_serve", "offer_id", "file_id", "token", "ttl", "size"}   // open a sending port for one file (§8.3)
{"cmd":"peer_close", "offer_id"}
{"cmd":"term_open", "session_id", "shell": null}     // start a shell: Termux/Linux `bash` (fallback sh), Windows `cmd.exe`
{"cmd":"term_input", "session_id", "data"}          // raw text written to the shell's stdin
{"cmd":"term_close", "session_id"}
{"cmd":"notify", "title", "body"}                    // termux-notification if available; browser: Notification API; PC: toast
{"cmd":"benchmark"}
{"cmd":"config", "slots"?, "paused"?}
{"cmd":"hold", "request_id", "seconds"}              // pause normal tasks while a service request runs (§9.7)
{"cmd":"release", "request_id"}
{"cmd":"removed"}                                    // forget token and exit (browser: show "removed")
{"cmd":"shutdown"}
```

Rules:
- A device is `online` while it polls; the coordinator marks it `offline` when `now - last_seen > offline_after`. Offline → its `assigned`/`running` tasks are requeued (attempt counted).
- Workers must reconnect forever with exponential backoff (1 s → 30 s) and re-register with the saved `device_id` + `token`.
- The worker persists `{device_id, token, url, name}` in `<state>/device.json`. Default state dir: `~/.phonecluster` on Termux/Linux; `<project>\data\worker` when `--state-dir` is given (the PC scripts pass it). Never write outside these dirs (plus the task workdir under them).
- Each task runs in its own thread; Python impls run in a subprocess; cancel/timeout kills the whole process tree.
- Log lines are batched and posted every `log_batch_ms`. Cap 10 000 lines per task (worker drops with a final `... truncated` line).
- Battery/thermal on Termux without Termux:API: read `/sys/class/power_supply/battery/{capacity,status,temp}` (temp is tenths of °C); with Termux:API use `termux-battery-status` JSON. On Linux (a phone running Linux such as the J2, or a laptop): the device's own battery under `/sys/class/power_supply` (`battery`, else the first `type` Battery whose `scope` isn't Device), its `capacity`/`status`/`temp`; when an Android-style `battery` has no `temp`, the battery thermal zone, else a cpu/soc one, else any; laptops (`BAT0`…) report no temperature, since CPU sensors there sit above the phone limits; no battery → nothing (a desktop). On Windows: PowerShell `Get-CimInstance Win32_Battery` every 60 s (may be null on desktops). On browsers: Battery Status API if present.
- Capabilities a worker reports: always `python` + `shell` for worker.py, `js` for the browser; plus `ffmpeg` (if `ffmpeg` on PATH or `tools/ffmpeg/ffmpeg.exe` next to the project), `termux_api` (if `termux-battery-status` is executable), `storage` (if it has ≥ 200 MB free for chunks), `wasm` (worker.py: wasmtime found or installable via Termux `pkg`; browser: WebAssembly present), `peer` (worker.py: may send cached inputs to other devices, §8.3), `windows`/`linux`/`termux`/`browser` as a platform tag.

### 8.3 Phone-to-phone transfers

Big inputs travel between devices instead of all coming from the PC (`coordinator/peers.py`, `transfers.peer`, `transfers.min_mb` = 4):

- **Who has what**: when a normal task finishes on a device with the `peer` cap, its whole-file inputs of ≥ `min_mb` count as held by that device for 6 h (in memory). A task assigned to a `peer` device whose inputs include such a file held by another device that may send now (online, not switched off, `jobs` safety ok, not answering a request) carries `peer_inputs`.
- **Fetch**: the worker calls `POST /api/w/peer/fetch`. The coordinator picks up to 3 holders (fewest open offers, fastest), sends each `peer_serve` with a random 24-character token and waits up to 5 s for `peer/ready`. A holder starts an **HTTPS** server (its own device certificate from the cluster CA; the fetcher checks the chain) on its LAN address and a random port **only now**, serves only that file and only to requests carrying header `X-Peer-Token`, supports `Range`, and closes the port when its last offer is closed (`peer_close`, sent when the fetcher reports) or unused for `ttl` s. A holder without the file answers `error: "not here"` and is forgotten.
- **Download**: the fetcher pulls 4 MiB pieces from every source **and** the coordinator at once (one thread each, shared queue), checks each piece against the SHA-256 list the coordinator computed from its own copy, gives a failing piece back to the queue (that peer stops), and finally reports per source `peer/result` (`error: "hash mismatch"` is logged as an event). No sources, or any failure: plain download from the coordinator. Concurrent tasks needing the same input wait for one download (per-file lock in the worker).
- Browser workers neither send nor fetch from peers (a page cannot open a port, and cannot check a peer's certificate against the cluster CA).

## 9. Scheduler

Runs in the coordinator as an asyncio task ticking every 0.5 s and also on events (task done, device poll).

1. **Pipelines:** jobs in `waiting` become `pending` when all `depends_on` jobs are `done`; if any dep is `failed`/`cancelled` the job becomes `failed` with error `dependency failed`. Resolve `$from_job` references at this point.
2. **Split:** a `pending` job is split by its plugin into tasks (status `queued`), job → `running`, `total_tasks` set. Split errors → job `failed`.
3. **Assign:** queued tasks in order (highest job `priority`, then oldest job, then lowest `idx`), each to the first device — visited in descending `bench_score` (null last) — that may take it (§9.3) and is eligible (§6):
   a **normal task** needs a free job slot and must not be "too slow" (§9.5); a **service task** (payload has `service`) needs the `ai` role and `ai_start` and does not use job slots.
   Assignment sets `status='assigned', device_id, attempts += 1, lease_expires = now + timeout + 60` and pushes a `task` command to that device's queue.
4. **Fault tolerance:** a task is requeued (`queued`, `device_id = NULL`, history appended) when its device goes offline, when `lease_expires` passes, or when the worker reports `ok=false` and `attempts < max_attempts`. Otherwise the task is `failed`. A `done` result from a stale attempt on the *current* device (attempt mismatch) is rejected with 409.
   **Late results:** a device that previously held the task (it is in the task's `history`) may deliver an `ok=true` result after the task was requeued or reassigned; the coordinator accepts it, marks the task `done` with that device, sends `cancel` to any device currently redoing it, and records a `task_late_result` event. A late `ok=false` is acknowledged and ignored. Results for `done`/`failed` tasks get 409. Workers keep unsent results in an **outbox** (see §8) and replay them on reconnect.
5. **Completion:** when every task is `done`/`failed`/`cancelled`: if any `failed` → job `failed` (with `fail_fast` the remaining tasks are cancelled at the first permanent failure); else run `plugin.reduce` → job `done`, `result` stored. Then dependents are re-evaluated and notifications fire.
6. Cancel: `job → cancelled`, queued tasks → `cancelled`, running tasks get a `cancel` command.
7. `targets: "all"` jobs are split against the set of online devices at split time (plugin receives `ctx.devices()`, which lists devices with the `jobs` role; `ctx.devices(role="ai")` for services).

Per-device command queues are `asyncio.Queue`s; the poll handler waits on the queue up to `poll_timeout` seconds and returns whatever is available (draining the queue).

### 9.3 Device roles, limits and safety (`coordinator/policy.py`)

Every connected device has, in `devices.settings`: `roles {jobs, ai, storage}` (default all on), `cpu_pct` (10-100, default 100), `mem_pct` (10-70, default 70), `gpu_pct` (0-90, default 80); `paused` is the master switch (used for nothing). `PATCH /api/devices/{id} {paused?, settings:{roles?, cpu_pct?, mem_pct?, gpu_pct?, min_battery?, max_temp?, resume_temp?}}` validates and clamps (400 on unknown roles or non-numbers; roles merge). Device dicts from the API and WebSocket carry `control = {roles, limits, job_slots, threads, usable_mem_mb, gpu, gpu_budget_mb, safety:{temp_state, gpu_state, jobs, ai_start, ai_keep, storage, gpu_start, notes}}`.

- **Core allowance** = round(cores × cpu_pct), at most cores − 1, at least 1. Job slots = min(worker slots, allowance); next to a running service a phone runs 1 normal task and a PC (windows/linux) half its slots. Model threads = allowance. The worker offers cores − 1 slots by default (`--slots` or the dashboard fix a number).
- **Balanced profile** (the user's choice; cluster `scheduler.*` and per-device settings can only make it stricter): battery temperature ≥ 40 °C → `warm` (no new work) until ≤ 36 °C; ≥ 43 °C → `hot` (every running task and service is requeued without spending an attempt, event `device_rested`). Normal tasks need `charging` or battery ≥ 20 %. Services/models start only when charging or battery ≥ 50 % and are moved off an unplugged device under 30 %. Usable memory for models ≤ 70 % of free. GPU: no reading → not used; ≥ 80 °C → no new models on it until ≤ 72 °C; ≥ 85 °C → models leave it (§6.4d guard).
- Switching `ai` off (or the master switch) moves services off the device at once; normal tasks already running finish. `storage` off: no new chunks are placed there (existing chunks stay readable).

### 9.5 Speed

- **Skip slow devices:** before giving a normal task to a device, the scheduler estimates its time per task (the job's measured average on that device; else scaled by benchmark score from the best-measured device; before any task finished, `1 / bench_score`). If the faster eligible devices together (slots ÷ their time) would finish the job's remaining queued tasks — plus half a task when they are all busy — within 90 % of this device's time for one task, the task waits for them.
- **Backups:** when a device has a free slot and no queued task it could take, it may run a copy of a slower device's running task (not for jobs with `options.backups = false`, the default for `shell` and `sensor`, nor for pinned tasks; one copy per task; the task must be ≥ 5 s old and the copy expected to finish 1.3× sooner, using the job's measured times). The worker gets a normal `task` command with `"backup": true`. The first `ok` result wins (`history` gets `backup_won_by`), the other copy is cancelled; a failed copy is ignored; if the original device drops, the copy takes over without a requeue.
- **Report:** a finished job gets `jobs.report = {wall_s, task_time_s, devices[{device_id, name, tasks, units, busy_s, avg_s, rate}], fastest_device{device_id, name, alone_s}, speedup, handover_s, single_device, backups{started, won}}` — `alone_s` is all the job's units at that device's measured rate, spread over its job slots, plus the median per-task hand-over time.
- **Speed memory** (`coordinator/speeds.py`, table `speeds(key, device_id, rate, samples, seconds, updated)`): every finished normal task records `units / seconds` for `(speed key, device)`. The speed key is the plugin's `speed_key(params)` (`python_map:<sha1 of code+fn+js>[:12]`, `hash_search:<algo>`, else the job type), stored as `options._speed_key`; units are `payload.meta.units` (1 when absent); seconds are the worker's `work_duration` (time inside the task's own code, without interpreter start-up or pauses; `tasks.work_seconds`), else `duration`. The rate is a time-weighted average over the last ~20 s of work. All speed estimates (`SpeedBook`) use, in order: the job's own finished tasks (Σunits/Σseconds), the speed memory, a rate scaled by benchmark score from a device of the same benchmark kind (Python and JavaScript scores are not compared); with nothing known, benchmark scores rank devices. `GET /api/speeds` lists the memory; a removed device's rows are deleted.

### 9.6 Automatic task sizing

A plugin may define `work(params, options, ctx) -> {units, per_unit?, grain?, state?} | None` and `piece(params, options, ctx, state, start, end) -> payload`. When `work()` returns units (built-ins: `python_map` items or blocks of lines, `prime_range`, `hash_search`, `monte_carlo_pi`, unless `chunk_size` / `lines_per_task` / `chunks` is set, whose defaults are now 0 = automatic; never for `targets: "all"`), the job starts with **no tasks**: `options._dynamic = {units, cursor, pieces, done, per_unit, grain}` and the plugin's `state` is kept in memory (rebuilt by calling `work()` again after a restart). On every assign pass, after higher-priority work, each device with a free slot gets the next piece `[cursor, cursor + size)` as a new task (`idx` = piece number, so results stay in order):

- known speed `r` (units/s, `per_unit` converts the learned per-item rate): `size = max(left·r/R / 2, min(r·2 s, left·r/R), r·0.5 s)`, at most `r·60 s` (`R` = Σ slots × rate over eligible devices; weighted factoring: every round hands out half of what is left, by speed, so all devices finish together); a device is skipped when the faster ones would finish everything left before its piece;
- unknown speed: a probe piece of `left / (10 × eligible slots)`;
- sizes are rounded up to `grain`; a remainder smaller than `grain` goes with the piece.

A job is complete when the cursor reached `units` and no piece is pending. `options._dynamic.done` counts finished units; the API shows it as `progress_units {units, done, cut, pieces}` (internal options stay hidden).

### 9.7 Requests first

When a service request is dispatched (§6.4c), the coordinator **holds** every device working for it (the instance's device, plus the shards of a split model): `{"cmd": "hold", "request_id", "seconds"}`; when the request ends, `{"cmd": "release", "request_id"}`. A held device gets no new normal tasks (and starts none: the worker's slot wait includes holds); its running normal tasks are **paused** — Windows: `NtSuspendProcess` on the task's process tree (Toolhelp snapshot), POSIX: `SIGSTOP` / `SIGCONT` to the process group — and resumed on release or after `scheduler.hold_seconds` (default 120) if a release is lost. Paused time counts neither towards the task timeout nor in `duration` / `work_duration`. Normal task processes always run below normal priority (Windows `BELOW_NORMAL_PRIORITY_CLASS`, POSIX nice 10 on the process group). `scheduler.requests_first: false` turns holds off.

### 9.8 Never compute the same thing twice (`coordinator/cache.py`)

- **Fingerprints**: task = SHA-256 of canonical JSON `{v, impls, inputs, requires}` (file ids are content hashes); job = `{v, type, plugin version (SHA-1 of the plugin's source), params (normalized, after $from_job), options minus internal and operational keys (timeout, max_attempts, requires, targets, fail_fast, cache, verify, backups, chunk_size, priority)}`.
- **Mode** per job: `off` (setting `cache.enabled` false, `options.cache` false, `targets: "all"`, or a type without `CACHE` and no `options.cache: true`), `refresh` (`options.cache: "refresh"`: compute, then replace), else `on`. Jobs fired by a schedule default to `cache: false`.
- **Job level**: at split time an `on` job whose fingerprint is stored completes at once with that result (`report = {cached, from_job, saved_s, wall_s:0}`, `options._cached`, API field `cached`, event `job_cached`, notification `job_done`). On completion every job not `off` stores its result with the devices that worked on it and its duration.
- **Task level** (fixed splits; automatically sized pieces differ between runs): each task gets `tasks.fingerprint`; an `on` job inserts a stored task as `done` with `reused = 1` (no device); every successful task of a job not `off` stores its raw result and outputs. A pipeline submitted again therefore re-runs only the steps whose inputs changed.
- An entry whose result or outputs point to a file no longer in the file store is dropped instead of reused. Results over `cache.max_result_kb` (4096) are not stored; the table is trimmed least-recently-used to `cache.max_mb` (256). `GET /api/cache` (stats + entries), `DELETE /api/cache`, `POST /api/jobs/{id}/rerun {fresh}` (same spec as a new job; `fresh` → `cache: "refresh"`).

### 9.9 Catching wrong answers (`coordinator/verify.py`)

- **Which tasks**: tasks of jobs whose plugin sets `VERIFY` (or `options.verify` truthy), not `options.verify: false`, not `targets: "all"`, not pinned, not reused, and not of a kind marked unrepeatable. After such a task finishes well, it is checked with probability `verify.share` (0.05; a number in `options.verify` overrides; a device with trust score < 0.75 → at least `verify.suspect_share` 0.5).
- **Check**: a task row with `role = 'check'`, `check_of = <original>`, the same `idx`, a payload restricted to the impl kind that ran (`impl` from the done body, else inferred from the device's order) and `avoid = [original device]`, `max_attempts 2`; queued only if some other device could take it. `tasks.result_hash` = SHA-256 of canonical `{result, outputs as (name, file_id)}`. The job finishes only after its checks; checks do not count in `total_tasks` / `done_tasks`, the reduce input or the speed report, and do not update piece progress.
- **Same hash** → original `verified = 1`; both devices `agreed += 1`, `score += 0.05` (max 1). **Different** → a `decide` task avoiding both; no third device → original `verified = -1`, events/notification `task_disputed`, nobody penalised. **Decider** agrees with one side → the other device is **wrong**: `wrong += 1`, `score −= 0.3`, a strike; the original takes the agreeing result (its cache entry is replaced, a streamed part rewritten); the device's other tasks finished in the last `verify.recheck_minutes` (60) of jobs that may be checked are queued again with `avoid` (finished jobs return to `running`, `options._rechecked`, event `job_rechecked`) and its cache entries from that window are dropped; event + notification `wrong_answer`. **Three different results** → the speed key is stored in setting `verify_unrepeatable` (no more checks of that kind), original `verified = -1`, event `work_not_repeatable`.
- `verify.strikes` (3) wrong answers within 24 h → `trust.benched`: the device gets no normal tasks except ones pinned to it (services and storage continue) until `POST /api/devices/{id}/trust` (score back to 0.5). A check queued for `WAIT_SECONDS` (120) without a device is cancelled (a decider then counts as disputed). Device dicts carry `trust = {score, checks, agreed, wrong, benched, state ('trusted'|'checking more'|'no jobs'), note, last_wrong}`; `GET /api/trust`. A job report carries `checks = {checked, confirmed, decided, disputed}` and `reused_tasks`.

## 10. Files, pooled storage, terminal, schedules, notifications

**Files (`data/files`)**: content-addressed by SHA-256. Upload via multipart `POST /api/files` (dashboard/SDK) or raw `PUT /api/files?name=` (workers/CLI). `GET /api/files/{id}/download` streams with the original name. Deleting a file that a job references is allowed (`refs` is informational).

**Pooled storage (blobs on phones)**: `POST /api/storage` (multipart file or `{file_id}`; optional `replication`, `scheme` 'auto'|'copies'|'ec') splits the content into `chunk_mb` MiB chunks, chooses `replication` online devices with `storage` cap ordered by `storage_free_mb` (PC worker excluded unless `include_pc: true`), sends `store_chunk`, waits for acks (60 s each; failed acks pick another device). Blob `status`: `ready` when every chunk has ≥ `replication` locations, `degraded` when ≥ 1, `lost` when a chunk has 0 online locations. `GET /api/storage/{id}` streams the blob: for each chunk use `data/storage/cache/<chunk_id>` if present, else send `send_chunk` to an online location and await the `PUT` (30 s timeout, try the next location). Cache is LRU-capped at `storage.cache_mb`. A background task every 60 s re-replicates `degraded` blobs from any available copy and deletes chunks of `deleting` blobs.

**Erasure coding** (`coordinator/erasure.py`: Reed–Solomon over GF(256), systematic Cauchy matrix scaled so parity 0 is XOR; bulk multiply via `bytes.translate`, add via big-int XOR): with `scheme` `auto` (setting `storage.scheme`, default) and no explicit `replication`, a blob put while ≥ 3 storage candidates are online is stored `ec` with `k`/`m` from `erasure.shape(n)`: `m = 1` below 6 devices, else 2; `k = min(n − m, 6)` (3 → 2+1, 4 → 3+1, 5 → 4+1, 6 → 4+2, ≥ 8 → 6+2); `scheme: "ec"` with fewer than 3 → 400. Every `k` consecutive chunks form a stripe (a short last stripe pads with zero-length pieces); its `m` parity pieces are content-addressed chunks in `blob_parity`. Each of the stripe's pieces goes to a different device (candidates rotated per stripe); a piece with no free device goes next to another piece of the stripe on the least-loaded device (status `degraded`) and is moved when a device becomes free. Reading uses data chunks directly; a chunk that cannot be fetched is rebuilt from any `k` pieces of its stripe. Status: `ready` (every piece placed, on distinct devices, every stripe readable), `degraded` (a piece missing or two pieces sharing a device), `lost` (a stripe with fewer than `k` pieces online). Repair rebuilds missing pieces from `k` others and places them. Deleting removes parity too. `GET /api/storage` and `/status` add `protection {scheme, overhead, survives, label}`; status adds `parity` (pieces with locations).

**Scrubbing**: every repair round each healthy storage device (jobs safety ok, not held) gets `scrub_chunks` with up to 16 of its chunks whose `COALESCE(checked_at, stored_at)` is older than `storage.scrub_hours` (24; 0 = off). The worker re-hashes each file (the chunk id is its SHA-256), deletes damaged ones and reports; checked ones get `checked_at`, bad/missing ones lose their location (event `blob_scrubbed`) and a repair run rebuilds them. Chunks read from the coordinator cache are hash-checked too.

**Terminal**: `POST /api/terminal {device_id}` → `{session_id}`. Coordinator sends `term_open`; the dashboard sends `{"op":"term_input","session_id","data"}` over the dashboard WebSocket and receives `{"ev":"term","session_id","data","pos"}`; the worker posts output to `/api/w/term/{sid}/output`. The worker runs the shell with pipes (line-oriented, not a pty) — interactive full-screen programs are out of scope. `DELETE /api/terminal/{sid}` closes it. Sessions auto-close after 30 min idle. The browser worker does not support terminals.
A session outlives the dashboard page: leaving the Terminal page or reloading the tab does not close it. The coordinator keeps the scrollback (the last 400 output chunks plus each command typed, as `$ <line>`); `GET /api/terminal/{sid}` → `{session_id, device_id, created, last, closed, exit_code, output, pos}` lets the page pick the shell up again. `pos` counts every character the session ever held, and live `term` events carry the same counter, so the page skips output it already replayed. The page remembers its session and the command history (last 200) in the tab's `sessionStorage`, re-reads the scrollback after the WebSocket reconnects, and lists open shells it isn't showing (another tab's, a closed tab's) with Resume / Close. When the idle timer or a departing device ends a session, pages get `{"ev":"term_closed","session_id","exit_code":null}`.

**Screen** (`coordinator/screen.py`): `POST /api/devices/{id}/screen` opens an Android phone's screen (platform `termux` or `linux`) in a scrcpy window on the PC, for watching and controlling it. scrcpy and its adb come from `tools/scrcpy`; the cluster's adb runs its own server on port 5039 (env `ANDROID_ADB_SERVER_PORT`, so another adb on the PC is left alone), logs to `data/tmp`, and is stopped with the coordinator (and by `stop.ps1`). The phone is found as: the adb serial at the address it polls from (adb over Wi-Fi, `host:5555`), else the one USB phone whose adb `model` matches the device's model, else `adb connect host:5555`; otherwise 400 with how to turn on USB debugging. One window per device (a second request answers `{already_open:true}`), several devices at once: each window gets its own tunnel port on the PC (`--force-adb-forward --port N`, the first free one from 27183; over Wi-Fi older phones refuse adb's reverse tunnel, and scrcpy's fallback otherwise gives every window port 27183, so opening a second phone closed the first); scrcpy's output goes to `data/logs/screen-<id>.log`, and if it quits within 2.5 s its last error is returned. Nothing changes on the phone; it needs USB debugging (PHONE-SETUP.md).

**Schedules**: `cron` is standard 5-field (`min hour dom mon dow`, supports `*`, lists `1,2`, ranges `1-5`, steps `*/10`) evaluated in local time by `coordinator/schedules.py` (own implementation, no croniter). A loop wakes every 20 s; when `next_run <= now` it submits `job_spec` (a jobspec, §5) with `name` suffixed by the timestamp, records `last_run`, computes the next `next_run`. `POST /api/schedules/{id}/run` fires immediately.

**Notifications** (`settings.notifications`): `{"desktop": true, "phone": false, "webhook_url": null, "on": ["job_done","job_failed","device_offline","device_joined"]}`.
Desktop: on Windows show a toast via PowerShell (`Windows.UI.Notifications` script, no third-party module); fallback prints. Phone: send `notify` to every online device with `termux_api`. Webhook: `POST webhook_url` JSON `{kind, message, data, ts}` with 5 s timeout. All notifications also appear as events and are pushed to the dashboard.

## 11. REST API (dashboard / SDK / CLI)

All under `/api`, JSON, auth per §3. Errors: `{"error": "<message>"}` with 4xx/5xx. List endpoints accept `?limit=&offset=`.

| method & path | purpose |
|---|---|
| `GET /info` | `{name:"phonecluster", version, urls:[...lan urls], port, has_pin:true, time}` (public) |
| `POST /login` `{pin}` | this PC only; sets the `pc_session` cookie, returns `{ok}` |
| `GET /join` · `POST /join/code` | `{url, urls, local_url, ca_fingerprint, server_pin, worker_url}`; with a code also `{code, expires, minutes, browser_url, install_cmd, windows_cmd, manual_cmd, qr_svg, install_qr_svg}` (§3; `install_qr_svg` encodes the Termux command, error correction L) |
| `GET /stats` | `{devices:{online, total, cores, slots_free}, jobs:{running, pending, done_24h, failed_24h}, tasks_per_min, leaderboard:[{device_id,name,bench_score,tasks_done}]}` |
| `GET /devices` · `GET /devices/{id}` · `PATCH /devices/{id}` `{name?, paused?, slots?, settings?:{roles?, cpu_pct?, mem_pct?, gpu_pct?, min_battery?, max_temp?, resume_temp?}}` · `DELETE /devices/{id}` (sends `removed`, requeues its tasks, sets removed=1) · `POST /devices/{id}/benchmark` · `POST /devices/{id}/notify` `{title, body}` · `POST /devices/{id}/screen` (§10) | device management; device dicts carry `control` (§9.3) |
| `GET /jobs?status=` · `POST /jobs` (jobspec) · `GET /jobs/{id}` · `GET /jobs/{id}/tasks` · `GET /jobs/{id}/result` · `POST /jobs/{id}/cancel` · `POST /jobs/{id}/retry` (requeues failed tasks, job → running) · `DELETE /jobs/{id}` · `GET /jobs/{id}/export?format=json|csv|zip` | jobs. Export: `json` = job+tasks+result; `csv` = one row per task (idx, device, status, duration, result as JSON string); `zip` = result.json + tasks.csv + every output file + logs |
| `POST /pipelines` | §5.1 |
| `POST /packages` (multipart `file` = zip, `?dry_run=true`) | job package: `job.yaml`/`job.json` (a §5 jobspec or a §5.1 `jobs:` list) plus code/data files; `@path` params and `$text/$file/$files/$json` forms are inlined/uploaded per `docs/PACKAGES.md`. Returns `{kind:"job", job_id, job, uploaded}` or `{kind:"pipeline", pipeline_id, jobs, uploaded}`; dry run returns the resolved spec |
| `GET /tasks/{id}` · `GET /tasks/{id}/log?tail=` · `POST /tasks/{id}/cancel` | tasks |
| `GET /files` · `POST /files` (multipart `file`) · `PUT /files?name=` (raw) · `GET /files/{id}` (meta) · `GET /files/{id}/download` · `DELETE /files/{id}` | file store |
| `GET /storage` · `POST /storage` (`scheme`?) · `GET /storage/{id}` (stream) · `GET /storage/{id}/status` · `DELETE /storage/{id}` | pooled storage (§10) |
| `POST /jobs/{id}/rerun` `{fresh}` · `GET /cache?limit=` · `DELETE /cache` | result reuse (§9.8) |
| `GET /trust` · `POST /devices/{id}/trust` `{reset}` | wrong-answer checks (§9.9) |
| `POST /terminal` `{device_id}` · `GET /terminal` · `GET /terminal/{sid}` (scrollback) · `DELETE /terminal/{sid}` | remote shell (§10) |
| `GET /schedules` · `POST /schedules` `{name, cron, job_spec, enabled}` · `PATCH /schedules/{id}` · `DELETE /schedules/{id}` · `POST /schedules/{id}/run` | schedules |
| `GET /templates` · `POST /templates` `{name, job_spec}` · `DELETE /templates/{id}` | templates |
| `GET /plugins` | `[{type, description, params, default_options}]` |
| `GET /events?limit=&kind=` | recent events |
| `GET /settings` · `PATCH /settings` | `{scheduler:{min_battery,max_temp,resume_temp,default_timeout,default_max_attempts}, storage:{...}, notifications:{...}, pin?}`; changing `pin` invalidates dashboard cookies but not device tokens |

WebSocket `/ws/dashboard`: server → client messages `{"ev": "<name>", ...}`: `snapshot` (devices, running jobs, stats on connect), `device`, `devices`, `job`, `task`, `log` (`task_id, lines`), `event`, `term` (`session_id, data`), `term_closed`, `stats` (every 5 s).
Client → server: `{"op":"sub_logs","task_id"}`, `{"op":"unsub_logs","task_id"}`, `{"op":"term_input","session_id","data"}`.

Static routes: `GET /` → `dashboard/index.html`; `/static/*` → `dashboard/`; `GET /w` → `worker/browser/index.html`; `/w/*` → `worker/browser/`; `GET /worker.py` → `worker/worker.py`; `GET /install.sh?code=` → shell script (below, only for a valid code); `GET /install.ps1?code=` → the Windows installer and `GET /python-win.zip?code=` → the PC's portable Python for it (§3, only for a valid code); `GET /ca.pem`; `GET /join` (this PC only: a fresh one-time code with its QR and commands; `Accept: application/json` returns the JSON form); `GET /qr.svg?text=` (this PC only).

`install.sh` (served with the PIN and URL templated in):

```sh
#!/bin/sh
# PhoneCluster worker installer for Termux (no root), Linux and macOS
PY=$(command -v python3 || command -v python)        # 3.8+; Termux only: pkg install -y python
mkdir -p ~/phonecluster && cd ~/phonecluster
curl -fsS "{URL}/worker.py" -o worker.py
exec "$PY" worker.py --url "{URL}" --ca cluster-ca.pem --join "{CODE}"   # see §3
```

The script (§3) first writes the cluster CA and fetches `worker.py` with `curl -fsSk --pinnedpubkey`. The Join page shows the one-liner and its own QR (scan, copy, paste into Termux; the same command on a Linux or Mac computer), the Windows one-liner (§3), the browser URL `{URL}/w?code={CODE}` (the second QR), a by-hand command with the CA fingerprint, and the fingerprint itself. `GET /api/join` returns the addresses and fingerprints without a code; `POST /api/join/code` makes one.

## 12. Configuration (`data/config.yaml`, created with defaults on first run)

```yaml
port: 8765                 # HTTPS only, for phones (§3)
host: 0.0.0.0
local_port: 8764           # plain HTTP on 127.0.0.1: dashboard, CLI, SDK, the PC worker
pin: "482913"              # generated
local_worker: true         # spawn worker.py on this PC
local_worker_slots: 0      # 0 = every core but one (the device's CPU limit still applies)
offline_after: 30
poll_timeout: 25
scheduler: {min_battery: 20, max_temp: 40.0, resume_temp: 36.0, default_timeout: 600, default_max_attempts: 3,
            requests_first: true, hold_seconds: 120}
           # Balanced profile values; settings here and per device can only make them stricter (§9.3)
llm:       {idle_minutes: 10, load_wait: 900, default_model: null}       # §6.4d
services:  {autoscale: true, max_copies: 3, scale_up_after: 2, scale_down_minutes: 3}   # §6.4c
storage:   {chunk_mb: 4, replication: 2, cache_mb: 512, include_pc: false, scheme: auto, scrub_hours: 24}   # §10
cache:     {enabled: true, max_mb: 256, max_result_kb: 4096}                                  # §9.8
verify:    {share: 0.05, suspect_share: 0.5, recheck_minutes: 60, strikes: 3}                 # §9.9
transfers: {peer: true, min_mb: 4}                                                            # §8.3
security:  {join_minutes: 10, extra_hosts: []}                                                # §3
notifications: {desktop: true, phone: false, webhook_url: null, on: [job_done, job_failed, device_offline, device_joined]}
log_level: info
```

`PATCH /api/settings` writes back to `config.yaml` (keeps comments out; plain dump is fine) and applies live.

## 13. SDK (`sdk/phonecluster`) and CLI — stdlib only

```python
from phonecluster import Cluster
c = Cluster(pin="482913")   # default http://127.0.0.1:8764 (the PC-only door); or env PC_URL / PC_PIN, or data/cli.json
#   an https:// URL is checked against data/tls/ca.pem (env PC_CA); c.join() makes a one-time join code

job = c.map(fn, items, chunk_size=10, name=None, js_fn=None, inputs=None, timeout=600, priority=0)
#   fn must be a top-level function `fn(item, ctx)`; source shipped with inspect.getsource. Returns Job.
job.wait(poll=1.0, timeout=None) -> Job     job.result() -> Any     job.status     job.tasks()    job.logs(task_idx=None)
job.cancel()   job.export(path, format="zip")
c.shell(cmd, targets="any"|"all"|[ids], timeout=600) -> Job
c.submit(type, params, **options) -> Job          # any plugin
c.pipeline([{"key":..., "type":..., "params":..., "depends_on":[...]}, ...]) -> Pipeline (.jobs, .wait(), .results())
c.run_package(path, dry_run=False, wait=False) -> Job | Pipeline | dict   # folder or .zip with job.yaml (docs/PACKAGES.md)
c.devices() ; c.device(id) ; c.remove_device(id) ; c.pause_device(id, paused=True)
c.jobs(status=None) ; c.job(id)
c.files.upload(path) -> file_id ; c.files.download(file_id, dest) ; c.files.list() ; c.files.delete(id)
c.storage.put(path, replication=None) -> blob_id ; c.storage.get(blob_id, dest) ; c.storage.list() ; c.storage.delete(id)
c.schedules.list()/create(name, cron, job_spec)/delete(id)/run(id)
c.templates.list()/create(name, job_spec)/delete(id)
c.stats() ; c.events(limit=50) ; c.plugins() ; c.settings() ; c.update_settings({...})
c.notify(device_id, title, body)
c.wasm(module, call=None, range=None, args_list=None, inputs=None, stdin=None, env=None, combine="auto", grain=None, memory_mb=None, cache=None, verify=None) -> Job   # §6.9
c.submit(..., cache=None|False|"refresh", verify=None|False|share) ; c.rerun(job_id, fresh=False) ; job.rerun(fresh) ; job.cached
c.cache_info(limit) ; c.clear_cache() ; c.trust() ; c.trust_again(device)
c.storage.put(path, replication=None, name=None, scheme=None)
```

CLI: `python -m phonecluster <command>` (also `scripts/pc.ps1`). Global flags `--url`, `--pin` (fallback env, then `data/cli.json`, written by `pc login`).
Commands: `login`, `join` (prints URL, PIN, install one-liner and an ASCII QR), `devices`, `device <id>`, `remove <id>`, `pause|resume <id>`, `jobs [--status]`, `job <id>`, `submit <type> [--param k=v ...] [--file k=path] [--wait]`, `run <folder|package.zip> [--dry-run] [--wait]`, `map <file.py:fn> <items.json|-> [--chunk N] [--wait]`, `shell "<cmd>" [--all|--device id] [--wait]`, `cancel <job>`, `retry <job>`, `logs <job|task> [-f]`, `result <job>`, `export <job> [--format zip|json|csv] [-o path]`, `files list|upload <path>|download <id> <dest>|rm <id>`, `storage list|put <path>|get <id> <dest>|rm <id>`, `schedules list|add <name> "<cron>" <jobspec.json>|rm <id>|run <id>`, `templates list|save <name> <jobspec.json>|rm <id>`, `terminal <device>` (interactive line terminal via WebSocket-less polling of `/api/tasks`? → NO: the CLI terminal uses the same WebSocket `/ws/dashboard`; implement a minimal RFC 6455 client in `client.py` for this one feature, stdlib only), `stats`, `events`, `plugins`, `bench [device]`, `wasm <module.wasm|id> [--call f] [--range A B] [--grain N] [--args ...] [--input f ...] [--stdin text|-] [--combine] [--memory MB] [--fresh|--no-cache] [--wait]`, `rerun <job> [--fresh] [--wait]`, `cache [show|clear]`, `trust [--again device]`, `storage put <path> [--scheme auto|copies|ec] [--replication N]`, `submit ... [--fresh|--no-cache]`.

`--param k=v` values are parsed as JSON when possible, else string.

## 14. Dashboard (`dashboard/`) — vanilla HTML/CSS/JS, mobile-friendly, dark theme

Views (hash-routed `#/devices` etc.): **Overview** (stats tiles, leaderboard, recent events), **Devices** (cards: name, platform, battery/charging/temp, slots in use, score, status; actions: pause, benchmark, notify, remove, edit thresholds), **Jobs** (table with status filters; **New job** form generated from `/api/plugins` PARAMS with a "targets" selector, priority, timeout; "Save as template" / "Load template"; **Pipeline builder**: add several job forms with keys and depends_on), **Job detail** (progress bar, tasks table with device/attempts/duration, click a task → live log pane, result JSON viewer, outputs list with download links, buttons cancel/retry/export json/csv/zip), **Files** (drag-drop upload, list, download, delete), **Storage** (upload to pool, replication selector, status per blob with chunk map, download, delete), **Terminal** (device selector + line terminal with history), **Schedules** (CRUD with a cron helper), **Events**, **Settings** (thresholds, notifications, PIN), **Join** (QR image, PIN, copy buttons for the Termux one-liner and browser URL).
Additions: job detail shows "Reused the result of an identical earlier job", task marks `reused` / `check` / `decider` / `checked` / `disputed`, the checks line of the report, "Computed again after …" for rechecked jobs, and **Run again** / **Run again fresh**; the New job form has "Reuse results of identical earlier work"; device cards show a trust chip and, when benched, **Trust again**; Storage has a protection selector (automatic parity pieces or N copies), a Protection column and parity pieces in the piece map; Settings → Automatic has result reuse, the double-check share, recheck window, strikes and phone-to-phone transfers (+ "Forget stored results"); Settings → Storage has protection and the scrub interval.
Login screen asks for the PIN and stores it via `POST /api/login`. Reconnecting WebSocket with status indicator. No external assets.

## 15. Browser worker (`worker/browser/`)

Page `/w?code=…&name=…`: registers with the one-time join code (form field "Join code"; the code is removed from the address bar after use) (platform `browser`, caps `["js","browser"]`, info from `navigator`), stores `device_id`/`token` in `localStorage`, long-polls with `fetch`, runs `js` impls in a Web Worker (`runner.js`) with per-task timeout via `terminate()`, reports Battery API values, requests a screen wake lock (`navigator.wakeLock`) with a hidden looping video fallback, shows status/battery/current task/log tail, has **Pause**, **Leave** (calls `/api/w/leave` and clears storage) and **Rename**. Must run on Chrome ≥ 80 (ES2017 syntax only, no modules, no optional chaining; BigInt only through `BigInt()` calls, never literals). Reports `wasm` when WebAssembly exists and runs `wasm` impls first (§6.9); done bodies carry `impl`.

### 15.1 Python in the browser (Pyodide)

When `tools/pyodide/` exists (`scripts/get-pyodide.ps1`, ~13 MB, served at `/w/pyodide/`, advertised as `pyodide` in `GET /api/info`), the browser worker also registers the `python` capability and runs `python` impls in a persistent Web Worker (`worker/browser/pyrunner.js`) on CPython compiled to WebAssembly. The harness mirrors §6.1 exactly (`ctx.log/input/inputs/output/workdir/progress/checkpoint/resume`; `print()` becomes log lines; inputs and outputs live in Pyodide's in-memory filesystem), so `python_map` jobs run on a browser phone without a JS twin. Limits: standard library only, no subprocess, slower than native, needs WebAssembly (Chrome 89+) and ~150 MB free RAM; the interpreter loads once per page (pre-warmed after registration) and is dropped on a task timeout or cancel. If loading fails the page re-registers without `python`. `?python=0` on the worker URL opts out.

## 16. Scripts

- `scripts/setup.ps1` — runs `get-python.ps1`, creates `data/` dirs.
- `scripts/get-python.ps1 [-Version 3.12.10]` — downloads Python's embeddable package (`python-<v>-embed-amd64.zip` from python.org), checks the published MD5 and the Python Software Foundation's Authenticode signature on `python.exe`, unpacks it into `tools\python\` and writes `python312._pth` = the stdlib zip, `.`, `Lib\site-packages`, `..\..` (the project), `..\..\sdk`, without `import site` (isolated mode: PYTHON* variables, the script's folder and the user's site-packages are ignored). pip is copied in once (by `.venv` or a system Python) and installs `requirements.txt` into `tools\python\Lib\site-packages`, ignoring pip settings files outside the project (`PIP_CONFIG_FILE=NUL`). No installer runs: nothing in the registry, PATH, Start menu or py launcher. Every script below uses `tools\python\python.exe`, falling back to `.venv`.
- `scripts/start.ps1 [-NoWorker] [-Port N]` — runs `python -m coordinator` with the cluster's Python (HTTPS on `port`, plain HTTP on 127.0.0.1:`local_port`). The coordinator, the PC worker and every task harness it starts run as `tools\python\python.exe` itself (`-u -X utf8`), so a Windows firewall rule can name that one program; temporary files go to `data\tmp`.
- `scripts/worker.ps1 [-Url http://127.0.0.1:8764] [-Join CODE] [-Pin]` — runs the PC worker only (`--state-dir data\worker`).
- `scripts/get-ffmpeg.ps1` — downloads `https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip` to `tools\`, extracts `ffmpeg.exe`/`ffprobe.exe` into `tools\ffmpeg\`, deletes the zip.
- `scripts/get-pyodide.ps1 [-Version 0.26.4]` — downloads the Pyodide npm tarball into `tools\pyodide\` (browser Python, §15.1).
- `scripts/get-wasmtime.ps1 [-Tag v49.0.4]` — downloads the pinned wasmtime Windows zip from GitHub, checks its published SHA-256, extracts `wasmtime.exe` (+ license) into `tools\wasmtime\` (§6.9). Nothing outside the folder.
- `scripts/get-scrcpy.ps1 [-Tag v5.0.1]` — downloads the pinned scrcpy Windows zip from GitHub, checks the SHA-256 from the release's `SHA256SUMS.txt`, unpacks it (scrcpy.exe, adb.exe and their DLLs) into `tools\scrcpy\` (§10 Screen). Nothing outside the folder.
- `scripts/test.ps1` — runs pytest with the cluster's Python.
- `scripts/pc.ps1` — CLI wrapper: `& tools\python\python.exe -m phonecluster @args` (`sdk` is on its path).

## 17. Tests (`tests/`, pytest + pytest-asyncio + httpx)

- `test_db.py`, `test_scheduler.py` (assignment order, health gating, requeue on offline/lease, attempts), `test_cron.py`, `test_plugins.py` (split/reduce of every built-in), `test_api.py` (auth, jobs, files, templates, schedules, export, settings), `test_storage.py` (chunking, placement, degraded/repair with a fake device), `test_worker_e2e.py` (start uvicorn on a free port in a thread with a temp `data` dir, launch `worker/worker.py` as a subprocess with a temp state dir, run `python_map`, `shell`, `hash_search`, `benchmark`, cancel, storage put/get, terminal echo; must pass on Windows), `test_sdk.py` (against the same in-process server).
- Tests must not touch `<project>\data`; they use `tmp_path` and env `PC_DATA_DIR`.

## 18. Coding conventions

- Python 3.12 for coordinator/SDK/tests; **worker.py must stay Python 3.8-compatible and stdlib-only** (no f-string `=` specifier, no `match`, no `|` types, no walrus in comprehensions that 3.8 rejects).
- Type hints everywhere in the coordinator; `pydantic` models for request bodies in `models.py`.
- Never `print` in the coordinator; use `logging` (`coordinator.*` loggers).
- All paths derived from `PC_ROOT` (default: parent of the `coordinator` package) and `PC_DATA_DIR` (default `PC_ROOT/data`).
- No network access to the internet at runtime except the optional notification webhook.
- Keep the worker resilient: any exception inside a task is a task failure, never a worker crash; any exception in the poll loop → backoff and retry.
- **Outbox:** when uploading an output or posting `done` fails with a transport error (coordinator unreachable), the worker stores the result plus the not-yet-uploaded output files under `<state>/outbox/<task_id>/` (browser: `localStorage` key `pc_outbox`, outputs kept only while they fit in ~1 MB base64). After every successful poll the worker replays the outbox in the background: upload pending outputs, then POST `done`; an entry is deleted on 2xx, 409 or any other HTTP error, and kept on transport errors. Entries older than 7 days are dropped. Failed results (`ok=false`) are never stored — the coordinator requeues on its own.
