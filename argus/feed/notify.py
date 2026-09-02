"""The hourly digest: what appeared since the last time we spoke.

Reads the events the reconciler already writes rather than scanning jobs,
because "new" has to mean *new to Argus* and nothing else can express that.
53% of postings carry no posted_at at all -- BambooHR publishes none -- so a
digest keyed on the posting's own timestamp would be blind to half the
corpus. The events table is the only honest source for "this appeared".

Three things stop it becoming a nuisance.

A watermark on the event id, not a timestamp. Ids are gapless and totally
ordered, so nothing is skipped when two polls land in the same second and
nothing is repeated when the clock moves.

Dedupe on the job, not the event. A posting that closes and comes back emits
`closed` then `reopened`, and a board that flaps emits that pair repeatedly.
The reader wants to hear about a job once.

A flood guard. The first run after a backfill, or the day a large source
lands, can put thousands of rows past the watermark -- 13,635 boards are
inbound from the Common Crawl fix alone. Past a threshold the digest
collapses to a single summary line: it degrades to a headline rather than
posting a thousand embeds.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from ..core import config, http

WATERMARK_KEY = "discord"

"""
Both are worth naming rather than inlining: the first is the only thing
standing between a backfill and a thousand messages, and the second decides
how long a job stays "already announced" once a board starts flapping.
"""
FLOOD_THRESHOLD = int(os.getenv("ARGUS_NOTIFY_FLOOD", "200"))
DEDUPE_WINDOW = int(os.getenv("ARGUS_NOTIFY_DEDUPE_DAYS", "30")) * 86400

"""
Discord caps a webhook at 10 embeds and 6000 characters per message. We stay
well inside both: one embed per company, ten companies, and the rest of the
digest reported as a count.
"""
MAX_EMBEDS = 10
MAX_JOBS_PER_EMBED = 8

SELECT_PENDING = """
SELECT e.id, e.ts, e.type, e.ats, e.slug, e.external_id, e.title, e.url,
       j.is_engineering, j.is_fde, j.role_family, j.seniority, j.location,
       b.company_name
FROM events e
JOIN jobs j
  ON j.ats = e.ats AND j.slug = e.slug AND j.external_id = e.external_id
LEFT JOIN boards b
  ON b.ats = e.ats AND b.slug = e.slug
WHERE e.id > ?
  AND e.type IN ('new', 'reopened')
  AND (j.is_engineering = 1 OR j.is_fde = 1)
  AND NOT EXISTS (
      SELECT 1 FROM notified_jobs n
      WHERE n.ats = e.ats AND n.slug = e.slug AND n.external_id = e.external_id
        AND n.notified_at > ?
  )
ORDER BY e.id
"""


@dataclass
class Digest:
    """What one tick would say. Built before anything is sent, so it can be
    rendered for review without touching the network or the watermark."""

    rows: list[dict] = field(default_factory=list)
    pending: int = 0
    flooded: bool = False
    from_id: int = 0
    to_id: int = 0

    @property
    def sending(self) -> int:
        return 0 if self.flooded else len(self.rows)

    @property
    def fde(self) -> int:
        return sum(1 for r in self.rows if r["is_fde"])


def now() -> int:
    return int(time.time())


def _watermark(conn) -> int | None:
    """None means never run. That distinction is load-bearing -- see build()."""
    row = conn.execute(
        "SELECT last_event_id FROM notifier_state WHERE key = ?", (WATERMARK_KEY,)
    ).fetchone()
    return None if row is None else int(row["last_event_id"])


def _max_event_id(conn) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()
    return int(row["m"])


def set_watermark(conn, event_id: int) -> None:
    conn.execute(
        """INSERT INTO notifier_state (key, last_event_id, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT (key) DO UPDATE SET last_event_id = ?, updated_at = ?""",
        (WATERMARK_KEY, event_id, now(), event_id, now()),
    )
    conn.commit()


def build(conn, since_id: int | None = None) -> Digest:
    """Collect what this tick would announce. Reads only.

    On a database that has never notified, the watermark is seeded at the
    current maximum and nothing is sent. Starting from zero would replay the
    entire history -- 374,647 events, of which 87,933 are engineering -- as
    though every one had just appeared. A digest's first duty is to be
    trustworthy about the word "new".
    """
    mark = since_id if since_id is not None else _watermark(conn)
    if mark is None:
        top = _max_event_id(conn)
        return Digest(rows=[], pending=0, from_id=top, to_id=top)

    cutoff = now() - DEDUPE_WINDOW
    rows = [dict(r) for r in conn.execute(SELECT_PENDING, (mark, cutoff))]
    d = Digest(
        rows=rows,
        pending=len(rows),
        flooded=len(rows) > FLOOD_THRESHOLD,
        from_id=mark,
        to_id=max([r["id"] for r in rows], default=_max_event_id(conn)),
    )
    if d.flooded:
        return d
    """
    Newest first for the reader, after the watermark has been taken from the
    original ordering. Ordering the query itself would put the cursor at the
    wrong end.
    """
    d.rows.reverse()
    return d


def _job_line(r: dict) -> str:
    bits = []
    if r["is_fde"]:
        bits.append("FDE")
    if r["seniority"]:
        bits.append(str(r["seniority"]))
    if r["location"]:
        bits.append(str(r["location"])[:40])
    tail = f"  ·  {' · '.join(bits)}" if bits else ""
    title = (r["title"] or "?")[:90]
    return f"[{title}]({r['url']}){tail}" if r["url"] else f"{title}{tail}"


def render(d: Digest) -> dict | None:
    """The Discord payload, or None when there is nothing to say.

    Silence is a valid outcome and the common one at 3am -- an empty digest
    posts nothing rather than an hourly "no new jobs".
    """
    if d.flooded:
        return {
            "content": (
                f"**{d.pending:,} new engineering roles** since the last digest "
                f"(events {d.from_id + 1}–{d.to_id}).\n"
                f"That is past the {FLOOD_THRESHOLD} flood threshold, so this is a "
                f"summary rather than {d.pending:,} listings — "
                f"`argus events --type new` to read them."
            )
        }
    if not d.rows:
        return None

    by_company: dict[str, list[dict]] = {}
    for r in d.rows:
        who = r["company_name"] or r["slug"] or "unknown"
        by_company.setdefault(str(who), []).append(r)

    """
    Companies with the most openings first: a company posting six roles is
    more interesting than six companies posting one.
    """
    ordered = sorted(by_company.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))
    embeds = []
    for who, jobs in ordered[:MAX_EMBEDS]:
        shown = jobs[:MAX_JOBS_PER_EMBED]
        body = "\n".join(_job_line(r) for r in shown)
        if len(jobs) > len(shown):
            body += f"\n_+{len(jobs) - len(shown)} more_"
        embeds.append({"title": who[:200], "description": body[:4000], "color": 0x0F766E})

    head = f"**{len(d.rows)} new** engineering role{'s' if len(d.rows) != 1 else ''}"
    if d.fde:
        head += f" · {d.fde} FDE"
    hidden = len(ordered) - len(embeds)
    if hidden > 0:
        head += f" · {hidden} more compan{'ies' if hidden != 1 else 'y'} not shown"
    return {"content": head, "embeds": embeds}


def mark_notified(conn, rows: list[dict]) -> None:
    if not rows:
        return
    ts = now()
    conn.executemany(
        """INSERT INTO notified_jobs (ats, slug, external_id, notified_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT (ats, slug, external_id)
           DO UPDATE SET notified_at = ?""",
        [(r["ats"], r["slug"], r["external_id"], ts, ts) for r in rows],
    )


def send(payload: dict, url: str) -> None:
    """A webhook answers 204 with no body, so this must not demand JSON."""
    http.post_nobody(url, json=payload)


def prune(conn) -> int:
    """Drop dedupe rows past the window.

    Without this the table grows by roughly the digest's daily volume
    forever -- ~720 rows a day -- to hold facts that stopped mattering after
    thirty days. Pruning here rather than in a separate chore because the
    notifier is the only writer and already runs hourly.
    """
    cur = conn.execute(
        "DELETE FROM notified_jobs WHERE notified_at < ?", (now() - DEDUPE_WINDOW,)
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def run(
    conn, *, url: str | None = None, since_id: int | None = None, dry: bool = False
) -> Digest:
    """One tick. Nothing is recorded unless the send succeeded.

    The watermark advances on a successful post and on a genuinely empty
    window, but never on a failed one -- a dropped webhook means the next
    tick repeats the attempt rather than losing the jobs.
    """
    d = build(conn, since_id=since_id)
    payload = render(d)

    if dry or since_id is not None:
        return d

    if payload is not None:
        send(payload, url or config.DISCORD_WEBHOOK or "")
        if not d.flooded:
            mark_notified(conn, d.rows)
    prune(conn)
    set_watermark(conn, d.to_id)
    return d


def render_text(d: Digest) -> str:
    """The same digest as plain text, for reviewing a run before it is wired
    to a webhook. Deliberately shows what would be posted, not a summary of
    it."""
    payload = render(d)
    out = [
        f"watermark {d.from_id} -> {d.to_id}   pending {d.pending}   "
        f"sending {d.sending}   fde {d.fde}" + ("   FLOODED" if d.flooded else "")
    ]
    if payload is None:
        out.append("\n(nothing to send -- no message would be posted)")
        return "\n".join(out)
    out.append("")
    out.append(payload["content"])
    for e in payload.get("embeds", []):
        out.append(f"\n  ── {e['title']}")
        for line in e["description"].split("\n"):
            out.append(f"     {line}")
    out.append("")
    out.append(
        f"payload: {len(json.dumps(payload)):,} bytes, {len(payload.get('embeds', []))} embeds"
    )
    return "\n".join(out)
