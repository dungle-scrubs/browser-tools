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

    def fake_popen(args, stdout=None, stderr=None, env=None, **kwargs):
        # First call only: launch_browser's first spawn is Chrome; anything
        # after it (utils.process_start_time's `ps` probe on platforms without
        # /proc) must not clobber what the assertions read. On Python 3.14
        # subprocess.run forwards text=True to Popen, so kwargs must be
        # accepted for the probe to arrive here at all.
        captured.setdefault("args", args)
        captured.setdefault("env", env)
        captured.setdefault("popen_kwargs", kwargs)
        return _FakeProcess()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    async def fake_check_cdp_port_async(*, port):
        return PortStatus(listening=True, browser_version="Chrome/999.0.0.0")

    monkeypatch.setattr(launcher, "check_cdp_port_async", fake_check_cdp_port_async)
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


class TestHeadedLaunchLeavesFocusAlone:
    """A headed launch must not take the user's focus.

    Measured on macOS: Chrome started normally opens a window and becomes the
    active app. Started with ``--no-startup-window`` it opens nothing, and a
    window created over CDP with ``background`` set leaves focus where it was.
    """

    def _launch_headed(self, monkeypatch, tmp_path, captured, open_first_window, extra_args=None):
        _patch_launch_plumbing(monkeypatch, captured)
        monkeypatch.setattr(launcher, "_open_first_window", open_first_window)

        async def fake_move(*, pid):
            return None

        monkeypatch.setattr(launcher, "_move_to_launching_desktop", fake_move)
        # launch_browser reads the returned process's pid to record the
        # supervisor on the registry entry, so the double must carry one.
        monkeypatch.setattr(
            "browser_tools.core.supervisor.spawn_supervisor",
            lambda **kwargs: _FakeProcess(),
        )
        return asyncio.run(
            launcher.launch_browser(
                port_override=9335,
                headless=False,
                working_dir=str(tmp_path),
                registry_path=str(tmp_path / "registry.json"),
                user_data_dir=str(tmp_path / "udd"),
                extra_args=extra_args,
            )
        )

    def test_headed_launch_starts_windowless_and_opens_a_background_window(
        self, monkeypatch, tmp_path
    ):
        captured: dict = {}

        async def fake_open(**kwargs):
            captured["opened"] = kwargs

        self._launch_headed(monkeypatch, tmp_path, captured, fake_open)

        assert "--no-startup-window" in captured["args"]
        assert captured["opened"]["port"] == 9335
        assert captured["opened"]["url"] == "about:blank"

    def test_headed_launch_passes_start_new_session(self, monkeypatch, tmp_path):
        """The browser detaches from the launching terminal's controlling tty
        (#177): the same session detachment the supervisor gets, so closing
        the terminal pane does not take the browser down."""
        captured: dict = {}

        async def fake_open(**kwargs):
            pass

        self._launch_headed(monkeypatch, tmp_path, captured, fake_open)

        assert captured["popen_kwargs"].get("start_new_session") is True

    def test_headed_url_opens_in_the_first_window_not_chromes_argv(
        self, monkeypatch, tmp_path
    ):
        """A URL after '--' opens as the first page (#179).

        --no-startup-window suppresses Chrome's own startup URLs, so the URL
        must not stay in Chrome's argv -- it reaches the browser through
        _open_first_window, which also keeps the launch from stealing focus.
        """
        captured: dict = {}

        async def fake_open(**kwargs):
            captured["opened"] = kwargs

        self._launch_headed(
            monkeypatch, tmp_path, captured, fake_open,
            extra_args=["http://127.0.0.1:17471/"],
        )

        assert "http://127.0.0.1:17471/" not in captured["args"]
        assert captured["opened"]["url"] == "http://127.0.0.1:17471/"
        assert captured["opened"]["extra_urls"] == []

    def test_headed_flags_pass_through_and_multiple_urls_open_as_tabs(
        self, monkeypatch, tmp_path
    ):
        captured: dict = {}

        async def fake_open(**kwargs):
            captured["opened"] = kwargs

        self._launch_headed(
            monkeypatch, tmp_path, captured, fake_open,
            extra_args=["--proxy-server=host:9", "https://a.example", "https://b.example"],
        )

        assert "--proxy-server=host:9" in captured["args"]
        assert "https://a.example" not in captured["args"]
        assert captured["opened"]["url"] == "https://a.example"
        assert captured["opened"]["extra_urls"] == ["https://b.example"]

    def test_headed_local_file_becomes_a_file_url(self, monkeypatch, tmp_path):
        captured: dict = {}

        async def fake_open(**kwargs):
            captured["opened"] = kwargs

        page = tmp_path / "page.html"
        page.write_text("<h1>hi</h1>")
        self._launch_headed(
            monkeypatch, tmp_path, captured, fake_open,
            extra_args=[str(page)],
        )

        assert captured["opened"]["url"].startswith("file://")
        assert captured["opened"]["url"].endswith("page.html")

    def test_headless_keeps_startup_urls_in_chromes_argv(self, monkeypatch, tmp_path):
        """Headless Chrome opens startup URL arguments natively, so the URL
        stays on Chrome's own command line."""
        captured: dict = {}
        _patch_launch_plumbing(monkeypatch, captured)

        async def must_not_run(**kwargs):
            raise AssertionError("headless launch opened a window")

        monkeypatch.setattr(launcher, "_open_first_window", must_not_run)
        asyncio.run(
            launcher.launch_browser(
                port_override=9336,
                headless=True,
                working_dir=str(tmp_path),
                registry_path=str(tmp_path / "registry.json"),
                user_data_dir=str(tmp_path / "udd"),
                extra_args=["http://127.0.0.1:17471/"],
            )
        )
        assert "--no-startup-window" not in captured["args"]
        assert "http://127.0.0.1:17471/" in captured["args"]

    def test_a_cancelled_launch_kills_the_unregistered_browser(self, monkeypatch, tmp_path):
        """Ctrl-C while the first window is opening cancels this task.
        CancelledError is not an Exception, so a plain ``except Exception``
        would skip the kill and leave Chrome running with no registry entry
        and no supervisor."""
        captured: dict = {}
        killed: list[int] = []

        class _KillableProcess(_FakeProcess):
            def kill(self):
                killed.append(self.pid)

        async def cancelled_open(**kwargs):
            raise asyncio.CancelledError()

        _patch_launch_plumbing(monkeypatch, captured)
        monkeypatch.setattr(
            launcher.subprocess, "Popen", lambda args, **kwargs: _KillableProcess()
        )
        monkeypatch.setattr(launcher, "_open_first_window", cancelled_open)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                launcher.launch_browser(
                    port_override=9338,
                    headless=False,
                    working_dir=str(tmp_path),
                    registry_path=str(tmp_path / "registry.json"),
                    user_data_dir=str(tmp_path / "udd"),
                )
            )
        assert killed == [424242]

    def test_failed_first_window_kills_the_unregistered_browser(self, monkeypatch, tmp_path):
        captured: dict = {}
        killed: list[int] = []

        class _KillableProcess(_FakeProcess):
            def kill(self):
                killed.append(self.pid)

        async def failing_open(**kwargs):
            raise ConnectionError("no browser")

        _patch_launch_plumbing(monkeypatch, captured)
        monkeypatch.setattr(
            launcher.subprocess, "Popen", lambda args, **kwargs: _KillableProcess()
        )
        monkeypatch.setattr(launcher, "_open_first_window", failing_open)

        with pytest.raises(RuntimeError, match="first window did not open"):
            asyncio.run(
                launcher.launch_browser(
                    port_override=9337,
                    headless=False,
                    working_dir=str(tmp_path),
                    registry_path=str(tmp_path / "registry.json"),
                    user_data_dir=str(tmp_path / "udd"),
                )
            )
        assert killed == [424242]
        assert not (tmp_path / "registry.json").exists() or "9337" not in (
            tmp_path / "registry.json"
        ).read_text()
