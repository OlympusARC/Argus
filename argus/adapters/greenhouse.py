"""Greenhouse: one unauthenticated GET returns the whole board.

  GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
  -> {"jobs": [...], "meta": {...}}

Verified 2026-08: unknown slug -> 404, no auth, no pagination (stripe returns
580 jobs in one response).

Deliberately does NOT pass content=true. That flag adds departments and
offices, but also the full HTML description for every posting -- roughly 3 MB
per large board, which across ~1,700 boards on an hourly cadence is several
gigabytes an hour for fields we drop before storing anyway.
"""

from __future__ import annotations

from datetime import datetime

from ..core import http
from ..core.models import FetchError, Posting
from .base import Adapter

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


def _epoch(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except (ValueError, TypeError):
        return None


class GreenhouseAdapter(Adapter):
    ats = "greenhouse"

    def board_url(self, slug: str) -> str:
        return API.format(slug=slug)

    def fetch(self, slug: str) -> list[Posting]:
        data = http.get_json(self.board_url(slug))
        jobs = (data or {}).get("jobs")
        if jobs is None:
            raise FetchError("response had no 'jobs' key")

        out: list[Posting] = []
        for j in jobs:
            jid = j.get("id")
            if not jid:
                continue
            location = (j.get("location") or {}).get("name")
            out.append(
                Posting(
                    ats=self.ats,
                    slug=slug,
                    external_id=str(jid),
                    title=(j.get("title") or "").strip(),
                    url=j.get("absolute_url") or "",
                    location=location,
                    locations=[location] if location else [],
                    # first_published is when it went live; updated_at moves on
                    # no-op republishes and is deliberately not hashed.
                    posted_at=_epoch(j.get("first_published") or j.get("updated_at")),
                    raw={k: v for k, v in j.items() if k not in ("content", "data_compliance")},
                )
            )
        return out
