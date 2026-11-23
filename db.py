"""Database helpers for BotoKo.

Every server worker process gets its own connection pool. Each request
borrows a connection for a few milliseconds and gives it back, so a small
pool (8 connections per worker) easily serves 50+ students at once.
The pool size comes from concurrency.plan(): one connection per thread, but
workers x pool always stays below the database's connection limit.
"""
import os
import threading
from pathlib import Path

from psycopg_pool import ConnectionPool

import concurrency

_pool = None
_pool_lock = threading.Lock()
_pool_pid = None


def get_pool():
    """Create the pool lazily, once per worker process (safe with gunicorn forking)."""
    global _pool, _pool_pid
    pid = os.getpid()
    if _pool is None or _pool_pid != pid:
        with _pool_lock:
            if _pool is None or _pool_pid != pid:
                url = os.environ.get("DATABASE_URL")
                if not url:
                    raise RuntimeError("DATABASE_URL is not set. See README.md.")
                _pool = ConnectionPool(
                    url,
                    min_size=1,
                    max_size=concurrency.plan()["pool"],   # 8 per worker (see concurrency.py)
                    timeout=30,              # wait up to 30s for a free connection
                    max_idle=300,
                    check=ConnectionPool.check_connection,  # drop dead connections
                    kwargs={"autocommit": True},
                    open=True,
                )
                _pool_pid = pid
    return _pool


def query_all(sql, params=None):
    with get_pool().connection() as conn:
        return conn.execute(sql, params).fetchall()


def query_one(sql, params=None):
    with get_pool().connection() as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql, params=None):
    with get_pool().connection() as conn:
        conn.execute(sql, params)


def init_schema():
    """Create tables if they don't exist yet. Runs on every app start."""
    sql = (Path(__file__).parent / "schema.sql").read_text()
    statements = [s.strip() for s in sql.split(";")]
    with get_pool().connection() as conn:
        # Advisory lock so two workers starting at once don't race on CREATE TABLE.
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(424242)")
            for stmt in statements:
                body = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
                if body:
                    conn.execute(body)
            migrate_events(conn)
            migrate_accounts(conn)
            migrate_results(conn)


# Database-level safety net for multi-seat positions: a ballot may have at most `seats` votes per
# position, and only for positions of the ballot's own event. (Replaces the old
# UNIQUE(student_id, position_id), which allowed only 1 vote per position.)
VOTE_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION botoko_vote_guard() RETURNS trigger AS $$
DECLARE
    pos_event INTEGER;
    pos_seats INTEGER;
    ballot_event INTEGER;
    already INTEGER;
BEGIN
    SELECT event_id, seats INTO pos_event, pos_seats FROM positions WHERE id = NEW.position_id;
    SELECT event_id INTO ballot_event FROM ballots WHERE id = NEW.ballot_id;
    IF pos_event IS DISTINCT FROM ballot_event THEN
        RAISE EXCEPTION 'vote for a position of another event' USING ERRCODE = 'check_violation';
    END IF;
    SELECT COUNT(*) INTO already FROM votes WHERE ballot_id = NEW.ballot_id AND position_id = NEW.position_id;
    IF already >= pos_seats THEN
        RAISE EXCEPTION 'too many votes for one position' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql
"""


def migrate_events(conn):
    """Upgrade an existing database to voting EVENTS without losing anything (safe to re-run).

    1. Positions/ballots from before events existed go into ONE default event, made from the old
       election settings (title, school, open/closed). All existing students are registered in it,
       with their voted / not-voted status.
    2. One vote per student PER EVENT: UNIQUE(event_id, student_id) on ballots. The new index is
       created BEFORE the old UNIQUE(student_id) is removed, so there is never a moment without one.
    3. Multi-seat positions: UNIQUE(ballot_id, candidate_id) + the vote guard trigger replace
       UNIQUE(student_id, position_id). Position names are unique per event instead of globally.
    """
    legacy_positions, legacy_ballots = conn.execute(
        """SELECT (SELECT COUNT(*) FROM positions WHERE event_id IS NULL),
                  (SELECT COUNT(*) FROM ballots WHERE event_id IS NULL)""").fetchone()
    if legacy_positions or legacy_ballots:
        title, is_open, school_name, school_year = conn.execute(
            "SELECT title, is_open, school_name, school_year FROM election WHERE id = 1").fetchone()
        first_vote, last_vote = conn.execute("SELECT MIN(cast_at), MAX(cast_at) FROM ballots").fetchone()
        manual = "open" if is_open else ("closed" if legacy_ballots else "auto")
        event_id = conn.execute(
            """INSERT INTO events (name, description, organization, school_year, starts_at, ends_at, manual)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (title, "Student council election (created automatically from the earlier single election).",
             school_name, school_year, first_vote, None if is_open else last_vote, manual)).fetchone()[0]
        conn.execute("UPDATE positions SET event_id = %s WHERE event_id IS NULL", (event_id,))
        conn.execute("UPDATE ballots SET event_id = %s WHERE event_id IS NULL", (event_id,))
        conn.execute(
            """INSERT INTO event_voters (event_id, student_id, has_voted, voted_at)
               SELECT %s, id, has_voted, voted_at FROM students
               ON CONFLICT (event_id, student_id) DO NOTHING""", (event_id,))
    # Only touch what isn't done yet: these statements lock the table, and this runs on every start.
    for table in ("positions", "ballots"):
        nullable = conn.execute(
            """SELECT is_nullable = 'YES' FROM information_schema.columns
                WHERE table_name = %s AND column_name = 'event_id'""", (table,)).fetchone()[0]
        if nullable:
            conn.execute(f"ALTER TABLE {table} ALTER COLUMN event_id SET NOT NULL")
    # new rules first ...
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ballots_event_student_key ON ballots (event_id, student_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS votes_ballot_candidate_key ON votes (ballot_id, candidate_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS positions_event_name_key ON positions (event_id, name)")
    if not conn.execute("SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'votes_guard')").fetchone()[0]:
        conn.execute(VOTE_GUARD_FUNCTION)
        conn.execute("CREATE TRIGGER votes_guard BEFORE INSERT ON votes FOR EACH ROW EXECUTE FUNCTION botoko_vote_guard()")
    # ... then remove the old single-election constraints (constraints only, no data is touched)
    for table, constraint in (("ballots", "ballots_student_id_key"),
                              ("votes", "votes_student_id_position_id_key"),
                              ("positions", "positions_name_key")):
        if conn.execute("SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = %s)", (constraint,)).fetchone()[0]:
            conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")


def migrate_accounts(conn):
    """LOGIN FIRST upgrade (safe to re-run, nothing is deleted): online registrations from before
    accounts existed (a request with its own password but no account) become PENDING accounts with a
    Voter ID and that same password, linked to their requests. The student finds the new Voter ID with
    "Forgot your Voter ID?" (student number + their password)."""
    import students as st
    rows = conn.execute(
        """SELECT DISTINCT ON (r.school_student_no) r.school_student_no, r.full_name, r.course, r.department,
                  r.year_level, r.section, r.email, r.password_hash
             FROM registrations r
            WHERE r.student_id IS NULL AND r.password_hash IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM students s WHERE s.school_student_no = r.school_student_no)
            ORDER BY r.school_student_no, r.id""").fetchall()
    for school_no, name, course, dept, year, section, email, pw_hash in rows:
        for _ in range(20):
            got = conn.execute(
                """INSERT INTO students (student_no, full_name, password_hash, password_generated, school_student_no,
                                         course, department, year_level, section, email, account_status)
                   VALUES (%s, %s, %s, FALSE, %s, %s, %s, %s, %s, %s, 'pending')
                   ON CONFLICT (student_no) DO NOTHING RETURNING id""",
                (st.new_voter_id(), name, pw_hash, school_no, course, dept, year, section, email or "")).fetchone()
            if got:
                conn.execute("""UPDATE registrations SET student_id = %s, password_hash = NULL
                                 WHERE school_student_no = %s AND student_id IS NULL""", (got[0], school_no))
                break


def migrate_results(conn):
    """Publish official results (safe to re-run): add results_published. Events that had ALREADY ended
    before this upgrade were public until now, so they start as published (nothing disappears)."""
    exists = conn.execute("""SELECT 1 FROM information_schema.columns
                              WHERE table_name = 'events' AND column_name = 'results_published'""").fetchone()
    if exists:
        return
    conn.execute("ALTER TABLE events ADD COLUMN results_published BOOLEAN NOT NULL DEFAULT FALSE")
    conn.execute("ALTER TABLE events ADD COLUMN results_published_at TIMESTAMPTZ")
    conn.execute("""UPDATE events e SET results_published = TRUE, results_published_at = COALESCE(e.ends_at, now())
                     WHERE e.manual = 'closed' OR (e.manual = 'auto' AND e.ends_at IS NOT NULL AND e.ends_at <= now())""")
