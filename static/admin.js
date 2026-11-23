/* Admin (COMELEC) page: tabs, auto-refreshing overview, student search, reset safety.
 * Tabs use the URL hash (#overview, #ballot, ...) so the server can send you back to
 * the right tab after a form is submitted, e.g. redirect to /admin#ballot.
 */
(function () {
  'use strict';
  var OVERVIEW_EVERY = 3000;
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.tabs [role=tab]'));
  var resultsRoot = document.querySelector('#panel-results [data-results]');
  var board = resultsRoot ? new BotoKoResults.Board(resultsRoot) : null;
  var overview = document.getElementById('panel-overview');
  var overviewTimer = null;

  /* ---------- tabs ---------- */
  function show(name) {
    var found = tabs.some(function (t) { return t.hash === '#' + name; });
    if (!found) name = 'overview';
    tabs.forEach(function (t) {
      var on = t.hash === '#' + name;
      t.setAttribute('aria-selected', String(on));
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute('aria-controls')).classList.toggle('is-active', on);
    });
    if (board) { if (name === 'results') board.start(); else board.stop(); }
    if (name === 'overview') refreshOverview();
    else clearTimeout(overviewTimer);
  }
  window.addEventListener('hashchange', function () {
    show(location.hash.slice(1));
    // e.g. "Back to Overview" at the bottom of a long tab: bring the tab bar back into view.
    document.querySelector('.tabs').scrollIntoView({ block: 'nearest' });
  });
  // Arrow keys move between tabs (standard keyboard pattern for tab lists).
  document.querySelector('.tabs').addEventListener('keydown', function (e) {
    var i = tabs.indexOf(document.activeElement);
    var fwd = e.key === 'ArrowRight' || e.key === 'ArrowDown', back = e.key === 'ArrowLeft' || e.key === 'ArrowUp';
    if (i < 0 || (!fwd && !back)) return;                  // the sidebar tabs are vertical: Up/Down too
    var next = tabs[(i + (fwd ? 1 : tabs.length - 1)) % tabs.length];
    next.focus();
    next.click();
    e.preventDefault();
  });
  // Links like <a href="#settings"> inside the page also switch tabs (handled by hashchange).

  /* ---------- overview auto-refresh ---------- */
  function o(name) { return overview.querySelector('[data-o="' + name + '"]'); }
  var esc = BotoKo.esc;

  function renderOverview(d) {
    var count = BotoKo.setCount || function (el, v, sfx) { el.textContent = v + (sfx || ''); };
    count(o('ballots'), d.ballots);                  // numbers count up (motion.js)
    count(o('registered'), d.registered);
    var turnout = d.registered ? Math.round(100 * d.ballots / d.registered) : 0;
    count(o('turnout'), turnout, '%');
    o('turnout-bar').style.transform = 'scaleX(' + Math.min(1, turnout / 100) + ')';
    count(o('pending'), Math.max(0, d.registered - d.ballots));
    o('updated').textContent = d.generated_at;

    var allOk = d.checks.every(function (c) { return c.ok; });
    o('checks-badge').textContent = allOk ? 'All passed' : 'Problem found';
    o('checks-badge').className = 'badge ' + (allOk ? 'badge-ok' : 'badge-bad');
    o('checks').innerHTML = d.checks.map(function (c) {
      return '<li class="' + (c.ok ? 'ok' : 'bad') + '"><span class="check-ico">' + BotoKo.icon(c.ok ? 'check' : 'x') + '</span>' +
        '<span><span class="check-label">' + esc(c.label) + '</span><span class="check-detail">' + esc(c.detail) + '</span></span></li>';
    }).join('');

    o('recent').innerHTML = d.recent.length ? d.recent.map(function (r) {
      return '<tr><td class="tnum">' + esc(r.time) + '</td><td class="mono">' + esc(r.student_no) + '</td>' +
        '<td class="mono">' + r.worker_pid + '</td><td class="mono">' + esc(r.thread) + '</td></tr>';
    }).join('') : '<tr><td colspan="4" class="empty">No votes yet.</td></tr>';
  }

  function setLive(ok, text) {
    o('live').classList.toggle('is-offline', !ok);
    o('live-text').textContent = text;
  }

  function refreshOverview() {
    clearTimeout(overviewTimer);
    if (document.hidden || !overview.classList.contains('is-active')) return;
    fetch(overview.dataset.overview, { cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) {
        if (r.status === 401) { setLive(false, 'Logged out'); throw new Error('auth'); }
        if (!r.ok) throw new Error(r.status);
        return r.json();
      })
      .then(function (d) {
        renderOverview(d);
        setLive(true, 'Auto-refresh');
        overviewTimer = setTimeout(refreshOverview, OVERVIEW_EVERY);
      })
      .catch(function (err) {
        if (err.message === 'auth') { BotoKo.toast('Your admin session ended. Please log in again.', 'error'); return; }
        setLive(false, 'Reconnecting...');
        overviewTimer = setTimeout(refreshOverview, OVERVIEW_EVERY * 2);
      });
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refreshOverview(); });

  /* ---------- Open / Close voting: remote procedure calls (JSON-RPC, static/rpc.js) ----------
   * <form data-rpc="open_voting|close_voting">. app.js first shows the confirm dialog; once the
   * admin confirms, we call the procedure instead of posting the form, then reload the page. */
  var TOAST_KEY = 'botoko-admin-toast';
  document.querySelectorAll('form[data-rpc]').forEach(function (form) {
    form.addEventListener('submit', function (e) {
      if (!window.rpc) return;                                       // plain form post
      if (form.dataset.confirm && !form.dataset.confirmed) return;  // app.js asks first
      e.preventDefault();
      if (form.dataset.sending) return;                              // second tap: ignore
      form.dataset.sending = '1';
      var btn = form.querySelector('[type=submit]'), label = btn.querySelector('.toggle-label');
      var idle = label ? label.textContent : '';
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
      form.classList.add('is-switching');                            // the switch slides over (CSS)
      if (label) label.textContent = form.dataset.rpc === 'close_voting' ? 'Ending voting...' : 'Opening...';
      var minWait = new Promise(function (res) { setTimeout(res, 380); });   // let the slide finish
      Promise.all([rpc(form.dataset.rpc, { event_id: Number(form.dataset.event) }), minWait]).then(function (both) {
        var r = both[0];
        try {
          sessionStorage.setItem(TOAST_KEY, r.is_open
            ? 'Voting is now OPEN. Students can submit their ballots.'
            : 'Voting has ENDED. No more ballots will be accepted.');
        } catch (err) { /* private mode: no toast after reload, that's fine */ }
        location.reload();                      // same tab (#hash) with the new state
      }, function (err) {
        if (err.code === rpc.codes.ADMIN_ONLY) { location.href = '/login'; return; }
        delete form.dataset.sending;
        form.classList.remove('is-switching');
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
        if (label) label.textContent = idle;
        BotoKo.toast(err.message, 'error');
      });
    });
  });
  try {
    var pending = sessionStorage.getItem(TOAST_KEY);
    if (pending) { sessionStorage.removeItem(TOAST_KEY); BotoKo.toast(pending, 'ok'); }
  } catch (err) { /* storage blocked */ }

  /* ---------- students: search + filter ---------- */
  var search = document.getElementById('student-search');
  var rows = Array.prototype.slice.call(document.querySelectorAll('#student-rows tr'));
  var filterBtns = Array.prototype.slice.call(document.querySelectorAll('[data-filter]'));
  var filter = 'all';
  var selects = Array.prototype.slice.call(document.querySelectorAll('[data-select-filter]'));
  function applyFilter() {
    var q = search.value.trim().toLowerCase();
    var shown = 0;
    rows.forEach(function (tr) {
      var byStatus = filter === 'all' ||
        (filter === 'reg' ? tr.dataset.reg === '1' : filter === 'unreg' ? tr.dataset.reg === '0'
          : tr.dataset.reg === '1' && tr.dataset.voted === filter);
      var ok = byStatus && (!q || tr.dataset.q.indexOf(q) !== -1);
      // Course / department / year / section dropdowns (empty = all)
      selects.forEach(function (s) { if (ok && s.value && tr.dataset[s.dataset.selectFilter] !== s.value) ok = false; });
      tr.hidden = !ok;
      if (ok) shown++;
    });
    document.getElementById('student-count').textContent = 'Showing ' + shown + ' of ' + rows.length;
    document.getElementById('student-empty').hidden = shown !== 0;
    countChecked();
  }

  /* ---------- register / unregister: checkboxes, "select all shown", count ---------- */
  var checkAll = document.getElementById('check-all');
  function boxes() { return Array.prototype.slice.call(document.querySelectorAll('#student-rows input[name=student_id]')); }
  function countChecked() {
    var out = document.getElementById('checked-count');
    if (!out) return;
    var n = boxes().filter(function (b) { return b.checked; }).length;
    out.textContent = n + ' selected';
    document.querySelectorAll('#register-form .register-bar button').forEach(function (b) { b.disabled = n === 0; });
  }
  if (checkAll) {
    checkAll.addEventListener('change', function () {
      boxes().forEach(function (b) { if (!b.closest('tr').hidden) b.checked = checkAll.checked; });
      countChecked();
    });
    document.getElementById('student-rows').addEventListener('change', countChecked);
    countChecked();
  }

  /* ---------- event switcher: changing the event opens it ---------- */
  document.querySelectorAll('[data-autosubmit]').forEach(function (sel) {
    sel.addEventListener('change', function () { sel.form.submit(); });
  });
  if (search) {
    search.addEventListener('input', applyFilter);
    selects.forEach(function (s) { s.addEventListener('change', applyFilter); });
    filterBtns.forEach(function (b) {
      b.addEventListener('click', function () {
        filter = b.dataset.filter;
        filterBtns.forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
        applyFilter();
      });
    });
  }

  /* ---------- turnout by group: Course / Department / Year-Section switch ---------- */
  var turnoutBtns = Array.prototype.slice.call(document.querySelectorAll('[data-turnout]'));
  turnoutBtns.forEach(function (b) {
    b.addEventListener('click', function () {
      turnoutBtns.forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
      document.querySelectorAll('[data-turnout-panel]').forEach(function (p) {
        p.removeAttribute('data-start-hidden');
        p.hidden = p.dataset.turnoutPanel !== b.dataset.turnout;
      });
    });
  });

  /* ---------- reset: button only works after typing RESET ---------- */
  var resetInput = document.getElementById('reset-confirm');
  var resetBtn = document.getElementById('reset-btn');
  if (resetInput) {
    var gate = function () { resetBtn.disabled = resetInput.value.trim() !== 'RESET'; };
    resetInput.addEventListener('input', gate);
    gate();
  }

  /* ---------- candidate photos ----------
   * The photo is shrunk IN THE BROWSER before upload: centre-cropped to a square,
   * resized to 400x400 and saved as JPEG (quality 0.8), so it is only ~30-60 KB.
   * The server checks the result again (type + size) before storing it in the database. */
  var PHOTO_SIZE = 400, PHOTO_MAX = 300 * 1024, PHOTO_TYPES = ['image/jpeg', 'image/png', 'image/webp'];

  function preparePhoto(file) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error('unreadable')); };
      img.onload = function () {
        URL.revokeObjectURL(url);
        var w = img.naturalWidth, h = img.naturalHeight, side = Math.min(w, h);
        if (!side) { reject(new Error('unreadable')); return; }
        // Centre square of the photo. Big photos are halved step by step first, which
        // looks much sharper than shrinking 4000px -> 400px in one go.
        var src = img, sx = (w - side) / 2, sy = (h - side) / 2, sSide = side;
        while (sSide / 2 >= PHOTO_SIZE * 1.5) {
          var half = document.createElement('canvas');
          half.width = half.height = Math.round(sSide / 2);
          var hctx = half.getContext('2d');
          hctx.imageSmoothingQuality = 'high';
          hctx.drawImage(src, sx, sy, sSide, sSide, 0, 0, half.width, half.height);
          src = half; sx = 0; sy = 0; sSide = half.width;
        }
        var canvas = document.createElement('canvas');
        canvas.width = canvas.height = PHOTO_SIZE;
        var ctx = canvas.getContext('2d');
        ctx.fillStyle = '#ffffff';                      // transparent PNGs get a white background
        ctx.fillRect(0, 0, PHOTO_SIZE, PHOTO_SIZE);
        ctx.imageSmoothingQuality = 'high';
        ctx.drawImage(src, sx, sy, sSide, sSide, 0, 0, PHOTO_SIZE, PHOTO_SIZE);
        var quality = 0.8;
        (function encode() {
          canvas.toBlob(function (blob) {
            if (!blob) { reject(new Error('encode')); return; }
            if (blob.size > PHOTO_MAX && quality > 0.45) { quality -= 0.1; encode(); return; }
            resolve(blob);
          }, 'image/jpeg', quality);
        })();
      };
      img.src = url;
    });
  }
  window.BotoKoPhoto = { prepare: preparePhoto };        // also used by the automated test

  document.querySelectorAll('[data-photo-input]').forEach(function (input) {
    var form = input.form;
    var field = input.closest('.photo-field');
    var preview = field.querySelector('[data-photo-preview]');
    var status = field.querySelector('[data-photo-status]');
    var setStatus = function (text, kind) {
      status.textContent = text;
      status.className = 'hint' + (kind ? ' is-' + kind : '');
    };
    input.addEventListener('change', function () {
      var file = input.files[0];
      form._photoBlob = null;
      if (!file) return;
      if (PHOTO_TYPES.indexOf(file.type) < 0) {
        input.value = '';
        setStatus('That file is not a JPG, PNG or WebP photo. Please choose another file.', 'error');
        return;
      }
      form._photoBusy = true;
      setStatus('Preparing photo...');
      preparePhoto(file).then(function (blob) {
        form._photoBlob = blob;
        preview.innerHTML = '';
        var img = document.createElement('img');
        img.className = 'avatar';
        img.alt = 'Preview of the new photo';
        img.width = img.height = 64;
        img.src = URL.createObjectURL(blob);
        preview.appendChild(img);
        preview.classList.add('has-photo');
        setStatus('Ready: 400×400, ' + Math.round(blob.size / 1024) + ' KB. Press Save to upload it.', 'ok');
      }, function () {
        input.value = '';
        setStatus('This file could not be read as a photo. Please choose another file.', 'error');
      }).then(function () { form._photoBusy = false; });
    });
  });

  // Forms with a prepared photo are sent with fetch(), so the small JPEG is uploaded
  // instead of the original file. Everything else (CSRF, name, party) is sent as usual.
  document.querySelectorAll('[data-photo-form]').forEach(function (form) {
    form.addEventListener('submit', function (e) {
      if (form._photoBusy) {
        e.preventDefault();
        e.stopImmediatePropagation();
        BotoKo.toast('Please wait a moment: the photo is still being prepared.', 'info');
        return;
      }
      if (!form._photoBlob) return;                   // no new photo: normal form submit
      e.preventDefault();                             // app.js still shows the busy spinner
      var data = new FormData(form);
      data.set('photo', form._photoBlob, 'photo.jpg');
      fetch(form.action, { method: 'POST', body: data, credentials: 'same-origin', redirect: 'manual' })
        .then(function (r) {
          // Success = the server redirects back to the admin page. Reload it there so the
          // message from the server (and the new photo) show up.
          if (r.type === 'opaqueredirect' || r.status === 302 || r.redirected) {
            history.replaceState(null, '', location.pathname + '#ballot');
            location.reload();
            return;
          }
          var err = new Error(r.status === 400 ? 'Your session expired. Reload the page and try again.'
                                               : 'Upload failed (error ' + r.status + '). Please try again.');
          err.fromServer = true;
          throw err;
        })
        .catch(function (err) {
          delete form.dataset.sending;
          BotoKo.clearBusy(form.querySelector('[type=submit]'));
          BotoKo.toast(err.fromServer ? err.message : 'Upload failed: check your connection and try again.', 'error');
        });
    });
  });

  /* ---------- accounts: verify each student once (filter, select, approve / reject, live badge) ---------- */
  var accUpdate = null;                         // called by the registrations poll with the account counts
  var accPanel = document.getElementById('panel-accounts');
  if (accPanel) {
    var accRowsNow = function () { return Array.prototype.slice.call(document.querySelectorAll('#acc-rows tr')); };
    var accSearch = document.getElementById('acc-search');
    var accBtns = Array.prototype.slice.call(accPanel.querySelectorAll('[data-acc-filter]'));
    var accFilter = (accBtns.filter(function (b) { return b.getAttribute('aria-pressed') === 'true'; })[0] || accBtns[0]).dataset.accFilter;
    var accEl = function (name) { return accPanel.querySelector('[data-a="' + name + '"]'); };
    var accBoxes = function () { return Array.prototype.slice.call(document.querySelectorAll('#acc-rows input[name=acc_id]')); };
    var accCount = function () {
      var n = accBoxes().filter(function (b) { return b.checked; }).length;
      document.getElementById('acc-checked').textContent = n + ' selected';
      accPanel.querySelectorAll('[data-acc-needs-selection]').forEach(function (b) { b.disabled = n === 0; });
    };
    var accApply = function () {
      var q = accSearch.value.trim().toLowerCase(), shown = 0, accRows = accRowsNow();
      accRows.forEach(function (tr) {
        var ok = (accFilter === 'all' || tr.dataset.status === accFilter) && (!q || tr.dataset.q.indexOf(q) !== -1);
        tr.hidden = !ok;
        if (!ok) { var box = tr.querySelector('input[name=acc_id]'); if (box) box.checked = false; }
        if (ok) shown++;
      });
      document.getElementById('acc-count').textContent = 'Showing ' + shown + ' of ' + accRows.length;
      document.getElementById('acc-empty').hidden = shown !== 0;
      if (accRows.length) accEl('empty-text').textContent = accFilter === 'pending' && !q ? 'No accounts waiting. All caught up!' : 'No accounts match.';
      accCount();
    };
    accSearch.addEventListener('input', accApply);
    accBtns.forEach(function (b) {
      b.addEventListener('click', function () {
        accFilter = b.dataset.accFilter;
        accBtns.forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
        accApply();
      });
    });
    document.getElementById('acc-check-all').addEventListener('change', function (e) {
      accBoxes().forEach(function (b) { if (!b.closest('tr').hidden) b.checked = e.target.checked; });
      accCount();
    });
    document.getElementById('acc-rows').addEventListener('change', accCount);

    var accAll = document.getElementById('acc-approve-all');
    accAll.addEventListener('click', function (e) {
      if (accAll.dataset.ok) return;
      e.preventDefault();
      BotoKo.confirm({ title: accAll.dataset.confirmClick, text: accAll.dataset.confirmText, ok: 'Approve all' }, function () {
        accAll.dataset.ok = '1';
        accAll.click();
      });
    });

    var accModal = document.getElementById('acc-reject-modal');
    var accReject = function (ids, who) {
      accEl('reject-ids').innerHTML = ids.map(function (id) {
        return '<input type="hidden" name="acc_id" value="' + Number(id) + '">';
      }).join('');
      accEl('reject-who').textContent = who;
      BotoKo.openModal(accModal);
    };
    accPanel.addEventListener('click', function (e) {
      var one = e.target.closest('[data-acc-reject]');
      if (one) { accReject([one.dataset.accReject], one.dataset.name); return; }
      if (e.target.closest('[data-acc-reject-selected]')) {
        var ids = accBoxes().filter(function (b) { return b.checked; }).map(function (b) { return b.value; });
        if (ids.length) accReject(ids, ids.length + ' selected account' + (ids.length === 1 ? '' : 's'));
        return;
      }
      var chip = e.target.closest('[data-acc-reason]');
      if (chip) {
        var box = document.getElementById('acc-reject-reason');
        box.value = chip.dataset.accReason;
        box.focus();
      }
    });
    accEl('reload').addEventListener('click', function (e) { e.preventDefault(); location.hash = 'accounts'; location.reload(); });

    var accKnown = Number(accPanel.dataset.accLatest) || 0;
    accUpdate = function (a) {
      if (!a) return;
      var badge = document.querySelector('[data-a="tab-count"]');
      if (badge && badge.textContent !== String(a.pending)) {
        badge.textContent = a.pending;
        badge.setAttribute('aria-label', a.pending + ' pending');
        badge.classList.remove('is-bump');
        void badge.offsetWidth;
        badge.classList.add('is-bump');
      }
      if (badge) badge.classList.toggle('is-zero', !a.pending);
      ['pending', 'verified', 'rejected'].forEach(function (k) { var el = accEl('count-' + k); if (el) el.textContent = a[k]; });
      accEl('live').classList.remove('is-offline');
      if (a.latest_id > accKnown) {
        accEl('new-text').textContent = 'New accounts are waiting (' + a.pending + ' pending now).';
        document.getElementById('new-accounts').hidden = false;
      }
    };
    accUpdate.sync = function (d) {
      var n = syncRows(document.getElementById('acc-rows'), d.acc_rows, d.acc_status,
                       document.getElementById('acc-form') || accPanel.querySelector('form'), accModal, accEl('reject-ids'));
      if (n.changed) accApply();
      if (n.added) BotoKo.toast(n.added === 1 ? 'New account waiting for approval.' : n.added + ' new accounts waiting for approval.', 'ok');
      accKnown = d.accounts ? d.accounts.latest_id : accKnown;
      document.getElementById('new-accounts').hidden = true;        // the rows are already here: no "Show them"
    };
    accApply();
  }

  /* ---------- live rows (Accounts + Registrations): new pending rows appear by themselves ----------
   * The 4-second poll below also brings the PENDING rows (drawn by the server with the same template as the page)
   * and every row's status. New rows are added at the top (marked "new"); rows that were pending here but were
   * approved / rejected elsewhere (e.g. another admin tab) are removed. Nothing is touched while an approve /
   * reject form is being sent, rows in an open "Reject" dialog stay, existing rows are never redrawn (ticked
   * boxes stay ticked), the filter / search are applied to the new rows, and the scroll position is kept. */
  function syncRows(tbody, items, statusMap, form, modal, rejectIds) {
    var out = { added: 0, removed: 0, changed: false };
    if (!tbody || !items || !statusMap) return out;
    if ((form && form.dataset.sending) || document.querySelector('form[data-sending]')) return out;   // action in progress
    var protect = {};
    if (modal && !modal.hidden && rejectIds) {
      rejectIds.querySelectorAll('input').forEach(function (i) { protect[i.value] = true; });
    }
    var have = {};
    Array.prototype.forEach.call(tbody.rows, function (tr) { have[tr.dataset.id] = tr; });
    // keep what the admin is looking at in the same place: remember the first visible row
    var anchor = null, anchorTop = 0;
    for (var i = 0; i < tbody.rows.length; i++) {
      var r = tbody.rows[i].getBoundingClientRect();
      if (r.height && r.bottom > 0) { anchor = tbody.rows[i]; anchorTop = r.top; break; }
    }
    Object.keys(have).forEach(function (id) {
      var tr = have[id];
      if (tr.dataset.status !== 'pending' || statusMap[id] === 'pending' || protect[id]) return;
      if (tr === anchor) anchor = null;
      tr.remove();
      out.removed++;
    });
    var holder = document.createElement('tbody');
    for (var k = items.length - 1; k >= 0; k--) {                // keep the server's order, newest at the top
      if (have[String(items[k].id)]) continue;
      holder.innerHTML = items[k].html;
      var row = holder.firstElementChild;
      if (!row) continue;
      row.classList.add('is-new');
      tbody.insertBefore(row, tbody.firstChild);
      out.added++;
    }
    out.changed = out.added > 0 || out.removed > 0;
    if (out.changed && anchor && anchorTop < 0) {
      var moved = anchor.getBoundingClientRect().top - anchorTop;
      if (moved) window.scrollBy(0, moved);
    }
    return out;
  }

  /* ---------- registrations: filter, select, approve / reject, live pending count ---------- */
  var regPanel = document.getElementById('panel-registrations');
  if (regPanel) {
    var regRowsNow = function () { return Array.prototype.slice.call(document.querySelectorAll('#reg-rows tr')); };
    var regSearch = document.getElementById('reg-search');
    var regBtns = Array.prototype.slice.call(regPanel.querySelectorAll('[data-reg-filter]'));
    var regFilter = (regBtns.filter(function (b) { return b.getAttribute('aria-pressed') === 'true'; })[0] || regBtns[0]).dataset.regFilter;
    var regForm = document.getElementById('reg-form');
    var regAll = document.getElementById('reg-check-all');
    var regEl = function (name) { return regPanel.querySelector('[data-r="' + name + '"]'); };
    var regBoxes = function () { return Array.prototype.slice.call(document.querySelectorAll('#reg-rows input[name=reg_id]')); };

    var regCount = function () {
      var n = regBoxes().filter(function (b) { return b.checked; }).length;
      document.getElementById('reg-checked').textContent = n + ' selected';
      regPanel.querySelectorAll('[data-needs-selection]').forEach(function (b) { b.disabled = n === 0; });
    };
    var regApply = function () {
      var q = regSearch.value.trim().toLowerCase(), shown = 0, regRows = regRowsNow();
      regRows.forEach(function (tr) {
        var ok = (regFilter === 'all' || tr.dataset.status === regFilter) && (!q || tr.dataset.q.indexOf(q) !== -1);
        tr.hidden = !ok;
        if (!ok) { var box = tr.querySelector('input[name=reg_id]'); if (box) box.checked = false; }
        if (ok) shown++;
      });
      document.getElementById('reg-count').textContent = 'Showing ' + shown + ' of ' + regRows.length;
      var empty = document.getElementById('reg-empty');
      empty.hidden = shown !== 0;
      if (regRows.length) regEl('empty-text').textContent = regFilter === 'pending' && !q ? 'No pending registrations. All caught up!' : 'No registrations match.';
      regCount();
    };
    regSearch.addEventListener('input', regApply);
    regBtns.forEach(function (b) {
      b.addEventListener('click', function () {
        regFilter = b.dataset.regFilter;
        regBtns.forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
        regApply();
      });
    });
    regAll.addEventListener('change', function () {
      regBoxes().forEach(function (b) { if (!b.closest('tr').hidden) b.checked = regAll.checked; });
      regCount();
    });
    document.getElementById('reg-rows').addEventListener('change', regCount);

    // "Approve all pending": ask first.
    var approveAll = document.getElementById('approve-all');
    approveAll.addEventListener('click', function (e) {
      if (approveAll.dataset.ok) return;
      e.preventDefault();
      BotoKo.confirm({ title: approveAll.dataset.confirmClick, text: approveAll.dataset.confirmText, ok: 'Approve all' }, function () {
        approveAll.dataset.ok = '1';
        approveAll.click();
      });
    });

    // Reject (one, or the selected ones): a dialog asks for the reason.
    var rejectModal = document.getElementById('reject-modal');
    var openReject = function (ids, who) {
      regEl('reject-ids').innerHTML = ids.map(function (id) {
        return '<input type="hidden" name="reg_id" value="' + Number(id) + '">';
      }).join('');
      regEl('reject-who').textContent = who;
      BotoKo.openModal(rejectModal);
    };
    regPanel.addEventListener('click', function (e) {
      var one = e.target.closest('[data-reject]');
      if (one) { openReject([one.dataset.reject], one.dataset.name); return; }
      if (e.target.closest('[data-reject-selected]')) {
        var ids = regBoxes().filter(function (b) { return b.checked; }).map(function (b) { return b.value; });
        if (ids.length) openReject(ids, ids.length + ' selected registration' + (ids.length === 1 ? '' : 's'));
        return;
      }
      var chip = e.target.closest('[data-reason]');
      if (chip) {
        var box = document.getElementById('reject-reason');
        box.value = chip.dataset.reason;
        box.focus();
      }
    });

    // Live pending count: the tab badge and the filter counts update every few seconds; when new
    // registrations come in, a banner offers to show them (the page isn't reloaded under the admin's hands).
    var known = Number(regPanel.dataset.latest) || 0;
    var regTimer = null;
    var setCount = function (name, v) { var el = regEl(name); if (el) el.textContent = v; };
    var pollRegs = function () {
      clearTimeout(regTimer);
      if (document.hidden) return;
      fetch(regPanel.dataset.registrations + '&rows=1', { cache: 'no-store', credentials: 'same-origin' })
        .then(function (res) { if (!res.ok) throw new Error(res.status); return res.json(); })
        .then(function (d) {
          var badge = document.querySelector('[data-r="tab-count"]');
          if (badge) {
            if (badge.textContent !== String(d.pending)) {
              badge.textContent = d.pending;
              badge.setAttribute('aria-label', d.pending + ' pending');
              badge.classList.remove('is-bump');
              void badge.offsetWidth;                    // restart the little "bump" animation
              badge.classList.add('is-bump');
            }
            badge.classList.toggle('is-zero', !d.pending);
          }
          setCount('count-pending', d.pending);
          setCount('count-approved', d.approved);
          setCount('count-rejected', d.rejected);
          if (accUpdate) accUpdate(d.accounts);
          if (accUpdate && accUpdate.sync) accUpdate.sync(d);
          var rs = syncRows(document.getElementById('reg-rows'), d.reg_rows, d.reg_status,
                            document.getElementById('reg-form'), rejectModal, regEl('reject-ids'));
          if (rs.changed) regApply();
          if (rs.added) {
            BotoKo.toast(rs.added === 1 ? 'New registration waiting for approval.' : rs.added + ' new registrations waiting for approval.', 'ok');
            known = d.latest_id;
            pollRegs.toasted = true;
          }
          regEl('live').classList.remove('is-offline');
          if (d.reg_rows) { known = Math.max(known, d.latest_id); document.getElementById('new-regs').hidden = true; }
          if (d.latest_id > known) {
            var fresh = regRowsNow().length ? d.latest_id - known : d.pending;
            regEl('new-text').textContent = 'New registrations came in (' + d.pending + ' pending now).';
            document.getElementById('new-regs').hidden = false;
            if (!pollRegs.toasted) { BotoKo.toast('New registration' + (fresh === 1 ? '' : 's') + ' waiting for approval.', 'ok'); pollRegs.toasted = true; }
          }
          regTimer = setTimeout(pollRegs, 4000);
        })
        .catch(function () {
          regEl('live').classList.add('is-offline');
          regTimer = setTimeout(pollRegs, 8000);
        });
    };
    regEl('reload').addEventListener('click', function (e) { e.preventDefault(); location.hash = 'registrations'; location.reload(); });
    document.addEventListener('visibilitychange', function () { if (!document.hidden) pollRegs(); });
    regApply();
    pollRegs();
  }

  show(location.hash.slice(1) || 'overview');

  /* ---------- dashboard: "Results by event" (any event, without reloading the page) ----------
   * The picker loads the event's card from /admin/api/results-card (same templates as the page), then a results
   * board (results.js) keeps it live: only for the chosen event, only while the Dashboard tab is shown and the
   * browser tab is visible. The choice is kept in the address (?results=<id>): refresh and Back keep it. */
  var dash = document.querySelector('[data-dash-results]');
  if (dash && window.BotoKoResults) {
    var dashSel = document.getElementById('dash-results-select');
    var dashSlot = dash.querySelector('.dash-results-slot');
    var dashBoard = null, dashSeq = 0;
    var dashOn = function () { return document.getElementById('panel-overview').classList.contains('is-active'); };
    var dashShown = function () { var b = dashSlot.querySelector('[data-dash-event]'); return b ? b.dataset.dashEvent : ''; };
    var dashStart = function () {
      if (dashBoard) dashBoard.stop();
      var root = dashSlot.querySelector('[data-results]');
      dashBoard = root ? new BotoKoResults.Board(root) : null;     // upcoming event: empty state, no board
      if (dashBoard && dashOn()) dashBoard.start();
    };
    var dashLoad = function (id, push) {
      id = String(id);
      dashSel.value = id;
      if (id === dashShown()) return;
      var seq = ++dashSeq;
      dash.setAttribute('aria-busy', 'true');
      dashSlot.classList.add('is-loading');
      fetch(dash.dataset.dashResults + '?event=' + encodeURIComponent(id), { cache: 'no-store', credentials: 'same-origin' })
        .then(function (r) { if (r.status === 401) { location.href = '/'; throw new Error('logged out'); } if (!r.ok) throw new Error(r.status); return r.text(); })
        .then(function (html) {
          if (seq !== dashSeq) return;                                  // the admin already picked another one
          if (dashBoard) { dashBoard.stop(); dashBoard = null; }
          dashSlot.innerHTML = html;
          dashStart();
          if (push) {
            var u = new URL(location.href);
            u.searchParams.set('results', id);
            history.pushState({ dashResults: id }, '', u.pathname + u.search + location.hash);
          }
        })
        .catch(function () { BotoKo.toast('Could not load the results of that event. Please try again.', 'error'); })
        .then(function () { if (seq === dashSeq) { dash.removeAttribute('aria-busy'); dashSlot.classList.remove('is-loading'); } });
    };
    dashSel.addEventListener('change', function () { dashLoad(dashSel.value, true); });
    window.addEventListener('popstate', function () {
      var id = new URL(location.href).searchParams.get('results') || dash.dataset.default;
      if (id && id !== dashShown()) dashLoad(id, false);
    });
    window.addEventListener('hashchange', function () {                   // other admin tab: pause the card
      if (dashBoard) { if (dashOn()) dashBoard.start(); else dashBoard.stop(); }
    });
    dashStart();
  }
})();
