# Vendored from chrome-agent v0.5.7 (https://github.com/captivus/chrome-agent).
# Copyright (c) 2026 Corey Gallon.
# SPDX-License-Identifier: MIT
# See /NOTICE for the full vendoring notice.
#
# This file is an ADAPTED vendored module (RFC-01, "Adapted modules"). The
# window marker is two scripts. The tab title prefix always runs. The colored
# border and corner badge that upstream drew over the viewport are a separate
# script that follows a persistent user setting (browser_tools.user_settings,
# ``bt window-border on|off``): the border sits on the page's outer edge and
# the badge on its top-left corner, so a person who needs to see that UI turns
# them off, and every running supervisor adds or removes them on its open tabs
# within a second. Both scripts guard on the top frame:
# Page.addScriptToEvaluateOnNewDocument runs in every iframe as well, and only
# the top document is the window. The badge fades while the pointer is near it.
#
# The supervisor is also spawned into its own session and records why it
# exited (#65): it claimed to be detached but shared the launching shell's
# session, so closing that terminal took it while the browser lived on, and it
# wrote nothing on the way out, so an exit left no trace.
#
# Retirement is also adapted (#94). Upstream retires the instance by calling
# the vendored registry's ``deregister``, which removes the entry and then
# deletes the recorded user-data dir without checking whether the instance is
# profile-bound. In browser-tools a named profile IS a user-data dir, so that
# destroyed a login every time a headed browser was closed normally. The
# registry module is verbatim vendored and cannot carry the check, so the
# supervisor retires through ``browser_tools.lifecycle.retire_instance``
# instead, which preserves a bound profile and reaps an unbound session dir.
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
   (current and future) so an agent-driven window is easy to tell apart from
   the user's own Chrome: its title is prefixed with the instance name, and,
   while the ``window_border`` user setting is on (the default), a colored
   border and corner badge are drawn over the page. The setting is re-read
   every second and applied to the open tabs, so turning it off clears the
   border from a running browser.
   ``Page.addScriptToEvaluateOnNewDocument`` re-runs the marker on every new
   document, but only while the registering connection is alive -- hence the
   same long-lived process.

The marker is page-observable (a modified ``document.title``, and a host
element while the border is drawn), so it is suppressed under a fingerprint
profile; the lifecycle job still runs. The injected code is a side-effect-free
IIFE that leaks no globals.
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
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .cdp_client import CDPClient, get_ws_url_async

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

# The supervisor is spawned with its output to DEVNULL, so nothing it printed
# survived it. One appended line per exit is what makes "why is there no
# supervisor?" answerable after the fact. Capped, because the file outlives
# every instance that writes to it.
_EXIT_LOG_MAX_BYTES = 256 * 1024


#: Set by :func:`main` so the watchdog thread, which has no arguments of its
#: own, can name the instance it is abandoning. Empty when nothing spawned us
#: through the entry point (a test driving the loop directly).
_EXIT_CONTEXT: dict[str, object] = {}


def supervisor_exit_log_path(registry_path: str) -> Path:
    """Where supervisor exits are recorded, next to the registry they retire from."""
    return Path(registry_path).parent / "supervisor-exits.log"


def log_supervisor_exit(*, reason: str, name: str, port: int, registry_path: str) -> None:
    """Append one line recording why this supervisor is leaving.

    Best-effort in every sense: a supervisor that cannot write its epitaph
    still exits, and the caller is usually already on its way out.

    Args:
        reason: Short phrase, e.g. "browser closed" or "event loop stalled".
        name: Instance name this supervisor was supervising.
        port: That instance's debugging port.
        registry_path: The registry this supervisor was spawned against.
    """
    line = (
        f"{datetime.now(timezone.utc).isoformat()}\t{name}\tport={port}"
        f"\tpid={os.getpid()}\t{reason}\n"
    )
    try:
        path = supervisor_exit_log_path(registry_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        if path.stat().st_size > _EXIT_LOG_MAX_BYTES:
            _trim_exit_log(path)
    except Exception:
        pass


def _trim_exit_log(path: Path) -> None:
    """Drop the oldest half of the log, keeping whole lines.

    The newest entries are the ones worth reading, and a rotation scheme would
    be more machinery than one line per browser close deserves.
    """
    try:
        kept = path.read_text(encoding="utf-8").splitlines(keepends=True)
        half = len(kept) // 2
        path.write_text("".join(kept[half:]), encoding="utf-8")
    except Exception:
        pass


# How close (CSS px) the pointer must come to the corner badge for it to fade
# out, so moving the mouse toward whatever the badge covers reveals it.
BADGE_YIELD_PX = 48

# Curated palette of vivid, well-separated colors, each dark enough for white
# badge text. A fixed palette (vs. a continuous hue) avoids deceptively-similar
# colors for different instances -- two instances are either an obvious match
# or obviously different, never a near-match that reads as the same.
_PALETTE = (
    "#d11149",  # crimson
    "#c2185b",  # pink
    "#7b1fa2",  # purple
    "#512da8",  # deep purple
    "#303f9f",  # indigo
    "#1976d2",  # blue
    "#0277bd",  # light blue
    "#00838f",  # cyan
    "#00695c",  # teal
    "#2e7d32",  # green
    "#e65100",  # orange
    "#d84315",  # deep orange
    "#5d4037",  # brown
    "#455a64",  # blue grey
)


def derive_color(name: str) -> str:
    """A stable palette color for an instance name (same name, same color)."""
    digest = int(hashlib.md5(name.encode()).hexdigest(), 16)
    return _PALETTE[digest % len(_PALETTE)]


@dataclass(frozen=True)
class BorderIds:
    """DOM names the border uses, derived from the instance name.

    Deterministic rather than random so a restarted supervisor can remove a
    border that an earlier supervisor process of the same instance drew.
    """

    host_id: str
    off_event: str

    @classmethod
    def for_instance(cls, name: str) -> "BorderIds":
        token = hashlib.sha256(name.encode()).hexdigest()[:12]
        return cls(host_id=f"bt-marker-{token}", off_event=f"bt-marker-off-{token}")


_TOP_FRAME_GUARD = "  try { if (window.self !== window.top) return; } catch(e) { return; }"


def build_title_script(*, name: str) -> str:
    """Build the IIFE that keeps ``document.title`` prefixed with the instance name.

    Re-applied idempotently on SPA/title changes. Leaks no globals.

    Runs in the top document only. ``Page.addScriptToEvaluateOnNewDocument``
    evaluates the source in every frame, and only the top document's title is
    the tab title. The ``window.top`` identity comparison is permitted across
    origins; the ``try`` covers sandboxed frames where the access throws, which
    are never the top document either.
    """
    NAME = json.dumps(name)
    return (
        "(() => {"
        f"{_TOP_FRAME_GUARD}"
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


def build_border_script(*, name: str, color: str, ids: BorderIds) -> str:
    """Build the IIFE that draws the border and corner badge.

    Draws a fixed, click-through 6px border and a corner badge inside a closed
    shadow DOM on a host element, and re-draws them if the page wipes them.
    Top frame only (see ``build_title_script``). The badge covers whatever the
    page puts in its top-left corner, so it fades out while the pointer is
    within ``BADGE_YIELD_PX`` of it.

    ``ids.off_event`` dispatched on ``document`` stops the script for good:
    observers and listeners are dropped and the host removed. DOM events cross
    isolated worlds, so one dispatch from the main world stops copies running
    in the marker's isolated world too. Leaks no globals.
    """
    NAME = json.dumps(name)
    COLOR = json.dumps(color)
    HOST = json.dumps(ids.host_id)
    OFF = json.dumps(ids.off_event)
    return (
        "(() => {"
        f"{_TOP_FRAME_GUARD}"
        f"  var NAME={NAME}, COLOR={COLOR}, HOST_ID={HOST}, OFF={OFF};"
        f"  var YIELD_PX = {BADGE_YIELD_PX};"
        "  var badge = null, stopped = false, observers = [];"
        "  function draw(){"
        "    try {"
        "      if (stopped) return true;"
        "      if (!document.documentElement) return false;"
        "      if (document.getElementById(HOST_ID)) return true;"
        "      var host = document.createElement('div');"
        "      host.id = HOST_ID;"
        "      host.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;z-index:2147483647;pointer-events:none;margin:0;padding:0;border:0;background:transparent';"
        "      var html = '<div style=\"position:fixed;top:0;left:0;right:0;bottom:0;border:6px solid ' + COLOR + ';box-sizing:border-box;pointer-events:none\"></div>'"
        "               + '<div style=\"position:fixed;top:0;left:0;background:' + COLOR + ';color:#fff;font:600 12px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;padding:3px 9px;border-bottom-right-radius:6px;pointer-events:none;white-space:nowrap;transition:opacity .15s ease\">\\uD83E\\uDD16 ' + NAME + '</div>';"
        "      var root = host.attachShadow ? host.attachShadow({mode:'closed'}) : host;"
        "      root.innerHTML = html;"
        "      badge = root.children[1];"
        "      document.documentElement.appendChild(host);"
        "      return true;"
        "    } catch(e){ return false; }"
        "  }"
        "  function observe(target, options, callback){ var mo = new MutationObserver(callback); observers.push(mo); mo.observe(target, options); return mo; }"
        "  function onMove(e){"
        "    if (!badge) return;"
        "    var r = badge.getBoundingClientRect();"
        "    badge.style.opacity = (e.clientX < r.right + YIELD_PX && e.clientY < r.bottom + YIELD_PX) ? '0' : '1';"
        "  }"
        "  function onLeave(){ if (badge) badge.style.opacity = '1'; }"
        "  function keepDrawn(){"
        "    try { if (document.documentElement) observe(document.documentElement, {childList:true}, function(){ if (!document.getElementById(HOST_ID)) draw(); }); } catch(e){}"
        "  }"
        "  function stop(){"
        "    stopped = true;"
        "    observers.forEach(function(o){ o.disconnect(); });"
        "    document.removeEventListener('mousemove', onMove, true);"
        "    document.removeEventListener('mouseleave', onLeave, true);"
        "    document.removeEventListener(OFF, stop);"
        "    var h = document.getElementById(HOST_ID); if (h) h.remove();"
        "  }"
        "  try {"
        "    document.addEventListener(OFF, stop);"
        "    document.addEventListener('mousemove', onMove, {capture:true, passive:true});"
        "    document.addEventListener('mouseleave', onLeave, {capture:true, passive:true});"
        "  } catch(e){}"
        "  if (!draw()) { var first = observe(document, {childList:true, subtree:true}, function(){ if (draw()) first.disconnect(); }); }"
        "  if (document.head || document.readyState !== 'loading') { keepDrawn(); }"
        "  else { document.addEventListener('DOMContentLoaded', keepDrawn); }"
        "})();"
    )


def build_border_removal(*, ids: BorderIds) -> str:
    """Build the expression that stops every border script and removes the border."""
    HOST = json.dumps(ids.host_id)
    OFF = json.dumps(ids.off_event)
    return (
        "(() => {"
        f"  document.dispatchEvent(new Event({OFF}));"
        f"  var h = document.getElementById({HOST}); if (h) h.remove();"
        "})();"
    )


@dataclass(frozen=True)
class MarkerScripts:
    """The marker sources for one instance, built once per supervisor."""

    title: str
    border: str
    border_removal: str

    @classmethod
    def for_instance(cls, name: str) -> "MarkerScripts":
        ids = BorderIds.for_instance(name)
        return cls(
            title=build_title_script(name=name),
            border=build_border_script(name=name, color=derive_color(name), ids=ids),
            border_removal=build_border_removal(ids=ids),
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


async def _register_marker_script(cdp: CDPClient, session_id: str, source: str) -> str | None:
    """Register ``source`` to run in every future document of this session.

    It runs in an isolated world, on every navigation. Returns the script
    identifier that ``Page.removeScriptToEvaluateOnNewDocument`` takes.
    """
    added = await cdp.send(
        method="Page.addScriptToEvaluateOnNewDocument",
        params={"source": source, "worldName": ISOLATED_WORLD},
        session_id=session_id,
    )
    return added.get("identifier")


async def _evaluate(cdp: CDPClient, session_id: str, expression: str) -> None:
    """Run ``expression`` once in the session's current document."""
    await cdp.send(
        method="Runtime.evaluate", params={"expression": expression}, session_id=session_id
    )


async def _add_marker_script(cdp: CDPClient, session_id: str, source: str) -> str | None:
    """Register ``source`` for future documents, then run it in the current one."""
    identifier = await _register_marker_script(cdp, session_id, source)
    await _evaluate(cdp, session_id, source)
    return identifier


@dataclass
class TabMark:
    """The marker state of one attached page session.

    ``border_script`` is the identifier of the registered border script, or
    None while no border is registered. ``lock`` serialises the add/remove
    work, which runs both from the tab's own setup and from a setting change.
    """

    session_id: str
    border_script: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


async def _set_tab_border(
    cdp: CDPClient, mark: TabMark, want: bool, scripts: MarkerScripts
) -> None:
    """Add or remove the border on one tab so it matches ``want``. Idempotent.

    The registration identifier is stored the moment Chrome returns it, before
    the current document is touched: a failure between the two (the tab
    navigates, the page goes away) would otherwise lose the identifier, and a
    later "off" could not unregister the script, so every future document in
    that tab would keep drawing the border.

    Turning off always runs the removal expression, even when this supervisor
    holds no identifier. A border can be drawn in the page while this mark is
    fresh: the connection dropped and reconnected, or an earlier supervisor
    process of the same instance drew it. The expression is idempotent and the
    ids are derived from the instance name, so it clears that border too.
    """
    async with mark.lock:
        try:
            if want:
                if mark.border_script is None:
                    mark.border_script = await _register_marker_script(
                        cdp, mark.session_id, scripts.border
                    )
                    await _evaluate(cdp, mark.session_id, scripts.border)
            else:
                if mark.border_script is not None:
                    await cdp.send(
                        method="Page.removeScriptToEvaluateOnNewDocument",
                        params={"identifier": mark.border_script},
                        session_id=mark.session_id,
                    )
                    # Cleared only once Chrome confirms: a failed removal keeps
                    # the identifier so the next change can retry it.
                    mark.border_script = None
                await _evaluate(cdp, mark.session_id, scripts.border_removal)
        except Exception:
            pass  # the tab may close mid-change; other tabs are unaffected


class BorderSetting:
    """The live value of the user's border setting, as the supervisor sees it."""

    def __init__(self, read: Callable[[], bool]) -> None:
        self._read = read
        self.on = self._safe_read(default=True)

    def _safe_read(self, *, default: bool) -> bool:
        try:
            return bool(self._read())
        except Exception:
            return default

    def refresh(self) -> bool:
        """Re-read the setting. Returns True when the value changed."""
        value = self._safe_read(default=self.on)
        changed = value != self.on
        self.on = value
        return changed


async def _setup_session(
    cdp: CDPClient,
    session_id: str,
    scripts: MarkerScripts,
    mark: TabMark,
    border: BorderSetting,
) -> None:
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
        await _add_marker_script(cdp, session_id, scripts.title)
    except Exception:
        # tab may close mid-setup; the guard keeps running for other tabs
        await _resume_target(cdp, session_id)
        return
    await _set_tab_border(cdp, mark, border.on, scripts)


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

    The log line is written first, from this watchdog thread. stderr goes to
    DEVNULL for a spawned supervisor, so the print below reaches nobody; the
    log is the only record that survives.
    """
    if _EXIT_CONTEXT:
        log_supervisor_exit(reason="event loop stalled", **_EXIT_CONTEXT)
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


async def _mark_target(
    cdp: CDPClient,
    target_id: str,
    scripts: MarkerScripts,
    marks: dict[str, TabMark],
    border: BorderSetting,
) -> None:
    """Attach to one page target and install the marker on its session."""
    try:
        result = await cdp.send(
            method="Target.attachToTarget",
            params={"targetId": target_id, "flatten": True},
        )
    except Exception:
        return  # the tab closed between the notice and the attach
    session_id = result.get("sessionId")
    if not session_id:
        return
    mark = TabMark(session_id=session_id)
    marks[target_id] = mark
    await _setup_session(cdp, session_id, scripts, mark, border)


async def _supervise_connection(
    *,
    port: int,
    scripts: MarkerScripts | None,
    border: BorderSetting | None = None,
    heartbeat: Heartbeat | None = None,
) -> None:
    """Hold one browser-level CDP connection until it drops.

    Connects, and (when ``scripts`` is set) installs the window marker on every
    current and future page target, then blocks until the connection drops or
    stops answering. Every second it re-reads the border setting and, when it
    changed, adds or removes the border on every marked tab. Returns when
    disconnected; raises if the connect itself fails. Caller decides whether a
    drop means the browser closed (retire) or was a transient blip (reconnect).
    """
    browser_ws = await get_ws_url_async(port=port, target_type="browser")
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

    marks: dict[str, TabMark] = {}
    if border is None:
        border = BorderSetting(lambda: True)

    if scripts is not None:
        handled: set[str] = set()

        def on_created(params: dict) -> None:
            info = params.get("targetInfo", {})
            target_id = info.get("targetId")
            if not target_id or target_id in handled:
                return
            if info.get("type") != "page":
                return  # not marked, so not attached to either
            handled.add(target_id)
            spawn(_mark_target(cdp, target_id, scripts, marks, border))

        def on_destroyed(params: dict) -> None:
            target_id = params.get("targetId", "")
            handled.discard(target_id)
            marks.pop(target_id, None)

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
            if scripts is not None and border.refresh():
                for mark in list(marks.values()):
                    spawn(_set_tab_border(cdp, mark, border.on, scripts))
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
    border_setting: Callable[[], bool] = lambda: True,
    watchdog: bool = True,
) -> None:
    """Supervise a launched browser until it actually closes.

    Holds a browser-level CDP connection. While alive and ``draw_border`` is
    set, marks every page target: the title prefix always, and the border and
    badge while ``border_setting()`` returns True (re-read every second). The
    instance is retired from the registry (and its session directory removed)
    ONLY when the browser is truly gone -- detected by its CDP port no longer
    listening.

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
    from browser_tools.lifecycle import retire_instance

    heartbeat = Heartbeat()
    if watchdog:
        start_watchdog(heartbeat=heartbeat)
        beat_task = asyncio.get_event_loop().create_task(_beat_forever(heartbeat))
    else:
        beat_task = None

    # Build the marker scripts once; both are idempotent, so a reconnect
    # re-injecting them on live tabs changes nothing.
    scripts = MarkerScripts.for_instance(name) if draw_border else None

    try:
        await _supervise_forever(
            port=port,
            name=name,
            registry_path=registry_path,
            scripts=scripts,
            border=BorderSetting(border_setting),
            heartbeat=heartbeat,
            retire=retire_instance,
        )
    finally:
        if beat_task is not None:
            beat_task.cancel()


async def _supervise_forever(
    *,
    port: int,
    name: str,
    registry_path: str | None,
    scripts: MarkerScripts | None,
    border: BorderSetting,
    heartbeat: Heartbeat,
    retire,
) -> None:
    """Connect, supervise, and reconnect until the browser is gone."""
    while True:
        try:
            await _supervise_connection(
                port=port, scripts=scripts, border=border, heartbeat=heartbeat
            )
        except Exception:
            # A failed (re)connect or a mid-stream CDP error lands here; fall
            # through to the liveness check to decide retire-vs-reconnect.
            pass

        if await _browser_gone(port):
            # Browser really closed -> retire from the registry and exit.
            retire(instance_name=name, registry_path=registry_path)
            if registry_path is not None:
                log_supervisor_exit(
                    reason="browser closed",
                    name=name,
                    port=port,
                    registry_path=registry_path,
                )
            return

        # Transient drop -- the browser is still alive. Reconnect and resume
        # supervising (re-installs the marker on the live tabs) rather than
        # orphaning a live instance.
        await asyncio.sleep(0.5)


def spawn_supervisor(
    *, port: int, name: str, registry_path: str, draw_border: bool
) -> subprocess.Popen:
    """Spawn the detached per-instance supervisor process for a launched browser.

    ``start_new_session`` is what makes "detached" true. Without it the
    supervisor stays in the launching shell's session and process group, so
    closing that terminal SIGHUPs it while the browser -- launched with a new
    session of its own -- lives on. That leaves a live instance nothing marks
    and nothing ever retires (#65).
    """
    return subprocess.Popen(
        [
            sys.executable, "-m", "browser_tools.core.supervisor",
            str(port), name, registry_path, "1" if draw_border else "0",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _log_on_signal(*signals: signal.Signals) -> None:
    """Record the signal that ends this supervisor, then die from it normally.

    A default-disposition SIGTERM (a logout, a reboot, a pkill) kills the
    process without unwinding anything, so the try/except around
    ``asyncio.run`` never sees it. That is the most likely way a supervisor
    dies in the field, and it was the one leaving no trace. The handler logs,
    restores the default disposition, and re-raises, so the exit status still
    says what killed it.
    """

    def handler(signum: int, _frame: object) -> None:
        if _EXIT_CONTEXT:
            log_supervisor_exit(
                reason=f"signal {signal.Signals(signum).name}", **_EXIT_CONTEXT
            )
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in signals:
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, handler)


def main() -> None:
    """Entry point: `python -m browser_tools.core.supervisor PORT NAME REGISTRY_PATH DRAW_BORDER`."""
    if len(sys.argv) < 5:
        print("usage: python -m browser_tools.core.supervisor PORT NAME REGISTRY_PATH DRAW_BORDER", file=sys.stderr)
        sys.exit(2)
    port = int(sys.argv[1])
    name = sys.argv[2]
    registry_path = sys.argv[3]
    draw_border = sys.argv[4] == "1"
    # The setting's owner lives outside the vendored core; this entry point is
    # the one place the supervisor is wired to it.
    from browser_tools.user_settings import window_border_enabled

    _EXIT_CONTEXT.update(name=name, port=port, registry_path=registry_path)
    _log_on_signal(signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    try:
        asyncio.run(run_supervisor(
            port=port,
            name=name,
            registry_path=registry_path,
            draw_border=draw_border,
            border_setting=window_border_enabled,
        ))
    except KeyboardInterrupt:
        log_supervisor_exit(
            reason="interrupted", name=name, port=port, registry_path=registry_path
        )
    except BaseException as exc:
        # Every other way out, including a signal that raises. An unlogged exit
        # is the thing #65 could not explain.
        log_supervisor_exit(
            reason=f"{type(exc).__name__}: {exc}",
            name=name,
            port=port,
            registry_path=registry_path,
        )
        raise


if __name__ == "__main__":
    main()
