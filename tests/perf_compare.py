"""BotoKo performance evidence (project tracker task 5.2): sequential vs concurrent server.

The SAME workload is run against two setups of the real app (serve.py), both on the TEST database botoko_test,
with a fresh database before every run:
  - Sequential baseline: 1 worker process x 1 thread          (WEB_CONCURRENCY=1, THREADS=1)
  - Concurrent:          the normal auto-detected setup        (1 worker per physical core x 8 threads)
Workloads: 100, 500 and 1,000 votes from different synthetic students. Each combination runs 3 times.

What is timed: ONLY the voting. All students log in first (not timed). Then CLIENTS client threads are released
at the same instant and send the votes (RPC cast_vote, exactly what the ballot page sends) until every student has
voted. Total time = first vote sent -> last answer received. After each run the database is checked: ballots stored =
votes sent, 0 duplicates, every candidate's count = what was sent.

Outputs: test_evidence/Performance_Results.xlsx, test_evidence/Performance_Chart.png, test_evidence/raw/perf_runs.json

    python tests/perf_compare.py                       # 100 / 500 / 1000 votes, 3 runs each, 100 client threads
    python tests/perf_compare.py --loads 100 --repeats 1   # quick check
"""
import argparse
import datetime as dt
import json
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent))
import botoko_testkit as kit  # noqa: E402

SETUPS = [("Sequential (1 worker x 1 thread)", 1, 1), ("Concurrent (auto: workers x threads)", None, None)]


def one_run(setup_name, workers, threads, n_votes, clients, rep):
    kit.reset_db()
    server = kit.Server(workers, threads, log_name=f"server-perf-{'seq' if workers else 'conc'}-{n_votes}-{rep}.log").start()
    try:
        banner = server.banner()
        S = kit.seed(n_votes)
        idx = list(range(1, n_votes + 1))
        logged, login_errors = kit.login_many(idx)
        time.sleep(1.0)
        results, total = kit.vote_burst(logged, S["main"], S["main_ballot"], clients)
        lat = [r[2] for r in results]
        ok = sum(1 for r in results if r[1])
        errors = [r[3] for r in results if not r[1]] + login_errors
        check = kit.db_check(S["main"], [i for i in idx if i in logged], S["main_ballot"])
    finally:
        server.stop()
    run = {"setup": setup_name, "votes": n_votes, "repeat": rep, "server": banner, "client_threads": min(clients, n_votes),
           "sent": len(results), "accepted": ok, "errors": len(errors), "error_samples": errors[:5],
           "total_s": round(total, 3), "votes_per_s": round(ok / total, 1) if total else 0,
           "avg_ms": round(1000 * statistics.mean(lat), 1) if lat else 0,
           "p95_ms": round(1000 * kit.percentile(lat, 95), 1) if lat else 0,
           "max_ms": round(1000 * max(lat), 1) if lat else 0,
           "db_ballots": check["ballots"], "db_duplicates": check["duplicates"], "db_vote_rows": check["vote_rows"],
           "db_expected_vote_rows": check["expected_vote_rows"], "db_tally_matches": check["tally_matches"],
           "db_worker_pids": check["worker_pids"], "db_check_ok": check["ok"] and check["ballots"] == n_votes,
           "time": dt.datetime.now().isoformat(timespec="seconds")}
    print(f"  {setup_name:38s} {n_votes:5d} votes  run {rep}: {run['total_s']:7.2f} s  {run['votes_per_s']:7.1f} votes/s  "
          f"avg {run['avg_ms']:7.1f} ms  p95 {run['p95_ms']:7.1f} ms  errors {run['errors']}  "
          f"DB ok {run['db_check_ok']} ({run['db_ballots']} ballots, {run['db_duplicates']} dup, {run['db_worker_pids']} PIDs)",
          flush=True)
    return run


def conditions(clients):
    try:
        cpu = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],
                             capture_output=True, text=True).stdout.strip()
    except OSError:
        cpu = platform.processor()
    pgver = kit.pg("psql.exe", "-d", "postgres", "-tAc", "SHOW server_version").stdout.strip()
    sys.path.insert(0, str(kit.PROJECT))
    import concurrency
    p = concurrency.plan()
    return [
        ("Date", dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("CPU model", cpu),
        ("CPU cores", f"{psutil.cpu_count(logical=False)} physical / {psutil.cpu_count(logical=True)} logical"),
        ("RAM", f"{psutil.virtual_memory().total / 2**30:.1f} GB"),
        ("Operating system", f"{platform.system()} {platform.release()} ({platform.version()})"),
        ("Python", platform.python_version()),
        ("Web server", "serve.py (Waitress, worker processes sharing one socket) - the same launcher as run-local.ps1"),
        ("Sequential setup", "WEB_CONCURRENCY=1, THREADS=1 -> 1 worker process x 1 thread = 1 request at a time"),
        ("Concurrent setup", f"auto-detected: {p['workers']} worker processes x {p['threads']} threads = "
                             f"{p['workers'] * p['threads']} requests at a time (DB pool {p['pool']} per worker)"),
        ("Database", f"PostgreSQL {pgver}, local cluster port {kit.PG_PORT}, database {kit.TEST_DB} "
                     "(dropped and re-created from schema.sql before EVERY run); botoko_live not used"),
        ("Client", f"Python script on the same laptop; {clients} client threads released at the same instant "
                   "(fewer when there are fewer votes), each vote = one RPC cast_vote request on a new connection"),
        ("What is timed", "Only the voting: all students log in first (not timed); total time = first vote sent -> "
                          "last answer received"),
        ("Votes", "Each synthetic student TEST-xxxx votes once: President, Vice President (skipped by every 7th), "
                  "1-3 Councilors"),
        ("Repeats", "3 runs per setup and workload; the table shows the average of the 3 runs"),
        ("Note", "Client and server share the same CPU, so the client's own work is included in the timings"),
    ]


def write_outputs(runs, conds, loads):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    summary = []
    for n in loads:
        for name, _, _ in SETUPS:
            rs = [r for r in runs if r["votes"] == n and r["setup"] == name]
            if not rs:
                continue
            summary.append({"setup": name, "votes": n, "runs": len(rs),
                            "total_s": round(statistics.mean(r["total_s"] for r in rs), 3),
                            "votes_per_s": round(statistics.mean(r["votes_per_s"] for r in rs), 1),
                            "avg_ms": round(statistics.mean(r["avg_ms"] for r in rs), 1),
                            "p95_ms": round(statistics.mean(r["p95_ms"] for r in rs), 1),
                            "errors": sum(r["errors"] for r in rs),
                            "db_ok": all(r["db_check_ok"] for r in rs),
                            "dups": sum(r["db_duplicates"] for r in rs)})
    wb = Workbook()
    ws = wb.active
    ws.title = "Results (average of 3)"
    head = ["Setup", "Votes", "Runs", "Total time (s)", "Votes per second", "Avg response (ms)", "95th pct response (ms)",
            "Errors (all runs)", "Duplicates (all runs)", "DB check: stored = sent", "Sequential time / concurrent time"]
    ws.append(head)
    for s in summary:
        seq = next((x for x in summary if x["votes"] == s["votes"] and x["setup"].startswith("Sequential")), None)
        ratio = round(seq["total_s"] / s["total_s"], 2) if (seq and s["setup"].startswith("Concurrent") and s["total_s"]) else ""
        ws.append([s["setup"], s["votes"], s["runs"], s["total_s"], s["votes_per_s"], s["avg_ms"], s["p95_ms"],
                   s["errors"], s["dups"], "PASS" if s["db_ok"] else "FAIL", ratio])
    raw = wb.create_sheet("Raw runs")
    cols = ["setup", "votes", "repeat", "server", "client_threads", "sent", "accepted", "errors", "total_s", "votes_per_s",
            "avg_ms", "p95_ms", "max_ms", "db_ballots", "db_duplicates", "db_vote_rows", "db_expected_vote_rows",
            "db_tally_matches", "db_worker_pids", "db_check_ok", "error_samples", "time"]
    raw.append(cols)
    for r in runs:
        raw.append([("; ".join(r[c]) if isinstance(r[c], list) else r[c]) for c in cols])
    cs = wb.create_sheet("Test conditions")
    cs.append(["Item", "Value"])
    for k, v in conds:
        cs.append([k, v])
    for sheet in (ws, raw, cs):
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F2A44")
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        for col in sheet.columns:
            sheet.column_dimensions[col[0].column_letter].width = min(60, max(12, max(len(str(c.value or "")) for c in col) + 2))
        sheet.freeze_panes = "A2"
    cs.column_dimensions["B"].width = 110
    target = kit.EVIDENCE / "Performance_Results.xlsx"
    try:
        wb.save(target)
    except PermissionError:                      # open in Excel: keep the results, say where they went
        target = kit.EVIDENCE / f"Performance_Results_{dt.datetime.now():%Y%m%d-%H%M}.xlsx"
        wb.save(target)
        print(f"!! Performance_Results.xlsx is open in another program (Excel?). Saved as {target.name} instead.")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 5.2), dpi=150)
    width = 0.38
    xs = range(len(loads))
    colors = {"Sequential": "#94a3b8", "Concurrent": "#4f46e5"}
    for k, (name, _, _) in enumerate(SETUPS):
        vals = [next((s["total_s"] for s in summary if s["votes"] == n and s["setup"] == name), 0) for n in loads]
        pos = [x + (k - 0.5) * width for x in xs]
        bars = ax.bar(pos, vals, width, label=name, color=colors[name.split()[0]])
        for b, v in zip(bars, vals):
            ax.annotate(f"{v:.2f} s", (b.get_x() + b.get_width() / 2, b.get_height()), ha="center", va="bottom",
                        fontsize=9, xytext=(0, 3), textcoords="offset points")
    ax.set_xticks(list(xs))
    ax.set_xticklabels([f"{n:,} votes" for n in loads])
    ax.set_ylabel("Total time to process all votes (seconds, average of 3 runs)")
    ax.set_title("BotoKo: sequential vs concurrent server - total time (lower is better)")
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    chart = kit.EVIDENCE / "Performance_Chart.png"
    try:
        with open(chart, "wb") as fh:
            fig.savefig(fh, format="png")
    except OSError:                              # open in a viewer: keep the chart, say where it went
        chart = kit.EVIDENCE / f"Performance_Chart_{dt.datetime.now():%Y%m%d-%H%M}.png"
        fig.savefig(chart)
        print(f"!! Performance_Chart.png is open in another program. Saved as {chart.name} instead.")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loads", type=int, nargs="+", default=[100, 500, 1000])
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--clients", type=int, default=100, help="client threads sending votes at the same time")
    ap.add_argument("--from-json", action="store_true",
                    help="don't measure again: rebuild the Excel file and chart from test_evidence/raw/perf_runs.json")
    args = ap.parse_args()
    kit.EVIDENCE.mkdir(exist_ok=True)
    kit.RAW.mkdir(exist_ok=True)
    runs = []
    if args.from_json:
        runs = json.loads((kit.RAW / "perf_runs.json").read_text(encoding="utf-8"))
        args.loads = sorted({r["votes"] for r in runs})
    for n in ([] if args.from_json else args.loads):                          # interleaved: seq, conc, seq, conc, ... (same conditions over time)
        for rep in range(1, args.repeats + 1):
            for name, w, t in SETUPS:
                runs.append(one_run(name, w, t, n, args.clients, rep))
                (kit.RAW / "perf_runs.json").write_text(json.dumps(runs, indent=2), encoding="utf-8")
    conds = conditions(args.clients)
    summary = write_outputs(runs, conds, args.loads)
    (kit.RAW / "perf_summary.json").write_text(json.dumps({"summary": summary, "conditions": conds}, indent=2), encoding="utf-8")
    print("\nAverage of the runs:")
    for s in summary:
        print(f"  {s['setup']:38s} {s['votes']:5d} votes: {s['total_s']:7.2f} s, {s['votes_per_s']:7.1f} votes/s, "
              f"avg {s['avg_ms']:7.1f} ms, p95 {s['p95_ms']:7.1f} ms, errors {s['errors']}, DB check {'PASS' if s['db_ok'] else 'FAIL'}")
    print(f"-> {kit.EVIDENCE / 'Performance_Results.xlsx'} and Performance_Chart.png")


if __name__ == "__main__":
    main()
