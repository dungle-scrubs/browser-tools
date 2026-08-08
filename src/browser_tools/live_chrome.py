"""Resolve the live Chrome backing a user-data-dir, behind one interface.

The sequence - read the SingletonLock PID, confirm it is alive, confirm it
actually holds this directory (not a recycled PID), find its debug port, and
confirm DevTools answers - was re-orchestrated at four call sites
(``persistent_browser._try_reuse_existing_chrome`` /
``_resolve_browser_state``, ``profile_catalog.describe_profile_runtime`` /
``delete_profile``, ``browser_session.handle_attach_browser``) with subtly
divergent shapes. The PID-recycle guard in particular was re-derived at each
site. This module owns the whole sequence once and returns a structured
``LiveChrome`` result.

Pure inspection plus a structured return; it does not launch or terminate
browsers. The low-level primitives live in :mod:`process_utils` and are reached
module-qualified (``process_utils.<fn>``) so a test patch at
``browser_tools.process_utils.<fn>`` propagates here - the single canonical
patch site for the whole resolution concept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import process_utils

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class LiveChrome:
    """Structured view of the Chrome holding a user-data-dir, if any.

    A non-None :func:`resolve_live_chrome` result means a live process holds
    the SingletonLock. ``holds_dir`` is ``True`` only when that process's
    ``--user-data-dir`` resolves to the directory asked about (the PID-recycle
    guard); it is ``None`` when the caller did not request verification, and
    ``False`` when verification was requested but the PID now points elsewhere.
    """

    pid: int
    holds_dir: bool | None
    intended_port: int | None
    port: int | None
    endpoint: str | None
    devtools_alive: bool
    port_collision_pids: list[int] = field(default_factory=list)


def resolve_live_chrome(
    user_data_dir: Path, *, verify_holds_dir: bool = False
) -> LiveChrome | None:
    """Resolve the live Chrome and its reachable DevTools endpoint for a dir.

    Args:
        user_data_dir: Chrome profile directory whose SingletonLock is inspected.
        verify_holds_dir: When True, confirm the lock PID's ``--user-data-dir``
            resolves to ``user_data_dir`` (guards against a recycled PID handing
            the wrong Chrome to another session). Off by default so the
            discovery loop over many profiles does not pay for a ``ps`` read per
            entry.

    Returns:
        A ``LiveChrome`` when a live process holds the SingletonLock, or None
        when no readable lock exists or the holding process is gone. ``port``,
        ``endpoint`` and ``devtools_alive`` are populated only when the intended
        debug port answers; ``port_collision_pids`` lists the other PIDs bound
        to that port when it does not.
    """
    lock_pid = process_utils.read_singleton_lock_pid(user_data_dir)
    if lock_pid is None or not process_utils.is_process_alive(lock_pid):
        return None

    holds = _pid_holds_dir(lock_pid, user_data_dir) if verify_holds_dir else None

    intended = process_utils.find_chrome_debug_port(lock_pid)
    if intended is None:
        return LiveChrome(
            pid=lock_pid,
            holds_dir=holds,
            intended_port=None,
            port=None,
            endpoint=None,
            devtools_alive=False,
            port_collision_pids=[],
        )

    endpoint = f"http://127.0.0.1:{intended}"
    alive = process_utils.is_devtools_available(endpoint)
    collisions: list[int] = []
    if not alive:
        collisions = [p for p in process_utils.find_listeners_on_port(intended) if p != lock_pid]
    return LiveChrome(
        pid=lock_pid,
        holds_dir=holds,
        intended_port=intended,
        port=intended if alive else None,
        endpoint=endpoint if alive else None,
        devtools_alive=alive,
        port_collision_pids=collisions,
    )


def _pid_holds_dir(pid: int, user_data_dir: Path) -> bool:
    """Confirm a PID's ``--user-data-dir`` resolves to ``user_data_dir``.

    Mirrors :func:`persistent_browser.pid_holds_user_data_dir` (kept there for
    the reaper's reuse) rather than importing it, to avoid a circular import
    back into the controller module.
    """
    actual = process_utils.find_chrome_user_data_dir(pid)
    if actual is None:
        return False
    try:
        return actual.resolve() == user_data_dir.resolve()
    except OSError:
        return False


__all__ = ["LiveChrome", "resolve_live_chrome"]
