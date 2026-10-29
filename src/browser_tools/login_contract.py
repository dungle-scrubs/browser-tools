"""Shared login contracts: fixed diagnostics, reason mapping, value types.

Shared vocabulary, deadline rules and finalization policy for the login
operation, extracted from :mod:`browser_tools.login` (repository module-size
rule). Browser and provider calls remain in the transaction module:

- :class:`LoginError` and the fixed diagnostic sentences (the JS helper's
  reason vocabulary and the session-acquisition mapping) - no browser- or
  provider-provided text ever crosses this boundary.
- :func:`validate_timeout_ms` - the one timeout rule shared by the CLI
  preconditions and the direct entry point.
- :class:`Deadline` (the one overall deadline every phase draws from) and
  :class:`Frame` (main-frame document identity), the value types the
  transaction passes around.
- The per-command timeout cap and the default overall timeout.
- The progress phases, stop causes and :class:`Progress` record the
  transaction's body keeps, and :func:`diagnosis`, the one mapping from
  (phase, cause, clear result) to the fixed diagnosis - the vocabulary
  both the body and the finalization owner agree on, extracted from
  login.py (module-size rule) without moving any browser-side behavior
  here.

The operation walkthrough this contract serves is The Manual's ``bt login``
section (``bt guide``); the transaction itself lives in login.py.
"""

from __future__ import annotations

import math
import time

from . import login_policy
from .core.attach import AmbiguousTargetError, TargetNotFoundError
from .core.errors import CDPError, NoPageError
from .lifecycle import LifecycleError

#: Overall deadline default, shared with the CLI flag.
DEFAULT_LOGIN_TIMEOUT_MS = 30_000

#: Per-CDP-command bound cap so one stalled command cannot eat the whole
#: deadline before the phase that needs it.
PER_COMMAND_TIMEOUT_SECONDS = 10.0

#: Fixed English for the JS helper's fixed reason vocabulary.
REASONS: dict[str, str] = {
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


#: Fixed diagnostics. None of these interpolate browser or provider text.
ENGINE_MESSAGE = (
    "login works only with Chrome instances. This instance runs a different "
    "engine, which the login transaction does not support"
)
TIMED_OUT = "the login did not complete within its timeout"
#: State-neutral: the login may have succeeded and the SPA restored values,
#: so this warning never claims the login failed or did not complete.
FIELDS_MAY_REMAIN = (
    "sensitive form fields may still hold values; check the tab before "
    "reusing it. The form was not resubmitted"
)
SESSION_FAILED = "the browser session failed during login ({phase})"
SUCCESS_UNMET = (
    "the form submission was requested but the configured success condition was not met "
    "within the timeout"
)
PAGE_CHANGED_BEFORE_SUBMIT = (
    "the page changed while submission was in progress; whether it completed "
    "could not be confirmed. The form was not resubmitted"
)
PAGE_CHANGED_BEFORE_FILL = (
    "the page changed after validation and before any value was inserted; "
    "nothing was sent to a replacement document"
)
PAGE_CHANGED_DURING_FILL = (
    "the page changed while the form was being filled; whether the values "
    "were inserted could not be confirmed, but nothing was sent to a "
    "replacement document and the form was not submitted"
)
FILL_UNCERTAIN_CLEARED = (
    "the fill could not be confirmed; the configured fields were cleared and "
    "the form was not submitted"
)
SUBMIT_UNCERTAIN_CLEARED = (
    "the form submission could not be confirmed; the configured fields were "
    "cleared and the form was not resubmitted"
)


class LoginError(LifecycleError):
    """One fixed-diagnostic login failure (CLI exit 1)."""

    def __init__(self, message: str, *, fields_may_remain: bool = False) -> None:
        super().__init__(message)
        self.fields_may_remain = fields_may_remain


class DeadlineExpiredError(LoginError):
    """The one operation deadline expired (a cooperative phase check).

    Internal to the login operation: the controller distinguishes "the
    clock ran out" from a call that failed with its own fixed error, so a
    timeout after a dispatched submission maps to the uncertain-submission
    diagnostic instead of a confirmed failure. Not part of the module's
    public vocabulary (``__all__``).
    """


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


def acquisition_message(exc: BaseException) -> str:
    """A fixed diagnostic for one session-entry failure.

    The underlying errors carry every tab's URL and title, or arbitrary
    protocol text - page-provided data the output channels never see. The
    generic :func:`one_shot.cli_cdp_errors` mapping is deliberately not
    used: it forwards ``str(exc)`` verbatim.
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


class Deadline:
    """The one overall deadline every phase draws from.

    The end time is immutable: a phase reads the remaining budget when it
    starts and may never refresh the absolute end. ``command_timeout`` is
    that read for one CDP command - each sequential call in a phase draws
    its own allowance, so two chained calls can never jointly outlive the
    operation.
    """

    def __init__(self, timeout_ms: int) -> None:
        self._ends_at = time.monotonic() + timeout_ms / 1000.0

    def remaining(self) -> float:
        return self._ends_at - time.monotonic()

    def command_timeout(self) -> float:
        """One command's allowance: remaining time, capped per command."""
        remaining = self.remaining()
        if remaining <= 0:
            raise DeadlineExpiredError(TIMED_OUT)
        return min(PER_COMMAND_TIMEOUT_SECONDS, remaining)


def reason_message(reason: str) -> str:
    """Fixed English for one JS reason code; unknown codes stay fixed too."""
    if reason in REASONS:
        return REASONS[reason]
    return "the login form did not match the configured policy"


class Frame:
    """The main frame's id and its loader id: the document identity."""

    def __init__(self, frame_id: str, loader_id: str) -> None:
        self.frame_id = frame_id
        self.loader_id = loader_id



# --------------------------------------------------------------------------- #
# Progress phases, stop causes, and the one finalization mapping
# --------------------------------------------------------------------------- #


class Phase:
    """The mutually exclusive progress phases the body records.

    In order: nothing inserted yet, the fill call dispatched, the fill
    call returned, the submit call dispatched, the submit call returned,
    and positive success observed. A dispatched phase means that call's
    outcome is unknown (it never returned); a response phase means the
    call returned, so its refusals are known facts rather than
    uncertainties.
    """

    UNTOUCHED = "untouched"
    FILL_DISPATCHED = "fill-dispatched"
    FILL_RESPONSE = "fill-response"
    SUBMIT_DISPATCHED = "submit-dispatched"
    SUBMIT_RESPONSE = "submit-response"
    OBSERVED_SUCCESS = "observed-success"


class KnownFailure:
    """The work learned its own fixed answer (a form or page refusal)."""

    def __init__(self, error: LoginError) -> None:
        self.error = error


class UnknownFailure:
    """A call failed while its outcome stayed unknown (transport/session)."""

    def __init__(self, error: LoginError) -> None:
        self.error = error


class ExpiredCause:
    """The one operation deadline ran out during normal work."""

    def __init__(self, error: LoginError) -> None:
        self.error = error


class UnmetSuccess:
    """The submission returned, but success was not met within the clock."""

    def __init__(self, error: LoginError | None) -> None:
        self.error = error


class CancelledCause:
    """External cancellation: propagates after confirmed clearing."""


class SucceededCause:
    """Positive success observed and accepted within the clock."""


CANCELLED = CancelledCause()
SUCCEEDED = SucceededCause()


class Progress:
    """What the transaction has done that finalization must answer for.

    The body records the phase immediately before the calls that make
    insertion possible (and back to untouched on a known pre-insert
    refusal), so an exception -- including cancellation -- never loses the
    insertion state. The document-bound form/frame is carried only from
    fill-dispatched onward: the only phases where field values may exist.
    The controller owns this object, appends the stop cause when normal
    work ends, and reads both, on every exit, to run the one owed final
    clear and select the fixed diagnosis its result picks.
    """

    def __init__(self) -> None:
        self.phase = Phase.UNTOUCHED
        self.stop: KnownFailure | UnknownFailure | ExpiredCause | UnmetSuccess | CancelledCause | SucceededCause | None = None
        self.session_object: str | None = None
        self.frame: Frame | None = None

    def mark_fill_dispatched(self, session_object: str, frame: Frame) -> None:
        self.phase = Phase.FILL_DISPATCHED
        self.session_object = session_object
        self.frame = frame

    def mark_fill_response(self) -> None:
        self.phase = Phase.FILL_RESPONSE

    def mark_submit_dispatched(self) -> None:
        self.phase = Phase.SUBMIT_DISPATCHED

    def mark_submit_response(self) -> None:
        self.phase = Phase.SUBMIT_RESPONSE

    def mark_observed_success(self) -> None:
        self.phase = Phase.OBSERVED_SUCCESS

    def mark_untouched(self) -> None:
        """A known pre-insert refusal: the document owes no clear."""
        self.phase = Phase.UNTOUCHED
        self.session_object = None
        self.frame = None

    def binding(self) -> tuple[str, Frame]:
        """The bound form/frame, from fill-dispatched onward."""
        assert self.session_object is not None and self.frame is not None
        return self.session_object, self.frame

    def stop_with(self, cause: KnownFailure | UnknownFailure | ExpiredCause
                  | UnmetSuccess | CancelledCause | SucceededCause) -> None:
        self.stop = cause


def may_remain() -> LoginError:
    """The sensitive-field warning, which outranks every other outcome."""
    return LoginError(FIELDS_MAY_REMAIN, fields_may_remain=True)


def diagnosis(progress: Progress, cleared: bool | None) -> LoginError | None:
    """The one phase/cause-to-diagnosis mapping.

    ``cleared`` is the final clear's result: True confirmed, False
    uncertain on a live document (the may-remain warning outranks
    everything), None replaced document (a clean clear: the values went
    with the document). Returns None when there is nothing to report - an
    authenticated success, or a cancellation that must propagate.
    """
    phase = progress.phase
    cause = progress.stop
    if phase == Phase.UNTOUCHED:
        # Nothing was inserted: no clear is owed and each cause keeps its
        # own fixed error; a cancellation propagates unchanged.
        if cause is CANCELLED or not isinstance(
            cause, (KnownFailure, UnknownFailure, ExpiredCause, UnmetSuccess)
        ):
            return None
        return cause.error
    if cause is CANCELLED:
        if cleared is False:
            return may_remain()
        if cleared is None and phase == Phase.FILL_DISPATCHED:
            return LoginError(PAGE_CHANGED_DURING_FILL)
        if cleared is None and phase == Phase.SUBMIT_DISPATCHED:
            return LoginError(PAGE_CHANGED_BEFORE_SUBMIT)
        return None
    if isinstance(cause, (UnknownFailure, ExpiredCause)) and phase in (
        Phase.FILL_DISPATCHED, Phase.SUBMIT_DISPATCHED,
    ):
        # The call never returned (or the clock ran out mid-flight): the
        # insertion/submission outcome is unknown, so the fixed
        # uncertain-outcome sentences speak, never a confirmed failure.
        if cleared is False:
            return may_remain()
        uncertain_fill = phase == Phase.FILL_DISPATCHED
        if cleared is None:
            return LoginError(
                PAGE_CHANGED_DURING_FILL if uncertain_fill
                else PAGE_CHANGED_BEFORE_SUBMIT
            )
        return LoginError(
            FILL_UNCERTAIN_CLEARED if uncertain_fill
            else SUBMIT_UNCERTAIN_CLEARED
        )
    if isinstance(cause, (KnownFailure, UnknownFailure, ExpiredCause)):
        # A known refusal keeps its reason once the clear is confirmed; an
        # unconfirmed clear adds the warning to it rather than replacing
        # it. Elsewhere the fixed error itself is the diagnosis.
        if cleared is False:
            return LoginError(
                f"{cause.error}. {FIELDS_MAY_REMAIN}", fields_may_remain=True
            )
        return cause.error
    if isinstance(cause, UnmetSuccess):
        if cleared is False:
            return may_remain()
        return cause.error if cause.error is not None else LoginError(SUCCESS_UNMET)
    if cause is SUCCEEDED:
        return may_remain() if cleared is False else None
    return None

__all__ = [
    "DEFAULT_LOGIN_TIMEOUT_MS",
    "ENGINE_MESSAGE",
    "FIELDS_MAY_REMAIN",
    "FILL_UNCERTAIN_CLEARED",
    "PAGE_CHANGED_BEFORE_FILL",
    "PAGE_CHANGED_BEFORE_SUBMIT",
    "PAGE_CHANGED_DURING_FILL",
    "PER_COMMAND_TIMEOUT_SECONDS",
    "REASONS",
    "SESSION_FAILED",
    "SUBMIT_UNCERTAIN_CLEARED",
    "SUCCESS_UNMET",
    "TIMED_OUT",
    "Deadline",
    "Frame",
    "LoginError",
    "acquisition_message",
    "reason_message",
    "validate_timeout_ms",
]
