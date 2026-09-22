# Findings: does the Lighthouse CLI run against a bt instance? (ticket 151)

Research worker output for
[dungle-scrubs/browser-tools#151](https://github.com/dungle-scrubs/browser-tools/issues/151),
under map [#147](https://github.com/dungle-scrubs/browser-tools/issues/147).
No commits, no pushes, no issue comments. Local files only.

Every claim below carries its standing: **documented** (the owning source says
it), **observed** (run here and seen), or **unverified**.

Versions used, all observed:

| Thing | Version |
|---|---|
| `bt` | browser-tools 0.8.0 |
| Lighthouse CLI | 13.5.0, run as `npx -y lighthouse@latest` |
| chrome-launcher (bundled in Lighthouse) | 1.2.1 |
| Chrome that `bt launch` started | Chrome/153.0.8010.53 |
| Node / npm | v24.15.0 / 11.12.1 |
| Date of runs | 2026-09-22 |

## The reframing finding: a headed run takes the screen

Lighthouse runs against a bt instance. The open question for the recipe is
which instance shape it may run against.

**A headed Lighthouse run brings the audited browser window to the front and
takes the user's keyboard focus.** Observed. The recipe must say headless.

Evidence, observed. During a run against headed instance `151-03` (pid 69666),
the frontmost application was sampled every 0.3 s with `lsappinfo front`. The
window came to the front for about 3 s in the middle of the run, then focus
returned to the terminal:

    8 samples   pid 11088  Ghostty          (before the tab opened)
    10 samples  pid 69666  Google Chrome    (instance 151-03, the audited browser)
    25 samples  pid 11088  Ghostty

Control, observed. The same sampler over a run against headless instance
`151-02` recorded 70 of 70 samples on Ghostty. Focus never moved.
Samples: `.scratch/retire-axi/work/151/focus-during-headed.txt` and
`focus-during-headless.txt`.

The launch itself is not the cause. `bt launch` put `151-03` in the background
(front app stayed pid 11088 straight after the launch), and the move happened
during the Lighthouse run.

Why bt's protection does not cover this, documented. `bt guide` section NEVER
TAKE THE SCREEN refuses `Page.bringToFront`, `Target.activateTarget` and
`Target.createTarget` with `background:false` (exit 2). Those refusals live in
`bt`. Lighthouse speaks CDP straight to the browser port and never passes
through `bt`, so no refusal applies. Lighthouse itself calls neither
`bringToFront` nor `activateTarget` (grep over `core/` and `cli/` in
lighthouse 13.5.0 returns nothing). It calls
`puppeteer.connect({browserURL}).newPage()`
(`core/gather/navigation-runner.js:282-283`), and `newPage` opens a foreground
tab, which raises the window.

Consequence for the guide: every bt verb is safe to run while a person is at
the keyboard, and a headed Lighthouse run is not. The recipe has to carry that
difference, not just the command line.

## The working command line

Observed, exit 0, report written, instance intact:

```sh
# 1. Work in your own directory. bt names the instance after it.
cd /path/to/your/work/dir

# 2. Launch headless. Headed steals the user's focus mid-run.
bt launch --headless

# 3. Read the port out of the registry, do not assume one.
PORT=$(bt status <instance> | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["port"])')

# 4. Audit through that port.
npx -y lighthouse@latest https://example.com \
  --port="$PORT" \
  --output=json --output=html \
  --output-path=./reports/run-name

# 5. Stop only what you launched.
bt stop <instance>
```

Flags, and what each one is for:

| Flag | Standing | Note |
|---|---|---|
| `--port=PORT` | documented + observed | The only flag that attaches Lighthouse to an existing browser. Default is `0`, which means a random port and Lighthouse's own Chrome. `npx lighthouse --help`, and observed. |
| `--output=json --output=html` | documented | Repeatable. Default is `html` alone. |
| `--output-path=PATH` | documented + observed | With two or more `--output` values, Lighthouse appends `.report.json` / `.report.html`. With exactly one, it writes `PATH` verbatim, extension and all. Observed: `--output=json --output-path=./reports/headed2-example` produced a file named `headed2-example` with no extension. |
| `--only-categories=performance` | documented + observed | Available categories in 13.5.0: `accessibility`, `best-practices`, `performance`, `seo`, `agentic-browsing`. |
| `--preset=desktop` | observed | Composes with `--port`. Run `desktop-perf.json` came back `formFactor: desktop`, performance 1, all 17 insights present. |
| `--quiet` | observed, with a cost | Suppresses the one line that says which browser Lighthouse used. See "No signal by default" below. |
| `--save-assets` | documented + observed | Writes `<path>-0.devtoolslog.json` and `<path>-0.trace.json` beside the report. The devtools log carries request headers, which is how the cookie claim below was verified. |
| `--chrome-flags=...` | documented | Ignored on an attached browser. chrome-launcher returns before it reads `chromeFlags` (`chrome-launcher.js:186-201`). |

Headless, headed, or special browser arguments:

- **Headless works.** Observed, exit 0. `environment.hostUserAgent` in the
  report reads `HeadlessChrome/153.0.0.0`.
- **Headed works too.** Observed, exit 0, `hostUserAgent` reads
  `Chrome/153.0.0.0`. It takes the screen, so the recipe should not use it.
- **No extra browser arguments are needed.** Observed. The instance ran on
  bt's own launch flags and nothing else:
  `--remote-debugging-port=9229 --user-data-dir=/tmp/chrome-agent/session-bupwv6t2
  --no-first-run --no-default-browser-check --password-store=basic
  --no-startup-window`.
- **Both shapes emit the same audits.** Observed: 161 audits and the same 17
  insights in the headed and the headless report.
- **Scores move with machine load, not with the head.** Observed. One headless
  run on example.com scored performance 0.78 at `benchmarkIndex` 1905; a second
  headless run scored 1.00 at 3538; headed runs scored 1.00 at 2665 and 3762.
  Do not read a headed-versus-headless difference into a single pair of runs.
- **Camoufox cannot be audited.** Documented, repo source:
  `src/browser_tools/camoufox_runner.py:5` states Camoufox "exposes no Chrome
  debugging port, so its liveness is process" identity. Lighthouse needs a CDP
  port. Not observed here; no Camoufox instance was launched.

## Sub-question answers

### Does Lighthouse close, restart, or disturb the instance?

**No. The instance survives untouched.** Observed across five runs against four
instances (`151-01` headed, `151-02` headless, `151-03` headed, `151-04` headless
on a named profile). After each run `bt status <instance>` reported the same
name, the same port, `alive: true`, the same pid, and the same target list as
before.

Mechanism, documented (implementation):

- chrome-launcher 1.2.1 `Launcher.launch()` returns early when a debugger
  answers on the requested port: "Found existing Chrome already running using
  port N, using that." (`chrome-launcher.js:186-201`). It does not spawn.
- `Launcher.kill()` returns immediately when `this.chromeProcess` is unset
  (`chrome-launcher.js:322-325`), which is the case on that early return. So
  the `launchedChrome?.kill()` in `cli/run.js:217` is a no-op against a browser
  Lighthouse did not start.
- Lighthouse closes only the page it opened and then disconnects:
  `await lhPage?.close(); await lhBrowser?.disconnect();`
  (`core/gather/navigation-runner.js:249-250`). There is no `browser.close()`.

**Registry survives.** Observed: `bt status` before and after each run is
byte-identical apart from the transient tab. Files
`status-before-headed.json` / `status-after-headed.json` and the headless pair.

**Window marking survives.** Observed. The headed instance's tab title stayed
`🤖 151-01 —` after the run, and `151-03` stayed `🤖 151-03 —`. Marking is a
headed-only thing: the headless instances carried the plain title `New Tab`,
before any Lighthouse run.

**bt commands work during a run.** Observed. `bt status 151-04` was polled every
0.5 s throughout a run and answered every time.

**One transient tab, and it shifts `--target` indices.** Observed. During the
run the instance holds two page targets, then one again:

    sample 1: ['https://httpbin.org/cookies']
    sample 3: ['https://httpbin.org/cookies', 'about:blank']
    sample 4: ['https://httpbin.org/cookies', 'https://httpbin.org/cookies']
    ... then back to one

`bt guide` defines `--target SPEC` as a 1-based index into page targets sorted
by target ID. A concurrent `bt` call using a numeric `--target` can therefore
address the wrong page while a Lighthouse run is in flight. Use `--url
SUBSTRING` or a target ID prefix, or do not drive the instance during a run.

**Storage side effects, documented (implementation), not measured here.** A
default run clears, for the audited origin only,
`file_systems, shader_cache, service_workers, cache_storage`
(`core/config/constants.js:69`), and wipes the whole browser HTTP cache with
`Network.clearBrowserCache` (`core/gather/driver/storage.js:122-133`). Cookies,
localStorage and IndexedDB are not in the cleared set. `--disable-storage-reset`
turns the clearing off.

### What does a named profile change?

**Lighthouse audits inside the profile's browser, and its own request carries
the profile's cookies.** Observed, and this is the strongest evidence in the
file.

Method: `bt launch --profile lh-test --headless` gave instance `151-04` on port
9234, bound to `~/.local/share/browser-tools/profiles/lh-test`. A probe cookie
was set by hand in that browser:

    bt 151-04 Page.navigate '{"url":"https://httpbin.org/cookies"}'
    bt 151-04 Runtime.evaluate '{"expression":"document.cookie = \"lhprobe=cookie-survived; path=/; max-age=86400\"; document.cookie","returnByValue":true}'
    -> "lhprobe=cookie-survived"

Then `npx -y lighthouse@latest https://httpbin.org/html --port=9234 --save-assets`.
The saved devtools log
(`reports/profile-httpbin-html-0.devtoolslog.json`) holds two
`Network.requestWillBeSentExtraInfo` events for Lighthouse's own navigation:

    "Cookie header": "lhprobe=cookie-survived"
    associatedCookies: [{"cookie": {"name":"lhprobe","value":"cookie-survived",
                        "domain":"httpbin.org", ...}, "blockedReasons": []}]

`blockedReasons` is empty, so the cookie was sent, not merely present.

Three further observations on the same run:

- Lighthouse did not spawn its own Chrome: its log says "Found existing Chrome
  already running using port 9234, using that."
- The audited page ran in that instance: the transient tab was seen in
  `bt status 151-04` while the run was in flight (`profile-tabs.txt`).
- The cookie survived. After both runs, `document.cookie` in that browser still
  returned `lhprobe=cookie-survived`, and `bt profile list` still showed
  `lh-test` held by `151-04`.

So the login recipe from `bt guide` (PROFILES AND LOGIN) carries straight over:
log in by hand once with `bt launch --profile NAME`, and every later
`--port` run audits the signed-in page. This is the same shape the Lighthouse
docs give for `chrome-debug` under "Testing on a site with authentication"
(`docs/readme.md`, lines 90-99), with `bt launch --profile NAME` in place of
`chrome-debug`.

The `lh-test` profile was deleted afterwards with `bt profile delete lh-test`.
Verified gone from `bt profile list` and from disk.

Caveat, documented above and not measured: a run wipes the profile's HTTP cache
browser-wide. A login is not affected. A page whose behaviour depends on a warm
cache is.

### No signal by default: a wrong port does not fail

**This is the sharpest hazard in the recipe, and the Lighthouse docs are silent
on it.**

Documented (implementation) and observed: when nothing answers on `--port`,
chrome-launcher does not error. It launches its own Chrome on that port,
audits with it, kills it, and exits 0
(`chrome-launcher.js:195-201`, the `portStrictMode` branch is not taken).

Observed against a genuinely free port 9391:

    LH:ChromeLauncher No debugging port found on port 9391, launching a new Chrome.
    lighthouse exit=0
    listeners on 9391 after: none

That report (`reports/freeport-example.json`) is a real, clean-looking report.
Its `environment.hostUserAgent` reads `Chrome/156.0.0.0`, because chrome-launcher
picked Google Chrome Canary, while the bt instance was Chrome 153. So the report
came from a different browser, with no profile, no login and no extensions, and
nothing in the exit code or the report says so.

There is no CLI flag that makes this fail. `portStrictMode` exists in
chrome-launcher but is not exposed: grep for it in `cli/cli-flags.js` returns
nothing, and it is absent from `npx lighthouse --help`.

Two guards for the recipe, both observed to work:

1. Read the port from `bt status` in the same script that runs Lighthouse.
   Never carry a port across sessions; bt assigns ports per launch, and this
   machine had eight live instances on eight ports during these runs.
2. Drop `--quiet` and check the log for
   `LH:ChromeLauncher Found existing Chrome already running using port N, using that.`
   With `--quiet` the stderr is empty and that line is gone. Observed: all four
   `--quiet` runs produced a zero-byte stderr file.

`environment.hostUserAgent` in the report is a weaker check. It separates
headless from headed and Canary from stable, but not two Chromes of the same
build.

## The insight audits

**Verified. The installed Lighthouse emits 17 insight audits, in a category
group named `insights` inside the performance category.** Both documented and
observed.

Documented: `core/config/default-config.js` lists exactly these 17 under
`audits` (lines 313-329) and again as `auditRefs` with `group: 'insights'`
(lines 427-443). Observed: the same 17 appear in every report produced here,
including the `--preset=desktop` run.

The config's own group description is the primary-source statement that ties
these to DevTools:

> These insights are also available in the Chrome DevTools Performance Panel -
> [record a trace](https://developer.chrome.com/docs/devtools/performance/reference)
> to view more detailed information.

(`core/config/default-config.js:21`)

The 17, with the score display mode each returned on `https://example.com`.
Every one carries `weight: 0`, so none of them moves the performance score:

| Audit id | Title | Mode on example.com |
|---|---|---|
| `cache-insight` | Use efficient cache lifetimes | metricSavings |
| `cls-culprits-insight` | Layout shift culprits | numeric |
| `document-latency-insight` | Document request latency | metricSavings |
| `dom-size-insight` | Optimize DOM size | numeric |
| `duplicated-javascript-insight` | Duplicated JavaScript | metricSavings |
| `font-display-insight` | Font display | metricSavings |
| `forced-reflow-insight` | Forced reflow | numeric |
| `image-delivery-insight` | Improve image delivery | metricSavings |
| `inp-breakdown-insight` | INP breakdown | notApplicable |
| `lcp-breakdown-insight` | LCP breakdown | informative |
| `lcp-discovery-insight` | LCP request discovery | notApplicable |
| `legacy-javascript-insight` | Legacy JavaScript | metricSavings |
| `modern-http-insight` | Modern HTTP | metricSavings |
| `network-dependency-tree-insight` | Network dependency tree | numeric |
| `render-blocking-insight` | Render-blocking requests | metricSavings |
| `third-parties-insight` | 3rd parties | numeric |
| `viewport-insight` | Optimize viewport for mobile | numeric |

Two qualifications the map should carry:

- **An 18th insight ships but does not run.**
  `core/audits/insights/slow-css-selector-insight.js` is in the package and is
  listed by `--list-all-audits`, which enumerates the audit directory rather
  than the config. It appears in no config, and it is absent from every report
  produced here. Observed and documented.
- **Interaction insights are not applicable to a navigation run.**
  `inp-breakdown-insight` and `lcp-discovery-insight` came back
  `notApplicable` on example.com. INP needs an interaction, and Lighthouse's
  navigation mode performs none. A recipe that promises "INP breakdown" will
  disappoint on a page with no interaction.

So the map's claim that Lighthouse covers load-time trace insights holds, for
load-time. Insights that need user interaction are reported, and empty.

## Other observations the recipe needs

- **A non-HTML URL fails the run, exit 1.** Observed. Auditing
  `https://httpbin.org/cookies` (`application/json`) returned
  `Runtime error encountered: The page provided is not HTML (served as MIME
  type application/json).`, exit 1, with every insight logging
  `Caught exception: NOT_HTML`. A report file is still written
  (`reports/profile-httpbin.json`), and it has `runtimeError.code: NOT_HTML`,
  no `fullPageScreenshot`, and an empty `final-screenshot`. A recipe that reads
  the report must check `runtimeError` and not only the exit code.
- **Run time.** Observed: 11 to 13 s wall clock per example.com run, including
  npx resolution.
- **Lighthouse 13.5.0 adds an `agentic-browsing` category** alongside
  performance, accessibility, best-practices and seo. Observed in
  `--only-categories` help text and in every full report. Not investigated.

## Incident: I audited another agent's instance

Reported here because it changed state outside this task's scope.

While testing the dead-port fallback I picked port 9299 after checking
`bt status` at the start of the session, which listed only ports 9222 and 9225.
By then a concurrent worker had launched instance `worker-a-01` on 9299.
Lighthouse attached to it: "Found existing Chrome already running using port
9299, using that."

What that did to `worker-a-01`, from the implementation cited above: opened one
tab, navigated it to `https://example.com`, cleared that browser's HTTP cache
browser-wide, cleared `file_systems / shader_cache / service_workers /
cache_storage` for `example.com`, closed its tab, disconnected. It did not
close, kill or restart the browser, and it did not touch that instance's own
tab.

State now: `worker-a-01` is still live and still registered, pid unchanged
(48470, listening on 9299 before and after). Its profile is `None`, a throwaway
session dir, so no login was at risk. Report of that run:
`reports/wrongport-example.json`.

I did not stop it and did not run `bt cleanup`. The user's `shopify-admin`
instance (`reviewsion-extensions-01`, port 9222) was never a target of a
Lighthouse run; the only contact with it was one read-only
`GET http://127.0.0.1:9222/json/version`, which creates no tab and changes
nothing.

Separately, `graybox-01` (port 9225) showed `alive: true` at the start of the
session and `alive: false` at the end. Nothing in this task touched it, and I
have no evidence about why it went down.

This incident is itself a finding: the "wrong port attaches to whatever is
there" hazard is not theoretical on a machine that runs several bt instances.

## Left open

- **Whether a headed run can be made safe.** Lighthouse has no flag for a
  background tab, and `puppeteer.newPage()` gives none. Untested: whether
  launching with `--no-startup-window` and never opening a window changes the
  focus behaviour, and whether a `--config-path` with a custom gatherer could
  reuse an existing tab instead of opening one. Not investigated.
- **Camoufox.** Claimed from repo source only, not observed. No Camoufox
  instance was launched.
- **The DevTools-versus-Lighthouse insight gap.** The config says the insights
  are also in the DevTools Performance panel. Whether the panel's set is
  exactly these 17, plus slow CSS selectors, was not checked against a DevTools
  build.
- **Cache wipe blast radius.** `Network.clearBrowserCache` is documented as
  browser-wide in the Lighthouse source, and was not measured. Its effect on a
  long-lived named profile used for repeated audits is unquantified.
- **Concurrency.** `bt status` polling during a run was fine. Two Lighthouse
  runs against one instance at once, and a Lighthouse run concurrent with a
  `bt run` step list, were not tried.
- **Whether the recipe belongs in `bt guide` at all, or beside it.** Out of
  scope for this ticket; the map settled it as a guide recipe.

## Artifacts

All under `/Users/kevin/dev/browser-tools/.scratch/retire-axi/work/151/`.

Reports:

| Path | Run |
|---|---|
| `reports/headed-example.report.json` / `.report.html` | headed instance `151-01`, port 9229 |
| `reports/headed2-example`, `reports/headed3-example` | headed instance `151-03`, port 9233, json only |
| `reports/headless-example.report.json` / `.report.html` | headless instance `151-02`, port 9232 |
| `reports/headless2-example` | headless `151-02`, repeat |
| `reports/desktop-perf.json` | headless `151-02`, `--preset=desktop --only-categories=performance` |
| `reports/profile-httpbin.json` | profile instance `151-04`, NOT_HTML failure |
| `reports/profile-httpbin-html.json` | profile instance `151-04`, the cookie-proof run |
| `reports/profile-httpbin-html-0.devtoolslog.json` | the devtools log holding the `Cookie` header |
| `reports/profile-httpbin-html-0.trace.json` | trace from the same run |
| `reports/freeport-example.json` | port 9391, nothing listening, Lighthouse's own Canary |
| `reports/wrongport-example.json` | port 9299, another worker's instance (see Incident) |

Evidence and scripts: `focus-during-headed.txt`, `focus-during-headless.txt`,
`focus-sample.sh`, `profile-tabs.txt`, `profilerun.sh`, `freeport.sh`,
`wrongport.sh`, `summarize.py`, `status-before-*.json`, `status-after-*.json`,
`launch-*.json`, `lh-*.stderr.txt`, `lighthouse-help.txt`,
`lighthouse-docs-readme.md`.

Lighthouse source read at
`/Users/kevin/.npm/_npx/ffe2131771d88588/node_modules/lighthouse` (13.5.0) and
`.../node_modules/chrome-launcher` (1.2.1). That is an npx cache and will be
evicted; the same files are in the lighthouse GitHub repo at the 13.5.0 tag.
