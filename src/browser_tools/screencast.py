"""Screencast capture state machine, extracted from CDPHandler.

Owns the Page.startScreencast frame buffer, the flow-control ack that keeps the
stream moving, and the write-to-dir on stop. CDPHandler holds one instance and
routes screencast_start / screencast_stop to it, so the capture state no longer
sits interleaved with sixteen unrelated handlers.

The recorder is constructed without a CDP client and bound to one on each
start(); the frame callback the CDP read loop fires (:meth:`on_frame`) acks via
the bound client. Acks are scheduled with ``ensure_future`` rather than awaited,
because the read loop is what resolves the ack's response - awaiting inline
would deadlock. The recorder holds every in-flight ack task, because the event
loop keeps only a weak reference: an unheld task can be garbage-collected
mid-flight, and the ack then never happens, stalling the stream through CDP
flow control.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

try:
    from .mcp_response import make_error, make_text
except ImportError:  # script-mode execution (mcp_daemon run directly)
    from mcp_response import make_error, make_text  # type: ignore[import-untyped,no-redef]

logger = logging.getLogger(__name__)


def _cdp_error_class() -> type[Exception]:
    """Import CDPError lazily.

    cdp_client uses a relative import of its own (chrome_utils) that fails
    outside package context, so importing it at module load breaks the daemon
    script's --help. Deferring until a capture actually starts keeps this module
    importable in all execution modes - the same trick cdp_handler uses.
    """
    try:
        from .cdp_client import CDPError
    except ImportError:
        from cdp_client import CDPError  # type: ignore[import-untyped,no-redef]
    return CDPError


class ScreencastRecorder:
    """Buffer painted frames between screencast_start and screencast_stop.

    Attributes:
        active: Whether a capture is in progress (set by start, cleared by stop
            or a failed start).
    """

    def __init__(self) -> None:
        self._active: bool = False
        self._frames: list[dict[str, Any]] = []
        self._max_frames: int = 600
        self._format: str = "jpeg"
        self._cdp: Any = None
        self._acks: set[asyncio.Task[None]] = set()

    @property
    def active(self) -> bool:
        """Whether a screencast capture is currently in progress."""
        return self._active

    @property
    def frame_count(self) -> int:
        """How many frames are buffered right now.

        A bounded capture (``bt screencast --dir DIR``) polls this so it can
        stop as soon as ``--max-frames`` is reached instead of waiting out the
        whole ``--duration`` against a stream CDP flow control has paused.
        """
        return len(self._frames)

    @property
    def pending_acks(self) -> int:
        """How many frame acks are in flight.

        Zero outside a capture, and zero again once :meth:`stop` returns.
        """
        return len(self._acks)

    def on_frame(self, params: dict[str, Any]) -> None:
        """Buffer one frame and ack it so the stream continues.

        Called from the CDP read loop, so this stays synchronous. Once the
        buffer is full we stop acking, which pauses the stream (CDP flow
        control) instead of growing memory without bound.
        """
        if not self._active:
            return
        if len(self._frames) >= self._max_frames:
            return
        self._frames.append(
            {
                "data": params.get("data", ""),
                "timestamp": params.get("metadata", {}).get("timestamp"),
            }
        )
        session_id = params.get("sessionId")
        cdp = self._cdp
        if session_id is not None and cdp is not None:
            task = asyncio.ensure_future(self._ack(cdp, session_id))
            self._acks.add(task)
            task.add_done_callback(self._acks.discard)

    async def _ack(self, cdp: Any, session_id: int) -> None:
        """Ack a frame; failures are best-effort (the stream will retry)."""
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            logger.debug("screencast ack failed", exc_info=True)

    async def _drain_acks(self) -> None:
        """Wait out every in-flight ack, so none outlives the capture.

        An ack still pending when the capture ends has nothing left to ack.
        Cancel it and collect it, rather than leave a task the loop will
        later report as destroyed while pending.
        """
        pending = list(self._acks)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._acks.clear()

    def _unsubscribe(self, cdp: Any) -> None:
        """Tear down a started capture's subscription and active flag."""
        self._active = False
        cdp.off("Page.screencastFrame", self.on_frame)

    async def start(self, cdp: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """Start buffering every painted frame via Page.startScreencast.

        Args:
            cdp: Connected CDPClient.
            arguments: Optional 'format' (jpeg|png), 'quality' (0-100),
                'every_nth_frame', 'max_frames', 'max_width', 'max_height'.

        Returns:
            JSON-RPC style response dict.
        """
        if self._active:
            return make_error(
                "a screencast is already recording in this process. "
                "One 'screencast' invocation captures and writes its own frames; "
                "there is no separate start and stop."
            )

        fmt = str(arguments.get("format", "jpeg")).lower()
        if fmt not in ("jpeg", "png"):
            return make_error("format must be 'jpeg' or 'png'")

        self._frames = []
        self._acks = set()
        self._format = fmt
        self._max_frames = max(1, int(arguments.get("max_frames", 600)))
        self._active = True
        self._cdp = cdp
        cdp.on("Page.screencastFrame", self.on_frame)

        params: dict[str, Any] = {
            "format": fmt,
            "everyNthFrame": max(1, int(arguments.get("every_nth_frame", 1))),
        }
        if fmt == "jpeg":
            params["quality"] = int(arguments.get("quality", 80))
        if arguments.get("max_width"):
            params["maxWidth"] = int(arguments["max_width"])
        if arguments.get("max_height"):
            params["maxHeight"] = int(arguments["max_height"])

        try:
            await cdp.send("Page.startScreencast", params)
        except _cdp_error_class() as exc:
            self._unsubscribe(cdp)
            return make_error(f"Page.startScreencast failed: {exc}")
        except Exception:
            logger.exception("Unexpected error in Page.startScreencast")
            self._unsubscribe(cdp)
            return make_error("Page.startScreencast failed")
        return make_text(
            "Screencast recording. Frames are buffered in this process and written "
            "when the capture ends at its duration or its frame cap."
        )

    async def stop(self, cdp: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        """Stop the screencast and write buffered frames to a directory.

        Args:
            cdp: Connected CDPClient.
            arguments: 'dir' (required) -- directory to write timestamped frames
                plus a frames.json manifest.

        Returns:
            JSON-RPC style response dict.
        """
        if not self._active:
            return make_error(
                "no screencast is recording in this process. The frame buffer is "
                "process-local, so a capture started elsewhere cannot be stopped here."
            )

        self._active = False
        with contextlib.suppress(Exception):
            await cdp.send("Page.stopScreencast")  # best-effort
        cdp.off("Page.screencastFrame", self.on_frame)
        await self._drain_acks()

        frames = self._frames
        self._frames = []
        truncated = len(frames) >= self._max_frames
        ext = "jpg" if self._format == "jpeg" else "png"

        lines = [f"Captured {len(frames)} frames."]
        if truncated:
            lines.append(
                f"Note: hit max_frames={self._max_frames}; "
                "capture may be truncated (raise max_frames or every_nth_frame)."
            )

        out_dir = str(arguments.get("dir", "")).strip()
        if not out_dir:
            return make_error("dir is required to write screencast frames")
        try:
            base = Path(out_dir).resolve()
            base.mkdir(parents=True, exist_ok=True)
            manifest = []
            for i, frame in enumerate(frames):
                fname = f"frame_{i:05d}.{ext}"
                (base / fname).write_bytes(base64.b64decode(frame["data"]))
                manifest.append({"file": fname, "timestamp": frame["timestamp"]})
            (base / "frames.json").write_text(json.dumps(manifest, indent=2))
            lines.append(f"Wrote {len(frames)} frames + frames.json to {base}")
        except Exception:
            logger.exception("could not write screencast frames")
            return make_error("could not write frames")
        return make_text("\n".join(lines))


__all__ = ["ScreencastRecorder"]
