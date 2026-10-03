"""A structured list must be constrained by what the question actually asks.

Regression cover for the list-head defect: the catalog answered topical
questions from a generic bucket, so two unrelated questions that resolved to the
same bundle produced the same rows. Measured on the 86-question benchmark, five
questions failed this way, and the mechanism had two halves:

* an open-vocabulary topic was **snapped onto the nearest taxonomy theme** —
  "Sustainable Development Goals" onto "Resources & Sustainable Development" —
  so the filter was wrong rather than missing;
* whatever no facet could express was **dropped**, leaving recency as the only
  ordering over a large bucket.

The tests below pin the rule that replaces it: every topical word is either
covered by a facet that genuinely means it, or constrains the rows explicitly,
or the structured path declines and lets semantic retrieval answer.

Numbered against the brief's required cases; the catalog is stubbed throughout,
so nothing here touches MySQL.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.retrieval.structured import planner, topic
from app.retrieval.structured.types import RecordFilters

# --------------------------------------------------------------------------- #
# Fixtures: a stub catalog that records the filters it was asked for.
# --------------------------------------------------------------------------- #

_SLOT_DEFAULTS = dict(
    operation="list", bundle=None, theme=None, tags=[], author=None,
    title_contains=None, group_by=None, secondary_group_by=None,
    count_of="records", limit=10,
    date_from=None, date_to=None, year=None, answer_format="default",
)


def slots(**over):
    return SimpleNamespace(**{**_SLOT_DEFAULTS, **over})


def plan_for(question, **over):
    """The single tool call the planner derives for a question."""
    return planner.plan(slots(**over), question=question).calls[0]


@pytest.fixture
def no_taxonomy(monkeypatch):
    """No theme resolves. Isolates the topic logic from the live taxonomy."""
    monkeypatch.setattr(
        "app.retrieval.structured.filters.resolve_theme", lambda name: None
    )


@pytest.fixture
def taxonomy(monkeypatch):
    """A theme vocabulary that resolves by loose containment, as the real one does."""
    known = ["Climate Change", "Energy", "Resources & Sustainable Development"]

    def resolve(name):
        if not name:
            return None
        wanted = name.lower()
        for theme in known:
            if theme.lower() in wanted or wanted in theme.lower():
                return theme
        # Fall back the way fuzzy matching does: the nearest by word overlap.
        asked = set(wanted.split())
        best = max(known, key=lambda t: len(asked & set(t.lower().split())))
        return best if asked & set(best.lower().split()) else None

    monkeypatch.setattr(
        "app.retrieval.structured.filters.resolve_theme", resolve
    )
    return resolve


@pytest.fixture
def catalog(monkeypatch):
    """Captures the kwargs `list_records` sends to SQL and returns fake rows."""
    seen: dict = {}

    def fake_list(**kwargs):
        seen.clear()
        seen.update(kwargs)
        n = kwargs.get("limit", 10)
        return [
            SimpleNamespace(
                document_id=f"d{i}", title=f"Row {i}", url=f"https://x/{i}",
                effective_start_date="2026-01-01", bundle=kwargs.get("bundle") or "news",
            )
            for i in range(n)
        ]

    monkeypatch.setattr("app.catalog.queries.list_documents", fake_list)
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **kw: 594)
    monkeypatch.setattr(
        "app.retrieval.structured.filters.resolve_theme", lambda name: None
    )
    monkeypatch.setattr(
        "app.retrieval.structured.filters.resolve_tag", lambda name: name
    )
    return seen


# --------------------------------------------------------------------------- #
# 1. Entity + topic list filtering
# --------------------------------------------------------------------------- #

def test_a_topic_the_facets_cannot_express_still_constrains_the_list(no_taxonomy):
    call = plan_for("What publications are available on Sustainable Development Goals?")
    assert set(call.filters.topic_terms) >= {"sustainable", "development", "goals"}


def test_the_constraint_reaches_sql(catalog):
    from app.retrieval.structured.tools import list_records

    list_records(None, RecordFilters(topic_terms=("adaptation", "climate")), limit=10)
    assert catalog["topic_terms"] == ("adaptation", "climate")


# --------------------------------------------------------------------------- #
# 2. Researcher / person list filtering
# --------------------------------------------------------------------------- #

def test_a_person_question_is_recognised():
    assert topic.wants_person("Which researchers work on AI and sustainability?")
    assert topic.wants_person("Who wrote the net-zero paper?")
    assert not topic.wants_person("What reports are available on climate adaptation?")


def test_the_structured_path_declines_a_person_question(monkeypatch):
    """A document catalog holds authorship, not "works on" — so listing documents
    at a person question is a confident non-answer. Declining hands it to
    semantic retrieval, which had the right papers all along."""
    from app.retrieval.structured import answerer

    monkeypatch.setattr(
        planner, "plan", lambda *a, **k: pytest.fail("must not plan a person question")
    )
    result = answerer.answer_structured(
        "Which researchers work on AI and sustainability?",
        analysis=slots(operation="lookup"),
    )
    assert result is None


def test_a_named_author_is_still_answerable(monkeypatch):
    """The decline is about *finding* people, not about filtering by one."""
    called: list = []
    monkeypatch.setattr(planner, "plan", lambda *a, **k: called.append(1) or "plan")
    monkeypatch.setattr(planner, "execute", lambda *a, **k: [])
    from app.retrieval.structured import answerer

    answerer.answer_structured(
        "Which papers did Meena Sehgal author?",
        analysis=slots(operation="list", author="Meena Sehgal"),
    )
    assert called, "a question naming the author must still reach the planner"


@pytest.mark.parametrize(
    "question, over",
    [
        ("Which authors have published the most on climate change?",
         dict(operation="distribution", group_by="author", theme="climate change")),
        ("Which researchers have written the most papers?",
         dict(operation="distribution", group_by="author")),
        ("How many authors are there?", dict(operation="count", count_of="author")),
    ],
)
def test_an_authorship_question_is_answered_from_the_catalog(monkeypatch, question, over):
    """Authorship is exactly what the catalog records. Measured: the first
    question was declined as a person question and refused."""
    called: list = []
    monkeypatch.setattr(planner, "plan", lambda *a, **k: called.append(1) or "plan")
    monkeypatch.setattr(planner, "execute", lambda *a, **k: [])
    from app.retrieval.structured import answerer

    answerer.answer_structured(question, analysis=slots(**over))
    assert called, "an authorship question must reach the planner"


def test_a_work_question_grouped_by_author_is_still_declined(monkeypatch):
    """Grouping by author answers who *wrote*; "who works on X" is not that claim."""
    monkeypatch.setattr(
        planner, "plan", lambda *a, **k: pytest.fail("must not plan a work question")
    )
    from app.retrieval.structured import answerer

    result = answerer.answer_structured(
        "Which researchers work on AI and sustainability?",
        analysis=slots(operation="distribution", group_by="author"),
    )
    assert result is None


def test_a_question_about_one_theme_is_not_answered_with_the_theme_list(monkeypatch):
    """Measured: "tell me about climate change theme" came back as the list of
    all seven themes. The listing enumerates the vocabulary and ignores the
    named theme, so the question belongs to the passage path, where the
    theme's own page answers it."""
    from app.retrieval.structured import answerer

    monkeypatch.setattr(
        planner, "plan", lambda *a, **k: pytest.fail("must not list the vocabulary")
    )
    result = answerer.answer_structured(
        "tell me about climate change theme",
        analysis=slots(operation="list_themes", theme="climate change"),
    )
    assert result is None


def test_a_question_for_the_theme_list_still_gets_it(monkeypatch):
    """The decline is about a *named* theme; "what themes are there?" names none."""
    called: list = []
    monkeypatch.setattr(planner, "plan", lambda *a, **k: called.append(1) or "plan")
    monkeypatch.setattr(planner, "execute", lambda *a, **k: [])
    from app.retrieval.structured import answerer

    answerer.answer_structured(
        "what themes are there?", analysis=slots(operation="list_themes"),
    )
    assert called, "a vocabulary question must still reach the planner"


# --------------------------------------------------------------------------- #
# 3. Project list filtering  &  4. Content-type filtering
# --------------------------------------------------------------------------- #

def test_a_thematic_project_question_is_constrained(no_taxonomy):
    call = plan_for(
        "What innovations and technologies are being demonstrated under ongoing projects?",
        bundle="ongoing_projects",
    )
    assert "innovations" in call.filters.topic_terms
    assert "technologies" in call.filters.topic_terms
    # The content type is named by the bundle, so it must not also be a topic word.
    assert "ongoing" not in call.filters.topic_terms
    assert "projects" not in call.filters.topic_terms


def test_the_content_type_alone_leaves_nothing_to_constrain(no_taxonomy):
    """Q110's shape, and why it must keep working: the question is *entirely*
    content type plus recency, so an unconstrained recent list is the answer."""
    call = plan_for("What policy briefs has TERI recently published?",
                    bundle="policy_brief")
    assert call.filters.topic_terms == ()


def test_naming_a_title_is_not_a_topic(no_taxonomy):
    """Measured: "Find the article titled Coastal Blue Carbon in Practice" kept
    "titled" as a topic word, so the lookup also required the title to contain
    "titled", matched nothing, and fell through to an answer with the wrong link."""
    for question in (
        "Find the article titled Coastal Blue Carbon in Practice",
        "Show the article called Coastal Blue Carbon in Practice",
        "Which article is named Coastal Blue Carbon in Practice?",
        "Article with the title Coastal Blue Carbon in Practice",
    ):
        call = plan_for(question, operation="lookup", bundle="article",
                        title_contains="Coastal Blue Carbon in Practice")
        assert call.filters.topic_terms == (), question


def test_a_collective_noun_is_not_a_topic(no_taxonomy):
    """"publications" names no subject whether or not a bundle was resolved."""
    assert "publications" not in topic.residual_topic(
        "What publications are available on water?", bundle=None)
    assert "documents" not in topic.residual_topic(
        "Which documents cover water?", bundle=None)


# --------------------------------------------------------------------------- #
# 5. Temporal list filtering
# --------------------------------------------------------------------------- #

def test_recency_words_are_not_topic_words(no_taxonomy):
    for question in ("What are the latest reports?", "What are the newest reports?",
                     "What reports came out recently?"):
        assert topic.residual_topic(question, bundle="report") == [], question


def test_a_date_scope_is_left_to_the_date_filter(no_taxonomy):
    call = plan_for("What reports came out in 2024?", bundle="report",
                    date_from="2024-01-01", date_to="2025-01-01")
    assert call.filters.date_from == "2024-01-01"
    assert call.filters.topic_terms == ()


# --------------------------------------------------------------------------- #
# 6-9. The list-head defect itself.
# --------------------------------------------------------------------------- #

def test_same_bundle_different_question_gives_different_constraints(no_taxonomy):
    """The defect in one assertion: two questions on one bundle used to produce
    a byte-identical list."""
    a = plan_for("What reports are available on plastic waste?", bundle="report")
    b = plan_for("What reports are available on green hydrogen?", bundle="report")
    assert a.filters.topic_terms != b.filters.topic_terms
    assert set(a.filters.topic_terms) >= {"plastic", "waste"}
    assert set(b.filters.topic_terms) >= {"green", "hydrogen"}


def test_same_entity_different_predicate_is_not_the_same_query(no_taxonomy):
    a = plan_for("Which reports discuss the cost of solar?", bundle="report")
    b = plan_for("Which reports evaluate the safety of solar?", bundle="report")
    assert a.filters.topic_terms != b.filters.topic_terms


def test_same_bundle_different_topic_ranks_differently():
    """Ordering must follow how much of the topic a row carries, not recency
    alone — the WHERE only requires one term."""
    from app.catalog import queries

    captured: dict = {}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params): captured.update(sql=sql, params=params)
        def fetchall(self): return []

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(queries, "mysql_connection", lambda: _Conn())
        mp.setattr(queries, "_table", lambda: "documents")
        queries.list_documents(topic_terms=("solar", "storage"), limit=5)
    assert "ORDER BY ((s.title LIKE %s) + (s.title LIKE %s)) DESC" in captured["sql"]
    # WHERE placeholders come first, ORDER BY placeholders after — the order the
    # statement reads in, or the parameters bind to the wrong clause.
    assert list(captured["params"][-2:]) == ["%solar%", "%storage%"]


def test_unrelated_questions_do_not_share_a_result_head(catalog):
    from app.retrieval.structured.tools import list_records

    seen = []
    for terms in (("plastic", "waste"), ("hydrogen",)):
        list_records(None, RecordFilters(topic_terms=terms), limit=5)
        seen.append(catalog["topic_terms"])
    assert seen[0] != seen[1]


# --------------------------------------------------------------------------- #
# 10. Q110 regression — the structured success that must survive.
# --------------------------------------------------------------------------- #

def test_q110_shape_still_returns_an_unconstrained_recent_list(catalog):
    """"What policy briefs has TERI recently published?" — no topic constraint,
    ordered by recency, ten rows. The whole point of the residual rule is that
    it does not fire here."""
    call = plan_for("What policy briefs has TERI recently published?",
                    bundle="policy_brief")
    assert call.filters.topic_terms == ()

    from app.retrieval.structured.tools import list_records

    result = list_records("policy_brief", call.filters, limit=10)
    assert result.ok
    assert catalog.get("topic_terms") is None
    assert catalog["bundle"] == "policy_brief"
    assert len((result.data or {}).get("records") or []) == 10


# --------------------------------------------------------------------------- #
# 11-15. The five benchmark failures, at the level the fix operates on.
# --------------------------------------------------------------------------- #

def test_q025_a_truncated_list_states_the_total(catalog):
    """"What are TERI's ongoing projects?" is legitimately unconstrained — the
    gap was that ten rows never said they were ten of 594."""
    from app.retrieval.structured.tools import list_records

    result = list_records("ongoing_projects", RecordFilters(), limit=10)
    assert (result.data or {}).get("total_matching") == 594
    assert "594" in result.rendered


def test_q035_topic_words_survive_the_bundle(no_taxonomy):
    call = plan_for(
        "What innovations and technologies are being demonstrated under ongoing projects?",
        bundle="ongoing_projects")
    assert call.filters.topic_terms


def test_q109_a_widened_theme_is_replaced_by_the_topic(taxonomy):
    """"climate change adaptation" resolves to the broader theme "Climate
    Change". Filtering on it drops the word that made the question specific, so
    the theme is not applied and the words constrain the rows instead."""
    assert not topic.faithful_theme("Climate Change Adaptation", "Climate Change")
    call = plan_for("Can you recommend reports on climate change adaptation?",
                    bundle="report", theme="Climate Change Adaptation")
    assert "adaptation" in call.filters.topic_terms


def test_q112_a_topic_that_is_not_a_theme_constrains_the_rows(taxonomy):
    call = plan_for("What publications are available on Sustainable Development Goals?",
                    theme="Sustainable Development Goals")
    assert set(call.filters.topic_terms) >= {"goals"}


def test_q119_a_person_question_never_reaches_the_list(monkeypatch):
    from app.retrieval.structured import answerer

    monkeypatch.setattr(
        planner, "plan", lambda *a, **k: pytest.fail("planned a person question")
    )
    assert answerer.answer_structured(
        "Which researchers work on AI and sustainability?",
        analysis=slots(operation="lookup"),
    ) is None


# --------------------------------------------------------------------------- #
# Safety: unknown topic -> fall through, never an arbitrary list.
# --------------------------------------------------------------------------- #

def test_a_topic_with_no_matching_rows_declines_rather_than_widening(monkeypatch):
    """The Phase 6 rule: "no trustworthy structured answer" beats "a plausible
    but wrong list". An empty constrained result must not retry unconstrained."""
    from app.retrieval.structured.tools import list_records

    calls: list = []

    def fake_list(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr("app.catalog.queries.list_documents", fake_list)
    monkeypatch.setattr(
        "app.retrieval.structured.filters.resolve_theme", lambda name: None)
    monkeypatch.setattr(
        "app.retrieval.structured.filters.resolve_tag", lambda name: name)

    result = list_records(None, RecordFilters(topic_terms=("nonexistenttopic",)), limit=10)
    assert not result.ok
    assert len(calls) == 1, "an empty topical list must not be retried without the topic"


def test_no_rows_are_invented(catalog):
    """Every rendered item comes from a returned row: the renderer cannot add
    one, so a hallucinated list entry is not reachable from this path."""
    from app.retrieval.structured.tools import list_records

    result = list_records(None, RecordFilters(topic_terms=("solar",)), limit=3)
    records = (result.data or {}).get("records") or []
    assert len(records) == 3
    for record in records:
        assert record["title"] in result.rendered


def test_the_residual_is_empty_when_every_word_is_accounted_for(taxonomy):
    """A faithful theme genuinely covers its words, so it leaves no residual and
    the theme filter does the work on its own."""
    assert topic.faithful_theme("climate", "Climate Change")
    assert topic.residual_topic("What reports are there on climate?",
                                bundle="report", theme="climate") == []
