"""BotoKo RPC layer: JSON-RPC 2.0 over HTTP (client-server model + Remote Procedure Call).

CLIENT                                   SERVER (this file, inside every worker process)
browser: static/rpc.js  ─┐   POST /rpc   ┌─ dispatcher: check JSON-RPC format + CSRF + login
python:  rpc_client.py  ─┴──────────────►├─ registry:   "cast_vote" -> cast_vote_rpc(choices)
                          ◄──────────────┘  run the procedure, reply {"result": ...} or {"error": ...}

  Request:   {"jsonrpc": "2.0", "method": "cast_vote", "params": {"event_id": 2, "choices": {"1": 3, "5": [7, 8]}}, "id": 7}
  Response:  {"jsonrpc": "2.0", "result": {"receipt_code": "9EE95D53", ...}, "id": 7,
              "meta": {"worker_pid": 18264, "thread_name": "waitress-3", "duration_ms": 12}}

- One endpoint, POST /rpc. GET /rpc describes the service (all procedures) and gives the caller a
  CSRF token, so a program that is not a web page (rpc_client.py) can call procedures too.
- Every remote procedure is a normal Python function registered with @rpc_method("name", role=...).
  Its parameters are checked against the function signature (unknown/missing -> error -32602).
- Authentication: the normal BotoKo login session cookie (student login or admin login).
  Every POST /rpc must send the session's CSRF token in the X-CSRF-Token header.
- The procedures call the SAME functions as the web pages, e.g. cast_vote() with its
  transaction + SELECT ... FOR UPDATE lock, unchanged.
- Each response has a "meta" field: the worker process (PID) and thread that ran the procedure.
"""
import hmac
import inspect
import json
import logging
import os
import threading
import time

import psycopg
from flask import Blueprint, Response, g, jsonify, request, session, url_for

log = logging.getLogger("botoko.rpc")
bp = Blueprint("rpc", __name__)
core = None                 # the app module (app.py), set by init_app()

# ---- JSON-RPC 2.0 error codes (standard) ----
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
# ---- BotoKo application errors (the -32000..-32099 range is reserved for servers) ----
NOT_LOGGED_IN = -32001      # no student/admin session, or it expired
ADMIN_ONLY = -32002         # admin procedure called without an admin session
CSRF_FAILED = -32003        # X-CSRF-Token header missing or wrong
VOTING_CLOSED = -32010
ALREADY_VOTED = -32011
INVALID_VOTE = -32012       # candidate doesn't exist or isn't running for that position
NOT_FOUND = -32013          # e.g. receipt code that isn't yours
PASSWORD_CHANGE_REQUIRED = -32014
NOT_REGISTERED = -32015       # logged in, but not registered for this voting event
NOT_STARTED = -32016          # upcoming event: no ballot or results yet
NO_EVENT = -32017             # no voting event with that id
REGISTRATION_REFUSED = -32018  # registration: invalid details, wrong existing password, or event ended
TOO_MANY_ATTEMPTS = -32019     # rate limit per IP address (create account / Voter ID lookup)
ACCOUNT_NOT_VERIFIED = -32020  # the student's account is still waiting for COMELEC (or was rejected)
RESULTS_NOT_PUBLISHED = -32021 # voting ended, COMELEC hasn't published the official results yet

METHODS = {}                # name -> {"func", "role", "doc", "params"}


class RpcError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def rpc_method(name, role="public"):
    """Register a Python function as a remote procedure. role: "public", "student" or "admin"."""
    def register(func):
        sig = inspect.signature(func)
        METHODS[name] = {"func": func, "role": role, "signature": sig,
                         "doc": (inspect.getdoc(func) or "").split("\n")[0],
                         "params": list(sig.parameters)}
        return func
    return register


def init_app(app, app_module):
    """Called once at the end of app.py: connect the RPC layer to the app."""
    global core
    core = app_module
    app.register_blueprint(bp)


# ---------------------------------------------------------------- dispatcher

def _caller():
    if session.get("is_admin"):
        return "admin"
    return session.get("student_no") or "guest"


def _require_student():
    """The same checks as @login_required on the web pages, as JSON-RPC errors."""
    sid = session.get("student_id")
    if not sid:
        raise RpcError(NOT_LOGGED_IN, "Please log in first.")
    student = core.load_student(sid)
    if student is None:
        session.clear()
        raise RpcError(NOT_LOGGED_IN, "This account no longer exists. Please log in again.")
    if session.get("sv", 0) != student["session_version"]:
        session.clear()
        raise RpcError(NOT_LOGGED_IN, "You were logged out because your password was changed. Please log in again.")
    g.student = student
    return student


def _require_admin():
    if not session.get("is_admin"):
        raise RpcError(ADMIN_ONLY, "Only a logged-in COMELEC admin can call this procedure.")


def _csrf_ok():
    sent = request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf", "")
    return bool(sent) and bool(expected) and hmac.compare_digest(sent, expected)


def _error(code, message, req_id=None, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "error": err, "id": req_id}


NO_WORKER_META = {"get_results"}       # replies without worker PID / thread in "meta" (unless admin)


def _meta(started):
    return {"worker_pid": os.getpid(), "thread_name": threading.current_thread().name,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1)}


def _bind(spec, params):
    """JSON-RPC params (object = by name, array = by position, missing = none) -> keyword arguments."""
    if params is None:
        params = {}
    try:
        if isinstance(params, dict):
            bound = spec["signature"].bind(**params)
        elif isinstance(params, list):
            bound = spec["signature"].bind(*params)
        else:
            raise RpcError(INVALID_PARAMS, "params must be an object or an array.")
    except TypeError as e:
        raise RpcError(INVALID_PARAMS, f"Invalid params: {e}. Expected: {', '.join(spec['params']) or 'no params'}.")
    return bound.arguments


def _handle_one(req, csrf_valid):
    """Run one JSON-RPC request object. Returns the response dict, or None for a notification."""
    started = time.perf_counter()
    if not isinstance(req, dict):
        return _error(INVALID_REQUEST, "Invalid Request: expected a JSON object.")
    is_notification = "id" not in req
    req_id = req.get("id")
    if req_id is not None and (isinstance(req_id, bool) or not isinstance(req_id, (str, int, float))):
        return _error(INVALID_REQUEST, "Invalid Request: id must be a string, a number or null.")
    method = req.get("method")
    status = "ok"
    try:
        if req.get("jsonrpc") != "2.0" or not isinstance(method, str):
            raise RpcError(INVALID_REQUEST, 'Invalid Request: needs "jsonrpc": "2.0" and a "method" string.')
        spec = METHODS.get(method)
        if spec is None:
            raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
        if not csrf_valid:
            raise RpcError(CSRF_FAILED, "Missing or wrong X-CSRF-Token header. Get a token with GET /rpc "
                                        "(or reload the page) and send it with every call.")
        kwargs = _bind(spec, req.get("params"))
        if spec["role"] == "student":
            _require_student()
        elif spec["role"] == "admin":
            _require_admin()
        result = spec["func"](**kwargs)
        response = {"jsonrpc": "2.0", "result": result, "id": req_id}
    except RpcError as e:
        status = f"error {e.code} {e.message}"
        response = _error(e.code, e.message, req_id, e.data)
    except Exception:                          # never leak internals to the caller
        log.exception("RPC %s failed", method)
        status = f"error {INTERNAL_ERROR} internal"
        response = _error(INTERNAL_ERROR, "Internal error. Please try again.", req_id)
    m = _meta(started)
    # Students / the public calling get_results don't see which worker process or thread answered.
    response["meta"] = m if (method not in NO_WORKER_META or session.get("is_admin")) else {"duration_ms": m["duration_ms"]}
    log.info("RPC %s by %s -> %s (pid %s, %s, %s ms)", method, _caller(), status,
             m["worker_pid"], m["thread_name"], round(m["duration_ms"]))
    return None if is_notification else response


@bp.post("/rpc")
def rpc_endpoint():
    """The one RPC endpoint. Accepts a single request, or a batch (JSON array) of requests."""
    if not request.is_json:
        return jsonify(_error(INVALID_REQUEST, "Send the request as JSON (Content-Type: application/json).")), 415
    try:
        payload = json.loads(request.get_data(as_text=True) or "")
    except ValueError:
        return jsonify(_error(PARSE_ERROR, "Parse error: the body is not valid JSON."))
    csrf_valid = _csrf_ok()
    if isinstance(payload, list):
        if not payload:
            return jsonify(_error(INVALID_REQUEST, "Invalid Request: empty batch."))
        if len(payload) > 20:
            return jsonify(_error(INVALID_REQUEST, "Invalid Request: at most 20 calls per batch."))
        replies = [r for r in (_handle_one(item, csrf_valid) for item in payload) if r is not None]
        return jsonify(replies) if replies else Response(status=204)
    reply = _handle_one(payload, csrf_valid)
    return jsonify(reply) if reply is not None else Response(status=204)


@bp.get("/rpc")
def rpc_describe():
    """Service description: every remote procedure, and a CSRF token for this session."""
    return jsonify({
        "service": "BotoKo JSON-RPC 2.0",
        "endpoint": url_for("rpc.rpc_endpoint"),
        "csrf_token": core.csrf_token(),
        "caller": _caller(),
        "methods": [{"name": n, "params": s["params"], "who": s["role"], "description": s["doc"]}
                    for n, s in sorted(METHODS.items())],
    })


# ---------------------------------------------------------------- parameter checks

def _int_id(value, what):
    if isinstance(value, bool):
        raise RpcError(INVALID_PARAMS, f"{what} must be a number.")
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit() and int(value) > 0:
        return int(value)
    raise RpcError(INVALID_PARAMS, f"{what} must be a positive whole number, got {value!r}.")


def _code(value, what):
    """8-character receipt / verification code (0-9, A-F), case and spaces ignored."""
    if not isinstance(value, str):
        raise RpcError(INVALID_PARAMS, f"{what} must be a string.")
    code = "".join(value.split()).replace("-", "").upper()
    if len(code) != 8 or any(ch not in "0123456789ABCDEF" for ch in code):
        raise RpcError(INVALID_PARAMS, f"{what} must have 8 characters (0-9, A-F), e.g. 9EE95D53.")
    return code


# ---------------------------------------------------------------- events

def _event(event_id, *, allow_upcoming=True):
    """Resolve the event for a call: the given event_id, else the student's/admin's current event.
    Raises NO_EVENT / NOT_STARTED as JSON-RPC errors."""
    if event_id is None:
        event_id = session.get("event_id") if not session.get("is_admin") else session.get("admin_event")
        if event_id is None:
            event_id = core.single_current_event()
        if event_id is None:
            raise RpcError(INVALID_PARAMS, "Invalid params: event_id is required (see get_events).")
    eid = _int_id(event_id, "event_id")
    try:
        ev = core.event_state(eid)
    except core.EventNotFound:
        raise RpcError(NO_EVENT, f"There is no voting event with id {eid}.")
    if not allow_upcoming and ev["status"] == "upcoming":
        raise RpcError(NOT_STARTED, f"\"{ev['name']}\" has not started yet (voting starts {ev['starts_txt'] or 'later'}).",
                       {"starts_at": ev["starts_txt"], "starts_ms": ev["starts_ms"]})
    return ev


def _event_public(ev):
    """What anyone may see about an event. Upcoming events: only what it is for and when."""
    out = {"id": ev["id"], "name": ev["name"], "description": ev["description"], "status": ev["status"],
           "starts_at": ev["starts_txt"], "ends_at": ev["ends_txt"], "starts_ms": ev["starts_ms"], "ends_ms": ev["ends_ms"]}
    if ev["status"] != "upcoming":
        out.update(organization=ev["organization"], registered=ev["registered"], ballots=ev["ballots"])
    return out


@rpc_method("get_events")
def get_events():
    """All voting events: current, upcoming and previous (status computed from the dates)."""
    return [_event_public(ev) for ev in core.load_events()]


# ---------------------------------------------------------------- remote procedures: anyone

@rpc_method("get_results")
def get_results(event_id=None):
    """Live tally of one event: votes per candidate, winners/leaders (top N for multi-seat), no-vote counts."""
    ev = _event(event_id, allow_upcoming=False)
    if not core.results_visible(ev):
        raise RpcError(RESULTS_NOT_PUBLISHED, "Voting has ended. The official results will be announced by the "
                                              "election committee soon.", {"event_id": ev["id"]})
    return core.public_results(core.compute_results(ev["id"]))     # no worker PIDs / threads: see get_worker_stats


@rpc_method("get_worker_stats", role="admin")
def get_worker_stats(event_id=None):
    """Admin only: ballots of one event per worker process (PID) and the number of threads that recorded them."""
    ev = _event(event_id)
    return core.worker_stats(ev["id"])


@rpc_method("verify_receipt")
def verify_receipt(verification_code):
    """Was a ballot with this code recorded, in which event, and when? Never returns the votes or the voter."""
    code = _code(verification_code, "verification_code")
    row = core.db.query_one(
        """SELECT to_char(b.cast_at AT TIME ZONE 'Asia/Manila', 'Mon DD, YYYY  HH12:MI:SS AM'), e.name
             FROM ballots b JOIN events e ON e.id = b.event_id WHERE b.receipt_code = %s""", (code,))
    return {"verification_code": code, "recorded": row is not None,
            "cast_at": row[0] if row else None, "event": row[1] if row else None}


# ---------------------------------------------------------------- remote procedures: logged-in student

def _password_change_check(ev, student, has_voted):
    if ev["require_password_change"] and student["password_generated"] and not has_voted:
        raise RpcError(PASSWORD_CHANGE_REQUIRED, "Before you vote, please choose your own password.",
                       {"url": url_for("account_password")})


def _registered(ev, student):
    """The student's registration in this event (checked on the server every call)."""
    reg = core.registration(ev["id"], student["id"])
    if reg is None:
        notice = core.registration_notice(ev["id"], student_id=student["id"])     # pending / rejected?
        raise RpcError(NOT_REGISTERED, notice["text"] if notice else core.NOT_REGISTERED,
                       {"event_id": ev["id"], "registration": notice["kind"] if notice else None})
    return reg


@rpc_method("get_my_events", role="student")
def get_my_events():
    """The events the logged-in student is registered in, with voted / not voted per event."""
    return [dict(_event_public(ev), has_voted=bool(ev["has_voted"]), voted_at=ev["voted_at"])
            for ev in core.student_events(g.student["id"])]


def _verified(student):
    if student["account_status"] != "verified":
        msg = (core.ACCOUNT_PENDING_MSG if student["account_status"] == "pending"
               else f"Your account was rejected: {student['account_reason'] or 'no reason given'}")
        raise RpcError(ACCOUNT_NOT_VERIFIED, msg, {"account_status": student["account_status"]})


@rpc_method("get_ballot", role="student")
def get_ballot(event_id=None):
    """Positions (with seats = how many to choose) and candidates of one event, for a registered student."""
    ev = _event(event_id)
    student = g.student
    _verified(student)
    has_voted = _registered(ev, student)[0]
    _password_change_check(ev, student, has_voted)
    return {
        "election": {"id": ev["id"], "title": ev["name"], "status": ev["status"], "is_open": ev["is_open"],
                     "school_name": ev["organization"], "school_year": ev["school_year"]},
        "voter": {"voter_id": student["student_no"], "name": student["full_name"]},
        "has_voted": bool(has_voted),
        "positions": core.load_ballot(ev["id"]) if ev["status"] == "current" else [],
    }


@rpc_method("cast_vote", role="student")
def cast_vote_rpc(choices, event_id=None):
    """Cast the student's ballot in one event. choices = {"<position_id>": <candidate_id>, [ids...] or null}."""
    ev = _event(event_id)
    student = g.student
    _verified(student)
    has_voted = _registered(ev, student)[0]
    _password_change_check(ev, student, has_voted)
    if not isinstance(choices, dict):
        raise RpcError(INVALID_PARAMS, 'choices must be an object, e.g. {"1": 3, "2": [7, 8], "3": null}.')
    if len(choices) > 100:
        raise RpcError(INVALID_PARAMS, "Too many positions in choices.")
    picked = {}
    for pos, cand in choices.items():
        pid = _int_id(pos, "position id")
        if cand is None or cand == "" or cand == []:
            picked[pid] = []                           # skipped position ("No vote")
            continue
        items = cand if isinstance(cand, list) else [cand]
        if len(items) > 20:
            raise RpcError(INVALID_PARAMS, f"Too many candidates for position {pid}.")
        picked[pid] = [_int_id(c, f"candidate id for position {pid}") for c in items]
    receipt_url = url_for("event_receipt", event_id=ev["id"])
    try:
        # The SAME function as the web form: one transaction, SELECT ... FOR UPDATE on the
        # (event, student) registration row, then all checks (seats, duplicates, event) and inserts.
        receipt, pid, thread = core.cast_vote(ev["id"], student["id"], picked)
    except core.VoteError as e:
        msg = str(e)
        if "already voted" in msg:
            raise RpcError(ALREADY_VOTED, msg, {"receipt_url": receipt_url})
        if "closed" in msg:
            raise RpcError(VOTING_CLOSED, msg)
        if "not started" in msg:
            raise RpcError(NOT_STARTED, msg)
        if "not registered" in msg:
            raise RpcError(NOT_REGISTERED, core.NOT_REGISTERED, {"event_id": ev["id"]})
        raise RpcError(INVALID_VOTE, msg)
    except psycopg.errors.UniqueViolation:            # safety net: UNIQUE(event_id, student_id)
        raise RpcError(ALREADY_VOTED, "You have already voted.", {"receipt_url": receipt_url})
    except psycopg.errors.CheckViolation:             # safety net: the votes_guard trigger
        raise RpcError(INVALID_VOTE, "Too many candidates for a position.")
    log.info("ballot %s recorded for student %s in event %s", receipt, student["student_no"], ev["id"])
    session["last_vote"] = {"pid": pid, "thread": thread}
    session["event_id"] = ev["id"]
    return {"event_id": ev["id"], "receipt_code": receipt,
            "positions_voted": sum(1 for v in picked.values() if v),
            "candidates_chosen": sum(len(v) for v in picked.values()),
            "receipt_url": receipt_url, "verify_url": url_for("verify", code=receipt, _external=True),
            "recorded_by": {"worker_pid": pid, "thread_name": thread}}


@rpc_method("get_receipt", role="student")
def get_receipt(receipt_code):
    """The logged-in student's own receipt (only their own: another code returns "not found")."""
    code = _code(receipt_code, "receipt_code")
    row = core.db.query_one(
        """SELECT id, event_id, receipt_code, to_char(cast_at AT TIME ZONE 'Asia/Manila', 'Mon DD, YYYY  HH12:MI:SS AM'),
                  worker_pid, thread_name
             FROM ballots WHERE student_id = %s AND receipt_code = %s""", (g.student["id"], code))
    if row is None:
        raise RpcError(NOT_FOUND, "No receipt with that code for your account.")
    ballot_id, event_id, code, cast_at, worker_pid, thread_name = row
    picks = core.own_picks(event_id, g.student["id"])
    ev = core.event_state(event_id)
    return {
        "event": {"id": ev["id"], "name": ev["name"]},
        "receipt_code": code, "ballot_no": f"{ballot_id:06d}", "cast_at": cast_at,
        "voter_id_masked": core.mask_voter_id(g.student["student_no"]),
        "school_name": ev["organization"], "school_year": ev["school_year"],
        "votes": [{"position": p["position"], "seats": p["seats"],
                   "candidates": [{"name": c["name"], "party": c["party"]} for c in p["chosen"]],
                   "no_vote": not p["chosen"]} for p in picks],
        "positions_voted": sum(1 for p in picks if p["chosen"]),
        "positions_skipped": sum(1 for p in picks if not p["chosen"]),
        "recorded_by": {"worker_pid": worker_pid, "thread_name": thread_name},
        "verify_url": url_for("verify", code=code, _external=True),
    }


# ---------------------------------------------------------------- remote procedures: admin

def _set_mode(event_id, mode):
    ev = _event(event_id)
    before = ev["is_open"]
    after = core.set_event_mode(ev["id"], mode)
    return {"event_id": ev["id"], "is_open": after["is_open"], "status": after["status"],
            "changed": before != after["is_open"]}


@rpc_method("open_voting", role="admin")
def open_voting(event_id=None):
    """Open one event now: its registered students can log in and submit ballots."""
    return _set_mode(event_id, "open")


@rpc_method("close_voting", role="admin")
def close_voting(event_id=None):
    """Close one event now: no more ballots are accepted (it becomes a PREVIOUS event)."""
    return _set_mode(event_id, "closed")


@rpc_method("get_turnout", role="admin")
def get_turnout(event_id=None):
    """One event: registered / voted / not yet voted, turnout %, and per course, department and class."""
    ev = _event(event_id)
    registered, voted = core.db.query_one(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE has_voted) FROM event_voters WHERE event_id = %s", (ev["id"],))
    return {"event_id": ev["id"], "registered": registered, "voted": voted, "not_yet_voted": registered - voted,
            "turnout_percent": round(100 * voted / registered, 1) if registered else 0.0,
            "by_group": core.turnout_by_group(ev["id"])}


# ---------------------------------------------------------------- accounts + one-click event registration

def _reg_error(e):
    return RpcError(REGISTRATION_REFUSED, str(e), {"field": e.field, "reason": e.code})


@rpc_method("create_account")
def create_account(school_student_no, full_name, course, department, year, section, password,
                   confirm_password=None, email=None):
    """Create a student account: the Voter ID is generated at once. Status: pending until COMELEC verifies it."""
    if core.over_limit("register", core.REGISTER_LIMIT):
        raise RpcError(TOO_MANY_ATTEMPTS, core.limit_message())
    form = {"school_student_no": school_student_no, "full_name": full_name, "course": course,
            "department": department, "year": "" if year is None else str(year), "section": section,
            "email": email or "", "password": password,
            "confirm_password": password if confirm_password is None else confirm_password}
    try:
        result = core.create_account({k: ("" if v is None else v) for k, v in form.items()})
    except core.RegError as e:
        raise _reg_error(e)
    core.count_attempt("register")
    return dict(result, message="Account created. Save your Voter ID; you'll use it to log in. "
                                "Status: PENDING VERIFICATION by the election committee.")


@rpc_method("get_account_status")
def get_account_status(school_student_no, password):
    """Forgot your Voter ID: the Voter ID, the account status (pending / verified / rejected + reason) and registrations."""
    try:
        school_no = core.clean_school_no(school_student_no)
    except core.RegError as e:
        raise _reg_error(e)
    if core.over_limit("status", core.STATUS_LIMIT):
        raise RpcError(TOO_MANY_ATTEMPTS, core.limit_message())
    result = core.account_status_info(school_no, str(password or ""))
    if result is None:
        core.count_attempt("status")
        raise RpcError(NOT_FOUND, "Wrong school student number or password.")
    return result


@rpc_method("register_for_event", role="student")
def register_for_event(event_id):
    """One-click registration of the logged-in (verified) student for a current/upcoming event.
    Auto-approved when the event allows it, otherwise pending until COMELEC approves."""
    ev = _event(event_id)
    _verified(g.student)
    try:
        r = core.register_for_event(ev["id"], g.student)
    except core.RegError as e:
        raise _reg_error(e)
    return dict(r, event_id=ev["id"])


@rpc_method("get_accounts", role="admin")
def get_accounts(status=None):
    """Student accounts (optionally only pending / verified / rejected), with counts."""
    if status not in (None, "pending", "verified", "rejected"):
        raise RpcError(INVALID_PARAMS, "status must be pending, verified, rejected or left out.")
    rows = [a for a in core.load_accounts() if status is None or a["status"] == status]
    return {"counts": core.account_counts(),
            "accounts": [{"student_id": a["id"], "voter_id": a["voter_id"], "school_student_no": a["school_student_no"],
                          "full_name": a["full_name"], "course": a["course"], "department": a["department"],
                          "year": a["year_level"], "section": a["section"], "status": a["status"],
                          "reason": a["reason"], "created_at": a["created"]} for a in rows]}


@rpc_method("verify_account", role="admin")
def verify_account(student_id):
    """Verify one student account (once per student): they can then register for events and vote."""
    sid = _int_id(student_id, "student_id")
    if core.db.query_one("SELECT 1 FROM students WHERE id = %s", (sid,)) is None:
        raise RpcError(NOT_FOUND, f"No account {sid}.")
    return {"student_id": sid, "verified": core.verify_accounts([sid]) == 1}


@rpc_method("reject_account", role="admin")
def reject_account(student_id, reason):
    """Reject one PENDING account with a reason (the student sees it after logging in)."""
    sid = _int_id(student_id, "student_id")
    try:
        n = core.reject_accounts([sid], reason if isinstance(reason, str) else "")
    except core.RegError as e:
        raise RpcError(INVALID_PARAMS, str(e))
    return {"student_id": sid, "rejected": n == 1}


@rpc_method("verify_all_pending_accounts", role="admin")
def verify_all_pending_accounts():
    """Verify every pending student account at once."""
    n = core.verify_accounts()
    return {"verified": n, "counts": core.account_counts()}


@rpc_method("get_registrations", role="admin")
def get_registrations(event_id=None, status=None):
    """Registration requests of one event (optionally only pending / approved / rejected), with counts."""
    ev = _event(event_id)
    if status not in (None, "pending", "approved", "rejected"):
        raise RpcError(INVALID_PARAMS, "status must be pending, approved, rejected or left out.")
    rows = [r for r in core.load_registrations(ev["id"]) if status is None or r["status"] == status]
    return {"event_id": ev["id"], "counts": core.registration_counts(ev["id"]),
            "registrations": [{"id": r["id"], "school_student_no": r["school_student_no"], "full_name": r["full_name"],
                               "course": r["course"], "department": r["department"], "year": r["year_level"],
                               "section": r["section"], "status": r["status"], "reason": r["reason"],
                               "submitted_at": r["submitted"], "has_account": r["existing"], "voter_id": r["voter_id"]}
                              for r in rows]}


@rpc_method("approve_registration", role="admin")
def approve_registration(registration_id, event_id=None):
    """Approve one registration: a new student gets a Voter ID, and is added to the event's voters."""
    ev = _event(event_id)
    rid = _int_id(registration_id, "registration_id")
    row = core.db.query_one("SELECT status FROM registrations WHERE id = %s AND event_id = %s", (rid, ev["id"]))
    if row is None:
        raise RpcError(NOT_FOUND, f"No registration {rid} in this event.")
    approved = core.approve_registrations(ev["id"], [rid]) == 1
    voter_id = core.db.query_one("""SELECT s.student_no FROM registrations r JOIN students s ON s.id = r.student_id
                                     WHERE r.id = %s""", (rid,))
    return {"registration_id": rid, "event_id": ev["id"], "approved": approved, "was": row[0],
            "voter_id": voter_id[0] if voter_id else None}


@rpc_method("reject_registration", role="admin")
def reject_registration(registration_id, reason, event_id=None):
    """Reject one PENDING registration with a reason (the student sees it)."""
    ev = _event(event_id)
    rid = _int_id(registration_id, "registration_id")
    try:
        n = core.reject_registrations(ev["id"], [rid], reason if isinstance(reason, str) else "")
    except core.RegError as e:
        raise RpcError(INVALID_PARAMS, str(e))
    return {"registration_id": rid, "event_id": ev["id"], "rejected": n == 1}


@rpc_method("approve_all_pending", role="admin")
def approve_all_pending(event_id=None):
    """Approve every pending registration of one event at once."""
    ev = _event(event_id)
    n = core.approve_registrations(ev["id"])
    return {"event_id": ev["id"], "approved": n, "counts": core.registration_counts(ev["id"])}
