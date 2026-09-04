"""Re-apply the region rules to postings already stored.

`region` is computed once, at write time, so a change to classify/geo.py
reaches new postings immediately and old ones never. There is no equivalent of
`argus classify` for it -- no column records which rules produced a row -- so
this sweeps the whole table.

It also fills in a Workday location from the posting URL where the ATS
published none: externalPath is /job/<location>/<title>_<req>, and 7,128 open
Workday postings had no locationsText while 99% of them carried that segment.
Doing it here as well as in the adapter is what lets stored rows catch up.

    python scripts/reregion.py --dry-run
    python scripts/reregion.py
"""

from __future__ import annotations

import argparse
import re
import time
from collections import Counter

from argus.adapters.workday import _location_from_path
from argus.classify import geo
from argus.core import db

"""
The tenant host varies, so the path is taken from whatever follows the site
segment rather than by splitting on a fixed number of slashes.
"""
_WORKDAY_PATH = re.compile(r"myworkdayjobs\.com/[^/]+(/.*)$")

"""
One statement per batch rather than one per row. The reconciler learned this
the expensive way -- 44,000 rows at a round trip each is tens of minutes, and
at a thousand a statement it is seconds.
"""
BATCH = 1000

UPDATE = """
UPDATE jobs SET location = v.location, region = v.region
FROM (VALUES {values}) AS v(ats, slug, external_id, location, region)
WHERE jobs.ats = v.ats AND jobs.slug = v.slug AND jobs.external_id = v.external_id
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    args = ap.parse_args()

    conn = db.connect()
    rows = conn.execute(
        "SELECT ats, slug, external_id, location, url, region FROM jobs"
    ).fetchall()

    before, after = Counter(), Counter()
    pending, filled = [], 0
    for r in rows:
        before[r["region"]] += 1
        loc = r["location"]
        if not loc and r["ats"] == "workday" and r["url"]:
            m = _WORKDAY_PATH.search(r["url"])
            found = _location_from_path(m.group(1)) if m else None
            if found:
                loc, filled = found, filled + 1
        region = geo.region(loc)
        after[region] += 1
        if region != r["region"] or loc != r["location"]:
            pending.append((r["ats"], r["slug"], r["external_id"], loc, region))

    print(
        f"  {len(rows):,} postings   {len(pending):,} to change   "
        f"{filled:,} locations recovered from a URL"
    )
    print(f"  {'region':10} {'before':>8} {'after':>8}")
    for k in ("us", "europe", "remote", "unknown", "other"):
        print(f"  {k:10} {before.get(k, 0):>8,} {after.get(k, 0):>8,}")

    if args.dry_run:
        print("\n  --dry-run: nothing written")
        return 0
    if not pending:
        return 0

    t0 = time.time()
    for i in range(0, len(pending), BATCH):
        chunk = pending[i : i + BATCH]
        values = ",".join(["(?,?,?,?,?)"] * len(chunk))
        conn.execute(UPDATE.format(values=values), [x for row in chunk for x in row])
        conn.commit()
        print(f"    {min(i + BATCH, len(pending)):,}/{len(pending):,}", flush=True)
    print(f"  wrote {len(pending):,} rows in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
