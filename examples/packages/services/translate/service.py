"""translate-demo: a translation-shaped service with a toy dictionary.

Real engine (example with Argos Translate, fully offline):

    def load(ctx):
        import argostranslate.package, argostranslate.translate
        # install a language pack shipped as a job input, once:
        # argostranslate.package.install_from_path(ctx.inputs[0])
        return argostranslate.translate

    def serve(request, ctx, engine):
        body = _payload(request)
        return {"text": engine.translate(body["text"], body["from"], body["to"])}
"""

import json

DICTIONARY = {
    "es": {"good": "buenos", "morning": "dias", "friend": "amigo", "hello": "hola",
           "thanks": "gracias", "phone": "telefono", "cluster": "grupo"},
    "fr": {"good": "bon", "morning": "matin", "friend": "ami", "hello": "bonjour",
           "thanks": "merci", "phone": "telephone", "cluster": "grappe"},
}


def load(ctx):
    ctx.log("translate-demo ready (languages: %s)" % ", ".join(sorted(DICTIONARY)))
    return DICTIONARY


def _payload(request):
    """Accept plain text or JSON {"text", "to", "from"}; params override."""
    raw = request.text()
    body = {"text": raw, "to": None, "from": "en"}
    if request.content_type.startswith("application/json"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                body.update({k: data[k] for k in ("text", "to", "from") if k in data})
        except ValueError:
            pass
    body["to"] = request.params.get("to") or body["to"]
    body["from"] = request.params.get("from") or body["from"]
    return body


def serve(request, ctx, model):
    body = _payload(request)
    target = (body["to"] or ctx.params.get("default_to") or "es").lower()
    table = model.get(target)
    if table is None:
        raise ValueError("unknown target language %r (have %s)" % (target, ", ".join(sorted(model))))
    words = body["text"].split()
    out = []
    for i, word in enumerate(words):
        out.append(table.get(word.lower().strip(".,!?"), word))
        if i % 20 == 19:
            ctx.emit({"progress": (i + 1) / float(len(words))})
    return {"text": " ".join(out), "from": body["from"], "to": target, "words": len(words)}
