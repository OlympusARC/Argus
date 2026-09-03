"""Normalized shapes shared by every adapter."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

"""
Fields that define "the posting changed". Deliberately excludes volatile
server-side timestamps (Greenhouse bumps updated_at on no-op republishes),
which would otherwise make every job look edited on every poll.
"""
_HASHED = (
    "title",
    "location",
    "locations",
    "department",
    "team",
    "employment_type",
    "workplace_type",
    "is_remote",
    "url",
    "compensation",
)


@dataclass(slots=True)
class Posting:
    ats: str
    slug: str
    external_id: str
    title: str
    url: str
    apply_url: str | None = None
    location: str | None = None
    locations: list[str] = field(default_factory=list)
    department: str | None = None
    team: str | None = None
    employment_type: str | None = None
    workplace_type: str | None = None
    is_remote: bool | None = None
    posted_at: int | None = None  # epoch seconds, UTC

    """
    The newest date this posting could have, when the source gives a bound
    rather than a date. Workday says "Posted 30+ Days Ago", which is not a
    date and must never be stored as one -- but it does say the posting is at
    most thirty days old, which is enough to reject it against a cutoff.

    Never written to the database, and never in _HASHED. It exists so the
    ingest filter can answer "is this definitely too old" without inventing a
    precision the source does not have.
    """
    posted_bound: int | None = None
    compensation: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Vendor-scoped stable id. Two sources naming the same posting collapse."""
        return f"{self.ats}:{self.slug}:{self.external_id}"

    def content_hash(self) -> str:
        payload = {k: getattr(self, k) for k in _HASHED}
        blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class BoardRef:
    """A board we know about, before it has been validated or polled."""

    ats: str
    slug: str
    company_name: str | None = None
    source: str | None = None
    detail: dict[str, Any] | None = None  # provenance payload, e.g. sample url
    posting: Posting | None = None  # some sources carry a seed posting
    website: str | None = None  # the company's own site
    careers_url: str | None = None  # page the ATS link was found on


@dataclass(slots=True)
class CompanyRef:
    """A company we know about, with or without a board.

    The companies with no board are the point. A source that names an employer
    and its domain but no ATS link is not a failed board discovery -- it is a
    probe target, and probing is how the board gets found. Yielding these
    separately keeps them out of the board registry, which must only ever hold
    things that actually look like boards.
    """

    name: str | None = None
    domain: str | None = None
    website: str | None = None
    careers_url: str | None = None
    source: str | None = None
    detail: dict[str, Any] | None = None
    board: tuple[str, str] | None = None  # (ats, slug) when one is known


class FetchError(RuntimeError):
    """Transient or permanent failure fetching a board."""

    def __init__(self, message: str, *, permanent: bool = False, status: int | None = None):
        super().__init__(message)
        self.permanent = permanent
        self.status = status
