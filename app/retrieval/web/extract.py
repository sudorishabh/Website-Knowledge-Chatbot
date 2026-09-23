"""A fetched page, read into a structured, provenance-carrying document.

:func:`extract` turns a :class:`~app.retrieval.web.fetch.FetchedPage` into a
:class:`WebDocument`: the metadata a citation needs (title, authors, the date the
page states and *where that date came from*, the canonical address) and the
content as an ordered list of :class:`WebBlock` — paragraphs, tables and
captions, each under the heading it sits beneath and, for a PDF, on its page.

HTML
----
Only the page's content is kept. When the page marks it (``<main>``,
``<article>``, ``role="main"``) only that is read; everything is read otherwise,
minus the furniture every page repeats: navigation, headers, footers, sidebars,
forms, share bars, cookie banners. Hidden text is dropped — anything ``hidden``,
``aria-hidden`` or styled invisible — because text a reader cannot see is not
evidence, and it is the classic carrier for instructions aimed at a model.

The date is taken from the page's own metadata, strongest first — JSON-LD
``datePublished``, then ``article:published_time``, then the citation and Dublin
Core tags, then a ``<time>`` element — and the source is recorded beside it, so
a date the answer states can be traced to what the page actually said.

PDF
---
Read through :mod:`app.core.pdf_text`, the same page reader and normalization
ingestion uses: one block per paragraph, each carrying its page number. The date
comes from the PDF's own information dictionary and is labelled as such, since
authoring tools stamp it and it is often the date a file was saved rather than
published.

Untrusted text
--------------
Every block passes :func:`sanitize`: control and zero-width characters are
removed, and sentences written *to a model* rather than to a reader ("ignore all
previous instructions", chat-template markers, "you are now...") are dropped and
counted. A page is evidence about its subject; it never gets to speak to the
system reading it. The prompt treats web text as data as well — this is the
first of two layers, not the only one.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Any, NamedTuple
from urllib.parse import urljoin, urlsplit

from app.config import get_settings
from app.core.dates import stated_day
from app.core.pdf_text import read_pages
from app.retrieval.web.fetch import HTML, PDF, TEXT, FetchedPage
from app.retrieval.web.safety import domain_of

logger = logging.getLogger(__name__)

__all__ = ["WebBlock", "WebDocument", "extract", "sanitize"]

PARAGRAPH = "paragraph"
TABLE = "table"
CAPTION = "caption"

# A page longer than this is read to here and marked truncated: no article is
# this long, and nothing downstream should pay for reading one that is.
_MAX_CHARS = 200_000


# --- the document -----------------------------------------------------------


@dataclass
class WebBlock:
    """One unit of content: a paragraph, a table (rows as ``a | b | c`` lines)
    or a caption, with the heading it sits beneath and its PDF page."""

    text: str
    kind: str = PARAGRAPH
    heading: str | None = None
    page: int | None = None


@dataclass
class WebDocument:
    """A fetched page as evidence: what it says, and everything needed to cite it.

    ``published`` is the date the document states for itself, and
    ``date_source`` says where it was read ("json_ld", "meta:article:
    published_time", "pdf_metadata", ...). ``fetched_at`` is when this copy was
    read — a retrieval date, never to be reported as a publication date.
    """

    url: str
    final_url: str
    canonical_url: str
    domain: str
    kind: str
    fetched_at: str
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    published: str | None = None
    date_source: str | None = None
    modified: str | None = None
    site_name: str | None = None
    description: str | None = None
    language: str | None = None
    blocks: list[WebBlock] = field(default_factory=list)
    page_count: int | None = None
    truncated: bool = False
    #: What sanitization removed, by kind — for the trace, not the answer.
    removed: dict[str, int] = field(default_factory=dict)

    @property
    def has_tables(self) -> bool:
        return any(b.kind == TABLE for b in self.blocks)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WebDocument":
        fields = {k: data.get(k) for k in cls.__dataclass_fields__ if k in data}
        fields["blocks"] = [WebBlock(**b) for b in data.get("blocks") or []]
        fields["authors"] = list(data.get("authors") or [])
        fields["removed"] = dict(data.get("removed") or {})
        return cls(**fields)


# --- untrusted text ---------------------------------------------------------

# Control characters (tab and newline kept) and the zero-width / bidi-override
# characters used to hide text from a reader while leaving it for a model.
_INVISIBLE = re.compile(
    "[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f"
    "​-‏‪-‮⁠-⁤﻿]"
)

# Wording addressed to a model, not a reader. Deliberately narrow: each pattern
# is an instruction or a chat-template token, not a topic, so an article that
# discusses AI keeps its sentences.
_INJECTION = re.compile(
    r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+|your\s+)*"
    r"(?:previous|prior|above|earlier|preceding|system)\s+"
    r"(?:instructions?|prompts?|directions?|rules|messages?|context)\b"
    r"|\byou\s+are\s+now\b|\bact\s+as\s+(?:an?\s+)?(?:ai|assistant|chatbot|language\s+model)\b"
    r"|\b(?:system|developer)\s+prompt\b|\bjailbreak\b|\bdeveloper\s+mode\b"
    r"|<\|(?:im_start|im_end|system|user|assistant|endoftext)\|>"
    r"|\[/?INST\]|<<\s*/?SYS\s*>>"
    r"|^\s*#{2,}\s*(?:system|assistant|instructions?)\b"
    r"|^\s*(?:system|assistant)\s*:"
    r"|\b(?:do\s+not|don't)\s+(?:tell|inform|reveal\s+to)\s+the\s+user\b",
    re.IGNORECASE | re.MULTILINE,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def sanitize(text: str) -> tuple[str, dict[str, int]]:
    """``text`` with invisible characters and model-addressed sentences removed,
    and a count of each removal."""
    removed: dict[str, int] = {}
    cleaned, invisible = _INVISIBLE.subn("", text or "")
    if invisible:
        removed["invisible_chars"] = invisible
    # Judged sentence by sentence rather than on the whole text, so a pattern
    # anchored at the start ("SYSTEM: ...") is caught mid-paragraph too.
    kept_lines: list[str] = []
    dropped = 0
    for line in cleaned.split("\n"):
        sentences = _SENTENCE.split(line)
        kept = [s for s in sentences if not _INJECTION.search(s)]
        dropped += len(sentences) - len(kept)
        if kept:
            kept_lines.append(" ".join(kept))
    if dropped:
        cleaned = "\n".join(kept_lines)
        removed["instruction_sentences"] = dropped
    return cleaned.strip(), removed


# --- HTML -------------------------------------------------------------------

_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "template", "svg", "canvas", "iframe", "object",
    "embed", "form", "button", "select", "textarea", "nav", "header", "footer",
    "aside", "dialog", "menu",
})
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
})
_MAIN_TAGS = frozenset({"main", "article"})
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "blockquote", "pre", "li", "dd", "dt", "address",
    "figure", "ul", "ol", "dl",
})
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_SKIP_ROLES = frozenset({
    "navigation", "banner", "contentinfo", "complementary", "search", "dialog",
    "alert", "menu", "menubar",
})
# Page furniture recognised by class or id. Matched on whole class tokens'
# words, so "node__content" is kept and "site-footer" is not.
_FURNITURE = re.compile(
    r"(?:^|[\s_-])(?:nav|navbar|menu|breadcrumbs?|footer|header|sidebar|"
    r"cookies?|consent|share|sharing|social|newsletter|subscribe|popup|modal|"
    r"advert|ads|banner|skip-link|pager|pagination|sr-only|visually-hidden|"
    r"screen-reader-text|comments?)(?:$|[\s_-])",
    re.IGNORECASE,
)
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px)?\s*(?:;|$)"
    r"|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)",
    re.IGNORECASE,
)

# Meta names carrying a publication date, strongest first. Each is recorded as
# the date's source when it wins.
_DATE_META = (
    "article:published_time", "citation_publication_date", "citation_date",
    "dc.date.issued", "dcterms.issued", "dc.date", "dcterms.date",
    "datepublished", "publish-date", "pubdate", "date",
)
_AUTHOR_META = ("citation_author", "author", "dc.creator", "dcterms.creator")


def _skippable(tag: str, attrs: dict[str, str]) -> bool:
    if tag in _SKIP_TAGS:
        return True
    if "hidden" in attrs or attrs.get("aria-hidden", "").lower() == "true":
        return True
    if _HIDDEN_STYLE.search(attrs.get("style", "")):
        return True
    if attrs.get("role", "").lower() in _SKIP_ROLES:
        return True
    marker = f"{attrs.get('class', '')} {attrs.get('id', '')}"
    return bool(marker.strip()) and bool(_FURNITURE.search(marker))


# Where a page states its date or its authors in markup rather than metadata —
# Drupal's ``created--on`` and ``field--name-field-article-authors``, a theme's
# ``byline``. Matched on class words, like ``_FURNITURE``.
_DATE_CLASS = re.compile(
    r"(?:^|[\s_-])(?:date|created|published|submitted|posted|pubdate)(?:$|[\s_-])",
    re.IGNORECASE,
)
_AUTHOR_CLASS = re.compile(
    r"(?:^|[\s_-])(?:authors?|byline|contributors?)(?:$|[\s_-])", re.IGNORECASE
)
# What an element's text is captured as, instead of becoming content.
_AS_DATE = "date"
_AS_AUTHOR = "author"


class _Frame(NamedTuple):
    """One open element: whether its content is skipped, whether it is inside
    the page's main content, and whether its text is a date or author label."""

    tag: str
    skipping: bool
    main: bool
    capture: str | None


_ROOT = _Frame("", False, False, None)


def _capture_of(attrs: dict[str, str]) -> str | None:
    marker = attrs.get("class", "")
    if _AUTHOR_CLASS.search(marker):
        return _AS_AUTHOR
    if _DATE_CLASS.search(marker):
        return _AS_DATE
    return None


class _PageParser(HTMLParser):
    """One pass over the page: metadata into ``meta``, content into blocks.

    Tolerant of real-world HTML — unclosed paragraphs, stray end tags, void
    elements written without a slash — by keeping an explicit element stack and
    closing back to the matching open tag.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.json_ld: list[str] = []
        self.title = ""
        self.canonical = ""
        self.language = ""
        self.times: list[tuple[str, bool]] = []          # (<time datetime>, in main)
        self.date_labels: list[tuple[str, bool]] = []    # (text of a date element, in main)
        self.author_labels: list[str] = []
        self.blocks: list[tuple[WebBlock, bool]] = []   # (block, inside main content)
        self._stack: list[_Frame] = []
        self._buffer: list[str] = []
        self._heading: str | None = None
        self._heading_buffer: list[str] | None = None
        self._table: list[list[str]] | None = None
        self._table_depth = 0
        self._cell: list[str] | None = None
        self._caption: list[str] | None = None
        self._in_title = False
        self._in_json_ld = False
        self._json_buffer: list[str] = []

    # -- state ------------------------------------------------------------
    @property
    def _top(self) -> _Frame:
        return self._stack[-1] if self._stack else _ROOT

    @property
    def _skipping(self) -> bool:
        return self._top.skipping

    @property
    def _in_main(self) -> bool:
        return self._top.main

    def _add(self, block: WebBlock, main: bool) -> None:
        self.blocks.append((block, main))

    def _flush(self, frame: _Frame | None = None) -> None:
        """Close the running paragraph — as content, or as the date or author
        label its element marks it as. ``frame`` is the element the text belongs
        to; it defaults to the one currently open."""
        frame = frame or self._top
        text = " ".join("".join(self._buffer).split())
        self._buffer = []
        if not text:
            return
        if frame.capture == _AS_AUTHOR:
            self.author_labels.append(text)
        elif frame.capture == _AS_DATE:
            self.date_labels.append((text, frame.main))
        else:
            self._add(WebBlock(text=text, heading=self._heading), frame.main)

    # -- parser callbacks -------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and a.get("lang"):
            self.language = a["lang"].strip()
        if tag == "meta":
            name = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if name and a.get("content"):
                self.meta.setdefault(name, []).append(a["content"].strip())
            return
        if tag == "link":
            if "canonical" in a.get("rel", "").lower().split() and a.get("href"):
                self.canonical = self.canonical or a["href"].strip()
            return
        if tag == "script":
            self._in_json_ld = "ld+json" in a.get("type", "").lower()
        if tag == "title" and not self._stack_has("body"):
            self._in_title = True
        if tag == "time" and a.get("datetime") and not self._skipping:
            self.times.append((a["datetime"], self._in_main))
        if tag in _VOID_TAGS:
            if tag == "br" and not self._skipping:
                self._buffer.append(" ")
            return

        parent = self._top
        # An article's own <header> holds its title and date, not site chrome.
        furniture = _skippable(tag, a) and not (tag == "header" and parent.main)
        skipping = parent.skipping or furniture
        main = parent.main or tag in _MAIN_TAGS or a.get("role", "").lower() == "main"
        capture = parent.capture or (None if skipping else _capture_of(a))
        if not skipping:
            if tag in _HEADINGS:
                self._flush()
                self._heading_buffer = []
            elif tag == "table":
                self._table_depth += 1
                if self._table_depth == 1:     # a nested table stays part of its parent
                    self._flush()
                    self._table = []
            elif tag == "tr" and self._table is not None:
                self._table.append([])
            elif tag in ("td", "th") and self._table is not None:
                self._cell = []
            elif tag in ("figcaption", "caption"):
                self._flush()
                self._caption = []
            elif tag in _BLOCK_TAGS or tag in _MAIN_TAGS or capture != parent.capture:
                self._flush()
        self._stack.append(_Frame(tag, skipping, main, capture))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def _stack_has(self, tag: str) -> bool:
        return any(frame.tag == tag for frame in self._stack)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "script" and self._in_json_ld:
            self.json_ld.append("".join(self._json_buffer))
            self._json_buffer, self._in_json_ld = [], False
        if not self._stack_has(tag):
            return                                   # a stray end tag
        while self._stack:
            frame = self._stack.pop()
            if not frame.skipping:
                self._close(frame)
            if frame.tag == tag:
                break

    def _close(self, frame: _Frame) -> None:
        """Finish an element that just closed."""
        tag = frame.tag
        if tag in _HEADINGS and self._heading_buffer is not None:
            heading = " ".join("".join(self._heading_buffer).split())
            self._heading_buffer = None
            if heading:
                self._heading = heading
        elif tag in ("td", "th") and self._cell is not None and self._table is not None:
            cell = " ".join("".join(self._cell).split())
            if not self._table:
                self._table.append([])
            self._table[-1].append(cell)
            self._cell = None
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1
            if self._table_depth == 0 and self._table is not None:
                rows = [" | ".join(r) for r in self._table if any(c for c in r)]
                self._table = None
                if rows:
                    self._add(WebBlock(text="\n".join(rows), kind=TABLE,
                                       heading=self._heading), frame.main)
        elif tag in ("figcaption", "caption") and self._caption is not None:
            text = " ".join("".join(self._caption).split())
            self._caption = None
            if text:
                self._add(WebBlock(text=text, kind=CAPTION, heading=self._heading),
                          frame.main)
        elif (tag in _BLOCK_TAGS or tag in _MAIN_TAGS
              or frame.capture != self._top.capture):
            # A label element closes its own text, so an author's name cannot
            # run into the paragraph after it.
            self._flush(frame)

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._json_buffer.append(data)
            return
        if self._in_title:
            self.title += data
            return
        if self._skipping:
            return
        if self._heading_buffer is not None:
            self._heading_buffer.append(data)
        elif self._cell is not None:
            self._cell.append(data)
        elif self._caption is not None:
            self._caption.append(data)
        elif self._table is None:
            self._buffer.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def _decode(content: bytes) -> str:
    """The page's text. A byte-order mark or a declared charset decides; UTF-8
    otherwise, with a Windows-1252 fallback for the legacy pages that are
    neither and say nothing."""
    if content.startswith(b"\xef\xbb\xbf"):
        return content[3:].decode("utf-8", errors="replace")
    declared = re.search(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9_\-]+)", content[:4096],
                         re.IGNORECASE)
    if declared:
        try:
            return content.decode(declared.group(1).decode("ascii"), errors="replace")
        except LookupError:
            pass
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


def _json_ld_objects(raw: list[str]) -> list[dict[str, Any]]:
    """Every JSON-LD object on the page, ``@graph`` members included."""
    out: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            out.append(node)
            walk(node.get("@graph"))

    for text in raw:
        try:
            walk(json.loads(text))
        except ValueError:
            continue
    return out


def _names(value: Any) -> list[str]:
    """Author names from a JSON-LD author value: a string, an object or a list."""
    if isinstance(value, list):
        return [n for item in value for n in _names(item)]
    if isinstance(value, dict):
        value = value.get("name")
    name = " ".join(str(value or "").split())
    return [name] if name and not name.lower().startswith("http") else []


def _first(meta: dict[str, list[str]], *names: str) -> str | None:
    for name in names:
        for value in meta.get(name, []):
            if value.strip():
                return value.strip()
    return None


def _published(parser: _PageParser, ld: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    for obj in ld:
        day = stated_day(obj.get("datePublished"))
        if day:
            return day, "json_ld"
    for name in _DATE_META:
        day = stated_day(_first(parser.meta, name))
        if day:
            return day, f"meta:{name}"
    # A <time> in the content itself before one elsewhere — a sidebar of recent
    # items carries dates too, and they belong to other documents. The same
    # preference, last of all, for the text of an element whose class names it
    # a date: how Drupal pages, TERI's among them, show a publication date.
    for values, source in ((parser.times, "time_element"),
                           (parser.date_labels, "page_markup")):
        for value, _ in sorted(values, key=lambda item: not item[1]):
            day = stated_day(value)
            if day:
                return day, source
    return None, None


def _author_labels(labels: list[str]) -> list[str]:
    """Names from byline/author elements, minus the labels around them."""
    names: list[str] = []
    for text in labels:
        text = re.sub(r"^(?:by|authors?)\s*[:\-]?\s+", "", text, flags=re.IGNORECASE)
        if 2 < len(text) <= 80 and not text.endswith(":") and "http" not in text.lower():
            names.append(text)
    return names


def _canonical(raw: str, base: str) -> str:
    """The page's declared canonical address, when it is a usable web URL."""
    if raw:
        absolute = urljoin(base, raw)
        if urlsplit(absolute).scheme in ("http", "https"):
            return absolute
    return base


def _html_document(page: FetchedPage) -> WebDocument:
    parser = _PageParser()
    text = _decode(page.content)
    truncated = len(text) > _MAX_CHARS * 5          # markup outweighs text ~5:1
    parser.feed(text[: _MAX_CHARS * 5])
    parser.close()

    ld = _json_ld_objects(parser.json_ld)
    main = [block for block, in_main in parser.blocks if in_main]
    blocks = main or [block for block, _ in parser.blocks]

    authors: list[str] = []
    for obj in ld:
        authors.extend(_names(obj.get("author")))
    for name in _AUTHOR_META:
        authors.extend(v for v in parser.meta.get(name, []) if not v.lower().startswith("http"))
    authors.extend(_author_labels(parser.author_labels))
    published, date_source = _published(parser, ld)
    modified = next((stated_day(o.get("dateModified")) for o in ld if o.get("dateModified")),
                    None) or stated_day(_first(parser.meta, "article:modified_time"))
    headline = next((str(o["headline"]).strip() for o in ld if o.get("headline")), None)
    h1 = next((b.heading for b in blocks if b.heading), None)
    canonical = _canonical(parser.canonical or (_first(parser.meta, "og:url") or ""),
                           page.final_url)
    return WebDocument(
        url=page.url, final_url=page.final_url, canonical_url=canonical,
        domain=domain_of(canonical), kind=HTML, fetched_at=page.fetched_at,
        title=(_first(parser.meta, "og:title", "citation_title", "dc.title") or headline
               or " ".join(parser.title.split()) or h1),
        authors=list(dict.fromkeys(a for a in authors if a)),
        published=published, date_source=date_source, modified=modified,
        site_name=_first(parser.meta, "og:site_name"),
        description=_first(parser.meta, "og:description", "description"),
        language=parser.language or None,
        blocks=blocks, truncated=truncated,
    )


# --- PDF and plain text -----------------------------------------------------

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def _paragraphs(text: str) -> list[str]:
    return [" ".join(p.split()) for p in _PARAGRAPH_BREAK.split(text or "") if p.strip()]


_NUMBERED_HEADING = re.compile(r"^\d+(?:\.\d+)*\.?\s+\S")
_SMALL_WORDS = frozenset({"a", "an", "and", "as", "at", "by", "for", "in", "of", "on",
                          "or", "the", "to", "with", "from", "vs"})


def _looks_like_heading(line: str) -> bool:
    """A short, unpunctuated, title-shaped line: "Executive Summary",
    "2.1 Source Apportionment", "KEY FINDINGS". Deliberately strict — a missed
    heading costs a label on a citation, a false one mislabels a passage."""
    words = line.split()
    if not (1 <= len(words) <= 12) or not 3 <= len(line) <= 90:
        return False
    if line[-1] in ".,;:!?" or not any(c.isalpha() for c in line):
        return False
    if _NUMBERED_HEADING.match(line) or line.isupper():
        return True
    capitalised = [w for w in words if w.lower() not in _SMALL_WORDS]
    return bool(capitalised) and all(w[:1].isupper() or w[:1].isdigit() for w in capitalised)


def _pdf_page_blocks(text: str, page: int, heading: str | None) -> tuple[list[WebBlock], str | None]:
    """A page's text as paragraph blocks under the running section heading.

    PyMuPDF's page text is one visual line per line, with no blank line between
    paragraphs, so paragraphs are closed where a line ends a sentence (or at a
    blank line, when there is one). A title-shaped line at a paragraph start
    becomes the heading for what follows, and carries over to the next page.
    """
    blocks: list[WebBlock] = []
    lines: list[str] = []

    def close() -> None:
        if lines:
            blocks.append(WebBlock(text=" ".join(lines), heading=heading, page=page))
            lines.clear()

    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not line:
            close()
            continue
        if not lines and _looks_like_heading(line):
            heading = line
            continue
        lines.append(line)
        if line[-1] in ".!?":
            close()
    close()
    return blocks, heading


# Metadata titles that name a file or a template, not the document.
_JUNK_TITLE = re.compile(r"^(?:microsoft\s+word\s*-|untitled)|\.(?:docx?|pdf|indd|pptx?)$",
                         re.IGNORECASE)


def _usable_title(title: str | None) -> str | None:
    title = " ".join((title or "").split())
    return title if len(title) >= 4 and not _JUNK_TITLE.search(title) else None


def _pdf_document(page: FetchedPage) -> WebDocument:
    settings = get_settings()
    pdf = read_pages(
        page.content,
        max_pages=int(settings.web_pdf_max_pages),
        drop_number_soup=bool(settings.pdf_drop_number_soup),
        running_header_min_fraction=float(settings.pdf_running_header_min_fraction),
    )
    blocks: list[WebBlock] = []
    heading: str | None = None
    for p in pdf.pages:
        page_blocks, heading = _pdf_page_blocks(p.text, p.page_number, heading)
        blocks.extend(page_blocks)
    # The cover's first title-shaped line, when the file's metadata names nothing.
    cover = pdf.pages[0].text.splitlines() if pdf.pages else []
    first_heading = next((" ".join(line.split()) for line in cover
                          if _looks_like_heading(" ".join(line.split()))), None)
    meta = pdf.metadata
    published = stated_day(meta.get("creationDate"))
    first_line = next((b.text for b in blocks if len(b.text) <= 200), None)
    authors = [a.strip() for a in re.split(r"[;,]|\band\b", meta.get("author", "")) if a.strip()]
    return WebDocument(
        url=page.url, final_url=page.final_url, canonical_url=page.final_url,
        domain=domain_of(page.final_url), kind=PDF, fetched_at=page.fetched_at,
        title=_usable_title(meta.get("title")) or first_heading or first_line,
        authors=authors,
        published=published, date_source="pdf_metadata" if published else None,
        modified=stated_day(meta.get("modDate")),
        blocks=blocks, page_count=pdf.page_count, truncated=pdf.truncated,
    )


def _text_document(page: FetchedPage) -> WebDocument:
    text = _decode(page.content)
    blocks = [WebBlock(text=p) for p in _paragraphs(text[:_MAX_CHARS])]
    return WebDocument(
        url=page.url, final_url=page.final_url, canonical_url=page.final_url,
        domain=domain_of(page.final_url), kind=TEXT, fetched_at=page.fetched_at,
        title=blocks[0].text[:200] if blocks else None,
        blocks=blocks, truncated=len(text) > _MAX_CHARS,
    )


# --- entry point ------------------------------------------------------------


def _sanitized(document: WebDocument) -> WebDocument:
    """Every block and the title passed through :func:`sanitize`; blocks that
    sanitize to nothing are dropped, and the text is capped at ``_MAX_CHARS``."""
    kept: list[WebBlock] = []
    totals: dict[str, int] = {}
    size = 0
    for block in document.blocks:
        text, removed = sanitize(block.text)
        for key, count in removed.items():
            totals[key] = totals.get(key, 0) + count
        if not text:
            continue
        if size + len(text) > _MAX_CHARS:
            document.truncated = True
            break
        size += len(text)
        block.text = text
        if block.heading:
            block.heading = sanitize(block.heading)[0] or None
        kept.append(block)
    document.blocks = kept
    if document.title:
        document.title = sanitize(document.title)[0] or None
    document.removed = totals
    return document


def extract(page: FetchedPage) -> WebDocument:
    """The fetched page as a :class:`WebDocument`. Raises what the underlying
    reader raises on a document it cannot read; the caller skips that page."""
    if page.kind == PDF:
        document = _pdf_document(page)
    elif page.kind == TEXT:
        document = _text_document(page)
    else:
        document = _html_document(page)
    return _sanitized(document)
