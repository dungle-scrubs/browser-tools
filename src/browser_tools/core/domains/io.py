# Vendored from chrome-agent v0.5.7 (https://github.com/captivus/chrome-agent).
# Copyright (c) 2026 Corey Gallon.
# SPDX-License-Identifier: MIT
# See /NOTICE for the full vendoring notice.
#
# This file is a verbatim vendored copy; the only permitted modification is
# rewriting intra-package imports to browser_tools.core. See RFC-01, section
# "Vendoring rules".

"""CDP IO domain.

Input/Output operations for streams produced by DevTools.

Auto-generated from Chrome DevTools Protocol schema.
Do not edit manually. Re-run the generator to update.
"""

from __future__ import annotations

from typing import Any

from ..cdp_client import CDPClient


StreamHandle = str

class IO:
    """Input/Output operations for streams produced by DevTools."""

    def __init__(self, client: CDPClient):
        self._client = client

    async def close(self, handle: StreamHandle) -> dict:
        """Close the stream, discard any temporary backing storage."""
        params: dict[str, Any] = {}
        params["handle"] = handle
        return await self._client.send(method="IO.close", params=params)

    async def read(
        self,
        handle: StreamHandle,
        offset: int | None = None,
        size: int | None = None,
    ) -> dict:
        """Read a chunk of the stream"""
        params: dict[str, Any] = {}
        params["handle"] = handle
        if offset is not None:
            params["offset"] = offset
        if size is not None:
            params["size"] = size
        return await self._client.send(method="IO.read", params=params)

    async def resolve_blob(self, object_id: str) -> dict:
        """Return UUID of Blob object specified by a remote object id."""
        params: dict[str, Any] = {}
        params["objectId"] = object_id
        return await self._client.send(method="IO.resolveBlob", params=params)
