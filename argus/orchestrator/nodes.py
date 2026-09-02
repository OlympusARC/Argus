"""The stages, wrapped as graph nodes.

Each node is a thin adapter: check the remaining budget, call the module
function the CLI already calls, return what happened. No stage logic lives
here -- that is the whole point of B0, and a node that starts growing its own
behaviour is a node that has drifted from the command doing the same job.

The budget check is inside every node rather than only in the policy because
the policy decides before a stage runs and cannot know how long it will take.
A stage that would obviously overrun the remaining time is skipped and
recorded, not started and killed mid-write.
"""

from __future__ import annotations

import time

"""
Rough seconds each stage needs to be worth starting. Deliberately generous:
the cost of skipping a stage is that it waits for tomorrow, the cost of
starting one with 30 seconds left is a killed job mid-write.
"""
MIN_SECONDS = {
    "discover": 600,
    "resolve": 300,
    "validate": 300,
    "classify": 120,
    "heal": 120,
    "prospect": 180,
}

"""
LLM calls one orchestrator run may make in total. A cap in the graph rather
than trust in the agents: free tiers have daily limits and an agent that
loops is the way to find them.
"""
LLM_BUDGET = 120


def _remaining(state: dict) -> int:
    return max(0, state["budget_s"] - state["spent_s"])


def _outcome(name: str, t0: float, **detail) -> dict:
    return {"task": name, "spent_s": int(time.time() - t0), **detail}


def _return(state: dict, out: dict) -> dict:
    """The state update for a finished node.

    Any outcome carrying a `skipped` key adds that node to the run's skipped
    list, whatever the reason. Found on the second live run: the budget check
    marked its own skips, but an agent declining internally -- no LLM provider
    configured -- returned a skip the graph never saw, so the policy offered
    prospect again 39 times. A node that did not do its work must not be
    choosable again, and where the refusal came from is irrelevant.
    """
    update = {"spent_s": state["spent_s"] + out["spent_s"], "done": [out]}
    if out.get("skipped"):
        update["skipped"] = [out["task"]]
    return update


def _skip(state: dict, name: str) -> dict | None:
    need = MIN_SECONDS.get(name, 60)
    left = _remaining(state)
    if left < need:
        return {
            "done": [{"task": name, "skipped": f"needs ~{need}s, {left}s left", "spent_s": 0}],
            "skipped": [name],
        }
    return None


def discover_node(conn):
    def node(state: dict) -> dict:
        if (s := _skip(state, "discover")) is not None:
            return s
        from .. import discovery

        t0 = time.time()
        results = discovery.run(conn, None)
        new = sum(r.new_boards for r in results)
        out = _outcome(
            "discover",
            t0,
            new_boards=new,
            new_companies=sum(r.new_companies for r in results),
            failed=[r.source for r in results if r.error],
        )
        return _return(state, out)

    return node


def validate_node(conn):
    def node(state: dict) -> dict:
        if (s := _skip(state, "validate")) is not None:
            return s
        from ..adapters import supported
        from ..registry import validate as validate_mod

        t0 = time.time()
        live = dead = 0
        """
        Split the cap across ATSs rather than spending it all on whichever
        sorts first: an unvalidated backlog is rarely one ATS's fault.
        """
        per = max(1, 2_000 // max(1, len(supported())))
        for ats in supported():
            res = validate_mod.run(conn, ats, limit=per)
            live += getattr(res, "live", 0) or 0
            dead += getattr(res, "dead", 0) or 0
        out = _outcome("validate", t0, live=live, dead=dead)
        return _return(state, out)

    return node


def resolve_node(conn):
    def node(state: dict) -> dict:
        if (s := _skip(state, "resolve")) is not None:
            return s
        from ..registry import careers

        t0 = time.time()
        rep = careers.backfill(conn, ats=None, limit=2_000)
        out = _outcome(
            "resolve", t0, checked=rep.checked, found=rep.found, boards=len(rep.new_boards)
        )
        return _return(state, out)

    return node


def classify_node(conn):
    def node(state: dict) -> dict:
        if (s := _skip(state, "classify")) is not None:
            return s
        from ..feed import jobs

        t0 = time.time()
        res = jobs.reclassify(conn)
        out = _outcome(
            "classify", t0, classified=res["classified"], changed=res["family_changed"]
        )
        return _return(state, out)

    return node


def heal_node(conn):
    """Diagnose whichever source the snapshot says collapsed.

    Takes the first one rather than all of them: the snapshot is re-measured
    after every node, so a second collapsed source is simply the next
    iteration's decision.
    """

    def node(state: dict) -> dict:
        if (s := _skip(state, "heal")) is not None:
            return s
        from ..agents import healer

        collapsed = (state.get("snapshot") or {}).get("collapsed") or []
        if not collapsed:
            """
            Marked skipped, not merely reported: an empty return would leave
            the policy free to choose heal again on the next measure.
            """
            return {
                "done": [{"task": "heal", "skipped": "nothing collapsed", "spent_s": 0}],
                "skipped": ["heal"],
            }

        t0 = time.time()
        res = healer.run(conn, collapsed[0]["source"], max_calls=LLM_BUDGET)
        out = _outcome("heal", t0, **{k: v for k, v in res.items() if k != "hypotheses"})
        return _return(state, out)

    return node


def prospect_node(conn):
    def node(state: dict) -> dict:
        if (s := _skip(state, "prospect")) is not None:
            return s
        from ..agents import prospector

        t0 = time.time()
        res = prospector.run(conn, max_calls=LLM_BUDGET)
        out = _outcome(
            "prospect",
            t0,
            **{k: v for k, v in res.items() if k not in ("candidates", "proposal_ids")},
        )
        return _return(state, out)

    return node


"""
The node table the graph is built from. The policy's `available` set is
derived from exactly this, which is what made rules 2 and 7 declared-but-
unreachable until now: adding the agents here is what switches them on.
"""
BUILDERS = {
    "discover": discover_node,
    "validate": validate_node,
    "resolve": resolve_node,
    "classify": classify_node,
    "heal": heal_node,
    "prospect": prospect_node,
}
