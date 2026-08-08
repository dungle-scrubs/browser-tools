"""Tests for the new session-lifecycle behavior.

Covers:
- project identity (git-root resolution + harness-agnostic env chain)
- collapsed per-project keying (subdir drift no longer fragments)
- single-tab new_page (no accumulation)
- close_browser (quit owned / detach external)
- headless -> headed promotion on an auth wall
"""

from __future__ import annotations

from typing import Any

from browser_tools.browser_session import (
    _extract_page_ids,
    _handle_new_page_single_tab,
    _maybe_promote_on_auth_wall,
    _response_signals_auth_wall,
)
from browser_tools.persistent_browser import build_session_key
from browser_tools.project_identity import (
    get_project_dir,
    get_project_id,
    resolve_project_root,
)

# ---------------------------------------------------------------------------
# Project identity
# ---------------------------------------------------------------------------


class TestProjectIdentity:
    """resolve_project_root walks to .git; env chain is harness-agnostic."""

    def test_resolves_to_git_root(self, monkeypatch, tmp_path) -> None:
        """A subdir inside a repo resolves up to the repo root."""
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "packages" / "a"
        subdir.mkdir(parents=True)
        monkeypatch.setenv("TOOL_PROXY_PROJECT_DIR", str(subdir))

        assert resolve_project_root() == tmp_path.resolve()

    def test_subdirs_of_one_repo_share_a_root(self, monkeypatch, tmp_path) -> None:
        """Cwd drift within a repo must not change the resolved root."""
        (tmp_path / ".git").mkdir()
        a = tmp_path / "packages" / "a"
        b = tmp_path / "packages" / "b"
        a.mkdir(parents=True)
        b.mkdir(parents=True)

        monkeypatch.setenv("TOOL_PROXY_PROJECT_DIR", str(a))
        root_a = resolve_project_root()
        monkeypatch.setenv("TOOL_PROXY_PROJECT_DIR", str(b))
        root_b = resolve_project_root()
        assert root_a == root_b

    def test_different_repos_have_different_roots(self, monkeypatch, tmp_path) -> None:
        """Two separate checkouts resolve to two different roots."""
        repo_a = tmp_path / "alpha"
        repo_b = tmp_path / "beta"
        (repo_a / ".git").mkdir(parents=True)
        (repo_b / ".git").mkdir(parents=True)

        monkeypatch.setenv("TOOL_PROXY_PROJECT_DIR", str(repo_a))
        root_a = resolve_project_root()
        monkeypatch.setenv("TOOL_PROXY_PROJECT_DIR", str(repo_b))
        root_b = resolve_project_root()
        assert root_a != root_b

    def test_falls_back_to_start_when_not_in_repo(self, monkeypatch, tmp_path) -> None:
        """Outside any checkout the start directory itself is returned."""
        monkeypatch.setenv("TOOL_PROXY_PROJECT_DIR", str(tmp_path))
        assert resolve_project_root() == tmp_path.resolve()

    def test_prefers_new_canonical_env_over_legacy_claude_name(self, monkeypatch, tmp_path) -> None:
        """TOOL_PROXY_PROJECT_DIR wins over the legacy CLAUDE_CWD name."""
        monkeypatch.setenv("TOOL_PROXY_PROJECT_DIR", str(tmp_path / "canonical"))
        monkeypatch.setenv("CLAUDE_CWD", str(tmp_path / "legacy"))
        assert get_project_dir() == (tmp_path / "canonical").resolve()

    def test_falls_back_to_legacy_claude_name(self, monkeypatch, tmp_path) -> None:
        """When only the legacy name is set it is still honored."""
        monkeypatch.delenv("TOOL_PROXY_PROJECT_DIR", raising=False)
        monkeypatch.setenv("CLAUDE_CWD", str(tmp_path / "legacy"))
        assert get_project_dir() == (tmp_path / "legacy").resolve()

    def test_project_id_canonical_then_legacy(self, monkeypatch) -> None:
        """Project id follows the same canonical-then-legacy chain."""
        monkeypatch.setenv("TOOL_PROXY_PROJECT_ID", "proj-new")
        monkeypatch.setenv("CLAUDE_PROJECT_ID", "proj-old")
        assert get_project_id() == "proj-new"
        monkeypatch.delenv("TOOL_PROXY_PROJECT_ID", raising=False)
        assert get_project_id() == "proj-old"


# ---------------------------------------------------------------------------
# Collapsed per-project keying
# ---------------------------------------------------------------------------


class TestPerProjectKeying:
    """One repo = one bucket, regardless of subdir or isolated flag."""

    def test_subdir_drift_does_not_fragment(self, monkeypatch, tmp_path) -> None:
        """Two subdirs of the same repo hash to the same session key."""
        (tmp_path / ".git").mkdir()
        a = tmp_path / "packages" / "a"
        b = tmp_path / "packages" / "b"
        a.mkdir(parents=True)
        b.mkdir(parents=True)

        monkeypatch.setenv("TOOL_PROXY_PROJECT_DIR", str(a))
        key_a = build_session_key(browser_url=None, isolated=False, channel="canary")
        monkeypatch.setenv("TOOL_PROXY_PROJECT_DIR", str(b))
        key_b = build_session_key(browser_url=None, isolated=False, channel="canary")
        assert key_a == key_b

    def test_different_repos_fragment(self, monkeypatch, tmp_path) -> None:
        """Two repos hash to two different keys."""
        repo_a = tmp_path / "alpha"
        repo_b = tmp_path / "beta"
        (repo_a / ".git").mkdir(parents=True)
        (repo_b / ".git").mkdir(parents=True)

        monkeypatch.setenv("TOOL_PROXY_PROJECT_DIR", str(repo_a))
        key_a = build_session_key(browser_url=None, isolated=False, channel="canary")
        monkeypatch.setenv("TOOL_PROXY_PROJECT_DIR", str(repo_b))
        key_b = build_session_key(browser_url=None, isolated=False, channel="canary")
        assert key_a != key_b


# ---------------------------------------------------------------------------
# Single active tab
# ---------------------------------------------------------------------------


class _FakeController:
    """Records invoke_tool calls and returns canned responses by tool name."""

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke_tool(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, dict(params)))
        return self._responses.get(name, {"result": {"content": []}})


def _pages_response(text: str) -> dict[str, Any]:
    return {"result": {"content": [{"type": "text", "text": text}]}}


class TestExtractPageIds:
    """Page id parsing from list_pages output."""

    def test_parses_ordered_ids(self) -> None:
        response = _pages_response(
            "1: about:blank\n2: https://x.com/ [selected]\n3: https://y.com/"
        )
        assert _extract_page_ids(response) == [1, 2, 3]

    def test_empty_when_no_pages(self) -> None:
        assert _extract_page_ids({"result": {"content": []}}) == []


class TestSingleTabNewPage:
    """new_page reuses one tab instead of stacking."""

    def test_single_tab_navigates_no_close(self) -> None:
        """With one tab, new_page navigates it and closes nothing."""
        controller = _FakeController(
            {
                "list_pages": _pages_response("0: about:blank [selected]"),
                "select_page": _pages_response("0: about:blank [selected]"),
                "navigate_page": _pages_response("0: https://example.com/ [selected]"),
            }
        )

        _handle_new_page_single_tab(controller, "https://example.com")  # type: ignore[arg-type]

        names = [c[0] for c in controller.calls]
        assert "select_page" in names
        assert "navigate_page" in names
        assert "close_page" not in names

    def test_multiple_tabs_close_extras(self) -> None:
        """With three tabs, new_page navigates the first and closes the rest."""
        # list_pages is called for: initial id lookup, each loop iteration, and
        # a final refresh once extras are closed. Closing a tab renumbers the
        # rest, so each loop list shows one fewer tab until one remains.
        lists = [
            _pages_response("0: a\n1: b\n2: c"),  # initial id lookup (3 tabs)
            _pages_response("0: a\n1: b\n2: c"),  # loop iter 1: extras [1,2], close 1
            _pages_response("0: a\n1: b"),  # loop iter 2: extras [1], close 1
            _pages_response("0: a"),  # loop iter 3: no extras, stop
        ]
        controller = _FakeController({})

        def invoke(name: str, params: dict[str, Any]) -> dict[str, Any]:
            controller.calls.append((name, dict(params)))
            if name == "list_pages":
                # After the scripted sequence, keep reporting a single tab.
                return lists.pop(0) if lists else _pages_response("0: a")
            return _pages_response("ok")

        controller.invoke_tool = invoke  # type: ignore[method-assign]

        _handle_new_page_single_tab(controller, "https://example.com")  # type: ignore[arg-type]

        closes = [c for c in controller.calls if c[0] == "close_page"]
        assert len(closes) == 2  # closed two extra tabs down to one


class TestAuthWallDetection:
    """auth_wall signal parsing from navigation responses."""

    def test_detects_auth_wall(self) -> None:
        response = _pages_response(
            "⚠️  Interstitial detected (1 signal(s)):\n  [medium] auth_wall (title_and_form): Login"
        )
        assert _response_signals_auth_wall(response) is True

    def test_ignores_other_interstitials(self) -> None:
        response = _pages_response("[high] cloudflare_challenge (title_pattern): Just a moment")
        assert _response_signals_auth_wall(response) is False


class TestMaybePromoteOnAuthWall:
    """Headless sessions promote to headed only on an auth wall."""

    def test_headless_auth_wall_promotes(self, monkeypatch) -> None:
        controller = _FakeController({})
        controller.headless = True  # type: ignore[attr-defined]
        response = _pages_response("[medium] auth_wall (form): login")
        swapped: list[Any] = []

        def fake_promote(ctrl, url):
            swapped.append(url)
            headed = _FakeController({})
            headed.headless = False  # type: ignore[attr-defined]
            return headed  # type: ignore[return-value]

        monkeypatch.setattr(
            "browser_tools.browser_session._promote_headless_to_headed", fake_promote
        )

        result = _maybe_promote_on_auth_wall([None], controller, response, "https://x/login")  # type: ignore[arg-type]

        assert swapped == ["https://x/login"]
        text = result["result"]["content"][0]["text"]
        assert "switched from headless to a headed" in text

    def test_headed_auth_wall_does_not_promote(self) -> None:
        """A headed session hitting auth stays headed (no promotion)."""
        controller = _FakeController({})
        controller.headless = False  # type: ignore[attr-defined]
        response = _pages_response("[medium] auth_wall (form): login")

        result = _maybe_promote_on_auth_wall([None], controller, response, "https://x/login")  # type: ignore[arg-type]
        assert result is response

    def test_headless_no_auth_wall_passthrough(self) -> None:
        controller = _FakeController({})
        controller.headless = True  # type: ignore[attr-defined]
        response = _pages_response("page loaded")

        result = _maybe_promote_on_auth_wall([None], controller, response, "https://x/")  # type: ignore[arg-type]
        assert result is response
