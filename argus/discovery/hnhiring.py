"""HN "Ask HN: Who is hiring?" threads, read in full.

The generic HN search source is relevance-ranked and caps at 1000 hits, so it
only ever sees the loudest mentions. These monthly threads are the single
densest concentration of fresh board links on the public web, and Algolia will
hand over an entire thread's comments by story tag -- so we read them whole
rather than searching them.
"""

from __future__ import annotations

from collections.abc import Iterator

from ..core import http, urls
from ..core.models import BoardRef, FetchError
from .base import Source

SEARCH = "https://hn.algolia.com/api/v1/search"


class HNHiringSource(Source):
    name = "hn_hiring"

    def __init__(self, max_threads: int = 60, per_thread: int = 1000):
        self.max_threads = max_threads
        self.per_thread = per_thread

    def _threads(self) -> list[str]:
        """Story ids for the monthly hiring posts (all authored by 'whoishiring')."""
        ids: list[str] = []
        for page in range(3):
            try:
                data = http.get_json(
                    SEARCH,
                    params={
                        "tags": "story,author_whoishiring",
                        "hitsPerPage": "100",
                        "page": str(page),
                    },
                )
            except FetchError:
                break
            hits = data.get("hits") or []
            if not hits:
                break
            for h in hits:
                title = (h.get("title") or "").lower()
                if "hiring" in title and "who" in title and h.get("objectID"):
                    ids.append(h["objectID"])
            if len(ids) >= self.max_threads:
                break
        return ids[: self.max_threads]

    def discover(self) -> Iterator[BoardRef]:
        seen: set[tuple[str, str]] = set()
        for sid in self._threads():
            page = 0
            while True:
                try:
                    data = http.get_json(
                        SEARCH,
                        params={
                            "tags": f"comment,story_{sid}",
                            "hitsPerPage": str(self.per_thread),
                            "page": str(page),
                        },
                    )
                except FetchError:
                    break
                hits = data.get("hits") or []
                for hit in hits:
                    for ref in urls.extract_all(hit.get("comment_text") or ""):
                        if (ref.ats, ref.slug) in seen:
                            continue
                        seen.add((ref.ats, ref.slug))
                        yield BoardRef(ref.ats, ref.slug, None, self.name, {"story": sid})
                page += 1
                if not hits or page >= int(data.get("nbPages") or 0):
                    break
