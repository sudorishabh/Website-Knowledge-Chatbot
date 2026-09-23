"""Canonical pages must be reachable: authority ranking, content terms, titles.

Regression cover for the largest failure class in the 86-question benchmark —
the authoritative page the reference set names reached retrieval for only 42% of
questions, and nine questions retrieved none of it at all.

Three distinct defects sat behind that, and each needs its own cover:

1. ``_authority_scores`` read a payload key nothing wrote, so authority was a
   constant and *completeness* — a length proxy — was the first tie-break inside
   a relevance band. Short canonical pages lose every length contest.
2. ``extract_key_terms`` skips its content-word pass whenever a precise pattern
   matched, and the organisation's acronym matches nearly every question, so the
   lexical leg pulled on ``['TERI']`` alone.
3. Neither of the above can retrieve a page whose text is a list of link labels;
   only its *title* identifies it.
"""
from __future__ import annotations

import pytest

from app.retrieval.search import reranker
from app.retrieval.search.strategies import extract_content_terms, extract_key_terms
from app.retrieval.search import title_leg


# --------------------------------------------------------------------------- #
# 1. Derived authority
# --------------------------------------------------------------------------- #
def test_a_service_node_outranks_an_attachment_on_authority():
    canonical = reranker.derived_authority({"source_type": "website", "bundle": "services"})
    news = reranker.derived_authority({"source_type": "website", "bundle": "news"})
    attachment = reranker.derived_authority(
        {"source_type": "pdf_attachment", "bundle": "page"}
    )
    assert canonical > news > attachment


def test_an_explicit_payload_authority_still_wins():
    """A corpus that stamps authority keeps control of it."""
    assert reranker.derived_authority({"source_type": "website", "bundle": "news"}) < 0.9
    scores = reranker._authority_scores(
        [_c({"source_type": "website", "bundle": "news", "source_authority": 0.99})]
    )
    assert scores == [0.99]


def test_an_unknown_source_is_neutral():
    assert reranker.derived_authority({}) == reranker._UNKNOWN


def _c(payload):
    from app.retrieval.search.hybrid_search import Candidate

    return Candidate(id=payload.get("chunk_id", "x"), score=0.5, payload=payload, vector=[0.0])


def test_authority_outranks_length_within_a_relevance_band(monkeypatch):
    """The exact inversion the benchmark found: a 60-word service node carrying
    the answer lost to a long attachment chunk that merely mentions the subject."""
    short_canonical = _c({
        "chunk_id": "service", "source_type": "website", "bundle": "services",
        "text": "Accredited by NABL we test water, soil and sludge.",
    })
    long_attachment = _c({
        "chunk_id": "annual", "source_type": "pdf_attachment", "bundle": "page",
        "text": "word " * 400,
    })
    short_canonical.payload["chunk_text"] = short_canonical.payload["text"]
    long_attachment.payload["chunk_text"] = long_attachment.payload["text"]

    # Same relevance, so the tie-break decides. Provider scores are equal.
    monkeypatch.setattr(reranker, "_semantic_scores", lambda q, c, p: [0.8] * len(c))
    out = reranker.rerank("water testing", [long_attachment, short_canonical])
    assert out[0].payload["chunk_id"] == "service"


# --------------------------------------------------------------------------- #
# 2. Content terms
# --------------------------------------------------------------------------- #
def test_precise_extraction_collapses_to_the_org_name():
    """The defect, pinned: this is why the lexical leg was inert."""
    assert extract_key_terms("What are TERI's flagship initiatives and centres of "
                             "excellence?") == ["TERI"]


def test_content_terms_recover_the_topical_words():
    terms = extract_content_terms(
        "What are TERI's flagship initiatives and centres of excellence?"
    )
    assert terms is not None
    for expected in ("flagship", "initiatives", "centres", "excellence"):
        assert expected in terms


def test_content_terms_drop_question_scaffolding():
    terms = extract_content_terms("What are the services that TERI does offer?") or []
    assert "what" not in terms and "does" not in terms


def test_content_terms_are_none_for_a_bare_question():
    assert extract_content_terms("What is it?") is None


# --------------------------------------------------------------------------- #
# 3. Title leg
# --------------------------------------------------------------------------- #
_ROWS = [
    ("hub", "Centres of Excellence", "page"),
    ("concor", "CONCOR TERI Centre of Excellence for Green and Sustainable Logistics", "page"),
    ("annual", "Annual Reports", "page"),
    ("news1", "TERI and DBT set up a Centre of Excellence", "news"),
    ("mission", "Mission and Goals", "page"),
    ("visionary", "Mr. Darbari Seth: The Visionary Founder of TERI", "page"),
    ("contact", "Contact Us", "page"),
    ("longone", "A very long editorial title that happens to mention excellence and "
                "centres and reports and mission in passing across many words", "news"),
]


@pytest.fixture
def titles(monkeypatch):
    monkeypatch.setattr(title_leg, "_titles_cache", None)
    monkeypatch.setattr(title_leg, "_titles_loaded_at", 0.0)
    import app.catalog.state as state

    monkeypatch.setattr(state, "website_titles", lambda: _ROWS)
    return _ROWS


def test_the_named_page_is_found(titles):
    ids = title_leg.title_candidates("Where can I download TERI's annual reports")
    assert ids and ids[0] == "annual"


def test_word_boundaries_are_respected(titles):
    """"vision" must not match "Visionary" — it put the founder page above
    "Mission and Goals" for a question about the mission and vision."""
    ids = title_leg.title_candidates("What is the primary mission and vision of TERI?")
    assert "mission" in ids
    assert ids.index("mission") < (ids.index("visionary") if "visionary" in ids else 99)


def test_a_ubiquitous_term_is_dropped_before_scoring():
    """The organisation's name is in most titles and cannot rank one above another."""
    catalogue = [(f"n{i}", f"TERI news item number {i}", "news") for i in range(400)]
    catalogue += [("hub", "Centres of Excellence", "page")]
    selective = title_leg._selective_terms(["teri", "excellence"], catalogue)
    assert "excellence" in selective
    assert "teri" not in selective


def test_frequency_is_not_judged_on_a_tiny_catalogue():
    """At 8 titles a 10% share rounds to zero and every term looks ubiquitous."""
    assert title_leg._selective_terms(["mission", "excellence"], _ROWS) == [
        "mission", "excellence"
    ]


def test_a_canonical_page_leads_a_news_item_at_equal_score(titles):
    ids = title_leg.title_candidates("centres of excellence")
    assert ids[0] in {"hub", "concor"}
    assert ids.index("news1") > 0


def test_a_long_editorial_title_is_not_a_page_name(titles):
    assert title_leg._score(_ROWS[-1][1], ["excellence", "centres"]) == 0


def test_a_bare_question_does_not_fire_the_leg(titles):
    assert title_leg.title_candidates("What is TERI?") == []


def test_singular_and_plural_both_match(titles):
    assert title_leg._score("Annual Reports", ["annual", "report"]) >= 2
    assert title_leg._score("Centres of Excellence", ["centre", "excellence"]) >= 2


def test_a_common_word_cannot_name_a_page_on_its_own():
    """The Q018 regression, pinned. "research" is in 1.5% of this catalogue's
    titles, so on its own it matched the grab-bag page "Our Research Focus" for
    a climate-finance question and displaced the real evidence; the answer that
    came back was about a spring census in Manipur."""
    catalogue = [(f"r{i}", f"Research note number {i}", "news") for i in range(120)]
    catalogue += [("focus", "Our Research Focus", "page"), ("contact", "Contact Us", "page")]
    catalogue += [(f"n{i}", f"Assorted item {i}", "news") for i in range(4000)]

    rare = title_leg._rare_terms(["research", "contact"], catalogue)
    assert "contact" in rare and "research" not in rare
    # Still selective enough to survive the 10% cut, so it can contribute to a
    # two-word match — it just cannot carry a match alone.
    assert "research" in title_leg._selective_terms(["research", "contact"], catalogue)

    assert title_leg._score("Our Research Focus", ["research"], rare) == 0
    assert title_leg._score("Contact Us", ["contact"], rare) == 1
    assert title_leg._score("Our Research Focus", ["research", "focus"], rare) == 2


def test_every_term_is_rare_in_a_small_catalogue(titles):
    """Same floor as the 10% rule: 8 titles carry no frequency signal at all."""
    assert title_leg._rare_terms(["mission", "excellence"], _ROWS) == ["mission", "excellence"]


# --------------------------------------------------------------------------- #
# 3b. A word that names a kind of document is not a page name.
# --------------------------------------------------------------------------- #
_KIND_ROWS = [
    ("index", "Articles & Publications", "page"),
    ("annual", "Annual Reports", "page"),
    ("past", "Past Events", "basic"),
    ("wq", "Water Quality Report 2012-13", "completed_projects"),
    ("art6", "Discussion Paper on Article 6", "policy_brief"),
] + [(f"n{i}", f"Assorted item {i}", "news") for i in range(4000)]


@pytest.fixture
def kind_titles(monkeypatch):
    monkeypatch.setattr(title_leg, "_titles_cache", None)
    monkeypatch.setattr(title_leg, "_titles_loaded_at", 0.0)
    import app.catalog.state as state

    monkeypatch.setattr(state, "website_titles", lambda: _KIND_ROWS)
    return _KIND_ROWS


def test_a_kind_word_cannot_name_a_page_on_its_own(kind_titles):
    """The 2026-09-18 regression, pinned. "articles" is in one title of 8,631
    — the "Articles & Publications" index — so by frequency it identified that
    page exactly, and "list the articles where IPCC is mentioned" retrieved
    link labels in place of anything about IPCC. The word says what kind of
    thing is wanted, not which page."""
    assert title_leg._rare_terms(["articles", "ipcc"], kind_titles) == ["ipcc"]
    assert title_leg.title_candidates("list the articles where IPCC is mentioned") == []


@pytest.mark.parametrize("question", [
    "reports where IPCC is mentioned",
    "upcoming events on green hydrogen",
    "papers discussing green finance",   # "paper" hits alone; a kind word
    "projects mentioning solar rooftops",
])
def test_the_rule_holds_for_every_kind_word(kind_titles, question):
    assert title_leg.title_candidates(question) == []


def test_two_kind_words_still_name_their_page(kind_titles):
    """Agreement between two words is evidence a single word cannot supply:
    "articles and publications" is the index page's own name, "annual reports"
    resolves exactly as before, and a paper *on Article 6* is a fair match for
    "papers discussing Article 6" even though both hits are kind words."""
    assert title_leg.title_candidates("articles and publications")[0] == "index"
    assert title_leg.title_candidates("TERI annual reports")[0] == "annual"
    assert title_leg.title_candidates("papers discussing Article 6") == ["art6"]


@pytest.mark.parametrize("word", [
    "articles", "article", "reports", "report", "news", "events", "event",
    "papers", "paper", "projects", "project", "briefs", "brief",
    "publications", "documents", "releases",
])
def test_kind_words_come_from_the_registry(word):
    assert title_leg._names_a_kind(word)


@pytest.mark.parametrize("word", [
    "contact", "mission", "excellence", "ipcc", "hydrogen", "annual", "centres",
])
def test_a_page_word_is_not_a_kind(word):
    assert not title_leg._names_a_kind(word)


def test_every_described_type_is_covered():
    """Drift guard: a type added to the glossary is a kind word here without
    this module being told, and the two layers cannot disagree."""
    from app.retrieval.structured.entities import entity_label
    from app.retrieval.understanding.catalog_prompt import BUNDLE_MEANINGS

    kinds = title_leg._kind_words()
    for bundle, _ in BUNDLE_MEANINGS:
        for word in entity_label(bundle, 2).split():
            assert word in kinds, (bundle, word)


def test_the_vocabulary_failing_open_restores_frequency_alone(monkeypatch):
    monkeypatch.setattr(title_leg, "_kind_words", lambda: frozenset())
    assert title_leg._rare_terms(["articles"], []) == ["articles"]
