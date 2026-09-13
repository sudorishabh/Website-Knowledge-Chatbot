"""Unit tests for Neo4j projection.

No live Neo4j: a fake session records the statements and parameters that would
be sent. That is deliberate rather than a limitation — asserting on the
*emitted* Cypher is what makes the safety properties structural. A statement
that interpolated a value, or a projection that leaked a provisional identity,
fails here rather than in review.
"""

from __future__ import annotations

import pytest

from app.knowledge.graph import project as gp
from app.knowledge.graph import writer


class _Result:
    def __init__(self, rows=(), deleted=0):
        self._rows = list(rows)
        self._deleted = deleted

    def consume(self):
        """Summary shape `writer.run_sweep` reads. Zero deletions by default; a
        test that cares sets `deleted` on the response it registered."""
        class _Counters:
            nodes_deleted = self._deleted

        class _Summary:
            counters = _Counters()

        return _Summary()

    def single(self):
        return self._rows[0] if self._rows else {"n": 0}

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    """Records statements and parameters instead of running them."""

    def __init__(self, responses=None):
        self.calls: list[tuple[str, dict]] = []
        self._responses = responses or {}

    def run(self, statement, **params):
        self.calls.append((statement, params))
        for needle, response in self._responses.items():
            if needle in statement:
                # A registered response may be a row list, or a ready-made
                # `_Result` when the test needs to control the deletion count a
                # sweep reports.
                return (
                    response if isinstance(response, _Result) else _Result(response)
                )
        return _Result()

    @property
    def statements(self) -> str:
        return "\n".join(s for s, _ in self.calls)

    def rows_for(self, needle: str) -> list[dict]:
        return [
            row
            for statement, params in self.calls
            if needle in statement
            for row in params.get("rows", [])
        ]


# --------------------------------------------------------------------------- #
# Safety: labels and relationship types come from a code-side allow-list
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "label", ["Entity", "Person", "Organization", "Project", "Claim", "Alias"]
)
def test_allowed_labels_pass(label):
    assert writer.safe_label(label) == label


@pytest.mark.parametrize(
    "label",
    ["Person) DETACH DELETE (n", "Secret", "", "Entity;DROP", "person"],
)
def test_unknown_labels_are_refused(label):
    """Cypher cannot parameterize a label, so this is the injection surface.
    Anything not in the allow-list must raise rather than reach the driver."""
    with pytest.raises(writer.UnsafeIdentifier):
        writer.safe_label(label)


@pytest.mark.parametrize("name", ["LED_BY", "FUNDED_BY", "SUBJECT", "SUPERSEDES"])
def test_allowed_relationships_pass(name):
    assert writer.safe_relationship(name) == name


@pytest.mark.parametrize(
    "name", ["SECRETLY_CONTROLS", "LED_BY]->() DELETE r //", "", "led_by"]
)
def test_unknown_relationships_are_refused(name):
    with pytest.raises(writer.UnsafeIdentifier):
        writer.safe_relationship(name)


def test_relationship_allow_list_is_exactly_the_closed_vocabulary():
    """A predicate that is not in the vocabulary cannot become an edge type."""
    from app.knowledge.claims import predicates as vocab
    from app.knowledge.graph import schema

    for name in vocab.PREDICATE_NAMES:
        assert writer.safe_relationship(name) == name
    for name in schema.PROVENANCE_RELATIONSHIPS:
        assert writer.safe_relationship(name) == name


def test_every_statement_is_parameterized():
    """No value is ever formatted into Cypher. The only `%s` in these constants
    are the label/relationship slots, which the allow-list fills."""
    import re

    for name in dir(writer):
        if not name.isupper():
            continue
        statement = getattr(writer, name)
        if not isinstance(statement, str) or "MATCH" not in statement.upper():
            continue
        # Anything that looks like an interpolated value rather than a
        # parameter would show up as a quoted literal next to a property.
        assert not re.search(r"=\s*'", statement), name
        assert not re.search(r'=\s*"', statement), name


# --------------------------------------------------------------------------- #
# What may and may not be projected
# --------------------------------------------------------------------------- #

def _entity(entity_id, entity_type, trust="derived", eligible=1):
    return {
        "entity_id": entity_id, "entity_type": entity_type,
        "canonical_name": entity_id, "normalized_name": entity_id,
        "trust": trust, "cms_uuid": None, "source": "test", "status": "active",
        "claim_eligible": eligible,
    }


def _claim(claim_id, subject, obj=None, *, status="active", predicate="FUNDED_BY",
           valid_from="2019-01-01", valid_until=None, basis="subject_period",
           chunk=None, document="doc-1"):
    return {
        "claim_id": claim_id, "predicate": predicate,
        "subject_entity_id": subject, "object_entity_id": obj,
        "object_literal": None, "valid_from": valid_from,
        "valid_until": valid_until, "temporal_basis": basis, "confidence": 1.0,
        "status": status, "evidence_kind": "cms_field" if not chunk else "chunk",
        "source_field": "field_completed_sponsors" if not chunk else None,
        "quote": None, "quote_start": None, "quote_end": None,
        "document_id": document, "chunk_id": chunk,
        "extraction_method": "cms_field", "extractor_version": "test",
    }


def _project(monkeypatch, entities, claims, links=(), documents=(), aliases=(),
             as_of="2026-01-01"):
    monkeypatch.setattr(gp, "_load_entities", lambda: list(entities))
    monkeypatch.setattr(gp, "_load_claims", lambda: list(claims))
    monkeypatch.setattr(gp, "_load_links", lambda: list(links))
    monkeypatch.setattr(gp, "_load_aliases", lambda ids: list(aliases))
    monkeypatch.setattr(gp, "_load_documents", lambda ids: list(documents))
    session = _FakeSession()
    report = gp.project(session=session, projection_version="v-test", as_of=as_of)
    return session, report


def test_only_projectable_entities_are_projected(monkeypatch):
    """`_load_entities` filters at the source, so a provisional identity never
    reaches the graph at all and no traversal can arrive at one.

    "Projectable" is broad eligibility *or* a predicate-scoped trust level —
    see `test_scoped_trust_projection` for why the second exists. Provisional is
    neither, which is the part that must not drift.
    """
    import inspect

    from app.knowledge.seed import TRUST_PROVISIONAL

    clause, params = gp._projectable_clause()
    assert "claim_eligible = 1" in clause
    assert TRUST_PROVISIONAL not in params
    assert "status = 'active'" in inspect.getsource(gp._load_entities)


def test_a_claim_naming_an_ineligible_entity_is_refused(monkeypatch):
    """The second gate: even a staged claim is refused if its entity is no
    longer eligible, because the entity store is authoritative at project time."""
    entities = [_entity("project_1", "PROJECT"), _entity("org_1", "ORGANIZATION")]
    claims = [
        _claim("claim_ok", "project_1", "org_1"),
        _claim("claim_bad", "project_1", "org_missing"),
        _claim("claim_bad2", "person_provisional", "org_1"),
    ]
    session, report = _project(monkeypatch, entities, claims)
    projected = {r["claim_id"] for r in session.rows_for("MERGE (cl:Claim")}
    assert projected == {"claim_ok"}
    assert report.skipped["claim_entity_not_eligible"] == 2


def test_trust_is_carried_onto_the_node(monkeypatch):
    """`pi_attested` must stay distinguishable from `authoritative` in the
    graph, or the distinction Phase 5.1 built is lost at projection."""
    entities = [
        _entity("person_1", "PERSON", trust="pi_attested"),
        _entity("person_2", "PERSON", trust="authoritative"),
    ]
    session, _ = _project(monkeypatch, entities, [])
    trust = {r["entity_id"]: r["trust"] for r in session.rows_for("MERGE (e:Entity")}
    assert trust == {"person_1": "pi_attested", "person_2": "authoritative"}


def test_typed_labels_are_applied_per_entity_type(monkeypatch):
    entities = [
        _entity("person_1", "PERSON"),
        _entity("org_1", "ORGANIZATION"),
        _entity("project_1", "PROJECT"),
    ]
    session, _ = _project(monkeypatch, entities, [])
    assert "SET e:Person" in session.statements
    assert "SET e:Organization" in session.statements
    assert "SET e:Project" in session.statements


# --------------------------------------------------------------------------- #
# Claims, evidence, and history
# --------------------------------------------------------------------------- #

def test_claims_of_every_status_are_projected(monkeypatch):
    """History is the point: a superseded claim is still the answer to "who led
    this in 2019"."""
    entities = [_entity("project_1", "PROJECT"), _entity("org_1", "ORGANIZATION")]
    claims = [
        _claim("claim_a", "project_1", "org_1", status="active"),
        _claim("claim_b", "project_1", "org_1", status="superseded"),
        _claim("claim_c", "project_1", "org_1", status="disputed"),
    ]
    session, _ = _project(monkeypatch, entities, claims)
    projected = {r["claim_id"] for r in session.rows_for("MERGE (cl:Claim")}
    assert projected == {"claim_a", "claim_b", "claim_c"}


def test_a_chunk_claim_is_supported_by_its_chunk(monkeypatch):
    entities = [_entity("project_1", "PROJECT"), _entity("org_1", "ORGANIZATION")]
    claims = [_claim("claim_a", "project_1", "org_1", chunk="chunk-1")]
    session, _ = _project(monkeypatch, entities, claims)
    assert session.rows_for("MERGE (c:Chunk")
    assert "MERGE (cl)-[:SUPPORTED_BY]->(c)" in session.statements


def test_a_cms_claim_is_supported_by_its_document(monkeypatch):
    """No chunk exists for a metadata fact, so evidence points at the document
    rather than inventing a span."""
    entities = [_entity("project_1", "PROJECT"), _entity("org_1", "ORGANIZATION")]
    claims = [_claim("claim_a", "project_1", "org_1", chunk=None)]
    session, _ = _project(monkeypatch, entities, claims)
    assert not session.rows_for("MERGE (c:Chunk")
    assert "MERGE (cl)-[:SUPPORTED_BY]->(d)" in session.statements


def test_only_chunks_carrying_a_claim_get_a_stub(monkeypatch):
    """A stub per corpus chunk would put ~149k nodes in the graph for no
    traversal benefit."""
    entities = [_entity("project_1", "PROJECT"), _entity("org_1", "ORGANIZATION")]
    claims = [_claim("claim_a", "project_1", "org_1", chunk="chunk-1")]
    session, _ = _project(monkeypatch, entities, claims)
    assert {r["chunk_id"] for r in session.rows_for("MERGE (c:Chunk")} == {"chunk-1"}


def test_contradiction_and_supersession_links_are_projected(monkeypatch):
    entities = [_entity("project_1", "PROJECT"), _entity("person_1", "PERSON")]
    claims = [
        _claim("claim_a", "project_1", "person_1", predicate="LED_BY"),
        _claim("claim_b", "project_1", "person_1", predicate="LED_BY"),
    ]
    links = [
        {"from_claim_id": "claim_a", "to_claim_id": "claim_b",
         "kind": "supersedes", "reason": "later start"},
        {"from_claim_id": "claim_b", "to_claim_id": "claim_a",
         "kind": "contradicts", "reason": "overlap"},
    ]
    session, report = _project(monkeypatch, entities, claims, links=links)
    assert "SUPERSEDES" in report.relationships
    assert "CONTRADICTS" in report.relationships


def test_a_link_to_an_unprojected_claim_is_dropped(monkeypatch):
    entities = [_entity("project_1", "PROJECT"), _entity("org_1", "ORGANIZATION")]
    claims = [_claim("claim_a", "project_1", "org_1")]
    links = [{"from_claim_id": "claim_a", "to_claim_id": "claim_missing",
              "kind": "supersedes", "reason": "x"}]
    _, report = _project(monkeypatch, entities, claims, links=links)
    assert "SUPERSEDES" not in report.relationships


# --------------------------------------------------------------------------- #
# Derived current state — the narrowest thing the graph asserts
# --------------------------------------------------------------------------- #

def test_disputed_claims_produce_no_current_state_edge(monkeypatch):
    """The safety property the whole conflict layer exists to produce."""
    entities = [_entity("project_1", "PROJECT"), _entity("person_1", "PERSON")]
    claims = [_claim("claim_a", "project_1", "person_1", predicate="LED_BY",
                     status="disputed")]
    _, report = _project(monkeypatch, entities, claims)
    assert not any("current" in key for key in report.relationships)


@pytest.mark.parametrize("status", ["superseded", "retracted", "disputed"])
def test_only_active_claims_become_current_state(monkeypatch, status):
    entities = [_entity("project_1", "PROJECT"), _entity("org_1", "ORGANIZATION")]
    claims = [_claim("claim_a", "project_1", "org_1", status=status)]
    _, report = _project(monkeypatch, entities, claims)
    assert not any("current" in key for key in report.relationships)


def test_an_expired_claim_is_history_not_current(monkeypatch):
    entities = [_entity("project_1", "PROJECT"), _entity("org_1", "ORGANIZATION")]
    claims = [_claim("claim_a", "project_1", "org_1",
                     valid_from="2010-01-01", valid_until="2012-01-01")]
    _, report = _project(monkeypatch, entities, claims, as_of="2026-01-01")
    assert not any("current" in key for key in report.relationships)
    # ...but the claim itself is still projected.
    assert report.nodes["Claim"] == 1


def test_an_open_ended_claim_is_current(monkeypatch):
    entities = [_entity("project_1", "PROJECT"), _entity("org_1", "ORGANIZATION")]
    claims = [_claim("claim_a", "project_1", "org_1", valid_from="2019-01-01")]
    session, report = _project(monkeypatch, entities, claims, as_of="2026-01-01")
    assert report.relationships["FUNDED_BY (current)"] == 1
    row = session.rows_for("MERGE (s)-[r:FUNDED_BY")[0]
    assert row["claim_id"] == "claim_a"


def test_every_current_edge_carries_its_claim_id(monkeypatch):
    """The provenance contract: edge -> claim_id -> claim -> chunk/document."""
    entities = [_entity("project_1", "PROJECT"), _entity("org_1", "ORGANIZATION")]
    claims = [_claim("claim_a", "project_1", "org_1")]
    session, _ = _project(monkeypatch, entities, claims)
    assert "MERGE (s)-[r:FUNDED_BY {claim_id: row.claim_id}]->(o)" in session.statements


def test_a_literal_claim_never_becomes_an_edge(monkeypatch):
    """A role is a property, not a relationship between two nodes."""
    entities = [_entity("person_1", "PERSON")]
    claims = [_claim("claim_a", "person_1", None, predicate="HAS_ROLE")]
    claims[0]["object_literal"] = "Senior Director"
    _, report = _project(monkeypatch, entities, claims)
    assert not any("current" in key for key in report.relationships)


def test_stale_generations_are_removed(monkeypatch):
    """A claim that stopped being current loses its edge without anything having
    to remember the edge existed."""
    entities = [_entity("project_1", "PROJECT"), _entity("org_1", "ORGANIZATION")]
    session, _ = _project(monkeypatch, entities, [])
    assert "r.projection_version <> $projection_version" in session.statements


def test_projection_version_is_a_parameter_not_interpolated(monkeypatch):
    entities = [_entity("project_1", "PROJECT")]
    session, _ = _project(monkeypatch, entities, [])
    versioned = [
        params for statement, params in session.calls
        if "$projection_version" in statement
    ]
    assert versioned
    assert all(p.get("projection_version") == "v-test" for p in versioned)


def test_projection_versions_are_distinct_per_run():
    from datetime import datetime, timezone

    a = gp.make_projection_version(at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    b = gp.make_projection_version(at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert a != b and a.startswith(gp.PROJECTOR_VERSION)


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #

def test_every_write_is_a_merge_on_a_deterministic_key():
    """What makes re-projection an update rather than a duplication."""
    for name in (
        "MERGE_ENTITY", "MERGE_ALIAS", "MERGE_CLAIM", "MERGE_CHUNK",
        "MERGE_DOCUMENT", "MERGE_PREDICATE",
    ):
        statement = getattr(writer, name)
        assert "MERGE" in statement
        assert "CREATE " not in statement


def test_repeated_projection_emits_identical_rows(monkeypatch):
    entities = [_entity("project_1", "PROJECT"), _entity("org_1", "ORGANIZATION")]
    claims = [_claim("claim_a", "project_1", "org_1")]
    first, _ = _project(monkeypatch, entities, claims)
    second, _ = _project(monkeypatch, entities, claims)
    assert first.rows_for("MERGE (cl:Claim") == second.rows_for("MERGE (cl:Claim")
    assert first.statements == second.statements


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

def test_verification_flags_a_disputed_claim_with_an_edge(monkeypatch):
    """The check worth having: a recount would not catch this."""
    from app.knowledge.graph import verify as gv

    monkeypatch.setattr(gv, "__name__", gv.__name__)
    session = _FakeSession(responses={
        "c.status <> 'active'": [{"claim_id": "claim_x", "status": "disputed"}],
    })
    monkeypatch.setattr(
        "app.knowledge.graph.project._load_entities", lambda: []
    )
    monkeypatch.setattr("app.catalog.assertions.all_staged", lambda: [])
    monkeypatch.setattr(
        "app.catalog.assertions._ensure", lambda: None
    )

    class _Cur:
        def execute(self, *a, **kw):
            pass

        def fetchone(self):
            return {"n": 0}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("app.core.clients.mysql_connection", lambda: _Conn())
    report = gv.verify(session=session)
    assert not report.ok
    assert any("disputed" in problem for problem in report.problems)


# --------------------------------------------------------------------------- #
# Retiring what MySQL no longer projects
#
# The projector was MERGE-only apart from current-state edges, so a row that
# stopped being projectable was never visited again: not updated, not removed.
# Measured on the live graph — 2 entities demoted from pi_attested to
# provisional in MySQL kept nodes advertising "pi_attested, claim_eligible:
# true", together with the 17 claims naming them and 2 aliases, all stranded on
# a projection generation four hours older than the rest of the graph. Those
# identities were reachable as graph answers.
# --------------------------------------------------------------------------- #

def _sweeps(session):
    """The retirement statements a pass emitted, by the node they retire."""
    return {
        "Claim": writer.DELETE_STALE_CLAIMS in session.statements,
        "Entity": writer.DELETE_STALE_ENTITIES in session.statements,
        "Alias": writer.DELETE_STALE_ALIASES in session.statements,
        "Chunk": writer.DELETE_ORPHAN_CHUNKS in session.statements,
        "Document": writer.DELETE_ORPHAN_DOCUMENTS in session.statements,
    }


def test_a_whole_corpus_pass_retires_every_kind_of_stale_node(monkeypatch):
    session, _ = _project(
        monkeypatch, [_entity("org_a", "ORGANIZATION")], [_claim("claim_1", "org_a")]
    )
    assert all(_sweeps(session).values()), _sweeps(session)


def test_the_sweeps_are_scoped_by_this_generations_stamp(monkeypatch):
    """A sweep with the wrong version, or none, would delete the whole graph."""
    session, _ = _project(
        monkeypatch, [_entity("org_a", "ORGANIZATION")], [_claim("claim_1", "org_a")]
    )
    for statement in (writer.DELETE_STALE_CLAIMS, writer.DELETE_STALE_ENTITIES,
                      writer.DELETE_STALE_ALIASES):
        params = [p for s, p in session.calls if s == statement]
        assert params, statement
        assert all(p.get("projection_version") == "v-test" for p in params)


def test_claims_are_retired_before_entities(monkeypatch):
    """Deleting a claim detaches its SUBJECT/OBJECT first, so neither sweep has
    to cope with the other's leftovers."""
    session, _ = _project(
        monkeypatch, [_entity("org_a", "ORGANIZATION")], [_claim("claim_1", "org_a")]
    )
    order = [s for s, _ in session.calls]
    assert order.index(writer.DELETE_STALE_CLAIMS) < order.index(
        writer.DELETE_STALE_ENTITIES
    )
    # And evidence stubs come last, once nothing else can still be citing them.
    assert order.index(writer.DELETE_STALE_ENTITIES) < order.index(
        writer.DELETE_ORPHAN_CHUNKS
    )
    assert order.index(writer.DELETE_ORPHAN_CHUNKS) < order.index(
        writer.DELETE_ORPHAN_DOCUMENTS
    )


def test_every_node_that_should_survive_is_restamped_this_generation(monkeypatch):
    """The sweeps are safe only because a pass re-stamps everything it keeps.
    An unconditional SET is what makes a MATCH re-stamp like a CREATE."""
    for statement in (writer.MERGE_ENTITY, writer.MERGE_CLAIM, writer.MERGE_ALIAS):
        assert "projection_version = $projection_version" in statement, statement
        # Not ON CREATE only: that would leave surviving nodes on an old stamp
        # and the next sweep would delete the entire graph.
        assert "ON CREATE" not in statement, statement

    session, _ = _project(
        monkeypatch, [_entity("org_a", "ORGANIZATION")], [_claim("claim_1", "org_a")],
        aliases=[{"entity_id": "org_a", "normalized": "a", "surface": "A",
                  "alias_type": "full_name", "autolink": 1, "is_ambiguous": 0}],
    )
    for statement in (writer.MERGE_ENTITY, writer.MERGE_CLAIM, writer.MERGE_ALIAS):
        params = [p for s, p in session.calls if s == statement]
        assert params, statement
        assert all(p.get("projection_version") == "v-test" for p in params)


def test_a_scoped_pass_never_sweeps(monkeypatch):
    """For a pass that examined one document, "an older stamp" is the rest of
    the corpus. This is the same distinction DELETE_CURRENT_STATE_FOR_CLAIMS
    already drew for relationships, and getting it wrong empties the graph."""
    from app.catalog import assertions as store

    monkeypatch.setattr(store, "by_claim_ids", lambda ids: [_claim("claim_1", "org_a")])
    monkeypatch.setattr(store, "links_among", lambda ids: [])
    monkeypatch.setattr(
        gp, "_load_entities_by_ids", lambda ids: [_entity("org_a", "ORGANIZATION")]
    )
    monkeypatch.setattr(gp, "_load_aliases", lambda ids: [])
    monkeypatch.setattr(gp, "_load_documents", lambda ids: [])

    session = _FakeSession()
    gp.project_claims(["claim_1"], session=session, projection_version="v-scoped")
    assert not any(_sweeps(session).values()), _sweeps(session)
    # It still retires the current-state edges of the claims it was given.
    assert writer.DELETE_CURRENT_STATE_FOR_CLAIMS in session.statements


def test_the_retirement_counts_are_reported(monkeypatch):
    """An operator has to be able to see that a pass removed something."""
    monkeypatch.setattr(gp, "_load_entities", lambda: [_entity("org_a", "ORGANIZATION")])
    monkeypatch.setattr(gp, "_load_claims", lambda: [_claim("claim_1", "org_a")])
    monkeypatch.setattr(gp, "_load_links", lambda: [])
    monkeypatch.setattr(gp, "_load_aliases", lambda ids: [])
    monkeypatch.setattr(gp, "_load_documents", lambda ids: [])

    session = _FakeSession(responses={
        "MATCH (c:Claim)": _Result(deleted=17),
    })
    report = gp.project(session=session, projection_version="v-test")
    assert report.skipped.get("Claim_retired") == 17


def test_a_demoted_entitys_claims_are_refused_and_therefore_retired(monkeypatch):
    """The exact live scenario. `_load_entities` stops returning the demoted
    person, `_partition_projectable` refuses the claims naming them, and because
    nothing re-stamps those claims the sweep is what finally removes them."""
    session, report = _project(
        monkeypatch,
        # Only the still-eligible organization comes back from MySQL now.
        [_entity("org_a", "ORGANIZATION")],
        [
            _claim("claim_keep", "org_a"),
            # A claim whose object is the demoted person.
            _claim("claim_stale", "org_a", "person_demoted", predicate="LED_BY"),
        ],
    )
    projected = {r["claim_id"] for r in session.rows_for("MERGE (cl:Claim")}
    assert projected == {"claim_keep"}, "a claim on an ineligible entity is refused"
    assert report.skipped.get("claim_entity_not_eligible") == 1
    # And the sweep is emitted, which is what removes the node the earlier pass
    # left behind for claim_stale.
    assert writer.DELETE_STALE_CLAIMS in session.statements


def test_historical_claims_between_eligible_entities_are_kept(monkeypatch):
    """Retirement is synchronisation, not pruning. Nothing is dropped for being
    old: a 1993 relationship is re-stamped every pass and stays queryable."""
    session, report = _project(
        monkeypatch,
        [_entity("proj_a", "PROJECT"), _entity("person_a", "PERSON",
                                               trust="pi_attested")],
        [
            _claim("claim_1993", "proj_a", "person_a", predicate="LED_BY",
                   valid_from="1993-11-30", valid_until="1996-11-29"),
            _claim("claim_superseded", "proj_a", "person_a", predicate="LED_BY",
                   status="superseded", valid_from="1990-01-01",
                   valid_until="1993-11-29"),
        ],
    )
    projected = {r["claim_id"] for r in session.rows_for("MERGE (cl:Claim")}
    assert projected == {"claim_1993", "claim_superseded"}
    assert not report.skipped.get("claim_entity_not_eligible")
    # Both are stamped this generation, so the sweep cannot touch them.
    for row_set in session.calls:
        statement, params = row_set
        if statement == writer.MERGE_CLAIM:
            assert params["projection_version"] == "v-test"


def test_the_stale_sweeps_name_no_label_or_type_from_input():
    """Retirement must not become a new injection surface."""
    import re

    for statement in (writer.DELETE_STALE_CLAIMS, writer.DELETE_STALE_ENTITIES,
                      writer.DELETE_STALE_ALIASES, writer.DELETE_ORPHAN_CHUNKS,
                      writer.DELETE_ORPHAN_DOCUMENTS):
        assert "%s" not in statement, statement
        assert not re.search(r"=\s*['\"]", statement), statement
        assert "$rows" not in statement, statement


def test_the_node_sweeps_never_match_a_relationship_directly():
    """DETACH DELETE is deliberate — it removes the node's own edges — but the
    node sweeps must not match relationships, which is what
    DELETE_STALE_CURRENT_STATE is for."""
    for statement in (writer.DELETE_STALE_CLAIMS, writer.DELETE_STALE_ENTITIES,
                      writer.DELETE_STALE_ALIASES):
        assert "DETACH DELETE" in statement
        assert "]->()" not in statement, statement


def test_run_sweep_reports_the_driver_deletion_count():
    """The count comes from the driver's own counters, not from a row list — a
    sweep has no rows, which is the whole reason it can retire something."""
    session = _FakeSession(responses={"MATCH (e:Entity)": _Result(deleted=2)})
    assert writer.run_sweep(session, writer.DELETE_STALE_ENTITIES,
                            projection_version="v") == 2
    assert writer.run_sweep(session, writer.DELETE_ORPHAN_CHUNKS) == 0


# --------------------------------------------------------------------------- #
# verify(): comparing state, not just counting nodes
#
# `verify` reported the live drift, but only through its count comparison. Its
# targeted "no provisional identity may exist in the graph" check was blind to
# it, because it read the graph's *own* `claim_eligible` — which a demoted
# entity still carries as `true`, that being the stale value. The one check whose
# docstring promised the trust property could never observe a violation of it,
# and once retirement keeps the counts right a demotion changes no count at all.
# --------------------------------------------------------------------------- #

def _mysql_entity(entity_id, trust, status="active"):
    return {
        "entity_id": entity_id, "entity_type": "PERSON",
        "canonical_name": entity_id, "normalized_name": entity_id,
        "trust": trust, "cms_uuid": None, "source": "documents_author",
        "status": status,
    }


def _graph_entity(entity_id, trust, *, claim_eligible=True, status="active"):
    return {
        "entity_id": entity_id, "trust": trust,
        "claim_eligible": claim_eligible, "status": status,
        "projection_version": "v-old",
    }


def _verify(monkeypatch, *, mysql_entities, graph_entities):
    """Run `verify` with MySQL stubbed and the graph faked.

    Counts are made to agree deliberately: the point of these tests is the state
    comparison, and a count mismatch would mask it.
    """
    from app.knowledge.graph import verify as gv

    monkeypatch.setattr(
        "app.knowledge.graph.project._load_entities", lambda: list(mysql_entities)
    )
    monkeypatch.setattr("app.catalog.assertions.all_staged", lambda: [])

    class _Cur:
        def execute(self, *a, **kw):
            pass

        def fetchone(self):
            return {"n": 0}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("app.core.clients.mysql_connection", lambda: _Conn())

    session = _FakeSession(responses={
        # Distinctive needles. Three statements begin "MATCH (e:Entity)", and
        # INELIGIBLE_ENTITIES also returns `e.entity_id AS entity_id, e.trust`,
        # so the state needle has to be a projection only ENTITY_STATE makes.
        "count(e)": [{"n": len(mysql_entities)}],
        "e.claim_eligible AS claim_eligible": list(graph_entities),
    })
    return gv.verify(session=session)


def test_a_consistent_graph_verifies_clean(monkeypatch):
    report = _verify(
        monkeypatch,
        mysql_entities=[_mysql_entity("person_a", "pi_attested")],
        graph_entities=[_graph_entity("person_a", "pi_attested")],
    )
    assert report.ok, report.problems


def test_a_stale_trust_value_is_reported(monkeypatch):
    """The live defect, reduced: MySQL demoted the person, the graph still says
    pi_attested, and the counts agree."""
    report = _verify(
        monkeypatch,
        mysql_entities=[_mysql_entity("person_a", "provisional")],
        graph_entities=[_graph_entity("person_a", "pi_attested")],
    )
    assert not report.ok
    assert any(
        "trust" in p and "person_a" in p and "provisional" in p and "pi_attested" in p
        for p in report.problems
    ), report.problems


def test_an_entity_absent_from_the_authoritative_set_is_reported(monkeypatch):
    """A demoted entity leaves `_load_entities` altogether, so it is not merely
    mismatched — it must not be in the graph at all."""
    report = _verify(
        monkeypatch,
        mysql_entities=[],
        graph_entities=[_graph_entity("person_demoted", "pi_attested")],
    )
    assert not report.ok
    assert any(
        "person_demoted" in p and "not projectable" in p for p in report.problems
    ), report.problems


def test_a_stale_status_is_reported(monkeypatch):
    report = _verify(
        monkeypatch,
        mysql_entities=[_mysql_entity("person_a", "pi_attested", status="merged")],
        graph_entities=[_graph_entity("person_a", "pi_attested", status="active")],
    )
    assert not report.ok
    assert any("status" in p and "person_a" in p for p in report.problems), report.problems


def test_an_entity_projected_as_ineligible_is_reported(monkeypatch):
    """Belt and braces: the projector only ever writes `claim_eligible: true`,
    so a false one means something wrote the graph outside the projector."""
    report = _verify(
        monkeypatch,
        mysql_entities=[_mysql_entity("person_a", "pi_attested")],
        graph_entities=[
            _graph_entity("person_a", "pi_attested", claim_eligible=False)
        ],
    )
    assert not report.ok
    assert any("claim_eligible" in p for p in report.problems), report.problems


def test_the_state_check_reads_mysql_rather_than_the_graphs_own_copy():
    """The old bug in one line: a check that asks the graph whether the graph is
    right cannot fail. ENTITY_STATE must *select* the fields for comparison in
    Python rather than filter on them in Cypher."""
    from app.knowledge.graph import verify as gv

    assert "WHERE" not in gv.ENTITY_STATE.upper()
    for field in ("trust", "claim_eligible", "status"):
        assert f"e.{field}" in gv.ENTITY_STATE


# --------------------------------------------------------------------------- #
# The demotion / promotion lifecycle, end to end through the projector
#
# The audit's exact scenario: an entity is projected while eligible, MySQL later
# demotes it, and a second projection must leave the graph with no trace of the
# identity or the claims that named it — while every claim between still-eligible
# entities survives untouched.
# --------------------------------------------------------------------------- #

def _projected_ids(session, needle):
    return {r.get("entity_id") or r.get("claim_id")
            for r in session.rows_for(needle)}


def test_the_demotion_lifecycle(monkeypatch):
    person = _entity("person_p", "PERSON", trust="pi_attested")
    org = _entity("org_a", "ORGANIZATION")
    project_entity = _entity("proj_a", "PROJECT", trust="authoritative")
    led = _claim("claim_led", "proj_a", "person_p", predicate="LED_BY")
    funded = _claim("claim_funded", "proj_a", "org_a")

    # --- 1. eligible: both claims project, the person is in the graph -------
    before, _ = _project(
        monkeypatch, [person, org, project_entity], [led, funded]
    )
    assert _projected_ids(before, "MERGE (e:Entity") == {
        "person_p", "org_a", "proj_a"
    }
    assert _projected_ids(before, "MERGE (cl:Claim") == {
        "claim_led", "claim_funded"
    }

    # --- 2. MySQL demotes the person: _load_entities stops returning them ---
    after, report = _project(monkeypatch, [org, project_entity], [led, funded])

    assert "person_p" not in _projected_ids(after, "MERGE (e:Entity"), (
        "a demoted identity must not be re-projected"
    )
    # The claim naming them is refused by the authoritative projectability rule.
    assert _projected_ids(after, "MERGE (cl:Claim") == {"claim_funded"}
    assert report.skipped.get("claim_entity_not_eligible") == 1
    # And the sweeps run, which is what removes the node and claim the first
    # pass wrote. Without them the graph keeps advertising `pi_attested`.
    assert writer.DELETE_STALE_ENTITIES in after.statements
    assert writer.DELETE_STALE_CLAIMS in after.statements

    # --- 3. the surviving claim is untouched by any of it -------------------
    kept = [r for r in after.rows_for("MERGE (cl:Claim")
            if r["claim_id"] == "claim_funded"]
    assert kept and kept[0]["subject_id"] == "proj_a"


def test_the_promotion_lifecycle(monkeypatch):
    """The reverse must work on the same mechanism, with no special case: an
    entity that becomes eligible is simply in the next pass's row set."""
    org = _entity("org_a", "ORGANIZATION")
    project_entity = _entity("proj_a", "PROJECT", trust="authoritative")
    person = _entity("person_p", "PERSON", trust="pi_attested")
    led = _claim("claim_led", "proj_a", "person_p", predicate="LED_BY")

    before, report = _project(monkeypatch, [org, project_entity], [led])
    assert "person_p" not in _projected_ids(before, "MERGE (e:Entity")
    assert report.skipped.get("claim_entity_not_eligible") == 1

    after, report = _project(monkeypatch, [org, project_entity, person], [led])
    assert "person_p" in _projected_ids(after, "MERGE (e:Entity")
    assert _projected_ids(after, "MERGE (cl:Claim") == {"claim_led"}
    assert not report.skipped.get("claim_entity_not_eligible")


def test_a_changed_trust_value_is_rewritten_not_merely_left(monkeypatch):
    """Promotion within the eligible set changes no count, so MERGE has to carry
    the new value — which it does, because the SET is unconditional."""
    session, _ = _project(
        monkeypatch,
        [_entity("person_p", "PERSON", trust="pi_attested")],
        [],
    )
    rows = [r for r in session.rows_for("MERGE (e:Entity")
            if r["entity_id"] == "person_p"]
    assert rows and rows[0]["trust"] == "pi_attested"
    assert rows[0]["claim_eligible"] is True


def test_a_removed_alias_is_retired_even_though_its_entity_survives(monkeypatch):
    """The case the orphan rule alone could not reach: the entity is still
    perfectly valid, so the alias keeps its HAS_ALIAS and is not an orphan. Only
    a generation stamp distinguishes it."""
    entity = _entity("org_a", "ORGANIZATION")
    alias = {"entity_id": "org_a", "normalized": "a", "surface": "A",
             "alias_type": "full_name", "autolink": 1, "is_ambiguous": 0}

    session, _ = _project(monkeypatch, [entity], [], aliases=[alias])
    stamped = [p for s, p in session.calls if s == writer.MERGE_ALIAS]
    assert stamped and stamped[0]["projection_version"] == "v-test"
    assert writer.DELETE_STALE_ALIASES in session.statements
    # The sweep keys on the stamp, so an alias MySQL no longer returns is left
    # on an older one and removed, without the projector diffing anything.
    assert "$projection_version" in writer.DELETE_STALE_ALIASES


def test_reprojection_is_idempotent_including_the_sweeps(monkeypatch):
    """Two identical passes must emit identical work. A sweep that depended on
    what it deleted last time would not."""
    entities = [_entity("org_a", "ORGANIZATION"),
                _entity("proj_a", "PROJECT", trust="authoritative")]
    claims = [_claim("claim_1", "proj_a", "org_a")]
    aliases = [{"entity_id": "org_a", "normalized": "a", "surface": "A",
                "alias_type": "full_name", "autolink": 1, "is_ambiguous": 0}]

    first, r1 = _project(monkeypatch, entities, claims, aliases=aliases)
    second, r2 = _project(monkeypatch, entities, claims, aliases=aliases)

    assert first.statements == second.statements
    assert first.rows_for("MERGE (e:Entity") == second.rows_for("MERGE (e:Entity")
    assert first.rows_for("MERGE (cl:Claim") == second.rows_for("MERGE (cl:Claim")
    assert first.rows_for("MERGE (a:Alias") == second.rows_for("MERGE (a:Alias")
    assert r1.nodes == r2.nodes
    assert r1.relationships == r2.relationships
    # Every node write is a MERGE on a deterministic key, so no pass can
    # duplicate one.
    for needle, key in (("MERGE (e:Entity", "entity_id"),
                        ("MERGE (cl:Claim", "claim_id"),
                        ("MERGE (a:Alias", "alias_key")):
        rows = second.rows_for(needle)
        assert len(rows) == len({r[key] for r in rows}), needle
