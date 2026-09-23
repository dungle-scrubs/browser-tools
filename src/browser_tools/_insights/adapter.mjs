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
    // The product token has to stand on its own. An unanchored match let any
    // string that merely ends in Chrome/N.N.N.N, such as a host name in a
    // forged user-agent, pass the validated-version gate.
    const product = candidate.match(/(?:^|\s)((?:HeadlessChrome|Chrome|Chromium)\/\d+\.\d+\.\d+\.\d+)(?=\s|$)/);
    if (product) return product[1];
    if (/^\d+\.\d+\.\d+\.\d+$/.test(candidate)) return `Chrome/${candidate}`;
  }
  return null;
}

const CAPTURE_REMEDY = 'Capture a Page.navigate step inside bt trace --steps FILE, then wait for load and drive the interaction before the trace ends.';

// Why this trace produced no Insight Set. The engine builds one per outermost
// main-frame navigation that commits a document, so each way of having none
// leaves its own mark in the events. One fixed string named a missing
// Page.navigate for every one of them, which told a person who had captured
// the navigate step to capture it again.
function zeroSetsDiagnostic(events) {
  const starts = events.filter(event => event.name === 'navigationStart');
  const committed = starts.filter(event => event.args?.data?.documentLoaderURL);
  const mainFrame = committed.filter(event => event.args?.data?.isOutermostMainFrame);
  if (mainFrame.length) {
    const urls = [...new Set(mainFrame.map(event => event.args.data.documentLoaderURL))];
    return `Zero Insight Sets: this trace commits ${mainFrame.length} main-frame navigation(s) (${urls.join(', ')}), but the engine built no Insight Set from them. The capture is not missing a navigation step. Check dataLossOccurred in the bt trace document, and capture the whole load window.`;
  }
  if (committed.length) {
    return `Zero Insight Sets: this trace commits ${committed.length} navigation(s), but none of them is an outermost main frame. Only a top-level navigation produces an Insight Set; a subframe navigation does not. ${CAPTURE_REMEDY}`;
  }
  if (starts.length) {
    return `Zero Insight Sets: this trace has ${starts.length} navigationStart event(s), but none of them committed a document (documentLoaderURL is empty), so there is no navigation to analyze. The navigation failed, or the trace ended before the document committed. Check the navigation's own result, and end the capture on wait-text or wait-stable rather than a fixed --duration.`;
  }
  const documentRequest = events.some(event =>
    event.name === 'ResourceSendRequest' && event.args?.data?.resourceType === 'Document');
  if (documentRequest) {
    return 'Zero Insight Sets: this trace records a document request but no navigationStart event. navigationStart is in the blink.user_timing trace category, which this capture did not include. Re-capture without --categories, or keep blink.user_timing and loading in the list you pass.';
  }
  return `Zero Insight Sets: this trace records no navigation at all - no navigationStart event and no document request. ${CAPTURE_REMEDY} A duration capture on an already loaded page is insufficient. If you narrowed --categories, keep blink.user_timing: navigationStart is in it.`;
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
  if (!sets?.size) return fail(zeroSetsDiagnostic(events));
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
  // Chrome commits its own error document when a navigation fails, and the
  // engine analyses it like any other page. Exit 0 over chrome-error:// read
  // as a clean report of a site that was never reached.
  const errorPages = document.navigations.filter(navigation => navigation.url.startsWith('chrome-error:'));
  if (errorPages.length) return fail(`Navigation failed: ${errorPages.length} of ${document.navigations.length} navigation(s) committed Chrome's error document (${[...new Set(errorPages.map(navigation => navigation.url))].join(', ')}). The requested page never loaded, so these insights describe the error page. Read the navigate step's errorText in the bt trace Run Document.`);
  if (errors.length) return fail(`Insight model errors: ${errors.join('; ')}`);
  console.log(JSON.stringify(document));
}

try {
  await analyze();
} catch (error) {
  fail(`Cannot analyze trace: ${error instanceof Error ? error.message : String(error)}`);
}
