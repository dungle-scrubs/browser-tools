"""The handler transport attaches one isolated session on the core client."""

from __future__ import annotations

import asyncio
import threading

from browser_tools import cdp_handler


def test_runtime_binds_commands_and_events_to_its_target(monkeypatch):
    calls = []
    subscriptions = []

    class Client:
        def __init__(self, ws_url):
            assert ws_url == "ws://test/browser"

        async def connect(self):
            pass

        async def close(self):
            calls.append(("close", None, None))

        async def send(self, method, params=None, session_id=None):
            calls.append((method, params, session_id))
            if method == "Target.attachToTarget":
                return {"sessionId": "SESSION"}
            return {}

        def on(self, event, callback, session_id=None):
            subscriptions.append((event, session_id))

        def off(self, event, callback):
            pass

    monkeypatch.setattr(cdp_handler, "CDPClient", Client, raising=False)
    monkeypatch.setattr(cdp_handler, "get_ws_url", lambda **kw: "ws://test/browser", raising=False)
    runtime = cdp_handler.CDPRuntime(port=9222, target_id="TARGET")
    thread = threading.Thread(target=runtime.run)
    thread.start()
    try:
        runtime.wait_ready(2)
        assert runtime.available
        future = asyncio.run_coroutine_threadsafe(runtime.send("DOM.getDocument"), runtime.loop)
        assert future.result(2) == {}
        assert ("DOM.getDocument", None, "SESSION") in calls
        assert ("Page.frameAttached", "SESSION") in subscriptions
        assert ("Runtime.executionContextCreated", "SESSION") in subscriptions
    finally:
        runtime.stop()
        thread.join(2)
    assert not thread.is_alive()
    assert calls[0] == ("Target.attachToTarget", {"targetId": "TARGET", "flatten": True}, None)
    assert ("Target.detachFromTarget", {"sessionId": "SESSION"}, None) in calls
    assert calls[-1] == ("close", None, None)


def test_runtime_stop_cancels_an_incomplete_connection(monkeypatch):
    connecting = threading.Event()
    closed = threading.Event()

    class Client:
        def __init__(self, ws_url):
            pass

        async def connect(self):
            connecting.set()
            await asyncio.Event().wait()

        async def close(self):
            closed.set()

    monkeypatch.setattr(cdp_handler, "CDPClient", Client)
    monkeypatch.setattr(cdp_handler, "get_ws_url", lambda **kw: "ws://test/browser")
    runtime = cdp_handler.CDPRuntime(9222, "TARGET")
    thread = threading.Thread(target=runtime.run, daemon=True)
    thread.start()
    assert connecting.wait(2)
    runtime.stop()
    thread.join(2)
    assert not thread.is_alive()
    assert closed.is_set()


def test_runtime_cleanup_is_bounded_when_detach_and_close_stall(monkeypatch):
    closed = threading.Event()

    class Client:
        def __init__(self, url):
            pass

        async def connect(self):
            pass

        async def close(self):
            closed.set()
            await asyncio.Event().wait()

        def on(self, *args, **kwargs):
            pass

        async def send(self, method, params=None, session_id=None):
            if method == "Target.attachToTarget":
                return {"sessionId": "session"}
            if method == "Target.detachFromTarget":
                await asyncio.Event().wait()
            return {}

    monkeypatch.setattr(cdp_handler, "CDPClient", Client)
    monkeypatch.setattr(cdp_handler, "get_ws_url", lambda **kw: "ws://test/browser")
    monkeypatch.setattr(cdp_handler, "RUNTIME_CLEANUP_TIMEOUT_SECONDS", 0.05, raising=False)
    runtime = cdp_handler.CDPRuntime(9222, "target")
    thread = threading.Thread(target=runtime.run, daemon=True)
    thread.start()
    runtime.wait_ready(1)
    runtime.stop()
    thread.join(0.5)
    assert not thread.is_alive()
    assert closed.is_set()
