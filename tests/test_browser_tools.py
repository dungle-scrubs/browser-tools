"""Tests for Browser Tools session.

Tests configuration, utilities, and snapshot parsing.
"""

import pytest

from browser_tools.chrome_config import (
    TOOL_CATEGORIES,
    get_all_tools,
    get_mcp_command,
    get_tool_category,
    get_tool_description,
    validate_tool_params,
)
from browser_tools.chrome_utils import (
    ParameterValidationError,
    extract_content,
    find_all_element_uids,
    find_button,
    find_element_uid,
    find_link,
    format_response,
    format_snapshot_summary,
    get_element_info,
    list_elements_by_role,
    parse_json_param,
    parse_snapshot,
    parse_viewport,
)


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


class TestGetAllTools:
    """Tests for tool listing."""

    def test_returns_all_tools(self):
        """Returns all tools from all categories."""
        tools = get_all_tools()

        assert len(tools) > 0
        assert "click" in tools
        assert "navigate_page" in tools

    def test_includes_navigation_tools(self):
        """Includes navigation tools."""
        tools = get_all_tools()

        for tool in TOOL_CATEGORIES["navigation"]:
            assert tool in tools

    def test_includes_input_tools(self):
        """Includes input tools."""
        tools = get_all_tools()

        for tool in TOOL_CATEGORIES["input"]:
            assert tool in tools


class TestGetToolCategory:
    """Tests for tool category lookup."""

    def test_navigation_tool(self):
        """Navigation tool returns navigation category."""
        category = get_tool_category("navigate_page")

        assert category == "navigation"

    def test_input_tool(self):
        """Input tool returns input category."""
        category = get_tool_category("click")

        assert category == "input"

    def test_unknown_tool(self):
        """Unknown tool returns None."""
        category = get_tool_category("unknown_tool")

        assert category is None


class TestValidateToolParams:
    """Tests for parameter validation."""

    def test_valid_params(self):
        """Valid parameters pass validation."""
        valid, error = validate_tool_params("click", {"uid": "123"})

        assert valid is True
        assert error is None

    def test_missing_required_param(self):
        """Missing required parameter fails."""
        valid, error = validate_tool_params("click", {})

        assert valid is False
        assert "uid" in error

    def test_unknown_tool(self):
        """Unknown tool fails."""
        valid, error = validate_tool_params("unknown_tool", {})

        assert valid is False
        assert "Unknown tool" in error

    def test_wrong_type(self):
        """Wrong parameter type fails."""
        valid, error = validate_tool_params("resize_page", {"width": "not_int", "height": 720})

        assert valid is False
        assert "Invalid type" in error


class TestGetToolDescription:
    """Tests for tool descriptions."""

    def test_known_tool(self):
        """Known tool returns description."""
        desc = get_tool_description("click")

        assert "Click" in desc

    def test_unknown_tool(self):
        """Unknown tool returns fallback."""
        desc = get_tool_description("unknown_tool")

        assert "No description available" in desc


class TestParseViewport:
    """Tests for viewport parsing."""

    def test_valid_viewport(self):
        """Valid viewport parses correctly."""
        width, height = parse_viewport("1280x720")

        assert width == 1280
        assert height == 720

    def test_uppercase_x(self):
        """Uppercase X works."""
        width, height = parse_viewport("1920X1080")

        assert width == 1920
        assert height == 1080

    def test_invalid_format(self):
        """Invalid format raises error."""
        with pytest.raises(ParameterValidationError):
            parse_viewport("invalid")

    def test_negative_values(self):
        """Negative values raise error."""
        with pytest.raises(ParameterValidationError):
            parse_viewport("-1280x720")


class TestParseJsonParam:
    """Tests for JSON parameter parsing."""

    def test_valid_json(self):
        """Valid JSON parses correctly."""
        result = parse_json_param('{"key": "value"}', "test")

        assert result == {"key": "value"}

    def test_invalid_json(self):
        """Invalid JSON raises error."""
        with pytest.raises(ParameterValidationError):
            parse_json_param("not json", "test")


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


class TestParseSnapshot:
    """Tests for snapshot parsing."""

    def test_parses_elements(self, sample_snapshot):
        """Parses elements from snapshot."""
        elements = parse_snapshot(sample_snapshot)

        assert len(elements) > 0

    def test_extracts_uid(self, sample_snapshot):
        """Extracts element UIDs."""
        elements = parse_snapshot(sample_snapshot)

        uids = [e["uid"] for e in elements]
        assert "456" in uids

    def test_extracts_role(self, sample_snapshot):
        """Extracts element roles."""
        elements = parse_snapshot(sample_snapshot)

        roles = [e["role"] for e in elements]
        assert "button" in roles

    def test_extracts_name(self, sample_snapshot):
        """Extracts element names."""
        elements = parse_snapshot(sample_snapshot)

        button = next(e for e in elements if e["role"] == "button")
        assert button["name"] == "Submit Form"

    def test_handles_empty_snapshot(self):
        """Empty snapshot returns empty list."""
        elements = parse_snapshot("")

        assert elements == []


class TestFindElementUid:
    """Tests for element UID finding."""

    def test_find_by_text(self, sample_snapshot):
        """Finds element by text."""
        uid = find_element_uid(sample_snapshot, text="Submit")

        assert uid == "456"

    def test_find_by_role(self, sample_snapshot):
        """Finds element by role."""
        uid = find_element_uid(sample_snapshot, role="link")

        assert uid == "101"

    def test_find_by_name(self, sample_snapshot):
        """Finds element by exact name."""
        uid = find_element_uid(sample_snapshot, name="Email")

        assert uid == "789"

    def test_case_insensitive(self, sample_snapshot):
        """Search is case insensitive by default."""
        uid = find_element_uid(sample_snapshot, text="submit")

        assert uid == "456"

    def test_not_found(self, sample_snapshot):
        """Returns None when not found."""
        uid = find_element_uid(sample_snapshot, text="nonexistent")

        assert uid is None


class TestFindAllElementUids:
    """Tests for finding multiple elements."""

    def test_find_all_by_role(self, sample_snapshot):
        """Finds all elements by role."""
        uids = find_all_element_uids(sample_snapshot, role="button")

        assert "456" in uids

    def test_returns_empty_when_not_found(self, sample_snapshot):
        """Returns empty list when none found."""
        uids = find_all_element_uids(sample_snapshot, role="nonexistent")

        assert uids == []


class TestConvenienceFunctions:
    """Tests for convenience find functions."""

    def test_find_button(self, sample_snapshot):
        """find_button finds buttons."""
        uid = find_button(sample_snapshot, "Submit")

        assert uid == "456"

    def test_find_link(self, sample_snapshot):
        """find_link finds links."""
        uid = find_link(sample_snapshot, "Learn More")

        assert uid == "101"


class TestGetElementInfo:
    """Tests for element info retrieval."""

    def test_returns_element_info(self, sample_snapshot):
        """Returns element info by UID."""
        info = get_element_info(sample_snapshot, "456")

        assert info is not None
        assert info["role"] == "button"
        assert info["name"] == "Submit Form"

    def test_not_found(self, sample_snapshot):
        """Returns None when not found."""
        info = get_element_info(sample_snapshot, "999")

        assert info is None


class TestListElementsByRole:
    """Tests for listing elements by role."""

    def test_lists_buttons(self, sample_snapshot):
        """Lists all buttons."""
        buttons = list_elements_by_role(sample_snapshot, "button")

        assert len(buttons) == 1
        assert buttons[0]["name"] == "Submit Form"


class TestFormatSnapshotSummary:
    """Tests for snapshot summary formatting."""

    def test_formats_summary(self, sample_snapshot):
        """Formats readable summary."""
        summary = format_snapshot_summary(sample_snapshot)

        assert "Found" in summary
        assert "elements" in summary

    def test_limits_elements(self, sample_snapshot):
        """Limits displayed elements."""
        summary = format_snapshot_summary(sample_snapshot, max_elements=1)

        assert "more" in summary

    def test_empty_snapshot(self):
        """Handles empty snapshot."""
        summary = format_snapshot_summary("")

        assert "No elements found" in summary
