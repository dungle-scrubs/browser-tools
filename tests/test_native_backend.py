"""The flipped native snapshot/UID backend on the CDP handler (ticket #41).

These prove that ``CDPHandler``'s native path -- the default backend for
``take_snapshot`` / ``click`` / ``fill`` after the RFC-01 Phase 2 flip -- drives
the #39 reader and #40 interactor over a CDP ``send`` and returns the same bare
MCP envelope shape the Node engine returned. No browser, no daemon: a fake async
``send`` serves recorded CDP results, exactly as the native modules' own unit
tests do.
"""

from __future__ import annotations

from typing import Any

import pytest

from browser_tools.cdp_handler import CDPHandler
from browser_tools.mcp_response import extract_text_items


def _ax(node_id: str, role: str, name: str = "", *, children=None, backend=None) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "nodeId": node_id,
        "role": {"type": "role", "value": role},
        "name": {"type": "computedString", "value": name},
        "childIds": children or [],
        "ignored": False,
    }
    if backend is not None:
        raw["backendDOMNodeId"] = backend
    return raw


_FORM_TREE = {
    "nodes": [
        _ax("1", "RootWebArea", "Form", children=["2"], backend=1),
        _ax("2", "button", "Submit", backend=50),
    ]
}


class _FakeCdpClient:
    """An async CDP ``send`` serving recorded results for the native path."""

    def __init__(self) -> None:
        self.methods: list[str] = []

    async def send(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        self.methods.append(method)
        if method == "Accessibility.getFullAXTree":
            return _FORM_TREE
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "TOP"}}}
        if method == "DOM.getBoxModel":
            return {"model": {"content": [10, 10, 20, 10, 20, 20, 10, 20]}}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj-1"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": "typed-value"}}
        return {}


def _handler_with_fake_client() -> tuple[CDPHandler, _FakeCdpClient]:
    handler = CDPHandler(browser_url=None)
    fake = _FakeCdpClient()
    # Shadow the CDP-or-error accessor so the native path drives the fake client.
    handler._cdp_or_error = lambda: (fake, None)  # type: ignore[method-assign]
    return handler, fake


def _bare_envelope(resp: dict[str, Any]) -> None:
    assert "result" in resp
    content = resp["result"]["content"]
    assert isinstance(content, list) and content
    assert all(item["type"] == "text" and isinstance(item["text"], str) for item in content)


@pytest.mark.asyncio
async def test_native_take_snapshot_returns_uid_tagged_tree_envelope() -> None:
    handler, fake = _handler_with_fake_client()
    resp = await handler._dispatch_native("take_snapshot", {})
    _bare_envelope(resp)
    text = "".join(extract_text_items(resp))
    assert "[uid=" in text
    assert 'button "Submit"' in text
    # The stitched read enabled Accessibility and probed frames.
    assert "Accessibility.enable" in fake.methods
    assert "Page.getFrameTree" in fake.methods


@pytest.mark.asyncio
async def test_native_click_resolves_snapshot_uid_and_dispatches_mouse() -> None:
    handler, fake = _handler_with_fake_client()
    await handler._dispatch_native("take_snapshot", {})
    # The button is the second node in document order: uid "<gen>-2".
    snapshot = handler._native_reader.current
    assert snapshot is not None
    button = next(n for n in snapshot.visible_nodes() if n.role == "button")
    resp = await handler._dispatch_native("click", {"uid": button.uid})
    _bare_envelope(resp)
    assert "Input.dispatchMouseEvent" in fake.methods
    assert resp["result"].get("isError") is not True


@pytest.mark.asyncio
async def test_native_fill_sets_value_and_reports_it() -> None:
    handler, fake = _handler_with_fake_client()
    await handler._dispatch_native("take_snapshot", {})
    button = next(n for n in handler._native_reader.current.visible_nodes() if n.role == "button")
    resp = await handler._dispatch_native("fill", {"uid": button.uid, "value": "hello"})
    _bare_envelope(resp)
    assert "Runtime.callFunctionOn" in fake.methods
    assert "typed-value" in "".join(extract_text_items(resp))


@pytest.mark.asyncio
async def test_native_interaction_without_uid_is_an_error_envelope() -> None:
    handler, _ = _handler_with_fake_client()
    resp = await handler._dispatch_native("click", {})
    assert resp["result"].get("isError") is True
    assert "uid" in "".join(extract_text_items(resp)).lower()


@pytest.mark.asyncio
async def test_native_stale_uid_is_refused_as_error_envelope() -> None:
    handler, _ = _handler_with_fake_client()
    await handler._dispatch_native("take_snapshot", {})
    # A UID from a superseded generation resolves for no snapshot.
    resp = await handler._dispatch_native("click", {"uid": "99-1"})
    assert resp["result"].get("isError") is True


@pytest.mark.asyncio
async def test_native_errors_when_cdp_unavailable() -> None:
    handler = CDPHandler(browser_url=None)
    handler._cdp_or_error = lambda: (None, {"result": {"content": [{"type": "text", "text": "down"}], "isError": True}})  # type: ignore[method-assign]
    resp = await handler._dispatch_native("take_snapshot", {})
    assert resp["result"].get("isError") is True
