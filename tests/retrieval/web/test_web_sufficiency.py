"""Whether the internal evidence already answers the question.

The verdict must rest on what the evidence *contains*, not on a score: measured
on this corpus, "What is TERI SAS" retrieved 0.66-cosine boilerplate and a
cross-encoder scored passages about SASMIRA at 0.99, and neither said "SAS".
Each check is exercised both ways — the evidence that satisfies it, and the
evidence that does not — and the decision's reasons are pinned, since they are
what the trace reports when someone asks why the web was or was not used.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.retrieval.search.hybrid_search import Candidate
from app.retrieval.web import planner, sufficiency
from app.retrieval.web.planner import EXPLICIT, FRESHNESS, plan
from app.retrieval.web.sufficiency import assess, decide


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    s = get_settings()
    for name, value in {
        "web_primary_domains": "teriin.org", "web_allow_third_party": True,
        "web_blocked_domains": "", "retrieval_top_k": 6,
        "web_organisation_names": "TERI, The Energy and Resources Institute",
        "web_subject_min_coverage": 0.5, "web_passage_min_coverage": 0.8,
        "web_min_internal_relevance": 0.0, "web_on_freshness": True,
        "web_fallback_enabled": True,
    }.items():
        monkeypatch.setattr(s, name, value)
    from datetime import date

    monkeypatch.setattr(planner, "today_utc", lambda: date(2026, 9, 23))
    return s


def _c(cid: str, text: str, *, title: str = "", date: str | None = None,
       bundle: str = "news", score: float = 0.7) -> Candidate:
    payload = {"chunk_text": text, "title": title, "bundle": bundle, "source_type": "website"}
    if date:
        payload["effective_start_date"] = f"{date}T00:00:00"
    return Candidate(id=cid, score=score, payload=payload, semantic_score=score)


# --- no evidence, low relevance --------------------------------------------------


def test_no_internal_evidence_is_insufficient():
    verdict = assess(plan("What does TERI do on solar?"), [])
    assert not verdict.sufficient
    assert verdict.reasons == (sufficiency.NO_INTERNAL_EVIDENCE,)


def test_the_relevance_floor_is_off_until_calibrated():
    verdict = assess(plan("What does TERI do on solar?"),
                     [_c("a", "TERI works on solar rooftops.", score=0.05)])
    assert verdict.sufficient


def test_a_calibrated_relevance_floor_is_applied(settings, monkeypatch):
    monkeypatch.setattr(settings, "web_min_internal_relevance", 0.3)
    verdict = assess(plan("What does TERI do on solar?"),
                     [_c("a", "TERI works on solar rooftops.", score=0.05)])
    assert sufficiency.LOW_RELEVANCE in verdict.reasons
    assert verdict.signals["top_relevance"] == 0.05


# --- named subjects --------------------------------------------------------------


def test_the_measured_sas_failure_is_caught_by_what_the_passages_say():
    # High scores, and not one passage mentions "SAS" as a word.
    ranked = [
        _c("a", "SASMIRA and TERI signed an MoU on sustainable textiles.", score=0.99),
        _c("b", "The Energy and Resources Institute (TERI) is an independent research "
                "organisation.", score=0.66),
    ]
    verdict = assess(plan("What is TERI SAS?"), ranked)
    assert not verdict.sufficient
    assert verdict.reasons == (sufficiency.SUBJECT_ABSENT,)
    assert verdict.signals["subjects"] == {"SAS": False}


def test_a_mentioned_subject_is_sufficient():
    ranked = [_c("a", "TERI School of Advanced Studies (TERI SAS) is a deemed university.")]
    assert assess(plan("What is TERI SAS?"), ranked).sufficient


def test_subject_coverage_is_a_share_not_all_or_nothing(settings, monkeypatch):
    question = "How do NPK fertilizer and Asparagopsis compare for Delhi farms?"
    ranked = [_c("a", "NPK fertilizer use in Delhi farms is rising.")]
    verdict = assess(plan(question), ranked)
    assert verdict.signals["subjects"] == {"NPK": True, "Asparagopsis": False, "Delhi": True}
    assert verdict.sufficient                         # 2 of 3 >= 0.5
    monkeypatch.setattr(settings, "web_subject_min_coverage", 0.9)
    assert sufficiency.SUBJECT_ABSENT in assess(plan(question), ranked).reasons


# --- people ------------------------------------------------------------------------


T1 = "Who is Mr Sayanta Ghosh at TERI, and how many publications does he have?"


def test_documents_a_person_wrote_are_not_a_profile_of_them():
    ranked = [
        _c("a", "Authors: Sayanta Ghosh, R. Example. GIS-based forest vulnerability in Assam.",
           title="GIS and ML-Driven Insights into Forest Vulnerability", bundle="research_papers"),
    ]
    verdict = assess(plan(T1), ranked)
    assert verdict.reasons == (sufficiency.NO_PROFILE,)
    assert verdict.signals["profile"] is None


def test_a_page_titled_with_the_persons_name_is_a_profile():
    ranked = [_c("p", "Associate Fellow, Centre for Geospatial Technology Application.",
                 title="Mr Sayanta Ghosh", bundle="people")]
    verdict = assess(plan(T1), ranked)
    assert verdict.sufficient and verdict.signals["profile"] == "p"


def test_a_people_page_that_names_them_is_a_profile():
    ranked = [_c("p", "Dr Raghab Ray is a Fellow working on blue carbon.",
                 title="Our experts", bundle="people")]
    assert assess(plan("Who is Dr Raghab Ray?"), ranked).sufficient


# --- passage hunts ------------------------------------------------------------------


T4 = ("Find the TERI article containing a paragraph about seaweed biomass replacing "
      "aquafeed, NPK fertilizer, and Asparagopsis cattle feed.")


def test_one_passage_holding_the_words_satisfies_a_passage_hunt():
    ranked = [
        _c("x", "Seaweed farming is expanding along India's coast."),
        _c("hit", "Seaweed biomass can replace aquafeed and NPK fertiliser, and "
                  "Asparagopsis is being trialled as a cattle feed additive.",
           title="Carbon Sequestration by Seaweed is Not a Straightforward Pathway"),
    ]
    verdict = assess(plan(T4), ranked)
    assert verdict.sufficient
    assert verdict.signals["passage"] == "hit"
    assert verdict.signals["passage_coverage"] >= 0.8   # inflections and spellings match


def test_the_corpus_paragraph_as_actually_written_satisfies_the_hunt():
    # The live article's wording, paraphrased by the user: "aqua feeds" for
    # "aquafeed", "displacing"/"substitute" for "replacing". A compound spelling
    # is matched; a synonym is not, and the share-based threshold absorbs it.
    ranked = [_c("seaweed", (
        "The second pathway of mitigation services involves post-harvest use of seaweed "
        "biomass displacing carbon-intensive products like aqua feeds. For instance, "
        "seaweed-based soil additive can be a potential substitute of nitrogen-phosphorous-"
        "potassium (NPK) fertilizer reducing N2O emissions or cattle feed with Asparagopsis "
        "sp. significantly lowers CH4 emission."),
        title="Carbon Sequestration by Seaweed is Not a Straightforward Pathway")]
    verdict = assess(plan(T4), ranked)
    assert verdict.sufficient
    assert verdict.signals["passage_coverage"] == round(8 / 9, 3)   # only "replacing" missing


@pytest.mark.parametrize("text", ["aqua feeds", "aqua-feed", "aquafeed", "Aquafeeds"])
def test_a_compound_matches_however_it_is_written(text):
    assert sufficiency._has_term("aquafeed", sufficiency._fold(f"prices of {text} rose"))


def test_short_stems_are_not_matched_inside_other_words():
    # Joining words is only for long stems: "air" is not in "chair".
    assert not sufficiency._has_term("air", sufficiency._fold("the chair was empty"))
    assert sufficiency._has_term("air", sufficiency._fold("the air was clean"))


def test_the_words_spread_over_several_passages_are_not_the_passage():
    ranked = [
        _c("a", "Seaweed biomass is a growing resource."),
        _c("b", "Aquafeed prices rose sharply."),
        _c("c", "NPK fertilizer subsidies and Asparagopsis cattle feed trials."),
    ]
    verdict = assess(plan(T4), ranked)
    assert sufficiency.PASSAGE_NOT_FOUND in verdict.reasons
    assert verdict.signals["passage_coverage"] < 0.8


def test_a_quoted_phrase_must_appear_word_for_word():
    question = 'Which report says "fleet modernisation is essential"?'
    near = [_c("a", "Fleet modernisation is, TERI argues, essential for Delhi.")]
    exact = [_c("b", "TERI concludes that fleet modernisation is essential for Delhi.")]
    assert sufficiency.PASSAGE_NOT_FOUND in assess(plan(question), near).reasons
    assert assess(plan(question), exact).sufficient


# --- periods -----------------------------------------------------------------------


def test_a_window_needs_evidence_dated_inside_it():
    question = "TERI vehicle pollution studies from 2019 to 2025"
    outside = [_c("a", "Vehicle pollution study for Delhi.", date="2016-11-22")]
    inside = [_c("b", "Vehicle pollution study for Delhi.", date="2019-06-13")]
    assert assess(plan(question), outside).reasons == (sufficiency.DATE_WINDOW_UNCOVERED,)
    assert assess(plan(question), inside).sufficient


T3 = ("What did TERI report about transport's contribution to Delhi air pollution "
      "around 2019, and what did TERI say in 2025?")


def test_a_comparison_of_years_needs_each_year():
    only_2025 = [_c("a", "Transport contributes 28% of winter PM2.5 in Delhi.",
                    date="2025-04-09")]
    verdict = assess(plan(T3), only_2025)
    assert verdict.reasons == (f"{sufficiency.PERIOD_UNCOVERED}:2019",)
    assert verdict.signals["periods"] == {"2019": False, "2025": True}


def test_a_year_is_covered_by_a_document_dated_then_or_one_discussing_it():
    ranked = [
        _c("a", "Transport contributes 28% of winter PM2.5 in Delhi.", date="2025-04-09"),
        _c("b", "In 2019, transport was 23% of winter PM2.5 in NCT Delhi.", date="2021-01-01"),
    ]
    assert assess(plan(T3), ranked).sufficient


# --- documents -----------------------------------------------------------------------


def test_a_requested_document_must_be_among_the_candidates():
    question = 'Give me the report "Towards Cleaner Freight in Delhi"'
    other = [_c("a", "Freight emissions in Delhi.", title="Delhi Freight Study 2019")]
    named = [_c("b", "Freight emissions in Delhi.", title="Towards Cleaner Freight in Delhi")]
    assert sufficiency.DOCUMENT_NOT_FOUND in assess(plan(question), other).reasons
    assert assess(plan(question), named).sufficient


# --- the decision ------------------------------------------------------------------


GOOD = [_c("a", "Delhi's vehicular emissions contribute 24% of PM10 in winter.",
           date="2025-04-09")]


def test_sufficient_internal_evidence_means_no_web_and_says_so():
    question = ("According to TERI, how much do vehicular emissions contribute to "
                "Delhi's particulate pollution?")
    decision = decide(plan(question), assess(plan(question), GOOD))
    assert decision.search is False and decision.reasons == ()
    record = decision.to_trace()
    assert record["internal"]["sufficient"] is True       # the "why not" answer


def test_insufficient_evidence_means_web_with_its_reasons():
    decision = decide(plan(T1), assess(plan(T1), GOOD))
    assert decision.search and decision.reasons == (sufficiency.NO_PROFILE,)


def test_an_explicit_request_searches_even_with_good_evidence():
    question = "Search the web for TERI vehicular emissions figures for Delhi"
    decision = decide(plan(question), assess(plan(question), GOOD))
    assert decision.search and decision.reasons == (EXPLICIT,)


def test_freshness_searches_unless_switched_off(settings, monkeypatch):
    question = "What is the latest TERI work on vehicle-related air pollution in Delhi?"
    assert decide(plan(question), assess(plan(question), GOOD)).reasons == (FRESHNESS,)
    monkeypatch.setattr(settings, "web_on_freshness", False)
    assert decide(plan(question), assess(plan(question), GOOD)).search is False


def test_the_fallback_can_be_switched_off_leaving_only_forced_reasons(settings, monkeypatch):
    monkeypatch.setattr(settings, "web_fallback_enabled", False)
    decision = decide(plan(T1), assess(plan(T1), GOOD))
    assert decision.search is False
    assert decision.to_trace()["internal"]["reasons"] == [sufficiency.NO_PROFILE]


def test_a_forced_decision_can_be_made_before_any_evidence_exists():
    question = "What is the latest TERI report?"
    assert decide(plan(question), None).reasons == (FRESHNESS,)
