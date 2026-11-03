# Features in detail

The README lists these in one line each; here is what each one does and how to use it. Commands run in
the project folder on the main PC.

**Job packages.** Put a `job.yaml` next to your code and data files and submit the folder in one go
with `scripts\pc.ps1 run <folder>`; file references like `code: "@main.py"` or `inputs: "@photos/*.jpg"`
are inlined or uploaded for you. See [PACKAGES.md](PACKAGES.md) and `examples/packages/`.

**Everyday tools.** Pipelines (job B waits for job A and can read its result), cron schedules, job
templates, result export as JSON / CSV / zip, live task logs, a line-mode remote shell into any Termux or
PC worker, pooled storage that spreads files across phones, and notifications (Windows toast, phone
notification, webhook).

**One program runs everywhere.** Compile once to WebAssembly (Rust, C, Zig, Go, ...) and the same `.wasm`
file runs on the PC (wasmtime), on Termux phones (wasmtime, installed by the worker the first time) and in a
browser tab like the Samsung J2's, sandboxed: no network, only its own `/in` and `/out` folders.
`scripts\pc.ps1 wasm examples\wasm\primes.wasm --call count_primes --range 2 100000000 --wait`.
See [WASM.md](WASM.md).

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
use the Services page. See [SERVICES.md](SERVICES.md).

**Run AI models, even ones too big for any single device.** Pick a GGUF model: if it fits on one
device it runs there alone on the fastest one (the PC's graphics card first); if it is too big it is
split over the fewest fast devices, each holding part of the layers. Or choose the devices yourself.
Every device can use the result, and a model moves to other devices when one gets too hot or is switched
off. `scripts\get-llama.ps1 -Vulkan` and `scripts\get-model.ps1` fetch llama.cpp and a small test model;
then just call it: `scripts\pc.ps1 llm ask chat "Hello"` loads the model on the best device the first
time, it unloads after 10 unused minutes, and a busy model gets a second copy. See [LLM.md](LLM.md).

**A phone's screen on the PC.** With scrcpy (`scripts\get-scrcpy.ps1`) the Devices page's **Screen**
button opens an Android phone in a window on the PC, to watch and control it, over USB or Wi-Fi. See
[PHONE-SETUP.md](PHONE-SETUP.md).

**Nothing is lost when the PC goes away.** A phone that finishes a task while the coordinator is
unreachable keeps the result and its output files in an outbox and delivers them on reconnect; the
coordinator accepts the late result even if it had already handed the task to someone else.

**Long tasks resume instead of restarting.** `ctx.checkpoint(state)` saves progress; if the device
dies the next attempt, on any device, gets it back through `ctx.resume()`. Map jobs checkpoint after
every item automatically, so a chunk half done on one phone finishes on another.

**Big files stream.** `c.map_lines(fn, "huge.txt")` or `pc map fn.py:fn huge.txt --lines` gives each
task its own byte range of the file, and `stream_results` writes items to a JSONL file as tasks finish.
Read them with `job.iter_results()` or `pc results <job>` without loading everything into memory.
