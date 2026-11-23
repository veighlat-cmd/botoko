/* BotoKo motion: small, tasteful animations (no libraries). Loaded on every page after app.js.
 *
 *   - Button ripple on click
 *   - Ballot positions / results cards fade up (staggered) when they scroll into view
 *   - Count-up numbers: BotoKo.setCount(el, value, suffix) (admin summary cards, live results)
 *   - Password eye icon pop when toggled
 *   - Receipt after a fresh vote: "Vote submitted" toast + ~2 s of light confetti (small canvas)
 *
 * Everything respects "reduce motion" (Windows: Settings > Accessibility > Visual effects >
 * Animation effects off; phones: Reduce motion): then nothing animates, values just appear.
 * Only transform and opacity are animated, so it stays smooth on phones.
 */
(function () {
  'use strict';
  var BotoKo = window.BotoKo = window.BotoKo || {};
  var mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  function reduced() { return !!(mq && mq.matches); }
  BotoKo.reducedMotion = reduced;

  // Restart a CSS animation class on an element (e.g. "pop" every time it is selected).
  BotoKo.replay = function (el, cls) {
    if (!el || reduced()) return;
    el.classList.remove(cls);
    void el.getBoundingClientRect();
    el.classList.add(cls);
    el.addEventListener('animationend', function done() {
      el.classList.remove(cls);
      el.removeEventListener('animationend', done);
    });
  };

  /* ---------- count-up numbers ---------- */
  // BotoKo.setCount(el, 42) or BotoKo.setCount(el, 30, '%'): animates from the number shown now.
  BotoKo.setCount = function (el, value, suffix) {
    if (!el) return;
    suffix = suffix || '';
    var to = Number(value) || 0;
    var from = parseFloat(String(el.textContent).replace(/[^\d.\-]/g, ''));
    if (el._countTo === to && el._counting) return;
    el._countTo = to;
    if (reduced() || isNaN(from) || from === to || document.hidden) {
      el.textContent = to + suffix;
      el._counting = false;
      return;
    }
    var start = null, dur = Math.min(900, 350 + Math.abs(to - from) * 12);
    el._counting = true;
    function step(t) {
      if (el._countTo !== to) return;                 // a newer value took over
      if (start === null) start = t;
      var k = Math.min(1, (t - start) / dur);
      var eased = 1 - Math.pow(1 - k, 3);              // ease-out
      el.textContent = Math.round(from + (to - from) * eased) + suffix;
      if (k < 1) requestAnimationFrame(step); else el._counting = false;
    }
    requestAnimationFrame(step);
  };

  // Admin summary cards: count up from 0 when the page opens.
  document.querySelectorAll('.summary-cards .stat-value').forEach(function (el) {
    var text = el.textContent.trim(), m = text.match(/^(\d+)(%?)$/);
    if (!m || reduced()) return;
    el.textContent = '0' + m[2];
    BotoKo.setCount(el, Number(m[1]), m[2]);
  });

  /* ---------- button ripple ---------- */
  document.addEventListener('pointerdown', function (e) {
    var btn = e.target.closest && e.target.closest('.btn, .tab');
    if (!btn || btn.disabled || reduced() || e.button !== 0) return;
    var r = btn.getBoundingClientRect(), size = Math.max(r.width, r.height) * 2;
    var dot = document.createElement('span');
    dot.className = 'ripple';
    dot.setAttribute('aria-hidden', 'true');
    dot.style.width = dot.style.height = size + 'px';
    dot.style.left = (e.clientX - r.left - size / 2) + 'px';
    dot.style.top = (e.clientY - r.top - size / 2) + 'px';
    btn.appendChild(dot);
    setTimeout(function () { dot.remove(); }, 600);
  }, { passive: true });

  /* ---------- password eye: small pop when toggled ---------- */
  document.addEventListener('click', function (e) {
    var t = e.target.closest && e.target.closest('[data-toggle-password]');
    if (t) BotoKo.replay(t.querySelector('svg'), 'eye-pop');
  });

  /* ---------- fade-up when scrolled into view (ballot positions, results cards) ---------- */
  function reveal(selector, childSelector) {
    var groups = document.querySelectorAll(selector);
    if (!groups.length) return;
    groups.forEach(function (g) {
      if (childSelector) {
        g.querySelectorAll(childSelector).forEach(function (c, i) { c.style.setProperty('--i', i); });
      }
    });
    if (reduced() || !('IntersectionObserver' in window)) {
      groups.forEach(function (g) { g.classList.add('is-in', 'reveal-done'); });
      return;
    }
    document.documentElement.classList.add('motion-reveal');   // CSS hides items until .is-in
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        var g = en.target;
        g.classList.add('is-in');
        io.unobserve(g);
        // When the fade-up is over, drop it (so a later "pop" on a card can't replay the fade-up).
        var n = childSelector ? g.querySelectorAll(childSelector).length : 1;
        setTimeout(function () { g.classList.add('reveal-done'); }, 520 + n * 70);
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.05 });
    groups.forEach(function (g) { io.observe(g); });
  }
  reveal('#ballot fieldset.position', '.cand');
  BotoKo.revealResults = function () { reveal('.results-grid > .result-card:not(.is-in)'); };

  /* ---------- receipt after a fresh vote: toast + light confetti (about 2 s) ---------- */
  function confetti() {
    var canvas = document.createElement('canvas');
    canvas.className = 'confetti';
    canvas.setAttribute('aria-hidden', 'true');
    document.body.appendChild(canvas);
    var ctx = canvas.getContext('2d'), dpr = Math.min(2, window.devicePixelRatio || 1);
    var W = canvas.width = innerWidth * dpr, H = canvas.height = innerHeight * dpr;
    var colors = ['#1442a8', '#c8102e', '#fcd116', '#136b40', '#7c4dff'];
    var bits = [];
    for (var i = 0; i < 110; i++) {
      bits.push({
        x: W * (0.2 + Math.random() * 0.6), y: H * 0.28 + Math.random() * 40 * dpr,
        vx: (Math.random() - 0.5) * 9 * dpr, vy: (-6 - Math.random() * 7) * dpr,
        w: (5 + Math.random() * 5) * dpr, h: (8 + Math.random() * 6) * dpr,
        a: Math.random() * Math.PI, va: (Math.random() - 0.5) * 0.3, c: colors[i % colors.length]
      });
    }
    var start = null;
    function frame(t) {
      if (start === null) start = t;
      var age = t - start;
      ctx.clearRect(0, 0, W, H);
      ctx.globalAlpha = Math.max(0, 1 - Math.max(0, age - 1400) / 600);   // fade out at the end
      bits.forEach(function (b) {
        b.vy += 0.28 * dpr; b.vx *= 0.99; b.x += b.vx; b.y += b.vy; b.a += b.va;
        ctx.save(); ctx.translate(b.x, b.y); ctx.rotate(b.a);
        ctx.fillStyle = b.c; ctx.fillRect(-b.w / 2, -b.h / 2, b.w, b.h);
        ctx.restore();
      });
      if (age < 2000) requestAnimationFrame(frame); else canvas.remove();
    }
    requestAnimationFrame(frame);
  }
  var JUST_VOTED = 'botoko-just-voted';
  if (document.querySelector('.receipt-page')) {
    var fresh = false;
    try { fresh = sessionStorage.getItem(JUST_VOTED) === '1'; sessionStorage.removeItem(JUST_VOTED); } catch (e) { /* blocked */ }
    if (fresh) {
      document.documentElement.classList.add('just-voted');
      if (BotoKo.toast) BotoKo.toast('Vote submitted. Salamat sa pagboto!', 'ok');
      if (!reduced()) setTimeout(confetti, 450);
    }
  }
  BotoKo.markJustVoted = function () {
    try { sessionStorage.setItem(JUST_VOTED, '1'); } catch (e) { /* blocked */ }
  };
})();
