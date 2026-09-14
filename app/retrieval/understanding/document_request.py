"""Is the user asking *for* a document, or *about what is inside* one?

The two questions look alike and are answered from completely different things.
"What does the 2024-25 annual report say about solar?" is answered from the
report's prose. "Give me the latest annual report" is answered by the document's
own identity — and no amount of page text will contain the sentence "this is the
latest annual report", because no report says that about itself.

Conflating them is what produced the observed failure: "give me latest annual
report" resolved correctly to the 2024-25 edition, retrieved two chunks from
deep inside it, and then refused, because the grounding prompt's rule 3 is right
that pages 148-150 do not bear on which edition is the latest one.

Deliberately lexical
--------------------
This is a routing decision made before generation, on the user's own words, so
it must be free and reproducible. An LLM call here would add latency and
variance to a question the phrasing already answers — and the grounding prompt
already encodes the same distinction in prose (see rule 3's "where can I
find/get/download X" clause), so the vocabulary is not being invented here.

Two signals, and both have to agree
-----------------------------------
A request is a document request when it **asks for the thing** and does **not
ask about its contents**. Requiring both is what keeps "what are the key
findings in the latest annual report?" on the content path: it names the
document, but what it wants is inside it.

The asymmetry is on purpose. A miss here costs nothing — the question falls
through to content QA, which is exactly today's behaviour — while a false
positive answers "here is the document" to someone who asked what it says. So
the content cues win ties.
"""
from __future__ import annotations

import re

__all__ = ["is_document_request"]

# Asking to be handed the document, or told where it is. "where can I find X" is
# here rather than on the content side because it is a locate question, which is
# the reading `app.generation.prompts` rule 3 already takes.
_LOCATE = re.compile(
    r"\b(?:give|show|send|share|provide|fetch|get|bring)\s+(?:me|us)\b"
    r"|\bwhere\s+(?:can|could|do|would|should)\s+(?:i|we|one)\s+"
    r"(?:find|get|download|access|read|see)\b"
    r"|\bwhere\s+(?:is|are)\b"
    r"|\bdo\s+you\s+have\b"
    r"|\bi\s+(?:want|need)\b"
    r"|\b(?:link|links)\s+(?:to|for)\b"
    r"|\bdownload\b"
    r"|\bpoint\s+me\s+to\b",
    re.IGNORECASE,
)

# Asking about what the document contains. Any of these outranks a locate cue —
# "show me what the annual report says about solar" wants the prose, not a link.
_ABOUT_CONTENT = re.compile(
    r"\bsays?\b|\bsaid\b|\bmentions?\b|\bstates?\b|\bcovers?\b|\bcontains?\b"
    r"|\bdiscuss\w*\b|\bfindings?\b|\bhighlights?\b|\bconclusions?\b"
    r"|\brecommendations?\b|\bkey\s+points?\b|\btakeaways?\b"
    r"|\bsummar\w+\b|\bexplain\b|\bdescribe\b|\banalys\w+\b"
    r"|\baccording\s+to\b|\bcontents?\b|\bwhat'?s\s+in\b|\bwhat\s+is\s+in\b"
    r"|\babout\b|\bregarding\b|\bfigures?\b|\bnumbers?\b|\bdata\b",
    re.IGNORECASE,
)


def is_document_request(question: str | None) -> bool:
    """Whether this turn asks for the document itself rather than its contents.

    ``False`` for anything unclear, which routes the question exactly where it
    goes today.
    """
    text = question or ""
    if not text.strip():
        return False
    if _ABOUT_CONTENT.search(text):
        return False
    return bool(_LOCATE.search(text))
