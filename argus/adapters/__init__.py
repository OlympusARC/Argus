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
from .workable import WorkableAdapter
from .workday import WorkdayAdapter

ADAPTERS: dict[str, Adapter] = {
    AshbyAdapter.ats: AshbyAdapter(),
    GreenhouseAdapter.ats: GreenhouseAdapter(),
    LeverAdapter.ats: LeverAdapter(),
    WorkdayAdapter.ats: WorkdayAdapter(),
    SmartRecruitersAdapter.ats: SmartRecruitersAdapter(),
    WorkableAdapter.ats: WorkableAdapter(),
    BreezyAdapter.ats: BreezyAdapter(),
    RecruiteeAdapter.ats: RecruiteeAdapter(),
    BambooHRAdapter.ats: BambooHRAdapter(),
}

"""
Recognized by the URL router and stored in the registry, but not yet pollable.
Adding one is: write the adapter, register it above, and the boards are
already waiting in the database.
"""
PLANNED = ("icims", "rippling", "jobvite")


def get(ats: str) -> Adapter | None:
    return ADAPTERS.get(ats)


def supported() -> list[str]:
    return sorted(ADAPTERS)
