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
    for _ in range(4):
        record(conn, "good", new_boards=500, refs=1_000)
    record(conn, "good", new_boards=480, refs=1_000)
    for _ in range(4):
        record(conn, "bad", new_boards=500, refs=1_000)
    record(conn, "bad", new_boards=0, refs=1_000)
    assert [t.source for t in runs.collapsed(conn)] == ["bad"]


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
