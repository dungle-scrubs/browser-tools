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

Neither family grew a second entry point, so there is nothing for a copy to
drift from. What does differ inside a run is the session's lifetime, and
that is what these tests pin.
"""

from __future__ import annotations

import asyncio

import pytest
from doubles import HandlerSurface

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


class FakeHandler(HandlerSurface):
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

    def test_a_failing_step_leaves_the_session_usable(self):
        """The path that matters: step 4 raises, step 5 still has a session.

        Checking only the clean exit passes just as well against a
        `_handler_for` that closes a borrowed handler when the body raises,
        which is exactly when a run must not lose it.
        """
        handler = FakeHandler()
        with pytest.raises(ValueError), curated._handler_for(handler, None, None, None, None):
            raise ValueError("the step failed")

        assert handler.stopped is False, "a failing step closed the run's session"
        assert curated.snapshot(instance=None, handler=handler)["snapshot"]

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
        """And forwards every argument the opener needs to pick the page."""
        import contextlib

        opened = []
        resolved = []
        monkeypatch.setattr(
            curated,
            "_resolve_port",
            lambda instance, registry_path, endpoint: resolved.append(
                (instance, registry_path, endpoint)
            )
            or 9222,
        )

        @contextlib.contextmanager
        def fake_session(port, target=None, *, external=False):
            opened.append((port, target, external))
            yield FakeHandler()

        monkeypatch.setattr(curated, "_cdp_handler_session", fake_session)
        with curated._handler_for(None, "inst-01", "3", "/reg.json", "http://x:9333"):
            pass

        assert resolved == [("inst-01", "/reg.json", "http://x:9333")]
        assert opened == [(9222, "3", True)], "target or external did not reach the opener"

    def test_an_endpointless_call_is_not_external(self, monkeypatch):
        import contextlib

        opened = []
        monkeypatch.setattr(curated, "_resolve_port", lambda *a, **k: 9222)

        @contextlib.contextmanager
        def fake_session(port, target=None, *, external=False):
            opened.append(external)
            yield FakeHandler()

        monkeypatch.setattr(curated, "_cdp_handler_session", fake_session)
        with curated._handler_for(None, "inst-01", None, None, None):
            pass
        assert opened == [False]

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
    async def test_a_blank_capture_is_retried_with_its_delay(self, monkeypatch):
        """Setting the delay to zero and counting sends proves neither.

        A capture that retried instantly would pass that, and instant retries
        are the thing the delay exists to avoid: the page has not repainted,
        so every attempt returns the same blank frame.
        """
        slept: list[float] = []

        async def record(seconds):
            slept.append(seconds)

        monkeypatch.setattr(curated, "screenshot_looks_blank", lambda _data: True)
        monkeypatch.setattr(curated.asyncio, "sleep", record)
        core = FakeCore({"Page.captureScreenshot": {"data": "blank"}})

        data = await curated.capture_on_session(core, "S1")

        attempts = curated.SCREENSHOT_BLANK_MAX_RETRIES + 1
        assert len(core.sent) == attempts
        assert slept == [curated.SCREENSHOT_BLANK_RETRY_DELAY_SECONDS] * (attempts - 1), (
            "the delay between attempts is wrong, or it waited after the last one"
        )
        assert data == "blank", "the final attempt's data must still be returned"

    @pytest.mark.asyncio
    async def test_a_good_capture_does_not_wait_at_all(self, monkeypatch):
        slept: list[float] = []

        async def record(seconds):
            slept.append(seconds)

        monkeypatch.setattr(curated, "screenshot_looks_blank", lambda _data: False)
        monkeypatch.setattr(curated.asyncio, "sleep", record)
        core = FakeCore({"Page.captureScreenshot": {"data": "good"}})

        assert await curated.capture_on_session(core, "S1") == "good"
        assert slept == []


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

    def test_a_timed_out_submission_is_cancelled_not_abandoned(self):
        """The wait ends; the work must not carry on over the session.

        An abandoned coroutine keeps sending on the session the next step is
        about to use, and the one teardown is about to close.
        """
        import threading

        from browser_tools.cdp_handler import CDPRuntime

        runtime = CDPRuntime(None)
        loop = asyncio.new_event_loop()
        runtime._loop = loop
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        started = threading.Event()
        cancelled = threading.Event()
        after_the_timeout: list[str] = []

        async def slow():
            started.set()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            after_the_timeout.append("kept going")

        try:
            with pytest.raises(TimeoutError):
                runtime.submit(slow(), timeout=0.1)
            assert started.wait(2), "the coroutine never started"
            assert cancelled.wait(2), "the coroutine was left running after the timeout"
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)
            loop.close()

        assert after_the_timeout == [], "work continued on the session after the wait ended"

    def test_submitting_from_the_runtimes_own_loop_is_refused(self):
        """It would have to run the coroutine and wait for it at once."""
        from browser_tools.cdp_handler import CDPRuntime

        runtime = CDPRuntime(None)

        async def body():
            return None

        async def inner():
            runtime._loop = asyncio.get_running_loop()
            with pytest.raises(RuntimeError, match="own loop"):
                runtime.submit(body(), timeout=0.2)

        asyncio.run(inner())

    def test_the_session_is_none_before_it_connects(self):
        from browser_tools.cdp_handler import CDPRuntime

        assert CDPRuntime(None).session is None

    def test_the_session_is_the_clients_pair_once_connected(self):
        from browser_tools.cdp_handler import CDPRuntime

        runtime = CDPRuntime(None)
        core = FakeCore()
        runtime._cdp_client = AttachedSessionClient(core, "S9")
        assert runtime.session == (core, "S9")


class TestAStepCleansUpAfterItselfOnTheSharedSession:
    """A leak that used to die with the process now outlives the step."""

    @pytest.mark.asyncio
    async def test_a_wait_unsubscribes_when_its_enable_fails(self):
        """`suppress(CDPError)` does not catch a dropped connection.

        The subscription is registered before the enable. If the enable
        raises anything but `CDPError`, the cleanup has to already be in
        scope, or the handler stays registered on a session later steps use
        and pushes into a queue nobody reads.
        """
        from browser_tools import events

        class Core:
            def __init__(self):
                self.handlers = {}

            def on(self, event, callback, session_id=None):
                self.handlers[event] = callback

            def off(self, event, callback):
                self.handlers.pop(event, None)

            async def send(self, **_kw):
                raise ConnectionError("socket gone")

        core = Core()
        with pytest.raises(ConnectionError):
            await events.wait_on_session(core, "S1", "Page.loadEventFired", None, 1)

        assert core.handlers == {}, "the subscription outlived the step that made it"
