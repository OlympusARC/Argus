"""Company identity: when two references are the same company, and when not.

This is the one place in the system where rows get *merged* rather than
inserted, so every rule here is a rule about not corrupting the registry. Two
failure modes are worth naming, because both are silent:

  splitting  -- the same company held twice, each row carrying half the
                metadata, so neither is ever complete enough to monitor.
  collapsing -- two different companies merged, which hands one company's
                careers page to another and points monitoring at the wrong ATS.
"""

import pytest

from argus.core import db
from argus.core.names import apex, base, plausibly_same
from argus.registry import companies as co


@pytest.fixture()
def conn(tmp_path):
    return db.init_db(tmp_path / "t.db")


def names(conn):
    return [
        dict(r)
        for r in conn.execute("SELECT id, name, domain, norm_name FROM companies ORDER BY id")
    ]


"""
----------------------------------------------------------- identity -----
"""


def test_same_domain_is_one_company(conn):
    a = co.upsert(conn, domain="ramp.com", name="Ramp")
    b = co.upsert(conn, domain="www.ramp.com", name="Ramp Financial")
    assert a == b
    assert len(names(conn)) == 1


def test_legal_suffix_does_not_split_a_company(conn):
    a = co.upsert(conn, name="Databricks, Inc.")
    b = co.upsert(conn, name="Databricks")
    assert a == b


def test_name_only_row_adopts_the_domain_when_it_is_learned(conn):
    first = co.upsert(conn, name="Acme Robotics")
    assert names(conn)[0]["domain"] is None
    second = co.upsert(conn, name="Acme Robotics", domain="acmerobotics.com")
    """
    Same row, now with a domain -- not a second row.
    """
    assert first == second
    rows = names(conn)
    assert len(rows) == 1 and rows[0]["domain"] == "acmerobotics.com"


def test_same_name_different_domains_stays_two_companies(conn):
    """Name collisions are real: several unrelated firms are called Alan."""
    a = co.upsert(conn, name="Alan", domain="alan.com")
    b = co.upsert(conn, name="Alan", domain="alan.eu")
    assert a != b
    assert len(names(conn)) == 2


def test_existing_values_are_never_overwritten(conn):
    cid = co.upsert(
        conn, domain="ramp.com", name="Ramp", careers_url="https://ramp.com/careers"
    )
    co.upsert(
        conn,
        domain="ramp.com",
        name="RAMP BUSINESS CORP",
        careers_url="https://elsewhere.example/jobs",
    )
    row = conn.execute("SELECT name, careers_url FROM companies WHERE id=?", (cid,)).fetchone()
    assert row["name"] == "Ramp"
    assert row["careers_url"] == "https://ramp.com/careers"


def test_a_reference_with_neither_name_nor_domain_is_dropped(conn):
    assert co.upsert(conn, website="https://boards.greenhouse.io/x") is None
    assert names(conn) == []


"""
------------------------------------------------------------- domains -----
"""


@pytest.mark.parametrize(
    "host",
    [
        "boards.greenhouse.io/stripe",  # an ATS, not a company site
        "jobs.ashbyhq.com/ramp",
        "nvidia.wd5.myworkdayjobs.com",
        "https://simplify.jobs/c/Mechanize",  # an aggregator's profile page
        "linkedin.com/company/ramp",
    ],
)
def test_ats_and_aggregator_hosts_are_never_a_company_domain(host):
    """
    Storing one of these merges every company that lists on that ATS.
    """
    assert apex(host) is None


def test_apex_strips_scheme_www_and_path():
    assert apex("https://www.Ramp.com/careers?x=1") == "ramp.com"


def test_normalization_is_shared_with_the_domain_guesser():
    """
    careers.py guesses <base>.com, so base() drifting apart from that guess
    would quietly stop name-only companies from ever resolving.
    """
    assert base("Databricks, Inc.") == "databricks"
    assert base("80,000 Hours") == "80000hours"


"""
--------------------------------------------------- guessed ownership -----
"""


class _Ref:
    def __init__(self, ats, slug):
        self.ats, self.slug = ats, slug


def test_a_guessed_domain_needs_the_page_to_prove_ownership(conn):
    cid = co.upsert(conn, name="Alan")
    """
    A page that names a completely different company proves nothing.
    """
    assert not co._owns(conn, cid, "alan", [_Ref("ashby", "someoneelse")])


def test_a_slug_matching_the_name_proves_ownership(conn):
    cid = co.upsert(conn, name="Alan")
    assert co._owns(conn, cid, "alan", [_Ref("ashby", "alan")])


def test_a_board_we_already_tied_to_the_company_proves_ownership(conn):
    cid = co.upsert(conn, name="Acme Robotics")
    conn.execute(
        """INSERT INTO boards (ats, slug, status, tier, first_seen_at, company_id)
                    VALUES ('ashby','acme-r','active',1,0,?)""",
        (cid,),
    )
    assert co._owns(conn, cid, "acmerobotics", [_Ref("ashby", "acme-r")])


def test_no_refs_never_proves_ownership(conn):
    cid = co.upsert(conn, name="Alan")
    assert not co._owns(conn, cid, "alan", [])


"""
-------------------------------------------------------- careers kind -----
"""


def test_careers_kind_separates_an_ats_from_a_page_from_nothing():
    """
    The middle case is the one that matters: a real careers page on no ATS we
    recognize is a monitoring target, not a dead end.
    """
    assert co.classify("https://x.com/careers", [_Ref("ashby", "x")]) == "ats"
    assert co.classify("https://x.com/careers", []) == "html"
    assert co.classify(None, []) == "none"


"""
------------------------------------------------------------- linking -----
"""


def test_adopting_boards_links_them_and_survives_a_dead_slug(conn):
    conn.execute("""INSERT INTO boards (ats, slug, company_name, status, tier, first_seen_at)
                    VALUES ('greenhouse','acme','Acme Robotics','dead',1,0)""")
    conn.execute("""INSERT INTO boards (ats, slug, company_name, status, tier, first_seen_at)
                    VALUES ('ashby','acme','Acme Robotics','active',1,0)""")
    res = co.adopt_boards(conn)
    assert res["linked"] == 2
    """
    One company holding both boards is the point: the Greenhouse slug dying
    is a migration, not the loss of the company.
    """
    assert res["companies"] == 1
    got = conn.execute(
        "SELECT COUNT(DISTINCT company_id) n FROM boards WHERE company_id IS NOT NULL"
    ).fetchone()["n"]
    assert got == 1


def test_boards_naming_nobody_are_left_unlinked(conn):
    conn.execute("""INSERT INTO boards (ats, slug, status, tier, first_seen_at)
                    VALUES ('ashby','mystery','active',1,0)""")
    co.adopt_boards(conn)
    """
    Inventing a company from a slug would fabricate a row that never merges
    with the real one when a source finally names it.
    """
    assert names(conn) == []


"""
------------------------------------------------------------- merging -----
"""


def test_learning_a_domain_that_another_row_owns_merges_them(conn):
    """The crash this prevents is a UNIQUE violation mid-sweep.

    A company can be created from a name long before anyone gives us its
    domain, and by the time we resolve that domain another source may already
    have created the row for it. Assigning the domain would violate the unique
    index; keeping both rows would split one company in two, with its boards on
    one and its careers page on the other.
    """
    known = co.upsert(conn, domain="acme.com", careers_url="https://acme.com/careers")
    named = co.upsert(conn, name="Acme")
    assert known != named
    conn.execute(
        """INSERT INTO boards (ats, slug, status, tier, first_seen_at, company_id)
                    VALUES ('ashby','acme','active',1,0,?)""",
        (named,),
    )

    co.merge_into(conn, named, known)

    rows = names(conn)
    assert len(rows) == 1 and rows[0]["id"] == known
    """
    The surviving row gains what only the dead one had...
    """
    assert rows[0]["name"] == "Acme"
    """
    ...keeps what it already had...
    """
    assert (
        conn.execute("SELECT careers_url FROM companies WHERE id=?", (known,)).fetchone()[
            "careers_url"
        ]
        == "https://acme.com/careers"
    )
    """
    ...and inherits the boards, which is the part that must not be dropped.
    """
    assert (
        conn.execute("SELECT company_id FROM boards WHERE slug='acme'").fetchone()["company_id"]
        == known
    )


def test_merging_a_company_into_itself_is_a_no_op(conn):
    cid = co.upsert(conn, domain="acme.com", name="Acme")
    co.merge_into(conn, cid, cid)
    assert len(names(conn)) == 1


def test_owner_of_domain_finds_the_row_that_holds_it(conn):
    cid = co.upsert(conn, domain="acme.com")
    assert co.owner_of_domain(conn, "acme.com") == cid
    assert co.owner_of_domain(conn, "nobody.com") is None


"""
------------------------------------------------- the acquisition trap -----
An acquired company's careers page keeps resolving and now serves the
acquirer's board. A prober walking a list of company domains therefore
concludes that the acquirer's domain is the acquired company's -- which is
how Figma ended up filed under visly.app. The board is real and kept; only
the claim about whose site it is gets dropped.
"""


@pytest.mark.parametrize(
    "domain,slug,name",
    [
        ("visly.app", "figma", "Figma"),  # Visly was acquired by Figma
        ("pipebio.com", "benchling", "Benchling"),
        ("openmeter.io", "kong", "Kong"),
        ("buildwithfern.com", "postman", "Postman"),
    ],
)
def test_an_unrelated_domain_is_not_attributed(domain, slug, name):
    assert not plausibly_same(domain, slug, name)


@pytest.mark.parametrize(
    "domain,slug,name",
    [
        ("ramp.com", "ramp", "Ramp"),
        ("weaveos.com", "weave", "Weave"),  # stem extends the slug
        ("getcargo.io", "cargo", "Cargo"),  # ...and the prefixed forms,
        ("usesimple.ai", "simple-ai", "Simple AI"),  # which are how startups buy a
        ("withdavid.ai", "david-ai", "David AI"),  # name someone else already has
        ("heymalama.co", "malama-health", "Malama Health"),
    ],
)
def test_a_company_owns_the_domain_that_looks_like_it(domain, slug, name):
    assert plausibly_same(domain, slug, name)


def test_attribution_needs_something_to_compare_against():
    assert not plausibly_same("ramp.com")
    assert not plausibly_same(None, "ramp")
    """
    An ATS host is not a company domain, so it can never be attributed.
    """
    assert not plausibly_same("boards.greenhouse.io", "figma", "Figma")


"""
------------------------------------------------------- empty database -----
"""


def test_stats_on_an_empty_database_are_zero_not_null(conn):
    """SUM() over zero rows returns NULL in SQLite, not 0.

    Every caller divides by the total to render a percentage, so a NULL here
    crashes `argus companies` on a fresh checkout -- the very first command a
    new contributor runs.
    """
    st = co.stats(conn)
    assert st["total"] == 0
    for key in (
        "domained",
        "careers",
        "ats",
        "html",
        "none_",
        "unprobed",
        "boards_linked",
        "monitored",
        "unmonitored",
        "page_but_unmonitored",
        "unguessable",
    ):
        assert st[key] == 0, f"{key} was {st[key]!r}, expected 0"
