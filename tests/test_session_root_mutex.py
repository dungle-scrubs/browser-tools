"""The session-root mutex closes the publication/deletion race (#201, S4-1).

Deterministic kernel-boundary witnesses for the shared synchronization
between a launch's visible allocation-plus-marker-publication and an orphan
sweep's check-and-delete transaction. The parent's frozen witness
(``evidence/publication-race-witness.py`` in the task folder) proved the
race: a sweep that observed no marker could pause, let the creator publish
the marker and prepare files, then delete the directory despite the held
lease. Here the same interleaving is forced with real kernel flock on the
real mutex file and thread events; nothing about the marker, the sweep, or
the launch is mocked into reporting a desired fact - the hooks only pause
threads at protocol boundaries and record the order they ran in.

Every test isolates both default-registry aliases (round-4 tests F1) and
stubs the OS-environment copy boundary, so no developer registry or real
environment value is ever read, copied, or printed, including in the
failure traces of a red run.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import stat
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from launcher_spawn_helpers import discard_spawn_environment

from browser_tools import session_initialization
from browser_tools.core import launcher
from browser_tools.core.connection import PortStatus

if TYPE_CHECKING:
    from collections.abc import Callable

#: The mutex file's protocol name, pinned at the kernel boundary these
#: witnesses hook into. A test below keeps it identical to the source
#: constant, so the two cannot drift apart.
SESSION_ROOT_MUTEX = ".bt-session-root.lock"

#: How long a witness waits for a coordinated thread before it concludes the
#: coordination itself failed. Long enough for a loaded machine, short
#: enough to keep a red run fast; never a retry that makes a race pass.
_COORDINATION_TIMEOUT = 10.0


class _FakeProcess:
    """Stands in for the spawned Chrome process."""

    pid = 424242

    def poll(self) -> int | None:
        return None


class _Recorder:
    """A thread-safe ordered event log."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self._lock = threading.Lock()

    def record(self, tag: str) -> None:
        with self._lock:
            self.events.append(tag)

    def before(self, first: str, second: str) -> None:
        """Assert ``first`` ran before ``second`` in the ordered log."""
        with self._lock:
            names = list(self.events)
        assert first in names, f"{first} never ran; saw {names}"
        assert second in names, f"{second} never ran; saw {names}"
        assert names.index(first) < names.index(second), (
            f"expected {first} before {second}, saw {names}"
        )


def _try_lock_mutex(root: Path) -> bool:
    """Report whether the session-root mutex can be locked right now.

    The witness's own kernel probe: a real ``flock`` on the real mutex file,
    non-blocking, releasing whatever it took. This is the boundary the two
    critical sections exclude each other at; no double ever reports it.
    """
    fd = os.open(root / SESSION_ROOT_MUTEX, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def _spawn_double(synthetic_env: dict[str, str]) -> Callable[..., _FakeProcess]:
    """A spawn double that discards the environment before anything else."""

    def spawn(args: list[str], **kwargs: object) -> _FakeProcess:
        discard_spawn_environment(kwargs.pop("env", None), synthetic_env)
        if args[0] != "synthetic-chrome":
            raise OSError("synthetic process metadata unavailable")
        return _FakeProcess()

    return spawn


async def _ready(*, port: int) -> PortStatus:
    return PortStatus(listening=True, browser_version="Chrome/999")


def _plant_orphan(root: Path, name: str, *, stale_marker: bool = False) -> Path:
    """Create a plain reapable orphan session directory under ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    orphan = root / name
    orphan.mkdir()
    if stale_marker:
        # A dead initializer's leftover: published, then the process died and
        # the kernel released the flock.
        (orphan / session_initialization.INITIALIZING_MARKER).write_text("")
    return orphan


def _blocking_mutex(root: Path) -> Path:
    """Make the mutex unacquirable: a directory where the file must be."""
    root.mkdir(parents=True, exist_ok=True)
    blocked = root / SESSION_ROOT_MUTEX
    blocked.mkdir()
    return blocked


def _launch_kwargs(tmp_path: Path, port: int) -> dict[str, object]:
    """The shared synthetic launch arguments every witness uses."""
    return {
        "binary": "synthetic-chrome",
        "port_override": port,
        "headless": True,
        "window_border": False,
        "working_dir": str(tmp_path),
        "registry_path": str(tmp_path / "creator.json"),
    }


class _LaunchThread:
    """A creator thread running one real ``launch_browser`` call."""

    def __init__(
        self,
        name: str,
        tmp_path: Path,
        port: int,
        initialize_directory: Callable[[Path], None] | None,
    ) -> None:
        self.outcomes: list[object] = []
        self.errors: list[BaseException] = []
        self._started = False
        self._thread = threading.Thread(target=self._run, name=name)
        self._kwargs = _launch_kwargs(tmp_path, port)
        if initialize_directory is not None:
            self._kwargs["initialize_directory"] = initialize_directory

    def _run(self) -> None:
        try:
            self.outcomes.append(asyncio.run(launcher.launch_browser(**self._kwargs)))
        except BaseException as exc:  # recorded and re-raised by the witness
            self.errors.append(exc)

    def start(self) -> None:
        self._started = True
        self._thread.start()

    def join(self) -> None:
        if not self._started:
            return
        self._thread.join(15)
        assert not self._thread.is_alive(), f"creator {self._thread.name} never finished"

    def result(self) -> object:
        if self.errors:
            raise self.errors[0]
        assert self.outcomes, "the creator finished without an outcome"
        return self.outcomes[0]


class TestProtocol:
    def test_the_witness_name_matches_the_source_constant(self) -> None:
        """The pinned kernel-boundary name is the source protocol's name."""
        assert session_initialization.SESSION_ROOT_MUTEX_NAME == SESSION_ROOT_MUTEX

    def test_the_mutex_file_is_a_plain_empty_regular_file(self, tmp_path: Path) -> None:
        """Created by the protocol itself: mode 0600, empty, never a journal."""
        fd = session_initialization.acquire_session_root_mutex(str(tmp_path))
        try:
            info = os.fstat(fd)
            assert stat.S_ISREG(info.st_mode)
            assert info.st_size == 0
            assert stat.S_IMODE(info.st_mode) == 0o600
        finally:
            session_initialization.release_session_root_mutex(fd)


class TestPublicationRace:
    def test_a_sweep_cannot_decide_on_a_directory_before_its_marker_is_published(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        isolated_default_registry: Path,
        synthetic_spawn_environment: dict[str, str],
    ) -> None:
        """The S4-1 interleaving, forced deterministically.

        The creator pauses between visible allocation and marker publication
        (inside the mutex's critical section). A sweep running then must be
        excluded: the kernel holds the mutex, and the sweep's marker
        observation can only happen after publication, finding the marker
        present and held, so the prepared files survive the sweep.
        """
        root = tmp_path / "sessions"
        monkeypatch.setattr(launcher, "_SESSION_ROOT", str(root))
        recorder = _Recorder()
        in_critical = threading.Event()
        release_creator = threading.Event()
        sweep_done = threading.Event()
        real_open = os.open
        staging_prefix = f"{session_initialization.INITIALIZING_MARKER}-"
        marker_name = session_initialization.INITIALIZING_MARKER

        def opening(path, *args, **kwargs):
            base = os.path.basename(os.fsdecode(path))
            thread = threading.current_thread().name
            if thread == "creator" and base.startswith(staging_prefix):
                recorder.record("creator-critical")
                in_critical.set()
                assert release_creator.wait(_COORDINATION_TIMEOUT), (
                    "the test never released the creator's critical section"
                )
            if thread == "sweeper" and base == marker_name:
                fd = real_open(path, *args, **kwargs)
                recorder.record("sweep-observed-marker")
                return fd
            return real_open(path, *args, **kwargs)

        def initialize(directory: Path) -> None:
            (directory / "prepared-registration.json").write_text('{"synthetic": true}')
            recorder.record("initializer")
            # Hold the per-directory lease until the sweep has finished, so
            # the sweep's observation deterministically meets a held marker
            # rather than racing the launch to its end.
            assert sweep_done.wait(_COORDINATION_TIMEOUT), "the sweep never finished"

        creator = _LaunchThread("creator", tmp_path, 9501, initialize)

        def sweep() -> None:
            try:
                launcher.cleanup_sessions(registry_path=str(tmp_path / "sweeper.json"))
            finally:
                sweep_done.set()

        sweeper = threading.Thread(target=sweep, name="sweeper")
        sweeper_started = False
        monkeypatch.setattr(os, "open", opening)
        monkeypatch.setattr(launcher.subprocess, "Popen", _spawn_double(synthetic_spawn_environment))
        monkeypatch.setattr(launcher, "check_cdp_port_async", _ready)

        creator.start()
        try:
            assert in_critical.wait(_COORDINATION_TIMEOUT), (
                "the creator never reached its critical section"
            )
            # Kernel witness: with the creator paused between allocation and
            # publication, the mutex is held, so no sweep can enter its
            # check-and-delete transaction.
            assert not _try_lock_mutex(root), (
                "the session-root mutex was free while the creator sat between "
                "visible allocation and marker publication"
            )
            sweeper.start()
            sweeper_started = True
        finally:
            release_creator.set()
            creator.join()
            if sweeper_started:
                sweeper.join(15)
                assert not sweeper.is_alive(), "the sweeper never finished"

        instance = creator.result()
        # The sweep's marker observation could only follow publication (the
        # creator left the critical section first), and it found the marker.
        recorder.before("creator-critical", "sweep-observed-marker")
        # The held lease won: the prepared files and the directory survived.
        session_dir = Path(instance.user_data_dir)
        assert (session_dir / "prepared-registration.json").is_file()
        assert session_dir.is_dir()
        # The mutex is free again once both critical sections ended.
        assert _try_lock_mutex(root)

    def test_a_sweep_mid_transaction_blocks_a_launchs_allocation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        isolated_default_registry: Path,
        synthetic_spawn_environment: dict[str, str],
    ) -> None:
        """The exclusion is mutual: a sweep paused inside its check-and-delete
        transaction holds the mutex, and a concurrent launch's allocation and
        publication critical section can only begin after it."""
        root = tmp_path / "sessions"
        orphan = _plant_orphan(root, "session-orphan", stale_marker=True)
        monkeypatch.setattr(launcher, "_SESSION_ROOT", str(root))
        recorder = _Recorder()
        in_transaction = threading.Event()
        release_sweep = threading.Event()
        real_rmtree = launcher.shutil.rmtree

        def pausing_rmtree(path, *args, **kwargs):
            if (
                threading.current_thread().name == "sweeper"
                and os.fspath(path).endswith("session-orphan")
            ):
                recorder.record("sweep-transaction")
                in_transaction.set()
                assert release_sweep.wait(_COORDINATION_TIMEOUT), (
                    "the test never released the sweep's transaction"
                )
            real_rmtree(path, *args, **kwargs)

        def initialize(directory: Path) -> None:
            recorder.record("initializer")

        creator = _LaunchThread("creator", tmp_path, 9502, initialize)

        def sweep() -> None:
            launcher.cleanup_sessions(registry_path=str(tmp_path / "sweeper.json"))

        sweeper = threading.Thread(target=sweep, name="sweeper")
        monkeypatch.setattr(launcher.shutil, "rmtree", pausing_rmtree)
        monkeypatch.setattr(launcher.subprocess, "Popen", _spawn_double(synthetic_spawn_environment))
        monkeypatch.setattr(launcher, "check_cdp_port_async", _ready)

        sweeper.start()
        try:
            assert in_transaction.wait(_COORDINATION_TIMEOUT), (
                "the sweep never reached its check-and-delete transaction"
            )
            # Kernel witness: the sweep holds the mutex mid-transaction.
            assert not _try_lock_mutex(root), (
                "the session-root mutex was free while the sweep sat inside "
                "its check-and-delete transaction"
            )
            creator.start()
        finally:
            release_sweep.set()
            sweeper.join(15)
            assert not sweeper.is_alive(), "the sweeper never finished"
            creator.join()

        instance = creator.result()
        # The orphan was reaped inside the transaction; the launch's own
        # allocation critical section (its initializer follows publication)
        # began only afterwards, and its session survived.
        assert not orphan.exists()
        recorder.before("sweep-transaction", "initializer")
        assert Path(instance.user_data_dir).is_dir()


class TestCriticalSectionBoundaries:
    def test_the_critical_section_ends_before_the_callback_and_a_sweep_still_completes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        isolated_default_registry: Path,
        synthetic_spawn_environment: dict[str, str],
    ) -> None:
        """The mutex covers allocation and publication only: while the
        initializer callback runs, the mutex is free (a probe locks it), a
        real sweep completes rather than deadlocking, the held directory
        stays, and a stale-marker orphan in the same sweep is still reaped."""
        root = tmp_path / "sessions"
        stale = _plant_orphan(root, "session-stale", stale_marker=True)
        monkeypatch.setattr(launcher, "_SESSION_ROOT", str(root))
        recorder = _Recorder()
        callback_started = threading.Event()
        release_initializer = threading.Event()

        def initialize(directory: Path) -> None:
            recorder.record("callback")
            callback_started.set()
            assert release_initializer.wait(_COORDINATION_TIMEOUT), (
                "the test never released the initializer"
            )
            (directory / "prepared-registration.json").write_text('{"synthetic": true}')

        creator = _LaunchThread("creator", tmp_path, 9503, initialize)
        monkeypatch.setattr(launcher.subprocess, "Popen", _spawn_double(synthetic_spawn_environment))
        monkeypatch.setattr(launcher, "check_cdp_port_async", _ready)

        creator.start()
        try:
            assert callback_started.wait(_COORDINATION_TIMEOUT), (
                "the initializer never started"
            )
            # Kernel witness: the mutex is free while only the callback runs.
            assert _try_lock_mutex(root), (
                "the session-root mutex was still held while the initializer "
                "callback ran; the critical section must end before it"
            )
            # The real sweep completes while the initializer is still blocked.
            removed = launcher.cleanup_sessions(registry_path=str(tmp_path / "other.json"))
            recorder.record("sweep-completed")
            assert removed == []
            assert not stale.exists(), "a stale-marker orphan was not reaped"
        finally:
            release_initializer.set()
            creator.join()

        instance = creator.result()
        session_dir = Path(instance.user_data_dir)
        assert session_dir.is_dir()
        assert (session_dir / "prepared-registration.json").is_file()
        recorder.before("callback", "sweep-completed")


class TestInabilityPolicies:
    def test_a_sweep_that_cannot_hold_the_mutex_skips_orphan_removal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        isolated_default_registry: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Sweep inability skips the unsafe walk; it never blindly deletes."""
        root = tmp_path / "sessions"
        orphan = _plant_orphan(root, "session-orphan")
        _blocking_mutex(root)
        monkeypatch.setattr(launcher, "_SESSION_ROOT", str(root))

        with caplog.at_level("WARNING"):
            removed = launcher.cleanup_sessions(registry_path=str(tmp_path / "sweeper.json"))

        assert removed == []
        assert orphan.is_dir(), "a sweep that could not hold the mutex still deleted"
        assert "Skipping the orphan sweep" in caplog.text

    def test_a_launch_that_cannot_hold_the_mutex_fails_operationally(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        isolated_default_registry: Path,
        synthetic_spawn_environment: dict[str, str],
    ) -> None:
        """Creator inability aborts before the initializer or any spawn; a
        session this call cannot protect never runs unprotected."""
        root = tmp_path / "sessions"
        _blocking_mutex(root)
        monkeypatch.setattr(launcher, "_SESSION_ROOT", str(root))
        ran: list[str] = []

        def initialize(directory: Path) -> None:
            ran.append("initializer")

        def spawn(args: list[str], **kwargs: object) -> _FakeProcess:
            discard_spawn_environment(kwargs.pop("env", None), synthetic_spawn_environment)
            ran.append("spawn")
            return _FakeProcess()

        monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
        monkeypatch.setattr(launcher, "check_cdp_port_async", _ready)

        with pytest.raises(OSError):
            asyncio.run(launcher.launch_browser(**_launch_kwargs(tmp_path, 9504) | {
                "initialize_directory": initialize,
            }))

        assert ran == []
        assert list(root.glob("session-*")) == []

    def test_a_plain_launch_without_an_initializer_never_takes_the_mutex(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        isolated_default_registry: Path,
        synthetic_spawn_environment: dict[str, str],
    ) -> None:
        """No-callback semantics: a plain launch ignores the mutex entirely,
        so an unacquirable mutex cannot break an ordinary launch."""
        root = tmp_path / "sessions"
        blocked = _blocking_mutex(root)
        monkeypatch.setattr(launcher, "_SESSION_ROOT", str(root))
        monkeypatch.setattr(launcher.subprocess, "Popen", _spawn_double(synthetic_spawn_environment))
        monkeypatch.setattr(launcher, "check_cdp_port_async", _ready)

        instance = asyncio.run(launcher.launch_browser(**_launch_kwargs(tmp_path, 9505)))

        assert Path(instance.user_data_dir).is_dir()
        assert blocked.is_dir(), "a plain launch disturbed the session-root mutex"


class TestSweepRegistryIsolation:
    """The sweep honors the default registry only through the isolated
    aliases (round-4 tests F1): both synthetic, never the developer's."""

    @staticmethod
    def _tracking_registry(synthetic_default: Path, orphan: Path) -> None:
        synthetic_default.write_text(
            json.dumps(
                {
                    "tracked": {
                        "port": 9506,
                        "pid": 999999,
                        "user_data_dir": str(orphan),
                    }
                }
            )
        )

    def test_the_sweep_honors_the_isolated_default_registry_alias(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        isolated_default_registry: Path,
    ) -> None:
        root = tmp_path / "sessions"
        orphan = _plant_orphan(root, "session-tracked")
        self._tracking_registry(isolated_default_registry, orphan)
        monkeypatch.setattr(launcher, "_SESSION_ROOT", str(root))

        launcher.cleanup_sessions(registry_path=str(tmp_path / "sweeper.json"))

        # The sweep consulted the launcher alias's synthetic registry (the
        # orphan is tracked there), not the developer's default registry.
        assert orphan.is_dir()

    def test_a_none_registry_resolves_through_the_core_alias(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        isolated_default_registry: Path,
    ) -> None:
        """A None registry path resolves through ``core.registry``'s alias.

        The dead synthetic entry is reaped and named by the cleanup, which is
        exactly what consulting the synthetic None-resolved registry means; a
        sweep that had read the developer's registry instead would have had
        nothing to remove and nothing to name.
        """
        root = tmp_path / "sessions"
        orphan = _plant_orphan(root, "session-tracked")
        self._tracking_registry(isolated_default_registry, orphan)
        monkeypatch.setattr(launcher, "_SESSION_ROOT", str(root))

        removed = launcher.cleanup_sessions(registry_path=None)

        assert removed == ["tracked"]
        assert not orphan.exists()
