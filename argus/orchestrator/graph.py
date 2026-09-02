"""The loop: measure, decide, act, record, repeat.

LangGraph supplies three things here and nothing else. It routes conditionally
from the policy to whichever node was chosen; it writes a small checkpoint
after every step so a run killed by the 50-minute Actions ceiling resumes
mid-plan instead of re-deciding from scratch; and it gives the whole thing a
trace.

It is not the source of truth for what work is done. That stays where it
already was -- next_poll_at, careers_checked_at, source_runs -- so if this
module were deleted the pipeline would still know exactly where it is. The
checkpoint holds decisions and spend, one level above the cursors.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from .nodes import BUILDERS
from .policy import END as POLICY_END
from .policy import decide


class OrchState(TypedDict):
    budget_s: int
    spent_s: int
    snapshot: dict
    """
    Append-only. Each node contributes its own outcome and never rewrites
    another's, which is what makes the reducer a plain concatenation.
    """
    done: Annotated[list, operator.add]
    skipped: Annotated[list, operator.add]
    steps: int


def build_graph(conn, *, checkpointer=None, node_names=None):
    """Assemble the graph. Returns (compiled_app, available_node_names)."""
    from langgraph.graph import END, START, StateGraph

    from . import measure

    names = list(node_names if node_names is not None else BUILDERS)
    available = set(names)

    def measure_node(state: OrchState) -> dict:
        snap = measure.snapshot(conn, budget_s=state["budget_s"], spent_s=state["spent_s"])
        """
        A node that skipped is carried into the snapshot so the policy stops
        offering it. Skipping costs no time, so without this the loop picks
        the same unaffordable stage forever -- observed on the first real run:
        discover skipped 39 times and only the step ceiling ended it.
        """
        snap["_skipped_tasks"] = list(state.get("skipped", []))
        return {"snapshot": snap, "steps": state.get("steps", 0) + 1}

    def route(state: OrchState) -> str:
        """
        A hard step ceiling on top of the budget. If a node ever returns
        without advancing spent_s -- a bug, or a stage that genuinely did
        nothing -- the budget alone would never end the run.
        """
        if state.get("steps", 0) > 40:
            return END
        """
        Already-skipped stages are removed from what the policy may choose.
        They are unaffordable for the rest of this run by definition: the
        budget only shrinks.
        """
        choosable = available - set(state.get("skipped", []))
        action, _ = decide(dict(state["snapshot"]), available=choosable)
        return END if action == POLICY_END else action

    g = StateGraph(OrchState)
    g.add_node("measure", measure_node)
    for name in names:
        g.add_node(name, BUILDERS[name](conn))
    g.add_edge(START, "measure")
    g.add_conditional_edges("measure", route, [*names, END])
    """
    Every node returns to measure rather than to the policy directly: acting
    changes the world, so the next decision must be made against a fresh
    reading of it, never against the snapshot that prompted the action.
    """
    for name in names:
        g.add_edge(name, "measure")

    return g.compile(checkpointer=checkpointer), available


def sqlite_checkpointer(path: str):
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    return SqliteSaver(sqlite3.connect(path, check_same_thread=False))


def postgres_checkpointer(url: str):
    """Checkpoints live in their own schema.

    The app schema is small enough to read end to end and worth keeping that
    way; LangGraph's tables are an implementation detail of the orchestrator,
    not part of the domain model.
    """
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg import Connection

    conn = Connection.connect(url, autocommit=True)
    conn.execute("CREATE SCHEMA IF NOT EXISTS langgraph")
    conn.execute("SET search_path TO langgraph, public")
    saver = PostgresSaver(conn)
    saver.setup()
    return saver
