"""One launch at a time chooses a debug port and waits for its browser (#234).

The vendored allocator picks the first port that no registry entry holds and
nothing listens on. A browser takes a moment to start listening, so two
launches that allocate in that moment pick the same port. One browser wins
the bind. The other launch then finds a browser answering on "its" port and
registers the wrong one, and its own browser runs on with no debug port.

``lifecycle.registry_lock`` already runs launches one at a time when their
registries share a directory, which covers every launch on the default
registry. Launches on registries in different directories shared nothing,
and concurrent test runs, each with a registry of its own, drove one
another's browsers. The port space belongs to the machine, not to a
registry, so this lock is one file at a fixed path. A Chrome launch holds it
from allocation until its browser listens, which is the span the vendored
``launch_browser`` call covers.

A Camoufox launch does not take it. Camoufox never listens on the port its
registry entry records, so that number cannot collide with a listener, and
its runner is already running when the entry is written.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .core import registry as core_registry

if TYPE_CHECKING:
    from collections.abc import Generator

#: The lock file's name, beside the default registry.
LOCK_NAME = ".bt-port-allocation.lock"

#: How long a launch waits for the launches ahead of it. Each holder keeps
#: the lock for one browser start, which the launcher itself bounds.
WAIT_SECONDS = 120.0

_POLL_SECONDS = 0.05


def lock_path() -> Path:
    """Return the machine-wide lock file's path.

    Returns:
        The path beside the default registry, whichever registry a launch
        itself uses.
    """
    return Path(core_registry.REGISTRY_PATH).parent / LOCK_NAME


def _open_lock_file(path: Path) -> int:
    """Open the lock file, creating it as an empty 0600 regular file.

    The sequence is the session-root mutex's
    (``session_initialization.acquire_session_root_mutex``). The process
    umask filters the creation mode, so the call that creates the file sets
    0600 on its own descriptor; a mask that removes owner bits would
    otherwise leave a file every later launch fails to open (#214). An
    existing name that is not an empty regular 0600 file is not this lock,
    so it is refused and left as it was. The open never follows a symlink
    and never blocks on a special file.

    Args:
        path: The lock file's path. Its directory must exist.

    Returns:
        The open descriptor. It is not inheritable.

    Raises:
        OSError: The file could not be opened, or an existing name is not an
            empty regular file with mode 0600.
    """
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(path, flags)
        created = False
    try:
        if created:
            os.fchmod(fd, 0o600)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size != 0
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise OSError(
                errno.EINVAL,
                "the port allocation lock must be an empty regular file, mode 0600",
                str(path),
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def held(wait_seconds: float | None = None) -> Generator[None]:
    """Hold the port-allocation lock for the duration of the block.

    The file is empty and is never read or written: the lock is the kernel
    flock alone, so it ends when the holder exits for any reason. The
    descriptor is not inheritable, so a spawned browser never holds it.

    Args:
        wait_seconds: The longest wait for other launches. Defaults to
            ``WAIT_SECONDS``.

    Yields:
        Nothing. The lock is held until the block ends.

    Raises:
        TimeoutError: Other launches held the lock for the whole wait.
        OSError: The lock file could not be opened, or the name holds
            something that is not this lock.
    """
    wait = WAIT_SECONDS if wait_seconds is None else wait_seconds
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = _open_lock_file(path)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"another launch held the port allocation lock for {wait:g}s ({path})"
                    ) from None
                time.sleep(_POLL_SECONDS)
        yield
    finally:
        os.close(fd)
