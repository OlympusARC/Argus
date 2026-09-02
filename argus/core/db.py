"""SQLite schema and connection handling.

Three tables that deliberately never merge:
  companies -- who is hiring. The durable entity, keyed by domain.
  boards    -- the registry. Discovery writes here, pollers only read/annotate.
  jobs      -- the feed. Only the reconciler writes here.

The company is the layer that outlives the others. A board slug dies the day a
company switches ATS and a posting dies the day it is filled, but the company
and its careers page persist -- so `companies` is what we monitor, and boards
are the current means of doing it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA_VERSION = 10

SCHEMA = """
-- Who is hiring, independent of how we currently reach them.
--
-- Identity is the apex domain, because that is the one attribute a company
-- does not change when it renames, rebrands or migrates ATS. Rows without a
-- domain are allowed (a board can name a company long before we resolve its
-- site) and are merged into the domain-bearing row the moment we learn it,
-- which is why norm_name is indexed.
CREATE TABLE IF NOT EXISTS companies (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    domain             TEXT,
    name               TEXT,
    norm_name          TEXT,
    website            TEXT,
    careers_url        TEXT,
    -- ats:  the careers page links to a board we can poll -- monitoring is the
    --       board poll, and careers_url is only the durable pointer back.
    -- html: a real careers page on no ATS we recognize. Nothing polls it yet.
    -- none: probed, nothing found. Retried on a slow cadence.
    careers_kind       TEXT,
    careers_checked_at INTEGER,
    first_seen_at      INTEGER NOT NULL,
    source             TEXT
);
-- Partial index: many companies legitimately have no domain yet, and NULLs
-- must not collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_domain
    ON companies(domain) WHERE domain IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_companies_norm    ON companies(norm_name);
CREATE INDEX IF NOT EXISTS idx_companies_careers ON companies(careers_kind, careers_checked_at);

CREATE TABLE IF NOT EXISTS boards (
    ats                  TEXT    NOT NULL,
    slug                 TEXT    NOT NULL,
    company_name         TEXT,
    -- unvalidated: discovered but never probed
    -- active: probe returned postings
    -- empty: probe succeeded but board has zero listings (still poll, may fill)
    -- dead: probe returned a permanent 404/410, or too many failures
    status               TEXT    NOT NULL DEFAULT 'unvalidated',
    tier                 INTEGER NOT NULL DEFAULT 1,
    job_count            INTEGER,
    next_poll_at         INTEGER,
    last_polled_at       INTEGER,
    last_ok_at           INTEGER,
    last_new_at          INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    first_seen_at        INTEGER NOT NULL,
    -- The company's own careers page, kept alongside the board. A slug goes
    -- dead when a company switches ATS, but the careers page follows them --
    -- so re-probing this is how we notice a migration instead of just losing
    -- the company.
    website              TEXT,
    careers_url          TEXT,
    careers_checked_at   INTEGER,
    -- Which company this board belongs to. Many-to-one on purpose: a company
    -- that migrates Greenhouse -> Ashby has two boards, one dead and one live,
    -- and both point at the same row.
    company_id           INTEGER REFERENCES companies(id),
    PRIMARY KEY (ats, slug)
);
CREATE INDEX IF NOT EXISTS idx_boards_due     ON boards(status, next_poll_at);
CREATE INDEX IF NOT EXISTS idx_boards_ats     ON boards(ats, status);
CREATE INDEX IF NOT EXISTS idx_boards_company ON boards(company_id);

-- Provenance is many-to-one: the same board is typically found by several
-- sources. Keeping this separate lets us measure which sources actually earn
-- their runtime instead of guessing.
CREATE TABLE IF NOT EXISTS board_sources (
    ats           TEXT    NOT NULL,
    slug          TEXT    NOT NULL,
    source        TEXT    NOT NULL,
    first_seen_at INTEGER NOT NULL,
    detail        TEXT,
    PRIMARY KEY (ats, slug, source)
);

CREATE TABLE IF NOT EXISTS jobs (
    ats               TEXT    NOT NULL,
    slug              TEXT    NOT NULL,
    external_id       TEXT    NOT NULL,
    title             TEXT,
    location          TEXT,
    locations_json    TEXT,
    url               TEXT,
    posted_at         INTEGER,
    first_seen_at     INTEGER NOT NULL,
    last_seen_at      INTEGER NOT NULL,
    closed_at         INTEGER,
    status            TEXT    NOT NULL DEFAULT 'open',
    missing_polls     INTEGER NOT NULL DEFAULT 0,
    content_hash      TEXT,
    source            TEXT,
    -- Role classification, computed at ingest so filtering is an index seek
    -- rather than a LIKE scan that grows with the corpus. classified_by holds
    -- the ruleset version, so improving the rules is a sweep of the rows that
    -- disagree with it rather than a re-poll of every board.
    role_family       TEXT,
    is_engineering    INTEGER,
    is_fde            INTEGER,
    seniority         TEXT,
    classified_by     TEXT,
    PRIMARY KEY (ats, slug, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_board_open  ON jobs(ats, slug, status);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen  ON jobs(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status_seen ON jobs(status, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_jobs_family ON jobs(role_family, status, first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_eng    ON jobs(is_engineering, status, first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_fde    ON jobs(is_fde, status, first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_stale  ON jobs(classified_by);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    type        TEXT    NOT NULL,   -- new | edited | closed | reopened
    ats         TEXT,
    slug        TEXT,
    external_id TEXT,
    title       TEXT,
    url         TEXT,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);

CREATE TABLE IF NOT EXISTS poll_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    INTEGER,
    finished_at   INTEGER,
    boards_polled INTEGER DEFAULT 0,
    boards_failed INTEGER DEFAULT 0,
    new_jobs      INTEGER DEFAULT 0,
    edited_jobs   INTEGER DEFAULT 0,
    closed_jobs   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS notifier_state (
    key           TEXT PRIMARY KEY,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    updated_at    INTEGER
);

CREATE TABLE IF NOT EXISTS notified_jobs (
    ats         TEXT NOT NULL,
    slug        TEXT NOT NULL,
    external_id TEXT NOT NULL,
    notified_at INTEGER NOT NULL,
    PRIMARY KEY (ats, slug, external_id)
);

CREATE INDEX IF NOT EXISTS idx_notified_at ON notified_jobs(notified_at);

CREATE TABLE IF NOT EXISTS source_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT    NOT NULL,
    started_at    INTEGER NOT NULL,
    finished_at   INTEGER,
    refs_seen     INTEGER DEFAULT 0,
    new_boards    INTEGER DEFAULT 0,
    new_companies INTEGER DEFAULT 0,
    seed_postings INTEGER DEFAULT 0,
    blocked       INTEGER DEFAULT 0,
    error         TEXT,
    skipped       TEXT
);

CREATE INDEX IF NOT EXISTS idx_source_runs ON source_runs(source, started_at DESC);

CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    payload     TEXT    NOT NULL,
    evidence    TEXT,
    status      TEXT    NOT NULL DEFAULT 'drafted',
    score       REAL,
    created_at  INTEGER NOT NULL,
    decided_at  INTEGER,
    decided_by  TEXT
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status, created_at DESC);
"""


"""
Applied in order for databases created before the current SCHEMA_VERSION.
"""
MIGRATIONS: dict[int, list[str]] = {
    # Where an LLM agent's output lands. Never the core tables: an agent
    # proposes, a deterministic gate disposes, and the separation is a table
    # rather than a convention so it cannot be argued away in a prompt.
    10: [
        "CREATE TABLE IF NOT EXISTS proposals ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL, kind TEXT NOT NULL, "
        "payload TEXT NOT NULL, evidence TEXT, status TEXT NOT NULL DEFAULT 'drafted', "
        "score REAL, created_at INTEGER NOT NULL, decided_at INTEGER, decided_by TEXT)",
        "CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status, created_at DESC)",
    ],
    # source_runs existed in the Postgres schema and not in this one, which is
    # the sort of drift that only shows up when something finally reads it.
    # blocked and skipped are new on both sides: a run that was rate-limited
    # into silence and a run that never started are different failures, and a
    # policy that cannot tell them apart will heal the wrong thing.
    9: [
        "CREATE TABLE IF NOT EXISTS source_runs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, "
        "started_at INTEGER NOT NULL, finished_at INTEGER, refs_seen INTEGER DEFAULT 0, "
        "new_boards INTEGER DEFAULT 0, new_companies INTEGER DEFAULT 0, "
        "seed_postings INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0, "
        "error TEXT, skipped TEXT)",
        "CREATE INDEX IF NOT EXISTS idx_source_runs ON source_runs(source, started_at DESC)",
    ],
    # The notifier's two tables. Separate from events on purpose: events are
    # the record of what happened, these are the record of what we said about
    # it, and a second reader (email, a second channel) gets its own watermark
    # row without touching the feed.
    8: [
        "CREATE TABLE IF NOT EXISTS notifier_state (key TEXT PRIMARY KEY, last_event_id INTEGER NOT NULL DEFAULT 0, updated_at INTEGER)",
        "CREATE TABLE IF NOT EXISTS notified_jobs (ats TEXT NOT NULL, slug TEXT NOT NULL, external_id TEXT NOT NULL, notified_at INTEGER NOT NULL, PRIMARY KEY (ats, slug, external_id))",
        "CREATE INDEX IF NOT EXISTS idx_notified_at ON notified_jobs(notified_at)",
    ],
    2: [
        "ALTER TABLE boards ADD COLUMN website TEXT",
        "ALTER TABLE boards ADD COLUMN careers_url TEXT",
        "ALTER TABLE boards ADD COLUMN careers_checked_at INTEGER",
    ],
    # The companies table itself is in SCHEMA and created by CREATE IF NOT
    # EXISTS, so only the back-reference needs an ALTER here.
    3: [
        "ALTER TABLE boards ADD COLUMN company_id INTEGER REFERENCES companies(id)",
    ],
    # raw_json was write-only: nothing in the codebase ever selected it. It
    # cost 616 bytes a row -- half the database -- to store vendor payloads we
    # had already parsed into typed columns. Adapters still build Posting.raw
    # for in-process debugging; it is simply no longer persisted.
    4: [
        "ALTER TABLE jobs DROP COLUMN raw_json",
    ],
    # apply_url was always url + "/application" (Ashby) or "/apply" (Lever) --
    # 100% derivable across 100,026 rows, so storing it was 8 MB of restating
    # the url column. locations_json is kept but written only when it holds
    # more than one location; see feed/jobs.py:_locations.
    # Pay and the sparse descriptive fields are not part of the product, so
    # they are not stored. They ARE still in Posting._HASHED, deliberately:
    # rehashing would give every one of 375k rows a new content_hash and emit
    # a spurious "edited" event for each. The cost is that an edit to a field
    # we no longer keep still registers as a change we cannot display.
    # Classification columns. Nullable, because rows written before this exist
    # and the sweep fills them in; classified_by NULL simply means never
    # classified, which is the same queue as classified by an older ruleset.
    7: [
        "ALTER TABLE jobs ADD COLUMN role_family TEXT",
        "ALTER TABLE jobs ADD COLUMN is_engineering INTEGER",
        "ALTER TABLE jobs ADD COLUMN is_fde INTEGER",
        "ALTER TABLE jobs ADD COLUMN seniority TEXT",
        "ALTER TABLE jobs ADD COLUMN classified_by TEXT",
    ],
    6: [
        "ALTER TABLE jobs DROP COLUMN compensation_json",
        "ALTER TABLE jobs DROP COLUMN department",
        "ALTER TABLE jobs DROP COLUMN team",
        "ALTER TABLE jobs DROP COLUMN employment_type",
        "ALTER TABLE jobs DROP COLUMN workplace_type",
        "ALTER TABLE jobs DROP COLUMN is_remote",
    ],
    5: [
        "ALTER TABLE jobs DROP COLUMN apply_url",
        "UPDATE jobs SET locations_json = NULL "
        "WHERE locations_json IS NOT NULL AND json_array_length(locations_json) <= 1",
    ],
}


def is_postgres() -> bool:
    return config.database_url() is not None


def insert_id(conn, sql: str, params: tuple = ()) -> int:
    """INSERT one row and return its generated id, on either backend.

    sqlite3 offers cursor.lastrowid; psycopg does not, and pg.py returns None
    there rather than raising. That made a missing id a silent failure: eight
    poll_runs rows were written with a null run_id, so the UPDATE that should
    have closed them matched nothing and every run since the Postgres
    migration is orphaned with zeroed counts. The same call in the new
    source_runs writer crashed instead, which is the only reason any of it
    was noticed.

    RETURNING works on SQLite 3.35+ and Postgres, so one statement covers
    both and there is no id to lose.
    """
    row = conn.execute(f"{sql.rstrip().rstrip(';')} RETURNING id", params).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING id produced no row")
    return int(row["id"])


def connect(path: Path | None = None, *, pooled: bool = False):
    """SQLite by default; Postgres whenever a database URL is configured.

    The same statements run on both -- portability is maintained in the SQL
    itself rather than by branching here. See core/pg.py.
    """
    url = config.database_url(pooled=pooled)
    if url and path is None:
        from . import pg

        return pg.connect(url)
    return connect_sqlite(path)


def connect_sqlite(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(path: Path | None = None) -> sqlite3.Connection:
    """Create or migrate the local schema; a no-op against Postgres.

    Postgres owns its schema in supabase/migrations, applied on merge to main
    by the Supabase GitHub integration. Two systems both entitled to create
    tables is how they end up disagreeing about which one is authoritative, so
    this returns the connection untouched and lets the migrations be the only
    writer of DDL there.
    """
    if is_postgres() and path is None:
        return connect(path)

    conn = connect(path)
    row = (
        conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        else None
    )
    current = int(row["value"]) if row else 0
    """
    Migrations run FIRST. SCHEMA indexes the columns they add, so creating
    the schema before migrating fails on an existing database with "no such
    column". On a fresh database current is 0, every migration is skipped,
    and SCHEMA alone is already correct.
    """
    for version in sorted(MIGRATIONS):
        if current and current < version:
            for stmt in MIGRATIONS[version]:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
    conn.executescript(SCHEMA)  # CREATE IF NOT EXISTS is safe to re-run
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    return conn
