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
 *   session.clear()           -> set both fields to '' with silent native
 *                                setters (no events), verify both empty
 *                                together, never return old values.
 *
 * Success polling is standalone Python; this helper has exactly one entry
 * point (begin), which is the whole surface the Python caller uses.
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
  const REASON_INVALID_FORM = 'form-invalid';

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
    if (!element.isConnected || element.matches(':disabled') || element.readOnly) {
      return false;
    }
    if (element.getClientRects().length === 0) return false;
    const style = getComputedStyle(element);
    if (style.visibility !== 'visible') return false;
    // A promised usable field must actually render: opacity zero and a
    // zero-size box both take credentials while being invisible to the
    // person. Offscreen placement is allowed - a below-fold form is a real
    // form - so there is deliberately no viewport-intersection check here.
    const rect = element.getBoundingClientRect();
    if (!(rect.width > 0 && rect.height > 0)) return false;
    for (let node = element; node instanceof Element; node = node.parentElement) {
      if (getComputedStyle(node).opacity === '0') return false;
    }
    return true;
  }

  function isSubmitButton(element) {
    if (!element.isConnected) return false;
    // Reflected type properties apply the browser's enumerated-value rules:
    // an invalid or missing value reads as the default ('submit' for
    // buttons, 'text' for inputs), which getAttribute + toLowerCase would
    // miss (e.g. type=' post ' on a method).
    if (element.tagName === 'BUTTON') return element.type === 'submit';
    if (element.tagName === 'INPUT') {
      return element.type === 'submit' || element.type === 'image';
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
    const raw = submitter && submitter.hasAttribute('formaction')
      ? submitter.getAttribute('formaction')
      : form.getAttribute('action');
    try {
      // Per HTML form submission, an absent or empty action - including an
      // empty submitter override - submits to the document URL, never the
      // <base> URL; only a nonempty action resolves against document.baseURI.
      // Chrome strips HTML spaces here, not JavaScript's Unicode whitespace.
      if (raw === null || /^[\t\n\f\r ]*$/.test(raw)) return new URL(document.URL);
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
    // The reflected property: invalid and missing values read as 'text',
    // exactly the browser's own enumerated-value semantics.
    if (username.element.type !== 'text' && username.element.type !== 'email') {
      return { ok: false, reason: REASON_USERNAME_TYPE };
    }
    if (!isInput(password.element)) return { ok: false, reason: REASON_PASSWORD_TYPE };
    if (password.element.type !== 'password') {
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
    // filling it would only type credentials into a signed-in page. Any
    // match counts, one or many: checking for "exactly one" would let a
    // repeated marker slip through and authenticate a stale page.
    let existing;
    try {
      existing = document.querySelectorAll(policy.success_selector);
    } catch (err) {
      return { ok: false, reason: REASON_BAD_SELECTOR };
    }
    if (existing.length > 0) return { ok: false, reason: REASON_ALREADY_AUTHENTICATED };

    return { ok: true };
  }

  // The native value setter, silent: no input/change events. One
  // implementation serves both callers. Cleanup must not dispatch events:
  // an empty-field notification is a no-op at best; at worst, while a
  // page's async submit handler is still awaiting (fetch or validation),
  // those events overwrite the framework state the handler will read, so
  // the submission would carry empty credentials. The DOM value is still
  // verifiably empty, which is the guarantee cleanup owes.
  function setNativeValueSilently(element, value) {
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
  }

  // The notifying variant for FILLING: the same native setter plus
  // input/change events, the framework-compatible recipe (a plain .value
  // assignment is invisible to React/Vue state, and the state is what an
  // async submit handler later submits).
  function setNativeValue(element, value) {
    setNativeValueSilently(element, value);
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
      // Silent setters only, and no notification for a field that is
      // already empty. Both fields are set first and verified together
      // afterwards: a handler that restores one field's remembered value
      // must not be able to do so after that field's own check has passed.
      // The previous value is never read out.
      const bound = nodes;
      if (!bound) return false;
      for (const element of [bound.username, bound.password]) {
        if (element.value === '') continue;
        try {
          setNativeValueSilently(element, '');
        } catch (err) {
          // Verified below; a failed setter means not cleared.
        }
      }
      if (bound.username.value !== '' || bound.password.value !== '') return false;
      // A framework can replace the bound nodes while retaining their values.
      // Confirm the current fields too, without writing replacement controls.
      try {
        for (const selector of [policy.username_selector, policy.password_selector]) {
          for (const element of document.querySelectorAll(selector)) {
            if (typeof element.value === 'string' && element.value !== '') return false;
          }
        }
      } catch (err) {
        return false;
      }
      return true;
    }

    return {
      outcome: null,
      fill: function (usernameValue, passwordValue) {
        const before = revalidate();
        if (before.ok !== true) return { ...before, insertionStarted: false };
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
            const needsValidation = !nodes.form.noValidate && !nodes.submit.formNoValidate;
            if (needsValidation && !HTMLFormElement.prototype.checkValidity.call(nodes.form)) {
              outcome = { ok: false, reason: REASON_INVALID_FORM };
            } else {
              HTMLFormElement.prototype.requestSubmit.call(nodes.form, nodes.submit);
              outcome = { ok: true };
            }
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

  return { begin: begin };
})();
