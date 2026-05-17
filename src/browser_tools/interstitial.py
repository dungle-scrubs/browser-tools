"""Interstitial detection for browser-tools.

Detects challenge pages, auth walls, and interstitials after navigation
using a two-pass approach: immediate detection + delayed detection for
late-injected DOM content (D-003).

Supports project-specific overrides via:
    ~/.config/tool-proxy/browser-tools/detect-interstitial.js
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Built-in detection script
_BUILTIN_SCRIPT = Path(__file__).parent / "detect_interstitial.js"
_OVERRIDE_PATH = Path.home() / ".config" / "tool-proxy" / "browser-tools" / "detect-interstitial.js"


def get_detection_script() -> str:
    """Load the interstitial detection JavaScript.

    Prefers project override if present, falls back to built-in.

    Returns:
        JavaScript source code for interstitial detection.
    """
    if _OVERRIDE_PATH.exists():
        try:
            return _OVERRIDE_PATH.read_text()
        except OSError:
            pass
    return _BUILTIN_SCRIPT.read_text()


def parse_detection_result(raw_json: str) -> list[dict[str, Any]]:
    """Parse the JSON output from the detection script.

    Args:
        raw_json: JSON string returned by Runtime.evaluate.

    Returns:
        List of detection result dictionaries.
    """
    try:
        results = json.loads(raw_json)
        if isinstance(results, list):
            return results
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def format_interstitials(
    detections: list[dict[str, Any]],
    auto_retried: bool = False,
    retries_used: int = 0,
) -> str | None:
    """Format detection results as human-readable text.

    Args:
        detections: List of detection result dictionaries.
        auto_retried: Whether auto-retry was attempted for JS-solvable challenges.
        retries_used: Number of retry attempts made.

    Returns:
        Formatted string, or None if no detections.
    """
    if not detections:
        return None

    lines = [f"⚠️  Interstitial detected ({len(detections)} signal(s)):"]
    for d in detections:
        confidence = d.get("confidence", "unknown")
        type_name = d.get("type", "unknown")
        details = d.get("details", "")
        signal = d.get("signal", "")
        lines.append(f"  [{confidence}] {type_name} ({signal}): {details}")

    if auto_retried:
        lines.append(
            f"\n⏳ Auto-retry was attempted ({retries_used} retries, "
            f"~{retries_used * 3}s wait) but the challenge persists."
        )

    lines.append("\nE003: Manual resolution may be required.")
    return "\n".join(lines)


async def detect_interstitials_async(
    cdp_client: Any, context_id: int | None = None
) -> list[dict[str, Any]]:
    """Run interstitial detection via CDP Runtime.evaluate.

    Implements two-pass detection: immediate + 500ms delayed.

    Args:
        cdp_client: Connected CDPClient instance.
        context_id: Execution context ID (None for default/top-level).

    Returns:
        Deduplicated list of detection results.
    """
    import asyncio

    script = get_detection_script()
    all_results: list[dict[str, Any]] = []

    # Pass 1: immediate
    pass1 = await _run_detection(cdp_client, script, context_id)
    all_results.extend(pass1)

    # Pass 2: delayed (catches late-injected DOM)
    await asyncio.sleep(0.5)
    pass2 = await _run_detection(cdp_client, script, context_id)
    all_results.extend(pass2)

    # Deduplicate by type (keep highest confidence)
    return _deduplicate(all_results)


def detect_interstitials_sync(
    cdp_client: Any, context_id: int | None = None
) -> list[dict[str, Any]]:
    """Synchronous wrapper for interstitial detection.

    Args:
        cdp_client: Connected CDPClient instance.
        context_id: Execution context ID.

    Returns:
        Deduplicated list of detection results.
    """
    import asyncio

    loop = asyncio.get_event_loop()
    if loop.is_running():
        # Already in async context — run directly
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(
                lambda: asyncio.run(detect_interstitials_async(cdp_client, context_id))
            ).result()
    return asyncio.run(detect_interstitials_async(cdp_client, context_id))


async def _run_detection(
    cdp_client: Any, script: str, context_id: int | None
) -> list[dict[str, Any]]:
    """Execute detection script and parse results.

    Args:
        cdp_client: Connected CDPClient.
        script: JavaScript detection script.
        context_id: Execution context ID.

    Returns:
        List of detection results.
    """
    params: dict[str, Any] = {
        "expression": script,
        "returnByValue": True,
    }
    if context_id is not None:
        params["contextId"] = context_id

    try:
        result = await cdp_client.send("Runtime.evaluate", params)
        value = result.get("result", {}).get("value", "[]")
        return parse_detection_result(value)
    except Exception:
        return []


def _deduplicate(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate by type, keeping highest confidence per type.

    Args:
        results: List of detection results.

    Returns:
        Deduplicated list.
    """
    rank = {"high": 3, "medium": 2, "low": 1}
    seen: dict[str, dict[str, Any]] = {}
    for r in results:
        type_name = r.get("type", "unknown")
        existing = seen.get(type_name)
        if not existing or rank.get(r.get("confidence", ""), 0) > rank.get(
            existing.get("confidence", ""), 0
        ):
            seen[type_name] = r
    return list(seen.values())
