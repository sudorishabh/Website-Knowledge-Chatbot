"""A person's latest publications, from the catalog.

Asked for someone's work ("Suruchi Bhadwal work"), the answer gives what their
profile says and then the newest documents the catalog records them as an
author of. The catalog keeps each author string as the document printed it, so
one person is often several strings: on 2026-09-28 Manish Kumar Shrivastava
was 18 documents under his full name, 2 as "Shrivastava Manish Kumar", 2 as
"Mr Manish Shrivastava" and 1 as "Shrivastava M K". A variant is counted as
theirs only when it fits them and no other author in the catalog: "Datta A"
fits Alekhya Datta and Arindam Datta alike, so it is neither's.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Sequence

from app.catalog import queries
from app.catalog.models import StateRecord
from app.retrieval.structured.entities import entity_label
from app.retrieval.structured.resolve import known_authors

#: Publications listed for a person's work.
LATEST = 5

_HONORIFICS = frozenset({"dr", "mr", "ms", "mrs", "prof", "professor", "shri", "smt", "sir"})


def _tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[a-z]+", (name or "").lower()) if t not in _HONORIFICS]


def _fits(name: Sequence[str], person: Sequence[str]) -> bool:
    """Whether ``name`` can be ``person`` written another way: it carries the
    person's surname and their first name or its initial, in any order, and
    nothing their name does not."""
    if len(name) < 2 or len(person) < 2:
        return list(name) == list(person)
    *given, surname = person
    if surname not in name:
        return False
    rest = list(name)
    rest.remove(surname)

    def carried(token: str) -> bool:
        return token in given or (len(token) == 1 and any(g.startswith(token) for g in given))

    first = given[0]
    return any(t in (first, first[0]) for t in rest) and all(carried(t) for t in rest)


def author_names(person: str, authors: Sequence[str]) -> list[str]:
    """The stored author strings that are ``person``: their own name, and each
    variant that fits them and no other author sharing their surname."""
    target = _tokens(person)
    if not target:
        return []
    names = {a: _tokens(a) for a in authors if a}
    mine = [a for a, tokens in names.items() if _fits(tokens, target)]
    rivals = [tokens for a, tokens in names.items()
              if a not in mine and target[-1] in tokens and sum(len(t) > 1 for t in tokens) >= 2]
    return [a for a in mine
            if names[a] == target or not any(_fits(names[a], rival) for rival in rivals)]


def latest_publications(person: str, *, limit: int = LATEST) -> tuple[list[StateRecord], int]:
    """The newest ``limit`` documents ``person`` is an author of, and how many
    there are in all. Website pages only, as every catalog listing is: a PDF
    attached to a page carries the page's title and authors too, and would list
    each publication twice."""
    names = author_names(person, known_authors())
    if not names:
        return [], 0
    scope = {"source_type": "website", "entity_type": "node", "authors": names}
    return queries.list_documents(**scope, limit=limit), queries.count_documents(**scope)


def _date(value: str | None) -> str:
    try:
        day = date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return ""
    return f"{day.day} {day:%B %Y}"


def _line(record: StateRecord) -> str:
    title = (record.title or record.document_id).replace("[", "(").replace("]", ")")
    head = f"[{title}]({record.url})" if record.url else title
    detail = ", ".join(part for part in (
        entity_label(record.bundle, 1) if record.bundle else "",
        _date(record.effective_start_date),
    ) if part)
    return f"- {head} — {detail}" if detail else f"- {head}"


def publications_section(person: str, records: Sequence[StateRecord], total: int) -> str:
    """The records as a closing section of the answer, or '' when there are none."""
    if not records:
        return ""
    lines = [f"### Latest publications by {person}"]
    if total > len(records):
        lines.append(f"The {len(records)} most recent of {total}:")
    lines += [_line(r) for r in records]
    return "\n".join(lines)
