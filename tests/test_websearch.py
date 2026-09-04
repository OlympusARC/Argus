"""The pluggable search backend, and the Monid response walk.

No network: every test drives _search through a stubbed transport, because
what is under test is which backend gets chosen and how a response is read,
not whether a search engine works.
"""

import pytest

from argus.core.models import FetchError
from argus.discovery import websearch
from argus.discovery.websearch import WebSearchSource, _monid_results


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch):
    """A developer's own key must not decide which backend a test exercises."""
    for k in ("MONID_API_KEY", "BRAVE_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_monid_is_preferred_over_brave(monkeypatch):
    """Both keys set is the interesting case: tinyfish is metered at $0 and
    Brave's free tier is a quota, so the cheaper one has to win."""
    monkeypatch.setenv("MONID_API_KEY", "k")
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    src = WebSearchSource()
    assert src.available() == (True, "")
    assert src.backend == "monid"


def test_brave_is_used_when_only_brave_is_configured(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    src = WebSearchSource()
    assert src.available()[0] and src.backend == "brave"


def test_no_key_reports_unavailable_and_names_both_options():
    ok, why = WebSearchSource().available()
    assert not ok
    assert "MONID_API_KEY" in why and "BRAVE_API_KEY" in why


"""
The response walk. Monid brokers a provider's own payload, so the nesting is
the provider's business -- these pin the behaviour, not one shape.
"""


def test_results_are_found_however_deeply_the_broker_wraps_them():
    shallow = {"results": [{"url": "https://a.test"}, {"url": "https://b.test"}]}
    deep = {"data": {"output": {"body": {"results": [{"url": "https://a.test"}]}}}}
    assert _monid_results(shallow) == ["https://a.test", "https://b.test"]
    assert _monid_results(deep) == ["https://a.test"]


def test_rank_order_survives_the_walk():
    """TinyFish returns results ranked, and a depth-first walk reverses them.
    Order is the one thing a search result carries beyond its URL."""
    payload = {"results": [{"position": i, "url": f"https://{i}.test"} for i in range(5)]}
    assert _monid_results(payload) == [f"https://{i}.test" for i in range(5)]


def test_duplicates_collapse_keeping_the_first():
    payload = {"results": [{"url": "https://a.test"}, {"url": "https://a.test"}]}
    assert _monid_results(payload) == ["https://a.test"]


def test_non_url_strings_are_not_mistaken_for_results():
    """`url` appears on things that are not results -- a logo, a next-page
    link, the provider's own docs. Only absolute http(s) counts."""
    payload = {"icon": {"url": "/static/logo.png"}, "results": [{"url": "https://a.test"}]}
    assert _monid_results(payload) == ["https://a.test"]


def test_an_empty_response_is_empty_not_an_error():
    assert _monid_results({}) == []
    assert _monid_results({"results": []}) == []


"""
Paging.
"""


def _stub(monkeypatch, pages):
    """Serve one canned payload per page, recording what was asked for."""
    calls = []

    def fake_post(url, *, json=None, **kw):
        calls.append(json)
        page = json["input"]["queryParams"]["page"]
        if page >= len(pages):
            return {"results": []}
        if isinstance(pages[page], Exception):
            raise pages[page]
        return {"results": [{"url": u} for u in pages[page]]}

    monkeypatch.setattr(websearch.http, "post_json", fake_post)
    return calls


def test_paging_accumulates_until_per_query_is_met(monkeypatch):
    calls = _stub(monkeypatch, [["https://a.test"], ["https://b.test"], ["https://c.test"]])
    monkeypatch.setenv("MONID_API_KEY", "k")
    src = WebSearchSource(per_query=3, pages=5)
    src.available()
    assert src._search("q") == ["https://a.test", "https://b.test", "https://c.test"]
    assert [c["input"]["queryParams"]["page"] for c in calls] == [0, 1, 2]


def test_an_empty_page_stops_paging_rather_than_burning_the_rest(monkeypatch):
    calls = _stub(monkeypatch, [["https://a.test"], []])
    monkeypatch.setenv("MONID_API_KEY", "k")
    src = WebSearchSource(per_query=50, pages=10)
    src.available()
    assert src._search("q") == ["https://a.test"]
    assert len(calls) == 2, "stopped at the first empty page, not page 10"


def test_a_failed_page_keeps_what_earlier_pages_returned(monkeypatch):
    """A broker 500 on page 2 must not discard page 1. The source's job is to
    yield what it found, not to be transactional."""
    _stub(monkeypatch, [["https://a.test"], FetchError("HTTP 500")])
    monkeypatch.setenv("MONID_API_KEY", "k")
    src = WebSearchSource(per_query=50, pages=5)
    src.available()
    assert src._search("q") == ["https://a.test"]


def test_per_query_is_a_ceiling(monkeypatch):
    _stub(monkeypatch, [[f"https://{i}.test" for i in range(10)]])
    monkeypatch.setenv("MONID_API_KEY", "k")
    src = WebSearchSource(per_query=4, pages=3)
    src.available()
    assert len(src._search("q")) == 4


def test_the_request_names_the_provider_and_endpoint(monkeypatch):
    calls = _stub(monkeypatch, [["https://a.test"]])
    monkeypatch.setenv("MONID_API_KEY", "k")
    src = WebSearchSource(per_query=1, pages=1)
    src.available()
    src._search("some query")
    body = calls[0]
    assert body["provider"] == "tinyfish" and body["endpoint"] == "/search"
    assert body["input"]["queryParams"]["query"] == "some query"


def test_include_domains_is_sent_only_when_asked_for(monkeypatch):
    """The keyword backends cannot use it -- a board is an empty shell to
    them -- so it must not leak into a plan that did not ask for it."""
    calls = _stub(monkeypatch, [["https://a.test"], ["https://b.test"]])
    monkeypatch.setenv("MONID_API_KEY", "k")
    src = WebSearchSource(per_query=1, pages=1)
    src.available()
    src._search("q")
    assert "include_domains" not in calls[0]["input"]["queryParams"]
    src._search("q", "jobs.lever.co")
    assert calls[1]["input"]["queryParams"]["include_domains"] == "jobs.lever.co"


"""
The query plan. It is per-backend because the backends see different webs:
TinyFish renders a page before indexing it and so can be pointed straight at
an ATS host; a keyword engine sees the same board as an empty shell.
"""


def test_monid_sweeps_every_ats_host(monkeypatch):
    monkeypatch.setenv("MONID_API_KEY", "k")
    src = WebSearchSource()
    src.available()
    plan = src._plan()
    assert {d for _, d in plan} == set(websearch.SWEEP_HOSTS)
    assert len(plan) == len(websearch.SWEEP_HOSTS) * len(websearch.SWEEP_PHRASINGS)


def test_bamboohr_is_not_swept(monkeypatch):
    """Its boards are <slug>.bamboohr.com, but the apex domain returns only
    BambooHR's own careers page -- the vendor, not its customers."""
    assert not any("bamboohr" in h for h in websearch.SWEEP_HOSTS)


def test_keyword_backends_get_the_mention_queries_with_no_host_filter(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    src = WebSearchSource()
    src.available()
    plan = src._plan()
    assert all(d is None for _, d in plan)
    assert [q for q, _ in plan] == list(websearch.QUERIES)


"""
discover().
"""


def test_a_result_that_is_itself_a_board_is_not_fetched(monkeypatch):
    """The SPA's served HTML is the empty shell. Fetching it costs a round
    trip and can only ever return what the URL already said."""
    monkeypatch.setenv("MONID_API_KEY", "k")
    fetched = []
    monkeypatch.setattr(websearch.http, "get_text", lambda u, **kw: fetched.append(u) or "")
    src = WebSearchSource(pages=1)
    src.available()
    monkeypatch.setattr(src, "_plan", lambda: [("q", "jobs.lever.co")])
    monkeypatch.setattr(src, "_search", lambda q, d=None: ["https://jobs.lever.co/acme"])
    monkeypatch.setattr(websearch.time, "sleep", lambda _s: None)
    refs = list(src.discover())
    assert [(r.ats, r.slug) for r in refs] == [("lever", "acme")]
    assert fetched == [], "a board result must not be fetched"


def test_a_result_that_merely_mentions_a_board_is_fetched(monkeypatch):
    monkeypatch.setenv("MONID_API_KEY", "k")
    monkeypatch.setattr(
        websearch.http,
        "get_text",
        lambda u, **kw: '<a href="https://jobs.lever.co/acme">jobs</a>',
    )
    src = WebSearchSource(pages=1)
    src.available()
    monkeypatch.setattr(src, "_plan", lambda: [("q", None)])
    monkeypatch.setattr(src, "_search", lambda q, d=None: ["https://someblog.test/hiring"])
    monkeypatch.setattr(websearch.time, "sleep", lambda _s: None)
    refs = list(src.discover())
    assert [(r.ats, r.slug) for r in refs] == [("lever", "acme")]
