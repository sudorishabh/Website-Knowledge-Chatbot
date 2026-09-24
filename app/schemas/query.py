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


class CitationSource(BaseModel):

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


class Citation(BaseModel):

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
