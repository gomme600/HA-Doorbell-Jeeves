"""Frame processing – downscaling and optimization for faster model inference."""

from __future__ import annotations

import io
import logging
from typing import Any

from PIL import Image

_LOGGER = logging.getLogger(__name__)


def process_frame(
    raw_jpeg: bytes,
    max_width: int = 640,
    max_height: int = 480,
    quality: int = 70,
) -> bytes:
    """Downscale and re-encode a camera frame for optimal model ingestion.

    This reduces:
      - Upload bandwidth to the model API
      - Token usage for vision processing
      - Latency for model responses

    Args:
        raw_jpeg: Original JPEG bytes from the camera.
        max_width: Maximum output width in pixels.
        max_height: Maximum output height in pixels.
        quality: JPEG compression quality (1-100, lower = smaller + faster).

    Returns:
        Optimized JPEG bytes, or the original if processing fails.
    """
    try:
        img = Image.open(io.BytesIO(raw_jpeg))
        original_size = img.size

        # Downscale maintaining aspect ratio
        img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)

        # Re-encode with target quality
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=quality, optimize=True)
        result = output.getvalue()

        if _LOGGER.isEnabledFor(logging.DEBUG):
            reduction = (1 - len(result) / len(raw_jpeg)) * 100
            _LOGGER.debug(
                "Frame: %dx%d → %dx%d (%.0f%% size reduction, %d→%d bytes)",
                original_size[0], original_size[1],
                img.size[0], img.size[1],
                reduction, len(raw_jpeg), len(result),
            )

        return result

    except Exception:
        _LOGGER.warning("Frame processing failed, using original", exc_info=True)
        return raw_jpeg
