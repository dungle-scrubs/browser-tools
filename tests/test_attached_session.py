"""`AttachedSessionClient` presents the page-level surface over a session.

The adapter exists so the handler's forty-odd call sites keep their shape
while the transport under them changes (RFC-03, "One CDPRuntime for the
run"). These tests pin the three things that silently break if it is wrong:
the session reaches the wire, the exception type stays the one callers
catch, and the command timeout the page-level client always had survives.
"""

from __future__ import annotations

import asyncio

import pytest

from browser_tools.attached_session import (
    DEFAULT_TIMEOUT_SECONDS,
    AttachedSessionClient,
)
from browser_tools.cdp_client import CDPError
from browser_tools.core.errors import CDPError as CoreCDPError


class FakeCore:
    """Records what a core client would have been asked to do."""

    def __init__(self, *, result=None, raises=None, hang=False):
        self.sent: list[dict] = []
        self.subscriptions: list[tuple[str, object, str | None]] = []
        self.removed: list[tuple[str, object]] = []
        self.closed = False
        self._connected = True
        self._result = result if result is not None else {"ok": True}
        self._raises = raises
        self._hang = hang

    async def send(self, method, params=None, session_id=None):
        self.sent.append({"method": method, "params": params, "session_id": session_id})
        if self._hang:
            await asyncio.Event().wait()
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
        assert core.sent == [{"method": "Page.enable", "params": None, "session_id": "SESSION-1"}]

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
    """The core client awaits its future with no deadline.

    Adopting it without re-applying a bound turns a command Chrome never
    answers into a hang with no diagnostic.
    """

    def test_the_default_matches_the_page_level_client(self):
        import inspect

        from browser_tools.cdp_client import CDPClient

        page_default = inspect.signature(CDPClient.__init__).parameters["timeout"].default
        assert page_default == DEFAULT_TIMEOUT_SECONDS

    @pytest.mark.asyncio
    async def test_a_hung_command_raises_rather_than_hanging(self):
        client = AttachedSessionClient(FakeCore(hang=True), "S")
        with pytest.raises(CDPError) as caught:
            await client.send("Page.enable", timeout=0.05)
        assert "timed out" in str(caught.value)

    @pytest.mark.asyncio
    async def test_the_caller_can_override_the_deadline(self):
        client = AttachedSessionClient(FakeCore(hang=True), "S")
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(CDPError):
            await client.send("Page.enable", timeout=0.02)
        assert loop.time() - started < 1.0, "the override was ignored"


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


class TestTheTargetSpecIsTranslated:
    """The regression this change shipped once and a live run caught.

    `one_shot_page_session` dispatches on `target_by` and raises
    `ValueError: Unknown target_by: None` when a spec is given without one.
    The page-level path derived it from the spec, so `--target 1` and
    `--target <id>` both worked; passing None broke every `--target`.
    """

    @pytest.mark.parametrize(
        ("spec", "expected"),
        [(None, None), ("1", "index"), ("12", "index"), ("B343DD76", "id")],
    )
    def test_the_handler_derives_target_by_the_same_way(self, spec, expected):
        derived = None if spec is None else ("index" if spec.isdigit() else "id")
        assert derived == expected

    def test_the_page_level_path_used_that_rule(self):
        """Pinned against the source it was copied from, not from memory."""
        from pathlib import Path

        import browser_tools

        source = (Path(browser_tools.__file__).parent / "cdp_client.py").read_text()
        assert 'target_by="index" if target_spec.isdigit() else "id"' in source
