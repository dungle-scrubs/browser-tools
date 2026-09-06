"""Process and Chrome utility functions for browser-tools.

Low-level helpers for inspecting Chrome processes, managing PIDs, finding
executables and ports, and interacting with Chrome's remote debugging
HTTP endpoints.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


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
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def read_process_args(pid: int) -> list[str] | None:
    """Read exact argv without interpreting flattened ps output or environment data."""
    if pid <= 0:
        return None
    try:
        if sys.platform == "linux":
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            return (
                [os.fsdecode(arg) for arg in raw.removesuffix(b"\0").split(b"\0")] if raw else None
            )
        if sys.platform != "darwin":
            return None
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2 (sys/sysctl.h)
        size = ctypes.c_size_t()
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
            return None
        if size.value < 4 or size.value > 8 * 1024 * 1024:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            return None
        raw = buffer.raw[: size.value]
        count = int.from_bytes(raw[:4], sys.byteorder, signed=True)
        if count <= 0:
            return None
        offset = raw.index(b"\0", 4) + 1  # executable path, then alignment padding
        while offset < len(raw) and raw[offset] == 0:
            offset += 1
        args = []
        for _ in range(count):
            end = raw.index(b"\0", offset)
            args.append(os.fsdecode(raw[offset:end]))
            offset = end + 1
        return args
    except (OSError, ValueError):
        return None


def find_chrome_user_data_dir(pid: int) -> Path | None:
    """Return the full --user-data-dir argument, including spaces."""
    args = read_process_args(pid)
    if args is None:
        return None
    return user_data_dir_from_args(args)


def user_data_dir_from_args(args: list[str]) -> Path | None:
    """Extract a profile directory from exact argv, without re-tokenizing it."""
    for index, arg in enumerate(args):
        value = None
        if arg.startswith("--user-data-dir="):
            value = arg.partition("=")[2]
        elif arg == "--user-data-dir" and index + 1 < len(args):
            value = args[index + 1]
        if value:
            try:
                return Path(value).expanduser().resolve()
            except (OSError, RuntimeError):
                return None
    return None


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


def terminate_process_and_wait(pid: int | None, timeout: float = 5.0) -> bool:
    """Terminate a process and block until it exits, escalating to SIGKILL.

    After sending SIGTERM, this waits
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


def make_private_dirs(path: Path) -> None:
    """Create each missing directory with owner-only permissions."""
    missing = []
    current = path
    while not current.exists() and not current.is_symlink():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
