"""BotoKo through an HTTPS tunnel (cloudflared quick tunnel), end to end, on the TEST database botoko_test.

Starts the app (serve.py) against botoko_test on port 5071, opens a quick tunnel to it, and tests THROUGH the
https://....trycloudflare.com address with real HTTP requests and a real browser (Playwright + Edge):
proxy headers (real visitor IP, https links, Secure cookie), create account -> admin approves -> log in -> register ->
vote -> receipt PNG/JPG -> duplicate vote rejected -> live results -> admin dashboard -> End voting now -> publish,
and 30 simultaneous votes (0 duplicates, 0 lost). Also checks that http://127.0.0.1:5071 and http://<laptop IP>:5071
still work and that direct visitors can't fake their IP. Writes test_evidence/raw/tunnel_test.json.

    py tests\\tunnel_test.py
"""
import io
import json
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import botoko_testkit as kit  # noqa: E402

CLOUDFLARED = r"C:\Program Files (x86)\cloudflared\cloudflared.exe"
LOCAL = kit.BASE
results = []


def ok(label, cond, extra=""):
    results.append({"check": label, "pass": bool(cond), "detail": str(extra)})
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""), flush=True)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def start_tunnel(port):
    log = kit.RAW / "cloudflared-test.log"
    log.unlink(missing_ok=True)
    proc = subprocess.Popen([CLOUDFLARED, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}",
                             "--logfile", str(log)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url, t0 = None, time.time()
    while time.time() - t0 < 60 and not url:
        time.sleep(1)
        if log.exists():
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", log.read_text(errors="replace"))
            url = m.group(0) if m else None
    t0 = time.time()
    while url and time.time() - t0 < 90:              # the new name needs a few seconds to work everywhere
        try:
            if requests.get(url + "/healthz", timeout=10).status_code == 200:
                return proc, url
        except requests.RequestException:
            pass
        time.sleep(2)
    proc.kill()
    raise RuntimeError(f"tunnel did not come up (url={url}); see {log}")


def main():
    kit.EVIDENCE.mkdir(exist_ok=True)
    kit.RAW.mkdir(exist_ok=True)
    kit.reset_db()
    server = kit.Server(log_name="server-tunnel-test.log").start()
    tunnel = None
    try:
        S = kit.seed(60, extra_events=True)
        with kit.db() as c:                                   # an auto-publish OFF event for End voting now + publish
            e_end = kit._event(c, "TEST Tunnel Election", "now() - interval '1 day'", "now() + interval '3 days'")
            b_end = kit._ballot(c, e_end, kit.BALLOT)
            with c.cursor() as cur:
                cur.executemany("INSERT INTO event_voters (event_id, student_id) VALUES (%s, %s)",
                                [(e_end, S["ids"][kit.voter_id(i)]) for i in range(1, 61)])
        tunnel, URL = start_tunnel(kit.APP_PORT)
        print("tunnel:", URL, flush=True)
        host = URL.split("//")[1]

        # ---------------- 1. proxy handling (HTTP level)
        kit.BASE = URL
        a = kit.Client()
        r = a.login("admin", kit.ADMIN_PASSWORD, follow=False)
        cookie = r.headers.get("Set-Cookie", "")
        ok("tunnel: admin login works, session cookie has Secure + HttpOnly", r.status_code in (302, 303) and "Secure" in cookie
           and "HttpOnly" in cookie, cookie.split(";")[0][:20] + "...; " + "; ".join(x.strip() for x in cookie.split(";")[1:]))
        a.get("/admin")
        a.fetch_rpc_token()
        bad = kit.Client()
        bad.login("admin", "wrong-password-for-the-ip-check")
        ip_row = kit.q("SELECT ip FROM form_attempts WHERE kind = 'admin_fail' ORDER BY id DESC LIMIT 1")
        seen_ip = ip_row[0][0] if ip_row else None
        ok("tunnel: rate limit / lockout see the REAL visitor IP (not 127.0.0.1)", seen_ip and seen_ip not in ("127.0.0.1", "::1"),
           f"recorded IP {seen_ip}")
        kit.q("DELETE FROM form_attempts")

        # ---------------- 2. student flow in a real browser over HTTPS
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            br = pw.chromium.launch(channel="msedge", headless=True)
            js_errors = []
            sctx = br.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, accept_downloads=True)
            st = sctx.new_page()
            st.on("pageerror", lambda e: js_errors.append(str(e)))
            st.goto(URL + "/account/create")
            st.fill("#school_student_no", "97-1-00001")
            st.fill("#full_name", "Tunnel Student")
            st.fill("#course", "BSIT")
            st.fill("#department", "TEST Department")
            st.select_option("#year", "1")
            st.fill("#section", "A")
            st.fill("#password", kit.STUDENT_PASSWORD)
            st.fill("#confirm_password", kit.STUDENT_PASSWORD)
            st.click("main form button[type=submit]")
            st.wait_for_load_state()
            vid = st.inner_text("#voter-id").strip() if st.locator("#voter-id").count() else ""
            ok("tunnel: create account -> Voter ID shown", vid.startswith("V-"), vid)
            st.goto(URL + "/")
            st.fill("#student_no", vid)
            st.fill("#password", kit.STUDENT_PASSWORD)
            st.click("main form button[type=submit]")
            st.wait_for_load_state()
            ok("tunnel: pending account logs in -> waiting page", st.url.endswith("/account"), st.url)
            sid = kit.q("SELECT id FROM students WHERE student_no = %s", (vid,))[0][0]
            # admin approves in the admin page (Accounts tab) through the tunnel
            actx = br.new_context(viewport={"width": 1366, "height": 900})
            ad = actx.new_page()
            ad.on("pageerror", lambda e: js_errors.append(str(e)))
            ad.goto(URL + "/")
            ad.fill("#student_no", "admin")
            ad.fill("#password", kit.ADMIN_PASSWORD)
            ad.click("main form button[type=submit]")
            ad.wait_for_url("**/admin")
            ad.goto(f"{URL}/admin?event={e_end}#accounts")
            row = '#acc-rows tr:has-text("Tunnel Student")'
            ad.wait_for_selector(row, timeout=15000)
            ad.click(row + " button:has-text('Approve')")
            ad.wait_for_load_state()
            approved = kit.q("SELECT account_status FROM students WHERE id = %s", (sid,))[0][0]
            ok("tunnel: admin approves the account (Approve button, CSRF form)", approved == "verified", approved)
            t0 = time.time()
            st.wait_for_url("**/events", timeout=20000)
            ok("tunnel: waiting student is taken to the events page by itself", True, f"{time.time() - t0:.1f} s")
            # register for the event (one click) and vote in the ballot page
            card = st.locator(f'article.event-card[data-event-id="{e_end}"]')
            card.locator("button:has-text('Register for this event')").click()
            st.wait_for_load_state()
            st.goto(f"{URL}/e/{e_end}/ballot")
            st.wait_for_timeout(1000)
            n = st.evaluate("document.querySelectorAll('fieldset.position').length")
            for i in range(n):
                st.evaluate(f"document.querySelectorAll('fieldset.position')[{i}].querySelectorAll('label.choice')[0].click()")
            st.wait_for_timeout(300)
            st.click("#submitBtn")
            st.wait_for_selector("#confirmBtn", state="visible")
            st.click("#confirmBtn")
            st.wait_for_url(f"**/e/{e_end}/receipt", timeout=20000)
            st.wait_for_timeout(800)
            code = st.inner_text("#receipt-code").strip()
            qr_link = st.inner_text(".paper-qr p")
            ok("tunnel: vote -> receipt; the receipt's verify link uses https://<tunnel>", code and f"https://{host}/verify" in qr_link,
               f"code {code}; {qr_link.splitlines()[1] if len(qr_link.splitlines()) > 1 else qr_link}")
            for fmt, want in (("png", "PNG"), ("jpg", "JPEG")):
                st.locator("[data-dl-menu] summary").click()
                with st.expect_download(timeout=30000) as dl:
                    st.evaluate("navigator.canShare = undefined")      # headless: no share sheet -> normal download
                    st.locator(f".dl-list a[href$='.{fmt}']").click()
                path = kit.RAW / f"tunnel-receipt.{fmt}"
                dl.value.save_as(path)
                im = Image.open(path)
                ok(f"tunnel: receipt {fmt.upper()} download is a valid image", im.format == want and im.size[0] == 1080,
                   f"{dl.value.suggested_filename}, {im.format} {im.size[0]}x{im.size[1]}")
            # duplicate vote rejected
            c = kit.Client()
            c.login(vid, kit.STUDENT_PASSWORD)
            c.fetch_rpc_token()
            dup = c.rpc("cast_vote", {"event_id": e_end, "choices": {str(b_end[0]["id"]): b_end[0]["candidates"][1]}})
            nb = kit.q("SELECT COUNT(*) FROM ballots WHERE student_id = %s AND event_id = %s", (sid, e_end))[0][0]
            ok("tunnel: duplicate vote rejected, still 1 ballot", dup.get("error", {}).get("code") == -32011 and nb == 1,
               dup.get("error", {}).get("message"))
            # live results page keeps updating through the tunnel
            st.goto(f"{URL}/e/{e_end}/results")
            st.wait_for_timeout(2500)
            before = st.text_content("[data-r='ballots']").strip()
            v2 = kit.Client()
            v2.login(kit.voter_id(2), kit.STUDENT_PASSWORD)
            v2.fetch_rpc_token()
            v2.rpc("cast_vote", {"event_id": e_end, "choices": kit.choices_for(2, b_end)})
            t0 = time.time()
            upd = None
            while time.time() - t0 < 15:
                if st.text_content("[data-r='ballots']").strip() == str(int(before) + 1):
                    upd = round(time.time() - t0, 1)
                    break
                time.sleep(0.3)
            ok("tunnel: open live results page shows a new vote by itself", upd is not None, f"{before} -> {int(before) + 1} after {upd} s")
            # admin dashboard: results by event card + switching
            ad.goto(f"{URL}/admin")
            ad.wait_for_timeout(2000)
            target = str(S["main"]) if ad.input_value("#dash-results-select") != str(S["main"]) else str(e_end)
            ad.select_option("#dash-results-select", target)
            switched = False
            for _ in range(60):
                if ad.get_attribute(".dash-results [data-dash-event]", "data-dash-event") == target:
                    switched = True
                    break
                time.sleep(0.25)
            ok("tunnel: admin dashboard loads, 'Results by event' switches without reload", switched and f"results={target}" in ad.url,
               f"switched to event {target}")
            # 30 simultaneous votes through the tunnel
            idx = list(range(21, 51))
            clients, login_errors = kit.login_many(idx, threads=15)
            if len(clients) < len(idx):                     # a login that hit a network hiccup: try it once more
                more, login_errors = kit.login_many([i for i in idx if i not in clients], threads=5)
                clients.update(more)
            res, secs = kit.vote_burst(clients, e_end, b_end, 30)
            acc = sum(1 for x in res if x[1])
            n_b, distinct = kit.q("""SELECT COUNT(*), COUNT(DISTINCT student_id) FROM ballots WHERE event_id = %s
                                      AND student_id = ANY(%s)""", (e_end, [S["ids"][kit.voter_id(i)] for i in idx]))[0]
            rows = kit.q("""SELECT COUNT(*) FROM votes v JOIN ballots b ON b.id = v.ballot_id WHERE b.event_id = %s
                            AND b.student_id = ANY(%s)""", (e_end, [S["ids"][kit.voter_id(i)] for i in idx]))[0][0]
            _, exp_rows = kit.expected_tally(idx, b_end)
            ok("tunnel: 30 simultaneous votes -> 30 stored, 0 duplicates, 0 lost, every vote row stored",
               len(clients) == 30 and acc == 30 and n_b == 30 and distinct == 30 and rows == exp_rows,
               f"logins {len(clients)}/30, accepted {acc}/30 in {secs:.2f} s, DB {n_b} ballots, {n_b - distinct} duplicates, "
               f"{rows}/{exp_rows} vote rows; errors: {[x[3] for x in res if not x[1]][:2] + login_errors[:2]}")
            # End voting now (button + confirmation) through the tunnel, then publish
            ad.goto(f"{URL}/admin?event={e_end}")
            ad.wait_for_timeout(800)
            ad.locator(".admin-head .toggle-form button").click()
            ad.wait_for_selector("#confirm-ok", state="visible")
            ad.click("#confirm-ok")
            ad.wait_for_timeout(3000)
            closed = kit.q("SELECT manual FROM events WHERE id = %s", (e_end,))[0][0]
            late = kit.Client()
            late.login(kit.voter_id(55), kit.STUDENT_PASSWORD)
            late.fetch_rpc_token()
            lr = late.rpc("cast_vote", {"event_id": e_end, "choices": kit.choices_for(55, b_end)})
            ok("tunnel: End voting now (with confirmation) -> closed; a later vote is rejected",
               closed == "closed" and lr.get("error", {}).get("code") == -32010, lr.get("error", {}).get("message"))
            st.goto(f"{URL}/e/{e_end}/results")
            st.wait_for_timeout(1500)
            pending = "announced by the election committee soon" in st.inner_text("main")
            page = a.get(f"/admin?event={e_end}")
            pub = a.post("/admin/results/publish", {"csrf": a.csrf_in(page.text), "event_id": str(e_end), "tab": "overview"})
            t0 = time.time()
            try:
                st.wait_for_selector(".winners-announce", timeout=20000)
                t_w = round(time.time() - t0, 1)
            except Exception:                                   # noqa: BLE001
                t_w = None
            ok("tunnel: results hidden until published, then the open page shows the winners by itself",
               pending and pub.status_code == 200 and t_w is not None, f"announced-soon page: {pending}; winners after {t_w} s")
            ok("tunnel: no JavaScript errors in the browser", not js_errors, " | ".join(js_errors[:3]))
            br.close()

        # ---------------- 3. local addresses keep working, direct visitors can't fake their IP
        kit.BASE = LOCAL
        loc = kit.Client()
        r = loc.login(kit.voter_id(3), kit.STUDENT_PASSWORD, follow=False)
        ok("local http://127.0.0.1: login works, cookie NOT Secure (plain http keeps working)",
           r.status_code in (302, 303) and "Secure" not in r.headers.get("Set-Cookie", ""))
        ip = lan_ip()
        if ip:
            kit.BASE = f"http://{ip}:{kit.APP_PORT}"
            lan = kit.Client()
            page = lan.get("/")
            lan.s.headers["X-Forwarded-For"] = "1.2.3.4"
            lan.post("/", {"csrf": lan.csrf_in(page.text), "student_no": "admin", "password": "wrong-again"})
            seen = kit.q("SELECT ip FROM form_attempts WHERE kind = 'admin_fail' ORDER BY id DESC LIMIT 1")
            ok(f"laptop IP http://{ip}: works, and a faked X-Forwarded-For is IGNORED for direct visitors",
               page.status_code == 200 and seen and seen[0][0] == ip, f"recorded IP {seen[0][0] if seen else None}")
            r = lan.get(f"/receipt/AAAAAAAA.png")
            kit.BASE = LOCAL
    finally:
        kit.BASE = LOCAL
        if tunnel:
            tunnel.kill()
        server.stop()
    (kit.RAW / "tunnel_test.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    n = sum(1 for r in results if r["pass"])
    print(f"\n{n}/{len(results)} passed")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
