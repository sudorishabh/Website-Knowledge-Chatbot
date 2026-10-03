"""What a list question is *about*, beyond the facets the catalog can express.

The problem this exists to solve
--------------------------------
The catalog can filter on a small, closed set of facets: content type, theme,
tag, author, title substring, date. A question's topic is open vocabulary. When
the two do not line up, the structured path used to answer anyway, and it failed
in two directions at once:

1. **The topic was snapped onto the nearest taxonomy theme.** Measured:
   "publications on *Sustainable Development Goals*" resolved to the theme
   "Resources & Sustainable Development", and "reports on *climate change
   adaptation*" to "Climate Change". Those are real themes, far broader than the
   question, so the filter was not absent — it was wrong. The answer came back as
   the ten most recent rows of a large bucket: an opinion piece on education, a
   children's science congress, a BioE3 video.
2. **Whatever the facets could not express simply vanished.** "Which researchers
   work on AI and sustainability?" carried no facet at all, so the plan was a
   bare `lookup_record` over everything, ordered by recency.

Both collapse to the same failure mode — *the list head*: the newest N rows of
whatever bucket survived, which is nearly identical for any two questions that
land in the same bucket. Two unrelated questions returning a byte-identical list
is the signature.

The rule this module enforces
-----------------------------
A list is only trustworthy when every topical word in the question is accounted
for — either by a facet that genuinely covers it, or by an explicit topic
constraint on the rows. What is left over after that accounting is the
*residual*, and a residual that nothing constrains means the structured path
must not answer. Section 6 of the brief, in one sentence: prefer "no trustworthy
structured answer" to "a plausible but wrong list".

Note what this deliberately does *not* do. It does not make the catalog smarter
about topics; it makes it honest about the ones it cannot handle, and hands
those to semantic retrieval, which is the layer designed for open vocabulary.
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

# Question scaffolding, presentation verbs, and the words that ask for a list.
# None of these say anything about subject matter, so none of them can leave a
# residual. Broader than the retrieval-side stop list because a catalog question
# is mostly scaffolding: "what X are available on Y" is five stop words and a Y.
_STOP = frozenset(
    """
    a about all an and any are as at available be been by can could did do does
    doing done for from get give had has have how i in into is it its just kindly
    know like list me more most much my need of on or our please provide provides
    provided published publish publishes offer offers offered recommend recommends
    say says see share show shows some tell that the their them there these this
    those to under up us was were what when where which who whom whose why will
    with would you your work works working conduct conducts conducted carry
    carries carried run runs running exist exists currently current latest recent
    recently new newest ongoing upcoming past present today now
    being having made make makes making use uses using given gives take takes
    find finds found look looks looking want wants need needs came come comes
    coming released release releases issued issue issues produced produce
    down mention mentions mentioned mentioning
    title titles titled called named headline headlines
    """.split()
)

# A word this short carries too little to be a topic on its own ("AI" is handled
# by the acronym path below, which keeps short uppercase tokens).
_MIN_TERM_LEN = 4

_WORD = re.compile(r"[a-z][a-z0-9-]{2,}")
_ACRONYM = re.compile(r"\b[A-Z]{2,}\b")

# Asking for people. A document catalog cannot answer these — it has authors, not
# researchers, and the two are not the same claim — so the structured path must
# decline rather than list documents at a person question. Measured: "Which
# researchers work on AI and sustainability?" returned an opinion piece on
# education, a memorial lecture and a solar-industry news item, and named nobody.
_PERSON_WORDS = frozenset(
    """
    researcher researchers scientist scientists expert experts author authors
    staff colleague colleagues person people team member members employee
    employees fellow fellows director directors head heads lead leads
    """.split()
)
_PERSON_ASK = re.compile(r"\b(who|whom|whose)\b", re.IGNORECASE)

# How alike two words must be to count as the same word despite a spelling
# difference. High enough that "enviroment"/"environment" (0.95) passes while
# "goals"/"resources" (0.29) does not: the point is to forgive a typo without
# forgiving a different word.
_SPELLING_RATIO = 0.82


def enabled() -> bool:
    """Whether the topic constraint is switched on.

    Read with a default rather than as an attribute: the test suite stubs
    settings with lightweight namespaces that carry only the fields a given test
    cares about, and a new flag must not turn those into errors. The default is
    the shipped behaviour, so an incomplete stub gets the real one.
    """
    from app.config import get_settings

    return bool(getattr(get_settings(), "structured_topic_constraint_enabled", True))


def content_terms(question: str) -> list[str]:
    """The question's subject-matter words, scaffolding removed.

    Acronyms survive their length check because they are the most specific thing
    a short token can be: "AI", "SDG", "LCA" name a topic exactly.
    """
    text = question or ""
    words = [
        w for w in _WORD.findall(text.lower())
        if w not in _STOP and len(w) >= _MIN_TERM_LEN
    ]
    acronyms = [
        a.lower() for a in _ACRONYM.findall(text) if a.lower() not in _STOP
    ]
    seen: set[str] = set()
    out: list[str] = []
    for word in acronyms + words:
        if word not in seen:
            seen.add(word)
            out.append(word)
    return out


def _facet_words(*values: str | None) -> set[str]:
    """Every word appearing in the facet values a plan actually applied."""
    words: set[str] = set()
    for value in values:
        if value:
            words.update(_WORD.findall(value.lower()))
    return words


def bundle_words(bundle: str | None) -> set[str]:
    """Words that naming this content type already accounts for.

    "What policy briefs has TERI recently published?" is *entirely* content type
    and recency: once `policy_brief` is chosen there is no topic left, and the
    ten most recent policy briefs are the right answer. Consuming the bundle's
    own vocabulary is what keeps that case working.
    """
    # A question may ask for the collective ("publications", "documents", "work")
    # and be planned onto one bundle or onto none. Either way the collective word
    # names no subject, so it is consumed whether or not a bundle was resolved —
    # measured: without this, "publications on the SDGs" constrained titles to
    # the word "publications" and returned the site's "Articles & Publications"
    # index page.
    words = {"publication", "publications", "document", "documents",
             "record", "records", "item", "items", "content", "material",
             "materials", "resource", "resources", "study", "studies"}
    if not bundle:
        return words
    words.update(_WORD.findall(bundle.replace("_", " ").lower()))
    try:
        from app.retrieval.structured.entities import entity_label

        for n in (1, 2):
            words.update(_WORD.findall(entity_label(bundle, n).lower()))
    except Exception:  # pragma: no cover - registry is optional here
        logger.debug("No label for bundle %r.", bundle, exc_info=True)
    return words


def _ubiquitous(terms: Sequence[str]) -> set[str]:
    """Terms too common across the corpus's titles to name a subject.

    Computed against the live title catalogue rather than configured, so the
    organisation's own name — in 11.9% of this corpus's titles — is dropped
    without this module knowing what the organisation is called. Fails open: if
    the catalogue is unreachable, nothing is dropped and the caller is left with
    a *larger* residual, which errs toward declining rather than guessing.
    """
    if not terms:
        return set()
    try:
        from app.catalog import state
        from app.retrieval.search import title_leg

        rows = state.website_titles()
        keep = set(title_leg._selective_terms(list(terms), rows))
        return {t for t in terms if t not in keep}
    except Exception:
        logger.debug("Title frequencies unavailable; keeping every term.", exc_info=True)
        return set()


def residual_topic(
    question: str,
    *,
    bundle: str | None = None,
    theme: str | None = None,
    tag: str | None = None,
    author: str | None = None,
    title_contains: str | None = None,
) -> list[str]:
    """Subject words the planned facets do not account for.

    Empty means the facets cover the question and the list can be trusted. Any
    word left over is subject matter nothing is filtering on, and the caller must
    either constrain the rows by it or decline.
    """
    covered = bundle_words(bundle) | _facet_words(theme, tag, author, title_contains)
    remaining = [t for t in content_terms(question) if t not in covered]
    return [t for t in remaining if t not in _ubiquitous(remaining)]


def wants_person(question: str) -> bool:
    """Whether the question asks for people rather than documents."""
    text = (question or "").lower()
    words = set(_WORD.findall(text))
    if words & _PERSON_WORDS:
        return True
    # "Who wrote X" asks for a person even without one of the nouns above.
    return bool(_PERSON_ASK.search(text)) and not words & {"what", "which"}


def faithful_theme(requested: str | None, resolved: str | None) -> bool:
    """Whether a resolved taxonomy theme really is what the question asked for.

    Resolution is fuzzy by design — it has to be, so that "climate" finds
    "Climate Change" — but fuzziness across a *widening* gap is how a question
    about the SDGs became a question about "Resources & Sustainable Development".

    The test is directional containment, not similarity: the resolved name is
    faithful only when it carries **every** word that was asked for. Naming part
    of a theme is fine ("climate" -> "Climate Change"), because the theme is
    still about the thing asked about. Dropping part of the ask is not
    ("climate change *adaptation*" -> "Climate Change"), because the answer then
    spans everything the missing word was there to exclude — which is how a
    request for adaptation reports returned a COP28 decarbonisation report and a
    white paper on the NAPCC.

    Word matching is approximate, because the other thing resolution exists for
    is spelling: "enviroment" must still canonicalize to "Environment". A
    *missing* word is what makes a substitution unfaithful; a misspelt one does
    not.
    """
    if not requested or not resolved:
        return True  # nothing was substituted
    asked = {w for w in _WORD.findall(requested.lower()) if w not in _STOP}
    got = {w for w in _WORD.findall(resolved.lower()) if w not in _STOP}
    if not asked or not got:
        return True
    return all(any(_same_word(a, g) for g in got) for a in asked)


def _same_word(asked: str, got: str) -> bool:
    """Whether two words are the same word, allowing for spelling and inflection."""
    if asked == got or asked.rstrip("s") == got.rstrip("s"):
        return True
    return SequenceMatcher(None, asked, got).ratio() >= _SPELLING_RATIO


def matched_terms(title: str | None, terms: Iterable[str]) -> int:
    """How many topic terms appear in a title. Used to rank a constrained list."""
    text = (title or "").lower()
    return sum(1 for t in terms if t and t in text)
