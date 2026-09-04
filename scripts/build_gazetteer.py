"""Build argus/classify/cities.tsv.gz from a GeoNames city dump.

Run by hand when the gazetteer needs refreshing; the output is committed, so
ingest never touches the network. GeoNames is CC-BY 4.0.

    curl -O https://download.geonames.org/export/dump/cities5000.zip
    unzip cities5000.zip
    python scripts/build_gazetteer.py cities5000.txt

The output is one line per place name: "<name>\\t<u|e|o>". Names are stored
in two forms -- lowercased as written, and folded to ASCII -- because a
posting may name a city in its own script ("София") while the dump's Latin
alternate name is what a rule would recognise.
"""

import gzip
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

"""
Europe as the region, not the EU as a bloc: the UK, Switzerland, Norway and
the Balkans are all places this feed wants.
"""
EUROPE = frozenset(
    {
        "AL",
        "AD",
        "AT",
        "BY",
        "BE",
        "BA",
        "BG",
        "HR",
        "CY",
        "CZ",
        "DK",
        "EE",
        "FI",
        "FR",
        "DE",
        "GI",
        "GR",
        "HU",
        "IS",
        "IE",
        "IM",
        "IT",
        "XK",
        "LV",
        "LI",
        "LT",
        "LU",
        "MT",
        "MD",
        "MC",
        "ME",
        "NL",
        "MK",
        "NO",
        "PL",
        "PT",
        "RO",
        "RS",
        "SK",
        "SI",
        "ES",
        "SE",
        "CH",
        "UA",
        "GB",
        "VA",
        "FO",
        "GG",
        "JE",
        "AX",
        "SM",
    }
)

"""
The ISO country code is stored, not the region, so a lookup can answer two
questions: where is this place, and does it agree with a two-letter code
written beside it. "Munich, DE" is Germany because Munich is DE; "Paris, TX"
is Texas because Paris is FR and FR is not TX.
"""

"""
Words that appear in a location field meaning "no location", and that
GeoNames also happens to hold as a place name.

Excluded at build time rather than filtered at lookup, so the data file is
correct by construction. The costly one is "tbd", which resolves to a town in
a country we do not want -- a posting whose location is genuinely undecided
would be *rejected* rather than merely unplaced.
"""
STOPWORDS = frozenset(
    {
        "all",
        "any",
        "anywhere",
        "area",
        "branch",
        "campus",
        "center",
        "centre",
        "central",
        "corporate",
        "depot",
        "district",
        "east",
        "factory",
        "field",
        "flexible",
        "global",
        "headquarters",
        "home",
        "hybrid",
        "international",
        "lab",
        "laboratory",
        "location",
        "main",
        "multiple",
        "national",
        "nationwide",
        "none",
        "north",
        "office",
        "onsite",
        "other",
        "plant",
        "region",
        "remote",
        "site",
        "south",
        "store",
        "tbd",
        "terminal",
        "unknown",
        "various",
        "virtual",
        "warehouse",
        "west",
        "work",
        "worldwide",
    }
)


def keys(name: str) -> set[str]:
    low = re.sub(r"[^\w ]+", " ", name.lower()).strip()
    folded = re.sub(
        r"[^a-z0-9 ]+",
        " ",
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower(),
    ).strip()
    return {k for k in (low, folded) if len(k) >= 3 and k not in STOPWORDS}


def main(src: str) -> None:
    """
    Population-weighted rather than first-wins. 1.6% of names exist in more
    than one region and the weighting settles them the way a reader would:
    Bristol is the English one, Washington the American one, Markham the
    Canadian one -- which is a rejection, and the reason non-target countries
    have to be in here at all rather than only the ones we want.
    """
    weight: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    with Path(src).open(encoding="utf-8") as fh:
        for line in fh:
            f = line.split("\t")
            if len(f) < 15:
                continue
            cc, pop = f[8], int(f[14] or 0)
            for name in {f[1], f[2]} | {x for x in f[3].split(",") if x}:
                for k in keys(name):
                    weight[k][cc] += pop

    out = Path(__file__).resolve().parents[1] / "argus" / "classify" / "cities.tsv.gz"
    n = 0
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as fh:
        for k in sorted(weight):
            cc = max(weight[k].items(), key=lambda kv: kv[1])[0]
            fh.write(f"{k}\t{cc}\n")
            n += 1
    print(f"{n:,} names -> {out} ({out.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "cities5000.txt")
