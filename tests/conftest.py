"""Pytest configuration and fixtures for browser-tools tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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


# ---------------------------------------------------------------------------
# Camoufox fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_camoufox_playwright():
    """Mock the camoufox sync API to avoid needing the real binary."""
    with patch("browser_tools.camoufox_session.Camoufox") as mock_cls:
        mock_page = MagicMock()
        mock_page.title.return_value = "Example Domain"
        mock_page.url = "https://example.com"
        mock_page.content.return_value = "<html><body>Hello</body></html>"
        mock_page.screenshot.return_value = b"\x89PNG\r\n\x1a\n"
        mock_page.evaluate.return_value = "test-result"
        mock_page.query_selector.return_value = MagicMock()

        mock_body_locator = MagicMock()
        mock_body_locator.aria_snapshot.return_value = '- heading "Example Domain" [level=1]'
        mock_page.locator.return_value = mock_body_locator

        mock_context = MagicMock()
        mock_context.new_page.return_value = mock_page
        mock_context.cookies.return_value = [
            {"name": "session", "value": "abc123", "domain": "example.com", "path": "/"},
        ]

        mock_browser = MagicMock()
        mock_browser.new_context.return_value = mock_context
        mock_browser.contexts = [mock_context]

        mock_instance = MagicMock()
        mock_instance.__enter__ = MagicMock(return_value=mock_browser)
        mock_instance.__exit__ = MagicMock(return_value=False)
        mock_cls.return_value = mock_instance

        yield {
            "cls": mock_cls,
            "browser": mock_browser,
            "context": mock_context,
            "page": mock_page,
        }


@pytest.fixture
def camoufox_session(mock_camoufox_playwright):
    """Create a CamoufoxSession with mocked browser."""
    from browser_tools.camoufox_session import CamoufoxSession

    return CamoufoxSession()


@pytest.fixture
def launched_camoufox_session(camoufox_session):
    """CamoufoxSession with browser already launched."""
    camoufox_session.call_tool("launch_browser", {})
    return camoufox_session
