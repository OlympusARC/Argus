"""Board registry operations: everything that writes to `boards`."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable

from ..core import config
from ..core.models import BoardRef


def now() -> int:
    return int(time.time())


def add_boards(
    conn: sqlite3.Connection, refs: Iterable[BoardRef], source: str
) -> dict[str, int]:
    """Insert-or-annotate. Never downgrades an existing board's status.

    A board already marked dead stays dead even if rediscovered; only an
    explicit re-validation can revive it. Otherwise a noisy source would keep
    resurrecting slugs we have already proven do not exist.
    """
    """
    Batched, because this is the hot path of every discovery source and the
    work is not the work -- it is the latency. Per ref this did a SELECT, an
    INSERT and another INSERT: three round trips to Postgres at about 90ms
    each, measured at 0.27s per ref. One job-list repo holds 19,466 rows and
    the source reads eleven of them, so simplify alone projected to 25 hours
    of waiting for a few seconds of computation.

    Four statements per batch instead of three per row. The two SELECTs
    exist because the counts are the point: a source is judged on how many
    boards it contributed that nobody had, so `new_boards` has to distinguish
    an insert from a no-op upsert, and ON CONFLICT does not report which
    happened.
    """
    ts = now()
    items = list(refs)
    seen = len(items)
    if not items:
        return {"seen": 0, "new_boards": 0, "new_links": 0}

    """
    A batch can name the same board twice -- two repos listing one company --
    and counting it twice would overstate the yield.
    """
    by_key: dict[tuple[str, str], object] = {}
    for ref in items:
        by_key.setdefault((ref.ats, ref.slug), ref)
    keys = list(by_key)
    flat = [x for k in keys for x in k]
    ph = ",".join("(?,?)" for _ in keys)

    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        existing = {
            (r["ats"], r["slug"])
            for r in cur.execute(
                f"SELECT ats, slug FROM boards WHERE (ats, slug) IN ({ph})", tuple(flat)
            ).fetchall()
        }
        linked = {
            (r["ats"], r["slug"])
            for r in cur.execute(
                f"""SELECT ats, slug FROM board_sources
                    WHERE source = ? AND (ats, slug) IN ({ph})""",
                (source, *flat),
            ).fetchall()
        }
        added = sum(1 for k in keys if k not in existing)
        links = sum(1 for k in keys if k not in linked)

        cur.executemany(
            """INSERT INTO boards (ats, slug, company_name, status, tier,
                                   next_poll_at, first_seen_at, website, careers_url)
               VALUES (?, ?, ?, 'unvalidated', ?, ?, ?, ?, ?)
               ON CONFLICT(ats, slug) DO UPDATE SET
                   company_name = COALESCE(boards.company_name, excluded.company_name),
                   website      = COALESCE(boards.website,      excluded.website),
                   careers_url  = COALESCE(boards.careers_url,  excluded.careers_url)""",
            [
                (
                    r.ats,
                    r.slug,
                    r.company_name,
                    config.DEFAULT_TIER,
                    ts,
                    ts,
                    r.website,
                    r.careers_url,
                )
                for r in by_key.values()
            ],
        )
        cur.executemany(
            """INSERT INTO board_sources (ats, slug, source, first_seen_at, detail)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (ats, slug, source) DO NOTHING""",
            [
                (r.ats, r.slug, source, ts, json.dumps(r.detail) if r.detail else None)
                for r in by_key.values()
            ],
        )
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise
    return {"seen": seen, "new_boards": added, "new_links": links}


def counts_by_ats(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT ats, status, COUNT(*) AS n FROM boards
           GROUP BY ats, status ORDER BY ats, status"""
    ).fetchall()


def counts_by_source(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT bs.source,
                  COUNT(*) AS boards,
                  SUM(CASE WHEN b.status='active' THEN 1 ELSE 0 END) AS active,
                  SUM(CASE WHEN b.status='dead'   THEN 1 ELSE 0 END) AS dead
           FROM board_sources bs JOIN boards b USING (ats, slug)
           GROUP BY bs.source ORDER BY boards DESC"""
    ).fetchall()


def unvalidated(conn: sqlite3.Connection, ats: str | None = None, limit: int | None = None):
    q = "SELECT ats, slug FROM boards WHERE status='unvalidated'"
    args: list = []
    if ats:
        q += " AND ats = ?"
        args.append(ats)
    q += " ORDER BY first_seen_at"
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q, args).fetchall()


def due(
    conn: sqlite3.Connection,
    ats: str | None = None,
    limit: int | None = None,
    force: bool = False,
):
    """Boards eligible for a poll right now; force ignores the schedule."""
    q = """SELECT ats, slug, tier, consecutive_failures FROM boards
           WHERE status IN ('active','empty')"""
    args: list = []
    if not force:
        q += " AND (next_poll_at IS NULL OR next_poll_at <= ?)"
        args.append(now())
    if ats:
        q += " AND ats = ?"
        args.append(ats)
    q += " ORDER BY next_poll_at IS NULL DESC, next_poll_at ASC"
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q, args).fetchall()


def mark_ok(
    conn: sqlite3.Connection, ats: str, slug: str, job_count: int, *, had_new: bool = False
):
    ts = now()
    row = conn.execute("SELECT tier FROM boards WHERE ats=? AND slug=?", (ats, slug)).fetchone()
    tier = row["tier"] if row else config.DEFAULT_TIER
    status = "active" if job_count > 0 else "empty"
    conn.execute(
        """UPDATE boards SET status=?, job_count=?, last_polled_at=?, last_ok_at=?,
                             consecutive_failures=0, last_error=NULL,
                             next_poll_at=?,
                             last_new_at=CASE WHEN ? THEN ? ELSE last_new_at END
           WHERE ats=? AND slug=?""",
        (
            status,
            job_count,
            ts,
            ts,
            ts + config.TIER_INTERVALS.get(tier, config.TIER_INTERVALS[1]),
            # A real bool, not 1/0: SQLite treats any non-zero as true, but
            # Postgres requires CASE WHEN to be given a boolean and rejects a
            # smallint outright.
            bool(had_new),
            ts,
            ats,
            slug,
        ),
    )


def mark_failure(
    conn: sqlite3.Connection, ats: str, slug: str, error: str, *, permanent: bool = False
):
    ts = now()
    row = conn.execute(
        "SELECT consecutive_failures FROM boards WHERE ats=? AND slug=?", (ats, slug)
    ).fetchone()
    fails = (row["consecutive_failures"] if row else 0) + 1
    if permanent or fails >= config.DEAD_AFTER_FAILURES:
        conn.execute(
            """UPDATE boards SET status='dead', consecutive_failures=?, last_error=?,
                                 last_polled_at=?, next_poll_at=NULL
               WHERE ats=? AND slug=?""",
            (fails, error[:500], ts, ats, slug),
        )
        return "dead"
    delay = min(config.BACKOFF_BASE * (2 ** (fails - 1)), config.BACKOFF_MAX)
    conn.execute(
        """UPDATE boards SET consecutive_failures=?, last_error=?, last_polled_at=?, next_poll_at=?
           WHERE ats=? AND slug=?""",
        (fails, error[:500], ts, ts + delay, ats, slug),
    )
    return "backoff"


def retier(conn: sqlite3.Connection) -> int:
    """Demote boards that have gone quiet. This is what makes 10k boards cheap."""
    cutoff = now() - config.QUIET_DEMOTE_AFTER
    cur = conn.execute(
        """UPDATE boards SET tier = MIN(tier + 1, 3)
           WHERE status IN ('active','empty') AND tier < 3
             AND COALESCE(last_new_at, first_seen_at) < ?""",
        (cutoff,),
    )
    return cur.rowcount
