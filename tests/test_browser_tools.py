"""Tests for MCP command building, response formatting, and content extraction."""

from __future__ import annotations

from browser_tools.chrome_config import get_mcp_command
from browser_tools.chrome_utils import extract_content, format_response


class TestGetMcpCommand:
    """Tests for MCP command building."""

    def test_default_command(self):
        """Default command uses canary channel."""
        cmd = get_mcp_command()

        assert "npx" in cmd
        assert "chrome-devtools-mcp@latest" in cmd
        assert "--channel" in cmd
        assert "canary" in cmd

    def test_headless_flag(self):
        """Headless flag is added."""
        cmd = get_mcp_command(headless=True)

        assert "--headless" in cmd

    def test_isolated_flag(self):
        """Isolated flag is added."""
        cmd = get_mcp_command(isolated=True)

        assert "--isolated" in cmd

    def test_viewport_option(self):
        """Viewport option is added."""
        cmd = get_mcp_command(viewport="1920x1080")

        assert "--viewport" in cmd
        assert "1920x1080" in cmd

    def test_browser_url_overrides(self):
        """Browser URL overrides other options."""
        cmd = get_mcp_command(headless=True, isolated=True, browser_url="http://localhost:9222")

        assert "--browserUrl" in cmd
        assert "http://localhost:9222" in cmd
        assert "--headless" not in cmd
        assert "--isolated" not in cmd


class TestExtractContent:
    """Tests for content extraction."""

    def test_text_content(self, sample_mcp_response):
        """Extracts text from content."""
        result = extract_content(sample_mcp_response)

        assert "Operation completed" in result

    def test_no_content(self):
        """Missing content returns JSON."""
        result = extract_content({"data": "test"})

        assert "data" in result

    def test_string_content(self):
        """String content returns directly."""
        result = extract_content({"content": "direct string"})

        assert result == "direct string"

    def test_json_rpc_result_content(self):
        """JSON-RPC wrapped content is extracted for persistent sessions."""
        result = extract_content({"result": {"content": [{"type": "text", "text": "wrapped"}]}})

        assert result == "wrapped"


class TestFormatResponse:
    """Tests for response formatting."""

    def test_text_format(self, sample_mcp_response):
        """Text format extracts content."""
        result = format_response(sample_mcp_response, "text")

        assert "Operation completed" in result

    def test_json_format(self, sample_mcp_response):
        """JSON format returns compact JSON."""
        result = format_response(sample_mcp_response, "json")

        assert '"content"' in result
        assert "\n" not in result

    def test_pretty_format(self, sample_mcp_response):
        """Pretty format returns indented JSON."""
        result = format_response(sample_mcp_response, "pretty")

        assert "\n" in result
