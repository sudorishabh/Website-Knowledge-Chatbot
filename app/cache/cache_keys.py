from __future__ import annotations

import hashlib

from app.catalog.queries import corpus_revision
from app.config import get_settings


def _sha(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


#: The read path's behaviour revision. Bumped by hand — the date of the change
#: is the convention — when a code change alters what the *same* question should
#: be answered with: how candidates are ranked, how the context is composed,
#: what the prompt asks for. Settings changes self-invalidate through
#: `_pref_fingerprint`; a code change has no knob to hash, so without this an
#: answer generated under the old behaviour is served for the rest of its TTL
#: (24 hours by default) and a ranking fix looks, from the chat window, like it
#: did nothing. That is how the fix for "who is X" answering from a 2020 page
#: was reported as not working three times: each retest matched the cached
#: answer at cosine 1.0.
#: 2026-09-24.1: priority pages read live now lead the context, and the stored
#: copies of those pages are no longer used.
#: 2026-09-25.1: sub-themes are gone from question time — no sub-theme
#: listings, and a theme filter matches its exact name only.
#: 2026-09-25.2: the list of themes is answered from the live home page, and
#: the theme pages are a flat list named "<theme> Theme".
#: 2026-09-25.3: answers take a shape (list, overview, direct fact) with
#: headings, lighter citations and a "Read more" link; a question naming one
#: theme is no longer answered with the theme list.
#: 2026-09-25.5: a compressed shape reminder follows the question.
#: 2026-09-25.6: a single-source list is cited once, on its opening sentence.
#: 2026-09-25.7: the shape reminder opens with the context-only rule.
#: 2026-09-25.8: a bare phrase naming the organisation's people or one of its
#: listed pages is answered from retrieval instead of by small talk.
#: 2026-09-25.9: a question for the organisation's people reads its people
#: listings.
#: 2026-09-25.10: a broad question is answered on its named reading, a long list
#: is grouped, and several people listings are named beside the question.
#: 2026-09-25.11: a question for the top or leading members of a long set is
#: answered with a selection of them, grouped and indexed by area.
#: 2026-09-25.12: the head of the organisation always leads a selection.
#: 2026-09-26.1: a summary of one theme is answered from its live page, and a
#: theme listing that also asks for content no longer repeats the catalog's list.
#: 2026-10-04.1: catalog answers follow their headline with detail, list items
#: are linked, and a count of a person's publications stays on the catalog.
#: 2026-10-04.6: a person's "articles" spans everything they published, a list
#: of what a person wrote stays on the catalog, and a count of one type by a
#: person gives their total across types.
#: 2026-10-04.10: a count or list of one type names the person's or theme's
#: other types, recent lists show five items with bylines, and every theme
#: listing gives each theme's count and newest item.
#: 2026-10-04.12: catalog answers are laid out as generated ones are — a lead
#: with bold figures, titled sections, an italic follow-up.
#: 2026-10-04.13: a request for the list of themes is answered theme by theme —
#: what each covers and holds — and only the home page's themes get figures.
#: 2026-10-04.14: a listed person named with a misspelling ("PK bhatacharia")
#: is answered from their profile.
#: 2026-10-04.15: a question about a subject ("what is renewable energy") is
#: answered as an overview from the angles the context covers, never as one fact.
#: 2026-10-04.16: a person asked for by post ("who is the director general") is
#: answered from the holder's profile, and every person question as a profile.
#: 2026-10-05.1: a series, programme or acronym no facet holds ("WSDS events in
#: 2026") is counted and listed by its titles in every spelling, as is a
#: count's subject no theme matches; a count of a kind of content goes to the
#: catalog whatever route it was read as, and the graph no longer replaces it;
#: a small count lists every item; a zero under a type and a period names the
#: subject's other types; and a theme no document carries no longer empties
#: passage search.
#: 2026-10-17.1: presence, office, branch and location questions (typos
#: forgiven) reach the regional centres, whose block carries each centre's
#: address and contacts from the live contact page, and the answer gives them.
#: 2026-10-17.3: the Centres of Excellence are a group of nine, each described
#: and linked; a question naming one reads that centre's live page; the
#: longest group phrase decides which group a question asks for.
#: 2026-10-21.1: a theme named by part of its name ("the marine theme") reads
#: that theme's live page; one several themes share ("the biofuel theme") is
#: asked about, and the reply chooses one or all of them.
#: 2026-10-21.2: a count or list on a topic its theme holds a corner of ("how
#: many publications on biofuels") is counted by title, and a near-tie between
#: theme pages sharing the question's words reads each of them.
#: 2026-10-21.3: a people question about a theme or centre ("people who
#: contributed", "who works on", "the team") is answered from that page's
#: Team section, with the staff listings only for a choice among people,
#: and a people listing stays one block.
#: 2026-10-21.4: the themes asked for by another name ("research areas", "TERI
#: divisions", "what sectors does TERI work in") are a theme listing, and one
#: asking for nothing else gets the whole overview.
#: 2026-10-21.5: "themes" alone gets the overview rather than a clarification,
#: and a live page whose connection fails outright is asked for once more.
#: 2026-10-21.6: people on the theme and centre Team sections are recognised by
#: name, and a question for a named person's work adds the projects that name
#: them in a role and, after the answer, their latest publications.
#: 2026-10-22.1: a request for the Centres of Excellence and nothing else is
#: answered with the names their live page lists, citing that page alone.
#: 2026-10-22.2: "divisions" or "departments" alone, misspelt, counted ("how
#: many are there") or in the singular beside TERI's name gets the overview.
#: 2026-10-22.3: a named person's profile leads a question about what they
#: wrote, with the catalog's count or list after the answer, and a page whose
#: body fails mid-read no longer drops the question's other live pages.
PIPELINE_REVISION = "2026-10-22.3"


def _pref_fingerprint() -> str:
    """Hash of the settings that decide what an answer says, plus
    `PIPELINE_REVISION`, so that toggling the feature, tuning its knobs,
    switching the model or shipping a behaviour change self-invalidates the
    semantic cache (otherwise old-mode answers would be served until TTL and
    pollute before/after comparisons).

    The chat model and the two knobs that shape its calls are here because a
    model is the largest single change to an answer there is, and it used to be
    missing: switching to gpt-6-luna on 2026-09-25 would have kept serving, for
    a day, every answer the previous model wrote."""
    s = get_settings()
    return _sha(
        PIPELINE_REVISION,
        str(s.prefer_website_enabled),
        str(s.website_candidate_k),
        str(s.retrieval_top_k),
        str(s.retrieval_candidate_k),
        str(s.context_token_budget),
        str(s.priority_own_slots),
        str(s.azure_openai_model),
        str(s.llm_temperature_supported),
        str((s.llm_reasoning_effort or "").strip()),
    )


def semantic_partition(top_k: int, answer_format: str) -> str | None:
    """Partition key for the semantic cache, or None when it must not be used.

    Four things decide whether a stored answer is still the answer: the
    retrieval-preference fingerprint, the result width, the answer format, and
    **the state of the corpus it was grounded in**. The last one used to be
    missing, so an answer survived any amount of ingestion and could be served
    for the whole TTL quoting text that had since been re-indexed or deleted.

    ``None`` when the corpus revision is unknown: an answer that cannot be dated
    against the corpus cannot be shown to be fresh, and bypassing the cache is
    the only safe reading of that. Callers skip the cache rather than fall back
    to a partial key.

    Caller identity is deliberately absent. The corpus is public and every
    caller retrieves over all of it, so two callers asking the same question
    are owed the same answer — partitioning by identity would only fragment
    the cache."""
    revision = corpus_revision()
    if revision is None:
        return None
    return _sha(_pref_fingerprint(), str(top_k), answer_format, revision)
