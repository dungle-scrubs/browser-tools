"""The shared fixture teardown kills what stop could not verify (#232).

Each test launches a stand-in process that names a user-data directory on
its command line, as a browser does, and starts it as the leader of its own
session, as the launcher starts Chrome.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import browser_teardown
import pytest

from browser_tools import lifecycle, process_utils
from browser_tools.process_utils import ScanUnknown

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_SLEEPER = "import time; time.sleep(60)"


@dataclass(frozen=True)
class Launched:
    name: str
    pid: int
    user_data_dir: str


def _exited(proc: subprocess.Popen[bytes], within: float = 3.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.02)
    return proc.poll() is not None


@pytest.fixture
def stand_in(tmp_path: Path) -> Iterator[tuple[Launched, subprocess.Popen[bytes]]]:
    """A live process that holds ``tmp_path / "session"`` the way Chrome does."""
    directory = tmp_path / "session"
    directory.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-c", _SLEEPER, f"--user-data-dir={directory}"],
        start_new_session=True,
    )
    try:
        yield Launched("stand-in-01", proc.pid, str(directory)), proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _stop_cannot_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    def stop(*, instance: str, registry_path: str) -> str:
        raise lifecycle.LifecycleError(
            f"Browser exit unknown for {instance}: process scan failed: ps exited 1; "
            "recovery state preserved"
        )

    monkeypatch.setattr(lifecycle, "stop", stop)


def _scan_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def scan(_user_data_dir: str) -> ScanUnknown:
        return ScanUnknown("failed: ps exited 1")

    monkeypatch.setattr(process_utils, "find_user_data_dir_processes", scan)


def test_a_verified_stop_kills_nothing(
    stand_in: tuple[Launched, subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    launched, proc = stand_in
    monkeypatch.setattr(lifecycle, "stop", lambda *, instance, registry_path: "Stopped")

    browser_teardown.stop_or_kill(launched, "unused-registry.json")

    assert proc.poll() is None


def test_an_unverified_stop_kills_the_browser_and_still_fails(
    stand_in: tuple[Launched, subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    launched, proc = stand_in
    _stop_cannot_verify(monkeypatch)

    with pytest.raises(lifecycle.LifecycleError, match="Browser exit unknown"):
        browser_teardown.stop_or_kill(launched, "unused-registry.json")

    assert _exited(proc)


def test_every_holder_of_the_directory_is_killed(
    stand_in: tuple[Launched, subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    launched, proc = stand_in
    helper = subprocess.Popen(
        [sys.executable, "-c", _SLEEPER, f"--user-data-dir={launched.user_data_dir}"],
        start_new_session=True,
    )
    _stop_cannot_verify(monkeypatch)
    try:
        with pytest.raises(lifecycle.LifecycleError):
            browser_teardown.stop_or_kill(launched, "unused-registry.json")

        assert _exited(proc)
        assert _exited(helper)
    finally:
        if helper.poll() is None:
            helper.kill()
        helper.wait()


def test_an_unreadable_scan_still_kills_the_browser_and_still_fails(
    stand_in: tuple[Launched, subprocess.Popen[bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    launched, proc = stand_in
    _stop_cannot_verify(monkeypatch)
    _scan_is_unreadable(monkeypatch)

    with pytest.raises(lifecycle.LifecycleError, match="Browser exit unknown"):
        browser_teardown.stop_or_kill(launched, "unused-registry.json")

    assert _exited(proc)


def test_the_fallback_kills_the_browsers_whole_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The stand-in starts one helper inside its own process group and prints
    # the helper's PID, as Chrome starts its renderers.
    directory = tmp_path / "session"
    directory.mkdir()
    parent = (
        "import subprocess, sys, time;"
        f"child = subprocess.Popen([sys.executable, '-c', {_SLEEPER!r}]);"
        "print(child.pid, flush=True); time.sleep(60)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", parent, f"--user-data-dir={directory}"],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        helper_pid = int(proc.stdout.readline())
        _stop_cannot_verify(monkeypatch)
        _scan_is_unreadable(monkeypatch)

        with pytest.raises(lifecycle.LifecycleError):
            browser_teardown.stop_or_kill(
                Launched("stand-in-01", proc.pid, str(directory)), "unused-registry.json"
            )

        deadline = time.monotonic() + 3.0
        while process_utils.process_running_state(helper_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert process_utils.process_running_state(helper_pid) is False
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, 9)
        proc.wait()


def test_the_fallback_never_signals_a_pid_that_is_not_a_live_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PID 1 is alive and is not this process's child. A recycled PID looks
    # the same from here, so the fallback must leave it alone.
    signalled: list[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, _signal: signalled.append(pid))
    monkeypatch.setattr(os, "killpg", lambda pid, _signal: signalled.append(pid))
    _stop_cannot_verify(monkeypatch)
    _scan_is_unreadable(monkeypatch)

    with pytest.raises(lifecycle.LifecycleError):
        browser_teardown.stop_or_kill(Launched("gone-01", 1, "/tmp/unused"), "unused.json")

    assert signalled == []


def _group_members(pgid: int) -> list[int]:
    """The PIDs in one process group, read with pgrep."""
    found = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True, check=False)
    return [int(line) for line in found.stdout.split()]


def test_the_fallback_kills_a_real_launched_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fallback rests on two facts about the real launcher: the browser is
    # a child of the process that launched it, and it leads the process group
    # that holds its helpers. Stand-ins cannot prove either.
    if not os.environ.get(lifecycle.CHROME_BINARY_ENV_VAR):
        pytest.skip("no plain Chrome binary for an isolated launch")
    registry = str(tmp_path / "registry.json")
    instance = lifecycle.launch(headless=True, registry_path=registry, browser_args=["about:blank"])
    real_stop = lifecycle.stop
    try:
        members = _group_members(instance.pid)
        assert instance.pid in members
        assert len(members) > 1, "expected the browser and at least one helper process"
        _stop_cannot_verify(monkeypatch)
        _scan_is_unreadable(monkeypatch)

        with pytest.raises(lifecycle.LifecycleError):
            browser_teardown.stop_or_kill(instance, registry)

        deadline = time.monotonic() + 5.0
        while _group_members(instance.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _group_members(instance.pid) == []
    finally:
        monkeypatch.undo()
        if _group_members(instance.pid):
            real_stop(instance=instance.name, registry_path=registry)
