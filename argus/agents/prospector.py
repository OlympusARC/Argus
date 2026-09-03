"""Find pages that enumerate job boards.

Fires on the orchestrator's rule 7, when the known sources stop finding much
and the answer is a new source rather than another sweep of the old ones.

The loop is short and the decisive step is not the model's. It suggests
candidate URLs; `registry_yield` fetches each one, extracts board references
with the same router the pipeline uses, and counts how many are new. That
number decides. A candidate with a beautiful rationale and zero new boards is
rejected on the evidence, which is what makes it safe to let an unreliable
proposer pick the candidates.

Deliberately not an open-ended tool-calling agent. The work is: propose URLs,
measure them, keep what pays. A ReAct loop would add turns, tokens and the
possibility of the model deciding to do something else, in exchange for
nothing this task needs.
"""

from __future__ import annotations

from .. import llm
from ..core import http, urls

CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["url", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

PROMPT = """You are finding web pages that link to many company job boards.

We crawl ATS job boards directly: Ashby (jobs.ashbyhq.com/<company>),
Greenhouse (boards.greenhouse.io/<company>, job-boards.greenhouse.io),
Lever (jobs.lever.co/<company>),
SmartRecruiters, Workday (<tenant>.myworkdayjobs.com), BambooHR, Breezy and
Recruitee.

No ATS publishes a list of its customers, so every board must be inferred
from third-party pages that happen to link to them. Good candidates are pages
carrying MANY such links at once:
- curated job-board or hiring lists on GitHub (raw file URLs work best)
- accelerator, incubator and VC portfolio pages
- "who is hiring" aggregations
- community job boards for a city, stack or industry

A page about one company is worthless -- we need pages with dozens.

Return specific, fetchable URLs. Prefer raw file URLs over rendered pages.
Do not return URLs you are unsure exist; a wrong guess costs a fetch and
teaches nothing."""

"""
The bar a candidate must clear to be worth proposing at all. Below this the
gate would reject it anyway, and filing it just adds noise to the review
queue.
"""
MIN_WORTH_FILING = 1


def registry_yield(conn, url: str) -> dict:
    """The deterministic gate, run inline so the agent sees the score too.

    Same extraction the pipeline uses, so a page that scores well here will
    behave the same way when a real source polls it.
    """
    try:
        html = http.get_text(url, probe=True, max_bytes=2_000_000)
    except Exception as exc:
        return {"url": url, "error": f"{type(exc).__name__}", "found": 0, "new": 0}

    refs = list(urls.extract_all(html))
    if not refs:
        return {"url": url, "found": 0, "new": 0}
    known = {(r["ats"], r["slug"]) for r in conn.execute("SELECT ats, slug FROM boards")}
    seen = {(r.ats, r.slug) for r in refs}
    fresh = seen - known
    return {
        "url": url,
        "found": len(seen),
        "new": len(fresh),
        "sample": sorted(f"{a}/{s}" for a, s in list(fresh)[:8]),
    }


def known_sources(conn) -> list[str]:
    """What already exists, so the model is not asked to reinvent it."""
    from .. import discovery

    return sorted(discovery.SOURCES)


def run(conn, *, rounds: int = 2, per_round: int = 6, max_calls: int = 6) -> dict:
    """Propose, measure, file. Returns what each candidate actually yielded.

    Rounds exist so the second attempt can see what the first measured: told
    that GitHub raw lists scored well and portfolio pages scored zero, the
    next set of candidates is meaningfully better.
    """
    from .. import proposals as prop

    llm.reset_calls()
    if not llm.available():
        return {"skipped": "no LLM provider configured", "filed": 0}

    existing = known_sources(conn)
    tried: list[dict] = []
    filed: list[int] = []

    for rnd in range(rounds):
        history = ""
        if tried:
            history = "\n\nAlready measured (do not repeat these):\n" + "\n".join(
                f"  {t['url']} -> {t['new']} new boards" for t in tried
            )
        got = llm.complete(
            [
                {"role": "system", "content": PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"We already have these sources: {', '.join(existing)}.\n"
                        f"Propose {per_round} NEW candidate URLs." + history
                    ),
                },
            ],
            schema=CANDIDATE_SCHEMA,
            max_tokens=2048,
            max_calls=max_calls,
        )
        if got is None:
            break

        for cand in got.get("candidates", []):
            url = cand.get("url", "")
            if not url or any(t["url"] == url for t in tried):
                continue
            score = registry_yield(conn, url)
            score["why"] = cand.get("why", "")
            score["round"] = rnd
            tried.append(score)
            if score["new"] >= MIN_WORTH_FILING:
                pid = prop.file(conn, "prospector", "source", {"url": url}, {"measured": score})
                """
                Gated immediately: the gate re-fetches and re-counts, so a
                page that changed between measuring and gating is caught
                rather than trusted.
                """
                prop.gate(conn, pid)
                filed.append(pid)

    return {
        "tried": len(tried),
        "filed": len(filed),
        "proposal_ids": filed,
        "best": max((t["new"] for t in tried), default=0),
        "candidates": tried,
        "llm_calls": llm.calls_made(),
    }
