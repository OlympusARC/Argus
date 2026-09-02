"""Company registry: everything that writes to `companies`.

The company is the entity we actually want to monitor. Boards come and go --
a slug dies the day a company switches ATS -- but the company and its careers
page persist, so this table is the spine and `boards` hangs off it.

Identity is the apex domain when we know it and the normalized name when we do
not. That split is the only real complexity here: sources arrive in either
order, and a name-only row must become the domain-bearing row rather than sit
beside it forever. `upsert` is where that merge happens.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..core import db
from ..core.names import apex
from ..core.names import base as norm


def now() -> int:
    return int(time.time())


def upsert(conn: sqlite3.Connection, **kw) -> int | None:
    """Insert or enrich one company; return its id."""
    cid, _ = upsert2(conn, **kw)
    return cid


def upsert2(
    conn: sqlite3.Connection,
    *,
    domain: str | None = None,
    name: str | None = None,
    website: str | None = None,
    careers_url: str | None = None,
    source: str = "unknown",
) -> tuple[int | None, bool]:
    """Insert or enrich one company; return (id, created).

    Never overwrites a value we already hold -- sources disagree and the first
    one to name a company is usually the more specific. The one exception is
    `domain`, which is filled in on a name-only row, because that is the whole
    point of the merge.
    """
    domain = apex(domain) or apex(website)
    nn = norm(name or "")
    if not domain and not nn:
        return None, False

    row = None
    if domain:
        row = conn.execute("SELECT id FROM companies WHERE domain = ?", (domain,)).fetchone()
    if row is None and nn:
        """
        Prefer an existing domain-bearing row: merging a name-only row into
        it is what stops the same company existing twice.
        """
        row = conn.execute(
            "SELECT id FROM companies WHERE norm_name = ? ORDER BY domain IS NULL, id LIMIT 1",
            (nn,),
        ).fetchone()
        """
        ...but only if it does not already claim a different domain.
        """
        if row is not None and domain:
            clash = conn.execute(
                "SELECT domain FROM companies WHERE id = ?", (row["id"],)
            ).fetchone()
            if clash and clash["domain"] and clash["domain"] != domain:
                row = None

    if row is None:
        return (
            db.insert_id(
                conn,
                """INSERT INTO companies (domain, name, norm_name, website, careers_url,
                                          first_seen_at, source)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    domain,
                    name,
                    nn or None,
                    website or (f"https://{domain}" if domain else None),
                    careers_url,
                    now(),
                    source,
                ),
            ),
            True,
        )

    cid = row["id"]
    conn.execute(
        """UPDATE companies SET
               domain      = COALESCE(domain, ?),
               name        = COALESCE(name, ?),
               norm_name   = COALESCE(norm_name, ?),
               website     = COALESCE(website, ?),
               careers_url = COALESCE(careers_url, ?)
           WHERE id = ?""",
        (
            domain,
            name,
            nn or None,
            website or (f"https://{domain}" if domain else None),
            careers_url,
            cid,
        ),
    )
    return cid, False


def merge_into(conn: sqlite3.Connection, src_id: int, dst_id: int) -> None:
    """Fold one company row into another, then delete the source.

    Needed because a company can be created from a name long before anyone
    tells us its domain, and the domain we later resolve may already belong to
    a row some other source created first. Assigning it would violate the
    unique index; keeping both would split the company in two, with its boards
    on one row and its careers page on the other.
    """
    if src_id == dst_id:
        return
    conn.execute("UPDATE boards SET company_id=? WHERE company_id=?", (dst_id, src_id))
    src = conn.execute("SELECT * FROM companies WHERE id=?", (src_id,)).fetchone()
    if src is not None:
        conn.execute(
            """UPDATE companies SET
                   name        = COALESCE(name, ?),
                   norm_name   = COALESCE(norm_name, ?),
                   website     = COALESCE(website, ?),
                   careers_url = COALESCE(careers_url, ?)
               WHERE id = ?""",
            (src["name"], src["norm_name"], src["website"], src["careers_url"], dst_id),
        )
    conn.execute("DELETE FROM companies WHERE id=?", (src_id,))


def owner_of_domain(conn: sqlite3.Connection, domain: str) -> int | None:
    row = conn.execute("SELECT id FROM companies WHERE domain=?", (domain,)).fetchone()
    return row["id"] if row else None


def link_board(conn: sqlite3.Connection, ats: str, slug: str, company_id: int) -> None:
    conn.execute(
        "UPDATE boards SET company_id=? WHERE ats=? AND slug=? AND company_id IS NULL",
        (company_id, ats, slug),
    )


def set_careers(
    conn: sqlite3.Connection, company_id: int, *, url: str | None, kind: str
) -> None:
    """Record the outcome of a careers probe. kind is 'ats', 'html' or 'none'."""
    conn.execute(
        """UPDATE companies SET careers_url = COALESCE(?, careers_url),
                                careers_kind = ?, careers_checked_at = ?
           WHERE id = ?""",
        (url, kind, now(), company_id),
    )


def classify(careers_url: str | None, refs: list) -> str:
    """What kind of careers page did we land on?

    'ats' means the page hands us a board we can poll, so the company is
    already monitored and nothing more is needed. Everything else is a page we
    hold but do not yet watch.
    """
    if refs:
        return "ats"
    return "html" if careers_url else "none"


def from_board_row(
    conn: sqlite3.Connection, row: sqlite3.Row, source: str
) -> tuple[int | None, bool]:
    """Derive a company from a board we already hold, and link them."""
    cid, created = upsert2(
        conn,
        domain=apex(row["website"]),
        name=row["company_name"],
        website=row["website"],
        careers_url=row["careers_url"],
        source=source,
    )
    if cid is not None:
        link_board(conn, row["ats"], row["slug"], cid)
    return cid, created


def adopt_boards(conn: sqlite3.Connection, limit: int | None = None) -> dict[str, int]:
    """Create a company for every board that names one, and link it.

    This is the backfill that turns an existing board registry into a company
    registry. Boards that name nobody are left unlinked rather than given a
    company invented from their slug: a slug is not a name, and guessing here
    would fabricate companies that never merge with the real row.
    """
    q = (
        "SELECT ats, slug, company_name, website, careers_url FROM boards "
        "WHERE company_id IS NULL AND (company_name IS NOT NULL OR website IS NOT NULL) "
        "ORDER BY job_count DESC"
    )
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q).fetchall()
    made = linked = 0
    conn.execute("BEGIN")
    try:
        for row in rows:
            cid, created = from_board_row(conn, row, source="boards")
            if cid is None:
                continue
            linked += 1
            made += created
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"boards": len(rows), "linked": linked, "companies": made}


def add_many(conn: sqlite3.Connection, records: Iterable[dict], source: str) -> dict[str, int]:
    """Bulk-add companies from a harvest. Each record: name/domain/website/careers_url.

    upsert2 resolves one company with two or three SELECTs and a write, which
    is right for one company and ruinous for a harvest: at roughly 90ms a
    round trip it measured 0.26s per record, and a single job-list repo holds
    19,466 of them. Discovery spent its entire time waiting.

    So the identity rules move into Python and the database is asked twice.
    The rules themselves are unchanged, and the tests that pin them do not
    know this function was rewritten.
    """
    recs = list(records)
    seen = len(recs)
    if not recs:
        return {"seen": 0, "new_companies": 0}

    """
    Resolve each record to the pair identity actually turns on, and collapse
    duplicates. A harvest names the same employer once per posting, so a batch
    of 500 refs is usually a few dozen companies.
    """
    prepared = []
    for rec in recs:
        domain = apex(rec.get("domain")) or apex(rec.get("website"))
        nn = norm(rec.get("name") or "")
        if not domain and not nn:
            continue
        prepared.append((domain, nn, rec))

    unique: dict[tuple[str | None, str], dict] = {}
    boards: list[tuple[tuple[str | None, str], tuple]] = []
    for domain, nn, rec in prepared:
        key = (domain, nn)
        keep = unique.setdefault(key, rec)
        """
        Merge what the duplicates know rather than taking the first blindly:
        one row may carry the careers page and another the website.
        """
        for col in ("name", "website", "careers_url", "domain"):
            if not keep.get(col) and rec.get(col):
                keep[col] = rec[col]
        if rec.get("board"):
            boards.append((key, rec["board"]))

    domains = {d for d, _ in unique if d}
    names = {n for _, n in unique if n}

    by_domain: dict[str, int] = {}
    by_name: dict[str, tuple[int, str | None]] = {}
    if domains:
        ph = ",".join("?" * len(domains))
        for r in conn.execute(
            f"SELECT id, domain FROM companies WHERE domain IN ({ph})", tuple(domains)
        ):
            by_domain[r["domain"]] = r["id"]
    if names:
        ph = ",".join("?" * len(names))
        """
        Domain-bearing rows first, matching upsert2's ORDER BY: merging a
        name-only row into one that has a domain is what stops the same
        company existing twice.
        """
        for r in conn.execute(
            f"""SELECT id, norm_name, domain FROM companies
                WHERE norm_name IN ({ph})
                ORDER BY domain IS NULL DESC, id DESC""",
            tuple(names),
        ):
            by_name[r["norm_name"]] = (r["id"], r["domain"])

    resolved: dict[tuple[str | None, str], int] = {}
    updates, inserts = [], []
    for (domain, nn), rec in unique.items():
        cid = by_domain.get(domain) if domain else None
        if cid is None and nn:
            hit = by_name.get(nn)
            """
            A name match is rejected when the row already claims a different
            domain -- two companies can share a name, and merging them loses
            both.
            """
            if hit and not (domain and hit[1] and hit[1] != domain):
                cid = hit[0]

        website = rec.get("website") or (f"https://{domain}" if domain else None)
        if cid is None:
            inserts.append(((domain, nn), rec, website))
        else:
            resolved[(domain, nn)] = cid
            updates.append(
                (domain, rec.get("name"), nn or None, website, rec.get("careers_url"), cid)
            )

    conn.execute("BEGIN")
    try:
        if updates:
            conn.executemany(
                """UPDATE companies SET
                       domain      = COALESCE(domain, ?),
                       name        = COALESCE(name, ?),
                       norm_name   = COALESCE(norm_name, ?),
                       website     = COALESCE(website, ?),
                       careers_url = COALESCE(careers_url, ?)
                   WHERE id = ?""",
                updates,
            )

        """
        Inserts stay one at a time: each needs its generated id to link a
        board, and they are the rare case -- a mature registry resolves almost
        every harvested name to a row it already has.
        """
        ts = now()
        for key, rec, website in inserts:
            resolved[key] = db.insert_id(
                conn,
                """INSERT INTO companies (domain, name, norm_name, website, careers_url,
                                          first_seen_at, source)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    key[0],
                    rec.get("name"),
                    key[1] or None,
                    website,
                    rec.get("careers_url"),
                    ts,
                    source,
                ),
            )

        links = [
            (resolved[key], board[0], board[1]) for key, board in boards if key in resolved
        ]
        if links:
            conn.executemany(
                "UPDATE boards SET company_id=? WHERE ats=? AND slug=? AND company_id IS NULL",
                links,
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"seen": seen, "new_companies": len(inserts)}


def needing_careers(
    conn: sqlite3.Connection, limit: int | None = None, recheck_days: int | None = None
):
    """Companies with a domain but no settled careers page, best targets first.

    Ordered by whether we already know a board for them: a company we cannot
    otherwise reach is worth probing before one we already poll.
    """
    q = """SELECT c.id, c.domain, c.name FROM companies c
           WHERE c.domain IS NOT NULL AND ("""
    args: list = []
    if recheck_days:
        q += "c.careers_checked_at IS NULL OR c.careers_checked_at < ?)"
        args.append(now() - recheck_days * 86400)
    else:
        q += "c.careers_checked_at IS NULL)"
    q += " ORDER BY (SELECT COUNT(*) FROM boards b WHERE b.company_id = c.id) ASC, c.id ASC"
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q, args).fetchall()


def unresolved(conn: sqlite3.Connection, limit: int | None = None):
    """Companies we have a name for but no domain -- the guessable ones."""
    q = (
        "SELECT id, name, norm_name FROM companies "
        "WHERE domain IS NULL AND name IS NOT NULL AND careers_checked_at IS NULL "
        "ORDER BY id"
    )
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q, ()).fetchall()


@dataclass
class Resolution:
    checked: int = 0
    ats: int = 0
    html: int = 0
    none: int = 0
    domains_learned: int = 0
    merged: int = 0
    rejected: int = 0
    new_boards: list = field(default_factory=list)

    def line(self) -> str:
        return (
            f"checked {self.checked:,}  ats {self.ats:,}  page-only {self.html:,}  "
            f"nothing {self.none:,}  domains learned {self.domains_learned:,}  "
            f"merged {self.merged:,}  guesses rejected {self.rejected:,}"
        )


def _owns(conn: sqlite3.Connection, company_id: int, norm_name: str, refs) -> bool:
    """Does this careers page belong to the company we guessed a domain for?

    A guessed domain is a claim, not a fact: probing `alan.com` for a company
    called Alan can land on an unrelated business that happens to run an ATS.
    The page has to prove ownership either by carrying a board we already tied
    to this company, or by carrying a slug that matches its name.
    """
    if not refs:
        return False
    owned = {
        (r["ats"], r["slug"])
        for r in conn.execute(
            "SELECT ats, slug FROM boards WHERE company_id = ?", (company_id,)
        )
    }
    for ref in refs:
        if (ref.ats, ref.slug) in owned:
            return True
        if norm_name and norm(ref.slug) == norm_name:
            return True
    return False


def resolve(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    workers: int | None = None,
    max_guesses: int = 3,
    guess_unknown: bool = True,
    progress_every: int = 250,
) -> Resolution:
    """Attach a careers page to every company we can, and learn domains on the way.

    Two populations, and they need different rules:

      known domain   -- the domain is authoritative, so whatever the careers
                        page says is accepted.
      name only      -- the domain is a guess, so it is only kept if the page
                        proves it belongs to this company (see _owns).

    Boards found on the way out are returned rather than written, so the caller
    decides whether a discovery pass should also enrol them.
    """
    from ..core import config
    from ..core.models import BoardRef
    from ..discovery.probe import MAX_ATTRIBUTABLE_REFS, probe_detailed

    rep = Resolution()
    """
    One work item per company, carrying every domain worth trying for it.
    Submitting each guess as its own item instead would probe all three TLDs
    for every company even when the first one answers, which is most of them.
    """
    targets: list[tuple] = [
        (r["id"], [r["domain"]], r["name"], None, True)
        for r in needing_careers(conn, limit=limit)
    ]
    if guess_unknown:
        room = None if limit is None else max(0, limit - len(targets))
        if room is None or room > 0:
            for r in unresolved(conn, limit=room):
                cands = _guesses(r["name"], max_guesses)
                if cands:
                    targets.append((r["id"], cands, r["name"], r["norm_name"], False))
    if not targets:
        return rep

    t0 = time.time()

    def work(t):
        """Try this company's domains in order; stop at the first with a board."""
        cid, domains, name, nn, authoritative = t
        fallback = (None, None)
        for domain in domains:
            url, refs, reachable = probe_detailed(domain)
            if refs:
                return t, url, refs, reachable, domain
            if reachable and fallback[0] is None:
                fallback = (reachable, domain)
        return t, None, [], fallback[0], fallback[1]

    with ThreadPoolExecutor(max_workers=workers or config.WORKERS) as pool:
        for t, url, refs, reachable, domain in pool.map(work, targets):
            cid, _domains, name, nn, authoritative = t
            """
            Every company probed gets an outcome recorded, including the ones
            where nothing was found. Leaving those unmarked would put them
            back at the front of the queue on the next run, forever.
            """
            rep.checked += 1
            if progress_every and rep.checked % progress_every == 0:
                rate = rep.checked / max(time.time() - t0, 1e-9)
                eta = (len(targets) - rep.checked) / max(rate, 1e-9)
                print(
                    f"    {rep.checked:,}/{len(targets):,}  {rate:.0f}/s  "
                    f"eta {eta / 60:.0f}m  ({rep.ats:,} on an ATS, "
                    f"{rep.html:,} page-only)",
                    file=sys.stderr,
                    flush=True,
                )

            """
            A guessed domain only counts if the page proves it is this
            company's. A page that merely loads proves nothing -- a parked
            domain does that -- so an unproven guess is recorded as a miss
            rather than stored as this company's careers page.
            """
            if not authoritative and not _owns(conn, cid, nn or "", refs):
                if refs or reachable:
                    rep.rejected += 1
                set_careers(conn, cid, url=None, kind="none")
                rep.none += 1
                continue

            kind = classify(url or reachable, refs)
            if not authoritative and domain:
                """
                The domain we just learned may already belong to a row some
                other source created first. That is the same company, so the
                two rows fold together rather than fighting over the domain.
                """
                owner = owner_of_domain(conn, domain)
                if owner is not None and owner != cid:
                    merge_into(conn, cid, owner)
                    cid = owner
                    rep.merged += 1
                else:
                    conn.execute(
                        "UPDATE companies SET domain = COALESCE(domain, ?), "
                        "website = COALESCE(website, ?) WHERE id = ?",
                        (domain, f"https://{domain}", cid),
                    )
                    rep.domains_learned += 1
            set_careers(conn, cid, url=url or reachable, kind=kind)
            rep.ats += kind == "ats"
            rep.html += kind == "html"
            rep.none += kind == "none"

            """
            An aggregator page lists many companies' boards. Keep the boards,
            drop the attribution -- otherwise one VC portfolio page renames a
            hundred companies after its owner.
            """
            owned = len(refs) <= MAX_ATTRIBUTABLE_REFS
            for ref in refs:
                rep.new_boards.append(
                    BoardRef(
                        ref.ats,
                        ref.slug,
                        name if owned else None,
                        "companies",
                        {"via": domain, "aggregator": not owned},
                        website=f"https://{domain}" if owned else None,
                        careers_url=url if owned else None,
                    )
                )
                if owned:
                    link_board(conn, ref.ats, ref.slug, cid)
    return rep


def _guesses(name: str, max_guesses: int) -> list[str]:
    """Domains worth trying for a company we only have a name for."""
    from .careers import TLDS

    stem = norm(name)
    if not (2 < len(stem) < 30):
        return []
    return [stem + tld for tld in TLDS[:max_guesses]]


def stats(conn: sqlite3.Connection) -> dict:
    """
    Every SUM is coalesced: over zero rows SQLite returns NULL, not 0, and a
    caller dividing by the total to show a percentage then fails on an empty
    database -- which is exactly the state a fresh checkout is in.
    """
    row = conn.execute(
        """SELECT COUNT(*) total,
                  COUNT(domain)                                domained,
                  COUNT(careers_url)                           careers,
                  COALESCE(SUM(CASE WHEN careers_kind='ats' THEN 1 ELSE 0 END), 0)  ats,
                  COALESCE(SUM(CASE WHEN careers_kind='html' THEN 1 ELSE 0 END), 0) html,
                  COALESCE(SUM(CASE WHEN careers_kind='none' THEN 1 ELSE 0 END), 0) none_,
                  COALESCE(SUM(CASE WHEN careers_checked_at IS NULL THEN 1 ELSE 0 END), 0) unprobed
           FROM companies"""
    ).fetchone()
    linked = conn.execute(
        "SELECT COUNT(*) n FROM boards WHERE company_id IS NOT NULL"
    ).fetchone()["n"]
    """
    Monitoring status is about boards, not about careers_kind. The two come
    apart constantly: NVIDIA's careers page renders its Workday link in JS so
    it probes as 'html', yet we poll that board every hour because another
    source found it. Reporting careers_kind as coverage would understate what
    is watched and overstate what is missing.
    """
    monitored = conn.execute(
        """SELECT COUNT(DISTINCT c.id) n FROM companies c
           JOIN boards b ON b.company_id = c.id
           WHERE b.status IN ('active','empty')"""
    ).fetchone()["n"]
    gap = conn.execute(
        """SELECT COUNT(*) n FROM companies c
           WHERE c.careers_kind = 'html'
             AND NOT EXISTS (SELECT 1 FROM boards b
                             WHERE b.company_id = c.id AND b.status IN ('active','empty'))"""
    ).fetchone()["n"]
    unreached = conn.execute(
        """SELECT COUNT(*) n FROM companies c
           WHERE NOT EXISTS (SELECT 1 FROM boards b
                             WHERE b.company_id = c.id AND b.status IN ('active','empty'))"""
    ).fetchone()["n"]
    """
    Companies whose name is too long or too short to be a plausible domain
    stem. `resolve` never queues them, so counting them as "not probed yet"
    would show a backlog that no amount of running ever clears.
    """
    unguessable = conn.execute(
        """SELECT COUNT(*) n FROM companies
           WHERE domain IS NULL AND careers_checked_at IS NULL
             AND (norm_name IS NULL OR LENGTH(norm_name) < 3 OR LENGTH(norm_name) > 29)"""
    ).fetchone()["n"]
    return dict(row) | {
        "boards_linked": linked,
        "monitored": monitored,
        "page_but_unmonitored": gap,
        "unmonitored": unreached,
        "unguessable": unguessable,
    }
