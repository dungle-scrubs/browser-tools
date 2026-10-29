"""The ownership boundary: recorded row, process identity, connected browser.

Unit tests with the process utilities mocked (the recorded PIDs are
deliberately synthetic); the live module proves the same checks against a
disposable Chrome, and test_login_browser.py proves the transaction refuses
before any credential when the boundary says no.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from browser_tools import login_ownership
from browser_tools.core.errors import CDPError
from browser_tools.lifecycle import ExtendedInstance

PID = 4242
START = "START-TOKEN"
DATA_DIR = "/recorded/session-dir"


def entry(**overrides: object) -> ExtendedInstance:
    fields: dict[str, object] = {
        "name": "web-01",
        "port": 9500,
        "pid": PID,
        "browser_version": "Chrome/1",
        "user_data_dir": DATA_DIR,
        "launched": None,
        "pid_start": START,
        "engine": "chrome",
        "profile": "web-profile",
    }
    fields.update(overrides)
    return ExtendedInstance(**fields)  # type: ignore[arg-type]


def snapshot(**overrides: object) -> login_ownership.OwnershipSnapshot:
    fields: dict[str, object] = {
        "pid": PID,
        "pid_start": START,
        "user_data_dir": DATA_DIR,
    }
    fields.update(overrides)
    return login_ownership.OwnershipSnapshot(**fields)  # type: ignore[arg-type]


@pytest.fixture
def live_identity(monkeypatch):
    """The process table says: our process, same start token, holds the dir."""

    def holds(candidate: login_ownership.OwnershipSnapshot) -> None:
        assert candidate.pid == PID and candidate.pid_start == START

    monkeypatch.setattr(login_ownership, "process_is_ours", lambda pid, expected_start: True)
    monkeypatch.setattr(login_ownership, "process_start_time", lambda pid: START)
    monkeypatch.setattr(login_ownership, "pid_holds_user_data_dir", lambda pid, path: True)
    return holds


class TestVerifyRecordedOwner:
    def test_a_positive_row_returns_the_snapshot(self, live_identity):
        result = login_ownership.verify_recorded_owner(entry())
        assert result == snapshot()

    def test_an_unnamed_launch_passes_the_same_checks(self, live_identity):
        """The named-profile label is not ownership evidence: a Chrome
        launched without --profile records its throwaway session directory
        exactly as a profile-bound one records its profile directory."""
        result = login_ownership.verify_recorded_owner(entry(profile=None))
        assert result == snapshot()

    def test_the_identity_is_actually_checked(self, monkeypatch):
        """The fixture's mocks are not decoration: each fact is required."""
        monkeypatch.setattr(login_ownership, "process_is_ours", lambda pid, expected_start: False)
        with pytest.raises(login_ownership.OwnershipError):
            login_ownership.verify_recorded_owner(entry())

    @pytest.mark.parametrize("overrides, message", [
        ({"pid_start": None}, "start-time identity"),
        ({"pid_start": ""}, "start-time identity"),
        ({"pid_start": 7}, "start-time identity"),
        ({"user_data_dir": None}, "no usable user-data directory"),
        ({"user_data_dir": ""}, "no usable user-data directory"),
        ({"user_data_dir": 5}, "no usable user-data directory"),
        ({"user_data_dir": ["/recorded"]}, "no usable user-data directory"),
        ({"user_data_dir": {"dir": "/recorded"}}, "no usable user-data directory"),
    ])
    def test_missing_or_malformed_evidence_refuses_fixed(
        self, live_identity, overrides, message
    ):
        with pytest.raises(login_ownership.OwnershipError, match=message):
            login_ownership.verify_recorded_owner(entry(**overrides))

    @pytest.mark.parametrize("is_ours, start_time, message", [
        (False, START, "registry row is stale"),
        (True, "DIFFERENT-TOKEN", "registry row is stale"),
    ])
    def test_a_recycled_or_foreign_pid_refuses(
        self, monkeypatch, is_ours, start_time, message
    ):
        monkeypatch.setattr(login_ownership, "process_is_ours", lambda pid, expected_start: is_ours)
        monkeypatch.setattr(login_ownership, "process_start_time", lambda pid: start_time)
        monkeypatch.setattr(login_ownership, "pid_holds_user_data_dir", lambda pid, path: True)
        with pytest.raises(login_ownership.OwnershipError, match=message):
            login_ownership.verify_recorded_owner(entry())

    def test_an_undeterminable_start_time_refuses(self, monkeypatch):
        """process_is_ours passes None through; ownership needs the token."""
        monkeypatch.setattr(login_ownership, "process_is_ours", lambda pid, expected_start: True)
        monkeypatch.setattr(login_ownership, "process_start_time", lambda pid: None)
        monkeypatch.setattr(login_ownership, "pid_holds_user_data_dir", lambda pid, path: True)
        with pytest.raises(login_ownership.OwnershipError, match="registry row is stale"):
            login_ownership.verify_recorded_owner(entry())

    @pytest.mark.parametrize("pid", [0, -1, True, "4242", None])
    def test_an_unusable_pid_refuses_without_touching_the_process_table(
        self, monkeypatch, pid
    ):
        def explode(*args, **kwargs):
            raise AssertionError("the process table must not be consulted")

        monkeypatch.setattr(login_ownership, "process_is_ours", explode)
        monkeypatch.setattr(login_ownership, "process_start_time", explode)
        monkeypatch.setattr(login_ownership, "pid_holds_user_data_dir", explode)
        with pytest.raises(login_ownership.OwnershipError, match="registry row is stale"):
            login_ownership.verify_recorded_owner(entry(pid=pid))

    def test_an_unheld_directory_refuses(self, monkeypatch, live_identity):
        monkeypatch.setattr(login_ownership, "process_is_ours", lambda pid, expected_start: True)
        monkeypatch.setattr(login_ownership, "process_start_time", lambda pid: START)
        monkeypatch.setattr(login_ownership, "pid_holds_user_data_dir", lambda pid, path: False)
        with pytest.raises(login_ownership.OwnershipError, match="user-data directory"):
            login_ownership.verify_recorded_owner(entry())


class FakeCDP:
    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.sent: list[tuple[str, object, object]] = []

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.sent.append((method, params, session_id))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def browser_reply(pid: object = PID, *, extra_browser: bool = False) -> dict:
    processes = [{"type": "browser", "id": pid, "cpuTime": 0.5}]
    if extra_browser:
        processes.append({"type": "browser", "id": 9999, "cpuTime": 0.1})
    processes.append({"type": "renderer", "id": 5150, "cpuTime": 1.0})
    return {"processInfo": processes}


@pytest.mark.asyncio
class TestVerifyConnectedBrowser:
    async def test_a_matching_single_browser_process_passes(self, live_identity):
        cdp = FakeCDP(browser_reply())
        await login_ownership.verify_connected_browser(cdp, snapshot(), timeout=5.0)
        assert cdp.sent == [("SystemInfo.getProcessInfo", {}, None)]

    async def test_a_different_browser_pid_refuses(self, live_identity):
        cdp = FakeCDP(browser_reply(pid=9999))
        with pytest.raises(login_ownership.OwnershipError, match="not the registered instance"):
            await login_ownership.verify_connected_browser(cdp, snapshot(), timeout=5.0)

    @pytest.mark.parametrize("reply", [
        browser_reply(extra_browser=True),
        {"processInfo": [{"type": "renderer", "id": PID, "cpuTime": 0.0}]},
        {"processInfo": [{"type": "browser", "id": True, "cpuTime": 0.0}]},
        {"processInfo": [{"type": "browser", "id": 0, "cpuTime": 0.0}]},
        {"processInfo": [{"type": "browser", "id": -1, "cpuTime": 0.0}]},
        {"processInfo": [{"type": "browser", "id": "4242", "cpuTime": 0.0}]},
        {"processInfo": None},
        {},
        {"processInfo": "SENTINEL-not-a-list"},
        {"unexpected": "SENTINEL-shape"},
    ])
    async def test_malformed_replies_refuse_without_echoing_them(
        self, live_identity, reply
    ):
        cdp = FakeCDP(reply)
        with pytest.raises(
            login_ownership.OwnershipError, match="did not report its process identity"
        ) as error:
            await login_ownership.verify_connected_browser(cdp, snapshot(), timeout=5.0)
        assert "SENTINEL" not in str(error.value)

    async def test_an_unavailable_command_refuses_without_its_text(self, live_identity):
        cdp = FakeCDP(CDPError(-32000, "SENTINEL-protocol-burst"))
        with pytest.raises(
            login_ownership.OwnershipError, match="did not report its process identity"
        ) as error:
            await login_ownership.verify_connected_browser(cdp, snapshot(), timeout=5.0)
        assert "SENTINEL" not in str(error.value)
        assert error.value.__cause__ is None

    async def test_the_identity_is_reproven_after_the_reply(self, monkeypatch):
        """A browser that died between registry read and reply must refuse."""
        monkeypatch.setattr(login_ownership, "process_is_ours", lambda pid, expected_start: True)
        monkeypatch.setattr(login_ownership, "process_start_time", lambda pid: "LATER-TOKEN")
        monkeypatch.setattr(login_ownership, "pid_holds_user_data_dir", lambda pid, path: True)
        cdp = FakeCDP(browser_reply())
        with pytest.raises(login_ownership.OwnershipError, match="registry row is stale"):
            await login_ownership.verify_connected_browser(cdp, snapshot(), timeout=5.0)

    async def test_the_hold_check_receives_the_recorded_directory(self, monkeypatch):
        seen: list[tuple[object, object]] = []

        def holding(pid, path):
            seen.append((pid, path))
            return True

        monkeypatch.setattr(login_ownership, "process_is_ours", lambda pid, expected_start: True)
        monkeypatch.setattr(login_ownership, "process_start_time", lambda pid: START)
        monkeypatch.setattr(login_ownership, "pid_holds_user_data_dir", holding)
        cdp = FakeCDP(browser_reply())
        await login_ownership.verify_connected_browser(cdp, snapshot(), timeout=5.0)
        assert seen == [(PID, Path(DATA_DIR))]
