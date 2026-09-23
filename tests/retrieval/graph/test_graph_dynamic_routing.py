"""Schema-aware graph routing: any approved predicate, over any period.

Two limitations are under test here, and the tests are organised around them.

**Routing was not the graph's boundary; a table of question shapes was.** Three
of the seven approved predicates had no template, no route and no class, so the
claims the graph held for them could not be asked for. The tests in sections A,
C and D assert the replacement property: every approved predicate is reachable,
in both directions its domain and range allow, and a predicate added to the
vocabulary becomes queryable without a new template, route or routing class.

**Temporal handling made the graph answer nothing at all.** Every claim in this
corpus has an end date in the past, and the four classes that shipped enabled
all read current-state edges — of which there are none, and can be none. Section
B, F and G assert that a relationship is retrievable however old it is, that a
window means the same thing at query time as it does in conflict detection, and
that an ended relationship is still refused as *current*.

What is deliberately unchanged, and asserted to be: no Cypher is accepted from
anywhere, a predicate never becomes a relationship type, an unapproved predicate
cannot be probed for, and every non-answer still falls back to existing
retrieval (sections C and E).
"""
from __future__ import annotations

import re

import pytest

from app.knowledge.claims import predicates as vocab
from app.knowledge.claims import temporal as claim_temporal
from app.knowledge.claims import types as claim_types
from app.retrieval.graph import intent as qi
from app.retrieval.graph import plans, policy, router
from app.retrieval.graph import templates as reg

ORG = "org_aeeeb2a91bdd"
PERSON = "person_1234567890ab"
PROJECT = "project_abcdef012345"


class _Decision:
    def __init__(self, entity_id, entity_type, surface_text, score=0.9):
        self.entity_id = entity_id
        self.entity_type = entity_type
        self.surface_text = surface_text
        self.score = score
        self.canonical = True
        self.decision = "AUTO"


def _routes_as(monkeypatch, resolved, ambiguous=(), spans=(), resolved_spans=None):
    """Stub resolution. `spans` is every mention (what masking blanks);
    `resolved_spans` is index-aligned with `resolved` and says where each anchor
    sits, which is what the two-hop chain is ordered by.

    Defaulting each anchor to offset 0 makes `_nearest_first` order the
    predicates by cue offset ascending — the question's own word order, which is
    what these tests asserted before the anchor position was available."""
    anchored = (
        list(resolved_spans) if resolved_spans is not None
        else [(0, 0)] * len(resolved)
    )
    monkeypatch.setattr(
        router, "_resolve_entities",
        lambda q, index: (resolved, list(ambiguous), list(spans), anchored),
    )


def _anchor_for(entity_type):
    return {"ORGANIZATION": (ORG, "DBT"), "PERSON": (PERSON, "A Person"),
            "PROJECT": (PROJECT, "Some Project")}[entity_type]


def _graph_reachable() -> bool:
    try:
        from app.core.clients.graph import graph_available

        return graph_available()
    except Exception:
        return False


# =========================================================================== #
# A. Relational coverage — every approved predicate, both directions
# =========================================================================== #


@pytest.mark.parametrize(
    "question,entity_type,predicate,side",
    [
        # funding
        ("Which projects has DBT funded?", "ORGANIZATION", "FUNDED_BY", "object"),
        ("Who funded Some Project?", "PROJECT", "FUNDED_BY", "subject"),
        ("Which organisations sponsored Some Project?",
         "PROJECT", "FUNDED_BY", "subject"),
        # leadership
        ("Who led Some Project?", "PROJECT", "LED_BY", "subject"),
        ("What projects did A Person lead?", "PERSON", "LED_BY", "object"),
        ("Who was the principal investigator on Some Project?",
         "PROJECT", "LED_BY", "subject"),
        # partnership
        ("Who did DBT partner with?", "ORGANIZATION", "PARTNER_OF", "object"),
        ("Which organisations collaborated on Some Project?",
         "PROJECT", "PARTNER_OF", "subject"),
        # employment
        ("Where does A Person work?", "PERSON", "WORKS_AT", "subject"),
        ("Who is employed by DBT?", "ORGANIZATION", "WORKS_AT", "object"),
        # membership
        ("Which committees is A Person a member of?",
         "PERSON", "MEMBER_OF", "subject"),
        ("Who are the members of DBT?", "ORGANIZATION", "MEMBER_OF", "object"),
        # parent/child organizations
        ("What are the subsidiaries of DBT?",
         "ORGANIZATION", "PARENT_OF", "subject"),
        # roles (a literal-valued predicate)
        ("What is the designation of A Person?", "PERSON", "HAS_ROLE", "subject"),
    ],
)
def test_every_approved_predicate_is_reachable(
    monkeypatch, question, entity_type, predicate, side
):
    """The headline property: no predicate is unaskable.

    ``PARTNER_OF``, ``PARENT_OF`` and ``HAS_ROLE`` are the three that had no
    template, no route and no class before this change, so a claim under any of
    them was stored, projected and then unreachable.
    """
    entity_id, surface = _anchor_for(entity_type)
    _routes_as(monkeypatch, [_Decision(entity_id, entity_type, surface)])
    outcome = router.route(question)
    assert outcome.routed, f"{question!r} did not route"
    assert outcome.route.parameters.get("predicate") == predicate
    assert outcome.route.plan.side == side


def test_every_predicate_in_the_vocabulary_has_phrasing():
    """A predicate approved without cues would be silently unaskable.

    The gap this test closes is the one the whole change is about: it must be
    impossible to add a predicate, have the ingestion layer accept claims under
    it, and discover months later that nothing can ask about them.
    """
    missing = [
        name for name in vocab.PREDICATE_NAMES if not qi.PREDICATE_CUES.get(name)
    ]
    assert missing == [], f"approved but unaskable: {missing}"
    assert set(plans.queryable_predicates()) == set(vocab.PREDICATE_NAMES)


def test_cues_never_name_a_predicate_the_vocabulary_does_not_have():
    """The cue table cannot resurrect a retired predicate."""
    for name in qi.PREDICATE_CUES:
        assert vocab.is_known(name), name
    stale = qi.read_relational("who bequeathed the funding")
    assert all(vocab.is_known(p) for p in stale.predicates)


def test_the_direction_comes_from_the_declared_domain_and_range():
    """Not from the question's phrasing, and not from a lookup table."""
    assert plans.sides_for("FUNDED_BY", "PROJECT") == ("subject",)
    assert plans.sides_for("FUNDED_BY", "ORGANIZATION") == ("object",)
    # A type at neither end has no side, so no plan and no query.
    assert plans.sides_for("FUNDED_BY", "PERSON") == ()
    # HAS_ROLE takes a literal object, so there is no object side to anchor.
    assert plans.sides_for("HAS_ROLE", "PERSON") == ("subject",)
    assert plans.sides_for("HAS_ROLE", "ORGANIZATION") == ()


def test_a_question_whose_entity_type_the_predicate_forbids_declines(monkeypatch):
    _routes_as(monkeypatch, [_Decision(PERSON, "PERSON", "A Person")])
    assert not router.route("Who funded A Person?").routed


def test_a_topical_question_still_does_not_route(monkeypatch):
    """Naming an entity is not enough; the graph is not a topic index."""
    _routes_as(monkeypatch, [_Decision(ORG, "ORGANIZATION", "DBT")])
    outcome = router.route("Tell me about DBT")
    assert not outcome.routed
    assert "not relational" in outcome.reason


def test_a_cue_inside_the_entitys_own_name_is_not_a_relationship(monkeypatch):
    """The subject must not supply its own predicate.

    "Department of Biotechnology" contains "department"; "National Centre for
    X" contains "centre". Both were matching PARENT_OF, so every question about
    such an organization became a question about its internal structure. Entity
    spans are masked before cues are matched.
    """
    question = "Which projects were funded by the Department of Biotechnology?"
    span = (question.index("Department"), question.index("Biotechnology") + len("Biotechnology"))
    _routes_as(
        monkeypatch, [_Decision(ORG, "ORGANIZATION", "Department of Biotechnology")],
        spans=[span],
    )
    outcome = router.route(question)
    assert outcome.routed
    assert outcome.route.parameters["predicate"] == "FUNDED_BY"


def test_masking_preserves_offsets_so_cue_order_survives():
    masked = router._mask_entities("who leads projects funded by ACME", [(29, 33)])
    assert len(masked) == len("who leads projects funded by ACME")
    assert "ACME" not in masked
    assert masked.index("leads") == 4


# =========================================================================== #
# B. Temporal reading — current, historical, point-in-time, ranges
# =========================================================================== #


@pytest.mark.parametrize(
    "question,kind,start,end",
    [
        ("Who currently leads Some Project?", "current", None, None),
        ("Who leads Some Project now?", "current", None, None),
        ("Who led Some Project in 2015?", "as_of", "2015-01-01", "2016-01-01"),
        ("Who led Some Project as of 2015-06-03?",
         "as_of", "2015-06-03", "2015-06-04"),
        ("Who led Some Project between 2017 and 2019?",
         "range", "2017-01-01", "2020-01-01"),
        ("Who led Some Project from 2017 to 2019?",
         "range", "2017-01-01", "2020-01-01"),
        ("Who has led Some Project since 2010?", "range", "2010-01-01", None),
        ("Who led Some Project after 2018?", "range", "2019-01-01", None),
        ("Who led Some Project before 2015?", "range", None, "2015-01-01"),
        ("Who led Some Project until 2014?", "range", None, "2015-01-01"),
        ("What is the leadership history of Some Project?",
         "history", None, None),
        ("Who used to lead Some Project?", "history", None, None),
        ("Who leads Some Project?", "unspecified", None, None),
    ],
)
def test_the_temporal_reading_of_a_question(question, kind, start, end):
    temporal = qi.read_temporal(question, today="2026-08-18")
    assert temporal.kind == kind, question
    if kind == "current":
        # A current question is a window of exactly today.
        assert temporal.window_start == "2026-08-18"
        assert temporal.window_end == "2026-08-19"
    else:
        assert temporal.window_start == start, question
        assert temporal.window_end == end, question


def test_an_explicit_interval_beats_a_bare_tense():
    """Precedence, not convenience: the interval is the stronger statement."""
    temporal = qi.read_temporal("who has historically led it since 2010")
    assert temporal.kind == "range" and temporal.window_start == "2010-01-01"


def test_history_beats_current_when_a_question_asks_for_both():
    """History is the superset, so it is the safe reading of an ambiguous ask."""
    assert qi.read_temporal("the current and past leadership").kind == "history"


def test_a_reversed_range_is_read_in_order():
    temporal = qi.read_temporal("between 2019 and 2017")
    assert (temporal.window_start, temporal.window_end) == ("2017-01-01", "2020-01-01")


def test_a_bare_quantity_is_not_read_as_a_year():
    """This corpus counts things in the thousands; 1030 is not a date."""
    assert qi.read_temporal("how many of the 1030 projects").kind == "unspecified"


def test_only_a_current_question_sets_current_only(monkeypatch):
    _routes_as(monkeypatch, [_Decision(PROJECT, "PROJECT", "Some Project")])
    for question, expected in (
        ("Who currently leads Some Project?", True),
        ("Who leads Some Project?", False),
        ("Who led Some Project in 2015?", False),
        ("What is the leadership history of Some Project?", False),
    ):
        outcome = router.route(question)
        assert outcome.routed, question
        assert outcome.route.parameters["current_only"] is expected, question


def test_a_current_question_is_modelled_as_current_and_others_as_historical(
    monkeypatch,
):
    """The distinction the architecture keeps, asserted at the route."""
    _routes_as(monkeypatch, [_Decision(PROJECT, "PROJECT", "Some Project")])
    assert router.route("Who currently leads Some Project?").route.mode == (
        reg.MODE_CURRENT
    )
    for question in ("Who led Some Project in 2015?",
                     "Who led Some Project?",
                     "What is the leadership history of Some Project?"):
        assert router.route(question).route.mode == reg.MODE_HISTORICAL, question


def test_a_history_question_naming_no_predicate_becomes_a_timeline(monkeypatch):
    _routes_as(monkeypatch, [_Decision(PROJECT, "PROJECT", "Some Project")])
    outcome = router.route("What is the history of Some Project?")
    assert outcome.route.template_id == "entity_timeline"
    assert outcome.route.plan.capability == plans.CLASS_TIMELINE


def test_an_explicit_as_of_overrides_an_inferred_tense(monkeypatch):
    """How a benchmark or a replay asks what was true on a given date."""
    _routes_as(monkeypatch, [_Decision(PROJECT, "PROJECT", "Some Project")])
    outcome = router.route("Who leads Some Project?", as_of="2015-06-01")
    assert outcome.route.parameters["window_start"] == "2015-06-01"
    assert outcome.route.parameters["window_end"] == "2015-06-02"
    assert outcome.route.parameters["current_only"] is False


# =========================================================================== #
# C. Dynamic routing, and the safety it must not cost
# =========================================================================== #


def test_a_newly_approved_predicate_is_queryable_with_no_new_route_class(
    monkeypatch,
):
    """The acceptance criterion, end to end.

    A predicate is added to the closed vocabulary and given phrasing. Nothing
    else is touched — no template is written, no entry is added to the router's
    pattern table, no routing class is invented and no configuration changes —
    and a question about it routes, selects a reviewed template, and lands in a
    capability class that is already enabled.
    """
    added = vocab.Predicate(
        name="ADVISED_BY",
        description="The project was advised by the organization.",
        domain=("PROJECT",), range=("ORGANIZATION",),
    )
    monkeypatch.setitem(vocab.PREDICATES, "ADVISED_BY", added)
    monkeypatch.setitem(qi.PREDICATE_CUES, "ADVISED_BY", ("advised", "adviser"))

    _routes_as(monkeypatch, [_Decision(PROJECT, "PROJECT", "Some Project")])
    outcome = router.route("Who advised Some Project?")

    assert outcome.routed
    assert outcome.route.parameters["predicate"] == "ADVISED_BY"
    assert outcome.route.template_id in reg.TEMPLATES, "an existing template"
    assert outcome.route.query_class in plans.CAPABILITY_CLASSES
    assert outcome.route.query_class in policy.DEFAULT_ENABLED_CLASSES
    # And the inverse direction comes for free from the declared range.
    _routes_as(monkeypatch, [_Decision(ORG, "ORGANIZATION", "DBT")])
    inverse = router.route("Which projects has DBT advised?")
    assert inverse.route.plan.side == "object"


def test_a_new_predicate_needs_no_new_cypher():
    """One template per *shape*, not per predicate — that is what makes the
    above possible. Adding a predicate must not require reviewing Cypher."""
    generic = ("relationship_by_subject", "relationship_by_object",
               "relationship_two_hop", "entity_timeline")
    for template_id in generic:
        cypher = reg.TEMPLATES[template_id].cypher
        for predicate in vocab.PREDICATE_NAMES:
            assert predicate not in cypher, (
                f"{template_id} names {predicate} literally; it should arrive "
                "as a bound parameter"
            )


def test_a_predicate_reaches_cypher_as_a_value_never_as_a_relationship_type():
    """The move that makes dynamic selection safe.

    ``c.predicate = $predicate`` is a bound value. A relationship type would be
    an identifier, and an identifier built from input is the injection this
    registry exists to prevent.
    """
    for template_id in ("relationship_by_subject", "relationship_by_object"):
        cypher = reg.TEMPLATES[template_id].cypher
        assert "c.predicate = $predicate" in cypher
        # No relationship type in these templates is anything but the fixed
        # structural ones.
        types = set(re.findall(r"\[:([A-Z_|]+)\]", cypher))
        assert types <= {"SUBJECT", "OBJECT", "SUBJECT|OBJECT", "SUPPORTED_BY"}


def test_an_unapproved_predicate_cannot_even_be_probed_for():
    template = reg.TEMPLATES["relationship_by_subject"]
    for bad in ("SECRETLY_FUNDS", "", "FUNDED_BY'", "*", None):
        with pytest.raises(reg.InvalidParameter):
            reg.validate_parameters(
                template,
                {"entity_id": ORG, "predicate": bad, "current_only": False},
            )


def test_arbitrary_cypher_cannot_execute():
    """There is no code path that accepts a query. Only ids select one."""
    from app.retrieval.graph import traverse

    result = traverse.run_template(
        "MATCH (n) DETACH DELETE n", {"entity_id": ORG}, limit=1
    )
    assert result.error is not None
    assert result.rows == []


def test_an_injected_value_is_rejected_before_the_driver():
    template = reg.TEMPLATES["relationship_by_subject"]
    for bad_entity in ("org_1' OR 1=1 --", "'; MATCH (n) DETACH DELETE n //",
                       "org_short", 12345):
        with pytest.raises(reg.InvalidParameter):
            reg.validate_parameters(
                template,
                {"entity_id": bad_entity, "predicate": "LED_BY",
                 "current_only": False},
            )


def test_a_window_bound_must_be_a_real_date():
    template = reg.TEMPLATES["relationship_by_subject"]
    with pytest.raises(reg.InvalidParameter):
        reg.validate_parameters(
            template,
            {"entity_id": ORG, "predicate": "LED_BY", "current_only": False,
             "window_start": "2015 OR true"},
        )


def test_current_only_must_be_a_bool():
    """A truthy string would silently turn a historical query into a current
    one, or the reverse — the single most consequential parameter here."""
    template = reg.TEMPLATES["relationship_by_subject"]
    with pytest.raises(reg.InvalidParameter):
        reg.validate_parameters(
            template,
            {"entity_id": ORG, "predicate": "LED_BY", "current_only": "yes"},
        )


def test_the_caller_cannot_widen_the_current_state_bases():
    """`current_bases` is derived by the validator, never accepted."""
    template = reg.TEMPLATES["relationship_by_subject"]
    checked = reg.validate_parameters(
        template,
        {"entity_id": ORG, "predicate": "LED_BY", "current_only": True,
         "current_bases": ["unknown", "document"]},
    )
    assert checked["current_bases"] == list(claim_types.CURRENT_STATE_BASES)
    assert "document" not in checked["current_bases"]
    assert "unknown" not in checked["current_bases"]


def test_the_timeline_is_restricted_to_the_approved_vocabulary():
    """Even the everything-about-X query cannot surface a retired predicate."""
    checked = reg.validate_parameters(
        reg.TEMPLATES["entity_timeline"],
        {"entity_id": ORG, "current_only": False},
    )
    assert checked["predicates"] == list(vocab.PREDICATE_NAMES)


def test_an_empty_window_is_refused_rather_than_silently_returning_nothing():
    with pytest.raises(reg.InvalidParameter):
        reg.validate_parameters(
            reg.TEMPLATES["relationship_by_subject"],
            {"entity_id": ORG, "predicate": "LED_BY", "current_only": False,
             "window_start": "2019-01-01", "window_end": "2018-01-01"},
        )


@pytest.mark.parametrize("template_id", sorted(reg.TEMPLATE_IDS))
def test_every_template_including_the_new_ones_obeys_the_registry_rules(
    template_id,
):
    """The registry's invariants, re-asserted over the widened set."""
    template = reg.TEMPLATES[template_id]
    cypher = template.cypher
    assert "LIMIT $limit" in cypher, "unbounded result set"
    assert not re.search(r"\[[^\]]*\*[^\]]*\]", cypher), "variable-length path"
    assert "%" not in cypher and "format(" not in cypher, "value formatted in"
    assert template.mode in reg.MODES

    # Node labels: `(x:Label` / `(:Label`. Every one is a literal from the
    # reviewed schema, never assembled from input.
    for label in re.findall(r"\(\s*\w*\s*:([A-Za-z_]+)", cypher):
        assert label in {
            "Entity", "Claim", "Chunk", "Document", "Alias", "Predicate",
            "Person", "Organization", "Project",
        }, f"{template_id}: unexpected label {label}"

    # Relationship types: `[x:TYPE` / `[:A|B`. The structural ones plus the
    # closed predicate vocabulary — exactly what
    # `knowledge.graph.writer.safe_relationship` allows to become an edge type,
    # so the read path cannot name an edge the write path could not create.
    from app.knowledge.graph import schema as graph_schema

    allowed_rels = set(graph_schema.PROVENANCE_RELATIONSHIPS) | set(
        vocab.PREDICATE_NAMES
    )
    for group in re.findall(r"\[\s*\w*\s*:([A-Z_|]+)", cypher):
        for rel in group.split("|"):
            assert rel in allowed_rels, (
                f"{template_id}: unexpected relationship type {rel}"
            )


@pytest.mark.parametrize("template_id", sorted(reg.TEMPLATE_IDS))
def test_every_template_still_has_a_class(template_id):
    assert policy.class_of(template_id) is not None


def test_a_plan_only_ever_names_a_template_the_registry_holds():
    for template_id in plans.CURRENT_EDGE_TEMPLATES.values():
        assert template_id in reg.TEMPLATES
    for template_id in plans.CURRENT_TWO_HOP_TEMPLATES.values():
        assert template_id in reg.TEMPLATES


def test_the_route_class_falls_back_to_the_template_for_legacy_routes():
    """Backward compatibility for `GRAPH_ROUTING_CLASSES`, at the gate itself."""
    legacy = router.Route(
        template_id="projects_funded_by_org", parameters={}, entity_id=ORG,
        entity_type="ORGANIZATION", entity_name="DBT",
        mode=reg.MODE_CURRENT, reason="t",
    )
    assert policy.class_of_route(legacy) == "current_funding"


# =========================================================================== #
# D. Multi-hop
# =========================================================================== #


def test_organization_to_project_to_person(monkeypatch):
    _routes_as(monkeypatch, [_Decision(ORG, "ORGANIZATION", "DBT")])
    outcome = router.route("Who leads the projects funded by DBT?")
    assert outcome.route.template_id == "relationship_two_hop"
    assert outcome.route.parameters["predicate"] == "FUNDED_BY"
    assert outcome.route.parameters["predicate2"] == "LED_BY"
    assert outcome.route.plan.hops == 2


def test_the_schema_picks_the_legal_ordering_not_the_word_order(monkeypatch):
    """"Who *leads* projects *funded* by X" names LED_BY first, but an
    organization is not a legal end of LED_BY, so the chain can only start at
    FUNDED_BY. No word-order heuristic is needed or used."""
    _routes_as(monkeypatch, [_Decision(ORG, "ORGANIZATION", "DBT")])
    plan = router.route("Who leads projects funded by DBT?").route.plan
    assert plan.predicates == ("FUNDED_BY", "LED_BY")


def test_a_chain_the_schema_has_no_path_for_is_refused():
    """FUNDED_BY ends at ORGANIZATION; LED_BY joins PROJECT to PERSON. There is
    no organization-shaped end for a leadership hop to continue from... but
    there is via the project end, so the illegal case is the reverse chain."""
    assert plans.two_hop(
        entity_id=PERSON, entity_type="PERSON", first="LED_BY",
        second="WORKS_AT", temporal=qi.TemporalIntent(),
    ) is None


def test_a_literal_valued_predicate_cannot_be_a_chain_leg():
    """HAS_ROLE's object is a string, so there is no entity to hop from."""
    assert plans.two_hop(
        entity_id=PERSON, entity_type="PERSON", first="HAS_ROLE",
        second="WORKS_AT", temporal=qi.TemporalIntent(),
    ) is None


def test_a_chain_cannot_use_one_predicate_twice():
    assert plans.two_hop(
        entity_id=ORG, entity_type="ORGANIZATION", first="FUNDED_BY",
        second="FUNDED_BY", temporal=qi.TemporalIntent(),
    ) is None


def test_a_current_multi_hop_question_uses_the_derived_edge_template(monkeypatch):
    _routes_as(monkeypatch, [_Decision(ORG, "ORGANIZATION", "DBT")])
    outcome = router.route("Who currently leads the projects funded by DBT?")
    assert outcome.route.template_id == "people_leading_projects_funded_by_org"
    assert outcome.route.mode == reg.MODE_CURRENT


def test_both_legs_of_a_chain_share_the_window(monkeypatch):
    """Otherwise a 2005 funding could be chained to a 2019 leadership and
    presented as one fact."""
    _routes_as(monkeypatch, [_Decision(ORG, "ORGANIZATION", "DBT")])
    params = router.route(
        "Who led the projects funded by DBT in 2010?"
    ).route.parameters
    assert params["window_start"] == "2010-01-01"
    assert params["window_end"] == "2011-01-01"
    cypher = reg.TEMPLATES["relationship_two_hop"].cypher
    assert cypher.count("$window_start") == cypher.count("$window_end")
    assert "c1.valid_until >  $window_start" in cypher
    assert "c2.valid_until >  $window_start" in cypher


# =========================================================================== #
# E. Fallback — every non-answer ends at existing retrieval
# =========================================================================== #


@pytest.fixture
def _clean_policy():
    policy.reset()
    yield
    policy.reset()


def _settings(**kwargs):
    class _S:
        graph_routing_enabled = True
        graph_routing_classes = None
        graph_routing_budget_seconds = 15.0

    settings = _S()
    for key, value in kwargs.items():
        setattr(settings, key, value)
    return settings


def test_an_unreachable_graph_falls_back(monkeypatch, _clean_policy):
    from app.retrieval.graph import traverse

    def _down(*args, **kwargs):
        raise RuntimeError("ServiceUnavailable: cannot reach Neo4j")

    monkeypatch.setattr("app.core.clients.graph.read_session", _down)
    result = traverse.run_template(
        "relationship_by_subject",
        {"entity_id": ORG, "predicate": "LED_BY", "current_only": False},
        limit=5,
    )
    assert result.error is not None
    assert result.rows == [] and result.empty


def test_a_graph_timeout_falls_back(monkeypatch, _clean_policy):
    import time

    def _slow(question, *, top_k, allowed):
        time.sleep(5)
        return policy.GraphAttempt(policy.ANSWERED, blocks=["late"])

    monkeypatch.setattr(policy, "_attempt", _slow)
    attempt = policy.attempt("q", settings=_settings(graph_routing_budget_seconds=0.3))
    assert attempt.outcome == policy.TIMED_OUT
    assert attempt.blocks == []


def test_no_matching_plan_is_not_routed_rather_than_failed(monkeypatch):
    _routes_as(monkeypatch, [_Decision(PERSON, "PERSON", "A Person")])
    outcome = router.route("Who funded A Person?")
    assert not outcome.routed
    assert outcome.route is None


def test_an_ambiguous_entity_does_not_guess(monkeypatch):
    _routes_as(monkeypatch, [], ambiguous=["TERI"])
    outcome = router.route("Which projects has TERI funded?")
    assert not outcome.routed
    assert outcome.ambiguous == ["TERI"]


def test_a_zero_result_is_not_a_failure(monkeypatch, _clean_policy):
    monkeypatch.setattr(
        policy, "_attempt",
        lambda *a, **kw: policy.GraphAttempt(
            policy.ZERO_RESULT, query_class=plans.CLASS_HISTORY, rows=0
        ),
    )
    attempt = policy.attempt("q", settings=_settings())
    assert attempt.outcome == policy.ZERO_RESULT
    assert attempt.fell_back and not attempt.used
    assert not policy.circuit_is_open(), "a correct 'nothing known' is not a fault"


def test_an_unsupported_scope_declines_the_graph(_clean_policy):
    """Fail closed: a narrowed question the graph cannot narrow is not the
    graph's to answer, however well it knows the relationship."""
    from qdrant_client.models import FieldCondition, MatchValue

    attempt = policy.attempt(
        "Which projects has DBT funded?",
        settings=_settings(),
        filters=[FieldCondition(key="source_type", match=MatchValue(value="pdf"))],
    )
    assert attempt.outcome == policy.SCOPE_UNSUPPORTED
    assert attempt.blocks == []


@pytest.mark.parametrize(
    "outcome",
    [policy.ZERO_RESULT, policy.FAILED, policy.TIMED_OUT, policy.NOT_ROUTED,
     policy.CLASS_DISABLED, policy.NO_EVIDENCE, policy.CIRCUIT_OPEN,
     policy.SCOPE_UNSUPPORTED],
)
def test_every_non_answer_still_returns_no_blocks(monkeypatch, outcome):
    from app.retrieval import retriever

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "graph_routing_enabled", True,
                        raising=False)
    monkeypatch.setattr(
        policy, "attempt",
        lambda *a, **kw: policy.GraphAttempt(outcome, blocks=["leaked"]),
    )
    assert retriever.graph_blocks_for("q", n=5) == []


# =========================================================================== #
# F. Historical correctness — the overlap rule, term by term
#
# The window clause is evaluated by the real Cypher engine over literal values,
# with no data touched, so what is tested is the reviewed query text rather
# than a Python restatement of it. The same matrix is checked against
# `claims.temporal.overlaps`, which is what conflict detection uses: "valid
# during" has to mean one thing on both sides of the system.
# =========================================================================== #

# (claim_from, claim_until, window_start, window_end, expected)
_OVERLAP_CASES = [
    # The scenario from the brief: LED_BY valid 2012..2018.
    ("2012-01-01", "2018-01-01", "2015-01-01", "2016-01-01", True),   # "in 2015"
    ("2012-01-01", "2018-01-01", "2019-01-01", None, False),          # "after 2018"
    ("2012-01-01", "2018-01-01", "2026-08-18", "2026-08-19", False),  # "currently"
    ("2012-01-01", "2018-01-01", "2017-01-01", "2020-01-01", True),   # 2017-2019
    ("2012-01-01", "2018-01-01", None, None, True),                   # no window
    # Boundaries are half-open: the end date is exclusive, so a succession is
    # not an overlap.
    ("2012-01-01", "2018-01-01", "2018-01-01", "2019-01-01", False),
    ("2012-01-01", "2018-01-01", "2011-01-01", "2012-01-01", False),
    ("2012-01-01", "2018-01-01", "2011-01-01", "2012-01-02", True),
    # Open-ended: a start with no stated end runs forward.
    ("2012-01-01", None, "2026-08-18", "2026-08-19", True),
    ("2012-01-01", None, "2011-01-01", "2012-01-01", False),
    # Very old relationships are retrieved on exactly the same terms.
    ("1996-01-01", "1999-01-01", "1997-01-01", "1998-01-01", True),
    ("1996-01-01", "1999-01-01", None, None, True),
    # An unknown window matches nothing when a period is asked for, and is
    # returned when none is.
    (None, None, "2015-01-01", "2016-01-01", False),
    (None, None, None, None, True),
]


@pytest.mark.parametrize(
    "claim_from,claim_until,window_start,window_end,expected", _OVERLAP_CASES
)
def test_the_window_rule_matches_conflict_detection(
    claim_from, claim_until, window_start, window_end, expected
):
    """One definition of "valid during", used by both sides of the system."""
    claim_window = claim_temporal.Window(claim_from, claim_until)
    if window_start is None and window_end is None:
        # No window asked for: the query applies no filter at all, which is not
        # something `overlaps` models. Everything is returned.
        assert expected is True
        return
    query_window = claim_temporal.Window(window_start, window_end)
    assert claim_temporal.overlaps(claim_window, query_window) is expected


@pytest.mark.skipif(not _graph_reachable(), reason="no Neo4j reachable")
@pytest.mark.parametrize(
    "claim_from,claim_until,window_start,window_end,expected", _OVERLAP_CASES
)
def test_the_window_clause_as_cypher_evaluates_it(
    claim_from, claim_until, window_start, window_end, expected
):
    """The reviewed clause text, run by the real engine, over no data.

    A read-only expression evaluation: the claim is a literal map rather than a
    node, so this touches nothing in the graph and still tests the exact string
    that ships.
    """
    from app.core.clients.graph import read_session

    clause = reg._overlap("c").strip()
    assert clause.startswith("AND ")
    query = (
        "WITH {valid_from: $vf, valid_until: $vu} AS c "
        f"RETURN ({clause[4:]}) AS included"
    )
    with read_session() as session:
        record = session.run(
            query, vf=claim_from, vu=claim_until,
            window_start=window_start, window_end=window_end,
        ).single()
    assert record["included"] is expected


def test_an_ended_claim_is_never_current_state_eligible():
    """The safety rule that is deliberately unchanged.

    The query-time `current_only` clause mirrors this function term for term,
    so an ended relationship cannot be asserted as present however it is asked
    for.
    """
    from app.knowledge.claims import conflicts as cf

    class _Claim:
        status = claim_types.STATUS_ACTIVE
        predicate = "LED_BY"
        temporal_basis = claim_types.BASIS_SUBJECT_PERIOD
        object_entity_id = PERSON
        valid_from = "2012-01-01"
        valid_until = "2018-01-01"

    assert cf.is_current_state_eligible(_Claim(), as_of="2015-01-01") is True
    assert cf.is_current_state_eligible(_Claim(), as_of="2026-08-18") is False


def test_the_current_clause_mirrors_projection_eligibility():
    """Not a paraphrase of it: the same status, the same basis list."""
    clause = reg._current_clause("c")
    assert "c.status = 'active'" in clause
    assert "c.temporal_basis IN $current_bases" in clause
    checked = reg.validate_parameters(
        reg.TEMPLATES["relationship_by_subject"],
        {"entity_id": ORG, "predicate": "LED_BY", "current_only": True},
    )
    assert tuple(checked["current_bases"]) == claim_types.CURRENT_STATE_BASES
    # `document` and `unknown` are excluded by the projector for the same
    # reason they are excluded here: neither is evidence about now.
    assert claim_types.BASIS_DOCUMENT not in checked["current_bases"]
    assert claim_types.BASIS_UNKNOWN not in checked["current_bases"]


# =========================================================================== #
# G. Age — there is no floor anywhere
# =========================================================================== #


def test_no_minimum_relationship_date_exists_in_the_retrieval_path():
    """Asserted structurally, because the failure mode is a constant nobody
    notices: a query-time comparison against "now" or against a fixed year
    would silently make old knowledge unreachable."""
    import inspect

    for module in (plans, qi, reg):
        source = inspect.getsource(module)
        for cypher_or_code in (source,):
            assert "MIGRATION_CUTOFF" not in cypher_or_code
    # No template compares a claim's dates to anything but the bound window.
    for template_id in reg.TEMPLATE_IDS:
        cypher = reg.TEMPLATES[template_id].cypher
        assert "date()" not in cypher, f"{template_id} compares against today"
        assert "datetime()" not in cypher, f"{template_id} compares against now"


def test_an_old_relationship_routes_and_windows_exactly_like_a_recent_one(
    monkeypatch,
):
    _routes_as(monkeypatch, [_Decision(PROJECT, "PROJECT", "Some Project")])
    old = router.route("Who led Some Project in 1996?").route
    recent = router.route("Who led Some Project in 2018?").route
    assert old.template_id == recent.template_id
    assert old.parameters["current_only"] == recent.parameters["current_only"]
    assert old.parameters["window_start"] == "1996-01-01"
    assert old.parameters["window_end"] == "1997-01-01"


def test_the_only_date_bound_in_the_claim_layer_is_a_noise_filter():
    """`MIN_YEAR` exists, and is not a relevance cutoff.

    1900 is there to reject extraction noise — a "year" of 0012 is a typo, not
    a fact. It is far below anything this corpus records (the oldest claim runs
    from 1970) and it bounds *parsing*, not retrieval, so it cannot make a valid
    historical relationship unreachable.
    """
    from app.knowledge.claims.validate import parse_iso_date

    assert claim_types.MIN_YEAR <= 1900
    assert parse_iso_date("1970-01-01") == "1970-01-01"
    assert parse_iso_date("1912-06-30") == "1912-06-30"
    assert parse_iso_date("0012-01-01") is None


def test_a_document_date_is_still_never_a_validity_basis():
    """The inference that would turn "reported in 2024" into "true from 2024",
    still refused, and still refused at the point it would matter most."""
    assert claim_types.BASIS_DOCUMENT not in claim_types.CURRENT_STATE_BASES


# =========================================================================== #
# Generation — a past relationship must not read as a present one
# =========================================================================== #


def _facts_block(rows, mode="historical", entity="DBT"):
    from app.retrieval.graph import facts

    class _Result:
        template_id = "relationship_by_object"
        rows = []
        claim_ids: list = []
        entity_ids: list = []
        document_ids: list = []
        truncated = False
        has_disputed = False

    result = _Result()
    result.rows = rows
    result.mode = mode

    class _Route:
        entity_name = entity
        plan = None

    return facts.as_block(result, _Route())


def test_an_ended_relationship_is_rendered_with_its_period():
    block = _facts_block([
        {"subject_name": "Some Project", "predicate": "FUNDED_BY",
         "object_name": "DBT", "valid_from": "2016-01-01",
         "valid_until": "2019-03-31", "claim_id": "claim_x", "status": "active"},
    ])
    assert "2016-01-01 until 2019-03-31" in block.text
    assert "including past relationships" in block.text


def test_a_current_result_says_so_in_its_heading():
    block = _facts_block([
        {"subject_name": "Some Project", "predicate": "FUNDED_BY",
         "object_name": "DBT", "valid_from": "2019-01-01",
         "valid_until": None, "claim_id": "claim_y"},
    ], mode="current")
    assert "as currently recorded" in block.text
    assert "since 2019-01-01" in block.text


def test_a_windowed_result_names_its_window():
    from app.retrieval.graph import facts

    class _Result:
        template_id = "relationship_by_subject"
        mode = "historical"
        rows = [{"subject_name": "P", "predicate": "LED_BY",
                 "object_name": "A Person", "valid_from": "2012-01-01",
                 "valid_until": "2018-01-01", "claim_id": "c"}]
        claim_ids: list = []
        entity_ids: list = []
        document_ids: list = []
        truncated = False
        has_disputed = False

    class _Plan:
        temporal = qi.TemporalIntent("as_of", "2015-01-01", "2016-01-01")

    class _Route:
        entity_name = "P"
        plan = _Plan()

    text = facts.render(_Result(), _Route())
    assert "between 2015-01-01 and 2016-01-01" in text


def test_a_two_hop_row_is_written_the_way_round_the_graph_says():
    block = _facts_block([
        {"anchor_name": "DBT", "via_predicate": "FUNDED_BY",
         "mid_name": "Some Project", "predicate": "LED_BY",
         "far_name": "A Person", "anchor_is_subject": False,
         "mid_is_subject": True, "claim_id": "claim_z",
         "valid_from": "2010-01-01", "valid_until": "2013-01-01"},
    ])
    assert "Some Project is funded by DBT" in block.text
    assert "Some Project is led by A Person" in block.text


def test_a_disputed_row_is_labelled():
    block = _facts_block([
        {"subject_name": "P", "predicate": "LED_BY", "object_name": "A",
         "status": "disputed", "claim_id": "c1"},
    ])
    assert "DISPUTED" in block.text


def test_every_approved_predicate_has_a_readable_phrase():
    from app.retrieval.graph import facts

    for name in vocab.PREDICATE_NAMES:
        assert facts._predicate_phrase(name) != "is related to", name


def test_the_graph_block_is_labelled_as_the_graph_in_the_prompt():
    """It used to render as "[1] (source)", which said nothing about what it
    was or how its lines should be read."""
    from app.generation.prompts import _source_hint, has_graph_facts

    assert "knowledge graph" in _source_hint(
        {"kind": "graph_facts", "mode": "historical"}
    )
    assert "past relationships" in _source_hint(
        {"kind": "graph_facts", "mode": "historical"}
    )
    assert "current relationships" in _source_hint(
        {"kind": "graph_facts", "mode": "current"}
    )
    block = _facts_block([
        {"subject_name": "P", "predicate": "LED_BY", "object_name": "A",
         "claim_id": "c"},
    ])
    assert has_graph_facts([block])


def test_the_validity_rule_reaches_the_prompt_only_when_graph_facts_are_present():
    from app.core.models.context import ContextBlock
    from app.generation import answerer

    graph_block = _facts_block([
        {"subject_name": "P", "predicate": "LED_BY", "object_name": "A",
         "valid_from": "2012-01-01", "valid_until": "2018-01-01",
         "claim_id": "c"},
    ])
    prose = ContextBlock(n=1, text="some passage", payload={"source_type": "pdf"})

    with_graph = answerer._build_system(
        None, None, graph_facts=True
    )
    without = answerer._build_system(None, None, graph_facts=False)

    assert "knowledge graph" in with_graph
    assert "has **ended**" in with_graph
    assert "never state a date that does not appear" in with_graph.replace("\n", " ")
    # Keyed to the validity rule itself rather than to the words "knowledge
    # graph": rule 6 now names the graph among the source kinds one answer may
    # cite from, which is true whether or not a facts block is present. What
    # has to stay conditional is the rule about reading validity windows.
    assert "has **ended**" not in without
    # And the flag is derived from the blocks, not passed by hand at each site.
    from app.generation.prompts import has_graph_facts

    assert has_graph_facts([graph_block, prose]) is True
    assert has_graph_facts([prose]) is False


def test_the_extra_rules_are_numbered_without_a_gap():
    """A rule 11 with no rule 10 reads as a truncated instruction."""
    from app.generation import answerer

    only_graph = answerer._build_system(None, None, graph_facts=True)
    assert "\n10. One block is headed" in only_graph

    both = answerer._build_system(
        None, None, has_history=True, graph_facts=True
    )
    assert "\n10. Earlier conversation turns" in both
    assert "\n11. One block is headed" in both


def test_the_graph_block_is_not_counted_as_a_pdf():
    """It carries no `source_type`, so it used to read as "not website" — a PDF.

    Two visible consequences: a context of one graph block plus website passages
    looked *mixed* and got a two-block answer structure it had no use for, and
    the graph's own facts were printed under the heading "From our documents".
    They did not come from a document.

    Both readers are gone — there is one answer structure now, and no group
    headings — so what is left to pin is the block's own description of itself.
    """
    from app.generation.prompts import format_context_blocks

    graph = _facts_block([
        {"subject_name": "P", "predicate": "LED_BY", "object_name": "A",
         "claim_id": "c"},
    ])
    rendered = format_context_blocks([graph])
    assert "knowledge graph" in rendered
    assert "pdf" not in rendered.lower().split("\n")[0]


# --------------------------------------------------------------------------- #
# The graph block is not a document: prompt grouping and citations
#
# `_source_kinded` fixed `has_mixed_sources` and `_is_website_led`, but two other
# readers of "what kind of source is this" were left on the old rule, and both
# told the user something untrue. These cover them.
# --------------------------------------------------------------------------- #


def test_the_context_is_not_grouped_by_source_kind():
    """The graph block used to be announced to the model as PDF contents.

    `format_context_blocks` prefixed groups with "— TERI website —" and
    "— PDF documents —", deciding a block's group with
    `"website" if source_type == "website" else "pdf"`; the graph block carries
    no `source_type` by design, so it fell to the else branch and the context
    opened with "— PDF documents —" directly above verified relationships.

    The headings are gone with the segregated context that produced them — the
    blocks arrive in one evidential order and are presented in it — so the bug
    has no branch left to take. Each block still names its own kind in its own
    header, which is what weighing the evidence actually needs.
    """
    from app.core.models.context import ContextBlock
    from app.generation.prompts import format_context_blocks

    graph = _facts_block([
        {"subject_name": "P", "predicate": "LED_BY", "object_name": "A",
         "claim_id": "claim_x"},
    ])
    website = ContextBlock(n=2, text="w", payload={"source_type": "website"})
    pdf = ContextBlock(n=3, text="p", payload={"source_type": "pdf_attachment"})

    rendered = format_context_blocks([graph, website, pdf])
    assert "— TERI website —" not in rendered
    assert "— PDF documents —" not in rendered
    # Each block still says what it is, in its own header and nowhere else.
    assert "knowledge graph" in rendered
    assert "[2] (website)" in rendered
    assert "[3] (pdf_attachment)" in rendered


def test_the_graph_block_is_cited_as_the_graph_not_as_a_pdf():
    """A citation for the graph block used to read `type="pdf_attachment"` with
    a null title and null url.

    `_source_type` ends in `or "pdf_attachment"`, so the block's deliberately
    absent `source_type` became a PDF, and the frontend — which labels a chip
    `title || document_id || type` — rendered the literal string
    "pdf_attachment" to the user under the heading "PDFs".
    """
    from app.retrieval.context.citations import GRAPH_CITATION_TYPE, build_citations

    block = _facts_block([
        {"subject_name": "P", "predicate": "LED_BY", "object_name": "A",
         "claim_id": "claim_x"},
    ])
    block.payload["claim_ids"] = ["claim_x"]
    (citation,) = build_citations([block])

    assert citation.type == GRAPH_CITATION_TYPE == "knowledge_graph"
    assert citation.title and "knowledge graph" in citation.title.lower()
    # No fabricated link and no document to resolve to: the block is not one.
    assert citation.url is None
    assert citation.document_id is None
    assert citation.page is None


def test_a_current_graph_citation_says_current_and_a_historical_one_does_not():
    """The citation must not be the one place a past relationship reads as a
    present one."""
    from app.retrieval.context.citations import build_citations

    historical = _facts_block(
        [{"subject_name": "P", "predicate": "LED_BY", "object_name": "A",
          "claim_id": "c1"}],
        mode="historical",
    )
    historical.payload["claim_ids"] = ["c1"]
    current = _facts_block(
        [{"subject_name": "P", "predicate": "LED_BY", "object_name": "A",
          "claim_id": "c1"}],
        mode="current",
    )
    current.payload["claim_ids"] = ["c1"]

    (hist,) = build_citations([historical])
    (curr,) = build_citations([current])
    assert "past relationships" in hist.title
    assert "currently recorded" in curr.title
    assert "1 record" in hist.title


def test_document_citations_are_unchanged_by_the_graph_branch():
    """The graph branch must not alter how an ordinary source is described."""
    from app.core.models.context import ContextBlock
    from app.retrieval.context.citations import build_citations

    website = ContextBlock(
        n=1, text="w",
        payload={"source_type": "website", "title": "A page",
                 "source_url": "https://example.org/a", "document_id": "doc-1"},
    )
    pdf = ContextBlock(
        n=2, text="p",
        payload={"source_type": "pdf_attachment", "title": "A report",
                 "file_url": "https://example.org/a.pdf", "page_number": 4,
                 "document_id": "doc-2"},
    )
    web_citation, pdf_citation = build_citations([website, pdf])
    assert (web_citation.type, web_citation.url) == ("website", "https://example.org/a")
    assert web_citation.document_id == "doc-1"
    assert pdf_citation.type == "pdf_attachment"
    assert pdf_citation.url == "https://example.org/a.pdf#page=4"
    assert pdf_citation.page == 4


def test_one_definition_of_the_graph_facts_marker():
    """Three layers read this marker. A second copy of the literal is a copy
    that can drift, which is how two of them came to disagree."""
    from app.core.models import context as core
    from app.generation import prompts
    from app.retrieval.graph import facts

    block = facts.as_block(
        type("R", (), {
            "rows": [{"subject_name": "P", "predicate": "LED_BY",
                      "object_name": "A", "claim_id": "c"}],
            "mode": "historical", "template_id": "relationship_by_object",
            "claim_ids": ["c"], "entity_ids": [], "document_ids": [],
            "truncated": False, "has_disputed": False,
        })(),
    )
    assert prompts.GRAPH_FACTS_KIND is core.GRAPH_FACTS_KIND
    assert prompts.is_graph_facts is core.is_graph_facts
    assert core.is_graph_facts(block.payload)
    assert core.source_kind(block.payload) is None


def test_an_undated_row_says_it_has_no_dates_rather_than_saying_nothing():
    """Every predicate phrase is present tense, so an undated row with no
    parenthetical asserted a present fact.

    Measured on the live corpus before the fix: the undated PARTNER_OF claim for
    "Framework for mainstreaming eco-housing in Pune" produced the answer "The
    Framework ... is a partner of TERI", with nothing in the context to say the
    relationship has no recorded period.
    """
    block = _facts_block([
        {"subject_name": "A project", "predicate": "PARTNER_OF",
         "object_name": "TERI", "claim_id": "claim_x", "status": "active",
         "valid_from": None, "valid_until": None},
    ])
    assert "(no recorded dates)" in block.text


@pytest.mark.parametrize(
    "row, expected",
    [
        ({"valid_from": "2016-01-01", "valid_until": "2019-03-31"},
         "(2016-01-01 until 2019-03-31)"),
        ({"valid_from": None, "valid_until": "2019-03-31"}, "(until 2019-03-31)"),
        ({"valid_from": "2016-01-01", "valid_until": None}, "(since 2016-01-01)"),
        ({"valid_from": None, "valid_until": None}, "(no recorded dates)"),
    ],
)
def test_every_window_shape_is_stated_explicitly(row, expected):
    """A dated row must not lose its dates, and an undated one must not stay
    silent. Both directions, so the fix for one cannot break the other."""
    from app.retrieval.graph import facts

    assert facts._validity(row).strip() == expected


def test_the_prompt_rule_describes_the_marker_the_renderer_actually_emits():
    """The rule used to describe "a line with no period in parentheses", a shape
    the renderer no longer produces. A rule about a shape that cannot occur
    teaches the model nothing about the shape that does."""
    from app.generation.prompts import graph_facts_rule
    from app.retrieval.graph import facts

    rule = graph_facts_rule(4)
    marker = facts._validity({"valid_from": None, "valid_until": None}).strip()
    assert marker in rule


# --------------------------------------------------------------------------- #
# Two-hop chain ordering: the chain is built outward from the anchor
# --------------------------------------------------------------------------- #

def test_a_spurious_cue_does_not_shadow_the_chain_the_question_asked_for():
    """The measured regression, with the live corpus's own numbers.

    "Which investigators lead work granted by the Ministry of Environment and
    Forests?" names three predicates, because "work" is a WORKS_AT cue. Iterating
    candidate pairs in cue order reached (WORKS_AT, LED_BY) first — legal, since
    an organization may be an employer and a person may lead a project — so the
    query asked for employees of the Ministry who lead projects. The corpus holds
    no WORKS_AT claim at all, so that returned zero rows, while the chain the
    question actually asked for, FUNDED_BY then LED_BY, held ten.
    """
    from app.retrieval.graph import intent as qi, router as R

    question = ("Which investigators lead work granted by the "
                "Ministry of Environment and Forests?")
    relational = qi.read_relational(question)
    # The premise: all three cues really do fire, and WORKS_AT really is named
    # before FUNDED_BY. Without this the test could pass for the wrong reason.
    assert relational.predicates == ("LED_BY", "WORKS_AT", "FUNDED_BY")

    anchor = (question.index("Ministry"), question.index("Forests?") + 7)
    ordered = R._nearest_first(relational, anchor)
    assert ordered[0] == "FUNDED_BY", (
        "the relationship named next to the anchor must be the first hop"
    )


def test_the_chain_order_is_unchanged_for_the_shapes_that_already_worked():
    from app.retrieval.graph import intent as qi, router as R

    for question in (
        "Who leads the projects funded by the Department of Biotechnology?",
        "Who leads projects funded by the Asian Development Bank?",
    ):
        relational = qi.read_relational(question)
        anchor_start = max(question.rfind("Department"), question.rfind("Asian"))
        anchor = (anchor_start, len(question) - 1)
        assert R._nearest_first(relational, anchor)[0] == "FUNDED_BY"


def test_a_cue_on_either_side_of_the_anchor_counts_the_same():
    """"projects funded by X" and "X's funded projects" both put FUNDED_BY next
    to the anchor, so distance is measured to the nearer edge of the span."""
    from app.retrieval.graph import intent as qi, router as R

    trailing = "Who leads projects funded by ORGNAME"
    anchor = (trailing.index("ORGNAME"), len(trailing))
    assert R._nearest_first(qi.read_relational(trailing), anchor)[0] == "FUNDED_BY"

    leading = "ORGNAME funded which projects, and who leads them"
    anchor = (0, len("ORGNAME"))
    assert R._nearest_first(qi.read_relational(leading), anchor)[0] == "FUNDED_BY"


def test_a_cue_inside_the_anchor_span_is_distance_zero():
    """Masking blanks entity spans before cues are read, so this should not
    normally arise — but if it does, a cue inside the name is as close as a cue
    can be, not maximally far."""
    from app.retrieval.graph import intent as qi, router as R

    relational = qi.RelationalIntent(
        predicates=("FUNDED_BY", "LED_BY"),
        offsets={"FUNDED_BY": 50, "LED_BY": 5},
    )
    assert R._nearest_first(relational, (45, 60))[0] == "FUNDED_BY"


def test_a_predicate_with_no_recorded_offset_sorts_last_but_is_kept():
    """It is still a legal candidate; the ordering simply cannot speak for it."""
    from app.retrieval.graph import intent as qi, router as R

    relational = qi.RelationalIntent(
        predicates=("FUNDED_BY", "LED_BY", "PARTNER_OF"),
        offsets={"FUNDED_BY": 40, "LED_BY": 5},
    )
    ordered = R._nearest_first(relational, (45, 50))
    assert ordered[-1] == "PARTNER_OF"
    assert set(ordered) == {"FUNDED_BY", "LED_BY", "PARTNER_OF"}


def test_read_relational_reports_where_each_cue_was_found():
    """The offsets are what makes the ordering above possible; a reader that
    silently lost them would take the router back to cue order."""
    from app.retrieval.graph import intent as qi

    relational = qi.read_relational("Who leads projects funded by someone?")
    assert set(relational.offsets) == set(relational.predicates)
    assert relational.offsets["LED_BY"] < relational.offsets["FUNDED_BY"]


def test_resolution_reports_anchor_spans_aligned_with_the_resolved_entities():
    """Two span lists with different meanings: every mention (for masking) and
    the resolved anchors (for chain ordering). Confusing them silently
    misattributes an anchor position, which is how the ordering would go wrong
    on a question that also names something unresolvable."""
    from app.retrieval.graph import router as R

    class _Mention:
        def __init__(self, surface, start, end):
            self.surface_text = surface
            self.start_offset = start
            self.end_offset = end
            self.entity_type = "ORGANIZATION"

        chunk_id = document_id = "query"

    mentions = [_Mention("Nobody", 0, 6), _Mention("DBT", 20, 23)]

    class _Unresolved:
        canonical = False
        entity_id = None
        decision = "UNRESOLVED"
        tier = ""
        claim_eligible = False
        candidate_audit: list = []
        score = 0.0
        entity_type = "ORGANIZATION"
        surface_text = "Nobody"

    resolved_decision = _Decision(ORG, "ORGANIZATION", "DBT")

    import app.knowledge.extract as extract_mod
    import app.knowledge.resolver as resolver_mod
    import app.knowledge.gazetteer as gaz_mod

    original = (extract_mod.extract_mentions, resolver_mod.resolve_mention,
                gaz_mod.get_gazetteer)
    try:
        extract_mod.extract_mentions = lambda *a, **k: mentions
        gaz_mod.get_gazetteer = lambda: None
        resolver_mod.resolve_mention = (
            lambda m, i, c: _Unresolved() if m.surface_text == "Nobody"
            else resolved_decision
        )
        resolved, ambiguous, spans, resolved_spans = R._resolve_entities("q", None)
    finally:
        (extract_mod.extract_mentions, resolver_mod.resolve_mention,
         gaz_mod.get_gazetteer) = original

    assert spans == [(0, 6), (20, 23)], "every mention is masked"
    assert len(resolved) == len(resolved_spans) == 1
    assert resolved_spans == [(20, 23)], "the anchor's own span, not the first one"


# --------------------------------------------------------------------------- #
# The block states its own record count
#
# Reaching the graph from the structured path (see tests/test_combined_answer.py)
# means a `count` question can now be answered from graph rows. Asked to count a
# 40-row block, the model answered "a total of 56 projects". The traversal knows
# the number, so the block says it.
# --------------------------------------------------------------------------- #

def _rows(n, **extra):
    return [
        dict({"subject_name": f"Project {i}", "predicate": "FUNDED_BY",
              "object_name": "DBT", "claim_id": f"claim_{i}",
              "valid_from": "2010-01-01", "valid_until": "2012-01-01"}, **extra)
        for i in range(n)
    ]


def test_the_block_states_its_record_total():
    block = _facts_block(_rows(40))
    assert "(40 records in total)" in block.text
    assert block.text.count("- Project") == 40


def test_a_single_record_is_not_pluralised():
    assert "(1 record in total)" in _facts_block(_rows(1)).text


def test_a_truncated_block_states_the_total_and_what_is_missing():
    """The count must be the number the graph holds, not the number rendered —
    that difference is exactly what a counted answer gets wrong."""
    from app.retrieval.graph import facts

    block = _facts_block(_rows(facts.MAX_LINES_HISTORICAL + 12))
    total = facts.MAX_LINES_HISTORICAL + 12
    assert f"({total} records in total" in block.text
    assert f"{facts.MAX_LINES_HISTORICAL} shown" in block.text
    assert "12 further records not shown" in block.text


def test_a_result_the_traversal_itself_capped_says_so():
    from app.retrieval.graph import facts

    class _Result:
        template_id = "relationship_by_object"
        mode = "historical"
        rows = _rows(3)
        claim_ids = ["c"]
        entity_ids: list = []
        document_ids: list = []
        truncated = True
        has_disputed = False

    text = facts.render(_Result())
    assert "3 records shown" in text
    assert "more records exist than were retrieved" in text


def test_the_prompt_tells_the_model_to_read_the_total_not_count_lines():
    from app.generation.prompts import graph_facts_rule

    rule = graph_facts_rule(4)
    assert "records in total" in rule
    assert "never count the lines yourself" in rule
