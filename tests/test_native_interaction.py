"""Unit tests for the native UID interaction path (ticket #40).

These exercise UID -> backend-node resolution and the CDP call sequence for
``click`` and ``fill`` against synthetic structures, with no live browser
(RFC-01 Testing Strategy; the live/corpus path is the parity harness). They
prove:

- An interaction resolves a UID through #39's reader and refuses exactly the
  UIDs the reader no longer resolves (stale, unknown, no snapshot) plus nodes
  with no DOM backing.
- ``click`` dispatches trusted mouse events at the element's box centre.
- ``fill`` focuses, resolves the node, and sets the value via a Runtime call.
- The same sans-IO protocol runs identically under the sync and async drivers.
"""

from __future__ import annotations

from typing import Any

import pytest

from browser_tools.native_interaction import (
    DOM_FOCUS,
    DOM_GET_BOX_MODEL,
    DOM_RESOLVE_NODE,
    DOM_SCROLL_INTO_VIEW,
    INPUT_DISPATCH_MOUSE,
    RUNTIME_CALL_FUNCTION_ON,
    CdpCall,
    InteractionResult,
    NativeInteractor,
    UidResolutionError,
    click_steps,
    drive_async,
    drive_sync,
    fill_steps,
)
from browser_tools.native_snapshot import NativeSnapshotReader


def _form_tree() -> dict[str, Any]:
    """root > (heading, form > (textbox=1-4 backend40, button=1-5 backend50))."""
    def node(nid, role, name="", parent=None, children=None, backend=None, value=None):
        raw: dict[str, Any] = {
            "nodeId": nid,
            "role": {"type": "role", "value": role},
            "name": {"type": "computedString", "value": name},
            "childIds": children or [],
            "ignored": False,
        }
        if parent is not None:
            raw["parentId"] = parent
        if backend is not None:
            raw["backendDOMNodeId"] = backend
        if value is not None:
            raw["value"] = {"type": "computedString", "value": value}
        return raw

    return {
        "nodes": [
            node("1", "RootWebArea", "Sign in", children=["2", "3"], backend=10),
            node("2", "heading", "Welcome", parent="1", backend=20),
            node("3", "form", parent="1", children=["4", "5"], backend=30),
            node("4", "textbox", "Email", parent="3", backend=40, value=""),
            node("5", "button", "Submit", parent="3", backend=50),
        ]
    }


# A backend node id -> a canned box-model quad (a 20x10 box at that offset).
def _box_for(backend: int) -> dict[str, Any]:
    top = backend  # deterministic, distinct per node
    quad = [10.0, top, 30.0, top, 30.0, top + 10.0, 10.0, top + 10.0]
    return {"model": {"content": quad, "width": 20, "height": 10}}


class _RecordingSend:
    """A synchronous fake CDP transport that records calls and returns canned results."""

    def __init__(self) -> None:
        self.calls: list[CdpCall] = []

    def __call__(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(CdpCall(method, params or {}))
        if method == DOM_GET_BOX_MODEL:
            return _box_for(int((params or {})["backendNodeId"]))
        if method == DOM_RESOLVE_NODE:
            return {"object": {"objectId": f"obj-{(params or {})['backendNodeId']}"}}
        if method == RUNTIME_CALL_FUNCTION_ON:
            # Echo the value the fill function would have set.
            return {"result": {"value": (params or {})["arguments"][0]["value"]}}
        return {}


# --------------------------------------------------------------------------- #
# UID -> node resolution (reuses #39's reader / stability contract)
# --------------------------------------------------------------------------- #


def test_resolve_uses_reader_current_snapshot():
    reader = NativeSnapshotReader()
    reader.build(_form_tree())
    node = NativeInteractor(reader).resolve("1-5")
    assert node.role == "button" and node.backend_node_id == 50


def test_resolve_raises_when_no_snapshot_taken():
    with pytest.raises(UidResolutionError) as exc:
        NativeInteractor(NativeSnapshotReader()).resolve("1-5")
    assert exc.value.uid == "1-5"
    assert "no current snapshot" in exc.value.reason


def test_resolve_raises_for_stale_uid_after_new_snapshot():
    reader = NativeSnapshotReader()
    reader.build(_form_tree())
    reader.build(_form_tree())  # supersedes generation 1
    with pytest.raises(UidResolutionError) as exc:
        NativeInteractor(reader).resolve("1-5")
    assert "stale" in exc.value.reason


def test_resolve_raises_for_stale_uid_after_navigation():
    reader = NativeSnapshotReader()
    reader.build(_form_tree())
    reader.note_navigation()
    with pytest.raises(UidResolutionError):
        NativeInteractor(reader).resolve("1-5")


def test_resolve_raises_for_node_without_backend_dom_node():
    tree = {
        "nodes": [
            {
                "nodeId": "1",
                "role": {"type": "role", "value": "RootWebArea"},
                "name": {"type": "computedString", "value": ""},
                "childIds": [],
                "ignored": False,
                # no backendDOMNodeId
            }
        ]
    }
    reader = NativeSnapshotReader()
    reader.build(tree)
    with pytest.raises(UidResolutionError) as exc:
        NativeInteractor(reader).resolve("1-1")
    assert "no backend DOM node" in exc.value.reason


# --------------------------------------------------------------------------- #
# click: CDP sequence (sans-IO generator + sync driver)
# --------------------------------------------------------------------------- #


def test_click_steps_sequence_and_centre_point():
    reader = NativeSnapshotReader()
    snap = reader.build(_form_tree())
    button = snap.resolve("1-5")
    assert button is not None

    steps = click_steps(button)
    # 1) scroll into view
    call = next(steps)
    assert call == CdpCall(DOM_SCROLL_INTO_VIEW, {"backendNodeId": 50})
    # 2) box model -> feed the canned quad
    call = steps.send({})
    assert call == CdpCall(DOM_GET_BOX_MODEL, {"backendNodeId": 50})
    # 3-5) mouse move/press/release at the box centre (x=20, y=55 for backend 50)
    call = steps.send(_box_for(50))
    assert call.method == INPUT_DISPATCH_MOUSE and call.params["type"] == "mouseMoved"
    assert (call.params["x"], call.params["y"]) == (20.0, 55.0)
    assert call.params["buttons"] == 0 and "button" not in call.params
    call = steps.send({})
    assert call.params["type"] == "mousePressed"
    assert call.params["button"] == "left" and call.params["buttons"] == 1
    assert call.params["clickCount"] == 1
    call = steps.send({})
    assert call.params["type"] == "mouseReleased"
    assert call.params["buttons"] == 0
    with pytest.raises(StopIteration) as stop:
        steps.send({})
    result = stop.value.value
    assert isinstance(result, InteractionResult)
    assert result.action == "click" and result.backend_node_id == 50
    assert result.point == (20.0, 55.0)


def test_click_via_sync_driver_records_full_cdp_sequence():
    reader = NativeSnapshotReader()
    reader.build(_form_tree())
    send = _RecordingSend()
    result = NativeInteractor(reader).click(send, "1-5")

    assert [c.method for c in send.calls] == [
        DOM_SCROLL_INTO_VIEW,
        DOM_GET_BOX_MODEL,
        INPUT_DISPATCH_MOUSE,
        INPUT_DISPATCH_MOUSE,
        INPUT_DISPATCH_MOUSE,
    ]
    assert result.methods == tuple(c.method for c in send.calls)
    assert result.point == (20.0, 55.0)


def test_click_refuses_stale_uid_before_any_cdp_call():
    reader = NativeSnapshotReader()
    reader.build(_form_tree())
    reader.build(_form_tree())
    send = _RecordingSend()
    with pytest.raises(UidResolutionError):
        NativeInteractor(reader).click(send, "1-5")
    assert send.calls == []  # nothing dispatched


# --------------------------------------------------------------------------- #
# fill: CDP sequence
# --------------------------------------------------------------------------- #


def test_fill_steps_sequence_focus_resolve_set_value():
    reader = NativeSnapshotReader()
    snap = reader.build(_form_tree())
    textbox = snap.resolve("1-4")
    assert textbox is not None

    steps = fill_steps(textbox, "a@b.com")
    assert next(steps) == CdpCall(DOM_SCROLL_INTO_VIEW, {"backendNodeId": 40})
    assert steps.send({}) == CdpCall(DOM_FOCUS, {"backendNodeId": 40})
    assert steps.send({}) == CdpCall(DOM_RESOLVE_NODE, {"backendNodeId": 40})
    call = steps.send({"object": {"objectId": "obj-40"}})
    assert call.method == RUNTIME_CALL_FUNCTION_ON
    assert call.params["objectId"] == "obj-40"
    assert call.params["arguments"] == [{"value": "a@b.com"}]
    assert call.params["returnByValue"] is True
    with pytest.raises(StopIteration) as stop:
        steps.send({"result": {"value": "a@b.com"}})
    result = stop.value.value
    assert result.action == "fill" and result.text == "a@b.com"
    assert result.value_after == "a@b.com"


def test_fill_via_sync_driver_records_full_cdp_sequence():
    reader = NativeSnapshotReader()
    reader.build(_form_tree())
    send = _RecordingSend()
    result = NativeInteractor(reader).fill(send, "1-4", "hello@example.com")

    assert [c.method for c in send.calls] == [
        DOM_SCROLL_INTO_VIEW,
        DOM_FOCUS,
        DOM_RESOLVE_NODE,
        RUNTIME_CALL_FUNCTION_ON,
    ]
    call = send.calls[-1]
    assert call.params["arguments"] == [{"value": "hello@example.com"}]
    assert result.value_after == "hello@example.com"


def test_fill_raises_when_resolve_node_returns_no_object_id():
    reader = NativeSnapshotReader()
    snap = reader.build(_form_tree())
    textbox = snap.resolve("1-4")
    assert textbox is not None
    steps = fill_steps(textbox, "x")
    next(steps)  # scroll
    steps.send({})  # focus
    steps.send({})  # resolveNode call issued
    with pytest.raises(ValueError, match="no objectId"):
        steps.send({"object": {}})


# --------------------------------------------------------------------------- #
# async driver runs the same protocol
# --------------------------------------------------------------------------- #


class _AsyncRecordingSend:
    def __init__(self) -> None:
        self._sync = _RecordingSend()

    @property
    def calls(self) -> list[CdpCall]:
        return self._sync.calls

    async def __call__(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._sync(method, params)


@pytest.mark.asyncio
async def test_async_driver_matches_sync_for_click():
    reader = NativeSnapshotReader()
    reader.build(_form_tree())
    send = _AsyncRecordingSend()
    result = await NativeInteractor(reader).click_async(send, "1-5")
    assert [c.method for c in send.calls] == [
        DOM_SCROLL_INTO_VIEW,
        DOM_GET_BOX_MODEL,
        INPUT_DISPATCH_MOUSE,
        INPUT_DISPATCH_MOUSE,
        INPUT_DISPATCH_MOUSE,
    ]
    assert result.point == (20.0, 55.0)


@pytest.mark.asyncio
async def test_async_driver_matches_sync_for_fill():
    reader = NativeSnapshotReader()
    reader.build(_form_tree())
    send = _AsyncRecordingSend()
    result = await NativeInteractor(reader).fill_async(send, "1-4", "typed")
    assert [c.method for c in send.calls][-1] == RUNTIME_CALL_FUNCTION_ON
    assert result.value_after == "typed"


def test_sync_and_async_drivers_share_one_protocol_definition():
    # drive_sync and drive_async consume click_steps / fill_steps; the protocol
    # is defined once. Prove the sync driver returns the generator's value.
    reader = NativeSnapshotReader()
    snap = reader.build(_form_tree())
    button = snap.resolve("1-5")
    assert button is not None
    send = _RecordingSend()
    result = drive_sync(click_steps(button), send)
    assert result.action == "click" and result.methods[0] == DOM_SCROLL_INTO_VIEW


@pytest.mark.asyncio
async def test_drive_async_returns_generator_value():
    reader = NativeSnapshotReader()
    snap = reader.build(_form_tree())
    textbox = snap.resolve("1-4")
    assert textbox is not None
    send = _AsyncRecordingSend()
    result = await drive_async(fill_steps(textbox, "v"), send)
    assert result.action == "fill" and result.value_after == "v"
