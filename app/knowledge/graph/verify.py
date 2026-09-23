"""Verify the graph against MySQL, and rebuild it when they disagree.

The projection is derived, so MySQL is always right and the graph is always the
thing that can be wrong. This diffs the two and reports; ``rebuild`` is the fix,
and it is always available because nothing in the graph is a system of record.

What is checked
---------------
* every projectable entity is present, and nothing else is. "Projectable" is
  broad eligibility *or* a predicate-scoped trust level, matching
  ``project._projectable_clause`` — an ``author_attested`` person is projected
  while remaining ineligible for anything but its one predicate;
* **every graph entity's trust, eligibility and status match the MySQL row it
  was derived from** — the check that a count comparison cannot make, and the
  one that catches a demotion the projector failed to retire. Eligibility is
  *compared*, never assumed true, because projection and eligibility are no
  longer the same question;
* every projectable claim is present, in the correct one of the graph's two
  claim shapes: an entity-valued predicate has an ``OBJECT`` edge, a
  literal-valued one (``AUTHORED``, ``HAS_ROLE``) keeps its object as a property
  and has none. There is no ``Literal`` node;
* current-state edges exist only for claims that are still eligible for one —
  in particular **no disputed claim has a current edge**, which is the safety
  property the whole conflict layer exists to produce;
* every current-state edge carries a ``claim_id`` that resolves to a real claim.

The presence checks would mostly be caught by a recount. The three that earn
their place are the state comparison, a disputed claim quietly keeping an edge,
and an edge citing a claim that no longer exists — none of which changes a count.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class VerificationReport:
    """Differences between MySQL and the graph."""

    expected: dict[str, int] = field(default_factory=dict)
    actual: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "expected": dict(sorted(self.expected.items())),
            "actual": dict(sorted(self.actual.items())),
            "problems": list(self.problems),
        }


COUNT_ENTITIES = "MATCH (e:Entity) RETURN count(e) AS n"
COUNT_CLAIMS = "MATCH (c:Claim) RETURN count(c) AS n"
COUNT_ALIASES = "MATCH (a:Alias) RETURN count(a) AS n"
COUNT_CURRENT = "MATCH ()-[r {current: true}]->() RETURN count(r) AS n"

# A disputed or superseded claim must never back a current-state edge.
DISPUTED_WITH_EDGE = """
MATCH (c:Claim)
WHERE c.status <> 'active'
MATCH ()-[r {current: true, claim_id: c.claim_id}]->()
RETURN c.claim_id AS claim_id, c.status AS status LIMIT 25
"""

# Every current-state edge must trace back to a claim that exists.
ORPHAN_EDGES = """
MATCH ()-[r {current: true}]->()
WHERE NOT EXISTS { MATCH (c:Claim {claim_id: r.claim_id}) }
RETURN r.claim_id AS claim_id LIMIT 25
"""

# No provisional identity may exist in the graph at all.
#
# "Ineligible" is not the same as "not projectable". A predicate-scoped trust
# level — `author_attested` — is deliberately NOT `claim_eligible`, because that
# flag means "eligible for anything", yet its entities are projected so their
# scoped claims have a subject. Those are excluded by `$scoped` rather than
# reported: see `app.knowledge.graph.project._projectable_clause`.
#
# Necessary but not sufficient, and the insufficiency mattered: this reads the
# graph's *own* copy of `claim_eligible`, so it can only catch an entity the
# projector wrote as ineligible. A demoted entity keeps a stale
# `claim_eligible: true` and sails past. `ENTITY_STATE` below is the check that
# compares the two stores.
INELIGIBLE_ENTITIES = """
MATCH (e:Entity)
WHERE e.claim_eligible <> true AND NOT e.trust IN $scoped
RETURN e.entity_id AS entity_id, e.trust AS trust LIMIT 25
"""

# An entity-valued predicate must have an OBJECT edge; a literal-valued one must
# not. The graph has two legitimate claim shapes and conflating them reports
# every AUTHORED and HAS_ROLE claim as broken:
#
#   entity-valued   Claim-[:OBJECT]->(Entity)
#   literal-valued  the object is a property on the Claim, and there is no
#                   Literal node by design
#
# Both lists come from the vocabulary's own `entity_valued`, so adding a
# predicate cannot leave this check behind.
CLAIMS_MISSING_OBJECT = """
MATCH (c:Claim)
WHERE NOT c.predicate IN $literal_valued AND NOT (c)-[:OBJECT]->()
RETURN c.claim_id AS claim_id, c.predicate AS predicate LIMIT 25
"""

LITERAL_CLAIMS_WITH_OBJECT = """
MATCH (c:Claim)-[:OBJECT]->()
WHERE c.predicate IN $literal_valued
RETURN c.claim_id AS claim_id, c.predicate AS predicate LIMIT 25
"""

# Every graph entity's trust and eligibility, to compare against MySQL. The
# comparison is done in Python rather than by shipping the authoritative set
# into Cypher: MySQL is the one that knows, and asking the graph to judge itself
# is exactly the mistake above.
ENTITY_STATE = """
MATCH (e:Entity)
RETURN e.entity_id AS entity_id, e.trust AS trust,
       e.claim_eligible AS claim_eligible, e.status AS status,
       e.projection_version AS projection_version
"""


# When the graph was last projected. Every projected node carries the stamp of
# the generation that wrote it (see project.make_projection_version), whose
# timestamp component sorts lexically — so the newest stamp is the maximum, and
# no extra bookkeeping node is needed to answer "how old is this projection?".
LATEST_PROJECTION = """
MATCH (e:Entity)
WHERE e.projection_version IS NOT NULL
RETURN max(e.projection_version) AS version
"""


@dataclass
class ProjectionFreshness:
    """How old the projection is, as far as the graph itself can say."""

    version: str | None = None
    projected_at: Any = None
    age_seconds: float | None = None

    @property
    def known(self) -> bool:
        return self.projected_at is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "projected_at": self.projected_at.isoformat() if self.known else None,
            "age_seconds": round(self.age_seconds) if self.age_seconds else None,
        }


def projection_freshness(*, session: Any = None) -> ProjectionFreshness:
    """Read the newest projection stamp in the graph. Never writes.

    An empty graph, or one whose nodes predate the stamp, reports an unknown
    age rather than an age of zero — "never projected" and "just projected" must
    not look the same.
    """
    from datetime import datetime, timezone

    from app.core.clients.graph import read_session

    def _read(open_session: Any) -> str | None:
        record = open_session.run(LATEST_PROJECTION).single()
        return record["version"] if record else None

    if session is not None:
        version = _read(session)
    else:
        with read_session() as open_session:
            version = _read(open_session)

    if not version:
        return ProjectionFreshness()
    # "graph-project-v1:20260814T065651:9f2c1a08"
    parts = version.split(":")
    if len(parts) < 2:
        return ProjectionFreshness(version=version)
    try:
        moment = datetime.strptime(parts[1], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return ProjectionFreshness(version=version)
    return ProjectionFreshness(
        version=version,
        projected_at=moment,
        age_seconds=(datetime.now(timezone.utc) - moment).total_seconds(),
    )


def literal_valued_predicates() -> list[str]:
    """Predicate names whose object is a literal, so they carry no OBJECT edge.

    Read from the vocabulary rather than listed, so a predicate added as
    literal-valued is exempt here without a second edit — and one added as
    entity-valued is required to have an edge without one either.
    """
    from app.knowledge.claims import predicates as vocab

    return sorted(
        name for name, predicate in vocab.PREDICATES.items()
        if not predicate.entity_valued
    )


def scoped_trust_levels() -> list[str]:
    """Trust levels that are projected without being broadly claim-eligible."""
    from app.knowledge.seed import PREDICATE_SCOPED_TRUST

    return sorted(PREDICATE_SCOPED_TRUST)


def verify(*, session: Any = None, as_of: str | None = None) -> VerificationReport:
    """Diff MySQL against the graph. Never writes."""
    from app.catalog import assertions as store
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection
    from app.core.clients.graph import read_session
    from app.knowledge.graph.project import (
        _current_state_rows, _load_entities, _projectable_clause,
    )

    report = VerificationReport()

    entities = _load_entities()
    eligible_ids = {e["entity_id"] for e in entities}
    claims = store.all_staged()
    projectable = [
        c
        for c in claims
        if c["subject_entity_id"] in eligible_ids
        and (not c["object_entity_id"] or c["object_entity_id"] in eligible_ids)
    ]
    current = _current_state_rows(projectable, as_of=as_of)

    table = state_table()
    # The alias expectation has to use the *same* projectability rule the
    # projector does. Filtering on `claim_eligible=1` alone under-counted by
    # every alias belonging to a predicate-scoped entity, which read as a graph
    # that had invented 258 aliases.
    projectable_clause, projectable_params = _projectable_clause()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS n FROM `{table}_entity_alias` a "
            f"JOIN `{table}_entity` e ON e.entity_id = a.entity_id "
            f"WHERE e.status='active' AND {projectable_clause}",
            projectable_params,
        )
        alias_count = int(cur.fetchone()["n"])

    report.expected = {
        "Entity": len(entities),
        "Claim": len(projectable),
        "Alias": alias_count,
        "current_state_edges": len(current),
    }

    def _check(open_session: Any) -> None:
        report.actual = {
            "Entity": open_session.run(COUNT_ENTITIES).single()["n"],
            "Claim": open_session.run(COUNT_CLAIMS).single()["n"],
            "Alias": open_session.run(COUNT_ALIASES).single()["n"],
            "current_state_edges": open_session.run(COUNT_CURRENT).single()["n"],
        }
        for key, expected in report.expected.items():
            actual = report.actual.get(key, 0)
            if actual != expected:
                report.problems.append(
                    f"{key}: expected {expected}, graph has {actual}"
                )
        for row in open_session.run(DISPUTED_WITH_EDGE):
            report.problems.append(
                f"non-active claim {row['claim_id']} ({row['status']}) "
                "backs a current-state edge"
            )
        for row in open_session.run(ORPHAN_EDGES):
            report.problems.append(
                f"current-state edge cites missing claim {row['claim_id']}"
            )
        for row in open_session.run(INELIGIBLE_ENTITIES, scoped=scoped_trust_levels()):
            report.problems.append(
                f"ineligible entity {row['entity_id']} ({row['trust']}) is in the graph"
            )
        # The two legitimate claim shapes, each checked against the other's rule.
        literal_valued = literal_valued_predicates()
        for row in open_session.run(
            CLAIMS_MISSING_OBJECT, literal_valued=literal_valued
        ):
            report.problems.append(
                f"entity-valued claim {row['claim_id']} ({row['predicate']}) "
                "has no OBJECT edge"
            )
        for row in open_session.run(
            LITERAL_CLAIMS_WITH_OBJECT, literal_valued=literal_valued
        ):
            report.problems.append(
                f"literal-valued claim {row['claim_id']} ({row['predicate']}) "
                "has an OBJECT edge, but its object is a property"
            )
        # The authoritative comparison: every graph entity against the MySQL row
        # it was derived from. A count match is not enough — two entities could
        # drop out while two others appear — and a demotion changes no count at
        # all once the projector retires properly, so this is what proves the
        # trust model in the graph is the trust model in the catalog.
        by_id = {e["entity_id"]: e for e in entities}
        for row in open_session.run(ENTITY_STATE):
            entity_id = row["entity_id"]
            authoritative = by_id.get(entity_id)
            if authoritative is None:
                report.problems.append(
                    f"entity {entity_id} is in the graph but is not projectable "
                    "in MySQL (demoted, retracted, merged or deleted)"
                )
                continue
            if row["trust"] != authoritative["trust"]:
                report.problems.append(
                    f"entity {entity_id} trust: MySQL says "
                    f"{authoritative['trust']!r}, graph says {row['trust']!r}"
                )
            # Compared against MySQL, not asserted to be true. Projection is no
            # longer the same thing as broad eligibility: a predicate-scoped
            # entity is projected carrying `claim_eligible: false`, and that is
            # the value the graph is *supposed* to advertise. Demanding true
            # here reported all 258 of them as corrupt.
            expected_eligible = bool(authoritative.get("claim_eligible", 1))
            if bool(row["claim_eligible"]) != expected_eligible:
                report.problems.append(
                    f"entity {entity_id} claim_eligible: MySQL says "
                    f"{expected_eligible!r}, graph says "
                    f"{row['claim_eligible']!r}"
                )
            if row["status"] != authoritative["status"]:
                report.problems.append(
                    f"entity {entity_id} status: MySQL says "
                    f"{authoritative['status']!r}, graph says {row['status']!r}"
                )

    if session is not None:
        _check(session)
    else:
        with read_session() as opened:
            _check(opened)
    return report


def rebuild(*, as_of: str | None = None) -> dict[str, Any]:
    """Drop the graph and project it again from MySQL.

    The always-available fix. Safe precisely because the graph is derived: a
    rebuild loses nothing, which is what made adopting a second database
    acceptable in the first place.
    """
    from app.core.clients.graph import write_session
    from app.knowledge.graph import writer
    from app.knowledge.graph.project import project
    from app.knowledge.graph.schema import ensure_graph_schema

    with write_session() as session:
        session.run(writer.DROP_ALL)
    ensure_graph_schema()
    report = project(as_of=as_of)
    logger.info("Rebuilt the graph: %s", report.as_dict())
    return report.as_dict()
