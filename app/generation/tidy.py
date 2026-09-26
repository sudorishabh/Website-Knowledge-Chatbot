"""Deterministic tidying of the list markup in an answer.

Three habits the prompt asks against and the small answering model keeps
anyway, measured on identical blocks on 2026-09-25 with the answer-shape rules,
the worked list example and the reminder beside the question all in place:

* a list drawn from one block cites that block on its opening sentence *and* on
  every item — nine centres, nine "[1]"s;
* an item the context gives no description for keeps the dash that would have
  introduced one — "**Centre for X** — [1]";
* the closing "Read more" line carries a citation, although the link is the
  source.

None of the three changes what the answer claims, so each is fixed here rather
than argued with. A list's citation moves to its opening sentence only when
every item cites the same single block, so no item loses a citation that
differed from its neighbours'; and the opening sentence always ends up
carrying it, so `query_pipeline._cited_blocks` and the sources footer see the
same blocks as before.

A grouped list — names under ### headings, as a selection of people is — has no
sentence above each group, so its items used to keep their markers: fifteen
people, fifteen "[2]"s, below an opening that already cited [1][2]. A run of
named items under a heading now hands its one shared marker to the answer's
opening, which gains it when it lacks it — the live answer that showed this
opened with no citation at all. Only named items ("- **Name** — ..."): an
overview's bullets are claims, and a claim keeps its own citation.
"""
from __future__ import annotations

import re

_BULLET = re.compile(r"^\s*(?:[-*]|\d+\.)\s+")
_HEADING = re.compile(r"^\s*#{1,6}\s")
_MARKER = re.compile(r"\[(\d+)\]")
# An em or en dash with nothing after it but citations and a full stop. Never a
# hyphen: item names carry them ("TERI-CFCL Centre").
_EMPTY_DESCRIPTION = re.compile(r"\s*[—–]\s*((?:\s*\[\d+\])*)\s*\.?\s*$")
_READ_MORE = re.compile(r"^\s*Read more:\s*\[[^\]]+\]\(https?://[^\s)]+\)")
_NAMED_ITEM = re.compile(r"^\s*(?:[-*]|\d+\.)\s+\*\*[^*]+\*\*")


def _drop_empty_description(line: str) -> str:
    match = _EMPTY_DESCRIPTION.search(line)
    if not match:
        return line
    markers = match.group(1).strip()
    return line[: match.start()] + (f" {markers}" if markers else "")


def _without_markers(line: str) -> str:
    out = _MARKER.sub("", line)
    # "sustainability [1]." leaves "sustainability ." behind, and a marker
    # mid-line leaves two spaces.
    out = re.sub(r"[ \t]+([.,;:])", r"\1", out)
    out = re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", out)
    return out.rstrip()


def _with_marker(line: str, n: str) -> str:
    """The opening sentence with ``[n]`` on it, before a closing colon or full
    stop if any — where rule 2 and the worked examples put a citation."""
    if f"[{n}]" in line:
        return line
    stripped = line.rstrip()
    if stripped.endswith((":", ".")):
        return f"{stripped[:-1].rstrip()} [{n}]{stripped[-1]}"
    return f"{stripped} [{n}]"


def _lead_index(lines: list[str], run_start: int) -> int | None:
    """The line introducing the list: the nearest line above it, past at most
    one blank line, that is neither a bullet nor a heading."""
    j = run_start - 1
    if j >= 0 and not lines[j].strip():
        j -= 1
    if j < 0:
        return None
    line = lines[j]
    if not line.strip() or _BULLET.match(line) or _HEADING.match(line):
        return None
    return j


def _under_heading(lines: list[str], run_start: int) -> bool:
    """Whether the run sits directly below a heading, past at most one blank."""
    j = run_start - 1
    if j >= 0 and not lines[j].strip():
        j -= 1
    return j >= 0 and bool(_HEADING.match(lines[j]))


def _opening_index(lines: list[str]) -> int | None:
    """The last line of the answer's opening — the prose before its first
    heading or list item — or None when the answer opens with either."""
    last = None
    for i, line in enumerate(lines):
        if _BULLET.match(line) or _HEADING.match(line):
            break
        if line.strip():
            last = i
    return last


def _collapse_run(lines: list[str], start: int, end: int, opening: int | None) -> None:
    """Move a single shared citation off every item of ``lines[start:end]``."""
    if end - start < 2:
        return
    cited = [set(_MARKER.findall(lines[i])) for i in range(start, end)]
    if len(cited[0]) != 1 or any(c != cited[0] for c in cited):
        return
    (n,) = cited[0]
    lead = _lead_index(lines, start)
    if lead is None:
        # A list under a heading has no sentence of its own to carry the
        # citation, so its named items hand it to the answer's opening, as a
        # flat list hands it to its lead. With no opening, or items that are
        # claims rather than names, the markers stay where they are.
        if (
            opening is not None
            and _under_heading(lines, start)
            and all(_NAMED_ITEM.match(lines[i]) for i in range(start, end))
        ):
            lines[opening] = _with_marker(lines[opening], n)
            for i in range(start, end):
                lines[i] = _without_markers(lines[i])
        return
    lines[lead] = _with_marker(lines[lead], n)
    for i in range(start, end):
        lines[i] = _without_markers(lines[i])


def tidy_lists(answer: str) -> str:
    """The answer with the three list habits above corrected; otherwise
    byte-for-byte unchanged, so a clean answer never triggers a correction."""
    lines = answer.split("\n")
    for i, line in enumerate(lines):
        if _BULLET.match(line):
            lines[i] = _drop_empty_description(line)
        elif _READ_MORE.match(line) and _MARKER.search(line):
            lines[i] = _without_markers(line)

    opening = _opening_index(lines)
    i = 0
    while i < len(lines):
        if not _BULLET.match(lines[i]):
            i += 1
            continue
        start = i
        while i < len(lines) and _BULLET.match(lines[i]):
            i += 1
        _collapse_run(lines, start, i, opening)
    return "\n".join(lines)
