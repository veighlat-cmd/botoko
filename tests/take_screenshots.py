"""Screenshots for the evidence (Part C), with the app running against the TEST database botoko_test.

  baseline_login.png, baseline_ballot.png, baseline_receipt.png, baseline_results.png
  parallel_panel_under_load.png : the admin Results tab's "Parallel processing" panel, captured WHILE 1,000 synthetic
                                  students are voting at the same time (concurrent setup)

Needs Playwright for Python + Microsoft Edge:  pip install playwright   (Edge is used through channel="msedge")
    python tests/take_screenshots.py
"""
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import botoko_testkit as kit  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Playwright is not installed: pip install playwright   (uses the installed Microsoft Edge)")

SHOTS = kit.EVIDENCE / "screenshots"
N = 1000


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    kit.reset_db()
    server = kit.Server(log_name="server-screenshots.log").start()
    notes = {"server": server.banner()}
    try:
        S = kit.seed(N)
        ev = S["main"]
        with sync_playwright() as pw:
            br = pw.chromium.launch(channel="msedge", headless=True)
            # ---- baseline: one student, login -> ballot -> receipt -> results
            c = br.new_context(viewport={"width": 1366, "height": 900})
            p = c.new_page()
            p.goto(kit.BASE + "/")
            p.wait_for_timeout(1200)
            p.screenshot(path=str(SHOTS / "baseline_login.png"))
            p.fill("#student_no", kit.voter_id(1))
            p.fill("#password", kit.STUDENT_PASSWORD)
            p.click("main form button[type=submit]")
            p.wait_for_url("**/events")
            p.goto(f"{kit.BASE}/e/{ev}/ballot")
            p.wait_for_timeout(900)
            n_pos = p.evaluate("document.querySelectorAll('fieldset.position').length")
            for i in range(n_pos):
                p.evaluate(f"document.querySelectorAll('fieldset.position')[{i}].querySelectorAll('label.choice')[0].click()")
            p.wait_for_timeout(600)
            p.screenshot(path=str(SHOTS / "baseline_ballot.png"), full_page=True)
            p.click("#submitBtn")
            p.wait_for_selector("#confirmBtn", state="visible")
            p.click("#confirmBtn")
            p.wait_for_url(f"**/e/{ev}/receipt", timeout=20000)
            p.wait_for_timeout(1800)
            p.screenshot(path=str(SHOTS / "baseline_receipt.png"), full_page=True)
            p.goto(f"{kit.BASE}/e/{ev}/results")
            p.wait_for_timeout(2800)
            p.screenshot(path=str(SHOTS / "baseline_results.png"), full_page=True)
            c.close()

            # ---- under load: 999 more students log in first, then all vote at the same time
            clients, login_errors = kit.login_many(range(2, N + 1))
            notes["logged_in"] = len(clients)
            a = br.new_context(viewport={"width": 1366, "height": 900})
            ap = a.new_page()
            ap.goto(kit.BASE + "/")
            ap.fill("#student_no", "admin")
            ap.fill("#password", kit.ADMIN_PASSWORD)
            ap.click("main form button[type=submit]")
            ap.wait_for_url("**/admin")
            ap.goto(f"{kit.BASE}/admin?event={ev}#results")
            ap.wait_for_timeout(2500)
            ap.click('#panel-results [data-r="view-more"]')          # the panel is under "View more"
            ap.wait_for_timeout(1200)
            panel = ap.locator('#panel-results [data-r="parallel"]')
            panel.scroll_into_view_if_needed()
            ap.wait_for_timeout(500)
            if not ap.evaluate("""() => { const el = document.querySelector('#panel-results [data-r="parallel"]');
                    const r = el.getBoundingClientRect(); return r.height > 50 && getComputedStyle(el.closest('.details-collapse')).opacity === '1'; }"""):
                raise RuntimeError("the Parallel processing panel is not visible")
            progress = [0]
            out = {}
            burst = threading.Thread(target=lambda: out.update(
                zip(("results", "seconds"), kit.vote_burst(clients, ev, S["main_ballot"], 100, progress))))
            burst.start()
            shown_sum = lambda: ap.evaluate(  # noqa: E731
                "[...document.querySelectorAll('#panel-results [data-wb]')].reduce((n, b) => n + Number(b.textContent), 0)")
            captured = False
            while burst.is_alive():
                ballots_shown = shown_sum()
                workers_shown = ap.locator("#panel-results .worker").count()
                if ballots_shown > 1 and progress[0] < len(clients) and workers_shown >= 1:
                    ap.wait_for_timeout(400)              # let the count-up animation settle a little
                    panel.screenshot(path=str(SHOTS / "parallel_panel_under_load.png"))
                    ap.screenshot(path=str(SHOTS / "parallel_panel_under_load_fullview.png"))
                    notes["capture"] = {"votes_sent_by_clients_at_capture": progress[0], "of": len(clients),
                                        "ballots_shown_in_panel": shown_sum(), "worker_cards_shown": workers_shown,
                                        "panel_text": panel.inner_text()[:400]}
                    captured = True
                    break
                time.sleep(0.2)
            burst.join()
            res = out.get("results", [])
            notes["burst"] = {"votes": len(res), "accepted": sum(1 for r in res if r[1]), "seconds": round(out.get("seconds", 0), 2)}
            notes["captured_during_run"] = captured
            ap.wait_for_timeout(3000)
            ap.screenshot(path=str(SHOTS / "parallel_panel_after_run.png"))
            n, distinct, pids = kit.q("""SELECT COUNT(*), COUNT(DISTINCT student_id), COUNT(DISTINCT worker_pid)
                                         FROM ballots WHERE event_id = %s""", (ev,))[0]
            notes["db_after"] = {"ballots": n, "duplicates": n - distinct, "worker_pids": pids,
                                 "expected_ballots": 1 + len(clients)}
            a.close()
            br.close()
    finally:
        server.stop()
    (kit.RAW / "screenshots.json").write_text(json.dumps(notes, indent=2), encoding="utf-8")
    print(json.dumps(notes, indent=2))


if __name__ == "__main__":
    main()
