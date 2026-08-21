"""Unit tests for the frozen parity corpus.

These assert the corpus is present, reproducible offline, and covers the
structural cases the RFC requires - iframe and shadow DOM included.
"""

from __future__ import annotations

from parity_corpus import CORPUS, SUPPORT_FILES, corpus_page


def test_every_corpus_fixture_exists():
    for page in CORPUS:
        assert page.path().is_file(), f"missing fixture: {page.filename}"


def test_support_files_exist():
    from parity_corpus import FIXTURES_DIR

    for name in SUPPORT_FILES:
        assert (FIXTURES_DIR / name).is_file(), f"missing support file: {name}"


def test_corpus_covers_the_required_structural_cases():
    ids = {page.page_id for page in CORPUS}
    # The RFC names iframe and shadow DOM explicitly; the rest round out the set.
    assert {"plain", "form", "iframe", "shadow", "dynamic"} <= ids


def test_page_ids_are_unique():
    ids = [page.page_id for page in CORPUS]
    assert len(ids) == len(set(ids))


def test_fixtures_are_local_file_urls():
    for page in CORPUS:
        assert page.file_url().startswith("file://")


def test_corpus_page_lookup():
    assert corpus_page("iframe").filename == "iframe.html"


def test_corpus_page_lookup_unknown_raises():
    import pytest

    with pytest.raises(KeyError):
        corpus_page("does-not-exist")


def test_iframe_fixture_actually_embeds_a_frame():
    html = (corpus_page("iframe").path()).read_text(encoding="utf-8")
    assert "<iframe" in html


def test_shadow_fixture_actually_attaches_a_shadow_root():
    html = (corpus_page("shadow").path()).read_text(encoding="utf-8")
    assert "attachShadow" in html


def test_form_fixture_has_interactive_controls_with_ids():
    html = (corpus_page("form").path()).read_text(encoding="utf-8")
    assert 'id="email"' in html
    assert "<button" in html
