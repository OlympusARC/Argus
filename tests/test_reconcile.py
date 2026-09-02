"""Set-diff behaviour, exercised against a temporary database.

These are the rules that decide whether the feed can be trusted, so each is
pinned rather than assumed.
"""

import pytest

from argus.core import config, db
from argus.core.models import Posting
from argus.feed import jobs, reconcile


@pytest.fixture(autouse=True)
def _store_everything(monkeypatch):
    """These tests exercise the diff, not the ruleset.

    Ingest filtering is on by default in production, but coupling a test
    about board isolation or reopen-versus-edit ordering to whether its
    fixture titles happen to look technical would make the diff's tests fail
    for reasons that have nothing to do with the diff. The filter has its
    own tests below.
    """
    monkeypatch.setattr(config, "STORE_ONLY_TECHNICAL", False)


@pytest.fixture()
def conn(tmp_path):
    c = db.init_db(tmp_path / "t.db")
    c.execute("""INSERT INTO boards (ats, slug, status, tier, first_seen_at)
                 VALUES ('ashby','acme','active',1,0)""")
    return c


def post(eid, title="Engineer", location="NYC", posted_at=None):
    return Posting(
        ats="ashby",
        slug="acme",
        external_id=eid,
        title=title,
        url=f"https://jobs.ashbyhq.com/acme/{eid}",
        location=location,
        posted_at=posted_at,
    )


def kinds(conn):
    return [r["type"] for r in conn.execute("SELECT type FROM events ORDER BY id")]


def test_first_sight_is_new(conn):
    res = reconcile.apply_board(conn, "ashby", "acme", [post("a"), post("b")])
    assert (res.new, res.edited, res.closed) == (2, 0, 0)
    assert kinds(conn) == ["new", "new"]


def test_unchanged_posting_emits_nothing(conn):
    reconcile.apply_board(conn, "ashby", "acme", [post("a")])
    res = reconcile.apply_board(conn, "ashby", "acme", [post("a")])
    assert (res.new, res.edited, res.closed) == (0, 0, 0)
    assert kinds(conn) == ["new"]  # still just the original


def test_content_change_is_an_edit(conn):
    reconcile.apply_board(conn, "ashby", "acme", [post("a", title="Engineer")])
    res = reconcile.apply_board(conn, "ashby", "acme", [post("a", title="Senior Engineer")])
    assert res.edited == 1
    assert kinds(conn)[-1] == "edited"


def test_close_requires_repeated_absence(conn):
    """One missing poll must not close a job -- a truncated response would
    otherwise wipe a board."""
    reconcile.apply_board(conn, "ashby", "acme", [post("a"), post("b")])
    first = reconcile.apply_board(conn, "ashby", "acme", [post("a")])
    assert first.closed == 0
    second = reconcile.apply_board(conn, "ashby", "acme", [post("a")])
    assert second.closed == 1
    assert kinds(conn)[-1] == "closed"


def test_reappearing_posting_reopens_rather_than_duplicates(conn):
    reconcile.apply_board(conn, "ashby", "acme", [post("a")])
    reconcile.apply_board(conn, "ashby", "acme", [])
    reconcile.apply_board(conn, "ashby", "acme", [])  # now closed
    res = reconcile.apply_board(conn, "ashby", "acme", [post("a")])
    assert res.reopened == 1 and res.new == 0
    n = conn.execute("SELECT COUNT(*) FROM jobs WHERE external_id='a'").fetchone()[0]
    assert n == 1  # not duplicated
    assert conn.execute("SELECT status FROM jobs WHERE external_id='a'").fetchone()[0] == "open"


def test_empty_response_on_a_busy_board_is_treated_as_suspicious(conn):
    """The guard that matters most: a board holding many open jobs returning
    nothing is a bad response, not a mass layoff."""
    many = [post(str(i)) for i in range(20)]
    reconcile.apply_board(conn, "ashby", "acme", many)
    res = reconcile.apply_board(conn, "ashby", "acme", [])
    assert res.suspicious is True
    assert res.closed == 0
    still_open = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='open'").fetchone()[0]
    assert still_open == 20


def test_small_board_emptying_is_allowed_to_close(conn):
    """The guard must not block legitimate closes on a small board."""
    reconcile.apply_board(conn, "ashby", "acme", [post("a")])
    reconcile.apply_board(conn, "ashby", "acme", [])
    res = reconcile.apply_board(conn, "ashby", "acme", [])
    assert res.suspicious is False and res.closed == 1


def test_seeded_posting_is_adopted_not_duplicated(conn):
    """Postings seeded from SimplifyJobs must merge with the polled row."""
    jobs.seed(conn, [post("a")], source="simplify")
    res = reconcile.apply_board(conn, "ashby", "acme", [post("a")])
    assert res.new == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_events_carry_location(conn):
    """Boards routinely post one role per country, so title alone makes the
    feed unreadable -- location is what tells the rows apart."""
    import json

    reconcile.apply_board(conn, "ashby", "acme", [post("a", location="Berlin")])
    d = conn.execute("SELECT detail_json FROM events WHERE type='new'").fetchone()[0]
    assert json.loads(d)["location"] == "Berlin"


def test_close_event_names_what_closed(conn):
    """A close carries the stored title/url; without it the feed shows a
    bare id and nobody can tell which role went away."""
    reconcile.apply_board(conn, "ashby", "acme", [post("a", title="Staff SRE")])
    reconcile.apply_board(conn, "ashby", "acme", [])
    reconcile.apply_board(conn, "ashby", "acme", [])
    row = conn.execute("SELECT title, url FROM events WHERE type='closed'").fetchone()
    assert row["title"] == "Staff SRE"
    assert row["url"].endswith("/a")


"""
The diff runs in the database now, and settles a batch of boards at once. Two
properties are worth pinning beyond "the right events fire": that nothing but
the changes crosses the wire, and that batching a board with others does not
change its outcome. A regression in either would be invisible -- correct
results, quietly billing for the whole corpus, or one board's bad response
corrupting another's.
"""


def test_an_unchanged_board_returns_no_rows(conn):
    """The steady state is the common case: most boards change nothing hourly."""
    from argus.feed import diff

    board = [post("1"), post("2"), post("3")]
    reconcile.apply_board(conn, "ashby", "acme", board)

    changed = diff.run_batch(conn, {("ashby", "acme"): board})[("ashby", "acme")]
    assert changed["new"] == []
    assert changed["edited"] == []
    assert changed["reopened"] == []
    assert changed["closed"] == []


def test_only_the_changed_row_comes_back(conn):
    """One edit in a board of fifty must return one row, not fifty."""
    from argus.feed import diff

    board = [post(str(i)) for i in range(50)]
    reconcile.apply_board(conn, "ashby", "acme", board)

    board[7] = post("7", title="Staff Engineer")
    changed = diff.run_batch(conn, {("ashby", "acme"): board})[("ashby", "acme")]
    assert [r["external_id"] for r in changed["edited"]] == ["7"]
    assert changed["new"] == [] and changed["reopened"] == []


def test_a_reopen_is_not_also_reported_as_an_edit(conn):
    """reopen runs before edit and leaves the new hash, so edit cannot see it."""
    from argus.feed import diff

    reconcile.apply_board(conn, "ashby", "acme", [post("1")])
    for _ in range(config.CLOSE_GRACE_POLLS):
        reconcile.apply_board(conn, "ashby", "acme", [])
    assert (
        conn.execute("SELECT status FROM jobs WHERE external_id='1'").fetchone()[0] == "closed"
    )

    changed = diff.run_batch(conn, {("ashby", "acme"): [post("1", title="Back Again")]})
    changed = changed[("ashby", "acme")]
    assert [r["external_id"] for r in changed["reopened"]] == ["1"]
    assert changed["edited"] == [], "a reopen must not double-report as an edit"


def test_boards_in_a_batch_do_not_leak_into_each_other(conn):
    """Every statement joins on (ats, slug); a missing join would cross boards."""
    conn.execute(
        """INSERT INTO boards (ats, slug, status, tier, first_seen_at)
           VALUES ('ashby','other','active',1,0)"""
    )
    shared_id = "same-external-id"
    results = reconcile.apply_batch(
        conn,
        {
            ("ashby", "acme"): [post(shared_id, title="Acme role")],
            ("ashby", "other"): [post(shared_id, title="Other role")],
        },
    )
    assert results[("ashby", "acme")].new == 1
    assert results[("ashby", "other")].new == 1
    titles = dict(
        conn.execute(
            "SELECT slug, title FROM jobs WHERE external_id=?", (shared_id,)
        ).fetchall()
    )
    assert titles == {"acme": "Acme role", "other": "Other role"}


def test_a_suspicious_board_does_not_stop_the_rest_of_the_batch(conn):
    """One bad response must not hold up the boards batched alongside it."""
    conn.execute(
        """INSERT INTO boards (ats, slug, status, tier, first_seen_at)
           VALUES ('ashby','other','active',1,0)"""
    )
    busy = [post(str(i)) for i in range(config.MASS_CLOSE_GUARD + 3)]
    reconcile.apply_board(conn, "ashby", "acme", busy)

    results = reconcile.apply_batch(
        conn,
        {
            ("ashby", "acme"): [],
            ("ashby", "other"): [post("x", title="Still hiring")],
        },
    )
    assert results[("ashby", "acme")].suspicious is True
    assert results[("ashby", "other")].new == 1
    still_open = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE slug='acme' AND status='open'"
    ).fetchone()[0]
    assert still_open == len(busy), "the suspect board must be left entirely alone"


def test_a_batch_is_bounded_by_postings_not_just_boards(conn, monkeypatch):
    """What broke the hourly poll for eighteen hours.

    Workday averages 183 open postings per board against Ashby's 17, and one
    board holds 20,598 -- so a hundred-board batch stages 181,401 rows, a
    third of the corpus, and the EDIT update exceeds Postgres's two-minute
    statement timeout. Counting boards measured the wrong thing.
    """
    from argus.core import config
    from argus.feed import reconcile

    monkeypatch.setattr(config, "BATCH_POSTINGS", 1_000)
    batches = []

    def spy(_conn, fetched):
        batches.append(sum(len(v) for v in fetched.values()))
        return {
            k: reconcile.BoardResult(ats=k[0], slug=k[1], ok=True, present=len(v))
            for k, v in fetched.items()
        }

    monkeypatch.setattr(reconcile, "apply_batch", spy)

    """Six boards of 400 postings: board-count batching would send all six."""
    for i in range(6):
        conn.execute(
            """INSERT INTO boards (ats, slug, status, tier, first_seen_at)
               VALUES ('ashby',?, 'active',1,0)""",
            (f"big{i}",),
        )

    class FakeAdapter:
        name = "ashby"

        def fetch(self, slug):
            return [post(f"{slug}-{n}") for n in range(400)]

    monkeypatch.setattr("argus.adapters.get", lambda _ats: FakeAdapter())
    reconcile.run(conn, "ashby", batch=100, workers=1)

    assert batches, "nothing was flushed"
    assert max(batches) <= 1_400, f"a batch staged {max(batches)} rows, over the budget"
    assert len(batches) >= 3, "one giant batch means the bound did not apply"


def test_one_oversized_board_goes_alone_rather_than_being_split(conn, monkeypatch):
    """A board is never split: the diff's guards reason about a whole board's
    contents, and half a board looks exactly like a mass deletion."""
    from argus.core import config
    from argus.feed import reconcile

    monkeypatch.setattr(config, "BATCH_POSTINGS", 100)
    seen = []

    def spy(_conn, fetched):
        seen.append({k: len(v) for k, v in fetched.items()})
        return {
            k: reconcile.BoardResult(ats=k[0], slug=k[1], ok=True, present=len(v))
            for k, v in fetched.items()
        }

    monkeypatch.setattr(reconcile, "apply_batch", spy)
    for slug in ("small", "huge"):
        conn.execute(
            """INSERT INTO boards (ats, slug, status, tier, first_seen_at)
               VALUES ('ashby',?, 'active',1,0)""",
            (slug,),
        )

    class FakeAdapter:
        name = "ashby"

        def fetch(self, slug):
            return [post(f"{slug}-{n}") for n in range(500 if slug == "huge" else 10)]

    monkeypatch.setattr("argus.adapters.get", lambda _ats: FakeAdapter())
    reconcile.run(conn, "ashby", batch=100, workers=1)

    huge = [b for b in seen if any(v >= 500 for v in b.values())]
    assert huge, "the oversized board was never flushed"
    assert all(len(b) == 1 for b in huge), "it must travel alone, and whole"


"""
Ingest filtering. The corpus is 82% retail, clinical and sales work, so what
is never stored matters as much as what is.
"""


def test_only_the_named_families_are_stored(conn, monkeypatch):
    monkeypatch.setattr(config, "STORE_ONLY_TECHNICAL", True)
    res = reconcile.apply_board(
        conn,
        "ashby",
        "acme",
        [
            post("a", title="Senior Backend Engineer"),
            post("b", title="Retail Sales Associate"),
            post("c", title="Machine Learning Engineer"),
            post("d", title="Delivery Driver"),
            post("e", title="Product Manager"),
            post("f", title="Senior Product Designer"),
            post("g", title="Registered Nurse"),
        ],
    )
    stored = [r["title"] for r in conn.execute("SELECT title FROM jobs ORDER BY external_id")]
    assert stored == ["Senior Backend Engineer", "Machine Learning Engineer", "Product Manager"]
    assert res.new == 3


def test_the_stored_set_is_configuration_not_a_property_of_the_classifier(conn, monkeypatch):
    """Product management at a software company is a tech job by most
    readings, and is_engineering cannot answer that -- it answers whether
    something is engineering work. The boundary is a separate decision."""
    monkeypatch.setattr(config, "STORE_ONLY_TECHNICAL", True)
    monkeypatch.setattr(config, "STORE_FAMILIES", {"engineering", "design"})
    reconcile.apply_board(
        conn,
        "ashby",
        "acme",
        [
            post("a", title="Senior Backend Engineer"),
            post("b", title="Senior Product Designer"),
            post("c", title="Product Manager"),
        ],
    )
    stored = sorted(r["title"] for r in conn.execute("SELECT title FROM jobs"))
    assert stored == ["Senior Backend Engineer", "Senior Product Designer"]


def test_the_guard_judges_the_fetch_not_the_filter(conn, monkeypatch):
    """A retail board can return five hundred postings of which none are
    technical. Staging zero rows is the correct outcome, not evidence of a
    broken response -- and judging the fetch by the filtered count would mark
    every such board suspicious on every poll."""
    monkeypatch.setattr(config, "STORE_ONLY_TECHNICAL", True)
    monkeypatch.setattr(config, "MASS_CLOSE_GUARD", 2)

    reconcile.apply_board(conn, "ashby", "acme", [post(f"e{i}") for i in range(5)])
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 5

    """A healthy fetch that happens to contain no technical roles."""
    res = reconcile.apply_board(
        conn, "ashby", "acme", [post(f"r{i}", title="Cashier") for i in range(40)]
    )
    assert not res.suspicious, "a full fetch of retail roles is not a bad response"


def test_an_empty_fetch_is_still_suspicious(conn, monkeypatch):
    """The guard must keep working: nothing returned at all, on a board that
    held many, is still a bad response."""
    monkeypatch.setattr(config, "STORE_ONLY_TECHNICAL", True)
    monkeypatch.setattr(config, "MASS_CLOSE_GUARD", 2)
    reconcile.apply_board(conn, "ashby", "acme", [post(f"e{i}") for i in range(5)])
    res = reconcile.apply_board(conn, "ashby", "acme", [])
    assert res.suspicious
    assert (
        conn.execute("SELECT COUNT(*) n FROM jobs WHERE status='open'").fetchone()["n"] == 5
    ), "a suspect board closes nothing"


def test_filtering_can_be_turned_off(conn, monkeypatch):
    """The trade is real -- an unstored posting can never be reclassified --
    so it is a setting rather than a constant."""
    monkeypatch.setattr(config, "STORE_ONLY_TECHNICAL", False)
    reconcile.apply_board(conn, "ashby", "acme", [post("a", title="Retail Sales Associate")])
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 1


"""
The age filter, and the relative dates that feed it.
"""


def test_a_posting_older_than_the_cutoff_is_not_stored(conn, monkeypatch):
    """16% of the corpus was over a year old and the oldest still-open
    posting was dated 2009. A board that never takes a listing down should
    not be able to fill the table with them."""
    from argus.feed import diff

    monkeypatch.setattr(config, "STORE_POSTED_AFTER", 1_782_864_000)
    diff.run_batch(
        conn,
        {
            ("ashby", "acme"): [
                post("1", posted_at=1_600_000_000),
                post("2", posted_at=1_790_000_000),
            ]
        },
    )
    kept = [r["external_id"] for r in conn.execute("SELECT external_id FROM jobs")]
    assert kept == ["2"]


def test_a_posting_with_no_date_is_kept(conn, monkeypatch):
    """Absence is not age. Workday's "Posted 30+ Days Ago" and BambooHR's
    silence are not evidence, and refusing on them would discard most of two
    ATSs on none."""
    from argus.feed import diff

    monkeypatch.setattr(config, "STORE_POSTED_AFTER", 1_782_864_000)
    diff.run_batch(conn, {("ashby", "acme"): [post("3", posted_at=None)]})
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 1


def test_workday_relative_dates_become_epochs():
    """Workday states an age relative to the request rather than a date, so
    the age plus the request time is the date."""
    from argus.adapters.workday import posted_from_relative

    now = 1_788_400_000
    assert posted_from_relative("Posted Today", now) == now
    assert posted_from_relative("Posted Yesterday", now) == now - 86_400
    assert posted_from_relative("Posted 5 Days Ago", now) == now - 5 * 86_400


def test_thirty_plus_days_is_not_a_date():
    """It means at least thirty days and nothing about the ceiling, so it
    covers last month and 2019 equally. Storing now-30d would invent a
    precision the source does not have -- and the age filter would trust it."""
    from argus.adapters.workday import posted_from_relative

    assert posted_from_relative("Posted 30+ Days Ago", 1_788_400_000) is None
    assert posted_from_relative(None) is None
    assert posted_from_relative("nonsense") is None


def test_a_missing_posted_date_is_filled_in_later(conn, monkeypatch):
    """posted_at is not in _HASHED so it cannot make a posting look edited --
    which also means a row cannot gain one through the edit path, because
    gaining a date does not change the hash. The Workday adapter learned to
    read relative dates after 54,843 of its postings were already stored."""
    from argus.feed import diff

    monkeypatch.setattr(config, "STORE_POSTED_AFTER", 0)
    diff.run_batch(conn, {("ashby", "acme"): [post("1", posted_at=None)]})
    assert conn.execute("SELECT posted_at FROM jobs").fetchone()["posted_at"] is None

    """
    Same posting, same hash -- only the date is new. It is touched, not
    edited, so nothing but the column moves.
    """
    changed = diff.run_batch(conn, {("ashby", "acme"): [post("1", posted_at=1_790_000_000)]})
    assert conn.execute("SELECT posted_at FROM jobs").fetchone()["posted_at"] == 1_790_000_000
    assert changed[("ashby", "acme")]["edited"] == [], "a filled-in date is not an edit"
    assert changed[("ashby", "acme")]["new"] == []


def test_a_date_we_already_have_is_never_overwritten(conn, monkeypatch):
    """Only nulls are filled. A source that starts reporting something
    different must not be able to rewrite history."""
    from argus.feed import diff

    monkeypatch.setattr(config, "STORE_POSTED_AFTER", 0)
    diff.run_batch(conn, {("ashby", "acme"): [post("1", posted_at=1_700_000_000)]})
    diff.run_batch(conn, {("ashby", "acme"): [post("1", posted_at=1_790_000_000)]})
    assert conn.execute("SELECT posted_at FROM jobs").fetchone()["posted_at"] == 1_700_000_000


def test_region_is_computed_at_ingest(conn, monkeypatch):
    """geo.region reads a gazetteer in Python, so the alternative to a stored
    column is fetching every row and deciding in the application. Written on
    insert, like role_family, for the same reason."""
    from argus.feed import diff

    monkeypatch.setattr(config, "STORE_POSTED_AFTER", 0)
    diff.run_batch(
        conn,
        {
            ("ashby", "acme"): [
                post("1", location="San Francisco, CA"),
                post("2", location="Berlin, Germany"),
                post("3", location="Remote"),
                post("4", location=None),
            ]
        },
    )
    got = {
        r["external_id"]: r["region"]
        for r in conn.execute("SELECT external_id, region FROM jobs")
    }
    assert got == {"1": "us", "2": "europe", "3": "remote", "4": "unknown"}


def test_a_posting_that_moves_gets_a_new_region(conn, monkeypatch):
    """Region rides the edit path rather than being frozen at insert: a
    posting relisted in another office is genuinely somewhere else."""
    from argus.feed import diff

    monkeypatch.setattr(config, "STORE_POSTED_AFTER", 0)
    diff.run_batch(conn, {("ashby", "acme"): [post("1", location="Austin, TX")]})
    assert conn.execute("SELECT region FROM jobs").fetchone()["region"] == "us"

    diff.run_batch(conn, {("ashby", "acme"): [post("1", location="Dublin, Ireland")]})
    assert conn.execute("SELECT region FROM jobs").fetchone()["region"] == "europe"
