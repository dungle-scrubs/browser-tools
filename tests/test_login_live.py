"""The whole login flow against a disposable Chrome and a synthetic form.

The behavioral contracts: what the real ``login_form.js`` does against the
real DOM (native setters firing framework-visible events, ``requestSubmit``
on the validated button, same-origin POST with overrides, browser form
semantics), the real relay recipe end to end, the real loaderId identity
across the navigation the submit causes, the field-clearing guarantee on
both the navigating and the single-page shapes, positive ownership of the
registered browser, and the bridge contract between the Python caller and
the injected helper.

The disposable Chrome, the loopback HTTPS site and the fake opchain come
from login_fixtures.py, imported explicitly; everything here is synthetic.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
from pathlib import Path

import pytest

from browser_tools import lifecycle
from browser_tools.one_shot import one_shot_page_session

# The pytest fixtures are re-exported under their own names (the explicit
# `x as x` alias is pyflakes' re-export form), so pytest resolves them from
# this module while lint stays clean without any waiver.
from login_fixtures import (
    LOGIN_PAGE,
    SENTINEL_PASS,
    SENTINEL_USER,
    SEPARATE_FORM_PAGE,
    SPA_PAGE,
    TWO_MARKERS_PAGE,
    _navigate_to,
    _relay_reads,
    _run_login,
    _write_fake_opchain,
    _write_policy,
)
from login_fixtures import disposable as disposable
from login_fixtures import fake_relay as fake_relay
from login_fixtures import site as site
from login_fixtures import spa as spa


class TestRefusalDiagnostics:
    @pytest.mark.parametrize("phase", ["before-fill", "constraint-validation"])
    def test_refusal_does_not_claim_an_unperformed_action(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd, phase
    ):
        import contextlib

        from browser_tools import login

        original_page = site.page
        real_session = login.one_shot_page_session
        real_read = login._read_credentials
        observed = {}

        @contextlib.asynccontextmanager
        async def inspect_session(*args):
            async with real_session(*args) as (cdp, session_id):
                async def read_then_disable(*read_args):
                    pair = await real_read(*read_args)
                    await cdp.send(
                        method="Runtime.evaluate", session_id=session_id,
                        params={"expression": "document.querySelector('#username').disabled = true"},
                    )
                    return pair

                if phase == "before-fill":
                    monkeypatch.setattr(login, "_read_credentials", read_then_disable)
                try:
                    yield cdp, session_id
                finally:
                    result = await cdp.send(
                        method="Runtime.evaluate", session_id=session_id,
                        params={
                            "expression": "document.querySelector('#username').value === '' && "
                                          "document.querySelector('#password').value === ''",
                            "returnByValue": True,
                        },
                    )
                    observed["empty"] = result["result"]["value"]

        try:
            site.page = LOGIN_PAGE
            if phase == "constraint-validation":
                site.page = LOGIN_PAGE.replace('name="u"', 'name="u" pattern="no-match"')
            _navigate_to(site, disposable)
            site.posts.clear()
            policy = _write_policy(tmp_path, site.origin)
            monkeypatch.setattr(login, "one_shot_page_session", inspect_session)
            assert _run_login(disposable, policy, monkeypatch) == 1
            output = capfd.readouterr()
            if phase == "before-fill":
                assert "disabled" in output.err
                assert "may still hold" not in output.err
            else:
                assert "validation" in output.err and "prevented submission" in output.err
            assert observed["empty"] is True
            assert not site.posts
            assert _relay_reads(fake_relay) == ["username", "password"]
            assert SENTINEL_USER not in output.out + output.err
            assert SENTINEL_PASS not in output.out + output.err
        finally:
            site.page = original_page


class TestTheWholeFlow:
    @pytest.mark.parametrize("case", ["several-tabs", "missing-target"])
    def test_target_selection_errors_never_print_browser_metadata(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd, case
    ):
        import asyncio


        port = lifecycle.resolve_cdp_port(disposable.name, registry_path=disposable.registry)
        targets = {}

        async def create_extra():
            async with one_shot_page_session(port, None, None) as (cdp, session_id):
                current = await cdp.send("Target.getTargetInfo", {}, session_id=session_id)
                targets["original"] = current["targetInfo"]["targetId"]
                extra = await cdp.send(
                    "Target.createTarget", {"url": "about:blank#SENTINEL-browser-url"},
                )
                targets["extra"] = extra["targetId"]

        async def close_extra():
            async with one_shot_page_session(port, targets["original"], "id") as (cdp, _):
                await cdp.send("Target.closeTarget", {"targetId": targets["extra"]})

        try:
            if case == "several-tabs":
                asyncio.run(create_extra())
            site.posts.clear()
            policy = _write_policy(tmp_path, site.origin)
            target = "SENTINEL-missing-target" if case == "missing-target" else None
            assert _run_login(disposable, policy, monkeypatch, target=target) == 1
            captured = capfd.readouterr()
            assert "--target" in captured.err
            assert "SENTINEL" not in captured.out + captured.err
            assert "Traceback" not in captured.err
            assert captured.out == ""
            assert not _relay_reads(fake_relay)
            assert not site.posts
        finally:
            if "extra" in targets:
                asyncio.run(close_extra())

    @pytest.mark.parametrize("dialog", ["alert", "prompt"])
    def test_synthetic_dialog_is_dismissed_without_exposing_its_contents(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd, dialog
    ):
        original_page = site.page
        expression = (
            "alert(event.target.value) === undefined" if dialog == "alert"
            else "prompt(document.querySelector('#username').value, event.target.value) === null"
        )
        try:
            site.page = LOGIN_PAGE + "<script>" + (
                "document.querySelector('#password').addEventListener('input', function(event) {"
                f"window.dialogDismissed = {expression};"
                "});"
                "document.querySelector('form').addEventListener('submit', function(event) {"
                "if (window.dialogDismissed !== true) event.preventDefault();"
                "});"
            ) + "</script>"
            _navigate_to(site, disposable)
            site.posts.clear()
            policy = _write_policy(tmp_path, site.origin)
            assert _run_login(disposable, policy, monkeypatch) == 0
            assert site.posts == [{"u": SENTINEL_USER, "p": SENTINEL_PASS}]
            captured = capfd.readouterr()
            assert json.loads(captured.out)["status"] == "authenticated"
            assert SENTINEL_USER not in captured.out + captured.err
            assert SENTINEL_PASS not in captured.out + captured.err
        finally:
            site.page = original_page

    @pytest.fixture(autouse=True)
    def back_on_the_login_page(self, site, disposable):
        """Each test starts from the form; the last one navigated away."""
        _navigate_to(site, disposable)

    def test_a_valid_form_fills_submits_once_and_authenticates(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd
    ):
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin)
        assert _run_login(disposable, policy, monkeypatch) == 0
        captured = capfd.readouterr()
        assert json.loads(captured.out) == {"account": "example", "status": "authenticated"}
        # Exactly one POST, carrying both synthetic values exactly once.
        assert site.posts == [{"u": SENTINEL_USER, "p": SENTINEL_PASS}]
        assert "SENTINEL" not in captured.err

    def test_wrong_origin_refuses_without_submitting(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd
    ):
        site.posts.clear()
        policy = _write_policy(tmp_path, "https://127.0.0.1:1")
        assert _run_login(disposable, policy, monkeypatch) == 1
        err = capfd.readouterr().err
        assert "configured origin" in err
        assert site.posts == []
        assert "SENTINEL" not in err

    def test_success_selector_already_matching_refuses(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd
    ):
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin, success_selector="form")
        assert _run_login(disposable, policy, monkeypatch) == 1
        assert "already holds" in capfd.readouterr().err
        assert site.posts == []

    def test_two_preexisting_success_markers_refuse_before_any_read(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd
    ):
        """Zero matches required, not exactly-one absence: a page already
        signed in with the marker repeated must refuse before the relay."""
        monkeypatch.setattr(site, "page", TWO_MARKERS_PAGE)
        _navigate_to(site, disposable)
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin)
        assert _run_login(disposable, policy, monkeypatch) == 1
        captured = capfd.readouterr()
        assert "already holds" in captured.err
        assert _relay_reads(fake_relay) == []
        assert site.posts == []
        assert "SENTINEL" not in captured.out + captured.err

    def test_password_in_a_separate_form_refuses_before_any_read(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd
    ):
        """Username and submit share one form, the password sits in another.

        Everything else about both forms is valid, so only the common-form
        check refuses; it must do so before any credential is read or sent.
        """
        monkeypatch.setattr(site, "page", SEPARATE_FORM_PAGE)
        _navigate_to(site, disposable)
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin)
        assert _run_login(disposable, policy, monkeypatch) == 1
        captured = capfd.readouterr()
        assert "same connected form" in captured.err
        assert _relay_reads(fake_relay) == []
        assert site.posts == []
        assert "SENTINEL" not in captured.out + captured.err

    def test_ambiguous_username_selector_refuses(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd
    ):
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin, username_selector="input")
        assert _run_login(disposable, policy, monkeypatch) == 1
        assert "exactly one" in capfd.readouterr().err
        assert site.posts == []

    @pytest.mark.parametrize("override", [False, True])
    def test_invalid_post_method_does_not_send_credentials_in_a_get(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd, override
    ):
        if override:
            page = LOGIN_PAGE.replace('id="go"', 'id="go" formmethod=" post "')
        else:
            page = LOGIN_PAGE.replace('method="post"', 'method=" post "')
        monkeypatch.setattr(site, "page", page)
        _navigate_to(site, disposable)
        site.posts.clear()
        before = len(site.gets)
        policy = _write_policy(tmp_path, site.origin)
        assert _run_login(disposable, policy, monkeypatch, timeout_ms=1000) == 1
        assert "POST" in capfd.readouterr().err
        assert site.posts == []
        assert not any("?" in path for path in site.gets[before:])

    @pytest.mark.parametrize("change", ["hidden-user", "hidden-password", "hidden-submit", "disabled-fieldset"])
    def test_unusable_fields_are_refused(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd, change
    ):
        page = LOGIN_PAGE
        if change == "disabled-fieldset":
            page = page.replace('<input id="username"', '<fieldset disabled><input id="username"').replace('</form>', '</fieldset></form>')
        else:
            identifier = {"hidden-user": "username", "hidden-password": "password", "hidden-submit": "go"}[change]
            page = page.replace(f'id="{identifier}"', f'id="{identifier}" style="display:none"')
        monkeypatch.setattr(site, "page", page)
        _navigate_to(site, disposable)
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin)
        assert _run_login(disposable, policy, monkeypatch, timeout_ms=1000) == 1
        assert "disabled" in capfd.readouterr().err
        assert site.posts == []


class TestClearingOnASinglePageForm:
    """The SPA shape: no navigation, so the live fields must be cleared."""

    @pytest.fixture(autouse=True)
    def fresh_spa(self, spa):
        spa.server.page = SPA_PAGE
        _navigate_to(spa.server, spa)

    @pytest.mark.parametrize("case", ["no-success", "replaced-fields"])
    def test_late_values_are_cleared_or_reported_on_every_exit(
        self, spa, fake_relay, tmp_path, monkeypatch, capfd, case
    ):
        original_page = spa.server.page
        page = SPA_PAGE
        if case == "no-success":
            page = page.replace("marker.setAttribute('data-signed-in', '');", "")
        else:
            form = "<form" + LOGIN_PAGE.split("<form", 1)[1].split("</form>", 1)[0] + "</form>"
            page = page.replace(
                "window.stateAfterResponse =",
                f"document.querySelector('form').outerHTML = {json.dumps(form)};"
                "window.stateAfterResponse =",
            )
        try:
            spa.server.page = page
            _navigate_to(spa.server, spa)
            spa.server.posts.clear()
            policy = _write_policy(tmp_path, spa.server.origin)
            assert _run_login(spa, policy, monkeypatch, timeout_ms=1000) == 1
            captured = capfd.readouterr()
            assert spa.server.posts == [{"u": SENTINEL_USER, "p": SENTINEL_PASS}], (
                "no synthetic POST was recorded; the login's stderr was: "
                f"{captured.err!r}"
            )
            state = self._page_state(spa)
            if case == "no-success":
                assert state["u"] == "" and state["p"] == ""
                assert "success condition" in captured.err
            else:
                assert "may still hold" in captured.err
            assert SENTINEL_USER not in captured.out + captured.err
            assert SENTINEL_PASS not in captured.out + captured.err
        finally:
            spa.server.page = original_page

    def _page_state(self, spa) -> dict[str, object]:
        import asyncio


        async def read() -> dict[str, object]:
            port = lifecycle.resolve_cdp_port(spa.name, registry_path=spa.registry)
            async with one_shot_page_session(port, None, None) as (cdp, session_id):
                result = await cdp.send(
                    method="Runtime.evaluate",
                    params={
                        "expression": "JSON.stringify({"
                                     "u: document.getElementById('username').value,"
                                     " p: document.getElementById('password').value,"
                                     " cleanAtYield: window.submitClearedBeforeYield === true,"
                                     " stateAfterResponse: window.stateAfterResponse || null})",
                        "returnByValue": True,
                    },
                    session_id=session_id,
                )
                return json.loads(result["result"]["value"])

        return asyncio.run(read())

    def test_fields_clear_without_clobbering_framework_state(
        self, spa, fake_relay, tmp_path, monkeypatch, capfd
    ):
        """The SPA shape: an async handler POSTs from framework-like state.

        The fixture's state is maintained from the fill's input events, its
        handler awaits a real response before adding the success marker, and
        it re-renders both fields from state once the response lands. The
        login must: send the pair (from state, not the cleared DOM), clear
        both fields before the page's first microtask and before returning,
        keep the framework state intact while the handler is awaiting, and
        clear again after the response re-rendered the fields.
        """
        from browser_tools import cli

        spa.server.posts.clear()
        monkeypatch.setenv("BROWSER_TOOLS_REGISTRY", spa.registry)
        policy = _write_policy(tmp_path, spa.server.origin)
        assert cli.main(
            ["login", spa.name, "--account", "example",
             "--config", str(policy), "--timeout-ms", "20000"]
        ) == 0
        captured = capfd.readouterr()
        assert json.loads(captured.out) == {"account": "example", "status": "authenticated"}
        # The submission carried the synthetic pair, read from the page's
        # own framework-like state after an await, exactly once.
        assert spa.server.posts == [{"u": SENTINEL_USER, "p": SENTINEL_PASS}]
        # Fields were empty at the first microtask after submit...
        state = self._page_state(spa)
        assert state["cleanAtYield"] is True
        # ...the handler's state still held the pair when the response
        # landed (clearing did not overwrite it)...
        assert state["stateAfterResponse"] == {"u": SENTINEL_USER, "p": SENTINEL_PASS}
        # ...and the fields are empty again after the response's re-render:
        # the final cleanup confirmation cleared the restored values.
        assert state["u"] == "" and state["p"] == ""
        assert "SENTINEL" not in captured.err


class TestPositiveOwnership:
    """The reproduced round-two defect, as a regression.

    A stale registry row (nonexistent PID, erased start token, swapped
    directory) whose port still serves a real browser must be refused
    before opchain is asked for anything; the restored row must log in
    normally against the same positively owned browser. The instance
    itself is unnamed (entry.profile is None): ownership is the physical
    process/directory binding, not a profile label.
    """

    @pytest.mark.parametrize("bad_dir", [None, 5, ["/recorded"], ""])
    def test_a_malformed_recorded_directory_refuses_before_any_read(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd, bad_dir
    ):
        """A registry row whose user-data directory is not a usable string
        is a missing-evidence refusal, never a crash or a credential read."""
        registry_path = Path(disposable.registry)
        original = registry_path.read_bytes()
        data = json.loads(original)
        data[disposable.name]["user_data_dir"] = bad_dir
        registry_path.write_text(json.dumps(data))
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin)
        try:
            assert _run_login(disposable, policy, monkeypatch) == 1
            captured = capfd.readouterr()
            assert "no usable user-data directory" in captured.err
            assert _relay_reads(fake_relay) == []
            assert site.posts == []
            assert "SENTINEL" not in captured.out + captured.err
        finally:
            registry_path.write_bytes(original)

    def test_a_stale_row_is_refused_before_any_read(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd
    ):
        registry_path = Path(disposable.registry)
        original = registry_path.read_bytes()
        data = json.loads(original)
        data[disposable.name]["pid"] = 2_000_000_000
        data[disposable.name]["pid_start"] = None
        data[disposable.name]["user_data_dir"] = str(tmp_path / "different-crashed-profile")
        registry_path.write_text(json.dumps(data))
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin)
        try:
            assert _run_login(disposable, policy, monkeypatch) == 1
            captured = capfd.readouterr()
            assert "registry row" in captured.err
            assert _relay_reads(fake_relay) == []
            assert site.posts == []
            assert "SENTINEL" not in captured.out + captured.err
        finally:
            registry_path.write_bytes(original)
        # Positive owned Chrome: the untouched row logs in end to end,
        # through the real process identity and the connected browser reply.
        _navigate_to(site, disposable)
        site.posts.clear()
        assert _run_login(disposable, policy, monkeypatch) == 0
        assert site.posts == [{"u": SENTINEL_USER, "p": SENTINEL_PASS}]


class TestFormSemantics:
    """Actual browser form semantics: actions, input types, usability."""

    def _run_case(self, site, disposable, monkeypatch, tmp_path, page):
        monkeypatch.setattr(site, "page", page)
        _navigate_to(site, disposable)
        site.posts.clear()
        policy = _write_policy(tmp_path, site.origin)
        return policy

    @pytest.mark.parametrize("case", [
        "omitted", "empty", "empty-override", "html-space", "html-space-override",
    ])
    def test_an_empty_action_submits_to_the_document_url_not_the_base(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd, case
    ):
        """The reproduced empty-action defect, with a cross-origin <base>."""
        original = site.page
        inputs = (
            '<input id="username" type="text" name="u">'
            '<input id="password" type="password" name="p">'
        )
        empty_action = "\t\n\f\r " if case.startswith("html-space") else ""
        if case == "omitted":
            form = f'<form method="post">{inputs}<button type="submit" id="go">Sign in</button></form>'
        elif case in {"empty", "html-space"}:
            form = f'<form method="post" action="{empty_action}">{inputs}<button type="submit" id="go">Sign in</button></form>'
        else:
            form = (
                '<form method="post" action="/login">' + inputs +
                f'<button type="submit" id="go" formaction="{empty_action}">Sign in</button></form>'
            )
        page = '<!doctype html><base href="https://other.invalid/dir/">' + form
        try:
            policy = self._run_case(site, disposable, monkeypatch, tmp_path, page)
            assert _run_login(disposable, policy, monkeypatch) == 0
            captured = capfd.readouterr()
            assert site.posts == [{"u": SENTINEL_USER, "p": SENTINEL_PASS}]
            assert "SENTINEL" not in captured.err
        finally:
            site.page = original

    @pytest.mark.parametrize("action", [
        "https://wrong.invalid/session", "\u00a0", "\u2003", "\ufeff", "\u000b",
    ])
    @pytest.mark.parametrize("override", [False, True])
    def test_a_cross_origin_action_refuses_before_any_read(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd, action, override
    ):
        original = site.page
        form_action = "/login" if override else action
        button_action = f' formaction="{action}"' if override else ""
        page = '<!doctype html><meta charset="utf-8"><base href="https://other.invalid/dir/">' + (
            f'<form method="post" action="{form_action}">'
            '<input id="username" type="text" name="u">'
            '<input id="password" type="password" name="p">'
            f'<button type="submit" id="go"{button_action}>Sign in</button></form>'
        )
        try:
            policy = self._run_case(site, disposable, monkeypatch, tmp_path, page)
            assert _run_login(disposable, policy, monkeypatch, timeout_ms=1000) == 1
            captured = capfd.readouterr()
            assert "somewhere other than" in captured.err
            assert _relay_reads(fake_relay) == []
            assert site.posts == []
            assert "SENTINEL" not in captured.out + captured.err
        finally:
            site.page = original

    def test_an_email_username_authenticates_with_a_valid_synthetic_value(
        self, site, disposable, tmp_path, monkeypatch, capfd
    ):
        """The named proof for the round-two username-type mutant: a real
        DOM type=email field is accepted (with a value constraint
        validation accepts) and submits exactly once."""

        email_user = "synthetic-user@example.invalid"
        bin_dir = tmp_path / "email-bin"
        bin_dir.mkdir()
        _write_fake_opchain(bin_dir, username=email_user)
        monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
        original = site.page
        page = LOGIN_PAGE.replace('id="username" type="text"', 'id="username" type="email"')
        try:
            policy = self._run_case(site, disposable, monkeypatch, tmp_path, page)
            assert _run_login(disposable, policy, monkeypatch) == 0
            captured = capfd.readouterr()
            assert site.posts == [{"u": email_user, "p": SENTINEL_PASS}]
            assert "SENTINEL" not in captured.err
        finally:
            site.page = original

    @pytest.mark.parametrize("username_type", ["number", "search", "tel"])
    def test_non_text_username_types_refuse_before_any_read(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd, username_type
    ):
        """The other half of the named mutant proof: number/search/tel
        username inputs are refused with zero credential reads."""
        original = site.page
        page = LOGIN_PAGE.replace(
            'id="username" type="text"', f'id="username" type="{username_type}"'
        )
        try:
            policy = self._run_case(site, disposable, monkeypatch, tmp_path, page)
            assert _run_login(disposable, policy, monkeypatch, timeout_ms=1000) == 1
            captured = capfd.readouterr()
            assert "ordinary text or email" in captured.err
            assert _relay_reads(fake_relay) == []
            assert site.posts == []
            assert "SENTINEL" not in captured.out + captured.err
        finally:
            site.page = original

    @pytest.mark.parametrize("case", ["opacity", "ancestor-opacity", "zero-size"])
    def test_invisible_username_fields_refuse_before_any_read(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd, case
    ):
        """The reproduced hidden-username defect: opacity-zero and
        zero-size fields take credentials while being invisible."""
        original = site.page
        style = {
            "opacity": "opacity:0",
            "ancestor-opacity": None,
            "zero-size": "width:0;height:0;border:0;padding:0",
        }[case]
        if case == "ancestor-opacity":
            page = (
                '<!doctype html><form method="post" action="/login" '
                'style="opacity:0">' + LOGIN_PAGE.split("<form", 1)[1]
            )
        else:
            page = LOGIN_PAGE.replace(
                '<input id="username" type="text" name="u">',
                f'<input id="username" type="text" name="u" style="{style}">',
            )
        try:
            policy = self._run_case(site, disposable, monkeypatch, tmp_path, page)
            assert _run_login(disposable, policy, monkeypatch, timeout_ms=1000) == 1
            captured = capfd.readouterr()
            assert "disabled" in captured.err
            assert _relay_reads(fake_relay) == []
            assert site.posts == []
            assert "SENTINEL" not in captured.out + captured.err
        finally:
            site.page = original

    def test_a_below_fold_form_still_authenticates(
        self, site, disposable, fake_relay, tmp_path, monkeypatch, capfd
    ):
        """Offscreen placement alone is allowed: no viewport intersection
        requirement may refuse a valid below-fold form."""
        original = site.page
        page = LOGIN_PAGE.replace(
            "<form", '<div style="height:4000px"></div><form', 1
        )
        try:
            policy = self._run_case(site, disposable, monkeypatch, tmp_path, page)
            assert _run_login(disposable, policy, monkeypatch) == 0
            assert site.posts == [{"u": SENTINEL_USER, "p": SENTINEL_PASS}]
        finally:
            site.page = original


def _helper_entry_calls(source: str) -> set[str]:
    """Entry points the Python caller actually calls, derived from the AST.

    Only evaluation-call string arguments count: entry points are called
    as ``__btLogin.<name>(`` inside string literals (plain or the literal
    parts of f-strings) passed as call arguments. Comments never parse and
    docstrings are not call arguments, so a seeded mention of a dead
    export in either cannot keep it alive (round-three B6).
    """

    def literal_strings(node: object) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.JoinedStr):
            return [
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            ]
        return []

    uses: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_eval_login_script"
        ):
            continue
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        for argument in arguments:
            for text in literal_strings(argument):
                uses.update(
                    name for name in re.findall(r"__btLogin\.(\w+)\s*\(", text)
                )
    return uses


class TestBridgeContract:
    def test_a_seeded_comment_or_docstring_mention_is_not_a_use(self):
        """The repeat guard reads actual call strings, never prose.

        Seeding both a comment and a docstring mention of a dead entry
        point into the caller's source must not count it as a use, nor
        may a diagnostic string; only the evaluated ``begin`` call does.
        """
        from browser_tools import login as login_module

        caller = Path(login_module.__file__).read_text(encoding="utf-8")
        assert _helper_entry_calls(caller) == {"begin"}, (
            "the caller's real entry-point calls changed; update this proof "
            "and the rule table together"
        )
        seeded = caller.replace(
            '"""',
            '"""Seeded mention: __btLogin.probe(policy) is not a use.',
            1,
        ).replace(
            "def _policy_json_for_page(",
            "# __btLogin.probe(\ndef _policy_json_for_page(",
            1,
        )
        seeded += '\nlogger.debug("__btLogin.probe(")\n'
        assert _helper_entry_calls(seeded) == {"begin"}, (
            "a seeded prose or diagnostic mention passed as a helper use"
        )

    def test_helper_exports_match_the_python_caller(self, disposable):
        """One enforcing check for the repeat dead-export mistake.

        The helper's exported entry points must be exactly the entry points
        the Python caller still uses (derived from login.py's own AST, so
        only actual ``__btLogin.<name>(`` call strings count), so an export
        left behind by a rewrite - like the obsolete success probe - fails
        here, named, with the instruction to remove it.
        """
        from browser_tools import login as login_module

        caller = Path(login_module.__file__).read_text(encoding="utf-8")
        used = sorted(_helper_entry_calls(caller))
        assert "begin" in used, "the Python caller must use the form entry point"

        async def exports_of_helper() -> list[str]:
            port = lifecycle.resolve_cdp_port(
                disposable.name, registry_path=disposable.registry
            )
            async with one_shot_page_session(port, None, None) as (cdp, session_id):
                tree = await cdp.send(
                    "Page.getFrameTree", {}, session_id=session_id, timeout=5.0
                )
                frame_id = tree["frameTree"]["frame"]["id"]
                world = await cdp.send(
                    "Page.createIsolatedWorld",
                    {"frameId": frame_id, "worldName": "bridge-contract",
                     "grantUniveralAccess": False},
                    session_id=session_id, timeout=5.0,
                )
                reply = await cdp.send(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            login_module._login_script_source()
                            + "\n;Object.keys(globalThis.__btLogin).sort()"
                        ),
                        "contextId": world["executionContextId"],
                        "returnByValue": True,
                    },
                    session_id=session_id, timeout=5.0,
                )
                return reply["result"]["value"]

        exports = asyncio.run(exports_of_helper())
        unused = sorted(set(exports) - set(used))
        assert not unused, (
            f"login_form.js exports {unused}, which the Python caller no longer "
            "uses; remove the dead entry point(s) and their contract comment"
        )
        missing = sorted(set(used) - set(exports))
        assert not missing, (
            f"the Python caller uses {missing}, which the helper no longer exports"
        )
