"""Shared helpers for BotoKo's evidence tests (run_test_cases.py, perf_compare.py, take_screenshots.py).

SAFETY
  - Only ever touches the database TEST_DB = "botoko_test" (on the local cluster, port 5433). Every destructive
    step checks the name first. botoko_live is never opened.
  - The app under test is started by these scripts with environment variables only (DATABASE_URL, PORT,
    SECRET_KEY, ADMIN_PASSWORD, WEB_CONCURRENCY, THREADS). run-local.ps1 and .secret_key are not used or changed:
    the test server gets its own random SECRET_KEY in memory.
  - Accounts are synthetic: Voter IDs TEST-0001, TEST-0002, ... with ONE random password per run that only
    lives in memory (never printed, never written to a file). The test admin password is random too.
"""
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import psycopg
import requests
from werkzeug.security import generate_password_hash

PROJECT = Path(__file__).resolve().parents[1]
EVIDENCE = PROJECT / "test_evidence"
RAW = EVIDENCE / "raw"
PG_BIN = Path(os.environ.get("PG_BIN", r"C:\Program Files\PostgreSQL\18\bin"))
PG_PORT = int(os.environ.get("TEST_PG_PORT", "5433"))
TEST_DB = "botoko_test"
DSN = f"postgresql://botoko@localhost:{PG_PORT}/{TEST_DB}"
APP_PORT = int(os.environ.get("TEST_APP_PORT", "5071"))   # (5060/5061 are on the browsers' blocked-port list)
BASE = f"http://127.0.0.1:{APP_PORT}"
CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')

# one run = one random student password + one random admin password (memory only)
STUDENT_PASSWORD = "Tst-" + secrets.token_urlsafe(12)
ADMIN_PASSWORD = "Adm-" + secrets.token_urlsafe(16)


def _guard():
    assert TEST_DB == "botoko_test" and "botoko_live" not in DSN, "refusing: not the test database"


def voter_id(i):
    return f"TEST-{i:04d}"


# ---------------------------------------------------------------- database
def pg(tool, *args, check=True):
    _guard()
    cmd = [str(PG_BIN / tool), "-h", "localhost", "-p", str(PG_PORT), "-U", "botoko", *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{tool} failed: {r.stderr.strip()}")
    return r


def reset_db():
    """Drop and re-create botoko_test from schema.sql (the app adds its later migrations when it starts)."""
    _guard()
    pg("dropdb.exe", "--if-exists", "--force", TEST_DB)
    pg("createdb.exe", TEST_DB)
    pg("psql.exe", "-d", TEST_DB, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(PROJECT / "schema.sql"))


def db():
    _guard()
    return psycopg.connect(DSN)


def q(sql, params=None):
    with db() as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else cur.rowcount


# ---------------------------------------------------------------- the app under test
class Server:
    """serve.py (the same launcher run-local.ps1 uses) against botoko_test.
    workers/threads None = the normal auto-detected setup (1 worker per physical core x 8 threads)."""

    def __init__(self, workers=None, threads=None, log_name="server.log"):
        self.workers, self.threads = workers, threads
        self.log_path = RAW / log_name
        self.proc = None

    def start(self, timeout=120):
        _guard()
        RAW.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items()
               if k not in ("WEB_CONCURRENCY", "THREADS", "DB_POOL_SIZE", "MAX_WORKERS", "SECRET_KEY", "DATABASE_URL")}
        env.update(DATABASE_URL=DSN, PORT=str(APP_PORT), SECRET_KEY=secrets.token_hex(32), ADMIN_PASSWORD=ADMIN_PASSWORD,
                   COOKIE_SECURE="0", PYTHONUNBUFFERED="1", REGISTER_LIMIT="100000")
        if self.workers:
            env["WEB_CONCURRENCY"] = str(self.workers)
        if self.threads:
            env["THREADS"] = str(self.threads)
        self.log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "serve.py"], cwd=PROJECT, env=env, stdout=self.log,
                                     stderr=subprocess.STDOUT)
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early, see {self.log_path}")
            try:
                if requests.get(BASE + "/healthz", timeout=2).status_code == 200:
                    time.sleep(1.0)                 # let every worker finish starting
                    return self
            except requests.RequestException:
                pass
            time.sleep(0.5)
        raise RuntimeError(f"server did not start in {timeout}s, see {self.log_path}")

    def banner(self):
        self.log.flush()
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"BotoKo: .*?requests at the same time.*", text)
        return m.group(0).strip() if m else ""

    def stop(self):
        if not self.proc:
            return
        try:
            parent = psutil.Process(self.proc.pid)
            for ch in parent.children(recursive=True):
                ch.kill()
            parent.kill()
        except psutil.NoSuchProcess:
            pass
        self.proc.wait(timeout=30)
        self.proc = None
        self.log.close()
        end = time.time() + 20                        # wait until the port is really free
        while time.time() < end:
            try:
                requests.get(BASE + "/healthz", timeout=1)
                time.sleep(0.5)
            except requests.RequestException:
                return


# ---------------------------------------------------------------- synthetic data
BALLOT = [("President", 1, ["TEST Candidate P1", "TEST Candidate P2"]),
          ("Vice President", 1, ["TEST Candidate V1", "TEST Candidate V2", "TEST Candidate V3"]),
          ("Councilor", 3, ["TEST Candidate C1", "TEST Candidate C2", "TEST Candidate C3", "TEST Candidate C4",
                            "TEST Candidate C5"])]


def _event(c, name, starts, ends, auto_approve=True):
    return c.execute(f"""INSERT INTO events (name, description, organization, school_year, starts_at, ends_at, auto_approve)
                         VALUES (%s, 'Synthetic test event', 'TEST School', '2026-2027', {starts}, {ends}, %s)
                         RETURNING id""", (name, auto_approve)).fetchone()[0]


def _ballot(c, eid, ballot):
    out = []
    for order, (pos, seats, names) in enumerate(ballot, 1):
        pid = c.execute("""INSERT INTO positions (event_id, name, seats, sort_order) VALUES (%s, %s, %s, %s) RETURNING id""",
                        (eid, pos, seats, order)).fetchone()[0]
        cids = [c.execute("""INSERT INTO candidates (position_id, full_name, party) VALUES (%s, %s, 'TEST Party')
                             RETURNING id""", (pid, n)).fetchone()[0] for n in names]
        out.append({"id": pid, "name": pos, "seats": seats, "candidates": cids})
    return out


def seed(n_students, extra_events=False):
    """TEST-0001..TEST-n (verified accounts, all with STUDENT_PASSWORD) registered in the CURRENT main event.
    extra_events: also an approval-required event, an UPCOMING one and a CLOSED one (first 20 students
    registered in the upcoming and closed ones)."""
    _guard()
    pw_hash = generate_password_hash(STUDENT_PASSWORD)
    with db() as c:
        main = _event(c, "TEST Main Election", "now() - interval '1 day'", "now() + interval '3 days'")
        info = {"main": main, "main_ballot": _ballot(c, main, BALLOT)}
        if extra_events:
            info["pending"] = _event(c, "TEST Approval Election", "now() - interval '1 day'", "now() + interval '3 days'",
                                     auto_approve=False)
            info["pending_ballot"] = _ballot(c, info["pending"], BALLOT[:1])
            info["upcoming"] = _event(c, "TEST Upcoming Election", "now() + interval '2 days'", "now() + interval '5 days'")
            info["upcoming_ballot"] = _ballot(c, info["upcoming"], BALLOT[:1])
            info["closed"] = _event(c, "TEST Closed Election", "now() - interval '5 days'", "now() - interval '1 day'")
            info["closed_ballot"] = _ballot(c, info["closed"], BALLOT[:1])
        with c.cursor() as cur:
            cur.executemany(
                """INSERT INTO students (student_no, full_name, password_hash, password_generated, school_student_no,
                                         course, department, year_level, section, account_status)
                   VALUES (%s, %s, %s, FALSE, %s, 'BSIT', 'TEST Department', 1, 'A', 'verified')""",
                [(voter_id(i), f"TEST Student {i:04d}", pw_hash, f"99-9-{i:05d}") for i in range(1, n_students + 1)])
        ids = dict(c.execute("SELECT student_no, id FROM students WHERE student_no LIKE 'TEST-%%'").fetchall())
        with c.cursor() as cur:
            cur.executemany("INSERT INTO event_voters (event_id, student_id) VALUES (%s, %s)",
                            [(main, sid) for sid in ids.values()])
            if extra_events:
                first20 = [ids[voter_id(i)] for i in range(1, min(20, n_students) + 1)]
                cur.executemany("INSERT INTO event_voters (event_id, student_id) VALUES (%s, %s)",
                                [(e, sid) for e in (info["upcoming"], info["closed"]) for sid in first20])
    info["ids"] = ids
    return info


def choices_for(i, ballot):
    """Deterministic ballot of student i: President rotates, Vice President skipped by every 7th student,
    Councilor 1-3 picks. Returns the RPC 'choices' object."""
    pres, vp, coun = ballot
    out = {str(pres["id"]): pres["candidates"][i % 2]}
    out[str(vp["id"])] = None if i % 7 == 0 else vp["candidates"][i % 3]
    k = i % 3 + 1
    out[str(coun["id"])] = [coun["candidates"][(i + j) % 5] for j in range(k)]
    return out


def expected_tally(indices, ballot):
    tally, rows = {}, 0
    for i in indices:
        for v in choices_for(i, ballot).values():
            for cid in ([] if v is None else v if isinstance(v, list) else [v]):
                tally[cid] = tally.get(cid, 0) + 1
                rows += 1
    return tally, rows


# ---------------------------------------------------------------- a browser-like client
class Client:
    """One student's browser: its own cookies (session), the page's CSRF token, real HTTP requests.
    'Connection: close' = every request is a new connection, like many different phones."""

    def __init__(self, timeout=180):
        self.s = requests.Session()
        self.s.headers["Connection"] = "close"
        self.timeout = timeout
        self.rpc_token = None

    def get(self, path, **kw):
        return self.s.get(BASE + path, timeout=self.timeout, **kw)

    def post(self, path, data, **kw):
        return self.s.post(BASE + path, data=data, timeout=self.timeout, **kw)

    @staticmethod
    def csrf_in(html):
        m = CSRF_RE.search(html)
        return m.group(1) if m else ""

    def login(self, user, password, follow=True):
        page = self.get("/")
        return self.post("/", {"csrf": self.csrf_in(page.text), "student_no": user, "password": password},
                         allow_redirects=follow)

    def fetch_rpc_token(self):
        self.rpc_token = self.get("/session/refresh").json()["csrf"]
        return self.rpc_token

    def rpc(self, method, params=None):
        r = self.s.post(BASE + "/rpc", json={"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1},
                        headers={"X-CSRF-Token": self.rpc_token or ""}, timeout=self.timeout)
        return r.json()


def login_many(indices, threads=32):
    """Log students in BEFORE a timed run (logins are not part of the measured workload)."""
    clients, errors = {}, []
    lock = threading.Lock()
    todo = list(indices)

    def work():
        while True:
            with lock:
                if not todo:
                    return
                i = todo.pop()
            c = Client()
            try:
                r = c.login(voter_id(i), STUDENT_PASSWORD, follow=False)
                if r.status_code not in (302, 303) or "/events" not in r.headers.get("Location", ""):
                    raise RuntimeError(f"login of {voter_id(i)} failed ({r.status_code})")
                c.fetch_rpc_token()
                with lock:
                    clients[i] = c
            except Exception as e:                  # noqa: BLE001 - recorded, reported
                with lock:
                    errors.append(f"{voter_id(i)}: {e}")
    ts = [threading.Thread(target=work) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return clients, errors


def vote_burst(clients, event_id, ballot, concurrency, progress=None):
    """Every client casts its vote with RPC cast_vote (what the ballot page does). `concurrency` client threads
    start at the same instant (barrier) and keep sending until all votes are sent.
    Returns per-vote results [(index, ok, latency_s, error)] and the total wall-clock time."""
    items = sorted(clients)
    lock = threading.Lock()
    queue = list(items)
    results = []
    n_threads = min(concurrency, len(items))
    barrier = threading.Barrier(n_threads + 1)

    def work():
        barrier.wait()
        while True:
            with lock:
                if not queue:
                    return
                i = queue.pop(0)
            c = clients[i]
            t0 = time.perf_counter()
            try:
                reply = c.rpc("cast_vote", {"event_id": event_id, "choices": choices_for(i, ballot)})
                ok = "result" in reply
                err = None if ok else f"{reply.get('error', {}).get('code')} {reply.get('error', {}).get('message')}"
            except Exception as e:                  # noqa: BLE001 - counted as an error
                ok, err = False, type(e).__name__ + ": " + str(e)[:120]
            dt = time.perf_counter() - t0
            with lock:
                results.append((i, ok, dt, err))
                if progress is not None:
                    progress[0] += 1
    ts = [threading.Thread(target=work) for _ in range(n_threads)]
    for t in ts:
        t.start()
    barrier.wait()
    start = time.perf_counter()
    for t in ts:
        t.join()
    return results, time.perf_counter() - start


def db_check(event_id, indices, ballot):
    """Votes stored = votes sent, zero duplicates, every candidate's count = what was sent."""
    n_ballots, n_distinct = q("SELECT COUNT(*), COUNT(DISTINCT student_id) FROM ballots WHERE event_id = %s", (event_id,))[0]
    voted_flags = q("SELECT COUNT(*) FROM event_voters WHERE event_id = %s AND has_voted", (event_id,))[0][0]
    vote_rows = q("SELECT COUNT(*) FROM votes v JOIN ballots b ON b.id = v.ballot_id WHERE b.event_id = %s", (event_id,))[0][0]
    per_cand = dict(q("""SELECT v.candidate_id, COUNT(*) FROM votes v JOIN ballots b ON b.id = v.ballot_id
                         WHERE b.event_id = %s GROUP BY v.candidate_id""", (event_id,)))
    exp_tally, exp_rows = expected_tally(indices, ballot)
    pids = q("SELECT COUNT(DISTINCT worker_pid) FROM ballots WHERE event_id = %s", (event_id,))[0][0]
    return {"ballots": n_ballots, "duplicates": n_ballots - n_distinct, "has_voted_flags": voted_flags,
            "vote_rows": vote_rows, "expected_vote_rows": exp_rows, "tally_matches": per_cand == exp_tally,
            "worker_pids": pids,
            "ok": n_ballots == len(indices) and n_ballots == n_distinct and voted_flags == len(indices)
            and vote_rows == exp_rows and per_cand == exp_tally}


def percentile(values, p):
    if not values:
        return 0.0
    v = sorted(values)
    k = max(0, min(len(v) - 1, int(round(p / 100 * len(v) + 0.5)) - 1))
    return v[k]
