#!/usr/bin/env python3
"""Persistent Chrome browser management for browser-tools.

This module works around tool-proxy's one-script-per-tool-call execution model.
The Python wrapper still exits after each request, but it reuses a long-lived
Chrome instance over a remote debugging port and restores the selected page in a
fresh MCP session before invoking the requested tool.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .browser_state import (  # re-exported for consumers
    HEADLESS_AUTH_MODES,
    ActiveAttachConfig,
    BrowserState,
    ProjectBrowserConfig,
    normalize_mode,
)
from .chrome_config import get_mcp_command
from .chrome_utils import MCPInvocationError
from .daemon_client import DaemonClient
from .mcp_response import extract_text_items  # re-exported for consumers
from .mcp_session import ChromeMcpSession  # noqa: TC001  # re-exported + used in type annotation
from .process_utils import (
    build_browser_command,
    clean_stale_singleton_lock,
    enumerate_tabs,
    find_chrome_debug_port,
    find_chrome_user_data_dir,  # type: ignore[reportUnusedImport]  # re-exported for tests
    find_free_port,
    find_listeners_on_port,
    is_devtools_available,
    is_process_alive,
    read_process_command,  # type: ignore[reportUnusedImport]  # noqa: F401  # re-exported for tests
    read_singleton_lock_pid,
    resolve_chrome_executable,
    select_tab_by_url,  # type: ignore[reportUnusedImport]  # noqa: F401  # re-exported for tests
    terminate_process,
    terminate_process_and_wait,
    wait_for_devtools,
)
from .project_identity import (
    get_project_dir,
    get_project_id,
    resolve_project_root,
)
from .tool_registry import INTERACTION_TOOLS

CACHE_DIR = Path.home() / ".cache" / "tool-proxy" / "browser-tools"
DEFAULT_BROWSER_TIMEOUT_SECONDS = 60
BROWSER_READY_TIMEOUT_SECONDS = 10.0
# Chrome launch attempts; each retry picks a fresh debug port so a lost race
# for the port (find_free_port releases it before Chrome binds) is recovered.
LAUNCH_PORT_ATTEMPTS = 3
INITIAL_PAGE_URL = "about:blank"
SELECTED_PAGE_PATTERN = re.compile(r"^\s*(\d+):.*\[selected\]\s*$", re.MULTILINE)
PAGE_LINE_PATTERN = re.compile(r"^\s*(\d+):\s*(.*?)(?:\s*\[selected\])?\s*$", re.MULTILINE)


DAEMON_STARTUP_TIMEOUT_SECONDS = 30
DAEMON_RECOVERY_RETRY_COUNT = 1
ACTIVE_ATTACH_TTL_SECONDS = 12 * 60 * 60
DAEMON_SCRIPT = Path(__file__).parent / "mcp_daemon.py"
BROWSER_TOOLS_ROOT = DAEMON_SCRIPT.parents[2]
PROJECT_CONFIG_FILENAMES = (
    ".browser-tools.json",
    str(Path(".tool-proxy") / "browser-tools.json"),
)


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

        Returns:
            None.

        Raises:
            ValueError: If both profile and isolated=True are set (E005).
        """
        if profile and isolated:
            raise ValueError(
                "E005: Cannot use 'profile' with 'isolated=True'. "
                "Named profiles persist state; isolated mode discards it. "
                "Use profile alone for persistent sessions, or isolated alone "
                "for throwaway sessions."
            )

        self.headless = headless
        self.isolated = isolated
        self.viewport = viewport
        self.channel = channel
        self.browser_url = browser_url
        self.force_persistent = force_persistent
        self.profile = profile
        self.stealth = stealth
        self.mode: str | None = None  # Set by attach_browser tool

        if profile:
            self.session_key = f"profile_{profile}"
            self.user_data_dir = CACHE_DIR / "profiles" / profile
        else:
            self.session_key = build_session_key(
                browser_url=browser_url,
                isolated=isolated,
                channel=channel,
            )
            self.user_data_dir = CACHE_DIR / "profiles" / self.session_key

        self.state_path = CACHE_DIR / f"{self.session_key}.json"

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

        Uses a file lock to prevent race conditions when multiple wrapper
        processes try to spawn the daemon simultaneously.

        Args:
            state: Browser state (mutated with daemon_pid and daemon_socket).
            mcp_command: Command to spawn the MCP subprocess inside the daemon.

        Returns:
            None.

        Raises:
            MCPInvocationError: If the daemon cannot be started.
        """
        # Fast path: daemon already running and reachable
        if self._is_daemon_alive(state):
            return

        # Serialize daemon spawning across concurrent wrapper calls
        lock_path = CACHE_DIR / f"{self.session_key}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                # Re-check after acquiring lock (another process may have spawned)
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
        pid_file = CACHE_DIR / f"{self.session_key}.daemon.pid"
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

        Returns:
            None.
        """
        if state.daemon_pid is not None and is_process_alive(state.daemon_pid):
            terminate_process(state.daemon_pid)
        state.daemon_pid = None
        state.daemon_socket = None
        state.save(self.state_path)
        for suffix in (".sock", ".daemon.pid"):
            (CACHE_DIR / f"{self.session_key}{suffix}").unlink(missing_ok=True)

    def stop_daemon_only(self) -> bool:
        """Stop the MCP daemon for this session, leaving the browser running.

        Used by ``close_browser`` when detaching from an externally attached
        browser (one browser-tools did not launch): the daemon is ours, the
        Chrome is the user's.

        Returns:
            True when a daemon was found and stopped.
        """
        state = BrowserState.from_path(self.state_path)
        stopped = False
        if state is not None and state.daemon_pid is not None and is_process_alive(state.daemon_pid):
            terminate_process_and_wait(state.daemon_pid, timeout=5)
            stopped = True
            state.daemon_pid = None
            state.daemon_socket = None
            state.save(self.state_path)
        for suffix in (".sock", ".daemon.pid"):
            (CACHE_DIR / f"{self.session_key}{suffix}").unlink(missing_ok=True)
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
        self.state_path.unlink(missing_ok=True)
        for suffix in (".sock", ".daemon.pid", ".lock"):
            (CACHE_DIR / f"{self.session_key}{suffix}").unlink(missing_ok=True)
        return result

    def _spawn_daemon(self, state: BrowserState, mcp_command: list[str]) -> None:
        """Spawn a new MCP daemon broker process.

        Args:
            state: Browser state (mutated with daemon_pid and daemon_socket).
            mcp_command: Command for the MCP subprocess inside the daemon.

        Returns:
            None.

        Raises:
            MCPInvocationError: If the daemon fails to start within the timeout.
        """
        # Kill old daemon if stuck
        if state.daemon_pid is not None and is_process_alive(state.daemon_pid):
            terminate_process(state.daemon_pid)

        socket_path = str(CACHE_DIR / f"{self.session_key}.sock")
        pid_file = str(CACHE_DIR / f"{self.session_key}.daemon.pid")

        # Clean stale files
        Path(socket_path).unlink(missing_ok=True)
        Path(pid_file).unlink(missing_ok=True)

        daemon_cmd = build_daemon_command(socket_path, pid_file, mcp_command)
        # Pass browser URL to daemon for CDP client
        if state.browser_url:
            daemon_cmd.extend(["--browser-url", state.browser_url])
        # Pass access mode to daemon
        if self.mode:
            daemon_cmd.extend(["--mode", self.mode])
        # Pass stealth mode to daemon
        if getattr(self, "stealth", False):
            daemon_cmd.append("--stealth")

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

        # Wait for the daemon socket to become connectable
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
                    # Daemon hasn't started listening yet — keep polling
                    pass
            time.sleep(0.2)
        else:
            raise MCPInvocationError(
                "Timed out waiting for MCP daemon to start "
                f"(waited {DAEMON_STARTUP_TIMEOUT_SECONDS}s)"
            )

        # Read daemon PID
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
                return self._make_state(
                    browser_url=self.browser_url, pid=None, user_data_dir=None
                )
            return None

        return self._try_reuse_existing_chrome(self.user_data_dir)

    def ensure_browser_state(self) -> BrowserState:
        """Return a live browser state, relaunching if necessary.

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
            and _pid_holds_user_data_dir(lock_pid, user_data_dir)
        ):
            raise MCPInvocationError(format_dead_port_error(self.profile, user_data_dir, lock_pid))

        # Clean stale singleton lock files so Chrome can launch cleanly.
        clean_stale_singleton_lock(user_data_dir)

        browser_url, pid = self._launch_chrome(executable, user_data_dir)
        state = self._make_state(browser_url=browser_url, pid=pid, user_data_dir=str(user_data_dir))
        state.save(self.state_path)
        return state

    def _make_state(
        self, *, browser_url: str, pid: int | None, user_data_dir: str | None
    ) -> BrowserState:
        """Build a BrowserState stamped with this controller's launch settings.

        Args:
            browser_url: Remote debugging endpoint for the browser.
            pid: PID of the browser process, or None for an external endpoint.
            user_data_dir: Profile directory string, or None for an external
                endpoint.

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
        if not _pid_holds_user_data_dir(lock_pid, user_data_dir):
            return None
        port = find_chrome_debug_port(lock_pid)
        if port is None:
            return None
        browser_url = f"http://127.0.0.1:{port}"
        if not is_devtools_available(browser_url):
            return None
        return self._make_state(
            browser_url=browser_url, pid=lock_pid, user_data_dir=str(user_data_dir)
        )

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


def get_project_cwd() -> Path:
    """Return the project working directory used for browser preferences.

    Reads the harness-provided project directory (new canonical
    ``TOOL_PROXY_PROJECT_DIR`` name first, legacy ``CLAUDE_CWD`` as fallback)
    so browser-tools is not coupled to one harness's env var name.

    Returns:
        Absolute project working directory path.
    """
    return get_project_dir()


def _project_key_suffix() -> str:
    """Return the short hash identifying the current project's config files.

    Keyed on the resolved project root (git root, else cwd) rather than the
    raw working directory so config/state files stay stable as the agent
    moves between subdirectories of one repository.

    Returns:
        16-char hex suffix derived from CLAUDE_PROJECT_ID and the project root.
    """
    raw_key = f"{get_project_id()}|{resolve_project_root()}"
    return hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:16]


def get_active_attach_config_path() -> Path:
    """Return the per-project file used to remember the active Chrome attach.

    Returns:
        Path to the active attach config JSON file.
    """
    return CACHE_DIR / f"active_attach_{_project_key_suffix()}.json"


def find_project_browser_config_path() -> Path | None:
    """Find the nearest browser-tools preference file for the current project.

    Returns:
        Matching config path, or None when no config exists.
    """
    cwd = get_project_cwd()
    for directory in (cwd, *cwd.parents):
        for filename in PROJECT_CONFIG_FILENAMES:
            candidate = directory / filename
            if candidate.exists():
                return candidate
    return None


def load_project_browser_config() -> ProjectBrowserConfig | None:
    """Load the current project's preferred browser-tools session config.

    Returns:
        Project browser config, or None when no usable config exists.
    """
    path = find_project_browser_config_path()
    if path is None:
        return None
    return ProjectBrowserConfig.from_path(path)


def create_controller_from_browser_config(
    config: ProjectBrowserConfig,
    *,
    source: str,
) -> PersistentChromeController:
    """Create a persistent controller from a browser session config.

    Args:
        config: Browser session configuration.
        source: Source label for diagnostics.

    Returns:
        Configured browser controller.
    """
    del source  # Reserved for future response diagnostics.
    mode = normalize_mode(config.mode)
    browser_url = config.endpoint or config.browser_url

    # Per-mode defaults for presentation (headless) and persistence (isolated),
    # each overridable by an explicit config field. Only the plain "headless"
    # mode is isolated; every auth mode keeps a persistent profile.
    if mode == "headless":
        default_headless, default_isolated = True, True
    elif mode in HEADLESS_AUTH_MODES:
        default_headless, default_isolated = True, False
    else:  # headed / headed-auth / auth / auth-headed / unknown
        default_headless, default_isolated = False, False

    headless = config.headless if config.headless is not None else default_headless
    isolated = config.isolated if config.isolated is not None else default_isolated

    # A named profile always persists login state, so it can never be isolated
    # (the PersistentChromeController constructor rejects that combination, E005).
    if config.profile:
        isolated = False

    controller = PersistentChromeController(
        headless=headless,
        isolated=isolated,
        viewport=config.viewport,
        channel=config.channel,
        browser_url=browser_url,
        profile=config.profile,
        stealth=config.stealth,
        force_persistent=True,
    )
    controller.mode = "full"
    return controller


def create_project_preferred_controller() -> PersistentChromeController | None:
    """Create a controller from the project's preferred browser config.

    Returns:
        Configured controller, or None when no project preference is set.
    """
    config = load_project_browser_config()
    if config is None:
        return None
    return create_controller_from_browser_config(config, source="project")


def get_session_override_path() -> Path:
    """Return the per-project browser session override file path.

    Returns:
        Path to the browser session override JSON file.
    """
    return CACHE_DIR / f"browser_session_{_project_key_suffix()}.json"


def save_session_override(config: ProjectBrowserConfig) -> None:
    """Persist a browser session override for the current project.

    Args:
        config: Browser session override to save.

    Returns:
        None.
    """
    config.saved_at = time.time()
    path = get_session_override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2, sort_keys=True))


def clear_session_override() -> None:
    """Remove the browser session override for the current project.

    Returns:
        None.
    """
    get_session_override_path().unlink(missing_ok=True)


def load_session_override() -> ProjectBrowserConfig | None:
    """Load the current project's explicit browser session override.

    Returns:
        Browser session override, or None when not set.
    """
    return ProjectBrowserConfig.from_path(get_session_override_path())


def create_session_override_controller() -> PersistentChromeController | None:
    """Create a controller from the explicit browser session override.

    Returns:
        Configured controller, or None when no override is set.
    """
    config = load_session_override()
    if config is None:
        return None
    return create_controller_from_browser_config(config, source="override")


def get_browser_session_status() -> dict[str, Any]:
    """Return project browser session diagnostics.

    Returns:
        Browser session status for override, project preference, and active attach.
    """
    override = load_session_override()
    project_path = find_project_browser_config_path()
    project = load_project_browser_config()
    active = ActiveAttachConfig.from_path(get_active_attach_config_path())
    active_live = active is not None and is_devtools_available(active.browser_url)
    selected_source = (
        "override"
        if override
        else "project"
        if project
        else "active_attach"
        if active_live
        else "default_headless"
    )
    return {
        "selected_source": selected_source,
        "override": asdict(override) if override else None,
        "project_config_path": str(project_path) if project_path else None,
        "project_preference": asdict(project) if project else None,
        "active_attach": asdict(active) if active else None,
        "active_attach_live": active_live,
        "default": {"mode": "headless", "headless": True, "isolated": True, "channel": "canary"},
    }


def save_active_attach_config(
    browser_url: str,
    *,
    profile: str | None = None,
    mode: str = "full",
    stealth: bool = False,
) -> None:
    """Persist the current external Chrome attachment for future tool calls.

    Args:
        browser_url: Remote debugging endpoint.
        profile: Optional named browser profile.
        mode: Access mode.
        stealth: Whether stealth patches are enabled.

    Returns:
        None.
    """
    ActiveAttachConfig(
        browser_url=browser_url,
        profile=profile,
        mode=mode,
        stealth=stealth,
        saved_at=time.time(),
    ).save(get_active_attach_config_path())


def clear_active_attach_config() -> None:
    """Remove the saved external Chrome attachment for this project.

    Returns:
        None.
    """
    get_active_attach_config_path().unlink(missing_ok=True)


def load_active_attach_controller() -> PersistentChromeController | None:
    """Recreate the attached-browser controller from saved project state.

    Returns:
        Configured controller when a recent live attach exists, otherwise None.
    """
    config = ActiveAttachConfig.from_path(get_active_attach_config_path())
    if config is None:
        return None
    if time.time() - config.saved_at > ACTIVE_ATTACH_TTL_SECONDS:
        clear_active_attach_config()
        return None
    if not is_devtools_available(config.browser_url):
        # Transient unreachability (Chrome restarting, port not yet up) must not
        # permanently drop the attachment to a fresh logged-out default. Keep
        # the saved config so the next call can reattach once Chrome is back;
        # only the TTL above clears it.
        return None

    controller = PersistentChromeController(
        isolated=False,
        browser_url=config.browser_url,
        profile=config.profile,
        stealth=config.stealth,
        force_persistent=True,
    )
    controller.mode = config.mode
    return controller


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


def _pid_holds_user_data_dir(pid: int, user_data_dir: Path) -> bool:
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


def build_session_key(
    *,
    browser_url: str | None,
    isolated: bool,
    channel: str,
) -> str:
    """Build a stable key for the on-disk profile of a non-named session.

    One repository is one project is one browser: the key is derived from
    the resolved project root (git root, else cwd) and channel only, so a
    headed and a headless call - or an isolated and a persistent call - from
    the same project resolve to the same user-data-dir and reuse one Chrome
    instead of fragmenting into several and losing login state.

    ``isolated`` is accepted for backward compatibility but no longer
    participates in the key: throwaway and persistent sessions share the
    project bucket so auth survives. ``browser_url`` still partitions,
    because an externally attached Chrome is genuinely a different browser.
    ``channel`` partitions because different Chrome channels are different
    binaries that must not share a profile directory.

    Args:
        browser_url: Explicit remote debugging endpoint, if any.
        isolated: Ignored (kept for call-site compatibility).
        channel: Chrome channel.

    Returns:
        Deterministic short hash identifying the browser session bucket.
    """
    del isolated  # See docstring: no longer fragments the per-project bucket.
    raw_key = "|".join(
        [
            browser_url or "auto",
            get_project_id(),
            str(resolve_project_root()),
            channel,
        ]
    )
    return hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:16]


# --- Process/OS utilities re-exported from process_utils.py ---
# Functions above are now defined in .process_utils and imported at top of file.


def describe_profile_runtime(profile_name: str) -> dict[str, Any]:
    """Describe the live state of a named profile's Chrome process.

    Args:
        profile_name: Named profile under ``CACHE_DIR / 'profiles'``.

    Returns:
        Status dict with keys:
            - ``profile``: profile name.
            - ``user_data_dir``: profile directory path string.
            - ``exists``: whether the profile directory exists.
            - ``pid``: PID of the Chrome holding the profile, or None.
            - ``intended_port``: ``--remote-debugging-port`` from the cmdline.
            - ``port``: live debug port reachable for CDP, or None.
            - ``endpoint``: ``http://127.0.0.1:<port>`` when alive, else None.
            - ``devtools_alive``: whether ``/json/version`` answered.
            - ``port_collision_pids``: other PIDs listening on intended_port
              when the holding process didn't bind it.
            - ``current_url``: URL of the first non-blank tab, when reachable.
            - ``tab_count``: number of pages reported by ``/json/list``.
    """
    profile_dir = CACHE_DIR / "profiles" / profile_name
    info: dict[str, Any] = {
        "profile": profile_name,
        "user_data_dir": str(profile_dir),
        "exists": profile_dir.exists(),
        "pid": None,
        "intended_port": None,
        "port": None,
        "endpoint": None,
        "devtools_alive": False,
        "port_collision_pids": [],
        "current_url": None,
        "tab_count": 0,
    }
    if not profile_dir.exists():
        return info

    lock_pid = read_singleton_lock_pid(profile_dir)
    if lock_pid is None or not is_process_alive(lock_pid):
        return info
    info["pid"] = lock_pid

    intended = find_chrome_debug_port(lock_pid)
    info["intended_port"] = intended

    if intended is not None:
        endpoint = f"http://127.0.0.1:{intended}"
        if is_devtools_available(endpoint):
            info["port"] = intended
            info["endpoint"] = endpoint
            info["devtools_alive"] = True
            tabs = enumerate_tabs(endpoint)
            info["tab_count"] = len(tabs)
            for tab in tabs:
                url = tab.get("url") or ""
                if url and url != INITIAL_PAGE_URL:
                    info["current_url"] = url
                    break
        else:
            others = [pid for pid in find_listeners_on_port(intended) if pid != lock_pid]
            info["port_collision_pids"] = others
    return info


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


# ---------------------------------------------------------------------------
# Named Profile Management
# ---------------------------------------------------------------------------


def _is_named_profile(name: str) -> bool:
    """Check whether a directory name is a named profile (not a session key hash).

    Named profiles are human-readable names. Session key hashes are 16-char
    hex strings generated by build_session_key().

    Args:
        name: Directory name to check.

    Returns:
        True when the name looks like a human-chosen profile name.
    """
    # Session keys from build_session_key are 16-char hex strings
    if len(name) == 16:
        try:
            int(name, 16)
            return False
        except ValueError:
            pass
    return True


def list_profiles() -> list[str]:
    """List all named browser profiles.

    Returns:
        Sorted list of profile names.
    """
    profiles_dir = CACHE_DIR / "profiles"
    if not profiles_dir.exists():
        return []
    return sorted(
        d.name for d in profiles_dir.iterdir() if d.is_dir() and _is_named_profile(d.name)
    )


def find_live_profiles() -> list[dict[str, Any]]:
    """Return runtime descriptors for every profile with a reachable debug port.

    Returns:
        List of ``describe_profile_runtime`` results filtered to those whose
        Chrome process is alive and whose DevTools endpoint answered.
    """
    live: list[dict[str, Any]] = []
    for name in list_profiles():
        info = describe_profile_runtime(name)
        if info.get("devtools_alive"):
            live.append(info)
    return live


def delete_profile(name: str) -> bool:
    """Delete a named browser profile and its associated state files.

    Args:
        name: Profile name to delete.

    Returns:
        True when the profile existed and was deleted, False otherwise.
    """
    # Reject anything that isn't a plain profile name so a crafted value like
    # "../.." or "/etc" cannot escape the profiles directory and delete
    # arbitrary paths.
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or os.sep in name
        or (os.altsep and os.altsep in name)
        or not _is_named_profile(name)
    ):
        return False
    try:
        profiles_root = (CACHE_DIR / "profiles").resolve()
        profile_dir = (CACHE_DIR / "profiles" / name).resolve()
    except (OSError, ValueError):
        return False
    if profile_dir.parent != profiles_root:
        return False
    if not profile_dir.exists() or not profile_dir.is_dir():
        return False

    # Quit the Chrome process holding this profile before removing the
    # directory; otherwise delete_profile leaves an orphaned Chrome running on
    # a now-deleted user-data-dir (the exact "can't close them" accumulation
    # this fixes). Also stop its MCP daemon if one is recorded.
    lock_pid = read_singleton_lock_pid(profile_dir)
    if lock_pid is not None and is_process_alive(lock_pid):
        terminate_process_and_wait(lock_pid, timeout=5)
    session_key = f"profile_{name}"
    daemon_pid_file = CACHE_DIR / f"{session_key}.daemon.pid"
    try:
        daemon_pid = int(daemon_pid_file.read_text().strip())
    except (OSError, ValueError):
        daemon_pid = None
    if daemon_pid is not None and is_process_alive(daemon_pid):
        terminate_process_and_wait(daemon_pid, timeout=5)

    # Remove the profile directory
    shutil.rmtree(profile_dir, ignore_errors=True)

    # Remove associated state files
    for suffix in (".json", ".sock", ".daemon.pid", ".lock"):
        (CACHE_DIR / f"{session_key}{suffix}").unlink(missing_ok=True)

    return True
