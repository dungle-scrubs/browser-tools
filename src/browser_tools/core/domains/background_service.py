# Vendored from chrome-agent v0.5.7 (https://github.com/captivus/chrome-agent).
# Copyright (c) 2026 Corey Gallon.
# SPDX-License-Identifier: MIT
# See /NOTICE for the full vendoring notice.
#
# This file is a verbatim vendored copy; the only permitted modification is
# rewriting intra-package imports to browser_tools.core. See RFC-01, section
# "Vendoring rules".

"""CDP BackgroundService domain.

Defines events for background web platform features.

Auto-generated from Chrome DevTools Protocol schema.
Do not edit manually. Re-run the generator to update.
"""

from __future__ import annotations

from typing import Any

from ..cdp_client import CDPClient


# The Background Service that will be associated with the commands/events.
# Every Background Service operates independently, but they share the same
# API.
ServiceName = str  # Literal enum: "backgroundFetch", "backgroundSync", "pushMessaging", "notifications", "paymentHandler", "periodicBackgroundSync"

# A key-value pair for additional event information to pass along.
EventMetadata = dict  # Object type

BackgroundServiceEvent = dict  # Object type

class BackgroundService:
    """Defines events for background web platform features."""

    def __init__(self, client: CDPClient):
        self._client = client

    async def start_observing(self, service: ServiceName) -> dict:
        """Enables event updates for the service."""
        params: dict[str, Any] = {}
        params["service"] = service
        return await self._client.send(method="BackgroundService.startObserving", params=params)

    async def stop_observing(self, service: ServiceName) -> dict:
        """Disables event updates for the service."""
        params: dict[str, Any] = {}
        params["service"] = service
        return await self._client.send(method="BackgroundService.stopObserving", params=params)

    async def set_recording(self, should_record: bool, service: ServiceName) -> dict:
        """Set the recording state for the service."""
        params: dict[str, Any] = {}
        params["shouldRecord"] = should_record
        params["service"] = service
        return await self._client.send(method="BackgroundService.setRecording", params=params)

    async def clear_events(self, service: ServiceName) -> dict:
        """Clears all stored data for the service."""
        params: dict[str, Any] = {}
        params["service"] = service
        return await self._client.send(method="BackgroundService.clearEvents", params=params)
