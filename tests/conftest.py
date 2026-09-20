"""Pytest configuration and fixtures for browser-tools tests."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items):
    """Auto-configure asyncio mode for all async tests."""
    for item in items:
        if item.get_closest_marker("asyncio") is not None:
            item.add_marker(pytest.mark.asyncio(loop_scope="function"))


@pytest.fixture
def sample_mcp_response():
    """Sample MCP tool response."""
    return {"content": [{"type": "text", "text": "Operation completed successfully"}]}


@pytest.fixture
def sample_error_response():
    """Sample MCP error response."""
    return {"error": {"code": -32600, "message": "Element not found"}}
