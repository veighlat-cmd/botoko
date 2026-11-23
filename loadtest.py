"""BotoKo load test: N students log in and vote at the SAME instant, across one or more voting events.
Uses only the Python standard library, so it runs on any laptop:
    python loadtest.py https://your-app.onrender.com accounts.csv
    python loadtest.py http://localhost:5000 accounts-demo.csv --users 50 --double 5 --expect-workers 4

Which event each student votes in:
  - the accounts CSV has an "events" column (seed.py --demo-events writes it): the CURRENT events
    each student is registered in, e.g. "2;3". Students are spread over their events (round robin).
  - or --event 2: everybody votes in event 2 (they must be registered there).

How students vote (default --mode rpc): log in on the normal login page (the first page), then call the
remote procedures get_ballot and cast_vote through BotoKo's JSON-RPC 2.0 endpoint (POST /rpc),
exactly like the ballot page does. --mode form posts the HTML form to /e/<event>/vote instead.

What it checks
  1. N students log in at the same moment, then all press "Submit" at the same moment.
     Every request must succeed (no errors, no crashes).
  2. Double-vote attack: --double students each open TWO sessions in the SAME event and submit both
     at once. Exactly one vote per student per event must be accepted.
  3. Per event, the live tally must match exactly what the test sent (no lost or extra votes).
  4. Voting is optional and some positions elect several people ("Choose up to N"): voters pick a
     random 0..N candidates (--skip-rate = chance of choosing nobody), and each position's
     "no vote" count must match.
  5. Parallel processing: which worker processes (PIDs) recorded the ballots.
     --expect-workers 4 also checks that the new ballots were spread over 4 different PIDs.
Before running: the events must be CURRENT (open), and the accounts used must not have voted in them
yet (use Admin > Settings > Reset votes afterwards to clear the test votes).
"""
import argparse
import os
import csv
import http.cookiejar
import json
import random
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')
CHOICE_RE = re.compile(r'type="(?:radio|checkbox)" name="pos_(\d+)" value="(\d+)"')
SEATS_RE = re.compile(r'id="pos-(\d+)"[^>]*data-seats="(\d+)"')


class RpcCallError(Exception):
    def __init__(self, error):
        super().__init__(f"RPC error {error.get('code')}: {error.get('message')}")
        self.code = error.get("code")


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = None
        self.ids = 0
        self.last_meta = None

    def get(self, path):
        with self.opener.open(self.base + path, timeout=90) as r:
            return r.geturl(), r.read().decode()

    def post(self, path, data):
        body = urllib.parse.urlencode(data, doseq=True).encode()
        with self.opener.open(self.base + path, data=body, timeout=90) as r:
            return r.geturl(), r.read().decode()

    def rpc(self, method, params=None):
        """JSON-RPC 2.0 call to POST /rpc with this client's session cookie + CSRF token."""
        if self.csrf is None:
            self.csrf = json.loads(self.get("/rpc")[1])["csrf_token"]
        self.ids += 1
        req = urllib.request.Request(
            self.base + "/rpc", method="POST",
            data=json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}, "id": self.ids}).encode(),
            headers={"Content-Type": "application/json", "X-CSRF-Token": self.csrf})
        with self.opener.open(req, timeout=90) as r:
            reply = json.loads(r.read().decode())
        self.last_meta = reply.get("meta")
        if "error" in reply:
            raise RpcCallError(reply["error"])
        return reply["result"]


def pct(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def fetch_results(base, event_id):
    return Client(base).rpc("get_results", {"event_id": event_id})


def fetch_workers(base, event_id, password):
    """Ballots per worker process (PID): ADMIN-ONLY (RPC get_worker_stats), so log in as the admin first.
    None if the admin login fails (then only the cast_vote "meta" PIDs are used)."""
    c = Client(base)
    _, page = c.get("/")
    m = CSRF_RE.search(page)
    final_url, _ = c.post("/", {"csrf": m.group(1) if m else "", "student_no": "admin", "password": password})
    if "/admin" not in final_url:
        return None
    return c.rpc("get_worker_stats", {"event_id": event_id})["processed_by"]


def tally(results):
    return {c["id"]: c["votes"] for p in results["positions"] for c in p["candidates"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("accounts", help="CSV with voter_id (or student_no), password and (optional) events columns")
    ap.add_argument("--users", type=int, default=50, help="students voting at the same time")
    ap.add_argument("--double", type=int, default=5, help="students who try to vote twice at once (same event)")
    ap.add_argument("--event", type=int, help="vote in this event only (default: the CSV's events column)")
    ap.add_argument("--skip-rate", type=float, default=0.3,
                    help="chance that a voter chooses nobody for a position (voting is optional), default 0.3")
    ap.add_argument("--mode", choices=["rpc", "form"], default="rpc",
                    help="rpc (default): vote with the remote procedure cast_vote; form: post the HTML form")
    ap.add_argument("--admin-password", default=os.environ.get("ADMIN_PASSWORD", "admin123"),
                    help="admin password, to read the per-worker ballot counts (admin-only); default: $ADMIN_PASSWORD")
    ap.add_argument("--expect-workers", type=int, default=0,
                    help="also check that the new ballots were recorded by this many different worker PIDs")
    args = ap.parse_args()

    # New files (Admin download / seed.py) have a voter_id column; older files have student_no.
    with open(args.accounts, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    accounts = []
    for i, r in enumerate(rows):
        if args.event:
            events = [args.event]
        else:
            events = [int(x) for x in (r.get("events") or "").replace(",", ";").split(";") if x.strip()]
        if events:
            accounts.append((r.get("voter_id") or r["student_no"], r["password"], events[i % len(events)]))
    if not args.event and not accounts:
        sys.exit("The CSV has no 'events' column: add --event <id> (see GET /rpc -> get_events).")
    need = args.users + args.double
    if len(accounts) < need:
        sys.exit(f"Need {need} accounts with an event in {args.accounts}, found {len(accounts)}.")

    print(f"Target: {args.url}")
    print("Waking up the server...", end=" ", flush=True)
    deadline = time.time() + 150
    while True:
        try:
            Client(args.url).get("/healthz")
            break
        except Exception:
            if time.time() > deadline:
                sys.exit("server did not respond")
            time.sleep(3)
    print("ok")

    single = accounts[:args.users]
    double = accounts[args.users:need]
    event_ids = sorted({e for _, _, e in single + double})
    before = {e: fetch_results(args.url, e) for e in event_ids}
    before_w = {e: fetch_workers(args.url, e, args.admin_password) for e in event_ids}
    for e in event_ids:
        if not before[e]["is_open"]:
            sys.exit(f"Event {e} ({before[e]['title']}) is not CURRENT. Open it in the Admin page first.")
    print("Events: " + ", ".join(f"{e} = {before[e]['title']} ({sum(1 for a in single + double if a[2] == e)} students)"
                                 for e in event_ids))

    # one session per single voter, two sessions per double voter
    sessions = [(sno, pw, ev, "single") for sno, pw, ev in single]
    sessions += [(sno, pw, ev, "double") for sno, pw, ev in double for _ in range(2)]
    n = len(sessions)
    lat = {"login": [], "ballot": [], "vote": []}
    errors = []
    outcomes = []          # (event, student_no, kind, accepted, picks {pos: [cands]}, positions)
    rpc_pids = set()       # worker PIDs that ran a cast_vote procedure (from the RPC "meta" field)
    lock = threading.Lock()
    login_gate = threading.Barrier(n, timeout=300)
    vote_gate = threading.Barrier(n, timeout=300)

    def timed(kind, fn):
        t = time.perf_counter()
        out = fn()
        with lock:
            lat[kind].append(time.perf_counter() - t)
        return out

    def choose(options):
        """options = {pos: (seats, [cands])} -> {pos: [picked]}: 0..seats candidates per position."""
        picks = {}
        for pos, (seats, cands) in options.items():
            if random.random() < args.skip_rate:
                picks[pos] = []                                   # skipped ("No vote")
            else:
                picks[pos] = random.sample(cands, random.randint(1, min(seats, len(cands))))
        return picks

    def worker(sno, pw, ev, kind):
        c = Client(args.url)
        picks, csrf, passed_gate, options = None, None, False, {}
        try:
            _, page = c.get("/")                               # the login page is the first page
            csrf = CSRF_RE.search(page).group(1)
            passed_gate = True
            login_gate.wait()                                   # everyone logs in together
            url, html = timed("login", lambda: c.post("/", {"csrf": csrf, "student_no": sno, "password": pw,
                                                            "next": f"/e/{ev}/ballot"}))
            if not url.endswith(f"/e/{ev}/ballot") or "Choose one candidate" not in html:
                raise RuntimeError("login failed or ballot not shown"
                                   + (" (not registered for this event)" if "not registered" in html else ""))
            if args.mode == "rpc":
                ballot = timed("ballot", lambda: c.rpc("get_ballot", {"event_id": ev}))
                options = {str(p["id"]): (p["seats"], [str(x["id"]) for x in p["candidates"]]) for p in ballot["positions"]}
            else:
                _, html = timed("ballot", lambda: c.get(f"/e/{ev}/ballot"))
                csrf = CSRF_RE.search(html).group(1)
                seats = dict(SEATS_RE.findall(html))
                for pos, cand in CHOICE_RE.findall(html):
                    options.setdefault(pos, (int(seats.get(pos, 1)), []))[1].append(cand)
            picks = choose(options)
        except Exception as e:
            with lock:
                errors.append(f"{sno} before voting: {e!r}")
            if not passed_gate:                  # keep the others from waiting forever
                try:
                    login_gate.wait()
                except threading.BrokenBarrierError:
                    pass
            picks = None
        try:
            vote_gate.wait()                                    # everyone presses Submit together
        except threading.BrokenBarrierError:
            pass
        if picks is None:
            return
        try:
            if args.mode == "rpc":
                # {"<position id>": [candidate ids] (several for "Choose up to N"), or None = skipped}
                params = {"event_id": ev, "choices": {p: ([int(x) for x in v] or None) for p, v in picks.items()}}
                try:
                    timed("vote", lambda: c.rpc("cast_vote", params))
                    accepted = True
                except RpcCallError as e:
                    if e.code != -32011:                 # -32011 = "already voted" (expected for double voters)
                        raise
                    accepted = False
                with lock:
                    if c.last_meta:
                        rpc_pids.add(c.last_meta["worker_pid"])
            else:
                form = [("csrf", csrf)] + [(f"pos_{p}", x) for p, v in picks.items() for x in v]
                _, html = timed("vote", lambda: c.post(f"/e/{ev}/vote", form))
                accepted = "Your vote was recorded" in html and "already voted" not in html
            with lock:
                outcomes.append((ev, sno, kind, accepted, picks, list(options)))
        except urllib.error.HTTPError as e:
            with lock:
                errors.append(f"{sno} vote: HTTP {e.code}")
        except Exception as e:
            with lock:
                errors.append(f"{sno} vote: {e}")

    print(f"Starting {n} simultaneous sessions "
          f"({args.users} students + {args.double} students voting twice), voting via "
          + ("RPC cast_vote (POST /rpc)" if args.mode == "rpc" else "the HTML form (POST /e/<event>/vote)") + "...")
    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker, args=s) for s in sessions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total_time = time.perf_counter() - t0
    time.sleep(2.0)                     # let the 1.5 s results cache expire
    after = {e: fetch_results(args.url, e) for e in event_ids}
    after_w = {e: fetch_workers(args.url, e, args.admin_password) for e in event_ids}

    # ---- verify (per event)
    accepted = [o for o in outcomes if o[3]]
    single_ok = sum(1 for o in accepted if o[2] == "single")
    per_student = {}
    for ev, sno, kind, ok, _, _ in outcomes:
        if kind == "double":
            per_student.setdefault((ev, sno), []).append(ok)
    double_ok = all(v.count(True) == 1 for v in per_student.values()) and len(per_student) == args.double
    tally_ok = ballots_ok = skips_ok = True
    n_skips = n_blank = n_multi = 0
    for e in event_ids:
        mine = [o for o in accepted if o[0] == e]
        expected, expected_skips = {}, {}
        for _, _, _, _, picks, positions in mine:
            for pos in positions:
                chosen = picks.get(pos, [])
                for cand in chosen:
                    expected[int(cand)] = expected.get(int(cand), 0) + 1
                if not chosen:
                    expected_skips[int(pos)] = expected_skips.get(int(pos), 0) + 1
                n_multi += len(chosen) > 1
            n_blank += not any(picks.values())
        tb, ta = tally(before[e]), tally(after[e])
        tally_ok &= all(ta.get(cid, 0) - tb.get(cid, 0) == expected.get(cid, 0) for cid in ta)
        ballots_ok &= after[e]["ballots"] - before[e]["ballots"] == len(mine)
        ab_b = {p["id"]: p.get("abstain") for p in before[e]["positions"]}
        ab_a = {p["id"]: p.get("abstain") for p in after[e]["positions"]}
        skips_ok &= all(ab_a[pid] - (ab_b.get(pid) or 0) == expected_skips.get(pid, 0) for pid in ab_a)
        n_skips += sum(expected_skips.values())

    def line(name, values):
        if values:
            print(f"  {name:7s} avg {statistics.mean(values):.2f}s   p95 {pct(values, 95):.2f}s   max {max(values):.2f}s")

    print("\nResponse times")
    line("login", lat["login"])
    line("ballot", lat["ballot"])
    line("vote", lat["vote"])
    print(f"  whole test took {total_time:.1f}s")

    before_by_pid, new_pids = {}, set()
    for e in event_ids:
        if after_w[e] is None:
            print("\n(Per-worker counts are admin-only and the admin login failed: use --admin-password.)")
            continue
        for w in before_w[e] or []:
            before_by_pid[(e, w["worker_pid"])] = w["ballots"]
        for w in after_w[e]:
            if w["ballots"] > before_by_pid.get((e, w["worker_pid"]), 0):
                new_pids.add(w["worker_pid"])
        workers = ", ".join(f"PID {w['worker_pid']}: {w['ballots']} ballots on {w['threads']} threads"
                            for w in after_w[e])
        print(f"\nParallel processing, event {e} ({after[e]['title']}): {workers or 'no ballots'}")
    if args.mode == "rpc":
        print(f"cast_vote procedures ran on {len(rpc_pids)} different worker PIDs: {sorted(rpc_pids)}")

    accepted_by_event = ", ".join(f"event {e}: {sum(1 for o in accepted if o[0] == e)}" for e in event_ids)
    checks = [
        (f"No errors or crashes ({len(errors)} errors)", not errors),
        (f"All {args.users} students voted successfully ({single_ok}/{args.users})", single_ok == args.users),
        (f"Double-vote attempts: exactly 1 accepted per student per event ({args.double} students)", double_ok),
        (f"Ballot count increased by exactly the accepted votes, per event ({accepted_by_event})", ballots_ok),
        ("Every candidate's tally matches the votes sent (per event)", tally_ok),
        (f"Skipped positions counted correctly ({n_skips} skips, {n_blank} blank ballots, "
         f"{n_multi} multi-candidate picks)", skips_ok),
    ]
    if args.expect_workers:
        if not new_pids and rpc_pids:              # no admin login: the cast_vote procedures' worker PIDs
            new_pids = set(rpc_pids)
        checks.append((f"New ballots spread across {args.expect_workers} worker processes "
                       f"({len(new_pids)} different PIDs: {sorted(new_pids)})", len(new_pids) >= args.expect_workers))
    print("\nResults")
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    for e in errors[:10]:
        print("   -", e)
    passed = all(ok for _, ok in checks)
    print("\nOVERALL:", "PASS" if passed else "FAIL")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
