"""Diagnose a source that stopped yielding.

Fires on the orchestrator's rule 2, when a source's new-board count collapses
against its own history. It reads the run history, probes what the source
actually talks to, and files a hypothesis. It never edits anything: there is
no gate for `diagnosis` and no applier, so the strongest thing it can do is
be read.

It has a real eval set, which is unusual for an agent written before it has
ever run. Three genuine regressions were diagnosed by hand here in one week:

  - Common Crawl defaulted to one host of ten, so nine ATSs were invisible
    while every run looked normal.
  - matchType=prefix combined with a `host/*` wildcard returns zero pages,
    which reads identically to a host with nothing on it.
  - robots.txt parsed as a company slug, so a crawl with no board pages still
    produced one confident, wrong board.

The shared shape of all three is the thing worth teaching it: a silent zero
is almost never the world being empty.
"""

from __future__ import annotations

import json

from .. import llm
from ..obs import runs as obs_runs

DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "theory": {"type": "string"},
                    "evidence": {"type": "string"},
                    "check": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["theory", "evidence", "check", "confidence"],
                "additionalProperties": False,
            },
        },
        "most_likely": {"type": "string"},
    },
    "required": ["hypotheses", "most_likely"],
    "additionalProperties": False,
}

PROMPT = """You diagnose discovery sources in a job-board crawler that have
stopped finding new boards.

A source fetches third-party pages and extracts references to ATS job boards
(Ashby, Greenhouse, Lever, Workday and similar). It never validates or polls;
its only job is to yield candidate boards.

Failures seen before in this exact system, all of which looked like "nothing
found" rather than an error:
- A host list defaulting to one entry, so most of the surface was never swept.
- A query parameter combination that returns zero results rather than an
  error (a wildcard plus a prefix-match flag that double up).
- The index refusing connections outright instead of answering 429, so a
  fully blocked run reported the same as a quiet one.
- A parser accepting robots.txt as a company slug, so a crawl with no real
  content still produced confident, wrong output.
- A source genuinely exhausted: it already found everything it can.

Given the run history and probe results, propose ranked hypotheses. For each:
  theory      what is wrong, specifically
  evidence    which number or probe result supports it
  check       one concrete thing a human could run to confirm it
  confidence  low, medium or high

Distinguish breakage from exhaustion. A source whose yield has been declining
for weeks toward zero is probably exhausted; one that dropped from thousands
to zero overnight is probably broken. Say which you think it is."""


def history(conn, source: str, limit: int = 12) -> list[dict]:
    rows = conn.execute(
        """SELECT started_at, finished_at, refs_seen, new_boards, new_companies,
                  blocked, error, skipped
           FROM source_runs WHERE source=? ORDER BY id DESC LIMIT ?""",
        (source, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def probe(source: str) -> dict:
    """Ask the source what it thinks it can reach.

    Deterministic and cheap: availability, the hosts or endpoints it is
    configured with, and whether it counted itself blocked. This is evidence
    the model reasons over, not something it produces.
    """
    from .. import discovery

    out: dict = {"source": source}
    try:
        src = discovery.build(source)
    except Exception as exc:
        return {**out, "buildable": False, "why": f"{type(exc).__name__}: {exc}"}

    out["buildable"] = True
    try:
        ok, why = src.available()
        out["available"] = ok
        out["availability_note"] = why
    except Exception as exc:
        out["available"] = False
        out["availability_note"] = f"{type(exc).__name__}: {exc}"

    for attr in ("hosts", "DEFAULT_HOSTS", "crawls", "max_pages", "pause", "retries"):
        val = getattr(src, attr, None)
        if val is not None and not callable(val):
            out[attr] = list(val) if isinstance(val, tuple) else val
    out["blocked_counter"] = getattr(src, "blocked", None)
    return out


def run(conn, source: str, *, max_calls: int = 4) -> dict:
    """Diagnose one source and file the hypothesis. Writes no other table."""
    from .. import proposals as prop

    if not llm.available():
        return {"skipped": "no LLM provider configured", "source": source}

    trend = obs_runs.trend(conn, source)
    hist = history(conn, source)
    probed = probe(source)

    facts = {
        "source": source,
        "collapsed": trend.collapsed,
        "reason": trend.reason,
        "latest_new_boards": trend.latest,
        "median_new_boards": trend.median,
        "runs_considered": trend.runs,
        "recent_runs": hist,
        "probe": probed,
    }
    got = llm.complete(
        [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": json.dumps(facts, indent=1, default=str)},
        ],
        schema=DIAGNOSIS_SCHEMA,
        max_tokens=3072,
        max_calls=max_calls,
    )
    if got is None:
        return {"skipped": "no provider answered", "source": source}

    pid = prop.file(
        conn,
        "healer",
        "diagnosis",
        {"source": source, "most_likely": got["most_likely"]},
        {"hypotheses": got["hypotheses"], "trend": trend.reason, "probe": probed},
    )
    """
    Gated so it lands in `pending` like anything else. There is no gate
    registered for diagnosis, which is exactly the point: the strongest
    outcome available to a healer is a human reading it.
    """
    prop.gate(conn, pid)
    return {
        "source": source,
        "proposal": pid,
        "most_likely": got["most_likely"],
        "hypotheses": len(got["hypotheses"]),
        "llm_calls": llm.calls_made(),
    }
