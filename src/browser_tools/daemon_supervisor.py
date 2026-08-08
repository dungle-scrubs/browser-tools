"""MCP daemon supervisor: owns the long-lived daemon broker process.

The persistent controller keeps a Chrome alive across wrapper invocations and
drives it through a chrome-devtools-mcp *subprocess*. That subprocess is itself
kept alive as a detached daemon (see ``mcp_daemon``) so CDP listeners accumulate
across the whole session. This module supervises that daemon from the controller
side: spawn it on demand, health-check it, hand out a connected client, and
recover (invalidate + respawn) when its transport dies.

Scope: this module owns the daemon *process* and its Unix-socket client. It does
not own the Chrome process, the on-disk session layout, or tool-call page-state
orchestration - those live in ``persistent_browser``, ``session_layout``, and the
controller respectively. ``chrome_owned`` is passed in (not computed here) so the
supervisor never has to know what a profile directory looks like.
"""

from __future__ import annotations

import fcntl
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .browser_state import BrowserState

from . import session_layout as layout
from .chrome_config import get_mcp_command
from .chrome_utils import MCPInvocationError
from .daemon_client import DaemonClient
from .process_utils import is_process_alive, terminate_process, terminate_process_and_wait

# How long to wait for a freshly spawned daemon to start listening on its socket.
DAEMON_STARTUP_TIMEOUT_SECONDS = 30

# Resolves to the installed package root, used as the daemon subprocess cwd so a
# wrapper running from another venv still finds browser-tools' own deps (CDP
# frame tools need ``websockets``).
BROWSER_TOOLS_ROOT = Path(__file__).resolve().parent.parent.parent


class McpDaemonSupervisor:
    """Spawn, health-check, and connect to the per-session MCP daemon.

    A supervisor is keyed by ``session_key``; all socket, pid, and lock paths
    derive from it. ``BrowserState`` is passed into each call (read and mutated
    in place) rather than held as state, so a Chrome restart that produces a new
    state object works without re-binding the supervisor.
    """

    def __init__(self, session_key: str) -> None:
        """Bind a supervisor to a session key.

        Args:
            session_key: Session key naming this daemon's socket/pid/lock files.
        """
        self.session_key = session_key

    def client(
        self,
        state: BrowserState,
        *,
        chrome_owned: bool = False,
        mode: str | None = None,
        stealth: bool = False,
    ) -> DaemonClient:
        """Return a client connected to the daemon, spawning it if needed.

        Args:
            state: Current browser state (mutated with daemon info on spawn).
            chrome_owned: Whether the daemon may quit this Chrome on idle.
            mode: Access mode forwarded to the daemon.
            stealth: Whether stealth patches are forwarded to the daemon.

        Returns:
            A ``DaemonClient`` context manager ready to be entered with ``with``.

        Raises:
            MCPInvocationError: If the daemon cannot be started or reached.
        """
        command = get_mcp_command(browser_url=state.browser_url)
        self.ensure(state, command, chrome_owned=chrome_owned, mode=mode, stealth=stealth)
        assert state.daemon_socket is not None
        return DaemonClient(state.daemon_socket)

    def ensure(
        self,
        state: BrowserState,
        mcp_command: list[str],
        *,
        chrome_owned: bool = False,
        mode: str | None = None,
        stealth: bool = False,
    ) -> None:
        """Ensure the MCP daemon broker is running.

        Uses a file lock to prevent races when multiple wrapper processes try to
        spawn the daemon simultaneously.

        Args:
            state: Browser state (mutated with daemon_pid and daemon_socket).
            mcp_command: Command to spawn the MCP subprocess inside the daemon.
            chrome_owned: Whether the daemon may quit this Chrome on idle.
            mode: Access mode forwarded to the daemon.
            stealth: Whether stealth patches are forwarded to the daemon.

        Raises:
            MCPInvocationError: If the daemon cannot be started.
        """
        # Fast path: daemon already running and reachable.
        if self.is_alive(state):
            return

        # Serialize daemon spawning across concurrent wrapper calls.
        lock_path = layout.lock_path(self.session_key)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                # Re-check after acquiring the lock: another process may have
                # just spawned it.
                if self.is_alive(state):
                    return
                self._spawn(
                    state,
                    mcp_command,
                    chrome_owned=chrome_owned,
                    mode=mode,
                    stealth=stealth,
                )
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def is_alive(self, state: BrowserState) -> bool:
        """Check whether the daemon is running and its socket is reachable.

        Args:
            state: Browser state with daemon_pid and daemon_socket.

        Returns:
            True when the daemon is alive and accepting connections.
        """
        if state.daemon_pid is None or state.daemon_socket is None:
            return False
        if not is_process_alive(state.daemon_pid):
            return False
        if not Path(state.daemon_socket).exists():
            return False
        pid_file = layout.daemon_pid_file(self.session_key)
        try:
            pid_file_pid = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            pid_file_pid = None
        if pid_file_pid != state.daemon_pid:
            return False
        try:
            test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            test_sock.settimeout(2)
            test_sock.connect(state.daemon_socket)
            test_sock.close()
            return True
        except OSError:
            return False

    def invalidate(self, state: BrowserState) -> None:
        """Forget any stale daemon metadata so the next call respawns cleanly.

        Args:
            state: Browser state to mutate.
        """
        if state.daemon_pid is not None and is_process_alive(state.daemon_pid):
            terminate_process(state.daemon_pid)
        state.daemon_pid = None
        state.daemon_socket = None
        state.save(layout.state_path(self.session_key))
        Path(layout.socket_path(self.session_key)).unlink(missing_ok=True)
        layout.daemon_pid_file(self.session_key).unlink(missing_ok=True)

    def stop_only(self, state: BrowserState | None) -> bool:
        """Stop the MCP daemon for this session, leaving the browser running.

        Used when detaching from an externally attached browser (one browser-tools
        did not launch): the daemon is ours, the Chrome is the user's.

        Args:
            state: Current session state, or None when no state file exists.

        Returns:
            True when a daemon was found and stopped.
        """
        stopped = False
        if (
            state is not None
            and state.daemon_pid is not None
            and is_process_alive(state.daemon_pid)
        ):
            terminate_process_and_wait(state.daemon_pid, timeout=5)
            stopped = True
            state.daemon_pid = None
            state.daemon_socket = None
            state.save(layout.state_path(self.session_key))
        Path(layout.socket_path(self.session_key)).unlink(missing_ok=True)
        layout.daemon_pid_file(self.session_key).unlink(missing_ok=True)
        return stopped

    def _spawn(
        self,
        state: BrowserState,
        mcp_command: list[str],
        *,
        chrome_owned: bool,
        mode: str | None,
        stealth: bool,
    ) -> None:
        """Spawn a new MCP daemon broker process.

        Args:
            state: Browser state (mutated with daemon_pid and daemon_socket).
            mcp_command: Command for the MCP subprocess inside the daemon.
            chrome_owned: Whether the daemon may quit this Chrome on idle.
            mode: Access mode forwarded to the daemon.
            stealth: Whether stealth patches are forwarded to the daemon.

        Raises:
            MCPInvocationError: If the daemon fails to start within the timeout.
        """
        # Kill an old daemon if it is stuck.
        if state.daemon_pid is not None and is_process_alive(state.daemon_pid):
            terminate_process(state.daemon_pid)

        socket_path = layout.socket_path(self.session_key)
        pid_file = str(layout.daemon_pid_file(self.session_key))

        # Clean stale files.
        Path(socket_path).unlink(missing_ok=True)
        Path(pid_file).unlink(missing_ok=True)

        daemon_cmd = build_daemon_command(socket_path, pid_file, mcp_command)
        # Pass browser URL to daemon for CDP client.
        if state.browser_url:
            daemon_cmd.extend(["--browser-url", state.browser_url])
        # Pass access mode to daemon.
        if mode:
            daemon_cmd.extend(["--mode", mode])
        # Pass stealth mode to daemon.
        if stealth:
            daemon_cmd.append("--stealth")
        # Pass the tool-launched Chrome to the daemon so it can quit it on idle
        # timeout or shutdown - but only when it is a private automation profile
        # we own. External / real-profile Chrome is left running.
        if chrome_owned and state.pid is not None:
            daemon_cmd.extend(
                [
                    "--chrome-pid",
                    str(state.pid),
                    "--chrome-owned",
                    "--chrome-user-data-dir",
                    str(state.user_data_dir),
                ]
            )

        try:
            subprocess.Popen(
                daemon_cmd,
                cwd=BROWSER_TOOLS_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise MCPInvocationError(f"Failed to start MCP daemon: {exc}") from exc

        # Wait for the daemon socket to become connectable.
        deadline = time.time() + DAEMON_STARTUP_TIMEOUT_SECONDS
        while time.time() < deadline:
            if Path(socket_path).exists():
                try:
                    test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    test_sock.settimeout(2)
                    test_sock.connect(socket_path)
                    test_sock.close()
                    break
                except OSError:
                    # Daemon hasn't started listening yet - keep polling.
                    pass
            time.sleep(0.2)
        else:
            raise MCPInvocationError(
                "Timed out waiting for MCP daemon to start "
                f"(waited {DAEMON_STARTUP_TIMEOUT_SECONDS}s)"
            )

        # Read daemon PID.
        try:
            daemon_pid = int(Path(pid_file).read_text().strip())
        except (OSError, ValueError):
            daemon_pid = None

        state.daemon_pid = daemon_pid
        state.daemon_socket = socket_path
        state.save(layout.state_path(self.session_key))


def is_recoverable_daemon_error(exc: MCPInvocationError) -> bool:
    """Return whether a daemon transport failure is safe to retry once.

    Args:
        exc: Raised MCP invocation error.

    Returns:
        True when the daemon transport died before a tool result was returned.
    """
    message = str(exc)
    return any(
        fragment in message
        for fragment in (
            "Failed to connect to MCP daemon",
            "MCP daemon connection error",
            "MCP daemon connection closed unexpectedly",
            "Failed to send request to MCP daemon",
        )
    )


def build_daemon_command(
    socket_path: str,
    pid_file: str,
    mcp_command: list[str],
) -> list[str]:
    """Build the command used to spawn the detached MCP daemon.

    The wrapper may run from tool-proxy's Python environment while importing
    browser-tools from source. The daemon needs browser-tools' own dependency
    set, because CDP frame tools require ``websockets``.
    """
    python_command: list[str] = [sys.executable]
    if shutil.which("uv") and (BROWSER_TOOLS_ROOT / "pyproject.toml").exists():
        python_command = ["uv", "run", "python"]

    return [
        *python_command,
        "-m",
        "browser_tools.mcp_daemon",
        "--socket",
        socket_path,
        "--pid-file",
        pid_file,
        "--mcp-command",
        json.dumps(mcp_command),
    ]


# ``contextlib`` is re-exported for callers (e.g. browser_session) that suppress
# BrowserToolsError around daemon-driven navigations.
__all__ = [
    "BROWSER_TOOLS_ROOT",
    "DAEMON_STARTUP_TIMEOUT_SECONDS",
    "McpDaemonSupervisor",
    "build_daemon_command",
    "is_recoverable_daemon_error",
]
