"""Answering "who is X" from the newest evidence, not the most website-y.

The bug, as reported:

    "who is Ajay Mathur"
      -> "Ajay Mathur is Director General, TERI [1]."
      -> and, in a captioned aside below it, "he was earlier ... at TERI as its
         Director General [4]."

Both facts were retrieved. The corpus held a 2020-11-09 web page headed
"Statement by Dr Ajay Mathur, Director General, TERI" and a 2023-05-16 PDF
saying he "was earlier ... at TERI as its Director General", and the answer led
with the older one in the present tense.

Four things had to be true at once for that to happen, and this file covers all
four:

1. `detect_mode` needed an explicit cue word — "currently", "today", "latest" —
   so "who is X" carried no temporal intent at all and the temporal band was
   inert;
2. even with CURRENT, `temporal_fit` scores both documents ``FIT_MISS``: each
   one's period closed on its own publication date, so the band could not
   separate a 2020 page from a 2023 one;
3. authority decided instead, and website outranks pdf_attachment;
4. the context builder then admitted website blocks first under their own cap,
   and the prompt's rule 5 said "Website sources are authoritative. If a website
   block and a PDF block disagree, the website statement is the answer."

No LLM, no Qdrant, no MySQL.
"""
from __future__ import annotations

import pytest

from app.core.models.context import ContextBlock
from app.generation.prompts import (
    GROUNDED_SYSTEM_PROMPT,
    PDF_LEAD,
    PDF_TAG,
    SUPERSEDED_MARKER,
    SUPERSEDES_MARKER,
    WEBSITE_TAG,
    format_context_blocks,
    supersession_note,
)
from app.retrieval.context.builder import build_context, flag_supersession
from app.retrieval.search.hybrid_search import Candidate
from app.retrieval.search.reranker import rerank
from app.retrieval.search.temporal_gate import (
    CURRENT,
    PAST,
    TemporalIntent,
    detect_mode,
)

# The two passages, as close to the reported ones as a fixture gets.
WEBSITE_TEXT = (
    "Statement by Dr Ajay Mathur, Director General, TERI congratulating the "
    "United States President-Elect and asking for climate leadership through "
    "action."
)
PDF_TEXT = (
    "Dr Ajay Mathur is the Director General of the International Solar Alliance "
    "(ISA). At ISA his focus is on solarization of the world. He was earlier in "
    "the Bureau of Energy Efficiency and at The Energy and Resources Institute "
    "as its Director General."
)


@pytest.fixture(autouse=True)
def _embedding_reranker(monkeypatch):
    """`.env` pins ``cross_encoder``, which re-scores these placeholder passages
    with a real model and collapses the relevance gap the fixtures set up."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "reranker_provider", "embedding",
                        raising=False)


#: Distinct one-hot vectors per candidate. The context builder drops a block
#: whose cosine against an admitted one clears `dedup_cosine_threshold`, so a
#: shared vector would silently admit exactly one of any fixture pair and read as
#: a ranking failure that is really a fixture artefact.
_VECTORS: dict[str, list[float]] = {}


def _vector(cid: str) -> list[float]:
    vec = [0.0] * 16
    vec[_VECTORS.setdefault(cid, len(_VECTORS)) % 16] = 1.0
    return vec


def _cand(cid, text, source_type, date, score, bundle="announcements"):
    return Candidate(
        id=cid, score=score, semantic_score=score, vector=_vector(cid),
        payload={
            "chunk_text": text, "source_type": source_type, "bundle": bundle,
            "effective_start_date": date, "start_precision": "day",
            "is_current": True, "title": cid, "document_id": cid,
        },
    )


def _website(score=0.72, date="2020-11-09", text=WEBSITE_TEXT):
    return _cand("web-2020", text, "website", date, score)


def _pdf(score=0.70, date="2023-05-16", text=PDF_TEXT):
    return _cand("pdf-2023", text, "pdf_attachment", date, score, bundle="events")


def _block(n, text, source_type, date):
    return ContextBlock(
        n=n, text=text,
        payload={
            "source_type": source_type, "effective_start_date": date,
            "start_precision": "day", "document_id": f"d{n}",
        },
    )


def _ranked(query, candidates, intent=None):
    return [c.id for c in rerank(query, candidates, temporal=intent)]


# --------------------------------------------------------------------------- #
# 1. A present-tense identity question is a question about now
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("question", [
    "who is Ajay Mathur",
    "who is the director general of TERI",
    "who heads TERI",
    "who leads TERI",
    "who runs TERI",
    "who is the CEO of X",
    "who is X",
    "who chairs the governing council",
    "who is the director general of TERI now",
    "director general of TERI now",
])
def test_a_present_tense_identity_question_is_current(question):
    assert detect_mode(question) == CURRENT


@pytest.mark.parametrize("question", [
    "who was Ajay Mathur",
    "who were the founders",
    "who was the former director general of TERI",
    "who is the former director general of TERI",
    "who served as director general",
    "who stepped down as director general",
])
def test_a_past_tense_identity_question_is_not_current(question):
    """"Who was" is the mirror of "who is", and a question naming the role as
    former is a past question however present-tense its verb — which is why the
    PAST pattern is consulted first."""
    assert detect_mode(question) == PAST


@pytest.mark.parametrize("question", [
    "who is currently the director general of TERI",
    "who is the current director general",
])
def test_explicit_current_wording_is_unchanged(question):
    assert detect_mode(question) == CURRENT


@pytest.mark.parametrize("question, mode", [
    ("who served as director general of TERI in 2018", "point_in_time"),
    ("upcoming events", "upcoming"),
    ("reports between 2019 and 2021", "date_range"),
    ("what is the budget", "none"),
    ("how many annual reports are there", "none"),
    ("when was Ajay Mathur director general of TERI", "none"),
])
def test_the_other_modes_are_preserved(question, mode):
    """The new clause must not swallow questions the existing patterns own. A
    year beats a tense; "what is the budget" is not an identity question and
    gains no temporal intent from a bare "is"."""
    assert detect_mode(question) == mode


# --------------------------------------------------------------------------- #
# 2. Ranking: newer evidence leads a question about the present
# --------------------------------------------------------------------------- #

def test_a_newer_pdf_outranks_an_older_website_page_for_a_current_question():
    """The reported ordering, inverted.

    Both documents score FIT_MISS — each closed its period on its own
    publication date — so temporal fit cannot separate them and the recency band
    does.
    """
    intent = TemporalIntent(mode=detect_mode("who is Ajay Mathur"))
    assert _ranked("who is Ajay Mathur", [_website(), _pdf()], intent) == [
        "pdf-2023", "web-2020",
    ]


@pytest.mark.parametrize("intent", [
    None,
    TemporalIntent(mode=PAST),
    TemporalIntent(mode="none"),
])
def test_the_same_pair_is_unchanged_when_the_question_is_not_about_now(intent):
    """The recency band is constant for every other question, so the ordering is
    exactly what it was before this existed — website leads on authority."""
    assert _ranked("q", [_website(), _pdf()], intent) == ["web-2020", "pdf-2023"]


def test_source_kind_still_breaks_a_tie_between_equally_current_evidence():
    """The preference survives as what it should always have been: a tie-break.
    Same relevance, same month, so the recency band holds one band and authority
    decides — website first."""
    web = _website(score=0.70, date="2023-05-02")
    pdf = _pdf(score=0.70, date="2023-05-16")
    assert _ranked("who is X", [pdf, web], TemporalIntent(mode=CURRENT))[0] == (
        "web-2020"
    )


def test_relevance_still_outranks_recency():
    """The invariant the band structure exists to protect: a newer document that
    answers the question less well does not lead. Nothing can cross a band."""
    weak_new = _pdf(score=0.20, date="2024-01-01")
    strong_old = _website(score=0.85, date="2019-01-01")
    assert _ranked("who is X", [weak_new, strong_old],
                   TemporalIntent(mode=CURRENT)) == ["web-2020", "pdf-2023"]


def test_a_year_is_the_bar_for_materially_newer():
    """Two write-ups of one season are not separated by the recency band; three
    years apart, they are."""
    close = _ranked("who is X", [_website(date="2023-01-10"),
                                 _pdf(date="2023-06-10")],
                    TemporalIntent(mode=CURRENT))
    assert close == ["web-2020", "pdf-2023"], "within a year: authority decides"
    far = _ranked("who is X", [_website(date="2020-01-10"),
                               _pdf(date="2023-06-10")],
                  TemporalIntent(mode=CURRENT))
    assert far == ["pdf-2023", "web-2020"], "three years apart: recency decides"


def test_an_attachment_does_not_lead_on_the_open_endedness_of_its_project():
    """The third report on this bug, and why the 2023 brief was second, not first.

    By then every dated block stood in recency order — behind a 2019 building-
    retrofit guideline that scored lowest of the six on relevance and led anyway.
    It hangs on an ``ongoing_projects`` page and inherits that bundle, and
    `temporal_fit` read the bundle as "runs to the present": a perfect fit under
    CURRENT, one band above every dated document, on the strength of a foreword
    signed with the subject's then title. The corpus holds 24 such attachment
    chunks for every project-page chunk, so this was not a corner case.
    """
    guideline = _cand(
        "guideline-2019", "Foreword. Dr A. Example, Director General, Org One.",
        "pdf_attachment", "2019-01-01", 0.71, bundle="ongoing_projects",
    )
    order = _ranked("who is A. Example", [guideline, _website(), _pdf()],
                    TemporalIntent(mode=CURRENT))
    assert order == ["pdf-2023", "web-2020", "guideline-2019"]


def test_the_project_page_itself_still_counts_as_current():
    """What the open-ended reading was for, kept: the page describing a project
    that has not ended is evidence about the present however long ago it began."""
    page = _cand("project-page", "An ongoing project on building retrofits.",
                 "website", "2019-01-01", 0.71, bundle="ongoing_projects")
    order = _ranked("what is ongoing", [_pdf(), page], TemporalIntent(mode=CURRENT))
    assert order[0] == "project-page"


# --------------------------------------------------------------------------- #
# 3. One evidence set: no per-source budgets, no reserved positions
# --------------------------------------------------------------------------- #

def test_website_and_pdf_evidence_reach_one_ranked_set():
    blocks = build_context([_pdf(), _website()], limit=6)
    kinds = {b.payload["source_type"] for b in blocks}
    assert kinds == {"website", "pdf_attachment"}, "both kinds admitted"
    assert len(blocks) == 2


def test_a_pdf_can_take_the_leading_block():
    """It could not before: website candidates were admitted first and the final
    order was website-first whatever the ranking said."""
    ranked = rerank("who is Ajay Mathur", [_website(), _pdf()],
                    temporal=TemporalIntent(mode=CURRENT))
    blocks = build_context(ranked, limit=6)
    assert blocks[0].payload["source_type"] == "pdf_attachment"


def test_no_per_source_budget_caps_the_pdf_evidence():
    """`pdf_max_slots` admitted two PDF chunks plus one conditional third, and
    nothing past that however well it ranked."""
    pdfs = [
        _cand(f"p{i}", f"PDF passage {i}.", "pdf_attachment", "2023-05-16",
              0.80 - i * 0.01, bundle="events")
        for i in range(5)
    ]
    blocks = build_context(pdfs, limit=5)
    assert len(blocks) == 5


def test_a_weak_website_chunk_no_longer_takes_a_reserved_slot():
    """The mirror of the cap: a website block used to be admitted ahead of every
    PDF as long as it cleared its own floor. Now it is ranked like anything
    else, so a far more relevant PDF leads."""
    ranked = rerank("who is X", [_website(score=0.35), _pdf(score=0.90)])
    blocks = build_context(ranked, limit=6)
    assert blocks[0].payload["source_type"] == "pdf_attachment"


# --------------------------------------------------------------------------- #
# 4. Comparing the same fact stated at two times
# --------------------------------------------------------------------------- #

QUESTION = "who is Ajay Mathur"


def _flagged(older_date="2020-11-09", newer_date="2023-05-16",
             older=WEBSITE_TEXT, newer=PDF_TEXT, mode=CURRENT,
             question=QUESTION):
    blocks = [
        _block(1, older, "website", older_date),
        _block(2, newer, "pdf_attachment", newer_date),
    ]
    flag_supersession(blocks, TemporalIntent(mode=mode), question)
    return blocks


def test_a_newer_source_framing_the_role_as_past_dates_the_older_one():
    older, newer = _flagged()
    assert older.superseded is True
    assert newer.supersedes is True
    assert newer.superseded is False


def test_the_older_block_is_kept_and_stays_citable():
    """Advisory, not a filter: it is still the evidence for what was true then."""
    older, _ = _flagged()
    assert older.text == WEBSITE_TEXT


def test_supersession_is_anchored_to_what_the_question_asked_about():
    """Two passages that are not both about the subject are not a supersession,
    however confidently the newer one frames something as past.

    The first attempt compared the blocks to each other — two or more shared
    capitalised words — and replaying the reported query flagged a 2019
    building-retrofit PDF as superseded by a 2021 announcement about a person it
    never mentions. Blocks run to thousands of characters and this corpus is all
    one organisation, so pairwise overlap is nearly free.
    """
    older, newer = _flagged(
        older="Statement by Dr Ajay Mathur, Director General, TERI.",
        newer="Ms Q. Unrelated was earlier at Some Other Body as its Registrar.",
    )
    assert older.superseded is False and newer.supersedes is False


def test_a_question_that_names_nothing_flags_nothing():
    """No subject to anchor on, so there is no claim this could make."""
    older, newer = _flagged(question="who is it")
    assert older.superseded is False and newer.supersedes is False


def test_past_framing_must_sit_near_the_subject():
    """A block says many things. "Formerly" three paragraphs from the only
    mention of the person asked about is about something else — on institutional
    prose, an unanchored search for it succeeds almost every time."""
    far = (
        "Dr Ajay Mathur opened the session. "
        + "Filler about unrelated programme delivery. " * 30
        + "The centre was formerly a regional testing station."
    )
    older, newer = _flagged(newer=far)
    assert older.superseded is False and newer.supersedes is False


def test_past_framing_beside_the_subject_still_counts():
    near = (
        "Dr Ajay Mathur is the Director General of the International Solar "
        "Alliance. He was earlier at TERI as its Director General."
        + " Filler. " * 40
    )
    older, newer = _flagged(newer=near)
    assert older.superseded is True and newer.supersedes is True


def test_supersession_needs_past_framing_in_the_newer_block():
    older, newer = _flagged(
        newer="Dr Ajay Mathur, Director General, TERI, opened the session.",
    )
    assert older.superseded is False and newer.supersedes is False


def test_supersession_needs_a_material_date_gap():
    """Within six months the two are contemporaneous write-ups rather than a
    record of a change. Six rather than the reranker's twelve: the substance of
    the claim is carried by the past framing and the subject match, so the gap
    only has to rule out two accounts of one moment."""
    older, newer = _flagged(older_date="2023-04-10", newer_date="2023-06-10")
    assert older.superseded is False and newer.supersedes is False

    older, newer = _flagged(older_date="2022-09-10", newer_date="2023-06-10")
    assert older.superseded is True and newer.supersedes is True


@pytest.mark.parametrize("mode", [PAST, "none", "point_in_time"])
def test_supersession_applies_only_to_questions_about_the_present(mode):
    """Asked what was true in 2020, the 2020 page is exactly the right source."""
    older, newer = _flagged(mode=mode)
    assert older.superseded is False and newer.supersedes is False


def test_an_undated_block_is_never_flagged():
    blocks = [
        ContextBlock(n=1, text=WEBSITE_TEXT, payload={"source_type": "website"}),
        _block(2, PDF_TEXT, "pdf_attachment", "2023-05-16"),
    ]
    flag_supersession(blocks, TemporalIntent(mode=CURRENT))
    assert blocks[0].superseded is False


def test_a_comparison_failure_costs_the_flag_and_not_the_context():
    """Anything unreadable leaves every block exactly as it arrived."""
    blocks = [_block(1, WEBSITE_TEXT, "website", "2020-11-09")]
    flag_supersession(blocks, object())  # no `.mode`
    assert blocks[0].superseded is False and blocks[0].supersedes is False


def test_the_markers_reach_the_prompt():
    older, newer = _flagged()
    rendered = format_context_blocks([older, newer])
    assert SUPERSEDED_MARKER in rendered
    assert SUPERSEDES_MARKER in rendered
    # Attached to the right block, in the header and not the body.
    header_1 = rendered.split("\n")[0]
    assert SUPERSEDED_MARKER in header_1


def test_an_unflagged_context_carries_no_markers():
    rendered = format_context_blocks(
        [_block(1, "Some passage.", "website", "2020-11-09")]
    )
    assert SUPERSEDED_MARKER not in rendered
    assert SUPERSEDES_MARKER not in rendered


# The dates note. Rule 9 and the header markers were both in the prompt on the
# run that still opened with the superseded block — [6] of six, in a
# 30,000-character context — so the same fact is now also stated beside the
# question, computed from the flags the builder set.

def test_a_flagged_context_puts_a_dates_note_beside_the_question():
    older, newer = _flagged()
    note = supersession_note([older, newer])
    assert "[2]" in note and "2023-05-16" in note, "the latest account, dated"
    assert "[1]" in note and "2020-11-09" in note, "the older one, dated"
    assert "most recent account" in note
    assert "past tense" in note


def test_an_unflagged_context_gets_no_note():
    assert supersession_note(
        [_block(1, "Some passage.", "website", "2020-11-09")]
    ) == ""


def test_the_note_names_no_one():
    """Computed from block numbers, dates and flags only — nothing in it is a
    person or an organisation, however the fixture text reads."""
    older, newer = _flagged()
    note = supersession_note([older, newer])
    assert "Mathur" not in note and "TERI" not in note


def test_the_note_reaches_the_human_turn_and_not_the_system_prompt(monkeypatch):
    from langchain_core.runnables import RunnableLambda

    from app.generation import answerer

    seen: dict = {}

    def capture(prompt_value):
        seen["messages"] = prompt_value.to_messages()
        return "an answer"

    monkeypatch.setattr(answerer, "get_llm", lambda **_: RunnableLambda(capture))
    older, newer = _flagged()
    answerer.generate_answer("who is Ajay Mathur", [older, newer])

    system, human = seen["messages"][0].content, seen["messages"][-1].content
    assert supersession_note([older, newer]) in human
    assert human.index("Dates in this context") < human.index("Question:")
    assert "Dates in this context" not in system


def test_an_unflagged_context_leaves_the_human_turn_as_it_was(monkeypatch):
    from langchain_core.runnables import RunnableLambda

    from app.generation import answerer

    seen: dict = {}

    def capture(prompt_value):
        seen["messages"] = prompt_value.to_messages()
        return "an answer"

    monkeypatch.setattr(answerer, "get_llm", lambda **_: RunnableLambda(capture))
    block = _block(1, "Some passage.", "website", "2020-11-09")
    answerer.generate_answer("a question", [block])

    human = seen["messages"][-1].content
    assert human == (
        f"Numbered context:\n{format_context_blocks([block])}\n\nQuestion: a question"
    )


# --------------------------------------------------------------------------- #
# 5. One answer
# --------------------------------------------------------------------------- #

def test_the_prompt_asks_for_one_answer_from_whatever_mix_of_sources():
    assert "one answer from all the blocks together" in GROUNDED_SYSTEM_PROMPT
    assert "whatever mix of sources" in GROUNDED_SYSTEM_PROMPT


def test_the_prompt_carries_no_separate_website_or_pdf_answer():
    for token in (WEBSITE_TAG, PDF_TAG, PDF_LEAD, "From our documents"):
        assert token not in GROUNDED_SYSTEM_PROMPT, token


def test_the_context_is_rendered_without_source_group_headings():
    rendered = format_context_blocks([
        _block(1, "A web passage.", "website", "2020-11-09"),
        _block(2, "A PDF passage.", "pdf_attachment", "2023-05-16"),
    ])
    assert "— TERI website —" not in rendered
    assert "— PDF documents —" not in rendered
    # Each block still names its own kind and date, which is what weighing it
    # needs — the grouping was the only thing that went.
    assert "(website · page date 2020-11-09)" in rendered
    assert "(pdf_attachment · page date 2023-05-16)" in rendered


def test_a_mixed_context_renders_as_one_sequence_of_numbered_blocks():
    rendered = format_context_blocks([
        _block(1, "A web passage.", "website", "2020-11-09"),
        _block(2, "A PDF passage.", "pdf_attachment", "2023-05-16"),
        _block(3, "Another web passage.", "website", "2021-01-01"),
    ])
    assert rendered.count("[1]") == 1
    headers = [line for line in rendered.split("\n") if line.startswith("[")]
    assert len(headers) == 3
    assert headers[0].startswith("[1] (website")
    assert headers[1].startswith("[2] (pdf_attachment")
    assert headers[2].startswith("[3] (website")


def test_the_answer_reader_yields_one_section_for_an_untagged_answer():
    """What a live answer now looks like to the frontend: one section, so one
    bubble — no "From our documents" panel to reconcile against it."""
    from app.generation.sections import PLAIN, split_sections

    answer = (
        "**Dr Ajay Mathur is the Director General of the International Solar "
        "Alliance** [2]. He was previously Director General of TERI [2], a post "
        "he held as of November 2020 [1]."
    )
    sections = split_sections(answer)
    assert [s.kind for s in sections] == [PLAIN]


# --------------------------------------------------------------------------- #
# 6. End to end through `retrieve`
#
# The three stages that produced the bug, run together on one call: the dual
# pull that fetches both kinds, the ranking that orders them, and the context
# build that admits them. Only Qdrant is stubbed.
# --------------------------------------------------------------------------- #

def _retrieve(monkeypatch, question, *, temporal, candidates):
    from app.config import get_settings
    from app.retrieval import retriever

    settings = get_settings()
    monkeypatch.setattr(settings, "reranker_provider", "embedding", raising=False)
    monkeypatch.setattr(settings, "graph_routing_enabled", False, raising=False)
    monkeypatch.setattr(settings, "keyword_leg_enabled", False, raising=False)
    monkeypatch.setattr(settings, "multi_query_enabled", False, raising=False)
    monkeypatch.setattr(settings, "corrective_loop_enabled", False, raising=False)
    monkeypatch.setattr(retriever, "dual_search", lambda *a, **k: list(candidates))
    monkeypatch.setattr(retriever, "search", lambda *a, **k: list(candidates))
    monkeypatch.setattr(retriever, "_observe_in_shadow", lambda *a, **k: None)
    return retriever.retrieve(
        question, query_vector=[0.1] * 16, temporal=temporal, n=6,
    )


def test_end_to_end_a_current_question_leads_with_the_newer_pdf(monkeypatch):
    """The reported query, through the real pipeline."""
    blocks = _retrieve(
        monkeypatch, "who is Ajay Mathur",
        temporal=TemporalIntent(mode=detect_mode("who is Ajay Mathur")),
        candidates=[_website(), _pdf()],
    )
    kinds = [b.payload["source_type"] for b in blocks]
    assert kinds == ["pdf_attachment", "website"], "one set, newest evidence first"
    assert blocks[0].superseded is False and blocks[0].supersedes is True
    assert blocks[1].superseded is True


def test_end_to_end_both_kinds_survive_into_one_evidence_set(monkeypatch):
    """The PDF is neither discarded nor demoted below the website block for
    being a PDF, and the website block is not dropped for being outranked."""
    blocks = _retrieve(
        monkeypatch, "who is Ajay Mathur",
        temporal=TemporalIntent(mode=CURRENT),
        candidates=[_website(), _pdf()],
    )
    assert {b.payload["source_type"] for b in blocks} == {
        "website", "pdf_attachment",
    }
    assert [b.n for b in blocks] == [1, 2], "numbered as one sequence"


def test_end_to_end_a_non_current_question_is_unchanged(monkeypatch):
    """No temporal intent: the ordering is exactly what it was before any of
    this — website first, on authority."""
    blocks = _retrieve(
        monkeypatch, "what did the statement say",
        temporal=None, candidates=[_website(), _pdf()],
    )
    assert [b.payload["source_type"] for b in blocks] == [
        "website", "pdf_attachment",
    ]
    assert all(not b.superseded and not b.supersedes for b in blocks)


# --------------------------------------------------------------------------- #
# 7. Presentation order is ranked order
#
# The second report on this bug. The ranking was by then doing its job — the
# 2023 brief came out **2nd** of six — and the answer still led with a 2020 page
# that ranked 6th, because `_order_for_attention` sat between them:
#
#     ranked    [1, 2, 3, 4, 5, 6]
#     displayed [1, 3, 5, 6, 4, 2]     <- rank 2 shown last, rank 6 shown 4th
#
# It put the two best blocks at the two ends of the prompt, matching the
# "lost in the middle" effect. Two things made it wrong here: it only ever ran
# on the minority path (`prefer_website_enabled` is on, so ordinary questions
# took the segregated build, which emitted its own order), and the ranked order
# now carries meaning — for a question about the present, rank encodes recency.
# --------------------------------------------------------------------------- #

def test_the_context_is_presented_in_ranked_order():
    """The exact shape that produced the second report: six blocks, and the
    second-ranked one must be block [2]."""
    cands = [
        _cand(f"c{i}", f"Passage {i}.", "website", "2020-01-01", 0.90 - i * 0.05)
        for i in range(6)
    ]
    blocks = build_context(cands, limit=6)
    assert [b.payload["document_id"] for b in blocks] == [
        "c0", "c1", "c2", "c3", "c4", "c5",
    ]
    assert [b.n for b in blocks] == [1, 2, 3, 4, 5, 6]


def test_the_best_evidence_is_not_moved_to_the_end():
    """Stated as the property rather than the permutation: whatever the ranker
    put second must not land last."""
    cands = [
        _cand(f"c{i}", f"Passage {i}.", "website", "2020-01-01", 0.90 - i * 0.05)
        for i in range(6)
    ]
    blocks = build_context(cands, limit=6)
    assert blocks[-1].payload["document_id"] != "c1"
    assert blocks[1].payload["document_id"] == "c1"


def test_ranked_order_survives_to_the_rendered_prompt():
    """End of the chain: what the model actually reads is numbered in rank
    order, so citing "[2]" cites the second-best block."""
    ranked = rerank("who is Ajay Mathur", [_website(), _pdf()],
                    temporal=TemporalIntent(mode=CURRENT))
    blocks = build_context(ranked, limit=6, temporal=TemporalIntent(mode=CURRENT),
                           question="who is Ajay Mathur")
    headers = [
        line for line in format_context_blocks(blocks).split("\n")
        if line.startswith("[")
    ]
    assert headers[0].startswith("[1] (pdf_attachment"), headers
    assert headers[1].startswith("[2] (website"), headers
