"""The vendored launcher's generic directory-initializer hook (#201).

The launcher itself stays policy-free: it accepts a callable, runs it inside
the launch window, owns the throwaway directory it allocated for that call,
and holds an initialization marker so a concurrent orphan sweep cannot reap
a session still being prepared. These tests call ``launch_browser`` directly
with the process/CDP/sweep seams stubbed, proving the real core behavior
rather than a lifecycle double.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from browser_tools.core import launcher


class _FakeProcess:
    """Stands in for the spawned Chrome process."""

    def __init__(self, returncode: int | None = None) -> None:
        self.pid = 424242
        self.returncode = returncode
        self.stderr = None

    def poll(self) -> int | None:
        return self.returncode


@pytest.fixture
def stubbed_boundaries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    """Stub spawn/CDP/sweep; keep allocation, preferences and the registry real."""
    order: list[str] = []
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(launcher, "_SESSION_ROOT", str(sessions_root))

    def fake_popen(args, stdout=None, stderr=None, env=None, **kwargs):
        if "spawn" not in order:
            order.append("spawn")
        return _FakeProcess()

    async def fake_check_cdp_port_async(*, port):
        return types.SimpleNamespace(listening=True, browser_version="Chrome/999.0.0.0")

    def fake_cleanup_sessions(registry_path=None):
        order.append("cleanup")
        return []

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launcher, "check_cdp_port_async", fake_check_cdp_port_async)
    monkeypatch.setattr(launcher, "cleanup_sessions", fake_cleanup_sessions)

    real_prepare = launcher._prepare_preferences

    def recording_prepare(path):
        order.append("preferences")
        real_prepare(path)

    monkeypatch.setattr(launcher, "_prepare_preferences", recording_prepare)
    return {"order": order, "sessions_root": sessions_root}


def _launch(
    registry_path: str,
    port: int,
    *,
    initialize_directory=None,
    user_data_dir: str | None = None,
    popen_process: _FakeProcess | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
):
    if popen_process is not None and monkeypatch is not None:
        monkeypatch.setattr(
            launcher.subprocess,
            "Popen",
            lambda *a, **k: popen_process,
        )
    return asyncio.run(
        launcher.launch_browser(
            port_override=port,
            binary="synthetic-chrome",
            headless=True,
            working_dir=str(Path(registry_path).parent),
            registry_path=registry_path,
            user_data_dir=user_data_dir,
            initialize_directory=initialize_directory,
        )
    )


class TestInitializerOrdering:
    def test_the_initializer_runs_between_sweep_preferences_and_spawn(
        self, monkeypatch: pytest.MonkeyPatch, stubbed_boundaries: dict[str, object], tmp_path: Path
    ) -> None:
        seen: dict[str, Path] = {}

        def record(user_data_dir: Path) -> None:
            seen["initializer"] = user_data_dir
            stubbed_boundaries["order"].append("initializer")

        registry = str(tmp_path / "registry.json")
        info = _launch(registry, 9481, initialize_directory=record)

        assert seen["initializer"] == Path(info.user_data_dir)
        assert stubbed_boundaries["order"] == [
            "cleanup",
            "initializer",
            "preferences",
            "spawn",
        ]

    def test_the_initializer_receives_a_freshly_allocated_directory(
        self, monkeypatch: pytest.MonkeyPatch, stubbed_boundaries: dict[str, object], tmp_path: Path
    ) -> None:
        seen: list[Path] = []

        def record(user_data_dir: Path) -> None:
            seen.append(user_data_dir)
            # Freshly allocated: no Chrome state exists yet, and the only
            # permitted occupant is the initialization marker itself.
            entries = {entry.name for entry in user_data_dir.iterdir()}
            assert entries <= {launcher._INITIALIZING_MARKER}

        registry = str(tmp_path / "registry.json")
        info = _launch(registry, 9482, initialize_directory=record)

        sessions_root = stubbed_boundaries["sessions_root"]
        assert seen == [Path(info.user_data_dir)]
        assert Path(info.user_data_dir).parent == sessions_root


class TestOwnedFailures:
    def test_an_initializer_failure_removes_only_the_freshly_allocated_directory(
        self, monkeypatch: pytest.MonkeyPatch, stubbed_boundaries: dict[str, object], tmp_path: Path
    ) -> None:
        allocated: list[Path] = []

        def failing(user_data_dir: Path) -> None:
            allocated.append(user_data_dir)
            raise ValueError("synthetic initializer failure")

        registry = str(tmp_path / "registry.json")
        with pytest.raises(ValueError, match="synthetic initializer failure"):
            _launch(registry, 9483, initialize_directory=failing)

        assert not allocated[0].exists()

    def test_an_initializer_interruption_removes_the_freshly_allocated_directory(
        self, monkeypatch: pytest.MonkeyPatch, stubbed_boundaries: dict[str, object], tmp_path: Path
    ) -> None:
        allocated: list[Path] = []

        def interrupted(user_data_dir: Path) -> None:
            allocated.append(user_data_dir)
            raise KeyboardInterrupt

        registry = str(tmp_path / "registry.json")
        with pytest.raises(KeyboardInterrupt):
            _launch(registry, 9484, initialize_directory=interrupted)

        assert not allocated[0].exists()

    def test_a_failure_never_removes_a_supplied_user_data_dir(
        self, monkeypatch: pytest.MonkeyPatch, stubbed_boundaries: dict[str, object], tmp_path: Path
    ) -> None:
        supplied = tmp_path / "named-profile"
        supplied.mkdir()
        (supplied / "keep.txt").write_text("profile state")

        def failing(user_data_dir: Path) -> None:
            raise ValueError("synthetic initializer failure")

        registry = str(tmp_path / "registry.json")
        with pytest.raises(ValueError, match="synthetic initializer failure"):
            _launch(registry, 9485, initialize_directory=failing, user_data_dir=str(supplied))

        assert (supplied / "keep.txt").read_text() == "profile state"

    def test_a_preference_failure_removes_the_freshly_allocated_directory(
        self, monkeypatch: pytest.MonkeyPatch, stubbed_boundaries: dict[str, object], tmp_path: Path
    ) -> None:
        def failing_prepare(path) -> None:
            raise RuntimeError("synthetic preference failure")

        monkeypatch.setattr(launcher, "_prepare_preferences", failing_prepare)
        registry = str(tmp_path / "registry.json")

        with pytest.raises(RuntimeError, match="synthetic preference failure"):
            asyncio.run(
                launcher.launch_browser(
                    port_override=9486,
                    binary="synthetic-chrome",
                    headless=True,
                    working_dir=str(tmp_path),
                    registry_path=registry,
                )
            )

        assert list(stubbed_boundaries["sessions_root"].glob("session-*")) == []

    def test_a_failed_cleanup_preserves_the_original_error_and_the_retained_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stubbed_boundaries: dict[str, object],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def failing(user_data_dir: Path) -> None:
            raise ValueError("synthetic initializer failure")

        def failing_rmtree(path, *args, **kwargs):
            raise OSError("synthetic cleanup failure")

        monkeypatch.setattr(launcher.shutil, "rmtree", failing_rmtree)
        registry = str(tmp_path / "registry.json")
        with pytest.raises(ValueError, match="synthetic initializer failure"):
            _launch(registry, 9487, initialize_directory=failing)

        retained = list(stubbed_boundaries["sessions_root"].glob("session-*"))
        assert len(retained) == 1
        assert str(retained[0]) in caplog.text

    def test_a_failure_after_spawn_leaves_the_session_directory_alone(
        self, monkeypatch: pytest.MonkeyPatch, stubbed_boundaries: dict[str, object], tmp_path: Path
    ) -> None:
        dead_process = _FakeProcess(returncode=1)
        registry = str(tmp_path / "registry.json")
        with pytest.raises(RuntimeError, match="Chrome exited immediately"):
            _launch(
                registry,
                9488,
                popen_process=dead_process,
                monkeypatch=monkeypatch,
            )

        assert len(list(stubbed_boundaries["sessions_root"].glob("session-*"))) == 1


class TestInitializationMarker:
    def test_an_active_initializer_is_not_swept_by_another_registry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        sessions_root = tmp_path / "sessions"
        sessions_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(launcher, "_SESSION_ROOT", str(sessions_root))
        other_registry = str(tmp_path / "other-registry.json")
        started = threading.Event()
        release = threading.Event()
        seen: list[Path] = []

        def blocking_initializer(user_data_dir: Path) -> None:
            seen.append(user_data_dir)
            started.set()
            assert release.wait(timeout=10)

        def fake_popen(args, stdout=None, stderr=None, env=None, **kwargs):
            return _FakeProcess()

        async def fake_check_cdp_port_async(*, port):
            return types.SimpleNamespace(listening=True, browser_version="Chrome/999.0.0.0")

        monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(launcher, "check_cdp_port_async", fake_check_cdp_port_async)

        registry = str(tmp_path / "registry.json")
        outcome: list[object] = []

        def run_launch():
            outcome.append(
                asyncio.run(
                    launcher.launch_browser(
                        port_override=9489,
                        binary="synthetic-chrome",
                        headless=True,
                        working_dir=str(tmp_path),
                        registry_path=registry,
                        initialize_directory=blocking_initializer,
                    )
                )
            )

        thread = threading.Thread(target=run_launch)
        thread.start()
        try:
            assert started.wait(timeout=10)
            launcher.cleanup_sessions(registry_path=other_registry)
            assert seen[0].is_dir()
        finally:
            release.set()
            thread.join(timeout=10)

        assert not thread.is_alive()
        assert outcome[0].user_data_dir == str(seen[0])

    def test_a_held_marker_blocks_the_sweep_until_released(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sessions_root = tmp_path / "sessions"
        sessions_root.mkdir(parents=True)
        session = sessions_root / "session-held"
        session.mkdir()
        monkeypatch.setattr(launcher, "_SESSION_ROOT", str(sessions_root))
        marker_fd = launcher._publish_initialization_marker(str(session))
        assert marker_fd is not None
        try:
            launcher.cleanup_sessions(registry_path=str(tmp_path / "registry.json"))
            assert session.is_dir()
        finally:
            launcher._release_initialization_marker(str(session), marker_fd)

        launcher.cleanup_sessions(registry_path=str(tmp_path / "registry.json"))
        assert not session.exists()

    def test_a_stale_marker_permits_the_ordinary_orphan_sweep(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sessions_root = tmp_path / "sessions"
        sessions_root.mkdir(parents=True)
        session = sessions_root / "session-stale"
        session.mkdir()
        # A dead initializer's leftover: published, then the process died and
        # the kernel released the flock.
        (session / launcher._INITIALIZING_MARKER).write_text("")
        monkeypatch.setattr(launcher, "_SESSION_ROOT", str(sessions_root))

        removed = launcher.cleanup_sessions(registry_path=str(tmp_path / "registry.json"))

        assert not session.exists()
        assert removed == []

    def test_a_fifo_marker_neither_blocks_the_sweep_nor_is_followed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sessions_root = tmp_path / "sessions"
        sessions_root.mkdir(parents=True)
        session = sessions_root / "session-fifo"
        session.mkdir()
        os.mkfifo(session / launcher._INITIALIZING_MARKER)
        monkeypatch.setattr(launcher, "_SESSION_ROOT", str(sessions_root))

        launcher.cleanup_sessions(registry_path=str(tmp_path / "registry.json"))

        # Unverifiable rather than dead: the directory is preserved.
        assert session.is_dir()

    def test_a_symlinked_marker_is_never_followed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        sessions_root = tmp_path / "sessions"
        sessions_root.mkdir(parents=True)
        session = sessions_root / "session-link"
        session.mkdir()
        target = tmp_path / "marker-target"
        target.write_text("")
        (session / launcher._INITIALIZING_MARKER).symlink_to(target)
        monkeypatch.setattr(launcher, "_SESSION_ROOT", str(sessions_root))

        launcher.cleanup_sessions(registry_path=str(tmp_path / "registry.json"))

        assert session.is_dir()
        assert target.read_text() == ""

    def test_a_supplied_directory_gets_no_marker(
        self, monkeypatch: pytest.MonkeyPatch, stubbed_boundaries: dict[str, object], tmp_path: Path
    ) -> None:
        supplied = tmp_path / "named-profile"
        supplied.mkdir()
        registry = str(tmp_path / "registry.json")
        info = _launch(
            registry, 9490, initialize_directory=lambda d: None, user_data_dir=str(supplied)
        )
        assert Path(info.user_data_dir) == supplied
        assert not (supplied / launcher._INITIALIZING_MARKER).exists()

    def test_a_successful_launch_leaves_no_marker_behind(
        self, monkeypatch: pytest.MonkeyPatch, stubbed_boundaries: dict[str, object], tmp_path: Path
    ) -> None:
        registry = str(tmp_path / "registry.json")
        info = _launch(registry, 9491, initialize_directory=lambda d: None)
        assert not (Path(info.user_data_dir) / launcher._INITIALIZING_MARKER).exists()


class TestGenericBoundary:
    def test_the_launcher_module_imports_nothing_from_onepassword(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import browser_tools.core.launcher; "
                "assert not [m for m in sys.modules if 'onepassword' in m]",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
