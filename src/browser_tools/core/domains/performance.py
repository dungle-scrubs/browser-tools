# Vendored from chrome-agent v0.5.7 (https://github.com/captivus/chrome-agent).
# Copyright (c) 2026 Corey Gallon.
# SPDX-License-Identifier: MIT
# See /NOTICE for the full vendoring notice.
#
# This file is a verbatim vendored copy; the only permitted modification is
# rewriting intra-package imports to browser_tools.core. See RFC-01, section
# "Vendoring rules".

"""CDP Performance domain.

Auto-generated from Chrome DevTools Protocol schema.
Do not edit manually. Re-run the generator to update.
"""

from __future__ import annotations

from typing import Any

from ..cdp_client import CDPClient


# Run-time execution metric.
Metric = dict  # Object type

class Performance:
    """CDP Performance domain."""

    def __init__(self, client: CDPClient):
        self._client = client

    async def disable(self) -> dict:
        """Disable collecting and reporting metrics."""
        return await self._client.send(method="Performance.disable")

    async def enable(self, time_domain: str | None = None) -> dict:
        """Enable collecting and reporting metrics."""
        params: dict[str, Any] = {}
        if time_domain is not None:
            params["timeDomain"] = time_domain
        return await self._client.send(method="Performance.enable", params=params)

    async def set_time_domain(self, time_domain: str) -> dict:
        """Sets time domain to use for collecting and reporting duration metrics.
Note that this must be called before enabling metrics collection. Calling
this method while metrics collection is enabled returns an error.
        """
        params: dict[str, Any] = {}
        params["timeDomain"] = time_domain
        return await self._client.send(method="Performance.setTimeDomain", params=params)

    async def get_metrics(self) -> dict:
        """Retrieve current values of run-time metrics."""
        return await self._client.send(method="Performance.getMetrics")
