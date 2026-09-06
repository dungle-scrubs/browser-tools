"""Screenshot heuristics recognize content and tolerate unavailable decoding."""

import base64
import builtins
import io

import pytest
from PIL import Image

from browser_tools.screenshot_utils import extract_screenshot_png_b64, screenshot_looks_blank


@pytest.mark.parametrize("blank", [True, False])
def test_screenshot_variance_distinguishes_content(blank):
    image = Image.new("L", (100, 100), color=255)
    if not blank:
        image.paste(0, (0, 0, 50, 100))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    assert screenshot_looks_blank(base64.b64encode(buffer.getvalue()).decode()) is blank


@pytest.mark.parametrize("payload", ["", "?", "abc", base64.b64encode(b"not a png" * 5).decode()])
def test_invalid_screenshot_is_not_discarded(payload):
    assert not screenshot_looks_blank(payload)


def test_screenshot_falls_back_without_optional_decoder(monkeypatch):
    image = Image.new("L", (1000, 1000), color=255)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    original = builtins.__import__

    def without_pillow(name, *args, **kwargs):
        if name == "PIL":
            raise ImportError("test optional decoder unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_pillow)
    assert screenshot_looks_blank(base64.b64encode(buffer.getvalue()).decode())


@pytest.mark.parametrize(
    "response, expected",
    [
        ({}, None),
        ({"result": {}}, None),
        ({"result": {"content": [None]}}, None),
        ({"result": {"content": [{"type": "image", "data": "encoded"}]}}, "encoded"),
        (
            {
                "result": {
                    "content": [
                        {"type": "text", "text": "image data:image/png;base64,encoded next"}
                    ]
                }
            },
            "encoded",
        ),
    ],
)
def test_legacy_screenshot_envelopes_are_read_consistently(response, expected):
    assert extract_screenshot_png_b64(response) == expected
