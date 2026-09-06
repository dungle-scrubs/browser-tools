# Vendored from chrome-agent v0.5.7 (https://github.com/captivus/chrome-agent).
# Copyright (c) 2026 Corey Gallon.
# SPDX-License-Identifier: MIT
# See /NOTICE for the full vendoring notice.
#
# This file is an ADAPTED vendored module (RFC-01, "Adapted modules"). The
# window marker no longer draws anything inside the page. Upstream drew a
# colored border and a corner badge as fixed-position elements over the
# viewport; both sat on top of page content (the badge over whatever the page
# put top-left, the border over its outer 6px) and got in the way of seeing
# the page. The marker is now the tab title prefix alone, which lives in the
# browser's tab strip, outside the page. The script also guards on the top
# frame: Page.addScriptToEvaluateOnNewDocument runs in every iframe as well,
# and only the top document's title is the tab title.
#
# The attach path is also adapted (see "Never hold a document paused" in the
# module docstring): targets are found by discovery instead of auto-attach and
# only page targets are attached to, every attached session is resumed before
# any marking work, and a watchdog exits the process when its event loop
# stalls. Intra-package imports are rewritten to browser_tools.core. Otherwise
# unchanged from chrome-agent v0.5.7. See RFC-01, section "Vendoring rules".

"""Per-instance supervisor for chrome-agent.

A detached process spawned per headed launch that holds a browser-level CDP
connection open for the browser's lifetime. It does two jobs:

1. **Lifecycle** -- when the connection drops (the window/browser is closed,
   crashes, or is shut down), it removes the instance from the registry and
   deletes its session directory. This keeps the registry mirroring reality in
   real time: close the window and the instance disappears, no command needed.
   Because chrome-agent has no daemon, this long-lived connection is the only
   thing that can observe a close as it happens.

2. **Window marker** (optional) -- while the browser is alive, mark every tab
   (current and future) by prefixing its title with the instance name, so an
   agent-driven window is easy to tell apart from the user's own Chrome in
   the tab strip and window title. Nothing is drawn inside the page.
   ``Page.addScriptToEvaluateOnNewDocument`` re-runs the marker on every new
   document, but only while the registering connection is alive -- hence the
   same long-lived process.

The marker is page-observable (a modified ``document.title``), so it is
suppressed under a fingerprint profile; the lifecycle job still runs. The
injected code is a side-effect-free IIFE that leaks no globals.
See ``planning/03-specs/BRW-03-learnings/01-detection-audit.md``.

**Never hold a document paused.** The supervisor is a permanently-attached CDP
client of a browser a human is using, so a bug in it must never be able to stop
that browser from loading pages. A client that auto-attaches with
``waitForDebuggerOnStart`` and then stops resuming wedges every document created
after it stalled: new tabs sit on Chrome's "Debugger paused in another tab"
banner and cross-origin iframes stay at ``readyState === "loading"`` with no
network requests issued, forever, because a page reload only makes another
paused document. Three rules keep the supervisor out of that state:

1. It never asks for a paused target, and it sends
   ``Runtime.runIfWaitingForDebugger`` on every session it attaches, before any
   marking work. Marking runs after the resume and cannot skip it.
2. It finds targets by discovery and attaches to page targets only
   (``_PAGE_TARGET_FILTER``), never by auto-attach. Chrome throttles a popup's
   creation on the auto-attaching client's handshake, so an auto-attaching
   supervisor that stalls blocks every ``window.open`` in the browser it
   watches -- measured: the call never returns while the client is frozen.
   Discovery is a notification Chrome does not wait on. Marking is also a
   top-document job, so iframe (OOPIF), worker, and extension targets are none
   of the supervisor's business, and each attached session would be protocol
   traffic this process must keep draining.
3. A watchdog thread exits the process if the event loop stops running
   (``_WATCHDOG_TIMEOUT_SECONDS``), and the connection is health-pinged so a
   half-open socket is dropped rather than held. A dead client releases
   everything it held; a live-but-stuck client is the worst state to be in.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time

from .cdp_client import CDPClient, get_ws_url

ISOLATED_WORLD = "__chrome_agent_marker__"

# Watch top-level page targets only: those are the only targets the marker
# applies to. The first matching entry decides, so the trailing catch-all
# excludes everything else (iframe/OOPIF, workers, service workers, extension
# background pages, browser UI).
_PAGE_TARGET_FILTER = [{"type": "page", "exclude": False}, {"exclude": True}]

# Health ping: a browser-level CDP round trip, so a socket that has gone
# half-open (connected, nothing flowing) is detected and dropped instead of
# being held open by a client that can no longer act on what it sees.
_HEALTH_PING_INTERVAL_SECONDS = 15.0
_HEALTH_PING_TIMEOUT_SECONDS = 10.0

# Watchdog: the event loop refreshes a heartbeat every second; a thread reads
# it and exits the process if the loop stops refreshing it for this long.
_HEARTBEAT_INTERVAL_SECONDS = 1.0
_WATCHDOG_TIMEOUT_SECONDS = 30.0
_WATCHDOG_POLL_SECONDS = 1.0
# How far the watchdog's own sleep must overshoot before the gap is read as a
# process-wide freeze rather than ordinary scheduling jitter. Well under the
# stall timeout (so a freeze long enough to look like a stall is always caught)
# and well over the jitter of a sleeping thread on a loaded machine.
_FREEZE_OVERSHOOT_SECONDS = 5.0
# Exit code for "the event loop stalled"; distinct from a clean retire (0).
EXIT_EVENT_LOOP_STALLED = 3


def build_overlay_script(*, name: str) -> str:
    """Build the IIFE injected into each page to mark the tab.

    Keeps ``document.title`` prefixed with the instance name (re-applied
    idempotently on SPA/title changes). Draws nothing inside the page: upstream
    also drew a fixed border and a corner badge over the viewport, and both
    covered page content. Leaks no globals.

    Runs in the top document only. ``Page.addScriptToEvaluateOnNewDocument``
    evaluates the source in every frame, and only the top document's title is
    the tab title. The ``window.top`` identity comparison is permitted across
    origins; the ``try`` covers sandboxed frames where the access throws, which
    are never the top document either.
    """
    NAME = json.dumps(name)
    return (
        "(() => {"
        "  try { if (window.self !== window.top) return; } catch(e) { return; }"
        f"  var NAME={NAME};"
        "  var PREFIX = '\\uD83E\\uDD16 ' + NAME + ' \\u2014 ';"  # 🤖 NAME —
        "  function fixTitle(){ try { var t = document.title || ''; if (t.indexOf(PREFIX) !== 0) document.title = PREFIX + t; } catch(e){} }"
        "  function watchTitle(){"
        "    fixTitle();"
        "    try {"
        "      var t = document.querySelector('title'); if (t) new MutationObserver(fixTitle).observe(t, {childList:true, characterData:true, subtree:true});"
        "      if (document.head) new MutationObserver(fixTitle).observe(document.head, {childList:true, subtree:true});"
        "    } catch(e){}"
        "  }"
        "  if (document.head || document.readyState !== 'loading') { watchTitle(); }"
        "  else { document.addEventListener('DOMContentLoaded', watchTitle); }"
        "})();"
    )


async def _resume_target(cdp: CDPClient, session_id: str) -> None:
    """Release a target that is (or might be) waiting for the debugger.

    ``Runtime.runIfWaitingForDebugger`` is a no-op on a target that is not
    waiting, so this is sent unconditionally rather than trusting the
    ``waitingForDebugger`` flag on the attach event. Failures are swallowed:
    the target may already be gone, and nothing the supervisor does afterwards
    depends on the answer.
    """
    try:
        await cdp.send(method="Runtime.runIfWaitingForDebugger", session_id=session_id)
    except Exception:
        pass


async def _release_target(cdp: CDPClient, session_id: str) -> None:
    """Resume a target the supervisor does not mark, then let go of it.

    Reached only for a session the supervisor did not ask for. Resuming first
    is what matters -- an attached session is the only thing that can keep a
    paused target paused -- and detaching then stops this process from carrying
    that target's protocol traffic.
    """
    await _resume_target(cdp, session_id)
    try:
        await cdp.send(method="Target.detachFromTarget", params={"sessionId": session_id})
    except Exception:
        pass


async def _setup_session(cdp: CDPClient, session_id: str, source: str) -> None:
    """Resume a page session, then register the marker on it.

    The resume comes first and unconditionally: marking a window must never be
    able to leave a document paused, so no marking work sits between the attach
    and the release. The marking itself is best-effort (the tab may close
    mid-setup) and is followed by a second resume on failure, in case the first
    one raced the target's arrival.
    """
    await _resume_target(cdp, session_id)
    try:
        await cdp.send(method="Page.enable", session_id=session_id)
        # Future documents: re-run on every navigation, in an isolated world.
        await cdp.send(
            method="Page.addScriptToEvaluateOnNewDocument",
            params={"source": source, "worldName": ISOLATED_WORLD},
            session_id=session_id,
        )
        # The already-loaded document needs a one-time injection.
        await cdp.send(
            method="Runtime.evaluate",
            params={"expression": source},
            session_id=session_id,
        )
    except Exception:
        # tab may close mid-setup; the guard keeps running for other tabs
        await _resume_target(cdp, session_id)


class Heartbeat:
    """A monotonic timestamp the event loop refreshes and the watchdog reads."""

    def __init__(self) -> None:
        self._last = time.monotonic()

    def beat(self) -> None:
        self._last = time.monotonic()

    def age(self) -> float:
        """Seconds since the last beat."""
        return time.monotonic() - self._last


def _exit_stalled() -> None:
    """Leave the browser alone by leaving: exit without unwinding anything.

    ``os._exit`` skips interpreter cleanup on purpose. The loop that would run
    it is the thing that stalled, so the only reliable action left is dropping
    the process -- which closes the CDP socket, which makes Chrome release
    every session this client held.
    """
    print(
        "supervisor: event loop stalled, exiting so the browser is released",
        file=sys.stderr,
    )
    os._exit(EXIT_EVENT_LOOP_STALLED)


def _round_says_stalled(
    *,
    heartbeat: Heartbeat,
    overshoot: float,
    poll: float,
    timeout: float,
    freeze_overshoot: float = _FREEZE_OVERSHOOT_SECONDS,
) -> bool:
    """Decide one watchdog round: is the loop stalled, or was the process frozen?

    ``overshoot`` is how much longer than ``poll`` the watchdog's own sleep
    actually took. A big overshoot means this thread was not running either, so
    the freeze was process-wide (host suspend, SIGSTOP) and says nothing about
    the event loop -- the heartbeat is beaten here to charge the gap to the
    machine and give the loop a fresh ``timeout`` to prove itself. The bar is
    high enough that scheduling jitter is not mistaken for a freeze, which
    would mask a real stall round after round.
    """
    if overshoot > max(poll, freeze_overshoot):
        heartbeat.beat()
        return False
    return heartbeat.age() > timeout


def start_watchdog(
    *,
    heartbeat: Heartbeat,
    timeout: float = _WATCHDOG_TIMEOUT_SECONDS,
    poll: float = _WATCHDOG_POLL_SECONDS,
    on_stall=_exit_stalled,
) -> threading.Thread:
    """Run a thread that fires ``on_stall`` once the heartbeat goes stale.

    A plain OS thread, not a task: a stalled event loop cannot police itself.

    A stale heartbeat only counts when this thread itself kept running. A host
    suspend, or a SIGSTOP, freezes every thread in the process, so the
    heartbeat is arbitrarily old on the way back through no fault of the loop.
    The thread measures how far its own sleep overshot to tell the two apart,
    and charges an overshoot to the machine by beating the heartbeat itself --
    which gives the loop a full fresh ``timeout`` to prove it is still running.
    """
    def watch() -> None:
        while True:
            before = time.monotonic()
            time.sleep(poll)
            overshoot = time.monotonic() - before - poll
            if _round_says_stalled(
                heartbeat=heartbeat, overshoot=overshoot, poll=poll, timeout=timeout
            ):
                on_stall()
                return

    thread = threading.Thread(target=watch, name="supervisor-watchdog", daemon=True)
    thread.start()
    return thread


async def _beat_forever(heartbeat: Heartbeat) -> None:
    """Refresh the heartbeat for as long as the event loop is running."""
    while True:
        heartbeat.beat()
        await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)


async def _connection_is_answering(cdp: CDPClient) -> bool:
    """Whether the browser still answers on this connection."""
    try:
        await asyncio.wait_for(
            cdp.send(method="Browser.getVersion"), _HEALTH_PING_TIMEOUT_SECONDS
        )
        return True
    except Exception:
        return False


async def _enable_page_discovery(cdp: CDPClient) -> None:
    """Ask to be told about page targets, without standing in their way.

    Discovery is a notification: Chrome reports targets as they appear and
    waits for nothing. ``Target.setAutoAttach`` is the opposite -- Chrome holds
    a popup's creation on the auto-attaching client's handshake, so a
    supervisor that stalls blocks every ``window.open`` in the browser, which
    is half of the failure this module exists to make impossible. Marking a tab
    title has no business gating a document, so the supervisor is not in that
    path at all.

    ``filter`` needs Chrome 106+; an older browser rejects the call and the
    retry without it reports every target type instead, which ``on_created``
    then filters.
    """
    params: dict = {"discover": True, "filter": _PAGE_TARGET_FILTER}
    try:
        await cdp.send(method="Target.setDiscoverTargets", params=params)
    except Exception:
        del params["filter"]
        await cdp.send(method="Target.setDiscoverTargets", params=params)


async def _mark_target(cdp: CDPClient, target_id: str, source: str) -> None:
    """Attach to one page target and install the marker on its session."""
    try:
        result = await cdp.send(
            method="Target.attachToTarget",
            params={"targetId": target_id, "flatten": True},
        )
    except Exception:
        return  # the tab closed between the notice and the attach
    session_id = result.get("sessionId")
    if session_id:
        await _setup_session(cdp, session_id, source)


async def _supervise_connection(
    *, port: int, draw_border: bool, source: str | None, heartbeat: Heartbeat | None = None
) -> None:
    """Hold one browser-level CDP connection until it drops.

    Connects, and (when ``draw_border`` and ``source`` are set) installs the
    window marker on every current and future page target, then blocks until the
    connection drops or stops answering. Returns when disconnected; raises if
    the connect itself fails. Caller decides whether a drop means the browser
    closed (retire) or was a transient blip (reconnect).
    """
    browser_ws = get_ws_url(port=port, target_type="browser")
    cdp = CDPClient(ws_url=browser_ws)
    await cdp.connect()
    loop = asyncio.get_event_loop()

    # Strong references to the in-flight per-session setups: a task nobody
    # holds can be garbage-collected mid-await.
    pending: set[asyncio.Task] = set()

    def spawn(coro) -> None:
        task = loop.create_task(coro)
        pending.add(task)
        task.add_done_callback(pending.discard)

    if draw_border and source is not None:
        handled: set[str] = set()

        def on_created(params: dict) -> None:
            info = params.get("targetInfo", {})
            target_id = info.get("targetId")
            if not target_id or target_id in handled:
                return
            if info.get("type") != "page":
                return  # not marked, so not attached to either
            handled.add(target_id)
            spawn(_mark_target(cdp, target_id, source))

        def on_destroyed(params: dict) -> None:
            handled.discard(params.get("targetId", ""))

        def on_attached(params: dict) -> None:
            # Nothing is expected here -- the supervisor attaches on its own
            # terms. A session the browser hands over anyway (an old browser
            # that ignored the filter, a target auto-attached to this
            # connection by something else) is released, never held paused.
            session_id = params.get("sessionId")
            if session_id and params.get("targetInfo", {}).get("type") != "page":
                spawn(_release_target(cdp, session_id))

        cdp.on(event="Target.targetCreated", callback=on_created)
        cdp.on(event="Target.targetDestroyed", callback=on_destroyed)
        cdp.on(event="Target.attachedToTarget", callback=on_attached)

        # Discovery reports every current AND future target, so this covers the
        # tabs already open as well as the ones opened later.
        await _enable_page_discovery(cdp)

    try:
        next_ping = loop.time() + _HEALTH_PING_INTERVAL_SECONDS
        while cdp._connected:
            await asyncio.sleep(1)
            if heartbeat is not None:
                heartbeat.beat()
            now = loop.time()
            if now >= next_ping:
                next_ping = now + _HEALTH_PING_INTERVAL_SECONDS
                if not await _connection_is_answering(cdp):
                    break
    finally:
        await cdp.close()


# Seconds to wait for a dropped browser's CDP port to stop listening before
# concluding it really closed. A browser that is genuinely closing drops its
# listener within a moment; a live one whose WebSocket merely dropped (host
# suspend/resume, transient blip) keeps the port up across this window.
_RETIRE_GRACE_SECONDS = 5.0


async def _browser_gone(port: int) -> bool:
    """Whether the browser is truly gone, vs. a transient CDP drop.

    Polls the CDP port for up to ``_RETIRE_GRACE_SECONDS``. Returns True as soon
    as the port stops listening (the browser closed), or False if it stays up
    for the whole window (the drop was transient -- e.g. a host suspend/resume
    severed the WebSocket while Chrome kept running and listening).
    """
    from .registry import _port_is_listening

    deadline = asyncio.get_event_loop().time() + _RETIRE_GRACE_SECONDS
    while asyncio.get_event_loop().time() < deadline:
        if not _port_is_listening(port):
            return True
        await asyncio.sleep(0.2)
    return False


async def run_supervisor(
    *,
    port: int,
    name: str,
    registry_path: str | None = None,
    draw_border: bool = True,
    watchdog: bool = True,
) -> None:
    """Supervise a launched browser until it actually closes.

    Holds a browser-level CDP connection. While alive and ``draw_border`` is set,
    marks every page target with the window marker (title prefix). The instance is retired from
    the registry (and its session directory removed) ONLY when the browser is
    truly gone -- detected by its CDP port no longer listening.

    A dropped CDP connection is NOT taken as proof the browser closed: a host
    suspend/resume (or any transient network blip) severs the long-lived
    WebSocket while Chrome keeps running and listening on its CDP port. Retiring
    on the dropped socket alone orphaned live instances from the registry across
    a suspend. Instead, when the connection drops we consult the port via
    ``_browser_gone`` -- the same signal ``_instance_is_alive`` trusts -- and
    reconnect-and-resume if the browser is still up, retiring only once it is gone.

    A watchdog runs alongside: if this loop stops running (deadlock, a blocking
    call on the event loop), the process exits instead of sitting attached to
    the browser doing nothing. ``watchdog=False`` runs without it, for tests
    that drive one pass of the loop directly.
    """
    from .registry import deregister

    heartbeat = Heartbeat()
    if watchdog:
        start_watchdog(heartbeat=heartbeat)
        beat_task = asyncio.get_event_loop().create_task(_beat_forever(heartbeat))
    else:
        beat_task = None

    # Build the marker script once; the title prefix is idempotent, so a
    # reconnect re-injecting it on live tabs changes nothing.
    source: str | None = None
    if draw_border:
        source = build_overlay_script(name=name)

    try:
        await _supervise_forever(
            port=port,
            name=name,
            registry_path=registry_path,
            draw_border=draw_border,
            source=source,
            heartbeat=heartbeat,
            deregister=deregister,
        )
    finally:
        if beat_task is not None:
            beat_task.cancel()


async def _supervise_forever(
    *,
    port: int,
    name: str,
    registry_path: str | None,
    draw_border: bool,
    source: str | None,
    heartbeat: Heartbeat,
    deregister,
) -> None:
    """Connect, supervise, and reconnect until the browser is gone."""
    while True:
        try:
            await _supervise_connection(
                port=port, draw_border=draw_border, source=source, heartbeat=heartbeat
            )
        except Exception:
            # A failed (re)connect or a mid-stream CDP error lands here; fall
            # through to the liveness check to decide retire-vs-reconnect.
            pass

        if await _browser_gone(port):
            # Browser really closed -> retire from the registry and exit.
            deregister(instance_name=name, registry_path=registry_path)
            return

        # Transient drop -- the browser is still alive. Reconnect and resume
        # supervising (re-installs the marker on the live tabs) rather than
        # orphaning a live instance.
        await asyncio.sleep(0.5)


def spawn_supervisor(
    *, port: int, name: str, registry_path: str, draw_border: bool
) -> subprocess.Popen:
    """Spawn the detached per-instance supervisor process for a launched browser."""
    return subprocess.Popen(
        [
            sys.executable, "-m", "browser_tools.core.supervisor",
            str(port), name, registry_path, "1" if draw_border else "0",
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> None:
    """Entry point: `python -m browser_tools.core.supervisor PORT NAME REGISTRY_PATH DRAW_BORDER`."""
    if len(sys.argv) < 5:
        print("usage: python -m browser_tools.core.supervisor PORT NAME REGISTRY_PATH DRAW_BORDER", file=sys.stderr)
        sys.exit(2)
    port = int(sys.argv[1])
    name = sys.argv[2]
    registry_path = sys.argv[3]
    draw_border = sys.argv[4] == "1"
    try:
        asyncio.run(run_supervisor(
            port=port, name=name, registry_path=registry_path, draw_border=draw_border,
        ))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
