"""tts-beep: text in, audio out, with the standard library only.

Shows how a service returns a binary file: build the bytes, hand them to
`ctx.output_bytes(data, "name.wav")`, and the caller gets a downloadable
output (the dashboard shows an audio player for .wav).
"""

import io
import math
import struct
import wave


def load(ctx):
    rate = int(ctx.params.get("sample_rate") or 16000)
    ctx.log("tts-beep ready, %d Hz" % rate)
    return {"rate": rate, "tone_ms": int(ctx.params.get("tone_ms") or 90)}


def _tone(rate, freq, ms, volume=0.4):
    n = int(rate * ms / 1000.0)
    frames = bytearray()
    for i in range(n):
        # short fade in/out so the beeps do not click
        env = min(1.0, i / (rate * 0.005), (n - i) / (rate * 0.005))
        sample = int(32767 * volume * env * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", sample)
    return bytes(frames)


def serve(request, ctx, model):
    text = request.text().strip()
    if not text:
        raise ValueError("send some text")
    rate = model["rate"]
    tone_ms = int(request.params.get("tone_ms") or model["tone_ms"])
    silence = b"\x00\x00" * int(rate * 0.03)
    audio = bytearray()
    for index, ch in enumerate(text):
        if ch.isspace():
            audio += b"\x00\x00" * int(rate * 0.12)
            continue
        # map the character to a pitch between 300 and 1100 Hz
        freq = 300 + (ord(ch.lower()) % 32) * 25
        audio += _tone(rate, freq, tone_ms) + silence
        if index % 10 == 9:
            ctx.emit({"chars": index + 1, "of": len(text)})
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(audio))
    ctx.output_bytes(buf.getvalue(), "speech.wav")
    return {"chars": len(text), "seconds": round(len(audio) / 2.0 / rate, 2), "sample_rate": rate}
