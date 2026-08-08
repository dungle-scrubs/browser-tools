"""Tests for attach_browser tool (M-1.2)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
from typing import Any

import pytest

from browser_tools.browser_session import (
    _format_live_profile_conflict_error,
    choose_live_profile_fallback,
    handle_attach_browser,
    handle_browser_session_status,
    handle_use_browser_session,
    select_default_controller,
)
from browser_tools.persistent_browser import (
    BrowserState,
    PersistentChromeController,
    enumerate_tabs,
    select_tab_by_url,
)
from browser_tools.profile_catalog import find_live_profiles
from browser_tools.session_store import (
    create_project_preferred_controller,
    load_active_attach_controller,
    load_session_override,
)


class FakeChromeHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler mimicking Chrome's /json/list endpoint."""

    tabs: list[dict[str, str]] = [  # noqa: RUF012
        {
            "id": "ABC123",
            "type": "page",
            "title": "Example",
            "url": "https://example.com/app",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/ABC123",
        },
        {
            "id": "DEF456",
            "type": "page",
            "title": "Admin",
            "url": "https://admin.example.com/dashboard",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/DEF456",
        },
        {
            "id": "GHI789",
            "type": "background_page",
            "title": "Extension Background",
            "url": "chrome-extension://abc/background.html",
        },
    ]

    def do_GET(self) -> None:
        """Handle GET requests."""
        if self.path == "/json/list":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(self.tabs).encode())
        elif self.path == "/json/version":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"Browser": "Chrome/130"}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress request logging in tests."""
        pass


@pytest.fixture
def fake_chrome():
    """Start a local HTTP server mimicking Chrome's debugging endpoint."""
    server = HTTPServer(("127.0.0.1", 0), FakeChromeHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


class TestEnumerateTabs:
    """Tests for tab enumeration via /json/list."""

    def test_returns_page_tabs(self, fake_chrome: str) -> None:
        """enumerate_tabs should return only page-type tabs."""
        tabs = enumerate_tabs(fake_chrome)
        assert len(tabs) == 2
        assert tabs[0]["title"] == "Example"
        assert tabs[1]["title"] == "Admin"

    def test_unreachable_endpoint_returns_empty(self) -> None:
        """Unreachable endpoint should return empty list."""
        tabs = enumerate_tabs("http://127.0.0.1:1")
        assert tabs == []

    def test_tab_has_expected_fields(self, fake_chrome: str) -> None:
        """Each tab should have id, title, url, and webSocketDebuggerUrl."""
        tabs = enumerate_tabs(fake_chrome)
        tab = tabs[0]
        assert "id" in tab
        assert "title" in tab
        assert "url" in tab
        assert "webSocketDebuggerUrl" in tab


class TestSelectTabByUrl:
    """Tests for tab auto-selection by URL pattern."""

    def test_selects_by_substring(self, fake_chrome: str) -> None:
        """URL substring match should select the correct tab."""
        tabs = enumerate_tabs(fake_chrome)
        selected = select_tab_by_url(tabs, "admin.example.com")
        assert selected is not None
        assert selected["title"] == "Admin"

    def test_selects_first_match(self, fake_chrome: str) -> None:
        """When multiple tabs match, the first one wins."""
        tabs = enumerate_tabs(fake_chrome)
        selected = select_tab_by_url(tabs, "example.com")
        assert selected is not None
        assert selected["title"] == "Example"

    def test_returns_none_for_no_match(self, fake_chrome: str) -> None:
        """No match should return None."""
        tabs = enumerate_tabs(fake_chrome)
        selected = select_tab_by_url(tabs, "nonexistent.com")
        assert selected is None

    def test_case_insensitive_match(self, fake_chrome: str) -> None:
        """URL matching should be case-insensitive."""
        tabs = enumerate_tabs(fake_chrome)
        selected = select_tab_by_url(tabs, "ADMIN.EXAMPLE.COM")
        assert selected is not None
        assert selected["title"] == "Admin"


class TestAttachBrowserTool:
    """Tests for the attach_browser tool integration."""

    def test_attach_sets_browser_url(self, monkeypatch, tmp_path: Path, fake_chrome: str) -> None:
        """attach_browser should configure the controller with the endpoint."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)

        controller = PersistentChromeController(
            isolated=False,
            browser_url=fake_chrome,
        )
        # Verify the controller is configured for the external browser
        assert controller.browser_url == fake_chrome
        assert controller.should_use_persistent_browser() is True

    def test_attach_does_not_launch_chrome(
        self, monkeypatch, tmp_path: Path, fake_chrome: str
    ) -> None:
        """When browser_url is set, no new Chrome process should be spawned."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)

        controller = PersistentChromeController(
            isolated=False,
            browser_url=fake_chrome,
        )
        state = controller.ensure_browser_state()
        # pid is None because we're attaching, not launching
        assert state.pid is None
        assert state.browser_url == fake_chrome

    def test_attach_persists_controller_config_across_tool_proxy_calls(
        self,
        monkeypatch,
        tmp_path: Path,
        fake_chrome: str,
    ) -> None:
        """attach_browser should save enough state for the next request to reuse it."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)

        controller_ref = [None]
        response = handle_attach_browser(
            controller_ref,
            {
                "endpoint": fake_chrome,
                "profile": "dev",
                "mode": "inspect",
                "stealth": True,
                "tab_url": "admin.example.com",
            },
        )

        loaded = load_active_attach_controller()
        assert response["result"]["content"][0]["text"].startswith("Connected to Chrome")
        assert loaded is not None
        assert loaded.browser_url == fake_chrome
        assert loaded.profile == "dev"
        assert loaded.mode == "inspect"
        assert loaded.stealth is True

        state = BrowserState.from_path(loaded.state_path)
        assert state is not None
        assert state.selected_page_url == "https://admin.example.com/dashboard"

    def test_project_preference_creates_headed_auth_profile(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """Project preference should select the same headed auth profile by default."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path / "cache")
        monkeypatch.setenv("CLAUDE_CWD", str(tmp_path))
        (tmp_path / ".browser-tools.json").write_text(
            json.dumps(
                {
                    "preferredSession": {
                        "mode": "headed-auth",
                        "profile": "google-auth",
                    }
                }
            )
        )

        controller = create_project_preferred_controller()

        assert controller is not None
        assert controller.headless is False
        assert controller.isolated is False
        assert controller.profile == "google-auth"
        assert controller.session_key == "profile_google-auth"

    def test_project_preference_can_use_external_endpoint(
        self,
        monkeypatch,
        tmp_path: Path,
        fake_chrome: str,
    ) -> None:
        """Project preference should support a stable external Chrome endpoint."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path / "cache")
        monkeypatch.setenv("CLAUDE_CWD", str(tmp_path))
        (tmp_path / ".tool-proxy").mkdir()
        (tmp_path / ".tool-proxy" / "browser-tools.json").write_text(
            json.dumps(
                {
                    "preferred_session": {
                        "mode": "headed-auth",
                        "endpoint": fake_chrome,
                        "profile": "google-auth",
                        "stealth": True,
                    }
                }
            )
        )

        controller = create_project_preferred_controller()

        assert controller is not None
        assert controller.browser_url == fake_chrome
        assert controller.profile == "google-auth"
        assert controller.stealth is True

    def test_use_browser_session_headless_overrides_project_auth(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """Explicit headless mode should override project headed-auth preference."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path / "cache")
        monkeypatch.setenv("CLAUDE_CWD", str(tmp_path))
        (tmp_path / ".browser-tools.json").write_text(
            json.dumps({"preferredSession": {"mode": "headed-auth", "profile": "google-auth"}})
        )

        response = handle_use_browser_session([None], {"mode": "headless"})
        override = load_session_override()

        assert response["result"]["content"][0]["text"].startswith(
            "Browser session override set: headless"
        )
        assert override is not None
        assert override.mode == "headless"
        controller = create_project_preferred_controller()
        assert controller is not None
        assert controller.profile == "google-auth"

    def test_use_browser_session_headed_defaults_to_project_bucket(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """Explicit headed mode with no profile uses this project's own bucket.

        Auth lands per-project (not a shared global "google-auth" named
        profile) so each project keeps its own login and the default headless
        session reuses the same cookies.
        """
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path / "cache")
        monkeypatch.setenv("CLAUDE_CWD", str(tmp_path))

        response = handle_use_browser_session([None], {"mode": "headed"})
        override = load_session_override()

        assert "Browser session override set: headed" in response["result"]["content"][0]["text"]
        assert override is not None
        assert override.mode == "headed"
        assert override.profile is None

    def test_use_browser_session_headless_auth_uses_project_profile(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """Headless auth should use the project auth profile when available."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path / "cache")
        monkeypatch.setenv("CLAUDE_CWD", str(tmp_path))
        (tmp_path / ".browser-tools.json").write_text(
            json.dumps({"preferredSession": {"mode": "headed-auth", "profile": "project-auth"}})
        )

        handle_use_browser_session([None], {"mode": "headless-auth"})
        override = load_session_override()

        assert override is not None
        assert override.mode == "headless-auth"
        assert override.profile == "project-auth"

    def test_browser_session_status_reports_override(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """browser_session_status should expose the selected session source."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path / "cache")
        monkeypatch.setenv("CLAUDE_CWD", str(tmp_path))
        handle_use_browser_session([None], {"mode": "headed-auth", "profile": "dev-auth"})

        response = handle_browser_session_status({})
        status = json.loads(response["result"]["content"][0]["text"])

        assert status["selected_source"] == "override"
        assert status["override"]["profile"] == "dev-auth"


class TestAttachByProfile:
    """Tests for attach_browser(profile=...) with auto-discovered endpoint."""

    def _setup_profile(self, monkeypatch, tmp_path: Path, fake_chrome: str, profile: str) -> Path:
        from urllib.parse import urlparse

        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        profile_dir = tmp_path / "profiles" / profile
        profile_dir.mkdir(parents=True)
        port = urlparse(fake_chrome).port

        monkeypatch.setattr("browser_tools.process_utils.read_singleton_lock_pid", lambda d: 9999)
        monkeypatch.setattr("browser_tools.process_utils.is_process_alive", lambda pid: True)
        monkeypatch.setattr("browser_tools.process_utils.find_chrome_debug_port", lambda pid: port)
        monkeypatch.setattr(
            "browser_tools.process_utils.find_chrome_user_data_dir",
            lambda pid: profile_dir.resolve(),
        )
        return profile_dir

    def test_attach_with_profile_only_discovers_endpoint(
        self, monkeypatch, tmp_path: Path, fake_chrome: str
    ) -> None:
        """Passing only profile should resolve the endpoint via the singleton lock."""
        self._setup_profile(monkeypatch, tmp_path, fake_chrome, "dev")

        response = handle_attach_browser([None], {"profile": "dev"})

        text = response["result"]["content"][0]["text"]
        assert "auto-discovered from profile 'dev'" in text
        assert fake_chrome in text

    def test_attach_with_no_profile_or_endpoint_errors(self, monkeypatch, tmp_path: Path) -> None:
        """Missing both profile and endpoint should return a clear error."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        response = handle_attach_browser([None], {})
        assert response["result"]["isError"] is True
        assert "endpoint" in response["result"]["content"][0]["text"]
        assert "profile" in response["result"]["content"][0]["text"]

    def test_attach_with_unknown_profile_errors(self, monkeypatch, tmp_path: Path) -> None:
        """Unknown profile name should suggest list_profiles."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        response = handle_attach_browser([None], {"profile": "ghost"})
        assert response["result"]["isError"] is True
        text = response["result"]["content"][0]["text"]
        assert "ghost" in text
        assert "list_profiles" in text

    def test_attach_profile_no_running_chrome_errors(self, monkeypatch, tmp_path: Path) -> None:
        """Profile dir present but no Chrome should hint at use_browser_session."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        (tmp_path / "profiles" / "dev").mkdir(parents=True)
        monkeypatch.setattr("browser_tools.process_utils.read_singleton_lock_pid", lambda d: None)

        response = handle_attach_browser([None], {"profile": "dev"})

        assert response["result"]["isError"] is True
        text = response["result"]["content"][0]["text"]
        assert "use_browser_session" in text
        assert "headed-auth" in text

    def test_attach_profile_dead_port_returns_actionable_error(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Live process + unreachable DevTools should return E001 with recovery steps."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        (tmp_path / "profiles" / "dev").mkdir(parents=True)
        monkeypatch.setattr("browser_tools.process_utils.read_singleton_lock_pid", lambda d: 4242)
        monkeypatch.setattr("browser_tools.process_utils.is_process_alive", lambda pid: True)
        monkeypatch.setattr("browser_tools.process_utils.find_chrome_debug_port", lambda pid: 9222)
        # is_devtools_available returns False — port is dead.
        monkeypatch.setattr("browser_tools.process_utils.is_devtools_available", lambda url: False)
        monkeypatch.setattr(
            "browser_tools.process_utils.find_listeners_on_port", lambda p: [11111, 22222]
        )

        response = handle_attach_browser([None], {"profile": "dev"})

        assert response["result"]["isError"] is True
        text = response["result"]["content"][0]["text"]
        assert "E001" in text
        assert "4242" in text
        assert "9222" in text
        assert "11111" in text
        assert "Recovery options" in text

    def test_attach_endpoint_profile_mismatch_rejected(
        self, monkeypatch, tmp_path: Path, fake_chrome: str
    ) -> None:
        """If endpoint points at a Chrome running a different profile, reject."""
        from urllib.parse import urlparse

        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        (tmp_path / "profiles" / "dev").mkdir(parents=True)
        port = urlparse(fake_chrome).port
        # Listener on the endpoint port returns a PID running a *different* dir.
        monkeypatch.setattr("browser_tools.process_utils.find_listeners_on_port", lambda p: [7777])
        monkeypatch.setattr(
            "browser_tools.process_utils.find_chrome_user_data_dir",
            lambda pid: (tmp_path / "profiles" / "other-profile").resolve(),
        )

        response = handle_attach_browser([None], {"profile": "dev", "endpoint": fake_chrome})

        assert response["result"]["isError"] is True
        text = response["result"]["content"][0]["text"]
        assert "other-profile" in text
        assert "attach_browser(profile='dev')" in text


class TestListProfilesStatus:
    """Tests for the enriched list_profiles handler."""

    def test_list_profiles_reports_runtime_status(
        self, monkeypatch, tmp_path: Path, fake_chrome: str
    ) -> None:
        from urllib.parse import urlparse

        from browser_tools.browser_session import handle_list_profiles

        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        (tmp_path / "profiles" / "live-app").mkdir(parents=True)
        (tmp_path / "profiles" / "stopped-app").mkdir(parents=True)

        port = urlparse(fake_chrome).port

        def fake_lock(profile_dir):
            return 6414 if profile_dir.name == "live-app" else None

        monkeypatch.setattr("browser_tools.process_utils.read_singleton_lock_pid", fake_lock)
        monkeypatch.setattr("browser_tools.process_utils.is_process_alive", lambda pid: True)
        monkeypatch.setattr("browser_tools.process_utils.find_chrome_debug_port", lambda pid: port)

        response = handle_list_profiles({})
        payload = json.loads(response["result"]["content"][0]["text"])

        names = {p["profile"]: p for p in payload["profiles"]}
        assert names["live-app"]["devtools_alive"] is True
        assert names["live-app"]["pid"] == 6414
        assert names["live-app"]["endpoint"] == fake_chrome
        assert names["stopped-app"]["devtools_alive"] is False
        assert names["stopped-app"]["pid"] is None
        assert "live-app" in payload["summary"]
        assert "use_browser_session" in payload["summary"]


class TestLiveProfileFallback:
    """Tests for the belt-and-suspenders live-profile fallback in create_session."""

    @staticmethod
    def _start_fake_chrome() -> tuple[str, HTTPServer]:
        """Start a fake Chrome server on an ephemeral port.

        Returns:
            ``(url, server)`` so the caller can stop the server.
        """
        server = HTTPServer(("127.0.0.1", 0), FakeChromeHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return f"http://127.0.0.1:{port}", server

    @staticmethod
    def _setup_profiles(monkeypatch, tmp_path: Path, profiles: dict[str, int | None]):
        """Create profile directories and patch the runtime probes.

        Args:
            monkeypatch: Pytest monkeypatch fixture.
            tmp_path: Test cache root.
            profiles: ``{name: port_or_None}``. ``port`` makes the profile look
                live and answering on that port; ``None`` makes the singleton
                lock empty (stopped).
        """
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        for name in profiles:
            (tmp_path / "profiles" / name).mkdir(parents=True)

        # Map profile dir -> fake PID so describe_profile_runtime treats it as alive.
        name_to_pid = {name: 1000 + i for i, name in enumerate(profiles)}
        pid_to_port = {name_to_pid[name]: port for name, port in profiles.items() if port}

        def fake_lock(profile_dir):
            return name_to_pid.get(profile_dir.name) if profiles.get(profile_dir.name) else None

        monkeypatch.setattr("browser_tools.process_utils.read_singleton_lock_pid", fake_lock)
        monkeypatch.setattr("browser_tools.process_utils.is_process_alive", lambda pid: True)
        monkeypatch.setattr(
            "browser_tools.process_utils.find_chrome_debug_port",
            lambda pid: pid_to_port.get(pid),
        )

    def test_find_live_profiles_filters_to_alive(self, monkeypatch, tmp_path: Path) -> None:
        """find_live_profiles should return only profiles with reachable DevTools."""
        url, server = self._start_fake_chrome()
        try:
            from urllib.parse import urlparse

            port = urlparse(url).port
            self._setup_profiles(monkeypatch, tmp_path, {"alive": port, "stopped": None})

            live = find_live_profiles()
            names = [info["profile"] for info in live]
            assert names == ["alive"]
            assert live[0]["devtools_alive"] is True
            assert live[0]["endpoint"] == url
        finally:
            server.shutdown()

    def test_choose_live_profile_fallback_picks_sole_live(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A single live profile should yield a headed-auth reuse controller."""
        url, server = self._start_fake_chrome()
        try:
            from urllib.parse import urlparse

            self._setup_profiles(monkeypatch, tmp_path, {"google-auth": urlparse(url).port})

            live = find_live_profiles()
            controller = choose_live_profile_fallback(live)
            assert controller is not None
            assert controller.profile == "google-auth"
            assert controller.headless is False
            assert controller.isolated is False
            assert len(live) == 1
        finally:
            server.shutdown()

    def test_choose_live_profile_fallback_ignores_unnamed_session(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A hashed session key belongs to another project — never auto-attach to it."""
        url, server = self._start_fake_chrome()
        try:
            from urllib.parse import urlparse

            session_key = "a1b2c3d4e5f60718"
            self._setup_profiles(monkeypatch, tmp_path, {session_key: urlparse(url).port})

            live = find_live_profiles()
            assert [info["profile"] for info in live] == [session_key]
            assert live[0]["named"] is False
            assert choose_live_profile_fallback(live) is None
        finally:
            server.shutdown()

    def test_choose_live_profile_fallback_skips_when_multiple_live(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Two live profiles should yield no auto-pick — caller must disambiguate."""
        url_a, server_a = self._start_fake_chrome()
        url_b, server_b = self._start_fake_chrome()
        try:
            from urllib.parse import urlparse

            self._setup_profiles(
                monkeypatch,
                tmp_path,
                {"google-auth": urlparse(url_a).port, "dev": urlparse(url_b).port},
            )

            live = find_live_profiles()
            assert choose_live_profile_fallback(live) is None
            names = {info["profile"] for info in live}
            assert names == {"google-auth", "dev"}
        finally:
            server_a.shutdown()
            server_b.shutdown()

    def test_choose_live_profile_fallback_returns_none_when_no_profiles(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """No live profiles should leave the caller to use the default."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        (tmp_path / "profiles").mkdir()

        live = find_live_profiles()
        assert live == []
        assert choose_live_profile_fallback(live) is None

    def test_select_default_controller_prefers_live_profile(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """With one live profile, the default selection auto-attaches to it."""
        url, server = self._start_fake_chrome()
        try:
            from urllib.parse import urlparse

            self._setup_profiles(monkeypatch, tmp_path, {"google-auth": urlparse(url).port})

            controller, conflict = select_default_controller()
            assert conflict is None
            assert controller.profile == "google-auth"
            assert controller.headless is False
            assert controller.isolated is False
        finally:
            server.shutdown()

    def test_select_default_controller_reports_conflict_when_multiple_live(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Multiple live profiles should yield default + conflict descriptors."""
        url_a, server_a = self._start_fake_chrome()
        url_b, server_b = self._start_fake_chrome()
        try:
            from urllib.parse import urlparse

            self._setup_profiles(
                monkeypatch,
                tmp_path,
                {"google-auth": urlparse(url_a).port, "dev": urlparse(url_b).port},
            )

            controller, conflict = select_default_controller()
            # Default controller still returned so session tools keep working.
            assert controller.isolated is True
            assert controller.headless is True
            assert conflict is not None
            assert {info["profile"] for info in conflict} == {"google-auth", "dev"}
        finally:
            server_a.shutdown()
            server_b.shutdown()

    def test_select_default_controller_reuses_own_live_session(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """This project's own live session wins over auto-attaching elsewhere.

        Hopping to another profile would abandon a Chrome this project
        launched, leaving it to linger as a second dock icon.
        """
        url_own, server_own = self._start_fake_chrome()
        url_named, server_named = self._start_fake_chrome()
        try:
            from urllib.parse import urlparse

            from browser_tools.persistent_browser import build_session_key

            own_key = build_session_key(browser_url=None, isolated=True, channel="canary")
            self._setup_profiles(
                monkeypatch,
                tmp_path,
                {
                    own_key: urlparse(url_own).port,
                    "google-auth": urlparse(url_named).port,
                },
            )

            controller, conflict = select_default_controller()
            assert conflict is None
            assert controller.session_key == own_key
            assert controller.headless is True
            assert controller.isolated is True
        finally:
            server_own.shutdown()
            server_named.shutdown()

    def test_select_default_controller_ignores_other_projects_sessions(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Another project's hashed session must not block or be attached to."""
        url_other, server_other = self._start_fake_chrome()
        url_named, server_named = self._start_fake_chrome()
        try:
            from urllib.parse import urlparse

            self._setup_profiles(
                monkeypatch,
                tmp_path,
                {
                    "fedcba9876543210": urlparse(url_other).port,
                    "google-auth": urlparse(url_named).port,
                },
            )

            controller, conflict = select_default_controller()
            assert conflict is None
            assert controller.profile == "google-auth"
        finally:
            server_other.shutdown()
            server_named.shutdown()

    def test_select_default_controller_falls_through_to_headless_when_no_live(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Zero live profiles should produce the default headless-isolated controller."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path)
        (tmp_path / "profiles").mkdir()

        controller, conflict = select_default_controller()
        assert conflict is None
        assert controller.headless is True
        assert controller.isolated is True

    def test_conflict_error_lists_each_live_profile(self) -> None:
        """The conflict error response should name every live profile + endpoint."""
        live = [
            {
                "profile": "google-auth",
                "endpoint": "http://127.0.0.1:52768",
                "current_url": "https://example.com/",
                "tab_count": 2,
            },
            {
                "profile": "dev",
                "endpoint": "http://127.0.0.1:63819",
                "current_url": None,
                "tab_count": 1,
            },
        ]

        response = _format_live_profile_conflict_error(live)
        text = response["result"]["content"][0]["text"]

        assert response["result"]["isError"] is True
        assert "google-auth" in text
        assert "dev" in text
        assert "http://127.0.0.1:52768" in text
        assert "http://127.0.0.1:63819" in text
        assert "use_browser_session" in text
        assert "attach_browser" in text
