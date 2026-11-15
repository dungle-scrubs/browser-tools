"""Idle expiry for managed browser instances (#195).

The clock is controlled and the durations are short, so nothing here waits an
hour to prove that an instance expires. The keeper is exercised in-process
through ``task_lease.watch_once``; the one live test drives a real headless
Chrome and a real keeper.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from idle_fixtures import (
    Capture,
    Clock,
    Launches,
    Owner,
    Patch,
    Stops,
    lease_value,
    owner_gone,
    write_lease,
)
from idle_fixtures import browser_double as browser_double
from idle_fixtures import clean_activity_state as clean_activity_state
from idle_fixtures import clock as clock
from idle_fixtures import owner as owner
from idle_fixtures import registry as registry
from idle_fixtures import stops as stops

from browser_tools import cli, idle, lifecycle, login, task_lease

# --------------------------------------------------------------------------- #
# The lease every launch gets
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("headless", [True, False])
def test_a_plain_launch_gets_a_lease_an_idle_timeout_and_a_keeper(
    registry: str,
    browser_double: Launches,
    headless: bool,
) -> None:
    instance = lifecycle.launch(name="plain", headless=headless, registry_path=registry)
    lease = lease_value(registry, instance.name)
    assert lease["idle_timeout"] == 3600
    assert lease["owner"] is None
    assert lease["state"] == "auto"
    assert lease["keeper"]["pid"]
    assert lease["operations"] == []
    assert lease["last_activity"] > 0


def test_the_status_block_reports_the_idle_state(registry: str, browser_double: Launches) -> None:
    instance = lifecycle.launch(name="plain", headless=True, registry_path=registry)
    reported = lifecycle.status(instance.name, registry)[0]["lease"]
    assert reported["owner"] == "none"
    assert reported["keeper"] == "running"
    assert reported["idle_timeout"] == 3600
    assert reported["last_activity"]
    assert reported["idle_deadline"]
    assert reported["idle_remaining"] > 0
    assert reported["active_operations"] == 0
    assert "close_reason" not in reported


def test_status_reports_no_deadline_for_a_retained_instance(
    registry: str,
    browser_double: Launches,
) -> None:
    """A retained instance does not idle-expire, so status shows no deadline."""
    instance = lifecycle.launch(name="kept", headless=True, registry_path=registry)
    lifecycle.retain(instance.name, registry_path=registry)
    reported = lifecycle.status(instance.name, registry)[0]["lease"]
    assert reported["state"] == "retained"
    assert reported["idle_deadline"] is None
    assert reported["idle_remaining"] is None


def test_a_legacy_lease_parses_and_is_never_idle_expired(
    registry: str,
    browser_double: Launches,
    stops: Stops,
    clock: Clock,
) -> None:
    instance = lifecycle.launch(name="legacy", headless=True, registry_path=registry)
    lease = lease_value(registry, instance.name)
    for key in ("idle_timeout", "last_activity", "operations"):
        lease.pop(key, None)
    write_lease(registry, instance.name, lease)
    clock.advance(10**6)
    assert task_lease.watch_once(instance.name, lease["generation"], registry)
    assert stops == []
    assert lease_value(registry, instance.name)["state"] == "auto"


def test_an_entry_without_a_lease_is_never_touched(
    registry: str,
    browser_double: Launches,
    stops: Stops,
    clock: Clock,
) -> None:
    instance = lifecycle.launch(name="plain", headless=True, registry_path=registry)
    raw = json.loads(Path(registry).read_text())
    del raw[instance.name]["lease"]
    Path(registry).write_text(json.dumps(raw))
    task_lease.begin(instance.name, registry)
    task_lease.finish()
    assert "lease" not in json.loads(Path(registry).read_text())[instance.name]
    assert not task_lease.watch_once(instance.name, "any-generation", registry)
    assert stops == []


# --------------------------------------------------------------------------- #
# The keeper's tick
# --------------------------------------------------------------------------- #


def test_the_keeper_closes_an_overdue_instance_for_idle(
    registry: str,
    browser_double: Launches,
    stops: Stops,
    clock: Clock,
) -> None:
    instance = lifecycle.launch(name="idle", headless=True, registry_path=registry)
    generation = lease_value(registry, instance.name)["generation"]
    clock.advance(3601)
    assert not task_lease.watch_once(instance.name, generation, registry)
    lease = lease_value(registry, instance.name)
    assert lease["state"] == "closing"
    assert lease["close_reason"] == "idle"
    assert [call["name"] for call in stops] == [instance.name]
    assert stops[0]["generation"] == generation


def test_waking_past_the_deadline_closes_the_instance(
    registry: str,
    browser_double: Launches,
    stops: Stops,
    clock: Clock,
) -> None:
    """A machine that slept through the deadline closes on wake.

    The deadline is wall-clock, so the jump is the sleep itself.
    """
    instance = lifecycle.launch(name="sleeper", headless=True, registry_path=registry)
    generation = lease_value(registry, instance.name)["generation"]
    clock.advance(8 * 3600)
    assert not task_lease.watch_once(instance.name, generation, registry)
    assert lease_value(registry, instance.name)["close_reason"] == "idle"


def test_a_live_operation_protects_the_instance(
    registry: str,
    browser_double: Launches,
    stops: Stops,
    clock: Clock,
) -> None:
    instance = lifecycle.launch(name="busy", headless=True, registry_path=registry)
    generation = lease_value(registry, instance.name)["generation"]
    task_lease.begin(instance.name, registry)
    clock.advance(3601)
    assert task_lease.watch_once(instance.name, generation, registry)
    assert stops == []
    assert lease_value(registry, instance.name)["state"] == "auto"


def test_a_gone_operation_is_pruned_and_restarts_the_countdown(
    registry: str,
    browser_double: Launches,
    stops: Stops,
    clock: Clock,
) -> None:
    instance = lifecycle.launch(name="crashed", headless=True, registry_path=registry)
    generation = lease_value(registry, instance.name)["generation"]
    lease = lease_value(registry, instance.name)
    lease["operations"] = [{"pid": 2000000000, "start": "1"}]
    write_lease(registry, instance.name, lease)
    clock.advance(3601)
    assert task_lease.watch_once(instance.name, generation, registry)
    value = lease_value(registry, instance.name)
    assert value["operations"] == []
    assert value["last_activity"] == clock.now
    assert stops == []


def test_an_unverified_operation_keeps_its_protection(
    registry: str,
    browser_double: Launches,
    stops: Stops,
    clock: Clock,
    monkeypatch: Patch,
) -> None:
    instance = lifecycle.launch(name="unsure", headless=True, registry_path=registry)
    generation = lease_value(registry, instance.name)["generation"]
    lease = lease_value(registry, instance.name)
    lease["operations"] = [{"pid": 2000000001, "start": "1"}]
    write_lease(registry, instance.name, lease)
    original = task_lease.observe
    monkeypatch.setattr(
        task_lease,
        "observe",
        lambda identity: "unverified" if identity.pid == 2000000001 else original(identity),
    )
    clock.advance(3601)
    assert task_lease.watch_once(instance.name, generation, registry)
    assert lease_value(registry, instance.name)["operations"]
    assert stops == []


def test_the_owner_still_closes_promptly(
    registry: str,
    browser_double: Launches,
    stops: Stops,
    clock: Clock,
    owner: Owner,
    monkeypatch: Patch,
) -> None:
    instance = lifecycle.launch(
        name="owned", headless=True, owner=owner.pid, registry_path=registry
    )
    generation = lease_value(registry, instance.name)["generation"]
    owner_gone(monkeypatch, owner.pid)
    assert not task_lease.watch_once(instance.name, generation, registry)
    lease = lease_value(registry, instance.name)
    assert lease["state"] == "closing"
    assert lease["close_reason"] == "owner"


def test_retain_stops_the_expiry(
    registry: str,
    browser_double: Launches,
    stops: Stops,
    clock: Clock,
) -> None:
    instance = lifecycle.launch(name="kept", headless=True, registry_path=registry)
    generation = lease_value(registry, instance.name)["generation"]
    lifecycle.retain(instance.name, registry_path=registry)
    clock.advance(10**6)
    assert not task_lease.watch_once(instance.name, generation, registry)
    assert stops == []
    assert lease_value(registry, instance.name)["state"] == "retained"


def test_one_instance_is_never_stopped_by_another_keepers_tick(
    registry: str,
    browser_double: Launches,
    stops: Stops,
    clock: Clock,
) -> None:
    first = lifecycle.launch(name="first", headless=True, registry_path=registry)
    second = lifecycle.launch(name="second", headless=True, registry_path=registry)
    generation = lease_value(registry, first.name)["generation"]
    clock.advance(3601)
    assert not task_lease.watch_once(first.name, generation, registry)
    assert [call["name"] for call in stops] == [first.name]
    assert lease_value(registry, second.name)["state"] == "auto"


def test_a_failed_close_is_recorded_as_close_failed(
    registry: str,
    browser_double: Launches,
    clock: Clock,
    monkeypatch: Patch,
) -> None:
    instance = lifecycle.launch(name="stubborn", headless=True, registry_path=registry)
    generation = lease_value(registry, instance.name)["generation"]

    def failing(*args, **kwargs):
        raise lifecycle.LifecycleError("owned browser survived")

    monkeypatch.setattr(lifecycle, "stop", failing)
    clock.advance(3601)
    assert not task_lease.watch_once(instance.name, generation, registry)
    lease = lease_value(registry, instance.name)
    assert lease["state"] == "close_failed"
    assert lease["close_error"] == "owned browser survived"


def test_a_keeper_that_cannot_start_fails_the_launch(
    registry: str,
    browser_double: Launches,
    monkeypatch: Patch,
) -> None:
    real_spawn = task_lease.spawn_keeper

    def spawn(argv, **kwargs):
        if "browser_tools.task_lease" in argv:
            raise OSError("disposable keeper spawn failed")
        return real_spawn(argv, **kwargs)

    monkeypatch.setattr(task_lease, "spawn_keeper", spawn)
    with pytest.raises(lifecycle.LifecycleError, match=r"Keeper setup failed.*new browser stopped"):
        lifecycle.launch(name="keeperless", headless=True, registry_path=registry)
    assert lifecycle.read_instances(registry) == []


# --------------------------------------------------------------------------- #
# Activity
# --------------------------------------------------------------------------- #


def test_only_the_named_instance_is_touched(
    registry: str,
    browser_double: Launches,
    clock: Clock,
) -> None:
    one = lifecycle.launch(name="one", headless=True, registry_path=registry)
    two = lifecycle.launch(name="two", headless=True, registry_path=registry)
    before = lease_value(registry, two.name)
    task_lease.begin(one.name, registry)
    assert lease_value(registry, one.name)["operations"]
    assert lease_value(registry, two.name)["operations"] == []
    assert lease_value(registry, two.name)["last_activity"] == before["last_activity"]
    task_lease.finish()
    assert lease_value(registry, one.name)["operations"] == []


def test_overlapping_operations_finish_independently(
    registry: str,
    browser_double: Launches,
    clock: Clock,
) -> None:
    instance = lifecycle.launch(name="overlap", headless=True, registry_path=registry)
    task_lease.begin(instance.name, registry)
    other = {"pid": 4242, "start": "other"}
    lease = lease_value(registry, instance.name)
    lease["operations"] = [*lease["operations"], other]
    write_lease(registry, instance.name, lease)
    task_lease.finish()
    remaining = lease_value(registry, instance.name)["operations"]
    assert other in remaining
    assert len(remaining) == 1


def test_finishing_restarts_the_countdown(
    registry: str,
    browser_double: Launches,
    clock: Clock,
) -> None:
    instance = lifecycle.launch(name="finished", headless=True, registry_path=registry)
    task_lease.begin(instance.name, registry)
    clock.advance(120)
    task_lease.finish()
    assert lease_value(registry, instance.name)["last_activity"] == clock.now


def test_status_and_help_do_not_touch_the_instance(
    registry: str,
    browser_double: Launches,
    clock: Clock,
    capsys: Capture,
) -> None:
    instance = lifecycle.launch(name="quiet", headless=True, registry_path=registry)
    before = lease_value(registry, instance.name)
    assert cli.main(["status"]) == cli.EXIT_OK
    capsys.readouterr()
    assert cli.main(["help"]) == cli.EXIT_OK
    capsys.readouterr()
    assert lease_value(registry, instance.name) == before


def test_a_command_that_fails_before_resolving_does_not_touch_the_instance(
    registry: str,
    browser_double: Launches,
    clock: Clock,
    capsys: Capture,
) -> None:
    instance = lifecycle.launch(name="quiet", headless=True, registry_path=registry)
    before = lease_value(registry, instance.name)
    assert cli.main(["navigate"]) == cli.EXIT_USAGE
    capsys.readouterr()
    assert lease_value(registry, instance.name) == before


# --------------------------------------------------------------------------- #
# The CLI surface
# --------------------------------------------------------------------------- #


def test_launch_takes_an_idle_timeout(
    registry: str,
    browser_double: Launches,
    capsys: Capture,
) -> None:
    assert cli.main(["launch", "--headless", "--name", "timed", "--idle-timeout", "30m"]) == (
        cli.EXIT_OK
    )
    out = json.loads(capsys.readouterr().out)
    assert out["idle_timeout"] == 1800
    assert out["idle_deadline"]
    assert lease_value(registry, out["name"])["idle_timeout"] == 1800


def test_launch_defaults_to_one_hour(
    registry: str,
    browser_double: Launches,
    capsys: Capture,
) -> None:
    assert cli.main(["launch", "--headless", "--name", "defaulted"]) == cli.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["idle_timeout"] == 3600


@pytest.mark.parametrize(
    "text",
    ["0s", "90", "1x", "1h1h", "30m1h", "8d", "-5s", "1.5h", "s", "1h 30m", ""],
)
def test_an_invalid_duration_is_a_usage_error(
    registry: str,
    browser_double: Launches,
    capsys: Capture,
    text: str,
) -> None:
    # Python 3.13's argparse reads "-5s" as an option and exits 2 itself.
    try:
        code = cli.main(["launch", "--headless", "--idle-timeout", text])
    except SystemExit as exc:
        code = exc.code
    assert code == cli.EXIT_USAGE
    assert "error:" in capsys.readouterr().err
    assert browser_double == []


def test_configure_changes_the_deadline(
    registry: str,
    browser_double: Launches,
    capsys: Capture,
) -> None:
    instance = lifecycle.launch(name="cfg", headless=True, registry_path=registry)
    assert cli.main(["configure", instance.name, "--idle-timeout", "2h"]) == cli.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["name"] == instance.name
    assert out["idle_timeout"] == 7200
    assert out["state"] == "auto"
    assert out["idle_deadline"]
    assert lease_value(registry, instance.name)["idle_timeout"] == 7200


def test_configure_takes_the_leading_instance_form(
    registry: str,
    browser_double: Launches,
    capsys: Capture,
) -> None:
    instance = lifecycle.launch(name="cfg", headless=True, registry_path=registry)
    assert cli.main([instance.name, "configure", "--idle-timeout", "45m"]) == cli.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["name"] == instance.name
    assert out["idle_timeout"] == 2700


def test_configure_requires_a_duration(
    registry: str,
    browser_double: Launches,
    capsys: Capture,
) -> None:
    instance = lifecycle.launch(name="cfg", headless=True, registry_path=registry)
    assert cli.main(["configure", instance.name]) == cli.EXIT_USAGE
    assert "error:" in capsys.readouterr().err


def test_configure_refuses_an_external_endpoint(
    registry: str,
    browser_double: Launches,
    capsys: Capture,
) -> None:
    instance = lifecycle.launch(name="cfg", headless=True, registry_path=registry)
    assert (
        cli.main(["configure", instance.name, "--idle-timeout", "2h", "--endpoint", "http://127.0.0.1:19423"])
        == cli.EXIT_USAGE
    )
    assert "does not take --endpoint" in capsys.readouterr().err


def test_configure_on_an_unknown_instance_fails(
    registry: str,
    browser_double: Launches,
    capsys: Capture,
) -> None:
    assert cli.main(["configure", "ghost", "--idle-timeout", "2h"]) == cli.EXIT_OPERATIONAL
    assert "error:" in capsys.readouterr().err


def test_configure_on_an_entry_without_a_lease_fails(
    registry: str,
    browser_double: Launches,
    capsys: Capture,
) -> None:
    instance = lifecycle.launch(name="plain", headless=True, registry_path=registry)
    raw = json.loads(Path(registry).read_text())
    del raw[instance.name]["lease"]
    Path(registry).write_text(json.dumps(raw))
    assert cli.main(["configure", instance.name, "--idle-timeout", "2h"]) == cli.EXIT_OPERATIONAL
    assert "not a managed lease" in capsys.readouterr().err


@pytest.mark.parametrize("state", ["closing", "close_failed"])
def test_configure_refuses_once_the_close_is_claimed(
    registry: str,
    browser_double: Launches,
    capsys: Capture,
    state: str,
) -> None:
    instance = lifecycle.launch(name="cfg", headless=True, registry_path=registry)
    lease = lease_value(registry, instance.name)
    lease["state"] = state
    write_lease(registry, instance.name, lease)
    assert cli.main(["configure", instance.name, "--idle-timeout", "2h"]) == cli.EXIT_OPERATIONAL
    assert "close already claimed" in capsys.readouterr().err


def test_configure_refuses_a_retained_instance(
    registry: str,
    browser_double: Launches,
    capsys: Capture,
) -> None:
    """A retained instance has no keeper, so no deadline may be reported for it."""
    instance = lifecycle.launch(name="kept", headless=True, registry_path=registry)
    lifecycle.retain(instance.name, registry_path=registry)
    before = lease_value(registry, instance.name)
    assert cli.main(["configure", instance.name, "--idle-timeout", "2h"]) == cli.EXIT_OPERATIONAL
    captured = capsys.readouterr()
    assert "retained and does not idle-expire" in captured.err
    assert captured.out == ""
    assert lease_value(registry, instance.name) == before


def test_a_configure_that_races_the_keeper_has_one_truthful_outcome(
    registry: str,
    browser_double: Launches,
    stops: Stops,
    clock: Clock,
    capsys: Capture,
) -> None:
    """The keeper's claim and the extension both take the registry lock.

    Whichever lands first wins, and the other says so: an extension is never
    reported as done while the browser is closing.
    """
    instance = lifecycle.launch(name="race", headless=True, registry_path=registry)
    generation = lease_value(registry, instance.name)["generation"]
    clock.advance(3601)
    assert not task_lease.watch_once(instance.name, generation, registry)
    assert lease_value(registry, instance.name)["close_reason"] == "idle"
    assert cli.main(["configure", instance.name, "--idle-timeout", "2h"]) == cli.EXIT_OPERATIONAL
    assert "close already claimed" in capsys.readouterr().err
    assert lease_value(registry, instance.name)["idle_timeout"] == 3600


def test_a_configure_before_the_deadline_postpones_the_close(
    registry: str,
    browser_double: Launches,
    stops: Stops,
    clock: Clock,
    capsys: Capture,
) -> None:
    instance = lifecycle.launch(name="race", headless=True, registry_path=registry)
    generation = lease_value(registry, instance.name)["generation"]
    clock.advance(3000)
    assert cli.main(["configure", instance.name, "--idle-timeout", "2h"]) == cli.EXIT_OK
    capsys.readouterr()
    clock.advance(3000)
    assert task_lease.watch_once(instance.name, generation, registry)
    assert stops == []


# --------------------------------------------------------------------------- #
# The durations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("90s", 90),
        ("30m", 1800),
        ("2h", 7200),
        ("1h30m", 5400),
        ("1d", 86400),
        ("1d2h3m4s", 93784),
    ],
)
def test_the_durations_the_cli_accepts(text: str, seconds: int) -> None:
    assert idle.parse_duration(text) == seconds


@pytest.mark.parametrize(
    "text",
    ["0s", "0m", "90", "1x", "1h1h", "30m1h", "8d", "-5s", "1.5h", "s", "1h 30m", "", "1 s"],
)
def test_the_durations_the_cli_refuses(text: str) -> None:
    from browser_tools.usage import UsageError

    with pytest.raises(UsageError):
        idle.parse_duration(text)


def test_configure_refuses_a_lease_from_an_earlier_release(
    registry: str,
    browser_double: Launches,
    capsys: Capture,
) -> None:
    # The earlier release's keeper enforces no deadline, so none is reported.
    instance = lifecycle.launch(name="legacy", headless=True, registry_path=registry)
    lease = lease_value(registry, instance.name)
    for key in ("idle_timeout", "last_activity", "operations"):
        lease.pop(key, None)
    write_lease(registry, instance.name, lease)
    assert cli.main(["configure", instance.name, "--idle-timeout", "2h"]) == cli.EXIT_OPERATIONAL
    captured = capsys.readouterr()
    assert "earlier release" in captured.err
    assert captured.out == ""
    assert lease_value(registry, instance.name) == lease


def test_finishing_leaves_a_replacement_that_reused_the_name_alone(
    registry: str,
    browser_double: Launches,
    clock: Clock,
) -> None:
    instance = lifecycle.launch(name="reused", headless=True, registry_path=registry)
    task_lease.begin(instance.name, registry)
    replacement = lease_value(registry, instance.name) | {"generation": "new", "operations": []}
    write_lease(registry, instance.name, replacement)
    clock.advance(50)
    task_lease.finish()
    assert lease_value(registry, instance.name) == replacement


def test_a_refused_login_does_not_restart_the_countdown(
    registry: str,
    browser_double: Launches,
    clock: Clock,
    monkeypatch: Patch,
) -> None:
    # A login refused at the ownership check never drove the browser.
    instance = lifecycle.launch(name="refused", headless=True, registry_path=registry)
    before = lease_value(registry, instance.name)

    def refuse(*args: object) -> None:
        raise lifecycle.LifecycleError("not ours")

    monkeypatch.setattr(login.login_policy, "load_account", lambda *args: (None, None))
    monkeypatch.setattr(login.login_ownership, "verify_recorded_owner", refuse)
    clock.advance(50)
    with pytest.raises(lifecycle.LifecycleError, match="not ours"):
        login.login(instance=instance.name, account="a", registry_path=registry)
    task_lease.finish()
    assert lease_value(registry, instance.name) == before


def _record_browser_process(registry: str, name: str) -> None:
    """Give the entry a start token, so its dead PID reads as gone.

    Args:
        registry: The registry file that holds the instance.
        name: The instance whose entry is changed.
    """
    raw = json.loads(Path(registry).read_text())
    raw[name]["pid_start"] = "token"
    Path(registry).write_text(json.dumps(raw))


def test_the_keeper_stops_watching_a_browser_that_is_gone(
    registry: str,
    browser_double: Launches,
    stops: Stops,
    clock: Clock,
) -> None:
    instance = lifecycle.launch(name="dead", headless=True, registry_path=registry)
    _record_browser_process(registry, instance.name)
    lease = lease_value(registry, instance.name)
    clock.advance(lease["idle_timeout"] + 1)
    assert task_lease.watch_once(instance.name, lease["generation"], registry) is False
    assert stops == []
    assert lease_value(registry, instance.name) == lease


def test_the_keeper_still_expires_a_browser_whose_launching_process_exited(
    registry: str,
    browser_double: Launches,
    stops: Stops,
    clock: Clock,
    monkeypatch: Patch,
) -> None:
    # A wrapper's PID is gone while the browser it started keeps the port.
    instance = lifecycle.launch(name="wrapped", headless=True, registry_path=registry)
    _record_browser_process(registry, instance.name)
    lease = lease_value(registry, instance.name)
    monkeypatch.setattr(lifecycle, "instance_is_live", lambda inst: inst.name == instance.name)
    assert task_lease.watch_once(instance.name, lease["generation"], registry) is True
    clock.advance(lease["idle_timeout"] + 1)
    assert task_lease.watch_once(instance.name, lease["generation"], registry) is False
    assert [call["name"] for call in stops] == [instance.name]
    assert lease_value(registry, instance.name)["close_reason"] == "idle"


# --------------------------------------------------------------------------- #
# The live check
# --------------------------------------------------------------------------- #


@pytest.mark.live
def test_a_real_idle_instance_closes_itself_and_an_extended_one_survives(
    registry: str,
    real_keeper: None,
) -> None:
    """A real browser, a real keeper, and a real expiry.

    Both instances start with a three-second timeout. The first, on a named
    profile, closes on its own and its profile's saved state survives. The
    second is extended before its deadline passes and is still alive after
    the first has closed; it is then stopped explicitly.
    """
    if not os.environ.get(lifecycle.CHROME_BINARY_ENV_VAR):
        pytest.skip(
            "no Chrome outside an application bundle was found; run "
            "`python -m playwright install chromium`"
        )
    first = lifecycle.launch(
        name="idle-live",
        profile="idle-profile",
        headless=True,
        registry_path=registry,
        idle_timeout=3,
        browser_args=["about:blank"],
    )
    second = lifecycle.launch(
        name="kept-live",
        headless=True,
        registry_path=registry,
        idle_timeout=3,
        browser_args=["about:blank"],
    )
    saved = Path(first.user_data_dir) / "saved-by-the-test"
    saved.write_text("kept")
    try:
        task_lease.configure(second.name, 300, registry)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not lifecycle.instance_is_registered(first.name, registry):
                break
            time.sleep(0.2)
        assert lifecycle.instance_is_registered(first.name, registry) is False
        assert lifecycle.process_running_state(first.pid) is False
        assert saved.read_text() == "kept"
        assert lifecycle.instance_is_live(second)
        assert lifecycle.status(second.name, registry)[0]["lease"]["state"] == "auto"
    finally:
        for instance in (first, second):
            if lifecycle.instance_is_registered(instance.name, registry):
                lifecycle.stop(instance.name, registry_path=registry)
