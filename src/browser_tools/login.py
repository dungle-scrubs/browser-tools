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

from . import credential_relay, curated, lifecycle, login_policy
from .core.attach import AmbiguousTargetError, TargetNotFoundError
from .core.errors import CDPError, NoPageError
from .lifecycle import LifecycleError
from .login_guards import DialogGuard, quiet_websocket_logs
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
_relay_tasks: set[asyncio.Task[str]] = set()

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
    "the form submission was requested but the configured success condition was not met "
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
_PAGE_CHANGED_DURING_FILL = (
    "the page changed while the form was being filled; whether the values "
    "were inserted could not be confirmed, but nothing was sent to a "
    "replacement document and the form was not submitted"
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
    "form-invalid": "the form's validation rules prevented submission",
}

logger = logging.getLogger(__name__)


class LoginError(LifecycleError):
    """One fixed-diagnostic login failure (CLI exit 1)."""

    def __init__(self, message: str, *, fields_may_remain: bool = False) -> None:
        super().__init__(message)
        self.fields_may_remain = fields_may_remain


def validate_timeout_ms(timeout_ms: object) -> None:
    """Refuse a timeout that is not a usable positive integer (exit 2).

    One check, shared by the CLI preconditions and the direct entry point
    so the two cannot drift. An ``int`` too large for its seconds
    conversion to represent is refused here rather than crashing
    ``math.isfinite``, which would convert it to float first and raise
    OverflowError; no arbitrary maximum is imposed on ordinary values.
    """
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
        raise login_policy.LoginPolicyError(
            "login --timeout-ms must be a finite positive integer"
        )
    try:
        seconds = timeout_ms / 1000.0
    except OverflowError:
        raise login_policy.LoginPolicyError(
            "login --timeout-ms is too large to represent"
        ) from None
    if not math.isfinite(seconds):
        raise login_policy.LoginPolicyError(
            "login --timeout-ms is too large to represent"
        ) from None


def _acquisition_message(exc: BaseException) -> str:
    """A fixed diagnostic for one session-entry failure.

    The underlying errors carry every tab's URL and title, or arbitrary
    protocol text - page-provided data the output channels never see. The
    generic :func:`cli_cdp_errors` mapping is deliberately not used: it
    forwards ``str(exc)`` verbatim.
    """
    if isinstance(exc, AmbiguousTargetError):
        return "several tabs are open; choose one with --target"
    if isinstance(exc, TargetNotFoundError):
        return "the --target value matches no open tab; no page was selected"
    if isinstance(exc, NoPageError):
        return "the browser has no open page; open the login page and retry"
    if isinstance(exc, ConnectionError):
        return "could not connect to the browser; check that the instance is still running"
    if isinstance(exc, CDPError):
        return "the browser session could not be opened"
    return "the browser session failed during login"


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
        result = await cdp.send(method=method, params=params, session_id=session_id, timeout=timeout)
    except Exception:
        logger.debug("login CDP send failed at %s", phase)
        raise LoginError(_SESSION_FAILED.format(phase=phase)) from None
    if not isinstance(result, dict):
        raise LoginError(_SESSION_FAILED.format(phase=phase))
    return result


def _outcome_of(result: object, phase: str) -> dict[str, Any]:
    """Parse a by-value Runtime result without exposing protocol contents."""
    if not isinstance(result, dict) or result.get("exceptionDetails"):
        raise LoginError(_SESSION_FAILED.format(phase=phase))
    remote = result.get("result")
    if not isinstance(remote, dict) or not isinstance(remote.get("value"), dict):
        raise LoginError(_SESSION_FAILED.format(phase=phase))
    return remote["value"]


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
    frame_tree = tree.get("frameTree")
    frame = frame_tree.get("frame") if isinstance(frame_tree, dict) else None
    if not isinstance(frame, dict):
        raise LoginError(_SESSION_FAILED.format(phase="frame lookup"))
    frame_id = frame.get("id")
    loader_id = frame.get("loaderId")
    if (
        not isinstance(frame_id, str) or not frame_id
        or not isinstance(loader_id, str) or not loader_id
    ):
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

    An owned task runs each blocking read. asyncio.wait leaves that task
    running when its caller is cancelled; a threading.Event asks the
    provider to kill and reap its process group. Cleanup waits are bounded,
    tolerate a second cancellation, and discard every late outcome.
    """
    cancel = threading.Event()

    def discard_outcome(task: asyncio.Task[str]) -> None:
        _relay_tasks.discard(task)
        if not task.cancelled():
            task.exception()

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
        task = asyncio.create_task(asyncio.to_thread(read, field))
        _relay_tasks.add(task)
        task.add_done_callback(discard_outcome)
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancel.set()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({task}, timeout=_RELAY_CANCEL_SETTLE_SECONDS)
            raise
        return task.result()

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
    has no per-command open bound of its own). The body runs inside one
    wall that fires just past the deadline; session teardown then runs in
    its own bounded grace, outside that wall, so a stalled detach cannot
    replace the body's diagnosis: a fields-may-remain warning outranks a
    timeout. The inner scope cancels at the deadline only after the
    cooperative phase checks have already reported their precise fixed
    messages.
    """
    if deadline.remaining() <= 0:
        raise LoginError(_TIMED_OUT)
    cm = one_shot_page_session(port, target_spec, target_by)
    entered = False
    body_failure: LoginError | None = None
    propagating: BaseException | None = None
    try:
        try:
            async with asyncio.timeout(deadline.remaining() + _WALL_CUSHION_SECONDS):
                cdp, session_id = await cm.__aenter__()  # pyright: ignore[reportAbstractUsage]
                entered = True
                await _transaction_on_session(
                    cdp, session_id, policy, account, binary, deadline
                )
        except LoginError as failure:
            body_failure = failure
        except TimeoutError:
            body_failure = LoginError(_TIMED_OUT)
        except BaseException as exc:
            propagating = exc
            raise
    finally:
        # Teardown is bounded by the cleanup grace. An exception raised here
        # would replace one already propagating, so teardown failures are
        # contained: with a body failure or an in-flight exception they are
        # dropped; with nothing else to report, a stalled teardown is
        # itself the timeout it caused.
        if entered:
            try:
                async with asyncio.timeout(_CLEANUP_GRACE_SECONDS):
                    await cm.__aexit__(None, None, None)  # pyright: ignore[reportAbstractUsage]
            except BaseException as failure:
                if propagating is not None:
                    pass
                elif isinstance(failure, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    if body_failure is None or not body_failure.fields_may_remain:
                        raise
                elif body_failure is None:
                    body_failure = LoginError(
                        _TIMED_OUT if isinstance(failure, TimeoutError)
                        else _SESSION_FAILED.format(phase="session cleanup")
                    )
    if body_failure is not None:
        raise body_failure


async def _transaction_on_session(cdp: Any, session_id: str, policy: login_policy.LoginPolicy,
                                  account: login_policy.AccountSpec, binary: str,
                                  deadline: _Deadline) -> None:
    frame = await _read_frame(cdp, session_id, deadline)
    dialogs = DialogGuard(cdp, session_id)
    dialogs.start()
    body_failure: LoginError | None = None
    try:
        await _send(cdp, session_id, "Page.enable", {}, "dialog handling", deadline)
        await _transaction_body(cdp, session_id, frame, policy, account, binary, deadline)
    except LoginError as failure:
        body_failure = failure
        raise
    finally:
        try:
            await dialogs.close()
        except (Exception, asyncio.CancelledError):
            if body_failure is None or not body_failure.fields_may_remain:
                raise


async def _transaction_body(cdp: Any, session_id: str, frame: _Frame,
                            policy: login_policy.LoginPolicy,
                            account: login_policy.AccountSpec, binary: str,
                            deadline: _Deadline) -> None:
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
            raise LoginError(_PAGE_CHANGED_DURING_FILL) from None
        if cleared:
            if isinstance(failure, asyncio.CancelledError):
                raise
            raise LoginError(_FILL_UNCERTAIN_CLEARED) from None
        raise LoginError(_FIELDS_MAY_REMAIN, fields_may_remain=True) from None
    if fill.get("ok") is not True:
        message = _reason_message(str(fill.get("reason", "")))
        if fill.get("insertionStarted") is False:
            raise LoginError(message)
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

    # Framework state can restore fields after either a successful response
    # or a failed one. Cleanup is owed on every exit, not only authentication.
    try:
        await _wait_for_success(cdp, session_id, frame, account, deadline)
    finally:
        cleared = await _try_clear(cdp, session_id, session_object, frame)
        if cleared is False:
            raise LoginError(_FIELDS_MAY_REMAIN, fields_may_remain=True)


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


def _probe_expression(account: login_policy.AccountSpec) -> str:
    """The tiny success probe: presence semantics, origin first.

    A standalone expression, not the whole form helper: the poll runs often,
    and only the configured origin and success selector are needed. Both
    arrive from the validated policy, so no credential and no page text is
    ever part of evaluated source.
    """
    return (
        "(function(){try{"
        "if(document.location.origin!=" + json.dumps(account.origin) + ")"
        "return{authenticated:false,originOk:false};"
        "return{authenticated:document.querySelector("
        + json.dumps(account.success_selector) + ")!==null,originOk:true};"
        "}catch(e){return{authenticated:false,originOk:false};}})()"
    )


async def _wait_for_success(cdp: Any, session_id: str, frame: _Frame,
                            account: login_policy.AccountSpec,
                            deadline: _Deadline) -> None:
    """Poll the configured success selector at the configured origin.

    Each poll creates one isolated world and evaluates the small probe in
    it. The POST response can commit between those two calls, destroying
    the fresh context; that is a transient not-yet (a connection loss is
    not - it exits fixed immediately). The deadline still governs: a
    probe that never lands reports the unmet success condition.
    """
    expression = _probe_expression(account)
    while True:
        remaining = deadline.remaining()
        if remaining <= 0:
            raise LoginError(_SUCCESS_UNMET)
        timeout = deadline.command_timeout("success check")
        try:
            world = await cdp.send(
                method="Page.createIsolatedWorld",
                params={
                    "frameId": frame.frame_id,
                    "worldName": _LOGIN_WORLD,
                    "grantUniveralAccess": False,
                },
                session_id=session_id, timeout=timeout,
            )
            context_id = world.get("executionContextId")
            if not isinstance(context_id, int):
                raise LoginError(_SESSION_FAILED.format(phase="success check"))
            result = await cdp.send(
                method="Runtime.evaluate",
                params={
                    "expression": expression,
                    "contextId": context_id,
                    "returnByValue": True,
                    "awaitPromise": False,
                },
                session_id=session_id, timeout=timeout,
            )
        except CDPError as error:
            if error.code != -32000 or not error.message.startswith((
                "Execution context was destroyed", "Cannot find context with specified id",
            )):
                raise LoginError(_SESSION_FAILED.format(phase="success check")) from None
            logger.debug("login success probe lost its context (continuing)")
        except TimeoutError:
            logger.debug("login success probe timed out (continuing)")
        except Exception:
            raise LoginError(_SESSION_FAILED.format(phase="success check")) from None
        else:
            value = _outcome_of(result, "success check")
            if value.get("authenticated") is True and value.get("originOk") is True:
                return
        await asyncio.sleep(min(SUCCESS_POLL_SECONDS, max(deadline.remaining(), 0.0)))


def login(*, instance: str | None, account: str, target: str | None = None,
          config: str | None = None, timeout_ms: int = DEFAULT_LOGIN_TIMEOUT_MS,
          registry_path: str | None = None) -> dict[str, Any]:
    """Run one configured login. Returns the success document on exit 0.

    Raises:
        login_policy.LoginPolicyError: Policy or invocation shape (exit 2).
        LifecycleError: Every operational failure, with a fixed diagnostic
            (exit 1), including :class:`credential_relay.RelayError` and
            every failure to open the page session (tab choice, target,
            connection): their own messages carry browser text and are
            replaced here, never forwarded.
    """
    validate_timeout_ms(timeout_ms)

    # One overall deadline for the whole operation: capability probe, session
    # acquisition, credential reads, and the browser transaction all draw
    # from it, and it is never reset along the way.
    deadline = _Deadline(timeout_ms)
    policy, account_spec = login_policy.load_account(config, account)

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
    binary = _ensure_relay_ready(policy, deadline)

    # One derivation of the --target grammar, shared with every other verb.
    target_spec, target_by = curated.target_selector(target, None)

    try:
        with quiet_websocket_logs():
            asyncio.run(
                _login_transaction(port, target_spec, target_by, policy, account_spec, binary, deadline)
            )
    except LifecycleError:
        raise
    except Exception as exc:
        # Transport libraries can include tab URLs, titles and protocol
        # contents in errors; only fixed diagnostics cross this boundary.
        raise LoginError(_acquisition_message(exc)) from None
    return {"account": account_spec.alias, "status": "authenticated"}


__all__ = ["DEFAULT_LOGIN_TIMEOUT_MS", "LoginError", "login", "validate_timeout_ms"]
