"""The policy and the loop.

The policy is a pure function over a dict, so most of this needs no database,
no graph and no network -- which is the point of keeping the intelligence in
a function rather than in the framework.
"""

import pytest

from argus.core import db
from argus.orchestrator import graph, measure, orchestrate, plan, policy

"""
The policy is pure Python and always testable. The graph needs langgraph,
which is an optional extra so that a runner doing nothing but poll installs
nothing extra -- so the tests that build a graph skip rather than fail when
it is absent. CI installs it; a contributor running `pip install -e .[dev]`
still gets every policy test.
"""


def _needs_langgraph():
    pytest.importorskip("langgraph", reason="optional [orchestrator] extra")


ALL = {"discover", "validate", "resolve", "classify"}


def snap(**kw):
    """A snapshot with everything quiet, overridden per test."""
    base = {
        "budget_s": 2700,
        "spent_s": 0,
        "collapsed": [],
        "unvalidated": 0,
        "active_boards": 7_645,
        "stale_classification": 0,
        "unresolved_companies": 0,
        "open_jobs": 500_000,
        "hours_since_discover": 1.0,
        "marginal_yield_7d": 5_000,
    }
    base.update(kw)
    return base


def test_a_spent_budget_ends_the_run():
    action, why = policy.decide(snap(spent_s=2700), available=ALL)
    assert action == policy.END and "budget" in why


def test_nothing_to_do_ends_early():
    """Doing nothing is a valid outcome, and cheaper than inventing work."""
    action, why = policy.decide(snap(), available=ALL)
    assert action == policy.END and why == "nothing worth doing"


def test_a_validate_backlog_outranks_routine_discovery():
    """13,635 boards are inbound from the Common Crawl fix. Probing what we
    have beats finding more we cannot vouch for."""
    action, _ = policy.decide(snap(unvalidated=1_864, hours_since_discover=48), available=ALL)
    assert action == "validate"


def test_a_collapsed_source_outranks_everything_but_the_budget():
    """Finding more boards with a broken source is how you get a month of
    quiet weeks."""
    s = snap(
        collapsed=[{"source": "commoncrawl", "reason": "0 new vs median 13000"}],
        unvalidated=9_999,
        hours_since_discover=99,
    )
    action, why = policy.decide(s, available=ALL | {"heal"})
    assert action == "heal" and "commoncrawl" in why


def test_an_unbuilt_node_is_reported_and_skipped_not_routed_to():
    """B5's agents are declared in the policy from B2. Until they exist the
    rule must fall through -- silently, and the fact must be visible."""
    s = snap(collapsed=[{"source": "commoncrawl", "reason": "broken"}], unvalidated=2_000)
    action, _ = policy.decide(s, available=ALL)
    assert action == "validate", "falls through to the next buildable rule"
    assert any("collapsed_source" in x for x in s["_skipped_rules"])


def test_discover_fires_when_due_and_nothing_is_more_urgent():
    action, _ = policy.decide(snap(hours_since_discover=25), available=ALL)
    assert action == "discover"


def test_discover_does_not_fire_twice_in_a_day():
    action, _ = policy.decide(snap(hours_since_discover=3), available=ALL)
    assert action == policy.END


def test_rule_order_is_the_priority():
    """Every rule firing at once must still yield the most urgent one."""
    s = snap(
        unvalidated=9_999,
        stale_classification=200_000,
        unresolved_companies=9_999,
        hours_since_discover=99,
        marginal_yield_7d=0,
    )
    assert policy.decide(s, available=ALL)[0] == "validate"


def test_explain_covers_every_rule():
    lines = policy.explain(snap(unvalidated=2_000), available=ALL)
    assert len(lines) == len(policy.RULES)
    assert any("->" in x and "validate" in x for x in lines)


"""
Below here the graph is exercised with fake nodes: the loop's behaviour is
what is under test, not the stages, which have their own suites.
"""


@pytest.fixture()
def conn(tmp_path):
    return db.init_db(tmp_path / "t.db")


def fake_builders(cost=600, log=None):
    def make(name):
        def builder(_conn):
            def node(state):
                if log is not None:
                    log.append(name)
                return {
                    "spent_s": state["spent_s"] + cost,
                    "done": [{"task": name, "spent_s": cost}],
                }

            return node

        return builder

    return {n: make(n) for n in ("discover", "validate")}


def test_the_loop_stops_when_the_budget_is_gone(conn, monkeypatch, tmp_path):
    _needs_langgraph()
    log = []
    monkeypatch.setattr(graph, "BUILDERS", fake_builders(cost=600, log=log))
    monkeypatch.setattr(
        measure,
        "snapshot",
        lambda c, budget_s=0, spent_s=0: snap(
            budget_s=budget_s, spent_s=spent_s, unvalidated=9_999
        ),
    )
    res = orchestrate(conn, budget_s=1_800, checkpointer=None)
    assert res["spent_s"] >= 1_800
    assert len(log) == 3, "1800s of budget at 600s a task"


def test_a_node_too_expensive_for_the_time_left_is_skipped_not_started(conn, monkeypatch):
    """The policy decides before a stage runs and cannot know how long it
    takes. Starting discover with 30 seconds left means a killed job
    mid-write, so the node refuses."""
    from argus.orchestrator import nodes

    state = {"budget_s": 2_700, "spent_s": 2_690}
    assert nodes._skip(state, "discover") is not None
    assert nodes._skip({"budget_s": 2_700, "spent_s": 0}, "discover") is None


def test_a_killed_run_resumes_from_its_checkpoint(conn, monkeypatch, tmp_path):
    _needs_langgraph()
    """The one thing LangGraph is here for. A 50-minute Actions ceiling must
    cost the remainder of a plan, not the whole plan."""
    calls = []

    def make(name):
        def builder(_conn):
            def node(state):
                calls.append(name)
                if len(calls) == 2 and not getattr(node, "resumed", False):
                    raise TimeoutError("job cancelled: 50 minute limit")
                return {
                    "spent_s": state["spent_s"] + 600,
                    "done": [{"task": name, "spent_s": 600}],
                }

            return node

        return builder

    monkeypatch.setattr(graph, "BUILDERS", {"validate": make("validate")})
    monkeypatch.setattr(
        measure,
        "snapshot",
        lambda c, budget_s=0, spent_s=0: snap(
            budget_s=budget_s, spent_s=spent_s, unvalidated=9_999
        ),
    )
    ck = graph.sqlite_checkpointer(str(tmp_path / "ck.sqlite"))
    with pytest.raises(TimeoutError):
        orchestrate(conn, budget_s=3_000, thread_id="t1", checkpointer=ck)

    done_before = len(calls)
    app, _ = graph.build_graph(conn, checkpointer=ck)
    state = app.get_state({"configurable": {"thread_id": "t1"}})
    assert state.next, "a checkpoint exists with work still pending"
    assert done_before >= 1, "the first task completed before the kill"


def test_plan_reports_without_doing_anything(conn):
    """--dry-run must not write. A policy you cannot inspect before it runs
    is one you have to trust."""
    before = conn.execute("SELECT COUNT(*) n FROM boards").fetchone()["n"]
    snapshot, lines, first = plan(conn, 2_700)
    assert isinstance(snapshot, dict) and lines and isinstance(first, str)
    assert conn.execute("SELECT COUNT(*) n FROM boards").fetchone()["n"] == before


def test_measure_runs_against_an_empty_database(conn):
    """Every count is COALESCEd; a fresh clone must not crash the policy."""
    s = measure.snapshot(conn, budget_s=100, spent_s=0)
    assert s["unvalidated"] == 0 and s["collapsed"] == []
    assert s["hours_since_discover"] is None
    assert policy.decide(s, available=ALL)[0] == "discover", "never discovered"


def test_a_skipped_node_is_never_offered_again(conn, monkeypatch):
    _needs_langgraph()
    """Found on the first real run: skipping costs no time, so the loop chose
    the same unaffordable stage 39 times and only the step ceiling ended it.
    A stage too expensive once is too expensive for the rest of the run --
    the budget only shrinks."""
    from argus.orchestrator import nodes

    seen = []

    def expensive(_conn):
        def node(state):
            seen.append("discover")
            return nodes._skip(state, "discover") or {}

        return node

    monkeypatch.setattr(graph, "BUILDERS", {"discover": expensive})
    monkeypatch.setattr(
        measure,
        "snapshot",
        lambda c, budget_s=0, spent_s=0: snap(
            budget_s=budget_s, spent_s=spent_s, hours_since_discover=99
        ),
    )
    res = orchestrate(conn, budget_s=60, checkpointer=None)
    assert len(seen) == 1, f"offered {len(seen)} times, should be once"
    assert res["skipped"] == ["discover"]


def test_an_agent_declining_internally_also_stops_being_offered(conn, monkeypatch):
    _needs_langgraph()
    """Second live run: the budget check marked its own skips, but an agent
    returning {"skipped": "no LLM provider configured"} produced an outcome
    the graph never registered -- so prospect was offered 39 times. Where the
    refusal comes from must not matter."""
    from argus.orchestrator import nodes

    seen = []

    def declining(_conn):
        def node(state):
            seen.append(1)
            out = nodes._outcome("prospect", __import__("time").time(), skipped="no provider")
            return nodes._return(state, out)

        return node

    monkeypatch.setattr(graph, "BUILDERS", {"prospect": declining})
    monkeypatch.setattr(
        measure,
        "snapshot",
        lambda c, budget_s=0, spent_s=0: snap(
            budget_s=budget_s, spent_s=spent_s, marginal_yield_7d=0
        ),
    )
    res = orchestrate(conn, budget_s=2_700, checkpointer=None)
    assert len(seen) == 1, f"offered {len(seen)} times, should be once"
    assert res["skipped"] == ["prospect"]


def test_the_validate_node_reads_the_fields_the_result_has(conn, monkeypatch):
    """It read `res.live`, which does not exist -- Result has `active`. Every
    ATS that killed no boards therefore reported nothing, and three of four
    validate tasks logged only their duration."""
    from argus.orchestrator import nodes
    from argus.registry import validate as validate_mod

    monkeypatch.setattr(
        validate_mod,
        "run",
        lambda conn, ats, **kw: validate_mod.Result(
            ats=ats, checked=10, active=4, empty=3, dead=2, errored=1
        ),
    )
    node = nodes.BUILDERS["validate"](conn)
    out = node({"budget_s": 2_700, "spent_s": 0})
    done = out["done"][0]
    assert done["active"] > 0, "active must be reported, not a field that does not exist"
    assert done["checked"] > 0 and done["empty"] > 0 and done["errored"] > 0


def test_a_validate_result_names_its_ats():
    """Four of these run in sequence and their lines are read side by side."""
    from argus.registry.validate import Result

    assert Result(ats="breezy", checked=3).line().startswith("breezy")
