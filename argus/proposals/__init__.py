"""An agent proposes; deterministic code disposes.

This is the safety boundary of the whole agent-first turn, and it is a table
rather than a convention on purpose: a rule expressed in a prompt can be
argued away by a sufficiently confident model, and a rule expressed as "this
code path is the only one that writes" cannot.

The gate is the interesting half. It does not judge a proposal's reasoning --
it *measures* the proposal's claim. A prospector says a page lists Ashby
boards; the gate fetches the page and counts how many boards on it are new to
the registry. Zero is a rejection backed by evidence, not an opinion.

Two kinds can never auto-apply regardless of score: anything touching the
reconciler's close logic and anything that would mark a board dead. They are
prevented by having no gate registered, so the prohibition is structural
rather than a flag somebody can flip.
"""

from __future__ import annotations

import json
import time

DRAFTED = "drafted"
AUTO_APPLIED = "auto_applied"
PENDING = "pending"
REJECTED = "rejected"
ACCEPTED = "accepted"


def now() -> int:
    return int(time.time())


def file(conn, agent: str, kind: str, payload: dict, evidence: dict | None = None) -> int:
    """Record a proposal. Writes nothing else, by construction."""
    from ..core.db import insert_id

    pid = insert_id(
        conn,
        """INSERT INTO proposals (agent, kind, payload, evidence, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (agent, kind, json.dumps(payload), json.dumps(evidence or {}), DRAFTED, now()),
    )
    conn.commit()
    return pid


def get(conn, pid: int) -> dict | None:
    row = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"] or "{}")
    d["evidence"] = json.loads(d["evidence"] or "{}")
    return d


def by_status(conn, status: str, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT id FROM proposals WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
    ).fetchall()
    return [get(conn, r["id"]) for r in rows]


def pending(conn, limit: int = 100) -> list[dict]:
    return by_status(conn, PENDING, limit)


def _decide(conn, pid: int, status: str, by: str, score: float | None = None) -> None:
    conn.execute(
        "UPDATE proposals SET status=?, decided_at=?, decided_by=?, score=? WHERE id=?",
        (status, now(), by, score, pid),
    )
    conn.commit()


def gate(conn, pid: int) -> str:
    """Measure a proposal's claim and route it. Returns the new status.

    A kind with no registered gate is never auto-applied -- it goes to a human
    or nowhere. That is how the reconcile path stays off-limits: not by a
    check that could be inverted, but by the absence of a way to say yes.
    """
    from .gates import GATES

    p = get(conn, pid)
    if p is None:
        raise KeyError(f"no proposal {pid}")

    checker = GATES.get(p["kind"])
    if checker is None:
        _decide(conn, pid, PENDING, "gate:no-checker")
        return PENDING

    verdict = checker(conn, p)
    conn.execute(
        "UPDATE proposals SET evidence=? WHERE id=?",
        (json.dumps({**p["evidence"], **verdict.evidence}), pid),
    )
    if verdict.status == AUTO_APPLIED:
        apply(conn, pid, by="gate", score=verdict.score)
        return AUTO_APPLIED
    _decide(conn, pid, verdict.status, "gate", verdict.score)
    return verdict.status


def apply(conn, pid: int, *, by: str, score: float | None = None) -> None:
    """Enact an accepted proposal. The only path from a proposal to the world.

    Applying is separate from gating so a human accepting something the gate
    left pending runs exactly the same code the gate would have.
    """
    from .gates import APPLIERS

    p = get(conn, pid)
    if p is None:
        raise KeyError(f"no proposal {pid}")
    applier = APPLIERS.get(p["kind"])
    if applier is None:
        raise ValueError(f"{p['kind']} has no applier: it can only be read, never enacted")
    applier(conn, p)
    _decide(conn, pid, AUTO_APPLIED if by == "gate" else ACCEPTED, by, score)


def reject(conn, pid: int, *, by: str = "human") -> None:
    _decide(conn, pid, REJECTED, by)


def summary(conn) -> dict[str, int]:
    return {
        r["status"]: r["n"]
        for r in conn.execute("SELECT status, COUNT(*) n FROM proposals GROUP BY status")
    }
