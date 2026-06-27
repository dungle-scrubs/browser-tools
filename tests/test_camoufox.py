"""Unit tests for CamoufoxSession — mocked, no real browser."""

from __future__ import annotations

import json


class TestLaunchBrowser:
    """Browser launch lifecycle."""

    def test_launch_browser_starts_browser_and_returns_session_info(
        self, camoufox_session, mock_camoufox_playwright
    ):
        """launch_browser creates a Camoufox browser and reports it is running."""
        result = camoufox_session.call_tool("launch_browser", {})

        assert "error" not in result
        assert result["result"]["status"] == "running"
        assert "fingerprint" in result["result"]
        mock_camoufox_playwright["cls"].assert_called_once()

    def test_launch_browser_passes_proxy_config(self, camoufox_session, mock_camoufox_playwright):
        """Proxy settings are forwarded to Camoufox for geo-aware fingerprinting."""
        proxy = {"server": "http://proxy:8080", "username": "user", "password": "pass"}
        result = camoufox_session.call_tool("launch_browser", {"proxy": proxy})

        assert "error" not in result
        call_kwargs = mock_camoufox_playwright["cls"].call_args
        assert call_kwargs.kwargs["proxy"] == proxy

    def test_launch_browser_twice_returns_already_running(self, launched_camoufox_session):
        """Calling launch_browser when already running returns status without error."""
        result = launched_camoufox_session.call_tool("launch_browser", {})

        assert "error" not in result
        assert result["result"]["status"] == "already_running"


class TestNavigate:
    """Page navigation."""

    def test_navigate_returns_page_title_and_url(
        self, launched_camoufox_session, mock_camoufox_playwright
    ):
        """navigate goes to a URL and returns the page title and current URL."""
        result = launched_camoufox_session.call_tool("navigate", {"url": "https://example.com"})

        assert "error" not in result
        assert result["result"]["title"] == "Example Domain"
        assert result["result"]["url"] == "https://example.com"
        mock_camoufox_playwright["page"].goto.assert_called_once()

    def test_navigate_detects_cloudflare_challenge(
        self, launched_camoufox_session, mock_camoufox_playwright
    ):
        """navigate flags Cloudflare challenge pages in the response."""
        mock_camoufox_playwright["page"].title.return_value = "Just a moment..."
        mock_camoufox_playwright[
            "page"
        ].content.return_value = (
            '<html><body><div id="cf-wrapper">Checking your browser</div></body></html>'
        )

        result = launched_camoufox_session.call_tool(
            "navigate", {"url": "https://protected.example.com"}
        )

        assert "error" not in result
        assert result["result"]["interstitial"]["detected"] is True
        assert result["result"]["interstitial"]["type"] == "cloudflare_challenge"

    def test_navigate_without_browser_returns_error(self, camoufox_session):
        """navigate before launch_browser returns a clear error."""
        result = camoufox_session.call_tool("navigate", {"url": "https://example.com"})

        assert "error" in result
        assert "launch_browser" in result["error"].lower()


class TestSnapshot:
    """Accessibility tree snapshots."""

    def test_snapshot_returns_accessibility_tree(
        self, launched_camoufox_session, mock_camoufox_playwright
    ):
        """snapshot returns a formatted accessibility tree from the page."""
        result = launched_camoufox_session.call_tool("snapshot", {})

        assert "error" not in result
        assert "tree" in result["result"]
        assert "Example Domain" in str(result["result"]["tree"])


class TestScreenshot:
    """Page screenshots."""

    def test_screenshot_saves_file_and_returns_path(
        self, launched_camoufox_session, mock_camoufox_playwright, tmp_path
    ):
        """screenshot captures page and returns the saved file path."""
        out = str(tmp_path / "test_shot.png")
        result = launched_camoufox_session.call_tool("screenshot", {"path": out})

        assert "error" not in result
        assert result["result"]["path"] == out
        mock_camoufox_playwright["page"].screenshot.assert_called_once()


class TestClick:
    """Element interaction — click."""

    def test_click_delegates_to_page(self, launched_camoufox_session, mock_camoufox_playwright):
        """click calls page.click with the given CSS selector."""
        result = launched_camoufox_session.call_tool("click", {"selector": "button.submit"})

        assert "error" not in result
        mock_camoufox_playwright["page"].click.assert_called_once_with("button.submit")


class TestFill:
    """Element interaction — fill."""

    def test_fill_types_text_into_field(self, launched_camoufox_session, mock_camoufox_playwright):
        """fill calls page.fill with selector and value."""
        result = launched_camoufox_session.call_tool(
            "fill", {"selector": "#email", "value": "test@test.com"}
        )

        assert "error" not in result
        mock_camoufox_playwright["page"].fill.assert_called_once_with("#email", "test@test.com")


class TestEvaluate:
    """JavaScript evaluation."""

    def test_evaluate_runs_js_and_returns_result(
        self, launched_camoufox_session, mock_camoufox_playwright
    ):
        """evaluate executes JS in page context and returns the value."""
        mock_camoufox_playwright["page"].evaluate.return_value = {"key": "value"}
        result = launched_camoufox_session.call_tool(
            "evaluate", {"script": "() => ({key: 'value'})"}
        )

        assert "error" not in result
        assert result["result"]["value"] == {"key": "value"}


class TestWaitForHuman:
    """Human-in-the-loop CAPTCHA flow."""

    def test_wait_for_human_polls_until_selector_disappears(
        self, launched_camoufox_session, mock_camoufox_playwright
    ):
        """wait_for_human returns success when the challenge element is gone."""
        from unittest.mock import MagicMock

        mock_camoufox_playwright["page"].query_selector.side_effect = [MagicMock(), None]

        result = launched_camoufox_session.call_tool(
            "wait_for_human",
            {"reason": "CAPTCHA", "check_selector_gone": "#challenge", "poll_interval": 0.01},
        )

        assert "error" not in result
        assert result["result"]["resolved"] is True

    def test_wait_for_human_times_out(self, launched_camoufox_session, mock_camoufox_playwright):
        """wait_for_human returns timeout when deadline expires."""
        from unittest.mock import MagicMock

        mock_camoufox_playwright["page"].query_selector.return_value = MagicMock()

        result = launched_camoufox_session.call_tool(
            "wait_for_human",
            {
                "reason": "CAPTCHA",
                "check_selector_gone": "#challenge",
                "timeout": 0.05,
                "poll_interval": 0.01,
            },
        )

        assert "error" not in result
        assert result["result"]["resolved"] is False
        assert "timeout" in result["result"]["reason"].lower()

    def test_wait_for_human_polls_until_url_matches(
        self, launched_camoufox_session, mock_camoufox_playwright
    ):
        """wait_for_human returns success when URL contains target string."""
        _url_iterator = iter(["https://example.com/challenge", "https://example.com/dashboard"])
        type(mock_camoufox_playwright["page"]).url = property(
            lambda self, _urls=_url_iterator: next(_urls)
        )

        result = launched_camoufox_session.call_tool(
            "wait_for_human",
            {"reason": "Login", "check_url_contains": "/dashboard", "poll_interval": 0.01},
        )

        assert "error" not in result
        assert result["result"]["resolved"] is True


class TestGetCookies:
    """Cookie extraction."""

    def test_get_cookies_returns_cookies_from_context(
        self, launched_camoufox_session, mock_camoufox_playwright
    ):
        """get_cookies returns cookies from the browser context."""
        result = launched_camoufox_session.call_tool("get_cookies", {})

        assert "error" not in result
        cookies = result["result"]["cookies"]
        assert len(cookies) == 1
        assert cookies[0]["name"] == "session"


class TestCloseBrowser:
    """Browser lifecycle — close."""

    def test_close_browser_cleans_up(self, launched_camoufox_session):
        """close_browser shuts down browser and clears internal state."""
        result = launched_camoufox_session.call_tool("close_browser", {})

        assert "error" not in result
        assert result["result"]["status"] == "closed"

        result2 = launched_camoufox_session.call_tool("navigate", {"url": "https://example.com"})
        assert "error" in result2


class TestUnknownTool:
    """Error handling."""

    def test_unknown_tool_returns_error(self, camoufox_session):
        """Calling a nonexistent tool returns an error, not an exception."""
        result = camoufox_session.call_tool("nonexistent_tool", {})

        assert "error" in result
        assert "Unknown tool" in result["error"]


class TestToolProxyProtocol:
    """Tool-proxy JSON integration."""

    def test_result_is_json_serializable(self, mock_camoufox_playwright):
        """All results must be JSON-serializable for tool-proxy protocol."""
        from browser_tools.camoufox_session import CamoufoxSession

        session = CamoufoxSession()
        result = session.call_tool("launch_browser", {})

        assert result["result"]["status"] == "running"
        json.dumps(result)
