# Vendored from chrome-agent v0.5.7 (https://github.com/captivus/chrome-agent).
# Copyright (c) 2026 Corey Gallon.
# SPDX-License-Identifier: MIT
# See /NOTICE for the full vendoring notice.
#
# This file is a verbatim vendored copy; the only permitted modification is
# rewriting intra-package imports to browser_tools.core. See RFC-01, section
# "Vendoring rules".

"""CDP DeviceOrientation domain.

Auto-generated from Chrome DevTools Protocol schema.
Do not edit manually. Re-run the generator to update.
"""

from __future__ import annotations

from typing import Any

from ..cdp_client import CDPClient


class DeviceOrientation:
    """CDP DeviceOrientation domain."""

    def __init__(self, client: CDPClient):
        self._client = client

    async def clear_device_orientation_override(self) -> dict:
        """Clears the overridden Device Orientation."""
        return await self._client.send(method="DeviceOrientation.clearDeviceOrientationOverride")

    async def set_device_orientation_override(
        self,
        alpha: float,
        beta: float,
        gamma: float,
    ) -> dict:
        """Overrides the Device Orientation."""
        params: dict[str, Any] = {}
        params["alpha"] = alpha
        params["beta"] = beta
        params["gamma"] = gamma
        return await self._client.send(method="DeviceOrientation.setDeviceOrientationOverride", params=params)
