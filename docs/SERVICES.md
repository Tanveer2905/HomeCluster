# Services: host a model on one phone, use it from any device

A **service** is a job that never finishes on its own. Its task loads a model
once on the device that runs it and then answers requests: speech-to-text,
image classification, translation, text-to-speech, a small LLM, anything you
can wrap in two Python functions. Any other device, the dashboard, the CLI or
the SDK can call it. The caller's data streams through the coordinator to the
model as it arrives, nothing is written to the hosting phone's storage unless
the service code chooses to, and partial results flow back live.

```
 J2 (browser, no compute)  --upload-->  coordinator  --stream-->  Redmi Note 10 (model loaded)
                            <--result--             <--partials--
```

## Starting a service

Write a package (see `examples/packages/services/`) and run it:

```powershell
scripts\pc.ps1 run examples\packages\services\echo-demo      # no dependencies, try this first
scripts\pc.ps1 run examples\packages\services\tts-beep       # text -> WAV, shows binary outputs
scripts\pc.ps1 run examples\packages\services\vosk-stt       # real speech-to-text (installs vosk on the phone)
scripts\pc.ps1 services                                      # who is hosting what
scripts\pc.ps1 service stop vosk-en
```

The manifest is a normal job of type `service`:

```yaml
name: "service: vosk-en"
type: service
params:
  name: vosk-en                     # the name callers use
  description: offline speech-to-text
  code: "@service.py"               # defines load() and serve()
  setup_cmd: pip install vosk       # optional, runs once on the host before load()
  inputs: ["@model.zip"]            # optional model files -> ctx.inputs
  accepts: audio/wav                # documentation for callers
  produces: text
  streaming_output: true            # the service emits partials
  request_timeout: 600              # per request, then the model is restarted
  params: {sample_rate: 16000}      # static settings -> ctx.params
options:
  targets: ["<device id>"]          # pin to a phone; "all" = one instance per device
```

Or from Python: `c.start_service("vosk-en", "service.py", setup_cmd="pip install vosk", ...)`.
The dashboard's **Services → Start service** opens the same form.

A service occupies one worker slot for as long as it runs. If the host phone
goes offline the coordinator reassigns the task to another capable device (up
to `max_attempts`, default 5); if the model process crashes it is restarted
in place. Cancelling the job stops the service.

## Writing the service code

```python
def load(ctx):
    """Called once per start. Return whatever serve() needs (the model)."""
    from vosk import Model
    return Model(ctx.service_dir + "/model")

def serve(request, ctx, model):          # or serve(request, ctx)
    """Called per request. Return a JSON-serialisable result."""
    for chunk in request.stream():       # bytes as they arrive over the network
        ...
        ctx.emit({"partial": text})      # optional live update to the caller
    return {"text": final_text}
```

`request`:

| member | meaning |
|---|---|
| `stream(chunk_size=None)` | yields the body chunk by chunk, no disk |
| `read()` / `text()` / `json()` | the whole body (buffered in memory) |
| `save(path)` | write the body to a file, if the model insists on one |
| `params` | dict from `?params=` / `--param k=v` / the form |
| `content_type`, `size`, `name` | what the caller sent (`X-File-Name` header / form file name) |
| `id` | request id |

`ctx`:

| member | meaning |
|---|---|
| `log(...)` | task log line (`print()` works too) |
| `emit(value)` | send a partial result now |
| `output(path, name)` / `output_bytes(data, name)` | return files (audio, images); callers download them |
| `service_dir` | persistent folder for this service on this device (model cache) |
| `inputs` / `input(id)` | model files from the job's `inputs` |
| `params` | static params from the job |
| `workdir` | per-request scratch folder, deleted afterwards |
| `device_name`, `service_name`, `task_id`, `request` | context |

Define `unload(model, ctx)` if the model needs cleanup on stop. Requests are
handled one at a time per instance; run the service on several devices
(`targets: "all"`) for parallelism. A request that exceeds `request_timeout`
kills and restarts the model process.

## Calling a service

```powershell
scripts\pc.ps1 call vosk-en recording.wav --follow          # --follow prints partials live
scripts\pc.ps1 call translate-demo --text "good morning" --param to=fr
scripts\pc.ps1 call tts-beep --text "hello" -o out\          # download output files
scripts\pc.ps1 requests                                      # recent requests
```

```python
r = c.call("vosk-en", path="recording.wav", params={"lang": "en"},
           on_partial=lambda p: print(p))
print(r["result"]["text"], r["ms"], "ms on", r["device_id"])
for f in r["outputs"]:
    c.files.download(f["file_id"], f["name"])
```

Dashboard: **Services** page → pick the service, choose a file or type text,
Call. Partials appear as they come; audio and image outputs preview inline.

Browser worker page (a phone that only runs Chrome): the **services on the
cluster** card at the bottom does the same, authenticated with the device's own
token, so the Samsung J2 can use Vosk on the Note 10 without knowing the PIN.

HTTP: `POST /api/services/{name}/call` with the raw body (any content type),
a multipart `file`, or JSON; `?params=<json>`; pick `?request_id=` yourself to
follow `GET /api/requests/{id}/partials?since=N` on a second connection.
Answers: `200` with the request dict (`result`, `partials`, `outputs`, `ms`),
`404` no such service, `503` all instances busy (`?queue_timeout=`) or a model
that cannot load, `502` the service failed, `504` timed out.

## Automatic behaviour

- **More copies when busy.** When callers wait (a request queued for 2 s, or two
  queued), the cluster starts another copy of the service on the fastest free
  device that may run services and does not host it yet, up to 3 copies; an
  extra copy that stays unused for 3 minutes while nobody waits is stopped
  again. Requests are spread over every ready copy. Settings → Automatic.
- **Requests first.** While a device answers a request, its normal tasks pause
  (they are frozen, not cancelled) and it gets no new ones; they resume as soon
  as the answer is sent, or after 2 minutes at most.
- **Language models load on call** and unload after 10 unused minutes: see
  [LLM.md](LLM.md).

## Limits

- The coordinator (PC) must be up; every request crosses the Wi-Fi twice.
- Only Termux and PC workers can host services (`service` capability); browser
  workers can call them but not host them.
- Memory is the real constraint on phones: Vosk small ≈ 300 MB, Pillow ≈ 50 MB,
  small LLMs 1 GB+. Pin services to the phones that can carry them.
- The hosting phone must stay awake and on Wi-Fi; charging is strongly advised.
