# Parity corpus and harness

This directory holds the parity gate for the Phase 2 native-snapshot rebuild
(RFC-01, Testing Strategy > "Parity gate" and "Native snapshot"). It builds the
gate; it does **not** build the native snapshot engine (that is #39-#41).

The gate compares two engines running the same frozen page corpus under one
normative operator. This ticket (#38) delivers the corpus and the harness; the
native engine plugs in during Phase 2 as the candidate.

## 1. Survey of the existing e2e fixtures

RFC-01 resolved the corpus question as "survey the current e2e fixtures first"
(Open Question 3). The survey of what the current e2e and snapshot tests
actually exercise:

- **`tests/test_e2e_camoufox.py`** - the only live-browser e2e suite. It
  launches a real Camoufox browser and drives `CamoufoxSession.call_tool`. Its
  pages/fixtures:
  - `https://example.com` - the single navigation target, used for
    `navigate`, `screenshot`, `snapshot`, `evaluate`, and `get_cookies`. A live
    public site, not a local fixture.
  - `snapshot` is asserted only shallowly: the returned tree is non-empty and
    contains the string `"Example Domain"`. No structural assertion, no UID
    interaction, no iframe or shadow DOM coverage.
- **`tests/test_e2e_backend_seam.py`** - exercises the `AutomationBackend`
  routing seam, not real pages.
- **`ax_find` / `ax_node`** (`src/browser_tools/cdp_handler.py`) - the
  Accessibility-domain read path that Phase 2 rebuilds on. In tests they are
  driven only against **synthetic CDP responses** (`tests/test_new_tools.py`,
  `tests/test_dispatch.py`), never against real pages. `ax_find` calls
  `Accessibility.queryAXTree` and formats `(role, name, backendDOMNodeId)`;
  `ax_node` resolves a selector to a backend DOM node and reads its partial AX
  tree. This is the (role, name, value) + backend-node material the parity
  operator compares.
- **`snapshot` / `take_snapshot`** - the Node-engine snapshot the UID tools
  depend on. `tool_registry.py` marks `click`/`hover`/`fill`/`fill_form`/
  `drag`/`press_key`/`upload_file` as `interaction=True`, meaning the
  controller takes a pre-snapshot so a UID resolves in the current session.
  Tests drive `take_snapshot` only with **canned responses**
  (`tests/test_persistent_browser.py`, `tests/test_page_selection.py`,
  `tests/test_mcp_daemon.py`); none run a real page.
- **`conftest.py`** - the one concrete snapshot sample in the repo is the
  mocked `aria_snapshot` return `- heading "Example Domain" [level=1]`
  (Playwright ARIA-snapshot YAML). This fixed the parser's target format.

**Finding.** The existing e2e coverage is a single live public page
(`example.com`) with a shallow snapshot assertion, and no structural cases at
all. There is nothing local, reproducible, or structural to reuse directly, and
nothing covering iframe or shadow DOM. So the corpus is authored fresh here as
local static fixtures, which also removes the dependency on a live public site
that the RFC's "reproducible offline" gate needs.

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
  (this repo's `CamoufoxSession`). `capture_corpus` runs an engine over the
  corpus.
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

### How Phase 2 plugs in

Phase 2 adds a `NativeSnapshotEngine` (and, for the reference baseline, a
`NodeEngine` over chrome-devtools-mcp) implementing the `ParityEngine`
protocol, captures the corpus through each, and calls `compare_corpus`. The
gate passes when `corpus_matches` holds for two consecutive flake-free runs.

## 4. Baseline status (rung honesty)

The harness and operator are real, unit-tested code
(`test_parity_operator.py`, `test_parity_engine.py`, `test_parity_corpus.py`):
order-insensitivity holds, and a role/name/value, UID-target, or text
difference is each caught. These run with no browser.

`run_baseline.py` and `test_parity_baseline.py` (`@pytest.mark.parity`) capture
against a **real** browser and **skip cleanly** when none is available, so the
default suite stays green offline.

In this environment Camoufox was installed and launched, so a real baseline was
captured from the live `AriaSnapshotEngine` path (real browser, real ARIA
snapshot, real UID resolution, real settle of the dynamic page). What was **not**
captured is the RFC's specific chrome-devtools-mcp **Node-engine** baseline:
that engine is a separate Node subprocess and is not wired here. It is Phase 2's
job to add that engine behind the same `ParityEngine` protocol and swap it into
`run_baseline.py`. So: the harness is proven end-to-end against a live browser;
the Node-vs-native comparison the gate ultimately runs is left as the pluggable
engine slot.

Observation from the live capture worth carrying into Phase 2: `iframe.html`'s
child-frame content does not appear in the `AriaSnapshotEngine` node set (the
Playwright `aria_snapshot()` of `body` does not descend into the child frame's
document). Whether an engine reaches across the frame boundary is exactly the
kind of difference the parity operator exists to surface - the corpus keeps the
iframe case so the gate can catch it.

## Running

```
uv run pytest tests/parity/            # operator + parser + corpus unit tests (no browser)
uv run pytest -m parity                # live parity capture (skips without a browser)
uv run python tests/parity/run_baseline.py --out tests/parity/baseline.json
```

`baseline.json` is environment-specific (its `url` fields embed absolute
`file://` paths) and is not committed; regenerate it with `run_baseline.py`.
