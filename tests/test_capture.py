"""Capture keeps received frames private and never replaces existing output."""

import base64
import json
import os
import select
import signal
import struct
import subprocess
import sys
import zlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from browser_tools.mcp_response import extract_text_items
from browser_tools.screencast import ScreencastRecorder


@pytest.mark.asyncio
async def test_capture_does_not_overwrite_existing_frames(tmp_path):
    cdp = MagicMock()
    cdp.send = AsyncMock(return_value={})
    recorder = ScreencastRecorder()
    await recorder.start(cdp, {})
    recorder.on_frame({"data": base64.b64encode(b"new frame").decode()})
    destination = tmp_path / "frame_00000.jpg"
    destination.write_bytes(b"existing frame")
    result = await recorder.stop(cdp, {"dir": str(tmp_path)})
    assert destination.read_bytes() == b"existing frame"
    assert not (tmp_path / "frames.json").exists()
    assert "could not write" in " ".join(extract_text_items(result))


@pytest.mark.parametrize("finish", ["sigint", "sigterm", "duration", "frame-limit"])
def test_capture_cli_writes_frames_after_a_second_cli_drives_page(tmp_path, finish):
    env = dict(os.environ, BROWSER_TOOLS_REGISTRY=str(tmp_path / "registry.json"))
    command = [sys.executable, "-m", "browser_tools.cli"]

    def invoke(*args):
        return subprocess.run(
            [*command, *args], env=env, capture_output=True, text=True, timeout=40
        )

    launched = invoke("launch", "--headless", "--no-window-border")
    assert launched.returncode == 0, launched.stderr
    name = json.loads(launched.stdout)["name"]
    process = None
    try:
        output = tmp_path / "frames"
        limits = {"duration": ["--duration", "1"], "frame-limit": ["--max-frames", "1"]}.get(
            finish, []
        )
        process = subprocess.Popen(
            [
                *command,
                "screencast",
                "record",
                name,
                "--dir",
                str(output),
                "--format",
                "png",
                *limits,
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stderr is not None
        assert select.select([process.stderr], [], [], 15)[0], "Capture never became ready"
        assert "Recording;" in process.stderr.readline()
        driven = invoke(
            name,
            "Runtime.evaluate",
            json.dumps(
                {
                    "expression": "document.body.textContent='Capture integration'; setInterval(()=>document.body.textContent=String(Date.now()),50)"
                }
            ),
        )
        assert driven.returncode == 0, driven.stderr
        if finish in ("sigint", "sigterm"):
            process.send_signal(signal.SIGINT if finish == "sigint" else signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        assert "result" in json.loads(stdout)
        manifest = json.loads((output / "frames.json").read_text())
        assert manifest
        for entry in manifest:
            frame = output / entry["file"]
            data = frame.read_bytes()
            assert data.startswith(b"\x89PNG\r\n\x1a\n")
            offset = 8
            compressed = bytearray()
            while offset < len(data):
                size = struct.unpack_from(">I", data, offset)[0]
                if data[offset + 4 : offset + 8] == b"IDAT":
                    compressed.extend(data[offset + 8 : offset + 8 + size])
                offset += size + 12
            assert zlib.decompress(compressed)
            assert frame.stat().st_mode & 0o777 == 0o600
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
        stopped = invoke("stop", name)
        assert stopped.returncode == 0, stopped.stderr


@pytest.mark.asyncio
async def test_capture_creates_every_output_directory_privately(tmp_path):
    cdp = MagicMock()
    cdp.send = AsyncMock(return_value={})
    recorder = ScreencastRecorder()
    await recorder.start(cdp, {})
    recorder.on_frame({"data": base64.b64encode(b"frame").decode()})
    output = tmp_path / "private" / "capture"
    await recorder.stop(cdp, {"dir": str(output)})
    assert output.stat().st_mode & 0o777 == 0o700
    assert output.parent.stat().st_mode & 0o777 == 0o700
