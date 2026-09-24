from __future__ import annotations

import logging
import math
import re
from datetime import date
from functools import lru_cache
from typing import Any, Sequence

from app.config import get_settings
from app.core.models.context import ContextBlock, source_kind
from app.core.clients import get_qdrant_client
from app.observability import retrieval_log
from app.retrieval.search.hybrid_search import _NON_SEARCHABLE_SECTIONS, Candidate

logger = logging.getLogger(__name__)

_CHARS_PER_TOKEN = 4

__all__ = ["ContextBlock", "build_context"]


@lru_cache(maxsize=1)
def _encoder() -> Any:
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:  # pragma: no cover - offline
        return None


def _count_tokens(text: str) -> int:
    enc = _encoder()
    if enc is not None:
        return len(enc.encode(text))
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _ids(payload: dict[str, Any]) -> set[str]:
    return {
        str(payload[k])
        for k in ("document_id", "pdf_id", "article_uuid")
        if payload.get(k)
    }


def _links(payload: dict[str, Any]) -> set[str]:
    return {
        str(payload[k])
        for k in ("linked_pdf_id", "linked_article_uuid")
        if payload.get(k)
    }


def _linked(a: dict[str, Any], b: dict[str, Any]) -> bool:
    ia, ib = _ids(a), _ids(b)
    if ia & ib:
        return True
    return bool(ia & _links(b) or ib & _links(a))


def _fetch_parents(parent_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    ids = [pid for pid in dict.fromkeys(parent_ids) if pid]
    if not ids:
        return {}
    settings = get_settings()
    client = get_qdrant_client()
    with retrieval_log.qdrant_call(
        "retrieve",
        stage="parent_fetch",
        request=lambda: {
            "collection": settings.qdrant_collection,
            "ids": ids,
            "requested": len(ids),
        },
    ) as call:
        try:
            records = client.retrieve(
                collection_name=settings.qdrant_collection, ids=ids, with_payload=True
            )
        except Exception as exc:  # pragma: no cover - parent missing / store hiccup
            call.fail(exc)
            logger.exception("Parent fetch failed; falling back to child text.")
            return {}
        call.qdrant_results(records)
    return {str(r.id): (r.payload or {}) for r in records}


def _numbered(blocks: list[ContextBlock]) -> list[ContextBlock]:
    """The blocks in ranked order, numbered from 1.

    This used to be `_order_for_attention`, which for three or more blocks put
    the odd-ranked ones first and appended the even-ranked ones reversed —
    `[1,2,3,4,5]` came out `[1,3,5,4,2]`, landing the two best blocks at the two
    ends of the prompt. The shape matches the "lost in the middle" effect and
    nothing in the repo ever claimed more than that; the reference doc called it
    "inferred from the code's shape, not a claim backed by an in-repo comment".

    It is gone because it was speculative and the cost turned out to be
    measurable. It only ever ran on the minority path: `prefer_website_enabled`
    is on in production, so an ordinary question took the segregated build,
    which emitted its own order and never called this. Unifying that build put
    every question through it — and on "who is Ajay mathur?" the block ranked
    **2nd**, a 2023 brief saying the subject "was earlier ... at TERI as its
    Director General", was moved to display position 6, below a 2020 page
    ranked 6th that called him TERI's Director General in the present tense.
    The answer led with the 2020 page.

    Position is not neutral to a model reading a numbered list, and the ranked
    order now carries meaning it did not before: for a question about the
    present, rank encodes how recent the evidence is. Scrambling it discards
    exactly the signal the ranking was built to express. The strongest block is
    still first; the difference is that the second strongest is now second.
    """
    for i, block in enumerate(blocks, start=1):
        block.n = i
    return blocks


def _is_website(payload: dict[str, Any]) -> bool:
    return payload.get("source_type") == "website"


# Page fields that describe the *child* chunk specifically, and are therefore
# wrong for a block that carries its parent's text instead.
_CHILD_PAGE_FIELDS = ("page_number", "page_range", "overlap_page_range")


def _is_excluded(payload: dict[str, Any] | None) -> bool:
    """Whether this chunk is one of the non-substantive sections search drops.

    Shares ``hybrid_search``'s list rather than repeating it: the query filter
    and this check have to name the same sections, or the exclusion holds on the
    way in and leaks on the way out.
    """
    return bool(payload) and payload.get("section_type") in _NON_SEARCHABLE_SECTIONS


def _admissible_text(
    cand: Candidate, parents: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any] | None] | None:
    """The text this candidate contributes and the parent it came from, or None.

    Section exclusion has to be decided on the text that ends up in the block,
    not on the candidate that carried it. ``build_filter`` drops toc /
    references / glossary chunks from every search, but parent expansion then
    replaces the matched text wholesale — so a body child inside a bibliography
    window used to carry the whole bibliography past a filter that had already
    excluded it.

    Order of preference, each step falling to the next:

    1. the parent's text, when there is a parent and it is substantive;
    2. the child's own text, when *it* is substantive — this is both the orphan
       case and the excluded-parent case, where the child is the largest
       admissible passage available;
    3. nothing: neither is substantive, so the candidate contributes no context.

    An excluded child under a substantive parent still expands (case 1). The
    classifier reads content rather than headings, so a citation-dense run
    inside a findings section is a fragment of that section; the section is what
    the block carries, and it is admissible.
    """
    parent = parents.get(cand.parent_id or "") or None
    parent_text = (parent or {}).get("chunk_text") or ""
    if parent_text and not _is_excluded(parent):
        return parent_text, parent
    if _is_excluded(cand.payload):
        return None
    return cand.text, None


def _block_payload(
    child: dict[str, Any], parent: dict[str, Any] | None
) -> dict[str, Any]:
    """The child's payload, re-pointed at the pages of the text being shown.

    Identity stays the child's — the chunk that matched is the chunk the
    citation resolves to — but provenance has to follow the text. Parent
    expansion swaps in a passage spanning the whole parent window, and citing
    the child's single page for it claims a narrower source than the evidence:
    the reader is pointed at page 7 for a statement that may live on page 9.

    A parent that carries no ``page_range`` (an unpaginated source) leaves the
    block with no page at all, rather than keeping the child's. That is the only
    honest option — the alternative is stretching one page number over text it
    does not describe.
    """
    payload = dict(child)
    if parent is None:
        return payload
    for field_name in _CHILD_PAGE_FIELDS:
        payload.pop(field_name, None)
    span = parent.get("page_range")
    if isinstance(span, (list, tuple)) and len(span) == 2:
        payload["page_range"] = list(span)
        payload["page_number"] = span[0]
    return payload


def _same_document(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True when both payloads describe one and the same document."""
    return bool(_ids(a) & _ids(b))


def _same_source_two_formats(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True when the pair is a website node and its own attached PDF — the same
    content in two formats, not a genuine conflict.

    Compares the *normalized* kind, so a legacy ``article`` point pairs with its
    attachment exactly as a ``website`` one does; matching on the raw value let
    that pair through as a contradiction.
    """
    return {source_kind(a), source_kind(b)} == {"website", "pdf_attachment"} and _linked(a, b)


def _conflicting(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two blocks are sources that might contradict each other.

    A conflict is a disagreement *between sources*, so it takes two distinct
    documents. ``_ids`` unions ``document_id``/``pdf_id``/``article_uuid``, and
    an overlap on any of them means one document reached two ways — most often
    two sections of one report, since ``_admit`` deduplicates by parent rather
    than by document. Flagging those marked the majority of live answers as
    self-contradictory, which is both wrong and load-bearing: the flag reaches
    the API response and the prompt's "prefer the later page date" rule.

    Sharing a *parent node* is deliberately not a conflict. Editions of one
    publication do arrive that way — separate attachment documents under a
    single node — but so does every catalogue page, and on this corpus the
    latter dominates: the largest such nodes carry 69 financial statements, 68
    announcements, 43 brochures. The two shapes are indistinguishable from the
    payload (same node, same title, same ``effective_start_date``), so treating the
    relationship as a disagreement flagged roughly a quarter of answers, mostly
    wrongly. Separating them needs a content signal and a threshold measured
    against a labelled set, not guessed; until then the honest reading of two
    files on one page is that they are two files on one page.
    """
    if _same_document(a, b):
        return False
    if _same_source_two_formats(a, b):
        return False
    return _linked(a, b)


def _admit(
    candidates: Sequence[Candidate],
    *,
    blocks: list[ContextBlock],
    block_vectors: list[list[float]],
    seen_parents: set[str],
    parents: dict[str, dict[str, Any]],
    spent: int,
    limit: int,
    token_budget: int,
    sim_threshold: float,
    max_add: int | None = None,
    floor: float | None = None,
) -> int:
    """Walk candidates in order, admitting each that survives parent-expand, dedup
    and the token budget, until `max_add` are added or `limit` total blocks is
    reached. Mutates blocks/block_vectors/seen_parents in place; returns the
    updated token spend. `floor` skips candidates below a raw-semantic relevance
    bar (used for the website slots)."""
    added = 0
    for cand in candidates:
        if len(blocks) >= limit or (max_add is not None and added >= max_add):
            break
        if floor is not None and cand.semantic_score < floor:
            continue
        key = cand.parent_id or cand.id
        if key in seen_parents:
            continue

        duplicate_of = None
        for kept, kvec in zip(blocks, block_vectors):
            if cand.vector and kvec and _cosine(cand.vector, kvec) >= sim_threshold:
                duplicate_of = kept
                break
        if duplicate_of is not None:
            seen_parents.add(key)
            if _linked(cand.payload, duplicate_of.payload):
                duplicate_of.also_available.append(dict(cand.payload))
            continue

        # Which text won decides whose provenance the block carries, so the two
        # are resolved together and never separately.
        admissible = _admissible_text(cand, parents)
        if admissible is None:
            continue
        text, parent_payload = admissible
        if not text.strip():
            continue
        cost = _count_tokens(text)
        if blocks and spent + cost > token_budget:
            continue
        seen_parents.add(key)
        spent += cost

        blocks.append(
            ContextBlock(
                n=len(blocks) + 1,
                text=text,
                payload=_block_payload(cand.payload, parent_payload),
                score=cand.score,
            )
        )
        block_vectors.append(list(cand.vector))
        added += 1
    return spent


def build_context(
    candidates: Sequence[Candidate],
    *,
    limit: int | None = None,
    token_budget: int | None = None,
    temporal: Any | None = None,
    question: str = "",
) -> list[ContextBlock]:
    """The evidence for one answer: one ranked set, whatever it is made of.

    Website pages, PDF attachments and the graph's facts block are admitted from
    a single ordered walk of `candidates`, which arrive in the reranker's order.
    Source kind gets no budget, no floor and no reserved position here; it is
    already represented in that order, as one term of the authority band, which
    sits below relevance, temporal fit and — on a question about the present —
    recency. A newer PDF can therefore lead an older web page, which is the
    point.

    What this replaced, and why. Until now a "segregated" build admitted website
    candidates first under their own cap and relevance floor, then let PDFs
    compete for two remaining slots plus a third gated on a high-confidence bar,
    and emitted them in that order regardless of rank. Two consequences made it
    untenable. The ordering was categorical rather than evidential, so a 2020
    announcement led a 2023 brief that corrected it; and the graph's facts block
    carries no ``source_type``, so it fell into the "not website" bucket and
    competed with PDF attachments for those two slots.
    """
    settings = get_settings()
    limit = limit or settings.retrieval_top_k
    token_budget = token_budget or settings.context_token_budget
    sim_threshold = settings.dedup_cosine_threshold
    if not candidates:
        return []

    parents = _fetch_parents([c.parent_id for c in candidates if c.parent_id])

    blocks: list[ContextBlock] = []
    block_vectors: list[list[float]] = []
    seen_parents: set[str] = set()
    spent = 0

    _admit(
        candidates, blocks=blocks, block_vectors=block_vectors,
        seen_parents=seen_parents, parents=parents, spent=spent, limit=limit,
        token_budget=token_budget, sim_threshold=sim_threshold,
    )
    ordered = _numbered(blocks)

    _flag_conflicts(ordered)
    flag_supersession(ordered, temporal, question)
    return ordered


def _flag_conflicts(blocks: list[ContextBlock]) -> None:
    for i, a in enumerate(blocks):
        for b in blocks[i + 1 :]:
            if _conflicting(a.payload, b.payload):
                a.conflict = b.conflict = True


# --------------------------------------------------------------------------- #
# Supersession: the same fact, stated at two times
# --------------------------------------------------------------------------- #
# `_conflicting` above asks whether two blocks are the kind of sources that
# might disagree — a question about provenance, answered from the payload. This
# asks a narrower and more useful one: does a newer block say, in so many words,
# that what an older block states is no longer the case?
#
# The case it was built for: a 2020 page headed "Statement by Dr Ajay Mathur,
# Director General, TERI" and a 2023 brief saying he "was earlier ... at TERI as
# its Director General". Those are not two independent facts. They are one
# relationship — person, role, organisation — recorded at two times, and only the
# newer one describes the present. Nothing compared them, so an answer to "who is
# the director general" could be assembled from the older.
#
# Deliberately conservative, because a false positive tells the model to discount
# good evidence. Three conditions, all required:
#
#   1. the question is about the present. For any other question the older
#      statement may be exactly what was asked for, and this does not run;
#   2. the newer block is newer by a wide margin — a year, the same span the
#      reranker's recency band uses, so two write-ups of one season never
#      qualify;
#   3. the newer block frames a role or affiliation in the *past*, and the two
#      blocks are about the same named subject.
#
# What it does not do: it does not read *which* role, or *whose*. Establishing
# that the past-framed role in the newer block is the same role the older block
# asserts needs relation extraction, and doing it with regular expressions would
# produce a confident wrong answer more often than a useful one. So the flag
# claims only what it can support — "a newer source describes this subject's
# affiliations in the past tense" — and the prompt is worded to match. The
# authoritative version of this comparison is the claim graph's `valid_from` /
# `valid_until` ladder; this is the document-evidence fallback for the very
# common case where the graph holds no claim.

#: Wording that puts a role, post or affiliation in the past.
_PAST_ROLE_FRAMING = re.compile(
    r"\bwas\s+(?:earlier|previously|formerly|then|the|a|an)\b"
    r"|\bhad\s+(?:been|previously)\b|\bused\s+to\s+be\b"
    r"|\bformer(?:ly)?\b|\bpreviously\b|\bearlier\s+(?:at|in|with|served|worked)\b"
    r"|\bbefore\s+joining\b|\bprior\s+to\s+joining\b"
    r"|\bstepped\s+down\b|\bsucceeded\s+(?:by|him|her|them)\b"
    r"|\bserved\s+as\b|\buntil\s+(?:19|20)\d{2}\b|\bex-[A-Za-z]",
    re.IGNORECASE,
)

#: How far from a subject mention the past-framing phrase has to sit. A block
#: runs to a few thousand characters and says many things; "previously" three
#: paragraphs away from the only mention of the subject is about something else.
#: Generous enough for the sentence that motivated it — "Dr X is the Director
#: General of the ISA. He was earlier in BEE and at TERI as its Director
#: General" — and no more.
_FRAMING_WINDOW = 400

#: How much newer the correcting source must be.
#:
#: Six months, not the year the reranker's recency band uses. The two are
#: guarding different things and were briefly given the same number for a
#: symmetry that does not hold. The band asks "is this materially newer
#: evidence about the present", where a year is a reasonable floor for a fact
#: that might have changed. This asks only "are these two write-ups of the same
#: moment", because the *substance* of the claim is already carried by the past
#: framing and the shared subject — a newer source that says in so many words
#: that a role has ended is not made less credible by being nine months newer
#: rather than thirteen.
#:
#: The case that forced the distinction: a 2020-07-06 webinar page and a
#: 2021-03-05 announcement that the subject had been elected elsewhere and that
#: a transition plan was in place, 242 days apart.
_SUPERSESSION_MIN_GAP_DAYS = 180

def _subject_terms(question: str) -> list[str]:
    """What the question is about, lowercased; ``[]`` when it names nothing.

    `strategies.extract_key_terms` already answers this for the keyword leg —
    quoted phrases, proper-noun bigrams, acronyms, codes and years, falling back
    to content words — so this is an accessor over it rather than a second
    opinion about what a question names.
    """
    from app.retrieval.search.strategies import extract_key_terms

    return [t.lower() for t in (extract_key_terms(question) or []) if t.strip()]


def _about(text: str, terms: list[str]) -> bool:
    """Whether this passage mentions everything the question named."""
    lowered = (text or "").lower()
    return all(term in lowered for term in terms)


def _frames_the_subject_as_past(text: str, terms: list[str]) -> bool:
    """Whether a past-role phrase sits near a mention of the question's subject.

    Proximity is the whole of the precision here. Asked to find "was earlier" or
    "formerly" anywhere in a multi-page block, a corpus of institutional prose
    obliges almost every time; asked to find one within a few hundred characters
    of the person being asked about, it does not.
    """
    lowered = (text or "").lower()
    positions = [
        m.start() for term in terms for m in re.finditer(re.escape(term), lowered)
    ]
    if not positions:
        return False
    return any(
        any(abs(m.start() - p) <= _FRAMING_WINDOW for p in positions)
        for m in _PAST_ROLE_FRAMING.finditer(text or "")
    )


def _block_date(payload: dict[str, Any]) -> date | None:
    from app.retrieval.search.temporal_gate import _as_date

    return _as_date(payload.get("effective_start_date"))


def flag_supersession(
    blocks: list[ContextBlock], temporal: Any | None, question: str = ""
) -> None:
    """Mark older blocks a newer one dates, for questions about the present.

    A no-op for every question that is not CURRENT, for every question that names
    no subject, and for every context where no pair clears the conditions above —
    which is almost all of them. Never raises: a comparison problem must cost the
    flag, never the answer.

    The subject comes from the *question*, not from what the two blocks happen to
    have in common. Pairwise overlap was the first attempt and it was far too
    loose: a block runs to thousands of characters, so any two of them on a
    single-organisation corpus share several capitalised words, and replaying the
    reported query flagged a 2019 building-retrofit PDF as superseded by a 2021
    announcement about a person it never mentions. Anchoring on what was asked
    about is both tighter and closer to the point — supersession only matters for
    the thing the question is about.
    """
    try:
        from app.retrieval.search.temporal_gate import CURRENT

        if temporal is None or getattr(temporal, "mode", None) != CURRENT:
            return
        terms = _subject_terms(question)
        if not terms:
            return
        dated = [
            (b, d) for b, d in ((b, _block_date(b.payload)) for b in blocks)
            if d is not None and _about(b.text, terms)
        ]
        if len(dated) < 2:
            return
        for older, older_date in dated:
            for newer, newer_date in dated:
                if newer is older:
                    continue
                if (newer_date - older_date).days < _SUPERSESSION_MIN_GAP_DAYS:
                    continue
                if not _frames_the_subject_as_past(newer.text, terms):
                    continue
                older.superseded = True
                newer.supersedes = True
                logger.info(
                    "Block %d (%s) is dated by the newer block %d (%s); subject %s.",
                    older.n, older_date, newer.n, newer_date, terms,
                )
    except Exception:  # pragma: no cover - defence in depth
        logger.warning("Supersession comparison failed; context unflagged.",
                       exc_info=True)
