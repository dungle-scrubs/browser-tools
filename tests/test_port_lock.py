"""Concurrent launches never share a debug port (#234).

Two launches on registries in different directories that allocated at the
same moment picked the same port, and the one whose browser lost the bind
registered the other's browser. The unit
tests use a lock file of their own, so a launch elsewhere on the machine
cannot slow them. The live test uses the real lock, because the machine-wide
path is the behavior it proves.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest
from launcher_spawn_helpers import stub_spawn_environment

from browser_tools import lifecycle, port_lock
from browser_tools.lifecycle import LifecycleError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: Takes the lock on argv[1], says so, and keeps it for argv[2] seconds.
_HOLDER = (
    "import fcntl, os, sys, time;"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600);"
    "fcntl.flock(fd, fcntl.LOCK_EX);"
    "print('held', flush=True);"
    "time.sleep(float(sys.argv[2]))"
)


@pytest.fixture
def own_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the lock at a file only this test uses."""
    path = tmp_path / "locks" / port_lock.LOCK_NAME
    monkeypatch.setattr(port_lock, "lock_path", lambda: path)
    return path


@pytest.fixture
def another_launch(own_lock: Path) -> Iterator[subprocess.Popen[str]]:
    """A second process that holds the lock for 30 seconds or until killed."""
    own_lock.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(own_lock), "30"], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_the_lock_file_is_private_and_empty(own_lock: Path) -> None:
    with port_lock.held():
        mode = stat.S_IMODE(own_lock.stat().st_mode)

    assert mode == 0o600
    assert own_lock.stat().st_size == 0


def test_a_launch_waits_for_the_launch_ahead_of_it(
    own_lock: Path, another_launch: subprocess.Popen[str]
) -> None:
    with (
        pytest.raises(TimeoutError, match="another launch held the port allocation lock"),
        port_lock.held(wait_seconds=0.3),
    ):
        pytest.fail("the lock was taken while another process held it")

    another_launch.kill()
    another_launch.wait()
    began = time.monotonic()
    with port_lock.held(wait_seconds=5.0):
        pass
    assert time.monotonic() - began < 2.0


def test_a_spawned_browser_does_not_inherit_the_lock(own_lock: Path) -> None:
    with port_lock.held():
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with port_lock.held(wait_seconds=1.0):
            pass
    finally:
        child.kill()
        child.wait()


def test_a_launch_that_cannot_take_the_lock_fails_without_a_browser(
    own_lock: Path,
    another_launch: subprocess.Popen[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stub_spawn_environment(monkeypatch)
    launched: list[object] = []

    async def launch_browser(**kwargs: object) -> object:
        launched.append(kwargs)
        raise AssertionError("the vendored launcher ran without the port lock")

    monkeypatch.setattr(lifecycle.core_launcher, "launch_browser", launch_browser)
    monkeypatch.setattr(port_lock, "WAIT_SECONDS", 0.3)

    with pytest.raises(LifecycleError, match="another launch held the port allocation lock"):
        lifecycle.launch(headless=True, registry_path=str(tmp_path / "registry.json"))

    assert launched == []


#: Launches one headless Chrome on the registry in argv[1] at the wall-clock
#: time in argv[2], then prints the port it was given and whether the browser
#: that answers there is the one it launched.
_LAUNCHER = r"""
import json, sys, time, urllib.request
from browser_tools import lifecycle

registry = sys.argv[1]
while time.time() < float(sys.argv[2]):
    time.sleep(0.001)
instance = lifecycle.launch(headless=True, registry_path=registry, browser_args=["about:blank"])
try:
    version = f"http://127.0.0.1:{instance.port}/json/version"
    with urllib.request.urlopen(version, timeout=5) as reply:
        answering = json.load(reply)["webSocketDebuggerUrl"]
    # Chrome writes this file only when it bound its debug port.
    try:
        with open(f"{instance.user_data_dir}/DevToolsActivePort") as active:
            own_port, own_path = active.read().split()[:2]
    except FileNotFoundError:
        own_port, own_path = "0", "no-debug-port"
    print(json.dumps({
        "port": instance.port,
        "own_port": int(own_port),
        "answered_by_own_browser": answering.endswith(own_path),
    }), flush=True)
    time.sleep(1.0)
finally:
    lifecycle.stop(instance=instance.name, registry_path=registry)
"""


def test_concurrent_launches_each_get_their_own_port_and_browser(tmp_path: Path) -> None:
    if not os.environ.get(lifecycle.CHROME_BINARY_ENV_VAR):
        pytest.skip("no plain Chrome binary for an isolated launch")
    # One directory per registry. Registries that share a directory share the
    # registry lock, which already runs their launches one at a time.
    start = str(time.time() + 3.0)
    launches = [
        subprocess.Popen(
            [sys.executable, "-c", _LAUNCHER, str(tmp_path / f"run-{n}" / "registry.json"), start],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(4)
    ]
    results = [launch.communicate(timeout=120) for launch in launches]

    rows = []
    for (out, err), launch in zip(results, launches, strict=True):
        assert launch.returncode == 0, err[-2000:]
        rows.append(json.loads(out.strip().splitlines()[-1]))
    assert all(row["answered_by_own_browser"] for row in rows), rows
    assert all(row["port"] == row["own_port"] for row in rows), rows
    assert len({row["port"] for row in rows}) == len(rows), rows
