"""Mine rules from the tail the ruleset cannot place.

The measured tail is 152,434 distinct titles that fired no rule, covering
240,679 postings. Labelling each one with a model is the obvious move and the
wrong one: it buys 152,434 rows of model opinion that cost money, expire the
next time RULESET changes, and explain nothing. Sampling the tail and mining
*regex* from it buys rules -- free forever after, reviewable, and applied by
the sweep that already exists.

So the agent's real output is a `ruleset_patch` proposal, and the gate scores
it on precision against already-labelled titles before anything is accepted.
The model proposes patterns; measurement decides.

Mode two exists for what r2 still cannot place, and writes
classified_by='llm:<model>' so those rows stay distinguishable from rule
output and sweepable when a later ruleset supersedes them.
"""

from __future__ import annotations

import random

from .. import llm

"""
Titles per request. Sized so a batch stays well inside a small model's
context while keeping the request count low: the whole sample is ~40 calls,
which fits any free tier's daily allowance several times over.
"""
BATCH = 50

FAMILIES = ("engineering", "fde", "data", "security", "product", "design", "other")

LABEL_SCHEMA = {
    "type": "object",
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "family": {"type": "string", "enum": list(FAMILIES)},
                    "software": {"type": "boolean"},
                },
                "required": ["title", "family", "software"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["labels"],
    "additionalProperties": False,
}

PATTERN_SCHEMA = {
    "type": "object",
    "properties": {
        "patterns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "family": {"type": "string", "enum": list(FAMILIES)},
                    "rationale": {"type": "string"},
                },
                "required": ["pattern", "family", "rationale"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["patterns"],
    "additionalProperties": False,
}

"""
`software` is a separate axis from `family` because of what the first real
digest showed: an HVAC manufacturer's welding, manufacturing and quality
engineers all matched is_engineering, and "Outside Sales Engineer" read as
forward-deployed. The corpus is full of genuine engineering that is not
software engineering, and one boolean is what distinguishes them.
"""
LABEL_PROMPT = """You are labelling job titles for a software-jobs index.

For each title return:
  family   one of: engineering, fde, data, security, product, design, other
  software true only if this is a SOFTWARE role -- writing, operating or
           designing software systems.

Critical distinctions, from real mistakes in this corpus:
- "Manufacturing Engineer", "Welding Engineer", "Quality Engineer" at an HVAC
  or industrial company are engineering but NOT software. family=engineering,
  software=false.
- "Outside Sales Engineer" and "Field Sales Engineer" at a hardware company
  are sales, not forward-deployed. family=other, software=false.
- "Forward Deployed Engineer", "Solutions Engineer", "Implementation
  Engineer" at a software company are fde, software=true.
- "Malware Analyst", "Threat Analyst", "Detection Engineer" are security.
- Recruiting, marketing, finance, sales, clinical and retail roles are other.

Return one entry per input title, with the title copied exactly."""

MINE_PROMPT = """You are proposing regular expressions for a job-title classifier.

Given titles already labelled, propose Python regex patterns that would
capture the labelled family, and that are specific enough not to catch
unrelated roles.

Rules:
- Lowercase, no anchors, no flags -- they are compiled with re.I.
- Prefer a word or short phrase over a long alternation.
- Propose at most 8 patterns; fewer good ones beats many loose ones.
- A pattern that would also match a common unrelated title is worse than no
  pattern: it silently mislabels, which is worse than leaving a row unlabelled.

rationale: one short sentence saying what it catches and what it avoids."""


def tail_titles(conn, limit: int = 2000, seed: int = 0) -> list[str]:
    """A sample of titles no rule placed.

    Sampled rather than taken in order: the tail is dominated by whichever
    enterprise Workday board was polled last, and a run of 2,000 consecutive
    Daikin titles would teach the miner about HVAC and nothing else.
    """
    rows = conn.execute(
        """SELECT DISTINCT title FROM jobs
           WHERE role_family IN ('other', 'unknown')
             AND title IS NOT NULL AND length(title) > 3"""
    ).fetchall()
    titles = [r["title"] for r in rows]
    rnd = random.Random(seed)
    rnd.shuffle(titles)
    return titles[:limit]


def label(titles: list[str], *, max_calls: int | None = None) -> list[dict]:
    """Label a sample in batches. Missing batches are skipped, not faked."""
    out: list[dict] = []
    for i in range(0, len(titles), BATCH):
        chunk = titles[i : i + BATCH]
        got = llm.complete(
            [
                {"role": "system", "content": LABEL_PROMPT},
                {"role": "user", "content": "\n".join(chunk)},
            ],
            schema=LABEL_SCHEMA,
            max_tokens=8192,
            max_calls=max_calls,
        )
        if got is None:
            continue
        out.extend(got.get("labels", []))
    return out


def mine(labelled: list[dict], *, max_calls: int | None = None) -> list[dict]:
    """Turn labels into candidate patterns, per family.

    Per family rather than all at once: a single call asked to cover seven
    families produces one weak pattern each, and the families we care about
    are worth their own attempt.
    """
    proposals: list[dict] = []
    by_family: dict[str, list[str]] = {}
    for row in labelled:
        if row.get("family") == "other":
            continue
        by_family.setdefault(row["family"], []).append(row["title"])

    for family, titles in sorted(by_family.items()):
        if len(titles) < 5:
            continue
        got = llm.complete(
            [
                {"role": "system", "content": MINE_PROMPT},
                {
                    "role": "user",
                    "content": f"family: {family}\n\n" + "\n".join(titles[:120]),
                },
            ],
            schema=PATTERN_SCHEMA,
            max_tokens=2048,
            max_calls=max_calls,
        )
        if got is None:
            continue
        for p in got.get("patterns", []):
            p["family"] = family
            proposals.append(p)
    return proposals


def run(conn, *, sample: int = 2000, max_calls: int = 80, seed: int = 0) -> dict:
    """Sample the tail, label it, mine patterns, file them as proposals.

    Files rather than applies. Every pattern goes through the same gate a
    human-written one would, and a pattern below the precision bar is held
    with its conflicts attached rather than silently improving the numbers.
    """
    from .. import proposals as prop

    llm.reset_calls()
    if not llm.available():
        return {"skipped": "no LLM provider configured", "filed": 0}

    titles = tail_titles(conn, limit=sample, seed=seed)
    if not titles:
        return {"skipped": "no unplaced titles", "filed": 0}

    labelled = label(titles, max_calls=max_calls)
    if not labelled:
        return {"skipped": "no provider answered", "filed": 0, "sampled": len(titles)}

    patterns = mine(labelled, max_calls=max_calls)
    filed = []
    for p in patterns:
        pid = prop.file(
            conn,
            "classifier",
            "ruleset_patch",
            {"pattern": p["pattern"], "family": p["family"]},
            {
                "rationale": p.get("rationale", ""),
                "sampled_titles": len(titles),
                "labelled": len(labelled),
            },
        )
        prop.gate(conn, pid)
        filed.append(pid)

    """
    Software-vs-engineering is the distinction the digest most needs and the
    one r1 gets wrong, so it is reported separately rather than buried in the
    family counts.
    """
    non_software_eng = sum(
        1 for x in labelled if x.get("family") == "engineering" and not x.get("software")
    )
    return {
        "sampled": len(titles),
        "labelled": len(labelled),
        "patterns": len(patterns),
        "filed": len(filed),
        "proposal_ids": filed,
        "llm_calls": llm.calls_made(),
        "non_software_engineering": non_software_eng,
    }
