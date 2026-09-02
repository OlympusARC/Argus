# Argus

A job aggregator built on three tiers that never merge: **companies** (who is
hiring), **boards** (how we currently reach them), and a **feed** of postings.

Each tier outlives the one below it. A posting dies when the role is filled; a
board slug dies the day a company switches ATS; the company and its careers page
survive both. So the company is what we track, and the board is only the current
means of doing it.

```
              daily                 daily                 hourly
  sources ──▶ companies ──▶ resolve careers ──▶ boards ──▶ poll ──▶ jobs ──▶ events
              domain          careers_url       ats+slug         ats+slug+external_id
                  ▲                                  │
                  └──────── a board that names its employer ──────┘
```

Nothing writes upward. Discovery fills companies and boards; only the reconciler
writes postings. That is what lets a noisy discovery source be harmless, and an
ATS be added without re-running discovery.

## Why discovery is hard

No ATS publishes an enumeration endpoint. `jobs.ashbyhq.com/sitemap.xml` returns
the SPA shell and `robots.txt` declares nothing, so there is no way to ask "which
companies use Ashby?" — every board must be *inferred* from third-party traces.
Completeness is unprovable; the goal is convergence, measured by the marginal
new-board yield of each run.

What makes it tractable: an unknown slug returns a clean `404` and a real one
returns the entire board in a single unauthenticated `GET`. Guessing is cheap, so
discovery sources are tuned for **recall, not precision** — `validate` is the one
place that decides what is real.

## Why companies are the spine

Almost everything that names an employer on the public web does *not* link its
ATS. Job-list repos route apply links through their own domain, funding filings
name a company and nothing else, VC portfolio pages list names with homepages.
Those are not failed board discoveries — they are probe targets:

```
name ──▶ guess <name>.com/.io/.ai ──▶ fetch /careers ──▶ ATS link? ──▶ board
```

A guessed domain is a claim, not a fact, so it is kept only when the page proves
it belongs to that company: it carries a board we already tied to them, or a slug
matching their name. Without that check a company called Alan happily adopts some
unrelated `alan.com` as its careers page.

## Quick start

```bash
make install                            # venv + editable install
./.venv/bin/argus init                  # create the database
./.venv/bin/argus sources               # which sources are ready
./.venv/bin/argus discover              # fill companies and boards
./.venv/bin/argus companies --resolve   # find their careers pages
./.venv/bin/argus validate              # probe boards, settle status
./.venv/bin/argus poll                  # reconcile, emit job events
./.venv/bin/argus stats                 # what we have
```

`companies --resolve` is the slow one: it fetches up to three candidate domains
per company and most of them miss. It is capped per run and scheduled daily
rather than swept, so the corpus fills in steadily instead of hammering thousands
of unrelated domains at once.

## Layout

```
argus/
  cli.py           command line surface
  core/            settings, storage, HTTP, data shapes, URL routing, name norms
  adapters/        one per ATS -- fetch a board, return Postings
  discovery/       one per source -- yield BoardRefs and CompanyRefs, nothing else
  registry/        who exists (companies), which boards are real, careers pages
  feed/            postings and the events emitted as they change
api/               FastAPI read surface
supabase/          SQL migrations, applied on merge to main
seeds/             hand-written slug lists (source, tracked)
docs/              architecture and the GitHub job-repo survey
tests/
```

The dependency direction is one-way: `core` knows nothing about the layers above
it, `discovery` only ever writes to `registry`, and only the reconciler writes to
`feed`.

## Operations

Scheduling is GitHub Actions, not an in-process scheduler — schedules live in
version-controlled YAML, each run is isolated, and failures are visible without
building anything. The pipeline's only runtime dependency is `requests`.

| workflow | cadence | does |
|---|---|---|
| `discover.yml` | daily | `discover`, then `companies --resolve` |
| `poll.yml` | hourly | `poll` across every due board |

## Notes that are easy to get wrong

- **ATS slugs are case-insensitive.** `ramp`, `Ramp` and `RAMP` are one board.
  Slugs are lowercased on parse; skipping this creates duplicate rows that poll
  the same board.
- **HN escapes URLs as HTML entities** (`&#x2F;` for `/`), which silently defeats
  any URL regex. `urls.extract_all` unescapes first — pinned by a test.
- **`content_hash` excludes server timestamps.** Greenhouse bumps `updated_at` on
  no-op republishes; hashing it would mark every job edited on every poll.
- **Rediscovery never resurrects a dead board**, or Common Crawl would revive the
  same dead slugs every month. Only `validate --revalidate-dead` does.
- **An ATS host is never a company domain.** Storing `boards.greenhouse.io` as a
  company's website merges every company on that ATS into one row.
- **A company with two boards is a migration, not two companies.** Identity is the
  domain, so a dead Greenhouse slug and a live Ashby one point at the same row.
- **An acquired company's careers page serves the acquirer's board.**
  `visly.app/careers` returns Figma's Greenhouse board, so any source probing an
  arbitrary domain must check the domain looks like the board it found before
  attributing it.

## Documentation

- [docs/architecture.md](docs/architecture.md) — tables, sources, deployment, open work
- [docs/architecture.excalidraw](docs/architecture.excalidraw) — the whole system on one
  canvas. Drag it onto [excalidraw.com](https://excalidraw.com), or regenerate it with
  `python scripts/make_diagram.py > docs/architecture.excalidraw`
- [docs/github-job-repos.md](docs/github-job-repos.md) — the survey of 958 job-list
  repos, what each is worth, and the families that look valuable but are not
