"""What to do next, decided from measured state.

This is the part of the orchestrator that matters, and it is deliberately not
a model. Every rule below came from a real incident in this project, the
thresholds are the numbers those incidents actually produced, and the whole
thing is a pure function over a dict -- so it can be tested without a
database, a graph, or a network.

Order is the design. The rules are tried top to bottom and the first match
wins, which means the ordering encodes priority: a broken source outranks
more discovery, because finding more boards with a broken source is how you
end up with a month of quiet weeks.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

END = "__end__"


@dataclass(frozen=True)
class Rule:
    name: str
    action: str
    why: Callable[[dict], str | None]
    """
    Rules whose node does not exist yet are declared, matched and logged, but
    fall through to the next rule. That keeps the policy's real shape visible
    from the first commit and makes adding the healer an addition rather than
    a rewrite.
    """
    planned: bool = False


def _budget(s: dict) -> str | None:
    if s["spent_s"] >= s["budget_s"]:
        return f"budget spent ({s['spent_s']}s of {s['budget_s']}s)"
    return None


def _collapsed(s: dict) -> str | None:
    bad = s.get("collapsed") or []
    return f"{bad[0]['source']}: {bad[0]['reason']}" if bad else None


def _unvalidated(s: dict) -> str | None:
    n = s.get("unvalidated", 0)
    return f"{n:,} boards unvalidated" if n > 1_500 else None


def _stale_ruleset(s: dict) -> str | None:
    n = s.get("stale_classification", 0)
    return f"{n:,} postings predate the ruleset" if n > 50_000 else None


def _unresolved(s: dict) -> str | None:
    n = s.get("unresolved_companies", 0)
    return f"{n:,} companies without a careers page" if n > 5_000 else None


def _discover_due(s: dict) -> str | None:
    h = s.get("hours_since_discover")
    if h is None:
        return "never discovered"
    return f"{h:.0f}h since last discover" if h >= 24 else None


def _yield_flat(s: dict) -> str | None:
    y = s.get("marginal_yield_7d")
    if y is None:
        return None
    return f"7-day marginal yield {y:,} boards" if y < 100 else None


"""
Tried in order; first match wins. Rules 2 and 7 route to agents that arrive
in B5 -- until then they log what they would have done and fall through.
"""
RULES: list[Rule] = [
    Rule("budget", END, _budget),
    Rule("collapsed_source", "heal", _collapsed, planned=True),
    Rule("validate_backlog", "validate", _unvalidated),
    Rule("stale_ruleset", "classify", _stale_ruleset),
    Rule("resolve_backlog", "resolve", _unresolved),
    Rule("discover_due", "discover", _discover_due),
    Rule("yield_flat", "prospect", _yield_flat, planned=True),
]


def decide(snapshot: dict, *, available: set[str] | None = None) -> tuple[str, str]:
    """Return (next_node, why).

    `available` is the set of node names the graph actually has. A rule whose
    node is missing is reported and skipped rather than routed to, which is
    what lets B2 ship a policy that already knows about B5's agents.
    """
    available = available if available is not None else set()
    for rule in RULES:
        why = rule.why(snapshot)
        if not why:
            continue
        if rule.action == END or rule.action in available:
            return rule.action, why
        """
        Matched, but the node is not built. Say so -- silently falling
        through would hide the fact that work was identified and dropped.
        """
        snapshot.setdefault("_skipped_rules", []).append(f"{rule.name}: {why}")
    return END, "nothing worth doing"


def explain(snapshot: dict, *, available: set[str] | None = None) -> list[str]:
    """Every rule and whether it fires, for `--dry-run`.

    A policy you cannot inspect before it runs is one you have to trust; this
    is what makes it reviewable instead.
    """
    available = available if available is not None else set()
    out = []
    for rule in RULES:
        why = rule.why(snapshot)
        if not why:
            out.append(f"  .  {rule.name:<18} no")
        elif rule.action != END and rule.action not in available:
            out.append(f"  ~  {rule.name:<18} {why}   [{rule.action} not built]")
        else:
            out.append(f"  -> {rule.name:<18} {why}   [{rule.action}]")
    return out
