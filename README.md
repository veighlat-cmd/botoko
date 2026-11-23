# BotoKo
**A Concurrent Online Voting System for Student Council Elections with Real-Time Tallying**

Web app (Python + Flask + PostgreSQL). Bubuksan lang ng mga estudyante ang link sa phone nila, walang i-install.

---

## What's new: fixes before the defense (November 19, 2026)

1. **Admin: new pending students appear by themselves.** The 4-second poll of the Accounts / Registrations tabs
   (`GET /admin/api/registrations?rows=1`, admin only) now also sends the pending rows. They are drawn with the same
   template as the page (`templates/_admin_rows.html`). `admin.js` adds the new rows without a reload and removes rows
   approved or rejected elsewhere. The filter, search, ticked boxes and scroll position are kept, nothing changes while
   an approve / reject is being sent, and polling pauses while the browser tab is hidden.
2. **Waiting student moves on by themselves.** The "waiting for verification" page asks `GET /account/status.json`
   (the student's own status only) every 4 seconds, paused while the tab is hidden. Verified: "You're approved!", then
   the events page. Rejected: the reason.
3. **End voting now.** On the admin event page the open event has an **End voting now** button. It asks "End voting now?
   Students will no longer be able to vote. This cannot be undone.", sets the end time to now, writes who / when to
   the server log, and a finished event has no reopen button. Votes take a shared lock on the event and ending voting
   takes it exclusively, so a ballot being saved at that moment is fully counted and every later one is rejected
   ("Voting is closed for this event. Your ballot was not recorded."). Publishing follows the event's auto-publish setting.
4. **Dashboard: Results by event.** The admin's first page has a picker with every event (Current / Upcoming /
   Previous, with its status). Choosing one loads its results, turnout and charts without reloading
   (`GET /admin/api/results-card`, admin only) and keeps it in the address (`?results=<id>`). An upcoming event shows
   "Voting has not started yet". The Parallel processing panel stays only in the Results tab.
5. **Results pages follow publishing.** An open live page switches to "announced soon" (or to the winners, with
   auto-publish) within about 2 s after voting ends. The "announced soon" page checks every 5 s and shows the winners
   as soon as they are published.

## What's new: download the receipt as an image (November 17, 2026)

On the receipt page (right after voting, and later from My Account > Vote History) there is a
**Download receipt** button with **Download as PNG** / **Download as JPG** (Print and Save as PDF are unchanged).

- **Drawn on the server** (`receipt_image.py`, Pillow + the QR code from segno + the bundled Inter font), so it looks the
  same on every phone and laptop and needs no internet. It shows the same things as the on-screen receipt:
  - the BotoKo logo and name, the school, the event, the school year and "OFFICIAL VOTING RECEIPT";
  - the receipt number, the verification code, the date and time (Philippine time) and the masked Voter ID;
  - every position with its choice(s) or NO VOTE, and the number of positions voted and skipped;
  - the QR code, the thank-you footer and a dashed tear-off border.

  White background, 1080 px wide. The file name looks like `BotoKo-receipt-SSC-Election-2026-A1B2C3D4.png`.
- **Address:** `GET /receipt/<verification code>.png` or `.jpg`. Only the student who owns the receipt, or the admin,
  can download it; another student or someone not logged in gets **403**. `Cache-Control: no-store`, like the receipt page.
- **Phones:** opens the share sheet (Web Share API with files) when the phone has one, otherwise a normal download.
  The button shows a spinner while the image is made, then the message "Receipt saved".
- **New library:** `Pillow` (in `requirements.txt`).

## What's new: 5 changes (November 16, 2026)

1. **Login fields are never pre-filled** (shared voting devices). The login, Create account, Change password and
   Forgot Voter ID forms use `autocomplete="off"` / `"new-password"`. `static/app.js` (`form[data-no-autofill]`)
   empties anything the browser fills in during the first ~2 seconds (until the person types), empties the fields
   again after Back, and sends the password in a hidden field so the browser doesn't offer to save it.
   The login pages, and every page of a logged-in student or the admin, are `Cache-Control: no-store`.
   Show / Hide password still works.
2. **Event list ("Choose a voting event"):** a **Log out** button where Back is on other pages (asks "Log out of
   BotoKo?", then the normal CSRF-protected logout), and no "Events" button in the header on that page.
3. **"Parallel processing" is admin-only.** It is only in the admin dashboard's Results tab. Student and projector
   results pages don't contain it at all, and `/api/results` + RPC `get_results` no longer send worker PIDs or
   threads. The admin card gets them from the admin-only RPC `get_worker_stats` (or `GET /admin/api/workers`).
   `loadtest.py` logs in as the admin for those numbers (`--admin-password`, default `$ADMIN_PASSWORD`).
4. **Friendlier "session expired":**
   - **Fixed secret key:** `run-local.ps1` (and `serve.py` / `app.py`) use `SECRET_KEY`, else the file
     `.secret_key`, made once and reused (it's in `.gitignore`: never commit or share it). Restarting the app no
     longer logs anyone out or breaks open pages. On Render, `SECRET_KEY` is generated once and kept.
   - **Old token on the login form:** the login page again with "Please log in again.". If someone is already
     logged in in this browser: "You're already logged in as ... in this browser" (Continue / Log out).
   - **Old token on any other form:** back to the same page with "This page was open too long or your login
     changed in another tab. Please try again." (nothing from the old form is done). On the ballot, the choices
     stay selected and pressing Confirm again works.
   - **Login changed in another tab:** the other open BotoKo tabs show "You logged in as ... in another tab -
     reload to continue".
   - **Tab back after 10+ minutes:** it gets a fresh token (`GET /session/refresh`) if the login is unchanged.
   - **Forged or missing tokens are still refused** (forms: nothing done, redirect to our own page only;
     RPC: error -32003).
5. **New event / Event details form:** one aligned CSS grid (`.form-grid` in `static/dash.css`):
   - Event name | School / organization
   - What is this voting for? (full width)
   - Voting starts | Voting ends, with "Philippine time" under each
   - School year
   - the two switches
   - Cancel + Create event / Save changes at the bottom right

   All single-line admin fields are the same height. One column on tablets and phones. "New event" opens a
   separate card under the Voting event selector, titled "Create a new voting event".

## What's new: live results bar graph (November 16, 2026)

**Overview** (top of the Live results page and the admin Results tab): each position is now a small
card with a **bar graph**. Every candidate has a bar with their **photo on top** and their **name and
party under it**. The bars are sorted by votes, so the leader is always first and gets a gold ring,
a crown and a "Leading" badge ("Winner" when voting has ended). Each bar shows the votes and the %.
The leader's bar is full height and the others are drawn relative to it.
When a position has many candidates, or on a narrow phone, the bars lie down (one row per candidate).
The bars grow and re-order smoothly as votes come in; with "reduce motion" nothing moves.

**View more** (the detailed graphs are kept): under each position name it now says who is leading,
the leader's row has a gold highlight, the bars are thicker with rounded ends, and the donut legend
no longer squeezes long names.

Code: `renderOverview()` in `static/results.js`, styles in `static/dash.css` ("LIVE RESULTS").

## What's new: the BotoKo seal logo (November 15, 2026)

The official "BOTO KO · BOSES KO" seal is now the logo everywhere: the top bar, the admin sidebar,
the login / create-account / forgot-ID pages, the printed receipt and voter slips, and the browser tab icon.

| File (in `static/`) | Used for |
|---|---|
| `logo-64.png`, `logo-128.png`, `logo-192.png`, `logo-512.png` | the logo; the browser picks the sharpest one for the screen (`ui.logo(size)` in `templates/_macros.html`) |
| `favicon.ico`, `favicon-32.png` | browser tab icon |
| `apple-touch-icon.png` | icon when a phone adds the site to its home screen |

To change the logo later, replace these PNG files with new ones of the same names and sizes.

## What's new: dark dashboard style (November 15, 2026)

- **Dark by default:** near-black navy background, violet/blue accents, tinted cards and coloured status pills.
  - The sun/moon button in the header switches to light mode, and the choice is remembered on that device.
  - Printing always uses dark text on white.
- **Admin dashboard:**
  - A **left sidebar** grouped as Overview / Management / System. It collapses to icons on desktop and slides in from the left on phones.
  - A slim top bar with a breadcrumb, a "System live" pill, a bell for approvals waiting (it links to Accounts/Registrations), the theme toggle and the Admin avatar.
  - The **Dashboard** shows six tinted stat cards (events, open now, registered voters, votes cast, turnout, not yet voted).
  - Below them: a **voter turnout by election** bar chart, a **latest results** donut (vote share in the event's biggest race), a **recent elections** table with status pills and turnout bars, and **quick actions**.
  - The integrity checks and latest ballots (worker PIDs) are still there.
  - The charts are plain CSS drawn from the server's numbers (no chart library).
- **Students:** the same look on the login, events, ballot, results and My Account pages.
- **Unchanged:** the ballot card sizes are identical to before at 360, 768, 1366 and 1920 px. Logic, locking, CSRF and RPC are unchanged.
- Styles: `static/dash.css` (loaded after `style.css` and `theme.css`).

## What's new: official results, winners and publishing (November 15, 2026)

- **Publish official results:**
  - When an event ends (its end time, or closed by the admin), the final results are visible **only to the admin**, marked "Final results - not yet published".
  - The admin reviews them and clicks **Publish official results** (with a confirmation). Only then do students see the final results and winners.
  - **Unpublish** undoes a mistake.
  - Event setting **"Publish results automatically when voting ends"** (default OFF).
- **What students see before publishing:**
  - The results page says "Voting has ended. The official results will be announced by the election committee soon."
  - The event card says "Results will be announced soon".
  - The server refuses the numbers everywhere: `/api/results` returns 403 and RPC `get_results` returns -32021.
- **Admin, finished event:**
  - A **Winners** card at the top of the Overview: each position with its winner(s), photo, name, party, votes and % of voters (top N for multi-seat).
  - Ties are shown as "Tie for last seat - needs a decision".
  - **Print / Save as PDF** opens the official results report: event, voting start and end, registered voters, total votes, turnout, full results per position, winners, and signature lines for the COMELEC chair. It has its own print stylesheet (A4).
  - **Download results (CSV)**.
- **Students, after publishing:**
  - A banner on the event list: "Results are out for <event> - see the winners" (events published in the last 14 days).
  - The results page starts with a **Winners** announcement: big cards per position, photo, name, party, votes, a Winner badge, and a short celebration animation (none with reduced motion). Below it, the overview and "View more" are unchanged.
- **Unchanged:**
  - Results are always the server's count-based tally from the database (`COUNT` of vote rows), never from the browser.
  - Live results during voting are unchanged.
- **Safe upgrade:** new columns `events.auto_publish`, `results_published` and `results_published_at`. Events that had already ended stay visible (marked published).
- **Tested:**
  - 37 HTTP checks, including the tie case, the count-based tally against the database, CSRF on publish, and students blocked from the report/CSV.
  - Playwright at 360/1366 px (38/38), including a real A4 PDF of the report.
  - The earlier suites, `regtest.py` and `loadtest.py` all pass.

## What's new: LOGIN FIRST, student accounts verified once (November 15, 2026)

**This is how BotoKo works now.** The older "What's new" sections below describe earlier versions, for example per-event login pages and the per-event registration form. Those are replaced by this flow; their old URLs redirect here.

**For students**
1. **Opening the site shows the login page** (Voter ID + password). Every student page needs a login. If you open a link while logged out, you log in first and then go back there.
2. **Create account** (link on the login page):
   - Fill in your school student number (e.g. 23-1-00479), full name, course, school/department, year, section, and a password (8+ characters). Email is optional.
   - Your **Voter ID (V-XXXXXX) is shown at once** in big text, with a copy button: "Save your Voter ID; you'll use it to log in."
   - Status: **PENDING VERIFICATION**. One account per student number: a second try says "An account with this student number already exists. Log in instead."
3. **Forgot your Voter ID?** Enter your student number and password to see your Voter ID and account status.
4. **Pending accounts:** you can log in, but you only see "Your account is waiting for verification by the election committee." and your profile. A rejected account sees the reason.
5. **Verified accounts** land on **Choose a voting event** (Current / Upcoming / Previous). Each card shows your state:
   - **Register for this event** (one tap; your details come from your account), then
   - **Registration pending**, then
   - **Vote now**, then
   - **Voted · view receipt**.
   - Upcoming events show only the name, description, dates and a countdown. Previous events show results, turnout and winners.
6. **My Account** (menu): profile, Voter ID, registrations with status, vote history with receipts, change password, log out.

**For COMELEC (admin)**
- Log in on the same first page with `admin` and the admin password. There's no visible admin link anywhere.
- **Accounts tab** (red badge = pending). Verify each student **once**:
  - Approve, Approve selected, Approve all pending, or Reject with a reason.
  - Search, and filter Pending / Verified / Rejected. The badge updates live.
- **Registrations per event:** the event setting **"Auto-approve registrations from verified accounts"** (Settings, default **ON**) approves one-tap registrations at once, so you don't approve twice.
  - Turn it off to approve each registration in the Registrations tab.
  - Re-registering after a rejected registration always waits for you, even when the setting is on.
- **Open projector view** (dashboard header): the live results in full screen.
- The class-list upload (Students tab) still works. Accounts made that way are already verified.

**Safe upgrade:**
- New columns: `students.account_status / account_reason / email / created_at` and `events.auto_approve`. Accounts that existed before count as verified.
- Old-style online registrations that had no account yet become PENDING accounts with the same password. The student finds the Voter ID with "Forgot your Voter ID?".
- Nothing is dropped.

**Removed (their URLs redirect):**

| Old page | Now goes to |
|---|---|
| per-event login `/e/<id>/login` | login page, then back to that event |
| per-event registration form `/e/<id>/register` | now a one-tap POST |
| `/register` | Create account |
| `/register/status` | Forgot your Voter ID |

**Tested** (separate test database, 4 worker processes):
- `regtest.py`: 50 students create accounts at the same instant, plus 10 double submissions.
  - 0 errors, exactly 1 account per student number, and 50 different Voter IDs.
  - Then: verify all, log in, one-tap register (auto-approved), and 50 simultaneous votes each sent twice.
  - Result: 50 ballots, 50 refused with -32011, 0 errors, the totals match, and the ballots were spread over 4 PIDs.
- 64 HTTP checks of the new rules.
- Playwright at 360 and 1366 px (39/39): open site, login page, create account, Voter ID, pending message, admin approves, log in, event list, register, vote, receipt, My Account.
- The earlier suites still pass: events 30/30, stages 46/46, admin security 31/31, admin browser 15/15, and `loadtest.py` PASS.

## What's new: "Log in" everywhere + My Account (November 15, 2026)

Students can log in even when **no voting event is open**.

- **Log in button:** shown in the top navigation of every public page (event list, upcoming, results, register, status). The empty "No voting is open right now" state has one too.
- **The login page (`/login`):**
  - Voter ID + password opens **My Account**. `admin` + the admin password still opens the dashboard (no visible admin link).
  - Links: "Don't have an account? Register" (choose an event) and "Forgot your Voter ID? Check registration status".
- **My Account (`/account`):**
  - Profile: Voter ID, name, student number, course, department, year-section.
  - **My registrations:** each event with Pending / Approved / Rejected (+ reason) and the event's Upcoming / Current / Previous status.
  - **Vote now** for each current event the student is approved for and hasn't voted in yet.
  - **Vote history:** date and receipt link.
  - **Register for another event**, Change password, Log out.
  - Empty state: "No voting events right now. You'll see them here when they open."
- **Pending or rejected students** (registered online, no Voter ID yet) log in with their **school student number** and the password they chose.
  - They see their status in My Account but can't vote; ballots still require a Voter ID login and an approved registration.
  - Once approved, My Account shows their Voter ID.
- **Unchanged:** logging in on an event's own login page still goes straight to that event's ballot. Registration rules, locking, CSRF and the admin login are unchanged.

## What's new: admin logs in on the student login page (November 13, 2026)

There is **no admin link** on any public or student page any more.

- **Logging in:**
  - On any login page, type **`admin`** as the Voter ID (any capitals) and the **admin password**. You go straight to the COMELEC dashboard, even from an event's login page.
  - A wrong admin password shows the normal "Wrong Voter ID or password", so nobody can tell that "admin" is special.
  - Only the username `admin` with `ADMIN_PASSWORD` gives admin access. A student's Voter ID or password never does.
- **Old addresses:** `/admin` and `/admin/login` still work; they open the login page. With no event open (or several), `/login` is a general login page.
- **Lockout:** 5 wrong admin passwords from one IP address block admin login from that IP for **15 minutes**. Students can still log in.
  - Every failed or blocked admin attempt is printed in the terminal, with the IP address and time.
  - During a lockout even the right password gets the normal error message. Wait 15 minutes.
- **Session:**
  - The admin is logged out after **30 minutes without activity**. The dashboard's auto-refresh doesn't count.
  - Logging in starts a brand-new session.
  - Cookies are HttpOnly and SameSite=Lax, and Secure on HTTPS (Render; `COOKIE_SECURE=0` only on this laptop's plain http).
- **Logout:** an always-visible **Log out** button on the dashboard and in the header. It's a POST with a CSRF token, like every admin action and the login form.
- **No caching:** admin pages are sent with `Cache-Control: no-store`, so pressing Back after logging out doesn't show admin data.
- **Weak password warning:** if `ADMIN_PASSWORD` is weak (e.g. `admin123`, or shorter than 10 characters), the dashboard shows a banner and the terminal prints "Change the admin password before the real election". To set it:
  - **This laptop:** in `run-local.ps1`, change the line `$env:ADMIN_PASSWORD = "admin123"`. Or type `$env:ADMIN_PASSWORD = "your-long-password"` in the terminal before `.
un-local.ps1`.
  - **Render:** Dashboard → botoko service → Environment → `ADMIN_PASSWORD` → Save.
- `rpc_client.py --admin-password ...` logs in the same way (Voter ID `admin`).
- **Fix in `serve.py` (Windows multi-process server):** each worker now takes new connections with a blocking `accept()` in its own thread, instead of Waitress' `select()` on the socket shared by all workers.
  - Before, about 1 request in 1,500 got no answer during bursts of 100 simultaneous connections. Bursts like that happen when many students press Submit together.
  - After: 0 lost in 6,000, still spread evenly over the 4 worker PIDs.
  - Render (Gunicorn on Linux) was not affected.

## What's new: online registration with COMELEC approval (November 13, 2026)

Students now **register themselves** for an event. COMELEC approves or rejects each request.

**How a student registers**
1. On the event list (Current or Upcoming), tap **Register to vote**. The event's login page also has "Not registered yet? Register here".
2. Type your **school student number** (e.g. 23-1-00479).
3. Fill in your full name, course, school/department, year and section, plus an optional email. **Choose a password** (at least 8 characters, with a strength hint).
   - If that student number **already has a BotoKo account**, for example from another event, BotoKo does **not** make a second account. It asks for the password you **already use** and adds this event to the same account.
4. You see: "Registration submitted. Status: PENDING. Wait for the election committee (COMELEC) to approve it."
5. **Check registration status** (link on the event list): type your student number and password. It shows each event as PENDING, APPROVED, or REJECTED with the reason. Once approved, it shows your **Voter ID** (V-XXXXXX) with a copy button. Log in with that Voter ID and **your own password**.

**How COMELEC approves** (Admin → choose the event → **Registrations** tab)
- A red badge on the tab shows how many are pending. It updates live every few seconds, and a banner says when new ones come in.
- The table lists student no., name, course, department, year-section, when it was submitted, and the status. You can search and filter by Pending / Approved / Rejected.
- **Approve** one, **Approve selected**, or **Approve all pending**.
  - Approving a new student creates their account with a Voter ID and adds them to the event's voters.
- **Reject** with a reason. The student sees the reason, can fix their details and register again.
- The class-list upload in the Students tab still works as another way to register students.

**Login rules**
- Pending: "Your registration for this event is still pending approval."
- Rejected: "Your registration was rejected: <reason>."
- Never registered: the not-registered warning, as before.

**Protection**
- CSRF on every form.
- Passwords are hashed like all the others (pbkdf2).
- One request per school student number per event, enforced by `UNIQUE (event_id, school_student_no)` plus a per-student lock, so double submissions at the same instant still store one request.
- **Rate limit per IP address, kept in the database** so it works across all workers:
  - 10 registrations per 10 minutes (`REGISTER_LIMIT`)
  - 20 wrong status checks per 10 minutes (`STATUS_LIMIT`)
  - Wrong existing passwords on the registration form count toward the 10.
- If a whole class registers from the **same school Wi-Fi**, they share one IP address. Raise the limit before starting the app, e.g. `$env:REGISTER_LIMIT = "100"`. On Render, `TRUST_PROXY=1` (set in render.yaml) makes the limit use each student's real IP address.

**Clean database for the real election**
- `run-local.ps1` now uses a new, empty database called **`botoko_live`** by default, and creates it the first time.
  - It has no sample students, candidates, votes or events. Create the real event, positions and candidates in the admin page.
- The old databases are kept as backups:
  - `botoko`: the earlier one, with the test students
  - `botoko_demo`: the demo events
- To use one again:
  ```
  .\run-local.ps1 -Database botoko        # the old database
  .\run-local.ps1 -Database botoko_demo   # the demo events
  ```

**Tested** (on a separate test database, 4 worker processes):
- `regtest.py`: 50 registrations at the same instant, plus 10 double submissions of the same number.
  - 0 errors, exactly 1 request per student per event.
  - Approve all, then 50 accounts with 50 different Voter IDs.
  - All 50 voted at the same instant, each sending twice: 50 ballots, 50 refused with -32011, 0 errors.
  - New votes equal what was sent, spread over 4 worker PIDs.
- 49 HTTP checks of the registration rules.
- Playwright at 360 px and 1366 px (39/39): register, pending, admin sees the live badge and approves, the status page shows the Voter ID, log in, vote. Also covers reject with a reason.
- The earlier events tests still pass (44/44 and 30/30).

Run it yourself on a **test** database (never the real one):
```
$env:REGISTER_LIMIT = "1000"; .\run-local.ps1 -Port 5001 -Database botoko_test     # window 1
py seed.py --demo-events --out accounts-test-events.csv                          # (once, with DATABASE_URL set to botoko_test)
py regtest.py --url http://localhost:5001 --events 2,3 --students 50              # window 2
```

## What's new: voting events (November 13, 2026)

BotoKo now runs **several elections**, and more than one can be open at the same time.

- **Events:** each event has a name, a description (what the voting is for), a school/organization, a start and an end. Its status is computed:
  - **Upcoming:** before the start.
  - **Current:** between the start and the end.
  - **Previous:** after the end, or closed by the admin.
  - The admin can still open or close an event by hand, or set it back to "follow the schedule".
- **First page: "Choose a voting event"**, with Current / Upcoming / Previous tabs:
  - **Current** events go to that event's login.
  - **Upcoming** events show only the name, what it is for, the start and end, and a countdown. No candidates.
  - **Previous** events show public results: when voting started and ended, turnout, all candidates, and the **winners**.
- **Registration per event** (`event_voters`): one account can be registered in several events.
  - A correct login for an event you're not registered in does **not** log you in. It shows: "You are not registered for this voting event yet. Please register with the election committee (COMELEC) first."
  - Logged-in students can switch to their other current events without logging in again. The server re-checks registration each time.
- **One vote per student per event:**
  - `cast_vote()` keeps its transaction, with `SELECT ... FOR UPDATE` on the (event, student) registration row.
  - The database guarantees it with `UNIQUE(event_id, student_id)` on ballots.
- **Positions with several winners** ("Councilor: vote up to 3"):
  - The ballot shows "Choose up to N" and "2 of 3 selected". At the limit, the other cards are disabled.
  - The server checks: at most N, no duplicates, and all candidates from that position and event. A database trigger also rejects more than N votes per position on a ballot.
  - Results mark the top N as winners. A tie at the last seat shows "Tie for last seat".
- **Results:**
  - An **overview** shows one line per position with a split bar and the leaders or winners, all on one screen.
  - The existing detailed graphs are under **View more**.
  - In the header, a red, pulsing **Live results** button appears for current events.
- **Navigation:** Back, Home (the event list), breadcrumbs ("Events › SSC Election 2026 › Ballot"), and confirmations for submitting a vote, logging out during voting, and admin deletes.
- **Admin:**
  - an **event switcher**, and New event / edit / delete
  - dates, plus open / close / follow schedule
  - positions with "number to elect"
  - **register / unregister students per event**, and the class-list upload can register into the selected event
- **Safe upgrade:** existing positions, candidates, voters and ballots move into **one default event**, made from the old election settings. No table or data is dropped.
- **Demo data:** `python seed.py --demo-events --out accounts-demo.csv` creates:
  - 1 previous event, with votes and winners
  - 2 current events, one with "Councilor: up to 3"
  - 1 upcoming event
  - 80 students: 1–20 in event A only, 21–60 in both, 61–80 in B only

  `loadtest.py` reads each student's events from that file.

## RPC: client-server model with JSON-RPC 2.0

BotoKo is a **client-server** system. The **server** (`app.py` + `rpc.py`, in every worker process) offers **remote procedures**; **clients** call them over the network with **JSON-RPC 2.0**:

```
 CLIENTS                                                    SERVER (4 worker processes x 8 threads)
 browser   static/rpc.js    rpc("cast_vote", {...})  ─┐                ┌─ rpc.py dispatcher: JSON-RPC format,
 terminal  rpc_client.py    client.call("get_results") ┼── POST /rpc ──►│  CSRF token, login/admin check, params
 loadtest  loadtest.py      50 voters at once         ─┘   (HTTP+JSON)  └─ procedure = a normal Python function,
                                         ◄── {"result": ...} or {"error": ...} + "meta" (worker PID, thread)
```

- **One endpoint:** `POST /rpc`. `GET /rpc` lists all procedures and gives a CSRF token, for programs that aren't web pages.
- **Registry:** every procedure is a normal Python function registered with `@rpc_method("name", role=...)`. Its parameters are checked against the function signature.
- **Same code as the pages:** `cast_vote` calls the existing `cast_vote()` (one transaction + `SELECT ... FOR UPDATE` on the student row, unchanged), and `get_results` uses the same 1.5 s results cache.
- **Security:**
  - authentication is the normal login session cookie (student or admin)
  - every call must send the session's CSRF token in the `X-CSRF-Token` header
  - admin procedures refuse non-admins
  - all params are validated
- **Proof of parallel processing:** each reply carries `"meta": {"worker_pid", "thread_name", "duration_ms"}`, and the server terminal logs every call, e.g.
  `RPC cast_vote by V-V9VMDM -> ok (pid 8900, waitress-7, 141 ms)`.
- **Where the UI uses it:**
  - the ballot's **Confirm vote** calls `rpc("cast_vote")`, then opens the receipt
  - **live results** refresh with `rpc("get_results")` every 2 s
  - the admin **Open/Close voting** buttons call `rpc("open_voting")` / `rpc("close_voting")`
  - without JavaScript, the old HTML forms still work (`/vote`, `/admin/toggle`)

### Remote procedures

`event_id` = which voting event (see `get_events`). It may be left out when the session already has one event (the student's current event, or the admin's selected event).

| Procedure | Params | Returns | Who can call it |
|---|---|---|---|
| `get_events` | none | every event: id, name, description, status (`current` / `upcoming` / `previous`), start and end. Upcoming events show only what the voting is for and when | anyone |
| `get_my_events` | none | the events the student is registered in, with voted / not voted per event | logged-in student |
| `get_ballot` | `event_id` | election info, `has_voted`, positions with `seats` (how many to choose) and candidates (photo, party, course & year, motto, background) | student **registered in that event** |
| `cast_vote` | `event_id`, `choices`: `{"<position_id>": <candidate_id>, [ids…] for "choose up to N", or null to skip}` | `receipt_code`, `positions_voted`, `candidates_chosen`, `receipt_url`, `verify_url`, `recorded_by` (worker PID + thread) | student registered in that event |
| `get_receipt` | `receipt_code` | the student's **own** receipt (any event): ballot no., time, masked Voter ID, each position's choice(s) or NO VOTE, totals | logged-in student |
| `verify_receipt` | `verification_code` | `recorded` (true/false), `cast_at` and the event name, never the votes or the voter | anyone |
| `get_results` | `event_id` | live tally: votes per candidate, `lead` = `win` (top N / winner) or `tie` (tie for the last seat), no-vote counts. **No worker PIDs / threads** (also not in `meta` for non-admins). Not for upcoming events | anyone |
| `get_worker_stats` | `event_id` | `processed_by`: ballots per worker process (PID) and number of threads (the "Parallel processing" card) | admin |
| `open_voting` | `event_id` | `{"is_open": true, "status": "current", "changed": …}` | admin |
| `close_voting` | `event_id` | `{"is_open": false, "status": "previous", "changed": …}` | admin |
| `get_turnout` | `event_id` | registered, voted, not yet voted, turnout %, turnout per course / department / class | admin |
| `create_account` | `school_student_no`, `full_name`, `course`, `department`, `year`, `section`, `password` (optional `confirm_password`, `email`) | `voter_id` (generated at once), `status` = `pending`, `message`. One account per student number (-32018 "already exists") | anyone (rate limit `REGISTER_LIMIT`) |
| `get_account_status` | `school_student_no`, `password` | `voter_id`, `name`, `status` = pending / verified / rejected (+ `reason`), `registrations` per event | anyone with the right password |
| `register_for_event` | `event_id` | `status` = `approved` (auto-approved) or `pending`, `already`, `auto_approved` | logged-in student with a **verified** account |
| `get_accounts` | optional `status` | student accounts + counts (pending / verified / rejected) | admin |
| `verify_account` | `student_id` | `verified` | admin |
| `reject_account` | `student_id`, `reason` | `rejected` (pending accounts only) | admin |
| `verify_all_pending_accounts` | none | how many were verified, new counts | admin |
| `get_registrations` | `event_id`, optional `status` | the event's registration requests + counts (pending / approved / rejected) | admin |
| `approve_registration` | `registration_id`, `event_id` | `approved`, `voter_id`. A new student gets an account with a generated Voter ID and is added to the event's voters | admin |
| `reject_registration` | `registration_id`, `reason`, `event_id` | `rejected` (only pending requests). The student sees the reason | admin |
| `approve_all_pending` | `event_id` | how many were approved, and the new counts | admin |

### Error codes

| Code | Meaning |
|---|---|
| -32700 / -32600 / -32601 / -32602 / -32603 | JSON-RPC standard: parse error / invalid request / method not found / invalid params / internal error |
| -32001 | not logged in (or session ended) |
| -32002 | admin only |
| -32003 | missing or wrong `X-CSRF-Token` |
| -32010 | voting is closed |
| -32011 | already voted (in this event) |
| -32012 | invalid vote: unknown candidate, a candidate of another position or event, the same candidate twice, or more than `seats` candidates |
| -32013 | receipt not found (not yours) |
| -32014 | must change password first |
| -32015 | not registered for this voting event |
| -32016 | the event has not started yet (no ballot or results) |
| -32017 | no voting event with that id |
| -32018 | registration refused: invalid details (`data.field` says which), wrong existing password, or the event has ended |
| -32019 | too many attempts from this IP address (create account / Voter ID lookup rate limit) |
| -32020 | the student's account is not verified yet (or was rejected): no ballot, no voting, no event registration |
| -32021 | voting ended, but COMELEC hasn't published the official results yet (`get_results`; the admin still gets them) |

### Example

```
POST /rpc
Content-Type: application/json
X-CSRF-Token: 3f9c...   (from the page, or from GET /rpc)

{"jsonrpc": "2.0", "method": "cast_vote", "id": 7,
 "params": {"event_id": 3, "choices": {"7": 15, "8": null, "9": [17, 18, 21]}}}
```
```json
{"jsonrpc": "2.0", "id": 7,
 "result": {"event_id": 3, "receipt_code": "3AC53634", "positions_voted": 2, "candidates_chosen": 4,
            "receipt_url": "/e/3/receipt", "verify_url": "http://localhost:5000/verify?code=3AC53634",
            "recorded_by": {"worker_pid": 8900, "thread_name": "waitress-1"}},
 "meta": {"worker_pid": 8900, "thread_name": "waitress-1", "duration_ms": 23.2}}
```
Voting a second time in the same event gives an error instead of a result (voting in another event is fine):
```json
{"jsonrpc": "2.0", "id": 8, "error": {"code": -32011, "message": "You have already voted.",
 "data": {"receipt_url": "/e/3/receipt"}}, "meta": {"worker_pid": 6832, "thread_name": "waitress-4", "duration_ms": 9.1}}
```

### Command-line client (a different program calling the server)

```
py rpc_client.py --url http://localhost:5000 list
py rpc_client.py --url http://localhost:5000 get_results
py rpc_client.py --url http://localhost:5000 verify_receipt '{"verification_code": "3AC53634"}'
py rpc_client.py --url http://localhost:5000 get_events
py rpc_client.py --url http://localhost:5000 get_results '{"event_id": 3}'
py rpc_client.py --url http://localhost:5000 --event 3 --voter V-ABC123 --password secret get_ballot
py rpc_client.py --url http://localhost:5000 --event 3 --voter V-ABC123 --password secret cast_vote   # 1st candidate(s) of each position
py rpc_client.py --url http://localhost:5000 --admin-password admin123 get_turnout
py rpc_client.py --url http://localhost:5000 create_account '{"school_student_no": "23-1-00479", "full_name": "Juan Dela Cruz", "course": "BSIT", "department": "College of Computing Studies", "year": 3, "section": "A", "password": "my-own-pass-1"}'
py rpc_client.py --url http://localhost:5000 get_account_status '{"school_student_no": "23-1-00479", "password": "my-own-pass-1"}'
py rpc_client.py --url http://localhost:5000 --admin-password admin123 verify_all_pending_accounts
py rpc_client.py --url http://localhost:5000 --voter V-ABC123 --password my-own-pass-1 register_for_event '{"event_id": 2}'
py rpc_client.py --url http://localhost:5000 --admin-password admin123 approve_all_pending '{"event_id": 2}'
```
It prints the JSON-RPC request and response, plus which worker process and thread ran the procedure.

### Tested
- `loadtest.py`: 50 simultaneous voters + 5 double voters, all through RPC `cast_vote`. OVERALL PASS:
  - 0 errors and 50/50 voted
  - exactly 1 vote per double voter, and totals and skips correct
  - `cast_vote` ran on **4 different worker PIDs**
- Same student, two sessions, `cast_vote` at the same instant (3 times): handled by **two different worker processes**. Each time one got the receipt and the other got `-32011 already voted` (row lock across processes).
- Protocol tests (34/34): every procedure, every error code, CSRF, roles, batch and notification calls.
- Browser tests (10/10): the ballot, live results and admin buttons go through RPC with no JavaScript errors, and the no-JavaScript form fallback works.

## What's new: 1 worker process per CPU core (November 12, 2026)

BotoKo now uses **all the CPU cores** of the machine it runs on instead of a fixed 2 workers.
The voting logic, locking, CSRF and UI are unchanged (only the admin panel below was removed).

- **Auto-detect** (`concurrency.py`): workers = number of **physical** CPU cores (`psutil.cpu_count(logical=False)`, fallback `os.cpu_count()`, minimum 1), **8 threads** each. A laptop with 4 cores / 8 logical runs **4 worker processes x 8 threads = 32 requests at the same time**.
  - `WEB_CONCURRENCY=6` overrides the number.
  - `MAX_WORKERS` (default **4**) caps the auto-detected number. On Render, the container can report the host's cores (e.g. 16) but the free plan only has 512 MB RAM, and every worker is a full copy of the app (~60 MB).
  - Database connections: 8 per worker, but never more than `DB_MAX_CONNECTIONS` (default 80) / workers, so workers x pool stays under PostgreSQL's limit (100).
- **Startup line**, e.g. `BotoKo: 4 CPU cores detected -> 4 worker processes x 8 threads = 32 requests at the same time`.
- **Render:** `gunicorn.conf.py` uses the auto-detected number. `WEB_CONCURRENCY` was removed from `render.yaml`, and `MAX_WORKERS: "4"` was added.
- **Windows laptop:** Gunicorn doesn't run on Windows and `py app.py` is 1 process, so `run-local.ps1` now starts **`serve.py`**. It opens one listening socket and shares it with N worker processes (`socket.share()`), and each worker runs **Waitress** (a production WSGI server for Windows) with 8 threads. It's the same model as Gunicorn. A worker that crashes is restarted automatically. `run-local.ps1 -Simple` (or `py app.py`) still starts the old 1-process server.
- **Panel hidden, monitor kept:** the *System Resources / Parallel Processing* panel is no longer shown anywhere in the interface. Everything behind it still runs in the background:
  - request/thread counting and the worker heartbeat
  - the `worker_stats` table
  - the admin-only JSON at **`/admin/api/system`** (log in as admin, then open the URL directly, e.g. during the defense)

  `worker_pid` and `thread_name` are still saved on every ballot.
- **Verified** with `run-local.ps1` + `loadtest.py --users 50 --double 5`:
  - OVERALL PASS: 0 errors, 50/50 voted, exactly 1 vote per double-voter, tallies correct
  - ballots handled by **4 different worker PIDs** (11 / 13 / 13 / 18 ballots), each using all 8 threads

```sql
SELECT worker_pid, COUNT(*) FROM ballots GROUP BY worker_pid;   -- shows the worker processes that recorded votes
```

## What's new (adviser change requests, November 2026)

The vote transaction (`cast_vote()` with `SELECT ... FOR UPDATE`), the `UNIQUE` constraints, CSRF on every form, and the Gunicorn config (2 workers × 8 threads) plus the DB pool are **unchanged**. The one exception is the rule "every position needs a candidate", which was removed because voting is now optional. Every database change is an upgrade-safe migration (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`), so an existing database on Render keeps its data.

1. **Candidate cards.** Each card shows a large photo (or initials), name, party, course & year, motto/platform and background list, with "View full profile". Tapping a card selects it with a clear highlight. Admins edit these fields in **Admin → Ballot**. Live results show each candidate's photo next to the vote count.
2. **System Resources / Parallel Processing panel** (Admin → Overview, refreshes every 2 s). *Hidden from the interface on November 12, 2026; the data is still at `/admin/api/system` (see above).* It showed:
   - server CPU cores (physical/logical), CPU %, and RAM
   - workers × threads per worker
   - busy threads now and the peak
   - requests per worker, and each worker's PID, CPU and memory
   - DB connections in use vs max
   - a vote-distribution chart by worker PID and thread
   - a "This device" box (the visitor's own CPU cores, memory and browser)

   Each Gunicorn worker writes a heartbeat row to the new `worker_stats` table every 2 s, so the panel sees **all** workers, not only the one answering. Data comes from `psutil`. The endpoint is `/admin/api/system` (admin only).
3. **Optional voting.** Students may skip any position ("No vote for this position"), or tap a chosen candidate again to unselect. They can even submit a blank ballot, after a confirmation. The review screen marks skipped positions and warns about them. The student is still marked as voted. Results show "N no vote" per position. `loadtest.py --skip-rate 0.3` (the default) makes test voters skip random positions, and it checks that the skip counts add up.
4. **Official voting receipt** (`/receipt`). It is narrow and printable, and shows:
   - school name, election title and school year
   - receipt/ballot number, verification code and date/time (Asia/Manila)
   - the masked Voter ID (`V-XXX` + last 3 characters)
   - each position's choice or **NO VOTE**, with voted/skipped totals
   - a QR code (`segno`)

   It has **Print** and **Save as PDF** buttons, and only the receipt is printed. Anyone can check a code at **`/verify`**, which shows only *that* a ballot was recorded and *when*, never the choices. Students can reopen the receipt from **Menu → Vote History**. The school name and year are set in **Admin → Settings → Election details**.
5. **UI polish.** Every page was checked at 360, 768 and 1366 px.
   - Admin Overview has summary cards (**Registered voters, Voted, Not yet voted, Turnout %**). Integrity checks and Latest ballots sit side by side.
   - Tables no longer break mid-word.
   - Phone layout for the Voter Profile is more compact.
   - The footer is cleaner, with Live results and Verify links.

New packages: `psutil` and `segno` (see `requirements.txt`). Run `pip install -r requirements.txt` again after updating.

## What's new (UI/UX redesign)

The voting logic, locking (`cast_vote()` with `SELECT ... FOR UPDATE`) and the database schema are **unchanged**. Only the interface was redesigned.

- **Design system:** colors, spacing, type and radius are CSS variables at the top of `static/style.css`. The colors come from the Philippine flag. There's a new BotoKo logo (inline SVG) and favicon. **Dark mode** follows the phone's setting. Text meets WCAG AA contrast.
- **Login:** shows if voting is open or closed, has a Show/Hide password button, clearer error messages, and keeps the student number after a wrong password.
- **Ballot:** big tap targets with initials avatars, a progress bar ("2 of 4 positions selected"), and a sticky bottom bar. The **Review vote** button stays disabled until every position is chosen, then opens a **"Review your vote"** screen (Back / Confirm) instead of the browser's `confirm()` popup. After Confirm, the button locks and a "Recording your vote..." screen appears, so double taps can't send twice.
- **Receipt:** success animation, large receipt code with a **Copy code** button, the chosen candidates, and the worker PID + thread that processed the vote (proof of parallel processing).
- **Friendly pages:** voting closed and no candidates yet (these re-check every 15 s), already voted (banner on the receipt), session expired, server busy, and page not found.
- **Live results (`/results`):** animated bars, a "Leading"/"Tied" badge, turnout meter, and a **LIVE** indicator with auto-refresh every 2 s (pauses when the tab is hidden, retries if the server is unreachable). **Projector mode** (button, `P` key, or `/results?projector=1`) makes the text bigger, hides navigation and goes fullscreen; press `Esc` to exit. The Parallel processing panel shows each worker PID with its ballots, threads and share.
- **Admin:** tabs for **Overview** (stats, integrity checks, parallel processing, latest ballots; auto-refresh every 3 s), **Ballot** (add, rename, reorder and delete positions; add, edit and remove candidates; every delete asks first), **Students** (search + Voted/Not yet filter, account creation that downloads the CSV), **Results**, and **Settings** (title, open/close voting, and a **Danger zone** where the Reset button only works after typing RESET, then asks again). Messages appear as toasts.
- **Mobile and accessibility:** no horizontal scrolling at 360 px, keyboard focus styles, labels on every field, skip link, arrow keys switch admin tabs.
- **Navigation:** a **Home** button on every page that knows who you are (not logged in → login, haven't voted → ballot, already voted → receipt, admin → Overview), and **Back** buttons on results, admin login, and error pages. Back returns to the previous BotoKo page, or Home if there isn't one, so it never leads to a blank page. On the review screen, the phone's Back button just closes the review and keeps your choices. The ballot page is never cached, so pressing Back from the receipt can't show a usable ballot again. On phones, admin tabs have a **Back to Overview** button.
- **Vote charts (`/results` and Admin → Results):** for each position, a **bar chart** sorted by votes (with "★ Leading" / "Tied" labels) and a **donut** with a legend. Each candidate keeps the same color in both charts (colors checked for color-blind safety in light and dark mode), and every bar shows the name, votes and percent, so nothing depends on color alone. Built with inline SVG + vanilla JS (`static/results.js`), no chart library. Charts animate smoothly between updates, show "No votes yet" for empty positions, and resize for phones and projector mode.
- **Candidate photos:** in Admin → **Ballot**, every candidate can have a photo (**Choose photo / Change photo / Remove photo**, under Edit or when adding a candidate).
  - The **browser** crops the photo to a square and shrinks it to 400×400 JPEG (~30-60 KB) before uploading, with a preview first. Big phone photos (5-10 MB) are fine.
  - The **server** accepts only real JPG/PNG/WebP files (it checks the file's first bytes, not its name), max 300 KB. Like other ballot edits, photos can only change while no votes have been cast.
  - Photos are stored **in PostgreSQL** (table `candidate_photos`), not on disk, because Render's disk is wiped on restart. The table is created automatically on start, so existing databases upgrade safely.
  - Served at `/photo/<id>?v=<version>` with a 1-year cache, so each phone downloads each photo only once. A changed photo gets a new URL.
  - Shown on the ballot, review screen, receipt, live results (bars + legend), and admin. Candidates without a photo show their initials as before.
- **Voter IDs and student details:** every student now has two different numbers.
  - **Voter ID** (e.g. `V-7KQ3M9`) is the login. BotoKo generates it: random, unique, with no look-alike characters (no 0/O, 1/I/L). Students can type it in any case, with or without the dash. If someone types their school number instead, they're told to use the Voter ID.
  - **School student number** (e.g. `23-1-00479`) is information only, unique per student. The format is one constant in `students.py` (`SCHOOL_NO_PATTERN`).
  - Each student also has a course, school/department, year (1–5) and section. Ballot and receipt show e.g. "Juan Dela Cruz · BSIT 3-A".
  - Existing databases upgrade safely (new columns + unique index in `schema.sql`). Old accounts keep working; the admin fills in their details later.
- **Admin → Students:**
  - Create accounts from a pasted class list or an uploaded `.csv`: `school_student_no, full_name, course, department, year, section` (header optional). Every row is checked first; if any row is wrong, **nothing** is created and each problem is listed by line, with the list pre-filled to fix. Students who already have an account are skipped.
  - The passwords are shown **once**: download the CSV (`voter_id, password, school_student_no, full_name, course, department, year, section`) or **Print voter slips** (cards with cut lines).
  - The student list has search (name, Voter ID, school number) and filters (course, department, year, section, voted). It becomes cards on phones.
  - **Edit** a student's details, or **Reset password** (new password shown once). The Voter ID and voting status can't be edited.
  - **Turnout by group** (course, department, year-section): counts only, never anyone's choices.
- **Student account menu (☰):** on every student page after login. It shows the name and class, plus:
  - **Voter Profile:** read-only details.
  - **Vote History:** status, time, receipt code and your own choices.
  - **Change Password:**
    - Needs your current password. The new one must be 8+ characters, different from the current one, and not your Voter ID or school number. It shows a strength hint.
    - Max 5 wrong current-password tries per 15 minutes, counted in the database.
    - Changing it keeps you logged in on this device and **logs out your other devices**. The admin Reset password does the same.
    - It never affects your vote.
  - **Log out.**
  - The menu is keyboard-friendly (Esc, focus kept inside). Opening it on the ballot keeps your choices, even if you visit another page and come back.
- **Optional (Settings, OFF by default):** "Require students to change password on first login": students who haven't voted must replace the slip password before seeing the ballot. Keep it OFF for `loadtest.py`.
- **Still light:** plain HTML/CSS/vanilla JS, no frameworks, no CDN, system fonts. CSS/JS are cached by the phone (`?v=` versioned URLs), so later pages load faster on slow data.

---

## Paano na-apply ang Distributed at Parallel Computing

| Concept | Saan makikita sa BotoKo |
|---|---|
| **Distributed** | 50+ estudyante, iba-iba ang device (phone/laptop), lahat konektado sa iisang online server at database. Ang admin page at live results screen ay hiwalay ding clients. |
| **Client-server + RPC** | Ang server ay nag-aalok ng *remote procedures* (`get_ballot`, `cast_vote`, `get_results`, `open_voting`, ...) sa iisang endpoint, `POST /rpc`, gamit ang **JSON-RPC 2.0** (`rpc.py`). Ang mga client (browser: `static/rpc.js`; terminal: `rpc_client.py`; `loadtest.py`) ay tumatawag sa mga ito sa network. Bawat sagot ay may `meta` na nagsasabi kung aling worker PID at thread ang nagpatakbo ng procedure. |
| **Parallel** | **1 worker process bawat CPU core** (auto-detect, `concurrency.py`) × **8 threads**: sa laptop na may 4 cores, 4 × 8 = 32 requests na sabay-sabay pinoproseso. Sa Render: `gunicorn.conf.py`. Sa Windows: `serve.py` (Waitress). Makikita sa receipt at admin page kung aling worker (PID) at thread ang humawak sa bawat boto. |
| **Locking / Concurrency control** | `cast_vote()` sa `app.py`: isang database transaction na may `SELECT ... FOR UPDATE` sa row ng estudyante. Kapag sabay nag-submit ang iisang estudyante sa dalawang device, maghihintay ang pangalawa at ire-reject ("You have already voted"). |
| **Safety net** | `UNIQUE(student_id)` sa `ballots` at `UNIQUE(student_id, position_id)` sa `votes`, kaya kahit may bug, hindi tatanggapin ng database ang dobleng boto. |
| **Walang lost update** | Hindi kami nag-a-update ng iisang counter (na pwedeng mag-agawan ang threads). Bawat boto ay sariling row, at ang tally ay `COUNT(*)`. Kaya imposibleng mawala ang boto. |
| **Scalable** | Lahat ng state ay nasa database, kaya pwedeng dagdagan ang workers o servers nang hindi binabago ang code. |

## Test results (ginawa bago i-release)

Tinest sa PostgreSQL gamit ang ilang worker processes na may threads (parehong model ng gunicorn). Pinakabago: 4 worker processes (1 bawat CPU core) × 8 threads sa Windows laptop:

| Test | Resulta |
|---|---|
| 50 estudyante, sabay-sabay nag-login at sabay-sabay pumindot ng Submit + 5 estudyanteng sinubukang bumoto nang 2 beses nang sabay | **PASS**: 0 errors, 50/50 tanggap, eksaktong 1 boto bawat double-voter, tama ang tally |
| 200 estudyante + 20 double-voters nang sabay | **PASS**: 0 errors, tama lahat. Pinakamatagal na vote: 0.67s |
| Maling candidate, 2 candidates sa iisang position, pekeng form (CSRF), sarado ang botohan | Tama ang pag-reject sa lahat |
| Nov 2026 (after adviser changes): 50 estudyante + 5 double-voters, random na nag-skip ng positions | **PASS**: 0 errors, 50/50, 1 boto bawat double-voter, tama ang tally at ang bilang ng skips |
| Blank ballot / skipped positions | Tinatanggap; naka-mark pa rin na "voted" ang estudyante |
| Nov 12, 2026: 4 worker processes (auto-detect) via `run-local.ps1`, 50 estudyante + 5 double-voters | **PASS**: 0 errors, walang double vote, tama ang totals; 4 iba-ibang worker PID ang humawak ng ballots |

---

## Mga file

| File | Para saan |
|---|---|
| `app.py` | Ang buong web app (pages, voting logic, locking, admin, student account menu) |
| `students.py` | Voter IDs, school student number format, class-list import at validation |
| `db.py` | Connection pool sa PostgreSQL (laki: galing sa `concurrency.py`) |
| `rpc.py` | **RPC server**: JSON-RPC 2.0 endpoint `POST /rpc`, procedure registry (`@rpc_method`), all remote procedures |
| `static/rpc.js` | **RPC client stub** for the browser: `rpc(method, params)` |
| `rpc_client.py` | **RPC command-line client**: a separate Python program that calls the server's procedures |
| `concurrency.py` | Ilang worker processes at threads: 1 process bawat physical CPU core (may `MAX_WORKERS` cap at `WEB_CONCURRENCY` override) |
| `serve.py` | Windows multi-process server: isang socket, N worker processes, Waitress na may 8 threads bawat isa (sinisimulan ng `run-local.ps1`) |
| `schema.sql` | Database tables (auto-create pag-start ng app) |
| `templates/` | Pages (HTML). `base.html` = layout, `_macros.html` = logo/icon/avatar/CSRF helpers, `_icons.html` = SVG icons, `_results.html` = live results board |
| `static/style.css` | Design system + lahat ng styles (may table of contents sa taas) |
| `static/app.js` | Shared JS: toasts, confirm dialog, show/hide password, anti-double-tap, copy code |
| `static/ballot.js` | Candidate selection/unselect, skip ("No vote"), full-profile dialog, "Review your vote" screen, safe submit |
| `static/results.js` | Live results (every 2 s): SVG bar + donut charts, projector mode |
| `static/admin.js` | Admin tabs, overview auto-refresh, student search, reset safety, photo crop/resize before upload |
| `gunicorn.conf.py` | Production server sa Render: workers (auto-detect) × threads (ang PARALLEL part) |
| `render.yaml` | One-click deploy sa Render |
| `seed.py` | Optional: sample candidates + accounts mula sa command line |
| `loadtest.py` | **50-user simultaneous test** (standard Python lang, walang i-install). Bumoboto sa RPC `cast_vote` (default; `--mode form` = lumang HTML form). `--skip-rate` = gaano kadalas mag-skip; `--expect-workers 4` = i-check na 4 worker PIDs ang humawak ng ballots |

---

## I-deploy online (Render)

1. **Gumawa ng GitHub repo** at i-upload ang lahat ng file sa folder na ito.
2. Gumawa ng account sa **render.com** (pwedeng mag-sign in gamit ang GitHub).
3. Sa Render dashboard: **New → Blueprint** → piliin ang repo ninyo. Mababasa nito ang `render.yaml` at gagawa ng web app + database.
4. Hihingin nito ang **ADMIN_PASSWORD**. Mag-type ng password para sa COMELEC/admin.
5. Hintayin matapos ang deploy (ilang minuto). Makakakuha kayo ng link tulad ng `https://botoko-xxxx.onrender.com`.
6. Buksan ang `https://...onrender.com/admin`, mag-login (Voter ID: `admin` + ang ADMIN_PASSWORD), tapos:
   - **Ballot tab:** i-delete, i-rename, i-reorder o idagdag ang positions at candidates ninyo. Pwede ring maglagay ng **photo** ng bawat candidate (Edit → Choose photo → Save). Gawin ito **bago** magbukas ng botohan; naka-lock na ang ballot kapag may bumoto na.
   - **Students tab:** i-paste ang class list o mag-upload ng `.csv` (`school_student_no, full_name, course, department, year, section` bawat linya) → **Check & create accounts**. Gagawa ang BotoKo ng **Voter ID** at password para sa bawat estudyante. I-download ang CSV at/o i-print ang **voter slips**. Isang beses lang ito makikita.
   - **Open voting** (button sa taas) kapag handa na.

### Mahalaga: Free vs $7 plan
- **Free:** natutulog ang server pag 15 minutes walang gumagamit, at mga 1 minuto bago magising. Mahina rin ang CPU (0.1). Kaya pa rin ang 50 users, pero kung literal na sabay-sabay ang lahat, aabot ng ilang segundo ang hintay ng iba.
- **Starter ($7/buwan, ~₱400):** hindi natutulog at 5× mas malakas. **Recommended para sa defense.** Sa Render: web service → Settings → Instance Type → Starter. Pwedeng i-downgrade pagkatapos.
- **Free database** ay nag-e-expire 30 days matapos gawin. Kaya gawin ang deploy mga 1–3 linggo bago ang defense, o i-upgrade ang database.

---

## Patakbuhin ang 50-user test sa deployed site

Sa laptop (kailangan lang ng Python 3):

1. Sa Admin → **Students → Test accounts**, gumawa ng **60 test accounts** → sa susunod na page, **Download CSV** at i-save bilang `accounts.csv`. (Ang `loadtest.py` ay nagbabasa ng `voter_id` column; gumagana pa rin ang lumang `student_no` files.) Siguraduhing **OFF** ang "Require students to change password on first login" sa Settings.
2. Siguraduhing **Open** ang voting.
3. Patakbuhin:
   ```
   python loadtest.py https://botoko-xxxx.onrender.com accounts.csv --users 50 --double 5
   ```
4. Dapat **OVERALL: PASS**. Pwede ring subukan ang `--users 100`.
5. Sa Admin → **Settings → Danger zone → Reset votes** (type RESET) para malinis bago ang totoong demo.

---

## Demo day checklist

- [ ] Starter plan naka-on (o buksan ang site 3 minuto bago magsimula para magising)
- [ ] Na-reset ang votes mula sa rehearsal
- [ ] Naka-print at naibigay na ang **voter slip** (Voter ID + password) ng bawat kaklase
- [ ] Tamang candidates sa ballot, at **Open** ang voting
- [ ] Naka-project ang **/results** page sa harap, naka-**Projector mode** (pindutin ang `P`, o buksan ang `/results?projector=1`)
- [ ] May backup internet (mobile hotspot)
- [ ] Pagkatapos bumoto ng lahat: ipakita ang **Admin → Overview → Integrity checks** (lahat ✓) at **Latest ballots** (iba-ibang worker PID at threads); o i-run ang `SELECT worker_pid, COUNT(*) FROM ballots GROUP BY worker_pid`

### Ipakita ang lock sa panel
Mag-login sa parehong account sa **dalawang phone**, piliin ang candidates sa pareho, at sabay pindutin ang Submit. Isa lang ang tatanggapin; ang isa ay "You have already voted."

---

## Patakbuhin sa sariling laptop (para sa development)

**Pinakamadali (kung naka-setup na ang private database sa `C:\Users\<ikaw>\botoko-db`):**
```
powershell -ExecutionPolicy Bypass -File .\run-local.ps1
```
Sinisimulan nito ang database (kung hindi pa tumatakbo, hal. pagkatapos mag-restart), sine-set ang settings, at pinapatakbo ang BotoKo gamit ang **1 worker process bawat CPU core** (`serve.py` + Waitress; awtomatikong ini-install ang Waitress kung wala pa). Makikita sa unang linya, hal.:
```
BotoKo: 4 CPU cores detected -> 4 worker processes x 8 threads = 32 requests at the same time
```
Gumagamit ito ng malinis na database na **`botoko_live`** (walang sample data). Mga option: `-Database botoko` (bumalik sa lumang database), `-Simple` (lumang paraan: 1 process, gaya ng `py app.py`), `-Port 5001`, `-DatabaseUrl "postgresql://..."` (ibang database, hal. para sa test).

**PowerShell, mano-mano** (default sa VS Code terminal sa Windows):
```
pip install -r requirements.txt
$env:DATABASE_URL = "postgresql://postgres:YOUR_PASSWORD@localhost:5432/botoko"
$env:ADMIN_PASSWORD = "admin123"
$env:COOKIE_SECURE = "0"
py seed.py --students 60 --open        # gagawa ng accounts.csv na may voter_id; hindi nito papalitan ang lumang accounts file
py app.py
```
**Command Prompt (cmd.exe):** pareho, pero `set DATABASE_URL=postgresql://...` (walang quotes, walang `$env:`).
**Mac/Linux:** `export DATABASE_URL=postgresql://...`

> Sa PowerShell, **hindi** gumagana ang `set DATABASE_URL=...` (iba ang ibig sabihin ng `set` doon). Gamitin ang `$env:...`.
> Nawawala ang mga ito kapag isinara ang terminal, kaya i-set ulit tuwing magbubukas ng bagong terminal.

Buksan ang http://localhost:5000. Pwedeng gamitin ang **External Database URL** ng Render database bilang `DATABASE_URL`.
