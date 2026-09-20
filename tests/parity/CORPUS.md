# Parity corpus and harness

This directory holds the parity gate for the Phase 2 native-snapshot rebuild
(RFC-01, Testing Strategy > "Parity gate" and "Native snapshot"). It builds the
gate; it does **not** build the native snapshot engine (that is #39-#41).

The gate compares two engines running the same frozen page corpus under one
normative operator. This ticket (#38) delivers the corpus and the harness; the
native engine plugs in during Phase 2 as the candidate.

## 1. Why the corpus is authored fresh

RFC-01 resolved the corpus question as "survey the current e2e fixtures first"
(Open Question 3). That survey ran before Phase 2 and found nothing reusable.

The whole of the repo's live-browser coverage was a single public page,
`https://example.com`, asserted shallowly: the snapshot tree is non-empty and
contains the string `"Example Domain"`. No structural assertion, no UID
interaction, no iframe, no shadow DOM. Everything else -- the Accessibility
read path behind `ax_find` / `ax_node`, and the snapshot the UID tools depend on
-- was driven against synthetic CDP responses and canned payloads, never a real
page. The one concrete snapshot sample in the tree was a mocked Playwright
ARIA-snapshot line, which is what fixed the parser's target format.

**Finding.** Nothing local, reproducible, or structural to reuse, and nothing
covering iframe or shadow DOM. So the corpus is authored fresh here as local
static fixtures, which also removes the dependency on a live public site that
the RFC's "reproducible offline" gate needs.

## 2. The frozen corpus

Local static HTML under `fixtures/`. Frozen before Phase 2 starts: editing a
fixture invalidates any baseline captured against it. Defined in
`parity_corpus.py` (`CORPUS`).

| page_id   | fixture         | structural case it covers                                  |
|-----------|-----------------|------------------------------------------------------------|
| `plain`   | `plain.html`    | plain static page: heading, prose, links                   |
| `form`    | `form.html`     | form: inputs, checkbox, buttons - the UID-interaction case |
| `iframe`  | `iframe.html`   | iframe: an embedded child frame (`iframe_child.html`)      |
| `shadow`  | `shadow.html`   | shadow DOM: controls inside an open shadow root            |
| `dynamic` | `dynamic.html`  | dynamic content: nodes added after the initial paint       |

`iframe_child.html` is a support file loaded by `iframe.html`; it is not a
corpus entry on its own. The iframe and shadow DOM cases are the two the RFC
names as mandatory; both are present.

## 3. The harness

- **`parity_comparison.py`** - the data model (`SnapshotNode`, `PageCapture`,
  `ParityDiff`, `ParityResult`) and the normative operator
  (`compare_captures`, `compare_corpus`, `corpus_matches`), plus JSON
  serialization so a baseline persists between engine runs. The operator is
  engine-agnostic and is the tested core of this ticket.
- **`parity_corpus.py`** - the frozen corpus and fixture-path/URL resolution.
- **`parity_engines.py`** - the `ParityEngine` protocol, the documented
  Playwright ARIA-snapshot parser (`parse_aria_snapshot`), and
  `AriaSnapshotEngine`, a working engine that drives any `call_tool` session
  (`camoufox_session.py`, beside these tests). `capture_corpus` runs an engine
  over the corpus.
- **`node_broker.py`** - the JSON-RPC-over-stdio multiplexer `NodeMcpSession`
  drives the real `chrome-devtools-mcp` subprocess with. It lives here, not in
  the package: it is a test oracle, and the product has no MCP surface.
- **`run_baseline.py`** - the runnable entry point that launches a real browser
  and writes a baseline JSON (see below).

### The normative comparison operator

Exactly as RFC-01 fixes it:

1. **Snapshot node sets** - compared order-insensitively on `(role, name,
   value)` tuples. Implemented as a multiset (`collections.Counter`) of tuples,
   so order in the tree is irrelevant but multiplicity is preserved (two
   identical buttons are two nodes). A UID or backend-node value on a node does
   **not** enter the key, so engines that assign different UID strings still
   match on structure.
2. **UID resolution** - compared by the backend node a click/fill resolves to,
   keyed by UID. A raw CDP `backendDOMNodeId` is a per-session opaque integer
   and is not comparable across two browser sessions, so `PageCapture` carries
   a **stable** identity (a DOM-tree path) for the node each UID resolves to,
   normalized by the engine at capture time. The operator compares those stable
   identities. `AriaSnapshotEngine` models a UID as an element's CSS id selector
   and its target as the element's DOM path; the native engine plugs in by
   normalizing its own UID -> backend node the same way.
3. **Text extraction** - compared exactly (string equality).

`compare_captures` returns a `ParityResult` whose `matched` is true only when
all three agree; each disagreement is a `ParityDiff` tagged with its dimension.
`corpus_matches` is the gate predicate over a whole corpus.

### The engines

Three implement `ParityEngine`:

- `NativeSnapshotEngine` / `NativeInteractionEngine` - the candidate, this
  repo's own CDP-native read and interaction path.
- `NodeEngine` over a live `chrome-devtools-mcp` subprocess - the authoritative
  baseline the Phase 2 gate compares against.
- `AriaSnapshotEngine` over Camoufox - the earlier baseline rung, still compared
  against native on their shared dimensions.

The gate passes when `corpus_covers` holds for two consecutive flake-free runs.

## 4. What runs, and what it proves

The harness and operator are real, unit-tested code
(`test_parity_operator.py`, `test_parity_engine.py`, `test_parity_corpus.py`):
order-insensitivity holds, and a role/name/value, UID-target, or text
difference is each caught. These run with no browser.

The live rungs are marked `@pytest.mark.parity` and **skip cleanly** when their
browser is unavailable, so the default suite stays green offline. Only a launch
may skip: a failure inside a capture fails the gate rather than reporting as an
unavailable engine. That distinction was not always made, and an upstream
argument change in `chrome-devtools-mcp` once turned the authoritative gate off
while the suite stayed green (#91).

| Rung | File | Needs |
|---|---|---|
| ARIA baseline | `test_parity_baseline.py` | Camoufox (`camoufox fetch`) |
| Native | `test_parity_native.py` | Playwright Chromium |
| Native interaction | `test_parity_native_interaction.py` | Playwright Chromium |
| **The gate: native vs Node** | `test_parity_gate.py` | Playwright Chromium **and** `npx` |

The gate requires native to **cover** the Node baseline: contain every
`(role, name, value)` node it reports, resolve every UID identically, and
extract identical text, while allowing native's additional nodes. The native
engine reads the raw accessibility tree and legitimately reports more detail
than the Node snapshot.

One observation from the ARIA rung, kept because the corpus exists to surface
exactly this: `iframe.html`'s child-frame content does not appear in the
`AriaSnapshotEngine` node set, because Playwright's `aria_snapshot()` of `body`
does not descend into the child frame's document. Native does cross that
boundary, which is what `test_native_covers_iframe_child_frame` pins.

## Running

```
uv run pytest tests/parity/            # operator + parser + corpus unit tests (no browser)
uv run pytest -m parity                # live parity capture (skips without a browser)
uv run python tests/parity/run_baseline.py --out tests/parity/baseline.json
```

`baseline.json` is environment-specific (its `url` fields embed absolute
`file://` paths) and is not committed; regenerate it with `run_baseline.py`.
