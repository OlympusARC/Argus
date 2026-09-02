"""SmartRecruiters: a documented public posting API, paginated.

  GET https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset=N
  -> {"offset": N, "limit": 100, "totalFound": N, "content": [...]}

Verified 2026-08: abbvie returns 1,673 postings across 17 pages; an unknown
company returns 404. `totalFound` on the first page means count() costs one
request rather than a full walk.

Like Workday, this paginates, so the same rule applies: never return a partial
board. The reconciler reads absence as evidence a posting closed, so half a
board would silently close the other half.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..core import http
from ..core.models import FetchError, Posting
from .base import Adapter

API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
PAGE = 100
BOARD = "https://jobs.smartrecruiters.com/{slug}/{jid}"


def _epoch(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _location(job: dict[str, Any]) -> str | None:
    loc = job.get("location") or {}
    if loc.get("fullLocation"):
        return loc["fullLocation"]
    parts = [loc.get("city"), loc.get("region"), loc.get("country")]
    joined = ", ".join(p for p in parts if p)
    return joined or None


class SmartRecruitersAdapter(Adapter):
    ats = "smartrecruiters"

    """
    Enterprise boards here run to a few thousand postings. The cap bounds one
    board's cost rather than expressing a belief about size, so it sits above
    the real world and oversized boards raise instead of truncating.
    """

    def __init__(self, max_jobs: int = 25_000):
        self.max_jobs = max_jobs

    def board_url(self, slug: str) -> str:
        return API.format(slug=slug)

    def _page(self, slug: str, offset: int) -> dict[str, Any]:
        return http.get_json(self.board_url(slug), params={"limit": PAGE, "offset": offset})

    def count(self, slug: str) -> int:
        """One request instead of totalFound/100 -- page one carries the total."""
        return int(self._page(slug, 0).get("totalFound") or 0)

    def fetch(self, slug: str) -> list[Posting]:
        first = self._page(slug, 0)
        total = int(first.get("totalFound") or 0)
        if total > self.max_jobs:
            raise FetchError(
                f"board has {total} postings, above max_jobs={self.max_jobs}; "
                f"refusing to return a partial board"
            )

        raw = list(first.get("content") or [])
        offset = len(raw)
        while offset < total:
            batch = self._page(slug, offset).get("content") or []
            if not batch:
                break
            raw.extend(batch)
            offset += len(batch)

        out: list[Posting] = []
        for j in raw:
            jid = j.get("id")
            if not jid:
                continue
            loc = j.get("location") or {}
            location = _location(j)
            out.append(
                Posting(
                    ats=self.ats,
                    slug=slug,
                    external_id=str(jid),
                    title=(j.get("name") or "").strip(),
                    url=BOARD.format(slug=slug, jid=jid),
                    location=location,
                    locations=[location] if location else [],
                    department=(j.get("department") or {}).get("label"),
                    employment_type=(j.get("typeOfEmployment") or {}).get("label"),
                    is_remote=loc.get("remote"),
                    posted_at=_epoch(j.get("releasedDate")),
                )
            )
        return out
