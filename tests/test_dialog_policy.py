"""Dialog policies through real CLI invocations against an isolated Chrome."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from urllib.parse import quote

import pytest

from browser_tools import cli, step_list
from browser_tools.dialog_policy import DialogPolicy


def invoke(endpoint: str, *args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "browser_tools.cli", *args, "--endpoint", endpoint],
        capture_output=True, text=True, timeout=6,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    return json.loads(completed.stdout)


def test_alert_click_returns_and_page_remains_readable(curated_browser: str) -> None:
    html = '<button onclick="alert(\'hello\');document.title=\'answered\'">Alert</button>'
    invoke(curated_browser, "Page.navigate", json.dumps({"url": "data:text/html," + quote(html)}))
    tree = invoke(curated_browser, "snapshot")["snapshot"]
    uid = re.search(r'uid=([^\s\]]+).*button.*Alert', tree)
    assert uid, tree
    result = invoke(curated_browser, "click", "--uid", uid[1])
    assert result["dialogs"] == [
        {"type": "alert", "message": "hello", "answer": "dismiss", "result": None}
    ]
    assert invoke(curated_browser, "eval", "document.title")["value"] == "answered"


@pytest.mark.parametrize("policy,expression,kind,expected", [
    ("accept", "confirm('continue?')", "confirm", True),
    ("dismiss", "confirm('continue?')", "confirm", False),
    ("accept:a:b", "prompt('continue?', 'default')", "prompt", "a:b"),
    ("accept", "prompt('continue?', 'default')", "prompt", "default"),
    ("accept:", "prompt('continue?', 'default')", "prompt", ""),
    ("dismiss", "prompt('continue?', 'default')", "prompt", None),
])
def test_page_receives_answer(curated_browser, policy, expression, kind, expected):
    result = invoke(curated_browser, "eval", expression, "--dialog", policy)
    assert result["value"] == expected
    assert result["dialogs"] == [{
        "type": kind, "message": "continue?", "answer": policy.split(":")[0],
        "result": expected,
    }]


def test_run_records_every_dialog_in_order(curated_browser, tmp_path):
    source = tmp_path / "dialogs.steps"
    source.write_text("eval \"alert('first');confirm('second')\"\neval \"prompt('third')\"\n")
    result = invoke(curated_browser, "run", str(source), "--dialog", "accept:yes")
    assert result["run"]["status"] == "ok"
    assert result["dialogs"] == [
        {"type": "alert", "message": "first", "answer": "accept", "result": None},
        {"type": "confirm", "message": "second", "answer": "accept", "result": True},
        {"type": "prompt", "message": "third", "answer": "accept", "result": "yes"},
    ]
    assert all("dialogs" not in step["result"] for step in result["steps"])


def test_plain_verb_and_run_omit_dialogs(curated_browser, tmp_path):
    assert "dialogs" not in invoke(curated_browser, "eval", "42")
    source = tmp_path / "plain.steps"
    source.write_text("eval '42'\n")
    result = invoke(curated_browser, "run", str(source))
    assert "dialogs" not in result
    assert "dialogs" not in result["steps"][0]["result"]


@pytest.mark.parametrize("step", [
    "eval '42' --dialog accept", "click --uid x --dialog=dismiss",
    "Runtime.evaluate '{}' --dialog accept", "eval '42' --dial accept",
])
def test_step_cannot_override_policy(step):
    with pytest.raises(step_list.StepListError):
        step_list.validate(step)


@pytest.mark.parametrize("argv", [
    ["click", "--uid", "x"], ["eval", "42"], ["press", "Enter"],
    ["type", "text"], ["hover", "--uid", "x"], ["wait-text", "text"],
    ["navigate", "about:blank"], ["run", "steps.txt"],
])
def test_carriers_have_active_default_and_reject_invalid_policy(argv):
    args = cli.build_parser().parse_args(argv)
    cli.check_preconditions(args)
    assert args.dialog == "dismiss"
    args = cli.build_parser().parse_args([*argv, "--dialog", "dismiss:text"])
    with pytest.raises(cli.UsageError, match="--dialog"):
        cli.check_preconditions(args)


class DialogCDP:
    """Protocol peer: a send reply can arrive only after the event callback returns."""

    def __init__(self):
        self.listeners = {}
        self.calls = []

    def on(self, name, callback):
        self.listeners[name] = callback

    def off(self, name, callback):
        assert self.listeners.pop(name) == callback

    async def send(self, method, params):
        self.calls.append((method, params))
        return {}

    def open(self, kind, message, default=""):
        self.listeners["Page.javascriptDialogOpening"]({
            "type": kind, "message": message, "defaultPrompt": default,
        })


@pytest.mark.asyncio
@pytest.mark.parametrize("value,kind,expected,params", [
    ("dismiss", "alert", None, {"accept": False}),
    ("accept", "alert", None, {"accept": True}),
    ("dismiss", "confirm", False, {"accept": False}),
    ("accept", "confirm", True, {"accept": True}),
    ("dismiss", "prompt", None, {"accept": False}),
    ("accept:a:b", "prompt", "a:b", {"accept": True, "promptText": "a:b"}),
    ("accept:", "prompt", "", {"accept": True, "promptText": ""}),
    ("accept", "prompt", "default", {"accept": True, "promptText": "default"}),
    ("dismiss", "beforeunload", False, {"accept": False}),
])
async def test_protocol_answers_and_records(value, kind, expected, params):
    cdp = DialogCDP()
    policy = DialogPolicy(value)
    policy.start(cdp)
    cdp.open(kind, "question", "default")
    assert await policy.document() == {"dialogs": [{
        "type": kind, "message": "question", "answer": value.split(":")[0],
        "result": expected,
    }]}
    assert cdp.calls == [("Page.handleJavaScriptDialog", params)]
    await policy.close()
    assert cdp.listeners == {}


@pytest.mark.asyncio
async def test_no_dialog_sends_nothing_and_has_no_key():
    cdp = DialogCDP()
    policy = DialogPolicy("dismiss")
    policy.start(cdp)
    assert await policy.document() == {}
    assert cdp.calls == []
    await policy.close()


@pytest.mark.asyncio
async def test_answers_more_dialogs_while_draining_in_arrival_order():
    cdp = DialogCDP()
    policy = DialogPolicy("accept")
    original = cdp.send

    async def send(method, params):
        await original(method, params)
        if len(cdp.calls) < 25:
            cdp.open("confirm", str(len(cdp.calls)))
        return {}

    cdp.send = send
    policy.start(cdp)
    cdp.open("confirm", "0")
    result = await policy.document()
    assert [r["message"] for r in result["dialogs"]] == [str(i) for i in range(25)]
    assert all(r["answer"] == "accept" and r["result"] is True for r in result["dialogs"])
    assert len(cdp.calls) == 25
    await policy.close()
