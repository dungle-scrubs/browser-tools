"""Tests for the ``console-list`` and ``network-list`` verbs (RFC-01 #43).

Exercises the SUBSCRIBE-FIRST collection window (``collect_on_session``),
console/network rendering, the full verb path over a fake CDP transport
(mirroring ``test_events.py``'s ``_make_wait_transport`` pattern), and the
CLI-front dispatch/exit-code mapping. All against an isolated registry file
and fake CDP transport -- no real browser or websocket is involved.

What is proven at the test-double level vs. against a live browser is called
out where it matters: the fake transport here answers ``Target.getTargets``/
``Target.attachToTarget``/``Target.detachFromTarget`` and fires scripted
events on ``Domain.enable`` (or after a scheduled delay, to simulate an event
arriving mid-window); it does not open a real websocket or run a real
browser. The collection primitive it drives -- ``collect_on_session``'s
subscribe-then-enable-then-sleep ordering -- is exercised directly against a
minimal fake in ``TestCollectOnSession`` the same way ``test_events.py``
proves ``wait_on_session`` with ``_RaceFakeCDP``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from browser_tools import cli, lifecycle, list_verbs
from browser_tools.core import registry as core_registry
from browser_tools.lifecycle import LifecycleError


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
# collect_on_session: SUBSCRIBE-FIRST and the mid-window race
# ---------------------------------------------------------------------------


class _RaceFakeCDP:
    """Fake CDP session firing one event during ``Domain.enable``, one during
    the collection window's sleep.

    Mirrors ``test_events.py``'s ``_RaceFakeCDP``: the enable-time event
    proves subscribe-before-enable buffering; the delayed event (fired from a
    background task scheduled during ``.enable`` but delivered only after a
    short sleep) proves an event arriving *during* the window -- not just
    during setup -- is still captured.
    """

    def __init__(self, fired_on_enable, fired_after_delay=()):
        self._fired_on_enable = fired_on_enable
        self._fired_after_delay = fired_after_delay
        self._handlers: dict[str, list[tuple]] = {}
        self.calls: list[str] = []
        self.handler_live_at_enable: bool | None = None
        self._background_tasks: list[asyncio.Task] = []

    def on(self, event, callback, session_id=None):
        self._handlers.setdefault(event, []).append((callback, session_id))

    def off(self, event, callback):
        self._handlers[event] = [
            (cb, sid) for (cb, sid) in self._handlers.get(event, []) if cb is not callback
        ]

    async def _fire_delayed(self, event, params, delay, session_id):
        await asyncio.sleep(delay)
        for cb, sid in self._handlers.get(event, []):
            if sid is None or sid == session_id:
                cb(params)

    async def send(self, method, params=None, session_id=None):
        self.calls.append(method)
        if method.endswith(".enable"):
            self.handler_live_at_enable = any(self._handlers.values())
            for event, event_params in self._fired_on_enable:
                for cb, sid in self._handlers.get(event, []):
                    if sid is None or sid == session_id:
                        cb(event_params)
            for event, event_params, delay in self._fired_after_delay:
                task = asyncio.ensure_future(
                    self._fire_delayed(event, event_params, delay, session_id)
                )
                self._background_tasks.append(task)
        return {}


class TestCollectOnSession:
    def test_catches_event_fired_during_enable(self):
        fake = _RaceFakeCDP([("Runtime.consoleAPICalled", {"n": 1})])
        result = asyncio.run(
            list_verbs.collect_on_session(fake, "S1", ["Runtime.consoleAPICalled"], duration=0)
        )
        assert fake.handler_live_at_enable is True
        assert result == [{"method": "Runtime.consoleAPICalled", "params": {"n": 1}}]

    def test_catches_event_fired_during_the_window(self):
        # Nothing fires at enable time; the event arrives partway through the
        # sleep window. Only a subscribe-first, window-spanning collector
        # catches this -- a collector that stopped listening after enable
        # would miss it entirely.
        fake = _RaceFakeCDP([], fired_after_delay=[("Runtime.consoleAPICalled", {"n": 2}, 0.02)])
        result = asyncio.run(
            list_verbs.collect_on_session(
                fake, "S1", ["Runtime.consoleAPICalled"], duration=0.05
            )
        )
        assert result == [{"method": "Runtime.consoleAPICalled", "params": {"n": 2}}]

    def test_collects_multiple_events_across_domains(self):
        fake = _RaceFakeCDP(
            [
                ("Network.requestWillBeSent", {"requestId": "r1"}),
                ("Network.responseReceived", {"requestId": "r1"}),
            ]
        )
        result = asyncio.run(
            list_verbs.collect_on_session(
                fake, "S1", ["Network.requestWillBeSent", "Network.responseReceived"], duration=0
            )
        )
        assert len(result) == 2
        assert fake.calls == ["Network.enable"]  # one enable per domain, not per event

    def test_handlers_removed_after_return(self):
        fake = _RaceFakeCDP([("Runtime.consoleAPICalled", {"n": 1})])
        asyncio.run(
            list_verbs.collect_on_session(fake, "S1", ["Runtime.consoleAPICalled"], duration=0)
        )
        assert fake._handlers.get("Runtime.consoleAPICalled") == []

    def test_zero_duration_skips_sleep_but_keeps_enable_time_events(self):
        fake = _RaceFakeCDP([("Runtime.consoleAPICalled", {"n": 1})])
        result = asyncio.run(
            list_verbs.collect_on_session(fake, "S1", ["Runtime.consoleAPICalled"], duration=0)
        )
        assert result == [{"method": "Runtime.consoleAPICalled", "params": {"n": 1}}]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRenderConsoleEntry:
    def test_string_value_arg(self):
        item = {
            "method": "Runtime.consoleAPICalled",
            "params": {
                "type": "log",
                "timestamp": 123.0,
                "args": [{"type": "string", "value": "hello"}],
            },
        }
        rendered = list_verbs._render_console_entry(item)
        assert rendered == {"type": "log", "text": "hello", "timestamp": 123.0}

    def test_multiple_args_joined(self):
        item = {
            "method": "Runtime.consoleAPICalled",
            "params": {
                "type": "warning",
                "timestamp": 1.0,
                "args": [{"value": "count:"}, {"value": 3}],
            },
        }
        rendered = list_verbs._render_console_entry(item)
        assert rendered["text"] == "count: 3"
        assert rendered["type"] == "warning"

    def test_object_arg_uses_description(self):
        item = {
            "method": "Runtime.consoleAPICalled",
            "params": {
                "type": "error",
                "args": [{"type": "object", "description": "Error: boom"}],
            },
        }
        rendered = list_verbs._render_console_entry(item)
        assert rendered["text"] == "Error: boom"

    def test_missing_type_defaults_to_log(self):
        item = {"method": "Runtime.consoleAPICalled", "params": {"args": []}}
        rendered = list_verbs._render_console_entry(item)
        assert rendered["type"] == "log"


class TestRenderNetworkEntries:
    def test_request_then_response_merge_into_one_row(self):
        raw = [
            {
                "method": "Network.requestWillBeSent",
                "params": {
                    "requestId": "r1",
                    "type": "Document",
                    "request": {"method": "GET", "url": "https://example.com/"},
                },
            },
            {
                "method": "Network.responseReceived",
                "params": {
                    "requestId": "r1",
                    "type": "Document",
                    "response": {"status": 200, "statusText": "OK", "url": "https://example.com/"},
                },
            },
        ]
        rows = list_verbs._render_network_entries(raw)
        assert rows == [
            {
                "requestId": "r1",
                "method": "GET",
                "url": "https://example.com/",
                "resourceType": "Document",
                "status": 200,
                "statusText": "OK",
            }
        ]

    def test_response_only_still_produces_a_row(self):
        raw = [
            {
                "method": "Network.responseReceived",
                "params": {
                    "requestId": "r2",
                    "type": "Script",
                    "response": {"status": 304, "statusText": "Not Modified", "url": "https://example.com/a.js"},
                },
            }
        ]
        rows = list_verbs._render_network_entries(raw)
        assert rows == [
            {
                "requestId": "r2",
                "method": None,
                "url": "https://example.com/a.js",
                "resourceType": "Script",
                "status": 304,
                "statusText": "Not Modified",
            }
        ]

    def test_request_only_still_produces_a_row_with_no_status(self):
        raw = [
            {
                "method": "Network.requestWillBeSent",
                "params": {
                    "requestId": "r3",
                    "type": "XHR",
                    "request": {"method": "POST", "url": "https://example.com/api"},
                },
            }
        ]
        rows = list_verbs._render_network_entries(raw)
        assert rows == [
            {
                "requestId": "r3",
                "method": "POST",
                "url": "https://example.com/api",
                "resourceType": "XHR",
                "status": None,
                "statusText": None,
            }
        ]

    def test_row_order_follows_first_sight_of_request_id(self):
        raw = [
            {"method": "Network.requestWillBeSent", "params": {"requestId": "r2", "request": {}}},
            {"method": "Network.requestWillBeSent", "params": {"requestId": "r1", "request": {}}},
        ]
        rows = list_verbs._render_network_entries(raw)
        assert [row["requestId"] for row in rows] == ["r2", "r1"]

    def test_events_missing_request_id_are_skipped(self):
        raw = [{"method": "Network.requestWillBeSent", "params": {"request": {}}}]
        assert list_verbs._render_network_entries(raw) == []


# ---------------------------------------------------------------------------
# Full path: fake CDP transport (connect / resolve target / attach)
# ---------------------------------------------------------------------------


def _make_list_transport(fired, targets=None):
    """Build a fake ``CDPClient`` for the full ``console_list``/``network_list`` path.

    Same shape as ``test_events.py``'s ``_make_wait_transport``: auto-answers
    the Target plumbing every collection needs and fires ``fired`` (a list of
    ``(event, params)``) synchronously when a ``Domain.enable`` is sent.
    """
    targets = (
        targets
        if targets is not None
        else [{"targetId": "T1", "type": "page", "url": "https://example.com"}]
    )

    class FakeCDP:
        def __init__(self, ws_url):
            self._handlers: dict[str, list[tuple]] = {}
            self.detached = False

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
                self.detached = True
                return {}
            if method.endswith(".enable"):
                for event, event_params in fired:
                    for cb, sid in self._handlers.get(event, []):
                        if sid is None or sid == session_id:
                            cb(event_params)
            return {}

    return FakeCDP


@pytest.fixture
def list_transport(monkeypatch):
    def _install(fired, targets=None):
        monkeypatch.setattr(list_verbs, "CDPClient", _make_list_transport(fired, targets))
        monkeypatch.setattr(list_verbs, "get_ws_url", lambda **kw: "ws://fake/browser")

    return _install


class TestConsoleListFullPath:
    def test_collects_and_renders(self, registry_path, list_transport):
        _seed(registry_path, {"site-01": _entry()})
        list_transport(
            [
                (
                    "Runtime.consoleAPICalled",
                    {"type": "log", "timestamp": 1.0, "args": [{"value": "hi"}]},
                )
            ]
        )
        result = list_verbs.console_list(instance="site-01", duration=0, registry_path=registry_path)
        assert result == [{"type": "log", "text": "hi", "timestamp": 1.0}]

    def test_no_events_returns_empty_list(self, registry_path, list_transport):
        _seed(registry_path, {"site-01": _entry()})
        list_transport([])
        result = list_verbs.console_list(instance="site-01", duration=0, registry_path=registry_path)
        assert result == []

    def test_unknown_instance_is_lifecycle_error(self, registry_path, list_transport):
        _seed(registry_path, {})
        list_transport([])
        with pytest.raises(LifecycleError):
            list_verbs.console_list(instance="ghost", duration=0, registry_path=registry_path)

    def test_instance_omitted_resolves_single(self, registry_path, list_transport):
        _seed(registry_path, {"only-01": _entry()})
        list_transport([("Runtime.consoleAPICalled", {"type": "log", "args": []})])
        result = list_verbs.console_list(instance=None, duration=0, registry_path=registry_path)
        assert result == [{"type": "log", "text": "", "timestamp": None}]


class TestNetworkListFullPath:
    def test_collects_and_renders(self, registry_path, list_transport):
        _seed(registry_path, {"site-01": _entry()})
        list_transport(
            [
                (
                    "Network.requestWillBeSent",
                    {"requestId": "r1", "type": "Document", "request": {"method": "GET", "url": "https://example.com/"}},
                ),
                (
                    "Network.responseReceived",
                    {"requestId": "r1", "type": "Document", "response": {"status": 200, "statusText": "OK", "url": "https://example.com/"}},
                ),
            ]
        )
        result = list_verbs.network_list(instance="site-01", duration=0, registry_path=registry_path)
        assert result == [
            {
                "requestId": "r1",
                "method": "GET",
                "url": "https://example.com/",
                "resourceType": "Document",
                "status": 200,
                "statusText": "OK",
            }
        ]

    def test_no_events_returns_empty_list(self, registry_path, list_transport):
        _seed(registry_path, {"site-01": _entry()})
        list_transport([])
        result = list_verbs.network_list(instance="site-01", duration=0, registry_path=registry_path)
        assert result == []


# ---------------------------------------------------------------------------
# CLI front: dispatch and exit codes
# ---------------------------------------------------------------------------


class TestCliFront:
    @pytest.fixture(autouse=True)
    def _isolate_registry(self, monkeypatch, tmp_path):
        self._registry_path = str(tmp_path / "registry.json")
        monkeypatch.setenv(lifecycle.REGISTRY_ENV_VAR, self._registry_path)

    def _install(self, monkeypatch, fired, targets=None):
        monkeypatch.setattr(list_verbs, "CDPClient", _make_list_transport(fired, targets))
        monkeypatch.setattr(list_verbs, "get_ws_url", lambda **kw: "ws://fake/browser")

    def test_console_list_prints_json_exit_ok(self, monkeypatch, capsys):
        _seed(self._registry_path, {"only-01": _entry()})
        self._install(
            monkeypatch,
            [("Runtime.consoleAPICalled", {"type": "log", "args": [{"value": "hi"}]})],
        )
        rc = cli.main(["console-list", "--duration", "0"])
        assert rc == cli.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out == [{"type": "log", "text": "hi", "timestamp": None}]

    def test_network_list_prints_json_exit_ok(self, monkeypatch, capsys):
        _seed(self._registry_path, {"only-01": _entry()})
        self._install(
            monkeypatch,
            [
                (
                    "Network.requestWillBeSent",
                    {"requestId": "r1", "type": "XHR", "request": {"method": "GET", "url": "https://x/"}},
                )
            ],
        )
        rc = cli.main(["network-list", "--duration", "0"])
        assert rc == cli.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out == [
            {
                "requestId": "r1",
                "method": "GET",
                "url": "https://x/",
                "resourceType": "XHR",
                "status": None,
                "statusText": None,
            }
        ]

    def test_console_list_both_target_and_url_exits_usage(self, monkeypatch, capsys):
        _seed(self._registry_path, {"only-01": _entry()})
        self._install(monkeypatch, [])
        rc = cli.main(["console-list", "--target", "1", "--url", "x"])
        assert rc == cli.EXIT_USAGE
        assert "error:" in capsys.readouterr().err

    def test_network_list_both_target_and_url_exits_usage(self, monkeypatch, capsys):
        _seed(self._registry_path, {"only-01": _entry()})
        self._install(monkeypatch, [])
        rc = cli.main(["network-list", "--target", "1", "--url", "x"])
        assert rc == cli.EXIT_USAGE
        assert "error:" in capsys.readouterr().err

    def test_console_list_unknown_instance_exits_operational(self, monkeypatch):
        _seed(self._registry_path, {"only-01": _entry()})
        self._install(monkeypatch, [])
        rc = cli.main(["console-list", "ghost", "--duration", "0"])
        assert rc == cli.EXIT_OPERATIONAL

    def test_network_list_unknown_instance_exits_operational(self, monkeypatch):
        _seed(self._registry_path, {"only-01": _entry()})
        self._install(monkeypatch, [])
        rc = cli.main(["network-list", "ghost", "--duration", "0"])
        assert rc == cli.EXIT_OPERATIONAL

    def test_console_list_passes_target_slot(self, monkeypatch):
        _seed(self._registry_path, {"only-01": _entry()})
        recorded = {}

        def fake_console_list(**kwargs):
            recorded.update(kwargs)
            return []

        monkeypatch.setattr(list_verbs, "console_list", fake_console_list)
        rc = cli.main(["console-list", "--url", "example.com", "--duration", "0"])
        assert rc == cli.EXIT_OK
        assert recorded["url"] == "example.com"
        assert recorded["duration"] == 0.0
