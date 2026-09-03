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


def post(eid, title="Engineer", location="NYC", posted_at=None, posted_bound=None):
    return Posting(
        ats="ashby",
        slug="acme",
        external_id=eid,
        title=title,
        url=f"https://jobs.ashbyhq.com/acme/{eid}",
        location=location,
        posted_at=posted_at,
        posted_bound=posted_bound,
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

    monkeypatch.setattr(config, "posted_after", lambda: 1_782_864_000)
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

    monkeypatch.setattr(config, "posted_after", lambda: 1_782_864_000)
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


def test_a_null_posted_date_is_filled_in_by_a_later_poll(conn, monkeypatch):
    """The ingest path no longer produces a null -- every posting is dated,
    by the source or by the run. This still matters for rows written before
    that was true, and for any future adapter that learns to date postings it
    once could not, which is exactly what happened when Workday learned to
    read "Posted 5 Days Ago" after 54,843 of its rows were already stored.
    """
    from argus.feed import diff

    monkeypatch.setattr(config, "posted_after", lambda: 0)
    diff.run_batch(conn, {("ashby", "acme"): [post("1", posted_at=None)]})
    conn.execute("UPDATE jobs SET posted_at = NULL")
    conn.commit()

    diff.run_batch(conn, {("ashby", "acme"): [post("1", posted_at=1_790_000_000)]})
    assert conn.execute("SELECT posted_at FROM jobs").fetchone()["posted_at"] == 1_790_000_000


def test_a_date_we_already_have_is_never_overwritten(conn, monkeypatch):
    """Only nulls are filled. A source that starts reporting something
    different must not be able to rewrite history."""
    from argus.feed import diff

    monkeypatch.setattr(config, "posted_after", lambda: 0)
    diff.run_batch(conn, {("ashby", "acme"): [post("1", posted_at=1_700_000_000)]})
    diff.run_batch(conn, {("ashby", "acme"): [post("1", posted_at=1_790_000_000)]})
    assert conn.execute("SELECT posted_at FROM jobs").fetchone()["posted_at"] == 1_700_000_000


def test_region_is_computed_at_ingest(conn, monkeypatch):
    """geo.region reads a gazetteer in Python, so the alternative to a stored
    column is fetching every row and deciding in the application. Written on
    insert, like role_family, for the same reason."""
    from argus.feed import diff

    monkeypatch.setattr(config, "posted_after", lambda: 0)
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

    monkeypatch.setattr(config, "posted_after", lambda: 0)
    diff.run_batch(conn, {("ashby", "acme"): [post("1", location="Austin, TX")]})
    assert conn.execute("SELECT region FROM jobs").fetchone()["region"] == "us"

    diff.run_batch(conn, {("ashby", "acme"): [post("1", location="Dublin, Ireland")]})
    assert conn.execute("SELECT region FROM jobs").fetchone()["region"] == "europe"


def test_a_bound_is_enough_to_reject_but_never_to_store(conn, monkeypatch):
    """Workday says "Posted 30+ Days Ago" for 71% of its undated postings.
    That is not a date, but it is a bound -- at least thirty days old, so at
    most now-30d -- and a bound is exactly what a rejection test needs. If
    even the newest date it could have is older than the cutoff, it is too
    old whatever the truth is."""
    from argus.feed import diff

    cutoff = 1_782_864_000
    monkeypatch.setattr(config, "posted_after", lambda: cutoff)

    diff.run_batch(
        conn,
        {
            ("ashby", "acme"): [
                post("old", posted_at=None, posted_bound=cutoff - 86_400),
                post("new", posted_at=None, posted_bound=cutoff + 86_400),
            ]
        },
    )
    rows = {
        r["external_id"]: r["posted_at"]
        for r in conn.execute("SELECT external_id, posted_at FROM jobs")
    }
    assert "old" not in rows, "the bound rejected it"
    assert "new" in rows, "the bound could not reject it, so it is kept"
    assert rows["new"] != cutoff + 86_400, "the bound itself was never stored"
    assert rows["new"] == jobs.now() // 86400 * 86400, "it took today instead"


def test_a_bound_does_not_make_a_posting_look_edited():
    """It is not in _HASHED, so a posting acquiring one is not news."""
    from argus.core.models import Posting

    plain = Posting(ats="a", slug="b", external_id="1", title="T", url="u")
    bounded = Posting(
        ats="a", slug="b", external_id="1", title="T", url="u", posted_bound=1_788_400_000
    )
    assert plain.content_hash() == bounded.content_hash()


def test_a_source_with_no_dates_at_all_is_exempt(conn, monkeypatch):
    """BambooHR publishes no date in its list endpoint, none in the detail
    page, nothing to parse and nothing to bound. Filtering it on age would
    not filter it -- it would delete the source, all 3,133 postings, 2,223 of
    them engineering roles reachable through no other ATS."""
    from argus.feed import diff

    conn.execute("""INSERT INTO boards (ats, slug, status, tier, first_seen_at)
                    VALUES ('bamboohr','acme','active',1,0)""")
    monkeypatch.setattr(config, "posted_after", lambda: 1_790_000_000)

    undated = Posting(
        ats="bamboohr",
        slug="acme",
        external_id="1",
        title="Software Engineer",
        url="https://acme.bamboohr.com/careers/1",
    )
    diff.run_batch(conn, {("bamboohr", "acme"): [undated]})
    assert (
        conn.execute("SELECT COUNT(*) n FROM jobs WHERE ats='bamboohr'").fetchone()["n"] == 1
    ), "kept despite having no date"


def test_the_exemption_does_not_leak_to_other_sources(conn, monkeypatch):
    """An exemption that applied everywhere would be a disabled filter."""
    from argus.feed import diff

    monkeypatch.setattr(config, "posted_after", lambda: 1_790_000_000)
    diff.run_batch(conn, {("ashby", "acme"): [post("1", posted_at=1_700_000_000)]})
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 0


def test_an_edit_never_erases_a_date_we_already_had(conn, monkeypatch):
    """The reason an hourly poll is enough: a posting is dated when first
    seen fresh, and keeps that date as it ages. Workday says "Posted 5 Days
    Ago" on day five and "Posted 30+ Days Ago" on day forty -- the second is
    undateable, so it arrives as NULL.

    Assigning that NULL erased a date we already knew, and only for postings
    that happened to be edited. Silent and selective, which is the worst
    shape a data-loss bug can have.
    """
    from argus.feed import diff

    monkeypatch.setattr(config, "posted_after", lambda: 0)
    diff.run_batch(conn, {("ashby", "acme"): [post("1", posted_at=1_786_000_000)]})
    assert conn.execute("SELECT posted_at FROM jobs").fetchone()["posted_at"] == 1_786_000_000

    diff.run_batch(conn, {("ashby", "acme"): [post("1", title="Senior", posted_at=None)]})
    row = conn.execute("SELECT posted_at, title FROM jobs").fetchone()
    assert row["title"] == "Senior", "the edit applied"
    assert row["posted_at"] == 1_786_000_000, "and the date survived it"


def test_a_source_correcting_a_date_still_wins(conn, monkeypatch):
    """COALESCE preserves, it does not freeze. A real value replaces the
    stored one; only NULL is ignored."""
    from argus.feed import diff

    monkeypatch.setattr(config, "posted_after", lambda: 0)
    diff.run_batch(conn, {("ashby", "acme"): [post("1", posted_at=1_786_000_000)]})
    diff.run_batch(
        conn, {("ashby", "acme"): [post("1", title="Staff", posted_at=1_787_000_000)]}
    )
    assert conn.execute("SELECT posted_at FROM jobs").fetchone()["posted_at"] == 1_787_000_000


def test_a_posting_the_source_will_not_date_gets_todays_date(conn, monkeypatch):
    """Every source, not a nominated few. A Posted column blank for some rows
    and filled for others reads as a bug rather than as an absence, and the
    most defensible thing known about an undated posting is when it turned
    up."""
    from argus.feed import diff

    conn.execute("""INSERT INTO boards (ats, slug, status, tier, first_seen_at)
                    VALUES ('bamboohr','acme','active',1,0)""")
    monkeypatch.setattr(config, "posted_after", lambda: 0)

    diff.run_batch(
        conn,
        {
            ("bamboohr", "acme"): [
                Posting(
                    ats="bamboohr",
                    slug="acme",
                    external_id="1",
                    title="Software Engineer",
                    url="https://acme.bamboohr.com/careers/1",
                )
            ]
        },
    )
    got = conn.execute("SELECT posted_at FROM jobs").fetchone()["posted_at"]
    assert got == jobs.now() // 86400 * 86400, "today, at day resolution"


def test_the_stand_in_date_is_written_once_not_refreshed(conn, monkeypatch):
    """The update paths COALESCE, so a later poll must not move it. A source
    that re-dated itself on every edit would sort to the top of the dashboard
    whenever a title changed."""
    from argus.feed import diff

    conn.execute("""INSERT INTO boards (ats, slug, status, tier, first_seen_at)
                    VALUES ('bamboohr','acme','active',1,0)""")
    monkeypatch.setattr(config, "posted_after", lambda: 0)

    def p(title):
        return Posting(
            ats="bamboohr",
            slug="acme",
            external_id="1",
            title=title,
            url="https://acme.bamboohr.com/careers/1",
        )

    diff.run_batch(conn, {("bamboohr", "acme"): [p("Engineer")]})
    first = conn.execute("SELECT posted_at FROM jobs").fetchone()["posted_at"]

    conn.execute("UPDATE jobs SET posted_at = ?", (first - 86_400 * 30,))
    conn.commit()
    diff.run_batch(conn, {("bamboohr", "acme"): [p("Senior Engineer")]})
    after = conn.execute("SELECT posted_at, title FROM jobs").fetchone()
    assert after["title"] == "Senior Engineer", "the edit applied"
    assert after["posted_at"] == first - 86_400 * 30, "the date did not move"


def test_every_source_gets_the_fallback_not_just_one(conn, monkeypatch):
    """An undated Ashby posting has the same hole in the same column as an
    undated BambooHR one."""
    from argus.feed import diff

    monkeypatch.setattr(config, "posted_after", lambda: 0)
    diff.run_batch(conn, {("ashby", "acme"): [post("1", posted_at=None)]})
    got = conn.execute("SELECT posted_at FROM jobs").fetchone()["posted_at"]
    assert got == jobs.now() // 86400 * 86400


def test_a_bounded_posting_is_rejected_before_it_can_be_stamped(conn, monkeypatch):
    """What keeps the fallback honest. "Posted 30+ Days Ago" carries a bound,
    fails the age test, and never reaches the fallback to be stamped with
    today -- which would turn a 2019 posting into a fresh one."""
    from argus.feed import diff

    cutoff = 1_786_320_000
    monkeypatch.setattr(config, "posted_after", lambda: cutoff)
    diff.run_batch(
        conn,
        {("ashby", "acme"): [post("old", posted_at=None, posted_bound=cutoff - 86_400)]},
    )
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 0


def test_the_seed_path_obeys_the_same_filters_as_the_poll_path(conn, monkeypatch):
    """A filter that guards one door is not a filter.

    Found in a finished corpus: 2,036 postings arrived through discovery
    carrying none of the ingest rules -- 1,483 published before the cutoff,
    472 in families the product does not serve, every one with a null region.
    The poll path had all three tests and the seed path had none.
    """
    monkeypatch.setattr(config, "posted_after", lambda: 1_786_320_000)
    monkeypatch.setattr(config, "STORE_ONLY_TECHNICAL", True)

    def P(eid, title="Software Engineer", posted=None, loc="New York, NY"):
        return Posting(
            ats="ashby",
            slug="acme",
            external_id=eid,
            title=title,
            url=f"https://jobs.ashbyhq.com/acme/{eid}",
            location=loc,
            posted_at=posted,
        )

    jobs.seed(
        conn,
        [
            P("keep", posted=1_790_000_000),
            P("old", posted=1_600_000_000),
            P("retail", title="Store Associate", posted=1_790_000_000),
            P("elsewhere", posted=1_790_000_000, loc="Bengaluru, India"),
        ],
        source="simplify",
    )
    kept = {r["external_id"] for r in conn.execute("SELECT external_id FROM jobs")}
    assert kept == {"keep"}


def test_a_seeded_row_carries_its_region(conn):
    """Seeded rows had a null region because _row never computed one, so 1,741
    of them were invisible to every region filter on the dashboard."""
    jobs.seed(
        conn,
        [
            Posting(
                ats="ashby",
                slug="acme",
                external_id="1",
                title="Software Engineer",
                url="https://jobs.ashbyhq.com/acme/1",
                location="Berlin, Germany",
                posted_at=1_790_000_000,
            )
        ],
        source="simplify",
    )
    assert conn.execute("SELECT region FROM jobs").fetchone()["region"] == "europe"


def test_the_fallback_date_does_not_encode_poll_order(conn, monkeypatch):
    """A per-second fallback made poll order into a sort key: every undated
    posting on one board shared one second, the next board got the next
    second, and "newest first" silently meant "polled last first". Page one
    of the dashboard was fifteen roles at one company.

    A day is the resolution the source actually supports -- it told us
    nothing, and the honest reading is "we saw this today"."""
    import time as _t

    from argus.feed import diff

    monkeypatch.setattr(config, "posted_after", lambda: 0)
    diff.run_batch(conn, {("ashby", "acme"): [post("1", posted_at=None)]})
    first = conn.execute("SELECT posted_at FROM jobs").fetchone()["posted_at"]

    """
    A later batch, a later second -- and the same stored date.
    """
    monkeypatch.setattr(jobs, "now", lambda: int(_t.time()) + 90)
    conn.execute("""INSERT INTO boards (ats, slug, status, tier, first_seen_at)
                    VALUES ('lever','other','active',1,0)""")
    diff.run_batch(
        conn,
        {
            ("lever", "other"): [
                Posting(
                    ats="lever",
                    slug="other",
                    external_id="9",
                    title="Engineer",
                    url="https://x/9",
                )
            ]
        },
    )
    second = conn.execute("SELECT posted_at FROM jobs WHERE ats='lever'").fetchone()[
        "posted_at"
    ]
    assert first == second, "two batches 90 seconds apart share one date"
    assert first % 86400 == 0, "and it is a day boundary"
