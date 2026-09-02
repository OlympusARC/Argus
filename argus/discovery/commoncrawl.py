"""Common Crawl index -- first-party, re-runnable board enumeration.

Better than any static slug dump: we query the CDX index ourselves, so we can
union across many monthly crawls and re-run against the newest one. On the
measured numbers it is also the most productive source we have -- 2,710 boards
no other source found, more than double the next best.

Two properties of the index govern this whole module.

It rate-limits hard, and not politely. Fourteen queries in quick succession
earns a refused connection rather than a 429, and the block lasts minutes. So
every request is paced and retried with backoff, the way the Wayback source
already does it.

And it must be queried by domain, not by prefix. The ATSs come in two shapes --
Greenhouse and Lever put the board slug in the path, Workday and Breezy put the
company in a subdomain -- and it is tempting to reach for a prefix match for
the first kind. That is a trap: `url=host/*` combined with `matchType=prefix`
returns zero pages, because the wildcard and the flag double up, and zero pages
is indistinguishable from a host with nothing on it.

A domain match on the bare host handles both shapes and is the only form
verified to work for either. Measured against the live index: `jobs.ashbyhq.com`
returns 2 pages as a domain match and 0 as a prefix match.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from ..core import http, urls
from ..core.models import BoardRef, FetchError
from .base import Source

COLLINFO = "https://index.commoncrawl.org/collinfo.json"
INDEX = "https://index.commoncrawl.org/{crawl}-index"

"""
Every ATS we can poll, swept as a domain. This used to be Ashby alone: the
source that finds more unique boards than any other was reading a tenth of the
surface it can see.
"""
DEFAULT_HOSTS = (
    "jobs.ashbyhq.com",
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "apply.workable.com",
    "jobs.smartrecruiters.com",
    "myworkdayjobs.com",
    "breezy.hr",
    "recruitee.com",
    "bamboohr.com",
)
ALL_HOSTS = DEFAULT_HOSTS


class CommonCrawlSource(Source):
    name = "commoncrawl"

    def __init__(self, hosts=None, crawls: int = 4, pause: float = 1.5, retries: int = 4):
        self.hosts = tuple(hosts or DEFAULT_HOSTS)
        self.crawls = crawls
        self.pause = pause
        self.retries = retries
        """
        Counted, not swallowed. A run blocked throughout used to be
        indistinguishable from a run that found nothing new -- which is exactly
        the failure that hides. The caller can see this.
        """
        self.blocked = 0
        self.queries = 0

    def _get(self, url: str, params: dict, lines: bool = False):
        """One paced, retried request. None once the retries are spent."""
        for attempt in range(self.retries):
            try:
                self.queries += 1
                if lines:
                    return list(http.get_lines(url, params=params))
                return http.get_json(url, params=params)
            except (FetchError, OSError):
                time.sleep(self.pause * (2**attempt))
        self.blocked += 1
        return None

    def _recent_crawls(self) -> list[str]:
        data = self._get(COLLINFO, {})
        return [c["id"] for c in data[: self.crawls]] if data else []

    def _query(self, host: str, **extra) -> dict:
        return {"url": host, "matchType": "domain", "output": "json", **extra}

    def _pages(self, crawl: str, host: str) -> int:
        info = self._get(INDEX.format(crawl=crawl), self._query(host, showNumPages="true"))
        return int(info.get("pages", 0)) if isinstance(info, dict) else 0

    def discover(self) -> Iterator[BoardRef]:
        seen: set[tuple[str, str]] = set()
        for crawl in self._recent_crawls():
            for host in self.hosts:
                pages = self._pages(crawl, host)
                time.sleep(self.pause)
                for page in range(pages):
                    lines = self._get(
                        INDEX.format(crawl=crawl),
                        self._query(host, page=str(page)),
                        lines=True,
                    )
                    time.sleep(self.pause)
                    if lines is None:
                        """
                        Give up on this host for this crawl rather than
                        grinding through the remaining pages while blocked.
                        """
                        break
                    for line in lines:
                        if not line.startswith("{"):
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        ref = urls.parse(rec.get("url", ""))
                        if ref and (ref.ats, ref.slug) not in seen:
                            seen.add((ref.ats, ref.slug))
                            yield BoardRef(
                                ref.ats,
                                ref.slug,
                                None,
                                self.name,
                                {"crawl": crawl, "host": host},
                            )
