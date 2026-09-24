"""The payload contract between ingestion and retrieval/generation.

Ingestion decides what a Qdrant point carries; retrieval, the context builder and
the citation builder decide what they read off one. Nothing enforced that the two
lists agreed, so a field could be renamed or dropped on the write side and the
read side would carry on returning ``None`` — no error, no failing test, just a
citation with no title or a filter that matches nothing.

This file is that enforcement, in both directions:

* every payload key retrieval or generation **reads** is either written by
  ``app.ingestion.chunking.payload.build_payload`` or listed here as a documented
  optional with a safe default;
* every field the application **filters** on is in
  ``app.core.clients.vector_store.PAYLOAD_INDEXES``, so a filter cannot be added
  against an unindexed field (which Qdrant serves by full scan, correctly but
  slowly, and therefore silently).

Verified against the live corpus on 2026-08-18: the 13 declared indexes were
exactly the 13 present on the ``documents`` collection, and every key below was
present on sampled points at the coverage its writer implies.
"""
from __future__ import annotations

import pytest

from app.core.clients.vector_store import PAYLOAD_INDEXES

# Keys the read side takes off a payload. Grouped by who reads them so a failure
# names the reader that would break.
READ_BY_RETRIEVAL = {
    # hybrid_search.build_filter + Candidate
    "is_parent", "is_current", "section_type", "chunk_text", "parent_chunk_id",
    # understanding.filters facets
    "categories", "tags", "source_type", "language", "effective_start_date",
    # context_builder identity / dedup / conflict flagging
    "document_id", "pdf_id", "article_uuid", "linked_pdf_id",
    "linked_article_uuid", "page_number", "page_range", "overlap_page_range",
    # scoped_retrieval neighbour expansion
    "chunk_index",
}

READ_BY_GENERATION = {
    # prompts._source_hint
    "source_type", "title", "section_heading", "has_table", "effective_start_date",
    "doc_version",
    # citations._source_from_payload / _primary_url
    "source_url", "file_url", "page_number", "page_range",
}

# Read but deliberately not written by ingestion, each with a default that makes
# absence correct rather than merely survivable.
DOCUMENTED_OPTIONALS = {
    # reranker._authority: an operator override that ingestion never writes.
    # Absent means "use the source_type default", which is every point today.
    "source_authority",
    # The graph facts block's own markers. Set by app.retrieval.graph.facts on a
    # block that never came from Qdrant at all.
    "kind", "mode", "claim_ids", "entity_ids", "document_ids", "template_id",
    "disputed", "source",
}


def _written_keys() -> set[str]:
    """Every key ``build_payload`` can emit, read out of its source.

    Read from the source text rather than by building a Chunk, because the point
    of the test is the *declared* contract: a key that only appears under some
    branch of the chunk shape still has to be accounted for.
    """
    import inspect
    import re

    from app.ingestion.chunking import payload as writer

    source = inspect.getsource(writer.build_payload)
    return set(re.findall(r'"([a-z_]+)":', source)) | set(
        re.findall(r'payload\["([a-z_]+)"\]', source)
    )


def test_every_field_retrieval_reads_is_written_or_a_documented_optional():
    written = _written_keys()
    missing = READ_BY_RETRIEVAL - written - DOCUMENTED_OPTIONALS
    assert not missing, (
        "retrieval reads payload fields ingestion does not write: "
        f"{sorted(missing)}. Either ingestion stopped writing them or the read "
        "side is on a stale name; a silent None is not an acceptable outcome."
    )


def test_every_field_generation_reads_is_written_or_a_documented_optional():
    written = _written_keys()
    missing = READ_BY_GENERATION - written - DOCUMENTED_OPTIONALS
    assert not missing, (
        "generation reads payload fields ingestion does not write: "
        f"{sorted(missing)}"
    )


@pytest.mark.parametrize("field", sorted(PAYLOAD_INDEXES))
def test_every_indexed_field_is_actually_written(field):
    """An index on a field nothing writes is an index that can never match."""
    assert field in _written_keys(), (
        f"{field!r} is indexed but ingestion never writes it"
    )


# The fields whose *absence* is the removed model. Named individually so a
# reintroduction fails with the field's own name in the message.
@pytest.mark.parametrize(
    "field",
    ["tenant_id", "acl", "acl_groups", "allowed_groups", "term_ids", "theme_ids",
     "taxonomy", "term_uuids"],
)
def test_the_removed_access_and_taxonomy_fields_are_absent_end_to_end(field):
    """Neither written, nor indexed, nor filtered on.

    Document-level access control and the taxonomy were both removed. The
    corpus is public and every caller reads all of it, so a reappearance of any
    of these is a model being revived rather than a filter being added.
    """
    assert field not in _written_keys(), f"ingestion writes {field!r} again"
    assert field not in PAYLOAD_INDEXES, f"{field!r} is indexed again"
    assert field not in READ_BY_RETRIEVAL | READ_BY_GENERATION


def test_the_mandatory_filter_scopes_by_shape_only():
    """The only conditions every search carries are about which points are
    *searchable* — never about who is asking."""
    from app.retrieval.search.hybrid_search import build_filter

    built = build_filter()
    keys = {c.key for c in built.must if hasattr(c, "key")}
    assert keys == {"is_parent", "is_current"}
    must_not = {c.key for c in (built.must_not or []) if hasattr(c, "key")}
    assert must_not == {"section_type"}


def test_the_facet_filters_only_name_indexed_fields():
    """A facet filter on an unindexed field is served by full scan — correct, and
    silently slow. This is the check that keeps the two lists together."""
    from app.retrieval.understanding.query_processor import QueryAnalysis
    from app.retrieval.understanding.filters import _facet_filters

    analysis = QueryAnalysis(
        search_query="q", intent="qa", theme="Sustainable Agriculture",
        tags=["Nanocellulose"], source_type="website", language="en",
        date_from="2020-01-01", date_to="2024-01-01",
    )

    def _keys(condition):
        key = getattr(condition, "key", None)
        if isinstance(key, str):
            yield key
            return
        for branch in ("must", "should", "must_not"):
            for item in getattr(condition, branch, None) or []:
                yield from _keys(item)

    named = {k for c in _facet_filters(analysis) for k in _keys(c)}
    assert named, "the facet builder produced no conditions to check"
    assert named <= set(PAYLOAD_INDEXES), (
        f"facet filters name unindexed fields: {sorted(named - set(PAYLOAD_INDEXES))}"
    )
