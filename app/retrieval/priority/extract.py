"""A rendered Drupal page as titled sections of plain text.

One extractor serves every page on the list, because they share one theme:
whatever the page is — a taxonomy term assembled from Views, a people listing,
a profile, an ordinary node — its content sits between the
``region-content`` div and the footer regions, after the site header, the main
menu and the themes menu. Surveyed on all 59 pages on 2026-09-24; each gave
clean text from that cut.

Sections follow the page's own headings. On the Views-built pages a section
title is an ``h2`` carrying ``block-title`` or ``section-heading`` ("Projects",
"Team", "NEW IN CLIMATE CHANGE"), while the items inside a section use bare
``h2``–``h5`` for their own titles — so only the classed headings (and the
page's ``h1``) open a section, and the rest stay lines of text. A page with no
classed headings is one section per ``h1``, cut to size.

What is dropped: scripts, styles, forms, images, the targets of ordinary links
(the citation links the page itself), link-only chrome such as "Read more", and
anything before the ``h1`` — on the people pages that is the tab strip, printed
twice.

What is kept as a link, never read: documents. A PDF (or office file) linked
from the page is written into the text as ``label (URL)`` so the answer can
hand it over, and nothing here or anywhere downstream downloads it. When the
link's own text is chrome ("Read more"), the URL is attached to the line before
it, which on these pages is the item's title.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

from app.retrieval.priority.fetch import is_document_url

#: Longest section text kept as one block, in characters (~600 tokens). Longer
#: sections are cut at line boundaries: the announcements page is 76k characters.
MAX_SECTION_CHARS = 2400

_START = re.compile(r'<div[^>]+class="[^"]*\bregion-content\b[^"]*"', re.I)
_END = re.compile(
    r'<div[^>]+class="[^"]*\bregion-footer(?:-links)?\b[^"]*"|<footer\b', re.I
)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

_SKIP = frozenset({"script", "style", "noscript", "svg", "form", "button",
                   "select", "option", "textarea", "iframe", "template"})
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                   "link", "meta", "source", "track", "wbr"})
_BLOCK = frozenset({"p", "div", "li", "ul", "ol", "section", "article", "table",
                    "tr", "dd", "dt", "dl", "blockquote", "header", "aside",
                    "figure", "figcaption", "main"})
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_SECTION_CLASSES = ("block-title", "section-heading", "page-header")

#: Link and button labels that carry no content.
_CHROME = frozenset({
    "read more", "more", "read more »", "view more", "view all", "know more",
    "view brochures", "watch video", "download", "click here", "search", "menu",
})


@dataclass(frozen=True)
class Section:
    heading: str | None
    text: str
    #: The page's opening section — its title and introduction.
    lead: bool = False


@dataclass(frozen=True)
class Link:
    text: str
    href: str

    @property
    def is_document(self) -> bool:
        return is_document_url(self.href)


@dataclass(frozen=True)
class PageContent:
    title: str | None
    sections: tuple[Section, ...]
    links: tuple[Link, ...]
    content_hash: str
    #: Documents the page links to, labelled, in page order — for handing over
    #: as links. Never fetched.
    documents: tuple[Link, ...] = ()

    @property
    def text(self) -> str:
        return "\n\n".join(s.text for s in self.sections)


class _Parser(HTMLParser):
    """Emits ``("break", heading)`` and ``("line", text)`` events in order."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.events: list[tuple[str, str]] = []
        self.links: list[Link] = []
        self._skip = 0
        self._buf: list[str] = []
        self._heading: tuple[str, bool] | None = None  # (tag, opens a section)
        self._anchor: tuple[str, list[str]] | None = None
        self._cell = False
        #: Document URLs already written into the text, and those only ever
        #: linked from an image (a thumbnail with no caption of its own).
        self.documents: dict[str, str] = {}
        self._unlabelled: dict[str, None] = {}

    # -- helpers ---------------------------------------------------------------
    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip(" |")
        self._buf = []
        if text:
            self.events.append(("line", text))

    # -- parser hooks ------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        attr = dict(attrs)
        if tag in _HEADINGS:
            self._flush()
            classes = (attr.get("class") or "").split()
            opens = tag == "h1" or any(c in classes for c in _SECTION_CLASSES)
            self._heading = (tag, opens)
        elif tag in _BLOCK or tag == "br":
            self._flush()
        elif tag in ("td", "th"):
            if self._cell:
                self._buf.append(" | ")
            self._cell = True
        elif tag == "a":
            self._anchor = (attr.get("href") or "", [])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" and not self._skip:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip or tag in _VOID:
            return
        if tag == "a" and self._anchor is not None:
            href, parts = self._anchor
            text = re.sub(r"\s+", " ", "".join(parts)).strip()
            self._anchor = None
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                return
            url = urljoin(self.base_url, href.strip())
            if text:
                self.links.append(Link(text, url))
            if is_document_url(url):
                if text and url not in self.documents:
                    # "Read more" says nothing; the item's title is the line before.
                    label = text if not _is_chrome(text) else next(
                        (t for kind, t in reversed(self.events) if kind == "line"), text
                    )
                    self.documents[url] = label
                    self._buf.append(f" ({url})")
                elif not text:
                    self._unlabelled.setdefault(url, None)
        elif tag in _HEADINGS and self._heading is not None:
            heading_tag, opens = self._heading
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            self._buf = []
            self._heading = None
            if text:
                kind = "h1" if heading_tag == "h1" else "break" if opens else "line"
                self.events.append((kind, text))
        elif tag == "tr":
            self._cell = False
            self._flush()
        elif tag in _BLOCK:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self._buf.append(data)
        if self._anchor is not None:
            self._anchor[1].append(data)

    def close(self) -> None:
        super().close()
        self._flush()
        # A document reached only through an image still gets named, by its file.
        orphans = [u for u in self._unlabelled if u not in self.documents]
        if orphans:
            self.events.append(("break", "Documents linked on this page"))
            for url in orphans:
                label = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
                self.documents[url] = label
                self.events.append(("line", f"{label} ({url})"))


def _main_region(html: str) -> str:
    start = _START.search(html)
    begin = start.start() if start else 0
    end = _END.search(html, begin)
    return html[begin:end.start() if end else len(html)]


#: A chrome label carrying a document link, as the parser writes it.
_CHROME_LINK = re.compile(r"^(?P<label>[^()]*?)\s*\((?P<url>https?://[^()\s]+)\)$")


def _is_chrome(text: str) -> bool:
    return text.lower().strip().rstrip(" .:»›>") in _CHROME


def _clean_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        link = _CHROME_LINK.match(line)
        if link and _is_chrome(link.group("label")):
            # "Read more (…pdf)": the document belongs to the item above it.
            if out and "(" + link.group("url") + ")" not in out[-1]:
                out[-1] = f"{out[-1]} ({link.group('url')})"
            elif not out:
                out.append(link.group("url"))
            continue
        if _is_chrome(line) or not re.search(r"\w", line):
            continue
        if out and out[-1] == line:
            continue
        out.append(line)
    return out


def _split(lines: list[str], limit: int) -> list[str]:
    """``lines`` joined into texts of at most ``limit`` characters, cut only
    between lines."""
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) > limit:
            pieces.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        pieces.append("\n".join(current))
    return pieces


def extract(html: str, base_url: str, *, max_chars: int = MAX_SECTION_CHARS) -> PageContent:
    """The page's content as sections. Never raises on bad markup — the parser
    is tolerant, and a page it cannot read yields no sections, which the caller
    treats as a failed fetch."""
    parser = _Parser(base_url)
    try:
        parser.feed(_main_region(html))
        parser.close()
    except Exception:  # pragma: no cover - HTMLParser is lenient; defence in depth
        pass
    events = parser.events
    # Content before the page's own title is navigation (the people pages' tab
    # strip); the h1 is where the page begins.
    first_h1 = next((i for i, (kind, _) in enumerate(events) if kind == "h1"), None)
    if first_h1 is not None:
        events = events[first_h1:]

    grouped: list[tuple[str | None, list[str]]] = []
    for kind, text in events:
        if kind in ("h1", "break"):
            grouped.append((text, []))
        elif grouped:
            grouped[-1][1].append(text)
        else:
            grouped.append((None, [text]))

    sections: list[Section] = []
    for index, (heading, lines) in enumerate(grouped):
        lines = _clean_lines(lines)
        if heading and lines and lines[0] == heading:
            # A profile prints the name as its title and again beneath it.
            lines = lines[1:]
        if not lines and index > 0:
            continue
        # Every piece is led by its heading, so a piece read alone still says
        # what it is part of ("Team", "Projects").
        room = max_chars - (len(heading) + 1 if heading else 0)
        for piece in _split(lines, room) or [""]:
            text = "\n".join(part for part in (heading, piece) if part)
            if text:
                sections.append(Section(heading, text, lead=not sections))

    title_match = _TITLE.search(html)
    page_title = (
        unescape(re.sub(r"\s+", " ", title_match.group(1))).split("|")[0].strip()
        if title_match else None
    )
    title = next((h for h, _ in grouped if h), None) or page_title
    digest = hashlib.sha256("\n\n".join(s.text for s in sections).encode("utf-8")).hexdigest()
    return PageContent(
        title=title,
        sections=tuple(sections),
        links=tuple(parser.links),
        content_hash=digest,
        documents=tuple(Link(label, url) for url, label in parser.documents.items()),
    )
