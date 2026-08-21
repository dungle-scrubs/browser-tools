"""Tests for the vendored launcher's fingerprint-to-launch-flags path (#45).

RFC-01 "Anti-detection": stealth.js and its JS-injection path are deleted;
Chrome fingerprinting is launch-flag profiles only (``core/fingerprint.py``).
These tests prove ``launch_browser`` turns a fingerprint profile file into
Chrome command-line flags and a ``TZ`` environment variable -- not that the
browser actually honors those flags at runtime (that guarantee needs a live
browser and is unproven here).

No real browser is launched: ``subprocess.Popen`` and ``check_cdp_port`` are
monkeypatched, and the launch runs headless against an isolated registry so
neither the desktop-move step nor the window-border supervisor spawn.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

from browser_tools.core import launcher
from browser_tools.core.connection import PortStatus


@dataclass
class _FakeProcess:
    pid: int = 424242
    returncode: int | None = None

    def poll(self):
        return None


def _fingerprint_file(tmp_path, **overrides):
    data = {
        "userAgent": "Mozilla/5.0 (Fake) FingerprintTest/1.0",
        "platform": "MacIntel",
        "vendor": "Google Inc.",
        "language": "en-GB",
        "timezone": "America/Chicago",
        "viewport": {"width": 1366, "height": 900},
    }
    data.update(overrides)
    path = tmp_path / "fingerprint.json"
    path.write_text(json.dumps(data))
    return str(path)


def _patch_launch_plumbing(monkeypatch, captured):
    """Stub the parts of launch_browser that would otherwise touch the
    network, spawn a real process, or walk the real /tmp/chrome-agent tree.
    """

    def fake_popen(args, stdout=None, stderr=None, env=None):
        captured["args"] = args
        captured["env"] = env
        return _FakeProcess()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        launcher, "check_cdp_port",
        lambda port: PortStatus(listening=True, browser_version="Chrome/999.0.0.0"),
    )
    monkeypatch.setattr(launcher, "cleanup_sessions", lambda registry_path=None: [])


class TestFingerprintBecomesLaunchFlags:
    def test_fingerprint_file_becomes_chrome_flags(self, monkeypatch, tmp_path):
        captured: dict = {}
        _patch_launch_plumbing(monkeypatch, captured)

        fingerprint_path = _fingerprint_file(tmp_path)

        info = asyncio.run(
            launcher.launch_browser(
                port_override=9333,
                fingerprint=fingerprint_path,
                headless=True,
                working_dir=str(tmp_path),
                registry_path=str(tmp_path / "registry.json"),
                user_data_dir=str(tmp_path / "udd"),
            )
        )

        assert info.port == 9333
        args = captured["args"]
        assert "--user-agent=Mozilla/5.0 (Fake) FingerprintTest/1.0" in args
        assert "--window-size=1366,900" in args
        assert "--lang=en-GB" in args
        assert captured["env"]["TZ"] == "America/Chicago"

    def test_no_fingerprint_means_no_fingerprint_flags(self, monkeypatch, tmp_path):
        captured: dict = {}
        _patch_launch_plumbing(monkeypatch, captured)

        asyncio.run(
            launcher.launch_browser(
                port_override=9334,
                fingerprint=None,
                headless=True,
                working_dir=str(tmp_path),
                registry_path=str(tmp_path / "registry.json"),
                user_data_dir=str(tmp_path / "udd"),
            )
        )

        args = captured["args"]
        assert not any(a.startswith("--user-agent=") for a in args)
        assert not any(a.startswith("--lang=") for a in args)
        assert "TZ" not in (captured["env"] or {})

    def test_missing_fingerprint_file_raises(self, monkeypatch, tmp_path):
        captured: dict = {}
        _patch_launch_plumbing(monkeypatch, captured)

        with pytest.raises(FileNotFoundError):
            asyncio.run(
                launcher.launch_browser(
                    port_override=9335,
                    fingerprint=str(tmp_path / "does-not-exist.json"),
                    headless=True,
                    working_dir=str(tmp_path),
                    registry_path=str(tmp_path / "registry.json"),
                    user_data_dir=str(tmp_path / "udd"),
                )
            )
