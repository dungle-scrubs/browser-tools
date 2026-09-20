"""Window marking wired through the CLI front (RFC-01 #48, "Window marking").

RFC-01: windows launched by the tool carry the supervisor's marker so a human
can tell agent-controlled windows from their own. The marker is the tab title
prefix (instance name) plus a colored border and corner badge drawn over the
page. Marking is on by default and disabled per launch with
``--no-window-border``. The border alone follows the persistent
``window_border`` user setting (``bt window-border on|off``), which running
supervisors re-read and apply to their open tabs.

Two seams are pinned here, so the flag is proven end to end without launching a
real browser:

1. **Threading** -- ``cli.main(["launch"])`` reaches the launcher's
   ``spawn_supervisor`` with ``draw_border=True`` by default and
   ``draw_border=False`` under ``--no-window-border``. This covers the whole
   path ``cli.py`` -> ``lifecycle.launch(window_border=...)`` ->
   ``core.launcher.launch_browser`` -> ``spawn_supervisor``.

2. **Marker build** -- the supervisor actually builds the marker scripts when
   ``draw_border`` is set and builds none when it is not, matching the ticket's
   "default launch marks the window (marker script built), ``--no-window-border``
   suppresses it."

3. **Border setting** -- the setting file round-trips, the CLI verb writes it,
   and a supervisor adds or removes the border on its marked tabs when the
   setting changes (against a fake CDP client).

No real browser: ``subprocess.Popen``, ``check_cdp_port``, ``cleanup_sessions``,
the desktop move, and ``spawn_supervisor`` are monkeypatched, following the
existing test-double pattern in ``test_core_launcher.py``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from browser_tools import cli, user_settings
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
    async def fake_check_cdp_port_async(*, port):
        return PortStatus(listening=True, browser_version="Chrome/999.0.0.0")

    monkeypatch.setattr(launcher, "check_cdp_port_async", fake_check_cdp_port_async)
    monkeypatch.setattr(launcher, "cleanup_sessions", lambda registry_path=None: [])

    async def fake_move(*, pid):  # headed path calls this; no real X11 here
        return None

    monkeypatch.setattr(launcher, "_move_to_launching_desktop", fake_move)

    async def fake_open_first_window(*, port):  # no real browser to open it in
        captured["first_window_port"] = port

    monkeypatch.setattr(launcher, "_open_first_window", fake_open_first_window)

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
    """The supervisor builds the marker scripts exactly when draw_border is set."""

    def _run_supervisor_once(self, monkeypatch, *, draw_border: bool) -> dict:
        captured: dict = {}

        async def fake_supervise(*, port, scripts, border=None, heartbeat=None):
            captured["scripts"] = scripts
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
                # No watchdog thread for a single scripted pass of the loop.
                watchdog=False,
            )
        )
        return captured

    def test_border_on_builds_marker_carrying_the_name(self, monkeypatch):
        scripts = self._run_supervisor_once(monkeypatch, draw_border=True)["scripts"]
        assert scripts is not None
        assert "demo-instance" in scripts.title
        assert "document.title = PREFIX + t" in scripts.title
        assert "demo-instance" in scripts.border

    def test_title_script_draws_nothing_inside_the_page(self, monkeypatch):
        """The title prefix is the part that is always on, so it must never
        cover page content: it creates no element at all."""
        title = self._run_supervisor_once(monkeypatch, draw_border=True)["scripts"].title
        assert "createElement" not in title
        assert "appendChild" not in title
        assert "position:fixed" not in title

    def test_border_script_draws_a_click_through_border_and_badge(self, monkeypatch):
        border = self._run_supervisor_once(monkeypatch, draw_border=True)["scripts"].border
        assert "border:6px solid" in border
        assert "pointer-events:none" in border
        assert "attachShadow({mode:'closed'})" in border
        # The badge yields to the pointer so what it covers can be seen.
        assert f"var YIELD_PX = {supervisor.BADGE_YIELD_PX};" in border

    def test_both_scripts_run_in_the_top_frame_only(self, monkeypatch):
        """Page.addScriptToEvaluateOnNewDocument runs a script in every frame.
        Only the top document is the window: iframes keep their own titles and
        draw no nested border. The guard is the first statement so nothing
        runs before it, and it is wrapped so a sandboxed frame that throws on
        ``window.top`` access is treated as a non-top frame."""
        scripts = self._run_supervisor_once(monkeypatch, draw_border=True)["scripts"]
        guard = "try { if (window.self !== window.top) return; } catch(e) { return; }"
        for source in (scripts.title, scripts.border):
            assert guard in source
            assert source.index(guard) < source.index("var NAME=")

    def test_removal_stops_the_border_script_and_removes_its_host(self, monkeypatch):
        scripts = self._run_supervisor_once(monkeypatch, draw_border=True)["scripts"]
        ids = supervisor.BorderIds.for_instance("demo-instance")
        assert f"OFF={json.dumps(ids.off_event)}" in scripts.border
        assert "document.addEventListener(OFF, stop)" in scripts.border
        assert json.dumps(ids.off_event) in scripts.border_removal
        assert json.dumps(ids.host_id) in scripts.border_removal

    def test_border_ids_are_stable_per_instance(self):
        """A restarted supervisor must find the border an earlier one drew."""
        assert supervisor.BorderIds.for_instance("a-01") == supervisor.BorderIds.for_instance("a-01")
        assert supervisor.BorderIds.for_instance("a-01") != supervisor.BorderIds.for_instance("b-01")

    def test_border_off_builds_no_marker(self, monkeypatch):
        assert self._run_supervisor_once(monkeypatch, draw_border=False)["scripts"] is None


class _FakeCDP:
    """Records CDP calls; answers addScriptToEvaluateOnNewDocument with ids.

    ``fail`` names methods that raise, so a partial failure can be driven: a
    tab that navigates or closes between two calls of one change.
    """

    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[tuple[str, dict | None, str | None]] = []
        self.fail = fail or set()
        self._next = 0

    async def send(self, method, params=None, session_id=None):
        self.calls.append((method, params, session_id))
        if method in self.fail:
            raise RuntimeError(f"{method} failed")
        if method == "Page.addScriptToEvaluateOnNewDocument":
            self._next += 1
            return {"identifier": str(self._next)}
        return {}

    def methods(self) -> list[str]:
        return [m for m, _, _ in self.calls]


class TestBorderSettingIsAppliedToMarkedTabs:
    def _scripts(self):
        return supervisor.MarkerScripts.for_instance("demo-instance")

    def test_new_tab_gets_title_and_border_while_setting_is_on(self):
        cdp, scripts = _FakeCDP(), self._scripts()
        mark = supervisor.TabMark(session_id="S1")
        border = supervisor.BorderSetting(lambda: True)
        asyncio.run(supervisor._setup_session(cdp, "S1", scripts, mark, border))

        added = [c[1]["source"] for c in cdp.calls if c[0] == "Page.addScriptToEvaluateOnNewDocument"]
        assert added == [scripts.title, scripts.border]
        # The resume comes before any marking work.
        assert cdp.calls[0][0] == "Runtime.runIfWaitingForDebugger"
        assert mark.border_script == "2"

    def test_new_tab_gets_title_only_while_setting_is_off(self):
        cdp, scripts = _FakeCDP(), self._scripts()
        mark = supervisor.TabMark(session_id="S1")
        border = supervisor.BorderSetting(lambda: False)
        asyncio.run(supervisor._setup_session(cdp, "S1", scripts, mark, border))

        added = [c[1]["source"] for c in cdp.calls if c[0] == "Page.addScriptToEvaluateOnNewDocument"]
        assert added == [scripts.title]
        assert mark.border_script is None

    def test_turning_off_removes_the_registered_script_and_the_drawn_border(self):
        cdp, scripts = _FakeCDP(), self._scripts()
        mark = supervisor.TabMark(session_id="S1", border_script="7")
        asyncio.run(supervisor._set_tab_border(cdp, mark, False, scripts))

        assert cdp.calls == [
            ("Page.removeScriptToEvaluateOnNewDocument", {"identifier": "7"}, "S1"),
            ("Runtime.evaluate", {"expression": scripts.border_removal}, "S1"),
        ]
        assert mark.border_script is None

    def test_turning_on_again_re_adds_the_border(self):
        cdp, scripts = _FakeCDP(), self._scripts()
        mark = supervisor.TabMark(session_id="S1")
        asyncio.run(supervisor._set_tab_border(cdp, mark, True, scripts))
        asyncio.run(supervisor._set_tab_border(cdp, mark, True, scripts))  # idempotent

        added = [c for c in cdp.calls if c[0] == "Page.addScriptToEvaluateOnNewDocument"]
        assert len(added) == 1
        assert ("Runtime.evaluate", {"expression": scripts.border}, "S1") in cdp.calls

    def test_a_failed_injection_still_keeps_the_registration_id(self):
        """Chrome returns the id on registration; the current document is
        touched after. If that second call fails (the tab navigated), losing
        the id would leave every future document in the tab drawing a border
        that can no longer be turned off."""
        cdp, scripts = _FakeCDP(fail={"Runtime.evaluate"}), self._scripts()
        mark = supervisor.TabMark(session_id="S1")
        asyncio.run(supervisor._set_tab_border(cdp, mark, True, scripts))

        assert mark.border_script == "1"

        cdp.calls.clear()
        cdp.fail = set()
        asyncio.run(supervisor._set_tab_border(cdp, mark, False, scripts))
        assert ("Page.removeScriptToEvaluateOnNewDocument", {"identifier": "1"}, "S1") in cdp.calls
        assert mark.border_script is None

    def test_a_failed_removal_keeps_the_id_for_the_next_attempt(self):
        cdp, scripts = _FakeCDP(fail={"Page.removeScriptToEvaluateOnNewDocument"}), self._scripts()
        mark = supervisor.TabMark(session_id="S1", border_script="7")
        asyncio.run(supervisor._set_tab_border(cdp, mark, False, scripts))

        assert mark.border_script == "7"

    def test_turning_off_clears_a_border_this_supervisor_did_not_register(self):
        """After a reconnect the tab's mark is fresh and holds no id, while the
        page may still carry a border drawn before the drop. The removal
        expression runs anyway; it is idempotent and keyed to the instance."""
        cdp, scripts = _FakeCDP(), self._scripts()
        mark = supervisor.TabMark(session_id="S1")
        asyncio.run(supervisor._set_tab_border(cdp, mark, False, scripts))

        assert ("Runtime.evaluate", {"expression": scripts.border_removal}, "S1") in cdp.calls
        assert "Page.removeScriptToEvaluateOnNewDocument" not in cdp.methods()

    def test_setting_refresh_reports_changes_and_survives_read_errors(self):
        values = iter([True, True, False, RuntimeError("unreadable")])

        def read():
            value = next(values)
            if isinstance(value, Exception):
                raise value
            return value

        border = supervisor.BorderSetting(read)
        assert border.on is True
        assert border.refresh() is False
        assert border.refresh() is True and border.on is False
        # A failed read keeps the last known value instead of flipping.
        assert border.refresh() is False and border.on is False


class TestWindowBorderSetting:
    def test_default_is_on(self, tmp_path):
        assert user_settings.window_border_enabled(tmp_path / "settings.json") is True

    def test_off_persists_and_keeps_other_settings(self, tmp_path):
        path = tmp_path / "browser-tools" / "settings.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"other": 1}))
        user_settings.set_window_border(False, path)
        assert user_settings.window_border_enabled(path) is False
        assert json.loads(path.read_text()) == {"other": 1, "window_border": "off"}
        user_settings.set_window_border(True, path)
        assert user_settings.window_border_enabled(path) is True

    def test_malformed_file_reads_as_default(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("{not json")
        assert user_settings.window_border_enabled(path) is True

    def test_path_follows_xdg_config_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert user_settings.settings_path() == tmp_path / "browser-tools" / "settings.json"

    def test_cli_verb_sets_and_reports_the_setting(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert cli.main(["window-border", "off"]) == 0
        assert json.loads(capsys.readouterr().out)["window_border"] == "off"
        assert cli.main(["window-border"]) == 0
        assert json.loads(capsys.readouterr().out)["window_border"] == "off"
        assert user_settings.window_border_enabled() is False
