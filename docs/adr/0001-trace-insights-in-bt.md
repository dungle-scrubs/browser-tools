---
status: accepted
date: 2026-09-22
---

# bt provides trace insights, accepting a component that tracks Chrome releases

browser-tools is Python with one runtime dependency, `websockets`. Trace insights, the named findings that attribute a Web Vital to its cause (LCP phase breakdown, render-blocking requests, layout shift culprits, long task attribution), come from Chrome's trace engine: TypeScript, versioned with Chrome, exposed as an internal DevTools API. Carrying them in bt means either a Node component pinned to a Chrome version or a Python port of the metric logic, and either one changes when Chrome changes.

Measured use through chrome-devtools-axi was 8 `perf-insight` calls in one project. Lighthouse 12 and later run the same insight audits over a page load, so a Lighthouse recipe covers the load case with no code in bt.

The driving dev decided to build them anyway. The reason: when a Web Vital regression appears in an interaction trace, knowing exactly what caused it is the point of capturing the trace, and a raw trace file only answers that with a human in the DevTools Performance panel. The load-only coverage from Lighthouse does not reach the interaction case from a one-line command.

## Considered options

- Drop insights. Capture traces in bt; analyse with Lighthouse for loads and a Lighthouse user-flow script for interactions. Rejected as above.
- A Node component under an optional extra, pinned to a Chrome version.
- A Python port of the metric definitions.

Which of the last two is built, and how it is packaged, is decided by the retirement map's ticket on insights. This record fixes only that bt carries insights and why.

## Consequences

- bt gains a component whose correctness depends on the installed Chrome version. Every Chrome update is a version to test against.
- The base install stays `websockets`-only; insights are expected to live behind an optional extra, which the implementing ticket confirms.
- chrome-devtools-axi can be retired without losing this capability.

## What the research established after this decision

Two tickets on the retirement map reported after this record was written. Neither changes the decision; both sharpen it.

[#150](https://github.com/dungle-scrubs/browser-tools/issues/150) found that the engine is not an ordinary dependency. chrome-devtools-mcp pins the DevTools Frontend as a Git submodule and bundles it; the separately published `@paulirish/trace_engine` snapshot that Lighthouse uses calls itself unstable and not for public consumption. So "a component that tracks Chrome releases" is concretely a pinned Frontend revision or an explicitly unstable package, with no upstream compatibility contract either way.

[#154](https://github.com/dungle-scrubs/browser-tools/issues/154) tested the capability end to end. The engine parsed a trace captured by a bt prototype and returned 19 of 19 insight models with zero model errors. A trace wrapping a driven click produced the interaction breakdown this decision was taken for, which a load-only capture does not report. The Node route is therefore demonstrated rather than assumed, and the interaction case is reachable.
