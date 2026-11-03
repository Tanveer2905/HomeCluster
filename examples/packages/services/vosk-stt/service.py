"""vosk-en: streaming speech-to-text on a phone.

The audio is fed to Vosk chunk by chunk straight from the network: the file
is never written to the hosting phone. Partial transcripts are emitted while
the upload is still running; the final text is returned at the end.

Input: a WAV file (PCM, mono or stereo, any common rate) or raw 16 kHz mono
PCM16 (`params: {"raw": true}`). Non-16 kHz audio is resampled with the
standard library's ``audioop`` (removed in Python 3.13: on such a phone send
16 kHz audio or install ``audioop-lts``).
"""

import json
import os
import struct
import urllib.request
import zipfile

CHUNK_FRAMES = 4000


def _download_model(ctx):
    """Fetch and unzip the model into the persistent service folder (once)."""
    url = ctx.params.get("model_url")
    dirname = ctx.params.get("model_dirname") or "model"
    target = os.path.join(ctx.service_dir, dirname)
    if os.path.isdir(target):
        return target
    if ctx.inputs:
        archive = ctx.inputs[0]
        ctx.log("unpacking model from job input")
    else:
        archive = os.path.join(ctx.service_dir, "model.zip")
        ctx.log("downloading model from %s (~40 MB, one time)" % url)
        with urllib.request.urlopen(url, timeout=120) as resp, open(archive, "wb") as out:
            while True:
                block = resp.read(1 << 20)
                if not block:
                    break
                out.write(block)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(ctx.service_dir)
    if not os.path.isdir(target):
        # the zip's top folder may be named differently: take the first dir
        dirs = [d for d in os.listdir(ctx.service_dir) if os.path.isdir(os.path.join(ctx.service_dir, d))]
        target = os.path.join(ctx.service_dir, dirs[0])
    return target


def load(ctx):
    from vosk import Model, SetLogLevel

    SetLogLevel(-1)
    path = _download_model(ctx)
    ctx.log("loading Vosk model from %s" % path)
    return {"model": Model(path)}


class _WavStream(object):
    """Turns the incoming byte stream into 16 kHz mono PCM16 frames."""

    def __init__(self, request):
        self.request = request
        self.raw = bool(request.params.get("raw"))
        self.rate = int(request.params.get("sample_rate") or 16000)
        self.channels = int(request.params.get("channels") or 1)
        self.width = 2
        self._header_done = self.raw
        self._buf = b""
        self._ratecv_state = None

    def _parse_header(self, data):
        # minimal RIFF/WAVE parse: find "fmt " and "data" chunks
        if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            self._header_done = True  # not a WAV: treat as raw PCM with the given params
            return data
        pos = 12
        while pos + 8 <= len(data):
            cid = data[pos:pos + 4]
            size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
            if cid == b"fmt ":
                fmt = data[pos + 8:pos + 8 + size]
                self.channels = struct.unpack("<H", fmt[2:4])[0]
                self.rate = struct.unpack("<I", fmt[4:8])[0]
                self.width = struct.unpack("<H", fmt[14:16])[0] // 8
            elif cid == b"data":
                self._header_done = True
                return data[pos + 8:]
            pos += 8 + size + (size % 2)
        return None  # need more bytes for the header

    def frames(self):
        import audioop

        for chunk in self.request.stream():
            self._buf += chunk
            if not self._header_done:
                rest = self._parse_header(self._buf)
                if rest is None:
                    continue
                self._buf = rest
            # keep whole frames only
            frame = self.width * self.channels
            usable = len(self._buf) - (len(self._buf) % frame)
            if usable < frame * CHUNK_FRAMES and not self.request.eof:
                continue
            data, self._buf = self._buf[:usable], self._buf[usable:]
            if self.width != 2:
                data = audioop.lin2lin(data, self.width, 2)
            if self.channels == 2:
                data = audioop.tomono(data, 2, 0.5, 0.5)
            if self.rate != 16000:
                data, self._ratecv_state = audioop.ratecv(data, 2, 1, self.rate, 16000, self._ratecv_state)
            if data:
                yield data
        if self._buf:
            yield self._buf


def serve(request, ctx, model):
    from vosk import KaldiRecognizer

    rec = KaldiRecognizer(model["model"], 16000)
    rec.SetWords(bool(request.params.get("words")))
    last_partial = ""
    total = 0
    for pcm in _WavStream(request).frames():
        total += len(pcm)
        if rec.AcceptWaveform(pcm):
            piece = json.loads(rec.Result())
            if piece.get("text"):
                ctx.emit({"final": piece["text"]})
        else:
            partial = json.loads(rec.PartialResult()).get("partial", "")
            if partial and partial != last_partial:
                last_partial = partial
                ctx.emit({"partial": partial})
    final = json.loads(rec.FinalResult())
    return {"text": final.get("text", ""), "seconds": round(total / 2.0 / 16000, 1),
            "words": final.get("result") if request.params.get("words") else None}
