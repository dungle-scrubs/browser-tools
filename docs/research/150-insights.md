# Ticket 150 research: Chrome DevTools trace insights

Research date: 2026-09-22

Standing labels:

- **Documented** - explicitly stated by the cited upstream source.
- **Observed** - established by inspecting cited source code or release metadata.
- **Unverified** - not specified upstream or not accessible under this ticket’s no-shell constraint.

## Reframing finding

**Observed - chrome-devtools-mcp v1.9.0 (`1cec9cd`):** The premise that one published package implements the insights used by `performance_analyze_insight` is no longer exact. Current chrome-devtools-mcp pins the DevTools Frontend as a Git submodule and bundles its MCP entry point. Its runtime `package.json` does not depend on `@paulirish/trace_engine`. [v1.9.0 package.json](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/chrome-devtools-mcp-v1.9.0/package.json#L1-L117), [.gitmodules](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/chrome-devtools-mcp-v1.9.0/.gitmodules#L1-L5)

**Documented - DevTools Frontend commit `011ec3baaaaefa54eba1b41be00caf48ad3e8b51`:** `mcp/mcp.ts` is the integration entry point intended for chrome-devtools-mcp. The chrome-devtools-mcp build integrates those exports and their transitive dependencies into its own bundle. [DevTools MCP README](https://chromium.googlesource.com/devtools/devtools-frontend/+/011ec3baaaaefa54eba1b41be00caf48ad3e8b51/mcp/README.md#14)

**Observed - current package ecosystem:** A separately published snapshot, `@paulirish/trace_engine`, still exists and is used by Lighthouse. It is a useful Node integration boundary, but it is not the dependency currently used by chrome-devtools-mcp. [package README](https://www.npmjs.com/package/%40paulirish%2Ftrace_engine), [Lighthouse package.json](https://github.com/GoogleChrome/lighthouse/blob/main/package.json#L181-L210)

This distinction changes the packaging comparison:

- “Use the same engine as chrome-devtools-mcp” means pinning or building from DevTools Frontend.
- “Install the published trace engine package” means using the independently released `@paulirish/trace_engine` snapshot.
- Those sources can be at different DevTools revisions.

## 1. Implementation, Chrome versioning, and entry points

### Current chrome-devtools-mcp implementation

**Observed - chrome-devtools-mcp v1.9.0:** `parseRawTraceBuffer` accepts either an array of trace events or an object containing `traceEvents`. It constructs `TraceModel.Model.createWithAllHandlers()`, parses the events with optional metadata, and returns both `parsedTrace` and the generated navigation insight sets. [PerformanceTrace.ts](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/chrome-devtools-mcp-v1.9.0/src/processors/PerformanceTrace.ts#L13-L77)

**Observed - chrome-devtools-mcp v1.9.0:** `performance_analyze_insight` selects an insight from the most recently stored performance trace and renders it through `PerformanceInsightFormatter`. The tool returns formatted text rather than the raw insight model. [performance.ts](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/chrome-devtools-mcp-v1.9.0/src/tools/performance.ts#L143-L177), [PerformanceTrace.ts](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/chrome-devtools-mcp-v1.9.0/src/processors/PerformanceTrace.ts#L99-L148)

### Versioning against Chrome

**Documented - DevTools Frontend:** The `chrome-devtools-frontend` npm version has the form `1.0.<Chromium commit position>`. It advances with Chromium commits and is published roughly daily. It is not aligned to Chrome’s major version. The package is also not offered as a normal CommonJS or ES module library. [DevTools Frontend README](https://github.com/ChromeDevTools/devtools-frontend#source-code-and-documentation)

**Observed - chrome-devtools-mcp v1.9.0:** chrome-devtools-mcp instead pins one DevTools Frontend commit through its submodule and bundles the selected exports. Updating the engine is therefore a source-pin and rebuild operation, not an automatic match to the Chrome binary found at runtime. [.gitmodules](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/chrome-devtools-mcp-v1.9.0/.gitmodules#L1-L5), [package scripts and published files](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/chrome-devtools-mcp-v1.9.0/package.json#L11-L54)

**Documented - chrome-devtools-mcp current README:** Upstream supports official Google Chrome and Chrome for Testing, and targets the latest Chrome Extended Stable release. Other Chromium builds can behave unexpectedly. [Requirements and support policy](https://github.com/ChromeDevTools/chrome-devtools-mcp#requirements)

**Unverified:** The exact DevTools Frontend submodule SHA stored in the v1.9.0 Git tree was not exposed by the available web rendering. The fact that a commit is pinned is established; its identifier remains open.

### Available entry points

**Observed - DevTools Frontend commit `011ec3...`:** The trace root exports `EntityMapper`, `EventsSerializer`, `Extras`, `Handlers`, `Helpers`, `Insights`, `Lantern`, `LanternComputationData`, `Name`, `Processor`, `Styles`, `TraceModel`, and `Types`. [trace.ts](https://github.com/ChromeDevTools/devtools-frontend/blob/011ec3baaaaefa54eba1b41be00caf48ad3e8b51/front_end/models/trace/trace.ts#L7-L32)

**Observed - current DevTools MCP entry point:** `mcp/mcp.ts` exposes the trace engine and MCP-facing formatters, including `TraceEngine`, `PerformanceInsightFormatter`, and `PerformanceTraceFormatter`. [mcp.ts, package snapshot 1.0.1630574](https://app.unpkg.com/chrome-devtools-frontend@1.0.1630574/files/mcp/mcp.ts)

**Documented - `@paulirish/trace_engine` 0.0.65:** The package’s root import is the supported entry shown by its README:

```js
import * as TraceModel from '@paulirish/trace_engine';

polyfillDOMRect();
const engine = TraceModel.Processor.TraceProcessor.createWithAllHandlers();
await engine.parse(traceEvents);
const parsedTrace = engine.data;
```

The README requires a `DOMRect` polyfill in Node and explicitly says the package is not for public consumption, has an unstable API, and can break on upgrade. It is prepared manually from DevTools Frontend and uses manually bumped `0.0.x` versions. [`@paulirish/trace_engine` README](https://www.npmjs.com/package/%40paulirish%2Ftrace_engine)

## 2. Insights produced today

**Observed - DevTools Frontend commit `011ec3...`:** `InsightModels` contains 19 insight keys. [types.ts](https://github.com/ChromeDevTools/devtools-frontend/blob/011ec3baaaaefa54eba1b41be00caf48ad3e8b51/front_end/models/trace/insights/types.ts#L122-L157)

1. `LCPBreakdown`
2. `INPBreakdown`
3. `CLSCulprits`
4. `ThirdParties`
5. `DocumentLatency`
6. `DOMSize`
7. `DuplicatedJavaScript`
8. `FontDisplay`
9. `ForcedReflow`
10. `ImageDelivery`
11. `LCPDiscovery`
12. `LegacyJavaScript`
13. `NetworkDependencyTree`
14. `RenderBlocking`
15. `SlowCSSSelector`
16. `Viewport`
17. `ModernHTTP`
18. `Cache`
19. `CharacterSet`

### Common result shape

**Observed - DevTools Frontend commit `011ec3...`:** Every insight model carries:

- `insightKey`
- localized strings, title, description, documentation link, and category
- `state`: `pass`, `fail`, or `informative`
- optional related trace events and warnings
- optional metric savings and wasted bytes
- optional frame and navigation references
- optional overlay generation

An `InsightSet` adds its navigation identifier, URL, frame, time bounds, the 19-model result map, per-model errors, and optional navigation event. [base model and InsightSet definitions](https://github.com/ChromeDevTools/devtools-frontend/blob/011ec3baaaaefa54eba1b41be00caf48ad3e8b51/front_end/models/trace/insights/types.ts#L56-L120)

### Example: `LCPBreakdown`

**Observed - DevTools Frontend commit `011ec3...`:** In addition to the common fields, `LCPBreakdown` can contain:

```ts
{
  insightKey: 'LCPBreakdown',
  state: 'pass' | 'fail' | 'informative',
  lcpMs?: number,
  lcpTs?: number,
  lcpEvent?: TraceEventLargestContentfulPaintCandidate,
  lcpRequest?: SyntheticNetworkRequest,
  subparts?: {
    ttfb: { bounds, label },
    loadDelay?: { bounds, label },
    loadDuration?: { bounds, label },
    renderDelay: { bounds, label }
  },
  relatedEvents?: TraceEvent[],
  warnings?: Warning[],
  metricSavings?: MetricSavings
}
```

[LCPBreakdown model](https://github.com/ChromeDevTools/devtools-frontend/blob/011ec3baaaaefa54eba1b41be00caf48ad3e8b51/front_end/models/trace/insights/LCPBreakdown.ts#L72-L116)

**Observed - same source:** When the required events are absent, LCP subpart calculation can return no result. Generation then produces a partial or informative model instead of inventing timings. [subpart and finalization logic](https://github.com/ChromeDevTools/devtools-frontend/blob/011ec3baaaaefa54eba1b41be00caf48ad3e8b51/front_end/models/trace/insights/LCPBreakdown.ts#L111-L116)

**Observed:** Raw models include Maps, trace-event objects, navigation references, and overlay-producing functions. They are internal JavaScript structures, not a stable JSON response contract. chrome-devtools-mcp deliberately exposes formatter output instead. [types.ts](https://github.com/ChromeDevTools/devtools-frontend/blob/011ec3baaaaefa54eba1b41be00caf48ad3e8b51/front_end/models/trace/insights/types.ts#L56-L120), [PerformanceTrace.ts](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/chrome-devtools-mcp-v1.9.0/src/processors/PerformanceTrace.ts#L99-L148)

## 3. Trace capture contract

### Categories

**Observed - chrome-devtools-mcp v1.9.0:** Its DevTools-default category set is:

```text
-*
blink.console
blink.user_timing
devtools.timeline
disabled-by-default-devtools.screenshot
disabled-by-default-devtools.timeline
disabled-by-default-devtools.timeline.frame
disabled-by-default-devtools.timeline.stack
disabled-by-default-v8.cpu_profiler
disabled-by-default-v8.cpu_profiler.hires
latencyInfo
loading
disabled-by-default-lighthouse
v8.execute
v8
```

[v1.9.0 performance source](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/chrome-devtools-mcp-v1.9.0/src/tools/performance.ts#L70-L107)

**Documented - chrome-devtools-mcp changelog:** v1.8.0 switched performance capture to the DevTools default categories and removed the invalidation-tracking category. v1.9.0 raised the tracing buffer to 1.2 GB to match DevTools. [v1.8.0 changes](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/CHANGELOG.md#L224-L264), [v1.9.0 buffer change](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/CHANGELOG.md#L192-L202)

### CDP options

**Observed - Puppeteer 25.10.0, the version pinned by chrome-devtools-mcp v1.9.0:** Puppeteer starts CDP tracing with `transferMode: "ReturnAsStream"` and passes included and excluded category lists through `traceConfig`. [Puppeteer Tracing.ts](https://github.com/puppeteer/puppeteer/blob/puppeteer-v25.10.0/packages/puppeteer-core/src/cdp/Tracing.ts#L84-L114)

**Documented - CDP tip-of-tree:** `Tracing.start` supports the modern `traceConfig`, stream transfer, JSON or protobuf stream formats, compression, and a Perfetto configuration. The older top-level `categories` and `options` parameters are deprecated. [Tracing.start](https://github.com/ChromeDevTools/devtools-protocol/blob/master/pdl/domains/Tracing.pdl#L88-L123)

**Documented - CDP tip-of-tree:** `TraceConfig` provides included and excluded categories and record modes. When no buffer size is supplied, the documented default is 200 MB. [TraceConfig](https://github.com/ChromeDevTools/devtools-protocol/blob/master/pdl/domains/Tracing.pdl#L10-L41)

**Documented - CDP tip-of-tree:** Completion reports whether data was lost and supplies the stream handle when stream transfer is used. A bt implementation must check `dataLossOccurred`; a syntactically valid but truncated trace is not equivalent to a complete engine input. [tracingComplete](https://github.com/ChromeDevTools/devtools-protocol/blob/master/pdl/domains/Tracing.pdl#L154-L174)

### Minimum compatibility target for bt

**Observed:** To reproduce chrome-devtools-mcp v1.9.0 input, bt’s trace verb needs:

- The category set above.
- CDP `Tracing.start` with `traceConfig`.
- Stream transfer.
- A buffer limit matching the 1.2 GB upstream setting.
- Collection through `Tracing.tracingComplete`.
- Rejection or explicit reporting when `dataLossOccurred` is true.
- Output as either a raw event array or an object with `traceEvents`.

[chrome-devtools-mcp parser](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/chrome-devtools-mcp-v1.9.0/src/processors/PerformanceTrace.ts#L13-L77), [CDP Tracing domain](https://github.com/ChromeDevTools/devtools-protocol/blob/master/pdl/domains/Tracing.pdl#L88-L174)

**Observed:** Reloading the page and auto-stopping after five seconds are chrome-devtools-mcp tool semantics. They are not parser requirements. The parser itself consumes trace events and optional metadata. [performance tool](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/chrome-devtools-mcp-v1.9.0/src/tools/performance.ts), [trace parser](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/chrome-devtools-mcp-v1.9.0/src/processors/PerformanceTrace.ts#L13-L77)

## 4. Measured upkeep of the three routes

Release window: 2025-09-22 through 2026-09-22.

| Route | What changes with Chrome | Compatibility record in the window | Fresh-machine requirement | Engine and Chrome mismatch |
|---|---|---|---|---|
| Node component, vendored or optional | **Observed:** Refresh the pinned DevTools source or `@paulirish/trace_engine` snapshot, rebuild the adapter, and accommodate changes in handlers, models, exports, and formatter output. | **Observed:** `@paulirish/trace_engine` published six versions: 0.0.60 through 0.0.65. **Documented:** zero releases identify a breaking-change section. **Unverified:** the actual number of breaks is not measurable from its release record because it has no compatibility changelog and explicitly warns that any upgrade can break consumers. [npm registry](https://registry.npmjs.org/%40paulirish%2Ftrace_engine), [package README](https://www.npmjs.com/package/%40paulirish%2Ftrace_engine) | **Documented:** Node plus a `DOMRect` polyfill. An installed-package form also requires npm/package retrieval. A vendored built bundle removes runtime installation but makes bt responsible for rebuilding and distributing the snapshot. [package README](https://www.npmjs.com/package/%40paulirish%2Ftrace_engine) | **Observed:** Missing expected events can produce partial models; individual insights can enter `modelErrors`; parser-level incompatibilities can throw. **Unverified:** upstream defines no general cross-version compatibility guarantee. [insight types](https://github.com/ChromeDevTools/devtools-frontend/blob/011ec3baaaaefa54eba1b41be00caf48ad3e8b51/front_end/models/trace/insights/types.ts#L56-L120) |
| Python port of metric definitions | **Observed:** Each changed insight must be ported together with the handler, event-normalization, navigation, entity, Lantern, and synthetic-event behavior it uses. The scope is larger than 19 formulas. [trace exports](https://github.com/ChromeDevTools/devtools-frontend/blob/011ec3baaaaefa54eba1b41be00caf48ad3e8b51/front_end/models/trace/trace.ts#L7-L32), [LCP imports and model](https://github.com/ChromeDevTools/devtools-frontend/blob/011ec3baaaaefa54eba1b41be00caf48ad3e8b51/front_end/models/trace/insights/LCPBreakdown.ts#L5-L116) | **Observed:** No upstream Python package or release history was found. Package breaking-change count is therefore not applicable. **Unverified:** semantic drift can only be measured after a port and a cross-engine fixture suite exist. | **Observed:** A pure Python port adds no trace-engine Node installation. Its Python dependencies and wheel policy remain a bt packaging decision. | **Unverified:** Behavior is defined by whichever Chrome traces and engine revision the port’s fixtures cover. New event variants can be ignored, partially modeled, or treated as errors unless ported. There is no upstream compatibility contract for a Python implementation. |
| Shell out to chrome-devtools-mcp or Lighthouse | **Observed:** Upgrade the executable and adapt to its CLI, output, state, audit identifiers, and capture changes. The executable carries its own engine snapshot, so bt does not port algorithms. | **Observed:** chrome-devtools-mcp has zero changelog entries explicitly marked breaking, although it crossed 1.0.0 once. **Observed:** Lighthouse had one breaking major release, v13.0.0, which replaced performance audits with insights, removed artifacts, and raised the Node minimum. [chrome-devtools-mcp changelog](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/CHANGELOG.md), [Lighthouse v13.0.0](https://github.com/GoogleChrome/lighthouse/releases/tag/v13.0.0) | **Documented:** chrome-devtools-mcp v1.9.0 requires Node `^20.19.0`, `^22.12.0`, or `>=23`, npm, and supported Chrome. Lighthouse main requires Node `>=22.19` and Chrome. [mcp package.json](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/chrome-devtools-mcp-v1.9.0/package.json#L101-L103), [CLI installation](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/cli.md#getting-started), [Lighthouse package.json](https://github.com/GoogleChrome/lighthouse/blob/main/package.json#L1-L17) | **Observed:** Engine-package mismatch is hidden inside the executable, but browser mismatch remains. chrome-devtools-mcp only supports its stated Chrome window; Lighthouse releases state their expected Chrome milestone. Upgrading either executable can change findings even when bt does not change. [mcp support policy](https://github.com/ChromeDevTools/chrome-devtools-mcp#requirements), [Lighthouse releases](https://github.com/GoogleChrome/lighthouse/releases) |

### Node component details

**Documented:** `@paulirish/trace_engine` is explicitly unstable and not intended as a supported public library. Its `0.0.x` version numbers do not encode compatibility with a Chrome milestone. [package README](https://www.npmjs.com/package/%40paulirish%2Ftrace_engine)

**Observed:** Vendoring the same DevTools MCP bundle structure used by chrome-devtools-mcp gives bt control over the pin and runtime installation. Installing the snapshot package gives a smaller integration surface, but ties bt to a separately published engine revision that may differ from chrome-devtools-mcp’s submodule revision.

### Python-port details

**Observed:** Insights depend on shared parsing infrastructure. For example, `LCPBreakdown` consumes navigation, metric-score, network-request, timing, and synthetic-event data. Porting only its arithmetic would not reproduce the upstream model. [LCPBreakdown source](https://github.com/ChromeDevTools/devtools-frontend/blob/011ec3baaaaefa54eba1b41be00caf48ad3e8b51/front_end/models/trace/insights/LCPBreakdown.ts)

**Unverified:** No primary upstream source defines a stable language-independent insight specification. DevTools TypeScript source and captured parity fixtures would therefore become the effective specification for a Python port.

### Shell-out details

**Observed - chrome-devtools-mcp current CLI:** `performance_analyze_insight` operates on the daemon’s stored trace by trace index, insight-set ID, and insight name. The public command does not accept an arbitrary bt trace file. [CLI skill command](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/skills/chrome-devtools-cli/SKILL.md), [CLI daemon model](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/cli.md#how-it-works)

**Observed:** Consequently, shelling out to chrome-devtools-mcp would require it to own capture in the same daemon session, or would require an additional unsupported/internal adapter for bt-produced traces.

**Observed - Lighthouse main:** Lighthouse depends on `@paulirish/trace_engine` 0.0.65 and represents parsed trace data and insights in its artifacts. Its stable CLI owns navigation and trace gathering; it is not documented as a general saved-Chrome-trace analyzer. [package dependency](https://github.com/GoogleChrome/lighthouse/blob/main/package.json#L181-L210), [artifact types](https://github.com/GoogleChrome/lighthouse/blob/main/types/artifacts.d.ts)

**Documented - Lighthouse releases:** Lighthouse versions identify an expected Chrome milestone. For example, v13.0.0 targeted Chrome 143. This is an executable release cadence, not a promise that any Lighthouse release will reproduce the insights of any arbitrary Chrome version. [Lighthouse v13.0.0](https://github.com/GoogleChrome/lighthouse/releases/tag/v13.0.0)

## What remains open

- **Unverified:** The exact DevTools Frontend commit pinned by chrome-devtools-mcp v1.9.0.
- **Unverified:** The real number of API-breaking changes across `@paulirish/trace_engine` 0.0.60-0.0.65. Upstream publishes no compatibility changelog, so “zero documented” cannot be converted into “zero actual.”
- **Unverified:** Exact bt optional-extra and dependency-policy constraints from the local `pyproject.toml`. The no-shell research interface did not expose the local file.
- **Unverified:** The amount of built bundle size added by vendoring. It should be measured from a reproducible build of the selected DevTools revision, not estimated from repository size.
- **Unverified:** Cross-version result drift. A fixture matrix is needed: traces from the supported Chrome milestones, run through each candidate engine revision, comparing insight presence, state, savings, warnings, formatter output, and `modelErrors`.
- **Unverified:** Whether bt should expose raw models, normalized JSON, or formatter text. That is an API decision for the grilling ticket, not a fact established here.

No route is selected by these findings.
