"""A document that changes bundle has moved, not disappeared.

Reconciliation decides what is gone by comparing a bundle's live set against a
catalog snapshot taken once, at the start of the run. A document that moves
bundles is missing from its old bundle's live set, so that snapshot marks it for
deletion — and if the new bundle was crawled earlier in the same run it has
already been re-indexed by then, so the delete takes a live document straight
back out. It reappears on the next sweep, which means an hour of absence for
something that never left the site.

Each candidate is therefore confirmed against the catalog as it stands at that
moment. These tests drive the real crawl and the real `_handle` over an
in-memory catalog and index, and assert on what is emitted and on the state left
behind — not on a helper's return value.

No MySQL, no Qdrant, no HTTP.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.catalog.models import StateRecord
from app.ingestion import pipeline
from app.ingestion.change_detection import ChangeStatus, drupal
from app.ingestion.extractors import drupal_extractor as de


class _World:
    """A site, the catalog built from it, and the index behind that."""

    def __init__(self) -> None:
        self.live: dict[str, list[str]] = {}
        self.catalog: dict[str, StateRecord] = {}
        self.points: set[str] = set()
        self.emitted: list[tuple[str, str]] = []
        self.reads: list[str] = []
        self.read_fails = False
        self.enumeration_empty = False

    def catalogue(self, uuid: str, bundle: str) -> None:
        self.catalog[uuid] = StateRecord(
            document_id=uuid, source_type="website", source_key=f"/{uuid}",
            fingerprint="fp-old", content_hash="h", doc_version=1, bundle=bundle,
            entity_type="node", changed_mark=1785000000,
        )
        self.points.add(uuid)

    def publish(self, bundle: str, *uuids: str) -> None:
        self.live.setdefault(bundle, []).extend(uuids)

    def live_in(self, bundle: str) -> list[str]:
        return [] if self.enumeration_empty else list(self.live.get(bundle, []))

    def get(self, uuid: str) -> StateRecord | None:
        if self.read_fails:
            raise RuntimeError("catalog unreachable")
        self.reads.append(uuid)
        return self.catalog.get(uuid)

    def searchable(self) -> set[str]:
        return {d for d in self.catalog if d in self.points}


@pytest.fixture
def world(monkeypatch):
    site = _World()
    settings = SimpleNamespace(
        drupal_max_retries=1, drupal_block_min_chars=200,
        ingest_reconcile_max_missing_ratio=0.10, ingest_reconcile_min_deletions=2,
    )
    site.settings = settings

    monkeypatch.setattr(drupal, "get_settings", lambda: settings)
    # Read once per run, exactly as production does — this staleness is the
    # whole reason the per-candidate confirmation exists.
    monkeypatch.setattr(
        drupal.state, "load",
        lambda source_type: (dict(site.catalog) if source_type == "website" else {}),
    )
    monkeypatch.setattr(drupal.state, "get", site.get)
    monkeypatch.setattr(drupal.dead_links, "ensure_table", lambda: None)
    monkeypatch.setattr(drupal.dead_links, "load", dict)
    monkeypatch.setattr(drupal.retries, "ensure_table", lambda: None)
    monkeypatch.setattr(drupal.retries, "floors", dict)
    monkeypatch.setattr(de, "_build_session", lambda retries: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        de, "iter_bundle_records",
        lambda s, b, **kw: iter([
            SimpleNamespace(
                uuid=u, bundle=b, changed="2026-08-02T00:00:00+00:00",
                source=f"/{u}", body="Body text, long enough to pass.", files=[],
            )
            for u in site.live_in(b)
        ]),
    )
    monkeypatch.setattr(de, "iter_node_uuids", lambda s, b, **kw: iter(site.live_in(b)))

    def save_state(record, doc, content_hash, version, indexed=True, **_kwargs):
        site.catalogue(record.document_id, record.bundle)

    monkeypatch.setattr(pipeline, "_save_state", save_state)
    monkeypatch.setattr(pipeline, "_log", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_enrich", lambda doc, content_hash: "off")
    monkeypatch.setattr(pipeline.state, "attachment_ids_for", lambda doc_id: [])
    monkeypatch.setattr(pipeline.state, "delete", lambda ids: [site.catalog.pop(i, None) for i in ids])
    monkeypatch.setattr(pipeline, "delete_document", lambda doc_id, keep_ids=None: site.points.discard(doc_id))
    # One real chunk: a document that chunks to nothing is an error outcome now
    # (tests/test_empty_extraction.py), and a moved document must still be
    # indexed under its new bundle for these tests to mean anything.
    monkeypatch.setattr(
        pipeline, "chunk_canonical",
        lambda doc: [SimpleNamespace(
            chunk_id=f"{doc.document_id}-c1", text="Body text.", is_parent=False
        )],
    )
    monkeypatch.setattr(pipeline, "index_chunks", lambda chunks: len(chunks))
    return site


def _sweep(world, bundles: list[str]) -> list[str]:
    """One full run — crawl and reconcile — applying every record as it lands.
    Returns the ids actually deleted."""
    deleted: list[str] = []
    for record in drupal.detect_drupal_changes(bundles=bundles, reconcile_deletes=True):
        world.emitted.append((record.document_id, record.status.value))
        pipeline._handle(
            record,
            build_doc=lambda r: SimpleNamespace(
                document_id=r.document_id, file_links=[], doc_version=1,
                title="t", tags=[], ensure_content_hash=lambda: "h2",
                # A real CanonicalDocument always carries a date, even when it
                # is None; the pipeline reports an undated one rather than
                # assuming the attribute is there.
                effective_start_date="2026-01-01T00:00:00+00:00",
            ),
        )
        if record.status is ChangeStatus.DELETED:
            deleted.append(record.document_id)
    return deleted


def _populate(world) -> None:
    """Two bundles with enough documents that one deletion is plausible."""
    for uuid in ("n-1", "n-2", "n-3"):
        world.publish("news", uuid)
        world.catalogue(uuid, "news")
    for uuid in ("e-1", "e-2", "e-3"):
        world.publish("events", uuid)
        world.catalogue(uuid, "events")


# --------------------------------------------------------------------------- #
# The move itself.
# --------------------------------------------------------------------------- #

def test_a_moved_document_survives_when_its_new_bundle_is_crawled_first(world):
    """The case that lost documents: 'events' is crawled first and re-indexes
    the mover, then 'news' reconciles against a snapshot that still calls it a
    news document."""
    _populate(world)
    world.catalogue("mover", "news")          # catalogued under the old bundle
    world.publish("events", "mover")          # live under the new one

    deleted = _sweep(world, ["events", "news"])

    assert "mover" not in deleted, "a document that moved must not be deleted"
    assert "mover" in world.searchable(), "and must still be indexed"
    assert world.catalog["mover"].bundle == "events", "under its new bundle"


def test_a_moved_document_ends_up_in_its_new_bundle_when_the_old_one_is_first(world):
    """The other order. The delete still happens — at that moment the catalog
    genuinely still files it under 'news' — but the mover is re-indexed under
    'events' later in the same run, so the run ends with it present."""
    _populate(world)
    world.catalogue("mover", "news")
    world.publish("events", "mover")

    _sweep(world, ["news", "events"])

    assert "mover" in world.searchable()
    assert world.catalog["mover"].bundle == "events"


def test_a_moved_document_is_untouched_by_the_following_sweep(world):
    _populate(world)
    world.catalogue("mover", "news")
    world.publish("events", "mover")
    _sweep(world, ["events", "news"])

    world.emitted.clear()
    deleted = _sweep(world, ["events", "news"])

    assert deleted == []
    assert "mover" in world.searchable()


# --------------------------------------------------------------------------- #
# Everything else about deletion is unchanged.
# --------------------------------------------------------------------------- #

def test_a_genuinely_deleted_document_is_still_deleted(world):
    _populate(world)
    world.catalogue("gone", "news")           # catalogued, but live nowhere

    deleted = _sweep(world, ["news", "events"])

    assert deleted == ["gone"]
    assert "gone" not in world.catalog
    assert "gone" not in world.points, "its vectors go too"


def test_documents_that_stayed_put_are_untouched(world):
    _populate(world)

    deleted = _sweep(world, ["news", "events"])

    assert deleted == []
    assert world.searchable() == {"n-1", "n-2", "n-3", "e-1", "e-2", "e-3"}


def test_lookalike_ids_across_bundles_do_not_interact(world):
    for uuid in ("doc-1", "doc-10", "doc-100"):
        world.publish("news", uuid)
        world.catalogue(uuid, "news")
    for uuid in ("doc-1x", "doc-1000"):
        world.publish("events", uuid)
        world.catalogue(uuid, "events")

    deleted = _sweep(world, ["news", "events"])

    assert deleted == []
    assert len(world.searchable()) == 5


# --------------------------------------------------------------------------- #
# Failing to confirm is not a licence to delete.
# --------------------------------------------------------------------------- #

def test_a_failed_confirmation_skips_the_deletion(world):
    _populate(world)
    world.catalogue("gone", "news")
    world.read_fails = True

    deleted = _sweep(world, ["news", "events"])

    assert deleted == [], "an unreadable catalog must not authorise a delete"
    assert "gone" in world.searchable()


# --------------------------------------------------------------------------- #
# The completeness guard still owns the batch decision.
# --------------------------------------------------------------------------- #

def test_the_guard_still_refuses_an_empty_enumeration(world):
    """The per-candidate check only ever removes candidates from a batch the
    guard already approved, so it cannot weaken the guard."""
    _populate(world)
    world.enumeration_empty = True

    deleted = _sweep(world, ["news", "events"])

    assert deleted == []
    assert len(world.searchable()) == 6


def test_the_guard_runs_before_any_candidate_is_confirmed(world):
    """Ordering, asserted directly: a refused bundle costs zero catalog reads."""
    _populate(world)
    world.enumeration_empty = True

    _sweep(world, ["news", "events"])

    assert world.reads == [], "the guard short-circuits before the confirmations"


def test_one_catalog_read_per_delete_candidate(world):
    """The cost of the protection, pinned: one read per candidate, and none for
    a document that was never a candidate."""
    _populate(world)
    world.catalogue("gone-1", "news")
    world.catalogue("gone-2", "news")

    _sweep(world, ["news", "events"])

    assert world.reads == ["gone-1", "gone-2"]
