"""Newly funded companies, via SEC Form D filings.

A US company files Form D within 15 days of closing a private raise, so the
EDGAR daily index is a free, official, structured feed of "who just raised".
Funding is the strongest leading indicator of hiring there is: the money
arrives, then the job posts do.

That ordering is why this is worth having even though the ATS is upstream of
social. A company that raised last week may not have a board yet -- catching
the filing means we are already watching when the board appears.

EDGAR requires a descriptive User-Agent with contact details and rate-limits
to ~10 requests/second; both are respected here.

Roughly 170 filings land per day, but most are SPVs, syndicates and fund
vehicles rather than operating companies, so those are filtered out by name
before we spend any probes on them.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from datetime import date, timedelta
from typing import NamedTuple

from ..core import http
from ..core.models import BoardRef, FetchError
from ..core.names import name_to_base, plausibly_same
from ..feed import jobs as jobs_mod
from .base import Source
from .probe import probe

DAILY = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{q}/form.{stamp}.idx"

"""
Fund vehicles, not operating companies. These dominate the raw feed.
"""
_VEHICLE = re.compile(
    r"(\b(fund|funds|llc|ltd|spv|scsp|sicav|syndicate|partners|partnership|"
    r"capital|ventures?|holdings?|trust|acquisition|reit|opportunit\w*|"
    r"advisors?|management|realty|properties|equity|associates|"
    r"co-?invest\w*)\b"
    r"|\bl\.?p\.?$|,\s*l\.?p\.?\b|\bs\.?a\.?r\.?l\b"
    r"|\bseries\s+[A-Z0-9]|\b[IVX]{1,4}\s*,?\s*l\.?p\.?)",
    re.I,
)


class Filing(NamedTuple):
    company: str
    cik: str
    filed: str


class FundingSource(Source):
    name = "funding"

    def __init__(
        self,
        days: int = 14,
        contact: str | None = None,
        probe_careers: bool = True,
        max_probe: int = 400,
    ):
        self.days = days
        self.contact = contact or os.getenv("ARGUS_SEC_CONTACT", "")
        self.probe_careers = probe_careers
        self.max_probe = max_probe

    def available(self) -> tuple[bool, str]:
        """
        Verified: EDGAR 403s a generic agent and 200s one carrying an email.
        Better to say so than to fail mid-run.
        """
        if "@" not in (self.contact or ""):
            return False, (
                "set ARGUS_SEC_CONTACT to 'Name you@example.com' -- "
                "SEC requires contact details in the User-Agent"
            )
        return True, ""

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.contact, "Accept-Encoding": "gzip, deflate"}

    def filings(self) -> list[Filing]:
        out: list[Filing] = []
        today = date.today()
        for back in range(self.days):
            d = today - timedelta(days=back)
            if d.weekday() >= 5:  # EDGAR does not publish weekends
                continue
            stamp = d.strftime("%Y%m%d")
            url = DAILY.format(year=d.year, q=(d.month - 1) // 3 + 1, stamp=stamp)
            try:
                """
                get_lines is a generator: the HTTP error surfaces on the
                first iteration, not at the call, so the loop must sit inside
                the try or the failure escapes. SEC answers 403 (not 404) for
                an index file that does not exist yet, e.g. today's.
                """
                for line in http.get_lines(url, headers=self._headers()):
                    if not line.startswith("D "):
                        continue
                    """
                    Fixed width: form, company name, CIK, date, path
                    """
                    m = re.match(r"^D\s{2,}(.+?)\s{2,}(\d+)\s+(\d{8})\s", line)
                    if m:
                        out.append(Filing(m.group(1).strip(), m.group(2), m.group(3)))
            except FetchError:
                continue
            time.sleep(0.15)  # EDGAR asks for <10 req/s
        return out

    @staticmethod
    def is_operating_company(name: str) -> bool:
        """Filter fund vehicles out before spending probes on them."""
        if _VEHICLE.search(name):
            return False
        base = name_to_base(name)
        return 2 < len(base) < 40

    def record(self, conn, filings: list[Filing]) -> int:
        """Write a funding event per filing.

        This is the actual deliverable. Whether the company already has a board
        we poll is irrelevant -- "they just raised" is the signal, and it
        arrives before the job posts do. Deduped on (cik, filed) so re-running
        over an overlapping window is free.
        """
        added = 0
        for f in filings:
            dupe = conn.execute(
                """SELECT 1 FROM events WHERE type='funding'
                   AND external_id=? AND title=?""",
                (f.cik, f.company),
            ).fetchone()
            if dupe:
                continue
            jobs_mod.record_event(
                conn,
                "funding",
                None,
                None,
                f.cik,
                f.company,
                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={f.cik}",
                {"form": "D", "filed": f.filed},
            )
            added += 1
        return added

    def discover(self) -> Iterator[BoardRef]:
        seen: set[tuple[str, str]] = set()
        companies = [f for f in self.filings() if self.is_operating_company(f.company)]
        self._last_companies = companies
        if not self.probe_careers:
            return
        for filing in companies[: self.max_probe]:
            base = name_to_base(filing.company)
            if not base:
                continue
            for tld in (".com", ".io", ".ai"):
                dom = base + tld
                try:
                    url, refs = probe(dom)
                except Exception:
                    continue
                if not url:
                    continue
                for ref in refs:
                    if (ref.ats, ref.slug) in seen:
                        continue
                    seen.add((ref.ats, ref.slug))
                    """
                    The domain here is a guess from the filing name, so it
                    has to look like the board it found -- otherwise one
                    acquired company's careers page files the acquirer under
                    the wrong domain.
                    """
                    mine = plausibly_same(dom, ref.slug, filing.company)
                    yield BoardRef(
                        ref.ats,
                        ref.slug,
                        filing.company if mine else None,
                        self.name,
                        {"form_d_filed": filing.filed, "cik": filing.cik, "attributed": mine},
                        website=f"https://{dom}" if mine else None,
                        careers_url=url if mine else None,
                    )
                break
