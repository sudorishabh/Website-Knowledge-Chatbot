from __future__ import annotations
from typing import TYPE_CHECKING, Any, Protocol
from app.core.models import CanonicalDocument, CanonicalSection, EntityRef, FileLink
from app.ingestion.bylines import authors_from
from app.ingestion.textutil import slugify as _slugify

if TYPE_CHECKING:
    from app.ingestion.bundle_dates import EffectiveDate

# Substring hints that route Drupal metadata fields into canonical facets.
# field_audit reports against these same rules — import from here, don't copy.
#
# Themes match on "theme" alone. They used to also absorb any field named
# category/area/division, which put things that are not themes (a division, a
# regional area) into a document's themes; those vocabularies are dimensions of
# their own and still reach the catalog through entity refs and raw_meta.
THEME_HINTS: tuple[str, ...] = ("theme",)
TAG_HINTS: tuple[str, ...] = ("tag", "keyword")
AUTHOR_HINTS: tuple[str, ...] = ("author",)

# Taxonomy vocabularies whose terms are categories regardless of the name of
# the referencing field — vocabulary routing beats field-name guessing.
CATEGORY_VOCABULARIES: tuple[str, ...] = ("themes",)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        items = [part.strip() for part in str(value).split(",")]
    return [item for item in items if item]


def _matching(meta: dict[str, Any], *substrings: str) -> list[tuple[str, Any]]:
    """Metadata fields whose *name* carries one of the hints and whose *value*
    can be a facet at all.

    A facet is a name. Drupal also exposes boolean switches whose field name
    happens to contain a hint — ``field_show_on_theme_page`` on the project
    bundles — and ``_as_list`` would turn ``True`` into the category ``"True"``,
    which then reached every chunk payload and the theme filter. The catalog
    layer already refused it (``theme_taxonomy._NOT_A_THEME``); it has to be
    refused here, where the payload is built from, or the two stores disagree.
    """
    return [
        (key, value)
        for key, value in meta.items()
        if value not in (None, "", [])
        and not isinstance(value, bool)
        and any(s in key.lower() for s in substrings)
    ]


def _pick(meta: dict[str, Any], *substrings: str) -> Any:
    """The one matching field a single-valued facet is read from: the first
    list-valued match, else the first match at all; None when nothing matches."""
    matches = _matching(meta, *substrings)
    for _, value in matches:
        if isinstance(value, (list, tuple)):
            return value
    return matches[0][1] if matches else None


def _union_list(meta: dict[str, Any], *substrings: str) -> list[str]:
    seen: dict[str, None] = {}
    for _, value in _matching(meta, *substrings):
        for item in _as_list(value):
            seen.setdefault(item, None)
    return list(seen)


class _PdfPage(Protocol):
    page_number: int
    text: str


class _PdfResult(Protocol):
    source: str
    pages: list[_PdfPage]


def from_pdf(
    result: _PdfResult,
    *,
    document_id: str | None = None,
    source_type: str = "pdf_attachment",
    **overrides: Any,
) -> CanonicalDocument:
    doc_id = document_id or _slugify(getattr(result, "source", "") or "")
    sections = [
        CanonicalSection(
            text=page.text,
            page_start=page.page_number,
            page_end=page.page_number,
            order=i,
        )
        for i, page in enumerate(result.pages)
        if getattr(page, "text", "")
    ]
    doc = CanonicalDocument(
        document_id=overrides.pop("document_id_override", doc_id),
        source_type=source_type,
        title=overrides.pop("title", getattr(result, "source", None)),
        sections=sections,
        pdf_id=overrides.pop("pdf_id", doc_id),
        pdf_path=overrides.pop("pdf_path", getattr(result, "source", None)),
        **overrides,
    )
    doc.ensure_content_hash()
    return doc


def drupal_facets(
    metadata: dict[str, Any], refs: list[EntityRef]
) -> dict[str, list[str]]:
    """Facet lists (categories/tags/authors) for a Drupal record. Shared by the
    node document and its attachment documents — an attached PDF inherits its
    node's facets so theme-scoped retrieval reaches the PDF content too.

    Only themes the record is actually tagged with land in ``categories``: its
    references into a theme vocabulary, plus theme-named metadata for the
    ref-less paths (``from_drupal_export`` / the upload routes have no
    relationships to read). A taxonomy term's ``parent`` is no longer folded in
    by name — a real parent inside a theme vocabulary already arrives as a ref
    below, and the parent of a term in some *other* vocabulary was never a theme.
    The primary-tag/sub-theme relationship is recorded on the theme rows
    themselves (see :mod:`app.catalog.theme_taxonomy`)."""
    categories = _union_list(metadata, *THEME_HINTS)
    # Any reference into a category vocabulary is a category, whatever the
    # referencing field is called — catches fields the name hints miss.
    for ref in refs:
        if ref.vocabulary in CATEGORY_VOCABULARIES and ref.label:
            if ref.label not in categories:
                categories.append(ref.label)
    return {
        "categories": categories,
        "tags": _union_list(metadata, *TAG_HINTS),
        # Read as a byline, not split on every comma: "Sehgal, Meena" is one
        # person and "A and B" two (see app.ingestion.bylines).
        "authors": authors_from(_pick(metadata, *AUTHOR_HINTS)),
    }


def _effective_dates_for(
    bundle: str | None, created: str | None, metadata: dict[str, Any]
) -> "EffectiveDate":
    """This record's effective date(s), and the evidence for them.

    **Keyed by bundle.** Which date a Drupal record carries is a property of its
    content type, not of which date-like fields happen to be present: ``news``
    takes ``field_news_date``, ``completed_projects`` takes the project's start,
    ``article`` takes its creation stamp. :mod:`app.ingestion.bundle_dates`
    declares that mapping — as an *ordered list* of fields, so a bundle whose
    content covers a period resolves a start and an end through the same code —
    and owns the decision — ingestion, the attachment path
    and the backfill all call the same function, because two copies of a
    conditional rule drift and a re-ingested document would then get a different
    date than the backfill gave it.

    The whole :class:`~app.ingestion.bundle_dates.EffectiveDate` is returned
    rather than the value alone: the caller writes the audit row, and a row
    derived from a second reading of the metadata could disagree with the value
    actually applied.
    """
    from app.ingestion.bundle_dates import resolve_effective_dates

    return resolve_effective_dates(bundle, created, metadata)


def _drupal_document(
    *,
    body: str,
    title: str | None,
    url: str | None,
    uuid: str | None,
    bundle: str | None,
    nid: int | None,
    created: str | None,
    changed: str | None,
    metadata: dict[str, Any],
    refs: list[EntityRef] | None = None,
    **overrides: Any,
) -> CanonicalDocument:
    refs = refs or []
    facets = drupal_facets(metadata, refs)
    resolved = _effective_dates_for(bundle, created, metadata)

    doc = CanonicalDocument(
        document_id=uuid or _slugify(url or f"{bundle}/{title}"),
        source_type=overrides.pop("source_type", "website"),
        title=(title or "").strip() or None,
        sections=[CanonicalSection(text=body, order=0)] if body else [],
        source_url=url,
        article_uuid=uuid or None,
        tags=facets["tags"],
        categories=facets["categories"],
        authors=facets["authors"],
        effective_start_date=resolved.start_value,
        date_source=resolved.source,
        start_precision=resolved.start_precision,
        effective_end_date=resolved.end_value,
        end_precision=resolved.end_precision,
        date_evidence=resolved,
        extra={"bundle": bundle, "nid": nid, "changed": changed},
        entity_refs=refs,
        raw_meta=dict(metadata),
        **overrides,
    )
    doc.ensure_content_hash()
    return doc


def from_drupal_record(record: Any, **overrides: Any) -> CanonicalDocument:
    overrides.setdefault("file_url", getattr(record, "pdf_url", None))
    return _drupal_document(
        body=record.body,
        title=record.title,
        url=record.url,
        uuid=record.uuid or None,
        bundle=record.bundle,
        nid=record.nid,
        created=record.created,
        changed=record.changed,
        metadata=record.metadata or {},
        refs=list(getattr(record, "refs", None) or []),
        file_links=[
            FileLink(uuid=f.uuid, origin=f.origin, url=f.url, filename=f.filename)
            for f in getattr(record, "files", None) or []
            if f.uuid
        ],
        **overrides,
    )


def from_drupal_export(item: dict[str, Any], **overrides: Any) -> CanonicalDocument:
    meta = {k: v for k, v in item.items() if k != "text"}
    return _drupal_document(
        body=item.get("text", "") or "",
        title=meta.get("title"),
        url=meta.get("url"),
        uuid=meta.get("uuid") or None,
        bundle=meta.get("bundle"),
        nid=meta.get("nid"),
        created=meta.get("created"),
        changed=meta.get("changed"),
        metadata=meta,
        **overrides,
    )
