"""Tests for the new session-lifecycle behavior.

Covers:
- project identity (git-root resolution + harness-agnostic env chain)
- collapsed per-project keying (subdir drift no longer fragments)
- single-tab new_page (no accumulation)
- close_browser (quit owned / detach external)
- headless -> headed promotion on an auth wall
"""

from __future__ import annotations

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
