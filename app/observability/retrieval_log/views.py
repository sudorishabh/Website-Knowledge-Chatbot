"""How each store's results are rendered into a trace.

This is the module that knows a Qdrant point has ``.id`` and ``.score``, that a
graph row is a mapping, and that a context block carries a payload. It knows it
by *duck-typing* — ``getattr``/``get`` and nothing else — because observability
sits at the bottom of the hierarchy (see ``tests/test_architecture.py``) and may
not import retrieval, catalog or core models. The upside is the same one the
layering exists for: these renderers can be read, changed and tested without a
running Qdrant, and no retrieval module has to learn the log's shape.

Everything here is bounded. ``limit`` caps the number of items rendered in full;
the caller still records the true total, so a capped sample never reads as a
smaller result set than the query actually returned.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from app.observability.retrieval_log.safe import clip, jsonable

#: Payload fields worth keeping per retrieved chunk: provenance (which document,
#: which page), the facets retrieval filters on, and the dates ranking uses.
#: Deliberately a list rather than "the whole payload" — a payload also carries
#: the chunk text twice over and a dozen ingestion bookkeeping fields, and a
#: trace is for reading.
CHUNK_METADATA_FIELDS: tuple[str, ...] = (
    "chunk_id",
    "document_id",
    "parent_chunk_id",
    "chunk_index",
    "title",
    "source_type",
    "section_heading",
    "section_type",
    "page_number",
    "page_range",
    "effective_start_date",
    "start_precision",
    "language",
    "categories",
    "tags",
    "authors",
    "source_url",
    "file_url",
    "has_table",
    "token_count",
    "is_current",
    "doc_version",
    "linked_pdf_id",
    "linked_article_uuid",
    "kind",
)


def chunk_metadata(payload: Any, *, limit: int) -> dict[str, Any]:
    """The interesting part of a chunk payload, bounded and redacted."""
    if not isinstance(payload, dict):
        return {}
    return {
        key: jsonable(payload[key], limit=limit)
        for key in CHUNK_METADATA_FIELDS
        if payload.get(key) is not None
    }


def _text_of(payload: Any) -> str:
    if isinstance(payload, dict):
        return payload.get("chunk_text") or ""
    return ""


def qdrant_points(
    points: Sequence[Any],
    *,
    limit: int,
    include_text: bool,
    text_limit: int,
) -> list[dict[str, Any]]:
    """Qdrant hits as ``{id, score, metadata, text}``, in the order returned.

    Rank is explicit so the trace still reads correctly after it has been loaded
    into a dataframe and sorted by something else.
    """
    out: list[dict[str, Any]] = []
    for rank, point in enumerate(list(points)[:limit], start=1):
        payload = getattr(point, "payload", None)
        if payload is None and isinstance(point, dict):
            payload = point
        score = getattr(point, "score", None)
        entry: dict[str, Any] = {
            "rank": rank,
            "id": str(getattr(point, "id", "") or ""),
            "metadata": chunk_metadata(payload, limit=text_limit),
        }
        if score is not None:
            try:
                entry["score"] = round(float(score), 6)
            except (TypeError, ValueError):
                entry["score"] = jsonable(score, limit=80)
        text = _text_of(payload)
        entry["text_chars"] = len(text)
        if include_text and text:
            entry["text"] = jsonable(text, limit=text_limit)
        out.append(entry)
    return out


def candidates(
    items: Sequence[Any],
    *,
    limit: int,
    include_text: bool,
    text_limit: int,
) -> list[dict[str, Any]]:
    """Post-search candidates, which carry three scores that must not be merged
    (see ``app.retrieval.search.hybrid_search.Candidate``): the current ranking
    value, the raw semantic relevance every configured floor is calibrated
    against, and the fusion value. A trace that recorded only one of them could
    not explain why a chunk was admitted."""
    out: list[dict[str, Any]] = []
    for rank, item in enumerate(list(items)[:limit], start=1):
        payload = getattr(item, "payload", None)
        entry: dict[str, Any] = {
            "rank": rank,
            "id": str(getattr(item, "id", "") or ""),
            "score": _round(getattr(item, "score", None)),
            "semantic_score": _round(getattr(item, "semantic_score", None)),
            "fusion_score": _round(getattr(item, "fusion_score", None)),
            "metadata": chunk_metadata(payload, limit=text_limit),
        }
        text = getattr(item, "text", None) or _text_of(payload)
        entry["text_chars"] = len(text or "")
        if include_text and text:
            entry["text"] = jsonable(text, limit=text_limit)
        out.append(entry)
    return out


def rows(items: Iterable[Any], *, limit: int, text_limit: int) -> list[Any]:
    """Graph rows / SQL rows, converted and bounded."""
    return [jsonable(row, limit=text_limit) for row in list(items)[:limit]]


# -- the compact renderings ------------------------------------------------
#
# The same information, one line per item instead of one object per item. This
# is the default because the structured form is unreadable at scale and the
# scale is not optional: a single question runs five Qdrant legs of forty hits
# each, and at eight lines a hit that is ten thousand lines of JSON for one
# query. A hit that reads
#
#     " 1. 0.531  Take that seaweed and make it fuel… · website · 2023-08-10
#       · 249dd74e | The coastal land can be effectively used to grow…"
#
# answers "what came back, in what order, and was it relevant?" at a glance,
# which is the question the file is opened for. `RETRIEVAL_LOG_DETAIL=full`
# restores the structured objects for programmatic work.

#: Snippet kept per hit in the compact rendering. Enough to recognise a passage,
#: not enough to bury the next one.
COMPACT_TEXT_CHARS = 160

#: Above this many rows, a statement is a *bulk load* rather than an answer, and
#: the compact rendering keeps only its count. The read path loads whole
#: vocabularies to resolve a question — the author gazetteer, the theme names,
#: the publisher list, the entity index — and one query did ten such loads of
#: between 414 and 2,793 rows. Sampling five of them tells a reader nothing they
#: could act on, while the count and the SQL tell them everything: which
#: vocabulary, how big, how long it took. `RETRIEVAL_LOG_DETAIL=full` samples
#: them like any other result.
BULK_ROWS = 200


def _short_id(value: Any) -> str:
    """The leading segment of a uuid — enough to match against another trace."""
    text = str(value or "")
    return text.split("-")[0][:8]


def _describe(metadata: dict[str, Any]) -> str:
    """A hit's provenance in one phrase: what it is, and where it came from."""
    parts: list[str] = []
    title = metadata.get("title")
    if title:
        # A plain ellipsis, not the truncation marker: a shortened title is
        # obviously shortened, and "…[truncated]" on every line is the kind of
        # noise this rendering exists to remove.
        text = str(title)
        parts.append(text if len(text) <= 60 else text[:59] + "…")
    page = metadata.get("page_number")
    span = metadata.get("page_range")
    if isinstance(span, (list, tuple)) and len(span) == 2 and span[0] != span[1]:
        parts.append(f"pp.{span[0]}-{span[1]}")
    elif page is not None:
        parts.append(f"p.{page}")
    source = metadata.get("source_type")
    if source:
        parts.append(str(source))
    effective = metadata.get("effective_start_date")
    if effective:
        parts.append(str(effective)[:10])
    document = metadata.get("document_id")
    if document:
        parts.append(_short_id(document))
    return " · ".join(parts) or "(no metadata)"


def qdrant_points_compact(
    points: Sequence[Any], *, limit: int, include_text: bool
) -> list[str]:
    """Qdrant hits as one line each: rank, score, provenance, snippet."""
    out: list[str] = []
    for rank, point in enumerate(list(points)[:limit], start=1):
        payload = getattr(point, "payload", None)
        if payload is None and isinstance(point, dict):
            payload = point
        metadata = chunk_metadata(payload, limit=200)
        score = getattr(point, "score", None)
        head = f"{rank:>2}."
        if score is not None:
            try:
                head += f" {float(score):.3f}"
            except (TypeError, ValueError):
                pass
        line = f"{head}  {_describe(metadata)}"
        text = _text_of(payload)
        if include_text and text:
            line += f" | {clip(' '.join(text.split()), COMPACT_TEXT_CHARS)}"
        elif text:
            line += f" | {len(text)} chars"
        out.append(line)
    return out


def candidates_compact(
    items: Sequence[Any], *, limit: int, include_text: bool
) -> list[str]:
    """Candidates as one line each, keeping all three scores."""
    out: list[str] = []
    for rank, item in enumerate(list(items)[:limit], start=1):
        metadata = chunk_metadata(getattr(item, "payload", None), limit=200)
        scored = []
        for name, attr in (("rank", "score"), ("sem", "semantic_score"),
                           ("rrf", "fusion_score")):
            value = _round(getattr(item, attr, None))
            if isinstance(value, float):
                scored.append(f"{name}={value:.3f}")
        line = f"{rank:>2}. {' '.join(scored)}  {_describe(metadata)}"
        text = getattr(item, "text", None) or ""
        if include_text and text:
            line += f" | {clip(' '.join(text.split()), COMPACT_TEXT_CHARS)}"
        out.append(line)
    return out


#: A list of this many short scalars or fewer is joined onto one line. Above it
#: the list is left alone, because at that length the items are the subject
#: (a hundred hydrated chunk ids) rather than a detail of it.
COMPACT_LIST_ITEMS = 60


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return clip(str(value), 60)


def _condition(node: Any) -> str:
    """One filter condition as a phrase: ``source_type=website``."""
    if not isinstance(node, dict):
        return _scalar(node)
    key = node.get("key")
    match = node.get("match")
    if key and isinstance(match, dict):
        if "value" in match:
            return f"{key}={_scalar(match['value'])}"
        if "any" in match:
            values = match["any"] or []
            listed = ", ".join(_scalar(v) for v in values[:6])
            more = f", +{len(values) - 6}" if len(values) > 6 else ""
            return f"{key} in [{listed}{more}]"
        if "text" in match:
            return f"{key} ~ {_scalar(match['text'])!r}"
        if "except" in match:
            return f"{key} not in [{len(match['except'] or [])} value(s)]"
    for name in ("range", "datetime_range"):
        bounds = node.get(name)
        if key and isinstance(bounds, dict):
            spelled = " ".join(
                f"{op} {_scalar(v)}" for op, v in bounds.items() if v is not None
            )
            return f"{key} {spelled}".strip()
    if node.get("has_id") is not None:
        return f"id in [{len(node['has_id'] or [])} id(s)]"
    if any(k in node for k in ("must", "should", "must_not")):
        return f"({filter_compact(node)})"
    import json

    return clip(json.dumps(node, default=str), 200)


def filter_compact(value: Any) -> str:
    """A Qdrant filter tree as one readable line.

    Every pull carries the mandatory shape conditions (``is_parent``,
    ``is_current``, the non-searchable section exclusion), so the structured form
    repeats forty lines of the same thing on every leg of every query — which is
    how a trace becomes ten thousand lines that say very little. The same tree
    reads as::

        is_parent=false AND is_current=true AND source_type=website
        AND NOT (section_type in [toc, references, glossary])

    Written against the already-serialized dictionary rather than the Qdrant
    models, because this layer may not import them.
    """
    if isinstance(value, (list, tuple)):
        return " AND ".join(_condition(v) for v in value) or "(none)"
    if not isinstance(value, dict):
        return _scalar(value)
    clauses: list[str] = []
    for key, joiner, wrap in (
        ("must", " AND ", "{0}"),
        ("should", " OR ", "ANY({0})"),
        ("must_not", " OR ", "NOT ({0})"),
    ):
        conditions = value.get(key) or []
        if conditions:
            clauses.append(wrap.format(joiner.join(_condition(c) for c in conditions)))
    if not clauses:
        return _condition(value)
    return " AND ".join(clauses)


def compact_request(fields: dict[str, Any]) -> dict[str, Any]:
    """A request record made readable: filters as one line, short lists joined.

    Applied by field *name* — the same idea as redacting by name, and for the
    same reason: the renderer cannot know what a value is, but the code that
    named it did. Anything unrecognised is left exactly as it was.
    """
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key in ("filter", "filters", "query_filter") and value not in (None, [], {}):
            out[key] = filter_compact(value)
        elif (
            isinstance(value, list)
            and 1 < len(value) <= COMPACT_LIST_ITEMS
            and all(isinstance(v, (str, int, float, bool)) for v in value)
        ):
            out[key] = ", ".join(_scalar(v) for v in value)
        else:
            out[key] = value
    return out


def rows_compact(items: Iterable[Any], *, limit: int) -> list[str]:
    """Graph / SQL rows as one line each — the row's own JSON, unindented.

    Still valid JSON per line, so a row can be read by eye *and* parsed back;
    it is only the indentation that made a ten-key row twelve lines long.
    """
    import json

    out: list[str] = []
    for row in list(items)[:limit]:
        converted = jsonable(row, limit=200)
        try:
            out.append(clip(json.dumps(converted, ensure_ascii=False, default=str), 300))
        except Exception:  # pragma: no cover - defence in depth
            out.append(clip(repr(converted), 300))
    return out


#: Runaway guard on the context text, not a display limit. What the LLM is sent
#: is already bounded by ``context_token_budget`` (9,000 tokens ≈ 36 KB), so this
#: only fires if that budget is ever raised to something pathological — the
#: context is the one thing in a trace that must not be a sample of itself.
PROMPT_TEXT_MAX = 200_000


def context_blocks(
    blocks: Sequence[Any],
    *,
    limit: int,
    include_text: bool,
    text_limit: int,
    compact: bool = False,
    rendered: str | None = None,
) -> dict[str, Any]:
    """The final context, exactly as the LLM receives it.

    Two halves, and the distinction is the point:

    ``prompt_text``  the *rendered* context — the string the generation step
                     interpolates into the prompt, block headers, source hints
                     and group headings included. Untruncated. Present only when
                     the caller had a prompt to render (``/chat``); ``/search``
                     runs no generation, so there is nothing to render and the
                     per-block text below stands in its place.
    ``blocks``       provenance, ranking and order per block. Ordering is meaning
                     here — the builder arranges blocks for attention and the
                     graph's facts block leads — so a trace that lost the order
                     could not explain the answer it produced.

    The per-block ``text`` is dropped once ``prompt_text`` is present, because
    the rendered string already contains every block verbatim and printing both
    doubles the file to say the same thing twice. ``text_chars`` stays either
    way, so the per-block sizes are still readable.
    """
    listed = list(blocks or [])
    entries: list[dict[str, Any]] = []
    total_chars = 0
    for block in listed:
        text = getattr(block, "text", "") or ""
        total_chars += len(text)
        if len(entries) >= limit:
            continue
        payload = getattr(block, "payload", None)
        metadata = chunk_metadata(payload, limit=text_limit)
        entry: dict[str, Any] = {
            "n": getattr(block, "n", len(entries) + 1),
            "score": _round(getattr(block, "score", None)),
            "conflict": bool(getattr(block, "conflict", False)),
            "text_chars": len(text),
        }
        # Only when set, so an ordinary answer's trace is unchanged. Written as
        # one field rather than two booleans because a block is at most one of
        # them and "superseded"/"supersedes" differ by two characters — easy to
        # misread in a dump, which is where this is read.
        if getattr(block, "superseded", False):
            entry["supersession"] = "superseded"
        elif getattr(block, "supersedes", False):
            entry["supersession"] = "supersedes"
        entry["source" if compact else "metadata"] = (
            _describe(metadata) if compact else metadata
        )
        if include_text and text:
            entry["text"] = clip(text, text_limit)
        entries.append(entry)
    out: dict[str, Any] = {
        "block_count": len(listed),
        "total_chars": total_chars,
        "blocks": entries,
        "blocks_truncated": len(entries) < len(listed),
    }
    if rendered:
        # The true size of what was sent, always — a truncated sample must never
        # be mistaken for the whole prompt. The string itself only at full
        # detail, where nothing is abridged by definition.
        out["prompt_chars"] = len(rendered)
        if include_text and not compact:
            out["prompt_text"] = clip(rendered, PROMPT_TEXT_MAX)
    return out


def _round(value: Any) -> Any:
    if value is None:
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return jsonable(value, limit=80)
