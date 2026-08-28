"""Window marking wired through the CLI front (RFC-01 #48, "Window marking").

RFC-01: windows launched by the tool carry the supervisor's marker so a human
can tell agent-controlled windows from their own. The marker is the tab title
prefix (instance name); nothing is drawn inside the page. Marking is on by
default and disabled per launch with ``--no-window-border``.

Two seams are pinned here, so the flag is proven end to end without launching a
real browser:

1. **Threading** -- ``cli.main(["launch"])`` reaches the launcher's
   ``spawn_supervisor`` with ``draw_border=True`` by default and
   ``draw_border=False`` under ``--no-window-border``. This covers the whole
   path ``cli.py`` -> ``lifecycle.launch(window_border=...)`` ->
   ``core.launcher.launch_browser`` -> ``spawn_supervisor``.

2. **Marker build** -- the supervisor actually builds a marker script when
   ``draw_border`` is set and builds none when it is not, matching the ticket's
   "default launch marks the window (marker script built), ``--no-window-border``
   suppresses it."

No real browser: ``subprocess.Popen``, ``check_cdp_port``, ``cleanup_sessions``,
the desktop move, and ``spawn_supervisor`` are monkeypatched, following the
existing test-double pattern in ``test_core_launcher.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from browser_tools import cli
from browser_tools.core import launcher, supervisor
from browser_tools.core.connection import PortStatus


@dataclass
class _FakeProcess:
    pid: int = 525252
    returncode: int | None = None

    def poll(self):
        return None


def _patch_headed_launch(monkeypatch, captured: dict) -> None:
    """Stub everything a headed launch would otherwise really do."""

    def fake_popen(args, stdout=None, stderr=None, env=None):
        return _FakeProcess()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        launcher,
        "check_cdp_port",
        lambda port: PortStatus(listening=True, browser_version="Chrome/999.0.0.0"),
    )
    monkeypatch.setattr(launcher, "cleanup_sessions", lambda registry_path=None: [])

    async def fake_move(*, pid):  # headed path calls this; no real X11 here
        return None

    monkeypatch.setattr(launcher, "_move_to_launching_desktop", fake_move)

    def fake_spawn(*, port, name, registry_path, draw_border):
        captured["draw_border"] = draw_border
        captured["name"] = name
        return None

    # launch_browser does ``from .supervisor import spawn_supervisor`` at call
    # time, so the attribute must be patched on the supervisor module itself.
    monkeypatch.setattr(supervisor, "spawn_supervisor", fake_spawn)


class TestFlagThreadsThroughToSupervisor:
    def test_default_launch_marks_the_window(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", str(tmp_path / "registry.json"))
        captured: dict = {}
        _patch_headed_launch(monkeypatch, captured)

        rc = cli.main(["launch"])

        assert rc == 0
        assert captured["draw_border"] is True

    def test_no_window_border_suppresses_the_marking(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", str(tmp_path / "registry.json"))
        captured: dict = {}
        _patch_headed_launch(monkeypatch, captured)

        rc = cli.main(["launch", "--no-window-border"])

        assert rc == 0
        assert captured["draw_border"] is False


class TestSupervisorBuildsOverlayWhenBorderOn:
    """The supervisor builds the overlay script exactly when draw_border is set."""

    def _run_supervisor_once(self, monkeypatch, *, draw_border: bool) -> dict:
        captured: dict = {}

        async def fake_supervise(*, port, draw_border, source):
            captured["draw_border"] = draw_border
            captured["source"] = source
            # Return without looping; run_supervisor then checks _browser_gone.

        async def fake_gone(port):
            return True  # browser "closed" -> run_supervisor retires and returns

        monkeypatch.setattr(supervisor, "_supervise_connection", fake_supervise)
        monkeypatch.setattr(supervisor, "_browser_gone", fake_gone)
        # run_supervisor does ``from .registry import deregister`` at call time.
        monkeypatch.setattr(
            "browser_tools.core.registry.deregister",
            lambda **kwargs: None,
        )

        asyncio.run(
            supervisor.run_supervisor(
                port=9333,
                name="demo-instance",
                registry_path=None,
                draw_border=draw_border,
            )
        )
        return captured

    def test_border_on_builds_marker_carrying_the_name(self, monkeypatch):
        captured = self._run_supervisor_once(monkeypatch, draw_border=True)
        assert captured["draw_border"] is True
        assert captured["source"] is not None
        # The marker is the title prefix carrying the instance name.
        assert "demo-instance" in captured["source"]
        assert "document.title = PREFIX + t" in captured["source"]

    def test_marker_draws_nothing_inside_the_page(self, monkeypatch):
        """Anything drawn inside the viewport sits on top of page content. The
        upstream border and corner badge both did, and got in the way of seeing
        the page; the marker must not create any element at all."""
        captured = self._run_supervisor_once(monkeypatch, draw_border=True)
        source = captured["source"]
        assert "createElement" not in source
        assert "appendChild" not in source
        assert "position:fixed" not in source
        assert "border:" not in source

    def test_marker_runs_in_the_top_frame_only(self, monkeypatch):
        """Page.addScriptToEvaluateOnNewDocument runs the script in every frame,
        and only the top document's title is the tab title; iframes must keep
        their own titles. The guard is the first statement so nothing runs
        before it, and it is wrapped so a sandboxed frame that throws on
        ``window.top`` access is treated as a non-top frame."""
        captured = self._run_supervisor_once(monkeypatch, draw_border=True)
        source = captured["source"]
        guard = "try { if (window.self !== window.top) return; } catch(e) { return; }"
        assert guard in source
        assert source.index(guard) < source.index("var NAME=")

    def test_border_off_builds_no_marker(self, monkeypatch):
        captured = self._run_supervisor_once(monkeypatch, draw_border=False)
        assert captured["draw_border"] is False
        assert captured["source"] is None
