"""Common Crawl host coverage and its failure behaviour.

Two bugs here were invisible rather than loud, which is why they lasted.

The source swept `jobs.ashbyhq.com` alone while finding more unique boards
than any other source -- it was reading a tenth of the surface it could see,
and nothing about the output said so.

And it swallowed every fetch failure with `continue`. The index refuses
connections outright when you query it too fast, so a run that was blocked
from start to finish reported exactly what a run that found nothing new
reports. That is the failure mode worth the most guarding against: a source
silently yielding zero.
"""

import pytest

from argus.core import http
from argus.core.models import FetchError
from argus.discovery import commoncrawl
from argus.discovery.commoncrawl import DEFAULT_HOSTS, CommonCrawlSource


def test_every_pollable_ats_is_swept():
    """A host missing here is a whole ATS the best source cannot see."""
    from argus import adapters

    swept = " ".join(DEFAULT_HOSTS)
    for ats in adapters.supported():
        needle = {
            "ashby": "ashbyhq",
            "greenhouse": "greenhouse",
            "lever": "lever",
            "workday": "myworkdayjobs",
            "smartrecruiters": "smartrecruiters",
            "breezy": "breezy",
            "recruitee": "recruitee",
            "bamboohr": "bamboohr",
        }[ats]
        assert needle in swept, f"{ats} is pollable but never swept"


def test_every_host_is_queried_as_a_domain():
    """The prefix form is a trap: `host/*` plus matchType=prefix returns zero.

    The wildcard and the flag double up, and zero pages reads exactly like a
    host with nothing on it. Measured against the live index, jobs.ashbyhq.com
    returns 2 pages as a domain match and 0 as a prefix match -- so a regression
    here silently loses an entire ATS.
    """
    src = CommonCrawlSource()
    for host in DEFAULT_HOSTS:
        q = src._query(host)
        assert q["matchType"] == "domain"
        assert q["url"] == host, "the bare host, never host/*"
        assert "*" not in q["url"]


def test_a_blocked_run_is_counted_not_swallowed(monkeypatch):
    """The index refuses connections rather than returning 429.

    Without the counter a fully blocked run is indistinguishable from a quiet
    one, and the operator has no way to tell 'nothing new' from 'never asked'.
    """

    def refuse(*a, **k):
        raise FetchError("Connection refused")

    monkeypatch.setattr(http, "get_json", refuse)
    monkeypatch.setattr(commoncrawl.time, "sleep", lambda *_: None)

    src = CommonCrawlSource(crawls=1, retries=2)
    assert list(src.discover()) == []
    assert src.blocked > 0, "a blocked run must say so"


def test_retries_back_off_before_giving_up(monkeypatch):
    attempts = []

    def flaky(*a, **k):
        attempts.append(1)
        raise FetchError("Connection refused")

    slept = []
    monkeypatch.setattr(http, "get_json", flaky)
    monkeypatch.setattr(commoncrawl.time, "sleep", lambda s: slept.append(s))

    src = CommonCrawlSource(retries=4, pause=1.0)
    assert src._get("http://x", {}) is None
    assert len(attempts) == 4
    assert slept == [1.0, 2.0, 4.0, 8.0], "each retry must wait longer than the last"


@pytest.mark.parametrize("crawls", [1, 4])
def test_crawl_count_is_respected(monkeypatch, crawls):
    monkeypatch.setattr(
        CommonCrawlSource,
        "_get",
        lambda self, u, p, lines=False: [{"id": f"c{i}"} for i in range(9)],
    )
    assert len(CommonCrawlSource(crawls=crawls)._recent_crawls()) == crawls


def test_wayback_sweeps_every_pollable_ats_too():
    """Wayback carried the same one-host default Common Crawl did. It sweeps a
    genuinely different corpus, so the boards it reaches are ones CC cannot --
    which makes the narrow default a pure loss."""
    from argus import adapters
    from argus.discovery.wayback import DEFAULT_HOSTS as WB

    swept = " ".join(h for h, _ in WB)
    for ats in adapters.supported():
        needle = {
            "ashby": "ashbyhq",
            "greenhouse": "greenhouse",
            "lever": "lever",
            "workday": "myworkdayjobs",
            "smartrecruiters": "smartrecruiters",
            "breezy": "breezy",
            "recruitee": "recruitee",
            "bamboohr": "bamboohr",
        }[ats]
        assert needle in swept, f"{ats} is pollable but wayback never sweeps it"


def test_wayback_uses_the_right_match_kind_per_host():
    """Unlike Common Crawl, the Wayback CDX needs prefix for path-based hosts
    and domain for subdomain-based ones. Both verified live; copying CC's
    single-form fix here would have been wrong."""
    from argus.discovery.wayback import DEFAULT_HOSTS as WB

    kinds = dict(WB)
    for h in ("myworkdayjobs.com", "breezy.hr", "recruitee.com", "bamboohr.com"):
        assert kinds[h] == "domain", h
    for h in ("jobs.ashbyhq.com", "jobs.lever.co", "jobs.smartrecruiters.com"):
        assert kinds[h] == "prefix", h
