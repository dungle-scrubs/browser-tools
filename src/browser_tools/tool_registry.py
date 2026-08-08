"""Single source of truth for tool routing and behavior flags.

Every frozenset that classifies a tool (which tools the CDP handler owns,
which are blocked in inspect mode, which need a pre-snapshot, which trigger
interstitial detection) is *derived* from the ``TOOLS`` table here. Before
this module those sets were defined independently in ``cdp_constants`` and
``persistent_browser``, which let two of them drift apart:
``INTERACTION_TOOLS`` (UID-based tools that need a snapshot first) silently
disagreed with ``INSPECT_BLOCKED_TOOLS`` (all page-mutating tools) because
they were maintained by hand in different files.

Flag meanings:

- ``cdp``: routed to the CDP handler (frame/ax/page-domain tools), not the
  chrome-devtools-mcp subprocess.
- ``local``: handled synchronously inside the daemon (no MCP or CDP round-trip).
- ``interaction``: references an element UID, so the controller takes a
  snapshot first so the UID is valid in the current session. A strict subset
  of mutating tools - ``handle_dialog`` mutates page state but takes no UID,
  so it is ``inspect_blocked`` but not ``interaction``.
- ``inspect_blocked``: refused in inspect (read-only) mode.
- ``navigation``: triggers post-call interstitial detection.
- ``inspect_warn``: allowed but warned in inspect mode.
- ``page_selecting``: chooses the active tab itself (``new_page``, ``select_page``),
  so the controller must skip the restore-before-call step that reselects the
  prior tab. A strict subset - the controller restores before most tools, so
  listing a tool here opts it out of that restore.
- ``screenshot_gate``: forwarded to the MCP subprocess like a default tool,
  but wrapped with a paint-ready gate and blank-frame retry so the captured
  image is not a mid-animation / mid-hydration frame (``take_screenshot``).

Tools absent from ``TOOLS`` are neither CDP- nor local-routed and fall through
to the default path: forwarded unchanged to the chrome-devtools-mcp subprocess.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolFlags:
    """Routing and behavior flags for a single tool.

    All flags default to False; declare only the True ones per tool.
    """

    cdp: bool = False
    local: bool = False
    interaction: bool = False
    inspect_blocked: bool = False
    navigation: bool = False
    inspect_warn: bool = False
    page_selecting: bool = False
    screenshot_gate: bool = False


# name -> flags. Only tools with at least one True flag need to be listed;
# everything else is a default chrome-devtools-mcp tool forwarded as-is.
TOOLS: dict[str, ToolFlags] = {
    # --- Local (daemon-synchronous) tools ---
    "attach_browser": ToolFlags(local=True),
    "list_profiles": ToolFlags(local=True),
    "delete_profile": ToolFlags(local=True),
    # --- Navigation (triggers interstitial detection) ---
    "navigate_page": ToolFlags(navigation=True, inspect_warn=True),
    "new_page": ToolFlags(navigation=True, inspect_warn=True, page_selecting=True),
    "close_page": ToolFlags(inspect_warn=True),
    # --- Page-selection tools: choose the active tab themselves, so they skip
    # the controller's restore-before-call step. select_page is a default-
    # forwarded chrome-devtools-mcp tool (no other flags); declaring it here
    # keeps PAGE_SELECTING_TOOLS complete. Adding it with only page_selecting
    # does not change routing, since CDP_TOOLS / LOCAL_TOOLS / etc. are derived
    # from the other flags.
    "select_page": ToolFlags(page_selecting=True),
    # --- UID-based interactions (need a pre-snapshot) ---
    "click": ToolFlags(interaction=True, inspect_blocked=True),
    "hover": ToolFlags(interaction=True, inspect_blocked=True),
    "fill": ToolFlags(interaction=True, inspect_blocked=True),
    "fill_form": ToolFlags(interaction=True, inspect_blocked=True),
    "drag": ToolFlags(interaction=True, inspect_blocked=True),
    "press_key": ToolFlags(interaction=True, inspect_blocked=True),
    "upload_file": ToolFlags(interaction=True, inspect_blocked=True),
    # --- Page-mutating but UID-less (blocked in inspect, no snapshot needed) ---
    "handle_dialog": ToolFlags(inspect_blocked=True),
    # Camoufox alias for ``fill``; declared so inspect-mode blocking stays
    # consistent even though it has no chrome-devtools-mcp schema entry.
    "type_text": ToolFlags(inspect_blocked=True),
    # --- Screenshot gate: forwarded to MCP but wrapped with a paint-ready
    # gate + blank-frame retry. take_screenshot is a default chrome-devtools-mcp
    # tool; declaring it here with only screenshot_gate does not change routing.
    "take_screenshot": ToolFlags(screenshot_gate=True),
    # --- CDP-routed tools (frame / accessibility / page / runtime domains) ---
    "list_frames": ToolFlags(cdp=True),
    "select_frame": ToolFlags(cdp=True),
    "reset_frame": ToolFlags(cdp=True),
    "get_frame_storage": ToolFlags(cdp=True),
    "get_frame_events": ToolFlags(cdp=True),
    "ax_find": ToolFlags(cdp=True),
    "ax_node": ToolFlags(cdp=True),
    "export_pdf": ToolFlags(cdp=True),
    "screenshot_element": ToolFlags(cdp=True),
    "screencast_start": ToolFlags(cdp=True),
    "screencast_stop": ToolFlags(cdp=True),
    "wait_idle": ToolFlags(cdp=True),
    "wait_stable": ToolFlags(cdp=True),
    "get_text": ToolFlags(cdp=True),
    "get_html": ToolFlags(cdp=True),
    "get_attr": ToolFlags(cdp=True),
    "element_exists": ToolFlags(cdp=True),
    "element_visible": ToolFlags(cdp=True),
}


def _names(flag: str) -> frozenset[str]:
    """Return the frozenset of tool names where ``flag`` is True."""
    return frozenset(name for name, flags in TOOLS.items() if getattr(flags, flag))


# Derived routing/behavior sets. Define these once; do not hand-maintain.
CDP_TOOLS = _names("cdp")
LOCAL_TOOLS = _names("local")
INTERACTION_TOOLS = _names("interaction")
INSPECT_BLOCKED_TOOLS = _names("inspect_blocked")
NAVIGATION_TOOLS = _names("navigation")
INSPECT_WARN_TOOLS = _names("inspect_warn")
PAGE_SELECTING_TOOLS = _names("page_selecting")
SCREENSHOT_GATE_TOOLS = _names("screenshot_gate")


__all__ = [
    "CDP_TOOLS",
    "INSPECT_BLOCKED_TOOLS",
    "INSPECT_WARN_TOOLS",
    "INTERACTION_TOOLS",
    "LOCAL_TOOLS",
    "NAVIGATION_TOOLS",
    "PAGE_SELECTING_TOOLS",
    "SCREENSHOT_GATE_TOOLS",
    "TOOLS",
    "ToolFlags",
]
