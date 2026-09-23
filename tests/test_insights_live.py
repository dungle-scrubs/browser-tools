"""Opt-in acceptance: capture over HTTP with a real headless shell, never skip launch failure."""

from __future__ import annotations

import functools
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from browser_tools import cli, lifecycle

FIXTURES = Path(__file__).parent / "fixtures" / "insights"


@pytest.mark.skipif(
    os.environ.get("BT_INSIGHTS_LIVE") != "1",
    reason="set BT_INSIGHTS_LIVE=1 for real-browser capture acceptance",
)
def test_capture_navigation_and_interaction(tmp_path, capsys):
    assert os.environ.get(lifecycle.CHROME_BINARY_ENV_VAR), "A headless shell is required"
    registry = str(tmp_path / "registry.json")
    # A launch failure is a failure, not a skipped or successful acceptance run.
    instance = lifecycle.launch(headless=True, registry_path=registry, browser_args=["about:blank"])
    try:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(SimpleHTTPRequestHandler, directory=str(FIXTURES))
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"http://127.0.0.1:{instance.port}"
            url = f"http://127.0.0.1:{server.server_port}/fixture.html"
            for driven, state in [(False, "pass"), (True, "fail")]:
                steps = tmp_path / "steps.txt"
                text = f"Page.navigate '{json.dumps({'url': url})}'\nwait-text 'block the main thread'\nwait-stable\n"
                if driven:
                    # The fixture's button is below the heading at x=32,y~112.
                    for kind in ["mousePressed", "mouseReleased"]:
                        params = {
                            "type": kind,
                            "x": 100,
                            "y": 130,
                            "button": "left",
                            "clickCount": 1,
                        }
                        text += f"Input.dispatchMouseEvent '{json.dumps(params)}'\n"
                    text += "wait-text 'blocked 280ms'\nwait-stable\n"
                steps.write_text(text)
                trace = tmp_path / f"{state}.json"
                assert (
                    cli.main(
                        [
                            "trace",
                            "--steps",
                            str(steps),
                            "--out",
                            str(trace),
                            "--endpoint",
                            endpoint,
                        ]
                    )
                    == 0
                )
                captured = capsys.readouterr()
                assert json.loads(captured.out)["trace"]["dataLossOccurred"] is False
                assert cli.main(["insights", "--trace", str(trace)]) == 0
                result = capsys.readouterr()
                models = {
                    i["key"]: i
                    for n in json.loads(result.out)["navigations"]
                    for i in n["insights"]
                }
                assert len(models) == 19
                assert models["INPBreakdown"]["state"] == state
                if driven:
                    assert "Processing duration: 280" in models["INPBreakdown"]["detail"]
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
    finally:
        lifecycle.stop(instance=instance.name, registry_path=registry)
