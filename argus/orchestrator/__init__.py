"""The orchestrator: what the pipeline does today, decided from what is true.

Cron still says *when* -- GitHub Actions, one entry, daily. This says *what*,
which is the part that used to live in a crontab as "run discover at 03:00 no
matter what".

The division of labour with the feed lane is deliberate and load-bearing:
poll and notify run on their own hourly cron with their own timeout, and are
not nodes in this graph. A stuck orchestrator, an exhausted LLM quota or a
LangGraph bug can therefore never delay a job posting reaching the digest.
"""

from __future__ import annotations

import time

from . import measure, policy
from .graph import build_graph, postgres_checkpointer, sqlite_checkpointer

__all__ = [
    "build_graph",
    "measure",
    "orchestrate",
    "plan",
    "policy",
    "postgres_checkpointer",
    "sqlite_checkpointer",
]


def plan(conn, budget_s: int) -> tuple[dict, list[str], str]:
    """What the orchestrator would do right now, without doing any of it.

    A policy you cannot inspect before it runs is one you have to trust.
    """
    from .nodes import BUILDERS

    available = set(BUILDERS)
    snap = measure.snapshot(conn, budget_s=budget_s, spent_s=0)
    lines = policy.explain(snap, available=available)
    action, why = policy.decide(dict(snap), available=available)
    first = "nothing" if action == policy.END else action
    return snap, lines, f"{first}  ({why})"


def orchestrate(
    conn,
    *,
    budget_s: int = 2700,
    thread_id: str | None = None,
    checkpointer=None,
) -> dict:
    """Run the loop until the budget is spent or nothing is worth doing.

    thread_id defaults to the date, so a relaunch on the same day resumes the
    same plan rather than starting a fresh one -- which is the entire reason
    the checkpointer is here.
    """
    app, _ = build_graph(conn, checkpointer=checkpointer)
    cfg = {
        "configurable": {"thread_id": thread_id or time.strftime("orch-%Y-%m-%d")},
        "recursion_limit": 100,
    }
    final = app.invoke(
        {
            "budget_s": budget_s,
            "spent_s": 0,
            "snapshot": {},
            "done": [],
            "skipped": [],
            "steps": 0,
        },
        cfg,
    )
    return {
        "spent_s": final["spent_s"],
        "budget_s": final["budget_s"],
        "steps": final.get("steps", 0),
        "done": final["done"],
        "skipped": final["skipped"],
    }
