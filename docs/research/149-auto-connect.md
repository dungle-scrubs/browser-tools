# Wayfinder 149: attaching bt to the user's running Chrome

## Reframing finding

**Documented:** Chrome 144+'s approval-based remote-debugging service is not a conventional HTTP CDP endpoint. It listens on loopback, but deliberately returns HTTP 404 for `/json`, including `/json/version`, and only admits browser WebSocket paths beginning with `/devtools/browser`. Each WebSocket request enters Chrome's approval flow. [Chromium source blob `45ff52e`, lines 598-605 and 827-867](https://chromium.googlesource.com/chromium/src/+/45ff52e032500bea7703cd4f22723b519fa81fa2/content/browser/devtools/devtools_http_handler.cc#598)

**Verdict:** bt's current HTTP `--endpoint` contract cannot use this endpoint as-is. Enabling one Chrome setting makes Chrome available, but bt still needs a discovery and raw-WebSocket connection path.

## What `--autoConnect` does

1. **Documented:** `CHROME_DEVTOOLS_AXI_AUTO_CONNECT=1` selects chrome-devtools-mcp's `--autoConnect` mode. `CHROME_DEVTOOLS_AXI_CHANNEL` supplies the Chrome channel, with stable as the default. [chrome-devtools-axi 0.1.28, commit `f6a9cb1`, connection-mode documentation](https://github.com/kunchenguid/chrome-devtools-axi/blob/f6a9cb1/AGENTS.md#L38-L47)

2. **Documented:** chrome-devtools-mcp requires Chrome 144+ to be running and Remote Debugging to be enabled at `chrome://inspect/#remote-debugging`. Chrome asks the user to allow every incoming connection and shows an automation banner while connected. [Chrome documentation, updated 2025-12-16, “How it works” and “Get started”](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session#how-it-works)

3. **Observed in source:** With `--autoConnect` and no explicit user-data directory, chrome-devtools-mcp passes the selected channel to `puppeteer.connect()`. With an explicit user-data directory, it reads that directory's `DevToolsActivePort`, validates the port and path, constructs the loopback WebSocket URL, and passes it to `puppeteer.connect()`. [chrome-devtools-mcp tag `chrome-devtools-mcp-v1.9.0`, `src/browser.ts`, lines 28-109](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/chrome-devtools-mcp-v1.9.0/src/browser.ts#L28-L109)

4. **Documented:** Puppeteer 25.11.0's `ConnectOptions.channel` locates the channel's default user-data directory, finds its active port, and connects to `ws://localhost:$ActivePort/devtools/browser`. It does not discover this connection through `/json/version`. [Puppeteer 25.11.0 `ConnectOptions.channel`, lines 263 and 287-301](https://pptr.dev/api/puppeteer.connectoptions#properties)

5. **Documented:** Chrome writes `DevToolsActivePort` into the user-data directory. Its first line is the listening port and its second line is the browser WebSocket path. [Chromium source blob `45ff52e`, lines 305-322](https://chromium.googlesource.com/chromium/src/+/45ff52e032500bea7703cd4f22723b519fa81fa2/content/browser/devtools/devtools_http_handler.cc#305)

6. **Documented:** The effective connection is therefore a loopback WebSocket such as `ws://127.0.0.1:<port>/devtools/browser` or the fuller path stored on the file's second line. Approval mode accepts any path with the `/devtools/browser` prefix and rejects other WebSocket paths. [Chromium source blob `45ff52e`, lines 827-867](https://chromium.googlesource.com/chromium/src/+/45ff52e032500bea7703cd4f22723b519fa81fa2/content/browser/devtools/devtools_http_handler.cc#827)

This is a CDP browser connection over WebSocket. The normal CDP discovery flow uses `/json/version` to obtain `webSocketDebuggerUrl`, but approval mode intentionally disables that flow. [Chrome DevTools Protocol, “How do I access the browser target?”](https://chromedevtools.github.io/devtools-protocol/#how-do-i-access-the-browser-target)

## Can bt use the endpoint as-is?

**Documented:** No. browser-tools 0.6.0 documents `--endpoint` as a loopback HTTP endpoint such as `http://127.0.0.1:9222`. [browser-tools 0.6.0 package documentation](https://pypi.org/project/browser-tools/0.6.0/#description)

**Documented:** Approval mode returns 404 before processing every `/json` command, including `/json/version`. It also returns 404 for the discovery page and frontend resources. [Chromium source blob `45ff52e`, lines 598-605 and 774-805](https://chromium.googlesource.com/chromium/src/+/45ff52e032500bea7703cd4f22723b519fa81fa2/content/browser/devtools/devtools_http_handler.cc#598)

**Documented:** bt needs both:

- Discovery of the channel's default user-data directory and its `DevToolsActivePort`.
- A direct browser WebSocket connection using the discovered port and path.

There is no well-known macOS socket in this flow. Chrome starts a loopback TCP server. It normally tries port 9222, falls back when that port is occupied, and records the actual endpoint in `DevToolsActivePort`. [Chromium commit `cec7af242a1ec8b33becd04582467cddb2902a26`, lines 33-40 and 123-186](https://chromium.googlesource.com/chromium/src/+/cec7af242a1ec8b33becd04582467cddb2902a26%5E%21/#33)

A `/json/version`-404 fallback to `ws://host:port/devtools/browser` is useful only after bt already knows the correct port. It does not solve discovery when Chrome had to move away from 9222.

## One-time machine setup and restart behavior

**Documented:** The one-time user setup is:

1. Open `chrome://inspect/#remote-debugging`.
2. Enable Remote Debugging.

Chrome 144+ then accepts connection requests from local clients. Each new connection still requires the user to click Allow. [Chrome configuration documentation, updated 2026-06-29, lines 83-116](https://developer.chrome.com/docs/devtools/agents/get-started/configuration#connect-to-an-existing-browser-session)

**Documented:** Chrome stores this checkbox as the application-wide Local State preference `devtools.remote_debugging.user-enabled`. It is global to Chrome rather than profile-specific because the debugging server is global. [Chromium commit snapshot `8115afe5`, `pref_names.h`, lines 1746-1758](https://chromium.googlesource.com/chromium/src/+/8115afe5c79963325c7ef8eebb733ecc3b4dd0fc/chrome/common/pref_names.h#1746)

**Documented:** The setting survives normal Chrome restarts because it is a persisted Local State preference. An administrator policy can disable the feature through `devtools.remote_debugging.allowed`. [Chromium commit `27485803c25baad7e0aeb4c4368403d10003dc53`, lines 251-286](https://chromium.googlesource.com/chromium/src/+/27485803c25baad7e0aeb4c4368403d10003dc53%5E%21/#251)

**Documented:** Chrome also reads the previous `DevToolsActivePort` on startup and attempts to reuse its first-line port. If the file is absent or invalid, it starts from port 9222 and may fall back to another available port. [Chromium commit `cec7af242a1ec8b33becd04582467cddb2902a26`, lines 101-120 and 162-186](https://chromium.googlesource.com/chromium/src/+/cec7af242a1ec8b33becd04582467cddb2902a26%5E%21/#101)

bt should still reread the file for every new connection. The file is discovery state, not a port that bt should cache permanently.

## Fact-or-choice verdict

### Established fact

bt needs a new direct-WebSocket discovery path. The Chrome setting alone cannot make the current HTTP `--endpoint` path work because Chrome deliberately suppresses `/json/version`.

### Remaining design choice

1. **Recommended: add first-class auto-connect discovery.**

   Add `--auto-connect`, optionally with `--channel`. Resolve the platform's default Chrome user-data directory, read both lines of `DevToolsActivePort`, and connect directly to that WebSocket. This matches chrome-devtools-mcp and Puppeteer, handles port fallback, and gives callers the same result as `CHROME_DEVTOOLS_AXI_AUTO_CONNECT=1`.

2. **Add a raw WebSocket endpoint form.**

   Let `--endpoint` accept `ws://127.0.0.1:<port>/devtools/browser...`. This is a useful low-level capability, but it leaves port and path discovery to the caller and does not provide axi parity by itself.

3. **Add an HTTP-404 WebSocket fallback.**

   When `/json/version` returns 404, try `/devtools/browser` on the same host and port. This complements option 1 or 2, but cannot replace discovery because the actual port is not guaranteed to be 9222.

The recommended design is option 1 plus raw WebSocket support as an internal connection primitive. This holds browser-tools' existing attach scope. Its ongoing upkeep is the platform mapping for Chrome channel user-data directories and validation of the two-line port file.

## What remains open

**Unverified locally:** This run could not reread `/Users/kevin/Library/Application Support/Google/Chrome/DevToolsActivePort` or inspect the structure of `Local State`. The ticket's report that the file is currently absent therefore remains unverified.

If Remote Debugging is enabled and Chrome 153 is running, absence of the file conflicts with the Chromium source path described above and needs a local-state check. The first local checks should be:

- Whether `devtools.remote_debugging.user-enabled` exists and is true.
- Whether `devtools.remote_debugging.allowed` is false or policy-controlled.
- Whether the stable Chrome user-data directory differs from the assumed path.
- Whether another port file is created after toggling Remote Debugging off and on or restarting Chrome.
