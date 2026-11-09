"""Reproduce an orphan sweep that checked the marker before publication."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

import pytest

from browser_tools.core import launcher
from browser_tools.core import registry as core_registry
from browser_tools.core.connection import PortStatus


class _Process:
    pid = 424242

    def poll(self) -> None:
        return None


def test_a_sweep_that_observed_absence_cannot_delete_later_prepared_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale absent-marker decision must not defeat a newly held lease."""
    root = tmp_path / "sessions"
    default_registry = str(tmp_path / "synthetic-default.json")
    monkeypatch.setattr(os, "environ", {"BROWSER_TOOLS_REGISTRY": default_registry})
    monkeypatch.setattr(launcher, "_SESSION_ROOT", str(root))
    monkeypatch.setattr(launcher, "REGISTRY_PATH", default_registry)
    monkeypatch.setattr(core_registry, "REGISTRY_PATH", default_registry)
    allocated = threading.Event()
    absent = threading.Event()
    prepared = threading.Event()
    swept = threading.Event()
    witness: list[Path] = []
    outcomes: list[BaseException | object] = []
    real_open = os.open

    def opening(path: str | bytes | os.PathLike[str], *args: object, **kwargs: object) -> int:
        name = os.fsdecode(path)
        if threading.current_thread().name == "creator" and Path(name).name.startswith(".bt-initializing-"):
            allocated.set()
            assert absent.wait(10), "sweep never observed the absent marker"
        if threading.current_thread().name == "sweeper" and Path(name).name == ".bt-initializing":
            try:
                return real_open(path, *args, **kwargs)
            except FileNotFoundError:
                absent.set()
                assert prepared.wait(10), "initializer never finished preparing files"
                raise
        return real_open(path, *args, **kwargs)

    def initialize(directory: Path) -> None:
        target = directory / "prepared-registration.json"
        target.write_text('{"synthetic": true}')
        witness.append(target)
        prepared.set()
        assert swept.wait(10), "sweep never finished"

    def spawn(args: list[str], **kwargs: object) -> _Process:
        kwargs.pop("env", None)
        if args[0] != "synthetic-chrome":
            raise OSError("synthetic process metadata unavailable")
        assert witness[0].is_file(), "stale absent-marker decision deleted held initialization"
        return _Process()

    async def ready(*, port: int) -> PortStatus:
        return PortStatus(listening=True, browser_version="Chrome/999")

    def create() -> None:
        try:
            outcomes.append(asyncio.run(launcher.launch_browser(
                binary="synthetic-chrome", port_override=9499, headless=True,
                window_border=False, working_dir=str(tmp_path),
                registry_path=str(tmp_path / "creator.json"), initialize_directory=initialize,
            )))
        except BaseException as exc:
            outcomes.append(exc)

    def sweep() -> None:
        assert allocated.wait(10), "creator never allocated its directory"
        try:
            launcher.cleanup_sessions(registry_path=str(tmp_path / "sweeper.json"))
        finally:
            swept.set()

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    monkeypatch.setattr(launcher, "check_cdp_port_async", ready)
    creator = threading.Thread(target=create, name="creator")
    sweeper = threading.Thread(target=sweep, name="sweeper")
    creator.start()
    sweeper.start()
    creator.join(15)
    sweeper.join(15)
    assert not creator.is_alive() and not sweeper.is_alive()
    assert len(outcomes) == 1
    if isinstance(outcomes[0], BaseException):
        raise outcomes[0]
    assert witness[0].is_file()
