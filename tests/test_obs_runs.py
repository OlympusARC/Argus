"""Trend detection: what the orchestrator's rule 2 will fire on.

The bar these have to clear is the failure they exist to catch. Common Crawl
swept one host of ten for months and every run looked normal, because nothing
compared a run against its own history. These tests are that comparison.
"""

import pytest

from argus.core import db
from argus.discovery import SourceResult
from argus.obs import runs


@pytest.fixture()
def conn(tmp_path):
    return db.init_db(tmp_path / "t.db")


def record(conn, source, *, new_boards=0, refs=0, blocked=0, skipped=None, error=None):
    rid = runs.start(conn, source)
    runs.finish(
        conn,
        rid,
        SourceResult(
            source,
            refs_seen=refs,
            new_boards=new_boards,
            blocked=blocked,
            skipped=skipped,
            error=error,
        ),
    )


def test_a_collapse_is_flagged(conn):
    """13,635 new boards one night, 0 the next, is the case this exists for."""
    for _ in range(4):
        record(conn, "commoncrawl", new_boards=13_000, refs=19_000)
    record(conn, "commoncrawl", new_boards=0, refs=100)
    t = runs.trend(conn, "commoncrawl")
    assert t.collapsed and "median" in t.reason


def test_a_steady_source_is_not_flagged(conn):
    for n in (600, 700, 650, 642, 610):
        record(conn, "jobarchive", new_boards=n, refs=5_000)
    assert not runs.trend(conn, "jobarchive").collapsed


def test_a_source_that_was_always_small_is_not_a_collapse(conn):
    """github yields single digits now that it is near-exhausted. That is
    exhaustion, not breakage, and healing it would waste a run."""
    for n in (4, 3, 5, 3, 3):
        record(conn, "github", new_boards=n, refs=1_900)
    assert not runs.trend(conn, "github").collapsed


def test_blocked_with_nothing_found_is_a_collapse_without_history(conn):
    """Common Crawl refuses connections rather than answering 429. A run that
    was prevented from looking did not decline to find boards -- and that
    needs no history to judge."""
    record(conn, "commoncrawl", new_boards=0, refs=0, blocked=6)
    t = runs.trend(conn, "commoncrawl")
    assert t.collapsed and "blocked" in t.reason


def test_a_first_quiet_run_is_not_a_collapse(conn):
    """Too little history to judge. A new source's first quiet night must not
    trigger a healer."""
    record(conn, "brandnew", new_boards=0, refs=10)
    assert not runs.trend(conn, "brandnew").collapsed


def test_skipped_runs_are_excluded_from_the_median(conn):
    """A source without an API key reports zero every night. Letting those
    into the history would make a genuine collapse look normal."""
    for _ in range(5):
        record(conn, "websearch", skipped="no BRAVE_API_KEY")
    for _ in range(3):
        record(conn, "websearch", new_boards=400, refs=900)
    record(conn, "websearch", new_boards=0, refs=5)
    t = runs.trend(conn, "websearch")
    assert t.collapsed, "the skipped zeros must not mask the drop"


def test_the_median_resists_one_spike(conn):
    """Yields are spiky -- a monthly HN thread, a new crawl landing. A mean
    would let one good run hide the next collapse."""
    for n in (100, 110, 90, 5_000, 105):
        record(conn, "hn_hiring", new_boards=n, refs=1_600)
    record(conn, "hn_hiring", new_boards=95, refs=1_600)
    assert not runs.trend(conn, "hn_hiring").collapsed


def test_collapsed_lists_only_the_broken(conn):
    """Rewritten with the rule it tests.

    This used to give "bad" healthy refs and zero new boards and expect a
    collapse. That is now saturation -- a working source with nothing left --
    and treating it as a fault flagged three healthy sources in a real corpus.
    A broken source is one that stops fetching.
    """
    for _ in range(4):
        record(conn, "good", new_boards=500, refs=1_000)
    record(conn, "good", new_boards=480, refs=1_000)
    for _ in range(4):
        record(conn, "bad", new_boards=500, refs=1_000)
    record(conn, "bad", new_boards=0, refs=50)
    assert [t.source for t in runs.collapsed(conn)] == ["bad"]


def test_a_source_out_of_things_to_find_is_not_in_that_list(conn):
    """The distinction the rewrite above exists for."""
    for _ in range(4):
        record(conn, "done", new_boards=500, refs=1_000)
    for _ in range(3):
        record(conn, "done", new_boards=0, refs=1_000)
    assert [t.source for t in runs.collapsed(conn)] == []
    assert runs.trend(conn, "done").saturated


def test_a_discover_sweep_writes_a_row(conn, monkeypatch):
    """The whole point of B1: recording happens inside discovery.run(), so no
    caller has to remember."""
    from argus import discovery
    from argus.core.models import BoardRef

    class Fake:
        def available(self):
            return True, ""

        def discover(self):
            yield BoardRef("ashby", "acme", None, "fake", {})

    monkeypatch.setitem(discovery.SOURCES, "fake", lambda **kw: Fake())
    monkeypatch.setattr(discovery, "build", lambda name, **kw: discovery.SOURCES[name]())
    discovery.run(conn, ["fake"])
    row = runs.latest(conn, "fake")
    assert row and row["new_boards"] == 1 and row["finished_at"]


def test_a_dry_run_records_nothing(conn, monkeypatch):
    from argus import discovery
    from argus.core.models import BoardRef

    class Fake:
        def available(self):
            return True, ""

        def discover(self):
            yield BoardRef("ashby", "acme", None, "fake", {})

    monkeypatch.setitem(discovery.SOURCES, "fake", lambda **kw: Fake())
    monkeypatch.setattr(discovery, "build", lambda name, **kw: discovery.SOURCES[name]())
    discovery.run(conn, ["fake"], dry_run=True)
    assert runs.latest(conn, "fake") is None


"""
Collapse is judged on refs, not on boards.

Found by running health against a healthy corpus: it flagged commoncrawl,
github and vcportfolio as collapsed and exited 1. All three were fine. Wiring
that into an hourly workflow would have failed it every hour on working
sources, which is how an alarm gets ignored.
"""


def _run(conn, source, refs, boards, blocked=0, finish=True):
    from argus.discovery import SourceResult

    rid = runs.start(conn, source)
    if finish:
        runs.finish(
            conn,
            rid,
            SourceResult(source, refs_seen=refs, new_boards=boards, blocked=blocked),
        )
    return rid


def test_a_source_that_stops_fetching_has_collapsed(conn):
    """What the original bug looked like: Common Crawl swept one host of ten
    and returned 2,709 refs where widening it yields 19,229."""
    for _ in range(4):
        _run(conn, "commoncrawl", refs=19_000, boards=500)
    _run(conn, "commoncrawl", refs=2_700, boards=0)

    t = runs.trend(conn, "commoncrawl")
    assert t.collapsed and "refs" in t.reason
    assert not t.saturated


def test_a_source_that_fetches_and_finds_nothing_is_saturated(conn):
    """The common case, and the opposite finding. Fetching as much as ever
    and none of it new is what success looks like for a source that has
    already given us everything it has."""
    for _ in range(3):
        _run(conn, "commoncrawl", refs=17_000, boards=800)
    for _ in range(3):
        _run(conn, "commoncrawl", refs=17_000, boards=0)

    t = runs.trend(conn, "commoncrawl")
    assert t.saturated, "healthy refs, nothing new"
    assert not t.collapsed, "and emphatically not a fault"


def test_a_broken_source_is_never_called_merely_saturated(conn):
    """Both look like zero new boards. Only one is a problem, and calling a
    broken source exhausted would retire it instead of fixing it."""
    for _ in range(4):
        _run(conn, "urlscan", refs=900, boards=10)
    for _ in range(3):
        _run(conn, "urlscan", refs=0, boards=0)

    t = runs.trend(conn, "urlscan")
    assert t.collapsed and not t.saturated


def test_an_interrupted_run_is_not_the_current_state(conn):
    """A killed run leaves a row with no finished_at and zeros in every
    counter. Reporting it showed github at 0 refs while its trend read 1,753
    from the last real one -- the table and the verdict disagreed."""
    _run(conn, "github", refs=1_900, boards=20)
    _run(conn, "github", refs=0, boards=0, finish=False)

    assert runs.latest(conn, "github")["refs_seen"] == 1_900
    assert runs.latest(conn, "github", finished_only=False)["refs_seen"] == 0


def test_blocked_and_empty_needs_no_history(conn):
    """Being prevented from looking is a collapse on its own evidence."""
    _run(conn, "commoncrawl", refs=0, boards=0, blocked=9)
    t = runs.trend(conn, "commoncrawl")
    assert t.collapsed and "blocked" in t.reason
