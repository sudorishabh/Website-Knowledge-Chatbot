"""Orchestration tests for scripts/build_knowledge.

The stages themselves are already covered by the knowledge-layer suites; what
is untested until now is the *wiring*: that the stages run in the order their
dependencies require, that a dry run touches no store, that ``--limit`` is
never presented as a corpus-wide verdict, and that the eligibility gates are
reached rather than bypassed.

Every library call is stubbed by patching the real modules' attributes, so the
call sites under test are the real ones. Nothing here needs MySQL, Qdrant,
Neo4j or an LLM, and several tests assert that no writer is reached at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from app.catalog import assertions as assertion_store
from app.catalog import entities as entity_store
from app.knowledge import pi_promotion, seed
from app.knowledge.claims import conflicts, extract_cms, validate
from scripts import build_knowledge as bk


@dataclass
class _SeedEntity:
    entity_id: str = "e1"
    aliases: list = field(default_factory=lambda: [("TERI", "acronym", "cms")])


@dataclass
class _Assertion:
    """Enough of an assertion for the orchestrator to move it around."""

    claim_id: str
    subject_entity_id: str = "org-1"
    object_entity_id: str | None = "org-2"
    predicate: str = "FUNDED_BY"
    status: str = "active"
    claim_eligible: bool = True
    # Read by extract_cms.stale_claim_ids, which the claims stage now calls to
    # retract claims the source stopped supporting.
    document_id: str = "d1"
    source_field: str = "field_completed_sponsors"
    evidence_kind: str = "cms_field"


def _staged_row(claim_id: str, **overrides) -> dict:
    """A staged claim as the store returns it, for `types.from_row`."""
    row = dict(
        claim_id=claim_id, subject_entity_id="org-1", predicate="FUNDED_BY",
        object_entity_id="org-2", object_literal=None, document_id="d1",
        chunk_id=None, evidence_kind="cms_field",
        source_field="field_completed_sponsors", quote=None, quote_start=None,
        quote_end=None, valid_from=None, valid_until=None,
        temporal_basis="unknown", confidence=1.0, status="active",
        extraction_method="cms_field", extractor_version="claims-cms-v2",
        vocabulary_version="predicates-v1", model=None, prompt_version=None,
    )
    row.update(overrides)
    return row


class _Recorder:
    """Records every write the run attempts, so a dry run can be asserted empty."""

    def __init__(self):
        self.calls: list[str] = []
        self.staged: list = []

    def writer(self, name, result=1):
        def _call(*args, **kwargs):
            self.calls.append(name)
            if name == "stage" and args:
                self.staged.extend(args[0])
            return result
        return _call


@pytest.fixture
def wired(monkeypatch):
    rec = _Recorder()

    # 1-4. entities
    monkeypatch.setattr(seed, "build_seed_entities",
                        lambda: [_SeedEntity(), _SeedEntity("e2")])
    monkeypatch.setattr(seed, "mine_acronym_aliases", lambda: [("e1", "TERI", 4)])
    monkeypatch.setattr(
        entity_store, "save_entities",
        rec.writer("save_entities", {"entities": 3, "aliases": 2,
                                     "identifiers": 1, "identifier_conflicts": 0}),
    )
    monkeypatch.setattr(entity_store, "mark_ambiguous_aliases",
                        rec.writer("mark_ambiguous_aliases"))
    monkeypatch.setattr(bk, "_write_acronym_aliases", rec.writer("acronym_aliases"))
    monkeypatch.setattr(pi_promotion, "evaluate_promotions",
                        lambda: [SimpleNamespace(promote=True),
                                 SimpleNamespace(promote=False)])
    monkeypatch.setattr(pi_promotion, "apply_promotions", rec.writer("apply_promotions"))
    monkeypatch.setattr(bk.Build, "index",
                        lambda self, refresh=False: SimpleNamespace(entities={}))

    # 7-10. claims
    monkeypatch.setattr(extract_cms, "extract_cms_claims",
                        lambda index, limit=None: [_Assertion("c1"), _Assertion("c2")])
    monkeypatch.setattr(
        validate, "validate",
        lambda assertions, **kw: validate.ValidationResult(list(assertions), []),
    )
    monkeypatch.setattr(validate, "dedupe", lambda assertions: list(assertions))
    monkeypatch.setattr(assertion_store, "stage", rec.writer("stage", 2))
    monkeypatch.setattr(assertion_store, "record_rejections",
                        rec.writer("record_rejections", 0))
    monkeypatch.setattr(assertion_store, "total", lambda: 2)
    # Conflict detection now reads the whole staged table rather than this
    # run's batch, and the claims stage retracts what the source no longer
    # supports. Both go through all_staged.
    monkeypatch.setattr(
        assertion_store, "all_staged",
        lambda: [_staged_row("c1"), _staged_row("c2")],
    )
    monkeypatch.setattr(assertion_store, "retract", rec.writer("retract", 0))
    monkeypatch.setattr(
        conflicts, "detect",
        lambda assertions: conflicts.ConflictReport(
            links=[], status_changes={}, examined=len(assertions), groups=1
        ),
    )
    monkeypatch.setattr(assertion_store, "save_links", rec.writer("save_links", 0))
    monkeypatch.setattr(assertion_store, "apply_status", rec.writer("apply_status", 0))
    return rec


def _run(**options):
    build = bk.Build(bk.Options(**options))
    return build, build.run()


def _stage(build, name):
    return [s for s in build.stages if s.name == name][0]


# --------------------------------------------------------------------------- #
# Dry run writes nothing.
# --------------------------------------------------------------------------- #

def test_dry_run_writes_nothing(wired):
    _, code = _run(dry_run=True, skip_project=True)
    assert wired.calls == []
    assert code == bk.EXIT_OK


def test_dry_run_still_runs_every_stage(wired):
    """A rehearsal that skipped the work would prove nothing."""
    build, _ = _run(dry_run=True, skip_project=True)
    assert [s.name for s in build.stages if not s.skipped] == [
        "seed", "acronyms", "ambiguity", "pi-promotion", "author-promotion",
        "claims", "conflicts",
    ]  # mentions is skipped unless --with-mentions
    assert _stage(build, "seed").counts["entities"] == 2
    assert _stage(build, "claims").counts["built"] == 2
    assert _stage(build, "claims").counts["accepted"] == 2
    assert "staged" not in _stage(build, "claims").counts


def test_dry_run_does_not_project(wired):
    build, _ = _run(dry_run=True)
    project = _stage(build, "project")
    assert not project.skipped and any("dry-run" in n for n in project.notes)


# --------------------------------------------------------------------------- #
# A real run writes, in dependency order.
# --------------------------------------------------------------------------- #

def test_write_run_calls_the_writers_in_order(wired):
    _, code = _run(skip_project=True)
    assert wired.calls == [
        "save_entities", "acronym_aliases", "mark_ambiguous_aliases",
        "apply_promotions", "stage", "record_rejections",
        # No "retract": nothing was stale, and the stage does not call the
        # writer to retract an empty list.
        # Statuses before links: an interruption between them must leave a
        # suppressed claim missing its audit link, never an unsuppressed claim
        # projecting an edge it should not.
        "apply_status", "save_links",
    ]
    assert code == bk.EXIT_OK


def test_stage_order_is_fixed(wired):
    build, _ = _run(skip_project=True)
    assert [s.name for s in build.stages] == [
        "seed", "acronyms", "ambiguity", "pi-promotion", "author-promotion",
        "mentions", "claims", "conflicts", "project",
    ]


def test_skips_are_honoured(wired):
    build, _ = _run(skip_seed=True, skip_acronyms=True, skip_promotion=True,
                    skip_project=True)
    assert "save_entities" not in wired.calls
    assert {s.name for s in build.stages if s.skipped} == {
        "seed", "acronyms", "pi-promotion", "author-promotion", "mentions",
        "project",
    }


# --------------------------------------------------------------------------- #
# --limit is a pilot control, never a corpus verdict.
# --------------------------------------------------------------------------- #

def test_limit_is_passed_to_claim_extraction(wired, monkeypatch):
    seen: list = []
    monkeypatch.setattr(
        extract_cms, "extract_cms_claims",
        lambda index, limit=None: seen.append(limit) or [_Assertion("c1")],
    )
    _run(limit=500, skip_project=True)
    assert seen == [500]


def test_conflicts_examine_the_whole_table_not_just_this_batch(wired, monkeypatch):
    """The correctness property, not a reporting nicety.

    The per-document ingest path stages claims this pass never re-extracts —
    LLM claims read from chunk text. A batch-scoped conflict pass would let one
    of those assert a second principal investigator for a project without ever
    being compared against the first, and both would project a current-state
    edge. So the pass reads the table.
    """
    from app.knowledge.claims import types as t

    outside = dict(
        claim_id="staged_elsewhere", subject_entity_id="project-1",
        predicate="LED_BY", object_entity_id="person-2", object_literal=None,
        document_id="d9", chunk_id="c9", evidence_kind="chunk",
        source_field=None, quote=None, quote_start=None, quote_end=None,
        valid_from="2020-01-01", valid_until=None,
        temporal_basis="stated", confidence=0.9, status="active",
        extraction_method="llm", extractor_version="claims-llm-v1",
        vocabulary_version="predicates-v1", model=None, prompt_version=None,
    )
    monkeypatch.setattr(assertion_store, "all_staged", lambda: [outside])

    seen: list = []
    monkeypatch.setattr(
        conflicts, "detect",
        lambda assertions: seen.append(list(assertions))
        or conflicts.ConflictReport(links=[], status_changes={},
                                    examined=len(assertions), groups=1),
    )

    build, code = _run(skip_project=True)
    stage = _stage(build, "conflicts")
    assert [a.claim_id for a in seen[0]] == ["staged_elsewhere"]
    assert stage.counts["from_store"] == 1
    assert code == bk.EXIT_OK


def test_a_limited_run_is_no_longer_a_partial_verdict(wired):
    """--limit caps extraction; the table it then examines is whole either way,
    so there is nothing partial left to warn about."""
    build, code = _run(limit=500, skip_project=True)
    stage = _stage(build, "conflicts")
    assert "partial" not in stage.counts
    assert "unexamined" not in stage.counts
    assert stage.notes == []
    assert code == bk.EXIT_OK


def test_a_dry_run_says_it_could_only_see_its_own_batch(wired):
    """Nothing was staged, so the table cannot be the scope and the run says so
    instead of quietly examining an empty one."""
    build, _ = _run(dry_run=True, skip_project=True)
    stage = _stage(build, "conflicts")
    assert any("only this run's batch" in n for n in stage.notes)
    assert "from_store" not in stage.counts


def test_status_is_applied_before_links_are_saved(wired, monkeypatch):
    """Interruption-safe order: a suppressed claim missing its audit link beats
    an unsuppressed claim projecting an edge it should not."""
    monkeypatch.setattr(
        conflicts, "detect",
        lambda assertions: conflicts.ConflictReport(
            links=[conflicts.ClaimLink("a", "b", "supersedes", "why")],
            status_changes={"b": "superseded"}, examined=2, groups=1,
        ),
    )
    build, _ = _run(skip_project=True)
    calls = wired.calls
    assert calls.index("apply_status") < calls.index("save_links")


# --------------------------------------------------------------------------- #
# Eligibility is enforced by validation, and the orchestrator cannot skip it.
# --------------------------------------------------------------------------- #

def test_provisional_entities_never_reach_staging(wired, monkeypatch):
    """A provisional PERSON is refused as `object_not_claim_eligible`. Only the
    accepted assertions may be staged."""
    good = _Assertion("keep")
    bad = _Assertion("drop", object_entity_id="person-provisional")
    monkeypatch.setattr(extract_cms, "extract_cms_claims",
                        lambda index, limit=None: [good, bad])
    monkeypatch.setattr(
        validate, "validate",
        lambda assertions, **kw: validate.ValidationResult(
            [a for a in assertions if a.claim_id != "drop"],
            [validate.Rejection("object_not_claim_eligible", "provisional", bad)],
        ),
    )
    build, _ = _run(skip_project=True)

    assert [a.claim_id for a in wired.staged] == ["keep"]
    stage = _stage(build, "claims")
    assert stage.counts["rejected"] == 1
    assert stage.counts["rejected_object_not_claim_eligible"] == 1


def test_validation_receives_the_configured_confidence_floor(wired, monkeypatch):
    from app.config import get_settings

    seen: dict = {}
    monkeypatch.setattr(
        validate, "validate",
        lambda assertions, **kw: seen.update(kw) or validate.ValidationResult(
            list(assertions), []
        ),
    )
    _run(skip_project=True)
    assert seen["min_confidence"] == get_settings().claim_min_confidence


def test_extracting_nothing_still_examines_what_is_already_staged(wired, monkeypatch):
    """A run that extracts nothing is not a run with nothing to check.

    Claims staged by an earlier run, or by the per-document ingest path, are
    still in the table and still need conflict verdicts. Before the pass read
    the table, an empty extraction meant an empty conflict pass.
    """
    monkeypatch.setattr(extract_cms, "extract_cms_claims", lambda index, limit=None: [])
    build, code = _run(skip_project=True)
    assert "stage" not in wired.calls
    assert _stage(build, "conflicts").counts["from_store"] == 2
    assert code == bk.EXIT_OK


def test_an_empty_table_leaves_conflicts_empty(wired, monkeypatch):
    monkeypatch.setattr(extract_cms, "extract_cms_claims", lambda index, limit=None: [])
    monkeypatch.setattr(assertion_store, "all_staged", lambda: [])
    build, code = _run(skip_project=True)
    assert _stage(build, "conflicts").notes == ["no claims to examine"]
    assert code == bk.EXIT_OK


# --------------------------------------------------------------------------- #
# Re-running, and failure isolation.
# --------------------------------------------------------------------------- #

def test_running_twice_repeats_the_same_writes(wired):
    """The orchestrator adds no bookkeeping: a second run offers the same rows,
    and the writers' own upsert/IGNORE semantics absorb them."""
    _run(skip_project=True)
    first = list(wired.calls)
    wired.calls.clear()
    _run(skip_project=True)
    assert wired.calls == first


def test_a_later_stage_failing_leaves_earlier_writes_intact(wired, monkeypatch):
    """Claims are staged before conflict detection runs. If detection blows up,
    the staged claims stay staged — the run reports fatal, it does not roll back
    work that already succeeded."""
    def boom(assertions):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(conflicts, "detect", boom)
    build = bk.Build(bk.Options(skip_project=True))
    with pytest.raises(RuntimeError):
        build.run()
    assert "stage" in wired.calls
    assert [a.claim_id for a in wired.staged] == ["c1", "c2"]


# --------------------------------------------------------------------------- #
# Mentions: off by default, resumable, and isolated per document.
# --------------------------------------------------------------------------- #

@pytest.fixture
def mention_wiring(monkeypatch, wired):
    """Stub the per-chunk path. Returns the recorder plus a knob for failures."""
    from app.catalog import mentions as mention_store
    from app.knowledge import extract, gazetteer, resolver

    state = SimpleNamespace(cached=set(), failing=set(), saved=[], recorded=[])

    monkeypatch.setattr(gazetteer, "get_gazetteer", lambda: object())
    monkeypatch.setattr(gazetteer, "gazetteer_version", lambda g: "gaz-1")
    monkeypatch.setattr(extract, "extraction_key", lambda h, f: f"{h}:{f}")

    def _extract(text, *, chunk_id, document_id, gazetteer=None):
        if document_id in state.failing:
            raise RuntimeError("unreadable payload")
        return [SimpleNamespace(chunk_id=chunk_id, surface_text=text)]

    monkeypatch.setattr(extract, "extract_mentions", _extract)
    monkeypatch.setattr(resolver, "resolve_mentions",
                        lambda mentions, index, context: list(mentions))
    monkeypatch.setattr(
        mention_store, "cached_extraction",
        lambda content_hash, key: 1 if content_hash in state.cached else None,
    )
    monkeypatch.setattr(
        mention_store, "save_mentions",
        lambda mentions, doc_version=None: state.saved.extend(mentions) or len(mentions),
    )
    monkeypatch.setattr(
        mention_store, "record_extraction",
        lambda *a, **k: state.recorded.append(a[0]),
    )
    monkeypatch.setattr(entity_store, "save_decisions", wired.writer("save_decisions"))
    monkeypatch.setattr(bk.Build, "_document_context", lambda self, d, f: None)

    def _documents(self):
        docs = [("doc-a", [{"chunk_id": "a1", "chunk_text": "x", "content_hash": "ha"}]),
                ("doc-b", [{"chunk_id": "b1", "chunk_text": "y", "content_hash": "hb"}])]
        limit = self.o.limit
        return iter(docs[:limit] if limit is not None else docs)

    monkeypatch.setattr(bk.Build, "_documents", _documents)
    return state


def test_mentions_are_off_by_default(mention_wiring):
    build, _ = _run(skip_project=True)
    assert _stage(build, "mentions").skipped
    assert mention_wiring.saved == []


def test_mentions_run_when_asked(mention_wiring):
    build, _ = _run(with_mentions=True, skip_project=True)
    stage = _stage(build, "mentions")
    assert stage.counts["documents"] == 2
    assert stage.counts["chunks"] == 2
    assert stage.counts["mentions"] == 2
    assert stage.counts["decisions"] == 2
    assert len(mention_wiring.saved) == 2


def test_dry_run_extracts_but_saves_no_mentions(mention_wiring):
    build, _ = _run(with_mentions=True, dry_run=True, skip_project=True)
    assert _stage(build, "mentions").counts["mentions"] == 2
    assert mention_wiring.saved == []
    assert mention_wiring.recorded == []


def test_already_extracted_chunks_are_skipped(mention_wiring):
    """The resume path: a cached (content_hash, extraction_key) is not redone."""
    mention_wiring.cached.add("ha")
    build, _ = _run(with_mentions=True, skip_project=True)
    stage = _stage(build, "mentions")
    assert stage.counts["cached"] == 1
    assert stage.counts["mentions"] == 1
    assert [m.chunk_id for m in mention_wiring.saved] == ["b1"]


def test_a_failing_document_does_not_stop_the_others(mention_wiring):
    """The isolation guarantee: doc-a fails, doc-b is still processed and its
    mentions are still written."""
    mention_wiring.failing.add("doc-a")
    build, code = _run(with_mentions=True, skip_project=True)
    stage = _stage(build, "mentions")

    assert stage.errors == [{"id": "doc-a", "error": "unreadable payload"}]
    assert [m.chunk_id for m in mention_wiring.saved] == ["b1"]
    assert stage.counts["documents"] == 2
    assert code == bk.EXIT_ERRORS


def test_a_failing_document_does_not_undo_earlier_writes(mention_wiring):
    """doc-a succeeds, doc-b fails: doc-a's mentions stay written."""
    mention_wiring.failing.add("doc-b")
    build, code = _run(with_mentions=True, skip_project=True)
    assert [m.chunk_id for m in mention_wiring.saved] == ["a1"]
    assert [e["id"] for e in _stage(build, "mentions").errors] == ["doc-b"]
    assert code == bk.EXIT_ERRORS


def test_a_failing_document_does_not_stop_later_stages(mention_wiring):
    """Claims still run: they read CMS fields, not mentions."""
    mention_wiring.failing.add("doc-a")
    build, _ = _run(with_mentions=True, skip_project=True)
    assert _stage(build, "claims").counts["built"] == 2


def test_limit_caps_the_documents_processed(mention_wiring):
    build, _ = _run(with_mentions=True, limit=1, skip_project=True)
    assert _stage(build, "mentions").counts["documents"] == 1


# --------------------------------------------------------------------------- #
# Fatal conditions and the CLI surface.
# --------------------------------------------------------------------------- #

def test_no_seed_entities_is_fatal(wired, monkeypatch):
    monkeypatch.setattr(seed, "build_seed_entities", lambda: [])
    assert bk.main(["--skip-project"]) == bk.EXIT_FATAL


def test_unexpected_failure_is_fatal_not_silent(wired, monkeypatch):
    def boom():
        raise RuntimeError("mysql down")

    monkeypatch.setattr(seed, "build_seed_entities", boom)
    assert bk.main(["--skip-project"]) == bk.EXIT_FATAL


def test_cli_dry_run_reports_json(wired, capsys):
    assert bk.main(["--limit", "500", "--dry-run", "--skip-project", "--json"]) == bk.EXIT_OK
    report = json.loads(capsys.readouterr().out)
    assert report["limit"] == 500
    assert report["dry_run"] is True
    assert report["with_mentions"] is False
    notes = [n for s in report["stages"] for n in s["notes"]]
    assert any("only this run's batch" in n for n in notes)


def test_cli_text_report_shows_the_dry_run_scope(wired, capsys):
    bk.main(["--limit", "500", "--dry-run", "--skip-project"])
    assert "only this run's batch" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Resolution context: the corroboration the mention stage resolves against
# --------------------------------------------------------------------------- #

def test_document_context_reads_raw_meta_from_its_own_reader(monkeypatch):
    """Regression, and it failed silently in the expensive direction.

    ``StateRecord`` has a ``raw_meta`` field but ``state._row_to_record`` never
    fills it — the blob is far too large to carry on every record
    ``state.load`` builds. Reading it off the record therefore yielded ``None``
    for every document, so the resolver saw an empty context. Corroboration is
    what it requires before linking a PERSON at all, so a uniquely-matching
    name landed on AMBIGUOUS instead of AUTO, with nothing in any count saying
    why.
    """
    from types import SimpleNamespace

    from app.catalog import state
    from app.knowledge.candidates import context_for_document

    # Exactly what state.get returns today: a record whose raw_meta is None.
    monkeypatch.setattr(
        state, "get", lambda document_id: SimpleNamespace(raw_meta=None)
    )
    monkeypatch.setattr(
        state, "raw_meta_for",
        lambda document_id: {
            "field_authors": ["Asha Rao"],
            "field_completed_sponsors": ["Ministry of Power"],
        },
    )
    monkeypatch.setattr(state, "authors_for", lambda document_id: [])

    build = bk.Build(bk.Options())
    context = build._document_context("doc-1", context_for_document)

    assert context.asserts("PERSON", "asha rao")
    assert context.asserts("ORGANIZATION", "ministry of power")


def test_document_context_is_empty_when_a_document_has_no_metadata(monkeypatch):
    from app.catalog import state
    from app.knowledge.candidates import context_for_document

    monkeypatch.setattr(state, "raw_meta_for", lambda document_id: None)
    monkeypatch.setattr(state, "authors_for", lambda document_id: [])
    build = bk.Build(bk.Options())
    context = build._document_context("doc-1", context_for_document)
    assert dict(context.cms_names) == {}
    assert context.document_id == "doc-1"


def test_document_context_does_not_fetch_the_whole_catalog_row(monkeypatch):
    """`state.get` selects every column including the metadata blob. The
    context needs only that blob, and the mention stage asks once per document,
    so the narrow reader is the right one."""
    from app.catalog import state
    from app.knowledge.candidates import context_for_document

    def _forbidden(document_id):
        raise AssertionError("the context must not read the whole row")

    monkeypatch.setattr(state, "get", _forbidden)
    monkeypatch.setattr(state, "raw_meta_for", lambda document_id: {})
    monkeypatch.setattr(state, "authors_for", lambda document_id: [])
    build = bk.Build(bk.Options())
    build._document_context("doc-1", context_for_document)


def test_person_corroboration_comes_from_the_author_facet(monkeypatch):
    """Regression, and the one that actually changed behaviour.

    Author names were moved out of ``raw_meta.field_authors`` into the
    ``documents_author`` facet — the metadata key is empty corpus-wide while
    the facet holds 1,860 rows. Reading only the metadata therefore left PERSON
    corroboration empty for *every* document, and PERSON is the one type the
    resolver requires corroboration for.
    """
    from app.catalog import state
    from app.knowledge.candidates import context_for_document

    monkeypatch.setattr(state, "raw_meta_for", lambda document_id: {})
    monkeypatch.setattr(
        state, "authors_for", lambda document_id: ["Dr Preeti Jain Das"]
    )

    build = bk.Build(bk.Options())
    context = build._document_context("doc-1", context_for_document)
    assert context.asserts("PERSON", "preeti jain das")


def test_the_two_author_sources_are_unioned_not_preferred(monkeypatch):
    """A corpus that repopulates the metadata key must keep working, so neither
    source wins — both contribute."""
    from app.catalog import state
    from app.knowledge.candidates import context_for_document

    monkeypatch.setattr(
        state, "raw_meta_for", lambda document_id: {"field_authors": ["Asha Rao"]}
    )
    monkeypatch.setattr(state, "authors_for", lambda document_id: ["Meena Sehgal"])

    build = bk.Build(bk.Options())
    context = build._document_context("doc-1", context_for_document)
    assert context.asserts("PERSON", "asha rao")
    assert context.asserts("PERSON", "meena sehgal")


def test_author_corroboration_revives_the_false_merge_guard():
    """What the empty context was costing, stated as the rule it disabled.

    ``scoring._vetoes`` refuses a PERSON candidate when the document's own
    metadata names a *different* person — the "Raj Sharma at TERI vs Raj Sharma
    at IIT Delhi" guard. With corroboration always empty that veto could never
    fire, so a deliberate false-merge protection was dead corpus-wide. Measured
    over 1,500 real chunks, reviving it moved 13 mentions from AMBIGUOUS to
    UNRESOLVED, every one of them vetoed by this rule.
    """
    from types import SimpleNamespace

    from app.knowledge.candidates import context_for_document
    from app.knowledge.scoring import _vetoes

    mention = SimpleNamespace(entity_type="PERSON", normalized_text="arun kumar")
    candidate = SimpleNamespace(
        entity_type="PERSON", normalized_name="arun kumar", source="exact_name",
        is_ambiguous=False, autolink=True,
    )

    empty = context_for_document("doc-1", None)
    assert "v_cms_names_someone_else" not in _vetoes(mention, candidate, empty)

    names_someone_else = context_for_document(
        "doc-1", None, authors=["Dr Preeti Jain Das"]
    )
    assert "v_cms_names_someone_else" in _vetoes(
        mention, candidate, names_someone_else
    )

    # And the author themself is still linkable.
    names_them = context_for_document("doc-1", None, authors=["Arun Kumar"])
    assert "v_cms_names_someone_else" not in _vetoes(mention, candidate, names_them)


# --------------------------------------------------------------------------- #
# Stale CMS claims are retracted, not left active forever.
# --------------------------------------------------------------------------- #

def test_a_claim_the_source_no_longer_states_is_retracted(wired, monkeypatch):
    """`extract_cms.stale_claim_ids` existed, was tested, and was called by
    nothing. So a sponsor corrected away in the CMS left its claim `active` in
    the table for good — a full corpus run measured 17 of them, each refused by
    projection as `claim_entity_not_eligible` on every pass, silently."""
    monkeypatch.setattr(
        assertion_store, "all_staged",
        lambda: [_staged_row("c1"), _staged_row("gone")],
    )
    retracted: list = []
    monkeypatch.setattr(
        assertion_store, "retract",
        lambda ids: retracted.extend(ids) or len(ids),
    )
    # This run re-extracts only c1, so "gone" is no longer supported.
    monkeypatch.setattr(
        extract_cms, "extract_cms_claims", lambda index, limit=None: [_Assertion("c1")]
    )

    build, _ = _run(skip_project=True)
    assert retracted == ["gone"]
    assert _stage(build, "claims").counts["retracted"] == 1


def test_a_dry_run_retracts_nothing(wired, monkeypatch):
    monkeypatch.setattr(
        assertion_store, "all_staged",
        lambda: [_staged_row("c1"), _staged_row("gone")],
    )
    monkeypatch.setattr(
        extract_cms, "extract_cms_claims", lambda index, limit=None: [_Assertion("c1")]
    )
    _run(dry_run=True, skip_project=True)
    assert "retract" not in wired.calls


def test_a_claim_from_a_field_this_run_never_saw_is_left_alone(wired, monkeypatch):
    """Only (document, field) pairs the run covered may be judged, so a --limit
    run cannot retract claims it never looked at."""
    monkeypatch.setattr(
        assertion_store, "all_staged",
        lambda: [_staged_row("elsewhere", document_id="d99",
                             source_field="field_ongoing_sponsors")],
    )
    retracted: list = []
    monkeypatch.setattr(
        assertion_store, "retract",
        lambda ids: retracted.extend(ids) or len(ids),
    )
    _run(limit=1, skip_project=True)
    assert retracted == []


def test_a_rejected_but_still_stated_claim_is_never_retracted(wired, monkeypatch):
    """The distinction staleness turns on.

    A LED_BY claim whose principal investigator is a *provisional* person is
    refused by validation every run — 17 of them in the live corpus — but the
    CMS field still states it. Retracting it would assert the source had
    changed when only our eligibility rules apply. So the stale check is fed
    the full extraction, not the accepted subset: the rejected claim's id stays
    in the fresh set and protects it.
    """
    still_stated = _Assertion("ineligible", object_entity_id="person-provisional")
    monkeypatch.setattr(
        assertion_store, "all_staged",
        lambda: [_staged_row("c1"), _staged_row("ineligible")],
    )
    monkeypatch.setattr(
        extract_cms, "extract_cms_claims",
        lambda index, limit=None: [_Assertion("c1"), still_stated],
    )
    # Validation refuses it, exactly as the eligibility gate does in the corpus.
    monkeypatch.setattr(
        validate, "validate",
        lambda assertions, **kw: validate.ValidationResult(
            [a for a in assertions if a.claim_id != "ineligible"],
            [SimpleNamespace(code="object_not_claim_eligible", detail="",
                             assertion=still_stated)],
        ),
    )
    retracted: list = []
    monkeypatch.setattr(
        assertion_store, "retract",
        lambda ids: retracted.extend(ids) or len(ids),
    )

    build, _ = _run(skip_project=True)
    assert retracted == []
    assert _stage(build, "claims").counts["retracted"] == 0
