"""Ashby: one unauthenticated GET returns the entire board.

  GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
  -> {"jobs": [...], "apiVersion": "1"}

Verified behaviour (2026-08): unknown slug -> 404; slug is case-insensitive;
no pagination, no auth, no documented rate limit.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..core import http
from ..core.models import Posting
from .base import Adapter

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def _epoch(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _locations(job: dict[str, Any]) -> list[str]:
    out = []
    for loc in [job.get("location")] + [
        s.get("location") for s in (job.get("secondaryLocations") or [])
    ]:
        if loc and loc not in out:
            out.append(loc)
    return out


class AshbyAdapter(Adapter):
    ats = "ashby"

    def board_url(self, slug: str) -> str:
        return API.format(slug=slug)

    def fetch(self, slug: str) -> list[Posting]:
        data = http.get_json(self.board_url(slug), params={"includeCompensation": "true"})
        jobs = (data or {}).get("jobs")
        if jobs is None:
            from ..core.models import FetchError

            raise FetchError("response had no 'jobs' key")

        out: list[Posting] = []
        for j in jobs:
            """
            isListed=False means Ashby is serving it but the board hides it;
            treating it as present would resurrect jobs the company unlisted.
            """
            if j.get("isListed") is False:
                continue
            jid = j.get("id")
            if not jid:
                continue
            out.append(
                Posting(
                    ats=self.ats,
                    slug=slug,
                    external_id=str(jid),
                    title=(j.get("title") or "").strip(),
                    url=j.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{jid}",
                    apply_url=j.get("applyUrl"),
                    location=j.get("location"),
                    locations=_locations(j),
                    department=j.get("department"),
                    team=j.get("team"),
                    employment_type=j.get("employmentType"),
                    workplace_type=j.get("workplaceType"),
                    is_remote=j.get("isRemote"),
                    posted_at=_epoch(j.get("publishedAt")),
                    compensation=j.get("compensation"),
                    # descriptionHtml is the bulk of the payload and we do not
                    # use it yet; dropping it keeps the DB an order of magnitude
                    # smaller and keeps content_hash stable against reformatting.
                    raw={
                        k: v
                        for k, v in j.items()
                        if k not in ("descriptionHtml", "descriptionPlain")
                    },
                )
            )
        return out
