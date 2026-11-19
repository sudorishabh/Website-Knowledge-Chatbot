"""Contextual follow-up questions: what a visitor might ask next, after an answer.

Two to four "Related questions" under a generated answer, each one a click
away from being asked. The hard part is not producing questions — any model
will — but producing ones that are worth the click:

* **Answerable.** A suggestion the bot then refuses, or answers with "that is
  not in the sources", is worse than none. So the call sees the numbered
  passages the answer was written from and, for each question, must first copy
  the words from one passage that answer it, and only then write the question
  those words answer. The screen checks the words really are in that passage
  and make a whole statement — not a name, a heading or a sentence cut short
  (see :func:`grounded`), and not a row of a list stitched across its lines
  (see :func:`list_row`): a question that does not stand on a sentence from
  our own material is never shown.

  Relevance scores cannot do this job, which is why the check reads text.
  Measured on this corpus, the reranker scored questions the bot rightly
  refuses ("What is the Director General's PhD thesis about?", "How many
  employees work at the Guwahati office?") at 0.99 and above, as high as
  questions it answers: it judges that a passage is *about* the subject, not
  that it holds the answer.
* **New.** A suggestion that restates what the answer just said, or the
  visitor's own question in other words, wastes the click. So the call sees the
  answer, and the screen below drops rewordings of anything already asked.
  A list answer ("list the management people") is the hard case: it goes on to
  give every entry of the passages it lists from, long after the call has
  launched, so any question about one of those entries repeats a row. For a
  list answer the call is told so, and only passages the final answer did not
  cite may supply a suggestion.
* **Free in latency.** The answer must never wait for them.

Shape of it
-----------
:class:`FollowupTask` owns one answer's call. The pipeline feeds it the
answer's tokens as they stream; once the opening has streamed — the first line
break past ``followups_launch_chars``, which is where the direct answer has
been given — it launches the call on its own thread with the answer *so far*.
The rest of the answer's streaming time hides the call. An answer shorter than
that launches it when it ends.

The questions travel as their own SSE event after ``sources``, not inside the
answer text: the answer passes through the date-claim guard, list tidying,
numeric checks, refusal detection, the semantic cache and the chat history,
and a block of suggestions inside it would trip every one of them. The widget
renders the answer on ``sources``, so waiting here only ever holds back
``done`` — bounded by ``followups_wait_seconds``.

When the answer is final, :meth:`FollowupTask.collect` ranks what survived the
screen against it: questions standing on passages the answer did *not* cite go
first, because that is the material the visitor has not yet been shown; then at
most one per passage, so three suggestions are three directions.

The lean toward broader questions — one of the proposals pitched at the wider
theme, none of them quiz questions — lives in the prompt and nowhere else. A
ranking that reserved a place for the broad question was tried and removed: the
one it pulled in was sometimes off the visitor's subject altogether (a question
on building energy efficiency under one about the Director General), because a
broad question stands on whatever passage it was written from, relevant or not.

How well this works is measured, not asserted: ``scripts/eval_followups.py``
asks questions through ``/chat``, clicks every suggestion the way the widget
does, and grades what each click got back.

Fails open everywhere: a failed call, an unusable reply or a late one means no
follow-ups, never a broken answer.
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

from pydantic import BaseModel, Field

from app.core.clients.embeddings import get_embeddings
from app.core.clients.llm import get_llm
from app.core.models.context import ContextBlock
from app.generation.faithfulness import extract_markers
from app.generation.sections import is_refusal, strip_tags
from app.retrieval.search.strategies import _cosine, _is_rewording

logger = logging.getLogger(__name__)

#: Longest suggestion shown. A chip is one line in the widget; past this it
#: wraps into a paragraph and stops reading as a question to click.
MAX_QUESTION_CHARS = 110

#: How much of each passage the call sees. Enough to name what the passage is
#: about and hold the sentence a question is answered by; the whole block would
#: multiply the call's input for no better suggestions.
_SNIPPET_CHARS = 500
_MAX_PASSAGES = 10
#: How much of the answer the call sees when it launches at the end of a long
#: one. The opening carries the direct answer; the tail is detail.
_MAX_ANSWER_CHARS = 3000
#: Earlier questions the call is told not to repeat.
_EARLIER_QUESTIONS = 5

#: The evidence a question is shown on: at least this many words, copied from
#: its passage. Fewer is a name, a title or a fragment, which says a passage
#: mentions a subject, not that it answers anything about it. Set from the eval:
#: of 353 questions shown across two runs, three clicks were refused. Two stood
#: on six or seven words ("Who serves on TERI's management team alongside Dr
#: Vibha Dhawan?" on "Dr Vibha Dhawan Director General, TERI"); the third on a
#: sentence cut off where the call's excerpt ended, which :func:`grounded`
#: refuses separately.
_MIN_EVIDENCE_WORDS = 8
#: Share of the evidence's three-word runs that must occur in the passage. Not
#: all of them: a model copying a sentence drops a comma, closes up an ellipsis
#: or straightens a quote, and that is still the passage's sentence. Well short
#: of it, the words are the model's, not the passage's.
_EVIDENCE_OVERLAP = 0.6
_WORD = re.compile(r"[^\W_]+")
#: How far (in words) a copied quote may drift from the passage's word order
#: and still be placed — the gap a dropped word or a closed-up ellipsis leaves.
_DRIFT = 3
#: A line ending in one of these ends a sentence or introduces what follows:
#: prose, not a row of a list.
_SENTENCE_END = (".", "!", "?", ":", ";")

#: Answer formats that enumerate the entries of their passages. A suggestion
#: on a passage such an answer drew from asks about a row it has just shown.
LIST_FORMATS = frozenset({"list", "table", "timeline"})


class _Suggestion(BaseModel):
    # Field order is the order the model writes them in: the passage, then the
    # words in it that answer, then the question those words answer — so the
    # question is written to fit the evidence, never the evidence found to fit
    # a question.
    passage: int = 0
    evidence: str = ""
    question: str = ""
    #: "broad" or "specific". Nothing reads it: labelling each question is how
    #: the model keeps count of the one broad question the prompt asks for.
    scope: str = ""


class _Followups(BaseModel):
    questions: list[_Suggestion] = Field(default_factory=list)


@dataclass(frozen=True)
class Candidate:
    """One proposed question, the passage it says it is answerable from, and
    the words in that passage that answer it."""

    question: str
    passage: int
    evidence: str = ""


_FOLLOWUPS_SYSTEM = (
    "You suggest what a visitor to the website of TERI (The Energy and Resources "
    "Institute) might want to ask next, after reading an answer to their "
    "question. You are given their question, the answer they have been shown so "
    "far, and the numbered passages that answer was written from.\n"
    "Rules for every question you suggest:\n"
    "1. Its answer must be written in ONE of the numbered passages, in the text "
    "you are shown. First give that passage's number. Then copy into 'evidence' "
    "the words from it that answer the question — one or two sentences, at most "
    "30 words, copied exactly. Only then write the question, asking no more than "
    "those words say. If they cover a wider region, another year or only part "
    "of what you meant to ask, change the question to fit them. Never ask for a "
    "detail — a figure, a qualification, a finding, a list — the passage does "
    "not state. Evidence is a sentence that says something: a heading, a title, "
    "a name with a job title or an entry in a list is not evidence, because a "
    "passage that only names a subject cannot answer questions about it.\n"
    "2. It must take the visitor somewhere new: not something the answer "
    "already says, and not their question (or an earlier one) in other words.\n"
    "3. It must follow on from what the visitor asked: the next thing someone "
    "interested in that would want to know, not a stray detail from a passage "
    "about something else.\n"
    "4. It must stand on its own. Name the subject in full — the project, "
    "report, programme, person, place or theme — never 'it', 'they', 'this', "
    "'the report' or 'the project'.\n"
    "5. One question about one thing: at most 12 words, ending with a question "
    "mark. Not two questions joined by 'and'. No yes/no questions.\n"
    "6. Vary them: different passages and different angles — a related project "
    "or publication, the people involved, a neighbouring theme, a detail worth "
    "going deeper on.\n"
    "7. Pitch them where a curious visitor would. Most should be specific: about "
    "a named project, study, programme, place or person in a passage — what it "
    "did, found or offers — with scope 'specific'. Exactly one should be broad: "
    "about the wider theme or area of work a passage belongs to, with scope "
    "'broad'. Every question, broad ones too, needs evidence with substance — "
    "something TERI did, found, built or recommends — not general words about "
    "its mission, role or impact. No quiz questions whose whole answer is one "
    "figure, date or name, and no questions about the document itself "
    "('according to the brochure', 'what does the council list say').\n"
    "8. Write the question in the language of the visitor's question.\n"
    "9. Ask only about TERI's work and what the passages contain. Never ask "
    "about the assistant, and add no names, facts or dates the passages do not "
    "contain.\n"
    "Example. Question: 'What does TERI do on air pollution?'\n"
    "  GOOD, specific: 'What did TERI's source apportionment study find for "
    "Delhi?' — evidence: 'The study found that transport and industry were the "
    "largest contributors to PM2.5 in Delhi in winter.'\n"
    "  GOOD, broad: 'How does TERI help Indian cities plan for cleaner air?' — "
    "evidence: 'TERI supports city governments in preparing clean air action "
    "plans under the National Clean Air Programme.'\n"
    "  BAD: 'What is TERI's work on air quality?' (the question again); 'What "
    "did it find?' (does not stand on its own); 'Is air pollution bad?' (yes/no, "
    "and not about TERI's work); 'What was Delhi's PM2.5 level in November "
    "2019?' (a quiz question: its whole answer is one figure); 'What are the "
    "study's methods and funders?' (two questions, and the passage states "
    "neither); 'How has TERI's research influenced industry and communities?' "
    "(the passage only calls the impact 'transformative': nothing concrete to "
    "answer with)"
)


def _excerpt(text: str) -> tuple[str, bool]:
    """The opening of a passage the call is shown, and whether it was cut.

    Cut at a word, never mid-word: a stub ("In its journey of m") is real text,
    so a quote of it would pass the evidence check while answering nothing.

    Line breaks are kept (blank lines and runs of spaces are not): they are how
    a list shows itself — a name on one line, a job title on the next — and
    rule 1 tells the model an entry in a list is not evidence. Flattened, "Mr R
    R Rashmi / Distinguished Fellow, Green Shipping" reads like a phrase.
    """
    lines = (" ".join(line.split()) for line in (text or "").splitlines())
    flat = "\n".join(line for line in lines if line)
    if len(flat) <= _SNIPPET_CHARS:
        return flat, False
    head = flat[:_SNIPPET_CHARS]
    cut = max(head.rfind(" "), head.rfind("\n"))
    return (head[:cut] if cut > 0 else head), True


def passage_digest(blocks: Sequence[ContextBlock]) -> str:
    """The passages as the call sees them: number, title, section, opening —
    with '…' where the opening stops short of the passage."""
    entries = []
    for block in blocks[:_MAX_PASSAGES]:
        payload = block.payload or {}
        head = " — ".join(
            str(part).strip()
            for part in (payload.get("title"), payload.get("section_heading"))
            if part and str(part).strip()
        )
        text, cut = _excerpt(block.text)
        if cut:
            text += " …"
        entries.append(f"[{block.n}] {head}\n{text}" if head else f"[{block.n}] {text}")
    return "\n\n".join(entries)


def _earlier_questions(history: Sequence[dict[str, str]] | None) -> list[str]:
    asked = [
        str(turn.get("content") or "").strip()
        for turn in (history or [])
        if turn.get("role") == "user"
    ]
    return [q for q in asked if q][-_EARLIER_QUESTIONS:]


def _human_turn(
    question: str,
    search_query: str,
    answer_so_far: str,
    blocks: Sequence[ContextBlock],
    history: Sequence[dict[str, str]] | None,
    n: int,
    list_answer: bool = False,
) -> str:
    parts = [f"Visitor's question: {question.strip()}"]
    if search_query and search_query.strip().lower() != question.strip().lower():
        parts.append(f"(Understood as: {search_query.strip()})")
    earlier = _earlier_questions(history)
    if earlier:
        parts.append("Asked earlier in this conversation:\n"
                     + "\n".join(f"- {q}" for q in earlier))
    shown = strip_tags(answer_so_far or "").strip()[:_MAX_ANSWER_CHARS]
    parts.append(f"The answer shown so far:\n{shown or '(nothing yet)'}")
    if list_answer:
        # The call launches near the top of the list; without this it cannot
        # know the rest of the answer will list every row it might ask about.
        parts.append("This answer is a list. It goes on to give every entry in the "
                     "passages it lists from: the ones it cites, and any passage "
                     "that is the same kind of list. A question about one of those "
                     "entries repeats the answer, so ask about what the other "
                     "passages add.")
    parts.append(f"Numbered passages:\n{passage_digest(blocks)}")
    parts.append(f"Suggest {n} follow-up questions. There is always something worth "
                 "asking: when a passage offers nothing that keeps the rules, use "
                 "another one.")
    return "\n\n".join(parts)


def generate_followups(
    question: str,
    blocks: Sequence[ContextBlock],
    *,
    answer_so_far: str,
    search_query: str = "",
    history: Sequence[dict[str, str]] | None = None,
    n: int = 5,
    reasoning_effort: str = "",
    list_answer: bool = False,
) -> list[Candidate]:
    """Up to ``n`` proposed questions, unscreened; ``[]`` on any failure.

    A structured call, read leniently — a missing or malformed list reads as
    "nothing proposed", which is the no-follow-ups path. An empty or unreadable
    reply gets one more try. Both happen: with this many rules and little
    deliberation (``followups_reasoning_effort`` "none") the model sometimes
    answers an ordinary context with ``{"questions": []}``, and now and then with
    JSON with trailing text. The empty replies came back in about two seconds
    against five for a real one, so the second try usually still lands inside
    the wait.
    """
    if not (question or "").strip() or not blocks or n <= 0:
        return []
    messages = [
        ("system", _FOLLOWUPS_SYSTEM),
        ("human", _human_turn(question, search_query, answer_so_far, blocks, history, n,
                              list_answer=list_answer)),
    ]
    raw: list[Any] = []
    for _ in range(2):
        try:
            model = get_llm(
                temperature=0.5, reasoning_effort=(reasoning_effort or "").strip() or None,
            ).with_structured_output(_Followups)
            result = model.invoke(messages)
            found = getattr(result, "questions", None)
            raw = list(found) if isinstance(found, (list, tuple)) else []
        except Exception:
            logger.warning("Follow-up generation failed.", exc_info=True)
            raw = []
        if raw:
            break
    out: list[Candidate] = []
    for item in raw:
        text = str(getattr(item, "question", "") or "").strip()
        try:
            passage = int(getattr(item, "passage", 0) or 0)
        except (TypeError, ValueError):
            passage = 0
        evidence = " ".join(str(getattr(item, "evidence", "") or "").split())
        if text:
            out.append(Candidate(question=text, passage=passage, evidence=evidence))
    return out[:n]


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


def grounded(evidence: str, passage: str) -> bool:
    """True when ``evidence`` is the passage's own words, not the model's, and a
    whole statement.

    Compared as runs of three words rather than as one string, so the small
    edits of copying (punctuation, a straightened quote, an ellipsis closing a
    gap) do not fail a real quote, while words the passage does not contain —
    a paraphrase, or a detail the model supplied — do.

    A whole statement: long enough to say something (see
    ``_MIN_EVIDENCE_WORDS``), and not running into the point where the call's
    excerpt of the passage was cut. A sentence quoted up to that point is a
    sentence the model never saw the end of — "68 per cent stated that
    temperatures" — and the question written on it asks for the part it missed.
    """
    quote = _words(evidence)
    if len(quote) < _MIN_EVIDENCE_WORDS:
        return False
    shown, cut = _excerpt(passage)
    if cut and quote[-3:] == _words(shown)[-3:]:
        return False
    text = _words(passage)
    runs = {tuple(quote[i:i + 3]) for i in range(len(quote) - 2)}
    found = {tuple(text[i:i + 3]) for i in range(len(text) - 2)}
    return len(runs & found) >= _EVIDENCE_OVERLAP * len(runs)


def _quoted_lines(evidence: str, passage: str) -> list[str]:
    """The lines of ``passage`` that ``evidence`` was copied from, in order.

    The quote is placed where most of its three-word runs agree it starts —
    a run like "member governing council" occurs on every row of a council
    list, but only one starting point fits the whole quote — and each run is
    then taken from the occurrence nearest that point.
    """
    lines = [" ".join(line.split()) for line in (passage or "").splitlines()]
    lines = [line for line in lines if line]
    words: list[str] = []
    line_of: list[int] = []
    for i, line in enumerate(lines):
        for word in _words(line):
            words.append(word)
            line_of.append(i)
    where: dict[tuple[str, ...], list[int]] = {}
    for p in range(len(words) - 2):
        where.setdefault(tuple(words[p:p + 3]), []).append(p)
    quote = _words(evidence)
    runs = [tuple(quote[i:i + 3]) for i in range(len(quote) - 2)]
    starts = Counter(p - i for i, run in enumerate(runs) for p in where.get(run, ()))
    if not starts:
        return []
    start = starts.most_common(1)[0][0]
    touched: set[int] = set()
    for i, run in enumerate(runs):
        near = [p for p in where.get(run, ()) if abs(p - start - i) <= _DRIFT]
        if near:
            p = min(near, key=lambda p: abs(p - start - i))
            touched.update(line_of[p:p + 3])
    return lines[min(touched):max(touched) + 1] if touched else []


def _ends_sentence(line: str) -> bool:
    return line.rstrip("'\"’”)]").endswith(_SENTENCE_END)


def _starts_afresh(line: str) -> bool:
    """A line opening with a capital or a digit. A script without capitals
    never does, so its quotes are never taken for list rows."""
    first = next((ch for ch in line if ch.isalnum()), "")
    return first.isupper() or first.isdigit()


def list_row(evidence: str, passage: str) -> bool:
    """True when ``evidence`` is lines of a list run together, not a sentence.

    Copying a list flattens it: "Mr R R Rashmi" and "Distinguished Fellow,
    Green Shipping", two lines of a directory, become one eight-word "quote"
    that passes :func:`grounded` — and says only that a person holds a title,
    nothing that answers "What is TERI's work on green shipping?".

    So a quote is a list row when it runs across lines, none of which ends a
    sentence, each starting afresh with a capital or a digit. A wrapped
    sentence fails the second test, going on in lower case ("…sustainable /
    management and / conservation of marine,"); a heading over its paragraph
    ("Founders / Mr Darbari S Seth was a unique individual … touch'.") and a
    sentence wrapped at acronyms ("air quality / (PM2.5) and the … / CRB
    period.") fail the first. Checked against the 418 suggestions shown in the
    earlier eval runs: this flags 11 — name-and-title rows, project titles, an
    address, a list of fuels — and no sentence.
    """
    lines = _quoted_lines(evidence, passage)
    return (len(lines) > 1
            and not any(_ends_sentence(line) for line in lines)
            and all(_starts_afresh(line) for line in lines[1:]))


def _embed(texts: list[str]) -> list[list[float]]:
    return get_embeddings().embed_documents(texts)


def screen(
    candidates: Sequence[Candidate],
    *,
    question: str,
    search_query: str = "",
    history: Sequence[dict[str, str]] | None = None,
    blocks: Sequence[ContextBlock],
    query_vector: Sequence[float] | None = None,
    threshold: float = 0.92,
) -> tuple[list[Candidate], Counter]:
    """The candidates worth showing, in the model's order, and why the rest went.

    Everything here is independent of the final answer, so it runs on the
    call's own thread rather than after the answer: shape, a real passage whose
    text holds the question's evidence (see :func:`grounded`) as a sentence
    rather than a row of a list (see :func:`list_row`), and not a repeat
    of anything already asked — lexically first (free), then by cosine against
    the question's vector and against each other, in one batched embedding
    call. An embedding failure skips the semantic half rather than dropping
    anything: a missing vector is a missing check, not evidence of
    duplication. There is no such allowance for evidence — a question whose
    answer is not in our material is the one suggestion that must never show.
    """
    dropped: Counter = Counter()
    texts = {block.n: block.text or "" for block in blocks}
    asked = [q for q in (question, search_query, *_earlier_questions(history))
             if q and q.strip()]
    seen = {q.strip().lower() for q in asked}
    lexical: list[Candidate] = []
    for candidate in candidates:
        text = " ".join(candidate.question.split())
        if not text.endswith("?") or len(text) > MAX_QUESTION_CHARS:
            dropped["shape"] += 1
            continue
        if candidate.passage not in texts:
            dropped["passage"] += 1
            continue
        if not grounded(candidate.evidence, texts[candidate.passage]):
            dropped["unsupported"] += 1
            continue
        if list_row(candidate.evidence, texts[candidate.passage]):
            dropped["list_entry"] += 1
            continue
        key = text.lower()
        if key in seen or any(_is_rewording(text, q) for q in asked):
            dropped["repeat"] += 1
            continue
        seen.add(key)
        lexical.append(replace(candidate, question=text))
    if not lexical or query_vector is None:
        return lexical, dropped
    try:
        vectors = _embed([c.question for c in lexical])
    except Exception:
        logger.debug("Could not embed follow-ups; lexical screen only.", exc_info=True)
        return lexical, dropped
    kept: list[Candidate] = []
    kept_vectors: list[Sequence[float]] = []
    for candidate, vector in zip(lexical, vectors):
        if _cosine(vector, query_vector) >= threshold:
            dropped["repeat"] += 1
            continue
        if any(_cosine(vector, other) >= threshold for other in kept_vectors):
            dropped["near_duplicate"] += 1
            continue
        kept.append(candidate)
        kept_vectors.append(vector)
    return kept, dropped


def rank(candidates: Sequence[Candidate], final_answer: str, n: int) -> list[str]:
    """The ``n`` to show, judged against the answer as it was finally sent.

    Passages the answer did not cite go first — the material the visitor has
    not been shown. Then one question per passage, so the suggestions point in
    different directions; only when that leaves fewer than ``n`` does a second
    question on the same passage get a place.
    """
    if n <= 0:
        return []
    cited = extract_markers(final_answer or "")
    ordered = ([c for c in candidates if c.passage not in cited]
               + [c for c in candidates if c.passage in cited])
    chosen: list[Candidate] = []
    passages: set[int] = set()
    for candidate in ordered:
        if candidate.passage not in passages:
            chosen.append(candidate)
            passages.add(candidate.passage)
    for candidate in ordered:
        if candidate not in chosen:
            chosen.append(candidate)
    return [c.question for c in chosen[:n]]


def unlisted(candidates: Sequence[Candidate], final_answer: str) -> list[Candidate]:
    """For a list answer: only the candidates on passages it did not cite.

    :func:`rank` merely puts those first, which is right for prose — a passage
    an answer cites is rarely used up by it. A list answer uses its passages up:
    it gives every row, so a question on one of them asks what the visitor has
    just read ("What is R Mukundan's role at Tata Chemicals?" under a list
    saying he is its Managing Director). Fewer suggestions, or none, beat that.
    """
    cited = extract_markers(final_answer or "")
    return [c for c in candidates if c.passage not in cited]


@dataclass
class Draft:
    """What the call's thread hands back: screened candidates and how it went."""

    candidates: list[Candidate] = field(default_factory=list)
    proposed: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    ms: float = 0.0


def draft_followups(
    question: str,
    blocks: Sequence[ContextBlock],
    *,
    answer_so_far: str,
    search_query: str = "",
    history: Sequence[dict[str, str]] | None = None,
    query_vector: Sequence[float] | None = None,
    n: int = 5,
    reasoning_effort: str = "",
    threshold: float = 0.92,
    list_answer: bool = False,
) -> Draft:
    """Propose and screen, timed. The body of the call's thread."""
    started = time.monotonic()
    proposed = generate_followups(
        question, blocks, answer_so_far=answer_so_far, search_query=search_query,
        history=history, n=n, reasoning_effort=reasoning_effort,
        list_answer=list_answer,
    )
    kept, dropped = screen(
        proposed, question=question, search_query=search_query, history=history,
        blocks=blocks, query_vector=query_vector, threshold=threshold,
    )
    return Draft(candidates=kept, proposed=len(proposed), dropped=dict(dropped),
                 ms=(time.monotonic() - started) * 1000)


class FollowupTask:
    """One answer's follow-up questions, from launch to the ones shown.

    Not thread-safe and not meant to be: the streaming generator drives it from
    whichever worker thread resumes it, one step at a time. Only the call itself
    runs elsewhere, on a single-thread executor this object owns — so
    :meth:`close` must run (the pipeline does it in a ``finally``) for a client
    that disconnects mid-answer.
    """

    def __init__(
        self,
        *,
        question: str,
        blocks: Sequence[ContextBlock],
        search_query: str = "",
        history: Sequence[dict[str, str]] | None = None,
        query_vector: Sequence[float] | None = None,
        prefix: str = "",
        launch_chars: int = 400,
        count: int = 3,
        wait_seconds: float = 10.0,
        reasoning_effort: str = "",
        threshold: float = 0.92,
        answer_format: str = "default",
    ) -> None:
        self._question = question
        #: A list answer gets the list note in the prompt and the stricter
        #: ranking (see :func:`unlisted`).
        self._list_answer = answer_format in LIST_FORMATS
        self._blocks = list(blocks)
        self._search_query = search_query
        self._history = list(history or [])
        self._query_vector = query_vector
        self._launch_at = max(0, int(launch_chars))
        self._count = max(0, int(count))
        self._wait = max(0.0, float(wait_seconds))
        self._effort = reasoning_effort
        self._threshold = threshold
        self._parts: list[str] = [prefix + "\n\n"] if prefix else []
        self._chars = sum(len(p) for p in self._parts)
        self._executor: ThreadPoolExecutor | None = None
        self._future: Any = None
        #: How much of the answer the call saw, for the trace.
        self.launched_at_chars: int | None = None

    @property
    def launched(self) -> bool:
        return self._future is not None

    def feed(self, token: str) -> None:
        """One streamed token. Launches at the first line break past the mark."""
        if self._future is not None or not token:
            return
        self._parts.append(token)
        self._chars += len(token)
        if self._chars >= self._launch_at and "\n" in token:
            self._launch("".join(self._parts))

    def finish(self, answer: str) -> None:
        """The answer has ended. Launch now if its opening never came — unless
        it is a refusal, which gets no follow-ups and so no call."""
        if self._future is None and not is_refusal(answer):
            self._launch(answer)

    def _launch(self, answer_so_far: str) -> None:
        self.launched_at_chars = len(answer_so_far)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="followups")
        self._future = self._executor.submit(
            draft_followups,
            self._question,
            self._blocks,
            answer_so_far=answer_so_far,
            search_query=self._search_query,
            history=self._history,
            query_vector=self._query_vector,
            # A couple more than are shown, so the screen still leaves enough.
            n=self._count + 2,
            reasoning_effort=self._effort,
            threshold=self._threshold,
            list_answer=self._list_answer,
        )

    def collect(self, final_answer: str) -> tuple[list[str], dict[str, Any]]:
        """The questions to show, and what to record about how they were got.

        Waits at most ``wait_seconds`` for the call; a late one is abandoned
        (its thread finishes in the background and its result is discarded).
        """
        info: dict[str, Any] = {"launched_at_chars": self.launched_at_chars}
        if self._future is None:
            info["skipped"] = "refusal" if is_refusal(final_answer) else "not launched"
            return [], info
        if is_refusal(final_answer):
            info["skipped"] = "refusal"
            return [], info
        started = time.monotonic()
        try:
            draft: Draft = self._future.result(timeout=self._wait)
        except FutureTimeout:
            self._future.cancel()
            info.update(timed_out=True,
                        waited_ms=round((time.monotonic() - started) * 1000, 1))
            logger.info("Follow-ups not ready %.1fs after the answer; sending none.",
                        self._wait)
            return [], info
        except Exception:
            logger.warning("Follow-up task failed; sending none.", exc_info=True)
            info["skipped"] = "error"
            return [], info
        candidates = draft.candidates
        dropped = dict(draft.dropped)
        if self._list_answer:
            info["list_answer"] = True
            candidates = unlisted(candidates, final_answer)
            if len(candidates) < len(draft.candidates):
                dropped["listed"] = len(draft.candidates) - len(candidates)
        questions = rank(candidates, final_answer, self._count)
        # Each shown question's passage and evidence, so a trace can answer
        # "why was this suggested?" with the words it stood on. Keyed "block",
        # not "passage": the trace redacts any key containing "pass".
        by_question = {c.question: c for c in candidates}
        info.update(
            ms=round(draft.ms, 1),
            waited_ms=round((time.monotonic() - started) * 1000, 1),
            proposed=draft.proposed,
            dropped=dropped,
            questions=questions,
            evidence=[{"block": by_question[q].passage,
                       "evidence": by_question[q].evidence} for q in questions],
        )
        return questions, info

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
