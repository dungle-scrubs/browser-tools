# Passthrough recipes

The `browser-tools` CLI has no curated `snapshot` / `click` / `fill` /
`screenshot` verbs today. Everything below is done through the raw-protocol
passthrough:

```
[INSTANCE] Domain.method '{...json params...}' [--target SPEC]
```

`INSTANCE` is omittable when exactly one instance runs. Params are a single JSON
string. Read the live schema for any domain with `help [INSTANCE] Domain`.

## Orient first

```bash
# What instances exist, with their page targets (index, id, url, title)?
browser-tools status

# What does this browser's protocol actually offer for a domain?
browser-tools help Page
browser-tools help DOM.getDocument
```

## Navigate

```bash
browser-tools Page.navigate '{"url": "https://example.com"}'
```

## Read the page (a snapshot, by hand)

```bash
# Full DOM tree
browser-tools DOM.getDocument '{"depth": -1, "pierce": true}'

# Rendered text / accessibility tree
browser-tools Accessibility.getFullAXTree '{}'

# Evaluate arbitrary JS and get the result
browser-tools Runtime.evaluate '{"expression": "document.title", "returnByValue": true}'
```

## Interact (click / fill, by hand)

There is no UID-addressed click verb yet. Drive interaction through CDP:

```bash
# Click by dispatching a synthetic input event at coordinates
browser-tools Input.dispatchMouseEvent '{"type": "mousePressed", "x": 120, "y": 240, "button": "left", "clickCount": 1}'
browser-tools Input.dispatchMouseEvent '{"type": "mouseReleased", "x": 120, "y": 240, "button": "left", "clickCount": 1}'

# Fill a field via JS
browser-tools Runtime.evaluate '{"expression": "document.querySelector(\"#email\").value = \"a@b.com\"", "returnByValue": true}'
```

## Screenshot (by hand)

```bash
browser-tools Page.captureScreenshot '{"format": "png"}'   # base64 data in the JSON result
```

## Wait for something to happen

Prefer the `wait` verb over polling:

```bash
# Block until the next page load fires (30s default deadline)
browser-tools wait --event Page.loadEventFired

# Block until a network response whose JSON mentions "/api/checkout"
browser-tools wait --event Network.responseReceived --match "/api/checkout" --timeout 15
```

## Stream events live

```bash
# Console + network as JSON lines until you stop it
browser-tools attach +Runtime.consoleAPICalled +Network.requestWillBeSent

# Short, bounded collectors
browser-tools console-list --duration 3
browser-tools network-list --duration 3
```

## Why passthrough covers everything

Any method the installed browser supports works through the passthrough without
a curated verb existing for it. When a dedicated verb does not exist, read the
live schema (`help INSTANCE Domain`) and send the method directly.
