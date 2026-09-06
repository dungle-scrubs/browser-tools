"""Capture failure paths through CLI dispatch and the real runtime owner."""

import asyncio
import base64
import json
import signal
from unittest.mock import AsyncMock

import pytest

from browser_tools import cdp_handler, cli, curated
from browser_tools.core.errors import CDPError


@pytest.mark.parametrize(
    "scenario, exit_code, has_manifest",
    [
        ("connect-failure", 1, False),
        ("start-failure", 1, False),
        ("startup-signal", 1, False),
        ("start-signal", 1, False),
        ("connect-timeout", 1, False),
        ("zero-frames", 1, False),
        ("transport-loss", 1, True),
        ("target-closure", 1, True),
        ("write-failure", 1, False),
        ("repeated-signals", 0, True),
        ("stop-timeout", 0, True),
    ],
)
def test_capture_finalizes_once_and_reports_failures(
    scenario, exit_code, has_manifest, tmp_path, monkeypatch, capsys
):
    output = tmp_path / "frames"
    handlers = {}
    calls = []
    callbacks = {}
    started = False

    original_signal = signal.signal

    def install(sig, handler):
        previous = original_signal(sig, handler)
        handlers[sig] = handler
        return previous

    class Client:
        def __init__(self, url):
            pass

        async def connect(self):
            if scenario == "connect-failure":
                raise ConnectionError("test transport unavailable")
            if scenario == "connect-timeout":
                await asyncio.Event().wait()
            if scenario == "startup-signal":
                handlers[signal.SIGINT](signal.SIGINT, None)
                await asyncio.Event().wait()

        async def close(self):
            calls.append("close")

        def on(self, event, callback, session_id=None):
            callbacks[event] = callback

        def off(self, event, callback):
            callbacks.pop(event, None)

        async def send(self, method, params=None, session_id=None):
            nonlocal started
            calls.append(method)
            if method == "Target.attachToTarget":
                return {"sessionId": "test-session"}
            if method == "Page.startScreencast":
                if scenario == "start-failure":
                    raise CDPError(-1, "test start rejected")
                if scenario == "start-signal":
                    handlers[signal.SIGINT](signal.SIGINT, None)
                    await asyncio.Event().wait()
                started = True
                if scenario != "zero-frames":
                    callbacks["Page.screencastFrame"](
                        {"data": base64.b64encode(b"test-frame").decode()}
                    )
            if method == "Page.getFrameTree" and started:
                if scenario == "transport-loss":
                    raise ConnectionError("test transport lost")
                if scenario == "target-closure":
                    raise CDPError(-1, "test target closed")
            if method == "Page.stopScreencast":
                if scenario == "transport-loss":
                    raise ConnectionError("test transport lost")
                if scenario == "target-closure":
                    raise CDPError(-1, "test target closed")
                if scenario == "repeated-signals":
                    handlers[signal.SIGINT](signal.SIGINT, None)
                    handlers[signal.SIGTERM](signal.SIGTERM, None)
                if scenario == "stop-timeout":
                    await asyncio.Event().wait()
                if scenario == "write-failure":
                    output.mkdir()
                    (output / "frame_00000.jpg").write_bytes(b"existing")
            return {}

    if scenario == "connect-timeout":
        monkeypatch.setattr(curated, "HANDLER_CONNECT_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(curated.signal, "signal", install)
    monkeypatch.setattr(curated, "_resolve_port", lambda *args: 9222)
    monkeypatch.setattr(curated, "resolve_page_target", AsyncMock(return_value="target"))
    monkeypatch.setattr(cdp_handler, "CDPClient", Client)
    monkeypatch.setattr(cdp_handler, "get_ws_url", lambda **kwargs: "ws://test/browser")
    result = cli.main(["screencast", "start", "--dir", str(output), "--duration", "0.1"])
    captured = capsys.readouterr()
    assert result == exit_code, (captured.err, calls)
    assert calls.count("close") == 1
    assert calls.count("Page.stopScreencast") <= 1
    assert "Page.screencastFrame" not in callbacks
    assert (output / "frames.json").exists() == has_manifest
    if exit_code == 0:
        assert "result" in json.loads(captured.out)
    else:
        assert captured.err
        assert not captured.out
    if scenario == "write-failure":
        assert str(output) in captured.err
        assert (output / "frame_00000.jpg").read_bytes() == b"existing"


@pytest.mark.parametrize(
    "invalid",
    [
        "missing-dir",
        "zero-duration",
        "nan",
        "infinity",
        "zero-limit",
        "nonempty",
        "symlink",
        "stop",
    ],
)
def test_capture_invalid_options_fail_before_connection(invalid, tmp_path, monkeypatch, capsys):
    output = tmp_path / "output"
    args = ["screencast", "record", "--dir", str(output)]
    if invalid == "missing-dir":
        args = ["screencast", "record"]
    elif invalid == "zero-duration":
        args += ["--duration", "0"]
    elif invalid in ("nan", "infinity"):
        args += ["--duration", invalid]
    elif invalid == "zero-limit":
        args += ["--max-frames", "0"]
    elif invalid == "nonempty":
        output.mkdir()
        (output / "existing").write_text("keep")
    elif invalid == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        output.symlink_to(target, target_is_directory=True)
    elif invalid == "stop":
        args = ["screencast", "stop"]

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid invocation reached instance resolution")

    monkeypatch.setattr(curated, "_resolve_port", forbidden)
    assert cli.main(args) == cli.EXIT_USAGE
    assert capsys.readouterr().err
