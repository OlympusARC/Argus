"""SimplifyJobs curated repos.

Highest-precision source we have: the postings are already filtered to
early-career roles, already normalized, and `url` is the raw ATS URL -- so one
parse yields both a registry row and a seed posting.

Paths verified 2026-08: both repos keep listings.json on branch `dev` at
.github/scripts/listings.json (NOT master, and the internships repo has been
renamed to Summer2027-Internships).
"""

from __future__ import annotations

from collections.abc import Iterator

from ..core import config, http, urls
from ..core.models import BoardRef, Posting
from .base import Source

RAW = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"


class SimplifySource(Source):
    name = "simplify"

    def __init__(self, repos=None, include_inactive: bool = False):
        self.repos = repos or config.SIMPLIFY_REPOS
        self.include_inactive = include_inactive

    def discover(self) -> Iterator[BoardRef]:
        for repo, branch, path, category in self.repos:
            url = RAW.format(repo=repo, branch=branch, path=path)
            rows = http.get_json(url)
            if not isinstance(rows, list):
                continue
            for row in rows:
                ref = urls.parse(row.get("url") or "")
                if not ref:
                    continue
                active = bool(row.get("active")) and bool(row.get("is_visible", True))
                posting = None
                if ref.external_id and (active or self.include_inactive):
                    posting = Posting(
                        ats=ref.ats,
                        slug=ref.slug,
                        external_id=ref.external_id,
                        title=row.get("title") or "",
                        url=row.get("url"),
                        locations=list(row.get("locations") or []),
                        location=(row.get("locations") or [None])[0],
                        posted_at=row.get("date_posted"),
                        raw={
                            "simplify_id": row.get("id"),
                            "category": row.get("category"),
                            "sponsorship": row.get("sponsorship"),
                            "degrees": row.get("degrees"),
                            "segment": category,
                        },
                    )
                yield BoardRef(
                    ats=ref.ats,
                    slug=ref.slug,
                    company_name=row.get("company_name"),
                    source=self.name,
                    detail={"repo": repo, "segment": category},
                    posting=posting,
                )
