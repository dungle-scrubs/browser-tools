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
# and only the top document's title is the tab title. Intra-package imports
# are rewritten to browser_tools.core. Otherwise unchanged from chrome-agent
# v0.5.7. See RFC-01, section "Vendoring rules".

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
"""

import asyncio
import json
import subprocess
import sys

from .cdp_client import CDPClient, get_ws_url

ISOLATED_WORLD = "__chrome_agent_marker__"

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


async def _setup_session(cdp: CDPClient, session_id: str, source: str) -> None:
    """Register the marker on a page session: future docs + the current doc."""
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
        pass  # tab may close mid-setup; the guard keeps running for other tabs


async def _supervise_connection(
    *, port: int, draw_border: bool, source: str | None
) -> None:
    """Hold one browser-level CDP connection until it drops.

    Connects, and (when ``draw_border`` and ``source`` are set) installs the
    window marker on every current and future page target, then blocks until the
    connection drops. Returns when disconnected; raises if the connect itself
    fails. Caller decides whether a drop means the browser closed (retire) or was
    a transient blip (reconnect).
    """
    browser_ws = get_ws_url(port=port, target_type="browser")
    cdp = CDPClient(ws_url=browser_ws)
    await cdp.connect()

    if draw_border and source is not None:
        loop = asyncio.get_event_loop()
        handled: set[str] = set()

        def on_attached(params: dict) -> None:
            info = params.get("targetInfo", {})
            session_id = params.get("sessionId")
            if info.get("type") != "page" or not session_id or session_id in handled:
                return
            handled.add(session_id)
            loop.create_task(_setup_session(cdp, session_id, source))

        cdp.on(event="Target.attachedToTarget", callback=on_attached)

        # autoAttach with flatten attaches to all current AND future page targets,
        # firing Target.attachedToTarget (with a sessionId) for each.
        await cdp.send(
            method="Target.setAutoAttach",
            params={"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )

    try:
        while cdp._connected:
            await asyncio.sleep(1)
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
    *, port: int, name: str, registry_path: str | None = None, draw_border: bool = True
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
    """
    from .registry import deregister

    # Build the marker script once; the title prefix is idempotent, so a
    # reconnect re-injecting it on live tabs changes nothing.
    source: str | None = None
    if draw_border:
        source = build_overlay_script(name=name)

    while True:
        try:
            await _supervise_connection(port=port, draw_border=draw_border, source=source)
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
