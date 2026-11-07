"""The empty initialization marker a launch holds over a fresh session.

Generic session-initialization infrastructure for the launch path: while an
initializer prepares a freshly allocated unbound session directory, an empty
marker held under a kernel flock stops a concurrent orphan sweep from reaping
the directory, and the sweep can tell a held, unverifiable, or kernel-released
marker apart without ever reading a marker's contents. A second, coarser
lease - the session-root mutex, an empty regular file at the root of the
session tree, also held under a kernel flock - serializes a launch's visible
allocation through its marker publication against an orphan sweep's whole
check-and-delete transaction, so a sweep can never act on an absent-marker
observation that predates the publication. Nothing here knows what the
initializer does or which policy supplied it; the module owns the marker and
mutex protocols only.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import stat
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

#: Name of the empty marker a launch holds while an initializer prepares a
#: freshly allocated unbound session directory. The marker is an empty
#: regular file; ownership is the kernel flock on it, never its content.
INITIALIZING_MARKER = ".bt-initializing"

#: Name of the empty session-root mutex file. A plain regular file at the
#: root of the session tree, never a directory the sweep enters and never a
#: name Chrome creates; its existence carries no state - only the kernel
#: flock on it does - and nothing is ever written to or read from it.
SESSION_ROOT_MUTEX_NAME = ".bt-session-root.lock"


def publish_initialization_marker(session_dir: str) -> int:
    """Create the session's initialization marker and hold its kernel flock.

    The marker is staged under a private name, locked, and only then linked
    into place, so a concurrent sweep that finds the published name always
    sees an already-held lock rather than a lock about to be taken. An empty
    regular file, mode 0600, with no content ever written or read.

    A session this marker protects must never continue unprotected: a
    publication failure raises instead of degrading to an unprotected
    window, and the caller aborts the launch before the initializer or any
    browser spawn runs.

    Args:
        session_dir: The freshly allocated session directory.

    Returns:
        The held marker's file descriptor, to pass to
        :func:`release_initialization_marker`.

    Raises:
        OSError: The marker could not be staged, locked, or linked into
            place. The staging name is removed and the descriptor closed
            before the raise; the caller removes the session directory it
            was allocating, or retains and logs its path when even that
            cleanup fails.
    """
    marker = os.path.join(session_dir, INITIALIZING_MARKER)
    staging = os.path.join(session_dir, f"{INITIALIZING_MARKER}-{os.getpid()}.tmp")
    fd: int | None = None
    try:
        fd = os.open(staging, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.link(staging, marker)
    except BaseException:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise
    finally:
        with contextlib.suppress(OSError):
            os.unlink(staging)
    return fd


def release_initialization_marker(session_dir: str, fd: int) -> None:
    """Remove the session's initialization marker and drop its lock.

    The name is removed before the lock is dropped, so a sweep that already
    holds an open descriptor sees the lock held until this returns, and a
    sweep arriving later finds no marker at all. Best-effort throughout - no
    step here can raise - so a release failure never masks the failure that
    aborted the launch, and the kernel releases the flock when the process
    exits regardless.

    Args:
        session_dir: The session directory the marker was published in.
        fd: The descriptor :func:`publish_initialization_marker` returned.
    """
    with contextlib.suppress(OSError):
        os.unlink(os.path.join(session_dir, INITIALIZING_MARKER))
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)


def session_root_mutex_path(session_root: str) -> str:
    """Return the session-root mutex file's path under ``session_root``.

    Args:
        session_root: The shared session root directory.

    Returns:
        The mutex file's path inside the session root.
    """
    return os.path.join(session_root, SESSION_ROOT_MUTEX_NAME)


def acquire_session_root_mutex(session_root: str) -> int:
    """Open and exclusively lock the session-root mutex, blocking until held.

    The mutex is an empty regular file, mode 0600, created on first use; no
    content is ever written or read and symlinks are never followed, so the
    lock is the kernel flock alone. Acquisition blocks while another launch
    or sweep holds it - both critical sections are short and wait on nothing
    but the filesystem - so inability to hold it is an :class:`OSError` each
    caller maps to its own failure policy: a launch that cannot serialize
    its allocation fails operationally rather than proceeding unprotected,
    and a sweep that cannot hold it skips the orphan walk rather than
    deleting without the serialization.

    Args:
        session_root: The shared session root directory; it must exist.

    Returns:
        The held mutex's file descriptor, for
        :func:`release_session_root_mutex`.

    Raises:
        OSError: The mutex could not be opened or locked. Nothing was held.
    """
    fd = os.open(
        session_root_mutex_path(session_root),
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
    return fd


def release_session_root_mutex(fd: int) -> None:
    """Drop the session-root mutex. Best-effort; never raises.

    A release failure never masks the failure (if any) that is unwinding
    past it, and the kernel releases the flock when the process exits
    regardless.

    Args:
        fd: The descriptor :func:`acquire_session_root_mutex` returned.
    """
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)


@contextlib.contextmanager
def session_root_exclusion(session_root: str) -> Generator[int]:
    """Hold the session-root mutex across one critical section.

    For a launch's critical section - visible allocation through marker
    publication. An orphan sweep holds the same mutex across its whole
    check-and-delete transaction instead (see
    :func:`acquire_session_root_mutex`); the two never nest with the
    initializer callback, preferences, browser startup, or any registry
    lock, so neither critical section can deadlock with them.

    Args:
        session_root: The shared session root directory; it must exist.

    Yields:
        The held mutex's file descriptor.

    Raises:
        OSError: The mutex could not be opened or locked.
    """
    fd = acquire_session_root_mutex(session_root)
    try:
        yield fd
    finally:
        release_session_root_mutex(fd)


def initialization_marker_holds(session_dir: str) -> bool:
    """Report whether an initialization marker holds this session directory.

    A marker that cannot be opened at all, or that is not an empty regular
    file, is unverifiable and reported as holding the directory: the sweep
    never follows a symlinked marker or blocks on a FIFO, it never reads a
    marker's contents (emptiness is the fstat size, nothing more), and it
    never deletes a directory whose initializer it cannot vouch is dead. A
    marker whose lock can be taken was left by an initializer the kernel
    already reaped, so the ordinary orphan sweep may proceed.

    Args:
        session_dir: An untracked candidate directory under the session root.

    Returns:
        True when the directory must be kept (held or unverifiable), False
        when no marker exists or the marker's lock is free.
    """
    marker = os.path.join(session_dir, INITIALIZING_MARKER)
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        return exc.errno != errno.ENOENT
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != 0:
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
