"""Company-name normalization.

One definition, shared. Three callers need to answer "are these the same
company?" -- the companies registry merging rows, the careers prober guessing a
domain, and the funding source filtering fund vehicles -- and they must answer
it identically or the registry grows duplicate companies that each hold half
the metadata.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_CLEAN = re.compile(r"[^a-z0-9]+")
"""
"Databricks, Inc." must become databricks.com, not databricksinc.com.
"""
_SUFFIX = re.compile(
    r"[,\s]+(inc|inc\.|incorporated|corp|corp\.|corporation|co|co\.|company|"
    r"plc|nv|bv|ag|gmbh|sa|pbc|pte|oy|ab)\.?$",
    re.I,
)


def base(name: str) -> str:
    """Company name -> the stem of its likely domain, and its identity key.

    'Databricks, Inc.' and 'Databricks' both collapse to 'databricks', which is
    what lets two sources naming the same company land on one row.
    """
    if not name:
        return ""
    prev = None
    while prev != name:  # "Foo, Inc. Corp." needs two passes
        prev = name
        name = _SUFFIX.sub("", name.strip())
    return _CLEAN.sub("", name.lower())


"""
Kept under the old name because funding.py reads as prose with it.
"""
name_to_base = base


"""
Hosts that are somebody's careers page but never a company's own domain.
Attaching one of these as a company's website merges unrelated companies.
"""
NOT_A_COMPANY = {
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "workable.com",
    "recruitee.com",
    "breezy.hr",
    "icims.com",
    "bamboohr.com",
    "rippling.com",
    "jobvite.com",
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "github.com",
    "simplify.jobs",
    "google.com",
    "notion.site",
    "notion.so",
}


def plausibly_same(domain: str | None, *identifiers: str | None) -> bool:
    """Could this domain belong to the thing these identifiers name?

    Guards against the acquisition trap. An acquired company's careers page
    keeps working and now points at the acquirer's board: `visly.app/careers`
    serves Figma's Greenhouse board, so a prober walking a list of company
    domains happily concludes that Figma's domain is visly.app. The board is
    real and worth keeping; the attribution is wrong.

    Containment rather than equality, because a company's domain stem and its
    ATS slug agree far more often than they match exactly -- weaveos.com runs
    the board `weave`. Missing an attribution is cheap (the company stays
    unresolved and gets probed again); a wrong one silently mislabels a company
    and hands it someone else's careers page.
    """
    host = apex(domain) or ""
    stem = _CLEAN.sub("", host.split(".")[0]) if host else ""
    if not stem:
        return False
    """
    A startup whose name is taken buys the name with a verb bolted on:
    usesimple.ai is Simple AI, withdavid.ai is David AI, heymalama.co is
    Malama Health. Comparing the bare stem would reject all of them.
    """
    stems = {stem}
    for prefix in ("get", "use", "with", "try", "join", "hey", "go", "my", "the", "we"):
        if stem.startswith(prefix) and len(stem) > len(prefix) + 2:
            stems.add(stem.removeprefix(prefix))
    for ident in identifiers:
        other = base(ident or "")
        if not other:
            continue
        if any(s == other or s in other or other in s for s in stems):
            return True
    return False


def apex(domain: str | None) -> str | None:
    """Strip www. and lowercase. Deliberately not a public-suffix parse.

    Trimming to a true apex would merge every *.myworkdayjobs.com tenant into
    one company, which is exactly wrong. Keeping the host as-is and rejecting
    the known ATS hosts is both simpler and more correct here.
    """
    if not domain:
        return None
    raw = domain.strip().lower()
    if not raw:
        return None
    """
    Always go through urlsplit, even for a bare host: without it a value like
    "boards.greenhouse.io/stripe" keeps its path, matches no entry in
    NOT_A_COMPANY, and gets stored as if it were a company's own domain.
    """
    try:
        host = urlsplit(raw if "://" in raw else "https://" + raw).hostname or ""
    except ValueError:
        return None
    host = host.removeprefix("www.")
    if not host or "." not in host or " " in host:
        return None
    if any(host == bad or host.endswith("." + bad) for bad in NOT_A_COMPANY):
        return None
    return host
