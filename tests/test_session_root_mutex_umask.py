"""The session-root mutex survives a restrictive umask (#214).

``os.open`` filters a new file's mode through the process umask. A first
acquisition under a mask that removes owner bits used to create the mutex as
mode 0400, refuse it as malformed, and leave it in place, so every later
launch failed. The restrictive mask runs in a child process, so neither this
process's umask nor the live session root changes, and the child gets a
literal environment, so no real value reaches its output.
"""

from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from browser_tools import session_initialization

if TYPE_CHECKING:
    from pathlib import Path

#: Removes the owner's write and execute bits, and every group and other bit.
_RESTRICTIVE_UMASK = 0o277

_CHILD = """
import os, sys
from browser_tools.session_initialization import (
    acquire_session_root_mutex,
    release_session_root_mutex,
)
os.umask(int(sys.argv[2], 8))
try:
    release_session_root_mutex(acquire_session_root_mutex(sys.argv[1]))
except OSError as error:
    print(f"refused errno={error.errno}")
else:
    print("acquired")
"""


def _acquire_in_child(root: Path, umask: int) -> str:
    """Acquire and release the mutex in a child process under ``umask``.

    Args:
        root: The synthetic session root.
        umask: The child's process umask.

    Returns:
        ``acquired``, or ``refused errno=<n>`` when the acquisition raised.
    """
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(root), f"{umask:o}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return result.stdout.strip()


class TestRestrictiveUmask:
    def test_a_first_acquisition_creates_a_usable_mutex_under_any_umask(
        self, tmp_path: Path
    ) -> None:
        """Created under umask 0277, the mutex is 0600 and later calls reuse it."""
        root = tmp_path / "sessions"
        root.mkdir()

        assert _acquire_in_child(root, _RESTRICTIVE_UMASK) == "acquired"

        mutex = root / session_initialization.SESSION_ROOT_MUTEX_NAME
        assert stat.S_IMODE(mutex.stat().st_mode) == 0o600
        assert _acquire_in_child(root, 0o022) == "acquired"

    def test_an_existing_owner_read_only_mutex_is_still_refused_untouched(
        self, tmp_path: Path
    ) -> None:
        """The fix normalizes only a file it just created, never an existing one.

        A 0400 file is what the earlier bug left behind. It is refused and
        keeps its mode: repairing a file at the protocol name would act on
        something this call never owned.
        """
        root = tmp_path / "sessions"
        root.mkdir()
        existing = root / session_initialization.SESSION_ROOT_MUTEX_NAME
        existing.write_text("")
        os.chmod(existing, 0o400)

        with pytest.raises(OSError) as refused:
            session_initialization.acquire_session_root_mutex(str(root))

        assert refused.value.errno in {errno.EACCES, errno.EINVAL}
        assert stat.S_IMODE(existing.stat().st_mode) == 0o400
        assert existing.read_text() == ""
