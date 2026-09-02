"""VC portfolio job boards.

A venture firm's jobs site is a curated index of exactly the companies that are
hiring and well funded, with links straight to their ATS. One page yields
dozens of boards, and the firms keep them current for us.

These sites run on a handful of platforms (Consider, Getro, custom). Rather
than reverse-engineering each platform's API -- which changes and needs
per-network ids -- we fetch the rendered HTML and run the shared URL router
over it. Slower per page, but it works uniformly and breaks rarely.

Verified 2026-08: jobs.a16z.com is Consider-powered and exposes greenhouse and
lever links directly in its markup.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

from ..core import http, urls
from ..core.models import BoardRef, FetchError
from .base import Source

"""
Firm -> job board. Boards that turn out to be JS-only simply yield nothing
and cost one request.
Measured 2026-08. Most VC job boards are client-rendered: they answer 200
with a ~20KB shell and no links until JavaScript runs, so they yield nothing
to an HTML fetch. The three marked SSR below are the ones that actually serve
markup; the rest are kept because they cost one request each and may start
server-rendering, but do not expect much from them.
"""
BOARDS = {
    "a16z": "https://jobs.a16z.com",  # SSR, ~8 refs
    "greylock": "https://jobs.greylock.com",  # SSR, ~18 refs
    "generalcatalyst": "https://jobs.generalcatalyst.com",  # SSR, few refs
    "accel": "https://jobs.accel.com",
    "insight": "https://jobs.insightpartners.com",
    "sequoia": "https://jobs.sequoiacap.com",
    "bessemer": "https://jobs.bvp.com",
    "kleiner": "https://jobs.kleinerperkins.com",
    "lightspeed": "https://jobs.lsvp.com",
    "firstround": "https://jobs.firstround.com",
    "battery": "https://jobs.battery.com",
}

"""
Pagination shapes seen across Consider/Getro boards.
"""
PAGE_PARAMS = ("?page={n}", "?page={n}&limit=100")


class VCPortfolioSource(Source):
    name = "vcportfolio"

    def __init__(
        self, boards: dict[str, str] | None = None, pages: int = 5, pause: float = 1.0
    ):
        self.boards = dict(boards or BOARDS)
        self.pages = pages
        self.pause = pause

    def discover(self) -> Iterator[BoardRef]:
        seen: set[tuple[str, str]] = set()
        for firm, base in self.boards.items():
            found_any = False
            for page in range(1, self.pages + 1):
                candidates = (
                    [base] if page == 1 else [base + p.format(n=page) for p in PAGE_PARAMS[:1]]
                )
                new_this_page = 0
                for url in candidates:
                    try:
                        html = http.get_text(
                            url,
                            probe=True,
                            max_bytes=3_000_000,
                            timeout=20,
                            allow_redirects=True,
                        )
                    except (FetchError, OSError, ValueError):
                        continue
                    for ref in urls.extract_all(html):
                        if (ref.ats, ref.slug) in seen:
                            continue
                        seen.add((ref.ats, ref.slug))
                        new_this_page += 1
                        found_any = True
                        yield BoardRef(
                            ref.ats, ref.slug, None, self.name, {"firm": firm, "page": page}
                        )
                """
                A page that adds nothing new means pagination is not working
                on this board -- stop rather than fetching four more copies.
                """
                if new_this_page == 0 and page > 1:
                    break
                time.sleep(self.pause)
            if not found_any:
                continue
