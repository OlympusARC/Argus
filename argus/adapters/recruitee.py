"""Recruitee: one unauthenticated GET returns the whole board.

  GET https://{slug}.recruitee.com/api/offers/
  -> {"offers": [...]}

Verified 2026-08: unknown company -> 404, no pagination. Timestamps arrive as
"2026-08-04 14:20:47 UTC" rather than ISO-8601, so they need parsing rather
than fromisoformat.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..core import http
from ..core.models import FetchError, Posting
from .base import Adapter

API = "https://{slug}.recruitee.com/api/offers/"


def _epoch(value: str | None) -> int | None:
    if not value:
        return None
    text = value.strip().removesuffix(" UTC")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=UTC).timestamp())
        except ValueError:
            continue
    return None


class RecruiteeAdapter(Adapter):
    ats = "recruitee"

    def board_url(self, slug: str) -> str:
        return API.format(slug=slug)

    def fetch(self, slug: str) -> list[Posting]:
        data = http.get_json(self.board_url(slug))
        offers = (data or {}).get("offers")
        if offers is None:
            raise FetchError("response had no 'offers' key")

        out: list[Posting] = []
        for j in offers:
            jid = j.get("id")
            if not jid:
                continue
            names = []
            for entry in j.get("locations") or []:
                name = (entry or {}).get("full_address") or (entry or {}).get("city")
                if name and name not in names:
                    names.append(name)
            if not names and j.get("location"):
                names.append(j["location"])
            out.append(
                Posting(
                    ats=self.ats,
                    slug=slug,
                    external_id=str(jid),
                    title=(j.get("title") or "").strip(),
                    url=j.get("careers_url") or "",
                    apply_url=j.get("careers_apply_url"),
                    location=names[0] if names else None,
                    locations=names,
                    department=j.get("department"),
                    employment_type=j.get("employment_type_code"),
                    is_remote=j.get("remote"),
                    posted_at=_epoch(j.get("published_at") or j.get("created_at")),
                )
            )
        return out
