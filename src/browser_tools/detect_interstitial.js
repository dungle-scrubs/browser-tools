/**
 * Interstitial detection script for browser-tools.
 *
 * Multi-signal heuristic detection (D-003) that identifies:
 * - Cloudflare challenges (by title pattern + meta tags)
 * - ngrok warnings (by content pattern)
 * - Auth walls / login pages
 * - Generic challenge pages
 * - DataDome (cookies, script sources, challenge iframes)
 * - Akamai Bot Manager (_abck cookie + empty page, reference numbers)
 * - PerimeterX / HUMAN (_px* cookies, script sources, challenge text)
 * - Imperva / Incapsula (session cookies, script sources, body text)
 * - AWS WAF (aws-waf-token cookie, CAPTCHA elements)
 *
 * Returns a JSON string with detection results.
 * Each detection has a confidence level: "high", "medium", or "low".
 *
 * This script is injected via Runtime.evaluate after navigation.
 * A project-specific override can be placed at:
 *   ~/.config/tool-proxy/browser-tools/detect-interstitial.js
 */
(function detectInterstitials() {
  const results = [];
  const title = document.title || '';
  const bodyText = (document.body && document.body.innerText) || '';
  const metaTags = Array.from(document.querySelectorAll('meta'));

  // --- Cloudflare Challenge ---
  // Title-based detection (most stable, highest confidence)
  if (/just a moment/i.test(title) || /attention required/i.test(title)) {
    results.push({
      type: 'cloudflare_challenge',
      confidence: 'high',
      signal: 'title_pattern',
      details: 'Page title matches Cloudflare challenge pattern: "' + title + '"'
    });
  }
  // Meta tag detection
  const cfMeta = metaTags.find(m =>
    m.getAttribute('name') === 'captcha-bypass' ||
    (m.getAttribute('content') || '').includes('cf-browser-verification')
  );
  if (cfMeta) {
    results.push({
      type: 'cloudflare_challenge',
      confidence: 'high',
      signal: 'meta_tag',
      details: 'Cloudflare verification meta tag detected'
    });
  }
  // CSS selector detection (low confidence — DOM structure may change)
  if (document.querySelector('#cf-wrapper') || document.querySelector('.cf-browser-verification')) {
    results.push({
      type: 'cloudflare_challenge',
      confidence: 'low',
      signal: 'css_selector',
      details: 'Cloudflare DOM elements detected (may be false positive)'
    });
  }

  // --- ngrok Warning ---
  if (/ngrok/i.test(title) && /free/i.test(title)) {
    results.push({
      type: 'ngrok_warning',
      confidence: 'high',
      signal: 'title_pattern',
      details: 'ngrok free tier warning page'
    });
  }
  if (/ERR_NGROK/i.test(bodyText) || /visit the site at/i.test(bodyText) && /ngrok/i.test(bodyText)) {
    results.push({
      type: 'ngrok_warning',
      confidence: 'medium',
      signal: 'body_text',
      details: 'ngrok error or warning content detected'
    });
  }
  // ngrok interstitial button
  if (document.querySelector('button[id="ngrok"]') || document.querySelector('#ngrok-warning')) {
    results.push({
      type: 'ngrok_warning',
      confidence: 'medium',
      signal: 'css_selector',
      details: 'ngrok interstitial button or warning element'
    });
  }

  // --- Auth Wall / Login Page ---
  const loginFormInputs = document.querySelectorAll(
    'input[type="password"], input[name="password"], input[autocomplete="current-password"]'
  );
  if (loginFormInputs.length > 0) {
    // Check for common login indicators
    const hasLoginTitle = /log\s*in|sign\s*in|authenticate|sso/i.test(title);
    const hasLoginContent = /log\s*in|sign\s*in|forgot.*password|remember me/i.test(bodyText.slice(0, 2000));

    if (hasLoginTitle || hasLoginContent) {
      results.push({
        type: 'auth_wall',
        confidence: hasLoginTitle ? 'medium' : 'low',
        signal: hasLoginTitle ? 'title_and_form' : 'form_heuristic',
        details: 'Login form detected — page may be an authentication wall'
      });
    }
  }

  // --- CAPTCHA ---
  const hasCaptcha = document.querySelector(
    '.g-recaptcha, .h-captcha, [data-sitekey], iframe[src*="recaptcha"], iframe[src*="hcaptcha"]'
  );
  if (hasCaptcha) {
    results.push({
      type: 'captcha',
      confidence: 'medium',
      signal: 'css_selector',
      details: 'CAPTCHA element detected'
    });
  }

  // --- Bot Detection / Access Denied ---
  if (/access denied|403 forbidden|blocked/i.test(title)) {
    results.push({
      type: 'access_denied',
      confidence: 'medium',
      signal: 'title_pattern',
      details: 'Access denied or blocked page title: "' + title + '"'
    });
  }

  // --- DataDome ---
  // Cookie-based detection (most reliable)
  if (document.cookie.indexOf('datadome') !== -1) {
    results.push({
      type: 'datadome',
      confidence: 'high',
      signal: 'cookie',
      details: 'DataDome cookie detected'
    });
  }
  // Script source detection
  if (document.querySelector('script[src*="datadome"]')) {
    results.push({
      type: 'datadome',
      confidence: 'high',
      signal: 'script_src',
      details: 'DataDome script tag detected'
    });
  }
  // Challenge page detection (iframe-based challenge)
  if (document.querySelector('iframe[src*="datadome"], iframe[src*="captcha-delivery.com"]')) {
    results.push({
      type: 'datadome',
      confidence: 'high',
      signal: 'challenge_iframe',
      details: 'DataDome challenge iframe detected'
    });
  }

  // --- Akamai Bot Manager ---
  if (document.cookie.indexOf('_abck') !== -1) {
    // _abck cookie alone isn't a challenge — it's the sensor cookie.
    // Only flag if combined with a short/empty page (likely blocked).
    var akamaiBodyLen = (document.body && document.body.innerText || '').length;
    if (akamaiBodyLen < 500) {
      results.push({
        type: 'akamai_bot_manager',
        confidence: 'medium',
        signal: 'cookie_and_empty_page',
        details: 'Akamai _abck cookie present with minimal page content (likely blocked)'
      });
    }
  }
  // Akamai reference number in body
  if (/reference\s*#?\s*\d+\.\w+\.\d+/i.test(bodyText.slice(0, 2000))) {
    results.push({
      type: 'akamai_bot_manager',
      confidence: 'medium',
      signal: 'reference_number',
      details: 'Akamai-style reference number detected in page body'
    });
  }

  // --- PerimeterX / HUMAN ---
  if (document.cookie.match(/_px[A-Za-z]/)) {
    results.push({
      type: 'perimeterx',
      confidence: 'medium',
      signal: 'cookie',
      details: 'PerimeterX cookie prefix (_px*) detected'
    });
  }
  if (document.querySelector('script[src*="perimeterx.net"], script[src*="px-cdn.net"], script[src*="px-cloud.net"]')) {
    results.push({
      type: 'perimeterx',
      confidence: 'high',
      signal: 'script_src',
      details: 'PerimeterX/HUMAN script detected'
    });
  }
  // PerimeterX "Press & Hold" challenge
  if (/press\s*(&|and)\s*hold|human\s*challenge/i.test(bodyText.slice(0, 3000))) {
    results.push({
      type: 'perimeterx',
      confidence: 'medium',
      signal: 'body_text',
      details: 'PerimeterX/HUMAN challenge text detected'
    });
  }

  // --- Imperva / Incapsula ---
  if (document.cookie.match(/incap_ses_|visid_incap_/)) {
    results.push({
      type: 'imperva',
      confidence: 'medium',
      signal: 'cookie',
      details: 'Imperva/Incapsula session cookie detected'
    });
  }
  if (/incapsula|imperva/i.test(bodyText.slice(0, 3000))) {
    results.push({
      type: 'imperva',
      confidence: 'medium',
      signal: 'body_text',
      details: 'Imperva/Incapsula reference in page body'
    });
  }
  // Incapsula challenge script
  if (document.querySelector('script[src*="incapsula"], script[src*="imperva"]')) {
    results.push({
      type: 'imperva',
      confidence: 'high',
      signal: 'script_src',
      details: 'Imperva/Incapsula script tag detected'
    });
  }

  // --- AWS WAF ---
  if (document.cookie.indexOf('aws-waf-token') !== -1) {
    results.push({
      type: 'aws_waf',
      confidence: 'high',
      signal: 'cookie',
      details: 'AWS WAF token cookie detected'
    });
  }
  // AWS WAF CAPTCHA integration
  if (document.querySelector('script[src*="awswaf"], #aws-waf-captcha-container, [data-aws-waf]')) {
    results.push({
      type: 'aws_waf',
      confidence: 'high',
      signal: 'dom_element',
      details: 'AWS WAF CAPTCHA or challenge element detected'
    });
  }

  // Deduplicate by type (keep highest confidence per type)
  const seen = new Map();
  for (const r of results) {
    const existing = seen.get(r.type);
    const rank = { high: 3, medium: 2, low: 1 };
    if (!existing || rank[r.confidence] > rank[existing.confidence]) {
      seen.set(r.type, r);
    }
  }

  return JSON.stringify(Array.from(seen.values()));
})();
