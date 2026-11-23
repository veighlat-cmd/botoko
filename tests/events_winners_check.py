"""Student "Choose a voting event" page: winners ONLY on a published previous event's card. TEST DB botoko_test only.
Events: 1 current, 1 upcoming, 1 previous WITH published results, 1 previous WITHOUT. 360 / 1366 px, dark + light.
    py tests\\events_winners_check.py        (needs Playwright + Microsoft Edge)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import botoko_testkit as kit  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = kit.EVIDENCE / "screenshots"
results = []


def ok(label, cond, extra=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""), flush=True)


kit.reset_db()
server = kit.Server(log_name="server-events-winners.log").start()
try:
    S = kit.seed(30)                                            # "TEST Main Election" = CURRENT
    with kit.db() as c:
        up = kit._event(c, "TEST Upcoming Election", "now() + interval '2 days'", "now() + interval '5 days'")
        kit._ballot(c, up, kit.BALLOT[:1])
        pub = kit._event(c, "TEST Published Election", "now() - interval '1 day'", "now() + interval '3 days'")
        pub_b = kit._ballot(c, pub, kit.BALLOT)
        unpub = kit._event(c, "TEST Unpublished Election", "now() - interval '1 day'", "now() + interval '3 days'")
        unpub_b = kit._ballot(c, unpub, kit.BALLOT)
        with c.cursor() as cur:
            cur.executemany("INSERT INTO event_voters (event_id, student_id) VALUES (%s, %s)",
                            [(e, sid) for e in (up, pub, unpub) for sid in S["ids"].values()])
    # real votes (clear winners: P1, V1, C1 C2 C3 in the published event; P2 ... in the unpublished one)
    clients, _ = kit.login_many(range(1, 11))
    for i, cl in clients.items():
        cl.rpc("cast_vote", {"event_id": pub, "choices": {str(pub_b[0]["id"]): pub_b[0]["candidates"][0],
                                                          str(pub_b[1]["id"]): pub_b[1]["candidates"][0],
                                                          str(pub_b[2]["id"]): pub_b[2]["candidates"][:3]}})
        cl.rpc("cast_vote", {"event_id": unpub, "choices": {str(unpub_b[0]["id"]): unpub_b[0]["candidates"][1]}})
    kit.q("UPDATE events SET manual = 'closed', results_published = TRUE, results_published_at = now() WHERE id = %s", (pub,))
    kit.q("UPDATE events SET manual = 'closed', results_published = FALSE WHERE id = %s", (unpub,))

    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge", headless=True)
        for theme in ("dark", "light"):
            for w, h in ((360, 740), (1366, 900)):
                tag = f"{theme}-{w}"
                ctx = br.new_context(viewport={"width": w, "height": h})
                ctx.add_init_script(f"try{{localStorage.setItem('botoko-theme','{theme}')}}catch(e){{}}")
                p = ctx.new_page()
                errs = []
                p.on("pageerror", lambda e: errs.append(str(e)))
                p.goto(kit.BASE + "/")
                p.fill("#student_no", kit.voter_id(1))
                p.fill("#password", kit.STUDENT_PASSWORD)
                p.click("main form button[type=submit]")
                p.wait_for_url("**/events")
                p.wait_for_timeout(800)
                html = p.content()
                ok(f"{tag}: no separate winners block above the tabs ('Results are out' banner gone)",
                   "Results are out" not in html and p.locator(".results-out").count() == 0)
                ok(f"{tag}: Current + Upcoming sections show NO winners",
                   p.locator("#panel-current .event-winners, #panel-upcoming .event-winners").count() == 0
                   and "Official winners" not in p.inner_text("#panel-current") + p.inner_text("#panel-upcoming"))
                p.click("#tab-previous")
                p.wait_for_timeout(500)
                pc = p.locator(f'#panel-previous article[data-event-id="{pub}"]')
                uc = p.locator(f'#panel-previous article[data-event-id="{unpub}"]')
                ptext = pc.inner_text()
                ok(f"{tag}: the PUBLISHED previous card shows its winners (President P1, Vice President V1, Councilor C1-C3)",
                   pc.locator(".event-winners").is_visible() and "TEST Candidate P1" in ptext and "TEST Candidate V1" in ptext
                   and all(f"TEST Candidate C{k}" in ptext for k in (1, 2, 3)) and "TEST Candidate P2" not in ptext)
                ok(f"{tag}: the published card keeps 'View results & winners' (link to the full official results)",
                   pc.locator(f'a[href="/e/{pub}/results"]').count() >= 1)
                utext = uc.inner_text()
                ok(f"{tag}: the NOT-published previous card: 'Results will be announced soon', no winners, no names",
                   "Results will be announced soon" in utext and uc.locator(".event-winners").count() == 0
                   and "TEST Candidate" not in utext)
                ok(f"{tag}: winners appear exactly once on the page, inside Previous",
                   p.locator(".event-winners").count() == 1 and p.locator("#panel-previous .event-winners").count() == 1)
                ok(f"{tag}: no sideways scroll, no JavaScript errors",
                   not p.evaluate("document.documentElement.scrollWidth > innerWidth + 1") and not errs, " | ".join(errs[:2]))
                pc.scroll_into_view_if_needed()
                p.screenshot(path=str(OUT / f"events_previous_winners_{tag}.png"), full_page=(w == 360))
                ctx.close()
        # no previous events at all -> the normal empty state
        kit.q("UPDATE events SET manual = 'open' WHERE id IN (%s, %s)", (pub, unpub))
        ctx = br.new_context(viewport={"width": 1366, "height": 900})
        p = ctx.new_page()
        p.goto(kit.BASE + "/")
        p.fill("#student_no", kit.voter_id(2))
        p.fill("#password", kit.STUDENT_PASSWORD)
        p.click("main form button[type=submit]")
        p.wait_for_url("**/events")
        p.click("#tab-previous")
        p.wait_for_timeout(400)
        ok("no previous events: Previous shows its normal empty state, no winners anywhere",
           "No finished events yet" in p.inner_text("#panel-previous") and p.locator(".event-winners").count() == 0)
        ctx.close()
        br.close()
finally:
    server.stop()
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
