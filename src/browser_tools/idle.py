"""Idle-timeout durations and deadlines for managed instances (issue #195).

This module holds the **duration grammar** the CLI accepts (``90s``,
``30m``, ``2h``, ``1h30m``, ``1d``) and the deadline arithmetic. It imports
nothing above it: the lease, its keeper and the activity bookkeeping live in
:mod:`browser_tools.task_lease`.

The countdown is measured against **wall-clock** time on purpose. macOS
monotonic time stops during system sleep, so a monotonic deadline would hand
an instance that slept through the night a fresh hour on wake. An overdue
instance closes on wake instead.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from .usage import UsageError

#: The idle timeout every managed launch starts with: one hour.
DEFAULT_IDLE_TIMEOUT = 3600

#: How that default is spelled, for the CLI's own default value.
DEFAULT_IDLE_TIMEOUT_TEXT = "1h"

#: The longest timeout a caller may configure. Beyond a week the instance is
#: not idle-expiring in any useful sense, and an accidental number would
#: otherwise read as "never".
MAX_IDLE_TIMEOUT = 7 * 24 * 3600

#: How many seconds each unit is worth.
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

#: Unit order. A duration lists its groups from the largest unit down.
_UNIT_RANK = {"s": 0, "m": 1, "h": 2, "d": 3}

_DURATION_PATTERN = re.compile(r"^(?:\d+[smhd])+$")

_DURATION_ERROR = (
    "An idle timeout is a duration such as 90s, 30m, 2h, 1h30m or 1d: one or "
    "more number-and-unit groups, the units s, m, h and d, each at most once, "
    "largest unit first, at most 7 days in total. A bare number is not a "
    "duration. For a browser that should never expire, use: bt retain INSTANCE"
)

def parse_duration(text: str) -> int:
    """Return the number of seconds ``text`` names.

    Accepts one or more ``<positive integer><unit>`` groups with the units
    ``s``, ``m``, ``h`` and ``d``, each at most once, largest unit first:
    ``90s``, ``30m``, ``2h``, ``1h30m``, ``1d``. A bare number is rejected:
    the unit is what says what the number counts.

    Args:
        text: The duration as the caller wrote it.

    Returns:
        The duration in seconds.

    Raises:
        UsageError: The text is not a duration, carries a zero, repeats a
            unit, lists the units out of order, or exceeds seven days (CLI
            exit 2).
    """
    if not _DURATION_PATTERN.fullmatch(text):
        raise UsageError(_DURATION_ERROR)
    total = 0
    seen: set[str] = set()
    previous_rank = len(_UNIT_RANK)
    for value, unit in re.findall(r"(\d+)([smhd])", text):
        if unit in seen:
            raise UsageError(_DURATION_ERROR)
        rank = _UNIT_RANK[unit]
        if rank >= previous_rank:
            # The groups run from the largest unit down, so each one must be
            # strictly smaller than the one before it.
            raise UsageError(_DURATION_ERROR)
        seen.add(unit)
        previous_rank = rank
        amount = int(value)
        if amount <= 0:
            raise UsageError(_DURATION_ERROR)
        total += amount * _UNIT_SECONDS[unit]
    if total <= 0 or total > MAX_IDLE_TIMEOUT:
        raise UsageError(_DURATION_ERROR)
    return total


def iso_utc(timestamp: float) -> str:
    """Return ``timestamp`` as an ISO-8601 UTC stamp.

    Args:
        timestamp: Seconds since the epoch.

    Returns:
        The same moment as an ISO-8601 string in UTC.
    """
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def deadline_of(last_activity: float | None, idle_timeout: int | None) -> float | None:
    """Return the epoch second the countdown ends.

    Args:
        last_activity: When the instance was last used, in epoch seconds.
        idle_timeout: The instance's idle timeout in seconds.

    Returns:
        The deadline in epoch seconds, or None when either value is absent:
        a lease from an earlier release has no countdown.
    """
    if last_activity is None or idle_timeout is None:
        return None
    return last_activity + idle_timeout


__all__ = [
    "DEFAULT_IDLE_TIMEOUT",
    "DEFAULT_IDLE_TIMEOUT_TEXT",
    "MAX_IDLE_TIMEOUT",
    "deadline_of",
    "iso_utc",
    "parse_duration",
]
