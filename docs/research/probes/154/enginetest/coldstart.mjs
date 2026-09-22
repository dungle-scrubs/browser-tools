// Measure what a Node insights adapter costs per invocation: process start,
// engine import, trace parse, insight generation. This is the number the
// ticket-155 analysis flagged as unverified and said to measure.
import { readFileSync } from 'node:fs';

const t0 = performance.now();

if (typeof globalThis.DOMRect === 'undefined') {
  globalThis.DOMRect = class DOMRect {
    constructor(x = 0, y = 0, w = 0, h = 0) {
      this.x = x; this.y = y; this.width = w; this.height = h;
      this.top = y; this.left = x; this.right = x + w; this.bottom = y + h;
    }
  };
}

const TraceModel = await import('@paulirish/trace_engine');
const tImport = performance.now();

const raw = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const events = Array.isArray(raw) ? raw : raw.traceEvents;
const tRead = performance.now();

const processor = TraceModel.Processor.TraceProcessor.createWithAllHandlers();
await processor.parse(events, {});
const parsed = processor.parsedTrace ?? processor.data;
const sets = parsed?.insights ?? processor.insights;
let models = 0;
for (const set of sets.values()) models += Object.keys(set.model ?? {}).length;
const tDone = performance.now();

console.log(JSON.stringify({
  events: events.length,
  models,
  importMs: Math.round(tImport - t0),
  readMs: Math.round(tRead - tImport),
  parseMs: Math.round(tDone - tRead),
  inProcessTotalMs: Math.round(tDone - t0),
}));
