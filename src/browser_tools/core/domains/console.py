# Vendored from chrome-agent v0.5.7 (https://github.com/captivus/chrome-agent).
# Copyright (c) 2026 Corey Gallon.
# SPDX-License-Identifier: MIT
# See /NOTICE for the full vendoring notice.
#
# This file is a verbatim vendored copy; the only permitted modification is
# rewriting intra-package imports to browser_tools.core. See RFC-01, section
# "Vendoring rules".

"""CDP Console domain.

This domain is deprecated - use Runtime or Log instead.

Auto-generated from Chrome DevTools Protocol schema.
Do not edit manually. Re-run the generator to update.
"""

from __future__ import annotations

from typing import Any

from ..cdp_client import CDPClient


# Console message.
ConsoleMessage = dict  # Object type

class Console:
    """This domain is deprecated - use Runtime or Log instead."""

    def __init__(self, client: CDPClient):
        self._client = client

    async def clear_messages(self) -> dict:
        """Does nothing."""
        return await self._client.send(method="Console.clearMessages")

    async def disable(self) -> dict:
        """Disables console domain, prevents further console messages from being reported to the client."""
        return await self._client.send(method="Console.disable")

    async def enable(self) -> dict:
        """Enables console domain, sends the messages collected so far to the client by means of the
`messageAdded` notification.
        """
        return await self._client.send(method="Console.enable")
