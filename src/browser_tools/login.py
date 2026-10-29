"""``bt login``: one configured login form, filled and submitted once.

The operation, end to end:

1. Load the operator's policy (:mod:`browser_tools.login_policy`) and refuse
   anything malformed, before the browser or 1Password is touched.
2. Resolve the instance and refuse non-Chrome engines.
3. Verify the installed opchain supports the private relay
   (:mod:`browser_tools.credential_relay`) -- grammar only, no credential.
4. Over one standalone one-shot CDP session (the existing
   :func:`browser_tools.one_shot.one_shot_page_session` seam), validate the
   whole form in an isolated world bound to that exact document: exact
   configured HTTPS origin, visible tab, exactly one usable username input
   and one password input in one connected form, exactly one usable submit
   button in the same form, a POST same-origin destination including
   ``formaction``/``formmethod`` overrides, and a success selector that does
   not already match. Only then are credentials read, through the relay.
5. Fill both fields with native value setters (values arrive as CDP call
   arguments, never as script source), revalidate after the input events,
   submit with the form's own ``requestSubmit`` on the validated button, and
   clear the fields synchronously.
6. Wait within the same overall deadline for the configured success selector
   at the configured origin. Submission alone never means authenticated.

Every diagnostic this module raises is a fixed sentence. Browser- and
page-provided text (JS exception details, CDP messages, page markup) is
treated as untrusted data and never printed; websockets' frame logging is
forced above DEBUG for the duration so no debug logger can echo a
value-bearing CDP message. The direct CDP socket and same-user processes
remain outside this guarantee: captures and debug clients must not run
concurrently with a login.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import credential_relay, lifecycle, login_policy
from .core.errors import CDPError
from .lifecycle import LifecycleError
from .one_shot import one_shot_page_session

if TYPE_CHECKING:
    from .endpoint import ResolvedEndpoint

#: Overall deadline default, shared with the CLI flag.
DEFAULT_LOGIN_TIMEOUT_MS = 30_000

#: How often the success condition is polled after submission.
SUCCESS_POLL_SECONDS = 0.25

#: Per-CDP-command bound cap so one stalled command cannot eat the whole
#: deadline before the phase that needs it.
PER_COMMAND_TIMEOUT_SECONDS = 10.0

#: Grace past the overall deadline that cancellation cleanup (relay group
#: teardown, field clearing, session close) gets before the hard wall.
_CLEANUP_GRACE_SECONDS = 8.0

#: The inner wall fires this far after the cooperative deadline so the
#: precise phase checks ("timed out during ...", "success condition was not
#: met") always report first; the wall only catches phases that ignore the
#: deadline, such as a hung session attach.
_WALL_CUSHION_SECONDS = 0.05

#: Bound on waiting for a cancelled relay read's worker thread to settle
#: after its cancellation signal is set; smaller than the cleanup grace.
_RELAY_CANCEL_SETTLE_SECONDS = 6.0

#: The isolated world name. Distinct from every other world bt uses so its
#: execution contexts are identifiable in a debug dump.
_LOGIN_WORLD = "browser-tools-login"

_LOGIN_SCRIPT_PATH = Path(__file__).parent / "login_form.js"
_login_script: str | None = None

#: Fixed diagnostics. None of these interpolate browser or provider text.
_ENGINE_MESSAGE = (
    "login works only with Chrome instances. This instance runs a different "
    "engine, which the login transaction does not support"
)
_TIMED_OUT = "the login did not complete within its timeout"
_FIELDS_MAY_REMAIN = (
    "the login did not complete and sensitive form fields may still hold "
    "values; check the tab before reusing it. The form was not resubmitted"
)
_SESSION_FAILED = "the browser session failed during login ({phase})"
_SUCCESS_UNMET = (
    "the form was submitted but the configured success condition was not met "
    "within the timeout"
)
_PAGE_CHANGED_BEFORE_SUBMIT = (
    "the page changed while submission was in progress; whether it completed "
    "could not be confirmed. The form was not resubmitted"
)
_PAGE_CHANGED_BEFORE_FILL = (
    "the page changed after validation and before any value was inserted; "
    "nothing was sent to a replacement document"
)
_FILL_UNCERTAIN_CLEARED = (
    "the fill could not be confirmed; the configured fields were cleared and "
    "the form was not submitted"
)
_SUBMIT_UNCERTAIN_CLEARED = (
    "the form submission could not be confirmed; the configured fields were "
    "cleared and the form was not resubmitted"
)

#: Fixed English for the JS helper's fixed reason vocabulary.
_REASONS: dict[str, str] = {
    "wrong-origin": "the selected page is not at the configured origin",
    "hidden-tab": (
        "the selected page is a hidden tab; input to a hidden tab is dropped, "
        "so bring the tab to the front or select a visible one with --target"
    ),
    "no-document": "the selected page has no usable document",
    "bad-selector": "a configured selector is not a valid selector for this page",
    "username-not-unique": "the configured username selector does not match exactly one input",
    "password-not-unique": "the configured password selector does not match exactly one input",
    "submit-not-unique": "the configured submit selector does not match exactly one element",
    "username-wrong-type": "the username field is not an ordinary text or email input",
    "password-wrong-type": "the password field is not a password input",
    "field-disabled": "one of the configured fields is disabled, read-only, or detached",
    "fields-not-distinct": "the username and password selectors resolve to the same element",
    "no-common-form": "the username and password fields are not in the same connected form",
    "form-detached": "the login form is not connected to the main document",
    "submit-wrong-form": "the submit button does not belong to the login form",
    "submit-wrong-type": "the submit element is not a submit button",
    "not-post": "the form (or its submit button override) does not submit by POST",
    "action-not-same-origin": (
        "the form (or its submit button override) submits somewhere other than "
        "the configured origin"
    ),
    "submit-unsupported": "this browser form does not support requestSubmit",
    "already-authenticated": (
        "the configured success condition already holds on this page, so "
        "nothing was filled or submitted"
    ),
    "document-changed": (
        "the page or its form changed during the login and no value was "
        "inserted into a replacement document"
    ),
    "fill-failed": "the configured fields could not be filled",
    "submit-failed": "the form could not be submitted",
}

logger = logging.getLogger(__name__)


class LoginError(LifecycleError):
    """One fixed-diagnostic login failure (CLI exit 1)."""

    def __init__(self, message: str, *, fields_may_remain: bool = False) -> None:
        super().__init__(message)
        self.fields_may_remain = fields_may_remain


@contextlib.contextmanager
def _quiet_websocket_logs():
    """Keep websockets' frame logging from echoing value-bearing CDP messages.

    websockets' sans-io protocol layer logs every frame at DEBUG (``"> %s"``),
    and ``Runtime.callFunctionOn`` frames carry credential values as JSON
    arguments. A process configured for DEBUG logging must not be able to
    echo them mid-login, so every logger under ``websockets`` is forced above
    DEBUG for the transaction and restored after.
    """
    root = logging.getLogger("websockets")
    saved: list[tuple[logging.Logger, int]] = [(root, root.level)]
    root.setLevel(logging.WARNING)
    for name, obj in list(logging.root.manager.loggerDict.items()):
        if name == "websockets" or not name.startswith("websockets."):
            continue
        if isinstance(obj, logging.Logger):
            saved.append((obj, obj.level))
            obj.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for saved_logger, level in saved:
            saved_logger.setLevel(level)


class _Deadline:
    """The one overall deadline every phase draws from."""

    def __init__(self, timeout_ms: int) -> None:
        self._ends_at = time.monotonic() + timeout_ms / 1000.0

    def remaining(self) -> float:
        return self._ends_at - time.monotonic()

    def command_timeout(self, phase: str) -> float:
        remaining = self.remaining()
        if remaining <= 0:
            raise LoginError(f"the login timed out during {phase}")
        return min(PER_COMMAND_TIMEOUT_SECONDS, remaining)


def _reason_message(reason: str) -> str:
    """Fixed English for one JS reason code; unknown codes stay fixed too."""
    if reason in _REASONS:
        return _REASONS[reason]
    return "the login form did not match the configured policy"


def _policy_json_for_page(account: login_policy.AccountSpec) -> str:
    """The exact JSON handed to the JS helper: selectors and origin only."""
    return json.dumps(
        {
            "origin": account.origin,
            "username_selector": account.username_selector,
            "password_selector": account.password_selector,
            "submit_selector": account.submit_selector,
            "success_selector": account.success_selector,
        }
    )


def _login_script_source() -> str:
    global _login_script
    if _login_script is None:
        _login_script = _LOGIN_SCRIPT_PATH.read_text(encoding="utf-8")
    return _login_script


async def _send(cdp: Any, session_id: str, method: str, params: dict[str, Any],
                phase: str, deadline: _Deadline) -> Any:
    """One bounded CDP send whose failures become fixed diagnostics."""
    timeout = deadline.command_timeout(phase)
    try:
        return await cdp.send(method=method, params=params, session_id=session_id, timeout=timeout)
    except (CDPError, ConnectionError, TimeoutError):
        logger.debug("login CDP send failed at %s", phase)
        raise LoginError(_SESSION_FAILED.format(phase=phase)) from None


def _outcome_of(result: dict[str, Any], phase: str) -> dict[str, Any]:
    """Read a by-value Runtime result, refusing untrusted exception text."""
    if result.get("exceptionDetails"):
        logger.debug("login JS exception at %s (details suppressed)", phase)
        raise LoginError(_SESSION_FAILED.format(phase=phase))
    value = result.get("result", {}).get("value")
    if not isinstance(value, dict):
        raise LoginError(_SESSION_FAILED.format(phase=phase))
    return value


async def _eval_login_script(cdp: Any, session_id: str, frame_id: str, expression_tail: str,
                             *, return_by_value: bool, phase: str,
                             deadline: _Deadline) -> dict[str, Any]:
    """Evaluate the login helper (plus one tail call) in a fresh isolated world.

    ``Page.createIsolatedWorld`` in the current document means the expression
    always runs against whatever document the frame now holds; worlds of a
    replaced document are gone, which is what binds the session object to
    the validated document.
    """
    world = await _send(
        cdp, session_id, "Page.createIsolatedWorld",
        {"frameId": frame_id, "worldName": _LOGIN_WORLD, "grantUniveralAccess": False},
        phase, deadline,
    )
    context_id = world.get("executionContextId")
    if not isinstance(context_id, int):
        raise LoginError(_SESSION_FAILED.format(phase=phase))
    result = await _send(
        cdp, session_id, "Runtime.evaluate",
        {
            "expression": _login_script_source() + "\n;" + expression_tail,
            "contextId": context_id,
            "returnByValue": return_by_value,
            "awaitPromise": False,
        },
        phase, deadline,
    )
    return result


async def _call_on_session(cdp: Any, session_id: str, object_id: str,
                           function_declaration: str, phase: str, deadline: _Deadline,
                           arguments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Call one method on the bound session object, by value.

    ``arguments`` carry values as call arguments; the function declaration is
    a constant string, so no value is ever part of evaluated script source.
    """
    params: dict[str, Any] = {
        "functionDeclaration": function_declaration,
        "objectId": object_id,
        "returnByValue": True,
        "awaitPromise": False,
    }
    if arguments is not None:
        params["arguments"] = arguments
    result = await _send(cdp, session_id, "Runtime.callFunctionOn", params, phase, deadline)
    return _outcome_of(result, phase)


class _Frame:
    """The main frame's id and its loader id: the document identity."""

    def __init__(self, frame_id: str, loader_id: str) -> None:
        self.frame_id = frame_id
        self.loader_id = loader_id


async def _read_frame(cdp: Any, session_id: str, deadline: _Deadline) -> _Frame:
    tree = await _send(cdp, session_id, "Page.getFrameTree", {}, "frame lookup", deadline)
    frame = ((tree or {}).get("frameTree") or {}).get("frame") or {}
    frame_id = frame.get("id")
    loader_id = frame.get("loaderId", "")
    if not isinstance(frame_id, str) or not frame_id:
        raise LoginError(_SESSION_FAILED.format(phase="frame lookup"))
    return _Frame(frame_id, loader_id)


async def _loader_changed(cdp: Any, session_id: str, frame: _Frame,
                          deadline: _Deadline) -> bool:
    """Whether a new document has loaded in the frame since validation.

    ``loaderId`` is a trusted browser-protocol fact (not page text): it
    changes on every document load and survives as the one same-frame
    identity check that works across navigations.
    """
    current = await _read_frame(cdp, session_id, deadline)
    return current.loader_id != frame.loader_id


def _ensure_relay_ready(policy: login_policy.LoginPolicy, deadline: _Deadline) -> str:
    """The opchain binary path, after the no-credential support probe.

    The probe draws from the same overall deadline as everything else,
    so the documented timeout covers it too.
    """
    binary = credential_relay.find_opchain()
    if binary is None:
        raise credential_relay.RelayMissingError(
            "opchain is not installed or not on PATH. bt login reads credentials "
            "only through opchain's --value-fd relay; install opchain and retry"
        )
    remaining = deadline.remaining()
    if remaining <= 0:
        raise LoginError(_TIMED_OUT)
    credential_relay.ensure_relay_supported(
        binary,
        policy.identity,
        timeout=min(credential_relay.PROBE_TIMEOUT_SECONDS, remaining),
    )
    return binary


async def _read_credentials(binary: str, policy: login_policy.LoginPolicy,
                            account: login_policy.AccountSpec,
                            deadline: _Deadline) -> tuple[str, str]:
    """Both fields through the private relay, inside the remaining budget.

    Each blocking read runs in a worker thread this coroutine owns. The
    ``asyncio.to_thread`` task is shielded, so cancelling this coroutine
    cannot detach its handle; the cancellation crosses into the thread as a
    :class:`threading.Event` that makes the provider kill and reap the
    opchain group for real. Before letting ``CancelledError`` propagate,
    the coroutine waits - bounded, and tolerant of a second cancellation -
    for that cleanup to finish, and a value that arrived despite the
    cancellation is never used.
    """
    cancel = threading.Event()

    def read(field: str) -> str:
        remaining = deadline.remaining()
        if remaining <= 0:
            raise LoginError("the login timed out waiting for the credential relay")
        return credential_relay.read_reference(
            binary=binary,
            identity=policy.identity,
            vault=policy.vault,
            item=account.item,
            field=field,
            command_timeout=min(credential_relay.DEFAULT_COMMAND_TIMEOUT_SECONDS, remaining),
            read_timeout=min(credential_relay.DEFAULT_READ_TIMEOUT_SECONDS, remaining),
            cancel=cancel,
        ).text()

    async def read_field(field: str) -> str:
        task = asyncio.ensure_future(asyncio.to_thread(read, field))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel.set()
            # The worker thread finishes the group kill, reap, and reader
            # teardown on its own bounds; a second cancellation cannot detach
            # this wait, and the thread's outcome - value or error - is
            # discarded, never used, because CancelledError is re-raised.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(task), _RELAY_CANCEL_SETTLE_SECONDS)
            raise

    username = await read_field(login_policy.USERNAME_FIELD)
    password = await read_field(login_policy.PASSWORD_FIELD)
    return username, password


async def _login_transaction(port: int | ResolvedEndpoint, target_spec: str | None, target_by: str | None,
                             policy: login_policy.LoginPolicy,
                             account: login_policy.AccountSpec,
                             binary: str, deadline: _Deadline) -> None:
    """The whole browser-side transaction, on one one-shot session.

    The deadline was created before the capability probe and is never reset,
    so the documented timeout covers session attach too (a registry session
    has no per-command open bound of its own). The inner scope cancels at
    the deadline, after the cooperative phase checks have already reported
    their precise fixed messages; the grace between the two scopes is the
    bounded budget cancellation cleanup gets before the hard wall.
    """
    if deadline.remaining() <= 0:
        raise LoginError(_TIMED_OUT)
    try:
        async with asyncio.timeout(deadline.remaining() + _CLEANUP_GRACE_SECONDS):
            async with asyncio.timeout(deadline.remaining() + _WALL_CUSHION_SECONDS):
                async with one_shot_page_session(port, target_spec, target_by) as (cdp, session_id):
                    with _quiet_websocket_logs():
                        await _transaction_on_session(cdp, session_id, policy, account, binary, deadline)
    except TimeoutError:
        raise LoginError(_TIMED_OUT) from None


async def _transaction_on_session(cdp: Any, session_id: str, policy: login_policy.LoginPolicy,
                                  account: login_policy.AccountSpec, binary: str,
                                  deadline: _Deadline) -> None:
    frame = await _read_frame(cdp, session_id, deadline)
    policy_json = _policy_json_for_page(account)

    # -- Validation, in the document, before any credential is read. -------
    begin = await _eval_login_script(
        cdp, session_id, frame.frame_id, f"__btLogin.begin({policy_json})",
        return_by_value=False, phase="form validation", deadline=deadline,
    )
    session_object = (begin.get("result") or {}).get("objectId")
    if not isinstance(session_object, str) or not session_object:
        raise LoginError(_SESSION_FAILED.format(phase="form validation"))
    outcome = await _call_on_session(
        cdp, session_id, session_object,
        "function () { return this.outcome; }", "form validation", deadline,
    )
    if outcome.get("ok") is not True:
        raise LoginError(_reason_message(str(outcome.get("reason", ""))))

    # -- Credential reads, between validation and any insertion. -----------
    username, password = await _read_credentials(binary, policy, account, deadline)

    # -- Revalidation in the bound world, then insertion. -------------------
    if await _loader_changed(cdp, session_id, frame, deadline):
        raise LoginError(_PAGE_CHANGED_BEFORE_FILL)
    try:
        fill = await _call_on_session(
            cdp, session_id, session_object,
            "function (u, p) { return this.fill(u, p); }", "form fill", deadline,
            arguments=[{"value": username}, {"value": password}],
        )
    except (LoginError, asyncio.CancelledError) as failure:
        cleared = await _try_clear(cdp, session_id, session_object, frame)
        if cleared is None:
            raise LoginError(_PAGE_CHANGED_BEFORE_FILL) from None
        if cleared:
            if isinstance(failure, asyncio.CancelledError):
                raise
            raise LoginError(_FILL_UNCERTAIN_CLEARED) from None
        raise LoginError(_FIELDS_MAY_REMAIN, fields_may_remain=True) from None
    if fill.get("ok") is not True:
        message = _reason_message(str(fill.get("reason", "")))
        if fill.get("cleared") is not True:
            raise LoginError(message + ". " + _FIELDS_MAY_REMAIN, fields_may_remain=True)
        raise LoginError(message)

    # -- Submission on the validated form, then a synchronous clear. -------
    try:
        submitted = await _call_on_session(
            cdp, session_id, session_object,
            "function () { return this.submit(); }", "form submission", deadline,
        )
    except (LoginError, asyncio.CancelledError) as failure:
        cleared = await _try_clear(cdp, session_id, session_object, frame)
        if cleared is None:
            raise LoginError(_PAGE_CHANGED_BEFORE_SUBMIT) from None
        if cleared:
            if isinstance(failure, asyncio.CancelledError):
                raise
            raise LoginError(_SUBMIT_UNCERTAIN_CLEARED) from None
        raise LoginError(_FIELDS_MAY_REMAIN, fields_may_remain=True) from None
    if submitted.get("ok") is not True:
        message = _reason_message(str(submitted.get("reason", "")))
        cleared = await _try_clear(cdp, session_id, session_object, frame)
        if cleared is False:
            raise LoginError(message + ". " + _FIELDS_MAY_REMAIN, fields_may_remain=True)
        raise LoginError(message)

    cleared = await _try_clear(cdp, session_id, session_object, frame)
    if cleared is False:
        raise LoginError(_FIELDS_MAY_REMAIN, fields_may_remain=True)

    # -- Wait for the configured success condition, same origin. -----------
    await _wait_for_success(cdp, session_id, frame, policy_json, deadline)


async def _try_clear(cdp: Any, session_id: str, session_object: str, frame: _Frame) -> bool | None:
    """Clear the bound fields. True=cleared, False=uncertain, None=replaced.

    A document replacement (the bound world died with it) is a clean clear:
    the values went with the document. Only an uncertain outcome on a
    still-live document means the fields may remain.
    """
    # Cleanup is still owed after the operation deadline. Its independent
    # budget stays inside the transaction's outer cleanup grace.
    cleanup_deadline = _Deadline(2000)
    try:
        result = await _call_on_session(
            cdp, session_id, session_object,
            "function () { return this.clear(); }", "field cleanup", cleanup_deadline,
        )
    except (LoginError, asyncio.CancelledError):
        try:
            if await _loader_changed(cdp, session_id, frame, cleanup_deadline):
                return None
        except (LoginError, asyncio.CancelledError):
            pass
        return False
    return result.get("cleared") is True


async def _wait_for_success(cdp: Any, session_id: str, frame: _Frame,
                            policy_json: str, deadline: _Deadline) -> None:
    """Poll the configured success selector at the configured origin."""
    while True:
        remaining = deadline.remaining()
        if remaining <= 0:
            raise LoginError(_SUCCESS_UNMET)
        probe = await _eval_login_script(
            cdp, session_id, frame.frame_id, f"__btLogin.probe({policy_json})",
            return_by_value=True, phase="success check", deadline=deadline,
        )
        value = _outcome_of(probe, "success check")
        if value.get("authenticated") is True and value.get("originOk") is True:
            return
        await asyncio.sleep(min(SUCCESS_POLL_SECONDS, max(remaining, 0.0)))


def login(*, instance: str | None, account: str, target: str | None = None,
          config: str | None = None, timeout_ms: int = DEFAULT_LOGIN_TIMEOUT_MS,
          registry_path: str | None = None) -> dict[str, Any]:
    """Run one configured login. Returns the success document on exit 0.

    Raises:
        login_policy.LoginPolicyError: Policy or invocation shape (exit 2).
        LifecycleError: Every operational failure, with a fixed diagnostic
            (exit 1), including :class:`credential_relay.RelayError`.
    """
    if not math.isfinite(timeout_ms) or timeout_ms <= 0:
        raise login_policy.LoginPolicyError("login --timeout-ms must be a finite positive integer")

    # One overall deadline for the whole operation: capability probe, session
    # acquisition, credential reads, and the browser transaction all draw
    # from it, and it is never reset along the way.
    deadline = _Deadline(timeout_ms)
    policy, account_spec = login_policy.load_account(config, account)
    binary = _ensure_relay_ready(policy, deadline)

    name = instance if instance is not None else lifecycle.resolve_single_instance(registry_path)
    entry = next(
        (item for item in lifecycle.read_instances(registry_path=registry_path)
         if item.name == name),
        None,
    )
    if entry is None:
        raise LifecycleError(f"no browser instance named '{name}' is registered")
    if entry.engine != lifecycle.DEFAULT_ENGINE:
        raise LifecycleError(_ENGINE_MESSAGE)
    port = lifecycle.resolve_cdp_port(name, registry_path=registry_path)

    target_by = ("index" if target.isdigit() else "id") if target is not None else None

    asyncio.run(
        _login_transaction(port, target, target_by, policy, account_spec, binary, deadline)
    )
    return {"account": account_spec.alias, "status": "authenticated"}


__all__ = ["DEFAULT_LOGIN_TIMEOUT_MS", "LoginError", "login"]
