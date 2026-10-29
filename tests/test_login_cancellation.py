"""Live cancellation of the async credential read, against a fake opchain.

The regression pinned here: cancelling the coroutine that awaits a relay
read must stop the relay child *for real*. Before the fix, asyncio
cancellation cancelled only the ``asyncio.to_thread`` waiter: the worker
thread, the opchain process, and its group kept running to their own
deadline, and a sentinel delivered mid-flight could still be consumed.

Every child is a synthetic ``opchain`` that records its pid and either
sleeps (live cancellation) or answers one field and sleeps on the other.
No real opchain, real credential, or real 1Password access happens here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import textwrap
import time
from typing import TYPE_CHECKING

import pytest

from browser_tools import login, login_policy

if TYPE_CHECKING:
    from pathlib import Path

VAULT = "dqss6l4yprxhntxymzjaltchwe"
ITEM = "bbbbbbbbbbbbbbbbbbbbbbbbbb"

POLICY = login_policy.LoginPolicy(identity="browser", vault=VAULT, accounts={})
ACCOUNT = login_policy.AccountSpec(
    alias="example",
    item=ITEM,
    origin="https://example.com",
    username_selector="#username",
    password_selector="#password",
    submit_selector="button[type=submit]",
    success_selector="[data-signed-in]",
)

#: The sentinel the fake writes into the relay pipe before it sleeps. If a
#: cancelled read ever returned a value, this is the value it would return.
SENTINEL_USER = "SENTINEL-user\n"

POLICY_DOCUMENT = {
    "version": 1,
    "identity": "browser",
    "vault": VAULT,
    "accounts": {
        "example": {
            "item": ITEM,
            "origin": "https://example.com",
            "username_selector": "#username",
            "password_selector": "#password",
            "submit_selector": "button[type=submit]",
            "success_selector": "[data-signed-in]",
        }
    },
}


def write_fake(bin_dir: Path, name: str, body: str) -> Path:
    path = bin_dir / name
    path.write_text(textwrap.dedent(body).lstrip("\n"))
    path.chmod(0o755)
    return path


def sleeping_relay_body(*, marker: str, hang_fields: set[str]) -> str:
    """A relay-capable fake: answers the probe, hangs on ``hang_fields``.

    Each invocation appends its pid to the marker file so the test can
    both find the live process group and count how many reads started.
    """
    return f'''
        #!/usr/bin/env python3
        import os, sys, time
        argv = sys.argv
        fd_text = argv[argv.index("--value-fd") + 1]
        if fd_text == "0":
            sys.stderr.write("opchain: relay: descriptor-standard\\n")
            sys.exit(1)
        ref = argv[argv.index("op") + 2]
        field = ref.rsplit("/", 1)[-1]
        with open({marker!r}, "a") as fh:
            fh.write(str(os.getpid()) + "\\n")
        if field in {sorted(hang_fields)!r}:
            time.sleep(30)
            sys.exit(0)
        os.write(int(fd_text), {SENTINEL_USER.encode()!r})
        sys.exit(0)
        '''


async def _wait_for_pids(marker: Path, count: int, timeout: float = 10.0) -> list[int]:
    """Pids recorded by the fake, awaited without blocking the loop."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        text = marker.read_text() if marker.exists() else ""
        pids = [int(line) for line in text.splitlines() if line.strip()]
        if len(pids) >= count:
            return pids
        await asyncio.sleep(0.02)
    pytest.fail(f"the fake opchain never started {count} read(s); marker={marker}")


def _assert_groups_dead(pids: list[int], timeout: float = 8.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if all(_dead(pid) for pid in pids):
            return
        time.sleep(0.05)
    pytest.fail(f"process group(s) survived cancellation: {pids}")


def _dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return False
    except OSError:
        return True


@pytest.fixture
def fake_relay(tmp_path) -> Path:
    marker = tmp_path / "pids.log"
    return write_fake(
        tmp_path, "opchain", sleeping_relay_body(marker=str(marker), hang_fields={"username"})
    )


class TestLiveCancellation:
    def test_cancel_during_a_live_read_kills_the_group_and_returns_nothing(
        self, fake_relay, tmp_path, capsys
    ):
        marker = tmp_path / "pids.log"

        async def scenario() -> float:
            deadline = login._Deadline(30_000)
            task = asyncio.ensure_future(
                login._read_credentials(str(fake_relay), POLICY, ACCOUNT, deadline)
            )
            pids = await _wait_for_pids(marker, count=1)
            task.cancel()
            started = time.monotonic()
            with pytest.raises(asyncio.CancelledError):
                await task
            return time.monotonic() - started

        elapsed = asyncio.run(scenario())
        # Prompt: bounded cleanup, not the fake's 30-second sleep, and not
        # the relay's own command deadline either.
        assert elapsed < 6.0, elapsed
        pids = [int(line) for line in marker.read_text().splitlines()]
        assert pids, "the fake opchain never started"
        _assert_groups_dead(pids)
        # Only the username read started; the password read never spawned.
        assert len(pids) == 1
        # No value was returned (CancelledError above) and none leaked.
        captured = capsys.readouterr()
        assert "SENTINEL" not in captured.out + captured.err

    def test_cancel_during_the_second_field_never_returns_the_first(
        self, tmp_path
    ):
        marker = tmp_path / "pids.log"
        binary = write_fake(
            tmp_path,
            "opchain",
            sleeping_relay_body(marker=str(marker), hang_fields={"password"}),
        )

        async def scenario() -> float:
            deadline = login._Deadline(30_000)
            task = asyncio.ensure_future(
                login._read_credentials(str(binary), POLICY, ACCOUNT, deadline)
            )
            pids = []
            while len(pids) < 2:
                pids = await _wait_for_pids(marker, count=2)
            task.cancel()
            started = time.monotonic()
            with pytest.raises(asyncio.CancelledError):
                await task
            return time.monotonic() - started

        elapsed = asyncio.run(scenario())
        assert elapsed < 6.0, elapsed
        pids = [int(line) for line in marker.read_text().splitlines()]
        assert len(pids) == 2, "both fields should have started"
        _assert_groups_dead(pids)

    def test_a_second_cancellation_during_cleanup_cannot_break_it(
        self, fake_relay, tmp_path
    ):
        marker = tmp_path / "pids.log"

        async def scenario() -> None:
            deadline = login._Deadline(30_000)
            task = asyncio.ensure_future(
                login._read_credentials(str(fake_relay), POLICY, ACCOUNT, deadline)
            )
            pids = await _wait_for_pids(marker, count=1)
            task.cancel()
            # Let the cleanup handler start, then hit it again mid-cleanup.
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        pids = [int(line) for line in marker.read_text().splitlines()]
        assert pids
        _assert_groups_dead(pids)


class TestOverallDeadlineWall:
    def test_a_hung_session_attach_is_bounded_by_the_deadline(self, monkeypatch):
        """The documented timeout covers session attach, with a grace."""

        @contextlib.asynccontextmanager
        async def hung(port, target_spec, target_by):
            await asyncio.Event().wait()  # never yields a session
            yield None, None

        monkeypatch.setattr(login, "one_shot_page_session", hung)
        deadline = login._Deadline(300)

        async def scenario() -> float:
            started = time.monotonic()
            await login._login_transaction(
                9222, None, None, POLICY, ACCOUNT, "/fake/opchain", deadline
            )
            return time.monotonic() - started

        with pytest.raises(login.LoginError, match="timeout"):
            asyncio.run(scenario())

    def test_the_probe_consumes_the_shared_deadline(self, monkeypatch, tmp_path):
        """Probe time counts against the total; the deadline never resets.

        A 300 ms budget with a probe that sleeps 250 ms must time out around
        300 ms in total. A reset after the probe would hand the browser phase
        a fresh 300 ms and finish near 550 ms instead.
        """
        from browser_tools import credential_relay
        from browser_tools.core import registry as core_registry

        monkeypatch.setattr(credential_relay, "find_opchain", lambda: "/fake/opchain")

        def slow_probe(*args, **kwargs):
            time.sleep(0.25)

        monkeypatch.setattr(credential_relay, "ensure_relay_supported", slow_probe)

        @contextlib.asynccontextmanager
        async def hung(port, target_spec, target_by):
            await asyncio.Event().wait()
            yield None, None

        monkeypatch.setattr(login, "one_shot_page_session", hung)

        config = tmp_path / "logins.json"
        config.write_text(json.dumps(POLICY_DOCUMENT))
        registry_path = str(tmp_path / "registry.json")
        core_registry._save_registry(  # pyright: ignore[reportPrivateUsage]
            {"web-01": {"port": 9500, "pid": 2_000_000_000,
                        "browser_version": "Chrome/1", "user_data_dir": "",
                        "engine": "chrome"}},
            registry_path,
        )
        started = time.monotonic()
        with pytest.raises(login.LoginError, match="timeout"):
            login.login(
                instance="web-01",
                account="example",
                config=str(config),
                timeout_ms=300,
                registry_path=registry_path,
            )
        assert time.monotonic() - started < 0.45
