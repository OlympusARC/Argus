"""Discovery source contract.

A source yields references, never decisions. It does not validate, does not
poll and does not decide what is real -- `argus validate` does that in one
place, so a noisy high-recall source costs nothing but HTTP.

Two kinds of reference, and the distinction is load-bearing:

  BoardRef    -- this looks like an ATS board. Goes to the board registry.
  CompanyRef  -- this is an employer, with no board attached (yet). Goes to the
                 companies table as a probe target.

The second exists because most of the web names employers without linking their
ATS. Forcing those into BoardRefs would fill the board registry with rows that
have no slug to poll; dropping them would throw away exactly the companies the
careers prober is designed to find boards for.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from ..core.models import BoardRef, CompanyRef

Ref = BoardRef | CompanyRef


class Source(ABC):
    name: str = ""
    needs_auth: bool = False

    @abstractmethod
    def discover(self) -> Iterator[Ref]: ...

    def available(self) -> tuple[bool, str]:
        """Cheap precondition check so a missing token skips one source, not the run."""
        return True, ""
