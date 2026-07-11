#!/usr/bin/env python3
"""Camoufox anti-detect browser session for tool-proxy.

Launches a Camoufox (custom Firefox) browser with C++ fingerprint injection
and exposes automation tools for navigating bot-protected sites.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from camoufox.sync_api import Camoufox
except ImportError:
    Camoufox = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# Named Camoufox profiles persist cookies + localStorage as a Playwright
# storage-state file so a login survives close_browser and process exit.
CAMOUFOX_STATE_DIR = Path.home() / ".cache" / "tool-proxy" / "browser-tools" / "camoufox_profiles"
_SAFE_PROFILE_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _camoufox_state_path(profile: str) -> Path | None:
    """Resolve the storage-state path for a named Camoufox profile.

    Args:
        profile: Requested profile name.

    Returns:
        Path to the profile's storage-state file, or None when the name is
        empty or contains anything but ``[A-Za-z0-9._-]`` (which could escape
        the state directory).
    """
    if not profile or profile in {".", ".."} or not _SAFE_PROFILE_RE.match(profile):
        return None
    return CAMOUFOX_STATE_DIR / f"{profile}.json"


class CamoufoxSession:
    """Manages a Camoufox anti-detect browser session.

    Wraps the Camoufox Python library to provide tool-proxy compatible
    browser automation with anti-detect fingerprinting.
    """

    def __init__(self) -> None:
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._camoufox_cm: Any = None
        self._storage_path: Path | None = None

    def call_tool(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a tool call.

        Args:
            tool: Tool name.
            args: Tool arguments.

        Returns:
            Result dict with 'result' or 'error' key.
        """
        args = args or {}
        handler = getattr(self, f"_tool_{tool}", None)
        if handler is None:
            return {"error": f"Unknown tool: {tool}"}
        try:
            return {"result": handler(args)}
        except Exception as exc:
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def _tool_launch_browser(self, args: dict[str, Any]) -> dict[str, Any]:
        """Launch a Camoufox anti-detect browser.

        Args:
            args: Launch configuration (headless, proxy, viewport, os).

        Returns:
            Session info including status and fingerprint summary.
        """
        if self._browser is not None:
            return {"status": "already_running", "fingerprint": "existing session"}

        camoufox_kwargs: dict[str, Any] = {}

        if args.get("headless"):
            camoufox_kwargs["headless"] = True

        proxy = args.get("proxy")
        if proxy:
            camoufox_kwargs["proxy"] = proxy

        target_os = args.get("os")
        if target_os:
            os_map = {"windows": "Windows", "macos": "Macintosh", "linux": "Linux"}
            camoufox_kwargs["os"] = os_map.get(target_os, target_os)

        # A named profile persists login state across sessions: load any saved
        # storage-state now and write it back on close.
        profile = args.get("profile")
        if profile:
            self._storage_path = _camoufox_state_path(profile)
            if self._storage_path is None:
                # Fail loudly rather than silently launching profile-less, which
                # would discard the login the caller asked us to persist.
                raise ValueError(
                    f"Invalid profile name '{profile}': use only letters, digits, "
                    "'.', '_', or '-'."
                )
        else:
            self._storage_path = None
        context_kwargs: dict[str, Any] = {}
        restored = False
        if self._storage_path is not None and self._storage_path.exists():
            context_kwargs["storage_state"] = str(self._storage_path)
            restored = True

        self._camoufox_cm = Camoufox(**camoufox_kwargs)  # type: ignore[reportOptionalCall]
        self._browser = self._camoufox_cm.__enter__()  # type: ignore[reportOptionalMemberAccess]
        self._context = self._browser.new_context(**context_kwargs)  # type: ignore[reportAttributeAccessIssue]
        self._page = self._context.new_page()  # type: ignore[reportOptionalMemberAccess]

        return {
            "status": "running",
            "fingerprint": "auto-generated via BrowserForge",
            "profile": profile if self._storage_path is not None else None,
            "restored_state": restored,
        }

    def _tool_navigate(self, args: dict[str, Any]) -> dict[str, Any]:
        """Navigate to a URL.

        Args:
            args: Must contain 'url'. Optional 'wait_until'.

        Returns:
            Page title, URL, and interstitial detection result.
        """
        self._ensure_page()
        url = args["url"]
        wait_until = args.get("wait_until", "load")
        self._page.goto(url, wait_until=wait_until)

        title = self._page.title()
        current_url = self._page.url
        interstitial = self._detect_interstitial(title, self._page.content())

        return {
            "title": title,
            "url": current_url,
            "interstitial": interstitial,
        }

    def _tool_snapshot(self, args: dict[str, Any]) -> dict[str, Any]:
        """Get accessibility tree snapshot.

        Uses Playwright's aria_snapshot() for a structured ARIA tree.
        Falls back to page.accessibility.snapshot() for standard Playwright,
        and to inner_text() as a last resort.

        Args:
            args: Empty dict (no parameters).

        Returns:
            Accessibility tree (structured text or dict).
        """
        self._ensure_page()

        # Prefer aria_snapshot (Camoufox Playwright)
        try:
            tree = self._page.locator("body").aria_snapshot()
            return {"tree": tree}
        except Exception:
            logger.debug("aria_snapshot unavailable, trying Playwright accessibility API")

        # Fallback: standard Playwright accessibility API
        try:
            tree = self._page.accessibility.snapshot()
            return {"tree": tree}
        except Exception:
            logger.debug("Playwright accessibility API unavailable, falling back to inner_text")

        # Last resort: text content
        text = self._page.inner_text("body")
        return {"tree": text}

    def _tool_screenshot(self, args: dict[str, Any]) -> dict[str, Any]:
        """Take a page screenshot.

        Args:
            args: Optional 'path' and 'full_page'.

        Returns:
            Path to saved screenshot file.
        """
        self._ensure_page()
        path = args.get("path")
        if not path:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = f"screenshot_{timestamp}.png"

        full_page = args.get("full_page", False)
        self._page.screenshot(path=path, full_page=full_page)

        return {"path": path}

    def _tool_click(self, args: dict[str, Any]) -> dict[str, Any]:
        """Click an element by CSS selector.

        Args:
            args: Must contain 'selector'.

        Returns:
            Confirmation.
        """
        self._ensure_page()
        selector = args["selector"]
        self._page.click(selector)
        return {"clicked": selector}

    def _tool_fill(self, args: dict[str, Any]) -> dict[str, Any]:
        """Type text into a form field.

        Args:
            args: Must contain 'selector' and 'value'.

        Returns:
            Confirmation.
        """
        self._ensure_page()
        self._page.fill(args["selector"], args["value"])
        return {"filled": args["selector"]}

    def _tool_evaluate(self, args: dict[str, Any]) -> dict[str, Any]:
        """Execute JavaScript in page context.

        Args:
            args: Must contain 'script'.

        Returns:
            JS evaluation result.
        """
        self._ensure_page()
        value = self._page.evaluate(args["script"])
        return {"value": value}

    def _tool_wait_for_human(self, args: dict[str, Any]) -> dict[str, Any]:
        """Pause and poll for human intervention (CAPTCHA, login, etc.).

        Args:
            args: Optional check_selector_gone, check_url_contains, timeout, poll_interval.

        Returns:
            Whether the challenge was resolved within the timeout.
        """
        self._ensure_page()
        reason = args.get("reason", "Manual intervention needed")
        timeout = args.get("timeout", 300)
        poll_interval = args.get("poll_interval", 2.0)
        check_selector = args.get("check_selector_gone")
        check_url = args.get("check_url_contains")

        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            # Check if challenge selector disappeared
            if check_selector:
                element = self._page.query_selector(check_selector)
                if element is None:
                    return {
                        "resolved": True,
                        "reason": f"Challenge element '{check_selector}' is gone",
                    }

            # Check if URL changed to expected destination
            if check_url and check_url in self._page.url:
                return {
                    "resolved": True,
                    "reason": f"URL now contains '{check_url}'",
                }

            # Neither condition met and no check specified — just wait once
            if not check_selector and not check_url:
                return {
                    "resolved": False,
                    "reason": "No check condition specified. Use check_selector_gone or check_url_contains.",
                }

            time.sleep(poll_interval)

        return {
            "resolved": False,
            "reason": f"Timeout after {timeout}s waiting for human intervention: {reason}",
        }

    def _tool_get_cookies(self, args: dict[str, Any]) -> dict[str, Any]:
        """Get cookies from browser context.

        Args:
            args: Optional 'urls' list to filter cookies.

        Returns:
            List of cookie dicts.
        """
        self._ensure_page()
        urls = args.get("urls")
        cookies = self._context.cookies(urls=urls) if urls else self._context.cookies()
        return {"cookies": cookies}

    def _tool_close_browser(self, args: dict[str, Any]) -> dict[str, Any]:
        """Close the browser and clean up.

        Args:
            args: Empty dict (no parameters).

        Returns:
            Confirmation.
        """
        saved = self._save_storage_state()
        if self._camoufox_cm is not None:
            self._camoufox_cm.__exit__(None, None, None)
        self._browser = None
        self._context = None
        self._page = None
        self._camoufox_cm = None
        self._storage_path = None
        return {"status": "closed", "state_saved": saved}

    def _save_storage_state(self) -> bool:
        """Persist the current context's cookies + localStorage, if a profile is set.

        Returns:
            True when a storage-state file was written, otherwise False.
        """
        if self._storage_path is None or self._context is None:
            return False
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            self._context.storage_state(path=str(self._storage_path))
        except Exception:
            logger.exception("Failed to persist Camoufox storage state")
            return False
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_page(self) -> None:
        """Raise if browser is not launched.

        Raises:
            RuntimeError: When no browser session is active.
        """
        if self._page is None:
            raise RuntimeError("Browser not launched. Call launch_browser first.")

    @staticmethod
    def _detect_interstitial(title: str, html: str) -> dict[str, Any]:
        """Detect common anti-bot interstitial pages.

        Args:
            title: Page title text.
            html: Raw HTML content of the page.

        Returns:
            Dict with 'detected' bool, and 'type'/'confidence' when detected.
        """
        title_lower = title.lower()

        # Cloudflare challenge
        if "just a moment" in title_lower or "cf-wrapper" in html.lower():
            return {"detected": True, "type": "cloudflare_challenge", "confidence": "high"}

        # Access denied / 403
        if any(phrase in title_lower for phrase in ("access denied", "403 forbidden", "blocked")):
            return {"detected": True, "type": "access_denied", "confidence": "medium"}

        # CAPTCHA
        captcha_markers = ["recaptcha", "hcaptcha", "g-recaptcha", "h-captcha"]
        html_lower = html.lower()
        if any(marker in html_lower for marker in captcha_markers):
            return {"detected": True, "type": "captcha", "confidence": "medium"}

        return {"detected": False}
