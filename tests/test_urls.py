"""Router tests use URLs observed in real data, not invented ones."""

from argus.core.urls import extract_all, parse

CASES = [
    # (url, expected ats, expected slug, expected external_id)
    (
        "https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245",
        "ashby",
        "ramp",
        "34413f8d-26bf-4bbc-8ade-eb309a0e2245",
    ),
    (
        "https://jobs.ashbyhq.com/mechanize/1ef28bb2-6251-4da6-a590-a4a7606368cb/application",
        "ashby",
        "mechanize",
        "1ef28bb2-6251-4da6-a590-a4a7606368cb",
    ),
    # observed in Common Crawl: tracking params must not leak into the slug
    (
        "https://jobs.ashbyhq.com/0g/d35c9785-1912-4c23-8d09-dbbe353d4733?utm_source=Longhash+job+board",
        "ashby",
        "0g",
        "d35c9785-1912-4c23-8d09-dbbe353d4733",
    ),
    ("https://jobs.ashbyhq.com/Ramp", "ashby", "ramp", None),  # case-insensitive
    ("https://jobs.ashbyhq.com/zip/", "ashby", "zip", None),
    (
        "https://job-boards.greenhouse.io/trueanomalyinc/jobs/4501234",
        "greenhouse",
        "trueanomalyinc",
        "4501234",
    ),
    ("https://boards.greenhouse.io/embed/job_board?for=stripe", "greenhouse", "stripe", None),
    (
        "https://jobs.lever.co/palantir/ac978161-6f46-4f6b-ad9e-a258e642751c",
        "lever",
        "palantir",
        "ac978161-6f46-4f6b-ad9e-a258e642751c",
    ),
    (
        "https://jobs.lever.co/palantir/ac978161-6f46-4f6b-ad9e-a258e642751c/apply",
        "lever",
        "palantir",
        "ac978161-6f46-4f6b-ad9e-a258e642751c",
    ),
    (
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x",
        "workday",
        "nvidia.wd5/nvidiaexternalcareersite",
        None,
    ),
    (
        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs",
        "workday",
        "nvidia.wd5/nvidiaexternalcareersite",
        None,
    ),
    ("https://apply.workable.com/acmeco/j/ABC123/", "workable", "acmeco", "ABC123"),
    ("https://someco.recruitee.com/o/engineer", "recruitee", "someco", None),
]

REJECT = [
    "https://jobs.ashbyhq.com/",
    "https://jobs.ashbyhq.com/meeting/abc",  # robots-disallowed app route
    "https://jobs.ashbyhq.com/api/whatever",
    "https://example.com/careers",
    "https://stripe.com/jobs/search?gh_jid=7532733",  # no slug recoverable
    "https://www.recruitee.com/pricing",
    "not a url at all",
]


def test_parses_real_urls():
    for url, ats, slug, ext in CASES:
        got = parse(url)
        assert got is not None, f"failed to parse {url}"
        assert (got.ats, got.slug) == (ats, slug), f"{url} -> {got}"
        assert got.external_id == ext, f"{url} -> {got.external_id!r} != {ext!r}"


def test_rejects_non_boards():
    for url in REJECT:
        assert parse(url) is None, f"should not have parsed {url}"


def test_extract_from_text_dedupes():
    text = (
        "we use https://jobs.ashbyhq.com/ramp/abc and also "
        "<a href='https://jobs.ashbyhq.com/ramp'>x</a> plus "
        "https://jobs.lever.co/palantir)."
    )
    refs = extract_all(text)
    assert {(r.ats, r.slug) for r in refs} == {("ashby", "ramp"), ("lever", "palantir")}


def test_extracts_from_html_escaped_text():
    """HN serves comment bodies with &#x2F; for '/'; regressing this silently
    drops ~200 boards per page, so it is pinned."""
    hn = (
        'due to it `<a href="https:&#x2F;&#x2F;jobs.ashbyhq.com&#x2F;permitflow">'
        "link</a>` and &#x2F;&#x2F;jobs.lever.co&#x2F;palantir too"
    )
    refs = extract_all(hn)
    assert ("ashby", "permitflow") in {(r.ats, r.slug) for r in refs}


def test_a_file_is_never_a_company():
    """Every ATS serves robots.txt off the path shape a board occupies.

    Found in a Common Crawl sweep where Lever's board pages were largely
    absent from the crawl: 62 of 62 records were robots.txt, and all 62
    parsed as a board named 'robots.txt'. Rejected in _clean_slug so one
    rule covers every ATS rather than eight separate guards.
    """
    for u in (
        "https://jobs.lever.co/robots.txt",
        "https://boards.greenhouse.io/sitemap.xml",
        "https://jobs.ashbyhq.com/favicon.ico",
        "https://apply.workable.com/app.js",
        "https://jobs.smartrecruiters.com/index.html",
    ):
        assert parse(u) is None, f"{u} is a file, not a board"


def test_boilerplate_pages_are_not_companies():
    for u in (
        "https://apply.workable.com/privacy",
        "https://jobs.lever.co/terms",
        "https://boards.greenhouse.io/login",
    ):
        assert parse(u) is None, f"{u} is boilerplate, not a board"


def test_real_slugs_still_parse():
    """The guard must not eat legitimate boards."""
    assert parse("https://jobs.lever.co/ramp") == ("lever", "ramp", None)
    assert parse("https://boards.greenhouse.io/stripe")[:2] == ("greenhouse", "stripe")
    assert parse("https://jobs.ashbyhq.com/openai")[:2] == ("ashby", "openai")
