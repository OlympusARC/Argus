"""Wayback Machine CDX index.

A genuinely different corpus from Common Crawl -- the Archive captures pages CC
never sampled, and captures them more often. The catch is aggressive rate
limiting (a single large query returns 429), so this source paginates and paces
itself deliberately rather than asking for everything at once.

It swept jobs.ashbyhq.com alone until now, the same defect Common Crawl
carried: nine of the ten ATSs we can poll were invisible to it, and nothing in
the output said so. Widening Common Crawl turned 2,709 boards into 19,229.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

from ..core import http, urls
from ..core.models import BoardRef, FetchError
from .base import Source

CDX = "https://web.archive.org/cdx/search/cdx"
"""
(host, matchType) pairs. Unlike Common Crawl -- where a bare domain match
handles both shapes -- the Wayback CDX wants prefix for a host that carries
the slug in its path and domain for an ATS that puts the company in a
subdomain. Verified against the live index rather than assumed: both forms
return rows, and copying Common Crawl's single-form fix here would have been
wrong.
"""
DEFAULT_HOSTS = (
    ("jobs.ashbyhq.com", "prefix"),
    ("boards.greenhouse.io", "prefix"),
    ("job-boards.greenhouse.io", "prefix"),
    ("jobs.lever.co", "prefix"),
    ("apply.workable.com", "prefix"),
    ("jobs.smartrecruiters.com", "prefix"),
    ("myworkdayjobs.com", "domain"),
    ("breezy.hr", "domain"),
    ("recruitee.com", "domain"),
    ("bamboohr.com", "domain"),
)


class WaybackSource(Source):
    name = "wayback"

    def __init__(self, hosts=None, pause: float = 3.0, max_pages: int = 40, retries: int = 4):
        self.hosts = tuple(hosts or DEFAULT_HOSTS)
        self.pause = pause
        self.max_pages = max_pages
        self.retries = retries

    def _params(self, host: str, match: str = "prefix", **extra) -> dict:
        return {
            "url": host,
            "matchType": match,
            "fl": "original",
            "collapse": "urlkey",
            "output": "text",
            **extra,
        }

    def _pages(self, host: str, match: str) -> int:
        try:
            for line in http.get_lines(
                CDX, params=self._params(host, match, showNumPages="true")
            ):
                return int(line.strip())
        except (FetchError, ValueError):
            return 1
        return 1

    def discover(self) -> Iterator[BoardRef]:
        seen: set[tuple[str, str]] = set()
        for host, match in self.hosts:
            pages = min(self._pages(host, match), self.max_pages)
            for page in range(pages):
                lines = None
                for attempt in range(self.retries):
                    try:
                        """
                        Materialize inside the retry loop: a 429 surfaces on
                        the first read, not at request time.
                        """
                        lines = list(
                            http.get_lines(
                                CDX, params=self._params(host, match, page=str(page))
                            )
                        )
                        break
                    except FetchError:
                        time.sleep(self.pause * (2**attempt))
                if lines is None:
                    continue
                for line in lines:
                    ref = urls.parse(line.strip())
                    if ref and (ref.ats, ref.slug) not in seen:
                        seen.add((ref.ats, ref.slug))
                        yield BoardRef(
                            ref.ats, ref.slug, None, self.name, {"host": host, "page": page}
                        )
                time.sleep(self.pause)
