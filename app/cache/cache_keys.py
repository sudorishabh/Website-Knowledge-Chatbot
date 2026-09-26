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
PIPELINE_REVISION = "2026-09-25.12"


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
