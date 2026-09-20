"""Profile deletion and exclusivity must not trust the registry (#95).

Three paths were built when a profile directory lived in ``/tmp`` and the
operating system cleared it at every reboot. The profile root is about to
become durable (#81), which turns each of them from a nuisance into a way to
lose a login permanently:

- ``cleanup`` protects an entry that carries a ``profile`` field. An old,
  partial or malformed entry can lack that field and still point at the
  profile root, and the vendored sweep deletes the recorded dir for every
  entry it judges dead. RFC-01 defines registry contents as untrusted input,
  so the resolved path decides, not a field.
- ``clean_stale_singleton_lock`` preserves the singleton files whenever *any*
  live process holds the recorded PID. ``/tmp`` used to erase the lock at
  reboot; a durable lock outlives the registry and the operating system can
  reuse the PID for something unrelated.
- Profile exclusivity checks for a holder and then launches. The two steps are
  not atomic, so two concurrent launches can both see no holder.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from browser_tools import lifecycle, process_utils
from browser_tools.core import registry as core_registry

if TYPE_CHECKING:
    from pathlib import Path


def _dead_entry(
    registry_path: str, *, name: str, user_data_dir: Path, profile: object
) -> None:
    """Write a registry entry for a dead instance, with a chosen profile field."""
    path = core_registry._resolve_path(registry_path)  # pyright: ignore[reportPrivateUsage]
    reg = core_registry._load_registry(path)  # pyright: ignore[reportPrivateUsage]
    reg[name] = {
        "port": 9999,
        "pid": 2,  # init; never ours
        "browser_version": "Chrome/153.0.0.0",
        "user_data_dir": str(user_data_dir),
        "launched": "2026-09-20T00:00:00Z",
        "pid_start": "nope",
    }
    if profile is not _ABSENT:
        reg[name]["profile"] = profile
    core_registry._save_registry(reg, path)  # pyright: ignore[reportPrivateUsage]


_ABSENT = object()


def _hold_lock(lock_root: str, started: object, release: object) -> None:
    """Hold the profile launch lock in a child process until told to release.

    Module scope so ``spawn`` can pickle it: the lock must be proven exclusive
    across processes, which is the case a thread-level lock would miss.
    """
    from browser_tools.lifecycle import profile_launch_lock

    with profile_launch_lock("dev", lock_root=lock_root):
        started.set()  # pyright: ignore[reportAttributeAccessIssue]
        release.wait(timeout=10)  # pyright: ignore[reportAttributeAccessIssue]


class TestCleanupTrustsThePathNotTheField:
    """A dir under the resolved profile root survives whatever the field says."""

    @pytest.mark.parametrize(
        ("label", "profile"),
        [("absent", _ABSENT), ("null", None), ("malformed", 17)],
        ids=["profile-field-absent", "profile-field-null", "profile-field-malformed"],
    )
    def test_a_profile_dir_survives_cleanup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, profile: object
    ) -> None:
        profiles_root = tmp_path / "profiles"
        profiles_root.mkdir()
        monkeypatch.setattr(lifecycle, "profiles_root", lambda: profiles_root)

        user_data_dir = profiles_root / "dev"
        user_data_dir.mkdir()
        (user_data_dir / "Cookies").write_bytes(b"login-state")

        registry_path = str(tmp_path / "registry.json")
        core_registry._save_registry({}, registry_path)  # pyright: ignore[reportPrivateUsage]
        _dead_entry(registry_path, name="web-01", user_data_dir=user_data_dir, profile=profile)

        lifecycle.cleanup(registry_path=registry_path)

        assert user_data_dir.exists(), f"cleanup deleted a profile dir ({label} field)"
        assert (user_data_dir / "Cookies").read_bytes() == b"login-state"

    def test_a_dir_outside_the_profile_root_is_still_reaped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hardening must not turn ephemeral sessions into orphans."""
        profiles_root = tmp_path / "profiles"
        profiles_root.mkdir()
        monkeypatch.setattr(lifecycle, "profiles_root", lambda: profiles_root)

        session_dir = tmp_path / "chrome-agent" / "session-abc"
        session_dir.mkdir(parents=True)

        registry_path = str(tmp_path / "registry.json")
        core_registry._save_registry({}, registry_path)  # pyright: ignore[reportPrivateUsage]
        _dead_entry(registry_path, name="web-02", user_data_dir=session_dir, profile=None)

        lifecycle.cleanup(registry_path=registry_path)

        assert not session_dir.exists(), "an unbound session dir was left behind"

    def test_cleanup_prunes_no_profile_on_age(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No age rule exists, and none may be added."""
        profiles_root = tmp_path / "profiles"
        profiles_root.mkdir()
        monkeypatch.setattr(lifecycle, "profiles_root", lambda: profiles_root)

        old = profiles_root / "ancient"
        old.mkdir()
        import os
        os.utime(old, (0, 0))

        registry_path = str(tmp_path / "registry.json")
        core_registry._save_registry({}, registry_path)  # pyright: ignore[reportPrivateUsage]

        lifecycle.cleanup(registry_path=registry_path)

        assert old.exists(), "cleanup pruned a profile by age"


class TestCleanupCorruptionSemanticsAreUnchanged:
    def test_an_unparseable_registry_deletes_nothing(self, tmp_path: Path) -> None:
        registry_path = tmp_path / "registry.json"
        registry_path.write_text("{ not json")

        removed = lifecycle.cleanup(registry_path=str(registry_path))

        assert removed == []
        assert not registry_path.exists(), "the corrupt file should be quarantined"
        assert list(tmp_path.glob("registry.json.corrupt-*")), "no quarantine file"


class TestStaleLockNeedsTheDirectoryHold:
    """A live PID is not enough; it must hold *this* directory."""

    def _lock(self, user_data_dir: Path, pid: int) -> None:
        import os
        for name in ("SingletonCookie", "SingletonSocket"):
            (user_data_dir / name).write_text("x")
        os.symlink(f"host-{pid}", user_data_dir / "SingletonLock")

    def test_a_recycled_pid_holding_another_dir_is_cleaned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_data_dir = tmp_path / "dev"
        user_data_dir.mkdir()
        self._lock(user_data_dir, 4242)

        monkeypatch.setattr(process_utils, "is_process_alive", lambda pid: True)
        monkeypatch.setattr(
            process_utils, "pid_holds_user_data_dir", lambda pid, d: False
        )

        process_utils.clean_stale_singleton_lock(user_data_dir)

        assert not (user_data_dir / "SingletonLock").is_symlink()
        assert not (user_data_dir / "SingletonCookie").exists()
        assert not (user_data_dir / "SingletonSocket").exists()

    def test_a_real_holder_is_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_data_dir = tmp_path / "dev"
        user_data_dir.mkdir()
        self._lock(user_data_dir, 4242)

        monkeypatch.setattr(process_utils, "is_process_alive", lambda pid: True)
        monkeypatch.setattr(
            process_utils, "pid_holds_user_data_dir", lambda pid, d: True
        )

        process_utils.clean_stale_singleton_lock(user_data_dir)

        assert (user_data_dir / "SingletonLock").is_symlink(), "cleaned a live holder"
        assert (user_data_dir / "SingletonCookie").exists()

    def test_a_dead_pid_is_cleaned(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        user_data_dir = tmp_path / "dev"
        user_data_dir.mkdir()
        self._lock(user_data_dir, 4242)

        monkeypatch.setattr(process_utils, "is_process_alive", lambda pid: False)

        process_utils.clean_stale_singleton_lock(user_data_dir)

        assert not (user_data_dir / "SingletonLock").is_symlink()


class TestExclusivityIsAtomic:
    """Holder check and registration happen under one per-profile lock."""

    def test_the_lock_is_exclusive_across_processes(self, tmp_path: Path) -> None:
        import multiprocessing

        lock_dir = tmp_path / "locks"
        lock_dir.mkdir()

        ctx = multiprocessing.get_context("spawn")
        started = ctx.Event()
        release = ctx.Event()
        proc = ctx.Process(target=_hold_lock, args=(str(lock_dir), started, release))
        proc.start()
        try:
            assert started.wait(timeout=10), "child never acquired the lock"
            with (
                pytest.raises(lifecycle.ProfileLockBusy),
                lifecycle.profile_launch_lock(
                    "dev", lock_root=str(lock_dir), timeout=0.5
                ),
            ):
                pass
        finally:
            release.set()
            proc.join(timeout=10)

        # Released: the same lock is takeable again.
        with lifecycle.profile_launch_lock("dev", lock_root=str(lock_dir), timeout=2):
            pass

    def test_a_different_profile_is_not_blocked(self, tmp_path: Path) -> None:
        lock_dir = tmp_path / "locks"
        lock_dir.mkdir()
        with (
            lifecycle.profile_launch_lock("dev", lock_root=str(lock_dir)),
            lifecycle.profile_launch_lock("other", lock_root=str(lock_dir)),
        ):
            pass
