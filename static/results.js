/* Live results board (used on /results and in the admin "Results" tab).
 *
 * Every 2 seconds it calls the remote procedure rpc("get_results") (JSON-RPC, static/rpc.js) and updates the page. For each position it
 * draws two charts with inline SVG (no chart library):
 *   - a horizontal bar chart, sorted by votes, leader marked with a "Leading" pill
 *   - a donut showing each candidate's share, with a legend
 * Each candidate keeps one colour (by ballot order) in both charts.
 *
 * Smooth updates: the chart HTML is built once; after that each 2 s update only
 * animates from the current values to the new ones (no redraw from zero, no flicker).
 * Polling pauses while the tab is hidden, and slows down while the server is unreachable.
 */
(function () {
  'use strict';
  var INTERVAL = 2000;
  var DURATION = 700;                     // animation length (ms)
  var SVG = 'http://www.w3.org/2000/svg';
  var REDUCED = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var esc = function (s) { return BotoKo.esc(s); };
  var boards = [];
  var chartCount = 0;                     // gives each chart's photo mask a unique id

  /* ---------- tiny animation helper ----------
   * new Tween(start, apply): apply(values) draws the values.
   * tween.go(target) animates every number in `target` from where it is now. */
  var running = [], frame = 0;
  function Tween(start, apply) {
    this.apply = apply;
    this.cur = copy(start);
    this.to = copy(start);
  }
  function copy(o) { var r = {}; for (var k in o) r[k] = o[k]; return r; }
  Tween.prototype.go = function (target, instant) {
    this.to = copy(target);
    if (instant || REDUCED || document.hidden) {
      this.cur = copy(target);
      var i = running.indexOf(this);
      if (i >= 0) running.splice(i, 1);
      this.apply(this.cur);
      return;
    }
    this.from = copy(this.cur);
    this.t0 = performance.now();
    if (running.indexOf(this) < 0) running.push(this);
    if (!frame) frame = requestAnimationFrame(step);
    // Safety net: if the browser throttles animation frames (battery saver, background
    // iframe), still land exactly on the final values shortly after the animation ends.
    setTimeout(finishDue, DURATION + 120);
  };
  function finishDue() {
    var now = performance.now();
    running.slice().forEach(function (t) {
      if (now - t.t0 < DURATION) return;
      t.cur = copy(t.to);
      t.apply(t.cur);
      running.splice(running.indexOf(t), 1);
    });
  }
  function step(now) {
    running.slice().forEach(function (t) {
      // Clamp: the frame's timestamp can be slightly older than t0 (would give p < 0).
      var p = Math.max(0, Math.min(1, (now - t.t0) / DURATION));
      var e = 1 - Math.pow(1 - p, 3);                       // ease-out
      for (var k in t.to) t.cur[k] = t.from[k] + (t.to[k] - t.from[k]) * e;
      t.apply(t.cur);
      if (p === 1) running.splice(running.indexOf(t), 1);
    });
    frame = running.length ? requestAnimationFrame(step) : 0;
  }

  function svgEl(tag, attrs, parent) {
    var el = document.createElementNS(SVG, tag);
    for (var k in attrs) el.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(el);
    return el;
  }
  // Candidate colour by ballot order: slots 1-8, then a neutral grey (labels carry identity).
  function color(i) { return 'var(--series-' + (i < 8 ? i + 1 : 'x') + ')'; }
  // Bar starting at x: a pill (both ends rounded); very short bars get flatter ends.
  function barPath(x, w, y, h) {
    if (w < 0.5) return '';
    var rx = Math.min(h / 2, w / 2), ry = h / 2;
    return 'M' + (x + rx) + ',' + y + 'h' + (w - 2 * rx) + 'a' + rx + ',' + ry + ' 0 0 1 0,' + h +
      'h' + (2 * rx - w) + 'a' + rx + ',' + ry + ' 0 0 1 0,' + (-h) + 'z';
  }
  // Shorten an SVG text with "..." until it fits maxWidth (full name stays in the legend/tooltip).
  function fitText(el, text, maxWidth) {
    el.textContent = text;
    if (maxWidth <= 0 || el.getComputedTextLength() <= maxWidth) return;
    var s = text;
    while (s.length > 1) {
      s = s.slice(0, -1);
      el.textContent = s.trim() + '…';
      if (el.getComputedTextLength() <= maxWidth) return;
    }
  }

  /* ======================= one position: bar chart + donut ======================= */
  function PositionChart(p) {
    var self = this;
    this.id = p.id;
    this.name = p.name;
    var card = this.card = document.createElement('section');
    card.className = 'card result-card';
    card.innerHTML =
      '<div class="card-head"><div class="rc-title"><h2>' + esc(p.name) + '</h2>' +
        '<p class="rc-lead" data-leadline></p></div><span class="badge" data-total></span></div>' +
      '<p class="chart-empty" data-empty hidden>' + BotoKo.icon('inbox') +
        '<span>No votes yet for this position. The charts fill in as ballots arrive.</span></p>' +
      '<figure class="chart"><figcaption class="chart-title">Votes per candidate</figcaption></figure>' +
      '<div class="chart-share">' +
        '<figure class="chart"><figcaption class="sr-only">Share of votes</figcaption></figure>' +
        '<ul class="legend" aria-label="Share of ' + esc(p.name) + ' votes"></ul>' +
      '</div>';
    this.totalEl = card.querySelector('[data-total]');
    this.emptyEl = card.querySelector('[data-empty]');
    this.leadEl = card.querySelector('[data-leadline]');

    // Bar chart (no viewBox: we lay it out in real pixels so text never shrinks).
    this.bars = svgEl('svg', { 'class': 'hbars', role: 'img' }, card.querySelectorAll('figure')[0]);
    // Round mask for the candidate photos (every row's photo sits at the same spot in its row).
    var clipId = 'hbclip-' + (++chartCount);
    var defs = svgEl('defs', {}, this.bars);
    this.clip = svgEl('circle', {}, svgEl('clipPath', { id: clipId }, defs));
    var chartNo = chartCount;
    // Donut (viewBox: the ring scales with its CSS size; circumference = 100 units).
    var donut = svgEl('svg', { 'class': 'donut', viewBox: '0 0 42 42', 'aria-hidden': 'true' }, card.querySelectorAll('figure')[1]);
    svgEl('circle', { 'class': 'dn-track', cx: 21, cy: 21, r: 15.9155 }, donut);
    var ring = svgEl('g', { transform: 'rotate(-90 21 21)' }, donut);   // start at 12 o'clock
    this.dnTotal = svgEl('text', { 'class': 'dn-total', x: 21, y: 22.5 }, donut);
    svgEl('text', { 'class': 'dn-label', x: 21, y: 27.5 }, donut).textContent = 'votes';
    var legend = card.querySelector('.legend');

    this.items = p.candidates.map(function (c, i) {
      var it = { c: c, i: i, lead: '', votes: 0 };
      // bar row: [name ... value] then the bar under it
      it.row = svgEl('g', { 'class': 'hb-row' }, self.bars);
      it.tip = svgEl('title', {}, it.row);
      it.leadBg = svgEl('rect', { 'class': 'hb-leadbg', x: 0, y: 0 }, it.row);   // soft gold card behind the leader
      var gid = 'hbg-' + chartNo + '-' + i;
      var grad = svgEl('linearGradient', { id: gid, x1: 0, x2: 1, y1: 0, y2: 0 }, defs);
      svgEl('stop', { offset: 0, style: 'stop-color:' + color(i) + ';stop-opacity:.55' }, grad);
      svgEl('stop', { offset: 1, style: 'stop-color:' + color(i) }, grad);
      it.hl = svgEl('rect', { 'class': 'hb-hl', x: 0, y: 0, rx: 6 }, it.row);   // flashes when votes go up
      // photo (or initials) at the start of the row
      var tone = BotoKo.tone(c.party || c.name);
      it.av = svgEl('g', { 'class': 'hb-av' }, it.row);
      if (c.photo) {
        it.avImg = svgEl('image', { href: c.photo, preserveAspectRatio: 'xMidYMid slice', 'clip-path': 'url(#' + clipId + ')' }, it.av);
      } else {
        it.avBg = svgEl('circle', { style: 'fill:var(--tone-' + tone + '-bg)' }, it.av);
        it.avText = svgEl('text', { 'text-anchor': 'middle', style: 'fill:var(--tone-' + tone + '-fg)' }, it.av);
        it.avText.textContent = BotoKo.initials(c.name);
      }
      it.avRing = svgEl('circle', { 'class': 'hb-ring' }, it.av);
      it.nameEl = svgEl('text', { 'class': 'hb-name', x: 0 }, it.row);
      it.valEl = svgEl('text', { 'class': 'hb-val', 'text-anchor': 'end' }, it.row);
      it.track = svgEl('rect', { 'class': 'hb-track', x: 0, rx: 4 }, it.row);
      it.bar = svgEl('path', { 'class': 'hb-bar', style: 'fill:url(#' + gid + ')' }, it.row);
      it.pill = svgEl('g', { 'class': 'hb-pill' }, it.row);
      it.pillRect = svgEl('rect', {}, it.pill);
      it.pillText = svgEl('text', {}, it.pill);
      // donut slice
      it.seg = svgEl('circle', { 'class': 'dn-seg', cx: 21, cy: 21, r: 15.9155, style: 'stroke:' + color(i) }, ring);
      // legend row
      var li = document.createElement('li');
      li.innerHTML = '<span class="swatch" style="background:' + color(i) + '"></span>' +
        (c.photo
          ? '<img class="avatar lg-photo" src="' + esc(c.photo) + '" alt="Photo of ' + esc(c.name) + '" width="28" height="28" loading="lazy" decoding="async">'
          : '<span class="avatar lg-photo tone-' + tone + '" aria-hidden="true">' + esc(BotoKo.initials(c.name)) + '</span>') +
        '<span class="lg-text"><span class="lg-name">' + esc(c.name) + '</span>' +
        (c.party ? '<span class="lg-party">' + esc(c.party) + '</span>' : '') + '</span>' +
        '<span class="lg-val">0 · 0%</span>';
      legend.appendChild(li);
      it.lgVal = li.querySelector('.lg-val');
      it.tween = new Tween({ rank: i, share: 0, votes: 0, len: 0, off: 0 }, function (v) { self.draw(it, v); });
      return it;
    });
  }

  // Measure the space we have and position everything (also after resize / projector mode).
  PositionChart.prototype.layout = function () {
    var W = this.bars.getBoundingClientRect().width;
    if (!W) { this.g = null; return false; }                // hidden (e.g. admin tab not open)
    var fs = parseFloat(getComputedStyle(this.bars).fontSize) || 16;
    var g = this.g = {
      W: W,
      nameY: Math.round(fs * 1.05),
      barY: Math.round(fs * 1.5),
      barH: Math.min(26, Math.max(12, Math.round(fs * 0.95))),
      fs: fs
    };
    g.rowH = g.barY + g.barH + Math.round(fs * 0.95);
    this.bars.setAttribute('height', this.items.length * g.rowH - Math.round(fs * 0.6));
    // Room for the value label at the end of each bar, sized for the current vote total.
    var first = this.items[0];
    this.valDigits = Math.max(3, String(this.total || 0).length);
    if (first) {
      first.valEl.textContent = new Array(this.valDigits + 1).join('8') + ' · 100%';
      g.valW = first.valEl.getComputedTextLength() + Math.round(fs * 0.6);
    }
    // Photo column on the left: as tall as the name line + bar together.
    g.av = g.barY + g.barH;
    g.x0 = g.av + Math.round(fs * 0.6);                     // where names and bars start
    g.trackW = Math.max(20, W - g.x0 - (g.valW || 0));
    var r = g.av / 2;
    this.clip.setAttribute('cx', r);
    this.clip.setAttribute('cy', r);
    this.clip.setAttribute('r', r);
    var self = this;
    this.items.forEach(function (it) {
      if (it.avImg) {
        it.avImg.setAttribute('width', g.av);
        it.avImg.setAttribute('height', g.av);
      } else {
        it.avBg.setAttribute('cx', r); it.avBg.setAttribute('cy', r); it.avBg.setAttribute('r', r);
        it.avText.setAttribute('x', r);
        it.avText.setAttribute('y', r + g.av * 0.13);
        it.avText.setAttribute('font-size', Math.round(g.av * 0.36));
      }
      it.avRing.setAttribute('cx', r); it.avRing.setAttribute('cy', r); it.avRing.setAttribute('r', r - 0.5);
      it.valEl.setAttribute('x', W);
      it.valEl.setAttribute('y', g.barY + g.barH / 2 + fs * 0.35);   // centred on the bar
      it.nameEl.setAttribute('x', g.x0);
      it.nameEl.setAttribute('y', g.nameY);
      it.track.setAttribute('x', g.x0);
      it.track.setAttribute('y', g.barY);
      it.track.setAttribute('height', g.barH);
      it.track.setAttribute('width', g.trackW);
      it.track.setAttribute('rx', g.barH / 2);
      var pad = Math.round(fs * 0.4);
      it.leadBg.setAttribute('x', -pad);
      it.leadBg.setAttribute('y', -pad);
      it.leadBg.setAttribute('width', W + 2 * pad);
      it.leadBg.setAttribute('height', g.barY + g.barH + 2 * pad);
      it.leadBg.setAttribute('rx', Math.round(fs * 0.75));
      it.hl.setAttribute('width', W);
      it.hl.setAttribute('height', g.barY + g.barH + Math.round(fs * 0.3));
      self.fitName(it);
      it.tween.apply(it.tween.cur);
    });
    return true;
  };

  // Line 1: name, then the "Leading"/"Tied" pill. Line 2: the bar, value at its end.
  PositionChart.prototype.fitName = function (it) {
    var g = this.g;
    if (!g) return;
    var pillW = 0;
    if (it.lead) {
      it.pill.removeAttribute('display');
      it.pill.setAttribute('class', 'hb-pill' + (it.lead === 'tied' ? ' tied' : ''));
      it.pillText.textContent = it.lead === 'tied' ? (this.final ? 'Tie: last seat' : 'Tied')
                                                   : (this.final ? '★ Winner' : '★ Leading');
      var tw = it.pillText.getComputedTextLength(), ph = Math.round(g.fs * 1.3), pad = Math.round(g.fs * 0.5);
      pillW = tw + 2 * pad;
      it.pillRect.setAttribute('width', pillW);
      it.pillRect.setAttribute('height', ph);
      it.pillRect.setAttribute('rx', ph / 2);
      it.pillRect.setAttribute('y', g.nameY - Math.round(g.fs * 0.98));
      it.pillText.setAttribute('x', pad);
      it.pillText.setAttribute('y', g.nameY - Math.round(g.fs * 0.15));
    } else {
      it.pill.setAttribute('display', 'none');
    }
    var gap = Math.round(g.fs * 0.5);
    fitText(it.nameEl, it.c.name, g.W - g.x0 - (pillW ? pillW + gap : 0));
    if (pillW) it.pill.setAttribute('transform', 'translate(' + (g.x0 + it.nameEl.getComputedTextLength() + gap) + ',0)');
  };

  // Draw one candidate at its current (animated) values.
  PositionChart.prototype.draw = function (it, v) {
    var g = this.g;
    if (g) {
      it.row.setAttribute('transform', 'translate(0,' + (v.rank * g.rowH).toFixed(1) + ')');
      it.bar.setAttribute('d', barPath(g.x0, v.share * g.trackW, g.barY, g.barH));
      it.valEl.textContent = Math.round(v.votes) + ' · ' + Math.round(v.share * 100) + '%';
    }
    // Donut slice: dash of length `len` starting at `off` (both in % of the ring).
    it.seg.setAttribute('stroke-dasharray', v.len > 0.05 ? v.len.toFixed(2) + ' ' + (100 - v.len).toFixed(2) : '0 100');
    it.seg.setAttribute('stroke-dashoffset', (-v.off).toFixed(2));
  };

  PositionChart.prototype.update = function (p, instant) {
    var self = this, total = p.total;
    this.total = total;
    // First draw after becoming visible, or the vote total got a digit longer than we made room for.
    if (!this.g) instant = !this.layout() || instant;
    else if (String(total).length > this.valDigits) this.layout();
    // "21 votes · 3 no vote" (voters may skip a position; abstain = ballots without a vote here)
    this.totalEl.textContent = total + (total === 1 ? ' vote' : ' votes') +
      (p.abstain ? ' · ' + p.abstain + ' no vote' : '');
    this.emptyEl.hidden = total > 0;
    this.dnTotal.textContent = total;

    var votes = {}, marks = {};
    p.candidates.forEach(function (c) { votes[c.id] = c.votes; marks[c.id] = c.lead; });
    this.final = !!p.final;
    this.items.forEach(function (it) { it.prev = it.votes; it.votes = votes[it.c.id] || 0; });
    // Bar order: most votes first; ties keep ballot order.
    var order = this.items.slice().sort(function (a, b) { return b.votes - a.votes || a.i - b.i; });
    var nonZero = this.items.filter(function (it) { return it.votes > 0; }).length;
    var GAP = nonZero > 1 ? 0.8 : 0;                         // ~2px surface gap between slices
    var off = 0, summary = [];

    this.items.forEach(function (it) {                       // donut + legend use ballot order
      var share = total ? it.votes / total : 0;
      // 'win' = in the top N (leading / winner), 'tie' = tied for the last seat (server: mark_winners)
      var lead = marks[it.c.id] === 'win' ? 'lead' : marks[it.c.id] === 'tie' ? 'tied' : '';
      if (lead !== it.lead) {
        var newLeader = lead === 'lead' && !instant;
        it.lead = lead; self.fitName(it);
        it.row.setAttribute('class', 'hb-row' + (lead === 'lead' ? ' is-lead' : lead === 'tied' ? ' is-tied' : ''));
        it.lgVal.parentNode.classList.toggle('is-lead', lead === 'lead');
        if (newLeader) flash(it.pill, 'is-pop');         // new leader: the badge pops once
      }
      // Votes went up: briefly highlight this candidate's row (chart + legend).
      if (!instant && it.votes > it.prev) { flash(it.hl, 'is-flash'); flash(it.lgVal.parentNode, 'is-flash'); }
      var len = share * 100;
      it.tween.go({
        rank: order.indexOf(it), share: share, votes: it.votes,
        len: Math.max(0, len - GAP), off: off + (len > GAP ? GAP / 2 : 0)
      }, instant);
      off += len;
      var pct = Math.round(share * 100);
      it.lgVal.textContent = it.votes + ' · ' + pct + '%';
      it.tip.textContent = it.c.name + (it.c.party ? ' (' + it.c.party + ')' : '') + ': ' +
        it.votes + (it.votes === 1 ? ' vote, ' : ' votes, ') + pct + '%';
    });
    order.forEach(function (it) {
      summary.push(it.c.name + ' ' + it.votes + (it.votes === 1 ? ' vote' : ' votes') + ' (' +
        (total ? Math.round(100 * it.votes / total) : 0) + '%)' +
        (it.lead === 'lead' ? (self.final ? ', winner' : ', leading') : it.lead === 'tied' ? ', tied for the last seat' : ''));
    });
    this.bars.setAttribute('aria-label', p.name + ': ' + (total ? summary.join('; ') : 'no votes yet'));
    var leaders = order.filter(function (it) { return it.lead === 'lead'; });
    var tied = order.some(function (it) { return it.lead === 'tied'; });
    this.leadEl.innerHTML = !total ? 'No votes yet' : (BotoKo.icon('crown') + '<span>' +
      (this.final ? (leaders.length > 1 ? 'Winners: ' : 'Winner: ') : 'Leading: ') +
      '<b>' + (leaders.length ? leaders.map(function (it) { return esc(it.c.name); }).join(', ') : '–') + '</b>' +
      (leaders.length === 1 ? ' · ' + Math.round(100 * leaders[0].votes / total) + '%' : '') +
      (tied ? ' · tie for the last seat' : '') + '</span>');
  };

  /* ======================= the whole board ======================= */
  function Board(root) {
    var self = this;
    this.root = root;
    this.src = root.dataset.src;
    this.event = Number(root.dataset.event) || null;
    this.ovShape = '';
    // "View more": the detailed graphs are collapsed under the overview until asked for.
    var more = root.querySelector('[data-r="view-more"]');
    if (more) more.addEventListener('click', function () { self.toggleDetails(); });
    root.addEventListener('click', function (e) {             // "Details" on an overview row
      var go = e.target.closest && e.target.closest('[data-goto]');
      if (!go) return;
      self.toggleDetails(true);
      var card = self.root.querySelector('[id="res-pos-' + go.dataset.goto + '"]') || document.getElementById('res-pos-' + go.dataset.goto);
      if (card) setTimeout(function () { card.scrollIntoView({ behavior: REDUCED ? 'auto' : 'smooth', block: 'start' }); }, 120);
    });
    this.shape = '';          // which positions/candidates are currently drawn
    this.charts = [];
    this.workerShape = '';
    this.failures = 0;
    this.timer = null;
    this.last = null;
    boards.push(this);
  }

  Board.prototype.el = function (name) { return this.root.querySelector('[data-r="' + name + '"]'); };

  Board.prototype.toggleDetails = function (open) {
    var root = this.root, btn = this.el('view-more'), self = this;
    if (open === undefined) open = !root.classList.contains('details-open');
    root.classList.toggle('details-open', open);
    if (btn) {
      btn.setAttribute('aria-expanded', String(open));
      btn.querySelector('[data-label]').textContent = open ? 'Show less' : 'View more: detailed graphs';
    }
    this.el('details').setAttribute('aria-hidden', String(!open));
    if (open) { self.relayout(); setTimeout(function () { self.relayout(); }, 450); }
  };

  /* ---- OVERVIEW: a bar graph per position, all positions visible at once ----
   * One column per candidate: the bar grows up with the votes and the candidate's photo rides
   * on top of it; name (and party) under the bar. Columns are sorted by votes, so the leader is
   * always first; the top N (seats) get a crown: "Leading" while voting is open, "Winner" at the end.
   * Bar heights are relative to the leader (the leader's bar is full height); the labels give
   * the exact votes and % of the position's votes.
   * Narrow screens / many candidates: the same bars lie down (one row per candidate).
   * Built once, then only numbers, heights and order change (smooth; nothing moves with reduce motion). */
  var OV_MIN_COL = 84;                     // narrower than this per candidate -> horizontal rows
  function chipPhoto(c) {
    return c.photo
      ? '<img class="avatar ovc-photo" src="' + esc(c.photo) + '" alt="" width="56" height="56" loading="lazy" decoding="async">'
      : '<span class="avatar ovc-photo tone-' + BotoKo.tone(c.party || c.name) + '" aria-hidden="true">' + esc(BotoKo.initials(c.name)) + '</span>';
  }
  Board.prototype.fitOverview = function () {
    var box = this.el('ov-rows');
    if (!box) return;
    box.querySelectorAll('.ovg').forEach(function (g) {
      var w = g.getBoundingClientRect().width;
      if (!w) return;                                  // hidden (admin tab not open yet)
      g.classList.toggle('is-rows', w / g.children.length < OV_MIN_COL);
    });
  };
  Board.prototype.renderOverview = function (d) {
    var box = this.el('ov-rows');
    if (!box) return;
    var self = this;
    if (this.ovShape !== this.shape) {
      this.ovShape = this.shape;
      this.ovDrawn = false;
      box.innerHTML = d.positions.length ? d.positions.map(function (p) {
        return '<div class="ov-row" data-pos="' + p.id + '">' +
          '<div class="ov-pos"><span class="ov-name">' + esc(p.name) + '</span>' +
          '<span class="ov-seats">' + (p.seats > 1 ? 'Top ' + p.seats + ' win' : '1 winner') + '</span>' +
          '<span class="ov-total" data-ov="total"></span>' +
          '<button type="button" class="ov-more" data-goto="' + p.id + '">Details ›</button></div>' +
          '<ol class="ovg" data-ov="cands" aria-label="' + esc(p.name) + ', most votes first">' + p.candidates.map(function (c, i) {
            return '<li class="ovc ov-cand" data-c="' + c.id + '" style="--c:' + color(i) + '">' +
              '<span class="ovc-track" aria-hidden="true"><span class="ovc-fill"></span></span>' +
              '<span class="ovc-num"><b class="tnum" data-ov="votes">0</b><small class="tnum" data-ov="pct">0%</small></span>' +
              '<span class="ovc-av">' + chipPhoto(c) + '<span class="ovc-crown" aria-hidden="true">' + BotoKo.icon('crown') + '</span></span>' +
              '<span class="ovc-label"><span class="ovc-name">' + esc(c.name) + '</span>' +
              (c.party ? '<span class="ovc-party">' + esc(c.party) + '</span>' : '') +
              '<span class="ov-badge" data-ov="badge" hidden></span>' +
              '<span class="sr-only" data-ov="sr"></span></span></li>';
          }).join('') + '</ol></div>';
      }).join('') : '<div class="empty">' + BotoKo.icon('inbox') + 'No candidates on the ballot yet.</div>';
      this.fitOverview();
    }
    d.positions.forEach(function (p) {
      var row = box.querySelector('[data-pos="' + p.id + '"]');
      if (!row) return;
      var total = p.total;
      row.querySelector('[data-ov="total"]').textContent = total + (total === 1 ? ' vote' : ' votes') +
        (p.abstain ? ' · ' + p.abstain + ' no vote' : '');
      var list = row.querySelector('[data-ov="cands"]');
      // most votes first; ties keep ballot order
      var ballotOrder = p.candidates.map(function (c) { return c.id; });
      var ordered = p.candidates.slice().sort(function (a, b) {
        return b.votes - a.votes || ballotOrder.indexOf(a.id) - ballotOrder.indexOf(b.id);
      });
      var max = ordered.length ? ordered[0].votes : 0;
      // FLIP: remember where each column is, re-order, then slide them from the old spot
      var before = {};
      var animate = self.ovDrawn && !REDUCED && list.getBoundingClientRect().width;
      if (animate) Array.prototype.forEach.call(list.children, function (li) { before[li.dataset.c] = li.getBoundingClientRect(); });
      ordered.forEach(function (c, rank) {
        var pct = total ? Math.round(100 * c.votes / total) : 0;
        var li = list.querySelector('[data-c="' + c.id + '"]');
        li.style.setProperty('--f', max ? (c.votes / max).toFixed(4) : '0');
        var vEl = li.querySelector('[data-ov="votes"]');
        if (self.ovDrawn && Number(vEl.textContent) !== c.votes) self.setNum(vEl, c.votes);
        else vEl.textContent = c.votes;
        li.querySelector('[data-ov="pct"]').textContent = pct + '%';
        var badge = li.querySelector('[data-ov="badge"]');
        var was = li.dataset.lead || '';
        li.dataset.lead = c.lead || '';
        li.classList.toggle('is-lead', c.lead === 'win');
        li.classList.toggle('is-tie', c.lead === 'tie');
        li.classList.toggle('is-zero', !c.votes);
        badge.hidden = !c.lead;
        badge.innerHTML = c.lead === 'win' ? BotoKo.icon('crown') + (d.final ? 'Winner' : 'Leading')
          : c.lead === 'tie' ? (d.final ? 'Tie for last seat' : 'Tied') : '';
        if (c.lead === 'win' && was !== 'win' && self.ovDrawn) { flash(badge, 'is-pop'); flash(li.querySelector('.ovc-crown'), 'is-pop'); }
        li.querySelector('[data-ov="sr"]').textContent = ': ' + c.votes + (c.votes === 1 ? ' vote' : ' votes') + ', ' + pct + '%' +
          (rank === 0 && c.votes ? ', most votes' : '');
        list.appendChild(li);                          // keep the columns sorted by votes
      });
      if (animate) Array.prototype.forEach.call(list.children, function (li) {
        var a = before[li.dataset.c], b = li.getBoundingClientRect();
        if (!a || (Math.abs(a.left - b.left) < 1 && Math.abs(a.top - b.top) < 1) || !li.animate) return;
        li.animate([{ transform: 'translate(' + (a.left - b.left) + 'px,' + (a.top - b.top) + 'px)' }, { transform: 'none' }],
          { duration: 550, easing: 'cubic-bezier(.2, .8, .2, 1)' });
      });
    });
    this.ovDrawn = true;
  };

  // Restart a short CSS animation class (works on SVG and HTML elements).
  function flash(el, cls) {
    if (!el || REDUCED) return;
    el.classList.remove(cls);
    void el.getBoundingClientRect();
    el.classList.add(cls);
  }

  // Set text; numbers count up (motion.js), and briefly "bump" if it changed (not on the first draw).
  Board.prototype.setNum = function (el, text) {
    text = String(text);
    if (!el || el.textContent === text || el._countTo === parseFloat(text)) return;
    var first = el.textContent === '–' || el.textContent === '';
    var m = text.match(/^(\d+)(%?)$/);
    if (m && !first && BotoKo.setCount) BotoKo.setCount(el, Number(m[1]), m[2]);
    else el.textContent = text;
    if (!first) {
      el.classList.remove('bump');
      void el.offsetWidth;          // restart the CSS animation
      el.classList.add('bump');
    }
  };

  Board.prototype.render = function (d) {
    var self = this;
    // A LIVE page whose results just became the official final results (voting ended + published, or
    // auto-publish): reload once, so the server shows the winners announcement at the top.
    if (this.firstFinal === undefined) this.firstFinal = !!d.final;
    else if (d.final && !this.firstFinal && !this.root.hasAttribute('data-manual')) { this.stopped = true; location.reload(); return; }
    this.last = d;
    this.root.classList.add('is-ready');              // hides the loading skeleton
    this.el('title').textContent = d.title;
    this.setNum(this.el('ballots'), d.ballots);
    this.setNum(this.el('registered'), d.registered);
    var turnout = d.registered ? Math.round(100 * d.ballots / d.registered) : 0;
    this.setNum(this.el('turnout'), turnout + '%');
    this.el('turnout-bar').style.transform = 'scaleX(' + Math.min(1, turnout / 100) + ')';
    var status = this.el('status');
    status.textContent = d.is_open ? 'Open' : 'Closed';
    status.className = 'stat-value ' + (d.is_open ? 'ok' : 'bad');
    this.el('updated').textContent = d.generated_at;

    /* ---- positions: rebuild only when the ballot itself changed ---- */
    var box = this.el('positions');
    var shape = JSON.stringify(d.positions.map(function (p) {
      return [p.id, p.name, p.candidates.map(function (c) { return [c.id, c.name, c.party, c.photo]; })];
    }));
    var fresh = shape !== this.shape;
    if (fresh) {
      this.shape = shape;
      box.innerHTML = '';
      this.charts = d.positions.map(function (p) {
        var chart = new PositionChart(p);
        chart.card.id = 'res-pos-' + p.id;
        box.appendChild(chart.card);
        return chart;
      });
      if (!d.positions.length) {
        box.innerHTML = '<div class="card empty">' + BotoKo.icon('inbox') + 'No candidates on the ballot yet.</div>';
      }
      if (BotoKo.revealResults) BotoKo.revealResults();   // cards fade up as they scroll into view
    }
    d.positions.forEach(function (p, i) { p.final = d.final; self.charts[i].update(p, false); });
    this.root.classList.toggle('is-final', !!d.final);
    this.renderOverview(d);

    /* ---- parallel processing: ADMIN Results tab only (the card isn't on student pages; the numbers
     *      come from the admin-only rpc("get_worker_stats"), never from get_results) ---- */
    var wbox = this.el('workers');
    if (!wbox || !this.workers) return;
    var workers = this.workers;
    var threads = workers.reduce(function (n, w) { return n + w.threads; }, 0);
    this.el('parallel-summary').innerHTML = workers.length ?
      '<span class="badge">' + workers.length + (workers.length === 1 ? ' worker process' : ' worker processes') + '</span>' +
      '<span class="badge">' + threads + (threads === 1 ? ' thread' : ' threads') + ' used</span>' : '';
    var wshape = workers.map(function (w) { return w.worker_pid; }).join(',');
    if (wshape !== this.workerShape) {
      this.workerShape = wshape;
      wbox.innerHTML = workers.length ? workers.map(function (w, i) {
        return '<div class="worker" data-wpid="' + w.worker_pid + '">' +
          '<div class="worker-top">' + BotoKo.icon('cpu') + '<span class="worker-name">Worker ' + (i + 1) + '</span>' +
          '<span class="worker-pid">PID ' + w.worker_pid + '</span></div>' +
          '<div class="worker-nums"><div><b data-wb>0</b><span>ballots</span></div>' +
          '<div><b data-wt>0</b><span>threads</span></div><div><b data-ws>0%</b><span>share</span></div></div>' +
          '<div class="bar" aria-hidden="true"><span></span></div>' +
          '<div class="threads" data-dots aria-hidden="true"></div></div>';
      }).join('') : '<div class="empty">' + BotoKo.icon('cpu') +
        'No ballots yet. When students vote, you will see which worker process and thread handled each ballot.</div>';
    }
    workers.forEach(function (w) {
      var card = wbox.querySelector('[data-wpid="' + w.worker_pid + '"]');
      var share = d.ballots ? w.ballots / d.ballots : 0;
      self.setNum(card.querySelector('[data-wb]'), w.ballots);
      self.setNum(card.querySelector('[data-wt]'), w.threads);
      card.querySelector('[data-ws]').textContent = Math.round(share * 100) + '%';
      card.querySelector('.bar > span').style.transform = 'scaleX(' + share + ')';
      var dots = card.querySelector('[data-dots]');
      var n = Math.min(w.threads, 64);
      if (dots.children.length !== n) dots.innerHTML = new Array(n + 1).join('<i></i>');
    });
  };

  // Window resized / projector mode toggled: re-measure the charts (no animation).
  Board.prototype.relayout = function () {
    this.charts.forEach(function (c) { c.layout(); });
    this.fitOverview();
  };

  Board.prototype.setOnline = function (ok) {
    var live = this.el('live');
    live.classList.toggle('is-offline', !ok);
    this.el('live-text').textContent = ok ? 'Live' : 'Reconnecting...';
  };

  Board.prototype.tick = function () {
    var self = this;
    clearTimeout(this.timer);
    if (this.stopped || document.hidden) return;     // resumes on start() / visibilitychange
    // Remote procedure call: rpc("get_results") (static/rpc.js). Plain /api/results only as a fallback.
    var load = window.rpc ? window.rpc('get_results', this.event ? { event_id: this.event } : {})
      : fetch(this.src, { cache: 'no-store', credentials: 'same-origin' })
          .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
    // Admin Results tab only: worker processes / threads (admin-only procedure)
    var params = this.event ? { event_id: this.event } : {};
    var loadWorkers = !this.el('workers') ? Promise.resolve(null)
      : (window.rpc ? window.rpc('get_worker_stats', params)
                    : fetch('/admin/api/workers' + (this.event ? '?event=' + this.event : ''), { cache: 'no-store', credentials: 'same-origin' })
                        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }))
          .catch(function (err) { if (window.console) console.warn('workers:', err); return null; });
    Promise.all([load, loadWorkers])
      .then(function (r) {
        if (r[1]) self.workers = r[1].processed_by;
        self.failures = 0; self.setOnline(true); self.render(r[0]);
      })
      .catch(function (err) {
        // Voting ended and the results are NOT published (yet): show the "announced soon" page instead of the last
        // live numbers (the server sends that page now). Student / projector pages only, never the admin's.
        if (err && err.code === -32021 && !self.root.hasAttribute('data-manual')) { self.stopped = true; location.reload(); return; }
        if (window.console) console.warn('results:', err); self.failures++; self.setOnline(false);
      })
      .then(function () {
        // 2 s normally; back off up to 10 s while the server can't be reached.
        self.timer = setTimeout(function () { self.tick(); }, Math.min(10000, INTERVAL * (1 + self.failures)));
      });
  };

  Board.prototype.start = function () {
    var self = this;
    this.stopped = false;
    if (!this.listening) {
      this.listening = true;
      document.addEventListener('visibilitychange', function () { if (!document.hidden) self.tick(); });
    }
    this.relayout();          // e.g. admin tab just became visible
    this.tick();
  };

  Board.prototype.stop = function () {
    this.stopped = true;
    clearTimeout(this.timer);
  };

  window.BotoKoResults = { Board: Board };

  var resizeTimer = 0;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { boards.forEach(function (b) { b.relayout(); }); }, 150);
  });

  /* ---- projector mode (bigger text, no navigation) ---- */
  function setProjector(on) {
    document.documentElement.classList.toggle('projector', on);
    try { localStorage.setItem('botoko-projector', on ? '1' : '0'); } catch (e) { /* private mode */ }
    document.querySelectorAll('[data-projector]').forEach(function (b) { b.setAttribute('aria-pressed', String(on)); });
    var fs = document.fullscreenElement;
    if (on && !fs && document.documentElement.requestFullscreen) document.documentElement.requestFullscreen().catch(function () {});
    if (!on && fs && document.exitFullscreen) document.exitFullscreen().catch(function () {});
    boards.forEach(function (b) { b.relayout(); });       // text size changed
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-results]').forEach(function (root) {
      if (!root.hasAttribute('data-manual')) new Board(root).start();
    });
    document.querySelectorAll('[data-projector], [data-projector-exit]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        setProjector(!document.documentElement.classList.contains('projector'));
      });
    });
    if (document.querySelector('[data-projector]')) {
      document.addEventListener('keydown', function (e) {
        if (e.target.closest && e.target.closest('input, textarea, select')) return;
        if (e.key === 'p' || e.key === 'P') setProjector(!document.documentElement.classList.contains('projector'));
        if (e.key === 'Escape' && document.documentElement.classList.contains('projector')) setProjector(false);
      });
    }
  });
})();
