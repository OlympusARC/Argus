"""One SQL snapshot of the world, taken before every decision.

Deliberately one function returning a plain dict. The policy is a pure
function over this dict, so the snapshot is the entire interface between
"what is true" and "what to do" -- and a test can hand the policy a
hand-written dict without touching a database.

Every query here is a count over an indexed column. The snapshot runs once
per orchestrator step, so it has to be cheap enough that measuring is never
the reason a step was skipped.
"""

from __future__ import annotations

import time

from ..obs import runs as obs_runs


def _one(conn, sql: str, params=()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row["n"] or 0) if row else 0


def snapshot(conn, *, budget_s: int = 0, spent_s: int = 0) -> dict:
    from ..classify import RULESET

    now = int(time.time())

    """
    A source that collapsed is the highest-priority signal, so it is measured
    first and carried whole -- the healer will want the reason, not just the
    fact.
    """
    collapsed = [
        {"source": t.source, "reason": t.reason, "latest": t.latest, "median": t.median}
        for t in obs_runs.collapsed(conn)
    ]

    last_discover = conn.execute(
        "SELECT MAX(started_at) t FROM source_runs WHERE skipped IS NULL"
    ).fetchone()
    hours = None
    if last_discover and last_discover["t"]:
        hours = (now - int(last_discover["t"])) / 3600.0

    """
    Marginal yield over the last week: how many new boards the whole of
    discovery is still finding. When this flattens, the answer is a new
    source rather than another sweep of the old ones.
    """
    weekly = _one(
        conn,
        "SELECT COALESCE(SUM(new_boards),0) n FROM source_runs WHERE started_at > ?",
        (now - 7 * 86400,),
    )

    return {
        "budget_s": budget_s,
        "spent_s": spent_s,
        "collapsed": collapsed,
        "unvalidated": _one(conn, "SELECT COUNT(*) n FROM boards WHERE status='unvalidated'"),
        "active_boards": _one(conn, "SELECT COUNT(*) n FROM boards WHERE status='active'"),
        "stale_classification": _one(
            conn,
            "SELECT COUNT(*) n FROM jobs WHERE classified_by IS NULL OR classified_by <> ?",
            (RULESET,),
        ),
        "unresolved_companies": _one(
            conn, "SELECT COUNT(*) n FROM companies WHERE careers_checked_at IS NULL"
        ),
        "open_jobs": _one(conn, "SELECT COUNT(*) n FROM jobs WHERE status='open'"),
        "hours_since_discover": hours,
        "marginal_yield_7d": weekly,
    }


def render(s: dict) -> str:
    """The snapshot as a human reads it, for --dry-run and the run log."""
    lines = [
        f"  active boards        {s['active_boards']:>10,}",
        f"  unvalidated          {s['unvalidated']:>10,}",
        f"  open jobs            {s['open_jobs']:>10,}",
        f"  stale classification {s['stale_classification']:>10,}",
        f"  unresolved companies {s['unresolved_companies']:>10,}",
        f"  7d marginal yield    {s['marginal_yield_7d']:>10,}",
    ]
    h = s.get("hours_since_discover")
    lines.append(f"  since last discover  {(f'{h:.0f}h' if h is not None else 'never'):>10}")
    if s["collapsed"]:
        lines.append(f"  collapsed sources    {len(s['collapsed']):>10}")
        for c in s["collapsed"]:
            lines.append(f"      {c['source']}: {c['reason']}")
    return "\n".join(lines)
