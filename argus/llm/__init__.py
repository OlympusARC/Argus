"""Structured completions from whichever free provider is answering.

Every provider here speaks the OpenAI-compatible shape, so this is a thin
wrapper over `requests` -- which Argus already depends on -- rather than an
SDK. Failover is a base-URL swap, which matters because free tiers disappear
and the model weights we care about are open and served in several places.

Three rules the rest of the system relies on.

Structured or nothing. Every call carries a JSON schema and a response that
does not validate is retried once and then dropped. A model's prose is never
parsed hopefully into a dict that later becomes a database row.

Never in the money path. If every provider is missing, exhausted or broken,
complete() returns None and the caller records that it was skipped. Poll,
reconcile and the digest do not import this module at all, so a dead provider
cannot delay a job posting.

Budgeted by the caller. There is no internal retry storm: one attempt per
provider, in order, then None.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from ..core import http


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    key_env: str
    model: str

    """
    Free-tier tokens per minute, or 0 when it is not known. This is the limit
    that actually binds, and it is not the one the marketing page leads with:
    Groq advertises 1,000 requests a day and enforces 8,000 tokens a minute,
    so a single request asking for max_tokens=8192 is rejected outright while
    997 of the day's requests remain. max_tokens counts against the budget as
    *requested*, not as used, so an over-generous ask costs the same as a
    long answer.
    """
    tpm: int = 0

    """
    Whether the model accepts `reasoning_effort`. gpt-oss thinks before it
    answers and bills the thinking as completion tokens: labelling fifty job
    titles cost 142 tokens a row at the default effort and 18 at "low", which
    is the difference between the classifier fitting an 8,000 TPM budget and
    not fitting it at all. A provider that does not know the field would
    reject the request, so it is opt-in per provider rather than always sent.
    """
    reasoning: bool = False

    @property
    def key(self) -> str | None:
        return os.getenv(self.key_env)

    @property
    def ready(self) -> bool:
        return bool(self.key)


"""
Ordered by what the measured workload needs: title classification is high
volume and trivially easy, so the fastest free tier goes first. Gemini is
last because its value is long context, which only the extractor needs.

Nemotron is second on capability rather than politeness -- it is tuned for
structured output and instruction following, which is exactly this workload
-- but note NVIDIA's terms cover evaluation rather than production, and the
weights are open, so if that ever bites the model survives a provider swap.
"""
PROVIDERS: list[Provider] = [
    Provider(
        "groq",
        "https://api.groq.com/openai/v1",
        "GROQ_API_KEY",
        "openai/gpt-oss-20b",
        tpm=8_000,
        reasoning=True,
    ),
    Provider(
        "nim",
        "https://integrate.api.nvidia.com/v1",
        "NVIDIA_API_KEY",
        "nvidia/nemotron-3-nano",
    ),
    Provider(
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "GEMINI_API_KEY",
        "gemini-flash-lite-latest",
    ),
]

TIMEOUT = float(os.getenv("ARGUS_LLM_TIMEOUT", "120"))

"""
How long to wait out a 429 before the single retry. Groq's token window
resets inside twenty seconds and says so in Retry-After; this is the same
order without parsing a header the transport does not surface.
"""
RATE_LIMIT_WAIT = float(os.getenv("ARGUS_LLM_RATE_WAIT", "20"))

"""
Counted for the orchestrator's budget. A node reads this to decide whether it
has spent its share, which is why it is module state rather than a return
value threaded through every helper.
"""
_calls = [0]


def calls_made() -> int:
    return _calls[0]


def reset_calls() -> None:
    _calls[0] = 0


def available() -> list[Provider]:
    return [p for p in PROVIDERS if p.ready]


"""
Why the last call to complete() came back None. A provider that cannot serve
a request is a fact worth reporting -- the alternative is what this module
did before: skip every batch and return an empty list that reads as "the
world is empty" rather than "nothing was asked properly".
"""
_skips: list[str] = []


def last_skips() -> list[str]:
    return list(_skips)


def estimate_tokens(text: str) -> int:
    """Four characters to a token, rounded up. Deliberately crude.

    It only has to be good enough to keep a request under a provider's
    per-minute ceiling, and the ceiling has enough slack that being 20% out
    changes nothing. Anything better would need the model's own tokenizer,
    which is a dependency this module exists to avoid.
    """
    return len(text) // 4 + 1


def request_cost(messages: list[dict], max_tokens: int) -> int:
    """What a request spends against a per-minute token budget.

    max_tokens is included because providers reserve it up front. This is the
    arithmetic that makes an 8,192-token ask fail against an 8,000 limit no
    matter how short the prompt is.
    """
    return sum(estimate_tokens(str(m.get("content", ""))) for m in messages) + max_tokens


def _post(
    p: Provider,
    messages: list[dict],
    schema: dict,
    max_tokens: int,
    effort: str | None = None,
) -> dict | None:
    body = {
        "model": p.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "out", "strict": True, "schema": schema},
        },
    }
    if effort and p.reasoning:
        body["reasoning_effort"] = effort
    _calls[0] += 1
    try:
        raw = http.post_json(
            f"{p.base_url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {p.key}"},
            timeout=TIMEOUT,
        )
    except http.FetchError as exc:
        """
        The shared session retries 429 for GET and HEAD only, because a
        retried POST is not generally safe. A completion is the exception --
        it writes nothing -- and a token-per-minute window is short enough to
        outwait. One wait, once: the caller owns the budget, and a second
        provider is usually cheaper than a second sleep.
        """
        if getattr(exc, "status", None) != 429:
            raise
        time.sleep(RATE_LIMIT_WAIT)
        _calls[0] += 1
        raw = http.post_json(
            f"{p.base_url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {p.key}"},
            timeout=TIMEOUT,
        )
    try:
        text = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def complete(
    messages: list[dict],
    *,
    schema: dict,
    max_tokens: int = 4096,
    max_calls: int | None = None,
    providers: list[Provider] | None = None,
    effort: str | None = None,
) -> dict | None:
    """One structured completion, or None.

    None is a first-class answer meaning "no provider could do this" -- not
    an exception, because the callers are pipeline stages for which a missing
    optional capability is a skip rather than a failure.
    """
    if max_calls is not None and _calls[0] >= max_calls:
        return None
    _skips.clear()
    chain = providers if providers is not None else available()
    cost = request_cost(messages, max_tokens)
    for p in chain:
        """
        Ask only where the ask can be served. A provider whose per-minute
        budget is smaller than this one request will reject it every time,
        and burning a call to discover that on each batch is how a run
        produces nothing while reporting no error.
        """
        if p.tpm and cost > p.tpm:
            _skips.append(f"{p.name}: request needs ~{cost:,} tokens, tpm limit is {p.tpm:,}")
            continue
        try:
            out = _post(p, messages, schema, max_tokens, effort)
        except Exception as exc:
            _skips.append(f"{p.name}: {type(exc).__name__}: {exc}")
            """
            Any failure -- rate limit, outage, malformed reply -- moves to the
            next provider. One attempt each, no retry storm: the caller owns
            the budget.
            """
            continue
        if out is not None:
            return out
        """
        A reply that arrived but did not validate gets exactly one more try
        on the same provider; models occasionally emit a stray prose preamble
        and then behave when asked again.
        """
        try:
            out = _post(p, messages, schema, max_tokens, effort)
        except Exception:
            continue
        if out is not None:
            return out
    return None


def describe() -> str:
    """What `argus llm` prints: which providers are configured, in order."""
    lines = []
    for p in PROVIDERS:
        mark = "ready" if p.ready else f"no {p.key_env}"
        lines.append(f"  {p.name:<8} {p.model:<28} {mark}")
    if not available():
        lines.append("\n  no provider configured -- LLM-backed tasks will be skipped")
    return "\n".join(lines)


def health() -> tuple[bool, str]:
    """A live round trip against the first ready provider."""
    chain = available()
    if not chain:
        return False, "no provider configured"
    t0 = time.time()
    out = complete(
        [{"role": "user", "content": 'Reply with {"ok": true}.'}],
        schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        max_tokens=64,
    )
    if out is None:
        return False, f"{chain[0].name} did not answer with valid JSON"
    return True, f"{chain[0].name} ok in {time.time() - t0:.1f}s"
