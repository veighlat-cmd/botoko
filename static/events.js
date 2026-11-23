/* Event pages: tabs (Current / Upcoming / Previous), countdowns to an event's start,
 * and confirmations for links (e.g. "Log out?"). Small vanilla JS, loaded on every page.
 */
(function () {
  'use strict';

  /* ---------- tabs: <nav data-tabs data-default="current"> with <a href="#key" aria-controls="panel-key"> ---------- */
  document.querySelectorAll('[data-tabs]').forEach(function (bar) {
    var tabs = Array.prototype.slice.call(bar.querySelectorAll('[role=tab]'));
    function show(key) {
      if (!tabs.some(function (t) { return t.hash === '#' + key; })) key = bar.dataset.default;
      tabs.forEach(function (t) {
        var on = t.hash === '#' + key;
        t.setAttribute('aria-selected', String(on));
        t.tabIndex = on ? 0 : -1;
        var panel = document.getElementById(t.getAttribute('aria-controls'));
        if (panel) panel.classList.toggle('is-active', on);
      });
    }
    tabs.forEach(function (t) {
      t.addEventListener('click', function (e) {
        e.preventDefault();
        history.replaceState(null, '', t.hash);
        show(t.hash.slice(1));
      });
    });
    bar.addEventListener('keydown', function (e) {             // arrow keys move between tabs
      var i = tabs.indexOf(document.activeElement);
      if (i < 0 || (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft')) return;
      var next = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      next.focus();
      next.click();
      e.preventDefault();
    });
    show(location.hash.slice(1));
  });

  /* ---------- countdown: <div data-countdown-to="<ms>"> with [data-cd=days|hours|minutes|seconds] ---------- */
  document.querySelectorAll('[data-countdown-to]').forEach(function (box) {
    var target = Number(box.dataset.countdownTo);
    if (!target) return;
    var reloaded = false;
    function tick() {
      var left = Math.max(0, target - Date.now());
      var s = Math.floor(left / 1000);
      var parts = { days: Math.floor(s / 86400), hours: Math.floor(s / 3600) % 24,
                    minutes: Math.floor(s / 60) % 60, seconds: s % 60 };
      Object.keys(parts).forEach(function (k) {
        var el = box.querySelector('[data-cd="' + k + '"]');
        if (el) el.textContent = k === 'days' ? parts[k] : String(parts[k]).padStart(2, '0');
      });
      if (left <= 0 && !reloaded) {             // voting just opened: let the server decide what to show
        reloaded = true;
        var done = box.querySelector('[data-cd-done]');
        if (done) done.hidden = false;
        setTimeout(function () { location.reload(); }, 2500);
        return;
      }
      setTimeout(tick, 1000 - (Date.now() % 1000));
    }
    tick();
  });

  // Small "starts in 2d 5h" text on upcoming event cards
  document.querySelectorAll('[data-countdown]').forEach(function (btn) {
    var target = Number(btn.dataset.countdown);
    if (!target) return;
    var left = target - Date.now();
    if (left <= 0) return;
    var h = Math.floor(left / 3600000), d = Math.floor(h / 24);
    var label = d >= 1 ? d + 'd ' + (h % 24) + 'h' : h >= 1 ? h + 'h ' + Math.floor(left / 60000) % 60 + 'm'
                                                         : Math.max(1, Math.floor(left / 60000)) + 'm';
    var span = document.createElement('span');
    span.className = 'cd-chip';
    span.textContent = 'in ' + label;
    btn.appendChild(span);
  });

  /* ---------- <a data-confirm-link="Question?">: ask first (e.g. log out) ---------- */
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[data-confirm-link]');
    if (!a || !window.BotoKo || !BotoKo.confirm) return;
    e.preventDefault();
    BotoKo.confirm({ title: a.dataset.confirmLink, text: a.dataset.confirmText || '', ok: a.dataset.confirmOk || 'Log out' },
      function () { location.href = a.href; });
  });
})();
