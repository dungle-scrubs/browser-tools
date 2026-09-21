"""A session opened once can serve many steps, from either verb family.

A Step Run holds one CDP connection and one attached Target session for its
whole duration (RFC-03, "One CDPRuntime for the run"). Every verb today opens
its own, so the run needs a way to hand one in. Two families need it and they
reach the browser differently:

- The curated verbs run over a ``CDPHandler``. They take ``handler=`` and skip
  opening one.
- The One-Shot Session verbs are coroutines over ``(client, sessionId)``. The
  run gets those from the handler and submits the coroutine to the handler's
  loop, which lives on a background thread.

Neither family grew a second entry point. RFC-03 forbids that for the parser
and the preconditions, and the reason carries here: one implementation per
verb cannot drift from itself, so a step behaves inside a run as it does
outside one.
"""

from __future__ import annotations

import asyncio

import pytest

from browser_tools import curated, passthrough
from browser_tools.attached_session import AttachedSessionClient


class FakeCore:
    def __init__(self, results=None):
        self._connected = True
        self.sent: list[dict] = []
        self._results = results or {}

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.sent.append({"method": method, "params": params, "session_id": session_id})
        return self._results.get(method, {})

    def on(self, event, callback, session_id=None): ...
    def off(self, event, callback): ...


class FakeHandler:
    """Records that it was used and that nobody closed it."""

    def __init__(self):
        self.stopped = False
        self.calls: list[str] = []

    def stop(self):
        self.stopped = True

    def call_native(self, name, arguments):
        self.calls.append(name)
        from browser_tools.mcp_response import make_text

        return make_text("[uid=AB-1] RootWebArea")

    def call_tool(self, name, arguments):
        self.calls.append(name)
        from browser_tools.mcp_response import make_text

        return make_text("ok")


class TestTheAdapterExposesWhatItWraps:
    def test_raw_is_the_underlying_client(self):
        core = FakeCore()
        assert AttachedSessionClient(core, "S1").raw is core

    def test_the_session_id_travels_with_it(self):
        client = AttachedSessionClient(FakeCore(), "S1")
        assert (client.raw, client.session_id) == (client._client, "S1")


class TestAVerbRunsOnASessionItWasGiven:
    """`_handler_for` is the seam. Given a handler, it opens nothing."""

    def test_the_given_handler_is_used(self):
        handler = FakeHandler()
        with curated._handler_for(handler, None, None, None, None) as got:
            assert got is handler

    def test_the_given_handler_is_not_closed(self):
        """The run owns it, so the verb must leave it running for step N+1."""
        handler = FakeHandler()
        with curated._handler_for(handler, None, None, None, None):
            pass
        assert handler.stopped is False, "a verb closed a session it does not own"

    def test_no_connection_is_opened(self, monkeypatch):
        opened = []
        monkeypatch.setattr(
            curated,
            "_resolve_port",
            lambda *a, **k: opened.append(True) or 9222,
        )
        handler = FakeHandler()
        with curated._handler_for(handler, None, None, None, None):
            pass
        assert opened == [], "a port was resolved although a session was supplied"

    def test_without_one_it_opens_its_own(self, monkeypatch):
        import contextlib

        opened = []
        monkeypatch.setattr(curated, "_resolve_port", lambda *a, **k: 9222)

        @contextlib.contextmanager
        def fake_session(port, target=None, *, external=False):
            opened.append(port)
            yield FakeHandler()

        monkeypatch.setattr(curated, "_cdp_handler_session", fake_session)
        with curated._handler_for(None, None, None, None, None):
            pass
        assert opened == [9222]

    def test_a_curated_verb_takes_the_handler_through(self):
        handler = FakeHandler()
        result = curated.snapshot(instance=None, handler=handler)
        assert handler.calls == ["take_snapshot"]
        assert "RootWebArea" in result["snapshot"]
        assert handler.stopped is False


class TestTheOneShotVerbsRunOnASuppliedSession:
    @pytest.mark.asyncio
    async def test_send_carries_the_session(self):
        core = FakeCore()
        await passthrough.send_on_session(core, "S1", "Page.getFrameTree", None)
        assert core.sent == [
            {"method": "Page.getFrameTree", "params": None, "session_id": "S1"}
        ]

    @pytest.mark.asyncio
    async def test_an_input_method_is_refused_on_a_hidden_tab(self):
        """The guard travels with the send, so a step cannot reach around it."""
        core = FakeCore(
            {"Runtime.evaluate": {"result": {"value": "hidden"}}}
        )
        with pytest.raises(passthrough.HiddenTargetError):
            await passthrough.send_on_session(
                core, "S1", "Input.dispatchMouseEvent", {"type": "mousePressed"}
            )

    @pytest.mark.asyncio
    async def test_an_input_method_passes_on_a_visible_tab(self):
        core = FakeCore({"Runtime.evaluate": {"result": {"value": "visible"}}})
        await passthrough.send_on_session(
            core, "S1", "Input.dispatchMouseEvent", {"type": "mousePressed"}
        )
        assert core.sent[-1]["method"] == "Input.dispatchMouseEvent"

    @pytest.mark.asyncio
    async def test_a_capture_returns_the_data(self):
        core = FakeCore({"Page.captureScreenshot": {"data": "iVBORw0KGgo" * 40}})
        data = await curated.capture_on_session(core, "S1")
        assert data.startswith("iVBORw0KGgo")
        assert core.sent[0]["session_id"] == "S1"

    @pytest.mark.asyncio
    async def test_a_blank_capture_is_retried(self, monkeypatch):
        monkeypatch.setattr(curated, "screenshot_looks_blank", lambda _data: True)
        monkeypatch.setattr(curated, "SCREENSHOT_BLANK_RETRY_DELAY_SECONDS", 0)
        core = FakeCore({"Page.captureScreenshot": {"data": "blank"}})
        await curated.capture_on_session(core, "S1")
        assert len(core.sent) == curated.SCREENSHOT_BLANK_MAX_RETRIES + 1


class TestSubmittingToTheRuntimesLoop:
    """A caller on another thread cannot await the runtime's coroutines."""

    def test_a_coroutine_runs_on_the_loop_and_returns_its_result(self):
        import threading

        from browser_tools.cdp_handler import CDPRuntime

        runtime = CDPRuntime(None)
        loop = asyncio.new_event_loop()
        runtime._loop = loop
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        async def body():
            await asyncio.sleep(0)
            return asyncio.get_running_loop() is loop

        try:
            ran_on_the_loop = runtime.submit(body(), timeout=5)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)
            loop.close()

        assert ran_on_the_loop is True, "the coroutine did not run on the runtime's loop"

    def test_submitting_before_the_runtime_runs_is_an_error(self):
        from browser_tools.cdp_handler import CDPRuntime

        async def body():
            return None

        with pytest.raises(RuntimeError):
            CDPRuntime(None).submit(body())

    @pytest.mark.parametrize("loop_state", ["absent", "closed"])
    def test_a_coroutine_that_never_ran_is_closed(self, loop_state, recwarn):
        """Otherwise it warns on stderr while a run reports why it failed.

        `run_coroutine_threadsafe` takes ownership only when it schedules.
        On either failure path nothing awaits the coroutine, and Python says
        so at collection time, in the middle of the run's diagnostics.
        """
        import gc

        from browser_tools.cdp_handler import CDPRuntime

        runtime = CDPRuntime(None)
        if loop_state == "closed":
            loop = asyncio.new_event_loop()
            loop.close()
            runtime._loop = loop

        async def body():
            return None

        coro = body()
        with pytest.raises(RuntimeError):
            runtime.submit(coro)

        assert coro.cr_frame is None, "the coroutine was left open"
        del coro
        gc.collect()
        assert not [
            w for w in recwarn.list if "never awaited" in str(w.message)
        ], "an un-awaited coroutine warning reached the caller"

    def test_the_session_is_none_before_it_connects(self):
        from browser_tools.cdp_handler import CDPRuntime

        assert CDPRuntime(None).session is None

    def test_the_session_is_the_clients_pair_once_connected(self):
        from browser_tools.cdp_handler import CDPRuntime

        runtime = CDPRuntime(None)
        core = FakeCore()
        runtime._cdp_client = AttachedSessionClient(core, "S9")
        assert runtime.session == (core, "S9")
