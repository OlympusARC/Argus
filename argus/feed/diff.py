"""The set-diff, computed in the database, for many boards at once.

The rules are unchanged -- they are expressed in SQL rather than as a Python
loop over rows pulled down from the database. Two costs drove the design, and
they pull in opposite directions:

  bytes        the reconciler used to read every stored posting for a board
               to compare it. The stored state is already in the database and
               the fetched state is in memory, so that dragged the larger side
               across the network to meet the smaller one.

  round trips  doing the comparison in SQL costs seven statements. Per board
               that is worse than what it replaced: 400 boards took 384
               seconds, which is 85 minutes for a full tier-1 sweep against a
               50-minute workflow timeout.

So the batch is the unit, not the board. Seven statements settle a hundred
boards as easily as one, and both costs land where they should: bytes
proportional to what changed, round trips proportional to the number of
batches rather than the number of boards.

Every construct here works identically on SQLite 3.39+ and Postgres, so the
same statements run in tests and in production -- the tested path is the
shipped one rather than an approximation of it.
"""

from __future__ import annotations

from ..classify import classify
from ..core import config
from ..core.models import Posting
from . import jobs as jobs_mod

BOARDS_DDL = """
CREATE TEMP TABLE IF NOT EXISTS staged_boards (
    ats        TEXT NOT NULL,
    slug       TEXT NOT NULL,
    suspicious SMALLINT NOT NULL DEFAULT 0,
    PRIMARY KEY (ats, slug)
)
"""

POSTINGS_DDL = """
CREATE TEMP TABLE IF NOT EXISTS staged_postings (
    ats             TEXT NOT NULL,
    slug            TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    title           TEXT,
    location        TEXT,
    locations_json  TEXT,
    url             TEXT,
    posted_at       BIGINT,
    content_hash    TEXT,
    role_family     TEXT,
    is_engineering  SMALLINT,
    is_fde          SMALLINT,
    seniority       TEXT,
    classified_by   TEXT,
    PRIMARY KEY (ats, slug, external_id)
)
"""

"""
One row per board in the batch: how many postings it holds open, and how many
the fetch returned. A board that held many and returned none is a bad
response, not a mass layoff -- and answering that costs two integers per
board rather than a read of every posting on it.
"""
GUARD = """
SELECT b.ats, b.slug,
       (SELECT COUNT(*) FROM jobs j
        WHERE j.ats=b.ats AND j.slug=b.slug AND j.status='open') AS open_before,
       (SELECT COUNT(*) FROM staged_postings s
        WHERE s.ats=b.ats AND s.slug=b.slug) AS present
FROM staged_boards b
"""

INSERT_NEW = """
INSERT INTO jobs (ats, slug, external_id, title, location, locations_json, url,
                  posted_at, first_seen_at, last_seen_at, content_hash, source,
                  role_family, is_engineering, is_fde, seniority, classified_by)
SELECT s.ats, s.slug, s.external_id, s.title, s.location, s.locations_json, s.url,
       s.posted_at, ?, ?, s.content_hash, 'poll',
       s.role_family, s.is_engineering, s.is_fde, s.seniority, s.classified_by
FROM staged_postings s
WHERE true
ON CONFLICT (ats, slug, external_id) DO NOTHING
RETURNING ats, slug, external_id, title, url, location
"""

"""
Reopen runs before edit so a returning posting is reported as a reopen rather
than an edit. It leaves the new content hash behind, which is what stops the
edit statement seeing the same row a second time.
"""
REOPEN = """
UPDATE jobs SET title=s.title, location=s.location, locations_json=s.locations_json,
                url=s.url, posted_at=s.posted_at, content_hash=s.content_hash,
                role_family=s.role_family, is_engineering=s.is_engineering,
                is_fde=s.is_fde, seniority=s.seniority, classified_by=s.classified_by,
                last_seen_at=?, missing_polls=0, status='open', closed_at=NULL
FROM staged_postings s
WHERE jobs.ats=s.ats AND jobs.slug=s.slug AND jobs.external_id=s.external_id
  AND jobs.status='closed'
RETURNING jobs.ats, jobs.slug, jobs.external_id, jobs.title, jobs.url, jobs.location
"""

EDIT = """
UPDATE jobs SET title=s.title, location=s.location, locations_json=s.locations_json,
                url=s.url, posted_at=s.posted_at, content_hash=s.content_hash,
                role_family=s.role_family, is_engineering=s.is_engineering,
                is_fde=s.is_fde, seniority=s.seniority, classified_by=s.classified_by,
                last_seen_at=?, missing_polls=0
FROM staged_postings s
WHERE jobs.ats=s.ats AND jobs.slug=s.slug AND jobs.external_id=s.external_id
  AND jobs.status='open'
  AND jobs.content_hash IS DISTINCT FROM s.content_hash
RETURNING jobs.ats, jobs.slug, jobs.external_id, jobs.title, jobs.url, jobs.location
"""

"""
Unchanged and still listed. Deliberately no RETURNING: there is no event to
emit, and returning these rows is exactly the cost this module exists to
remove.
"""
TOUCH = """
UPDATE jobs SET last_seen_at=?, missing_polls=0
FROM staged_postings s
WHERE jobs.ats=s.ats AND jobs.slug=s.slug AND jobs.external_id=s.external_id
  AND jobs.status='open'
  AND jobs.content_hash IS NOT DISTINCT FROM s.content_hash
"""

"""
Absent from a successful fetch. The grace counter rises and the row closes
only once it has been missing from CLOSE_GRACE_POLLS consecutive successful
polls -- a full-board endpoint is near-authoritative, not perfectly so.

Boards flagged suspicious by the guard are excluded here rather than earlier,
which is what lets a suspect board leave no trace at all.
"""
MISSING = """
UPDATE jobs SET missing_polls = missing_polls + 1,
                status = CASE WHEN missing_polls + 1 >= ? THEN 'closed' ELSE status END,
                closed_at = CASE WHEN missing_polls + 1 >= ? THEN ? ELSE closed_at END
WHERE status='open'
  AND EXISTS (SELECT 1 FROM staged_boards b
              WHERE b.ats=jobs.ats AND b.slug=jobs.slug AND b.suspicious=0)
  AND NOT EXISTS (SELECT 1 FROM staged_postings s
                  WHERE s.ats=jobs.ats AND s.slug=jobs.slug
                    AND s.external_id=jobs.external_id)
RETURNING ats, slug, external_id, title, url, location, missing_polls, status
"""

Key = tuple[str, str]


def _reset(conn) -> None:
    conn.execute(BOARDS_DDL)
    conn.execute(POSTINGS_DDL)
    conn.execute("DELETE FROM staged_boards")
    conn.execute("DELETE FROM staged_postings")


def _stage(conn, fetched: dict[Key, list[Posting]]) -> None:
    conn.executemany(
        "INSERT INTO staged_boards (ats, slug) VALUES (?,?) ON CONFLICT DO NOTHING",
        list(fetched),
    )
    rows = []
    for (ats, slug), postings in fetched.items():
        for p in postings:
            role = classify(p.title, p.department)
            rows.append(
                (
                    ats,
                    slug,
                    p.external_id,
                    p.title,
                    p.location,
                    jobs_mod._locations(p),
                    p.url,
                    p.posted_at,
                    p.content_hash(),
                    role.family,
                    int(role.is_engineering),
                    int(role.is_fde),
                    role.seniority,
                    role.ruleset,
                )
            )
    if rows:
        conn.executemany(
            """INSERT INTO staged_postings
                   (ats, slug, external_id, title, location, locations_json, url,
                    posted_at, content_hash, role_family, is_engineering, is_fde,
                    seniority, classified_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (ats, slug, external_id) DO NOTHING""",
            rows,
        )


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def run_batch(conn, fetched: dict[Key, list[Posting]]) -> dict[Key, dict]:
    """Diff every board in the batch. Returns per-board changes and events.

    Seven statements settle the whole batch, so the cost is proportional to
    the number of batches rather than the number of boards.
    """
    result: dict[Key, dict] = {
        key: {"new": [], "reopened": [], "edited": [], "closed": [], "suspicious": False}
        for key in fetched
    }
    if not fetched:
        return result

    _reset(conn)
    _stage(conn, fetched)

    suspicious: list[Key] = []
    for row in _rows(conn.execute(GUARD, ())):
        key = (row["ats"], row["slug"])
        if row["present"] == 0 and row["open_before"] > config.MASS_CLOSE_GUARD:
            suspicious.append(key)
            if key in result:
                result[key]["suspicious"] = True
    if suspicious:
        conn.executemany(
            "UPDATE staged_boards SET suspicious=1 WHERE ats=? AND slug=?", suspicious
        )
        """
        A suspect board must leave no trace, so its staged postings are
        dropped before anything is written -- not merely skipped at close
        time. Its fetch returned nothing, so there is nothing to lose.
        """
        conn.executemany("DELETE FROM staged_postings WHERE ats=? AND slug=?", suspicious)

    ts = jobs_mod.now()
    for kind, sql, params in (
        ("new", INSERT_NEW, (ts, ts)),
        ("reopened", REOPEN, (ts,)),
        ("edited", EDIT, (ts,)),
    ):
        for row in _rows(conn.execute(sql, params)):
            key = (row["ats"], row["slug"])
            if key in result:
                result[key][kind].append(row)
    conn.execute(TOUCH, (ts,))

    grace = config.CLOSE_GRACE_POLLS
    for row in _rows(conn.execute(MISSING, (grace, grace, ts))):
        if row["status"] != "closed":
            continue
        key = (row["ats"], row["slug"])
        if key in result:
            result[key]["closed"].append(row)
    return result
