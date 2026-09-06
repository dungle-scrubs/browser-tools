#!/usr/bin/env python3
"""CDP handler and toolset definitions for browser-tools CLI.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cdp_constants import REQUEST_TIMEOUT_SECONDS
from .core.cdp_client import CDPClient, get_ws_url
from .core.errors import CDPError
from .interstitial import (
    DETECT_TOTAL_TIMEOUT_SECONDS,
    detect_interstitials_async,
    detect_with_retry,
)
from .mcp_response import make_error, make_text
from .native_interaction import NativeInteractor, UidResolutionError
from .native_snapshot import NativeSnapshotReader
from .screencast import ScreencastRecorder
from .screenshot_utils import SCREENSHOT_PAINT_READY_TIMEOUT_MS
from .tool_registry import TOOLS

logger = logging.getLogger(__name__)

RUNTIME_CLEANUP_TIMEOUT_SECONDS = 5.0
DEFAULT_WAIT_TIMEOUT_MS = 5000
DEFAULT_IDLE_MS = 500
DEFAULT_STABLE_MS = 300


def _js_string(value: str) -> str:
    """Encode one JavaScript string literal without relying on Python repr."""
    return json.dumps(value)


class ToolInvocationError(Exception):
    """Raised when a CDP-backed tool fails during execution.

    Wraps the underlying cause so callers get the original exception
    chain plus a tool-call-scoped context.
    """

    def __init__(self, method: str, cause: BaseException) -> None:
        super().__init__(f"{method}: {cause}")
        self.method = method
        self.cause = cause


class CdpToolError(Exception):
    """A CDP tool call failed; carries the label for the error response.

    ``str()`` yields ``"<label> failed"`` (unexpected) or
    ``"<label> failed: <cause>"`` (a wrapped CDP protocol failure), which
    ``call_tool``'s top-level handler turns into the matching ``make_error``
    text. Raised by :func:`_cdp_call` so handlers stay linear.
    """

    def __init__(self, label: str, cause: BaseException | None = None) -> None:
        self.label = label
        self.cause = cause
        if cause is not None:
            super().__init__(f"{label} failed: {cause}")
        else:
            super().__init__(f"{label} failed")


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
    try:
        if params is not None:
            return await cdp_client.send(method, params)
        return await cdp_client.send(method)
    except (CDPError, ConnectionError) as exc:
        raise ToolInvocationError(method, exc) from exc


async def _cdp_call(
    cdp_client: Any,
    method: str,
    params: dict[str, Any] | None,
    *,
    label: str,
) -> dict[str, Any]:
    """Send one CDP command, raising :class:`CdpToolError` on failure.

    Consolidates the error mapping that was duplicated in every handler:
    ``ToolInvocationError`` (an expected CDP protocol failure) and any
    unexpected exception both become a ``CdpToolError`` carrying ``label``.
    ``call_tool``'s top-level ``except`` turns it into the matching
    ``make_error`` text, so handlers stay linear with no per-call try/except.
    """
    try:
        if params is not None:
            return await _safe_cdp_send(cdp_client, method, params)
        return await _safe_cdp_send(cdp_client, method)
    except ToolInvocationError as exc:
        raise CdpToolError(label, exc.cause) from exc
    except Exception:
        logger.exception("Unexpected error in %s", label)
        raise CdpToolError(label) from None


@dataclass
class ElementEvalResult:
    """Result of evaluating a JS expression against a CSS-selected element.

    ``found`` is True only when the selector matched an element; ``value``
    holds the expression's return value, which may be None when the expression
    itself yields null (for example an absent attribute on a present element).
    Element-not-found is reported as ``found=False`` so callers never overload
    a null value to mean "missing element" - the bug that previously forced
    each handler to invent its own sentinel string.
    """

    found: bool
    value: Any


# The querySelector + not-found wrapper shared by every element-reading tool.
# Uses a namespaced ``__found__`` key so a returned object that happens to have
# a ``found`` field cannot be misread as the protocol envelope.
_ELEM_NOT_FOUND_JS = "return {__found__: false};"

# element_visible: evaluated against the matched element (``el``) via
# eval_on_element. A selector that matches nothing never reaches this - it is
# reported as found=False upstream - so the expression assumes a real element.
_VISIBILITY_EXPR = """(function (el) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none') return false;
    if (style.visibility === 'hidden') return false;
    if (parseFloat(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
})(el)"""


def _element_eval_js(selector: str, expression: str) -> str:
    """Build the IIFE that resolves ``selector`` and evaluates ``expression``.

    ``expression`` runs with ``el`` bound to the matched element and may return
    any JSON-serializable value, including null.
    """
    return f"""
    (() => {{
        const el = document.querySelector({_js_string(selector)});
        if (!el) {{ {_ELEM_NOT_FOUND_JS} }}
        return {{__found__: true, value: ({expression})}};
    }})()
    """


async def eval_on_element(
    cdp: Any, selector: str, expression: str, *, label: str
) -> ElementEvalResult:
    """Evaluate ``expression`` against the element matching ``selector``.

    Single owner of the querySelector wrapper, the Runtime.evaluate call, and
    the not-found contract. Before this, six handlers reimplemented the same
    idea in three incompatible styles: a None return, sentinel strings
    (``__ELEMENT_NOT_FOUND__`` / ``__ATTR_NULL__``), and inline JS returning
    ``false`` / ``0``. One interface here replaces all three.

    Args:
        cdp: Connected CDPClient.
        selector: CSS selector string.
        expression: JS expression evaluated with ``el`` bound to the matched
            element. May return any JSON-serializable value, including null.
        label: Error-mapping label forwarded to :func:`_cdp_call`.

    Returns:
        ``ElementEvalResult`` with ``found=False`` when the selector matched
        nothing, otherwise ``found=True`` and the expression's value.

    Raises:
        CdpToolError: on CDP protocol failure (mapped by ``_cdp_call``).
    """
    result = await _cdp_call(
        cdp,
        "Runtime.evaluate",
        {"expression": _element_eval_js(selector, expression), "returnByValue": True},
        label=label,
    )
    val = result.get("result", {}).get("value")
    if isinstance(val, dict) and val.get("__found__") is True:
        return ElementEvalResult(found=True, value=val.get("value"))
    return ElementEvalResult(found=False, value=None)


class CDPRuntime:
    """Own one browser connection and one flattened page session."""

    def __init__(self, port: int, target_id: str) -> None:
        self.port = port
        self.target_id = target_id
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_requested = threading.Event()
        self._cdp_client: Any = None
        self._session_id: str | None = None
        self._connected = False
        self._commands: set[asyncio.Task[Any]] = set()
        self._error: BaseException | None = None
        from .frame_manager import FrameManager

        self._frame_manager = FrameManager()
        self._screencast = ScreencastRecorder()

    @property
    def available(self) -> bool:
        return self.connected

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def client(self) -> Any:
        return self if self._cdp_client is not None else None

    @property
    def frame_manager(self) -> Any:
        return self._frame_manager

    @property
    def screencast(self) -> ScreencastRecorder:
        return self._screencast

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    def cdp_or_error(self) -> tuple[Any, dict[str, Any] | None]:
        if not self.connected:
            return None, make_error("CDP client not connected")
        return self, None

    def wait_ready(self, timeout: float) -> None:
        """Wait for startup, propagating its cause instead of hiding failure."""
        if not self._ready.wait(timeout):
            raise TimeoutError("CDP connection timed out")
        if self._error is not None:
            raise self._error

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.connected:
            raise ConnectionError("CDP session is disconnected")
        command = asyncio.create_task(
            self._cdp_client.send(method, params, session_id=self._session_id)
        )
        self._commands.add(command)

        def completed(task: asyncio.Task[Any]) -> None:
            self._commands.discard(task)
            if not task.cancelled():
                task.exception()

        command.add_done_callback(completed)
        try:
            # Cancelling a caller must not cancel the core client's response
            # future: a late reply still needs to be consumed by its read loop.
            return await asyncio.shield(command)
        except ConnectionError:
            self._connected = False
            raise

    def on(self, event: str, callback: Any) -> None:
        self._cdp_client.on(event, callback, session_id=self._session_id)

    def off(self, event: str, callback: Any) -> None:
        self._cdp_client.off(event, callback)

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        try:
            self._task = self._loop.create_task(self._main())
            if self._stop_requested.is_set():
                self._loop.call_soon(self._task.cancel)
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            self._error = ConnectionError("CDP startup cancelled")
        except Exception as exc:
            self._error = exc
        finally:
            self._connected = False
            self._ready.set()
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()
            self._loop = None

    async def _main(self) -> None:
        try:
            self._cdp_client = CDPClient(get_ws_url(port=self.port, target_type="browser"))
            await self._cdp_client.connect()
            result = await self._cdp_client.send(
                "Target.attachToTarget", {"targetId": self.target_id, "flatten": True}
            )
            self._session_id = result["sessionId"]
            self._connected = True
            for event, callback in (
                ("Page.frameAttached", self._frame_manager.handle_frame_attached),
                ("Page.frameDetached", self._frame_manager.handle_frame_detached),
                ("Page.frameNavigated", self._frame_manager.handle_frame_navigated),
                (
                    "Runtime.executionContextCreated",
                    self._frame_manager.handle_execution_context_created,
                ),
                (
                    "Runtime.executionContextDestroyed",
                    self._frame_manager.handle_execution_context_destroyed,
                ),
            ):
                self.on(event, callback)
            await self.send("Page.enable")
            await self.send("Runtime.enable")
            result = await self.send("Page.getFrameTree")
            if "frameTree" in result:
                self._frame_manager.update_from_frame_tree(result["frameTree"])
            self._ready.set()
            if self._stop_event is not None:
                await self._stop_event.wait()
        finally:
            self._connected = False
            if self._cdp_client is not None:
                # Reserve half of the cleanup grace for closing the socket,
                # even if the browser never acknowledges detach.
                grace = RUNTIME_CLEANUP_TIMEOUT_SECONDS / 2
                if self._session_id is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            self._cdp_client.send(
                                "Target.detachFromTarget", {"sessionId": self._session_id}
                            ),
                            timeout=grace,
                        )
                try:
                    await asyncio.wait_for(self._cdp_client.close(), timeout=grace)
                except Exception:
                    logger.debug("CDP cleanup failed", exc_info=True)

    def stop(self) -> None:
        self._stop_requested.set()

        def request_stop() -> None:
            if not self._ready.is_set() and self._task is not None:
                self._task.cancel()
            elif self._stop_event is not None:
                self._stop_event.set()

        loop = self._loop
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(request_stop)

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
        if not self._loop or not self._cdp_client or not self.connected:
            return False

        future = asyncio.run_coroutine_threadsafe(
            self._await_paint_ready_async(timeout_ms), self._loop
        )
        try:
            return future.result(timeout=(timeout_ms / 1000.0) + 2.0)
        except Exception:
            logger.debug("await_paint_ready timed out or failed", exc_info=True)
            return False

    async def _await_paint_ready_async(self, timeout_ms: int) -> bool:
        """Run the rAF + fonts.ready gate via Runtime.evaluate.

        The JS resolves once the next frame after the current one has been
        scheduled by the compositor, with a hard timeout safety net so a
        broken page (background tab, no rAF firing) cannot hang the invocation.
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
            await self.send(
                "Runtime.evaluate",
                {"expression": js, "awaitPromise": True, "returnByValue": True},
            )
            return True
        except Exception:
            logger.debug("_await_paint_ready_async failed", exc_info=True)
            return False

    def run_post_navigation_detection(self) -> dict[str, Any] | None:
        """Run post-navigation interstitial detection (thread-safe).

        Delegates the detect-and-retry policy to :mod:`interstitial`; this
        method only marshals that coroutine onto the CDP event loop and enforces
        the outer timeout. The retry tuning and the single-shot detection both
        live in the interstitial module, so the whole challenge-response policy
        has one home rather than being smeared across the runtime, interstitial,
        and cdp_constants.

        Returns:
            Dict with 'detections', 'auto_retried', 'retries_used', or None if
            CDP is unavailable.
        """
        if not self._loop or not self._cdp_client or not self.connected:
            return None

        async def _detect_once() -> list[dict[str, Any]]:
            return await detect_interstitials_async(self)

        async def _run() -> dict[str, Any]:
            return await detect_with_retry(_detect_once)

        future = asyncio.run_coroutine_threadsafe(_run(), self._loop)
        try:
            return future.result(timeout=DETECT_TOTAL_TIMEOUT_SECONDS)
        except Exception:
            logger.debug("run_post_navigation_detection failed", exc_info=True)
            return None


class CDPHandler:
    """CDP tool handler registry: routes the 18 CDP tools to their handlers, each
    executed on the :class:`CDPRuntime` event loop.

    Owns no connection state itself - the runtime does - and reaches the browser
    through the runtime's ``client`` / ``frame_manager`` / ``screencast`` seam.
    CLI invocations construct the runtime and manage its lifetime.
    """

    def __init__(self, runtime: CDPRuntime) -> None:
        self.runtime = runtime
        self._handlers: dict[str, Any] = {
            name: getattr(self, tool.method) for name, tool in TOOLS.items()
        }
        self._native_reader = NativeSnapshotReader()
        self._native_interactor = NativeInteractor(self._native_reader)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a CDP/frame tool (thread-safe, blocks until complete).

        Args:
            name: Tool name.
            arguments: Tool arguments.

        Returns:
            JSON-RPC style response dict.
        """
        if not self.runtime.loop or not self.runtime.loop.is_running():
            return make_error("CDP handler not initialized")

        future = asyncio.run_coroutine_threadsafe(
            self._dispatch_tool(name, arguments), self.runtime.loop
        )
        try:
            return future.result(timeout=REQUEST_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("call_tool(%s) error: %s", name, exc)
            future.cancel()
            return make_error(str(exc))

    def call_native(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a native snapshot/UID tool on the CDP loop (thread-safe).

        The flipped default backend for ``take_snapshot`` / ``click`` / ``fill``
        (RFC-01 Phase 2, #41). It runs the native read/interaction path built in
        #39/#40 over this session's CDP client and returns the same bare MCP
        envelope shape the Node engine returned, so the frozen tool response
        shape is preserved. Mirrors :meth:`call_tool`'s loop scheduling.

        Args:
            name: One of ``take_snapshot``, ``click``, ``fill``.
            arguments: Tool arguments (``uid``/``value`` for interactions).

        Returns:
            JSON-RPC style response dict.
        """
        if not self.runtime.loop or not self.runtime.loop.is_running():
            return make_error("CDP handler not initialized")

        future = asyncio.run_coroutine_threadsafe(
            self._dispatch_native(name, arguments), self.runtime.loop
        )
        try:
            return future.result(timeout=REQUEST_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("call_native(%s) error: %s", name, exc)
            future.cancel()
            return make_error(str(exc))

    def mark_native_navigation(self) -> None:
        """Invalidate the native snapshot's UIDs after a navigation.

        A navigation replaces the document, so every outstanding native UID is
        stale. Called after a navigation action so a UID
        from a pre-navigation snapshot no longer resolves (native's stability
        contract). No-op cost when the Node engine is selected.
        """
        self._native_reader.note_navigation()

    async def _dispatch_native(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Route a native tool call to the snapshot or interaction path."""
        cdp, err = self.runtime.cdp_or_error()
        if err is not None:
            return err
        send = cdp.send

        if name == "take_snapshot":
            snapshot = await self._native_reader.snapshot_stitched(send)
            return make_text(snapshot.format_tree())

        if name in ("click", "fill"):
            uid = str(arguments.get("uid", "")).strip()
            if not uid:
                return make_error(
                    f"E008: native '{name}' requires a 'uid' from a prior take_snapshot"
                )
            try:
                if name == "click":
                    result = await self._native_interactor.click_async(send, uid)
                    assert result.point is not None  # click_steps always sets point
                    return make_text(
                        f"Clicked uid={result.uid} at ({result.point[0]:.0f}, {result.point[1]:.0f})."
                    )
                value = str(arguments.get("value", ""))
                result = await self._native_interactor.fill_async(send, uid, value)
                return make_text(f"Filled uid={result.uid} (value now {result.value_after!r}).")
            except UidResolutionError as exc:
                return make_error(str(exc))

        return make_error(f"Unknown native tool: {name}")

    async def _dispatch_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Route a tool call to its registered handler.

        ``tool_registry.TOOLS`` is the binding of tool names to methods.

        Args:
            name: Tool name.
            arguments: Tool arguments.

        Returns:
            JSON-RPC style response dict.
        """
        handler = self._handlers.get(name)
        if handler is None:
            return make_error(f"Unknown CDP tool: {name}")
        return await handler(arguments)

    async def _handle_list_frames(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle list_frames tool."""
        fm = self.runtime.frame_manager
        if fm is None:
            return make_error("Frame manager not initialized")

        frames = fm.get_flat_frames()
        if not frames and self.runtime.connected:
            await self._refresh_frame_tree()
            frames = fm.get_flat_frames()
        if not frames:
            return make_text("No frames available.")

        lines = ["Frames in current page:\n"]
        for frame in frames:
            indent = "  " * frame["depth"]
            selected = " [selected]" if frame["frameId"] == fm.selected_frame_id else ""
            name = f' name="{frame["name"]}"' if frame.get("name") else ""
            lines.append(f"{indent}{frame['frameId']}: {frame['url']}{name}{selected}")
        return make_text("\n".join(lines))

    async def _handle_select_frame(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle select_frame tool."""
        fm = self.runtime.frame_manager
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

    async def _handle_reset_frame(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle reset_frame tool."""
        fm = self.runtime.frame_manager
        if fm is None:
            return make_error("Frame manager not initialized")

        fm.reset_frame()
        return make_text("Frame selection cleared. Now targeting top-level page.")

    async def _handle_get_frame_events(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle get_frame_events tool."""
        fm = self.runtime.frame_manager
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
        fm = self.runtime.frame_manager
        cdp = self.runtime.client
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
                    value = str(c.get("value", ""))
                    shown = (
                        value
                        if arguments.get("reveal_values", False) is True
                        else f"<len {len(value)}>"
                    )
                    result_parts.append(f"  {c.get('name')}: {shown}")
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
                logger.exception("Storage fetch error (localStorage)")
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
                logger.exception("Storage fetch error (sessionStorage)")

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
        cdp, err = self.runtime.cdp_or_error()
        if err is not None:
            return err

        role = arguments.get("role", "").strip()
        name = arguments.get("name", "").strip()

        if not role and not name:
            return make_error("E007: ax_find requires at least one of 'role' or 'name'")

        params: dict[str, Any] = {}
        if role:
            params["role"] = role
        if name:
            params["accessibleName"] = name

        result = await _cdp_call(
            cdp, "Accessibility.queryAXTree", params, label="Accessibility.queryAXTree"
        )

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
        cdp, err = self.runtime.cdp_or_error()
        if err is not None:
            return err

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        # Resolve selector to a backend DOM node ID
        eval_result = await _cdp_call(
            cdp,
            "Runtime.evaluate",
            {
                "expression": f"document.querySelector({_js_string(selector)})",
                "returnByValue": False,
            },
            label="Runtime.evaluate",
        )

        remote_obj = eval_result.get("result", {})
        if remote_obj.get("type") == "undefined" or remote_obj.get("subtype") == "null":
            return make_error(f"E006: No element found matching selector '{selector}'")

        object_id = remote_obj.get("objectId")
        if not object_id:
            return make_error(f"E006: No element found matching selector '{selector}'")

        # Get the backend DOM node ID
        node_result = await _cdp_call(
            cdp, "DOM.requestNode", {"objectId": object_id}, label="DOM.requestNode"
        )

        backend_node_id = node_result.get("nodeId")
        if not backend_node_id:
            return make_error("Could not resolve element to DOM node")

        # Get partial AX tree for this node
        ax_result = await _cdp_call(
            cdp,
            "Accessibility.getPartialAXTree",
            {"nodeId": backend_node_id, "fetchRelatives": False},
            label="Accessibility.getPartialAXTree",
        )

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
        import time

        cdp, err = self.runtime.cdp_or_error()
        if err is not None:
            return err

        landscape = arguments.get("landscape", False)
        print_background = arguments.get("print_background", True)
        out_path = arguments.get("path", "")
        if not out_path:
            out_path = f"{int(time.time())}_page.pdf"

        result = await _cdp_call(
            cdp,
            "Page.printToPDF",
            {
                "landscape": landscape,
                "printBackground": print_background,
                "transferMode": "ReturnAsBase64",
            },
            label="Page.printToPDF",
        )

        pdf_data = result.get("data", "")
        if not pdf_data:
            return make_error("No PDF data returned from Chrome")

        try:
            pdf_bytes = base64.b64decode(pdf_data)
            abs_path = str(Path(out_path).resolve())
            Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
            Path(abs_path).write_bytes(pdf_bytes)
        except Exception:
            logger.exception("Unexpected error writing PDF to '%s'", out_path)
            return make_error(f"E009: Failed to write PDF to '{out_path}'")

        return make_text(f"PDF saved to: {abs_path} ({len(pdf_bytes):,} bytes)")

    async def _handle_screenshot_element(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Capture a screenshot of a specific element by CSS selector.

        Args:
            arguments: Tool arguments with 'selector' and optional 'path' keys.

        Returns:
            JSON-RPC style response dict with base64 image and optional file path.
        """

        cdp, err = self.runtime.cdp_or_error()
        if err is not None:
            return err

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        # Scroll element into view and get bounding rect
        js = f"""
        (() => {{
            const el = document.querySelector({_js_string(selector)});
            if (!el) return null;
            el.scrollIntoView({{block: 'center'}});
            const r = el.getBoundingClientRect();
            return {{x: r.x, y: r.y, width: r.width, height: r.height}};
        }})()
        """
        eval_result = await _cdp_call(
            cdp,
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True},
            label="Runtime.evaluate",
        )

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

        shot_result = await _cdp_call(
            cdp,
            "Page.captureScreenshot",
            {"format": "png", "clip": clip},
            label="Page.captureScreenshot",
        )

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
                logger.warning("Could not write screenshot to %s: %s", out_path, exc)
                lines.append(f"Warning: could not write file: {exc}")

        lines.append(f"data:image/png;base64,{img_data}")
        return make_text("\n".join(lines))

    # ------------------------------------------------------------------ #
    # Screencast capture (catches transient states like loading spinners) #
    # ------------------------------------------------------------------ #

    async def _handle_screencast_start(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Begin screencast capture; delegates to :class:`ScreencastRecorder`."""
        cdp, err = self.runtime.cdp_or_error()
        if err is not None:
            return err
        return await self.runtime.screencast.start(cdp, arguments)

    async def _handle_screencast_stop(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Stop screencast capture and write frames; delegates to the recorder."""
        cdp, err = self.runtime.cdp_or_error()
        if err is not None:
            return err
        return await self.runtime.screencast.stop(cdp, arguments)

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
        cdp, err = self.runtime.cdp_or_error()
        if err is not None:
            return err

        timeout_ms = int(arguments.get("timeout_ms", DEFAULT_WAIT_TIMEOUT_MS))
        idle_ms = int(arguments.get("idle_ms", DEFAULT_IDLE_MS))

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

        result = await _cdp_call(
            cdp,
            "Runtime.evaluate",
            {
                "expression": js,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": timeout_ms + 2000,
            },
            label="wait_idle",
        )

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
        cdp, err = self.runtime.cdp_or_error()
        if err is not None:
            return err

        timeout_ms = int(arguments.get("timeout_ms", DEFAULT_WAIT_TIMEOUT_MS))
        stable_ms = int(arguments.get("stable_ms", DEFAULT_STABLE_MS))

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

        result = await _cdp_call(
            cdp,
            "Runtime.evaluate",
            {
                "expression": js,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": timeout_ms + 2000,
            },
            label="wait_stable",
        )

        exc_details = result.get("exceptionDetails")
        if exc_details:
            msg = exc_details.get("exception", {}).get("description", "wait_stable failed")
            return make_error(msg)

        msg = result.get("result", {}).get("value", "stable")
        return make_text(f"DOM stable: {msg}")

    # ------------------------------------------------------------------ #
    # Content extraction tools                                             #
    # ------------------------------------------------------------------ #

    async def _handle_get_text(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Get text content of element by CSS selector.

        Args:
            arguments: Tool arguments with 'selector' key.

        Returns:
            JSON-RPC style response dict with element text content.
        """
        cdp, err = self.runtime.cdp_or_error()
        if err is not None:
            return err

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        res = await eval_on_element(cdp, selector, "el.textContent", label="get_text")
        if not res.found:
            return make_error(f"E006: No element found matching selector '{selector}'")
        return make_text("" if res.value is None else str(res.value))

    async def _handle_get_html(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Get outer HTML of element by CSS selector.

        Args:
            arguments: Tool arguments with 'selector' key.

        Returns:
            JSON-RPC style response dict with element outer HTML.
        """
        cdp, err = self.runtime.cdp_or_error()
        if err is not None:
            return err

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        res = await eval_on_element(cdp, selector, "el.outerHTML", label="get_html")
        if not res.found:
            return make_error(f"E006: No element found matching selector '{selector}'")
        return make_text("" if res.value is None else str(res.value))

    async def _handle_get_attr(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Get attribute value of element by CSS selector.

        Returns null (not an error) if element exists but attribute is absent:
        a present element with an absent attribute yields a null value through
        :func:`eval_on_element`, distinct from the ``found=False`` an unmatched
        selector produces, so the two cases no longer need sentinel strings.

        Args:
            arguments: Tool arguments with 'selector' and 'attribute' keys.

        Returns:
            JSON-RPC style response dict with attribute value.
        """
        cdp, err = self.runtime.cdp_or_error()
        if err is not None:
            return err

        selector = arguments.get("selector", "").strip()
        attribute = arguments.get("attribute", "").strip()
        if not selector:
            return make_error("selector is required")
        if not attribute:
            return make_error("attribute is required")

        res = await eval_on_element(
            cdp, selector, f"el.getAttribute({_js_string(attribute)})", label="get_attr"
        )
        if not res.found:
            return make_error(f"E006: No element found matching selector '{selector}'")
        if res.value is None:
            return make_text(f"null (element exists but '{attribute}' attribute is not present)")
        return make_text(str(res.value))

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
        cdp, err = self.runtime.cdp_or_error()
        if err is not None:
            return err

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        js = f"document.querySelectorAll({_js_string(selector)}).length"
        result = await _cdp_call(
            cdp,
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True},
            label="element_exists",
        )

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
        display != none AND visibility != hidden AND opacity > 0. A selector
        that matches nothing is reported as not visible rather than an error,
        so the not-found contract from :func:`eval_on_element` maps cleanly to
        ``visible=False``.

        Args:
            arguments: Tool arguments with 'selector' key.

        Returns:
            JSON-RPC style response dict with visible bool.
        """
        cdp, err = self.runtime.cdp_or_error()
        if err is not None:
            return err

        selector = arguments.get("selector", "").strip()
        if not selector:
            return make_error("selector is required")

        res = await eval_on_element(cdp, selector, _VISIBILITY_EXPR, label="element_visible")
        visible = res.found and bool(res.value)
        return make_text(f'{{"visible": {str(visible).lower()}}}')

    async def _refresh_frame_tree(self) -> None:
        """Refresh the frame tree from Chrome."""
        if self.runtime.client and self.runtime.client.connected:
            try:
                result = await self.runtime.client.send("Page.getFrameTree")
                if "frameTree" in result:
                    self.runtime.frame_manager.update_from_frame_tree(result["frameTree"])
            except Exception:
                logger.debug("Failed to refresh frame tree", exc_info=True)
