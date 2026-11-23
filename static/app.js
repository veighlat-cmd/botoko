/* BotoKo shared JavaScript (no frameworks).
 * Everything here is "progressive enhancement": the site still works without it.
 *   - Toasts: auto-hide flash messages, BotoKo.toast("text", "ok" | "error")
 *   - Modal:  BotoKo.openModal(el) / closeModal(), with focus handling and Esc to close
 *   - Confirm: <form data-confirm="Question?"> asks before submitting
 *   - Busy forms: <form data-busy="Logging in..."> disables the button to stop double taps
 *   - Password toggle, copy-to-clipboard
 */
(function () {
  'use strict';
  var BotoKo = window.BotoKo = {};

  /* ---------- helpers shared with results.js (same formulas as app.py) ---------- */
  BotoKo.esc = function (s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };
  BotoKo.initials = function (name) {
    var words = String(name).replace(/\./g, ' ').split(/\s+/).filter(function (w) { return /^[A-Za-z0-9À-￿]/.test(w); });
    if (!words.length) return '?';
    if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
    return (words[0][0] + words[words.length - 1][0]).toUpperCase();
  };
  BotoKo.tone = function (text) {
    var sum = 0, s = String(text);
    for (var i = 0; i < s.length; i++) sum += s.charCodeAt(i);
    return sum % 6;
  };
  BotoKo.icon = function (name, cls) {
    return '<svg class="icon ' + (cls || '') + '" aria-hidden="true" focusable="false"><use href="#i-' + name + '"></use></svg>';
  };

  /* ---------- toasts ---------- */
  function dismiss(toast) {
    if (!toast || toast.classList.contains('is-leaving')) return;
    toast.classList.add('is-leaving');
    setTimeout(function () { toast.remove(); }, 220);
  }
  function arm(toast) {
    var close = toast.querySelector('.toast-close');
    if (close) close.addEventListener('click', function () { dismiss(toast); });
    // Success messages go away by themselves; errors stay longer so they can be read.
    var isError = toast.classList.contains('toast-error') || toast.classList.contains('toast-already');
    setTimeout(function () { dismiss(toast); }, isError ? 10000 : 5000);
  }
  BotoKo.toast = function (message, kind) {
    var box = document.getElementById('toasts');
    if (!box) return;
    kind = kind || 'ok';
    var el = document.createElement('div');
    el.className = 'toast toast-' + kind;
    el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    el.innerHTML = BotoKo.icon(kind === 'ok' ? 'check' : (kind === 'info' ? 'info' : 'alert')) +
      '<span class="toast-msg">' + BotoKo.esc(message) + '</span>' +
      '<button type="button" class="btn btn-ghost btn-icon toast-close" aria-label="Dismiss">' + BotoKo.icon('x') + '</button>';
    box.appendChild(el);
    arm(el);
  };

  /* ---------- modal (a stack: e.g. the account menu can open on top of the review screen) ---------- */
  var stack = [];                                   // [{el, back}] - last one is on top
  function topModal() { return stack.length ? stack[stack.length - 1].el : null; }
  // Things you can Tab to. (Not just "[href]": that would also match the <use href> inside SVG icons.)
  var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), ' +
                  'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  function focusables(el) {
    return Array.prototype.filter.call(el.querySelectorAll(FOCUSABLE), function (x) { return !x.hidden; });
  }
  BotoKo.openModal = function (modal) {
    if (stack.some(function (m) { return m.el === modal; })) return;
    stack.push({ el: modal, back: document.activeElement });
    modal.hidden = false;
    document.body.classList.add('modal-open');
    var first = modal.querySelector('[data-autofocus]') || focusables(modal)[0];
    if (first) first.focus();
  };
  // Close `modal` (or the top one). Focus goes back to whatever opened it.
  BotoKo.closeModal = function (modal) {
    var i = stack.length - 1;
    if (modal) i = stack.map(function (m) { return m.el; }).indexOf(modal);
    if (i < 0) return;
    var item = stack.splice(i, 1)[0];
    item.el.hidden = true;
    if (!stack.length) document.body.classList.remove('modal-open');
    if (item.back && item.back.focus) item.back.focus();
    if (item.el.onModalClose) item.el.onModalClose();     // optional hook (ballot.js, account menu)
  };
  document.addEventListener('keydown', function (e) {
    var top = topModal();
    if (!top) return;
    if (e.key === 'Escape' && !top.hasAttribute('data-locked')) { BotoKo.closeModal(top); return; }
    if (e.key === 'Tab') {               // keep keyboard focus inside the open dialog
      var items = focusables(top);
      if (!items.length) return;
      var first = items[0], last = items[items.length - 1];
      if (!top.contains(document.activeElement) || document.activeElement === top) { first.focus(); e.preventDefault(); }
      else if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
      else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
    }
  });
  document.addEventListener('click', function (e) {
    var top = topModal();
    if (top && e.target === top && !top.hasAttribute('data-locked')) BotoKo.closeModal(top);   // tap outside
    var closer = e.target.closest && e.target.closest('[data-close-modal]');
    if (closer) BotoKo.closeModal(closer.closest('.modal') || undefined);
  });

  /* ---------- student account menu (drawer) ---------- */
  function forgetBallot() {           // saved ballot choices (see ballot.js) are removed on log out
    try {
      Object.keys(sessionStorage).forEach(function (k) { if (k.indexOf('botoko-ballot-') === 0) sessionStorage.removeItem(k); });
    } catch (err) { /* storage blocked */ }
  }
  BotoKo.forgetBallot = forgetBallot;
  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('account-menu-btn');
    var drawer = document.getElementById('account-menu');
    if (!btn || !drawer) return;
    btn.addEventListener('click', function () {
      btn.setAttribute('aria-expanded', 'true');
      BotoKo.openModal(drawer);
    });
    drawer.onModalClose = function () { btn.setAttribute('aria-expanded', 'false'); };
    drawer.addEventListener('click', function (e) {
      var link = e.target.closest('a[href]');
      if (!link) return;
      if (link.hasAttribute('data-logout')) forgetBallot();
      BotoKo.closeModal(drawer);      // close, then the link opens its page
    });
  });

  /* ---------- confirm dialog: <form data-confirm="..." data-confirm-ok="Delete" data-danger> ---------- */
  var confirmModal = null;
  function buildConfirm() {
    confirmModal = document.createElement('div');
    confirmModal.className = 'modal';
    confirmModal.hidden = true;
    confirmModal.setAttribute('role', 'dialog');
    confirmModal.setAttribute('aria-modal', 'true');
    confirmModal.setAttribute('aria-labelledby', 'confirm-title');
    confirmModal.innerHTML =
      '<div class="modal-panel">' +
        '<h2 id="confirm-title"></h2><p class="muted" id="confirm-text"></p>' +
        '<div class="modal-actions">' +
          '<button type="button" class="btn" data-close-modal data-autofocus>Cancel</button>' +
          '<button type="button" class="btn" id="confirm-ok">OK</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(confirmModal);
  }
  BotoKo.confirm = function (opts, onOk) {
    if (!confirmModal) buildConfirm();
    confirmModal.querySelector('#confirm-title').textContent = opts.title || 'Are you sure?';
    confirmModal.querySelector('#confirm-text').textContent = opts.text || '';
    var ok = confirmModal.querySelector('#confirm-ok');
    ok.textContent = opts.ok || 'Confirm';
    ok.className = 'btn ' + (opts.danger ? 'btn-danger' : 'btn-primary');
    ok.onclick = function () { BotoKo.closeModal(); onOk(); };
    BotoKo.openModal(confirmModal);
  };

  /* ---------- buttons that show a spinner while the page is loading ---------- */
  BotoKo.setBusy = function (btn, label) {
    if (!btn) return;
    if (!btn.dataset.idle) btn.dataset.idle = btn.innerHTML;
    btn.disabled = true;
    btn.classList.add('is-loading');
    btn.setAttribute('aria-busy', 'true');
    btn.innerHTML = '<span class="spinner" aria-hidden="true"></span><span>' + BotoKo.esc(label || 'Please wait...') + '</span>';
  };
  BotoKo.clearBusy = function (btn) {
    if (!btn || !btn.dataset.idle) return;
    btn.disabled = false;
    btn.classList.remove('is-loading');
    btn.removeAttribute('aria-busy');
    btn.innerHTML = btn.dataset.idle;
  };

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (form.dataset.confirm && !form.dataset.confirmed) {
      e.preventDefault();
      BotoKo.confirm({
        title: form.dataset.confirm,
        text: form.dataset.confirmText,
        ok: form.dataset.confirmOk,
        danger: form.hasAttribute('data-danger')
      }, function () {
        form.dataset.confirmed = '1';
        if (form.requestSubmit) form.requestSubmit(); else form.submit();
      });
      return;
    }
    delete form.dataset.confirmed;
    if (form.hasAttribute('data-logout-form')) forgetBallot();      // "Log out" button on the event list
    if (form.dataset.busy !== undefined) {
      if (form.dataset.sending) { e.preventDefault(); return; }   // second tap: ignore
      form.dataset.sending = '1';
      var btn = form.querySelector('[type=submit]');
      // Wait one tick so the button's own name/value is still sent with the form.
      setTimeout(function () { BotoKo.setBusy(btn, form.dataset.busy); }, 0);
      // Forms that download a file never leave the page, so re-enable them after a while.
      if (form.hasAttribute('data-download')) {
        setTimeout(function () { delete form.dataset.sending; BotoKo.clearBusy(btn); }, 6000);
      }
    }
  });

  // Coming back with the browser Back button can restore a page with "busy" buttons: reset them.
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    document.querySelectorAll('form[data-sending]').forEach(function (f) {
      delete f.dataset.sending;
      BotoKo.clearBusy(f.querySelector('[type=submit]'));
    });
  });

  /* ---------- Back buttons: <a href="fallback" data-back> ----------
   * If we arrived here from another BotoKo page, go back in history (keeps that page's
   * state, e.g. the admin tab). Otherwise (opened from a link, new tab, bookmark) follow
   * the fallback link, so Back never leads to a blank page or out of BotoKo. */
  function cameFromBotoKo() {
    var ref = document.referrer;
    return !!ref && ref.indexOf(location.origin + '/') === 0 && ref !== location.href && history.length > 1;
  }
  document.addEventListener('click', function (e) {
    var link = e.target.closest && e.target.closest('[data-back]');
    if (!link || e.ctrlKey || e.metaKey || e.shiftKey) return;
    if (cameFromBotoKo()) { e.preventDefault(); history.back(); }
  });

  /* ---------- page setup ---------- */
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('#toasts .toast').forEach(arm);
    // Back buttons that only make sense when there is a BotoKo page to return to.
    if (cameFromBotoKo()) {
      document.querySelectorAll('[data-back="history"]').forEach(function (b) { b.hidden = false; });
    }

    // Show / hide password
    document.querySelectorAll('[data-toggle-password]').forEach(function (btn) {
      var input = document.getElementById(btn.getAttribute('aria-controls'));
      btn.hidden = false;
      btn.addEventListener('click', function () {
        var show = input.type === 'password';
        input.type = show ? 'text' : 'password';
        btn.setAttribute('aria-pressed', String(show));
        btn.querySelector('.pw-label').textContent = show ? 'Hide' : 'Show';
        btn.querySelector('use').setAttribute('href', show ? '#i-eye-off' : '#i-eye');
        input.focus();
      });
    });

    // Password strength hint: <input data-strength="hint-element-id">. A simple guide, not a rule.
    document.querySelectorAll('[data-strength]').forEach(function (input) {
      var out = document.getElementById(input.dataset.strength);
      var bar = out.querySelector('.strength-bar > span'), text = out.querySelector('.strength-text');
      input.addEventListener('input', function () {
        var v = input.value, score = 0;
        if (v.length >= 8) score++;
        if (v.length >= 12) score++;
        if (/[a-z]/.test(v) && /[A-Z]/.test(v)) score++;
        if (/\d/.test(v)) score++;
        if (/[^A-Za-z0-9]/.test(v)) score++;
        var label = !v ? '' : v.length < 8 ? 'Too short (at least 8 characters)'
          : score <= 2 ? 'Weak: add numbers, symbols or more letters'
          : score === 3 ? 'Okay' : 'Strong';
        out.dataset.level = !v ? '' : v.length < 8 ? '0' : score <= 2 ? '1' : score === 3 ? '2' : '3';
        text.textContent = label ? 'Strength: ' + label : '';
        bar.style.transform = 'scaleX(' + (v ? Math.max(0.1, Math.min(1, score / 5)) : 0) + ')';
      });
    });

    // Copy buttons: <button data-copy="#element">
    document.querySelectorAll('[data-copy]').forEach(function (btn) {
      btn.hidden = false;
      btn.addEventListener('click', function () {
        var text = document.querySelector(btn.dataset.copy).textContent.trim();
        var done = function () {
          var label = btn.querySelector('.copy-label');
          var idle = label.dataset.idle || (label.dataset.idle = label.textContent);
          label.textContent = 'Copied!';
          setTimeout(function () { label.textContent = idle; }, 2000);
        };
        if (navigator.clipboard && window.isSecureContext) {
          navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text) && done(); });
        } else if (fallbackCopy(text)) {
          done();
        }
      });
    });
  });

  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
    ta.remove();
    if (!ok) BotoKo.toast('Could not copy. Please write the code down instead.', 'error');
    return ok;
  }

  /* ---------- shared voting devices: login fields are never pre-filled ----------
   * <form data-no-autofill> (login, create account, change password, forgot Voter ID):
   *  1. Browsers may fill in a saved Voter ID / password AFTER the page loads, so the fields are emptied a few
   *     times during the first ~2 s, until the person starts typing. Values written by the SERVER (e.g. the
   *     Voter ID after a wrong password) are kept: only what the browser filled in is removed.
   *  2. Back to this page after logging out (back/forward cache): every field is emptied again.
   *  3. When the form is sent, the passwords travel in hidden fields and the visible password boxes are
   *     emptied first, so the browser has no typed password to offer to save. (Without JavaScript the form
   *     still works normally.) */
  function noAutofill(form) {
    var fields = Array.prototype.filter.call(form.querySelectorAll('input'), function (i) {
      return i.type === 'password' || i.type === 'text' || i.type === 'email' || i.type === '' || i.type === 'search';
    });
    var touched = false;
    function mark(e) {
      // Typing / pasting / tapping = the person is filling in the form. (Autofill doesn't fire these;
      // Safari's autofill fires beforeinput with inputType "insertReplacementText": not counted.)
      if (e.isTrusted === false || e.inputType === 'insertReplacementText') return;
      touched = true;
    }
    ['keydown', 'beforeinput', 'paste', 'drop', 'compositionstart', 'pointerdown'].forEach(function (ev) { form.addEventListener(ev, mark, true); });
    function clearFilled(all) {
      fields.forEach(function (i) {
        var want = all || i.type === 'password' ? '' : i.defaultValue;     // server value stays (not for passwords)
        if (i.value !== want) i.value = want;
      });
    }
    [0, 60, 250, 600, 1200, 2000].forEach(function (ms) {
      setTimeout(function () { if (!touched) clearFilled(false); }, ms);
    });
    window.addEventListener('pageshow', function (e) {
      if (!e.persisted) return;                    // came back with Back / Forward: start empty
      touched = false;
      clearFilled(true);
      form.querySelectorAll('input[data-sent-copy]').forEach(function (h) { h.remove(); });
      fields.forEach(function (i) { if (i.dataset.name) { i.name = i.dataset.name; delete i.dataset.name; } });
      delete form.dataset.sending;
      if (BotoKo.clearBusy) BotoKo.clearBusy(form.querySelector('[type=submit]'));
    });
    form.addEventListener('submit', function () {
      fields.forEach(function (i) {
        if (i.type !== 'password' || !i.name) return;
        var copy = document.createElement('input');
        copy.type = 'hidden';
        copy.name = i.name;
        copy.value = i.value;
        copy.setAttribute('data-sent-copy', '');
        form.appendChild(copy);
        i.dataset.name = i.name;
        i.removeAttribute('name');
        i.value = '';
      });
    });
  }
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('form[data-no-autofill]').forEach(noAutofill);
  });

  /* ---------- login changed in another tab of this browser ----------
   * One browser = one login (one cookie). Every page says who is logged in (<meta name="botoko-who">) and
   * writes it to localStorage; the OTHER open BotoKo tabs get a "storage" event and show a banner
   * ("You logged in as the admin in another tab - reload to continue") instead of failing on their next form. */
  var whoMeta = document.querySelector('meta[name="botoko-who"]');
  var me = whoMeta ? whoMeta.getAttribute('content') || '' : null;
  var WHO_KEY = 'botoko-who';
  var authBanner = null;
  function showAuthBanner(v) {
    if (!authBanner) {
      authBanner = document.createElement('div');
      authBanner.className = 'auth-banner';
      authBanner.setAttribute('role', 'alert');
      authBanner.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-alert"></use></svg>' +
        '<span class="ab-text"></span><button type="button" class="btn btn-sm btn-primary">Reload</button>';
      authBanner.querySelector('button').addEventListener('click', function () { location.reload(); });
      document.body.insertBefore(authBanner, document.body.firstChild);
    }
    authBanner.querySelector('.ab-text').textContent = v.who
      ? 'You logged in as ' + (v.label || 'someone else') + ' in another tab - reload to continue.'
      : 'You logged out in another tab - reload to continue.';
    authBanner.hidden = false;
  }
  if (me !== null) {
    try { localStorage.setItem(WHO_KEY, JSON.stringify({ who: me, label: whoMeta.dataset.label || '', t: Date.now() })); }
    catch (err) { /* storage blocked: no banner, forms still answer with a friendly message */ }
    window.addEventListener('storage', function (e) {
      if (e.key !== WHO_KEY || !e.newValue) return;
      var v;
      try { v = JSON.parse(e.newValue); } catch (err) { return; }
      if (v.who === me) { if (authBanner) authBanner.hidden = true; } else showAuthBanner(v);
    });
  }

  /* Fresh CSRF token (same login only): after the tab was in the background for 10+ minutes, and after an
   * RPC "wrong token" answer (rpc.js). If the login changed meanwhile, the token is NOT replaced (old forms
   * stay refused) and the banner explains why. Resolves true when the page has a fresh token. */
  BotoKo.refreshToken = function () {
    return fetch('/session/refresh', { cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (me !== null && d.who !== me) { showAuthBanner(d); return false; }
        var m = document.querySelector('meta[name="csrf-token"]');
        if (m) m.setAttribute('content', d.csrf);
        document.querySelectorAll('input[name="csrf"]').forEach(function (i) { i.value = d.csrf; });
        return true;
      })
      .catch(function () { return false; });
  };
  var hiddenAt = 0;
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { hiddenAt = Date.now(); return; }
    if (hiddenAt && Date.now() - hiddenAt > 10 * 60 * 1000) BotoKo.refreshToken();
    hiddenAt = 0;
  });
})();
