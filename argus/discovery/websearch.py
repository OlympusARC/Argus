"""Web search -> fetch the linking pages -> mine them for ATS links.

Generalizes BOARD's google_linkto. The insight there was right and worth
keeping: searching for pages *on* jobs.ashbyhq.com finds nothing, because the
board is a JS-rendered SPA. Searching for pages that *mention* those URLs finds
plenty, because career pages, blogs and aggregators are ordinary HTML.

The original bound this to scraping Google via googlesearch-python, which is
fragile and ToS-grey. Here the search backend is pluggable and picks the first
one actually available:

  1. Brave Search API  (BRAVE_API_KEY -- free tier, documented, stable)
  2. DuckDuckGo HTML   (no key) -- verified dead 2026-08: answers 202 with an
     anti-bot page, so the source reports itself unavailable without a key
     rather than silently returning nothing.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator

from ..core import http, urls
from ..core.models import BoardRef, FetchError
from .base import Source

BRAVE = "https://api.search.brave.com/res/v1/web/search"
DDG = "https://html.duckduckgo.com/html/"

QUERIES = (
    '"jobs.ashbyhq.com"',
    '"jobs.ashbyhq.com" careers hiring',
    '"jobs.ashbyhq.com" apply engineering',
    '"job-boards.greenhouse.io" careers',
    '"jobs.lever.co" careers hiring',
)

_DDG_HREF = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"')


class WebSearchSource(Source):
    name = "websearch"

    def __init__(self, queries=None, per_query: int = 20, pause: float = 2.0):
        self.queries = tuple(queries or QUERIES)
        self.per_query = per_query
        self.pause = pause
        self.backend = None

    def available(self) -> tuple[bool, str]:
        if os.getenv("BRAVE_API_KEY"):
            self.backend = "brave"
            return True, ""
        """
        DuckDuckGo's keyless HTML endpoint now answers 202 with an anti-bot
        page for every request, so it yields nothing. Reporting unavailable
        beats silently returning zero refs and looking like a dry vector.
        """
        self.backend = "ddg"
        return False, "set BRAVE_API_KEY (free tier); DuckDuckGo keyless is anti-bot blocked"

    def _search(self, query: str) -> list[str]:
        if self.backend == "brave":
            try:
                data = http.get_json(
                    BRAVE,
                    params={"q": query, "count": self.per_query},
                    headers={
                        "X-Subscription-Token": os.environ["BRAVE_API_KEY"],
                        "Accept": "application/json",
                    },
                )
                return [
                    r["url"] for r in (data.get("web") or {}).get("results", []) if r.get("url")
                ]
            except (FetchError, KeyError):
                return []
        try:
            html = http.get_text(DDG, params={"q": query})
        except FetchError:
            return []
        return _DDG_HREF.findall(html)[: self.per_query]

    def discover(self) -> Iterator[BoardRef]:
        if self.backend is None:
            self.available()
        seen: set[tuple[str, str]] = set()
        visited: set[str] = set()
        for query in self.queries:
            for url in self._search(query):
                if url in visited:
                    continue
                visited.add(url)
                """
                the result URL itself sometimes contains the board link
                """
                for ref in urls.extract_all(url):
                    if (ref.ats, ref.slug) not in seen:
                        seen.add((ref.ats, ref.slug))
                        yield BoardRef(
                            ref.ats, ref.slug, None, self.name, {"via": "result_url"}
                        )
                try:
                    html = http.get_text(url, timeout=10)
                except (FetchError, Exception):
                    continue
                for ref in urls.extract_all(html):
                    if (ref.ats, ref.slug) not in seen:
                        seen.add((ref.ats, ref.slug))
                        yield BoardRef(ref.ats, ref.slug, None, self.name, {"via": url[:120]})
            time.sleep(self.pause)
