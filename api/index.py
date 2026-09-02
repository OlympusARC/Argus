"""Read API. Vercel serves any api/*.py as a handler, so this is the entrypoint.

Two resources, not three. `boards` stays internal -- it is how we reach a
company, which is an implementation detail of collection, not something a
reader should have to join through. `jobs` therefore answers by company.

Everything here is a read. The pipeline is the only writer, and it runs on
GitHub Actions, so this process never needs a write connection or a
transaction. It talks to the transaction pooler because serverless
invocations are short and numerous, where the workers hold one connection for
a long poll and use the session pooler instead.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from argus.classify import RULESET
from argus.core import config, pg

app = FastAPI(
    title="Argus",
    version="0.1.0",
    description="Companies, the boards that reach them, and the postings they carry.",
)

API_KEY = os.getenv("ARGUS_API_KEY")


def require_key(x_api_key: str | None = Header(default=None)) -> None:
    """A single shared key.

    Not sophisticated, but the service is internet-facing by default and an
    unauthenticated database read endpoint is the kind of thing that stays
    unauthenticated for a year. Unset means open, which is fine locally and
    should never be the case in deployment.
    """
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="bad or missing X-API-Key")


"""
One connection per process, reused across invocations rather than opened per
request. Measured against the live database: the query itself runs in 0.1 ms
and opening a connection costs 529 ms, so a per-request connection would make
this endpoint 99% handshake. Serverless keeps instances warm between
invocations, which is exactly what makes reuse worth having.
"""
_conn = None


def db():
    global _conn
    url = config.database_url(pooled=True)
    if not url:
        raise HTTPException(status_code=503, detail="no database configured")
    if _conn is not None:
        try:
            _conn.execute("SELECT 1").fetchone()
        except Exception:
            """
            The pooler drops idle connections and an instance can be paused
            for longer than it tolerates. A dead connection is expected, not
            exceptional -- reconnect rather than fail the request.
            """
            _conn = None
    if _conn is None:
        _conn = pg.connect(url)
    return _conn


def _rows(conn, sql: str, params: tuple) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


@app.get("/health")
def health() -> dict:
    return {"ok": True, "ruleset": RULESET}


@app.get("/jobs", dependencies=[Depends(require_key)])
def jobs(
    conn=Depends(db),
    family: str | None = Query(
        None, description="engineering, fde, data, product, design, security"
    ),
    engineering: bool | None = Query(
        None, description="engineering roles, FDE and security included"
    ),
    fde: bool | None = Query(None, description="forward-deployed family only"),
    seniority: str | None = Query(None, description="intern, new_grad, senior, staff, ..."),
    status: str = Query("open"),
    since: int | None = Query(None, description="unix seconds; postings first seen after this"),
    q: str | None = Query(None, description="substring of the title"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """Postings, newest first.

    The classification filters are indexed columns rather than a LIKE over
    titles, which is the difference between 0.1 ms and 280 ms and, more to the
    point, between constant and linear in the size of the corpus.
    """
    where = ["j.status = ?"]
    args: list[Any] = [status]
    if family:
        where.append("j.role_family = ?")
        args.append(family)
    if engineering is not None:
        where.append("j.is_engineering = ?")
        args.append(1 if engineering else 0)
    if fde is not None:
        where.append("j.is_fde = ?")
        args.append(1 if fde else 0)
    if seniority:
        where.append("j.seniority = ?")
        args.append(seniority)
    if since:
        where.append("j.first_seen_at >= ?")
        args.append(since)
    if q:
        where.append("j.title ILIKE ?")
        args.append(f"%{q}%")

    sql = f"""
        SELECT j.ats, j.slug, j.external_id, j.title, j.url, j.location,
               j.role_family, j.seniority, j.is_engineering, j.is_fde,
               j.posted_at, j.first_seen_at,
               c.name AS company, c.domain, c.careers_url
        FROM jobs j
        LEFT JOIN boards b ON b.ats = j.ats AND b.slug = j.slug
        LEFT JOIN companies c ON c.id = b.company_id
        WHERE {" AND ".join(where)}
        ORDER BY j.first_seen_at DESC
        LIMIT ? OFFSET ?
    """
    return {"jobs": _rows(conn, sql, (*args, limit, offset)), "limit": limit, "offset": offset}


@app.get("/companies", dependencies=[Depends(require_key)])
def companies(
    conn=Depends(db),
    monitored: bool | None = Query(None, description="has at least one live board"),
    has_careers: bool | None = Query(None),
    q: str | None = Query(None, description="substring of the name or domain"),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """Who we watch, and where.

    careers_kind describes the page, not our coverage: a careers page that
    renders its ATS link in JavaScript probes as 'html' even when the board
    behind it is polled hourly. `monitored` is the honest question and asks
    about boards.
    """
    where = ["1=1"]
    args: list[Any] = []
    if has_careers is not None:
        where.append("c.careers_url IS NOT NULL" if has_careers else "c.careers_url IS NULL")
    if q:
        where.append("(c.name ILIKE ? OR c.domain ILIKE ?)")
        args.extend([f"%{q}%", f"%{q}%"])
    live = """EXISTS (SELECT 1 FROM boards b
                      WHERE b.company_id = c.id AND b.status IN ('active','empty'))"""
    if monitored is not None:
        where.append(live if monitored else f"NOT {live}")

    sql = f"""
        SELECT c.id, c.name, c.domain, c.website, c.careers_url, c.careers_kind,
               {live} AS monitored,
               (SELECT COUNT(*) FROM boards b WHERE b.company_id = c.id) AS boards
        FROM companies c
        WHERE {" AND ".join(where)}
        ORDER BY monitored DESC, c.id
        LIMIT ? OFFSET ?
    """
    return {
        "companies": _rows(conn, sql, (*args, limit, offset)),
        "limit": limit,
        "offset": offset,
    }


@app.get("/companies/{company_id}/jobs", dependencies=[Depends(require_key)])
def company_jobs(company_id: int, conn=Depends(db), limit: int = Query(100, le=500)) -> dict:
    """Every posting across every board this company has ever used.

    Survives an ATS migration by construction: a dead Greenhouse board and a
    live Ashby one both hang off the same company row.
    """
    sql = """
        SELECT j.ats, j.slug, j.external_id, j.title, j.url, j.location,
               j.role_family, j.status, j.first_seen_at
        FROM jobs j
        JOIN boards b ON b.ats = j.ats AND b.slug = j.slug
        WHERE b.company_id = ?
        ORDER BY j.first_seen_at DESC
        LIMIT ?
    """
    return {"jobs": _rows(conn, sql, (company_id, limit))}


@app.get("/events", dependencies=[Depends(require_key)])
def events(
    conn=Depends(db),
    type: str | None = Query(
        None, description="new, edited, closed, reopened, migrated, funding"
    ),
    since: int | None = Query(None, description="unix seconds"),
    engineering_only: bool = Query(False),
    limit: int = Query(100, le=500),
) -> dict:
    """The change feed. This is what a notifier subscribes to."""
    where = ["1=1"]
    args: list[Any] = []
    if type:
        where.append("e.type = ?")
        args.append(type)
    if since:
        where.append("e.ts >= ?")
        args.append(since)
    if engineering_only:
        where.append(
            """EXISTS (SELECT 1 FROM jobs j
                       WHERE j.ats = e.ats AND j.slug = e.slug
                         AND j.external_id = e.external_id AND j.is_engineering = 1)"""
        )
    sql = f"""
        SELECT e.ts, e.type, e.ats, e.slug, e.external_id, e.title, e.url
        FROM events e
        WHERE {" AND ".join(where)}
        ORDER BY e.ts DESC
        LIMIT ?
    """
    return {"events": _rows(conn, sql, (*args, limit))}


@app.get("/health/pipeline", dependencies=[Depends(require_key)])
def pipeline(conn=Depends(db)) -> dict:
    """Domain health, which no APM product can infer.

    The failures that hurt here are a source silently yielding zero, an ATS
    rate-limiting us into backoff, or a board mass-closing on a bad response.
    None of those look like an error to a tracing tool.
    """
    counts = _rows(
        conn,
        """SELECT 'companies' AS name, COUNT(*) AS n FROM companies
           UNION ALL SELECT 'boards', COUNT(*) FROM boards
           UNION ALL SELECT 'boards_live', COUNT(*) FROM boards
                     WHERE status IN ('active','empty')
           UNION ALL SELECT 'jobs_open', COUNT(*) FROM jobs WHERE status='open'
           UNION ALL SELECT 'jobs_engineering', COUNT(*) FROM jobs
                     WHERE status='open' AND is_engineering=1
           UNION ALL SELECT 'jobs_fde', COUNT(*) FROM jobs
                     WHERE status='open' AND is_fde=1""",
        (),
    )
    last_poll = _rows(
        conn,
        """SELECT started_at, finished_at, boards_polled, boards_failed,
                  new_jobs, edited_jobs, closed_jobs
           FROM poll_runs ORDER BY id DESC LIMIT 5""",
        (),
    )
    stale = _rows(
        conn,
        "SELECT COUNT(*) AS n FROM jobs WHERE classified_by IS NULL OR classified_by <> ?",
        (RULESET,),
    )
    return {
        "counts": {r["name"]: r["n"] for r in counts},
        "recent_polls": last_poll,
        "unclassified": stale[0]["n"] if stale else 0,
        "ruleset": RULESET,
    }
