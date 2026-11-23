/* Ballot page: candidate cards, progress counter, "Review your vote" screen, and safe submit.
 *
 * Flow: pick a candidate for each position you want (or "No vote" to skip it) -> "Review vote"
 * -> check the list -> "Confirm vote", which calls the remote procedure rpc("cast_vote", {choices})
 * (JSON-RPC, see rpc.py); without JavaScript the normal form posts to /vote. Voting is optional per position.
 * Double taps are blocked here, and the server is still the real guard: cast_vote()
 * locks the student's row, so a second submit can never create a second ballot.
 */
(function () {
  'use strict';
  var form = document.getElementById('ballot');
  if (!form) return;

  var positions = Array.prototype.slice.call(form.querySelectorAll('fieldset.position'));
  var submitBtn = document.getElementById('submitBtn');
  var progressText = document.getElementById('progress-text');
  var progressFill = document.getElementById('progress-fill');
  var review = document.getElementById('review');
  var reviewList = document.getElementById('review-list');
  var reviewTitle = document.getElementById('review-title');
  var reviewWarning = document.getElementById('review-warning');
  var confirmBtn = document.getElementById('confirmBtn');
  var confirmIdle = confirmBtn.innerHTML;
  var sending = document.getElementById('sending');
  var submitting = false;
  var esc = BotoKo.esc;

  // Positions with seats > 1 ("Choose up to 3") use checkboxes, the others radio buttons.
  function seats(fs) { return Number(fs.dataset.seats) || 1; }
  // The chosen CANDIDATE inputs of a position ([] if skipped or not decided yet).
  function picks(fs) { return Array.prototype.slice.call(fs.querySelectorAll('.cand input:checked')); }
  function chosen(fs) { return picks(fs)[0] || null; }
  function skippedOnPurpose(fs) { return !!fs.querySelector('input[data-skip]:checked'); }

  /* Multi-seat: at most N picks. When N are chosen the other cards are disabled, with a message.
   * Choosing a candidate clears "No vote", choosing "No vote" clears the candidates. */
  function enforceLimit(fs) {
    if (seats(fs) < 2) return;
    var full = picks(fs).length >= seats(fs);
    fs.querySelectorAll('.cand input').forEach(function (inp) {
      var off = full && !inp.checked;
      inp.disabled = off;
      inp.closest('.cand').classList.toggle('is-disabled', off);
    });
    var msg = fs.querySelector('.seat-limit');
    if (msg) msg.hidden = !full;
  }
  form.addEventListener('change', function (e) {
    var t = e.target, fs = t.closest && t.closest('fieldset.position');
    if (!fs || seats(fs) < 2 || !t.checked) return;
    if (t.hasAttribute('data-skip')) {
      fs.querySelectorAll('.cand input:checked').forEach(function (inp) { inp.checked = false; });
    } else {
      var skip = fs.querySelector('input[data-skip]');
      if (skip) skip.checked = false;
    }
  }, true);                                          // capture: runs before the update() listener

  // Small round picture for the review list, built from the data-* attributes on the radio.
  function avatarHtml(input) {
    var d = input.dataset;
    return d.photo
      ? '<img class="avatar md" src="' + esc(d.photo) + '" alt="" width="40" height="40">'
      : '<span class="avatar md tone-' + esc(d.tone) + '" aria-hidden="true">' + esc(d.initials) + '</span>';
  }

  // Highlight the selected card (CSS :has() does this too; the class covers older phones).
  function markSelected() {
    form.querySelectorAll('.cand').forEach(function (card) {
      card.classList.toggle('is-selected', card.querySelector('.choice input').checked);
    });
  }

  function update() {
    var voted = 0, skipped = 0;
    positions.forEach(function (fs) {
      var n = picks(fs).length, max = seats(fs), skip = skippedOnPurpose(fs);
      if (n) voted++;
      if (skip) skipped++;
      fs.classList.toggle('is-done', n > 0);
      fs.classList.toggle('is-skipped', skip);
      fs.querySelector('.pos-state').textContent = max > 1
        ? (n ? n + ' of ' + max + ' selected' : skip ? 'No vote' : 'Choose up to ' + max + ' or skip')
        : (n ? 'Selected' : skip ? 'No vote' : 'Choose 1 or skip');
      enforceLimit(fs);
    });
    var total = positions.length;
    var text = voted + ' of ' + total + ' positions voted' + (skipped ? ' · ' + skipped + ' skipped' : '');
    if (progressText.textContent !== text && progressText.textContent && BotoKo.replay) BotoKo.replay(progressText, 'bump');
    progressText.textContent = text;
    progressFill.style.transform = 'scaleX(' + (total ? (voted + skipped) / total : 0) + ')';
    return { voted: voted, total: total };
  }

  /* ---------- tap a selected candidate again = unselect it ----------
   * Radio buttons can't normally be unchecked, so we remember each position's current
   * choice: clicking the radio that was ALREADY chosen clears it. */
  var current = {};
  function remember() {
    positions.forEach(function (fs) {
      var name = fs.querySelector('input').name;
      var on = Array.prototype.slice.call(fs.querySelectorAll('input:checked')).map(function (i) { return i.value; });
      current[name] = on.length ? on.join(',') : null;
    });
  }
  form.addEventListener('click', function (e) {
    var input = e.target;
    if (!input.matches || !input.matches('input[type=radio]:not([data-skip])')) return;
    if (current[input.name] === input.value) {           // it was already selected
      input.checked = false;
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }
  });

  /* ---------- "View full profile" dialog ---------- */
  var profile = document.getElementById('profile');
  var profileBody = document.getElementById('profile-body');
  var profileSelect = document.getElementById('profile-select');
  var profileInput = null;                       // the radio of the candidate being viewed
  form.querySelectorAll('[data-profile]').forEach(function (b) { b.hidden = false; });
  form.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-profile]');
    if (!btn) return;
    profileInput = btn.closest('.cand').querySelector('input[type=radio]');
    profileBody.innerHTML = '';
    profileBody.appendChild(document.getElementById(btn.dataset.profile).content.cloneNode(true));
    var already = profileInput.checked;
    profileSelect.disabled = already || profileInput.disabled;
    profileSelect.lastChild.textContent = already ? ' Selected'
      : profileInput.disabled ? ' Maximum reached: unselect one first' : ' Select this candidate';
    BotoKo.openModal(profile);
  });
  profileSelect.addEventListener('click', function () {
    if (!profileInput) return;
    profileInput.checked = true;
    profileInput.dispatchEvent(new Event('change', { bubbles: true }));
    BotoKo.closeModal(profile);
  });

  /* ---------- review screen ---------- */
  function openReview() {
    var counts = update(), skippedNames = [];
    reviewList.innerHTML = '';
    var row = 0;
    positions.forEach(function (fs) {
      var chosenInputs = picks(fs), max = seats(fs);
      if (!chosenInputs.length) {
        var li = document.createElement('li');
        li.style.setProperty('--i', row++);           // review rows slide in one by one (CSS)
        skippedNames.push(fs.dataset.position);
        li.className = 'rv-skipped';
        li.innerHTML = '<span class="avatar md rv-none" aria-hidden="true">–</span>' +
          '<span class="rv-text"><span class="rv-pos">' + esc(fs.dataset.position) + '</span>' +
          '<span class="rv-name">No vote (skipped)</span></span>' +
          '<button type="button" class="btn btn-sm" data-change="' + fs.id + '">Change</button>';
        reviewList.appendChild(li);
        return;
      }
      // Every chosen candidate (several for "Choose up to N"); the Change button on the first row.
      chosenInputs.forEach(function (input, k) {
        var d = input.dataset, li = document.createElement('li');
        li.style.setProperty('--i', row++);
        li.innerHTML = avatarHtml(input) +
          '<span class="rv-text"><span class="rv-pos">' + esc(fs.dataset.position) +
          (max > 1 ? ' · ' + (k + 1) + ' of ' + chosenInputs.length + ' chosen (up to ' + max + ')' : '') + '</span>' +
          '<span class="rv-name">' + esc(d.name) + '</span>' +
          (d.party ? '<span class="rv-party">' + esc(d.party) + '</span>' : '') + '</span>' +
          (k === 0 ? '<button type="button" class="btn btn-sm" data-change="' + fs.id + '">Change</button>' : '');
        reviewList.appendChild(li);
      });
    });
    var blank = counts.voted === 0;
    reviewTitle.textContent = blank ? 'Submit a blank ballot?' : 'Review your vote';
    if (blank) {
      reviewWarning.innerHTML = BotoKo.icon('alert') + '<p><strong>You did not vote for any position.</strong>' +
        'Submit a blank ballot? You will be marked as voted and can\'t vote again.</p>';
    } else if (skippedNames.length) {
      reviewWarning.innerHTML = BotoKo.icon('alert') + '<p><strong>You skipped ' + skippedNames.length +
        ' position' + (skippedNames.length === 1 ? '' : 's') + ':</strong>' + esc(skippedNames.join(', ')) +
        '. You can\'t vote for ' + (skippedNames.length === 1 ? 'it' : 'them') + ' later. Press Change to vote for it now.</p>';
    }
    reviewWarning.hidden = !(blank || skippedNames.length);
    confirmBtn.innerHTML = blank ? BotoKo.icon('check') + ' Submit blank ballot' : confirmIdle;
    BotoKo.openModal(review);
    // Add a history entry, so the phone's Back button closes the review screen
    // (choices stay selected) instead of leaving the ballot.
    if (window.history && history.pushState) {
      history.pushState({ botokoReview: true }, '');
      reviewInHistory = true;
    }
  }

  var reviewInHistory = false;
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
  // Phone/browser Back while reviewing: just close the review screen.
  window.addEventListener('popstate', function () {
    if (!reviewInHistory) return;
    reviewInHistory = false;
    if (!review.hidden && !submitting) BotoKo.closeModal(review);
  });
  // Closed with our Back button, Esc, or "Change": remove the history entry we added.
  review.onModalClose = function () {
    if (reviewInHistory && !submitting) { reviewInHistory = false; history.back(); }
  };

  /* Keep the choices if the student opens another page (e.g. Voter Profile from the menu)
   * and comes back: they are saved in this browser tab only (sessionStorage), per student,
   * and removed after voting (receipt page) or on log out. The server never sees them. */
  var KEY = 'botoko-ballot-' + form.dataset.voter + '-' + form.dataset.event;     // per student AND event
  function saveChoices() {
    var picks = {};
    positions.forEach(function (fs) {
      var on = fs.querySelectorAll('input:checked');
      if (on.length) picks[on[0].name] = Array.prototype.map.call(on, function (c) { return c.value; });   // "" = skipped
    });
    try { sessionStorage.setItem(KEY, JSON.stringify(picks)); } catch (e) { /* storage blocked */ }
  }
  function restoreChoices() {
    var picks = {};
    try { picks = JSON.parse(sessionStorage.getItem(KEY) || '{}'); } catch (e) { return; }
    Object.keys(picks).forEach(function (name) {
      [].concat(picks[name]).forEach(function (value) {
        var input = form.querySelector('input[name="' + name + '"][value="' + value + '"]');
        if (input) input.checked = true;          // ignored if that candidate no longer exists
      });
    });
  }

  /* After a position gets its FIRST answer: the chosen card "pops", then the page glides to the
   * next position. Changing your mind in the same position doesn't scroll. */
  function scrollToNext(fs) {
    var next = positions[positions.indexOf(fs) + 1];
    if (!next) return;
    setTimeout(function () {
      if (!review.hidden || !profile.hidden) return;         // a dialog is open: stay put
      var top = next.getBoundingClientRect().top;
      if (top > 8 && top < innerHeight * 0.35) return;       // already right there
      next.scrollIntoView({ behavior: BotoKo.reducedMotion && BotoKo.reducedMotion() ? 'auto' : 'smooth', block: 'start' });
    }, 380);
  }
  form.addEventListener('change', function (e) {
    var t = e.target, fs = t && t.closest && t.closest('fieldset.position');
    var isChoice = fs && t.matches('input[type=radio], input[type=checkbox]');
    var before = isChoice ? current[t.name] : null;
    update(); markSelected(); remember(); saveChoices();
    if (isChoice && t.checked) {
      if (BotoKo.replay) BotoKo.replay(t.closest('.cand') || t.closest('.skip-choice'), 'just-picked');
      // Single choice: glide on after the first answer. "Choose up to N": once N are chosen (or skipped).
      var done = seats(fs) < 2 ? before == null
        : t.hasAttribute('data-skip') || picks(fs).length === seats(fs);
      if (done) scrollToNext(fs);
    }
  });

  // Logging out with choices that were not submitted yet: ask first (the choices would be lost).
  document.addEventListener('click', function (e) {
    var out = e.target.closest && e.target.closest('[data-logout]');
    if (!out || submitting || !positions.some(function (fs) { return fs.querySelector('input:checked'); })) return;
    e.preventDefault();
    BotoKo.confirm({ title: 'Log out without voting?', text: 'Your choices on this ballot have not been submitted yet.',
                     ok: 'Log out', danger: true }, function () { location.href = out.href; });
  });
  restoreChoices();
  markSelected();
  remember();

  // "Review vote" opens the review screen instead of submitting.
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (submitting) return;
    openReview();
  });

  // "Change" in the review list: close it and jump to that position.
  reviewList.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-change]');
    if (!btn) return;
    BotoKo.closeModal(review);
    var fs = document.getElementById(btn.dataset.change);
    fs.scrollIntoView({ behavior: 'smooth', block: 'center' });
    var input = fs.querySelector('input[type=radio]:checked') || fs.querySelector('input[type=radio]');
    if (input) input.focus({ preventScroll: true });
  });

  // The ballot as RPC params: {"<position id>": <candidate id>, [ids] for "Choose up to N",
  // or null = skipped ("No vote")}
  function collectChoices() {
    var choices = {};
    positions.forEach(function (fs) {
      var ids = picks(fs).map(function (i) { return Number(i.value); });
      choices[fs.id.replace('pos-', '')] = !ids.length ? null : seats(fs) > 1 ? ids : ids[0];
    });
    return choices;
  }

  function unlock() {
    submitting = false;
    sending.hidden = true;
    review.removeAttribute('data-locked');
    review.querySelectorAll('button').forEach(function (b) { b.disabled = false; });
    BotoKo.clearBusy(confirmBtn);
  }

  // Final confirm: submit exactly once, as a remote procedure call: rpc("cast_vote", {choices}).
  // The server runs the same cast_vote() as the form (transaction + row lock), then we show the receipt.
  confirmBtn.addEventListener('click', function () {
    if (submitting) return;
    submitting = true;
    BotoKo.setBusy(confirmBtn, 'Submitting...');
    review.setAttribute('data-locked', '');          // can't close the review screen any more
    review.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
    sending.hidden = false;
    var slowNote = setTimeout(function () {
      document.getElementById('sending-note').textContent =
        'Still working: many students are voting right now. Please wait, your vote is in line.';
    }, 8000);
    if (!window.rpc) { form.submit(); return; }       // rpc.js missing: post the form instead
    rpc('cast_vote', { event_id: Number(form.dataset.event), choices: collectChoices() }).then(function (r) {
      if (BotoKo.markJustVoted) BotoKo.markJustVoted();     // receipt page: "Vote submitted" + confetti
      if (BotoKo.setDone) BotoKo.setDone(confirmBtn, 'Vote submitted');
      setTimeout(function () { location.replace(r.receipt_url); }, 350);   // the receipt; Back won't return to the ballot
    }, function (err) {
      clearTimeout(slowNote);
      var c = rpc.codes;
      if (err.code === c.ALREADY_VOTED || err.code === c.VOTING_CLOSED || err.code === c.NOT_LOGGED_IN ||
          err.code === c.PASSWORD_CHANGE_REQUIRED) {
        // The ballot page shows the right thing for each case: the receipt ("already voted"),
        // "voting is closed", the login page, or the change-password page.
        location.replace('/e/' + form.dataset.event + '/ballot');
        return;
      }
      unlock();
      BotoKo.toast(err.code === c.NETWORK
        ? err.message + ' Press "Confirm vote" again: you can never vote twice, so trying again is safe.'
        : err.message, 'error');
    });
  });

  // If the browser restores this page from its back/forward cache, unlock it again.
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    reviewInHistory = false;
    unlock();
    BotoKo.closeModal(review);
    update();
  });

  submitBtn.disabled = false;
  update();
})();
