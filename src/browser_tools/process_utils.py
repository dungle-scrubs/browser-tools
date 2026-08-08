"""Process and Chrome utility functions for browser-tools.

Low-level helpers for inspecting Chrome processes, managing PIDs, finding
executables and ports, and interacting with Chrome's remote debugging
HTTP endpoints. Extracted from persistent_browser.py to keep the module
under 800 lines.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .session_layout import INITIAL_PAGE_URL as _initial_page_url

_DEBUG_PORT_PATTERN = re.compile(r"--remote-debugging-port=(\d+)")
_USER_DATA_DIR_PATTERN = re.compile(r"--user-data-dir=(\S+)")

# Advanced escape hatch for connecting to a non-loopback CDP endpoint.
_ALLOW_REMOTE_ENDPOINT_ENV = "BROWSER_TOOLS_ALLOW_REMOTE_ENDPOINT"


def validate_local_endpoint(endpoint: str) -> str | None:
    """Validate that a CDP endpoint targets the local loopback interface.

    Chrome remote debugging is loopback-bound by default, so an endpoint that
    resolves elsewhere is almost always a mistake or an attempt to make the
    wrapper issue requests to an arbitrary host. Non-loopback endpoints are
    rejected unless ``BROWSER_TOOLS_ALLOW_REMOTE_ENDPOINT=1`` is set.

    Args:
        endpoint: Remote debugging endpoint URL (e.g. http://127.0.0.1:9222).

    Returns:
        None when the endpoint is acceptable, otherwise a human-readable
        error message explaining the rejection.
    """
    if os.environ.get(_ALLOW_REMOTE_ENDPOINT_ENV) == "1":
        return None
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return f"Invalid endpoint '{endpoint}': expected http(s)://host:port."
    host = parsed.hostname
    if host == "localhost":
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return (
            f"Endpoint host '{host}' is not loopback. Chrome remote debugging is "
            f"local; set {_ALLOW_REMOTE_ENDPOINT_ENV}=1 to allow a remote endpoint."
        )
    if not ip.is_loopback:
        return (
            f"Endpoint host '{host}' is not loopback. Chrome remote debugging is "
            f"local; set {_ALLOW_REMOTE_ENDPOINT_ENV}=1 to allow a remote endpoint."
        )
    return None


def resolve_chrome_executable(channel: str) -> str | None:
    """Find a Chrome executable for the requested channel.

    Args:
        channel: Requested Chrome channel.

    Returns:
        Executable path when found, otherwise None.
    """
    mac_candidates = {
        "canary": [
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        ],
        "stable": [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ],
        "beta": [
            "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
        ],
        "dev": [
            "/Applications/Google Chrome Dev.app/Contents/MacOS/Google Chrome Dev",
        ],
    }
    linux_candidates = {
        "canary": ["google-chrome-canary", "chrome-canary", "google-chrome"],
        "stable": ["google-chrome", "chromium", "chromium-browser"],
        "beta": ["google-chrome-beta", "google-chrome"],
        "dev": ["google-chrome-unstable", "google-chrome"],
    }

    for candidate in mac_candidates.get(channel, []):
        if Path(candidate).exists():
            return candidate
    for candidate in linux_candidates.get(channel, []):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def resolve_system_profile_dir(channel: str) -> Path | None:
    """Locate the user's everyday Chrome profile directory for a channel.

    This is the real profile the user browses with day to day (cookies,
    extensions, history), NOT one of browser-tools' private automation
    profiles. Used by ``mode='real'`` so automation drives the same Chrome
    the user already has, giving a single dock icon that closes normally.

    Args:
        channel: Requested Chrome channel.

    Returns:
        Path to the real user-data-dir, or None on an unsupported platform
        or channel.
    """
    home = Path.home()
    if sys.platform == "darwin":
        mac_dirs = {
            "canary": "Google/Chrome Canary",
            "stable": "Google/Chrome",
            "beta": "Google/Chrome Beta",
            "dev": "Google/Chrome Dev",
        }
        rel = mac_dirs.get(channel)
        return home / "Library" / "Application Support" / rel if rel else None
    linux_dirs = {
        "canary": ".config/google-chrome-unstable",
        "stable": ".config/google-chrome",
        "beta": ".config/google-chrome-beta",
        "dev": ".config/google-chrome-unstable",
    }
    rel = linux_dirs.get(channel)
    return home / rel if rel else None


def build_browser_command(
    *,
    executable: str,
    port: int,
    user_data_dir: Path,
    headless: bool,
    viewport: str | None,
    system_profile: bool = False,
) -> list[str]:
    """Build the Chrome launch command for a persistent remote-debugging session.

    Args:
        executable: Chrome executable path.
        port: Remote debugging port.
        user_data_dir: Dedicated browser profile directory.
        headless: Whether to launch headless.
        viewport: Initial window size formatted as WIDTHxHEIGHT.
        system_profile: When True the user-data-dir is the user's real everyday
            profile (mode='real'); ``--disable-sync`` is omitted so Google sign-in
            and sync keep working as they do in the user's normal browser.

    Returns:
        Command list for subprocess.Popen.
    """
    command = [
        executable,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if not system_profile:
        command.append("--disable-sync")
    if headless:
        command.append("--headless=new")
    if viewport:
        width, height = viewport.lower().split("x", 1)
        command.append(f"--window-size={width},{height}")
    command.append(_initial_page_url)
    return command


def find_free_port() -> int:
    """Find an available localhost TCP port.

    Returns:
        Free TCP port number.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_devtools(browser_url: str, timeout_seconds: float) -> bool:
    """Wait for Chrome's remote debugging endpoint to become reachable.

    Args:
        browser_url: Base URL of the remote debugging endpoint.
        timeout_seconds: Maximum number of seconds to wait.

    Returns:
        True when the endpoint becomes reachable before the timeout.
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if is_devtools_available(browser_url):
            return True
        time.sleep(0.2)
    return False


def is_devtools_available(browser_url: str) -> bool:
    """Check whether the remote debugging endpoint is reachable.

    Args:
        browser_url: Base URL of the remote debugging endpoint.

    Returns:
        True when the endpoint responds successfully.
    """
    request = urllib.request.Request(f"{browser_url}/json/version")
    try:
        with urllib.request.urlopen(request, timeout=1) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


def enumerate_tabs(browser_url: str) -> list[dict[str, Any]]:
    """Enumerate open browser tabs via the /json/list debugging endpoint.

    Only returns tabs with type "page" (not background pages, service workers,
    or extensions).

    Args:
        browser_url: Base URL of the remote debugging endpoint.

    Returns:
        List of tab dictionaries with id, title, url, webSocketDebuggerUrl.
    """
    request = urllib.request.Request(f"{browser_url}/json/list")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            tabs = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return []
    return [tab for tab in tabs if tab.get("type") == "page"]


def select_tab_by_url(tabs: list[dict[str, Any]], url_pattern: str) -> dict[str, Any] | None:
    """Select the first tab whose URL contains the given pattern.

    Args:
        tabs: List of tab dictionaries from enumerate_tabs.
        url_pattern: Substring to match against tab URLs (case-insensitive).

    Returns:
        First matching tab dictionary, or None if no match.
    """
    pattern_lower = url_pattern.lower()
    for tab in tabs:
        if pattern_lower in tab.get("url", "").lower():
            return tab
    return None


def read_singleton_lock_pid(user_data_dir: Path) -> int | None:
    """Read the PID Chrome embedded in its SingletonLock symlink.

    Chrome creates ``SingletonLock`` as a symlink whose target is
    ``<hostname>-<pid>``. The PID identifies the running Chrome process
    holding this user-data-dir.

    Args:
        user_data_dir: Chrome profile directory.

    Returns:
        PID of the holding Chrome process, or None when no readable lock exists.
    """
    lock_path = user_data_dir / "SingletonLock"
    try:
        target = os.readlink(lock_path)
    except OSError:
        return None
    if "-" not in target:
        return None
    pid_part = target.rsplit("-", 1)[1]
    try:
        return int(pid_part)
    except ValueError:
        return None


def clean_stale_singleton_lock(user_data_dir: Path) -> None:
    """Remove Chrome singleton files when no live process holds the profile.

    Args:
        user_data_dir: Chrome profile directory.

    Returns:
        None.
    """
    lock_pid = read_singleton_lock_pid(user_data_dir)
    if lock_pid is not None and is_process_alive(lock_pid):
        return
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (user_data_dir / name).unlink()
        except (OSError, FileNotFoundError):
            continue


def read_process_command(pid: int) -> str | None:
    """Read the full command line of a running process via ``ps``.

    Args:
        pid: PID to inspect.

    Returns:
        Command-line string, or None when the process is gone or unreadable.
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def find_chrome_debug_port(pid: int) -> int | None:
    """Inspect a running Chrome process's command line for its debug port.

    Args:
        pid: PID of the Chrome process.

    Returns:
        Remote-debugging-port value if present in the command line, else None.
    """
    cmd = read_process_command(pid)
    if cmd is None:
        return None
    match = _DEBUG_PORT_PATTERN.search(cmd)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def find_chrome_user_data_dir(pid: int) -> Path | None:
    """Extract a Chrome process's --user-data-dir flag from its command line.

    Args:
        pid: PID of the Chrome process.

    Returns:
        The configured user-data directory, or None when not present.
    """
    cmd = read_process_command(pid)
    if cmd is None:
        return None
    match = _USER_DATA_DIR_PATTERN.search(cmd)
    if match is None:
        return None
    try:
        return Path(match.group(1)).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def find_listeners_on_port(port: int) -> list[int]:
    """Return PIDs holding TCP listeners on the given port.

    Uses ``lsof`` so it works without root on macOS and Linux. Returns an
    empty list if ``lsof`` is missing or no listeners are found.

    Args:
        port: TCP port to probe.

    Returns:
        Distinct PIDs listening on ``port`` (any address family).
    """
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return []
    if result.returncode not in (0, 1):
        return []
    pids: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid not in pids:
            pids.append(pid)
    return pids


def read_process_start_time(pid: int) -> str | None:
    """Read a process's start time, used as an identity token across signals.

    A PID alone is not an identity: the kernel recycles it, so a PID that was
    Chrome a moment ago can be an unrelated process by the time a signal is
    sent. The (pid, start time) pair is stable for the lifetime of a process and
    is not reused, so it is safe to compare before signalling.

    Args:
        pid: PID to inspect.

    Returns:
        The ``ps`` start-time string, or None when the process is gone or
        unreadable.
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    token = result.stdout.strip()
    return token or None


def is_process_alive(pid: int) -> bool:
    """Check whether a process id currently exists.

    Args:
        pid: Process id to probe.

    Returns:
        True when the process exists.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate_process(pid: int | None) -> None:
    """Best-effort termination for a spawned Chrome process.

    Args:
        pid: Process id to terminate.

    Returns:
        None.
    """
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return


def terminate_process_and_wait(pid: int | None, timeout: float = 5.0) -> bool:
    """Terminate a process and block until it exits, escalating to SIGKILL.

    Unlike :func:`terminate_process` (fire-and-forget SIGTERM), this waits
    for the process to actually exit so a caller can safely reuse the
    resources it held - notably a Chrome user-data-dir SingletonLock, which
    a still-dying Chrome would keep and hand off to a relaunch. SIGTERM is
    tried first; if the process is still alive after ``timeout`` seconds it
    is SIGKILLed and reaped.

    Args:
        pid: Process id to terminate.
        timeout: Seconds to wait after SIGTERM before escalating to SIGKILL.

    Returns:
        True when the process is gone before the deadline, False if it could
        not be reaped (already dead, or permission/ESRCH errors).
    """
    if pid is None:
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        # Already gone, or not ours - either way, nothing to wait on.
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return not is_process_alive(pid)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(0.1)
    return not is_process_alive(pid)
