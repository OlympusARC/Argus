"""Where a job is, from a field nobody agreed the format of.

The same city arrives as "San Francisco", "San Francisco, CA",
"San Francisco, CA, United States" and "US, CA, Santa Clara", and 9.6% of the
corpus leaves the field empty. So these tests are mostly about what the
module refuses to guess.
"""

import pytest

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
    """
    A bare "Birmingham" used to stay unknown rather than be guessed. The
    gazetteer settles it by population instead, and the change is deliberate:
    12,050 open postings sat in `unknown`, and a bucket that large defeats
    the point of filtering by region at all.

    It is safe here because both Birminghams are in target regions, so the
    ingest decision is the same either way and only the label differs. Where
    a name is shared with somewhere we do not want -- London, Ontario against
    London, England -- population picks the more likely referent.
    """
    assert geo.region("Birmingham") == geo.EUROPE
    assert geo.region("London") == geo.EUROPE


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


def test_a_place_that_is_not_a_place_is_still_unknown():
    """The gazetteer resolves towns, not premises. A single short token is
    also refused: against 524,062 names the hit rate below four characters
    is mostly accident, and a wrong region is worse than none."""
    assert geo.region("PICKLE RESEARCH CAMPUS") == geo.UNKNOWN
    assert geo.region("Ord") == geo.UNKNOWN


@pytest.mark.parametrize("word", ["Headquarters", "TBD", "None", "Corporate"])
def test_a_word_meaning_no_location_is_not_read_as_one(word):
    """GeoNames holds a place called Headquarters, and one called TBD. The
    second is the costly one: it sits in a country we do not want, so a
    posting whose location is genuinely undecided would be *rejected* rather
    than merely unplaced. They are excluded when the file is built."""
    assert geo.region(word) == geo.UNKNOWN


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


"""
The gazetteer. A GeoNames extract committed alongside the code, so ingest
never touches the network and the answer is the same on every runner.
"""


def test_the_gazetteer_reads_a_city_in_its_own_script():
    """ "София" is Sofia. Names are stored as written and folded to ASCII,
    because a posting may use either."""
    assert geo.region("София") == geo.EUROPE
    assert geo.region("Sofia") == geo.EUROPE


def test_towns_too_small_for_the_hand_written_lists():
    for town in ("Schrobenhausen", "Stevenage", "Metzingen / Riederich", "Espelkamp"):
        assert geo.region(town) == geo.EUROPE, town


def test_the_gazetteer_rejects_as_well_as_accepts():
    """Rejecting is the point. A table of only the places we want would leave
    Mississauga and Pretoria in `unknown`, which ingest keeps."""
    for town in ("Mississauga", "Pretoria", "Markham", "Cape Town"):
        assert geo.region(town) == geo.OTHER, town


def test_the_most_specific_component_decides():
    """Longest first, so "Washington - Pullman" is read as Washington rather
    than by whichever fragment happened to match."""
    assert geo.region("Washington - Pullman") == geo.US


def test_the_hand_written_rules_still_win():
    """They encode judgement a population table cannot: that EMEA is a
    region, that a bare Remote is its own answer."""
    assert geo.region("EMEA") == geo.EUROPE
    assert geo.region("Remote") == geo.REMOTE


def test_a_missing_gazetteer_is_not_fatal(monkeypatch):
    """Without the file every rule above still applies; the answer is just
    `unknown` more often. A data file that fails to ship must not take the
    pipeline with it."""
    from pathlib import Path

    geo._gazetteer.cache_clear()
    monkeypatch.setattr(geo, "_DATA", Path("/nonexistent/cities.tsv.gz"))
    try:
        assert geo._gazetteer() == {}
        assert geo.region("San Francisco, CA") == geo.US
        assert geo.region("Schrobenhausen") == geo.UNKNOWN
    finally:
        geo._gazetteer.cache_clear()


def test_a_bare_word_may_accept_but_not_refuse():
    """A rejection at ingest is irreversible -- the posting is never stored --
    while a wrong `us` is a row in the wrong bucket. Bare words are where the
    table is least trustworthy: GeoNames holds a town called Progress, so
    "Work IN Progress" resolved to somewhere we do not want and the posting
    would have been dropped."""
    assert geo.region("Work IN Progress") == geo.UNKNOWN
    assert geo.region("Springfield Illinois") == geo.US
    # ...but a whole fragment still refuses
    assert geo.region("Mississauga, Ontario") == geo.OTHER


@pytest.mark.parametrize(
    "location", ["Zzyzxville, TX", "Zzyzxville, tx", "Zzyzxville, Tx", "Portland, or"]
)
def test_a_state_code_is_read_whatever_its_case(location):
    """It was uppercase-only, so an ATS writing "Austin, tx" placed and one
    writing "Austin, TX" did not. A made-up city here, because a real one
    would be placed by the gazetteer and hide the bug."""
    assert geo.region(location) == geo.US


@pytest.mark.parametrize(
    "location", ["sf", "SF", "sf, ca", "NYC", "Bay Area", "Remote - SF", "Silicon Valley"]
)
def test_shorthand_a_person_writes_but_no_gazetteer_holds(location):
    """ "sf" is two characters, below the length any place lookup will trust,
    and it is what a startup actually puts in the field."""
    assert geo.region(location) == geo.US


"""
Two-letter codes. Five US state codes are also European country codes --
AL, DE, MD, ME, MT -- and both readings are wrong as a blanket rule.
"""


@pytest.mark.parametrize(
    "location,want",
    [
        # the city agrees with the code, so the code is a country
        ("Munich, DE", "europe"),
        ("Valletta, MT", "europe"),
        # the city disagrees, so the code is a state. These are real US cities
        # with European names and are the reason neither reading can be assumed.
        ("Paris, TX", "us"),
        ("Berlin, CT", "us"),
        ("Dublin, OH", "us"),
        ("Vienna, VA", "us"),
        ("Dover, DE", "us"),
        ("Birmingham, AL", "us"),
    ],
)
def test_a_two_letter_code_is_believed_only_when_the_city_agrees(location, want):
    assert geo.region(location) == want, location


def test_a_spelled_out_country_beats_a_two_letter_code():
    """ "Tarragona, CT, Spain" was read as Connecticut: the abbreviation was
    tried first and Spain never got a look."""
    assert geo.region("Tarragona, CT, Spain") == geo.EUROPE
    assert geo.region("Cambridge, MA, United States") == geo.US


@pytest.mark.parametrize(
    "location,want",
    [
        ("Woking, Surrey", "europe"),
        ("Mississauga, Ontario", "other"),
        ("Metzingen / Riederich", "europe"),
        ("Washington - Pullman", "us"),
    ],
)
def test_the_leftmost_component_decides_not_the_longest(location, want):
    """ "Woking, Surrey" splits into two six-character fragments, so sorting
    by length left the tie to be broken arbitrarily -- and it broke on Surrey,
    which GeoNames holds as the one in British Columbia. Six English postings
    were refused as Canadian.

    Leftmost is where the specific component goes: the town names the place,
    what follows it names the region."""
    assert geo.region(location) == want, location
