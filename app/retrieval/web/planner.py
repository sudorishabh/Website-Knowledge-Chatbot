"""What a question needs from the web, decided before anything is searched.

:func:`plan` reads the question — and what query understanding already
extracted from it (the rewritten search query, the capabilities, the answer
format, the temporal mode and window) — and returns a :class:`WebPlan`: the
signals that decide whether the web is consulted and how, and the queries to
send if it is.

Deliberately deterministic: regular expressions over the user's own words plus
the understanding output, no model call. Deciding *whether* to search must be
free (it runs on every question when web retrieval is on) and reproducible (the
trace has to be able to say why a question did or did not reach the web).

The signals
-----------
``explicit``        the user asked for the web by name ("search online", "look
                    it up on the internet"). The one unambiguous trigger.
``freshness``       the answer is wanted as of now ("latest", "most recent",
                    "this year", a year no earlier than this one). The corpus
                    lags the site by up to a sweep; the web does not.
``find_passage``    the user is looking for a specific text ("find the article
                    containing a paragraph about ..."), so the evidence must
                    contain those words, not merely be about the topic.
``person``          a named person is asked about ("who is Mr X") — whose
                    profile page ingestion may never have crawled.
``org_scoped``      the question names the organisation, so its own domains are
                    searched first and third-party sites only as a fallback.
``comparison``      several separate periods or things are asked about, so
                    evidence is needed for each ("in 2019 ... and in 2025").
``consolidation``   an overview across many documents is wanted ("a
                    consolidated overview from 2019 to 2025").
``document_request`` the user asks for a document itself, not its content.

Only the first two *force* a web search. The rest shape the queries and tell
:mod:`.sufficiency` what the internal evidence has to show before the web can
be skipped.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace

from app.config import get_settings
from app.core.dates import today_utc
from app.retrieval.search.strategies import extract_content_terms, extract_key_terms
from app.retrieval.understanding.document_request import is_document_request
from app.retrieval.web.safety import policy

__all__ = ["WebPlan", "WebQuery", "plan"]

EXPLICIT = "explicit_request"
FRESHNESS = "freshness"

_EXPLICIT = re.compile(
    r"\b(?:search|look\s*(?:it\s+)?up|check|browse|find|google)\b[^.?!]{0,40}"
    r"\b(?:the\s+)?(?:web|internet|online|google)\b"
    r"|\b(?:web|internet|online|google)\s+search\b"
    r"|\bsearch\s+engine\b|\bgoogle\s+it\b|\bon\s+the\s+(?:web|internet)\b",
    re.IGNORECASE,
)
_FRESHNESS = re.compile(
    r"\b(?:latest|newest|most\s+recent|recent(?:ly)?|current(?:ly)?|today|now|"
    r"this\s+(?:week|month|year)|up[- ]to[- ]date|so\s+far|as\s+of\s+(?:now|today)|"
    r"just\s+(?:released|published|launched|announced)|new(?:ly)?\s+(?:released|published))\b",
    re.IGNORECASE,
)
_PASSAGE_NOUNS = (r"articles?|paragraphs?|passages?|documents?|pages?|reports?|papers?|"
                  r"blogs?|op-?eds?|pieces?|publications?|posts?|sources?|lines?|sentences?")
_FIND_PASSAGE = re.compile(
    rf"\b(?:find|locate|identify|which|what|show\s+me|point\s+me\s+to)\b[^?]{{0,60}}"
    rf"\b(?:{_PASSAGE_NOUNS})\b[^?]{{0,40}}"
    r"\b(?:contain(?:s|ing)?|mention(?:s|ing)?|say(?:s|ing)?|stat(?:es|ing)|quot(?:es|ing)|"
    r"with\s+(?:a|the)\s+(?:paragraph|passage|line|sentence|phrase))\b"
    r"|\bexact\s+(?:words|wording|phrase|quote|sentence)\b|\bthe\s+paragraph\s+(?:about|on|that)\b",
    re.IGNORECASE,
)
_QUOTED = re.compile(r"[\"“]([^\"”]{3,})[\"”]")
_HONORIFIC = r"(?:mr|mrs|ms|dr|prof|professor|shri|smt|sri)\.?"
_PERSON = re.compile(
    rf"\b(?:who\s+(?:is|was)|profile\s+of|about|tell\s+me\s+about)\s+"
    rf"(?:{_HONORIFIC}\s+)?"
    r"([A-Za-z][\w.'-]+(?:\s+[A-Za-z][\w.'-]+){0,3})",
    re.IGNORECASE,
)
_HAS_HONORIFIC = re.compile(rf"\b(?:who\s+(?:is|was)|about|profile\s+of)\s+{_HONORIFIC}\s",
                            re.IGNORECASE)
# Where a name ends: the first of these, or punctuation.
_NAME_STOP = frozenset(
    "at in of from and with for on who whose what which the a an is was does did has "
    "have to his her their its".split()
)
# What a "who is ..." question asks about when it is not a person.
_NOT_A_NAME = frozenset(
    "the a an this that these those our your my director head chair chairman ceo "
    "president founder secretary minister leader current new former".split()
)
_COMPARISON = re.compile(
    r"\bcompar(?:e|ed|ing|ison)\b|\bversus\b|\bvs\.?\b|\bdiffer(?:ence|ent|s)?\b"
    r"|\bchang(?:e|ed|es)\s+(?:between|since|from)\b",
    re.IGNORECASE,
)
_CONSOLIDATION = re.compile(
    r"\bconsolidat\w*|\boverview\b|\bsummar(?:y|ise|ize)\b|\ball\s+(?:of\s+)?(?:the\s+|its\s+)?"
    r"(?:work|reports|studies|publications|projects|articles)\b|\bbody\s+of\s+work\b"
    r"|\bover\s+the\s+years\b|\btimeline\b|\blist\s+(?:of|all)\b",
    re.IGNORECASE,
)
_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
_YEAR_RANGE = re.compile(
    r"\b(?:from|between)\s+((?:19|20)\d{2})\s+(?:to|and|until|till|-|–)\s+((?:19|20)\d{2})\b"
    r"|\b((?:19|20)\d{2})\s*(?:-|–|to)\s*((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
# The scaffolding of a "find the article containing ..." question: words that
# describe the request, not the text being looked for.
_REQUEST_WORDS = frozenset(
    "find locate identify show point which article articles paragraph paragraphs "
    "passage passages document documents page pages report reports paper papers blog "
    "piece publication publications source sources line sentence containing contains "
    "contain mention mentions mentioning about discuss discusses discussing says said "
    "stating quote quoting exact words wording phrase latest recent work give overview "
    "consolidated many much according around approximately search web internet online "
    "google look lookup please tell know".split()
)
# How a request to search is phrased around what is to be searched for. Removed
# from the query sent, so the provider gets "India's EV policy updates" rather
# than "Can you search the web for India's EV policy updates?".
_REQUEST_FRAME = re.compile(
    r"^\s*(?:(?:can|could|would|will)\s+you\s+|please\s+)*"
    r"(?:(?:search|check|browse|look\s*up|google|find)\s+(?:the\s+)?"
    r"(?:web|internet|online)\s+(?:for\s+|about\s+)?|(?:search|look)\s+(?:up|for)\s+)?"
    r"|\s+(?:on|from)\s+the\s+(?:web|internet)\b|\s+online\b|\?+\s*$",
    re.IGNORECASE,
)
_CONSOLIDATION_FORMATS = frozenset({"list", "table", "timeline", "summary", "detailed"})
_CONSOLIDATION_CAPABILITIES = frozenset({"summarization"})


@dataclass(frozen=True)
class WebQuery:
    """One query for the search provider.

    ``fallback`` marks a query run only when the ones before it came back
    short — the open-web query of an organisation-scoped question.
    """

    text: str
    site: str | None = None
    purpose: str = "primary"
    fallback: bool = False


@dataclass(frozen=True)
class WebPlan:
    """Every signal the web decision and the web queries are built from."""

    question: str
    search_query: str
    explicit: bool = False
    freshness: bool = False
    find_passage: bool = False
    person: str | None = None
    org_scoped: bool = False
    comparison: bool = False
    consolidation: bool = False
    document_request: bool = False
    date_from: str | None = None
    date_to: str | None = None
    years: tuple[int, ...] = ()
    entities: tuple[str, ...] = ()
    phrases: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    queries: tuple[WebQuery, ...] = ()

    @property
    def forced_reasons(self) -> tuple[str, ...]:
        """Why the web must be consulted whatever the corpus holds, if at all."""
        return tuple(reason for reason, on in ((EXPLICIT, self.explicit),
                                               (FRESHNESS, self.freshness)) if on)

    @property
    def forced(self) -> bool:
        return bool(self.forced_reasons)

    @property
    def multi_document(self) -> bool:
        """Whether a good answer needs more than one document."""
        return self.comparison or self.consolidation

    def to_trace(self) -> dict:
        record = asdict(self)
        record["forced_reasons"] = list(self.forced_reasons)
        return {k: v for k, v in record.items() if v not in (None, "", (), [], False)}


def _organisation_names() -> tuple[str, ...]:
    raw = str(getattr(get_settings(), "web_organisation_names", "") or "")
    return tuple(n.strip() for n in raw.split(",") if n.strip())


def _mentions(text: str, names: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE) for name in names)


def _person(question: str) -> str | None:
    """The person a "who is ..." question names, without their honorific.

    A lowercase name ("who is ajay mathur") is accepted after "who is"; after
    the looser "about"/"profile of" an honorific or capitals are required, so
    "tell me about solar energy" names nobody.
    """
    for match in _PERSON.finditer(question):
        words: list[str] = []
        for word in match.group(1).split():
            clean = word.strip(".,;:?!'\"")
            if not clean or clean.lower() in _NAME_STOP:
                break
            words.append(clean)
        if not words or words[0].lower() in _NOT_A_NAME:
            continue
        lead = match.group(0).lower()
        strict = not lead.startswith("who")
        honorific = bool(_HAS_HONORIFIC.match(match.group(0) + " "))
        if strict and not honorific and not all(w[:1].isupper() for w in words):
            continue
        if len(words) == 1 and not honorific:
            continue                      # one bare word is a topic, not a name
        return " ".join(words)
    return None


def _window(date_from: str | None, date_to: str | None, question: str
            ) -> tuple[str | None, str | None]:
    """The period asked about: understanding's own window when it extracted
    one, else a year range written in the question ("from 2019 to 2025").
    The end is exclusive, as understanding's ``date_to`` is."""
    if date_from or date_to:
        return date_from, date_to
    match = _YEAR_RANGE.search(question)
    if not match:
        return None, None
    first, last = (g for g in match.groups() if g)
    lo, hi = sorted((int(first), int(last)))
    return f"{lo}-01-01", f"{hi + 1}-01-01"


def _terms(question: str, organisation: tuple[str, ...]) -> tuple[str, ...]:
    """The words the evidence itself should contain: the question's content
    words minus the request's scaffolding and the organisation's own names."""
    org_words = {w.lower() for name in organisation for w in name.split()}
    return tuple(
        t for t in (extract_content_terms(question) or [])
        if t.lower() not in _REQUEST_WORDS and t.lower() not in org_words
    )


def _entities(text: str, organisation: tuple[str, ...]) -> tuple[str, ...]:
    """Named things in the question: key terms plus single capitalised words
    the key-term patterns skip ("Asparagopsis", "Delhi"), minus the
    organisation's names and bare years."""
    org = {n.lower() for n in organisation}
    found = list(extract_key_terms(text) or [])
    # Single capitalised words, which the bigram pattern misses — except a
    # sentence's first word, which is capitalised for grammar, not as a name.
    for sentence in re.split(r"(?<=[.?!])\s+", text):
        for word in sentence.split()[1:]:
            word = word.strip(".,;:?!()'\"")
            if len(word) >= 4 and word[0].isupper() and word[1:].islower():
                found.append(word)
    out: list[str] = []
    for term in (re.sub(r"['’]s$", "", t) for t in found):
        if term.lower() in org or _YEAR.fullmatch(term) or term.lower() in _REQUEST_WORDS:
            continue
        if term.lower() not in {o.lower() for o in out}:
            out.append(term)
    return tuple(out)


def _query_text(p: WebPlan, organisation: tuple[str, ...], *, site_restricted: bool) -> str:
    """The words to send. Keyword-shaped for a passage hunt (a search engine
    matches words, and the request's scaffolding only dilutes them), the name
    for a person, and the rewritten question otherwise. The organisation's name
    is added to an open-web query about it, and left off a site-restricted one,
    where the site already says it."""
    if p.find_passage:
        quoted = [f'"{phrase}"' for phrase in p.phrases]
        text = " ".join(dict.fromkeys(quoted + list(p.terms)))
    elif p.person:
        text = f"{p.person} profile"
    else:
        text = _REQUEST_FRAME.sub(" ", p.search_query).strip() or p.search_query
    if p.org_scoped and organisation and not site_restricted and not _mentions(text, organisation):
        text = f"{organisation[0]} {text}"
    return " ".join(text.split())


def _queries(p: WebPlan, organisation: tuple[str, ...]) -> tuple[WebQuery, ...]:
    domain_policy = policy()
    queries: list[WebQuery] = [
        WebQuery(text=_query_text(p, organisation, site_restricted=True), site=site,
                 purpose="primary")
        for site in domain_policy.primary[:2]
    ]
    if domain_policy.allow_third_party or not queries:
        queries.append(WebQuery(
            text=_query_text(p, organisation, site_restricted=False), site=None,
            purpose="open",
            # About the organisation: its own site first, the open web only if
            # that comes back short. Anything else: both, side by side.
            fallback=p.org_scoped and bool(queries),
        ))
    return tuple(q for q in queries if q.text)


def plan(
    question: str,
    search_query: str | None = None,
    *,
    capabilities: set[str] | frozenset[str] = frozenset(),
    answer_format: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> WebPlan:
    """The web plan for one question. ``search_query`` is understanding's
    standalone rewrite (the question itself when absent); the other arguments
    are what understanding extracted, passed as plain values."""
    question = " ".join((question or "").split())
    search_query = " ".join((search_query or question).split())
    both = f"{question} {search_query}"
    organisation = _organisation_names()
    years = tuple(sorted({int(y) for y in _YEAR.findall(both)}))
    window_from, window_to = _window(date_from, date_to, question)
    ranged = bool(_YEAR_RANGE.search(question)) or bool(date_from and date_to)
    this_year = today_utc().year
    consolidation = (bool(_CONSOLIDATION.search(question))
                     or (answer_format or "") in _CONSOLIDATION_FORMATS
                     or bool(set(capabilities) & _CONSOLIDATION_CAPABILITIES)
                     or (ranged and len(years) >= 2))

    draft = WebPlan(
        question=question,
        search_query=search_query,
        explicit=bool(_EXPLICIT.search(question)),
        freshness=bool(_FRESHNESS.search(question)) or any(y >= this_year for y in years),
        find_passage=bool(_FIND_PASSAGE.search(question)) or bool(_QUOTED.search(question)),
        person=_person(question),
        org_scoped=_mentions(both, organisation),
        comparison=("comparison" in capabilities or bool(_COMPARISON.search(question))
                    or (len(years) >= 2 and not ranged)),
        consolidation=consolidation,
        # "Give me an overview of ..." reads as a request for a document by its
        # wording, but an overview is content assembled from many.
        document_request=is_document_request(question) and not consolidation,
        date_from=window_from,
        date_to=window_to,
        years=years,
        entities=_entities(question, organisation),
        phrases=tuple(m.strip() for m in _QUOTED.findall(question)),
        terms=_terms(question, organisation),
    )
    return replace(draft, queries=_queries(draft, organisation))
