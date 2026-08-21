# Vendored from chrome-agent v0.5.7 (https://github.com/captivus/chrome-agent).
# Copyright (c) 2026 Corey Gallon.
# SPDX-License-Identifier: MIT
# See /NOTICE for the full vendoring notice.
#
# This file is a verbatim vendored copy; the only permitted modification is
# rewriting intra-package imports to browser_tools.core. See RFC-01, section
# "Vendoring rules".

"""CDP Schema domain.

This domain is deprecated.

Auto-generated from Chrome DevTools Protocol schema.
Do not edit manually. Re-run the generator to update.
"""

from __future__ import annotations

from typing import Any

from ..cdp_client import CDPClient


# Description of the protocol domain.
Domain = dict  # Object type

class Schema:
    """This domain is deprecated."""

    def __init__(self, client: CDPClient):
        self._client = client

    async def get_domains(self) -> dict:
        """Returns supported domains."""
        return await self._client.send(method="Schema.getDomains")
