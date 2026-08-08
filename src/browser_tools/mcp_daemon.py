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
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    from .cdp_constants import (
        CDP_TOOLS,
        INSPECT_BLOCKED_TOOLS,
        INSPECT_WARN_TOOLS,  # type: ignore[import-untyped]
        INTERSTITIAL_AUTO_RETRY_TYPES,  # type: ignore[import-untyped]
        INTERSTITIAL_MAX_RETRIES,  # type: ignore[import-untyped]
        INTERSTITIAL_RETRY_DELAY_SECONDS,  # type: ignore[import-untyped]
        LOCAL_TOOLS,
        NAVIGATION_TOOLS,
        REQUEST_TIMEOUT_SECONDS,
        SCREENSHOT_BLANK_MAX_RETRIES,
        SCREENSHOT_BLANK_RETRY_DELAY_SECONDS,
    )
    from .cdp_handler import CDPHandler
    from .mcp_response import append_text, make_error, make_text
    from .screenshot_utils import (
        extract_screenshot_png_b64,
        screenshot_looks_blank,
    )
except ImportError:
    from cdp_constants import (  # type: ignore[import-untyped,no-redef]
        CDP_TOOLS,
        INSPECT_BLOCKED_TOOLS,
        INSPECT_WARN_TOOLS,  # type: ignore[import-untyped]  # noqa: F401
        INTERSTITIAL_AUTO_RETRY_TYPES,  # type: ignore[import-untyped]  # noqa: F401
        INTERSTITIAL_MAX_RETRIES,  # type: ignore[import-untyped]  # noqa: F401
        INTERSTITIAL_RETRY_DELAY_SECONDS,  # type: ignore[import-untyped]  # noqa: F401
        LOCAL_TOOLS,
        NAVIGATION_TOOLS,
        REQUEST_TIMEOUT_SECONDS,
        SCREENSHOT_BLANK_MAX_RETRIES,
        SCREENSHOT_BLANK_RETRY_DELAY_SECONDS,
    )
    from cdp_handler import (  # type: ignore[import-untyped,no-redef]
        CDPHandler,
    )
    from mcp_response import (  # type: ignore[import-untyped,no-redef]
        append_text,
        make_error,
        make_text,
    )
    from screenshot_utils import (  # type: ignore[import-untyped,no-redef]
        extract_screenshot_png_b64,
        screenshot_looks_blank,
    )

IDLE_TIMEOUT_SECONDS = 30 * 60  # 30 minutes
MCP_INIT_TIMEOUT_SECONDS = 60


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
                # Subprocess already exited; reap it to avoid a zombie, stop the
                # CDP client, then tear the daemon down.
                with contextlib.suppress(Exception):
                    proc.wait(timeout=5)
                cdp_handler.stop()
                _cleanup_files(socket_path, pid_file)
                os._exit(1)
            if time.time() - last_activity[0] > IDLE_TIMEOUT_SECONDS:
                _reap_process(proc)
                cdp_handler.stop()
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
        _reap_process(proc)
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
            logger.exception("Client handler failed")
        finally:
            with contextlib.suppress(OSError):
                client_sock.close()


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
            if cdp_handler.mode == "inspect" and tool_name in INSPECT_BLOCKED_TOOLS:
                response = {
                    "jsonrpc": "2.0",
                    "result": make_error(
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
                        append_text(response, f"\n\n{warning}")
                elif detection_result and detection_result.get("auto_retried"):
                    # Challenge was detected but auto-cleared
                    retries_used = detection_result.get("retries_used", 0)
                    append_text(
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
            return make_text("No named profiles found.")
        lines = ["Named profiles:"]
        for p in profiles:
            lines.append(f"  {p}")
        return make_text("\n".join(lines))

    elif name == "delete_profile":
        from .persistent_browser import delete_profile

        profile_name = arguments.get("name", "")
        if not profile_name:
            return make_error("Profile name is required")
        if delete_profile(profile_name):
            return make_text(f"Profile '{profile_name}' deleted.")
        return make_error(f"Profile '{profile_name}' not found.")

    elif name == "attach_browser":
        # attach_browser is handled at the session level, not daemon level
        return make_text("attach_browser handled by session wrapper")

    return make_error(f"Unknown local tool: {name}")


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


def _reap_process(proc: subprocess.Popen[str]) -> None:
    """Terminate the MCP subprocess and wait for it so no zombie is left.

    Args:
        proc: The MCP subprocess to stop.

    Returns:
        None.
    """
    with contextlib.suppress(Exception):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(Exception):
            proc.kill()
            proc.wait(timeout=5)
    except Exception:
        pass


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
    args = parser.parse_args()

    try:
        command = json.loads(args.mcp_command)
    except json.JSONDecodeError:
        sys.exit(1)

    main(args.socket, args.pid_file, command, args.browser_url, args.mode, args.stealth)
