# PhoneCluster SDK and CLI

`sdk/phonecluster` — stdlib only, no dependencies, Python 3.8+ (it runs in Termux too).
Implements the surface in [SPEC.md](SPEC.md) section 13 against the REST API of section 11.

```powershell
# in the project folder; no install needed
$env:PYTHONPATH = "$PWD\sdk"
python -c "from phonecluster import Cluster; print(Cluster().stats())"

# or use the cluster's own Python, which has sdk on its path already
& .\tools\python\python.exe -c "from phonecluster import Cluster; print(Cluster().stats())"
.\scripts\pc.ps1 devices   # the CLI
```

## Connecting

```python
from phonecluster import Cluster
c = Cluster(pin="482913", timeout=30)   # default http://127.0.0.1:8764: the dashboard API answers only on the PC
c = Cluster()                    # resolve from the environment instead
c = Cluster(url="auto", pin=…)   # find the coordinator by LAN broadcast (UDP 8766)

from phonecluster.client import discover
discover()                       # -> "https://…:8765" or None
discover(all_replies=True)       # -> [{"url", "name", "urls"}, …]
```

Resolution order for both `url` and `pin`: **constructor argument → `PC_URL` / `PC_PIN` →
`<root>/data/cli.json` → `~/.phonecluster/cli.json`**, where `<root>` is `PC_ROOT` or the
directory holding `sdk/`. `pc login` writes `data/cli.json`. Every `/api/*` request carries
the `X-Pin` header. The SDK and CLI manage the cluster **from the PC only** (SPEC 3): another
machine gets 403 even with the PIN. An `https://` URL (the LAN door) is checked against the
cluster's own CA, `data/tls/ca.pem` (or `PC_CA`); any other certificate is refused.
`c.join()` / `pc join` make a one-time join code for a new device.

## Errors

| exception | raised when |
|---|---|
| `ClusterError(status, message)` | any non-2xx response (`.status`, `.message` — the server's `error` text), or an unreachable coordinator (`status == 0`) |
| `JobFailed(job, error)` | `Job.wait()` on a job that ended `failed` or `cancelled` (`.job`, `.job_id`, `.status`, `.error`) |
| `TimeoutError` | `Job.wait(timeout=…)` expired |
| `PhoneClusterError` | base class of the two above; also bad SDK usage |

## Running work

```python
job = c.map(fn, items, chunk_size=10, name=None, js_fn=None, inputs=None,
            timeout=600, priority=0, max_attempts=3, wait=False, stream_results=False)
job = c.map_lines(fn, "huge.txt" | file_id, lines_per_task=1000, line_format="auto",
                  stream_results=True, ...)        # one item per line, fetched by byte range
```

`ctx.checkpoint(state)` / `ctx.resume(default)` inside `fn` make long tasks resumable
(map jobs checkpoint after every item on their own). With `stream_results=True` the job's
result is `{"results_file", "count", ...}` and the items come from `job.iter_results()`.

Services (a model hosted on one device, docs/SERVICES.md):

```python
job = c.start_service("vosk-en", "service.py", setup_cmd="pip install vosk",
                      accepts="audio/wav", produces="text", streaming_output=True,
                      request_timeout=600, params={...}, targets=["<device id>"])
c.services()                                  # [{name, instances:[{device_name, status, ...}], ...}]
r = c.call("vosk-en", path="clip.wav", params={"lang": "en"}, on_partial=print)
r = c.call("translate-demo", text="good morning", params={"to": "fr"})
r = c.call("image-describe", data=png_bytes, content_type="image/png")
r["result"], r["partials"], r["outputs"], r["ms"], r["device_id"]
c.requests(limit=50); c.service_request(id); c.cancel_request(id)
c.stop_service("vosk-en")                     # cancels the hosting job(s)
```

`call()` raises `ClusterError` with status 404 (no such service), 503 (all busy), 502
(the service failed) or 504 (timed out).

AI models (docs/LLM.md), devices by name or id. Without `host`/`weights` the placement is
automatic (alone on the fastest device that holds it, split when it is too big; `devices`
narrows the choice); with them, or `mode="manual"`, exactly `devices` hold the layers:

```python
c.llm.models(); c.llm.upload_model("m.gguf"); c.llm.fetch_model(url); c.llm.delete_model(name)
c.llm.devices()                                # memory, GPU, llama.cpp build, eligibility
c.llm.plan(model)                              # automatic: -> {placement, summary, devices, ...}
c.llm.plan(model, ["note10", "redmi5a"], host="pc", weights={"note10": 3}, ctx=2048)
c.llm.deploy(name, model, devices=None, host=None, weights=None, ctx=2048, mode=None,
             threads=0, force=False, wait=True, timeout=None)
c.llm.deployments(); c.llm.deployment(name); c.llm.wait_ready(name)
c.llm.keep(name, True)            # never unload; False: unload after llm.idle_minutes unused
c.llm.default(); c.llm.set_default("model.gguf")   # what the name "chat" reaches
c.llm.chat("chat", prompt="hi")   # any model name works even when not loaded: it loads first
c.llm.chat(name, prompt="hi" | messages=[...], system=None, max_tokens=512,
           temperature=None, on_token=print)  # -> {text, tokens, tokens_per_second, ...}
c.llm.stop(name); c.llm.restart(name); c.llm.remove(name)
```

`fn` must be a **top-level function `fn(item, ctx)` defined in a real file** — its source is
shipped with `inspect.getsource` (dedented). Leave `chunk_size` out (the default): the cluster
then cuts the items into pieces sized to each device's measured speed as the job runs (SPEC
9.6); a number fixes the chunk size. The SDK sends the `python_map` params
`code`, `fn_name`, `items`, `chunk_size` (when given), plus `js_code` (from `js_fn`, either JS source or a
`.js` path) and `inputs` (a list of file ids) when given. When the items encode to more than
2 MB of JSON they are uploaded to the file store and sent as `items_file` instead.
`ctx` inside `fn` is the worker harness context of SPEC 6.1: `ctx.log`, `ctx.inputs`,
`ctx.input(file_id)`, `ctx.output(path)`, `ctx.workdir`, `ctx.progress`.

```python
c.submit(type, params, name=None, priority=0, depends_on=None, timeout=None,
         max_attempts=None, requires=None, targets=None, chunk_size=None,
         fail_fast=None, options=None, wait=False, cache=None, verify=None) -> Job
c.submit_spec({...}) -> Job                       # a raw job spec (SPEC 5)
c.shell(cmd, targets="any"|"all"|[ids], timeout=600, cmd_windows=None) -> Job
c.pipeline([{"key": "a", "type": ..., "params": {...}},
            {"key": "b", "depends_on": ["a"], ...}], name=None) -> Pipeline
```

The keyword options are folded into the job spec's `options` object; `name`, `priority` and
`depends_on` stay at the top level. `cache`: repeatable job types reuse stored results of
identical work by default; `False` computes anyway, `"refresh"` computes and replaces the stored
result (SPEC 9.8). `verify`: `False` turns second-device checks off for the job, a number
(0..1) sets the share of tasks checked (SPEC 9.9). `map()` and `wasm()` take both too.

```python
# WebAssembly (docs/WASM.md): the same .wasm on the PC, phones and browsers
c.wasm(module, call=None, range=None, args_list=None, inputs=None, stdin=None, env=None,
       combine="auto", grain=None, memory_mb=None, name=None, timeout=600, wait=False,
       cache=None, verify=None) -> Job
#   module / inputs: local paths (uploaded for you) or file ids
c.wasm("primes.wasm", call="count_primes", range=(2, 10**8), wait=True).result()   # {"sum": 5761455, ...}
c.wasm("wordcount.wasm", inputs=["a.txt", "b.txt"], wait=True).result()          # [{"input", "result", "outputs"}]

# results of identical work, and rechecking devices
c.rerun(job_id, fresh=False) -> Job     # the same spec again; fresh = ignore + replace stored results
c.cache_info(limit=50) -> {"stats": {entries, jobs, tasks, bytes, hits, saved_s, ...}, "entries": [...]}
c.clear_cache()
c.trust() -> [{device_id, name, state, score, checks, agreed, wrong, benched, note}]
c.trust_again(device)                   # forgive a device that gave wrong answers (id or name)
```

## Job

| member | does |
|---|---|
| `.id` `.name` `.type` `.status` `.error` `.data` | last server payload (`.data` is the raw dict) |
| `.done` `.progress` | terminal-state flag, 0..1 from `done_tasks`/`total_tasks` |
| `.refresh()` | re-fetch `GET /api/jobs/{id}` |
| `.wait(poll=1.0, timeout=None)` | poll until the job finishes; raises `JobFailed` / `TimeoutError` |
| `.result()` | `GET /api/jobs/{id}/result` (unwraps a `{"result": …}` envelope) |
| `.tasks()` | list of task dicts |
| `.logs(task_idx=None)` | log text for one task, or every task with `[task N]` headers |
| `.cancel()` `.retry()` `.delete()` | job control |
| `.rerun(fresh=False, wait=False)` · `.cached` | a new job with the same spec; whether this job was answered by an identical earlier one |
| `.export(path, format="zip")` | download `zip` / `json` / `csv`; a directory path becomes `<id>.<format>` |
| `.outputs()` | `[{file_id, name, size, task_idx}, …]` gathered from the tasks |
| `.download_outputs(dir)` | download them all; returns the written paths |
| `.iter_results(wait=False)` | yield items from `GET /api/jobs/{id}/results` (JSON lines) one at a time; a running streamed job yields what exists so far |
| `.download_results(path, wait=True)` | save that stream to a `.jsonl` file |

`Pipeline`: `.id`, `.jobs` (key → `Job`), `.wait()`, `.results()` (key → result),
`.statuses()`, and `pipeline["key"]`.

## Devices, files, storage, and the rest

```python
c.devices() ; c.device(id) ; c.update_device(id, **fields) ; c.rename_device(id, name)
c.pause_device(id, paused=True) ; c.remove_device(id) ; c.notify(id, title, body)
c.device_controls(id_or_name, on=None, jobs=None, ai=None, storage=None,
                  cpu_pct=None, mem_pct=None, gpu_pct=None)
                          # roles and limits (capped by the Balanced safety profile);
                          # the returned device has a `control` block with notes
c.job(id).data["report"]  # finished jobs: speed against the fastest device alone
c.speeds()                # learned rates: [{key, device, rate (units/s), samples}]
c.bench(device_id)        # -> dict from POST /api/devices/{id}/benchmark
c.bench()                 # -> Job: a `benchmark` job with targets "all"

c.jobs(status=None, limit=None, offset=None) -> [Job]   ;  c.job(id) -> Job
c.task(id) ; c.task_log(id, tail=None) ; c.cancel_task(id)

c.files.upload(path) -> file_id          # raw PUT /api/files?name=, streamed
c.files.upload_bytes(data, name) -> file_id
c.files.download(file_id, dest)          # dest may be a directory
c.files.list() ; c.files.meta(id) ; c.files.delete(id)

c.storage.put(path, replication=None, scheme=None) -> blob_id     # hand-built multipart POST
c.storage.put_file_id(file_id, replication=None, scheme=None) -> blob_id
#   scheme: "auto" (parity pieces from 3 storage phones on, else copies), "copies" or "ec";
#   replication alone asks for that many full copies
c.storage.get(blob_id, dest) ; c.storage.status(id) ; c.storage.list() ; c.storage.delete(id)

c.schedules.list() / create(name, cron, job_spec, enabled=True) / update(id, **f)
             / delete(id) / run(id)
c.templates.list() / create(name, job_spec) / delete(id)

c.stats() ; c.events(limit=50, kind=None) ; c.plugins() ; c.info() ; c.join()
c.settings() ; c.update_settings({...}) ; c.login()
c.terminal(device_id)                    # interactive; used by `pc terminal`
```

Uploads stream from disk in a single request with an explicit `Content-Length` — a 1 GB
file never lands in memory.

## Terminal and the WebSocket client

`c.terminal(device_id)` opens a session (`POST /api/terminal`), connects a **minimal RFC 6455
client** to `/ws/dashboard?pin=…`, sends `{"op":"term_input","session_id","data"}` for each
line you type, prints `{"ev":"term"}` events for that session, and exits on `term_closed` or
Ctrl+C (then `DELETE /api/terminal/{sid}`). Lower level pieces, useful on their own:

```python
ws = c.dashboard_socket()          # -> WebSocket, already handshaken
ws.send_json({"op": "sub_logs", "task_id": "..."})
event = ws.recv_json(timeout=1.0)  # None on timeout or close
ws.close()
ws_encode_frame(opcode, payload, mask=True)   # client frames are always masked
ws_decode_frame(buffer, offset=0)             # -> (fin, opcode, payload, next) | None
```

Text frames, continuation frames, ping/pong and close are handled; `wss://` works via `ssl`.

## CLI

`python -m phonecluster <command>`, `scripts\pc.ps1 <command>`, or `pc <command>` after a
`pip install -e sdk`. Global flags: `--url/-u`, `--pin/-p`, `--http-timeout`. **Exit code 1
on any failure** (2 for bad arguments, 130 on Ctrl+C).

| command | notes |
|---|---|
| `login [url\|auto] [pin] [--discover]` | writes `data/cli.json`; prompts for anything missing; `auto` / `--discover` find the coordinator by broadcast |
| `discover [--timeout S]` | list coordinators answering on the LAN |
| `join [--termux]` | a one-time join code with the Termux / Linux / Mac one-liner, the Windows one-liner and the browser URL, plus an ASCII QR of the browser URL (`--termux`: of the Termux command) |
| `run <folder\|package.zip> [--dry-run] [--wait]` | submit a job package (docs/PACKAGES.md) |
| `services` · `service stop <name>` · `requests [--limit N]` | running model services, stop one, recent requests |
| `llm models\|fetch <url>\|upload <path>\|devices` | GGUF models and which devices can take one (memory, GPU) |
| `llm plan\|run <model> [--on DEV]... [--host DEV] [--weight DEV=N] [--manual\|--auto] [--ctx N]` | preview or start a model; automatic placement unless `--host`/`--weight`/`--manual` (`run` also takes `--name`, `--threads`, `--force`, `--wait`) |
| `llm list\|stop\|restart\|rm <name>` · `llm ask <name> "<prompt>"` · `llm chat <name>` | manage and talk to models (a model that is not loaded loads first; the name `chat` reaches the default model); `chat` keeps the conversation |
| `llm keep <name> on\|off` · `llm default [model.gguf]` | keep a model loaded / let it unload when unused; show or set what `chat` reaches |
| `speeds` | what the cluster learned about each device's speed per kind of work |
| `call <service> [file\|-] [--text T\|--json J] [--param k=v] [--follow] [-o dir]` | send a request to a service; `--follow` streams partials to stderr, `-o` downloads output files |
| `devices` · `device <id>` · `remove <id>` · `pause <id>` · `resume <id>` | |
| `device-set <device> [--on\|--off] [--jobs on\|off] [--ai on\|off] [--storage on\|off] [--cpu PCT] [--mem PCT] [--gpu PCT]` | what a connected device is used for and how hard it may work; prints roles, limits and safety notes |
| `jobs [--status S] [--limit N]` · `job <id>` | job table / job detail with its tasks and the speed report (also: reused results, double-checked tasks) |
| `submit <type> [--param k=v] [--file k=path] [--wait]` | also `--name --priority --timeout --max-attempts --chunk --requires --targets --device --all --fresh --no-cache` |
| `wasm <module.wasm\|id> [--call F] [--range A B] [--grain N] [--args JSON]... [--input F]... [--stdin T\|-] [--combine auto\|list\|sum\|text] [--memory MB] [--fresh\|--no-cache] [--wait]` | run one WebAssembly module on every kind of device (docs/WASM.md) |
| `rerun <job> [--fresh] [--wait]` | the same job again; `--fresh` ignores and replaces stored results |
| `cache [show\|clear] [--limit N]` | stored results: how many, how big, how often reused |
| `trust [--again DEVICE]` | how each device's double-checked answers went; `--again` forgives one |
| `map <file.py:fn> <items.json\|-\|big.txt> [--chunk N] [--lines] [--stream] [--wait]` | imports the file, ships the function; `-` reads the JSON list from stdin; `--lines` maps over every line of a big file by byte range (`--line-format text\|json\|auto`); `--stream` writes results as JSONL |
| `shell "<cmd>" [--all\|--device id] [--wait]` | `--wait` prints per-device stdout |
| `cancel <job>` · `retry <job>` · `result <job>` | |
| `results <job> [-o file] [--wait]` | stream the job's items as JSON lines (what exists so far, or everything after `--wait`) |
| `logs <job\|task> [-f] [--task N]` | `-f` polls every second until the job finishes |
| `export <job> [--format zip\|json\|csv] [-o path]` | defaults to `<job_id>.<format>` |
| `files list\|upload <path>\|download <id> <dest>\|rm <id>` | |
| `storage list\|put <path> [--scheme auto\|copies\|ec] [--replication N]\|get <id> <dest>\|rm <id>\|status <id>` | the list shows each blob's protection (e.g. `3+1 erasure coded`, `2 copies`) |
| `schedules list\|add <name> "<cron>" <jobspec.json>\|rm <id>\|run <id>` | |
| `templates list\|save <name> <jobspec.json>\|rm <id>` | |
| `terminal <device>` | line-oriented remote shell over the dashboard WebSocket |
| `stats` · `events [--limit N] [--kind K]` · `plugins` · `bench [device] [--wait]` | |

`--param k=v` parses the value as JSON when it can (`count=1000` → int, `prefix="0000"` →
string, `flags=[1,2]` → list) and otherwise keeps the raw string, so `prefix=0000` is the
string `"0000"`. Repeating a key builds a list. `--file k=path` uploads the file and passes
its id as parameter `k`; repeat it for a list of ids.

```powershell
scripts\pc.ps1 login http://127.0.0.1:8764 482913
scripts\pc.ps1 devices
scripts\pc.ps1 submit hash_search --param prefix=00000 --param count=2000000 --wait
scripts\pc.ps1 map examples\map_squares.py:square items.json --chunk 20 --wait
scripts\pc.ps1 shell "uptime" --all --wait
scripts\pc.ps1 logs 3f2a91b0c4d1 -f
```

The QR needs the optional `qrcode` package (it is in `tools\python`); without it `join`
prints `open <url>/join for the QR`.

## Examples

Every script in `examples/` puts `../sdk` on `sys.path` first, so nothing needs installing:

| file | shows |
|---|---|
| `map_squares.py` | `c.map` over 1..200, chunking, per-task report |
| `wordcount.py` | upload a file, map line ranges with `inputs=[…]`, reduce client-side |
| `hash_search.py` | the `hash_search` plugin, live progress |
| `monte_carlo_pi.py` | `monte_carlo_pi`, work distribution per device |
| `prime_range.py` | `prime_range` with `--list` |
| `shell_all.py` | one command on every device, `cmd_windows` fallback |
| `resize_images.py` | upload a folder, `media` resize, download outputs |
| `pipeline_demo.py` | two-stage pipeline with `$from_job` |
| `sensor_snapshot.py` | `sensor` photos from every Termux:API phone |
| `schedule_demo.py` | create / list / fire / delete a cron schedule |
| `storage_demo.py` | pooled storage round trip with a sha256 check |

## Tests

```powershell
& .\tools\python\python.exe -m pytest tests\test_sdk_unit.py -q   # in the project folder
```

`tests/test_sdk_unit.py` runs the whole SDK and CLI against a stdlib `http.server` fake
coordinator (no network, no coordinator needed) and unit-tests the WebSocket frame codec
against hand-built frames.
