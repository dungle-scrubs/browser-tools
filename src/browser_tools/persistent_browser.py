#!/usr/bin/env python3
"""Persistent Chrome browser management for browser-tools.

This module works around tool-proxy's one-script-per-tool-call execution model.
The Python wrapper still exits after each request, but it reuses a long-lived
Chrome instance over a remote debugging port and restores the selected page in a
fresh MCP session before invoking the requested tool.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import session_layout as layout
from .browser_state import (  # re-exported for consumers
    BrowserState,
    ProjectBrowserConfig,  # type: ignore[reportUnusedImport]  # noqa: F401  # re-exported for tests
)
from .chrome_config import get_mcp_command
from .chrome_utils import MCPInvocationError
from .daemon_client import DaemonClient
from .mcp_response import extract_text_items  # re-exported for consumers
from .mcp_session import ChromeMcpSession  # noqa: TC001  # re-exported + used in type annotation
from .process_utils import (
    build_browser_command,
    clean_stale_singleton_lock,
    enumerate_tabs,  # type: ignore[reportUnusedImport]  # noqa: F401  # re-exported for tests
    find_chrome_debug_port,
    find_chrome_user_data_dir,  # type: ignore[reportUnusedImport]  # re-exported for tests
    find_free_port,
    find_listeners_on_port,
    is_devtools_available,
    is_process_alive,
    read_process_command,  # type: ignore[reportUnusedImport]  # noqa: F401  # re-exported for tests
    read_process_start_time,
    read_singleton_lock_pid,
    resolve_chrome_executable,
    resolve_system_profile_dir,
    select_tab_by_url,  # type: ignore[reportUnusedImport]  # noqa: F401  # re-exported for tests
    terminate_process,
    terminate_process_and_wait,
    wait_for_devtools,
)
from .project_identity import (
    get_project_dir,  # type: ignore[reportUnusedImport]  # noqa: F401  # re-exported for tests
)
from .session_layout import (  # re-exported: session_layout owns the layout now
    CACHE_DIR,  # type: ignore[reportUnusedImport]  # noqa: F401
    INITIAL_PAGE_URL,  # type: ignore[reportUnusedImport]  # noqa: F401
    build_session_key,
    clear_session_files,  # type: ignore[reportUnusedImport]  # noqa: F401
)
from .session_reaper import (
    reap_orphaned_sessions,  # type: ignore[reportUnusedImport]  # noqa: F401  # re-exported for callers
)
from .tool_registry import INTERACTION_TOOLS

DEFAULT_BROWSER_TIMEOUT_SECONDS = 60
BROWSER_READY_TIMEOUT_SECONDS = 10.0
# Chrome launch attempts; each retry picks a fresh debug port so a lost race
# for the port (find_free_port releases it before Chrome binds) is recovered.
LAUNCH_PORT_ATTEMPTS = 3
SELECTED_PAGE_PATTERN = re.compile(r"^\s*(\d+):.*\[selected\]\s*$", re.MULTILINE)
PAGE_LINE_PATTERN = re.compile(r"^\s*(\d+):\s*(.*?)(?:\s*\[selected\])?\s*$", re.MULTILINE)


DAEMON_STARTUP_TIMEOUT_SECONDS = 30
DAEMON_RECOVERY_RETRY_COUNT = 1
# Grace period after SIGTERM before escalating to SIGKILL.
CHROME_QUIT_TIMEOUT_SECONDS = 5.0
DAEMON_SCRIPT = Path(__file__).parent / "mcp_daemon.py"
BROWSER_TOOLS_ROOT = DAEMON_SCRIPT.parents[2]


def is_owned_profile_dir(user_data_dir: str | Path | None) -> bool:
    """Report whether a user-data-dir is a browser-tools-owned automation profile.

    Only Chrome instances running one of browser-tools' private profiles (under
    ``CACHE_DIR/profiles``) may be force-quit on teardown. Instances driving the
    user's real everyday profile (mode='real') or an externally attached Chrome
    are never killed — they are only detached from.

    Args:
        user_data_dir: The Chrome instance's ``--user-data-dir`` value.

    Returns:
        True when the directory is a private automation profile that the tool
        may terminate, False otherwise.
    """
    if user_data_dir is None:
        return False
    try:
        return Path(user_data_dir).resolve().is_relative_to(layout.profiles_dir().resolve())
    except (OSError, ValueError):
        return False


def quit_owned_chrome(
    pid: int, user_data_dir: str | Path | None, started_at: str | None = None
) -> bool:
    """Quit a Chrome process that browser-tools owns, escalating if it hangs.

    Sends SIGTERM, waits, then SIGKILLs a process that has not exited, and
    finally clears the profile's stale singleton lock so the next launch on that
    ``user-data-dir`` is not refused. Callers are responsible for establishing
    ownership first — this function does not re-check it.

    When ``started_at`` is given, the target's identity is re-verified
    immediately before each signal. A PID alone is not an identity: the process
    can exit and the kernel can hand that PID to something unrelated, and the
    five-second wait before SIGKILL is more than long enough for that to happen.

    Args:
        pid: Chrome process ID to quit.
        user_data_dir: The instance's profile directory, used for lock cleanup.
        started_at: Recorded ``ps`` start time pinning the process identity.
            None skips the check, for callers with no recorded token.

    Returns:
        True when the process is gone afterwards, False when it was already gone
        before signalling, is no longer the process we recorded, or survived.
    """

    def is_still_target() -> bool:
        if not is_process_alive(pid):
            return False
        return started_at is None or read_process_start_time(pid) == started_at

    if not is_still_target():
        return False
    terminate_process(pid)  # SIGTERM
    if not _wait_for_process_exit(pid, CHROME_QUIT_TIMEOUT_SECONDS) and is_still_target():
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
        _wait_for_process_exit(pid, CHROME_QUIT_TIMEOUT_SECONDS)
    # A signal can fail silently (EPERM), so confirm rather than assume.
    if is_still_target():
        return False
    if user_data_dir:
        clean_stale_singleton_lock(Path(user_data_dir))
    return True


def close_active_session(controller: PersistentChromeController) -> dict[str, Any]:
    """Tear down the running browser session backing ``controller``.

    Stops the MCP daemon broker, then either quits the Chrome the tool launched
    (only when it is a private automation profile we own) or simply detaches
    from an external / real-profile Chrome, leaving it running. Transient
    session state and the active-attach record are cleared; explicit session
    overrides and project preferences are left intact.

    Args:
        controller: The controller whose session should be closed.

    Returns:
        A summary dict with keys ``quit_chrome``, ``quit_failed``, ``detached``,
        ``daemon_stopped``, ``pid``, and ``endpoint``.
    """
    state = BrowserState.from_path(controller.state_path)
    session_key = controller.session_key
    summary: dict[str, Any] = {
        "quit_chrome": False,
        "detached": False,
        "daemon_stopped": False,
        "pid": state.pid if state else None,
        "endpoint": (state.browser_url if state else None) or controller.browser_url,
    }

    # Stop the MCP daemon broker for this session and clean its runtime files.
    if state is not None and state.daemon_pid is not None and is_process_alive(state.daemon_pid):
        terminate_process(state.daemon_pid)
        summary["daemon_stopped"] = True
    Path(layout.socket_path(session_key)).unlink(missing_ok=True)
    layout.daemon_pid_file(session_key).unlink(missing_ok=True)
    layout.lock_path(session_key).unlink(missing_ok=True)

    # Only a private automation profile launched by the tool may be force-quit.
    attached_external = controller.browser_url is not None
    user_data_dir = state.user_data_dir if state else str(controller.user_data_dir)
    pid = state.pid if state else None
    owned = (not attached_external) and is_owned_profile_dir(user_data_dir)

    if owned and pid is not None:
        # Report what happened, not what was attempted: signalling can fail
        # (EPERM), and "quit" would then be a lie the caller acts on.
        started_at = state.chrome_started_at if state else None
        summary["quit_chrome"] = quit_owned_chrome(pid, user_data_dir, started_at)
        summary["quit_failed"] = not summary["quit_chrome"] and is_process_alive(pid)
    elif summary["endpoint"] is not None or pid is not None:
        summary["detached"] = True

    # Forget the running-session state; keep overrides / project preferences.
    controller.state_path.unlink(missing_ok=True)
    # Lazy import: session_store imports PersistentChromeController from here.
    from .session_store import clear_active_attach_config

    clear_active_attach_config()
    return summary


class PersistentChromeController:
    """Coordinates persistent browser reuse across wrapper invocations."""

    def __init__(
        self,
        *,
        headless: bool = False,
        isolated: bool = True,
        viewport: str | None = None,
        channel: str = "canary",
        browser_url: str | None = None,
        force_persistent: bool = False,
        profile: str | None = None,
        stealth: bool = False,
        system_profile: bool = False,
    ) -> None:
        """Configure the persistent browser controller.

        Args:
            headless: Whether an auto-managed browser should run headless.
            isolated: Whether to use a dedicated persistent user-data-dir.
            viewport: Initial window size formatted as WIDTHxHEIGHT.
            channel: Chrome channel to launch when auto-managing a browser.
            browser_url: Existing remote debugging endpoint to reuse.
            force_persistent: When True, always use the persistent browser path.
            profile: Named profile for persistent cookie/session storage.
            stealth: Whether to inject stealth patches to reduce automation
                fingerprinting (navigator.webdriver, plugins, WebGL, etc.).
            system_profile: When True, drive the user's real everyday Chrome
                profile (mode='real') instead of a private automation profile.
                Its Chrome is never force-quit on teardown — only detached.

        Returns:
            None.

        Raises:
            ValueError: If both profile and isolated=True are set (E005), or if
                system_profile is combined with profile/isolated (E006).
        """
        if profile and isolated:
            raise ValueError(
                "E005: Cannot use 'profile' with 'isolated=True'. "
                "Named profiles persist state; isolated mode discards it. "
                "Use profile alone for persistent sessions, or isolated alone "
                "for throwaway sessions."
            )
        if system_profile and (profile or isolated):
            raise ValueError(
                "E006: mode='real' drives your everyday Chrome profile and "
                "cannot be combined with a named 'profile' or 'isolated=True'."
            )

        self.headless = headless
        self.isolated = isolated
        self.viewport = viewport
        self.channel = channel
        self.browser_url = browser_url
        self.force_persistent = force_persistent
        self.profile = profile
        self.stealth = stealth
        self.system_profile = system_profile
        self.mode: str | None = None  # Set by attach_browser tool

        if system_profile:
            system_dir = resolve_system_profile_dir(channel)
            if system_dir is None:
                raise ValueError(
                    f"E006: mode='real' is not supported for channel '{channel}' on this platform."
                )
            self.session_key = f"real_{channel}"
            self.user_data_dir = system_dir
        elif profile:
            self.session_key = f"profile_{profile}"
            self.user_data_dir = layout.profile_dir(profile)
        else:
            self.session_key = build_session_key(
                browser_url=browser_url,
                isolated=isolated,
                channel=channel,
            )
            self.user_data_dir = layout.profile_dir(self.session_key)

        self.state_path = layout.state_path(self.session_key)

    def should_use_persistent_browser(self) -> bool:
        """Determine whether this invocation should reuse a persistent browser.

        Returns:
            True when persistent browser reuse is enabled for this invocation.
        """
        return (
            self.force_persistent
            or self.browser_url is not None
            or self.isolated
            or self.profile is not None
        )

    def invoke_tool(self, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Invoke one tool against a persistent browser-backed session.

        The MCP subprocess is kept alive as a daemon between calls so that
        CDP listeners (console, network, performance) accumulate data across
        the entire browsing session.

        Args:
            tool_name: browser-tools MCP tool name.
            params: Tool arguments.

        Returns:
            Raw JSON-RPC response from the MCP server.

        Raises:
            MCPInvocationError: If browser startup or tool invocation fails.
        """
        state = self.ensure_browser_state()
        normalized_params = normalize_tool_params(tool_name, params)

        for attempt in range(DAEMON_RECOVERY_RETRY_COUNT + 1):
            try:
                return self._invoke_tool_once(state, tool_name, normalized_params)
            except MCPInvocationError as exc:
                if attempt >= DAEMON_RECOVERY_RETRY_COUNT or not is_recoverable_daemon_error(exc):
                    raise
                self._invalidate_daemon(state)

        raise MCPInvocationError("Unreachable daemon recovery path")

    def _invoke_tool_once(
        self, state: BrowserState, tool_name: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a single tool call against the current daemon.

        Args:
            state: Active browser state.
            tool_name: browser-tools MCP tool name.
            params: Normalized tool arguments.

        Returns:
            Raw JSON-RPC response from the MCP server.
        """
        with self._connect_mcp(state) as client:
            if should_restore_selection(tool_name) and (
                state.selected_page_url or state.selected_page_id
            ):
                self._restore_selected_page(client, state)
            if needs_pre_snapshot(tool_name):
                client.call_tool("take_snapshot", {})
            response = client.call_tool(tool_name, params)

            # After interactions that may cause navigation, refresh page info
            if needs_pre_snapshot(tool_name) or tool_name in {"navigate_page"}:
                refresh = client.call_tool("list_pages", {})
                self._update_state_from_response(state, "list_pages", {}, refresh)

        self._update_state_from_response(state, tool_name, params, response)
        return response

    def _connect_mcp(self, state: BrowserState) -> DaemonClient:
        """Return a client connected to the MCP daemon, spawning it if needed.

        Args:
            state: Current browser state (mutated with daemon info on spawn).

        Returns:
            DaemonClient context manager ready to be entered with ``with``.

        Raises:
            MCPInvocationError: If the daemon cannot be started or reached.
        """
        command = get_mcp_command(browser_url=state.browser_url)
        self._ensure_daemon(state, command)
        assert state.daemon_socket is not None
        return DaemonClient(state.daemon_socket)

    def _ensure_daemon(self, state: BrowserState, mcp_command: list[str]) -> None:
        """Ensure the MCP daemon broker is running.

        Uses a file lock to prevent races when multiple wrapper processes try
        to spawn the daemon simultaneously.

        Args:
            state: Browser state (mutated with daemon_pid and daemon_socket).
            mcp_command: Command to spawn the MCP subprocess inside the daemon.

        Raises:
            MCPInvocationError: If the daemon cannot be started.
        """
        if self._is_daemon_alive(state):
            return

        lock_path = layout.lock_path(self.session_key)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                if self._is_daemon_alive(state):
                    return
                self._spawn_daemon(state, mcp_command)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _is_daemon_alive(self, state: BrowserState) -> bool:
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

    def _invalidate_daemon(self, state: BrowserState) -> None:
        """Forget any stale daemon metadata so the next call respawns cleanly.

        Args:
            state: Browser state to mutate.
        """
        if state.daemon_pid is not None and is_process_alive(state.daemon_pid):
            terminate_process(state.daemon_pid)
        state.daemon_pid = None
        state.daemon_socket = None
        state.save(self.state_path)
        Path(layout.socket_path(self.session_key)).unlink(missing_ok=True)
        layout.daemon_pid_file(self.session_key).unlink(missing_ok=True)

    def stop_daemon_only(self) -> bool:
        """Stop the MCP daemon for this session, leaving the browser running.

        Used when detaching from an externally attached browser (one
        browser-tools did not launch): the daemon is ours, the Chrome is the
        user's.

        Returns:
            True when a daemon was found and stopped.
        """
        state = BrowserState.from_path(self.state_path)
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
            state.save(self.state_path)
        Path(layout.socket_path(self.session_key)).unlink(missing_ok=True)
        layout.daemon_pid_file(self.session_key).unlink(missing_ok=True)
        return stopped

    def close_owned_browser(self) -> dict[str, Any]:
        """Quit a tool-launched Chrome and stop its daemon, keeping the profile.

        Used by ``close_browser`` (owned teardown) and by the headless-to-
        headed promotion. Cookies and login state survive on disk because the
        user-data-dir is left intact; only the running processes and their
        lock/state artifacts are cleared so the next call relaunches cleanly
        into the same profile (no re-auth).

        Returns:
            Dict with ``daemon_stopped`` and ``chrome_quit`` booleans.
        """
        state = BrowserState.from_path(self.state_path)
        result: dict[str, Any] = {"daemon_stopped": False, "chrome_quit": False}
        if state is not None:
            if state.daemon_pid is not None and is_process_alive(state.daemon_pid):
                terminate_process_and_wait(state.daemon_pid, timeout=5)
                result["daemon_stopped"] = True
            if state.pid is not None and is_process_alive(state.pid):
                terminate_process_and_wait(state.pid, timeout=5)
                result["chrome_quit"] = True
            if state.user_data_dir:
                clean_stale_singleton_lock(Path(state.user_data_dir))
        layout.clear_session_files(self.session_key)
        return result

    def _spawn_daemon(self, state: BrowserState, mcp_command: list[str]) -> None:
        """Spawn a new MCP daemon broker process.

        Args:
            state: Browser state (mutated with daemon_pid and daemon_socket).
            mcp_command: Command for the MCP subprocess inside the daemon.

        Raises:
            MCPInvocationError: If the daemon fails to start within the timeout.
        """
        if state.daemon_pid is not None and is_process_alive(state.daemon_pid):
            terminate_process(state.daemon_pid)

        socket_path = layout.socket_path(self.session_key)
        pid_file = str(layout.daemon_pid_file(self.session_key))

        Path(socket_path).unlink(missing_ok=True)
        Path(pid_file).unlink(missing_ok=True)

        daemon_cmd = build_daemon_command(socket_path, pid_file, mcp_command)
        if state.browser_url:
            daemon_cmd.extend(["--browser-url", state.browser_url])
        if self.mode:
            daemon_cmd.extend(["--mode", self.mode])
        if getattr(self, "stealth", False):
            daemon_cmd.append("--stealth")
        if state.pid is not None and is_owned_profile_dir(state.user_data_dir):
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
                    pass
            time.sleep(0.2)
        else:
            raise MCPInvocationError(
                "Timed out waiting for MCP daemon to start "
                f"(waited {DAEMON_STARTUP_TIMEOUT_SECONDS}s)"
            )

        try:
            daemon_pid = int(Path(pid_file).read_text().strip())
        except (OSError, ValueError):
            daemon_pid = None

        state.daemon_pid = daemon_pid
        state.daemon_socket = socket_path
        state.save(self.state_path)

    def find_live_state(self) -> BrowserState | None:
        """Return a reachable browser state for this controller, or None.

        Checks, in order and without launching anything: the saved state
        file (when it still describes a live browser), a live Chrome holding
        this controller's user-data-dir (discovered via the singleton lock),
        and an external attach endpoint. Used both as the fast path inside
        :meth:`ensure_browser_state` and by the session selector to decide
        whether this project already has a running browser to reuse.

        Returns:
            A usable BrowserState, or None when nothing live is reachable.
        """
        state = BrowserState.from_path(self.state_path)
        if state and self._is_state_usable(state):
            return state

        if self.browser_url:
            if is_devtools_available(self.browser_url):
                return self._make_state(browser_url=self.browser_url, pid=None, user_data_dir=None)
            return None

        return self._try_reuse_existing_chrome(self.user_data_dir)

    def ensure_browser_state(self) -> BrowserState:
        """Return a live browser state, relaunching if necessary.

        Holds this session's spawn lock for the whole decision. The orphan sweep
        takes the same lock, so without it a sweep could read a stale
        ``last_used_at``, decide the session is idle, and quit the browser in the
        window where this call is reviving it.

        Returns:
            BrowserState for a reachable browser instance.

        Raises:
            MCPInvocationError: If no browser can be launched or connected.
        """
        lock_path = layout.lock_path(self.session_key)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                return self._resolve_browser_state()
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _resolve_browser_state(self) -> BrowserState:
        """Reuse, attach to, or launch a browser for this session.

        Must be called while holding the session's spawn lock.

        Returns:
            BrowserState for a reachable browser instance.

        Raises:
            MCPInvocationError: If no browser can be launched or connected.
        """
        live = self.find_live_state()
        if live is not None:
            live.last_used_at = time.time()
            live.save(self.state_path)
            return live

        if self.browser_url:
            # External endpoint that is not reachable: nothing to launch.
            raise MCPInvocationError(
                f"Chrome remote debugging endpoint is unavailable: {self.browser_url}"
            )

        executable = resolve_chrome_executable(self.channel)
        if executable is None:
            raise MCPInvocationError(
                f"Could not find a Chrome executable for channel '{self.channel}'."
            )

        user_data_dir = self.user_data_dir
        user_data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        # If a Chrome process holds this profile but its debug port isn't
        # reachable, refuse to launch a second instance (Chrome's
        # single-instance check would fail) and surface what's wrong so the
        # caller can recover. Otherwise the agent loops on a silent failure.
        lock_pid = read_singleton_lock_pid(user_data_dir)
        if (
            lock_pid is not None
            and is_process_alive(lock_pid)
            and pid_holds_user_data_dir(lock_pid, user_data_dir)
        ):
            if self.system_profile:
                # mode='real': the user's everyday Chrome is open but was not
                # started with remote debugging, so we cannot attach and must
                # not force-quit their browser. Tell them how to recover.
                raise MCPInvocationError(
                    f"Your everyday {self.channel} Chrome (pid {lock_pid}) is running "
                    "without --remote-debugging-port, so mode='real' cannot drive it. "
                    "Quit that Chrome and retry (browser-tools will relaunch it with "
                    "debugging enabled), or restart it yourself with "
                    "--remote-debugging-port=<port>."
                )
            raise MCPInvocationError(format_dead_port_error(self.profile, user_data_dir, lock_pid))

        # Clean stale singleton lock files so Chrome can launch cleanly.
        clean_stale_singleton_lock(user_data_dir)

        browser_url, pid = self._launch_chrome(executable, user_data_dir)
        # We started this Chrome, so we may quit it later. mode='real' drives
        # the user's everyday browser and is never ours to force-quit.
        state = self._make_state(
            browser_url=browser_url,
            pid=pid,
            user_data_dir=str(user_data_dir),
            chrome_owned=not self.system_profile,
        )
        state.save(self.state_path)
        return state

    def _make_state(
        self,
        *,
        browser_url: str,
        pid: int | None,
        user_data_dir: str | None,
        chrome_owned: bool = False,
    ) -> BrowserState:
        """Build a BrowserState stamped with this controller's launch settings.

        Args:
            browser_url: Remote debugging endpoint for the browser.
            pid: PID of the browser process, or None for an external endpoint.
            user_data_dir: Profile directory string, or None for an external
                endpoint.
            chrome_owned: True only when this tool launched ``pid`` itself. Only
                an owned browser may later be force-quit.

        Returns:
            A freshly timestamped BrowserState.
        """
        return BrowserState(
            browser_url=browser_url,
            selected_page_id=None,
            pid=pid,
            user_data_dir=user_data_dir,
            headless=self.headless,
            isolated=self.isolated,
            channel=self.channel,
            viewport=self.viewport,
            last_used_at=time.time(),
            chrome_owned=chrome_owned,
            chrome_started_at=(
                read_process_start_time(pid) if chrome_owned and pid is not None else None
            ),
        )

    def _launch_chrome(self, executable: str, user_data_dir: Path) -> tuple[str, int]:
        """Launch Chrome, retrying on a lost race for the chosen debug port.

        ``find_free_port`` releases the port before Chrome binds it, so another
        process can steal it in between. Each attempt picks a fresh port, so a
        stolen port is recovered rather than surfaced as a spurious timeout.

        Args:
            executable: Chrome executable path.
            user_data_dir: Profile directory to launch into.

        Returns:
            The reachable ``browser_url`` and the launched process PID.

        Raises:
            MCPInvocationError: If Chrome cannot be launched or never exposes a
                reachable debug port within the retry budget.
        """
        last_error = ""
        for _ in range(LAUNCH_PORT_ATTEMPTS):
            port = find_free_port()
            browser_url = f"http://127.0.0.1:{port}"
            command = build_browser_command(
                executable=executable,
                port=port,
                user_data_dir=user_data_dir,
                headless=self.headless,
                viewport=self.viewport,
                system_profile=self.system_profile,
            )
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                raise MCPInvocationError(f"Failed to launch Chrome: {exc}") from exc

            if wait_for_devtools(browser_url, timeout_seconds=BROWSER_READY_TIMEOUT_SECONDS):
                return browser_url, process.pid

            last_error = (
                f"Timed out waiting for Chrome remote debugging endpoint at {browser_url}. "
                f"Chrome (pid {process.pid}, user-data-dir {user_data_dir}) failed to expose "
                "its debug port. Another Chrome may be holding the profile, or the executable "
                f"({executable}) may be unable to start."
            )
            # Fully retire this attempt before retrying: SIGTERM is async, and a
            # relaunch into the same user-data-dir while the old Chrome still
            # holds the SingletonLock would just hand off to the dying instance
            # (which never binds a port). Wait for it to die, then re-clean the
            # lock so the next attempt launches into a clean profile.
            terminate_process(process.pid)
            _wait_for_process_exit(process.pid, timeout_seconds=BROWSER_READY_TIMEOUT_SECONDS)
            clean_stale_singleton_lock(user_data_dir)

        raise MCPInvocationError(last_error)

    def _try_reuse_existing_chrome(self, user_data_dir: Path) -> BrowserState | None:
        """Reuse a Chrome already holding this user-data-dir, if one exists.

        Args:
            user_data_dir: Profile directory the controller wants to launch into.

        Returns:
            BrowserState pointing at the existing Chrome's debug port, or None
            when no usable live Chrome holds the directory.
        """
        lock_pid = read_singleton_lock_pid(user_data_dir)
        if lock_pid is None or not is_process_alive(lock_pid):
            return None
        # The SingletonLock PID may have been recycled by an unrelated process,
        # or point at a Chrome running a different profile. Only reuse it when
        # it actually holds this user-data-dir.
        if not pid_holds_user_data_dir(lock_pid, user_data_dir):
            return None
        port = find_chrome_debug_port(lock_pid)
        if port is None:
            return None
        browser_url = f"http://127.0.0.1:{port}"
        if not is_devtools_available(browser_url):
            return None
        # Carry ownership forward from the state we are replacing rather than
        # re-deriving it: this path runs when a previous invocation's state was
        # invalidated but its Chrome survived, and that Chrome is still ours
        # only if we launched it. A browser the user started on this directory
        # by hand stays unowned.
        previous = BrowserState.from_path(self.state_path)
        inherited = previous is not None and previous.chrome_owned and previous.pid == lock_pid
        state = self._make_state(
            browser_url=browser_url,
            pid=lock_pid,
            user_data_dir=str(user_data_dir),
            chrome_owned=inherited,
        )
        if inherited and previous is not None:
            state.chrome_started_at = previous.chrome_started_at
        return state

    def _is_state_usable(self, state: BrowserState) -> bool:
        """Check whether saved browser state still points to a live browser.

        Args:
            state: Saved browser state.

        Returns:
            True when the browser process and remote debugging endpoint are both live.
        """
        if state.browser_url != (self.browser_url or state.browser_url):
            return False
        # channel selects the on-disk profile dir, so a mismatch means a
        # different bucket. ``isolated`` no longer participates (one bucket
        # per project regardless of isolated/headed/headless), and headless /
        # viewport are presentation-only and must NOT force a relaunch (that
        # would abandon the logged-in profile).
        if state.channel != self.channel:
            return False
        # For auto-managed browsers, confirm the saved state still describes the
        # profile dir this controller resolves to, so recorded state can't
        # silently point at a different (logged-out) user-data-dir.
        if (
            self.browser_url is None
            and state.user_data_dir is not None
            and Path(state.user_data_dir).resolve() != self.user_data_dir.resolve()
        ):
            return False
        if state.pid is not None and not is_process_alive(state.pid):
            return False
        return is_devtools_available(state.browser_url)

    def _restore_selected_page(
        self, client: ChromeMcpSession | DaemonClient, state: BrowserState
    ) -> None:
        """Restore the last selected page in the MCP session.

        Page IDs are not stable across MCP session restarts, so we resolve
        the target page by URL first, then select it by its current ID.
        When using the daemon, the MCP session persists and this is a
        harmless no-op (selecting the already-selected page).

        Args:
            client: Active MCP session or daemon client with call_tool method.
            state: Persisted browser state.

        Returns:
            None.
        """
        if state.selected_page_url is None and state.selected_page_id is None:
            return

        # Resolve the correct page ID in this session by listing pages first
        if state.selected_page_url:
            list_response = client.call_tool("list_pages", {})
            resolved_id = resolve_page_id_by_url(list_response, state.selected_page_url)
            if resolved_id is None:
                state.selected_page_id = None
                state.selected_page_url = None
                state.last_used_at = time.time()
                state.save(self.state_path)
                return
            target_id = resolved_id
        else:
            target_id = state.selected_page_id

        response = client.call_tool("select_page", {"pageId": target_id})
        if "error" in response:
            state.selected_page_id = None
            state.selected_page_url = None
            state.last_used_at = time.time()
            state.save(self.state_path)
        else:
            state.selected_page_id = target_id

    def _update_state_from_response(
        self,
        state: BrowserState,
        tool_name: str,
        params: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        """Persist selected-page changes inferred from the tool response.

        Args:
            state: Browser state to mutate and save.
            tool_name: Tool that was invoked.
            params: Normalized tool arguments.
            response: Raw JSON-RPC response.

        Returns:
            None.
        """
        selected_page_id = extract_selected_page_id(response)
        selected_page_url = extract_selected_page_url(response)

        if selected_page_id is not None:
            state.selected_page_id = selected_page_id
            if selected_page_url is not None:
                state.selected_page_url = selected_page_url
        elif tool_name == "select_page":
            page_id = params.get("pageId")
            state.selected_page_id = page_id if isinstance(page_id, int) else state.selected_page_id
        elif tool_name == "close_page":
            closed_page_id = params.get("pageId")
            if state.selected_page_id == closed_page_id:
                state.selected_page_id = None
                state.selected_page_url = None

        state.last_used_at = time.time()
        state.save(self.state_path)


def normalize_tool_params(tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Normalize wrapper arguments to the MCP server's actual schema.

    Args:
        tool_name: Tool being invoked.
        params: Original wrapper arguments.

    Returns:
        Normalized argument dictionary.
    """
    normalized = dict(params)
    if tool_name in {"select_page", "close_page"} and "pageId" not in normalized:
        page_idx = normalized.pop("pageIdx", None)
        if isinstance(page_idx, int):
            normalized["pageId"] = page_idx
    return normalized


def needs_pre_snapshot(tool_name: str) -> bool:
    """Decide whether a snapshot should be taken before the tool runs.

    The upstream MCP server requires a snapshot in the same session before
    any interaction tool that references element UIDs.

    Args:
        tool_name: Tool being invoked.

    Returns:
        True when an automatic pre-snapshot is needed.
    """
    return tool_name in INTERACTION_TOOLS


def should_restore_selection(tool_name: str) -> bool:
    """Decide whether the previous page selection should be restored first.

    Args:
        tool_name: Tool being invoked.

    Returns:
        True when restoring selection is required for correct behavior.
    """
    return tool_name not in {"new_page", "select_page"}


def extract_selected_page_id(response: dict[str, Any]) -> int | None:
    """Parse the selected page id from a page-list style MCP response.

    Args:
        response: Raw JSON-RPC response containing tool output.

    Returns:
        Selected page id if present, otherwise None.
    """
    texts = extract_text_items(response)
    for text in texts:
        match = SELECTED_PAGE_PATTERN.search(text)
        if match:
            return int(match.group(1))
    return None


def extract_selected_page_url(response: dict[str, Any]) -> str | None:
    """Parse the selected page URL from a page-list style MCP response.

    Args:
        response: Raw JSON-RPC response containing tool output.

    Returns:
        URL of the selected page if present, otherwise None.
    """
    texts = extract_text_items(response)
    for text in texts:
        for match in PAGE_LINE_PATTERN.finditer(text):
            line = match.group(0)
            if "[selected]" in line:
                return match.group(2).strip()
    return None


def resolve_page_id_by_url(response: dict[str, Any], target_url: str) -> int | None:
    """Find the page ID for a given URL in a list_pages response.

    Args:
        response: Raw JSON-RPC response from list_pages.
        target_url: URL to search for.

    Returns:
        Page ID matching the URL, or None if not found.
    """
    texts = extract_text_items(response)
    for text in texts:
        for match in PAGE_LINE_PATTERN.finditer(text):
            page_id = int(match.group(1))
            page_url = match.group(2).strip()
            if page_url == target_url or page_url.rstrip("/") == target_url.rstrip("/"):
                return page_id
    return None


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
    python_command = [sys.executable]
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


def _wait_for_process_exit(pid: int, timeout_seconds: float) -> bool:
    """Poll until a process exits or a deadline passes.

    Args:
        pid: Process ID to watch.
        timeout_seconds: Maximum time to wait.

    Returns:
        True when the process is gone before the deadline, else False.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(0.1)
    return not is_process_alive(pid)


def pid_holds_user_data_dir(pid: int, user_data_dir: Path) -> bool:
    """Check whether a live PID is a Chrome holding the given profile dir.

    Guards against a recycled SingletonLock PID (now an unrelated process) or a
    Chrome running a different ``--user-data-dir``.

    Args:
        pid: Process ID recorded in the profile's SingletonLock.
        user_data_dir: Profile directory the caller expects that PID to hold.

    Returns:
        True only when ``pid``'s command line resolves to ``user_data_dir``.
    """
    actual = find_chrome_user_data_dir(pid)
    if actual is None:
        return False
    try:
        return actual.resolve() == user_data_dir.resolve()
    except OSError:
        return False


def format_dead_port_error(
    profile: str | None,
    user_data_dir: Path,
    lock_pid: int,
) -> str:
    """Build the user-facing error for a profile whose debug port is unusable.

    Args:
        profile: Named profile (None for ad-hoc launches).
        user_data_dir: Profile directory holding the singleton lock.
        lock_pid: PID of the Chrome process holding the directory.

    Returns:
        Multi-line error string describing the dead-port state and the
        concrete steps to recover.
    """
    intended = find_chrome_debug_port(lock_pid)
    target = f"profile '{profile}'" if profile else f"user-data-dir {user_data_dir}"
    lines = [
        f"E001: Chrome (pid {lock_pid}) is holding {target} but its remote",
        "debugging endpoint is unreachable. browser-tools refuses to launch a",
        "second Chrome on this profile because the singleton check would fail.",
    ]
    if intended is None:
        lines.append("")
        lines.append("  - The process command line has no --remote-debugging-port flag.")
    else:
        endpoint = f"http://127.0.0.1:{intended}"
        lines.append("")
        lines.append(f"  - Intended debug port from cmdline: {intended} (endpoint {endpoint})")
        collisions = [pid for pid in find_listeners_on_port(intended) if pid != lock_pid]
        if collisions:
            lines.append(
                f"  - That port is currently bound by another process: pid(s) "
                f"{', '.join(str(p) for p in collisions)}. Chrome failed to bind it."
            )
        else:
            lines.append(
                "  - No other process is listening on that port; Chrome may not "
                "have finished starting or DevTools is disabled."
            )
    lines.extend(
        [
            "",
            "Recovery options:",
            f"  1. kill {lock_pid} and call this tool again (it will relaunch on a free port).",
            "  2. Quit the conflicting Chrome window manually, then retry.",
            "  3. Restart the profile with an explicit free port and re-attach.",
        ]
    )
    return "\n".join(lines)


# is_process_alive / terminate_process now provided by process_utils.py
# (imported at top of file and re-exported for backward-compatible imports)
