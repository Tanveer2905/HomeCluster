# Running AI models on the cluster

PhoneCluster runs GGUF language models with llama.cpp. Placement can be
automatic or chosen by you:

- **A model that fits on one device runs there alone**, on the device that
  answers fastest (the PC's graphics card first), with no network in the way.
- **A model too big for any single device is split** over the fewest, fastest
  devices that hold it together. Each device keeps a share of the layers in
  its memory, and one **host** runs the front end that owns the model file,
  drives the others and answers chats.
- **You decide which devices may take part.** Every device has an "AI models"
  switch, memory and GPU limits, and a master switch, and stays connected
  either way.
- **Devices are protected.** A phone that gets warm, a battery running low or
  a graphics card running hot makes the model move to other devices (or pause)
  before anything is strained.

The running model shows up as a normal service, so any device, a
browser-only phone like the Samsung J2 included, can chat with it.

```
alone:   any device ──▶ PC: llama-server, all 24 layers on the RTX 3050
split:   any device ──▶ PC (host) ──▶ Redmi Note 10: layers 1-10
                                 └─▶ PC CPU: layers 11-16, PC GPU: layers 17-24 + output
```

## One-time setup on the PC

```powershell
scripts\get-llama.ps1 -Vulkan   # llama.cpp b11157: Windows CPU + GPU (Vulkan) builds, Android arm64 build
scripts\get-model.ps1           # a small test model (Qwen2.5 0.5B, 469 MB) into data\models
```

Everything lands in `tools\llama.cpp` inside the project. The GPU build uses
the graphics driver that is already installed; nothing is installed or
configured on the system. Without `-Vulkan` everything works on CPUs only.

Any other GGUF model works: upload it on the **Models** page, paste its
download link there, or `scripts\get-model.ps1 -Url <link to a .gguf>`.
Q4_K_M quantizations are a good size/quality balance.

Every device must run the **same llama.cpp version** (the RPC protocol
changes between versions), so the coordinator pins one release and hands it
to the devices:

| device | what it gets |
|---|---|
| Windows PC | the prebuilt Windows build, and the GPU build when it was downloaded |
| Android 9+ arm64 in Termux (Redmi Note 10) | the prebuilt Android build, ~70 MB, downloaded once |
| Older or 32-bit Android (Redmi 5A on Android 7), Linux | compiles llama.cpp itself on first use: `pkg install git cmake clang` happens automatically, takes 15-40 minutes once and needs internet on that phone once |
| Browser workers (Samsung J2) | cannot hold a model, but can chat with one |

Phone GPUs are not used: llama.cpp's GPU backend does not run usefully on the
Adreno and Mali chips of these phones, so on phones models run on the CPU.

## Running a model

**Dashboard → Models**:

1. Pick the model and a name (the name callers use, e.g. `qwen-small`).
2. Placement **Automatic (recommended)**: untick any device you want to keep
   out. The preview says what will happen, for example *"fits on PC alone (24
   of 24 layers on the RTX 3050) · roughly 260 tokens/s"*, or which devices
   share it and how many layers each part holds.
3. Or **I choose**: tick the devices that hold the model, pick the host, and
   optionally type share weights (`3` and `1` = three times the layers).
   One device that is also the host runs the model alone.
4. **Run model**. Status goes *starting* → *loading* → *ready*.
5. Chat in the **Chat** box, from the Services page, from the J2's worker
   page, or from the CLI and SDK.

**CLI**:

```powershell
scripts\pc.ps1 llm devices                          # memory, GPU, llama.cpp build, eligibility
scripts\pc.ps1 llm plan qwen2.5-0.5b-instruct-q4_k_m.gguf          # where would it run?
scripts\pc.ps1 llm run  qwen2.5-0.5b-instruct-q4_k_m.gguf --name qwen-small --wait
scripts\pc.ps1 llm run  big-model.gguf --name big --on note10 --on redmi5a --on pc   # auto, among these
scripts\pc.ps1 llm run  big-model.gguf --name big --on note10 --on redmi5a --host pc `
    --weight note10=3 --weight redmi5a=1 --wait                       # manual
scripts\pc.ps1 llm chat qwen-small                  # interactive, keeps the conversation
scripts\pc.ps1 llm ask  qwen-small "Summarize what a cluster is in one line."
scripts\pc.ps1 llm list
scripts\pc.ps1 llm stop qwen-small
```

**Python**:

```python
c = Cluster()   # on the PC: the PIN from `pc login` (or Cluster(pin="<your PIN>"))
c.llm.deploy("qwen-small", "qwen2.5-0.5b-instruct-q4_k_m.gguf", wait=True)     # automatic
c.llm.deploy("big", "big-model.gguf", devices=["note10", "redmi5a"], host="pc",
             weights={"note10": 3, "redmi5a": 1}, ctx=2048, wait=True)      # manual
print(c.llm.chat("qwen-small", prompt="Hello!", on_token=lambda t: print(t, end=""))["text"])
c.llm.stop("qwen-small")
```

Devices can be named or given by id everywhere.

## Models load when you call them

You don't have to press **Run model** at all. Any model file can be called by
its name (the file name without `.gguf`): if it is not running, the cluster
loads it on the best device first and then answers. `chat` reaches the default
model (**Use for chat** on the Models page, `scripts\pc.ps1 llm default
<model.gguf>`, or the only model when there is just one).

```powershell
scripts\pc.ps1 llm ask chat "Hello!"            # loads the default model if needed (a few seconds on the GPU)
```

The J2's worker page lists these models too ("loads on the first call").

A model nobody used for **10 minutes** unloads by itself to free memory
(status *idle*); the next message loads it again. Change the time in
**Settings → Automatic** (0 = never), or keep one model always loaded with its
**Keep loaded** button (`scripts\pc.ps1 llm keep <name> on`).

**Busy models get a second copy.** When callers have to wait for a model that
runs alone on one device, the cluster starts another copy on a free device
that can hold it (up to 3 copies) and spreads the requests; the extra copies
stop after 3 quiet minutes. And while a device answers, its other tasks pause,
so the reply is not slowed down by background work.

## How the automatic choice is made

For every device that may take a model right now, the planner knows two
*parts*: its CPU (memory it may use) and, on the PC, its graphics card (the
GPU share you set). It tries every device alone and every combination of
devices, works out whether the layers fit, and estimates the time per word:
layers on each part ÷ that part's speed, plus about 6 ms per device across
the Wi-Fi. The fastest plan that fits wins; with a tie, the one with fewer
devices.

- A device alone has no network cost, so a model that fits on one device
  almost always runs alone.
- Within a device the graphics card is filled first, then its CPU.
- In a split, the fastest part comes last and also holds the output layer.
- The host keeps the model file: a PC in the set, otherwise the fastest phone
  with room for the file.

## How memory is planned

The model's own file says how big each layer is, how big the token
embeddings and the output layer are, and how much context memory each
token needs. Each part needs its layers plus context memory plus a work
buffer (about 50 MB for small models, ~150 MB for 7B ones); the part holding
the output layer also needs the output weights; the host keeps the token
embeddings.

What a device may use for a model:

- **memory**: what it reports free, minus a reserve of 10 % of its RAM (at
  least 256 MB), and never more than its *memory limit* (default and maximum
  70 % of the free memory);
- **graphics card**: its *GPU limit* (default 80 %) of the card's memory,
  within what is free, minus 300 MB for the driver.

Automatic plans also leave one layer of room on each part. A plan that does
not fit is refused unless you choose *Run anyway* (manual placement).

Rough guide for Q4 models: 0.5B ≈ 0.5 GB, 1.5B ≈ 1.2 GB, 3B ≈ 2 GB,
7-8B ≈ 4.5-5 GB, 13-14B ≈ 8-9 GB total, plus context.

## Your control and the device's safety

On **Devices**, each card has:

- **Switch off / Switch on**: off keeps the device connected but uses it for
  nothing; models move off it at once.
- **Use for: Jobs / AI models / Storage**: click to switch each role.
  Turning *AI models* off moves models off that device; jobs keep running.
- **Limits**: CPU share (tasks and model threads; one core always stays idle),
  memory share for models (up to 70 % of free), and on the PC the share of
  the graphics card's memory (0 % keeps the card out).

The same from the CLI: `scripts\pc.ps1 device-set pc --gpu 60`,
`scripts\pc.ps1 device-set redmi5a --ai off`, `scripts\pc.ps1 device-set note10 --off`.

The **Balanced** safety profile always applies:

| condition | what happens |
|---|---|
| battery temperature 40 °C | no new models or tasks on the device until it is back at 36 °C |
| battery temperature 43 °C | models and tasks move off the device now |
| not charging and under 50 % battery | no new model starts there |
| not charging and under 30 % battery | models move off |
| graphics card 80 °C | no new model on the card until 72 °C |
| graphics card 85 °C | its layers move back to the CPUs |
| no temperature reading from the card | the card is not used |

When a device holding part of an **automatic** model goes offline, gets too
hot or is switched off, the model is placed again on the devices that are
left (the event log says *"moved"*). If nothing else can hold it, it waits
(*degraded*) and starts again as soon as it fits somewhere. A **manual**
model waits for its own devices. **Restart** places a model again from
scratch, so an automatic one returns to the graphics card after it cooled
down.

## When a device drops

If a device holding layers goes offline, an automatic model is placed again
on the others; a manual one pauses (*degraded*, "waiting for redmi5a") and
the host stops. When the device returns its share restarts on a new port and
the host reloads automatically. Weights the shards received are cached on
each device (`-c`), so a reload is much faster than the first load. If a
shard fails permanently, the model is marked *failed*; **Restart** starts it
from scratch.

## Speed

Measured on this PC with the 0.5B test model:

- PC graphics card (RTX 3050): about 180 tokens/s in llama-bench, ~100 through the cluster;
- PC CPU: about 80 tokens/s;
- PC plus phones: a few tokens per second, limited by the slowest phone and Wi-Fi latency;
- phones only, 3B+ model: about 0.5-2 tokens per second.

Splitting makes a model *possible*, not faster: every generated token has to
pass through every device holding layers. That is why the automatic choice
keeps a model on one device whenever it fits.

The first load of a big split model also takes a while: the host sends each
device its share of the weights over Wi-Fi (about 1 minute per 300 MB on a
typical home network).

## Network and security

- The host must be able to reach each device holding layers directly on the
  LAN. Routers with *AP isolation* / *client isolation* (common on guest
  networks) block this; use the main network or the PC's hotspot.
- When the **PC holds layers** for a host elsewhere, Windows asks to allow
  `ggml-rpc-server.exe` through the firewall the first time. Allow it on
  private networks, or run once as administrator (your project folder in place
  of `D:\cluster`):

  ```powershell
  netsh advfirewall firewall add rule name="PhoneCluster llama rpc" dir=in action=allow program="D:\cluster\tools\llama.cpp\win-x64\ggml-rpc-server.exe" enable=yes profile=private
  ```

  (and the same for `win-vulkan-x64\ggml-rpc-server.exe` when the PC's GPU holds layers).
- llama.cpp's RPC server has **no password** and has had a published
  remote-code-execution bug. PhoneCluster therefore starts it only while a
  model is split, binds it to the Wi-Fi address on a random port, and kills
  it when the model stops or its device leaves. A model running alone uses no
  RPC server at all. The chat server listens on 127.0.0.1 only. Run split
  models on your own home network, not on public Wi-Fi.
- The GPU build is started with two settings that apply to that one process
  only: which card to use, and switching off AMD's "switchable graphics"
  helper, which makes the Vulkan device list hang on laptops with both an AMD
  and an NVIDIA chip. No driver, registry or system setting is changed.
- Phones must stay awake and in Termux while holding layers; Android kills
  memory-hungry background apps. Charging is strongly advised (and required
  under 50 % battery).

## Files

| where | what |
|---|---|
| `tools\llama.cpp\` (PC) | pinned llama.cpp builds (`win-x64`, `win-vulkan-x64`) and `VERSION` |
| `tools\llama.cpp\rpc-cache\` (PC) | weights cached while the PC holds layers; safe to delete |
| `data\models\` (PC) | GGUF model files |
| `~/.phonecluster/runtime/llama-<tag>/` (phone) | the phone's llama.cpp |
| `~/.phonecluster/runtime/rpc-cache/` (phone) | cached weights, reused on reload; safe to delete |
| `~/.phonecluster/models/` (host device) | the model file, when a phone is the host |
