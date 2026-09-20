"""Single source of truth for tool routing.

``CDP_TOOLS`` -- the tools the CDP handler owns -- is *derived* from the
``TOOLS`` table here rather than written out a second time. Before this module
that set lived independently in ``cdp_constants`` and in the persistent
controller, and the copies drifted.

The table used to carry seven more flags, classifying tools for the MCP front:
which were refused or warned in inspect mode, which needed a pre-snapshot,
which selected the active tab, which took the screenshot gate, which the
session adapter kept to one tab. The front is gone (#92), and with it every
consumer of those flags, so the table keeps the one flag that still routes
anything.

A tool absent from ``TOOLS`` is not CDP-routed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolFlags:
    """Routing flags for a single tool.

    All flags default to False; declare only the True ones per tool.
    """

    cdp: bool = False


# name -> flags. Only tools with at least one True flag need to be listed.
TOOLS: dict[str, ToolFlags] = {
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


# Derived routing set. Define it once; do not hand-maintain.
CDP_TOOLS = _names("cdp")


__all__ = [
    "CDP_TOOLS",
    "TOOLS",
    "ToolFlags",
]
