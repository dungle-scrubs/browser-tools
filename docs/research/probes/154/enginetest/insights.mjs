// Feed a bt-captured trace to the real DevTools trace engine and list the
// insights it produces. This is the acceptance test for the capture contract:
// if the engine parses this file and returns insight models, bt's trace verb
// produces a valid input.
//
// Usage: node insights.mjs <trace.json>
import { readFileSync } from 'node:fs';

// The engine touches DOMRect; Node has no DOM.
if (typeof globalThis.DOMRect === 'undefined') {
  globalThis.DOMRect = class DOMRect {
    constructor(x = 0, y = 0, width = 0, height = 0) {
      this.x = x; this.y = y; this.width = width; this.height = height;
      this.top = y; this.left = x; this.right = x + width; this.bottom = y + height;
    }
  };
}

const TraceModel = await import('@paulirish/trace_engine');

const raw = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const events = Array.isArray(raw) ? raw : raw.traceEvents;

const processor = TraceModel.Processor.TraceProcessor.createWithAllHandlers();
await processor.parse(events, {});
const parsed = processor.parsedTrace ?? processor.data;

const insightSets = parsed?.insights ?? processor.insights;
const out = { events: events.length, insightSets: 0, insights: {}, errors: {} };

if (insightSets && typeof insightSets.values === 'function') {
  for (const set of insightSets.values()) {
    out.insightSets += 1;
    out.url = set.url?.toString?.() ?? String(set.url ?? '');
    for (const [key, model] of Object.entries(set.model ?? {})) {
      if (model instanceof Error) { out.errors[key] = model.message; continue; }
      out.insights[key] = {
        state: model?.state ?? null,
        hasSavings: Boolean(model?.metricSavings),
      };
    }
  }
}
console.log(JSON.stringify(out, null, 2));
