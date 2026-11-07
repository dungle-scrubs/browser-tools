"""Shared fixtures for the 1Password lifecycle and CLI tests (#201).

One owner for the synthetic official registration, the faked vendored
launcher, and the platform/codesign/Camoufox guards. Extracted from
``test_onepassword_lifecycle.py`` (repository module-size rule): the lifecycle
and CLI test modules import what they use explicitly, so the autouse guards
apply only to the modules that import them. Everything here is synthetic;
no real registration, browser or codesign is touched.

The ``stubbed_core_boundaries`` fixtures run the vendored launcher for real
with only the process spawn, the CDP readiness probe and the startup sweep
stubbed, for the tests that must prove the launcher's own initializer hook
and generated files rather than a policy double.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from typing import TYPE_CHECKING, NoReturn

import pytest

from browser_tools import lifecycle, onepassword
from browser_tools.core import launcher as core_launcher
from browser_tools.core import registry as core_registry
from browser_tools.core.connection import PortStatus

if TYPE_CHECKING:
    from pathlib import Path

STABLE = onepassword.STABLE_EXTENSION_ORIGIN
OTHER_ORIGIN = "chrome-extension://" + "b" * 32 + "/"
HOSTS = "NativeMessagingHosts"
DEST_NAME = "com.1password.1password.json"


@pytest.fixture(autouse=True)
def _never_read_the_real_registration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        onepassword,
        "SOURCE_MANIFEST",
        tmp_path / "absent" / HOSTS / DEST_NAME,
    )


@pytest.fixture(autouse=True)
def _no_real_codesign(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(onepassword, "subprocess", types.SimpleNamespace(run=fake_run))


@pytest.fixture(autouse=True)
def _no_real_camoufox_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("a provisioning test attempted to launch a real Camoufox")

    monkeypatch.setattr(lifecycle, "_spawn_camoufox_process", forbidden)


@pytest.fixture(autouse=True)
def _darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the platform so the positive paths run anywhere the suite runs."""
    monkeypatch.setattr(sys, "platform", "darwin")


@pytest.fixture
def registry_path(tmp_path: Path) -> str:
    return str(tmp_path / "registry.json")


@pytest.fixture
def profiles_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "profiles"
    monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(root))
    return root


@pytest.fixture
def valid_installation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A valid official registration the provisioning path can read."""
    helper = tmp_path / "app" / "1Password-BrowserSupport"
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o755)
    hosts = tmp_path / "official-chrome" / HOSTS
    hosts.mkdir(parents=True, exist_ok=True)
    (hosts / DEST_NAME).write_text(
        json.dumps(
            {
                "name": "com.1password.1password",
                "description": "1Password native messaging host",
                "path": str(helper),
                "type": "stdio",
                "allowed_origins": [STABLE, OTHER_ORIGIN],
            }
        )
    )
    monkeypatch.setattr(onepassword, "SOURCE_MANIFEST", hosts / DEST_NAME)
    return helper


@pytest.fixture
def fake_browser_launch(monkeypatch: pytest.MonkeyPatch, registry_path: str) -> dict[str, object]:
    """Replace the vendored launcher; record what it was handed."""
    captured: dict[str, object] = {}

    async def fake_launch_browser(**kwargs: object) -> object:
        captured.update(kwargs)
        info = core_registry.register(
            working_dir=str(kwargs.get("working_dir")),
            pid=4321,
            browser_version="Chrome/9",
            user_data_dir=str(kwargs.get("user_data_dir")),
            port_override=9450,
            registry_path=str(kwargs.get("registry_path")),
            pid_start="tok",
        )
        return core_registry.lookup(info.name, registry_path=str(kwargs.get("registry_path")))

    monkeypatch.setattr(lifecycle.core_launcher, "launch_browser", fake_launch_browser)
    return captured


class _FakeProcess:
    """Stands in for the spawned Chrome process."""

    pid: int = 424242

    def poll(self) -> None:
        return None


@pytest.fixture
def synthetic_chrome_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A plain executable the environment override accepts as the browser."""
    binary = tmp_path / "synthetic-chrome"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv(lifecycle.CHROME_BINARY_ENV_VAR, str(binary))
    return binary


@pytest.fixture
def stubbed_core_boundaries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    """Stub the spawn/CDP/sweep seams; keep allocation, preferences, registry.

    The vendored launcher itself runs. ``order`` records the launch sequence
    (cleanup sweep, initializer, preference preparation, spawn), and
    ``sessions_root`` is the isolated session root fresh directories are
    allocated under.
    """
    order: list[str] = []
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(core_launcher, "_SESSION_ROOT", str(sessions_root))

    def fake_popen(args, stdout=None, stderr=None, env=None, **kwargs):
        # The first spawn is Chrome; the process-identity probe after it runs
        # `ps` through Popen too, and is not a browser spawn.
        if "spawn" not in order:
            order.append("spawn")
        return _FakeProcess()

    async def fake_check_cdp_port_async(*, port):
        return PortStatus(listening=True, browser_version="Chrome/999.0.0.0")

    def fake_cleanup_sessions(registry_path=None):
        order.append("cleanup")
        return []

    monkeypatch.setattr(core_launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(core_launcher, "check_cdp_port_async", fake_check_cdp_port_async)
    monkeypatch.setattr(core_launcher, "cleanup_sessions", fake_cleanup_sessions)

    real_prepare = core_launcher._prepare_preferences

    def recording_prepare(path):
        order.append("preferences")
        real_prepare(path)

    monkeypatch.setattr(core_launcher, "_prepare_preferences", recording_prepare)
    return {"order": order, "sessions_root": sessions_root}
