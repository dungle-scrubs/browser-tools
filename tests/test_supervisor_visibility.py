"""A supervisor that dies must leave a trace, and must not die with the shell.

Both registered instances on one machine were alive with no supervisor
process running (#65). While a supervisor is missing the window carries no
marking, and nothing ever retires the instance from the registry when the
browser closes. The supervisor wrote nothing on the way out, so why it left
was unanswerable.

Three parts:

- ``spawn_supervisor`` detaches into its own session, so a closing terminal
  does not take it. It already claimed to be detached; it was not.
- An exit log records why each supervisor left.
- ``bt status`` reports whether an instance has its supervisor, since
  nothing else notices.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from browser_tools import lifecycle
from browser_tools.core import registry as core_registry
from browser_tools.core import supervisor

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def registry_path(tmp_path: Path) -> str:
    return str(tmp_path / "registry.json")


def _seed(registry_path: str, **extra) -> None:
    entry = {
        "port": 9222,
        "pid": 2_000_000_000,
        "browser_version": "Chrome/1",
        "user_data_dir": "",
        "launched": "2026-01-01T00:00:00+00:00",
        "pid_start": None,
    }
    entry.update(extra)
    core_registry._save_registry({"only-01": entry}, registry_path)


class TestSupervisorIsDetached:
    def test_spawn_starts_a_new_session(self, monkeypatch) -> None:
        """Without a new session, closing the terminal SIGHUPs the supervisor."""
        captured: dict = {}

        def fake_popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return object()

        monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)

        supervisor.spawn_supervisor(
            port=9222, name="only-01", registry_path="/tmp/r.json", draw_border=True
        )

        assert captured["kwargs"].get("start_new_session") is True, (
            "the supervisor must outlive the shell that launched the browser"
        )


class TestExitLog:
    def test_a_recorded_exit_names_the_reason(self, tmp_path: Path) -> None:
        """An exit with no trace is why #65 could not be answered."""
        registry_path = str(tmp_path / "registry.json")

        supervisor.log_supervisor_exit(
            reason="browser closed",
            name="only-01",
            port=9222,
            registry_path=registry_path,
        )

        log = supervisor.supervisor_exit_log_path(registry_path)
        assert log.exists()
        line = log.read_text().strip()
        assert "only-01" in line
        assert "9222" in line
        assert "browser closed" in line

    def test_entries_accumulate(self, tmp_path: Path) -> None:
        """One instance's exit must not erase another's."""
        registry_path = str(tmp_path / "registry.json")

        supervisor.log_supervisor_exit(
            reason="browser closed", name="a-01", port=9222, registry_path=registry_path
        )
        supervisor.log_supervisor_exit(
            reason="event loop stalled", name="b-01", port=9223, registry_path=registry_path
        )

        lines = supervisor.supervisor_exit_log_path(registry_path).read_text().splitlines()
        assert len(lines) == 2
        assert "a-01" in lines[0]
        assert "b-01" in lines[1]

    def test_the_log_is_capped(self, tmp_path: Path, monkeypatch) -> None:
        """An append-only file on a long-lived machine must not grow forever."""
        registry_path = str(tmp_path / "registry.json")
        monkeypatch.setattr(supervisor, "_EXIT_LOG_MAX_BYTES", 400)

        for i in range(200):
            supervisor.log_supervisor_exit(
                reason=f"exit {i}", name="a-01", port=9222, registry_path=registry_path
            )

        log = supervisor.supervisor_exit_log_path(registry_path)
        assert log.stat().st_size <= 400
        # The newest entry survives the trim; that is the one worth reading.
        assert "exit 199" in log.read_text()

    def test_a_signal_is_recorded(self, tmp_path: Path, monkeypatch) -> None:
        """A default SIGTERM kills the process before any except clause runs."""
        import signal

        registry_path = str(tmp_path / "registry.json")
        monkeypatch.setitem(supervisor._EXIT_CONTEXT, "name", "a-01")
        monkeypatch.setitem(supervisor._EXIT_CONTEXT, "port", 9222)
        monkeypatch.setitem(supervisor._EXIT_CONTEXT, "registry_path", registry_path)

        installed: dict[int, object] = {}
        killed: list[int] = []
        monkeypatch.setattr(
            supervisor.signal,
            "signal",
            lambda sig, handler: installed.__setitem__(int(sig), handler),
        )
        monkeypatch.setattr(supervisor.os, "kill", lambda _pid, sig: killed.append(sig))

        supervisor._log_on_signal(signal.SIGTERM)
        installed[int(signal.SIGTERM)](int(signal.SIGTERM), None)

        log = supervisor.supervisor_exit_log_path(registry_path).read_text()
        assert "signal SIGTERM" in log
        assert killed == [int(signal.SIGTERM)], "the signal must still end the process"

    def test_a_failed_write_is_not_fatal(self, tmp_path: Path) -> None:
        """Losing the log must never take the supervisor down with it."""
        unwritable = tmp_path / "nope" / "deeper" / "registry.json"
        supervisor.log_supervisor_exit(
            reason="browser closed",
            name="a-01",
            port=9222,
            registry_path=str(unwritable),
        )


class TestStatusReportsTheSupervisor:
    def test_a_recorded_live_supervisor_reads_running(self, registry_path) -> None:
        """A supervisor we can still identify is present."""
        import os

        _seed(
            registry_path,
            supervisor_pid=os.getpid(),
            supervisor_pid_start=None,
        )

        row = lifecycle.status(registry_path=registry_path)[0]

        assert row["supervisor"] == "running"

    def test_a_recorded_dead_supervisor_reads_missing(self, registry_path) -> None:
        """This is the state #65 found and nothing reported."""
        _seed(registry_path, supervisor_pid=2_000_000_001, supervisor_pid_start=None)

        row = lifecycle.status(registry_path=registry_path)[0]

        assert row["supervisor"] == "missing"

    def test_no_recorded_supervisor_reads_none(self, registry_path) -> None:
        """A headless instance has no supervisor by design, and is not faulted."""
        _seed(registry_path)

        row = lifecycle.status(registry_path=registry_path)[0]

        assert row["supervisor"] is None


class TestSupervisorIsRecorded:
    """The write lives beside ``annotate_entry``.

    ``core/registry.py`` is a verbatim vendored module, so extended fields are
    written from the lifecycle layer through the registry's own atomic
    load/save helpers, exactly as the engine/profile fields are.
    """

    def test_the_spawned_pid_lands_in_the_registry(self, registry_path) -> None:
        """status can only report a supervisor the registry knows about."""
        _seed(registry_path)

        lifecycle.record_supervisor(
            "only-01", pid=4242, pid_start="tok", registry_path=registry_path
        )

        entry = core_registry._load_registry(registry_path)["only-01"]
        assert entry["supervisor_pid"] == 4242
        assert entry["supervisor_pid_start"] == "tok"

    def test_an_unknown_instance_is_ignored(self, registry_path) -> None:
        """A race that retired the instance first must not raise in the launcher."""
        _seed(registry_path)

        lifecycle.record_supervisor(
            "ghost-01", pid=1, pid_start=None, registry_path=registry_path
        )

        assert "ghost-01" not in core_registry._load_registry(registry_path)


def test_subprocess_is_the_real_module() -> None:
    """Guard the monkeypatch seam above against an import rename."""
    assert supervisor.subprocess is subprocess
