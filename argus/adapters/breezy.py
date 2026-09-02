"""Breezy HR: one unauthenticated GET returns the whole board as a bare list.

  GET https://{slug}.breezy.hr/json
  -> [ ... ]

Verified 2026-08: a live board returns 200 with a JSON list, a dead one a
clean 404. That distinction is what lets validate tell a retired subdomain
from a company that simply has no openings.
"""

from __future__ import annotations

from datetime import datetime

from ..core import http
from ..core.models import FetchError, Posting
from .base import Adapter

API = "https://{slug}.breezy.hr/json"


def _epoch(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


class BreezyAdapter(Adapter):
    ats = "breezy"

    def board_url(self, slug: str) -> str:
        return API.format(slug=slug)

    def fetch(self, slug: str) -> list[Posting]:
        data = http.get_json(self.board_url(slug))
        if not isinstance(data, list):
            raise FetchError("expected a list of postings")

        out: list[Posting] = []
        for j in data:
            jid = j.get("id")
            if not jid:
                continue
            loc = j.get("location") or {}
            names = []
            for entry in j.get("locations") or [loc]:
                name = (entry or {}).get("name")
                if name and name not in names:
                    names.append(name)
            out.append(
                Posting(
                    ats=self.ats,
                    slug=slug,
                    external_id=str(jid),
                    title=(j.get("name") or "").strip(),
                    url=j.get("url") or "",
                    location=names[0] if names else None,
                    locations=names,
                    department=j.get("department"),
                    employment_type=(j.get("type") or {}).get("name"),
                    is_remote=loc.get("is_remote"),
                    posted_at=_epoch(j.get("published_date")),
                )
            )
        return out
