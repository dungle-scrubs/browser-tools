// The upstream formatter uses ParsedURL only for the last path component of a font URL.
class FontURL {
  constructor(value) {
    try {
      const url = new URL(value);
      this.isValid = true;
      this.lastPathComponent = url.pathname.split('/').at(-1);
    } catch {
      this.isValid = false;
      this.lastPathComponent = '';
    }
  }
}
export const ParsedURL = {ParsedURL: FontURL};

// The standalone engine replaces DevTools i18n with {i18nId, values} tokens.
// Give those tokens their English text without replacing event objects, whose
// identity is used by the formatter and engine Maps.
export function resolveTextTokens(value, seen = new WeakSet()) {
  if (!value || typeof value !== 'object' || seen.has(value)) return;
  seen.add(value);
  if (typeof value.i18nId === 'string') {
    Object.defineProperty(value, 'toString', {value() {
      return this.i18nId.replace(/\{(\w+)\}/g, (token, key) =>
        this.values?.[key] === undefined ? token : String(this.values[key]));
    }});
  }
  const children = value instanceof Map ? [...value.keys(), ...value.values()]
    : value instanceof Set ? [...value] : Object.values(value);
  for (const child of children) resolveTextTokens(child, seen);
}
