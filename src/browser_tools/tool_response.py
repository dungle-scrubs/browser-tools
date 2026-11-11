"""Canonical construction and reading of tool response envelopes.

Every tool response in browser-tools carries its content under ``result``:

    {"result": {"content": [{"type": "text", "text": "..."}], "isError"?: bool}}

The handlers in ``cdp_handler`` build their results with :func:`make_text` and
:func:`make_error`; ``curated.py`` reads them back with
:func:`extract_text_items`. Two shapes exist: the unframed one
(:func:`error_response`), and the one :func:`make_text` and :func:`make_error`
build, which carries a JSON-RPC frame around it with a fixed placeholder
``id``.

Build and read responses only through the helpers here, so ``isError`` and
the error-text prefix convention cannot drift apart between call sites.
"""

from __future__ import annotations

from typing import Any


def _text_item(text: str) -> dict[str, str]:
    """Build a single text content item."""
    return {"type": "text", "text": text}


# ---------------------------------------------------------------------------
# Unframed envelope
# ---------------------------------------------------------------------------


def error_response(message: str) -> dict[str, Any]:
    """Build an unframed error response with ``isError`` set.

    ``message`` is used verbatim as the text content; callers own the wording
    (including any leading ``Error:``). This keeps a message that already
    carries its own prefix unchanged. For the convention that auto-prefixes a
    clean message, use :func:`make_error`.
    """
    return {"result": {"content": [_text_item(message)], "isError": True}}


# ---------------------------------------------------------------------------
# Framed envelope
# ---------------------------------------------------------------------------


def make_text(text: str) -> dict[str, Any]:
    """Build a framed success response.

    The ``id`` is a fixed placeholder (``0``); nothing rewrites it.
    """
    return {"jsonrpc": "2.0", "result": {"content": [_text_item(text)]}, "id": 0}


def make_error(message: str) -> dict[str, Any]:
    """Build a framed error response.

    The ``id`` is a fixed placeholder (``0``). A clean ``message`` is prefixed
    with ``Error: `` to match the established CDP error-text convention;
    callers there pass unprefixed messages.
    """
    return {
        "jsonrpc": "2.0",
        "result": {"content": [_text_item(f"Error: {message}")], "isError": True},
        "id": 0,
    }


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def extract_text_items(response: dict[str, Any]) -> list[str]:
    """Return every text string in a response's content array.

    Tolerates every envelope shape that appears in browser-tools:
    - bare ``{"content": [...]}`` (no result wrapper)
    - ``{"result": {"content": [...]}}``
    - framed ``{"jsonrpc", "result": {"content": [...]}, "id"}``

    Returns an empty list when no content array is present, so callers can
    treat "no text" and "missing envelope" identically.
    """
    result = response.get("result")
    if isinstance(result, dict):
        content = result.get("content")
    elif "content" in response:
        content = response["content"]
    else:
        return []
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                texts.append(text)
    return texts


__all__ = [
    "error_response",
    "extract_text_items",
    "make_error",
    "make_text",
]
