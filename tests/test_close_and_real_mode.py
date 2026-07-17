"""Tests for clean session teardown (close_browser) and mode='real'.

Covers the ownership rule that decides which Chrome may be force-quit, the
real-profile (`system_profile`) controller wiring and its launch/error path,
the ``close_active_session`` teardown branches, the daemon-side owned-Chrome
terminator, and the ``close_browser`` tool handler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from browser_tools.persistent_browser import (
    BrowserState,
    PersistentChromeController,
    close_active_session,
    is_owned_profile_dir,
)
from browser_tools.process_utils import build_browser_command, resolve_system_profile_dir

if TYPE_CHECKING:
    from pathlib import Path


class TestIsOwnedProfileDir:
    """Ownership rule: only private automation profiles may be quit."""

    def test_private_profile_is_owned(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("browser_tools.persistent_browser.CACHE_DIR", tmp_path)
        owned = tmp_path / "profiles" / "my-app"
        assert is_owned_profile_dir(str(owned)) is True

    def test_real_profile_is_not_owned(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("browser_tools.persistent_browser.CACHE_DIR", tmp_path)
        real = "/Users/someone/Library/Application Support/Google/Chrome Canary"
        assert is_owned_profile_dir(real) is False

    def test_none_is_not_owned(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("browser_tools.persistent_browser.CACHE_DIR", tmp_path)
        assert is_owned_profile_dir(None) is False


class TestBuildBrowserCommand:
    """The system_profile flag governs --disable-sync."""

    def test_private_profile_disables_sync(self, tmp_path: Path) -> None:
        cmd = build_browser_command(
            executable="/bin/chrome",
            port=9333,
            user_data_dir=tmp_path,
            headless=False,
            viewport=None,
        )
        assert "--disable-sync" in cmd

    def test_real_profile_keeps_sync(self, tmp_path: Path) -> None:
        cmd = build_browser_command(
            executable="/bin/chrome",
            port=9333,
            user_data_dir=tmp_path,
            headless=False,
            viewport=None,
            system_profile=True,
        )
        assert "--disable-sync" not in cmd
        assert "--no-first-run" in cmd


class TestResolveSystemProfileDir:
    """The real everyday profile directory resolves per channel."""

    def test_unknown_channel_returns_none(self) -> None:
        assert resolve_system_profile_dir("nonsense") is None

    def test_known_channel_resolves(self) -> None:
        # Canary resolves on both macOS and Linux mappings.
        assert resolve_system_profile_dir("canary") is not None


class TestRealModeController:
    """mode='real' wires the controller to the user's everyday profile."""

    def test_system_profile_uses_real_dir_and_key(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("browser_tools.persistent_browser.CACHE_DIR", tmp_path)
        real = tmp_path / "real-canary"
        monkeypatch.setattr(
            "browser_tools.persistent_browser.resolve_system_profile_dir",
            lambda channel: real,
        )
        controller = PersistentChromeController(system_profile=True, isolated=False)
        assert controller.user_data_dir == real
        assert controller.session_key == "real_canary"
        # A real-profile Chrome is never tool-owned.
        assert is_owned_profile_dir(str(controller.user_data_dir)) is False

    def test_system_profile_rejects_isolated(self) -> None:
        with pytest.raises(ValueError, match="E006"):
            PersistentChromeController(system_profile=True, isolated=True)

    def test_system_profile_rejects_named_profile(self) -> None:
        with pytest.raises(ValueError, match="E006"):
            PersistentChromeController(
                system_profile=True, isolated=False, profile="my-app"
            )

    def test_real_chrome_without_debug_port_gives_actionable_error(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        from browser_tools.persistent_browser import MCPInvocationError

        monkeypatch.setattr("browser_tools.persistent_browser.CACHE_DIR", tmp_path)
        real = tmp_path / "real-canary"
        monkeypatch.setattr(
            "browser_tools.persistent_browser.resolve_system_profile_dir",
            lambda channel: real,
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.resolve_chrome_executable",
            lambda channel: "/bin/chrome",
        )
        # No reachable Chrome to reuse, but a live singleton lock with no port.
        monkeypatch.setattr(
            PersistentChromeController, "_try_reuse_existing_chrome", lambda self, d: None
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.read_singleton_lock_pid", lambda d: 4242
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.is_process_alive", lambda pid: True
        )
        controller = PersistentChromeController(system_profile=True, isolated=False)
        with pytest.raises(MCPInvocationError, match="remote-debugging-port"):
            controller.ensure_browser_state()


class TestCloseActiveSession:
    """close_active_session quits owned Chrome, detaches from the rest."""

    def _write_state(self, controller: PersistentChromeController, **kwargs) -> None:
        BrowserState(**kwargs).save(controller.state_path)

    def test_quits_owned_chrome(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("browser_tools.persistent_browser.CACHE_DIR", tmp_path)
        killed: list[int] = []
        monkeypatch.setattr(
            "browser_tools.persistent_browser.terminate_process",
            lambda pid: killed.append(pid),
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.is_process_alive",
            lambda pid: pid not in killed,  # alive until terminated
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.clean_stale_singleton_lock", lambda d: None
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.clear_active_attach_config", lambda: None
        )
        controller = PersistentChromeController(
            isolated=False, profile="owned-app"
        )
        self._write_state(
            controller,
            browser_url="http://127.0.0.1:9444",
            pid=555,
            user_data_dir=str(controller.user_data_dir),
        )
        summary = close_active_session(controller)
        assert summary["quit_chrome"] is True
        assert summary["detached"] is False
        assert 555 in killed
        assert not controller.state_path.exists()

    def test_detaches_from_external_chrome(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("browser_tools.persistent_browser.CACHE_DIR", tmp_path)
        killed: list[int] = []
        monkeypatch.setattr(
            "browser_tools.persistent_browser.terminate_process",
            lambda pid: killed.append(pid),
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.is_process_alive", lambda pid: True
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.clear_active_attach_config", lambda: None
        )
        controller = PersistentChromeController(
            isolated=False, browser_url="http://127.0.0.1:9222"
        )
        self._write_state(
            controller,
            browser_url="http://127.0.0.1:9222",
            pid=999,
            user_data_dir="/Users/someone/Library/Application Support/Google/Chrome Canary",
        )
        summary = close_active_session(controller)
        assert summary["quit_chrome"] is False
        assert summary["detached"] is True
        # External Chrome PID must never be terminated.
        assert 999 not in killed


class TestDaemonOwnedChromeTerminator:
    """The daemon only quits Chrome it was told it owns."""

    def test_noop_when_not_owned(self, monkeypatch) -> None:
        from browser_tools import mcp_daemon

        killed: list[int] = []
        monkeypatch.setattr(
            "browser_tools.process_utils.terminate_process",
            lambda pid: killed.append(pid),
        )
        monkeypatch.setattr(
            "browser_tools.process_utils.is_process_alive", lambda pid: True
        )
        mcp_daemon._terminate_owned_chrome(123, chrome_owned=False)
        mcp_daemon._terminate_owned_chrome(None, chrome_owned=True)
        assert killed == []

    def test_terminates_when_owned(self, monkeypatch) -> None:
        from browser_tools import mcp_daemon

        killed: list[int] = []
        monkeypatch.setattr(
            "browser_tools.process_utils.terminate_process",
            lambda pid: killed.append(pid),
        )
        monkeypatch.setattr(
            "browser_tools.process_utils.is_process_alive",
            lambda pid: pid not in killed,
        )
        mcp_daemon._terminate_owned_chrome(123, chrome_owned=True)
        assert 123 in killed


class TestHandleCloseBrowser:
    """The close_browser tool handler reports and resets session state."""

    def test_reports_quit_and_clears_ref(self, monkeypatch, tmp_path: Path) -> None:
        from browser_tools import browser_tools_session as bts

        monkeypatch.setattr(
            "browser_tools.persistent_browser.close_active_session",
            lambda controller: {
                "quit_chrome": True,
                "detached": False,
                "daemon_stopped": True,
                "pid": 321,
                "endpoint": "http://127.0.0.1:9444",
            },
        )
        controller_ref: list[object] = [object()]
        resp = bts.handle_close_browser(controller_ref, object(), {})
        text = resp["result"]["content"][0]["text"]
        assert "quit" in text.lower()
        assert "321" in text
        assert controller_ref[0] is None

    def test_no_active_session(self, monkeypatch) -> None:
        from browser_tools import browser_tools_session as bts

        resp = bts.handle_close_browser([None], None, {})
        assert "No active browser session" in resp["result"]["content"][0]["text"]
