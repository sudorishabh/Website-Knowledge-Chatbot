"""A name the catalog's facets cannot place, matched in titles.

The problem this exists to solve
--------------------------------
"Tell me the count of all the WSDS related events that happened in 2026" was
answered "the page does not give a count", while the catalog holds 19 of them.
WSDS is an event series. The catalog has no facet for a series — its events
carry the name in their titles ("WSDS 2026 Thematic Track: …") — and query
understanding, given only facet slots to fill, put the name where it could not
be counted. Measured 2026-10-05 over 18 questions naming a series, programme or
acronym, two runs each, it went to one of three places:

* the theme slot (WSDS, GRIHA, COP30, Act4Earth, BIPV, TADOX). No theme has the
  name, so the count matched nothing and fell through to passage search, which
  cannot count;
* the title slot, as the question wrote it. A substring misses the name's other
  spelling: "WSDS" missed the curtain raiser titled "World Sustainable
  Development Summit 2027", so 2026 had 18 events instead of 19, and the full
  name alone found 1;
* nowhere, so "how many Green Olympiad events happened" counted all 1,082.

The rule
--------
A name the question gives that no facet places constrains the rows by title,
in every spelling titles use: as written, singular or plural, and as its
acronym or its expansion when titles carry both. Word-bounded, because the
names this is for are short and a substring is wrong for them: "ITEC" is inside
"architecture" and "COP" inside "cooperation".

Three limits keep it to names:

* **Shape.** Outside the title slot, a name is an acronym ("WSDS", "COP30",
  "Act4Earth", or a word the site's titles only ever write in capitals, typed
  in lower case) or words the question capitalises ("Green Olympiad").
  "renewable energy" is a subject: it is written about rather than titled, and
  a list keeps it with the topic constraint. A *count* is the exception — a
  subject dropped from the theme slot has nothing else to narrow it, so it is
  counted by title too, and the answer says so.
* **A type to count.** The question must ask for a kind of content besides the
  name — "WSDS events", "GRIHA press releases". "How many Darbari Seth Memorial
  Lectures have there been" counts the editions of the series, which the
  series' own page states (25) and the catalog's event pages undercount, so it
  is left to the page.
* **In the titles.** A name no title carries is not titled by it, so whatever
  slot it came in keeps the meaning it had before.
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

from app.core.corpus import DEFAULT_BUNDLES
from app.retrieval.structured import topic

logger = logging.getLogger(__name__)

# Words an acronym's letters skip, and a name may hold between its own words.
_JOINING = frozenset("of and for the on in to at a an by with &".split())

# A word that names a kind of content, so a question holding one outside the
# name counts or lists that kind *of* the name. Every content type's own words
# and the collective ones ("publications", "items"), plus the words the catalog
# glossary maps onto events. Not the people, page and service types: "how many
# people attended WSDS" counts attendees, not anything the catalog holds.
_NOT_COUNTED = frozenset(
    "people person page pages carousel carousels service services".split()
)
_EVENT_WORDS = frozenset(
    "conference conferences workshop workshops seminar seminars webinar webinars".split()
)

# Words around a name that are not part of it: the question's scaffolding, the
# words that ask for a count, and the content-type words.
_COUNT_WORDS = frozenset("count counts counted number numbers total tally".split())

# How long the title table is reused, as the title leg does.
_TITLE_TTL_SECONDS = 300.0
_titles: list[str] | None = None
_acronyms: dict[str, str] | None = None
_loaded_at = 0.0

# A word that names most of the site cannot narrow it: the organisation's own
# name is in 13% of titles. Tighter than the topic constraint's bar, because a
# name narrows by itself rather than ranking alongside other words.
_MAX_TITLE_SHARE = 0.05
# How consistently titles must capitalise a word for its lower-case spelling in
# a question to be read as the acronym.
_CAPITALISED_SHARE = 0.9
# How common a second spelling-out of an acronym must be beside the first.
_RIVAL_SHARE = 0.25

# Chunks read to find the pages that mention a name in their text: several per
# page, so a few hundred covers the scope of one count.
_MENTION_POINTS = 400
_MENTION_PAYLOAD = ["document_id", "title", "source_url", "effective_start_date"]

_ORDINAL = re.compile(r"\d+(?:st|nd|rd|th)", re.IGNORECASE)
_POSSESSIVE = re.compile(r"['’]s\b")
_QUESTION_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9&]*")
_TITLE_WORD = re.compile(r"[A-Za-z][A-Za-z'’]*|&")

# A question asking how many, and the words that may stand between that and
# the noun it counts ("the count of all the WSDS related events").
_COUNT_ASK = re.compile(r"\b(?:how\s+many|number\s+of|count\s+(?:of|the|all))\b", re.I)
_BETWEEN = frozenset("all the of total related different various".split())
_ALTERNATIVES = frozenset({"and", "or"})
_COUNTED_WINDOW = 12

# Words showing the user asked about titles as such ("reports titled X", a
# quoted phrase), where a title filter is exactly what was asked for.
_TITLE_QUESTION = re.compile(r"\b(titles?|titled|called|named|headlines?)\b", re.I)
_QUOTED_PHRASE = re.compile(r"[\"“”].+?[\"“”]")


@dataclass(frozen=True)
class TitleName:
    """A name the rows are constrained by. ``slot`` is the analysis slot it was
    read from — "theme", "title" — or None when the question wrote it and no
    slot held it; the planner clears that slot, since the name replaces it."""

    name: str
    spellings: tuple[str, ...]
    slot: str | None = None


def enabled() -> bool:
    """Whether names are matched in titles and counts routed to the catalog.
    Read with a default, as `topic.enabled` is, so a test's partial settings
    stub gets the shipped behaviour rather than an AttributeError."""
    from app.config import get_settings

    return bool(getattr(get_settings(), "catalog_title_names_enabled", True))


def asks_about_titles(question: str | None) -> bool:
    """Whether the question asks about titles themselves, so a title phrase in
    it is what to match exactly rather than a name to match every way."""
    text = question or ""
    return bool(_TITLE_QUESTION.search(text) or _QUOTED_PHRASE.search(text))


def _type_words() -> frozenset[str]:
    """Every word that is part of a content type's name, for telling a name's
    words from the words around it ("Feature" in "TERI Feature Articles")."""
    words = set(topic.bundle_words(None)) | _EVENT_WORDS
    for bundle in DEFAULT_BUNDLES:
        words |= topic.bundle_words(bundle)
    return frozenset(words - _NOT_COUNTED)


def _type_phrases() -> frozenset[str]:
    """The phrases that name a kind of content as a whole — "events", "press
    releases", "research papers" — for finding what a question counts. Whole
    labels rather than their words: "research" alone is not a type, and "how
    many research staff" counts people."""
    from app.retrieval.structured.entities import entity_label

    phrases = {"publication", "publications", "document", "documents",
               "paper", "papers", "brief", "briefs"} | _EVENT_WORDS
    for bundle in DEFAULT_BUNDLES:
        # The type's own name ("news") beside its labels ("news items").
        phrases |= {bundle.replace("_", " "), entity_label(bundle, 1).lower(),
                    entity_label(bundle, 2).lower()}
    return frozenset(phrases - _NOT_COUNTED)


def _type_at(words: Sequence[str], i: int) -> tuple[str, int] | None:
    """The content-type phrase starting at ``words[i]`` and how many words it
    takes, or None."""
    phrases = _type_phrases()
    pair = " ".join(w.lower() for w in words[i:i + 2])
    if len(words) > i + 1 and pair in phrases:
        return pair, 2
    if words[i].lower() in phrases:
        return words[i].lower(), 1
    return None


def _acronym_shaped(token: str) -> bool:
    """"WSDS", "SDGs", "COP30", "G20", "Act4Earth" — not "2026" or "3rd"."""
    if _ORDINAL.fullmatch(token):
        return False
    letters = [c for c in token if c.isalpha()]
    if not letters:
        return False
    if any(c.isdigit() for c in token):
        return True
    if len(letters) > 2 and token.endswith("s") and token[:-1].isupper():
        return True  # a plural acronym: "SDGs"
    return len(letters) >= 2 and token.isupper()


def _load() -> tuple[list[str], dict[str, str]]:
    """Website titles, and the words they write in capitals (lower case -> as
    written), reused for `_TITLE_TTL_SECONDS`. Empty when the catalog is
    unreachable, which leaves every name to keep its slot's old meaning."""
    global _titles, _acronyms, _loaded_at
    if _titles is not None and time.monotonic() - _loaded_at < _TITLE_TTL_SECONDS:
        return _titles, _acronyms or {}
    try:
        from app.catalog import state

        titles = [title for _, title, _ in state.website_titles() if title]
    except Exception:
        logger.warning("Titles unavailable; names are not matched in titles.", exc_info=True)
        return [], {}
    seen: Counter[str] = Counter()
    capitalised: Counter[str] = Counter()
    forms: dict[str, Counter[str]] = {}
    for title in titles:
        letters = [c for c in title if c.isalpha()]
        # A title in capitals throughout says nothing about which of its words
        # are acronyms.
        if not letters or sum(c.islower() for c in letters) < 0.3 * len(letters):
            continue
        for token in set(_QUESTION_WORD.findall(title)):
            key = token.lower()
            seen[key] += 1
            if _acronym_shaped(token):
                capitalised[key] += 1
                forms.setdefault(key, Counter())[token] += 1
    ceiling = _MAX_TITLE_SHARE * max(len(titles), 1)
    acronyms = {
        key: forms[key].most_common(1)[0][0]
        for key, n in capitalised.items()
        if n >= 2 and n >= _CAPITALISED_SHARE * seen[key] and seen[key] <= ceiling
    }
    _titles, _acronyms, _loaded_at = titles, acronyms, time.monotonic()
    return titles, acronyms


def _ubiquitous(word: str, titles: Sequence[str]) -> bool:
    pattern = re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)
    hits = sum(1 for title in titles if pattern.search(title))
    return hits > _MAX_TITLE_SHARE * max(len(titles), 1)


def _significant(words: Iterable[str]) -> list[str]:
    return [w for w in words if w.lower() not in _JOINING]


def _initials(name: str) -> str:
    return "".join(w[0].upper() for w in _significant(_TITLE_WORD.findall(name)))


def expansions(acronym: str, titles: Sequence[str]) -> list[str]:
    """What titles spell ``acronym`` out as: a run of capitalised words whose
    initials are its letters, joining words skipped — "World Sustainable
    Development Summit" for WSDS. Only runs that two or more titles carry, and
    that are common beside the most common one: "Sustainable Development in
    Goa" has the initials of SDG in three titles, against 14 for "Sustainable
    Development Goals"."""
    letters = acronym.upper()
    found: Counter[str] = Counter()
    for title in titles:
        words = _TITLE_WORD.findall(title)
        for i, first in enumerate(words):
            if first[0] != letters[0]:
                continue
            initials = ""
            for j in range(i, len(words)):
                word = words[j]
                if j > i and word.lower() in _JOINING:
                    continue
                if not word[0].isupper():
                    break
                initials += word[0]
                if initials == letters:
                    found[" ".join(words[i:j + 1])] += 1
                    break
                if not letters.startswith(initials):
                    break
    ranked = found.most_common(2)
    floor = max(2, _RIVAL_SHARE * ranked[0][1]) if ranked else 2
    return [phrase for phrase, n in ranked if n >= floor]


def spellings(name: str) -> tuple[str, ...]:
    """Every way titles write ``name``: as given; with a leading acronym spelt
    out ("WSDS 2024" is also "World Sustainable Development Summit 2024"); or
    as its acronym, when titles carry that."""
    titles, acronyms = _load()
    words = name.split()
    if not words:
        return ()
    head = acronyms.get(words[0].lower(), words[0])
    words[0] = head
    out = [" ".join(words)]
    bare = head[:-1] if head.endswith("s") and head[:-1].isupper() else head
    if bare.isalpha() and bare.isupper() and len(bare) >= 3:
        rest = " ".join(words[1:])
        out += [f"{spelt} {rest}".strip() for spelt in expansions(bare, titles)]
    elif len(_significant(words)) >= 3:
        acronym = _initials(name)
        if acronym.lower() in acronyms:
            out.append(acronyms[acronym.lower()])
    return tuple(dict.fromkeys(out))


def _word_pattern(word: str, *, last: bool) -> str:
    """One word of a name as a pattern: a space or hyphen allowed where letters
    meet digits ("COP30", "COP 30", "COP-30"), and the last word either
    singular or plural ("Lecture", "Lectures"; "SDG", "SDGs")."""
    stem = word
    if last and stem.isalpha() and len(stem) > 3 and stem.endswith("s") and not stem.endswith("ss"):
        stem = stem[:-1]
    parts = re.findall(r"[A-Za-z]+|\d+|[^A-Za-z\d]", stem)
    body = ""
    for prev, part in zip([None, *parts], parts):
        if prev is not None and prev.isalnum() and part.isalnum() and prev.isdigit() != part.isdigit():
            body += r"[\s-]?"
        body += re.escape(part)
    if last and stem.isalpha():
        body += "s?"
    return body


def pattern(names: Sequence[str]) -> str:
    """A case-insensitive, word-bounded regular expression matching any of
    ``names``, in the syntax both MySQL's REGEXP and Python's ``re`` read."""
    alternatives = []
    for name in names:
        words = name.split()
        if words:
            alternatives.append(r"[\s-]+".join(
                _word_pattern(w, last=i == len(words) - 1) for i, w in enumerate(words)
            ))
    return r"(?i)\b(?:" + "|".join(alternatives) + r")\b"


def in_titles(names: Sequence[str]) -> bool:
    """Whether any website title carries one of ``names``."""
    titles, _ = _load()
    if not titles or not names:
        return False
    compiled = re.compile(pattern(names))
    return any(compiled.search(title) for title in titles)


def _runs(text: str) -> list[list[str]]:
    """The words of ``text`` in runs, split wherever anything but a space or a
    hyphen separates two of them: "WSDS-related events, in 2026" is
    [["WSDS", "related", "events"], ["in", "2026"]]."""
    runs: list[list[str]] = []
    previous = None
    for match in _QUESTION_WORD.finditer(text):
        if previous is None or text[previous.end():match.start()].strip(" -"):
            runs.append([])
        runs[-1].append(match.group(0))
        previous = match
    return runs


def _is_acronym(token: str, acronyms: dict[str, str]) -> bool:
    return _acronym_shaped(token) or token.lower() in acronyms


def named_in(question: str, *, covered: Iterable[str] = ()) -> str | None:
    """The name a question writes that no facet covers, or None.

    Consecutive words that are acronyms or capitalised, holding an acronym
    ("WSDS", "CEO Forum") or two or more words long ("Green Olympiad"), with no
    question word, count word or content-type word among them. ``covered`` are
    the words of the facets already applied — an author, a theme — which are
    not a name to find. An acronym naming most of the site (the organisation's
    own) narrows nothing and is passed over."""
    titles, acronyms = _load()
    if not titles:
        return None
    skip = {w.lower() for w in covered} | topic._STOP | _COUNT_WORDS | _type_words()
    for run in _runs(_POSSESSIVE.sub("", question or "")):
        names: list[list[str]] = [[]]
        for token in run:
            if token.lower() in skip or not (_is_acronym(token, acronyms) or token[0].isupper()):
                names.append([])
                continue
            names[-1].append(acronyms.get(token.lower(), token))
        for words in names:
            if any(_is_acronym(w, acronyms) for w in words):
                if len(words) == 1 and _ubiquitous(words[0], titles):
                    continue
            elif len(words) < 2:
                continue
            return " ".join(words)
    return None


def counted_types(question: str | None) -> tuple[str, ...]:
    """The kinds of content a counting question counts — ("events",), ("press
    releases",), ("events", "news") — or () when what it counts is not one.

    Read from the words after "how many", "number of" or "count of/the": the
    counted noun is the first word that is neither a determiner nor part of a
    name. "how many COP28 related events and news" counts events and news;
    "how many people attended", "how many MW does the report cite" and "how
    many ITEC training programmes" count something the catalog has no type for.
    """
    match = _COUNT_ASK.search(question or "")
    if not match:
        return ()
    _, acronyms = _load()
    words = _QUESTION_WORD.findall((question or "")[match.end():])[:_COUNTED_WINDOW]
    found: list[str] = []
    i = 0
    while i < len(words):
        typed = _type_at(words, i)
        if typed is not None:
            found.append(typed[0])
            i += typed[1]
            # "... events and news": a second type joined to the first.
            if (i + 1 < len(words) and words[i].lower() in _ALTERNATIVES
                    and _type_at(words, i + 1) is not None):
                i += 1
                continue
            break
        word = words[i]
        if found or not (word.lower() in _BETWEEN or word.isdigit()
                         or _is_acronym(word, acronyms) or word[0].isupper()):
            break
        i += 1
    return tuple(found)


def type_bundle(phrase: str) -> str | None:
    """The content type a counted phrase names: "press releases" ->
    "press_release", "workshops" -> "events". None for a collective word
    ("publications", "items"), which counts every type; a word naming several
    types ("projects") is returned as typed, for the count's own guard to ask
    which was meant."""
    from app.retrieval.structured.entities import ambiguous_bundles, is_known, normalize_entity

    words = phrase.split()
    for candidate in (phrase, words[0] if words else ""):
        if candidate in _EVENT_WORDS:
            return "events"
        bundle = normalize_entity(candidate)
        if is_known(bundle):
            return bundle
        if ambiguous_bundles(candidate):
            return candidate
    return None


def _asks_for_a_type(question: str, *, besides: str) -> bool:
    """Whether the question names a kind of content outside ``besides``."""
    inside = {w.lower() for w in _QUESTION_WORD.findall(besides)}
    words = [w for w in _QUESTION_WORD.findall(question or "") if w.lower() not in inside]
    return any(_type_at(words, i) is not None for i in range(len(words)))


def mentioned_in_text(
    spellings: Sequence[str], *, bundle: str | None = None,
    effective_from: datetime | None = None, effective_to: datetime | None = None,
) -> list[dict[str, str]]:
    """Website pages in the scope whose text names the name and whose title
    does not, newest first, as {"title", "url", "date"}.

    A count by title is exact about titles and blind to the rest: the pages of
    2026's CEO Forum and ACT4EARTH Manifesto name WSDS in their text, and
    neither title carries the name. Read from the chunk text's
    full-text index, so it is the vector store's view of the pages. ``[]`` on
    any failure: the count above it stands without this."""
    from qdrant_client.models import (
        DatetimeRange, FieldCondition, Filter, MatchText, MatchValue,
    )

    from app.config import get_settings
    from app.core.clients import get_qdrant_client
    from app.observability import retrieval_log
    from app.retrieval.search.hybrid_search import build_filter

    if not spellings:
        return []
    conditions: list[object] = [
        FieldCondition(key="source_type", match=MatchValue(value="website")),
        Filter(should=[
            FieldCondition(key="chunk_text", match=MatchText(text=s.lower()))
            for s in spellings
        ]),
    ]
    if bundle:
        conditions.append(FieldCondition(key="bundle", match=MatchValue(value=bundle)))
    if effective_from or effective_to:
        conditions.append(FieldCondition(key="effective_start_date", range=DatetimeRange(
            gte=_utc(effective_from), lt=_utc(effective_to))))
    scroll_filter = build_filter(extra=conditions)
    settings = get_settings()
    with retrieval_log.qdrant_call(
        "scroll", stage="title_name_mentions",
        request=lambda: {"collection": settings.qdrant_collection,
                         "filter": scroll_filter, "limit": _MENTION_POINTS},
    ) as call:
        try:
            points, _ = get_qdrant_client().scroll(
                collection_name=settings.qdrant_collection, scroll_filter=scroll_filter,
                limit=_MENTION_POINTS, with_payload=_MENTION_PAYLOAD, with_vectors=False,
            )
        except Exception as exc:
            call.fail(exc)
            logger.warning("Mention scroll failed; no text mentions.", exc_info=True)
            return []
        call.qdrant_results(points)
    titled = re.compile(pattern(spellings))
    pages: dict[str, dict[str, str]] = {}
    for point in points:
        payload = point.payload or {}
        title = str(payload.get("title") or "")
        doc = str(payload.get("document_id") or "")
        if not doc or not title or titled.search(title) or doc in pages:
            continue
        pages[doc] = {"title": title, "url": str(payload.get("source_url") or ""),
                      "date": str(payload.get("effective_start_date") or "")}
    return sorted(pages.values(), key=lambda p: p["date"], reverse=True)


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def basis(spellings: Sequence[str], total: int) -> str:
    """What a count by title name counted, after its headline: "Each of them
    names WSDS or World Sustainable Development Summit in its title." """
    if not spellings or total <= 0:
        return ""
    which = " or ".join(spellings)
    if total == 1:
        return f"It names {which} in its title."
    return f"Each of them names {which} in its title."


def place(
    question: str | None, *, theme: str | None = None, theme_dropped: bool = False,
    counting: bool = False, title: str | None = None, covered: Iterable[str] = (),
) -> TitleName | None:
    """The name a catalog question's rows should be matched by in titles, or
    None to plan the question as before.

    ``theme`` is the theme slot and ``theme_dropped`` whether it will not be
    applied as asked — no theme has the name, or the nearest is broader;
    ``counting`` whether the operation reports a number; ``title`` the title
    slot; ``covered`` the words of the other facets applied.

    A dropped theme is read first: a name-shaped one always, and for a count
    any subject at all — a list keeps a subject by its topic constraint, a
    count has nothing else to narrow it, and counted without it "how many
    events on air quality in 2026" was every event of the year. Then a title
    slot on a question not about titles, then a name the question writes that
    no slot holds."""
    if not question or not enabled() or not topic.enabled():
        return None
    candidate: tuple[str, str | None] | None = None
    if theme and theme_dropped and (counting or _name_shaped(theme, question)):
        candidate = (theme, "theme")
    elif title and not asks_about_titles(question):
        candidate = (title, "title")
    else:
        found = named_in(question, covered=covered)
        if found:
            candidate = (found, None)
    if candidate is None:
        return None
    name, slot = candidate
    if not _asks_for_a_type(question, besides=name):
        return None
    forms = spellings(name)
    if not in_titles(forms):
        return None
    logger.info("Matching %r in titles as %s.", name, " or ".join(forms))
    return TitleName(name=forms[0], spellings=forms, slot=slot)


def _name_shaped(value: str, question: str) -> bool:
    """A slot value that reads as a name: an acronym, or capitalised words.

    Read in the question's own casing where the question holds the words,
    because the model capitalises freely — it wrote the subject "renewable
    energy" into the slot as "Renewable Energy" — while the user's casing is
    evidence of a name."""
    _, acronyms = _load()
    written = {w.lower(): w for w in _QUESTION_WORD.findall(question or "")}
    tokens = [written.get(t.lower(), t) for t in _QUESTION_WORD.findall(value)]
    if not tokens:
        return False
    if any(_is_acronym(t, acronyms) for t in tokens):
        return True
    significant = _significant(tokens)
    return len(significant) >= 2 and all(t[0].isupper() for t in significant)
