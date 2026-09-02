"""Community job-list repos that only publish a README.

Several well-maintained lists never produce a listings.json -- the README *is*
the artifact. That is fine for us: the rendered table's apply links carry the
raw ATS URL, so the shared router reads them directly with no HTML parsing and
no per-repo scraper.

Kept separate from the `simplify` source because these yield boards only, not
seed postings, and measuring them apart is the only way to know whether they
earn their runtime.
"""

from __future__ import annotations

from collections.abc import Iterator

from ..core import config, http, urls
from ..core.models import BoardRef, FetchError
from .base import Source

RAW = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"


class JobReposSource(Source):
    name = "jobrepos"

    def __init__(self, repos=None):
        self.repos = repos or config.COMMUNITY_REPOS

    def discover(self) -> Iterator[BoardRef]:
        seen: set[tuple[str, str]] = set()
        for repo, branch, path in self.repos:
            try:
                text = http.get_text(
                    RAW.format(repo=repo, branch=branch, path=path), probe=True, timeout=20
                )
            except (FetchError, OSError, ValueError):
                continue
            for ref in urls.extract_all(text):
                if (ref.ats, ref.slug) in seen:
                    continue
                seen.add((ref.ats, ref.slug))
                yield BoardRef(ref.ats, ref.slug, None, self.name, {"repo": repo})
