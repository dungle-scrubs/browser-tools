"""Live parity test for the native UID *interaction* path (ticket #40).

Skips cleanly when no Playwright Chromium is available, so the default
``pytest`` run stays green offline. Run explicitly with:  uv run pytest -m parity

Three rungs are asserted here:

1. **Native UID resolution matches direct DOM resolution.** The
   :class:`~parity_engines.NativeInteractionEngine` resolves each interactive
   UID through the real interaction path (native UID -> backend node -> DOM
   element/path) and keys it by ``#id``. Against the ARIA baseline's JS-computed
   ``#id -> DOM path`` on the *same* page, the ``uid_target`` dimension MUST
   agree: a native UID resolves to the node a click/fill would act on.

2. **Flake-free across two runs.** The native interaction targets match across
   two consecutive captures of the frozen corpus (the RFC's flake-free property
   applied to the interaction candidate).

3. **Click / fill actually work on the frozen form page.** The production
   :class:`~browser_tools.native_interaction.NativeInteractor` drives real CDP
   over the live session: ``fill`` sets input values (with ``input``/``change``
   fired), ``click`` toggles a checkbox and reaches a button's click handler.

The authoritative chrome-devtools-mcp Node-vs-native gate is #41; this proves
the native interaction path resolves and dispatches correctly where a live
browser is reachable.
"""

from __future__ import annotations

import pytest
from live_chromium import PlaywrightChromiumSession, chromium_available
from parity_comparison import compare_corpus, corpus_matches
from parity_corpus import corpus_page
from parity_engines import AriaSnapshotEngine, NativeInteractionEngine, capture_corpus

from browser_tools.native_interaction import NativeInteractor, UidResolutionError
from browser_tools.native_snapshot import NativeSnapshot, NativeSnapshotReader

_AVAILABLE, _WHY = chromium_available()

pytestmark = [
    pytest.mark.parity,
    pytest.mark.skipif(not _AVAILABLE, reason=f"no live Chromium: {_WHY}"),
]


@pytest.fixture
def chromium_session():
    try:
        with PlaywrightChromiumSession() as session:
            session.navigate("about:blank")
            yield session
    except Exception as exc:  # missing browser binary, sandbox denial, etc.
        pytest.skip(f"could not launch a live Chromium: {exc}")


def _uid_for(snapshot: NativeSnapshot, role: str, name: str) -> str:
    for node in snapshot.visible_nodes():
        if node.role == role and node.name == name:
            return node.uid
    raise AssertionError(f"no visible {role!r} named {name!r} in snapshot")


def test_native_uid_resolution_matches_direct_dom(chromium_session):
    """Every UID the baseline resolves, the native path resolves identically.

    The baseline's ``#id -> DOM path`` comes from ``document.querySelectorAll``,
    which does not pierce shadow roots. The native path resolves through the
    accessibility tree, which does -- so on the shadow-DOM page it resolves a
    *superset* (the open shadow root's controls). That is correct native
    behaviour, and the true Node baseline (#41) pierces shadow DOM too. So the
    contract asserted here is: native never conflicts with, and never misses,
    a target the baseline resolved.
    """
    native = capture_corpus(NativeInteractionEngine(chromium_session))
    aria = capture_corpus(AriaSnapshotEngine(chromium_session))
    for page_id in native:
        base = aria[page_id].uid_targets
        cand = native[page_id].uid_targets
        for uid, base_path in base.items():
            assert uid in cand, f"{page_id}: native did not resolve {uid} (baseline -> {base_path})"
            assert cand[uid] == base_path, (
                f"{page_id}: native resolves {uid} -> {cand[uid]}, baseline -> {base_path}"
            )
    # The form page (light DOM) must agree exactly in both directions.
    assert native["form"].uid_targets == aria["form"].uid_targets
    assert native["form"].uid_targets, "no native UID targets resolved on the form page"
    # The shadow page is where the native path resolves controls the shadow-blind
    # baseline cannot: native is a strict superset there.
    assert set(aria["shadow"].uid_targets) < set(native["shadow"].uid_targets)


def test_native_interaction_targets_are_flake_free(chromium_session):
    engine = NativeInteractionEngine(chromium_session)
    first = capture_corpus(engine)
    second = capture_corpus(engine)
    results = compare_corpus(first, second)
    broken = {pid: [d.detail for d in r.diffs] for pid, r in results.items() if not r.matched}
    assert corpus_matches(results), f"native interaction not flake-free: {broken}"


def test_native_fill_and_click_on_form_page(chromium_session):
    """Drive the production NativeInteractor against the live form page."""
    form = corpus_page("form")
    reader = NativeSnapshotReader()
    chromium_session.navigate(form.file_url())
    reader.note_navigation()
    snapshot = reader.build(chromium_session.get_full_ax_tree())
    interactor = NativeInteractor(reader)
    send = chromium_session.cdp_send

    # fill: two text inputs get their value set, firing input/change.
    email_uid = _uid_for(snapshot, "textbox", "Email")
    name_uid = _uid_for(snapshot, "textbox", "Full name")
    fill_email = interactor.fill(send, email_uid, "agent@example.com")
    interactor.fill(send, name_uid, "Ada Lovelace")
    assert fill_email.value_after == "agent@example.com"
    assert chromium_session.evaluate("document.getElementById('email').value") == "agent@example.com"
    assert chromium_session.evaluate("document.getElementById('name').value") == "Ada Lovelace"

    # click: a trusted mouse click toggles the checkbox.
    assert chromium_session.evaluate("document.getElementById('subscribe').checked") is False
    subscribe_uid = _uid_for(snapshot, "checkbox", "Subscribe")
    interactor.click(send, subscribe_uid)
    assert chromium_session.evaluate("document.getElementById('subscribe').checked") is True

    # click: the trusted mouse click reaches a button's own click handler.
    chromium_session.evaluate(
        "document.getElementById('submit').addEventListener('click', (e) => "
        "{ e.preventDefault(); window.__submitClicked = true; })"
    )
    submit_uid = _uid_for(snapshot, "button", "Submit")
    click_submit = interactor.click(send, submit_uid)
    assert chromium_session.evaluate("window.__submitClicked") is True
    assert click_submit.point is not None  # a real viewport point was targeted


def test_stale_uid_is_refused_against_live_session(chromium_session):
    """A UID from a superseded snapshot resolves for neither engine (no CDP call)."""
    form = corpus_page("form")
    reader = NativeSnapshotReader()
    chromium_session.navigate(form.file_url())
    reader.note_navigation()
    reader.build(chromium_session.get_full_ax_tree())
    reader.build(chromium_session.get_full_ax_tree())  # supersede generation 1
    with pytest.raises(UidResolutionError):
        NativeInteractor(reader).click(chromium_session.cdp_send, "1-5")
