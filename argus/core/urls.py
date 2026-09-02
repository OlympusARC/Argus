"""Recognize ATS board URLs and extract (ats, slug, external_id).

Every discovery source hands us URLs, not slugs, so this is the single place
that knows what a board URL looks like. We parse *all* known ATSs even though
only Ashby is polled today: unrecognized boards still land in the registry, so
adding an adapter later is a flag flip rather than a re-run of discovery.
"""

from __future__ import annotations

import html
import re
from typing import NamedTuple
from urllib.parse import parse_qs, urlsplit


class Ref(NamedTuple):
    ats: str
    slug: str
    external_id: str | None = None


"""
First path segments on jobs.ashbyhq.com that are app routes, not boards.
Ashby 404s these anyway, so this is a cost optimization, not correctness.
"""
ASHBY_RESERVED = {
    "meeting",
    "api",
    "b",
    "embed",
    "assets",
    "static",
    "_next",
    "favicon.ico",
    "robots.txt",
    "sitemap.xml",
    "manifest.json",
    "login",
    "signup",
    "app",
}
GENERIC_RESERVED = {
    "",
    "jobs",
    "job",
    "search",
    "embed",
    "api",
    "static",
    "assets",
    "privacy",
    "terms",
    "legal",
    "login",
    "signin",
    "signup",
    "about",
    "help",
    "support",
    "contact",
    "pricing",
    "blog",
    "security",
}

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

"""
A file is never a company. Every ATS serves these off the same path shape a
board occupies, so `jobs.lever.co/robots.txt` parses as cleanly as
`jobs.lever.co/ramp` unless the extension is rejected -- and in the crawl
where Lever's board pages were largely absent, robots.txt was almost the
only thing there. Rejected here rather than per-ATS so one rule covers all
of them.
"""
_FILE_RE = re.compile(
    r"\.(txt|xml|ico|html?|json|js|css|png|jpe?g|gif|svg|webp|pdf|php|aspx?)$"
)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _clean_slug(raw: str) -> str | None:
    """
    Ashby (and most ATSs) treat the slug case-insensitively; normalizing
    prevents Ramp/ramp/RAMP becoming three rows that poll one board.
    """
    s = raw.strip().strip("/").lower()
    s = s.split("?")[0].split("#")[0]
    s = s.rstrip(".,);:'\"]>")
    if _FILE_RE.search(s):
        return None
    return s if _SLUG_RE.match(s) else None


def parse(url: str) -> Ref | None:
    """Return a Ref for any recognized ATS URL, else None."""
    if not url or "://" not in url:
        url = "https://" + url.lstrip("/")
    try:
        u = urlsplit(url)
    except ValueError:
        return None
    host = (u.hostname or "").lower().removeprefix("www.")
    parts = [p for p in u.path.split("/") if p]
    qs = parse_qs(u.query)

    """
    --- Ashby -----------------------------------------------------------
    """
    if host == "jobs.ashbyhq.com":
        if not parts:
            return None
        slug = _clean_slug(parts[0])
        if not slug or slug in ASHBY_RESERVED:
            return None
        ext = parts[1] if len(parts) > 1 and _UUID_RE.match(parts[1]) else None
        return Ref("ashby", slug, ext)

    """
    --- Greenhouse ------------------------------------------------------
    """
    if host.endswith("greenhouse.io"):
        """
        boards.greenhouse.io/embed/job_board?for={slug}
        """
        if "for" in qs:
            slug = _clean_slug(qs["for"][0])
            token = qs.get("token", [None])[0]
            return Ref("greenhouse", slug, token) if slug else None
        if (
            host
            in (
                "boards.greenhouse.io",
                "job-boards.greenhouse.io",
                "boards.eu.greenhouse.io",
                "job-boards.eu.greenhouse.io",
            )
            and parts
        ):
            if parts[0] in ("embed", "job_app"):
                return None
            slug = _clean_slug(parts[0])
            if not slug or slug in GENERIC_RESERVED:
                return None
            ext = parts[2] if len(parts) > 2 and parts[1] == "jobs" else None
            return Ref("greenhouse", slug, ext)
        return None

    """
    --- Lever -----------------------------------------------------------
    """
    if host in ("jobs.lever.co", "jobs.eu.lever.co"):
        if not parts:
            return None
        slug = _clean_slug(parts[0])
        if not slug or slug in GENERIC_RESERVED:
            return None
        ext = parts[1] if len(parts) > 1 and _UUID_RE.match(parts[1]) else None
        return Ref("lever", slug, ext)

    """
    --- Workday ---------------------------------------------------------
    {tenant}.wdN.myworkdayjobs.com/{lang}/{site}/...  -- the board identity is
    the (tenant, site) pair, so the slug encodes both.
    """
    m = re.match(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$", host)
    if m:
        tenant, pod = m.group(1), m.group(2)
        site = None
        if parts:
            if parts[0] == "wday" and len(parts) >= 4:  # /wday/cxs/{tenant}/{site}
                site = parts[3]
            else:
                site = (
                    parts[1]
                    if len(parts) > 1 and re.match(r"^[a-z]{2}(-[A-Z]{2})?$", parts[0])
                    else parts[0]
                )
        if not site:
            return None
        return Ref("workday", f"{tenant}.{pod}/{site}".lower(), None)

    """
    --- SmartRecruiters -------------------------------------------------
    """
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and parts:
        slug = _clean_slug(parts[0])
        return (
            Ref("smartrecruiters", slug, parts[1] if len(parts) > 1 else None) if slug else None
        )

    """
    --- Workable --------------------------------------------------------
    """
    if host == "apply.workable.com" and parts:
        slug = _clean_slug(parts[0])
        if not slug or slug in GENERIC_RESERVED:
            return None
        ext = parts[2] if len(parts) > 2 and parts[1] == "j" else None
        return Ref("workable", slug, ext)

    """
    --- Single-tenant subdomain ATSs ------------------------------------
    """
    for suffix, ats in (
        (".recruitee.com", "recruitee"),
        (".breezy.hr", "breezy"),
        (".icims.com", "icims"),
        (".bamboohr.com", "bamboohr"),
        (".rippling.com", "rippling"),
    ):
        if host.endswith(suffix):
            slug = _clean_slug(host[: -len(suffix)])
            if slug and slug not in ("jobs", "www", "careers", "app", "api"):
                return Ref(ats, slug, None)
            return None

    if host == "jobs.jobvite.com" and parts:
        slug = _clean_slug(parts[0])
        return Ref("jobvite", slug, None) if slug else None

    return None


_URL_IN_TEXT = re.compile(
    r"""https?://(?:jobs\.ashbyhq\.com|(?:job-)?boards(?:\.eu)?\.greenhouse\.io"""
    r"""|jobs(?:\.eu)?\.lever\.co|apply\.workable\.com|jobs\.smartrecruiters\.com"""
    r"""|[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com|[a-z0-9-]+\.(?:recruitee\.com|breezy\.hr|icims\.com))"""
    r"""/[^\s"'<>)\]}\\]*""",
    re.I,
)


def extract_all(text: str) -> list[Ref]:
    """Pull every recognizable board reference out of a blob of text/HTML.

    Unescaping first is not optional: HN's API returns comment bodies with
    slashes as &#x2F;, which silently defeats every URL regex. Any HTML-bearing
    source has the same problem, so it is handled here once.
    """
    out, seen = [], set()
    for m in _URL_IN_TEXT.finditer(html.unescape(text or "")):
        ref = parse(m.group(0))
        if ref and (ref.ats, ref.slug) not in seen:
            seen.add((ref.ats, ref.slug))
            out.append(ref)
    return out
