"""Load a local SQLite corpus into Postgres.

Written as a script rather than a CLI command because it is not part of the
pipeline: it exists to move an existing corpus once, and to restore one if the
database is ever lost. Keeping it out of `argus` keeps the shipped surface
honest about what runs on a schedule.

Uses COPY rather than INSERT for the same reason batching mattered in the
reconciler -- 375,000 postings at one round trip each is hours, and at one
stream is minutes.

Order matters: companies before boards, because boards.company_id references
them and ids are preserved rather than remapped.

    python scripts/backfill.py data/backfill-source.db --replace
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.core import config, pg  # noqa: E402

"""
Dependency order, and the columns to carry. Listed explicitly rather than
read from the schema so that a column added on one side and not the other is
a loud failure here instead of a silent gap in the copy.
"""
TABLES = [
    (
        "companies",
        "id domain name norm_name website careers_url careers_kind "
        "careers_checked_at first_seen_at source",
    ),
    (
        "boards",
        "ats slug company_name status tier job_count next_poll_at last_polled_at "
        "last_ok_at last_new_at consecutive_failures last_error first_seen_at "
        "website careers_url careers_checked_at company_id",
    ),
    ("board_sources", "ats slug source first_seen_at detail"),
    (
        "jobs",
        "ats slug external_id title location locations_json url posted_at "
        "first_seen_at last_seen_at closed_at status missing_polls content_hash "
        "source role_family is_engineering is_fde seniority classified_by",
    ),
    ("events", "id ts type ats slug external_id title url detail_json"),
    (
        "poll_runs",
        "id started_at finished_at boards_polled boards_failed new_jobs "
        "edited_jobs closed_jobs",
    ),
]

"""
Sequences to fast-forward once explicit ids have been copied in. Without this
the next ordinary INSERT reuses id 1 and collides immediately.
"""
SEQUENCES = ["companies", "events", "poll_runs"]


def copy_table(src: sqlite3.Connection, dst, table: str, columns: list[str], batch: int) -> int:
    cols = ", ".join(columns)
    rows = src.execute(f"SELECT {cols} FROM {table}")  # noqa: S608 -- columns are literals above
    n = 0
    t0 = time.time()
    with dst.raw.cursor() as cur, cur.copy(f"COPY {table} ({cols}) FROM STDIN") as cp:
        while True:
            chunk = rows.fetchmany(batch)
            if not chunk:
                break
            for row in chunk:
                cp.write_row(tuple(row))
            n += len(chunk)
            print(
                f"    {table:<14} {n:>9,} rows  {n / max(time.time() - t0, 1e-9):>8,.0f}/s",
                end="\r",
                file=sys.stderr,
                flush=True,
            )
    print(f"    {table:<14} {n:>9,} rows in {time.time() - t0:>5.0f}s", file=sys.stderr)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path, help="SQLite database to read")
    ap.add_argument(
        "--replace",
        action="store_true",
        help="TRUNCATE the destination tables first. Required: a partial load "
        "on top of existing rows would conflict on every primary key.",
    )
    ap.add_argument("--batch", type=int, default=20_000)
    args = ap.parse_args()

    url = config.database_url()
    if not url:
        print("no database configured (set SUPABASE_DB_PASSWORD)", file=sys.stderr)
        return 2
    if not args.source.exists():
        print(f"no such file: {args.source}", file=sys.stderr)
        return 2

    src = sqlite3.connect(f"file:{args.source}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    version = src.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    print(f"source {args.source} at schema_version {version['value'] if version else '?'}")

    dst = pg.connect(url, autocommit=False)
    try:
        if args.replace:
            names = ", ".join(t for t, _ in TABLES)
            print(f"truncating {names}")
            dst.raw.cursor().execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")

        total = 0
        for table, spec in TABLES:
            total += copy_table(src, dst, table, spec.split(), args.batch)

        for table in SEQUENCES:
            dst.raw.cursor().execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
            )
        dst.raw.commit()
        print(f"\ncopied {total:,} rows")

        print("\nverifying:")
        ok = True
        for table, _ in TABLES:
            want = src.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]  # noqa: S608
            got = dst.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]  # noqa: S608
            mark = "ok " if want == got else "MISMATCH"
            ok &= want == got
            print(f"  {mark} {table:<14} sqlite {want:>9,}   postgres {got:>9,}")
        return 0 if ok else 1
    except Exception:
        dst.raw.rollback()
        raise
    finally:
        dst.close()


if __name__ == "__main__":
    raise SystemExit(main())
