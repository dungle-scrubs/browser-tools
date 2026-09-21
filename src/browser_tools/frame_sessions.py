"""The CDP sessions below the page session, one per Out-of-Process Frame.

RFC-04. A frame Chrome puts in its own renderer process gets its own CDP
target. `Page.getFrameTree` on the page session does not contain it and its
lifecycle events do not arrive there, so without this module `frames list`
does not show it, `frames select` cannot select it, and `snapshot` shows the
`Iframe` node with nothing under it.

`Target.setAutoAttach` with `flatten: true` delivers each such target as a
`Target.attachedToTarget` on the session that owns it. This module turns that
event into a **Frame Session**: a session with `Page` and `Runtime` enabled,
its frames spliced into the one Spliced Frame Tree, and its own
`setAutoAttach` for whatever is nested below it.

Four things measured before this was written
(`docs/rfc/04_reach-into-cross-origin-iframes.measurements.md`):

- The child's frame id **is** its target id, and its own `parentId` names a
  frame in the parent's tree. The splice point is given, not guessed.
- Auto-attach does not cascade. Each new session needs its own
  `setAutoAttach`, so this recurses.
- Removing an iframe detaches its session, and a later send on it is
  `-32001 Session with given id not found`.
- A same-origin navigation inside a child keeps its session.

Three rules that are not about CDP at all, and that the RFC-04 review put
here after finding each one broken in the draft:

- **Nothing here may fail an invocation that did not ask about a child
  frame.** Setup runs as its own task, and every failure ends as a frame that
  is absent or marked unreachable, never as a raised exception reaching the
  caller.
- **The receive loop is shared and calls handlers synchronously.** An
  exception escaping a handler closes the whole connection, page session
  included. Every handler here contains its own.
- **A completion whose session has gone is discarded.** A `Page.getFrameTree`
  can return after its session detached, and applying it would resurrect
  frames that are not there.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core.cdp_client import CDPClient as CoreCDPClient
    from .frame_manager import FrameManager

logger = logging.getLogger(__name__)

#: The target types this design supports. A positive list, not an exclusion:
#: auto-attach reaches workers and service workers too, and the RFC-04 review
#: found that `targetId != own_target_id` sends a worker into `Page.enable`.
#: A type nobody has thought about does not become a Frame Session by default.
SUPPORTED_TARGET_TYPES = frozenset({"iframe"})

#: How deep the recursion goes. A page can nest frames without limit.
MAX_DEPTH = 10

#: How many Frame Sessions one runtime will hold. A depth bound alone is not a
#: bound: twenty sibling iframes all pass a depth limit of ten, and that is the
#: shape an ad-heavy page has.
MAX_SESSIONS = 32

#: How many sessions may be setting themselves up at once. Each costs four
#: round trips before it is useful.
MAX_CONCURRENT_SETUP = 8

#: What one Frame Session's setup gets before it is abandoned. A child that
#: never answers must not hold the frames below the ones that did.
SETUP_TIMEOUT_SECONDS = 5.0

#: How long the whole discovery wave gets, including nested frames. A page
#: that never goes quiet is a page whose frames keep attaching, and the
#: caller gets whatever was discovered by this point rather than nothing.
SETTLE_TIMEOUT_SECONDS = 3.0


@dataclass
class FrameSession:
    """One attached Out-of-Process Frame target."""

    session_id: str
    target_id: str
    depth: int
    #: The target's URL at attach time, kept so a session that has to be
    #: abandoned can still be listed as unreachable by URL. By then its
    #: frame is out of the map, so the map cannot answer for it.
    url: str = ""
    #: Bumped when this session is torn down, so a read issued against it and
    #: returning afterwards can be recognised as stale and discarded.
    generation: int = 0
    ready: bool = False
    #: Its own `Page.getFrameTree`, held when the parent frame was not in the
    #: map yet. Spliced when the placeholder arrives.
    pending_tree: dict[str, Any] | None = None
    subscriptions: list[tuple[str, Any]] = field(default_factory=list)


class FrameSessions:
    """Owns every Frame Session below the page session.

    Does not own the page session, and does not close the connection.
    """

    def __init__(
        self,
        client: CoreCDPClient,
        frames: FrameManager,
        page_session_id: str,
        page_target_id: str,
    ) -> None:
        self._client = client
        self._frames = frames
        self._page_session_id = page_session_id
        self._page_target_id = page_target_id
        self._sessions: dict[str, FrameSession] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._setup_slots = asyncio.Semaphore(MAX_CONCURRENT_SETUP)
        #: Frames past a bound: listed with their URL, marked unreachable,
        #: never silently dropped.
        self._unreachable: dict[str, str] = {}
        self._generation = 0
        self._started = False

    # ----------------------------------------------------------------- read

    @property
    def sessions(self) -> dict[str, FrameSession]:
        """Every Frame Session, by session id."""
        return self._sessions

    @property
    def unreachable(self) -> dict[str, str]:
        """Target id to URL, for frames a bound refused."""
        return dict(self._unreachable)

    # ---------------------------------------------------------------- start

    async def start(self) -> None:
        """Subscribe, then turn auto-attach on for the page session.

        Subscribe first. `setAutoAttach` delivers its attaches immediately,
        and a handler registered after the call misses them, which is the
        same mistake `Runtime.enable` made before RFC-03's review found it.
        """
        if self._started:
            return
        self._started = True
        self._subscribe(self._page_session_id)
        await self._set_auto_attach(self._page_session_id)

    def _subscribe(self, session_id: str) -> None:
        self._client.on("Target.attachedToTarget", self._on_attached, session_id)
        self._client.on("Target.detachedFromTarget", self._on_detached, session_id)

    def _subscribe_runtime(self, session_id: str) -> None:
        """File this session's execution contexts against its frames.

        Subscribe before the enable, never after. `Runtime.enable` replays
        the contexts that already exist, and a handler registered afterwards
        throws that replay away - the same mistake `_connect_cdp` records
        for the page session, where it left every frame with no context and
        made `storage get` return cookies only.

        Bound to this session id, because a context id means nothing without
        it. `functools.partial` rather than a lambda: a lambda closing over
        the loop variable would file every session's contexts under the last
        session id.
        """
        for event, handler in (
            ("Runtime.executionContextCreated", self._frames.handle_execution_context_created),
            ("Runtime.executionContextDestroyed", self._frames.handle_execution_context_destroyed),
            ("Runtime.executionContextsCleared", self._frames.handle_execution_contexts_cleared),
        ):
            self._client.on(event, functools.partial(handler, session_id=session_id), session_id)

    async def _set_auto_attach(self, session_id: str) -> None:
        """Turn auto-attach on, and wait until its attaches have arrived.

        The second command is a barrier, not a read. One CDP connection
        delivers messages in order, so by the time its reply lands, every
        `Target.attachedToTarget` that `setAutoAttach` produced has already
        been dispatched to the receive loop. That turns discovery into a round
        trip instead of a guess about how long to wait.

        The timed window this replaced cost 313 ms on a page with no
        cross-origin iframe, measured, which is the common page. The barrier
        costs one round trip whether or not anything attaches.
        """
        await self._client.send(
            method="Target.setAutoAttach",
            params={"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
            session_id=session_id,
        )
        await self._client.send(method="Target.getTargetInfo", session_id=session_id)

    # -------------------------------------------------------------- handlers

    def _on_attached(self, params: dict[str, Any]) -> None:
        """`Target.attachedToTarget`, from the receive loop.

        Synchronous, on the shared receive loop, so it contains everything
        and does the work in a task.
        """
        try:
            self._schedule_setup(params)
        except Exception:
            logger.exception("frame session attach handling failed; page session unaffected")

    def _on_detached(self, params: dict[str, Any]) -> None:
        try:
            session_id = params.get("sessionId", "")
            if session_id:
                self.drop(session_id)
        except Exception:
            logger.exception("frame session detach handling failed; page session unaffected")

    def _schedule_setup(self, params: dict[str, Any]) -> None:
        info = params.get("targetInfo") or {}
        if not self._is_supported(info):
            return
        self._admit(params.get("sessionId", ""), info)

    def _admit(self, session_id: str, info: dict[str, Any]) -> None:
        """Take on one attached target, unless a bound or a duplicate says no.

        Two sessions for one target would splice the same subtree twice, so
        the target is checked as well as the session id. Nothing observed
        has sent the same target twice; the check is here because the cost
        of being wrong is a duplicated subtree in the listing and the cost
        of the check is a scan of at most `MAX_SESSIONS` entries.

        Depth is not checked here. An ``iframe`` target's attach carries no
        ancestry - ``openerId`` is not set for one - so how deep the frame
        sits is only knowable once its tree has been spliced under its
        parent. `_setup_once` applies that bound there.
        """
        target_id = info.get("targetId", "")
        if not session_id or not target_id:
            return
        if session_id in self._sessions or self._has_target(target_id):
            return
        if len(self._sessions) >= MAX_SESSIONS:
            self._unreachable[target_id] = info.get("url", "")
            return

        session = FrameSession(
            session_id=session_id,
            target_id=target_id,
            depth=0,
            url=info.get("url", ""),
            generation=self._generation,
        )
        self._sessions[session_id] = session
        task = asyncio.ensure_future(self._setup(session))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _has_target(self, target_id: str) -> bool:
        return any(s.target_id == target_id for s in self._sessions.values())

    def _is_supported(self, info: dict[str, Any]) -> bool:
        """A supported type, and not this runtime's own page target.

        The second half is the ancestry check in its cheapest form. The page
        target arriving as an attach is the explicit attach's own event, not a
        child, and driving the page through a second session the frame manager
        does not know about would be worse than ignoring it.
        """
        if info.get("type") not in SUPPORTED_TARGET_TYPES:
            return False
        return info.get("targetId") != self._page_target_id

    # ----------------------------------------------------------------- setup

    async def _setup(self, session: FrameSession) -> None:
        """Enable, read the tree, splice, and recurse. Never raises."""
        try:
            async with self._setup_slots:
                await asyncio.wait_for(self._setup_once(session), SETUP_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.debug("frame session %s did not set up in time", session.session_id)
            self._abandon(session, "setup timed out")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("frame session %s setup failed: %s", session.session_id, exc)
            self._abandon(session, str(exc))

    async def _setup_once(self, session: FrameSession) -> None:
        generation = session.generation
        self._subscribe(session.session_id)

        await self._client.send(method="Page.enable", session_id=session.session_id)
        result = await self._client.send(method="Page.getFrameTree", session_id=session.session_id)
        if self._is_stale(session, generation):
            return

        tree = result.get("frameTree")
        if tree:
            if self._frames.splice_session_tree(tree, session.session_id):
                if self._too_deep(session, tree):
                    return
            else:
                # The parent session has not reported the placeholder frame
                # yet. Hold it rather than attaching it anywhere else.
                session.pending_tree = tree

        self._subscribe_runtime(session.session_id)
        await self._client.send(method="Runtime.enable", session_id=session.session_id)
        if self._is_stale(session, generation):
            return
        await self._set_auto_attach(session.session_id)
        session.ready = True

    def _abandon(self, session: FrameSession, why: str) -> None:
        """Let a Frame Session go, and list the frame it answered for.

        Order matters. `drop` clears the session's `_unreachable` entry,
        because the usual reason to drop one is that its target went away
        and a gone frame is not an unreachable frame. Recording before the
        drop therefore recorded nothing, which made every setup failure
        invisible: the frames vanished from the listing and no row said
        why.
        """
        url = session.url
        self.drop(session.session_id)
        self._unreachable[session.target_id] = url
        logger.debug("frame session %s abandoned: %s", session.session_id, why)

    def _too_deep(self, session: FrameSession, tree: dict[str, Any]) -> bool:
        """Past `MAX_DEPTH`: take the subtree back out and list it instead.

        Checked after the splice because that is the first moment the depth
        is known. Undoing one splice is cheaper than the alternative, which
        is guessing the depth from an attach event that does not carry it.
        """
        frame_id = tree.get("frame", {}).get("id", "")
        session.depth = self._frames.depth_of(frame_id) if frame_id else 0
        if session.depth <= MAX_DEPTH:
            return False
        self._abandon(session, f"depth {session.depth} is past the bound")
        self._unreachable[session.target_id] = tree.get("frame", {}).get("url", "") or session.url
        return True

    def _is_stale(self, session: FrameSession, generation: int) -> bool:
        """Did this session go away while the read was in flight?"""
        live = self._sessions.get(session.session_id)
        return live is not session or session.generation != generation

    # -------------------------------------------------------------- teardown

    def drop(self, session_id: str) -> None:
        """Forget a Frame Session and everything below it.

        Its frames go, and so does any Frame Session that answered for one of
        them: leaving those behind leaks a session for the rest of the run.
        """
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        session.generation += 1
        orphaned = self._frames.forget_session(session_id)
        for other in list(self._sessions.values()):
            if self._frames.session_for(other.target_id) is None and other.target_id in orphaned:
                self.drop(other.session_id)
        self._unreachable.pop(session.target_id, None)

    async def settle(self, timeout: float = SETTLE_TIMEOUT_SECONDS) -> None:
        """Wait for the discovery wave to finish, bounded, never raising.

        Without this the invocation is ready the moment the page session is,
        and `frames select` on an Out-of-Process Frame fails against a tree
        the child has not been spliced into yet. Measured: it failed
        standalone and passed inside a Step Run, purely because the run did
        more work first.

        This is a drain, not a wait. `_set_auto_attach` already carries a
        round-trip barrier, so when `start` returns, the first wave's tasks
        exist. Each task ends with its own barriered `setAutoAttach`, so a
        nested frame's task exists before its parent's task completes, and
        draining until empty covers every depth with no sleeping at all.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._tasks and loop.time() < deadline:
            await asyncio.wait(set(self._tasks), timeout=max(0.0, deadline - loop.time()))
        self.resplice_pending()

    def resplice_pending(self) -> None:
        """Try again for every held tree whose parent may have arrived.

        A child session can attach before the parent session reports the
        placeholder frame. Called after the page session's tree changes.

        Runs to a fixpoint. Splicing one held tree can supply the parent
        another held tree was waiting for, and a nested chain of
        Out-of-Process Frames arrives in whatever order its renderers
        answered in, so one pass down the list leaves the deeper ones held.
        """
        while True:
            spliced = False
            for session in list(self._sessions.values()):
                if session.pending_tree is None:
                    continue
                if self._frames.splice_session_tree(session.pending_tree, session.session_id):
                    session.pending_tree = None
                    spliced = True
            if not spliced:
                return

    async def close(self) -> None:
        """Cancel outstanding setup. The connection closes the sessions."""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self._tasks.clear()
        self._sessions.clear()
