"""CLI handler tools validate input before opening a connection."""

import pytest

from browser_tools import cli


def test_unknown_tool_lists_available_tools_before_connecting(capsys):
    assert cli.main(["tool", "not_a_tool"]) == 2
    error = capsys.readouterr().err
    assert "Unknown handler tool" in error
    assert "get_text" in error


@pytest.mark.parametrize(
    "args, message",
    [
        (["screencast_start"], "screencast record"),
        (["get_text", "[]"], "JSON object"),
        (["get_text", "{"], "JSON"),
    ],
)
def test_invalid_tool_calls_are_usage_errors(args, message, capsys):
    assert cli.main(["tool", *args]) == 2
    assert message in capsys.readouterr().err


def test_curated_unknown_instance_uses_bt_remedy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", str(tmp_path / "registry.json"))
    assert cli.main(["snapshot", "ghost"]) == 1
    error = capsys.readouterr().err
    assert "bt launch" in error
    assert "chrome-agent" not in error


@pytest.mark.parametrize("case", ["pdf", "error"])
def test_tool_dispatches_arguments_and_reports_handler_errors(case, tmp_path, monkeypatch, capsys):
    import base64
    from unittest.mock import AsyncMock

    from browser_tools import cdp_handler, curated

    calls = []

    class Client:
        def __init__(self, url):
            pass

        async def connect(self):
            pass

        async def close(self):
            calls.append(("close", None))

        def on(self, *args, **kwargs):
            pass

        def off(self, *args, **kwargs):
            pass

        async def send(self, method, params=None, session_id=None):
            calls.append((method, params))
            if method == "Target.attachToTarget":
                return {"sessionId": "session"}
            if method == "Page.printToPDF":
                assert session_id == "session"
                return {"data": base64.b64encode(b"%PDF-test").decode()}
            return {}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(curated, "_resolve_port", lambda *args: 9222)
    monkeypatch.setattr(curated, "resolve_page_target", AsyncMock(return_value="target"))
    monkeypatch.setattr(cdp_handler, "CDPClient", Client)
    monkeypatch.setattr(cdp_handler, "get_ws_url", lambda **kwargs: "ws://test/browser")
    if case == "pdf":
        assert cli.main(["tool", "export_pdf", '{"path":"relative.pdf","landscape":true}']) == 0
        assert (tmp_path / "relative.pdf").read_bytes() == b"%PDF-test"
        assert (
            "Page.printToPDF",
            {"landscape": True, "printBackground": True, "transferMode": "ReturnAsBase64"},
        ) in calls
    else:
        assert cli.main(["tool", "get_text", "{}"]) == 1
        assert capsys.readouterr().err == "error: selector is required\n"
    assert calls[-1] == ("close", None)
