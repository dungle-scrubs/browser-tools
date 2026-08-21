# Vendored from chrome-agent v0.5.7 (https://github.com/captivus/chrome-agent).
# Copyright (c) 2026 Corey Gallon.
# SPDX-License-Identifier: MIT
# See /NOTICE for the full vendoring notice.
#
# This file is a verbatim vendored copy; the only permitted modification is
# rewriting intra-package imports to browser_tools.core. See RFC-01, section
# "Vendoring rules".

"""CDP EventBreakpoints domain.

EventBreakpoints permits setting JavaScript breakpoints on operations and events
occurring in native code invoked from JavaScript. Once breakpoint is hit, it is
reported through Debugger domain, similarly to regular breakpoints being hit.

Auto-generated from Chrome DevTools Protocol schema.
Do not edit manually. Re-run the generator to update.
"""

from __future__ import annotations

from typing import Any

from ..cdp_client import CDPClient


class EventBreakpoints:
    """EventBreakpoints permits setting JavaScript breakpoints on operations and events
occurring in native code invoked from JavaScript. Once breakpoint is hit, it is
reported through Debugger domain, similarly to regular breakpoints being hit."""

    def __init__(self, client: CDPClient):
        self._client = client

    async def set_instrumentation_breakpoint(self, event_name: str) -> dict:
        """Sets breakpoint on particular native event."""
        params: dict[str, Any] = {}
        params["eventName"] = event_name
        return await self._client.send(method="EventBreakpoints.setInstrumentationBreakpoint", params=params)

    async def remove_instrumentation_breakpoint(self, event_name: str) -> dict:
        """Removes breakpoint on particular native event."""
        params: dict[str, Any] = {}
        params["eventName"] = event_name
        return await self._client.send(method="EventBreakpoints.removeInstrumentationBreakpoint", params=params)

    async def disable(self) -> dict:
        """Removes all breakpoints"""
        return await self._client.send(method="EventBreakpoints.disable")
