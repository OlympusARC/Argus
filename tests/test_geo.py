"""Where a job is, from a field nobody agreed the format of.

The same city arrives as "San Francisco", "San Francisco, CA",
"San Francisco, CA, United States" and "US, CA, Santa Clara", and 9.6% of the
corpus leaves the field empty. So these tests are mostly about what the
module refuses to guess.
"""

from argus.classify import geo
from argus.core import config


def test_a_country_places_a_posting():
    assert geo.region("Austin, TX, United States") == geo.US
    assert geo.region("London, England, United Kingdom") == geo.EUROPE
    assert geo.region("Bangalore, India") == geo.OTHER


def test_a_bare_city_places_a_posting():
    """64,137 postings name one component, and it is rarely a country."""
    assert geo.region("San Francisco") == geo.US
    assert geo.region("London") == geo.EUROPE
    assert geo.region("Munich") == geo.EUROPE
    assert geo.region("Bengaluru") == geo.OTHER


def test_the_non_target_list_runs_before_the_city_lists():
    """Order is what makes the gazetteers safe. Without it "Birmingham" is a
    coin toss between Alabama and England."""
    assert geo.region("Birmingham, UK") == geo.EUROPE
    assert geo.region("Birmingham, AL") == geo.US
    assert geo.region("Cambridge, MA") == geo.US
    assert geo.region("Cambridge, United Kingdom") == geo.EUROPE
    assert geo.region("Birmingham") == geo.UNKNOWN, "alone it is unreadable, and stays so"


def test_austria_is_not_australia():
    assert geo.region("Vienna, Austria") == geo.EUROPE
    assert geo.region("Sydney, Australia") == geo.OTHER


def test_a_state_code_needs_its_slot():
    """A bare \\b(CA|IN|OR|ME)\\b reads the ordinary words in, or and me as
    Indiana, Oregon and Maine, and finds CA inside Casablanca."""
    assert geo.region("Casablanca") == geo.OTHER
    assert geo.region("Indianapolis, IN") == geo.US
    assert geo.region("Portland, OR") == geo.US


def test_the_us_token_takes_more_than_a_comma():
    """Every one of these is in the corpus and every one means the same."""
    for t in ("Remote - US", "US Remote", "Remote (US)", "US - Remote", "US, CA, Santa Clara"):
        assert geo.region(t) == geo.US, t


def test_remote_is_its_own_answer_not_a_missing_one():
    """A posting that says how the work happens and not where is a different
    fact from a posting that says nothing at all."""
    assert geo.region("Remote") == geo.REMOTE
    assert geo.region("Hybrid") == geo.REMOTE
    assert geo.region(None) == geo.UNKNOWN
    assert geo.region("") == geo.UNKNOWN


def test_a_real_place_beats_the_work_arrangement():
    assert geo.region("London (Hybrid)") == geo.EUROPE
    assert geo.region("Remote - Bengaluru") == geo.OTHER


def test_an_unrecognised_place_is_unknown_not_a_guess():
    assert geo.region("Metzingen / Riederich") == geo.UNKNOWN
    assert geo.region("PICKLE RESEARCH CAMPUS") == geo.UNKNOWN


def test_only_a_positive_elsewhere_is_refused():
    """The policy keeps four regions of five. `other` is the only one that
    asserts the job is somewhere we do not want -- everything else merely
    failed to say, and a posting refused at ingest cannot be reconsidered."""
    assert {"us", "europe", "remote", "unknown"} == config.STORE_REGIONS
    assert geo.in_target("San Francisco")
    assert geo.in_target("Berlin")
    assert geo.in_target("Remote")
    assert geo.in_target(None)
    assert not geo.in_target("Bengaluru, India")
