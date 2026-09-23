"""Context expansion is well-defined whether or not a child has a parent.

The chunker deliberately emits no parent for a single-child window (see
tests/test_chunk_orphans.py). These tests drive the real `build_context` path to
pin what that means downstream: an orphan expands to its own text, a
parent-backed child expands to the parent, and neither leaks into the other.

Only `_fetch_parents` is stubbed — it is the one call that would reach Qdrant.
"""

from __future__ import annotations

import pytest

from app.retrieval.context import builder as context_builder
from app.retrieval.context.builder import build_context
from app.retrieval.search.hybrid_search import Candidate

ORPHAN_TEXT = "Alpha: the single-child section body, which is the whole window."
CHILD_TEXT = "Bravo: one child of a section that has several."
PARENT_TEXT = (
    "Bravo section heading. Bravo: one child of a section that has several. "
    "Bravo: a sibling child. Bravo: a third child, none of which stands alone."
)


def _cand(cid: str, text: str, *, parent: str | None = None, section: str | None = None):
    payload = {
        "chunk_id": cid,
        "document_id": "doc1",
        "source_type": "pdf_attachment",
        "chunk_text": text,
    }
    if parent:
        payload["parent_chunk_id"] = parent
    if section:
        payload["section_heading"] = section
    # Vectors left empty so the cosine near-dup path stays out of the way; these
    # tests are about parent-key expansion and dedup.
    return Candidate(id=cid, score=0.9, payload=payload, semantic_score=0.9)


@pytest.fixture
def parents(monkeypatch):
    """Stub the one call that would hit the vector store."""
    store = {
        "p-bravo": {"chunk_id": "p-bravo", "chunk_text": PARENT_TEXT, "is_parent": True},
        "p-charlie": {"chunk_id": "p-charlie", "chunk_text": "Charlie parent body.", "is_parent": True},
        "p-delta": {"chunk_id": "p-delta", "chunk_text": "Delta parent body.", "is_parent": True},
    }
    monkeypatch.setattr(
        context_builder, "_fetch_parents",
        lambda ids: {pid: store[pid] for pid in dict.fromkeys(ids) if pid in store},
    )
    return store


# --- 1. an orphan returns its own text -------------------------------------- #

def test_orphan_expands_to_its_own_text(parents):
    blocks = build_context([_cand("c-alpha", ORPHAN_TEXT)], limit=6, token_budget=9000)
    assert len(blocks) == 1
    assert blocks[0].text == ORPHAN_TEXT
    assert blocks[0].payload["chunk_id"] == "c-alpha"


def test_orphan_does_not_trigger_a_parent_fetch(monkeypatch):
    """No parent id means nothing to look up."""
    asked: list[list[str]] = []

    def spy(ids):
        asked.append(list(ids))
        return {}

    monkeypatch.setattr(context_builder, "_fetch_parents", spy)
    blocks = build_context([_cand("c-alpha", ORPHAN_TEXT)], limit=6, token_budget=9000)
    assert asked == [[]]
    assert blocks[0].text == ORPHAN_TEXT


# --- 2. a parent-backed child expands to the parent ------------------------- #

def test_child_with_a_parent_expands_to_the_parent_text(parents):
    blocks = build_context(
        [_cand("c-bravo", CHILD_TEXT, parent="p-bravo")], limit=6, token_budget=9000
    )
    assert len(blocks) == 1
    assert blocks[0].text == PARENT_TEXT
    # The block keeps the CHILD's payload, so citations still point at the hit.
    assert blocks[0].payload["chunk_id"] == "c-bravo"


def test_parent_expansion_is_strictly_more_than_the_child(parents):
    blocks = build_context(
        [_cand("c-bravo", CHILD_TEXT, parent="p-bravo")], limit=6, token_budget=9000
    )
    assert CHILD_TEXT in blocks[0].text
    assert len(blocks[0].text) > len(CHILD_TEXT)


def test_missing_parent_falls_back_to_child_text(parents):
    """A dangling parent id must not empty the block."""
    blocks = build_context(
        [_cand("c-bravo", CHILD_TEXT, parent="p-does-not-exist")],
        limit=6, token_budget=9000,
    )
    assert blocks[0].text == CHILD_TEXT


# --- 3. both shapes are usable side by side --------------------------------- #

def test_orphan_and_parent_backed_child_are_both_usable(parents):
    blocks = build_context(
        [_cand("c-alpha", ORPHAN_TEXT), _cand("c-bravo", CHILD_TEXT, parent="p-bravo")],
        limit=6, token_budget=9000,
    )
    assert len(blocks) == 2
    texts = [b.text for b in blocks]
    assert ORPHAN_TEXT in texts
    assert PARENT_TEXT in texts
    assert all(b.text.strip() for b in blocks)
    assert [b.n for b in blocks] == [1, 2]


# --- 4. siblings collapse to one block -------------------------------------- #

def test_two_children_of_one_parent_yield_a_single_block(parents):
    blocks = build_context(
        [
            _cand("c-bravo-1", CHILD_TEXT, parent="p-bravo"),
            _cand("c-bravo-2", "Bravo: a sibling child.", parent="p-bravo"),
        ],
        limit=6, token_budget=9000,
    )
    assert len(blocks) == 1
    assert blocks[0].text == PARENT_TEXT
    assert blocks[0].text.count("Bravo section heading") == 1


def test_orphans_are_not_collapsed_with_each_other(parents):
    """Distinct orphans key on their own ids, so they must stay distinct."""
    blocks = build_context(
        [_cand("c-alpha-1", "First orphan body."), _cand("c-alpha-2", "Second orphan body.")],
        limit=6, token_budget=9000,
    )
    assert [b.text for b in blocks] == ["First orphan body.", "Second orphan body."]


# --- 5. no cross-section leakage -------------------------------------------- #

def test_children_from_different_sections_do_not_leak(parents):
    blocks = build_context(
        [
            _cand("c-bravo", CHILD_TEXT, parent="p-bravo", section="B"),
            _cand("c-charlie", "Charlie child.", parent="p-charlie", section="C"),
            _cand("c-delta", "Delta child.", parent="p-delta", section="D"),
        ],
        limit=6, token_budget=9000,
    )
    assert len(blocks) == 3
    texts = {b.payload["chunk_id"]: b.text for b in blocks}
    assert texts["c-bravo"] == PARENT_TEXT
    assert texts["c-charlie"] == "Charlie parent body."
    assert texts["c-delta"] == "Delta parent body."
    # Each block holds one section's content and no other's.
    assert "Charlie" not in texts["c-bravo"] and "Delta" not in texts["c-bravo"]
    assert "Bravo" not in texts["c-charlie"] and "Delta" not in texts["c-charlie"]


def test_a_mixed_set_keeps_every_section_represented(parents):
    blocks = build_context(
        [
            _cand("c-alpha", ORPHAN_TEXT, section="A"),
            _cand("c-bravo-1", CHILD_TEXT, parent="p-bravo", section="B"),
            _cand("c-bravo-2", "Bravo: a sibling child.", parent="p-bravo", section="B"),
            _cand("c-charlie", "Charlie child.", parent="p-charlie", section="C"),
        ],
        limit=6, token_budget=9000,
    )
    # Three sections in, three blocks out — the Bravo siblings share one.
    # Blocks come out in ranked order, numbered from 1 (`_numbered`).
    assert len(blocks) == 3
    assert {b.payload["chunk_id"] for b in blocks} == {"c-alpha", "c-bravo-1", "c-charlie"}
    assert [b.n for b in blocks] == [1, 2, 3]
    by_id = {b.payload["chunk_id"]: b.text for b in blocks}
    assert by_id["c-alpha"] == ORPHAN_TEXT          # orphan kept its own text
    assert by_id["c-bravo-1"] == PARENT_TEXT        # siblings collapsed to the parent
    assert by_id["c-charlie"] == "Charlie parent body."
