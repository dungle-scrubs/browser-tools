"""Automation-backend seam: run one automation tool against the active browser.

Two adapters at one interface - Chrome (via PersistentChromeController) and
Camoufox (via CamoufoxSession). CamoufoxBackend owns the browser-tools to
Camoufox tool-name mapping, argument translation, and result wrapping: the
translation that previously lived as loose free functions in browser_session.

browser_session.call_tool invokes the two adapters at different points because
routing order is load-bearing. Camoufox automation tools must be resolved
before the session-management tools and the live-profile-conflict gate, while
Chrome's navigation hooks (single-tab reuse, headless to headed auth-wall
promotion) run after them. The Protocol still pins the shared invoke(tool,
args) shape both adapters satisfy, and a future backend (for example remote
CDP) drops in at the same seam.

Lifecycle and session tools (launch_camoufox, attach_browser,
use_browser_session, ...) are NOT automation tools and stay routed in
browser_session.call_tool.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

from .mcp_response import error_response, text_response

if TYPE_CHECKING:
    from .camoufox_session import CamoufoxSession
    from .persistent_browser import PersistentChromeController

# browser-tools automation tool -> Camoufox tool (None = explicitly unsupported).
# The routing truth for "is this a Camoufox-handled automation tool?"; consulted
# by browser_session.call_tool and by CamoufoxBackend.invoke.
CAMOUFOX_TOOL_MAP: dict[str, str | None] = {
    "navigate_page": "navigate",
    "new_page": "navigate",  # Camoufox uses a single page; navigate covers this
    "take_snapshot": "snapshot",
    "take_screenshot": "screenshot",
    "click": "click",
    "fill": "fill",
    "type_text": "fill",  # alias
    "evaluate_script": "evaluate",
    "wait_for": None,  # not mapped - use wait_for_human instead
}


class AutomationBackend(Protocol):
    """Run one automation tool against a browser backend.

    Callers (browser_session.call_tool) and tests cross this seam.
    """

    def invoke(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute ``tool`` with ``args``; return an MCP-shaped response."""
        ...


class ChromeBackend:
    """Chrome automation via a PersistentChromeController.

    Thin on purpose: the controller already speaks MCP-shaped responses, so
    invoke is a direct delegation. Present so the seam has two real adapters
    (Chrome + Camoufox), not one; a future backend satisfies the same shape.
    """

    def __init__(self, controller: PersistentChromeController) -> None:
        self._controller = controller

    def invoke(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return self._controller.invoke_tool(tool, args)


def _translate_args(chrome_tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Translate browser-tools argument names to Camoufox argument names."""
    if chrome_tool in ("navigate_page", "new_page"):
        return {"url": args.get("url", ""), "wait_until": args.get("wait_until", "load")}
    if chrome_tool == "take_screenshot":
        return {
            "path": args.get("filePath", args.get("path", "")),
            "full_page": args.get("fullPage", False),
        }
    if chrome_tool == "click":
        return {"selector": args.get("uid", "")}
    if chrome_tool in ("fill", "type_text"):
        return {"selector": args.get("uid", ""), "value": args.get("value", "")}
    if chrome_tool == "evaluate_script":
        return {"script": args.get("function", "")}
    return args


def _wrap_result(result: dict[str, Any]) -> dict[str, Any]:
    """Wrap a CamoufoxSession.call_tool result in MCP JSON-RPC format."""
    if "error" in result:
        return error_response(f"Error: {result['error']}")
    text = json.dumps(result.get("result", result), indent=2)
    return text_response(text)


class CamoufoxBackend:
    """Camoufox automation via a CamoufoxSession.

    Owns the tool-name mapping, argument translation, and result wrapping.
    Tools in :data:`CAMOUFOX_TOOL_MAP` take the mapped path (a None value means
    explicitly unsupported); Camoufox-native tools (wait_for_human, get_cookies)
    pass through unmapped.
    """

    def __init__(self, session: CamoufoxSession) -> None:
        self._session = session

    def invoke(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool in CAMOUFOX_TOOL_MAP:
            mapped = CAMOUFOX_TOOL_MAP[tool]
            if mapped is None:
                return error_response(
                    f"Error: Tool '{tool}' is not supported in Camoufox mode. "
                    "Use the camoufox-specific equivalent."
                )
            return _wrap_result(self._session.call_tool(mapped, _translate_args(tool, args)))
        # Camoufox-native tool (e.g. wait_for_human, get_cookies): no mapping.
        return _wrap_result(self._session.call_tool(tool, args))


__all__ = ["CAMOUFOX_TOOL_MAP", "AutomationBackend", "CamoufoxBackend", "ChromeBackend"]
