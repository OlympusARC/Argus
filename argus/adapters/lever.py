"""Lever: one unauthenticated GET returns the whole board.

  GET https://api.lever.co/v0/postings/{slug}?mode=json
  -> [ ... ]                      (a bare list, not an object)

Verified 2026-08: an unknown slug returns 404 with {"ok": false}, while a real
but empty board returns 200 with []. That distinction is what lets validate
tell a dead slug from a company that simply has no openings.
"""

from __future__ import annotations

from typing import Any

from ..core import http
from ..core.models import FetchError, Posting
from .base import Adapter

API = "https://api.lever.co/v0/postings/{slug}"


class LeverAdapter(Adapter):
    ats = "lever"

    def board_url(self, slug: str) -> str:
        return API.format(slug=slug)

    def fetch(self, slug: str) -> list[Posting]:
        data = http.get_json(self.board_url(slug), params={"mode": "json"})
        if not isinstance(data, list):
            raise FetchError("expected a list of postings")

        out: list[Posting] = []
        for j in data:
            jid = j.get("id")
            if not jid:
                continue
            cat: dict[str, Any] = j.get("categories") or {}
            locations = list(cat.get("allLocations") or [])
            location = cat.get("location") or (locations[0] if locations else None)
            if location and location not in locations:
                locations.insert(0, location)
            created = j.get("createdAt")
            """
            Lever names its pay fields differently from every other ATS, so
            they were never mapped and the salary on ~11k postings survived
            only inside the unread raw payload.
            """
            comp = j.get("salaryRange") or None
            if comp and j.get("salaryDescription"):
                comp = dict(comp, description=j["salaryDescription"])
            elif not comp and j.get("salaryDescription"):
                comp = {"description": j["salaryDescription"]}
            out.append(
                Posting(
                    ats=self.ats,
                    slug=slug,
                    external_id=str(jid),
                    title=(j.get("text") or "").strip(),
                    url=j.get("hostedUrl") or "",
                    apply_url=j.get("applyUrl"),
                    location=location,
                    locations=locations,
                    team=cat.get("team"),
                    department=cat.get("department"),
                    employment_type=cat.get("commitment"),
                    workplace_type=j.get("workplaceType"),
                    # Lever reports createdAt in milliseconds.
                    posted_at=int(created / 1000)
                    if isinstance(created, (int, float))
                    else None,
                    compensation=comp,
                    # `opening` and `openingPlain` are the job description under
                    # names the description filter never covered, so 35 MB of
                    # body text was stored against the intent of this list.
                    raw={
                        k: v
                        for k, v in j.items()
                        if k
                        not in (
                            "description",
                            "descriptionPlain",
                            "descriptionBody",
                            "descriptionBodyPlain",
                            "additional",
                            "additionalPlain",
                            "lists",
                            "opening",
                            "openingPlain",
                        )
                    },
                )
            )
        return out
