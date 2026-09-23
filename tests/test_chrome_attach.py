"""External attachment contracts. Fake peers do not verify Chrome approval UI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import plistlib
import socket
import subprocess
import time

import pytest
from websockets.asyncio.server import serve

from browser_tools import chrome_discovery, cli, external_connection, step_list
from browser_tools.usage import UsageError


@pytest.fixture
def discovery(tmp_path, monkeypatch):
    profile = tmp_path / "chrome"
    profile.mkdir()
    monkeypatch.setattr(chrome_discovery, "user_data_directory", lambda channel: profile)
    monkeypatch.setattr(chrome_discovery, "remote_debugging_disallowed", lambda: False)
    registry = tmp_path / "registry.json"
    monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", str(registry))
    return profile, registry


@pytest.mark.parametrize(
    "url",
    [
        "ws://localhost:17480/devtools/browser/x",
        "ws://10.0.0.1:17480/devtools/browser/x",
        "ws://127.0.0.1:17480/devtools/page/x",
        "ws://127.0.0.1:17480/devtools/browser/",
        "ws://127.0.0.1:0/devtools/browser/x",
        "ws://127.0.0.1:bad/devtools/browser/x",
        "ws://127.0.0.1:17480/devtools/browser/x?y",
        "ws://user@127.0.0.1:17480/devtools/browser/x",
        "ws://[broken:17480/devtools/browser/x",
    ],
)
def test_invalid_websocket_is_usage_error(url, discovery, capsys):
    assert cli.main(["snapshot", "--endpoint", url]) == 2
    assert capsys.readouterr().err.startswith("error:")
    assert not discovery[1].exists()


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "1",
        "0\n/devtools/browser/x",
        "17480\n/other",
        "17480\n/devtools/browser/x\nextra",
        "abc\n/devtools/browser/x",
        "17480\n@remote/devtools/browser/x",
    ],
)
def test_missing_or_malformed_file_names_path(text, discovery, capsys):
    path = discovery[0] / "DevToolsActivePort"
    if text is not None:
        path.write_text(text)
    started = time.monotonic()
    assert cli.main(["snapshot", "--chrome-profile"]) == 2
    assert time.monotonic() - started < 1
    error = capsys.readouterr().err
    assert str(path) in error
    assert "Traceback" not in error
    if text is None:
        assert "both IPv4 and IPv6" in error
    assert not discovery[1].exists()


def test_policy_refusal_has_different_remedy(discovery, monkeypatch, capsys):
    assert cli.main(["snapshot", "--chrome-profile"]) == 2
    assert "not enabled" in capsys.readouterr().err
    monkeypatch.setattr(chrome_discovery, "remote_debugging_disallowed", lambda: True)
    assert cli.main(["snapshot", "--chrome-profile"]) == 2
    error = capsys.readouterr().err
    assert "not permitted" in error and "administrator" in error
    assert "devtools.remote_debugging.allowed" in error
    assert not discovery[1].exists()


@pytest.mark.parametrize(
    "platform,channel,ending",
    [
        ("darwin", "stable", "Google/Chrome"),
        ("darwin", "beta", "Google/Chrome Beta"),
        ("darwin", "dev", "Google/Chrome Dev"),
        ("darwin", "canary", "Google/Chrome Canary"),
        ("linux", "stable", "google-chrome"),
        ("linux", "beta", "google-chrome-beta"),
        ("linux", "dev", "google-chrome-unstable"),
        ("linux", "canary", "google-chrome-canary"),
        ("win32", "stable", "Google/Chrome/User Data"),
        ("win32", "beta", "Google/Chrome Beta/User Data"),
        ("win32", "dev", "Google/Chrome Dev/User Data"),
        ("win32", "canary", "Google/Chrome SxS/User Data"),
    ],
)
def test_channel_paths(platform, channel, ending, monkeypatch):
    monkeypatch.setattr(chrome_discovery.sys, "platform", platform)
    monkeypatch.setenv("LOCALAPPDATA", "/local")
    monkeypatch.delenv("CHROME_USER_DATA_DIR", raising=False)
    assert str(chrome_discovery.user_data_directory(channel)).endswith(ending)


@pytest.mark.parametrize("verb", ["launch", "stop", "cleanup", "profile", "guide", "status"])
def test_registry_verbs_refuse_discovery(verb, discovery, capsys):
    assert cli.main([verb, "--chrome-profile"]) == 2
    assert "does not take --chrome-profile" in capsys.readouterr().err
    assert not discovery[1].exists()


def test_flags_and_steps_refuse_conflicting_endpoints(discovery, capsys):
    assert (
        cli.main(
            [
                "snapshot",
                "--chrome-profile",
                "--endpoint",
                "ws://127.0.0.1:17480/devtools/browser/x",
            ]
        )
        == 2
    )
    assert "cannot be combined" in capsys.readouterr().err
    with pytest.raises(UsageError, match="chrome-profile"):
        step_list.validate("snapshot --chrome-profile")


@pytest.mark.asyncio
async def test_no_approval_is_bounded_in_raw_and_curated_verbs(discovery, monkeypatch, capsys):
    # A non-answering peer tests the timer, not Chrome's Allow UI.
    monkeypatch.setattr(external_connection, "APPROVAL_TIMEOUT", 0.2)
    stop = asyncio.Event()

    async def stalled(reader, writer):
        try:
            await stop.wait()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(stalled, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    assert port != 9222
    async with server:
        try:
            for verb in (["Browser.getVersion"], ["snapshot"]):
                started = time.monotonic()
                code = await asyncio.to_thread(
                    cli.main, [*verb, "--endpoint", f"ws://127.0.0.1:{port}/devtools/browser/test"]
                )
                assert code == 1
                assert time.monotonic() - started < 2
                error = capsys.readouterr().err
                # Two plain TCP listeners, with no Chrome anywhere, reach this
                # message. It must name the causes and the check, not assert
                # the one cause that cannot be true here.
                assert "Allow prompt" in error
                assert "busy or hung" in error
                assert "not a browser" in error
                assert "No approval received" not in error
                assert not discovery[1].exists()
        finally:
            stop.set()
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_wrong_websocket_peer_is_not_given_a_verb(discovery, capsys):
    methods = []

    async def wrong_peer(ws):
        async for raw in ws:
            message = json.loads(raw)
            methods.append(message["method"])
            await ws.send(json.dumps({"id": message["id"], "result": {}}))

    async with serve(wrong_peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        assert port != 9222
        code = await asyncio.to_thread(
            cli.main,
            [
                "Runtime.evaluate",
                '{"expression":"1+1"}',
                "--endpoint",
                f"ws://127.0.0.1:{port}/devtools/browser/test",
            ],
        )
    assert code == 1
    assert methods == ["Browser.getVersion"]
    assert "DevTools" in capsys.readouterr().err
    assert not discovery[1].exists()


@contextlib.contextmanager
def shell(profile, port):
    binary = os.environ.get("BROWSER_TOOLS_CHROME_BINARY")
    if not binary or ".app/" in binary:
        pytest.skip("requires the Playwright headless shell")
    log = profile / "shell.log"
    with log.open("w") as output:
        process = subprocess.Popen(
            [
                binary,
                "--headless",
                f"--user-data-dir={profile}",
                f"--remote-debugging-port={port}",
                "--no-first-run",
                "about:blank",
            ],
            stdout=output,
            stderr=output,
        )
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                text = log.read_text()
                if (profile / "DevToolsActivePort").exists() or "Cannot start http server" in text:
                    break
                if process.poll() is not None:
                    if "MachPortRendezvousServer" in text and "Permission denied" in text:
                        pytest.skip(
                            "sandbox denies headless-shell Mach port registration; live attachment unverified"
                        )
                    pytest.fail(f"headless shell exited: {text}")
                time.sleep(0.02)
            yield process
        finally:
            process.terminate()
            process.wait(timeout=5)


@contextlib.contextmanager
def held_ports():
    with socket.socket() as ipv4, socket.socket(socket.AF_INET6) as ipv6:
        ipv4.bind(("127.0.0.1", 0))
        port = ipv4.getsockname()[1]
        assert port != 9222
        ipv6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        ipv6.bind(("::1", port))
        ipv4.listen()
        ipv6.listen()
        yield port, ipv4, ipv6


@pytest.mark.parametrize("case", ["free", "ipv4-held", "both-held", "zero"])
def test_live_port_cases_and_registry_absence(case, discovery, capsys):
    profile, registry = discovery
    with held_ports() as (preferred, ipv4, ipv6):
        if case in ("free", "zero"):
            ipv4.close()
        if case != "both-held":
            ipv6.close()
        with shell(profile, 0 if case == "zero" else preferred) as process:
            path = profile / "DevToolsActivePort"
            if case == "both-held":
                assert process.poll() is None
                assert not path.exists()
                assert cli.main(["snapshot", "--chrome-profile"]) == 2
                assert "both IPv4 and IPv6" in capsys.readouterr().err
            else:
                resolved = chrome_discovery.discover_chrome()
                assert resolved.port != 9222
                if case != "zero":
                    assert resolved.port == preferred
                assert resolved.websocket_url is not None
                assert resolved.websocket_url.endswith(path.read_text().splitlines()[1])
                assert resolved.flag == "--chrome-profile"
                assert cli.main(["eval", "6 * 7", "--chrome-profile"]) == 0
                assert json.loads(capsys.readouterr().out)["value"] == 42
                host = "[::1]" if case == "ipv4-held" else "127.0.0.1"
                url = f"ws://{host}:{resolved.port}{path.read_text().splitlines()[1]}"
                assert (
                    cli.main(
                        [
                            "Runtime.evaluate",
                            '{"expression":"40+2","returnByValue":true}',
                            "--endpoint",
                            url,
                        ]
                    )
                    == 0
                )
                assert json.loads(capsys.readouterr().out)["result"]["value"] == 42
                for method in ("Browser.close", "Browser.crash"):
                    assert cli.main([method, "--chrome-profile"]) == 2
                    assert "refused" in capsys.readouterr().err
                assert cli.main(["snapshot", "--chrome-profile", "--target", "missing-target"]) == 1
                assert capsys.readouterr().err
                assert process.poll() is None
            assert not registry.exists()


@pytest.mark.asyncio
async def test_only_the_family_that_knows_the_guid_is_dialled(discovery, capsys):
    """HIGH-3. Two peers answer /json/version; only the one with the port

    file's browser path is a candidate, and only it gets a WebSocket. The
    reviewed build raced both families and gave an impostor 2 runs out of 10.
    """
    impostor_sockets = []
    real_methods = []
    browser_path = "/devtools/browser/the-real-guid"

    def version(url):
        def respond(connection, request):
            if request.path == "/json/version":
                return connection.respond(200, json.dumps({"webSocketDebuggerUrl": url}))
            return None

        return respond

    async def impostor(ws):
        impostor_sockets.append(ws)
        async for raw in ws:
            await ws.send(json.dumps({"id": json.loads(raw)["id"], "result": {}}))

    async def real(ws):
        async for raw in ws:
            message = json.loads(raw)
            real_methods.append(message["method"])
            result = {
                "Browser.getVersion": {"protocolVersion": "1.3", "product": "Chrome/test"},
                "Target.getTargets": {
                    "targetInfos": [{"targetId": "page1", "type": "page", "url": "about:blank"}]
                },
                "Target.attachToTarget": {"sessionId": "session1"},
                "Runtime.evaluate": {"result": {"value": 42}},
            }.get(message["method"], {})
            await ws.send(json.dumps({"id": message["id"], "result": result}))

    async with serve(impostor, "127.0.0.1", 0, process_request=version(
        "ws://127.0.0.1:0/devtools/browser/impostor-guid"
    )) as ipv4:
        port = ipv4.sockets[0].getsockname()[1]
        assert port != 9222
        async with serve(real, "::1", port, process_request=version(
            f"ws://[::1]:{port}{browser_path}"
        )):
            (discovery[0] / "DevToolsActivePort").write_text(f"{port}\n{browser_path}\n")
            for _ in range(5):
                real_methods.clear()
                code = await asyncio.to_thread(
                    cli.main, ["Runtime.evaluate", '{"expression":"40+2"}', "--chrome-profile"]
                )
                assert code == 0
                assert json.loads(capsys.readouterr().out)["result"]["value"] == 42
                assert real_methods == [
                    "Browser.getVersion",
                    "Target.getTargets",
                    "Target.attachToTarget",
                    "Runtime.evaluate",
                    "Target.detachFromTarget",
                ]
    assert impostor_sockets == []
    assert not discovery[1].exists()


@pytest.mark.asyncio
async def test_one_invocation_opens_one_websocket(discovery, capsys):
    """Two verifiable peers, one connection. Chrome asks approval per

    connection, so a second connection is a second Allow prompt for one
    invocation, and the guide says "click Allow" in the singular.
    """
    connections = {"127.0.0.1": 0, "[::1]": 0}
    browser_path = "/devtools/browser/shared-guid"

    def version(host, port_holder):
        def respond(connection, request):
            if request.path == "/json/version":
                return connection.respond(
                    200,
                    json.dumps(
                        {"webSocketDebuggerUrl": f"ws://{host}:{port_holder[0]}{browser_path}"}
                    ),
                )
            return None

        return respond

    def peer(host):
        async def handler(ws):
            connections[host] += 1
            async for raw in ws:
                message = json.loads(raw)
                result = {
                    "Browser.getVersion": {"protocolVersion": "1.3", "product": "Chrome/test"},
                    "Target.getTargets": {
                        "targetInfos": [
                            {"targetId": "page1", "type": "page", "url": "about:blank"}
                        ]
                    },
                    "Target.attachToTarget": {"sessionId": "session1"},
                    "Runtime.evaluate": {"result": {"value": 1}},
                }.get(message["method"], {})
                await ws.send(json.dumps({"id": message["id"], "result": result}))

        return handler

    holder = [0]
    async with serve(
        peer("127.0.0.1"), "127.0.0.1", 0, process_request=version("127.0.0.1", holder)
    ) as ipv4:
        holder[0] = ipv4.sockets[0].getsockname()[1]
        assert holder[0] != 9222
        async with serve(
            peer("[::1]"), "::1", holder[0], process_request=version("[::1]", holder)
        ):
            (discovery[0] / "DevToolsActivePort").write_text(f"{holder[0]}\n{browser_path}\n")
            code = await asyncio.to_thread(
                cli.main, ["Runtime.evaluate", '{"expression":"1"}', "--chrome-profile"]
            )
    assert code == 0
    capsys.readouterr()
    assert connections == {"127.0.0.1": 1, "[::1]": 0}


@pytest.mark.asyncio
async def test_a_peer_that_cannot_prove_the_guid_is_refused(discovery, capsys):
    """HIGH-3, the stale port file. The Chrome that wrote the file is gone and

    something else now holds the port. The reviewed build delivered
    Page.navigate to it.
    """
    dialled = []

    def version(connection, request):
        if request.path == "/json/version":
            return connection.respond(
                200, json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/mine"})
            )
        return None

    async def impostor(ws):
        dialled.append(ws.request.path)

    async with serve(impostor, "127.0.0.1", 0, process_request=version) as server:
        port = server.sockets[0].getsockname()[1]
        assert port != 9222
        (discovery[0] / "DevToolsActivePort").write_text(
            f"{port}\n/devtools/browser/gone-with-the-browser\n"
        )
        code = await asyncio.to_thread(
            cli.main, ["Page.navigate", '{"url":"https://example.com"}', "--chrome-profile"]
        )
    assert code == 2
    error = capsys.readouterr().err
    assert "stale" in error
    assert "Traceback" not in error
    assert dialled == []
    assert not discovery[1].exists()


@pytest.mark.parametrize("method", ["Browser.close", "Browser.crash"])
def test_discovered_browser_enders_never_connect(method, discovery, capsys, monkeypatch):
    (discovery[0] / "DevToolsActivePort").write_text("17480\n/devtools/browser/x\n")

    def forbidden(*args, **kwargs):
        pytest.fail("a refused method tried to connect")

    monkeypatch.setattr(external_connection, "_verified_client", forbidden)
    assert cli.main([method, "--chrome-profile"]) == 2
    assert "refused" in capsys.readouterr().err
    assert not discovery[1].exists()


@pytest.mark.asyncio
async def test_default_30_second_approval_deadline(discovery, capsys, record_property):
    assert external_connection.APPROVAL_TIMEOUT == 30
    stop = asyncio.Event()

    async def stalled(reader, writer):
        try:
            await stop.wait()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(stalled, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    assert port != 9222
    async with server:
        try:
            started = time.monotonic()
            code = await asyncio.to_thread(
                cli.main,
                [
                    "Browser.getVersion",
                    "--endpoint",
                    f"ws://127.0.0.1:{port}/devtools/browser/pending",
                ],
            )
            elapsed = time.monotonic() - started
            record_property("approval_timeout_elapsed_seconds", elapsed)
            assert code == 1
            assert 29 <= elapsed < 31
            assert "got no DevTools answer in 30s" in capsys.readouterr().err
            assert not discovery[1].exists()
        finally:
            stop.set()
            await asyncio.sleep(0)


@pytest.mark.parametrize(
    "argv",
    [
        ["window-border", "off", "--chrome-profile"],
        ["snapshot", "--chrome-profile", "~/dir", "--endpoint", "http://127.0.0.1:9222"],
        ["snapshot", "--chrome-profile", "a", "--chrome-profile", "b"],
    ],
)
def test_invalid_discovery_flags_do_not_read_the_profile(argv, discovery, monkeypatch, capsys):
    def forbidden(*args):
        pytest.fail("invalid flags reached discovery")

    monkeypatch.setattr(chrome_discovery, "discover_chrome", forbidden)
    assert cli.main(argv) == 2
    assert capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["snapshot", "--chrome-profile", "bogus"],
    ["snapshot", "--chrome-profile=bogus"],
])
def test_a_directory_that_is_not_there_names_the_directory(argv, discovery, capsys, tmp_path):
    """The flag takes a path now, so a value bt cannot use is a path that is

    not there. The reviewed build answered "unknown Chrome channel 'bogus'",
    which is the wrong shape of answer for a flag that takes a directory.
    """
    assert cli.main([*argv[:1], *[a.replace("bogus", str(tmp_path / "nope")) for a in argv[1:]]]) == 2
    error = capsys.readouterr().err
    assert str(tmp_path / "nope") in error
    assert "Traceback" not in error


@pytest.mark.asyncio
async def test_websocket_redirect_is_not_followed(discovery, capsys):
    requests = []

    def redirect(connection, request):
        requests.append(request.path)
        response = connection.respond(302, "redirect forbidden")
        response.headers["Location"] = "ws://192.0.2.1:17480/devtools/browser/remote"
        return response

    async def unused(ws):
        pytest.fail("redirected connection was accepted")

    async with serve(unused, "127.0.0.1", 0, process_request=redirect) as server:
        port = server.sockets[0].getsockname()[1]
        assert port != 9222
        code = await asyncio.to_thread(
            cli.main,
            ["Browser.getVersion", "--endpoint", f"ws://127.0.0.1:{port}/devtools/browser/test"],
        )
    assert code == 1
    assert "302" in capsys.readouterr().err
    assert requests == ["/devtools/browser/test"]
    assert not discovery[1].exists()


@pytest.mark.asyncio
async def test_403_is_distinct_from_an_approval_timeout(discovery, capsys):
    def refuse(connection, request):
        return connection.respond(403, "Forbidden")

    async def unused(ws):
        pytest.fail("forbidden connection was accepted")

    async with serve(unused, "127.0.0.1", 0, process_request=refuse) as server:
        port = server.sockets[0].getsockname()[1]
        assert port != 9222
        code = await asyncio.to_thread(
            cli.main,
            ["Browser.getVersion", "--endpoint", f"ws://127.0.0.1:{port}/devtools/browser/test"],
        )
    assert code == 1
    error = capsys.readouterr().err
    assert "not permit" in error and "RemoteDebuggingAllowed" in error
    assert "No approval received" not in error
    assert not discovery[1].exists()


# --------------------------------------------------------------------------- #
# HIGH-1: every send is bounded, not only the handshake
# --------------------------------------------------------------------------- #


def _devtools_http(document, *, path="/json/version"):
    """A ``process_request`` that answers one DevTools HTTP path."""

    def respond(connection, request):
        if request.path == path:
            return connection.respond(200, json.dumps(document))
        return None

    return respond


def _replies(port, silent_after=(), sessionless=False):
    """A CDP peer that answers by table and ignores the methods in ``silent_after``."""
    seen = []

    async def handler(ws):
        async for raw in ws:
            message = json.loads(raw)
            seen.append(message["method"])
            if message["method"] in silent_after:
                continue
            attach = {} if sessionless else {"sessionId": "session1"}
            result = {
                "Browser.getVersion": {"protocolVersion": "1.3", "product": "Chrome/test"},
                "Target.getTargets": {
                    "targetInfos": [{"targetId": "page1", "type": "page", "url": "about:blank"}]
                },
                "Target.attachToTarget": attach,
                "Runtime.evaluate": {"result": {"value": 1}},
            }.get(message["method"], {})
            await ws.send(json.dumps({"id": message["id"], "result": result}))

    return handler, seen


@pytest.mark.parametrize(
    "verb",
    [
        ["Runtime.evaluate", '{"expression":"1"}'],
        ["attach", "+Page.loadEventFired"],
        ["console-list", "--duration", "0.5"],
        ["snapshot"],
    ],
)
@pytest.mark.asyncio
async def test_a_verified_peer_that_goes_silent_does_not_hang(verb, discovery, monkeypatch, capsys):
    """HIGH-1. The peer completes the handshake and answers Browser.getVersion,

    then answers nothing. The reviewed build had these four still running at
    90 to 120 seconds, because the 30-second bound closed before `yield client`
    and every later send defaulted to no timeout.
    """
    assert external_connection.SESSION_TIMEOUT == 30
    assert external_connection.COMMAND_TIMEOUT == 120
    monkeypatch.setattr("browser_tools.one_shot.SESSION_TIMEOUT", 0.3)
    handler, seen = _replies(0, silent_after={"Target.getTargets"})

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        assert port != 9222
        started = time.monotonic()
        code = await asyncio.to_thread(
            cli.main, [*verb, "--endpoint", f"ws://127.0.0.1:{port}/devtools/browser/test"]
        )
        elapsed = time.monotonic() - started
    assert code == 1
    assert elapsed < 10, f"{verb} took {elapsed:.1f}s"
    assert seen == ["Browser.getVersion", "Target.getTargets"]
    error = capsys.readouterr().err
    assert "stopped answering" in error
    assert "Traceback" not in error
    assert not discovery[1].exists()


@pytest.mark.asyncio
async def test_a_silent_teardown_does_not_cost_the_verb_its_answer(discovery, monkeypatch, capsys):
    """HIGH-1, teardown. Target.detachFromTarget was unbounded too, so a

    completed verb could hang on the way out.
    """
    monkeypatch.setattr("browser_tools.one_shot.TEARDOWN_TIMEOUT", 0.3)
    handler, seen = _replies(0, silent_after={"Target.detachFromTarget"})

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        assert port != 9222
        started = time.monotonic()
        code = await asyncio.to_thread(
            cli.main,
            [
                "Runtime.evaluate",
                '{"expression":"1"}',
                "--endpoint",
                f"ws://127.0.0.1:{port}/devtools/browser/test",
            ],
        )
        elapsed = time.monotonic() - started
    assert code == 0
    assert elapsed < 10
    assert seen[-1] == "Target.detachFromTarget"
    assert json.loads(capsys.readouterr().out)["result"]["value"] == 1


# --------------------------------------------------------------------------- #
# HIGH-2: the peer does not choose the host bt dials
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_http_endpoint_cannot_redirect_bt_off_box(discovery, capsys):
    """HIGH-2. /json/version named ws://192.0.2.1:41234/... and the reviewed

    build dialled it, off-box, failing 11 seconds later with an unhandled
    TimeoutError traceback.
    """
    rogue = "ws://192.0.2.1:41234/devtools/browser/rogue"

    async def unused(ws):
        pytest.fail("the rogue endpoint was dialled")

    async with serve(
        unused, "127.0.0.1", 0, process_request=_devtools_http({"webSocketDebuggerUrl": rogue})
    ) as server:
        port = server.sockets[0].getsockname()[1]
        assert port != 9222
        started = time.monotonic()
        code = await asyncio.to_thread(
            cli.main,
            ["Runtime.evaluate", '{"expression":"1"}', "--endpoint", f"http://127.0.0.1:{port}"],
        )
        elapsed = time.monotonic() - started
    assert code == 1
    assert elapsed < 5
    error = capsys.readouterr().err
    assert "192.0.2.1" in error
    assert "not loopback" in error
    assert "Traceback" not in error
    assert not discovery[1].exists()


@pytest.mark.asyncio
async def test_an_http_endpoint_dials_the_family_that_was_named(discovery, capsys):
    """M5. core.cdp_client requests http://localhost:{port}, so a listener on

    [::1] only was reached through --endpoint http://127.0.0.1.
    """
    handler, seen = _replies(0)
    async with serve(
        handler,
        "::1",
        0,
        process_request=_devtools_http({"webSocketDebuggerUrl": "ws://[::1]:1/devtools/browser/x"}),
    ) as server:
        port = server.sockets[0].getsockname()[1]
        assert port != 9222
        code = await asyncio.to_thread(
            cli.main,
            ["Runtime.evaluate", '{"expression":"1"}', "--endpoint", f"http://127.0.0.1:{port}"],
        )
    assert code == 1
    assert seen == []
    assert f"no DevTools HTTP endpoint at 127.0.0.1:{port}" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# M4 / M6 / LOW: the invocation's own refusals and diagnostics
# --------------------------------------------------------------------------- #


def test_an_abbreviated_endpoint_still_collides_with_the_profile(discovery, monkeypatch, capsys):
    """M4. argparse accepts --endp for --endpoint, the exact-token scan did

    not, and the discovered browser silently replaced the endpoint typed.
    """
    def forbidden(*args):
        pytest.fail("discovery ran for an invocation that names two browsers")

    monkeypatch.setattr(chrome_discovery, "discover_chrome", forbidden)
    code = cli.main(
        ["eval", "1", "--chrome-profile", "--endp", "ws://127.0.0.1:1/devtools/browser/zzz"]
    )
    assert code == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_a_registry_verb_names_its_own_refusal(discovery, capsys):
    """LOW. `bt profile --chrome-profile list` reported an unknown channel

    'list'; what is wrong with it is that profile does not take the flag.
    """
    assert cli.main(["profile", "--chrome-profile", "list"]) == 2
    error = capsys.readouterr().err
    assert "profile does not take --chrome-profile" in error
    assert "channel" not in error


@pytest.mark.asyncio
async def test_a_json_operand_is_not_read_as_the_profile(discovery, capsys):
    """LOW. `bt Runtime.evaluate --chrome-profile '{"expression":"1"}'` read

    the params as the flag's value.
    """
    handler, seen = _replies(0)
    browser_path = "/devtools/browser/operand-guid"
    async with serve(
        handler,
        "127.0.0.1",
        0,
        process_request=_devtools_http({"webSocketDebuggerUrl": f"ws://127.0.0.1:1{browser_path}"}),
    ) as server:
        port = server.sockets[0].getsockname()[1]
        assert port != 9222
        (discovery[0] / "DevToolsActivePort").write_text(f"{port}\n{browser_path}\n")
        code = await asyncio.to_thread(
            cli.main, ["Runtime.evaluate", "--chrome-profile", '{"expression":"1"}']
        )
    assert code == 0
    assert "Runtime.evaluate" in seen
    assert json.loads(capsys.readouterr().out)["result"]["value"] == 1


@pytest.mark.asyncio
async def test_a_reply_without_a_session_id_is_a_diagnostic(discovery, capsys):
    """LOW. one_shot read session_result["sessionId"] straight out of a reply

    it had already decided to trust, printing a bare KeyError traceback.
    """
    handler, _ = _replies(0, sessionless=True)
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        assert port != 9222
        code = await asyncio.to_thread(
            cli.main,
            [
                "Runtime.evaluate",
                '{"expression":"1"}',
                "--endpoint",
                f"ws://127.0.0.1:{port}/devtools/browser/test",
            ],
        )
    assert code == 1
    error = capsys.readouterr().err
    assert "sessionId" in error
    assert "Traceback" not in error


@pytest.mark.parametrize("verb", [["Runtime.evaluate", '{"expression":"1"}'], ["snapshot"]])
def test_a_dead_endpoint_reports_the_port_holder_not_bt_launch(verb, discovery, capsys):
    """M6. `curated` changed to `if external and not reason`, and reason is set

    on every ordinary connect failure, so the port-holder diagnosis became
    unreachable and the remedy named bt launch and bt status -- neither of
    which can see an external browser.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert port != 9222
    assert cli.main([*verb, "--endpoint", f"http://127.0.0.1:{port}"]) == 1
    error = capsys.readouterr().err
    assert f"nothing is listening on port {port}" in error
    assert "bt launch" not in error
    assert "bt status" not in error


# --------------------------------------------------------------------------- #
# The flag takes a directory
# --------------------------------------------------------------------------- #


def test_a_default_data_directory_is_refused_with_the_instruction(monkeypatch, capsys, tmp_path):
    """The flag's only reachable target used to be the one directory Chrome

    declines. Now it says so, and says what to do instead.
    """
    monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", str(tmp_path / "registry.json"))
    default = chrome_discovery.user_data_directory("stable")
    assert cli.main(["snapshot", "--chrome-profile", str(default)]) == 2
    error = capsys.readouterr().err
    assert "non-default data directory" in error
    assert "--user-data-dir" in error
    assert "--remote-debugging-port=0" in error
    assert not (tmp_path / "registry.json").exists()


def test_a_channel_name_that_resolves_to_a_default_is_refused(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", str(tmp_path / "registry.json"))
    assert cli.main(["snapshot", "--chrome-profile", "stable"]) == 2
    assert "non-default data directory" in capsys.readouterr().err


def test_a_directory_path_is_read_as_a_user_data_directory(discovery, capsys, tmp_path):
    """The working flow: the person starts Chrome on a directory of their own

    and names that directory.
    """
    profile = tmp_path / "bt-debug-profile"
    profile.mkdir()
    with shell(profile, 0):
        resolved = chrome_discovery.discover_chrome(str(profile))
        assert resolved.flag == "--chrome-profile"
        assert resolved.websocket_url is not None
        assert cli.main(["eval", "6 * 7", "--chrome-profile", str(profile)]) == 0
        assert json.loads(capsys.readouterr().out)["value"] == 42
    assert not discovery[1].exists()


def test_the_port_file_guid_is_never_sent_to_the_other_family(discovery, capsys):
    """HIGH-3, the leak. A plain non-CDP listener on the IPv4 side logged

    GET /devtools/browser/<guid>, which is the browser's whole authentication.
    """
    import http.server
    import threading

    seen_paths = []

    class Logger(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen_paths.append(self.path)
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert port != 9222
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Logger)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        profile = discovery[0]
        with shell(profile, port):
            path = profile / "DevToolsActivePort"
            if not path.exists():
                pytest.skip("the shell did not bind the held port on the other family")
            assert cli.main(["eval", "6 * 7", "--chrome-profile"]) == 0
            assert json.loads(capsys.readouterr().out)["value"] == 42
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert seen_paths, "the IPv4 side was never probed at all"
    assert all(p == "/json/version" for p in seen_paths), seen_paths
    assert not discovery[1].exists()


# --------------------------------------------------------------------------- #
# M8: managed policy detection
# --------------------------------------------------------------------------- #


def test_the_per_user_managed_preferences_path_is_read():
    """M8. macOS puts the managed domain under /Library/Managed

    Preferences/<user>/, which is the path this machine has and the one the
    reviewed build never read.
    """
    import getpass

    paths = [str(p) for p in chrome_discovery._macos_policy_paths()]
    assert f"/Library/Managed Preferences/{getpass.getuser()}/com.google.Chrome.plist" in paths
    assert "/Library/Managed Preferences/com.google.Chrome.plist" in paths


@pytest.mark.parametrize("value", [False, 0])
def test_an_integer_zero_policy_is_an_explicit_refusal(value, discovery, monkeypatch, capsys, tmp_path):
    """M8. A plist writes <false/> and <integer>0</integer>, and Chrome reads

    both as off. Testing `is False` saw only the first.
    """
    plist = tmp_path / "com.google.Chrome.plist"
    plist.write_bytes(plistlib.dumps({"RemoteDebuggingAllowed": value}))
    assert chrome_discovery._policy_forbids(plistlib.loads(plist.read_bytes())["RemoteDebuggingAllowed"])
    monkeypatch.setattr(chrome_discovery.sys, "platform", "darwin")
    monkeypatch.setattr(chrome_discovery, "_macos_policy_paths", lambda: [plist])
    assert cli.main(["snapshot", "--chrome-profile"]) == 2
    assert "RemoteDebuggingAllowed" in capsys.readouterr().err
    assert not discovery[1].exists()


@pytest.mark.asyncio
async def test_help_reads_the_live_schema_from_an_external_endpoint(discovery, capsys):
    """LOW. run_help refused live help for every external endpoint although

    Chrome serves /json/protocol beside /json/version, and the static text it
    printed instead ended "Launch a browser first: bt launch" -- wrong advice
    for someone attaching.
    """
    schema = {
        "domains": [
            {
                "domain": "Page",
                "commands": [{"name": "navigate", "parameters": [], "returns": []}],
                "events": [],
            }
        ]
    }

    def respond(connection, request):
        if request.path == "/json/protocol":
            return connection.respond(200, json.dumps(schema))
        if request.path == "/json/version":
            return connection.respond(
                200, json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/x"})
            )
        return None

    async def unused(ws):
        pytest.fail("help opened a WebSocket")

    async with serve(unused, "127.0.0.1", 0, process_request=respond) as server:
        port = server.sockets[0].getsockname()[1]
        assert port != 9222
        code = await asyncio.to_thread(
            cli.main, ["help", "Page.navigate", "--endpoint", f"http://127.0.0.1:{port}"]
        )
    assert code == 0
    out = capsys.readouterr().out
    assert "Page.navigate" in out
    assert "bt launch" not in out


@pytest.mark.asyncio
async def test_help_without_a_schema_does_not_send_you_to_bt_launch(discovery, capsys):
    async def unused(ws):
        pytest.fail("help opened a WebSocket")

    async with serve(unused, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        assert port != 9222
        code = await asyncio.to_thread(
            cli.main, ["help", "--endpoint", f"ws://127.0.0.1:{port}/devtools/browser/x"]
        )
    assert code == 0
    out = capsys.readouterr().out
    assert "did not serve /json/protocol" in out
    assert "bt launch" not in out


def test_help_with_no_browser_still_says_to_launch_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", str(tmp_path / "registry.json"))
    assert cli.main(["help"]) == 0
    assert "Launch a browser first: bt launch" in capsys.readouterr().out
