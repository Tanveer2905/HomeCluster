# Deviations from SPEC.md

Each entry explains a place where an implementation differs from, or had to
resolve an ambiguity in, `docs/SPEC.md`.

## Dashboard

### Deviations

1. **Extra JS files under `dashboard/`.**
   SPEC §1 / §14 name `index.html`, `app.js`, `style.css`. The code is split into
   `util.js`, `api.js`, `ws.js`, `jobform.js`, `views.js`, `views2.js`, `app.js`
   (loaded as plain `<script>` tags in that order — still no build step, no
   modules, no frameworks, no external assets). One 100 KB `app.js` was not
   maintainable. **The coordinator must serve every file in `dashboard/` under
   `/static/<name>`**, not just the three named ones.

2. **Downloads go through `fetch` + a blob URL instead of a plain `<a href>`.**
   SPEC §3 allows either the `pc_pin` cookie or the `X-Pin` header. A plain link
   would rely on the cookie only, so the dashboard would break wherever cookies
   are blocked (the SPEC explicitly wants header auth to work). Every download
   (`/api/files/{id}/download`, `/api/storage/{id}`, `/api/jobs/{id}/export`)
   is fetched with `X-Pin`, then handed to a temporary `<a download>`. The
   filename comes from `Content-Disposition` when present. Note this buffers the
   whole response in memory — fine for LAN-sized files, but a multi-GB pooled
   blob will be memory-heavy in the browser.

3. **Uploads use `XMLHttpRequest`, not `fetch`.**
   Only XHR reports upload progress. The multipart body and the `X-Pin` header
   are exactly as SPEC §11 describes (`POST /api/files` and `POST /api/storage`,
   field name `file`, plus a `replication` field for storage).

4. **Settings view exposes a few notification kinds beyond SPEC §10's default
   list.** The `on` checkboxes offer `job_done, job_failed, device_offline,
   device_joined` (the config defaults) plus `device_online, device_removed,
   blob_degraded, error`. All are valid event kinds from SPEC §4; the coordinator
   should accept any subset.

5. **`PATCH /api/settings` is sent one section at a time.** Each card ("Save
   scheduler", "Save storage", "Save notifications", "Change PIN") sends only its
   own sub-object, and omits fields left blank, so a partial patch must merge
   rather than replace. SPEC §11 does not say whether PATCH merges or replaces —
   the dashboard assumes **merge**.

6. **The polling fallback also polls `/api/devices` and `/api/stats`.** The
   requirement was `/api/jobs` every 5 s while the socket is down; devices and
   stats are polled on the same 5 s tick so the Overview and Devices views are
   not frozen. All polling stops entirely while the WebSocket is open.

### Ambiguities resolved defensively (coordinator should confirm)

7. **List-endpoint response shape is not pinned.** SPEC §11 says list endpoints
   accept `?limit=&offset=` but never states whether the body is a bare array or
   an envelope. The dashboard accepts **either**: a bare array, or an object with
   an `items` / `results` / `data` / `jobs` / `devices` / `tasks` / `files` /
   `blobs` / `events` / `schedules` / `templates` / `plugins` key. A bare array is
   preferred and is what the mock returned.

8. **`GET /api/tasks/{id}/log?tail=` response shape is not pinned.** The
   dashboard accepts plain text, a JSON array of lines, `{"lines": [...]}`,
   `{"log": "..."}` or `{"text": "..."}`. `{"lines": [...]}` is recommended for
   symmetry with the WebSocket `log` event.

9. **`GET /api/storage/{id}/status` shape is not pinned.** The chunk map expects
   `{id, status, replication, n_chunks, chunks: [{idx, chunk_id, size,
   locations: ["<device_id>", ...]}]}`. `locations` may also be a list of objects
   with a `device_id`/`id` field, or a plain count (the map then shows the count
   without device names). A chunk with 0 locations renders red, fewer than
   `replication` renders amber, otherwise green.

10. **Device "slots in use" is not a DB column.** SPEC §4 stores `slots` only.
    The device card shows `used / total` when the device JSON carries `running`
    (a list or a number) or `free_slots`; otherwise it shows `- / slots`.
    **Requested: include `free_slots` (or `running`) in the device JSON.**

11. **`POST /api/terminal` response key.** SPEC §10 says `{session_id}`; the
    dashboard also accepts `{id}`.

12. **Upload response key.** `POST /api/files` is expected to return the file
    row (SPEC §4 `files`); the dashboard reads `id` and also accepts `file_id`
    (the shape `PUT /api/files` returns per SPEC §8).

13. **`GET /api/join` `qr_svg` is injected as inline HTML.** SPEC §11 defines it
    as SVG markup produced by our own coordinator, so it is trusted. Every other
    server-supplied string in the UI is inserted as a text node, never as HTML.

14. **Job `depends_on` in the pipeline builder** is submitted as sibling *keys*
    (SPEC §5.1 allows keys or existing job ids). The builder only offers keys of
    other steps in the same pipeline.

15. **Unknown WebSocket `ev` values are ignored silently** rather than logged, so
    the coordinator can add events without breaking older dashboard tabs.

## SDK/CLI (`sdk/phonecluster`, `examples/`, `tests/test_sdk_unit.py`)

### Things the coordinator must accept (extra wire detail SPEC left open)

1. **`python_map` gets an extra param `fn_name`.** SPEC 7 says `code` "defines
   `fn(item, ctx)`" but does not say how the plugin finds the function when the
   user's function is called something else. `Cluster.map()` ships
   `{"code": <dedented inspect.getsource(fn)>, "fn_name": "<fn.__name__>",
   "items": [...], "chunk_size": N}` (+ optional `js_code`, `inputs`,
   `items_file`). The plugin should use `params["fn_name"]` when present and
   fall back to a single top-level function / a function literally named `fn`.
2. **`$from_job` must be resolved recursively**, not only at the top level of
   `params`. `examples/pipeline_demo.py` sends
   `"items": [{"$from_job": "primes", "path": "count"}]` because `python_map`
   needs `items` to stay a list. Walk nested dicts and lists when resolving.
3. **Header case.** `urllib` normalises header names, so the SDK's PIN header
   goes out as `X-pin`, not `X-Pin`. This is legal HTTP (RFC 7230 header names
   are case-insensitive) — do not match the header case-sensitively.
4. **`GET /api/jobs/{id}/result`** may return either the bare result or an
   envelope `{"result": ...}`; the SDK unwraps the envelope only when the
   response has no other meaningful keys. Returning the bare value is fine.
5. **List endpoints** may return a bare JSON array or an envelope
   (`{"devices": [...]}`, `{"jobs": [...], "total": n}`); the SDK accepts both.
6. **`PUT /api/files?name=`** must answer `{"file_id", "size", "name"}` (SPEC 8);
   the SDK also accepts `id` instead of `file_id`. `POST /api/storage` must
   answer `{"blob_id"}` or `{"id"}`. `POST /api/terminal` must answer
   `{"session_id"}` (`id` also accepted).
7. **`POST /api/storage` multipart** is sent with the file part named `file`,
   plus text parts `name` and (when given) `replication`.

### SDK surface

8. `Cluster.map()` gained `max_attempts=3` and `wait=False`; `Cluster.submit()`
   takes the SPEC 5 options as keyword arguments instead of a bare `**options`
   bag, so `c.submit(type, params, timeout=…, targets=…, requires=…)` folds
   them into `jobspec.options` correctly.
9. **`Job.wait(timeout=…)` raises the builtin `TimeoutError`** when it expires
   (SPEC only defines `JobFailed` for failed/cancelled). `JobFailed` and
   `ClusterError` are siblings under a new base class `PhoneClusterError`.
10. `Job.logs()` returns **text**: one task's log for `task_idx=N`, or every
    task concatenated behind `[task N] <task_id>` headers. `/api/tasks/{id}/log`
    may return `{"lines": [...]}`, a bare list, or plain text — all are handled.
11. Additive helpers not named in SPEC 13, all thin wrappers over documented
    endpoints: `Job.refresh/retry/delete/outputs/download_outputs/progress/done`,
    `Pipeline.statuses`, `Cluster.info/login/join/task/task_log/cancel_task/
    submit_spec/update_device/rename_device/dashboard_socket/open_terminal/
    close_terminal`, `files.upload_bytes/meta`, `storage.put_file_id/status`,
    `schedules.update`.
12. **`Cluster.bench(device_id=None)`**: with a device id it POSTs
    `/api/devices/{id}/benchmark` and returns the response dict; with no
    argument it submits a `benchmark` job with `targets: "all"` and returns a
    `Job`. SPEC's `bench [device]` did not say what "all devices" means.
13. **Config fallback**: after `<root>/data/cli.json` the SDK also tries
    `~/.phonecluster/cli.json`, so the CLI is usable from a phone (Termux),
    where the project folder does not exist.
14. Items larger than **2 MB of JSON** are uploaded to the file store and passed
    as `items_file` instead of inline `items` (SPEC 7 allows both; this picks
    between them automatically).

### CLI

15. Extra global flag `--http-timeout` (per-request socket timeout).
16. `storage status <id>` added next to `list|put|get|rm` (it maps onto the
    documented `GET /api/storage/{id}/status`).
17. `logs` gained `--task N` to show a single task index; `bench` gained
    `--wait`; `submit` accepts `--targets/--device/--all/--requires/
    --max-attempts/--chunk/--name/--priority/--timeout`.
18. Exit codes: **1** for any failure (as specified), **2** for argparse usage
    errors (argparse's own convention), **130** on Ctrl+C.
19. `pc join` renders the ASCII QR with the optional `qrcode` package, which is
    present in the project's `tools\python` (it is already a coordinator dependency).
    Without it the command prints `open <url>/join for the QR` instead. The SDK
    itself never imports it, so the package stays dependency-free.
20. `pc login` saves `data/cli.json` even when the PIN cannot be verified
    (the coordinator may not be running yet); it prints `(unverified)`.
21. `--param k=v` repeated with the same key builds a list (needed for list
    params); `--file k=path` repeated likewise builds a list of file ids.

### Tests

22. `tests/test_sdk_unit.py` is self-contained (no `conftest.py`, which another
    component owns) and never touches the project's `data`: an autouse fixture
    points `PC_ROOT` at `tmp_path` and clears `PC_URL`/`PC_PIN`. SPEC 17's
    `test_sdk.py` (against the real in-process coordinator) is still expected
    from the coordinator side; this file is the offline unit-level companion.

## Workers (`worker/worker.py`, `worker/browser/`, worker tests)

### Things the coordinator must know (wire detail SPEC left open)

1. **`bench_done` in the register response.** The worker runs the §7 benchmark
   automatically once after its first successful registration *unless* the
   register response contains `"bench_done": true`. SPEC §8's register response
   table does not list this field. **Please send `bench_done: true` whenever the
   device row already has a `bench_score`**, otherwise every worker restart
   burns 3 s of CPU re-benchmarking. Any other value (or a missing field) means
   "benchmark now". It is only ever run once per worker process.

2. **`pin` may be `null` when re-attaching.** SPEC §8 lists `pin` as a register
   field, but a worker restarted from `device.json` has no PIN (it is never
   persisted). It then sends `{"pin": null, "device_id": ..., "token": ...}`.
   Accept a null/absent `pin` when `device_id` + `token` are valid; reply `401`
   if they are not — the worker then forgets them and re-registers with `--pin`
   if it has one, or exits with code 2 telling the user to pass `--pin`.

3. **Register is also the rename/update channel.** The browser worker's
   *Rename* button re-registers with the same `device_id` + `token` and a new
   `name`. A register that re-attaches should update `name`, `caps`, `info` and
   `slots` on the existing row.

4. **Cancelled tasks still POST `/api/w/task/{id}/done`** with
   `{"ok": false, "error": "cancelled", ...}`. Timed-out tasks post
   `{"ok": false, "error": "timeout after <N>s"}`. A `409` response is expected
   and ignored by the worker.

5. **Shell task failures carry the full §6.2 result too.** On a non-zero exit
   the worker posts `{"ok": false, "error": "command exited with code N",
   "traceback": "<last 4 KB of stderr>", "result": {"exit_code", "stdout",
   "stderr"}, "outputs": [...], "duration", "log_lines"}` — a superset of
   §6.4's failure shape, so the `shell` plugin's reduce can still report
   `{device_id: {exit_code, stdout, stderr}}` for failed devices. Files left in
   `PC_OUTPUT_DIR` are uploaded even when the command fails.

6. **`__PC_PROGRESS__ <f>` lines are forwarded verbatim** into the task log
   stream (SPEC §6.1 calls it "a log line"). The coordinator/dashboard can parse
   them out of `/api/w/task/{id}/log`; the worker also tracks the value locally.
   They count towards the 10 000-line cap.

7. **Browser workers report `slots: 1`** and never more; `temp` and
   `storage_free_mb` are always `null`, and `info.python` / `info.shell` are
   `null`. Their `info.model` is the (truncated) user-agent string. Pausing in
   the page is local: it reports `free_slots: 0` (there is no worker→coordinator
   pause endpoint in SPEC §8).

8. **The browser worker POSTs `/api/w/leave` on `pagehide`** using
   `fetch(..., {keepalive: true})` so closing the tab takes the device offline
   immediately. It may arrive after the page is already gone.

9. **Every worker call except `/api/w/register` sends `X-Device-Token`**,
   including `GET /api/w/files/{id}`, `GET /api/w/chunk/{id}` and the two `PUT`s.
   Uploads send `Content-Type: application/octet-stream` and an explicit
   `Content-Length`.

10. **Long-poll socket timeout is `poll_timeout + 15 s`.** If the coordinator
    holds a poll open longer than that, the worker treats it as a network error
    and reconnects (1 s → 30 s backoff).

### Deviations

11. **Python harness stdin JSON has two extra keys.** SPEC §6.1 specifies
    `{"code", "entry", "args", "inputs", "workdir", "task_id"}`; the worker also
    passes `"device_name"` (required by `ctx.device_name`, which §6.1 mandates
    but the stdin object had no way to supply) and `"input_order"` (the input
    file ids in payload order, so `ctx.inputs` is ordered independently of dict
    iteration). This is internal to `worker.py` — nothing else sees it.

12. **`ctx.output()` registrations travel in a file, not a sentinel.** SPEC §6.1
    defines exactly three stdout sentinels (`__PC_RESULT__`, `__PC_ERROR__`,
    `__PC_PROGRESS__`), so rather than invent a fourth, the harness writes
    `<workdir>/__pc_outputs.json` (`[{"path", "name"}, ...]`) before printing
    the result; the parent reads it, uploads each file and then deletes the
    workdir. Consequence: a task's own file named `__pc_outputs.json` in its
    workdir would be overwritten.

13. **`device.json` also stores `slots`.** SPEC §8 says `{device_id, token, url,
    name}`; the worker adds `slots` so a `config` command's slot count survives
    a restart. Unknown keys are ignored on load.

14. **Extra shell environment variables.** Beyond SPEC §6.2's `PC_TASK_ID`,
    `PC_WORKDIR`, `PC_INPUT_<n>`, `PC_INPUTS`, `PC_OUTPUT_DIR`, the worker also
    sets `PC_DEVICE_NAME`, and — when ffmpeg was found — `PC_FFMPEG` plus its
    directory prepended to `PATH`. That last one is what makes the `media`
    plugin work on the PC with the bundled `tools/ffmpeg/ffmpeg.exe` without
    every command needing an absolute path.

15. **`worker.py` is ~1975 lines, not the ~1200 the brief suggested.** It is one
    file, stdlib-only and 3.8-clean as required; the extra length is the
    "never crash" defensive handling (every filesystem / subprocess / socket
    call guarded), the embedded ~110-line harness, and the fact that §8 asks for
    tasks, chunks, terminal, notifications, benchmark, battery and capability
    detection in the same file. Splitting it was not an option: the coordinator
    serves it as a single `GET /worker.py`.

16. **The poll loop runs in a background thread.** `Worker.run()` starts it and
    the main thread waits in `join(0.25)` loops, because a blocking socket read
    on Windows is not interruptible by Ctrl+C. `SIGINT` therefore keeps its
    default handler (KeyboardInterrupt → `POST /api/w/leave`, kill tasks, exit);
    only `SIGTERM` gets a custom handler.

17. **The worker clears `<state>/work` at startup**, removing task workdirs
    orphaned by a crash. `<state>/cache` and `<state>/store` are never cleared.

18. **The browser worker ships its own SHA-256.** `crypto.subtle` is only
    available in secure contexts and the cluster runs on `http://<lan-ip>:8765`,
    so the §7 hash phase uses a hand-written SHA-256 in `runner.js` (verified
    against `hashlib` in the test suite). Its `hash_mb_s` is therefore ~20x
    lower than a Termux worker's on the same phone — compare `score` (the loop,
    divided by 25 as specified), not `hash_mb_s`, across kinds.

19. **The wake-lock fallback video is generated at runtime.** SPEC §15 asks for
    a "hidden looping tiny inline video"; with no external assets allowed and no
    reliable way to inline a valid video as a data URI, `worker.js` records a
    ~0.4 s clip from a 2x2 canvas with `MediaRecorder` and loops that blob. If
    `navigator.wakeLock` exists it is used first; the video is the fallback.

20. **`worker/browser/` has exactly the four files SPEC §1 names**
    (`index.html`, `worker.js`, `runner.js`, `style.css`). `index.html`
    references them as `/w/style.css`, `/w/worker.js` and (from `worker.js`)
    `/w/runner.js` — the coordinator must serve `/w/*` from `worker/browser/`
    with `Content-Type: application/javascript` for the `.js` files, or the
    `new Worker('/w/runner.js')` call fails.

### Tests

21. `tests/fake_coordinator.py` implements the `/api/w/*` half of SPEC §8 (plus
    `/w` and `/w/*` static serving, so the browser worker can be driven against
    it) and records every call. `tests/test_worker_unit.py` runs
    `worker/worker.py` as a subprocess against it. Both are self-contained: no
    `conftest.py` (another component owns that file) and nothing is written to
    the project's `data` (pytest `tmp_path` only).

22. `FakeCoordinator` sets `allow_reuse_address = False`. On Windows,
    `SO_REUSEADDR` lets a second server silently bind a port another process
    already owns, which makes tests talk to the wrong server. Tests bind port 0.

23. `test_worker_compiles_on_older_python` uses the local Python 3.10 install
    (or any `python3.8`/`python3.9` on PATH) as a proxy for the 3.8 syntax rule
    and skips when none is present. `test_browser_js_syntax` skips without Node.

## Coordinator (`coordinator/`, `tests/test_*.py` except worker/sdk unit files)

1. **Package is split further than SPEC §1 lists.** Routes live in
   `api_devices.py`, `api_jobs.py`, `api_files.py`, `api_worker.py`,
   `static_routes.py`, `ws_routes.py`; shared runtime state in `state.py`;
   the dashboard WebSocket fan-out in `hub.py`; plugin `ctx` in
   `plugin_ctx.py`; QR rendering in `qr.py`. `app.py` only wires them up.
2. **Register response carries `bench_done: true`** when the device already
   has a `bench_score`, so a restarted worker skips the 3 s benchmark
   (requested by the worker component). Re-registering with a valid
   `device_id` + `token` also updates `name`/`caps`/`info`/`slots`, which is
   how the browser worker's Rename works; `pin` may be null in that case.
3. **`python_map` honours `params.fn_name`** (falls back to `fn`), and
   **`$from_job` references are resolved recursively** through nested dicts
   and lists, as the SDK requires.
4. **Device JSON includes `free_slots` and `running`** (list of task ids) on
   top of the SPEC §4 columns, for the dashboard's "slots in use".
5. **List endpoints return bare JSON arrays.** `GET /api/tasks/{id}/log`
   returns `{"lines": [...]}`. `GET /api/jobs/{id}/result` returns
   `{"id", "status", "result", "error"}`. `GET /api/storage/{id}/status`
   returns `{id, status, replication, n_chunks, chunks:[{idx, chunk_id, size,
   locations:[device_id]}]}`. `PATCH /api/settings` merges per section.
6. **Pooled storage placement:** the PC worker is excluded unless
   `storage.include_pc` is true **or it is the only online device with the
   `storage` capability** (otherwise a one-PC cluster could never store
   anything).
7. **`$from_job` in `params` is resolved when the job leaves `waiting`**; an
   unknown reference fails the job with `could not resolve $from_job`.
8. **Files-typed params accept a bare string.** `Plugin.normalize_params`
   wraps a single file id into a one-element list before `split()`, so a CLI
   `--file inputs=path` (or a hand-written jobspec) cannot fan out into 64
   single-character "file ids". Found during the live smoke test.
9. **`media.format` defaults to empty**, not `mp4`. With the old default a
   `resize` of a PNG was muxed into an H.264 `.mp4`. An empty format now means
   "keep the image's own extension" for images, `mp4` for video, `mp3` for
   `extract_audio`.
10. **Join page / QR never advertise a loopback host.** `_base_url` uses the
    request's `Host` header as SPEC intends, except when it is `localhost` /
    `127.x` / `::1`, in which case the best LAN address is used. The LAN
    address list is ordered with the default-route interface first (found via
    a connected UDP socket, no packets sent), so the real Wi-Fi IP beats
    VirtualBox / hotspot adapters in the banner and on the join page.
11. **Notifications:** Windows toasts are sent with a PowerShell
    `Windows.UI.Notifications` script; if that fails the notification is only
    logged. Webhook posts use httpx with a 5 s timeout and never retry.
12. **Terminal scrollback** of the last 400 chunks per session is kept in
    memory only; nothing about terminals is persisted.
13. **Result outbox and late results (added 2026-09-11).** SPEC §9.4 originally
    rejected every `done` whose device/attempt did not match. Now a device
    listed in the task's `history` may deliver an `ok=true` result late; the
    coordinator marks the task done with that device, cancels the duplicate
    attempt (even when the duplicate runs on the *same* device, detected by
    the `attempt` number the workers now send) and records `task_late_result`.
    Late failures are acknowledged and ignored. Workers keep undeliverable
    successful results in an outbox (`<state>/outbox/` for worker.py,
    `localStorage['pc_outbox']` for the browser) and replay them after the
    next successful poll; SPEC §8/§18 were updated to match.
14. **Job packages (added 2026-09-11).** `POST /api/packages`, CLI `pc run`,
    SDK `run_package()` and the dashboard's *Run package* button. Resolution
    rules live in `coordinator/packages.py` and `docs/PACKAGES.md`. Pipeline
    manifests keep `depends_on` as keys (resolved on promotion), like
    `POST /api/pipelines`. Unpacking refuses absolute paths, `..`, more than
    10 000 files or 1 GiB; a zip whose entries all share one top folder is
    flattened.
15. **`files`-typed params also tolerate the CLI default.** `pc submit --file inputs=path`
    now always sends a list for `inputs`/`files` keys (SDK deviation, mirrored
    by `Plugin.normalize_params` on the coordinator, item 8).
16. **Checkpoints (added 2026-09-11, SPEC §6.4a).** New `tasks.checkpoint`
    column (added by an additive migration in `db.py`), worker endpoint
    `POST /api/w/task/{id}/checkpoint`, `checkpoint` field on the `task`
    command. The Python harness signals checkpoints with a fourth stdout
    sentinel `__PC_CHECKPOINT__<json>` (SPEC §6.1 originally listed three);
    shell tasks use `$PC_CHECKPOINT_FILE`. `python_map` checkpoints per item
    in both its Python and JS runners.
17. **Streaming (added 2026-09-11, SPEC §6.4b).** `inputs` entries may be
    byte-range dicts; both file endpoints honour `Range`. `python_map` gained
    `lines_file` / `lines_per_task` / `line_format`; the coordinator scans the
    file once for line offsets (`FileStore.line_ranges`). `options.stream_results`
    is handled generically in the scheduler (`coordinator/results.py`) and
    replaces `reduce()`; parts live under `data/results/`. Part files are
    written in binary so JSONL keeps `\n` line ends on Windows.
18. **Auto-discovery (added 2026-09-11, SPEC §2.1).** Was explicitly *not*
    chosen in the first round; added on request. UDP responder in
    `coordinator/discovery.py` (config `discovery`, `discovery_port`), probe in
    `worker.py` (`discover()`, `--url auto`, `--discover-host`, re-probe after 3
    connection failures) and in the SDK (`discover()`, `Cluster(url="auto")`,
    `pc discover`, `pc login --discover`). Tests disable the responder in the
    `app` fixture and use loopback targets. The reply URL is derived from the
    interface the probe arrived on, so multi-homed PCs answer with the right
    address for each network.
19. **Browser Python (added 2026-09-11, SPEC §15.1).** Chosen over a
    Python-to-JS transpiler: Pyodide gives real CPython semantics for the
    user's `fn`, so no subset rules to learn. Runtime downloaded into
    `tools/pyodide/` by `scripts/get-pyodide.ps1` (npm tarball, 0.26.4) and
    served at `/w/pyodide/`; `.wasm` gets `application/wasm` explicitly because
    Windows' mimetypes table lacks it. The browser worker advertises `python`
    only when `/api/info.pyodide` is set and WebAssembly exists, pre-warms the
    interpreter after registering, and re-registers without `python` if loading
    fails. Timeouts/cancels terminate the interpreter (Pyodide cannot be
    interrupted without SharedArrayBuffer, which plain-http LAN pages lack).
    `tests/test_pyrunner.py` drives the same `pyrunner.js` in Node against the
    downloaded runtime and is skipped when it is absent.
20. **Services (added 2026-09-11, SPEC §6.4c).** New job type, worker
    capability `service`, service harness child process with a framed stdin
    protocol (so audio reaches the model without a file on the phone), and a
    coordinator relay (`coordinator/services.py`, `api_services.py`). Choices:
    one request at a time per instance (parallelism = more instances); the
    caller's upload is relayed with a 32-chunk in-memory queue (backpressure)
    and never spooled to disk; a request timeout restarts the model process
    because Python cannot be interrupted safely; late `done` reports from a
    device that no longer holds the request are ignored, not 409. The call
    endpoints accept the PIN **or** a device token so a browser worker page
    can call services. Lease refresh on poll (`status.running`) was added so
    service tasks (timeout 0) never expire while their host keeps reporting
    them; it also benefits any long task.
21. **Worker HTTP pool hardening (found while testing services).** Idle pooled
    connections older than 4 s are discarded (uvicorn closes keep-alive
    sockets after 5 s), the retry after a dead pooled socket always opens a
    fresh one, and the log pump now re-queues lines whose post failed with a
    transport error instead of dropping them; the pump thread also survives
    unexpected exceptions.
22. **Browser worker auto-rejoin.** When the join link carries the PIN and the
    saved identity is rejected (coordinator data was reset), the page registers
    again as a new device instead of asking for the PIN.
23. **Distributed LLMs (added 2026-09-24, SPEC §6.4d).** Built on llama.cpp's
    RPC backend (release pinned in `tools/llama.cpp/VERSION`, b11157 at the
    time; the RPC server is named `ggml-rpc-server` in 2026 builds). This is
    the first feature where the coordinator's peers accept **inbound**
    connections: the host's `llama-server` connects to each shard's
    `ggml-rpc-server` over the LAN. The pull-only rule of SPEC §2 still holds
    for everything else. Mitigations for the unauthenticated RPC server
    (user's choice "LAN, only while running"): bound to the device's LAN
    address on a random port, started only inside a shard service, killed with
    the service's process group/tree on stop, cancel or worker exit, and
    `unload()` runs at interpreter exit too. `llama-server` itself binds
    127.0.0.1 and is reached only through the service relay. The split is
    computed by the coordinator (`-ts`, `--fit off`) because llama.cpp's own
    auto-fit keeps 1 GiB per device, which rules phones out. `--tensor-split`
    order follows the `--rpc` order (verified), and the largest share goes
    last because the last device also gets the output layer and logits buffer
    (307 MB measured for Qwen2.5 0.5B, matching n_vocab x 512 x 4).
24. **Service harness grew `ctx.announce()`, `ctx.lan_ip()`,
    `ctx.runtime_dir`, `ctx.model_path`**, and payloads may carry
    `service.needs` (`llama`, `model`) that the worker provisions before
    `load()`. Hidden services (`_` prefix) are omitted from `/api/services`.
    Workers now re-announce running services after re-registering, fixing a
    gap where a restarted coordinator forgot services still running on phones.
25. **Device roles, limits and the Balanced safety profile (added 2026-10-01,
    SPEC §9.3).** The user asked for full control over which connected devices
    take part, a user-set GPU share, and "devices safety should be important"
    (chose *Balanced*). New `coordinator/policy.py`; per-device `settings.roles`
    and `cpu_pct`/`mem_pct`/`gpu_pct`; `paused` became the master switch (also
    stops storage placement and moves models off). Balanced replaces the old
    42/38 °C defaults with 40/36 °C plus a 43 °C level that moves running work
    off without spending attempts, and per-device or cluster settings can now
    only *tighten* the limits (a per-device `min_battery` below 20 % is ignored;
    the old test asserting the opposite was changed). Models need charging or
    50 % battery. Usable model memory is additionally capped at 70 % of free.
    Services no longer hold a worker job slot; a phone hosting a model runs one
    normal task beside it, a PC half its slots.
26. **All cores (SPEC §9.3).** Worker default slots: cores − 1 (was
    min(4, cores)); a count saved in `device.json` sticks only when it came from
    `--slots` or the dashboard (`slots_set`). `local_worker_slots: 0` (new
    default) means "worker default"; the old written default `4` in this
    project's `data/config.yaml` was changed to 0. The coordinator lowers the
    slots further to `cpu_pct`.
27. **Automatic model placement and the PC GPU (SPEC §6.4d).** `mode: auto`
    (default when no host/weights are given) chooses single-device or split by
    a rough time-per-token model over every candidate and combination;
    single-device deployments run `llama-server` alone (no RPC) and, on the PC,
    with the Vulkan build (`tools/llama.cpp/win-vulkan-x64`, downloaded by
    `get-llama.ps1 -Vulkan`, nothing installed or configured system-wide). A
    PC shard can expose CPU and GPU as two RPC devices (`-d CPU,Vulkan0`; the
    head then needs one `-ts` entry per part — verified on b11157, and the
    shard banner lists both). Two process-local environment variables are
    needed: `GGML_VK_VISIBLE_DEVICES` (keep the integrated Radeon, which shares
    system RAM, out) and `DISABLE_LAYER_AMD_SWITCHABLE_GRAPHICS_1=1` (AMD's
    implicit Vulkan layer on this hybrid laptop makes `vkEnumeratePhysicalDevices`
    loop forever: "Trimming device count from 2 to 1"). GPU use requires a
    temperature reading (`nvidia-smi`, read-only), so other cards are ignored.
    The phones' GPUs are not used: llama.cpp's Vulkan backend does not run
    usefully on Adreno 612/308 or Mali-T720.
28. **Planner memory model rewritten.** `gguf.py` now reads the tensor
    directory, so per-layer, embedding and output sizes are exact. The old
    "logits buffer = n_vocab × 512 × 4 on the last device" was wrong for this
    build (measured: the device holding the output needs output weights +
    ~49 MB compute buffer; the CPU output buffer is 0.6 MB): it is replaced by a
    per-part compute buffer (`512 × n_ff × 16` bytes + `n_embd × 4.5` per context
    token, within 3 MB of measurement) and the real output tensor size. Auto
    plans leave one layer of slack per part because llama.cpp rounds `-ts`
    proportions (4,8,12 became 5,8,11 in a test).
29. **Speed: slow-device skipping, backups and the speed report (SPEC §9.5).**
    The user asked that normal jobs never be slower than the PC alone.
    Assignment is now task-major (each task to the fastest device that may
    take it) instead of device-major; same order for the existing tests.
    Backup copies are tracked in memory only (a coordinator restart forgets
    them; a late result from a forgotten copy gets the usual 409). Jobs table
    gained a `report` column (additive migration). Measured on the PC + a
    browser tab: 0.94× on a 0.4 s job (hand-over overhead dominates) and 1.08×
    on a 1.7 s job, against the fastest single device.
30. **Automatic task sizing (added 2026-10-15, SPEC §9.6).** Jobs of the four
    divisible built-ins no longer split up front unless a chunk size is given:
    the defaults of `chunk_size` (python_map, prime_range, hash_search),
    `lines_per_task` and `chunks` (monte_carlo_pi) changed to 0 = automatic,
    and the SDK/CLI stopped sending 10 / 1000. `total_tasks` therefore grows
    while such a job runs; progress is in units (`progress_units`). The first
    live run exposed two traps, both fixed: Python start-up (~0.15 s per task)
    made small pieces look slow, so speeds are learned from a new
    `work_duration` (a fourth harness sentinel, `__PC_WORK__`, marks where the
    task's own code starts); and the prime sieve's fixed per-piece cost made
    tail pieces of 1 000 numbers look 20× slower, so pieces never go below
    0.5 s of work and the speed memory is time-weighted instead of averaging
    samples. Result on the PC + a browser tab: 37-49 pieces and 1.53-1.55×
    the fastest device alone on 80 M numbers (was 136 pieces before the fixes;
    1.08× with fixed chunks); sub-second jobs stay around 1×.
31. **Speed memory (SPEC §9.5).** New `speeds` table and `GET /api/speeds`;
    rates are units per second per kind of work, which also captures "this job
    runs faster in the browser's JavaScript than in the PC's Python" (7.3 M vs
    1.2 M numbers/s per slot for primes) without a separate impl comparison.
    Benchmark scaling is only between devices of the same benchmark kind.
32. **Requests first (SPEC §9.7).** Normal tasks on a device answering a
    service request are *suspended* (whole process tree; `NtSuspendProcess` via
    ctypes on Windows, `SIGSTOP` on POSIX), not cancelled, so no work is lost;
    holds expire on their own after `hold_seconds` in case a release is lost.
    Worker commands `hold` / `release` are new; old workers log them as
    unknown and simply keep working. Batch task processes always run below
    normal priority. Verified live: 11 running pieces paused during a chat and
    resumed after it.
33. **Models load on call and unload when unused (SPEC §6.4d).** A service call
    may now *start* something: any model file can be called by its name
    without `.gguf`, `chat` reaches the default model. This applies to device
    tokens too (a phone can wake a model), which is what the user asked for.
    New deployment status `idle`. Live: `pc llm ask chat ...` with nothing
    loaded answered in 2.3 s including loading onto the GPU; a 6 s idle setting
    unloaded it with no `llama-server` left.
34. **More copies of busy services (SPEC §6.4c).** Only models that run alone
    on one device are copied (a split model would need a second set of
    devices); copies of LLMs are hidden "replica" deployment rows so the
    service relay's existing load balancing just sees more instances. Not
    tried live (it needs a second device that can hold a model); covered by
    tests.
35. **Results are reused (SPEC §9.8).** The SPEC said every submitted job runs.
    Repeatable job types (maps, number crunching, media, WebAssembly) now answer
    identical work from stored results, whole jobs and fixed-split tasks alike,
    which the user chose ("on, opt-out per job"). A function that reads the
    clock, the network or random numbers must say `cache: false`; scheduled jobs
    default to `cache: false` because a schedule repeats a job on purpose.
    Automatically sized pieces are not reused one by one (their boundaries
    differ between runs); only the whole job is.
36. **Some tasks run twice (SPEC §9.9).** About 5 % of the tasks of exact job
    types are run again on another device and compared ("warn and recheck").
    The extra rows (`role` check/decide) are invisible to reduce, counters and
    the speed report, and a job waits for its checks, at most 2 minutes for a
    free device. A check only compares like with like: the same impl kind
    (Python vs Python, JS vs JS), because monte_carlo_pi's Python and JS use
    different random generators. Media is never checked: ffmpeg builds differ
    byte for byte. A device that gave wrong answers 3 times in a day gets no
    unpinned normal tasks until the user trusts it again; it stays connected.
    Finished jobs can return to `running` when a device that worked on them
    is caught; downstream pipeline jobs that already used their result are not
    re-run (the event names the reopened jobs).
37. **Workers accept inbound connections for phone-to-phone transfers (SPEC
    §8.3).** SPEC §2 said workers never accept inbound connections. A
    `peer`-capable worker now opens a port on its LAN address, but only while
    the coordinator has asked it to send one file, only for requests carrying
    that offer's one-time token, and closes it when the fetcher is done or
    after 60 s unused ("only while sending", the user's choice). Every piece is
    checked against the coordinator's own hashes, so a bad phone cannot slip in
    wrong bytes. Browser workers neither send nor receive (no ports, and no
    `crypto.subtle` over plain http for a cheap check). As with split models,
    routers with client isolation block it; the worker then quietly downloads
    from the coordinator.
38. **Erasure-coded storage (SPEC §10).** Blobs stored while three or more
    storage phones are online use parity pieces instead of full copies by
    default (`storage.scheme: auto`), unless a number of copies is given
    explicitly (the API's `replication` keeps its old meaning, so older
    scripts and tests behave as before). Existing copy-based blobs stay as they
    are and remain readable. The code is pure Python (`bytes.translate` +
    big-integer XOR), no numpy and nothing to download; encoding 16 MB with two
    parity pieces takes about 0.06 s on the PC.
39. **Storage scrubbing.** Phones now re-hash their stored pieces (a few per
    minute, all of them once a day) and delete damaged ones, which the
    coordinator rebuilds. Not in the original SPEC.
40. **WebAssembly tasks (SPEC §6.9).** A new impl kind and job type. The PC
    uses a pinned wasmtime release downloaded into `tools/wasmtime` (approved;
    checksum verified, nothing installed on the system). Termux phones run
    `pkg install wasmtime` on their first WebAssembly task, inside Termux, as
    the user chose; a phone where that fails stops offering `wasm`. The browser
    worker runs WebAssembly itself with a small WASI layer of its own (no
    library). Its folders are flat (`/in`, `/out` without subfolders) and
    calls that need a real OS (sockets, sleeping, directories) answer
    "not supported". The example modules in `examples/wasm` are built by a
    small Python script because no compiler is installed; real programs come
    from a compiler (docs/WASM.md).
41. **Tasks report which impl ran (`impl` in the done body).** Needed so checks
    compare like with like; old workers that do not send it are handled by
    inferring from the device kind.
42. **A worker downloads a shared input once.** Found live: when several tasks
    on one worker started together and needed the same input (a `.wasm`
    module), they all wrote the same temporary file and some failed with
    "file not found" (retried successfully). Temporary names are now unique
    and a per-file lock makes the other tasks wait for the first download.
43. **Keeping outsiders out (SPEC §3), user's choices: one-time join codes,
    HTTPS with the cluster's own certificate, the dashboard only on this PC,
    split models kept as they were.** Changes against the original SPEC:
    the PIN no longer joins devices (only the PC's own worker, from the PC
    itself) and is never shown by `/join`, `/install.sh` or `/qr.svg`, which
    were public before; `/join` and `/qr.svg` now open only on the PC, and
    `/install.sh` only for a valid code. The cluster listens on two ports: 8765
    is HTTPS only (phones), 8764 is plain HTTP bound to 127.0.0.1 (dashboard,
    CLI, SDK, the PC worker), so the PC's own browser shows no certificate
    warning and phones get encryption. Certificates come from a CA the cluster
    makes in `data/tls` with the `cryptography` library (installed into
    `.venv`, the one new download); nothing goes into the Windows certificate
    store, so browsers on phones warn once about the unknown certificate. The
    dashboard cookie is now a random session token, not the PIN. Lockouts slow
    any guessing to a crawl. Joined devices from before must join again with a
    code (their saved identity lacks the CA, and their URLs were plain HTTP).
44. **Termux installs check the PC's key.** The one-liner uses `curl -k
    --pinnedpubkey`: certificate-authority checks are off (the cluster's CA is
    not in Termux's store yet) but curl refuses any server whose public key is
    not the PC's, which is stricter. Checked live with Windows' curl: the right
    key gets the script, a wrong key gets exit 90 and nothing.
45. **What is still not protected.** (a) The llama.cpp RPC servers of a model
    split across devices: no password and no encryption while that model runs
    (the user chose to keep splitting as it was). (b) A browser worker cannot
    check the certificate itself; if someone on the Wi-Fi intercepted the J2's
    first visit, they could take its join code (single use: the J2 would then
    fail to join, which shows). (c) Anyone who can use the PC itself can use
    the dashboard with the PIN; the PC is trusted. (d) The firewall rule is a
    Windows setting the user makes; the README gives the narrowest form.
46. **Test helper fix.** `WorkerProc.wait_registered` in
    `tests/test_worker_unit.py` now also waits for the worker's
    `device.json`: it used to stop the worker the moment the coordinator saw
    it register, sometimes before the worker had saved its identity, which
    made two outbox tests fail about one run in three.
47. **The cluster's own Python (`tools/python`) instead of `.venv`.** A
    venv's `python.exe` only launches the system Python
    (`C:\Python312\python.exe`), so the process that listens was the same
    program every other Python script on the PC uses, and a firewall rule
    naming it opened the port for all of them. `scripts/get-python.ps1` puts
    Python's official embeddable package (3.12.10, the newest 3.12 with a
    Windows build; MD5 and PSF signature checked) in `tools/python`: a plain
    zip, no installer, no registry, PATH or py-launcher entries. Its
    `._pth` file makes it isolated (PYTHON* variables, the script's folder
    and user packages ignored) and puts the project and `sdk` on its path;
    pip ignores pip settings files outside the project (this PC has global
    ones adding an extra package index). The coordinator, PC worker and
    task harnesses now run as `sys.executable -u -X utf8` (the embeddable
    Python has no `PYTHONUNBUFFERED`/`PYTHONUTF8` from the environment) and
    temporary files go to `data/tmp` instead of the user's TEMP. The
    README's firewall rule now names `<project>\tools\python\python.exe`.
    `.venv` stays as a fallback for the scripts and may be deleted.
48. **Terminal sessions outlive the page (SPEC §10).** The dashboard used to
    close the shell whenever you left the Terminal page, and its output and
    command history lived only in that page, so a look at Overview lost
    both. Now leaving or reloading keeps the shell; the coordinator's
    scrollback also records the commands typed (`$ <line>`), is readable at
    `GET /api/terminal/{sid}`, and `term` events carry a `pos` counter so the
    replay and live output never double up. History is per browser tab
    (`sessionStorage`); open shells the tab doesn't know about are offered
    with Resume / Close. Idle-reaped shells and shells of a device that left
    now tell the page (`term_closed`), which they didn't before.
49. **Battery and temperature on Linux workers (SPEC §8).** The worker read
    them only on Android/Termux and Windows, so the J2 running Linux showed
    neither, and the Balanced heat and battery limits could not protect it.
    It now reads `/sys/class/power_supply` and the thermal zones on any
    Linux; laptops report their battery but no temperature, desktops nothing.
50. **A phone's screen on the PC (SPEC §10, user asked for scrcpy).** The
    Devices page has a **Screen** button for Android phones that opens the
    phone in a scrcpy window on the PC (watch and control). scrcpy v5.0.1
    is pinned in `tools/scrcpy` with its own adb (SHA-256 checked). That
    adb uses its own server port (5039) so another adb on the PC (the
    user's debloat tool runs one) is never restarted, and it stops with the
    cluster. One thing could not be kept inside the project: adb on Windows
    always reads the Windows account's key in `%USERPROFILE%\.android`
    (settings to move it are ignored, checked with adb's own trace); it is
    only read, and it already existed. The phone must allow USB debugging;
    adb over Wi-Fi is the owner's choice (`adb tcpip 5555`), the cluster
    never switches it on. Found live: with two phones on Wi-Fi, opening the
    second closed the first. Both phones refuse adb's reverse tunnel over
    Wi-Fi ("more than one device", even with `-s`), and scrcpy's fallback
    forward tunnel took port 27183 for both. Each window now gets its own
    port; checked live with the Redmi 5A and the J2 open together.
51. **One-liners for laptops and other PCs (SPEC §3, user asked).** The
    Termux script only worked where a `python` command exists, which most
    Linux systems and Macs lack (they have `python3`), and it then ran
    Termux's `pkg`. It now picks `python3`, else `python` (3.8+), and runs
    `pkg install python` only on Termux, so the same one-liner adds a Linux
    or Mac computer (Mac untested; it reports itself as `linux`). Windows
    had no way in short of copying the project, so the Join page now has a
    PowerShell one-liner (`/install.ps1`, valid code only) that puts
    everything into `%USERPROFILE%\phonecluster`, including a
    `start-worker.cmd` for later. Python comes from the PC (`/python-win.zip`:
    `tools/python` without its libraries), not the internet, so a laptop
    needs no internet and trusts only the pinned PC; a laptop's own Python is
    the fallback when the PC has none to lend (or Windows is 32-bit). The
    worker keeps its state in that folder (`--state-dir state`) instead of
    `~\.phonecluster`, so deleting the one folder removes everything. The
    script avoids script modules (`Expand-Archive`, `Get-FileHash`) so it
    also runs under Windows' default "Restricted" script policy; the
    end-to-end test runs it that way.
