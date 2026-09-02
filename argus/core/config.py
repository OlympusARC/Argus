"""Central config. Every value is overridable by env var."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(os.getenv("ARGUS_DATA", ROOT / "data"))
DB_PATH = Path(os.getenv("ARGUS_DB", DATA_DIR / "argus.db"))

"""
When set, every command talks to Postgres instead of the local SQLite file.
Only the password is secret -- the rest is derived from the Supabase project
ref, so CI and a laptop can share one variable.

Workers want the session pooler (5432): they hold one connection for a long
poll. The API wants the transaction pooler (6543), where connections are
short and numerous.
"""
DATABASE_URL = os.getenv("ARGUS_DATABASE_URL") or os.getenv("DATABASE_URL")
SUPABASE_REF = os.getenv("SUPABASE_REF") or None
SUPABASE_REGION = os.getenv("SUPABASE_REGION", "us-west-2")
SUPABASE_DB_PASSWORD = os.getenv("SUPABASE_DB_PASSWORD")


def database_url(pooled: bool = False) -> str | None:
    """The Postgres URL, explicit or composed from the project ref."""
    if DATABASE_URL:
        return DATABASE_URL
    if not (SUPABASE_DB_PASSWORD and SUPABASE_REF):
        return None
    port = 6543 if pooled else 5432
    return (
        f"postgresql://postgres.{SUPABASE_REF}:{SUPABASE_DB_PASSWORD}"
        f"@aws-0-{SUPABASE_REGION}.pooler.supabase.com:{port}/postgres"
    )


"""
Seeds are hand-written source, not generated output, so they live outside
data/ -- which lets data/ be ignored wholesale.
"""
SEEDS_DIR = ROOT / "seeds"
EVENTS_LOG = Path(os.getenv("ARGUS_EVENTS_LOG", DATA_DIR / "events.jsonl"))

USER_AGENT = os.getenv(
    "ARGUS_UA",
    "argus/0.1 (+https://github.com/OlympusARC/Argus) python-requests",
)
HTTP_TIMEOUT = float(os.getenv("ARGUS_HTTP_TIMEOUT", "20"))

"""
Concurrency is capped per ATS host, not globally: each ATS is a single origin
and we would rather be a well-behaved client than fast.
"""
PER_HOST_CONCURRENCY = int(os.getenv("ARGUS_PER_HOST_CONCURRENCY", "4"))
WORKERS = int(os.getenv("ARGUS_WORKERS", "12"))

"""
Poll cadence per tier, in seconds. Boards start at tier 1 and get demoted
when they go quiet (see reconcile.retier).
"""
TIER_INTERVALS = {
    1: int(os.getenv("ARGUS_TIER1", str(60 * 60))),  # hourly
    2: int(os.getenv("ARGUS_TIER2", str(6 * 60 * 60))),  # 6h
    3: int(os.getenv("ARGUS_TIER3", str(24 * 60 * 60))),  # daily
}
DEFAULT_TIER = 1
"""
Demote a board to the next tier after this long with no new postings.
"""
QUIET_DEMOTE_AFTER = int(os.getenv("ARGUS_QUIET_DEMOTE_AFTER", str(14 * 86400)))

"""
A posting is only closed after it is absent from this many *successful*
consecutive polls. Absence from a full-board endpoint is near-authoritative,
but a truncated response should never mass-close a board.
"""
CLOSE_GRACE_POLLS = int(os.getenv("ARGUS_CLOSE_GRACE_POLLS", "2"))
"""
Safety valve: if a poll returns 0 jobs for a board that had more than this
many open jobs, treat it as suspect and skip the close pass.
"""
MASS_CLOSE_GUARD = int(os.getenv("ARGUS_MASS_CLOSE_GUARD", "5"))

"""
Store only what the product serves.

The corpus is 82% retail, clinical and sales work -- 725,539 postings of a
500 MB budget spent on store associates and delivery drivers that no query
ever asks for. Filtering at ingest is the difference between 609 MB and
115 MB.

The cost: a posting that is never stored can never be reclassified, so a
later ruleset only improves what arrives after it. Every live board is
re-polled hourly, so a broadened ruleset recovers its misses within a day --
but set this to 0 before a ruleset change if you want the old corpus
re-labelled rather than re-fetched.
"""
STORE_ONLY_TECHNICAL = os.getenv("ARGUS_STORE_ONLY_TECHNICAL", "1") not in ("0", "false", "")

"""
Rows a single diff batch may stage. The board count alone is the wrong
bound: a hundred Workday boards stages 181,401 postings where a hundred
Ashby boards stages 1,700, and the first exceeds Postgres's two-minute
statement timeout. Measured -- 181k rows times out, 20k settles in seconds --
with room left for the corpus to grow.
"""
BATCH_POSTINGS = int(os.getenv("ARGUS_BATCH_POSTINGS", "20000"))

"""
Exponential backoff on consecutive board failures.
"""
BACKOFF_BASE = int(os.getenv("ARGUS_BACKOFF_BASE", "900"))  # 15 min
BACKOFF_MAX = int(os.getenv("ARGUS_BACKOFF_MAX", str(24 * 3600)))
"""
Boards that fail this many times in a row are parked as 'dead'.
"""
DEAD_AFTER_FAILURES = int(os.getenv("ARGUS_DEAD_AFTER_FAILURES", "8"))

"""
---------------------------------------------------------------------------
Community job-list repos.

Every entry below was verified by fetching the file and running urls.parse
over it. Three families look useful and are not: jobright-ai, workopia and
the dreamworkhq JSON all route their apply links through their own domain, so
they yield no boards at all no matter how many postings they list.

These repos are downstream of the company careers pages we actually monitor.
They earn their place by naming employers we have not seen, not by being a
source of truth about any one posting.
---------------------------------------------------------------------------
"""

"""
The Simplify listings.json shape: a flat list of {company_name, url, active}.
Widely cloned, so one reader serves all of them. (repo, branch, path, segment)
"""
SIMPLIFY_REPOS = [
    ("SimplifyJobs/New-Grad-Positions", "dev", ".github/scripts/listings.json", "new_grad"),
    (
        "SimplifyJobs/Summer2027-Internships",
        "dev",
        ".github/scripts/listings.json",
        "internship",
    ),
    ("vanshb03/New-Grad-2027", "dev", ".github/scripts/listings.json", "new_grad"),
    ("vanshb03/Summer2027-Internships", "dev", ".github/scripts/listings.json", "internship"),
    # Forks that have drifted far enough to carry boards the parents never had.
    # polinavishnev alone holds 212 boards the registry had never seen; its
    # postings are long dead, which costs nothing because seeding skips
    # anything not marked active.
    ("polinavishnev/New-Grad-Positions", "dev", ".github/scripts/listings.json", "new_grad"),
    ("haydenthai/New-Grad-2025", "main", ".github/scripts/listings.json", "new_grad"),
    ("Craftix-AI-Inc/New-Grad-Positions", "dev", ".github/scripts/listings.json", "new_grad"),
    (
        "lucianlavric/CanadaTechInternships-Summer2026",
        "dev",
        ".github/scripts/listings.json",
        "internship",
    ),
    (
        "MubeenMohammed/canada-2026-internships",
        "main",
        ".github/scripts/listings.json",
        "internship",
    ),
    ("swetha7502/Summer2026-Internships", "dev", ".github/scripts/listings.json", "internship"),
    (
        "Jose-Gael-Cruz-Lopez/underclassmen-opportunities",
        "main",
        ".github/scripts/listings.json",
        "internship",
    ),
]

"""
Structured repos that agree on nothing. Read by discovery/jobjson.py, which
walks the JSON looking for anything the URL router recognizes rather than
knowing any one schema. (repo, branch, path)
"""
JOBJSON_REPOS = [
    ("tailed-community/tech-new-grads-2025-2026", "main", "data/current.json"),
    ("tailed-community/tech-internships-2025-2026", "main", "data/current.json"),
    ("ctsc/southeast-tech-internships-2026-2027", "main", "data/jobs.json"),
    ("sonak11/internatlas", "main", "generated/exports/internships.json"),
    ("Donkey0322/JobRadar-AI", "main", "data/opportunities.ndjson"),
    ("coconight01/2027-North-America-New-Grad-Jobs", "main", "data/jobs.json"),
    ("ademismkv/Internships-in-APAC-EMEA-2027", "main", "data/listings.json"),
    (
        "zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships",
        "main",
        "data/jobs.json",
    ),
    (
        "Amit-Mahato54322/automated-list-of-summer-2027-and-fall-2026-tech-internships",
        "main",
        "data/jobs.json",
    ),
    ("ApplyGuy/2027-New-Grad-Jobs", "main", "data/new-grad-jobs.json"),
    ("ApplyGuy/2027-Internships", "main", "data/internships.json"),
    ("Jiang6082/QJS", "main", "data/us_financial_services_internship_scan_raw.json"),
    ("Alanhsiu/jobwatch", "main", "roles.json"),
    ("harrycodingnow/new-grad-2027-tracker", "main", "data/active_jobs.json"),
    ("michae1lm/Summer-2027-Internships-Jobs", "main", "opportunities.json"),
    # Links go through dreamworkhq.com so these yield no boards -- but every
    # record carries companyDomain, which is exactly what the careers prober
    # needs. Kept for the companies, not the boards.
    ("dreamworkhq/Tech-Internships-2027", "main", "data/listings.json"),
    ("dreamworkhq/Tech-Internships-2027", "main", "data/international-listings.json"),
    ("dreamworkhq/New-Grad-Software-Engineer-Jobs", "main", "data/listings.json"),
    ("dreamworkhq/AI-ML-Jobs", "main", "data/listings.json"),
    ("dreamworkhq/Remote-Tech-Jobs", "main", "data/listings.json"),
    ("dreamworkhq/Data-Science-Jobs", "main", "data/listings.json"),
    ("dreamworkhq/Cybersecurity-Jobs", "main", "data/listings.json"),
    ("dreamworkhq/Open-Tech-Internships-2027", "main", "data/listings.json"),
    ("dreamworkhq/Open-Tech-Internships-2027", "main", "data/international-listings.json"),
]

"""
Historical archives: closed postings, so worthless to the feed, but a board
that carried a job in 2025 still exists. ~90 MB, so this is opt-in via
`discover -s jobarchive` rather than part of the weekly run.
"""
JOBJSON_ARCHIVES = [
    ("tailed-community/tech-internships-2025-2026", "main", "data/archived.json"),
    ("tailed-community/tech-new-grads-2025-2026", "main", "data/archived.json"),
    ("vanshb03/Summer2027-Internships", "dev", "archived/2025/archived.json"),
    ("vanshb03/Summer2027-Internships", "dev", "archived/2026/archived.json"),
    ("vanshb03/New-Grad-2027", "dev", "archived/2025/listings.json"),
    ("Donkey0322/JobRadar-AI", "main", "data/archive/opportunities-001.ndjson"),
    ("Donkey0322/JobRadar-AI", "main", "data/archive/opportunities-002.ndjson"),
    ("Donkey0322/JobRadar-AI", "main", "data/archive/opportunities-003.ndjson"),
]

"""
Repos with no structured file -- their README is a rendered table of postings
whose apply links carry the raw ATS URL, which the router can read directly.
"""
COMMUNITY_REPOS = [
    ("speedyapply/2027-SWE-College-Jobs", "main", "README.md"),
    ("speedyapply/2027-AI-College-Jobs", "main", "README.md"),
    ("Education-Victory/2025-New-Grad-Positions", "main", "README.md"),
    ("coderQuad/New-Grad-Positions", "main", "README.md"),
    ("arunike/Summer-2026-Internship", "main", "README.md"),
    ("ReaVNaiL/New-Grad-2024", "main", "README.md"),
    ("Ouckah/Summer2025-Internships", "main", "README.md"),
    # The zapplyjobs family: one repo per job family, all refreshed daily, all
    # linking straight to the employer's ATS.
    ("zapplyjobs/Internships-2027", "main", "README.md"),
    ("zapplyjobs/New-Grad-Jobs-2027", "main", "README.md"),
    ("zapplyjobs/New-Grad-Software-Engineering-Jobs-2027", "main", "README.md"),
    ("zapplyjobs/New-Grad-Data-Science-Jobs-2027", "main", "README.md"),
    ("zapplyjobs/New-Grad-Hardware-Engineering-Jobs-2027", "main", "README.md"),
    ("zapplyjobs/New-Grad-IT-Jobs-2027", "main", "README.md"),
    ("zapplyjobs/New-Grad-Healthcare-Jobs-2027", "main", "README.md"),
    ("zapplyjobs/Canada-Jobs-2027", "main", "README.md"),
    ("zapplyjobs/awesome-ml-internships", "main", "README.md"),
    ("resumax/new-grad-tech-jobs", "main", "README.md"),
    ("resumax/tech-internships", "main", "README.md"),
    ("Chieler/Summer-2027-SWE-Internships", "main", "README.md"),
    ("DereC4/internships-and-newgrad", "main", "README.md"),
    ("tonyyu2170/summer2027-internship-tracker", "main", "README.md"),
    ("PrepAIJobs/New-Grad-2026", "main", "README.md"),
    ("PrepAIJobs/Summer2026-Internships", "main", "README.md"),
    ("sndsh404/summer-2027-internships", "main", "README.md"),
    ("Krishpraj/Canadian_Internships_2026", "main", "README.md"),
    ("negarprh/Canadian-Tech-Internships-2027", "main", "README.md"),
    ("didtheyghostme/Singapore-Summer2026-TechInternships", "main", "README.md"),
    ("kazisean/nyc-internship-2026", "main", "README.md"),
    ("soongenwong/Europe-Tech-Internships-2026", "main", "README.md"),
    ("armankuzembayev/tech_internships_london_2026", "main", "README.md"),
    ("northwesternfintech/2027QuantInternships", "main", "README.md"),
    ("LogicodeHQ/hardware-jobs-internships-2026", "main", "README.md"),
    ("dpnsu/2026-SWE-College-Jobs", "master", "README.md"),
    # No longer updated, but each still names boards nothing else has. Cheap to
    # re-read (a README is kilobytes), so they stay in the weekly run.
    ("BlinkyJobs/2026-summer-internships", "main", "README.md"),
    ("arunike/Summer-2025-Internship-List", "main", "README.md"),
    ("Rokkam19/2026-New-Grad-Positions", "add-singularity-data", "README.md"),
    ("elaine-zheng/summer2020internships", "master", "README.md"),
    ("AlanChen4/Summer-2024-SWE-Internships", "main", "README.md"),
    ("AlanChen4/2024-SWE-New-Grad", "main", "README.md"),
    ("christine-hu/summer-2019-internships", "master", "README.md"),
    ("HassanChowdhry/Canadian-Tech-Internships-2025", "main", "README.md"),
    ("isaiahiruoha/Canadian-Tech-And-Business-Internships-Summer-2025", "main", "README.md"),
    ("Dannny-Babs/Canadian-Tech-Internships-2025", "main", "README.md"),
    ("kxrt/Singapore-Summer2024-TechInternships", "main", "README.md"),
    ("ykeim/Cybersecurity-Internships-Summer2023", "main", "README.md"),
    ("jxucoder/2021-new-grads", "master", "README.md"),
    ("jxucoder/2024-new-grad-jobs", "main", "README.md"),
    ("Offerin-AI/new-grad-sde-jobs", "main", "README.md"),
]
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")

"""
The hourly digest's destination. Absent means the notifier is inert: it still
builds and can be reviewed with --dry-run, but sends nothing. That is the
right default for a fresh clone, and it is why `argus notify` never fails a
poll run just because no webhook is configured.
"""
DISCORD_WEBHOOK = os.getenv("ARGUS_DISCORD_WEBHOOK") or os.getenv("DISCORD_WEBHOOK_URL")
