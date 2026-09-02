"""Careers-page attribution rules.

The aggregator guard is pinned because violating it is silent and expensive:
one YC company's page (getcargo.io) linked to 239 boards and mislabelled all of
them -- Ramp included -- with the page owner's name.
"""

from argus.discovery.probe import MAX_ATTRIBUTABLE_REFS
from argus.registry.careers import candidate_domains


def test_known_website_is_tried_first():
    got = candidate_domains("ramp", "Ramp", "https://ramp.com/about")
    assert got[0] == "ramp.com"


def test_guesses_derive_from_name_and_slug():
    got = candidate_domains("acmerobotics", "Acme Robotics", None)
    assert "acmerobotics.com" in got
    assert any(d.endswith(".io") for d in got)


def test_junk_names_do_not_produce_guesses():
    assert candidate_domains("a", None, None) == []


def test_attribution_threshold_is_small():
    """
    A company careers page links to its own board, maybe a couple of
    regional ones. Anything more is an aggregator.
    """
    assert 1 <= MAX_ATTRIBUTABLE_REFS <= 5
