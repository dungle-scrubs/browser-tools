# Vendored from chrome-agent v0.5.7 (https://github.com/captivus/chrome-agent).
# Copyright (c) 2026 Corey Gallon.
# SPDX-License-Identifier: MIT
# See /NOTICE for the full vendoring notice.
#
# This file is a verbatim vendored copy; the only permitted modification is
# rewriting intra-package imports to browser_tools.core. See RFC-01, section
# "Vendoring rules".

"""CDP Tethering domain.

The Tethering domain defines methods and events for browser port binding.

Auto-generated from Chrome DevTools Protocol schema.
Do not edit manually. Re-run the generator to update.
"""

from __future__ import annotations

from typing import Any

from ..cdp_client import CDPClient


class Tethering:
    """The Tethering domain defines methods and events for browser port binding."""

    def __init__(self, client: CDPClient):
        self._client = client

    async def bind(self, port: int) -> dict:
        """Request browser port binding."""
        params: dict[str, Any] = {}
        params["port"] = port
        return await self._client.send(method="Tethering.bind", params=params)

    async def unbind(self, port: int) -> dict:
        """Request browser port unbinding."""
        params: dict[str, Any] = {}
        params["port"] = port
        return await self._client.send(method="Tethering.unbind", params=params)
