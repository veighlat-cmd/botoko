"""BotoKo functional test cases TC-01 .. TC-14 (project tracker task 5.1).

Starts the app (serve.py, normal setup) against the TEST database botoko_test (never botoko_live), seeds synthetic
accounts TEST-0001.., runs every case with real HTTP requests (sessions + CSRF tokens like a browser) and checks the
outcome in the database. Writes test_evidence/Test_Cases.xlsx (+ test_evidence/raw/test_cases.json).

    python tests/run_test_cases.py
"""
import datetime as dt
import html
import io
import json
import re
import sys
import threading
import time
import traceback
from pathlib import Path

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import botoko_testkit as kit  # noqa: E402

TODAY = dt.date.today().isoformat()
CASES = []


def case(tc_id, title, steps, expected):
    def deco(fn):
        CASES.append({"id": tc_id, "title": title, "steps": steps, "expected": expected, "fn": fn})
        return fn
    return deco


def text_of(resp):
    """Visible text of an HTML page (tags removed), to look for messages shown to the user."""
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", resp.text, flags=re.S)
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t)))


def ballots_of(sid, event_id=None):
    if event_id:
        return kit.q("SELECT COUNT(*) FROM ballots WHERE student_id = %s AND event_id = %s", (sid, event_id))[0][0]
    return kit.q("SELECT COUNT(*) FROM ballots WHERE student_id = %s", (sid,))[0][0]


def form_votes(ballot, picks):
    """The ballot FORM fields (pos_<id> may repeat for multi-seat positions)."""
    data = []
    for pos, cids in zip(ballot, picks):
        for cid in cids:
            data.append((f"pos_{pos['id']}", str(cid)))
    return data


# ============================================================================ the cases
@case("TC-01", "Valid login", "Open the login page, enter Voter ID TEST-0001 and the correct password, submit.",
      "Logged in; the 'Choose a voting event' page (/events) is shown.")
def tc01(S):
    c = kit.Client()
    r = c.login(kit.voter_id(1), kit.STUDENT_PASSWORD)
    shown = "Choose a voting event" in text_of(r)
    actual = f"HTTP {r.status_code}, landed on {r.url.replace(kit.BASE, '')}, 'Choose a voting event' shown: {shown}"
    return r.url.endswith("/events") and shown, actual


@case("TC-02", "Wrong password", "Login with Voter ID TEST-0002 and a wrong password; then open /events.",
      "Error message shown; not logged in (/events sends back to the login page).")
def tc02(S):
    c = kit.Client()
    r = c.login(kit.voter_id(2), "definitely-wrong-password")
    msg = "Wrong Voter ID or password" in text_of(r) or "wrong" in text_of(r).lower()
    after = c.get("/events")
    logged_out = 'id="student_no"' in after.text and "/events" not in after.url.split("?")[0][-7:]
    actual = (f"HTTP {r.status_code}, stayed on {r.url.replace(kit.BASE, '')}, error shown: {msg}; "
              f"/events -> {after.url.replace(kit.BASE, '')} (login form shown: {'id=\"student_no\"' in after.text})")
    return msg and logged_out, actual


@case("TC-03", "Unregistered Voter ID", "Login with Voter ID TEST-9999 (no such account) and any password; open /events.",
      "Error message shown; not logged in.")
def tc03(S):
    c = kit.Client()
    r = c.login("TEST-9999", kit.STUDENT_PASSWORD)
    msg = "wrong" in text_of(r).lower()
    after = c.get("/events")
    actual = (f"HTTP {r.status_code}, error shown: {msg}; /events -> {after.url.replace(kit.BASE, '')} "
              f"(login form shown: {'id=\"student_no\"' in after.text})")
    return msg and 'id="student_no"' in after.text, actual


@case("TC-04", "Unapproved student (auto-approve OFF)",
      "TEST-0004 logs in, taps Register on 'TEST Approval Election' (auto-approve OFF), then opens its ballot and also "
      "tries RPC cast_vote.",
      "Registration is PENDING; ballot not available ('pending approval' message); vote refused and not saved.")
def tc04(S):
    c = kit.Client()
    c.login(kit.voter_id(4), kit.STUDENT_PASSWORD)
    ev = S["pending"]
    page = c.get("/events")
    reg = c.post(f"/e/{ev}/register", {"csrf": c.csrf_in(page.text)})
    reg_text = text_of(reg)
    status = kit.q("""SELECT r.status FROM registrations r WHERE r.event_id = %s AND r.student_id = %s""",
                   (ev, S["ids"][kit.voter_id(4)]))
    bal = c.get(f"/e/{ev}/ballot")
    pending_msg = "pending approval" in text_of(bal).lower()
    c.fetch_rpc_token()
    pb = S["pending_ballot"][0]
    rpc = c.rpc("cast_vote", {"event_id": ev, "choices": {str(pb["id"]): pb["candidates"][0]}})
    saved = ballots_of(S["ids"][kit.voter_id(4)], ev)
    actual = (f"register -> 'PENDING' message: {'PENDING' in reg_text}, DB status: {status[0][0] if status else None}; "
              f"ballot -> {bal.url.replace(kit.BASE, '')} with 'pending approval': {pending_msg}; "
              f"RPC cast_vote -> error {rpc.get('error', {}).get('code')} '{rpc.get('error', {}).get('message', '')}'; "
              f"ballots saved: {saved}")
    return (status and status[0][0] == "pending" and pending_msg and "error" in rpc and saved == 0
            and "/ballot" not in bal.url), actual


@case("TC-05", "Cast a vote", "TEST-0005 logs in, opens the ballot of 'TEST Main Election', submits the ballot form "
      "(President, Vice President, 2 Councilors).",
      "Vote saved in the database (1 ballot, 4 vote rows); the receipt page is shown.")
def tc05(S):
    c = kit.Client()
    c.login(kit.voter_id(5), kit.STUDENT_PASSWORD)
    ev, b = S["main"], S["main_ballot"]
    page = c.get(f"/e/{ev}/ballot")
    picks = [[b[0]["candidates"][0]], [b[1]["candidates"][1]], b[2]["candidates"][:2]]
    r = c.post(f"/e/{ev}/vote", [("csrf", c.csrf_in(page.text))] + form_votes(b, picks))
    sid = S["ids"][kit.voter_id(5)]
    rows = kit.q("SELECT COUNT(*) FROM votes WHERE student_id = %s", (sid,))[0][0]
    code = kit.q("SELECT receipt_code FROM ballots WHERE student_id = %s AND event_id = %s", (sid, ev))
    S["tc05_code"] = code[0][0] if code else None
    shown = "Your vote was recorded" in text_of(r) and "OFFICIAL VOTING RECEIPT" in text_of(r)
    actual = (f"HTTP {r.status_code}, landed on {r.url.replace(kit.BASE, '')}, receipt shown: {shown}, "
              f"receipt code {S['tc05_code']}; DB: {ballots_of(sid, ev)} ballot, {rows} vote rows")
    return r.url.endswith(f"/e/{ev}/receipt") and shown and ballots_of(sid, ev) == 1 and rows == 4, actual


@case("TC-06", "Duplicate vote in the same event",
      "TEST-0005 (already voted in TC-05) submits the ballot form again, then tries RPC cast_vote too.",
      "Both rejected ('already voted'); still exactly 1 ballot and 4 vote rows in the database.")
def tc06(S):
    c = kit.Client()
    c.login(kit.voter_id(5), kit.STUDENT_PASSWORD)
    ev, b = S["main"], S["main_ballot"]
    page = c.get("/events")
    r = c.post(f"/e/{ev}/vote", [("csrf", c.csrf_in(page.text))] + form_votes(b, [[b[0]["candidates"][1]], [], []]))
    c.fetch_rpc_token()
    rpc = c.rpc("cast_vote", {"event_id": ev, "choices": {str(b[0]["id"]): b[0]["candidates"][1]}})
    sid = S["ids"][kit.voter_id(5)]
    rows = kit.q("SELECT COUNT(*) FROM votes WHERE student_id = %s", (sid,))[0][0]
    already = "already voted" in text_of(r).lower()
    actual = (f"form -> {r.url.replace(kit.BASE, '')}, 'already voted' shown: {already}; RPC -> error "
              f"{rpc.get('error', {}).get('code')} '{rpc.get('error', {}).get('message', '')}'; "
              f"DB: {ballots_of(sid, ev)} ballot, {rows} vote rows")
    return already and rpc.get("error", {}).get("code") == -32011 and ballots_of(sid, ev) == 1 and rows == 4, actual


@case("TC-07", "Vote in an upcoming or closed event",
      "TEST-0007 (registered in 'TEST Upcoming Election' and 'TEST Closed Election') opens both ballots and tries to "
      "vote in both (ballot form + RPC cast_vote).",
      "No ballot (no candidates to choose) for either event; every vote attempt refused; nothing saved.")
def tc07(S):
    c = kit.Client()
    c.login(kit.voter_id(7), kit.STUDENT_PASSWORD)
    sid = S["ids"][kit.voter_id(7)]
    c.fetch_rpc_token()
    parts, good = [], True
    for key in ("upcoming", "closed"):
        ev, pb = S[key], S[key + "_ballot"][0]
        page = c.get(f"/e/{ev}/ballot")
        has_choices = 'name="pos_' in page.text
        form = c.post(f"/e/{ev}/vote", [("csrf", c.csrf_in(page.text) or c.rpc_token), (f"pos_{pb['id']}", str(pb["candidates"][0]))])
        rpc = c.rpc("cast_vote", {"event_id": ev, "choices": {str(pb["id"]): pb["candidates"][0]}})
        saved = ballots_of(sid, ev)
        parts.append(f"{key}: ballot page has candidates: {has_choices}; form vote -> {form.url.replace(kit.BASE, '')}; "
                     f"RPC -> error {rpc.get('error', {}).get('code')} '{rpc.get('error', {}).get('message', '')}'; saved: {saved}")
        good = good and not has_choices and "error" in rpc and saved == 0
    return good, " | ".join(parts)


@case("TC-08", "More candidates than allowed",
      "TEST-0008 submits the ballot with 4 Councilors (the position allows up to 3): ballot form and RPC cast_vote.",
      "Blocked with 'Too many candidates' message; nothing saved.")
def tc08(S):
    c = kit.Client()
    c.login(kit.voter_id(8), kit.STUDENT_PASSWORD)
    ev, b = S["main"], S["main_ballot"]
    page = c.get(f"/e/{ev}/ballot")
    picks = [[b[0]["candidates"][0]], [b[1]["candidates"][0]], b[2]["candidates"][:4]]
    r = c.post(f"/e/{ev}/vote", [("csrf", c.csrf_in(page.text))] + form_votes(b, picks))
    c.fetch_rpc_token()
    rpc = c.rpc("cast_vote", {"event_id": ev, "choices": {str(b[2]["id"]): b[2]["candidates"][:4]}})
    sid = S["ids"][kit.voter_id(8)]
    msg = "Too many candidates" in text_of(r)
    actual = (f"form -> {r.url.replace(kit.BASE, '')}, 'Too many candidates chosen for one position (at most 3)' shown: "
              f"{msg}; RPC -> error {rpc.get('error', {}).get('code')} '{rpc.get('error', {}).get('message', '')}'; "
              f"ballots saved: {ballots_of(sid, ev)}")
    return msg and "error" in rpc and ballots_of(sid, ev) == 0, actual


@case("TC-09", "Expired / invalid session submits a vote",
      "(a) a browser with a forged session cookie submits the ballot form; (b) TEST-0009 logs in, then the session is "
      "invalidated on the server (password changed elsewhere: session_version + 1) and the open ballot is submitted; "
      "(c) RPC cast_vote without a session.",
      "Each time: sent to the login page / 'not logged in'; no vote saved.")
def tc09(S):
    ev, b = S["main"], S["main_ballot"]
    data = form_votes(b, [[b[0]["candidates"][0]], [], []])
    forged = kit.Client()
    forged.s.cookies.set("session", "eyJzdHVkZW50X2lkIjoxfQ.forged.signature", domain="127.0.0.1")
    ra = forged.post(f"/e/{ev}/vote", [("csrf", "forged")] + data)
    a_ok = 'id="student_no"' in ra.text
    c = kit.Client()
    c.login(kit.voter_id(9), kit.STUDENT_PASSWORD)
    page = c.get(f"/e/{ev}/ballot")
    sid = S["ids"][kit.voter_id(9)]
    kit.q("UPDATE students SET session_version = session_version + 1 WHERE id = %s", (sid,))
    rb = c.post(f"/e/{ev}/vote", [("csrf", c.csrf_in(page.text))] + data)
    b_ok = 'id="student_no"' in rb.text and "logged out" in text_of(rb).lower()
    anon = kit.Client()
    anon.rpc_token = anon.get("/session/refresh").json()["csrf"]
    rc = anon.rpc("cast_vote", {"event_id": ev, "choices": {str(b[0]["id"]): b[0]["candidates"][0]}})
    total_forged = kit.q("SELECT COUNT(*) FROM ballots WHERE student_id = 1 AND event_id = %s", (ev,))[0][0]
    actual = (f"(a) forged cookie -> {ra.url.replace(kit.BASE, '')}, login form: {a_ok}; "
              f"(b) expired session -> {rb.url.replace(kit.BASE, '')}, login form + 'logged out' message: {b_ok}; "
              f"(c) RPC -> error {rc.get('error', {}).get('code')} '{rc.get('error', {}).get('message', '')}'; "
              f"ballots saved: TEST-0009 {ballots_of(sid, ev)}, student #1 {total_forged}")
    return a_ok and b_ok and rc.get("error", {}).get("code") == -32001 and ballots_of(sid, ev) == 0 and total_forged == 0, actual


@case("TC-10", "Receipt download PNG and JPG",
      "TEST-0005 (voted in TC-05) downloads /receipt/<code>.png and .jpg.",
      "HTTP 200, valid PNG and JPEG images (open with an image library), about 1080 px wide.")
def tc10(S):
    c = kit.Client()
    c.login(kit.voter_id(5), kit.STUDENT_PASSWORD)
    parts, good = [], bool(S.get("tc05_code"))
    for fmt, want in (("png", "PNG"), ("jpg", "JPEG")):
        r = c.get(f"/receipt/{S.get('tc05_code')}.{fmt}")
        try:
            im = Image.open(io.BytesIO(r.content))
            im.load()
            desc = f"{im.format} {im.size[0]}x{im.size[1]}"
            good = good and r.status_code == 200 and im.format == want and im.size[0] == 1080
        except Exception as e:                      # noqa: BLE001
            desc, good = f"not an image ({e})", False
        parts.append(f"{fmt}: HTTP {r.status_code}, {r.headers.get('Content-Type')}, {desc}, {len(r.content)} bytes, "
                     f"file name {r.headers.get('Content-Disposition', '').split('filename=')[-1]}")
    return good, " | ".join(parts)


@case("TC-11", "Live results update after a vote from a second client",
      "Client A (TEST-0011) reads the live results (RPC get_results); client B (TEST-0012, separate session) votes; "
      "A reads the results again after 2 s.",
      "A's second reading shows 1 more ballot and +1 vote for the candidate B chose.")
def tc11(S):
    ev, b = S["main"], S["main_ballot"]
    a = kit.Client()
    a.login(kit.voter_id(11), kit.STUDENT_PASSWORD)
    a.fetch_rpc_token()
    before = a.rpc("get_results", {"event_id": ev})["result"]
    bb = kit.Client()
    bb.login(kit.voter_id(12), kit.STUDENT_PASSWORD)
    bb.fetch_rpc_token()
    cand = b[0]["candidates"][1]
    vote = bb.rpc("cast_vote", {"event_id": ev, "choices": {str(b[0]["id"]): cand}})
    time.sleep(2.0)                                     # results are cached for 1.5 s
    after = a.rpc("get_results", {"event_id": ev})["result"]

    def votes_for(res):
        for p in res["positions"]:
            for c in p["candidates"]:
                if c["id"] == cand:
                    return c["votes"]
    actual = (f"B's vote: {'recorded, receipt ' + vote['result']['receipt_code'] if 'result' in vote else vote.get('error')}; "
              f"A before: {before['ballots']} ballots, candidate {votes_for(before)} votes; "
              f"A after: {after['ballots']} ballots, candidate {votes_for(after)} votes")
    return "result" in vote and after["ballots"] == before["ballots"] + 1 and votes_for(after) == votes_for(before) + 1, actual


@case("TC-12", "Admin login",
      "On the same login page, enter Voter ID 'admin' and the (test) admin password.",
      "The COMELEC admin dashboard (/admin) is shown.")
def tc12(S):
    c = kit.Client()
    r = c.login("admin", kit.ADMIN_PASSWORD)
    dash = "panel-overview" in r.text and "Election management" in text_of(r)
    actual = f"HTTP {r.status_code}, landed on {r.url.replace(kit.BASE, '')}, dashboard shown: {dash}"
    return r.url.endswith("/admin") and dash, actual


@case("TC-13", "100 simultaneous votes",
      "100 different students (TEST-0101..TEST-0200) log in first; then all 100 send RPC cast_vote at the same instant "
      "(100 client threads released together). Then the database is queried.",
      "All 100 votes accepted; exactly 100 ballots, 0 duplicates, every candidate's count = what was sent.")
def tc13(S):
    ev, b = S["main"], S["main_ballot"]
    before = kit.q("SELECT COUNT(*) FROM ballots WHERE event_id = %s", (ev,))[0][0]
    idx = list(range(101, 201))
    clients, login_errors = kit.login_many(idx)
    res, total = kit.vote_burst(clients, ev, b, concurrency=100)
    ok = sum(1 for r in res if r[1])
    errs = [r[3] for r in res if not r[1]]
    ids = [S["ids"][kit.voter_id(i)] for i in idx]
    n, distinct = kit.q("""SELECT COUNT(*), COUNT(DISTINCT student_id) FROM ballots WHERE event_id = %s AND student_id = ANY(%s)""",
                        (ev, ids))[0]
    per_cand = dict(kit.q("""SELECT v.candidate_id, COUNT(*) FROM votes v JOIN ballots b ON b.id = v.ballot_id
                             WHERE b.event_id = %s AND b.student_id = ANY(%s) GROUP BY v.candidate_id""", (ev, ids)))
    exp, exp_rows = kit.expected_tally(idx, b)
    after = kit.q("SELECT COUNT(*) FROM ballots WHERE event_id = %s", (ev,))[0][0]
    pids = kit.q("SELECT COUNT(DISTINCT worker_pid) FROM ballots WHERE event_id = %s AND student_id = ANY(%s)", (ev, ids))[0][0]
    S["tc13"] = {"accepted": ok, "errors": errs[:5], "seconds": round(total, 3), "worker_pids": pids}
    actual = (f"logins ok {len(clients)}/100; accepted {ok}/100 in {total:.2f} s, errors {len(errs)}; DB: {n} ballots "
              f"({n - distinct} duplicates), event total {before} -> {after}; tally matches what was sent: {per_cand == exp} "
              f"({sum(per_cand.values())}/{exp_rows} vote rows); handled by {pids} worker processes")
    return (len(clients) == 100 and ok == 100 and n == 100 and distinct == 100 and per_cand == exp
            and after == before + 100), actual


@case("TC-14", "Server unavailable",
      "TEST-0014 logs in (has a session + token); the server is stopped; the client sends RPC cast_vote; the database "
      "is checked.",
      "The request fails (connection refused); no vote recorded.")
def tc14(S):
    c = kit.Client(timeout=10)
    c.login(kit.voter_id(14), kit.STUDENT_PASSWORD)
    c.fetch_rpc_token()
    ev, b = S["main"], S["main_ballot"]
    total_before = kit.q("SELECT COUNT(*) FROM ballots")[0][0]
    S["server"].stop()
    try:
        c.rpc("cast_vote", {"event_id": ev, "choices": {str(b[0]["id"]): b[0]["candidates"][0]}})
        outcome, failed = "request unexpectedly succeeded", False
    except requests.RequestException as e:
        outcome, failed = f"request failed: {type(e).__name__}", True
    sid = S["ids"][kit.voter_id(14)]
    total_after = kit.q("SELECT COUNT(*) FROM ballots")[0][0]
    actual = f"server stopped; {outcome}; TEST-0014 ballots: {ballots_of(sid)}; total ballots {total_before} -> {total_after}"
    return failed and ballots_of(sid) == 0 and total_after == total_before, actual


# ============================================================================ TC-15 .. TC-20 (defense fixes)
class Manual(Exception):
    """The case needs a real browser and Playwright isn't installed: reported as MANUAL, never as PASS."""


def browser(S):
    if "br" not in S:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise Manual("needs a real browser: pip install playwright (Microsoft Edge is used)")
        S["pw"] = sync_playwright().start()
        S["br"] = S["pw"].chromium.launch(channel="msedge", headless=True)
    return S["br"]


def page_login(S, user, password, w=1366, h=900, mobile=False):
    ctx = browser(S).new_context(viewport={"width": w, "height": h}, has_touch=mobile, is_mobile=mobile)
    p = ctx.new_page()
    p.on("pageerror", lambda e: S.setdefault("js_errors", []).append(f"{p.url}: {e}"))
    p.goto(kit.BASE + "/")
    p.fill("#student_no", user)
    p.fill("#password", password)
    p.click("main form button[type=submit]")
    p.wait_for_load_state("domcontentloaded")
    return p


def admin_rpc(S):
    if "admin_rpc" not in S:
        c = kit.Client()
        c.login("admin", kit.ADMIN_PASSWORD)
        c.fetch_rpc_token()
        S["admin_rpc"] = c
    return S["admin_rpc"]


def create_account(name, school_no):
    """A NEW student creates an account on the website (pending verification). Returns (voter id, student id)."""
    c = kit.Client()
    page = c.get("/account/create")
    c.post("/account/create", {"csrf": c.csrf_in(page.text), "school_student_no": school_no, "full_name": name,
                               "course": "BSIT", "department": "TEST Department", "year": "1", "section": "A",
                               "password": kit.STUDENT_PASSWORD, "confirm_password": kit.STUDENT_PASSWORD})
    row = kit.q("SELECT student_no, id FROM students WHERE school_student_no = %s", (school_no,))
    return row[0] if row else (None, None)


def wait_until(fn, timeout=15.0, step=0.25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return round(time.time() - t0, 1)
        time.sleep(step)
    return None


def setup_defense_events(S):
    """Three more CURRENT events (60 students each: TEST-0021..0080): end-early (auto-publish OFF),
    race (auto-publish OFF) and auto-publish ON."""
    with kit.db() as c:
        for key, name, auto in (("e17", "TEST End-Early Election", False), ("e18", "TEST Race Election", False),
                                ("e19", "TEST Auto-Publish Election", True)):
            eid = kit._event(c, name, "now() - interval '1 day'", "now() + interval '3 days'")
            c.execute("UPDATE events SET auto_publish = %s WHERE id = %s", (auto, eid))
            S[key], S[key + "_ballot"] = eid, kit._ballot(c, eid, kit.BALLOT)
            with c.cursor() as cur:
                cur.executemany("INSERT INTO event_voters (event_id, student_id) VALUES (%s, %s)",
                                [(eid, S["ids"][kit.voter_id(i)]) for i in range(21, 81)])


@case("TC-15", "Admin pending list shows a newly registered student automatically (no refresh)",
      "Admin opens the Accounts tab, filters 'Pending' and searches 'tc15'. A NEW student creates an account; another "
      "new student (name not matching the search) too. Then a verified student registers for the approval-required "
      "event (Registrations tab). Finally the first new account is approved from ANOTHER admin session.",
      "Within a few seconds the new student's row (Approve / Reject) appears by itself, the page is not reloaded, the "
      "filter/search stay (the non-matching row is hidden), no duplicate rows; the new registration row appears too; "
      "the row approved elsewhere disappears from the pending list.")
def tc15(S):
    p = page_login(S, "admin", kit.ADMIN_PASSWORD)
    p.goto(f"{kit.BASE}/admin?event={S['pending']}#accounts")
    p.wait_for_timeout(1500)
    p.click('[data-acc-filter="pending"]')
    p.fill("#acc-search", "tc15")
    p.evaluate("window.__noReload = 1")
    badge0 = p.text_content('[data-a="tab-count"]').strip()
    vid, sid = create_account("TC15 Newstudent Alpha", "98-1-01501")
    create_account("Zed Otherperson", "98-1-01502")
    row = '#acc-rows tr:has-text("TC15 Newstudent Alpha")'
    t_acc = wait_until(lambda: p.locator(row).count() == 1 and p.locator(row).is_visible())
    other_hidden = wait_until(lambda: p.locator('#acc-rows tr:has-text("Zed Otherperson")').count() == 1, 8) is not None \
        and not p.locator('#acc-rows tr:has-text("Zed Otherperson")').is_visible()
    has_buttons = p.locator(row + " button:has-text('Approve')").count() == 1 and p.locator(row + " button:has-text('Reject')").count() == 1
    p.wait_for_timeout(9000)                                   # two more polls: still exactly one row
    dup_free = p.locator(row).count() == 1
    no_reload = p.evaluate("window.__noReload === 1")
    kept = p.input_value("#acc-search") == "tc15" and p.get_attribute('[data-acc-filter="pending"]', "aria-pressed") == "true"
    badge1 = p.text_content('[data-a="tab-count"]').strip()
    # registrations tab: a verified student registers for the approval-required event
    p.evaluate("location.hash = '#registrations'")
    v = kit.Client()
    v.login(kit.voter_id(15), kit.STUDENT_PASSWORD)
    v.post(f"/e/{S['pending']}/register", {"csrf": v.csrf_in(v.get('/events').text)})
    t_reg = wait_until(lambda: p.locator('#reg-rows tr:has-text("TEST Student 0015")').count() == 1)
    # approved from ANOTHER admin session -> the row leaves the pending list
    p.evaluate("location.hash = '#accounts'")
    admin_rpc(S).rpc("verify_account", {"student_id": sid})
    t_gone = wait_until(lambda: p.locator(row).count() == 0)
    still_no_reload = p.evaluate("window.__noReload === 1")
    p.context.close()
    actual = (f"account row appeared after {t_acc} s (badge {badge0} -> {badge1}), Approve/Reject buttons: {has_buttons}; "
              f"non-matching new row present but hidden by the search: {other_hidden}; no duplicates after 2 more polls: "
              f"{dup_free}; filter + search kept: {kept}; page not reloaded: {no_reload and still_no_reload}; "
              f"registration row appeared after {t_reg} s; approved in another admin session -> row removed after {t_gone} s")
    return (t_acc is not None and t_acc <= 10 and has_buttons and other_hidden and dup_free and kept and no_reload
            and still_no_reload and t_reg is not None and t_gone is not None), actual


@case("TC-16", "Waiting student is redirected to events automatically after approval (no click)",
      "A new student creates an account and logs in on a phone-size screen: the 'waiting for verification' page. The admin "
      "verifies the account (from the admin side). Nothing is clicked on the student's screen. A second waiting student "
      "(desktop) is rejected with a reason.",
      "Within a few seconds the student sees 'You're approved!' and is taken to the events page by itself; the rejected "
      "student sees the rejection reason by itself.")
def tc16(S):
    vid, sid = create_account("TC16 Waiting Student", "98-1-01601")
    p = page_login(S, vid, kit.STUDENT_PASSWORD, 390, 844, mobile=True)
    waiting = p.url.endswith("/account") and "waiting for verification" in p.inner_text("main")
    admin_rpc(S).rpc("verify_account", {"student_id": sid})
    t0 = time.time()
    try:
        p.wait_for_selector("text=You're approved", timeout=15000)
        t_msg = round(time.time() - t0, 1)
    except Exception:                                   # noqa: BLE001
        t_msg = None
    try:
        p.wait_for_url("**/events", timeout=15000)
        t_url = round(time.time() - t0, 1)
    except Exception:                                   # noqa: BLE001
        t_url = None
    events_shown = "Choose a voting event" in p.inner_text("main") if t_url else False
    p.context.close()
    vid2, sid2 = create_account("TC16 Rejected Student", "98-1-01602")
    q = page_login(S, vid2, kit.STUDENT_PASSWORD)
    admin_rpc(S).rpc("reject_account", {"student_id": sid2, "reason": "School number not on the class list"})
    t1 = time.time()
    try:
        q.wait_for_selector("text=Your account was rejected: School number not on the class list", timeout=15000)
        t_rej = round(time.time() - t1, 1)
    except Exception:                                   # noqa: BLE001
        t_rej = None
    q.context.close()
    actual = (f"phone: waiting page shown: {waiting}; after approval 'You're approved!' after {t_msg} s and the events page "
              f"('Choose a voting event': {events_shown}) after {t_url} s, without any click; desktop: rejection reason shown "
              f"after {t_rej} s without any click")
    return waiting and t_msg is not None and t_url is not None and t_url <= 12 and events_shown and t_rej is not None, actual


@case("TC-17", "Admin ends voting early; ballot closes and new votes are rejected",
      "'TEST End-Early Election' is open (ends in 3 days, auto-publish OFF). TEST-0021 votes. The admin opens the event "
      "page, clicks 'End voting now' and confirms. Then TEST-0022 opens the ballot and tries to vote (form + RPC).",
      "Confirmation 'End voting now? Students will no longer be able to vote. This cannot be undone.'; the event becomes "
      "Previous/closed with the end time = now, the action is logged (who + when), no reopen button; the ballot has no "
      "candidates; the vote is rejected with a clear message; TEST-0021's vote is still counted.")
def tc17(S):
    e, b = S["e17"], S["e17_ballot"]
    v = kit.Client()
    v.login(kit.voter_id(21), kit.STUDENT_PASSWORD)
    v.fetch_rpc_token()
    first = v.rpc("cast_vote", {"event_id": e, "choices": kit.choices_for(21, b)})
    p = page_login(S, "admin", kit.ADMIN_PASSWORD)
    p.goto(f"{kit.BASE}/admin?event={e}")
    p.wait_for_timeout(800)
    btn = p.locator(".admin-head .toggle-form button")
    label = btn.inner_text().strip()
    btn.click()
    p.wait_for_selector("#confirm-ok", state="visible")
    title, text = p.inner_text("#confirm-title").strip(), p.inner_text("#confirm-text").strip()
    p.click("#confirm-ok")
    p.wait_for_timeout(2500)
    head = p.inner_text(".admin-head")
    reopen = p.locator(".admin-head .toggle-form").count()
    p.context.close()
    manual, ended_now, status = kit.q(f"""SELECT manual, ends_at <= now() AND ends_at > now() - interval '5 minutes',
                                              {'CASE WHEN manual = %s THEN %s ELSE %s END'} FROM events WHERE id = %s""",
                                       ("closed", "previous", "other", e))[0]
    log = S["server"].log_path.read_text(encoding="utf-8", errors="replace")
    logged = any("VOTING ENDED by admin" in ln and f"for event {e} " in ln for ln in log.splitlines())
    s = kit.Client()
    s.login(kit.voter_id(22), kit.STUDENT_PASSWORD)
    page = s.get(f"/e/{e}/ballot")
    has_choices = 'name="pos_' in page.text
    s.fetch_rpc_token()                                   # (the closed ballot page has no form: use the session's token)
    form = s.post(f"/e/{e}/vote", [("csrf", s.csrf_in(page.text) or s.rpc_token)] + form_votes(b, [[b[0]["candidates"][0]], [], []]))
    rpc = s.rpc("cast_vote", {"event_id": e, "choices": kit.choices_for(22, b)})
    msg = "Voting is closed for this event. Your ballot was not recorded." in text_of(form)
    n_ballots = kit.q("SELECT COUNT(*) FROM ballots WHERE event_id = %s", (e,))[0][0]
    actual = (f"button '{label}' -> confirmation '{title} {text}'; DB: manual={manual}, end time set to now: {ended_now}; "
              f"server log line 'VOTING ENDED by admin ... for event {e}': {logged}; admin page shows 'Voting has ended': "
              f"{'voting has ended' in head.lower()}, reopen button: {bool(reopen)}; ballot has candidates: {has_choices}; form vote -> "
              f"message shown: {msg}; RPC -> error {rpc.get('error', {}).get('code')} '{rpc.get('error', {}).get('message', '')}'; "
              f"TEST-0021 voted before: {'result' in first}; ballots in the event: {n_ballots}")
    return (label == "End voting now" and title == "End voting now?" and
            text == "Students will no longer be able to vote. This cannot be undone." and manual == "closed" and ended_now
            and logged and "voting has ended" in head.lower() and not reopen and not has_choices and msg
            and rpc.get("error", {}).get("code") == -32010 and "result" in first and n_ballots == 1), actual


@case("TC-18", "Vote submitted after admin ended voting is rejected; no duplicate or partial ballots",
      "60 students of 'TEST Race Election' log in; all 60 send their vote at the same instant and, while the votes are "
      "being saved, the admin ends voting (RPC close_voting). Afterwards 5 of the rejected students try again.",
      "Every vote is either fully counted or cleanly rejected ('Voting is closed'); ballots stored = votes accepted, 0 "
      "duplicates, every stored ballot has all its vote rows; every vote sent after the close is rejected.")
def tc18(S):
    e, b = S["e18"], S["e18_ballot"]
    idx = list(range(21, 81))
    clients, _ = kit.login_many(idx)
    progress, out = [0], {}
    th = threading.Thread(target=lambda: out.update(zip(("res", "secs"), kit.vote_burst(clients, e, b, 60, progress))))
    th.start()
    wait_until(lambda: progress[0] >= 8, 20, 0.01)
    closed = admin_rpc(S).rpc("close_voting", {"event_id": e})
    sent_at_close = progress[0]
    th.join()
    res = out["res"]
    acc = [r for r in res if r[1]]
    rej = [r for r in res if not r[1]]
    clean_rej = all((r[3] or "").startswith("-32010 ") for r in rej)
    stored = dict(kit.q("""SELECT b.student_id, COUNT(v.id) FROM ballots b LEFT JOIN votes v ON v.ballot_id = b.id
                            WHERE b.event_id = %s GROUP BY b.student_id""", (e,)))
    n, distinct = kit.q("SELECT COUNT(*), COUNT(DISTINCT student_id) FROM ballots WHERE event_id = %s", (e,))[0]
    flags = kit.q("SELECT COUNT(*) FROM event_voters WHERE event_id = %s AND has_voted", (e,))[0][0]

    def picks(i):
        return sum(len(v) if isinstance(v, list) else (0 if v is None else 1) for v in kit.choices_for(i, b).values())
    complete = all(stored.get(S["ids"][kit.voter_id(i)]) == picks(i) for i, ok, _, _ in acc)
    later = [i for i, ok, _, _ in rej][:5]
    again = [clients[i].rpc("cast_vote", {"event_id": e, "choices": kit.choices_for(i, b)}) for i in later]
    again_rejected = all(r.get("error", {}).get("code") == -32010 for r in again)
    n2 = kit.q("SELECT COUNT(*) FROM ballots WHERE event_id = %s", (e,))[0][0]
    actual = (f"close_voting answered {'ok' if 'result' in closed else closed.get('error')} when {sent_at_close} of 60 votes "
              f"had been sent; accepted {len(acc)}, rejected {len(rej)} (all with -32010 'Voting is closed': {clean_rej}); "
              f"DB: {n} ballots, {n - distinct} duplicates, has_voted flags {flags}, every stored ballot complete: {complete}; "
              f"{len(again)} votes sent after the close -> all rejected: {again_rejected}; ballots still {n2}")
    return ("result" in closed and len(acc) + len(rej) == 60 and clean_rej and n == len(acc) == flags and n == distinct
            and complete and again_rejected and n2 == n and len(again) > 0), actual


@case("TC-19", "Winners appear after ending voting according to the auto-publish setting",
      "(A) 'TEST Auto-Publish Election' (auto-publish ON): 10 students vote; a student keeps its live results page open; "
      "the admin ends voting. (B) 'TEST End-Early Election' (auto-publish OFF, ended in TC-17): a student opens its results "
      "page; then the admin clicks 'Publish official results'.",
      "(A) right after ending: results official, the open page shows the winners by itself (no reload by the student). "
      "(B) before publishing: no numbers ('announced soon'), RPC refuses (-32021); after publishing the open page shows "
      "the winners by itself within a few seconds.")
def tc19(S):
    e, b = S["e19"], S["e19_ballot"]
    clients, _ = kit.login_many(range(21, 31))
    clear = {str(b[0]["id"]): b[0]["candidates"][0], str(b[1]["id"]): b[1]["candidates"][0], str(b[2]["id"]): b[2]["candidates"][:3]}
    voted = sum(1 for c in clients.values() if "result" in c.rpc("cast_vote", {"event_id": e, "choices": clear}))
    sp = page_login(S, kit.voter_id(31), kit.STUDENT_PASSWORD)
    sp.goto(f"{kit.BASE}/e/{e}/results")
    sp.wait_for_timeout(2500)
    live_before = sp.locator(".winners-announce").count() == 0
    admin_rpc(S).rpc("close_voting", {"event_id": e})
    t0 = time.time()
    try:
        sp.wait_for_selector(".winners-announce", timeout=20000)
        t_a = round(time.time() - t0, 1)
    except Exception:                                   # noqa: BLE001
        t_a = None
    st = kit.Client()
    st.login(kit.voter_id(32), kit.STUDENT_PASSWORD)
    st.fetch_rpc_token()
    ra = st.rpc("get_results", {"event_id": e}).get("result", {})
    winners_a = sum(1 for p in ra.get("positions", []) for c in p["candidates"] if c.get("lead") == "win")
    sp.context.close()
    # (B) auto-publish OFF
    e2 = S["e17"]
    rb_before = st.rpc("get_results", {"event_id": e2})
    pend_page = st.get(f"/e/{e2}/results")
    pending_ok = "announced by the election committee soon" in text_of(pend_page) and "TEST Candidate" not in pend_page.text
    sp2 = page_login(S, kit.voter_id(33), kit.STUDENT_PASSWORD)
    sp2.goto(f"{kit.BASE}/e/{e2}/results")
    sp2.wait_for_timeout(1500)
    adm = kit.Client()
    adm.login("admin", kit.ADMIN_PASSWORD)
    page = adm.get(f"/admin?event={e2}")
    pub = adm.post("/admin/results/publish", {"csrf": adm.csrf_in(page.text), "event_id": str(e2), "tab": "overview"})
    t1 = time.time()
    try:
        sp2.wait_for_selector(".winners-announce", timeout=20000)
        t_b = round(time.time() - t1, 1)
    except Exception:                                   # noqa: BLE001
        t_b = None
    sp2.context.close()
    rb_after = st.rpc("get_results", {"event_id": e2})
    actual = (f"(A) {voted} votes cast; auto-publish ON: open live page showed the winners {t_a} s after the admin ended voting (no reload by "
              f"the student); RPC get_results: final={ra.get('final')}, {winners_a} winner marks. (B) auto-publish OFF: before "
              f"publishing RPC -> error {rb_before.get('error', {}).get('code')}, page shows 'announced soon' without numbers: "
              f"{pending_ok}; after 'Publish official results' (HTTP {pub.status_code}) the open page showed the winners after "
              f"{t_b} s; RPC now final={rb_after.get('result', {}).get('final')}")
    return (live_before and t_a is not None and t_a <= 10 and ra.get("final") is True and winners_a > 0
            and rb_before.get("error", {}).get("code") == -32021 and pending_ok and t_b is not None and t_b <= 10
            and rb_after.get("result", {}).get("final") is True), actual


@case("TC-20", "Admin dashboard can switch between multiple events and shows the correct results for each",
      "Admin logs in (dashboard). The 'Results by event' selector lists all events with their status. The admin switches "
      "to a finished event, an upcoming event and the main open event; then presses Back and reloads. Also checked on a "
      "phone-size screen.",
      "Default = an open event; every switch updates the results/turnout without a full reload and shows that event's own "
      "numbers (= database); the upcoming event shows 'Voting has not started yet'; the URL keeps ?results=<id> (Back and "
      "refresh keep it); live updates follow only the selected event; the Parallel processing panel is not duplicated; "
      "no sideways scroll on a phone.")
def tc20(S):
    p = page_login(S, "admin", kit.ADMIN_PASSWORD)
    p.wait_for_timeout(1500)
    sel = "#dash-results-select"
    options = p.eval_on_selector_all(sel + " option", "os => os.map(o => [o.value, o.textContent.trim()])")
    n_events = kit.q("SELECT COUNT(*) FROM events")[0][0]
    status_of = dict((str(i), s) for i, s in kit.q(f"SELECT e.id, {app_status_sql()} FROM events e"))
    default = p.input_value(sel)
    labelled = all(any(w in t for w in ("Open", "Not started", "Closed")) for _, t in options)
    p.evaluate("window.__noReload = 1")

    def shown_ballots():
        t = p.text_content(".dash-results [data-r='ballots']") if p.locator(".dash-results [data-r='ballots']").count() else ""
        return (t or "").strip()

    checks = []
    for key in ("e17", "upcoming", "main"):
        eid = str(S[key])
        p.select_option(sel, eid)
        ok_switch = wait_until(lambda: p.get_attribute(".dash-results [data-dash-event]", "data-dash-event") == eid, 10) is not None
        if key == "upcoming":
            shown = "Voting has not started yet" in p.inner_text(".dash-results")
            checks.append((key, ok_switch and shown, "empty state 'Voting has not started yet'" if shown else "no empty state"))
        else:
            want = str(kit.q("SELECT COUNT(*) FROM ballots WHERE event_id = %s", (S[key],))[0][0])
            got = wait_until(lambda: shown_ballots() == want, 12) is not None
            checks.append((key, ok_switch and got, f"votes cast shown {shown_ballots()} = database {want}"))
        checks[-1] = checks[-1] + (f"results={eid}" in p.url,)
    # live updates follow only the selected event (main): watch the RPC calls for 5 s
    calls = []
    p.on("request", lambda r: calls.append(r.post_data or "") if r.url.endswith("/rpc") and "get_results" in (r.post_data or "") else None)
    p.wait_for_timeout(5000)
    only_main = bool(calls) and all(f'"event_id":{S["main"]}' in c.replace(" ", "") for c in calls)
    no_reload = p.evaluate("window.__noReload === 1")
    p.go_back()
    back_ok = wait_until(lambda: p.get_attribute(".dash-results [data-dash-event]", "data-dash-event") == str(S["upcoming"]), 10) is not None
    p.reload()
    p.wait_for_timeout(1500)
    reload_ok = p.input_value(sel) == str(S["upcoming"])
    one_panel = p.locator("#panel-overview [data-r='parallel']").count() == 0 and p.locator("#panel-results [data-r='parallel']").count() == 1
    p.context.close()
    ph = page_login(S, "admin", kit.ADMIN_PASSWORD, 390, 844, mobile=True)
    ph.wait_for_timeout(1500)
    phone_ok = ph.locator(sel).is_visible() and not ph.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
    ph.context.close()
    actual = (f"selector lists {len(options)} of {n_events} events, each labelled with its status: {labelled}; default "
              f"selection: event {default} ({status_of.get(default)}); "
              + "; ".join(f"{k}: switched + correct: {ok} ({d}), in URL: {u}" for k, ok, d, u in checks)
              + f"; no full page reload: {no_reload}; live updates only for the selected event: {only_main} ({len(calls)} "
              f"calls in 5 s); Back -> previous selection: {back_ok}; refresh keeps it: {reload_ok}; Parallel processing "
              f"panel only in the Results tab: {one_panel}; phone 390 px: selector visible, no sideways scroll: {phone_ok}")
    return (len(options) == n_events and labelled and status_of.get(default) == "current" and all(c[1] and c[3] for c in checks)
            and no_reload and only_main and back_ok and reload_ok and one_panel and phone_ok), actual


def app_status_sql():
    return """CASE WHEN e.manual = 'closed' THEN 'previous' WHEN e.manual = 'open' THEN 'current'
                   WHEN e.starts_at IS NULL OR now() < e.starts_at THEN 'upcoming'
                   WHEN e.ends_at IS NOT NULL AND now() >= e.ends_at THEN 'previous' ELSE 'current' END"""


# ============================================================================ run + report
def write_xlsx(rows, path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Test Cases"
    head = ["Test ID", "Test Case", "Input/Steps", "Expected Result", "Actual Result", "Pass/Fail", "Date Tested", "Notes"]
    ws.append(head)
    for r in rows:
        ws.append([r["id"], r["title"], r["steps"], r["expected"], r["actual"], r["result"], r["date"], r["note"]])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F2A44")
    widths = [9, 30, 55, 45, 70, 10, 13, 40]
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        res = row[5]
        res.font = Font(bold=True, color="FFFFFF")
        res.fill = PatternFill("solid", fgColor={"PASS": "15803D", "MANUAL": "6B7280"}.get(res.value, "B91C1C"))
    ws.freeze_panes = "A2"
    wb.save(path)


def main():
    kit.EVIDENCE.mkdir(exist_ok=True)
    kit.RAW.mkdir(exist_ok=True)
    print(f"Test database: {kit.TEST_DB} (port {kit.PG_PORT}); app under test: {kit.BASE}")
    kit.reset_db()
    server = kit.Server(log_name="server-test-cases.log").start()
    print(server.banner())
    S = kit.seed(200, extra_events=True)
    S["server"] = server
    setup_defense_events(S)
    rows = []
    order = [c for c in CASES if c["id"] != "TC-14"] + [c for c in CASES if c["id"] == "TC-14"]   # TC-14 stops the server
    try:
        for c in order:
            t0 = time.time()
            try:
                passed, actual = c["fn"](S)
                result = "PASS" if passed else "FAIL"
                note = "" if passed else "Did not meet the expected result (see Actual Result)."
            except Manual as m:
                result, actual, note = "MANUAL", f"Not run automatically: {m}", "Run this case by hand in a browser."
            except Exception as e:                  # noqa: BLE001 - a crash is a FAIL, recorded as such
                result, actual = "FAIL", f"ERROR while running: {type(e).__name__}: {e}"
                note = traceback.format_exc(limit=2).splitlines()[-1]
            rows.append({"id": c["id"], "title": c["title"], "steps": c["steps"], "expected": c["expected"],
                         "actual": actual, "result": result, "date": TODAY, "note": note,
                         "seconds": round(time.time() - t0, 2)})
            print(f"{c['id']} {result}  {c['title']}\n      {actual}")
    finally:
        if "br" in S:
            S["br"].close()
            S["pw"].stop()
        server.stop()
    rows.sort(key=lambda r: int(r["id"].split("-")[1]))
    if S.get("js_errors"):
        print("JavaScript errors seen in the browser:", S["js_errors"][:5])
    write_xlsx(rows, kit.EVIDENCE / "Test_Cases.xlsx")
    (kit.RAW / "test_cases.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    n_pass = sum(1 for r in rows if r["result"] == "PASS")
    print(f"\n{n_pass}/{len(rows)} passed -> {kit.EVIDENCE / 'Test_Cases.xlsx'}")
    return 0 if n_pass == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
