"""Shared fake-CDP support for the login browser-transaction tests.

The fake implements exactly the command surface ``login.py`` uses --
``Page.getFrameTree``, ``SystemInfo.getProcessInfo``,
``Page.createIsolatedWorld``, ``Runtime.evaluate`` and
``Runtime.callFunctionOn`` -- with scriptable outcomes and a shared event
log that also records the relay reads, so behavioral modules can prove the
*ordering* that is the whole point of the transaction:

    validate everything -> read credentials -> revalidate -> fill ->
    revalidate -> submit -> clear -> wait for success

Extracted from test_login_browser.py (repository module-size rule): the
behavioral classes import these fixtures and helpers explicitly, with
same-name aliases where the name is a pytest fixture.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from browser_tools import credential_relay, login_ownership
from browser_tools import login as login_module
from browser_tools.core import registry as core_registry
from browser_tools.core.errors import CDPError
from browser_tools.credential_relay import Secret

if TYPE_CHECKING:
    from browser_tools.lifecycle import ExtendedInstance

ORIGIN = "https://example.com"
VAULT = "dqss6l4yprxhntxymzjaltchwe"
ITEM = "bbbbbbbbbbbbbbbbbbbbbbbbbb"
SENTINEL_USER = "SENTINEL-user"
SENTINEL_PASS = "SENTINEL-pass"

OK_OUTCOME = {"ok": True}
OK_FILL = {"ok": True}
OK_SUBMIT = {"ok": True}
OK_CLEAR = {"ok": True, "cleared": True}
AUTHENTICATED_PROBE = {"result": {"value": {"authenticated": True, "originOk": True}}}
UNAUTHENTICATED_PROBE = {"result": {"value": {"authenticated": False, "originOk": True}}}


def policy_file(tmp_path, *, origin: str = ORIGIN, alias: str = "example") -> str:
    document = {
        "version": 1,
        "identity": "browser",
        "vault": VAULT,
        "accounts": {
            alias: {
                "item": ITEM,
                "origin": origin,
                "username_selector": "#username",
                "password_selector": "#password",
                "submit_selector": "button[type=submit]",
                "success_selector": "[data-signed-in]",
            }
        },
    }
    path = tmp_path / "logins.json"
    path.write_text(json.dumps(document))
    return str(path)


OWNER_PID = 2_000_000_000
OWNER_START = "START-TOKEN-1"
OWNER_DIR = "/recorded/session-dir"


def owner_snapshot() -> login_ownership.OwnershipSnapshot:
    """The explicitly verified owner every fake-CDP run stands on."""
    return login_ownership.OwnershipSnapshot(
        pid=OWNER_PID, pid_start=OWNER_START, user_data_dir=OWNER_DIR,
    )


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """An isolated registry holding one ownership-bearing Chrome instance.

    The row carries the evidence the real check requires (a recorded
    user-data directory and start-time identity); the ownership
    verification itself is mocked at the seam, because the recorded PID is
    deliberately nonexistent in unit tests.
    """
    path = str(tmp_path / "registry.json")
    monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", path)
    core_registry._save_registry(  # pyright: ignore[reportPrivateUsage]
        {"web-01": {
            "port": 9500,
            "pid": OWNER_PID,
            "browser_version": "Chrome/1",
            "user_data_dir": OWNER_DIR,
            "pid_start": OWNER_START,
            "engine": "chrome",
        }},
        path,
    )
    return path


class FakeCDP:
    """The login command surface, scriptable and logged."""

    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self.log = log
        self.loader_id = "L1"
        self.world_dead = False  # a replaced document: object ids are gone
        self.outcome: dict = OK_OUTCOME
        self.fill: dict | Exception = OK_FILL
        self.submit: dict | Exception = OK_SUBMIT
        self.clear: dict | Exception = OK_CLEAR
        self.probe: dict = AUTHENTICATED_PROBE
        self.process_info: dict | Exception = {
            "processInfo": [{"type": "browser", "id": OWNER_PID, "cpuTime": 0.5}]
        }
        self.raise_on: dict[str, Exception] = {}
        self.exception_details_on: set[str] = set()
        self._handlers: dict[str, list[Any]] = {}

    def on(self, event, callback, session_id=None) -> None:
        self._handlers.setdefault(event, []).append(callback)

    def off(self, event, callback) -> None:
        self._handlers[event] = [
            cb for cb in self._handlers.get(event, []) if cb is not callback
        ]

    def navigate(self) -> None:
        """Replace the document: new loader, dead world."""
        self.loader_id = "L2"
        self.world_dead = True

    def emit(self, event: str, payload: dict) -> None:
        """Deliver one event to registered handlers, as the client would."""
        for callback in list(self._handlers.get(event, [])):
            callback(payload)

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.log.append((method, params))
        if method in self.raise_on:
            raise self.raise_on[method]
        if method == "SystemInfo.getProcessInfo":
            if isinstance(self.process_info, Exception):
                raise self.process_info
            return self.process_info
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F1", "loaderId": self.loader_id}}}
        if method == "Page.enable":
            return {}
        if method == "Page.handleJavaScriptDialog":
            return {}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if method == "Runtime.evaluate":
            # The Python side evaluates the form helper's begin call, plus
            # one tail call, in a freshly created isolated world; the small
            # standalone success probe evaluates without the helper at all.
            # A replaced document never reaches this branch; only the bound
            # session object (callFunctionOn) dies with it.
            expression = params.get("expression", "")
            tail = expression.rsplit(";", 1)[-1].strip()
            if tail.startswith("__btLogin.begin"):
                if "begin" in self.exception_details_on:
                    return {
                        "result": {"type": "object", "objectId": "ERROR-OBJECT"},
                        "exceptionDetails": {"text": SENTINEL_USER, "exception": {}},
                    }
                return {"result": {"objectId": "SESSION1"}}
            # Everything else evaluated in a fresh world is the probe.
            if "probe" in self.exception_details_on:
                return {
                    "result": {"type": "object"},
                    "exceptionDetails": {"text": SENTINEL_USER, "exception": {}},
                }
            return self.probe
        if method == "Runtime.callFunctionOn":
            if self.world_dead:
                raise CDPError(-32000, "Could not find object with given id")
            declaration = params.get("functionDeclaration", "")
            if "this.outcome" in declaration:
                if "outcome" in self.exception_details_on:
                    return {
                        "result": {"type": "object"},
                        "exceptionDetails": {"text": SENTINEL_USER, "exception": {}},
                    }
                return {"result": {"value": self.outcome}}
            if "this.fill" in declaration:
                self.log.append(("fill-arguments", params.get("arguments")))
                self.log.append(("fill-declaration", declaration))
                if isinstance(self.fill, Exception):
                    raise self.fill
                return {"result": {"value": self.fill}}
            if "this.submit" in declaration:
                if isinstance(self.submit, Exception):
                    raise self.submit
                return {"result": {"value": self.submit}}
            if "this.clear" in declaration:
                if isinstance(self.clear, Exception):
                    raise self.clear
                return {"result": {"value": self.clear}}
        raise CDPError(-32000, f"unexpected method {method}")


@pytest.fixture
def harness(monkeypatch, tmp_path, registry):
    """A login run over the fake CDP, with the relay stubbed and logged."""
    log: list[tuple[str, Any]] = []
    fake = FakeCDP(log)
    relay_reads: list[str] = []

    import contextlib

    @contextlib.asynccontextmanager
    async def fake_session(port, target_spec, target_by):
        log.append(("open-session", (port, target_spec, target_by)))
        try:
            yield fake, "S1"
        finally:
            log.append(("close-session", None))

    monkeypatch.setattr(login_module, "one_shot_page_session", fake_session)
    monkeypatch.setattr(credential_relay, "find_opchain", lambda: "/fake/bin/opchain")
    monkeypatch.setattr(credential_relay, "ensure_relay_supported", lambda *a, **k: None)
    # Ownership is verified at the seam in unit runs: the recorded PID is
    # deliberately nonexistent, and the fake CDP answers the connected
    # browser identity for it. login_ownership's own module tests and the
    # live module exercise the real checks.
    seen_entries: list[ExtendedInstance] = []

    def verified_owner(entry: ExtendedInstance) -> login_ownership.OwnershipSnapshot:
        seen_entries.append(entry)
        return owner_snapshot()

    monkeypatch.setattr(login_ownership, "verify_recorded_owner", verified_owner)
    monkeypatch.setattr(login_ownership, "_identity_holds", lambda snapshot: None)

    def fake_read(*, field, **kwargs):
        relay_reads.append(field)
        log.append(("relay-read", field))
        value = SENTINEL_USER if field == "username" else SENTINEL_PASS
        return Secret(value.encode("utf-8"))

    monkeypatch.setattr(credential_relay, "read_reference", fake_read)

    config = policy_file(tmp_path)

    def run(**overrides):
        kwargs = {
            "instance": "web-01",
            "account": "example",
            "config": config,
            "timeout_ms": 2_000,
            "registry_path": registry,
        }
        kwargs.update(overrides)
        return login_module.login(**kwargs)

    import types

    return types.SimpleNamespace(
        fake=fake, log=log, relay_reads=relay_reads, run=run, config=config,
        seen_entries=seen_entries,
    )
