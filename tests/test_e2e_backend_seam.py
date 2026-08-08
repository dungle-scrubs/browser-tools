"""E2E test for the AutomationBackend seam — launches a real Camoufox browser.

Drives browser_session.call_tool through the Chrome/Camoufox seam: launch via
the router, then navigate/screenshot routed through CamoufoxBackend (which maps
the tool names and translates the args), then close. The unit suite covers
CamoufoxBackend.invoke with a fake session; this file exercises the same path
against a live browser.

Isolated in its own module (rather than appended to test_e2e_camoufox.py)
because Camoufox's sync Playwright API cannot launch a second browser while
another module-scope session is live — each e2e module owns its own browser.

Requires `camoufox fetch`. Excluded from CI unit runs (see ci.yml).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

try:
    from camoufox.sync_api import Camoufox  # noqa: F401

    CAMOUFOX_AVAILABLE = True
except ImportError:
    CAMOUFOX_AVAILABLE = False

# Skipped in CI: this needs a locally-fetched Camoufox browser binary and real
# network, like test_e2e_camoufox (which ci.yml ignores outright). Guard on the
# CI env var so this file is safe even though it is not in the ignore list.
pytestmark = pytest.mark.skipif(
    not CAMOUFOX_AVAILABLE or os.environ.get("CI") == "true",
    reason="camoufox e2e: needs a locally-fetched browser; skipped in CI",
)


def test_router_routes_automation_through_camoufox_backend(tmp_path: Path) -> None:
    """launch/navigate/screenshot/close flow through call_tool and the seam."""
    from browser_tools.browser_session import create_tool_proxy_handlers
    from browser_tools.mcp_response import extract_text_items

    def _text(resp: dict) -> str:
        return "".join(extract_text_items(resp))

    # Camoufox-only tools never reach the Chrome path, so no controller is needed.
    _, call_tool = create_tool_proxy_handlers()

    # Launch via the router (exercises _handle_launch_camoufox).
    launch = call_tool(None, "launch_camoufox", {"headless": True})  # type: ignore[arg-type]
    assert "launched" in _text(launch).lower()

    # navigate_page routes through CamoufoxBackend, which maps it to "navigate"
    # and translates the args.
    nav = call_tool(None, "navigate_page", {"url": "https://example.com"})  # type: ignore[arg-type]
    assert "Example Domain" in _text(nav)

    # take_screenshot routes through CamoufoxBackend -> "screenshot", with
    # filePath/fullPage translated to path/full_page.
    out = tmp_path / "seam_shot.png"
    shot = call_tool(  # type: ignore[arg-type]
        None, "take_screenshot", {"filePath": str(out), "fullPage": False}
    )
    assert out.exists() and out.stat().st_size > 0
    assert "seam_shot" in _text(shot)

    # close via the router.
    close = call_tool(None, "close_camoufox", {})  # type: ignore[arg-type]
    assert "closed" in _text(close).lower()
