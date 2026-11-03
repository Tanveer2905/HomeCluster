"""image-describe: an image-classification-shaped service.

Where a real model goes:
  * load(ctx): open the weights once, e.g.
        import tflite_runtime.interpreter as tfl
        return tfl.Interpreter(model_path=ctx.inputs[0])
  * serve(): preprocess the image, run inference, return labels.
The Pillow-only version below keeps the same interface so callers do not
change when the model does.
"""

import io


def load(ctx):
    from PIL import Image  # noqa: F401  (fail fast if Pillow is missing)

    ctx.log("image-describe ready (Pillow only; no classifier loaded)")
    return {"top_k": int(ctx.params.get("top_k") or 3)}


def _dominant_colours(img, k):
    small = img.convert("RGB").resize((64, 64))
    counts = {}
    for pixel in small.getdata():
        key = tuple(v // 32 * 32 for v in pixel)  # quantise to 8 levels per channel
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:k]
    total = float(64 * 64)
    return [{"rgb": list(rgb), "share": round(n / total, 3)} for rgb, n in ranked]


def serve(request, ctx, model):
    from PIL import Image

    # whole image needed for decoding: request.read() buffers the stream in memory
    img = Image.open(io.BytesIO(request.read()))
    img.load()
    ctx.emit({"stage": "decoded", "size": list(img.size)})
    k = int(request.params.get("top_k") or model["top_k"])
    return {
        "width": img.size[0],
        "height": img.size[1],
        "mode": img.mode,
        "format": img.format,
        "dominant_colours": _dominant_colours(img, k),
        # a real classifier returns e.g. [{"label": "cat", "score": 0.93}, ...]
        "labels": [],
    }
