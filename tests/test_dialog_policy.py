"""Dialog policies through real CLI invocations against an isolated Chrome."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from urllib.parse import quote

import pytest

from browser_tools import cli, dialog_policy, step_list
from browser_tools.dialog_policy import DialogPolicy
from browser_tools.usage import UsageError


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


@pytest.mark.parametrize("verb", ["press", "type", "hover", "wait-text"])
def test_other_carriers_answer_page_handlers(curated_browser, verb):
    html = '<input aria-label="Field"><button>Hover</button><p>Ready</p>'
    invoke(curated_browser, "navigate", "data:text/html," + quote(html))
    if verb in {"press", "type"}:
        event = "keydown" if verb == "press" else "input"
        invoke(curated_browser, "eval", f"""document.querySelector('input').focus();
          document.querySelector('input').addEventListener('{event}', () => {{
            window.answer = confirm('{verb}');
          }}, {{once:true}})""")
        args = [verb, "x"]
    elif verb == "hover":
        invoke(curated_browser, "eval", """document.querySelector('button').onmouseenter =
          () => { window.answer = confirm('hover'); }""")
        tree = invoke(curated_browser, "snapshot")["snapshot"]
        uid = re.search(r'uid=([^\s\]]+).*button.*Hover', tree)
        assert uid, tree
        args = [verb, "--uid", uid[1]]
    else:
        # Trigger during this invocation's first text read, without a timer race.
        invoke(curated_browser, "eval", """Object.defineProperty(document.body, 'innerText', {
          get() { delete this.innerText; window.answer = confirm('wait-text');
            return 'Ready'; }, configurable: true
        })""")
        args = [verb, "Ready"]
    result = invoke(curated_browser, *args)
    assert result["dialogs"] == [
        {"type": "confirm", "message": verb, "answer": "dismiss", "result": False}
    ]
    assert invoke(curated_browser, "eval", "window.answer")["value"] is False


def test_navigate_accepts_beforeunload(curated_browser):
    invoke(curated_browser, "navigate", "data:text/html," + quote('<button>Activate</button>'))
    tree = invoke(curated_browser, "snapshot")["snapshot"]
    uid = re.search(r'uid=([^\s\]]+).*button.*Activate', tree)
    assert uid, tree
    invoke(curated_browser, "click", "--uid", uid[1])
    invoke(curated_browser, "eval", "window.onbeforeunload = () => 'Leave?'")
    result = invoke(curated_browser, "navigate", "about:blank", "--dialog", "accept")
    assert len(result["dialogs"]) == 1
    record = result["dialogs"][0]
    assert record["type"] == "beforeunload"
    assert isinstance(record["message"], str)
    assert record["answer"] == "accept" and record["result"] is True
    assert invoke(curated_browser, "eval", "location.href")["value"] == "about:blank"


def test_snapshot_does_not_subscribe_to_dialogs(curated_browser, monkeypatch, capsys):
    from browser_tools.attached_session import AttachedSessionClient

    subscribed = []
    original = AttachedSessionClient.on

    def observe(self, event, callback):
        subscribed.append(event)
        original(self, event, callback)

    monkeypatch.setattr(AttachedSessionClient, "on", observe)
    assert cli.main(["snapshot", "--endpoint", curated_browser]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "snapshot" in json.loads(captured.out)
    assert "Page.javascriptDialogOpening" not in subscribed
    subscribed.clear()
    assert cli.main(["eval", "42", "--endpoint", curated_browser]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["value"] == 42
    assert subscribed.count("Page.javascriptDialogOpening") == 1


def test_plain_click_has_no_dialog_output_or_fixed_delay(curated_browser):
    import statistics
    import time

    from browser_tools import curated

    invoke(curated_browser, "navigate", "data:text/html," + quote(
        '<button onclick="window.clicked=(window.clicked || 0)+1">Plain</button>'
    ))
    tree = invoke(curated_browser, "snapshot")["snapshot"]
    uid = re.search(r'uid=([^\s\]]+).*button.*Plain', tree)
    assert uid, tree
    timings = {"baseline": [], "dismiss": []}
    for _ in range(10):
        for name in timings:
            # Disable only the policy for the control; use the same awaited click.
            with curated._cdp_handler_session(
                int(curated_browser.rsplit(":", 1)[1]),
                dialog="dismiss" if name == "dismiss" else None,
            ) as handler:
                started = time.monotonic()
                result = curated.click(instance=None, uid=uid[1], handler=handler)
                records = handler.dialog_document()
                timings[name].append(time.monotonic() - started)
                assert records == {} and "dialogs" not in result
    baseline = statistics.median(timings["baseline"])
    active = statistics.median(timings["dismiss"])
    print(json.dumps({"plainClickMedianSeconds": {"baseline": baseline, "dismiss": active}}))
    assert active < baseline + 0.05
    assert invoke(curated_browser, "eval", "window.clicked")["value"] == 20


@pytest.mark.parametrize("step", [
    "eval '42' --dialog accept", "click --uid x --dialog=dismiss",
    "Runtime.evaluate '{}' --dialog accept", "eval '42' --dial accept",
])
def test_step_cannot_override_policy(step):
    with pytest.raises(step_list.StepListError):
        step_list.validate(step)


@pytest.mark.parametrize("argv", [
    ["click", "--uid", "x"], ["fill", "--uid", "x", "--text", "t"],
    ["eval", "42"], ["press", "Enter"],
    ["type", "text"], ["hover", "--uid", "x"], ["wait-text", "text"],
    ["navigate", "about:blank"], ["network-get", "--url", "x"],
    ["run", "steps.txt"],
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


def test_fill_answers_a_dialog_its_own_input_handler_raises(curated_browser: str) -> None:
    """``fill`` carries the policy because setting a value fires ``input``.

    RFC-05 section 4 listed eight carriers and left ``fill`` out, while stating
    the rule as every verb that drives the page. Measured against a real
    browser before ``fill`` carried the policy: this call never returned, and
    the six-second bound in :func:`invoke` expired.
    """
    html = '<input oninput="alert(\'from-fill\');document.title=\'fired\'">'
    invoke(curated_browser, "Page.navigate", json.dumps({"url": "data:text/html," + quote(html)}))
    tree = invoke(curated_browser, "snapshot")["snapshot"]
    uid = re.search(r"\[uid=([^\s\]]+)\] textbox", tree)
    assert uid, tree

    result = invoke(curated_browser, "fill", "--uid", uid[1], "--text", "hello")

    assert result["dialogs"] == [
        {"type": "alert", "message": "from-fill", "answer": "dismiss", "result": None}
    ]
    assert invoke(curated_browser, "eval", "document.title")["value"] == "fired"


# An absolute bound, deliberately not derived from DRAIN_BUDGET_SECONDS: a
# regression that removes the budget must fail this test rather than inherit
# its own broken value and hang the suite.
FLOOD_BOUND_SECONDS = 15.0


@pytest.mark.asyncio
async def test_a_dialog_flood_ends_the_drain_instead_of_running_forever():
    """A page that opens dialogs faster than they are answered must not hang.

    The drain's exit condition is "everything seen has been collected", and a
    page calling alert() from a zero-delay interval makes that permanently
    false. Measured before the budget existed: 191,145 dialogs answered in
    three seconds with the drain still running, and nothing above it was
    bounded either, so `bt eval` hung with no timeout at all.
    """
    cdp = DialogCDP()
    policy = DialogPolicy("dismiss")
    policy.start(cdp)
    original = cdp.send

    async def flood(method, params):
        # Every answer releases script that opens the next dialog.
        cdp.open("alert", "again")
        return await original(method, params)

    cdp.send = flood
    cdp.open("alert", "first")

    started = time.monotonic()
    # wait_for, not a bare await: without the budget this drain never returns,
    # and a test that hangs stops the whole suite instead of failing it.
    document = await asyncio.wait_for(policy.document(), timeout=FLOOD_BOUND_SECONDS)
    elapsed = time.monotonic() - started

    assert elapsed < dialog_policy.DRAIN_BUDGET_SECONDS + 2.0, elapsed
    assert document["dialogsTruncated"] is True
    assert document["dialogs"], "the answers it did collect are still reported"
    await policy.close()


@pytest.mark.asyncio
async def test_a_failed_answer_keeps_every_other_record():
    """One dialog that vanished must not discard the rest, nor escape.

    asyncio.gather without return_exceptions used to propagate the CDPError
    from a "No dialog is showing" reply. cli.main catches LifecycleError and
    UsageError, and that is neither, so the invocation died with a traceback
    and printed nothing -- losing a whole Run Document to one late dialog.
    """
    cdp = DialogCDP()
    policy = DialogPolicy("dismiss")
    policy.start(cdp)
    calls = {"n": 0}
    original = cdp.send

    async def flaky(method, params):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("No dialog is showing")
        return await original(method, params)

    cdp.send = flaky
    cdp.open("alert", "vanished")
    cdp.open("alert", "answered")

    document = await policy.document()

    assert len(document["dialogs"]) == 2
    assert "error" in document["dialogs"][0]
    assert "No dialog is showing" in document["dialogs"][0]["error"]
    assert "error" not in document["dialogs"][1]
    await policy.close()


def test_a_non_string_policy_is_a_usage_error_not_an_attribute_error():
    """step_run.run(dialog=...) is a public signature that can pass None."""
    with pytest.raises(UsageError):
        dialog_policy.validate_policy(None)


def _raw(endpoint: str, *args: str, timeout: float = 25) -> subprocess.CompletedProcess:
    """invoke() for the failure cases: it asserts exit 0 and empty stderr."""
    return subprocess.run(
        [sys.executable, "-m", "browser_tools.cli", *args, "--endpoint", endpoint],
        capture_output=True, text=True, timeout=timeout,
    )


def _arm_beforeunload(endpoint: str) -> None:
    """A beforeunload prompt only fires after the page has user activation."""
    html = "<button>Go</button>"
    invoke(endpoint, "Page.navigate", json.dumps({"url": "data:text/html," + quote(html)}))
    tree = invoke(endpoint, "snapshot")["snapshot"]
    uid = re.search(r"\[uid=([^\s\]]+)\] button", tree)
    assert uid, tree
    invoke(endpoint, "click", "--uid", uid[1])
    invoke(endpoint, "eval", "window.onbeforeunload=()=>'x'")


def test_a_cancelled_navigation_says_a_prompt_was_declined(curated_browser: str) -> None:
    """The default declines beforeunload, which cancels the navigation.

    That is the headline case of the verb, so the caller must be told why.
    Before this, it reported a bare ``net::ERR_ABORTED`` with no ``dialogs``
    key, plus an internal ``call_native(navigate) error:`` line on stderr from
    a logger warning the project installs no handler for.
    """
    _arm_beforeunload(curated_browser)

    done = _raw(curated_browser, "navigate", "about:blank")

    assert done.returncode == 1
    assert json.loads(done.stdout)["dialogs"] == [
        {"type": "beforeunload", "message": "", "answer": "dismiss", "result": False}
    ]
    assert "--dialog accept" in done.stderr
    assert "call_native(" not in done.stderr


def test_accepting_beforeunload_lets_the_navigation_through(curated_browser: str) -> None:
    _arm_beforeunload(curated_browser)

    result = invoke(curated_browser, "navigate", "about:blank", "--dialog", "accept")

    assert result["dialogs"] == [
        {"type": "beforeunload", "message": "", "answer": "accept", "result": True}
    ]


def test_network_get_reload_answers_beforeunload_instead_of_hanging(curated_browser: str) -> None:
    """``--reload`` sends Page.reload, which runs beforeunload.

    The same omission that left ``fill`` able to hang. Without the policy this
    call never returns and the instance is wedged.
    """
    _arm_beforeunload(curated_browser)

    done = _raw(curated_browser, "network-get", "--url", "data:", "--reload", timeout=25)

    assert done.returncode in (0, 1), done.stderr
    assert "did not answer in time" not in done.stderr
