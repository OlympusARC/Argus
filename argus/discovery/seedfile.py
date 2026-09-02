"""Hand-curated seed files: data/seeds/{ats}.txt, one slug or URL per line.

Lowest volume, highest precision, zero network. `#` comments allowed. A bare
token is read as a slug for the ats named by the filename; a full URL is routed
through the parser so a seed file can mix ATSs.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from ..core import config, urls
from ..core.models import BoardRef
from .base import Source


class SeedFileSource(Source):
    name = "seedfile"

    def __init__(self, directory: Path | None = None):
        self.dir = Path(directory or config.SEEDS_DIR)

    def available(self) -> tuple[bool, str]:
        if not self.dir.is_dir():
            return False, f"no seeds directory at {self.dir}"
        return True, ""

    """
    A seed file names its ATS via the filename; a full URL inside it wins.
    """
    HOSTS = {
        "ashby": "https://jobs.ashbyhq.com/{slug}",
        "greenhouse": "https://job-boards.greenhouse.io/{slug}",
        "lever": "https://jobs.lever.co/{slug}",
    }

    def discover(self) -> Iterator[BoardRef]:
        for path in sorted(self.dir.glob("*.txt")):
            default_ats = path.stem.lower()
            template = self.HOSTS.get(default_ats)
            for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                is_url = line.startswith(("http://", "https://")) or "/" in line
                if is_url:
                    ref = urls.parse(line)
                elif template:
                    ref = urls.parse(template.format(slug=line))
                else:
                    continue  # bare slug, unknown ATS
                if ref:
                    yield BoardRef(ref.ats, ref.slug, None, self.name, {"file": path.name})
