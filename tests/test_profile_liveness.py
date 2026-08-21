"""Tests for #36: engine-aware liveness, profile exclusivity, Camoufox launch.

Everything here runs against an isolated registry and profiles root; no real
browser is launched. The Camoufox host process (``_spawn_camoufox_process``)
and, where liveness itself is not under test, ``instance_is_live`` are stubbed
so the guarantees are asserted at the introspection level.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from browser_tools import lifecycle
from browser_tools.core import registry as core_registry
from browser_tools.lifecycle import ExtendedInstance, LifecycleError


@pytest.fixture
def registry_path(tmp_path):
    return str(tmp_path / "registry.json")


@pytest.fixture(autouse=True)
def _isolate_profiles(monkeypatch, tmp_path):
    # Never touch the default /tmp/browser-tools-profiles.
    monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(tmp_path / "profiles"))


def _ext(
    name="c-01",
    *,
    engine="camoufox",
    profile="dev",
    pid=1234,
    pid_start="tok",
    user_data_dir="/tmp/x",
    port=9300,
) -> ExtendedInstance:
    return ExtendedInstance(
        name=name,
        port=port,
        pid=pid,
        browser_version="",
        user_data_dir=user_data_dir,
        launched=None,
        pid_start=pid_start,
        engine=engine,
        profile=profile,
    )


def _entry(**kw) -> dict:
    base = {
        "port": 9300,
        "pid": 1234,
        "browser_version": "camoufox",
        "user_data_dir": "/tmp/x",
        "launched": "2026-01-01T00:00:00+00:00",
        "pid_start": "tok",
        "engine": "camoufox",
        "profile": "dev",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Engine-aware liveness
# ---------------------------------------------------------------------------


class TestEngineAwareLiveness:
    def test_camoufox_live_when_ours_and_holds_dir(self, monkeypatch):
        monkeypatch.setattr(lifecycle, "process_is_ours", lambda pid, expected_start=None: True)
        monkeypatch.setattr(lifecycle, "pid_holds_user_data_dir", lambda pid, d: True)
        assert lifecycle.instance_is_live(_ext(user_data_dir="/tmp/dev")) is True

    def test_camoufox_pid_reuse_reads_dead(self, monkeypatch):
        # PID reused after reboot: start-time token mismatches -> not ours ->
        # dead, even though some live process happens to hold the dir.
        monkeypatch.setattr(lifecycle, "process_is_ours", lambda pid, expected_start=None: False)
        monkeypatch.setattr(lifecycle, "pid_holds_user_data_dir", lambda pid, d: True)
        assert lifecycle.instance_is_live(_ext()) is False

    def test_camoufox_recycled_pid_holding_other_dir_reads_dead(self, monkeypatch):
        # PID recycled to a live but unrelated process that does not hold our
        # profile dir -> dead (defense in depth beyond process identity).
        monkeypatch.setattr(lifecycle, "process_is_ours", lambda pid, expected_start=None: True)
        monkeypatch.setattr(lifecycle, "pid_holds_user_data_dir", lambda pid, d: False)
        assert lifecycle.instance_is_live(_ext()) is False

    def test_camoufox_without_dir_reads_dead(self):
        assert lifecycle.instance_is_live(_ext(user_data_dir="")) is False

    def test_chrome_dispatches_to_vendored_ladder(self, monkeypatch):
        seen = {}

        def fake(pid, port, pid_start=None, user_data_dir=""):
            seen["args"] = (pid, port, pid_start, user_data_dir)
            return True

        monkeypatch.setattr(core_registry, "_instance_is_alive", fake)
        ext = _ext(engine="chrome", port=9222, user_data_dir="/tmp/s")
        assert lifecycle.instance_is_live(ext) is True
        assert seen["args"] == (1234, 9222, "tok", "/tmp/s")

    def test_chrome_port_reuse_reads_dead(self, monkeypatch):
        # Recorded PID not ours; port listens but a DIFFERENT profile claims it.
        monkeypatch.setattr(core_registry, "process_is_ours", lambda pid, expected_start=None: False)
        monkeypatch.setattr(core_registry, "_port_is_listening", lambda port: True)
        monkeypatch.setattr(core_registry, "_cdp_port_claimants", lambda port: {"/tmp/other"})
        ext = _ext(engine="chrome", user_data_dir="/tmp/mine", port=9222)
        assert lifecycle.instance_is_live(ext) is False

    def test_chrome_pid_reuse_dead_port_reads_dead(self, monkeypatch):
        monkeypatch.setattr(core_registry, "process_is_ours", lambda pid, expected_start=None: False)
        monkeypatch.setattr(core_registry, "_port_is_listening", lambda port: False)
        ext = _ext(engine="chrome", user_data_dir="/tmp/mine", port=9222)
        assert lifecycle.instance_is_live(ext) is False


# ---------------------------------------------------------------------------
# Profile exclusivity
# ---------------------------------------------------------------------------


class TestProfileExclusivity:
    def test_second_launch_on_held_profile_fails_naming_holder(self, registry_path, monkeypatch):
        core_registry._save_registry(
            {"holder-01": _entry(profile="dev", user_data_dir=str(lifecycle.profile_user_data_dir("dev")))},
            registry_path,
        )
        monkeypatch.setattr(lifecycle, "instance_is_live", lambda inst: True)

        def _must_not_launch(*a, **k):
            raise AssertionError("must never launch a second browser on a held profile")

        monkeypatch.setattr(lifecycle, "_spawn_camoufox_process", _must_not_launch)

        with pytest.raises(LifecycleError) as exc:
            lifecycle.launch(engine="camoufox", profile="dev", registry_path=registry_path)
        assert "holder-01" in str(exc.value)

    def test_launch_succeeds_after_holder_dies_and_stale_lock_cleaned(
        self, registry_path, monkeypatch
    ):
        # Dead holder (bogus PID) leaves a stale SingletonLock in the profile dir.
        profile_dir = lifecycle.profile_user_data_dir("dev")
        profile_dir.mkdir(parents=True)
        lock = profile_dir / "SingletonLock"
        lock.symlink_to("host-2000000000")  # points at a dead PID
        core_registry._save_registry(
            {"holder-01": _entry(profile="dev", pid=2_000_000_000, pid_start=None,
                                 user_data_dir=str(profile_dir))},
            registry_path,
        )
        monkeypatch.setattr(
            lifecycle, "_spawn_camoufox_process", lambda user_data_dir, headless: (5555, "tok2")
        )

        inst = lifecycle.launch(engine="camoufox", profile="dev", registry_path=registry_path)
        assert inst.engine == "camoufox"
        assert inst.pid == 5555
        # The stale singleton lock was cleaned before the exclusivity check.
        assert not lock.exists()


# ---------------------------------------------------------------------------
# Camoufox launch + registration
# ---------------------------------------------------------------------------


class TestCamoufoxRegistration:
    def test_registers_engine_camoufox_and_reports_live_via_hold(
        self, registry_path, monkeypatch
    ):
        monkeypatch.setattr(
            lifecycle, "_spawn_camoufox_process", lambda user_data_dir, headless: (6161, "tok")
        )
        inst = lifecycle.launch(engine="camoufox", profile="dev", registry_path=registry_path)
        assert inst.engine == "camoufox"

        raw = json.loads(Path(registry_path).read_text())
        assert raw[inst.name]["engine"] == "camoufox"
        assert raw[inst.name]["pid"] == 6161
        assert raw[inst.name]["profile"] == "dev"
        # The bound user-data-dir is the persistent profile dir.
        assert raw[inst.name]["user_data_dir"] == str(lifecycle.profile_user_data_dir("dev"))

        # status reports it live via the user-data-dir hold, with no CDP targets.
        monkeypatch.setattr(lifecycle, "process_is_ours", lambda pid, expected_start=None: True)
        monkeypatch.setattr(lifecycle, "pid_holds_user_data_dir", lambda pid, d: True)
        rows = lifecycle.status(registry_path=registry_path)
        row = next(r for r in rows if r["name"] == inst.name)
        assert row["alive"] is True
        assert row["engine"] == "camoufox"
        assert row["targets"] == []

    def test_unbound_camoufox_gets_ephemeral_dir(self, registry_path, monkeypatch):
        monkeypatch.setattr(
            lifecycle, "_spawn_camoufox_process", lambda user_data_dir, headless: (6262, "tok")
        )
        inst = lifecycle.launch(engine="camoufox", registry_path=registry_path)
        assert inst.profile is None
        assert ".ephemeral" in inst.user_data_dir


# ---------------------------------------------------------------------------
# Profile persistence vs. reaping across stop / cleanup
# ---------------------------------------------------------------------------


class TestProfilePersistence:
    def test_stop_preserves_profile_dir(self, registry_path, monkeypatch):
        profile_dir = lifecycle.profile_user_data_dir("keep")
        profile_dir.mkdir(parents=True)
        (profile_dir / "marker").write_text("x")
        core_registry._save_registry(
            {"keep-01": _entry(profile="keep", user_data_dir=str(profile_dir))}, registry_path
        )
        monkeypatch.setattr(lifecycle, "instance_is_live", lambda inst: True)
        monkeypatch.setattr(lifecycle, "_terminate_verified", lambda ext: None)

        msg = lifecycle.stop(instance="keep-01", registry_path=registry_path)
        assert "keep-01" in msg
        assert profile_dir.exists()
        assert (profile_dir / "marker").exists()
        assert json.loads(Path(registry_path).read_text()) == {}

    def test_stop_reaps_unbound_camoufox_dir(self, registry_path, monkeypatch, tmp_path):
        eph = tmp_path / "eph"
        eph.mkdir()
        core_registry._save_registry(
            {"eph-01": _entry(profile=None, user_data_dir=str(eph))}, registry_path
        )
        monkeypatch.setattr(lifecycle, "instance_is_live", lambda inst: True)
        monkeypatch.setattr(lifecycle, "_terminate_verified", lambda ext: None)

        lifecycle.stop(instance="eph-01", registry_path=registry_path)
        assert not eph.exists()

    def test_cleanup_preserves_dead_profile_dir(self, registry_path):
        profile_dir = lifecycle.profile_user_data_dir("keep2")
        profile_dir.mkdir(parents=True)
        core_registry._save_registry(
            {"keep2-01": _entry(profile="keep2", pid=2_000_000_000, pid_start=None,
                                user_data_dir=str(profile_dir))},
            registry_path,
        )
        removed = lifecycle.cleanup(registry_path=registry_path)
        assert "keep2-01" in removed
        assert profile_dir.exists()  # dead profile-bound dir preserved
        assert json.loads(Path(registry_path).read_text()) == {}


# ---------------------------------------------------------------------------
# Registry corruption: unknown vs retired
# ---------------------------------------------------------------------------


class TestCorruptRegistry:
    @staticmethod
    def _corrupt(registry_path):
        Path(registry_path).write_text("{ this is not json")

    def test_status_reports_unknown(self, registry_path):
        self._corrupt(registry_path)
        rows = lifecycle.status(registry_path=registry_path)
        assert len(rows) == 1
        assert rows[0]["status"] == "unknown"

    def test_stop_refuses_to_signal(self, registry_path):
        self._corrupt(registry_path)
        with pytest.raises(LifecycleError) as exc:
            lifecycle.stop(instance="whatever", registry_path=registry_path)
        assert "unparseable" in str(exc.value).lower()

    def test_cleanup_deletes_nothing_and_quarantines(self, registry_path):
        # A session dir that a naive cleanup might sweep must survive.
        self._corrupt(registry_path)
        removed = lifecycle.cleanup(registry_path=registry_path)
        assert removed == []
        assert not Path(registry_path).exists()  # corrupt file moved aside
        quarantines = list(Path(registry_path).parent.glob("registry.json.corrupt-*"))
        assert quarantines

    def test_parseable_missing_instance_is_retired_not_unknown(self, registry_path):
        core_registry._save_registry({"other-01": _entry(profile=None)}, registry_path)
        with pytest.raises(LifecycleError) as exc:
            lifecycle.stop(instance="ghost", registry_path=registry_path)
        # A retired instance is a not-found error, never the corruption message.
        assert "unparseable" not in str(exc.value).lower()
        assert "ghost" in str(exc.value)
