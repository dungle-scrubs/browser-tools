"""The normative parity comparison operator (RFC-01, Testing Strategy).

This module is the heart of the Phase 2 parity gate. It is engine-agnostic:
it compares two ``PageCapture`` values, whatever engine produced them, under
the operator the RFC fixes as normative:

- **Snapshot node sets** are compared order-insensitively on ``(role, name,
  value)`` tuples. Order in the tree does not matter; multiplicity does (two
  identical buttons are two nodes, not one), so the comparison is over a
  multiset of tuples.
- **UID resolution** is compared by the backend node a click or fill resolves
  to. A UID that resolves to a different backend node in the candidate run is
  a parity failure, even if the node set is identical.
- **Text extraction** is compared exactly. Any difference in extracted text is
  a failure.

The operator has no knowledge of Chrome, CDP, or Playwright. Engines (see
``parity_engines``) are responsible for producing ``PageCapture`` values;
Phase 2 plugs the native-snapshot engine in as the candidate against a
baseline captured from another engine and calls :func:`compare_captures`.

On backend-node identity: a raw CDP ``backendDOMNodeId`` is an opaque per
session integer and is not comparable across two browser sessions. A
``PageCapture`` therefore carries a *stable* identity for the backend node a
UID resolves to (for example a DOM path, or the element's stable test id),
normalized by the engine at capture time. The operator compares those stable
identities for equality; it never assumes two sessions share raw node ids.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

Dimension = Literal["node_set", "uid_target", "text"]


@dataclass(frozen=True)
class SnapshotNode:
    """One accessibility node in a snapshot.

    Only ``(role, name, value)`` participates in node-set equality. ``uid`` and
    ``backend_node`` are carried for the UID-resolution comparison and for
    debugging; they are deliberately excluded from the node-set key so that two
    engines that assign different UID strings can still match on structure.
    """

    role: str
    name: str
    value: str | None = None
    uid: str | None = None
    backend_node: str | None = None

    def key(self) -> tuple[str, str, str | None]:
        """The ``(role, name, value)`` tuple used for node-set comparison."""
        return (self.role, self.name, self.value)


@dataclass(frozen=True)
class PageCapture:
    """The result of running one corpus page through one engine.

    Attributes:
        page_id: Stable corpus page identifier (matches across engines).
        url: The URL that was captured (informational).
        nodes: The snapshot node set.
        uid_targets: Map of UID -> stable identity of the backend node that a
            click/fill on that UID resolves to.
        text: The exact text extracted from the page.
    """

    page_id: str
    url: str
    nodes: tuple[SnapshotNode, ...] = ()
    uid_targets: dict[str, str] = field(default_factory=dict)
    text: str = ""


@dataclass(frozen=True)
class ParityDiff:
    """A single difference the operator found, tagged with its dimension."""

    dimension: Dimension
    detail: str


@dataclass(frozen=True)
class ParityResult:
    """The outcome of comparing one page across two engines."""

    page_id: str
    diffs: tuple[ParityDiff, ...] = ()

    @property
    def matched(self) -> bool:
        """True when the candidate matched the baseline on every dimension."""
        return not self.diffs


def _node_set_diffs(
    baseline: tuple[SnapshotNode, ...],
    candidate: tuple[SnapshotNode, ...],
) -> list[ParityDiff]:
    """Order-insensitive multiset comparison on ``(role, name, value)``."""
    base_counts = Counter(node.key() for node in baseline)
    cand_counts = Counter(node.key() for node in candidate)
    if base_counts == cand_counts:
        return []

    diffs: list[ParityDiff] = []
    # Tuples present in the baseline but missing (or under-counted) in candidate.
    missing = base_counts - cand_counts
    for key, count in sorted(missing.items(), key=lambda kv: tuple(str(p) for p in kv[0])):
        diffs.append(
            ParityDiff(
                "node_set",
                f"missing from candidate x{count}: role={key[0]!r} name={key[1]!r} value={key[2]!r}",
            )
        )
    # Tuples present in the candidate but absent (or over-counted) in baseline.
    extra = cand_counts - base_counts
    for key, count in sorted(extra.items(), key=lambda kv: tuple(str(p) for p in kv[0])):
        diffs.append(
            ParityDiff(
                "node_set",
                f"unexpected in candidate x{count}: role={key[0]!r} name={key[1]!r} value={key[2]!r}",
            )
        )
    return diffs


def _uid_target_diffs(
    baseline: dict[str, str],
    candidate: dict[str, str],
) -> list[ParityDiff]:
    """Compare UID -> backend-node resolution, keyed by UID."""
    diffs: list[ParityDiff] = []
    for uid in sorted(set(baseline) | set(candidate)):
        base_target = baseline.get(uid)
        cand_target = candidate.get(uid)
        if base_target == cand_target:
            continue
        if base_target is None:
            diffs.append(
                ParityDiff("uid_target", f"uid {uid!r} resolves in candidate only -> {cand_target!r}")
            )
        elif cand_target is None:
            diffs.append(
                ParityDiff("uid_target", f"uid {uid!r} does not resolve in candidate (baseline -> {base_target!r})")
            )
        else:
            diffs.append(
                ParityDiff(
                    "uid_target",
                    f"uid {uid!r} resolves to {cand_target!r}, baseline resolves to {base_target!r}",
                )
            )
    return diffs


def _text_diffs(baseline: str, candidate: str) -> list[ParityDiff]:
    """Exact text comparison."""
    if baseline == candidate:
        return []
    return [ParityDiff("text", f"text differs: baseline {baseline!r} != candidate {candidate!r}")]


def compare_captures(baseline: PageCapture, candidate: PageCapture) -> ParityResult:
    """Apply the normative parity operator to one page.

    Args:
        baseline: The reference capture (for Phase 2, the Node engine).
        candidate: The capture under test (for Phase 2, the native engine).

    Returns:
        A :class:`ParityResult`; ``matched`` is True only when the node set,
        every UID resolution, and the extracted text all agree.
    """
    diffs: list[ParityDiff] = []
    diffs.extend(_node_set_diffs(baseline.nodes, candidate.nodes))
    diffs.extend(_uid_target_diffs(baseline.uid_targets, candidate.uid_targets))
    diffs.extend(_text_diffs(baseline.text, candidate.text))
    return ParityResult(page_id=baseline.page_id, diffs=tuple(diffs))


def compare_corpus(
    baseline: dict[str, PageCapture],
    candidate: dict[str, PageCapture],
) -> dict[str, ParityResult]:
    """Compare a whole captured corpus, page by page.

    A page present in only one run is itself a parity failure, reported as a
    synthetic diff so the gate never silently drops a page.
    """
    results: dict[str, ParityResult] = {}
    for page_id in sorted(set(baseline) | set(candidate)):
        if page_id not in candidate:
            results[page_id] = ParityResult(
                page_id, (ParityDiff("node_set", "page missing from candidate run"),)
            )
        elif page_id not in baseline:
            results[page_id] = ParityResult(
                page_id, (ParityDiff("node_set", "page missing from baseline run"),)
            )
        else:
            results[page_id] = compare_captures(baseline[page_id], candidate[page_id])
    return results


def corpus_matches(results: dict[str, ParityResult]) -> bool:
    """True when every page in the corpus matched. This is the gate predicate."""
    return all(result.matched for result in results.values())


# --------------------------------------------------------------------------- #
# Superset gate (RFC-01 Phase 2, ticket #41): how the authoritative Node-vs-
# native gate treats native's *correct superset* nodes.
#
# The native engine reads the raw CDP accessibility tree; the Node baseline
# (chrome-devtools-mcp) reports a filtered view of it. Two consequences the gate
# must not misread as regressions:
#
#   1. Native surfaces AX-internal detail the Node snapshot omits -- ``InlineTextBox``
#      leaves, empty structural ``generic`` / ``LabelText`` / ``paragraph``
#      containers, and the ``StaticText`` under a labelled control. These are
#      strictly *additional* nodes; native never drops one the baseline has.
#   2. Native pierces an open shadow root (ticket #40's finding); the Node
#      baseline pierces it too, so those nodes are shared, not extra -- but where
#      native's detail differs it is again additive.
#
# So the gate is a *coverage* test, not strict equality: native MUST contain every
# ``(role, name, value)`` node the Node baseline reports (multiset subset), MUST
# resolve every UID the baseline resolves to the same backend node, and MUST
# extract identical text. Native MAY report additional nodes. A *missing* baseline
# node, a diverging UID target, or any text difference is still a failure -- which
# is exactly what caught the pre-#41 cross-frame gap (native missing the iframe
# child's nodes) before stitching closed it. The strict :func:`compare_captures`
# is unchanged and still backs the operator's unit tests.
# --------------------------------------------------------------------------- #


def _uid_target_superset_diffs(
    baseline: dict[str, str],
    candidate: dict[str, str],
) -> list[ParityDiff]:
    """Superset UID comparison: candidate must cover baseline, may resolve more.

    Fails only when the candidate does not resolve a UID the baseline resolves,
    or resolves it to a different backend node. A UID the candidate resolves and
    the baseline does not is an *additional* resolution (native pierces shadow
    roots and iframes the top-document ``querySelectorAll`` baseline cannot see),
    and is not a failure -- the superset counterpart of the node-set rule.
    """
    diffs: list[ParityDiff] = []
    for uid in sorted(baseline):
        base_target = baseline[uid]
        cand_target = candidate.get(uid)
        if cand_target is None:
            diffs.append(
                ParityDiff(
                    "uid_target",
                    f"uid {uid!r} does not resolve in candidate (baseline -> {base_target!r})",
                )
            )
        elif cand_target != base_target:
            diffs.append(
                ParityDiff(
                    "uid_target",
                    f"uid {uid!r} resolves to {cand_target!r}, baseline resolves to {base_target!r}",
                )
            )
    return diffs


def superset_diffs(baseline: PageCapture, candidate: PageCapture) -> list[ParityDiff]:
    """Diffs where ``candidate`` fails to *cover* ``baseline`` (see module note).

    A node-set diff is raised only for a baseline node the candidate lacks (an
    under-count), never for a candidate node absent from the baseline. UID
    resolution is compared under the same superset rule. Text is compared exactly
    (both engines run identical JS on identical DOM, so text is not a superset
    dimension).
    """
    diffs: list[ParityDiff] = []

    base_counts = Counter(node.key() for node in baseline.nodes)
    cand_counts = Counter(node.key() for node in candidate.nodes)
    missing = base_counts - cand_counts
    for key, count in sorted(missing.items(), key=lambda kv: tuple(str(p) for p in kv[0])):
        diffs.append(
            ParityDiff(
                "node_set",
                f"baseline node not covered by candidate x{count}: "
                f"role={key[0]!r} name={key[1]!r} value={key[2]!r}",
            )
        )

    diffs.extend(_uid_target_superset_diffs(baseline.uid_targets, candidate.uid_targets))
    diffs.extend(_text_diffs(baseline.text, candidate.text))
    return diffs


def compare_superset(baseline: PageCapture, candidate: PageCapture) -> ParityResult:
    """Gate one page under superset semantics: candidate must cover baseline."""
    return ParityResult(page_id=baseline.page_id, diffs=tuple(superset_diffs(baseline, candidate)))


def corpus_covers(
    baseline: dict[str, PageCapture],
    candidate: dict[str, PageCapture],
) -> dict[str, ParityResult]:
    """Superset-gate a whole captured corpus, page by page.

    A page present in only one run is a failure, reported as a synthetic diff so
    the gate never silently drops a page (mirrors :func:`compare_corpus`).
    """
    results: dict[str, ParityResult] = {}
    for page_id in sorted(set(baseline) | set(candidate)):
        if page_id not in candidate:
            results[page_id] = ParityResult(
                page_id, (ParityDiff("node_set", "page missing from candidate run"),)
            )
        elif page_id not in baseline:
            results[page_id] = ParityResult(
                page_id, (ParityDiff("node_set", "page missing from baseline run"),)
            )
        else:
            results[page_id] = compare_superset(baseline[page_id], candidate[page_id])
    return results


# --------------------------------------------------------------------------- #
# Serialization: a baseline captured from one engine is persisted as JSON so a
# later native-engine run can be compared against it without re-launching the
# first engine.
# --------------------------------------------------------------------------- #


def capture_to_dict(capture: PageCapture) -> dict[str, object]:
    """Convert a :class:`PageCapture` to a JSON-serializable dict."""
    return {
        "page_id": capture.page_id,
        "url": capture.url,
        "nodes": [
            {
                "role": n.role,
                "name": n.name,
                "value": n.value,
                "uid": n.uid,
                "backend_node": n.backend_node,
            }
            for n in capture.nodes
        ],
        "uid_targets": dict(capture.uid_targets),
        "text": capture.text,
    }


def capture_from_dict(data: dict[str, object]) -> PageCapture:
    """Rebuild a :class:`PageCapture` from :func:`capture_to_dict` output."""
    raw_nodes = data.get("nodes", []) or []
    nodes = tuple(
        SnapshotNode(
            role=str(n["role"]),
            name=str(n["name"]),
            value=n.get("value"),
            uid=n.get("uid"),
            backend_node=n.get("backend_node"),
        )
        for n in raw_nodes  # type: ignore[union-attr]
    )
    return PageCapture(
        page_id=str(data["page_id"]),
        url=str(data.get("url", "")),
        nodes=nodes,
        uid_targets=dict(data.get("uid_targets", {}) or {}),  # type: ignore[arg-type]
        text=str(data.get("text", "")),
    )


def corpus_to_dict(captures: dict[str, PageCapture]) -> dict[str, object]:
    """Serialize a whole captured corpus."""
    return {page_id: capture_to_dict(cap) for page_id, cap in captures.items()}


def corpus_from_dict(data: dict[str, object]) -> dict[str, PageCapture]:
    """Deserialize a whole captured corpus."""
    return {page_id: capture_from_dict(cap) for page_id, cap in data.items()}  # type: ignore[arg-type]
