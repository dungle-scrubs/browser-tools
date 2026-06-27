"""Screenshot readiness and blank-frame detection utilities.

Two-layer mitigation for blank/half-rendered screenshots:
  1. Pre-capture rAF gate - wait for requestAnimationFrame + fonts
  2. Post-capture blank check - detect near-uniform PNG frames
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Empirical thresholds. PNG of a near-uniform frame compresses to a tiny
# fraction of raw pixel size; a real screenshot is typically >0.05 bytes/px.
# Stddev threshold is on 0-255 luminance — under ~5 means almost no contrast.
SCREENSHOT_BLANK_BYTES_PER_PIXEL_THRESHOLD = 0.02
SCREENSHOT_BLANK_LUMINANCE_STDDEV_THRESHOLD = 5.0

SCREENSHOT_PAINT_READY_TIMEOUT_MS = 1500
SCREENSHOT_BLANK_RETRY_DELAY_SECONDS = 0.25
SCREENSHOT_BLANK_MAX_RETRIES = 1


def extract_screenshot_png_b64(response: dict[str, Any]) -> str | None:
    """Find the PNG base64 payload in a take_screenshot MCP response.

    chrome-devtools-mcp returns screenshots as either an ``image`` content
    block (``{"type": "image", "data": "<b64>", ...}``) or, for some
    versions, embedded as a ``data:image/...`` URI inside a text block.
    We probe for both shapes and return the raw base64 string, or None
    if no image is found (e.g. the screenshot was saved-to-file only,
    or the response is an error).
    """
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if not isinstance(content, list):
        return None

    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            data = block.get("data")
            if isinstance(data, str) and data:
                return data
        if block.get("type") == "text":
            text = block.get("text", "")
            marker = "data:image/"
            idx = text.find(marker)
            if idx >= 0:
                comma = text.find(",", idx)
                if comma > 0:
                    payload = text[comma + 1 :].strip()
                    end = len(payload)
                    for i, ch in enumerate(payload):
                        if ch in (" ", "\n", "\r", "\t"):
                            end = i
                            break
                    return payload[:end] or None
    return None


def screenshot_looks_blank(png_b64: str) -> bool:
    """Heuristically decide whether a captured PNG is effectively blank.

    Two signals, in order of precision:

    1. Luminance variance (preferred) — decode via Pillow if available,
       downscale to a small grid for speed, compute stddev of grayscale
       pixels. A stddev under SCREENSHOT_BLANK_LUMINANCE_STDDEV_THRESHOLD
       means almost no contrast (solid color, gradient-only loader, etc.).

    2. Compressed-size ratio (fallback) — uniform pixels compress to a
       tiny fraction of raw size. Parsing just the IHDR chunk gives us
       width/height with no decoding cost. If Pillow is not importable
       in the daemon's Python, this is the floor.

    Returns False on any decoding error so we never accidentally drop a
    real screenshot due to a corrupt-looking buffer.
    """
    import base64 as _b64
    import binascii

    try:
        png_bytes = _b64.b64decode(png_b64, validate=False)
    except (binascii.Error, ValueError):
        return False
    if len(png_bytes) < 24 or png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        return False

    # IHDR is always the first chunk after the 8-byte signature; layout:
    # 4 bytes length, 4 bytes "IHDR", 4 bytes width, 4 bytes height, ...
    try:
        width = int.from_bytes(png_bytes[16:20], "big")
        height = int.from_bytes(png_bytes[20:24], "big")
    except (ValueError, IndexError):
        return False
    if width <= 0 or height <= 0:
        return False

    # Try Pillow path for the more precise variance signal.
    try:
        import io as _io

        from PIL import Image  # type: ignore[import-untyped]

        img = Image.open(_io.BytesIO(png_bytes)).convert("L")
        img.thumbnail((96, 96))
        pixels = list(img.getdata())  # type: ignore[reportArgumentType]
        if not pixels:
            return False
        n = len(pixels)
        mean = sum(pixels) / n
        var = sum((p - mean) * (p - mean) for p in pixels) / n
        stddev = var**0.5
        return stddev < SCREENSHOT_BLANK_LUMINANCE_STDDEV_THRESHOLD
    except (OSError, ImportError):
        # Pillow missing or decode failed — fall through to size-ratio.
        logger.debug("Pillow not available for blank detection", exc_info=True)

    ratio = len(png_bytes) / float(width * height)
    return ratio < SCREENSHOT_BLANK_BYTES_PER_PIXEL_THRESHOLD
