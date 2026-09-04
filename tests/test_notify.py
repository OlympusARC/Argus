"""The digest's rules, pinned.

Every test here exists because the failure it prevents is one the reader
would notice immediately: a first run that replays the entire history, a job
announced twice, a watermark that advances past jobs a failed webhook never
delivered.
"""

import pytest

from argus.core import db
from argus.feed import notify


@pytest.fixture()
def conn(tmp_path):
    c = db.init_db(tmp_path / "t.db")
    c.execute(
        """INSERT INTO boards (ats, slug, company_name, status, tier, first_seen_at)
           VALUES ('ashby','acme','Acme','active',1,0)"""
    )
    return c


def add_job(
    conn,
    eid,
    *,
    title="Backend Engineer",
    eng=1,
    fde=0,
    loc="NYC",
    seniority="new_grad",
    region="us",
):
    """Defaults land inside the digest's filter on purpose.

    The digest only announces new-grad and intern roles in the US or remote,
    so a fixture outside that would make every test here assert on an empty
    list and pass for the wrong reason. Tests that care about the filter set
    these explicitly.
    """
    conn.execute(
        """INSERT INTO jobs (ats, slug, external_id, title, url, location,
                             first_seen_at, last_seen_at, status,
                             is_engineering, is_fde, role_family,
                             seniority, region)
           VALUES ('ashby','acme',?,?,?,?,0,0,'open',?,?,'engineering',?,?)""",
        (
            eid,
            title,
            f"https://jobs.ashbyhq.com/acme/{eid}",
            loc,
            eng,
            fde,
            seniority,
            region,
        ),
    )


def add_event(conn, eid, kind="new", title="Backend Engineer", url=""):
    """title and url come from here, not from jobs.

    SELECT_PENDING reads e.title and e.url, so a test that seeds only the
    job's URL renders lines with no link and asserts on the wrong thing.
    """
    conn.execute(
        """INSERT INTO events (ts, type, ats, slug, external_id, title, url)
           VALUES (0, ?, 'ashby','acme', ?, ?, ?)""",
        (kind, eid, title, url),
    )


def seed(conn, n=3, kind="new", **kw):
    for i in range(n):
        add_job(conn, f"j{i}", **kw)
        add_event(conn, f"j{i}", kind)


def test_a_fresh_database_announces_nothing(conn):
    """The most expensive possible bug: 374,647 historical events replayed as
    though every job had just appeared. A first run seeds the watermark at the
    current maximum and stays silent."""
    seed(conn, 5)
    d = notify.build(conn)
    assert d.rows == []
    assert d.to_id == 5, "watermark should seed at the current max, not 0"
    assert notify.render(d) is None


def test_only_events_past_the_watermark_are_sent(conn):
    seed(conn, 3)
    notify.set_watermark(conn, 0)
    assert len(notify.build(conn).rows) == 3

    notify.set_watermark(conn, 2)
    d = notify.build(conn)
    assert [r["external_id"] for r in d.rows] == ["j2"]


def test_the_watermark_advances_on_an_empty_window(conn):
    """Otherwise every quiet hour re-scans the same range forever."""
    seed(conn, 2)
    notify.set_watermark(conn, 2)
    d = notify.run(conn, url="http://unused", dry=False)
    assert d.rows == []
    assert notify._watermark(conn) == 2


def test_non_technical_roles_are_not_announced(conn):
    add_job(conn, "sales", title="Account Executive", eng=0, fde=0)
    add_event(conn, "sales")
    add_job(conn, "eng", title="Backend Engineer", eng=1)
    add_event(conn, "eng")
    notify.set_watermark(conn, 0)
    assert [r["external_id"] for r in notify.build(conn).rows] == ["eng"]


def test_fde_is_announced_even_when_not_flagged_engineering(conn):
    """is_fde is its own axis; a forward-deployed role that did not also match
    the engineering patterns still belongs in the digest."""
    add_job(conn, "fde1", title="Forward Deployed Engineer", eng=0, fde=1)
    add_event(conn, "fde1")
    notify.set_watermark(conn, 0)
    d = notify.build(conn)
    assert [r["external_id"] for r in d.rows] == ["fde1"]
    assert d.fde == 1


def test_closed_and_edited_events_are_not_announced(conn):
    add_job(conn, "j0")
    for kind in ("edited", "closed"):
        add_event(conn, "j0", kind)
    notify.set_watermark(conn, 0)
    assert notify.build(conn).rows == []


def test_a_job_is_announced_once_even_when_it_reopens(conn):
    """A flapping board emits closed then reopened, repeatedly. The reader
    wants to hear about the job once -- which is why dedupe keys on the job
    and not on the event id."""
    add_job(conn, "j0")
    add_event(conn, "j0", "new")
    notify.set_watermark(conn, 0)

    d = notify.build(conn)
    assert len(d.rows) == 1
    notify.mark_notified(conn, d.rows)
    notify.set_watermark(conn, d.to_id)

    add_event(conn, "j0", "reopened")
    assert notify.build(conn).rows == [], "already announced"


def test_dedupe_expires_after_the_window(conn):
    add_job(conn, "j0")
    add_event(conn, "j0", "new")
    notify.set_watermark(conn, 0)
    stale = notify.now() - notify.DEDUPE_WINDOW - 1
    conn.execute(
        """INSERT INTO notified_jobs (ats, slug, external_id, notified_at)
           VALUES ('ashby','acme','j0',?)""",
        (stale,),
    )
    assert len(notify.build(conn).rows) == 1, "past the window, it is news again"


def test_flood_guard_collapses_to_a_summary(conn, monkeypatch):
    monkeypatch.setattr(notify, "FLOOD_THRESHOLD", 3)
    seed(conn, 5)
    notify.set_watermark(conn, 0)
    d = notify.build(conn)
    assert d.flooded and d.sending == 0
    payload = notify.render(d)
    assert "embeds" not in payload
    assert "5 new" in payload["content"]


def test_a_failed_send_does_not_advance_the_watermark(conn, monkeypatch):
    """A dropped webhook must cost a retry, never the jobs."""
    seed(conn, 2)
    notify.set_watermark(conn, 0)

    def boom(payload, url):
        raise RuntimeError("502 from discord")

    monkeypatch.setattr(notify, "send", boom)
    with pytest.raises(RuntimeError):
        notify.run(conn, url="http://x")
    assert notify._watermark(conn) == 0, "watermark held"
    assert len(notify.build(conn).rows) == 2, "jobs still pending"


def test_dry_run_touches_nothing(conn, monkeypatch):
    seed(conn, 2)
    notify.set_watermark(conn, 0)
    monkeypatch.setattr(notify, "send", lambda *a, **k: pytest.fail("dry run must not send"))
    notify.run(conn, url="http://x", dry=True)
    assert notify._watermark(conn) == 0


def test_payload_stays_inside_discord_limits(conn):
    """Ten embeds and 6000 characters are hard caps; exceeding either is a
    400 from the webhook, which would look exactly like a broken notifier.

    Driven with real-length Workday URLs, because that is what broke the
    first attempt: a line count cannot bound a payload whose lines carry a
    300-character URL."""
    long_url = (
        "https://globalhr.wd5.myworkdayjobs.com/rec_rtx_ext_gateway/job/"
        "US-TX-McKinney-2501-West-University-Drive/" + "Software-Engineer-New-Grad" * 3
    )
    for i in range(60):
        lvl = "new_grad" if i % 2 else "intern"
        conn.execute(
            """INSERT INTO jobs (ats, slug, external_id, title, url, location,
                                 first_seen_at, last_seen_at, status,
                                 is_engineering, is_fde, role_family,
                                 seniority, region)
               VALUES ('ashby','acme',?,?,?,?,0,0,'open',1,0,'engineering',?,'us')""",
            (
                f"j{i}",
                "Staff Software Engineer, Platform Infrastructure and Reliability",
                f"{long_url}/{i}",
                "San Francisco, California, United States",
                lvl,
            ),
        )
        add_event(
            conn,
            f"j{i}",
            title="Staff Software Engineer, Platform Infrastructure and Reliability",
            url=f"{long_url}/{i}",
        )
    notify.set_watermark(conn, 0)
    d = notify.build(conn)
    d.flooded = False  # exercise the embed path regardless of the guard
    payload = notify.render(d)

    assert len(payload["embeds"]) <= 10
    total = len(payload["content"]) + sum(
        len(e["title"]) + len(e["description"]) for e in payload["embeds"]
    )
    assert total < 6000, f"payload {total} chars exceeds Discord's limit"
    assert all(len(e["description"]) <= 4096 for e in payload["embeds"])


def test_both_groups_survive_a_tight_budget(conn):
    """The trim takes lines round-robin. Filling one group and then the next
    would let long URLs in the first spend the whole budget, leaving the
    second empty -- which reads as a broken filter, not a full message."""
    long_url = "https://x.test/" + "a" * 300
    for i in range(40):
        lvl = "new_grad" if i < 20 else "intern"
        conn.execute(
            """INSERT INTO jobs (ats, slug, external_id, title, url, location,
                                 first_seen_at, last_seen_at, status,
                                 is_engineering, is_fde, role_family,
                                 seniority, region)
               VALUES ('ashby','acme',?,?,?,'NYC',0,0,'open',1,0,'engineering',?,'us')""",
            (f"j{i}", "Software Engineer", f"{long_url}/{i}", lvl),
        )
        add_event(conn, f"j{i}", title="Software Engineer", url=f"{long_url}/{i}")
    notify.set_watermark(conn, 0)
    d = notify.build(conn)
    d.flooded = False
    payload = notify.render(d)
    assert len(payload["embeds"]) == 2
    for e in payload["embeds"]:
        assert "https://" in e["description"], f"{e['title']} got no lines at all"


def test_rows_are_newest_first(conn):
    seed(conn, 3)
    notify.set_watermark(conn, 0)
    d = notify.build(conn)
    assert [r["id"] for r in d.rows] == [3, 2, 1]
    assert d.to_id == 3, "watermark still takes the maximum, not the first row"


def test_a_204_with_no_body_counts_as_delivered(monkeypatch):
    """Discord answers 204 No Content. post_json would call r.json() on that
    and raise, so every successful delivery would look like a failure --
    watermark stuck, same jobs resent every hour, forever. The one path
    --dry-run cannot reach, so it is pinned here."""
    from unittest.mock import MagicMock, patch

    from argus.core import http

    r = MagicMock(status_code=204, text="")
    r.json.side_effect = ValueError("no body")
    with patch.object(http.session(), "post", return_value=r):
        assert http.post_nobody("https://discord.com/api/webhooks/x", json={}) == 204


def test_a_webhook_error_is_still_an_error(monkeypatch):
    from unittest.mock import MagicMock, patch

    from argus.core import http
    from argus.core.models import FetchError

    r = MagicMock(status_code=400, text="invalid embed")
    with (
        patch.object(http.session(), "post", return_value=r),
        pytest.raises(FetchError, match="400"),
    ):
        http.post_nobody("https://discord.com/api/webhooks/x", json={})


def test_rate_limit_waits_then_succeeds(monkeypatch):
    from unittest.mock import MagicMock, patch

    from argus.core import http

    limited = MagicMock(status_code=429, text="", headers={"Retry-After": "0.01"})
    ok = MagicMock(status_code=204, text="", headers={})
    monkeypatch.setattr("time.sleep", lambda _s: None)
    with patch.object(http.session(), "post", side_effect=[limited, ok]) as m:
        assert http.post_nobody("https://discord.com/api/webhooks/x", json={}) == 204
    assert m.call_count == 2


def test_prune_drops_only_expired_dedupe_rows(conn):
    fresh, stale = notify.now(), notify.now() - notify.DEDUPE_WINDOW - 1
    conn.executemany(
        """INSERT INTO notified_jobs (ats, slug, external_id, notified_at)
           VALUES ('ashby','acme',?,?)""",
        [("keep", fresh), ("drop", stale)],
    )
    notify.prune(conn)
    left = [r["external_id"] for r in conn.execute("SELECT external_id FROM notified_jobs")]
    assert left == ["keep"]


"""
The two groups. The digest exists to answer one question -- what opened
today that I could apply to -- so what it leaves out matters as much as what
it includes.
"""


def test_only_new_grad_and_intern_are_announced(conn):
    """A senior role is a real posting and belongs in the corpus. It is not
    what this notification is for."""
    for i, lvl in enumerate(["new_grad", "intern", "senior", "staff", "manager", None]):
        add_job(conn, f"j{i}", seniority=lvl)
        add_event(conn, f"j{i}")
    notify.set_watermark(conn, 0)
    d = notify.build(conn)
    assert sorted(r["seniority"] for r in d.rows) == ["intern", "new_grad"]


def test_only_us_and_remote_are_announced(conn):
    for i, reg in enumerate(["us", "remote", "europe", "unknown", "other"]):
        add_job(conn, f"j{i}", region=reg)
        add_event(conn, f"j{i}")
    notify.set_watermark(conn, 0)
    d = notify.build(conn)
    assert sorted(r["region"] for r in d.rows) == ["remote", "us"]


def test_unknown_region_is_excluded_though_it_is_stored(conn):
    """Ingest keeps unknown-region rows because an unparseable location is
    more likely worth a look than in the wrong hemisphere. A push
    notification has the opposite economics -- 12,978 of them would drown
    the two groups this exists for."""
    add_job(conn, "j0", region="unknown")
    add_event(conn, "j0")
    notify.set_watermark(conn, 0)
    assert notify.build(conn).rows == []


def test_the_two_groups_are_separate_and_labelled(conn):
    add_job(conn, "a", seniority="new_grad")
    add_event(conn, "a")
    add_job(conn, "b", seniority="intern")
    add_event(conn, "b")
    notify.set_watermark(conn, 0)
    payload = notify.render(notify.build(conn))
    titles = [e["title"] for e in payload["embeds"]]
    assert titles == ["New Grad · 1", "Internship · 1"]
    colors = {e["color"] for e in payload["embeds"]}
    assert len(colors) == 2, "the two sections must be distinguishable at a glance"


def test_group_order_is_fixed_not_by_size(conn):
    """The reader looks for the same section in the same place every day. A
    section that moves because it happened to be smaller is worse than one
    that is sometimes short."""
    for i in range(5):
        add_job(conn, f"i{i}", seniority="intern")
        add_event(conn, f"i{i}")
    add_job(conn, "g0", seniority="new_grad")
    add_event(conn, "g0")
    notify.set_watermark(conn, 0)
    labels = [lbl for lbl, _ in notify.build(conn).grouped()]
    assert labels == ["New Grad", "Internship"], "GROUPS order, not row counts"


def test_a_group_with_nothing_new_is_omitted_entirely(conn):
    """An empty section every day trains the reader to skip the message."""
    add_job(conn, "g0", seniority="new_grad")
    add_event(conn, "g0")
    notify.set_watermark(conn, 0)
    payload = notify.render(notify.build(conn))
    assert [e["title"] for e in payload["embeds"]] == ["New Grad · 1"]


def test_nothing_matching_posts_nothing(conn):
    """Silence beats an hourly 'no new grad roles'."""
    add_job(conn, "j0", seniority="senior")
    add_event(conn, "j0")
    notify.set_watermark(conn, 0)
    assert notify.render(notify.build(conn)) is None


def test_the_heading_names_both_counts_and_the_region_scope(conn):
    add_job(conn, "a", seniority="new_grad")
    add_event(conn, "a")
    add_job(conn, "b", seniority="intern")
    add_event(conn, "b")
    notify.set_watermark(conn, 0)
    head = notify.render(notify.build(conn))["content"]
    assert "1 new grad" in head and "1 internship" in head
    assert "US & remote" in head


def test_the_query_filter_is_generated_from_the_groups(conn):
    """The filter and the labels cannot be edited apart."""
    for _, level, regions in notify.GROUPS:
        assert f"'{level}'" in notify.SELECT_PENDING
        for r in regions:
            assert f"'{r}'" in notify.SELECT_PENDING
    assert "__LEVELS__" not in notify.SELECT_PENDING


def test_the_flood_summary_names_the_actual_filter(conn, monkeypatch):
    """It said "engineering roles", which the digest no longer carries. A
    summary that describes itself as something broader than it is sends the
    reader looking for postings it was never going to hold."""
    monkeypatch.setattr(notify, "FLOOD_THRESHOLD", 1)
    for i in range(3):
        add_job(conn, f"j{i}")
        add_event(conn, f"j{i}")
    notify.set_watermark(conn, 0)
    content = notify.render(notify.build(conn))["content"]
    assert "new grad" in content and "internship" in content
    assert "US & remote" in content
    assert "engineering roles" not in content


def test_the_flood_threshold_clears_an_ordinary_catch_up(conn):
    """Measured: 1,108 matching roles in 24h, so a two-hourly run sees ~90.
    The old 200 fired on a run of 208, which was a missed poll rather than a
    backfill."""
    assert notify.FLOOD_THRESHOLD >= 500
