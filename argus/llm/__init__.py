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
    Provider("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai/gpt-oss-20b"),
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


def _post(p: Provider, messages: list[dict], schema: dict, max_tokens: int) -> dict | None:
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
) -> dict | None:
    """One structured completion, or None.

    None is a first-class answer meaning "no provider could do this" -- not
    an exception, because the callers are pipeline stages for which a missing
    optional capability is a skip rather than a failure.
    """
    if max_calls is not None and _calls[0] >= max_calls:
        return None
    chain = providers if providers is not None else available()
    for p in chain:
        try:
            out = _post(p, messages, schema, max_tokens)
        except Exception:
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
            out = _post(p, messages, schema, max_tokens)
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
