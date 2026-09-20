"""The profile root is durable storage, and what is in /tmp moves there (#81).

A named profile holds a login. Under ``/tmp`` macOS deleted every signed-in
session at boot and on its periodic sweep, and the failure was silent: the
browser turned up logged out days later with no error anywhere. A
``shopify-admin`` profile created 2026-09-08 was found recreated empty after a
reboot on 2026-09-19.

The move is the dangerous half. It runs against directories holding the user's
authenticated sessions, on a code path where two vendored functions already
``rmtree`` whatever a registry entry names, and once the profiles have moved on
disk, reverting the code does not move them back. So: the transfer is verified
before the source is removed, it refuses a live holder and a collision rather
than merging, and the reverse migration ships with the forward one.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from browser_tools import cli, lifecycle
from browser_tools.core import registry as core_registry

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A durable root and a legacy root, both isolated from the machine's."""
    durable = tmp_path / "durable"
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(durable))
    monkeypatch.setenv(lifecycle.LEGACY_PROFILES_ENV_VAR, str(legacy))
    monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", str(tmp_path / "registry.json"))
    return durable, legacy


def _profile_with_a_login(root: Path, name: str, cookies: bytes = b"login-state") -> Path:
    """A profile directory shaped like a real Chrome one."""
    profile = root / name
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_bytes(cookies)
    (profile / "Default" / "Preferences").write_text('{"x": 1}')
    (profile / "Local State").write_text("{}")
    return profile


# --------------------------------------------------------------------------- #
# The precedence
# --------------------------------------------------------------------------- #


class TestTheRootResolvesByPrecedence:
    def test_the_explicit_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(tmp_path / "explicit"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        assert lifecycle.profiles_root() == tmp_path / "explicit"

    def test_xdg_data_home_is_next(self, monkeypatch, tmp_path):
        monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, "")
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        assert lifecycle.profiles_root() == tmp_path / "xdg" / "browser-tools" / "profiles"

    def test_the_default_is_under_local_share(self, monkeypatch, tmp_path):
        monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, "")
        monkeypatch.setenv("XDG_DATA_HOME", "")
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        assert (
            lifecycle.profiles_root()
            == tmp_path / ".local" / "share" / "browser-tools" / "profiles"
        )

    def test_an_empty_value_falls_through_at_every_level(self, monkeypatch, tmp_path):
        monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, "")
        monkeypatch.setenv("XDG_DATA_HOME", "")
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        assert lifecycle.profiles_root().is_relative_to(tmp_path)

    def test_the_default_is_not_in_a_temp_root(self, monkeypatch, tmp_path):
        """The whole point: the operating system must not clear it."""
        monkeypatch.delenv(lifecycle.PROFILES_ENV_VAR, raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        assert not str(lifecycle.profiles_root()).startswith("/tmp")

    def test_the_registry_stays_in_tmp(self, monkeypatch):
        """A cleared registry after a reboot is self-consistent; a profile is not."""
        monkeypatch.delenv("BROWSER_TOOLS_REGISTRY", raising=False)
        assert lifecycle.registry_path_from_env() is None


class TestUnboundSessionsStayTemporary:
    def test_the_ephemeral_root_is_outside_the_profile_root(self, roots):
        durable, _ = roots
        assert not lifecycle.ephemeral_root().is_relative_to(durable)
        assert str(lifecycle.ephemeral_root()).startswith("/tmp")


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #


class TestMigration:
    def test_every_legacy_profile_moves_with_its_login(self, roots):
        durable, legacy = roots
        _profile_with_a_login(legacy, "work", b"work-cookies")
        _profile_with_a_login(legacy, "play", b"play-cookies")

        result = lifecycle.migrate_profiles()

        assert sorted(entry["name"] for entry in result["migrated"]) == ["play", "work"]
        assert result["refused"] == []
        assert (durable / "work" / "Default" / "Cookies").read_bytes() == b"work-cookies"
        assert (durable / "play" / "Default" / "Cookies").read_bytes() == b"play-cookies"
        assert (durable / "work" / "Local State").is_file()

    def test_the_source_is_gone_afterwards(self, roots):
        _, legacy = roots
        _profile_with_a_login(legacy, "work")
        lifecycle.migrate_profiles()
        assert not (legacy / "work").exists()

    def test_it_is_idempotent(self, roots):
        durable, legacy = roots
        _profile_with_a_login(legacy, "work")
        lifecycle.migrate_profiles()
        second = lifecycle.migrate_profiles()
        assert second["migrated"] == []
        assert second["refused"] == []
        assert (durable / "work" / "Default" / "Cookies").is_file()

    def test_a_dry_run_moves_nothing(self, roots):
        durable, legacy = roots
        _profile_with_a_login(legacy, "work")
        result = lifecycle.migrate_profiles(dry_run=True)
        assert [entry["name"] for entry in result["migrated"]] == ["work"]
        assert (legacy / "work").is_dir()
        assert not durable.exists() or not (durable / "work").exists()

    def test_a_collision_is_refused_not_merged(self, roots):
        durable, legacy = roots
        _profile_with_a_login(legacy, "work", b"old")
        _profile_with_a_login(durable, "work", b"new")

        result = lifecycle.migrate_profiles()

        assert result["migrated"] == []
        assert result["refused"][0]["name"] == "work"
        assert "does not merge" in result["refused"][0]["reason"]
        assert (durable / "work" / "Default" / "Cookies").read_bytes() == b"new"
        assert (legacy / "work" / "Default" / "Cookies").read_bytes() == b"old"

    def test_a_live_holder_is_refused(self, roots, monkeypatch):
        _durable, legacy = roots
        _profile_with_a_login(legacy, "work")
        monkeypatch.setattr(
            lifecycle, "find_profile_holder", lambda name, registry_path=None: "web-01"
        )
        result = lifecycle.migrate_profiles()
        assert result["migrated"] == []
        assert "web-01" in result["refused"][0]["reason"]
        assert "bt stop web-01" in result["refused"][0]["reason"]
        assert (legacy / "work" / "Default" / "Cookies").is_file()

    def test_one_refusal_does_not_stop_the_others(self, roots):
        durable, legacy = roots
        _profile_with_a_login(legacy, "blocked", b"old")
        _profile_with_a_login(durable, "blocked", b"new")
        _profile_with_a_login(legacy, "clear")

        result = lifecycle.migrate_profiles()

        assert [entry["name"] for entry in result["migrated"]] == ["clear"]
        assert [entry["name"] for entry in result["refused"]] == ["blocked"]

    def test_a_failed_transfer_leaves_the_source_untouched(self, roots, monkeypatch):
        """The login must stay readable at its old path until the new one verifies."""
        durable, legacy = roots
        _profile_with_a_login(legacy, "work", b"precious")

        def _broken(source, destination):
            raise lifecycle.ProfileMigrationError("Default/Cookies does not match")

        monkeypatch.setattr(lifecycle, "_verify_transfer", _broken)
        result = lifecycle.migrate_profiles()

        assert result["migrated"] == []
        assert "does not match" in result["refused"][0]["reason"]
        assert (legacy / "work" / "Default" / "Cookies").read_bytes() == b"precious"
        assert not (durable / "work").exists()

    def test_no_staging_directory_survives_a_failure(self, roots, monkeypatch):
        durable, legacy = roots
        _profile_with_a_login(legacy, "work")
        monkeypatch.setattr(
            lifecycle,
            "_verify_transfer",
            lambda source, destination: (_ for _ in ()).throw(
                lifecycle.ProfileMigrationError("no")
            ),
        )
        lifecycle.migrate_profiles()
        leftovers = (
            [p.name for p in durable.iterdir()] if durable.exists() else []
        )
        assert leftovers == []

    def test_a_staging_directory_is_never_read_as_a_profile(self, roots):
        durable, _legacy = roots
        durable.mkdir(parents=True)
        (durable / f"{lifecycle.MIGRATION_STAGING_PREFIX}work-123").mkdir()
        assert lifecycle.profile_list() == []

    def test_a_symlink_is_not_migrated(self, roots, tmp_path):
        _durable, legacy = roots
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (legacy / "escape").symlink_to(outside, target_is_directory=True)
        result = lifecycle.migrate_profiles()
        assert result["migrated"] == []
        assert outside.is_dir()

    def test_nothing_happens_when_the_roots_are_the_same(self, tmp_path, monkeypatch):
        root = tmp_path / "same"
        root.mkdir()
        _profile_with_a_login(root, "work")
        monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(root))
        monkeypatch.setenv(lifecycle.LEGACY_PROFILES_ENV_VAR, str(root))
        result = lifecycle.migrate_profiles()
        assert result["migrated"] == []
        assert (root / "work" / "Default" / "Cookies").is_file()


class TestTheReverseMigration:
    """Reverting the code does not move the profiles back; this does."""

    def test_it_moves_them_back(self, roots):
        durable, legacy = roots
        _profile_with_a_login(legacy, "work", b"login")
        lifecycle.migrate_profiles()
        assert (durable / "work").is_dir()

        result = lifecycle.migrate_profiles(back=True)

        assert [entry["name"] for entry in result["migrated"]] == ["work"]
        assert (legacy / "work" / "Default" / "Cookies").read_bytes() == b"login"
        assert not (durable / "work").exists()

    def test_it_refuses_a_collision_too(self, roots):
        durable, legacy = roots
        _profile_with_a_login(durable, "work", b"new")
        _profile_with_a_login(legacy, "work", b"old")
        result = lifecycle.migrate_profiles(back=True)
        assert result["migrated"] == []
        assert (legacy / "work" / "Default" / "Cookies").read_bytes() == b"old"


class TestVerification:
    def test_a_missing_file_fails_verification(self, roots, tmp_path):
        _, legacy = roots
        source = _profile_with_a_login(legacy, "work")
        partial = tmp_path / "partial"
        (partial / "Default").mkdir(parents=True)
        (partial / "Default" / "Cookies").write_bytes(b"login-state")
        with pytest.raises(lifecycle.ProfileMigrationError) as exc:
            lifecycle._verify_transfer(source, partial)  # pyright: ignore[reportPrivateUsage]
        assert "missing" in str(exc.value)

    def test_a_differing_size_fails_verification(self, roots, tmp_path):
        _, legacy = roots
        source = _profile_with_a_login(legacy, "work")
        partial = tmp_path / "partial"
        (partial / "Default").mkdir(parents=True)
        (partial / "Default" / "Cookies").write_bytes(b"short")
        (partial / "Default" / "Preferences").write_text('{"x": 1}')
        (partial / "Local State").write_text("{}")
        with pytest.raises(lifecycle.ProfileMigrationError) as exc:
            lifecycle._verify_transfer(source, partial)  # pyright: ignore[reportPrivateUsage]
        assert "different size" in str(exc.value)

    def test_cookies_are_compared_byte_for_byte(self, roots, tmp_path):
        _, legacy = roots
        source = _profile_with_a_login(legacy, "work", b"aaaa")
        corrupt = tmp_path / "corrupt"
        (corrupt / "Default").mkdir(parents=True)
        (corrupt / "Default" / "Cookies").write_bytes(b"bbbb")
        (corrupt / "Default" / "Preferences").write_text('{"x": 1}')
        (corrupt / "Local State").write_text("{}")
        with pytest.raises(lifecycle.ProfileMigrationError) as exc:
            lifecycle._verify_transfer(source, corrupt)  # pyright: ignore[reportPrivateUsage]
        assert "Cookies" in str(exc.value)


# --------------------------------------------------------------------------- #
# Nothing is abandoned in the old root
# --------------------------------------------------------------------------- #


class TestNothingIsAbandoned:
    def test_profile_list_shows_a_legacy_profile_and_marks_it(self, roots):
        _, legacy = roots
        _profile_with_a_login(legacy, "work")
        listed = lifecycle.profile_list()
        assert listed[0]["name"] == "work"
        assert listed[0]["legacy"] is True
        assert listed[0]["path"] == str(legacy / "work")

    def test_a_migrated_profile_is_not_marked_legacy(self, roots):
        _, legacy = roots
        _profile_with_a_login(legacy, "work")
        lifecycle.migrate_profiles()
        assert lifecycle.profile_list()[0]["legacy"] is False

    def test_the_durable_copy_wins_when_a_name_exists_in_both(self, roots):
        durable, legacy = roots
        _profile_with_a_login(durable, "work")
        _profile_with_a_login(legacy, "work")
        listed = lifecycle.profile_list()
        assert len(listed) == 1
        assert listed[0]["legacy"] is False

    def test_profile_delete_reaches_a_legacy_profile(self, roots):
        _, legacy = roots
        _profile_with_a_login(legacy, "work")
        result = lifecycle.profile_delete("work")
        assert result["deleted"] == "work"
        assert not (legacy / "work").exists()

    def test_launch_brings_its_own_profile_forward(self, roots, monkeypatch):
        durable, legacy = roots
        _profile_with_a_login(legacy, "work", b"login")
        assert lifecycle.migrate_one_profile("work") is True
        assert (durable / "work" / "Default" / "Cookies").read_bytes() == b"login"
        assert not (legacy / "work").exists()

    def test_it_does_nothing_when_the_durable_copy_exists(self, roots):
        durable, legacy = roots
        _profile_with_a_login(durable, "work", b"new")
        _profile_with_a_login(legacy, "work", b"old")
        assert lifecycle.migrate_one_profile("work") is False
        assert (durable / "work" / "Default" / "Cookies").read_bytes() == b"new"

    def test_it_refuses_while_a_live_instance_holds_the_profile(self, roots, monkeypatch):
        _, legacy = roots
        _profile_with_a_login(legacy, "work")
        monkeypatch.setattr(
            lifecycle, "find_profile_holder", lambda name, registry_path=None: "web-01"
        )
        with pytest.raises(lifecycle.ProfileMigrationError) as exc:
            lifecycle.migrate_one_profile("work")
        assert "bt stop web-01" in str(exc.value)

    def test_cleanup_does_not_reap_a_not_yet_migrated_profile(self, roots, tmp_path):
        """An entry written before the move still points into the old root."""
        _durable, legacy = roots
        profile = _profile_with_a_login(legacy, "work", b"login")
        registry_path = str(tmp_path / "registry.json")
        core_registry._save_registry(  # pyright: ignore[reportPrivateUsage]
            {
                "web-01": {
                    "port": 9999,
                    "pid": 2,
                    "browser_version": "Chrome/153.0.0.0",
                    "user_data_dir": str(profile),
                    "launched": "2026-09-20T00:00:00Z",
                }
            },
            registry_path,
        )
        lifecycle.cleanup(registry_path=registry_path)
        assert (profile / "Default" / "Cookies").read_bytes() == b"login"


# --------------------------------------------------------------------------- #
# Through the CLI
# --------------------------------------------------------------------------- #


class TestMigrateThroughTheCli:
    def test_a_dry_run_reports_what_would_move(self, roots, capsys):
        _, legacy = roots
        _profile_with_a_login(legacy, "work")
        assert cli.main(["profile", "migrate", "--dry-run"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["dry_run"] is True
        assert [entry["name"] for entry in payload["migrated"]] == ["work"]
        assert (legacy / "work").is_dir()

    def test_it_migrates_and_reports(self, roots, capsys):
        durable, legacy = roots
        _profile_with_a_login(legacy, "work")
        assert cli.main(["profile", "migrate"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["to"] == str(durable)
        assert (durable / "work" / "Default" / "Cookies").is_file()

    def test_back_reverses_it(self, roots, capsys):
        _durable, legacy = roots
        _profile_with_a_login(legacy, "work")
        cli.main(["profile", "migrate"])
        capsys.readouterr()
        assert cli.main(["profile", "migrate", "--back"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["to"] == str(legacy)
        assert (legacy / "work" / "Default" / "Cookies").is_file()

    def test_it_rejects_an_endpoint(self, roots, capsys):
        assert cli.main(["profile", "migrate", "--endpoint", "http://127.0.0.1:9222"]) == 2
        assert "does not take --endpoint" in capsys.readouterr().err
