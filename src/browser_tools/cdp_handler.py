#!/usr/bin/env python3
"""CDP handler and toolset definitions for browser-tools daemon.

Tool handler methods for CDP domain operations: frame management,
accessibility, content extraction, export, and screencast.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    from .cdp_constants import (
        INTERSTITIAL_AUTO_RETRY_TYPES,
        INTERSTITIAL_MAX_RETRIES,
        INTERSTITIAL_RETRY_DELAY_SECONDS,
        REQUEST_TIMEOUT_SECONDS,
        SCREENSHOT_PAINT_READY_TIMEOUT_MS,
    )
except ImportError:
    from cdp_constants import (  # type: ignore[import-untyped,no-redef]
        INTERSTITIAL_AUTO_RETRY_TYPES,
        INTERSTITIAL_MAX_RETRIES,
        INTERSTITIAL_RETRY_DELAY_SECONDS,
        REQUEST_TIMEOUT_SECONDS,
        SCREENSHOT_PAINT_READY_TIMEOUT_MS,
    )


class ToolInvocationError(Exception):
    """Raised when a CDP-backed tool fails during execution.

    Wraps the underlying cause so callers get the original exception
    chain plus a tool-call-scoped context.
    """

    def __init__(self, method: str, cause: BaseException) -> None:
        super().__init__(f"{method}: {cause}")
        self.method = method
        self.cause = cause


def _get_cdp_error_class() -> type[Exception]:
    """Import CDPError lazily to avoid breaking script-mode execution.

    cdp_client.py uses relative imports that fail when modules are
    loaded outside of the browser_tools package (e.g. --help flag
    parsing in the daemon script).  Deferring the import until a CDP
    call is actually attempted keeps the module importable in all
    execution modes.
    """
    try:
        from .cdp_client import CDPError  # type: ignore[import-untyped,reportMissingImports]
    except ImportError:
        from cdp_client import (  # type: ignore[import-untyped,reportMissingImports]
            CDPError,  # type: ignore[no-redef]
        )
    return CDPError


async def _safe_cdp_send(
    cdp_client: Any, method: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Send a CDP command, raising ToolInvocationError on failure.

    Args:
        cdp_client: Connected CDPClient.
        method: CDP method name.
        params: Optional parameters dict.

    Returns:
        Parsed result dict from CDP.

    Raises:
        ToolInvocationError: Wraps CDPError (expected CDP protocol
            failures) so callers can distinguish them from unexpected
            exceptions.
    """
    CDPError = _get_cdp_error_class()
    try:
        if params is not None:
            return await cdp_client.send(method, params)
        return await cdp_client.send(method)
    except CDPError as exc:
        raise ToolInvocationError(method, exc) from exc


class CDPHandler:
    """Manages CDP client and frame manager in a dedicated asyncio event loop.

    Runs in a background thread with its own event loop. Tool calls from
    the main thread are dispatched via thread-safe call_tool().
    """

    def __init__(
        self,
        browser_url: str | None,
        mode: str = "full",
        stealth: bool = False,
    ) -> None:
        """Initialize the CDP handler.

        Args:
            browser_url: Chrome remote debugging URL.
            mode: Access mode ('full' or 'inspect').
            stealth: Whether to inject stealth patches to reduce automation fingerprinting.
        """
        self._browser_url = browser_url
        self._mode = mode
        self._stealth = stealth
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stop_event: asyncio.Event | None = None
        self._cdp_client: Any = None
        self._frame_manager: Any = None
        # Screencast capture state (Page.startScreencast → Page.screencastFrame).
        self._screencast_active: bool = False
        self._screencast_frames: list[dict[str, Any]] = []
        self._screencast_max_frames: int = 600
        self._screencast_format: str = "jpeg"

    @property
    def available(self) -> bool:
        """Whether the CDP client is connected and ready."""
        return self._cdp_client is not None and self._cdp_client.connected

    @property
    def mode(self) -> str:
        """Current access mode ('full' or 'inspect')."""
        return self._mode

    def run(self) -> None:
        """Run the asyncio event loop (called in a background thread)."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()
            self._loop = None

    async def _main(self) -> None:
        """Main async entry point."""
        from .frame_manager import FrameManager

        self._frame_manager = FrameManager()

        if self._browser_url:
            await self._connect_cdp()

        self._ready.set()

        # Wait until stopped
        if self._stop_event:
            await self._stop_event.wait()

        if self._cdp_client:
            await self._cdp_client.disconnect()

    async def _connect_cdp(self) -> None:
        """Connect the CDP client to Chrome."""
        try:
            from .cdp_client import CDPClient, get_page_ws_url

            browser_url: str = self._browser_url  # type: ignore[assignment]  # guarded by if-self._browser_url
            ws_url = get_page_ws_url(browser_url)
            if not ws_url:
                return

            self._cdp_client = CDPClient(ws_url)
            await self._cdp_client.connect()

            # Enable Page domain for frame events
            await self._cdp_client.send("Page.enable")
            await self._cdp_client.send("Runtime.enable")

            # Inject stealth patches before any page JS runs
            if self._stealth:
                await self._inject_stealth()

            # Get initial frame tree
            result = await self._cdp_client.send("Page.getFrameTree")
            if "frameTree" in result:
                self._frame_manager.update_from_frame_tree(result["frameTree"])

            # Subscribe to frame lifecycle events only (D-001)
            self._cdp_client.on("Page.frameAttached", self._frame_manager.handle_frame_attached)
            self._cdp_client.on("Page.frameDetached", self._frame_manager.handle_frame_detached)
            self._cdp_client.on("Page.frameNavigated", self._frame_manager.handle_frame_navigated)
            self._cdp_client.on(
                "Runtime.executionContextCreated",
                self._frame_manager.handle_execution_context_created,
            )
            self._cdp_client.on(
                "Runtime.executionContextDestroyed",
                self._frame_manager.handle_execution_context_destroyed,
            )
        except Exception:
            logger.exception(
                "CDP connection failed for %s", self._browser_url
            )
            self._cdp_client = None

    async def _inject_stealth(self) -> None:
        """Inject stealth.js via Page.addScriptToEvaluateOnNewDocument.

        Runs before any page JavaScript on every navigation. Reduces
        automation fingerprinting by patching navigator.webdriver,
        plugins, WebGL renderer, etc.
        """
        stealth_path = Path(__file__).parent / "stealth.js"
        try:
            script = stealth_path.read_text()
            await self._cdp_client.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": script},
            )
        except Exception:
            logger.debug("Stealth injection failed", exc_info=True)
            # Non-fatal -- stealth is best-effort

    def stop(self) -> None:
        """Signal the background loop to stop."""
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a CDP/frame tool (thread-safe, blocks until complete).

        Args:
            name: Tool name.
            arguments: Tool arguments.

        Returns:
            JSON-RPC style response dict.
        """
        if not self._loop or not self._loop.is_running():
            return make_error("CDP handler not initialized")

        future = asyncio.run_coroutine_threadsafe(self._dispatch_tool(name, arguments), self._loop)
        try:
            return future.result(timeout=REQUEST_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("call_tool(%s) error: %s", name, exc)
            future.cancel()
            return make_error(str(exc))

    async def _dispatch_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Route a tool call to the appropriate handler.

        Args:
            name: Tool name.
            arguments: Tool arguments.

        Returns:
            JSON-RPC style response dict.
        """
        if name == "list_frames":
            return self._handle_list_frames()
        elif name == "select_frame":
            return self._handle_select_frame(arguments)
        elif name == "reset_frame":
            return self._handle_reset_frame()
        elif name == "get_frame_events":
            return self._handle_get_frame_events()
        elif name == "get_frame_storage":
            return await self._handle_get_frame_storage(arguments)
        # Accessibility tools
        elif name == "ax_find":
            return await self._handle_ax_find(arguments)
        elif name == "ax_node":
            return await self._handle_ax_node(arguments)
        # Page export/capture
        elif name == "export_pdf":
            return await self._handle_export_pdf(arguments)
        elif name == "screenshot_element":
            return await self._handle_screenshot_element(arguments)
        elif name == "screencast_start":
            return await self._handle_screencast_start(arguments)
        elif name == "screencast_stop":
            return await self._handle_screencast_stop(arguments)
        # Semantic waits
        elif name == "wait_idle":
            return await self._handle_wait_idle(arguments)
        elif name == "wait_stable":
            return await self._handle_wait_stable(arguments)
        # Content extraction
        elif name == "get_text":
            return await self._handle_get_text(arguments)
        elif name == "get_html":
            return await self._handle_get_html(arguments)
        elif name == "get_attr":
            return await self._handle_get_attr(arguments)
        # Element queries
        elif name == "element_exists":
            return await self._handle_element_exists(arguments)
        elif name == "element_visible":
            return await self._handle_element_visible(arguments)
        else:
            return make_error(f"Unknown CDP tool: {name}")

    def _handle_list_frames(self) -> dict[str, Any]:
        """Handle list_frames tool."""
        fm = self._frame_manager
        if fm is None:
            return make_error("Frame manager not initialized")

        frames = fm.get_flat_frames()
        if not frames:
            # Try refreshing frame tree if CDP is available
            if self._cdp_client and self._cdp_client.connected:
                _ = asyncio.ensure_future(self._refresh_frame_tree())  # noqa: RUF006
                return make_text("No frames available. Refreshing frame tree...")

            return make_text("No frames available. CDP client not connected.")

        lines = ["Frames in current page:\n"]
        for frame in frames:
            indent = "  " * frame["depth"]
            selected = " [selected]" if frame["frameId"] == fm.selected_frame_id else ""
            name = f' name="{frame["name"]}"' if frame.get("name") else ""
            lines.append(f"{indent}{frame['frameId']}: {frame['url']}{name}{selected}")
        return make_text("\n".join(lines))

    def _handle_select_frame(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle select_frame tool."""
        fm = self._frame_manager
        if fm is None:
            return make_error("Frame manager not initialized")

        url_pattern = arguments.get("url_pattern", "")
        if not url_pattern:
            return make_error("url_pattern is required")

        frame = fm.select_frame_by_url(url_pattern)
        if frame is None:
            return make_error(
                f"E002: No frame found matching '{url_pattern}'. "
                "Use list_frames to see available frames."
            )

        ctx_id = fm.get_selected_execution_context_id()
        ctx_info = f", executionContextId={ctx_id}" if ctx_id else " (no execution context yet)"
        return make_text(
            f"Selected frame: {frame.frame_id}\n"
            f"URL: {frame.url}\n"
            f"Origin: {frame.security_origin}{ctx_info}"
        )

    def _handle_reset_frame(self) -> dict[str, Any]:
        """Handle reset_frame tool."""
        fm = self._frame_manager
        if fm is None:
            return make_error("Frame manager not initialized")

        fm.reset_frame()
        return make_text("Frame selection cleared. Now targeting top-level page.")

    def _handle_get_frame_events(self) -> dict[str, Any]:
        """Handle get_frame_events tool."""
        fm = self._frame_manager
        if fm is None:
            return make_error("Frame manager not initialized")

        events = fm.drain_events()
        if not events:
            return make_text("No frame events since last check.")

        lines = [f"{len(events)} frame event(s):\n"]
        for evt in events:
            url_info = f" → {evt['url']}" if evt.get("url") else ""
            lines.append(f"  [{evt['type']}] {evt['frameId']}{url_info}")
        return make_text("\n".join(lines))

    async def _handle_get_frame_storage(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle get_frame_storage tool."""
        fm = self._frame_manager
        cdp = self._cdp_client
        if fm is None or cdp is None or not cdp.connected:
            return make_error("CDP client not connected")

        selected = fm.get_selected_frame()
        if selected is None:
            return make_error("No frame selected. Use select_frame first.")

        storage_types = arguments.get(
            "storage_types", ["cookies", "localStorage", "sessionStorage"]
        )
        ctx_id = fm.get_selected_execution_context_id()
        result_parts: list[str] = []

        if "cookies" in storage_types:
            try:
                cookies_result = await _safe_cdp_send(
                    cdp, "Network.getCookies", {"urls": [selected.url]}
                )
                cookies = cookies_result.get("cookies", [])
                result_parts.append(f"Cookies ({len(cookies)}):")
                for c in cookies[:20]:
                    result_parts.append(f"  {c.get('name')}: {c.get('value', '')[:50]}")
            except ToolInvocationError as exc:
                result_parts.append(f"Cookies: error - {exc.cause}")

            except Exception:
                logger.exception("Storage fetch error (Cookies)")

                result_parts.append("Cookies: error - unexpected")

        if ctx_id and "localStorage" in storage_types:
            try:
                ls_result = await _safe_cdp_send(
                    cdp,
                    "Runtime.evaluate",
                    {
                        "expression": "JSON.stringify(Object.fromEntries(Object.entries(localStorage).slice(0, 20)))",
                        "contextId": ctx_id,
                        "returnByValue": True,
                    },
                )
                ls_value = ls_result.get("result", {}).get("value", "{}")
                result_parts.append(f"\nlocalStorage: {ls_value}")
            except ToolInvocationError as exc:
                result_parts.append(f"\nlocalStorage: error - {exc.cause}")

            except Exception:
                logger.exception("Storage fetch error (\nlocalStorage)")

                result_parts.append("\nlocalStorage: error - unexpected")

        if ctx_id and "sessionStorage" in storage_types:
            try:
                ss_result = await _safe_cdp_send(
                    cdp,
                    "Runtime.evaluate",
                    {
                        "expression": "JSON.stringify(Object.fromEntries(Object.entries(sessionStorage).slice(0, 20)))",
                        "contextId": ctx_id,
                        "returnByValue": True,
                    },
                )
                ss_value = ss_result.get("result", {}).get("value", "{}")
                result_parts.append(f"\nsessionStorage: {ss_value}")
            except ToolInvocationError as exc:
                result_parts.append(f"\nsessionStorage: error - {exc.cause}")

            except Exception:
                logger.exception("Storage fetch error (\nsessionStorage)")

                result_parts.append("\nsessionStorage: error - unexpected")

        if not result_parts:
            return make_text("No storage data retrieved.")

        return make_text("\n".join(result_parts))

    # ------------------------------------------------------------------ #
    # Accessibility tools                                                  #
    # ------------------------------------------------------------------ #

    async def _handle_ax_find(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Find accessibility nodes by role and/or accessible name.

        Args:
            arguments: Tool arguments with optional 'role' and 'name' keys.

        Returns:
            JSON-RPC style response dict with list of matching AX nodes.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return make_error("CDP client not connected")

        role = arguments.get("role", "").strip()
        name = arguments.get("name", "").strip()

        if not role and not name:
            return make_error("E007: ax_find requires at least one of 'role' or 'name'")

        params: dict[str, Any] = {}
        if role:
            params["role"] = role
        if name:
            params["accessibleName"] = name

        try:
            result = await _safe_cdp_send(cdp, "Accessibility.queryAXTree", params)
        except ToolInvocationError as exc:
            return make_error(f"Accessibility.queryAXTree failed: {exc.cause}")

        except Exception:
            logger.exception(
                "Unexpected error in Accessibility.queryAXTree failed"
            )

            return make_error("Accessibility.queryAXTree failed")

        nodes = result.get("nodes", [])
        # Filter out ignored nodes
        visible_nodes = [n for n in nodes if not n.get("ignored", False)]

        if not visible_nodes:
            criteria = []
            if role:
                criteria.append(f"role={role!r}")
            if name:
                criteria.append(f"name={name!r}")
            return make_text(f"No accessibility nodes found matching {', '.join(criteria)}.")

        lines = [f"{len(visible_nodes)} node(s) found:\n"]
        for node in visible_nodes:
            node_role = node.get("role", {}).get("value", "unknown")
            node_name = node.get("name", {}).get("value", "")
            node_id = node.get("nodeId", "")
            backend_id = node.get("backendDOMNodeId", "")

            # Extract key properties
            props = {}
            for prop in node.get("properties", []):
                k = prop.get("name", "")
                v = prop.get("value", {}).get("value", "")
                if k in ("disabled", "checked", "expanded", "required", "selected", "focused"):
                    props[k] = v

            prop_str = ""
            if props:
                prop_str = " " + " ".join(f"{k}={v}" for k, v in props.items())

            name_str = f' "{node_name}"' if node_name else ""
            lines.append(
                f"  [{node_role}]{name_str}{prop_str} (nodeId={node_id} backendDOMNodeId={backend_id})"
            )

        return make_text("\n".join(lines))

    async def _handle_ax_node(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Inspect a single element's accessibility properties.

        Args:
            arguments: Tool arguments with 'selector' key.

        Returns:
            JSON-RPC style response dict with AX node properties.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return make_error("CDP client not connected")

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        # Resolve selector to a backend DOM node ID
        try:
            eval_result = await _safe_cdp_send(
                cdp,
                "Runtime.evaluate",
                {
                    "expression": f"document.querySelector({selector!r})",
                    "returnByValue": False,
                },
            )
        except ToolInvocationError as exc:
            return make_error(f"Runtime.evaluate failed: {exc.cause}")

        except Exception:
            logger.exception("Unexpected error in Runtime.evaluate failed")

            return make_error("Runtime.evaluate failed")

        remote_obj = eval_result.get("result", {})
        if remote_obj.get("type") == "undefined" or remote_obj.get("subtype") == "null":
            return make_error(f"E006: No element found matching selector '{selector}'")

        object_id = remote_obj.get("objectId")
        if not object_id:
            return make_error(f"E006: No element found matching selector '{selector}'")

        # Get the backend DOM node ID
        try:
            node_result = await _safe_cdp_send(cdp, "DOM.requestNode", {"objectId": object_id})
        except ToolInvocationError as exc:
            return make_error(f"DOM.requestNode failed: {exc.cause}")

        except Exception:
            logger.exception("Unexpected error in DOM.requestNode failed")

            return make_error("DOM.requestNode failed")

        backend_node_id = node_result.get("nodeId")
        if not backend_node_id:
            return make_error("Could not resolve element to DOM node")

        # Get partial AX tree for this node
        try:
            ax_result = await _safe_cdp_send(
                cdp,
                "Accessibility.getPartialAXTree",
                {"nodeId": backend_node_id, "fetchRelatives": False},
            )
        except ToolInvocationError as exc:
            return make_error(f"Accessibility.getPartialAXTree failed: {exc.cause}")

        except Exception:
            logger.exception(
                "Unexpected error in Accessibility.getPartialAXTree failed"
            )

            return make_error("Accessibility.getPartialAXTree failed")

        nodes = ax_result.get("nodes", [])
        if not nodes:
            return make_text(f"No accessibility info found for selector '{selector}'")

        # Find the primary node (the one we requested)
        node = nodes[0]
        for n in nodes:
            if n.get("backendDOMNodeId") == backend_node_id:
                node = n
                break

        role = node.get("role", {}).get("value", "unknown")
        name = node.get("name", {}).get("value", "")
        description = node.get("description", {}).get("value", "")
        ignored = node.get("ignored", False)

        lines = [
            f"Accessibility properties for '{selector}':",
            f"  role: {role}",
            f"  name: {name!r}",
        ]
        if description:
            lines.append(f"  description: {description!r}")
        if ignored:
            lines.append("  ignored: true (not in accessibility tree)")

        for prop in node.get("properties", []):
            k = prop.get("name", "")
            v = prop.get("value", {}).get("value", "")
            lines.append(f"  {k}: {v}")

        return make_text("\n".join(lines))

    # ------------------------------------------------------------------ #
    # Page export / capture tools                                          #
    # ------------------------------------------------------------------ #

    async def _handle_export_pdf(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Export the current page as a PDF file.

        Args:
            arguments: Tool arguments with optional 'path', 'landscape',
                'print_background' keys.

        Returns:
            JSON-RPC style response dict with output file path.
        """
        import base64
        import time

        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return make_error("CDP client not connected")

        landscape = arguments.get("landscape", False)
        print_background = arguments.get("print_background", True)
        out_path = arguments.get("path", "")
        if not out_path:
            out_path = f"{int(time.time())}_page.pdf"

        try:
            result = await _safe_cdp_send(
                cdp,
                "Page.printToPDF",
                {
                    "landscape": landscape,
                    "printBackground": print_background,
                    "transferMode": "ReturnAsBase64",
                },
            )
        except ToolInvocationError as exc:
            return make_error(f"Page.printToPDF failed: {exc.cause}")

        except Exception:
            logger.exception("Unexpected error in Page.printToPDF failed")

            return make_error("Page.printToPDF failed")

        pdf_data = result.get("data", "")
        if not pdf_data:
            return make_error("No PDF data returned from Chrome")

        try:
            pdf_bytes = base64.b64decode(pdf_data)
            abs_path = str(Path(out_path).resolve())
            Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
            Path(abs_path).write_bytes(pdf_bytes)
        except ToolInvocationError as exc:
            return make_error(f"E009: Failed to write PDF to '{out_path}': {exc.cause}")

        except Exception:
            logger.exception(
                "Unexpected error in E009: Failed to write PDF to '{out_path}'"
            )

            return make_error("E009: Failed to write PDF to '{out_path}' failed")

        return make_text(f"PDF saved to: {abs_path} ({len(pdf_bytes):,} bytes)")

    async def _handle_screenshot_element(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Capture a screenshot of a specific element by CSS selector.

        Args:
            arguments: Tool arguments with 'selector' and optional 'path' keys.

        Returns:
            JSON-RPC style response dict with base64 image and optional file path.
        """
        import base64

        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return make_error("CDP client not connected")

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        # Scroll element into view and get bounding rect
        js = f"""
        (() => {{
            const el = document.querySelector({selector!r});
            if (!el) return null;
            el.scrollIntoView({{block: 'center'}});
            const r = el.getBoundingClientRect();
            return {{x: r.x, y: r.y, width: r.width, height: r.height}};
        }})()
        """
        try:
            eval_result = await _safe_cdp_send(
                cdp,
                "Runtime.evaluate",
                {"expression": js, "returnByValue": True},
            )
        except ToolInvocationError as exc:
            return make_error(f"Runtime.evaluate failed: {exc.cause}")

        except Exception:
            logger.exception("Unexpected error in Runtime.evaluate failed")

            return make_error("Runtime.evaluate failed")

        rect = eval_result.get("result", {}).get("value")
        if rect is None:
            return make_error(f"E006: No element found matching selector '{selector}'")

        if rect.get("width", 0) == 0 or rect.get("height", 0) == 0:
            return make_error(
                f"Element '{selector}' has zero dimensions (possibly hidden or off-screen)"
            )

        clip = {
            "x": rect["x"],
            "y": rect["y"],
            "width": rect["width"],
            "height": rect["height"],
            "scale": 1,
        }

        try:
            shot_result = await _safe_cdp_send(
                cdp,
                "Page.captureScreenshot",
                {"format": "png", "clip": clip},
            )
        except ToolInvocationError as exc:
            return make_error(f"Page.captureScreenshot failed: {exc.cause}")

        except Exception:
            logger.exception(
                "Unexpected error in Page.captureScreenshot failed"
            )

            return make_error("Page.captureScreenshot failed")

        img_data = shot_result.get("data", "")
        if not img_data:
            return make_error("No image data returned from Chrome")

        out_path = arguments.get("path", "")
        lines = []
        if out_path:
            try:
                img_bytes = base64.b64decode(img_data)
                abs_path = str(Path(out_path).resolve())
                Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
                Path(abs_path).write_bytes(img_bytes)
                lines.append(f"Screenshot saved to: {abs_path}")
            except Exception as exc:
                logger.warning(
                    "Could not write screenshot to %s: %s", out_path, exc
                )
                lines.append(f"Warning: could not write file: {exc}")

        lines.append(f"data:image/png;base64,{img_data}")
        return make_text("\n".join(lines))

    # ------------------------------------------------------------------ #
    # Screencast capture (catches transient states like loading spinners) #
    # ------------------------------------------------------------------ #

    def _on_screencast_frame(self, params: dict[str, Any]) -> None:
        """Buffer a screencast frame and ack it so the stream continues.

        Called from the CDP read loop, so this stays synchronous: the ack is
        scheduled with ``ensure_future`` rather than awaited here, because the
        read loop is what resolves the ack's response -- awaiting it inline would
        deadlock. Once the buffer is full we stop acking, which pauses the stream
        (CDP flow control) instead of growing memory without bound.
        """
        if not self._screencast_active:
            return
        if len(self._screencast_frames) >= self._screencast_max_frames:
            return
        self._screencast_frames.append(
            {
                "data": params.get("data", ""),
                "timestamp": params.get("metadata", {}).get("timestamp"),
            }
        )
        session_id = params.get("sessionId")
        cdp = self._cdp_client
        if session_id is not None and cdp is not None:
            _ = asyncio.ensure_future(  # noqa: RUF006
                _safe_cdp_send(cdp, "Page.screencastFrameAck", {"sessionId": session_id})
            )

    async def _handle_screencast_start(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Start buffering every painted frame via Page.startScreencast.

        Drive the UI with the normal click/fill/navigate tools between
        screencast_start and screencast_stop; transient states (loading spinners,
        skeletons, flashes) that a discrete take_screenshot would miss are captured.

        Args:
            arguments: Optional 'format' (jpeg|png), 'quality' (0-100),
                'every_nth_frame', 'max_frames', 'max_width', 'max_height'.

        Returns:
            JSON-RPC style response dict.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return make_error("CDP client not connected")
        if self._screencast_active:
            return make_error("screencast already recording; call screencast_stop first")

        fmt = str(arguments.get("format", "jpeg")).lower()
        if fmt not in ("jpeg", "png"):
            return make_error("format must be 'jpeg' or 'png'")

        self._screencast_frames = []
        self._screencast_format = fmt
        self._screencast_max_frames = max(1, int(arguments.get("max_frames", 600)))
        self._screencast_active = True
        cdp.on("Page.screencastFrame", self._on_screencast_frame)

        params: dict[str, Any] = {
            "format": fmt,
            "everyNthFrame": max(1, int(arguments.get("every_nth_frame", 1))),
        }
        if fmt == "jpeg":
            params["quality"] = int(arguments.get("quality", 80))
        if arguments.get("max_width"):
            params["maxWidth"] = int(arguments["max_width"])
        if arguments.get("max_height"):
            params["maxHeight"] = int(arguments["max_height"])

        try:
            await _safe_cdp_send(cdp, "Page.startScreencast", params)
        except ToolInvocationError as exc:
            self._screencast_active = False
            cdp.off("Page.screencastFrame", self._on_screencast_frame)
            return make_error(f"Page.startScreencast failed: {exc.cause}")
        except Exception:
            self._screencast_active = False
            cdp.off("Page.screencastFrame", self._on_screencast_frame)
            logger.exception("Unexpected error in Page.startScreencast")
            return make_error("Page.startScreencast failed")
        return make_text(
            "Screencast recording. Drive the UI with click/fill/navigate, "
            "then call screencast_stop to write the frames."
        )

    async def _handle_screencast_stop(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Stop the screencast and write buffered frames to a directory.

        Args:
            arguments: 'dir' (required) -- directory to write timestamped frames
                plus a frames.json manifest.

        Returns:
            JSON-RPC style response dict.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return make_error("CDP client not connected")
        if not self._screencast_active:
            return make_error("no screencast in progress; call screencast_start first")

        self._screencast_active = False
        with contextlib.suppress(Exception):
            await _safe_cdp_send(cdp, "Page.stopScreencast")  # best-effort
        cdp.off("Page.screencastFrame", self._on_screencast_frame)

        frames = self._screencast_frames
        self._screencast_frames = []
        truncated = len(frames) >= self._screencast_max_frames
        ext = "jpg" if self._screencast_format == "jpeg" else "png"

        lines = [f"Captured {len(frames)} frames."]
        if truncated:
            lines.append(
                f"Note: hit max_frames={self._screencast_max_frames}; "
                "capture may be truncated (raise max_frames or every_nth_frame)."
            )

        out_dir = str(arguments.get("dir", "")).strip()
        if not out_dir:
            return make_error("dir is required to write screencast frames")
        try:
            base = Path(out_dir).resolve()
            base.mkdir(parents=True, exist_ok=True)
            manifest = []
            for i, frame in enumerate(frames):
                fname = f"frame_{i:05d}.{ext}"
                (base / fname).write_bytes(base64.b64decode(frame["data"]))
                manifest.append({"file": fname, "timestamp": frame["timestamp"]})
            (base / "frames.json").write_text(json.dumps(manifest, indent=2))
            lines.append(f"Wrote {len(frames)} frames + frames.json to {base}")
        except ToolInvocationError as exc:
            return make_error(f"could not write frames: {exc.cause}")

        except Exception:
            logger.exception("Unexpected error in could not write frames")

            return make_error("could not write frames failed")
        return make_text("\n".join(lines))

    # ------------------------------------------------------------------ #
    # Semantic wait tools                                                  #
    # ------------------------------------------------------------------ #

    async def _handle_wait_idle(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Wait for network to be idle (no new resources loaded for idle_ms).

        Uses PerformanceResourceTiming to detect in-flight activity.
        Does not subscribe to Network CDP events -- avoids the two-CDP-client
        constraint (D-006).

        Args:
            arguments: Tool arguments with optional 'timeout_ms' and 'idle_ms'.

        Returns:
            JSON-RPC style response dict.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return make_error("CDP client not connected")

        timeout_ms = int(arguments.get("timeout_ms", 5000))
        idle_ms = int(arguments.get("idle_ms", 500))

        # JS: poll until resource count is stable for idle_ms
        js = f"""
        new Promise((resolve, reject) => {{
            const IDLE_MS = {idle_ms};
            const TIMEOUT_MS = {timeout_ms};
            const POLL_MS = 100;
            const start = Date.now();
            let lastCount = performance.getEntriesByType('resource').length;
            let stableSince = Date.now();

            function check() {{
                const now = Date.now();
                if (now - start > TIMEOUT_MS) {{
                    reject(new Error('E008: wait_idle timed out after ' + TIMEOUT_MS + 'ms'));
                    return;
                }}
                const count = performance.getEntriesByType('resource').length;
                if (count !== lastCount) {{
                    lastCount = count;
                    stableSince = now;
                }}
                if (now - stableSince >= IDLE_MS) {{
                    resolve('idle after ' + (now - start) + 'ms');
                    return;
                }}
                setTimeout(check, POLL_MS);
            }}
            setTimeout(check, POLL_MS);
        }})
        """

        try:
            result = await _safe_cdp_send(
                cdp,
                "Runtime.evaluate",
                {
                    "expression": js,
                    "awaitPromise": True,
                    "returnByValue": True,
                    "timeout": timeout_ms + 2000,
                },
            )
        except ToolInvocationError as exc:
            return make_error(f"wait_idle failed: {exc.cause}")

        except Exception:
            logger.exception("Unexpected error in wait_idle failed")

            return make_error("wait_idle failed")

        # Check for JS exception
        exc_details = result.get("exceptionDetails")
        if exc_details:
            msg = exc_details.get("exception", {}).get("description", "wait_idle failed")
            return make_error(msg)

        msg = result.get("result", {}).get("value", "idle")
        return make_text(f"Network idle: {msg}")

    async def _handle_wait_stable(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Wait for DOM mutations to stop for stable_ms.

        Injects a MutationObserver that resolves when no mutations occur
        for the quiescence period. Cleans up on both success and timeout.

        Args:
            arguments: Tool arguments with optional 'timeout_ms' and 'stable_ms'.

        Returns:
            JSON-RPC style response dict.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return make_error("CDP client not connected")

        timeout_ms = int(arguments.get("timeout_ms", 5000))
        stable_ms = int(arguments.get("stable_ms", 300))

        js = f"""
        new Promise((resolve, reject) => {{
            const STABLE_MS = {stable_ms};
            const TIMEOUT_MS = {timeout_ms};
            const start = Date.now();
            let timer = null;
            let observer = null;

            function cleanup() {{
                if (observer) {{ observer.disconnect(); observer = null; }}
                if (timer) {{ clearTimeout(timer); timer = null; }}
            }}

            const timeout = setTimeout(() => {{
                cleanup();
                reject(new Error('E008: wait_stable timed out after ' + TIMEOUT_MS + 'ms'));
            }}, TIMEOUT_MS);

            function resetTimer() {{
                if (timer) clearTimeout(timer);
                timer = setTimeout(() => {{
                    clearTimeout(timeout);
                    cleanup();
                    resolve('stable after ' + (Date.now() - start) + 'ms');
                }}, STABLE_MS);
            }}

            observer = new MutationObserver(resetTimer);
            observer.observe(document.documentElement, {{
                subtree: true,
                childList: true,
                attributes: true,
                characterData: true,
            }});

            // Start the initial stability timer
            resetTimer();
        }})
        """

        try:
            result = await _safe_cdp_send(
                cdp,
                "Runtime.evaluate",
                {
                    "expression": js,
                    "awaitPromise": True,
                    "returnByValue": True,
                    "timeout": timeout_ms + 2000,
                },
            )
        except ToolInvocationError as exc:
            return make_error(f"wait_stable failed: {exc.cause}")

        except Exception:
            logger.exception("Unexpected error in wait_stable failed")

            return make_error("wait_stable failed")

        exc_details = result.get("exceptionDetails")
        if exc_details:
            msg = exc_details.get("exception", {}).get("description", "wait_stable failed")
            return make_error(msg)

        msg = result.get("result", {}).get("value", "stable")
        return make_text(f"DOM stable: {msg}")

    # ------------------------------------------------------------------ #
    # Content extraction tools                                             #
    # ------------------------------------------------------------------ #

    async def _query_element_js(
        self, cdp: Any, selector: str, expression: str
    ) -> dict[str, Any] | None:
        """Evaluate a JS expression on a queried element.

        Args:
            cdp: Connected CDPClient instance.
            selector: CSS selector string.
            expression: JS expression using 'el' as the element variable.
                Must return the desired value or null if element not found.

        Returns:
            CDP result dict on success, or None if the element was not found.
        """
        js = f"""
        (() => {{
            const el = document.querySelector({selector!r});
            if (!el) return null;
            return {expression};
        }})()
        """
        result = await _safe_cdp_send(
            cdp,
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True},
        )
        val = result.get("result", {}).get("value")
        # None (Python) means JS returned null (element not found)
        return result if val is not None else None

    async def _handle_get_text(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Get text content of element by CSS selector.

        Args:
            arguments: Tool arguments with 'selector' key.

        Returns:
            JSON-RPC style response dict with element text content.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return make_error("CDP client not connected")

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        try:
            result = await self._query_element_js(cdp, selector, "el.textContent")
        except ToolInvocationError as exc:
            return make_error(f"get_text failed: {exc.cause}")

        except Exception:
            logger.exception("Unexpected error in get_text failed")

            return make_error("get_text failed")

        if result is None:
            return make_error(f"E006: No element found matching selector '{selector}'")

        text = result.get("result", {}).get("value", "")
        return make_text(str(text))

    async def _handle_get_html(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Get outer HTML of element by CSS selector.

        Args:
            arguments: Tool arguments with 'selector' key.

        Returns:
            JSON-RPC style response dict with element outer HTML.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return make_error("CDP client not connected")

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        try:
            result = await self._query_element_js(cdp, selector, "el.outerHTML")
        except ToolInvocationError as exc:
            return make_error(f"get_html failed: {exc.cause}")

        except Exception:
            logger.exception("Unexpected error in get_html failed")

            return make_error("get_html failed")

        if result is None:
            return make_error(f"E006: No element found matching selector '{selector}'")

        html = result.get("result", {}).get("value", "")
        return make_text(str(html))

    async def _handle_get_attr(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Get attribute value of element by CSS selector.

        Returns null (not an error) if element exists but attribute is absent.

        Args:
            arguments: Tool arguments with 'selector' and 'attribute' keys.

        Returns:
            JSON-RPC style response dict with attribute value.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return make_error("CDP client not connected")

        selector = arguments.get("selector", "").strip()
        attribute = arguments.get("attribute", "").strip()
        if not selector:
            return make_error("selector is required")
        if not attribute:
            return make_error("attribute is required")

        js = f"""
        (() => {{
            const el = document.querySelector({selector!r});
            if (!el) return '__ELEMENT_NOT_FOUND__';
            const val = el.getAttribute({attribute!r});
            return val === null ? '__ATTR_NULL__' : val;
        }})()
        """
        try:
            result = await _safe_cdp_send(
                cdp,
                "Runtime.evaluate",
                {"expression": js, "returnByValue": True},
            )
        except ToolInvocationError as exc:
            return make_error(f"get_attr failed: {exc.cause}")

        except Exception:
            logger.exception("Unexpected error in get_attr failed")

            return make_error("get_attr failed")

        val = result.get("result", {}).get("value", "__ELEMENT_NOT_FOUND__")
        if val == "__ELEMENT_NOT_FOUND__":
            return make_error(f"E006: No element found matching selector '{selector}'")
        if val == "__ATTR_NULL__":
            return make_text(f"null (element exists but '{attribute}' attribute is not present)")
        return make_text(str(val))

    # ------------------------------------------------------------------ #
    # Element query tools                                                  #
    # ------------------------------------------------------------------ #

    async def _handle_element_exists(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Check if one or more elements matching a CSS selector exist.

        Never throws on not-found -- this is a boolean query.

        Args:
            arguments: Tool arguments with 'selector' key.

        Returns:
            JSON-RPC style response dict with exists bool and count.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return make_error("CDP client not connected")

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        js = f"document.querySelectorAll({selector!r}).length"
        try:
            result = await _safe_cdp_send(
                cdp,
                "Runtime.evaluate",
                {"expression": js, "returnByValue": True},
            )
        except ToolInvocationError as exc:
            return make_error(f"element_exists failed: {exc.cause}")

        except Exception:
            logger.exception("Unexpected error in element_exists failed")

            return make_error("element_exists failed")

        # querySelectorAll throws for invalid selectors -- check for exception
        exc_details = result.get("exceptionDetails")
        if exc_details:
            msg = exc_details.get("exception", {}).get("description", "Invalid selector")
            return make_error(f"Invalid selector '{selector}': {msg}")

        count = result.get("result", {}).get("value", 0)
        exists = count > 0
        return make_text(f'{{"exists": {str(exists).lower()}, "count": {count}}}')

    async def _handle_element_visible(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Check if an element is visible (rendered, non-zero size, not CSS-hidden).

        Visibility: element exists AND has non-zero bounding rect AND
        display != none AND visibility != hidden AND opacity > 0.

        Args:
            arguments: Tool arguments with 'selector' key.

        Returns:
            JSON-RPC style response dict with visible bool.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return make_error("CDP client not connected")

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        js = f"""
        (() => {{
            const el = document.querySelector({selector!r});
            if (!el) return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none') return false;
            if (style.visibility === 'hidden') return false;
            if (parseFloat(style.opacity) === 0) return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        }})()
        """
        try:
            result = await _safe_cdp_send(
                cdp,
                "Runtime.evaluate",
                {"expression": js, "returnByValue": True},
            )
        except ToolInvocationError as exc:
            return make_error(f"element_visible failed: {exc.cause}")

        except Exception:
            logger.exception("Unexpected error in element_visible failed")

            return make_error("element_visible failed")

        visible = result.get("result", {}).get("value", False)
        return make_text(f'{{"visible": {str(visible).lower()}}}')

    async def _refresh_frame_tree(self) -> None:
        """Refresh the frame tree from Chrome."""
        if self._cdp_client and self._cdp_client.connected:
            try:
                result = await self._cdp_client.send("Page.getFrameTree")
                if "frameTree" in result:
                    self._frame_manager.update_from_frame_tree(result["frameTree"])
            except Exception:
                logger.debug("Failed to refresh frame tree", exc_info=True)

    def await_paint_ready(self, timeout_ms: int = SCREENSHOT_PAINT_READY_TIMEOUT_MS) -> bool:
        """Block until Chrome has painted at least one stable frame.

        Used as a pre-capture gate for take_screenshot. Waits for two
        requestAnimationFrame ticks (one to flush the current frame, one to
        confirm the next frame committed) plus document.fonts.ready, so any
        in-flight CSS transition / font swap / layout reflow has reached
        the compositor before we capture.

        DOM-mutation waits (wait_stable) miss this case because CSS
        transform/opacity animations don't fire MutationObserver callbacks
        -- the DOM looks settled while pixels are still moving.

        Returns:
            True if the gate completed; False if CDP is unavailable or the
            evaluate timed out. Never raises -- screenshot must still proceed
            on a best-effort basis if this gate fails.
        """
        if not self._loop or not self._cdp_client or not self._cdp_client.connected:
            return False

        future = asyncio.run_coroutine_threadsafe(
            self._await_paint_ready_async(timeout_ms), self._loop
        )
        try:
            return future.result(timeout=(timeout_ms / 1000.0) + 2.0)
        except Exception:
            logger.debug(
                "await_paint_ready timed out or failed", exc_info=True
            )
            return False

    async def _await_paint_ready_async(self, timeout_ms: int) -> bool:
        """Run the rAF + fonts.ready gate via Runtime.evaluate.

        The JS resolves once the next frame after the current one has been
        scheduled by the compositor, with a hard timeout safety net so a
        broken page (background tab, no rAF firing) can't hang the daemon.
        """
        js = f"""
        (async () => {{
            const timeout = new Promise(r => setTimeout(() => r('timeout'), {int(timeout_ms)}));
            const paint = new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r('paint'))));
            const fonts = (document.fonts && document.fonts.ready) ? document.fonts.ready : Promise.resolve();
            await Promise.race([Promise.all([paint, fonts]), timeout]);
            return true;
        }})()
        """
        try:
            await self._cdp_client.send(
                "Runtime.evaluate",
                {"expression": js, "awaitPromise": True, "returnByValue": True},
            )
            return True
        except Exception:
            logger.debug("_await_paint_ready_async failed", exc_info=True)
            return False

    def run_post_navigation_detection(self) -> dict[str, Any] | None:
        """Run interstitial detection with auto-retry for JS-solvable challenges.

        For challenges that can auto-solve (e.g., Cloudflare JS challenge),
        waits and re-checks up to INTERSTITIAL_MAX_RETRIES times before
        reporting the interstitial. Human-interaction challenges (CAPTCHA,
        auth walls) are reported immediately.

        Returns:
            Dict with 'detections' list and 'auto_retried' bool,
            or None if CDP unavailable.
        """
        if not self._loop or not self._cdp_client or not self._cdp_client.connected:
            return None

        future = asyncio.run_coroutine_threadsafe(self._detect_with_retry(), self._loop)
        try:
            # Total timeout: initial detect + (retries * delay) + buffer
            timeout = 10 + (INTERSTITIAL_MAX_RETRIES * (INTERSTITIAL_RETRY_DELAY_SECONDS + 2))
            return future.result(timeout=timeout)
        except Exception:
            logger.debug("run_post_navigation_detection failed", exc_info=True)
            return None

    async def _detect_with_retry(self) -> dict[str, Any]:
        """Detect interstitials and auto-retry JS-solvable challenges.

        Returns:
            Dict with 'detections' (list) and 'auto_retried' (bool)
            and 'retries_used' (int).
        """

        detections = await self._detect_interstitials()
        if not detections:
            return {"detections": [], "auto_retried": False, "retries_used": 0}

        # Split into auto-retryable vs. immediate-report
        retryable = [d for d in detections if d.get("type") in INTERSTITIAL_AUTO_RETRY_TYPES]
        non_retryable = [
            d for d in detections if d.get("type") not in INTERSTITIAL_AUTO_RETRY_TYPES
        ]

        # If nothing is retryable, report immediately
        if not retryable:
            return {"detections": detections, "auto_retried": False, "retries_used": 0}

        # Auto-retry: wait for JS challenge to self-solve
        for attempt in range(INTERSTITIAL_MAX_RETRIES):
            await asyncio.sleep(INTERSTITIAL_RETRY_DELAY_SECONDS)
            detections = await self._detect_interstitials()

            if not detections:
                # Challenge cleared
                return {"detections": [], "auto_retried": True, "retries_used": attempt + 1}

            retryable = [d for d in detections if d.get("type") in INTERSTITIAL_AUTO_RETRY_TYPES]
            if not retryable:
                # Only non-retryable left (or all cleared)
                return {
                    "detections": detections,
                    "auto_retried": True,
                    "retries_used": attempt + 1,
                }

        # Exhausted retries -- report whatever remains plus any non-retryable
        return {
            "detections": detections + non_retryable,
            "auto_retried": True,
            "retries_used": INTERSTITIAL_MAX_RETRIES,
        }

    async def _detect_interstitials(self) -> list[dict[str, Any]]:
        """Run interstitial detection via CDP."""
        try:
            from .interstitial import detect_interstitials_async

            return await detect_interstitials_async(self._cdp_client)
        except Exception:
            logger.debug("Interstitial detection failed", exc_info=True)
            return []


def make_text(text: str) -> dict[str, Any]:
    """Build a JSON-RPC success response with text content.

    Args:
        text: Text content.

    Returns:
        JSON-RPC response dict.
    """
    return {
        "jsonrpc": "2.0",
        "result": {"content": [{"type": "text", "text": text}]},
        "id": 0,
    }


def make_error(message: str) -> dict[str, Any]:
    """Build a JSON-RPC error response.

    Args:
        message: Error message.

    Returns:
        JSON-RPC response dict.
    """
    return {
        "jsonrpc": "2.0",
        "result": {"content": [{"type": "text", "text": f"Error: {message}"}], "isError": True},
        "id": 0,
    }
