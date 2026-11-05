# Phone setup notes (no root required)

All phones must be on the same Wi-Fi as the PC (or connected to the PC's mobile hotspot).
The PC's firewall must let phones reach port 8765. If they can't, run once on the PC (as administrator,
before starting the cluster); it opens that port only for the cluster's own Python, only on Private
networks and only to your local network (see the README; put your project folder in place of `D:\cluster`):

```powershell
netsh advfirewall firewall add rule name="PhoneCluster" dir=in action=allow program="D:\cluster\tools\python\python.exe" protocol=TCP localport=8765 profile=private remoteip=localsubnet
```

## Redmi Note 10 (Android 11+)

1. Install **Termux** from F-Droid (the Play Store build is outdated and broken). Open it once.
2. Optional but recommended: install **Termux:API** (F-Droid) and in Termux run `pkg install termux-api`.
   This enables accurate battery/temperature reporting, phone notifications, and sensor jobs (camera, mic, GPS).
3. On the PC's Join page, scan the **Termux phone** QR with the phone's camera or QR scanner and tap
   **Copy** (scanning only copies the text, nothing runs). Then long-press in Termux and **Paste**, Enter.
   The command holds a **one-time code** (works once, for 10 minutes; press *New code* for each phone)
   and checks that it is really talking to your PC before it downloads anything. Don't scan the other
   QR (*Browser phone*) with a Termux phone: it opens the browser worker and uses the code up there. Next time just run `cd ~/phonecluster && python worker.py`: the phone remembers it
   joined, follows the PC to a new IP on its own, and trusts only the cluster's own certificate.
4. Optional: `pkg install ffmpeg` to let this phone run media jobs.
5. Keep the phone awake while it works: in Termux's notification tap **Acquire wakelock**.
   Also disable battery optimization for Termux (Settings → Apps → Termux → Battery → No restrictions),
   otherwise MIUI kills it after a few minutes in the background.

## Redmi 5A (Android 7.1 / 8.1)

Same as above. Termux from F-Droid still supports Android 7. Expect the phone to be slow; the scheduler
gives it fewer tasks automatically because of its benchmark score. Charge it while it works; the coordinator
pauses devices under 20 % battery unless they are charging.

## Samsung J2 (Android 5.1, 1 GB RAM)

Modern Termux does not run on Android 5, so this phone joins through the browser:

1. Open Chrome and scan the QR on the PC's Join page (or open `https://<pc-ip>:8765/w` and type the
   one-time code shown there). Chrome warns once that the connection is "not private": the cluster uses
   its own certificate. Tap *Advanced* → *Continue*.
2. The page registers itself, runs a short benchmark, and holds a screen wake lock while it is visible
   (it falls back to a silent looping video on older Chrome). Set the phone's screen timeout to a long value anyway.
3. Leave the tab in the foreground. Chrome throttles background tabs heavily.
4. **Pause** stops taking new tasks; **Leave** removes the phone and forgets its identity; **Rename** changes the name shown on the dashboard.

The browser worker runs JavaScript jobs (`hash_search`, `monte_carlo_pi`, `prime_range`, `benchmark`,
and any `python_map` job that also provides `js_code`). Once `scripts\get-pyodide.ps1` has been run on
the PC it also runs plain Python jobs: the page shows **python: ready (Pyodide ...)** after a one-time
~13 MB download, and `python_map` jobs run on it with the Python standard library. It still cannot run
shell, media, or sensor jobs, and it has no terminal. Add `&python=0` to the URL to keep Python off on a
phone with too little memory.

It also runs **WebAssembly** jobs ([WASM.md](WASM.md)) with Chrome's own engine, nothing to download: the
same `.wasm` file the PC and the Termux phones run, usually much faster than its JavaScript or Python twin.
Its answers are checked against the other devices like everyone else's. Add `&wasm=0` to the URL to opt out.

## Any other phone

- Android 7 or newer: Termux route.
- Android 5 or 6, iPhone, iPad: browser route.
- Laptops and other PCs: see "Adding a laptop or another PC" below.

## What a phone is used for, and how hard

Every connected phone has a card on the dashboard's **Devices** page:

- **Switch off** keeps it connected but unused (models move off it at once).
- **Use for: Jobs / AI models / Storage** switches each kind of work on or off.
- **Limits** sets the share of its cores and of its free memory the cluster may use. One core always stays
  idle, and models never take more than 70 % of the free memory.

The cluster also protects it (the *Balanced* profile): at a battery temperature of 40 °C it gets no new work
until it cools to 36 °C; at 43 °C its running tasks and models move to other devices; models start only while
it charges or has more than 50 % battery, and leave it when it is unplugged under 30 %; normal tasks stop under
20 % unless it charges. The card shows why a phone is resting.

## Holding part of an AI model

Any Termux phone can hold a model or part of one (see [LLM.md](LLM.md)); nothing extra to install:

- **Redmi Note 10** (Android 11, arm64): downloads the prebuilt llama.cpp from the PC (~70 MB) the first time.
- **Redmi 5A** (Android 7): the prebuilt build needs Android 9, so the phone compiles llama.cpp itself
  the first time (Termux installs `git cmake clang` automatically; 15-40 minutes; needs internet once).
  Keep it on the charger for that.
- **Samsung J2** (browser): cannot hold layers, but can chat with a running model from the services card
  on its worker page.

While a phone holds layers, keep Termux in the foreground with the wakelock on and the phone charging:
Android kills memory-hungry background apps, and the model pauses until the phone is back. The phone must
be reachable from the host on the Wi-Fi; guest networks with "client isolation" block that.

## WebAssembly, storage and passing files on

- **WebAssembly jobs** ([WASM.md](WASM.md)): a Termux phone installs the runtime itself the first time it
  gets one (`pkg install wasmtime`, inside Termux only, needs internet once, about 10 MB). If that fails
  (an old 32-bit Termux, no internet) the phone simply stops offering WebAssembly and those tasks go to other
  devices; nothing else changes.
- **Storage**: with three or more phones offering storage, files are kept as pieces plus a parity piece on
  different phones, so any one phone can be lost, switched off or wiped without losing a file. Each phone
  re-checks its pieces once a day in small batches (a few seconds of reading per minute) and only while it
  is cool and not busy answering a request.
- **Passing big inputs on**: a Termux phone that already has a big input (a video, a data file, a model) may be
  asked to send it to another phone. It opens a port on the Wi-Fi only for that transfer, for a one-time
  password, and closes it again; nothing to set up. On guest networks with "client isolation" this cannot
  work and files simply come from the PC.
- **Wrong answers**: if a phone's answer disagrees with two other devices, the dashboard shows it as
  *checking more*, its recent work is redone elsewhere, and after three wrong answers in a day it gets no more
  jobs until you press **Trust again** on its card. Old phones that overheat or have worn memory are the usual
  cause: let it cool, charge it, then trust it again.

## Seeing and controlling a phone's screen from the PC

The **Screen** button on a phone's card opens its screen in a window on the PC: you can watch it and use it with
the mouse and keyboard. It uses scrcpy (`scripts\get-scrcpy.ps1` puts it in `tools\scrcpy`, with its own adb).
Nothing is installed on the phone and no root is needed, but the phone must allow USB debugging:

1. Turn on Developer options: Settings → About phone → tap *MIUI version* (Samsung: *Build number*) 7 times.
2. In Developer options turn on **USB debugging**. On Xiaomi phones also turn on **USB debugging (Security
   settings)**, otherwise you can see the screen but not control it (MIUI may ask for a SIM card or Mi account).
3. Connect the phone to the PC by USB, tap **Allow** on its *Allow USB debugging?* prompt (tick *Always allow*),
   then press **Screen** on the dashboard.

Without the cable: once, while it is still on USB, run
`.\tools\scrcpy\adb.exe tcpip 5555` in the project folder, then unplug it. The **Screen** button then reaches it over the
Wi-Fi at the address it uses for the cluster, until the phone restarts. While that is on, the phone accepts
debugging connections over the Wi-Fi, though only from PCs it has allowed; restart the phone to switch it off.

Only one adb program can use a USB phone at a time: if another one is running on the PC (Android Studio, a
debloat tool), close it first with its `adb kill-server`. The cluster's adb uses its own port (5039) and stops
with the cluster. adb on Windows always uses your Windows account's adb key (`%USERPROFILE%\.android`, only
read), so a phone that already allowed this PC doesn't ask again.

The J2 works only if Android still runs underneath its Linux and allows USB debugging.

## Adding a laptop or another PC

Open **Join** on the PC's dashboard. The commands work like the Termux one: each checks that it is really talking to
your PC, and the code works once, for 10 minutes.

- **Windows**: copy **Windows PC or laptop** and paste it into PowerShell or Command Prompt on the laptop. Keep the
  window open. Everything goes into one folder, `phonecluster` in the user's folder (`C:\Users\<name>\phonecluster`):
  the worker, its state, and a copy of the PC's own portable Python (from the PC, not the internet). Nothing is
  installed, no admin rights are needed and no setting changes. Later, double-click `start-worker.cmd` in that folder
  to start the worker again.
- **Linux or Mac** (Mac untested): copy **Termux / Linux / Mac one-liner** and paste it into a terminal. It needs
  Python 3.8 or newer (`python3`). Next time: `cd ~/phonecluster && python3 worker.py`.

The laptop connects out to the PC; it needs no firewall change. When it first passes a file to another device,
Windows may ask whether `python.exe` may accept connections: **Allow** (Private networks) lets the other devices fetch
from it directly; **Cancel** is fine too, they then fetch from the PC instead.

## Updates

Workers update themselves. When `worker/worker.py` on the PC changes, every phone, laptop and the PC's own worker
fetches the new copy as soon as it is idle, checks it, keeps the old one as `worker.py.bak` and restarts within a few
seconds; it stays joined. A device that is busy (a task, a model or service, an open terminal) waits until it is idle.
Browser tabs reload themselves when the PC has a newer worker page.

A worker from before this feature can't do that yet: its card on the Devices page says **update by hand once**.
Fetch worker.py over the old one, then start it again; from then on it updates itself:

1. Stop the worker (Ctrl+C in Termux or its terminal).
2. From the Join page's Termux one-liner, copy two things: the `sha256//…` key (it proves the file comes from your
   PC) and the PC's address (the `https://…:8765` part). Put both into this command, replacing `KEY` and `ADDRESS`:

   ```sh
   cd ~/phonecluster && curl -fsSk --max-time 60 --pinnedpubkey "KEY" ADDRESS/worker.py -o worker.py
   ```

3. Start it again: `python worker.py` on Termux, `python3 worker.py` on Linux (the J2). It keeps its saved place in
   the cluster.

## Removing a phone

Close Termux (or press Ctrl+C in it) or close the browser tab. The dashboard shows it offline within 30 seconds and
reassigns its tasks. To forget it completely click **Remove** on the dashboard.

A laptop: close its worker window, click **Remove**, and delete its `phonecluster` folder (on Linux or Mac also
`~/.phonecluster`).
