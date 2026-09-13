"""Project MySQL entities and staged claims into Neo4j.

Neo4j is a **rebuildable projection**, never a system of record. Everything here
is derived from MySQL, so a corrupt graph is fixed by rebuilding rather than by
forensics, and an unreachable graph costs a retry rather than data.

What is projected, and what is refused
--------------------------------------
* **Entities**: ``claim_eligible`` ones, plus those holding a predicate-scoped
  trust level (``author_attested``). The provisional people still do not reach
  the graph at all, so a traversal cannot arrive at a name-level identity and
  mistake it for a person. Trust and the real ``claim_eligible`` value are both
  carried onto the node, so ``pi_attested`` stays distinguishable from
  ``authoritative`` — and an ``author_attested`` node is visibly not eligible
  for anything beyond the one predicate its level grants. Without this a scoped
  claim would exist in MySQL and be unprojectable, since a claim whose subject
  has no node is refused by ``_partition_projectable``.
* **Claims**: every staged claim, whatever its status, because history is the
  point — a superseded claim is still the answer to "who led this in 2019".
* **Current-state edges**: only from claims that are ``active``, non-disputed,
  currently valid and eligible on both ends. This is the one place where the
  graph asserts "this is true now", and it is the narrowest thing here.

Every projected current-state edge carries ``claim_id`` back to the claim that
produced it, so the chain

    edge -> claim_id -> claim -> chunk_id/document_id -> Qdrant -> source text

is walkable for anything the graph asserts.

Idempotency, retirement and rebuild
-----------------------------------
Every write is ``MERGE`` on a deterministic key, so re-projecting is an update.
``projection_version`` stamps each generation, and a whole-corpus pass finishes
by deleting everything it did **not** re-stamp: current-state edges, and also
the Entity, Claim and Alias nodes behind them. That is how a claim which stopped
being current loses its edge, and how an entity which stopped being
``claim_eligible`` loses its node, without anything having to remember either
existed.

MERGE alone was not enough, and the gap was real. Because every statement here
writes only the rows MySQL currently says are projectable, a row that *stops*
being projectable was never visited again — so an entity demoted from
``pi_attested`` to ``provisional`` kept a node still advertising
``pi_attested, claim_eligible: true``, and the claims naming it kept theirs.
Retirement closes that, and it is deliberately a *sweep by generation* rather
than a diff: the projector never has to compute what disappeared, only what
should be here now.

Retirement is synchronisation, never pruning. Nothing is dropped because it is
old: MySQL keeps every claim it ever staged, and what leaves the graph is only
what the projectability rule above already refused. A 1993 relationship between
two still-eligible entities is re-stamped every pass and stays queryable.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from app.knowledge.claims import conflicts as cf
from app.knowledge.claims import predicates as vocab
from app.knowledge.claims import types as claim_types
from app.knowledge.graph import schema, writer

logger = logging.getLogger(__name__)

PROJECTOR_VERSION = "graph-project-v1"

# Entity type -> the typed label it also carries alongside :Entity.
_TYPE_LABELS = {
    "PERSON": "Person",
    "ORGANIZATION": "Organization",
    "PROJECT": "Project",
}


def make_projection_version(*, at: datetime | None = None) -> str:
    """A stamp identifying one generation of projected current-state edges."""
    moment = (at or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S")
    digest = hashlib.sha256(
        f"{PROJECTOR_VERSION}\x1f{moment}".encode("utf-8")
    ).hexdigest()[:8]
    return f"{PROJECTOR_VERSION}:{moment}:{digest}"


@dataclass
class ProjectionReport:
    """What one projection pass wrote."""

    projection_version: str
    nodes: dict[str, int] = field(default_factory=dict)
    relationships: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)

    def note(self, bucket: dict[str, int], key: str, count: int) -> None:
        if count:
            bucket[key] = bucket.get(key, 0) + count

    def as_dict(self) -> dict[str, Any]:
        return {
            "projection_version": self.projection_version,
            "nodes": dict(sorted(self.nodes.items())),
            "relationships": dict(sorted(self.relationships.items())),
            "skipped": dict(sorted(self.skipped.items())),
        }


# --------------------------------------------------------------------------- #
# Reading MySQL
# --------------------------------------------------------------------------- #

def _load_entities() -> list[dict[str, Any]]:
    """Claim-eligible, active entities only.

    The filter is the point: a provisional identity must not exist in the graph
    at all, so no traversal can reach one.
    """
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    table = state_table()
    clause, params = _projectable_clause()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT entity_id, entity_type, canonical_name, normalized_name, "
            f"trust, claim_eligible, cms_uuid, source, status FROM `{table}_entity` "
            f"WHERE status = 'active' AND {clause}",
            params,
        )
        return list(cur.fetchall())


def _projectable_clause() -> tuple[str, list[str]]:
    """SQL for "this entity may hold a node", and its parameters.

    Broad eligibility is not the only reason to exist in the graph. A
    predicate-scoped trust level — ``author_attested`` — is deliberately *not*
    ``claim_eligible``, because that flag means "eligible for anything". But its
    claims are real, and a claim whose subject has no node cannot be projected
    at all, so the authorship edge would exist in MySQL and nowhere else.

    Widening here does not widen eligibility. The node carries its true
    ``claim_eligible`` value, so a consumer can still tell the two apart, and
    every other gate is untouched: validation still refuses these people as the
    *object* of a claim, and :func:`_current_state_rows` still requires an
    entity-valued object, which ``AUTHORED`` does not have.

    Read from :data:`app.knowledge.seed.PREDICATE_SCOPED_TRUST` rather than
    listed here, so a new scoped level cannot be added without its entities
    becoming projectable.
    """
    from app.knowledge.seed import PREDICATE_SCOPED_TRUST

    levels = sorted(PREDICATE_SCOPED_TRUST)
    if not levels:
        return "claim_eligible = 1", []
    placeholders = ", ".join(["%s"] * len(levels))
    return f"(claim_eligible = 1 OR trust IN ({placeholders}))", levels


def _load_entities_by_ids(entity_ids: set[str]) -> list[dict[str, Any]]:
    """Claim-eligible, active entities among the given ids.

    The scoped counterpart of :func:`_load_entities`, and it keeps the same
    filter: a provisional identity must not reach the graph, whichever pass is
    asking. Naming an id here does not exempt it.
    """
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    if not entity_ids:
        return []
    table = state_table()
    ids = sorted(entity_ids)
    clause, params = _projectable_clause()
    out: list[dict[str, Any]] = []
    with mysql_connection() as conn, conn.cursor() as cur:
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            placeholders = ", ".join(["%s"] * len(batch))
            cur.execute(
                f"SELECT entity_id, entity_type, canonical_name, normalized_name, "
                f"trust, claim_eligible, cms_uuid, source, status FROM `{table}_entity` "
                f"WHERE status = 'active' AND {clause} "
                f"AND entity_id IN ({placeholders})",
                [*params, *batch],
            )
            out.extend(cur.fetchall())
    return out


def _load_aliases(entity_ids: set[str]) -> list[dict[str, Any]]:
    """Aliases of the given entities.

    Filtered in SQL rather than in Python. The corpus pass reads a few thousand
    ids either way, but the scoped pass reads a handful, and loading every alias
    in the store — most of them belonging to the provisional identities that are
    never projected — to discard them per document is the wrong shape.
    """
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    if not entity_ids:
        return []
    table = state_table()
    ids = sorted(entity_ids)
    out: list[dict[str, Any]] = []
    with mysql_connection() as conn, conn.cursor() as cur:
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            placeholders = ", ".join(["%s"] * len(batch))
            cur.execute(
                f"SELECT entity_id, normalized, surface, alias_type, autolink, "
                f"is_ambiguous FROM `{table}_entity_alias` "
                f"WHERE entity_id IN ({placeholders})",
                batch,
            )
            out.extend(cur.fetchall())
    return out


def _load_claims() -> list[dict[str, Any]]:
    from app.catalog import assertions as store

    return store.all_staged()


def _load_links() -> list[dict[str, Any]]:
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT from_claim_id, to_claim_id, kind, reason "
            f"FROM `{table}_assertion_link`"
        )
        return list(cur.fetchall())


def _load_documents(document_ids: set[str]) -> list[dict[str, Any]]:
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    if not document_ids:
        return []
    table = state_table()
    ids = sorted(document_ids)
    out: list[dict[str, Any]] = []
    with mysql_connection() as conn, conn.cursor() as cur:
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            placeholders = ", ".join(["%s"] * len(batch))
            cur.execute(
                f"SELECT document_id, title, source_type, bundle, effective_start_date, url "
                f"FROM `{table}` WHERE document_id IN ({placeholders})",
                batch,
            )
            out.extend(cur.fetchall())
    return out


def _iso(value: Any) -> str | None:
    from app.knowledge.claims.temporal import as_iso

    return as_iso(value)


# --------------------------------------------------------------------------- #
# Projection
# --------------------------------------------------------------------------- #

def project(
    *, session: Any = None, projection_version: str | None = None,
    as_of: str | None = None,
) -> ProjectionReport:
    """Project entities, claims, evidence and current state. Idempotent.

    The whole-corpus pass, and the repair path for everything else: because it
    examines every staged claim, it can finish by deleting every current-state
    edge it did not re-stamp, which is what makes it able to correct drift left
    by a scoped run (:func:`project_claims`) or by a run that never finished.
    """
    version = projection_version or make_projection_version()
    report = ProjectionReport(projection_version=version)

    entities = _load_entities()
    eligible_ids = {e["entity_id"] for e in entities}
    aliases = _load_aliases(eligible_ids)
    claims = _load_claims()
    links = _load_links()

    projectable, refused = _partition_projectable(claims, eligible_ids)
    report.note(report.skipped, "claim_entity_not_eligible", refused)

    document_ids = {c["document_id"] for c in projectable if c["document_id"]}
    chunk_ids = {c["chunk_id"] for c in projectable if c["chunk_id"]}
    documents = _load_documents(document_ids)

    def retire(open_session: Any, _current: list[dict[str, Any]]) -> None:
        """Anything from an older generation is no longer in the graph.

        A whole-corpus pass has just re-stamped every node MySQL says belongs
        here, so an older stamp is proof that the row behind it is gone from the
        authoritative set — demoted, retracted, merged away, or made ineligible.
        Retiring by stamp is what turns this pass into a true reconciliation
        rather than an accumulating upsert.

        This is *synchronisation, not destruction*. MySQL keeps every claim it
        ever staged; what goes here is only what `_partition_projectable` — the
        authoritative projectability rule, unchanged — already declined to
        project. A 1993 claim between two still-eligible entities is re-stamped
        and stays, which is the property the historical query path depends on.

        Only the whole-corpus pass may do this; `project_claims` re-stamps one
        document's rows, so for it "an older stamp" would mean the rest of the
        corpus. See `writer.DELETE_STALE_CLAIMS`.
        """
        open_session.run(
            writer.DELETE_STALE_CURRENT_STATE, projection_version=version
        )
        for label, statement in (
            ("Claim", writer.DELETE_STALE_CLAIMS),
            ("Entity", writer.DELETE_STALE_ENTITIES),
            ("Alias", writer.DELETE_STALE_ALIASES),
        ):
            removed = writer.run_sweep(
                open_session, statement, projection_version=version
            )
            report.note(report.skipped, f"{label}_retired", removed)
        # Evidence stubs exist only to back a claim, so a stub the sweeps above
        # just disconnected is spent. Chunks first: a document may be reachable
        # only through one.
        for label, statement in (
            ("Chunk", writer.DELETE_ORPHAN_CHUNKS),
            ("Document", writer.DELETE_ORPHAN_DOCUMENTS),
        ):
            removed = writer.run_sweep(open_session, statement)
            report.note(report.skipped, f"{label}_retired", removed)

    _in_session(
        session,
        lambda opened: _write_projection(
            opened, entities=entities, aliases=aliases, projectable=projectable,
            documents=documents, chunk_ids=chunk_ids, links=links,
            report=report, version=version, as_of=as_of, retire=retire,
        ),
    )
    logger.info("Projection %s complete: %s", version, report.as_dict())
    return report


def _write_projection(
    open_session: Any,
    *,
    entities: list[dict[str, Any]],
    aliases: list[dict[str, Any]],
    projectable: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    chunk_ids: set[str],
    links: list[dict[str, Any]],
    report: ProjectionReport,
    version: str,
    as_of: str | None,
    retire: Any,
) -> None:
    """Write one projection into an open session.

    Shared verbatim by the whole-corpus :func:`project` and the scoped
    :func:`project_claims`, so the two cannot write different graphs from the
    same rows. Everything that differs between them is decided *before* this
    runs — which entities and claims were loaded — except the retirement of
    current-state edges, which is the one step whose correct scope depends on
    what the caller examined. That arrives as ``retire``.
    """
    # --- entities ---------------------------------------------------- #
    rows = [
        {
            "entity_id": e["entity_id"],
            "canonical_name": e["canonical_name"],
            "normalized_name": e["normalized_name"],
            "entity_type": e["entity_type"],
            "trust": e["trust"],
            # The real value, not an assumption. It used to be safe to hardcode
            # because only eligible entities were loaded; a predicate-scoped
            # level is projected without being eligible, and a node claiming
            # otherwise would misrepresent it to every consumer.
            "claim_eligible": bool(e.get("claim_eligible", 1)),
            "cms_uuid": e["cms_uuid"],
            "source": e["source"],
            "status": e["status"],
        }
        for e in entities
    ]
    writer.run_batches(
        open_session, writer.MERGE_ENTITY, rows, projection_version=version
    )
    report.note(report.nodes, "Entity", len(rows))
    for entity_type, label in _TYPE_LABELS.items():
        typed = [r for r in rows if r["entity_type"] == entity_type]
        if not typed:
            continue
        statement = writer.ADD_TYPE_LABEL % writer.safe_label(label)
        writer.run_batches(open_session, statement, typed)
        report.note(report.nodes, label, len(typed))

    # --- aliases ------------------------------------------------------ #
    alias_rows = [
        {
            "entity_id": a["entity_id"],
            "alias_key": schema.alias_key(
                a["entity_id"], a["normalized"], a["alias_type"]
            ),
            "normalized": a["normalized"],
            "surface": a["surface"],
            "alias_type": a["alias_type"],
            "autolink": bool(a["autolink"]),
            "is_ambiguous": bool(a["is_ambiguous"]),
        }
        for a in aliases
    ]
    writer.run_batches(
        open_session, writer.MERGE_ALIAS, alias_rows,
        projection_version=version,
    )
    report.note(report.nodes, "Alias", len(alias_rows))
    report.note(report.relationships, "HAS_ALIAS", len(alias_rows))

    # --- predicate vocabulary ----------------------------------------- #
    predicate_rows = [
        {
            "name": p.name, "description": p.description,
            "domain": list(p.domain), "range": list(p.range),
            "functional": p.functional, "object_kind": p.object_kind,
            "vocabulary_version": vocab.VOCABULARY_VERSION,
        }
        for p in vocab.PREDICATES.values()
    ]
    writer.run_batches(open_session, writer.MERGE_PREDICATE, predicate_rows)
    report.note(report.nodes, "Predicate", len(predicate_rows))

    # --- provenance stubs --------------------------------------------- #
    document_rows = [
        {
            "document_id": d["document_id"], "title": d["title"],
            "source_type": d["source_type"], "bundle": d["bundle"],
            "effective_start_date": _iso(d["effective_start_date"]), "url": d["url"],
        }
        for d in documents
    ]
    writer.run_batches(open_session, writer.MERGE_DOCUMENT, document_rows)
    report.note(report.nodes, "Document", len(document_rows))

    chunk_rows = [
        {"chunk_id": c["chunk_id"], "document_id": c["document_id"]}
        for c in projectable
        if c["chunk_id"]
    ]
    # Only chunks that actually carry a claim get a stub. A stub per corpus
    # chunk would put ~149k nodes in the graph for no traversal benefit.
    writer.run_batches(open_session, writer.MERGE_CHUNK, chunk_rows)
    report.note(report.nodes, "Chunk", len(chunk_ids))
    report.note(report.relationships, "PART_OF", len(chunk_rows))

    # --- claims -------------------------------------------------------- #
    claim_rows = [
        {
            "claim_id": c["claim_id"], "predicate": c["predicate"],
            "subject_id": c["subject_entity_id"],
            "object_id": c["object_entity_id"],
            "object_literal": c["object_literal"],
            "valid_from": _iso(c["valid_from"]),
            "valid_until": _iso(c["valid_until"]),
            "temporal_basis": c["temporal_basis"],
            "confidence": float(c["confidence"] or 0.0),
            "status": c["status"], "evidence_kind": c["evidence_kind"],
            "source_field": c["source_field"], "quote": c["quote"],
            "quote_start": c["quote_start"], "quote_end": c["quote_end"],
            "document_id": c["document_id"], "chunk_id": c["chunk_id"],
            "extraction_method": c["extraction_method"],
            "extractor_version": c["extractor_version"],
        }
        for c in projectable
    ]
    writer.run_batches(
        open_session, writer.MERGE_CLAIM, claim_rows,
        projection_version=version,
    )
    report.note(report.nodes, "Claim", len(claim_rows))

    writer.run_batches(open_session, writer.LINK_CLAIM_SUBJECT, claim_rows)
    report.note(report.relationships, "SUBJECT", len(claim_rows))
    with_object = [r for r in claim_rows if r["object_id"]]
    writer.run_batches(open_session, writer.LINK_CLAIM_OBJECT, with_object)
    report.note(report.relationships, "OBJECT", len(with_object))
    writer.run_batches(open_session, writer.LINK_CLAIM_PREDICATE, claim_rows)
    report.note(report.relationships, "USES_PREDICATE", len(claim_rows))

    with_chunk = [r for r in claim_rows if r["chunk_id"]]
    writer.run_batches(open_session, writer.LINK_CLAIM_CHUNK, with_chunk)
    without_chunk = [
        r for r in claim_rows if not r["chunk_id"] and r["document_id"]
    ]
    writer.run_batches(open_session, writer.LINK_CLAIM_DOCUMENT, without_chunk)
    report.note(
        report.relationships, "SUPPORTED_BY", len(with_chunk) + len(without_chunk)
    )

    # --- contradiction and supersession -------------------------------- #
    projected_ids = {r["claim_id"] for r in claim_rows}
    for kind, rel in (
        (cf.LINK_CONTRADICTS, "CONTRADICTS"),
        (cf.LINK_SUPERSEDES, "SUPERSEDES"),
    ):
        rows_for_kind = [
            {
                "from_claim_id": l["from_claim_id"],
                "to_claim_id": l["to_claim_id"],
                "reason": l["reason"],
            }
            for l in links
            if l["kind"] == kind
            and l["from_claim_id"] in projected_ids
            and l["to_claim_id"] in projected_ids
        ]
        if not rows_for_kind:
            continue
        statement = writer.LINK_CLAIM_CLAIM % writer.safe_relationship(rel)
        writer.run_batches(open_session, statement, rows_for_kind)
        report.note(report.relationships, rel, len(rows_for_kind))

    # --- derived current state ----------------------------------------- #
    current = _current_state_rows(projectable, as_of=as_of)
    by_predicate: dict[str, list[dict]] = {}
    for row in current:
        by_predicate.setdefault(row["predicate"], []).append(row)
    for predicate_name, rows_for_predicate in sorted(by_predicate.items()):
        statement = (
            writer.PROJECT_CURRENT_STATE
            % writer.safe_relationship(predicate_name)
        )
        writer.run_batches(
            open_session, statement, rows_for_predicate,
            projection_version=version,
        )
        report.note(
            report.relationships, f"{predicate_name} (current)",
            len(rows_for_predicate),
        )
    # Retire the current-state edges this pass no longer justifies. Which
    # edges those are is the one thing a scoped pass cannot answer the same way
    # as a whole-corpus one, so the caller supplies the cleanup.
    retire(open_session, current)


def _in_session(session: Any, run: Any) -> None:
    """Run ``run`` against the given session, or against a fresh write one."""
    from app.core.clients.graph import write_session

    if session is not None:
        run(session)
        return
    with write_session() as opened:
        run(opened)


def project_claims(
    claim_ids: Sequence[str], *, session: Any = None,
    projection_version: str | None = None, as_of: str | None = None,
) -> ProjectionReport:
    """Project a named set of claims and the entities they touch. Idempotent.

    The per-document counterpart of :func:`project`, and the difference that
    matters is what it does *not* do. :func:`project` finishes with
    ``DELETE_STALE_CURRENT_STATE``, which removes every current-state edge the
    run did not re-stamp — correct after examining the whole staged set, and
    catastrophic after examining one document's claims, because the rest of the
    corpus's edges are exactly the ones it did not re-stamp.

    So this pass retires by name instead: of the claims it was given, those that
    are no longer current-state eligible lose their edge, and every relationship
    belonging to a claim outside the set is left alone.

    Everything else is the same code — the same loaders, the same eligibility
    filter, the same MERGE statements, the same ``safe_label`` and
    ``safe_relationship`` allow-lists. A scoped pass cannot create a label or a
    relationship type the whole-corpus pass could not.
    """
    version = projection_version or make_projection_version()
    report = ProjectionReport(projection_version=version)

    wanted = sorted({c for c in claim_ids if c})
    if not wanted:
        return report

    from app.catalog import assertions as store

    claims = store.by_claim_ids(wanted)
    if not claims:
        return report

    # Only the entities these claims touch, and only if the store still says
    # they may carry claims. The same authority question project() asks.
    touched: set[str] = set()
    for claim in claims:
        touched.add(claim["subject_entity_id"])
        if claim["object_entity_id"]:
            touched.add(claim["object_entity_id"])
    entities = _load_entities_by_ids(touched)
    eligible_ids = {e["entity_id"] for e in entities}
    aliases = _load_aliases(eligible_ids)

    projectable, refused = _partition_projectable(claims, eligible_ids)
    report.note(report.skipped, "claim_entity_not_eligible", refused)

    projected_ids = {c["claim_id"] for c in projectable}
    links = store.links_among(sorted(projected_ids))
    document_ids = {c["document_id"] for c in projectable if c["document_id"]}
    chunk_ids = {c["chunk_id"] for c in projectable if c["chunk_id"]}
    documents = _load_documents(document_ids)

    def retire(open_session: Any, current: list[dict[str, Any]]) -> None:
        """Drop the edges of claims in scope that no longer qualify.

        Includes the claims refused above — a claim whose entity was demoted
        must lose its edge just as surely as one that was retracted — so the
        set is every claim id this call was *asked* about, minus the ones that
        just projected a current edge.
        """
        still_current = {row["claim_id"] for row in current}
        stale = [
            {"claim_id": claim_id}
            for claim_id in wanted
            if claim_id not in still_current
        ]
        if not stale:
            return
        removed = writer.run_batches(
            open_session, writer.DELETE_CURRENT_STATE_FOR_CLAIMS, stale
        )
        report.note(report.skipped, "current_state_retired", removed)

    _in_session(
        session,
        lambda opened: _write_projection(
            opened, entities=entities, aliases=aliases, projectable=projectable,
            documents=documents, chunk_ids=chunk_ids, links=links,
            report=report, version=version, as_of=as_of, retire=retire,
        ),
    )
    logger.info(
        "Scoped projection %s: %d claim(s), %d entity(ies).",
        version, len(projectable), len(entities),
    )
    return report


def _partition_projectable(
    claims: list[dict[str, Any]], eligible_ids: set[str]
) -> tuple[list[dict[str, Any]], int]:
    """Split claims into projectable and refused.

    A claim may only be projected when *both* ends are still eligible. The
    entity store is authoritative here, not the claim row: a demotion since
    staging must take effect without rewriting claims.
    """
    projectable: list[dict[str, Any]] = []
    refused = 0
    for claim in claims:
        if claim["subject_entity_id"] not in eligible_ids:
            refused += 1
            continue
        if claim["object_entity_id"] and claim["object_entity_id"] not in eligible_ids:
            refused += 1
            continue
        projectable.append(claim)
    return projectable, refused



def _current_state_rows(
    claims: list[dict[str, Any]], *, as_of: str | None
) -> list[dict[str, Any]]:
    """Claims that may become a current-state edge.

    Entity-valued only: a literal is a property, not a relationship, so
    ``HAS_ROLE`` never becomes an edge between two nodes.
    """
    rows: list[dict[str, Any]] = []
    for claim in claims:
        if not claim["object_entity_id"]:
            continue
        if not cf.is_current_state_eligible(
            claim_types.from_row(claim), as_of=as_of
        ):
            continue
        rows.append({
            "claim_id": claim["claim_id"],
            "predicate": claim["predicate"],
            "subject_id": claim["subject_entity_id"],
            "object_id": claim["object_entity_id"],
            "confidence": float(claim["confidence"] or 0.0),
            "valid_from": _iso(claim["valid_from"]),
            "valid_until": _iso(claim["valid_until"]),
            "temporal_basis": claim["temporal_basis"],
        })
    return rows
