"""Careers-page operations on the registry.

Two jobs, both about the fact that a board slug is a fragile identifier while a
company's careers page is a durable one:

  backfill -- find and store the careers page for boards that lack one
  recheck  -- re-probe stored careers pages to catch ATS migrations

A dead Ashby slug and a company that stopped hiring look identical from the ATS
alone. The careers page tells them apart: if it now links to Greenhouse, the
company migrated and we should follow it rather than lose them.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..core import config
from ..core.models import BoardRef
from ..discovery.probe import MAX_ATTRIBUTABLE_REFS, domain_of, probe
from ..feed import jobs as jobs_mod

"""
Ordered by prior likelihood for a tech company.
"""
TLDS = (".com", ".io", ".ai", ".co", ".dev", ".app")
_CLEAN = re.compile(r"[^a-z0-9]+")


def candidate_domains(
    slug: str, company_name: str | None, website: str | None, max_guesses: int = 3
) -> list[str]:
    """Domains worth probing for a board, best first."""
    out: list[str] = []
    known = domain_of(website or "")
    if known:
        out.append(known)
    bases: list[str] = []
    if company_name:
        base = _CLEAN.sub("", company_name.lower())
        if 2 < len(base) < 30:
            bases.append(base)
    slug_base = _CLEAN.sub("", slug.lower())
    if 2 < len(slug_base) < 30 and slug_base not in bases:
        bases.append(slug_base)
    for base in bases:
        for tld in TLDS[:max_guesses]:
            cand = base + tld
            if cand not in out:
                out.append(cand)
    return out


@dataclass
class Report:
    checked: int = 0
    found: int = 0
    confirmed: int = 0
    migrated: int = 0
    gone: int = 0
    new_boards: list[BoardRef] = field(default_factory=list)


def _rows_needing_backfill(conn, ats, limit):
    q = (
        "SELECT ats, slug, company_name, website FROM boards "
        "WHERE careers_url IS NULL AND status IN ('active','empty')"
    )
    args = []
    if ats:
        q += " AND ats = ?"
        args.append(ats)
    q += " ORDER BY job_count DESC"  # busiest companies first
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q, args).fetchall()


def backfill(
    conn: sqlite3.Connection,
    *,
    ats: str | None = None,
    limit: int | None = None,
    workers: int | None = None,
    max_guesses: int = 3,
    progress_every: int = 200,
) -> Report:
    rows = _rows_needing_backfill(conn, ats, limit)
    rep = Report()
    if not rows:
        return rep

    def work(row):
        for dom in candidate_domains(
            row["slug"], row["company_name"], row["website"], max_guesses
        ):
            url, refs = probe(dom)
            if not url:
                continue
            """
            Only trust a guessed domain when the page links back to THIS
            board. Without that check a slug like "alan" would happily
            attach some unrelated alan.com as the company's careers page.
            """
            if any(r.ats == row["ats"] and r.slug == row["slug"] for r in refs):
                return row, dom, url, refs
        return row, None, None, []

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers or config.WORKERS) as pool:
        for row, dom, url, refs in pool.map(work, rows):
            rep.checked += 1
            if url:
                conn.execute(
                    "UPDATE boards SET website=?, careers_url=?, careers_checked_at=? "
                    "WHERE ats=? AND slug=?",
                    (f"https://{dom}", url, int(time.time()), row["ats"], row["slug"]),
                )
                rep.found += 1
                owned = len(refs) <= MAX_ATTRIBUTABLE_REFS
                for r in refs:
                    if (r.ats, r.slug) != (row["ats"], row["slug"]):
                        rep.new_boards.append(
                            BoardRef(
                                r.ats,
                                r.slug,
                                row["company_name"] if owned else None,
                                "careers",
                                {"via": row["slug"], "aggregator": not owned},
                                website=f"https://{dom}" if owned else None,
                                careers_url=url if owned else None,
                            )
                        )
            if progress_every and rep.checked % progress_every == 0:
                rate = rep.checked / max(time.time() - t0, 1e-9)
                print(
                    f"    {rep.checked:,}/{len(rows):,}  {rate:.0f}/s  found {rep.found:,}",
                    file=sys.stderr,
                    flush=True,
                )
    return rep


def recheck(
    conn: sqlite3.Connection,
    *,
    ats: str | None = None,
    limit: int | None = None,
    workers: int | None = None,
    older_than_days: int = 7,
) -> Report:
    """Re-probe stored careers pages and notice when a company changes ATS."""
    cutoff = int(time.time()) - older_than_days * 86400
    q = (
        "SELECT ats, slug, company_name, website, careers_url FROM boards "
        "WHERE careers_url IS NOT NULL "
        "AND (careers_checked_at IS NULL OR careers_checked_at < ?)"
    )
    args: list = [cutoff]
    if ats:
        q += " AND ats = ?"
        args.append(ats)
    q += " ORDER BY careers_checked_at IS NULL DESC, careers_checked_at ASC"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q, args).fetchall()
    rep = Report()
    if not rows:
        return rep

    def work(row):
        dom = domain_of(row["careers_url"]) or domain_of(row["website"] or "")
        return (row, *probe(dom)) if dom else (row, None, [])

    with ThreadPoolExecutor(max_workers=workers or config.WORKERS) as pool:
        for row, url, refs in pool.map(work, rows):
            rep.checked += 1
            now = int(time.time())
            conn.execute(
                "UPDATE boards SET careers_checked_at=? WHERE ats=? AND slug=?",
                (now, row["ats"], row["slug"]),
            )
            if not refs:
                rep.gone += 1
                continue
            still_here = any(r.ats == row["ats"] and r.slug == row["slug"] for r in refs)
            if still_here:
                rep.confirmed += 1
            others = [r for r in refs if (r.ats, r.slug) != (row["ats"], row["slug"])]
            if others and not still_here:
                rep.migrated += 1
                jobs_mod.record_event(
                    conn,
                    "migrated",
                    row["ats"],
                    row["slug"],
                    None,
                    row["company_name"],
                    row["careers_url"],
                    {"now_on": [f"{r.ats}:{r.slug}" for r in others]},
                )
            for r in others:
                rep.new_boards.append(
                    BoardRef(
                        r.ats,
                        r.slug,
                        row["company_name"],
                        "careers_recheck",
                        {"migrated_from": f"{row['ats']}:{row['slug']}"},
                        website=row["website"],
                        careers_url=url or row["careers_url"],
                    )
                )
    return rep
