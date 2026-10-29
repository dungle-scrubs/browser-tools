"""``bt login``: one configured login form, filled and submitted once.

One standalone CDP session over the existing :func:`one_shot_page_session`
seam: positively own the registered browser
(:mod:`browser_tools.login_ownership`), validate the whole form in an
isolated world bound to that exact document, read the two credentials
through the private relay (:mod:`browser_tools.credential_relay`), fill,
submit once, clear, and wait for the configured success condition - all
under one overall deadline. The full walkthrough, contract and limits are
The Manual's APPROVED LOGINS section (``bt guide``); the shared fixed
diagnostic vocabulary and value types are :mod:`browser_tools.login_contract`.

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
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import credential_relay, curated, lifecycle, login_ownership, login_policy
from . import login_contract as contract
from .core.errors import CDPError
from .lifecycle import LifecycleError
from .login_contract import DEFAULT_LOGIN_TIMEOUT_MS, LoginError, validate_timeout_ms
from .login_guards import DialogGuard, quiet_websocket_logs
from .one_shot import one_shot_page_session

if TYPE_CHECKING:
    from .endpoint import ResolvedEndpoint
    from .login_ownership import OwnershipSnapshot

#: How often the success condition is polled after submission.
SUCCESS_POLL_SECONDS = 0.25

#: Grace past the overall deadline that session-close teardown gets before
#: the hard wall. Separate from the field-clear budget: closing the one-shot
#: session (detach, socket close) is the slowest cleanup step, so it gets the
#: widest bound; relay group teardown and the field clear each keep their own
#: tighter bounds (``_RELAY_CANCEL_SETTLE_SECONDS`` and the 2-second clear).
_CLEANUP_GRACE_SECONDS = 8.0

#: Both operation walls (session acquisition and the body's active work)
#: fire this far after the cooperative deadline so the precise phase checks
#: ("timed out during ...", "success condition was not met") always report
#: first; a wall only catches phases that ignore the deadline, such as a hung
#: session attach. Field finalization and dialog shutdown never run inside a
#: wall: they draw from their own budgets instead.
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

logger = logging.getLogger(__name__)


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
                phase: str, deadline: contract.Deadline) -> Any:
    """One bounded CDP send whose failures become fixed diagnostics."""
    timeout = deadline.command_timeout(phase)
    try:
        result = await cdp.send(method=method, params=params, session_id=session_id, timeout=timeout)
    except Exception:
        logger.debug("login CDP send failed at %s", phase)
        raise LoginError(contract.SESSION_FAILED.format(phase=phase)) from None
    if not isinstance(result, dict):
        raise LoginError(contract.SESSION_FAILED.format(phase=phase))
    return result


def _outcome_of(result: object, phase: str) -> dict[str, Any]:
    """Parse a by-value Runtime result without exposing protocol contents."""
    if not isinstance(result, dict) or result.get("exceptionDetails"):
        raise LoginError(contract.SESSION_FAILED.format(phase=phase))
    remote = result.get("result")
    if not isinstance(remote, dict) or not isinstance(remote.get("value"), dict):
        raise LoginError(contract.SESSION_FAILED.format(phase=phase))
    return remote["value"]


async def _eval_login_script(cdp: Any, session_id: str, frame_id: str, expression_tail: str,
                             *, return_by_value: bool, phase: str,
                             deadline: contract.Deadline) -> dict[str, Any]:
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
        raise LoginError(contract.SESSION_FAILED.format(phase=phase))
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
                           function_declaration: str, phase: str, deadline: contract.Deadline,
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


async def _read_frame(cdp: Any, session_id: str, deadline: contract.Deadline) -> contract.Frame:
    tree = await _send(cdp, session_id, "Page.getFrameTree", {}, "frame lookup", deadline)
    frame_tree = tree.get("frameTree")
    frame = frame_tree.get("frame") if isinstance(frame_tree, dict) else None
    if not isinstance(frame, dict):
        raise LoginError(contract.SESSION_FAILED.format(phase="frame lookup"))
    frame_id = frame.get("id")
    loader_id = frame.get("loaderId")
    if (
        not isinstance(frame_id, str) or not frame_id
        or not isinstance(loader_id, str) or not loader_id
    ):
        raise LoginError(contract.SESSION_FAILED.format(phase="frame lookup"))
    return contract.Frame(frame_id, loader_id)


async def _loader_changed(cdp: Any, session_id: str, frame: contract.Frame,
                          deadline: contract.Deadline) -> bool:
    """Whether a new document has loaded in the frame since validation.

    ``loaderId`` is a trusted browser-protocol fact (not page text): it
    changes on every document load and survives as the one same-frame
    identity check that works across navigations.
    """
    current = await _read_frame(cdp, session_id, deadline)
    return current.loader_id != frame.loader_id


def _ensure_relay_ready(policy: login_policy.LoginPolicy, deadline: contract.Deadline) -> str:
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
        raise LoginError(contract.TIMED_OUT)
    credential_relay.ensure_relay_supported(
        binary,
        policy.identity,
        timeout=min(credential_relay.PROBE_TIMEOUT_SECONDS, remaining),
    )
    return binary


async def _read_credentials(binary: str, policy: login_policy.LoginPolicy,
                            account: login_policy.AccountSpec,
                            deadline: contract.Deadline) -> tuple[str, str]:
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


class _LoginMarker(LoginError):
    """A body failure whose final diagnosis the controller must complete.

    The body raises these where the outcome depends on the bounded cleanup
    that runs outside the work wall; the controller replaces them with the
    precise fixed diagnostic once the clear's own result is known.
    """


class _FillFailed(_LoginMarker):
    """The fill call failed; whether it inserted anything is unknown."""

    def __init__(self) -> None:
        # A placeholder that never surfaces: the controller replaces it with
        # the precise diagnosis once the final clear's result is known.
        super().__init__("the form fill could not be confirmed")


class _SubmitFailed(_LoginMarker):
    """The submit call failed; whether it submitted anything is unknown."""

    def __init__(self) -> None:
        super().__init__("the form submission could not be confirmed")


class _State:
    """Base of the mutually exclusive progress variants."""


class _Untouched(_State):
    """Nothing was inserted: no clear is owed on any exit.

    This is also the state the body returns to when the page refuses the
    fill before insertion began, so a known pre-insert refusal owes no
    cleanup either.
    """


class _MayHoldValues(_State):
    """Base of the variants where field values may exist."""

    def __init__(self, session_object: str, frame: contract.Frame) -> None:
        self.session_object = session_object
        self.frame = frame


class _FillDispatched(_MayHoldValues):
    """The fill call was dispatched; insertion may have started."""


class _Filled(_MayHoldValues):
    """Both values were inserted and revalidated."""


class _SubmitDispatched(_MayHoldValues):
    """The submit call was dispatched; its outcome is unknown."""


class _Submitted(_MayHoldValues):
    """The submission request returned successfully."""


class _Progress:
    """What the transaction has done that finalization must answer for.

    The body marks this object immediately before the field and submit
    calls (and back to untouched on a known pre-insert refusal), so that an
    exception -- including cancellation -- never loses the insertion state.
    The controller owns the object and reads it after the work wall, on
    every exit, to decide whether a bounded final clear is owed.
    """

    def __init__(self) -> None:
        self.state: _State = _Untouched()

    def mark(self, state: _State) -> None:
        self.state = state


async def _login_transaction(port: int | ResolvedEndpoint, target_spec: str | None, target_by: str | None,
                             policy: login_policy.LoginPolicy,
                             account: login_policy.AccountSpec,
                             binary: str, deadline: contract.Deadline,
                             ownership: OwnershipSnapshot) -> None:
    """The whole browser-side transaction, on one one-shot session.

    The deadline was created before the capability probe and is never reset,
    so the documented timeout covers session attach too (a registry session
    has no per-command open bound of its own). The operation wall covers
    session acquisition only; everything after attach owns its own budget:
    :func:`_transaction_on_session` wraps its active work in a wall of the
    remaining budget, runs all field finalization and dialog shutdown
    outside that wall, and the session teardown below runs in its own
    bounded grace, so a stalled detach cannot replace the body's diagnosis:
    a fields-may-remain warning outranks a timeout. The inner wall cancels
    at the deadline only after the cooperative phase checks have already
    reported their precise fixed messages.
    """
    if deadline.remaining() <= 0:
        raise LoginError(contract.TIMED_OUT)
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
                cdp, session_id, ownership, policy, account, binary, deadline
            )
        except LoginError as failure:
            body_failure = failure
        except TimeoutError:
            body_failure = LoginError(contract.TIMED_OUT)
        except BaseException as exc:
            propagating = exc
            raise
    finally:
        # Teardown is bounded by the cleanup grace, separate from both the
        # acquisition wall and the field-clear budget. An exception raised
        # here would replace one already propagating, so teardown failures
        # are contained: with a body failure or an in-flight exception they
        # are dropped; with nothing else to report, a stalled teardown is
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
                        contract.TIMED_OUT if isinstance(failure, TimeoutError)
                        else contract.SESSION_FAILED.format(phase="session cleanup")
                    )
    if body_failure is not None:
        raise body_failure


async def _transaction_on_session(cdp: Any, session_id: str, ownership: OwnershipSnapshot,
                                  policy: login_policy.LoginPolicy,
                                  account: login_policy.AccountSpec, binary: str,
                                  deadline: contract.Deadline) -> None:
    """Own the active work, then every field finalization, outside its wall.

    The work wall bounds the active phase only (dialog enable, validation,
    credential reads, fill, submission request) and fires just past the
    cooperative deadline. Field finalization and dialog shutdown run
    outside that wall on budgets separate from it and from each other: the
    2-second field-clear bound, the guard's own settle bound, and the
    session-close grace owned by the transaction below. A page that
    restores values after a confirmed clear is why the final clear runs on
    every exit where insertion was possible.
    """
    await login_ownership.verify_connected_browser(
        cdp, ownership, deadline.command_timeout("owner verification")
    )
    frame = await _read_frame(cdp, session_id, deadline)
    dialogs = DialogGuard(cdp, session_id)
    dialogs.start()
    failure: LoginError | None = None
    try:
        progress = _Progress()
        submitted = False
        try:
            async with asyncio.timeout(deadline.remaining() + _WALL_CUSHION_SECONDS):
                await _send(cdp, session_id, "Page.enable", {}, "dialog handling", deadline)
                await _transaction_body(
                    cdp, session_id, frame, policy, account, binary, deadline, progress
                )
            submitted = True
        except asyncio.CancelledError:
            # Ordinary external cancellation: cleanup first, then propagate,
            # unless the clear could not be confirmed - the sensitive-field
            # warning outranks the cancellation.
            final = await _finalize_after_cancellation(cdp, session_id, progress)
            if final is not None:
                failure = final
            else:
                raise
        except LoginError as error:
            failure = await _finalize_failure(cdp, session_id, progress, error)
        except TimeoutError:
            failure = await _finalize_failure(
                cdp, session_id, progress, LoginError(contract.TIMED_OUT)
            )
        except BaseException:
            # Relay and transport failures keep their own fixed text; only
            # the owed clear runs here, and its outcome cannot replace what
            # already has a diagnosis of its own.
            await _finalize_failure(cdp, session_id, progress, None)
            raise
        if submitted:
            state = progress.state
            if isinstance(state, _MayHoldValues):
                cleared = await _try_clear(cdp, session_id, state.session_object, state.frame)
                if cleared is False:
                    failure = LoginError(contract.FIELDS_MAY_REMAIN, fields_may_remain=True)
                else:
                    try:
                        await _wait_for_success(cdp, session_id, frame, account, deadline)
                    except LoginError as unmet:
                        cleared = await _try_clear(
                            cdp, session_id, state.session_object, state.frame
                        )
                        failure = (
                            LoginError(contract.FIELDS_MAY_REMAIN, fields_may_remain=True)
                            if cleared is False else unmet
                        )
                    except asyncio.CancelledError:
                        cleared = await _try_clear(
                            cdp, session_id, state.session_object, state.frame
                        )
                        if cleared is False:
                            failure = LoginError(contract.FIELDS_MAY_REMAIN, fields_may_remain=True)
                        else:
                            raise
                    else:
                        cleared = await _try_clear(
                            cdp, session_id, state.session_object, state.frame
                        )
                        if cleared is False:
                            failure = LoginError(contract.FIELDS_MAY_REMAIN, fields_may_remain=True)
    finally:
        try:
            await dialogs.close()
        except (Exception, asyncio.CancelledError):
            if failure is None or not failure.fields_may_remain:
                raise
    if failure is not None:
        raise failure


async def _transaction_body(cdp: Any, session_id: str, frame: contract.Frame,
                            policy: login_policy.LoginPolicy,
                            account: login_policy.AccountSpec, binary: str,
                            deadline: contract.Deadline, progress: _Progress) -> None:
    """Validate, read, fill and request submission, marking ``progress``.

    Cleanup and success polling are the controller's: this body only marks
    what it has done, immediately before the calls that make insertion
    possible, so the controller can answer for the fields on every exit.
    """
    policy_json = _policy_json_for_page(account)

    # -- Validation, in the document, before any credential is read. -------
    begin = await _eval_login_script(
        cdp, session_id, frame.frame_id, f"__btLogin.begin({policy_json})",
        return_by_value=False, phase="form validation", deadline=deadline,
    )
    if begin.get("exceptionDetails"):
        # A thrown helper is not a session object; its Error remote object
        # is never trusted as one.
        raise LoginError(contract.SESSION_FAILED.format(phase="form validation"))
    session_object = (begin.get("result") or {}).get("objectId")
    if not isinstance(session_object, str) or not session_object:
        raise LoginError(contract.SESSION_FAILED.format(phase="form validation"))
    outcome = await _call_on_session(
        cdp, session_id, session_object,
        "function () { return this.outcome; }", "form validation", deadline,
    )
    if outcome.get("ok") is not True:
        raise LoginError(contract.reason_message(str(outcome.get("reason", ""))))

    # -- Credential reads, between validation and any insertion. -----------
    username, password = await _read_credentials(binary, policy, account, deadline)

    # -- Revalidation in the bound world, then insertion. -------------------
    if await _loader_changed(cdp, session_id, frame, deadline):
        raise LoginError(contract.PAGE_CHANGED_BEFORE_FILL)
    progress.mark(_FillDispatched(session_object, frame))
    try:
        fill = await _call_on_session(
            cdp, session_id, session_object,
            "function (u, p) { return this.fill(u, p); }", "form fill", deadline,
            arguments=[{"value": username}, {"value": password}],
        )
    except asyncio.CancelledError:
        raise
    except LoginError as failure:
        raise _FillFailed() from failure
    if fill.get("ok") is not True:
        message = contract.reason_message(str(fill.get("reason", "")))
        if fill.get("insertionStarted") is False:
            # The page refused before insertion began; the document is still
            # untouched and owes no clear.
            progress.mark(_Untouched())
            raise LoginError(message)
        raise LoginError(message)
    progress.mark(_Filled(session_object, frame))

    # -- Submission on the validated form. The JS helper clears the fields
    # -- synchronously before returning; the controller confirms that clear
    # -- and polls for success, both outside the work wall.
    progress.mark(_SubmitDispatched(session_object, frame))
    try:
        submitted = await _call_on_session(
            cdp, session_id, session_object,
            "function () { return this.submit(); }", "form submission", deadline,
        )
    except asyncio.CancelledError:
        raise
    except LoginError as failure:
        raise _SubmitFailed() from failure
    if submitted.get("ok") is not True:
        raise LoginError(contract.reason_message(str(submitted.get("reason", ""))))
    progress.mark(_Submitted(session_object, frame))


async def _finalize_failure(cdp: Any, session_id: str, progress: _Progress,
                            failure: LoginError | None) -> LoginError | None:
    """Run the owed final clear; return the diagnosis to report.

    Nothing was inserted (validation refusal, pre-insert refusal, a relay
    or loader failure before the fill) owes no clear and keeps its own
    diagnosis. Every other failure runs one bounded clear and maps to the
    precise fixed diagnostic its result selects, preserving the distinctions
    between a replaced document, a confirmed clear, and uncertainty.
    """
    state = progress.state
    if not isinstance(state, _MayHoldValues):
        return failure
    cleared = await _try_clear(cdp, session_id, state.session_object, state.frame)
    if failure is None:
        return None
    if isinstance(failure, _FillFailed):
        if cleared is None:
            return LoginError(contract.PAGE_CHANGED_DURING_FILL)
        if cleared:
            return LoginError(contract.FILL_UNCERTAIN_CLEARED)
        return LoginError(contract.FIELDS_MAY_REMAIN, fields_may_remain=True)
    if isinstance(failure, _SubmitFailed):
        if cleared is None:
            return LoginError(contract.PAGE_CHANGED_BEFORE_SUBMIT)
        if cleared:
            return LoginError(contract.SUBMIT_UNCERTAIN_CLEARED)
        return LoginError(contract.FIELDS_MAY_REMAIN, fields_may_remain=True)
    if cleared is False:
        return LoginError(f"{failure}. {contract.FIELDS_MAY_REMAIN}", fields_may_remain=True)
    return failure


async def _finalize_after_cancellation(cdp: Any, session_id: str,
                                       progress: _Progress) -> LoginError | None:
    """The same finalization for an external cancellation.

    Returns None when the cancellation should propagate after cleanup; a
    returned failure (an unconfirmed clear, or a document change whose
    fixed diagnosis outranks the cancellation) replaces it.
    """
    state = progress.state
    if not isinstance(state, _MayHoldValues):
        return None
    cleared = await _try_clear(cdp, session_id, state.session_object, state.frame)
    if cleared is False:
        return LoginError(contract.FIELDS_MAY_REMAIN, fields_may_remain=True)
    if isinstance(state, _FillDispatched) and cleared is None:
        return LoginError(contract.PAGE_CHANGED_DURING_FILL)
    if isinstance(state, _SubmitDispatched) and cleared is None:
        return LoginError(contract.PAGE_CHANGED_BEFORE_SUBMIT)
    return None


async def _try_clear(cdp: Any, session_id: str, session_object: str, frame: contract.Frame) -> bool | None:
    """Clear the bound fields. True=cleared, False=uncertain, None=replaced.

    A document replacement (the bound world died with it) is a clean clear:
    the values went with the document. Only an uncertain outcome on a
    still-live document means the fields may remain.

    Cleanup is still owed after the operation deadline. It runs outside the
    work wall on its own budget - the 2-second bound below, separate from
    both the operation walls and the session-close grace - never on the
    operation's remaining time.
    """
    cleanup_deadline = contract.Deadline(2000)
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


async def _wait_for_success(cdp: Any, session_id: str, frame: contract.Frame,
                            account: login_policy.AccountSpec,
                            deadline: contract.Deadline) -> None:
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
            raise LoginError(contract.SUCCESS_UNMET)
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
                raise LoginError(contract.SESSION_FAILED.format(phase="success check"))
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
                raise LoginError(contract.SESSION_FAILED.format(phase="success check")) from None
            logger.debug("login success probe lost its context (continuing)")
        except TimeoutError:
            logger.debug("login success probe timed out (continuing)")
        except Exception:
            raise LoginError(contract.SESSION_FAILED.format(phase="success check")) from None
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
    deadline = contract.Deadline(timeout_ms)
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
        raise LifecycleError(contract.ENGINE_MESSAGE)
    # Positive ownership before anything else runs: the relay probe, the
    # session, and the reads all come after this. A registry row alone (or
    # a live-looking port) is never enough to hand a credential to.
    ownership = login_ownership.verify_recorded_owner(entry)
    port = lifecycle.resolve_cdp_port(name, registry_path=registry_path)
    binary = _ensure_relay_ready(policy, deadline)

    # One derivation of the --target grammar, shared with every other verb.
    target_spec, target_by = curated.target_selector(target, None)

    try:
        with quiet_websocket_logs():
            asyncio.run(
                _login_transaction(
                    port, target_spec, target_by, policy, account_spec, binary,
                    deadline, ownership,
                )
            )
    except LifecycleError:
        raise
    except Exception as exc:
        # Transport libraries can include tab URLs, titles and protocol
        # contents in errors; only fixed diagnostics cross this boundary.
        raise LoginError(contract.acquisition_message(exc)) from None
    return {"account": account_spec.alias, "status": "authenticated"}


__all__ = ["DEFAULT_LOGIN_TIMEOUT_MS", "LoginError", "login", "validate_timeout_ms"]
