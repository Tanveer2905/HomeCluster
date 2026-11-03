"""Minimal GGUF header reader for memory planning (SPEC 6.8).

Reads the key/value metadata and the tensor directory at the start of a
``.gguf`` file (never the tensor data) and derives what the planner needs:
layer count, bytes per layer, the token-embedding and output tensors, KV-cache
bytes per context token and the per-device compute buffer.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Tuple

MAGIC = b"GGUF"
# value types
U8, I8, U16, I16, U32, I32, F32, BOOL, STRING, ARRAY, U64, I64, F64 = range(13)
_SCALARS = {U8: "<B", I8: "<b", U16: "<H", I16: "<h", U32: "<I", I32: "<i",
            F32: "<f", BOOL: "<?", U64: "<Q", I64: "<q", F64: "<d"}

#: llama.cpp's default micro-batch; the compute buffer scales with it
UBATCH = 512
_BLOCK = re.compile(r"^blk\.(\d+)\.")


class GGUFError(ValueError):
    pass


def _read(fh: BinaryIO, fmt: str) -> Any:
    size = struct.calcsize(fmt)
    data = fh.read(size)
    if len(data) != size:
        raise GGUFError("truncated GGUF header")
    return struct.unpack(fmt, data)[0]


def _string(fh: BinaryIO) -> str:
    length = _read(fh, "<Q")
    if length > 1 << 24:
        raise GGUFError("unreasonable string length %d" % length)
    return fh.read(length).decode("utf-8", "replace")


def _value(fh: BinaryIO, kind: int, keep_arrays: bool) -> Any:
    if kind in _SCALARS:
        return _read(fh, _SCALARS[kind])
    if kind == STRING:
        return _string(fh)
    if kind == ARRAY:
        item = _read(fh, "<I")
        count = _read(fh, "<Q")
        if item in _SCALARS and not keep_arrays:
            fh.seek(struct.calcsize(_SCALARS[item]) * count, 1)
            return {"array_of": item, "count": count}
        values = [_value(fh, item, keep_arrays) for _ in range(count)]
        return values if keep_arrays else {"array_of": item, "count": count, "values": values[:64]}
    raise GGUFError("unknown GGUF value type %d" % kind)


def _read_header(fh: BinaryIO) -> Tuple[Dict[str, Any], int]:
    if fh.read(4) != MAGIC:
        raise GGUFError("not a GGUF file")
    version = _read(fh, "<I")
    if version < 2:
        raise GGUFError("GGUF v%d is too old" % version)
    tensor_count = _read(fh, "<Q")
    kv_count = _read(fh, "<Q")
    meta: Dict[str, Any] = {"__version__": version, "__tensors__": tensor_count}
    for _ in range(kv_count):
        key = _string(fh)
        kind = _read(fh, "<I")
        # per-layer arrays (e.g. head_count_kv on some models) are small: keep them
        keep = key.endswith("head_count_kv") or key.endswith("head_count") \
            or key.endswith("feed_forward_length")
        meta[key] = _value(fh, kind, keep)
    return meta, tensor_count


def read_metadata(path: Path) -> Dict[str, Any]:
    """All metadata keys; big arrays are reduced to ``{array_of, count}``."""
    with Path(path).open("rb") as fh:
        return _read_header(fh)[0]


def read_tensor_sizes(path: Path) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Metadata plus ``{tensor name: bytes}``.

    Sizes come from the gaps between tensor data offsets, so every
    quantization type works without a table of block sizes.
    """
    path = Path(path)
    with path.open("rb") as fh:
        meta, count = _read_header(fh)
        if count > 1 << 20:
            raise GGUFError("unreasonable tensor count %d" % count)
        entries: List[Tuple[str, int]] = []
        for _ in range(count):
            name = _string(fh)
            n_dims = _read(fh, "<I")
            if n_dims > 8:
                raise GGUFError("unreasonable tensor rank %d" % n_dims)
            fh.seek(8 * n_dims + 4, 1)  # dims, type
            entries.append((name, _read(fh, "<Q")))
        align = int(meta.get("general.alignment") or 32)
        start = fh.tell()
        data_start = (start + align - 1) // align * align
    end = path.stat().st_size - data_start
    ordered = sorted(entries, key=lambda e: e[1])
    sizes: Dict[str, int] = {}
    for index, (name, offset) in enumerate(ordered):
        following = ordered[index + 1][1] if index + 1 < len(ordered) else end
        sizes[name] = max(0, following - offset)
    return meta, sizes


def _num(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list) and value:
        return float(max(value))
    return None


def summarize(path: Path) -> Dict[str, Any]:
    """Planner facts for one model; ``{"error": ...}`` if the header is unreadable.

    ``layer_bytes``  average weights of one repeating layer
    ``embd_bytes``   token embeddings: always stay on the model's host
    ``output_bytes`` output projection: lands on the last device holding
                     layers (a copy of the embeddings when they are tied)
    ``compute_bytes_base`` / ``compute_bytes_per_ctx``
                     llama.cpp's scratch buffer on every device holding layers
                     (measured: Qwen2.5 0.5B, 45 MB at ctx 1024, 49 MB at 2048)
    """
    try:
        meta, sizes = read_tensor_sizes(path)
    except (OSError, GGUFError, struct.error) as exc:
        try:
            meta, sizes = read_metadata(path), {}
        except (OSError, GGUFError, struct.error):
            return {"error": str(exc)}
    arch = str(meta.get("general.architecture") or "")

    def key(name: str) -> Any:
        return meta.get("%s.%s" % (arch, name))

    n_layer = _num(key("block_count"))
    n_embd = _num(key("embedding_length"))
    n_ff = _num(key("feed_forward_length"))
    n_head = _num(key("attention.head_count"))
    n_head_kv = _num(key("attention.head_count_kv")) or n_head
    head_dim_k = _num(key("attention.key_length")) or (n_embd / n_head if n_embd and n_head else None)
    head_dim_v = _num(key("attention.value_length")) or head_dim_k
    tokens = meta.get("tokenizer.ggml.tokens")
    n_vocab = tokens.get("count") if isinstance(tokens, dict) else (len(tokens) if isinstance(tokens, list) else None)
    n_vocab = n_vocab or _num(key("vocab_size"))
    kv_per_token = None
    if n_layer and n_head_kv and head_dim_k and head_dim_v:
        kv_per_token = int(n_layer * n_head_kv * (head_dim_k + head_dim_v) * 2)  # f16 K and V

    embd = sizes.get("token_embd.weight")
    output = sizes.get("output.weight")
    if output is None and embd:
        output = embd  # tied embeddings: llama.cpp puts a copy next to the last layer
    per_layer: Dict[int, int] = {}
    for name, size in sizes.items():
        match = _BLOCK.match(name)
        if match:
            index = int(match.group(1))
            per_layer[index] = per_layer.get(index, 0) + size
    layer_bytes = int(sum(per_layer.values()) / len(per_layer)) if per_layer else None
    compute_base = compute_ctx = None
    if n_embd:
        # the feed-forward intermediate for one micro-batch dominates
        compute_base = int(UBATCH * (n_ff or 4 * n_embd) * 16)
        compute_ctx = int(n_embd * 4.5)
    return {
        "architecture": arch or None,
        "name": meta.get("general.name"),
        "size_label": meta.get("general.size_label"),
        "layers": int(n_layer) if n_layer else None,
        "embedding": int(n_embd) if n_embd else None,
        "feed_forward": int(n_ff) if n_ff else None,
        "context_length": int(_num(key("context_length")) or 0) or None,
        "vocab": int(n_vocab) if n_vocab else None,
        "kv_bytes_per_token": kv_per_token,
        "layer_bytes": layer_bytes,
        "embd_bytes": embd,
        "output_bytes": output,
        "compute_bytes_base": compute_base,
        "compute_bytes_per_ctx": compute_ctx,
        "file_type": meta.get("general.file_type"),
    }
