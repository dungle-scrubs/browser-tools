"""Tests for interstitial detection (M-3.1, M-3.2) and auto-retry."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from browser_tools.interstitial import (
    _deduplicate,
    detect_with_retry,
    format_interstitials,
    get_detection_script,
    parse_detection_result,
)


class TestGetDetectionScript:
    """Tests for loading the detection script."""

    def test_loads_builtin_script(self) -> None:
        """Built-in script should be loadable."""
        script = get_detection_script()
        assert "detectInterstitials" in script
        assert "cloudflare" in script.lower()

    def test_override_takes_precedence(self, monkeypatch, tmp_path: Path) -> None:
        """Project override should be preferred over built-in."""
        override = tmp_path / "detect-interstitial.js"
        override.write_text("// custom override\n(function() { return '[]'; })()")
        monkeypatch.setattr("browser_tools.interstitial._OVERRIDE_PATH", override)

        script = get_detection_script()
        assert "custom override" in script

    def test_falls_back_on_missing_override(self, monkeypatch, tmp_path: Path) -> None:
        """Missing override should fall back to built-in."""
        monkeypatch.setattr(
            "browser_tools.interstitial._OVERRIDE_PATH", tmp_path / "nonexistent.js"
        )

        script = get_detection_script()
        assert "detectInterstitials" in script


class TestParseDetectionResult:
    """Tests for parsing detection script output."""

    def test_parses_valid_json(self) -> None:
        """Valid JSON array should parse correctly."""
        raw = json.dumps(
            [{"type": "cloudflare_challenge", "confidence": "high", "signal": "title_pattern"}]
        )
        results = parse_detection_result(raw)
        assert len(results) == 1
        assert results[0]["type"] == "cloudflare_challenge"

    def test_empty_array(self) -> None:
        """Empty array is a valid clean page result."""
        results = parse_detection_result("[]")
        assert results == []

    def test_invalid_json_returns_empty(self) -> None:
        """Invalid JSON should return empty list."""
        results = parse_detection_result("not json")
        assert results == []

    def test_none_returns_empty(self) -> None:
        """None input should return empty list."""
        results = parse_detection_result(None)
        assert results == []

    def test_non_array_returns_empty(self) -> None:
        """Non-array JSON should return empty list."""
        results = parse_detection_result('{"type": "test"}')
        assert results == []


class TestDeduplicate:
    """Tests for detection result deduplication."""

    def test_keeps_highest_confidence(self) -> None:
        """When same type appears twice, highest confidence wins."""
        results = [
            {"type": "cloudflare_challenge", "confidence": "low", "signal": "css"},
            {"type": "cloudflare_challenge", "confidence": "high", "signal": "title"},
        ]
        deduped = _deduplicate(results)
        assert len(deduped) == 1
        assert deduped[0]["confidence"] == "high"
        assert deduped[0]["signal"] == "title"

    def test_different_types_preserved(self) -> None:
        """Different types should both be kept."""
        results = [
            {"type": "cloudflare_challenge", "confidence": "high"},
            {"type": "auth_wall", "confidence": "medium"},
        ]
        deduped = _deduplicate(results)
        assert len(deduped) == 2

    def test_empty_list(self) -> None:
        """Empty input returns empty output."""
        assert _deduplicate([]) == []


class TestFormatInterstitials:
    """Tests for human-readable interstitial formatting."""

    def test_formats_detections(self) -> None:
        """Detections should be formatted with confidence and type."""
        detections = [
            {
                "type": "cloudflare_challenge",
                "confidence": "high",
                "signal": "title_pattern",
                "details": "Title matches Cloudflare pattern",
            }
        ]
        text = format_interstitials(detections)
        assert text is not None
        assert "cloudflare_challenge" in text
        assert "high" in text
        assert "E003" in text

    def test_empty_returns_none(self) -> None:
        """No detections should return None."""
        assert format_interstitials([]) is None

    def test_multiple_detections(self) -> None:
        """Multiple detections should all appear."""
        detections = [
            {
                "type": "cloudflare_challenge",
                "confidence": "high",
                "signal": "title",
                "details": "cf",
            },
            {"type": "captcha", "confidence": "medium", "signal": "css", "details": "recaptcha"},
        ]
        text = format_interstitials(detections)
        assert "cloudflare_challenge" in text
        assert "captcha" in text
        assert "2 signal" in text


class TestDetectionScriptContent:
    """Tests for the detection script's patterns against mock HTML."""

    def _run_script_on_html(self, html: str) -> list[dict]:
        """Helper: simulate running detection script against HTML content.

        Since we can't run real JS, we test the pattern logic indirectly
        by verifying the script source contains the expected patterns.
        """
        # This is a static analysis test — verify patterns exist in script
        script = get_detection_script()
        return script  # Return script for assertion

    def test_script_detects_cloudflare_title_pattern(self) -> None:
        """Script should check for 'Just a moment' title."""
        script = get_detection_script()
        assert "just a moment" in script.lower()

    def test_script_detects_ngrok(self) -> None:
        """Script should check for ngrok patterns."""
        script = get_detection_script()
        assert "ngrok" in script.lower()

    def test_script_detects_auth_wall(self) -> None:
        """Script should check for login/auth patterns."""
        script = get_detection_script()
        assert "password" in script.lower()
        assert "log" in script.lower() and "in" in script.lower()

    def test_script_detects_captcha(self) -> None:
        """Script should check for CAPTCHA elements."""
        script = get_detection_script()
        assert "recaptcha" in script.lower()
        assert "hcaptcha" in script.lower()

    def test_script_returns_json(self) -> None:
        """Script should return JSON.stringify output."""
        script = get_detection_script()
        assert "JSON.stringify" in script

    def test_script_has_confidence_levels(self) -> None:
        """Script should assign confidence levels."""
        script = get_detection_script()
        assert "'high'" in script
        assert "'medium'" in script
        assert "'low'" in script

    def test_script_detects_datadome(self) -> None:
        """Script should check for DataDome signatures."""
        script = get_detection_script()
        assert "datadome" in script.lower()
        assert "captcha-delivery.com" in script

    def test_script_detects_akamai(self) -> None:
        """Script should check for Akamai Bot Manager signatures."""
        script = get_detection_script()
        assert "_abck" in script

    def test_script_detects_perimeterx(self) -> None:
        """Script should check for PerimeterX/HUMAN signatures."""
        script = get_detection_script()
        assert "perimeterx" in script.lower()
        assert "px-cdn" in script.lower()
        assert "press" in script.lower()

    def test_script_detects_imperva(self) -> None:
        """Script should check for Imperva/Incapsula signatures."""
        script = get_detection_script()
        assert "incap_ses_" in script
        assert "visid_incap_" in script
        assert "incapsula" in script.lower()

    def test_script_detects_aws_waf(self) -> None:
        """Script should check for AWS WAF signatures."""
        script = get_detection_script()
        assert "aws-waf-token" in script
        assert "aws-waf" in script.lower()


class TestFormatInterstitialsRetry:
    """Tests for format_interstitials with auto-retry metadata."""

    def test_no_retry_info_by_default(self) -> None:
        """Without retry params, output should match legacy format."""
        detections = [
            {
                "type": "captcha",
                "confidence": "medium",
                "signal": "css_selector",
                "details": "reCAPTCHA detected",
            }
        ]
        text = format_interstitials(detections)
        assert "Auto-retry" not in text
        assert "E003" in text

    def test_shows_retry_info_when_exhausted(self) -> None:
        """When auto-retry was attempted but failed, show retry context."""
        detections = [
            {
                "type": "cloudflare_challenge",
                "confidence": "high",
                "signal": "title_pattern",
                "details": "Title matches Cloudflare pattern",
            }
        ]
        text = format_interstitials(detections, auto_retried=True, retries_used=3)
        assert "Auto-retry was attempted" in text
        assert "3 retries" in text
        assert "~9s wait" in text
        assert "E003" in text

    def test_no_retry_info_when_not_retried(self) -> None:
        """When auto_retried is False, don't show retry info."""
        detections = [
            {
                "type": "auth_wall",
                "confidence": "medium",
                "signal": "title_and_form",
                "details": "Login form detected",
            }
        ]
        text = format_interstitials(detections, auto_retried=False, retries_used=0)
        assert "Auto-retry" not in text


class TestAutoRetryConstants:
    """Tests for auto-retry constants and type classification."""

    def test_retry_types_are_js_solvable(self) -> None:
        """Only JS-solvable challenge types should be in auto-retry set."""
        from browser_tools.interstitial import INTERSTITIAL_AUTO_RETRY_TYPES

        assert "cloudflare_challenge" in INTERSTITIAL_AUTO_RETRY_TYPES
        assert "access_denied" in INTERSTITIAL_AUTO_RETRY_TYPES
        # Human-interaction types must NOT be in the set
        assert "captcha" not in INTERSTITIAL_AUTO_RETRY_TYPES
        assert "auth_wall" not in INTERSTITIAL_AUTO_RETRY_TYPES
        assert "ngrok_warning" not in INTERSTITIAL_AUTO_RETRY_TYPES

    def test_retry_delay_is_reasonable(self) -> None:
        """Retry delay should be between 1-10 seconds."""
        from browser_tools.interstitial import INTERSTITIAL_RETRY_DELAY_SECONDS

        assert 1.0 <= INTERSTITIAL_RETRY_DELAY_SECONDS <= 10.0

    def test_max_retries_is_bounded(self) -> None:
        """Max retries should be small to keep navigation responsive."""
        from browser_tools.interstitial import INTERSTITIAL_MAX_RETRIES

        assert 1 <= INTERSTITIAL_MAX_RETRIES <= 5

    def test_total_wait_time_under_20s(self) -> None:
        """Total auto-retry wait must not exceed 20s to keep UX responsive."""
        from browser_tools.interstitial import (
            INTERSTITIAL_MAX_RETRIES,
            INTERSTITIAL_RETRY_DELAY_SECONDS,
        )

        total = INTERSTITIAL_MAX_RETRIES * INTERSTITIAL_RETRY_DELAY_SECONDS
        assert total <= 20.0


class TestDetectWithRetry:
    """Tests for interstitial.detect_with_retry policy.

    The retry policy now has its own interface (detect_with_retry, taking a
    single-shot detection callable), so these tests exercise it directly
    instead of poking private methods off a half-built CDPHandler.
    """

    @pytest.mark.asyncio
    async def test_no_detections_returns_empty(self) -> None:
        """When no interstitial detected, return empty with no retry."""
        detect_once = AsyncMock(return_value=[])

        result = await detect_with_retry(detect_once)

        assert result["detections"] == []
        assert result["auto_retried"] is False
        assert result["retries_used"] == 0
        detect_once.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_retryable_reported_immediately(self) -> None:
        """CAPTCHA/auth_wall should be reported without retry."""
        captcha = [{"type": "captcha", "confidence": "medium", "signal": "css"}]
        detect_once = AsyncMock(return_value=captcha)

        result = await detect_with_retry(detect_once)

        assert result["detections"] == captcha
        assert result["auto_retried"] is False
        # Only the initial detection call, no retries
        assert detect_once.call_count == 1

    @pytest.mark.asyncio
    async def test_cloudflare_clears_on_first_retry(self) -> None:
        """JS challenge that clears after one retry should report success."""
        cloudflare = [{"type": "cloudflare_challenge", "confidence": "high", "signal": "title"}]
        detect_once = AsyncMock(side_effect=[cloudflare, []])  # Initial, then cleared

        with patch("browser_tools.interstitial.INTERSTITIAL_RETRY_DELAY_SECONDS", 0.01):
            result = await detect_with_retry(detect_once)

        assert result["detections"] == []
        assert result["auto_retried"] is True
        assert result["retries_used"] == 1

    @pytest.mark.asyncio
    async def test_cloudflare_persists_exhausts_retries(self) -> None:
        """JS challenge that never clears should exhaust retries and report."""
        cloudflare = [{"type": "cloudflare_challenge", "confidence": "high", "signal": "title"}]
        detect_once = AsyncMock(return_value=cloudflare)

        with patch("browser_tools.interstitial.INTERSTITIAL_RETRY_DELAY_SECONDS", 0.01):
            result = await detect_with_retry(detect_once)

        assert len(result["detections"]) > 0
        assert result["auto_retried"] is True
        assert result["retries_used"] == 3  # INTERSTITIAL_MAX_RETRIES

    @pytest.mark.asyncio
    async def test_mixed_retryable_and_non_retryable(self) -> None:
        """Mixed types: retry for JS challenge, report CAPTCHA immediately."""
        mixed = [
            {"type": "cloudflare_challenge", "confidence": "high", "signal": "title"},
            {"type": "captcha", "confidence": "medium", "signal": "css"},
        ]
        # After retry, only captcha remains (cloudflare cleared)
        captcha_only = [{"type": "captcha", "confidence": "medium", "signal": "css"}]
        detect_once = AsyncMock(side_effect=[mixed, captcha_only])

        with patch("browser_tools.interstitial.INTERSTITIAL_RETRY_DELAY_SECONDS", 0.01):
            result = await detect_with_retry(detect_once)

        assert result["auto_retried"] is True
        assert result["retries_used"] == 1
        # Should report the captcha (non-retryable that remains)
        assert any(d["type"] == "captcha" for d in result["detections"])

    @pytest.mark.asyncio
    async def test_access_denied_is_retryable(self) -> None:
        """access_denied type should trigger auto-retry."""
        denied = [{"type": "access_denied", "confidence": "medium", "signal": "title"}]
        detect_once = AsyncMock(side_effect=[denied, []])  # Clears on retry

        with patch("browser_tools.interstitial.INTERSTITIAL_RETRY_DELAY_SECONDS", 0.01):
            result = await detect_with_retry(detect_once)

        assert result["detections"] == []
        assert result["auto_retried"] is True
        assert result["retries_used"] == 1


class TestStealthRemoved:
    """RFC-01 "Anti-detection": stealth.js and its injection path are deleted.

    Each JS override was independently detectable, so the merged tool MUST NOT
    inject JavaScript for fingerprint purposes. These tests replace the old
    ``TestStealthScript`` suite, which asserted the deleted script's content
    (navigator.webdriver/plugins/WebGL/chrome.runtime/permissions patches) --
    that content no longer exists, so those assertions are meaningless. Chrome
    fingerprinting is now launch-flag profiles (``core/fingerprint.py``);
    Camoufox remains the engine-level path.
    """

    def test_stealth_script_does_not_exist(self) -> None:
        """stealth.js must be gone, not merely unused."""
        stealth_path = Path(__file__).resolve().parents[1] / "src" / "browser_tools" / "stealth.js"
        assert not stealth_path.exists(), f"stealth.js still present at {stealth_path}"

    def test_cdp_handler_has_no_injection_method(self) -> None:
        """The JS-injection call site (_inject_stealth) must be gone from cdp_handler."""
        from browser_tools.cdp_handler import CDPRuntime

        assert not hasattr(CDPRuntime, "_inject_stealth")

    def test_cdp_handler_source_has_no_stealth_script_injection(self) -> None:
        """No addScriptToEvaluateOnNewDocument call in cdp_handler.py reads stealth.js."""
        cdp_handler_path = (
            Path(__file__).resolve().parents[1] / "src" / "browser_tools" / "cdp_handler.py"
        )
        source = cdp_handler_path.read_text()
        assert "stealth.js" not in source
        assert "addScriptToEvaluateOnNewDocument" not in source


class TestStealthDaemonWiring:
    """Tests for stealth flag propagation through daemon.

    The ``stealth`` argument stays on the frozen MCP surface (RFC-01, "MCP
    compatibility contract") -- removing it would change a tool's argument
    shape. It is accepted and stored but no longer triggers any JS injection
    (see TestStealthRemoved): the flag is now inert.
    """

    def test_cdp_handler_accepts_stealth_flag(self) -> None:
        """CDPHandler should propagate the stealth flag to its CDP runtime."""
        from browser_tools.mcp_daemon import CDPHandler

        handler = CDPHandler(None, mode="full", stealth=True)
        assert handler._rt._stealth is True

    def test_cdp_handler_defaults_stealth_false(self) -> None:
        """CDPHandler should default to stealth=False on its CDP runtime."""
        from browser_tools.mcp_daemon import CDPHandler

        handler = CDPHandler(None)
        assert handler._rt._stealth is False

    def test_enterprise_detections_not_auto_retried(self) -> None:
        """Enterprise bot protections should not be in auto-retry set."""
        from browser_tools.interstitial import INTERSTITIAL_AUTO_RETRY_TYPES

        enterprise_types = {"datadome", "akamai_bot_manager", "perimeterx", "imperva", "aws_waf"}
        for t in enterprise_types:
            assert t not in INTERSTITIAL_AUTO_RETRY_TYPES, (
                f"{t} should not be auto-retried — requires human interaction"
            )
