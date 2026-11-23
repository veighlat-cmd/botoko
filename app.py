"""BotoKo: A Concurrent Online Voting System for Student Council Elections
with Real-Time Tallying.

How the parallel / distributed parts work
-----------------------------------------
* Distributed: every student votes from their own phone/laptop (client).
  All of them talk over the internet to this one web server + one database.
* Parallel: gunicorn runs several worker PROCESSES, each with several
  THREADS, so many students' requests are processed at the same time.
* Concurrency control (locking): cast_vote() runs inside a database
  transaction and locks the student's row with SELECT ... FOR UPDATE.
  If the same student submits twice at the same instant, the second
  request waits, then sees has_voted = TRUE and is rejected. UNIQUE
  constraints on ballots/votes are a second safety net.
"""
import csv
import hmac
import io
import logging
import os
import platform
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from functools import wraps

import psutil          # CPU cores, RAM and per-process CPU/memory for the background monitor
import psycopg
import segno           # QR code on the voting receipt (pure Python, no other dependencies)
from flask import (Flask, Response, abort, flash, g, get_template_attribute, has_request_context, jsonify,
                   make_response, redirect, render_template, request, session, url_for)
from flask.sessions import SecureCookieSessionInterface
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import BadRequest
from werkzeug.security import check_password_hash, generate_password_hash

import concurrency      # worker processes / threads from the CPU cores (concurrency.py)
import db
import receipt_image    # the receipt as a PNG / JPG image (Pillow, drawn on the server)
from secret_key import load_secret_key   # the SAME SECRET_KEY across restarts (.secret_key)
import students as st   # Voter IDs, school student numbers, class-list import (students.py)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [pid %(process)d %(threadName)s] %(message)s")
log = logging.getLogger("botoko")

app = Flask(__name__)
# The SAME key across restarts and worker processes (SECRET_KEY, else the .secret_key file): see secret_key.py.
app.secret_key, SECRET_KEY_SOURCE = load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 4,
    # CSS/JS/icons are requested with ?v=<file time> (see asset() below), so phones
    # can safely cache them for a week and don't re-download them on slow data.
    SEND_FILE_MAX_AGE_DEFAULT=60 * 60 * 24 * 7,
    # No request may be bigger than 1 MB (photos are shrunk to ~50 KB in the browser).
    MAX_CONTENT_LENGTH=1024 * 1024,
)
log.info("SECRET_KEY from %s", SECRET_KEY_SOURCE)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
PASSWORD_METHOD = "pbkdf2:sha256:20000"   # fast enough for a small shared CPU

# The admin logs in on the SAME login page as the students: Voter ID "admin" + ADMIN_PASSWORD.
# Nothing on the public pages links to the admin dashboard.
ADMIN_USERNAME = "admin"
ADMIN_IDLE_MINUTES = max(1, int(os.environ.get("ADMIN_IDLE_MINUTES", "30") or 30))   # logged out after 30 min idle
ADMIN_LOCK_TRIES = 5                 # 5 wrong admin passwords from one IP address ...
ADMIN_LOCK_MINUTES = 15              # ... block admin login from that IP for 15 minutes
WEAK_ADMIN_PASSWORDS = {"admin", "admin123", "admin1234", "admin12345", "administrator", "password", "password1",
                        "password123", "12345678", "123456789", "1234567890", "qwerty123", "botoko", "botoko123",
                        "botoko2026", "comelec", "comelec123", "comelec2026", "letmein123"}


def admin_password_is_weak(password=None):
    pw = ADMIN_PASSWORD if password is None else password
    return bool(pw) and (len(pw) < 10 or pw.lower() in WEAK_ADMIN_PASSWORDS)


ADMIN_PASSWORD_WEAK = admin_password_is_weak()
WEAK_ADMIN_WARNING = (
    "WARNING: Change the admin password before the real election. The current ADMIN_PASSWORD is weak "
    "(a common password like admin123, or shorter than 10 characters).\n"
    "  This laptop: in run-local.ps1 change the line  $env:ADMIN_PASSWORD = \"admin123\"  to a long password,\n"
    "               or type  $env:ADMIN_PASSWORD = \"your-long-password\"  in the terminal before .\\run-local.ps1\n"
    "  Render:      Dashboard > botoko service > Environment > ADMIN_PASSWORD > Save (the app restarts).")

_schema_ready = False
_schema_lock = threading.Lock()


@app.before_request
def ensure_schema():
    global _schema_ready
    if not _schema_ready:
        with _schema_lock:
            if not _schema_ready:
                db.init_schema()
                _schema_ready = True


# ---------------------------------------------------------------- parallel processing monitor
# Runs in the BACKGROUND only (no panel in the interface). Each worker PROCESS counts its own
# requests and busy threads. Worker processes don't share memory, so every worker writes a small
# "heartbeat" row (its PID, busy threads, CPU, memory...) to the worker_stats table every 2 seconds.
# Admins can read everything as JSON at /admin/api/system (e.g. during the defense).

PLAN = concurrency.plan()               # 1 worker process per physical CPU core, see concurrency.py
WORKERS_CONFIGURED = PLAN["workers"]
THREADS_CONFIGURED = PLAN["threads"]
POOL_MAX = PLAN["pool"]
HEARTBEAT_SECONDS = 2
HOSTNAME = socket.gethostname()

_load = {"busy": 0, "handled": 0, "peak": 0}   # this worker process only (peak = most at once)
_load_lock = threading.Lock()          # threads of the same process share _load, so lock it
_heartbeat = {"pid": None}
_heartbeat_lock = threading.Lock()


def server_kind():
    """Which server runs this process: "gunicorn" (Render), "waitress" (serve.py / run-local.ps1 on
    Windows) or "flask-dev" (py app.py). gunicorn.conf.py and serve.py set BOTOKO_SERVER."""
    kind = os.environ.get("BOTOKO_SERVER")
    if kind:
        return kind
    if "gunicorn" in sys.modules:
        return "gunicorn"
    if "waitress" in sys.modules:
        return "waitress"
    return "flask-dev"


def multi_process():
    return server_kind() in ("gunicorn", "waitress")


def threads_per_worker():
    """Threads that can serve requests at the same time in one worker (0 = dev server: unlimited)."""
    return THREADS_CONFIGURED if multi_process() else 0


@app.before_request
def count_request_start():
    with _load_lock:
        _load["busy"] += 1
        _load["peak"] = max(_load["peak"], _load["busy"])
    g.counted = True
    start_heartbeat()


@app.teardown_request
def count_request_end(exc):
    if g.pop("counted", False):         # runs even if the request failed
        with _load_lock:
            _load["busy"] -= 1
            _load["handled"] += 1


def worker_snapshot(proc):
    """This worker's live numbers."""
    with _load_lock:
        busy, handled, peak = _load["busy"], _load["handled"], _load["peak"]
    try:
        stats = db.get_pool().get_stats()
        pool_in_use = stats.get("pool_size", 0) - stats.get("pool_available", 0)
    except Exception:
        pool_in_use = 0
    return {"pid": os.getpid(), "hostname": HOSTNAME, "threads_busy": busy, "threads_peak": peak,
            "threads_total": threads_per_worker(), "requests_handled": handled,
            "cpu_percent": round(proc.cpu_percent(interval=None), 1),
            "memory_mb": round(proc.memory_info().rss / 2**20, 1),
            "pool_in_use": pool_in_use, "pool_max": POOL_MAX}


def start_heartbeat():
    """Start this worker's heartbeat thread once (on its first request)."""
    pid = os.getpid()
    if _heartbeat["pid"] == pid:
        return
    with _heartbeat_lock:
        if _heartbeat["pid"] != pid:
            _heartbeat["pid"] = pid
            threading.Thread(target=_heartbeat_loop, name="heartbeat", daemon=True).start()


def _heartbeat_loop():
    """Every 2 s: write this worker's numbers to worker_stats. Uses its OWN connection,
    so it never takes a pool connection away from a student who is voting."""
    proc = psutil.Process()
    proc.cpu_percent(interval=None)          # first call only starts the measurement
    conn, beats = None, 0
    while True:
        time.sleep(HEARTBEAT_SECONDS)
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, connect_timeout=5)
            s = worker_snapshot(proc)
            conn.execute(
                """INSERT INTO worker_stats (hostname, pid, threads_busy, threads_peak, threads_total, requests_handled,
                                             cpu_percent, memory_mb, pool_in_use, pool_max, updated_at)
                   VALUES (%(hostname)s, %(pid)s, %(threads_busy)s, %(threads_peak)s, %(threads_total)s, %(requests_handled)s,
                           %(cpu_percent)s, %(memory_mb)s, %(pool_in_use)s, %(pool_max)s, now())
                   ON CONFLICT (hostname, pid) DO UPDATE SET
                       threads_busy = EXCLUDED.threads_busy, threads_peak = EXCLUDED.threads_peak,
                       threads_total = EXCLUDED.threads_total,
                       requests_handled = EXCLUDED.requests_handled, cpu_percent = EXCLUDED.cpu_percent,
                       memory_mb = EXCLUDED.memory_mb, pool_in_use = EXCLUDED.pool_in_use,
                       pool_max = EXCLUDED.pool_max, updated_at = now()""", s)
            beats += 1
            if beats % 900 == 1:             # now and then, forget workers that stopped long ago
                conn.execute("DELETE FROM worker_stats WHERE updated_at < now() - interval '1 hour'")
        except Exception as e:               # never crash the worker because of the monitor
            log.warning("heartbeat failed: %s", e)
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            conn = None


# ---------------------------------------------------------------- helpers

def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


# ---------------------------------------------------------------- behind an HTTPS tunnel on this laptop (cloudflared)
# A tunnel such as  cloudflared tunnel --url http://127.0.0.1:5000  runs ON this laptop, so its requests reach the app
# from 127.0.0.1 / ::1 and carry X-Forwarded-For (the real visitor) and X-Forwarded-Proto: https. ONLY for those
# requests the headers are trusted, for exactly ONE proxy hop (werkzeug ProxyFix): the per-IP rate limits and the admin
# lockout see the real visitor's IP, and links / QR codes use https://<tunnel address>. Direct visitors
# (http://localhost:5000, http://<laptop IP>:5000) are not proxied: their X-Forwarded-* headers are ignored, exactly as
# before. (On Render the proxy is not on the same machine: TRUST_PROXY + client_ip() handle it, unchanged.)
class LocalTunnelProxyFix:
    LOOPBACK = {"127.0.0.1", "::1"}

    def __init__(self, wsgi_app):
        self.plain = wsgi_app
        self.proxied = ProxyFix(wsgi_app, x_for=1, x_proto=1, x_host=1)

    def __call__(self, environ, start_response):
        if environ.get("REMOTE_ADDR") in self.LOOPBACK and environ.get("HTTP_X_FORWARDED_FOR"):
            return self.proxied(environ, start_response)
        return self.plain(environ, start_response)


if os.environ.get("TRUST_LOCAL_PROXY", "1") == "1":
    app.wsgi_app = LocalTunnelProxyFix(app.wsgi_app)


class BotoKoSessionInterface(SecureCookieSessionInterface):
    """The login cookie is "Secure" (sent over HTTPS only) on HTTPS requests, e.g. through the tunnel, and as
    configured (COOKIE_SECURE) otherwise, so http://localhost:5000 and http://<laptop IP>:5000 keep working."""

    def get_cookie_secure(self, app):
        return bool(app.config["SESSION_COOKIE_SECURE"] or (has_request_context() and request.is_secure))


app.session_interface = BotoKoSessionInterface()


def asset(filename):
    """URL of a static file with its modification time attached, e.g. style.css?v=1712345678.
    When the file changes the URL changes, so browsers never use an old cached copy."""
    try:
        version = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
    except OSError:
        version = 0
    return url_for("static", filename=filename, v=version)


app.jinja_env.globals["asset"] = asset


@app.template_filter("initials")
def initials(name):
    """'Andrea Villanueva' -> 'AV' (used for the candidate avatar circles)."""
    words = [w for w in str(name).replace(".", " ").split() if w[:1].isalnum()]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[-1][0]).upper()


@app.template_filter("tone")
def tone(text):
    """Pick one of 6 avatar colours from a party/candidate name (same formula as static/app.js)."""
    return sum(ord(ch) for ch in str(text)) % 6


class CsrfError(BadRequest):
    """The form's CSRF token is missing or isn't this session's: the page was open too long, the login changed
    in another tab of this browser, or the request is forged. The form is NEVER processed; csrf_failed()
    sends the person back with a friendly message and a fresh token."""
    description = "Your session expired. Please go back and try again."


def check_csrf():
    sent = request.form.get("csrf", "")
    if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
        raise CsrfError()


def who_am_i():
    """Who is logged in in THIS browser: ("admin", "the admin"), ("student:V-7KQ3M9", "Juan Dela Cruz (V-7KQ3M9)")
    or ("", "") = nobody. Shown on every page (<meta name="botoko-who">) so other tabs notice a login change."""
    if session.get("is_admin"):
        return "admin", "the admin"
    if session.get("student_id"):
        return "student:" + str(session.get("student_no", "")), f"{session.get('name', '')} ({session.get('student_no', '')})"
    return "", ""


app.jinja_env.globals["who_am_i"] = who_am_i


app.jinja_env.globals.update(ADMIN_PASSWORD_WEAK=ADMIN_PASSWORD_WEAK, ADMIN_IDLE_MINUTES=ADMIN_IDLE_MINUTES)
app.jinja_env.globals.update(VOTER_ID_EXAMPLE=st.VOTER_ID_EXAMPLE, SCHOOL_NO_EXAMPLE=st.SCHOOL_NO_EXAMPLE,
                             SCHOOL_NO_PATTERN=st.SCHOOL_NO_PATTERN, MAX_YEAR=st.MAX_YEAR)
app.jinja_env.filters["group"] = lambda s: st.group_label(s["course"], s["year_level"], s["section"])

STUDENT_COLUMNS = ["id", "student_no", "full_name", "school_student_no", "course", "department",
                   "year_level", "section", "has_voted", "voted_at", "session_version", "password_generated",
                   "account_status", "account_reason", "email"]


def load_student(student_id):
    """One student's account row as a dict (student_no = their Voter ID), or None.
    Also includes require_password_change (the admin setting), in the same query."""
    row = db.query_one(
        f"""SELECT {', '.join(STUDENT_COLUMNS)},
                   (SELECT require_password_change FROM election WHERE id = 1)
              FROM students WHERE id = %s""", (student_id,))
    return dict(zip(STUDENT_COLUMNS + ["require_password_change"], row)) if row else None


def start_student_session(student):
    """Log a student in on THIS device (also used after a password change, to rotate the session).
    Remembers the account's session_version: when it changes, other devices are logged out."""
    session.clear()
    session.permanent = True
    session.update(student_id=student["id"], student_no=student["student_no"], name=student["full_name"],
                   group=st.group_label(student["course"], student["year_level"], student["section"]),
                   sv=student["session_version"], csrf=secrets.token_hex(16))


def load_session_student():
    """Load the logged-in student's row into g.student. Returns a redirect if the account is gone
    or its password was changed elsewhere (that logs out every other device), else None."""
    g.student = load_student(session["student_id"])
    if g.student is None:                       # account was deleted
        session.clear()
        return redirect(url_for("login"))
    if session.get("sv", 0) != g.student["session_version"]:
        # The password was changed (by the student elsewhere, or reset by COMELEC).
        session.clear()
        flash("You were logged out because your password was changed. Please log in again.", "error")
        return redirect(url_for("login"))
    group = st.group_label(g.student["course"], g.student["year_level"], g.student["section"])
    if session.get("name") != g.student["full_name"] or session.get("group") != group:
        session.update(name=g.student["full_name"], group=group)    # admin edited the details
    return None


def login_url():
    """The login page (the first page), coming back to THIS page after logging in."""
    here = request.full_path.rstrip("?")
    return url_for("login", next=here) if here not in ("/", "/login") else url_for("login")


def login_required(view):
    """Account pages (My Account, profile, history, password): logged in as a student. A student whose
    account is still pending verification (or rejected) may open these, but can't vote."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if session.get("is_admin"):
            return redirect(url_for("admin"))
        if "student_id" not in session:
            return redirect(login_url())
        problem = load_session_student()
        if problem is not None:
            return problem
        return view(*args, **kwargs)
    return wrapper


def verified_required(allow_admin=False):
    """Every student page: logged in (else to the login page, then back here) with a VERIFIED account
    (pending / rejected accounts go to My Account, which explains why). allow_admin: the COMELEC admin
    may open the page too (results, projector view, upcoming info)."""
    def decorate(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if session.get("is_admin"):
                return view(*args, **kwargs) if allow_admin else redirect(url_for("admin"))
            if "student_id" not in session:
                return redirect(login_url())
            problem = load_session_student()
            if problem is not None:
                return problem
            if g.student["account_status"] != "verified":
                return redirect(url_for("my_account"))
            return view(*args, **kwargs)
        return wrapper
    return decorate


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def default_school_year():
    """e.g. "2026-2027" (a Philippine school year starts around June)."""
    t = time.localtime()
    start = t.tm_year if t.tm_mon >= 6 else t.tm_year - 1
    return f"{start}-{start + 1}"


def election_state():
    """Site-wide state for pages that don't belong to one event (event list, account pages,
    verify, admin login). The old single "election" row now only holds global settings."""
    row = db.query_one("SELECT require_password_change FROM election WHERE id = 1")
    return {"title": "BotoKo voting events", "is_open": False, "require_password_change": row[0],
            "school_name": "", "school_year_setting": "", "school_year": default_school_year(),
            "status": None, "id": None}


# ---------------------------------------------------------------- voting events
# Status is computed from the dates, unless the admin opened/closed the event by hand:
#   manual 'closed' -> PREVIOUS, manual 'open' -> CURRENT,
#   manual 'auto'   -> before starts_at UPCOMING, after ends_at PREVIOUS, otherwise CURRENT.
EVENT_STATUS_SQL = """CASE WHEN e.manual = 'closed' THEN 'previous'
                           WHEN e.manual = 'open' THEN 'current'
                           WHEN e.starts_at IS NULL OR now() < e.starts_at THEN 'upcoming'
                           WHEN e.ends_at IS NOT NULL AND now() >= e.ends_at THEN 'previous'
                           ELSE 'current' END"""
STATUS_LABELS = {"current": "Current", "upcoming": "Upcoming", "previous": "Previous"}
_MNL = "AT TIME ZONE 'Asia/Manila'"
_SHOW = "'Mon DD, YYYY  HH12:MI AM'"
_INPUT = "'YYYY-MM-DD\"T\"HH24:MI'"


class EventNotFound(Exception):
    pass


def load_events(event_id=None):
    """Events as dicts (all of them, or just one), with status and display-ready dates."""
    where, params = ("WHERE e.id = %s", (event_id,)) if event_id is not None else ("", ())
    rows = db.query_all(
        f"""SELECT e.id, e.name, e.description, e.organization, e.school_year, e.manual, {EVENT_STATUS_SQL}, e.auto_approve,
                   e.results_published, e.auto_publish, to_char(e.results_published_at {_MNL}, {_SHOW}),
                   (extract(epoch FROM COALESCE(e.results_published_at, e.ends_at)) * 1000)::bigint,
                   to_char(e.starts_at {_MNL}, {_SHOW}), to_char(e.ends_at {_MNL}, {_SHOW}),
                   to_char(e.starts_at {_MNL}, {_INPUT}), to_char(e.ends_at {_MNL}, {_INPUT}),
                   (extract(epoch FROM e.starts_at) * 1000)::bigint, (extract(epoch FROM e.ends_at) * 1000)::bigint,
                   (SELECT COUNT(*) FROM event_voters v WHERE v.event_id = e.id),
                   (SELECT COUNT(*) FROM ballots b WHERE b.event_id = e.id),
                   (SELECT COUNT(*) FROM positions p WHERE p.event_id = e.id)
              FROM events e {where}
             ORDER BY e.starts_at NULLS LAST, e.id""", params)
    out = []
    for (eid, name, desc, org, sy, manual, status, auto_approve, published, auto_publish, published_txt, published_ms,
         s_txt, e_txt, s_in, e_in, s_ms, e_ms, registered, ballots, n_positions) in rows:
        # Final results are OFFICIAL (visible to students) only once COMELEC publishes them, or at once
        # when the event has "Publish results automatically when voting ends".
        official = status == "previous" and bool(published or auto_publish)
        out.append({
            "id": eid, "name": name, "title": name, "description": desc, "organization": org,
            "school_name": org, "school_year_setting": sy, "school_year": sy or default_school_year(),
            "manual": manual, "status": status, "status_label": STATUS_LABELS[status],
            "is_open": status == "current",
            "starts_txt": s_txt, "ends_txt": e_txt, "starts_input": s_in or "", "ends_input": e_in or "",
            "starts_ms": s_ms, "ends_ms": e_ms,
            "registered": int(registered), "ballots": int(ballots), "positions": int(n_positions),
            "auto_approve": bool(auto_approve), "auto_publish": bool(auto_publish),
            "published": official, "results_pending": status == "previous" and not official,
            "published_txt": published_txt if published else (e_txt if official else None),
            "published_ms": published_ms if official else None,
        })
    return out


def event_state(event_id):
    """One event (raises EventNotFound). Has the same keys templates used for the old election
    (title, is_open, school_name, school_year), plus the global password setting."""
    found = load_events(event_id)
    if not found:
        raise EventNotFound(event_id)
    ev = found[0]
    ev["require_password_change"] = db.query_one("SELECT require_password_change FROM election WHERE id = 1")[0]
    return ev


def event_or_404(event_id):
    try:
        return event_state(event_id)
    except EventNotFound:
        abort(404)


def registration(event_id, student_id):
    """(has_voted, voted_at) if the student is registered in the event, else None."""
    return db.query_one("SELECT has_voted, voted_at FROM event_voters WHERE event_id = %s AND student_id = %s",
                        (event_id, student_id))


def student_events(student_id):
    """Every event this student is registered in, with their own voted / not voted status."""
    regs = {eid: (voted, at) for eid, voted, at in db.query_all(
        f"""SELECT event_id, has_voted, to_char(voted_at {_MNL}, {_SHOW})
              FROM event_voters WHERE student_id = %s""", (student_id,))}
    out = []
    for ev in load_events():
        if ev["id"] in regs:
            ev["has_voted"], ev["voted_at"] = regs[ev["id"]]
            out.append(ev)
    return out


# Photo "version" = when it was last changed (microseconds). It goes into the photo URL,
# so a changed photo gets a new URL and phones can cache each URL forever.
PHOTO_VERSION_SQL = "(extract(epoch FROM ph.updated_at) * 1000000)::bigint"


def photo_url(candidate_id, version):
    """URL of a candidate's photo, or None if they have no photo (show initials instead)."""
    if version is None:
        return None
    return url_for("photo", candidate_id=candidate_id, v=version)


def background_items(text):
    """Candidate background text (one achievement per line) -> list of non-empty lines."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def load_ballot(event_id):
    """One event's positions with their candidates (and card details), in display order.
    seats = how many candidates a voter may choose for that position (1 = classic single choice)."""
    rows = db.query_all(
        f"""SELECT p.id, p.name, p.seats, c.id, c.full_name, c.party, {PHOTO_VERSION_SQL},
                   c.course_year, c.motto, c.background
             FROM positions p
             LEFT JOIN candidates c ON c.position_id = p.id
             LEFT JOIN candidate_photos ph ON ph.candidate_id = c.id
            WHERE p.event_id = %s
            ORDER BY p.sort_order, p.id, c.id""", (event_id,))
    positions = []
    for pid, pname, seats, cid, cname, party, photo_v, course_year, motto, background in rows:
        if not positions or positions[-1]["id"] != pid:
            positions.append({"id": pid, "name": pname, "seats": seats, "candidates": []})
        if cid is not None:
            positions[-1]["candidates"].append(
                {"id": cid, "name": cname, "party": party, "photo": photo_url(cid, photo_v),
                 "course_year": course_year, "motto": motto, "background": background_items(background)})
    return [p for p in positions if p["candidates"]]


class VoteError(Exception):
    pass


def cast_vote(event_id, student_id, choices):
    """Record one ballot for one event, atomically.

    choices = {position_id: [candidate_id, ...]} (an empty list or a missing position = skipped).
    Returns (receipt_code, worker_pid, thread_name). Raises VoteError.
    """
    worker_pid = os.getpid()
    thread_name = threading.current_thread().name
    receipt = secrets.token_hex(4).upper()

    with db.get_pool().connection() as conn:
        with conn.transaction():                       # BEGIN ... COMMIT (or ROLLBACK on error)
            # The event's lock, SHARED: ballots never wait for each other here, but "End voting now"
            # (set_event_mode, EXCLUSIVE) waits until the ballots being saved right now are committed, and
            # ballots that arrive after it read the new status below: fully counted or cleanly rejected.
            conn.execute("SELECT pg_advisory_xact_lock_shared(%s::int, %s::int)", (EVENT_LOCK, event_id))
            row = conn.execute(f"SELECT {EVENT_STATUS_SQL} FROM events e WHERE e.id = %s", (event_id,)).fetchone()
            if row is None:
                raise VoteError("This voting event doesn't exist.")
            if row[0] == "upcoming":
                raise VoteError("Voting has not started yet for this event. Your ballot was not recorded.")
            if row[0] != "current":
                raise VoteError("Voting is closed for this event. Your ballot was not recorded.")

            # LOCK: only one request per (event, student) gets past this line at a time.
            # Another event's registration row is not locked: voting in Event A never blocks Event B.
            row = conn.execute(
                """SELECT has_voted FROM event_voters
                    WHERE event_id = %s AND student_id = %s FOR UPDATE""", (event_id, student_id)).fetchone()
            if row is None:
                raise VoteError("You are not registered for this voting event.")
            if row[0]:
                raise VoteError("You have already voted.")

            # Validate. Voting is OPTIONAL per position (0 picks = skipped, even a blank ballot).
            # A position may elect several people (seats): at most `seats` picks, no duplicates,
            # and every candidate must exist, run for that position, and belong to THIS event.
            seats = dict(conn.execute("SELECT id, seats FROM positions WHERE event_id = %s",
                                      (event_id,)).fetchall())
            valid = dict(conn.execute(
                """SELECT c.id, c.position_id FROM candidates c
                     JOIN positions p ON p.id = c.position_id WHERE p.event_id = %s""", (event_id,)).fetchall())
            for pid, picked in choices.items():
                if pid not in seats:
                    raise VoteError("Invalid position selected.")
                if len(picked) != len(set(picked)):
                    raise VoteError("The same candidate was chosen twice.")
                if len(picked) > seats[pid]:
                    raise VoteError(f"Too many candidates chosen for one position (at most {seats[pid]}).")
                for cid in picked:
                    if valid.get(cid) != pid:
                        raise VoteError("Invalid candidate selected.")

            ballot_id = conn.execute(
                """INSERT INTO ballots (event_id, student_id, receipt_code, worker_pid, thread_name)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (event_id, student_id, receipt, worker_pid, thread_name)).fetchone()[0]
            for pid, picked in choices.items():
                for cid in picked:
                    conn.execute(
                        """INSERT INTO votes (ballot_id, student_id, position_id, candidate_id)
                           VALUES (%s, %s, %s, %s)""", (ballot_id, student_id, pid, cid))
            conn.execute("""UPDATE event_voters SET has_voted = TRUE, voted_at = now()
                             WHERE event_id = %s AND student_id = %s""", (event_id, student_id))
    # Leaving the transaction block = COMMIT = locks released.
    return receipt, worker_pid, thread_name


def mark_winners(candidates, seats, final):
    """Top `seats` candidates (by votes) win. Adds c["lead"] = 'win' | 'tie' | '' and c["rank"].
    A tie that crosses the last seat ("tie for the last seat") marks those candidates 'tie'.
    Nobody wins with 0 votes. final=False (voting still open) -> the same marks mean "leading"."""
    ordered = sorted(candidates, key=lambda c: -c["votes"])
    for i, c in enumerate(ordered):
        c["rank"] = 1 + sum(1 for o in ordered if o["votes"] > c["votes"])
        c["lead"] = ""
    if not ordered or ordered[0]["votes"] == 0:
        return {"winners": [], "tie": False}
    cut = ordered[min(seats, len(ordered)) - 1]["votes"]          # votes of the last seat
    above = [c for c in ordered if c["votes"] > cut]
    at_cut = [c for c in ordered if c["votes"] == cut and cut > 0]
    tie = len(above) + len(at_cut) > seats
    for c in above:
        c["lead"] = "win"
    for c in at_cut:
        c["lead"] = "tie" if tie else "win"
    return {"winners": [c["id"] for c in ordered if c["lead"] == "win"], "tie": tie,
            "tie_ids": [c["id"] for c in at_cut] if tie else [], "seats_left": seats - len(above)}


# Live results are cached for 1.5 s per worker AND per event so that many screens
# refreshing at once don't hammer the database.
_results_cache = {}                     # event_id -> (time, data)
_results_lock = threading.Lock()


def compute_results(event_id):
    with _results_lock:
        hit = _results_cache.get(event_id)
        if hit and time.monotonic() - hit[0] < 1.5:
            return hit[1]
        ev = event_state(event_id)
        rows = db.query_all(
            f"""SELECT p.id, p.name, p.seats, c.id, c.full_name, c.party, COUNT(v.id), {PHOTO_VERSION_SQL},
                       (SELECT COUNT(*) FROM ballots WHERE event_id = %s),
                       (SELECT COUNT(DISTINCT b.id) FROM ballots b JOIN votes v2 ON v2.ballot_id = b.id
                         WHERE v2.position_id = p.id)
                 FROM positions p
                 JOIN candidates c ON c.position_id = p.id
                 LEFT JOIN candidate_photos ph ON ph.candidate_id = c.id
                 LEFT JOIN votes v ON v.candidate_id = c.id
                WHERE p.event_id = %s
                GROUP BY p.id, p.name, p.seats, p.sort_order, c.id, c.full_name, c.party, ph.updated_at
                ORDER BY p.sort_order, p.id, c.id""", (event_id, event_id))
        positions = []
        for pid, pname, seats, cid, cname, party, count, photo_v, n_ballots, n_voted_here in rows:
            if not positions or positions[-1]["id"] != pid:
                # Skipped ("no vote") = ballots without any vote for this position. Counted in the
                # same SQL statement as the votes, so both numbers come from one consistent snapshot.
                positions.append({"id": pid, "name": pname, "seats": seats, "total": 0,
                                  "abstain": int(n_ballots) - int(n_voted_here), "candidates": []})
            positions[-1]["candidates"].append(
                {"id": cid, "name": cname, "party": party, "votes": int(count),
                 "photo": photo_url(cid, photo_v)})
            positions[-1]["total"] += int(count)
        final = ev["status"] == "previous"
        for p in positions:
            p.update(mark_winners(p["candidates"], p["seats"], final))
        workers = db.query_all(
            """SELECT worker_pid, COUNT(*), COUNT(DISTINCT thread_name)
                 FROM ballots WHERE event_id = %s GROUP BY worker_pid ORDER BY worker_pid""", (event_id,))
        data = {
            "event_id": event_id,
            "title": ev["name"],
            "description": ev["description"],
            "status": ev["status"],
            "is_open": ev["is_open"],
            "final": final,
            "starts_txt": ev["starts_txt"],
            "ends_txt": ev["ends_txt"],
            "registered": ev["registered"],
            "ballots": ev["ballots"],
            "positions": positions,
            "processed_by": [{"worker_pid": w[0], "ballots": int(w[1]), "threads": int(w[2])}
                             for w in workers],
            "generated_at": time.strftime("%H:%M:%S"),
        }
        _results_cache[event_id] = (time.monotonic(), data)
        return data


def public_results(data):
    """What students and the public get from compute_results(): the tally only. Which worker process and
    thread recorded the ballots ("processed_by") is ADMIN-ONLY: see worker_stats(), /admin/api/workers
    and the admin RPC get_worker_stats."""
    return {k: v for k, v in data.items() if k != "processed_by"}


def worker_stats(event_id):
    """ADMIN-ONLY: ballots of one event per worker process (PID) and how many threads handled them."""
    data = compute_results(event_id)
    return {"event_id": event_id, "ballots": data["ballots"], "processed_by": data["processed_by"],
            "generated_at": data["generated_at"]}


# ---------------------------------------------------------------- event list (the first page)

def crumbs(*items):
    """Breadcrumbs for the page header: crumbs(("Events", url), ("SSC Election", url), ("Ballot", None))."""
    return [{"label": label, "url": url} for label, url in items]


def my_event_states(student):
    """{event_id: {status: approved|pending|rejected|removed, has_voted, reason, ...}} for this student."""
    return {r["event_id"]: r for r in registrations_overview(student["school_student_no"] or student["student_no"],
                                                              student["id"])}


@app.route("/events")
@verified_required()
def index():
    """After logging in: "Choose a voting event" (Current / Upcoming / Previous). Each card shows the
    student's own state: Register -> Registration pending -> Vote now -> Voted (view receipt)."""
    events = load_events()
    mine = my_event_states(g.student)
    groups = {status: [e for e in events if e["status"] == status] for status in ("current", "upcoming", "previous")}
    groups["previous"].sort(key=lambda e: -(e["ends_ms"] or e["starts_ms"] or 0))   # newest first
    # Winners appear ONLY on the card of a PREVIOUS event whose official results are PUBLISHED (same rule as the
    # results page; from the server-side tally). Not published yet: the card says "Results will be announced soon".
    winners = {e["id"]: winners_summary(compute_results(e["id"])) for e in groups["previous"] if e["published"]}
    return render_template("events.html", election=election_state(), groups=groups, mine=mine,
                           winners=winners, crumbs=crumbs(("Events", None)))


# ---------------------------------------------------------------- one event

def event_crumbs(ev, *rest):
    return crumbs(("Events", url_for("index")), (ev["name"], url_for("event_home", event_id=ev["id"])), *rest)


@app.route("/e/<int:event_id>")
@verified_required(allow_admin=True)
def event_home(event_id):
    """An event card leads here. CURRENT -> ballot / receipt (or back to the list to register),
    UPCOMING -> info + countdown only, PREVIOUS -> the final results."""
    ev = event_or_404(event_id)
    if ev["status"] == "previous" or (session.get("is_admin") and ev["status"] == "current"):
        return redirect(url_for("event_results", event_id=event_id))
    state = None if session.get("is_admin") else my_event_states(g.student).get(event_id)
    if ev["status"] == "upcoming":
        # Only what the voting is for and when it starts/ends. No candidates or other details yet.
        return render_template("event_upcoming.html", election=ev, ev=ev, state=state, crumbs=event_crumbs(ev))
    if state and state["status"] == "approved":
        return redirect(url_for("event_receipt" if state["has_voted"] else "event_ballot", event_id=event_id))
    if state is None:
        flash(f"You are not registered for this voting event yet. Tap \"Register for this event\" on the "
              f"{ev['name']} card.", "info")
    elif state["status"] == "pending":
        flash(f"Your registration for {ev['name']} is still pending approval.", "info")
    else:
        flash(f"Your registration for {ev['name']} was rejected: {state['reason'] or 'no reason given'}", "error")
    return redirect(url_for("index") + "#current")


WRONG_LOGIN = ("Wrong Voter ID or password. Check for typos (passwords are case-sensitive), "
               "or ask your COMELEC officer for help.")


def is_admin_username(typed):
    """Only the exact username "admin" (any capitals) is checked against ADMIN_PASSWORD. A student's
    Voter ID or password can never lead to an admin login."""
    return typed.strip().lower() == ADMIN_USERNAME


def admin_locked_out():
    return db.query_one(
        """SELECT EXISTS (SELECT 1 FROM form_attempts WHERE kind = 'admin_lock' AND ip = %s
                            AND at > now() - make_interval(mins => %s))""", (client_ip(), ADMIN_LOCK_MINUTES))[0]


def admin_login_failed():
    """Count a wrong admin password for this IP (in the database: all worker processes see it).
    The 5th within 15 minutes locks admin login from this IP for 15 minutes."""
    ip = client_ip()
    with db.get_pool().connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('botoko-admin-fail:' || %s))", (ip,))
            conn.execute("INSERT INTO form_attempts (kind, ip) VALUES ('admin_fail', %s)", (ip,))
            fails = conn.execute(
                """SELECT COUNT(*) FROM form_attempts WHERE kind = 'admin_fail' AND ip = %s
                    AND at > now() - make_interval(mins => %s)""", (ip, ADMIN_LOCK_MINUTES)).fetchone()[0]
            if fails >= ADMIN_LOCK_TRIES:
                conn.execute("INSERT INTO form_attempts (kind, ip) VALUES ('admin_lock', %s)", (ip,))
                conn.execute("DELETE FROM form_attempts WHERE kind = 'admin_fail' AND ip = %s", (ip,))
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log.warning("FAILED admin login from IP %s at %s (%d of %d)", ip, stamp, min(fails, ADMIN_LOCK_TRIES), ADMIN_LOCK_TRIES)
    if fails >= ADMIN_LOCK_TRIES:
        log.warning("ADMIN LOGIN LOCKED for IP %s for %d minutes (from %s). Students can still log in.",
                    ip, ADMIN_LOCK_MINUTES, stamp)


def try_admin_login(password):
    """Username "admin" was typed. True = logged in as admin (new session). False = refused: wrong
    password, locked out, or no ADMIN_PASSWORD set. The caller then shows the NORMAL wrong-login
    message, so the page never reveals that "admin" is special."""
    ip = client_ip()
    if admin_locked_out():
        log.warning("BLOCKED admin login from IP %s at %s: locked out after %d wrong passwords",
                    ip, time.strftime("%Y-%m-%d %H:%M:%S"), ADMIN_LOCK_TRIES)
        return False
    if ADMIN_PASSWORD and hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
        db.execute("DELETE FROM form_attempts WHERE kind = 'admin_fail' AND ip = %s", (ip,))
        session.clear()                          # a brand-new session (any student login here is ended)
        session.permanent = True
        session.update(is_admin=True, csrf=secrets.token_hex(16), admin_seen=time.time())
        log.info("admin logged in from IP %s", ip)
        return True
    admin_login_failed()
    return False


@app.before_request
def admin_idle_timeout():
    """Admin session: ends after 30 minutes without activity. The dashboard's own auto-refresh
    (/admin/api/...) does not count as activity."""
    if request.endpoint == "static" or not session.get("is_admin"):
        return
    now = time.time()
    if now - session.get("admin_seen", 0) > ADMIN_IDLE_MINUTES * 60:
        session.clear()
        flash(f"You were logged out after {ADMIN_IDLE_MINUTES} minutes without activity. Please log in again.", "info")
        log.info("admin session ended after %d minutes without activity", ADMIN_IDLE_MINUTES)
        return
    if not request.path.startswith("/admin/api/"):
        session["admin_seen"] = now


NO_STORE_PAGES = {"login", "logout", "create_account_page", "forgot_voter_id"}


@app.after_request
def admin_no_store(resp):
    """Admin pages, the login pages and every page of a logged-in student are never kept by the browser:
    Back after logging out (e.g. on a shared voting device) doesn't show the previous person's page or login."""
    if request.endpoint == "static":
        return resp
    if (request.path.startswith("/admin") or session.get("is_admin") or session.get("student_id")
            or request.endpoint in NO_STORE_PAGES):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Pragma"] = "no-cache"
    return resp


NOT_REGISTERED = ("You are not registered for this voting event yet. "
                  "Please register with the election committee (COMELEC) first.")


@app.route("/e/<int:event_id>/login", methods=["GET", "POST"])
def event_login(event_id):
    """Old per-event login page: everyone logs in on the first page now (then comes back to this event)."""
    return redirect(url_for("login", next=url_for("event_home", event_id=event_id)), code=303)


def voter_required(view):
    """Pages of one event for a logged-in student: the event must exist, and the student must be
    REGISTERED in it (checked on the server every time). Sets g.student, g.event, g.reg."""
    @wraps(view)
    def wrapper(event_id, *args, **kwargs):
        ev = event_or_404(event_id)
        if session.get("is_admin"):
            return redirect(url_for("event_results", event_id=event_id))
        if "student_id" not in session:
            return redirect(login_url())
        problem = load_session_student()
        if problem is not None:
            return problem
        if g.student["account_status"] != "verified":
            return redirect(url_for("my_account"))
        g.event = ev
        reg = registration(event_id, g.student["id"])
        if reg is None:
            notice = registration_notice(event_id, student_id=g.student["id"])
            flash(notice["text"] if notice else "You are not registered for this voting event yet. "
                  "Tap Register on the event card first.", "error")
            return redirect(url_for("index") + "#" + ev["status"])
        g.reg = {"has_voted": reg[0], "voted_at": reg[1]}
        session["event_id"] = event_id
        if (ev["require_password_change"] and g.student["password_generated"] and not reg[0]
                and request.endpoint in ("event_ballot", "event_vote")):
            flash("Before you vote, please choose your own password.", "info")
            return redirect(url_for("account_password"))
        return view(event_id, *args, **kwargs)
    return wrapper


def switchable_events(student_id, except_id):
    """Other CURRENT events this student is registered in (switch without logging in again)."""
    return [e for e in student_events(student_id) if e["status"] == "current" and e["id"] != except_id]


@app.route("/e/<int:event_id>/ballot")
@voter_required
def event_ballot(event_id):
    ev = g.event
    if g.reg["has_voted"]:
        flash("You have already voted in this event. Here is the receipt of your ballot.", "already")
        return redirect(url_for("event_receipt", event_id=event_id))
    positions = load_ballot(event_id) if ev["is_open"] else []
    page = make_response(render_template(
        "ballot.html", election=ev, ev=ev, positions=positions,
        other_events=switchable_events(g.student["id"], event_id),
        crumbs=event_crumbs(ev, ("Ballot", None))))
    # Never keep a copy of the ballot in the browser: pressing Back from the receipt
    # must ask the server again (which then sends the student to their receipt).
    page.headers["Cache-Control"] = "no-store"
    return page


def form_choices():
    """The ballot form -> {position_id: [candidate_id, ...]}. pos_<id> can repeat (multi-seat)."""
    choices = {}
    for key, values in request.form.lists():
        if not key.startswith("pos_"):
            continue
        picked = [v for v in values if v.strip()]       # "" = "No vote for this position"
        try:
            choices[int(key[4:])] = [int(v) for v in picked]
        except ValueError:
            abort(400)
    return choices


@app.route("/e/<int:event_id>/vote", methods=["POST"])
@voter_required
def event_vote(event_id):
    """The ballot form without JavaScript (with JavaScript the page calls RPC cast_vote)."""
    check_csrf()
    try:
        receipt, pid, thread = cast_vote(event_id, g.student["id"], form_choices())
    except VoteError as e:
        # "already" messages are shown as a banner on the receipt page, others as a toast.
        if "already voted" in str(e):
            flash(str(e), "already")
            return redirect(url_for("event_receipt", event_id=event_id))
        flash(str(e), "error")
        return redirect(url_for("event_ballot", event_id=event_id))
    except psycopg.errors.UniqueViolation:
        # Safety net: the UNIQUE(event_id, student_id) constraint caught a duplicate.
        flash("You have already voted.", "already")
        return redirect(url_for("event_receipt", event_id=event_id))
    log.info("ballot %s recorded for student %s in event %s", receipt, session["student_no"], event_id)
    session["last_vote"] = {"pid": pid, "thread": thread}
    return redirect(url_for("event_receipt", event_id=event_id))


def mask_voter_id(voter_id):
    """Show only the last 3 characters on the receipt: V-7KQ3M9 -> V-XXX3M9."""
    v = voter_id or ""
    if len(v) <= 3:
        return v
    if v.startswith("V-"):
        return "V-" + "X" * (len(v) - 5) + v[-3:]
    return "X" * (len(v) - 3) + v[-3:]


def qr_svg(text):
    """A QR code as inline SVG (works offline and prints sharply)."""
    # omitsize: no fixed width/height (uses a viewBox), so CSS can scale it without cropping
    return segno.make(text, error="m").svg_inline(scale=4, border=2, dark="#111111", light="#ffffff",
                                                  omitsize=True)


def own_picks(event_id, student_id):
    """Every position of the event with the candidate(s) THIS student chose ([] = skipped).
    Only ever called with the logged-in student's own id (or, for the admin, the owner of a receipt)."""
    rows = db.query_all(
        f"""SELECT p.id, p.name, p.seats, c.full_name, c.party, c.id, {PHOTO_VERSION_SQL}
              FROM positions p
              LEFT JOIN votes v ON v.position_id = p.id AND v.student_id = %s
              LEFT JOIN candidates c ON c.id = v.candidate_id
              LEFT JOIN candidate_photos ph ON ph.candidate_id = c.id
             WHERE p.event_id = %s AND EXISTS (SELECT 1 FROM candidates c2 WHERE c2.position_id = p.id)
             ORDER BY p.sort_order, p.id, c.full_name""", (student_id, event_id))
    out = []
    for pid, pos, seats, name, party, cid, photo_v in rows:
        if not out or out[-1]["id"] != pid:
            out.append({"id": pid, "position": pos, "seats": seats, "chosen": []})
        if name:
            out[-1]["chosen"].append({"name": name, "party": party, "photo": photo_url(cid, photo_v)})
    return out


@app.route("/e/<int:event_id>/receipt")
@voter_required
def event_receipt(event_id):
    """The official voting receipt for one event (also reachable later from Vote History)."""
    row = db.query_one(
        """SELECT b.id, b.receipt_code,
                  to_char(b.cast_at AT TIME ZONE 'Asia/Manila', 'Mon DD, YYYY  HH12:MI:SS AM'),
                  b.worker_pid, b.thread_name
             FROM ballots b WHERE b.event_id = %s AND b.student_id = %s""", (event_id, g.student["id"]))
    if row is None:
        return redirect(url_for("event_ballot", event_id=event_id))
    ballot_id, code, cast_at, worker_pid, thread_name = row
    picks = own_picks(event_id, g.student["id"])
    verify_url = url_for("verify", code=code, _external=True)
    return render_template("receipt.html", election=g.event, ev=g.event, code=code, cast_at=cast_at,
                           ballot_no=f"{ballot_id:06d}", masked_voter_id=mask_voter_id(g.student["student_no"]),
                           worker_pid=worker_pid, thread_name=thread_name, picks=picks,
                           n_voted=sum(1 for p in picks if p["chosen"]), verify_url=verify_url,
                           qr=qr_svg(verify_url), other_events=switchable_events(g.student["id"], event_id),
                           dl_name=receipt_file_name(g.event["name"], code),
                           crumbs=event_crumbs(g.event, ("Receipt", None)))


def receipt_file_name(event_name, code):
    """BotoKo-receipt-SSC-Election-2026-A1B2C3D4 (no extension)."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", event_name or "event").strip("-")[:40].strip("-") or "event"
    return f"BotoKo-receipt-{slug}-{code}"


RECEIPT_CODE = re.compile(r"[0-9A-F]{8}")


@app.route("/receipt/<code>.<fmt>")
def receipt_download(code, fmt):
    """The official voting receipt as an image: /receipt/<verification code>.png or .jpg (drawn on the server,
    receipt_image.py). ONLY the student who owns the receipt, or the admin. Anyone else (another student,
    not logged in) gets 403. Never cached (same as the receipt page)."""
    if fmt not in ("png", "jpg"):
        abort(404)
    code = code.upper()
    is_admin = bool(session.get("is_admin"))
    if not is_admin:
        if "student_id" not in session or load_session_student() is not None:
            abort(403)
    row = db.query_one(
        """SELECT b.id, b.event_id, b.student_id, s.student_no,
                  to_char(b.cast_at AT TIME ZONE 'Asia/Manila', 'Mon DD, YYYY  HH12:MI:SS AM')
             FROM ballots b JOIN students s ON s.id = b.student_id
            WHERE b.receipt_code = %s""", (code,)) if RECEIPT_CODE.fullmatch(code) else None
    if row is None or (not is_admin and row[2] != g.student["id"]):
        abort(404 if is_admin else 403)
    ballot_id, event_id, owner_id, owner_no, cast_at = row
    ev = event_state(event_id)
    image = receipt_image.render({
        "school": ev["school_name"], "title": ev["title"], "school_year": ev["school_year"],
        "ballot_no": f"{ballot_id:06d}", "code": code, "cast_at": cast_at, "voter_id": mask_voter_id(owner_no),
        "picks": own_picks(event_id, owner_id),                       # the OWNER's choices (checked above)
        "verify_url": url_for("verify", code=code, _external=True), "verify_base": url_for("verify", _external=True),
    }, fmt)
    log.info("receipt image (%s) of ballot %s downloaded by %s", fmt, code, "admin" if is_admin else session.get("student_no"))
    resp = make_response(image)
    resp.headers["Content-Type"] = "image/png" if fmt == "png" else "image/jpeg"
    resp.headers["Content-Disposition"] = f'attachment; filename="{receipt_file_name(ev["name"], code)}.{fmt}"'
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.route("/e/<int:event_id>/results")
@verified_required(allow_admin=True)
def event_results(event_id):
    """Results of one event (logged-in students and the admin, e.g. the projector view).
    CURRENT: live. PREVIOUS: final, with the winners.
    UPCOMING: nothing to show yet (back to the event's info page)."""
    ev = event_or_404(event_id)
    if ev["status"] == "upcoming":
        return redirect(url_for("event_home", event_id=event_id))
    if not results_visible(ev):
        # Voting ended, COMELEC hasn't published the official results yet: no numbers, no winners.
        return render_template("results_pending.html", election=ev, ev=ev, crumbs=event_crumbs(ev, ("Results", None)))
    data = compute_results(event_id)
    return render_template("results.html", election=ev, ev=ev, data=data,
                           winners=winners_summary(data) if ev["status"] == "previous" else None,
                           crumbs=event_crumbs(ev, ("Results", None)))


def results_visible(ev):
    """Live results (current) are visible as before. FINAL results: only the admin, until published."""
    return ev["status"] != "previous" or ev["published"] or bool(session.get("is_admin"))


def winners_summary(data):
    """From the server-side tally (compute_results): per position the winner(s) and any tie for the last
    seat, with votes and % of the ballots cast. Never computed in the browser."""
    ballots = data["ballots"] or 0
    out = []
    for p in data["positions"]:
        cands = sorted(p["candidates"], key=lambda c: -c["votes"])
        for c in cands:
            c["pct"] = round(100 * c["votes"] / ballots, 1) if ballots else 0.0
        out.append({"id": p["id"], "name": p["name"], "seats": p["seats"], "abstain": p["abstain"],
                    "winners": [c for c in cands if c.get("lead") == "win"],
                    "tied": [c for c in cands if c.get("lead") == "tie"], "tie": p.get("tie", False),
                    "seats_left": p.get("seats_left", p["seats"]), "candidates": cands})
    return out


# ---------------------------------------------------------------- old single-election URLs
# Kept so old links, bookmarks and cached pages still work: they go to the right event.

def session_event_id():
    eid = session.get("event_id")
    if eid and db.query_one("SELECT 1 FROM events WHERE id = %s", (eid,)):
        return eid
    return None


def single_current_event():
    current = [e["id"] for e in load_events() if e["status"] == "current"]
    return current[0] if len(current) == 1 else None


def safe_next(target):
    """Only our own pages (no other websites) may be the 'go back to' target after logging in."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target \
            and not target.startswith(("/login", "/logout")) and target != "/":
        return target
    return None


def after_login_page():
    """Verified students: the event list. Pending / rejected accounts: My Account (status + profile)."""
    return url_for("index") if g.student["account_status"] == "verified" else url_for("my_account")


@app.route("/login", methods=["GET", "POST"])
@app.route("/", methods=["GET", "POST"])
def login():
    """THE FIRST PAGE: log in with Voter ID + password ("admin" + the admin password opens the COMELEC
    dashboard). Links: Create account, Forgot your Voter ID. ?next=<page> goes back there after login."""
    nxt = safe_next(request.values.get("next"))
    if request.method == "GET":
        if session.get("is_admin"):
            return redirect(url_for("admin"))
        if session.get("student_id") and load_session_student() is None:
            return redirect(nxt or after_login_page())
        return render_template("signin.html", election=election_state(), student_no="", next=nxt)
    check_csrf()
    typed = request.form.get("student_no", "").strip()
    password = request.form.get("password", "")
    if is_admin_username(typed):
        if try_admin_login(password):
            return redirect(nxt if nxt and nxt.startswith("/admin") else url_for("admin"))
        flash(WRONG_LOGIN, "error")
    elif st.looks_like_school_no(typed):
        flash("Use your Voter ID, not your school student number. Forgot it? Tap \"Forgot your Voter ID?\" "
              f"below. It looks like {st.VOTER_ID_EXAMPLE}.", "error")
    else:
        voter_id = st.normalize_login(typed)
        row = db.query_one("""SELECT id, full_name, password_hash, course, year_level, section, session_version
                                FROM students WHERE student_no = %s""", (voter_id,))
        if row and check_password_hash(row[2], password):
            start_student_session({"id": row[0], "student_no": voter_id, "full_name": row[1], "course": row[3],
                                   "year_level": row[4], "section": row[5], "session_version": row[6]})
            g.student = load_student(row[0])
            return redirect(nxt if nxt and g.student["account_status"] == "verified" else after_login_page())
        flash(WRONG_LOGIN, "error")
    return render_template("signin.html", election=election_state(), student_no=typed, next=nxt)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    """Students: the Log out link (GET). Admin: only the Log out BUTTON (POST + CSRF token), so another
    website can't log the admin out, and a GET never ends the admin session."""
    if session.get("is_admin"):
        if request.method != "POST":
            return redirect(url_for("admin"))
        check_csrf()
        session.clear()
        log.info("admin logged out")
        flash("You have been logged out of the admin dashboard.", "ok")
        return redirect(url_for("login"))
    if request.method == "POST":
        check_csrf()
    session.clear()
    flash("You have been logged out.", "ok")
    return redirect(url_for("login"))


@app.route("/ballot")
def ballot():
    eid = session_event_id()
    return redirect(url_for("event_ballot", event_id=eid) if eid else url_for("index"))


@app.route("/vote", methods=["POST"])
def vote():
    eid = session_event_id()
    if not eid:
        return redirect(url_for("index"))
    return redirect(url_for("event_vote", event_id=eid), code=307)      # 307 keeps the POST


@app.route("/receipt")
def receipt():
    eid = session_event_id()
    return redirect(url_for("event_receipt", event_id=eid) if eid else url_for("account_history"))


@app.route("/results")
def results():
    eid = request.args.get("event", type=int) or session_event_id() or single_current_event()
    if not eid:
        return redirect(url_for("index"))
    return redirect(url_for("event_results", event_id=eid, **({"projector": 1} if request.args.get("projector") else {})))


@app.route("/verify")
def verify():
    """Public: check that a ballot with this verification code was recorded, and when.
    It never shows the votes or the voter (ballot secrecy)."""
    typed = request.args.get("code", "")
    code = "".join(typed.split()).replace("-", "").upper()[:16]
    result = None
    if code:
        if len(code) == 8 and all(ch in "0123456789ABCDEF" for ch in code):
            row = db.query_one(
                """SELECT to_char(b.cast_at AT TIME ZONE 'Asia/Manila', 'Mon DD, YYYY  HH12:MI:SS AM'), e.name
                     FROM ballots b JOIN events e ON e.id = b.event_id WHERE b.receipt_code = %s""", (code,))
            result = {"found": row is not None, "cast_at": row[0] if row else None, "event": row[1] if row else None}
        else:
            result = {"found": False, "bad_format": True}
    return render_template("verify.html", election=election_state(), code=code, typed=typed, result=result,
                           crumbs=crumbs(("Events", url_for("index")), ("Verify a receipt", None)))


# ---------------------------------------------------------------- student account menu

@app.route("/account")
@login_required
def my_account():
    """My Account: profile, Voter ID, account status, my registrations, Vote now, vote history,
    register for more events. A PENDING (or rejected) account sees only its status + profile."""
    s = g.student
    regs = registrations_overview(s["school_student_no"] or s["student_no"], s["id"])
    history = [dict(zip(["event_id", "event", "event_status", "cast_at", "code"], r)) for r in db.query_all(
        f"""SELECT e.id, e.name, {EVENT_STATUS_SQL}, to_char(b.cast_at {_MNL}, {_SHOW}), b.receipt_code
              FROM ballots b JOIN events e ON e.id = b.event_id
             WHERE b.student_id = %s ORDER BY b.cast_at DESC""", (s["id"],))]
    mine = {r["event_id"] for r in regs}
    resp = make_response(render_template(
        "my_account.html", election=election_state(), s=s, verified=s["account_status"] == "verified",
        regs=regs, history=history,
        vote_now=[r for r in regs if r["event_status"] == "current" and r["status"] == "approved" and not r["has_voted"]],
        open_events=[e for e in load_events() if e["status"] in ("current", "upcoming") and e["id"] not in mine],
        any_events=bool(db.query_one("SELECT 1 FROM events LIMIT 1")),
        crumbs=crumbs(("Events", url_for("index")), ("My Account", None))))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/account/status.json")
def account_status_json():
    """The logged-in student's OWN account status, for the "waiting for verification" page (my_account.html asks
    every 4 seconds while it is visible): pending / verified / rejected (+ reason). Nothing about anyone else."""
    if "student_id" not in session or load_session_student() is not None:
        resp = jsonify({"status": "logged_out", "login_url": url_for("login")})
    else:
        s = g.student
        resp = jsonify({"status": s["account_status"], "reason": s.get("account_reason") or "",
                        "events_url": url_for("index")})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/account/profile")
@login_required
def account_profile():
    """Read-only: the details COMELEC has on file. Corrections go through COMELEC."""
    return render_template("account_profile.html", election=election_state(), s=g.student,
                           crumbs=crumbs(("Events", url_for("index")), ("Voter Profile", None)))


@app.route("/account/history")
@login_required
def account_history():
    """The student's voting record: every event they are registered in."""
    history = []
    for ev in student_events(g.student["id"]):
        row = db.query_one(
            """SELECT receipt_code, to_char(cast_at AT TIME ZONE 'Asia/Manila', 'Mon DD, YYYY HH12:MI:SS AM')
                 FROM ballots WHERE event_id = %s AND student_id = %s""", (ev["id"], g.student["id"]))
        history.append({
            "event": ev, "title": ev["name"], "is_open": ev["is_open"], "status": ev["status"],
            "voted": row is not None, "cast_at": row[1] if row else None, "code": row[0] if row else None,
            "picks": own_picks(ev["id"], g.student["id"]) if row else [],
        })
    return render_template("account_history.html", election=election_state(), history=history,
                           crumbs=crumbs(("Events", url_for("index")), ("Vote History", None)))


PASSWORD_MIN = 8
FAIL_LIMIT = 5                      # wrong "current password" tries ...
FAIL_WINDOW_MIN = 15                # ... per this many minutes, per student


def new_password_problems(student, current, new, confirm):
    """Rules for a student's own new password. Returns a list of problems (empty = OK)."""
    problems = []
    if len(new) < PASSWORD_MIN:
        problems.append(f"Your new password must be at least {PASSWORD_MIN} characters.")
    if len(new) > 128:
        problems.append("Your new password is too long (max 128 characters).")
    if new and new == current:
        problems.append("Your new password must be different from your current password.")
    voter_id = student["student_no"] or ""
    school_no = student["school_student_no"] or ""
    forbidden = {x.lower() for x in (voter_id, voter_id.replace("-", ""), school_no, school_no.replace("-", "")) if x}
    if new.lower() in forbidden:
        problems.append("Don't use your Voter ID or school student number as your password.")
    if new != confirm:
        problems.append("The new password and the confirmation don't match.")
    return problems


@app.route("/account/password", methods=["GET", "POST"])
@login_required
def account_password():
    """Change your own password. Wrong current-password tries are limited (stored in the
    database, so the limit holds across all server workers). Never touches the student's vote."""
    s = g.student
    errors = []
    if request.method == "POST":
        check_csrf()
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        new_version = None
        with db.get_pool().connection() as conn:
            with conn.transaction():
                # Lock this student's row: two tries at the same instant are handled one at a time.
                pw_hash = conn.execute("SELECT password_hash FROM students WHERE id = %s FOR UPDATE",
                                       (s["id"],)).fetchone()[0]
                fails, oldest = conn.execute(
                    """SELECT COUNT(*), MIN(failed_at) FROM password_change_failures
                        WHERE student_id = %s AND failed_at > now() - make_interval(mins => %s)""",
                    (s["id"], FAIL_WINDOW_MIN)).fetchone()
                if fails >= FAIL_LIMIT:
                    wait = conn.execute("SELECT CEIL(EXTRACT(EPOCH FROM (%s + make_interval(mins => %s) - now())) / 60)",
                                        (oldest, FAIL_WINDOW_MIN)).fetchone()[0]
                    errors.append(f"Too many wrong tries. Please wait {max(1, int(wait))} minute(s) and try again, "
                                  "or ask your COMELEC officer to reset your password.")
                elif not check_password_hash(pw_hash, current):
                    conn.execute("INSERT INTO password_change_failures (student_id) VALUES (%s)", (s["id"],))
                    left = FAIL_LIMIT - fails - 1
                    errors.append("Your current password is not correct. " +
                                  (f"You have {left} more tr{'y' if left == 1 else 'ies'}." if left else
                                   f"Please wait {FAIL_WINDOW_MIN} minutes before trying again."))
                else:
                    errors = new_password_problems(s, current, new, confirm)
                    if not errors:
                        # session_version + 1: every OTHER device is logged out on its next click.
                        new_version = conn.execute(
                            """UPDATE students SET password_hash = %s, password_generated = FALSE,
                                      session_version = session_version + 1
                                WHERE id = %s RETURNING session_version""",
                            (generate_password_hash(new, method=PASSWORD_METHOD), s["id"])).fetchone()[0]
                        conn.execute("DELETE FROM password_change_failures WHERE student_id = %s", (s["id"],))
        if new_version is not None:
            start_student_session(dict(s, session_version=new_version))   # new session on this device
            log.info("student %s changed their password", s["student_no"])
            flash("Your password was changed. Other devices were logged out.", "ok")
            return redirect(url_for("index"))
    forced = bool(s["require_password_change"] and s["password_generated"] and not s["has_voted"])
    return render_template("account_password.html", election=election_state(), errors=errors, forced=forced)


# ---------------------------------------------------------------- live results

@app.route("/api/results")
def api_results():
    """JSON results of one event: ?event=<id> (default: the student's event, or the only current one).
    Upcoming events have no results yet."""
    eid = request.args.get("event", type=int) or session_event_id() or single_current_event()
    if not eid:
        return jsonify({"error": "Add ?event=<id>."}), 400
    try:
        ev = event_state(eid)
    except EventNotFound:
        return jsonify({"error": "No such event."}), 404
    if ev["status"] == "upcoming":
        return jsonify({"error": "This event has not started yet."}), 404
    if not results_visible(ev):
        return jsonify({"error": "Voting has ended. The official results will be announced by the election "
                                 "committee soon.", "not_published": True}), 403
    return jsonify(public_results(compute_results(eid)))      # no worker PIDs / threads (admin-only)


@app.route("/session/refresh")
def session_refresh():
    """A fresh CSRF token + who is logged in, for a tab that was in the background for a long time
    (static/app.js) or after an RPC "wrong token" answer (static/rpc.js). Same-origin only: other websites
    can't read it (no CORS), and the token is useless to them without this browser's cookie."""
    who, label = who_am_i()
    resp = jsonify({"csrf": csrf_token(), "who": who, "label": label})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/healthz")
def healthz():
    db.query_one("SELECT 1")
    return {"ok": True, "pid": os.getpid()}


# ---------------------------------------------------------------- admin

ADMIN_TABS = ("overview", "ballot", "students", "accounts", "registrations", "results", "settings")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """Old address: the admin now logs in on the normal login page (Voter ID "admin")."""
    return redirect(url_for("login"), code=303)


def pick_admin_event(events, wanted=None):
    """The event the admin dashboard works on (event switcher): ?event=<id>, else the last one
    chosen, else the first CURRENT event, else the first upcoming, else the newest."""
    ids = [e["id"] for e in events]
    wanted = wanted or session.get("admin_event")
    if wanted not in ids:
        by_status = {s: [e["id"] for e in events if e["status"] == s] for s in ("current", "upcoming", "previous")}
        wanted = (by_status["current"] or by_status["upcoming"] or by_status["previous"][-1:] or [None])[0]
    session["admin_event"] = wanted
    return wanted


def dash_results_event(events, chosen=None):
    """The event shown in the dashboard's "Results by event" card: ?results=<id> if it exists, else the CURRENT
    (open) event (the dashboard's own event if it is open), else the most recently ended event, else the next
    upcoming one."""
    by_id = {e["id"]: e for e in events}
    if chosen in by_id:
        return by_id[chosen]
    current = [e for e in events if e["status"] == "current"]
    if current:
        mine = [e for e in current if e["id"] == session.get("admin_event")]
        return (mine or current)[0]
    previous = sorted((e for e in events if e["status"] == "previous"), key=lambda e: e["ends_ms"] or 0)
    if previous:
        return previous[-1]
    upcoming = [e for e in events if e["status"] == "upcoming"]
    return upcoming[0] if upcoming else None


@app.route("/admin/api/results-card")
def admin_api_results_card():
    """ADMIN-ONLY: the "Results by event" card of the dashboard for one event (?event=<id>), as HTML drawn with
    the same templates as the page (admin.js swaps it in without reloading, then starts its live updates)."""
    if not session.get("is_admin"):
        return jsonify({"error": "Please log in again."}), 401
    try:
        d = event_state(request.args.get("event", type=int))
    except EventNotFound:
        return jsonify({"error": "No such event."}), 404
    resp = make_response(render_template("_dash_results.html", d=d))
    resp.headers["Cache-Control"] = "no-store"
    return resp


def admin_eid():
    """Event id for admin form posts (hidden event_id field, else the event shown on the dashboard)."""
    eid = request.form.get("event_id", type=int) or session.get("admin_event")
    if not eid or not db.query_one("SELECT 1 FROM events WHERE id = %s", (eid,)):
        abort(400, "Choose an event first.")
    return eid


@app.route("/admin")
@admin_required
def admin():
    events = load_events()
    eid = pick_admin_event(events, request.args.get("event", type=int))
    if eid is None:                                   # no events at all yet
        return render_template("admin_first_event.html", election=election_state(), events=[])
    ev = event_state(eid)
    data = compute_results(eid)
    cols = ["id", "voter_id", "school_student_no", "full_name", "course", "department",
            "year_level", "section", "registered", "has_voted", "voted_at"]
    students = [dict(zip(cols, r)) for r in db.query_all(
        """SELECT s.id, s.student_no, s.school_student_no, s.full_name, s.course, s.department, s.year_level,
                  s.section, v.student_id IS NOT NULL, COALESCE(v.has_voted, FALSE),
                  to_char(v.voted_at AT TIME ZONE 'Asia/Manila', 'Mon DD, HH12:MI AM')
             FROM students s LEFT JOIN event_voters v ON v.student_id = s.id AND v.event_id = %s
            ORDER BY s.full_name, s.student_no""", (eid,))]
    registrations = load_registrations(eid)
    return render_template("admin.html", election=ev, ev=ev, events=events, data=data,
                           winners=winners_summary(data) if ev["status"] == "previous" else None,
                           registrations=registrations, reg_counts=registration_counts(eid),
                           accounts=load_accounts(), acc_counts=account_counts(),
                           overview=admin_overview(eid, data), ballot=load_ballot_all(eid),
                           students=students, turnout_groups=turnout_by_group(eid), locked=not no_ballots_yet(eid),
                           dash_ev=dash_results_event(events, request.args.get("results", type=int)))


def turnout_by_group(event_id):
    """Votes cast / registered per course, department and class (e.g. BSIT 3-A), for the students
    REGISTERED in this event. Only counts: it never says who voted for whom."""
    groups = {
        "course": "COALESCE(s.course, 'Not set')",
        "department": "COALESCE(s.department, 'Not set')",
        "class": "CASE WHEN s.course IS NULL THEN 'Not set' "
                 "ELSE s.course || ' ' || s.year_level || '-' || s.section END",
    }
    out = {}
    for name, expr in groups.items():
        rows = db.query_all(
            f"""SELECT {expr} AS grp, COUNT(*), COUNT(*) FILTER (WHERE v.has_voted)
                  FROM students s JOIN event_voters v ON v.student_id = s.id AND v.event_id = %s
                 GROUP BY grp ORDER BY grp""", (event_id,))
        out[name] = [{"name": r[0], "registered": int(r[1]), "voted": int(r[2]),
                      "pct": round(100 * int(r[2]) / int(r[1])) if r[1] else 0} for r in rows]
    return out


@app.route("/admin/api/system")
def admin_api_system():
    """System Resources / Parallel Processing data as JSON (admins only). There is no panel in the
    interface any more; open this URL directly to show the live numbers."""
    if not session.get("is_admin"):
        return jsonify({"error": "Please log in again."}), 401
    vm = psutil.virtual_memory()
    me = worker_snapshot(psutil.Process())
    rows = db.query_all(
        """SELECT hostname, pid, threads_busy, threads_peak, threads_total, requests_handled, cpu_percent,
                  memory_mb, pool_in_use, pool_max, EXTRACT(EPOCH FROM now() - updated_at)
             FROM worker_stats WHERE updated_at > now() - interval '10 seconds'
            ORDER BY hostname, pid""")
    cols = ["hostname", "pid", "threads_busy", "threads_peak", "threads_total", "requests_handled",
            "cpu_percent", "memory_mb", "pool_in_use", "pool_max", "age_s"]
    workers = {(r[0], r[1]): dict(zip(cols, r)) for r in rows}
    workers[(me["hostname"], me["pid"])] = dict(me, age_s=0)          # this worker: live numbers
    for w in workers.values():
        w["is_me"] = (w["hostname"], w["pid"]) == (me["hostname"], me["pid"])
        w["age_s"] = round(float(w["age_s"]), 1)
        w["cpu_percent"], w["memory_mb"] = float(w["cpu_percent"]), float(w["memory_mb"])
    workers = sorted(workers.values(), key=lambda w: (w["hostname"], w["pid"]))
    per_worker = threads_per_worker()
    by_pid = db.query_all(
        """SELECT worker_pid, COUNT(*), COUNT(DISTINCT thread_name) FROM ballots
            GROUP BY worker_pid ORDER BY COUNT(*) DESC""")
    by_thread = db.query_all(
        """SELECT worker_pid, thread_name, COUNT(*) FROM ballots
            GROUP BY worker_pid, thread_name ORDER BY COUNT(*) DESC, worker_pid LIMIT 12""")
    return jsonify({
        "server": {
            "hostname": HOSTNAME,
            "os": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "cpu_physical": psutil.cpu_count(logical=False),
            "cpu_logical": psutil.cpu_count(logical=True),
            "cpu_percent": psutil.cpu_percent(interval=None),
            "ram_total_mb": round(vm.total / 2**20),
            "ram_used_mb": round(vm.used / 2**20),
            "ram_percent": vm.percent,
        },
        "config": {
            "server": server_kind(),
            "cpu_cores_detected": PLAN["cores"],
            "workers": WORKERS_CONFIGURED if multi_process() else 1,
            "workers_rule": PLAN["reason"],
            "max_workers_cap": int(os.environ.get("MAX_WORKERS", concurrency.DEFAULT_MAX_WORKERS)),
            "threads_per_worker": per_worker,
            "total_threads": WORKERS_CONFIGURED * per_worker if multi_process() else 0,
            "pool_max_per_worker": POOL_MAX,
            "startup_line": concurrency.banner(PLAN),
        },
        "workers": workers,
        "live": {
            "threads_busy": sum(w["threads_busy"] for w in workers),
            "threads_peak": sum(w["threads_peak"] for w in workers),
            "threads_total": sum(w["threads_total"] for w in workers),
            "requests_handled": sum(w["requests_handled"] for w in workers),
            "pool_in_use": sum(w["pool_in_use"] for w in workers),
            "pool_max": sum(w["pool_max"] for w in workers),
        },
        "ballots_by_worker": [{"pid": p, "ballots": int(n), "threads": int(t)} for p, n, t in by_pid],
        "ballots_by_thread": [{"pid": p, "thread": t, "ballots": int(n)} for p, t, n in by_thread],
        "generated_at": time.strftime("%H:%M:%S"),
    })


@app.route("/admin/api/workers")
def admin_api_workers():
    """ADMIN-ONLY JSON for the "Parallel processing" card of the admin Results tab (?event=<id>):
    ballots per worker process (PID) and threads. Students' results never include this."""
    if not session.get("is_admin"):
        return jsonify({"error": "Please log in again."}), 401
    eid = request.args.get("event", type=int) or session.get("admin_event")
    try:
        event_state(eid)
    except EventNotFound:
        return jsonify({"error": "No such event."}), 404
    return jsonify(worker_stats(eid))


@app.route("/admin/api/overview")
def admin_api_overview():
    """JSON for the auto-refreshing Overview tab (?event=<id>)."""
    if not session.get("is_admin"):
        return jsonify({"error": "Please log in again."}), 401
    eid = request.args.get("event", type=int) or session.get("admin_event")
    try:
        event_state(eid)
    except EventNotFound:
        return jsonify({"error": "No such event."}), 404
    return jsonify(admin_overview(eid, compute_results(eid)))


def admin_overview(event_id, data):
    recent = db.query_all(
        """SELECT s.student_no,
                  to_char(b.cast_at AT TIME ZONE 'Asia/Manila', 'HH12:MI:SS.MS AM'),
                  b.worker_pid, b.thread_name
             FROM ballots b JOIN students s ON s.id = b.student_id
            WHERE b.event_id = %s
            ORDER BY b.cast_at DESC LIMIT 10""", (event_id,))
    return {
        "title": data["title"],
        "is_open": data["is_open"],
        "status": data["status"],
        "ballots": data["ballots"],
        "registered": data["registered"],
        "processed_by": data["processed_by"],
        "checks": [{"label": label, "ok": ok, "detail": detail}
                   for label, ok, detail in integrity_checks(event_id)],
        # "Thread-10 (process_request_thread)" -> "Thread-10": keeps the table narrow on phones.
        "recent": [{"student_no": sno, "time": t, "worker_pid": pid, "thread": (th or "").split(" (")[0]}
                   for sno, t, pid, th in recent],
        "generated_at": time.strftime("%H:%M:%S"),
    }


def load_ballot_all(event_id):
    rows = db.query_all(
        f"""SELECT p.id, p.name, p.sort_order, p.seats, c.id, c.full_name, c.party, {PHOTO_VERSION_SQL},
                   c.course_year, c.motto, c.background
              FROM positions p
              LEFT JOIN candidates c ON c.position_id = p.id
              LEFT JOIN candidate_photos ph ON ph.candidate_id = c.id
             WHERE p.event_id = %s
             ORDER BY p.sort_order, p.id, c.id""", (event_id,))
    out = []
    for pid, pname, order, seats, cid, cname, party, photo_v, course_year, motto, background in rows:
        if not out or out[-1]["id"] != pid:
            out.append({"id": pid, "name": pname, "order": order, "seats": seats, "candidates": []})
        if cid is not None:
            out[-1]["candidates"].append(
                {"id": cid, "name": cname, "party": party, "photo": photo_url(cid, photo_v),
                 "course_year": course_year, "motto": motto, "background": background})
    return out


def integrity_checks(event_id):
    r = db.query_one(
        """SELECT
             (SELECT COUNT(*) FROM ballots WHERE event_id = %(e)s),
             (SELECT COUNT(*) FROM event_voters WHERE event_id = %(e)s AND has_voted),
             (SELECT COUNT(*) FROM votes v JOIN ballots b ON b.id = v.ballot_id WHERE b.event_id = %(e)s),
             (SELECT COUNT(*) FROM (SELECT student_id FROM ballots WHERE event_id = %(e)s
                                    GROUP BY student_id HAVING COUNT(*) > 1) d),
             (SELECT COUNT(*) FROM (SELECT v.ballot_id, v.candidate_id FROM votes v
                                      JOIN ballots b ON b.id = v.ballot_id WHERE b.event_id = %(e)s
                                    GROUP BY v.ballot_id, v.candidate_id HAVING COUNT(*) > 1) d),
             (SELECT COUNT(*) FROM (SELECT v.ballot_id, v.position_id FROM votes v
                                      JOIN ballots b ON b.id = v.ballot_id
                                      JOIN positions p ON p.id = v.position_id
                                     WHERE b.event_id = %(e)s
                                     GROUP BY v.ballot_id, v.position_id, p.seats HAVING COUNT(*) > p.seats) d),
             (SELECT COUNT(*) FROM votes v JOIN ballots b ON b.id = v.ballot_id
                JOIN positions p ON p.id = v.position_id
               WHERE b.event_id = %(e)s AND (v.student_id <> b.student_id OR p.event_id <> b.event_id))""",
        {"e": event_id})
    ballots, marked, votes, dup_ballots, dup_votes, over_seats, mismatched = (int(x) for x in r)
    return [
        ("Ballots = students marked as voted", ballots == marked, f"{ballots} = {marked}"),
        ("Every vote belongs to its voter's ballot (and this event)", mismatched == 0,
         f"{votes} votes on {ballots} ballots, {mismatched} mismatched"),
        ("No student voted twice in this event", dup_ballots == 0, f"{dup_ballots} duplicates"),
        ("No candidate chosen twice on a ballot", dup_votes == 0, f"{dup_votes} duplicates"),
        ("No position has more picks than seats", over_seats == 0, f"{over_seats} over the limit"),
    ]


def admin_post():
    check_csrf()
    if not session.get("is_admin"):
        abort(403)


def back_to_admin(tab="overview", event_id=None):
    """Redirect to the admin page for this event, re-opening the tab the form was sent from."""
    tab = request.form.get("tab") or tab
    eid = event_id or request.form.get("event_id", type=int) or session.get("admin_event")
    return redirect(url_for("admin", **({"event": eid} if eid else {})) + "#" + (tab if tab in ADMIN_TABS else "overview"))


def clear_results_cache(event_id=None):
    with _results_lock:
        if event_id is None:
            _results_cache.clear()
        else:
            _results_cache.pop(event_id, None)


def no_ballots_yet(event_id):
    return db.query_one("SELECT COUNT(*) FROM ballots WHERE event_id = %s", (event_id,))[0] == 0


def ballot_locked(event_id):
    """Once anyone has voted in an event, its ballot can't change (it would invalidate cast ballots)."""
    if no_ballots_yet(event_id):
        return False
    flash("The ballot is locked because votes were already cast in this event. "
          "Reset this event's votes first (Settings tab).", "error")
    return True


EVENT_LOCK = 7301          # advisory lock (EVENT_LOCK, event id): cast_vote SHARED, set_event_mode EXCLUSIVE


def set_event_mode(event_id, mode):
    """'open' (CURRENT now), 'closed' (PREVIOUS now) or 'auto' (follow the start/end dates).

    Takes the event's lock EXCLUSIVELY (cast_vote takes it shared): it waits for the ballots that are being
    saved at this moment (they are fully counted), and every ballot after it sees the new status.
    Closing an OPEN event = "End voting now": the end time becomes now (so the event stays finished even if
    it is later set back to follow its schedule), and who ended it and when is written to the server log."""
    with db.get_pool().connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s::int, %s::int)", (EVENT_LOCK, event_id))
            row = conn.execute(f"SELECT {EVENT_STATUS_SQL}, e.name, to_char(e.ends_at {_MNL}, {_SHOW}) FROM events e WHERE e.id = %s",
                               (event_id,)).fetchone()
            ending_early = row is not None and mode == "closed" and row[0] == "current"
            if ending_early:
                conn.execute("""UPDATE events SET manual = 'closed',
                                       ends_at = CASE WHEN ends_at IS NULL OR ends_at > now() THEN now() ELSE ends_at END
                                 WHERE id = %s""", (event_id,))
            else:
                conn.execute("UPDATE events SET manual = %s WHERE id = %s", (mode, event_id))
    if ending_early:
        who = "admin" if session.get("is_admin") else "unknown"
        log.warning("VOTING ENDED by %s (IP %s) for event %s \"%s\" at %s Philippine time (scheduled end: %s)",
                    who, client_ip(), event_id, row[1], time.strftime("%Y-%m-%d %H:%M:%S"), row[2] or "not set")
    clear_results_cache(event_id)
    return event_state(event_id)


@app.post("/admin/toggle")
@admin_required
def admin_toggle():
    """Open / close voting for the selected event (the RPC open_voting / close_voting does the same)."""
    admin_post()
    eid = admin_eid()
    mode = request.form.get("mode")
    if mode not in ("open", "closed", "auto"):
        mode = "closed" if event_state(eid)["is_open"] else "open"
    ev = set_event_mode(eid, mode)
    flash({"open": "Voting is now OPEN for this event. Registered students can submit their ballots.",
           "closed": "Voting has ENDED for this event. No more ballots will be accepted."
                     + (" The official results are published automatically." if ev["auto_publish"] else
                        " Review the results, then click \"Publish official results\" when ready."),
           "auto": f"This event now follows its schedule. Status right now: {ev['status_label']}."}[mode], "ok")
    return back_to_admin()


def parse_local_dt(text):
    """'2026-11-20T08:00' from a datetime-local input, read as Philippine time. '' -> None."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return time.strptime(text, "%Y-%m-%dT%H:%M") and text
    except ValueError:
        return False


def event_form_fields():
    f = {
        "name": " ".join(request.form.get("name", "").split())[:120],
        "description": request.form.get("description", "").strip()[:1000],
        "organization": " ".join(request.form.get("organization", "").split())[:120],
        "school_year": " ".join(request.form.get("school_year", "").split())[:20],
        "starts": parse_local_dt(request.form.get("starts_at")),
        "ends": parse_local_dt(request.form.get("ends_at")),
        # Registrations from VERIFIED accounts are approved at once (COMELEC already checked the student).
        "auto_approve": request.form.get("auto_approve") == "1",
        "auto_publish": request.form.get("auto_publish") == "1",       # default OFF: COMELEC publishes by hand
    }
    problems = []
    if not f["name"]:
        problems.append("the event needs a name")
    if f["starts"] is False or f["ends"] is False:
        problems.append("a date/time is not valid")
    elif f["starts"] and f["ends"] and f["ends"] <= f["starts"]:
        problems.append("voting must end after it starts")
    return f, problems


@app.post("/admin/event")
@admin_required
def admin_event_create():
    """Create a new voting event (it follows its dates: upcoming until it starts)."""
    admin_post()
    f, problems = event_form_fields()
    if problems:
        flash("Event not created: " + "; ".join(problems) + ".", "error")
        return back_to_admin("overview")
    eid = db.query_one(
        """INSERT INTO events (name, description, organization, school_year, starts_at, ends_at, auto_approve, auto_publish)
           VALUES (%s, %s, %s, %s, %s::timestamp AT TIME ZONE 'Asia/Manila', %s::timestamp AT TIME ZONE 'Asia/Manila', %s, %s)
           RETURNING id""",
        (f["name"], f["description"], f["organization"], f["school_year"], f["starts"] or None, f["ends"] or None,
         f["auto_approve"], f["auto_publish"]))[0]
    flash(f"Created the event \"{f['name']}\". Now add its positions and register its voters.", "ok")
    return back_to_admin("ballot", eid)


@app.post("/admin/event/save")
@admin_required
def admin_event_save():
    """Settings: the selected event's name, description, organization, school year and dates."""
    admin_post()
    eid = admin_eid()
    f, problems = event_form_fields()
    if problems:
        flash("Not saved: " + "; ".join(problems) + ".", "error")
        return back_to_admin("settings")
    db.execute(
        """UPDATE events SET name = %s, description = %s, organization = %s, school_year = %s,
                  starts_at = %s::timestamp AT TIME ZONE 'Asia/Manila', ends_at = %s::timestamp AT TIME ZONE 'Asia/Manila',
                  auto_approve = %s, auto_publish = %s
            WHERE id = %s""",
        (f["name"], f["description"], f["organization"], f["school_year"], f["starts"] or None, f["ends"] or None,
         f["auto_approve"], f["auto_publish"], eid))
    clear_results_cache(eid)
    flash("Event details saved.", "ok")
    return back_to_admin("settings")


@app.post("/admin/event/delete")
@admin_required
def admin_event_delete():
    """Delete the selected event with its positions, candidates, registrations and ballots.
    Student accounts are kept (they may be registered in other events)."""
    admin_post()
    eid = admin_eid()
    ev = event_state(eid)
    if request.form.get("confirm", "").strip() != "DELETE":
        flash("Type DELETE (in capital letters) to confirm.", "error")
        return back_to_admin("settings")
    db.execute("DELETE FROM events WHERE id = %s", (eid,))
    clear_results_cache(eid)
    session.pop("admin_event", None)
    log.info("admin deleted event %s (%s, %d ballots)", eid, ev["name"], ev["ballots"])
    flash(f"Deleted the event \"{ev['name']}\" ({ev['ballots']} ballots). Student accounts were kept.", "ok")
    return redirect(url_for("admin"))


@app.post("/admin/password-rule")
@admin_required
def admin_password_rule():
    """Settings: turn "students must change the generated password before voting" on/off (all events)."""
    admin_post()
    on = db.query_one("""UPDATE election SET require_password_change = NOT require_password_change
                         WHERE id = 1 RETURNING require_password_change""")[0]
    flash("Students must now change their password before voting." if on else
          "Students can vote with the password from their slip.", "ok")
    return back_to_admin("settings")


@app.post("/admin/reset")
@admin_required
def admin_reset():
    """Delete all votes of the selected event (for rehearsals). Accounts, registrations and
    candidates are kept. Other events are not touched."""
    admin_post()
    eid = admin_eid()
    if request.form.get("confirm") != "RESET":
        flash('Type RESET (in capital letters) to confirm.', "error")
        return back_to_admin("settings")
    with db.get_pool().connection() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM ballots WHERE event_id = %s", (eid,))       # votes go with their ballot
            conn.execute("UPDATE event_voters SET has_voted = FALSE, voted_at = NULL WHERE event_id = %s", (eid,))
    clear_results_cache(eid)
    flash("All votes of this event were cleared.", "ok")
    return back_to_admin("settings")


@app.post("/admin/register")
@admin_required
def admin_register():
    """Register / unregister students for the selected event (Students tab).
    Students who already voted in this event can't be unregistered."""
    admin_post()
    eid = admin_eid()
    ids = [int(x) for x in request.form.getlist("student_id") if x.isdigit()]
    action = request.form.get("action")
    if not ids:
        flash("Select at least one student first.", "error")
        return back_to_admin("students")
    if action == "register":
        n = db.query_one(
            """WITH added AS (INSERT INTO event_voters (event_id, student_id)
                              SELECT %s, id FROM students WHERE id = ANY(%s)
                              ON CONFLICT (event_id, student_id) DO NOTHING RETURNING 1)
               SELECT COUNT(*) FROM added""", (eid, ids))[0]
        db.execute("""UPDATE registrations SET status = 'approved', reason = '', decided_at = now()
                       WHERE event_id = %s AND student_id = ANY(%s) AND status <> 'approved'""", (eid, ids))
        flash(f"Registered {n} student{'s' if n != 1 else ''} for this event.", "ok")
    elif action == "unregister":
        gone = [r[0] for r in db.query_all(
            """DELETE FROM event_voters WHERE event_id = %s AND student_id = ANY(%s)
                AND NOT has_voted RETURNING student_id""", (eid, ids))]
        n = len(gone)
        db.execute("""UPDATE registrations SET status = 'rejected', reason = 'Removed from this event by COMELEC.',
                             decided_at = now() WHERE event_id = %s AND student_id = ANY(%s)""", (eid, gone))
        flash(f"Removed {n} student{'s' if n != 1 else ''} from this event "
              "(students who already voted stay registered).", "ok")
    clear_results_cache(eid)
    return back_to_admin("students")


def owned_position(event_id, position_id):
    """The position id if it belongs to this event (never edit another event by accident)."""
    row = db.query_one("SELECT id FROM positions WHERE id = %s AND event_id = %s", (position_id, event_id))
    if row is None:
        abort(400, "That position is not part of the selected event.")
    return row[0]


def owned_candidate(event_id, candidate_id):
    row = db.query_one("""SELECT c.id FROM candidates c JOIN positions p ON p.id = c.position_id
                           WHERE c.id = %s AND p.event_id = %s""", (candidate_id, event_id))
    if row is None:
        abort(400, "That candidate is not part of the selected event.")
    return row[0]


def seats_field():
    return max(1, min(request.form.get("seats", type=int) or 1, 20))


@app.post("/admin/position")
@admin_required
def admin_add_position():
    admin_post()
    eid = admin_eid()
    if ballot_locked(eid):
        return back_to_admin("ballot")
    name = request.form.get("name", "").strip()[:80]
    if name:
        row = db.query_one("""INSERT INTO positions (event_id, name, seats, sort_order)
                      VALUES (%s, %s, %s, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM positions WHERE event_id = %s))
                      ON CONFLICT (event_id, name) DO NOTHING RETURNING id""", (eid, name, seats_field(), eid))
        if row:
            flash(f"Added position: {name}. Now add its candidates.", "ok")
        else:
            flash(f'This event already has a position called "{name}".', "error")
    clear_results_cache(eid)
    return back_to_admin("ballot")


@app.post("/admin/position/edit")
@admin_required
def admin_edit_position():
    admin_post()
    eid = admin_eid()
    if ballot_locked(eid):
        return back_to_admin("ballot")
    name = request.form.get("name", "").strip()[:80]
    position_id = owned_position(eid, request.form.get("id", type=int))
    if name:
        try:
            db.execute("UPDATE positions SET name = %s, seats = %s WHERE id = %s", (name, seats_field(), position_id))
            flash(f"Saved position: {name}.", "ok")
        except psycopg.errors.UniqueViolation:
            flash(f'This event already has a position called "{name}".', "error")
    clear_results_cache(eid)
    return back_to_admin("ballot")


@app.post("/admin/position/move")
@admin_required
def admin_move_position():
    """Move a position one step up or down on this event's ballot."""
    admin_post()
    eid = admin_eid()
    if ballot_locked(eid):
        return back_to_admin("ballot")
    position_id = request.form.get("id", type=int)
    step = -1 if request.form.get("dir") == "up" else 1
    with db.get_pool().connection() as conn:
        with conn.transaction():
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM positions WHERE event_id = %s ORDER BY sort_order, id FOR UPDATE", (eid,)).fetchall()]
            if position_id in ids:
                i = ids.index(position_id)
                if 0 <= i + step < len(ids):
                    ids[i], ids[i + step] = ids[i + step], ids[i]
                for order, pid in enumerate(ids, 1):
                    conn.execute("UPDATE positions SET sort_order = %s WHERE id = %s", (order, pid))
    clear_results_cache(eid)
    return back_to_admin("ballot")


# ---- candidate photos
# The browser (static/admin.js) already shrinks photos to a 400x400 JPEG (~30-60 KB).
# The server still checks everything, because a request can be sent without our page.
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_PHOTO_BYTES = 300 * 1024


class PhotoError(Exception):
    pass


def sniff_image(data):
    """The file's REAL type, from its first bytes ("magic bytes"), not its name."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def read_photo_upload():
    """Return (mime, bytes) for the uploaded "photo" field, None if no file was chosen."""
    f = request.files.get("photo")
    if f is None or not f.filename:
        return None
    data = f.read(MAX_PHOTO_BYTES + 1)
    if not data:
        raise PhotoError("The photo file is empty.")
    if len(data) > MAX_PHOTO_BYTES:
        raise PhotoError("That photo is too big (over 300 KB). Please choose it again on this page, "
                         "which shrinks it automatically.")
    real_type = sniff_image(data)
    if f.mimetype not in PHOTO_TYPES or real_type is None:
        raise PhotoError("That file is not a JPG, PNG or WebP photo.")
    return real_type, data


def save_photo(conn, candidate_id, photo):
    mime, data = photo
    conn.execute(
        """INSERT INTO candidate_photos (candidate_id, mime, data, updated_at)
           VALUES (%s, %s, %s, clock_timestamp())
           ON CONFLICT (candidate_id) DO UPDATE
              SET mime = EXCLUDED.mime, data = EXCLUDED.data, updated_at = clock_timestamp()""",
        (candidate_id, mime, data))


@app.route("/photo/<int:candidate_id>")
def photo(candidate_id):
    """Serve a candidate photo from the database.

    The page links to /photo/<id>?v=<version>. When v matches, the browser may keep the
    photo for a year without asking again (each phone downloads each photo only once).
    """
    row = db.query_one(
        "SELECT mime, data, (extract(epoch FROM updated_at) * 1000000)::bigint "
        "FROM candidate_photos WHERE candidate_id = %s", (candidate_id,))
    if row is None:
        return Response("No photo", status=404, mimetype="text/plain",
                        headers={"Cache-Control": "no-cache"})
    mime, data, version = row
    resp = Response(bytes(data), mimetype=mime)
    resp.set_etag(f"{candidate_id}-{version}")
    if request.args.get("v") == str(version):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:                                   # old or missing ?v=: let the browser re-check
        resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp.make_conditional(request)   # answers 304 Not Modified when the ETag matches


MAX_BACKGROUND_ITEMS = 12


def candidate_fields():
    """Candidate details from the admin form, trimmed to sensible lengths."""
    lines = background_items(request.form.get("background", ""))[:MAX_BACKGROUND_ITEMS]
    return {
        "name": request.form.get("name", "").strip()[:80],
        "party": request.form.get("party", "").strip()[:60],
        "course_year": request.form.get("course_year", "").strip()[:40],
        "motto": " ".join(request.form.get("motto", "").split())[:160],
        "background": "\n".join(line[:120] for line in lines),
    }


@app.post("/admin/candidate")
@admin_required
def admin_add_candidate():
    admin_post()
    eid = admin_eid()
    if ballot_locked(eid):
        return back_to_admin("ballot")
    f = candidate_fields()
    position_id = owned_position(eid, request.form.get("position_id", type=int))
    try:
        photo_upload = read_photo_upload()
    except PhotoError as e:
        flash(f"{e} The candidate was not added.", "error")
        return back_to_admin("ballot")
    if f["name"] and position_id:
        with db.get_pool().connection() as conn:
            with conn.transaction():
                cid = conn.execute(
                    """INSERT INTO candidates (position_id, full_name, party, course_year, motto, background)
                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                    (position_id, f["name"], f["party"], f["course_year"], f["motto"], f["background"])).fetchone()[0]
                if photo_upload:
                    save_photo(conn, cid, photo_upload)
        flash(f"Added candidate: {f['name']}" + (" (with photo)." if photo_upload else "."), "ok")
    clear_results_cache()
    return back_to_admin("ballot")


@app.post("/admin/candidate/edit")
@admin_required
def admin_edit_candidate():
    """Save the candidate's details, and the photo too if a new one was chosen."""
    admin_post()
    eid = admin_eid()
    if ballot_locked(eid):
        return back_to_admin("ballot")
    f = candidate_fields()
    candidate_id = owned_candidate(eid, request.form.get("id", type=int))
    try:
        photo_upload = read_photo_upload()
    except PhotoError as e:
        flash(f"{e} Nothing was changed.", "error")
        return back_to_admin("ballot")
    if f["name"] and candidate_id:
        with db.get_pool().connection() as conn:
            with conn.transaction():
                conn.execute(
                    """UPDATE candidates SET full_name = %s, party = %s, course_year = %s, motto = %s,
                              background = %s WHERE id = %s""",
                    (f["name"], f["party"], f["course_year"], f["motto"], f["background"], candidate_id))
                if photo_upload:
                    save_photo(conn, candidate_id, photo_upload)
        flash(f"Saved changes to {f['name']}" + (" (new photo)." if photo_upload else "."), "ok")
    clear_results_cache()
    return back_to_admin("ballot")


@app.post("/admin/candidate/photo/remove")
@admin_required
def admin_remove_photo():
    admin_post()
    eid = admin_eid()
    if ballot_locked(eid):
        return back_to_admin("ballot")
    candidate_id = owned_candidate(eid, request.form.get("id", type=int))
    db.execute("DELETE FROM candidate_photos WHERE candidate_id = %s", (candidate_id,))
    flash("Photo removed. The candidate's initials are shown instead.", "ok")
    clear_results_cache()
    return back_to_admin("ballot")


@app.post("/admin/delete")
@admin_required
def admin_delete():
    admin_post()
    eid = admin_eid()
    if ballot_locked(eid):
        return back_to_admin("ballot")
    kind = request.form.get("kind")
    item_id = request.form.get("id", type=int)
    if kind == "position":
        item_id = owned_position(eid, item_id)
    elif kind == "candidate":
        item_id = owned_candidate(eid, item_id)
    if kind == "position":
        db.execute("DELETE FROM positions WHERE id = %s", (item_id,))
        flash("Position and its candidates were removed.", "ok")
    elif kind == "candidate":
        db.execute("DELETE FROM candidates WHERE id = %s", (item_id,))
        flash("Candidate removed.", "ok")
    clear_results_cache()
    return back_to_admin("ballot")


# ---- student accounts (admin)

ACCOUNT_CSV_COLUMNS = ["voter_id", "password", "school_student_no", "full_name", "course",
                       "department", "year", "section"]


def insert_student(conn, details, password):
    """Create one account with a new random Voter ID and return that Voter ID.

    If the random Voter ID already exists (very unlikely), ON CONFLICT skips the insert
    and we simply try another one. The UNIQUE index on school_student_no still raises an
    error if that number is taken (the whole import is then rolled back).
    """
    pw_hash = generate_password_hash(password, method=PASSWORD_METHOD)
    for _ in range(20):
        voter_id = st.new_voter_id()
        row = conn.execute(
            """INSERT INTO students (student_no, full_name, password_hash, school_student_no,
                                     course, department, year_level, section)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (student_no) DO NOTHING RETURNING id""",
            (voter_id, details["full_name"], pw_hash, details["school_student_no"], details["course"],
             details["department"], details["year"], details["section"])).fetchone()
        if row:
            return voter_id
    raise RuntimeError("Could not find a free Voter ID")


def test_account_rows(count):
    """Details for numbered TEST accounts (school numbers 99-9-00001, 99-9-00002, ...)."""
    taken = {r[0] for r in db.query_all(
        "SELECT school_student_no FROM students WHERE school_student_no LIKE %s", ("99-9-%",))}
    courses = ["BSIT", "BSCS", "BSIS"]
    rows, n = [], 0
    while len(rows) < count:
        n += 1
        number = f"99-9-{n:05d}"
        if number in taken:
            continue
        rows.append({"school_student_no": number, "full_name": f"Test Student {n}",
                     "course": courses[n % 3], "department": "College of Computing Studies",
                     "year": n % 4 + 1, "section": "ABC"[n % 3]})
    return rows


def register_by_school_no(event_id, numbers):
    """Register existing accounts (by school student number) in an event. Returns how many were new."""
    return db.query_one(
        """WITH added AS (INSERT INTO event_voters (event_id, student_id)
                          SELECT %s, id FROM students WHERE school_student_no = ANY(%s)
                          ON CONFLICT (event_id, student_id) DO NOTHING RETURNING 1)
           SELECT COUNT(*) FROM added""", (event_id, numbers))[0]


def accounts_csv(created):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(ACCOUNT_CSV_COLUMNS)
    for a in created:
        writer.writerow([a["voter_id"], a["password"], a["school_student_no"], a["full_name"],
                         a["course"], a["department"], a["year"], a["section"]])
    return "﻿" + out.getvalue()        # the BOM makes Excel read names like "Peña" correctly


def one_time_page(**context):
    """Pages that show passwords: never stored by the browser, never shown again."""
    page = make_response(render_template("accounts_created.html", election=election_state(), **context))
    page.headers["Cache-Control"] = "no-store"
    return page


@app.post("/admin/accounts")
@admin_required
def admin_accounts():
    """Create student accounts from a class list (pasted and/or a .csv file), or test accounts.

    Every row is checked first. If ANY row has a problem, NOTHING is created and the
    problems are listed. Voter IDs and passwords are generated here and shown only once.
    """
    admin_post()
    # Register the students in the event selected on the dashboard (checkbox, on by default).
    eid = admin_eid() if request.form.get("register_event") else None
    numbers = []
    if request.form.get("mode") == "test":
        count = max(1, min(request.form.get("count", type=int) or 0, 2000))
        to_create, skipped = test_account_rows(count), []
    else:
        text = request.form.get("students", "")
        upload = request.files.get("csv_file")
        if upload and upload.filename:
            raw = upload.read()
            try:
                file_text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:              # e.g. saved by older Excel
                file_text = raw.decode("cp1252", errors="replace")
            text = file_text + ("\n" + text if text.strip() else "")
        rows = st.parse_class_list(text)
        if not rows:
            flash("Nothing to create. Paste the class list or choose a .csv file first.", "error")
            return back_to_admin("students")
        numbers = [values[0].strip() for _, values in rows]
        existing = {r[0] for r in db.query_all(
            "SELECT school_student_no FROM students WHERE school_student_no = ANY(%s)", (numbers,))}
        to_create, skipped, errors = st.check_class_list(rows, existing)
        if not errors and not to_create and eid:
            # Everyone on the list already has an account: just register them in this event.
            n = register_by_school_no(eid, numbers)
            flash(f"All {len(rows)} students already had accounts. {n} newly registered for this event.", "ok")
            return back_to_admin("students")
        if errors or not to_create:
            return render_template("accounts_check.html", election=election_state(), errors=errors,
                                   ev=event_state(eid) if eid else None,
                                   skipped=skipped, total=len(rows), text=text)
    created = []
    try:
        with db.get_pool().connection() as conn:
            with conn.transaction():
                for details in to_create:
                    password = st.new_password()
                    voter_id = insert_student(conn, details, password)
                    created.append(dict(details, voter_id=voter_id, password=password))
                    if eid:
                        conn.execute("""INSERT INTO event_voters (event_id, student_id)
                                        SELECT %s, id FROM students WHERE student_no = %s
                                        ON CONFLICT (event_id, student_id) DO NOTHING""", (eid, voter_id))
    except psycopg.errors.UniqueViolation:
        # Someone else created an account with one of these school numbers at the same moment.
        flash("One of these school student numbers was just registered by someone else. "
              "Nothing was created. Please try again.", "error")
        return back_to_admin("students")
    if eid and numbers:
        register_by_school_no(eid, numbers)            # students on the list who already had accounts
    clear_results_cache()
    log.info("admin created %d student accounts (%d skipped)%s", len(created), len(skipped),
             f", registered in event {eid}" if eid else "")
    return one_time_page(created=created, skipped=skipped, csv_data=accounts_csv(created))


@app.route("/admin/student/<int:student_id>", methods=["GET", "POST"])
@admin_required
def admin_student(student_id):
    """Edit one student's details (never the Voter ID, never the voting status)."""
    student = load_student(student_id)
    if student is None:
        abort(404)
    errors = []
    if request.method == "POST":
        admin_post()
        details, errors = st.clean_details({k: request.form.get(k, "") for k in st.CSV_COLUMNS})
        if not errors:
            other = db.query_one("SELECT full_name FROM students WHERE school_student_no = %s AND id <> %s",
                                 (details["school_student_no"], student_id))
            if other:
                errors.append(f"school student number {details['school_student_no']} already belongs "
                              f"to {other[0]}")
        if not errors:
            try:
                db.execute(
                    """UPDATE students SET school_student_no = %s, full_name = %s, course = %s,
                              department = %s, year_level = %s, section = %s WHERE id = %s""",
                    (details["school_student_no"], details["full_name"], details["course"],
                     details["department"], details["year"], details["section"], student_id))
                flash(f"Saved {details['full_name']}'s details.", "ok")
                return redirect(url_for("admin") + "#students")
            except psycopg.errors.UniqueViolation:
                errors.append("that school student number was just given to another student")
    return render_template("admin_student.html", election=election_state(), s=student,
                           form=request.form if request.method == "POST" else None, errors=errors)


@app.post("/admin/student/<int:student_id>/reset-password")
@admin_required
def admin_reset_password(student_id):
    """Give one student a new random password (shown once). The old one stops working."""
    admin_post()
    student = load_student(student_id)
    if student is None:
        abort(404)
    password = st.new_password()
    with db.get_pool().connection() as conn:
        with conn.transaction():
            # session_version + 1 logs the student out on every device that was logged in.
            conn.execute("""UPDATE students SET password_hash = %s, password_generated = TRUE,
                                   session_version = session_version + 1 WHERE id = %s""",
                         (generate_password_hash(password, method=PASSWORD_METHOD), student_id))
            conn.execute("DELETE FROM password_change_failures WHERE student_id = %s", (student_id,))
    log.info("admin reset the password of %s", student["student_no"])
    account = {"voter_id": student["student_no"], "password": password,
               "school_student_no": student["school_student_no"] or "", "full_name": student["full_name"],
               "course": student["course"] or "", "department": student["department"] or "",
               "year": student["year_level"] or "", "section": student["section"] or ""}
    return one_time_page(created=[account], skipped=[], csv_data=accounts_csv([account]), reset=True)


# ---------------------------------------------------------------- online registration
# Students register THEMSELVES for a CURRENT or UPCOMING event; COMELEC approves or rejects.
#   * New student: they choose their own password (hashed like every other password). No account
#     exists until COMELEC approves: approving creates the account with a system-generated Voter ID.
#   * Student who already has an account (or a request in another event): NO second account. They
#     type their EXISTING password and the request is linked to the same account / student number.
#   * One request per school student number per event: UNIQUE (event_id, school_student_no), plus a
#     per-student advisory lock so two submissions at the same instant are handled one at a time.

REG_LABELS = {"pending": "Pending", "approved": "Approved", "rejected": "Rejected", "removed": "Removed"}
REG_PENDING_MSG = "Your registration for this event is still pending approval."
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TRUST_PROXY = os.environ.get("TRUST_PROXY") == "1"     # behind Render's proxy: client IP is in X-Forwarded-For


def env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


# Per IP address, per 10 minutes. Classmates on the SAME school Wi-Fi share one public IP address:
# raise REGISTER_LIMIT on the server if a whole class registers at once from one network.
REGISTER_LIMIT = env_int("REGISTER_LIMIT", 10)        # registrations (and wrong existing-password tries)
STATUS_LIMIT = env_int("STATUS_LIMIT", 20)            # wrong student number / password on the status page
LOOKUP_LIMIT = env_int("LOOKUP_LIMIT", 60)            # "Continue" on step 1 of the form
LIMIT_WINDOW_MIN = 10


class RegError(Exception):
    """A registration problem to show the student. field = which form field it is about."""
    def __init__(self, message, field=None, code="invalid"):
        super().__init__(message)
        self.field, self.code = field, code


def client_ip():
    if TRUST_PROXY and request.access_route:
        return request.access_route[0]
    return request.remote_addr or "unknown"


def over_limit(kind, limit):
    """True if this IP already used `limit` attempts of this kind in the last 10 minutes (all workers)."""
    return db.query_one(
        """SELECT COUNT(*) >= %s FROM form_attempts
            WHERE kind = %s AND ip = %s AND at > now() - make_interval(mins => %s)""",
        (limit, kind, client_ip(), LIMIT_WINDOW_MIN))[0]


def count_attempt(kind):
    with db.get_pool().connection() as conn:
        conn.execute("INSERT INTO form_attempts (kind, ip) VALUES (%s, %s)", (kind, client_ip()))
        if secrets.randbelow(50) == 0:                        # now and then: forget old attempts
            conn.execute("DELETE FROM form_attempts WHERE at < now() - interval '1 day'")


def limit_message():
    return (f"Too many attempts from this network. Please wait {LIMIT_WINDOW_MIN} minutes and try again, "
            "or ask COMELEC for help.")


def clean_school_no(raw):
    number = "".join(str(raw or "").split())
    if not number:
        raise RegError("Please type your school student number.", "school_student_no")
    if not st.looks_like_school_no(number):
        raise RegError(f'"{number[:20]}" does not look like a school student number. '
                       f"Use the format {st.SCHOOL_NO_EXAMPLE}.", "school_student_no")
    return number


def registration_open(ev):
    if ev["status"] not in ("current", "upcoming"):
        raise RegError("Registration is closed: this voting event has ended.", code="closed")


def password_problems(password, confirm, school_no):
    problems = []
    if len(password) < PASSWORD_MIN:
        problems.append(("password", f"Your password must be at least {PASSWORD_MIN} characters."))
    elif len(password) > 128:
        problems.append(("password", "Your password is too long (max 128 characters)."))
    elif password.lower() in (school_no.lower(), school_no.replace("-", "").lower()):
        problems.append(("password", "Don't use your school student number as your password."))
    if password != confirm:
        problems.append(("confirm_password", "The password and the confirmation don't match."))
    return problems


ACCOUNT_LABELS = {"pending": "Pending verification", "verified": "Verified", "rejected": "Rejected"}
ACCOUNT_PENDING_MSG = "Your account is waiting for verification by the election committee."
ACCOUNT_EXISTS_MSG = "An account with this student number already exists. Log in instead."
DETAIL_FIELDS = {"full name": "full_name", "course": "course", "school/department": "department",
                 "section": "section", "year": "year"}


def create_account(form):
    """Create account (web form and RPC). The Voter ID is generated AT ONCE and returned.
    One account per school student number (UNIQUE index: two submissions at the same instant can't
    both succeed). Status: PENDING VERIFICATION until COMELEC verifies the student (once)."""
    school_no = clean_school_no(form.get("school_student_no"))
    details, errors = st.clean_details({**{k: form.get(k, "") for k in st.CSV_COLUMNS},
                                        "school_student_no": school_no})
    if errors:
        first = next((f for label, f in DETAIL_FIELDS.items() if errors[0].startswith(label)), None)
        raise RegError("Please check: " + "; ".join(errors) + ".", first)
    email = " ".join(str(form.get("email") or "").split())[:120]
    if email and not EMAIL_RE.match(email):
        raise RegError("That email address doesn't look right (you may leave it empty).", "email")
    password = str(form.get("password") or "")
    problems = password_problems(password, str(form.get("confirm_password") or ""), school_no)
    if problems:
        raise RegError(" ".join(x[1] for x in problems), problems[0][0])
    if db.query_one("SELECT 1 FROM students WHERE school_student_no = %s", (school_no,)):
        raise RegError(ACCOUNT_EXISTS_MSG, "school_student_no", code="exists")
    pw_hash = generate_password_hash(password, method=PASSWORD_METHOD)
    try:
        for _ in range(20):
            row = db.query_one(
                """INSERT INTO students (student_no, full_name, password_hash, password_generated, school_student_no,
                                         course, department, year_level, section, email, account_status, created_at)
                   VALUES (%s, %s, %s, FALSE, %s, %s, %s, %s, %s, %s, 'pending', now())
                   ON CONFLICT (student_no) DO NOTHING RETURNING id, student_no""",
                (st.new_voter_id(), details["full_name"], pw_hash, school_no, details["course"],
                 details["department"], details["year"], details["section"], email))
            if row:
                break
        else:
            raise RuntimeError("Could not find a free Voter ID")
    except psycopg.errors.UniqueViolation:           # the same student number at the same instant
        raise RegError(ACCOUNT_EXISTS_MSG, "school_student_no", code="exists")
    log.info("account created: %s (%s), pending verification", row[1], school_no)
    return {"student_id": row[0], "voter_id": row[1], "status": "pending", "name": details["full_name"],
            "school_student_no": school_no}


def account_status_info(school_no, password):
    """"Forgot your Voter ID?" and RPC get_account_status: Voter ID + account status, after the password."""
    row = db.query_one("""SELECT id, student_no, password_hash, full_name, account_status, account_reason
                            FROM students WHERE school_student_no = %s""", (school_no,))
    if not row or not check_password_hash(row[2], password):
        return None
    return {"voter_id": row[1], "name": row[3], "school_student_no": school_no, "status": row[4],
            "status_label": ACCOUNT_LABELS[row[4]], "reason": row[5] or "",
            "registrations": registrations_overview(school_no, row[0])}


def register_for_event(event_id, student):
    """One click "Register for this event" (logged-in student with a VERIFIED account; the details come
    from the account). Auto-approved when the event allows it ("Auto-approve registrations from
    verified accounts"), except after a rejection: then COMELEC decides again.
    Returns {"status": "approved" | "pending", "already", "auto_approved"}."""
    ev = event_state(event_id)
    registration_open(ev)
    if student["account_status"] != "verified":
        raise RegError(ACCOUNT_PENDING_MSG, code="not_verified")
    key = student["school_student_no"] or student["student_no"]
    with db.get_pool().connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('botoko-reg:' || %s))", (key,))
            if conn.execute("SELECT 1 FROM event_voters WHERE event_id = %s AND student_id = %s",
                            (event_id, student["id"])).fetchone():
                return {"status": "approved", "already": True, "auto_approved": False}
            mine = conn.execute(
                """SELECT id, status FROM registrations WHERE event_id = %s AND (student_id = %s OR school_student_no = %s)
                    ORDER BY id LIMIT 1""", (event_id, student["id"], key)).fetchone()
            if mine and mine[1] == "pending":
                return {"status": "pending", "already": True, "auto_approved": False}
            if mine and mine[1] == "approved":           # approved before, then removed by COMELEC
                raise RegError("COMELEC removed you from this event. Ask them if this is a mistake.", code="removed")
            auto = ev["auto_approve"] and mine is None
            status = "approved" if auto else "pending"
            vals = (student["id"], student["full_name"], student["course"] or "", student["department"] or "",
                    student["year_level"], student["section"] or "", status)
            if mine:                                      # rejected before: back to PENDING for COMELEC
                conn.execute(
                    """UPDATE registrations SET student_id = %s, full_name = %s, course = %s, department = %s,
                              year_level = %s, section = %s, status = %s, reason = '', submitted_at = now(),
                              decided_at = NULL, password_hash = NULL WHERE id = %s""", vals + (mine[0],))
            else:
                conn.execute(
                    """INSERT INTO registrations (event_id, school_student_no, student_id, full_name, course,
                                                  department, year_level, section, status, decided_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s = 'approved' THEN now() END)""",
                    (event_id, key) + vals + (status,))
            if auto:
                conn.execute("""INSERT INTO event_voters (event_id, student_id) VALUES (%s, %s)
                                ON CONFLICT (event_id, student_id) DO NOTHING""", (event_id, student["id"]))
    if auto:
        clear_results_cache(event_id)
    log.info("student %s registered for event %s: %s", student["student_no"], event_id,
             "auto-approved" if auto else "pending")
    return {"status": status, "already": False, "auto_approved": auto}


# ---- account verification (admin, once per student)

def account_counts():
    row = db.query_one(
        """SELECT COUNT(*) FILTER (WHERE account_status = 'pending'), COUNT(*) FILTER (WHERE account_status = 'verified'),
                  COUNT(*) FILTER (WHERE account_status = 'rejected'),
                  COALESCE(MAX(id) FILTER (WHERE account_status = 'pending'), 0) FROM students""")
    return {"pending": row[0], "verified": row[1], "rejected": row[2], "latest_id": row[3]}


def load_accounts():
    cols = ["id", "voter_id", "school_student_no", "full_name", "course", "department", "year_level", "section",
            "email", "status", "reason", "created", "decided"]
    return [dict(zip(cols, r)) for r in db.query_all(
        f"""SELECT id, student_no, school_student_no, full_name, course, department, year_level, section, email,
                   account_status, account_reason, to_char(created_at {_MNL}, 'Mon DD, HH12:MI AM'),
                   to_char(account_decided_at {_MNL}, 'Mon DD, HH12:MI AM')
              FROM students ORDER BY (account_status = 'pending') DESC, created_at DESC, id DESC""")]


def verify_accounts(ids=None):
    """Verify the given accounts (or ALL pending ones). Returns how many changed."""
    where, params = ("account_status = 'pending'", ()) if ids is None else ("id = ANY(%s) AND account_status <> 'verified'", (list(ids),))
    n = db.query_one(f"""WITH v AS (UPDATE students SET account_status = 'verified', account_reason = '',
                                           account_decided_at = now() WHERE {where} RETURNING 1)
                         SELECT COUNT(*) FROM v""", params)[0]
    log.info("admin verified %d account(s)", n)
    return n


def reject_accounts(ids, reason):
    reason = " ".join((reason or "").split())[:200]
    if not reason:
        raise RegError("Type the reason for rejecting (the student will see it).", "reason")
    n = db.query_one("""WITH r AS (UPDATE students SET account_status = 'rejected', account_reason = %s,
                                          account_decided_at = now()
                                    WHERE id = ANY(%s) AND account_status = 'pending' RETURNING 1)
                        SELECT COUNT(*) FROM r""", (reason, list(ids)))[0]
    log.info("admin rejected %d account(s)", n)
    return n


def registrations_overview(school_no, student_id):
    """Every event this student registered for (online) or was registered in (by COMELEC), with the
    registration status and the event status. Used by the status page and My Account."""
    rows = db.query_all(
        f"""SELECT e.id, e.name, {EVENT_STATUS_SQL}, r.status, r.reason,
                   to_char(r.submitted_at {_MNL}, {_SHOW}), to_char(r.decided_at {_MNL}, {_SHOW}),
                   v.student_id IS NOT NULL, COALESCE(v.has_voted, FALSE), to_char(v.voted_at {_MNL}, {_SHOW}),
                   to_char(e.starts_at {_MNL}, {_SHOW}), to_char(e.ends_at {_MNL}, {_SHOW})
              FROM events e
              LEFT JOIN registrations r ON r.event_id = e.id AND (r.school_student_no = %s OR r.student_id = %s)
              LEFT JOIN event_voters v ON v.event_id = e.id AND v.student_id = %s
             WHERE r.id IS NOT NULL OR v.student_id IS NOT NULL
             ORDER BY e.starts_at NULLS LAST, e.id""", (school_no or "", student_id or 0, student_id or 0))
    events = []
    for (eid, ename, ev_status, status, reason, submitted, decided, is_voter, voted, voted_at,
         starts, ends) in rows:
        if is_voter:
            status = "approved"                  # registered by COMELEC (class list) counts as approved
        elif status == "approved":
            status = "removed"                   # approved earlier, then unregistered by COMELEC
        events.append({"event_id": eid, "event": ename, "event_status": ev_status,
                       "event_status_label": STATUS_LABELS[ev_status], "status": status,
                       "label": REG_LABELS[status], "reason": reason or "",
                       "submitted_at": submitted, "decided_at": decided, "has_voted": bool(voted),
                       "voted_at": voted_at, "starts_txt": starts, "ends_txt": ends})
    return events


def registration_notice(event_id, student_id=None, school_no=None):
    """Login message for a student who is NOT on this event's voter list: pending / rejected / None."""
    row = db.query_one(
        """SELECT status, reason FROM registrations
            WHERE event_id = %s AND (student_id = %s OR school_student_no = %s)
            ORDER BY submitted_at DESC LIMIT 1""", (event_id, student_id or 0, school_no or ""))
    if row is None or row[0] == "approved":
        return None
    if row[0] == "pending":
        return {"kind": "pending", "title": "Registration pending", "text": REG_PENDING_MSG}
    return {"kind": "rejected", "title": "Registration rejected",
            "text": f"Your registration was rejected: {row[1] or 'no reason given'}"}


def approve_one(conn, event_id, reg_id):
    """Approve one request in its own transaction. A NEW student gets an account with a generated
    Voter ID (and the password they chose). Returns the Voter ID, or None if it was already approved."""
    with conn.transaction():
        found = conn.execute("SELECT school_student_no FROM registrations WHERE id = %s AND event_id = %s",
                             (reg_id, event_id)).fetchone()
        if found is None:
            return None
        school_no = found[0]
        # Same lock as register_for_event: one student number at a time.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('botoko-reg:' || %s))", (school_no,))
        student_id, full_name, course, department, year, section, pw_hash, status = conn.execute(
            """SELECT student_id, full_name, course, department, year_level, section, password_hash, status
                 FROM registrations WHERE id = %s FOR UPDATE""", (reg_id,)).fetchone()
        if status == "approved":
            return None
        if student_id is None:
            acct = conn.execute("SELECT id FROM students WHERE school_student_no = %s", (school_no,)).fetchone()
            if acct:
                student_id = acct[0]
            else:
                if not pw_hash:
                    raise RuntimeError(f"registration {reg_id} has no password")
                for _ in range(20):
                    row = conn.execute(
                        """INSERT INTO students (student_no, full_name, password_hash, password_generated,
                                                 school_student_no, course, department, year_level, section)
                           VALUES (%s, %s, %s, FALSE, %s, %s, %s, %s, %s)
                           ON CONFLICT (student_no) DO NOTHING RETURNING id""",
                        (st.new_voter_id(), full_name, pw_hash, school_no, course, department, year, section)).fetchone()
                    if row:
                        student_id = row[0]
                        break
                else:
                    raise RuntimeError("Could not find a free Voter ID")
            # The account now holds the password: link this student's other requests and clear their copies.
            conn.execute("""UPDATE registrations SET student_id = %s, password_hash = NULL
                             WHERE school_student_no = %s""", (student_id, school_no))
        conn.execute("""INSERT INTO event_voters (event_id, student_id) VALUES (%s, %s)
                        ON CONFLICT (event_id, student_id) DO NOTHING""", (event_id, student_id))
        conn.execute("""UPDATE registrations SET status = 'approved', reason = '', decided_at = now(),
                               student_id = %s, password_hash = NULL WHERE id = %s""", (student_id, reg_id))
        return conn.execute("SELECT student_no FROM students WHERE id = %s", (student_id,)).fetchone()[0]


def approve_registrations(event_id, ids=None):
    """Approve the given requests (or ALL pending ones of the event). Returns how many were approved."""
    if ids is None:
        ids = [r[0] for r in db.query_all(
            "SELECT id FROM registrations WHERE event_id = %s AND status = 'pending' ORDER BY id", (event_id,))]
    done = 0
    with db.get_pool().connection() as conn:
        for reg_id in ids:
            if approve_one(conn, event_id, reg_id):
                done += 1
    if done:
        clear_results_cache(event_id)
        log.info("admin approved %d registration(s) for event %s", done, event_id)
    return done


def reject_registrations(event_id, ids, reason):
    """Reject PENDING requests with a reason (the student sees it on the status page and at login)."""
    reason = " ".join((reason or "").split())[:200]
    if not reason:
        raise RegError("Type the reason for rejecting (the student will see it).", "reason")
    n = db.query_one(
        """WITH r AS (UPDATE registrations SET status = 'rejected', reason = %s, decided_at = now()
                       WHERE event_id = %s AND id = ANY(%s) AND status = 'pending' RETURNING 1)
           SELECT COUNT(*) FROM r""", (reason, event_id, list(ids)))[0]
    log.info("admin rejected %d registration(s) for event %s", n, event_id)
    return n


def registration_counts(event_id):
    row = db.query_one(
        """SELECT COUNT(*) FILTER (WHERE status = 'pending'), COUNT(*) FILTER (WHERE status = 'approved'),
                  COUNT(*) FILTER (WHERE status = 'rejected'), COALESCE(MAX(id), 0)
             FROM registrations WHERE event_id = %s""", (event_id,))
    return {"pending": row[0], "approved": row[1], "rejected": row[2], "latest_id": row[3]}


def load_registrations(event_id):
    cols = ["id", "school_student_no", "full_name", "course", "department", "year_level", "section", "email",
            "status", "reason", "submitted", "decided", "existing", "voter_id"]
    return [dict(zip(cols, r)) for r in db.query_all(
        f"""SELECT r.id, r.school_student_no, r.full_name, r.course, r.department, r.year_level, r.section,
                   r.email, r.status, r.reason, to_char(r.submitted_at {_MNL}, 'Mon DD, HH12:MI AM'),
                   to_char(r.decided_at {_MNL}, 'Mon DD, HH12:MI AM'), r.student_id IS NOT NULL, s.student_no
              FROM registrations r LEFT JOIN students s ON s.id = r.student_id
             WHERE r.event_id = %s
             ORDER BY (r.status = 'pending') DESC, r.submitted_at DESC""", (event_id,))]


# ---- create account, forgot Voter ID, one-click event registration (students)

ACCOUNT_FIELDS = ["school_student_no", "full_name", "course", "department", "year", "section", "email"]


@app.route("/account/create", methods=["GET", "POST"])
def create_account_page():
    """Create account: details + own password. The Voter ID is shown at once (big, with a copy button).
    Status: PENDING VERIFICATION until COMELEC verifies the account."""
    if session.get("is_admin"):
        return redirect(url_for("admin"))
    if session.get("student_id"):
        return redirect(url_for("my_account"))
    form, error, field, created = {k: "" for k in ACCOUNT_FIELDS}, None, None, None
    if request.method == "POST":
        check_csrf()
        form = {k: request.form.get(k, "") for k in ACCOUNT_FIELDS}
        try:
            if over_limit("register", REGISTER_LIMIT):
                raise RegError(limit_message(), code="rate_limited")
            created = create_account(dict(form, password=request.form.get("password", ""),
                                          confirm_password=request.form.get("confirm_password", "")))
            count_attempt("register")
        except RegError as e:
            error, field = str(e), e.field
    resp = make_response(render_template("create_account.html", election=election_state(), form=form,
                                         error=error, field=field, created=created,
                                         exists=bool(error and error == ACCOUNT_EXISTS_MSG)))
    resp.headers["Cache-Control"] = "no-store"          # shows the new Voter ID: never keep a copy
    return resp


@app.route("/forgot", methods=["GET", "POST"])
def forgot_voter_id():
    """Forgot your Voter ID? Student number + password -> the Voter ID and the account status."""
    result, error, school_no = None, None, ""
    if request.method == "POST":
        check_csrf()
        school_no = request.form.get("school_student_no", "").strip()[:20]
        try:
            school_no = clean_school_no(school_no)
            if over_limit("status", STATUS_LIMIT):
                raise RegError(limit_message())
            result = account_status_info(school_no, request.form.get("password", ""))
            if result is None:
                count_attempt("status")
                raise RegError("Wrong student number or password. Use the password you chose when you created "
                               "your account.")
        except RegError as e:
            error = str(e)
    resp = make_response(render_template("forgot.html", election=election_state(), result=result, error=error,
                                         school_no=school_no))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/e/<int:event_id>/register", methods=["GET", "POST"])
@verified_required()
def event_register(event_id):
    """One click "Register for this event" (POST + CSRF). The old per-event details form is gone:
    GET goes back to the event list."""
    ev = event_or_404(event_id)
    back = url_for("index") + "#" + ev["status"]
    if request.method == "GET":
        return redirect(back)
    check_csrf()
    try:
        r = register_for_event(event_id, g.student)
    except RegError as e:
        flash(str(e), "error")
        return redirect(back)
    if r["status"] == "approved" and not r["already"]:
        flash(f"You're registered for {ev['name']}." + (" You can vote now." if ev["status"] == "current" else
              " You can vote when voting opens."), "ok")
    elif r["status"] == "approved":
        flash(f"You're already registered for {ev['name']}.", "ok")
    else:
        flash(f"Registration for {ev['name']} sent. Status: PENDING. Wait for the election committee "
              "(COMELEC) to approve it.", "ok")
    return redirect(back)


@app.route("/register/status", methods=["GET", "POST"])
def register_status():
    """Old "Check registration status" page -> "Forgot your Voter ID?" (Voter ID + account status)."""
    return redirect(url_for("forgot_voter_id"), code=303)


@app.route("/register")
def register_choose():
    """Old "Register" page -> Create account (logged in: the event list, where Register is one click)."""
    return redirect(url_for("index") if session.get("student_id") else url_for("create_account_page"))


# ---- registrations (admin)

@app.get("/admin/api/registrations")
@admin_required
def admin_api_registrations():
    """Live counts for the Accounts + Registrations tab badges (admin.js asks every 4 seconds while the tab is visible).
    ?rows=1 also sends the PENDING rows (drawn by the same template macros as the page, so admin.js can add the new
    ones without reloading) and the status of every row (so rows approved / rejected elsewhere can be removed)."""
    eid = request.args.get("event", type=int) or session.get("admin_event")
    if not eid:
        return jsonify({"error": "no event"}), 400
    out = dict(registration_counts(eid), accounts=account_counts())
    if request.args.get("rows") == "1":
        acc_row = get_template_attribute("_admin_rows.html", "acc_row")
        reg_row = get_template_attribute("_admin_rows.html", "reg_row")
        accounts, regs = load_accounts(), load_registrations(eid)
        out["acc_rows"] = [{"id": a["id"], "html": str(acc_row(a))} for a in accounts if a["status"] == "pending"]
        out["acc_status"] = {str(a["id"]): a["status"] for a in accounts}
        out["reg_rows"] = [{"id": r["id"], "html": str(reg_row(r))} for r in regs if r["status"] == "pending"]
        out["reg_status"] = {str(r["id"]): r["status"] for r in regs}
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def reg_ids_field():
    return [int(x) for x in request.form.getlist("reg_id") if x.isdigit()]


@app.post("/admin/registrations/approve")
@admin_required
def admin_approve_registrations():
    admin_post()
    eid = admin_eid()
    if request.form.get("all") == "1":
        n = approve_registrations(eid)
        flash(f"Approved all {n} pending registration{'s' if n != 1 else ''}." if n else
              "There were no pending registrations to approve.", "ok" if n else "info")
    else:
        one = request.form.get("one", type=int)
        ids = [one] if one else reg_ids_field()
        if not ids:
            flash("Select at least one registration first.", "error")
            return back_to_admin("registrations")
        n = approve_registrations(eid, ids)
        flash(f"Approved {n} registration{'s' if n != 1 else ''}. "
              "New students got a Voter ID and can now log in with their own password.", "ok")
    return back_to_admin("registrations")


# ---- official results: publish / unpublish, printable report, CSV

@app.post("/admin/results/publish")
@admin_required
def admin_publish_results():
    """Publish the OFFICIAL results of a finished event: from now on students see the final results
    and the winners. Only possible after voting has ended."""
    admin_post()
    eid = admin_eid()
    ev = event_state(eid)
    if ev["status"] != "previous":
        flash("Results can be published only after voting has ended (close the voting first).", "error")
        return back_to_admin("overview")
    db.execute("UPDATE events SET results_published = TRUE, results_published_at = now() WHERE id = %s", (eid,))
    log.info("admin published the official results of event %s", eid)
    flash(f"The official results of {ev['name']} are published. Students can now see the winners.", "ok")
    return back_to_admin("overview")


@app.post("/admin/results/unpublish")
@admin_required
def admin_unpublish_results():
    """Undo a publish by mistake: students see "results will be announced soon" again. Also turns
    off automatic publishing for this event (otherwise the results would stay visible)."""
    admin_post()
    eid = admin_eid()
    db.execute("""UPDATE events SET results_published = FALSE, results_published_at = NULL, auto_publish = FALSE
                   WHERE id = %s""", (eid,))
    log.info("admin unpublished the results of event %s", eid)
    flash("Results unpublished. Students see \"The official results will be announced soon\" again. "
          "(Automatic publishing is now off for this event.)", "ok")
    return back_to_admin("overview")


@app.route("/admin/report")
@admin_required
def admin_results_report():
    """Official results report (print / save as PDF): event, voting start and end, registered voters,
    total votes, turnout, full results per position, winners, signature line for the COMELEC chair."""
    eid = request.args.get("event", type=int) or session.get("admin_event")
    ev = event_or_404(eid)
    data = compute_results(eid)
    return render_template("report.html", ev=ev, data=data, winners=winners_summary(data),
                           printed=time.strftime("%B %d, %Y  %I:%M %p"))


@app.route("/admin/results.csv")
@admin_required
def admin_results_csv():
    """Full results as a spreadsheet (one row per candidate + one 'No vote' row per position)."""
    eid = request.args.get("event", type=int) or session.get("admin_event")
    ev = event_or_404(eid)
    data = compute_results(eid)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["event", "status", "voting_started", "voting_ended", "registered_voters", "ballots_cast", "turnout_percent"])
    turnout = round(100 * data["ballots"] / data["registered"], 1) if data["registered"] else 0
    w.writerow([ev["name"], "official (published)" if ev["published"] else
                ("final, NOT yet published" if ev["status"] == "previous" else "live (voting open)"),
                ev["starts_txt"] or "", ev["ends_txt"] or "", data["registered"], data["ballots"], turnout])
    w.writerow([])
    w.writerow(["position", "seats", "rank", "candidate", "party", "votes", "percent_of_ballots", "result"])
    for p in winners_summary(data):
        for c in p["candidates"]:
            w.writerow([p["name"], p["seats"], c["rank"], c["name"], c["party"], c["votes"], c["pct"],
                        {"win": "Winner", "tie": "Tie for last seat"}.get(c.get("lead"), "")])
        w.writerow([p["name"], p["seats"], "", "No vote (skipped)", "", p["abstain"], "", ""])
    name = "".join(ch if ch.isalnum() else "-" for ch in ev["name"]).strip("-").lower() or "results"
    resp = make_response("\ufeff" + out.getvalue())          # BOM: Excel reads names like "Peña" right
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="botoko-results-{name}.csv"'
    return resp


@app.post("/admin/accounts/verify")
@admin_required
def admin_verify_accounts():
    """Verify student accounts (once per student): one, the selected ones, or all pending."""
    admin_post()
    if request.form.get("all") == "1":
        n = verify_accounts()
        flash(f"Verified all {n} pending account{'s' if n != 1 else ''}." if n else "There were no pending accounts.",
              "ok" if n else "info")
    else:
        one = request.form.get("one", type=int)
        ids = [one] if one else [int(x) for x in request.form.getlist("acc_id") if x.isdigit()]
        if not ids:
            flash("Select at least one account first.", "error")
            return back_to_admin("accounts")
        n = verify_accounts(ids)
        flash(f"Verified {n} account{'s' if n != 1 else ''}. They can now register for events and vote.", "ok")
    return back_to_admin("accounts")


@app.post("/admin/accounts/reject")
@admin_required
def admin_reject_accounts():
    admin_post()
    one = request.form.get("one", type=int)
    ids = [one] if one else [int(x) for x in request.form.getlist("acc_id") if x.isdigit()]
    if not ids:
        flash("Select at least one account first.", "error")
        return back_to_admin("accounts")
    try:
        n = reject_accounts(ids, request.form.get("reason", ""))
        flash(f"Rejected {n} account{'s' if n != 1 else ''}. The student sees your reason.", "ok")
    except RegError as e:
        flash(str(e), "error")
    return back_to_admin("accounts")


@app.post("/admin/registrations/reject")
@admin_required
def admin_reject_registrations():
    admin_post()
    eid = admin_eid()
    one = request.form.get("one", type=int)
    ids = [one] if one else reg_ids_field()
    if not ids:
        flash("Select at least one registration first.", "error")
        return back_to_admin("registrations")
    try:
        n = reject_registrations(eid, ids, request.form.get("reason", ""))
        flash(f"Rejected {n} registration{'s' if n != 1 else ''}. The student sees your reason.", "ok")
    except RegError as e:
        flash(str(e), "error")
    return back_to_admin("registrations")


@app.route("/favicon.ico")
def favicon():
    return redirect(asset("favicon.ico"))


# ---------------------------------------------------------------- errors
# Each error gets a friendly page. "kind" picks the icon and buttons in error.html.

def error_page(status, kind, title, message):
    return render_template("error.html", kind=kind, title=title, message=message), status


RETRY_MESSAGE = "This page was open too long or your login changed in another tab. Please try again."


def same_site_referrer():
    """The page the form was sent from, if it is one of OUR pages (never another website)."""
    ref = request.referrer
    if not ref:
        return None
    u = urllib.parse.urlparse(ref)
    if u.netloc != request.host or not u.path.startswith("/") or u.path.startswith("//"):
        return None
    return u.path + ("?" + u.query if u.query else "")


@app.errorhandler(CsrfError)
def csrf_failed(e):
    """Wrong / missing CSRF token: nothing from the form was used (still rejected). Instead of an error page:
      - login form: the login page again with a fresh token ("Please log in again."); if someone is already
        logged in in this browser, it says so (Continue / Log out);
      - any other form: back to the SAME page with a fresh token and a friendly message. (The ballot's
        choices are kept in this tab: static/ballot.js.)"""
    who, label = who_am_i()
    log.info("CSRF check failed on %s %s (session: %s, from %s)", request.method, request.path, who or "not logged in",
             request.referrer or "-")
    if request.endpoint == "login":
        if who:
            continue_url = url_for("admin") if who == "admin" else url_for("index")
            resp = make_response(render_template("signin.html", election=election_state(), student_no="", next=None,
                                                 already={"label": label, "continue_url": continue_url,
                                                          "is_admin": who == "admin"}))
            resp.headers["Cache-Control"] = "no-store"
            return resp
        flash("Please log in again.", "relogin")
        nxt = safe_next(request.form.get("next"))
        return redirect(url_for("login", next=nxt) if nxt else url_for("login"), code=303)
    if request.endpoint == "logout" and not who:
        flash("You have been logged out.", "ok")
        return redirect(url_for("login"), code=303)
    flash(RETRY_MESSAGE, "error")
    target = same_site_referrer()
    if not target:
        target = url_for("admin") if who == "admin" else (url_for("index") if who else url_for("login"))
    tab = request.form.get("tab")
    if tab in ADMIN_TABS and target.startswith("/admin"):
        target = target.split("#")[0] + "#" + tab                  # re-open the admin tab the form was on
    return redirect(target, code=303)


@app.errorhandler(400)
def bad_request(e):
    message = getattr(e, "description", "") or "Bad request."
    if "session expired" in message.lower():
        return error_page(400, "expired", "Your session expired",
                          "For your security, this page expired before it was sent. Nothing was "
                          "submitted from it. Please start again.")
    return error_page(400, "generic", "Something went wrong", message)


@app.errorhandler(403)
def forbidden(e):
    return error_page(403, "generic", "Not allowed", "You don't have permission to do that.")


@app.errorhandler(404)
def not_found(e):
    return error_page(404, "notfound", "Page not found",
                      "We couldn't find that page. Check the link, or go back to the start.")


@app.errorhandler(413)
def too_large(e):
    # Only possible when a big photo is sent without our page shrinking it first.
    if request.path.startswith("/admin") and session.get("is_admin"):
        flash("That file is too big to upload. Choose the photo again in the Ballot tab: "
              "the page shrinks it automatically.", "error")
        return redirect(url_for("admin") + "#ballot")
    return error_page(413, "generic", "File too big", "That upload is too big (limit 1 MB).")


@app.errorhandler(405)
def method_not_allowed(e):
    return error_page(405, "generic", "That didn't work",
                      "That page can't be opened this way. Please go back to the start.")


@app.errorhandler(Exception)
def server_error(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    log.exception("Unhandled error")
    html, status = error_page(503, "busy", "Server is busy",
                              "Many students are using BotoKo right now. Please wait a few seconds "
                              "and try again. If you were submitting your vote, open BotoKo again: "
                              "your receipt will appear if the vote was recorded.")
    return html, status, {"Retry-After": "5"}


# ---------------------------------------------------------------- RPC (client-server, JSON-RPC 2.0)
# POST /rpc runs remote procedures (get_ballot, cast_vote, get_results, ...) that call the SAME
# functions as the pages above. See rpc.py. The module object is passed in (instead of rpc.py
# importing app.py) so it also works when this file runs as "py app.py" (module __main__).
import rpc  # noqa: E402

rpc.init_app(app, sys.modules[__name__])


def warn_if_weak_admin_password():
    """Printed once when the server starts (serve.py, py app.py, gunicorn)."""
    if not ADMIN_PASSWORD:
        print("NOTE: ADMIN_PASSWORD is not set, so nobody can log in as admin.", flush=True)
    elif ADMIN_PASSWORD_WEAK:
        print("\n" + "!" * 100 + "\n" + WEAK_ADMIN_WARNING + "\n" + "!" * 100 + "\n", flush=True)


def check_database_or_exit():
    """Before starting a server: one clear message if DATABASE_URL is missing or wrong
    (instead of endless connection retries). Used by "py app.py" and serve.py."""
    warn_if_weak_admin_password()
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit(
            "DATABASE_URL is not set, so BotoKo can't reach PostgreSQL.\n"
            "Set it in THIS terminal first, then run again:\n"
            '  PowerShell:  $env:DATABASE_URL = "postgresql://postgres:YOUR_PASSWORD@localhost:5432/botoko"\n'
            "  cmd.exe:     set DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost:5432/botoko\n"
            "(Also set ADMIN_PASSWORD the same way to use the admin page.)")
    # Try the database once before starting, so a wrong setting gives one clear message
    # instead of endless connection retries.
    try:
        psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5).close()
    except psycopg.OperationalError as e:
        reason = str(e)
        if "password authentication failed" in reason:
            hint = ("The PostgreSQL user name or password in DATABASE_URL is wrong.\n"
                    "  - Did you replace YOUR_PASSWORD with your real password?\n"
                    "  - If the password has special characters (@ : / # % ? & or spaces), encode it:\n"
                    "      $pw = [uri]::EscapeDataString('your password here')\n"
                    '      $env:DATABASE_URL = "postgresql://postgres:$pw@localhost:5432/botoko"')
        elif "does not exist" in reason:
            hint = ("The database in DATABASE_URL doesn't exist yet. Create it once, e.g.:\n"
                    '  & "C:\\Program Files\\PostgreSQL\\18\\bin\\createdb.exe" -U postgres botoko')
        elif "refused" in reason or "timeout" in reason.lower():
            hint = ("PostgreSQL isn't running on that host/port. Start the PostgreSQL service "
                    "(Windows: Services > postgresql-x64-18 > Start) or check the port in DATABASE_URL.")
        else:
            hint = "Check DATABASE_URL."
        raise SystemExit(f"Can't connect to PostgreSQL.\n{hint}\n\nDetails: {reason.strip().splitlines()[-1]}")


if __name__ == "__main__":
    # Simple local mode: Flask's development server = 1 process (uses 1 CPU core).
    # For 1 worker process per CPU core on Windows use run-local.ps1 (serve.py);
    # in production (Render) use: gunicorn -c gunicorn.conf.py app:app
    check_database_or_exit()
    p = concurrency.plan()
    print(f"BotoKo: {p['cores']} CPU cores detected -> Flask development server: 1 process "
          f"(run-local.ps1 starts {p['workers']} worker processes x {p['threads']} threads)", flush=True)
    app.config["SESSION_COOKIE_SECURE"] = False
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True, debug=False)
