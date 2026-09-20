"""Unit tests for the native UID interaction path (#40, reworked by #96).

These exercise UID resolution and the CDP call sequence for ``click`` and
``fill`` against synthetic structures, with no live browser (RFC-01 Testing
Strategy; the live/corpus path is the parity harness). They prove:

- An interaction resolves a UID from the UID itself -- its document token and
  its backend DOM node -- and takes no snapshot. The internal snapshot this
  path used to take is what made the old staleness check unable to fire.
- A UID minted against a previous document is refused before any interaction
  traffic, with the remedy in the message.
- A UID whose DOM node is not an Element is refused rather than focused.
- ``click`` dispatches trusted mouse events at the element's box centre.
- ``fill`` focuses, resolves the node, and sets the value via a Runtime call.
- The same sans-IO protocol runs identically under the sync and async drivers.
"""

from __future__ import annotations

from typing import Any

import pytest

from browser_tools.native_interaction import (
    DOM_DESCRIBE_NODE,
    DOM_FOCUS,
    DOM_GET_BOX_MODEL,
    DOM_RESOLVE_NODE,
    DOM_SCROLL_INTO_VIEW,
    ELEMENT_NODE_TYPE,
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
from browser_tools.native_snapshot import (
    PAGE_GET_FRAME_TREE,
    NativeSnapshotReader,
    doc_token_from_loader_id,
)

LOADER_ID = "D0C0FFEE1234ABCDEF0123456789ABCD"
DOC = doc_token_from_loader_id(LOADER_ID)
OTHER_LOADER_ID = "0B50LETE5678FEDCBA9876543210FEDC"
OTHER_DOC = doc_token_from_loader_id(OTHER_LOADER_ID)

TEXTBOX = f"{DOC}-40"
BUTTON = f"{DOC}-50"


def _form_tree() -> dict[str, Any]:
    """root(10) > (heading(20), form(30) > (textbox(40), button(50)))."""

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


def _box_for(backend: int) -> dict[str, Any]:
    """A canned 20x10 box-model quad, at an offset distinct per node."""
    top = backend
    quad = [10.0, top, 30.0, top, 30.0, top + 10.0, 10.0, top + 10.0]
    return {"model": {"content": quad, "width": 20, "height": 10}}


class _RecordingSend:
    """A synchronous fake CDP transport recording calls and returning canned results."""

    def __init__(self, *, loader_id: str = LOADER_ID, node_type: int = ELEMENT_NODE_TYPE) -> None:
        self.calls: list[CdpCall] = []
        self._loader_id = loader_id
        self._node_type = node_type

    @property
    def methods(self) -> list[str]:
        return [call.method for call in self.calls]

    def __call__(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(CdpCall(method, params or {}))
        if method == PAGE_GET_FRAME_TREE:
            return {"frameTree": {"frame": {"loaderId": self._loader_id}}}
        if method == DOM_DESCRIBE_NODE:
            return {"node": {"nodeType": self._node_type}}
        if method == DOM_GET_BOX_MODEL:
            return _box_for(int((params or {})["backendNodeId"]))
        if method == DOM_RESOLVE_NODE:
            return {"object": {"objectId": f"obj-{(params or {})['backendNodeId']}"}}
        if method == RUNTIME_CALL_FUNCTION_ON:
            return {"result": {"value": (params or {})["arguments"][0]["value"]}}
        return {}


class _AsyncRecordingSend:
    def __init__(self, **kwargs: Any) -> None:
        self._sync = _RecordingSend(**kwargs)

    @property
    def calls(self) -> list[CdpCall]:
        return self._sync.calls

    @property
    def methods(self) -> list[str]:
        return self._sync.methods

    async def __call__(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._sync(method, params)


def _interactor() -> NativeInteractor:
    """An interactor with no snapshot taken -- the state a CLI verb runs in."""
    return NativeInteractor(NativeSnapshotReader())


# --------------------------------------------------------------------------- #
# Resolution: from the UID, against the live document
# --------------------------------------------------------------------------- #


class TestResolution:
    def test_resolves_without_any_snapshot(self) -> None:
        """The whole point: a one-shot process has no snapshot and must still act."""
        send = _RecordingSend()
        assert _interactor().resolve(send, BUTTON) == 50
        assert send.methods == [PAGE_GET_FRAME_TREE], "resolution read no tree"

    def test_a_uid_from_a_previous_document_is_refused(self) -> None:
        send = _RecordingSend(loader_id=OTHER_LOADER_ID)
        with pytest.raises(UidResolutionError) as exc:
            _interactor().resolve(send, BUTTON)
        assert exc.value.uid == BUTTON
        assert "previous document" in exc.value.reason
        assert "take a new snapshot" in exc.value.reason, "the remedy must be named"

    def test_a_uid_naming_no_dom_node_is_refused(self) -> None:
        send = _RecordingSend()
        with pytest.raises(UidResolutionError) as exc:
            _interactor().resolve(send, f"{DOC}-x7")
        assert "no backend DOM node" in exc.value.reason

    def test_a_malformed_uid_is_refused(self) -> None:
        send = _RecordingSend()
        with pytest.raises(UidResolutionError):
            _interactor().resolve(send, "nonsense")

    def test_the_same_uid_resolves_twice_with_no_snapshot_between(self) -> None:
        """Two interactions from one snapshot: neither invalidates the other."""
        send = _RecordingSend()
        interactor = _interactor()
        assert interactor.resolve(send, TEXTBOX) == 40
        assert interactor.resolve(send, BUTTON) == 50

    def test_a_uid_survives_a_new_snapshot_of_the_same_document(self) -> None:
        reader = NativeSnapshotReader()
        reader.build(_form_tree(), doc_token=DOC)
        reader.build(_form_tree(), doc_token=DOC)
        assert NativeInteractor(reader).resolve(_RecordingSend(), BUTTON) == 50


# --------------------------------------------------------------------------- #
# The element check
# --------------------------------------------------------------------------- #


class TestElementCheck:
    """A UID whose node is not an Element is refused, not focused."""

    TEXT_NODE_TYPE = 3

    def test_click_refuses_a_text_node(self) -> None:
        send = _RecordingSend(node_type=self.TEXT_NODE_TYPE)
        with pytest.raises(UidResolutionError, match="not an element"):
            _interactor().click(send, BUTTON)
        assert INPUT_DISPATCH_MOUSE not in send.methods
        assert DOM_SCROLL_INTO_VIEW not in send.methods

    def test_fill_refuses_a_text_node(self) -> None:
        send = _RecordingSend(node_type=self.TEXT_NODE_TYPE)
        with pytest.raises(UidResolutionError, match="not an element"):
            _interactor().fill(send, TEXTBOX, "secret")
        assert DOM_FOCUS not in send.methods, "a non-element was focused"
        assert RUNTIME_CALL_FUNCTION_ON not in send.methods

    def test_the_check_names_the_remedy(self) -> None:
        send = _RecordingSend(node_type=self.TEXT_NODE_TYPE)
        with pytest.raises(UidResolutionError) as exc:
            _interactor().click(send, BUTTON)
        assert "take a new snapshot" in exc.value.reason


# --------------------------------------------------------------------------- #
# click: CDP sequence (sans-IO generator + sync driver)
# --------------------------------------------------------------------------- #


class TestClick:
    def test_steps_sequence_and_centre_point(self) -> None:
        steps = click_steps(BUTTON, 50)
        call = next(steps)
        assert call == CdpCall(DOM_DESCRIBE_NODE, {"backendNodeId": 50})
        call = steps.send({"node": {"nodeType": ELEMENT_NODE_TYPE}})
        assert call == CdpCall(DOM_SCROLL_INTO_VIEW, {"backendNodeId": 50})
        call = steps.send({})
        assert call == CdpCall(DOM_GET_BOX_MODEL, {"backendNodeId": 50})
        # Mouse move/press/release at the box centre (x=20, y=55 for backend 50).
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
        assert result.uid == BUTTON
        assert result.point == (20.0, 55.0)

    def test_sync_driver_records_the_full_cdp_sequence(self) -> None:
        send = _RecordingSend()
        result = _interactor().click(send, BUTTON)

        assert send.methods == [
            PAGE_GET_FRAME_TREE,
            DOM_DESCRIBE_NODE,
            DOM_SCROLL_INTO_VIEW,
            DOM_GET_BOX_MODEL,
            INPUT_DISPATCH_MOUSE,
            INPUT_DISPATCH_MOUSE,
            INPUT_DISPATCH_MOUSE,
        ]
        assert result.point == (20.0, 55.0)

    def test_no_snapshot_is_ever_taken(self) -> None:
        """``Accessibility.getFullAXTree`` must not appear in an interaction."""
        send = _RecordingSend()
        _interactor().click(send, BUTTON)
        assert not any("Accessibility" in method for method in send.methods)

    def test_a_uid_from_a_previous_document_dispatches_nothing(self) -> None:
        send = _RecordingSend(loader_id=OTHER_LOADER_ID)
        with pytest.raises(UidResolutionError):
            _interactor().click(send, BUTTON)
        assert send.methods == [PAGE_GET_FRAME_TREE], "interaction traffic was sent"


# --------------------------------------------------------------------------- #
# fill: CDP sequence
# --------------------------------------------------------------------------- #


class TestFill:
    def test_steps_sequence_focus_resolve_set_value(self) -> None:
        steps = fill_steps(TEXTBOX, 40, "a@b.com")
        assert next(steps) == CdpCall(DOM_DESCRIBE_NODE, {"backendNodeId": 40})
        assert steps.send({"node": {"nodeType": ELEMENT_NODE_TYPE}}) == CdpCall(
            DOM_SCROLL_INTO_VIEW, {"backendNodeId": 40}
        )
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
        assert result.uid == TEXTBOX
        assert result.value_after == "a@b.com"

    def test_sync_driver_records_the_full_cdp_sequence(self) -> None:
        send = _RecordingSend()
        result = _interactor().fill(send, TEXTBOX, "hello@example.com")

        assert send.methods == [
            PAGE_GET_FRAME_TREE,
            DOM_DESCRIBE_NODE,
            DOM_SCROLL_INTO_VIEW,
            DOM_FOCUS,
            DOM_RESOLVE_NODE,
            RUNTIME_CALL_FUNCTION_ON,
        ]
        assert send.calls[-1].params["arguments"] == [{"value": "hello@example.com"}]
        assert result.value_after == "hello@example.com"

    def test_two_fills_from_one_snapshot_each_land_on_their_own_node(self) -> None:
        """The defect this ticket exists for, at unit level.

        Under the old scheme the second fill re-snapshotted, the ordinals had
        shifted, and the value went into whatever node now sat at that position.
        """
        send = _RecordingSend()
        interactor = _interactor()

        first = interactor.fill(send, TEXTBOX, "user@example.com")
        second = interactor.fill(send, BUTTON, "second")

        assert first.backend_node_id == 40
        assert second.backend_node_id == 50
        focused = [c.params["backendNodeId"] for c in send.calls if c.method == DOM_FOCUS]
        assert focused == [40, 50], "a fill focused the wrong node"

    def test_raises_when_resolve_node_returns_no_object_id(self) -> None:
        steps = fill_steps(TEXTBOX, 40, "x")
        next(steps)
        steps.send({"node": {"nodeType": ELEMENT_NODE_TYPE}})
        steps.send({})  # focus
        steps.send({})  # resolveNode issued
        with pytest.raises(ValueError, match="no objectId"):
            steps.send({"object": {}})


# --------------------------------------------------------------------------- #
# The async driver runs the same protocol
# --------------------------------------------------------------------------- #


class TestBothDriversShareOneProtocol:
    @pytest.mark.asyncio
    async def test_async_matches_sync_for_click(self) -> None:
        send = _AsyncRecordingSend()
        result = await _interactor().click_async(send, BUTTON)
        assert send.methods == [
            PAGE_GET_FRAME_TREE,
            DOM_DESCRIBE_NODE,
            DOM_SCROLL_INTO_VIEW,
            DOM_GET_BOX_MODEL,
            INPUT_DISPATCH_MOUSE,
            INPUT_DISPATCH_MOUSE,
            INPUT_DISPATCH_MOUSE,
        ]
        assert result.point == (20.0, 55.0)

    @pytest.mark.asyncio
    async def test_async_matches_sync_for_fill(self) -> None:
        send = _AsyncRecordingSend()
        result = await _interactor().fill_async(send, TEXTBOX, "typed")
        assert send.methods[-1] == RUNTIME_CALL_FUNCTION_ON
        assert result.value_after == "typed"

    @pytest.mark.asyncio
    async def test_async_refuses_a_previous_document_before_dispatching(self) -> None:
        send = _AsyncRecordingSend(loader_id=OTHER_LOADER_ID)
        with pytest.raises(UidResolutionError):
            await _interactor().fill_async(send, TEXTBOX, "secret")
        assert send.methods == [PAGE_GET_FRAME_TREE]

    def test_drive_sync_returns_the_generator_value(self) -> None:
        send = _RecordingSend()
        result = drive_sync(click_steps(BUTTON, 50), send)
        assert result.action == "click" and result.methods[0] == DOM_DESCRIBE_NODE

    @pytest.mark.asyncio
    async def test_drive_async_returns_the_generator_value(self) -> None:
        send = _AsyncRecordingSend()
        result = await drive_async(fill_steps(TEXTBOX, 40, "v"), send)
        assert result.action == "fill" and result.value_after == "v"
