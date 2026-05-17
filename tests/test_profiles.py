"""Tests for named browser profile support (M-1.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from browser_tools.persistent_browser import (
    CACHE_DIR,
    PersistentChromeController,
    delete_profile,
    list_profiles,
)

PROFILES_DIR = CACHE_DIR / "profiles"


class TestNamedProfileCreation:
    """Tests for creating named browser profiles."""

    def test_profile_creates_persistent_directory(self, monkeypatch, tmp_path: Path) -> None:
        """Named profile should create a user-data-dir at profiles/{name}/."""
        monkeypatch.setattr("persistent_browser.CACHE_DIR", tmp_path)

        controller = PersistentChromeController(
            profile="my-test-profile",
            isolated=False,
            browser_url="http://127.0.0.1:9222",
        )
        profile_dir = tmp_path / "profiles" / "my-test-profile"
        assert controller.user_data_dir == profile_dir

    def test_profile_sets_deterministic_session_key(self, monkeypatch, tmp_path: Path) -> None:
        """Named profile session key should be derived from the profile name."""
        monkeypatch.setattr("persistent_browser.CACHE_DIR", tmp_path)

        controller = PersistentChromeController(
            profile="my-test-profile",
            isolated=False,
            browser_url="http://127.0.0.1:9222",
        )
        assert controller.session_key == "profile_my-test-profile"

    def test_same_profile_reuses_session_key(self, monkeypatch, tmp_path: Path) -> None:
        """Same profile name should produce the same session key."""
        monkeypatch.setattr("persistent_browser.CACHE_DIR", tmp_path)

        c1 = PersistentChromeController(
            profile="dev", isolated=False, browser_url="http://127.0.0.1:9222"
        )
        c2 = PersistentChromeController(
            profile="dev", isolated=False, browser_url="http://127.0.0.1:9222"
        )
        assert c1.session_key == c2.session_key

    def test_profile_and_isolated_raises_error(self, monkeypatch, tmp_path: Path) -> None:
        """Setting both profile and isolated=True should raise E005."""
        monkeypatch.setattr("persistent_browser.CACHE_DIR", tmp_path)

        with pytest.raises(ValueError, match="E005"):
            PersistentChromeController(
                profile="test",
                isolated=True,
                browser_url="http://127.0.0.1:9222",
            )

    def test_profile_forces_persistent_browser(self, monkeypatch, tmp_path: Path) -> None:
        """Named profile should always use persistent browser path."""
        monkeypatch.setattr("persistent_browser.CACHE_DIR", tmp_path)

        controller = PersistentChromeController(
            profile="dev",
            isolated=False,
        )
        assert controller.should_use_persistent_browser() is True

    def test_profile_directory_has_restricted_permissions(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Profile directory should be created with 0700 permissions."""
        monkeypatch.setattr("persistent_browser.CACHE_DIR", tmp_path)

        controller = PersistentChromeController(
            profile="secure-test",
            isolated=False,
            browser_url="http://127.0.0.1:9222",
        )
        profile_dir = tmp_path / "profiles" / "secure-test"
        assert controller.user_data_dir == profile_dir

    def test_state_file_uses_profile_session_key(self, monkeypatch, tmp_path: Path) -> None:
        """State file should be named after the profile session key."""
        monkeypatch.setattr("persistent_browser.CACHE_DIR", tmp_path)

        controller = PersistentChromeController(
            profile="my-app",
            isolated=False,
            browser_url="http://127.0.0.1:9222",
        )
        assert controller.state_path == tmp_path / "profile_my-app.json"


class TestListProfiles:
    """Tests for listing named profiles."""

    def test_list_profiles_empty(self, monkeypatch, tmp_path: Path) -> None:
        """Returns empty list when no profiles exist."""
        monkeypatch.setattr("persistent_browser.CACHE_DIR", tmp_path)

        result = list_profiles()
        assert result == []

    def test_list_profiles_returns_names(self, monkeypatch, tmp_path: Path) -> None:
        """Returns profile directory names."""
        monkeypatch.setattr("persistent_browser.CACHE_DIR", tmp_path)
        profiles_dir = tmp_path / "profiles"
        (profiles_dir / "dev").mkdir(parents=True)
        (profiles_dir / "staging").mkdir(parents=True)

        result = list_profiles()
        assert sorted(result) == ["dev", "staging"]

    def test_list_profiles_ignores_files(self, monkeypatch, tmp_path: Path) -> None:
        """Only directories in profiles/ are listed, not files."""
        monkeypatch.setattr("persistent_browser.CACHE_DIR", tmp_path)
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir(parents=True)
        (profiles_dir / "dev").mkdir()
        (profiles_dir / "some_file.json").write_text("{}")

        result = list_profiles()
        assert result == ["dev"]

    def test_list_profiles_ignores_session_key_dirs(self, monkeypatch, tmp_path: Path) -> None:
        """Hex-hash directories (legacy session keys) are excluded from profile listing."""
        monkeypatch.setattr("persistent_browser.CACHE_DIR", tmp_path)
        profiles_dir = tmp_path / "profiles"
        (profiles_dir / "dev").mkdir(parents=True)
        (profiles_dir / "a1b2c3d4e5f60718").mkdir()  # 16-char hex session key hash

        result = list_profiles()
        assert result == ["dev"]


class TestDeleteProfile:
    """Tests for deleting named profiles."""

    def test_delete_existing_profile(self, monkeypatch, tmp_path: Path) -> None:
        """Deleting an existing profile removes its directory."""
        monkeypatch.setattr("persistent_browser.CACHE_DIR", tmp_path)
        profiles_dir = tmp_path / "profiles"
        (profiles_dir / "dev").mkdir(parents=True)
        # Also create a state file
        (tmp_path / "profile_dev.json").write_text('{"browser_url": "test"}')

        result = delete_profile("dev")
        assert result is True
        assert not (profiles_dir / "dev").exists()
        assert not (tmp_path / "profile_dev.json").exists()

    def test_delete_nonexistent_profile(self, monkeypatch, tmp_path: Path) -> None:
        """Deleting a non-existent profile returns False."""
        monkeypatch.setattr("persistent_browser.CACHE_DIR", tmp_path)

        result = delete_profile("nonexistent")
        assert result is False

    def test_delete_cleans_daemon_files(self, monkeypatch, tmp_path: Path) -> None:
        """Deleting a profile also removes daemon socket and pid files."""
        monkeypatch.setattr("persistent_browser.CACHE_DIR", tmp_path)
        profiles_dir = tmp_path / "profiles"
        (profiles_dir / "dev").mkdir(parents=True)
        (tmp_path / "profile_dev.json").write_text("{}")
        (tmp_path / "profile_dev.sock").write_text("")
        (tmp_path / "profile_dev.daemon.pid").write_text("12345")
        (tmp_path / "profile_dev.lock").write_text("")

        result = delete_profile("dev")
        assert result is True
        assert not (tmp_path / "profile_dev.sock").exists()
        assert not (tmp_path / "profile_dev.daemon.pid").exists()
        assert not (tmp_path / "profile_dev.lock").exists()
