"""QR codes for the ``/join`` page (SPEC §11), rendered as inline SVG."""

from __future__ import annotations

import io
import logging
import re

log = logging.getLogger("coordinator.qr")

_PROLOG = re.compile(rb"^\s*<\?xml[^>]*\?>\s*", re.IGNORECASE)


def qr_svg(text: str, box_size: int = 10, border: int = 2, error: str = "M") -> str:
    """Inline ``<svg>`` markup for ``text`` (no XML prolog, safe for innerHTML).

    ``error``: ``L`` keeps a long text (the Termux command) less dense, which a
    phone camera reads more easily off a screen; ``M`` (default) suits links."""
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage

        code = qrcode.QRCode(
            version=None,
            error_correction=getattr(qrcode.constants, "ERROR_CORRECT_%s" % str(error).upper(),
                                     qrcode.constants.ERROR_CORRECT_M),
            box_size=box_size,
            border=border,
        )
        code.add_data(str(text))
        code.make(fit=True)
        image = code.make_image(image_factory=SvgPathImage)
        buffer = io.BytesIO()
        image.save(buffer)
        raw = _PROLOG.sub(b"", buffer.getvalue())
        return raw.decode("utf-8")
    except Exception:  # pragma: no cover - qrcode missing or broken
        log.exception("could not render a QR code")
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"></svg>'
