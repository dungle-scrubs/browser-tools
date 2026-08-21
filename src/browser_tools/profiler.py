#!/usr/bin/env python3
"""
Chrome CPU Profiler via CDP

Keeps its own long-lived process and the ``browser-tools-profiler`` entry
point (RFC-01, "Profiling"). Target discovery and CDP transport go through
the vendored ``core.cdp_client`` -- the same client every other adapted
module uses -- instead of a standalone websocket/protocol path. The
``Profiler``/``Performance`` CDP calls go through the generated
``core.domains`` bindings over that same client.

This module MUST NOT depend on the MCP front or the daemon: it has no
imports from ``cli``, ``mcp_server``, ``daemon_supervisor``, or
``tool_registry``, and runs standalone against a browser's CDP port.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from .core.cdp_client import CDPClient, get_ws_url
from .core.domains.performance import Performance
from .core.domains.profiler import Profiler

logger = logging.getLogger(__name__)


class CDPProfiler:
    """CPU profiling session over the vendored core CDP client.

    Wraps ``core.cdp_client.CDPClient`` for the websocket transport and the
    generated ``core.domains.profiler.Profiler`` / ``core.domains.performance.
    Performance`` domain bindings for the CDP calls themselves.
    """

    def __init__(self, ws_url: str) -> None:
        self.ws_url = ws_url
        self.cdp: CDPClient | None = None
        self.profiler: Profiler | None = None
        self.performance: Performance | None = None
        self.profile: dict[str, Any] | None = None

    async def connect(self) -> None:
        self.cdp = CDPClient(ws_url=self.ws_url)
        await self.cdp.connect()
        self.profiler = Profiler(self.cdp)
        self.performance = Performance(self.cdp)

    def _require_profiler(self) -> Profiler:
        if self.profiler is None:
            raise RuntimeError("Not connected")
        return self.profiler

    def _require_performance(self) -> Performance:
        if self.performance is None:
            raise RuntimeError("Not connected")
        return self.performance

    async def start_profiling(self) -> None:
        """Enable and start the profiler."""
        profiler = self._require_profiler()
        await profiler.enable()
        await profiler.start()

    async def stop_profiling(self) -> dict[str, Any]:
        """Stop the profiler and return the collected profile."""
        result = await self._require_profiler().stop()
        self.profile = result.get("profile", {})
        return self.profile if self.profile is not None else {}

    async def enable_performance_metrics(self) -> None:
        await self._require_performance().enable()

    async def get_performance_metrics(self) -> dict[str, Any]:
        return await self._require_performance().get_metrics()

    async def close(self) -> None:
        if self.cdp is not None:
            await self.cdp.close()


def analyze_profile(profile: dict[str, Any], top_n: int = 20) -> list[dict[str, Any]]:
    """
    Analyze CPU profile and return top functions by self-time

    Returns list of {name, url, line, selfTime, totalTime, hitCount}
    """
    nodes = profile.get("nodes", [])
    samples = profile.get("samples", [])
    time_deltas = profile.get("timeDeltas", [])

    if not nodes or not samples:
        return []

    # Calculate time per node from samples
    node_times = {}
    for i, node_id in enumerate(samples):
        delta = time_deltas[i] if i < len(time_deltas) else 0
        node_times[node_id] = node_times.get(node_id, 0) + delta

    # Build results
    results = []
    for node in nodes:
        node_id = node["id"]
        call_frame = node.get("callFrame", {})

        function_name = call_frame.get("functionName", "(anonymous)")
        url = call_frame.get("url", "")
        line = call_frame.get("lineNumber", 0)

        self_time = node_times.get(node_id, 0)
        hit_count = node.get("hitCount", 0)

        if self_time > 0 or hit_count > 0:
            results.append(
                {
                    "name": function_name or "(anonymous)",
                    "url": url,
                    "line": line + 1,  # 1-indexed for display
                    "selfTime": self_time / 1000,  # Convert to ms
                    "hitCount": hit_count,
                }
            )

    # Sort by self time descending
    results.sort(key=lambda x: x["selfTime"], reverse=True)  # type: ignore[reportUnknownLambdaType]
    return results[:top_n]


def format_results(results: list[dict[str, Any]], format_type: str = "text") -> str:
    """Format profile results for output"""
    if format_type == "json":
        return json.dumps(results, indent=2)

    if not results:
        return "No profile data collected"

    lines = ["Top functions by self-time:\n"]
    lines.append(f"{'Function':<40} {'Time (ms)':<12} {'Hits':<8} {'Location'}")
    lines.append("-" * 100)

    for r in results:
        name = r["name"][:38] + ".." if len(r["name"]) > 40 else r["name"]
        url = r["url"].split("/")[-1] if r["url"] else ""
        location = f"{url}:{r['line']}" if url else ""
        lines.append(f"{name:<40} {r['selfTime']:<12.2f} {r['hitCount']:<8} {location}")

    return "\n".join(lines)


async def profile_page(duration: float = 5.0, port: int = 9222, format_type: str = "text") -> str:
    """
    Profile the current page for specified duration

    Args:
        duration: How long to profile in seconds
        port: Chrome remote debugging port
        format_type: Output format (text or json)

    Returns:
        Formatted profile results
    """
    ws_url = get_ws_url(port=port, target_type="page")
    profiler = CDPProfiler(ws_url)

    try:
        await profiler.connect()
        logger.info("Starting profiler for %ds...", duration)
        await profiler.start_profiling()

        await asyncio.sleep(duration)

        logger.info("Stopping profiler...")
        profile = await profiler.stop_profiling()

        results = analyze_profile(profile)
        return format_results(results, format_type)

    finally:
        await profiler.close()


async def profile_until_high_cpu(
    threshold: float = 80.0,
    timeout: float = 60.0,
    sample_window: float = 3.0,
    port: int = 9222,
    format_type: str = "text",
) -> str:
    """
    Start profiling, wait for high CPU, then capture

    Args:
        threshold: CPU percentage threshold to trigger capture
        timeout: Maximum wait time in seconds
        sample_window: How long to profile after detecting high CPU
        port: Chrome remote debugging port
        format_type: Output format

    Returns:
        Formatted profile results
    """
    ws_url = get_ws_url(port=port, target_type="page")
    profiler = CDPProfiler(ws_url)

    try:
        await profiler.connect()

        # Enable performance metrics
        await profiler.enable_performance_metrics()
        await profiler.start_profiling()

        logger.info("Profiling... waiting for CPU > %d%% (timeout: %ds)", threshold, timeout)
        logger.info("Trigger the freeze now!")

        start = time.time()
        last_timestamp = None

        while time.time() - start < timeout:
            metrics = await profiler.get_performance_metrics()

            # Look for TaskDuration metric increase
            for metric in metrics.get("metrics", []):
                if metric["name"] == "TaskDuration":
                    current = metric["value"]
                    if last_timestamp is not None:
                        # High task duration increase indicates CPU spike
                        delta = current - last_timestamp
                        if delta > 0.5:  # More than 500ms of task time in sample period
                            logger.info("High CPU detected! Capturing for %ds...", sample_window)
                            await asyncio.sleep(sample_window)
                            profile = await profiler.stop_profiling()
                            results = analyze_profile(profile)
                            return format_results(results, format_type)
                    last_timestamp = current

            await asyncio.sleep(0.5)

        # Timeout - return what we have
        logger.info("Timeout reached, returning profile...")
        profile = await profiler.stop_profiling()
        results = analyze_profile(profile)
        return format_results(results, format_type)

    finally:
        await profiler.close()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Chrome CPU Profiler")
    parser.add_argument("--port", type=int, default=9222, help="Chrome debug port")
    parser.add_argument("--format", choices=["text", "json"], default="text")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Timed profile
    timed = subparsers.add_parser("timed", help="Profile for fixed duration")
    timed.add_argument("--duration", type=float, default=5.0, help="Duration in seconds")

    # Wait for high CPU
    watch = subparsers.add_parser("watch", help="Wait for high CPU then capture")
    watch.add_argument("--threshold", type=float, default=80.0, help="CPU threshold percentage")
    watch.add_argument("--timeout", type=float, default=60.0, help="Max wait time")
    watch.add_argument("--window", type=float, default=3.0, help="Capture window after spike")

    args = parser.parse_args()

    if args.command == "timed":
        result = asyncio.run(
            profile_page(duration=args.duration, port=args.port, format_type=args.format)
        )
        print(result)
    elif args.command == "watch":
        result = asyncio.run(
            profile_until_high_cpu(
                threshold=args.threshold,
                timeout=args.timeout,
                sample_window=args.window,
                port=args.port,
                format_type=args.format,
            )
        )
        print(result)


if __name__ == "__main__":
    main()
