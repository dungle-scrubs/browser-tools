"""Tests for the ``attach`` and ``wait`` event verbs (RFC-01 #42).

These exercise argument resolution, ``wait``'s SUBSCRIBE-FIRST buffering
(including the between-subscribe-and-examine race, forced deterministically
with a fake event source), attach two-observer isolation (driven through the
real ``core.cdp_client.CDPClient`` session-filtered dispatch that
``core.attach.run_attach`` composes), and the CLI-front exit-code mapping. All
against an isolated registry file and fake CDP transport -- no real browser or
websocket is involved.

What is proven at the test-double level vs. against a live browser is called
out where it matters: the subprocess streaming loop of ``core.attach.run_attach``
(stdin commands, signal handling, liveness polling) is verbatim vendored and is
only exercised end-to-end against a live browser; here the isolation *primitive*
it relies on -- per-session handler registration and off-by-identity removal --
is proven directly.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from browser_tools import cli, events, lifecycle
from browser_tools.core import registry as core_registry
from browser_tools.core.cdp_client import CDPClient
from browser_tools.events import WaitTimeout
from browser_tools.lifecycle import LifecycleError
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
# attach argument resolution
# ---------------------------------------------------------------------------


class TestResolveAttachArgs:
    def test_instance_and_events(self):
        instance, evts = events.resolve_attach_args(
            ["site-01", "+Page.loadEventFired", "+Network.requestWillBeSent"]
        )
        assert instance == "site-01"
        assert evts == ["Page.loadEventFired", "Network.requestWillBeSent"]

    def test_events_only_instance_none(self):
        instance, evts = events.resolve_attach_args(["+Page.loadEventFired"])
        assert instance is None
        assert evts == ["Page.loadEventFired"]

    def test_no_subscription_is_usage_error(self):
        with pytest.raises(UsageError):
            events.resolve_attach_args(["site-01"])

    def test_empty_is_usage_error(self):
        with pytest.raises(UsageError):
            events.resolve_attach_args([])

    def test_malformed_subscription_is_usage_error(self):
        with pytest.raises(UsageError):
            events.resolve_attach_args(["+notanevent"])

    def test_second_bare_token_is_usage_error(self):
        with pytest.raises(UsageError):
            events.resolve_attach_args(["site-01", "site-02", "+Page.loadEventFired"])


# ---------------------------------------------------------------------------
# wait: SUBSCRIBE-FIRST buffering and the between-subscribe-and-examine race
# ---------------------------------------------------------------------------


class _RaceFakeCDP:
    """Fake CDP session that fires an event DURING ``Domain.enable``.

    This is the crux of the subscribe-first proof. ``wait_on_session`` does,
    in order: (1) ``on()`` to subscribe, (2) ``await send("Domain.enable")``,
    (3) begin examining the buffer. This fake fires the event inside step (2) --
    strictly after subscription and strictly before examination -- which is
    exactly the window the RFC says an event must not be lost in.

    Because the event is delivered synchronously inside the ``send`` await, a
    subscribe-first implementation already has the handler registered and
    buffers it; an examine-first implementation would have no handler yet and
    drop it, then block until the deadline. ``handler_live_at_enable`` records
    which case held, so the test asserts the ordering, not just the outcome.
    """

    def __init__(self, fired: list[tuple[str, dict]]):
        self._fired = fired
        self._handlers: dict[str, list[tuple]] = {}
        self.calls: list[str] = []
        self.handler_live_at_enable: bool | None = None

    def on(self, event, callback, session_id=None):
        self._handlers.setdefault(event, []).append((callback, session_id))

    def off(self, event, callback):
        self._handlers[event] = [
            (cb, sid) for (cb, sid) in self._handlers.get(event, []) if cb is not callback
        ]

    async def send(self, method, params=None, session_id=None):
        self.calls.append(method)
        if method.endswith(".enable"):
            self.handler_live_at_enable = any(self._handlers.values())
            for event, event_params in self._fired:
                for cb, sid in self._handlers.get(event, []):
                    if sid is None or sid == session_id:
                        cb(event_params)
        return {}


class TestWaitSubscribeFirst:
    def test_catches_event_fired_between_subscribe_and_examine(self):
        # The event fires during Domain.enable -- after subscription, before
        # the wait loop examines the buffer. Only subscribe-first catches it.
        fake = _RaceFakeCDP([("Page.loadEventFired", {"timestamp": 1})])
        result = asyncio.run(
            events.wait_on_session(fake, "S1", "Page.loadEventFired", None, timeout=5)
        )
        # Proof the ordering was subscribe-then-enable: the handler was already
        # registered when Domain.enable fired the event.
        assert fake.handler_live_at_enable is True
        assert result == {"method": "Page.loadEventFired", "params": {"timestamp": 1}}
        assert fake.calls == ["Page.enable"]

    def test_match_skips_nonmatching_buffered_events(self):
        # Two events buffered during enable; only the second matches the
        # substring. The loop drains past the first and returns the second.
        fake = _RaceFakeCDP(
            [
                ("Network.responseReceived", {"url": "https://other.example"}),
                ("Network.responseReceived", {"url": "https://target.example/api"}),
            ]
        )
        result = asyncio.run(
            events.wait_on_session(
                fake, "S1", "Network.responseReceived", "target.example", timeout=5
            )
        )
        assert result["params"]["url"] == "https://target.example/api"

    def test_deadline_with_no_matching_event_raises_wait_timeout(self):
        # A non-matching event fires, then nothing else. The wait must reach its
        # deadline (empty buffer after draining the non-match) and raise.
        fake = _RaceFakeCDP([("Network.responseReceived", {"url": "https://other.example"})])
        with pytest.raises(WaitTimeout):
            asyncio.run(
                events.wait_on_session(
                    fake, "S1", "Network.responseReceived", "target.example", timeout=0.05
                )
            )

    def test_handler_removed_after_return(self):
        fake = _RaceFakeCDP([("Page.loadEventFired", {"timestamp": 1})])
        asyncio.run(events.wait_on_session(fake, "S1", "Page.loadEventFired", None, timeout=5))
        # off() was called in the finally, leaving no live handler behind.
        assert fake._handlers.get("Page.loadEventFired") == []


# ---------------------------------------------------------------------------
# attach: two-observer isolation
# ---------------------------------------------------------------------------


class _FakeWS:
    """Async-iterable fake websocket yielding pre-scripted raw CDP frames.

    Drives the REAL ``CDPClient._recv_loop`` dispatch so the session-filtering
    and handler-matching under test is production code, not a re-implementation.
    """

    def __init__(self, frames: list[str]):
        self._frames = frames

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for frame in self._frames:
            yield frame


def _event_frame(method: str, session_id: str, params: dict) -> str:
    return json.dumps({"method": method, "params": params, "sessionId": session_id})


async def _drive(cdp: CDPClient, frames: list[str]) -> None:
    """Run the real recv loop over one batch of frames, then stop."""
    cdp._ws = _FakeWS(frames)
    cdp._connected = True
    await cdp._recv_loop()


class TestAttachTwoObserverIsolation:
    """Two observers over one transport must not see each other's subscriptions.

    This drives the isolation *primitive* ``core.attach.run_attach`` composes:
    it registers each subscription with ``cdp.on(event, handler,
    session_id=session_id)`` and retires one with ``cdp.off(event, handler)``.
    The subprocess streaming loop around that (stdin, signals, liveness) is
    verbatim vendored and is only proven end-to-end against a live browser;
    here the per-session registration and off-by-identity removal it depends on
    are proven directly against the real ``CDPClient`` dispatch.
    """

    def test_observers_isolated_and_retire_does_not_disturb_other(self):
        cdp = CDPClient(ws_url="ws://fake/browser")

        a_seen: list[dict] = []
        b_seen: list[dict] = []

        # Observer A (session "SA") subscribes to Page.loadEventFired.
        # Observer B (session "SB") subscribes to Page.loadEventFired AND a
        # B-only event, Network.requestWillBeSent. Registration mirrors
        # run_attach._subscribe exactly.
        def handler_a(params):
            a_seen.append(params)

        def handler_b_load(params):
            b_seen.append(params)

        def handler_b_net(params):
            b_seen.append(params)

        cdp.on(event="Page.loadEventFired", callback=handler_a, session_id="SA")
        cdp.on(event="Page.loadEventFired", callback=handler_b_load, session_id="SB")
        cdp.on(event="Network.requestWillBeSent", callback=handler_b_net, session_id="SB")

        # Phase 1: fire one load event per session, plus a B-only event whose
        # session is SA -- A never subscribed to it, so A must not receive it,
        # and its SA tag must not leak it into B either.
        frames_1 = [
            _event_frame("Page.loadEventFired", "SA", {"n": "a1"}),
            _event_frame("Page.loadEventFired", "SB", {"n": "b1"}),
            _event_frame("Network.requestWillBeSent", "SA", {"n": "stray"}),
        ]
        asyncio.run(_drive(cdp, frames_1))

        # A saw only its own session's load event; B saw only its own.
        assert a_seen == [{"n": "a1"}]
        assert b_seen == [{"n": "b1"}]

        # Phase 2: observer B retires (mirrors run_attach._unsubscribe:
        # cdp.off(event, handler) by identity for each of B's subscriptions).
        cdp.off(event="Page.loadEventFired", callback=handler_b_load)
        cdp.off(event="Network.requestWillBeSent", callback=handler_b_net)

        frames_2 = [
            _event_frame("Page.loadEventFired", "SA", {"n": "a2"}),
            _event_frame("Page.loadEventFired", "SB", {"n": "b2"}),
        ]
        asyncio.run(_drive(cdp, frames_2))

        # A's stream continued undisturbed by B's retirement; B received nothing.
        assert a_seen == [{"n": "a1"}, {"n": "a2"}]
        assert b_seen == [{"n": "b1"}]


# ---------------------------------------------------------------------------
# Full wait path: fake CDP transport (connect / resolve target / attach)
# ---------------------------------------------------------------------------


def _make_wait_transport(fired, targets=None):
    """Build a fake ``CDPClient`` for the full ``events.wait`` path.

    Auto-answers the Target plumbing every wait needs and fires ``fired``
    (a list of ``(event, params)``) synchronously when a ``Domain.enable`` is
    sent -- the same between-subscribe-and-examine timing as the unit fake, now
    over the connect/resolve/attach plumbing ``wait`` shares with passthrough.
    """
    targets = targets if targets is not None else [
        {"targetId": "T1", "type": "page", "url": "https://example.com"}
    ]

    class FakeCDP:
        def __init__(self, ws_url):
            self._handlers: dict[str, list[tuple]] = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def on(self, event, callback, session_id=None):
            self._handlers.setdefault(event, []).append((callback, session_id))

        def off(self, event, callback):
            self._handlers[event] = [
                (cb, sid) for (cb, sid) in self._handlers.get(event, []) if cb is not callback
            ]

        async def send(self, method, params=None, session_id=None):
            if method == "Target.getTargets":
                return {"targetInfos": targets}
            if method == "Target.attachToTarget":
                return {"sessionId": "S1"}
            if method == "Target.detachFromTarget":
                return {}
            if method.endswith(".enable"):
                for event, event_params in fired:
                    for cb, sid in self._handlers.get(event, []):
                        if sid is None or sid == session_id:
                            cb(event_params)
            return {}

    return FakeCDP


@pytest.fixture
def wait_transport(monkeypatch):
    def _install(fired, targets=None):
        monkeypatch.setattr(events, "CDPClient", _make_wait_transport(fired, targets))
        monkeypatch.setattr(events, "get_ws_url", lambda **kw: "ws://fake/browser")

    return _install


class TestWaitFullPath:
    def test_match_returns_event(self, registry_path, wait_transport):
        _seed(registry_path, {"site-01": _entry()})
        wait_transport([("Page.loadEventFired", {"timestamp": 9})])
        result = events.wait(
            instance="site-01",
            event="Page.loadEventFired",
            registry_path=registry_path,
        )
        assert result == {"method": "Page.loadEventFired", "params": {"timestamp": 9}}

    def test_deadline_raises_wait_timeout(self, registry_path, wait_transport):
        _seed(registry_path, {"site-01": _entry()})
        wait_transport([])  # nothing ever fires
        with pytest.raises(WaitTimeout):
            events.wait(
                instance="site-01",
                event="Page.loadEventFired",
                timeout=0.05,
                registry_path=registry_path,
            )

    def test_unknown_instance_is_lifecycle_error(self, registry_path, wait_transport):
        _seed(registry_path, {})
        wait_transport([])
        with pytest.raises(LifecycleError):
            events.wait(
                instance="ghost", event="Page.loadEventFired", registry_path=registry_path
            )

    def test_instance_omitted_resolves_single(self, registry_path, wait_transport):
        _seed(registry_path, {"only-01": _entry()})
        wait_transport([("Page.loadEventFired", {"ok": True})])
        result = events.wait(
            instance=None, event="Page.loadEventFired", registry_path=registry_path
        )
        assert result["params"] == {"ok": True}


# ---------------------------------------------------------------------------
# CLI front: dispatch and exit codes
# ---------------------------------------------------------------------------


class TestCliFront:
    @pytest.fixture(autouse=True)
    def _isolate_registry(self, monkeypatch, tmp_path):
        self._registry_path = str(tmp_path / "registry.json")
        monkeypatch.setenv(lifecycle.REGISTRY_ENV_VAR, self._registry_path)

    def _install_wait(self, monkeypatch, fired, targets=None):
        monkeypatch.setattr(events, "CDPClient", _make_wait_transport(fired, targets))
        monkeypatch.setattr(events, "get_ws_url", lambda **kw: "ws://fake/browser")

    def test_wait_match_prints_json_exit_ok(self, monkeypatch, capsys):
        _seed(self._registry_path, {"only-01": _entry()})
        self._install_wait(monkeypatch, [("Page.loadEventFired", {"timestamp": 3})])
        rc = cli.main(["wait", "--event", "Page.loadEventFired"])
        assert rc == cli.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out == {"method": "Page.loadEventFired", "params": {"timestamp": 3}}

    def test_wait_deadline_exit_operational_empty_stdout_stderr_diagnostic(
        self, monkeypatch, capsys
    ):
        _seed(self._registry_path, {"only-01": _entry()})
        self._install_wait(monkeypatch, [])  # nothing fires -> deadline
        rc = cli.main(["wait", "--event", "Page.loadEventFired", "--timeout", "0.05"])
        captured = capsys.readouterr()
        assert rc == cli.EXIT_OPERATIONAL
        assert captured.out == ""  # no partial output on stdout
        assert "timeout" in captured.err

    def test_wait_missing_event_flag_exits_usage(self, capsys):
        _seed(self._registry_path, {"only-01": _entry()})
        with pytest.raises(SystemExit) as exc:
            cli.main(["wait"])
        assert exc.value.code == cli.EXIT_USAGE

    def test_wait_both_target_and_url_exits_usage(self, monkeypatch, capsys):
        _seed(self._registry_path, {"only-01": _entry()})
        self._install_wait(monkeypatch, [])
        rc = cli.main(
            ["wait", "--event", "Page.loadEventFired", "--target", "1", "--url", "x"]
        )
        assert rc == cli.EXIT_USAGE
        assert "error:" in capsys.readouterr().err

    def test_attach_dispatches_to_core_run_attach(self, monkeypatch):
        _seed(self._registry_path, {"only-01": _entry()})
        recorded = {}

        async def fake_run_attach(**kwargs):
            recorded.update(kwargs)

        monkeypatch.setattr(events.core_attach, "run_attach", fake_run_attach)
        rc = cli.main(["attach", "+Page.loadEventFired", "+Network.requestWillBeSent"])
        assert rc == cli.EXIT_OK
        assert recorded["instance_name"] == "only-01"
        assert recorded["subscriptions"] == ["Page.loadEventFired", "Network.requestWillBeSent"]

    def test_attach_passes_target_slot(self, monkeypatch):
        _seed(self._registry_path, {"only-01": _entry()})
        recorded = {}

        async def fake_run_attach(**kwargs):
            recorded.update(kwargs)

        monkeypatch.setattr(events.core_attach, "run_attach", fake_run_attach)
        rc = cli.main(["attach", "+Page.loadEventFired", "--url", "example.com"])
        assert rc == cli.EXIT_OK
        assert recorded["target_spec"] == "example.com"
        assert recorded["target_by"] == "url"

    def test_attach_no_subscription_exits_usage(self, capsys):
        _seed(self._registry_path, {"only-01": _entry()})
        rc = cli.main(["attach", "only-01"])
        assert rc == cli.EXIT_USAGE
        assert "error:" in capsys.readouterr().err

    def test_attach_both_target_and_url_exits_usage(self, capsys):
        _seed(self._registry_path, {"only-01": _entry()})
        rc = cli.main(["attach", "+Page.loadEventFired", "--target", "1", "--url", "x"])
        assert rc == cli.EXIT_USAGE
        assert "error:" in capsys.readouterr().err

    def test_attach_unknown_instance_exits_operational(self, monkeypatch):
        _seed(self._registry_path, {"only-01": _entry()})
        rc = cli.main(["attach", "ghost", "+Page.loadEventFired"])
        # ghost is the bare instance token; core lookup fails -> operational.
        assert rc == cli.EXIT_OPERATIONAL
