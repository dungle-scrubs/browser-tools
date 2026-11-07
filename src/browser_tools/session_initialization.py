"""The empty initialization marker a launch holds over a fresh session.

Generic session-initialization infrastructure for the launch path: while an
initializer prepares a freshly allocated unbound session directory, an empty
marker held under a kernel flock stops a concurrent orphan sweep from reaping
the directory, and the sweep can tell a held, unverifiable, or kernel-released
marker apart without ever reading a marker's contents. Nothing here knows
what the initializer does or which policy supplied it; the module owns the
marker protocol only.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import stat

#: Name of the empty marker a launch holds while an initializer prepares a
#: freshly allocated unbound session directory. The marker is an empty
#: regular file; ownership is the kernel flock on it, never its content.
INITIALIZING_MARKER = ".bt-initializing"


def publish_initialization_marker(session_dir: str) -> int | None:
    """Create the session's initialization marker and hold its kernel flock.

    The marker is staged under a private name, locked, and only then linked
    into place, so a concurrent sweep that finds the published name always
    sees an already-held lock rather than a lock about to be taken. An empty
    regular file, mode 0600, with no content ever written or read.

    Args:
        session_dir: The freshly allocated session directory.

    Returns:
        The held marker's file descriptor, to pass to
        :func:`release_initialization_marker`, or None when the marker could
        not be published. None degrades to today's unprotected window rather
        than failing the launch.
    """
    marker = os.path.join(session_dir, INITIALIZING_MARKER)
    staging = os.path.join(session_dir, f"{INITIALIZING_MARKER}-{os.getpid()}.tmp")
    try:
        fd = os.open(staging, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.link(staging, marker)
    except OSError:
        os.close(fd)
        return None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(staging)
    return fd


def release_initialization_marker(session_dir: str, fd: int) -> None:
    """Remove the session's initialization marker and drop its lock.

    The name is removed before the lock is dropped, so a sweep that already
    holds an open descriptor sees the lock held until this returns, and a
    sweep arriving later finds no marker at all. Best-effort: a failure here
    never fails the launch, and the kernel releases the flock when the
    process exits regardless.

    Args:
        session_dir: The session directory the marker was published in.
        fd: The descriptor :func:`publish_initialization_marker` returned.
    """
    with contextlib.suppress(OSError):
        os.unlink(os.path.join(session_dir, INITIALIZING_MARKER))
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def initialization_marker_holds(session_dir: str) -> bool:
    """Report whether an initialization marker holds this session directory.

    A marker that cannot be opened at all, or that is not an empty regular
    file, is unverifiable and reported as holding the directory: the sweep
    never follows a symlinked marker or blocks on a FIFO, and it never deletes
    a directory whose initializer it cannot vouch is dead. A marker whose lock
    can be taken was left by an initializer the kernel already reaped, so the
    ordinary orphan sweep may proceed.

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
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
