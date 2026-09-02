"""Workable: the public job-board widget endpoint.

  GET https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true
  -> {"name": "...", "description": "...", "jobs": [...]}

Verified 2026-08: unknown account -> 404, no pagination, and the response
carries the company's own name alongside the postings -- which is worth more
than it looks, since most sources yield a bare slug and the companies table
has to guess a name from it otherwise.
"""

from __future__ import annotations

from datetime import date

from ..core import http
from ..core.models import FetchError, Posting
from .base import Adapter

API = "https://apply.workable.com/api/v1/widget/accounts/{slug}"


def _epoch(value: str | None) -> int | None:
    """published_on is a bare date, not a timestamp."""
    if not value:
        return None
    try:
        d = date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None
    import calendar

    return calendar.timegm(d.timetuple())


def _locations(job: dict) -> list[str]:
    out: list[str] = []
    for loc in job.get("locations") or []:
        parts = [loc.get("city"), loc.get("region"), loc.get("country")]
        joined = ", ".join(p for p in parts if p)
        if joined and joined not in out:
            out.append(joined)
    if not out:
        parts = [job.get("city"), job.get("state"), job.get("country")]
        joined = ", ".join(p for p in parts if p)
        if joined:
            out.append(joined)
    return out


class WorkableAdapter(Adapter):
    ats = "workable"

    def board_url(self, slug: str) -> str:
        return API.format(slug=slug)

    def company_name(self, slug: str) -> str | None:
        """The account's own name, for the companies registry."""
        data = http.get_json(self.board_url(slug), params={"details": "true"})
        return (data or {}).get("name")

    def fetch(self, slug: str) -> list[Posting]:
        data = http.get_json(self.board_url(slug), params={"details": "true"})
        jobs = (data or {}).get("jobs")
        if jobs is None:
            raise FetchError("response had no 'jobs' key")

        out: list[Posting] = []
        for j in jobs:
            """
            shortcode is the stable public identifier and the one that appears
            in the posting URL; `code` is the employer's own requisition
            reference and is frequently absent.
            """
            jid = j.get("shortcode")
            if not jid:
                continue
            locs = _locations(j)
            out.append(
                Posting(
                    ats=self.ats,
                    slug=slug,
                    external_id=str(jid),
                    title=(j.get("title") or "").strip(),
                    url=j.get("url") or j.get("shortlink") or "",
                    apply_url=j.get("application_url"),
                    location=locs[0] if locs else None,
                    locations=locs,
                    department=j.get("department"),
                    employment_type=j.get("employment_type"),
                    is_remote=j.get("telecommuting"),
                    posted_at=_epoch(j.get("published_on") or j.get("created_at")),
                )
            )
        return out
