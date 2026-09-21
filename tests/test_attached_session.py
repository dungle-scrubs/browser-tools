"""`AttachedSessionClient` presents the page-level surface over a session.

The adapter exists so the handler's forty-odd call sites keep their shape
while the transport under them changes (RFC-03, "One CDPRuntime for the
run"). These tests pin the three things that silently break if it is wrong:
the session reaches the wire, the exception type stays the one callers
catch, and the command timeout the page-level client always had survives.
"""

from __future__ import annotations

import pytest

from browser_tools.attached_session import (
    DEFAULT_TIMEOUT_SECONDS,
    AttachedSessionClient,
)
from browser_tools.cdp_client import CDPError
from browser_tools.core.errors import CDPError as CoreCDPError


class FakeCore:
    """Records what a core client would have been asked to do."""

    def __init__(self, *, result=None, raises=None):
        self.sent: list[dict] = []
        self.subscriptions: list[tuple[str, object, str | None]] = []
        self.removed: list[tuple[str, object]] = []
        self.closed = False
        self._connected = True
        self._result = result if result is not None else {"ok": True}
        self._raises = raises

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.sent.append(
            {
                "method": method,
                "params": params,
                "session_id": session_id,
                "timeout": timeout,
            }
        )
        if self._raises is not None:
            raise self._raises
        return self._result

    def on(self, event, callback, session_id=None):
        self.subscriptions.append((event, callback, session_id))

    def off(self, event, callback):
        self.removed.append((event, callback))

    async def close(self):
        self.closed = True


@pytest.fixture
def core() -> FakeCore:
    return FakeCore()


@pytest.fixture
def client(core: FakeCore) -> AttachedSessionClient:
    return AttachedSessionClient(core, "SESSION-1")


class TestEverySendCarriesTheSession:
    """A send without the session goes to the browser, not the page."""

    @pytest.mark.asyncio
    async def test_the_session_id_is_attached(self, client, core):
        await client.send("Page.enable")
        assert core.sent[0]["session_id"] == "SESSION-1"

    @pytest.mark.asyncio
    async def test_params_pass_through_unchanged(self, client, core):
        await client.send("Page.navigate", {"url": "https://example.com"})
        assert core.sent[0]["params"] == {"url": "https://example.com"}

    @pytest.mark.asyncio
    async def test_the_result_is_returned_as_is(self, core):
        client = AttachedSessionClient(FakeCore(result={"frameTree": {}}), "S")
        assert await client.send("Page.getFrameTree") == {"frameTree": {}}

    def test_the_session_id_is_readable(self, client):
        assert client.session_id == "SESSION-1"


class TestTheExceptionTypeCallersCatch:
    """`cdp_handler` and `screencast` both catch `cdp_client.CDPError`.

    The core client raises an unrelated `core.errors.CDPError` and a bare
    `ConnectionError`. Either would escape every existing handler.
    """

    @pytest.mark.asyncio
    async def test_a_core_protocol_error_becomes_the_callers_error(self):
        core = FakeCore(raises=CoreCDPError(code=-32000, message="No node"))
        client = AttachedSessionClient(core, "S")
        with pytest.raises(CDPError) as caught:
            await client.send("DOM.querySelector")
        assert "No node" in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_lost_connection_becomes_the_callers_error(self):
        core = FakeCore(raises=ConnectionError("socket gone"))
        client = AttachedSessionClient(core, "S")
        with pytest.raises(CDPError):
            await client.send("Page.enable")

    @pytest.mark.asyncio
    async def test_sending_after_disconnect_is_refused(self, client):
        await client.disconnect()
        with pytest.raises(CDPError):
            await client.send("Page.enable")


class TestTheCommandTimeoutSurvives:
    """The core client defaults to no deadline.

    Adopting it without asking for a bound turns a command Chrome never
    answers into a hang with no diagnostic. The bound is asked for on the
    call rather than wrapped around it: only the core client's `send` can
    retire the pending entry when the wait ends early. What a wrapper would
    have cost is in `tests/test_core_send_timeout.py`.
    """

    def test_the_default_matches_the_page_level_client(self):
        import inspect

        from browser_tools.cdp_client import CDPClient

        page_default = inspect.signature(CDPClient.__init__).parameters["timeout"].default
        assert page_default == DEFAULT_TIMEOUT_SECONDS

    @pytest.mark.asyncio
    async def test_every_command_asks_for_the_bound(self, client, core):
        await client.send("Page.enable")
        assert core.sent[0]["timeout"] == DEFAULT_TIMEOUT_SECONDS

    @pytest.mark.asyncio
    async def test_the_caller_can_override_the_deadline(self, client, core):
        await client.send("Page.enable", timeout=2.5)
        assert core.sent[0]["timeout"] == 2.5

    @pytest.mark.asyncio
    async def test_a_timeout_reaches_the_caller_as_their_own_error(self):
        client = AttachedSessionClient(FakeCore(raises=TimeoutError()), "S")
        with pytest.raises(CDPError) as caught:
            await client.send("Page.enable", timeout=0.05)
        assert "timed out" in str(caught.value)


class TestSubscriptionsAreScopedToTheSession:
    """A browser-level connection sees every attached target's events."""

    def test_on_registers_with_the_session_filter(self, client, core):
        def handler(_params): ...

        client.on("Page.frameNavigated", handler)
        assert core.subscriptions == [("Page.frameNavigated", handler, "SESSION-1")]

    def test_off_removes_the_handler(self, client, core):
        def handler(_params): ...

        client.off("Page.frameNavigated", handler)
        assert core.removed == [("Page.frameNavigated", handler)]


class TestLifetimeBelongsToTheSeam:
    """`one_shot_page_session` opened the connection and detaches it."""

    @pytest.mark.asyncio
    async def test_disconnect_does_not_close_the_connection(self, client, core):
        await client.disconnect()
        assert core.closed is False, "the adapter closed a socket it does not own"

    def test_connected_tracks_the_underlying_client(self, client, core):
        assert client.connected is True
        core._connected = False
        assert client.connected is False

    @pytest.mark.asyncio
    async def test_connected_is_false_after_disconnect(self, client):
        await client.disconnect()
        assert client.connected is False


class TestOffMatchesTheWayCallersExpect:
    """The core client matches by identity; the page-level client did not.

    `ScreencastRecorder.start` subscribes with `self.on_frame` and `stop`
    unsubscribes with `self.on_frame`. Those are two bound-method objects:
    equal, never identical. Under identity matching the subscription would
    survive its own removal.
    """

    def test_a_bound_method_is_equal_but_not_identical(self):
        class Recorder:
            def on_frame(self, params): ...

        recorder = Recorder()
        assert recorder.on_frame == recorder.on_frame
        assert recorder.on_frame is not recorder.on_frame

    def test_off_removes_a_freshly_bound_method(self, client, core):
        class Recorder:
            def on_frame(self, params): ...

        recorder = Recorder()
        client.on("Page.screencastFrame", recorder.on_frame)
        client.off("Page.screencastFrame", recorder.on_frame)

        assert core.removed, "off passed nothing to the core client"
        removed_event, removed_handler = core.removed[0]
        registered_handler = core.subscriptions[0][1]
        assert removed_event == "Page.screencastFrame"
        assert removed_handler is registered_handler, (
            "off handed the core client a different object than on did, "
            "so identity matching would not remove it"
        )

    def test_a_handler_never_registered_is_passed_through(self, client, core):
        def stranger(_params): ...

        client.off("Page.frameNavigated", stranger)
        assert core.removed == [("Page.frameNavigated", stranger)]

    def test_the_same_handler_can_be_removed_once_per_registration(self, client, core):
        def handler(_params): ...

        client.on("Page.loadEventFired", handler)
        client.on("Page.loadEventFired", handler)
        client.off("Page.loadEventFired", handler)
        client.off("Page.loadEventFired", handler)
        assert len(core.removed) == 2


# What the runtime asks the One-Shot Session for, including how a missing
# target spec is translated, is pinned in tests/test_handler_transport.py
# against the real `CDPRuntime`. An earlier version of that check lived here
# and recomputed the expression in the test body, so it would have passed
# against any implementation.
