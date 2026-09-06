"""Under supervision, new documents still load -- including a cross-origin iframe.

The regression this guards, against a real browser: a supervisor that attaches
to new targets and then stops resuming them wedges every document created after
that point. New tabs freeze on Chrome's "Debugger paused in another tab" banner,
and a cross-origin iframe (an out-of-process iframe: its own CDP target, of type
``iframe``, not ``page``) sits at ``readyState === "loading"`` with zero network
requests issued. Reloading only makes another paused document, so the browser
never recovers and the human loses the session by restarting it.

Two shapes are covered, each the shape that broke in the field:

- a page holding a **cross-origin iframe**, opened as a new tab, and
- a tab opened by **window.open** from a supervised page,

and both are checked twice: with the supervisor running, and with the supervisor
**stalled** (SIGSTOP). The stalled pass is the real guard -- a supervisor that
cannot act must not be able to stop documents from loading. Marking a window
does not need to gate document start, so the supervisor is not in that path at
all.

``tests/test_supervisor_never_pauses.py`` pins the same guarantee at the unit
level (attach params, resume ordering, the watchdog). This file is the end-to-end
proof and needs a real Chrome; it is skipped where there is none, and in CI.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from browser_tools.core.cdp_client import CDPClient, get_ws_url
from browser_tools.core.launcher import find_chrome_binary
from browser_tools.core.supervisor import spawn_supervisor

_CHROME = find_chrome_binary()

pytestmark = [
    pytest.mark.skipif(_CHROME is None, reason="no Chrome/Chromium binary on this machine"),
    pytest.mark.skipif(os.environ.get("CI") == "true", reason="launches a real browser"),
    pytest.mark.skipif(
        not hasattr(signal, "SIGSTOP"), reason="the stall case needs SIGSTOP (POSIX)"
    ),
]

DOCUMENT_TIMEOUT_SECONDS = 20.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --------------------------------------------------------------------------
# A two-origin site. The parent is served over ``localhost`` and its iframe over
# ``127.0.0.1``: same server, different sites, so Chrome puts the iframe in its
# own process and gives it its own CDP target -- the OOPIF the field failure
# wedged. The child issues a subresource request, so "zero requests issued" is
# observable rather than merely implied.
# --------------------------------------------------------------------------

PARENT_HTML = """<!doctype html>
<html><head><title>parent</title></head><body>
<h1>parent</h1>
<iframe id="f" src="http://127.0.0.1:{port}/child.html" width="300" height="120"></iframe>
</body></html>
"""

CHILD_HTML = """<!doctype html>
<html><head><title>child</title></head><body>
<h1>child</h1>
<script>fetch("ping.txt");</script>
</body></html>
"""


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Serve the two-origin site on loopback for the duration of the module."""
    root = tmp_path_factory.mktemp("site")
    port = _free_port()
    (root / "parent.html").write_text(PARENT_HTML.format(port=port))
    (root / "child.html").write_text(CHILD_HTML)
    (root / "ping.txt").write_text("pong\n")

    handler = partial(_QuietHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "parent": f"http://localhost:{port}/parent.html",
            "child": f"http://127.0.0.1:{port}/child.html",
        }
    finally:
        server.shutdown()
        server.server_close()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture(scope="module")
def supervised_browser(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """A headless Chrome with the real supervisor process attached to it.

    Chrome is launched directly (rather than through ``launch_browser``) so the
    test owns its user-data-dir and never touches the shared session root; the
    supervisor is the real spawned process, marking on.
    """
    assert _CHROME is not None
    port = _free_port()
    user_data_dir = tmp_path_factory.mktemp("chrome-profile")
    registry_path = str(tmp_path_factory.mktemp("registry") / "registry.json")

    browser = subprocess.Popen(
        [
            _CHROME,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--headless=new",
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    supervisor: subprocess.Popen | None = None
    try:
        # A setup that raises here (no CDP, no marker) must still take the
        # browser and the supervisor down with it, or the run leaks both.
        _wait_for_cdp(port)
        supervisor = spawn_supervisor(
            port=port, name="liveness-test", registry_path=registry_path, draw_border=True
        )
        _wait_for_marker(port)
        yield {"port": port, "supervisor": supervisor}
    finally:
        if supervisor is not None:
            with _ignore_process_errors():
                supervisor.send_signal(signal.SIGCONT)  # in case a test left it stalled
            supervisor.terminate()
        browser.terminate()
        with _ignore_process_errors():
            browser.wait(timeout=10)
        shutil.rmtree(user_data_dir, ignore_errors=True)


class _ignore_process_errors:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, *rest: Any) -> bool:
        return exc_type is not None and issubclass(exc_type, (OSError, subprocess.TimeoutExpired))


def _wait_for_cdp(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read()
            return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError(f"Chrome never listened on {port}")


def _wait_for_marker(port: int, timeout: float = 30.0) -> None:
    """Wait until the supervisor has marked a tab, i.e. it is attached and working."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2).read()
            if any("liveness-test" in target.get("title", "") for target in json.loads(raw)):
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise TimeoutError("the supervisor never marked a tab")


# --------------------------------------------------------------------------
# CDP helpers: open documents and read back how far they actually got.
# --------------------------------------------------------------------------


CALL_TIMEOUT_SECONDS = 15.0


async def _send(cdp: CDPClient, method: str, **kwargs: Any) -> dict[str, Any]:
    """One CDP call with a hard deadline, so a wedge fails instead of hanging."""
    try:
        return await asyncio.wait_for(cdp.send(method=method, **kwargs), CALL_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise AssertionError(f"{method} never answered within {CALL_TIMEOUT_SECONDS}s") from exc


async def _document_state(cdp: CDPClient, target_id: str) -> dict[str, Any]:
    """``readyState`` and issued-request count for one target."""
    session = await _send(
        cdp, "Target.attachToTarget", params={"targetId": target_id, "flatten": True}
    )
    result = await _send(
        cdp,
        "Runtime.evaluate",
        params={
            "expression": (
                "({state: document.readyState,"
                " requests: performance.getEntriesByType('resource').length})"
            ),
            "returnByValue": True,
        },
        session_id=session["sessionId"],
    )
    return result["result"]["value"]


async def _await_complete(cdp: CDPClient, *, url: str, kind: str) -> dict[str, Any]:
    """Wait for the target serving ``url`` to reach ``readyState === 'complete'``.

    A wedged document fails here the way it failed in the field: it stays at
    ``loading`` (or never appears at all) until the deadline.
    """
    deadline = time.monotonic() + DOCUMENT_TIMEOUT_SECONDS
    last: Any = "no such target"
    while time.monotonic() < deadline:
        targets = await _send(cdp, "Target.getTargets")
        for info in targets["targetInfos"]:
            if info["type"] != kind or info["url"] != url:
                continue
            try:
                last = await _document_state(cdp, info["targetId"])
            except Exception as exc:  # a paused target can stop answering
                last = f"{type(exc).__name__}: {exc}"
                break
            if last["state"] == "complete":
                return last
        await asyncio.sleep(0.25)
    raise AssertionError(f"{kind} {url} never completed: {last}")


async def _open_tab_and_check(port: int, site: dict[str, str]) -> None:
    """A new tab with a cross-origin iframe: both documents must finish."""
    cdp = CDPClient(ws_url=get_ws_url(port=port, target_type="browser"))
    await cdp.connect()
    try:
        await _send(cdp, "Target.createTarget", params={"url": site["parent"]})
        await _await_complete(cdp, url=site["parent"], kind="page")
        child = await _await_complete(cdp, url=site["child"], kind="iframe")
        # The wedged OOPIF issued no requests at all; a live one issued its fetch.
        assert child["requests"] > 0, f"the iframe issued no requests: {child}"
    finally:
        await cdp.close()


async def _window_open_and_check(port: int, site: dict[str, str]) -> None:
    """A tab opened by the page itself (window.open) must finish loading too."""
    cdp = CDPClient(ws_url=get_ws_url(port=port, target_type="browser"))
    await cdp.connect()
    try:
        opener = await _send(cdp, "Target.createTarget", params={"url": site["parent"]})
        await _await_complete(cdp, url=site["parent"], kind="page")
        session = await _send(
            cdp, "Target.attachToTarget", params={"targetId": opener["targetId"], "flatten": True}
        )
        popped = f"{site['parent']}?popped=1"
        await _send(
            cdp,
            "Runtime.evaluate",
            # userGesture, or Chrome's popup blocker eats the window.open.
            params={"expression": f"window.open({popped!r}, '_blank')", "userGesture": True},
            session_id=session["sessionId"],
        )
        await _await_complete(cdp, url=popped, kind="page")
    finally:
        await cdp.close()


# --------------------------------------------------------------------------
# The tests.
# --------------------------------------------------------------------------


def test_a_cross_origin_iframe_loads_under_supervision(supervised_browser, site) -> None:
    asyncio.run(_open_tab_and_check(supervised_browser["port"], site))


def test_a_window_open_tab_loads_under_supervision(supervised_browser, site) -> None:
    asyncio.run(_window_open_and_check(supervised_browser["port"], site))


def test_a_stalled_supervisor_does_not_wedge_new_documents(supervised_browser, site) -> None:
    """The field failure, reproduced as far as it can be: freeze the supervisor
    mid-flight and keep opening documents. A supervisor that never pauses a
    target has nothing to release, so a frozen one changes nothing."""
    supervisor = supervised_browser["supervisor"]
    supervisor.send_signal(signal.SIGSTOP)
    try:
        asyncio.run(_open_tab_and_check(supervised_browser["port"], site))
        asyncio.run(_window_open_and_check(supervised_browser["port"], site))
    finally:
        supervisor.send_signal(signal.SIGCONT)


def test_the_supervisor_attaches_to_page_targets_only(supervised_browser, site) -> None:
    """Marking is a top-document job. An OOPIF, a worker, or an extension page
    is protocol traffic the supervisor would have to keep draining, and a
    session it could leave paused, for nothing it uses."""

    async def check() -> None:
        # One connection opens the page and waits for the iframe, attaching to
        # targets as it probes them; it is closed before the census so what is
        # left attached is the supervisor's doing alone.
        opener = CDPClient(
            ws_url=get_ws_url(port=supervised_browser["port"], target_type="browser")
        )
        await opener.connect()
        try:
            await _send(opener, "Target.createTarget", params={"url": site["parent"]})
            await _await_complete(opener, url=site["child"], kind="iframe")
        finally:
            await opener.close()
        await asyncio.sleep(1)  # let Chrome retire the closed connection's sessions

        census = CDPClient(
            ws_url=get_ws_url(port=supervised_browser["port"], target_type="browser")
        )
        await census.connect()
        try:
            targets = await _send(census, "Target.getTargets")
        finally:
            await census.close()
        attached = {info["type"] for info in targets["targetInfos"] if info.get("attached")}
        assert attached <= {"page"}, f"supervisor attached to {attached}"

    asyncio.run(check())


if __name__ == "__main__":  # pragma: no cover - convenience for a manual run
    sys.exit(pytest.main([__file__, "-v"]))
