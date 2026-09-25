"""From matched pages to the blocks that lead the context.

One :class:`PriorityEvidence` per question, built by :func:`gather` before the
answer cache is consulted (the pages' content hashes are part of the cache
key) and carried into retrieval, which uses it three ways:

1. **drop the stored copy** of every listed page from the ranked candidates —
   website chunks only; the PDFs ingested from those pages are ordinary corpus
   documents and stay;
2. **extend** it with any listed page whose stored copy ranked near the top,
   since ordinary search just said that page is relevant;
3. **merge** its blocks in front of the corpus blocks, leaving the corpus at
   least two slots.

Which sections become blocks: the opening section of every page the question
is *about* (named, a person's profile, the theme facet, a description match),
then the other sections that score at least ``priority_section_floor`` against
the question, best first, up to ``priority_max_blocks`` in all.
"""
from __future__ import annotations

import hashlib
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from app.config import get_settings
from app.core.models.context import PRIORITY_PAGE_KIND, ContextBlock, source_kind
from app.observability import retrieval_log
from app.retrieval.priority import fetch as fetching
from app.retrieval.priority import match
from app.retrieval.priority.extract import PageContent, Section, extract
from app.retrieval.priority.match import ABOUT, GROUP_KIND, SURFACED, Target
from app.retrieval.priority.people import Person, people_on
from app.retrieval.priority.registry import PEOPLE, Registry, load_registry, normalize_url

logger = logging.getLogger(__name__)

#: Corpus slots a merge always leaves, when the corpus has blocks to fill them.
CORPUS_MIN_SLOTS = 2
#: A page whose extracted text is shorter than this is treated as unreadable
#: (a maintenance page, an error page served as 200).
MIN_PAGE_CHARS = 80
#: People named in one question whose profiles are read.
MAX_PEOPLE = 2

Embed = Callable[[list[str]], list[list[float]]]


@dataclass
class PageRead:
    target: Target
    url: str
    content: PageContent | None = None
    fetched: fetching.FetchedPage | None = None
    ms: float = 0.0

    def describe(self) -> dict:
        page = self.fetched
        return {
            "name": self.target.name, "url": self.url, "reason": self.target.reason,
            "ok": self.content is not None,
            "from_cache": bool(page and page.from_cache),
            "stale": bool(page and page.stale),
            "ms": round(self.ms, 1),
            "sections": len(self.content.sections) if self.content else 0,
            "documents": len(self.content.documents) if self.content else 0,
            "hash": self.content.content_hash[:16] if self.content else None,
        }


@dataclass
class PriorityEvidence:
    """What the priority pages contribute to one question."""

    registry: Registry
    query_vector: list[float] = field(default_factory=list)
    targets: list[Target] = field(default_factory=list)
    reads: list[PageRead] = field(default_factory=list)
    blocks: list[ContextBlock] = field(default_factory=list)
    similar_top: list[dict] = field(default_factory=list)
    suppressed: int = 0
    embed: Embed | None = None

    # -- the cache key -------------------------------------------------------------
    def fingerprint(self) -> dict[str, Any]:
        """The pages this answer stands on, by content. Empty when none do, so
        an ordinary question's cache key is exactly what it always was."""
        used = sorted({
            f"{normalize_url(b.payload.get('source_url')) or b.payload.get('document_id')}"
            f"#{str(b.payload.get('content_hash') or '')[:16]}"
            for b in self.blocks
        })
        return {"priority": used} if used else {}

    # -- retrieval's side ------------------------------------------------------------
    def drop_stored_copies(self, candidates: Sequence[Any], *, top_n: int) -> list[Any]:
        """``candidates`` without the stored copy of any listed page. Listed
        pages whose stored copy ranked within ``top_n`` are read live instead."""
        kept: list[Any] = []
        surfaced: list[Target] = []
        seen: set[str] = set()
        for rank, candidate in enumerate(candidates):
            payload = getattr(candidate, "payload", {}) or {}
            page = (self.registry.page_for(payload.get("source_url"))
                    if source_kind(payload) == "website" else None)
            if page is None:
                kept.append(candidate)
                continue
            self.suppressed += 1
            if rank < top_n and page.key not in seen:
                seen.add(page.key)
                surfaced.append(Target(page.name, page.kind, SURFACED, url=page.url, page=page))
        if surfaced:
            self.extend(surfaced)
        self._note()
        return kept

    def extend(self, targets: Sequence[Target]) -> None:
        """Read more pages for this question, within the caps."""
        settings = get_settings()
        known = {t.key for t in self.targets}
        room = max(0, settings.priority_max_pages - _page_count(self.targets))
        new = [t for t in match.ranked(targets) if t.key not in known][:room]
        if not new:
            return
        self.targets.extend(new)
        reads = _read_all(new, self.registry)
        self.reads.extend(reads)
        self.blocks = _select(self.targets, self.reads, self.query_vector,
                              limit=settings.priority_max_blocks, embed=self.embed)

    def merge(self, blocks: Sequence[ContextBlock], *, limit: int, token_budget: int) -> list[ContextBlock]:
        """This question's priority blocks first, then the corpus's, numbered."""
        from app.retrieval.context.builder import _count_tokens

        reserved = min(CORPUS_MIN_SLOTS, len(blocks))
        lead = self.blocks[: max(0, limit - reserved)]
        merged: list[ContextBlock] = []
        spent = 0
        texts: set[str] = set()
        for block in [*lead, *blocks]:
            if len(merged) >= limit:
                break
            key = " ".join(block.text.split())
            if key in texts:
                continue
            cost = _count_tokens(block.text)
            if merged and spent + cost > token_budget:
                continue
            texts.add(key)
            merged.append(block)
            spent += cost
        for n, block in enumerate(merged, start=1):
            block.n = n
        return merged

    # -- trace -------------------------------------------------------------------------
    def describe(self) -> dict:
        return {
            "targets": [t.describe() for t in self.targets],
            "similar_top": self.similar_top,
            "reads": [r.describe() for r in self.reads],
            "blocks": [
                {"page": b.payload.get("priority_page"), "section": b.payload.get("section_heading"),
                 "reason": b.payload.get("priority_reason"), "score": round(b.score, 4),
                 "chars": len(b.text)}
                for b in self.blocks
            ],
            "stored_copies_dropped": self.suppressed,
        }

    def _note(self) -> None:
        retrieval_log.note(priority_pages=self.describe())


def _page_count(targets: Sequence[Target]) -> int:
    return sum(1 for t in targets if t.kind != GROUP_KIND)


# -- reading pages -------------------------------------------------------------------

def _usable(html: str) -> bool:
    return len(extract(html, "https://teriin.org/").text) >= MIN_PAGE_CHARS


def _read(target: Target, registry: Registry) -> PageRead:
    url = target.url or ""
    started = time.monotonic()
    fetched = fetching.fetch(url, allowed_hosts=registry.hosts, validate=_usable)
    read = PageRead(target, url, fetched=fetched)
    if fetched is not None:
        content = extract(fetched.html, fetched.final_url or url)
        if len(content.text) >= MIN_PAGE_CHARS:
            read.content = content
    read.ms = (time.monotonic() - started) * 1000
    return read


def _read_all(targets: Sequence[Target], registry: Registry) -> list[PageRead]:
    wanted = [t for t in targets if t.kind != GROUP_KIND and t.url]
    if not wanted:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(wanted))) as pool:
        return list(pool.map(lambda t: _read(t, registry), wanted))


def listed_people(registry: Registry) -> list[Person]:
    """Everyone on the people listings, read live (and cached with the pages).
    A listing that cannot be read contributes nobody; the question then goes to
    the corpus, as it would for a name that is not listed."""
    listings = [p for p in registry.pages if p.kind == PEOPLE]
    targets = [Target(p.name, p.kind, match.NAME, url=p.url, page=p) for p in listings]
    people: list[Person] = []
    for read in _read_all(targets, registry):
        if read.content is not None:
            people.extend(people_on(read.content, read.target.name))
    return people


# -- choosing sections -----------------------------------------------------------------

_section_vectors: dict[str, list[float]] = {}
_MAX_CACHED_VECTORS = 4096


def _default_embed(texts: list[str]) -> list[list[float]]:
    from app.core.clients.embeddings import get_embeddings

    return get_embeddings().embed_documents(texts)


def _vectors_for(texts: list[str], embed: Embed | None) -> list[list[float] | None]:
    """Section vectors, embedded once per distinct text. A failure leaves them
    unscored — the pages' opening sections are still admitted."""
    keys = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]
    missing = [(k, t) for k, t in zip(keys, texts) if k not in _section_vectors]
    if missing:
        try:
            vectors = (embed or _default_embed)([t for _, t in missing])
        except Exception:
            logger.warning("Priority page sections could not be embedded; "
                           "only opening sections will be used.", exc_info=True)
            vectors = []
        if len(_section_vectors) + len(vectors) > _MAX_CACHED_VECTORS:
            _section_vectors.clear()
        for (key, _), vector in zip(missing, vectors):
            _section_vectors[key] = list(vector)
    return [_section_vectors.get(k) for k in keys]


def clear_vectors() -> None:
    _section_vectors.clear()


def _cosine(a: Sequence[float], b: Sequence[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _doc_id(key: str) -> str:
    return "priority:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _page_block(read: PageRead, section: Section, index: int, score: float) -> ContextBlock:
    content, fetched = read.content, read.fetched
    assert content is not None and fetched is not None
    key = normalize_url(read.url)
    return ContextBlock(
        n=0,
        text=section.text,
        score=score,
        payload={
            "kind": PRIORITY_PAGE_KIND,
            "source_type": "website",
            "bundle": "priority_page",
            # The organisation's own page, so the prompt marks it "official page".
            "source_authority": 1.0,
            "title": content.title or read.target.name,
            "section_heading": None if section.lead else section.heading,
            "source_url": fetched.final_url or read.url,
            "document_id": _doc_id(key),
            "chunk_id": f"{_doc_id(key)}:{index}",
            "priority_page": read.target.name,
            "priority_reason": read.target.reason,
            "content_hash": content.content_hash,
            "fetched_at": fetched.fetched_at.isoformat(timespec="seconds"),
            "stale": fetched.stale,
        },
    )


def _group_block(target: Target) -> ContextBlock:
    group = target.group
    assert group is not None
    lines = [group.name]
    if group.description:
        lines.append(group.description)
    for member in group.members:
        lines.append(f"- {member.name}: {member.description} ({member.url})")
    text = "\n".join(lines)
    return ContextBlock(
        n=0,
        text=text,
        score=1.0,
        payload={
            "kind": PRIORITY_PAGE_KIND,
            "source_type": "website",
            "bundle": "priority_page",
            "source_authority": 1.0,
            "title": group.name,
            "document_id": _doc_id(group.key),
            "chunk_id": f"{_doc_id(group.key)}:0",
            "priority_page": group.name,
            "priority_reason": target.reason,
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        },
    )


def _select(
    targets: Sequence[Target],
    reads: Sequence[PageRead],
    query_vector: Sequence[float],
    *,
    limit: int,
    embed: Embed | None,
) -> list[ContextBlock]:
    settings = get_settings()
    usable = [r for r in reads if r.content is not None and r.fetched is not None]
    flat: list[tuple[PageRead, int, Section]] = [
        (r, i, s) for r in usable for i, s in enumerate(r.content.sections)
    ]
    vectors = _vectors_for([s.text for _, _, s in flat], embed) if flat else []
    scores = {(id(r), i): _cosine(query_vector, v) for (r, i, _), v in zip(flat, vectors)}

    by_key = {r.target.key: r for r in usable}
    chosen: list[ContextBlock] = []
    taken: set[tuple[int, int]] = set()
    # Opening sections of what the question is about, strongest reason first.
    for target in match.ranked(targets):
        if len(chosen) >= limit:
            break
        if target.kind == GROUP_KIND:
            chosen.append(_group_block(target))
            continue
        read = by_key.get(target.key)
        if read is None or target.reason not in ABOUT:
            continue
        section = read.content.sections[0]
        chosen.append(_page_block(read, section, 0, scores.get((id(read), 0), 0.0)))
        taken.add((id(read), 0))
    # Then whatever else on those pages answers the question best.
    rest = sorted(
        ((scores.get((id(r), i), 0.0), r, i, s) for r, i, s in flat if (id(r), i) not in taken),
        key=lambda item: item[0], reverse=True,
    )
    for score, read, index, section in rest:
        if len(chosen) >= limit or score < settings.priority_section_floor:
            break
        chosen.append(_page_block(read, section, index, score))
    return chosen


# -- the entry points --------------------------------------------------------------------

def explicit_targets(
    question: str,
    *,
    theme: str | None = None,
    themes_listing: bool = False,
    registry: Registry | None = None,
) -> list[Target]:
    """The deterministic triggers for ``question``: cheap enough to run before
    routing, which needs them to decide whether a catalog answer may stand."""
    registry = registry or load_registry()
    if not registry.pages:
        return []
    return match.explicit(question, registry=registry, people=listed_people(registry),
                          theme=theme, themes_listing=themes_listing)


def gather(
    question: str,
    *,
    query_vector: Sequence[float],
    theme: str | None = None,
    themes_listing: bool = False,
    explicit: Sequence[Target] | None = None,
    registry: Registry | None = None,
    embed: Embed | None = None,
) -> PriorityEvidence:
    """Everything the priority pages contribute to ``question``. Never raises:
    a failure here costs the priority blocks, never the answer."""
    registry = registry or load_registry()
    evidence = PriorityEvidence(registry=registry, query_vector=list(query_vector or []),
                                embed=embed)
    if not registry.pages:
        return evidence
    settings = get_settings()
    try:
        targets = list(explicit) if explicit is not None else explicit_targets(
            question, theme=theme, themes_listing=themes_listing, registry=registry)
        people = [t for t in targets if t.reason == match.PERSON][:MAX_PEOPLE]
        others = [t for t in targets if t.reason != match.PERSON]
        targets = match.ranked([*people, *others])
        # A question for the list of themes is about no one theme, so a
        # description match would only add a page the answer does not need.
        lists_themes = any(t.page is not None and t.page.is_home for t in targets)
        if _page_count(targets) < settings.priority_max_pages and not lists_themes:
            found, evidence.similar_top = match.similar(query_vector, registry=registry, embed=embed)
            targets = match.ranked([*targets, *found])
        pages = [t for t in targets if t.kind != GROUP_KIND][: settings.priority_max_pages]
        groups = [t for t in targets if t.kind == GROUP_KIND][:1]
        evidence.targets = match.ranked([*pages, *groups])
        evidence.reads = _read_all(pages, registry)
        evidence.blocks = _select(evidence.targets, evidence.reads, evidence.query_vector,
                                  limit=settings.priority_max_blocks, embed=embed)
    except Exception:
        logger.warning("Priority pages failed for this question; answering from "
                       "the corpus alone.", exc_info=True)
        evidence.blocks = []
    evidence._note()
    return evidence
