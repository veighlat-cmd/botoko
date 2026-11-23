-- BotoKo database schema (PostgreSQL)
-- Safe to run many times: every statement uses IF NOT EXISTS.

-- One row only: is the election open or closed?
CREATE TABLE IF NOT EXISTS election (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    title       TEXT    NOT NULL DEFAULT 'Student Council Election',
    is_open     BOOLEAN NOT NULL DEFAULT FALSE
);
INSERT INTO election (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS students (
    id            SERIAL PRIMARY KEY,
    student_no    TEXT    NOT NULL UNIQUE,
    full_name     TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    has_voted     BOOLEAN NOT NULL DEFAULT FALSE,
    voted_at      TIMESTAMPTZ
);

-- Student details (added later: safe to run on a database that already has students
-- and votes, nothing is lost). student_no above is the login = the Voter ID (e.g. V-7KQ3M9).
-- school_student_no is information only (e.g. 23-1-00479) and must be unique. It may be
-- empty only for accounts created before this upgrade (the admin can fill it in later).
ALTER TABLE students ADD COLUMN IF NOT EXISTS school_student_no TEXT;
ALTER TABLE students ADD COLUMN IF NOT EXISTS course     TEXT;
ALTER TABLE students ADD COLUMN IF NOT EXISTS department TEXT;
ALTER TABLE students ADD COLUMN IF NOT EXISTS year_level INTEGER CHECK (year_level BETWEEN 1 AND 5);
ALTER TABLE students ADD COLUMN IF NOT EXISTS section    TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS students_school_student_no_key ON students (school_student_no);

CREATE TABLE IF NOT EXISTS positions (
    id         SERIAL PRIMARY KEY,
    name       TEXT    NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS candidates (
    id          SERIAL PRIMARY KEY,
    position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    full_name   TEXT    NOT NULL,
    party       TEXT    NOT NULL DEFAULT ''
);
-- Candidate card details (added later, safe to re-run). Existing candidates get empty text.
-- NOTE: never put a semicolon in a comment in this file (db.init_schema splits on it).
-- background = achievements / experience / organizations, one item per line.
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS course_year TEXT NOT NULL DEFAULT '';
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS motto       TEXT NOT NULL DEFAULT '';
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS background  TEXT NOT NULL DEFAULT '';

-- One ballot per student. UNIQUE(student_id) is the database-level guarantee
-- against double voting, even if two requests arrive at the same instant.
-- worker_pid / thread_name record WHICH server process and thread handled
-- the vote, so the admin page can show the parallel processing.
CREATE TABLE IF NOT EXISTS ballots (
    id           SERIAL PRIMARY KEY,
    student_id   INTEGER NOT NULL UNIQUE REFERENCES students(id) ON DELETE CASCADE,
    receipt_code TEXT    NOT NULL UNIQUE,
    worker_pid   INTEGER NOT NULL,
    thread_name  TEXT    NOT NULL,
    cast_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per (student, position). The tally is COUNT(*) over this table,
-- so there is no shared counter that two threads could overwrite.
CREATE TABLE IF NOT EXISTS votes (
    id           SERIAL PRIMARY KEY,
    ballot_id    INTEGER NOT NULL REFERENCES ballots(id) ON DELETE CASCADE,
    student_id   INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    position_id  INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    UNIQUE (student_id, position_id)
);

CREATE INDEX IF NOT EXISTS votes_candidate_idx ON votes (candidate_id);

-- Student account security (added later, safe to re-run).
-- session_version: +1 whenever the password changes. Each login remembers the number it
--   started with, so every OTHER device (with the old number) is logged out automatically.
-- password_generated: TRUE while the student still uses the password COMELEC gave them.
ALTER TABLE students ADD COLUMN IF NOT EXISTS session_version    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE students ADD COLUMN IF NOT EXISTS password_generated BOOLEAN NOT NULL DEFAULT TRUE;
-- Shown on the official voting receipt (admin Settings). Empty = left out / filled in automatically.
ALTER TABLE election ADD COLUMN IF NOT EXISTS school_name TEXT NOT NULL DEFAULT '';
ALTER TABLE election ADD COLUMN IF NOT EXISTS school_year TEXT NOT NULL DEFAULT '';
-- Optional rule (OFF by default): make students change the generated password before voting.
ALTER TABLE election ADD COLUMN IF NOT EXISTS require_password_change BOOLEAN NOT NULL DEFAULT FALSE;

-- Wrong "current password" attempts on the Change Password page (max 5 per 15 minutes).
-- Kept in the database, not in Python memory, so every server worker sees the same count.
CREATE TABLE IF NOT EXISTS password_change_failures (
    id         SERIAL PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    failed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS password_change_failures_idx ON password_change_failures (student_id, failed_at);

-- Parallel processing monitor (background only, read at /admin/api/system). Every server worker
-- process writes its own row here every 2 seconds (a "heartbeat"). Worker processes don't
-- share memory, so the database is where the admin page can see all of them at once.
CREATE TABLE IF NOT EXISTS worker_stats (
    hostname         TEXT        NOT NULL,
    pid              INTEGER     NOT NULL,
    threads_busy     INTEGER     NOT NULL DEFAULT 0,
    threads_peak     INTEGER     NOT NULL DEFAULT 0,
    threads_total    INTEGER     NOT NULL DEFAULT 0,
    requests_handled BIGINT      NOT NULL DEFAULT 0,
    cpu_percent      REAL        NOT NULL DEFAULT 0,
    memory_mb        REAL        NOT NULL DEFAULT 0,
    pool_in_use      INTEGER     NOT NULL DEFAULT 0,
    pool_max         INTEGER     NOT NULL DEFAULT 0,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (hostname, pid)
);
ALTER TABLE worker_stats ADD COLUMN IF NOT EXISTS threads_peak INTEGER NOT NULL DEFAULT 0;

-- Candidate photos are stored IN the database (not on disk), because Render's disk
-- is wiped on every restart. One photo per candidate, deleted with the candidate.
-- updated_at becomes part of the photo URL, so phones can cache each photo forever.
CREATE TABLE IF NOT EXISTS candidate_photos (
    candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
    mime         TEXT        NOT NULL,
    data         BYTEA       NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- ===================== Voting EVENTS (added later, safe to re-run) =====================
-- Several elections can exist at once (e.g. SSC Election + Org Election). Each event has its own
-- positions, candidates, registered voters and ballots. Status is COMPUTED (see app.py):
--   manual = 'open'   -> CURRENT   (admin opened it by hand)
--   manual = 'closed' -> PREVIOUS  (admin closed it by hand)
--   manual = 'auto'   -> from the dates: before starts_at UPCOMING, after ends_at PREVIOUS, else CURRENT
-- Existing positions, ballots and voters are moved into one default event by db.py (migrate_events).
CREATE TABLE IF NOT EXISTS events (
    id           SERIAL PRIMARY KEY,
    name         TEXT        NOT NULL,
    description  TEXT        NOT NULL DEFAULT '',
    organization TEXT        NOT NULL DEFAULT '',
    school_year  TEXT        NOT NULL DEFAULT '',
    starts_at    TIMESTAMPTZ,
    ends_at      TIMESTAMPTZ,
    manual       TEXT        NOT NULL DEFAULT 'auto' CHECK (manual IN ('auto', 'open', 'closed')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS event_id INTEGER REFERENCES events(id) ON DELETE CASCADE;
-- seats = how many candidates a voter may choose (and how many win), e.g. Councilor: 3
ALTER TABLE positions ADD COLUMN IF NOT EXISTS seats INTEGER NOT NULL DEFAULT 1 CHECK (seats BETWEEN 1 AND 20);
ALTER TABLE ballots ADD COLUMN IF NOT EXISTS event_id INTEGER REFERENCES events(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS positions_event_idx ON positions (event_id);
CREATE INDEX IF NOT EXISTS ballots_event_idx ON ballots (event_id);

-- Who may vote in which event. One student account (Voter ID) can be registered in several events.
-- has_voted is PER EVENT: the vote lock is SELECT ... FOR UPDATE on this row (event, student).
CREATE TABLE IF NOT EXISTS event_voters (
    event_id      INTEGER     NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    student_id    INTEGER     NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    has_voted     BOOLEAN     NOT NULL DEFAULT FALSE,
    voted_at      TIMESTAMPTZ,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, student_id)
);
CREATE INDEX IF NOT EXISTS event_voters_student_idx ON event_voters (student_id);

-- ---------------------------------------------------------------- online registration
-- Students register THEMSELVES for an event (school student number, details, own password).
-- COMELEC approves or rejects each request in the admin Registrations tab. Approving a NEW student
-- creates their account (system-generated Voter ID) and adds them to event_voters.
--   student_id    = the account, once it exists (existing account, or created on approval)
--   password_hash = the password the NEW student chose (hashed like every other password),
--                   moved to the account on approval and then cleared here
-- One request per school student number per event: UNIQUE (event_id, school_student_no).
CREATE TABLE IF NOT EXISTS registrations (
    id                SERIAL PRIMARY KEY,
    event_id          INTEGER     NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    school_student_no TEXT        NOT NULL,
    student_id        INTEGER     REFERENCES students(id) ON DELETE CASCADE,
    full_name         TEXT        NOT NULL,
    course            TEXT        NOT NULL DEFAULT '',
    department        TEXT        NOT NULL DEFAULT '',
    year_level        INTEGER     CHECK (year_level BETWEEN 1 AND 5),
    section           TEXT        NOT NULL DEFAULT '',
    email             TEXT        NOT NULL DEFAULT '',
    password_hash     TEXT,
    status            TEXT        NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    reason            TEXT        NOT NULL DEFAULT '',
    submitted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at        TIMESTAMPTZ,
    UNIQUE (event_id, school_student_no)
);
CREATE INDEX IF NOT EXISTS registrations_event_status_idx ON registrations (event_id, status);
CREATE INDEX IF NOT EXISTS registrations_school_no_idx ON registrations (school_student_no);
CREATE INDEX IF NOT EXISTS registrations_student_idx ON registrations (student_id);

-- Rate limit for the public registration / status forms (per IP address, kept in the database so
-- every worker process sees the same count). Old rows are deleted as new ones come in.
CREATE TABLE IF NOT EXISTS form_attempts (
    id      BIGSERIAL   PRIMARY KEY,
    kind    TEXT        NOT NULL,
    ip      TEXT        NOT NULL,
    at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS form_attempts_idx ON form_attempts (kind, ip, at);

-- ---------------------------------------------------------------- LOGIN FIRST: student accounts
-- Students create their OWN account (Voter ID generated at once) and COMELEC verifies it ONCE.
--   pending  = created online, waiting for COMELEC (can log in, sees the status, can't register or vote)
--   verified = can register for events (one click) and vote
--   rejected = sees the reason
-- Accounts that existed before this (class-list imports, earlier registrations) count as verified.
ALTER TABLE students ADD COLUMN IF NOT EXISTS account_status TEXT NOT NULL DEFAULT 'verified'
    CHECK (account_status IN ('pending', 'verified', 'rejected'));
ALTER TABLE students ADD COLUMN IF NOT EXISTS account_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE students ADD COLUMN IF NOT EXISTS account_decided_at TIMESTAMPTZ;
ALTER TABLE students ADD COLUMN IF NOT EXISTS email TEXT NOT NULL DEFAULT '';
ALTER TABLE students ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();
CREATE INDEX IF NOT EXISTS students_account_status_idx ON students (account_status);
-- Per event: registrations from VERIFIED accounts are approved at once (default ON).
ALTER TABLE events ADD COLUMN IF NOT EXISTS auto_approve BOOLEAN NOT NULL DEFAULT TRUE;

-- ---------------------------------------------------------------- official results
-- auto_publish = "Publish results automatically when voting ends" (default OFF: COMELEC reviews the
-- final results and clicks "Publish official results"). results_published / results_published_at are
-- added by db.py (migrate_results), which also marks events that had ALREADY ended as published.
ALTER TABLE events ADD COLUMN IF NOT EXISTS auto_publish BOOLEAN NOT NULL DEFAULT FALSE;
