# PhoneCluster

Turn old Android phones and a Windows PC into a free LAN compute and storage cluster.
No root, no cloud, no accounts. Everything lives inside this folder, wherever you put it (the examples
below use `D:\cluster`; any folder works).

```
 phones (Termux or Chrome)  ──Wi-Fi──▶  coordinator on the PC (port 8765)  ◀──  dashboard / CLI / Python SDK
                                          scheduler · SQLite · file store · ffmpeg
```

## Quick start (PC)

Double-click **`start.bat`**: it runs setup on the first use, starts the coordinator and the PC worker
in their own window, and opens the dashboard. **`stop.bat`** stops everything. The same steps by hand:

```powershell
scripts\setup.ps1        # once: the cluster's own Python + its libraries into tools\python (nothing outside this folder)
scripts\get-ffmpeg.ps1   # once, optional: static ffmpeg into tools\ffmpeg for media jobs
scripts\get-pyodide.ps1  # once, optional: Python-in-the-browser runtime so browser phones run Python jobs
scripts\get-llama.ps1 -Vulkan  # once, optional: llama.cpp (CPU + PC graphics card builds) for AI models
scripts\get-model.ps1    # once, optional: a small test model for that (469 MB)
scripts\get-wasmtime.ps1 # once, optional: WebAssembly runtime for the PC (13 MB) so one .wasm runs everywhere
scripts\get-scrcpy.ps1   # once, optional: scrcpy (11 MB) to see and control a phone's screen from the Devices page
scripts\start.ps1        # starts the coordinator + the PC worker; prints the dashboard URL and PIN
```

The dashboard opens at **http://127.0.0.1:8764**, and only on this PC: other devices on the Wi-Fi cannot
open it, even with the PIN. Enter the PIN (it is in `data\config.yaml` and the coordinator window), then go
to **Join** to add phones.

Phones reach the PC on port 8765 (HTTPS). If Windows blocks them, allow it once (admin PowerShell; this is
the one Windows setting the cluster needs, and you decide whether to make it). Add it **before the first
start**, so Windows doesn't ask. Put your folder in place of `D:\cluster` (`scripts\setup.ps1` prints the
first command with it filled in):

```powershell
netsh advfirewall firewall add rule name="PhoneCluster" dir=in action=allow program="D:\cluster\tools\python\python.exe" protocol=TCP localport=8765 profile=private remoteip=localsubnet
netsh advfirewall firewall add rule name="PhoneCluster discovery" dir=in action=allow program="D:\cluster\tools\python\python.exe" protocol=UDP localport=8766 profile=private remoteip=localsubnet
```

The rule names only the cluster's own Python (`tools\python`, used by nothing else on the PC), only port
8765, only networks Windows calls *Private* and only devices on the same local network. When the cluster
isn't running nothing listens there, so it opens nothing; other programs, including any other Python, are
not affected. Set your home Wi-Fi to *Private* (Settings → Network) and leave café or hotel networks
*Public*. The second rule (automatic discovery) is optional. If Windows asks anyway: *Cancel* makes it
block phones (delete the block rules it adds for this folder's `tools\python\python.exe` in *Windows Defender
Firewall → Advanced settings → Inbound Rules*). To undo everything: `netsh advfirewall firewall delete rule
name="PhoneCluster"` (and `"PhoneCluster discovery"`).

A joined phone follows the PC if its IP changes later (LAN broadcast), and trusts only the cluster's own
certificate, so a fake answer leads nowhere. `scripts\pc.ps1 discover` shows what answers.

**Who can get in.** Phones join only with a **one-time code** from the PC's Join page (works once, for 10
minutes); the PIN never leaves the PC. All traffic to phones is encrypted (HTTPS) with certificates the
cluster makes for itself in `data\tls` (nothing is installed in Windows). Wrong codes, PINs or device keys
lock the guesser out for a while. Requests that name the PC by a strange host name are refused.

## Adding a phone

| phone | how |
|---|---|
| Android 7+ (Redmi 5A, Redmi Note 10) | Install Termux from F-Droid. On the PC's Join page scan the *Termux phone* QR, tap Copy, paste into Termux (a one-time code is inside). Optional `pkg install termux-api ffmpeg`. |
| Android 5/6 (Samsung J2), iPhone, anything with Chrome | Scan the QR on the PC's Join page. The browser warns once that the connection is "not private" (the cluster's own certificate): continue. The tab is the worker. |
| Windows laptop or PC | Paste the Join page's *Windows PC or laptop* command into PowerShell or Command Prompt on it. Everything goes into `phonecluster` in the user's folder, Python included (copied from this PC); no admin rights. `start-worker.cmd` there starts it again later. |
| Linux or Mac computer | Paste the *Termux / Linux / Mac* one-liner into a terminal (needs `python3` 3.8+). |

Details, battery-optimization settings and per-phone notes: [docs/PHONE-SETUP.md](docs/PHONE-SETUP.md).

**Removing a phone:** close Termux or the browser tab. It shows offline within 30 s and its tasks are reassigned.
Click **Remove** on the Devices page to forget it.

## What it can run

| job type | what | runs on |
|---|---|---|
| `python_map` | your Python function over a list of items, or over every line of a big file, chunked across devices | Termux + PC, and browsers once `get-pyodide.ps1` has run |
| `shell` | a shell command on one device or every device | Termux + PC |
| `media` | ffmpeg resize / thumbnail / transcode / extract audio, videos split into segments and joined back | devices with ffmpeg |
| `hash_search`, `prime_range`, `monte_carlo_pi` | number crunching demos | everything, including browsers |
| `sensor` | camera photo, mic recording, GPS, battery, sensors via Termux:API | phones with Termux:API |
| `benchmark` | scores a device; faster devices get tasks first | everything |
| `custom` | raw task payloads for your own plugins | whatever the payload asks for |
| `service` | a model that stays loaded and answers requests from other devices | Termux + PC |
| `wasm` | one compiled WebAssembly program, run as a function over a range or as a program per input file | PC, Termux phones and browsers, the same `.wasm` file |

Add your own type by dropping a module into `data/plugins/` (see [docs/SPEC.md](docs/SPEC.md) section 7).

**Job packages:** put a `job.yaml` next to your code and data files and submit the folder in one go
with `scripts\pc.ps1 run <folder>`; file references like `code: "@main.py"` or `inputs: "@photos/*.jpg"`
are inlined or uploaded for you. See [docs/PACKAGES.md](docs/PACKAGES.md) and `examples/packages/`.

Also: pipelines (job B waits for job A and can read its result), cron schedules, job templates,
result export as JSON / CSV / zip, live task logs, a line-mode remote shell into any Termux or PC worker,
pooled storage that spreads files across phones, and notifications (Windows toast, phone notification,
webhook).

**One program runs everywhere.** Compile once to WebAssembly (Rust, C, Zig, Go, ...) and the same `.wasm`
file runs on the PC (wasmtime), on Termux phones (wasmtime, installed by the worker the first time) and in a
browser tab like the Samsung J2's, sandboxed: no network, only its own `/in` and `/out` folders.
`scripts\pc.ps1 wasm examples\wasm\primes.wasm --call count_primes --range 2 100000000 --wait`.
See [docs/WASM.md](docs/WASM.md).

**Never compute the same thing twice.** A job whose code and inputs match an earlier one (maps, number
crunching, media, WebAssembly) is answered from the stored result at once, and a job where only some inputs
changed re-runs only those tasks, so a pipeline run again recomputes only what changed. Shell commands,
sensors and services are never reused; a job can say `cache: false`, and the job page has **Run again fresh**.
`scripts\pc.ps1 cache` shows what is stored.

**Wrong answers from old hardware get caught.** About 5 % of the tasks of exact job types run a second time
on another device. If the answers differ, a third device decides; the device that was wrong loses trust, its
recent work is computed again (finished jobs included) and you get a notification. A device that is wrong
three times in a day gets no more jobs until you press **Trust again**. `scripts\pc.ps1 trust`.

**Phones pass big inputs to each other.** When several phones need the same big file, a phone that already
has it sends it to the next one while the PC sends other pieces, instead of the PC's Wi-Fi carrying every
copy. A phone opens a port only while sending, only for a one-time token, and every piece is checked against
the PC's own copy.

**Storage that survives a lost phone at 1.3x the space.** With three or more storage phones, files are
stored as data pieces plus a parity piece on different phones (erasure coding) instead of two full copies:
losing any one phone loses nothing. Every stored piece is re-checked on its phone once a day; a damaged one
is rebuilt from the others.

**You decide what each device does, and the cluster keeps it safe.** Every connected device has a
master switch, role switches (jobs / AI models / storage) and limits for its CPU, memory and, on the PC,
graphics card. The *Balanced* safety profile rests a phone at 40 °C, moves its work off at 43 °C, starts
models only on a charging phone or one above 50 % battery, leaves one core idle and never gives a model
more than 70 % of the free memory. `scripts\pc.ps1 device-set redmi5a --ai off --cpu 50`.

**Jobs finish at least as fast as on the fastest device alone.** You don't pick chunk sizes: the
cluster cuts the work into pieces as it goes, sized to each device's measured speed so they all finish
together, and remembers every device's speed per kind of work for next time (`scripts\pc.ps1 speeds`).
A slow phone is skipped when the faster devices would finish before it; a device that runs out of work
runs a backup copy of a slower device's task and the first result wins. Chats and other service
requests come first: a device's background tasks pause while it answers. Each job reports how it did
against its fastest device alone (`scripts\pc.ps1 job <id>`, the job page).

**Host a model on one phone, use it from all of them.** A `service` job keeps a model loaded on a
capable device (Vosk speech-to-text, image classification, translation, TTS, ...) and any other device
calls it: the input streams through the coordinator to the model, partial results come back live, and the
weakest phone gets the answer without doing the work. Start one with `scripts\pc.ps1 run
examples\packages\services\echo-demo`, call it with `scripts\pc.ps1 call echo-demo file.bin --follow`, or
use the Services page. See [docs/SERVICES.md](docs/SERVICES.md).

**Run AI models, even ones too big for any single device.** Pick a GGUF model: if it fits on one
device it runs there alone on the fastest one (the PC's graphics card first); if it is too big it is
split over the fewest fast devices, each holding part of the layers. Or choose the devices yourself.
Every device can use the result, and a model moves to other devices when one gets too hot or is switched
off. `scripts\get-llama.ps1 -Vulkan` and `scripts\get-model.ps1` fetch llama.cpp and a small test model;
then just call it: `scripts\pc.ps1 llm ask chat "Hello"` loads the model on the best device the first
time, it unloads after 10 unused minutes, and a busy model gets a second copy. See [docs/LLM.md](docs/LLM.md).

**Nothing is lost when the PC goes away.** A phone that finishes a task while the coordinator is
unreachable keeps the result and its output files in an outbox and delivers them on reconnect; the
coordinator accepts the late result even if it had already handed the task to someone else.

**Long tasks resume instead of restarting.** `ctx.checkpoint(state)` saves progress; if the device
dies the next attempt, on any device, gets it back through `ctx.resume()`. Map jobs checkpoint after
every item automatically, so a chunk half done on one phone finishes on another.

**Big files stream.** `c.map_lines(fn, "huge.txt")` or `pc map fn.py:fn huge.txt --lines` gives each
task its own byte range of the file, and `stream_results` writes items to a JSONL file as tasks finish.
Read them with `job.iter_results()` or `pc results <job>` without loading everything into memory.

## Python SDK and CLI

```python
import sys; sys.path.insert(0, r"D:\cluster\sdk")   # this folder's sdk\ (tools\python finds it by itself)
from phonecluster import Cluster

c = Cluster(pin="123456")   # on the PC: http://127.0.0.1:8764 (the dashboard API opens only there)

def square(item, ctx):
    ctx.log("squaring %s" % item)
    return item * item

job = c.map(square, list(range(1000)), chunk_size=50)
print(job.wait().result()[:5])            # [0, 1, 4, 9, 16]
print(c.shell("uname -a", targets="all").wait().result())
```

```powershell
scripts\pc.ps1 login                      # stores URL + PIN in data\cli.json
scripts\pc.ps1 devices
scripts\pc.ps1 submit prime_range --param end=5000000 --wait
scripts\pc.ps1 shell "df -h" --all --wait
scripts\pc.ps1 submit media --param op=thumbnail --file inputs=C:\clip.mp4 --wait
scripts\pc.ps1 run examples\packages\wordcount --wait
scripts\pc.ps1 export <job-id> --format zip -o results.zip
scripts\pc.ps1 terminal <device-id>
```

More examples in `examples/`, full reference in [docs/SDK.md](docs/SDK.md).

## Layout

```
coordinator/   FastAPI app: API, scheduler, storage, plugins, dashboard hosting
worker/        worker.py (Termux/PC, single file, stdlib only) and browser/ (Chrome worker page)
dashboard/     vanilla-JS dashboard, served at /
sdk/           phonecluster Python package (SDK + CLI), stdlib only
tests/         625 tests incl. end-to-end runs with real worker processes, wasmtime and real llama.cpp
data/          runtime state: config.yaml (PIN), cluster.db, files/, logs/  (created on first start)
tools/ffmpeg/  ffmpeg.exe + ffprobe.exe (downloaded by scripts\get-ffmpeg.ps1)
tools/pyodide/ CPython for browsers (downloaded by scripts\get-pyodide.ps1)
tools/llama.cpp/ pinned llama.cpp builds for distributed models (scripts\get-llama.ps1)
tools/wasmtime/ WebAssembly runtime for the PC worker (scripts\get-wasmtime.ps1)
tools/scrcpy/  scrcpy + its adb: a phone's screen in a window on the PC (scripts\get-scrcpy.ps1)
data/models/   GGUF model files
tools/python/  the cluster's own Python 3.12 + its libraries (scripts\get-python.ps1; a plain zip, nothing
               installed in Windows; isolated: ignores other Pythons, PYTHON* variables, user packages)
.venv/         older project venv (used only if tools/python is missing; safe to delete)
```

Run the tests with `scripts\test.ps1`. The design contract is [docs/SPEC.md](docs/SPEC.md);
deliberate departures from it are listed in [docs/DEVIATIONS.md](docs/DEVIATIONS.md).

`data/`, `tools/` and `.venv/` are not part of the repository: the scripts above download or create
them inside the folder.

## License

MIT, see [LICENSE](LICENSE).
