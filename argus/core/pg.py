"""Postgres behind the same interface as sqlite3.

The codebase writes hand-rolled SQL with `?` placeholders and reads rows with
`row["column"]`. Rather than rewrite 85 call sites or take on an ORM, this
wraps psycopg thinly enough that the same statements run on both engines.

Everything the wrapper has to do is a consequence of one decision made much
earlier: keep the SQL portable. `ON CONFLICT`, `COALESCE` and
`SUM(CASE WHEN ...)` are already understood by both, so what is left is
mechanical -- placeholder style, and a cursor that behaves like sqlite3's
connection-level execute().

What this is NOT is a dialect translator. If a statement needs different SQL
per engine, that belongs in the caller as an explicit branch, not hidden here.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

"""
`?` inside a string literal is data, not a placeholder. Splitting on quotes
and only rewriting the parts outside them is cruder than a parser and
sufficient: the SQL here has no dollar-quoting and no escaped quotes inside
literals.
"""
_LITERAL = re.compile(r"('[^']*')")


def to_pyformat(sql: str) -> str:
    """Rewrite `?` placeholders to psycopg's `%s`, leaving literals alone."""
    parts = _LITERAL.split(sql)
    for i in range(0, len(parts), 2):
        parts[i] = parts[i].replace("?", "%s").replace("%%s", "%s")
    return "".join(parts)


class Cursor:
    """A psycopg cursor that answers to sqlite3's habits."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql: str, params: tuple | list = ()) -> Cursor:
        """
        sqlite3 accepts bare BEGIN/COMMIT/ROLLBACK on a connection in
        autocommit mode. psycopg manages transactions itself, so those become
        no-ops rather than errors -- the calling code brackets its writes and
        that bracketing stays meaningful on SQLite.
        """
        stripped = sql.strip().upper()
        if stripped in ("BEGIN", "COMMIT", "ROLLBACK"):
            return self
        self._cur.execute(to_pyformat(sql), tuple(params))
        return self

    def executemany(self, sql: str, rows) -> Cursor:
        rows = [tuple(r) for r in rows]
        if rows:
            self._cur.executemany(to_pyformat(sql), rows)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self):
        """
        Postgres has no lastrowid. Callers that need a generated id use
        RETURNING and read it from the row instead; this exists only so the
        attribute access does not explode.
        """
        return None


"""
A poll holds one connection for its whole run -- twenty minutes for a large
ATS -- and spends nearly all of it waiting on HTTP rather than on the
database. Supabase's pooler reclaims a session that sits idle that long, so
the next write finds a closed socket and the run dies partway through:

    psycopg.OperationalError: consuming input failed:
        server closed the connection unexpectedly

Measured: of nine parallel polls, the five short ones finished and the four
longest died, greenhouse at 1,861 boards of 5,111.

Reconnecting is safe here because every statement this wrapper carries is
idempotent -- upserts, and UPDATEs qualified by a key. A retried write does
what the first attempt would have. It is deliberately one retry: a genuinely
unreachable database should fail the run, not spin.

Only for a connection that is gone. A syntax error or a constraint violation
raises unchanged, because retrying those just produces the same error twice.
"""


def _is_disconnect(exc: Exception) -> bool:
    import psycopg

    if not isinstance(exc, psycopg.OperationalError):
        return False
    text = str(exc).lower()
    return any(
        s in text
        for s in (
            "server closed the connection",
            "connection is closed",
            "consuming input failed",
            "ssl connection has been closed",
            "terminating connection",
            "broken pipe",
        )
    )


class Connection:
    """Connection-level execute(), the way sqlite3 offers it."""

    def __init__(self, raw, url: str | None = None, autocommit: bool = True):
        self._raw = raw
        self._url = url
        self._autocommit = autocommit

    def _reconnect(self) -> None:
        import psycopg
        from psycopg.rows import dict_row

        if not self._url:
            raise
        """
        The old socket is already gone; closing is tidiness, and failing to
        close something that is not there must not mask the reconnect.
        """
        with contextlib.suppress(Exception):
            self._raw.close()
        self._raw = psycopg.connect(
            self._url, row_factory=dict_row, autocommit=self._autocommit
        )

    def _retrying(self, fn):
        try:
            return fn()
        except Exception as exc:
            if not _is_disconnect(exc):
                raise
            self._reconnect()
            return fn()

    def execute(self, sql: str, params: tuple | list = ()) -> Cursor:
        return self._retrying(lambda: Cursor(self._raw.cursor()).execute(sql, params))

    def executescript(self, sql: str) -> None:
        def once():
            with self._raw.cursor() as cur:
                cur.execute(sql)

        self._retrying(once)

    def executemany(self, sql: str, rows) -> Cursor:
        rows = list(rows)
        return self._retrying(lambda: Cursor(self._raw.cursor()).executemany(sql, rows))

    def cursor(self) -> Cursor:
        return Cursor(self._raw.cursor())

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()

    @property
    def raw(self) -> Any:
        """The psycopg connection, for the places that need COPY or RETURNING."""
        return self._raw


def connect(url: str, *, autocommit: bool = True) -> Connection:
    import psycopg
    from psycopg.rows import dict_row

    raw = psycopg.connect(url, row_factory=dict_row, autocommit=autocommit)
    """
    The url is kept so the connection can rebuild itself; see Connection.
    """
    return Connection(raw, url=url, autocommit=autocommit)
