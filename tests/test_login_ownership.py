"""The ownership boundary: recorded row, process identity, connected browser.

Unit tests with the process utilities mocked (the recorded PIDs are
deliberately synthetic); the live module proves the same checks against a
disposable Chrome, and test_login_browser.py proves the transaction refuses
before any credential when the boundary says no.

The fourth-round regressions here pin the budget contract: the recorded
inspection and the post-reply recheck draw their subprocess budgets from
the one operation deadline (checked before and after each fact), the
recheck runs off the event loop so the loop stays responsive, its helper
children settle inside the budget, and the row's port is validated and
carried rather than re-resolved through the liveness ladder.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from browser_tools import login_contract, login_ownership, process_utils
from browser_tools.core.errors import CDPError
from browser_tools.lifecycle import ExtendedInstance

PID = 4242
START = "START-TOKEN"
DATA_DIR = "/recorded/session-dir"
PORT = 9500


def entry(**overrides: object) -> ExtendedInstance:
    fields: dict[str, object] = {
        "name": "web-01",
        "port": PORT,
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
        "port": PORT,
    }
    fields.update(overrides)
    return login_ownership.OwnershipSnapshot(**fields)  # type: ignore[arg-type]


@pytest.fixture
def live_identity(monkeypatch):
    """The process table says: our process, same start token, holds the dir."""

    def token(pid: int, timeout: float = 5.0) -> str | None:
        return START

    monkeypatch.setattr(login_ownership, "process_is_ours", lambda **kwargs: True)
    monkeypatch.setattr(login_ownership, "process_start_token", token)
    monkeypatch.setattr(
        login_ownership, "pid_holds_user_data_dir",
        lambda pid, path, timeout=2.0: True,
    )


class TestVerifyRecordedOwner:
    def test_a_positive_row_returns_the_snapshot(self, live_identity):
        result = login_ownership.verify_recorded_owner(
            entry(), login_contract.Deadline(30_000)
        )
        assert result == snapshot()

    def test_the_validated_port_is_carried_for_the_login(self, live_identity):
        result = login_ownership.verify_recorded_owner(
            entry(port=8_001), login_contract.Deadline(30_000)
        )
        assert result.port == 8_001

    def test_an_unnamed_launch_passes_the_same_checks(self, live_identity):
        """The named-profile label is not ownership evidence: a Chrome
        launched without --profile records its throwaway session directory
        exactly as a profile-bound one records its profile directory."""
        result = login_ownership.verify_recorded_owner(
            entry(profile=None), login_contract.Deadline(30_000)
        )
        assert result == snapshot()

    def test_the_identity_is_actually_checked(self, monkeypatch):
        """The fixture's mocks are not decoration: each fact is required."""
        monkeypatch.setattr(login_ownership, "process_is_ours", lambda **kwargs: False)
        monkeypatch.setattr(login_ownership, "process_start_token", lambda pid, timeout=5.0: START)
        monkeypatch.setattr(
            login_ownership, "pid_holds_user_data_dir", lambda pid, path, timeout=2.0: True
        )
        with pytest.raises(login_ownership.OwnershipError):
            login_ownership.verify_recorded_owner(entry(), login_contract.Deadline(30_000))

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
            login_ownership.verify_recorded_owner(
                entry(**overrides), login_contract.Deadline(30_000)
            )

    @pytest.mark.parametrize("port, message", [
        (0, "no usable debug port"),
        (-1, "no usable debug port"),
        (65_536, "no usable debug port"),
        (True, "no usable debug port"),
        ("9500", "no usable debug port"),
        (None, "no usable debug port"),
        ([9500], "no usable debug port"),
    ])
    def test_an_unusable_recorded_port_refuses(self, live_identity, port, message):
        """The row's port is validated with the ownership fields and carried
        on the snapshot; the login never re-resolves it through the generic
        liveness lookup."""
        with pytest.raises(login_ownership.OwnershipError, match=message):
            login_ownership.verify_recorded_owner(
                entry(port=port), login_contract.Deadline(30_000)
            )

    @pytest.mark.parametrize("token_value, message", [
        ("DIFFERENT-TOKEN", "registry row is stale"),
        (None, "registry row is stale"),
    ])
    def test_a_recycled_or_foreign_pid_refuses(
        self, monkeypatch, token_value, message
    ):
        monkeypatch.setattr(login_ownership, "process_is_ours", lambda **kwargs: True)
        monkeypatch.setattr(
            login_ownership, "process_start_token",
            lambda pid, timeout=5.0: token_value,
        )
        monkeypatch.setattr(
            login_ownership, "pid_holds_user_data_dir", lambda pid, path, timeout=2.0: True
        )
        with pytest.raises(login_ownership.OwnershipError, match=message):
            login_ownership.verify_recorded_owner(entry(), login_contract.Deadline(30_000))

    def test_an_undeterminable_start_time_refuses(self, monkeypatch):
        """process_is_ours passes a missing token through; ownership needs it."""
        monkeypatch.setattr(login_ownership, "process_is_ours", lambda **kwargs: True)
        monkeypatch.setattr(
            login_ownership, "process_start_token", lambda pid, timeout=5.0: None
        )
        monkeypatch.setattr(
            login_ownership, "pid_holds_user_data_dir", lambda pid, path, timeout=2.0: True
        )
        with pytest.raises(login_ownership.OwnershipError, match="registry row is stale"):
            login_ownership.verify_recorded_owner(entry(), login_contract.Deadline(30_000))

    @pytest.mark.parametrize("pid", [0, -1, True, "4242", None])
    def test_an_unusable_pid_refuses_without_touching_the_process_table(
        self, monkeypatch, pid
    ):
        def explode(*args, **kwargs):
            raise AssertionError("the process table must not be consulted")

        monkeypatch.setattr(login_ownership, "process_is_ours", explode)
        monkeypatch.setattr(login_ownership, "process_start_token", explode)
        monkeypatch.setattr(login_ownership, "pid_holds_user_data_dir", explode)
        with pytest.raises(login_ownership.OwnershipError, match="registry row is stale"):
            login_ownership.verify_recorded_owner(
                entry(pid=pid), login_contract.Deadline(30_000)
            )

    def test_an_unheld_directory_refuses(self, monkeypatch, live_identity):
        monkeypatch.setattr(login_ownership, "process_is_ours", lambda **kwargs: True)
        monkeypatch.setattr(
            login_ownership, "process_start_token", lambda pid, timeout=5.0: START
        )
        monkeypatch.setattr(
            login_ownership, "pid_holds_user_data_dir", lambda pid, path, timeout=2.0: False
        )
        with pytest.raises(login_ownership.OwnershipError, match="user-data directory"):
            login_ownership.verify_recorded_owner(entry(), login_contract.Deadline(30_000))

    def test_the_start_token_is_read_exactly_once(self, monkeypatch):
        """The redundant second read is gone: one existence signal check and
        one token read prove the recorded identity."""
        reads = []

        def token(pid: int, timeout: float = 5.0) -> str | None:
            reads.append(pid)
            return START

        monkeypatch.setattr(login_ownership, "process_is_ours", lambda **kwargs: True)
        monkeypatch.setattr(login_ownership, "process_start_token", token)
        monkeypatch.setattr(
            login_ownership, "pid_holds_user_data_dir", lambda pid, path, timeout=2.0: True
        )
        login_ownership.verify_recorded_owner(entry(), login_contract.Deadline(30_000))
        assert reads == [PID]


class TestRecordedOwnerBudget:
    """The one operation deadline bounds the whole recorded inspection."""

    @pytest.fixture
    def slow_metadata(self, monkeypatch):
        """A metadata reader that honours the timeout its caller passes,
        the way a bounded subprocess call does: it never works longer than
        its budget, and records every budget it was given."""
        budgets: dict[str, list[float]] = {"token": [], "holding": []}

        def token(pid: int, timeout: float = 5.0) -> str | None:
            budgets["token"].append(timeout)
            return START

        def holding(pid: int, path: Path, timeout: float = 2.0) -> bool:
            budgets["holding"].append(timeout)
            time.sleep(min(0.4, timeout))
            return True

        monkeypatch.setattr(login_ownership, "process_is_ours", lambda **kwargs: True)
        monkeypatch.setattr(login_ownership, "process_start_token", token)
        monkeypatch.setattr(login_ownership, "pid_holds_user_data_dir", holding)
        return budgets

    def test_slow_metadata_cannot_outlive_the_deadline(self, slow_metadata):
        """Each process fact runs on the remaining budget, shrunk from the
        utilities' own caps and never widened past them."""
        started = time.monotonic()
        with pytest.raises(login_contract.DeadlineExpiredError, match="timeout"):
            login_ownership.verify_recorded_owner(
                entry(), login_contract.Deadline(300)
            )
        elapsed = time.monotonic() - started
        # Green sits near the 300 ms budget; the pre-fix code ran the
        # reader's own 2 s cap instead (measured 0.5 s+ in the red proof).
        assert elapsed < 0.45, (
            f"the metadata inspection ran {elapsed:.3f}s under a 300 ms "
            "operation deadline"
        )
        assert slow_metadata["holding"], "the directory hold was never consulted"
        assert all(b <= 2.0 for b in slow_metadata["holding"]), slow_metadata
        assert all(b <= 5.0 for b in slow_metadata["token"]), slow_metadata

    @pytest.mark.parametrize("fact", ["token", "directory"])
    def test_a_metadata_timeout_is_not_a_stale_owner_diagnosis(self, monkeypatch, fact):
        """An unreadable fact after its budget expires is an operation timeout."""
        def token(pid, timeout=5.0):
            if fact == "token":
                time.sleep(timeout)
                return None
            return START

        def holding(pid, path, timeout=2.0):
            time.sleep(timeout)
            return False

        monkeypatch.setattr(login_ownership, "process_is_ours", lambda **kwargs: True)
        monkeypatch.setattr(login_ownership, "process_start_token", token)
        monkeypatch.setattr(login_ownership, "pid_holds_user_data_dir", holding)
        with pytest.raises(login_contract.DeadlineExpiredError, match="timeout"):
            login_ownership.verify_recorded_owner(entry(), login_contract.Deadline(50))

    def test_the_budget_shrinks_each_fact_below_its_cap(self, monkeypatch):
        budgets: list[float] = []

        def holding(pid: int, path: Path, timeout: float = 2.0) -> bool:
            budgets.append(timeout)
            return True

        monkeypatch.setattr(login_ownership, "process_is_ours", lambda **kwargs: True)
        monkeypatch.setattr(
            login_ownership, "process_start_token", lambda pid, timeout=5.0: START
        )
        monkeypatch.setattr(login_ownership, "pid_holds_user_data_dir", holding)
        login_ownership.verify_recorded_owner(
            entry(), login_contract.Deadline(500)
        )
        assert budgets and all(0 < b <= 0.5 for b in budgets), budgets

    def test_an_expired_clock_refuses_before_any_process_fact(self, monkeypatch):
        """Expiry prevents any further field read: no subprocess budget is
        spent once the operation deadline is gone."""
        from browser_tools import login_contract as contract

        deadline = contract.Deadline(30_000)
        deadline._ends_at = time.monotonic() - 1  # pyright: ignore[reportPrivateUsage]

        def explode(*args, **kwargs):
            raise AssertionError("an expired operation must not inspect processes")

        # The existence signal is a bare os.kill (no subprocess budget); the
        # token and directory reads are the facts that must never run.
        monkeypatch.setattr(login_ownership, "process_is_ours", lambda **kwargs: True)
        monkeypatch.setattr(login_ownership, "process_start_token", explode)
        monkeypatch.setattr(login_ownership, "pid_holds_user_data_dir", explode)
        with pytest.raises(login_contract.DeadlineExpiredError, match="timeout"):
            login_ownership.verify_recorded_owner(entry(), deadline)


class TestDirectoryAdapterBudget:
    """The real directory-adapter chain honors the forwarded budget.

    Round-four T13: every other test here mocks ``pid_holds_user_data_dir``
    (the recorded PIDs are synthetic), so deleting the timeout forwarding
    inside the adapter survived the named relay proof. This regression runs
    the whole real chain - ``pid_holds_user_data_dir`` ->
    ``find_chrome_user_data_dir`` -> ``read_process_command`` ->
    ``subprocess.run`` - against one controlled fake ``ps`` found through
    ``PATH``; no ownership helper is mocked. The synthetic PID is never a
    real process and nothing inspects a real process's arguments.
    """

    @pytest.mark.skipif(
        not (Path("/bin/sh").is_file() and Path("/bin/sleep").is_file()),
        reason="POSIX shell utilities needed by the controlled fake ps",
    )
    def test_the_directory_command_runs_on_the_forwarded_budget(
        self, tmp_path, monkeypatch
    ):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        stage = tmp_path / "directory-command-ran"
        # A POSIX fake ps: record the stage and the controlled child's
        # own pid, then become one sleeping child (``exec`` keeps a single
        # PID, so the timeout kill of subprocess.run leaves no descendant
        # behind). POSIX shell only - no Python startup eating into the
        # 150 ms budget.
        script = "#!/bin/sh\n"
        script += 'if [ -n "${PS_WARMUP:-}" ]; then exit 0; fi\n'
        script += 'printf "%s\\n" "$$" > "' + str(stage) + '"\n'
        script += "exec /bin/sleep 0.6\n"
        (bin_dir / "ps").write_text(script)
        (bin_dir / "ps").chmod(0o755)
        monkeypatch.setenv(
            "PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        )
        # Establish executable readiness outside the measured budget.
        # Warmup touches neither the stage nor the timed measurement;
        # the 150 ms budget and 600 ms delay below stay unchanged. This
        # does not establish why the round-four witness missed its stage.
        monkeypatch.setenv("PS_WARMUP", "1")
        subprocess.run([str(bin_dir / "ps")], check=True, timeout=5.0)
        monkeypatch.delenv("PS_WARMUP")

        started = time.monotonic()
        holds = process_utils.pid_holds_user_data_dir(
            PID, Path(DATA_DIR), timeout=0.15
        )
        elapsed = time.monotonic() - started

        # The chain really reached its directory command and can report
        # only the controlled child's pid: without this marker an early
        # failure (import, spawn or call-shape change) could fake a fast
        # green.
        assert stage.is_file(), (
            "the directory-command stage marker is missing: the adapter "
            "never ran its ps command"
        )
        recorded = stage.read_text().split()
        assert len(recorded) == 1 and recorded[0].isdigit()
        # The predicate failed closed promptly on its 150 ms forwarded
        # budget and its one child was reaped; without the forwarding the
        # adapter uses the nested 2 s default, the fake's 600 ms delay
        # runs to completion, and this assertion fails (measured ~0.6 s).
        assert holds is False
        with pytest.raises(OSError):
            os.kill(int(recorded[0]), 0)
        assert elapsed < 0.4, (
            f"the directory command took {elapsed:.3f}s under a 150 ms "
            "forwarded budget; the adapter fell back to its nested default"
        )


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
        await login_ownership.verify_connected_browser(
            cdp, snapshot(), login_contract.Deadline(30_000)
        )
        assert [item[0] for item in cdp.sent] == ["SystemInfo.getProcessInfo"]

    async def test_a_different_browser_pid_refuses(self, live_identity):
        cdp = FakeCDP(browser_reply(pid=9999))
        with pytest.raises(login_ownership.OwnershipError, match="not the registered instance"):
            await login_ownership.verify_connected_browser(
                cdp, snapshot(), login_contract.Deadline(30_000)
            )

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
            await login_ownership.verify_connected_browser(
                cdp, snapshot(), login_contract.Deadline(30_000)
            )
        assert "SENTINEL" not in str(error.value)

    async def test_an_unavailable_command_refuses_without_its_text(self, live_identity):
        cdp = FakeCDP(CDPError(-32000, "SENTINEL-protocol-burst"))
        with pytest.raises(
            login_ownership.OwnershipError, match="did not report its process identity"
        ) as error:
            await login_ownership.verify_connected_browser(
                cdp, snapshot(), login_contract.Deadline(30_000)
            )
        assert "SENTINEL" not in str(error.value)
        assert error.value.__cause__ is None

    async def test_a_query_that_exhausts_the_budget_is_the_operation_timeout(
        self, live_identity
    ):
        """Round-four B7: deadline-expired outranks identity-unavailable.

        A transport whose query runs past the operation's remaining budget
        must surface as the operation timeout, never as a browser that
        "did not report its process identity" - the budget fact is the
        stronger, earlier one, and its fixed text already exists.
        """

        class ExhaustedTransport:
            async def send(self, method, params=None, session_id=None, timeout=None):
                await asyncio.sleep(0.18)  # outlives the 120 ms budget below
                raise TimeoutError

        with pytest.raises(
            login_contract.DeadlineExpiredError, match="timeout"
        ) as error:
            await login_ownership.verify_connected_browser(
                ExhaustedTransport(), snapshot(), login_contract.Deadline(120)
            )
        assert "identity" not in str(error.value)

    async def test_a_query_timeout_with_budget_left_is_identity_unavailability(
        self, live_identity
    ):
        """The other side of the same boundary: a query that fails while
        budget remains is one operational fact - the browser could not be
        asked - and keeps the identity-unavailable refusal."""

        class TimingOutTransport:
            async def send(self, method, params=None, session_id=None, timeout=None):
                raise TimeoutError

        with pytest.raises(
            login_ownership.OwnershipError, match="did not report its process identity"
        ) as error:
            await login_ownership.verify_connected_browser(
                TimingOutTransport(), snapshot(), login_contract.Deadline(30_000)
            )
        assert error.value.__cause__ is None

    async def test_the_identity_is_reproven_after_the_reply(self, monkeypatch):
        """A browser that died between registry read and reply must refuse."""
        monkeypatch.setattr(login_ownership, "process_is_ours", lambda **kwargs: True)
        monkeypatch.setattr(
            login_ownership, "process_start_token",
            lambda pid, timeout=5.0: "LATER-TOKEN",
        )
        monkeypatch.setattr(
            login_ownership, "pid_holds_user_data_dir", lambda pid, path, timeout=2.0: True
        )
        cdp = FakeCDP(browser_reply())
        with pytest.raises(login_ownership.OwnershipError, match="registry row is stale"):
            await login_ownership.verify_connected_browser(
                cdp, snapshot(), login_contract.Deadline(30_000)
            )

    async def test_the_hold_check_receives_the_recorded_directory(self, monkeypatch):
        seen: list[tuple[object, object]] = []

        def holding(pid, path, timeout=2.0):
            seen.append((pid, path))
            return True

        monkeypatch.setattr(login_ownership, "process_is_ours", lambda **kwargs: True)
        monkeypatch.setattr(
            login_ownership, "process_start_token", lambda pid, timeout=5.0: START
        )
        monkeypatch.setattr(login_ownership, "pid_holds_user_data_dir", holding)
        cdp = FakeCDP(browser_reply())
        await login_ownership.verify_connected_browser(
            cdp, snapshot(), login_contract.Deadline(30_000)
        )
        assert seen == [(PID, Path(DATA_DIR))]


@pytest.mark.asyncio
class TestRecheckOffTheEventLoop:
    """The post-reply recheck must not block the receive/event loop, and its
    bounded subprocess work must settle inside the operation budget."""

    def _patch_slow(self, monkeypatch, *, holding) -> None:
        monkeypatch.setattr(login_ownership, "process_is_ours", lambda **kwargs: True)
        monkeypatch.setattr(
            login_ownership, "process_start_token",
            lambda pid, timeout=5.0: START,
        )
        monkeypatch.setattr(login_ownership, "pid_holds_user_data_dir", holding)

    async def test_a_ticker_stays_responsive_during_the_recheck(self, monkeypatch):
        gaps: list[float] = []
        stop = asyncio.Event()

        async def ticker() -> None:
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        self._patch_slow(monkeypatch, holding=lambda pid, path, timeout=2.0: (
            time.sleep(0.3) or True
        ))
        cdp = FakeCDP(browser_reply())
        task = asyncio.ensure_future(ticker())
        try:
            await login_ownership.verify_connected_browser(
                cdp, snapshot(), login_contract.Deadline(30_000)
            )
        finally:
            stop.set()
            await task
        assert gaps, "the ticker never ran"
        assert max(gaps) < 0.15, (
            f"the event loop stalled {max(gaps):.3f}s during the post-reply "
            "ownership recheck"
        )

    async def test_recheck_helper_children_settle_inside_the_budget(
        self, monkeypatch
    ):
        seen: dict[str, object] = {}

        def holding(pid: int, path: Path, timeout: float = 2.0) -> bool:
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
            )
            seen["child"] = child
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            return True

        self._patch_slow(monkeypatch, holding=holding)
        cdp = FakeCDP(browser_reply())
        started = time.monotonic()
        with pytest.raises(login_contract.DeadlineExpiredError, match="timeout"):
            # The synthetic child outlives any honest operation budget, so
            # the recheck refuses inside the 400 ms deadline - never waits
            # for the helper's own 2 s cap.
            await login_ownership.verify_connected_browser(
                cdp, snapshot(), login_contract.Deadline(400)
            )
        elapsed = time.monotonic() - started
        child = seen["child"]
        assert child.poll() is not None, "a helper child survived the recheck"
        assert elapsed < 0.8, (
            f"the recheck took {elapsed:.3f}s; its helper budgets sat outside "
            "the operation deadline"
        )

    async def test_an_abandoned_recheck_leaves_no_late_exception(
        self, monkeypatch, caplog
    ):
        """Cancelling the await must not surface a late exception from the
        executor thread; the thread finishes on its capped budget and its
        result is simply discarded."""
        import logging

        def holding(pid: int, path: Path, timeout: float = 2.0) -> bool:
            deadline = time.monotonic() + min(0.3, timeout)
            while time.monotonic() < deadline:
                time.sleep(0.02)
            return False  # a refusal, arriving after the await gave up

        self._patch_slow(monkeypatch, holding=holding)
        cdp = FakeCDP(browser_reply())

        async def scenario() -> None:
            task = asyncio.ensure_future(
                login_ownership.verify_connected_browser(
                    cdp, snapshot(), login_contract.Deadline(30_000)
                )
            )
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # Let the thread finish inside the still-live loop; its refusal
            # must be discarded, never raised or logged.
            await asyncio.sleep(0.4)

        with caplog.at_level(logging.DEBUG):
            await scenario()
        assert "never retrieved" not in caplog.text
        assert "OwnershipError" not in caplog.text
