# Argus

A job aggregator built on companies, the boards that reach them, and the postings they carry.

Argus maintains a registry of company job boards, polls them on a schedule, and emits
events when a posting appears, changes or closes. An LLM-driven orchestrator decides what
maintenance work the system needs next and runs it within a fixed time budget.

Built on one runtime dependency (`requests`), SQLite or Postgres, and GitHub Actions.

---

## How it works

Most companies post their jobs on their own careers page, hosted by one of a dozen
platforms — Greenhouse, Lever, Workday and the like. Those pages are the source. Argus
reads them directly, rather than waiting for a job to reach an aggregator.

It runs as a loop with four steps.

**1. Find the boards.** Eighteen sources are swept for links that look like a company job
board — Common Crawl, GitHub, Hacker News, SEC filings of companies that just raised, and
others. Each yields a candidate like *"Acme is on Greenhouse under the slug `acmecorp`"*.
Nobody publishes a list of these, so finding them is a job in itself.

**2. Check they are real.** Each new board is visited once. Does it respond? Does it have
any jobs? A board that answers but is empty is a real company that has paused hiring; a
board that does not answer at all has moved or shut down. Those are very different facts,
so they are stored as different states.

**3. Read them, on a clock.** Every two hours each live board is fetched and compared
against what was stored last time. Anything that changed becomes an event — a job
**opened**, **changed**, **reopened** or **closed**. That comparison is the whole product:
it is what lets you see a role the hour it appears, instead of whenever an aggregator
catches up.

**4. Keep the list clean.** A ruleset labels every job by kind (engineering, product,
security…), seniority and region, and anything outside the target is dropped *before* it
is stored rather than filtered out later at display time.

Once a day a separate process looks at the state of the whole system and decides what
needs attention most — a backlog of unchecked boards, a source that quietly stopped
working, jobs labelled by an outdated ruleset — then spends a fixed time budget on it.
That part uses an LLM, and it is deliberately kept away from step 3: **the job feed runs
on plain Python and would keep working if every model, every API key and every optional
dependency vanished.**

What comes out: a Discord digest of new roles, a read API, and a dashboard.

---

## The problem

Job boards are not a search problem. They are an inventory problem, with three properties
that break the obvious design.

**There is no index of job boards.** Every company hosts its own, on one of a dozen ATS
platforms, at a URL derived from a slug nobody publishes. `greenhouse.io/acmecorp` exists
only if you already know Acme uses Greenhouse and that their slug is `acmecorp`. Finding
boards is a discovery problem in its own right, distinct from reading them.

**Aggregators are lossy.** A posting reaches an aggregator when the company chooses to
syndicate it, which is neither immediate nor complete. Reading the company's own board is
the only way to see a posting at the moment it opens, and the only way to see the ones
never syndicated at all.

**Boards decay silently.** A slug is renamed, a company migrates ATS, a board empties out.
Nothing announces this. A registry that does not actively distinguish *empty* from *gone*
degrades into a list of URLs that mostly 404, and the failure looks exactly like a quiet
hiring market.

Argus treats all three as first-class: discovery is a subsystem, polling reads sources
directly, and every board carries a validated status that decay updates.

---

## Architecture

<img width="803" height="580" alt="arch" src="https://github.com/user-attachments/assets/08dbe3b6-b655-4a8d-bdbe-62cd1a632915" />


**Lane A is the feed.** Poll due boards, diff against what is stored, write events, post a
digest. No LLM, no graph framework, no optional dependencies. It is the part that must run
every two hours without fail, so it is the part with nothing in it that can fail
interestingly.

**Lane B is the brain.** It measures the system, applies a policy to decide the single most
valuable thing to do next, and runs it — within a wall-clock budget, resumable from a
checkpoint. It requires `langgraph` and an LLM provider, both optional extras. A runner
that only polls installs neither.

The separation is enforced by a test: the feed lane never imports the orchestrator or the
LLM client.

---

## The data model

Companies are the spine, not boards.

```
companies ──< boards ──< jobs
     │           │
     │           └──< board_sources    which discovery source found it, and when
     └──────────────  domain, name, careers_url
```

A board is a *reach* into a company, not the company itself. Acme on Greenhouse and Acme on
Workday after a migration are two boards and one company. Modelling it the other way makes
a migration look like a company disappearing and a new one being born, which discards the
posting history that makes the record worth keeping.

Boards carry a status that validation settles:

| status | meaning |
|---|---|
| `unvalidated` | discovered, never probed |
| `active` | responds, has postings |
| `empty` | responds, has none — the company is real, hiring is paused |
| `dead` | does not respond, repeatedly |

`empty` and `dead` are separate on purpose. Collapsing them discards the difference between
a company that stopped hiring and a board that stopped existing, and only one of those is
worth re-checking.

The schema is versioned (`SCHEMA_VERSION = 11`) with in-place migrations, and every
statement is portable across SQLite 3.39+ and Postgres.

**Tables:** `companies`, `boards`, `board_sources`, `jobs`, `events`, `poll_runs`,
`source_runs`, `proposals`, `notifier_state`, `notified_jobs`, `meta`.

---

## Lane A — the feed

### Adapters

Eight ATS platforms are pollable. Each adapter is a small module turning a board slug into
`Posting` records.

`ashby` · `bamboohr` · `breezy` · `greenhouse` · `lever` · `recruitee` · `smartrecruiters` · `workday`

Three more — `icims`, `rippling`, `jobvite` — are recognised and stored by discovery but
have no adapter, so their boards accumulate as `unvalidated` rather than being silently
dropped.

Polling order is not arbitrary. Workday paginates at 20 postings per request and averages
183 per board, making it an order of magnitude slower than the rest, so it is polled last,
with fewer workers, and takes whatever time remains. Per-host concurrency is capped by an
in-process semaphore — which is why exactly one poll runner may exist at a time.

### Reconcile and diff

Each poll fetches a board, compares it against stored state, and produces one of four
outcomes per posting: **new**, **edited**, **reopened**, **closed**. Closure is
grace-guarded (`CLOSE_GRACE_POLLS`) so a single bad fetch cannot close a board's entire
inventory, and a mass-close guard (`MASS_CLOSE_GUARD`) aborts the write when a board's
disappearance looks like an outage rather than a hiring freeze.

### Ingest filtering

Filtering happens **before storage**, not at query time. Three axes:

- **Family** — `engineering`, `fde`, `ai`, `data`, `security`, `product`
- **Region** — `us`, `europe`, `remote`, `unknown`; anything resolving to `other` is rejected
- **Age** — a fixed cutoff timestamp; anything older is not stored

A posting whose board publishes no date takes the current run's date, truncated to the day.
Day resolution matters: at per-second resolution the fallback encodes poll order, so
sorting by date returns one company's entire board before the next one starts.

### Classification

A regex ruleset (`classify/`) assigns each posting a family, a seniority and an
`is_engineering` flag. It is pure Python with **no model calls** — `argus classify` runs
with no API key configured. Geographic resolution (`classify/geo.py`) maps a free-text
location to a region through an ordered cascade: non-target countries and cities first,
then US states and abbreviations, then European countries, then cities, then remote-only
phrasing, then unknown.

Every posting records the `ruleset` version that labelled it, so a ruleset change leaves
the affected rows identifiable and re-sweepable.

---

## Lane B — the orchestrator

### The policy is a pure function

The decision is made neither by a model nor by the graph framework. It is an ordered list
of rules over a measured snapshot — plain Python, testable without a database, a network or
a graph:

| # | rule | fires when | routes to |
|---|---|---|---|
| 1 | `budget` | wall-clock budget spent | **end** |
| 2 | `collapsed_source` | a source's yield collapsed against its own history | `heal` |
| 3 | `validate_backlog` | more than 1,500 boards unvalidated | `validate` |
| 4 | `stale_ruleset` | more than 50,000 postings predate the current ruleset | `classify` |
| 5 | `resolve_backlog` | more than 5,000 companies without a careers page | `resolve` |
| 6 | `discover_due` | 24h or more since the last discovery run | `discover` |
| 7 | `yield_flat` | 7-day marginal yield under 100 boards | `prospect` |

First match wins, and rule order *is* the priority: a collapsed source outranks everything
but the budget, because finding more boards through a broken source is how you get a month
of quiet weeks.

`argus orchestrate --dry-run` prints every rule and whether it fires, writing nothing. A
policy you cannot inspect before it runs is one you have to trust.

Two properties are enforced by the loop rather than by the rules. A node too expensive for
the remaining budget is skipped *before* it starts, rather than killed mid-write. And a
node that declines — for any reason, including an agent reporting no LLM provider — is
never offered again during the same run.

### The agents

Three agents, each with the same discipline: **the model proposes, measurement disposes.**

| agent | proposes | what actually decides |
|---|---|---|
| `classifier` (`argus mine`) | regex patterns mined from the tail of titles no rule matched | precision scored against already-labelled titles |
| `prospector` (`argus prospect`) | URLs of pages that might enumerate job boards | how many *new* boards each URL yields when fetched |
| `healer` (`argus heal`) | a hypothesis for why a source's yield collapsed | nothing — it writes a diagnosis and never acts |

The classifier is the clearest case. The obvious move is to label the unplaced tail with a
model, and the output would be model opinion that costs money, expires the next time the
ruleset changes, and explains nothing. Mining *regex* from a sample instead produces rules:
free forever after, reviewable by a human, and applied by the sweep that already exists.

Note the healer's asymmetry. It has no gate and no applier, so the strongest thing it can
do is be read.

### Proposals and gates

Agent output does not take effect. It lands in `proposals`, and a gate decides:

```
agent ──▶ proposal ──▶ gate ──▶ accepted ──▶ applier
                          └───▶ rejected, with a recorded reason
```

`source` proposals are gated on measured new-board yield. `ruleset_patch` proposals are
gated on precision against known labels, because a pattern that mislabels an
already-correct title is the expensive kind of wrong. `diagnosis` has **no gate,
deliberately** — giving one to a healer's hypothesis would be the first step toward letting
it act.

`argus proposals` shows what the agents suggested and what the gates decided.

### Budget and resumption

The orchestrator runs against a wall-clock budget and checkpoints through LangGraph, so a
run killed by a CI job ceiling costs the remainder of a plan rather than the whole plan.

---

## Discovery

Eighteen sources, fourteen enabled by default. Each yields `BoardRef` records that flow
through one URL router, so every source benefits from every parser improvement.

| source | what it reads |
|---|---|
| `seedfile` | a curated starting list |
| `ashby_customers` | Ashby's own customer list |
| `simplify` · `jobrepos` · `jobjson` | community-maintained job repositories |
| `hn` · `hn_hiring` | Hacker News, and the monthly hiring threads |
| `commoncrawl` | Common Crawl indexes, per ATS host |
| `github` | code search for board URLs |
| `urlscan` | submitted-URL archives |
| `vcportfolio` | venture portfolio pages |
| `funding` | SEC EDGAR Form D filings — a company that just raised is about to hire |
| `ycombinator` | the YC company directory |
| `websearch` | pluggable search backend |
| `wayback` · `linkedin` · `zero2sudo` · `jobarchive` | opt-in |

`argus sources` lists them with readiness and any missing configuration.

### Source health

`argus health` reports per-source yield and trend. A source is judged on **refs seen**, not
on new boards found, because those answer different questions. Refs answer *is this source
working*; new boards answer *has the world changed*. A mature source finding nothing new is
saturated and healthy; a source seeing nothing at all is broken.

Saturation is reported explicitly, so a working-but-exhausted source is never mistaken for
a collapsed one.

---

## Storage

SQLite by default; Postgres when `SUPABASE_*` or `DATABASE_URL` is set. Every statement is
written to run on both — `ON CONFLICT DO NOTHING` rather than SQLite-only `OR IGNORE`,
`RETURNING id` because psycopg exposes no `lastrowid`.

Writes are batched. The pooler-facing connection reconnects once on a dropped socket, since
a session pooler will close an idle connection during a long run.

---

## The read API

`api/index.py` — FastAPI, deployed as a Vercel function. Everything here is a read: the
pipeline is the only writer and it runs on GitHub Actions, so this process never needs a
write connection or a transaction.

| route | returns |
|---|---|
| `GET /health` | liveness, unauthenticated |
| `GET /jobs` | open postings, filterable |
| `GET /companies` | the company registry |
| `GET /companies/{id}/jobs` | one company's postings |
| `GET /events` | recent new/edited/closed events |
| `GET /health/pipeline` | run recency and feed freshness |

Two resources, not three. `boards` stays internal — it is how a company is reached, an
implementation detail of collection rather than something a reader should have to join
through — so `jobs` answers by company.

Requests carry `X-API-Key` when `ARGUS_API_KEY` is set. The API uses the *transaction*
pooler, because serverless invocations are short and numerous, where the workers hold one
connection for a long poll and use the session pooler instead.

---

## The dashboard

`web-app/` — Next.js 16, React 19, Tailwind 4, shadcn/Radix components, reaching Postgres
through the transaction pooler.

Filters and sort live in URL parameters rather than React state, so the table stays a
server component and any view is linkable. Applied and hidden marks are per-browser
`localStorage`, not database rows: the jobs table is rebuildable, and there are no
accounts, so a database row would be the truth for every viewer rather than for one person.

```bash
cd web-app && npm install && npm run dev
```

---

## Scheduling

Four GitHub Actions workflows.

| workflow | schedule | concurrency group | ceiling |
|---|---|---|---|
| `ci` | push / PR | — | — |
| `poll` | `0 */2 * * *` | `pipeline` | 50 min |
| `discover` | `0 3 * * *` | `discovery` | 240 min |
| `orchestrate` | `0 9 * * *` | `pipeline` | 50 min |

A full registry sweep takes roughly 50 minutes, so a two-hourly poll leaves 70 minutes of
headroom. `poll` and `orchestrate` share a concurrency group so Lane B can never overlap
the feed and steal an ATS's rate limit. `discover` holds its own group: GitHub keeps at
most one *pending* run per group, so a long discovery sharing the feed's group would
collapse the queued polls into a single run.

`orchestrate` runs at 09:00 rather than alongside `discover` so that `hours_since_discover`
is small when the policy reads it — rule 6 does not fire, and the budget goes to
validation, resolution and the agents instead of repeating a sweep that has just finished.

---

## Quick start

```bash
pip install -e '.[dev]'          # add ,postgres and/or ,orchestrator as needed

argus init                       # create the database
argus discover                   # fill the board registry
argus validate                   # probe and settle new boards
argus poll                       # reconcile due boards, emit events
argus notify                     # post the digest
```

`argus stats` at any point gives a registry and feed summary.

---

## Commands

| command | purpose |
|---|---|
| `init` | create the database |
| `discover` | fill the board registry from all sources |
| `validate` | probe unvalidated boards, settle their status |
| `poll` | reconcile due boards and emit job events |
| `classify` | apply the role ruleset to postings that predate it |
| `notify` | post the digest of new engineering roles |
| `events` | recent new/edited/closed job events |
| `careers` | find or re-check company careers pages |
| `companies` | the company registry: who we watch and where |
| `stats` | registry and feed summary |
| `sources` | list discovery sources and readiness |
| `health` | per-source yield, and which sources have collapsed |
| `llm` | which LLM providers are configured |
| `orchestrate` | decide and run the most valuable work within a budget |
| `mine` | mine ruleset patterns from the unplaced title tail |
| `prospect` | propose new discovery sources, measured by what they yield |
| `heal` | diagnose a source whose yield collapsed |
| `proposals` | what the agents suggested, and what the gates decided |

---

## Configuration

All configuration is environment variables. None is required for a SQLite run.

### Storage

| variable | default | purpose |
|---|---|---|
| `ARGUS_DB` | `data/argus.db` | SQLite path |
| `ARGUS_DATABASE_URL` / `DATABASE_URL` | — | Postgres DSN |
| `SUPABASE_REF` · `SUPABASE_REGION` · `SUPABASE_DB_PASSWORD` | — | Supabase pooler connection |

### Ingest

| variable | default | purpose |
|---|---|---|
| `ARGUS_STORE_ONLY_TECHNICAL` | `1` | restrict to technical families |
| `ARGUS_STORE_FAMILIES` | `engineering,fde,ai,data,security,product` | which families to keep |
| `ARGUS_STORE_REGIONS` | `us,europe,remote,unknown` | which regions to keep |
| `ARGUS_STORE_POSTED_AFTER` | fixed timestamp | reject postings older than this |

### Polling

| variable | default | purpose |
|---|---|---|
| `ARGUS_WORKERS` | `12` | poll concurrency |
| `ARGUS_PER_HOST_CONCURRENCY` | `4` | per-host cap |
| `ARGUS_HTTP_TIMEOUT` | `20` | seconds |
| `ARGUS_CLOSE_GRACE_POLLS` | `2` | polls a posting must be absent before closing |
| `ARGUS_MASS_CLOSE_GUARD` | `5` | abort if a board loses more than this at once |
| `ARGUS_BACKOFF_BASE` / `ARGUS_BACKOFF_MAX` | `900` / `86400` | failure backoff, seconds |
| `ARGUS_DEAD_AFTER_FAILURES` | `8` | consecutive failures before a board is dead |

### Integrations

| variable | purpose |
|---|---|
| `GROQ_API_KEY` · `NVIDIA_API_KEY` · `GEMINI_API_KEY` | LLM providers, tried in that order |
| `GITHUB_TOKEN` | raises the GitHub discovery rate limit |
| `ARGUS_SEC_CONTACT` | name and email; EDGAR requires contact details in the User-Agent |
| `ARGUS_DISCORD_WEBHOOK` | digest destination |
| `ARGUS_API_KEY` | required in `X-API-Key` on the read API; unset leaves it open |

No webhook configured is not an error — `notify` prints what it would have sent and exits 0.

---

## Layout

```
argus/
  adapters/      one module per ATS; slug -> Posting
  discovery/     one module per source; yields BoardRef
  registry/      boards, companies, careers pages, validation
  feed/          reconcile, diff, notify — Lane A
  classify/      the role ruleset and geographic resolution
  orchestrator/  measure, policy, nodes, graph — Lane B
  agents/        classifier, prospector, healer
  proposals/     gates and appliers
  obs/           run history and source health
  core/          config, db, http, models, urls
api/             FastAPI read surface, deployed on Vercel
web-app/         Next.js dashboard
scripts/         one-off maintenance
seeds/           curated starting lists, per ATS
supabase/        migrations
tests/
```

---

## Development

```bash
pip install -e '.[dev,orchestrator]'
ruff check . && ruff format --check .
pytest
```

The orchestrator extra is installed in CI so the tests exercise what ships. The pipeline
itself needs neither it nor an LLM client, and a test asserts the feed lane never imports
either.
