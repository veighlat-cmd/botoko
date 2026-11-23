BotoKo - test evidence (project tracker tasks 5.1 and 5.2)
Tested: 2026-11-18 on AMD Ryzen 3 7320U (4 physical / 8 logical cores), 7.3 GB RAM, Windows 11, Python 3.14.5,
PostgreSQL 18.6. Every result below comes from actually running the scripts.

SAFETY
- All tests use a SEPARATE database, botoko_test (local cluster, port 5433), dropped and re-created from schema.sql
  before each run. botoko_live is never opened by the scripts.
- The scripts start the app themselves (serve.py, the same launcher run-local.ps1 uses) on port 5071, using
  environment variables only (DATABASE_URL, PORT, a temporary SECRET_KEY, a random test admin password, and
  WEB_CONCURRENCY/THREADS for the sequential baseline). run-local.ps1 and .secret_key are not used or changed.
- Accounts are synthetic: Voter IDs TEST-0001, TEST-0002, ... with one random password per run that exists only in
  memory (not printed, not saved).

FILES
  Test_Cases.xlsx              TC-01 .. TC-20: steps, expected result, actual result, PASS/FAIL, date (task 5.1)
  Performance_Results.xlsx     averages, raw data of all 18 runs, test conditions (task 5.2)
  Performance_Chart.png        sequential vs concurrent total time for 100 / 500 / 1,000 votes
  screenshots\                 baseline_login/ballot/receipt/results.png, parallel_panel_under_load.png
                               (+ parallel_panel_under_load_fullview.png and parallel_panel_after_run.png)
  raw\                         JSON of every result, the console output and the test servers' logs

HOW TO RE-RUN (PowerShell, in the project folder C:\Users\cathyrine\Downloads\botoko\botoko)
  Needs: the local PostgreSQL cluster on port 5433 running (run-local.ps1 starts it), Python packages from
  requirements.txt plus:  py -m pip install openpyxl matplotlib requests
  1) Functional test cases TC-01..TC-20 (about 3 minutes). TC-15, 16, 17, 19 and 20 check what the SCREEN does
     by itself, so they use a real browser: py -m pip install playwright  (uses Microsoft Edge). Without
     Playwright those cases are reported as MANUAL, never as PASS.
       py tests\run_test_cases.py
  2) Performance comparison (about 30 minutes: 2 setups x 3 workloads x 3 runs, fresh database each run):
       py tests\perf_compare.py
     Options: --loads 100 500 1000   --repeats 3   --clients 100
     Rebuild the Excel file + chart from the saved raw data without measuring again:
       py tests\perf_compare.py --from-json
  3) Screenshots (needs Playwright + Microsoft Edge:  py -m pip install playwright):
       py tests\take_screenshots.py
  Close Performance_Results.xlsx / Performance_Chart.png before re-running (a file open in Excel can't be replaced;
  the script then saves under a dated name instead and says so).

HOW THE PERFORMANCE TEST WORKS
  Same workload on two setups of the real app: Sequential = 1 worker process x 1 thread; Concurrent = the normal
  auto-detected setup (4 worker processes x 8 threads on this laptop). All students log in first (not timed); then
  100 client threads are released at the same instant and send the votes (RPC cast_vote, what the ballot page sends)
  until every student has voted. Total time = first vote sent -> last answer received. After every run the database is
  checked: ballots stored = votes sent, 0 duplicates, each candidate's count = what was sent. Client and server run on
  the same laptop, so the client's own work is included in the timings.

RESULTS (average of 3 runs)
  Votes  Setup        Total time  Votes/s  Avg response  95th pct  Errors  DB check
  100    Sequential     2.29 s      43.8     1302 ms     2175 ms     0     PASS
  100    Concurrent     1.18 s      93.4      797 ms     1122 ms     0     PASS
  500    Sequential    10.97 s      45.9     1953 ms     2455 ms     0     PASS
  500    Concurrent     5.10 s     101.6      943 ms     1921 ms     0     PASS
  1000   Sequential    24.02 s      41.7     2291 ms     2894 ms     0     PASS
  1000   Concurrent    10.51 s      96.9     1015 ms     1974 ms     0     PASS
  Sequential time / concurrent time: 1.94 (100), 2.15 (500), 2.29 (1,000).
  Spread between the 3 runs: concurrent 0.74-1.55 s (100), 3.85-5.95 s (500), 9.39-12.62 s (1,000);
  sequential 2.18-2.45 s, 10.04-12.37 s, 23.15-24.65 s.

OBSERVATIONS (not failures)
  - TC-07: voting in an UPCOMING event is refused correctly, but the message is "Voting is closed." (the same as for
    a finished event) rather than "has not started yet".
  - Response times are around 0.8-2.3 s on average because 100 votes arrive at the same instant and wait in line;
    the per-vote work itself is short.

SUMMARY FOR THE TRACKER (paste-ready)
Task 5.1: We ran 14 automated functional test cases (TC-01 to TC-14) with real HTTP requests (browser-like sessions and
CSRF tokens) against a separate test database, and all 14 passed on 2026-11-18. They cover valid and invalid logins, the
admin login, pending registrations, casting a vote, duplicate votes, voting in upcoming or closed events, choosing too
many candidates, invalid or expired sessions, receipt downloads (PNG and JPG), live results updates, and the server
being unavailable. In the simultaneous-voting case, 100 different students voted at the same instant: all 100 votes
were accepted and stored exactly once (100 ballots, 0 duplicates, every candidate's count matched what was sent).

Task 5.2: We timed the same voting workload on a sequential server (1 worker x 1 thread) and on BotoKo's normal concurrent
server (4 worker processes x 8 threads) with 100, 500 and 1,000 votes, 3 runs each on a fresh database. On average the
concurrent server processed all votes in 1.18 s, 5.10 s and 10.51 s, compared with 2.29 s, 10.97 s and 24.02 s for the
sequential server, which is 1.94, 2.15 and 2.29 times faster (about 93-102 vs 42-46 votes per second). All 18 runs had
0 errors, and the database check confirmed every time that the votes stored equalled the votes sent, with zero
duplicates. The tests ran on one 4-core laptop (AMD Ryzen 3 7320U, 7.3 GB RAM) with the client on the same machine.

UPDATE 2026-11-19: DEFENSE FIXES (TC-15 .. TC-20, all PASS; TC-01 .. TC-14 still PASS)
  TC-15  Admin pending list: a new account appeared by itself after 2.0 s, and a new event registration after 2.3 s
         (no reload, filter and search kept, no duplicates). A row approved in another admin session was removed after 3.2 s.
  TC-16  Waiting student (phone size): "You're approved!" appeared 4.4 s after the admin approved, and the events page
         opened after 5.6 s, with no click. A rejected student saw the reason after 4.5 s.
  TC-17  "End voting now" with the confirmation "End voting now? Students will no longer be able to vote. This cannot be
         undone.": the event closed, its end time became now, the server log recorded who and when, the ballot closed
         and later votes were rejected ("Voting is closed for this event. Your ballot was not recorded.").
  TC-18  60 votes raced the close: 59 were fully counted and 1 was cleanly rejected. 0 duplicates and 0 incomplete
         ballots; votes sent after the close were rejected.
  TC-19  Auto-publish ON: an open results page showed the winners 2.1 s after voting ended. Auto-publish OFF: the
         results stayed hidden ("announced soon", RPC -32021) until "Publish official results"; the open page then
         showed the winners after 3.5 s.
  TC-20  Dashboard "Results by event": all 7 events listed with their status. Switching showed each event's own numbers
         (= database) without a page reload, the upcoming event showed "Voting has not started yet", ?results=<id>
         survives Back and refresh, and live updates follow only the selected event.

WHEN DO STUDENTS SEE THE WINNERS? (from the code)
  - Voting ends when the scheduled end time passes (checked on every request, so it takes effect immediately) or when
    the admin clicks "End voting now".
  - Auto-publish ON: the results are official the moment voting ends. An open results page asks the server every
    2 seconds (static/results.js), sees the final results and reloads itself with the winners at the top: within about
    2 s (measured 2.1 s).
  - Auto-publish OFF: after voting ends students see only "Voting has ended. The official results will be announced by
    the election committee soon." (no numbers; the server refuses them: /api/results 403, RPC -32021). An open live page
    switches to that message within about 2 s. The winners appear only after the admin clicks "Publish official
    results": the "announced soon" page checks every 5 seconds and then reloads itself, so within about 5 s
    (measured 3.5 s).
  - The admin always sees the final results (also before publishing).
