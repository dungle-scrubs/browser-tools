# Agent rules

Repeat-mistake rules for this repository, each paired with the check that
enforces it. A rule a check enforces stays out of `AGENTS.md`: the check's
error teaches it. Add a row when a mistake class is fixed at the enforcement
level, and drop the row once its mistake cannot happen.

| Rule | Enforced by | First occurrence |
| --- | --- | --- |
| When the Python caller stops using an entry point of the injected JS helper (`login_form.js`), delete the JS declaration, its export, and its contract-comment entry in the same change. The helper's exports must stay exactly the entry points `login.py` still references as `__btLogin.<name>`; success polling is standalone Python and is never a helper entry point. | `tests/test_login_live.py::TestBridgeContract::test_helper_exports_match_the_python_caller` (fails naming each unused export and instructs removal) | Round-two review, Quality Q4 and Reuse: the round-one polling rewrite left `probe` exported with no caller. |
| Provider startup failures produce fixed relay diagnostics without the operating-system error text. | `tests/test_credential_relay.py::TestMissingAndUnsupportedBinaries::test_an_unspawnable_binary_is_one_fixed_operational_error` and `test_an_injected_oserror_is_normalized_without_its_text` | The startup path in `869fdd6` let `OSError` escape the diagnostic boundary. |
