"""Reed-Solomon erasure coding over GF(256), pure Python (SPEC §10).

``k`` data pieces get ``m`` parity pieces; any ``k`` of the ``k + m`` rebuild
everything.  Storing a file as 3 data + 1 parity pieces on four phones takes
1.33x its size and survives losing any one phone -- the same safety as two
full copies (2x).

The code is *systematic* (data pieces are stored unchanged, so reading needs
no decoding while all phones are up) and built from a Cauchy matrix scaled so
that the first parity piece is the plain XOR of the data: the common case
``m = 1`` costs one XOR pass.  Multiplying a whole piece by a constant uses
``bytes.translate`` with a 256-byte table and adding pieces is one big-integer
XOR, so everything runs at C speed without numpy.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence

POLY = 0x11D  # x^8 + x^4 + x^3 + x^2 + 1

EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= POLY
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]

_TABLES: Dict[int, bytes] = {}


def mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def inv(a: int) -> int:
    if a == 0:
        raise ZeroDivisionError("0 has no inverse in GF(256)")
    return EXP[255 - LOG[a]]


def table(c: int) -> bytes:
    """Translation table that multiplies every byte by ``c``."""
    found = _TABLES.get(c)
    if found is None:
        found = bytes(mul(c, x) for x in range(256))
        _TABLES[c] = found
    return found


def parity_matrix(k: int, m: int) -> List[List[int]]:
    """``m x k`` coefficients; row 0 is all ones (XOR parity)."""
    if k < 1 or m < 0 or k + m > 256:
        raise ValueError("need 1 <= k and k + m <= 256")
    rows = []
    for i in range(m):
        # Cauchy entry 1/(x_i + y_j) with x_i = i, y_j = m + j, then column j
        # scaled by y_j (= 1/C[0][j]) so that row 0 becomes all ones
        rows.append([mul(inv(i ^ (m + j)), m + j) for j in range(k)])
    return rows


def _combine(pieces: Sequence[bytes], coefs: Sequence[int], length: int) -> bytes:
    acc = 0
    for piece, coef in zip(pieces, coefs):
        if coef == 0 or not piece:
            continue
        if len(piece) < length:
            piece = piece + bytes(length - len(piece))
        scaled = piece if coef == 1 else piece.translate(table(coef))
        acc ^= int.from_bytes(scaled, "little")
    return acc.to_bytes(length, "little")


def encode(data: Sequence[bytes], m: int) -> List[bytes]:
    """Parity pieces for ``data`` (shorter pieces count as zero-padded)."""
    k = len(data)
    length = max((len(d) for d in data), default=0)
    return [_combine(data, row, length) for row in parity_matrix(k, m)]


def _invert(matrix: List[List[int]]) -> List[List[int]]:
    n = len(matrix)
    work = [list(row) + [1 if i == j else 0 for j in range(n)] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = next((r for r in range(col, n) if work[r][col]), None)
        if pivot is None:
            raise ValueError("pieces do not determine the data")
        work[col], work[pivot] = work[pivot], work[col]
        scale = inv(work[col][col])
        work[col] = [mul(v, scale) for v in work[col]]
        for r in range(n):
            if r != col and work[r][col]:
                factor = work[r][col]
                work[r] = [a ^ mul(factor, b) for a, b in zip(work[r], work[col])]
    return [row[n:] for row in work]


def reconstruct(shards: Dict[int, bytes], k: int, m: int, lengths: Sequence[int],
                want: Iterable[int]) -> Dict[int, bytes]:
    """Rebuild pieces from any ``k`` known ones.

    ``shards`` maps index -> bytes: ``0..k-1`` are data, ``k..k+m-1`` parity.
    ``lengths`` are the real sizes of the ``k`` data pieces (0 for a padding
    position of a short last stripe); parity pieces are as long as the longest
    data piece.  Returns ``{index: bytes}`` for every index in ``want``.
    """
    want = list(want)
    length = max(lengths) if lengths else 0
    known = dict(shards)
    for j in range(k):  # padding positions are known zeros
        if j < len(lengths) and lengths[j] == 0:
            known.setdefault(j, b"")
    if all(i in known for i in want):
        return {i: known[i] for i in want}
    if len(known) < k:
        raise ValueError("need %d pieces, have %d" % (k, len(known)))
    parity = parity_matrix(k, m)
    chosen = sorted(known, key=lambda i: (i >= k, i))[:k]
    matrix = [([1 if c == i else 0 for c in range(k)] if i < k else parity[i - k]) for i in chosen]
    inverse = _invert(matrix)
    pieces = [known[i] for i in chosen]
    data: Dict[int, bytes] = {}
    for j in range(k):
        if j in known:
            data[j] = known[j]
        else:
            data[j] = _combine(pieces, inverse[j], length)[: (lengths[j] if j < len(lengths) else length)]
    out: Dict[int, bytes] = {}
    for i in want:
        if i < k:
            out[i] = data[i]
        else:
            out[i] = _combine([data[j] for j in range(k)], parity[i - k], length)
    return out


def shape(devices: int) -> Dict[int, int]:
    """Data / parity pieces per stripe for ``devices`` storage devices.

    One parity piece (survives losing any one device, like 2 copies) up to
    five devices, two from six on (survives two); at most 6 data pieces.
    Fewer than three devices: no erasure coding (``{}``).
    """
    if devices < 3:
        return {}
    m = 1 if devices < 6 else 2
    return {"k": min(devices - m, 6), "m": m}
