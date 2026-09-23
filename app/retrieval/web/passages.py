"""Web documents as retrievable passages, on the same footing as corpus chunks.

A :class:`~app.retrieval.web.extract.WebDocument` is cut into passages and each
passage becomes a :class:`~app.retrieval.search.hybrid_search.Candidate` — the
type every ranking and context-building step already takes — so web evidence
is reranked, de-duplicated, budgeted and cited by exactly the machinery the
corpus goes through, and never by a parallel copy of it.

Parent and child, as in the corpus
----------------------------------
A passage (the *child*) is a paragraph or two: small enough that a question
about one figure matches the passage that states it, and that "find the
paragraph about X" can point at the paragraph. Its *context* is the run of the
same section around it, up to ``_CONTEXT_WORDS``, carried on the payload as
``context_text`` — what the model is shown, so a figure arrives with the
sentences that qualify it. That is the corpus's parent expansion, done here
because a web page has no parent chunk in Qdrant to fetch.

The same relevance scale
------------------------
A passage is embedded with the corpus's embedding model, and with the same
``title › heading`` breadcrumb ingestion prefixes to a child's embedded text
(``app.ingestion.chunking._breadcrumb``), and scored by cosine against the
question's own vector. So ``semantic_score`` means for a web passage what it
means for a corpus chunk, and the reranker's relevance bands and thresholds
apply to both without translation. The stored ``chunk_text`` stays the plain
passage, as it does in the corpus: citations quote it.

Provenance
----------
Every candidate's payload says where it came from and when: the URL and domain,
whether the site is the organisation's own, the title and authors, the date the
document states *and where that date was read*, when it was retrieved, the
search that found it and at what rank, and the page and section for a PDF. See
:func:`_payload` for the full contract.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Callable, Sequence

from app.config import get_settings
from app.core.dates import stated_day
from app.core.models.context import WEB_SOURCE_TYPE
from app.retrieval.search.hybrid_search import Candidate
from app.retrieval.search.strategies import extract_content_terms
from app.retrieval.web.extract import PARAGRAPH, TABLE, WebDocument
from app.retrieval.web.providers import SearchHit
from app.retrieval.web.safety import DomainPolicy, policy, url_key

__all__ = ["Passage", "split", "to_candidates"]

# Passage sizes in words — about 1.35 tokens each on this corpus's prose. A child
# aims at a paragraph or two and never exceeds the corpus's own child size; its
# context is roughly a third of a corpus parent, since several web documents
# share one context budget.
_TARGET_WORDS = 180
_MAX_WORDS = 300
_MIN_WORDS = 8           # below this a fragment ("9 April 2025", "Share") is not evidence
_CONTEXT_WORDS = 600
_MAX_TABLE_CHARS = 3000
# The breadcrumb cap, as ingestion's 32-token cap in words.
_BREADCRUMB_WORDS = 24
_BREADCRUMB_SEP = " › "
# Passages per document that are embedded at all. A long PDF yields hundreds;
# beyond this the ones sharing most words with the question are kept.
_MAX_EMBEDDED_PER_DOCUMENT = 60

_SENTENCE = re.compile(r"(?<=[.!?])\s+")

Embedder = Callable[[list[str]], list[list[float]]]


@dataclass(frozen=True)
class Passage:
    """One retrievable unit of a document, and where in it it sits."""

    text: str
    heading: str | None
    kind: str
    index: int              # position among the document's passages
    section: int            # ordinal of the section (run of one heading) it belongs to
    page_start: int | None = None
    page_end: int | None = None


def _words(text: str) -> int:
    return len(text.split())


def _is_fragment(text: str) -> bool:
    """A block that is page furniture even inside the content — a lone word
    ("Share", "Print") or a bare date line — and must not be merged into the
    passage beside it. Short content ("Fleet modernisation") is merged, since a
    list's items say something together."""
    words = text.split()
    return len(words) < 4 and (len(words) <= 1 or stated_day(text) is not None)


def _sentence_windows(text: str, max_words: int) -> list[str]:
    """A long paragraph cut at sentence ends into windows of at most
    ``max_words``; a single sentence longer than that is kept whole."""
    windows: list[str] = []
    current: list[str] = []
    size = 0
    for sentence in _SENTENCE.split(text):
        n = _words(sentence)
        if current and size + n > max_words:
            windows.append(" ".join(current))
            current, size = [], 0
        current.append(sentence)
        size += n
    if current:
        windows.append(" ".join(current))
    return windows


def split(document: WebDocument) -> list[Passage]:
    """The document's passages, in reading order.

    Consecutive paragraphs under one heading are joined until they reach
    ``_TARGET_WORDS``; a paragraph over ``_MAX_WORDS`` is cut at sentence ends.
    A table is always its own passage, so its rows are never split from their
    header. A heading change always closes the running passage — a passage
    never straddles two sections.
    """
    passages: list[Passage] = []
    parts: list[str] = []
    pages: list[int] = []
    size = 0
    section = -1
    heading_key: object = object()

    def emit(text: str, heading: str | None, kind: str, page_list: list[int]) -> None:
        if kind != TABLE and _words(text) < _MIN_WORDS:
            return
        passages.append(Passage(
            text=text, heading=heading, kind=kind, index=len(passages), section=section,
            page_start=min(page_list) if page_list else None,
            page_end=max(page_list) if page_list else None,
        ))

    def flush(heading: str | None) -> None:
        nonlocal size
        if parts:
            emit(" ".join(parts), heading, PARAGRAPH, pages[:])
        parts.clear()
        pages.clear()
        size = 0

    current_heading: str | None = None
    for block in document.blocks:
        if block.heading != heading_key:
            flush(current_heading)
            heading_key, current_heading = block.heading, block.heading
            section += 1
        page = [block.page] if block.page is not None else []
        if block.kind == TABLE:
            flush(current_heading)
            emit(block.text[:_MAX_TABLE_CHARS], current_heading, TABLE, page)
            continue
        text = block.text
        if _is_fragment(text):
            continue
        for window in (_sentence_windows(text, _MAX_WORDS)
                       if _words(text) > _MAX_WORDS else [text]):
            n = _words(window)
            if parts and size + n > _MAX_WORDS:
                flush(current_heading)
            parts.append(window)
            pages.extend(page)
            size += n
            if size >= _TARGET_WORDS:
                flush(current_heading)
    flush(current_heading)
    return passages


def _context(passages: list[Passage], i: int) -> tuple[str, int | None, int | None]:
    """The section around passage ``i``, grown outward from it one neighbour at
    a time, alternately after and before, up to ``_CONTEXT_WORDS``. Returns the
    text (headed by the section heading) and the pages it spans."""
    child = passages[i]
    same = [p for p in passages if p.section == child.section]
    at = next(k for k, p in enumerate(same) if p.index == child.index)
    lo = hi = at
    size = _words(child.text)
    grew = True
    while grew:
        grew = False
        for step in (1, -1):
            j = hi + 1 if step == 1 else lo - 1
            if 0 <= j < len(same) and size + _words(same[j].text) <= _CONTEXT_WORDS:
                size += _words(same[j].text)
                lo, hi = min(lo, j), max(hi, j)
                grew = True
    window = same[lo:hi + 1]
    body = "\n\n".join(p.text for p in window)
    pages = [n for p in window for n in (p.page_start, p.page_end) if n is not None]
    text = f"{child.heading}\n\n{body}" if child.heading else body
    return text, (min(pages) if pages else None), (max(pages) if pages else None)


def _breadcrumb(document: WebDocument, heading: str | None) -> str:
    """Ingestion's "title › heading" trail, capped the same way (in words)."""
    parts = [p.strip() for p in (document.title, heading) if p and p.strip()]
    if not parts:
        return ""
    return " ".join(_BREADCRUMB_SEP.join(parts).split()[:_BREADCRUMB_WORDS])


def _embed_text(document: WebDocument, passage: Passage) -> str:
    crumb = _breadcrumb(document, passage.heading)
    return f"{crumb}\n\n{passage.text}" if crumb else passage.text


def _shortlist(passages: list[Passage], terms: set[str], keep: int) -> list[Passage]:
    """At most ``keep`` passages, preferring those sharing the most words with
    the question, in document order. Bounds embedding cost on a long PDF
    without simply keeping its first pages."""
    if len(passages) <= keep:
        return passages
    scored = sorted(
        passages,
        key=lambda p: (-sum(t in p.text.lower() for t in terms), p.index),
    )[:keep]
    return sorted(scored, key=lambda p: p.index)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _document_id(document: WebDocument) -> str:
    digest = hashlib.sha1(url_key(document.canonical_url).encode("utf-8")).hexdigest()
    return f"web:{digest[:16]}"


def _payload(
    document: WebDocument,
    hit: SearchHit,
    passage: Passage,
    domain_policy: DomainPolicy,
    passages: list[Passage],
) -> dict:
    """The provenance contract of one web passage.

    Dates are the subtle part. ``published_date`` is the date the document
    states for itself, and ``date_source`` names where it was read; when the
    page states none, the search provider's estimate is used and labelled
    ``search_provider``, and when there is neither, the passage is undated —
    never dated by when it was fetched. ``retrieved_at`` is that fetch time and
    nothing else. ``effective_start_date`` carries the same publication date,
    because it is the field ranking reads for every candidate.
    """
    context, context_start, context_end = _context(passages, passage.index)
    published, date_source = document.published, document.date_source
    if not published and hit.published:
        published, date_source = hit.published, "search_provider"
    document_id = _document_id(document)
    is_pdf = document.kind == "pdf"
    payload = {
        "source_type": WEB_SOURCE_TYPE,
        "document_id": document_id,
        "chunk_id": f"{document_id}:{passage.index}",
        "chunk_text": passage.text,
        "context_text": context,
        "title": document.title or hit.title or document.domain,
        "section_heading": passage.heading,
        "url": document.final_url,
        "canonical_url": document.canonical_url,
        "file_url": document.final_url if is_pdf else None,
        "domain": document.domain,
        "site_name": document.site_name,
        "is_primary_source": domain_policy.is_primary(document.canonical_url),
        "content_type": document.kind,
        "authors": list(document.authors),
        "published_date": published,
        "date_source": date_source,
        "effective_start_date": f"{published}T00:00:00" if published else None,
        "retrieved_at": document.fetched_at,
        "retrieval_method": f"web_search:{hit.provider}",
        "search_query": hit.query,
        "search_rank": hit.rank,
        "passage_index": passage.index,
        "has_table": passage.kind == TABLE or None,
        "untrusted": True,
    }
    if is_pdf and passage.page_start is not None:
        # The passage's own page is where its citation points; the context's
        # span is what the header states, as the corpus does for parents.
        payload["page_number"] = passage.page_start
        payload["page_range"] = [context_start, context_end]
    return {k: v for k, v in payload.items() if v not in (None, "", [])}


def _default_embedder(texts: list[str]) -> list[list[float]]:
    from app.core.clients import get_embeddings

    return get_embeddings().embed_documents(texts)


def to_candidates(
    documents: Sequence[tuple[WebDocument, SearchHit]],
    query: str,
    query_vector: Sequence[float],
    *,
    embed: Embedder | None = None,
    per_document: int | None = None,
    limit: int | None = None,
) -> list[Candidate]:
    """The best passages of the given documents as ranked candidates.

    One embedding call for every passage of every document. Each document
    contributes at most ``per_document`` passages, at most ``limit`` are
    returned in all, and a passage near-identical to one already chosen (the
    same article syndicated on two sites) is skipped. Raises what the embedder
    raises; the caller decides what a failure costs.
    """
    settings = get_settings()
    per_document = max(1, int(per_document or settings.web_passages_per_document))
    limit = max(1, int(limit or settings.web_max_candidates))
    terms = {t.lower() for t in (extract_content_terms(query) or [])}
    domain_policy = policy()

    work: list[tuple[WebDocument, SearchHit, list[Passage], Passage]] = []
    for document, hit in documents:
        passages = split(document)
        for passage in _shortlist(passages, terms, _MAX_EMBEDDED_PER_DOCUMENT):
            work.append((document, hit, passages, passage))
    if not work:
        return []

    vectors = (embed or _default_embedder)([_embed_text(d, p) for d, _, _, p in work])
    scored = sorted(
        (
            (_cosine(query_vector, vector), vector, document, hit, passages, passage)
            for (document, hit, passages, passage), vector in zip(work, vectors)
        ),
        key=lambda item: (-item[0], item[3].rank, item[5].index),
    )

    threshold = float(settings.dedup_cosine_threshold)
    chosen: list[Candidate] = []
    per_doc: dict[str, int] = {}
    for score, vector, document, hit, passages, passage in scored:
        if len(chosen) >= limit:
            break
        doc_id = _document_id(document)
        if per_doc.get(doc_id, 0) >= per_document:
            continue
        if any(_cosine(vector, c.vector) >= threshold for c in chosen):
            continue
        payload = _payload(document, hit, passage, domain_policy, passages)
        chosen.append(Candidate(
            id=payload["chunk_id"], score=score, payload=payload,
            vector=list(vector), semantic_score=score,
        ))
        per_doc[doc_id] = per_doc.get(doc_id, 0) + 1
    return chosen
