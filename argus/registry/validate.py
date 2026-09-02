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
    checked: int = 0
    active: int = 0
    empty: int = 0
    dead: int = 0
    errored: int = 0
    jobs: int = 0

    def line(self) -> str:
        return (
            f"checked {self.checked:,}  active {self.active:,}  empty {self.empty:,}  "
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
    res = Result()
    if not slugs:
        return res

    t0 = time.time()
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
            if progress_every and res.checked % progress_every == 0:
                rate = res.checked / max(time.time() - t0, 1e-9)
                eta = (len(slugs) - res.checked) / max(rate, 1e-9)
                print(
                    f"    {res.checked:,}/{len(slugs):,}  {rate:.0f}/s  "
                    f"eta {eta / 60:.1f}m  ({res.active:,} active)",
                    file=sys.stderr,
                    flush=True,
                )
    return res
