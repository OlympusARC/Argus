"""Where is this job? US, Europe, elsewhere, or not stated.

Titles are written by people describing a role and are mostly literal.
Locations are written by whatever field an ATS happened to expose, and are
not: the same city arrives as "San Francisco", "San Francisco, CA",
"San Francisco, CA, United States" and "US, CA, San Francisco", while 9.6%
of postings carry no location at all.

So this answers a coarser question than `classify` does -- which of four
buckets -- and it answers `unknown` freely. A location that cannot be read
is a fact about the posting, not a reason to guess at it.

Order is the design, as it is next door. The countries we are not interested
in run first, because "Bangalore, India" and "India - Hyderabad" must not
reach a city list that has never heard of either. Then the explicit US and
European markers, then the bare-city gazetteers that catch "London" and
"San Francisco" written with no country at all -- 64,137 postings name a
single component, and a country is rarely what it is.
"""

from __future__ import annotations

import gzip
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

US = "us"
EUROPE = "europe"
REMOTE = "remote"
OTHER = "other"
UNKNOWN = "unknown"

"""
Explicitly not our target. Listed rather than inferred, because the negative
is what makes the city lists safe: without it, "Birmingham" is a coin toss
between Alabama and England and "Cambridge" between Massachusetts and
Cambridgeshire, but "Birmingham, UK" and "Cambridge, MA" both resolve
before either list is consulted.
"""
NON_TARGET_COUNTRY = re.compile(
    r"\b(india|singapore|china|hong kong|taiwan|japan|korea|vietnam|thailand|"
    r"malaysia|indonesia|philippines|bangladesh|pakistan|sri lanka|nepal|"
    r"australia|new zealand|"
    r"canada|mexico|brazil|argentina|chile|colombia|peru|uruguay|costa rica|"
    r"panama|guatemala|dominican republic|puerto rico|"
    r"israel|turkey|türkiye|uae|united arab emirates|saudi|qatar|kuwait|"
    r"bahrain|oman|jordan|lebanon|egypt|morocco|tunisia|"
    r"south africa|nigeria|kenya|ghana|ethiopia|"
    r"russia|kazakhstan|uzbekistan|georgia country|armenia|azerbaijan)\b",
    re.I,
)

"""
The same for cities. India alone arrives as Bengaluru, Bangalore, Hyderabad,
Pune and Chennai with no country attached in thousands of postings, and a
bare city that is neither ours nor named here would otherwise sit in
`unknown` looking like a gap in the gazetteers rather than a decided answer.
"""
NON_TARGET_CITY = re.compile(
    r"\b(bengaluru|bangalore|hyderabad|pune|chennai|mumbai|bombay|"
    r"new delhi|delhi|noida|gurgaon|gurugram|kolkata|ahmedabad|jaipur|"
    r"kochi|coimbatore|thiruvananthapuram|trivandrum|indore|chandigarh|"
    r"toronto|vancouver|montreal|montréal|ottawa|calgary|edmonton|waterloo, on|"
    r"tel aviv|jerusalem|haifa|herzliya|"
    # Guards the state entry in US_STATE: "Tbilisi, Georgia" must resolve to
    # the country, and NON_TARGET runs first.
    r"tbilisi|batumi|kutaisi|yerevan|baku|"
    # Canadian cities seen in the corpus beyond the six already listed
    r"mississauga|markham|kitchener|oakville|brampton|burnaby|richmond hill|"
    r"halifax|winnipeg|saskatoon|regina|london, on|hamilton, ontario|"
    r"tokyo|osaka|kyoto|yokohama|seoul|busan|"
    r"shanghai|beijing|shenzhen|guangzhou|hangzhou|chengdu|suzhou|wuhan|"
    r"taipei|hsinchu|kaohsiung|"
    r"sydney|melbourne|brisbane|perth|canberra|adelaide|auckland|wellington|"
    r"são paulo|sao paulo|rio de janeiro|belo horizonte|porto alegre|"
    r"mexico city|ciudad de méxico|guadalajara|monterrey|"
    r"buenos aires|santiago|bogot[áa]|lima|montevideo|san jos[ée], costa rica|"
    r"dubai|abu dhabi|doha|riyadh|jeddah|cairo|casablanca|"
    r"johannesburg|cape town|nairobi|lagos|accra|"
    r"kuala lumpur|jakarta|manila|cebu|bangkok|ho chi minh|hanoi|"
    r"dhaka|karachi|lahore|islamabad|colombo|kathmandu)\b",
    re.I,
)

"""
The bare token "US" needs a delimiter or it matches inside ordinary words,
but a comma is not the only one an ATS uses: "Remote - US", "US Remote",
"Remote (US)" and "US/Canada" are all in the corpus and all mean the same
thing. The separator class covers what actually appears rather than what a
schema would have chosen.
"""
US_COUNTRY = re.compile(
    r"\b(united states(?: of america)?|u\.?s\.?a\.?|usa)\b"
    r"|(?:^|[,\-/(\s])us(?:$|[,\-/)\s])",
    re.I,
)

US_STATE = re.compile(
    r"\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|"
    r"delaware|florida|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|"
    r"louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|"
    r"missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|"
    r"new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
    r"rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|"
    # See NON_TARGET_CITY: Tbilisi and Batumi are matched before this runs,
    # which is what lets the state be listed without claiming the country.
    r"georgia|"
    r"virginia|west virginia|wisconsin|wyoming|district of columbia)\b",
    re.I,
)

"""
Two-letter state codes only in a positional slot -- start of the string, after
a comma, or after a space when the code ends the string. Bare `\b(CA|IN|OR)\b`
would take "CA" out of "Casablanca" and, worse, read the ordinary words "in",
"or" and "me" as Indiana, Oregon and Maine.

The space form is why the trailing anchor matters. Workday puts the location
in its URL path as hyphens -- "Southlake-TX" -- so a comma never appears, and
requiring one left 3,426 US postings unplaceable. Allowing a space only at the
end still refuses "Work IN Progress", where the code is followed by a word.
"""
US_STATE_ABBR = re.compile(
    r"(?:^|,\s*|\s)(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|"
    r"MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|"
    r"VT|VA|WA|WV|WI|WY|DC)(?:\s*$|\s*,)",
    # Case-insensitive: an ATS writes "Austin, tx" as readily as "Austin, TX",
    # and matching only the uppercase form silently placed one and not the
    # other. The positional slot is what keeps "or" and "me" out, not the
    # capitals -- and a lowercase "Portland, or" is Oregon anyway.
    re.I,
)

"""
Abbreviations a person writes in a location field and no gazetteer holds.
Kept short and unambiguous: "sf" and "nyc" mean one thing each, where "la"
could be Los Angeles or Louisiana -- both US, so it is safe here, and both
are already covered by the state codes above.
"""
US_SHORTHAND = re.compile(
    r"(?:^|[,\s/|-])(sf|sfo|nyc|nyc metro|bay area|socal|norcal|dmv|"
    r"silicon valley|south bay|east bay|dtla)(?:$|[,\s/|-])",
    re.I,
)

EU_COUNTRY = re.compile(
    r"\b(united kingdom|england|scotland|wales|northern ireland|great britain|"
    r"u\.?k\.?|ireland|france|germany|deutschland|spain|españa|portugal|italy|"
    r"italia|netherlands|holland|belgium|luxembourg|switzerland|schweiz|suisse|"
    r"austria|österreich|denmark|danmark|sweden|sverige|norway|norge|finland|"
    r"suomi|iceland|poland|polska|czech(?:ia| republic)?|slovakia|slovenia|"
    r"hungary|romania|bulgaria|greece|croatia|serbia|estonia|latvia|lithuania|"
    r"ukraine|malta|cyprus|albania|bosnia|montenegro|north macedonia|moldova|"
    r"monaco|andorra|liechtenstein|emea|europe|european union|\beu\b)\b",
    re.I,
)

"""
Bare cities, for the 64,137 postings that name one component and no country.
Deliberately not exhaustive: this is the head of a long tail, and a city that
is not here returns `unknown` rather than a guess.

Names that are a city in both regions are simply absent -- Birmingham,
Cambridge, Manchester, Bristol, Richmond, Windsor, Athens, Naples, Odessa,
Toledo, Frankfort and Dublin are all also American towns, and any of them
alone is unreadable. They resolve when a country or state accompanies them,
which is the common case, and stay unknown when nothing does.
"""
US_CITY = re.compile(
    r"\b(san francisco|sf bay area|bay area|silicon valley|palo alto|mountain view|"
    r"menlo park|sunnyvale|santa clara|san jose|cupertino|redwood city|"
    r"oakland|berkeley|fremont|san mateo|foster city|emeryville|"
    r"los angeles|santa monica|pasadena|irvine|san diego|sacramento|"
    r"new york city|nyc|manhattan|brooklyn|queens|"
    r"seattle|bellevue|redmond|kirkland|tacoma|"
    r"austin|dallas|houston|san antonio|fort worth|plano|irving|"
    r"chicago|evanston|naperville|"
    r"boston|cambridge, ma|somerville|waltham|burlington, ma|lexington, ma|"
    r"denver|boulder|colorado springs|"
    r"atlanta|charlotte|raleigh|durham|research triangle|nashville|"
    r"miami|orlando|tampa|jacksonville|"
    r"philadelphia|pittsburgh|baltimore|arlington, va|reston|mclean|"
    r"herndon|tysons|alexandria, va|"
    r"minneapolis|st\.? paul|detroit|ann arbor|columbus|cleveland|cincinnati|"
    r"indianapolis|kansas city|st\.? louis|milwaukee|omaha|des moines|"
    r"phoenix|scottsdale|tempe|tucson|las vegas|reno|salt lake city|"
    r"portland, or|beaverton|hillsboro|"
    r"new orleans|memphis|louisville|oklahoma city|tulsa|albuquerque|"
    r"boise|anchorage|honolulu|providence|hartford|stamford|"
    r"princeton|newark|jersey city|hoboken|white plains|stamford|"
    r"buffalo|rochester, ny|syracuse|albany, ny|"
    r"madison, wi|iowa city|chapel hill|greenville|"
    r"washington,? d\.?c\.?|silver spring|bethesda|rockville|"
    r"penn state|college station|los gatos|santa cruz|san rafael|"
    r"culver city|el segundo|torrance|carlsbad|"
    r"westlake village|thousand oaks|"
    r"chandler, az|mesa, az|hillsboro|"
    r"morrisville|cary, nc|"
    r"schaumburg|deerfield|"
    r"framingham|marlborough|andover|"
    r"king of prussia|malvern, pa|"
    r"beaverton|vancouver, wa)\b",
    re.I,
)

EU_CITY = re.compile(
    r"\b(london|greater london|shoreditch|canary wharf|"
    r"paris|île[\s-]de[\s-]france|lyon|toulouse|marseille|bordeaux|lille|nantes|"
    r"nice|grenoble|strasbourg|montpellier|rennes|sophia antipolis|"
    r"berlin|munich|münchen|hamburg|frankfurt|cologne|köln|stuttgart|"
    r"düsseldorf|dusseldorf|dresden|leipzig|hannover|nuremberg|nürnberg|"
    r"karlsruhe|heidelberg|darmstadt|walldorf|"
    r"amsterdam|rotterdam|utrecht|eindhoven|the hague|den haag|delft|"
    r"brussels|bruxelles|antwerp|antwerpen|ghent|leuven|"
    r"madrid|barcelona|valencia|seville|sevilla|malaga|málaga|bilbao|zaragoza|"
    r"lisbon|lisboa|porto|braga|"
    r"milan|milano|rome|roma|turin|torino|bologna|florence|firenze|naples, it|"
    r"zurich|zürich|geneva|genève|basel|bern|lausanne|lugano|zug|"
    r"vienna|wien|graz|linz|salzburg|innsbruck|"
    r"copenhagen|københavn|aarhus|odense|"
    r"stockholm|gothenburg|göteborg|malmö|malmo|uppsala|lund|"
    r"oslo|bergen|trondheim|stavanger|"
    r"helsinki|espoo|tampere|oulu|"
    r"reykjavik|reykjavík|"
    r"warsaw|warszawa|krakow|kraków|cracow|wroclaw|wrocław|gdansk|gdańsk|"
    r"poznan|poznań|katowice|lodz|łódź|"
    r"prague|praha|brno|ostrava|bratislava|kosice|"
    r"budapest|debrecen|bucharest|bucurești|cluj|timisoara|timișoara|iasi|"
    r"sofia|plovdiv|varna|"
    r"athens, gr|thessaloniki|zagreb|ljubljana|belgrade|beograd|novi sad|"
    r"tallinn|tartu|riga|vilnius|kaunas|"
    r"kyiv|kiev|lviv|kharkiv|"
    r"dublin, ie|dublin, ireland|cork|galway|limerick|"
    r"edinburgh|glasgow|aberdeen|belfast|cardiff|swansea|"
    r"leeds|sheffield|liverpool|newcastle|nottingham|southampton|brighton|"
    r"oxford|reading, uk|milton keynes|"
    r"luxembourg|valletta|nicosia|monaco|andorra)\b",
    re.I,
)


"""
A work arrangement standing in for a place.

Separate from `unknown` because they are different facts: "Remote" is a
posting that told us how the job is worked and declined to say where, while
`unknown` is a posting that said something none of these lists recognise.
Both are kept under the current policy, but only one of them is a gap in the
gazetteers, and conflating them would hide that.

Checked last of all, so "Remote - US" and "London (Hybrid)" resolve to a
real region first.
"""
REMOTE_ONLY = re.compile(
    r"\b(remote|hybrid|on[\s-]?site|anywhere|work[\s-]from[\s-]home|wfh|"
    r"home[\s-]based|any location|flexible|virtual|distributed|"
    r"worldwide|global|multiple locations|various)\b",
    re.I,
)


def region(location: str | None) -> str:
    """Which bucket a location string falls in.

    Five answers, not two. `other` is the only one that asserts the job is
    somewhere we do not want; `unknown` and `remote` both mean the posting
    cannot be placed, and are kept apart because one is a silent ATS field
    and the other is a deliberate statement about how the work happens.
    """
    if not location or not location.strip():
        return UNKNOWN

    text = location.strip()
    hit = _iso3_prefix(text)
    if hit:
        return hit
    if NON_TARGET_COUNTRY.search(text) or NON_TARGET_CITY.search(text):
        return OTHER
    if US_COUNTRY.search(text) or US_STATE.search(text):
        return US
    """
    A spelled-out country beats a two-letter code. "Tarragona, CT, Spain" was
    read as Connecticut because the abbreviation was tried first and Spain
    never got a look.
    """
    if EU_COUNTRY.search(text):
        return EUROPE
    hit = _code_confirmed_by_city(text)
    if hit:
        return hit
    if US_STATE_ABBR.search(text) or US_SHORTHAND.search(text):
        return US
    if US_CITY.search(text):
        return US
    if EU_CITY.search(text):
        return EUROPE
    """
    The gazetteer runs after the hand-written rules, not instead of them.
    Those encode judgement a population table cannot -- that "EMEA" means
    Europe, that a bare "Remote" is its own answer -- and they are faster.
    This catches what is left: a town nobody thought to list.
    """
    hit = _gazetteer_region(text)
    if hit:
        return hit
    if REMOTE_ONLY.search(text):
        return REMOTE
    return UNKNOWN


"""
Workday tenants often prefix a location with an ISO alpha-3 country code:
"IND-BLR-Divyasree Technopolis", "CAN-ON-Mississauga". Only alpha-3 is read.
Alpha-2 collides with US state codes -- "CA-QC-Longueuil" is Canada and
"CA-San-Jose" is California -- and with company-internal codes like "DLF-"
and "BCA-", so guessing from two letters is worse than not guessing.
"""
_ISO3 = {
    "usa": US,
    "gbr": EUROPE,
    "irl": EUROPE,
    "deu": EUROPE,
    "fra": EUROPE,
    "esp": EUROPE,
    "prt": EUROPE,
    "ita": EUROPE,
    "nld": EUROPE,
    "bel": EUROPE,
    "che": EUROPE,
    "aut": EUROPE,
    "swe": EUROPE,
    "nor": EUROPE,
    "dnk": EUROPE,
    "fin": EUROPE,
    "pol": EUROPE,
    "cze": EUROPE,
    "rou": EUROPE,
    "bgr": EUROPE,
    "grc": EUROPE,
    "hun": EUROPE,
    "ukr": EUROPE,
    "hrv": EUROPE,
    "srb": EUROPE,
    "est": EUROPE,
    "lva": EUROPE,
    "ltu": EUROPE,
    "svk": EUROPE,
    "svn": EUROPE,
    "lux": EUROPE,
    "mlt": EUROPE,
    "cyp": EUROPE,
    "isl": EUROPE,
    "ind": OTHER,
    "can": OTHER,
    "chn": OTHER,
    "jpn": OTHER,
    "kor": OTHER,
    "sgp": OTHER,
    "aus": OTHER,
    "nzl": OTHER,
    "bra": OTHER,
    "mex": OTHER,
    "arg": OTHER,
    "chl": OTHER,
    "col": OTHER,
    "zaf": OTHER,
    "isr": OTHER,
    "are": OTHER,
    "sau": OTHER,
    "tur": OTHER,
    "rus": OTHER,
    "vnm": OTHER,
    "phl": OTHER,
    "tha": OTHER,
    "idn": OTHER,
    "mys": OTHER,
    "egy": OTHER,
    "nga": OTHER,
    "ken": OTHER,
    "pak": OTHER,
    "bgd": OTHER,
    "lka": OTHER,
    "hkg": OTHER,
    "twn": OTHER,
    "per": OTHER,
    "crc": OTHER,
    "cri": OTHER,
}
_ISO3_PREFIX = re.compile(r"^([A-Za-z]{3})[\s-]")


def _iso3_prefix(text: str) -> str | None:
    m = _ISO3_PREFIX.match(text)
    return _ISO3.get(m.group(1).lower()) if m else None


"""
Names are tried longest first so the most specific component wins:
"Washington - Pullman" should be read as Washington, and a short fragment
like "US" or a house number should never decide it.

A whole string or a separator-delimited fragment may return any region. A
bare word split out of one may only return a region we keep.

That asymmetry is the point. A rejection at ingest is irreversible -- the
posting is never stored and cannot be reconsidered -- while a wrong `us` is
merely a row in the wrong bucket. And bare words are where the table is
least trustworthy: GeoNames holds a town called Progress, so "Work IN
Progress" resolved to a country we do not want and the posting would have
been dropped. Allowing bare words to accept but not refuse keeps 801
postings that fragments alone would leave unplaced, without that risk.

Single words are only tried at four characters or more. Below that the hit
rate against a 524,052-name table is mostly accident -- there is a town
called "Ord" -- and a wrong region is worse than none.
"""
_SPLIT = re.compile(r"[,/|()\[\]]+|\s+-\s+|\u2013|\u2014")
_MIN_WORD = 4
"""
The gazetteer stores an ISO country code rather than a region, so a lookup
can answer two questions: where is this place, and does it agree with a
two-letter code written beside it.
"""
_EUROPE_CC = frozenset(
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


def _region_of(cc: str) -> str:
    return US if cc == "US" else EUROPE if cc in _EUROPE_CC else OTHER


def _lookup(table: dict[str, str], cand: str) -> str | None:
    cc = _country_of(cand)
    return _region_of(cc) if cc else None


def _country_of(cand: str) -> str | None:
    table = _gazetteer()
    for key in _keys(cand):
        cc = table.get(key)
        if cc:
            return cc
    return None


def _gazetteer_region(text: str) -> str | None:
    table = _gazetteer()
    if not table:
        return None
    parts = [p.strip() for p in _SPLIT.split(text) if p and p.strip()]
    for cand in sorted({*parts, text}, key=len, reverse=True):
        hit = _lookup(table, cand)
        if hit:
            return hit
    words = {w for p in parts for w in p.split() if len(w) >= _MIN_WORD}
    for cand in sorted(words, key=len, reverse=True):
        hit = _lookup(table, cand)
        if hit and hit != OTHER:
            return hit
    return None


"""
"Munich, DE" is Germany and "Paris, TX" is Texas, and the difference is not
in the code -- it is in whether the city agrees with it.

Five US state codes are also European country codes: AL, DE, MD, ME and MT.
Reading them as states put Munich in Delaware. Reading them as countries
would put Birmingham, Alabama in Albania. So neither is assumed: the city
beside the code is looked up, and the code is believed only when the city
says the same thing. Paris is FR, which is not TX, so Texas keeps it.
"""
_TRAILING_CODE = re.compile(r"^(.*?)[,\s]+([A-Za-z]{2})\s*$")


def _code_confirmed_by_city(text: str) -> str | None:
    m = _TRAILING_CODE.match(text.strip())
    if not m:
        return None
    city, code = m.group(1).strip(), m.group(2).upper()
    if not city:
        return None
    return _region_of(code) if _country_of(city) == code else None


def in_target(location: str | None) -> bool:
    """Whether a posting's location clears the region policy.

    The policy is stated as what to keep, and it keeps everything except a
    posting that positively names somewhere outside the target: `other` is
    the only rejection. An absent location, a bare "Remote" and an
    unrecognised string are all kept, because none of them is evidence that
    the job is elsewhere -- and a posting refused at ingest can never be
    reconsidered, unlike one that is merely filtered out of a query.
    """
    from ..core import config

    return region(location) in config.STORE_REGIONS


"""
A GeoNames extract, built by scripts/build_gazetteer.py and committed so that
ingest never touches the network. 524,062 names at 2.3 MB, loaded once per
process in 0.4 seconds -- which a poll pays once and then reuses across
thousands of postings.

Names are stored both as written and folded to ASCII, because a posting may
name a city in its own script: "\u0421\u043e\u0444\u0438\u044f" is in here, and resolves to Europe.
"""
_DATA = Path(__file__).with_name("cities.tsv.gz")


def _keys(name: str) -> list[str]:
    low = re.sub(r"[^\w ]+", " ", name.lower()).strip()
    folded = re.sub(
        r"[^a-z0-9 ]+",
        " ",
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower(),
    ).strip()
    return [k for k in dict.fromkeys((low, folded)) if len(k) >= 3]


@lru_cache(maxsize=1)
def _gazetteer() -> dict[str, str]:
    """Absent is not fatal: without the file every rule above still applies
    and the answer is simply `unknown` more often."""
    if not _DATA.exists():
        return {}
    out: dict[str, str] = {}
    with gzip.open(_DATA, "rt", encoding="utf-8") as fh:
        for line in fh:
            key, _, code = line.rstrip("\n").partition("\t")
            if code:
                out[key] = code
    return out
