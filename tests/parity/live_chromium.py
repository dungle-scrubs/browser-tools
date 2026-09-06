"""A live Chromium session that drives both parity engines over one page.

The native snapshot engine (ticket #39) reads the CDP Accessibility domain,
which is a Chromium capability; Camoufox is Firefox and does not expose
``Accessibility.getFullAXTree``. To compare the native engine against an ARIA
baseline "where meaningful" (ticket #39), this adapter runs *both* engines
against the **same** Playwright Chromium page:

- :class:`PlaywrightChromiumSession` implements ``call_tool`` for
  :class:`~parity_engines.AriaSnapshotEngine` (navigate / snapshot / evaluate),
  and the native ``navigate`` / ``get_full_ax_tree`` / ``evaluate`` methods for
  :class:`~parity_engines.NativeSnapshotEngine`.

Driving both on one browser and one DOM isolates the parity signal to the
snapshot mechanism itself: text and UID targets are computed by identical JS,
so any difference the operator reports is the node set (ARIA-YAML roles vs CDP
AX roles). The authoritative chrome-devtools-mcp Node-vs-native gate is #41.

This module imports Playwright lazily so the default (no-browser) test run never
requires it; :func:`chromium_available` reports whether a live run is possible.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
from typing import Any


def chromium_available() -> tuple[bool, str]:
    """Report whether a Playwright Chromium can be launched here."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False, "playwright is not installed"
    return True, ""


class PlaywrightChromiumSession:
    """A live Chromium page exposed to both parity engines.

    Use as a context manager so the browser and Playwright driver are always
    torn down::

        with PlaywrightChromiumSession() as session:
            aria = AriaSnapshotEngine(session)
            native = NativeSnapshotEngine(session)
    """

    def __init__(self, *, headless: bool = True) -> None:
        self._headless = headless
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._cdp: Any = None
        self._runtime: Any = None
        self._thread: Any = None

    def __enter__(self) -> PlaywrightChromiumSession:
        from playwright.sync_api import sync_playwright

        from browser_tools.cdp_handler import CDPRuntime

        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        browser_args = [f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"]
        self._pw = sync_playwright().start()
        # Prefer the Playwright-managed Chromium; fall back to an installed
        # Google Chrome via the "chrome" channel when the bundled binary was
        # not downloaded (``playwright install`` never run in this environment).
        try:
            self._browser = self._pw.chromium.launch(headless=self._headless, args=browser_args)
        except Exception:
            self._browser = self._pw.chromium.launch(
                headless=self._headless, channel="chrome", args=browser_args
            )
        self._context = self._browser.new_context()
        self._page = self._context.new_page()
        selector = self._context.new_cdp_session(self._page)
        target_id = selector.send("Target.getTargetInfo")["targetInfo"]["targetId"]
        selector.detach()
        self._runtime = CDPRuntime(port, target_id)
        self._thread = threading.Thread(target=self._runtime.run, daemon=True)
        self._thread.start()
        self._runtime.wait_ready(10)
        runtime = self._runtime

        class BoundCoreSession:
            def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                return asyncio.run_coroutine_threadsafe(
                    runtime.send(method, params), runtime.loop
                ).result(10)

        self._cdp = BoundCoreSession()
        self._cdp.send("Accessibility.enable")
        # The native interaction path (ticket #40) addresses nodes by
        # backendNodeId over the DOM/Runtime domains; enable them and prime the
        # DOM node map so backendNodeId -> object resolution is available.
        self._cdp.send("DOM.enable")
        self._cdp.send("Runtime.enable")
        return self

    def __exit__(self, *exc: object) -> None:
        if self._runtime is not None:
            self._runtime.stop()
        if self._thread is not None:
            self._thread.join(6)
        for close in (
            lambda: self._context.close() if self._context else None,
            lambda: self._browser.close() if self._browser else None,
            lambda: self._pw.stop() if self._pw else None,
        ):
            with contextlib.suppress(Exception):
                close()

    # -- native engine interface ------------------------------------------- #

    def navigate(self, url: str) -> None:
        self._page.goto(url, wait_until="load")
        # A fresh document invalidates the CDP DOM node map; re-prime it so
        # backendNodeId addressing (getBoxModel / resolveNode) stays valid.
        self._cdp.send("DOM.getDocument", {"depth": -1})

    def get_full_ax_tree(self) -> dict[str, Any]:
        return self._cdp.send("Accessibility.getFullAXTree")

    def get_stitched_ax_tree(self) -> dict[str, Any]:
        """Full accessibility tree stitched across child frames (ticket #41).

        The synchronous counterpart of
        :func:`~browser_tools.native_snapshot.read_stitched_ax_tree`: it drives the
        same discovery (``Page.getFrameTree`` -> ``DOM.getFrameOwner`` ->
        per-frame ``Accessibility.getFullAXTree``) over the core CDP
        session and reuses the production :func:`stitch_ax_frames` transform, so
        the native parity engines read the cross-frame node set the flipped native
        backend serves.
        """
        from browser_tools.native_snapshot import stitch_ax_frames

        top = self._cdp.send("Accessibility.getFullAXTree")
        self._cdp.send("Page.enable")
        frame_tree = self._cdp.send("Page.getFrameTree")

        frame_ids: list[str] = []

        def walk(node: dict[str, Any], is_root: bool) -> None:
            frame = node.get("frame", {})
            frame_id = frame.get("id")
            if not is_root and frame_id is not None:
                frame_ids.append(str(frame_id))
            for child in node.get("childFrames", []) or []:
                walk(child, False)

        walk(frame_tree.get("frameTree", {}), True)

        child_frames: list[tuple[int, dict[str, Any]]] = []
        for frame_id in frame_ids:
            owner = self._cdp.send("DOM.getFrameOwner", {"frameId": frame_id})
            backend = owner.get("backendNodeId")
            if not isinstance(backend, int):
                continue
            child = self._cdp.send("Accessibility.getFullAXTree", {"frameId": frame_id})
            child_frames.append((backend, child))

        return stitch_ax_frames(top, child_frames)

    def evaluate(self, script: str) -> Any:
        return self._page.evaluate(script)

    def cdp_send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Synchronous CDP transport for the native interaction path (#40).

        Matches the ``send(method, params) -> result`` shape the native
        interaction drivers expect, so the production
        :class:`~browser_tools.native_interaction.NativeInteractor` runs
        unchanged against this live session via its synchronous driver.
        """
        return self._cdp.send(method, params or {})

    # -- AriaSnapshotEngine ``call_tool`` interface ------------------------ #

    def call_tool(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = args or {}
        try:
            if tool == "navigate":
                self._page.goto(args["url"], wait_until="load")
                return {"result": {"url": self._page.url, "title": self._page.title()}}
            if tool == "snapshot":
                return {"result": {"tree": self._page.locator("body").aria_snapshot()}}
            if tool == "evaluate":
                return {"result": {"value": self._page.evaluate(args["script"])}}
            return {"error": f"unsupported tool {tool}"}
        except Exception as exc:  # surface as the engines expect
            return {"error": str(exc)}
