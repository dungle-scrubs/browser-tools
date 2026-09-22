// Show what the engine says about the interaction in a wrapped-run trace.
import { readFileSync } from 'node:fs';

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
const sets = parsed?.insights ?? processor.insights;

for (const set of sets.values()) {
  const inp = set.model?.INPBreakdown;
  const cls = set.model?.CLSCulprits;
  const lcp = set.model?.LCPBreakdown;
  const out = {
    INPBreakdown: {
      state: inp?.state ?? null,
      longestInteractionMs: inp?.longestInteractionEvent?.dur != null
        ? Math.round(inp.longestInteractionEvent.dur / 1000) : null,
      interactionType: inp?.longestInteractionEvent?.type ?? null,
      subparts: inp?.longestInteractionEvent ? {
        inputDelayMs: Math.round((inp.longestInteractionEvent.inputDelay ?? 0) / 1000),
        processingDurationMs: Math.round((inp.longestInteractionEvent.mainThreadHandling ?? 0) / 1000),
        presentationDelayMs: Math.round((inp.longestInteractionEvent.presentationDelay ?? 0) / 1000),
      } : null,
      allInteractions: inp?.highestInpEvent ? 1 : (inp?.longestInteractionEvent ? 1 : 0),
    },
    CLSCulprits: {
      state: cls?.state ?? null,
      shifts: cls?.shifts?.size ?? cls?.shifts?.length ?? null,
    },
    LCPBreakdown: { state: lcp?.state ?? null, lcpMs: lcp?.lcpMs != null ? Math.round(lcp.lcpMs) : null },
  };
  console.log(JSON.stringify(out, null, 2));
}
