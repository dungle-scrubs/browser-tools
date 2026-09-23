import { readFileSync } from 'node:fs';
import { resolveTextTokens } from './formatter_compat.js';
import { millis } from './vendor/UnitFormatters.js';

const packageData = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'));
const engineRevision = packageData.dependencies['@paulirish/trace_engine'];
const validatedVersions = ['151.0.7922.34', '153.0.0.0'];
const document = {engineRevision, browserVersion: null, navigations: []};

function fail(message, code = 1) {
  console.log(JSON.stringify({...document, error: message}));
  process.exitCode = code;
}

function producerVersion(raw, events) {
  const candidates = [raw.metadata?.['product-version'], raw.metadata?.['user-agent'], raw.metadata?.browserVersion];
  for (const event of events) {
    if (event.cat === '__metadata' || event.name === 'TracingStartedInBrowser') {
      candidates.push(event.args?.data?.browserVersion, event.args?.data?.['product-version'], event.args?.['product-version']);
    }
  }
  for (const candidate of candidates) {
    if (typeof candidate !== 'string') continue;
    const product = candidate.match(/(?:HeadlessChrome|Chrome|Chromium)\/\d+\.\d+\.\d+\.\d+/);
    if (product) return product[0];
    if (/^\d+\.\d+\.\d+\.\d+$/.test(candidate)) return `Chrome/${candidate}`;
  }
  return null;
}

async function analyze() {
  const [path, selected, ignore] = process.argv.slice(2);
  const raw = JSON.parse(readFileSync(path, 'utf8'));
  const events = Array.isArray(raw) ? raw : raw?.traceEvents;
  if (!Array.isArray(events) || events.some(event => !event || typeof event !== 'object' || typeof event.name !== 'string')) {
    return fail('Invalid trace: expected a traceEvents array of event objects');
  }
  document.browserVersion = producerVersion(raw, events);
  if (!validatedVersions.includes(document.browserVersion?.split('/')[1]) && ignore !== 'true') {
    return fail(`Unsupported trace browser version ${document.browserVersion ?? '(metadata missing)'}. Engine ${engineRevision} is validated only with Chrome ${validatedVersions.join(' or ')}; the fixture matrix does not exist. Capture with that build, or pass --ignore-engine-mismatch to accept unvalidated results.`);
  }
  globalThis.DOMRect ??= class DOMRect {
    constructor(x = 0, y = 0, width = 0, height = 0) {
      Object.assign(this, {x, y, width, height, top: y, left: x, right: x + width, bottom: y + height});
    }
  };
  const Trace = await import('@paulirish/trace_engine');
  const {PerformanceInsightFormatter} = await import('./vendor/PerformanceInsightFormatter.js');
  const names = Object.keys(Trace.Insights.Models).sort();
  if (selected && !names.includes(selected)) return fail(`Unknown insight '${selected}'. Valid names: ${names.join(', ')}`, 2);
  Trace.Helpers.SyntheticEvents.SyntheticEventsManager.createAndActivate(events);
  const processor = Trace.Processor.TraceProcessor.createWithAllHandlers();
  await processor.parse(events, {insightTimeFormatters: {milli: millis}});
  const data = processor.data;
  // Bridge the pinned formatter's old map to the engine's corrected initiator lookup.
  const eventToInitiator = new Map(data.NetworkRequests.byTime.map(request =>
    [request, Trace.Extras.Initiators.getNetworkInitiator(data, request)]));
  const formatterData = {...data, NetworkRequests: {...data.NetworkRequests, eventToInitiator}};
  const sets = processor.insights;
  if (!sets?.size) return fail('Zero Insight Sets: this trace contains no supported navigation. Capture a Page.navigate step inside bt trace --steps FILE, then wait for load and drive the interaction before the trace ends. A duration capture on an already loaded page is insufficient.');
  resolveTextTokens(sets);
  const errors = [];
  for (const set of sets.values()) {
    for (const [key, error] of Object.entries(set.modelErrors ?? {})) {
      if (!selected || key === selected) errors.push(`${key}: ${error.message}`);
    }
    const insights = [];
    const focus = {parsedTrace: {data: formatterData, insights: sets}, eventsSerializer: new Trace.EventsSerializer.EventsSerializer()};
    for (const [key, model] of Object.entries(set.model)) {
      if (selected && key !== selected) continue;
      if (model instanceof Error) {
        errors.push(`${key}: ${model.message}`);
        continue;
      }
      const title = String(model.title);
      const formatter = new PerformanceInsightFormatter(focus, {...model, title});
      const savings = Object.entries(model.metricSavings ?? {}).filter(([metric, ms]) => metric !== 'CLS' && Number.isFinite(ms) && ms > 0).sort((a, b) => b[1] - a[1]);
      insights.push({key, state: model.state, title,
        savings: savings.length ? {metric: savings[0][0], ms: savings[0][1]} : null,
        detail: formatter.formatInsight()});
    }
    document.navigations.push({url: String(set.url ?? ''), insights});
  }
  if (errors.length) return fail(`Insight model errors: ${errors.join('; ')}`);
  console.log(JSON.stringify(document));
}

try {
  await analyze();
} catch (error) {
  fail(`Cannot analyze trace: ${error instanceof Error ? error.message : String(error)}`);
}
