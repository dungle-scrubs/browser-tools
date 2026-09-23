# Pinned upstream rendering

Source: ChromeDevTools/devtools-frontend commit
`011ec3baaaaefa54eba1b41be00caf48ad3e8b51`, BSD-3-Clause (LICENSE).

The standalone trace_engine package omits DevTools' insight text formatter.
This directory contains its PerformanceInsightFormatter, UnitFormatters,
and only the network-rendering methods they use from PerformanceTraceFormatter
and NetworkRequestFormatter. No model verdicts or text templates are rewritten.
CharacterSet has no dedicated detail renderer at this formatter revision;
its model state and title are still included.

Regenerate with Node >=22.13 using `scripts/vendor_insight_formatter.mjs`.
Supply a directory containing LICENSE from that commit's root and the four
named TypeScript sources from
`front_end/models/ai_assistance/data_formatters/`. The script strips types,
rewrites module paths and extracts the required methods. Generated JavaScript
runs without TypeScript or a build tool on the user's machine.

The handwritten formatter_compat.js supplies font URL names and English text
for the standalone engine's i18n tokens. adapter.mjs supplies the old
network-initiator map through the current engine's corrected lookup.
DevTools annotations are disabled because there is no annotation repository
in an offline file invocation. Refresh these bridges when changing either pin.
