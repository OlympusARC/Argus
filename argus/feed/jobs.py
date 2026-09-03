"""Writes to the `jobs` table. Owned by the reconciler; seeding is the one
exception, and seeded rows are tagged so they can be told apart from polled
ones until a real poll confirms them.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable

from ..classify import classify
from ..core.models import Posting


def now() -> int:
    return int(time.time())


def _locations(p: Posting) -> str | None:
    """Only store the list when it says something `location` does not.

    96% of postings carry a single location that is byte-identical to the
    scalar column beside it. Storing the array anyway cost 8 MB to repeat
    ourselves; the reader falls back to `location` when this is NULL.
    """
    if not p.locations or len(p.locations) <= 1:
        return None
    return json.dumps(p.locations)


def _row(p: Posting, ts: int, source: str) -> tuple:
    role = classify(p.title, p.department)
    return (
        p.ats,
        p.slug,
        p.external_id,
        p.title,
        p.location,
        _locations(p),
        p.url,
        p.posted_at,
        ts,
        ts,
        p.content_hash(),
        source,
        role.family,
        int(role.is_engineering),
        int(role.is_fde),
        role.seniority,
        role.ruleset,
    )


INSERT = """
INSERT INTO jobs (ats, slug, external_id, title, location, locations_json,
                  url, posted_at,
                  first_seen_at, last_seen_at, content_hash, source,
                  role_family, is_engineering, is_fde, seniority, classified_by)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

"""
Seeding must never disturb a row the poller already owns: a polled row is
more trustworthy than a seeded one. ON CONFLICT DO NOTHING says that in a
dialect both SQLite and Postgres understand, where OR IGNORE is SQLite-only.
"""
SEED_CONFLICT = " ON CONFLICT (ats, slug, external_id) DO NOTHING"


def seed(conn: sqlite3.Connection, postings: Iterable[Posting], source: str) -> int:
    """Insert postings we learned about from a discovery source.

    Existing rows are left completely alone: a polled row is always more
    trustworthy than a seeded one, and re-seeding must never reset last_seen_at
    or it would keep a dead posting alive forever.
    """
    ts = now()
    rows = [_row(p, ts, source) for p in postings]
    if not rows:
        return 0

    """
    One statement for the batch. Per row this was a round trip each, and
    simplify alone seeds 2,172 postings a run -- measured at 195 seconds of
    the source's 263, all of it latency rather than work.

    Counting them needs its own query, because executemany does not report a
    usable per-statement rowcount and ON CONFLICT DO NOTHING makes the total
    ambiguous anyway. Asking which keys already exist is one round trip and
    exact, where cur.rowcount was neither.
    """
    keys = [(r[0], r[1], r[2]) for r in rows]
    flat = [x for k in keys for x in k]
    ph = ",".join("(?,?,?)" for _ in keys)

    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        before = {
            (x["ats"], x["slug"], x["external_id"])
            for x in cur.execute(
                f"""SELECT ats, slug, external_id FROM jobs
                    WHERE (ats, slug, external_id) IN ({ph})""",
                tuple(flat),
            ).fetchall()
        }
        cur.executemany(INSERT + SEED_CONFLICT, rows)
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise
    return sum(1 for k in set(keys) if k not in before)


def open_ids(conn: sqlite3.Connection, ats: str, slug: str) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        """SELECT external_id, content_hash, status, missing_polls
           FROM jobs WHERE ats=? AND slug=? AND status='open'""",
        (ats, slug),
    ).fetchall()
    return {r["external_id"]: r for r in rows}


def record_event(
    conn: sqlite3.Connection,
    kind: str,
    p_ats: str,
    p_slug: str,
    ext: str | None,
    title: str | None,
    url: str | None,
    detail: dict | None = None,
) -> None:
    conn.execute(
        """INSERT INTO events (ts, type, ats, slug, external_id, title, url, detail_json)
           VALUES (?,?,?,?,?,?,?,?)""",
        (now(), kind, p_ats, p_slug, ext, title, url, json.dumps(detail) if detail else None),
    )


def board_state(conn: sqlite3.Connection, ats: str, slug: str) -> dict[str, sqlite3.Row]:
    """Every posting we have ever seen on this board, open or closed.

    Closed rows are included so a reappearing posting is recognised as a reopen
    rather than inserted again under the same primary key.

    Only the four columns the diff actually needs. title, url and location used
    to come along for every row so that a close event could name the posting --
    96 bytes of url each, to serve the handful that actually close. Those are
    read back from the close itself now, which is most of this query's cost on
    a remote database.
    """
    rows = conn.execute(
        """SELECT external_id, content_hash, status, missing_polls
           FROM jobs WHERE ats=? AND slug=?""",
        (ats, slug),
    ).fetchall()
    return {r["external_id"]: r for r in rows}


def insert(conn: sqlite3.Connection, p: Posting, source: str = "poll") -> None:
    conn.execute(INSERT, _row(p, now(), source))


def update(conn: sqlite3.Connection, p: Posting) -> None:
    """Refresh a posting whose content hash moved.

    Re-classifies rather than carrying the old family forward: a retitled
    posting is exactly the case where the family can change, and a stale
    role_family left behind would be invisible.
    """
    role = classify(p.title, p.department)
    conn.execute(
        """UPDATE jobs SET title=?, location=?, locations_json=?, url=?, posted_at=?,
                           last_seen_at=?, missing_polls=0, status='open', closed_at=NULL,
                           content_hash=?, role_family=?, is_engineering=?, is_fde=?,
                           seniority=?, classified_by=?
           WHERE ats=? AND slug=? AND external_id=?""",
        (
            p.title,
            p.location,
            _locations(p),
            p.url,
            p.posted_at,
            now(),
            p.content_hash(),
            role.family,
            int(role.is_engineering),
            int(role.is_fde),
            role.seniority,
            role.ruleset,
            p.ats,
            p.slug,
            p.external_id,
        ),
    )


def reclassify(conn: sqlite3.Connection, batch: int = 20_000) -> dict[str, int]:
    """Re-run the ruleset over every row it did not produce.

    This is what makes the rules improvable. Raise RULESET, run this, and only
    rows that disagree are touched -- no board is re-polled, and postings that
    have since closed keep a classification rather than losing one.
    """
    from ..classify import RULESET

    seen = changed = 0
    while True:
        rows = conn.execute(
            """SELECT ats, slug, external_id, title, role_family FROM jobs
               WHERE classified_by IS NULL OR classified_by <> ? LIMIT ?""",
            (RULESET, batch),
        ).fetchall()
        if not rows:
            break
        """
        One statement for the batch, not one per row. Classification is pure
        CPU and takes microseconds; the update is a network round trip to
        Postgres and takes milliseconds, so a per-row UPDATE made the sweep
        entirely latency -- 182,630 round trips, measured at four and a half
        hours for a job whose actual work is seconds.
        """
        params = []
        for r in rows:
            role = classify(r["title"])
            seen += 1
            changed += role.family != r["role_family"]
            params.append(
                (
                    role.family,
                    int(role.is_engineering),
                    int(role.is_fde),
                    role.seniority,
                    role.ruleset,
                    r["ats"],
                    r["slug"],
                    r["external_id"],
                )
            )

        conn.execute("BEGIN")
        try:
            conn.executemany(
                """UPDATE jobs SET role_family=?, is_engineering=?, is_fde=?,
                                   seniority=?, classified_by=?
                   WHERE ats=? AND slug=? AND external_id=?""",
                params,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {"classified": seen, "family_changed": changed}


def touch(conn: sqlite3.Connection, ats: str, slug: str, external_id: str) -> None:
    """Unchanged and still listed: only the liveness columns move."""
    conn.execute(
        """UPDATE jobs SET last_seen_at=?, missing_polls=0 WHERE ats=? AND slug=? AND external_id=?""",
        (now(), ats, slug, external_id),
    )


def mark_missing(
    conn: sqlite3.Connection, ats: str, slug: str, external_id: str, count: int
) -> None:
    conn.execute(
        "UPDATE jobs SET missing_polls=? WHERE ats=? AND slug=? AND external_id=?",
        (count, ats, slug, external_id),
    )


def close(conn: sqlite3.Connection, ats: str, slug: str, external_id: str) -> None:
    conn.execute(
        """UPDATE jobs SET status='closed', closed_at=? WHERE ats=? AND slug=? AND external_id=?""",
        (now(), ats, slug, external_id),
    )


def reopen(conn: sqlite3.Connection, p: Posting) -> None:
    update(conn, p)  # update() already clears closed_at and status


"""
Batched writes.

One round trip per posting is free against a local file and ruinous against a
network: eight boards and 847 postings took 151 seconds on Postgres, because
each insert and each event was its own round trip. These do the same work in
one statement each, which is what makes an hourly poll of thousands of boards
possible at all.

The diff itself is unchanged and still decided in Python -- these only change
how its conclusions are written.
"""


def insert_many(conn, postings: list[Posting], source: str = "poll") -> None:
    if postings:
        ts = now()
        conn.executemany(INSERT, [_row(p, ts, source) for p in postings])


def update_many(conn, postings: list[Posting]) -> None:
    """Refresh postings whose content hash moved, re-classifying each."""
    if not postings:
        return
    ts = now()
    rows = []
    for p in postings:
        role = classify(p.title, p.department)
        rows.append(
            (
                p.title,
                p.location,
                _locations(p),
                p.url,
                p.posted_at,
                ts,
                p.content_hash(),
                role.family,
                int(role.is_engineering),
                int(role.is_fde),
                role.seniority,
                role.ruleset,
                p.ats,
                p.slug,
                p.external_id,
            )
        )
    conn.executemany(
        """UPDATE jobs SET title=?, location=?, locations_json=?, url=?, posted_at=?,
                           last_seen_at=?, missing_polls=0, status='open', closed_at=NULL,
                           content_hash=?, role_family=?, is_engineering=?, is_fde=?,
                           seniority=?, classified_by=?
           WHERE ats=? AND slug=? AND external_id=?""",
        rows,
    )


def touch_many(conn, ats: str, slug: str, ids: list[str]) -> None:
    """Unchanged and still listed: only the liveness columns move."""
    if ids:
        ts = now()
        conn.executemany(
            """UPDATE jobs SET last_seen_at=?, missing_polls=0
               WHERE ats=? AND slug=? AND external_id=?""",
            [(ts, ats, slug, i) for i in ids],
        )


def mark_missing_many(conn, ats: str, slug: str, counts: dict[str, int]) -> None:
    if counts:
        conn.executemany(
            "UPDATE jobs SET missing_polls=? WHERE ats=? AND slug=? AND external_id=?",
            [(n, ats, slug, i) for i, n in counts.items()],
        )


def close_many(conn, ats: str, slug: str, ids: list[str]) -> list[dict]:
    """Close postings and return what they were, for the events.

    The title and url come back from the close rather than being carried
    along by board_state for every row on the board.
    """
    if not ids:
        return []
    ts = now()
    conn.executemany(
        """UPDATE jobs SET status='closed', closed_at=?
           WHERE ats=? AND slug=? AND external_id=?""",
        [(ts, ats, slug, i) for i in ids],
    )
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""SELECT external_id, title, url, location, missing_polls FROM jobs
            WHERE ats=? AND slug=? AND external_id IN ({placeholders})""",
        (ats, slug, *ids),
    ).fetchall()
    return [dict(r) for r in rows]


def record_events(conn, rows: list[tuple]) -> None:
    """rows: (kind, ats, slug, external_id, title, url, detail dict or None)."""
    if not rows:
        return
    ts = now()
    conn.executemany(
        """INSERT INTO events (ts, type, ats, slug, external_id, title, url, detail_json)
           VALUES (?,?,?,?,?,?,?,?)""",
        [
            (ts, kind, a, s, e, t, u, json.dumps(d) if d else None)
            for kind, a, s, e, t, u, d in rows
        ],
    )
