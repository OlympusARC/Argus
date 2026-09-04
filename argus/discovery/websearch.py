"""Web search -> fetch the linking pages -> mine them for ATS links.

Generalizes BOARD's google_linkto. The insight there was right and worth
keeping: searching for pages *on* jobs.ashbyhq.com finds nothing, because the
board is a JS-rendered SPA. Searching for pages that *mention* those URLs finds
plenty, because career pages, blogs and aggregators are ordinary HTML.

The original bound this to scraping Google via googlesearch-python, which is
fragile and ToS-grey. Here the search backend is pluggable and picks the first
one actually available:

  1. Monid -> TinyFish /search  (MONID_API_KEY) -- metered at $0 per call, so
     it costs nothing but a key. Preferred for that reason alone.
  2. Brave Search API           (BRAVE_API_KEY) -- free tier, needs a key.
  3. DuckDuckGo HTML            (no key) -- verified dead 2026-08: answers 202
     with an anti-bot page, so the source reports itself unavailable rather
     than silently returning nothing.

The premise above holds for keyword engines, which is what backends 2 and 3
are: they index HTML as served, so a board is invisible and only the pages
mentioning it can be found. TinyFish renders the page first, so it sees the
boards themselves -- which makes a direct host sweep possible there and only
there. Measured on three pages of one host: 26 boards with include_domains
against 14 without.

So the query plan is per-backend, not global. That is the whole reason
_plan() exists rather than one QUERIES list.
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
MONID = "https://api.monid.ai/v1/run"

"""
Monid brokers many providers behind one endpoint; the provider and endpoint
are part of the request body rather than the URL.
"""
MONID_PROVIDER = "tinyfish"
MONID_ENDPOINT = "/search"

"""
Sharpens TinyFish's ranking. It asks for a short statement of what the results
are for, and this task is unusual enough to be worth saying plainly: we want
the pages that mention a board, not the board.
"""
MONID_PURPOSE = (
    "Find applicant-tracking-system job board URLs for individual companies, "
    "and web pages that link to them."
)

"""
For the keyword backends: find pages that *mention* a board.
"""
QUERIES = (
    '"jobs.ashbyhq.com"',
    '"jobs.ashbyhq.com" careers hiring',
    '"jobs.ashbyhq.com" apply engineering',
    '"job-boards.greenhouse.io" careers',
    '"jobs.lever.co" careers hiring',
)

"""
For the rendering backend: sweep each ATS host directly.

bamboohr is absent on purpose. Its boards are <slug>.bamboohr.com, but the
apex domain only ever returns BambooHR's own careers page -- the filter
matches the vendor rather than its customers, and three phrasings all
returned www.bamboohr.com/careers/ and nothing else.
"""
SWEEP_HOSTS = (
    "jobs.ashbyhq.com",
    "job-boards.greenhouse.io",
    "boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.smartrecruiters.com",
    "breezy.hr",
    "recruitee.com",
    "myworkdayjobs.com",
)

"""
Two phrasings per host. Ranking differs enough between them to be worth the
second call, and both are free.
"""
SWEEP_PHRASINGS = (
    "company careers open positions",
    "software engineer openings",
)

_DDG_HREF = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"')


def _monid_results(payload) -> list[str]:
    """Pull result URLs out of a Monid /run response.

    The broker wraps a provider's own response, and how deeply is the
    provider's business rather than a documented contract. Rather than pin one
    shape and break when it is wrapped one level further, walk the structure
    for objects carrying a `url` -- TinyFish documents position/title/url/
    site_name/snippet per result, and `url` is the only field this source
    needs.
    """
    found: list[str] = []
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            u = node.get("url")
            if isinstance(u, str) and u.startswith(("http://", "https://")):
                found.append(u)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    """
    The walk is depth-first over a stack, so results come back reversed;
    TinyFish returns them ranked and that order is worth keeping.
    """
    seen, ordered = set(), []
    for u in reversed(found):
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered


class WebSearchSource(Source):
    name = "websearch"

    def __init__(self, queries=None, per_query: int = 20, pause: float = 2.0, pages: int = 3):
        self.queries = tuple(queries or QUERIES)
        self.per_query = per_query
        self.pause = pause
        self.pages = pages
        self.backend = None

    def available(self) -> tuple[bool, str]:
        if os.getenv("MONID_API_KEY"):
            self.backend = "monid"
            return True, ""
        if os.getenv("BRAVE_API_KEY"):
            self.backend = "brave"
            return True, ""
        """
        DuckDuckGo's keyless HTML endpoint now answers 202 with an anti-bot
        page for every request, so it yields nothing. Reporting unavailable
        beats silently returning zero refs and looking like a dry vector.
        """
        self.backend = "ddg"
        return False, (
            "set MONID_API_KEY (tinyfish /search is metered at $0) or BRAVE_API_KEY; "
            "DuckDuckGo keyless is anti-bot blocked"
        )

    def _plan(self) -> list[tuple[str, str | None]]:
        """(query, include_domains) pairs, chosen by backend."""
        if self.backend == "monid":
            return [(p, h) for h in SWEEP_HOSTS for p in SWEEP_PHRASINGS]
        return [(q, None) for q in self.queries]

    def _monid(self, query: str, domains: str | None = None) -> list[str]:
        """One TinyFish search through Monid's broker.

        Paged: TinyFish caps a page at its own size and accepts page 0-10, so
        asking for more than one page is how per_query is actually honoured
        rather than silently truncated to whatever one page holds.
        """
        out: list[str] = []
        headers = {
            "Authorization": f"Bearer {os.environ['MONID_API_KEY']}",
            "Content-Type": "application/json",
        }
        for page in range(self.pages):
            """
            The result fields are documented (position, title, url, site_name,
            snippet) but the request keys are not, beyond the filter names in
            the endpoint's own description. `POST /v1/inspect` with this
            provider and endpoint returns the input schema and settles it; if
            the key is not `query`, that is the line to change.
            """
            params = {
                "query": query,
                "domain_type": "web",
                "purpose": MONID_PURPOSE,
                "page": page,
            }
            if domains:
                params["include_domains"] = domains
            try:
                data = http.post_json(
                    MONID,
                    json={
                        "provider": MONID_PROVIDER,
                        "endpoint": MONID_ENDPOINT,
                        "input": {"queryParams": params},
                    },
                    headers=headers,
                )
            except FetchError:
                break
            hits = _monid_results(data)
            if not hits:
                break
            out.extend(hits)
            if len(out) >= self.per_query:
                break
        return out[: self.per_query]

    def _search(self, query: str, domains: str | None = None) -> list[str]:
        if self.backend == "monid":
            return self._monid(query, domains)
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
        for query, domains in self._plan():
            for url in self._search(query, domains):
                if url in visited:
                    continue
                visited.add(url)
                """
                the result URL itself sometimes contains the board link
                """
                direct = urls.extract_all(url)
                for ref in direct:
                    if (ref.ats, ref.slug) not in seen:
                        seen.add((ref.ats, ref.slug))
                        yield BoardRef(
                            ref.ats, ref.slug, None, self.name, {"via": "result_url"}
                        )
                """
                A result that was itself a board is not worth fetching: it is
                the SPA, and its served HTML is the empty shell that made a
                keyword engine useless here in the first place. Only pages
                that merely mention a board repay the round trip.
                """
                if direct:
                    continue
                try:
                    html = http.get_text(url, timeout=10)
                except (FetchError, Exception):
                    continue
                for ref in urls.extract_all(html):
                    if (ref.ats, ref.slug) not in seen:
                        seen.add((ref.ats, ref.slug))
                        yield BoardRef(ref.ats, ref.slug, None, self.name, {"via": url[:120]})
            time.sleep(self.pause)
