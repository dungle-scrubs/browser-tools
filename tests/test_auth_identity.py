"""Tests for session-identity, state-reuse, and endpoint-safety behavior.

These cover the paths that decide whether a logged-in browser profile is
reused or silently abandoned - the core of cross-invocation auth persistence.
"""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from browser_tools.browser_state import BrowserState
from browser_tools.daemon_client import DaemonClient
from browser_tools.persistent_browser import (
    PersistentChromeController,
    ProjectBrowserConfig,
    build_session_key,
    create_controller_from_browser_config,
    delete_profile,
)
from browser_tools.process_utils import validate_local_endpoint


class TestBuildSessionKey:
    """build_session_key must key only on durable project identity."""

    def test_headed_and_headless_share_a_bucket(self) -> None:
        """Presentation flags must not change the profile directory."""
        headed = build_session_key(browser_url=None, isolated=False, channel="canary")
        headless = build_session_key(browser_url=None, isolated=False, channel="canary")
        assert headed == headless

    def test_channel_changes_bucket(self) -> None:
        """Different Chrome channels are different binaries -> different dirs."""
        a = build_session_key(browser_url=None, isolated=False, channel="canary")
        b = build_session_key(browser_url=None, isolated=False, channel="stable")
        assert a != b

    def test_isolated_does_not_fragment_a_project(self) -> None:
        """One project is one bucket regardless of the isolated flag.

        Collapsing isolated out of the key is what stops a project from
        spawning a second Chrome and losing its login when a headed-auth call
        (isolated=False) and a default headless call (isolated=True) land in
        different directories.
        """
        default = build_session_key(browser_url=None, isolated=False, channel="canary")
        isolated = build_session_key(browser_url=None, isolated=True, channel="canary")
        assert default == isolated

    def test_external_endpoint_changes_bucket(self) -> None:
        """An explicitly attached browser is a different browser."""
        auto = build_session_key(browser_url=None, isolated=False, channel="canary")
        attached = build_session_key(
            browser_url="http://127.0.0.1:9222", isolated=False, channel="canary"
        )
        assert auto != attached


class TestControllerProfileDir:
    """Non-profile controllers must resolve headed/headless to the same dir."""

    def test_headed_headless_same_user_data_dir(self, monkeypatch, tmp_path: Path) -> None:
        """A headed and a headless call share cookies via one profile dir."""
        monkeypatch.setattr("browser_tools.persistent_browser.CACHE_DIR", tmp_path)
        headed = PersistentChromeController(headless=False, isolated=False)
        headless = PersistentChromeController(headless=True, isolated=False)
        assert headed.user_data_dir == headless.user_data_dir


class TestIsStateUsable:
    """_is_state_usable decides reuse vs relaunch (== keep vs drop login)."""

    def _state(self, controller: PersistentChromeController, **overrides) -> BrowserState:
        data = {
            "browser_url": "http://127.0.0.1:9222",
            "user_data_dir": str(controller.user_data_dir),
            "headless": controller.headless,
            "isolated": controller.isolated,
            "channel": controller.channel,
            "viewport": controller.viewport,
        }
        data.update(overrides)
        return BrowserState(**data)

    def test_reused_across_headless_switch(self, monkeypatch, tmp_path: Path) -> None:
        """Saved headed state is still usable for a headless controller."""
        monkeypatch.setattr("browser_tools.persistent_browser.CACHE_DIR", tmp_path)
        controller = PersistentChromeController(headless=True, isolated=False)
        state = self._state(controller, headless=False)  # saved by a headed run
        with (
            patch("browser_tools.persistent_browser.is_process_alive", return_value=True),
            patch("browser_tools.persistent_browser.is_devtools_available", return_value=True),
        ):
            assert controller._is_state_usable(state) is True

    def test_rejected_on_channel_mismatch(self, monkeypatch, tmp_path: Path) -> None:
        """A different channel points at a different profile dir."""
        monkeypatch.setattr("browser_tools.persistent_browser.CACHE_DIR", tmp_path)
        controller = PersistentChromeController(isolated=False, channel="canary")
        state = self._state(controller, channel="stable")
        with patch("browser_tools.persistent_browser.is_devtools_available", return_value=True):
            assert controller._is_state_usable(state) is False

    def test_rejected_on_user_data_dir_mismatch(self, monkeypatch, tmp_path: Path) -> None:
        """Recorded state describing a different dir must not be trusted."""
        monkeypatch.setattr("browser_tools.persistent_browser.CACHE_DIR", tmp_path)
        controller = PersistentChromeController(isolated=False)
        state = self._state(controller, user_data_dir=str(tmp_path / "somewhere-else"))
        with patch("browser_tools.persistent_browser.is_devtools_available", return_value=True):
            assert controller._is_state_usable(state) is False


class TestShouldUsePersistentBrowser:
    """The False path is the only cookie-discarding route; verify it."""

    def test_false_when_all_inputs_absent(self) -> None:
        controller = PersistentChromeController(headless=False, isolated=False)
        assert controller.should_use_persistent_browser() is False

    def test_true_for_isolated(self) -> None:
        controller = PersistentChromeController(headless=False, isolated=True)
        assert controller.should_use_persistent_browser() is True


class TestCreateControllerFromConfig:
    """The mode -> (headless, isolated) matrix must stay predictable."""

    @pytest.mark.parametrize(
        ("mode", "expected_headless", "expected_isolated"),
        [
            ("headless", True, True),
            ("headless-auth", True, False),
            ("headed-auth", False, False),
            ("auth", False, False),
            ("auth_headed", False, False),
        ],
    )
    def test_mode_matrix(
        self, monkeypatch, tmp_path: Path, mode, expected_headless, expected_isolated
    ) -> None:
        monkeypatch.setattr("browser_tools.persistent_browser.CACHE_DIR", tmp_path)
        controller = create_controller_from_browser_config(
            ProjectBrowserConfig(mode=mode), source="test"
        )
        assert controller.headless is expected_headless
        assert controller.isolated is expected_isolated

    def test_profile_forces_persistent(self, monkeypatch, tmp_path: Path) -> None:
        """A named profile must never be isolated (no E005 raise)."""
        monkeypatch.setattr("browser_tools.persistent_browser.CACHE_DIR", tmp_path)
        controller = create_controller_from_browser_config(
            ProjectBrowserConfig(mode="headless", profile="dev"), source="test"
        )
        assert controller.isolated is False
        assert controller.profile == "dev"

    def test_explicit_flags_override_mode_defaults(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("browser_tools.persistent_browser.CACHE_DIR", tmp_path)
        controller = create_controller_from_browser_config(
            ProjectBrowserConfig(mode="headless", headless=False, isolated=False), source="test"
        )
        assert controller.headless is False
        assert controller.isolated is False


class TestDeleteProfileTraversal:
    """delete_profile must never escape the profiles directory."""

    @pytest.mark.parametrize("name", ["..", ".", "../evil", "a/b", "a\\b", "", "a\x00b"])
    def test_rejects_unsafe_names(self, monkeypatch, tmp_path: Path, name) -> None:
        monkeypatch.setattr("browser_tools.persistent_browser.CACHE_DIR", tmp_path)
        outside = tmp_path / "evil"
        outside.mkdir()
        assert delete_profile(name) is False
        assert outside.exists()  # nothing outside profiles/ was touched

    def test_deletes_valid_profile(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("browser_tools.persistent_browser.CACHE_DIR", tmp_path)
        (tmp_path / "profiles" / "dev").mkdir(parents=True)
        assert delete_profile("dev") is True
        assert not (tmp_path / "profiles" / "dev").exists()


class TestValidateLocalEndpoint:
    """Endpoint host validation guards against SSRF-style outbound requests."""

    @pytest.mark.parametrize(
        "endpoint",
        ["http://127.0.0.1:9222", "http://localhost:9222", "http://[::1]:9222"],
    )
    def test_accepts_loopback(self, endpoint) -> None:
        assert validate_local_endpoint(endpoint) is None

    @pytest.mark.parametrize(
        "endpoint",
        ["http://169.254.169.254", "http://example.com:9222", "http://10.0.0.5:9222"],
    )
    def test_rejects_non_loopback(self, endpoint) -> None:
        assert validate_local_endpoint(endpoint) is not None

    def test_rejects_bad_scheme(self) -> None:
        assert validate_local_endpoint("ftp://127.0.0.1") is not None

    def test_env_override_allows_remote(self, monkeypatch) -> None:
        monkeypatch.setenv("BROWSER_TOOLS_ALLOW_REMOTE_ENDPOINT", "1")
        assert validate_local_endpoint("http://example.com:9222") is None


class TestBrowserStateSave:
    """Persisted state must be atomic and owner-only (auth-adjacent data)."""

    def test_save_is_private_and_roundtrips(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "state.json"
        state = BrowserState(browser_url="http://127.0.0.1:9222", pid=123)
        state.save(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        loaded = BrowserState.from_path(path)
        assert loaded is not None
        assert loaded.browser_url == "http://127.0.0.1:9222"
        assert loaded.pid == 123

    def test_from_path_ignores_unknown_keys(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text('{"browser_url": "http://127.0.0.1:9222", "future_field": 1}')
        loaded = BrowserState.from_path(path)
        assert loaded is not None
        assert loaded.browser_url == "http://127.0.0.1:9222"


class _FakeSock:
    """Minimal socket double that replays queued recv chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def settimeout(self, _t) -> None:
        pass

    def close(self) -> None:
        pass


class TestDaemonClientResponseMatching:
    """call_tool must return the response whose id matches the request."""

    def test_skips_stale_and_notification_lines(self) -> None:
        client = DaemonClient("/tmp/does-not-matter.sock")
        client._sock = _FakeSock(  # type: ignore[assignment]
            [
                b'{"jsonrpc": "2.0", "method": "note"}\n',  # notification, no id
                b'{"id": 0, "result": "stale"}\n',  # earlier request's reply
                b'{"id": 1, "result": "real"}\n',  # our reply
            ]
        )
        response = client.call_tool("do_thing", {})
        assert response["id"] == 1
        assert response["result"] == "real"
