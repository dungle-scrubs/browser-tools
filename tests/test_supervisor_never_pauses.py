"""The supervisor may never hold a document paused, or sit stuck while attached.

The failure this pins down: the supervisor is a permanently-attached CDP client
of a browser a human is using, so anything it does to a target it must also be
able to undo. A client that auto-attaches with ``waitForDebuggerOnStart`` and
then stops resuming wedges every document created after it stalled -- new tabs
sit on Chrome's "Debugger paused in another tab" banner and cross-origin iframes
stay at ``readyState === "loading"`` with no requests issued, forever, because a
reload only makes another paused document. The browser never recovers on its
own; a human loses the session by restarting it.

Three properties keep the supervisor out of that state, and each is pinned here
against a fake CDP client:

1. It finds targets by discovery, never by auto-attach (Chrome holds a popup's
   creation on an auto-attaching client's handshake), and resumes every session
   it attaches before doing any marking work.
2. It attaches to page targets only -- an iframe (OOPIF), worker, or extension
   target is none of its business, and anything else that reaches it is
   released, not held.
3. Its watchdog exits the process when the event loop stalls, and does NOT
   exit when the whole process was frozen (host suspend, SIGSTOP) -- which
   says nothing about the loop.

``tests/test_supervisor_document_liveness.py`` is the same guarantee against a
real browser.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import textwrap
import threading

import pytest

from browser_tools.core import supervisor


class FakeCDP:
    """Records CDP sends and lets a test fail a chosen method."""

    def __init__(self, *, fail: set[str] | None = None, reject_filter: bool = False) -> None:
        self.sent: list[tuple[str, dict | None, str | None]] = []
        self._fail = fail or set()
        self._reject_filter = reject_filter
        self._connected = True

    async def send(
        self, method: str, params: dict | None = None, session_id: str | None = None
    ) -> dict:
        self.sent.append((method, params, session_id))
        if self._reject_filter and params and "filter" in params:
            raise RuntimeError("filter: unsupported")
        if method in self._fail:
            raise RuntimeError(f"{method} failed")
        if method == "Target.attachToTarget":
            return {"sessionId": "SESSION"}
        return {}

    def methods(self, *, session_id: str | None = None) -> list[str]:
        return [m for m, _, sid in self.sent if session_id is None or sid == session_id]


class TestTargetsAreWatchedNotIntercepted:
    def test_targets_are_found_by_discovery(self) -> None:
        """Auto-attach would make Chrome hold every popup's creation on this
        process answering; discovery is a notification Chrome waits on for
        nothing."""
        cdp = FakeCDP()
        asyncio.run(supervisor._enable_page_discovery(cdp))

        assert cdp.methods() == ["Target.setDiscoverTargets"]
        assert "Target.setAutoAttach" not in cdp.methods()

    def test_discovery_asks_for_page_targets_only(self) -> None:
        """An OOPIF, a worker, or an extension page is never marked, so the
        supervisor has no reason to attach to one."""
        cdp = FakeCDP()
        asyncio.run(supervisor._enable_page_discovery(cdp))

        _, params, _ = cdp.sent[0]
        assert params is not None
        assert params["filter"] == [{"type": "page", "exclude": False}, {"exclude": True}]

    def test_a_browser_without_filter_support_still_only_discovers(self) -> None:
        """Chrome below 106 rejects ``filter``; the retry drops it and the
        handler filters instead. Discovery is still all that is asked for."""
        cdp = FakeCDP(reject_filter=True)
        asyncio.run(supervisor._enable_page_discovery(cdp))

        assert cdp.methods() == ["Target.setDiscoverTargets", "Target.setDiscoverTargets"]
        _, retry, _ = cdp.sent[1]
        assert retry is not None
        assert "filter" not in retry
        assert retry["discover"] is True


def _setup(cdp: FakeCDP) -> None:
    """Run the per-session marking setup with the border setting on."""
    scripts = supervisor.MarkerScripts.for_instance("t-01")
    mark = supervisor.TabMark(session_id="S1")
    border = supervisor.BorderSetting(lambda: True)
    asyncio.run(supervisor._setup_session(cdp, "S1", scripts, mark, border))


class TestEverySessionIsResumed:
    def test_resume_comes_before_any_marking_work(self) -> None:
        cdp = FakeCDP()
        _setup(cdp)

        methods = cdp.methods(session_id="S1")
        assert methods[0] == "Runtime.runIfWaitingForDebugger"
        assert "Page.addScriptToEvaluateOnNewDocument" in methods
        assert methods.index("Runtime.runIfWaitingForDebugger") < methods.index("Page.enable")

    @pytest.mark.parametrize(
        "failing", ["Page.enable", "Page.addScriptToEvaluateOnNewDocument", "Runtime.evaluate"]
    )
    def test_marking_that_blows_up_still_leaves_the_target_resumed(self, failing: str) -> None:
        """Marking is best-effort; a target left paused is not."""
        cdp = FakeCDP(fail={failing})
        _setup(cdp)

        assert cdp.methods(session_id="S1").count("Runtime.runIfWaitingForDebugger") == 2

    def test_a_resume_that_fails_does_not_stop_the_marking(self) -> None:
        """The tab may have closed; the guard keeps working for other tabs."""
        cdp = FakeCDP(fail={"Runtime.runIfWaitingForDebugger"})
        _setup(cdp)

        assert "Page.addScriptToEvaluateOnNewDocument" in cdp.methods(session_id="S1")

    def test_a_non_page_target_is_resumed_and_let_go(self) -> None:
        """Reached only when the auto-attach filter did not hold. Nothing is
        marked, and nothing is kept -- an attached session is the only thing
        that can hold a paused target paused."""
        cdp = FakeCDP()
        asyncio.run(supervisor._release_target(cdp, "S2"))

        assert cdp.methods() == ["Runtime.runIfWaitingForDebugger", "Target.detachFromTarget"]
        assert cdp.sent[1][1] == {"sessionId": "S2"}


class TestAttachRouting:
    """The target handlers are built inside ``_supervise_connection``; drive them
    through the real function with a fake client and fake target events."""

    def _events(self, event_name: str, events: list[dict], *, monkeypatch) -> FakeCDP:
        cdp = FakeCDP()
        handlers: dict[str, list] = {}

        def fake_on(*, event, callback, session_id=None):
            handlers.setdefault(event, []).append(callback)

        cdp.on = fake_on  # type: ignore[attr-defined]
        cdp.attach_session = "SESSION"  # returned for Target.attachToTarget

        async def fake_connect():
            return None

        async def fake_close():
            return None

        cdp.connect = fake_connect  # type: ignore[attr-defined]
        cdp.close = fake_close  # type: ignore[attr-defined]

        monkeypatch.setattr(supervisor, "get_ws_url", lambda **kwargs: "ws://fake")
        monkeypatch.setattr(supervisor, "CDPClient", lambda ws_url: cdp)

        async def drive():
            task = asyncio.get_event_loop().create_task(
                supervisor._supervise_connection(
                    port=1, scripts=supervisor.MarkerScripts.for_instance("t-01")
                )
            )
            await asyncio.sleep(0)  # let the setup run up to the supervise loop
            for event in events:
                for callback in handlers[event_name]:
                    callback(event)
            cdp._connected = False
            await asyncio.sleep(0.05)  # let the spawned per-session work finish
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(drive())
        return cdp

    def test_a_discovered_page_is_attached_resumed_then_marked(self, monkeypatch) -> None:
        cdp = self._events(
            "Target.targetCreated",
            [{"targetInfo": {"targetId": "T1", "type": "page"}}],
            monkeypatch=monkeypatch,
        )

        # [0] is the discovery setup this fake connection made on the way in.
        assert cdp.methods()[1] == "Target.attachToTarget"
        session_methods = cdp.methods(session_id="SESSION")
        assert session_methods[0] == "Runtime.runIfWaitingForDebugger"
        assert "Page.addScriptToEvaluateOnNewDocument" in session_methods

    def test_a_discovered_iframe_is_not_attached_at_all(self, monkeypatch) -> None:
        """The wedged document in the field report was an OOPIF. Marking is a
        top-document job, so the supervisor never opens a session on one."""
        cdp = self._events(
            "Target.targetCreated",
            [{"targetInfo": {"targetId": "F1", "type": "iframe"}}],
            monkeypatch=monkeypatch,
        )

        assert cdp.methods() == ["Target.setDiscoverTargets"]  # discovery only

    def test_a_session_handed_over_anyway_is_released(self, monkeypatch) -> None:
        """Belt and braces: a non-page session that arrives regardless (an old
        browser that ignored the filter) is resumed and dropped, never held."""
        cdp = self._events(
            "Target.attachedToTarget",
            [{"sessionId": "F1", "targetInfo": {"type": "iframe"}}],
            monkeypatch=monkeypatch,
        )

        assert cdp.methods(session_id="F1") == ["Runtime.runIfWaitingForDebugger"]
        assert ("Target.detachFromTarget", {"sessionId": "F1"}, None) in cdp.sent
        assert "Page.addScriptToEvaluateOnNewDocument" not in cdp.methods()

    def test_a_repeated_notice_is_not_set_up_twice(self, monkeypatch) -> None:
        event = {"targetInfo": {"targetId": "T1", "type": "page"}}
        cdp = self._events("Target.targetCreated", [event, event], monkeypatch=monkeypatch)

        assert cdp.methods().count("Page.enable") == 1


class TestWatchdog:
    def test_a_stale_heartbeat_is_a_stall(self) -> None:
        heartbeat = supervisor.Heartbeat()
        heartbeat._last -= 60.0

        assert supervisor._round_says_stalled(
            heartbeat=heartbeat, overshoot=0.0, poll=1.0, timeout=30.0
        )

    def test_a_beating_heartbeat_is_not_a_stall(self) -> None:
        heartbeat = supervisor.Heartbeat()

        assert not supervisor._round_says_stalled(
            heartbeat=heartbeat, overshoot=0.0, poll=1.0, timeout=30.0
        )

    def test_a_frozen_process_is_not_a_stall(self) -> None:
        """A host suspend or a SIGSTOP freezes the watchdog thread too, so the
        old heartbeat says nothing about the event loop. The gap is charged to
        the machine and the loop gets a fresh window."""
        heartbeat = supervisor.Heartbeat()
        heartbeat._last -= 600.0

        assert not supervisor._round_says_stalled(
            heartbeat=heartbeat, overshoot=599.0, poll=1.0, timeout=30.0
        )
        assert heartbeat.age() < 1.0  # beaten, so the next round judges afresh

    def test_the_watchdog_thread_fires_on_a_stalled_loop(self) -> None:
        fired = threading.Event()
        heartbeat = supervisor.Heartbeat()
        heartbeat._last -= 10.0
        supervisor.start_watchdog(
            heartbeat=heartbeat, timeout=1.0, poll=0.05, on_stall=fired.set
        )

        assert fired.wait(timeout=5)

    def test_a_stalled_supervisor_exits_the_process(self) -> None:
        """The whole point: a dead client releases every target it held, so a
        supervisor that cannot act must not keep its connection. Run it for
        real -- ``os._exit`` is not observable in-process."""
        script = textwrap.dedent(
            """
            import asyncio, time
            from browser_tools.core import supervisor

            async def main():
                heartbeat = supervisor.Heartbeat()
                supervisor.start_watchdog(heartbeat=heartbeat, timeout=1.0, poll=0.2)
                asyncio.get_event_loop().create_task(supervisor._beat_forever(heartbeat))
                await asyncio.sleep(0.2)
                time.sleep(20)  # block the event loop; threads keep running

            asyncio.run(main())
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=20
        )

        assert result.returncode == supervisor.EXIT_EVENT_LOOP_STALLED
        assert "event loop stalled" in result.stderr
