"""LinkedIn guest jobs API.

LinkedIn serves job cards without auth at
  /jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=&location=&start=N

Ported from binaryshrey/BOARD, but with expectations set honestly: LinkedIn is
a *job* surface, not a board directory. Cards link to LinkedIn's own job pages,
and only the subset that use external apply expose an ATS URL -- so yield per
request is far lower than any crawl-index source. It also rate-limits hard
(BOARD needed a rotating proxy pool), which is why the defaults here are small
and this source is off by default.

Kept because it reaches postings that never appear in a crawl index, not
because it is efficient.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator

from ..core import http, urls
from ..core.models import BoardRef, FetchError
from .base import Source

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_JOB_ID = re.compile(r"/jobs/view/[^/\"']*?(\d{8,})")

KEYWORDS = ("software engineer intern", "new grad software engineer")


class LinkedInSource(Source):
    name = "linkedin"

    def __init__(
        self,
        keywords=None,
        location: str = "United States",
        pages: int = 3,
        per_page: int = 25,
        pause: float = 2.0,
        max_jobs: int = 150,
    ):
        self.keywords = tuple(keywords or KEYWORDS)
        self.location = location
        self.pages = pages
        self.per_page = per_page
        self.pause = pause
        self.max_jobs = max_jobs

    def _job_ids(self) -> list[str]:
        ids: list[str] = []
        for kw in self.keywords:
            for page in range(self.pages):
                try:
                    html = http.get_text(
                        SEARCH,
                        headers={"User-Agent": BROWSER_UA},
                        params={
                            "keywords": kw,
                            "location": self.location,
                            "start": str(page * self.per_page),
                        },
                    )
                except FetchError:
                    break
                found = _JOB_ID.findall(html)
                if not found:
                    break
                ids.extend(found)
                time.sleep(self.pause)
                if len(ids) >= self.max_jobs:
                    return ids[: self.max_jobs]
        return ids[: self.max_jobs]

    def discover(self) -> Iterator[BoardRef]:
        seen: set[tuple[str, str]] = set()
        for jid in dict.fromkeys(self._job_ids()):
            try:
                html = http.get_text(
                    f"https://www.linkedin.com/jobs/view/{jid}",
                    headers={"User-Agent": BROWSER_UA},
                    timeout=12,
                )
            except (FetchError, Exception):
                continue
            for ref in urls.extract_all(html):
                if (ref.ats, ref.slug) not in seen:
                    seen.add((ref.ats, ref.slug))
                    yield BoardRef(ref.ats, ref.slug, None, self.name, {"linkedin_job": jid})
            time.sleep(self.pause)
