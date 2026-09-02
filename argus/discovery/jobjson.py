"""Structured job-list repos, read by shape rather than by schema.

Dozens of GitHub repos publish a machine-readable list of postings, and no two
agree on field names: `url` / `applyUrl` / `link`, `company_name` / `company` /
`employer`, `companyDomain` / `company_website`. Writing a parser per repo
means a parser to fix every time one of them renames a column.

So this walks the JSON instead. For every object in the tree it asks two
questions -- does any string here parse as an ATS board, and does any string
here look like the employer's own domain -- and keeps whatever it finds. A repo
can restructure entirely and this still reads it, which is the same bet
`urls.extract_all` makes on raw HTML.

The company half matters as much as the board half. Several of these lists
route every apply link through their own domain, so they yield no boards at
all -- but they still name the employer and its website, and that is a probe
target the careers prober turns into a board later.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from ..core import config, http, urls
from ..core.models import BoardRef, CompanyRef, FetchError
from ..core.names import apex
from .base import Source

RAW = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"

"""
Keys whose value is the employer's name, in rough order of how often a repo
picks each one.
"""
NAME_KEYS = (
    "company_name",
    "companyName",
    "company",
    "employer",
    "org",
    "organization",
    "organisation",
)
"""
Keys whose value is the employer's own site. `company_url` is deliberately
included even though several repos point it at their own profile page --
apex() rejects those hosts, so a wrong guess costs nothing.
"""
DOMAIN_KEYS = (
    "companyDomain",
    "company_domain",
    "company_website",
    "companyWebsite",
    "company_url",
    "companyUrl",
    "website",
    "domain",
    "site",
)


def _walk(node: Any, depth: int = 0) -> Iterator[dict]:
    """Yield every dict in a JSON tree, deepest structure included."""
    if depth > 12:
        return
    if isinstance(node, dict):
        yield node
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from _walk(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            if isinstance(v, (dict, list)):
                yield from _walk(v, depth + 1)


def _first(obj: dict, keys) -> str | None:
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _board_in(obj: dict):
    """The first ATS board any string value in this object points at."""
    for v in obj.values():
        if isinstance(v, str) and "/" in v:
            ref = urls.parse(v)
            if ref:
                return ref
    return None


def parse_document(text: str) -> Any:
    """JSON, or NDJSON, whichever this is. Several repos ship the latter."""
    text = text.lstrip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows or None


class JobJsonSource(Source):
    name = "jobjson"

    def __init__(self, repos=None, timeout: float = 180.0):
        self.repos = repos or config.JOBJSON_REPOS
        self.timeout = timeout

    def discover(self) -> Iterator[BoardRef | CompanyRef]:
        seen_boards: set[tuple[str, str]] = set()
        seen_companies: set[str] = set()
        for repo, branch, path in self.repos:
            try:
                text = http.get_text(
                    RAW.format(repo=repo, branch=branch, path=path),
                    probe=True,
                    timeout=self.timeout,
                )
            except (FetchError, OSError, ValueError):
                continue
            doc = parse_document(text)
            if doc is None:
                continue
            for obj in _walk(doc):
                name = _first(obj, NAME_KEYS)
                domain = apex(_first(obj, DOMAIN_KEYS))
                ref = _board_in(obj)
                if ref is not None:
                    if (ref.ats, ref.slug) not in seen_boards:
                        seen_boards.add((ref.ats, ref.slug))
                        yield BoardRef(
                            ref.ats,
                            ref.slug,
                            name,
                            self.name,
                            {"repo": repo},
                            website=f"https://{domain}" if domain else None,
                        )
                    continue
                """
                No board here, but a named employer is still worth having:
                the careers prober turns a domain into a board.
                """
                if not (name or domain):
                    continue
                key = domain or (name or "").lower()
                if key in seen_companies:
                    continue
                seen_companies.add(key)
                yield CompanyRef(
                    name=name,
                    domain=domain,
                    website=f"https://{domain}" if domain else None,
                    source=self.name,
                    detail={"repo": repo},
                )


class JobArchiveSource(JobJsonSource):
    """The same reader pointed at the archive files.

    Split out rather than flagged because the cost profile is completely
    different: ~90 MB of closed postings whose only value is the boards they
    name, which is worth paying once and never on a weekly cadence.
    """

    name = "jobarchive"

    def __init__(self, repos=None, timeout: float = 600.0):
        super().__init__(repos=repos or config.JOBJSON_ARCHIVES, timeout=timeout)
