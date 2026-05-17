"""E2E tests — launches real Camoufox browser. Requires `camoufox fetch`.

Run with: pytest tests/test_e2e_camoufox.py -v
Skip with: pytest tests/ --ignore=tests/test_e2e_camoufox.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    from camoufox.sync_api import Camoufox  # noqa: F401

    CAMOUFOX_AVAILABLE = True
except ImportError:
    CAMOUFOX_AVAILABLE = False

pytestmark = pytest.mark.skipif(not CAMOUFOX_AVAILABLE, reason="camoufox not installed")


@pytest.fixture(scope="module")
def e2e_session():
    """Create a real CamoufoxSession with a live browser for the test module."""
    from browser_tools.camoufox_session import CamoufoxSession

    session = CamoufoxSession()
    result = session.call_tool("launch_browser", {"headless": True})
    assert result["result"]["status"] == "running"
    yield session
    session.call_tool("close_browser", {})


class TestE2ESmoke:
    """Smoke tests against a live Camoufox instance."""

    def test_navigate_to_example_com(self, e2e_session):
        """Can navigate to example.com and get the real page title."""
        result = e2e_session.call_tool("navigate", {"url": "https://example.com"})

        assert "error" not in result
        assert "Example Domain" in result["result"]["title"]
        assert result["result"]["interstitial"]["detected"] is False

    def test_screenshot_returns_valid_file(self, e2e_session, tmp_path):
        """screenshot saves a real PNG file to disk."""
        out = str(tmp_path / "e2e_shot.png")
        result = e2e_session.call_tool("screenshot", {"path": out})

        assert "error" not in result
        assert Path(out).exists()
        assert Path(out).stat().st_size > 100

    def test_snapshot_returns_tree(self, e2e_session):
        """snapshot returns a real accessibility tree with content."""
        result = e2e_session.call_tool("snapshot", {})

        assert "error" not in result
        tree = result["result"]["tree"]
        assert tree is not None
        assert "Example Domain" in str(tree)

    def test_evaluate_returns_js_result(self, e2e_session):
        """evaluate runs real JS and returns the result."""
        result = e2e_session.call_tool("evaluate", {"script": "document.title"})

        assert "error" not in result
        assert "Example Domain" in str(result["result"]["value"])

    def test_get_cookies_returns_list(self, e2e_session):
        """get_cookies returns a list (may be empty for example.com)."""
        result = e2e_session.call_tool("get_cookies", {})

        assert "error" not in result
        assert isinstance(result["result"]["cookies"], list)


class TestE2EFingerprint:
    """Verify Camoufox fingerprint injection is working."""

    def test_navigator_webdriver_is_not_true(self, e2e_session):
        """navigator.webdriver should not reveal automation."""
        result = e2e_session.call_tool("evaluate", {"script": "navigator.webdriver"})

        assert "error" not in result
        assert result["result"]["value"] is not True

    def test_user_agent_is_not_empty(self, e2e_session):
        """User agent should be a realistic browser string."""
        result = e2e_session.call_tool("evaluate", {"script": "navigator.userAgent"})

        assert "error" not in result
        ua = result["result"]["value"]
        assert isinstance(ua, str)
        assert len(ua) > 20
        assert "Firefox" in ua or "Gecko" in ua

    def test_webgl_renderer_is_not_automation_default(self, e2e_session):
        """WebGL renderer should not be the default automation string if available."""
        script = """
        (() => {
            const canvas = document.createElement('canvas');
            const gl = canvas.getContext('webgl');
            if (!gl) return 'no-webgl';
            const ext = gl.getExtension('WEBGL_debug_renderer_info');
            if (!ext) return 'no-ext';
            return gl.getParameter(ext.UNMASKED_RENDERER_WEBGL);
        })()
        """
        result = e2e_session.call_tool("evaluate", {"script": script})

        assert "error" not in result
        renderer = result["result"]["value"]
        # In headless mode WebGL may not be available — that's OK
        if renderer not in (None, "no-webgl", "no-ext"):
            # If WebGL IS available, it should not be the automation default
            assert "SwiftShader" not in renderer
