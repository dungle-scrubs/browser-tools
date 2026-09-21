"""What has to be true before an invocation is allowed to read the frame map.

Every defect pinned here was found the same way: a page holding twenty
sibling cross-origin iframes, and ten identical `frames list --frames all`
invocations against it. They answered 0, 8, 12 and 20 out-of-process frames.
Discovery was not the problem - the Frame Sessions were all attached, ready
and spliced every single time. The read simply started before they were.

    `available` is what the caller polls to decide the invocation may start,
    and `_connect_cdp` assigns `_cdp_client` as its first statement. Between
    that assignment and the end of setup sit `Page.enable`, the frame tree,
    `Runtime.enable` and Frame Sessions, and the poll sleeps 20 ms. Whatever
    the map held when the read won the race was the answer.

It is not an RFC-04 defect. Every verb routed through a `CDPHandler` has
always been able to read a half-built frame map; twenty Out-of-Process
Frames only made the window wide enough to lose reliably.

The rest pin what reading the code turned up on the way, each of which cost
information rather than correctness: a failure that erased its own
diagnostic, a splice order that left deep frames unattached, and a depth
bound that could never fire.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from test_handler_transport import FakeCore, RecordingSeam, _connect

from browser_tools.cdp_handler import CDPRuntime
from browser_tools.frame_manager import FrameManager
from browser_tools.frame_sessions import MAX_DEPTH, FrameSessions


@pytest.fixture
def recording_seam(monkeypatch):
    """The `test_handler_transport` fixture, which pytest does not share."""
    recorder = RecordingSeam()
    monkeypatch.setattr("browser_tools.one_shot.one_shot_page_session", recorder)
    return recorder


class ReadinessWatchingCore(FakeCore):
    """`FakeCore`, plus whether the runtime called itself ready yet."""

    def __init__(self, runtime: CDPRuntime) -> None:
        super().__init__()
        self._runtime = runtime
        #: One entry per setup command: the method, and whether the caller
        #: would have been allowed to start by then.
        self.readiness: list[tuple[str, bool]] = []

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.readiness.append((method, self._runtime._ready.is_set()))
        return await super().send(method, params, session_id, timeout)


class TestTheInvocationWaitsForItsOwnSetup:
    """`available` means setup finished, not that a socket opened."""

    @pytest.mark.asyncio
    async def test_no_setup_command_runs_after_the_caller_may_start(self, monkeypatch):
        runtime = CDPRuntime("http://127.0.0.1:9222")
        core = ReadinessWatchingCore(runtime)

        @contextlib.asynccontextmanager
        async def seam(port, target_spec, target_by):
            yield core, "SESSION-1"

        monkeypatch.setattr("browser_tools.one_shot.one_shot_page_session", seam)
        await _connect(runtime)

        assert core.readiness, "setup sent no commands, so this proves nothing"
        assert [m for m, ready in core.readiness if ready] == []

    @pytest.mark.asyncio
    async def test_frame_session_discovery_is_setup_too(self, monkeypatch):
        """The wide case, and the one that made the race visible.

        `--frames all` adds four more commands after `Runtime.enable`, and
        they are the ones the twenty-iframe page was still running when the
        read started. A readiness check that stops before them passes while
        the defect is fully present.
        """
        runtime = CDPRuntime("http://127.0.0.1:9222", all_frames=True)
        core = ReadinessWatchingCore(runtime)

        @contextlib.asynccontextmanager
        async def seam(port, target_spec, target_by):
            yield core, "SESSION-1"

        monkeypatch.setattr("browser_tools.one_shot.one_shot_page_session", seam)
        await _connect(runtime)

        assert "Target.setAutoAttach" in [m for m, _ in core.readiness]
        assert [m for m, ready in core.readiness if ready] == []

    @pytest.mark.asyncio
    async def test_it_is_ready_once_setup_is_over(self, recording_seam):
        runtime = CDPRuntime("http://127.0.0.1:9222")
        await _connect(runtime)
        assert runtime.available is True

    def test_a_connected_client_alone_is_not_available(self):
        """The exact state the race read from: connected, setup unfinished."""
        runtime = CDPRuntime("http://127.0.0.1:9222")
        runtime._cdp_client = _ConnectedClient()
        assert runtime._ready.is_set() is False
        assert runtime.available is False
        runtime._ready.set()
        assert runtime.available is True


class _ConnectedClient:
    connected = True


# ---------------------------------------------------------------- discovery


class ScriptedClient:
    """A CDP client whose per-method answers a test writes.

    Records every command as `(method, session_id)` so a test can assert
    which session a command went to, which is the whole difficulty in this
    module: the same method means different things on the page session and
    on a Frame Session.
    """

    connected = True

    def __init__(self, answers: dict[str, Any] | None = None, fail: set[str] | None = None):
        self.answers = answers or {}
        self.fail = fail or set()
        self.sent: list[tuple[str, str | None]] = []
        self.handlers: dict[str, list[Any]] = {}

    async def send(self, method: str, params: Any = None, session_id: str | None = None):
        self.sent.append((method, session_id))
        if method in self.fail:
            raise RuntimeError(f"{method} refused")
        answer = self.answers.get(method)
        if callable(answer):
            return answer(session_id)
        return answer or {}

    def on(self, event: str, handler: Any, session_id: str | None = None) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def off(self, event: str, handler: Any) -> None: ...


def _page_map() -> FrameManager:
    frames = FrameManager()
    frames.update_from_frame_tree({"frame": {"id": "ROOT", "url": "http://host/"}})
    return frames


def _tree(frame_id: str, parent_id: str, url: str = "http://child/") -> dict[str, Any]:
    return {"frame": {"id": frame_id, "parentId": parent_id, "url": url}}


def _attached(session_id: str, target_id: str, url: str = "http://child/") -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "targetInfo": {"targetId": target_id, "type": "iframe", "url": url},
    }


class TestAFrameSessionThatFailsIsStillListed:
    """`drop` clears the unreachable entry, so recording before it recorded nothing."""

    @pytest.mark.asyncio
    async def test_a_setup_failure_leaves_a_row_naming_the_frame(self):
        client = ScriptedClient(fail={"Page.enable"})
        sessions = FrameSessions(client, _page_map(), "PAGE-SESSION", "PAGE-TARGET")
        sessions._on_attached(_attached("S1", "T1", "http://child/ad"))
        await sessions.settle(timeout=1.0)

        assert sessions.sessions == {}
        assert sessions.unreachable == {"T1": "http://child/ad"}

    @pytest.mark.asyncio
    async def test_a_frame_whose_target_went_away_is_not_called_unreachable(self):
        """A detach is not a failure, and `drop` is right to clear the row."""
        client = ScriptedClient(
            answers={"Page.getFrameTree": lambda sid: {"frameTree": _tree("C1", "ROOT")}}
        )
        sessions = FrameSessions(client, _page_map(), "PAGE-SESSION", "PAGE-TARGET")
        sessions._on_attached(_attached("S1", "T1"))
        await sessions.settle(timeout=1.0)
        assert sessions.unreachable == {}

        sessions._on_detached({"sessionId": "S1"})
        assert sessions.sessions == {}
        assert sessions.unreachable == {}


class TestEveryHeldTreeSplicesWhateverTheOrder:
    """One pass down the list leaves a nested chain half-attached."""

    @pytest.mark.asyncio
    async def test_a_chain_held_in_reverse_order_all_splices(self):
        frames = _page_map()
        client = ScriptedClient()
        sessions = FrameSessions(client, frames, "PAGE-SESSION", "PAGE-TARGET")

        # Grandchild first, so the pass that could splice it runs before the
        # pass that supplies its parent.
        for session_id, frame_id, parent_id in (
            ("S3", "C3", "C2"),
            ("S2", "C2", "C1"),
            ("S1", "C1", "ROOT"),
        ):
            sessions._on_attached(_attached(session_id, f"T{session_id}"))
            sessions.sessions[session_id].pending_tree = _tree(frame_id, parent_id)

        sessions.resplice_pending()

        assert [s.pending_tree for s in sessions.sessions.values()] == [None, None, None]
        assert frames.depth_of("C3") == 3
        await sessions.close()

    @pytest.mark.asyncio
    async def test_a_tree_whose_parent_never_arrives_is_still_held(self):
        frames = _page_map()
        sessions = FrameSessions(ScriptedClient(), frames, "PAGE-SESSION", "PAGE-TARGET")
        sessions._on_attached(_attached("S1", "T1"))
        sessions.sessions["S1"].pending_tree = _tree("C1", "NOT-IN-THE-MAP")

        sessions.resplice_pending()

        assert sessions.sessions["S1"].pending_tree is not None
        await sessions.close()


class TestTheDepthBoundUsesTheTree:
    """An `iframe` attach carries no ancestry, so depth is only knowable after the splice."""

    def test_an_attach_event_cannot_say_how_deep_a_frame_is(self):
        """`openerId` is what the bound used to read, and iframes do not set it."""
        assert "openerId" not in _attached("S1", "T1")["targetInfo"]

    def test_depth_counts_from_the_root(self):
        frames = _page_map()
        frames.splice_session_tree(_tree("C1", "ROOT"), "S1")
        frames.splice_session_tree(_tree("C2", "C1"), "S2")
        assert frames.depth_of("ROOT") == 0
        assert frames.depth_of("C1") == 1
        assert frames.depth_of("C2") == 2

    def test_depth_of_an_unknown_frame_is_zero(self):
        assert _page_map().depth_of("NOBODY") == 0

    def test_a_cycle_returns_rather_than_hanging(self):
        frames = _page_map()
        frames.splice_session_tree(_tree("C1", "ROOT"), "S1")
        frames._frames["ROOT"].parent_frame_id = "C1"
        assert frames.depth_of("C1") <= 2

    @pytest.mark.asyncio
    async def test_a_frame_past_the_bound_is_listed_and_let_go(self):
        frames = _page_map()
        parent = "ROOT"
        for depth in range(1, MAX_DEPTH + 1):
            frames.splice_session_tree(_tree(f"C{depth}", parent), f"OLD{depth}")
            parent = f"C{depth}"

        deep = _tree("TOO-DEEP", parent, "http://child/deep")
        client = ScriptedClient(answers={"Page.getFrameTree": lambda sid: {"frameTree": deep}})
        sessions = FrameSessions(client, frames, "PAGE-SESSION", "PAGE-TARGET")
        sessions._on_attached(_attached("S1", "T1", "http://child/deep"))
        await sessions.settle(timeout=1.0)

        assert sessions.sessions == {}
        assert sessions.unreachable == {"T1": "http://child/deep"}
        assert "TOO-DEEP" not in [f["frameId"] for f in frames.get_flat_frames()]


class TestOneTargetGetsOneSession:
    @pytest.mark.asyncio
    async def test_the_same_target_offered_twice_is_taken_once(self):
        sessions = FrameSessions(ScriptedClient(), _page_map(), "PAGE-SESSION", "PAGE-TARGET")
        sessions._on_attached(_attached("S1", "T1"))
        sessions._on_attached(_attached("S2", "T1"))
        assert list(sessions.sessions) == ["S1"]
        await sessions.close()

    @pytest.mark.asyncio
    async def test_the_runtimes_own_page_target_is_never_taken(self):
        sessions = FrameSessions(ScriptedClient(), _page_map(), "PAGE-SESSION", "PAGE-TARGET")
        sessions._on_attached(
            {
                "sessionId": "S1",
                "targetInfo": {"targetId": "PAGE-TARGET", "type": "iframe", "url": "http://host/"},
            }
        )
        assert sessions.sessions == {}


class TestSettleDrainsWithoutSleeping:
    """The common page has no Out-of-Process Frame and must pay nothing.

    Asserted as "it never waits" rather than "it finished quickly". A wall
    clock reading is a measurement of the machine, and one taken under a
    parallel test run reports the machine being busy.
    """

    @pytest.mark.asyncio
    async def test_a_page_with_nothing_to_discover_never_waits(self, monkeypatch):
        async def refuse(*args, **kwargs):
            raise AssertionError("settle waited with nothing to wait for")

        monkeypatch.setattr(asyncio, "wait", refuse)
        monkeypatch.setattr(asyncio, "sleep", refuse)
        sessions = FrameSessions(ScriptedClient(), _page_map(), "PAGE-SESSION", "PAGE-TARGET")
        await sessions.settle(timeout=3.0)
