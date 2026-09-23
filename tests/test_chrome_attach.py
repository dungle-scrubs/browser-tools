"""External attachment contracts. Fake peers do not verify Chrome approval UI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
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
                assert "click Allow" in capsys.readouterr().err
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
                assert resolved.websocket_urls[0].endswith(path.read_text().splitlines()[1])
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
async def test_discovery_ignores_non_cdp_ipv4_peer_and_drives_ipv6(discovery, capsys):
    calls = []

    async def cdp_peer(ws):
        async for raw in ws:
            msg = json.loads(raw)
            calls.append(msg["method"])
            result = {
                "Browser.getVersion": {"protocolVersion": "1.3", "product": "Chrome/test"},
                "Target.getTargets": {
                    "targetInfos": [{"targetId": "page1", "type": "page", "url": "about:blank"}]
                },
                "Target.attachToTarget": {"sessionId": "session1"},
                "Runtime.evaluate": {"result": {"value": 42}},
            }.get(msg["method"], {})
            await ws.send(json.dumps({"id": msg["id"], "result": result}))

    async def other_peer(ws):
        async for raw in ws:
            msg = json.loads(raw)
            await ws.send(json.dumps({"id": msg["id"], "result": {}}))

    async with serve(other_peer, "127.0.0.1", 0) as ipv4:
        port = ipv4.sockets[0].getsockname()[1]
        assert port != 9222
        async with serve(cdp_peer, "::1", port):
            (discovery[0] / "DevToolsActivePort").write_text(
                f"{port}\n/devtools/browser/distinct-id\n"
            )
            code = await asyncio.to_thread(
                cli.main, ["Runtime.evaluate", '{"expression":"40+2"}', "--chrome-profile"]
            )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["result"]["value"] == 42
    assert calls == [
        "Browser.getVersion",
        "Target.getTargets",
        "Target.attachToTarget",
        "Runtime.evaluate",
        "Target.detachFromTarget",
    ]
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
            assert external_connection.APPROVAL_MESSAGE in capsys.readouterr().err
            assert not discovery[1].exists()
        finally:
            stop.set()
            await asyncio.sleep(0)


@pytest.mark.parametrize(
    "argv",
    [
        ["snapshot", "--chrome-profile", "bogus"],
        ["window-border", "off", "--chrome-profile"],
        ["snapshot", "--chrome-profile=bogus"],
    ],
)
def test_invalid_discovery_flags_do_not_read_the_profile(argv, discovery, monkeypatch, capsys):
    def forbidden(*args):
        pytest.fail("invalid flags reached discovery")

    monkeypatch.setattr(chrome_discovery, "discover_chrome", forbidden)
    assert cli.main(argv) == 2
    assert capsys.readouterr().err


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
