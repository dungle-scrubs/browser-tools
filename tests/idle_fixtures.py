"""Fixtures and helpers the idle-expiry tests share (#195).

A test module imports the fixtures it uses by name (``registry as
registry``), the way the 1Password lifecycle tests import theirs.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from browser_tools import lifecycle, task_lease
from browser_tools.core import registry as core_registry

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The fixture types the tests below take.
Launches = list[dict[str, Any]]
Stops = list[dict[str, object]]
Capture = pytest.CaptureFixture[str]
Patch = pytest.MonkeyPatch
Owner = subprocess.Popen[bytes]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(autouse=True)
def clean_activity_state() -> Iterator[None]:
    """Forget the instances a previous test began activity for."""
    task_lease._begun.clear()
    yield
    task_lease._begun.clear()


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: Patch) -> str:
    path = str(tmp_path / "registry.json")
    monkeypatch.setenv(lifecycle.REGISTRY_ENV_VAR, path)
    return path


@pytest.fixture
def browser_double(tmp_path: Path, monkeypatch: Patch) -> Launches:
    """A launch that registers an entry without starting a browser.

    Args:
        tmp_path: The test's temporary directory, for session directories.
        monkeypatch: Replaces the launcher's browser launch.

    Returns:
        The keyword arguments of each launch, in order.
    """
    launched: Launches = []

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


class Clock:
    """A wall clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: Patch) -> Clock:
    """Replace ``time.time`` with a clock the test moves by hand.

    Args:
        monkeypatch: Replaces ``time.time``.

    Returns:
        The clock now behind ``time.time``.
    """
    value = Clock()
    monkeypatch.setattr(time, "time", value)
    return value


@pytest.fixture
def stops(monkeypatch: Patch) -> Stops:
    """Record the closes a keeper claims, without stopping anything.

    Args:
        monkeypatch: Replaces ``lifecycle.stop``.

    Returns:
        One record per claimed close: name, target and lease generation.
    """
    calls: Stops = []

    def stop(name=None, target=None, registry_path=None, **kwargs):
        calls.append(
            {
                "name": name,
                "target": target,
                "generation": kwargs.get("_lease_generation"),
            }
        )
        return f"Stopped {name}"

    monkeypatch.setattr(lifecycle, "stop", stop)
    return calls


@pytest.fixture
def owner() -> Iterator[Owner]:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def lease_value(registry: str, name: str) -> dict[str, Any]:
    return json.loads(Path(registry).read_text())[name]["lease"]


def write_lease(registry: str, name: str, lease: dict[str, Any]) -> None:
    raw = json.loads(Path(registry).read_text())
    raw[name]["lease"] = lease
    Path(registry).write_text(json.dumps(raw))


def owner_gone(monkeypatch: Patch, pid: int) -> None:
    original = task_lease.observe
    monkeypatch.setattr(
        task_lease,
        "observe",
        lambda identity: "gone" if identity.pid == pid else original(identity),
    )
