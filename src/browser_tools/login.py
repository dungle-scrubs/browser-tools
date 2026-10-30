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
from .login_contract import (
    CANCELLED,
    DEFAULT_LOGIN_TIMEOUT_MS,
    BoundProgress,
    ExpiredCause,
    KnownFailure,
    LoginError,
    Progress,
    StopCause,
    UnknownFailure,
    UnmetSuccess,
    Untouched,
    diagnosis,
    may_remain,
    validate_timeout_ms,
)
from .login_guards import DialogGuard, quiet_websocket_logs
from .one_shot import one_shot_page_session

if TYPE_CHECKING:
    from .login_ownership import OwnershipSnapshot

#: How often the success condition is polled after submission.
SUCCESS_POLL_SECONDS = 0.25

#: Grace past the overall deadline that session-close teardown gets before
#: the hard wall. Separate from the field-clear budget: closing the one-shot
#: session (detach, socket close) is the slowest cleanup step, so it gets the
#: widest bound; relay group teardown and the field clear each keep their own
#: tighter bounds (``_RELAY_CANCEL_SETTLE_SECONDS`` and the 2-second clear).
_CLEANUP_GRACE_SECONDS = 8.0

#: All three operation walls (session acquisition, the body's active work,
#: and the success wait) fire this far after the cooperative deadline so
#: cooperative timeout and unmet-success checks report first; a wall
#: catches phases that ignore the deadline, such as a hung session attach.
#: Field finalization
#: and dialog shutdown never run inside a wall: they draw from their own
#: budgets instead.
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
    """One bounded CDP send whose failures become fixed diagnostics.

    The allowance is drawn when the call starts - remaining time, capped
    per command - so sequential calls each get their own budget and can
    never jointly outlive the one operation deadline.
    """
    timeout = deadline.command_timeout()
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
            raise contract.DeadlineExpiredError(
                "the login timed out waiting for the credential relay"
            )
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


async def _login_transaction(port: int, target_spec: str | None, target_by: str | None,
                             policy: login_policy.LoginPolicy,
                             account: login_policy.AccountSpec,
                             binary: str, deadline: contract.Deadline,
                             ownership: OwnershipSnapshot) -> None:
    """The whole browser-side transaction, on one one-shot session.

    The deadline was created before the capability probe and is never reset,
    so the documented timeout covers session attach too (a registry session
    has no per-command open bound of its own). The acquisition wall covers
    session acquisition only; everything after attach draws from the same
    immutable clock: :func:`_transaction_on_session` wraps its normal work
    in walls ending at that one absolute deadline, runs all field
    finalization and dialog shutdown outside them, and the session teardown
    below runs in its own bounded grace, so a stalled detach cannot replace
    the body's diagnosis: a fields-may-remain warning outranks a timeout.
    The inner walls cancel at the deadline only after the cooperative phase
    checks have already reported their precise fixed messages.
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
    """Own normal work and finalization under the one clock.

    Normal work -- owner verification, validation, credential reads,
    fill, submission, the complete success wait and acceptance of a
    positive result -- all draws from the one immutable deadline: every
    call takes its allowance when it starts, every wall (the body's and
    the success wait's) ends at that same absolute end (plus the
    cushion), and no positive result is accepted after it. Field
    finalization and dialog shutdown run outside the walls on budgets
    separate from the clock and from each other: the 2-second field-clear
    bound, the guard's own settle bound, and the session-close grace
    owned by the transaction below. A page that restores values after a
    confirmed clear is why the final clear runs on every exit where
    insertion was possible.
    """
    await login_ownership.verify_connected_browser(cdp, ownership, deadline)
    frame = await _read_frame(cdp, session_id, deadline)
    dialogs = DialogGuard(cdp, session_id)
    dialogs.start()
    failure: LoginError | None = None
    try:
        progress = Progress()
        try:
            async with asyncio.timeout(deadline.remaining() + _WALL_CUSHION_SECONDS):
                await _send(cdp, session_id, "Page.enable", {}, "dialog handling", deadline)
                await _transaction_body(
                    cdp, session_id, frame, policy, account, binary, deadline, progress
                )
            # The submit call returned ok: confirm the helper's synchronous
            # clear on the finalization budget, then wait for success under
            # the success wait's own wall, which ends at the same immutable
            # deadline as the body wall.
            state = progress.state
            if not isinstance(state, BoundProgress):
                # The body cannot return successfully without a bound form;
                # reaching here unbound is an impossible internal state.
                failure = LoginError(contract.FINALIZATION_FAILED)
            else:
                cleared = await _try_clear(
                    cdp, session_id, state.session_object, state.frame
                )
                if cleared is False:
                    failure = may_remain()
                else:
                    unmet = await _await_success(
                        cdp, session_id, frame, account, deadline, progress
                    )
                    failure = await _finalize(
                        cdp, session_id, progress,
                        None if unmet is None else UnmetSuccess(unmet),
                    )
        except asyncio.CancelledError:
            # Ordinary external cancellation: cleanup first, then
            # propagate, unless the clear could not be confirmed - the
            # sensitive-field warning outranks the cancellation.
            final = await _finalize(cdp, session_id, progress, CANCELLED)
            if final is not None:
                failure = final
            else:
                raise
        except contract.DeadlineExpiredError as expired:
            failure = await _finalize(cdp, session_id, progress, ExpiredCause(expired))
        except LoginError as error:
            if progress.stop is None:
                progress.stop_with(UnknownFailure(error))
            failure = await _finalize(cdp, session_id, progress)
        except TimeoutError:
            # A wall fired while normal work was in flight: the operation
            # clock ran out. (The success wait catches its own wall and
            # reports the unmet condition itself, so no post-submit arm is
            # needed here.)
            failure = await _finalize(
                cdp, session_id, progress, ExpiredCause(LoginError(contract.TIMED_OUT))
            )
        except BaseException:
            # Relay and transport failures keep their own fixed text; only
            # the owed clear runs here, and its outcome cannot replace what
            # already has a diagnosis of its own.
            await _finalize(cdp, session_id, progress, clear_only=True)
            raise
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
                            deadline: contract.Deadline, progress: Progress) -> None:
    """Validate, read, fill and request submission, recording ``progress``.

    Cleanup and success polling are the controller's: this body only marks
    the phase, immediately before the calls that make insertion possible,
    and the stop cause where the work learns its own fixed answer, so the
    controller can answer for the fields on every exit.
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
        error = LoginError(contract.reason_message(str(outcome.get("reason", ""))))
        progress.stop_with(KnownFailure(error))
        raise error

    # -- Credential reads, between validation and any insertion. -----------
    username, password = await _read_credentials(binary, policy, account, deadline)

    # -- Revalidation in the bound world, then insertion. -------------------
    if await _loader_changed(cdp, session_id, frame, deadline):
        error = LoginError(contract.PAGE_CHANGED_BEFORE_FILL)
        progress.stop_with(KnownFailure(error))
        raise error
    progress.mark_fill_dispatched(session_object, frame)
    fill = await _call_on_session(
        cdp, session_id, session_object,
        "function (u, p) { return this.fill(u, p); }", "form fill", deadline,
        arguments=[{"value": username}, {"value": password}],
    )
    progress.mark_fill_response()
    if fill.get("ok") is not True:
        message = contract.reason_message(str(fill.get("reason", "")))
        if fill.get("insertionStarted") is False:
            # The page refused before insertion began; the document is still
            # untouched and owes no clear.
            progress.mark_untouched()
        error = LoginError(message)
        progress.stop_with(KnownFailure(error))
        raise error

    # -- Submission on the validated form. The JS helper clears the fields
    # -- synchronously before returning; the controller confirms that clear
    # -- and then waits for success, outside the body wall but inside the
    # -- success wait's own wall on the same immutable deadline.
    progress.mark_submit_dispatched()
    submitted = await _call_on_session(
        cdp, session_id, session_object,
        "function () { return this.submit(); }", "form submission", deadline,
    )
    progress.mark_submit_response()
    if submitted.get("ok") is not True:
        error = LoginError(contract.reason_message(str(submitted.get("reason", ""))))
        progress.stop_with(KnownFailure(error))
        raise error


async def _finalize(cdp: Any, session_id: str, progress: Progress,
                    cause: StopCause | None = None, *,
                    clear_only: bool = False) -> LoginError | None:
    """Run the one owed final clear; return the diagnosis to report.

    The single owner of final clearing. Nothing inserted (validation
    refusal, pre-insert refusal, a relay or loader failure before the
    fill) owes no clear and keeps its own diagnosis; every other exit
    runs exactly one bounded clear - on the 2-second finalization budget
    inside :func:`_try_clear`, never the work deadline - and the one
    mapping above selects the resulting fixed diagnosis.
    ``clear_only`` is the path for exceptions with their own diagnosis
    (relay and transport failures whose text is already fixed): the owed
    clear still runs, and nothing here replaces what is propagating.
    """
    if cause is not None:
        progress.stop_with(cause)
    state = progress.state
    if isinstance(state, Untouched):
        return None if clear_only else diagnosis(progress, None)
    cleared = await _try_clear(cdp, session_id, state.session_object, state.frame)
    if clear_only:
        return None
    return diagnosis(progress, cleared)


async def _await_success(cdp: Any, session_id: str, frame: contract.Frame,
                         account: login_policy.AccountSpec,
                         deadline: contract.Deadline,
                         progress: Progress) -> LoginError | None:
    """Wait for success under the clock; None when observed and accepted.

    The wall ends at the same immutable deadline as the body wall, so a
    hung transport cannot extend the wait. Expiry inside the wait -
    cooperative or wall - is the unmet success condition, never a
    timeout of the operation and never an accepted late success.
    """
    try:
        async with asyncio.timeout(deadline.remaining() + _WALL_CUSHION_SECONDS):
            await _wait_for_success(cdp, session_id, frame, account, deadline)
    except (contract.DeadlineExpiredError, TimeoutError):
        return LoginError(contract.SUCCESS_UNMET)
    except LoginError as wait_failure:
        return wait_failure
    progress.mark_observed_success()
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
    it, and each of those two calls draws its own allowance when it
    starts - the evaluate's is recalculated after the world exists - so
    chained calls that individually fit an old allowance can never
    jointly outlive the deadline. After the evaluation returns, the
    clock is checked before any positive result is accepted, so a
    transport that ignores its supplied timeout cannot authenticate
    late. The POST response can commit between those two calls,
    destroying the fresh context; that is a transient not-yet (a
    connection loss is not - it exits fixed immediately).
    """
    expression = _probe_expression(account)
    while True:
        if deadline.remaining() <= 0:
            raise LoginError(contract.SUCCESS_UNMET)
        try:
            world = await cdp.send(
                method="Page.createIsolatedWorld",
                params={
                    "frameId": frame.frame_id,
                    "worldName": _LOGIN_WORLD,
                    "grantUniveralAccess": False,
                },
                session_id=session_id, timeout=deadline.command_timeout(),
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
                session_id=session_id, timeout=deadline.command_timeout(),
            )
        except LoginError:
            raise
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
            if deadline.remaining() <= 0:
                # The clock ran out while the probe call was in flight (a
                # transport may ignore its supplied timeout): no positive
                # result is accepted after the deadline.
                raise LoginError(contract.SUCCESS_UNMET)
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
    # a live-looking port) is never enough to hand a credential to. The
    # check validates and carries the row's recorded debug port: the login
    # connects to exactly that port instead of re-running the generic
    # liveness lookup, which can spend process time the deadline does not
    # have and treats an unattributable listener as alive.
    ownership = login_ownership.verify_recorded_owner(entry, deadline)
    port = ownership.port
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
