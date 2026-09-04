"""The set-diff loop: compare a board against what we last saw and emit events.

This is the product. Everything else exists to hand this function a list of
boards worth polling.

Three rules earn their complexity:

  * A failed poll never closes anything. Absence only means "gone" when we
    actually got an answer; otherwise a network blip would close a whole board.
  * A posting must be absent from several consecutive *successful* polls before
    it closes. Full-board endpoints are near-authoritative but not perfectly so.
  * A board that had many open jobs and suddenly returns none is treated as a
    bad response, not a mass layoff. That one guard is the difference between
    a wrong answer and a catastrophic one.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .. import adapters
from ..core import config, db
from ..core.models import FetchError, Posting
from ..registry import boards as registry
from . import diff, jobs


@dataclass
class BoardResult:
    ats: str
    slug: str
    ok: bool = False
    present: int = 0
    new: int = 0
    edited: int = 0
    closed: int = 0
    reopened: int = 0
    suspicious: bool = False
    error: str | None = None


@dataclass
class RunSummary:
    """
    `ats` is on the summary rather than left to the caller because the line
    is read in CI, where eight of these appear in sequence with nothing to
    tell them apart. Decoding them meant knowing the loop order in the
    workflow file.
    """

    ats: str = ""
    boards: int = 0
    failed: int = 0
    new: int = 0
    edited: int = 0
    closed: int = 0
    reopened: int = 0
    suspicious: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def line(self) -> str:
        who = f"{self.ats:<16}" if self.ats else ""
        rate = f"  {self.boards / self.seconds:.1f}/s" if self.seconds > 0 else ""
        return (
            f"{who}{self.boards:,} boards  {self.failed:,} failed  "
            f"+{self.new:,} new  ~{self.edited:,} edited  "
            f"-{self.closed:,} closed  ^{self.reopened:,} reopened  "
            f"in {self.seconds:.0f}s{rate}"
        )


def _events_from(ats: str, slug: str, changes: dict) -> list[tuple]:
    events: list[tuple] = []
    for kind in ("new", "reopened", "edited"):
        for row in changes[kind]:
            events.append(
                (
                    kind,
                    ats,
                    slug,
                    row["external_id"],
                    row["title"],
                    row["url"],
                    {"location": row["location"]},
                )
            )
    for row in changes["closed"]:
        events.append(
            (
                "closed",
                ats,
                slug,
                row["external_id"],
                row["title"],
                row["url"],
                {"location": row["location"], "missing_polls": row["missing_polls"]},
            )
        )
    return events


def apply_batch(
    conn: sqlite3.Connection, fetched: dict[tuple[str, str], list[Posting]]
) -> dict[tuple[str, str], BoardResult]:
    """Diff a batch of boards in one pass and emit their events.

    The batch is the unit because the diff costs seven statements however many
    boards it settles. Per board that was slower than what it replaced -- 400
    boards took 384 seconds, which is 85 minutes for a full tier-1 sweep.
    """
    changed = diff.run_batch(conn, fetched)
    events: list[tuple] = []
    out: dict[tuple[str, str], BoardResult] = {}
    for (ats, slug), postings in fetched.items():
        c = changed[(ats, slug)]
        res = BoardResult(ats, slug, ok=True, present=len(postings))
        if c["suspicious"]:
            res.suspicious = True
            out[(ats, slug)] = res
            continue
        res.new = len(c["new"])
        res.reopened = len(c["reopened"])
        res.edited = len(c["edited"])
        res.closed = len(c["closed"])
        events.extend(_events_from(ats, slug, c))
        out[(ats, slug)] = res
    jobs.record_events(conn, events)
    return out


def apply_board(
    conn: sqlite3.Connection, ats: str, slug: str, postings: list[Posting]
) -> BoardResult:
    """Diff a single board. A batch of one, so both paths share the rules."""
    return apply_batch(conn, {(ats, slug): postings})[(ats, slug)]


def _fetch(adapter, slug: str):
    try:
        return slug, adapter.fetch(slug), None
    except FetchError as exc:
        return slug, None, exc


def run(
    conn: sqlite3.Connection,
    ats: str,
    *,
    limit: int | None = None,
    workers: int | None = None,
    force: bool = False,
    progress_every: int = 250,
    batch: int = 100,
) -> RunSummary:
    adapter = adapters.get(ats)
    if adapter is None:
        raise SystemExit(f"no adapter for {ats!r}; supported: {adapters.supported()}")

    due = registry.due(conn, ats=ats, limit=limit, force=force)
    summary = RunSummary(ats=ats)
    if not due:
        return summary

    slugs = [r["slug"] for r in due]
    started = int(time.time())
    run_id = db.insert_id(conn, "INSERT INTO poll_runs (started_at) VALUES (?)", (started,))
    t0 = time.time()

    """
    Fetches run concurrently; every database write happens here on the main
    thread. Keeps SQLite single-writer and the diff logic free of locking.

    Results are accumulated into batches before being written, because the
    diff costs a fixed seven statements whether it settles one board or a
    hundred. Board-at-a-time made a 400-board poll take 384 seconds against a
    remote database; batching makes that cost proportional to the number of
    batches instead.

    The batch is bounded by postings as well as boards, because the two are
    not proportional: Workday averages 183 open postings per board against
    Ashby's 17, and one board holds 20,598. A hundred busy Workday boards
    stages 181,401 rows -- a third of the corpus in one statement -- and the
    EDIT update then exceeds Postgres's two-minute statement timeout, which
    is what broke the hourly poll. Counting boards measured the wrong thing;
    the cost was always in the rows.
    """
    pending: dict[tuple[str, str], list[Posting]] = {}
    pending_postings = 0

    def flush() -> None:
        nonlocal pending, pending_postings
        if not pending:
            return
        for (b_ats, b_slug), res in apply_batch(conn, pending).items():
            if res.suspicious:
                summary.suspicious.append(b_slug)
            summary.new += res.new
            summary.edited += res.edited
            summary.closed += res.closed
            summary.reopened += res.reopened
            registry.mark_ok(conn, b_ats, b_slug, res.present, had_new=bool(res.new))
        pending = {}
        pending_postings = 0

    with ThreadPoolExecutor(max_workers=workers or config.WORKERS) as pool:
        for slug, postings, err in pool.map(lambda s: _fetch(adapter, s), slugs):
            summary.boards += 1
            if err is not None:
                registry.mark_failure(conn, ats, slug, str(err), permanent=err.permanent)
                summary.failed += 1
            else:
                """
                Flush before adding when this board would push the batch past
                the row budget, so a single enormous board goes in a batch of
                its own rather than dragging a full one over the limit. A
                board is never split: the diff's guards reason about a whole
                board's contents, and half a board looks exactly like a
                mass deletion.
                """
                if pending and pending_postings + len(postings) > config.BATCH_POSTINGS:
                    flush()
                pending[(ats, slug)] = postings
                pending_postings += len(postings)
                if len(pending) >= batch or pending_postings >= config.BATCH_POSTINGS:
                    flush()
            if progress_every and summary.boards % progress_every == 0:
                rate = summary.boards / max(time.time() - t0, 1e-9)
                print(
                    f"    {summary.boards:,}/{len(slugs):,}  {rate:.0f}/s  "
                    f"+{summary.new:,} new",
                    file=sys.stderr,
                    flush=True,
                )
    flush()

    summary.seconds = time.time() - t0
    conn.execute(
        """UPDATE poll_runs SET finished_at=?, boards_polled=?, boards_failed=?,
                                new_jobs=?, edited_jobs=?, closed_jobs=? WHERE id=?""",
        (
            int(time.time()),
            summary.boards,
            summary.failed,
            summary.new,
            summary.edited,
            summary.closed,
            run_id,
        ),
    )
    return summary
