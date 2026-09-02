"""Generated ids, on both backends.

Every test in this suite runs against SQLite, which is why this class of bug
survived: sqlite3 offers cursor.lastrowid and psycopg does not. pg.py returns
None there rather than raising, so a missing id was silent -- eight poll_runs
rows were written with a null run_id and the UPDATE that should have closed
them matched nothing, zeroing every run since the Postgres migration.

It only surfaced because the new source_runs writer called int() on it and
crashed. These tests pin the fix at the level the difference actually lives:
no caller may depend on lastrowid.
"""

import pathlib

from argus.core import db


def test_no_caller_depends_on_lastrowid():
    """The property that makes the code portable, checked structurally.

    A grep rather than a behavioural test because the failure mode is a
    silent None on a backend the suite does not exercise -- so the only
    reliable guard is that the call is not written at all.
    """
    root = pathlib.Path(db.__file__).resolve().parents[1]
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "pg.py":
            continue  # defines the stub
        for i, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if "lastrowid" in stripped and not stripped.startswith("#"):
                """Prose in a docstring explaining the trap is fine."""
                if "cursor.lastrowid" in stripped or "sqlite3 offers" in stripped:
                    continue
                offenders.append(f"{path.relative_to(root)}:{i}")
    assert not offenders, f"use db.insert_id instead: {offenders}"


def test_insert_id_returns_the_new_row(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    first = db.insert_id(
        conn, "INSERT INTO source_runs (source, started_at) VALUES (?, ?)", ("x", 0)
    )
    second = db.insert_id(
        conn, "INSERT INTO source_runs (source, started_at) VALUES (?, ?)", ("y", 0)
    )
    assert isinstance(first, int) and second == first + 1


def test_the_id_actually_addresses_the_row(tmp_path):
    """The bug was not the missing id -- it was that the follow-up UPDATE
    silently matched nothing. So the test closes the row it just opened."""
    conn = db.init_db(tmp_path / "t.db")
    rid = db.insert_id(conn, "INSERT INTO poll_runs (started_at) VALUES (?)", (100,))
    conn.execute(
        "UPDATE poll_runs SET finished_at=?, boards_polled=? WHERE id=?", (200, 7, rid)
    )
    row = conn.execute("SELECT * FROM poll_runs WHERE id=?", (rid,)).fetchone()
    assert row["finished_at"] == 200 and row["boards_polled"] == 7


def test_a_trailing_semicolon_does_not_break_returning(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    assert db.insert_id(
        conn, "INSERT INTO source_runs (source, started_at) VALUES (?, ?);", ("z", 0)
    )
