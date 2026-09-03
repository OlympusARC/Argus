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
                  ▲                                  │                    │
                  └──── a board that names its employer ────┘             ▼
                                                                       digest
```

Nothing writes upward. Discovery fills companies and boards; only the reconciler
writes postings. That is what lets a noisy discovery source be harmless, and an
ATS be added without re-running discovery.

## Two lanes

The pipeline splits in two, and the split is the most important thing about it.

**Lane A is the feed.** Hourly, fixed, deliberately dumb: poll every due board,
diff it against what we stored, emit events, post a digest. It must produce job
alerts even if everything else is broken.

**Lane B is the brain.** Daily, budgeted, and it *decides*. Rather than "run
discover at 03:00 no matter what", it reads the measured state of the system and
picks the most valuable work available — validate a backlog, investigate a source
that stopped yielding, sweep a stale ruleset, or go looking for new sources.

```
cron ─┬─ hourly ──▶  LANE A   poll ──▶ notify              (no LangGraph, no LLM)
      │
      └─ daily ───▶  LANE B   measure ──▶ policy ──▶ act ──▶ record ──▶ ↺
                                              │
                                   discover · resolve · validate · classify
                                   heal · prospect          (LLM, gated)
```

Lane A is not a node in Lane B's graph, and a test fails if the feed ever imports
the LLM layer or the graph framework. A stuck brain, an exhausted quota or a
LangGraph bug can therefore never delay a job posting.

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

## Agents propose; code disposes

Lane B can use a model for the parts that are genuinely open-ended — finding new
sources, diagnosing a broken one, improving the classification rules. None of
them can write anything.

An agent's output is a row in `proposals`. A deterministic **gate** then measures
the claim rather than judging the reasoning: a prospector says a page lists job
boards, so the gate fetches it, extracts with the pipeline's own router, and
counts how many are new to the registry. Twenty-five or more applies itself, one
to twenty-four waits for a human, zero is a rejection with the evidence attached.

Two prohibitions are structural rather than procedural. A `diagnosis` has no gate,
so there is no way to say yes to it, and no applier, so it cannot be enacted at
all. Nothing touching the reconcile path has either — and a test asserts their
continued absence, which is a check nobody can invert.

## Quick start

```bash
make install                    # venv + editable install
argus init                      # create the database
argus sources                   # which discovery sources are ready
argus discover                  # fill companies and boards
argus companies --resolve       # find their careers pages
argus validate                  # probe boards, settle status
argus poll                      # reconcile, emit job events
argus stats                     # what we have
```

`companies --resolve` is the slow one: it fetches up to three candidate domains
per company and most of them miss. It is capped per run and scheduled daily
rather than swept, so the corpus fills in steadily instead of hammering thousands
of unrelated domains at once.

## Commands

| | |
|---|---|
| `init` | create or migrate the database |
| `discover` | sweep every ready source for boards and companies |
| `validate` | probe unvalidated boards, settle active or dead |
| `companies` | the registry; `--resolve` attaches careers pages |
| `careers` | find or re-check a company's careers page |
| `poll` | reconcile due boards and emit job events |
| `classify` | apply the current ruleset to postings that predate it |
| `notify` | post the hourly digest of new engineering roles |
| `events` | recent new / edited / closed job events |
| `stats` | registry and feed summary |
| `health` | per-source yield, and which sources have collapsed |
| `orchestrate` | decide and run the most valuable work within a budget |
| `proposals` | what the agents suggested, and what the gates decided |
| `llm` | which model providers are configured |
| `mine` | mine ruleset patterns from the unplaced title tail |
| `prospect` | propose new discovery sources, measured by what they yield |
| `heal` | diagnose a source whose yield collapsed |

Two are worth knowing before you run them. `argus orchestrate --dry-run` prints
the state, every policy rule and what it would start with, without doing any of
it. `argus notify --dry-run` renders the digest that would be posted and moves
nothing.

## Layout

```
argus/
  cli.py           command line surface, deliberately thin
  core/            settings, storage, HTTP, data shapes, URL routing, name norms
  adapters/        one per ATS -- fetch a board, return Postings
  discovery/       one per source -- yield BoardRefs and CompanyRefs, nothing else
  registry/        who exists (companies), which boards are real, careers pages
  classify/        the role ruleset: engineering, fde, ai, data, security, ...
  feed/            postings, the set-diff that writes them, and the digest
  obs/             what each source actually produced, run over run
  orchestrator/    measure, policy, nodes, graph -- Lane B's loop
  proposals/       where an agent's output lands, and the gates that judge it
  llm/             the provider chain; structured output or nothing
  agents/          classifier, prospector, healer -- all advisory
api/               FastAPI read surface
supabase/          SQL migrations for Postgres
scripts/           backfill: restore a corpus into Postgres
seeds/             hand-written slug lists (source, tracked)
tests/
```

The dependency direction is one-way: `core` knows nothing about the layers above
it, `discovery` only ever writes to `registry`, and only the reconciler writes to
`feed`.

## Running it

The pipeline's only runtime dependency is `requests`. Everything else is an
optional extra, so a runner that only polls installs neither the API nor the
graph framework:

```bash
pip install -e '.[postgres]'                 # the pipeline
pip install -e '.[postgres,orchestrator]'    # + Lane B
pip install -e '.[dev]'                      # + tests and lint
```

Scheduling is GitHub Actions rather than an in-process scheduler — schedules live
in version-controlled YAML, each run is isolated, and failures are visible without
building anything.

| workflow | cadence | does |
|---|---|---|
| `poll.yml` | hourly | `poll`, `classify`, then `notify` |
| `discover.yml` | daily | `discover`, then `companies --resolve` |
| `orchestrate.yml` | daily | Lane B, within a 45-minute budget |

> **The workflows are not committed yet.** They are held back deliberately until
> the pipeline has been exercised by hand against a fresh database. Nothing in
> this repo runs on a schedule until they land.

### Configuration

| variable | for |
|---|---|
| `SUPABASE_DB_PASSWORD` | Postgres, with the ref below. Either absent, everything falls back to local SQLite |
| `SUPABASE_REF` | which project. Not committed -- see below |
| `SUPABASE_REGION` | defaults to `us-west-2` |
| `ARGUS_DISCORD_WEBHOOK` | the hourly digest. Absent, `notify` prints and exits 0 |
| `ARGUS_STORE_FAMILIES` | which role families are stored at all (see below) |
| `ARGUS_STORE_REGIONS` | which regions are stored; `other` is the only rejection |
| `ARGUS_STORE_POSTED_AFTER` | the oldest posting worth storing, as an epoch |
| `ARGUS_AGE_EXEMPT_ATS` | sources that publish no date, so cannot be aged |
| `GROQ_API_KEY` / `NVIDIA_API_KEY` / `GEMINI_API_KEY` | the agents. Absent, they skip |
| `GITHUB_TOKEN`, `BRAVE_API_KEY`, `ARGUS_SEC_CONTACT` | individual discovery sources |

Every one is optional. A fresh clone with no configuration at all runs against
SQLite, skips the sources and agents that need credentials, and says which ones
it skipped.

`SUPABASE_REF` carries no default, which is deliberate and the odd one out --
the region defaults happily enough. The ref is not a secret; it is a username,
and the password is what guards the database. But it names one specific host on
a port open to the internet, a Supabase project ref cannot be rotated the way a
password can, and nothing here is a browser client that would publish it anyway.
A public repository is public forever, so there is nothing to buy by committing
it. Set it as a repository *variable* and the password as a *secret*.

### What gets stored

Postings are filtered at ingest, not after. The corpus a board returns is mostly
retail, clinical and sales work — roughly 80% of it — and storing that spends a
500 MB budget on postings no query asks for.

Six families are kept: `engineering`, `fde`, `ai`, `data`, `security` and
`product`. Two are not: `design`, and the `other` catch-all that holds retail,
clinical, sales, and non-software engineering — mechanical, civil, structural,
manufacturing.

The set is `ARGUS_STORE_FAMILIES` rather than a property of the classifier,
because the boundary is a product decision. `is_engineering` answers whether
something is engineering work; it cannot answer whether a product manager at a
software company is worth keeping.

The trade is real: a posting that is never stored can never be reclassified, so
a later ruleset only improves what arrives after it. Every live board is
re-polled hourly, so a broadened ruleset recovers its misses within a day — but
set `ARGUS_STORE_ONLY_TECHNICAL=0` before a ruleset change if you would rather
relabel the corpus than re-fetch it.

Migrations in `supabase/migrations/` are **not** applied automatically by any
workflow. Apply them with `make db-push` (which runs `supabase db push`) against
the linked project.

## Notes that are easy to get wrong

- **ATS slugs are case-insensitive.** `ramp`, `Ramp` and `RAMP` are one board.
  Slugs are lowercased on parse; skipping this creates duplicate rows that poll
  the same board.
- **A file is never a company.** Every ATS serves `robots.txt` and `sitemap.xml`
  off the same path shape a board occupies. In one crawl, 62 of 62 Lever records
  were `robots.txt` — and all 62 parsed cleanly as a board named `robots.txt`.
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
- **A batch is bounded by postings, not by boards.** Workday averages 183 open
  postings per board against Ashby's 17, and one board holds 20,598 — so a
  hundred busy boards stages 181,401 rows and the edit statement runs past
  Postgres's statement timeout.
- **A trailing `\b` cannot match a suffix.** `quantitative research` silently
  missed every *Researcher* in the corpus. The same shape hid *Vulnerability
  Researcher* and *Solutions Architecture*.
- **Substring matching on job titles is a trap.** `%llm%` matches *Fulfillment
  Associate* and *Licensed Master Social Worker*. Every pattern uses word
  boundaries for that reason.
- **A bound is not a date, and is still worth having.** Workday will not date
  71% of what it declines to date — it says "Posted 30+ Days Ago", which covers
  last month and 2019 equally. Storing `now − 30d` would invent precision, but
  the bound still answers the only question the age filter asks: if even the
  newest date it could have is too old, it is too old.
- **A date is captured once, while the posting is fresh.** An hourly poll sees
  a posting within an hour of it appearing, so "Posted 5 Days Ago" becomes a
  real date; the same posting reads "Posted 30+ Days Ago" a month later and
  cannot be dated at all. Which is why the update paths `COALESCE` rather than
  assign — assigning erased the date we already had, and only for postings that
  happened to be edited.
- **An age filter on a source with no dates is a delete, not a filter.**
  BambooHR publishes none — not in the list endpoint, not in the detail page —
  so `ARGUS_AGE_EXEMPT_ATS` exempts it rather than silently discarding 3,133
  postings, 2,223 of them engineering roles reachable through no other ATS.
  Those get their discovery date instead, written once at insert. Written on
  every poll it would re-date the posting each time it was edited, and sort it
  to the top of the dashboard for changing a title.
- **A silent zero is almost never the world being empty.** A source that returns
  nothing has usually been blocked, misconfigured or narrowed — Common Crawl swept
  one host of ten for months while every run looked entirely normal.
- **`Retry-After` is a request, not an instruction.** Cloudflare answers a
  rate-limited client with `Retry-After: 39481` — eleven hours — and urllib3
  obeys it literally, while holding the per-host slot. One request took a
  twelve-thread poll to zero boards in two hours. A delay named in hours is a
  refusal; `http.RETRY_AFTER_MAX` caps what we will wait for. Workable was
  removed for answering that to everything: recognising a source we can never
  poll just files boards forever, and 6,285 had accumulated against 664 jobs.
- **A connection held across slow work is a connection you will lose.** A poll
  keeps one session for twenty minutes and spends nearly all of it on HTTP, so
  the pooler reclaims it and the next write finds a closed socket. The wrapper
  reconnects once; the statements are idempotent, which is what makes that safe.
- **The free-tier limit that binds is tokens per minute, not requests per day.**
  Groq advertises 1,000 RPD and enforces 8,000 TPM, and `max_tokens` is charged
  as *requested* rather than as used — so a batch asking for 8,192 was refused
  outright while 997 of the day's requests remained.
- **A reasoning model bills its thinking as completion tokens.** Labelling fifty
  job titles cost 142 tokens a row at gpt-oss's default effort and 18 at
  `reasoning_effort: "low"`. Same answers, same model: an eight-minute job
  instead of a forty-three-minute one.
