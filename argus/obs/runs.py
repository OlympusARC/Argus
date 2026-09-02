"""What each discovery source actually produced, run over run.

This is the orchestrator's sensory input. A policy that cannot measure cannot
decide, and until something wrote this table the only way to answer "is
discovery working?" was an afternoon of ad-hoc SQL -- which is precisely how
Common Crawl swept one host of ten for months without anyone noticing.

The distinction the schema exists to preserve: a source that found nothing,
a source that was blocked from asking, and a source that never ran are three
different facts. Collapsed into one they all read as a quiet week, and the
quiet week is the failure that hides.

Trend is deliberately median-based rather than mean-based. Discovery yields
are spiky -- a monthly HN thread, a new crawl landing -- and one good run
would drag a mean far enough that the next collapse looks normal.

Ordering is by id, not by started_at. Two runs inside the same second sort
arbitrarily by timestamp, which makes "the latest run" a coin flip -- the
same reason the notifier's watermark is an event id.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass

"""
A run has to fall this far below its own history to count as collapsed. Set
by the case it exists to catch: Common Crawl went from 13,635 new boards to
0, and Ashby-only Wayback sits at a tenth of what widening it would yield.
"""
COLLAPSE_RATIO = 0.2

"""
Fewer runs than this and there is no history to judge against, so nothing is
ever called a collapse -- a new source's first quiet run is not a regression.
"""
MIN_HISTORY = 3


@dataclass
class Trend:
    source: str
    latest: int = 0
    median: float = 0.0
    runs: int = 0
    blocked: int = 0
    collapsed: bool = False
    reason: str = ""

    @property
    def ratio(self) -> float:
        return self.latest / self.median if self.median else 1.0

    @property
    def arrow(self) -> str:
        if self.collapsed:
            return "!"
        if self.runs < MIN_HISTORY:
            return "-"
        if self.ratio >= 1.5:
            return "^"
        if self.ratio <= 0.5:
            return "v"
        return "="


def now() -> int:
    return int(time.time())


def start(conn, source: str) -> int:
    from ..core.db import insert_id

    return insert_id(
        conn, "INSERT INTO source_runs (source, started_at) VALUES (?, ?)", (source, now())
    )


def finish(conn, run_id: int, result) -> None:
    """Close a run out from a SourceResult.

    Takes the result object rather than a dozen arguments so that adding a
    field to SourceResult does not mean editing every caller -- there is only
    one caller, and it is inside discovery.run().
    """
    conn.execute(
        """UPDATE source_runs
           SET finished_at=?, refs_seen=?, new_boards=?, new_companies=?,
               seed_postings=?, blocked=?, error=?, skipped=?
           WHERE id=?""",
        (
            now(),
            getattr(result, "refs_seen", 0) or getattr(result, "dry_refs", 0),
            getattr(result, "new_boards", 0),
            getattr(result, "new_companies", 0),
            getattr(result, "postings", 0),
            getattr(result, "blocked", 0),
            getattr(result, "error", None),
            getattr(result, "skipped", None),
            run_id,
        ),
    )
    conn.commit()


def trend(conn, source: str, window: int = 10) -> Trend:
    """Compare the newest completed run against its own recent history.

    Skipped runs are excluded: a source without an API key reports zero every
    night, and letting those into the median would make a genuine collapse
    look like business as usual.
    """
    rows = conn.execute(
        """SELECT new_boards, blocked, refs_seen FROM source_runs
           WHERE source=? AND finished_at IS NOT NULL AND skipped IS NULL
           ORDER BY id DESC LIMIT ?""",
        (source, window),
    ).fetchall()
    if not rows:
        return Trend(source)

    latest = int(rows[0]["new_boards"] or 0)
    blocked = int(rows[0]["blocked"] or 0)
    refs = int(rows[0]["refs_seen"] or 0)
    history = [int(r["new_boards"] or 0) for r in rows[1:]]
    med = statistics.median(history) if history else 0.0
    t = Trend(source, latest=latest, median=med, runs=len(rows), blocked=blocked)

    """
    Blocked-with-nothing-to-show is a collapse on its own evidence: the source
    did not decline to find boards, it was prevented from looking. That needs
    no history at all.
    """
    if blocked and refs == 0:
        t.collapsed, t.reason = True, f"blocked {blocked}x, returned nothing"
    elif len(rows) >= MIN_HISTORY and med > 0 and latest < COLLAPSE_RATIO * med:
        t.collapsed = True
        t.reason = f"{latest} new vs median {med:.0f}"
    return t


def latest(conn, source: str) -> dict | None:
    row = conn.execute(
        """SELECT * FROM source_runs WHERE source=?
           ORDER BY id DESC LIMIT 1""",
        (source,),
    ).fetchone()
    return dict(row) if row else None


def all_trends(conn, window: int = 10) -> list[Trend]:
    names = [
        r["source"]
        for r in conn.execute("SELECT DISTINCT source FROM source_runs ORDER BY source")
    ]
    return [trend(conn, n, window) for n in names]


def collapsed(conn) -> list[Trend]:
    """What the orchestrator's rule 2 asks for."""
    return [t for t in all_trends(conn) if t.collapsed]
