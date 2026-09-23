"""Build the knowledge layer from an already-ingested corpus.

Every stage below already existed, tested, in ``app.knowledge`` and
``app.catalog``; what did not exist was a committed command that ran them in
order. The knowledge layer was therefore reproducible only by hand, which is why
a clean rebuild left ``documents_entity`` and ``documents_assertion`` empty while
the phase reports recorded 1,653 staged claims. This module is that command and
nothing more: it orchestrates, it does not extract, resolve, validate or score.

    python -m scripts.build_knowledge --dry-run
    python -m scripts.build_knowledge --limit 500
    python -m scripts.build_knowledge                    # full corpus

Order, and why it cannot be rearranged
--------------------------------------
1. seed entities        from CMS metadata; everything else needs the entity ids
2. mine acronyms        pairs corpus glosses to seeded names, so it follows (1)
3. mark ambiguity       an acronym can *create* ambiguity, so it follows (2)
4. promote PIs          needs the ambiguity marks and the full person population
5. extract mentions     needs the gazetteer, which is built from (1)-(4)
6. resolve mentions     needs the entity index and the mentions from (5)
7. extract CMS claims   needs the entity index; reads CMS fields, not mentions
8. validate             trust and eligibility gates decide what may be staged
9. stage + rejections   durable claims
10. conflicts + status  supersession and dispute verdicts over the staged set
11. project             MySQL -> Neo4j

Re-running is safe by construction, and this module adds no bookkeeping of its
own: mentions are ``INSERT IGNORE`` on their span, extraction is cached on
``(content_hash, extraction_key)``, entity ids derive from the seed source,
decisions upsert per span, assertions upsert on ``claim_id``, links upsert on
their triple, and every Neo4j write is a ``MERGE``. Interrupting a run and
re-running it resumes rather than duplicates.

``--limit`` caps *extraction*. Conflict detection and projection still read
the whole staged table, so a limited run is a smaller build, never a
partial verdict about the corpus.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, Iterator

from app.knowledge.reporting import Stage, print_stages, stage_timer

logger = logging.getLogger("build_knowledge")

EXIT_OK = 0
EXIT_ERRORS = 1      # finished, but individual documents failed
EXIT_FATAL = 2       # could not run: a store was unreachable or a stage is unusable

# Points per Qdrant scroll while grouping chunks into documents.
_SCROLL_BATCH = 1000

class Fatal(RuntimeError):
    """A condition that makes the rest of the build meaningless."""


@dataclass
class Options:
    limit: int | None = None
    dry_run: bool = False
    with_mentions: bool = False
    skip_seed: bool = False
    skip_acronyms: bool = False
    skip_promotion: bool = False
    skip_project: bool = False


class Build:
    """One build run.

    Writes are gated in exactly one way: every stage computes its result first
    and reports the count from that, then writes only ``if self.writes``. A
    dry run therefore exercises the whole pipeline — including the gates that
    reject claims — and touches no store, which is what makes it a useful
    rehearsal rather than a syntax check.
    """

    def __init__(self, options: Options) -> None:
        self.o = options
        self.stages: list[Stage] = []
        self._index: Any = None

    @property
    def writes(self) -> bool:
        return not self.o.dry_run

    # ------------------------------------------------------------------ #
    # Stage plumbing
    # ------------------------------------------------------------------ #

    def _stage(self, name: str, *, skip: bool = False) -> Iterator[Stage]:
        """One timed, reported stage. The shared implementation, so a stage
        means the same thing here as it does in the per-document pipeline."""
        return stage_timer(self.stages, name, skip=skip, log=logger)

    def index(self, *, refresh: bool = False) -> Any:
        """The entity index, loaded once and reused.

        Refreshed after seeding, because everything downstream resolves against
        what seeding just wrote. The refresh also clears the process-wide cache
        in ``app.knowledge.candidates``, so anything else in this process — the
        per-document stage, a test — sees the seeded entities rather than the
        index as it stood before the build started.
        """
        from app.knowledge.candidates import get_entity_index, reload_entity_index

        if self._index is None or refresh:
            self._index = reload_entity_index() if refresh else get_entity_index()
        return self._index

    # ------------------------------------------------------------------ #
    # 1-4. Canonical entities. The same four steps, in the same order, as
    #      scripts/seed_entities.py — reused rather than reimplemented so the
    #      two commands cannot drift.
    # ------------------------------------------------------------------ #

    def seed(self) -> None:
        from app.catalog import entities as store
        from app.knowledge.seed import build_seed_entities

        with self._stage("seed", skip=self.o.skip_seed) as stage:
            if stage.skipped:
                return
            entities = build_seed_entities()
            if not entities:
                raise Fatal(
                    "no seed entities were built from the catalog; the corpus "
                    "has no CMS metadata to derive identities from"
                )
            if self.writes:
                stage.counts.update(store.save_entities(entities))
            else:
                stage.counts["entities"] = len(entities)
                stage.counts["aliases"] = sum(len(e.aliases) for e in entities)
            if stage.counts.get("identifier_conflicts"):
                # Two CMS records claiming one project code. Reported, never
                # resolved: Tier 0 must stay a lookup, so an ambiguous code is
                # better left denoting nobody than denoting the wrong project.
                stage.notes.append(
                    f"{stage.counts['identifier_conflicts']} identifier conflicts "
                    "(two records claiming one code; first kept)"
                )

    def acronyms(self) -> None:
        from app.knowledge.seed import mine_acronym_aliases

        with self._stage("acronyms", skip=self.o.skip_acronyms) as stage:
            if stage.skipped:
                return
            found = mine_acronym_aliases()
            stage.counts["mined"] = len(found)
            if self.writes:
                stage.counts["written"] = _write_acronym_aliases(found)

    def ambiguity(self) -> None:
        from app.catalog import entities as store

        with self._stage("ambiguity") as stage:
            if self.writes:
                stage.counts["marked"] = store.mark_ambiguous_aliases()
            else:
                stage.notes.append("ambiguity marking needs the alias rows above")

    def promotion(self) -> None:
        with self._stage("pi-promotion", skip=self.o.skip_promotion) as stage:
            if stage.skipped:
                return
            from app.knowledge.pi_promotion import apply_promotions, evaluate_promotions

            decisions = evaluate_promotions()
            stage.counts["considered"] = len(decisions)
            stage.counts["passed"] = sum(1 for d in decisions if d.promote)
            if self.writes:
                stage.counts["raised"] = apply_promotions(decisions)

    def author_promotion(self) -> None:
        """Raise CMS author names to ``author_attested``.

        A stage rather than a one-off script run, for the same reason
        ``pi-promotion`` is one: seeding writes ``trust`` with
        ``ON DUPLICATE KEY UPDATE``, so *every* promotion is reset to the level
        the seeder computes each time this runs. A promotion applied out of band
        survives exactly until the next build and then silently disappears,
        taking its AUTHORED edges out of the graph with it.

        Idempotent and ordered after seeding for that reason: it re-derives the
        same decisions from the same evidence and re-applies them, so the store
        converges whatever state it started in.
        """
        with self._stage("author-promotion", skip=self.o.skip_promotion) as stage:
            if stage.skipped:
                return
            from app.knowledge.author_promotion import (
                apply_promotions, evaluate_promotions,
            )

            decisions = evaluate_promotions()
            stage.counts["considered"] = len(decisions)
            stage.counts["passed"] = sum(1 for d in decisions if d.promote)
            if self.writes:
                stage.counts["raised"] = apply_promotions(decisions)

    # ------------------------------------------------------------------ #
    # 5-6. Mentions and resolution. Off by default: nothing reads these tables
    #      at query time (the router extracts from the *question* and resolves
    #      against the entity index), and this is by far the most expensive
    #      stage. They are audit and evaluation material, and the input a text
    #      claim extractor would need.
    # ------------------------------------------------------------------ #

    def mentions(self) -> None:
        """Extract and resolve mentions, one document at a time.

        Per document rather than per chunk because a document is the unit that
        can fail: one unreadable payload should cost its own document and
        nothing else. Resolution runs per chunk inside that, because it shares
        co-occurrence context across a chunk's mentions.

        Resumable without bookkeeping of its own: a chunk whose
        ``(content_hash, extraction_key)`` is already recorded is skipped, so an
        interrupted run continues where it stopped, and a re-run after a
        re-index still hits cache for every paragraph whose text is unchanged.
        """
        from app.catalog import mentions as mention_store
        from app.knowledge.candidates import context_for_document
        from app.knowledge.extract import EXTRACTOR_VERSION, extract_mentions, extraction_key
        from app.knowledge.gazetteer import gazetteer_version, get_gazetteer
        from app.knowledge.resolver import resolve_mentions

        with self._stage("mentions", skip=not self.o.with_mentions) as stage:
            if stage.skipped:
                return
            gazetteer = get_gazetteer()
            fingerprint = gazetteer_version(gazetteer)
            index = self.index()
            counts = {
                "documents": 0, "chunks": 0, "cached": 0,
                "mentions": 0, "decisions": 0,
            }

            for document_id, chunks in self._documents():
                counts["documents"] += 1
                try:
                    context = self._document_context(document_id, context_for_document)
                    for chunk in chunks:
                        counts["chunks"] += 1
                        content_hash = chunk.get("content_hash") or ""
                        key = extraction_key(content_hash, fingerprint)
                        if content_hash and mention_store.cached_extraction(
                            content_hash, key
                        ) is not None:
                            counts["cached"] += 1
                            continue

                        found = extract_mentions(
                            chunk.get("chunk_text") or "",
                            chunk_id=chunk["chunk_id"],
                            document_id=document_id,
                            gazetteer=gazetteer,
                        )
                        counts["mentions"] += len(found)
                        decisions = resolve_mentions(found, index, context)
                        counts["decisions"] += len(decisions)

                        if self.writes:
                            mention_store.save_mentions(
                                found, doc_version=chunk.get("doc_version")
                            )
                            if decisions:
                                from app.catalog import entities as entity_store

                                entity_store.save_decisions(decisions)
                            if content_hash:
                                mention_store.record_extraction(
                                    content_hash, key, EXTRACTOR_VERSION, len(found)
                                )
                except Exception as exc:
                    # One document's failure is that document's failure. The
                    # documents already written stay written — every writer here
                    # commits per call — and the id is reported so the run can
                    # be repeated for it alone.
                    logger.warning("Document %s failed: %s", document_id, exc)
                    stage.errors.append({"id": document_id, "error": str(exc)})

            stage.counts.update(counts)

    def _document_context(self, document_id: str, context_for_document: Any) -> Any:
        """Corroboration from the document's own CMS metadata, or an empty one.

        Read through ``state.raw_meta_for`` rather than off a ``StateRecord``.
        The record has a ``raw_meta`` field but ``state._row_to_record`` never
        fills it — the blob is far too large to carry on every record
        ``state.load`` builds — so this used to be unconditionally ``None`` and
        every document resolved with an *empty* context.

        That failed silently and in the expensive direction. Corroboration is
        what the resolver requires before it will link a PERSON at all
        (``thresholds.require_corroboration``), so with it always empty a
        uniquely-matching person name landed on ``AMBIGUOUS`` — "unique name
        match but no corroborating context" — instead of ``AUTO``. Nothing
        errored; the run simply resolved less than it could, and no count said
        why.

        Author names come from the ``documents_author`` facet, not the metadata
        blob. They were moved there and ``raw_meta.field_authors`` is now empty
        corpus-wide, so reading only the blob left PERSON — the one type that
        *requires* corroboration — with none of it. Fixing the blob read alone
        would have changed nothing for PERSON.
        """
        from app.catalog import state

        return context_for_document(
            document_id,
            state.raw_meta_for(document_id),
            authors=state.authors_for(document_id),
        )

    def _documents(self) -> Iterator[tuple[str, list[dict[str, Any]]]]:
        """Current child chunks from Qdrant, grouped by document, capped by
        ``--limit``.

        The filter is written here rather than borrowed from retrieval on
        purpose: retrieval excludes tables of contents and bibliographies
        because they pollute *search*, but a bibliography is exactly where
        author names live, so extraction wants them.
        """
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        from app.config import get_settings
        from app.core.clients import get_qdrant_client

        client = get_qdrant_client()
        collection = get_settings().qdrant_collection
        scroll_filter = Filter(
            must=[
                FieldCondition(key="is_parent", match=MatchValue(value=False)),
                FieldCondition(key="is_current", match=MatchValue(value=True)),
            ]
        )

        grouped: dict[str, list[dict[str, Any]]] = {}
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=collection, scroll_filter=scroll_filter,
                limit=_SCROLL_BATCH, with_payload=True, with_vectors=False,
                offset=offset,
            )
            for point in points:
                payload = point.payload or {}
                document_id = payload.get("document_id")
                if not document_id:
                    continue
                if document_id not in grouped:
                    if self.o.limit is not None and len(grouped) >= self.o.limit:
                        continue
                    grouped[document_id] = []
                grouped[document_id].append({**payload, "chunk_id": str(point.id)})
            if offset is None:
                break
        yield from grouped.items()

    # ------------------------------------------------------------------ #
    # 7-10. Claims. Extraction reads CMS fields directly, so this path does
    #       not depend on mentions; validation is what enforces trust and
    #       eligibility, and it is never bypassed.
    # ------------------------------------------------------------------ #

    def claims(self) -> list[Any]:
        """Extract, validate and stage CMS claims. Returns what was staged."""
        from app.config import get_settings
        from app.knowledge.claims.extract_cms import extract_cms_claims
        from app.knowledge.claims.validate import dedupe, validate

        with self._stage("claims") as stage:
            built = extract_cms_claims(self.index(), limit=self.o.limit)
            stage.counts["built"] = len(built)
            if not built:
                return []

            # The gates that decide what may become a claim live here, and this
            # module does not get a say in them: a provisional PERSON is refused
            # as `object_not_claim_eligible` exactly as it is on the ingest path.
            result = validate(
                built,
                index=self.index(),
                # CMS-field claims quote a metadata value, not a passage, so
                # there is no chunk text for quote verification to locate.
                chunk_texts={},
                min_confidence=get_settings().claim_min_confidence,
            )
            accepted = dedupe(result.accepted)
            stage.counts["accepted"] = len(accepted)
            stage.counts["rejected"] = len(result.rejected)
            for code, count in sorted(result.counts.items()):
                stage.counts[f"rejected_{code}"] = count

            if self.writes:
                from app.catalog import assertions as store

                stage.counts["staged"] = store.stage(accepted)
                stage.counts["rejections_recorded"] = store.record_rejections(
                    result.rejected
                )
                # `built`, not `accepted`. Staleness is a question about what
                # the *source states* — what extraction produced — not about
                # what validation allowed. A claim rejected as
                # `object_not_claim_eligible` is still stated by its CMS field
                # and must not be retracted; passing the full extraction keeps
                # its id in the fresh set so it is protected, while widening the
                # (document, field) pairs this run is entitled to judge.
                stage.counts["retracted"] = self._retract_stale(built)
            return accepted

    def _retract_stale(self, fresh: list[Any]) -> int:
        """Retract staged CMS claims their source no longer supports.

        ``extract_cms.stale_claim_ids`` was written for this and, until now, was
        called by nothing but its own tests — so a claim whose sponsor was
        corrected away, or whose subject stopped being claim-eligible, stayed
        ``active`` in the table forever. A full corpus run measured 17 of them:
        still staged, still active, and refused by projection as
        ``claim_entity_not_eligible`` on every pass without anyone being told.

        Retracted, never deleted: the claim was true of the source as it stood.
        Only ``(document, field)`` pairs this run actually examined are judged,
        so a ``--limit`` run cannot retract claims it never looked at.
        """
        from app.catalog import assertions as store
        from app.knowledge.claims.extract_cms import stale_claim_ids

        try:
            stale = stale_claim_ids(fresh, store.all_staged())
        except Exception as exc:  # pragma: no cover - store hiccup
            logger.warning("Could not check for stale claims: %s", exc)
            return 0
        return store.retract(stale) if stale else 0

    def conflicts(self, staged: list[Any]) -> None:
        """Supersession and dispute verdicts over **every** staged claim.

        Not over this run's batch. That distinction used to be forced: ``detect``
        reads assertion objects while the store returns dicts, and with no
        reverse mapping a pass could only examine what it had just built. So the
        stage examined its own CMS batch and reported an error whenever the
        table held more — which it now always does, because the per-document
        ingest path stages claims this pass never re-extracts (LLM claims from
        chunk text, and anything staged since the last run).

        ``claims.types.from_row`` supplies the missing mapping, so the whole
        table is examinable and this stage examines it. That matters beyond
        tidiness: conflict detection is what stops two documents asserting
        different principal investigators for one project from *both* producing
        a current-state edge. A claim outside the batch escaping it is a
        correctness hole, not a reporting gap.

        A consequence worth stating: ``--limit`` no longer makes conflict
        detection partial. It limits *extraction*; the table it then examines is
        whole either way.

        Under ``--dry-run`` nothing was staged, so the batch is all there is and
        the stage says so.
        """
        from app.catalog import assertions as store
        from app.knowledge.claims import conflicts as detector
        from app.knowledge.claims import types as t

        with self._stage("conflicts") as stage:
            if self.writes:
                try:
                    rows = store.all_staged()
                except Exception as exc:
                    stage.fail("conflicts", f"could not read staged claims: {exc}")
                    return
                scope = [t.from_row(row) for row in rows]
                stage.counts["from_store"] = len(scope)
                stage.counts["beyond_this_batch"] = max(0, len(scope) - len(staged))
            else:
                scope = list(staged)
                stage.notes.append(
                    "dry run: nothing was staged, so only this run's batch could "
                    "be examined"
                )

            if not scope:
                stage.notes.append("no claims to examine")
                return

            report = detector.detect(scope)
            stage.counts["examined"] = report.examined
            stage.counts["groups"] = report.groups
            stage.counts["links"] = len(report.links)
            stage.counts["disputed"] = len(report.disputed)
            stage.counts["superseded"] = len(report.superseded)

            if self.writes:
                # Statuses before links, so an interruption between them leaves a
                # suppressed claim missing its audit link rather than an
                # unsuppressed claim projecting an edge it should not.
                stage.counts["status_applied"] = store.apply_status(
                    report.status_changes
                )
                stage.counts["links_saved"] = store.save_links(
                    report.links, detector=detector.DETECTOR_VERSION
                )

    # ------------------------------------------------------------------ #
    # 11. Projection.
    # ------------------------------------------------------------------ #

    def project(self) -> None:
        with self._stage("project", skip=self.o.skip_project) as stage:
            if stage.skipped:
                return
            from app.core.clients import graph_available

            if not graph_available():
                stage.notes.append(
                    "Neo4j unreachable; MySQL is authoritative and "
                    "scripts.project_graph rebuilds the projection later"
                )
                stage.errors.append({"id": "neo4j", "error": "unreachable"})
                return
            if not self.writes:
                stage.notes.append("projection skipped under --dry-run")
                return
            from app.knowledge.graph.project import project as run_projection
            from app.knowledge.graph.schema import ensure_graph_schema

            # Same two calls, in the same order, as scripts/project_graph.py:
            # the constraints have to exist before the first MERGE.
            ensure_graph_schema()
            report = run_projection().as_dict()
            # Flattened, because every count in the report lives one level down
            # under nodes/relationships/skipped. The previous form looked for
            # top-level ints, of which there are none, so this stage reported
            # "-" however much it had just written.
            stage.notes.append(f"version {report['projection_version']}")
            for bucket in ("nodes", "relationships", "skipped"):
                for name, count in sorted(report.get(bucket, {}).items()):
                    stage.counts[f"{bucket[:4]}_{name}"] = count

    # ------------------------------------------------------------------ #
    # Run
    # ------------------------------------------------------------------ #

    def run(self) -> int:
        self.seed()
        self.acronyms()
        self.ambiguity()
        self.promotion()
        self.author_promotion()
        # Everything below resolves against what seeding just wrote, including
        # the promotions — a PI raised above is claim-eligible from here on.
        self.index(refresh=True)
        self.mentions()
        staged = self.claims()
        self.conflicts(staged)
        self.project()
        return self.exit_code()

    def exit_code(self) -> int:
        return EXIT_ERRORS if any(s.errors for s in self.stages) else EXIT_OK

    def report(self) -> dict[str, Any]:
        return {
            "dry_run": self.o.dry_run,
            "limit": self.o.limit,
            "with_mentions": self.o.with_mentions,
            "stages": [s.as_dict() for s in self.stages],
            "errors": sum(len(s.errors) for s in self.stages),
        }


def _write_acronym_aliases(found: list[tuple[str, str, int]]) -> int:
    """Persist mined acronym aliases.

    The same statement ``scripts/seed_entities.py`` uses. ``INSERT IGNORE``
    keeps a re-run from duplicating an alias that is already there.
    """
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection
    from app.knowledge.normalize import normalize_org

    if not found:
        return 0
    table = state_table()
    rows = [
        (entity_id, normalize_org(acronym), acronym, "acronym", 1, 0, f"gloss_x{count}")
        for entity_id, acronym, count in found
    ]
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.executemany(
            f"INSERT IGNORE INTO `{table}_entity_alias` "
            "(entity_id, normalized, surface, alias_type, autolink, is_ambiguous, "
            " source) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            rows,
        )
        conn.commit()
    return len(rows)


def _print(report: dict[str, Any]) -> None:
    mode = "DRY RUN — nothing written" if report["dry_run"] else "writing"
    scope = f"limit={report['limit']}" if report["limit"] else "full corpus"
    print(f"build_knowledge ({mode}, {scope})")
    print_stages(report["stages"])
    print(f"  {'errors':14} {report['errors']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Pilot control: cap the documents processed. Makes conflict "
             "detection partial (see the run's notes).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run every stage and write nothing.",
    )
    parser.add_argument(
        "--with-mentions", action="store_true",
        help="Also extract and resolve mentions. Off by default: nothing reads "
             "those tables at query time, and it is the most expensive stage.",
    )
    parser.add_argument("--skip-seed", action="store_true")
    parser.add_argument("--skip-acronyms", action="store_true")
    parser.add_argument("--skip-promotion", action="store_true")
    parser.add_argument("--skip-project", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    build = Build(
        Options(
            limit=args.limit, dry_run=args.dry_run, with_mentions=args.with_mentions,
            skip_seed=args.skip_seed, skip_acronyms=args.skip_acronyms,
            skip_promotion=args.skip_promotion, skip_project=args.skip_project,
        )
    )
    try:
        code = build.run()
    except Fatal as exc:
        logger.error("%s", exc)
        return EXIT_FATAL
    except Exception:
        logger.exception("Knowledge build failed.")
        return EXIT_FATAL

    report = build.report()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print(report)
    return code


if __name__ == "__main__":
    sys.exit(main())
