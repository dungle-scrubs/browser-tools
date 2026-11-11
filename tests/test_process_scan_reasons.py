"""An unreadable process scan names the check that failed (#208).

A full suite once failed teardown with "Browser exit unknown" after the
system-wide ``ps`` scan came back unknown, and nothing recorded which of its
failure branches fired. Each branch now carries its own reason into the
``stop`` error, so the next occurrence explains itself.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from browser_tools import lifecycle, process_utils
from browser_tools.lifecycle import ExtendedInstance, LifecycleError
from browser_tools.process_utils import ScanUnknown


def _fake_ps(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0, stdout: str = ""
) -> None:
    def run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["ps"], returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(process_utils.subprocess, "run", run)


def _raising_ps(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    def run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise exc

    monkeypatch.setattr(process_utils.subprocess, "run", run)


class TestScanReasons:
    def test_a_readable_scan_lists_the_holder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_ps(monkeypatch, stdout="  41 chrome --user-data-dir=/tmp/held --x\n  42 sh\n")

        assert process_utils.find_user_data_dir_processes("/tmp/held") == [41]

    def test_no_directory(self) -> None:
        result = process_utils.find_user_data_dir_processes("")

        assert result == ScanUnknown("had no user-data directory to look for")

    def test_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_ps(monkeypatch, returncode=1, stdout="  1 launchd\n")

        result = process_utils.find_user_data_dir_processes("/tmp/held")

        assert result == ScanUnknown("failed: ps exited 1")

    def test_empty_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_ps(monkeypatch, stdout="\n")

        result = process_utils.find_user_data_dir_processes("/tmp/held")

        assert result == ScanUnknown("failed: ps printed nothing")

    def test_a_line_without_arguments_is_named_without_quoting_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_ps(monkeypatch, stdout="  1 launchd\n  77\n")

        result = process_utils.find_user_data_dir_processes("/tmp/held")

        assert result == ScanUnknown("could not parse ps line 2: 1 field(s), first field numeric")

    def test_a_line_without_a_pid_never_echoes_its_arguments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_ps(monkeypatch, stdout="tool --token=hunter2\n")

        result = process_utils.find_user_data_dir_processes("/tmp/held")

        assert isinstance(result, ScanUnknown)
        assert "hunter2" not in result.reason
        assert result.reason == "could not parse ps line 1: 2 field(s), first field not numeric"

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _raising_ps(monkeypatch, subprocess.TimeoutExpired(["ps"], 2))

        result = process_utils.find_user_data_dir_processes("/tmp/held")

        assert result == ScanUnknown("timed out: ps took longer than 2s")

    def test_os_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _raising_ps(monkeypatch, PermissionError("denied"))

        result = process_utils.find_user_data_dir_processes("/tmp/held")

        assert result == ScanUnknown("failed: PermissionError")


def _instance() -> ExtendedInstance:
    return ExtendedInstance(
        name="browser-tools-01",
        port=9425,
        pid=4242,
        browser_version="",
        user_data_dir="/tmp/held",
        launched=None,
        pid_start="tok",
        engine="chrome",
        profile=None,
    )


class TestStopNamesTheReason:
    def test_an_unknown_scan_before_termination(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            lifecycle,
            "find_user_data_dir_processes",
            lambda _dir: ScanUnknown("timed out: ps took longer than 2s"),
        )

        with pytest.raises(LifecycleError) as caught:
            lifecycle._stop_managed(_instance(), None)  # pyright: ignore[reportPrivateUsage]

        assert str(caught.value) == (
            "Browser exit unknown for browser-tools-01: process scan timed out: ps took "
            "longer than 2s; recovery state preserved"
        )

    def test_an_unverified_termination(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(lifecycle, "find_user_data_dir_processes", lambda _dir: [4242])
        monkeypatch.setattr(lifecycle, "process_start_time", lambda _pid: "tok")
        monkeypatch.setattr(lifecycle, "_terminate_verified", lambda _ext: False)

        with pytest.raises(LifecycleError, match=r"termination of process 4242 could not"):
            lifecycle._stop_managed(_instance(), None)  # pyright: ignore[reportPrivateUsage]

    def test_an_unknown_scan_after_termination(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scans = iter([[4242], ScanUnknown("failed: ps printed nothing")])
        monkeypatch.setattr(lifecycle, "find_user_data_dir_processes", lambda _dir: next(scans))
        monkeypatch.setattr(lifecycle, "process_start_time", lambda _pid: "tok")
        monkeypatch.setattr(lifecycle, "_terminate_verified", lambda _ext: True)

        with pytest.raises(LifecycleError, match=r": process scan failed: ps printed nothing;"):
            lifecycle._stop_managed(_instance(), None)  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.parametrize(
        ("state", "reason"),
        [
            (None, "state of directory holder 4242 could not be read"),
            (True, "process 4242 still holds the user-data directory"),
        ],
    )
    def test_a_holder_after_termination(
        self, monkeypatch: pytest.MonkeyPatch, state: bool | None, reason: str
    ) -> None:
        monkeypatch.setattr(lifecycle, "find_user_data_dir_processes", lambda _dir: [4242])
        monkeypatch.setattr(lifecycle, "process_start_time", lambda _pid: "tok")
        monkeypatch.setattr(lifecycle, "_terminate_verified", lambda _ext: True)
        monkeypatch.setattr(lifecycle, "process_running_state", lambda _pid: state)

        with pytest.raises(LifecycleError) as caught:
            lifecycle._stop_managed(_instance(), None)  # pyright: ignore[reportPrivateUsage]

        assert str(caught.value) == (
            f"Browser exit unknown for browser-tools-01: {reason}; recovery state preserved"
        )
