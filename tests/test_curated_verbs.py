"""Tests for the curated tool verbs (RFC-01 #50).

These exercise the CLI verbs that front the curated tools: instance resolution
(omittable when one instance runs), dispatch to the *same* implementation the
MCP tool uses, argument parsing, and exit-code mapping. All against an isolated
registry file and test doubles -- no real browser, websocket, or CDP handler
runtime is involved.

Two transports are doubled:

- The **handler transport** is doubled with a fake ``CDPHandler`` installed over
  ``curated.CDPHandler``. It records every ``call_tool`` / ``call_native`` /
  ``run_post_navigation_detection`` invocation, so a verb's dispatch to the
  right tool with the right parsed args is proven directly, and reports
  ``available`` immediately so the one-shot session's connect wait is a no-op.
- The **session transport** (``screenshot`` only) is doubled with a fake
  ``core.cdp_client.CDPClient`` over ``curated.CDPClient`` / ``curated.get_ws_url``,
  mirroring tests/test_passthrough.py.

What is only asserted at the fake-transport level, not against a live browser:
the fake handler returns canned envelopes, so these prove the wiring (which tool,
which args, which envelope -> which exit code), not the tools' own browser
behavior, which is covered by the tools' own suites. The one-shot handler's real
thread/connect lifecycle and the native UID stability across a fresh snapshot are
only exercised end-to-end against a live instance and are unproven here.
"""

from __future__ import annotations

import base64
import json
from typing import ClassVar

import pytest

from browser_tools import cli, curated, lifecycle
from browser_tools.core import registry as core_registry
from browser_tools.lifecycle import LifecycleError
from browser_tools.mcp_response import make_error, make_text
from browser_tools.passthrough import UsageError


@pytest.fixture
def registry_path(tmp_path):
    return str(tmp_path / "registry.json")


def _seed(registry_path: str, entries: dict) -> None:
    core_registry._save_registry(entries, registry_path)


def _entry(port: int = 9222, **extra) -> dict:
    base = {
        "port": port,
        "pid": 2_000_000_000,
        "browser_version": "Chrome/1",
        "user_data_dir": "",
        "launched": "2026-01-01T00:00:00+00:00",
        "pid_start": None,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Fake CDPHandler -- handler-transport double
# ---------------------------------------------------------------------------


class FakeHandler:
    """A stand-in for the one-shot ``CDPHandler`` the handler transport builds.

    Records the tool/native calls a verb dispatches and returns canned
    envelopes, so a test can assert exactly which implementation a verb reached
    and with which parsed arguments.
    """

    #: Set by ``fake_handler`` for the next-constructed instance.
    tool_responses: ClassVar[dict[str, dict]] = {}
    native_responses: ClassVar[dict[str, dict]] = {}
    detection: ClassVar[dict | None] = None
    instances: ClassVar[list[FakeHandler]] = []

    def __init__(self, browser_url, mode="full", stealth=False):
        self.browser_url = browser_url
        self.mode = mode
        self.tool_calls: list[tuple[str, dict]] = []
        self.native_calls: list[tuple[str, dict]] = []
        self.detection_runs = 0
        self.stopped = False
        FakeHandler.instances.append(self)

    @property
    def available(self) -> bool:
        return True

    def run(self) -> None:  # runs on a background thread; returns immediately
        return None

    def stop(self) -> None:
        self.stopped = True

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.tool_calls.append((name, arguments))
        return FakeHandler.tool_responses.get(name, make_text(f"{name}-ok"))

    def call_native(self, name: str, arguments: dict) -> dict:
        self.native_calls.append((name, arguments))
        return FakeHandler.native_responses.get(name, make_text(f"{name}-ok"))

    def run_post_navigation_detection(self) -> dict | None:
        self.detection_runs += 1
        return FakeHandler.detection


@pytest.fixture
def fake_handler(monkeypatch):
    """Install the fake handler over ``curated.CDPHandler`` and reset its state."""
    FakeHandler.instances = []
    FakeHandler.tool_responses = {}
    FakeHandler.native_responses = {}
    FakeHandler.detection = None
    monkeypatch.setattr(curated, "CDPHandler", FakeHandler)
    return FakeHandler


# ---------------------------------------------------------------------------
# Instance / port resolution
# ---------------------------------------------------------------------------


class TestInstanceResolution:
    def test_omitted_instance_resolves_single(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry(port=9333)})
        curated.snapshot(instance=None, registry_path=registry_path)
        # Port from the registry reached the handler's browser_url.
        assert fake_handler.instances[0].browser_url == "http://127.0.0.1:9333"

    def test_omitted_instance_multiple_is_lifecycle_error(self, registry_path, fake_handler):
        _seed(registry_path, {"a-01": _entry(port=9222), "b-01": _entry(port=9223)})
        with pytest.raises(LifecycleError):
            curated.snapshot(instance=None, registry_path=registry_path)

    def test_unknown_instance_is_lifecycle_error(self, registry_path, fake_handler):
        _seed(registry_path, {})
        with pytest.raises(LifecycleError):
            curated.snapshot(instance="ghost", registry_path=registry_path)

    def test_named_instance_resolves_its_port(self, registry_path, fake_handler):
        _seed(registry_path, {"a-01": _entry(port=9222), "b-01": _entry(port=9223)})
        curated.snapshot(instance="b-01", registry_path=registry_path)
        assert fake_handler.instances[0].browser_url == "http://127.0.0.1:9223"

    def test_connect_timeout_is_lifecycle_error(self, registry_path, monkeypatch):
        _seed(registry_path, {"only-01": _entry()})

        class NeverReady(FakeHandler):
            @property
            def available(self) -> bool:
                return False

        monkeypatch.setattr(curated, "CDPHandler", NeverReady)
        monkeypatch.setattr(curated, "HANDLER_CONNECT_TIMEOUT_SECONDS", 0.05)
        with pytest.raises(LifecycleError):
            curated.snapshot(instance="only-01", registry_path=registry_path)


# ---------------------------------------------------------------------------
# Dispatch: native snapshot / interaction
# ---------------------------------------------------------------------------


class TestNativeDispatch:
    def test_snapshot_dispatches_to_take_snapshot(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        FakeHandler.native_responses = {"take_snapshot": make_text("[uid=1-1] RootWebArea")}
        out = curated.snapshot(instance=None, registry_path=registry_path)
        assert fake_handler.instances[0].native_calls == [("take_snapshot", {})]
        assert out == {"snapshot": "[uid=1-1] RootWebArea"}

    def test_click_snapshots_first_then_clicks(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        curated.click(instance=None, uid="1-5", registry_path=registry_path)
        assert fake_handler.instances[0].native_calls == [
            ("take_snapshot", {}),
            ("click", {"uid": "1-5"}),
        ]

    def test_fill_snapshots_first_then_fills(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        curated.fill(instance=None, uid="1-5", text="hello", registry_path=registry_path)
        assert fake_handler.instances[0].native_calls == [
            ("take_snapshot", {}),
            ("fill", {"uid": "1-5", "value": "hello"}),
        ]

    def test_native_tool_error_is_lifecycle_error(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        FakeHandler.native_responses = {"click": make_error("cannot interact with uid '1-5'")}
        with pytest.raises(LifecycleError) as exc:
            curated.click(instance=None, uid="1-5", registry_path=registry_path)
        assert "1-5" in str(exc.value)


# ---------------------------------------------------------------------------
# Dispatch: waits, detect, frames, storage, screencast
# ---------------------------------------------------------------------------


class TestHandlerToolDispatch:
    def test_wait_idle_passes_parsed_args(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        curated.wait_idle(instance=None, timeout_ms=8000, idle_ms=250, registry_path=registry_path)
        assert fake_handler.instances[0].tool_calls == [
            ("wait_idle", {"timeout_ms": 8000, "idle_ms": 250})
        ]

    def test_wait_stable_passes_parsed_args(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        curated.wait_stable(instance=None, timeout_ms=7000, stable_ms=200, registry_path=registry_path)
        assert fake_handler.instances[0].tool_calls == [
            ("wait_stable", {"timeout_ms": 7000, "stable_ms": 200})
        ]

    def test_detect_runs_post_navigation_detection_and_formats(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        FakeHandler.detection = {
            "detections": [{"type": "cloudflare_challenge", "confidence": "high", "signal": "s", "details": "d"}],
            "auto_retried": True,
            "retries_used": 2,
        }
        out = curated.detect(instance=None, registry_path=registry_path)
        assert fake_handler.instances[0].detection_runs == 1
        assert out["auto_retried"] is True
        assert out["retries_used"] == 2
        assert out["detections"][0]["type"] == "cloudflare_challenge"
        assert "Interstitial detected" in out["report"]

    def test_detect_none_result_is_lifecycle_error(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        FakeHandler.detection = None
        with pytest.raises(LifecycleError):
            curated.detect(instance=None, registry_path=registry_path)

    def test_detect_no_detections_report_is_none(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        FakeHandler.detection = {"detections": [], "auto_retried": False, "retries_used": 0}
        out = curated.detect(instance=None, registry_path=registry_path)
        assert out["report"] is None

    def test_frames_list_dispatches(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        curated.frames_list(instance=None, registry_path=registry_path)
        assert fake_handler.instances[0].tool_calls == [("list_frames", {})]

    def test_frames_select_passes_pattern(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        curated.frames_select(instance=None, pattern="checkout", registry_path=registry_path)
        assert fake_handler.instances[0].tool_calls == [
            ("select_frame", {"url_pattern": "checkout"})
        ]

    def test_frames_reset_dispatches(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        curated.frames_reset(instance=None, registry_path=registry_path)
        assert fake_handler.instances[0].tool_calls == [("reset_frame", {})]

    def test_storage_get_without_key_reads_directly(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        curated.storage_get(instance=None, key=None, registry_path=registry_path)
        assert fake_handler.instances[0].tool_calls == [("get_frame_storage", {})]

    def test_storage_get_with_key_selects_frame_first(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        curated.storage_get(instance=None, key="pay.example", registry_path=registry_path)
        assert fake_handler.instances[0].tool_calls == [
            ("select_frame", {"url_pattern": "pay.example"}),
            ("get_frame_storage", {}),
        ]

    def test_storage_get_no_frame_selected_is_lifecycle_error(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        FakeHandler.tool_responses = {"get_frame_storage": make_error("No frame selected. Use select_frame first.")}
        with pytest.raises(LifecycleError):
            curated.storage_get(instance=None, key=None, registry_path=registry_path)

    def test_screencast_start_passes_opts(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        curated.screencast_start(instance=None, fmt="png", max_frames=100, registry_path=registry_path)
        assert fake_handler.instances[0].tool_calls == [
            ("screencast_start", {"format": "png", "max_frames": 100})
        ]

    def test_screencast_stop_passes_dir(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        curated.screencast_stop(instance=None, out_dir="/tmp/cast", registry_path=registry_path)
        assert fake_handler.instances[0].tool_calls == [("screencast_stop", {"dir": "/tmp/cast"})]


# ---------------------------------------------------------------------------
# Screenshot -- session transport double (mirrors test_passthrough)
# ---------------------------------------------------------------------------


_ONE_PX_PNG = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
).decode()


def _make_fake_cdp_client_cls(shot_data="Zm9v", targets=None):
    targets = targets if targets is not None else [
        {"targetId": "T1", "type": "page", "url": "https://example.com"}
    ]
    calls: list[tuple[str, dict | None, str | None]] = []

    class FakeCDPClient:
        def __init__(self, ws_url):
            self.ws_url = ws_url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, method, params=None, session_id=None):
            calls.append((method, params, session_id))
            if method == "Target.getTargets":
                return {"targetInfos": targets}
            if method == "Target.attachToTarget":
                return {"sessionId": "S1"}
            if method == "Target.detachFromTarget":
                return {}
            if method == "Page.captureScreenshot":
                return {"data": shot_data}
            return {}

    return FakeCDPClient, calls


@pytest.fixture
def fake_screenshot_transport(monkeypatch):
    """Install a fake CDPClient/get_ws_url pair over the one-shot seam.

    The connect/getTargets/attach/detach protocol itself is proven once,
    directly, in ``tests/test_one_shot.py``; this double exists so
    ``screenshot``'s own capture-and-retry body can be exercised end to end
    without a real browser.
    """

    def _install(shot_data="Zm9v", targets=None):
        fake_cls, calls = _make_fake_cdp_client_cls(shot_data=shot_data, targets=targets)
        monkeypatch.setattr("browser_tools.one_shot.CDPClient", fake_cls)
        monkeypatch.setattr(
            "browser_tools.one_shot.get_ws_url", lambda **kw: "ws://fake/browser"
        )
        return calls

    return _install


class TestScreenshot:
    def test_capture_reaches_page_capture_screenshot(self, registry_path, fake_screenshot_transport):
        _seed(registry_path, {"only-01": _entry()})
        calls = fake_screenshot_transport(shot_data="Zm9v")
        out = curated.screenshot(instance=None, registry_path=registry_path)
        # The capture reached Page.captureScreenshot over the isolated
        # session the one-shot seam opened (the seam's own connect/attach/
        # detach protocol is proven once in tests/test_one_shot.py).
        assert any(c[0] == "Page.captureScreenshot" for c in calls)
        assert out["data"] == "data:image/png;base64,Zm9v"

    def test_path_writes_file(self, registry_path, fake_screenshot_transport, tmp_path):
        _seed(registry_path, {"only-01": _entry()})
        fake_screenshot_transport(shot_data=_ONE_PX_PNG)
        dest = tmp_path / "shot.png"
        out = curated.screenshot(instance=None, path=str(dest), registry_path=registry_path)
        assert out["saved"] == str(dest.resolve())
        assert dest.read_bytes() == base64.b64decode(_ONE_PX_PNG)

    def test_target_flag_selects_target(self, registry_path, fake_screenshot_transport):
        _seed(registry_path, {"only-01": _entry()})
        targets = [
            {"targetId": "AAAA1111", "type": "page", "url": "https://a.example"},
            {"targetId": "BBBB2222", "type": "page", "url": "https://b.example"},
        ]
        calls = fake_screenshot_transport(targets=targets)
        curated.screenshot(instance=None, target="BBBB2222", registry_path=registry_path)
        attach_call = calls[1]
        assert attach_call[0] == "Target.attachToTarget"
        assert attach_call[1] == {"targetId": "BBBB2222", "flatten": True}

    def test_both_target_and_url_is_usage_error(self, registry_path, fake_screenshot_transport):
        _seed(registry_path, {"only-01": _entry()})
        fake_screenshot_transport()
        with pytest.raises(UsageError):
            curated.screenshot(instance=None, target="1", url="x", registry_path=registry_path)

    def test_blank_capture_is_retried(self, registry_path, fake_screenshot_transport, monkeypatch):
        _seed(registry_path, {"only-01": _entry()})
        calls = fake_screenshot_transport(shot_data="Zm9v")
        # Force the blank-frame guard to see the first capture as blank.
        seen: list[str] = []

        def fake_blank(data: str) -> bool:
            seen.append(data)
            return len(seen) == 1

        monkeypatch.setattr(curated, "screenshot_looks_blank", fake_blank)
        monkeypatch.setattr(curated, "SCREENSHOT_BLANK_RETRY_DELAY_SECONDS", 0.0)
        monkeypatch.setattr(curated, "SCREENSHOT_BLANK_MAX_RETRIES", 1)
        curated.screenshot(instance=None, registry_path=registry_path)
        capture_calls = [c for c in calls if c[0] == "Page.captureScreenshot"]
        assert len(capture_calls) == 2


# ---------------------------------------------------------------------------
# CLI front: end-to-end dispatch and exit codes
# ---------------------------------------------------------------------------


class TestCliFront:
    @pytest.fixture(autouse=True)
    def _isolate_registry(self, monkeypatch, tmp_path):
        self._registry_path = str(tmp_path / "registry.json")
        monkeypatch.setenv(lifecycle.REGISTRY_ENV_VAR, self._registry_path)

    def test_snapshot_prints_json_exit_ok(self, capsys, fake_handler):
        _seed(self._registry_path, {"only-01": _entry()})
        FakeHandler.native_responses = {"take_snapshot": make_text("TREE")}
        rc = cli.main(["snapshot"])
        assert rc == cli.EXIT_OK
        assert json.loads(capsys.readouterr().out) == {"snapshot": "TREE"}

    def test_click_requires_uid_exit_usage(self, capsys, fake_handler):
        _seed(self._registry_path, {"only-01": _entry()})
        rc = cli.main(["click"])
        assert rc == cli.EXIT_USAGE
        assert "error:" in capsys.readouterr().err

    def test_click_with_uid_dispatches(self, capsys, fake_handler):
        _seed(self._registry_path, {"only-01": _entry()})
        rc = cli.main(["click", "--uid", "1-5"])
        assert rc == cli.EXIT_OK
        assert fake_handler.instances[0].native_calls[-1] == ("click", {"uid": "1-5"})

    def test_fill_requires_text_exit_usage(self, capsys, fake_handler):
        _seed(self._registry_path, {"only-01": _entry()})
        rc = cli.main(["fill", "--uid", "1-5"])
        assert rc == cli.EXIT_USAGE

    def test_fill_dispatches(self, capsys, fake_handler):
        _seed(self._registry_path, {"only-01": _entry()})
        rc = cli.main(["fill", "--uid", "1-5", "--text", "hi"])
        assert rc == cli.EXIT_OK
        assert fake_handler.instances[0].native_calls[-1] == ("fill", {"uid": "1-5", "value": "hi"})

    def test_tool_error_exits_operational(self, capsys, fake_handler):
        _seed(self._registry_path, {"only-01": _entry()})
        FakeHandler.tool_responses = {"wait_idle": make_error("wait_idle timed out")}
        rc = cli.main(["wait-idle"])
        assert rc == cli.EXIT_OPERATIONAL
        assert "error:" in capsys.readouterr().err

    def test_instance_prefixed_form(self, capsys, fake_handler):
        _seed(self._registry_path, {"site-01": _entry(port=9500)})
        rc = cli.main(["snapshot", "site-01"])
        assert rc == cli.EXIT_OK
        assert fake_handler.instances[0].browser_url == "http://127.0.0.1:9500"

    def test_frames_select_via_cli(self, capsys, fake_handler):
        _seed(self._registry_path, {"only-01": _entry()})
        rc = cli.main(["frames", "select", "checkout"])
        assert rc == cli.EXIT_OK
        assert fake_handler.instances[0].tool_calls == [("select_frame", {"url_pattern": "checkout"})]

    def test_frames_without_action_exits_usage(self, capsys, fake_handler):
        _seed(self._registry_path, {"only-01": _entry()})
        rc = cli.main(["frames"])
        assert rc == cli.EXIT_USAGE

    def test_storage_get_key_via_cli(self, capsys, fake_handler):
        _seed(self._registry_path, {"only-01": _entry()})
        rc = cli.main(["storage", "get", "--key", "pay"])
        assert rc == cli.EXIT_OK
        assert fake_handler.instances[0].tool_calls == [
            ("select_frame", {"url_pattern": "pay"}),
            ("get_frame_storage", {}),
        ]

    def test_storage_without_action_exits_usage(self, capsys, fake_handler):
        _seed(self._registry_path, {"only-01": _entry()})
        rc = cli.main(["storage"])
        assert rc == cli.EXIT_USAGE

    def test_screencast_stop_requires_dir(self, capsys, fake_handler):
        _seed(self._registry_path, {"only-01": _entry()})
        rc = cli.main(["screencast", "stop"])
        assert rc == cli.EXIT_USAGE

    def test_screencast_start_via_cli(self, capsys, fake_handler):
        _seed(self._registry_path, {"only-01": _entry()})
        rc = cli.main(["screencast", "start", "--format", "png"])
        assert rc == cli.EXIT_OK
        assert fake_handler.instances[0].tool_calls[0][0] == "screencast_start"
        assert fake_handler.instances[0].tool_calls[0][1]["format"] == "png"

    def test_wait_stable_defaults_via_cli(self, capsys, fake_handler):
        _seed(self._registry_path, {"only-01": _entry()})
        rc = cli.main(["wait-stable"])
        assert rc == cli.EXIT_OK
        assert fake_handler.instances[0].tool_calls == [
            ("wait_stable", {"timeout_ms": curated.DEFAULT_WAIT_TIMEOUT_MS, "stable_ms": curated.DEFAULT_STABLE_MS})
        ]

    def test_detect_via_cli(self, capsys, fake_handler):
        _seed(self._registry_path, {"only-01": _entry()})
        FakeHandler.detection = {"detections": [], "auto_retried": False, "retries_used": 0}
        rc = cli.main(["detect"])
        assert rc == cli.EXIT_OK
        assert fake_handler.instances[0].detection_runs == 1

    def test_screenshot_via_cli(self, capsys, fake_screenshot_transport):
        _seed(self._registry_path, {"only-01": _entry()})
        fake_screenshot_transport(shot_data="Zm9v")
        rc = cli.main(["screenshot"])
        assert rc == cli.EXIT_OK
        assert json.loads(capsys.readouterr().out) == {"data": "data:image/png;base64,Zm9v"}

    def test_curated_verb_not_treated_as_passthrough_head(self, capsys, fake_handler):
        # A curated verb is a known verb, so main() never routes it to raw
        # passthrough dispatch even though no instance token precedes it.
        _seed(self._registry_path, {"only-01": _entry()})
        FakeHandler.native_responses = {"take_snapshot": make_text("TREE")}
        rc = cli.main(["snapshot"])
        assert rc == cli.EXIT_OK
        assert fake_handler.instances[0].native_calls == [("take_snapshot", {})]
