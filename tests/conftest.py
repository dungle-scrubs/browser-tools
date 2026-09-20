"""Pytest configuration and fixtures for browser-tools tests."""

from __future__ import annotations

import pytest

from browser_tools import lifecycle


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
