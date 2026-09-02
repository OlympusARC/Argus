"""Discovery source registry.

Sources only ever yield BoardRefs. They never validate and never poll, so a
noisy high-recall source costs nothing but HTTP -- `validate` is the single
place that decides what is real.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .ashby_customers import AshbyCustomersSource
from .base import Source
from .commoncrawl import CommonCrawlSource
from .funding import FundingSource
from .github import GitHubSource
from .hn import HackerNewsSource
from .hnhiring import HNHiringSource
from .jobjson import JobArchiveSource, JobJsonSource
from .jobrepos import JobReposSource
from .linkedin import LinkedInSource
from .seedfile import SeedFileSource
from .simplify import SimplifySource
from .urlscan import UrlscanSource
from .vcportfolio import VCPortfolioSource
from .wayback import WaybackSource
from .websearch import WebSearchSource
from .ycombinator import YCombinatorSource
from .zero2sudo import Zero2SudoSource

SOURCES: dict[str, type[Source]] = {
    "seedfile": SeedFileSource,
    "ashby_customers": AshbyCustomersSource,
    "simplify": SimplifySource,
    "jobrepos": JobReposSource,
    "jobjson": JobJsonSource,
    "jobarchive": JobArchiveSource,
    "vcportfolio": VCPortfolioSource,
    "funding": FundingSource,
    "hn": HackerNewsSource,
    "hn_hiring": HNHiringSource,
    "urlscan": UrlscanSource,
    "commoncrawl": CommonCrawlSource,
    "wayback": WaybackSource,
    "github": GitHubSource,
    "websearch": WebSearchSource,
    "ycombinator": YCombinatorSource,
    "linkedin": LinkedInSource,
    "zero2sudo": Zero2SudoSource,
}

"""
Cheapest / highest-precision first, so a partial run is still useful.
"""
DEFAULT_ORDER = (
    "seedfile",
    "ashby_customers",
    "simplify",
    "jobrepos",
    "jobjson",
    "hn",
    "hn_hiring",
    "urlscan",
    "vcportfolio",
    "commoncrawl",
    "github",
    "funding",
    "websearch",
    "ycombinator",
)

"""
Real sources, excluded from a default run because they are slow, rate-limited
or need credentials. Run explicitly with -s <name>.
"""
OPT_IN = ("wayback", "linkedin", "zero2sudo", "jobarchive")


def build(name: str, **kwargs) -> Source:
    return SOURCES[name](**kwargs)


"""
The sweep itself, lifted out of the CLI so that anything -- a command, an
orchestrator node, a test -- can run it and read what happened. The command
keeps only its printing.
"""


@dataclass
class SourceResult:
    """One source's contribution to a sweep.

    Carries the skip and failure cases rather than raising, because a sweep
    over eighteen sources must survive any one of them: whatever a dying
    source yielded before it died is already written and still worth having.
    """

    source: str
    refs_seen: int = 0
    new_boards: int = 0
    new_companies: int = 0
    postings: int = 0
    funding: int = 0
    blocked: int = 0
    duration_s: float = 0.0
    error: str | None = None
    skipped: str | None = None
    """
    Dry runs write nothing, so their counts come from what was accumulated
    rather than from what the registry reports.
    """
    dry: bool = False
    dry_refs: int = 0
    dry_unique: int = 0
    dry_companies: int = 0
    dry_postings: int = 0


def _record(conn, obs_runs, result: SourceResult, dry_run: bool) -> None:
    """A skipped source still gets a row.

    Otherwise "never ran" and "ran and found nothing" are indistinguishable
    in the history, and the trend calculation would treat a missing API key
    as a regression worth healing.
    """
    if dry_run:
        return
    rid = obs_runs.start(conn, result.source)
    obs_runs.finish(conn, rid, result)


def run(
    conn,
    names: Sequence[str] | None = None,
    *,
    dry_run: bool = False,
    batch: int = 500,
    limit: int | None = None,
    kwargs_for: dict[str, dict] | None = None,
    on_result: Callable[[SourceResult], None] | None = None,
) -> list[SourceResult]:
    """Sweep each source in turn. Returns one result per name, in order.

    on_result fires as each source finishes rather than at the end: a full
    sweep runs for tens of minutes and a caller that only learns the outcome
    afterwards cannot report progress.
    """
    from ..core.models import CompanyRef
    from ..feed import jobs as jobs_mod
    from ..obs import runs as obs_runs
    from ..registry import boards as registry_boards
    from ..registry import companies

    kwargs_for = kwargs_for or {}
    results: list[SourceResult] = []

    for name in names or list(DEFAULT_ORDER):
        if name not in SOURCES:
            r = SourceResult(name, skipped="unknown source")
            results.append(r)
            if on_result:
                on_result(r)
            continue

        src = build(name, **kwargs_for.get(name, {}))
        ok, why = src.available()
        if not ok:
            r = SourceResult(name, skipped=why)
            _record(conn, obs_runs, r, dry_run)
            results.append(r)
            if on_result:
                on_result(r)
            continue

        run_id = None if dry_run else obs_runs.start(conn, name)
        t0 = time.time()
        refs: list = []
        firms: list = []
        postings: list = []
        seen = added = seeded = newfirms = 0

        def flush(source=name):
            """Write incrementally.

            A source like ycombinator runs for 20+ minutes over 6k companies.
            Buffering everything until it finishes means one crash discards the
            whole sweep, so batches land as they are produced.
            """
            nonlocal refs, firms, postings, seen, added, seeded, newfirms
            if dry_run or not (refs or firms):
                return
            if refs:
                res = registry_boards.add_boards(conn, refs, source=source)
                seen += res["seen"]
                added += res["new_boards"]
                """
                A board that names its employer is also a company we know.
                """
                newfirms += companies.add_many(
                    conn,
                    (
                        {
                            "name": r.company_name,
                            "website": r.website,
                            "careers_url": r.careers_url,
                            "board": (r.ats, r.slug),
                        }
                        for r in refs
                        if r.company_name or r.website
                    ),
                    source=source,
                )["new_companies"]
            if firms:
                seen += len(firms)
                newfirms += companies.add_many(
                    conn,
                    (
                        {
                            "name": f.name,
                            "domain": f.domain,
                            "website": f.website,
                            "careers_url": f.careers_url,
                            "board": f.board,
                        }
                        for f in firms
                    ),
                    source=source,
                )["new_companies"]
            if postings:
                seeded += jobs_mod.seed(conn, postings, source=source)
            refs, firms, postings = [], [], []

        err = None
        try:
            for ref in src.discover():
                """
                Sources may yield either kind; see discovery/base.py.
                """
                if isinstance(ref, CompanyRef):
                    firms.append(ref)
                else:
                    refs.append(ref)
                    if ref.posting is not None:
                        postings.append(ref.posting)
                if len(refs) + len(firms) >= batch:
                    flush()
                if limit and (seen + len(refs) + len(firms)) >= limit:
                    break
        except Exception as exc:
            """
            A failing source must not kill the run, and whatever it yielded
            before dying is still worth keeping.
            """
            err = f"{type(exc).__name__}: {exc}"
        flush()

        if dry_run:
            r = SourceResult(
                name,
                dry=True,
                dry_refs=len(refs),
                dry_unique=len({(x.ats, x.slug) for x in refs}),
                dry_companies=len(firms),
                dry_postings=len(postings),
                duration_s=time.time() - t0,
                error=err,
            )
        else:
            funding = 0
            """
            Some sources carry a signal beyond the boards they yield.
            """
            if hasattr(src, "record") and getattr(src, "_last_companies", None):
                funding = src.record(conn, src._last_companies) or 0
            r = SourceResult(
                name,
                refs_seen=seen,
                new_boards=added,
                new_companies=newfirms,
                postings=seeded,
                funding=funding,
                blocked=int(getattr(src, "blocked", 0) or 0),
                duration_s=time.time() - t0,
                error=err,
            )
        if run_id is not None:
            obs_runs.finish(conn, run_id, r)
        results.append(r)
        if on_result:
            on_result(r)
    return results
