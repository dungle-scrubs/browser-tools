"""Every capture-test launcher uses the configured browser, never an app lookup."""

from __future__ import annotations

import runpy
from pathlib import Path

import node_broker
import pytest
from parity_engines import NodeMcpSession

from browser_tools.lifecycle import CHROME_BINARY_ENV_VAR


def test_node_launcher_passes_the_test_binary(monkeypatch):
    monkeypatch.setenv(CHROME_BINARY_ENV_VAR, "/test/chrome-headless-shell")
    commands = []

    class StopBeforeLaunch(Exception):
        pass

    def broker(command):
        commands.append(command)
        raise StopBeforeLaunch

    monkeypatch.setattr(node_broker, "McpBroker", broker)
    with pytest.raises(StopBeforeLaunch):
        NodeMcpSession(channel="stable").__enter__()
    assert commands == [[
        "npx", "-y", "chrome-devtools-mcp@latest", "--isolated",
        "--executablePath", "/test/chrome-headless-shell", "--headless",
    ]]


def test_node_launcher_refuses_without_a_test_binary(monkeypatch):
    monkeypatch.delenv(CHROME_BINARY_ENV_VAR, raising=False)

    def forbidden(command):
        pytest.fail("launcher started a broker without a configured test binary")

    monkeypatch.setattr(node_broker, "McpBroker", forbidden)
    with pytest.raises(RuntimeError, match=CHROME_BINARY_ENV_VAR):
        NodeMcpSession().__enter__()


@pytest.mark.parametrize("binary", [None, "/test/chrome-headless-shell"])
def test_supervisor_fixture_does_not_discover_an_application(monkeypatch, binary):
    from browser_tools.core import launcher

    def forbidden():
        pytest.fail("test fixture searched for an installed application")

    monkeypatch.setattr(launcher, "find_chrome_binary", forbidden)
    if binary is None:
        monkeypatch.delenv(CHROME_BINARY_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(CHROME_BINARY_ENV_VAR, binary)
    module = runpy.run_path(
        str(Path(__file__).parents[1] / "test_supervisor_document_liveness.py")
    )
    assert module["_CHROME"] == binary
