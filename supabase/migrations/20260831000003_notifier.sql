-- The notifier's two tables.
--
-- Kept separate from events deliberately: events record what happened, these
-- record what we have said about it. A second channel -- email, a second
-- Discord room -- takes another notifier_state row and shares the feed
-- untouched.
--
-- bigint epochs and no timestamptz, matching the rest of the schema: the
-- writer is SQLite-shaped and sends integers.

CREATE TABLE IF NOT EXISTS notifier_state (
    key           text PRIMARY KEY,
    last_event_id bigint NOT NULL DEFAULT 0,
    updated_at    bigint
);

CREATE TABLE IF NOT EXISTS notified_jobs (
    ats         text   NOT NULL,
    slug        text   NOT NULL,
    external_id text   NOT NULL,
    notified_at bigint NOT NULL,
    PRIMARY KEY (ats, slug, external_id)
);

-- Supports the dedupe window predicate in SELECT_PENDING.
CREATE INDEX IF NOT EXISTS idx_notified_at ON notified_jobs (notified_at);
