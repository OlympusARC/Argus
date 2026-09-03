"""Adapter registry. `ats` strings here must match those produced by urls.parse."""

from __future__ import annotations

from .ashby import AshbyAdapter
from .bamboohr import BambooHRAdapter
from .base import Adapter
from .breezy import BreezyAdapter
from .greenhouse import GreenhouseAdapter
from .lever import LeverAdapter
from .recruitee import RecruiteeAdapter
from .smartrecruiters import SmartRecruitersAdapter
from .workday import WorkdayAdapter

ADAPTERS: dict[str, Adapter] = {
    AshbyAdapter.ats: AshbyAdapter(),
    GreenhouseAdapter.ats: GreenhouseAdapter(),
    LeverAdapter.ats: LeverAdapter(),
    WorkdayAdapter.ats: WorkdayAdapter(),
    SmartRecruitersAdapter.ats: SmartRecruitersAdapter(),
    BreezyAdapter.ats: BreezyAdapter(),
    RecruiteeAdapter.ats: RecruiteeAdapter(),
    BambooHRAdapter.ats: BambooHRAdapter(),
}

"""
Recognized by the URL router and stored in the registry, but not polled.

icims, rippling and jobvite have no adapter yet: write one, register it
above, and the boards are already waiting in the database.

workable is different -- the adapter exists and works. It is unregistered
because Cloudflare rate-limits us to a standstill: a 429 carrying
Retry-After: 39481, eleven hours, on every request. The cap in core/http
stops that hanging a run, but a source that answers 429 to everything
returns nothing either way.

It was never worth much. 1,479 active boards produced 664 postings -- 0.4 a
board where Workday gives 14 -- and 0.6% of the feed, for more validate and
poll time than every other ATS combined. Its 664 postings are genuinely
unique, so re-register it if that changes; the boards and their jobs stay in
the database meanwhile.
"""
PLANNED = ("icims", "rippling", "jobvite", "workable")


def get(ats: str) -> Adapter | None:
    return ADAPTERS.get(ats)


def supported() -> list[str]:
    return sorted(ADAPTERS)
