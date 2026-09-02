-- Argus: companies, the boards that reach them, and the postings they carry.
--
-- Mirrors argus/core/db.py at schema_version 7. The two are kept deliberately
-- close so the same hand-written SQL runs on both engines: SQLite for tests
-- and local work, Postgres for production.
--
-- Two type choices are worth stating, because both look wrong at a glance.
--
-- Timestamps are bigint epoch seconds rather than timestamptz. Every
-- comparison in the codebase is integer arithmetic against time.time(), and
-- converting would touch every scheduling predicate for no gain the product
-- can see. The column names all end in _at so the unit is not a mystery.
--
-- Booleans are smallint rather than boolean, for the same reason: SQLite has
-- no boolean type, and the writer sends 0/1. A real boolean column would
-- reject that int and split the code into two dialects.

-- ---------------------------------------------------------------- companies
-- Who is hiring, independent of how we currently reach them. Identity is the
-- apex domain -- the one attribute a company keeps when it renames, rebrands
-- or migrates ATS. Rows without a domain are allowed, because a board can
-- name a company long before we resolve its site, and are merged into the
-- domain-bearing row the moment we learn it.
CREATE TABLE IF NOT EXISTS companies (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    domain             text,
    name               text,
    norm_name          text,
    website            text,
    careers_url        text,
    -- ats:  the careers page links to a board we can poll
    -- html: a real careers page on no ATS we recognize
    -- none: probed, nothing found
    careers_kind       text,
    careers_checked_at bigint,
    first_seen_at      bigint NOT NULL,
    source             text
);

-- Partial, because many companies legitimately have no domain yet and NULLs
-- must not collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_domain
    ON companies (domain) WHERE domain IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_companies_norm
    ON companies (norm_name);
CREATE INDEX IF NOT EXISTS idx_companies_careers
    ON companies (careers_kind, careers_checked_at);

-- ------------------------------------------------------------------- boards
-- How we currently reach a company. Many-to-one on purpose: a company that
-- migrated Greenhouse to Ashby has two boards, one dead and one live, and
-- both point at the same row.
CREATE TABLE IF NOT EXISTS boards (
    ats                  text    NOT NULL,
    slug                 text    NOT NULL,
    company_name         text,
    -- unvalidated | active | empty | dead
    status               text    NOT NULL DEFAULT 'unvalidated',
    tier                 smallint NOT NULL DEFAULT 1,
    job_count            integer,
    next_poll_at         bigint,
    last_polled_at       bigint,
    last_ok_at           bigint,
    last_new_at          bigint,
    consecutive_failures integer NOT NULL DEFAULT 0,
    last_error           text,
    first_seen_at        bigint  NOT NULL,
    website              text,
    careers_url          text,
    careers_checked_at   bigint,
    company_id           bigint REFERENCES companies (id) ON DELETE SET NULL,
    PRIMARY KEY (ats, slug)
);

CREATE INDEX IF NOT EXISTS idx_boards_due     ON boards (status, next_poll_at);
CREATE INDEX IF NOT EXISTS idx_boards_ats     ON boards (ats, status);
CREATE INDEX IF NOT EXISTS idx_boards_company ON boards (company_id);

-- ------------------------------------------------------------ board_sources
-- Provenance, many-to-one: the same board is typically found by several
-- sources. Separate so we can measure which sources earn their runtime.
CREATE TABLE IF NOT EXISTS board_sources (
    ats           text   NOT NULL,
    slug          text   NOT NULL,
    source        text   NOT NULL,
    first_seen_at bigint NOT NULL,
    detail        text,
    PRIMARY KEY (ats, slug, source)
);

-- --------------------------------------------------------------------- jobs
-- The feed. Only the reconciler writes here.
--
-- content_hash, last_seen_at, status and missing_polls are not metadata --
-- they are the diff. A posting closes when it has been absent from
-- CLOSE_GRACE_POLLS consecutive successful polls, which is what missing_polls
-- counts.
CREATE TABLE IF NOT EXISTS jobs (
    ats               text   NOT NULL,
    slug              text   NOT NULL,
    external_id       text   NOT NULL,
    title             text,
    location          text,
    locations_json    text,
    url               text,
    posted_at         bigint,
    first_seen_at     bigint NOT NULL,
    last_seen_at      bigint NOT NULL,
    closed_at         bigint,
    status            text   NOT NULL DEFAULT 'open',
    missing_polls     integer NOT NULL DEFAULT 0,
    content_hash      text,
    source            text,
    -- Role classification, computed at ingest so filtering is an index seek.
    -- classified_by holds the ruleset version: improving the rules is a sweep
    -- of the rows that disagree with it, not a re-poll of every board.
    role_family       text,
    is_engineering    smallint,
    is_fde            smallint,
    seniority         text,
    classified_by     text,
    PRIMARY KEY (ats, slug, external_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_board_open  ON jobs (ats, slug, status);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen  ON jobs (first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status_seen ON jobs (status, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_jobs_family      ON jobs (role_family, status, first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_eng         ON jobs (is_engineering, status, first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_fde         ON jobs (is_fde, status, first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_stale       ON jobs (classified_by);
CREATE INDEX IF NOT EXISTS idx_jobs_company     ON jobs (ats, slug);

-- ------------------------------------------------------------------- events
-- The change log, and the notifier's trigger. The only table that grows
-- without bound: one row per posting change, forever.
--
-- Not partitioned, deliberately. Monthly partitions need something to create
-- next month's partition, and a scheduled DELETE by ts is both simpler and
-- sufficient at this size. Revisit if it stops being true.
CREATE TABLE IF NOT EXISTS events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts          bigint NOT NULL,
    type        text   NOT NULL,
    ats         text,
    slug        text,
    external_id text,
    title       text,
    url         text,
    detail_json text
);

CREATE INDEX IF NOT EXISTS idx_events_ts   ON events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (type, ts DESC);

-- ---------------------------------------------------------------- run stats
-- poll_runs answers "did the last poll work". source_runs answers "is this
-- discovery source still earning its runtime", which is the question that
-- decides what to keep and which nothing off-the-shelf can infer.
CREATE TABLE IF NOT EXISTS poll_runs (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at    bigint,
    finished_at   bigint,
    boards_polled integer DEFAULT 0,
    boards_failed integer DEFAULT 0,
    new_jobs      integer DEFAULT 0,
    edited_jobs   integer DEFAULT 0,
    closed_jobs   integer DEFAULT 0
);

CREATE TABLE IF NOT EXISTS source_runs (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source         text   NOT NULL,
    started_at     bigint NOT NULL,
    finished_at    bigint,
    refs_seen      integer DEFAULT 0,
    new_boards     integer DEFAULT 0,
    new_companies  integer DEFAULT 0,
    seed_postings  integer DEFAULT 0,
    error          text
);

CREATE INDEX IF NOT EXISTS idx_source_runs ON source_runs (source, started_at DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   text PRIMARY KEY,
    value text
);

INSERT INTO meta (key, value) VALUES ('schema_version', '7')
    ON CONFLICT (key) DO UPDATE SET value = excluded.value;
