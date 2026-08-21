# tool-proxy retirement note (RFC-01 Phase 4)

RFC-01 "Out of tool-proxy" retires the tool-proxy `browser-tools` app together
with the global "route browser automation through tool-proxy" instruction. The
optional MCP front stays available for harnesses that cannot run a CLI; agents
that can run a CLI use `browser-tools` / `bt` (and the `browser-tools` agent
skill) directly.

**This note is a hand-off for a human. It changes nothing outside this
repository.** Both targets below live in separate repositories and were
inspected read-only. Do not let an agent edit them as part of RFC-01 #48.

Verify each reference is still current before deleting; line numbers are from
inspection on 2026-08-21 and may shift.

---

## 1. tool-proxy `browser-tools` app -- delete

**Repository:** `~/dev/tool-proxy` (separate repo)
**App directory:** `~/dev/tool-proxy/apps/browser-tools/`

### 1a. Delete the whole app directory

```
~/dev/tool-proxy/apps/browser-tools/
├── app.config.json      # discovery metadata + triggers for "Browser Automation"
├── CLAUDE.md            # app instructions ("When to Use vs Playwright", etc.)
├── context.md          # indexed context
├── README.md           # adapter readme (points at ~/dev/browser-tools)
├── tools.json          # ~41 KB of tool schemas
├── scripts/
│   └── browser_tools_session.py   # adapter shim into browser_tools.browser_tools_session
└── tests/
    └── test_adapter.py
```

The adapter is only a schema/discovery shim; the runtime, CLI, daemon, CDP
helpers, and Camoufox session already live in the standalone package at
`~/dev/browser-tools`, so deleting the app removes no browser capability.

### 1b. Remove the app's dependency wiring in `~/dev/tool-proxy/pyproject.toml`

- Line ~8: the `"browser-tools",` entry in `[project].dependencies`.
- Line ~41: `browser-tools = { path = "../browser-tools", editable = true }`
  under `[tool.uv.sources]`.

After editing, refresh the lockfile (`uv lock`) so `uv.lock` drops the
`browser-tools` entry rather than being hand-edited.

### 1c. Remove the canary wiring in `~/dev/tool-proxy/canary/canary.config.yaml`

- Lines ~125-127: the skip stanza
  ```yaml
    browser-tools:
      skip: true
      reason: "Requires running browser"
  ```
- Lines ~198-212: the `subprocess_mcp` entry `- app: browser-tools` with its
  `command: ["npx", "-y", "chrome-devtools-mcp@latest", ...]` and its
  `ignore_tools:` list.
- Lines ~223-224: the comment noting playwright is omitted because browser-tools
  is preferred -- reword or drop it once the app is gone.

### 1d. Update discovery/validation tests in `~/dev/tool-proxy/service/`

These assert browser-tools ownership and will fail once the app is removed;
update or delete the cases:

- `service/src/__tests__/integration/discovery.test.ts` -- lines ~108-109,
  ~165-166, ~271-272 map queries ("chrome devtools performance trace", "debug
  javascript in the browser", "take screenshot of the page", "test website
  checkout flow", "test website form submission") to the `browser-tools` app and
  its tools.
- `service/src/__tests__/add-app-validation.test.ts` -- line ~26 uses
  `"browser-tools"` as a valid app-name fixture; swap in another app name.

### 1e. Prune docs mentions (optional, non-blocking)

- `~/dev/tool-proxy/README.md` line ~157: the Web-category row
  `| Web | firecrawl, web-search, playwright, browser-tools |` -- drop
  `browser-tools`.
- `~/dev/tool-proxy/docs/adr/0001-extraction-criteria-for-standalone-apps.md`
  line ~5 names browser-tools as an extraction candidate; leave as history or add
  a note that extraction completed and the app was retired.

---

## 2. Global routing instruction -- remove/rewrite

**File:** `~/.agents/GLOBAL.md` (outside this repo)

### 2a. Delete the "Browser Automation" section

Remove this section verbatim (currently lines ~219-223):

```markdown
## Browser Automation

If the tool-proxy MCP is available, handle browser automation through its
`browser-tools` integration instead of direct browser-control CLIs or ad hoc
automation scripts.
```

### 2b. Replacement

Agents drive the browser through the CLI and the skill directly. If any standing
instruction is wanted in its place, use something like:

```markdown
## Browser Automation

Handle browser automation with the `browser-tools` CLI (`browser-tools` / `bt`)
and its agent skill (`~/.agents/skills/browser-tools`). The optional MCP front
remains only for harnesses that cannot run a CLI.
```

If no standing instruction is wanted, delete the section outright -- the skill's
own description is enough for discovery.

### 2c. Remove the "browser automation" trigger word from the Tool Proxy section

`~/.agents/GLOBAL.md` line ~214 lists `browser automation` among the tool-proxy
`discover_tools` triggers:

```
Common triggers: GitHub, Vercel, web scraping, pen.dev, browser automation,
Algolia, GA4, GCS, Google Search Console, GTM, image generation, Intercom,
...
```

Drop `browser automation,` from that list so browser work no longer routes into
tool-proxy discovery.

---

## Order and rollback

Do the GLOBAL.md edit (section 2) and the tool-proxy edits (section 1) together,
since the routing instruction and the app are a pair. After the tool-proxy edits,
run its test suite and `uv lock` so the removed app leaves the workspace clean.
Both repos are version-controlled; revert the respective commits to roll back.
Nothing here touches `~/dev/browser-tools`.
