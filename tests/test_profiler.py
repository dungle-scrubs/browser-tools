"""Tests for the CPU profiler rebuilt over the core CDP client (RFC-01 #46).

The profiler keeps its own long-lived process and the
``browser-tools-profiler`` entry point, and MUST NOT depend on the MCP front
or the daemon. These tests exercise it against a fake
``core.cdp_client.CDPClient``/``get_ws_url`` pair -- mirroring the fake
transport double in tests/test_passthrough.py -- so no real browser,
websocket, MCP front, or daemon process is ever involved.
"""

from __future__ import annotations

import json

import pytest

from browser_tools import profiler
from browser_tools.profiler import CDPProfiler, analyze_profile, format_results

# ---------------------------------------------------------------------------
# Fake CDP transport -- shared client `send` path double
# ---------------------------------------------------------------------------

FAKE_PROFILE = {
    "nodes": [
        {
            "id": 1,
            "callFrame": {"functionName": "(root)", "url": "", "lineNumber": 0},
            "hitCount": 0,
        },
        {
            "id": 2,
            "callFrame": {
                "functionName": "hot_function",
                "url": "https://example.com/app.js",
                "lineNumber": 41,
            },
            "hitCount": 3,
        },
    ],
    "samples": [1, 2, 2, 2],
    "timeDeltas": [0, 1000, 1000, 1000],
}


def make_fake_cdp_client_cls(calls: list[tuple[str, dict | None]], metrics_sequence=None):
    """Build a fake ``core.cdp_client.CDPClient`` recording every ``send``.

    Answers ``Profiler.*``/``Performance.*`` the way a real browser would,
    so tests can assert on the exact CDP call sequence the profiler issued
    -- proving it reaches the shared client ``send`` path, not just that a
    wrapper ran.
    """
    metrics_iter = iter(metrics_sequence or [])

    class FakeCDPClient:
        def __init__(self, ws_url):
            self.ws_url = ws_url
            self.connected = False

        async def connect(self):
            self.connected = True

        async def send(self, method, params=None, session_id=None):
            calls.append((method, params))
            if method == "Profiler.stop":
                return {"profile": FAKE_PROFILE}
            if method == "Performance.getMetrics":
                try:
                    metrics = next(metrics_iter)
                except StopIteration:
                    metrics = []
                return {"metrics": metrics}
            return {}

        async def close(self):
            self.connected = False

    return FakeCDPClient


@pytest.fixture
def fake_transport(monkeypatch):
    """Install a fake CDPClient/get_ws_url pair and return its call log."""

    def _install(metrics_sequence=None):
        calls: list[tuple[str, dict | None]] = []
        ws_urls: list[tuple[int, str]] = []
        fake_cls = make_fake_cdp_client_cls(calls, metrics_sequence=metrics_sequence)
        monkeypatch.setattr(profiler, "CDPClient", fake_cls)

        def _fake_get_ws_url(*, port, target_type):
            ws_urls.append((port, target_type))
            return f"ws://fake/{target_type}/{port}"

        monkeypatch.setattr(profiler, "get_ws_url", _fake_get_ws_url)
        return calls, ws_urls

    return _install


# ---------------------------------------------------------------------------
# Pure functions -- unchanged by the transport migration
# ---------------------------------------------------------------------------


class TestAnalyzeProfile:
    def test_computes_self_time_and_hit_count(self):
        results = analyze_profile(FAKE_PROFILE)
        assert len(results) == 1
        assert results[0]["name"] == "hot_function"
        assert results[0]["hitCount"] == 3
        assert results[0]["selfTime"] == 3.0  # 3000us of samples -> 3ms
        assert results[0]["line"] == 42  # 1-indexed

    def test_empty_profile_yields_no_results(self):
        assert analyze_profile({}) == []


class TestFormatResults:
    def test_json_format_round_trips(self):
        results = analyze_profile(FAKE_PROFILE)
        parsed = json.loads(format_results(results, "json"))
        assert parsed == results

    def test_text_format_lists_no_data_message(self):
        assert format_results([], "text") == "No profile data collected"

    def test_text_format_includes_function_name(self):
        results = analyze_profile(FAKE_PROFILE)
        assert "hot_function" in format_results(results, "text")


# ---------------------------------------------------------------------------
# CDPProfiler -- transport wiring over the core client
# ---------------------------------------------------------------------------


class TestCDPProfilerTransport:
    @pytest.mark.asyncio
    async def test_connect_uses_core_cdp_client(self, fake_transport):
        fake_transport()
        cp = CDPProfiler("ws://fake/page/9222")
        await cp.connect()
        assert cp.cdp is not None
        assert cp.cdp.connected
        await cp.close()
        assert not cp.cdp.connected

    @pytest.mark.asyncio
    async def test_start_profiling_sends_enable_then_start(self, fake_transport):
        calls, _ = fake_transport()
        cp = CDPProfiler("ws://fake/page/9222")
        await cp.connect()
        await cp.start_profiling()
        assert calls == [("Profiler.enable", None), ("Profiler.start", None)]

    @pytest.mark.asyncio
    async def test_stop_profiling_returns_profile_data(self, fake_transport):
        fake_transport()
        cp = CDPProfiler("ws://fake/page/9222")
        await cp.connect()
        profile = await cp.stop_profiling()
        assert profile == FAKE_PROFILE

    @pytest.mark.asyncio
    async def test_methods_require_connect_first(self):
        cp = CDPProfiler("ws://fake/page/9222")
        with pytest.raises(RuntimeError, match="Not connected"):
            await cp.start_profiling()
        with pytest.raises(RuntimeError, match="Not connected"):
            await cp.stop_profiling()
        with pytest.raises(RuntimeError, match="Not connected"):
            await cp.enable_performance_metrics()
        with pytest.raises(RuntimeError, match="Not connected"):
            await cp.get_performance_metrics()


# ---------------------------------------------------------------------------
# Smoke test (RFC-01 Testing Strategy: "Profiler smoke test (Phase 3)")
# ---------------------------------------------------------------------------


class TestProfilerSmoke:
    @pytest.mark.asyncio
    async def test_start_profile_stop_artifact_exists_no_daemon(self, fake_transport):
        """Start -> profile a page -> stop -> a profile artifact exists.

        Runs profiler.profile_page end-to-end against the fake core
        transport: target discovery (get_ws_url) resolves a page target,
        Profiler.enable/start/stop is the CDP call sequence issued, and the
        formatted result carries real (fake) profile data. Nothing in this
        path touches cli.py, a daemon, or an MCP front -- profiler.py has no
        import of any of them (see module docstring), and this test never
        starts one.
        """
        calls, ws_urls = fake_transport()

        result = await profiler.profile_page(duration=0, port=9222, format_type="json")

        # Target discovery went through core.cdp_client.get_ws_url for a
        # page target on the requested port.
        assert ws_urls == [(9222, "page")]

        # The CDP call sequence a profiling session issues.
        assert [m for m, _ in calls] == ["Profiler.enable", "Profiler.start", "Profiler.stop"]

        # The artifact: real profile data, formatted and non-empty.
        parsed = json.loads(result)
        assert parsed
        assert parsed[0]["name"] == "hot_function"
        assert parsed[0]["hitCount"] == 3

    @pytest.mark.asyncio
    async def test_watch_stops_on_high_cpu_and_produces_artifact(self, fake_transport):
        """profile_until_high_cpu detects a CPU spike and captures a profile.

        Two Performance.getMetrics polls: a baseline, then a reading whose
        TaskDuration delta crosses the 0.5s trigger. Proves the watch path
        also runs entirely over the fake core transport, no daemon involved.
        """
        metrics_sequence = [
            [{"name": "TaskDuration", "value": 0.0}],
            [{"name": "TaskDuration", "value": 0.9}],
        ]
        calls, ws_urls = fake_transport(metrics_sequence=metrics_sequence)

        result = await profiler.profile_until_high_cpu(
            threshold=80.0,
            timeout=5.0,
            sample_window=0,
            port=9333,
            format_type="json",
        )

        assert ws_urls == [(9333, "page")]
        methods = [m for m, _ in calls]
        assert methods[:3] == ["Performance.enable", "Profiler.enable", "Profiler.start"]
        assert methods.count("Performance.getMetrics") == 2
        assert methods[-1] == "Profiler.stop"

        parsed = json.loads(result)
        assert parsed
        assert parsed[0]["name"] == "hot_function"
