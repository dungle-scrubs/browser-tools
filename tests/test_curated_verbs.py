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
import time
from typing import ClassVar

import pytest
from doubles import HandlerSurface

from browser_tools import cli, curated, lifecycle
from browser_tools.core import registry as core_registry
from browser_tools.lifecycle import LifecycleError
from browser_tools.mcp_response import extract_text_items, make_error, make_text
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


class FakeHandler(HandlerSurface):
    """A stand-in for the one-shot ``CDPHandler`` the handler transport builds.

    Records the tool/native calls a verb dispatches and returns canned
    envelopes, so a test can assert exactly which implementation a verb reached
    and with which parsed arguments.
    """

    #: Set by ``fake_handler`` for the next-constructed instance.
    tool_responses: ClassVar[dict[str, dict]] = {}
    native_responses: ClassVar[dict[str, dict]] = {}
    detection: ClassVar[dict | None] = None
    #: What ``screencast_frame_count`` reports; the capture loop ends at once
    #: when it is already at or above the cap.
    frames: ClassVar[int] = 0
    instances: ClassVar[list[FakeHandler]] = []

    def __init__(
        self,
        browser_url,
        mode="full",
        stealth=False,
        target_spec=None,
        target_by=None,
        all_frames=False,
    ):
        self.browser_url = browser_url
        self.mode = mode
        self.target_spec = target_spec
        self.target_by = target_by
        self.all_frames = all_frames
        #: Mirrors ``CDPHandler.deadline``: None outside a Step Run.
        self.deadline = None
        self.tool_calls: list[tuple[str, dict]] = []
        self.native_calls: list[tuple[str, dict]] = []
        self.detection_runs = 0
        self.detection_budgets: list[int | None] = []
        self.stopped = False
        #: Mirrors ``CDPHandler.connect_error``: None while the connection is
        #: still coming up, a reason once it has failed.
        self.connect_error: str | None = None
        #: Frames the bounded ``screencast`` capture will see buffered (#99).
        self.frame_count = FakeHandler.frames
        FakeHandler.instances.append(self)

    @property
    def available(self) -> bool:
        return True

    @property
    def screencast_frame_count(self) -> int:
        return self.frame_count

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

    def run_post_navigation_detection(self, max_retries: int | None = None) -> dict | None:
        self.detection_runs += 1
        self.detection_budgets.append(max_retries)
        return FakeHandler.detection

    def page_visibility_state(self) -> str | None:
        return "visible"


@pytest.fixture
def fake_handler(monkeypatch):
    """Install the fake handler over ``curated.CDPHandler`` and reset its state."""
    FakeHandler.instances = []
    FakeHandler.tool_responses = {}
    FakeHandler.native_responses = {}
    FakeHandler.detection = None
    FakeHandler.frames = 0
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
            """Never comes up and never says why, so the deadline decides."""

            @property
            def available(self) -> bool:
                return False

        monkeypatch.setattr(curated, "CDPHandler", NeverReady)
        monkeypatch.setattr(curated, "HANDLER_CONNECT_TIMEOUT_SECONDS", 0.05)
        with pytest.raises(LifecycleError):
            curated.snapshot(instance="only-01", registry_path=registry_path)

    def test_a_known_failure_does_not_wait_out_the_deadline(
        self, monkeypatch, registry_path
    ):
        """The runtime knows why. Reporting it late says less, slower."""
        _seed(registry_path, {"only-01": _entry()})

        class FailsImmediately(FakeHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.connect_error = "Cannot reach browser on port 9222"

            @property
            def available(self) -> bool:
                return False

        monkeypatch.setattr(curated, "CDPHandler", FailsImmediately)
        monkeypatch.setattr(curated, "HANDLER_CONNECT_TIMEOUT_SECONDS", 30.0)
        started = time.monotonic()
        with pytest.raises(LifecycleError) as caught:
            curated.snapshot(instance="only-01", registry_path=registry_path)
        elapsed = time.monotonic() - started

        assert elapsed < 5.0, f"waited {elapsed:.1f}s for an answer it already had"
        assert "Cannot reach browser on port 9222" in str(caught.value)


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

    def test_click_takes_no_snapshot_of_its_own(self, registry_path, fake_handler):
        """The internal snapshot was what made the staleness check unable to
        fire: it was always "current", so a uid from another tree resolved
        against it by ordinal (#96)."""
        _seed(registry_path, {"only-01": _entry()})
        curated.click(instance=None, uid="D0C0FFEE1234-50", registry_path=registry_path)
        assert fake_handler.instances[0].native_calls == [
            ("click", {"uid": "D0C0FFEE1234-50"}),
        ]

    def test_fill_takes_no_snapshot_of_its_own(self, registry_path, fake_handler):
        _seed(registry_path, {"only-01": _entry()})
        curated.fill(
            instance=None, uid="D0C0FFEE1234-40", text="hello", registry_path=registry_path
        )
        assert fake_handler.instances[0].native_calls == [
            ("fill", {"uid": "D0C0FFEE1234-40", "value": "hello"}),
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
        FakeHandler.tool_responses = {
            "get_frame_storage": make_error(
                "No frame selected. Run 'frames select PATTERN' first, "
                "or name the frame on the read with --key."
            )
        }
        with pytest.raises(LifecycleError):
            curated.storage_get(instance=None, key=None, registry_path=registry_path)

    def test_screencast_starts_waits_and_stops_over_one_handler(
        self, registry_path, fake_handler
    ):
        """One invocation, one handler: the frame buffer is process-local."""
        _seed(registry_path, {"only-01": _entry()})
        FakeHandler.frames = 3
        out = curated.screencast(
            instance=None,
            out_dir="/tmp/cast",
            duration=5.0,
            fmt="png",
            max_frames=3,
            registry_path=registry_path,
        )
        assert len(fake_handler.instances) == 1
        assert fake_handler.instances[0].tool_calls == [
            ("screencast_start", {"format": "png", "max_frames": 3}),
            ("screencast_stop", {"dir": "/tmp/cast"}),
        ]
        assert out["frames"] == 3
        assert out["dir"] == "/tmp/cast"

    def test_screencast_stops_early_when_the_frame_cap_is_reached(
        self, registry_path, fake_handler
    ):
        """A full buffer pauses the stream, so waiting out --duration is dead time."""
        import time

        _seed(registry_path, {"only-01": _entry()})
        FakeHandler.frames = 10
        started = time.monotonic()
        curated.screencast(
            instance=None, out_dir="/tmp/cast", duration=30.0, max_frames=10,
            registry_path=registry_path,
        )
        assert time.monotonic() - started < 5.0

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"duration": 0}, "--duration"),
            ({"duration": -1}, "--duration"),
            ({"fmt": "gif"}, "--format"),
        ],
    )
    def test_screencast_bounds_are_validated_before_the_browser(
        self, kwargs, message, registry_path, fake_handler
    ):
        _seed(registry_path, {"only-01": _entry()})
        with pytest.raises(UsageError) as exc:
            curated.screencast(
                instance=None, out_dir="/tmp/cast", registry_path=registry_path, **kwargs
            )
        assert message in str(exc.value)
        assert fake_handler.instances == []


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
        async def _fake_get_ws_url_async(**kw):
            return "ws://fake/browser"

        monkeypatch.setattr(
            "browser_tools.one_shot.get_ws_url_async", _fake_get_ws_url_async
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

    def test_screencast_requires_dir(self, capsys, fake_handler):
        _seed(self._registry_path, {"only-01": _entry()})
        rc = cli.main(["screencast"])
        assert rc == cli.EXIT_USAGE

    def test_screencast_via_cli(self, capsys, fake_handler):
        _seed(self._registry_path, {"only-01": _entry()})
        rc = cli.main(["screencast", "--dir", "/tmp/cast", "--duration", "0.05", "--format", "png"])
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

    def test_detect_no_wait_sends_a_zero_budget(self, capsys, fake_handler):
        """--no-wait must reach the handler as a zero retry budget."""
        _seed(self._registry_path, {"only-01": _entry()})
        FakeHandler.detection = {"detections": [], "auto_retried": False, "retries_used": 0}

        rc = cli.main(["detect", "--no-wait"])

        assert rc == cli.EXIT_OK
        assert fake_handler.instances[0].detection_budgets == [0]

    def test_detect_wait_seconds_converts_to_retries(self, capsys, fake_handler):
        """--wait SECONDS becomes a retry count at the 3s retry delay."""
        _seed(self._registry_path, {"only-01": _entry()})
        FakeHandler.detection = {"detections": [], "auto_retried": False, "retries_used": 0}

        rc = cli.main(["detect", "--wait", "6"])

        assert rc == cli.EXIT_OK
        assert fake_handler.instances[0].detection_budgets == [2]

    def test_detect_default_leaves_budget_unset(self, capsys, fake_handler):
        """With no flag the module default applies, not a computed budget."""
        _seed(self._registry_path, {"only-01": _entry()})
        FakeHandler.detection = {"detections": [], "auto_retried": False, "retries_used": 0}

        rc = cli.main(["detect"])

        assert rc == cli.EXIT_OK
        assert fake_handler.instances[0].detection_budgets == [None]

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


# RFC-05: CLI results and native CDP semantics. Browser assertions below skip
# only when the isolated browser cannot start.
class TestSixVerbContracts:
    @pytest.mark.parametrize(("argv", "name", "arguments", "result"), [
        (["eval", "const a=2; a+1", "--await"], "eval",
         {"source": "const a=2; a+1", "await_promise": True},
         {"value": 3, "type": "number", "url": "https://example.com", "targetId": "T1"}),
        (["press", "Enter", "--modifiers", "Meta"], "press",
         {"key": "Enter", "modifiers": "Meta"},
         {"key": "Enter", "modifiers": ["Meta"], "dispatched": ["keyDown", "keyUp"]}),
        (["hover", "--uid", "AB-1"], "hover", {"uid": "AB-1"},
         {"uid": "AB-1", "x": 12.5, "y": 30.0}),
        (["type", "A quote: 'yes'\n{\"ok\":true}\n你好 🐈"], "type",
         {"text": "A quote: 'yes'\n{\"ok\":true}\n你好 🐈", "source": "text"},
         {"chars": 36, "source": "text"}),
        (["wait-text", "Ready", "--timeout-ms", "321"], "wait-text",
         {"substring": "Ready", "timeout_ms": 321},
         {"found": True, "substring": "Ready", "waitedMs": 20}),
        (["network-get", "--url", "/api"], "network-get",
         {"url": "/api", "request_id": None, "response_file": None},
         {"requestId": "1.2", "url": "/api", "status": 200, "mimeType": "text/plain",
          "bytes": 2, "matched": 1, "body": "ok", "base64Encoded": False}),
    ])
    def test_cli_exact_document(self, argv, name, arguments, result, fake_handler, capsys):
        fake_handler.native_responses[name] = make_text(json.dumps(result))
        assert cli.main([*argv, "--endpoint", "http://127.0.0.1:9222"]) == 0
        assert json.loads(capsys.readouterr().out) == result
        assert fake_handler.instances[-1].native_calls == [(name, arguments)]

    @pytest.mark.parametrize("source", ["file", "stdin"])
    def test_prose_file_preserves_text(self, source, fake_handler, tmp_path, monkeypatch, capsys):
        import io

        prose = 'She said "don\'t".\n{"nested": [1, 2]}\nภาษาไทย 🐈\n'
        path = tmp_path / 'message.txt'
        path.write_text(prose, encoding='utf-8')
        argument = str(path) if source == "file" else "-"
        monkeypatch.setattr("sys.stdin", io.StringIO(prose))
        result = {"chars": len(prose), "source": "file", "path": argument}
        fake_handler.native_responses['type'] = make_text(json.dumps(result))
        assert cli.main(['type', '--file', argument, '--endpoint', 'http://127.0.0.1:9222']) == 0
        assert json.loads(capsys.readouterr().out) == result
        assert fake_handler.instances[-1].native_calls[0][1]['text'] == prose

    @pytest.mark.parametrize("verb", ["press", "hover", "type"])
    def test_hidden_tab_refused(self, verb, fake_handler, monkeypatch):
        monkeypatch.setattr(fake_handler, "page_visibility_state", lambda self: "hidden")
        argv = {'press': ['Enter'], 'hover': ['--uid', 'AB-1'], 'type': ['hello']}[verb]
        assert cli.main([verb, *argv, '--endpoint', 'http://127.0.0.1:9222']) == 1
        assert fake_handler.instances[-1].native_calls == []


def test_eval_throw_regression_at_cli(monkeypatch, fake_handler, capsys):
    """Same exceptionDetails that raw passthrough accepts must make eval fail.

    Run the real evaluation operation through the existing handler double,
    so removing the exceptionDetails check makes this regression go red.
    """
    import asyncio
    from unittest.mock import AsyncMock

    from test_passthrough import make_fake_cdp_client_cls

    from browser_tools import curated_runtime

    detail = {'text': 'Uncaught', 'exception': {'description': 'Error: regression-marker'}}
    raw_client, _ = make_fake_cdp_client_cls(responder=lambda *_: {'exceptionDetails': detail})
    monkeypatch.setattr('browser_tools.one_shot.CDPClient', raw_client)
    monkeypatch.setattr('browser_tools.one_shot.get_ws_url_async', AsyncMock(return_value='ws://fake/browser'))
    assert cli.main(['Runtime.evaluate', json.dumps({'expression': 'throw new Error("regression-marker")'}),
                     '--endpoint', 'http://127.0.0.1:9222']) == 0
    assert 'exceptionDetails' in json.loads(capsys.readouterr().out)
    cdp = AsyncMock()
    cdp.send.side_effect = [{}, {'exceptionDetails': detail},
                            {'targetInfo': {'url': 'https://page', 'targetId': 'T1'}}]

    def call_native(self, name, args):
        try:
            result = asyncio.run(curated_runtime.evaluate(cdp, args['source'], False, None))
            return make_text(json.dumps(result))
        except LifecycleError as exc:
            return make_error(str(exc))

    monkeypatch.setattr(fake_handler, 'call_native', call_native)
    assert cli.main(['eval', '(() => { throw new Error("regression-marker"); })()',
                     '--endpoint', 'http://127.0.0.1:9222']) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'Error: regression-marker' in captured.err


@pytest.mark.asyncio
async def test_eval_value_and_await_are_cdp_parameters():
    from unittest.mock import AsyncMock

    from browser_tools import curated_runtime

    cdp = AsyncMock()
    cdp.send.side_effect = [{}, {'result': {'value': {'a': [1]}, 'type': 'object'}},
                            {'targetInfo': {'url': 'https://page', 'targetId': 'T1'}}]
    result = await curated_runtime.evaluate(cdp, '({a:[1]})', True, 7)
    params = cdp.send.call_args_list[1].args[1]
    assert params['returnByValue'] is True
    assert params['awaitPromise'] is True
    assert params['contextId'] == 7
    assert result == {'value': {'a': [1]}, 'type': 'object', 'url': 'https://page', 'targetId': 'T1'}


@pytest.mark.asyncio
async def test_eval_statements_use_completion_without_retrying_execution():
    from unittest.mock import AsyncMock

    from browser_tools import curated_runtime

    cdp = AsyncMock()
    cdp.send.side_effect = [{'exceptionDetails': {'text': 'SyntaxError'}}, {},
                            {'exceptionDetails': {'text': 'thrown once'}}]
    with pytest.raises(LifecycleError, match='thrown once'):
        await curated_runtime.evaluate(cdp, 'const x = 1; throw x', False, None)
    assert cdp.send.await_count == 3
    expression = cdp.send.call_args_list[2].args[1]['expression']
    assert expression.startswith('(() =>') and 'return eval(' in expression


@pytest.mark.parametrize('key', ['Enter', 'Tab', 'Escape', 'Backspace', 'Delete',
    'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown',
    *[f'F{i}' for i in range(1,13)], 'a', 'A', '1', '!', ' ', 'é'])
def test_press_key_table_carries_default_action_fields(key):
    from browser_tools.curated_runtime import key_event

    event, _ = key_event(key, None)
    assert {'code', 'windowsVirtualKeyCode', 'nativeVirtualKeyCode', 'text'} <= event.keys()
    if key == 'Enter':
        assert event['text'] == '\r' and event['windowsVirtualKeyCode'] == 13
    if len(key) == 1:
        assert event['text'] == key


def test_press_modifier_mask_and_duplicate_names():
    from browser_tools.curated_runtime import key_event

    event, names = key_event('a', 'Control,Shift,Control')
    assert event['modifiers'] == 10
    assert names == ['Control', 'Shift']
    assert event['text'] == ''


@pytest.mark.asyncio
async def test_native_press_sends_down_and_up_and_type_one_insert():
    from unittest.mock import AsyncMock

    from browser_tools.cdp_handler import CDPHandler

    handler = CDPHandler(None)
    cdp = AsyncMock()
    await handler._dispatch_curated_action(cdp, 'press', {'key': 'Enter', 'modifiers': None})
    events = [call.args[1] for call in cdp.send.call_args_list]
    assert [event['type'] for event in events] == ['keyDown', 'keyUp']
    assert events[0]['text'] == '\r'
    cdp.reset_mock()
    response = await handler._dispatch_curated_action(cdp, 'type', {'text': 'a\n🐈', 'source': 'text'})
    cdp.send.assert_awaited_once_with('Input.insertText', {'text': 'a\n🐈'})
    assert json.loads(''.join(extract_text_items(response))) == {'chars': 3, 'source': 'text'}


@pytest.mark.asyncio
async def test_hover_uses_live_uid_box_and_native_mouse():
    from unittest.mock import AsyncMock

    from browser_tools.native_interaction import NativeInteractor
    from browser_tools.native_snapshot import NativeSnapshotReader, doc_token

    send = AsyncMock(side_effect=[
        {'frameTree': {'frame': {'id': 'f', 'loaderId': 'l'}}},
        {'node': {'nodeType': 1}}, {},
        {'model': {'content': [10,20,30,20,30,60,10,60]}}, {},
    ])
    point = await NativeInteractor(NativeSnapshotReader()).hover_async(send, f'{doc_token("f","l")}-3')
    assert point == (20,40)
    assert send.call_args_list[-2].args[0] == 'DOM.getBoxModel'
    send.assert_awaited_with('Input.dispatchMouseEvent', {'type':'mouseMoved','x':20,'y':40,'buttons':0})


@pytest.mark.parametrize(('size', 'encoded', 'omitted'), [
    (1024*1024, False, False), (1024*1024+1, False, True), (4, True, True), (0, False, False),
])
def test_network_body_limits_and_file_bytes(size, encoded, omitted, tmp_path):
    from browser_tools.curated_runtime import response_document

    data = b'\xff\x00\x01\x02' if encoded else b'x' * size
    payload = {'body': base64.b64encode(data).decode() if encoded else data.decode(), 'base64Encoded': encoded}
    event = {'requestId': '1.2', 'response': {'url':'https://a/api', 'status':200, 'mimeType':'text/plain'}}
    result = response_document(event, payload, 2, None)
    assert result['bytes'] == len(data) and result['matched'] == 2
    assert ('bodyOmitted' in result) == omitted
    assert ('body' in result) != omitted
    if omitted:
        assert '--response-file' in result['bodyOmitted']
    path = tmp_path / 'nested' / 'body'
    written = response_document(event, payload, 2, str(path))
    assert written['responseFile'] == str(path)
    assert path.read_bytes() == data
    assert 'body' not in written and 'bodyOmitted' not in written


def test_network_limits_count_utf8_bytes():
    from browser_tools.curated_runtime import response_document

    text = 'é' * (512*1024+1)
    event = {'requestId':'1','response':{'url':'u','status':200,'mimeType':'text/plain'}}
    result = response_document(event, {'body':text}, 1, None)
    assert result['bytes'] == len(text.encode('utf-8'))
    assert 'bodyOmitted' in result


@pytest.mark.asyncio
async def test_network_subscribes_before_enable_and_fetches_last(monkeypatch):
    from test_list_verbs import _RaceFakeCDP

    from browser_tools import curated_runtime

    def event(identity):
        return {'requestId': identity, 'frameId':'f', 'response':{
            'url':'https://a/api', 'status':200, 'mimeType':'text/plain'}}
    cdp = _RaceFakeCDP([
        ('Network.responseReceived', event('1')),
        ('Network.loadingFinished', {'requestId':'1'}),
    ], [
        ('Network.responseReceived', event('2'), .001),
        ('Network.loadingFinished', {'requestId':'2'}, .001),
    ])
    original = cdp.send
    bodies = []
    async def send(method, params=None):
        if method == 'Network.getResponseBody':
            bodies.append(params['requestId'])
            return {'body':'last', 'base64Encoded':False}
        return await original(method, params)
    monkeypatch.setattr(cdp, 'send', send)
    monkeypatch.setattr(curated_runtime, 'NETWORK_WINDOW_SECONDS', .02)
    result = await curated_runtime.network_get(cdp, '/api', None, None, None, None, frozenset())
    assert cdp.handler_live_at_enable
    assert cdp.calls.index('Network.enable') < cdp.calls.index('Page.reload')
    assert bodies == ['2'] and result['matched'] == 2 and result['body'] == 'last'
    assert 'Network.disable' in cdp.calls
    assert not any(cdp._handlers.values())


@pytest.mark.asyncio
@pytest.mark.parametrize('keep', [frozenset(), frozenset({'Network'})])
async def test_network_expired_request_id_preserves_error_and_cleans_up(monkeypatch, keep):
    from test_list_verbs import _RaceFakeCDP

    from browser_tools import curated_runtime
    from browser_tools.cdp_client import CDPError

    cdp = _RaceFakeCDP([])
    original = cdp.send
    async def send(method, params=None):
        if method == 'Network.getResponseBody':
            raise CDPError('No resource with given identifier found')
        return await original(method, params)
    monkeypatch.setattr(cdp, 'send', send)
    monkeypatch.setattr(curated_runtime, 'NETWORK_WINDOW_SECONDS', 0)
    with pytest.raises(CDPError, match='No resource'):
        await curated_runtime.network_get(cdp, None, 'old', None, None, None, keep)
    assert ('Network.disable' in cdp.calls) == ('Network' not in keep)
    assert not any(cdp._handlers.values())


class TestCuratedInChrome:
    """These assert browser effects, never inferred effects from sent commands."""

    @pytest.fixture
    def drive(self, curated_browser, capsys):
        def invoke(*argv, status=0):
            code = cli.main([*argv, '--endpoint', curated_browser])
            captured = capsys.readouterr()
            assert code == status, captured.err
            if status:
                assert captured.out == ''
                return captured.err
            return json.loads(captured.out)
        return invoke

    @pytest.fixture
    def page(self, drive):
        import urllib.parse

        html = '''<!doctype html><style>#h {color:rgb(1,2,3)} #h:hover {color:rgb(9,8,7)}</style>
        <form onsubmit="event.preventDefault();window.submitted=true">
        <input id="i" value="existing"><button>Submit</button></form>
        <button id="h">Hover me</button><p id="p">Already here</p>
        <script>window.events=[];for(const name of ['beforeinput','input','keydown','keyup'])
        i.addEventListener(name,e=>events.push(e.type));</script>'''
        drive('Page.navigate', json.dumps({'url': 'data:text/html,' + urllib.parse.quote(html)}))
        drive('wait-text', 'Already here')
        return drive

    def test_throw_has_exit_one_while_raw_has_zero(self, page):
        source = 'throw new Error("regression-marker")'
        raw = page('Runtime.evaluate', json.dumps({'expression': source}))
        assert 'exceptionDetails' in raw
        assert 'regression-marker' in page('eval', source, status=1)

    @pytest.mark.parametrize(('source', 'value'), [
        ('1+2', 3), ('({a:[1,2]})', {'a':[1,2]}),
        ('const x = "a;b"; const y = {f: () => 4}; [x, y.f()]', ['a;b',4]),
        ('Promise.resolve(9)', 9), ('12', 12),
        ('const n=1; return n+1', 2),
    ])
    def test_eval_completion_and_await(self, page, source, value):
        assert page('eval', source, '--await')['value'] == value

    def test_enter_submits_form(self, page):
        page('eval', 'document.querySelector("#i").focus()')
        page('press', 'Enter')
        assert page('eval', 'window.submitted')['value'] is True

    def test_hover_sets_css_rule(self, page):
        import re

        tree = page('snapshot')['snapshot']
        uid = re.search(r'\[uid=([^\]]+)\] button "Hover me"', tree).group(1)
        page('hover', '--uid', uid)
        assert page('eval', 'getComputedStyle(document.querySelector("#h")).color')['value'] == 'rgb(9, 8, 7)'

    def test_prose_inserts_at_caret_and_fires_input_not_keys(self, page, tmp_path):
        prose = 'She said "don\'t".\n{"ok": true}\nภาษาไทย 🐈'
        path = tmp_path / 'prose.txt'
        path.write_text(prose)
        page('eval', 'document.body.innerHTML="<textarea id=i>AB</textarea>"; '
             'window.events=[]; for(const name of ["beforeinput","input","keydown","keyup"]) '
             'i.addEventListener(name,e=>events.push(e.type)); i.focus(); i.setSelectionRange(1,1)')
        page('type', '--file', str(path))
        assert page('eval', 'document.querySelector("#i").value')['value'] == 'A'+prose+'B'
        assert page('eval', 'window.events')['value'] == ['beforeinput', 'input']

    def test_wait_present_later_and_never(self, page):
        assert page('wait-text', 'Already here')['found']
        page('eval', 'setTimeout(()=>document.querySelector("#p").textContent="Later",500)')
        assert page('wait-text', 'Later')['found']
        assert 'timed out' in page('wait-text', 'Never here', '--timeout-ms', '20', status=1)

    def test_wait_does_not_lose_text_during_observer_installation(self, page):
        page('eval', '''window.OriginalObserver=MutationObserver;
          window.MutationObserver=class extends OriginalObserver {
            observe(node, options) {
              document.querySelector('#p').textContent='Arrived during setup';
              super.observe(node, options);
            }
          }''')
        try:
            assert page('wait-text', 'Arrived during setup', '--timeout-ms', '1000')['found']
        finally:
            page('eval', 'window.MutationObserver=OriginalObserver')

    def test_network_reload_collects_last_response(self, page, tmp_path):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == '/':
                    data = b'''<script>fetch('/api?first').then(()=>fetch('/api?last'))</script>'''
                    mime = 'text/html'
                else:
                    data = self.path.encode()
                    mime = 'text/plain'
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            page('Page.navigate', json.dumps({'url': f'http://127.0.0.1:{server.server_port}/'}))
            result = page('network-get', '--url', '/api?')
            assert result['matched'] == 2 and result['body'] == '/api?last'
            assert result['bytes'] == 9
            path = tmp_path / 'response.txt'
            saved = page('network-get', '--url', '/api?', '--response-file', str(path))
            assert saved['responseFile'] == str(path) and path.read_bytes() == b'/api?last'
            assert 'No resource' in page('network-get', '--request-id', result['requestId'], status=1)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['none', 'unfinished', 'failed'])
async def test_network_window_failure_paths(outcome, monkeypatch):
    from test_list_verbs import _RaceFakeCDP

    from browser_tools import curated_runtime

    events = [] if outcome == 'none' else [('Network.responseReceived', {
        'requestId':'1','response':{'url':'/api','status':200,'mimeType':'text/plain'}})]
    if outcome == 'failed':
        events.append(('Network.loadingFailed', {'requestId':'1','errorText':'net::ERR_FAILED'}))
    cdp = _RaceFakeCDP(events)
    monkeypatch.setattr(curated_runtime, 'NETWORK_WINDOW_SECONDS', 0)
    with pytest.raises(LifecycleError, match={'none':'no matching', 'unfinished':'did not finish', 'failed':'net::ERR_FAILED'}[outcome]):
        await curated_runtime.network_get(cdp, '/api', None, None, None, None, frozenset())
    assert not any(cdp._handlers.values())
    assert 'Network.disable' in cdp.calls
    assert 'Network.getResponseBody' not in cdp.calls


@pytest.mark.asyncio
async def test_network_cancellation_removes_listeners_and_disables(monkeypatch):
    import asyncio

    from test_list_verbs import _RaceFakeCDP

    from browser_tools import curated_runtime

    cdp = _RaceFakeCDP([])
    task = asyncio.create_task(curated_runtime.network_get(cdp, '/api', None, None, None, None, frozenset()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not any(cdp._handlers.values())
    assert 'Network.disable' in cdp.calls


@pytest.mark.asyncio
async def test_wait_text_deadline_is_stderr_only_via_cli(monkeypatch, fake_handler, capsys):
    fake_handler.native_responses['wait-text'] = make_error('wait-text timed out after 1 ms')
    assert cli.main(['wait-text', 'never', '--timeout-ms', '1', '--endpoint', 'http://127.0.0.1:9222']) == 1
    captured = capsys.readouterr()
    assert captured.out == '' and 'timed out' in captured.err


@pytest.mark.asyncio
@pytest.mark.parametrize(('source', 'expected'), [
    ('1+2', 3), ('({a: [1,2]})', {'a':[1,2]}),
    ('const x=";"; const f=()=>({a:3}); [x, f().a]', [';',3]),
    ('let x=1\nx+2 // last expression', 3),
    ('const x=3; return x+1', 4), ('Promise.resolve(8)', 8),
    ('"quote: \\\" and apostrophe: \'"', 'quote: " and apostrophe: \''),
])
async def test_eval_generated_javascript_in_v8(source, expected):
    """Execute the generated JS in Node's V8, independently of CDP doubles."""
    import shutil
    import subprocess
    from unittest.mock import AsyncMock

    from browser_tools import curated_runtime

    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is unavailable for the generated JavaScript check')
    async def send(method, params=None):
        if method == 'Target.getTargetInfo':
            return {'targetInfo':{'url':'https://fixture','targetId':'T1'}}
        program = '''const vm = require('node:vm');
          (async () => {
            try {
              const expression = JSON.parse(process.argv[1]);
              if (process.argv[2] === 'compile') {
                new vm.Script(expression); console.log('{}');
              } else {
                const value = await vm.runInNewContext(expression);
                console.log(JSON.stringify({result:{value,type:typeof value}}));
              }
            } catch(e) { console.log(JSON.stringify({exceptionDetails:{text:String(e)}})); }
          })();'''
        completed = subprocess.run([node, '-e', program, json.dumps(params['expression']),
            'compile' if method == 'Runtime.compileScript' else 'evaluate'],
            capture_output=True, text=True, check=True)
        return json.loads(completed.stdout)
    cdp = AsyncMock()
    cdp.send.side_effect = send
    result = await curated_runtime.evaluate(cdp, source, True, None)
    assert result['value'] == expected
