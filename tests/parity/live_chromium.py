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
    """Report whether the Playwright package is importable here.

    It does not check that a browser binary was ever downloaded, so it can
    answer yes on a machine where the launch will fail. That is deliberate and
    safe only because :class:`PlaywrightChromiumSession` raises on a failed
    launch and every caller turns that into a skip naming the reason. It was
    not safe while the session silently substituted the developer's own Chrome
    for the missing one.
    """
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
        # The Playwright-managed Chromium, and nothing else.
        #
        # This used to fall back to the developer's installed Google Chrome
        # through the ``chrome`` channel whenever the bundled build was
        # missing. That substituted their own logged-in browser for the one
        # under test, silently: the parity comparison then ran against a
        # different build than the baseline it is checked against, and the
        # suite stayed green while comparing the wrong thing. On macOS it was
        # worse than wrong. Chrome's app bundle aborts during application
        # registration when its inner binary is executed directly, so each
        # fallback killed a Chrome and raised a system crash dialog on the
        # developer's screen, attributed to whatever they last had open.
        #
        # Every caller already skips when this raises, naming the reason, and
        # the ``parity`` marker is documented as "skips when no browser is
        # available". Letting it raise is that documented behaviour. A missing
        # browser is not a licence to reach for the user's.
        try:
            self._browser = self._pw.chromium.launch(headless=self._headless)
        except Exception as exc:
            # ``__exit__`` never runs when ``__enter__`` raises, so the driver
            # this method started has to be stopped here or it is leaked.
            with contextlib.suppress(Exception):
                self._pw.stop()
            self._pw = None
            raise RuntimeError(
                f"{exc}\n\nThe parity suite needs the Playwright-managed "
                "Chromium. Install it with:\n"
                "    python -m playwright install chromium"
            ) from exc
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

    def get_stitched_ax_tree(self) -> dict[str, Any]:
        """Full accessibility tree stitched across child frames (ticket #41).

        The synchronous counterpart of
        :func:`~browser_tools.native_snapshot.read_stitched_ax_tree`: it drives the
        same discovery (``Page.getFrameTree`` -> ``DOM.getFrameOwner`` ->
        per-frame ``Accessibility.getFullAXTree``) over Playwright's sync CDP
        session and reuses the production :func:`stitch_ax_frames` transform, so
        the native parity engines read the cross-frame node set the flipped native
        backend serves.
        """
        from browser_tools.native_snapshot import (
            ChildFrameTree,
            doc_token_from_frame,
            stitch_ax_frames,
        )

        top = self._cdp.send("Accessibility.getFullAXTree")
        self._cdp.send("Page.enable")
        frame_tree = self._cdp.send("Page.getFrameTree")
        top_frame = frame_tree.get("frameTree", {}).get("frame", {})

        pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []

        def walk(node: dict[str, Any]) -> None:
            frame = node.get("frame", {})
            for child in node.get("childFrames", []) or []:
                child_frame = child.get("frame", {})
                if child_frame.get("id") is not None:
                    pairs.append((child_frame, frame))
                walk(child)

        walk(frame_tree.get("frameTree", {}))

        child_frames: list[ChildFrameTree] = []
        for child_frame, parent_frame in pairs:
            frame_id = str(child_frame.get("id"))
            owner = self._cdp.send("DOM.getFrameOwner", {"frameId": frame_id})
            backend = owner.get("backendNodeId")
            if not isinstance(backend, int):
                continue
            child = self._cdp.send("Accessibility.getFullAXTree", {"frameId": frame_id})
            child_frames.append(
                ChildFrameTree(
                    owner_backend_node_id=backend,
                    owner_doc_token=doc_token_from_frame(parent_frame),
                    doc_token=doc_token_from_frame(child_frame),
                    result=child,
                )
            )

        return stitch_ax_frames(top, child_frames, top_doc_token=doc_token_from_frame(top_frame))

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
