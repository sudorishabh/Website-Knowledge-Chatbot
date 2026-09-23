"""The six web-retrieval benchmark questions, as data.

Each :class:`Benchmark` records what a correct answer rests on and how it must
be reached: the expected sources (and whether the corpus already holds them),
the facts the answer must state, how its dates must be handled, whether the web
should be consulted and why, and whether more than one document is needed.

Sources were verified on 2026-09-23 against the live catalog, the live Qdrant
collection and the live teriin.org pages; each records how. Two expectations
differ from the brief that proposed these questions, both because the corpus
turned out to hold more than the brief assumed:

* **T4** — the brief expected a web search. The article is in the corpus
  (ingested, dated 2026-08-25), ranks first for the question, and its paragraph
  satisfies the passage check (coverage 0.889: the user's "replacing aquafeed"
  is "displacing ... aqua feeds" in the text). So the web is *not* consulted;
  it remains the fallback if the passage were not found.
* **T2** — the brief called the source "TERI's 2025 air-quality article". It is
  "Bridging the Gap: The Urgent Need for Enhanced Public Transport in India's Air
  Quality Mission" (9 April 2025), which reports figures from the TERI-ARAI
  source apportionment study, whose monitoring ran in 2016-17. That gap between
  publication year and data year is itself what the answer must preserve.

``evidence`` is a snapshot of what internal retrieval returned for the question
(real excerpts, abbreviated), so the web decision can be tested
deterministically; ``web_evidence`` is the same for the page the web supplies.
"""
from __future__ import annotations

from dataclasses import dataclass, field

CORPUS = "corpus"
WEB = "web"

# One character inside a sentence: anything but a full stop, or a decimal point
# (the "." in "PM2.5" does not end a sentence).
_IN_SENTENCE = r"(?:[^.]|\.(?=\d))"


def _seasonal(season: str, share: str, pollutant: str) -> str:
    """A share stated for one pollutant and one season, in either order, within
    one sentence: "24% of PM10 ... in winter" or "in winter, PM10 was 24%"."""
    pair = (rf"(?:{share}\s?%{_IN_SENTENCE}{{0,15}}{pollutant}"
            rf"|{pollutant}{_IN_SENTENCE}{{0,15}}{share}\s?%)")
    return (rf"{season}{_IN_SENTENCE}{{0,160}}{pair}"
            rf"|{pair}{_IN_SENTENCE}{{0,160}}{season}")


@dataclass(frozen=True)
class Source:
    title: str
    url: str
    held_by: str                     # "corpus" (ingested) or "web" (fetched at query time)
    published: str | None            # the date the document states or carries
    data_period: str | None = None   # the period its figures describe, when different
    verified: str = ""


@dataclass(frozen=True)
class Evidence:
    """One internal candidate, as retrieval returned it."""

    title: str
    text: str
    date: str | None = None
    bundle: str = "news"
    source_type: str = "website"


@dataclass(frozen=True)
class Benchmark:
    id: str
    question: str
    sources: tuple[Source, ...]
    #: Regular expressions an answer must match, one per fact (case-insensitive).
    facts: tuple[str, ...]
    date_handling: str
    web_should_trigger: bool
    #: The decision reason expected when the web triggers.
    web_reason: str | None
    multi_document: bool
    #: Planner signals the question must raise (name -> expected value).
    signals: dict = field(default_factory=dict)
    evidence: tuple[Evidence, ...] = ()
    web_evidence: str = ""
    notes: str = ""


BENCHMARKS: tuple[Benchmark, ...] = (
    Benchmark(
        id="T1",
        question="Who is Mr Sayanta Ghosh at TERI, and how many publications does he have?",
        sources=(Source(
            title="Mr Sayanta Ghosh | TERI",
            url="https://www.teriin.org/user/15680",
            held_by=WEB, published=None,
            verified="catalog: absent (people bundle holds 8 rows); live page fetched",
        ),),
        facts=(r"associate\s+fellow", r"geospatial", r"more\s+than\s+25\s+publications"),
        date_handling=("The profile states no date: the answer must not attach one, and "
                       "must not present the corpus's 50 authored-document rows as his "
                       "publication count — the profile's own statement is the source."),
        web_should_trigger=True, web_reason="no_profile", multi_document=False,
        signals={"person": "Sayanta Ghosh", "org_scoped": True, "forced": False},
        # Measured: of the 40 internal candidates none mentions him; the best are
        # other speakers' bionotes.
        evidence=(Evidence(
            title="Bionotes", bundle="events", source_type="pdf_attachment",
            text="His professional affiliations include a Life Fellow Membership of the "
                 "Indian Institute of Materials Management (IIMM). Dr Gopal K Sarangi is "
                 "an Associate Professor and Head of the Department of Policy and "
                 "Management Studies.",
            date="2024-07-08"),),
        web_evidence=("Mr Sayanta Ghosh is a Associate Fellow and Area Convener for the "
                      "Centre for Geospatial Technology Application (CGTA) in the Land "
                      "Resources Division. He has more than 25 publications to his credit "
                      "in various reputed scientific journals and international conference "
                      "proceedings."),
    ),
    Benchmark(
        id="T2",
        question=("According to TERI, how much do vehicular emissions contribute to "
                  "Delhi's particulate pollution?"),
        sources=(Source(
            title="Bridging the Gap: The Urgent Need for Enhanced Public Transport in "
                  "India's Air Quality Mission",
            url="https://teriin.org/article/bridging-gap-urgent-need-enhanced-public-"
                "transport-indias-air-quality-mission",
            held_by=CORPUS, published="2025-04-09", data_period="2016-17 (TERI-ARAI study)",
            verified="catalog document 39bd4dba…; sentence present in its Qdrant chunk",
        ),),
        facts=(_seasonal("winter", "24", r"pm\s?10"),
               _seasonal("winter", "28", r"pm\s?2\.5"),
               _seasonal("summer", "15", r"pm\s?10"),
               _seasonal("summer", "17", r"pm\s?2\.5")),
        date_handling=("Four figures, by season and pollutant — never collapsed into one "
                       "\"28%\". Published 2025; the figures come from the 2016-17 study."),
        web_should_trigger=False, web_reason=None, multi_document=False,
        signals={"org_scoped": True, "forced": False, "person": None},
        evidence=(Evidence(
            title="Bridging the Gap: The Urgent Need for Enhanced Public Transport in "
                  "India's Air Quality Mission", bundle="article", date="2025-04-09",
            text="Delhi's vehicular emissions contribute 24% of PM10 and 28% of PM2.5 "
                 "levels in winter, and 15% of PM10 and 17% of PM2.5 in summer."),),
    ),
    Benchmark(
        id="T3",
        question=("What did TERI report about transport's contribution to Delhi air "
                  "pollution around 2019, and what did TERI say in 2025?"),
        sources=(
            Source(
                title="Developing Strategies for Control of Air Pollution in India and "
                      "its Cities",
                url="https://teriin.org/project/developing-strategies-control-air-"
                    "pollution-india-and-its-cities",
                held_by=CORPUS, published="2018-06-26 (CMS page date)", data_period="2019",
                verified="full-text match in the corpus",
            ),
            Source(
                title="Bridging the Gap: The Urgent Need for Enhanced Public Transport "
                      "in India's Air Quality Mission",
                url="https://teriin.org/article/bridging-gap-urgent-need-enhanced-"
                    "public-transport-indias-air-quality-mission",
                held_by=CORPUS, published="2025-04-09", data_period="2016-17",
                verified="catalog document 39bd4dba…",
            ),
        ),
        facts=(r"23\s?%", r"winter", r"2019", r"2025"),
        date_handling=("Two study contexts, not one time series: 2019 = transport 23% of "
                       "winter-season PM2.5 in NCT Delhi (a page whose CMS date is 2018); "
                       "2025 = the article's seasonal PM2.5/PM10 shares, from a 2016-17 "
                       "study. The answer must say they are not directly comparable."),
        web_should_trigger=False, web_reason=None, multi_document=True,
        signals={"comparison": True, "years": (2019, 2025), "forced": False},
        evidence=(
            Evidence(
                title="Developing Strategies for Control of Air Pollution in India and "
                      "its Cities", bundle="completed_projects", date="2018-06-26",
                text="Transport (23%), industries including power plants (23%), and "
                     "biomass burning (14%), are the major contributors to winter season "
                     "PM2.5 concentrations in NCT Delhi during 2019."),
            Evidence(
                title="Bridging the Gap", bundle="article", date="2025-04-09",
                text="Delhi's vehicular emissions contribute 24% of PM10 and 28% of PM2.5 "
                     "levels in winter, and 15% of PM10 and 17% of PM2.5 in summer."),
        ),
    ),
    Benchmark(
        id="T4",
        question=("Find the TERI article containing a paragraph about seaweed biomass "
                  "replacing aquafeed, NPK fertilizer, and Asparagopsis cattle feed."),
        sources=(Source(
            title="Carbon Sequestration by Seaweed is Not a Straightforward Pathway",
            url="https://teriin.org/article/carbon-sequestration-seaweed-not-"
                "straightforward-pathway",
            held_by=CORPUS, published="2026-08-25",
            verified="catalog document 491ae95c…; ranks first; passage coverage 0.889",
        ),),
        facts=(r"carbon\s+sequestration\s+by\s+seaweed", r"aqua\s?feeds?", r"npk",
               r"asparagopsis"),
        date_handling="The article's own date (2026-08-25); no data-year claim is asked for.",
        web_should_trigger=False, web_reason=None, multi_document=False,
        signals={"find_passage": True, "forced": False},
        evidence=(Evidence(
            title="Carbon Sequestration by Seaweed is Not a Straightforward Pathway",
            bundle="article", date="2026-08-25",
            text="The second pathway of mitigation services involves post-harvest use of "
                 "seaweed biomass displacing carbon-intensive products like aqua feeds. "
                 "For instance, seaweed-based soil additive can be a potential substitute "
                 "of nitrogen-phosphorous-potassium (NPK) fertilizer reducing N2O "
                 "emissions or cattle feed with Asparagopsis sp. significantly lowers CH4 "
                 "emission."),),
        notes="Author per the brief: Dr Raghab Ray (profile: teriin.org/profile/Raghab-Ray).",
    ),
    Benchmark(
        id="T5",
        question="What is the latest TERI work on vehicle-related air pollution in Delhi?",
        sources=(
            Source(
                title="A-PAG, IIT Delhi, and TERI Release Landmark Report on Interstate "
                      "Truck Emissions and Mitigation Strategies in Delhi",
                url="https://www.teriin.org/press-release/pag-iit-delhi-and-teri-release-"
                    "landmark-report-interstate-truck-emissions-and",
                held_by=CORPUS, published="2026-06-29",
                verified="catalog: two documents (page and in-body PDF)",
            ),
            Source(
                title="Towards Cleaner Freight in Delhi",
                url="https://teriin.org/policy-brief/towards-cleaner-freight-delhi",
                held_by=CORPUS, published="2026-06-29", verified="catalog",
            ),
        ),
        facts=(r"truck", r"(?:29\s+june|june\s+29|2026-06-29|june\s+2026)",
               r"(?:quarter|23\s?%)"),
        date_handling=("Newest relevant TERI source by publication date (29 June 2026), "
                       "stated with its date."),
        web_should_trigger=True, web_reason="freshness", multi_document=False,
        signals={"freshness": True, "forced": True},
        evidence=(Evidence(
            title="A-PAG, IIT Delhi, and TERI Release Landmark Report on Interstate Truck "
                  "Emissions", bundle="press_release", date="2026-06-29",
            text="Heavy-duty trucks entering Delhi from other states are responsible for "
                 "nearly a quarter of transport-related pollution in Delhi."),),
    ),
    Benchmark(
        id="T6",
        question=("Give me a consolidated overview of TERI's work on vehicle air pollution "
                  "from 2019 to 2025."),
        sources=(
            Source(title="State of air in Delhi NCR – pollution and solution",
                   url="https://www.teriin.org/infographics/state-air-delhi-ncr-pollution-"
                       "and-solution",
                   held_by=CORPUS, published="2019-06-13", verified="catalog"),
            Source(title="Bridging the Gap: The Urgent Need for Enhanced Public Transport "
                         "in India's Air Quality Mission",
                   url="https://teriin.org/article/bridging-gap-urgent-need-enhanced-"
                       "public-transport-indias-air-quality-mission",
                   held_by=CORPUS, published="2025-04-09", data_period="2016-17",
                   verified="catalog"),
        ),
        facts=(r"2019", r"2025"),
        date_handling=("A structured list or table, one entry per TERI source, each with "
                       "its publication year kept apart from the year of the data it "
                       "reports; no single annual series is constructed."),
        web_should_trigger=False, web_reason=None, multi_document=True,
        signals={"consolidation": True, "date_from": "2019-01-01", "date_to": "2026-01-01",
                 "forced": False},
        evidence=(
            Evidence(title="State of air in Delhi NCR – pollution and solution",
                     bundle="infographics", date="2019-06-13",
                     text="Vehicles are a major source of air pollution in Delhi NCR."),
            Evidence(title="Bridging the Gap", bundle="article", date="2025-04-09",
                     text="Delhi's vehicular emissions contribute 24% of PM10 in winter."),
        ),
    ),
)
