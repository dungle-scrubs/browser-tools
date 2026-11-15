"""One teardown for every fixture that launches a real browser (#208, #232).

``lifecycle.stop`` refuses to report success when it cannot verify that the
browser exited, and it leaves the browser running so its recovery state
stays intact. That is right for a person's browser and wrong for a fixture:
the suite then ends with a headless browser nobody will stop. A fixture
calls ``stop_or_kill`` instead. The teardown still fails with stop's reason,
but the browser is gone first.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from typing import Protocol

from browser_tools import lifecycle, process_utils

#: How long a killed browser gets to disappear before teardown moves on.
_REAP_TIMEOUT_S = 2.0


class LaunchedBrowser(Protocol):
    """The fields of a launched instance that teardown reads."""

    @property
    def name(self) -> str: ...

    @property
    def pid(self) -> int: ...

    @property
    def user_data_dir(self) -> str: ...


def stop_or_kill(instance: LaunchedBrowser, registry_path: str) -> None:
    """Stop a fixture's browser, killing it when stop cannot verify the exit.

    Args:
        instance: The instance ``lifecycle.launch`` returned.
        registry_path: The fixture's own registry.

    Raises:
        lifecycle.LifecycleError: Stop's own failure, raised after the
            browser has been killed, so the teardown still fails.
    """
    try:
        lifecycle.stop(instance=instance.name, registry_path=registry_path)
    except lifecycle.LifecycleError:
        kill_browser(instance)
        raise


def kill_browser(instance: LaunchedBrowser) -> None:
    """Kill every process that still holds the instance's directory.

    The process scan is the first choice because it finds the browser and
    each helper by the directory on its command line. When the scan itself
    is unreadable, which is the failure that makes stop give up (#208), the
    fallback reads nothing from ``ps``: it signals the recorded PID only
    while that PID is a live child of this process.

    Args:
        instance: The instance ``lifecycle.launch`` returned.
    """
    holders = process_utils.find_user_data_dir_processes(instance.user_data_dir)
    if isinstance(holders, process_utils.ScanUnknown):
        _kill_live_child(instance.pid)
        return
    for pid in holders:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    _reap(instance.pid)


def _is_live_child(pid: int) -> bool:
    """Whether ``pid`` is a child of this process that has not exited.

    A child's PID cannot be reused until its parent has waited for it, so a
    live child is the process the fixture launched and not a recycled PID.
    """
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    return reaped == 0


def _kill_live_child(pid: int) -> None:
    """Kill the recorded browser without reading ``ps``.

    The launcher starts Chrome as the leader of its own session, so its
    process group is the browser and its helpers. Camoufox's runner shares
    the suite's group, so only the runner itself is signalled.
    """
    if not _is_live_child(pid):
        return
    with contextlib.suppress(ProcessLookupError):
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    _reap(pid)


def _reap(pid: int) -> None:
    """Wait briefly for a killed child, so it does not linger as a zombie."""
    deadline = time.monotonic() + _REAP_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            reaped, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if reaped != 0:
            return
        time.sleep(0.02)
