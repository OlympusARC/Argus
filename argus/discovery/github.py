"""GitHub code search.

The only source that requires auth: /search/code returns 401 anonymously, no
matter the rate limit. We do not ask the user for a PAT -- if `gh` is logged in
we borrow its token at runtime and never write it anywhere.

Code search caps any one query at 1000 results, so we widen with query variants
rather than deep pagination. Authenticated limit is ~10 req/min, hence the
deliberate sleep.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator

from ..core import http, urls
from ..core.models import BoardRef, FetchError
from .base import Source

API = "https://api.github.com/search/code"
VARIANTS = (
    '"jobs.ashbyhq.com"',
    '"jobs.ashbyhq.com" extension:md',
    '"jobs.ashbyhq.com" extension:json',
    '"jobs.ashbyhq.com" extension:yml',
    '"jobs.ashbyhq.com" extension:yaml',
    '"jobs.ashbyhq.com" extension:txt',
    '"jobs.ashbyhq.com" extension:csv',
    '"jobs.ashbyhq.com" extension:html',
    '"jobs.ashbyhq.com" extension:py',
    '"jobs.ashbyhq.com" extension:ts',
    '"api.ashbyhq.com/posting-api/job-board"',
    '"posting-api/job-board"',
)


def _token() -> str | None:
    tok = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if tok:
        return tok.strip()
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class GitHubSource(Source):
    name = "github"
    needs_auth = True

    def __init__(self, variants=None, pages: int = 5, pause: float = 6.5):
        self.variants = tuple(variants or VARIANTS)
        self.pages = pages
        self.pause = pause
        self._tok = None

    def available(self) -> tuple[bool, str]:
        self._tok = _token()
        if not self._tok:
            return False, "no GITHUB_TOKEN and `gh auth token` unavailable"
        return True, ""

    def discover(self) -> Iterator[BoardRef]:
        if not self._tok:
            ok, why = self.available()
            if not ok:
                return
        headers = {
            "Authorization": f"Bearer {self._tok}",
            "Accept": "application/vnd.github.text-match+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        seen: set[tuple[str, str]] = set()
        first = True
        for q in self.variants:
            for page in range(1, self.pages + 1):
                if not first:
                    time.sleep(self.pause)  # authenticated code search is ~10/min
                first = False
                try:
                    data = http.get_json(
                        API,
                        headers=headers,
                        params={"q": q, "per_page": "100", "page": str(page)},
                    )
                except FetchError:
                    break
                items = data.get("items") or []
                if not items:
                    break
                for item in items:
                    blob = " ".join(
                        frag.get("fragment", "")
                        for m in (item.get("text_matches") or [])
                        for frag in [m]
                    ) or item.get("html_url", "")
                    for ref in urls.extract_all(blob):
                        if (ref.ats, ref.slug) in seen:
                            continue
                        seen.add((ref.ats, ref.slug))
                        yield BoardRef(
                            ref.ats,
                            ref.slug,
                            None,
                            self.name,
                            {"repo": (item.get("repository") or {}).get("full_name")},
                        )
                if len(items) < 100:
                    break
