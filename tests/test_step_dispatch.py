"""One dispatch decides what a step does, whether or not it is in a run.

`cli.step_envelope` returns the JSON document a step-capable verb produces.
`_run` prints what it returns; a Step Run will collect it into the Run
Document instead. That is what makes a step's `result` byte-identical to what
the same step prints alone, which is the composability RFC-03 specifies.

A Step Run with its own dispatch table would be a second place to add a verb
and a second place to get it wrong. RFC-03 rules that out for the parser and
for the preconditions, and the reason is the same here.
"""

from __future__ import annotations

import argparse

import pytest
from doubles import HandlerSurface

from browser_tools import cli, step_list


class FakeHandler(HandlerSurface):
    """Records that the session reached the verb."""

    def __init__(self):
        self.stopped = False
        self.seen: list[str] = []
        self.session = ("CLIENT", "SESSION-1")
        self.available = True
        self.connect_error = None

    def require_session(self):
        from browser_tools.lifecycle import LifecycleError

        if self.session is None or not self.available:
            raise LifecycleError(self.connect_error or "the CDP session is not connected")
        return self.session

    def stop(self):
        self.stopped = True

    def call_native(self, name, arguments):
        from browser_tools.mcp_response import make_text

        self.seen.append(name)
        if name in {"eval", "press", "hover", "type", "wait-text", "network-get"}:
            return make_text('{"unchanged":"result"}')
        return make_text("[uid=AB-1] RootWebArea")

    def call_tool(self, name, arguments):
        from browser_tools.mcp_response import make_text

        self.seen.append(name)
        return make_text("ok")

    def submit(self, coro, timeout=None):
        self.seen.append(getattr(coro, "__qualname__", "coroutine"))
        coro.close()
        return []


def _args(command: str, **fields) -> argparse.Namespace:
    base = {
        "command": command,
        "instance": None,
        "target": None,
        "url": None,
        "endpoint": None,
    }
    base.update(fields)
    return argparse.Namespace(**base)


class TestTheDispatchCoversTheStepSurface:
    """The guard. A verb that is a step must have somewhere to go."""

    def test_every_step_verb_is_dispatched(self, monkeypatch):
        """Not a single one may fall through to the refusal at the end."""
        monkeypatch.setattr(
            "browser_tools.lifecycle.registry_path_from_env", lambda: None
        )
        unreachable = []
        for verb in sorted(step_list.STEP_VERBS):
            try:
                cli.step_envelope(_args(verb), None, handler=FakeHandler())
            except cli.PassthroughUsageError as exc:
                if "cannot be a step" in str(exc):
                    unreachable.append(verb)
            except Exception:
                # Any other failure means the dispatch found the verb and the
                # verb itself objected, which is what this test wants.
                pass
        assert not unreachable, (
            f"{unreachable} is on the step surface with no branch in "
            "step_envelope. A Step Run would refuse a step validation accepted."
        )

    def test_a_verb_that_is_not_a_step_is_refused(self):
        with pytest.raises(cli.PassthroughUsageError, match="cannot be a step"):
            cli.step_envelope(_args("launch"), None)

    def test_the_refusal_names_the_verb(self):
        with pytest.raises(cli.PassthroughUsageError, match="attach"):
            cli.step_envelope(_args("attach"), None)


class TestTheSessionReachesTheVerb:
    """A step runs on the run's session, not one of its own."""

    def test_a_curated_verb_uses_the_supplied_handler(self):
        handler = FakeHandler()
        result = cli.step_envelope(_args("snapshot"), None, handler=handler)
        assert handler.seen == ["take_snapshot"]
        assert "RootWebArea" in result["snapshot"]

    @pytest.mark.parametrize("phrase", [
        "eval 1", "press Enter", "hover --uid AB-1", "type hello",
        "wait-text ready", "network-get --request-id 1.2",
    ])
    def test_six_verbs_borrow_the_session_and_keep_the_result(self, phrase, monkeypatch):
        import shlex

        handler = FakeHandler()
        args = cli.build_parser().parse_args(shlex.split(phrase))
        cli.check_preconditions(args)
        def refuse(*args, **kwargs):
            raise AssertionError("a step tried to open a second session")
        monkeypatch.setattr("browser_tools.curated._resolve_port", refuse)
        result = cli.step_envelope(args, None, handler=handler)
        assert result == {"unchanged": "result"}
        assert handler.seen == [args.command]
        assert not handler.stopped

    def test_a_frames_sub_action_uses_it_too(self):
        handler = FakeHandler()
        cli.step_envelope(
            _args("frames", frames_action="list"), None, handler=handler
        )
        assert handler.seen == ["list_frames"]

    def test_a_list_verb_submits_to_the_handlers_loop(self):
        handler = FakeHandler()
        cli.step_envelope(
            _args("console-list", duration=0.1), None, handler=handler
        )
        assert handler.seen and "collect_on_session" in handler.seen[0]

    def test_no_verb_closes_the_session(self):
        handler = FakeHandler()
        cli.step_envelope(_args("snapshot"), None, handler=handler)
        assert handler.stopped is False


class TestWithoutAHandlerNothingChanges:
    """The bare path still opens its own session, as it always did."""

    def test_a_curated_verb_opens_one(self, monkeypatch):
        opened = []
        monkeypatch.setattr(
            "browser_tools.curated.snapshot",
            lambda **kw: opened.append(kw.get("handler")) or {"snapshot": "x"},
        )
        cli.step_envelope(_args("snapshot"), None)
        assert opened == [None], "a bare invocation was handed a session"


class TestAStepWillNotSendOnAMissingSession:
    """`session` is None before the connection, and outlives a disconnect.

    Unpacking it blind turns both cases into a `TypeError` about `NoneType`
    at the point of use, which tells a caller nothing about the browser.
    """

    @pytest.mark.parametrize("verb", ["screenshot", "console-list", "wait"])
    def test_a_disconnected_handler_is_refused_with_a_reason(self, verb):
        from browser_tools.lifecycle import LifecycleError

        handler = FakeHandler()
        handler.available = False
        handler.connect_error = "Cannot reach browser on port 9222"

        with pytest.raises(LifecycleError, match="Cannot reach browser"):
            cli.step_envelope(
                _args(verb, duration=0.1, path=None, event="X.y", match=None, timeout=1),
                None,
                handler=handler,
            )

    def test_a_handler_with_no_session_is_refused(self):
        from browser_tools.lifecycle import LifecycleError

        handler = FakeHandler()
        handler.session = None

        with pytest.raises(LifecycleError):
            cli.step_envelope(_args("screenshot", path=None), None, handler=handler)


@pytest.mark.asyncio
@pytest.mark.parametrize(('name','arguments'), [
    ('eval', {'source':'document.title', 'await_promise':False}),
    ('press', {'key':'Enter', 'modifiers':None}),
    ('type', {'text':'hello', 'source':'text'}),
    ('wait-text', {'substring':'ready','timeout_ms':5000}),
    ('network-get', {'request_id':'1.2'}),
    ('hover', {'uid':'placeholder'}),
])
@pytest.mark.parametrize('frame_session', [None, 'CHILD-SESSION'])
async def test_six_verbs_follow_selected_frame(name, arguments, monkeypatch, frame_session):
    from unittest.mock import AsyncMock, Mock

    from browser_tools import curated_runtime
    from browser_tools.cdp_handler import CDPHandler
    from browser_tools.frame_manager import FrameManager
    from browser_tools.native_snapshot import doc_token

    handler = CDPHandler(None)
    frames = FrameManager()
    frames.update_from_frame_tree({'frame': {'id':'TOP', 'url':'https://top', 'loaderId':'top'},
        'childFrames':[{'frame':{'id':'CHILD','url':'https://child','loaderId':'child'}}]})
    frames._frames['CHILD'].frame_session_id = frame_session
    frames.handle_execution_context_created({'context': {'id':7, 'auxData':{'frameId':'CHILD','isDefault':True}}}, frame_session)
    handler._rt._frame_manager = frames
    page_cdp = Mock(send=AsyncMock())
    selected_cdp = Mock(send=AsyncMock(return_value={'result':{'objectId':'focused'}}))
    chosen_sessions = []
    def selected_client(session):
        chosen_sessions.append(session)
        return selected_cdp
    monkeypatch.setattr(handler, 'client_for_session', selected_client)
    await handler._handle_select_frame({'url_pattern':'child'})
    eval_action = AsyncMock(return_value={})
    wait_action = AsyncMock(return_value={})
    network_action = AsyncMock(return_value={})
    hover_action = AsyncMock(return_value=(1,2))
    monkeypatch.setattr(curated_runtime, 'evaluate', eval_action)
    monkeypatch.setattr(curated_runtime, 'wait_text', wait_action)
    monkeypatch.setattr(curated_runtime, 'network_get', network_action)
    monkeypatch.setattr(handler._native_interactor, 'hover_async', hover_action)
    if name == 'hover':
        arguments = {'uid':f'{doc_token("CHILD","child")}-3'}
    await handler._dispatch_curated_action(page_cdp, name, arguments)
    assert chosen_sessions == [frame_session]
    if name == 'eval':
        assert eval_action.call_args.args[:4] == (selected_cdp, 'document.title', False, 7)
    elif name == 'wait-text':
        assert wait_action.call_args.args == (selected_cdp, 'ready', 5000, 7)
    elif name == 'network-get':
        assert network_action.call_args.args[4:6] == ('CHILD','https://child')
    elif name == 'hover':
        assert hover_action.call_args.args == (selected_cdp.send, arguments['uid'])
    else:
        first = selected_cdp.send.call_args_list[0]
        assert first.args == ('Runtime.evaluate', {'expression':'document.activeElement','contextId':7})
        assert selected_cdp.send.call_args_list[1].args == ('DOM.focus', {'objectId':'focused'})
    page_cdp.send.assert_not_called()


@pytest.mark.parametrize('phrase', [
    'eval 1', 'press Enter', 'hover --uid AB-1', 'type hello',
    'wait-text ready --timeout-ms 5000', 'network-get --request-id 1.2',
])
def test_six_verbs_are_interrupted_by_the_run_deadline(phrase, monkeypatch):
    import asyncio
    import shlex
    import threading
    import time

    from browser_tools import step_run
    from browser_tools.cdp_handler import CDPHandler

    handler = CDPHandler(None)
    loop = asyncio.new_event_loop()
    handler._rt._loop = loop
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    cancelled = threading.Event()
    async def blocked(*args):
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.set()
    monkeypatch.setattr(handler, '_dispatch_native', blocked)
    monkeypatch.setattr(handler, 'page_visibility_state', lambda: 'visible')
    args = cli.build_parser().parse_args(shlex.split(phrase))
    cli.check_preconditions(args)
    step = step_list.Step(1,1,phrase,shlex.split(phrase),args)
    try:
        start = time.monotonic()
        result, ok = step_run.execute([step], handler, timeout=.03)
        assert not ok and result['run']['status'] == 'timeout'
        assert time.monotonic() - start < 1
        assert cancelled.wait(1), 'the timed-out native operation was left running'
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
