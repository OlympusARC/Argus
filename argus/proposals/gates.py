"""The deterministic half: measure the claim, then decide.

A gate never asks whether a proposal sounds right. It performs the check the
proposal implicitly makes and counts the result, so a rejection carries
evidence rather than a verdict. That is what makes an unreliable proposer
safe to run: the model's job is to generate candidates, and generating a bad
candidate costs one fetch.

What has no gate here cannot be auto-applied, and what has no applier cannot
be enacted at all. `diagnosis` deliberately has neither: a healer's output is
something you read, never something that edits the system.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import AUTO_APPLIED, PENDING, REJECTED

"""
How many boards new to the registry a proposed source must yield to apply
itself. Set against the measured spread: the sources worth having found
thousands each, and the ones not worth having found under a hundred.
"""
SOURCE_AUTO_APPLY = 25

"""
A proposed classification pattern must be this precise on a held-out sample.
Below it the rule would mislabel more than it fixes, and a mislabelled
posting is worse than an unlabelled one -- it is silently wrong.
"""
RULESET_MIN_PRECISION = 0.90


@dataclass
class Verdict:
    status: str
    score: float | None = None
    evidence: dict = field(default_factory=dict)


def _gate_source(conn, p: dict) -> Verdict:
    """Fetch the proposed page and count boards we do not already have.

    This is the whole safety argument for the prospector in one function: it
    does not matter how convincing the model's reasoning was, only how many
    real boards the URL actually yields.
    """
    from ..core import http, urls

    url = p["payload"].get("url")
    if not url:
        return Verdict(REJECTED, 0.0, {"why": "no url in payload"})
    try:
        html = http.get_text(url, probe=True, max_bytes=2_000_000)
    except Exception as exc:
        return Verdict(REJECTED, 0.0, {"why": f"unfetchable: {type(exc).__name__}"})

    refs = list(urls.extract_all(html))
    if not refs:
        return Verdict(REJECTED, 0.0, {"boards_found": 0, "why": "no board links"})

    known = {(r["ats"], r["slug"]) for r in conn.execute("SELECT ats, slug FROM boards")}
    fresh = {(r.ats, r.slug) for r in refs} - known
    ev = {
        "boards_found": len(refs),
        "boards_new": len(fresh),
        "sample": sorted(f"{a}/{s}" for a, s in list(fresh)[:10]),
    }
    if len(fresh) >= SOURCE_AUTO_APPLY:
        return Verdict(AUTO_APPLIED, float(len(fresh)), ev)
    if fresh:
        return Verdict(PENDING, float(len(fresh)), ev)
    return Verdict(REJECTED, 0.0, {**ev, "why": "every board already known"})


def _gate_ruleset(conn, p: dict) -> Verdict:
    """Compile the proposed pattern and score it on a labelled sample.

    The sample is titles the current ruleset already places confidently. A
    pattern that fires on those is claiming they were misfiled, which is a
    much stronger claim than "this catches something new" and is exactly what
    precision measures here.
    """
    from ..classify import classify

    pattern = p["payload"].get("pattern")
    family = p["payload"].get("family")
    if not pattern or not family:
        return Verdict(REJECTED, 0.0, {"why": "pattern and family are both required"})
    try:
        rx = re.compile(pattern, re.I)
    except re.error as exc:
        return Verdict(REJECTED, 0.0, {"why": f"uncompilable: {exc}"})

    rows = conn.execute(
        """SELECT DISTINCT title, role_family FROM jobs
           WHERE title IS NOT NULL AND role_family IS NOT NULL LIMIT 5000"""
    ).fetchall()
    hits = [r for r in rows if rx.search(r["title"] or "")]
    if not hits:
        return Verdict(REJECTED, 0.0, {"why": "matches nothing in the sample"})

    """
    A hit is correct if the current ruleset had no opinion (it is genuinely
    new information) or already agrees. A hit that contradicts a confident
    existing label is the expensive kind of wrong.
    """
    good = sum(
        1
        for r in hits
        if r["role_family"] in (None, "other", family) or classify(r["title"]).family == family
    )
    precision = good / len(hits)
    ev = {
        "sample_hits": len(hits),
        "agree": good,
        "precision": round(precision, 3),
        "conflicts": [
            r["title"] for r in hits if r["role_family"] not in (None, "other", family)
        ][:10],
    }
    if precision >= RULESET_MIN_PRECISION:
        return Verdict(AUTO_APPLIED, precision, ev)
    return Verdict(PENDING, precision, ev)


def _apply_source(conn, p: dict) -> None:
    """Ingest the boards the gate already proved were there.

    extract_all yields Refs -- an ats and a slug parsed out of a URL. The
    registry takes BoardRefs, which additionally carry who found them and
    what else was known about the company. The conversion is the same one
    every discovery source does; the source name records which proposal it
    came from, so an auto-applied page that later turns out to be junk can be
    traced back to the proposal that argued for it.
    """
    from ..core import http, urls
    from ..core.models import BoardRef
    from ..registry import boards as registry_boards

    src = f"proposal:{p['id']}"
    seen: set[tuple[str, str]] = set()
    refs = []
    for r in urls.extract_all(http.get_text(p["payload"]["url"], probe=True)):
        if (r.ats, r.slug) in seen:
            continue
        seen.add((r.ats, r.slug))
        refs.append(BoardRef(r.ats, r.slug, None, src, {"url": p["payload"]["url"]}))
    if refs:
        registry_boards.add_boards(conn, refs, source=src)


def _apply_ruleset(conn, p: dict) -> None:
    """Ruleset patches are code, not data.

    Applying one means writing a regex into classify/__init__.py and bumping
    RULESET, which is a commit a human makes and reviews. Recording acceptance
    is the whole action here -- pretending otherwise would put a model's
    output directly into the code path that labels every posting.
    """
    return None


# Deliberately absent: `diagnosis`. A healer's finding is a hypothesis to read,
# and giving it a gate would be the first step toward letting it act.
GATES = {
    "source": _gate_source,
    "ruleset_patch": _gate_ruleset,
}

APPLIERS = {
    "source": _apply_source,
    "ruleset_patch": _apply_ruleset,
}
