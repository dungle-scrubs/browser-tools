"""The launch-time sweep never removes a directory a running browser holds.

A launch removes session directories that no registry it can see tracks. A
browser tracked by a registry in another directory was invisible to it, and
a headless shell writes no SingletonLock, so its directory was deleted while
it ran. Concurrent test runs did this to one another.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

from browser_tools import process_utils
from browser_tools.core import launcher
from browser_tools.core import registry as core_registry
from browser_tools.process_utils import ScanUnknown

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def session_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty session root and an empty default registry, both temporary."""
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(launcher, "_SESSION_ROOT", str(root))
    monkeypatch.setattr(launcher, "REGISTRY_PATH", str(tmp_path / "default" / "registry.json"))
    monkeypatch.setattr(core_registry, "REGISTRY_PATH", str(tmp_path / "default" / "registry.json"))
    return root


@pytest.fixture
def held_session(session_root: Path) -> Iterator[tuple[Path, subprocess.Popen[bytes]]]:
    """A session directory that a live process names, with no SingletonLock."""
    session = session_root / "session-held"
    session.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", f"--user-data-dir={session}"]
    )
    try:
        yield session, proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_a_directory_a_running_process_holds_survives_another_registrys_sweep(
    held_session: tuple[Path, subprocess.Popen[bytes]], tmp_path: Path
) -> None:
    session, proc = held_session

    launcher.cleanup_sessions(registry_path=str(tmp_path / "other" / "registry.json"))
    assert session.is_dir()

    proc.kill()
    proc.wait()
    launcher.cleanup_sessions(registry_path=str(tmp_path / "other" / "registry.json"))
    assert not session.exists()


def test_an_unreadable_scan_removes_nothing(
    session_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = session_root / "session-unknown"
    session.mkdir()
    monkeypatch.setattr(
        process_utils, "held_user_data_dirs", lambda: ScanUnknown("failed: ps exited 1")
    )

    launcher.cleanup_sessions(registry_path=str(tmp_path / "other" / "registry.json"))

    assert session.is_dir()


def test_the_scan_runs_only_for_a_directory_it_could_save(
    session_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A sweep with nothing to remove reads no process list: most launches
    # find none, and the launcher's spawn doubles replace subprocess.
    def scan() -> set[str]:
        raise AssertionError("the sweep scanned processes with no candidate directory")

    monkeypatch.setattr(process_utils, "held_user_data_dirs", scan)

    launcher.cleanup_sessions(registry_path=str(tmp_path / "other" / "registry.json"))


def test_held_directories_come_from_every_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        stdout = (
            "  41 chrome --user-data-dir=/tmp/one --x\n"
            "  42 sh\n"
            "  43 chrome --user-data-dir=/tmp/two\n"
        )
        return subprocess.CompletedProcess(["ps"], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(process_utils.subprocess, "run", run)

    held = process_utils.held_user_data_dirs()

    assert isinstance(held, set)
    assert {path.rsplit("/", 1)[-1] for path in held} == {"one", "two"}
