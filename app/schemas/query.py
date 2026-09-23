from __future__ import annotations

from pydantic import BaseModel, Field


class ChatTurn(BaseModel):

    role: str = Field(description='"user" or "assistant"')
    content: str


class QueryRequest(BaseModel):
    # user_groups is intentionally absent: the caller's authorization groups
    # come from the authenticated principal (see
    # app/api/auth.py), never from the request body.
    question: str = Field(min_length=1)
    history: list[ChatTurn] = Field(default_factory=list)
    # Bounded: this is public input, and an absurd top_k inflates retrieval and
    # context-assembly work per request. None = server default.
    top_k: int | None = Field(default=None, ge=1, le=50)


class Provenance(BaseModel):
    """Where a source came from and how it was found — the fields that let a
    reader, or a developer, answer "why was this used?".

    All optional and additive: a client that has never heard of them sees the
    citation it always saw. Dates are deliberately narrow. ``published_date`` is
    set only for a web source, and is the date that page states for itself, with
    ``date_source`` saying where it was read; a corpus source's date is its CMS
    page date, which is a different fact and is not repeated here as a
    publication date. ``retrieved_at`` is when a web page was read — never a
    publication date.
    """

    domain: str | None = None
    authors: list[str] = Field(default_factory=list)
    published_date: str | None = None
    date_source: str | None = None
    retrieved_at: str | None = None
    # "corpus", "corpus_via_web_search:<provider>", "web_search:<provider>" or
    # "knowledge_graph".
    retrieval_method: str | None = None
    # For a web source: whether it is on the organisation's own site.
    is_primary_source: bool | None = None
    chunk_id: str | None = None


class CitationSource(Provenance):

    type: str
    title: str | None = None
    url: str | None = None
    # The first and last page of the cited evidence. Equal for a single-page
    # passage; a parent-expanded block genuinely spans several, and citing only
    # the first would claim a narrower source than the text supports.
    page: int | None = None
    page_end: int | None = None
    section: str | None = None
    # The reporting period the document covers ("2024-25"), when one was
    # recovered at ingest. Editions of a series share a title and a
    # effective_start_date, so without this a citation cannot say which one it is.
    edition: str | None = None


class Citation(Provenance):

    n: int
    type: str
    title: str | None = None
    url: str | None = None
    # The first and last page of the cited evidence. Equal for a single-page
    # passage; a parent-expanded block genuinely spans several, and citing only
    # the first would claim a narrower source than the text supports.
    page: int | None = None
    page_end: int | None = None
    section: str | None = None
    # The reporting period the document covers ("2024-25"), when one was
    # recovered at ingest. Editions of a series share a title and a
    # effective_start_date, so without this a citation cannot say which one it is.
    edition: str | None = None
    document_id: str | None = None
    # The ranking score the evidence was admitted on (the reranker's relevance).
    score: float | None = None
    also_available: list[CitationSource] = Field(default_factory=list)


class SearchRequest(BaseModel):
    # Identity comes from the authenticated principal, not the body (see QueryRequest).
    question: str = Field(min_length=1)
    history: list[ChatTurn] = Field(default_factory=list)
    top_k: int | None = Field(default=None, ge=1, le=50)


class SearchBlock(BaseModel):
    n: int
    score: float
    conflict: bool = False
    text: str
    document_id: str | None = None
    source_type: str | None = None
    title: str | None = None
    page_number: int | None = None
    section_heading: str | None = None


class DetectedIntent(BaseModel):
    label: str
    confidence: float = 1.0
    rationale: str = ""


class SearchResponse(BaseModel):
    intent: str
    answer_format: str = "default"
    search_query: str
    # Multi-label understanding exposed for inspection/debugging (see
    # docs/intent-classification-design.md). `intent` stays the single-label
    # route the pipeline acts on; `intents` is the full detected set.
    intents: list[DetectedIntent] = Field(default_factory=list)
    is_ambiguous: bool = False
    blocks: list[SearchBlock] = Field(default_factory=list)
