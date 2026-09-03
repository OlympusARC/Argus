"""Field mapping for every ATS adapter, against recorded real payloads.

These are trimmed captures of live responses, not invented shapes. The point
is not that the parsing works today -- it is that when a vendor renames a
field, exactly one test fails and names the adapter.

Two properties matter more than any individual field:

  * external_id must come from the vendor's own stable identifier. It is a
    third of the primary key, so if it drifts between polls every posting on
    the board closes and reopens under a new id.
  * a posting with no id is skipped, never invented. A synthesised id would
    be stable only until the payload order changed.
"""

import pytest

from argus import adapters
from argus.core import http

PAYLOADS = {
    "smartrecruiters": {
        "totalFound": 1,
        "content": [
            {
                "id": "743999659865126",
                "name": "Application System Engineer",
                "releasedDate": "2017-09-15T00:33:29.000Z",
                "location": {
                    "city": "San Leandro",
                    "region": "CA",
                    "country": "us",
                    "remote": False,
                    "fullLocation": "San Leandro, CA, United States",
                },
                "department": {"label": "Engineering"},
                "typeOfEmployment": {"label": "Full-time"},
            }
        ],
    },
    "breezy": [
        {
            "id": "4136f1c6a49f",
            "name": "Chief Executive Officer (CEO)",
            "url": "https://25madison-llc.breezy.hr/p/4136f1c6a49f-ceo",
            "published_date": "2026-08-25T20:08:15.734Z",
            "type": {"id": "contract", "name": "Contract"},
            "location": {"name": "United States", "is_remote": False},
            "department": None,
        }
    ],
    "recruitee": {
        "offers": [
            {
                "id": 1527683,
                "slug": "project-manager-3",
                "title": "Project Manager",
                "location": "Reston, Virginia, United States",
                "careers_url": "https://aboutobjects.recruitee.com/o/project-manager-3",
                "careers_apply_url": "https://aboutobjects.recruitee.com/o/project-manager-3/c/new",
                "published_at": "2023-11-28 19:50:40 UTC",
                "department": "Engineering",
                "employment_type_code": "fulltime",
                "remote": False,
            }
        ]
    },
    "bamboohr": {
        "meta": {"totalCount": 1},
        "result": [
            {
                "id": "24",
                "jobOpeningName": "Entry -Mid Android Developer",
                "departmentLabel": "Engineering",
                "employmentStatusLabel": "Full-Time",
                "location": {"city": "Jersey City", "state": "New Jersey"},
                "isRemote": None,
            }
        ],
    },
}

EXPECTED = {
    "smartrecruiters": ("743999659865126", "Application System Engineer"),
    "breezy": ("4136f1c6a49f", "Chief Executive Officer (CEO)"),
    "recruitee": ("1527683", "Project Manager"),
    "bamboohr": ("24", "Entry -Mid Android Developer"),
}


@pytest.fixture()
def canned(monkeypatch):
    """Serve a recorded payload instead of hitting the network."""

    def serve(payload):
        monkeypatch.setattr(http, "get_json", lambda *a, **k: payload)

    return serve


@pytest.mark.parametrize("ats", sorted(PAYLOADS))
def test_adapter_maps_the_vendor_payload(ats, canned):
    canned(PAYLOADS[ats])
    postings = adapters.get(ats).fetch("acme")
    assert len(postings) == 1
    p = postings[0]
    external_id, title = EXPECTED[ats]
    assert (p.external_id, p.title) == (external_id, title)
    assert p.ats == ats
    assert p.slug == "acme"
    assert p.url.startswith("https://")
    assert p.location
    assert p.content_hash()


@pytest.mark.parametrize("ats", sorted(PAYLOADS))
def test_a_posting_with_no_id_is_skipped_not_invented(ats, canned):
    """A synthesised id is stable only until the payload order changes."""
    payload = PAYLOADS[ats]
    if isinstance(payload, list):
        stripped = [dict(payload[0])]
        rows = stripped
    else:
        key = next(k for k in ("content", "jobs", "offers", "result") if k in payload)
        stripped = {**payload, key: [dict(payload[key][0])]}
        rows = stripped[key]
    for field in ("id", "shortcode"):
        rows[0].pop(field, None)
    canned(stripped)
    assert adapters.get(ats).fetch("acme") == []


@pytest.mark.parametrize("ats", sorted(PAYLOADS))
def test_the_same_payload_hashes_the_same_twice(ats, canned):
    """content_hash drives edit detection; instability would flag every poll."""
    canned(PAYLOADS[ats])
    first = adapters.get(ats).fetch("acme")[0].content_hash()
    canned(PAYLOADS[ats])
    assert adapters.get(ats).fetch("acme")[0].content_hash() == first


def test_paginated_adapters_refuse_to_return_a_partial_board(canned):
    """The reconciler reads absence as a close, so half a board closes the rest."""
    from argus.core.models import FetchError

    canned({"totalFound": 999_999, "content": []})
    with pytest.raises(FetchError, match="partial board"):
        adapters.get("smartrecruiters").fetch("acme")


def test_every_registered_adapter_declares_its_ats():
    for name, adapter in adapters.ADAPTERS.items():
        assert adapter.ats == name
    assert not set(adapters.supported()) & set(adapters.PLANNED)


"""
Rate limiting that is really a refusal.
"""


def test_retry_after_is_capped():
    """Workable sits behind Cloudflare, which answers a rate-limited client
    with Retry-After: 39481 -- eleven hours. urllib3 obeys it literally, and
    it holds the per-host slot while it sleeps, so one request took a
    twelve-thread poll to zero boards in two hours."""

    class FakeResponse:
        headers = {"Retry-After": "39481"}

    capped = http.CappedRetry(total=3, respect_retry_after_header=True)
    assert capped.get_retry_after(FakeResponse()) == http.RETRY_AFTER_MAX


def test_a_reasonable_retry_after_is_still_honoured():
    """The cap is a ceiling, not a replacement. A server asking for a few
    seconds is being cooperative and should be believed."""

    class FakeResponse:
        headers = {"Retry-After": "5"}

    capped = http.CappedRetry(total=3, respect_retry_after_header=True)
    assert capped.get_retry_after(FakeResponse()) == 5


def test_no_header_stays_none():
    class FakeResponse:
        headers: dict = {}

    assert http.CappedRetry(total=3).get_retry_after(FakeResponse()) is None
