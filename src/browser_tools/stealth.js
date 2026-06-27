/**
 * Stealth patches to reduce automation fingerprinting.
 *
 * Injected via Page.addScriptToEvaluateOnNewDocument so these run
 * before any page JavaScript. Based on well-documented evasions from
 * puppeteer-extra-plugin-stealth and similar projects.
 *
 * This does NOT guarantee bypass of enterprise bot management (DataDome,
 * Akamai, PerimeterX, etc.) — those use behavioral analysis, sensor
 * data, and server-side signals that cannot be patched client-side.
 * But it eliminates the trivial detection vectors that many simpler
 * protections check first.
 *
 * Patches applied:
 *   1. navigator.webdriver → undefined
 *   2. navigator.plugins   → populated array (non-empty)
 *   3. navigator.languages → realistic array
 *   4. window.chrome       → present with runtime stub
 *   5. Permissions.query   → "prompt" for notifications
 *   6. WebGL renderer      → generic strings (not "Google SwiftShader")
 *   7. Broken image size   → natural dimensions for broken images
 *   8. CDP Runtime domain  → hide Runtime.enable artifacts
 */
(function applyStealthPatches() {
  'use strict';

  // --- 1. navigator.webdriver ---
  // Most basic detection vector. Chrome sets this to true when
  // controlled via CDP/WebDriver.
  try {
    Object.defineProperty(navigator, 'webdriver', {
      get: function() { return undefined; },
      configurable: true,
    });
  } catch (e) {}

  // Also delete from prototype chain
  try {
    var proto = Object.getPrototypeOf(navigator);
    if (proto) {
      delete proto.webdriver;
    }
  } catch (e) {}

  // --- 2. navigator.plugins ---
  // Headless Chrome has an empty plugins array. Real Chrome always has
  // at least a few (PDF Viewer, Chrome PDF Viewer, etc.).
  try {
    var pluginData = [
      { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    ];

    var fakePlugins = {
      length: pluginData.length,
      item: function(i) { return this[i] || null; },
      namedItem: function(name) {
        for (var j = 0; j < pluginData.length; j++) {
          if (pluginData[j].name === name) return this[j];
        }
        return null;
      },
      refresh: function() {},
    };

    for (var i = 0; i < pluginData.length; i++) {
      fakePlugins[i] = pluginData[i];
    }

    Object.defineProperty(navigator, 'plugins', {
      get: function() { return fakePlugins; },
      configurable: true,
    });
  } catch (e) {}

  // --- 3. navigator.languages ---
  // Ensure realistic language array
  try {
    if (!navigator.languages || navigator.languages.length === 0) {
      Object.defineProperty(navigator, 'languages', {
        get: function() { return ['en-US', 'en']; },
        configurable: true,
      });
    }
  } catch (e) {}

  // --- 4. window.chrome ---
  // Real Chrome always has window.chrome with a runtime object.
  // Headless Chrome may be missing it entirely.
  try {
    if (!window.chrome) {
      window.chrome = {};
    }
    if (!window.chrome.runtime) {
      window.chrome.runtime = {
        connect: function() { return {}; },
        sendMessage: function() {},
        id: undefined,
      };
    }
  } catch (e) {}

  // --- 5. Permissions.query ---
  // Headless Chrome returns "denied" for notification permissions.
  // Real browsers return "prompt" by default.
  try {
    var originalQuery = Permissions.prototype.query;
    Permissions.prototype.query = function(parameters) {
      if (parameters && parameters.name === 'notifications') {
        return Promise.resolve({ state: 'prompt', onchange: null });
      }
      return originalQuery.call(this, parameters);
    };
  } catch (e) {}

  // --- 6. WebGL renderer ---
  // Headless Chrome reports "Google SwiftShader" as the WebGL renderer,
  // which is a dead giveaway for headless mode.
  try {
    var getParameterProto = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
      // UNMASKED_VENDOR_WEBGL
      if (param === 0x9245) return 'Google Inc. (NVIDIA)';
      // UNMASKED_RENDERER_WEBGL
      if (param === 0x9246) return 'ANGLE (NVIDIA, NVIDIA GeForce, OpenGL 4.1)';
      return getParameterProto.call(this, param);
    };
  } catch (e) {}

  // Also patch WebGL2
  try {
    if (typeof WebGL2RenderingContext !== 'undefined') {
      var getParameter2Proto = WebGL2RenderingContext.prototype.getParameter;
      WebGL2RenderingContext.prototype.getParameter = function(param) {
        if (param === 0x9245) return 'Google Inc. (NVIDIA)';
        if (param === 0x9246) return 'ANGLE (NVIDIA, NVIDIA GeForce, OpenGL 4.1)';
        return getParameter2Proto.call(this, param);
      };
    }
  } catch (e) {}

  // --- 7. Broken image dimensions ---
  // In headless Chrome, broken images have 0x0 dimensions.
  // In real Chrome, they render as a small placeholder (e.g. 16x16).
  try {
    ['height', 'width'].forEach(function(prop) {
      var imgProto = Object.getOwnPropertyDescriptor(
        HTMLImageElement.prototype, prop
      );
      if (imgProto) {
        var originalGet = imgProto.get;
        Object.defineProperty(HTMLImageElement.prototype, prop, {
          get: function() {
            if (this.complete && this.naturalHeight === 0) {
              return prop === 'height' ? 16 : 16;
            }
            return originalGet.call(this);
          },
          configurable: true,
        });
      }
    });
  } catch (e) {}

  // --- 8. CDP Runtime detection ---
  // Some bot detectors check for the existence of CDP-injected globals
  // like __cdp_runtime__ or detect Runtime.enable side effects.
  try {
    // Clean up any CDP artifacts from the global scope
    var cdpArtifacts = ['__cdp_runtime__', '__playwright_evaluation_script__'];
    cdpArtifacts.forEach(function(name) {
      try { delete window[name]; } catch (e) {}
    });
  } catch (e) {}

})();
