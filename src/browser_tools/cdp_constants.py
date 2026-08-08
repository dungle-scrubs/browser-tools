"""CDP toolset definitions and constants for browser-tools daemon.

Extracted from cdp_handler.py to keep the module under 800 lines.
"""

from __future__ import annotations

REQUEST_TIMEOUT_SECONDS = 120

# Screenshot readiness + blank-frame retry settings.
#
# The chrome-devtools-mcp subprocess takes screenshots immediately when asked,
# which can capture mid-paint frames during CSS transitions or post-reload
# hydration -- producing visually blank or half-rendered images even when
# wait_stable (DOM-mutation based) and wait_idle (network based) report ready.
#
# Two-layer mitigation runs on every take_screenshot:
#   1. Pre-capture rAF gate -- wait two requestAnimationFrame ticks plus
#      document.fonts.ready so the compositor has flushed at least one frame
#      with the latest layout. Cheap; runs always.
#   2. Post-capture blank check -- if the resulting PNG looks near-uniform
#      (very high compression ratio or low luminance variance), wait briefly
#      and retry once. Targets the cases the rAF gate misses.

# Import from the authoritative screenshot_utils module (or define fallbacks).
try:
    from .screenshot_utils import (
        SCREENSHOT_BLANK_BYTES_PER_PIXEL_THRESHOLD as _blank_bpp,
    )
    from .screenshot_utils import (
        SCREENSHOT_BLANK_LUMINANCE_STDDEV_THRESHOLD as _blank_stddev,
    )
    from .screenshot_utils import (
        SCREENSHOT_BLANK_MAX_RETRIES as _blank_max_retries,
    )
    from .screenshot_utils import (
        SCREENSHOT_BLANK_RETRY_DELAY_SECONDS as _blank_retry_secs,
    )
    from .screenshot_utils import (
        SCREENSHOT_PAINT_READY_TIMEOUT_MS as _paint_ready_ms,
    )
except ImportError:
    _paint_ready_ms = 1500
    _blank_retry_secs = 0.25
    _blank_max_retries = 1
    _blank_bpp = 0.02
    _blank_stddev = 5.0

SCREENSHOT_PAINT_READY_TIMEOUT_MS = _paint_ready_ms
SCREENSHOT_BLANK_RETRY_DELAY_SECONDS = _blank_retry_secs
SCREENSHOT_BLANK_MAX_RETRIES = _blank_max_retries
SCREENSHOT_BLANK_BYTES_PER_PIXEL_THRESHOLD = _blank_bpp
SCREENSHOT_BLANK_LUMINANCE_STDDEV_THRESHOLD = _blank_stddev

# Tool routing and behavior sets (CDP_TOOLS, INSPECT_BLOCKED_TOOLS,
# NAVIGATION_TOOLS, INSPECT_WARN_TOOLS, INTERACTION_TOOLS) now live in
# tool_registry.py as the single source of truth. Import them from there.

__all__ = [
    "REQUEST_TIMEOUT_SECONDS",
    "SCREENSHOT_BLANK_BYTES_PER_PIXEL_THRESHOLD",
    "SCREENSHOT_BLANK_LUMINANCE_STDDEV_THRESHOLD",
    "SCREENSHOT_BLANK_MAX_RETRIES",
    "SCREENSHOT_BLANK_RETRY_DELAY_SECONDS",
    "SCREENSHOT_PAINT_READY_TIMEOUT_MS",
]
