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
            loc = j.get("location") or {}
            parts = [loc.get("city"), loc.get("state"), loc.get("country")]
            location = ", ".join(p for p in parts if p) or None
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
                    is_remote=j.get("isRemote"),
                    posted_at=None,
                )
            )
        return out
