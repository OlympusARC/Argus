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
Recognized by the URL router and stored in the registry, but not yet pollable.
Adding one is: write the adapter, register it above, and the boards are
already waiting in the database.

Workable is not here, and not in the router either. Cloudflare answers every
request with a 429 carrying Retry-After: 39481 -- eleven hours -- so the
source returns nothing however patiently we ask. Keeping it recognised would
mean discovery continuing to file boards that can never be polled: 6,285 of
them had accumulated, against 664 postings.
"""
PLANNED = ("icims", "rippling", "jobvite")


def get(ats: str) -> Adapter | None:
    return ADAPTERS.get(ats)


def supported() -> list[str]:
    return sorted(ADAPTERS)
