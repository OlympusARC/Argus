"""Adapter contract. One class per ATS; the reconciler knows nothing else."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..core.models import Posting


class Adapter(ABC):
    ats: str = ""

    @abstractmethod
    def board_url(self, slug: str) -> str:
        """The endpoint a poll hits. Used for logging and per-host throttling."""

    def count(self, slug: str) -> int:
        """How many postings the board has, for validation only.

        Defaults to fetching the board, which is right for the single-request
        ATSs. Paginated adapters should override this: validation only needs to
        know the board exists and roughly how big it is, and paying full
        pagination for that across thousands of boards is pure waste.
        """
        return len(self.fetch(slug))

    @abstractmethod
    def fetch(self, slug: str) -> list[Posting]:
        """Return every currently-listed posting on the board.

        Must raise FetchError(permanent=True) when the board provably does not
        exist, and FetchError(permanent=False) for anything transient. The
        reconciler relies on that distinction: it never closes postings on a
        failed poll, and only a permanent failure can mark a board dead.
        """
