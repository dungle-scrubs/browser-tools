"""Unit tests for the normative parity comparison operator.

These prove the operator on synthetic captures - no browser involved:

- node sets compare order-insensitively;
- a role / name / value mismatch is caught;
- a UID-target difference is caught;
- a text difference is caught.
"""

from __future__ import annotations

import json

from parity_comparison import (
    PageCapture,
    ParityResult,
    SnapshotNode,
    capture_from_dict,
    capture_to_dict,
    compare_captures,
    compare_corpus,
    compare_superset,
    corpus_covers,
    corpus_from_dict,
    corpus_matches,
    corpus_to_dict,
)
from parity_engines import parse_node_snapshot


def _capture(page_id="p", nodes=(), uid_targets=None, text="") -> PageCapture:
    return PageCapture(
        page_id=page_id,
        url=f"file:///{page_id}.html",
        nodes=tuple(nodes),
        uid_targets=dict(uid_targets or {}),
        text=text,
    )


def _base_nodes() -> tuple[SnapshotNode, ...]:
    return (
        SnapshotNode(role="heading", name="Title", value=None),
        SnapshotNode(role="textbox", name="Email", value="a@b.com"),
        SnapshotNode(role="button", name="Submit", value=None),
    )


# --------------------------------------------------------------------------- #
# Order-insensitivity
# --------------------------------------------------------------------------- #


def test_node_set_is_order_insensitive():
    """A shuffled node set with identical tuples matches."""
    base = _capture(nodes=_base_nodes())
    shuffled = _capture(nodes=tuple(reversed(_base_nodes())))
    result = compare_captures(base, shuffled)
    assert result.matched
    assert result.diffs == ()


def test_duplicate_nodes_are_a_multiset_not_a_set():
    """Two identical nodes are not the same as one - multiplicity matters."""
    two = _capture(nodes=(SnapshotNode("button", "Go"), SnapshotNode("button", "Go")))
    one = _capture(nodes=(SnapshotNode("button", "Go"),))
    result = compare_captures(two, one)
    assert not result.matched
    assert any(d.dimension == "node_set" for d in result.diffs)


# --------------------------------------------------------------------------- #
# Role / name / value mismatches
# --------------------------------------------------------------------------- #


def test_role_mismatch_is_caught():
    base = _capture(nodes=(SnapshotNode("button", "Submit"),))
    cand = _capture(nodes=(SnapshotNode("link", "Submit"),))
    result = compare_captures(base, cand)
    assert not result.matched
    assert all(d.dimension == "node_set" for d in result.diffs)


def test_name_mismatch_is_caught():
    base = _capture(nodes=(SnapshotNode("button", "Submit"),))
    cand = _capture(nodes=(SnapshotNode("button", "Send"),))
    result = compare_captures(base, cand)
    assert not result.matched
    assert any("Submit" in d.detail for d in result.diffs)
    assert any("Send" in d.detail for d in result.diffs)


def test_value_mismatch_is_caught():
    base = _capture(nodes=(SnapshotNode("textbox", "Email", "a@b.com"),))
    cand = _capture(nodes=(SnapshotNode("textbox", "Email", "x@y.com"),))
    result = compare_captures(base, cand)
    assert not result.matched
    assert all(d.dimension == "node_set" for d in result.diffs)


def test_value_is_part_of_the_key_not_ignored():
    """None value and empty-string value are distinct tuples."""
    base = _capture(nodes=(SnapshotNode("textbox", "Email", None),))
    cand = _capture(nodes=(SnapshotNode("textbox", "Email", ""),))
    assert not compare_captures(base, cand).matched


def test_uid_and_backend_node_do_not_affect_node_set_equality():
    """Different UID strings / backend nodes still match on (role, name, value)."""
    base = _capture(nodes=(SnapshotNode("button", "Go", None, uid="1", backend_node="A"),))
    cand = _capture(nodes=(SnapshotNode("button", "Go", None, uid="9", backend_node="Z"),))
    assert compare_captures(base, cand).matched


# --------------------------------------------------------------------------- #
# UID-target resolution
# --------------------------------------------------------------------------- #


def test_uid_target_difference_is_caught():
    """Same node set, but a UID resolves to a different backend node."""
    nodes = (SnapshotNode("button", "Go"),)
    base = _capture(nodes=nodes, uid_targets={"#go": "html>body>button#go"})
    cand = _capture(nodes=nodes, uid_targets={"#go": "html>body>div>button#go"})
    result = compare_captures(base, cand)
    assert not result.matched
    assert [d.dimension for d in result.diffs] == ["uid_target"]


def test_uid_missing_in_candidate_is_caught():
    base = _capture(uid_targets={"#go": "path-a"})
    cand = _capture(uid_targets={})
    result = compare_captures(base, cand)
    assert not result.matched
    assert result.diffs[0].dimension == "uid_target"


def test_uid_extra_in_candidate_is_caught():
    base = _capture(uid_targets={})
    cand = _capture(uid_targets={"#go": "path-a"})
    result = compare_captures(base, cand)
    assert not result.matched
    assert result.diffs[0].dimension == "uid_target"


def test_matching_uid_targets_pass():
    targets = {"#go": "path-a", "#stop": "path-b"}
    assert compare_captures(_capture(uid_targets=targets), _capture(uid_targets=dict(targets))).matched


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #


def test_text_difference_is_caught():
    base = _capture(text="Hello world")
    cand = _capture(text="Hello  world")
    result = compare_captures(base, cand)
    assert not result.matched
    assert [d.dimension for d in result.diffs] == ["text"]


def test_text_compared_exactly_including_whitespace():
    assert not compare_captures(_capture(text="a\nb"), _capture(text="a b")).matched
    assert compare_captures(_capture(text="a\nb"), _capture(text="a\nb")).matched


# --------------------------------------------------------------------------- #
# All dimensions together
# --------------------------------------------------------------------------- #


def test_full_match_across_all_dimensions():
    base = _capture(
        nodes=_base_nodes(),
        uid_targets={"#email": "html>body>form#f>input#email"},
        text="Title\nEmail\nSubmit",
    )
    cand = _capture(
        nodes=tuple(reversed(_base_nodes())),
        uid_targets={"#email": "html>body>form#f>input#email"},
        text="Title\nEmail\nSubmit",
    )
    assert compare_captures(base, cand).matched


def test_one_diff_per_broken_dimension():
    base = _capture(
        nodes=(SnapshotNode("button", "Go"),),
        uid_targets={"#go": "a"},
        text="one",
    )
    cand = _capture(
        nodes=(SnapshotNode("link", "Go"),),
        uid_targets={"#go": "b"},
        text="two",
    )
    dims = {d.dimension for d in compare_captures(base, cand).diffs}
    assert dims == {"node_set", "uid_target", "text"}


# --------------------------------------------------------------------------- #
# Corpus-level comparison
# --------------------------------------------------------------------------- #


def test_compare_corpus_matches_when_all_pages_match():
    base = {"a": _capture("a", text="x"), "b": _capture("b", text="y")}
    cand = {"a": _capture("a", text="x"), "b": _capture("b", text="y")}
    results = compare_corpus(base, cand)
    assert corpus_matches(results)


def test_compare_corpus_flags_a_page_missing_from_candidate():
    base = {"a": _capture("a"), "b": _capture("b")}
    cand = {"a": _capture("a")}
    results = compare_corpus(base, cand)
    assert not corpus_matches(results)
    assert not results["b"].matched


def test_compare_corpus_flags_a_page_missing_from_baseline():
    base = {"a": _capture("a")}
    cand = {"a": _capture("a"), "b": _capture("b")}
    results = compare_corpus(base, cand)
    assert not corpus_matches(results)
    assert not results["b"].matched


def test_parity_result_matched_property():
    assert ParityResult("p").matched
    assert not ParityResult("p", (compare_captures(_capture(text="x"), _capture(text="y")).diffs[0],)).matched


# --------------------------------------------------------------------------- #
# Serialization round-trip (a baseline persists as JSON between engine runs)
# --------------------------------------------------------------------------- #


def test_capture_json_round_trip_preserves_equality():
    original = _capture(
        nodes=_base_nodes(),
        uid_targets={"#email": "path-a"},
        text="Title\nEmail\nSubmit",
    )
    restored = capture_from_dict(json.loads(json.dumps(capture_to_dict(original))))
    # A round-tripped baseline must still compare equal to a fresh identical run.
    assert compare_captures(original, restored).matched
    assert restored.nodes == original.nodes
    assert restored.uid_targets == original.uid_targets
    assert restored.text == original.text


def test_corpus_json_round_trip():
    corpus = {
        "plain": _capture("plain", nodes=(SnapshotNode("heading", "Hi"),), text="Hi"),
        "form": _capture("form", uid_targets={"#go": "path"}, text="form"),
    }
    restored = corpus_from_dict(json.loads(json.dumps(corpus_to_dict(corpus))))
    assert corpus_matches(compare_corpus(corpus, restored))


# --------------------------------------------------------------------------- #
# Superset gate (ticket #41): candidate must COVER baseline, may report more.
# --------------------------------------------------------------------------- #


def test_superset_passes_when_candidate_adds_nodes():
    """Native's extra AX-internal detail is allowed: candidate ⊇ baseline matches."""
    base = _capture(nodes=(SnapshotNode("button", "Go"),))
    cand = _capture(
        nodes=(
            SnapshotNode("button", "Go"),
            SnapshotNode("InlineTextBox", "Go"),  # extra detail native surfaces
            SnapshotNode("generic", ""),
        )
    )
    assert compare_superset(base, cand).matched


def test_superset_fails_when_candidate_misses_a_baseline_node():
    base = _capture(nodes=(SnapshotNode("button", "Go"), SnapshotNode("heading", "Hi")))
    cand = _capture(nodes=(SnapshotNode("button", "Go"),))
    result = compare_superset(base, cand)
    assert not result.matched
    assert any("not covered" in d.detail and "heading" in d.detail for d in result.diffs)


def test_superset_respects_multiplicity():
    """Two baseline buttons need two in the candidate; one is under-coverage."""
    base = _capture(nodes=(SnapshotNode("button", "Go"), SnapshotNode("button", "Go")))
    cand = _capture(nodes=(SnapshotNode("button", "Go"),))
    assert not compare_superset(base, cand).matched


def test_superset_allows_candidate_only_uid_but_requires_baseline_uids():
    """Native may resolve extra UIDs (shadow/iframe), but must cover baseline UIDs."""
    base = _capture(uid_targets={"#a": "path-a"})
    cand = _capture(uid_targets={"#a": "path-a", "#framed": "path-framed"})
    assert compare_superset(base, cand).matched  # extra #framed is a superset, ok

    missing = compare_superset(_capture(uid_targets={"#a": "path-a"}), _capture(uid_targets={}))
    assert not missing.matched
    assert missing.diffs[0].dimension == "uid_target"


def test_superset_fails_on_diverging_uid_target():
    base = _capture(uid_targets={"#a": "path-a"})
    cand = _capture(uid_targets={"#a": "path-b"})
    result = compare_superset(base, cand)
    assert not result.matched
    assert result.diffs[0].dimension == "uid_target"


def test_superset_text_is_still_exact():
    assert not compare_superset(_capture(text="a"), _capture(text="a ")).matched


def test_corpus_covers_flags_missing_page():
    base = {"a": _capture("a"), "b": _capture("b")}
    cand = {"a": _capture("a")}
    results = corpus_covers(base, cand)
    assert not corpus_matches(results)
    assert not results["b"].matched


# --------------------------------------------------------------------------- #
# chrome-devtools-mcp snapshot parser (Node baseline node set)
# --------------------------------------------------------------------------- #


def test_parse_node_snapshot_extracts_role_name_value():
    text = (
        "## Latest page snapshot\n"
        'uid=1_0 RootWebArea "Parity Form Page" url="file:///x"\n'
        '  uid=1_1 heading "Parity Form Page" level="1"\n'
        '  uid=1_4 textbox "Email" value="a@b.com"\n'
        '  uid=1_8 button "Submit"\n'
    )
    nodes = parse_node_snapshot(text)
    keys = [(n.role, n.name, n.value) for n in nodes]
    assert ("RootWebArea", "Parity Form Page", None) in keys
    assert ("heading", "Parity Form Page", None) in keys
    assert ("textbox", "Email", "a@b.com") in keys
    assert ("button", "Submit", None) in keys


def test_parse_node_snapshot_skips_non_node_lines():
    text = "## Latest page snapshot\nsome prose without a uid token\n"
    assert parse_node_snapshot(text) == []


def test_parse_node_snapshot_handles_unnamed_node():
    nodes = parse_node_snapshot("uid=2_3 generic\n")
    assert nodes == [SnapshotNode("generic", "", None)]
