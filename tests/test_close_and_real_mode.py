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
    reap_orphaned_sessions,
)
from browser_tools.process_utils import build_browser_command, resolve_system_profile_dir

if TYPE_CHECKING:
    from pathlib import Path


class TestIsOwnedProfileDir:
    """Ownership rule: only private automation profiles may be quit."""

    def test_private_profile_is_owned(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        owned = tmp_path / "profiles" / "my-app"
        assert is_owned_profile_dir(str(owned)) is True

    def test_real_profile_is_not_owned(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        real = "/Users/someone/Library/Application Support/Google/Chrome Canary"
        assert is_owned_profile_dir(real) is False

    def test_none_is_not_owned(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
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
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
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
            PersistentChromeController(system_profile=True, isolated=False, profile="my-app")

    def test_real_chrome_without_debug_port_gives_actionable_error(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        from browser_tools.persistent_browser import MCPInvocationError

        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
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
        monkeypatch.setattr("browser_tools.persistent_browser.is_process_alive", lambda pid: True)
        # The everyday Chrome genuinely holds the real profile dir.
        monkeypatch.setattr(
            "browser_tools.persistent_browser.pid_holds_user_data_dir",
            lambda pid, d: True,
        )
        controller = PersistentChromeController(system_profile=True, isolated=False)
        with pytest.raises(MCPInvocationError, match="remote-debugging-port"):
            controller.ensure_browser_state()


class TestCloseActiveSession:
    """close_active_session quits owned Chrome, detaches from the rest."""

    def _write_state(self, controller: PersistentChromeController, **kwargs) -> None:
        BrowserState(**kwargs).save(controller.state_path)

    def test_quits_owned_chrome(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
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
        monkeypatch.setattr("browser_tools.session_store.clear_active_attach_config", lambda: None)
        controller = PersistentChromeController(isolated=False, profile="owned-app")
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
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        killed: list[int] = []
        monkeypatch.setattr(
            "browser_tools.persistent_browser.terminate_process",
            lambda pid: killed.append(pid),
        )
        monkeypatch.setattr("browser_tools.persistent_browser.is_process_alive", lambda pid: True)
        monkeypatch.setattr("browser_tools.session_store.clear_active_attach_config", lambda: None)
        controller = PersistentChromeController(isolated=False, browser_url="http://127.0.0.1:9222")
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
    """The daemon delegates owned-Chrome teardown to quit_owned_chrome."""

    def test_noop_when_not_owned(self, monkeypatch) -> None:
        from browser_tools import mcp_daemon

        killed: list[int] = []
        monkeypatch.setattr(
            "browser_tools.persistent_browser.terminate_process",
            lambda pid: killed.append(pid),
        )
        monkeypatch.setattr("browser_tools.persistent_browser.is_process_alive", lambda pid: True)
        mcp_daemon._terminate_owned_chrome(123, chrome_owned=False, chrome_user_data_dir=None)
        mcp_daemon._terminate_owned_chrome(None, chrome_owned=True, chrome_user_data_dir=None)
        assert killed == []

    def test_terminates_when_owned(self, monkeypatch) -> None:
        from browser_tools import mcp_daemon

        killed: list[int] = []
        monkeypatch.setattr(
            "browser_tools.persistent_browser.terminate_process",
            lambda pid: killed.append(pid),
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.is_process_alive",
            lambda pid: pid not in killed,
        )
        mcp_daemon._terminate_owned_chrome(123, chrome_owned=True, chrome_user_data_dir=None)
        assert 123 in killed


class TestHandleCloseBrowser:
    """The close_browser tool handler reports and resets session state."""

    def test_reports_quit_and_clears_ref(self, monkeypatch, tmp_path: Path) -> None:
        from browser_tools import browser_session as bts

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
        from browser_tools import browser_session as bts

        resp = bts.handle_close_browser([None], None, {})
        assert "No active browser session" in resp["result"]["content"][0]["text"]


class TestReapOrphanedSessions:
    """The orphan sweep quits owned Chromes whose daemon died without teardown."""

    STALE = 1_000_000.0  # far enough in the past to be idle past the timeout

    STARTED_AT = "Fri Jul 17 15:45:27 2026"

    @classmethod
    def _patch_process_probes(
        cls,
        monkeypatch,
        *,
        holds_dir: bool = True,
        started_at: str | None = None,
        unkillable: bool = False,
    ) -> list[int]:
        """Make every recorded PID look alive until it is terminated.

        Args:
            monkeypatch: Pytest monkeypatch fixture.
            holds_dir: Whether a PID's cmdline resolves to its profile dir.
            started_at: Start-time token the live process reports. Defaults to
                the one sessions are written with; pass a different value to
                simulate a recycled PID.
            unkillable: Keep the process alive through every signal, as a
                failed (EPERM) kill would.

        Returns:
            The list terminated PIDs are appended to.
        """
        killed: list[int] = []
        monkeypatch.setattr(
            "browser_tools.persistent_browser.terminate_process",
            lambda pid: killed.append(pid),
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.is_process_alive",
            lambda pid: unkillable or pid not in killed,
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.read_process_start_time",
            lambda pid: started_at if started_at is not None else cls.STARTED_AT,
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.pid_holds_user_data_dir",
            lambda pid, directory: holds_dir,
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.clean_stale_singleton_lock", lambda d: None
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser._wait_for_process_exit",
            lambda pid, timeout: not unkillable,
        )
        return killed

    def _write_session(self, cache: Path, key: str, **kwargs) -> Path:
        """Write a session state file under a patched cache dir.

        Args:
            cache: Patched CACHE_DIR.
            key: Session key.
            **kwargs: BrowserState field overrides.

        Returns:
            Path to the written state file.
        """
        profile_dir = cache / "profiles" / key
        profile_dir.mkdir(parents=True, exist_ok=True)
        fields = {
            "browser_url": "http://127.0.0.1:9111",
            "pid": 4242,
            "user_data_dir": str(profile_dir),
            "chrome_owned": True,
            "chrome_started_at": self.STARTED_AT,
            "last_used_at": self.STALE,
            "daemon_pid": None,
            **kwargs,
        }
        state_path = cache / f"{key}.json"
        BrowserState(**fields).save(state_path)
        return state_path

    def test_quits_orphan_and_clears_state(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        killed = self._patch_process_probes(monkeypatch)
        state_path = self._write_session(tmp_path, "deadbeefdeadbeef")
        (tmp_path / "deadbeefdeadbeef.sock").touch()
        (tmp_path / "deadbeefdeadbeef.daemon.pid").write_text("999")

        reaped = reap_orphaned_sessions()

        assert [entry["session_key"] for entry in reaped] == ["deadbeefdeadbeef"]
        assert killed == [4242]
        assert not state_path.exists()
        assert not (tmp_path / "deadbeefdeadbeef.sock").exists()
        assert not (tmp_path / "deadbeefdeadbeef.daemon.pid").exists()
        # The profile itself survives, so cookies outlive the orphaned process.
        assert (tmp_path / "profiles" / "deadbeefdeadbeef").exists()

    def test_skips_session_with_live_daemon(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        killed = self._patch_process_probes(monkeypatch)
        state_path = self._write_session(tmp_path, "aaaabbbbccccdddd", daemon_pid=777)
        (tmp_path / "aaaabbbbccccdddd.daemon.pid").write_text("777")

        assert reap_orphaned_sessions() == []
        assert killed == []
        assert state_path.exists()

    def test_recycled_daemon_pid_does_not_exempt_forever(self, monkeypatch, tmp_path: Path) -> None:
        """A live PID that is no longer *our* daemon must not block reaping."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        killed = self._patch_process_probes(monkeypatch)
        self._write_session(tmp_path, "bbbbccccddddeeee", daemon_pid=777)
        # The daemon's own PID file names a different process: 777 was recycled.
        (tmp_path / "bbbbccccddddeeee.daemon.pid").write_text("31337")

        assert [entry["pid"] for entry in reap_orphaned_sessions()] == [4242]
        assert killed == [4242]

    def test_unowned_state_flag_blocks_reaping(self, monkeypatch, tmp_path: Path) -> None:
        """A browser we did not launch is never force-quit, profile dir notwithstanding."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        killed = self._patch_process_probes(monkeypatch)
        state_path = self._write_session(tmp_path, "ccccddddeeeeffff", chrome_owned=False)

        assert reap_orphaned_sessions() == []
        assert killed == []
        assert state_path.exists()

    def test_start_time_mismatch_is_not_signalled(self, monkeypatch, tmp_path: Path) -> None:
        """A PID recycled since launch fails the identity check before any signal."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        killed = self._patch_process_probes(monkeypatch, started_at="Sat Jul 25 09:00:00 2026")
        state_path = self._write_session(tmp_path, "ddddeeeeffff0000")

        assert reap_orphaned_sessions() == []
        assert killed == []
        # State is kept: the browser, whatever it is now, was not dealt with.
        assert state_path.exists()

    def test_surviving_chrome_keeps_its_state(self, monkeypatch, tmp_path: Path) -> None:
        """A kill that silently fails must not erase the record of what is running."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        killed = self._patch_process_probes(monkeypatch, unkillable=True)
        state_path = self._write_session(tmp_path, "eeeeffff00001111")

        assert reap_orphaned_sessions() == []
        assert killed == [4242]  # signalled, but it did not die
        assert state_path.exists()

    def test_lock_file_survives_reaping(self, monkeypatch, tmp_path: Path) -> None:
        """Unlinking a held lock would let two wrappers both think they hold it."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        self._patch_process_probes(monkeypatch)
        self._write_session(tmp_path, "ffff000011112222")
        lock_path = tmp_path / "ffff000011112222.lock"

        assert len(reap_orphaned_sessions()) == 1
        assert lock_path.exists()

    def test_skips_recently_used_session(self, monkeypatch, tmp_path: Path) -> None:
        import time

        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        killed = self._patch_process_probes(monkeypatch)
        self._write_session(tmp_path, "1111222233334444", last_used_at=time.time())

        assert reap_orphaned_sessions() == []
        assert killed == []

    def test_never_quits_unowned_browser(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        killed = self._patch_process_probes(monkeypatch)
        # mode='real' / externally attached Chrome: outside CACHE_DIR/profiles.
        state_path = tmp_path / "real_canary.json"
        BrowserState(
            browser_url="http://127.0.0.1:9222",
            pid=4242,
            user_data_dir="/Users/someone/Library/Application Support/Google/Chrome Canary",
            # Even with the owned flag set, the directory barrier holds.
            chrome_owned=True,
            chrome_started_at=self.STARTED_AT,
            last_used_at=self.STALE,
        ).save(state_path)

        assert reap_orphaned_sessions() == []
        assert killed == []
        assert state_path.exists()

    def test_recycled_pid_is_not_signalled(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        killed = self._patch_process_probes(monkeypatch, holds_dir=False)
        state_path = self._write_session(tmp_path, "5555666677778888")

        assert reap_orphaned_sessions() == []
        assert killed == []
        # The stale record is still dropped so it is not re-examined forever.
        assert not state_path.exists()

    def test_ignores_override_and_attach_records(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        killed = self._patch_process_probes(monkeypatch)
        override = tmp_path / "browser_session_abc.json"
        override.write_text('{"mode": "headless", "isolated": true}')
        attach = tmp_path / "active_attach_abc.json"
        attach.write_text('{"browser_url": "http://127.0.0.1:9222"}')

        assert reap_orphaned_sessions() == []
        assert killed == []
        assert override.exists()
        assert attach.exists()


class TestChromeOwnershipProvenance:
    """Ownership is recorded at launch, not inferred later from the profile dir."""

    @staticmethod
    def _patch_launch(monkeypatch, tmp_path: Path, pid: int = 4242) -> None:
        """Stub out everything between "decide to launch" and a running Chrome.

        Args:
            monkeypatch: Pytest monkeypatch fixture.
            tmp_path: Test cache root.
            pid: PID the fake launch should report.
        """
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        monkeypatch.setattr(
            "browser_tools.persistent_browser.resolve_chrome_executable",
            lambda channel: "/bin/chrome",
        )
        monkeypatch.setattr(
            PersistentChromeController,
            "_try_reuse_existing_chrome",
            lambda self, directory: None,
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.read_singleton_lock_pid", lambda d: None
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.clean_stale_singleton_lock", lambda d: None
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.read_process_start_time",
            lambda p: "Fri Jul 17 15:45:27 2026",
        )
        monkeypatch.setattr(
            PersistentChromeController,
            "_launch_chrome",
            lambda self, executable, directory: ("http://127.0.0.1:9555", pid),
        )

    def test_launched_chrome_is_recorded_as_owned(self, monkeypatch, tmp_path: Path) -> None:
        self._patch_launch(monkeypatch, tmp_path)
        controller = PersistentChromeController(isolated=False, profile="fresh-app")

        state = controller.ensure_browser_state()

        assert state.chrome_owned is True
        assert state.chrome_started_at == "Fri Jul 17 15:45:27 2026"

    def test_real_profile_launch_is_never_owned(self, monkeypatch, tmp_path: Path) -> None:
        real = tmp_path / "real-canary"
        monkeypatch.setattr(
            "browser_tools.persistent_browser.resolve_system_profile_dir",
            lambda channel: real,
        )
        self._patch_launch(monkeypatch, tmp_path)
        controller = PersistentChromeController(system_profile=True, isolated=False)

        state = controller.ensure_browser_state()

        assert state.chrome_owned is False
        assert state.chrome_started_at is None

    def test_reuse_inherits_ownership_from_prior_state(self, monkeypatch, tmp_path: Path) -> None:
        """A relaunch-free reuse of our own Chrome keeps it reapable."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        monkeypatch.setattr(
            "browser_tools.persistent_browser.read_singleton_lock_pid", lambda d: 777
        )
        monkeypatch.setattr("browser_tools.persistent_browser.is_process_alive", lambda pid: True)
        monkeypatch.setattr(
            "browser_tools.persistent_browser.pid_holds_user_data_dir", lambda pid, d: True
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.find_chrome_debug_port", lambda pid: 9556
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.is_devtools_available", lambda url: True
        )
        controller = PersistentChromeController(isolated=False, profile="reused-app")
        BrowserState(
            browser_url="http://127.0.0.1:9556",
            pid=777,
            user_data_dir=str(controller.user_data_dir),
            chrome_owned=True,
            chrome_started_at="Wed Jul 15 11:39:42 2026",
        ).save(controller.state_path)

        state = controller._try_reuse_existing_chrome(controller.user_data_dir)

        assert state is not None
        assert state.chrome_owned is True
        assert state.chrome_started_at == "Wed Jul 15 11:39:42 2026"

    def test_reuse_of_a_user_started_chrome_stays_unowned(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A Chrome we find but never launched must not become force-quittable."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        monkeypatch.setattr(
            "browser_tools.persistent_browser.read_singleton_lock_pid", lambda d: 777
        )
        monkeypatch.setattr("browser_tools.persistent_browser.is_process_alive", lambda pid: True)
        monkeypatch.setattr(
            "browser_tools.persistent_browser.pid_holds_user_data_dir", lambda pid, d: True
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.find_chrome_debug_port", lambda pid: 9556
        )
        monkeypatch.setattr(
            "browser_tools.persistent_browser.is_devtools_available", lambda url: True
        )
        controller = PersistentChromeController(isolated=False, profile="foreign-app")
        # No prior state at all: we have no evidence this Chrome is ours.

        state = controller._try_reuse_existing_chrome(controller.user_data_dir)

        assert state is not None
        assert state.chrome_owned is False
