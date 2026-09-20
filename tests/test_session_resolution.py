"""Tests for session resolution -- ``resolve_session_controller``.

One owner of the priority order (explicit override, then a sole live profile,
then default headless), so the session bootstrap and ``browser_session_status``
cannot drift apart.

This file previously also covered ``dispatch_session_tool``, the session-adapter
tool dispatcher. That dispatcher had no production caller after RFC-01 Phase 4
retired the tool-proxy app, and was deleted with it (#68). ``session_store``
still reaches ``resolve_session_controller``, which is what these tests hold.
"""

from __future__ import annotations


class TestSessionResolution:
    """resolve_session_controller owns the priority order; status delegates to it."""

    def test_override_wins(self, monkeypatch, tmp_path) -> None:
        """An explicit override resolves to the override source."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path / "cache")
        monkeypatch.setenv("CLAUDE_CWD", str(tmp_path))
        from browser_tools.browser_session import (
            handle_use_browser_session,
            resolve_session_controller,
        )

        handle_use_browser_session([None], {"mode": "headed-auth", "profile": "dev"})
        resolution = resolve_session_controller()
        assert resolution.source == "override"
        assert resolution.controller.profile == "dev"

    def test_default_headless_when_nothing_configured(self, monkeypatch, tmp_path) -> None:
        """No config and no live profiles resolve to the default headless source."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path / "cache")
        (tmp_path / "cache").mkdir()
        monkeypatch.setenv("CLAUDE_CWD", str(tmp_path))
        from browser_tools.browser_session import resolve_session_controller

        resolution = resolve_session_controller()
        assert resolution.source == "default_headless"
        assert resolution.controller.headless is True

    def test_status_source_matches_resolver(self, monkeypatch, tmp_path) -> None:
        """browser_session_status.selected_source tracks resolve_session_controller (no drift)."""
        monkeypatch.setattr("browser_tools.session_layout.CACHE_DIR", tmp_path / "cache")
        monkeypatch.setenv("CLAUDE_CWD", str(tmp_path))
        from browser_tools.browser_session import (
            handle_use_browser_session,
            resolve_session_controller,
        )
        from browser_tools.session_store import get_browser_session_status

        handle_use_browser_session([None], {"mode": "headed-auth", "profile": "z"})
        status = get_browser_session_status()
        assert status["selected_source"] == resolve_session_controller().source == "override"
