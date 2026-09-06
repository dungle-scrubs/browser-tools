"""Named profiles reject unsafe names and migrate only idle storage."""

import fcntl
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from browser_tools import lifecycle


@pytest.fixture
def process_inventory(monkeypatch):
    """Bound the OS inventory to this test's real processes, not desktop apps."""
    pids = [os.getpid()]
    run = subprocess.run

    def bounded_run(args, **kwargs):
        if args[:1] == ["ps"]:
            return subprocess.CompletedProcess(args, 0, "".join(f"{pid} S\n" for pid in pids), "")
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", bounded_run)
    return pids


@pytest.mark.parametrize("name", ["..", ".", "../escape", "a/b", "x" * 65, ".Ephemeral", ""])
def test_launch_rejects_invalid_profile_before_creating_directories(name, monkeypatch, tmp_path):
    root = tmp_path / "profiles"
    monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(root))
    monkeypatch.setattr(
        lifecycle.core_launcher,
        "launch_browser",
        AsyncMock(side_effect=AssertionError("invalid name reached browser launch")),
    )
    with pytest.raises(lifecycle.LifecycleError, match="Profile Name"):
        lifecycle.launch(profile=name)
    assert not root.exists()


def test_launch_migrates_idle_profile_and_preserves_contents(
    monkeypatch, tmp_path, process_inventory
):
    monkeypatch.delenv(lifecycle.PROFILES_ENV_VAR, raising=False)
    monkeypatch.setattr(lifecycle.Path, "home", lambda: tmp_path)
    legacy = tmp_path / "legacy"
    source = legacy / "login"
    source.mkdir(parents=True, mode=0o700)
    (source / "state").write_text("login state")
    monkeypatch.setattr(lifecycle, "LEGACY_PROFILES_ROOT", legacy, raising=False)
    destination = tmp_path / ".cache/browser-tools/profiles/login"
    launcher = AsyncMock(
        return_value=SimpleNamespace(
            name="test",
            port=9222,
            pid=123,
            browser_version="test",
            user_data_dir=str(destination),
            pid_start=None,
        )
    )
    monkeypatch.setattr(lifecycle.core_launcher, "launch_browser", launcher)
    lifecycle.launch(profile="login", registry_path=str(tmp_path / "registry.json"))
    assert not source.exists()
    assert (destination / "state").read_text() == "login state"
    assert destination.stat().st_mode & 0o777 == 0o700


def test_live_unregistered_profile_is_deferred_then_migrated(
    monkeypatch, tmp_path, process_inventory
):
    monkeypatch.delenv(lifecycle.PROFILES_ENV_VAR, raising=False)
    monkeypatch.setattr(lifecycle.Path, "home", lambda: tmp_path)
    legacy = tmp_path / "legacy"
    source = legacy / "login"
    source.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(lifecycle, "LEGACY_PROFILES_ROOT", legacy)
    destination = tmp_path / ".cache/browser-tools/profiles/login"
    launcher = AsyncMock(
        return_value=SimpleNamespace(
            name="test",
            port=9222,
            pid=123,
            browser_version="test",
            user_data_dir=str(destination),
            pid_start=None,
        )
    )
    monkeypatch.setattr(lifecycle.core_launcher, "launch_browser", launcher)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", f"--user-data-dir={source}"]
    )
    process_inventory.append(child.pid)
    try:
        with pytest.raises(lifecycle.LifecycleError, match="holder"):
            lifecycle.launch(profile="login", registry_path=str(tmp_path / "registry.json"))
        assert source.exists()
        assert not destination.exists()
        launcher.assert_not_called()
    finally:
        child.terminate()
        child.wait(timeout=5)
    lifecycle.launch(profile="login", registry_path=str(tmp_path / "registry.json"))
    assert destination.exists()
    assert not source.exists()


def test_migration_refuses_an_unsafe_source_parent(monkeypatch, tmp_path, process_inventory):
    monkeypatch.delenv(lifecycle.PROFILES_ENV_VAR, raising=False)
    monkeypatch.setattr(lifecycle.Path, "home", lambda: tmp_path)
    legacy = tmp_path / "legacy"
    (legacy / "login").mkdir(parents=True)
    legacy.chmod(0o777)
    monkeypatch.setattr(lifecycle, "LEGACY_PROFILES_ROOT", legacy)
    launcher = AsyncMock(side_effect=AssertionError("unsafe migration reached launch"))
    monkeypatch.setattr(lifecycle.core_launcher, "launch_browser", launcher)
    with pytest.raises(lifecycle.LifecycleError, match="unsafe"):
        lifecycle.launch(profile="login", registry_path=str(tmp_path / "registry.json"))
    assert (legacy / "login").exists()
    launcher.assert_not_called()


def test_named_launch_cannot_pass_another_lifecycle_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle.Path, "home", lambda: tmp_path)
    monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(tmp_path / "profiles"))
    monkeypatch.setattr(lifecycle, "PROFILE_LOCK_TIMEOUT", 0, raising=False)
    lock = tmp_path / ".cache/browser-tools/profile-lifecycle.lock"
    lock.parent.mkdir(parents=True, mode=0o700)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    launcher = AsyncMock(side_effect=AssertionError("launch passed another owner's lock"))
    monkeypatch.setattr(lifecycle.core_launcher, "launch_browser", launcher)
    try:
        with pytest.raises(lifecycle.LifecycleError, match="lock"):
            lifecycle.launch(profile="login")
        assert not (tmp_path / "profiles").exists()
        launcher.assert_not_called()
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    "case",
    [
        "collision",
        "uncertain",
        "symlink",
        "rename-failure",
        "second-check-holder",
        "override",
        "reserved",
    ],
)
def test_migration_preserves_deferred_data(case, monkeypatch, tmp_path, process_inventory):
    monkeypatch.delenv(lifecycle.PROFILES_ENV_VAR, raising=False)
    monkeypatch.setattr(lifecycle.Path, "home", lambda: tmp_path)
    legacy = tmp_path / "legacy"
    source = legacy / "login"
    source.mkdir(parents=True, mode=0o700)
    (source / "state").write_text("legacy")
    destination = tmp_path / ".cache/browser-tools/profiles/login"
    monkeypatch.setattr(lifecycle, "LEGACY_PROFILES_ROOT", legacy)
    launcher = AsyncMock(
        return_value=SimpleNamespace(
            name="test",
            port=9222,
            pid=123,
            browser_version="test",
            user_data_dir=str(destination),
            pid_start=None,
        )
    )
    monkeypatch.setattr(lifecycle.core_launcher, "launch_browser", launcher)
    blocked = case in {"uncertain", "symlink", "rename-failure", "second-check-holder"}
    if case == "collision":
        destination.mkdir(parents=True, mode=0o700)
        (destination / "state").write_text("destination")
    elif case == "uncertain":
        monkeypatch.setattr(lifecycle, "read_process_args", lambda pid: None)
    elif case == "symlink":
        actual = tmp_path / "actual"
        source.rename(actual)
        source.symlink_to(actual, target_is_directory=True)
    elif case == "rename-failure":

        def fail_rename(*args):
            raise OSError("cross-device rename")

        monkeypatch.setattr(lifecycle, "_rename_profile", fail_rename)
    elif case == "second-check-holder":
        reads = iter([["test"], ["test", f"--user-data-dir={source}"]])
        monkeypatch.setattr(lifecycle, "read_process_args", lambda pid: next(reads))
    elif case == "override":
        monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(tmp_path / "override"))
    elif case == "reserved":
        reserved = legacy / ".ePhEmErAl"
        reserved.mkdir(mode=0o700)
        (reserved / "state").write_text("keep reserved")
    if blocked:
        with pytest.raises(lifecycle.LifecycleError, match="deferred"):
            lifecycle.launch(profile="login", registry_path=str(tmp_path / "registry.json"))
        launcher.assert_not_called()
        assert not destination.exists()
    else:
        lifecycle.launch(profile="login", registry_path=str(tmp_path / "registry.json"))
        launcher.assert_called_once()
    if case == "reserved":
        assert (reserved / "state").read_text() == "keep reserved"
        assert not source.exists()
    else:
        assert (source / "state").read_text() == "legacy"
    if case == "collision":
        assert (destination / "state").read_text() == "destination"


def test_atomic_profile_move_never_replaces_a_destination(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "state").write_text("source")
    (destination / "state").write_text("destination")
    with pytest.raises(OSError):
        lifecycle._rename_profile(source, destination)
    assert (source / "state").read_text() == "source"
    assert (destination / "state").read_text() == "destination"


def test_unsafe_legacy_root_does_not_block_an_unrelated_profile(monkeypatch, tmp_path):
    monkeypatch.delenv(lifecycle.PROFILES_ENV_VAR, raising=False)
    monkeypatch.setattr(lifecycle.Path, "home", lambda: tmp_path)
    legacy = tmp_path / "legacy"
    (legacy / "unrelated").mkdir(parents=True)
    legacy.chmod(0o777)
    monkeypatch.setattr(lifecycle, "LEGACY_PROFILES_ROOT", legacy)
    destination = tmp_path / ".cache/browser-tools/profiles/work"
    launcher = AsyncMock(
        return_value=SimpleNamespace(
            name="test",
            port=9222,
            pid=123,
            browser_version="test",
            user_data_dir=str(destination),
            pid_start=None,
        )
    )
    monkeypatch.setattr(lifecycle.core_launcher, "launch_browser", launcher)
    lifecycle.launch(profile="work", registry_path=str(tmp_path / "registry.json"))
    launcher.assert_called_once()
    assert (legacy / "unrelated").is_dir()


def test_registered_holder_blocks_migration(monkeypatch, tmp_path, process_inventory):
    import json

    from browser_tools.core.utils import process_start_time

    monkeypatch.delenv(lifecycle.PROFILES_ENV_VAR, raising=False)
    monkeypatch.setattr(lifecycle.Path, "home", lambda: tmp_path)
    legacy = tmp_path / "legacy"
    source = legacy / "login"
    source.mkdir(parents=True)
    monkeypatch.setattr(lifecycle, "LEGACY_PROFILES_ROOT", legacy)
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "holder": {
                    "pid": os.getpid(),
                    "pid_start": process_start_time(os.getpid()),
                    "port": 9222,
                    "user_data_dir": str(source),
                    "browser_version": "test",
                }
            }
        )
    )
    with pytest.raises(lifecycle.LifecycleError, match="live holder holder"):
        lifecycle.launch(profile="login", registry_path=str(registry))
    assert source.is_dir()
    assert not (tmp_path / ".cache/browser-tools/profiles/login").exists()


def test_launch_recovers_after_interruption_immediately_after_rename(
    monkeypatch, tmp_path, process_inventory
):
    monkeypatch.delenv(lifecycle.PROFILES_ENV_VAR, raising=False)
    monkeypatch.setattr(lifecycle.Path, "home", lambda: tmp_path)
    legacy = tmp_path / "legacy"
    source = legacy / "login"
    source.mkdir(parents=True)
    (source / "state").write_text("preserved")
    destination = tmp_path / ".cache/browser-tools/profiles/login"
    monkeypatch.setattr(lifecycle, "LEGACY_PROFILES_ROOT", legacy)
    rename = lifecycle._rename_profile

    def interrupted(source, destination):
        rename(source, destination)
        raise KeyboardInterrupt

    monkeypatch.setattr(lifecycle, "_rename_profile", interrupted)
    with pytest.raises(KeyboardInterrupt):
        lifecycle.launch(profile="login", registry_path=str(tmp_path / "registry.json"))
    launcher = AsyncMock(
        return_value=SimpleNamespace(
            name="test",
            port=9222,
            pid=123,
            browser_version="test",
            user_data_dir=str(destination),
            pid_start=None,
        )
    )
    monkeypatch.setattr(lifecycle.core_launcher, "launch_browser", launcher)
    lifecycle.launch(profile="login", registry_path=str(tmp_path / "registry.json"))
    launcher.assert_called_once()
    assert (destination / "state").read_text() == "preserved"
    assert not source.exists()


def test_two_cooperating_named_launches_serialize(tmp_path):
    import time

    script = r"""
import asyncio, os, sys, time
from pathlib import Path
from types import SimpleNamespace
from browser_tools import lifecycle
root = Path(sys.argv[1])
name = sys.argv[2]
lifecycle.Path.home = staticmethod(lambda: root)
os.environ[lifecycle.PROFILES_ENV_VAR] = str(root / "profiles")
async def launch_browser(**kwargs):
    fd = os.open(root / "inside", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    (root / (name + "-entered")).touch()
    if name == "first":
        deadline = time.monotonic() + 10
        while not (root / "release").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("test release not received")
            await asyncio.sleep(0.01)
    (root / "inside").unlink()
    return SimpleNamespace(name=name, port=9222, pid=os.getpid(), browser_version="test", user_data_dir=str(root / "profiles" / name), pid_start=None)
lifecycle.core_launcher.launch_browser = launch_browser
(root / (name + "-attempting")).touch()
lifecycle.launch(profile=name, registry_path=str(root / "registry.json"))
"""
    children = []

    def wait_for(path):
        deadline = time.monotonic() + 5
        while not path.exists():
            assert time.monotonic() < deadline, f"child did not reach {path.name}"
            time.sleep(0.01)

    try:
        for name in ("first", "second"):
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", script, str(tmp_path), name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
            wait_for(tmp_path / (name + ("-entered" if name == "first" else "-attempting")))
        assert not (tmp_path / "second-entered").exists()
        (tmp_path / "release").touch()
        for child in children:
            _, error = child.communicate(timeout=10)
            assert child.returncode == 0, error
        assert (tmp_path / "second-entered").exists()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate()
