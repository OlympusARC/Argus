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

from ..classify import classify, geo
from ..core import config
from ..core.models import Posting
from . import jobs as jobs_mod

BOARDS_DDL = """
CREATE TEMP TABLE IF NOT EXISTS staged_boards (
    ats        TEXT NOT NULL,
    slug       TEXT NOT NULL,
    suspicious SMALLINT NOT NULL DEFAULT 0,
    fetched    INTEGER NOT NULL DEFAULT 0,
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
    region          TEXT,
    PRIMARY KEY (ats, slug, external_id)
)
"""

"""
One row per board in the batch: how many postings it holds open, and how many
the fetch returned. A board that held many and returned none is a bad
response, not a mass layoff -- and answering that costs two integers per
board rather than a read of every posting on it.

`present` counts what the fetch returned, not what was staged. Those differ
once ingest filtering is on: a retail board can return five hundred postings
of which none are technical, and staging zero rows is the correct outcome
rather than evidence of a broken response. Judging the fetch by the filtered
count would mark every such board suspicious on every poll.
"""
GUARD = """
SELECT b.ats, b.slug, b.fetched AS present,
       (SELECT COUNT(*) FROM jobs j
        WHERE j.ats=b.ats AND j.slug=b.slug AND j.status='open') AS open_before
FROM staged_boards b
"""

INSERT_NEW = """
INSERT INTO jobs (ats, slug, external_id, title, location, locations_json, url,
                  posted_at, first_seen_at, last_seen_at, content_hash, source,
                  role_family, is_engineering, is_fde, seniority, classified_by, region)
SELECT s.ats, s.slug, s.external_id, s.title, s.location, s.locations_json, s.url,
       s.posted_at, ?, ?, s.content_hash, 'poll',
       s.role_family, s.is_engineering, s.is_fde, s.seniority, s.classified_by,
       s.region
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
                region=s.region,
                role_family=s.role_family, is_engineering=s.is_engineering,
                is_fde=s.is_fde, seniority=s.seniority, classified_by=s.classified_by,
                last_seen_at=?, missing_polls=0, status='open', closed_at=NULL
FROM staged_postings s
WHERE jobs.ats=s.ats AND jobs.slug=s.slug AND jobs.external_id=s.external_id
  AND jobs.status='closed'
RETURNING jobs.ats, jobs.slug, jobs.external_id, jobs.title, jobs.url, jobs.location
"""

"""
Fill in a posted date we did not have, without calling it an edit.

posted_at is not in _HASHED, deliberately -- it must not make a posting look
edited. But that also means a row cannot acquire one later through the edit
path, because gaining a date does not change the hash, so the posting is
merely touched and the column stays null forever.

That is not hypothetical: the Workday adapter learned to read "Posted 5 Days
Ago" after 54,843 of its postings were already stored, and without this they
would have stayed dateless until each one closed and returned.

Only ever fills a null. It never overwrites a date the board previously gave
us, so a source that starts reporting something different cannot rewrite
history, and it emits nothing -- no event, no edit, no digest line.
"""
BACKFILL_POSTED = """
UPDATE jobs SET posted_at = s.posted_at
FROM staged_postings s
WHERE jobs.ats=s.ats AND jobs.slug=s.slug AND jobs.external_id=s.external_id
  AND jobs.posted_at IS NULL
  AND s.posted_at IS NOT NULL
"""

EDIT = """
UPDATE jobs SET title=s.title, location=s.location, locations_json=s.locations_json,
                url=s.url, posted_at=s.posted_at, content_hash=s.content_hash,
                region=s.region,
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

"""
Postings dropped at ingest, so a run can report what it chose not to store.
Module state because the diff is called per batch and the count belongs to
the run.
"""
_last_skipped = [0]


def skipped_count() -> int:
    return _last_skipped[0]


def reset_skipped() -> None:
    _last_skipped[0] = 0


def _reset(conn) -> None:
    conn.execute(BOARDS_DDL)
    conn.execute(POSTINGS_DDL)
    conn.execute("DELETE FROM staged_boards")
    conn.execute("DELETE FROM staged_postings")


def _stage(conn, fetched: dict[Key, list[Posting]]) -> None:
    """
    The board row carries the raw fetch count, because the guard judges the
    response and the filter judges the posting -- two different questions.
    """
    conn.executemany(
        "INSERT INTO staged_boards (ats, slug, fetched) VALUES (?,?,?) ON CONFLICT DO NOTHING",
        [(a, s, len(p)) for (a, s), p in fetched.items()],
    )
    rows = []
    skipped = 0
    for (ats, slug), postings in fetched.items():
        for p in postings:
            role = classify(p.title, p.department)
            """
            Filtered here rather than after storing, because the corpus is
            82% retail, clinical and sales work that the product never serves
            -- 725,539 postings of a 500 MB budget spent on Domino's cashiers.

            The cost is that a posting we never store can never be
            reclassified: a later ruleset that catches something this one
            missed only applies to postings arriving after it. That is
            acceptable because every live board is re-polled hourly, so a
            broadened ruleset recovers its misses within a day -- but it is a
            real trade, which is why it is a setting rather than a constant.
            """
            if config.STORE_ONLY_TECHNICAL and role.family not in config.STORE_FAMILIES:
                skipped += 1
                continue

            """
            And the same test on where the job is, for the same reason and at
            the same cost. The region policy is far more permissive than the
            family one: it rejects only a posting that names somewhere
            outside the target, and keeps every posting that simply does not
            say. A location field is optional in most ATS schemas, and 9.6%
            of the corpus leaves it empty -- refusing those would discard
            every posting from a board whose ATS never fills it in.
            """
            if not geo.in_target(p.location):
                skipped += 1
                continue

            """
            And the same test on when it was posted. Only a date the board
            actually gave us can fail this -- a posting that states no date
            is kept, exactly as one that states no location is.
            """
            if (
                config.STORE_POSTED_AFTER
                and p.posted_at
                and p.posted_at < config.STORE_POSTED_AFTER
            ):
                skipped += 1
                continue
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
                    geo.region(p.location),
                )
            )
    if skipped:
        _last_skipped[0] += skipped
    if rows:
        conn.executemany(
            """INSERT INTO staged_postings
                   (ats, slug, external_id, title, location, locations_json, url,
                    posted_at, content_hash, role_family, is_engineering, is_fde,
                    seniority, classified_by, region)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
    """
    After the three that report, because this one reports nothing: a row that
    gains a date it never had is not news to anybody.
    """
    conn.execute(BACKFILL_POSTED)

    grace = config.CLOSE_GRACE_POLLS
    for row in _rows(conn.execute(MISSING, (grace, grace, ts))):
        if row["status"] != "closed":
            continue
        key = (row["ats"], row["slug"])
        if key in result:
            result[key]["closed"].append(row)
    return result
