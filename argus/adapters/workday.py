"""Workday: the messiest of the ATSs, and the largest employer surface.

  POST https://{tenant}.{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
       {"appliedFacets":{}, "limit":20, "offset":N, "searchText":""}
  -> {"total": N, "jobPostings":[{title, externalPath, locationsText,
                                  postedOn, bulletFields}]}

Board identity is the (tenant, pod, site) triple, carried in our registry as
the slug "{tenant}.{pod}/{site}".

Verified 2026-08:
  * limit is hard-capped at 20 -- 50 and above return 400. NVIDIA's 2,000
    postings therefore cost 100 requests, against one for Ashby.
  * site names are case-insensitive, so the router's lowercasing is safe.
  * an unknown site returns 404 and an unknown tenant 422; both are permanent.

The critical rule here, which single-request adapters get for free: a
paginated fetch must never return a PARTIAL board. The reconciler treats
absence as evidence that a posting closed, so half a board would silently
close the other half. Any interruption raises instead of returning what we
have, and a board larger than max_jobs raises rather than truncating.
"""

from __future__ import annotations

import re
from typing import Any

from ..core import http
from ..core.models import FetchError, Posting
from .base import Adapter

API = "https://{host}/wday/cxs/{tenant}/{site}/jobs"
PAGE = 20  # server-enforced; larger values 400
_REQ_ID = re.compile(r"_([A-Za-z0-9-]+)$")


def split_slug(slug: str) -> tuple[str, str, str]:
    """'nvidia.wd5/nvidiaexternalcareersite' -> (host, tenant, site)"""
    try:
        hostpart, site = slug.split("/", 1)
        tenant, pod = hostpart.split(".", 1)
    except ValueError as exc:
        raise FetchError(f"malformed workday slug {slug!r}", permanent=True) from exc
    return f"{tenant}.{pod}.myworkdayjobs.com", tenant, site


class WorkdayAdapter(Adapter):
    ats = "workday"

    """
    Enterprise boards are genuinely huge -- oreillyauto carries 18,292
    postings. The cap exists to bound one board's cost, not to express a
    belief about size, so it sits above the real world and such boards are
    demoted to a slower tier instead of being skipped.
    """

    def __init__(self, max_jobs: int = 25_000):
        self.max_jobs = max_jobs

    def board_url(self, slug: str) -> str:
        host, tenant, site = split_slug(slug)
        return API.format(host=host, tenant=tenant, site=site)

    def _page(self, url: str, offset: int) -> dict[str, Any]:
        return http.post_json(
            url,
            json={"appliedFacets": {}, "limit": PAGE, "offset": offset, "searchText": ""},
            headers={"Accept": "application/json"},
        )

    def count(self, slug: str) -> int:
        """One request instead of total/20 -- page one already carries `total`."""
        return int(self._page(self.board_url(slug), 0).get("total") or 0)

    def fetch(self, slug: str) -> list[Posting]:
        host, tenant, site = split_slug(slug)
        url = self.board_url(slug)

        first = self._page(url, 0)
        total = int(first.get("total") or 0)
        if total > self.max_jobs:
            """
            Truncating would let the reconciler close everything past the cut.
            """
            raise FetchError(
                f"board has {total} postings, above max_jobs={self.max_jobs}; "
                f"refusing to return a partial board"
            )

        raw = list(first.get("jobPostings") or [])
        offset = len(raw)
        while offset < total:
            page = self._page(url, offset)  # a failure here propagates
            batch = page.get("jobPostings") or []
            if not batch:
                break  # server stopped early
            raw.extend(batch)
            offset += len(batch)

        out: list[Posting] = []
        for j in raw:
            path = j.get("externalPath") or ""
            bullets = j.get("bulletFields") or []
            """
            bulletFields[0] is the requisition id (e.g. JR2019004) and is the
            most stable identifier Workday exposes; the path tail is a fallback.
            """
            external_id = bullets[0] if bullets else None
            if not external_id:
                m = _REQ_ID.search(path)
                external_id = m.group(1) if m else path
            if not external_id:
                continue
            location = j.get("locationsText")
            """
            Workday often reports "3 Locations" instead of naming them.
            """
            if location and re.fullmatch(r"\d+\s+Locations?", location.strip()):
                location = None
            out.append(
                Posting(
                    ats=self.ats,
                    slug=slug,
                    external_id=str(external_id),
                    title=(j.get("title") or "").strip(),
                    url=f"https://{host}/{site}{path}" if path else f"https://{host}/{site}",
                    location=location,
                    locations=[location] if location else [],
                    # postedOn is relative text ("Posted Today"), not a date, so
                    # there is nothing honest to store as an epoch.
                    posted_at=None,
                    raw={
                        "externalPath": path,
                        "postedOn": j.get("postedOn"),
                        "locationsText": j.get("locationsText"),
                        "bulletFields": bullets,
                    },
                )
            )
        return out
