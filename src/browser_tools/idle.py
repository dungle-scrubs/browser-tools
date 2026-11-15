"""Idle expiry for managed browser instances (issue #195).

Two things live here, and nothing else does:

- the **duration grammar** the CLI accepts (``90s``, ``30m``, ``2h``,
  ``1h30m``, ``1d``), and
- the **activity bookkeeping** that tells a keeper an instance is still in
  use.

Activity is the lifetime of a ``bt`` process that resolved a registered
instance and is about to drive it. :func:`begin` records that process against
the instance's lease; :func:`finish` removes it and refreshes the instance's
last-activity stamp. A command that never resolved an instance never appears
here, so ``status``, ``help`` and a failed invocation leave the countdown
alone.

The countdown is measured against **wall-clock** time on purpose. macOS
monotonic time stops during system sleep, so a monotonic deadline would hand
an instance that slept through the night a fresh hour on wake. An overdue
instance closes on wake instead.
"""

from __future__ import annotations

import os
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from . import lifecycle
from .core.utils import process_start_time
from .usage import UsageError

if TYPE_CHECKING:
    from .task_lease import ProcessIdentity

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

#: Instances this process began activity for, and the registry each lives in.
#: A process touches only the instances it began: one command drives one
#: instance, and a command that drives another must not reset this one's
#: countdown.
_begun: dict[str, str | None] = {}

#: This process's own identity, cached: the start-time token does not change
#: while the process lives, and reading it costs a process query.
_identity: ProcessIdentity | None = None


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
    """Return ``timestamp`` as an ISO-8601 UTC stamp."""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def deadline_of(last_activity: float | None, idle_timeout: int | None) -> float | None:
    """Return the epoch second the countdown ends, or None when it does not run."""
    if last_activity is None or idle_timeout is None:
        return None
    return last_activity + idle_timeout


def process_identity() -> ProcessIdentity | None:
    """This process's identity, or None when it cannot be read.

    The identity is the PID plus its process-start token, so a recycled PID
    is never mistaken for this process. It is cached once it is known: the
    token does not change while the process lives. A read that fails is not
    cached, so a later call tries again.
    """
    global _identity
    if _identity is not None:
        return _identity
    from . import task_lease

    start = process_start_time(os.getpid())
    if not start:
        return None
    _identity = task_lease.ProcessIdentity(os.getpid(), start)
    return _identity


def begin(name: str, registry_path: str | None) -> None:
    """Record that this process is about to drive the instance ``name``.

    Idempotent per process and per instance: a command that resolves the
    same instance twice (a Step Run, a verb that resolves and then re-resolves)
    holds one protection, not two.

    Nothing happens for an entry without a lease, for a lease that is not in
    the ``auto`` state, for an unknown registry, or when the registry lock
    cannot be taken: the command still runs, it simply does not count as
    activity.

    Args:
        name: The registered instance this process is about to drive.
        registry_path: The registry that holds it.
    """
    if name in _begun:
        return
    identity = process_identity()
    if identity is None:
        return
    from . import task_lease

    try:
        with lifecycle.registry_lock(registry_path):
            path, registry = task_lease.load_registry(registry_path)
            entry = registry.get(name)
            if entry is None:
                return
            value = entry.get("lease")
            if not isinstance(value, dict):
                return
            try:
                lease = task_lease.Lease.parse(value)
            except ValueError:
                return
            if lease.state != "auto":
                return
            records = [operation.record() for operation in lease.operations]
            if identity.record() not in records:
                records.append(identity.record())
                value["operations"] = records
                task_lease.save_registry(path, registry)
    except (lifecycle.LifecycleError, OSError):
        return
    _begun[name] = registry_path


def finish() -> None:
    """Release this process's protection and refresh the idle countdown.

    Runs at the end of every command, successful or not. Only this process's
    own identity is removed, so two overlapping commands each keep their own
    protection until they finish. The instance's last-activity stamp is set
    to now, which starts the countdown again from the moment the work ended.
    """
    begun = dict(_begun)
    _begun.clear()
    identity = process_identity()
    if identity is None or not begun:
        return
    from . import task_lease

    for name, registry_path in begun.items():
        try:
            with lifecycle.registry_lock(registry_path):
                path, registry = task_lease.load_registry(registry_path)
                entry = registry.get(name)
                if entry is None:
                    continue
                value = entry.get("lease")
                if not isinstance(value, dict):
                    continue
                try:
                    lease = task_lease.Lease.parse(value)
                except ValueError:
                    continue
                remaining = [
                    operation for operation in lease.operations if operation != identity
                ]
                value["operations"] = [operation.record() for operation in remaining]
                value["last_activity"] = time.time()
                task_lease.save_registry(path, registry)
        except (lifecycle.LifecycleError, OSError):
            continue


__all__ = [
    "DEFAULT_IDLE_TIMEOUT",
    "DEFAULT_IDLE_TIMEOUT_TEXT",
    "MAX_IDLE_TIMEOUT",
    "begin",
    "deadline_of",
    "finish",
    "iso_utc",
    "parse_duration",
    "process_identity",
]
