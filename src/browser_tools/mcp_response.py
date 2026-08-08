"""Canonical construction and reading of MCP tool-call response envelopes.

Every tool response in browser-tools is an MCP content envelope:

    {"content": [{"type": "text", "text": "..."}], "isError"?: bool}

Two transports carry it, and each wraps the envelope differently:

- **Daemon socket** (``DaemonClient`` <-> ``mcp_daemon``): newline-delimited
  JSON-RPC frames ``{"jsonrpc": "2.0", "result": <envelope>, "id": n}``. The
  ``id`` matches the request; the client reads until it sees its id.
- **Wrapper return** (``browser_tools_session`` -> tool-proxy): the bare
  envelope wrapped once as ``{"result": <envelope>}``, with no JSON-RPC
  framing because it is a direct return value, not a socket message.

Before this module existed the envelope was hand-built at ~20 sites, which
let ``isError`` drift and the error-text prefix convention diverge. Build
and read responses only through the helpers here.
"""

from __future__ import annotations

from typing import Any


def _text_item(text: str) -> dict[str, str]:
    """Build a single MCP text content item."""
    return {"type": "text", "text": text}


# ---------------------------------------------------------------------------
# Bare envelope (wrapper -> tool-proxy return path)
# ---------------------------------------------------------------------------


def text_response(text: str) -> dict[str, Any]:
    """Build a bare success response: ``{"result": {"content": [text]}}``."""
    return {"result": {"content": [_text_item(text)]}}


def error_response(message: str) -> dict[str, Any]:
    """Build a bare error response with ``isError`` set.

    ``message`` is used verbatim as the text content; callers own the wording
    (including any leading ``Error:``). This keeps the wrapper return path
    behavior-preserving for messages that already carry their own prefix
    (e.g. the E001 dead-port diagnostic). For the daemon-socket convention of
    auto-prefixing a clean message, use :func:`make_error`.
    """
    return {"result": {"content": [_text_item(message)], "isError": True}}


# ---------------------------------------------------------------------------
# JSON-RPC framed envelope (daemon socket path)
# ---------------------------------------------------------------------------


def make_text(text: str) -> dict[str, Any]:
    """Build a JSON-RPC framed success response for the daemon socket.

    The ``id`` is a placeholder (``0``); the daemon overwrites it with the
    matching client request id before sending.
    """
    return {"jsonrpc": "2.0", "result": {"content": [_text_item(text)]}, "id": 0}


def make_error(message: str) -> dict[str, Any]:
    """Build a JSON-RPC framed error response for the daemon socket.

    The ``id`` is a placeholder (``0``); the daemon overwrites it. A clean
    ``message`` is prefixed with ``Error: `` to match the established CDP/
    daemon error-text convention; callers there pass unprefixed messages.
    """
    return {
        "jsonrpc": "2.0",
        "result": {"content": [_text_item(f"Error: {message}")], "isError": True},
        "id": 0,
    }


# ---------------------------------------------------------------------------
# Readers / mutators (shared by both transports)
# ---------------------------------------------------------------------------


def extract_text_items(response: dict[str, Any]) -> list[str]:
    """Return every text string in a response's content array.

    Tolerates all envelope shapes that appear in browser-tools:
    - bare legacy ``{"content": [...]}`` (no result wrapper)
    - wrapper return ``{"result": {"content": [...]}}``
    - daemon JSON-RPC frame ``{"jsonrpc", "result": {"content": [...]}, "id"}``

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


def append_text(response: dict[str, Any], text: str) -> None:
    """Append a text content item to an existing response envelope in place.

    Used by the daemon to attach interstitial / screenshot diagnostics to an
    already-built response without rebuilding it.
    """
    result = response.get("result")
    if not isinstance(result, dict):
        return
    content = result.get("content")
    if isinstance(content, list):
        content.append(_text_item(text))


__all__ = [
    "append_text",
    "error_response",
    "extract_text_items",
    "make_error",
    "make_text",
    "text_response",
]
