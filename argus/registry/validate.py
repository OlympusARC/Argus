"""Probe unvalidated boards and settle their status.

This is the only place that decides whether a discovered slug is real, which is
why discovery sources are free to be noisy. One GET per board, concurrency
capped by http.host_slot so we stay a polite client.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .. import adapters
from ..core import config
from ..core.models import FetchError
from . import boards as registry


@dataclass
class Result:
    """
    `ats` is on the result because these run one per ATS and their lines are
    read side by side in CI. Without it four identical progress lines say
    only that something is happening.
    """

    ats: str = ""
    checked: int = 0
    active: int = 0
    empty: int = 0
    dead: int = 0
    errored: int = 0
    jobs: int = 0

    def line(self) -> str:
        who = f"{self.ats:<16}" if self.ats else ""
        return (
            f"{who}checked {self.checked:,}  active {self.active:,}  empty {self.empty:,}  "
            f"dead {self.dead:,}  errors {self.errored:,}  jobs {self.jobs:,}"
        )


def _probe(adapter, slug: str):
    try:
        return slug, adapter.count(slug), None
    except FetchError as exc:
        return slug, None, exc


def run(
    conn: sqlite3.Connection,
    ats: str,
    *,
    limit: int | None = None,
    workers: int | None = None,
    revalidate: bool = False,
    progress_every: int = 250,
) -> Result:
    adapter = adapters.get(ats)
    if adapter is None:
        raise SystemExit(f"no adapter for {ats!r}; supported: {adapters.supported()}")

    if revalidate:
        """
        Companies adopt Ashby after we first looked. Without this sweep the
        registry only ever decays.
        """
        q = "SELECT slug FROM boards WHERE ats=? AND status='dead' ORDER BY last_polled_at"
        rows = conn.execute(q + (f" LIMIT {int(limit)}" if limit else ""), (ats,)).fetchall()
    else:
        rows = registry.unvalidated(conn, ats=ats, limit=limit)
    slugs = [r["slug"] for r in rows]
    res = Result(ats=ats)
    if not slugs:
        return res

    if progress_every:
        print(
            f"{ats:<16}start   {len(slugs):,} boards to probe"
            f"{'  (revalidating dead)' if revalidate else ''}",
            file=sys.stderr,
            flush=True,
        )
    t0 = time.time()
    """
    On boards *or* elapsed time, whichever comes first. A count alone leaves
    a slow ATS silent -- the same reason poll reports this way.
    """
    last_report = [time.time()]
    with ThreadPoolExecutor(max_workers=workers or config.WORKERS) as pool:
        for slug, count, err in pool.map(lambda s: _probe(adapter, s), slugs):
            res.checked += 1
            if err is None:
                registry.mark_ok(conn, ats, slug, count)
                res.jobs += count
                if count:
                    res.active += 1
                else:
                    res.empty += 1
            elif err.permanent:
                registry.mark_failure(conn, ats, slug, str(err), permanent=True)
                res.dead += 1
            else:
                registry.mark_failure(conn, ats, slug, str(err))
                res.errored += 1
            due = progress_every and res.checked % progress_every == 0
            overdue = progress_every and time.time() - last_report[0] >= 30
            if progress_every and (due or overdue):
                last_report[0] = time.time()
                rate = res.checked / max(time.time() - t0, 1e-9)
                left = (len(slugs) - res.checked) / max(rate, 1e-9)
                print(
                    f"{ats:<16}{res.checked:,}/{len(slugs):,}  {rate:.1f}/s  "
                    f"{res.active:,} active  {res.empty:,} empty  {res.dead:,} dead  "
                    f"eta {left / 60:.0f}m",
                    file=sys.stderr,
                    flush=True,
                )
    return res
