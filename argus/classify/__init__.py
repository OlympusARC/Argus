"""Role classification: what kind of job is this?

Computed at ingest and stored, so filtering for engineering roles is an index
seek rather than a scan of every open posting. The corpus is 26% engineering
and 1.6% forward-deployed, so the interesting slice is small and the
uninteresting remainder is large -- exactly the shape that punishes a
LIKE '%engineer%' over a growing table.

Rules rather than a model, deliberately. A model here would need an eval set,
a provider, a budget and a regression story before it beat a regex on titles
that are mostly literal. Every row records the RULESET version that produced
it, so raising the version and sweeping rows where classified_by differs is
how a better ruleset gets applied without re-polling anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

"""
Bump on any change to the patterns below. Rows carry this, so a sweep can
find exactly what is stale.
"""
RULESET = "r2"

"""
Hard exclusions come first. The corpus is dominated by Workday enterprise
boards -- retail, clinical and logistics roles outnumber engineering three to
one -- and several of them contain words that would otherwise match, like
'Service Technician' or 'Sales Associate'.
"""
SOFTWARE_SIGNAL = re.compile(
    r"\b(software|swe|sde|sdet|developer|programmer|full[\s-]?stack|"
    r"back[\s-]?end|front[\s-]?end|devops|sre|cloud|kubernetes|"
    r"machine learning|data (engineer|scientist)|firmware|embedded|"
    # Stack names belong here as much as role nouns, because the exclusions
    # collide with them: "server" is in NOT_TECHNICAL for restaurant work and
    # was killing "Server Side Java Engineer" and "SQL Server DBA".
    r"java|python|golang|typescript|kotlin|scala|"
    r"sql|postgres|mysql|oracle|hadoop|"
    r"dba|sysadmin|api|backend|frontend|"
    r"server[\s-]?(engineer|administrator|side))\b"
    r"|\b(c\+\+|c#|\.net)",
    re.I,
)

NOT_TECHNICAL = re.compile(
    r"\b(nurse|nursing|rn|lpn|cna|physician|surgeon|pharmacist|dental|therapist|"
    r"caregiver|patient care|phlebotom|radiolog|sonograph|veterinar|"
    r"cashier|barista|server|busser|dishwasher|cook|chef|baker|butcher|"
    r"retail|store associate|sales associate|stocker|merchandiser|"
    r"driver|trucker|cdl|courier|warehouse|forklift|picker|packer|"
    r"janitor|custodian|housekeep|groundskeep|landscap|"
    r"teacher|tutor|professor|lecturer|substitute|"
    r"security guard|police|firefighter|paramedic|"
    r"plumber|electrician apprentice|hvac|welder|machinist|carpenter|"
    r"weld(ing|er)[\s-]?engineer|manufacturing[\s-]?engineer|"
    r"mechanical[\s-]?engineer|civil[\s-]?engineer|structural[\s-]?engineer|"
    r"industrial[\s-]?engineer|chemical[\s-]?engineer|petroleum[\s-]?engineer|"
    r"mining[\s-]?engineer|environmental[\s-]?engineer|"
    r"field[\s-]?service[\s-]?technician|maintenance[\s-]?technician)\b",
    re.I,
)

"""
The forward-deployed family. Nine spellings for one idea: an engineer who
sits with the customer. Checked before the general engineering patterns
because 'Solutions Engineer' would otherwise land in engineering and lose the
distinction that makes it findable.
"""
FDE = re.compile(
    r"\b(forward[\s-]?deployed"
    r"|solutions?[\s-]?(engineer|architect(ure)?)"
    r"|sales[\s-]?engineer"
    r"|field[\s-]?engineer"
    r"|deployment[\s-]?engineer"
    r"|implementation[\s-]?(engineer|consultant|specialist)"
    r"|customer[\s-]?engineer"
    r"|technical[\s-]?account[\s-]?(manager|executive)"
    r"|partner[\s-]?engineer"
    r"|developer[\s-]?advocate"
    r"|technical[\s-]?consultant"
    r"|deployment[\s-]?strategist"
    r"|forward[\s-]?deployed[\s-]?(engineer|architect|scientist|analyst)?"
    r"|field[\s-]?(application|systems?)[\s-]?engineer"
    r"|integration[\s-]?engineer"
    r"|professional[\s-]?services[\s-]?(engineer|consultant))\b",
    re.I,
)

"""
Artificial intelligence and machine learning, as its own family.

Ordered before DATA and ENGINEERING for the same reason FDE is: 'Machine
Learning Engineer' contains 'engineer' and 'Data Scientist, ML' contains
'data', so whichever pattern runs first absorbs it and the distinction is
lost.

Two tiers on purpose. Strong terms stand alone -- nothing called 'MLOps' is
anything else. The bare token 'ai' does not, because it matched 'AI Visual
Creator' and 'Generative AI Associate' in the corpus, which are marketing and
gig work; it only counts next to a role noun.
"""
AI = re.compile(
    r"\b(machine[\s-]?learning|deep[\s-]?learning|mlops|ml[\s-]?ops|"
    r"reinforcement[\s-]?learning|computer[\s-]?vision|"
    r"natural[\s-]?language[\s-]?processing|"
    r"llm|large[\s-]?language[\s-]?model|"
    r"generative[\s-]?ai|gen[\s-]?ai|genai|"
    r"neural[\s-]?network|transformer[\s-]?model|"
    r"applied[\s-]?scientist|research[\s-]?scientist|research[\s-]?engineer)\b"
    r"|\b(ai|ml|nlp)[\s-]?(engineer|scientist|researcher|developer|architect|"
    r"infrastructure|platform|systems?|ops)\b"
    r"|\b(engineer|scientist|researcher|architect)[,\s-]+(ai|ml)\b",
    re.I,
)

ENGINEERING = re.compile(
    r"\b(software|swe|sde|engineer|engineering|developer|programmer|"
    r"back[\s-]?end|front[\s-]?end|full[\s-]?stack|"
    r"devops|sre|site reliability|platform|infrastructure|systems?[\s-]?engineer|"
    r"embedded|firmware|hardware engineer|asic|fpga|silicon|"
    r"mobile|ios|android|"
    r"architect|technical lead|tech lead|"
    r"qa engineer|test engineer|automation engineer|"
    r"compiler|kernel|distributed systems|"
    r"kubernetes|terraform|cloud engineer|cloud architect|"
    r"graphics engineer|blockchain|smart contract|"
    r"simulation engineer|game (engineer|developer|programmer)|"
    r"sdet|quality assurance engineer|"
    r"member of technical staff|"
    # Stacks and tools, because a real slice of the corpus names the technology
    # and never the role: "Java Lead", "Core Java" and "Oracle DBA" carry no
    # engineer or developer noun for any other pattern to catch.
    #
    # Only unambiguous names are listed bare. "Ruby" is a person and a
    # restaurant chain, "React" is an ordinary verb and "Spark" is an energy
    # company, so those need their qualifier -- a pattern that fires on
    # "React to customer needs" costs more than the roles it finds.
    r"java|python|golang|typescript|javascript|kotlin|scala|perl|"
    r"ruby on rails|react[\s.]?js|react native|node[\s.]?js|angular|vue[\s.]?js|"
    r"django|spring boot|"
    r"hadoop|apache spark|snowflake|databricks|kafka|airflow|"
    r"docker|jenkins|ansible|postgres|mysql|mongodb|"
    r"sysadmin|systems? administrator|network (engineer|administrator)|"
    r"dba|database administrator|"
    r"aws|gcp)\b"
    r"|\brobotics?[\s-]?(engineer|software|developer|scientist|architect)\b"
    r"|\b(software|systems?|controls?)[\s-]?engineer.{0,20}robotic"
    r"|\bazure[\s-]?(engineer|developer|architect|administrator|cloud)\b"
    r"|\b(c\+\+|c#|\.net)"
    # "... Development" only after a technology noun: bare `development`
    # would take "Business Development Representative", which is sales.
    r"|\b(software|api|web|mobile|game|platform|firmware|embedded|full[\s-]?stack)"
    r"[\s-]?development\b",
    re.I,
)

SECURITY = re.compile(
    r"\b(security engineer|appsec|infosec|application security|product security|"
    r"offensive security|penetration[\s-]?test(er|ing)?|pentest(er)?|"
    r"red team|blue team|ethical hack(er|ing)?|bug bounty|"
    r"threat (intel|detection)|incident response|detection engineer|"
    r"cryptograph|security research(er)?|"
    r"network exploitation|reverse engineer(ing)?|malware|"
    r"vulnerability[\s-]?(research(er)?|management)|security (analyst|architect|operations)|"
    r"\bsoc\b|siem|iam engineer|identity and access)\b",
    re.I,
)

DATA = re.compile(
    r"\b(data (engineer|scientist|analyst|architect|scien(ce|tists?))|analytics engineer|"
    r"business intelligence|bi (developer|analyst)|"
    r"quantitative[\s-]?(research(er)?|analy(st|tics)|develop(er|ment)?|trader|"
    r"strategist|engineer)|quant\b|"
    r"statistician|econometric)\b",
    re.I,
)

PRODUCT = re.compile(
    r"\b(product manager|product owner|program manager|technical program manager|"
    r"tpm\b|product management)\b",
    re.I,
)

DESIGN = re.compile(
    r"\b(designer|design lead|ux|ui designer|user experience|user research|"
    r"product design|visual design|brand design)\b",
    re.I,
)

SENIORITY = [
    ("intern", re.compile(r"\b(intern|internship|co[\s-]?op|placement student)\b", re.I)),
    (
        "new_grad",
        re.compile(
            r"\b(new[\s-]?grad|graduate (program|scheme|engineer|developer)|"
            r"entry[\s-]?level|early career|campus|university grad|"
            r"junior|jr\.?|associate engineer|engineer i\b|level 1)\b",
            re.I,
        ),
    ),
    ("executive", re.compile(r"\b(chief|cto|ceo|cio|ciso|vp|vice president|head of)\b", re.I)),
    ("director", re.compile(r"\b(director|sr\.? director|senior director)\b", re.I)),
    ("manager", re.compile(r"\b(manager|management)\b", re.I)),
    ("principal", re.compile(r"\b(principal|distinguished|fellow|architect)\b", re.I)),
    ("staff", re.compile(r"\b(staff|senior staff)\b", re.I)),
    ("lead", re.compile(r"\b(lead|leader)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?|iii|iv|level 3|l[45678])\b", re.I)),
]


@dataclass(slots=True, frozen=True)
class Role:
    family: str
    is_engineering: bool
    is_fde: bool
    seniority: str | None
    ruleset: str = RULESET


def seniority_of(title: str) -> str | None:
    """First match wins, so the list is ordered most-specific first.

    'Senior Engineering Manager' is a manager, not a senior IC, and 'Intern'
    beats every other signal because an internship's level is the point.
    """
    for name, pattern in SENIORITY:
        if pattern.search(title):
            return name
    return None


def classify(title: str | None, department: str | None = None) -> Role:
    """Map a posting to a role family.

    Order is the whole design. Exclusions run first because the corpus is
    mostly non-technical; the forward-deployed family runs before engineering
    because its titles contain 'engineer' and would otherwise be absorbed.
    """
    text = f"{title or ''} {department or ''}".strip()
    if not text:
        return Role("unknown", False, False, None)

    level = seniority_of(title or "")

    """
    An exclusion never beats an explicit software signal: a manufacturing
    engineer is out, a manufacturing systems software engineer is in.
    """
    if NOT_TECHNICAL.search(text) and not SOFTWARE_SIGNAL.search(text):
        return Role("other", False, False, level)
    if FDE.search(text):
        return Role("fde", True, True, level)
    if SECURITY.search(text):
        return Role("security", True, False, level)
    if AI.search(text):
        return Role("ai", True, False, level)
    """
    Data work counts as engineering in r2. A data engineer builds systems and
    a data scientist writes code; excluding 8,629 open postings from every
    engineering filter served nobody looking for technical work.
    """
    if DATA.search(text):
        return Role("data", True, False, level)
    if ENGINEERING.search(text):
        return Role("engineering", True, False, level)
    if PRODUCT.search(text):
        return Role("product", False, False, level)
    if DESIGN.search(text):
        return Role("design", False, False, level)
    return Role("other", False, False, level)
