"""Post-navigation interstitial detection for browser-tools.

Single owner of the challenge-response policy: loads the detection script,
runs a two-pass single-shot detect (immediate + delayed for late-injected
DOM, D-003), auto-retries JS-solvable challenges, deduplicates, and formats
the result. The retry tuning (delay, max retries, retryable types) lives here
next to the loop that reads it; CDPHandler exposes only a thread-safe
``run_post_navigation_detection`` that marshals :func:`detect_with_retry` onto
its event loop.

Supports project-specific overrides via:
    ~/.config/tool-proxy/browser-tools/detect-interstitial.js
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Built-in detection script
_BUILTIN_SCRIPT = Path(__file__).parent / "detect_interstitial.js"
_OVERRIDE_PATH = Path.home() / ".config" / "tool-proxy" / "browser-tools" / "detect-interstitial.js"

# Auto-retry tuning for JS-solvable challenges (Cloudflare JS challenge,
# access-denied pages). Human-interaction challenges (CAPTCHA, auth walls,
# ngrok warnings, enterprise bot managers) are reported immediately. Kept here,
# next to detect_with_retry, the only reader.
INTERSTITIAL_RETRY_DELAY_SECONDS = 3.0
INTERSTITIAL_MAX_RETRIES = 3
INTERSTITIAL_AUTO_RETRY_TYPES = frozenset({"cloudflare_challenge", "access_denied"})

# Signal taxonomy. A detection's ``signal`` field says what kind of evidence
# fired. A Presence Signal establishes that a site *uses* a vendor: its cookie
# is set, or its script is on the page. Those are present on every page of a
# protected site, including ordinary pages inside a logged-in session, so they
# do not mean this page is blocking. Every other signal is a Challenge Signal:
# the page itself is blocking or asking. Only a Challenge Signal makes a page
# an Interstitial.
PRESENCE_SIGNALS = frozenset({"cookie", "script_src"})


def is_challenge_signal(detection: dict[str, Any]) -> bool:
    """Return whether a detection is evidence that this page is blocking.

    Args:
        detection: One detection dict from the detection script.

    Returns:
        True for a Challenge Signal, False for a Presence Signal.
    """
    return detection.get("signal") not in PRESENCE_SIGNALS


def detect_total_timeout(max_retries: int | None = None) -> float:
    """Outer timeout for one detect-and-retry cycle.

    Used by the thread-safe marshaler in
    ``CDPHandler.run_post_navigation_detection``. The budget varies per call
    now that the caller can set it, so this is computed rather than a
    constant. Generous buffer over (initial detect + retries * delay) so a
    slow page cannot hang the daemon.

    Args:
        max_retries: Retry budget, or None for :data:`INTERSTITIAL_MAX_RETRIES`.

    Returns:
        Seconds.
    """
    retries = INTERSTITIAL_MAX_RETRIES if max_retries is None else max_retries
    return 10 + retries * (INTERSTITIAL_RETRY_DELAY_SECONDS + 2)


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
            logger.debug("Failed to read override script, falling back to built-in", exc_info=True)
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
        logger.debug("Failed to parse detection result JSON", exc_info=True)
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


def _split_on_challenge_gate(
    results: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split detections into challenges and vendor presence.

    The Challenge Gate: only a Challenge Signal makes a page an Interstitial.
    Presence is returned separately so a caller that wants to know which vendor
    fronts a site still can, without it driving the blocked answer.

    Args:
        results: Detections from one single-shot pass.

    Returns:
        ``(challenges, presence)``.
    """
    challenges = [d for d in results if is_challenge_signal(d)]
    presence = [d for d in results if not is_challenge_signal(d)]
    return challenges, presence


async def detect_with_retry(
    detect_once: Callable[[], Awaitable[list[dict[str, Any]]]],
    *,
    max_retries: int | None = None,
    delay: float | None = None,
) -> dict[str, Any]:
    """Run interstitial detection with auto-retry for JS-solvable challenges.

    The retry policy: call ``detect_once``; if only non-retryable types remain,
    report immediately; if any retryable type is present, wait
    :data:`INTERSTITIAL_RETRY_DELAY_SECONDS` and re-check, up to
    :data:`INTERSTITIAL_MAX_RETRIES` times. A challenge that clears returns an
    empty detection list with ``auto_retried=True``.

    Args:
        detect_once: A single-shot detection pass returning a list of detection
            dicts (typically :func:`detect_interstitials_async` bound to a CDP
            client). Passed in so this module owns the policy without owning
            the CDP client or the event loop.

    Returns:
        Dict with ``detections`` (list), ``auto_retried`` (bool), and
        ``retries_used`` (int).
    """
    if max_retries is None:
        max_retries = INTERSTITIAL_MAX_RETRIES
    if delay is None:
        delay = INTERSTITIAL_RETRY_DELAY_SECONDS

    detections, presence = _split_on_challenge_gate(await detect_once())
    if not detections:
        return {
            "detections": [],
            "presence": presence,
            "auto_retried": False,
            "retries_used": 0,
        }

    retryable = [d for d in detections if d.get("type") in INTERSTITIAL_AUTO_RETRY_TYPES]

    if not retryable:
        return {
            "detections": detections,
            "presence": presence,
            "auto_retried": False,
            "retries_used": 0,
        }

    for attempt in range(max_retries):
        await asyncio.sleep(delay)
        detections, presence = _split_on_challenge_gate(await detect_once())

        if not detections:
            return {
                "detections": [],
                "presence": presence,
                "auto_retried": True,
                "retries_used": attempt + 1,
            }

        retryable = [d for d in detections if d.get("type") in INTERSTITIAL_AUTO_RETRY_TYPES]
        if not retryable:
            return {
                "detections": detections,
                "presence": presence,
                "auto_retried": True,
                "retries_used": attempt + 1,
            }

    # Exhausted retries -- report the last pass, which is the page's current
    # state. The first pass's non-retryable detections are not appended: they
    # are stale by this point, and anything still present is already here.
    return {
        "detections": detections,
        "presence": presence,
        # With a zero budget the loop never ran, so nothing was retried.
        "auto_retried": max_retries > 0,
        "retries_used": max_retries,
    }


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
    except (OSError, AttributeError, TypeError):
        # CDP client may be unreachable or return unexpected shapes.
        # Detection is best-effort — never crash the caller.
        logger.debug("Interstitial detection failed", exc_info=True)
        return []


def _deduplicate(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate by type, keeping the most informative detection per type.

    A Challenge Signal always outranks a Presence Signal for the same type,
    whatever their confidence strings say: the vendor's cookie being set tells
    the caller nothing about whether this page is blocking, while the challenge
    iframe does. Within one signal class, confidence decides. Ranking on
    confidence alone dropped the challenge evidence for any vendor whose
    cookie check runs earlier in the script than its challenge check, which is
    all of them.

    Args:
        results: List of detection results.

    Returns:
        Deduplicated list.
    """
    rank = {"high": 3, "medium": 2, "low": 1}

    def score(detection: dict[str, Any]) -> tuple[int, int]:
        return (
            1 if is_challenge_signal(detection) else 0,
            rank.get(detection.get("confidence", ""), 0),
        )

    seen: dict[str, dict[str, Any]] = {}
    for r in results:
        type_name = r.get("type", "unknown")
        existing = seen.get(type_name)
        if not existing or score(r) > score(existing):
            seen[type_name] = r
    return list(seen.values())


__all__ = [
    "INTERSTITIAL_AUTO_RETRY_TYPES",
    "INTERSTITIAL_MAX_RETRIES",
    "INTERSTITIAL_RETRY_DELAY_SECONDS",
    "PRESENCE_SIGNALS",
    "detect_interstitials_async",
    "detect_total_timeout",
    "detect_with_retry",
    "format_interstitials",
    "get_detection_script",
    "is_challenge_signal",
    "parse_detection_result",
]
