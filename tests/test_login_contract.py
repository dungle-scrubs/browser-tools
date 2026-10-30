"""The finalization mapping: one matrix, fail-closed at its edges.

Direct unit proofs of :func:`browser_tools.login_contract.diagnosis` - the
one (state, cause, clear result) -> fixed diagnosis mapping - and of the
progress variants it reads. The behavioral proofs of the same matrix run
the real controller over the fake CDP boundary (tests/test_login_cancellation.py)
and real disposable browsers (tests/test_login_browser.py,
tests/test_login_live.py); these direct cases pin the mapping itself,
including the combinations the controller can never produce, which must
fail closed with the fixed finalization sentence instead of silently
granting an outcome (round-four Q9/B1).
"""

from __future__ import annotations

import pytest

from browser_tools import login_contract as contract

SESSION = "session-object-1"
FRAME = contract.Frame("frame-1", "loader-1")


def error(text: str) -> contract.LoginError:
    return contract.LoginError(text)


def bound(phase: contract.BoundPhase) -> contract.BoundProgress:
    return contract.BoundProgress(SESSION, FRAME, phase)


def progress_in(
    state: contract.Untouched | contract.BoundProgress, stop=None
) -> contract.Progress:
    """A record holding exactly the given state (and optional stop cause)."""
    progress = contract.Progress()
    progress.state = state
    if stop is not None:
        progress.stop_with(stop)
    return progress


class TestProgressVariants:
    def test_a_fresh_record_is_untouched_and_carries_no_binding(self):
        progress = contract.Progress()
        assert isinstance(progress.state, contract.Untouched)
        assert progress.stop is None

    def test_binding_is_required_and_immutable_from_fill_dispatched(self):
        progress = contract.Progress()
        progress.mark_fill_dispatched(SESSION, FRAME)
        progress.mark_fill_response()
        progress.mark_submit_dispatched()
        progress.mark_submit_response()
        progress.mark_observed_success()
        state = progress.state
        assert isinstance(state, contract.BoundProgress)
        assert state.session_object == SESSION
        assert state.frame is FRAME
        assert state.phase is contract.BoundPhase.OBSERVED_SUCCESS

    def test_a_pre_insert_refusal_returns_to_untouched(self):
        progress = contract.Progress()
        progress.mark_fill_dispatched(SESSION, FRAME)
        progress.mark_untouched()
        assert isinstance(progress.state, contract.Untouched)

    def test_a_bound_phase_cannot_be_advanced_while_untouched(self):
        """No assertion recovers missing handles: advancing an unbound
        record is an impossible transition and fails closed with the fixed
        finalization sentence."""
        progress = contract.Progress()
        with pytest.raises(contract.LoginError, match="finalization"):
            progress.mark_fill_response()


class TestDiagnosisMatrix:
    """Every reachable (state, cause, cleared) keeps its fixed sentence."""

    @pytest.mark.parametrize("cleared", [True, None])
    def test_an_observed_success_with_a_certified_clear_reports_nothing(
        self, cleared
    ):
        # Observed success and no cause: the authenticated outcome.
        progress = progress_in(bound(contract.BoundPhase.OBSERVED_SUCCESS))
        assert contract.diagnosis(progress, cleared) is None

    def test_an_observed_success_with_an_uncertain_clear_warns(self):
        progress = progress_in(bound(contract.BoundPhase.OBSERVED_SUCCESS))
        result = contract.diagnosis(progress, False)
        assert result is not None and result.fields_may_remain

    @pytest.mark.parametrize("cause_class", [
        contract.KnownFailure, contract.UnknownFailure, contract.ExpiredCause,
    ])
    def test_untouched_keeps_each_cause_and_its_own_error(self, cause_class):
        progress = progress_in(
            contract.Untouched(), cause_class(error("SENTINEL-cause-error"))
        )
        result = contract.diagnosis(progress, None)
        assert result is not None and str(result) == "SENTINEL-cause-error"

    @pytest.mark.parametrize("phase, cleared, expected", [
        (
            contract.BoundPhase.FILL_DISPATCHED, True,
            contract.FILL_UNCERTAIN_CLEARED,
        ),
        (
            contract.BoundPhase.FILL_DISPATCHED, None,
            contract.PAGE_CHANGED_DURING_FILL,
        ),
        (
            contract.BoundPhase.SUBMIT_DISPATCHED, True,
            contract.SUBMIT_UNCERTAIN_CLEARED,
        ),
        (
            contract.BoundPhase.SUBMIT_DISPATCHED, None,
            contract.PAGE_CHANGED_BEFORE_SUBMIT,
        ),
    ])
    def test_an_uncertain_outcome_is_never_a_confirmed_failure(
        self, phase, cleared, expected
    ):
        progress = progress_in(bound(phase), contract.ExpiredCause(error("unused")))
        result = contract.diagnosis(progress, cleared)
        assert result is not None and expected in str(result)

    def test_an_uncertain_clear_on_a_live_document_outranks_everything(self):
        progress = progress_in(
            bound(contract.BoundPhase.SUBMIT_DISPATCHED),
            contract.UnknownFailure(error("SENTINEL-cause-error")),
        )
        result = contract.diagnosis(progress, False)
        assert result is not None and result.fields_may_remain

    def test_a_known_refusal_after_the_response_keeps_its_reason(self):
        progress = progress_in(
            bound(contract.BoundPhase.SUBMIT_RESPONSE),
            contract.KnownFailure(error("SENTINEL-refusal")),
        )
        result = contract.diagnosis(progress, True)
        assert result is not None and str(result) == "SENTINEL-refusal"

    def test_an_unmet_success_keeps_its_fixed_sentence(self):
        progress = progress_in(
            bound(contract.BoundPhase.SUBMIT_RESPONSE), contract.UnmetSuccess(None)
        )
        result = contract.diagnosis(progress, True)
        assert result is not None and contract.SUCCESS_UNMET in str(result)

    def test_a_cancellation_propagates_when_nothing_is_owed(self):
        progress = progress_in(contract.Untouched(), contract.CANCELLED)
        assert contract.diagnosis(progress, None) is None


class TestImpossibleStatesFailClosed:
    """Combinations the controller cannot produce never grant an outcome.

    Round-four B1: the previous string-phase mapping returned None - the
    authenticated outcome - for any unrecognized combination. These pin
    the fail-closed edge: one fixed session-failed(finalization) sentence.
    """

    @pytest.mark.parametrize("state_factory", [
        lambda: contract.Untouched(),
        lambda: bound(contract.BoundPhase.FILL_DISPATCHED),
        lambda: bound(contract.BoundPhase.FILL_RESPONSE),
        lambda: bound(contract.BoundPhase.SUBMIT_DISPATCHED),
        lambda: bound(contract.BoundPhase.SUBMIT_RESPONSE),
    ])
    def test_no_cause_outside_observed_success_fails_closed(
        self, state_factory
    ):
        progress = contract.Progress()
        progress.state = state_factory()
        result = contract.diagnosis(progress, True)
        assert result is not None
        assert "finalization" in str(result)
        assert not result.fields_may_remain

    @pytest.mark.parametrize("cause", [None, object()])
    def test_uncertain_clear_keeps_warning_priority_even_for_invalid_state(self, cause):
        progress = progress_in(bound(contract.BoundPhase.FILL_DISPATCHED))
        progress.stop = cause
        result = contract.diagnosis(progress, False)
        assert result is not None and "finalization" in str(result)
        assert result.fields_may_remain, "invalid-state refusal lost the sensitive-field warning"
        assert contract.FIELDS_MAY_REMAIN in str(result)

    def test_an_untouched_unmet_success_fails_closed(self):
        """The old mapping returned None (an authenticated success) here."""
        progress = contract.Progress()
        progress.state = contract.Untouched()
        progress.stop_with(contract.UnmetSuccess(None))
        result = contract.diagnosis(progress, None)
        assert result is not None and "finalization" in str(result)

    def test_an_unknown_cause_type_fails_closed(self):
        # A cause outside the fixed stop-cause vocabulary.
        progress = progress_in(bound(contract.BoundPhase.OBSERVED_SUCCESS))
        progress.stop = object()
        result = contract.diagnosis(progress, False)
        assert result is not None and "finalization" in str(result)
