"""Async CDP WebSocket client for direct Chrome DevTools Protocol access.

This module provides a lightweight CDP client that connects to Chrome's
WebSocket debugging endpoint. It runs alongside the MCP subprocess —
the MCP subprocess handles existing tools while this client handles
frame-aware tools and event subscriptions.

Key constraint (D-001): This client subscribes ONLY to frame lifecycle
events (Page.frameAttached, Page.frameDetached, Page.frameNavigated,
Page.executionContextCreated, Page.executionContextDestroyed).
The MCP subprocess handles all other CDP domains.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .chrome_utils import BrowserToolsError

try:
    import websockets  # type: ignore[import-untyped]
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    websockets = None  # type: ignore[assignment]
    ws_connect = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class CDPError(BrowserToolsError):
    """Error from a CDP protocol call."""


class CDPClient:
    """Async WebSocket client for Chrome DevTools Protocol.

    Manages a single WebSocket connection to Chrome's debugging endpoint.
    Supports sending CDP commands and subscribing to events.

    Usage:
        async with CDPClient("ws://127.0.0.1:9222/devtools/browser/...") as cdp:
            result = await cdp.send("Page.getFrameTree")
            cdp.on("Page.frameNavigated", handler)
    """

    def __init__(self, ws_url: str, timeout: float = 30.0) -> None:
        """Initialize CDP client.

        Args:
            ws_url: WebSocket debugging URL from Chrome's /json endpoint.
            timeout: Default timeout in seconds for CDP commands.
        """
        self._ws_url = ws_url
        self._timeout = timeout
        self._ws: Any = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._event_handlers: dict[str, list[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        """Whether the WebSocket connection is active."""
        return self._connected

    async def connect(self) -> None:
        """Establish WebSocket connection to Chrome.

        Raises:
            CDPError: If websockets is not installed or connection fails.
        """
        if ws_connect is None:
            raise CDPError("websockets library not installed. Run: uv sync --frozen --no-dev")
        try:
            self._ws = await ws_connect(self._ws_url, max_size=100_000_000)
        except OSError as exc:
            raise CDPError(f"Failed to connect to Chrome CDP: {exc}") from exc
        self._connected = True
        self._reader_task = asyncio.create_task(self._read_loop())

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        self._connected = False
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        if self._ws:
            await self._ws.close()
            self._ws = None
        # Reject any pending requests
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(CDPError("Connection closed"))
        self._pending.clear()

    async def __aenter__(self) -> CDPClient:
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: type | None, exc: BaseException | None, tb: Any) -> None:
        """Async context manager exit."""
        await self.disconnect()

    async def send(
        self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        """Send a CDP command and wait for the response.

        Args:
            method: CDP method name (e.g., "Page.getFrameTree").
            params: Method parameters.
            timeout: Override default timeout for this call.

        Returns:
            CDP response result dictionary.

        Raises:
            CDPError: If the command fails or times out.
        """
        if not self._ws or not self._connected:
            raise CDPError("Not connected to Chrome CDP")

        self._msg_id += 1
        msg_id = self._msg_id
        message: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            message["params"] = params

        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future

        try:
            await self._ws.send(json.dumps(message))
        except (OSError, TypeError, asyncio.CancelledError) as exc:
            self._pending.pop(msg_id, None)
            raise CDPError(f"Failed to send CDP command: {exc}") from exc

        effective_timeout = timeout if timeout is not None else self._timeout
        try:
            response = await asyncio.wait_for(future, timeout=effective_timeout)
        except TimeoutError as exc:
            self._pending.pop(msg_id, None)
            raise CDPError(f"CDP command timed out after {effective_timeout}s: {method}") from exc

        if "error" in response:
            raise CDPError(
                f"CDP error in {method}: {response['error'].get('message', response['error'])}"
            )
        return response.get("result", {})

    def on(self, event: str, handler: Any) -> None:
        """Register an event handler.

        Args:
            event: CDP event name (e.g., "Page.frameNavigated").
            handler: Callable(params_dict) or async callable.
        """
        self._event_handlers.setdefault(event, []).append(handler)

    def off(self, event: str, handler: Any) -> None:
        """Remove an event handler.

        Args:
            event: CDP event name.
            handler: Previously registered handler.
        """
        handlers = self._event_handlers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    async def _read_loop(self) -> None:
        """Read WebSocket messages and route to handlers or pending futures."""
        try:
            async for raw_message in self._ws:
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue

                # Response to a command
                if "id" in message:
                    msg_id = message["id"]
                    future = self._pending.pop(msg_id, None)
                    if future and not future.done():
                        future.set_result(message)
                    continue

                # Event notification
                method = message.get("method")
                params = message.get("params", {})
                if method:
                    for handler in self._event_handlers.get(method, []):
                        try:
                            result = handler(params)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception:
                            logger.exception("Error in CDP event handler for %s", method)
        except asyncio.CancelledError:
            raise
        except OSError:
            logger.debug("CDP read loop ended", exc_info=True)
        finally:
            self._connected = False


def get_browser_ws_url(browser_url: str) -> str | None:
    """Get the browser-level WebSocket URL from Chrome's /json/version endpoint.

    Args:
        browser_url: Base URL of the remote debugging endpoint.

    Returns:
        Browser WebSocket URL, or None if unavailable.
    """
    try:
        request = urllib.request.Request(f"{browser_url}/json/version")
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read())
            return data.get("webSocketDebuggerUrl")
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def get_page_ws_url(browser_url: str, page_index: int = 0) -> str | None:
    """Get the WebSocket URL for a specific page tab.

    Args:
        browser_url: Base URL of the remote debugging endpoint.
        page_index: Index of the page in the tab list (default: first page).

    Returns:
        Page WebSocket URL, or None if unavailable.
    """
    try:
        request = urllib.request.Request(f"{browser_url}/json/list")
        with urllib.request.urlopen(request, timeout=5) as response:
            tabs = json.loads(response.read())
            pages = [t for t in tabs if t.get("type") == "page"]
            if page_index < len(pages):
                return pages[page_index].get("webSocketDebuggerUrl")
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        logger.debug("Failed to get page WS URL from %s", browser_url, exc_info=True)
    return None
