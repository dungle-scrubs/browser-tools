"""Tests for persistent Chrome browser reuse in browser-tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
from typing import Any
from unittest.mock import patch

from browser_tools.persistent_browser import (
    BrowserState,
    PersistentChromeController,
    clean_stale_singleton_lock,
    find_chrome_debug_port,
    read_singleton_lock_pid,
)


def make_response(text: str) -> dict:
    """Build a minimal MCP text response for tests.

    Args:
        text: Text content to embed in the response.

    Returns:
        JSON-RPC response dict with a single text content item.
    """
    return {"result": {"content": [{"type": "text", "text": text}]}}


class FakeClient:
    """Test double for DaemonClient (and ChromeMcpSession)."""

    calls: list[tuple[str, dict]] = []  # noqa: RUF012
    responses: dict[str, dict] = {}  # noqa: RUF012

    def __init__(self, *args: Any, **kwargs: Any):
        """Accept any constructor args for compatibility.

        Args:
            args: Positional arguments (ignored).
            kwargs: Keyword arguments (ignored).

        Returns:
            None.
        """
        pass

    def __enter__(self) -> FakeClient:
        """Context manager entry - reset call log.

        Returns:
            Self.
        """
        type(self).calls = []
        return self

    def __exit__(self, exc_type: type | None, exc: BaseException | None, traceback: Any) -> None:
        """Context manager exit.

        Args:
            exc_type: Exception type.
            exc: Exception instance.
            traceback: Traceback instance.

        Returns:
            None.
        """
        return None

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Record tool calls and return configured responses.

        Args:
            name: Tool name.
            arguments: Tool arguments.

        Returns:
            Configured fake response.
        """
        type(self).calls.append((name, arguments))
        return type(self).responses.get(name, {"result": {"content": []}})


class TestPersistentChromeController:
    """Tests for restoring page selection across tool calls."""

    def test_restores_selected_page_by_url_before_snapshot(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Non-page-selection tools should resolve page by URL and restore selection."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)

        FakeClient.responses = {
            "list_pages": make_response(
                "## Pages\n1: about:blank\n2: https://example.com/ [selected]"
            ),
            "select_page": make_response(
                "## Pages\n1: about:blank\n2: https://example.com/ [selected]"
            ),
            "take_snapshot": make_response(
                '## Latest page snapshot\nuid=1_0 RootWebArea url="https://example.com/"'
            ),
        }

        controller = PersistentChromeController(
            isolated=True,
            browser_url="http://127.0.0.1:9222",
            force_persistent=True,
        )
        state = BrowserState(
            browser_url="http://127.0.0.1:9222",
            selected_page_id=99,
            selected_page_url="https://example.com/",
            daemon_pid=99999,
            daemon_socket="/fake/socket",
        )
        monkeypatch.setattr(controller, "ensure_browser_state", lambda: state)
        monkeypatch.setattr(controller, "_connect_mcp", lambda s: FakeClient())

        controller.invoke_tool("take_snapshot", {})

        assert FakeClient.calls == [
            ("list_pages", {}),
            ("select_page", {"pageId": 2}),
            ("take_snapshot", {}),
        ]

    def test_new_page_skips_restore_and_updates_selected_page(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """new_page should not reselect the prior tab before opening a new one."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)

        FakeClient.responses = {
            "new_page": make_response(
                "## Pages\n1: about:blank\n2: https://example.com/ [selected]"
            ),
        }

        controller = PersistentChromeController(
            isolated=True,
            browser_url="http://127.0.0.1:9222",
            force_persistent=True,
        )
        state = BrowserState(
            browser_url="http://127.0.0.1:9222",
            selected_page_id=1,
            daemon_pid=99999,
            daemon_socket="/fake/socket",
        )
        monkeypatch.setattr(controller, "ensure_browser_state", lambda: state)
        monkeypatch.setattr(controller, "_connect_mcp", lambda s: FakeClient())

        controller.invoke_tool("new_page", {"url": "https://example.com"})

        # new_page is a navigation tool, so the post-call list_pages refresh
        # now runs (latent gap closed): the new tab's selected id+url are
        # re-read instead of being left stale.
        assert FakeClient.calls == [
            ("new_page", {"url": "https://example.com"}),
            ("list_pages", {}),
        ]
        assert state.selected_page_id == 2
        assert state.selected_page_url == "https://example.com/"
        assert controller.state_path.exists()

    def test_daemon_spawned_on_first_call(self, monkeypatch, tmp_path: Path) -> None:
        """Supervisor.ensure should spawn the daemon when no daemon_pid is set."""
        from browser_tools.daemon_supervisor import McpDaemonSupervisor

        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)

        supervisor = McpDaemonSupervisor("deadbeefdeadbeef")
        state = BrowserState(
            browser_url="http://127.0.0.1:9222",
            daemon_pid=None,
            daemon_socket=None,
        )

        spawn_calls: list[tuple] = []

        def fake_spawn(self_ref, st, cmd, **kwargs):
            spawn_calls.append((st, cmd))
            st.daemon_pid = 12345
            st.daemon_socket = str(tmp_path / "test.sock")

        monkeypatch.setattr(McpDaemonSupervisor, "_spawn", fake_spawn)
        supervisor.ensure(state, ["npx", "test"])

        assert len(spawn_calls) == 1
        assert state.daemon_pid == 12345

    def test_daemon_not_respawned_if_alive(self, monkeypatch, tmp_path: Path) -> None:
        """Supervisor.ensure should not spawn when the daemon is already alive."""
        from browser_tools.daemon_supervisor import McpDaemonSupervisor

        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)

        supervisor = McpDaemonSupervisor("deadbeefdeadbeef")
        state = BrowserState(
            browser_url="http://127.0.0.1:9222",
            daemon_pid=99999,
            daemon_socket="/fake/socket",
        )

        monkeypatch.setattr(supervisor, "is_alive", lambda s: True)

        spawn_calls: list[tuple] = []
        monkeypatch.setattr(
            supervisor, "_spawn", lambda st, cmd, **kw: spawn_calls.append((st, cmd))
        )

        supervisor.ensure(state, ["npx", "test"])
        assert len(spawn_calls) == 0

    def test_retries_once_after_recoverable_daemon_failure(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Recoverable daemon transport errors should trigger one clean retry."""
        from browser_tools.persistent_browser import MCPInvocationError

        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)

        controller = PersistentChromeController(
            isolated=True,
            browser_url="http://127.0.0.1:9222",
            force_persistent=True,
        )
        state = BrowserState(
            browser_url="http://127.0.0.1:9222",
            daemon_pid=99999,
            daemon_socket="/fake/socket",
        )
        monkeypatch.setattr(controller, "ensure_browser_state", lambda: state)

        attempts = {"count": 0}

        class FlakyClient(FakeClient):
            def __enter__(self) -> FakeClient:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise MCPInvocationError("MCP daemon connection closed unexpectedly")
                return super().__enter__()

        FakeClient.responses = {
            "take_snapshot": make_response("snapshot ok"),
        }
        monkeypatch.setattr(controller, "_connect_mcp", lambda s: FlakyClient())

        invalidations: list[BrowserState] = []
        monkeypatch.setattr(controller, "_invalidate_daemon", lambda s: invalidations.append(s))

        response = controller.invoke_tool("take_snapshot", {})

        assert response["result"]["content"][0]["text"] == "snapshot ok"
        assert attempts["count"] == 2
        assert invalidations == [state]


class TestBrowserStateDaemonFields:
    """Tests for daemon-related BrowserState persistence."""

    def test_daemon_fields_persist_to_disk(self, tmp_path: Path) -> None:
        """daemon_pid and daemon_socket should round-trip through JSON."""
        state = BrowserState(
            browser_url="http://127.0.0.1:9222",
            daemon_pid=42,
            daemon_socket="/tmp/test.sock",
        )
        state_path = tmp_path / "state.json"
        state.save(state_path)

        loaded = BrowserState.from_path(state_path)
        assert loaded is not None
        assert loaded.daemon_pid == 42
        assert loaded.daemon_socket == "/tmp/test.sock"

    def test_missing_daemon_fields_default_to_none(self, tmp_path: Path) -> None:
        """State files from before the daemon feature should load with None defaults."""
        state_path = tmp_path / "state.json"
        state_path.write_text('{"browser_url": "http://127.0.0.1:9222"}')

        loaded = BrowserState.from_path(state_path)
        assert loaded is not None
        assert loaded.daemon_pid is None
        assert loaded.daemon_socket is None


class TestSingletonLockHelpers:
    """Tests for stale-lock detection and recovery on auto-launch."""

    def test_read_singleton_lock_pid_parses_pid(self, tmp_path: Path) -> None:
        """SingletonLock target ``host-pid`` should yield the PID."""
        (tmp_path / "SingletonLock").symlink_to("Mac.local-12345")

        assert read_singleton_lock_pid(tmp_path) == 12345

    def test_read_singleton_lock_pid_missing_lock(self, tmp_path: Path) -> None:
        """No SingletonLock returns None."""
        assert read_singleton_lock_pid(tmp_path) is None

    def test_read_singleton_lock_pid_malformed_target(self, tmp_path: Path) -> None:
        """Targets without a numeric suffix return None."""
        (tmp_path / "SingletonLock").symlink_to("not-a-pid")

        assert read_singleton_lock_pid(tmp_path) is None

    def test_clean_stale_singleton_lock_removes_when_pid_dead(self, tmp_path: Path) -> None:
        """Stale singleton files are removed when their PID is no longer alive."""
        (tmp_path / "SingletonLock").symlink_to("host-1")  # PID 1 is init; stub it out
        (tmp_path / "SingletonCookie").symlink_to("host-1")
        (tmp_path / "SingletonSocket").symlink_to("/tmp/somewhere")

        with patch("browser_tools.process_utils.is_process_alive", return_value=False):
            clean_stale_singleton_lock(tmp_path)

        assert not (tmp_path / "SingletonLock").exists()
        assert not (tmp_path / "SingletonCookie").exists()
        assert not (tmp_path / "SingletonSocket").exists()

    def test_clean_stale_singleton_lock_preserves_live_lock(self, tmp_path: Path) -> None:
        """Live singleton files are left alone."""
        (tmp_path / "SingletonLock").symlink_to("host-12345")

        with patch("browser_tools.process_utils.is_process_alive", return_value=True):
            clean_stale_singleton_lock(tmp_path)

        assert (tmp_path / "SingletonLock").is_symlink()

    def test_find_chrome_debug_port_parses_ps_output(self) -> None:
        """The ps command line should be scanned for --remote-debugging-port."""
        fake = type(
            "R",
            (),
            {
                "returncode": 0,
                "stdout": "/Applications/Chrome --remote-debugging-port=51843 --user-data-dir=/x\n",
            },
        )()

        with patch("browser_tools.persistent_browser.subprocess.run", return_value=fake):
            assert find_chrome_debug_port(99999) == 51843

    def test_find_chrome_debug_port_no_port_arg(self) -> None:
        """A Chrome process without --remote-debugging-port returns None."""
        fake = type("R", (), {"returncode": 0, "stdout": "/Applications/Chrome --foo\n"})()

        with patch("browser_tools.persistent_browser.subprocess.run", return_value=fake):
            assert find_chrome_debug_port(99999) is None

    def test_find_chrome_debug_port_dead_process(self) -> None:
        """A dead PID (ps returncode != 0) returns None."""
        fake = type("R", (), {"returncode": 1, "stdout": ""})()

        with patch("browser_tools.persistent_browser.subprocess.run", return_value=fake):
            assert find_chrome_debug_port(99999) is None


class TestReuseExistingChrome:
    """Tests for PersistentChromeController._try_reuse_existing_chrome."""

    def test_returns_none_when_no_lock(self, tmp_path: Path) -> None:
        """Without a SingletonLock, no Chrome to reuse."""
        controller = PersistentChromeController(headless=True, isolated=True)

        result = controller._try_reuse_existing_chrome(tmp_path)

        assert result is None

    def test_returns_none_when_lock_pid_dead(self, tmp_path: Path) -> None:
        """Stale lock with dead PID is treated as no Chrome."""
        (tmp_path / "SingletonLock").symlink_to("host-1")
        controller = PersistentChromeController(headless=True, isolated=True)

        with patch("browser_tools.persistent_browser.is_process_alive", return_value=False):
            result = controller._try_reuse_existing_chrome(tmp_path)

        assert result is None

    def test_reuses_live_chrome_with_reachable_port(self, tmp_path: Path) -> None:
        """A live Chrome holding this dir with a reachable port is reused as-is."""
        (tmp_path / "SingletonLock").symlink_to("host-99999")
        controller = PersistentChromeController(headless=True, isolated=True)

        with (
            patch("browser_tools.persistent_browser.is_process_alive", return_value=True),
            patch(
                "browser_tools.persistent_browser.find_chrome_user_data_dir",
                return_value=tmp_path,
            ),
            patch("browser_tools.persistent_browser.find_chrome_debug_port", return_value=12345),
            patch("browser_tools.persistent_browser.is_devtools_available", return_value=True),
        ):
            result = controller._try_reuse_existing_chrome(tmp_path)

        assert result is not None
        assert result.browser_url == "http://127.0.0.1:12345"
        assert result.pid == 99999
        assert result.user_data_dir == str(tmp_path)

    def test_returns_none_when_port_unreachable(self, tmp_path: Path) -> None:
        """Live Chrome but unreachable debug port should not be reused."""
        (tmp_path / "SingletonLock").symlink_to("host-99999")
        controller = PersistentChromeController(headless=True, isolated=True)

        with (
            patch("browser_tools.persistent_browser.is_process_alive", return_value=True),
            patch(
                "browser_tools.persistent_browser.find_chrome_user_data_dir",
                return_value=tmp_path,
            ),
            patch("browser_tools.persistent_browser.find_chrome_debug_port", return_value=12345),
            patch("browser_tools.persistent_browser.is_devtools_available", return_value=False),
        ):
            result = controller._try_reuse_existing_chrome(tmp_path)

        assert result is None

    def test_returns_none_when_pid_holds_different_dir(self, tmp_path: Path) -> None:
        """A live PID running a different user-data-dir must not be reused.

        Guards against a recycled SingletonLock PID or a Chrome launched with an
        unrelated profile silently serving another session's requests.
        """
        (tmp_path / "SingletonLock").symlink_to("host-99999")
        controller = PersistentChromeController(headless=True, isolated=True)

        with (
            patch("browser_tools.persistent_browser.is_process_alive", return_value=True),
            patch(
                "browser_tools.persistent_browser.find_chrome_user_data_dir",
                return_value=tmp_path / "someone-else",
            ),
            patch("browser_tools.persistent_browser.find_chrome_debug_port", return_value=12345),
            patch("browser_tools.persistent_browser.is_devtools_available", return_value=True),
        ):
            result = controller._try_reuse_existing_chrome(tmp_path)

        assert result is None
