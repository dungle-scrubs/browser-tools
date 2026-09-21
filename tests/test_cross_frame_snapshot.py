"""Reaching inside a cross-origin iframe: whose tree, whose ids, whose session.

RFC-04 Phase 2b. Three facts decide everything here, and the middle one was
measured rather than assumed because the RFC had it slightly wrong.

    `Accessibility.getFullAXTree` answers for the renderer it is sent to. A
    cross-origin iframe is a different renderer, so the page session returns
    a tree whose `Iframe` node has nothing under it.

    A `backendDOMNodeId` is unique within a renderer process, not within a
    document and not within a browser. Same-process frames share one
    sequence and never collide; a cross-origin child's ids overlapped the
    page's on 5 of its 7 nodes. So one token per build mints `x<ordinal>`
    UIDs for the colliding half, and those address nothing.

    A UID minted inside a frame carries that frame's document. Checking it
    against the main frame's token alone refuses a node that is on screen.

Verified end to end against a real browser: clicking the host's button and
the cross-origin child's button in one run leaves `{"h":"HOST"}` in the
host's localStorage and `{"c":"CHILD"}` in the child's.
"""

from __future__ import annotations

from typing import Any

import pytest

from browser_tools.cdp_handler import CDPHandler, CDPRuntime
from browser_tools.frame_manager import FrameManager
from browser_tools.native_snapshot import (
    NODE_DOC_TOKEN_KEY,
    ChildFrameTree,
    NativeSnapshotReader,
    doc_token,
    live_doc_tokens,
    stitch_ax_frames,
)

PARENT = doc_token("ROOT", "LOADER-ROOT")
CHILD = doc_token("CHILD", "LOADER-CHILD")


def _node(node_id: str, role: str, backend: int | None, children: list[str]) -> dict[str, Any]:
    raw: dict[str, Any] = {"nodeId": node_id, "role": {"value": role}, "childIds": children}
    if backend is not None:
        raw["backendDOMNodeId"] = backend
    return raw


class TestABackendIdMeansNothingWithoutItsDocument:
    """The collision the single-token build could not represent."""

    def test_two_documents_may_both_own_backend_node_two(self):
        top = {"nodes": [_node("1", "RootWebArea", 1, ["2"]), _node("2", "Iframe", 2, [])]}
        child = {"nodes": [_node("1", "RootWebArea", 1, ["2"]), _node("2", "button", 2, [])]}
        merged = stitch_ax_frames(
            top,
            [
                ChildFrameTree(
                    owner_backend_node_id=2,
                    owner_doc_token=PARENT,
                    doc_token=CHILD,
                    result=child,
                )
            ],
            top_doc_token=PARENT,
        )
        snap = NativeSnapshotReader().build(merged, doc_token=PARENT)

        button = next(n for n in snap.nodes if n.role == "button")
        assert button.uid == f"{CHILD}-2", "the child's button took the parent's token"
        assert button.backend_node_id == 2
        assert "-x" not in button.uid, (
            "the child's button collided with the parent's Iframe and became "
            "unaddressable, which is the defect this phase exists to fix"
        )

    def test_the_parents_node_keeps_its_own_uid(self):
        top = {"nodes": [_node("1", "RootWebArea", 1, ["2"]), _node("2", "Iframe", 2, [])]}
        child = {"nodes": [_node("1", "button", 2, [])]}
        merged = stitch_ax_frames(
            top,
            [ChildFrameTree(2, PARENT, CHILD, child)],
            top_doc_token=PARENT,
        )
        snap = NativeSnapshotReader().build(merged, doc_token=PARENT)
        iframe = next(n for n in snap.nodes if n.role == "Iframe")
        assert iframe.uid == f"{PARENT}-2"

    def test_every_node_is_stamped_with_the_document_it_came_from(self):
        top = {"nodes": [_node("1", "RootWebArea", 1, ["2"]), _node("2", "Iframe", 2, [])]}
        child = {"nodes": [_node("1", "button", 9, [])]}
        merged = stitch_ax_frames(
            top, [ChildFrameTree(2, PARENT, CHILD, child)], top_doc_token=PARENT
        )
        stamps = {n["nodeId"]: n[NODE_DOC_TOKEN_KEY] for n in merged["nodes"]}
        assert stamps == {"1": PARENT, "2": PARENT, "f1:1": CHILD}


class TestTheOwnerLookupIsScopedToItsDocument:
    def test_a_nested_frame_does_not_hang_off_a_same_id_node_elsewhere(self):
        """The owner is found by backend id, which repeats across renderers."""
        other = doc_token("OTHER", "LOADER-OTHER")
        top = {"nodes": [_node("1", "RootWebArea", 1, ["2"]), _node("2", "Iframe", 5, [])]}
        # The first child owns backend 5 as well, in its own document.
        child_a = {"nodes": [_node("1", "RootWebArea", 5, ["2"]), _node("2", "Iframe", 7, [])]}
        grandchild = {"nodes": [_node("1", "button", 1, [])]}
        merged = stitch_ax_frames(
            top,
            [
                ChildFrameTree(5, PARENT, CHILD, child_a),
                # The grandchild hangs off backend 7 IN THE CHILD's document.
                ChildFrameTree(7, CHILD, other, grandchild),
            ],
            top_doc_token=PARENT,
        )
        snap = NativeSnapshotReader().build(merged, doc_token=PARENT)
        button = next(n for n in snap.nodes if n.role == "button")
        inner_iframe = next(n for n in snap.nodes if n.uid == f"{CHILD}-7")
        assert button.parent_uid == inner_iframe.uid

    def test_the_wrong_document_is_not_chosen_when_the_id_repeats(self):
        """Two documents hold backend 5. The grandchild belongs to one of them.

        A lookup by backend id alone finds whichever was merged first, which
        is the top document's `Iframe`, and hangs the grandchild there - one
        level too high and in the wrong process.
        """
        other = doc_token("OTHER", "LOADER-OTHER")
        top = {"nodes": [_node("1", "RootWebArea", 1, ["2"]), _node("2", "Iframe", 5, [])]}
        child = {"nodes": [_node("1", "RootWebArea", 5, [])]}
        grandchild = {"nodes": [_node("1", "button", 1, [])]}
        merged = stitch_ax_frames(
            top,
            [
                ChildFrameTree(5, PARENT, CHILD, child),
                ChildFrameTree(5, CHILD, other, grandchild),
            ],
            top_doc_token=PARENT,
        )
        snap = NativeSnapshotReader().build(merged, doc_token=PARENT)
        button = next(n for n in snap.nodes if n.role == "button")
        assert button.parent_uid == f"{CHILD}-5", (
            "the grandchild hung off the top document's node of the same "
            "backend id instead of its own parent's"
        )

    def test_a_frame_whose_owner_is_missing_keeps_its_nodes(self):
        top = {"nodes": [_node("1", "RootWebArea", 1, [])]}
        child = {"nodes": [_node("1", "button", 9, [])]}
        merged = stitch_ax_frames(
            top, [ChildFrameTree(999, PARENT, CHILD, child)], top_doc_token=PARENT
        )
        assert any(n["nodeId"] == "f1:1" for n in merged["nodes"])


class TestADocumentTokenIsAnIdentity:
    def test_two_frames_sharing_a_loader_id_get_different_tokens(self):
        assert doc_token("FRAME-A", "SAME") != doc_token("FRAME-B", "SAME")

    def test_two_loader_ids_sharing_a_prefix_get_different_tokens(self):
        """The collision the old 12-character truncation allowed."""
        assert doc_token("F", "A" * 12 + "0" * 20) != doc_token("F", "A" * 12 + "1" * 20)

    def test_the_same_document_gets_the_same_token_every_time(self):
        assert doc_token("F", "L") == doc_token("F", "L")


class TestAUidIsCheckedAgainstEveryLiveDocument:
    def test_the_token_set_covers_child_frames(self):
        tree = {
            "frame": {"id": "ROOT", "loaderId": "LOADER-ROOT"},
            "childFrames": [
                {
                    "frame": {"id": "CHILD", "loaderId": "LOADER-CHILD"},
                    "childFrames": [{"frame": {"id": "GRAND", "loaderId": "LG"}}],
                }
            ],
        }
        assert live_doc_tokens(tree) == {PARENT, CHILD, doc_token("GRAND", "LG")}

    def test_an_empty_tree_admits_nothing(self):
        assert live_doc_tokens({}) == set()


class _Client:
    connected = True
    session_id = "PAGE-SESSION"

    def __init__(self) -> None:
        self.raw = self
        self._connected = True
        self.sent: list[tuple[str, str | None, dict[str, Any] | None]] = []

    async def send(self, method, params=None, session_id="PAGE-SESSION", timeout=None):
        self.sent.append((method, session_id, params))
        if method == "Accessibility.getFullAXTree":
            if session_id == "S1":
                return {"nodes": [_node("1", "button", 2, [])]}
            return {"nodes": [_node("1", "RootWebArea", 1, ["2"]), _node("2", "Iframe", 2, [])]}
        if method == "DOM.getFrameOwner":
            return {"backendNodeId": 2}
        return {}


def _handler(client: Any, frames: FrameManager, sessions: Any) -> CDPHandler:
    runtime = CDPRuntime.__new__(CDPRuntime)
    runtime._cdp_client = client
    runtime._frame_manager = frames
    runtime._frame_sessions = sessions
    handler = CDPHandler.__new__(CDPHandler)
    handler._rt = runtime
    return handler


def _two_process_page() -> FrameManager:
    frames = FrameManager()
    frames.update_from_frame_tree({"frame": {"id": "ROOT", "loaderId": "LOADER-ROOT"}})
    frames.splice_session_tree(
        {"frame": {"id": "CHILD", "parentId": "ROOT", "loaderId": "LOADER-CHILD"}}, "S1"
    )
    return frames


class TestEachFramesTreeIsReadOnItsOwnSession:
    @pytest.mark.asyncio
    async def test_the_child_tree_comes_from_the_child_session(self):
        client = _Client()
        handler = _handler(client, _two_process_page(), object())
        documents = handler._cross_process_documents()

        merged, token = await handler._stitch_across_sessions(documents)

        trees = [sid for m, sid, _ in client.sent if m == "Accessibility.getFullAXTree"]
        assert trees == ["PAGE-SESSION", "S1"]
        assert token == PARENT
        assert {n[NODE_DOC_TOKEN_KEY] for n in merged["nodes"]} == {PARENT, CHILD}

    @pytest.mark.asyncio
    async def test_the_owner_lookup_goes_to_the_parent(self):
        """`DOM.getFrameOwner` answers about a frame's owner node, which is
        in the parent's document, so it is the parent's session that knows."""
        client = _Client()
        handler = _handler(client, _two_process_page(), object())

        await handler._stitch_across_sessions(handler._cross_process_documents())

        owners = [sid for m, sid, _ in client.sent if m == "DOM.getFrameOwner"]
        assert owners == ["PAGE-SESSION"]

    @pytest.mark.asyncio
    async def test_the_child_tree_is_asked_for_without_a_frame_id(self):
        """Its own session owns exactly that frame; naming one asks a
        renderer about a frame it does not have."""
        client = _Client()
        handler = _handler(client, _two_process_page(), object())

        await handler._stitch_across_sessions(handler._cross_process_documents())

        child_reads = [
            params
            for m, sid, params in client.sent
            if m == "Accessibility.getFullAXTree" and sid == "S1"
        ]
        assert child_reads == [None]

    def test_a_page_with_no_out_of_process_frame_takes_the_plain_path(self):
        frames = FrameManager()
        frames.update_from_frame_tree({"frame": {"id": "ROOT", "loaderId": "L"}})
        handler = _handler(_Client(), frames, object())
        assert handler._cross_process_documents() == []

    def test_the_default_flag_never_takes_the_cross_session_path(self):
        handler = _handler(_Client(), _two_process_page(), None)
        assert handler._cross_process_documents() == []


class TestAnInteractionGoesToTheDocumentTheUidNames:
    @pytest.mark.asyncio
    async def test_a_child_uid_is_sent_to_the_child_session(self):
        client = _Client()
        handler = _handler(client, _two_process_page(), object())
        # Asserted by where the command lands, not by object identity: a
        # bound method is a fresh object on every access.
        await handler._send_for_uid(f"{CHILD}-2", client.send)("DOM.resolveNode")
        assert [sid for _, sid, _ in client.sent] == ["S1"]

    @pytest.mark.asyncio
    async def test_a_page_uid_stays_on_the_page_session(self):
        client = _Client()
        handler = _handler(client, _two_process_page(), object())
        await handler._send_for_uid(f"{PARENT}-1", client.send)("DOM.resolveNode")
        assert [sid for _, sid, _ in client.sent] == ["PAGE-SESSION"]

    @pytest.mark.asyncio
    async def test_an_unknown_token_falls_back_rather_than_failing_here(self):
        """The staleness error belongs to the resolver, which words it."""
        client = _Client()
        handler = _handler(client, _two_process_page(), object())
        await handler._send_for_uid("DEADBEEF0000-1", client.send)("DOM.resolveNode")
        assert [sid for _, sid, _ in client.sent] == ["PAGE-SESSION"]


class _InteractionClient(_Client):
    """Answers the frame-tree read each session gets during UID resolution."""

    async def send(self, method, params=None, session_id="PAGE-SESSION", timeout=None):
        self.sent.append((method, session_id, params))
        if method == "Page.getFrameTree":
            if session_id == "S1":
                return {"frameTree": {"frame": {"id": "CHILD", "loaderId": "LOADER-CHILD"}}}
            return {"frameTree": {"frame": {"id": "ROOT", "loaderId": "LOADER-ROOT"}}}
        return {}


class TestTheDispatcherRoutesTheInteractionItWasHanded:
    """`_send_for_uid` being right is not enough; the dispatcher has to call it."""

    @pytest.mark.asyncio
    async def test_a_click_on_a_child_uid_resolves_against_the_child_session(self):
        client = _InteractionClient()
        handler = _handler(client, _two_process_page(), object())
        handler._native_reader = NativeSnapshotReader()
        from browser_tools.native_interaction import NativeInteractor

        handler._native_interactor = NativeInteractor(handler._native_reader)

        await handler._dispatch_native("click", {"uid": f"{CHILD}-2"})

        trees = [sid for m, sid, _ in client.sent if m == "Page.getFrameTree"]
        assert trees and trees[0] == "S1", (
            f"the child's UID was resolved against {trees}, not its own session"
        )

    @pytest.mark.asyncio
    async def test_a_click_on_a_page_uid_stays_on_the_page_session(self):
        client = _InteractionClient()
        handler = _handler(client, _two_process_page(), object())
        handler._native_reader = NativeSnapshotReader()
        from browser_tools.native_interaction import NativeInteractor

        handler._native_interactor = NativeInteractor(handler._native_reader)

        await handler._dispatch_native("click", {"uid": f"{PARENT}-1"})

        trees = [sid for m, sid, _ in client.sent if m == "Page.getFrameTree"]
        assert trees and trees[0] == "PAGE-SESSION"


class TestARefusedUidSaysWhichReasonItWas:
    """A live UID refused for the wrong stated reason sends the caller in
    circles: taking another snapshot mints the same UID and fails again."""

    class _WithTargets(_InteractionClient):
        async def send(self, method, params=None, session_id="PAGE-SESSION", timeout=None):
            if method == "Target.getTargets":
                self.sent.append((method, session_id, params))
                return {
                    "targetInfos": [
                        {"type": "iframe", "url": "http://other/ad"},
                        {"type": "page", "url": "http://host/"},
                    ]
                }
            return await super().send(method, params, session_id, timeout)

    @pytest.mark.asyncio
    async def test_without_the_flag_it_names_the_flag(self):
        client = self._WithTargets()
        handler = _handler(client, _two_process_page(), None)
        handler._native_reader = NativeSnapshotReader()
        from browser_tools.native_interaction import NativeInteractor

        handler._native_interactor = NativeInteractor(handler._native_reader)

        result = await handler._dispatch_native("click", {"uid": "DEADBEEF0000-2"})

        text = str(result)
        assert "--frames all" in text
        assert "http://other/ad" in text

    @pytest.mark.asyncio
    async def test_with_the_flag_on_it_stays_the_plain_staleness_error(self):
        """Every live document really was checked, so the UID really is stale."""
        client = self._WithTargets()
        handler = _handler(client, _two_process_page(), object())
        handler._native_reader = NativeSnapshotReader()
        from browser_tools.native_interaction import NativeInteractor

        handler._native_interactor = NativeInteractor(handler._native_reader)

        result = await handler._dispatch_native("click", {"uid": "DEADBEEF0000-2"})

        assert "--frames all" not in str(result)

    @pytest.mark.asyncio
    async def test_a_page_with_no_cross_origin_iframe_gets_the_plain_error(self):
        client = _InteractionClient()
        handler = _handler(client, _two_process_page(), None)
        handler._native_reader = NativeSnapshotReader()
        from browser_tools.native_interaction import NativeInteractor

        handler._native_interactor = NativeInteractor(handler._native_reader)

        result = await handler._dispatch_native("click", {"uid": "DEADBEEF0000-2"})

        assert "--frames all" not in str(result)
