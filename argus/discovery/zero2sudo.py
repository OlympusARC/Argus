"""@zero2sudo Instagram stories -- a human curator as a discovery source.

Ported from binaryshrey/BOARD, where it emailed the links directly. Here it is
wired as a normal discovery source instead: story links run through the shared
URL router, so anything pointing at a recognized ATS lands in the registry and
is polled like any other board, while non-ATS links are surfaced as events.

Requires Instagram credentials, so it is disabled unless both env vars are set:
    ARGUS_IG_USER / ARGUS_IG_PASS

Worth knowing before enabling: instagrapi drives Instagram's private mobile
API. That is against Instagram's terms, and automated logins do get accounts
flagged. Use a throwaway account, not your main one.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Iterator

from ..core import config, urls
from ..core.models import BoardRef
from .base import Source

TARGET = os.getenv("ARGUS_IG_TARGET", "zero2sudo")
SESSION_FILE = config.DATA_DIR / "ig_session.json"
_URL_RE = re.compile(r"https?://[^\s\"'<>\])​]+")


class Zero2SudoSource(Source):
    name = "zero2sudo"
    needs_auth = True

    def __init__(self, target: str = TARGET):
        self.target = target

    def available(self) -> tuple[bool, str]:
        if not (os.getenv("ARGUS_IG_USER") and os.getenv("ARGUS_IG_PASS")):
            return False, "set ARGUS_IG_USER and ARGUS_IG_PASS to enable"
        try:
            import instagrapi  # noqa: F401
        except ImportError:
            return False, "pip install instagrapi"
        return True, ""

    def _client(self):
        from instagrapi import Client

        cl = Client()
        cl.delay_range = [1, 3]
        if SESSION_FILE.exists():
            """
            A stale or corrupt session file is the expected case, not an
            error: logging in afresh below is the fallback.
            """
            with contextlib.suppress(Exception):
                cl.load_settings(SESSION_FILE)
        cl.login(os.environ["ARGUS_IG_USER"], os.environ["ARGUS_IG_PASS"])
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        cl.dump_settings(SESSION_FILE)
        return cl

    def story_links(self) -> list[str]:
        cl = self._client()
        uid = cl.user_id_from_username(self.target)
        out: list[str] = []
        for story in cl.user_stories(uid):
            for attr in ("caption_text", "video_url", "thumbnail_url"):
                out += _URL_RE.findall(str(getattr(story, attr, "") or ""))
            for link in getattr(story, "links", None) or []:
                url = getattr(link, "webUri", None) or getattr(link, "url", None)
                if url:
                    out.append(str(url))
        return out

    def discover(self) -> Iterator[BoardRef]:
        seen: set[tuple[str, str]] = set()
        for link in self.story_links():
            for ref in urls.extract_all(link):
                if (ref.ats, ref.slug) not in seen:
                    seen.add((ref.ats, ref.slug))
                    yield BoardRef(ref.ats, ref.slug, None, self.name, {"link": link[:200]})
