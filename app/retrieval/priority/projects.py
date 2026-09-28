"""The projects a named person has a stated role in, from the ingested project pages.

A profile page is a biography, and no record links a person to a project: a
project page is a title, two dates and a PDF, and the knowledge graph links
people to organisations only. The project text does name them, as "Principal
Investigator: ...", "Team: ..." or "Contact ...": on 2026-09-28 Suruchi Bhadwal
was named in 30 project chunks. Only a mention in one of those roles counts; a
reviewer, a moderator or a guest on a radio programme did not do the project.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.config import get_settings
from app.core.models.context import ContextBlock
from app.observability import retrieval_log

logger = logging.getLogger(__name__)

PROJECT_BUNDLES = ("completed_projects", "ongoing_projects")
#: Projects read into the context for one person, latest first.
MAX_PROJECTS = 4
#: The payload field that marks a project block with whose role it shows.
PROJECT_OF = "project_of"
#: Chunks read to find the mentions; Suruchi Bhadwal had 30.
_SCAN = 200
#: How far before a name its role may stand: "Team: Ms Suruchi Bhadwal; Dr Manish
#: Kumar Shrivastava" names the second member 45 characters after "Team".
_REACH = 120

_HONORIFICS = frozenset({"dr", "mr", "ms", "mrs", "prof", "professor", "shri", "smt", "sir"})
_HONORIFIC_DOT = re.compile(r"\b(?:dr|mr|ms|mrs|prof|shri|smt)\.", re.I)
_ROLE = re.compile(
    r"\b(?:co-?principal investigator|principal investigator|co-?investigator|investigator"
    r"|project (?:leader|lead|director|coordinator|manager|team)|team(?: members?)?"
    r"|contact(?: details| person)?|for more information|coordinator|led by)\b",
    re.I,
)
_SENTENCE_END = re.compile(r"[.!?]\s")
#: An acknowledgement names people it thanks, not people who did the work:
#: "gratitude, the guidance and support received from Charter advisory team, Dr
#: Vibha Dhawan" reads as a team role and is a thank-you.
_THANKS = re.compile(r"acknowledg|gratitude|grateful|thank", re.I)


def _name_pattern(name: str) -> re.Pattern[str] | None:
    tokens = [t for t in re.findall(r"[A-Za-z]+", name) if t.lower() not in _HONORIFICS]
    if not tokens:
        return None
    return re.compile(r"\b" + r"[.\s]*".join(map(re.escape, tokens)) + r"\b", re.I)


def role_of(text: str, name: str) -> str | None:
    """The role ``text`` gives ``name`` where it names them, or None: a role
    heading shortly before the name, with no sentence ending in between, and
    not in a thank-you."""
    pattern = _name_pattern(name)
    if pattern is None:
        return None
    for found in pattern.finditer(text):
        before = text[max(0, found.start() - _REACH):found.start()]
        # "Dr." is not the end of a sentence.
        window = _HONORIFIC_DOT.sub(lambda m: m.group(0)[:-1], before)
        roles = list(_ROLE.finditer(window))
        if not roles or _THANKS.search(window):
            continue
        if not _SENTENCE_END.search(window[roles[-1].end():]):
            return roles[-1].group(0).lower()
    return None


def _scroll(name: str) -> list[Any]:
    from qdrant_client.models import FieldCondition, MatchAny, MatchText

    from app.core.clients.vector_store import get_qdrant_client
    from app.retrieval.search.hybrid_search import build_filter

    words = " ".join(t for t in re.findall(r"[A-Za-z]+", name) if t.lower() not in _HONORIFICS)
    settings = get_settings()
    scope = build_filter(extra=[
        FieldCondition(key="chunk_text", match=MatchText(text=words)),
        FieldCondition(key="bundle", match=MatchAny(any=list(PROJECT_BUNDLES))),
    ])
    with retrieval_log.qdrant_call(
        "scroll", stage="person_projects",
        request=lambda: {"collection": settings.qdrant_collection, "name": words, "limit": _SCAN},
    ) as call:
        points, _ = get_qdrant_client().scroll(
            settings.qdrant_collection, scroll_filter=scope, limit=_SCAN,
            with_payload=True, with_vectors=False,
        )
        call.qdrant_results(points)
    return list(points)


def person_projects(name: str, *, limit: int = MAX_PROJECTS) -> list[ContextBlock]:
    """One block for each project whose text names ``name`` in a role, the
    latest ``limit`` by the project's date. Never raises: a failed search costs
    the projects, not the answer."""
    try:
        points = _scroll(name)
    except Exception:
        logger.warning("Projects naming %s could not be searched.", name, exc_info=True)
        return []
    projects: dict[str, dict[str, Any]] = {}
    for point in points:
        payload = dict(point.payload or {})
        text = payload.get("chunk_text") or ""
        role = role_of(text, name)
        if role is None:
            continue
        key = str(payload.get("source_url") or payload.get("document_id"))
        kept = projects.get(key)
        # The project page's own text over an attachment's, whose title is
        # often the file's address.
        if kept is None or (payload.get("source_type") == "website"
                            and kept.get("source_type") != "website"):
            projects[key] = {**payload, PROJECT_OF: name, "project_role": role}
    latest = sorted(projects.values(), key=lambda p: str(p.get("effective_start_date") or ""),
                    reverse=True)[:limit]
    return [ContextBlock(n=0, text=p.pop("chunk_text", ""), payload=p, score=1.0)
            for p in latest]
