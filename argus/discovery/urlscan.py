"""urlscan.io public search.

A genuinely independent corpus: urlscan indexes pages that people and bots
submitted for scanning, which overlaps Common Crawl only partly and refreshes
continuously. No API key needed for search.

Ported from binaryshrey/BOARD, with two changes: it sweeps every ATS host we
recognize rather than Ashby alone, and slugs go through the shared URL router
instead of a bare path split -- the original returned app routes like
`careers`, `download` and `events` as if they were companies.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

from ..core import http, urls
from ..core.models import BoardRef, FetchError
from .base import Source

API = "https://urlscan.io/api/v1/search/"

HOSTS = (
    "jobs.ashbyhq.com",
    "job-boards.greenhouse.io",
    "boards.greenhouse.io",
    "jobs.lever.co",
    "apply.workable.com",
    "jobs.smartrecruiters.com",
)


class UrlscanSource(Source):
    name = "urlscan"

    def __init__(
        self, hosts=None, max_pages: int = 10, page_size: int = 100, pause: float = 0.5
    ):
        self.hosts = tuple(hosts or HOSTS)
        self.max_pages = max_pages
        self.page_size = page_size
        self.pause = pause

    def discover(self) -> Iterator[BoardRef]:
        seen: set[tuple[str, str]] = set()
        for host in self.hosts:
            after: str | None = None
            for _ in range(self.max_pages):
                params = {"q": f"domain:{host}", "size": str(self.page_size)}
                if after:
                    params["search_after"] = after
                try:
                    data = http.get_json(API, params=params)
                except FetchError:
                    break
                results = data.get("results") or []
                if not results:
                    break
                for item in results:
                    ref = urls.parse((item.get("page") or {}).get("url", ""))
                    if ref and (ref.ats, ref.slug) not in seen:
                        seen.add((ref.ats, ref.slug))
                        yield BoardRef(ref.ats, ref.slug, None, self.name, {"host": host})
                if len(results) < self.page_size:
                    break
                sort_vals = results[-1].get("sort")
                if not sort_vals:
                    break
                after = ",".join(str(v) for v in sort_vals)
                time.sleep(self.pause)
