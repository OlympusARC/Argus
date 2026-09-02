"""Hacker News via the free Algolia API.

Verified 2026-08: 1,330 comments mention jobs.ashbyhq.com. "Who is hiring?"
threads are a monthly refill of *fresh* boards, which is exactly the tail that
Common Crawl misses.

Algolia caps any single query at 1000 hits, so we window backwards by
created_at instead of paginating -- otherwise we would only ever see the newest
thousand comments.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

from ..core import http, urls
from ..core.models import BoardRef, FetchError
from .base import Source

API = "https://hn.algolia.com/api/v1/search_by_date"
QUERIES = ("jobs.ashbyhq.com", "ashbyhq.com")


class HackerNewsSource(Source):
    name = "hn"

    def __init__(self, queries=None, max_windows: int = 25, hits_per_page: int = 500):
        self.queries = tuple(queries or QUERIES)
        self.max_windows = max_windows
        self.hits_per_page = hits_per_page

    def discover(self) -> Iterator[BoardRef]:
        seen: set[tuple[str, str]] = set()
        for query in self.queries:
            before = int(time.time())
            for _ in range(self.max_windows):
                try:
                    data = http.get_json(
                        API,
                        params={
                            "query": query,
                            "tags": "(comment,story)",
                            "hitsPerPage": str(self.hits_per_page),
                            "numericFilters": f"created_at_i<{before}",
                        },
                    )
                except FetchError:
                    break
                hits = data.get("hits") or []
                if not hits:
                    break
                oldest = before
                for hit in hits:
                    ts = hit.get("created_at_i")
                    if ts:
                        oldest = min(oldest, ts)
                    blob = " ".join(
                        filter(
                            None,
                            (hit.get("comment_text"), hit.get("story_text"), hit.get("url")),
                        )
                    )
                    for ref in urls.extract_all(blob):
                        if (ref.ats, ref.slug) in seen:
                            continue
                        seen.add((ref.ats, ref.slug))
                        yield BoardRef(
                            ref.ats,
                            ref.slug,
                            None,
                            self.name,
                            {"hn_object_id": hit.get("objectID")},
                        )
                if oldest >= before:  # no forward progress; stop windowing
                    break
                before = oldest
