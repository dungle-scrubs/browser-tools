"""Opt-in acceptance: capture over HTTP with a real headless shell, never skip launch failure."""

from __future__ import annotations

import functools
import json
import os
import re
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from browser_tools import cli, lifecycle

FIXTURES = Path(__file__).parent / "fixtures" / "insights"

#: BT_INSIGHTS_LIVE_UNVALIDATED=1 runs every assertion with
#: --ignore-engine-mismatch. A pass on a build absent from the adapter's
#: validated list is the evidence for adding it; without the variable, an
#: unlisted build fails here, which is how a new Chrome stable is noticed.
ENGINE_FLAGS = (
    ["--ignore-engine-mismatch"] if os.environ.get("BT_INSIGHTS_LIVE_UNVALIDATED") == "1" else []
)


def insights(trace: Path, *extra: str) -> list[str]:
    return ["insights", "--trace", str(trace), *ENGINE_FLAGS, *extra]


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
                text = (
                    f"Page.navigate '{json.dumps({'url': url})}'\n"
                    "wait-text 'block the main thread'\nwait-stable\n"
                )
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
                assert cli.main(insights(trace)) == 0
                result = capsys.readouterr()
                models = {
                    i["key"]: i
                    for n in json.loads(result.out)["navigations"]
                    for i in n["insights"]
                }
                assert len(models) == 19
                assert models["INPBreakdown"]["state"] == state
                if driven:
                    # The click handler busy-waits until 280ms have passed, so
                    # the engine reports at least 280. The ceiling only catches
                    # a different handler; a loaded CI runner adds dispatch
                    # delay on top of the 280.
                    detail = models["INPBreakdown"]["detail"]
                    processing = re.search(r"Processing duration: (\d+)", detail)
                    assert processing, detail
                    assert 280 <= int(processing[1]) < 400, detail
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
    finally:
        lifecycle.stop(instance=instance.name, registry_path=registry)


@pytest.mark.skipif(
    os.environ.get("BT_INSIGHTS_LIVE") != "1",
    reason="set BT_INSIGHTS_LIVE=1 for real-browser capture acceptance",
)
def test_zero_set_and_failure_causes_reproduce_live(tmp_path, capsys):
    """Re-capture every committed zero-set and failure fixture with a real shell.

    The committed traces are evidence of one run. This proves the same capture
    still produces the same cause today, on this machine's headless shell.
    """
    assert os.environ.get(lifecycle.CHROME_BINARY_ENV_VAR), "A headless shell is required"
    registry = str(tmp_path / "registry.json")
    instance = lifecycle.launch(headless=True, registry_path=registry, browser_args=["about:blank"])
    try:


        class Handler(SimpleHTTPRequestHandler):
            """The fixture directory, with one path that answers too late."""

            def do_GET(self):
                if self.path.startswith("/slow.html"):
                    time.sleep(10)
                super().do_GET()

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(Handler, directory=str(FIXTURES))
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"http://127.0.0.1:{instance.port}"
            base = f"http://127.0.0.1:{server.server_port}"
            load = "wait-text 'block the main thread'\nwait-stable\n"
            cases = {
                # A navigation the trace records only as a document request,
                # because the category list drops blink.user_timing.
                "narrow-categories": (
                    f"Page.navigate '{json.dumps({'url': f'{base}/fixture.html'})}'\n{load}",
                    ["--categories=devtools.timeline,v8.execute"],
                    1,
                    "blink.user_timing",
                ),
                # A navigation Chrome answers with its own error document.
                "error-page": (
                    "Page.navigate '{\"url\": \"http://127.0.0.1:1/never.html\"}'\nwait-stable\n",
                    [],
                    1,
                    "chrome-error://chromewebdata/",
                ),
                # Two navigations in one capture: two Insight Sets.
                "two-navigations": (
                    f"Page.navigate '{json.dumps({'url': f'{base}/fixture.html'})}'\n{load}"
                    f"Page.navigate '{json.dumps({'url': f'{base}/fixture.html?second'})}'\n{load}",
                    [],
                    0,
                    None,
                ),
                # A navigation the trace window closes on before the document
                # commits. The server holds the response past --duration, so
                # the only navigationStart has an empty documentLoaderURL.
                # This one runs last: it leaves a navigation in flight.
                "uncommitted-navigation": (
                    f"Page.navigate '{json.dumps({'url': f'{base}/slow.html'})}'\n",
                    ["--duration", "1"],
                    1,
                    "documentLoaderURL is empty",
                ),
            }
            for name, (text, extra, expected, phrase) in cases.items():
                steps = tmp_path / f"{name}.steps"
                steps.write_text(text)
                trace = tmp_path / f"{name}.json"
                cli.main(
                    [
                        "trace", "--steps", str(steps), "--out", str(trace),
                        "--endpoint", endpoint, *extra,
                    ]
                )
                assert json.loads(capsys.readouterr().out)["trace"]["dataLossOccurred"] is False
                assert cli.main(insights(trace)) == expected, name
                result = capsys.readouterr()
                if phrase is not None:
                    assert phrase in result.err, (name, result.err)
                else:
                    document = json.loads(result.out)
                    assert len(document["navigations"]) == 2
                    text = insights(trace, "--format", "text")
                    assert cli.main(text) == 0
                    assert "# Navigation 2 of 2:" in capsys.readouterr().out
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
    finally:
        lifecycle.stop(instance=instance.name, registry_path=registry)
