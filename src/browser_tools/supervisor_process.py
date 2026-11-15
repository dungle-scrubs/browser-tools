"""Whether a process the registry recorded is still the one that was started.

A registry entry names the supervisor that watches an instance: its PID and
the start-time token that PID carried when it was recorded. Two questions
follow: is that process still running, and does the PID still carry the
token it was registered with. A PID alone cannot answer either one -- the
number is reused -- so both the liveness and the identity are checked.

The entry is read here too: an entry that names no supervisor has none to
report, which is not a fault.
"""

from __future__ import annotations

from typing import Any

from .core.utils import process_is_ours, process_start_time
from .process_utils import process_running_state


def state(entry: dict[str, Any]) -> str | None:
    """Whether ``entry``'s supervisor is still running.

    Returns "running", "missing", or None when no supervisor was ever
    recorded. None is not a fault: a headless launch gets no supervisor by
    design, and entries written before supervisors were recorded have nothing
    to check.

    Without a supervisor a live instance carries no window marking and is
    never retired from the registry when its browser closes, and nothing else
    reports that (#65).
    """
    pid = entry.get("supervisor_pid")
    if not isinstance(pid, int):
        return None
    ours = process_is_ours(pid=pid, expected_start=entry.get("supervisor_pid_start"))
    return "running" if ours else "missing"


def identity_matches(pid: int, expected_start: str | None) -> bool | None:
    """Whether ``pid`` still is the process that was started as ``expected_start``.

    Returns:
        True when the PID still carries the recorded start time, False when
        it carries another one, and None when the question could not be
        answered: the process's state or its start time could not be read.
    """
    running = process_running_state(pid)
    if running is not True:
        return running
    actual_start = process_start_time(pid)
    if expected_start is None or actual_start is None:
        return None
    return actual_start == expected_start


__all__ = ["identity_matches", "state"]
