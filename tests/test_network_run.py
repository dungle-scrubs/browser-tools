"""Run-owned responses, tested through the real run engine and CDP transport seam."""

from __future__ import annotations

import contextlib
import json

import pytest

from browser_tools import cli
from browser_tools.core.errors import CDPError


class NetworkTransport:
    def __init__(self):
        self.handlers = {}
        self.calls = []
        self._connected = True
        self.marker = None
        self.network_enabled = False

    def on(self, event, callback, session_id=None):
        self.handlers.setdefault(event, []).append((callback, session_id))

    def off(self, event, callback):
        self.handlers[event] = [(cb, sid) for cb, sid in self.handlers[event] if cb is not callback]

    def emit(self, event, params):
        if not self.network_enabled:
            return
        for callback, session_id in list(self.handlers.get(event, [])):
            assert session_id == 'S1'
            callback(params)

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.calls.append(method)
        assert session_id == 'S1'
        if method == 'Page.getFrameTree':
            return {'frameTree': {'frame': {'id': 'f', 'url': 'https://fixture.test/'}}}
        if method == 'Network.enable':
            self.network_enabled = True
        elif method == 'Network.disable':
            self.network_enabled = False
        elif method == 'Page.reload':
            self.marker = None
        elif method == 'Runtime.evaluate':
            if params['expression'] == 'trigger':
                self.marker = 'kept'
                for identity in ('1', '2'):
                    self.emit('Network.responseReceived', {
                        'requestId': identity, 'frameId': 'f',
                        'response': {'url': '/api', 'status': 200, 'mimeType': 'text/plain'},
                    })
                    self.emit('Network.loadingFinished', {'requestId': identity})
            return {'result': {'value': self.marker, 'type': 'string'}}
        elif method == 'Target.getTargetInfo':
            return {'targetInfo': {'url': 'https://fixture.test/', 'targetId': 'T1'}}
        elif method == 'Network.getResponseBody':
            if not self.network_enabled:
                raise CDPError(-32000, 'body buffer was discarded')
            return {'body': 'response-' + params['requestId'], 'base64Encoded': False}
        return {}


def test_run_retains_responses_through_other_network_steps(monkeypatch, tmp_path, capsys):
    transport = NetworkTransport()

    @contextlib.asynccontextmanager
    async def session(*args, **kwargs):
        yield transport, 'S1'

    monkeypatch.setattr('browser_tools.one_shot.one_shot_page_session', session)
    path = tmp_path / 'steps'
    path.write_text(
        'Runtime.evaluate \'{"expression":"trigger"}\'\n'
        'network-list --duration 0\n'
        'network-get --url /api --duration 0\n'
        'network-get --request-id 1 --duration 0\n'
        'network-get --url /api --duration 0\n'
        'eval window.marker\n'
    )
    assert cli.main(['run', str(path), '--endpoint', 'http://127.0.0.1:9222']) == 0
    document = json.loads(capsys.readouterr().out)
    assert document['run'] == {'steps': 6, 'completed': 6, 'status': 'ok'}
    assert document['steps'][2]['result']['body'] == 'response-2'
    assert document['steps'][2]['result']['matched'] == 2
    assert document['steps'][3]['result']['body'] == 'response-1'
    assert document['steps'][4]['result']['body'] == 'response-2'
    assert document['steps'][5]['result']['value'] == 'kept'
    assert 'Page.reload' not in transport.calls
    assert not any(transport.handlers.get(event) for event in (
        'Network.responseReceived', 'Network.loadingFinished', 'Network.loadingFailed',
    ))


@pytest.mark.parametrize('flags', ['--reload', '--duration -1', '--duration nan', '--duration inf'])
def test_invalid_network_step_runs_nothing(flags, monkeypatch, tmp_path, capsys):
    def refuse(*args, **kwargs):
        raise AssertionError('invalid list connected to the browser')

    monkeypatch.setattr('browser_tools.curated.run_session', refuse)
    path = tmp_path / 'steps'
    path.write_text(f'eval 1\nnetwork-get --url /api {flags}\n')
    assert cli.main(['run', str(path), '--endpoint', 'http://127.0.0.1:9222']) == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'line 2' in captured.err


def test_run_deadline_interrupts_network_window(monkeypatch, tmp_path, capsys):
    import time

    transport = NetworkTransport()

    @contextlib.asynccontextmanager
    async def session(*args, **kwargs):
        yield transport, 'S1'

    monkeypatch.setattr('browser_tools.one_shot.one_shot_page_session', session)
    path = tmp_path / 'steps'
    path.write_text('network-get --url /never --duration 20\neval 1\n')
    start = time.monotonic()
    assert cli.main(['run', str(path), '--timeout', '.05', '--endpoint', 'http://127.0.0.1:9222']) == 1
    elapsed = time.monotonic() - start
    document = json.loads(capsys.readouterr().out)
    assert document['run'] == {'steps': 2, 'completed': 0, 'status': 'timeout'}
    assert len(document['steps']) == 1
    assert elapsed < 1, elapsed
    assert not transport.handlers['Network.responseReceived']
