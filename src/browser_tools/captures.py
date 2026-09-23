"""Bounded trace and heap captures over the invocation's attached session."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from pathlib import Path
from typing import Any

from . import curated, step_list, step_run
from .lifecycle import LifecycleError
from .one_shot import cli_cdp_errors, domains_enabled

DEFAULT_CATEGORIES: tuple[str, ...] = (
    "-*",
    "blink.console",
    "blink.user_timing",
    "devtools.timeline",
    "disabled-by-default-devtools.screenshot",
    "disabled-by-default-devtools.timeline",
    "disabled-by-default-devtools.timeline.frame",
    "disabled-by-default-devtools.timeline.stack",
    "disabled-by-default-v8.cpu_profiler",
    "disabled-by-default-v8.cpu_profiler.hires",
    "latencyInfo",
    "loading",
    "disabled-by-default-lighthouse",
    "v8.execute",
    "v8",
)
CAPTURE_TIMEOUT = 120.0


class TraceCapture:
    """Own the completion subscription and stream until both are released."""

    def __init__(self, cdp: Any, session_id: str) -> None:
        self.cdp = cdp
        self.session_id = session_id
        self.complete: asyncio.Future[dict[str, Any]] | None = None
        # Keep the same callback object for CDPClient.off's identity comparison.
        self.callback = self._on_complete

    def _on_complete(self, params: dict[str, Any]) -> None:
        if self.complete is not None and not self.complete.done():
            self.complete.set_result(params)

    async def start(self, categories: list[str]) -> None:
        self.complete = asyncio.get_running_loop().create_future()
        self.cdp.on("Tracing.tracingComplete", self.callback, self.session_id)
        try:
            await self.cdp.send(
                "Tracing.start",
                {
                    "traceConfig": {
                        "includedCategories": [c for c in categories if not c.startswith("-")],
                        "excludedCategories": [c[1:] for c in categories if c.startswith("-")],
                        "recordMode": "recordUntilFull",
                    },
                    "transferMode": "ReturnAsStream",
                    "streamFormat": "json",
                    "streamCompression": "none",
                },
                session_id=self.session_id,
                timeout=CAPTURE_TIMEOUT,
            )
        except BaseException:
            self.cdp.off("Tracing.tracingComplete", self.callback)
            raise

    async def finish(self, path: Path) -> dict[str, Any]:
        try:
            async with asyncio.timeout(CAPTURE_TIMEOUT):
                await self.cdp.send("Tracing.end", session_id=self.session_id)
                assert self.complete is not None
                completion = await self.complete
                handle = completion.get("stream")
                if not handle:
                    raise LifecycleError("Tracing completed without a stream")
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("wb") as output:
                        while True:
                            chunk = await self.cdp.send(
                                "IO.read", {"handle": handle}, session_id=self.session_id
                            )
                            data = chunk["data"]
                            output.write(
                                base64.b64decode(data)
                                if chunk.get("base64Encoded")
                                else data.encode("utf-8")
                            )
                            if chunk["eof"]:
                                break
                finally:
                    await self.cdp.send(
                        "IO.close", {"handle": handle}, session_id=self.session_id, timeout=5
                    )
                return completion
        finally:
            self.cdp.off("Tracing.tracingComplete", self.callback)


@cli_cdp_errors
def trace(
    *,
    instance: str | None,
    out: str,
    duration: float | None = None,
    source: str | None = None,
    categories: list[str] | None = None,
    timeout: float | None = None,
    target: str | None = None,
    endpoint: str | None = None,
    registry_path: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Write Chrome's unmodified trace and return its summary and run outcome."""
    steps = (
        None
        if source is None
        else step_list.validate(
            step_run.read_source(source),
            registry_path=registry_path,
            endpoint=endpoint,
        )
    )
    selected = list(DEFAULT_CATEGORIES) if categories is None else categories
    path = Path(out).resolve()
    document: dict[str, Any] = {}
    succeeded = True
    ended_by = "duration"
    with curated.run_session(instance, target, None, registry_path, endpoint) as handler:
        capture = TraceCapture(*handler.require_session())
        try:
            handler.submit(capture.start(selected))
        except TimeoutError as exc:
            raise LifecycleError("Tracing.start timed out") from exc
        started = time.monotonic()
        try:
            if steps is None:
                assert duration is not None
                time.sleep(duration)
            else:
                bound = timeout or None
                duration_first = duration is not None and (bound is None or duration <= bound)
                if duration_first:
                    bound = duration
                run, succeeded = step_run.execute(steps, handler, registry_path, bound)
                document["run"] = run
                ended_by = (
                    ("duration" if duration_first else "timeout")
                    if (run["run"]["status"] == step_run.STATUS_TIMEOUT)
                    else "steps"
                )
        finally:
            elapsed = (time.monotonic() - started) * 1000
            try:
                completion = handler.submit(capture.finish(path))
            except OSError as exc:
                raise LifecycleError(f"cannot write captured trace: {exc}") from exc
    try:
        events = len(json.loads(path.read_bytes())["traceEvents"])
    except (OSError, ValueError, KeyError) as exc:
        raise LifecycleError(f"cannot read captured trace: {exc}") from exc
    document["trace"] = {
        "path": str(path),
        "events": events,
        "bytes": path.stat().st_size,
        "dataLossOccurred": completion["dataLossOccurred"],
        "durationMs": elapsed,
        "endedBy": ended_by,
        "categories": selected,
    }
    return document, succeeded


async def _heap(cdp: Any, session_id: str, path: Path, keep: frozenset[str]) -> int:
    chunks = 0
    write_error: OSError | None = None
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:

        def on_chunk(params: dict[str, Any]) -> None:
            nonlocal chunks, write_error
            chunks += 1
            if write_error is None:
                try:
                    output.write(params["chunk"])
                except OSError as exc:
                    write_error = exc

        cdp.on("HeapProfiler.addHeapSnapshotChunk", on_chunk, session_id)
        try:
            async with domains_enabled(cdp, session_id, ["HeapProfiler"], keep):
                await cdp.send(
                    "HeapProfiler.takeHeapSnapshot",
                    {},
                    session_id=session_id,
                    timeout=CAPTURE_TIMEOUT,
                )
            if write_error is not None:
                raise write_error
        finally:
            cdp.off("HeapProfiler.addHeapSnapshotChunk", on_chunk)
    return chunks


@cli_cdp_errors
def heap(
    *,
    instance: str | None,
    out: str,
    target: str | None = None,
    endpoint: str | None = None,
    registry_path: str | None = None,
    handler: Any = None,
) -> dict[str, Any]:
    """Write a heap snapshot, subscribing before its synchronous chunk delivery."""
    path = Path(out).resolve()
    scope = (
        contextlib.nullcontext(handler)
        if handler is not None
        else curated.run_session(
            instance,
            target,
            None,
            registry_path,
            endpoint,
        )
    )
    try:
        with scope as opened:
            cdp, session_id = opened.require_session()
            chunks = opened.submit(_heap(cdp, session_id, path, opened.caller_enabled))
        snapshot = json.loads(path.read_bytes())["snapshot"]
        return {
            "path": str(path),
            "nodes": snapshot["node_count"],
            "edges": snapshot["edge_count"],
            "bytes": path.stat().st_size,
            "chunks": chunks,
        }
    except (OSError, ValueError, KeyError) as exc:
        raise LifecycleError(f"cannot write heap snapshot: {exc}") from exc
