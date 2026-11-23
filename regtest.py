"""BotoKo accounts + voting concurrency test (uses the JSON-RPC layer, standard library only).

  1. N students CREATE ACCOUNTS AT THE SAME INSTANT (threads + a barrier), plus extra "double"
     submissions of the same student number at the same instant
     -> exactly ONE account per student number, N different Voter IDs (given at once), 0 errors.
  2. The admin verifies all pending accounts (RPC verify_all_pending_accounts).
  3. Every student logs in with their Voter ID and registers for one of the events with one click
     (RPC register_for_event, at the same instant): auto-approved for verified accounts.
  4. ALL of them vote at the same instant, each sending its vote TWICE at once
     -> exactly one ballot per student per event (the other gets -32011), 0 errors, the new votes per
     candidate match what was sent, and the work is spread over the worker PIDs.

RUN IT ON A TEST DATABASE, NOT THE REAL ONE (it creates N test students 99-8-xxxxx and votes with them).
The server must allow many accounts from one IP address (this test runs on one computer), and the events
must have "Auto-approve registrations from verified accounts" on (the default):

  $env:REGISTER_LIMIT = "1000"; .\\run-local.ps1 -Port 5001 -Database botoko_test
  py regtest.py --url http://localhost:5001 --events 2,3 --students 50
"""
import argparse
import random
import sys
import threading
import time
from collections import Counter

from rpc_client import BotoKoClient, RpcError

FIRST = ["Andrea", "Bea", "Carlo", "Dana", "Enzo", "Faye", "Gabo", "Hana", "Ivan", "Jia", "Kiko", "Lara"]
LAST = ["Reyes", "Santos", "Cruz", "Bautista", "Garcia", "Mendoza", "Torres", "Ramos", "Flores", "Navarro"]


def at_once(jobs):
    """Run every job in its own thread, all released at the same instant. Returns results in order."""
    barrier = threading.Barrier(len(jobs))
    out = [None] * len(jobs)

    def run(i, job):
        barrier.wait()
        try:
            out[i] = ("ok", job())
        except RpcError as e:
            out[i] = ("rpc", e)
        except Exception as e:            # network error, timeout ...
            out[i] = ("error", e)

    threads = [threading.Thread(target=run, args=(i, j)) for i, j in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:5001")
    ap.add_argument("--events", default="2,3", help="CURRENT event ids, comma-separated (see rpc_client.py get_events)")
    ap.add_argument("--students", type=int, default=50)
    ap.add_argument("--doubles", type=int, default=10, help="extra same-number submissions at the same instant")
    ap.add_argument("--admin-password", default="admin123")
    args = ap.parse_args()
    events = [int(x) for x in args.events.split(",") if x.strip()]
    run = random.randint(10000, 99999 - args.students)          # fresh student numbers every run
    students = [{"school_student_no": f"99-8-{run + i:05d}", "event_id": events[i % len(events)],
                 "full_name": f"{random.choice(FIRST)} {random.choice(LAST)} {i + 1}", "course": random.choice(["BSIT", "BSCS", "BSIS"]),
                 "department": "College of Computing Studies", "year": random.randint(1, 4), "section": random.choice("ABC"),
                 "password": f"Test-pass-{run}-{i}"} for i in range(args.students)]
    failures = []

    def check(label, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + label + (f"  [{detail}]" if detail else ""))
        if not cond:
            failures.append(label)

    admin = BotoKoClient(args.url, verbose=False)
    admin.login_admin(args.admin_password)
    for ev in events:
        info = next((e for e in admin.call("get_events") if e["id"] == ev), None)
        if not info or info["status"] != "current":
            raise SystemExit(f"Event {ev} is not a CURRENT event. Use --events with current event ids.")
    base_registered = {ev: admin.call("get_turnout", {"event_id": ev})["registered"] for ev in events}

    # ---- 1. simultaneous account creation (+ doubles of the same student number at the same instant)
    print(f"\n1. {args.students} students create accounts at the same instant (+{args.doubles} double submissions)")
    jobs = []
    for s in students + students[:args.doubles]:
        def job(s=s):
            return BotoKoClient(args.url, verbose=False).call(
                "create_account", {k: s[k] for k in ("school_student_no", "full_name", "course", "department",
                                                     "year", "section", "password")})
        jobs.append(job)
    t0 = time.perf_counter()
    res = at_once(jobs)
    took = time.perf_counter() - t0
    if any(r[0] == "rpc" and r[1].code == -32019 for r in res):
        print("   The server refused: too many accounts from one IP. Start it with $env:REGISTER_LIMIT = \"1000\".")
    made = [r[1] for r in res if r[0] == "ok"]
    exists = [r[1] for r in res if r[0] == "rpc" and r[1].code == -32018 and "already exists" in r[1].message]
    other = [r[1] for r in res if not (r[0] == "ok" or (r[0] == "rpc" and r[1].code == -32018 and "already exists" in r[1].message))]
    check("0 errors", not other, "; ".join(str(e) for e in other[:3]))
    check(f"{args.students} accounts created, the {args.doubles} doubles got 'already exists'",
          len(made) == args.students and len(exists) == args.doubles, f"created {len(made)}, exists {len(exists)}, {took:.1f} s")
    vid = {m["school_student_no"]: m["voter_id"] for m in made}
    check(f"{args.students} different Voter IDs, given at once", len(set(vid.values())) == args.students
          and all(v.startswith("V-") for v in vid.values()))
    accts = admin.call("get_accounts", {"status": "pending"})["accounts"]
    mine = [a for a in accts if a["school_student_no"] in vid]
    check("exactly ONE account per student number, all PENDING VERIFICATION",
          len(mine) == args.students and len({a["school_student_no"] for a in mine}) == args.students)

    # ---- 2. admin verifies all pending accounts
    print("\n2. admin verifies all pending accounts (RPC verify_all_pending_accounts)")
    v = admin.call("verify_all_pending_accounts")
    check(f"verified {v['verified']}, 0 pending left", v["counts"]["pending"] == 0 and v["verified"] >= args.students)

    # ---- 3. log in + one-click registration (at the same instant)
    print(f"\n3. {args.students} students log in with their Voter ID and register for an event at the same instant")
    clients = []
    for s in students:
        c = BotoKoClient(args.url, verbose=False)
        c.login_student(vid[s["school_student_no"]], s["password"])
        clients.append((s, c))
    res = at_once([lambda s=s, c=c: c.call("register_for_event", {"event_id": s["event_id"]}) for s, c in clients])
    bad = [r for r in res if r[0] != "ok"]
    check("0 errors", not bad, "; ".join(str(r[1]) for r in bad[:3]))
    check("every registration auto-approved (verified accounts)", all(r[0] == "ok" and r[1]["status"] == "approved" for r in res))

    # ---- 4. everyone votes at the same instant, each sends the vote TWICE
    print(f"\n4. {args.students} students vote at the same instant (each sends TWICE)")
    ready = []
    for s, c in clients:
        ballot = c.call("get_ballot", {"event_id": s["event_id"]})
        picks = {}
        for p in ballot["positions"]:
            n = random.randint(0, p["seats"])                          # 0 = skip ("No vote")
            picks[str(p["id"])] = [x["id"] for x in random.sample(p["candidates"], min(n, len(p["candidates"])))]
        ready.append((s, c, picks))
    res_before = {ev: admin.call("get_results", {"event_id": ev}) for ev in events}
    jobs = []
    for s, c, picks in ready:
        for _ in range(2):
            jobs.append(lambda c=c, s=s, picks=picks: c.call("cast_vote", {"event_id": s["event_id"], "choices": picks}))
    t0 = time.perf_counter()
    res = at_once(jobs)
    took = time.perf_counter() - t0
    oks = [r[1] for r in res if r[0] == "ok"]
    dup = [r[1] for r in res if r[0] == "rpc" and r[1].code == -32011]
    other = [r[1] for r in res if not (r[0] == "ok" or (r[0] == "rpc" and r[1].code == -32011))]
    check("0 errors", not other, "; ".join(str(e) for e in other[:3]))
    check(f"{args.students} ballots recorded, {args.students} second sends refused (-32011 already voted)",
          len(oks) == args.students and len(dup) == args.students, f"ok {len(oks)}, refused {len(dup)}, {took:.1f} s")
    pairs = [(res[2 * i], res[2 * i + 1]) for i in range(args.students)]
    check("every student: exactly one of their two sends won", all(sum(1 for r in p if r[0] == "ok") == 1 for p in pairs))
    pid_list = Counter(r["recorded_by"]["worker_pid"] for r in oks)
    print(f"   worker processes that recorded the ballots: {dict(pid_list)}")
    check("spread over more than one worker process", len(pid_list) > 1, f"{len(pid_list)} PIDs")

    time.sleep(2)                                                 # results are cached 1.5 s
    for ev in events:
        sent = Counter(cid for s, c, picks in ready if s["event_id"] == ev for ids in picks.values() for cid in ids)
        after_r = admin.call("get_results", {"event_id": ev})
        old = {c["id"]: c["votes"] for p in res_before[ev]["positions"] for c in p["candidates"]}
        delta = {c["id"]: c["votes"] - old.get(c["id"], 0) for p in after_r["positions"] for c in p["candidates"]}
        check(f"event {ev}: new votes per candidate = what was sent",
              all(delta[cid] == sent.get(cid, 0) for cid in delta), f"{sum(sent.values())} votes")
        voters = sum(1 for s in students if s["event_id"] == ev)
        check(f"event {ev}: ballots +{voters}", after_r["ballots"] - res_before[ev]["ballots"] == voters)
        turnout = admin.call("get_turnout", {"event_id": ev})
        check(f"event {ev}: registered voters +{voters}", turnout["registered"] - base_registered[ev] == voters)

    print("\nOVERALL: " + ("PASS" if not failures else f"FAIL ({len(failures)}: {', '.join(failures)})"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
