#!/usr/bin/env python3
"""MCP daemon broker for persistent browser-tools sessions.

Keeps the chrome-devtools-mcp subprocess alive across wrapper invocations.
Wrapper processes connect over a Unix domain socket to send tool calls to
the long-lived MCP session, preserving CDP listeners and their buffers
(console messages, network requests, performance traces).

Extended (V2): Also maintains a direct CDP WebSocket client for frame-aware
tools and event subscriptions. The MCP subprocess handles all existing tools;
the CDP client handles frame-aware tools exclusively.

Usage:
    python mcp_daemon.py --socket /path/to/sock --pid-file /path/to/pid \
        --mcp-command '["npx", "-y", "chrome-devtools-mcp@latest", ...]' \
        [--browser-url http://127.0.0.1:9222]

Protocol (over Unix socket):
    Client sends newline-delimited JSON-RPC requests.
    Daemon responds with newline-delimited JSON-RPC responses.
    Client disconnects when done; daemon stays running.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

IDLE_TIMEOUT_SECONDS = 30 * 60  # 30 minutes
MCP_INIT_TIMEOUT_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 120

# Interstitial auto-retry settings
INTERSTITIAL_RETRY_DELAY_SECONDS = 3.0
INTERSTITIAL_MAX_RETRIES = 3
# Challenge types eligible for auto-retry (JS-solvable, no human interaction)
INTERSTITIAL_AUTO_RETRY_TYPES = frozenset(
    {
        "cloudflare_challenge",
        "access_denied",
    }
)

# take_screenshot readiness + blank-frame retry settings.
#
# The chrome-devtools-mcp subprocess takes screenshots immediately when asked,
# which can capture mid-paint frames during CSS transitions or post-reload
# hydration — producing visually blank or half-rendered images even when
# wait_stable (DOM-mutation based) and wait_idle (network based) report ready.
#
# Two-layer mitigation runs on every take_screenshot:
#   1. Pre-capture rAF gate — wait two requestAnimationFrame ticks plus
#      document.fonts.ready so the compositor has flushed at least one frame
#      with the latest layout. Cheap; runs always.
#   2. Post-capture blank check — if the resulting PNG looks near-uniform
#      (very high compression ratio or low luminance variance), wait briefly
#      and retry once. Targets the cases the rAF gate misses.
SCREENSHOT_PAINT_READY_TIMEOUT_MS = 1500
SCREENSHOT_BLANK_RETRY_DELAY_SECONDS = 0.25
SCREENSHOT_BLANK_MAX_RETRIES = 1
# Empirical thresholds. PNG of a near-uniform frame compresses to a tiny
# fraction of raw pixel size; a real screenshot is typically >0.05 bytes/px.
# Stddev threshold is on 0–255 luminance — under ~5 means almost no contrast.
SCREENSHOT_BLANK_BYTES_PER_PIXEL_THRESHOLD = 0.02
SCREENSHOT_BLANK_LUMINANCE_STDDEV_THRESHOLD = 5.0

# Frame-aware tools handled by CDP client, not MCP subprocess
CDP_TOOLS = frozenset(
    {
        "list_frames",
        "select_frame",
        "reset_frame",
        "get_frame_storage",
        "get_frame_events",
        # Accessibility tools (Accessibility CDP domain)
        "ax_find",
        "ax_node",
        # Page export/capture tools (Page CDP domain)
        "export_pdf",
        "screenshot_element",
        "screencast_start",
        "screencast_stop",
        # Semantic wait tools (Runtime.evaluate — needs async CDP, D-006)
        "wait_idle",
        "wait_stable",
        # Content extraction tools (Runtime.evaluate — needs async CDP, D-006)
        "get_text",
        "get_html",
        "get_attr",
        # Element query tools (Runtime.evaluate)
        "element_exists",
        "element_visible",
    }
)

# Tools handled locally by the daemon (no MCP or CDP needed)
LOCAL_TOOLS = frozenset(
    {
        "attach_browser",
        "list_profiles",
        "delete_profile",
    }
)

# Interaction tools blocked in inspect mode
INSPECT_BLOCKED_TOOLS = frozenset(
    {
        "click",
        "hover",
        "fill",
        "fill_form",
        "drag",
        "press_key",
        "upload_file",
        "handle_dialog",
        "type_text",
    }
)

# Navigation tools that trigger interstitial detection
NAVIGATION_TOOLS = frozenset(
    {
        "navigate_page",
        "new_page",
    }
)

# Navigation tools that get a warning in inspect mode
INSPECT_WARN_TOOLS = frozenset(
    {
        "navigate_page",
        "new_page",
        "close_page",
    }
)


def main(
    socket_path: str,
    pid_file: str,
    mcp_command: list[str],
    browser_url: str | None = None,
    mode: str = "full",
    stealth: bool = False,
) -> None:
    """Run the MCP daemon broker.

    Spawns the MCP subprocess, initializes the session, optionally starts
    a CDP WebSocket client, then accepts client connections on a Unix domain
    socket and forwards JSON-RPC requests to the appropriate backend.

    Args:
        socket_path: Path for the Unix domain socket.
        pid_file: Path to write the daemon PID.
        mcp_command: Command to spawn the MCP subprocess.
        browser_url: Chrome remote debugging URL for CDP client.
        mode: Access mode ('full' or 'inspect').
        stealth: Whether to inject stealth patches to reduce automation fingerprinting.

    Returns:
        None.
    """
    Path(pid_file).parent.mkdir(parents=True, exist_ok=True)
    Path(pid_file).write_text(str(os.getpid()))

    Path(socket_path).unlink(missing_ok=True)

    # Redirect stdio for daemon behavior
    devnull_fd = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull_fd, 1)
    os.dup2(devnull_fd, 2)
    os.close(devnull_fd)

    try:
        proc = subprocess.Popen(
            mcp_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except (FileNotFoundError, OSError):
        _cleanup_files(socket_path, pid_file)
        sys.exit(1)

    msg_id_counter = [0]
    pending: dict[int, queue.Queue[dict[str, Any]]] = {}
    lock = threading.Lock()
    last_activity = [time.time()]

    def read_mcp_stdout() -> None:
        """Route JSON-RPC responses from MCP stdout to waiting callers."""
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            resp_id = payload.get("id")
            with lock:
                resp_queue = pending.get(resp_id)
            if resp_queue is not None:
                resp_queue.put(payload)

    reader = threading.Thread(target=read_mcp_stdout, daemon=True)
    reader.start()

    def send_to_mcp(method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Send a JSON-RPC request to MCP and wait for the response.

        Args:
            method: JSON-RPC method name.
            params: Method parameters.
            timeout: Seconds to wait for a response.

        Returns:
            JSON-RPC response dict.
        """
        with lock:
            msg_id_counter[0] += 1
            internal_id = msg_id_counter[0]
            resp_q: queue.Queue[dict[str, Any]] = queue.Queue()
            pending[internal_id] = resp_q

        request = {"jsonrpc": "2.0", "method": method, "params": params, "id": internal_id}
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()

        try:
            response = resp_q.get(timeout=timeout)
        except queue.Empty:
            with lock:
                pending.pop(internal_id, None)
            return {"jsonrpc": "2.0", "error": {"code": -32000, "message": "Timeout"}, "id": 0}

        with lock:
            pending.pop(internal_id, None)
        return response

    # Initialize the MCP session
    init_resp = send_to_mcp(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "browser-tools-daemon", "version": "2.0.0"},
        },
        timeout=MCP_INIT_TIMEOUT_SECONDS,
    )
    if "error" in init_resp:
        proc.terminate()
        _cleanup_files(socket_path, pid_file)
        sys.exit(1)

    # Initialize CDP client and frame manager in a background thread
    cdp_handler = CDPHandler(browser_url, mode, stealth=stealth)
    cdp_thread = threading.Thread(target=cdp_handler.run, daemon=True)
    cdp_thread.start()

    # Health monitor
    def health_check() -> None:
        """Periodically verify the MCP subprocess is alive and daemon is active."""
        while True:
            time.sleep(5)
            if proc.poll() is not None:
                _cleanup_files(socket_path, pid_file)
                os._exit(1)
            if time.time() - last_activity[0] > IDLE_TIMEOUT_SECONDS:
                proc.terminate()
                cdp_handler.stop()
                _cleanup_files(socket_path, pid_file)
                os._exit(0)

    health = threading.Thread(target=health_check, daemon=True)
    health.start()

    # Listen on Unix socket
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    server.listen(2)
    server.settimeout(10)

    def handle_shutdown(signum: int, frame: Any) -> None:
        """Gracefully terminate and clean up.

        Args:
            signum: Signal number.
            frame: Interrupted stack frame.

        Returns:
            None.
        """
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        cdp_handler.stop()
        server.close()
        _cleanup_files(socket_path, pid_file)
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    while True:
        try:
            client_sock, _ = server.accept()
        except TimeoutError:
            continue
        except OSError:
            break

        last_activity[0] = time.time()
        try:
            _handle_client(
                client_sock,
                proc,
                msg_id_counter,
                pending,
                lock,
                last_activity,
                cdp_handler,
            )
        except Exception:
            pass
        finally:
            try:
                client_sock.close()
            except OSError:
                pass


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

    def run(self) -> None:
        """Run the asyncio event loop (called in a background thread)."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        self._loop.run_until_complete(self._main())

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

            ws_url = get_page_ws_url(self._browser_url)
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
            pass  # Non-fatal — stealth is best-effort

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
        if not self._loop:
            return _make_error("CDP handler not initialized")

        future = asyncio.run_coroutine_threadsafe(self._dispatch_tool(name, arguments), self._loop)
        try:
            return future.result(timeout=REQUEST_TIMEOUT_SECONDS)
        except Exception as exc:
            return _make_error(str(exc))

    async def _dispatch_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Route a tool call to the appropriate handler.

        Args:
            name: Tool name.
            arguments: Tool arguments.

        Returns:
            JSON-RPC style response dict.
        """
        fm = self._frame_manager
        cdp = self._cdp_client

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
            return _make_error(f"Unknown CDP tool: {name}")

    def _handle_list_frames(self) -> dict[str, Any]:
        """Handle list_frames tool."""
        fm = self._frame_manager
        if fm is None:
            return _make_error("Frame manager not initialized")

        frames = fm.get_flat_frames()
        if not frames:
            # Try refreshing frame tree if CDP is available
            if self._cdp_client and self._cdp_client.connected:
                asyncio.ensure_future(self._refresh_frame_tree())
                return _make_text("No frames available. Refreshing frame tree...")

            return _make_text("No frames available. CDP client not connected.")

        lines = ["Frames in current page:\n"]
        for frame in frames:
            indent = "  " * frame["depth"]
            selected = " [selected]" if frame["frameId"] == fm.selected_frame_id else ""
            name = f' name="{frame["name"]}"' if frame.get("name") else ""
            lines.append(f"{indent}{frame['frameId']}: {frame['url']}{name}{selected}")
        return _make_text("\n".join(lines))

    def _handle_select_frame(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle select_frame tool."""
        fm = self._frame_manager
        if fm is None:
            return _make_error("Frame manager not initialized")

        url_pattern = arguments.get("url_pattern", "")
        if not url_pattern:
            return _make_error("url_pattern is required")

        frame = fm.select_frame_by_url(url_pattern)
        if frame is None:
            return _make_error(
                f"E002: No frame found matching '{url_pattern}'. "
                "Use list_frames to see available frames."
            )

        ctx_id = fm.get_selected_execution_context_id()
        ctx_info = f", executionContextId={ctx_id}" if ctx_id else " (no execution context yet)"
        return _make_text(
            f"Selected frame: {frame.frame_id}\n"
            f"URL: {frame.url}\n"
            f"Origin: {frame.security_origin}{ctx_info}"
        )

    def _handle_reset_frame(self) -> dict[str, Any]:
        """Handle reset_frame tool."""
        fm = self._frame_manager
        if fm is None:
            return _make_error("Frame manager not initialized")

        fm.reset_frame()
        return _make_text("Frame selection cleared. Now targeting top-level page.")

    def _handle_get_frame_events(self) -> dict[str, Any]:
        """Handle get_frame_events tool."""
        fm = self._frame_manager
        if fm is None:
            return _make_error("Frame manager not initialized")

        events = fm.drain_events()
        if not events:
            return _make_text("No frame events since last check.")

        lines = [f"{len(events)} frame event(s):\n"]
        for evt in events:
            url_info = f" → {evt['url']}" if evt.get("url") else ""
            lines.append(f"  [{evt['type']}] {evt['frameId']}{url_info}")
        return _make_text("\n".join(lines))

    async def _handle_get_frame_storage(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle get_frame_storage tool."""
        fm = self._frame_manager
        cdp = self._cdp_client
        if fm is None or cdp is None or not cdp.connected:
            return _make_error("CDP client not connected")

        selected = fm.get_selected_frame()
        if selected is None:
            return _make_error("No frame selected. Use select_frame first.")

        storage_types = arguments.get(
            "storage_types", ["cookies", "localStorage", "sessionStorage"]
        )
        ctx_id = fm.get_selected_execution_context_id()
        result_parts: list[str] = []

        if "cookies" in storage_types:
            try:
                cookies_result = await cdp.send("Network.getCookies", {"urls": [selected.url]})
                cookies = cookies_result.get("cookies", [])
                result_parts.append(f"Cookies ({len(cookies)}):")
                for c in cookies[:20]:
                    result_parts.append(f"  {c.get('name')}: {c.get('value', '')[:50]}")
            except Exception as exc:
                result_parts.append(f"Cookies: error - {exc}")

        if ctx_id and "localStorage" in storage_types:
            try:
                ls_result = await cdp.send(
                    "Runtime.evaluate",
                    {
                        "expression": "JSON.stringify(Object.fromEntries(Object.entries(localStorage).slice(0, 20)))",
                        "contextId": ctx_id,
                        "returnByValue": True,
                    },
                )
                ls_value = ls_result.get("result", {}).get("value", "{}")
                result_parts.append(f"\nlocalStorage: {ls_value}")
            except Exception as exc:
                result_parts.append(f"\nlocalStorage: error - {exc}")

        if ctx_id and "sessionStorage" in storage_types:
            try:
                ss_result = await cdp.send(
                    "Runtime.evaluate",
                    {
                        "expression": "JSON.stringify(Object.fromEntries(Object.entries(sessionStorage).slice(0, 20)))",
                        "contextId": ctx_id,
                        "returnByValue": True,
                    },
                )
                ss_value = ss_result.get("result", {}).get("value", "{}")
                result_parts.append(f"\nsessionStorage: {ss_value}")
            except Exception as exc:
                result_parts.append(f"\nsessionStorage: error - {exc}")

        if not result_parts:
            return _make_text("No storage data retrieved.")

        return _make_text("\n".join(result_parts))

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
            return _make_error("CDP client not connected")

        role = arguments.get("role", "").strip()
        name = arguments.get("name", "").strip()

        if not role and not name:
            return _make_error("E007: ax_find requires at least one of 'role' or 'name'")

        params: dict[str, Any] = {}
        if role:
            params["role"] = role
        if name:
            params["accessibleName"] = name

        try:
            result = await cdp.send("Accessibility.queryAXTree", params)
        except Exception as exc:
            return _make_error(f"Accessibility.queryAXTree failed: {exc}")

        nodes = result.get("nodes", [])
        # Filter out ignored nodes
        visible_nodes = [n for n in nodes if not n.get("ignored", False)]

        if not visible_nodes:
            criteria = []
            if role:
                criteria.append(f"role={role!r}")
            if name:
                criteria.append(f"name={name!r}")
            return _make_text(f"No accessibility nodes found matching {', '.join(criteria)}.")

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

        return _make_text("\n".join(lines))

    async def _handle_ax_node(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Inspect a single element's accessibility properties.

        Args:
            arguments: Tool arguments with 'selector' key.

        Returns:
            JSON-RPC style response dict with AX node properties.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return _make_error("CDP client not connected")

        selector = arguments.get("selector", "").strip()
        if not selector:
            return _make_error("selector is required")

        # Resolve selector to a backend DOM node ID
        try:
            eval_result = await cdp.send(
                "Runtime.evaluate",
                {
                    "expression": f"document.querySelector({selector!r})",
                    "returnByValue": False,
                },
            )
        except Exception as exc:
            return _make_error(f"Runtime.evaluate failed: {exc}")

        remote_obj = eval_result.get("result", {})
        if remote_obj.get("type") == "undefined" or remote_obj.get("subtype") == "null":
            return _make_error(f"E006: No element found matching selector '{selector}'")

        object_id = remote_obj.get("objectId")
        if not object_id:
            return _make_error(f"E006: No element found matching selector '{selector}'")

        # Get the backend DOM node ID
        try:
            node_result = await cdp.send("DOM.requestNode", {"objectId": object_id})
        except Exception as exc:
            return _make_error(f"DOM.requestNode failed: {exc}")

        backend_node_id = node_result.get("nodeId")
        if not backend_node_id:
            return _make_error("Could not resolve element to DOM node")

        # Get partial AX tree for this node
        try:
            ax_result = await cdp.send(
                "Accessibility.getPartialAXTree",
                {"nodeId": backend_node_id, "fetchRelatives": False},
            )
        except Exception as exc:
            return _make_error(f"Accessibility.getPartialAXTree failed: {exc}")

        nodes = ax_result.get("nodes", [])
        if not nodes:
            return _make_text(f"No accessibility info found for selector '{selector}'")

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

        return _make_text("\n".join(lines))

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
            return _make_error("CDP client not connected")

        landscape = arguments.get("landscape", False)
        print_background = arguments.get("print_background", True)
        out_path = arguments.get("path", "")
        if not out_path:
            out_path = f"{int(time.time())}_page.pdf"

        try:
            result = await cdp.send(
                "Page.printToPDF",
                {
                    "landscape": landscape,
                    "printBackground": print_background,
                    "transferMode": "ReturnAsBase64",
                },
            )
        except Exception as exc:
            return _make_error(f"Page.printToPDF failed: {exc}")

        pdf_data = result.get("data", "")
        if not pdf_data:
            return _make_error("No PDF data returned from Chrome")

        try:
            pdf_bytes = base64.b64decode(pdf_data)
            abs_path = str(Path(out_path).resolve())
            Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
            Path(abs_path).write_bytes(pdf_bytes)
        except Exception as exc:
            return _make_error(f"E009: Failed to write PDF to '{out_path}': {exc}")

        return _make_text(f"PDF saved to: {abs_path} ({len(pdf_bytes):,} bytes)")

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
            return _make_error("CDP client not connected")

        selector = arguments.get("selector", "").strip()
        if not selector:
            return _make_error("selector is required")

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
            eval_result = await cdp.send(
                "Runtime.evaluate",
                {"expression": js, "returnByValue": True},
            )
        except Exception as exc:
            return _make_error(f"Runtime.evaluate failed: {exc}")

        rect = eval_result.get("result", {}).get("value")
        if rect is None:
            return _make_error(f"E006: No element found matching selector '{selector}'")

        if rect.get("width", 0) == 0 or rect.get("height", 0) == 0:
            return _make_error(
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
            shot_result = await cdp.send(
                "Page.captureScreenshot",
                {"format": "png", "clip": clip},
            )
        except Exception as exc:
            return _make_error(f"Page.captureScreenshot failed: {exc}")

        img_data = shot_result.get("data", "")
        if not img_data:
            return _make_error("No image data returned from Chrome")

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
                lines.append(f"Warning: could not write file: {exc}")

        lines.append(f"data:image/png;base64,{img_data}")
        return _make_text("\n".join(lines))

    # ------------------------------------------------------------------ #
    # Screencast capture (catches transient states like loading spinners) #
    # ------------------------------------------------------------------ #

    def _on_screencast_frame(self, params: dict[str, Any]) -> None:
        """Buffer a screencast frame and ack it so the stream continues.

        Called from the CDP read loop, so this stays synchronous: the ack is
        scheduled with ``ensure_future`` rather than awaited here, because the
        read loop is what resolves the ack's response — awaiting it inline would
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
            asyncio.ensure_future(
                cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
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
            return _make_error("CDP client not connected")
        if self._screencast_active:
            return _make_error("screencast already recording; call screencast_stop first")

        fmt = str(arguments.get("format", "jpeg")).lower()
        if fmt not in ("jpeg", "png"):
            return _make_error("format must be 'jpeg' or 'png'")

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
            await cdp.send("Page.startScreencast", params)
        except Exception as exc:
            self._screencast_active = False
            cdp.off("Page.screencastFrame", self._on_screencast_frame)
            return _make_error(f"Page.startScreencast failed: {exc}")
        return _make_text(
            "Screencast recording. Drive the UI with click/fill/navigate, "
            "then call screencast_stop to write the frames."
        )

    async def _handle_screencast_stop(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Stop the screencast and write buffered frames to a directory.

        Args:
            arguments: 'dir' (required) — directory to write timestamped frames
                plus a frames.json manifest.

        Returns:
            JSON-RPC style response dict.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return _make_error("CDP client not connected")
        if not self._screencast_active:
            return _make_error("no screencast in progress; call screencast_start first")

        self._screencast_active = False
        try:
            await cdp.send("Page.stopScreencast")
        except Exception:
            pass  # stopping is best-effort; we still return what we captured
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
            return _make_error("dir is required to write screencast frames")
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
        except Exception as exc:
            return _make_error(f"could not write frames: {exc}")
        return _make_text("\n".join(lines))

    # ------------------------------------------------------------------ #
    # Semantic wait tools                                                  #
    # ------------------------------------------------------------------ #

    async def _handle_wait_idle(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Wait for network to be idle (no new resources loaded for idle_ms).

        Uses PerformanceResourceTiming to detect in-flight activity.
        Does not subscribe to Network CDP events — avoids the two-CDP-client
        constraint (D-006).

        Args:
            arguments: Tool arguments with optional 'timeout_ms' and 'idle_ms'.

        Returns:
            JSON-RPC style response dict.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return _make_error("CDP client not connected")

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
            result = await cdp.send(
                "Runtime.evaluate",
                {
                    "expression": js,
                    "awaitPromise": True,
                    "returnByValue": True,
                    "timeout": timeout_ms + 2000,
                },
            )
        except Exception as exc:
            return _make_error(f"wait_idle failed: {exc}")

        # Check for JS exception
        exc_details = result.get("exceptionDetails")
        if exc_details:
            msg = exc_details.get("exception", {}).get("description", "wait_idle failed")
            return _make_error(msg)

        msg = result.get("result", {}).get("value", "idle")
        return _make_text(f"Network idle: {msg}")

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
            return _make_error("CDP client not connected")

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
            result = await cdp.send(
                "Runtime.evaluate",
                {
                    "expression": js,
                    "awaitPromise": True,
                    "returnByValue": True,
                    "timeout": timeout_ms + 2000,
                },
            )
        except Exception as exc:
            return _make_error(f"wait_stable failed: {exc}")

        exc_details = result.get("exceptionDetails")
        if exc_details:
            msg = exc_details.get("exception", {}).get("description", "wait_stable failed")
            return _make_error(msg)

        msg = result.get("result", {}).get("value", "stable")
        return _make_text(f"DOM stable: {msg}")

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
        result = await cdp.send(
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
            return _make_error("CDP client not connected")

        selector = arguments.get("selector", "").strip()
        if not selector:
            return _make_error("selector is required")

        try:
            result = await self._query_element_js(cdp, selector, "el.textContent")
        except Exception as exc:
            return _make_error(f"get_text failed: {exc}")

        if result is None:
            return _make_error(f"E006: No element found matching selector '{selector}'")

        text = result.get("result", {}).get("value", "")
        return _make_text(str(text))

    async def _handle_get_html(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Get outer HTML of element by CSS selector.

        Args:
            arguments: Tool arguments with 'selector' key.

        Returns:
            JSON-RPC style response dict with element outer HTML.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return _make_error("CDP client not connected")

        selector = arguments.get("selector", "").strip()
        if not selector:
            return _make_error("selector is required")

        try:
            result = await self._query_element_js(cdp, selector, "el.outerHTML")
        except Exception as exc:
            return _make_error(f"get_html failed: {exc}")

        if result is None:
            return _make_error(f"E006: No element found matching selector '{selector}'")

        html = result.get("result", {}).get("value", "")
        return _make_text(str(html))

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
            return _make_error("CDP client not connected")

        selector = arguments.get("selector", "").strip()
        attribute = arguments.get("attribute", "").strip()
        if not selector:
            return _make_error("selector is required")
        if not attribute:
            return _make_error("attribute is required")

        js = f"""
        (() => {{
            const el = document.querySelector({selector!r});
            if (!el) return '__ELEMENT_NOT_FOUND__';
            const val = el.getAttribute({attribute!r});
            return val === null ? '__ATTR_NULL__' : val;
        }})()
        """
        try:
            result = await cdp.send(
                "Runtime.evaluate",
                {"expression": js, "returnByValue": True},
            )
        except Exception as exc:
            return _make_error(f"get_attr failed: {exc}")

        val = result.get("result", {}).get("value", "__ELEMENT_NOT_FOUND__")
        if val == "__ELEMENT_NOT_FOUND__":
            return _make_error(f"E006: No element found matching selector '{selector}'")
        if val == "__ATTR_NULL__":
            return _make_text(f"null (element exists but '{attribute}' attribute is not present)")
        return _make_text(str(val))

    # ------------------------------------------------------------------ #
    # Element query tools                                                  #
    # ------------------------------------------------------------------ #

    async def _handle_element_exists(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Check if one or more elements matching a CSS selector exist.

        Never throws on not-found — this is a boolean query.

        Args:
            arguments: Tool arguments with 'selector' key.

        Returns:
            JSON-RPC style response dict with exists bool and count.
        """
        cdp = self._cdp_client
        if cdp is None or not cdp.connected:
            return _make_error("CDP client not connected")

        selector = arguments.get("selector", "").strip()
        if not selector:
            return _make_error("selector is required")

        js = f"document.querySelectorAll({selector!r}).length"
        try:
            result = await cdp.send(
                "Runtime.evaluate",
                {"expression": js, "returnByValue": True},
            )
        except Exception as exc:
            return _make_error(f"element_exists failed: {exc}")

        # querySelectorAll throws for invalid selectors — check for exception
        exc_details = result.get("exceptionDetails")
        if exc_details:
            msg = exc_details.get("exception", {}).get("description", "Invalid selector")
            return _make_error(f"Invalid selector '{selector}': {msg}")

        count = result.get("result", {}).get("value", 0)
        exists = count > 0
        return _make_text(f'{{"exists": {str(exists).lower()}, "count": {count}}}')

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
            return _make_error("CDP client not connected")

        selector = arguments.get("selector", "").strip()
        if not selector:
            return _make_error("selector is required")

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
            result = await cdp.send(
                "Runtime.evaluate",
                {"expression": js, "returnByValue": True},
            )
        except Exception as exc:
            return _make_error(f"element_visible failed: {exc}")

        visible = result.get("result", {}).get("value", False)
        return _make_text(f'{{"visible": {str(visible).lower()}}}')

    async def _refresh_frame_tree(self) -> None:
        """Refresh the frame tree from Chrome."""
        if self._cdp_client and self._cdp_client.connected:
            try:
                result = await self._cdp_client.send("Page.getFrameTree")
                if "frameTree" in result:
                    self._frame_manager.update_from_frame_tree(result["frameTree"])
            except Exception:
                pass

    def await_paint_ready(self, timeout_ms: int = SCREENSHOT_PAINT_READY_TIMEOUT_MS) -> bool:
        """Block until Chrome has painted at least one stable frame.

        Used as a pre-capture gate for take_screenshot. Waits for two
        requestAnimationFrame ticks (one to flush the current frame, one to
        confirm the next frame committed) plus document.fonts.ready, so any
        in-flight CSS transition / font swap / layout reflow has reached
        the compositor before we capture.

        DOM-mutation waits (wait_stable) miss this case because CSS
        transform/opacity animations don't fire MutationObserver callbacks
        — the DOM looks settled while pixels are still moving.

        Returns:
            True if the gate completed; False if CDP is unavailable or the
            evaluate timed out. Never raises — screenshot must still proceed
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

        # Exhausted retries — report whatever remains plus any non-retryable
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
            return []


def _handle_client(
    client_sock: socket.socket,
    mcp_proc: subprocess.Popen[str],
    msg_id_counter: list[int],
    pending: dict[int, queue.Queue[dict[str, Any]]],
    lock: threading.Lock,
    last_activity: list[float],
    cdp_handler: CDPHandler,
) -> None:
    """Handle one client connection with sequential JSON-RPC requests.

    Routes tool calls to either the MCP subprocess, CDP handler, or local
    handlers based on the tool name.

    Args:
        client_sock: Connected client socket.
        mcp_proc: Running MCP subprocess.
        msg_id_counter: Shared message ID counter.
        pending: Map of internal message IDs to response queues.
        lock: Lock protecting pending and msg_id_counter.
        last_activity: Last activity timestamp.
        cdp_handler: CDP/frame tool handler.

    Returns:
        None.
    """
    client_sock.settimeout(REQUEST_TIMEOUT_SECONDS)
    buf = b""

    while True:
        try:
            data = client_sock.recv(65536)
        except TimeoutError:
            break
        if not data:
            break

        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue

            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue

            last_activity[0] = time.time()
            client_id = request.get("id")

            # Extract tool name for routing
            params = request.get("params", {})
            tool_name = params.get("name", "")

            # Check inspect mode
            if cdp_handler._mode == "inspect":
                if tool_name in INSPECT_BLOCKED_TOOLS:
                    response = {
                        "jsonrpc": "2.0",
                        "result": _make_error(
                            f"E004: Tool '{tool_name}' is blocked in inspect mode. "
                            "Observation tools only: take_snapshot, take_screenshot, "
                            "list_pages, evaluate_script, list_console_messages, "
                            "list_network_requests, list_frames, get_frame_storage."
                        )["result"],
                        "id": client_id,
                    }
                    try:
                        client_sock.sendall(json.dumps(response).encode() + b"\n")
                    except OSError:
                        break
                    continue

            # Route to appropriate handler
            if tool_name in CDP_TOOLS:
                response = cdp_handler.call_tool(tool_name, params.get("arguments", {}))
                response["id"] = client_id
                try:
                    client_sock.sendall(json.dumps(response).encode() + b"\n")
                except OSError:
                    break
                continue

            if tool_name in LOCAL_TOOLS:
                response = _handle_local_tool(tool_name, params.get("arguments", {}))
                response["id"] = client_id
                try:
                    client_sock.sendall(json.dumps(response).encode() + b"\n")
                except OSError:
                    break
                continue

            # take_screenshot is forwarded to the MCP subprocess like other
            # default tools, but wrapped with a paint-ready gate and a
            # blank-frame retry so the LLM doesn't get back an image that
            # captured a mid-animation / mid-hydration frame. See
            # _take_screenshot_with_paint_gate for the full sequence.
            if tool_name == "take_screenshot":
                response = _take_screenshot_with_paint_gate(
                    request,
                    client_id,
                    mcp_proc,
                    msg_id_counter,
                    pending,
                    lock,
                    cdp_handler,
                )
            else:
                # Default: forward to MCP subprocess unchanged.
                response = _forward_to_mcp_subprocess(
                    request,
                    client_id,
                    mcp_proc,
                    msg_id_counter,
                    pending,
                    lock,
                )

            # Post-navigation interstitial detection with auto-retry
            if tool_name in NAVIGATION_TOOLS and "error" not in response:
                detection_result = cdp_handler.run_post_navigation_detection()
                if detection_result and detection_result.get("detections"):
                    from .interstitial import format_interstitials

                    detections = detection_result["detections"]
                    auto_retried = detection_result.get("auto_retried", False)
                    retries_used = detection_result.get("retries_used", 0)
                    warning = format_interstitials(
                        detections,
                        auto_retried=auto_retried,
                        retries_used=retries_used,
                    )
                    if warning:
                        _append_text_to_response(response, f"\n\n{warning}")
                elif detection_result and detection_result.get("auto_retried"):
                    # Challenge was detected but auto-cleared
                    retries_used = detection_result.get("retries_used", 0)
                    _append_text_to_response(
                        response,
                        f"\n\n✅ Anti-bot challenge detected and auto-cleared "
                        f"after {retries_used} retry(ies).",
                    )

            try:
                client_sock.sendall(json.dumps(response).encode() + b"\n")
            except OSError:
                break


def _forward_to_mcp_subprocess(
    request: dict[str, Any],
    client_id: Any,
    mcp_proc: subprocess.Popen[str],
    msg_id_counter: list[int],
    pending: dict[int, queue.Queue[dict[str, Any]]],
    lock: threading.Lock,
) -> dict[str, Any]:
    """Send a tool-call request to the chrome-devtools-mcp subprocess and
    block until its response arrives (or REQUEST_TIMEOUT_SECONDS elapses).

    Extracted from the inline default branch in _handle_client so the
    take_screenshot wrapper can call it more than once for retries while
    keeping the original numbering/pending bookkeeping centralized.

    The MCP subprocess identifies responses by id, so each forward gets a
    fresh internal id; the caller's id is restored on the way out.
    """
    with lock:
        msg_id_counter[0] += 1
        internal_id = msg_id_counter[0]
        resp_q: queue.Queue[dict[str, Any]] = queue.Queue()
        pending[internal_id] = resp_q

    request["id"] = internal_id
    assert mcp_proc.stdin is not None
    mcp_proc.stdin.write(json.dumps(request) + "\n")
    mcp_proc.stdin.flush()

    try:
        response = resp_q.get(timeout=REQUEST_TIMEOUT_SECONDS)
    except queue.Empty:
        response = {
            "jsonrpc": "2.0",
            "error": {"code": -32000, "message": "Request timeout"},
            "id": client_id,
        }
    else:
        response["id"] = client_id

    with lock:
        pending.pop(internal_id, None)

    return response


def _extract_screenshot_png_b64(response: dict[str, Any]) -> str | None:
    """Find the PNG base64 payload in a take_screenshot MCP response.

    chrome-devtools-mcp returns screenshots as either an `image` content
    block (`{"type": "image", "data": "<b64>", ...}`) or, for some
    versions, embedded as a `data:image/...` URI inside a text block.
    We probe for both shapes and return the raw base64 string, or None
    if no image is found (e.g. the screenshot was saved-to-file only,
    or the response is an error).
    """
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if not isinstance(content, list):
        return None

    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            data = block.get("data")
            if isinstance(data, str) and data:
                return data
        if block.get("type") == "text":
            text = block.get("text", "")
            marker = "data:image/"
            idx = text.find(marker)
            if idx >= 0:
                # Extract from "data:image/<fmt>;base64,<payload>"
                comma = text.find(",", idx)
                if comma > 0:
                    payload = text[comma + 1 :].strip()
                    # Stop at first whitespace/newline — payload should be a
                    # contiguous base64 run with no embedded whitespace.
                    end = len(payload)
                    for i, ch in enumerate(payload):
                        if ch in (" ", "\n", "\r", "\t"):
                            end = i
                            break
                    return payload[:end] or None
    return None


def _screenshot_looks_blank(png_b64: str) -> bool:
    """Heuristically decide whether a captured PNG is effectively blank.

    Two signals, in order of precision:

    1. Luminance variance (preferred) — decode via Pillow if available,
       downscale to a small grid for speed, compute stddev of grayscale
       pixels. A stddev under SCREENSHOT_BLANK_LUMINANCE_STDDEV_THRESHOLD
       means almost no contrast (solid color, gradient-only loader, etc.).

    2. Compressed-size ratio (fallback) — uniform pixels compress to a
       tiny fraction of raw size. Parsing just the IHDR chunk gives us
       width/height with no decoding cost. If Pillow is not importable
       in the daemon's Python, this is the floor.

    Returns False on any decoding error so we never accidentally drop a
    real screenshot due to a corrupt-looking buffer.
    """
    import base64 as _b64

    try:
        png_bytes = _b64.b64decode(png_b64, validate=False)
    except Exception:
        return False
    if len(png_bytes) < 24 or png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        return False

    # IHDR is always the first chunk after the 8-byte signature; layout:
    # 4 bytes length, 4 bytes "IHDR", 4 bytes width, 4 bytes height, ...
    try:
        width = int.from_bytes(png_bytes[16:20], "big")
        height = int.from_bytes(png_bytes[20:24], "big")
    except Exception:
        return False
    if width <= 0 or height <= 0:
        return False

    # Try Pillow path for the more precise variance signal.
    try:
        import io as _io

        from PIL import Image  # type: ignore

        img = Image.open(_io.BytesIO(png_bytes)).convert("L")
        # Thumbnail to bound CPU on large fullPage screenshots.
        img.thumbnail((96, 96))
        pixels = list(img.getdata())
        if not pixels:
            return False
        n = len(pixels)
        mean = sum(pixels) / n
        var = sum((p - mean) * (p - mean) for p in pixels) / n
        stddev = var**0.5
        return stddev < SCREENSHOT_BLANK_LUMINANCE_STDDEV_THRESHOLD
    except Exception:
        # Pillow missing or decode failed — fall through to size-ratio.
        pass

    ratio = len(png_bytes) / float(width * height)
    return ratio < SCREENSHOT_BLANK_BYTES_PER_PIXEL_THRESHOLD


def _take_screenshot_with_paint_gate(
    request: dict[str, Any],
    client_id: Any,
    mcp_proc: subprocess.Popen[str],
    msg_id_counter: list[int],
    pending: dict[int, queue.Queue[dict[str, Any]]],
    lock: threading.Lock,
    cdp_handler: CDPHandler,
) -> dict[str, Any]:
    """Wrap take_screenshot with a pre-capture rAF gate and post-capture
    blank-frame retry.

    Sequence on each attempt:
        1. await_paint_ready() — wait for the next compositor frame so any
           in-flight CSS animation/font swap/hydration reflow has settled.
        2. Forward the original take_screenshot call to chrome-devtools-mcp.
        3. Inspect the returned PNG; if it looks blank, sleep a short delay
           and loop. Up to SCREENSHOT_BLANK_MAX_RETRIES retries.

    If retries are exhausted, the last response is returned as-is with a
    short note appended so the caller can see why the image may be empty
    (e.g. the page really is blank — about:blank, a fully transparent
    overlay, or a long-loading SPA shell).
    """
    last_response: dict[str, Any] = {}
    for attempt in range(SCREENSHOT_BLANK_MAX_RETRIES + 1):
        # rAF gate is best-effort: it returns False if CDP is unavailable,
        # in which case we still proceed to capture so the tool stays
        # functional in headless/no-CDP environments.
        cdp_handler.await_paint_ready()

        last_response = _forward_to_mcp_subprocess(
            request, client_id, mcp_proc, msg_id_counter, pending, lock
        )

        # Errors from the subprocess shouldn't trigger a retry — they're
        # not blank-frame failures and would just compound the user's wait.
        if "error" in last_response:
            return last_response

        png_b64 = _extract_screenshot_png_b64(last_response)
        if png_b64 is None:
            # No image content found (e.g. saved-to-file only response).
            # Nothing for us to inspect; trust the subprocess.
            return last_response

        if not _screenshot_looks_blank(png_b64):
            return last_response

        # Looks blank. If we have retries left, brief sleep so any
        # animation/transition gets more time, then loop.
        if attempt < SCREENSHOT_BLANK_MAX_RETRIES:
            time.sleep(SCREENSHOT_BLANK_RETRY_DELAY_SECONDS)

    # Exhausted retries — return the last attempt with a diagnostic note
    # so the caller knows the daemon already tried to wait through this.
    _append_text_to_response(
        last_response,
        "\n\n⚠️ Screenshot looked near-uniform after "
        f"{SCREENSHOT_BLANK_MAX_RETRIES + 1} attempts with paint-ready waits. "
        "The page may genuinely be blank, fully transparent, or still "
        "rendering — try wait_stable / wait_idle before retrying.",
    )
    return last_response


def _handle_local_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle tools that execute locally in the daemon.

    Args:
        name: Tool name.
        arguments: Tool arguments.

    Returns:
        JSON-RPC style response dict.
    """
    if name == "list_profiles":
        from .persistent_browser import list_profiles

        profiles = list_profiles()
        if not profiles:
            return _make_text("No named profiles found.")
        lines = ["Named profiles:"]
        for p in profiles:
            lines.append(f"  {p}")
        return _make_text("\n".join(lines))

    elif name == "delete_profile":
        from .persistent_browser import delete_profile

        profile_name = arguments.get("name", "")
        if not profile_name:
            return _make_error("Profile name is required")
        if delete_profile(profile_name):
            return _make_text(f"Profile '{profile_name}' deleted.")
        return _make_error(f"Profile '{profile_name}' not found.")

    elif name == "attach_browser":
        # attach_browser is handled at the session level, not daemon level
        return _make_text("attach_browser handled by session wrapper")

    return _make_error(f"Unknown local tool: {name}")


def _append_text_to_response(response: dict[str, Any], text: str) -> None:
    """Append text content to an existing JSON-RPC response.

    Args:
        response: JSON-RPC response dict to mutate.
        text: Text to append.
    """
    result = response.get("result", {})
    content = result.get("content", [])
    if isinstance(content, list):
        content.append({"type": "text", "text": text})
        result["content"] = content
        response["result"] = result


def _make_text(text: str) -> dict[str, Any]:
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


def _make_error(message: str) -> dict[str, Any]:
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


def _cleanup_files(socket_path: str, pid_file: str) -> None:
    """Remove socket and PID files.

    Args:
        socket_path: Unix socket path.
        pid_file: PID file path.

    Returns:
        None.
    """
    Path(socket_path).unlink(missing_ok=True)
    Path(pid_file).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MCP daemon broker")
    parser.add_argument("--socket", required=True, help="Unix domain socket path")
    parser.add_argument("--pid-file", required=True, help="PID file path")
    parser.add_argument("--mcp-command", required=True, help="JSON-encoded MCP server command")
    parser.add_argument("--browser-url", help="Chrome remote debugging URL for CDP client")
    parser.add_argument(
        "--mode", default="full", choices=["full", "inspect"], help="Access mode (default: full)"
    )
    parser.add_argument(
        "--stealth",
        action="store_true",
        help="Inject stealth patches to reduce automation fingerprinting",
    )
    args = parser.parse_args()

    try:
        command = json.loads(args.mcp_command)
    except json.JSONDecodeError:
        sys.exit(1)

    main(args.socket, args.pid_file, command, args.browser_url, args.mode, args.stealth)
