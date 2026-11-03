# PhoneCluster

Turn the old phones, laptops and PCs in your home into one free compute and storage cluster.
No root, no cloud, no accounts. Everything lives inside this folder, wherever you put it (the examples
below use `D:\cluster`; any folder works).

```
 phones, laptops, other PCs  ──Wi-Fi──▶  coordinator on the main PC (port 8765)  ◀──  dashboard / CLI / Python SDK
 (Termux, Chrome or worker.py)           scheduler · SQLite · file store · ffmpeg
```

## Requirements

- **Main PC**: Windows 10 or 11 (the coordinator's scripts are PowerShell). No admin rights, except for the
  optional firewall rule below. Internet is needed only to download things once (setup, the optional tools,
  Python on a phone); the cluster itself runs on your own network.
- **Devices that join**, all on the same Wi-Fi or local network:
  - Android 7+ with Termux (no root)
  - Android 5+, iPhone or anything else with a modern browser
  - Windows 10 or 11 laptops and PCs
  - Linux with Python 3.8+ (Mac should work the same way but is untested)

## Quick start (main PC)

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
to **Join** to add devices.

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

## Adding a device

| device | how |
|---|---|
| Android 7+ (Redmi 5A, Redmi Note 10) | Install Termux from F-Droid. On the PC's Join page scan the *Termux phone* QR, tap Copy, paste into Termux (a one-time code is inside). Optional `pkg install termux-api ffmpeg`. |
| Android 5/6 (Samsung J2), iPhone, anything with Chrome | Scan the QR on the PC's Join page. The browser warns once that the connection is "not private" (the cluster's own certificate): continue. The tab is the worker. |
| Windows laptop or PC | Paste the Join page's *Windows PC or laptop* command into PowerShell or Command Prompt on it. Everything goes into `phonecluster` in the user's folder, Python included (copied from this PC); no admin rights. `start-worker.cmd` there starts it again later. |
| Linux or Mac computer (Mac untested) | Paste the *Termux / Linux / Mac* one-liner into a terminal (needs `python3` 3.8+). |

Details, battery-optimization settings, per-phone notes and laptops: [docs/PHONE-SETUP.md](docs/PHONE-SETUP.md).

**Removing a device:** close Termux, the browser tab or the worker's window. It shows offline within 30 s and
its tasks are reassigned. Click **Remove** on the Devices page to forget it.

## What it can run

| job type | what | runs on |
|---|---|---|
| `python_map` | your Python function over a list of items, or over every line of a big file, chunked across devices | Termux phones, PCs and laptops, and browsers once `get-pyodide.ps1` has run |
| `shell` | a shell command on one device or every device | Termux phones, PCs and laptops |
| `media` | ffmpeg resize / thumbnail / transcode / extract audio, videos split into segments and joined back | devices with ffmpeg |
| `hash_search`, `prime_range`, `monte_carlo_pi` | number crunching demos | everything, including browsers |
| `sensor` | camera photo, mic recording, GPS, battery, sensors via Termux:API | phones with Termux:API |
| `benchmark` | scores a device; faster devices get tasks first | everything |
| `custom` | raw task payloads for your own plugins | whatever the payload asks for |
| `service` | a model that stays loaded and answers requests from other devices | Termux phones, PCs and laptops |
| `wasm` | one compiled WebAssembly program, run as a function over a range or as a program per input file | PC, Termux phones and browsers, the same `.wasm` file |

Add your own type by dropping a module into `data/plugins/` (see [docs/SPEC.md](docs/SPEC.md) section 7).

## Features

- **One program everywhere**: compile to WebAssembly once and the same `.wasm` runs on the PC, phones and browser tabs.
- **AI models, even too big for one device**: llama.cpp models run on the best device, or split across several.
- **Model services**: keep a model loaded on one device and call it from all the others.
- **As fast as the fastest device, or faster**: work is sized to each device's measured speed, and slow devices are skipped.
- **Never computes the same thing twice**: earlier results are reused, and only changed inputs run again.
- **Catches wrong answers**: about 5 % of tasks are re-checked on another device, and wrong devices lose trust.
- **Safe for old phones**: per-device switches and limits; phones rest when hot, and models start only with enough battery.
- **Storage across phones** that survives losing one, at 1.3x the space (erasure coding, checked daily).
- **Nothing lost**: results wait in an outbox if the PC is away, and long tasks resume on another device.
- **Everyday tools**: job packages, pipelines, cron schedules, exports, live logs, remote shell, a phone's screen on the PC, notifications.

Each one in detail, with the commands: [docs/FEATURES.md](docs/FEATURES.md).

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
