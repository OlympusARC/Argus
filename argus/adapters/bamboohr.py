"""BambooHR: the hosted careers list, one unauthenticated GET.

  GET https://{slug}.bamboohr.com/careers/list
  -> {"meta": {"totalCount": N}, "result": [...]}

Verified 2026-08: unknown subdomain -> 404, no pagination. The payload is
sparser than most -- no timestamps at all -- so posted_at is left null rather
than invented, the same call the Workday adapter makes about "Posted Today".
"""

from __future__ import annotations

from ..core import http
from ..core.models import FetchError, Posting
from .base import Adapter

API = "https://{slug}.bamboohr.com/careers/list"
BOARD = "https://{slug}.bamboohr.com/careers/{jid}"


def _location(j: dict) -> str | None:
    """Merge the two location objects BambooHR returns.

    `location` holds only city and state, and on 749 of 2,235 open postings
    both are null. `atsLocation` holds city, state, province *and country* --
    which `location` has no field for at all -- and is populated when the
    other is not: budibase/49 reports {"city": null, "state": null} beside
    {"country": "United Kingdom"}.

    Reading both took unknown from 126 to 33 across 25 boards, and correctly
    refused 52 more as Canadian.
    """
    loc = j.get("location") or {}
    ats = j.get("atsLocation") or {}
    parts = [
        loc.get("city") or ats.get("city"),
        loc.get("state") or ats.get("state") or ats.get("province"),
        loc.get("country") or ats.get("country"),
    ]
    return ", ".join(p for p in parts if p) or None


class BambooHRAdapter(Adapter):
    ats = "bamboohr"

    def board_url(self, slug: str) -> str:
        return API.format(slug=slug)

    def count(self, slug: str) -> int:
        """meta.totalCount is authoritative and avoids building Postings."""
        data = http.get_json(self.board_url(slug))
        return int((data or {}).get("meta", {}).get("totalCount") or 0)

    def fetch(self, slug: str) -> list[Posting]:
        data = http.get_json(self.board_url(slug))
        result = (data or {}).get("result")
        if result is None:
            raise FetchError("response had no 'result' key")

        out: list[Posting] = []
        for j in result:
            jid = j.get("id")
            if not jid:
                continue
            location = _location(j)
            out.append(
                Posting(
                    ats=self.ats,
                    slug=slug,
                    external_id=str(jid),
                    title=(j.get("jobOpeningName") or "").strip(),
                    url=BOARD.format(slug=slug, jid=jid),
                    location=location,
                    locations=[location] if location else [],
                    department=j.get("departmentLabel"),
                    employment_type=j.get("employmentStatusLabel"),
                    # Always null in practice -- 441 postings sampled across
                    # 30 boards, not one set. locationType (0, 1, 2) looks
                    # like the field that carries it, but the values are
                    # undocumented and the careers page is a JS bundle that
                    # mentions both "remote" and "onsite" whatever the value,
                    # so there is nothing to check a guess against.
                    is_remote=j.get("isRemote"),
                    posted_at=None,
                )
            )
        return out
