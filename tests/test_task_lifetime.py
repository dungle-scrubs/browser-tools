from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from browser_tools import cli, lifecycle, task_lease
from browser_tools.core import registry as core_registry


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def owner():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = str(tmp_path / "registry.json")
    monkeypatch.setenv(lifecycle.REGISTRY_ENV_VAR, path)
    return path


@pytest.fixture
def browser_double(tmp_path, monkeypatch):
    launched = []

    async def launch(**kwargs):
        launched.append(kwargs)
        directory = Path(kwargs.get("user_data_dir") or tmp_path / "session")
        directory.mkdir(exist_ok=True, parents=True)
        (directory / "marker").write_text("disposable")
        return core_registry.register(
            working_dir=kwargs["working_dir"],
            pid=2000000000,
            browser_version="Chrome/1",
            user_data_dir=str(directory),
            port_override=free_port(),
            registry_path=kwargs["registry_path"],
        )

    monkeypatch.setattr(lifecycle.core_launcher, "launch_browser", launch)
    return launched


@pytest.fixture
def keeper_double(monkeypatch):
    spawned = []
    real_popen = subprocess.Popen

    def popen(argv, **kwargs):
        if "browser_tools.task_lease" in argv:
            proc = real_popen(
                [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"],
                **kwargs,
            )
            spawned.append(proc)
            return proc
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(task_lease.subprocess, "Popen", popen)
    try:
        yield spawned
    finally:
        for proc in spawned:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def launch_owned(registry, owner):
    return lifecycle.launch(name="owned", headless=True, owner=owner.pid, registry_path=registry)


def lease_value(registry, name):
    return json.loads(Path(registry).read_text())[name]["lease"]


def owner_gone(monkeypatch, pid):
    original = task_lease.observe
    monkeypatch.setattr(
        task_lease,
        "observe",
        lambda identity: "gone" if identity.pid == pid else original(identity),
    )


def test_plain_launch_has_no_lease_or_keeper(registry, browser_double, keeper_double):
    instance = lifecycle.launch(name="plain", headless=True, registry_path=registry)
    assert "lease" not in lifecycle.status(instance.name, registry)[0]
    assert keeper_double == []
    assert len(browser_double) == 1


def test_keeper_retries_a_temporary_registry_lock_timeout(registry, monkeypatch):
    calls = []
    sleeps = []

    def watch(*args):
        calls.append(args)
        if len(calls) == 1:
            with lifecycle.registry_lock(registry, timeout=0):
                raise AssertionError("lock should be unavailable")
        return False

    def busy(*args):
        raise BlockingIOError

    monkeypatch.setattr(lifecycle.fcntl, "flock", busy)
    monkeypatch.setattr(task_lease, "watch_once", watch)
    monkeypatch.setattr(task_lease.time, "sleep", sleeps.append)
    monkeypatch.setattr(sys, "argv", ["keeper", "owned", "generation", registry])
    task_lease.main()
    assert calls == [("owned", "generation", registry)] * 2
    assert sleeps == [0.25]


def test_keeper_exits_on_unknown_registry_state(registry, monkeypatch):
    calls = []

    def unknown(*args):
        calls.append(args)
        raise lifecycle.LifecycleError("Registry state unknown")

    monkeypatch.setattr(task_lease, "watch_once", unknown)
    monkeypatch.setattr(sys, "argv", ["keeper", "owned", "generation", registry])
    task_lease.main()
    assert calls == [("owned", "generation", registry)]


@pytest.mark.parametrize("pid", [-1, 0, 2000000000])
def test_invalid_owner_is_refused_before_launch(registry, browser_double, pid):
    with pytest.raises(lifecycle.LifecycleError, match="Owner"):
        lifecycle.launch(owner=pid, registry_path=registry)
    assert browser_double == []
    assert not Path(registry).exists()


def test_retain_wins_before_owner_exit_and_preserves_extension_fields(
    registry, owner, browser_double, keeper_double, monkeypatch
):
    instance = launch_owned(registry, owner)
    raw = json.loads(Path(registry).read_text())
    raw[instance.name]["lease"]["extension"] = "keep"
    raw["other"] = {**raw[instance.name], "lease": None, "extension": "unrelated"}
    Path(registry).write_text(json.dumps(raw))
    first = lifecycle.retain(instance.name, "handoff", registry)
    second = lifecycle.retain(instance.name, registry_path=registry)
    assert (
        first
        == second
        == {"name": instance.name, "retained": True, "mode": "headless", "note": "handoff"}
    )
    owner_gone(monkeypatch, owner.pid)
    assert not task_lease.watch_once(
        instance.name, lease_value(registry, instance.name)["generation"], registry
    )
    assert lease_value(registry, instance.name)["state"] == "retained"
    assert lease_value(registry, instance.name)["extension"] == "keep"
    assert json.loads(Path(registry).read_text())["other"]["extension"] == "unrelated"
    assert Path(instance.user_data_dir).exists()


def test_unknown_owner_does_not_close_and_missing_keeper_is_visible(
    registry, owner, browser_double, keeper_double, monkeypatch
):
    instance = launch_owned(registry, owner)
    generation = lease_value(registry, instance.name)["generation"]
    keeper_double[0].kill()
    keeper_double[0].wait()
    original = task_lease.observe
    monkeypatch.setattr(
        task_lease,
        "observe",
        lambda identity: "unverified" if identity.pid == owner.pid else original(identity),
    )
    assert task_lease.watch_once(instance.name, generation, registry)
    status = lifecycle.status(instance.name, registry)[0]["lease"]
    assert status["state"] == "auto"
    assert status["owner"] == "unverified"
    assert status["keeper"] == "missing"
    assert Path(instance.user_data_dir).exists()


def test_stale_generation_cannot_stop_new_instance_at_stop_lookup(
    registry, owner, browser_double, keeper_double
):
    instance = launch_owned(registry, owner)
    old_generation = lease_value(registry, instance.name)["generation"]
    raw = json.loads(Path(registry).read_text())
    raw[instance.name]["lease"].update(generation="new-generation", state="closing")
    Path(registry).write_text(json.dumps(raw))
    assert not task_lease.watch_once(instance.name, old_generation, registry)
    with pytest.raises(lifecycle.LifecycleError, match="generation"):
        lifecycle.stop(instance.name, registry_path=registry, _lease_generation=old_generation)
    assert lease_value(registry, instance.name)["generation"] == "new-generation"
    assert Path(instance.user_data_dir).exists()


def test_close_claim_wins_and_failed_close_remains_recoverable(
    registry, owner, browser_double, keeper_double, monkeypatch
):
    instance = launch_owned(registry, owner)
    generation = lease_value(registry, instance.name)["generation"]
    owner_gone(monkeypatch, owner.pid)
    original_stop = lifecycle.stop

    def failing_stop(*args, **kwargs):
        with pytest.raises(lifecycle.LifecycleError, match="close already claimed"):
            lifecycle.retain(instance.name, registry_path=registry)
        raise lifecycle.LifecycleError("owned browser survived")

    monkeypatch.setattr(lifecycle, "stop", failing_stop)
    assert not task_lease.watch_once(instance.name, generation, registry)
    value = lease_value(registry, instance.name)
    assert value["state"] == "close_failed"
    assert value["generation"] == generation
    assert value["close_error"] == "owned browser survived"
    assert Path(instance.user_data_dir).exists()
    monkeypatch.setattr(lifecycle, "stop", original_stop)
    assert "owned-01" in lifecycle.stop(instance.name, registry_path=registry)
    assert lifecycle.read_instances(registry) == []


def test_cli_owner_and_retain_contract(registry, owner, browser_double, keeper_double, capsys):
    assert (
        cli.main(["launch", "--headless", "--owner", str(owner.pid), "--name", "cli"])
        == cli.EXIT_OK
    )
    launched = json.loads(capsys.readouterr().out)
    assert launched["name"] == "cli-01"
    assert cli.main(["retain", "cli-01", "--note", "another agent"]) == cli.EXIT_OK
    retained = json.loads(capsys.readouterr().out)
    assert retained == {
        "name": "cli-01",
        "retained": True,
        "mode": "headless",
        "note": "another agent",
    }
    assert cli.main(["status", "cli-01"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)[0]["lease"]["state"] == "retained"
    assert cli.main(["retain", "cli-01", "--endpoint", "http://127.0.0.1:19423"]) == cli.EXIT_USAGE
    assert "does not take --endpoint" in capsys.readouterr().err


def test_keeper_setup_failure_truthfully_cleans_only_new_browser(
    registry, owner, browser_double, monkeypatch
):
    real_popen = subprocess.Popen

    def popen(argv, **kwargs):
        if "browser_tools.task_lease" in argv:
            raise OSError("disposable keeper spawn failed")
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(task_lease.subprocess, "Popen", popen)
    with pytest.raises(lifecycle.LifecycleError, match=r"Keeper setup failed.*new browser stopped"):
        launch_owned(registry, owner)
    assert lifecycle.read_instances(registry) == []


@pytest.mark.parametrize("retain", [False, True])
@pytest.mark.parametrize("owner_signal", [signal.SIGTERM, signal.SIGKILL])
def test_real_keeper_handles_sigkill_owner_and_named_profile(registry, owner, retain, owner_signal):
    instance = lifecycle.launch(
        name="live",
        profile="task-profile",
        headless=True,
        owner=owner.pid,
        registry_path=registry,
        browser_args=["about:blank"],
    )
    try:
        assert lifecycle.status(instance.name, registry)[0]["lease"]["keeper"] == "running"
        if retain:
            assert lifecycle.retain(instance.name, registry_path=registry)["mode"] == "headless"
        os.kill(owner.pid, owner_signal)
        owner.wait()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            instances = lifecycle.read_instances(registry)
            if not retain and not instances:
                break
            if (
                retain
                and instances
                and lifecycle.status(instance.name, registry)[0]["lease"]["keeper"] == "missing"
            ):
                break
            time.sleep(0.1)
        if retain:
            assert lifecycle.instance_is_live(instance)
            assert lifecycle.status(instance.name, registry)[0]["lease"]["state"] == "retained"
        else:
            assert lifecycle.read_instances(registry) == []
            assert lifecycle.process_running_state(instance.pid) is False
        assert Path(instance.user_data_dir).is_dir()
    finally:
        if lifecycle.instance_is_registered(instance.name, registry):
            lifecycle.stop(instance.name, registry_path=registry)


def test_keeper_setup_cleanup_failure_retains_error_and_generation(
    registry, owner, browser_double, monkeypatch
):
    real_popen = subprocess.Popen

    def popen(argv, **kwargs):
        if "browser_tools.task_lease" in argv:
            raise OSError("keeper unavailable")
        return real_popen(argv, **kwargs)

    def survivor(*args, **kwargs):
        raise lifecycle.LifecycleError("owned survivor")

    monkeypatch.setattr(task_lease.subprocess, "Popen", popen)
    monkeypatch.setattr(lifecycle, "stop", survivor)
    with pytest.raises(lifecycle.LifecycleError, match="browser cleanup failed: owned survivor"):
        launch_owned(registry, owner)
    value = lease_value(registry, "owned-01")
    assert value["state"] == "close_failed"
    assert value["generation"]
    assert value["close_error"] == "owned survivor"
    assert lifecycle.read_instances(registry)[0].name == "owned-01"


def test_delayed_claim_rechecks_generation_at_actual_stop(
    registry, owner, browser_double, keeper_double, monkeypatch
):
    instance = launch_owned(registry, owner)
    old_generation = lease_value(registry, instance.name)["generation"]
    owner_gone(monkeypatch, owner.pid)
    original_stop = lifecycle.stop

    def replace_before_stop(*args, **kwargs):
        raw = json.loads(Path(registry).read_text())
        raw[instance.name]["lease"].update(generation="replacement", state="auto")
        Path(registry).write_text(json.dumps(raw))
        return original_stop(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "stop", replace_before_stop)
    assert not task_lease.watch_once(instance.name, old_generation, registry)
    value = lease_value(registry, instance.name)
    assert value["generation"] == "replacement"
    assert value["state"] == "auto"
    assert "close_error" not in value
    assert Path(instance.user_data_dir).exists()


def test_camoufox_owner_binding_uses_same_lease_and_retain_contract(
    registry, owner, keeper_double, monkeypatch
):
    monkeypatch.setattr(
        lifecycle, "_spawn_camoufox_process", lambda directory, headless: (2000000000, None)
    )
    instance = lifecycle.launch(
        engine="camoufox", name="cam", headless=True, owner=owner.pid, registry_path=registry
    )
    assert instance.engine == "camoufox"
    assert len(keeper_double) == 1
    assert lifecycle.status(instance.name, registry)[0]["lease"]["state"] == "auto"
    assert lifecycle.retain(instance.name, registry_path=registry)["mode"] == "headless"
    assert "cam-01" in lifecycle.stop(instance.name, registry_path=registry)
    assert lifecycle.read_instances(registry) == []
