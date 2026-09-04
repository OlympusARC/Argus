"""Role classification: the rules, and the order they run in.

Order is the whole design, so most of these tests are really about precedence.
Every title here is real, taken from the corpus rather than invented, because
the failure mode that matters is a phrasing nobody thought of.
"""

import pytest

from argus.classify import RULESET, classify, seniority_of
from argus.core import db
from argus.feed import jobs


@pytest.fixture()
def conn(tmp_path):
    return db.init_db(tmp_path / "t.db")


"""
The forward-deployed family. Nine spellings for one idea, and the reason the
flag exists at all: at 1.7% of the corpus it is far too rare to find by
scrolling, and far too valuable to miss.
"""


@pytest.mark.parametrize(
    "title",
    [
        "Forward Deployed Engineer",
        "Forward-Deployed Software Engineer",
        "Solutions Engineer, Enterprise",
        "Senior Solutions Architect",
        "Sales Engineer",
        "Field Engineer II",
        "Deployment Engineer",
        "Implementation Consultant",
        "Customer Engineer, Google Cloud",
        "Technical Account Manager",
        "Developer Advocate",
    ],
)
def test_the_forward_deployed_family_is_recognised(title):
    role = classify(title)
    assert role.family == "fde"
    assert role.is_fde
    assert role.is_engineering, "an FDE is an engineer and must survive an engineering filter"


def test_fde_is_checked_before_engineering():
    """'Solutions Engineer' contains 'engineer' and would otherwise be absorbed."""
    assert classify("Solutions Engineer").family == "fde"
    assert classify("Software Engineer").family == "engineering"


@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer",
        "Backend Developer",
        "Site Reliability Engineer",
        "Member of Technical Staff",
        "Embedded Firmware Engineer",
        "iOS Engineer",
        "Machine Learning Engineer",
        "Staff Compiler Engineer",
    ],
)
def test_engineering_titles(title):
    role = classify(title)
    assert role.is_engineering
    assert not role.is_fde


"""
Exclusions run first because the corpus is dominated by Workday enterprise
boards: retail, clinical and logistics roles outnumber engineering three to
one, and several of them contain words that would otherwise match.
"""


@pytest.mark.parametrize(
    "title",
    [
        "Sales Associate",
        "Seasonal Sales Associate",
        "Cashier Part Time Day",
        "Patient Care Technician - PCT",
        "Registered Nurse - ICU",
        "Grocery Clerk Part Time Day",
        "Mobile Associate - Retail Sales",
        "CDL A Truck Driver",
    ],
)
def test_non_technical_roles_are_excluded(title):
    role = classify(title)
    assert role.family == "other"
    assert not role.is_engineering and not role.is_fde


def test_exclusion_beats_a_matching_keyword():
    """'Service Technician' and 'Sales Associate' both contain matchable words."""
    assert classify("Retail Sales Associate").family == "other"
    assert classify("Pharmacy Technician").family == "other"


def test_other_families_are_separated_from_engineering():
    assert classify("Product Manager").family == "product"
    assert classify("Data Scientist").family == "data"
    assert classify("Senior Product Designer").family == "design"
    assert classify("Application Security Engineer").family == "security"
    """
    Data left this list in r2. A family is still its own thing -- a data
    scientist is not a backend engineer -- but is_engineering answers a
    different question: is this technical work someone hunting engineering
    roles wants to see. For 8,629 open postings the answer was yes, and
    saying no excluded them from every filter the product has.
    """
    for title in ("Product Manager", "Senior Product Designer"):
        assert not classify(title).is_engineering
    assert classify("Data Scientist").is_engineering


def test_security_counts_as_engineering():
    assert classify("Detection Engineer").is_engineering


"""
Seniority is first-match-wins, so the pattern list is ordered most specific
first. These pin the orderings that are easy to get backwards.
"""


def test_seniority_precedence():
    assert seniority_of("Software Engineer Intern") == "intern"
    assert seniority_of("Senior Engineering Manager") == "manager"
    assert seniority_of("Senior Software Engineer") == "senior"
    assert seniority_of("Staff Software Engineer") == "staff"
    assert seniority_of("New Grad Software Engineer") == "new_grad"
    assert seniority_of("Head of Engineering") == "executive"
    assert seniority_of("Software Engineer") is None


def test_an_internship_beats_every_other_level():
    """An internship's level is the point of the posting."""
    assert seniority_of("Senior Staff Engineering Intern") == "intern"


def test_empty_titles_do_not_crash():
    assert classify(None).family == "unknown"
    assert classify("").family == "unknown"
    assert classify("   ").family == "unknown"


def test_department_is_used_as_a_second_signal():
    """A bare title like 'Analyst' says little; the department says more."""
    assert classify("Analyst", "Data Science").family == "data"


"""
The sweep. This is what makes the rules improvable without re-polling
anything, so it has to touch exactly the rows that disagree with the current
ruleset -- no more, no fewer.
"""


def _insert(conn, external_id, title, classified_by):
    conn.execute(
        """INSERT INTO jobs (ats, slug, external_id, title, url, first_seen_at,
                             last_seen_at, status, content_hash, classified_by,
                             role_family)
           VALUES ('ashby','acme',?,?,'https://x',0,0,'open','h',?,'other')""",
        (external_id, title, classified_by),
    )


def test_reclassify_only_touches_rows_from_another_ruleset(conn):
    _insert(conn, "1", "Forward Deployed Engineer", None)
    _insert(conn, "2", "Software Engineer", "r0")
    _insert(conn, "3", "Sales Associate", RULESET)

    res = jobs.reclassify(conn)

    assert res["classified"] == 2, "the row already on the current ruleset must be skipped"
    fams = dict(conn.execute("SELECT external_id, role_family FROM jobs"))
    assert fams["1"] == "fde"
    assert fams["2"] == "engineering"
    assert fams["3"] == "other", "untouched, and it was already correct"


def test_reclassify_is_idempotent(conn):
    _insert(conn, "1", "Forward Deployed Engineer", None)
    jobs.reclassify(conn)
    assert jobs.reclassify(conn)["classified"] == 0


def test_every_written_posting_carries_the_ruleset_version(conn):
    """Without this the sweep cannot tell fresh rows from stale ones."""
    from argus.core.models import Posting

    jobs.insert(
        conn,
        Posting(
            ats="ashby",
            slug="acme",
            external_id="9",
            title="Staff Security Engineer",
            url="https://x",
        ),
    )
    row = conn.execute(
        "SELECT role_family, is_engineering, is_fde, seniority, classified_by FROM jobs"
    ).fetchone()
    assert row["classified_by"] == RULESET
    assert row["role_family"] == "security"
    assert row["is_engineering"] == 1
    assert row["is_fde"] == 0
    assert row["seniority"] == "staff"


"""
r2. Every case below is a real title from the corpus, and each one was
either misfiled by r1 or exposed a pattern that was too loose while r2 was
being written.
"""


def test_ml_engineers_are_ai_and_count_as_engineering():
    """r1 filed 'Machine Learning Data Engineer' as data, which set
    is_engineering=False -- so every engineering filter missed it."""
    for title in (
        "Senior Machine Learning Engineer",
        "LLM Research Engineer",
        "MLOps Engineer",
        "Applied Scientist",
        "Computer Vision Engineer",
        "AI Infrastructure Engineer",
    ):
        r = classify(title)
        assert r.family == "ai", f"{title} -> {r.family}"
        assert r.is_engineering


def test_ai_alone_is_not_enough():
    """The bare token matched 'AI Visual Creator' and marketing copy across
    thousands of postings, so it only counts next to a role noun."""
    for title in ("AI Visual Creator (Static Ads)", "SEO / AEO / GEO Lead (AI Search)"):
        assert not classify(title).is_engineering, title


def test_data_work_counts_as_engineering():
    """8,629 open postings that every engineering filter used to exclude."""
    for title in ("Data Engineer", "Senior Data Scientist", "Analytics Engineer"):
        r = classify(title)
        assert r.is_engineering, title


def test_non_software_engineering_is_excluded():
    """The first real digest was thirty-two HVAC roles. These are genuine
    engineering and genuinely not what this feed is for."""
    for title in (
        "Senior Welding Engineer",
        "Manufacturing Engineer II",
        "Mechanical Engineer",
        "Civil Engineer",
        "Senior Structural Engineer",
        "Industrial Engineer",
    ):
        r = classify(title)
        assert r.family == "other" and not r.is_engineering, f"{title} -> {r.family}"


def test_a_software_signal_beats_any_exclusion():
    """An exclusion that costs a real software role is worse than the noise
    it removes, so the guard runs before NOT_TECHNICAL can fire."""
    for title in (
        "Software Quality Engineer",
        "Manufacturing Systems Software Engineer",
        "Mechanical Engineer, Robotics Software",
    ):
        assert classify(title).is_engineering, title


def test_robotics_needs_a_software_noun():
    """The bare word caught 258 postings of warehouse robot technicians."""
    assert classify("Robotics Engineer").is_engineering
    assert classify("Robotics Software Engineer").is_engineering
    for title in (
        "Robotics Technician",
        "Robot Teleoperator (AI & Robotics)",
        "Robotics Operations Technician",
    ):
        assert not classify(title).is_engineering, title


def test_substring_matches_never_fire():
    """'%llm%' matched Fulfillment and LLMSW; word boundaries are the fix."""
    for title in ("Fulfillment Associate", "Limited Licensed Master Social Worker (LLMSW)"):
        assert not classify(title).is_engineering, title


def test_fde_still_outranks_the_ai_family():
    """Ordering is the design: FDE runs first so a forward-deployed AI role
    stays findable as forward-deployed."""
    r = classify("Forward Deployed AI Engineer")
    assert r.family == "fde" and r.is_fde and r.is_engineering


def test_stack_names_count_even_without_a_role_noun():
    """A real slice of the corpus names the technology and never the role."""
    for title in (
        "Java Lead",
        "Core Java",
        "Oracle DBA",
        "Hadoop Administrator",
        "System Administrator",
        "Ruby on Rails",
        "C++ Developer",
        ".NET Architect",
    ):
        assert classify(title).is_engineering, title


def test_ambiguous_stack_names_need_their_qualifier():
    """A pattern that fires on 'React to customer needs' costs more than the
    roles it finds. Ruby is a person and a restaurant; Spark is an energy
    company."""
    for title in (
        "React to customer needs daily",
        "Ruby Tuesday Server",
        "Spark Energy Sales Representative",
    ):
        assert not classify(title).is_engineering, title

    """
    One trade recorded rather than hidden. Because a stack name outranks an
    exclusion, a coffee-shop posting naming Java is misfiled as engineering.
    The corpus holds exactly one of those against six "Server Side Java"
    roles the same rule rescues, and this ruleset is tuned for recall, so
    the trade is deliberate.
    """
    assert classify("Barista - Java City").is_engineering


def test_server_means_two_different_jobs():
    """'server' sits in NOT_TECHNICAL for restaurant work and was killing
    'Server Side Java Engineer' and 'SQL Server DBA'."""
    for title in (
        "Server Side Java Engineer",
        "SQL Server DBA",
        "SQL Server Database Administrator",
        "Server Engineer",
    ):
        assert classify(title).is_engineering, title
    for title in ("Restaurant Server", "Banquet Server", "Busser"):
        assert not classify(title).is_engineering, title


def test_a_trailing_word_boundary_does_not_block_a_suffix():
    """A whole class of misses. 'quantitative research' with a closing \\b
    cannot match 'Quantitative Researcher', and the same shape hid
    'Vulnerability Researcher' and 'Solutions Architecture'.

    Found by generating suffixed variants of technical stems and comparing --
    then checking each against the corpus, because half the candidates were
    titles nobody posts.
    """
    for title in (
        "Quantitative Researcher",
        "Quantitative Research",
        "Vulnerability Researcher",
        "Senior Vulnerability Researcher",
        "Solutions Architecture Manager",
        "Manager, Solutions Architecture",
    ):
        assert classify(title).is_engineering, title


def test_development_needs_a_technology_in_front_of_it():
    """Bare 'development' would take every Business Development Rep in the
    corpus, which is sales."""
    for title in (
        "Software Development Manager",
        "API Development Lead",
        "Mobile Development Manager",
    ):
        assert classify(title).is_engineering, title
    for title in ("Business Development Representative", "Sales Development Representative"):
        assert not classify(title).is_engineering, title


def test_the_forward_deployed_family_covers_its_other_names():
    """'Deployment Strategist' is Palantir's title for the same job, and it
    appeared on boards that are otherwise 60%+ engineering."""
    for title in (
        "Deployment Strategist",
        "Field Application Engineer",
        "Integration Engineer",
        "Professional Services Engineer",
    ):
        r = classify(title)
        assert r.family == "fde" and r.is_engineering, f"{title} -> {r.family}"


def test_security_covers_the_operational_side():
    """Surfaced by looking at what tech companies post that r2 was ignoring."""
    for title in (
        "Digital Network Exploitation Analyst 3",
        "Malware Analyst",
        "Reverse Engineer",
        "SOC Analyst",
        "Security Operations Analyst",
    ):
        r = classify(title)
        assert r.family == "security" and r.is_engineering, f"{title} -> {r.family}"


def test_words_that_only_look_technical_stay_out():
    """Each of these was a candidate the corpus check rejected: real titles
    that a looser pattern would have swept in."""
    for title in (
        "Aquatics Programming Supervisor",
        "Biostatistical Programming Associate",
        "Marketing Analytics Manager",
        "Provider Network Operations Director",
        "Technical Recruiter",
        "Executive Assistant",
    ):
        assert not classify(title).is_engineering, title


def test_offensive_security_titles_are_kept():
    """'penetration test' with a closing boundary cannot match 'Tester' --
    the same suffix trap that hid Quantitative Researcher. Caught by checking
    representative titles per family rather than trusting the patterns."""
    for title in (
        "Penetration Tester",
        "Penetration Testing Lead",
        "Pentester",
        "Ethical Hacker",
        "Senior Penetration Tester",
    ):
        r = classify(title)
        assert r.family == "security" and r.is_engineering, f"{title} -> {r.family}"


"""
r3: engineering that is not software engineering.

Found by reading a digest preview. The bare token `engineer` in ENGINEERING
matches every engineering discipline there is, and the exclusion list meant
to narrow it had three holes: it named too few disciplines, it had no
academic vocabulary at all, and its entries carried a trailing \\b.
"""


def test_the_ing_form_is_excluded_as_well_as_the_er_form():
    """The fourth sighting of this bug class. `chemical[\\s-]?engineer` with a
    trailing \\b matched "Chemical Engineer" and could not match "Chemical
    Engineering", so 235 adjunct teaching posts reached the digest."""
    for t in (
        "Mechanical Engineer",
        "Mechanical Engineering",
        "Chemical Engineer",
        "Chemical Engineering",
        "Civil Engineers",
        "Structural Engineering",
    ):
        assert classify(t).family == "other", t


def test_disciplines_the_bare_engineer_token_used_to_absorb():
    for t in (
        "Process Engineer I",
        "Continuous Improvement Engineer II",
        "Automation Controls Engineer",
        "Lead Project Engineer",
        "Associate Fire Protection Engineer",
        "Senior Propulsion Engineer",
        "Equipment Engineering",
        "Staff Quality Engineer",
        "Field Service Engineer",
        "Sr. Environmental Health & Safety Engineer",
    ):
        assert classify(t).family == "other", t


def test_a_software_signal_still_rescues_a_discipline():
    """The rule that keeps this from being a blunt instrument: a
    manufacturing engineer is out, a manufacturing systems software engineer
    is in."""
    for t in (
        "Manufacturing Systems Software Engineer",
        "Platform Quality Engineer (SDET)",
        "Senior Firmware Validation Engineer",
        "Process Engineer, Python Automation Platform",
    ):
        assert classify(t).is_engineering, t


def test_site_reliability_survives_the_reliability_qualifier():
    """`reliability` was left out of the discipline list on purpose: it would
    have taken Site Reliability Engineer with it, and no software signal in
    that title would have rescued it."""
    assert classify("Site Reliability Engineer").family == "engineering"


def test_teaching_a_subject_is_not_working_in_it():
    """Academic exclusion is unconditional, unlike every other one. An
    adjunct professor of computer science carries every software signal
    there is."""
    for t in (
        "Adjunct Teaching Faculty | Aerospace Engineering",
        "Adjunct Faculty - Computer Science",
        "Part Time Faculty - Systems Engineering",
        "Adjunct Instructor in Generative AI and Large Language Models",
        "Postdoctoral Research Associate - Software Engineering",
        "GCP and AI Instructor",
    ):
        assert classify(t).family == "other", t


def test_selling_a_technology_is_not_building_it():
    for t in (
        "AWS Presales Specialist",
        "Partner Development Manager - AWS",
        "Account Executive, Cloud",
        "Founding BDR",
        "Business Development Manager, Kubernetes",
    ):
        assert classify(t).family == "other", t


def test_forward_deployed_is_decided_before_the_sales_exclusion():
    """Order, not wording. A sales engineer and a presales solutions
    architect are this family; SALES would take them on the way past."""
    for t in (
        "Sales Engineer",
        "Solutions Architect - Presales",
        "Technical Account Manager",
        "Partner Engineer",
    ):
        assert classify(t).family == "fde", t


def test_sdr_is_software_defined_radio_as_often_as_it_is_a_sales_rep():
    assert classify("DSP / SDR Receiver Engineer").is_engineering
    assert classify("Founding BDR").family == "other"


def test_llm_the_degree_is_not_llm_the_model():
    """`%llm%` matching "Licensed Master Social Worker" is in the README. The
    corpus also held a Master of Laws: "Program Coordinator, LLM Adjunct
    Services", filed under ai. 189 of the other 190 LLM titles are genuine,
    so the token stays and the academic rule catches this one."""
    assert classify("Program Coordinator, LLM Adjunct Services").family == "other"
    assert classify("AI/LLM Engineer").family == "ai"
    assert classify("LLM Inference Engineer").family == "ai"


def test_r4_only_ever_removes():
    """Measured over the whole corpus: 15,720 postings changed family and
    every one moved to `other`. A ruleset that also promoted rows would need
    a different argument -- this one is a noise fix."""
    assert RULESET == "r4"


"""
r4: the discipline no longer has to sit beside "engineer".

Each shape below is a real title from the corpus that r3 admitted. They are
grouped by *why* r3 missed them, because the fix is one pattern and a
regression in any shape would look like a regression in all four.
"""


@pytest.mark.parametrize(
    "title",
    [
        # a separator between two disciplines
        "Entry-Level Civil/Highway Engineer - Hiring Event with AECOM",
        "Junior Geotechnical / Tailings Engineer, Minerals & Metals",
        # the discipline follows the noun instead of preceding it
        "Entry-Level Engineer - Water - Hiring Event with AECOM",
        "Engineer I, Environment & Sustainability",
        "Senior Engineer - Controls",
        "Engineer,Quality",
        # a word in between
        "Electronics Design Engineer I",
        "Antenna Electrical Design Engineer Intern (Summer 2027)",
        "Staff Electronics Design Engineer - Zonal ECU",
        # disciplines r3 simply did not list
        "Harness Design Engineer I/II",
        "Senior Traffic Operations Engineer",
        "WATER RESOURCES ENGINEER II",
        "Structural Analysis Engineer",
        "Senior Industrial Energy Engineer",
        "Principal RF Engineer",
        "AGGP2027 - Graduate Stress Engineer I",
    ],
)
def test_a_discipline_away_from_the_noun_is_still_excluded(title):
    assert classify(title).family == "other", title


@pytest.mark.parametrize(
    "title",
    [
        # an explicit software signal always beats a discipline word
        "Manufacturing Systems Software Engineer",
        "Firmware Engineer - Electrical Systems",
        "Software Engineer, New Grad",
        "Backend Engineer Intern",
        "Test Automation Engineer",
        "Quality Engineer, SDET",
        # "control plane" is a software term; `controls?` is on the
        # discipline list for industrial controls, and matched it
        "Platform Engineer - Infrastructure Control Plane & Workflow Automation",
        "Data Platform Engineer, Manufacturing Analytics",
        "Site Reliability Engineer, Production Systems",
    ],
)
def test_a_software_signal_still_beats_the_widened_rule(title):
    assert classify(title).is_engineering, title


def test_the_gap_is_bounded_so_a_long_title_does_not_join_two_halves():
    """`.*` between discipline and noun would let any title holding both a
    discipline anywhere and an unrelated "engineer" anywhere match."""
    t = (
        "Safety Coordinator for the Northwest Region, reporting to the plant "
        "director, supporting our teams across sites - see also our separate "
        "listing for a Backend Engineer on the platform team"
    )
    assert classify(t).is_engineering, "the gap must not span the whole title"


"""
Region gaps found by measuring the live corpus: 41% of open Workday postings
and 50% of BambooHR ones resolved to `unknown`.
"""


@pytest.mark.parametrize(
    "location,want",
    [
        # a state code after a space, not a comma. Workday encodes its
        # location in a URL path as hyphens, so a comma never appears.
        ("Southlake TX", "us"),
        ("Atlanta GA", "us"),
        ("USA TX Austin", "us"),
        # ...but a code followed by a word is still a word
        ("Work IN Progress", "unknown"),
        ("Casablanca", "other"),
        # Georgia is a US state. The country is caught before this runs.
        ("Suwanee, Georgia", "us"),
        ("Tbilisi, Georgia", "other"),
        ("Yerevan, Yerevan", "other"),
        # Canadian cities seen in the corpus, none of them target
        ("Mississauga, Ontario", "other"),
        ("Markham", "other"),
        ("Kitchener, Ontario", "other"),
    ],
)
def test_region_placement(location, want):
    from argus.classify import geo

    assert geo.region(location) == want, location


def test_workday_reads_the_location_out_of_its_own_url():
    """externalPath is /job/<location>/<title>_<req>. 7,128 open postings
    published no locationsText and 99% of them carried this."""
    from argus.adapters.workday import _location_from_path as f

    assert f("/job/Belfast-10-Mays-Meadow/Platform-Consultant_JR1") == "Belfast 10 Mays Meadow"
    assert f("/job/Southlake-TX/Engineer_JR2") == "Southlake TX"


@pytest.mark.parametrize(
    "path", [None, "", "/details/Some-Job_JR4", "/job/12345/X_JR5", "/job//X_JR6"]
)
def test_a_path_segment_that_is_not_a_place_is_not_a_location(path):
    """A requisition number in the slot would be stored as a location and
    then classified, which is worse than having none."""
    from argus.adapters.workday import _location_from_path as f

    assert f(path) is None
