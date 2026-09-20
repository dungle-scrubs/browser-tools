"""``profile list`` and ``profile delete NAME`` (#98).

The rules here were recovered from the two MCP handlers the front deletion
removed (#86), so the deletion did not take them with it. Both verbs read the
profile root through :func:`lifecycle.profiles_root` rather than a constant,
which is why the root move in #81 needs no change on this side.

``profile delete`` is the only way a profile goes away. ``cleanup`` must not
prune one on age, and ``tests/test_profile_path_hardening.py`` pins that.
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
def profile_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated profile root, reached only through the resolver."""
    root = tmp_path / "profiles"
    root.mkdir()
    monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(root))
    monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", str(tmp_path / "registry.json"))
    return root


@pytest.fixture
def live_holder(monkeypatch: pytest.MonkeyPatch):
    """Write a registry entry for a profile and make liveness read it as live.

    Liveness for a Camoufox entry is process identity plus a user-data-dir
    hold, both of which are OS probes. Stubbing the two probes exercises the
    real ``instance_is_live`` / ``find_profile_holder`` path without a browser.
    """

    def _make(registry_path: str, *, name: str, profile: str, user_data_dir: Path) -> str:
        _write_entry(registry_path, name=name, profile=profile, user_data_dir=user_data_dir)
        monkeypatch.setattr("browser_tools.lifecycle.process_start_time", lambda pid: None)
        monkeypatch.setattr(
            "browser_tools.lifecycle.process_is_ours", lambda **kwargs: True
        )
        monkeypatch.setattr(
            "browser_tools.lifecycle.pid_holds_user_data_dir", lambda pid, d: True
        )
        return name

    return _make


def _write_entry(registry_path: str, *, name: str, profile: str, user_data_dir: Path) -> None:
    """Write one Camoufox registry entry bound to ``profile``."""
    import os

    path = core_registry._resolve_path(registry_path)  # pyright: ignore[reportPrivateUsage]
    reg = core_registry._load_registry(path)  # pyright: ignore[reportPrivateUsage]
    reg[name] = {
        "port": 9999,
        "pid": os.getpid(),
        "browser_version": "Chrome/153.0.0.0",
        "user_data_dir": str(user_data_dir),
        "launched": "2026-09-20T00:00:00Z",
        "engine": "camoufox",
        "profile": profile,
    }
    core_registry._save_registry(reg, path)  # pyright: ignore[reportPrivateUsage]


# --------------------------------------------------------------------------- #
# Name validation
# --------------------------------------------------------------------------- #


class TestProfileNameValidation:
    @pytest.mark.parametrize(
        "name", ["work", "work-2", "a", "a1", "my.profile", "graybox-integrated-demo", "9"]
    )
    def test_a_usable_name_passes(self, name):
        assert lifecycle.validate_profile_name(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "",
            ".",
            "..",
            "../escape",
            "/absolute",
            "Work",
            "with space",
            "has_underscore",
            "-leading",
            "trailing-",
            ".hidden",
            "trailing.",
            "locks/../..",
            "a/b",
        ],
    )
    def test_an_unusable_name_is_refused(self, name):
        with pytest.raises(lifecycle.ProfileNameError):
            lifecycle.validate_profile_name(name)

    def test_the_refusal_points_at_the_listing_verb(self):
        with pytest.raises(lifecycle.ProfileNameError) as exc:
            lifecycle.validate_profile_name("Work")
        assert "bt profile list" in str(exc.value)

    def test_the_name_pattern_excludes_the_roots_own_directories(self):
        """`.locks` and `.ephemeral` share the root and must never be profiles."""
        assert not lifecycle.PROFILE_NAME_PATTERN.fullmatch(".locks")
        assert not lifecycle.PROFILE_NAME_PATTERN.fullmatch(".ephemeral")


# --------------------------------------------------------------------------- #
# profile list
# --------------------------------------------------------------------------- #


class TestProfileList:
    def test_an_empty_root_lists_nothing(self, profile_root):
        assert lifecycle.profile_list() == []

    def test_a_missing_root_lists_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(tmp_path / "never-made"))
        assert lifecycle.profile_list() == []

    def test_every_profile_is_reported_with_its_name_and_path(self, profile_root):
        (profile_root / "work").mkdir()
        (profile_root / "play").mkdir()
        listed = lifecycle.profile_list()
        assert [entry["name"] for entry in listed] == ["play", "work"]
        assert listed[1]["path"] == str(profile_root / "work")

    def test_it_needs_no_running_instance(self, profile_root):
        (profile_root / "work").mkdir()
        assert lifecycle.profile_list()[0]["held_by"] is None

    def test_the_roots_own_directories_are_not_profiles(self, profile_root):
        (profile_root / ".locks").mkdir()
        (profile_root / ".ephemeral").mkdir()
        (profile_root / "work").mkdir()
        assert [entry["name"] for entry in lifecycle.profile_list()] == ["work"]

    def test_a_symlink_in_the_root_is_not_a_profile(self, profile_root):
        """Listing it would name something `profile delete` refuses."""
        outside = profile_root.parent / "elsewhere"
        outside.mkdir()
        (profile_root / "escape").symlink_to(outside, target_is_directory=True)
        assert lifecycle.profile_list() == []

    def test_a_loose_file_in_the_root_is_not_a_profile(self, profile_root):
        (profile_root / "notes.txt").write_text("x")
        assert lifecycle.profile_list() == []

    def test_a_live_holder_is_named(self, profile_root, tmp_path, live_holder):
        (profile_root / "work").mkdir()
        registry = str(tmp_path / "registry.json")
        live_holder(registry, name="holder-01", profile="work", user_data_dir=profile_root / "work")
        listed = lifecycle.profile_list(registry_path=registry)
        assert listed[0]["held_by"] == "holder-01"

    def test_the_root_comes_from_the_resolver_not_a_constant(self, tmp_path, monkeypatch):
        """A changed BROWSER_TOOLS_PROFILES_DIR is honoured with no code change."""
        first = tmp_path / "one"
        second = tmp_path / "two"
        (first / "alpha").mkdir(parents=True)
        (second / "beta").mkdir(parents=True)
        monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(first))
        assert [e["name"] for e in lifecycle.profile_list()] == ["alpha"]
        monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(second))
        assert [e["name"] for e in lifecycle.profile_list()] == ["beta"]


# --------------------------------------------------------------------------- #
# profile delete
# --------------------------------------------------------------------------- #


class TestProfileDelete:
    def test_it_removes_exactly_that_directory(self, profile_root):
        (profile_root / "work").mkdir()
        (profile_root / "work" / "Default").mkdir()
        (profile_root / "keep").mkdir()
        result = lifecycle.profile_delete("work")
        assert result == {"deleted": "work", "path": str(profile_root / "work")}
        assert not (profile_root / "work").exists()
        assert (profile_root / "keep").is_dir()

    @pytest.mark.parametrize("name", ["../escape", "..", "Work", "with space", "a/b"])
    def test_an_unusable_name_deletes_nothing(self, name, profile_root):
        (profile_root / "work").mkdir()
        outside = profile_root.parent / "escape"
        outside.mkdir()
        with pytest.raises(lifecycle.ProfileNameError):
            lifecycle.profile_delete(name)
        assert (profile_root / "work").is_dir()
        assert outside.is_dir()

    def test_a_symlink_out_of_the_root_is_refused_on_the_resolved_path(self, profile_root):
        """Containment is decided on the resolved path, not on the string."""
        outside = profile_root.parent / "elsewhere"
        outside.mkdir()
        (profile_root / "work").symlink_to(outside, target_is_directory=True)
        with pytest.raises(lifecycle.ProfileNameError) as exc:
            lifecycle.profile_delete("work")
        assert "outside the profile root" in str(exc.value)
        assert outside.is_dir()
        assert (profile_root / "work").is_symlink()

    def test_an_unknown_profile_is_an_operational_error(self, profile_root):
        with pytest.raises(lifecycle.LifecycleError) as exc:
            lifecycle.profile_delete("nope")
        assert "bt profile list" in str(exc.value)

    def test_a_live_holder_refuses_and_names_the_remedy(self, profile_root, tmp_path, live_holder):
        (profile_root / "work").mkdir()
        registry = str(tmp_path / "registry.json")
        live_holder(registry, name="holder-01", profile="work", user_data_dir=profile_root / "work")
        with pytest.raises(lifecycle.LifecycleError) as exc:
            lifecycle.profile_delete("work", registry_path=registry)
        message = str(exc.value)
        assert "holder-01" in message
        assert "bt stop holder-01" in message
        assert (profile_root / "work").is_dir()

    def test_a_live_holder_refusal_is_not_a_usage_error(self, profile_root, tmp_path, live_holder):
        """It is exit 1: the invocation was well-formed, the state refuses it."""
        (profile_root / "work").mkdir()
        registry = str(tmp_path / "registry.json")
        live_holder(registry, name="holder-01", profile="work", user_data_dir=profile_root / "work")
        with pytest.raises(lifecycle.LifecycleError) as exc:
            lifecycle.profile_delete("work", registry_path=registry)
        assert not isinstance(exc.value, lifecycle.ProfileNameError)


# --------------------------------------------------------------------------- #
# Through the CLI, with exit codes
# --------------------------------------------------------------------------- #


class TestProfileVerbsThroughTheCli:
    def test_list_prints_json_and_exits_zero(self, profile_root, capsys):
        (profile_root / "work").mkdir()
        assert cli.main(["profile", "list"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert [entry["name"] for entry in payload] == ["work"]

    def test_an_empty_root_prints_an_empty_list(self, profile_root, capsys):
        assert cli.main(["profile", "list"]) == 0
        assert json.loads(capsys.readouterr().out) == []

    def test_delete_prints_what_it_removed(self, profile_root, capsys):
        (profile_root / "work").mkdir()
        assert cli.main(["profile", "delete", "work"]) == 0
        assert json.loads(capsys.readouterr().out)["deleted"] == "work"

    @pytest.mark.parametrize("name", ["../escape", "Work", "with space"])
    def test_a_bad_name_exits_2(self, name, profile_root, capsys):
        assert cli.main(["profile", "delete", name]) == 2
        assert "not a usable profile name" in capsys.readouterr().err

    def test_a_path_escape_exits_2(self, profile_root, capsys):
        outside = profile_root.parent / "elsewhere"
        outside.mkdir()
        (profile_root / "work").symlink_to(outside, target_is_directory=True)
        assert cli.main(["profile", "delete", "work"]) == 2
        assert "outside the profile root" in capsys.readouterr().err
        assert outside.is_dir()

    def test_a_live_holder_exits_1(self, profile_root, tmp_path, capsys, live_holder):
        (profile_root / "work").mkdir()
        registry = str(tmp_path / "registry.json")
        live_holder(registry, name="holder-01", profile="work", user_data_dir=profile_root / "work")
        assert cli.main(["profile", "delete", "work"]) == 1
        err = capsys.readouterr().err
        assert "holder-01" in err
        assert "bt stop holder-01" in err

    def test_a_missing_sub_action_exits_2(self, profile_root, capsys):
        assert cli.main(["profile"]) == 2
        assert "list, delete NAME, or migrate" in capsys.readouterr().err

    def test_delete_without_a_name_exits_2(self, profile_root):
        with pytest.raises(SystemExit) as exc:
            cli.main(["profile", "delete"])
        assert exc.value.code == 2

    @pytest.mark.parametrize("verb", [["profile", "list"], ["profile", "delete", "work"]])
    def test_both_verbs_reject_an_endpoint(self, verb, profile_root, capsys):
        assert cli.main([*verb, "--endpoint", "http://127.0.0.1:9222"]) == 2
        assert "does not take --endpoint" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# launch validates the name it turns into a path
# --------------------------------------------------------------------------- #


class TestLaunchValidatesTheProfileName:
    @pytest.mark.parametrize("name", ["../escape", "Work", "with space"])
    def test_an_unusable_profile_name_fails_before_any_launch(
        self, name, profile_root, tmp_path, monkeypatch
    ):
        """Without this, a launch could write outside the root, or create a
        profile no profile verb can name."""

        def _never(*args, **kwargs):
            raise AssertionError("the launcher was reached with an invalid profile name")

        monkeypatch.setattr("browser_tools.core.launcher.launch_browser", _never)
        with pytest.raises(lifecycle.ProfileNameError):
            lifecycle.launch(profile=name, registry_path=str(tmp_path / "registry.json"))
        assert list(profile_root.iterdir()) == []
