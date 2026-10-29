/**
 * browser-tools login form helper.
 *
 * Injected into an isolated world (Page.createIsolatedWorld) of the main
 * frame by browser_tools.login. All DOM validation, filling, submission and
 * cleanup for `bt login` happens here, in the world the Python side bound
 * to one specific document: when the document is replaced, the world's
 * RemoteObject references die with it, so a stale session object can never
 * touch a replacement document.
 *
 * Contract with the Python side (login.py):
 *
 *   __btLogin.begin(policy)   -> session object with an `outcome` property
 *                                (plain JSON: {ok: true} or {ok: false,
 *                                reason: "<fixed code>"}) and the methods
 *                                below. Validation runs fully before any
 *                                credential is read.
 *   session.fill(u, p)        -> revalidate, insert both values with native
 *                                setters + input/change events, revalidate
 *                                again. Values arrive as call arguments,
 *                                never inside script source. Returns a fixed
 *                                outcome; never returns the values.
 *   session.submit()          -> revalidate, then form.requestSubmit(button).
 *   session.clear()           -> set both fields to '' with native setters,
 *                                verify empty, never return old values.
 *   __btLogin.probe(policy)   -> {authenticated, originOk} in whatever
 *                                document currently lives in the frame.
 *
 * Every failure returns a REASON_* string from the fixed vocabulary below.
 * No page-provided text (exception messages, node data, attributes) is ever
 * returned or thrown upward.
 */
globalThis.__btLogin = (function () {
  const REASON_WRONG_ORIGIN = 'wrong-origin';
  const REASON_HIDDEN_TAB = 'hidden-tab';
  const REASON_NO_DOCUMENT = 'no-document';
  const REASON_BAD_SELECTOR = 'bad-selector';
  const REASON_USERNAME_COUNT = 'username-not-unique';
  const REASON_PASSWORD_COUNT = 'password-not-unique';
  const REASON_SUBMIT_COUNT = 'submit-not-unique';
  const REASON_USERNAME_TYPE = 'username-wrong-type';
  const REASON_PASSWORD_TYPE = 'password-wrong-type';
  const REASON_FIELD_DISABLED = 'field-disabled';
  const REASON_FIELDS_NOT_DISTINCT = 'fields-not-distinct';
  const REASON_NO_COMMON_FORM = 'no-common-form';
  const REASON_FORM_DETACHED = 'form-detached';
  const REASON_SUBMIT_WRONG_FORM = 'submit-wrong-form';
  const REASON_SUBMIT_TYPE = 'submit-wrong-type';
  const REASON_NOT_POST = 'not-post';
  const REASON_ACTION_NOT_SAME_ORIGIN = 'action-not-same-origin';
  const REASON_SUBMIT_UNSUPPORTED = 'submit-unsupported';
  const REASON_ALREADY_AUTHENTICATED = 'already-authenticated';
  const REASON_DOCUMENT_CHANGED = 'document-changed';
  const REASON_FILL_FAILED = 'fill-failed';
  const REASON_SUBMIT_FAILED = 'submit-failed';

  function originOk(policy) {
    try {
      return window.location.origin === policy.origin;
    } catch (err) {
      return false;
    }
  }

  function documentUsable() {
    return Boolean(document && document.documentElement);
  }

  function visible() {
    return document.visibilityState === 'visible';
  }

  function queryExactlyOne(selector) {
    let found;
    try {
      found = document.querySelectorAll(selector);
    } catch (err) {
      return { error: REASON_BAD_SELECTOR };
    }
    if (found.length !== 1) {
      return { error: 'count:' + String(found.length) };
    }
    return { element: found[0] };
  }

  function isInput(element) {
    return Boolean(element) && element.tagName === 'INPUT';
  }

  function isEnabled(element) {
    return element.isConnected && !element.matches(':disabled') &&
      !element.readOnly && element.getClientRects().length > 0 &&
      getComputedStyle(element).visibility === 'visible';
  }

  function isSubmitButton(element) {
    if (!element.isConnected) return false;
    if (element.tagName === 'BUTTON') {
      const type = (element.getAttribute('type') || 'submit').toLowerCase();
      return type === 'submit';
    }
    if (element.tagName === 'INPUT') {
      const type = (element.getAttribute('type') || '').toLowerCase();
      return type === 'submit' || type === 'image';
    }
    return false;
  }

  // The form's submission destination, honoring submitter overrides
  // (formaction/formmethod), which a plain form.method read would miss.
  function effectiveMethod(form, submitter) {
    // Reflected properties apply the browser's enumerated-value rules. A
    // raw value such as ' post ' is invalid and actually submits by GET.
    return submitter && submitter.hasAttribute('formmethod')
      ? submitter.formMethod : form.method;
  }

  function effectiveAction(form, submitter) {
    const raw = (submitter && submitter.hasAttribute('formaction')
      ? submitter.getAttribute('formaction')
      : form.getAttribute('action')) || '';
    try {
      // An empty/absent action resolves to the document URL.
      return new URL(raw, document.baseURI);
    } catch (err) {
      return null;
    }
  }

  function submissionDestinationOk(form, submitter) {
    if (effectiveMethod(form, submitter) !== 'post') {
      return REASON_NOT_POST;
    }
    const action = effectiveAction(form, submitter);
    if (!action || action.origin !== window.location.origin) {
      return REASON_ACTION_NOT_SAME_ORIGIN;
    }
    return null;
  }

  function validate(policy) {
    if (!documentUsable()) return { ok: false, reason: REASON_NO_DOCUMENT };
    if (!originOk(policy)) return { ok: false, reason: REASON_WRONG_ORIGIN };
    if (!visible()) return { ok: false, reason: REASON_HIDDEN_TAB };

    const username = queryExactlyOne(policy.username_selector);
    if (username.error === REASON_BAD_SELECTOR) {
      return { ok: false, reason: REASON_BAD_SELECTOR };
    }
    if (!username.element) return { ok: false, reason: REASON_USERNAME_COUNT };
    const password = queryExactlyOne(policy.password_selector);
    if (password.error === REASON_BAD_SELECTOR) {
      return { ok: false, reason: REASON_BAD_SELECTOR };
    }
    if (!password.element) return { ok: false, reason: REASON_PASSWORD_COUNT };
    const submit = queryExactlyOne(policy.submit_selector);
    if (submit.error === REASON_BAD_SELECTOR) {
      return { ok: false, reason: REASON_BAD_SELECTOR };
    }
    if (!submit.element) return { ok: false, reason: REASON_SUBMIT_COUNT };

    if (!isInput(username.element)) return { ok: false, reason: REASON_USERNAME_TYPE };
    const usernameType = (username.element.getAttribute('type') || 'text').toLowerCase();
    if (usernameType !== 'text' && usernameType !== 'email') {
      return { ok: false, reason: REASON_USERNAME_TYPE };
    }
    if (!isInput(password.element)) return { ok: false, reason: REASON_PASSWORD_TYPE };
    if ((password.element.getAttribute('type') || '').toLowerCase() !== 'password') {
      return { ok: false, reason: REASON_PASSWORD_TYPE };
    }
    if (!isEnabled(username.element) || !isEnabled(password.element)) {
      return { ok: false, reason: REASON_FIELD_DISABLED };
    }
    if (username.element === password.element) {
      return { ok: false, reason: REASON_FIELDS_NOT_DISTINCT };
    }

    const form = username.element.form;
    if (!form || form.tagName !== 'FORM' || !form.isConnected) {
      return { ok: false, reason: REASON_NO_COMMON_FORM };
    }
    if (password.element.form !== form) return { ok: false, reason: REASON_NO_COMMON_FORM };
    if (form.ownerDocument !== document) return { ok: false, reason: REASON_FORM_DETACHED };

    if (!isSubmitButton(submit.element)) return { ok: false, reason: REASON_SUBMIT_TYPE };
    if (submit.element.form !== form) return { ok: false, reason: REASON_SUBMIT_WRONG_FORM };
    if (!isEnabled(submit.element)) return { ok: false, reason: REASON_FIELD_DISABLED };
    if (typeof HTMLFormElement.prototype.requestSubmit !== 'function') {
      return { ok: false, reason: REASON_SUBMIT_UNSUPPORTED };
    }

    const destination = submissionDestinationOk(form, submit.element);
    if (destination) return { ok: false, reason: destination };

    // A page that already satisfies the success selector needs no login;
    // filling it would only type credentials into a signed-in page.
    const success = queryExactlyOne(policy.success_selector);
    if (success.error === REASON_BAD_SELECTOR) {
      return { ok: false, reason: REASON_BAD_SELECTOR };
    }
    if (success.element) return { ok: false, reason: REASON_ALREADY_AUTHENTICATED };

    return { ok: true };
  }

  // Native value setter + input/change events, the framework-compatible
  // recipe: a plain .value assignment is invisible to React/Vue state.
  function setNativeValue(element, value) {
    const prototype =
      element.tagName === 'TEXTAREA'
        ? window.HTMLTextAreaElement && HTMLTextAreaElement.prototype
        : window.HTMLInputElement && HTMLInputElement.prototype;
    const descriptor = prototype && Object.getOwnPropertyDescriptor(prototype, 'value');
    if (descriptor && descriptor.set) {
      descriptor.set.call(element, value);
    } else {
      element.value = value;
    }
    element.dispatchEvent(new Event('input', { bubbles: true }));
    element.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function makeSession(policy, boundNodes) {
    const nodes = boundNodes;

    function revalidate() {
      const outcome = validate(policy);
      if (outcome.ok !== true) return outcome;
      // Identity, not selectors: the bound nodes must still be the connected
      // nodes that satisfy the policy. A same-document swap fails here.
      const bound = nodes;
      if (
        !bound ||
        !bound.username.isConnected ||
        !bound.password.isConnected ||
        !bound.form.isConnected ||
        !bound.submit.isConnected ||
        bound.username.form !== bound.form ||
        bound.password.form !== bound.form ||
        bound.submit.form !== bound.form ||
        !bound.username.matches(policy.username_selector) ||
        !bound.password.matches(policy.password_selector) ||
        !bound.submit.matches(policy.submit_selector)
      ) {
        return { ok: false, reason: REASON_DOCUMENT_CHANGED };
      }
      const destination = submissionDestinationOk(bound.form, bound.submit);
      if (destination) return { ok: false, reason: destination };
      return { ok: true };
    }

    function clearFields() {
      // Insert '' and confirm empty. The previous value is never read out.
      const bound = nodes;
      if (!bound) return false;
      let cleared = true;
      for (const element of [bound.username, bound.password]) {
        try {
          setNativeValue(element, '');
          if (element.value !== '') cleared = false;
        } catch (err) {
          cleared = false;
        }
      }
      return cleared;
    }

    return {
      outcome: null,
      fill: function (usernameValue, passwordValue) {
        const before = revalidate();
        if (before.ok !== true) return before;
        try {
          setNativeValue(nodes.username, usernameValue);
          setNativeValue(nodes.password, passwordValue);
        } catch (err) {
          const cleared = clearFields();
          return { ok: false, reason: REASON_FILL_FAILED, cleared: cleared };
        }
        // After the input events: page handlers have run, and the document
        // may have reacted. Revalidate before anything is submitted.
        const after = revalidate();
        if (after.ok !== true) {
          const cleared = clearFields();
          return { ok: false, reason: after.reason, cleared: cleared };
        }
        return { ok: true };
      },
      submit: function () {
        const before = revalidate();
        let outcome = before;
        if (before.ok === true) {
          try {
            HTMLFormElement.prototype.requestSubmit.call(nodes.form, nodes.submit);
            outcome = { ok: true };
          } catch (err) {
            outcome = { ok: false, reason: REASON_SUBMIT_FAILED };
          }
        }
        // Native submission snapshots form data synchronously. Clear before
        // yielding to page microtasks or returning across the CDP boundary.
        const cleared = clearFields();
        return { ...outcome, cleared: cleared };
      },
      clear: function () {
        const cleared = clearFields();
        return { ok: true, cleared: cleared };
      },
    };
  }

  function begin(policy) {
    const outcome = validate(policy);
    let nodes = null;
    if (outcome.ok === true) {
      // The validated nodes, bound by identity (the same synchronous turn,
      // so these are the nodes validate() saw). Everything after this runs
      // against these references, never a fresh selector lookup.
      nodes = {
        username: queryExactlyOne(policy.username_selector).element,
        password: queryExactlyOne(policy.password_selector).element,
        submit: queryExactlyOne(policy.submit_selector).element,
      };
      nodes.form = nodes.username.form;
    }
    const session = makeSession(policy, nodes);
    session.outcome = outcome;
    return session;
  }

  function probe(policy) {
    if (!documentUsable() || !originOk(policy)) {
      return { authenticated: false, originOk: false };
    }
    let matched = false;
    try {
      matched = document.querySelectorAll(policy.success_selector).length > 0;
    } catch (err) {
      matched = false;
    }
    return { authenticated: matched, originOk: true };
  }

  return { begin: begin, probe: probe };
})();
