"""What `CDPRuntime` asks the One-Shot Session for, and what it does when it cannot get it.

Every test here pins a finding from the cross-family review of the transport
change. The three that matter are:

- A missing target spec used to mean "the first page". The One-Shot Session
  reads it as "ask the user which one", which broke every handler-routed verb
  as soon as a second tab was open.
- A browser that is not there is an outcome, not a defect, and must not print
  a traceback.
- The caller polling `available` should learn why, not wait out its deadline
  and guess.

These drive the real `CDPRuntime` against an instrumented seam. The earlier
version of the target test recomputed the translation expression in the test
body and so would have passed against any implementation at all.
"""

from __future__ import annotations

import contextlib
import logging

import pytest

from browser_tools.cdp_handler import CDPRuntime
from browser_tools.core.attach import AmbiguousTargetError, TargetNotFoundError
from browser_tools.core.errors import NoPageError


class RecordingSeam:
    """Stands in for `one_shot_page_session` and records its arguments."""

    def __init__(self, *, raises: Exception | None = None):
        self.calls: list[tuple[int, str | None, str | None]] = []
        self._raises = raises

    @contextlib.asynccontextmanager
    async def __call__(self, port, target_spec, target_by):
        self.calls.append((port, target_spec, target_by))
        if self._raises is not None:
            raise self._raises
        yield FakeCore(), "SESSION-1"


class FakeCore:
    """Answers the five setup commands `_connect_cdp` sends."""

    def __init__(self) -> None:
        self._connected = True
        self.subscribed: list[str] = []
        #: Every command and subscription in the order it happened, so a test
        #: can assert that a subscription preceded the enable that replays to
        #: it rather than only that both occurred.
        self.timeline: list[str] = []

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.timeline.append(method)
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F1", "url": "about:blank"}}}
        return {}

    def on(self, event, callback, session_id=None):
        self.subscribed.append(event)
        self.timeline.append(f"on:{event}")

    def off(self, event, callback): ...


@pytest.fixture
def seam(monkeypatch):
    recorder = RecordingSeam()
    monkeypatch.setattr("browser_tools.one_shot.one_shot_page_session", recorder)
    return recorder


async def _connect(runtime: CDPRuntime) -> None:
    """Run the real `_connect_cdp` with a real exit stack under it."""
    import contextlib as _contextlib

    from browser_tools.frame_manager import FrameManager

    runtime._frame_manager = FrameManager()
    runtime._sessions = _contextlib.AsyncExitStack()
    try:
        await runtime._connect_cdp()
    finally:
        with _contextlib.suppress(Exception):
            await runtime._sessions.aclose()


class TestTheTargetSpecReachesTheSeamTranslated:
    """The seam rejects a spec with no `target_by`, so the runtime derives one."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            (None, ("1", "index")),
            ("1", ("1", "index")),
            ("12", ("12", "index")),
            ("B343DD76", ("B343DD76", "id")),
        ],
    )
    async def test_the_seam_is_called_with(self, seam, spec, expected):
        runtime = CDPRuntime("http://127.0.0.1:9222", target_spec=spec)
        await _connect(runtime)
        assert seam.calls == [(9222, *expected)]

    @pytest.mark.asyncio
    async def test_no_spec_names_the_first_page_rather_than_no_page(self, seam):
        """The regression this test exists for.

        `resolve_page_ws_url` answered a missing spec with `pages[0]`.
        `resolve_target` answers it with `AmbiguousTargetError` unless exactly
        one page exists. Handing the absence straight through would break
        `snapshot`, `frames`, `storage`, `detect` and the waits the moment a
        popup or a second tab exists, and most of those have no `--target`.
        """
        runtime = CDPRuntime("http://127.0.0.1:9222", target_spec=None)
        await _connect(runtime)
        _port, spec, by = seam.calls[0]
        assert (spec, by) == ("1", "index"), "a missing spec must not travel as None"


class TestAnOrdinaryFailureReadsLikeOne:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure",
        [
            ConnectionError("Cannot reach browser on port 9222"),
            AmbiguousTargetError(targets=[{"targetId": "A" * 32, "url": "a", "title": "a"}]),
            TargetNotFoundError("Index 99 out of range (1-1)", targets=[]),
            NoPageError(),
        ],
        ids=["no browser", "ambiguous", "not found", "no pages at all"],
    )
    async def test_no_traceback_is_logged(self, monkeypatch, caplog, failure):
        monkeypatch.setattr(
            "browser_tools.one_shot.one_shot_page_session", RecordingSeam(raises=failure)
        )
        runtime = CDPRuntime("http://127.0.0.1:9222")
        with caplog.at_level(logging.ERROR):
            await _connect(runtime)
        assert not any(r.exc_info for r in caplog.records), "an outcome was logged as a defect"

    @pytest.mark.asyncio
    async def test_a_browser_with_no_tabs_is_the_ordinary_state_of_a_fresh_launch(
        self, monkeypatch, caplog
    ):
        """The regression this test exists for.

        A headless shell starts with no tab at all, so zero page targets is
        what a freshly launched instance looks like, not a defect. The comment
        above `_EXPECTED_CONNECT_FAILURES` already called a target spec naming
        no page an ordinary outcome, but `NoPageError` was absent from the
        tuple. Every verb against such an instance therefore printed a
        twenty-line Python traceback and then the correct one-line diagnostic
        underneath it, which reads as a crash.
        """
        monkeypatch.setattr(
            "browser_tools.one_shot.one_shot_page_session", RecordingSeam(raises=NoPageError())
        )
        runtime = CDPRuntime("http://127.0.0.1:9222")
        with caplog.at_level(logging.ERROR):
            await _connect(runtime)
        assert not any(r.exc_info for r in caplog.records), "a fresh launch logged as a defect"
        assert runtime.available is False
        assert "no open pages" in (runtime.connect_error or "")

    @pytest.mark.asyncio
    async def test_the_reason_is_recorded_for_the_caller(self, monkeypatch):
        monkeypatch.setattr(
            "browser_tools.one_shot.one_shot_page_session",
            RecordingSeam(raises=ConnectionError("Cannot reach browser on port 9222")),
        )
        runtime = CDPRuntime("http://127.0.0.1:9222")
        await _connect(runtime)
        assert runtime.available is False
        assert "Cannot reach browser" in (runtime.connect_error or "")

    @pytest.mark.asyncio
    async def test_an_unexpected_failure_keeps_its_traceback(self, monkeypatch, caplog):
        monkeypatch.setattr(
            "browser_tools.one_shot.one_shot_page_session",
            RecordingSeam(raises=RuntimeError("something nobody predicted")),
        )
        runtime = CDPRuntime("http://127.0.0.1:9222")
        with caplog.at_level(logging.ERROR):
            await _connect(runtime)
        assert any(r.exc_info for r in caplog.records), "a defect was logged as an outcome"

    @pytest.mark.asyncio
    async def test_nothing_is_recorded_on_success(self, seam):
        runtime = CDPRuntime("http://127.0.0.1:9222")
        await _connect(runtime)
        assert runtime.connect_error is None
        assert runtime.available is True


class TestTheFrameEventsAreStillWired:
    """The subscriptions must survive every change above.

    Seven now, not five. `Page.navigatedWithinDocument` because a single-page
    app changes its URL without loading a document, and
    `Runtime.executionContextsCleared` because without it every frame keeps a
    context id that no longer exists.
    """

    @pytest.mark.asyncio
    async def test_every_frame_event_is_registered(self, seam):
        runtime = CDPRuntime("http://127.0.0.1:9222")
        await _connect(runtime)
        assert set(runtime.client._client.subscribed) == {
            "Page.frameAttached",
            "Page.frameDetached",
            "Page.frameNavigated",
            "Page.navigatedWithinDocument",
            "Runtime.executionContextCreated",
            "Runtime.executionContextDestroyed",
            "Runtime.executionContextsCleared",
        }

    @pytest.mark.asyncio
    async def test_the_runtime_replay_lands_on_a_registered_handler(self, seam):
        """`Runtime.enable` replays the contexts that already exist.

        Enabling before subscribing threw that replay away, so every frame
        kept `execution_context_id = None` and `storage get` returned cookies
        only, with no localStorage or sessionStorage, and exit 0.
        """
        runtime = CDPRuntime("http://127.0.0.1:9222")
        await _connect(runtime)
        timeline = runtime.client._client.timeline
        assert timeline.index("on:Runtime.executionContextCreated") < timeline.index(
            "Runtime.enable"
        ), (
            "Runtime was enabled before its context handler was registered, so "
            "the replay went nowhere and every frame kept no context id"
        )
        assert timeline.index("Page.getFrameTree") < timeline.index("Runtime.enable"), (
            "Runtime was enabled before the frame tree existed, so the replayed "
            "contexts had no frame to be filed against"
        )
        assert timeline.index("on:Page.frameNavigated") < timeline.index("Page.enable"), (
            "Page was enabled before its handlers were registered"
        )
