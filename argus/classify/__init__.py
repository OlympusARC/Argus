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
RULESET = "r4"

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
    # Rescues for the discipline exclusions below. "Quality Engineer" is
    # usually a factory job and "Validation Engineer" usually a lab one, but
    # SDET, QA and firmware versions of both are ours.
    r"qa|sdet|test[\s-]?automation|robotics?|site[\s-]?reliability|"
    r"server[\s-]?(engineer|administrator|side)|"
    # More rescues, each from a title the discipline list wrongly claimed.
    # "control plane" is the one that bit: `controls?` is on that list for
    # industrial controls, and it matched a platform-infrastructure role.
    r"control[\s-]?plane|platform[\s-]engineer|infrastructure[\s-]engineer|"
    # `web` and `mobile` are qualified: bare, they rescued "Mobile Associate
    # - Retail Sales" out of NOT_TECHNICAL, which is a phone shop.
    r"web[\s-]?(developer|engineer|dev)|mobile[\s-]?(developer|engineer|app)|"
    r"ios|android|linux|unix|docker|aws|azure|gcp|terraform|"
    r"micro[\s-]?services?|distributed[\s-]systems?|compiler|observability|"
    r"react|node\.?js|rust|ruby|perl|etl|"
    r"data[\s-](platform|pipeline|scien\w*)|"
    # An AI or data-science role that also names a discipline: "Quality Data
    # Science Co-op", "AI Scientist Intern, Computational Protein Design".
    # The AI and DATA rules run after the exclusion, so without these the
    # exclusion gets there first.
    r"ai[\s-](scientist|engineer|research\w*)|data[\s-]scien\w*)\b"
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
    r"field[\s-]?service[\s-]?technician|maintenance[\s-]?technician)\b",
    re.I,
)

r"""
Engineering that is not software engineering.

The bare token `engineer` in ENGINEERING matches every engineering discipline
there is, so this decides which ones are ours. It replaces a list of
`mechanical[\s-]?engineer` style alternatives that carried a trailing \b and
therefore could not match the -ing form: "Mechanical Engineer" was excluded
and "Mechanical Engineering" was not, which is how 235 adjunct teaching posts
arrived in the digest. Matching `engineer\w*` covers engineer, engineering
and engineers at once.

A discipline here is still rescued by an explicit software signal, because a
manufacturing systems software engineer is ours and a manufacturing engineer
is not. The qualifiers left out are as considered as the ones in: `design`
splits evenly between mechanical and product work, and `reliability` would
have taken Site Reliability Engineer with it.
"""
"""
Disciplines that are engineering but not ours.

The discipline word is deliberately *not* anchored beside "engineer".
Measured against the corpus, requiring adjacency missed four shapes at
once: "Civil/Highway Engineer" (a slash), "Engineer - Water" (reversed),
"Electronics Design Engineer" (a word in between) and titles whose
discipline was simply absent from the list. Anchoring caught 13 of 34,217
open engineering roles; allowing a bounded gap in either direction catches
2,683.

Broadening is only safe because of the guard at the call site: an
exclusion never beats an explicit SOFTWARE_SIGNAL, so "Manufacturing
Systems Software Engineer" survives being full of discipline words.
"""
_DISCIPLINE = (
    r"mechanical|electrical|electro[\s-]?mechanical|electronics?|"
    r"civil|structural|chemical|industrial|manufacturing|process|"
    r"aerospace|aeronautical|astronautical|biomedical|bio[\s-]?medical|"
    r"petroleum|mining|geological|geotechnical|environmental|environment|"
    r"nuclear|agricultural|marine|automotive|materials|metallurg\w*|"
    r"welding|packaging|corrosion|drilling|reservoir|thermal|"
    r"optical|optics|acoustic|propulsion|hydraulic|pneumatic|"
    r"fire[\s-]?protection|architectural|continuous[\s-]?improvement|"
    r"facilities|maintenance|equipment|production|safety|quality|"
    r"project|controls?|validation|calibration|metrology|supplier|"
    r"service|plant|tooling|molding|moulding|textile|water|wastewater|"
    r"highway|traffic|transportation|survey|hvac|piping|"
    r"harness|vehicle|chassis|powertrain|battery|stress|fatigue|"
    r"avionics|payload|mission[\s-]?operations|ground[\s-]?support|"
    r"semiconductor|photonics|antenna|rf|wafer|fab|"
    r"sustainability|utilities|energy|mep|geomatics|"
    r"transmission|distribution|substation|construction|"
    r"spacecraft|gnc|guidance|launch[\s-]?operations|flight[\s-]?test|"
    # Unqualified "Design Engineer" is a hardware role in every
    # sampled case -- mechanical, electrical, ASIC, FPGA, payloads.
    # The software ones say so and are rescued by SOFTWARE_SIGNAL.
    r"design"
)

"""
Bounded gaps rather than `.*`: a long title can hold both a discipline and
an unrelated "engineer", and an unbounded match would join them.
"""
"""
The noun is not only "engineer". An internship names its discipline and
often never says the word: "Silicon Photonics Advanced Packaging Intern",
"Field/Project Operations Internship - Heavy Civil Infrastructure". Those
are the titles the digest exists to keep out, and requiring "engineer" let
every one of them through.
"""
_NOUN = r"(?:engineer\w*|intern(?:ship)?|co[\s-]?op)"

NON_SOFTWARE_ENG = re.compile(
    rf"\b({_DISCIPLINE})\b[\w\s/&,.'()–—·-]{{0,36}}\b{_NOUN}\b"
    rf"|\b{_NOUN}\b[\w\s/&,.'()–—·-]{{0,24}}\b({_DISCIPLINE})\b",
    re.I,
)

"""
Teaching about a subject is not working in it.

Unconditional, and the only exclusion that is: an adjunct professor of
computer science carries every software signal there is and is still a
teaching job. Running this before the software rescue is the whole point --
"Adjunct Instructor in Generative AI and Large Language Models" would
otherwise be filed under ai.
"""
ACADEMIC = re.compile(
    r"\b(adjunct|faculty|professor|lecturer|instructor|"
    r"post[\s-]?doc\w*|post[\s-]?doctoral|tenure[\s-]?track|"
    r"teaching[\s-]?(assistant|associate|fellow)|visiting[\s-]?scholar)\b",
    re.I,
)

"""
Selling a technology is not building it.

Checked after FDE and never before it: a sales engineer, a technical
account manager and a presales solutions architect are forward-deployed
roles and belong in that family. Unconditional once past it, with no
software rescue -- naming a technology is what a technology salesperson
does. What this catches is the layer around them -- "AWS
Presales Specialist", "Partner Development Manager - AWS" -- which reach
ENGINEERING only because it lists `aws` as a bare token.
"""
SALES = re.compile(
    r"\b(account[\s-]?(executive|manager)|business[\s-]?development|"
    # bdm and bdr are unambiguous; sdr is software-defined radio as often as
    # it is a sales development rep -- "DSP / SDR Receiver Engineer" is ours.
    r"bdm|bdr|"
    r"sales[\s-]?(rep(resentative)?|manager|director|specialist|associate|"
    r"consultant|executive|lead|operations)|"
    r"pre[\s-]?sales[\s-]?(specialist|consultant|manager|representative)|"
    r"partner[\s-]?(development|manager)|"
    r"channel[\s-]?(sales|partner|manager)|"
    r"territory[\s-]?manager|inside[\s-]?sales|field[\s-]?sales|"
    r"revenue[\s-]?operations)\b",
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
    Teaching first, and unconditionally. An adjunct professor of computer
    science carries every software signal there is; the rescue below would
    put them in engineering.
    """
    if ACADEMIC.search(text):
        return Role("other", False, False, level)

    """
    Forward-deployed before the remaining exclusions, not after. A sales
    engineer and a presales solutions architect are this family, and SALES
    would otherwise take them on their way past.
    """
    if FDE.search(text):
        return Role("fde", True, True, level)

    """
    An exclusion never beats an explicit software signal: a manufacturing
    engineer is out, a manufacturing systems software engineer is in.
    """
    """
    Sales is excluded outright, like teaching and unlike the disciplines. The
    software rescue exists because a manufacturing systems software engineer
    builds software; a business development manager for Kubernetes does not,
    however many technologies the title names. The roles that both sell and
    build are forward-deployed, and they were decided above.
    """
    if SALES.search(text):
        return Role("other", False, False, level)

    """
    An exclusion never beats an explicit software signal: a manufacturing
    engineer is out, a manufacturing systems software engineer is in.
    """
    if (
        NOT_TECHNICAL.search(text) or NON_SOFTWARE_ENG.search(text)
    ) and not SOFTWARE_SIGNAL.search(text):
        return Role("other", False, False, level)
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
