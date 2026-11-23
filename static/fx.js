/* BotoKo interface effects (small, no libraries). Loaded on every page after app.js and motion.js.
 *   - header gets a soft blur + shadow when the page is scrolled; "Back to top" button
 *   - tabs: a sliding indicator moves to the selected tab
 *   - candidate cards: very subtle 3D tilt that follows the mouse (desktop only, max 4 degrees)
 *   - inputs: green check once a typed value is valid
 *   - tooltips for icon-only buttons and for disabled buttons (the reason why)
 *   - phones: a light vibration when a candidate is chosen and when the vote is submitted
 *   - BotoKo.setDone(btn, label): a button briefly shows a check mark after success
 *   - numbers with [data-countup] count up when they appear
 * Movement respects "reduce motion" (then nothing moves; colours and focus still change).
 */
(function () {
  'use strict';
  var BotoKo = window.BotoKo = window.BotoKo || {};
  var mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  function reduced() { return !!(mq && mq.matches); }
  var fine = window.matchMedia && window.matchMedia('(hover: hover) and (pointer: fine)').matches;

  /* ---------- header blur on scroll + back to top ---------- */
  var topbar = document.querySelector('.topbar');
  var toTop = document.createElement('button');
  toTop.type = 'button';
  toTop.className = 'to-top';
  toTop.setAttribute('aria-label', 'Back to top');
  toTop.dataset.tip = 'Back to top';
  toTop.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-back"></use></svg>';
  toTop.tabIndex = -1;
  document.body.appendChild(toTop);
  toTop.addEventListener('click', function () {
    window.scrollTo({ top: 0, behavior: reduced() ? 'auto' : 'smooth' });
    var main = document.getElementById('main');
    if (main) main.focus({ preventScroll: true });
  });
  var ticking = false;
  function onScroll() {
    ticking = false;
    var y = window.scrollY || document.documentElement.scrollTop;
    if (topbar) topbar.classList.toggle('is-scrolled', y > 8);
    var on = y > 600;
    toTop.classList.toggle('is-on', on);
    toTop.tabIndex = on ? 0 : -1;
  }
  window.addEventListener('scroll', function () {
    if (!ticking) { ticking = true; requestAnimationFrame(onScroll); }
  }, { passive: true });
  onScroll();

  /* ---------- tabs: sliding indicator ---------- */
  function setupTabs(list) {
    var ind = document.createElement('span');
    ind.className = 'tab-indicator';
    ind.setAttribute('aria-hidden', 'true');
    list.insertBefore(ind, list.firstChild);
    list.classList.add('has-indicator');
    function place() {
      var sel = list.querySelector('[role=tab][aria-selected="true"]');
      if (!sel || !sel.offsetWidth) { ind.style.opacity = '0'; return; }
      ind.style.opacity = '1';
      ind.style.height = sel.offsetHeight + 'px';
      ind.style.transform = 'translate(' + sel.offsetLeft + 'px,' + sel.offsetTop + 'px) scaleX(' + (sel.offsetWidth / 100) + ')';
    }
    new MutationObserver(place).observe(list, { attributes: true, subtree: true, attributeFilter: ['aria-selected'] });
    window.addEventListener('resize', place);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(place);
    place();
  }
  document.querySelectorAll('.tabs[role=tablist]').forEach(setupTabs);

  /* ---------- candidate cards: subtle 3D tilt (desktop mouse only) ---------- */
  if (fine) {
    document.querySelectorAll('.cand').forEach(function (card) {
      card.addEventListener('pointermove', function (e) {
        if (reduced() || e.pointerType !== 'mouse') return;
        var r = card.getBoundingClientRect();
        var x = (e.clientX - r.left) / r.width - 0.5, y = (e.clientY - r.top) / r.height - 0.5;
        card.style.setProperty('--ry', (x * 8).toFixed(2) + 'deg');       // max 4 degrees each way
        card.style.setProperty('--rx', (-y * 8).toFixed(2) + 'deg');
        card.classList.add('is-tilting');
      });
      card.addEventListener('pointerleave', function () {
        card.classList.remove('is-tilting');
        card.style.removeProperty('--rx');
        card.style.removeProperty('--ry');
      });
    });
  }

  /* ---------- inputs: green check once the typed value is valid ---------- */
  document.addEventListener('input', function (e) {
    var t = e.target;
    if (t.matches && t.matches('.field input, .field textarea')) t.classList.add('was-typed');
  });

  /* ---------- tooltips ---------- */
  var tip = document.createElement('div');
  tip.className = 'tip';
  tip.setAttribute('role', 'tooltip');
  tip.id = 'botoko-tip';
  document.body.appendChild(tip);
  var tipFor = null;
  function tipText(el) {
    if (el.classList.contains('tip-wrap')) {
      var inner = el.querySelector('button, .btn');
      return inner && (inner.disabled || inner.getAttribute('aria-disabled') === 'true') ? el.dataset.tip : '';
    }
    return el.dataset.tip || el.getAttribute('aria-label') || '';
  }
  function showTip(el) {
    var text = tipText(el);
    if (!text) return;
    tipFor = el;
    tip.textContent = text;
    var r = el.getBoundingClientRect();
    tip.style.left = '0px';
    tip.style.top = '0px';
    tip.classList.add('is-on');
    var w = tip.offsetWidth, h = tip.offsetHeight;
    var left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
    var top = r.top - h - 8 < 8 ? r.bottom + 8 : r.top - h - 8;
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }
  function hideTip() { tipFor = null; tip.classList.remove('is-on'); }
  var TIP_SEL = '[data-tip], .btn-icon[aria-label], .menu-btn[aria-label]';
  document.addEventListener('pointerover', function (e) {
    var el = e.target.closest && e.target.closest(TIP_SEL);
    if (el && el !== tipFor) showTip(el); else if (!el && tipFor) hideTip();
  });
  document.addEventListener('focusin', function (e) {
    var el = e.target.closest && e.target.closest(TIP_SEL);
    if (el) showTip(el); else hideTip();
  });
  document.addEventListener('focusout', hideTip);
  window.addEventListener('scroll', hideTip, { passive: true });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hideTip(); });

  // Disabled buttons say WHY: <button data-disabled-reason="Select at least one row first">
  document.querySelectorAll('[data-disabled-reason]').forEach(function (btn) {
    var wrap = document.createElement('span');
    wrap.className = 'tip-wrap';
    wrap.dataset.tip = btn.dataset.disabledReason;
    btn.parentNode.insertBefore(wrap, btn);
    wrap.appendChild(btn);
  });


  /* ---------- light / dark theme (dark is the default; remembered on this device) ---------- */
  document.addEventListener('click', function (e) {
    if (!e.target.closest || !e.target.closest('[data-theme-toggle]')) return;
    var root = document.documentElement;
    var next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('botoko-theme', next); } catch (err) { /* private mode */ }
    hideTip();
  });

  /* ---------- admin: sidebar (phones: slide-in menu; desktop: collapse), breadcrumb, quick actions ---------- */
  var shell = document.getElementById('admin-shell');
  if (shell) {
    var toggleBtn = shell.querySelector('[data-side-toggle]');
    var setOpen = function (open) {
      shell.classList.toggle('is-open', open);
      if (toggleBtn) toggleBtn.setAttribute('aria-expanded', String(open));
      if (open) { var sel = shell.querySelector('.side-nav [aria-selected="true"]'); if (sel) sel.focus(); }
    };
    if (toggleBtn) toggleBtn.addEventListener('click', function () { setOpen(!shell.classList.contains('is-open')); });
    shell.addEventListener('click', function (e) {
      if (e.target.closest('[data-side-close]') || (e.target.closest('.side-nav .tab') && shell.classList.contains('is-open'))) setOpen(false);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && shell.classList.contains('is-open')) { setOpen(false); if (toggleBtn) toggleBtn.focus(); }
    });
    try { if (localStorage.getItem('botoko-side') === 'collapsed') shell.classList.add('is-collapsed'); } catch (err) { /* ignore */ }
    var collapse = shell.querySelector('[data-side-collapse]');
    if (collapse) collapse.addEventListener('click', function () {
      var c = shell.classList.toggle('is-collapsed');
      try { localStorage.setItem('botoko-side', c ? 'collapsed' : 'open'); } catch (err) { /* ignore */ }
      window.dispatchEvent(new Event('resize'));                 // moves the tab indicator
    });
    // "New event" button: aria-expanded follows the card (also when a quick action opens it)
    document.querySelectorAll('details[id]').forEach(function (d) {
      var btns = document.querySelectorAll('[data-open-details="' + d.id + '"][aria-expanded]');
      if (!btns.length) return;
      d.addEventListener('toggle', function () { btns.forEach(function (b) { b.setAttribute('aria-expanded', String(d.open)); }); });
    });
    var crumb = shell.querySelector('[data-crumb]');
    var syncCrumb = function () {
      var sel = shell.querySelector('.side-nav [aria-selected="true"]');
      if (crumb && sel) crumb.textContent = sel.dataset.label || sel.textContent.trim();
    };
    new MutationObserver(syncCrumb).observe(shell.querySelector('.side-nav'), { attributes: true, subtree: true, attributeFilter: ['aria-selected'] });
    syncCrumb();
    document.addEventListener('click', function (e) {
      var q = e.target.closest && e.target.closest('[data-open-details]');
      if (q) {
        var d = document.getElementById(q.dataset.openDetails);
        // The "New event" button itself toggles the card; quick actions always open it.
        if (d && d.open && q.hasAttribute('aria-expanded')) { d.open = false; return; }
        if (d) { d.open = true; d.scrollIntoView({ behavior: reduced() ? 'auto' : 'smooth', block: 'start' }); var f = d.querySelector('input:not([type=hidden])'); if (f) setTimeout(function () { f.focus({ preventScroll: true }); }, 300); }
        return;
      }
      var cl = e.target.closest && e.target.closest('[data-close-details]');
      if (cl) {
        var dd = document.getElementById(cl.dataset.closeDetails);
        if (dd) { dd.open = false; var opener = document.querySelector('[data-open-details="' + dd.id + '"][aria-expanded]'); if (opener) opener.focus(); }
        return;
      }
      var fs = e.target.closest && e.target.closest('[data-focus-select]');
      if (fs) {
        e.preventDefault();
        var sel = document.getElementById('event-select');
        if (sel) { sel.scrollIntoView({ behavior: reduced() ? 'auto' : 'smooth', block: 'center' }); sel.focus({ preventScroll: true }); }
      }
    });
  }

  /* ---------- phones: light vibration on choosing a candidate / submitting the vote ---------- */
  function buzz(pattern) {
    try { if (navigator.vibrate && !reduced()) navigator.vibrate(pattern); } catch (err) { /* not supported */ }
  }
  document.addEventListener('change', function (e) {
    if (e.target.matches && e.target.matches('.choice input') && e.target.checked) buzz(12);
  });
  document.addEventListener('click', function (e) {
    if (e.target.closest && e.target.closest('#confirmBtn')) buzz([18, 40, 18]);
  });

  /* ---------- a button briefly shows a check mark after success ---------- */
  BotoKo.setDone = function (btn, label) {
    if (!btn) return;
    btn.classList.remove('is-loading');
    btn.classList.add('is-done');
    btn.removeAttribute('aria-busy');
    btn.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-check"></use></svg><span>' +
      (BotoKo.esc ? BotoKo.esc(label || 'Done') : (label || 'Done')) + '</span>';
  };

  /* ---------- numbers count up when they appear: <b data-countup>42</b> ---------- */
  var counters = Array.prototype.slice.call(document.querySelectorAll('[data-countup]'));
  if (counters.length && BotoKo.setCount && 'IntersectionObserver' in window && !reduced()) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        io.unobserve(en.target);
        var m = /^(\d+)(.*)$/.exec(en.target.textContent.trim());
        if (!m) return;
        en.target.textContent = '0' + m[2];
        BotoKo.setCount(en.target, Number(m[1]), m[2]);
      });
    }, { threshold: 0.4 });
    counters.forEach(function (c) { io.observe(c); });
  }
})();
