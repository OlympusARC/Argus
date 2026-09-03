"""The Postgres wrapper, and the connection it has to survive losing.

These do not need a database: the reconnect logic is a decision about which
exceptions mean "the socket is gone", and that is testable with a fake.
"""

import pytest

from argus.core import pg


class Boom(Exception):
    pass


def test_placeholders_outside_literals_only():
    """`?` inside a string literal is data. Rewriting it corrupts the query."""
    assert pg.to_pyformat("SELECT * FROM t WHERE a=? AND b=?") == (
        "SELECT * FROM t WHERE a=%s AND b=%s"
    )
    assert pg.to_pyformat("SELECT '?' AS q WHERE a=?") == "SELECT '?' AS q WHERE a=%s"


def test_only_a_lost_connection_counts_as_one():
    """A syntax error retried is the same syntax error twice."""
    import psycopg

    gone = psycopg.OperationalError("consuming input failed: server closed the connection")
    assert pg._is_disconnect(gone)
    assert pg._is_disconnect(psycopg.OperationalError("SSL connection has been closed"))
    assert not pg._is_disconnect(psycopg.OperationalError("password authentication failed"))
    assert not pg._is_disconnect(Boom("anything else"))


class FakeCursor:
    def execute(self, sql, params=()):
        return self

    def executemany(self, sql, rows):
        return self


class FakeRaw:
    """Fails the first N cursor() calls the way a dropped socket does."""

    def __init__(self, fail_times=0, exc=None):
        self.fail_times = fail_times
        self.exc = exc
        self.cursors = 0
        self.closed = 0

    def cursor(self):
        self.cursors += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.exc
        return FakeCursor()

    def close(self):
        self.closed += 1


def test_a_dropped_connection_is_rebuilt_once(monkeypatch):
    """A poll holds one connection for twenty minutes and spends nearly all
    of it on HTTP. The pooler reclaims the idle session, and the next write
    finds a closed socket -- which killed four of nine polls at greenhouse
    1,861 boards of 5,111."""
    import psycopg

    raw = FakeRaw(fail_times=1, exc=psycopg.OperationalError("server closed the connection"))
    conn = pg.Connection(raw, url="postgresql://x/y")

    rebuilt = []
    monkeypatch.setattr(conn, "_reconnect", lambda: rebuilt.append(1))
    conn.execute("SELECT 1")
    assert rebuilt == [1], "reconnected exactly once"
    assert raw.cursors == 2, "the statement was retried after reconnecting"


def test_it_does_not_spin(monkeypatch):
    """One retry. A database that is genuinely unreachable should fail the
    run rather than loop."""
    import psycopg

    exc = psycopg.OperationalError("server closed the connection")
    raw = FakeRaw(fail_times=9, exc=exc)
    conn = pg.Connection(raw, url="postgresql://x/y")
    monkeypatch.setattr(conn, "_reconnect", lambda: None)

    with pytest.raises(psycopg.OperationalError):
        conn.execute("SELECT 1")
    assert raw.cursors == 2, "tried twice, then gave up"


def test_an_ordinary_error_is_not_retried():
    raw = FakeRaw(fail_times=1, exc=Boom("constraint violation"))
    conn = pg.Connection(raw, url="postgresql://x/y")
    with pytest.raises(Boom):
        conn.execute("SELECT 1")
    assert raw.cursors == 1, "raised straight through"


def test_executemany_survives_a_drop_too(monkeypatch):
    """Its rows may be a generator, which a retry would find exhausted."""
    import psycopg

    raw = FakeRaw(fail_times=1, exc=psycopg.OperationalError("connection is closed"))
    conn = pg.Connection(raw, url="postgresql://x/y")
    monkeypatch.setattr(conn, "_reconnect", lambda: None)
    conn.executemany("UPDATE t SET a=? WHERE b=?", (x for x in [(1, 2), (3, 4)]))
    assert raw.cursors == 2
