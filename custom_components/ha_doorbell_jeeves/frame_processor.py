"""Frame processing – downscaling and optimization for faster model inference."""

from __future__ import annotations

import io
import logging

from PIL import Image

_LOGGER = logging.getLogger(__name__)


def process_frame(
    raw_jpeg: bytes,
    max_width: int = 640,
    max_height: int = 480,
    quality: int = 70,
) -> bytes:
    """Downscale and re-encode a camera frame for optimal model ingestion.

    Args:
        raw_jpeg: Original JPEG bytes from the camera.
        max_width: Maximum output width in pixels.
        max_height: Maximum output height in pixels.
        quality: JPEG compression quality (1-100).

    Returns:
        Optimized JPEG bytes, or the original if processing fails.
    """
    try:
        # Ensure quality is a valid integer (config values may arrive as strings)
        quality = max(1, min(100, int(quality)))

        img = Image.open(io.BytesIO(raw_jpeg))

        # Convert to RGB if necessary (e.g., RGBA or palette mode can't save as JPEG)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)

        output = io.BytesIO()
        img.save(output, format="JPEG", quality=quality, optimize=True)
        return output.getvalue()

    except Exception:
        _LOGGER.warning("Frame processing failed, using original", exc_info=True)
        return raw_jpeg
