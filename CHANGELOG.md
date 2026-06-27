# Changelog

## 0.1.0

### Added
- Chrome DevTools MCP wrapper with snapshot-based page automation
- Persistent browser sessions with named profiles and daemon-based MCP reuse
- Frame-aware CDP tools: frame tree management, execution context resolution, storage inspection
- Interstitial detection for Cloudflare, DataDome, Akamai, PerimeterX, Imperva, AWS WAF challenge pages
- Camoufox anti-detect browsing with Firefox-based fingerprint injection
- CPU profiling via direct CDP (threshold-triggered and timed capture)
- Stealth.js injection via `Page.addScriptToEvaluateOnNewDocument`
- Screenshot blank-frame detection and retry (Pillow + PNG compression ratio)
- Screencast capture (Page.startScreencast / Page.screencastFrame)
- Accessibility tree tools (ax_find, ax_node)
- Content extraction tools (get_text, get_html, get_attr)
- Element query tools (element_exists, element_visible)
- Semantic wait tools (wait_idle, wait_stable)
- Page export tools (export_pdf, screenshot_element)
- Inspect mode for read-only observation
- Unix domain socket daemon client for persistent MCP reuse
- Project browser configuration via `.browser-tools.json`
- Live profile discovery and auto-attach to running Chrome instances
