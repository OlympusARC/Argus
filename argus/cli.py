"""Command line entry point:  python -m argus <command>"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import adapters, discovery
from .core import config, db
from .feed import jobs, reconcile
from .registry import boards as registry
from .registry import careers as careerpage
from .registry import companies
from .registry import validate as validate_mod


def _fmt(n) -> str:
    return "-" if n is None else f"{n:,}"


"""
---------------------------------------------------------------- init -----
"""


def cmd_init(args) -> int:
    conn = db.init_db()
    """
    Same trap as the version check: sqlite_master is not a thing on Postgres.
    """
    listing = (
        "SELECT tablename AS name FROM pg_tables WHERE schemaname='public' ORDER BY name"
        if db.is_postgres()
        else "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [r["name"] for r in conn.execute(listing)]
    print(f"initialized {config.DB_PATH}")
    print("tables:", ", ".join(t for t in tables if not t.startswith("sqlite_")))
    return 0


"""
------------------------------------------------------------ discover -----
"""


def cmd_discover(args) -> int:
    """Print what discovery.run does. The sweep itself lives in the module so
    an orchestrator node can call it too; this is only the reporting."""
    conn = db.init_db()
    grand = {"boards": 0, "postings": 0, "companies": 0}

    def report(r: discovery.SourceResult) -> None:
        if r.skipped:
            print(f"~ {r.source:12} skipped -- {r.skipped}", flush=True)
            return
        if r.error:
            print(f"! {r.source:12} failed -- {r.error}", file=sys.stderr)
        if r.dry:
            print(
                f"  {r.source:12} {r.dry_refs:>7} refs  {r.dry_unique:>7} unique  "
                f"{r.dry_companies:>7} companies  {r.dry_postings:>7} postings  "
                f"({r.duration_s:.1f}s)  [dry run]",
                flush=True,
            )
            return
        if r.funding:
            print(f"  {r.source:12} {r.funding:>7} funding events recorded", flush=True)
        grand["boards"] += r.new_boards
        grand["postings"] += r.postings
        grand["companies"] += r.new_companies
        print(
            f"  {r.source:12} {r.refs_seen:>7} refs  {r.new_boards:>7} new boards  "
            f"{r.new_companies:>7} new companies  {r.postings:>7} seed postings  "
            f"({r.duration_s:.1f}s)",
            flush=True,
        )

    discovery.run(
        conn,
        args.source or None,
        dry_run=args.dry_run,
        batch=args.batch,
        limit=args.limit,
        kwargs_for={"commoncrawl": {"crawls": args.crawls}},
        on_result=report,
    )
    if not args.dry_run:
        print(
            f"\ntotal: {_fmt(grand['boards'])} new boards, "
            f"{_fmt(grand['companies'])} new companies, "
            f"{_fmt(grand['postings'])} seed postings"
        )
    return 0


"""
------------------------------------------------------------ validate -----
"""


def cmd_validate(args) -> int:
    conn = db.connect()
    t0 = time.time()
    res = validate_mod.run(
        conn, args.ats, limit=args.limit, workers=args.workers, revalidate=args.revalidate_dead
    )
    if not res.checked:
        print("nothing to validate")
        return 0
    print(f"{res.line()}  in {time.time() - t0:.0f}s")
    live = res.active + res.empty
    print(f"  -> {live:,} live boards ({live / res.checked:.0%} of probed)")
    return 0


"""
---------------------------------------------------------------- poll -----
"""


def cmd_poll(args) -> int:
    conn = db.connect()
    summary = reconcile.run(
        conn, args.ats, limit=args.limit, workers=args.workers, force=args.force
    )
    if not summary.boards:
        print("no boards due -- pass --force to poll regardless of schedule")
        return 0
    print(summary.line())
    if summary.suspicious:
        print(
            f"\n!! {len(summary.suspicious)} board(s) returned empty while holding many "
            f"open jobs; their closes were skipped:"
        )
        for slug in summary.suspicious[:10]:
            print(f"     {args.ats}:{slug}")
    return 0


def cmd_events(args) -> int:
    conn = db.connect()
    q = """SELECT e.ts, e.type, e.ats, e.slug, e.title, e.url, e.detail_json,
                  (SELECT company_name FROM boards b
                   WHERE b.ats=e.ats AND b.slug=e.slug) AS company
           FROM events e"""
    params: list = []
    where = []
    if args.type:
        where.append("e.type = ?")
        params.append(args.type)
    if args.since:
        where.append("e.ts >= ?")
        params.append(int(time.time()) - args.since * 3600)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += f" ORDER BY e.ts DESC LIMIT {int(args.limit)}"
    rows = conn.execute(q, params).fetchall()
    if not rows:
        print("no events yet -- run `argus poll`")
        return 0
    for r in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
        loc = ""
        if r["detail_json"]:
            try:
                loc = (json.loads(r["detail_json"]) or {}).get("location") or ""
            except ValueError:
                loc = ""
        """
        Funding events are not board-scoped, so ats/slug are null there.
        """
        who = r["company"] or r["slug"] or "-"
        what = r["title"] or "?"
        if r["type"] == "funding":
            who, what = "SEC Form D", f"{what}  (raised)"
        print(f"{when}  {r['type']:<8} {who[:20]:<21} {what[:42]:<43} {loc[:22]}")
    return 0


"""
----------------------------------------------------------------- llm -----
"""


def cmd_llm(args) -> int:
    from . import llm

    print("providers, in order:")
    print(llm.describe())
    if args.check:
        ok, why = llm.health()
        print(f"\nlive check: {'ok' if ok else 'FAILED'} -- {why}")
        return 0 if ok else 1
    return 0


def cmd_mine(args) -> int:
    from .agents import classifier

    conn = db.connect()
    t0 = time.time()
    res = classifier.run(conn, sample=args.sample, max_calls=args.max_calls, seed=args.seed)
    if res.get("skipped"):
        print(f"skipped -- {res['skipped']}")
        return 0
    print(
        f"sampled {res['sampled']:,} unplaced titles, labelled {res['labelled']:,}, "
        f"mined {res['patterns']} patterns in {time.time() - t0:.0f}s "
        f"({res['llm_calls']} llm calls)"
    )
    if res.get("failures"):
        want = -(-res["sampled"] // classifier.BATCH)
        print(f"  {len(res['failures'])} of {want} batches did not answer:")
        for why in dict.fromkeys(res["failures"]):
            print(f"    {why}")
    print(f"  filed {res['filed']} proposals: {res['proposal_ids']}")
    print(
        f"  of the labelled sample, {res['non_software_engineering']:,} were "
        f"engineering but NOT software"
    )
    print("\n`argus proposals` to review what the gate decided")
    return 0


def cmd_heal(args) -> int:
    from .agents import healer
    from .obs import runs as obs_runs

    conn = db.connect()
    targets = [args.source] if args.source else [t.source for t in obs_runs.collapsed(conn)]
    if not targets:
        print("nothing has collapsed")
        return 0
    for name in targets:
        res = healer.run(conn, name)
        if res.get("skipped"):
            print(f"{name}: skipped -- {res['skipped']}")
            continue
        print(f"{name}: {res['hypotheses']} hypotheses, proposal #{res['proposal']}")
        print(f"  most likely: {res['most_likely']}")
    return 0


def cmd_prospect(args) -> int:
    from .agents import prospector

    conn = db.connect()
    res = prospector.run(conn)
    if res.get("skipped"):
        print(f"skipped -- {res['skipped']}")
        return 0
    print(
        f"measured {res['tried']} candidates, filed {res['filed']} ({res['llm_calls']} calls)"
    )
    for c in res["candidates"]:
        mark = "+" if c["new"] else " "
        print(f"  {mark} {c['new']:>5} new / {c['found']:>5} found  {c['url'][:70]}")
    return 0


"""
----------------------------------------------------------- proposals -----
"""


def cmd_proposals(args) -> int:
    import json as _json

    from . import proposals

    conn = db.connect()

    if args.accept:
        proposals.apply(conn, args.accept, by="human")
        print(f"applied proposal {args.accept}")
        return 0
    if args.reject:
        proposals.reject(conn, args.reject)
        print(f"rejected proposal {args.reject}")
        return 0
    if args.gate:
        status = proposals.gate(conn, args.gate)
        p = proposals.get(conn, args.gate)
        print(f"proposal {args.gate}: {status}")
        print(f"  evidence: {_json.dumps(p['evidence'])[:400]}")
        return 0

    rows = proposals.by_status(conn, args.status, limit=args.limit)
    if not rows:
        print(f"no {args.status} proposals")
        return 0
    for p in rows:
        head = f"#{p['id']:<5} {p['agent']:<12} {p['kind']:<14} {p['status']}"
        if p["score"] is not None:
            head += f"  score={p['score']:.3g}"
        print(head)
        print(f"       {_json.dumps(p['payload'])[:120]}")
        if p["evidence"]:
            print(f"       evidence: {_json.dumps(p['evidence'])[:160]}")
    return 0


"""
-------------------------------------------------- orchestrate (Lane B) ---
"""


def cmd_orchestrate(args) -> int:
    from . import orchestrator

    conn = db.connect()
    budget_s = int(args.budget * 60)

    if args.dry_run:
        snap, lines, first = orchestrator.plan(conn, budget_s)
        print("state:")
        print(orchestrator.measure.render(snap))
        print("\npolicy:")
        for ln in lines:
            print(ln)
        print(f"\nwould start with: {first}")
        return 0

    ck = None
    if not args.no_checkpoint:
        url = config.database_url()
        try:
            ck = (
                orchestrator.postgres_checkpointer(url)
                if url
                else orchestrator.sqlite_checkpointer(str(config.DB_PATH) + ".orch")
            )
        except Exception as exc:
            """
            A checkpointer that will not open costs resumability, not the
            run. Losing tonight's work because the memory failed would be a
            worse trade than running without it.
            """
            print(f"~ checkpointing disabled -- {type(exc).__name__}: {exc}", file=sys.stderr)

    t0 = time.time()
    res = orchestrator.orchestrate(
        conn, budget_s=budget_s, thread_id=args.thread, checkpointer=ck
    )
    print(
        f"{len(res['done'])} task(s) in {time.time() - t0:.0f}s "
        f"({res['spent_s']}s of {res['budget_s']}s budget, {res['steps']} steps)"
    )
    for d in res["done"]:
        if d.get("skipped"):
            print(f"  ~ {d['task']:<10} skipped -- {d['skipped']}")
        else:
            detail = ", ".join(
                f"{k}={v}" for k, v in d.items() if k not in ("task", "spent_s") and v
            )
            print(f"  - {d['task']:<10} {d['spent_s']:>5}s  {detail}")
    return 0


"""
-------------------------------------------------------------- health -----
"""


def _ago(ts: int | None) -> str:
    if not ts:
        return "never"
    d = int(time.time()) - int(ts)
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


def cmd_health(args) -> int:
    from .obs import runs as obs_runs

    conn = db.connect()
    trends = obs_runs.all_trends(conn)
    if not trends:
        print("no source runs recorded yet -- run `argus discover`")
        return 0

    print(f"{'source':<18}{'last run':>10}{'refs':>9}{'new':>8}{'blocked':>9}  trend")
    for t in sorted(trends, key=lambda x: (not x.collapsed, x.source)):
        last = obs_runs.latest(conn, t.source) or {}
        note = ""
        if t.collapsed:
            note = f"  <- {t.reason}"
        elif last.get("skipped"):
            note = f"  ({last['skipped']})"
        elif last.get("error"):
            note = f"  ({str(last['error'])[:40]})"
        print(
            f"{t.source:<18}{_ago(last.get('started_at')):>10}"
            f"{_fmt(last.get('refs_seen') or 0):>9}{_fmt(last.get('new_boards') or 0):>8}"
            f"{last.get('blocked') or 0:>9}  {t.arrow}{note}"
        )

    bad = [t for t in trends if t.collapsed]
    if bad:
        print(f"\n{len(bad)} source{'s' if len(bad) != 1 else ''} collapsed:")
        for t in bad:
            print(f"  {t.source:<18}{t.reason}")
        return 1
    return 0


"""
-------------------------------------------------------------- notify -----
"""


def cmd_notify(args) -> int:
    from .feed import notify

    conn = db.connect()
    d = notify.build(conn, since_id=args.since_id)

    if args.dry_run or args.since_id is not None:
        print(notify.render_text(d))
        if args.since_id is not None:
            print("\n(--since-id is a preview: the watermark was not moved)")
        return 0

    url = config.DISCORD_WEBHOOK
    if not url:
        """
        Not an error. A fresh clone has no webhook and poll.yml calls this
        unconditionally -- failing here would fail the hourly run over a
        missing optional secret.
        """
        print("no webhook configured (set ARGUS_DISCORD_WEBHOOK); nothing sent")
        print(notify.render_text(d))
        return 0

    d = notify.run(conn, url=url)
    if d.flooded:
        print(f"flood guard: {d.pending:,} pending, sent a summary instead")
    elif d.sending:
        print(f"sent {d.sending} job{'s' if d.sending != 1 else ''} ({d.fde} fde)")
    else:
        print("nothing new")
    print(f"watermark now {d.to_id}")
    return 0


"""
------------------------------------------------------------- careers -----
"""


def cmd_careers(args) -> int:
    conn = db.init_db()
    t0 = time.time()
    if args.recheck:
        rep = careerpage.recheck(
            conn,
            ats=args.ats,
            limit=args.limit,
            workers=args.workers,
            older_than_days=args.older_than,
        )
        if not rep.checked:
            print("nothing due for recheck")
            return 0
        print(f"rechecked {rep.checked:,} careers pages in {time.time() - t0:.0f}s")
        print(f"  still on the same board : {rep.confirmed:,}")
        print(f"  MIGRATED to another ATS : {rep.migrated:,}")
        print(f"  no ATS link found now   : {rep.gone:,}")
    else:
        rep = careerpage.backfill(
            conn, ats=args.ats, limit=args.limit, workers=args.workers, max_guesses=args.guesses
        )
        if not rep.checked:
            print("every board already has a careers page")
            return 0
        print(f"probed {rep.checked:,} boards in {time.time() - t0:.0f}s")
        print(f"  careers page found: {rep.found:,} ({rep.found / rep.checked:.0%})")
    if rep.new_boards:
        res = registry.add_boards(conn, rep.new_boards, source="careers")
        print(f"  additional boards discovered on those pages: {res['new_boards']:,}")
    return 0


"""
----------------------------------------------------------- companies -----
"""


"""
------------------------------------------------------------- classify -----
"""


def cmd_classify(args) -> int:
    conn = db.init_db()
    t0 = time.time()
    res = jobs.reclassify(conn)
    if res["classified"]:
        print(
            f"classified {res['classified']:,} postings in {time.time() - t0:.0f}s "
            f"({res['family_changed']:,} changed family)"
        )
    else:
        print("every posting already carries the current ruleset")

    rows = conn.execute(
        """SELECT role_family, COUNT(*) n, SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open_now
           FROM jobs GROUP BY 1 ORDER BY n DESC"""
    ).fetchall()
    if rows:
        total = sum(r["n"] for r in rows) or 1
        print(f"\n{'family':<14}{'postings':>10}{'share':>8}{'open':>10}")
        for r in rows:
            print(
                f"{r['role_family'] or '-':<14}{_fmt(r['n']):>10}"
                f"{r['n'] / total:>7.1%}{_fmt(r['open_now']):>10}"
            )
    return 0


def cmd_companies(args) -> int:
    conn = db.init_db()
    if args.adopt:
        res = companies.adopt_boards(conn)
        print(f"adopted {res['linked']:,} boards -> {res['companies']:,} new companies")
    if args.resolve:
        t0 = time.time()
        rep = companies.resolve(
            conn,
            limit=args.limit,
            workers=args.workers,
            max_guesses=args.guesses,
            guess_unknown=not args.known_domains_only,
        )
        if not rep.checked:
            print("nothing left to resolve")
        else:
            print(f"{rep.line()}  in {time.time() - t0:.0f}s")
            if rep.new_boards:
                out = registry.add_boards(conn, rep.new_boards, source="companies")
                print(f"  boards found on those pages: {out['new_boards']:,} new")
    st = companies.stats(conn)
    total = st["total"] or 1
    print("\ncompanies")
    print(f"   total              {_fmt(st['total']):>9}")
    print(f"   with a domain      {_fmt(st['domained']):>9}  ({st['domained'] / total:.0%})")
    print(f"   with careers page  {_fmt(st['careers']):>9}  ({st['careers'] / total:.0%})")
    print("\ncareers page probe outcome")
    for label, key in (
        ("links to an ATS", "ats"),
        ("page, no known ATS", "html"),
        ("nothing found", "none_"),
        ("not probed yet", "unprobed"),
    ):
        print(f"   {label:<20} {_fmt(st[key] or 0):>9}")
    if st["unguessable"]:
        print(
            f"   {'(no domain guessable':<20} {_fmt(st['unguessable']):>9}   "
            f"from the name alone -- never queued)"
        )

    """
    careers_kind describes the PAGE; monitoring describes the BOARD. A
    company can probe as 'html' and still be polled hourly, because another
    source found its board -- so coverage is reported separately.
    """
    print("\nmonitoring")
    print(
        f"   monitored by a live board {_fmt(st['monitored']):>9}  "
        f"({st['monitored'] / total:.0%})"
    )
    print(
        f"   not monitored             {_fmt(st['unmonitored']):>9}  "
        f"({st['unmonitored'] / total:.0%})"
    )
    print(
        f"     of which we hold a careers page "
        f"no adapter can read {_fmt(st['page_but_unmonitored']):>7}"
    )
    print(f"\n   boards linked to a company {_fmt(st['boards_linked']):>9}")
    if args.list:
        print("\nsample")
        rows = conn.execute(
            """SELECT c.name, c.domain, c.careers_url, c.careers_kind,
                      (SELECT COUNT(*) FROM boards b WHERE b.company_id=c.id) boards
               FROM companies c WHERE c.careers_url IS NOT NULL
               ORDER BY boards DESC, c.id LIMIT ?""",
            (args.list,),
        ).fetchall()
        for r in rows:
            print(
                f"   {(r['name'] or '-')[:26]:<27} {(r['domain'] or '-')[:24]:<25} "
                f"{(r['careers_kind'] or '-'):<6} {r['boards']:>2} boards  "
                f"{(r['careers_url'] or '')[:44]}"
            )
    return 0


"""
--------------------------------------------------------------- stats -----
"""


def cmd_stats(args) -> int:
    conn = db.connect()
    print("boards by ats/status")
    pollable = set(adapters.supported())
    for r in registry.counts_by_ats(conn):
        mark = " " if r["ats"] in pollable else "*"
        print(f"  {mark}{r['ats']:<16} {r['status']:<12} {_fmt(r['n']):>9}")
    print("  (* = recognized and stored, no adapter yet -- not polled)")

    rows = registry.counts_by_source(conn)
    if rows:
        print("\nboards by discovery source")
        for r in rows:
            print(
                f"   {r['source']:<16} total {_fmt(r['boards']):>8}   "
                f"active {_fmt(r['active']):>8}   dead {_fmt(r['dead']):>8}"
            )

    j = conn.execute(
        """SELECT status, COUNT(*) n, COUNT(DISTINCT ats||'/'||slug) boards
           FROM jobs GROUP BY status"""
    ).fetchall()
    if j:
        print("\njobs")
        for r in j:
            print(f"   {r['status']:<16} {_fmt(r['n']):>9} across {_fmt(r['boards'])} boards")
    st = companies.stats(conn)
    if st["total"]:
        t = st["total"]
        print("\ncompanies")
        print(f"   known              {_fmt(t):>9}")
        print(f"   with a domain      {_fmt(st['domained']):>9}  ({st['domained'] / t:.0%})")
        print(f"   with careers page  {_fmt(st['careers']):>9}  ({st['careers'] / t:.0%})")
        print(
            f"   monitored          {_fmt(st['monitored']):>9}  "
            f"({st['monitored'] / t:.0%})  a live board polls them"
        )
        print(
            f"   not monitored      {_fmt(st['unmonitored']):>9}  ({st['unmonitored'] / t:.0%})"
        )
        print(
            f"     ..with a careers page we cannot read {_fmt(st['page_but_unmonitored']):>7}"
        )

    cov = conn.execute(
        """SELECT COUNT(*) total,
                  COUNT(company_name) named,
                  COUNT(website)      sited,
                  COUNT(careers_url)  careers
           FROM boards"""
    ).fetchone()
    if cov and cov["total"]:
        t = cov["total"]
        print("\nmetadata coverage")
        for label, n in (
            ("company name", cov["named"]),
            ("website", cov["sited"]),
            ("careers page", cov["careers"]),
        ):
            n = n or 0
            print(f"   {label:<16} {_fmt(n):>9} / {_fmt(t)}  ({n / t:.0%})")

    ev = conn.execute(
        "SELECT type, COUNT(*) n FROM events GROUP BY type ORDER BY n DESC"
    ).fetchall()
    if ev:
        print("\nevents")
        for r in ev:
            print(f"   {r['type']:<16} {_fmt(r['n']):>9}")
    return 0


"""
-------------------------------------------------------------- sources ----
"""


def cmd_sources(args) -> int:
    rows = [(n, False) for n in discovery.DEFAULT_ORDER] + [(n, True) for n in discovery.OPT_IN]
    print(f"{'source':<18}{'auth':<7}{'default':<9}status")
    for name, opt_in in rows:
        src = discovery.build(name)
        ok, why = src.available()
        print(
            f"{name:<18}{'yes' if src.needs_auth else 'no':<7}"
            f"{'opt-in' if opt_in else 'yes':<9}"
            f"{'ready' if ok else 'unavailable -- ' + why}"
        )
    print(f"\npollable ATSs: {', '.join(adapters.supported())}")
    print(f"stored-only  : {', '.join(adapters.PLANNED)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="argus", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database").set_defaults(fn=cmd_init)

    d = sub.add_parser("discover", help="fill the board registry from all sources")
    d.add_argument(
        "-s",
        "--source",
        action="append",
        choices=list(discovery.SOURCES),
        help="run only this source (repeatable)",
    )
    d.add_argument(
        "--crawls",
        type=int,
        default=12,
        help="how many Common Crawl collections to sweep (default 4)",
    )
    d.add_argument("--limit", type=int, help="stop each source after N refs")
    d.add_argument(
        "--batch",
        type=int,
        default=500,
        help="write to the database every N refs (default 500)",
    )
    d.add_argument("--dry-run", action="store_true", help="count without writing")
    d.set_defaults(fn=cmd_discover)

    v = sub.add_parser("validate", help="probe unvalidated boards, settle their status")
    v.add_argument("--ats", default="ashby", help="which ATS to probe (default ashby)")
    v.add_argument("--limit", type=int, help="probe at most N boards")
    v.add_argument("--workers", type=int, help=f"concurrency (default {config.WORKERS})")
    v.add_argument(
        "--revalidate-dead",
        action="store_true",
        help="re-probe boards previously marked dead (companies adopt Ashby later)",
    )
    v.set_defaults(fn=cmd_validate)

    pl = sub.add_parser("poll", help="reconcile due boards and emit job events")
    pl.add_argument("--ats", default="ashby")
    pl.add_argument("--limit", type=int, help="poll at most N boards")
    pl.add_argument("--workers", type=int)
    pl.add_argument(
        "--force", action="store_true", help="poll regardless of each board's schedule"
    )
    pl.set_defaults(fn=cmd_poll)

    lm = sub.add_parser("llm", help="which LLM providers are configured")
    lm.add_argument("--check", action="store_true", help="make one live round trip")
    lm.set_defaults(fn=cmd_llm)

    mn = sub.add_parser("mine", help="mine ruleset patterns from the unplaced title tail")
    mn.add_argument("--sample", type=int, default=2000, help="titles to sample (default 2000)")
    mn.add_argument("--max-calls", type=int, default=80, help="cap LLM calls for the run")
    mn.add_argument("--seed", type=int, default=0, help="sampling seed, for a repeatable run")
    mn.set_defaults(fn=cmd_mine)

    hl = sub.add_parser("heal", help="diagnose a source whose yield collapsed")
    hl.add_argument("source", nargs="?", help="source name (default: whatever collapsed)")
    hl.set_defaults(fn=cmd_heal)

    sub.add_parser(
        "prospect", help="propose new discovery sources, measured by what they yield"
    ).set_defaults(fn=cmd_prospect)

    pr = sub.add_parser(
        "proposals", help="what the agents suggested, and what the gates decided"
    )
    pr.add_argument(
        "--status", default="pending", help="drafted|pending|auto_applied|accepted|rejected"
    )
    pr.add_argument("--limit", type=int, default=25)
    pr.add_argument("--gate", type=int, metavar="ID", help="run the gate on one proposal")
    pr.add_argument("--accept", type=int, metavar="ID", help="apply a pending proposal")
    pr.add_argument("--reject", type=int, metavar="ID")
    pr.set_defaults(fn=cmd_proposals)

    orc = sub.add_parser(
        "orchestrate", help="decide and run the most valuable work within a budget"
    )
    orc.add_argument(
        "--budget",
        type=float,
        default=45,
        metavar="MIN",
        help="minutes of work to plan for (default 45, inside the 50-minute CI ceiling)",
    )
    orc.add_argument(
        "--dry-run",
        action="store_true",
        help="print the state, every policy rule, and what it would start with",
    )
    orc.add_argument("--thread", default=None, help="checkpoint thread id (default: today)")
    orc.add_argument("--no-checkpoint", action="store_true", help="run without resumability")
    orc.set_defaults(fn=cmd_orchestrate)

    sub.add_parser(
        "health", help="per-source yield, and which sources have collapsed"
    ).set_defaults(fn=cmd_health)

    nt = sub.add_parser("notify", help="post the hourly digest of new engineering roles")
    nt.add_argument(
        "--dry-run",
        action="store_true",
        help="render what would be posted; send nothing, move nothing",
    )
    nt.add_argument(
        "--since-id",
        type=int,
        default=None,
        metavar="N",
        help="preview the digest as if the watermark were N (implies --dry-run)",
    )
    nt.set_defaults(fn=cmd_notify)

    ev = sub.add_parser("events", help="recent new/edited/closed job events")
    ev.add_argument(
        "--type", choices=("new", "edited", "closed", "reopened", "migrated", "funding")
    )
    ev.add_argument("--since", type=int, metavar="HOURS", help="only the last N hours")
    ev.add_argument("--limit", type=int, default=40)
    ev.set_defaults(fn=cmd_events)

    cp = sub.add_parser("careers", help="find or re-check company careers pages")
    cp.add_argument(
        "--recheck",
        action="store_true",
        help="re-probe stored careers pages to detect ATS migrations",
    )
    cp.add_argument("--ats", help="restrict to one ATS")
    cp.add_argument("--limit", type=int)
    cp.add_argument("--workers", type=int)
    cp.add_argument(
        "--guesses", type=int, default=3, help="TLDs to try per candidate name (default 3)"
    )
    cp.add_argument(
        "--older-than",
        type=int,
        default=7,
        help="recheck pages not verified in N days (default 7)",
    )
    cp.set_defaults(fn=cmd_careers)

    cm = sub.add_parser("companies", help="the company registry: who we watch and where")
    cm.add_argument(
        "--adopt", action="store_true", help="create a company for every board that names one"
    )
    cm.add_argument(
        "--resolve",
        action="store_true",
        help="probe for careers pages, learning domains as it goes",
    )
    cm.add_argument("--limit", type=int, help="resolve at most N companies")
    cm.add_argument("--workers", type=int)
    cm.add_argument(
        "--guesses",
        type=int,
        default=3,
        help="TLDs to try per name when the domain is unknown (default 3)",
    )
    cm.add_argument(
        "--known-domains-only",
        action="store_true",
        help="skip companies whose domain would have to be guessed",
    )
    cm.add_argument("--list", type=int, metavar="N", help="print N resolved companies")
    cm.set_defaults(fn=cmd_companies)

    cl = sub.add_parser("classify", help="apply the role ruleset to postings that predate it")
    cl.set_defaults(fn=cmd_classify)

    sub.add_parser("stats", help="registry and feed summary").set_defaults(fn=cmd_stats)
    sub.add_parser("sources", help="list discovery sources and readiness").set_defaults(
        fn=cmd_sources
    )

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
