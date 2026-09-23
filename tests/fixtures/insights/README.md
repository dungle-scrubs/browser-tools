# Real trace fixtures

These are unmodified synthetic captures, not hand-authored trace events.

- `load-only.json`, `driven.json`, `nav.json`, `nav-click.json`: captured on
  2026-09-23 by `bt trace` from browser-tools commit `2a4fc3b`, using the
  Playwright headless shell, producer `HeadlessChrome/151.0.7922.34`.
  `fixture.html` has the 280ms click handler. The two navigation step lists
  record the commands used. Absolute file URLs are historical capture data.
- The duration capture on an already loaded page and the click-only capture
  each produce zero Insight Sets. Navigation produces INP pass; navigation
  plus click produces INP fail with 280ms processing time.
- Both file navigation captures lack document request timing. Engine 0.0.65
  records a DocumentLatency model error separately from its 18 successful
  models. Tests explicitly retain that failure.
- `http-navigation.json`: the original RFC-05 HTTP capture probe's streamed
  trace, producer `HeadlessChrome/153.0.0.0`. Its capture script is
  `docs/research/probes/154/exp-b.py` and page is
  `docs/research/probes/154/fixture.html`. It yields 19 models without errors,
  including RenderBlocking fail. It is prototype evidence, not a new capture
  from the merged trace verb.

The four zero-set and failure fixtures were captured on 2026-09-23 the same
way, over a loopback HTTP server, producer `HeadlessChrome/151.0.7922.34`.
Each one names a different cause, and each was a case the single fixed
zero-Insight-Set diagnostic described wrongly:

- `narrow-categories.json`: the `nav-click` step list captured with
  `--categories=devtools.timeline,v8.execute`. The navigation is in the trace
  as a Document request, but `navigationStart` is in `blink.user_timing`,
  which that category list drops.
- `uncommitted-navigation.json`: `Page.navigate` to a 404 URL with
  `--duration 3`. One `navigationStart`, `documentLoaderURL` empty: no
  document committed inside the trace window.
- `error-page.json`: `Page.navigate` to `http://127.0.0.1:1/never.html`, which
  fails with `net::ERR_UNSAFE_PORT`, then `wait-stable`. Chrome commits
  `chrome-error://chromewebdata/` and the engine analyses that error page.
- `two-navigations.json`: `Page.navigate` to two pages in one step list, each
  followed by `wait-text` and `wait-stable`. Two Insight Sets, 19 models each.

`tests/test_insights_live.py` repeats navigation-only and navigation-plus-click
captures using the merged `bt trace` over a loopback HTTP server. Run after
`bt insights --setup`, with `BT_INSIGHTS_LIVE=1`. When selected, it fails if
Chrome cannot launch. It uses the headless shell selected by `tests/conftest.py`
and stops only its own instance. Ordinary fixture tests require setup too;
missing setup is explicitly skipped and cannot establish engine acceptance.
