"""Fill the database with a sample ballot and student accounts.

Usage (DATABASE_URL must be set):
    python seed.py --students 60            # sample candidates + 60 accounts
    python seed.py --students 60 --open     # ...and open voting
    python seed.py --demo-events --out accounts-demo.csv   # 4 demo events + 80 registered students

Each account gets a random Voter ID (e.g. V-7KQ3M9, the login) and password, plus sample
student details. They are written to accounts.csv (the database only keeps password hashes),
in the same columns as the Admin download, so loadtest.py can use the file directly.
You can also do all of this from the Admin page instead.
"""
import argparse
import csv
import os
import random
import sys

import db
import students as st
from app import ACCOUNT_CSV_COLUMNS, insert_student

# position -> [(name, party, course & year, motto, background lines)]
SAMPLE_BALLOT = {
    "President": [
        ("Andrea Villanueva", "Partido Bago", "BSIT 4", "Serbisyong tapat para sa bawat estudyante.",
         "Vice President, Student Council 2024-2025\nDean's Lister (4 semesters)\nFounder, Peer Tutoring Circle"),
        ("Miguel Santos", "Kabataan Muna", "BSBA 4", "Boses ng estudyante, aksyon ng konseho.",
         "Class President, BSBA 3-A\nOrganizer, Campus Clean-Up Drive 2025\nMember, Junior Marketing Association")],
    "Vice President": [
        ("Patricia Ramos", "Partido Bago", "BSED 3", "Bukas na pinto, bukas na isip.",
         "Secretary, Education Society\nVolunteer, Brigada Eskwela 2024"),
        ("Joshua Navarro", "Kabataan Muna", "BSCS 3", "Mas mabilis na serbisyo gamit ang teknolohiya.",
         "Lead Developer, school org website\nMember, Google Developer Student Club")],
    "Secretary": [
        ("Bea Mendoza", "Partido Bago", "BSIT 2", "Malinaw na tala, tapat na ulat.",
         "Class Secretary, BSIT 1-B\nEditor, The Campus Chronicle"),
        ("Carlo Reyes", "Kabataan Muna", "BSBA 2", "", "Member, Debate Society"),
        ("Lara Aquino", "Independent", "BSCS 2", "Walang kulay, puro serbisyo.", "")],
    "Treasurer": [
        ("Kevin Lim", "Partido Bago", "BSBA 3", "Bawat piso, may resibo.",
         "Treasurer, Junior Finance Society\nCertified bookkeeping (TESDA NC III)"),
        ("Sofia Garcia", "Kabataan Muna", "BSIT 3", "Transparent na pondo para sa lahat.",
         "Auditor, Student Council 2024-2025")],
}
FIRST = ["Juan", "Maria", "Jose", "Ana", "Mark", "Kristine", "John Paul", "Angelica", "Paolo", "Jasmine",
         "Carlo", "Nicole", "Miguel", "Patricia", "Joshua", "Bea", "Kevin", "Sofia", "Rafael", "Clarisse"]
LAST = ["Dela Cruz", "Santos", "Reyes", "Garcia", "Mendoza", "Bautista", "Villanueva", "Ramos",
        "Aquino", "Navarro", "Castillo", "Flores", "Torres", "Lim", "Ocampo", "Domingo"]
COURSES = [("BSIT", "College of Computing Studies"), ("BSCS", "College of Computing Studies"),
           ("BSED", "College of Education"), ("BSBA", "College of Business Administration")]


CCS_BALLOT = {
    "Governor": [
        ("Rafael Torres", "Sulong CCS", "BSCS 4", "Code para sa bayan.", "President, Programming Guild\nHackathon champion 2025"),
        ("Nicole Ocampo", "Bagong CCS", "BSIT 4", "Lahat kasama, walang maiiwan.", "Vice Governor 2025\nDean's Lister")],
    "Vice Governor": [
        ("Mark Castillo", "Sulong CCS", "BSIT 3", "Mabilis at maayos.", "Class President, BSIT 3-A"),
        ("Jasmine Flores", "Bagong CCS", "BSCS 3", "Boses ng bawat seksyon.", "Secretary, ACM Student Chapter")],
    # 3 seats: each voter may choose up to 3, the top 3 win
    "Councilor": [
        ("Paolo Domingo", "Sulong CCS", "BSIT 2", "Laging handang tumulong.", "Peer tutor"),
        ("Angelica Lim", "Sulong CCS", "BSCS 2", "Tapat at masipag.", "Member, Women in Tech"),
        ("John Paul Reyes", "Bagong CCS", "BSIT 2", "Para sa mas magandang lab.", "Lab assistant"),
        ("Kristine Bautista", "Bagong CCS", "BSCS 1", "Bago pero handa.", ""),
        ("Jose Navarro", "Independent", "BSIT 3", "Walang partido, puro trabaho.", "Varsity player"),
        ("Clarisse Garcia", "Independent", "BSCS 3", "Malinis na konseho.", "Editor, CCS Bulletin")],
}
OLD_BALLOT = {
    "President": [("Liza Soriano", "Partido Bago", "BSA 4", "", ""), ("Ramon Cruz", "Kabataan Muna", "BSBA 4", "", "")],
    "Vice President": [("Ella Tan", "Partido Bago", "BSED 4", "", ""), ("Noel Ramos", "Kabataan Muna", "BSIT 4", "", "")],
    "Board Member": [("Ivy Santos", "Partido Bago", "BSCS 3", "", ""), ("Leo Garcia", "Kabataan Muna", "BSIT 3", "", ""),
                     ("Mia Flores", "Independent", "BSBA 3", "", ""), ("Ben Aquino", "Independent", "BSED 3", "", "")],
}
SEATS = {"Councilor": 3, "Board Member": 2}
EVENTS_CSV_COLUMNS = ACCOUNT_CSV_COLUMNS + ["events", "event_names"]


def add_ballot(conn, event_id, ballot):
    for order, (pos, cands) in enumerate(ballot.items(), 1):
        pid = conn.execute("""INSERT INTO positions (event_id, name, seats, sort_order) VALUES (%s, %s, %s, %s)
                              RETURNING id""", (event_id, pos, SEATS.get(pos, 1), order)).fetchone()[0]
        for name, party, course_year, motto, background in cands:
            conn.execute("""INSERT INTO candidates (position_id, full_name, party, course_year, motto, background)
                            VALUES (%s, %s, %s, %s, %s, %s)""", (pid, name, party, course_year, motto, background))


def new_event(conn, name, description, organization, starts, ends, school_year="2026-2027"):
    """starts / ends: SQL expressions relative to now(), e.g. "now() - interval '1 day'"."""
    return conn.execute(
        f"""INSERT INTO events (name, description, organization, school_year, starts_at, ends_at)
            VALUES (%s, %s, %s, %s, {starts}, {ends}) RETURNING id""",
        (name, description, organization, school_year)).fetchone()[0]


def make_students(conn, count, rng):
    taken = {r[0] for r in conn.execute("SELECT school_student_no FROM students "
                                        "WHERE school_student_no IS NOT NULL").fetchall()}
    rows, n = [], 0
    while len(rows) < count:
        n += 1
        number = f"23-1-{n:05d}"                 # sample school numbers, skipping used ones
        if number in taken:
            continue
        course, dept = rng.choice(COURSES)
        details = {"school_student_no": number, "full_name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                   "course": course, "department": dept, "year": rng.randint(1, 4), "section": rng.choice("ABC")}
        password = st.new_password()
        voter_id = insert_student(conn, details, password)
        sid = conn.execute("SELECT id FROM students WHERE student_no = %s", (voter_id,)).fetchone()[0]
        rows.append({"id": sid, "row": [voter_id, password, number, details["full_name"], course, dept,
                                        details["year"], details["section"]]})
    return rows


def register(conn, event_id, students):
    for s in students:
        conn.execute("""INSERT INTO event_voters (event_id, student_id) VALUES (%s, %s)
                        ON CONFLICT (event_id, student_id) DO NOTHING""", (event_id, s["id"]))


def demo_events(args, rng):
    """4 demo events: one PREVIOUS (with votes and winners), two CURRENT (one with Councilor - vote
    up to 3), one UPCOMING. Students 1-20: only event A, 21-60: both current events, 61-80: only B."""
    import app as core                        # cast_vote(): the real voting code, with its locking
    with db.get_pool().connection() as conn:
        with conn.transaction():
            prev = new_event(conn, "SSC Election 2025",
                             "Election of the Supreme Student Council officers for School Year 2025-2026.",
                             "Supreme Student Council", "now() - interval '30 days'", "now() - interval '28 days'", "2025-2026")
            add_ballot(conn, prev, OLD_BALLOT)
            ev_a = new_event(conn, "SSC Election 2026",
                             "Elect the Supreme Student Council officers for School Year 2026-2027.",
                             "Supreme Student Council", "now() - interval '1 day'", "now() + interval '3 days'")
            add_ballot(conn, ev_a, SAMPLE_BALLOT)
            ev_b = new_event(conn, "CCS Student Council 2026",
                             "Elect the officers of the College of Computing Studies Student Council, including 3 councilors.",
                             "College of Computing Studies", "now() - interval '2 hours'", "now() + interval '2 days'")
            add_ballot(conn, ev_b, CCS_BALLOT)
            upcoming = new_event(conn, "Batch 2027 Class Officers Election",
                                 "Choose the class officers of Batch 2027 (president, secretary and treasurer).",
                                 "Batch 2027", "now() + interval '5 days'", "now() + interval '6 days'")
            add_ballot(conn, upcoming, {"Class President": [("Hidden Candidate", "", "", "", "")]})
            people = make_students(conn, max(args.students, 80), rng)
            only_a, both, only_b = people[:20], people[20:60], people[60:80]
            register(conn, ev_a, only_a + both)
            register(conn, ev_b, both + only_b)
            register(conn, upcoming, people[:30])
            register(conn, prev, people[:60])
            conn.execute("UPDATE events SET manual = 'open' WHERE id = %s", (prev,))   # open it briefly to vote
    # Votes for the previous event through the real cast_vote() (45 of its 60 voters, clear winners).
    weights = {"Liza Soriano": 3, "Ramon Cruz": 2, "Ella Tan": 2, "Noel Ramos": 3,
               "Ivy Santos": 4, "Leo Garcia": 3, "Mia Flores": 2, "Ben Aquino": 1}
    ballot = core.load_ballot(prev)
    for s in people[:45]:
        choices = {}
        for p in ballot:
            names = [c for c in p["candidates"]]
            if p["seats"] > 1:
                pool = names[:]
                picked = []
                for _ in range(rng.randint(1, p["seats"])):
                    c = rng.choices(pool, weights=[weights[x["name"]] for x in pool])[0]
                    pool.remove(c)
                    picked.append(c["id"])
                choices[p["id"]] = picked
            elif rng.random() > 0.1:                 # some skip a position
                choices[p["id"]] = [rng.choices(names, weights=[weights[x["name"]] for x in names])[0]["id"]]
        core.cast_vote(prev, s["id"], choices)
    with db.get_pool().connection() as conn:
        with conn.transaction():
            conn.execute("UPDATE events SET manual = 'auto' WHERE id = %s", (prev,))    # dates: ended 28 days ago
            # The demo's finished election: COMELEC already published its official results.
            conn.execute("""UPDATE events SET results_published = TRUE, results_published_at = ends_at + interval '1 day'
                             WHERE id = %s""", (prev,))
            # The demo ballots get times inside that event's voting window.
            conn.execute("""UPDATE ballots b SET cast_at = e.starts_at + random() * (e.ends_at - e.starts_at)
                              FROM events e WHERE e.id = b.event_id AND b.event_id = %s""", (prev,))
            conn.execute("""UPDATE event_voters v SET voted_at = b.cast_at FROM ballots b
                             WHERE b.event_id = v.event_id AND b.student_id = v.student_id AND v.event_id = %s""", (prev,))
    names = {prev: "SSC Election 2025", ev_a: "SSC Election 2026", ev_b: "CCS Student Council 2026",
             upcoming: "Batch 2027 Class Officers Election"}
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(EVENTS_CSV_COLUMNS)
        for i, s in enumerate(people):
            current = ([ev_a] if i < 60 else []) + ([ev_b] if 20 <= i < 80 else [])
            w.writerow(s["row"] + [";".join(map(str, current)), "; ".join(names[e] for e in current)])
    print(f"Demo events: previous {prev} (45 votes), current {ev_a} and {ev_b} (Councilor: vote up to 3), upcoming {upcoming}")
    print(f"Created {len(people)} students -> {args.out} (the 'events' column = current events each one may vote in)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--students", type=int, default=60)
    ap.add_argument("--out", default="accounts.csv")
    ap.add_argument("--open", action="store_true", help="open voting after seeding")
    ap.add_argument("--demo-events", action="store_true",
                    help="create 4 demo events (previous, 2 current, upcoming) and 80 registered students")
    args = ap.parse_args()

    # Never mix formats: append only to a CSV made by this version, never overwrite one with passwords.
    if os.path.exists(args.out) and os.path.getsize(args.out) > 0:
        with open(args.out, newline="", encoding="utf-8-sig") as f:
            header = next(csv.reader(f), [])
        if args.demo_events or header != ACCOUNT_CSV_COLUMNS:
            sys.exit(f"{args.out} already exists. Nothing was created. Use another file, e.g. --out accounts-new.csv")
        append = True
    else:
        append = False

    db.init_schema()
    rng = random.SystemRandom()
    if args.demo_events:
        demo_events(args, rng)
        db.get_pool().close()
        return
    rows = []
    with db.get_pool().connection() as conn:
        with conn.transaction():
            # The sample ballot goes into an event: the newest one, or a new "Student Council Election".
            row = conn.execute("SELECT id FROM events ORDER BY id DESC LIMIT 1").fetchone()
            event_id = row[0] if row else new_event(
                conn, "Student Council Election", "Elect the student council officers.", "", "NULL", "NULL")
            if conn.execute("SELECT COUNT(*) FROM positions WHERE event_id = %s", (event_id,)).fetchone()[0] == 0:
                add_ballot(conn, event_id, SAMPLE_BALLOT)
                print("Added sample ballot.")
            people = make_students(conn, args.students, rng)
            register(conn, event_id, people)               # the new accounts may vote in this event
            rows = [p["row"] for p in people]
            if args.open:
                conn.execute("UPDATE events SET manual = 'open' WHERE id = %s", (event_id,))

    with open(args.out, "a" if append else "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not append:
            w.writerow(ACCOUNT_CSV_COLUMNS)
        w.writerows(rows)
    print(f"Created {len(rows)} student accounts -> {args.out} (registered in event {event_id})")
    if args.open:
        print("Voting is OPEN.")
    db.get_pool().close()   # close cleanly (avoids a harmless warning at exit on Python 3.14)


if __name__ == "__main__":
    main()
