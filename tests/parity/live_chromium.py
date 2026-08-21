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

import contextlib
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

    def __enter__(self) -> PlaywrightChromiumSession:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        # Prefer the Playwright-managed Chromium; fall back to an installed
        # Google Chrome via the "chrome" channel when the bundled binary was
        # not downloaded (``playwright install`` never run in this environment).
        try:
            self._browser = self._pw.chromium.launch(headless=self._headless)
        except Exception:
            self._browser = self._pw.chromium.launch(headless=self._headless, channel="chrome")
        self._context = self._browser.new_context()
        self._page = self._context.new_page()
        self._cdp = self._context.new_cdp_session(self._page)
        self._cdp.send("Accessibility.enable")
        # The native interaction path (ticket #40) addresses nodes by
        # backendNodeId over the DOM/Runtime domains; enable them and prime the
        # DOM node map so backendNodeId -> object resolution is available.
        self._cdp.send("DOM.enable")
        self._cdp.send("Runtime.enable")
        return self

    def __exit__(self, *exc: object) -> None:
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
