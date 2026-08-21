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
import contextlib
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    from .cdp_constants import (
        REQUEST_TIMEOUT_SECONDS,
        SCREENSHOT_BLANK_MAX_RETRIES,
        SCREENSHOT_BLANK_RETRY_DELAY_SECONDS,
    )
    from .cdp_handler import CDPHandler
    from .interstitial import format_interstitials
    from .mcp_broker import McpBroker
    from .mcp_response import append_text, make_error
    from .screenshot_utils import (
        extract_screenshot_png_b64,
        screenshot_looks_blank,
    )
    from .tool_registry import (
        CDP_TOOLS,
        INSPECT_BLOCKED_TOOLS,
        NAVIGATION_TOOLS,
        SCREENSHOT_GATE_TOOLS,
    )
except ImportError:
    from cdp_constants import (  # type: ignore[import-untyped,no-redef]
        REQUEST_TIMEOUT_SECONDS,
        SCREENSHOT_BLANK_MAX_RETRIES,
        SCREENSHOT_BLANK_RETRY_DELAY_SECONDS,
    )
    from cdp_handler import (  # type: ignore[import-untyped,no-redef]
        CDPHandler,
    )
    from interstitial import (  # type: ignore[import-untyped,no-redef]
        format_interstitials,
    )
    from mcp_broker import McpBroker  # type: ignore[import-untyped,no-redef]
    from mcp_response import (  # type: ignore[import-untyped,no-redef]
        append_text,
        make_error,
    )
    from screenshot_utils import (  # type: ignore[import-untyped,no-redef]
        extract_screenshot_png_b64,
        screenshot_looks_blank,
    )
    from tool_registry import (  # type: ignore[import-untyped,no-redef]
        CDP_TOOLS,
        INSPECT_BLOCKED_TOOLS,
        NAVIGATION_TOOLS,
        SCREENSHOT_GATE_TOOLS,
    )

IDLE_TIMEOUT_SECONDS = 30 * 60  # 30 minutes
MCP_INIT_TIMEOUT_SECONDS = 60

#: Snapshot/UID tools the native backend serves by default (RFC-01 Phase 2 flip,
#: ticket #41). Under the default ``native`` engine these route to the CDP-native
#: read/interaction path; under ``--engine mcp`` they fall through to the Node
#: broker exactly as before. Their name, argument, and response shapes are
#: unchanged -- only the backend behind them moves.
NATIVE_DISPATCH_TOOLS = frozenset({"take_snapshot", "click", "fill"})
DEFAULT_DISPATCH_ENGINE = "native"


def _terminate_owned_chrome(
    chrome_pid: int | None,
    chrome_owned: bool,
    chrome_user_data_dir: str | None,
) -> None:
    """Quit the tool-launched Chrome when the daemon shuts down or idles out.

    Only fires when browser-tools launched Chrome into a private automation
    profile it owns; an externally attached or real-profile Chrome is never
    touched. Delegates to the controller's ``quit_owned_chrome`` so there is a
    single owner for owned-Chrome teardown - the daemon inherits the same
    SIGTERM -> SIGKILL sequence and stale-lock cleanup, and never re-implements
    process signalling.

    Args:
        chrome_pid: PID of the tool-launched Chrome, if known.
        chrome_owned: Whether that Chrome is a private profile safe to quit.
        chrome_user_data_dir: The Chrome's profile directory, so its stale
            singleton lock is cleaned after it exits. None skips lock cleanup.

    Returns:
        None.
    """
    if not chrome_owned or chrome_pid is None:
        return
    try:
        from .persistent_browser import quit_owned_chrome
    except ImportError:
        from browser_tools.persistent_browser import quit_owned_chrome
    quit_owned_chrome(chrome_pid, chrome_user_data_dir, None)


def main(
    socket_path: str,
    pid_file: str,
    mcp_command: list[str],
    browser_url: str | None = None,
    mode: str = "full",
    stealth: bool = False,
    chrome_pid: int | None = None,
    chrome_owned: bool = False,
    chrome_user_data_dir: str | None = None,
    engine: str = DEFAULT_DISPATCH_ENGINE,
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
        chrome_pid: PID of the tool-launched Chrome to quit on idle/shutdown.
        chrome_owned: Whether that Chrome is a private profile safe to quit.
        chrome_user_data_dir: That Chrome's profile directory, for stale-lock cleanup.
        engine: Snapshot/UID backend: ``"native"`` (default) or ``"mcp"`` (Node).

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
        broker = McpBroker(mcp_command)
    except (FileNotFoundError, OSError):
        _cleanup_files(socket_path, pid_file)
        sys.exit(1)

    broker.start()

    # Initialize the MCP session
    init_resp = broker.request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "browser-tools-daemon", "version": "2.0.0"},
        },
        timeout=MCP_INIT_TIMEOUT_SECONDS,
    )
    if "error" in init_resp:
        broker.terminate()
        _cleanup_files(socket_path, pid_file)
        sys.exit(1)

    # Initialize CDP client and frame manager in a background thread
    cdp_handler = CDPHandler(browser_url, mode, stealth=stealth)
    cdp_thread = threading.Thread(target=cdp_handler.run, daemon=True)
    cdp_thread.start()

    last_activity = [time.time()]

    # Health monitor
    def health_check() -> None:
        """Periodically verify the MCP subprocess is alive and daemon is active."""
        while True:
            time.sleep(5)
            if not broker.is_alive():
                # Subprocess already exited; stop the CDP client, then tear the
                # daemon down. os._exit reaps the broker's child as this process
                # exits, so no separate wait is needed here.
                cdp_handler.stop()
                _cleanup_files(socket_path, pid_file)
                os._exit(1)
            if time.time() - last_activity[0] > IDLE_TIMEOUT_SECONDS:
                broker.terminate()
                cdp_handler.stop()
                _terminate_owned_chrome(chrome_pid, chrome_owned, chrome_user_data_dir)
                _cleanup_files(socket_path, pid_file)
                os._exit(0)

    health = threading.Thread(target=health_check, daemon=True)
    health.start()

    # Listen on Unix socket. Restrict access to the owner: any local user who
    # can connect to this socket can drive a possibly logged-in browser.
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    try:
        os.chmod(socket_path, 0o600)
    except OSError:
        logger.warning("Could not restrict daemon socket permissions on %s", socket_path)
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
        broker.terminate()
        cdp_handler.stop()
        _terminate_owned_chrome(chrome_pid, chrome_owned, chrome_user_data_dir)
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
            _handle_client(client_sock, broker, cdp_handler, last_activity, engine)
        except Exception:
            logger.exception("Client handler failed")
        finally:
            with contextlib.suppress(OSError):
                client_sock.close()


def _handle_client(
    client_sock: socket.socket,
    broker: McpBroker,
    cdp_handler: CDPHandler,
    last_activity: list[float],
    engine: str = DEFAULT_DISPATCH_ENGINE,
) -> None:
    """Handle one client connection with sequential JSON-RPC requests.

    Reads each request and delegates routing to :func:`dispatch_tool`; this
    loop owns only the socket read/write and the activity timestamp.

    Args:
        client_sock: Connected client socket.
        broker: MCP request multiplexer owning the subprocess.
        cdp_handler: CDP/frame tool handler.
        last_activity: Last activity timestamp.

    Returns:
        None.
    """
    client_sock.settimeout(REQUEST_TIMEOUT_SECONDS)
    buf = b""
    ctx = DispatchContext(broker, cdp_handler, engine)

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
            response = dispatch_tool(request, request.get("id"), ctx)
            try:
                client_sock.sendall(json.dumps(response).encode() + b"\n")
            except OSError:
                break


def _take_screenshot_with_paint_gate(
    request: dict[str, Any],
    client_id: Any,
    broker: McpBroker,
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

        last_response = broker.request(
            request["method"],
            request.get("params", {}),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        last_response["id"] = client_id

        # Errors from the subprocess shouldn't trigger a retry — they're
        # not blank-frame failures and would just compound the user's wait.
        if "error" in last_response:
            return last_response

        png_b64 = extract_screenshot_png_b64(last_response)
        if png_b64 is None:
            # No image content found (e.g. saved-to-file only response).
            # Nothing for us to inspect; trust the subprocess.
            return last_response

        if not screenshot_looks_blank(png_b64):
            return last_response

        # Looks blank. If we have retries left, brief sleep so any
        # animation/transition gets more time, then loop.
        if attempt < SCREENSHOT_BLANK_MAX_RETRIES:
            time.sleep(SCREENSHOT_BLANK_RETRY_DELAY_SECONDS)

    # Exhausted retries — return the last attempt with a diagnostic note
    # so the caller knows the daemon already tried to wait through this.
    append_text(
        last_response,
        "\n\n⚠️ Screenshot looked near-uniform after "
        f"{SCREENSHOT_BLANK_MAX_RETRIES + 1} attempts with paint-ready waits. "
        "The page may genuinely be blank, fully transparent, or still "
        "rendering — try wait_stable / wait_idle before retrying.",
    )
    return last_response


@dataclass
class DispatchContext:
    """Collaborators needed to route and execute a tool call.

    Built once per client connection; ``broker``, ``cdp_handler``, and ``engine``
    do not change between requests on the same connection.
    """

    broker: McpBroker
    cdp_handler: CDPHandler
    #: Snapshot/UID backend for this connection. ``"native"`` (default) routes
    #: take_snapshot/click/fill to the CDP-native path; ``"mcp"`` keeps the Node
    #: engine (RFC-01 Phase 2 escape hatch, removed in #47).
    engine: str = DEFAULT_DISPATCH_ENGINE


def dispatch_tool(request: dict[str, Any], client_id: Any, ctx: DispatchContext) -> dict[str, Any]:
    """Route one tool call to the right backend and apply cross-cutting policy.

    All routing is data-driven from ``tool_registry`` flags rather than an
    if-chain over tool names:

    - inspect mode refuses ``inspect_blocked`` tools;
    - ``cdp`` tools -> CDP handler, ``local`` tools -> the local handler;
    - ``screenshot_gate`` tools -> the paint-gate wrapper; everything else is
      forwarded to the MCP subprocess via the broker;
    - ``navigation`` tools trigger post-call interstitial detection.

    The caller's ``client_id`` is reattached onto every response (the broker
    uses its own internal id namespace).

    Args:
        request: Parsed JSON-RPC request from the client socket.
        client_id: Caller's request id, reattached onto the response.
        ctx: Broker + CDP handler collaborators.

    Returns:
        JSON-RPC response dict with ``id`` set to ``client_id``.
    """
    params = request.get("params", {})
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})

    # Inspect mode: refuse page-mutating tools.
    if ctx.cdp_handler.mode == "inspect" and tool_name in INSPECT_BLOCKED_TOOLS:
        return {
            "jsonrpc": "2.0",
            "result": make_error(
                f"E004: Tool '{tool_name}' is blocked in inspect mode. "
                "Observation tools only: take_snapshot, take_screenshot, "
                "list_pages, evaluate_script, list_console_messages, "
                "list_network_requests, list_frames, get_frame_storage."
            )["result"],
            "id": client_id,
        }

    # Native snapshot/UID backend is the default (RFC-01 Phase 2 flip): route
    # take_snapshot/click/fill to the CDP-native path. Under ``--engine mcp``
    # this branch is skipped and the tools fall through to the Node broker,
    # exactly as before the flip. The response shape is identical either way.
    if ctx.engine != "mcp" and tool_name in NATIVE_DISPATCH_TOOLS:
        response = ctx.cdp_handler.call_native(tool_name, arguments)
        response["id"] = client_id
    # Route by registry flags.
    elif tool_name in CDP_TOOLS:
        response = ctx.cdp_handler.call_tool(tool_name, arguments)
        response["id"] = client_id
    elif tool_name in SCREENSHOT_GATE_TOOLS:
        response = _take_screenshot_with_paint_gate(request, client_id, ctx.broker, ctx.cdp_handler)
    else:
        # Default: forward to the MCP subprocess via the broker.
        response = ctx.broker.request(request["method"], params, timeout=REQUEST_TIMEOUT_SECONDS)
        response["id"] = client_id

    # Post-navigation interstitial detection with auto-retry.
    if tool_name in NAVIGATION_TOOLS and "error" not in response:
        # A navigation invalidates the native snapshot's UIDs (native's
        # stability contract). No-op cost under the Node engine.
        if ctx.engine != "mcp":
            ctx.cdp_handler.mark_native_navigation()
        _append_interstitial_warning(response, ctx.cdp_handler)

    return response


def _append_interstitial_warning(response: dict[str, Any], cdp_handler: CDPHandler) -> None:
    """Run post-navigation interstitial detection and annotate the response.

    When a challenge page is detected after navigation, append a formatted
    warning. When a challenge was detected but auto-cleared by retry, note that
    instead. Mutates ``response`` in place.

    Args:
        response: JSON-RPC response to annotate.
        cdp_handler: CDP handler running interstitial detection.

    Returns:
        None.
    """
    detection_result = cdp_handler.run_post_navigation_detection()
    if detection_result and detection_result.get("detections"):
        detections = detection_result["detections"]
        auto_retried = detection_result.get("auto_retried", False)
        retries_used = detection_result.get("retries_used", 0)
        warning = format_interstitials(
            detections,
            auto_retried=auto_retried,
            retries_used=retries_used,
        )
        if warning:
            append_text(response, f"\n\n{warning}")
    elif detection_result and detection_result.get("auto_retried"):
        # Challenge was detected but auto-cleared.
        retries_used = detection_result.get("retries_used", 0)
        append_text(
            response,
            f"\n\n✅ Anti-bot challenge detected and auto-cleared after {retries_used} retry(ies).",
        )


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
    # Ensure parent dir is on sys.path for direct execution
    _parent = Path(__file__).resolve().parent.parent
    if str(_parent) not in sys.path:
        sys.path.insert(0, str(_parent))

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
    parser.add_argument(
        "--chrome-pid",
        type=int,
        default=None,
        help="PID of the tool-launched Chrome to quit on idle timeout / shutdown",
    )
    parser.add_argument(
        "--chrome-owned",
        action="store_true",
        help="Whether the Chrome at --chrome-pid is a private profile safe to quit",
    )
    parser.add_argument(
        "--chrome-user-data-dir",
        default=None,
        help="Profile directory of the owned Chrome, for stale-lock cleanup on quit",
    )
    parser.add_argument(
        "--engine",
        default=DEFAULT_DISPATCH_ENGINE,
        choices=["native", "mcp"],
        help="Snapshot/UID backend: 'native' (default, CDP-native) or 'mcp' (Node engine)",
    )
    args = parser.parse_args()

    try:
        command = json.loads(args.mcp_command)
    except json.JSONDecodeError:
        sys.exit(1)

    main(
        args.socket,
        args.pid_file,
        command,
        args.browser_url,
        args.mode,
        args.stealth,
        args.chrome_pid,
        args.chrome_owned,
        args.chrome_user_data_dir,
        args.engine,
    )
