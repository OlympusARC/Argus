"""The LLM layer and the agents on top of it.

No test here needs an API key. That is the point: every agent must be
importable, runnable and correct-in-its-refusal on a machine with no
provider, because that is what the degradation contract promises the rest of
the pipeline.
"""

import pytest

from argus import llm
from argus.agents import classifier, healer, prospector
from argus.core import db
from argus.proposals import PENDING, by_status, get


@pytest.fixture()
def conn(tmp_path):
    c = db.init_db(tmp_path / "t.db")
    for i, t in enumerate(
        ["Field Marketing Lead", "Account Executive", "Malware Analyst", "Payroll Specialist"]
    ):
        c.execute(
            """INSERT INTO jobs (ats, slug, external_id, title, role_family,
                                 first_seen_at, last_seen_at, status)
               VALUES ('ashby','acme',?,?, 'other',0,0,'open')""",
            (f"j{i}", t),
        )
    return c


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    """Every test runs as if no provider were configured, unless it says
    otherwise. Prevents a developer's exported key from making a test pass
    for the wrong reason -- or spending money in CI."""
    for p in llm.PROVIDERS:
        monkeypatch.delenv(p.key_env, raising=False)
    llm.reset_calls()


def fake_provider(monkeypatch, replies):
    """Install a provider that returns canned structured answers."""
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    seq = list(replies)

    def fake_post(p, messages, schema, max_tokens, effort=None):
        llm._calls[0] += 1
        return seq.pop(0) if seq else None

    monkeypatch.setattr(llm, "_post", fake_post)


"""
The degradation contract. If these fail, an outage at a free provider becomes
an outage in the pipeline.
"""


def test_no_provider_means_none_not_an_exception():
    assert llm.available() == []
    assert llm.complete([{"role": "user", "content": "x"}], schema={}) is None


def test_every_agent_skips_cleanly_without_a_provider(conn):
    assert classifier.run(conn)["skipped"]
    assert prospector.run(conn)["skipped"]
    assert healer.run(conn, "commoncrawl")["skipped"]


def test_the_feed_never_imports_the_llm_layer():
    """Structural, not aspirational: if reconcile or notify ever import llm,
    a provider outage acquires the ability to delay a job posting."""
    import pathlib

    root = pathlib.Path(healer.__file__).resolve().parents[1]
    for name in ("feed/reconcile.py", "feed/notify.py", "feed/diff.py", "feed/jobs.py"):
        src = (root / name).read_text()
        assert "import llm" not in src and "from ..llm" not in src, name
        """
        Same argument for langgraph: the feed lane must run on a machine that
        installed neither optional extra, or "a stuck brain can never delay
        the feed" stops being true at import time.
        """
        assert "langgraph" not in src, name


def test_a_malformed_reply_is_retried_once_then_dropped(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    calls = []

    def always_bad(p, messages, schema, max_tokens, effort=None):
        calls.append(1)
        llm._calls[0] += 1
        return None

    monkeypatch.setattr(llm, "_post", always_bad)
    assert llm.complete([{"role": "user", "content": "x"}], schema={}) is None
    assert len(calls) == 2, "one retry, not a storm"


def test_failover_moves_to_the_next_provider(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    seen = []

    def flaky(p, messages, schema, max_tokens, effort=None):
        seen.append(p.name)
        if p.name == "groq":
            raise ConnectionError("groq down")
        return {"ok": True}

    monkeypatch.setattr(llm, "_post", flaky)
    assert llm.complete([{"role": "user", "content": "x"}], schema={}) == {"ok": True}
    assert seen == ["groq", "nim"]


def test_the_call_cap_is_enforced(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setattr(llm, "_post", lambda *a: {"ok": True})
    llm._calls[0] = 5
    assert llm.complete([{"role": "user", "content": "x"}], schema={}, max_calls=5) is None


"""
The classifier.
"""


def test_mining_files_proposals_and_never_writes_jobs(conn, monkeypatch):
    fake_provider(
        monkeypatch,
        [
            {
                "labels": [
                    {"title": "Malware Analyst", "family": "security", "software": True},
                    {
                        "title": "Threat Detection Engineer",
                        "family": "security",
                        "software": True,
                    },
                    {"title": "SOC Analyst", "family": "security", "software": True},
                    {"title": "Incident Responder", "family": "security", "software": True},
                    {"title": "Security Engineer II", "family": "security", "software": True},
                ]
            },
            {
                "patterns": [
                    {
                        "pattern": "malware",
                        "family": "security",
                        "rationale": "catches malware roles",
                    }
                ]
            },
        ],
    )
    before = conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
    res = classifier.run(conn, sample=10)
    assert res["filed"] == 1
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == before
    assert conn.execute("SELECT COUNT(*) n FROM proposals").fetchone()["n"] == 1


def test_the_tail_sample_is_shuffled_not_taken_in_order(conn):
    """The tail is dominated by whichever enterprise board polled last. 2,000
    consecutive Daikin titles would teach the miner about HVAC and nothing
    else."""
    for i in range(50):
        conn.execute(
            """INSERT INTO jobs (ats, slug, external_id, title, role_family,
                                 first_seen_at, last_seen_at, status)
               VALUES ('workday','daikin',?,?, 'other',0,0,'open')""",
            (f"d{i}", f"Manufacturing Engineer {i}"),
        )
    a = classifier.tail_titles(conn, limit=10, seed=1)
    b = classifier.tail_titles(conn, limit=10, seed=2)
    assert a != b, "different seeds must give different samples"
    assert classifier.tail_titles(conn, limit=10, seed=1) == a, "same seed is repeatable"


"""
The prospector. The decisive step is measurement, so that is what is tested.
"""


def test_a_candidate_with_no_new_boards_is_never_filed(conn, monkeypatch):
    fake_provider(
        monkeypatch,
        [{"candidates": [{"url": "https://example.com/empty", "why": "looks good"}]}],
    )
    monkeypatch.setattr("argus.core.http.get_text", lambda *a, **k: "<html>nothing</html>")
    res = prospector.run(conn, rounds=1)
    assert res["tried"] == 1 and res["filed"] == 0, (
        "a beautiful rationale with zero yield is still zero"
    )


def test_a_productive_candidate_is_filed_and_gated(conn, monkeypatch):
    html = " ".join(f'<a href="https://jobs.ashbyhq.com/c{i}">x</a>' for i in range(30))
    fake_provider(
        monkeypatch, [{"candidates": [{"url": "https://example.com/list", "why": "a list"}]}]
    )
    monkeypatch.setattr("argus.core.http.get_text", lambda *a, **k: html)
    res = prospector.run(conn, rounds=1)
    assert res["filed"] == 1 and res["best"] == 30
    assert conn.execute("SELECT COUNT(*) n FROM boards").fetchone()["n"] == 30


def test_registry_yield_counts_only_boards_we_lack(conn, monkeypatch):
    conn.execute(
        """INSERT INTO boards (ats, slug, status, tier, first_seen_at)
           VALUES ('ashby','known','active',1,0)"""
    )
    html = (
        '<a href="https://jobs.ashbyhq.com/known">a</a>'
        '<a href="https://jobs.ashbyhq.com/fresh">b</a>'
    )
    monkeypatch.setattr("argus.core.http.get_text", lambda *a, **k: html)
    score = prospector.registry_yield(conn, "https://example.com")
    assert score["found"] == 2 and score["new"] == 1


def test_an_unfetchable_candidate_is_scored_zero_not_raised(conn, monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("dns")

    monkeypatch.setattr("argus.core.http.get_text", boom)
    assert prospector.registry_yield(conn, "https://nope.invalid")["new"] == 0


"""
The healer. Its output must remain unable to change anything.
"""


def test_a_diagnosis_is_filed_as_pending_and_nothing_else(conn, monkeypatch):
    from argus.discovery import SourceResult
    from argus.obs import runs as obs_runs

    for _ in range(4):
        rid = obs_runs.start(conn, "commoncrawl")
        obs_runs.finish(
            conn, rid, SourceResult("commoncrawl", refs_seen=19000, new_boards=13000)
        )
    rid = obs_runs.start(conn, "commoncrawl")
    obs_runs.finish(conn, rid, SourceResult("commoncrawl", refs_seen=100, new_boards=0))

    fake_provider(
        monkeypatch,
        [
            {
                "hypotheses": [
                    {
                        "theory": "host list defaulted to one entry",
                        "evidence": "refs fell from 19000 to 100",
                        "check": "print DEFAULT_HOSTS",
                        "confidence": "high",
                    }
                ],
                "most_likely": "host list defaulted to one entry",
            }
        ],
    )
    before = conn.execute("SELECT COUNT(*) n FROM boards").fetchone()["n"]
    res = healer.run(conn, "commoncrawl")
    assert res["proposal"]
    assert get(conn, res["proposal"])["status"] == PENDING
    assert conn.execute("SELECT COUNT(*) n FROM boards").fetchone()["n"] == before
    assert by_status(conn, PENDING)


def test_the_probe_is_deterministic_and_needs_no_model(conn):
    """Evidence the model reasons over, not evidence it produces."""
    out = healer.probe("commoncrawl")
    assert out["buildable"] and "hosts" in out
    """
    The number this guards is "more than one". Common Crawl swept a single
    host for months while every run looked normal, and the fix widened it to
    ten. It is nine now because workable was removed -- Cloudflare rate-limits
    it to a standstill -- so the bound is deliberately loose: it should fail
    if the sweep narrows again, not every time the host list is edited.
    """
    assert len(out["hosts"]) >= 8, "the real host list, post-fix"


"""
The token budget. Found live: the classifier asked for a flat max_tokens=8192
and Groq's free tier allows 8,000 tokens per minute, so every batch was
rejected with a 413 and then silently skipped. The agent reported an empty
tail rather than a broken provider.
"""


def test_a_request_is_costed_including_the_tokens_it_reserves():
    """max_tokens is charged as requested, not as used. Costing only the
    prompt is what made an over-generous ask look free."""
    msgs = [{"role": "user", "content": "x" * 400}]
    assert llm.request_cost(msgs, 0) == pytest.approx(101, abs=2)
    assert llm.request_cost(msgs, 8192) > 8192


def test_a_provider_that_cannot_serve_the_request_is_skipped_not_tried():
    """Trying anyway costs a call to learn what arithmetic already knew, and
    on a per-minute budget the failed call is charged too."""
    small = llm.Provider("tiny", "https://example.invalid/v1", "PATH", "m", tpm=1_000)
    out = llm.complete(
        [{"role": "user", "content": "hello"}],
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        max_tokens=8192,
        providers=[small],
    )
    assert out is None
    assert any("tpm limit" in s for s in llm.last_skips())


def test_an_unknown_tpm_does_not_block_a_provider():
    """tpm=0 means unmeasured, not zero. Treating it as a limit would disable
    every provider whose ceiling we have not looked up."""
    p = llm.Provider("unknown", "https://example.invalid/v1", "PATH", "m")
    assert p.tpm == 0
    llm.complete(
        [{"role": "user", "content": "hello"}],
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        max_tokens=8192,
        providers=[p],
    )
    assert not any("tpm limit" in s for s in llm.last_skips()), "attempted, not skipped"


def test_the_reply_budget_scales_with_the_batch():
    """Each label echoes its title back, so a batch of long titles needs a
    bigger answer than a batch of short ones."""
    short = classifier.reply_tokens(["QA"] * 50)
    long = classifier.reply_tokens(
        ["Senior Staff Software Engineer, Platform Infrastructure"] * 50
    )
    assert short < long
    assert long < 8_000, "a full batch must fit the smallest free per-minute budget"


def test_a_skipped_batch_is_reported_rather_than_dropped(monkeypatch):
    """The bug this block exists for: label() returned [] and the caller read
    it as an empty tail."""
    monkeypatch.setattr(llm, "available", lambda: [])
    labels, failures = classifier.label(["Software Engineer"] * 120)
    assert labels == []
    assert len(failures) == 3, "one per batch, not one for the run"


def test_reasoning_effort_is_sent_only_where_it_is_understood(monkeypatch):
    """gpt-oss bills its thinking as completion tokens -- 142 a row at the
    default effort against 18 at "low", which decides whether a batch fits an
    8,000 TPM budget. But a provider that does not know the field rejects the
    whole request, so it must not be sent blindly."""
    seen = {}

    def capture(p, messages, schema, max_tokens, effort=None):
        seen[p.name] = effort
        return {"ok": True}

    monkeypatch.setattr(llm, "_post", capture)
    thinker = llm.Provider("thinks", "https://x.invalid/v1", "K", "m", reasoning=True)
    plain = llm.Provider("plain", "https://x.invalid/v1", "K", "m")

    llm.complete(
        [{"role": "user", "content": "x"}], schema={}, providers=[thinker], effort="low"
    )
    llm.complete([{"role": "user", "content": "x"}], schema={}, providers=[plain], effort="low")
    assert seen == {"thinks": "low", "plain": "low"}, "_post decides, not complete"


def test_the_classifier_asks_for_the_cheap_reasoning_mode(monkeypatch):
    """The setting that made a 43-minute job an 8-minute one."""
    monkeypatch.setenv("GROQ_API_KEY", "k")
    seen = []

    def capture(p, messages, schema, max_tokens, effort=None):
        seen.append((effort, max_tokens))
        llm._calls[0] += 1
        return {"labels": []}

    monkeypatch.setattr(llm, "_post", capture)
    classifier.label(["Software Engineer"] * 10)
    assert seen and seen[0][0] == "low"
    assert seen[0][1] < 8_000, "and the ask must fit the per-minute ceiling"
