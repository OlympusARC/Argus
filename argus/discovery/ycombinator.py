"""Y Combinator: the company corpus we were missing.

api.ycombinator.com/v0.1/companies is public, paginated, and returns name,
website, slug and batch for the whole YC portfolio. Two things make it valuable
beyond raw slug count:

  * YC companies skew heavily to Ashby/Greenhouse/Lever, and many are too new
    or too small to be well crawled -- exactly the tail where Common Crawl
    saturated.
  * It carries real company NAMES and domains. Every source except SimplifyJobs
    yields a bare slug, so this materially improves the registry's metadata.

Boards are found by probing each company's careers page (see careers.py).
workatastartup.com is deliberately not used: it 406s any non-browser client and
would drag a headless browser into the dependency tree.
"""

from __future__ import annotations

from collections.abc import Iterator

from ..core import http, urls
from ..core.models import BoardRef, FetchError
from ..core.names import plausibly_same
from .base import Source
from .probe import MAX_ATTRIBUTABLE_REFS, domain_of, probe_many

API = "https://api.ycombinator.com/v0.1/companies"
HN_JOBS = "https://news.ycombinator.com/jobs"


class YCombinatorSource(Source):
    name = "ycombinator"

    def __init__(
        self,
        max_pages: int = 250,
        workers: int = 12,
        probe_careers: bool = True,
        limit_companies: int | None = None,
    ):
        self.max_pages = max_pages
        self.workers = workers
        self.probe_careers = probe_careers
        self.limit_companies = limit_companies

    def companies(self) -> list[dict]:
        out, url, pages = [], API, 0
        while url and pages < self.max_pages:
            try:
                data = http.get_json(url)
            except FetchError:
                break
            out.extend(data.get("companies") or [])
            url = data.get("nextPage")
            pages += 1
            if self.limit_companies and len(out) >= self.limit_companies:
                break
        return out[: self.limit_companies] if self.limit_companies else out

    def discover(self) -> Iterator[BoardRef]:
        seen: set[tuple[str, str]] = set()

        """
        news.ycombinator.com/jobs is cheap and occasionally carries direct
        ATS links, so take it before the expensive sweep.
        """
        try:
            for ref in urls.extract_all(http.get_text(HN_JOBS)):
                if (ref.ats, ref.slug) not in seen:
                    seen.add((ref.ats, ref.slug))
                    yield BoardRef(ref.ats, ref.slug, None, self.name, {"origin": "hn_jobs"})
        except FetchError:
            pass

        if not self.probe_careers:
            return

        targets = []
        for c in self.companies():
            dom = domain_of(c.get("website") or "")
            if dom:
                targets.append((dom, c.get("name")))
        for domain, name, careers_url, refs in probe_many(targets, workers=self.workers):
            """
            Only claim the page belongs to this company when it points at a
            handful of boards. Beyond that it is an aggregator: keep the
            boards, drop the attribution.
            """
            owned = len(refs) <= MAX_ATTRIBUTABLE_REFS
            for ref in refs:
                if (ref.ats, ref.slug) in seen:
                    continue
                seen.add((ref.ats, ref.slug))
                """
                ...and even for a single board, the domain has to look like
                it belongs to it. An acquired company's careers page still
                resolves and now serves the acquirer's board: visly.app
                serves Figma's, and attributing it would file Figma under
                visly.app forever.
                """
                mine = owned and plausibly_same(domain, ref.slug, name)
                yield BoardRef(
                    ref.ats,
                    ref.slug,
                    name if mine else None,
                    self.name,
                    {
                        "origin": "careers",
                        "aggregator": not owned,
                        "found_on": domain,
                        "attributed": mine,
                    },
                    website=f"https://{domain}" if mine else None,
                    careers_url=careers_url if mine else None,
                )
