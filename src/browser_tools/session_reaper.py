"""Orphaned-session reaping: the safety net over the on-disk session layout.

A daemon quits the Chrome it owns on its idle timeout and on shutdown, but a
daemon that is SIGKILLed or that dies with the machine never gets the chance.
Its Chrome then survives indefinitely - and because headless Chrome still
registers a macOS dock icon with no window, the user cannot quit it from the
UI at all. This module is that safety net, run on every session creation.

The layout (where state files live, how session keys are built, the file-level
cleanup) is owned by ``session_layout``; the Chrome teardown primitives
(``quit_owned_chrome``, ``is_owned_profile_dir``, ``pid_holds_user_data_dir``)
are owned by ``persistent_browser``. This module is the *ledger* that reads
the layout, decides which recorded sessions are orphaned, and asks
``persistent_browser`` to quit the Chromes behind them. It owns no processes
and no paths of its own.
"""

from __future__ import annotations

import contextlib
import fcntl
import time
from pathlib import Path
from typing import Any

from . import persistent_browser
from . import session_layout as layout
from .browser_state import BrowserState

# How long an owned automation Chrome may sit with no live daemon before the
# orphan sweep quits it. Matches mcp_daemon.IDLE_TIMEOUT_SECONDS: a session this
# idle would already have been torn down had its daemon survived.
ORPHAN_REAP_IDLE_SECONDS = 30 * 60


def _daemon_still_running(state: BrowserState, session_key: str) -> bool:
    """Check whether the daemon recorded in ``state`` is genuinely still alive.

    A bare "is that PID alive" test is not enough: the daemon's PID can be
    recycled by an unrelated long-lived process, which would make its Chrome
    look permanently managed and exempt it from reaping forever. The PID file is
    what the daemon itself writes, so agreement between the two is the identity
    check.

    Args:
        state: Session state naming a daemon PID.
        session_key: Session key identifying that daemon's PID file.

    Returns:
        True when a live process matches the PID that daemon recorded on disk.
    """
    if state.daemon_pid is None or not persistent_browser.is_process_alive(state.daemon_pid):
        return False
    try:
        recorded = int(layout.daemon_pid_file(session_key).read_text().strip())
    except (OSError, ValueError):
        return False
    return recorded == state.daemon_pid


def _looks_orphaned(state: BrowserState | None, session_key: str, now: float) -> bool:
    """Apply the cheap, file-only half of the orphan test.

    Deliberately excludes the probes that cost a process lookup, so the sweep
    can rule most sessions out before it touches a lock file. A True result is
    provisional: the caller must re-run the full test under the session lock.

    Args:
        state: Parsed session state, or None when the file was unreadable.
        session_key: Session key owning that state.
        now: Current wall-clock time.

    Returns:
        True when this session is worth locking and examining properly.
    """
    if state is None or state.pid is None:
        return False
    # Only a Chrome this tool launched may be force-quit. The directory check is
    # kept as a second, independent barrier against a mode='real' or external
    # browser reaching this point with a stale owned flag.
    if not state.chrome_owned or not persistent_browser.is_owned_profile_dir(state.user_data_dir):
        return False
    # A live daemon still owns this Chrome and will quit it on its idle timeout.
    if _daemon_still_running(state, session_key):
        return False
    return now - state.last_used_at >= ORPHAN_REAP_IDLE_SECONDS


def _quit_session_if_orphaned(
    state_path: Path, session_key: str, now: float
) -> dict[str, Any] | None:
    """Apply the orphan test to one session and quit its Chrome when it passes.

    Must be called while holding the session's spawn lock, because it reads the
    state file and signals the PID it names.

    Args:
        state_path: ``<session_key>.json`` file to evaluate.
        session_key: Session key owning that state file.
        now: Current wall-clock time, shared across one sweep.

    Returns:
        A summary dict when a Chrome was quit, else None.
    """
    # Re-read under the lock: the pre-filter's copy may be stale by now.
    state = BrowserState.from_path(state_path)
    if not _looks_orphaned(state, session_key, now) or state is None or state.pid is None:
        return None
    user_data_dir = Path(str(state.user_data_dir))
    # Guard against a recycled PID that is now an unrelated process.
    if not persistent_browser.is_process_alive(
        state.pid
    ) or not persistent_browser.pid_holds_user_data_dir(state.pid, user_data_dir):
        layout.clear_session_files(session_key, keep_lock=True)
        return None
    if not persistent_browser.quit_owned_chrome(state.pid, user_data_dir, state.chrome_started_at):
        # The browser survived, most likely because signalling failed. Leave
        # every runtime file in place: the state is the only record of what is
        # still running, and the next sweep needs it to try again.
        return None
    # Cleanup happens here, under the lock, so no other wrapper can be midway
    # through spawning a daemon against files we are deleting.
    layout.clear_session_files(session_key, keep_lock=True)
    return {
        "session_key": session_key,
        "pid": state.pid,
        "user_data_dir": str(user_data_dir),
        "endpoint": state.browser_url,
    }


def _reap_one_session(state_path: Path, now: float) -> dict[str, Any] | None:
    """Quit the Chrome behind a single orphaned session state file.

    Takes the session's spawn lock before reading the state, so a wrapper that
    is concurrently launching or reusing this session cannot have its Chrome
    signalled out from under it. A session whose lock is already held is in use
    by definition and is skipped.

    Args:
        state_path: ``<session_key>.json`` file to evaluate.
        now: Current wall-clock time, shared across one sweep.

    Returns:
        A summary dict when a Chrome was quit, else None.
    """
    session_key = state_path.stem
    lock_file_path = layout.lock_path(session_key)
    summary: dict[str, Any] | None = None
    with contextlib.suppress(OSError), open(lock_file_path, "w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Another wrapper holds this session; it is in use, not orphaned.
            return None
        try:
            summary = _quit_session_if_orphaned(state_path, session_key, now)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
    return summary


def reap_orphaned_sessions() -> list[dict[str, Any]]:
    """Quit automation Chromes whose daemon died and left them running.

    A session is only reaped when all of these hold, so a browser that is still
    in use or that the tool does not own is never signalled:

    - the state records ``chrome_owned`` - this tool launched that process, and
      it is not a ``mode='real'`` or externally attached browser,
    - its ``user-data-dir`` is a private automation profile under
      ``CACHE_DIR/profiles``, an independent second barrier,
    - the daemon recorded for it is gone, confirmed against its PID file so a
      recycled daemon PID cannot exempt a browser forever,
    - it has been idle longer than the daemon's own idle timeout,
    - the recorded PID's command line still resolves to that profile,
    - and its ``ps`` start time still matches the one recorded at launch,
      re-checked immediately before each signal.

    Returns:
        Summaries of the sessions whose Chrome was quit.
    """
    now = time.time()
    reaped: list[dict[str, Any]] = []
    for state_path in layout.iter_session_state_paths():
        # Cheap pre-filter first: this runs on every tool call, and most state
        # files describe sessions whose Chrome is long gone.
        if not _looks_orphaned(BrowserState.from_path(state_path), state_path.stem, now):
            continue
        summary = _reap_one_session(state_path, now)
        if summary is not None:
            reaped.append(summary)
    return reaped
