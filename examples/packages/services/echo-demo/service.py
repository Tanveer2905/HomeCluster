"""echo-demo: the smallest possible service.

Every service defines two functions:

    load(ctx)                 -> called once when the service starts; return the model
    serve(request, ctx[, model]) -> called per request; return a JSON-serialisable result

`request.stream()` yields the caller's bytes as they arrive over the network,
so a model can start working before the upload has finished and nothing is
written to the phone's storage. `ctx.emit(...)` sends a partial result to the
caller immediately.
"""

import hashlib
import time


def load(ctx):
    ctx.log("echo-demo ready on %s (service_dir=%s)" % (ctx.device_name, ctx.service_dir))
    return {"started": time.time()}


def serve(request, ctx, model):
    digest = hashlib.sha256()
    total = 0
    chunks = 0
    started = time.time()
    for chunk in request.stream():
        digest.update(chunk)
        total += len(chunk)
        chunks += 1
        ctx.emit({"received": total, "chunks": chunks})
    text = None
    if request.content_type.startswith("text/"):
        # request.read() would also work, but we already consumed the stream
        text = "(text of %d bytes)" % total
    result = {
        "bytes": total,
        "chunks": chunks,
        "sha256": digest.hexdigest(),
        "content_type": request.content_type,
        "name": request.name,
        "params": request.params,
        "seconds": round(time.time() - started, 3),
        "served_by": ctx.device_name,
        "uptime": round(time.time() - model["started"], 1),
    }
    if text and request.params.get("upper"):
        result["note"] = text.upper()
    return result
