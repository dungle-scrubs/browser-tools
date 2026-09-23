"""Capture contracts, including a real interaction and a retained multi-chunk heap."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from browser_tools import captures, cli, curated, step_list


def invoke(endpoint, capsys, *args):
    code = cli.main([*args, "--endpoint", endpoint])
    result = capsys.readouterr()
    return code, json.loads(result.out) if result.out else None, result.err


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--duration", "0"],
        ["--duration", "nan"],
        ["--duration", "1", "--categories", "v8", "--categories", "loading"],
    ],
)
def test_trace_invalid_window(tmp_path, capsys, args):
    path = tmp_path / "trace.json"
    code = cli.main(["trace", "--out", str(path), *args])
    assert code == 2
    assert not path.exists()
    assert not capsys.readouterr().out


@pytest.mark.parametrize(
    "step",
    [
        "trace --out x --duration 1",
        "snapshot --target 1",
        "snapshot --endpoint http://127.0.0.1:1",
        "snapshot --url test",
        "run steps",
    ],
)
def test_validation_precedes_start(tmp_path, capsys, monkeypatch, step):
    source = tmp_path / "steps"
    source.write_text("eval '1'\n" + step)

    def forbidden(*args, **kwargs):
        pytest.fail("invalid list opened a session")

    monkeypatch.setattr(curated, "run_session", forbidden)
    code = cli.main(["trace", "--out", str(tmp_path / "out"), "--steps", str(source)])
    assert code == 2
    assert not capsys.readouterr().out
    assert not (tmp_path / "out").exists()


def test_heap_is_a_step():
    assert step_list.validate("heap --out /tmp/heap.heapsnapshot")[0].args.command == "heap"


def test_real_interaction_and_heap(curated_browser, tmp_path, capsys):
    endpoint = curated_browser
    fixture = Path("docs/research/probes/154/interact.html").read_text()
    encoded = base64.b64encode(fixture.encode()).decode()
    source = tmp_path / "steps"
    source.write_text(
        f'Page.navigate \'{{"url":"data:text/html;base64,{encoded}"}}\'\nwait-text "Slow click"\n'
    )
    code, _, err = invoke(endpoint, capsys, "run", str(source))
    assert code == 0, err
    control = tmp_path / "control.json"
    code, plain, err = invoke(
        endpoint, capsys, "trace", "--out", str(control), "--duration", "0.25"
    )
    assert code == 0, err
    assert "run" not in plain
    assert plain["trace"]["endedBy"] == "duration"
    assert plain["trace"]["categories"] == list(captures.DEFAULT_CATEGORIES)
    source.write_text(
        'Input.dispatchMouseEvent \'{"type":"mousePressed","x":90,"y":365,"button":"left","clickCount":1}\'\n'
        'Input.dispatchMouseEvent \'{"type":"mouseReleased","x":90,"y":365,"button":"left","clickCount":1}\'\n'
        'wait-text "clicked:"\nwait-stable\n'
    )
    interaction = tmp_path / "interaction.json"
    code, driven, err = invoke(
        endpoint, capsys, "trace", "--out", str(interaction), "--steps", str(source)
    )
    assert code == 0, err
    assert driven["run"]["run"]["completed"] == 4
    assert driven["trace"]["endedBy"] == "steps"

    def interactions(path):
        return [
            event
            for event in json.loads(path.read_bytes())["traceEvents"]
            if event.get("name") == "EventTiming"
            and event.get("args", {}).get("data", {}).get("interactionId", 0)
        ]

    assert interactions(interaction)
    assert not interactions(control)
    for path, document in [(control, plain), (interaction, driven)]:
        assert document["trace"]["bytes"] == path.stat().st_size
        assert document["trace"]["events"] == len(json.loads(path.read_bytes())["traceEvents"])
        assert document["trace"]["dataLossOccurred"] is False

    code, override, err = invoke(
        endpoint,
        capsys,
        "trace",
        "--out",
        str(tmp_path / "custom.json"),
        "--duration",
        "0.05",
        "--categories",
        "blink.user_timing",
    )
    assert code == 0, err
    assert override["trace"]["categories"] == ["blink.user_timing"]

    heap_path = tmp_path / "large.heapsnapshot"
    source.write_text(
        "eval \"window.held = Array.from({length:100000}, (_,i) => ({index:i, text:'retained-'+i})); window.held.length\"\n"
        f"heap --out {heap_path}\n"
    )
    code, run, err = invoke(endpoint, capsys, "run", str(source))
    assert code == 0, err
    summary = run["steps"][1]["result"]
    snapshot = json.loads(heap_path.read_bytes())
    meta = snapshot["snapshot"]["meta"]
    assert summary["nodes"] == len(snapshot["nodes"]) // len(meta["node_fields"])
    assert summary["edges"] == len(snapshot["edges"]) // len(meta["edge_fields"])
    assert summary["nodes"] > 200000
    assert summary["bytes"] == heap_path.stat().st_size
    assert summary["chunks"] > 3
    code, standalone, err = invoke(
        endpoint, capsys, "heap", "--out", str(tmp_path / "standalone.heapsnapshot")
    )
    assert code == 0, err
    assert standalone["nodes"] > 200000
    print(json.dumps({"control": plain, "interaction": driven, "heap": summary}))


@pytest.mark.parametrize(
    ("options", "ended", "status"),
    [
        ([], "steps", "failed"),
        (["--timeout", "0.1"], "timeout", "timeout"),
        (["--duration", "0.1", "--timeout", "3"], "duration", "timeout"),
        (["--duration", "3", "--timeout", "0.1"], "timeout", "timeout"),
    ],
)
def test_failed_run_still_writes_trace(curated_browser, tmp_path, capsys, options, ended, status):
    source = tmp_path / "steps"
    source.write_text("wait-text absent --timeout-ms 400\neval 'window.mustNotRun = true'\n")
    path = tmp_path / "failed.json"
    code, document, err = invoke(
        curated_browser, capsys, "trace", "--steps", str(source), "--out", str(path), *options
    )
    assert code == 1, err
    assert document["trace"]["path"] == str(path)
    assert document["trace"]["endedBy"] == ended
    assert document["run"]["run"]["status"] == status
    assert len(document["run"]["steps"]) == 1
    assert json.loads(path.read_bytes())["traceEvents"]


@pytest.mark.asyncio
async def test_stream_bytes_loss_and_cleanup(tmp_path):
    payload = b'{"traceEvents":[{"name":"unicode-\xe2\x98\x83"}]}'

    class CDP:
        def __init__(self):
            self.callback = None
            self.closed = False
            self.reads = 0

        def on(self, event, callback, session):
            self.callback = callback

        def off(self, event, callback):
            assert callback is self.callback
            self.callback = None

        async def send(self, method, params=None, **kwargs):
            if method == "Tracing.end":
                self.callback({"stream": "42", "dataLossOccurred": True})
            if method == "IO.read":
                self.reads += 1
                return {
                    "data": base64.b64encode(payload).decode(),
                    "base64Encoded": True,
                    "eof": True,
                }
            if method == "IO.close":
                assert params["handle"] == "42"
                self.closed = True
            return {}

    cdp = CDP()
    capture = captures.TraceCapture(cdp, "session")
    await capture.start(list(captures.DEFAULT_CATEGORIES))
    path = tmp_path / "trace.json"
    completion = await capture.finish(path)
    assert completion["dataLossOccurred"] is True
    assert path.read_bytes() == payload
    assert cdp.closed and cdp.callback is None


@pytest.mark.parametrize("fail", [False, True])
def test_cli_preserves_run_outcome_and_loss(tmp_path, monkeypatch, capsys, fail):
    import asyncio
    import contextlib

    from doubles import HandlerSurface

    from browser_tools import step_run
    from browser_tools.lifecycle import LifecycleError

    calls = []
    payload = b'{"traceEvents":[{"name":"event"}]}'

    class CDP:
        def on(self, event, callback, session_id):
            self.callback = callback

        def off(self, event, callback):
            assert self.callback is callback
            self.callback = None

        async def send(self, method, params=None, **kwargs):
            calls.append(method)
            if method == "Tracing.start":
                assert params["traceConfig"]["includedCategories"] == ["blink.user_timing"]
                assert params["transferMode"] == "ReturnAsStream"
            if method == "Tracing.end":
                self.callback({"stream": "19", "dataLossOccurred": True})
            if method == "IO.read":
                return {"data": payload.decode(), "eof": True}
            return {}

    cdp = CDP()

    class CaptureHandler(HandlerSurface):
        session = (cdp, "session")

        def submit(self, coro, timeout=None):
            return asyncio.run(coro)

    handler = CaptureHandler()
    monkeypatch.setattr(curated, "run_session", lambda *a: contextlib.nullcontext(handler))

    def one_step(step, handler, registry_path):
        calls.append(f"step {step.number}")
        if fail:
            raise LifecycleError("deliberate failure")
        return {"answer": 1}

    monkeypatch.setattr(step_run, "_one_step", one_step)
    source = tmp_path / "steps"
    source.write_text("eval '1'\neval '2'\n")
    output = tmp_path / "trace.json"
    code = cli.main(
        ["trace", "--steps", str(source), "--out", str(output), "--categories", "blink.user_timing"]
    )
    document = json.loads(capsys.readouterr().out)
    assert code == (1 if fail else 0)
    assert document["run"]["run"]["status"] == ("failed" if fail else "ok")
    assert document["trace"]["path"] == str(output)
    assert document["trace"]["dataLossOccurred"] is True
    assert document["trace"]["events"] == 1
    assert output.read_bytes() == payload
    assert calls.index("Tracing.start") < calls.index("step 1") < calls.index("Tracing.end")
    assert ("step 2" in calls) is (not fail)
    assert calls[-1] == "IO.close"
    assert cdp.callback is None
    assert handler.deadline is None


@pytest.mark.asyncio
@pytest.mark.parametrize("keep", [frozenset(), frozenset({"HeapProfiler"})])
async def test_heap_collects_synchronous_chunks_and_restores_domain(tmp_path, keep):
    payload = '{"snapshot":{"node_count":2,"edge_count":1},"nodes":[],"edges":[]}'
    calls = []

    class CDP:
        def on(self, event, callback, session):
            self.callback = callback

        def off(self, event, callback):
            assert callback is self.callback
            self.callback = None

        async def send(self, method, params=None, **kwargs):
            calls.append(method)
            if method == "HeapProfiler.takeHeapSnapshot":
                for part in [payload[:7], payload[7:20], payload[20:]]:
                    self.callback({"chunk": part})
            return {}

    cdp = CDP()
    path = tmp_path / "snapshot"
    chunks = await captures._heap(cdp, "session", path, keep)
    assert chunks == 3
    assert path.read_text() == payload
    assert cdp.callback is None
    assert ("HeapProfiler.disable" in calls) is (not keep)
