"""Pytest configuration and fixtures for browser-tools tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from browser_tools import lifecycle


#: Set for the whole session so no test can launch the developer's own browser.
#: Auto-detection finds ``/Applications/Google Chrome.app``, and executing an
#: application bundle's inner binary directly aborts in macOS application
#: registration under repeated launches. A suite that does it kills their Chrome
#: over and over and raises a system crash dialog each time. A headless shell is
#: a plain binary, registers as nothing, and never aborts.
def _plain_chrome_binary() -> str | None:
    """A Chrome binary that is not an application bundle, or None."""
    roots: list[Path] = []
    named = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if named:
        roots.append(Path(named))
    roots.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    roots.append(Path.home() / ".cache" / "ms-playwright")
    for root in roots:
        if not root.is_dir():
            continue
        found = sorted(root.glob("chromium_headless_shell-*/*/chrome-headless-shell"))
        for candidate in reversed(found):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def pytest_configure(config):
    """Pin the Chrome binary before any fixture, including module-scoped ones.

    This runs once per session rather than as an autouse fixture, because
    ``curated_browser`` is module-scoped and would launch before a
    function-scoped fixture could set anything. An operator who has already
    chosen a binary keeps it.
    """
    if os.environ.get(lifecycle.CHROME_BINARY_ENV_VAR):
        return
    binary = _plain_chrome_binary()
    if binary is not None:
        os.environ[lifecycle.CHROME_BINARY_ENV_VAR] = binary


def pytest_collection_modifyitems(config, items):
    """Auto-configure asyncio mode for all async tests."""
    for item in items:
        if item.get_closest_marker("asyncio") is not None:
            item.add_marker(pytest.mark.asyncio(loop_scope="function"))


@pytest.fixture(autouse=True)
def isolated_profile_roots(tmp_path_factory, monkeypatch):
    """Point both profile roots at empty temp dirs for every test.

    The legacy root defaults to ``/tmp/browser-tools-profiles``, which on a
    developer machine holds real logged-in profiles. Code under test reads,
    migrates and deletes profile directories, so no test may be one typo away
    from the real ones. A test that wants either root sets it itself; this
    fixture only guarantees the default is never the machine's own.
    """
    root = tmp_path_factory.mktemp("profile-roots")
    monkeypatch.setenv(lifecycle.PROFILES_ENV_VAR, str(root / "profiles"))
    monkeypatch.setenv(lifecycle.LEGACY_PROFILES_ENV_VAR, str(root / "legacy"))


@pytest.fixture
def sample_mcp_response():
    """Sample MCP tool response."""
    return {"content": [{"type": "text", "text": "Operation completed successfully"}]}


@pytest.fixture
def sample_error_response():
    """Sample MCP error response."""
    return {"error": {"code": -32600, "message": "Element not found"}}


@pytest.fixture(scope="module")
def curated_browser(tmp_path_factory):
    """An isolated Chrome for the RFC-05 acceptance tests; never the user's tabs."""
    if not os.environ.get(lifecycle.CHROME_BINARY_ENV_VAR):
        pytest.skip(
            "no Chrome outside an application bundle was found, and launching "
            "the one in /Applications crashes the developer's browser; run "
            "`python -m playwright install chromium`"
        )
    root = tmp_path_factory.mktemp("curated-browser")
    registry = str(root / "registry.json")
    try:
        # ``about:blank`` because a headless shell starts with no tab at all,
        # where the full browser opens one. Without it the launch succeeds and
        # every verb then fails with "Browser is running but has no open pages".
        instance = lifecycle.launch(
            headless=True, registry_path=registry, browser_args=["about:blank"]
        )
    except lifecycle.LifecycleError as exc:
        pytest.skip(f"cannot launch isolated Chrome: {exc}")
    try:
        yield f"http://127.0.0.1:{instance.port}"
    finally:
        lifecycle.stop(instance=instance.name, registry_path=registry)
