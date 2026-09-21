"""Where a frame-scoped read is sent, and whose context id it carries.

RFC-04 Routing. An execution context id is renderer-local: it is unique
inside one renderer and means nothing outside it. Two consequences, and
before this the code had neither.

    The map needs the session in its key. Two frames in two Frame Sessions
    can both own context 1, so a map keyed by the id alone let the second
    overwrite the first, and then let a destroy for either clear the wrong
    frame.

    The read needs the session in its address. Sending the child's context
    id on the page session does not reach the child.

Pinned live as well as here: with a host page and a cross-origin iframe
both writing `localStorage.who`, `storage get` returns HOST for the host
frame and CHILD for the selected iframe. The suite passed for a while with
`client_for_session` missing altogether, which is why the send target is
asserted here rather than left to the live run.
"""

from __future__ import annotations

from typing import Any

import pytest

from browser_tools.cdp_handler import CDPHandler, CDPRuntime
from browser_tools.frame_manager import FrameManager
from browser_tools.frame_sessions import FrameSessions


class Wire:
    """The browser-level connection, recording the session on every command.

    Layered the way production is, because the routing under test happens
    at exactly this seam: `client_for_session` builds a real
    `AttachedSessionClient` over this object, and that adapter is what puts
    the session id on the wire. A single flat double would let a broken
    adapter pass.
    """

    #: What `AttachedSessionClient.connected` reads.
    _connected = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str | None, dict[str, Any] | None]] = []

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.sent.append((method, session_id, params))
        if method == "Network.getCookies":
            return {"cookies": []}
        return {"result": {"value": "{}"}}

    def on(self, event, handler, session_id=None) -> None: ...
    def off(self, event, handler) -> None: ...


class PageClient:
    """Stands in for the `AttachedSessionClient` bound to the page session."""

    session_id = "PAGE-SESSION"
    connected = True

    def __init__(self) -> None:
        self.raw = Wire()

    @property
    def sent(self):
        return self.raw.sent

    async def send(self, method, params=None, timeout=None):
        return await self.raw.send(method, params, self.session_id, timeout)


def _handler(client: Any, frames: FrameManager) -> CDPHandler:
    runtime = CDPRuntime.__new__(CDPRuntime)
    runtime._cdp_client = client
    runtime._frame_manager = frames
    handler = CDPHandler.__new__(CDPHandler)
    handler._rt = runtime
    return handler


def _page_with_child(child_session: str | None) -> FrameManager:
    frames = FrameManager()
    frames.update_from_frame_tree(
        {"frame": {"id": "ROOT", "url": "http://host/", "securityOrigin": "http://host"}}
    )
    frames.splice_session_tree(
        {"frame": {"id": "CHILD", "parentId": "ROOT", "url": "http://other/ad"}},
        child_session or "S1",
    )
    if child_session is None:
        frames._frames["CHILD"].frame_session_id = None
    return frames


class TestTheContextMapIsKeyedByTheSessionToo:
    def test_two_sessions_may_each_own_context_one(self):
        frames = _page_with_child("S1")
        page_ctx = {"context": {"id": 1, "auxData": {"frameId": "ROOT", "isDefault": True}}}
        child_ctx = {"context": {"id": 1, "auxData": {"frameId": "CHILD", "isDefault": True}}}
        frames.handle_execution_context_created(page_ctx)
        frames.handle_execution_context_created(child_ctx, session_id="S1")

        assert frames._frames["ROOT"].execution_context_id == 1
        assert frames._frames["CHILD"].execution_context_id == 1

    def test_a_childs_destroy_does_not_clear_the_page(self):
        """The sequence the RFC wrote out, which used to clear the wrong frame."""
        frames = _page_with_child("S1")
        frames.handle_execution_context_created(
            {"context": {"id": 1, "auxData": {"frameId": "ROOT", "isDefault": True}}}
        )
        frames.handle_execution_context_created(
            {"context": {"id": 1, "auxData": {"frameId": "CHILD", "isDefault": True}}},
            session_id="S1",
        )
        frames.handle_execution_context_destroyed({"executionContextId": 1}, session_id="S1")

        assert frames._frames["ROOT"].execution_context_id == 1, "the page lost its context"
        assert frames._frames["CHILD"].execution_context_id is None

    def test_a_childs_clear_does_not_clear_the_page(self):
        frames = _page_with_child("S1")
        frames.handle_execution_context_created(
            {"context": {"id": 7, "auxData": {"frameId": "ROOT", "isDefault": True}}}
        )
        frames.handle_execution_context_created(
            {"context": {"id": 1, "auxData": {"frameId": "CHILD", "isDefault": True}}},
            session_id="S1",
        )
        frames.handle_execution_contexts_cleared(session_id="S1")

        assert frames._frames["ROOT"].execution_context_id == 7
        assert frames._frames["CHILD"].execution_context_id is None

    def test_the_pages_own_clear_still_clears_the_page(self):
        frames = _page_with_child("S1")
        frames.handle_execution_context_created(
            {"context": {"id": 7, "auxData": {"frameId": "ROOT", "isDefault": True}}}
        )
        frames.handle_execution_contexts_cleared()
        assert frames._frames["ROOT"].execution_context_id is None


class TestTheSelectionIsAnAddressNotAnId:
    def test_it_names_the_session_the_context_belongs_to(self):
        frames = _page_with_child("S1")
        frames.handle_execution_context_created(
            {"context": {"id": 1, "auxData": {"frameId": "CHILD", "isDefault": True}}},
            session_id="S1",
        )
        frames.select_frame_by_url("other")
        assert frames.get_selected_context() == ("S1", 1)

    def test_a_page_frame_names_the_page_session(self):
        frames = _page_with_child("S1")
        frames.handle_execution_context_created(
            {"context": {"id": 4, "auxData": {"frameId": "ROOT", "isDefault": True}}}
        )
        frames.select_frame_by_url("host")
        assert frames.get_selected_context() == (None, 4)

    def test_no_context_is_no_address(self):
        frames = _page_with_child("S1")
        frames.select_frame_by_url("other")
        assert frames.get_selected_context() is None


class TestTheReadGoesToTheFramesOwnSession:
    """The half no unit test covered: `client_for_session` was absent and
    every test still passed, because nothing asserted the send target."""

    @pytest.mark.asyncio
    async def test_storage_evaluates_on_the_frame_session(self):
        frames = _page_with_child("S1")
        frames.handle_execution_context_created(
            {"context": {"id": 1, "auxData": {"frameId": "CHILD", "isDefault": True}}},
            session_id="S1",
        )
        frames.select_frame_by_url("other")
        client = PageClient()
        handler = _handler(client, frames)

        await handler._handle_get_frame_storage({})

        evaluates = [(m, sid) for m, sid, _ in client.sent if m == "Runtime.evaluate"]
        assert evaluates, "no evaluate was sent at all"
        assert {sid for _, sid in evaluates} == {"S1"}, (
            f"the child's context id went to the wrong session: {evaluates}"
        )

    @pytest.mark.asyncio
    async def test_a_page_frame_still_evaluates_on_the_page_session(self):
        frames = _page_with_child("S1")
        frames.handle_execution_context_created(
            {"context": {"id": 4, "auxData": {"frameId": "ROOT", "isDefault": True}}}
        )
        frames.select_frame_by_url("host")
        client = PageClient()
        handler = _handler(client, frames)

        await handler._handle_get_frame_storage({})

        evaluates = [(m, sid) for m, sid, _ in client.sent if m == "Runtime.evaluate"]
        assert {sid for _, sid in evaluates} == {"PAGE-SESSION"}

    @pytest.mark.asyncio
    async def test_cookies_stay_on_the_page_session(self):
        """`Network.getCookies` takes a URL, not a context. It is not routed."""
        frames = _page_with_child("S1")
        frames.handle_execution_context_created(
            {"context": {"id": 1, "auxData": {"frameId": "CHILD", "isDefault": True}}},
            session_id="S1",
        )
        frames.select_frame_by_url("other")
        client = PageClient()
        handler = _handler(client, frames)

        await handler._handle_get_frame_storage({})

        cookies = [sid for m, sid, _ in client.sent if m == "Network.getCookies"]
        assert cookies == ["PAGE-SESSION"]

    def test_the_page_session_needs_no_adapter(self):
        handler = _handler(PageClient(), _page_with_child("S1"))
        assert handler.client_for_session(None) is handler._cdp_client

    def test_a_frame_session_gets_one_bound_to_it(self):
        handler = _handler(PageClient(), _page_with_child("S1"))
        assert handler.client_for_session("S1").session_id == "S1"


class TimelineClient(Wire):
    """Records subscriptions and commands on one timeline, in order.

    Two of the rules here are about ordering and binding, and neither is
    visible in a client that only records commands: a handler registered
    after the enable that replays to it still exists, and a handler bound
    to the wrong session still runs.
    """

    def __init__(self) -> None:
        super().__init__()
        self.timeline: list[str] = []
        self.handlers: list[tuple[str, str | None, Any]] = []

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.timeline.append(f"send:{method}")
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "CHILD", "parentId": "ROOT", "url": "http://o/"}}}
        return await super().send(method, params, session_id, timeout)

    def on(self, event, handler, session_id=None) -> None:
        self.timeline.append(f"on:{event}")
        self.handlers.append((event, session_id, handler))

    def handler_for(self, event: str, session_id: str):
        return next(h for e, sid, h in self.handlers if e == event and sid == session_id)


def _root_only() -> FrameManager:
    frames = FrameManager()
    frames.update_from_frame_tree({"frame": {"id": "ROOT", "url": "http://host/"}})
    return frames


class TestAChildsContextsAreFiledUnderItsOwnSession:
    @pytest.mark.asyncio
    async def test_the_subscription_precedes_the_enable_that_replays_to_it(self):
        """`Runtime.enable` replays the contexts that already exist.

        Subscribing after it throws that replay away, which is how the page
        session lost every context before RFC-03's review found it. The
        child path is the same mistake in a new place.
        """
        client = TimelineClient()
        sessions = FrameSessions(client, _root_only(), "PAGE-SESSION", "PAGE-TARGET")
        sessions._on_attached(
            {
                "sessionId": "S1",
                "targetInfo": {"targetId": "T1", "type": "iframe", "url": "http://o/"},
            }
        )
        await sessions.settle(timeout=1.0)

        subscribed = client.timeline.index("on:Runtime.executionContextCreated")
        enabled = client.timeline.index("send:Runtime.enable")
        assert subscribed < enabled, client.timeline

    def test_the_handler_carries_its_own_session_not_the_default(self):
        """A bare handler defaults to the page session.

        The frame's own `execution_context_id` is set either way, so the
        mis-filing only shows when something reads the map back. A destroy
        does, and finds nothing.
        """
        frames = _page_with_child("S1")
        client = TimelineClient()
        sessions = FrameSessions(client, frames, "PAGE-SESSION", "PAGE-TARGET")
        sessions._subscribe_runtime("S1")

        client.handler_for("Runtime.executionContextCreated", "S1")(
            {"context": {"id": 1, "auxData": {"frameId": "CHILD", "isDefault": True}}}
        )
        client.handler_for("Runtime.executionContextDestroyed", "S1")(
            {"executionContextId": 1}
        )

        assert frames._frames["CHILD"].execution_context_id is None, (
            "the destroy could not find the context, so it was filed under "
            "the wrong session"
        )


class TestADestroyStopsAtTheFrameItActuallyOwns:
    def test_a_respliced_frame_keeps_the_context_its_new_session_gave_it(self):
        """The guard the session key alone does not provide.

        A frame can move between Frame Sessions - a renderer swap does it.
        Its old session's destroy still arrives, and the two sessions can
        have handed out the same context id, so the id matching is not
        enough to say the event is about this context.
        """
        frames = _page_with_child("S1")
        frames.handle_execution_context_created(
            {"context": {"id": 1, "auxData": {"frameId": "CHILD", "isDefault": True}}},
            session_id="S1",
        )
        # The frame moves to a new session, which hands out the same id.
        frames._frames["CHILD"].frame_session_id = "S2"
        frames.handle_execution_context_created(
            {"context": {"id": 1, "auxData": {"frameId": "CHILD", "isDefault": True}}},
            session_id="S2",
        )
        # The old session's destroy arrives late.
        frames.handle_execution_context_destroyed({"executionContextId": 1}, session_id="S1")

        assert frames._frames["CHILD"].execution_context_id == 1, (
            "a stale destroy from the frame's previous session cleared the "
            "context its current session gave it"
        )
