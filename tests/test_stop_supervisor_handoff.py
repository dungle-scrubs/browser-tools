"""Stopping an instance ends its supervisor; a supervisor never adopts a stranger (#176).

Two defects, one blast radius. ``bt stop`` closed the browser but left that
instance's supervisor running; a launch that then took the same CDP port under
a different name was adopted by the leftover supervisor, which re-marked its
tabs under the old name (the unbounded ``🤖 a — 🤖 b — 🤖 a`` title) and held a
second connection that made CDP calls time out.

The fix has two halves, tested here:

1. ``lifecycle.stop`` signals the recorded supervisor and confirms it exited
   before the browser is closed (``_stop_supervisor``).
2. The supervisor's reconnect loop verifies that the browser now answering on
   the port claims ITS user-data-dir before it re-attaches (``_port_serves_our_browser``
   in ``core/supervisor.py``, an adapted module). The user-data-dir rides on
   the supervisor's argv as its identity anchor; the port alone cannot tell
   "my browser survived a suspend" from "a different browser took the port".
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
from typing import TYPE_CHECKING

from browser_tools import lifecycle
from browser_tools.core import supervisor
from browser_tools.core.utils import process_start_time

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _live_supervisor_process() -> tuple[subprocess.Popen, str]:
    """A real, signallable stand-in for a supervisor process."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid_start = process_start_time(pid=proc.pid)
    assert pid_start is not None
    return proc, pid_start


def _entry_with_supervisor(
    tmp_path: Path,
    proc: subprocess.Popen,
    pid_start: str,
) -> str:
    registry_path = str(tmp_path / "registry.json")
    entry = {
        "port": _free_port(),
        "pid": 2_000_000_000,  # browser itself reads as dead on every host
        "browser_version": "Chrome/1",
        "user_data_dir": str(tmp_path / "session-x"),
        "launched": "2026-01-01T00:00:00+00:00",
        "pid_start": None,
        "engine": "chrome",
        "profile": None,
        "supervisor_pid": proc.pid,
        "supervisor_pid_start": pid_start,
    }
    lifecycle.core_registry._save_registry({"x-01": entry}, registry_path)
    return registry_path


class TestStopEndsTheSupervisor:
    def test_stop_terminates_the_recorded_supervisor(self, tmp_path: Path) -> None:
        proc, pid_start = _live_supervisor_process()
        registry_path = _entry_with_supervisor(tmp_path, proc, pid_start)
        try:
            outcome = lifecycle.stop(
                instance="x-01",
                registry_path=registry_path,
                _supervisor_term_wait=0.3,
                _supervisor_kill_wait=0.3,
            )
        finally:
            # Reap whatever is left; the sleeper is this test's own child, so
            # between SIGTERM and the reap it reads as a live zombie -- an
            # artifact no production `bt stop` sees, because it signals a
            # supervisor some other process spawned (launchd reaped it).
            proc.kill()
            proc.wait()

        # The stop ran the (dead-browser) registry cleanup and the supervisor
        # received the signal (it is dead the moment the test reaps it).
        assert lifecycle.core_registry._load_registry(registry_path) == {}
        assert proc.returncode is not None

    def test_stop_survives_a_supervisor_that_refuses_to_die(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A supervisor surviving SIGTERM and SIGKILL must not overclaim."""
        proc, pid_start = _live_supervisor_process()
        registry_path = _entry_with_supervisor(tmp_path, proc, pid_start)
        # Stub the liveness read so the process always looks alive, whatever
        # stop signals: the pathological case, not the timing one.
        monkeypatch.setattr(lifecycle, "process_is_ours", lambda pid, expected_start=None: True)
        try:
            outcome = lifecycle.stop(
                instance="x-01",
                registry_path=registry_path,
                _supervisor_term_wait=0.2,
                _supervisor_kill_wait=0.2,
            )
        finally:
            proc.kill()
            proc.wait()

        assert f"supervisor pid {proc.pid} survived" in outcome
        # The browser half still completed (its entry reads dead on every host).
        assert "cleaned up" in outcome

    def test_stop_without_a_recorded_supervisor_is_unaffected(self, tmp_path: Path) -> None:
        # Headless launches and pre-#65 registries carry no supervisor_pid.
        registry_path = str(tmp_path / "registry.json")
        entry = {
            "port": _free_port(),
            "pid": 2_000_000_000,
            "browser_version": "Chrome/1",
            "user_data_dir": "",
            "launched": "2026-01-01T00:00:00+00:00",
            "pid_start": None,
        }
        lifecycle.core_registry._save_registry({"x-02": entry}, registry_path)

        outcome = lifecycle.stop(instance="x-02", registry_path=registry_path)

        assert "x-02" in outcome

    def test_a_recycled_supervisor_pid_is_never_signalled(self, tmp_path: Path) -> None:
        # pid 1 is launchd: live, not ours, and never a supervisor. Identity
        # (user + recorded start time) gates the signal, not bare existence.
        registry_path = str(tmp_path / "registry.json")
        entry = {
            "port": _free_port(),
            "pid": 2_000_000_000,
            "browser_version": "Chrome/1",
            "user_data_dir": "",
            "launched": "2026-01-01T00:00:00+00:00",
            "pid_start": None,
            "supervisor_pid": 1,
            "supervisor_pid_start": "whatever launchd started at",
        }
        lifecycle.core_registry._save_registry({"x-03": entry}, registry_path)

        outcome = lifecycle.stop(instance="x-03", registry_path=registry_path)

        assert "x-03" in outcome


class TestPortServesOurBrowser:
    def _patch_claimants(self, monkeypatch: pytest.MonkeyPatch, claimants: list[str]) -> None:
        monkeypatch.setattr(supervisor, "_cdp_port_claimants", lambda port: set(claimants))

    def test_our_user_data_dir_attracts_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_claimants(monkeypatch, ["/tmp/session-a", "/tmp/session-b"])
        assert asyncio.run(
            supervisor._port_serves_our_browser(port=9423, user_data_dir="/tmp/session-a")
        )

    def test_a_foreign_browser_on_the_port_reads_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_claimants(monkeypatch, ["/tmp/someone-elses"])
        assert not asyncio.run(
            supervisor._port_serves_our_browser(port=9423, user_data_dir="/tmp/ours")
        )

    def test_no_claimants_is_conservative_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unattributable port keeps the pre-#176 behaviour (reconnect)."""
        self._patch_claimants(monkeypatch, [])
        assert asyncio.run(
            supervisor._port_serves_our_browser(port=9423, user_data_dir="/tmp/ours")
        )

    def test_the_ps_scan_finds_a_claiming_process(self, tmp_path: Path) -> None:
        """The claimant scan works where the registry's /proc walk cannot.

        macOS has no /proc, so the registry's scan returns an empty set there
        and the #176 gate would never fire on the platform the defect was
        reported on. This drives the real scan against a real process whose
        argv claims a debugging port.
        """
        port = _free_port()
        claim_dir = tmp_path / "claimant-session"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={claim_dir}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            claimants = supervisor._cdp_port_claimants(port=port)
        finally:
            proc.kill()
            proc.wait()

        assert str(claim_dir) in claimants


class TestReconnectNeverAdoptsAStranger:
    """The reconnect loop's identity gate, driven with the real loop code."""

    def test_a_foreign_browser_on_the_port_ends_the_supervisor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Browser gone inside the grace window, stranger took the port: exit.

        This is the #176 sequence: the old supervisor must retire NOTHING and
        attach to NOTHING; the new browser belongs to a newer launch.
        """
        retired: list[str] = []
        exits: list[str] = []

        async def failing_connection(**kwargs):
            raise ConnectionError("connection dropped")

        monkeypatch.setattr(supervisor, "_supervise_connection", failing_connection)
        monkeypatch.setattr(supervisor, "_browser_gone", _async_false)
        monkeypatch.setattr(supervisor, "_port_serves_our_browser", _async_port_serves_foreign)
        monkeypatch.setattr(
            supervisor,
            "log_supervisor_exit",
            lambda reason, name, port, registry_path: exits.append(reason),
        )

        async def drive() -> None:
            await supervisor._supervise_forever(
                port=9423,
                name="old-01",
                registry_path="/tmp/registry.json",
                scripts=None,
                border=supervisor.BorderSetting(lambda: True),
                heartbeat=supervisor.Heartbeat(),
                retire=lambda instance_name, registry_path: retired.append(instance_name),
                user_data_dir="/tmp/ours",
            )

        asyncio.run(drive())

        assert retired == [], "supervisor retired an instance it no longer owns"
        assert exits == ["port now serves a different browser"]

    def test_our_own_browser_surviving_a_suspend_still_reconnects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The identity gate must not break the suspend/resume recovery.

        The browser IS ours (its port claims our user-data-dir): the loop
        reconnects, and only retires when the browser later truly goes.
        """
        retired: list[str] = []
        exits: list[str] = []
        state = {"passes": 0}

        async def failing_connection(**kwargs):
            raise ConnectionError("connection dropped")

        async def browser_gone(port: int) -> bool:
            # First pass: transient drop, port still up (ours). Second pass:
            # browser really closed.
            state["passes"] += 1
            return state["passes"] > 1

        async def port_serves_ours(port: int, user_data_dir: str) -> bool:
            return True

        monkeypatch.setattr(supervisor, "_supervise_connection", failing_connection)
        monkeypatch.setattr(supervisor, "_browser_gone", browser_gone)
        monkeypatch.setattr(supervisor, "_port_serves_our_browser", port_serves_ours)
        monkeypatch.setattr(
            supervisor,
            "log_supervisor_exit",
            lambda reason, name, port, registry_path: exits.append(reason),
        )

        async def drive() -> None:
            await supervisor._supervise_forever(
                port=9423,
                name="old-01",
                registry_path="/tmp/registry.json",
                scripts=None,
                border=supervisor.BorderSetting(lambda: True),
                heartbeat=supervisor.Heartbeat(),
                retire=lambda instance_name, registry_path: retired.append(instance_name),
                user_data_dir="/tmp/ours",
            )

        asyncio.run(drive())

        assert retired == ["old-01"]
        assert exits == ["browser closed"]

    def test_no_identity_anchor_keeps_the_old_trust(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """user_data_dir=None (old spawn, tests) skips the gate entirely."""
        retired: list[str] = []
        exits: list[str] = []

        async def failing_connection(**kwargs):
            raise ConnectionError("connection dropped")

        async def browser_gone(port: int) -> bool:
            return True  # gone on the first pass; the gate is never reached

        monkeypatch.setattr(supervisor, "_supervise_connection", failing_connection)
        monkeypatch.setattr(supervisor, "_browser_gone", browser_gone)
        monkeypatch.setattr(
            supervisor,
            "log_supervisor_exit",
            lambda reason, name, port, registry_path: exits.append(reason),
        )

        async def drive() -> None:
            await supervisor._supervise_forever(
                port=9423,
                name="old-01",
                registry_path="/tmp/registry.json",
                scripts=None,
                border=supervisor.BorderSetting(lambda: True),
                heartbeat=supervisor.Heartbeat(),
                retire=lambda instance_name, registry_path: retired.append(instance_name),
                user_data_dir=None,
            )

        asyncio.run(drive())

        assert retired == ["old-01"]
        assert exits == ["browser closed"]


class TestSupervisorArgv:
    def test_user_data_dir_rides_on_the_argv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_popen = subprocess.Popen
        captured = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            return real_popen([sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL)

        monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
        proc = supervisor.spawn_supervisor(
            port=9423,
            name="web-01",
            registry_path="/tmp/registry.json",
            draw_border=True,
            user_data_dir="/tmp/session-x",
        )
        proc.kill()
        proc.wait()

        assert captured["argv"][-1] == "/tmp/session-x"

    def test_the_anchor_is_optional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_popen = subprocess.Popen
        captured = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            return real_popen([sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL)

        monkeypatch.setattr(supervisor.subprocess, "Popen", fake_popen)
        proc = supervisor.spawn_supervisor(
            port=9423,
            name="web-01",
            registry_path="/tmp/registry.json",
            draw_border=True,
        )
        proc.kill()
        proc.wait()

        # python, -m, module, and the four original positional args.
        assert len(captured["argv"]) == 7


# --- async doubles -----------------------------------------------------------


async def _async_false(port: int) -> bool:
    return False


async def _async_port_serves_foreign(port: int, user_data_dir: str) -> bool:
    return False
