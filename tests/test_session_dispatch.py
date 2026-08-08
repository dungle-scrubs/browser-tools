"""Tests for the session-adapter tool dispatcher - ``dispatch_session_tool``.

The session-adapter counterpart of ``mcp_daemon.dispatch_tool``. Before this
seam existed, ``call_tool`` was a nested closure with no test surface, so its
cross-cutting policies (single-tab reuse, headless-to-headed auth-wall
promotion) were tested as private free-function imports - the *routing order*
that integrates them was never exercised. These tests drive the whole dispatch
through one interface with fakes, the same way ``test_dispatch.py`` covers the
Daemon.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from browser_tools.browser_session import SessionDispatchContext, dispatch_session_tool
from browser_tools.mcp_response import extract_text_items


def _ctx(
    controller_ref: list[Any] | None = None,
    camoufox_ref: list[Any] | None = None,
    conflict: list[dict[str, Any]] | None = None,
) -> SessionDispatchContext:
    """Build a dispatch context from the given mutable refs.

    ``conflict`` is the list of live-profile descriptors (or None); it is wrapped
    in a single-element list because ``live_profile_conflict[0]`` unwraps to it,
    mirroring how ``create_session`` stores the conflict.
    """
    return SessionDispatchContext(
        controller_ref=controller_ref if controller_ref is not None else [None],
        camoufox_ref=camoufox_ref if camoufox_ref is not None else [None],
        live_profile_conflict=[conflict],
    )


def _text(response: dict[str, Any]) -> str:
    return "".join(extract_text_items(response))


class FakeController:
    """Records invoke_tool calls and serves canned responses by tool name."""

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None, headless: bool = False):
        self._responses = responses or {}
        self.headless = headless
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke_tool(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, dict(params)))
        return self._responses.get(name, {"result": {"content": [{"type": "text", "text": "ok"}]}})


class FakeCamoufox:
    """Records call_tool calls and serves a canned result."""

    def __init__(self, result: dict[str, Any] | None = None):
        self._result = result if result is not None else {"result": {"status": "done"}}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((tool, dict(args or {})))
        return self._result


# --------------------------------------------------------------------------- #
# Backend selection: Camoufox vs Chrome
# --------------------------------------------------------------------------- #


def test_default_tool_routes_to_chrome_backend() -> None:
    controller = FakeController()
    response = dispatch_session_tool(_ctx([controller]), controller, "take_snapshot", {})
    assert (controller.calls[0][0], _text(response)) == ("take_snapshot", "ok")


def test_camoufox_active_routes_mapped_tool_to_camoufox() -> None:
    """When a Camoufox session is active, a mapped automation tool skips Chrome."""
    controller = FakeController()
    camoufox = FakeCamoufox()
    dispatch_session_tool(
        _ctx([controller], [camoufox]), controller, "navigate_page", {"url": "https://x"}
    )
    assert controller.calls == []
    assert camoufox.calls[0][0] == "navigate"  # CAMOUFOX_TOOL_MAP: navigate_page -> navigate


def test_camoufox_inactive_falls_through_to_chrome() -> None:
    """No active Camoufox session: a mapped tool goes to Chrome as usual."""
    controller = FakeController()
    dispatch_session_tool(_ctx([controller]), controller, "navigate_page", {"url": "https://x"})
    assert controller.calls[0][0] == "navigate_page"


def test_camoufox_exclusive_tool_without_session_errors() -> None:
    """wait_for_human requires an active Camoufox session."""
    controller = FakeController()
    response = dispatch_session_tool(_ctx([controller]), controller, "wait_for_human", {})
    assert "requires an active Camoufox session" in _text(response)
    assert controller.calls == []


# --------------------------------------------------------------------------- #
# Live-profile conflict gate
# --------------------------------------------------------------------------- #


def test_conflict_gate_refuses_non_session_tool() -> None:
    """With an unresolved multi-profile conflict, a normal tool is refused."""
    controller = FakeController()
    conflict = [{"profile": "a", "endpoint": "http://127.0.0.1:1"}, {"profile": "b"}]
    response = dispatch_session_tool(
        _ctx([controller], conflict=conflict), controller, "take_snapshot", {}
    )
    assert controller.calls == []
    assert "Multiple browsers are live" in _text(response)


def test_conflict_gate_clears_once_controller_swapped() -> None:
    """attach_browser / use_browser_session clear the gate by swapping the controller."""
    default = FakeController()
    swapped = FakeController()
    # controller_ref holds the swapped controller; the passed-in `controller`
    # is the old default. The gate only fires when controller_ref[0] is still
    # the passed-in controller, so a swapped controller bypasses it.
    response = dispatch_session_tool(
        _ctx([swapped], conflict=[{"profile": "a", "named": True}]), default, "click", {}
    )
    assert (swapped.calls[0][0], _text(response)) == ("click", "ok")


# --------------------------------------------------------------------------- #
# Single-tab + auth-wall promotion integration (the previously untested order)
# --------------------------------------------------------------------------- #


def _auth_wall_response() -> dict[str, Any]:
    return {"result": {"content": [{"type": "text", "text": "[medium] auth_wall (form): login"}]}}


def test_new_page_uses_single_tab_then_promotes_on_auth_wall() -> None:
    """new_page on a headless controller: single-tab reuse, then auth promotion."""
    controller = FakeController(
        {
            "list_pages": {"result": {"content": [{"type": "text", "text": "0: about:blank [selected]"}]}},
            "select_page": {"result": {"content": [{"type": "text", "text": "ok"}]}},
            "navigate_page": _auth_wall_response(),
        },
        headless=True,
    )
    ref = [controller]
    headed = FakeController(headless=False)

    with patch("browser_tools.browser_session._promote_headless_to_headed", return_value=headed):
        response = dispatch_session_tool(_ctx(ref), controller, "new_page", {"url": "https://x/login"})

    # Single-tab path ran: list_pages + select_page + navigate_page were issued.
    names = [c[0] for c in controller.calls]
    assert "select_page" in names and "navigate_page" in names
    # Promotion swapped the controller and rewrote the response.
    assert ref[0] is headed
    assert "switched from headless to a headed" in _text(response)


def test_navigate_page_url_promotes_on_auth_wall() -> None:
    """navigate_page (type=url) on a headless controller promotes on an auth wall."""
    controller = FakeController({"navigate_page": _auth_wall_response()}, headless=True)
    ref = [controller]
    headed = FakeController(headless=False)

    with patch("browser_tools.browser_session._promote_headless_to_headed", return_value=headed):
        dispatch_session_tool(_ctx(ref), controller, "navigate_page", {"type": "url", "url": "https://x"})

    assert ref[0] is headed


def test_navigate_page_reload_does_not_promote() -> None:
    """navigate_page type=reload is not a fresh login-wall event: no promotion check."""
    controller = FakeController({"navigate_page": _auth_wall_response()}, headless=True)
    ref = [controller]

    with patch(
        "browser_tools.browser_session._promote_headless_to_headed"
    ) as promote, patch(
        "browser_tools.browser_session._maybe_promote_on_auth_wall"
    ) as maybe:
        maybe.return_value = {"result": {"content": [{"type": "text", "text": "reloaded"}]}}
        dispatch_session_tool(_ctx(ref), controller, "navigate_page", {"type": "reload"})

    # The auth-promotion hook must not run for a reload.
    assert maybe.call_count == 0
    assert promote.call_count == 0


def test_headed_controller_skips_promotion() -> None:
    """A headed controller never promotes, even on an auth-wall navigation."""
    controller = FakeController({"navigate_page": _auth_wall_response()}, headless=False)
    response = dispatch_session_tool(
        _ctx([controller]), controller, "navigate_page", {"url": "https://x"}
    )
    assert _text(response) == "[medium] auth_wall (form): login"


# ---------------------------------------------------------------------------
# Session resolution: one owner for the bootstrap and status (no drift)
# ---------------------------------------------------------------------------


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
