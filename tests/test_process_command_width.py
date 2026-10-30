"""Regression tests for the width of the process-command scans.

Linux ``ps`` truncates every command line to the output width, which it takes
from ``COLUMNS`` (or the terminal size, or 80). Chrome's command line is
longer than that, so the truncation cut ``--user-data-dir`` off the end of the
line. The holder of a profile then looked like a stranger: the scan found no
holder and the shutdown path refused to stop the browser.

The fix asks ``ps`` for unlimited width. These tests run the real
``read_process_command`` -> ``pid_holds_user_data_dir`` /
``find_chrome_debug_port`` chain and the real ``find_user_data_dir_processes``
scanner against one real child process whose command line is wider than the
column, with no ``ps`` or adapter mock anywhere: a mocked ``ps`` cannot
truncate, so it cannot witness the defect the real one caused.

On a platform whose ``ps`` ignores ``COLUMNS`` for a piped command line
(macOS), the scans were never blind and these tests pass in both states.
"""

from __future__ import annotations

import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest

from browser_tools import process_utils

if TYPE_CHECKING:
    from pathlib import Path

_PADDING = "x" * 220
_DEBUG_PORT = 19423
_CHILD_LIFETIME_SECONDS = 300
_READY_TIMEOUT_SECONDS = 30.0
_REAP_TIMEOUT_SECONDS = 10.0

# One line, so ``ps`` never breaks the command line across lines.
_CHILD_CODE = (
    "import pathlib, sys, time; "
    "pathlib.Path(sys.argv[1]).write_text('ready'); "
    f"time.sleep({_CHILD_LIFETIME_SECONDS})"
)


@pytest.fixture
def wide_command_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real child process whose command line is wider than the ps column.

    The child carries more than 200 characters of padding before its
    ``--remote-debugging-port`` and its ``--user-data-dir``, so a scan that
    honours the column width loses both flags. ``COLUMNS`` is set to 40 to
    make that truncation deterministic where ``ps`` honours it.

    Yields:
        The child process and the user-data directory it advertises.
    """
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    ready = tmp_path / "ready"
    monkeypatch.setenv("COLUMNS", "40")
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CHILD_CODE,
            str(ready),
            f"--padding={_PADDING}",
            f"--remote-debugging-port={_DEBUG_PORT}",
            f"--user-data-dir={profile_dir}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
        while not ready.exists() and time.monotonic() < deadline:
            if child.poll() is not None:
                raise AssertionError(
                    f"the child exited with {child.returncode} before it was ready"
                )
            time.sleep(0.05)
        assert ready.exists(), "the child never reported itself ready"
        yield child, profile_dir
    finally:
        _reap(child)


def _reap(child: subprocess.Popen[bytes]) -> None:
    """Terminate the owned child, escalating to a kill, and reap it."""
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=_REAP_TIMEOUT_SECONDS)
    else:
        child.wait()


class TestCommandWidth:
    """The real scans carry a command line that is wider than the column."""

    def test_the_read_process_command_carries_the_whole_line(self, wide_command_child):
        """The single-process read keeps the flags a narrow column would cut."""
        child, profile_dir = wide_command_child

        command = process_utils.read_process_command(child.pid)

        assert command is not None, "the child's command line was unreadable"
        assert f"--remote-debugging-port={_DEBUG_PORT}" in command
        assert f"--user-data-dir={profile_dir}" in command

    def test_the_holder_check_sees_the_directory(self, wide_command_child):
        """The ownership predicate finds the directory the child advertises."""
        child, profile_dir = wide_command_child

        holds = process_utils.pid_holds_user_data_dir(child.pid, profile_dir)

        assert holds is True, "the child holding the directory was not recognised"

    def test_the_debug_port_is_found(self, wide_command_child):
        """The debug port is read off the same wide command line."""
        child, _profile_dir = wide_command_child

        port = process_utils.find_chrome_debug_port(child.pid)

        assert port == _DEBUG_PORT

    def test_the_process_scan_lists_the_holder(self, wide_command_child):
        """The system-wide scan finds the holder of the advertised directory."""
        child, profile_dir = wide_command_child

        pids = process_utils.find_user_data_dir_processes(str(profile_dir))

        assert pids is not None, "the process scan could not be read"
        assert child.pid in pids, "the scan missed the process holding the directory"

    def test_a_narrow_column_would_hide_the_holder(self, wide_command_child):
        """Witness that the fixture is meaningful where ps honours COLUMNS.

        On such a platform the plain scan is blind to the very flags the
        fixed scan reads, which is what makes the tests above a real
        regression rather than a restatement of the current behaviour.
        """
        child, profile_dir = wide_command_child

        plain = subprocess.run(
            ["ps", "-p", str(child.pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout

        if f"--user-data-dir={profile_dir}" in plain:
            pytest.skip("this platform's ps does not narrow a piped command line")
        assert f"--remote-debugging-port={_DEBUG_PORT}" not in plain
