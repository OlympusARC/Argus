"""The boundary between what a model says and what the system does.

These are the tests that make an unreliable proposer safe to run. If any of
them fails, the answer is not to loosen the test.
"""

import pytest

from argus.core import db
from argus.proposals import (
    ACCEPTED,
    AUTO_APPLIED,
    PENDING,
    REJECTED,
    apply,
    by_status,
    file,
    gate,
    get,
    reject,
)
from argus.proposals import gates as gates_mod


@pytest.fixture()
def conn(tmp_path):
    c = db.init_db(tmp_path / "t.db")
    for i, (title, fam) in enumerate(
        [
            ("Backend Engineer", "engineering"),
            ("Account Executive", "other"),
            ("Staff Software Engineer", "engineering"),
            ("Registered Nurse", "other"),
            ("Malware Analyst", "other"),
        ]
    ):
        c.execute(
            """INSERT INTO jobs (ats, slug, external_id, title, role_family,
                                 first_seen_at, last_seen_at, status)
               VALUES ('ashby','acme',?,?,?,0,0,'open')""",
            (f"j{i}", title, fam),
        )
    return c


def test_filing_a_proposal_writes_nothing_else(conn):
    """The core claim of the whole design: an agent's output is one row."""
    before = {
        t: conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]  # noqa: S608
        for t in ("boards", "companies", "jobs", "events")
    }
    file(conn, "prospector", "source", {"url": "https://example.com"})
    after = {
        t: conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]  # noqa: S608
        for t in ("boards", "companies", "jobs", "events")
    }
    assert before == after


def test_a_source_yielding_nothing_new_is_rejected_with_evidence(conn, monkeypatch):
    """Rejection is measured, not judged. However convincing the model's
    reasoning, zero new boards is zero."""
    monkeypatch.setattr(gates_mod, "_gate_source", gates_mod._gate_source)  # keep the real one
    monkeypatch.setattr("argus.core.http.get_text", lambda *a, **k: "<html>nothing here</html>")
    pid = file(conn, "prospector", "source", {"url": "https://example.com"})
    assert gate(conn, pid) == REJECTED
    assert get(conn, pid)["evidence"]["why"]


def test_a_high_yield_source_auto_applies_and_ingests(conn, monkeypatch):
    html = " ".join(f'<a href="https://jobs.ashbyhq.com/co{i}">x</a>' for i in range(40))
    monkeypatch.setattr("argus.core.http.get_text", lambda *a, **k: html)
    pid = file(conn, "prospector", "source", {"url": "https://example.com"})
    assert gate(conn, pid) == AUTO_APPLIED
    assert conn.execute("SELECT COUNT(*) n FROM boards").fetchone()["n"] == 40


def test_a_low_yield_source_waits_for_a_human(conn, monkeypatch):
    html = '<a href="https://jobs.ashbyhq.com/onlyone">x</a>'
    monkeypatch.setattr("argus.core.http.get_text", lambda *a, **k: html)
    pid = file(conn, "prospector", "source", {"url": "https://example.com"})
    assert gate(conn, pid) == PENDING
    assert conn.execute("SELECT COUNT(*) n FROM boards").fetchone()["n"] == 0, (
        "pending means pending: nothing is written until a human says so"
    )


def test_an_unfetchable_url_is_rejected_not_raised(conn, monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("dns failure")

    monkeypatch.setattr("argus.core.http.get_text", boom)
    pid = file(conn, "prospector", "source", {"url": "https://nope.invalid"})
    assert gate(conn, pid) == REJECTED


def test_a_precise_ruleset_patch_auto_applies(conn):
    """'Malware Analyst' is currently 'other' -- r1 misses it. A pattern that
    catches it and nothing already-labelled is exactly what we want."""
    pid = file(
        conn, "classifier", "ruleset_patch", {"pattern": r"malware", "family": "security"}
    )
    assert gate(conn, pid) == AUTO_APPLIED


def test_an_overreaching_pattern_is_held_for_review(conn):
    """A pattern matching everything would relabel confident rows. Precision
    catches it, and the conflicts are attached so a human can see why."""
    pid = file(conn, "classifier", "ruleset_patch", {"pattern": r"e", "family": "security"})
    assert gate(conn, pid) == PENDING
    assert get(conn, pid)["evidence"]["conflicts"]


def test_an_uncompilable_pattern_is_rejected(conn):
    pid = file(conn, "classifier", "ruleset_patch", {"pattern": r"([unclosed", "family": "x"})
    assert gate(conn, pid) == REJECTED


def test_a_ruleset_patch_never_edits_the_ruleset(conn):
    """Applying records acceptance. Writing the regex into classify/ is a
    commit a human makes -- a model's output must not reach the code path
    that labels every posting."""
    import argus.classify as classify_mod

    before = classify_mod.RULESET
    pid = file(
        conn, "classifier", "ruleset_patch", {"pattern": r"malware", "family": "security"}
    )
    gate(conn, pid)
    assert before == classify_mod.RULESET


def test_a_diagnosis_can_never_auto_apply(conn):
    """No gate is registered for it, so there is no way to say yes. The
    prohibition is structural, not a flag."""
    pid = file(conn, "healer", "diagnosis", {"source": "commoncrawl", "theory": "blocked"})
    assert gate(conn, pid) == PENDING
    assert "diagnosis" not in gates_mod.GATES


def test_a_diagnosis_can_never_be_enacted(conn):
    pid = file(conn, "healer", "diagnosis", {"source": "commoncrawl"})
    with pytest.raises(ValueError, match="no applier"):
        apply(conn, pid, by="human")


def test_there_is_no_gate_for_anything_touching_the_feed(conn):
    """The reconcile path is off-limits by absence, not by a check that could
    be inverted."""
    for kind in ("close_job", "mark_board_dead", "reconcile_patch"):
        assert kind not in gates_mod.GATES
        assert kind not in gates_mod.APPLIERS


def test_a_human_accepting_runs_the_same_code_as_the_gate(conn, monkeypatch):
    html = '<a href="https://jobs.ashbyhq.com/onlyone">x</a>'
    monkeypatch.setattr("argus.core.http.get_text", lambda *a, **k: html)
    pid = file(conn, "prospector", "source", {"url": "https://example.com"})
    assert gate(conn, pid) == PENDING
    apply(conn, pid, by="human")
    assert get(conn, pid)["status"] == ACCEPTED
    assert conn.execute("SELECT COUNT(*) n FROM boards").fetchone()["n"] == 1


def test_rejecting_leaves_the_evidence_readable(conn, monkeypatch):
    monkeypatch.setattr("argus.core.http.get_text", lambda *a, **k: "")
    pid = file(conn, "prospector", "source", {"url": "https://example.com"})
    gate(conn, pid)
    reject(conn, pid)
    p = get(conn, pid)
    assert p["status"] == REJECTED and p["decided_at"]
    assert by_status(conn, REJECTED)
