from __future__ import annotations

import io

from PIL import Image

from custom_components.ha_doorbell_jeeves.frame_processor import process_frame


def _make_jpeg(width: int, height: int, color: tuple[int, int, int] = (50, 120, 200)) -> bytes:
    image = Image.new("RGB", (width, height), color=color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def test_process_frame_resizes_and_outputs_jpeg() -> None:
    raw = _make_jpeg(1920, 1080)
    processed = process_frame(raw, max_width=640, max_height=480, quality=75)
    assert processed != raw

    out = Image.open(io.BytesIO(processed))
    assert out.width <= 640
    assert out.height <= 480
    assert out.format == "JPEG"


def test_process_frame_accepts_string_quality() -> None:
    raw = _make_jpeg(800, 600)
    processed = process_frame(raw, max_width=400, max_height=300, quality="95")
    out = Image.open(io.BytesIO(processed))
    assert out.width <= 400
    assert out.height <= 300


def test_process_frame_returns_original_on_invalid_input() -> None:
    raw = b"this-is-not-a-jpeg"
    processed = process_frame(raw)
    assert processed == raw

