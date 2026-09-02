"""Ashby's own published customer list.

Tiny but the highest-precision source there is -- these are companies Ashby
names publicly. The live page is a JS-rendered marketing site (916 KB of HTML
yielding one usable link), so the reliable part is the extracted customer list;
we still scrape the page opportunistically in case it degrades to static HTML.

List ported from binaryshrey/BOARD.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from ..core import http, urls
from ..core.models import BoardRef, FetchError
from .base import Source

CUSTOMERS_URL = "https://www.ashbyhq.com/customers"

KNOWN = (
    "ramp",
    "notion",
    "linear",
    "cursor",
    "replit",
    "clay",
    "harvey",
    "vanta",
    "retool",
    "posthog",
    "deel",
    "shopify",
    "snowflake",
    "zapier",
    "reddit",
    "mercury",
    "ironclad",
    "lemonade",
    "lime",
    "gorgias",
    "uipath",
    "deliveroo",
    "alan",
    "altura",
    "amo",
    "aurora-solar",
    "boomi",
    "brightline",
    "coder",
    "convictional",
    "dave",
    "eightsleep",
    "flock-safety",
    "form-energy",
    "fullstory",
    "hackerone",
    "january",
    "marqeta",
    "monte-carlo",
    "multiverse",
    "netgear",
    "sequoia",
    "stytch",
    "oyster",
    "hopper",
    "superhumanapp",
    "cohere",
    "supabase",
    "teal",
)

_HREF = re.compile(r"/customers/([a-z0-9][a-z0-9-]{1,60})", re.I)


class AshbyCustomersSource(Source):
    name = "ashby_customers"

    def discover(self) -> Iterator[BoardRef]:
        seen: set[str] = set()
        for slug in KNOWN:
            seen.add(slug)
            yield BoardRef("ashby", slug, None, self.name, {"origin": "published_list"})
        try:
            html = http.get_text(CUSTOMERS_URL)
        except FetchError:
            return
        """
        /customers/{slug} case-study links, when the page serves static HTML
        """
        for m in _HREF.finditer(html):
            slug = m.group(1).lower()
            if slug not in seen:
                seen.add(slug)
                yield BoardRef("ashby", slug, None, self.name, {"origin": "page"})
        for ref in urls.extract_all(html):
            if ref.ats == "ashby" and ref.slug not in seen:
                seen.add(ref.slug)
                yield BoardRef(ref.ats, ref.slug, None, self.name, {"origin": "page_link"})
