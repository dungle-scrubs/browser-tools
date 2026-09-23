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
from .usage import UsageError

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
        self.events = TraceEventCounter()
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
                            raw = (
                                base64.b64decode(data)
                                if chunk.get("base64Encoded")
                                else data.encode("utf-8")
                            )
                            output.write(raw)
                            self.events.feed(raw)
                            if chunk["eof"]:
                                break
                finally:
                    await self.cdp.send(
                        "IO.close", {"handle": handle}, session_id=self.session_id, timeout=5
                    )
                return completion
        finally:
            self.cdp.off("Tracing.tracingComplete", self.callback)



class TraceEventCounter:
    """Count ``traceEvents`` entries as bytes stream past, in constant memory.

    The summary used to re-read the finished file with ``json.loads`` to get
    one integer. Measured on a real 1.2 MB Chrome trace: 4.8x the file size in
    peak Python memory, against 0.21x for this counter, and the reviewer
    measured 6.1x on a 21.7 MB trace and 6.4x on a 27.5 MB heap snapshot. Heap
    snapshots of real pages run 50-500 MB, so the cost was worst exactly where
    the verb is most wanted, and ``MemoryError`` is not in the ``except``
    tuple guarding the read, so it escaped as a bare traceback with the
    capture already on disk and its path never reported.
    """

    def __init__(self) -> None:
        self.count = 0
        self._depth = 0
        self._in_string = False
        self._escaped = False
        self._seen_key = False
        self._array_depth: int | None = None
        self._closed = False
        self._tail = b""

    def feed(self, data: bytes) -> None:
        if not self._seen_key:
            probe = self._tail + data
            if b'"traceEvents"' in probe:
                self._seen_key = True
            self._tail = probe[-13:]
        for byte in data:
            self._step(byte)

    def _step(self, byte: int) -> None:
        if self._closed and not self._in_string:
            return
        char = chr(byte)
        if self._in_string:
            if self._escaped:
                self._escaped = False
            elif char == "\\":
                self._escaped = True
            elif char == '"':
                self._in_string = False
            return
        if char == '"':
            self._in_string = True
        elif char == "[":
            if self._seen_key and self._array_depth is None:
                self._array_depth = self._depth
            self._depth += 1
        elif char == "{":
            self._depth += 1
            if self._array_depth is not None and self._depth == self._array_depth + 2:
                self.count += 1
        elif char in "}]":
            self._depth -= 1
            if (
                char == "]"
                and self._array_depth is not None
                and self._depth == self._array_depth
            ):
                # The traceEvents array has closed. Anything after it, such as
                # metadata objects, sits at the same depth and must not count.
                self._closed = True


def _refuse_unwritable(path: Path, verb: str) -> None:
    """Fail before the capture starts if the destination cannot be written."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LifecycleError(f"cannot write captured {verb}: {exc}") from exc
    if path.is_dir():
        raise LifecycleError(f"cannot write captured {verb}: {path} is a directory")
    try:
        with path.open("ab"):
            pass
    except OSError as exc:
        raise LifecycleError(f"cannot write captured {verb}: {exc}") from exc

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
    all_frames: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Write Chrome's unmodified trace and return its summary and run outcome."""
    if duration is not None and timeout == 0:
        # `run` and `wait` both document --timeout 0 as no whole-run deadline.
        # --duration has to bound the run as well, because one thread cannot
        # close the trace on time otherwise, so the two contradict each other.
        # Silently letting --duration win turned an explicit opt-out into a
        # deadline, and a run that outlived it reported status "timeout".
        raise UsageError(
            "trace --timeout 0 means no deadline, which contradicts --duration. "
            "Pass one or the other."
        )
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
    # Before Tracing.start, not after Tracing.end. An unwritable --out used to
    # be discovered only when the capture was already finished, so a
    # `--duration 60` run spent the whole minute and then threw the trace away
    # for a condition knowable at argument-parse time. This is the same rule
    # the RFC states for --steps: no Tracing.start, no output file.
    _refuse_unwritable(path, "trace")
    document: dict[str, Any] = {}
    succeeded = True
    ended_by = "duration"
    session = (
        curated.run_session(instance, target, None, registry_path, endpoint, all_frames)
        if steps is not None
        else curated.capture_session(instance, target, registry_path, endpoint)
    )
    with session as handler:
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
    document["trace"] = {
        "path": str(path),
        "events": capture.events.count,
        "bytes": path.stat().st_size,
        # Protocol-required, but a missing key used to be a raw KeyError with
        # the trace already on disk and its path never reported.
        "dataLossOccurred": completion.get("dataLossOccurred"),
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



# A heap snapshot writes "snapshot" as its first key, so its counts are
# reachable from a bounded prefix. Reading the whole file to get two integers
# peaked at 6.4x the file size in measurement, on files that run 50-500 MB for
# a real page.
_SNAPSHOT_PREFIX_BYTES = 1 << 16


def _heap_summary(path: Path) -> dict[str, Any]:
    """Read node_count and edge_count without loading the snapshot."""
    with path.open("rb") as handle:
        prefix = handle.read(_SNAPSHOT_PREFIX_BYTES)
    text = prefix.decode("utf-8", errors="replace")
    marker = text.find('"snapshot"')
    if marker == -1:
        raise LifecycleError("cannot read heap snapshot: no snapshot header")
    start = text.find("{", marker + len('"snapshot"'))
    if start == -1:
        raise LifecycleError("cannot read heap snapshot: malformed snapshot header")
    try:
        snapshot, _ = json.JSONDecoder().raw_decode(text, start)
    except ValueError as exc:
        raise LifecycleError(f"cannot read heap snapshot: {exc}") from exc
    try:
        return {"nodes": snapshot["node_count"], "edges": snapshot["edge_count"]}
    except KeyError as exc:
        raise LifecycleError(f"cannot read heap snapshot: missing {exc}") from exc

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
    _refuse_unwritable(path, "heap snapshot")
    # Write beside the destination and rename on success. path.open("w") used
    # to truncate before takeHeapSnapshot was called, so a failed capture left
    # a partial file where a good snapshot had been.
    staged = path.with_name(path.name + ".partial")
    scope = (
        contextlib.nullcontext(handler)
        if handler is not None
        else curated.capture_session(instance, target, registry_path, endpoint)
    )
    try:
        with scope as opened:
            cdp, session_id = opened.require_session()
            chunks = opened.submit(_heap(cdp, session_id, staged, opened.caller_enabled))
        summary = _heap_summary(staged)
        staged.replace(path)
        return {
            "path": str(path),
            "nodes": summary["nodes"],
            "edges": summary["edges"],
            "bytes": path.stat().st_size,
            "chunks": chunks,
        }
    except (OSError, ValueError, KeyError) as exc:
        raise LifecycleError(f"cannot write heap snapshot: {exc}") from exc
    finally:
        staged.unlink(missing_ok=True)
