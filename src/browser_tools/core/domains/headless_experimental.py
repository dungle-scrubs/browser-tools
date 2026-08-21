# Vendored from chrome-agent v0.5.7 (https://github.com/captivus/chrome-agent).
# Copyright (c) 2026 Corey Gallon.
# SPDX-License-Identifier: MIT
# See /NOTICE for the full vendoring notice.
#
# This file is a verbatim vendored copy; the only permitted modification is
# rewriting intra-package imports to browser_tools.core. See RFC-01, section
# "Vendoring rules".

"""CDP HeadlessExperimental domain.

This domain provides experimental commands only supported in headless mode.

Auto-generated from Chrome DevTools Protocol schema.
Do not edit manually. Re-run the generator to update.
"""

from __future__ import annotations

from typing import Any

from ..cdp_client import CDPClient


# Encoding options for a screenshot.
ScreenshotParams = dict  # Object type

class HeadlessExperimental:
    """This domain provides experimental commands only supported in headless mode."""

    def __init__(self, client: CDPClient):
        self._client = client

    async def begin_frame(
        self,
        frame_time_ticks: float | None = None,
        interval: float | None = None,
        no_display_updates: bool | None = None,
        screenshot: ScreenshotParams | None = None,
    ) -> dict:
        """Sends a BeginFrame to the target and returns when the frame was completed. Optionally captures a
screenshot from the resulting frame. Requires that the target was created with enabled
BeginFrameControl. Designed for use with --run-all-compositor-stages-before-draw, see also
https://goo.gle/chrome-headless-rendering for more background.
        """
        params: dict[str, Any] = {}
        if frame_time_ticks is not None:
            params["frameTimeTicks"] = frame_time_ticks
        if interval is not None:
            params["interval"] = interval
        if no_display_updates is not None:
            params["noDisplayUpdates"] = no_display_updates
        if screenshot is not None:
            params["screenshot"] = screenshot
        return await self._client.send(method="HeadlessExperimental.beginFrame", params=params)

    async def disable(self) -> dict:
        """Disables headless events for the target."""
        return await self._client.send(method="HeadlessExperimental.disable")

    async def enable(self) -> dict:
        """Enables headless events for the target."""
        return await self._client.send(method="HeadlessExperimental.enable")
