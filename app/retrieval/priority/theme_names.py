"""Themes named by part of their name: "the biofuel theme", "the marine theme".

A theme page is named outright by its full name ("climate change"), or by that
name with "theme" after it ("the water theme"); see
:attr:`app.retrieval.priority.registry.PriorityPage.match_names`. People also
name a theme by part of its name, and that named nothing. Measured 2026-10-21
over 25 such questions, none reached a theme page: "tell me about biofuel
theme" was summarised from the one catalog document tagged with one of the
three biofuel themes, "the marine theme" missed "Marine and Coastal", and "the
city theme" missed "Cities".

A :class:`Mention` is the words the question puts beside "theme" or
"thematic" ("the biofuel theme", "the theme on biofuels"), or the theme query
understanding extracted, together with the theme pages whose names hold every
one of those words. Words compare loosely: a plural, one typo in a word of
five letters or more, or a shared stem ("agricultural" and "agriculture"). A
page whose name is those words and no more is the one meant: "environmental"
is the Environment theme, not "Environment and Public Health".

A mention that fits several pages is answered one of two ways, by the caller:
asked about ("Which did you mean?") when the question says "theme" and means
one, or read whole when it says "themes" or names no theme at all.

Nothing here touches the network.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

from app.retrieval.priority.registry import (
    THEME,
    TYPO_MIN_LETTERS,
    PriorityPage,
    Registry,
    normalize_text,
    one_edit_apart,
)

#: Words that say a theme is meant. "themes" asks for several.
_THEME_WORDS = frozenset({"theme", "themes", "thematic"})
#: Words after "theme" that put its name after it: "the theme on biofuels".
_NAME_FOLLOWS = frozenset({"on", "of", "for", "about", "called", "named"})
#: Words in a theme's name that name nothing on their own.
_FILLER = frozenset({"and", "for", "of", "the", "teri"})
#: The most pages one mention may fit. More, and the words are too common to
#: name a theme ("sustainable"); four is also the most a question back can list.
MAX_FITS = 4
#: Shortest stem two words must share to be one word ("agricultur").
_MIN_STEM = 6
#: Share of the shorter word the stem must cover.
_STEM_SHARE = 0.8
#: Words in a reply that take every page offered.
_EVERY = frozenset({"all", "both", "each", "every", "everything"})
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_NUMBER = re.compile(r"(\d)(?:st|nd|rd|th)?")


@dataclass(frozen=True)
class Mention:
    """Words naming a theme in part, and the theme pages they fit."""

    #: The words, normalized: ("biofuel",).
    words: tuple[str, ...]
    #: Every page they fit, in the list's order.
    pages: tuple[PriorityPage, ...]
    #: Said with "theme" or "thematic"; False for understanding's theme alone.
    named: bool = True
    #: Said with "themes": several are asked for.
    plural: bool = False

    @property
    def phrase(self) -> str:
        return " ".join(self.words)

    @property
    def ambiguous(self) -> bool:
        """Whether the question means one theme and these words fit several."""
        return self.named and not self.plural and len(self.pages) > 1

    def narrowed(self, reply: str) -> Mention:
        """The pages a reply to "which did you mean?" chooses: by number ("2",
        "the second"), by name ("microbial"), or all of them ("all", "both").
        All of them when the reply chooses none, since the user has already
        been asked once."""
        chosen = _chosen(self.pages, reply)
        return replace(self, pages=chosen) if chosen else self


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return f"{word[:-3]}y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def same_word(a: str, b: str) -> bool:
    """Whether ``a`` and ``b`` are one word, give or take a plural, a typo or
    an ending ("agricultural", "agriculture")."""
    a, b = _singular(a), _singular(b)
    if a == b:
        return True
    shorter = min(len(a), len(b))
    if shorter >= TYPO_MIN_LETTERS and one_edit_apart(a, b):
        return True
    stem = len(os.path.commonprefix([a, b]))
    return stem >= _MIN_STEM and stem >= _STEM_SHARE * shorter


def _name_words(page: PriorityPage) -> tuple[str, ...]:
    return tuple(w for w in normalize_text(page.topic).split() if w not in _FILLER)


def _theme_pages(registry: Registry) -> list[PriorityPage]:
    return [p for p in registry.pages if p.kind == THEME]


def _vocabulary(registry: Registry) -> frozenset[str]:
    return frozenset(w for page in _theme_pages(registry) for w in _name_words(page))


def _fits(words: Sequence[str], registry: Registry) -> tuple[PriorityPage, ...]:
    """The theme pages whose names hold every one of ``words``; only the one
    named by exactly those words, when there is one."""
    wanted = [w for w in words if w not in _FILLER]
    if not wanted:
        return ()
    pages = [
        page for page in _theme_pages(registry)
        if all(any(same_word(w, n) for n in _name_words(page)) for w in wanted)
    ]
    exact = [p for p in pages if len(_name_words(p)) == len(wanted)]
    return tuple(exact or pages)


def _run(tokens: Iterable[str], vocabulary: frozenset[str]) -> list[str]:
    """The leading tokens that are words of some theme's name, joined by "and"."""
    run: list[str] = []
    for token in tokens:
        if token == "and" and run:
            run.append(token)
        elif any(same_word(token, w) for w in vocabulary):
            run.append(token)
        else:
            break
    while run and run[-1] == "and":
        run.pop()
    return run


def _phrases(tokens: Sequence[str], vocabulary: frozenset[str]) -> list[tuple[tuple[str, ...], bool]]:
    """Each run of name words beside a theme word, and whether it said "themes"."""
    found: list[tuple[tuple[str, ...], bool]] = []
    for i, token in enumerate(tokens):
        if token not in _THEME_WORDS:
            continue
        run = _run(reversed(tokens[:i]), vocabulary)[::-1]
        if not run and tokens[i + 1:i + 2] and tokens[i + 1] in _NAME_FOLLOWS:
            after = list(tokens[i + 2:])
            if after[:1] == ["the"]:
                after = after[1:]
            run = _run(after, vocabulary)
        if run:
            found.append((tuple(run), token == "themes"))
    return found


def _mentions_of(words: tuple[str, ...], registry: Registry, *, named: bool,
                 plural: bool, whole: bool = True) -> list[Mention]:
    """``words`` as one mention, or, when together they fit no page, each part
    between "and"s as its own: "the climate change and energy themes".

    "themes" after words that fit one page is not a name: "TERI's research
    themes" asks for the list of themes, not for the research centre's page."""
    pages = _fits(words, registry)
    if pages:
        if whole and plural and len(pages) < 2:
            return []
        return [Mention(words, pages, named=named, plural=plural)]
    parts = " ".join(words).split(" and ")
    if len(parts) < 2:
        return []
    return [m for part in parts
            for m in _mentions_of(tuple(part.split()), registry, named=named,
                                  plural=plural, whole=False)]


def mentions(text: str, registry: Registry, *, facet: str | None = None) -> list[Mention]:
    """The themes ``text`` names by part of their name, and understanding's
    ``facet`` when it is part of a theme's name. A mention fitting more than
    :data:`MAX_FITS` pages names none of them."""
    vocabulary = _vocabulary(registry)
    if not vocabulary:
        return []
    found: list[Mention] = []
    for words, plural in _phrases(normalize_text(text).split(), vocabulary):
        found += _mentions_of(words, registry, named=True, plural=plural)
    facet_words = tuple(normalize_text(facet).split())
    if facet_words:
        found += _mentions_of(facet_words, registry, named=False, plural=False)
    kept: dict[tuple[str, ...], Mention] = {}
    for mention in found:
        if len(mention.pages) > MAX_FITS:
            continue
        key = tuple(p.key for p in mention.pages)
        seen = kept.get(key)
        if seen is None:
            kept[key] = mention
        else:
            kept[key] = replace(seen, named=seen.named or mention.named,
                                plural=seen.plural or mention.plural)
    return list(kept.values())


def topic_mentions(text: str, registry: Registry) -> list[Mention]:
    """Every run of theme-name words in ``text``, "theme" said or not, as
    unnamed mentions: "TERI's work on biofuels" has "biofuels", which fits the
    three biofuel themes. Too loose to name a page by itself ("air" and "water"
    are everyday words), so it only explains which pages a question already
    sits close to (`match.similar`)."""
    vocabulary = _vocabulary(registry)
    tokens = normalize_text(text).split()
    found: list[Mention] = []
    i = 0
    while i < len(tokens):
        run = _run(tokens[i:], vocabulary) if tokens[i] != "and" else []
        if not run:
            i += 1
            continue
        found += [m for m in _mentions_of(tuple(run), registry, named=False, plural=False)
                  if len(m.pages) <= MAX_FITS]
        i += len(run)
    return found


def to_ask_about(found: Sequence[Mention]) -> Mention | None:
    """The mention to ask "which did you mean?" about: one that fits several
    pages, when no other mention already picks one of them."""
    for mention in found:
        if not mention.ambiguous:
            continue
        picked = any(len(other.pages) == 1 and other.pages[0] in mention.pages
                     for other in found)
        if not picked:
            return mention
    return None


def _chosen(pages: Sequence[PriorityPage], reply: str) -> tuple[PriorityPage, ...]:
    tokens = normalize_text(reply).split()
    if _EVERY & set(tokens):
        return tuple(pages)
    for token in tokens:
        number = _ORDINALS.get(token)
        if number is None and (m := _NUMBER.fullmatch(token)):
            number = int(m.group(1))
        if number is not None and 1 <= number <= len(pages):
            return (pages[number - 1],)
    words = [t for t in tokens if t not in _FILLER]
    scored = [
        (sum(any(same_word(w, n) for n in _name_words(page)) for w in words), page)
        for page in pages
    ]
    best = max((score for score, _ in scored), default=0)
    return tuple(page for score, page in scored if score and score == best)
