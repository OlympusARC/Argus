"""The sweep loop, now that it is callable.

These pin the properties that were previously only exercised through the CLI
and would have been easy to lose in the extraction: incremental writes,
surviving a source that dies mid-yield, and honouring the caps.
"""

import pytest

from argus import discovery
from argus.core import db
from argus.core.models import BoardRef, CompanyRef


@pytest.fixture()
def conn(tmp_path):
    return db.init_db(tmp_path / "t.db")


class FakeSource:
    """A source under our control: yields what we say, fails when we say."""

    name = "fake"

    def __init__(self, items=None, fail_after=None):
        self.items = items or []
        self.fail_after = fail_after

    def available(self):
        return True, ""

    def discover(self):
        for i, item in enumerate(self.items):
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("source died")
            yield item


def board(slug, **kw):
    return BoardRef("ashby", slug, kw.pop("company", None), "fake", {})


def install(monkeypatch, src):
    monkeypatch.setitem(discovery.SOURCES, "fake", lambda **kw: src)
    monkeypatch.setattr(discovery, "build", lambda name, **kw: discovery.SOURCES[name](**kw))


def test_a_sweep_writes_boards_and_reports_them(conn, monkeypatch):
    install(monkeypatch, FakeSource([board(f"c{i}") for i in range(5)]))
    (r,) = discovery.run(conn, ["fake"])
    assert (r.refs_seen, r.new_boards, r.error) == (5, 5, None)
    assert conn.execute("SELECT COUNT(*) n FROM boards").fetchone()["n"] == 5


def test_a_dying_source_keeps_what_it_already_yielded(conn, monkeypatch):
    """The reason flush() exists. ycombinator runs 20+ minutes over 6k
    companies; a crash at minute 19 must not discard minutes 1-18."""
    install(monkeypatch, FakeSource([board(f"c{i}") for i in range(10)], fail_after=6))
    (r,) = discovery.run(conn, ["fake"], batch=2)
    assert r.error is not None and "source died" in r.error
    assert conn.execute("SELECT COUNT(*) n FROM boards").fetchone()["n"] == 6, (
        "everything before the failure is written"
    )


def test_a_dying_source_does_not_stop_the_sweep(conn, monkeypatch):
    """Eighteen sources run in one pass; any one of them may be broken."""
    good = FakeSource([board("kept")])
    bad = FakeSource([board("x")], fail_after=0)
    monkeypatch.setitem(discovery.SOURCES, "bad", lambda **kw: bad)
    monkeypatch.setitem(discovery.SOURCES, "good", lambda **kw: good)
    monkeypatch.setattr(discovery, "build", lambda name, **kw: discovery.SOURCES[name]())
    results = discovery.run(conn, ["bad", "good"])
    assert results[0].error is not None
    assert results[1].new_boards == 1


def test_writes_land_incrementally_not_at_the_end(conn, monkeypatch):
    """Asserted by observing the database mid-sweep: at batch=2, boards must
    already exist before the generator is exhausted."""
    seen_midway = []

    class Watching(FakeSource):
        def discover(self):
            for i in range(6):
                if i == 5:
                    seen_midway.append(
                        conn.execute("SELECT COUNT(*) n FROM boards").fetchone()["n"]
                    )
                yield board(f"c{i}")

    install(monkeypatch, Watching())
    discovery.run(conn, ["fake"], batch=2)
    assert seen_midway and seen_midway[0] >= 2, "nothing was written before the end"


def test_a_dry_run_writes_nothing(conn, monkeypatch):
    install(monkeypatch, FakeSource([board(f"c{i}") for i in range(4)]))
    (r,) = discovery.run(conn, ["fake"], dry_run=True)
    assert r.dry and r.dry_refs == 4 and r.dry_unique == 4
    assert conn.execute("SELECT COUNT(*) n FROM boards").fetchone()["n"] == 0


def test_limit_stops_the_sweep(conn, monkeypatch):
    install(monkeypatch, FakeSource([board(f"c{i}") for i in range(100)]))
    (r,) = discovery.run(conn, ["fake"], batch=5, limit=10)
    assert r.refs_seen <= 15, "limit is a bound, not a suggestion"


def test_company_refs_become_companies(conn, monkeypatch):
    install(
        monkeypatch,
        FakeSource([CompanyRef(name="Acme", domain="acme.com", source="fake")]),
    )
    (r,) = discovery.run(conn, ["fake"])
    assert r.new_companies == 1
    assert conn.execute("SELECT COUNT(*) n FROM companies").fetchone()["n"] == 1


def test_an_unavailable_source_is_skipped_not_failed(conn, monkeypatch):
    class Unavailable(FakeSource):
        def available(self):
            return False, "no API key"

    install(monkeypatch, Unavailable())
    (r,) = discovery.run(conn, ["fake"])
    assert r.skipped == "no API key" and r.error is None


def test_an_unknown_source_is_reported_not_raised(conn):
    """argparse blocks this from the CLI, but an orchestrator node calling
    run() directly has no such guard."""
    (r,) = discovery.run(conn, ["no_such_source"])
    assert r.skipped == "unknown source"


def test_on_result_fires_per_source_as_it_finishes(conn, monkeypatch):
    """A full sweep runs for tens of minutes; a caller that only learns the
    outcome at the end cannot report progress."""
    install(monkeypatch, FakeSource([board("a")]))
    monkeypatch.setitem(discovery.SOURCES, "fake2", lambda **kw: FakeSource([board("b")]))
    got = []
    discovery.run(conn, ["fake", "fake2"], on_result=got.append)
    assert [r.source for r in got] == ["fake", "fake2"]
