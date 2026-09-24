"""Tolerant parsing of the ISO date bounds an LLM supplies.

Every date scope in the app originates as a string an LLM wrote into a
structured-output field, so the values are only *mostly* ISO. The observed
failure is trailing JSON punctuation swallowed into the string literal — the
model emits ``"date_to":"2022-01-01},"`` and the lenient JSON parser hands us
``'2022-01-01},'`` rather than raising.

That mattered because a bound that fails to parse is dropped, and a dropped
bound silently *widens* the query: "between 2020 and 2021" answers as "since
2020" and reports a confidently wrong number. Two defences live here:

- :data:`IsoDate` sanitizes at the model boundary, so a salvaged value reaches
  SQL *and* the answer text that echoes the scope back to the user;
- :func:`parse_iso_date` logs whatever it still cannot read, so a genuinely
  unreadable bound leaves a trace instead of vanishing.

Both keep the "degrade, don't raise" contract the retrieval layer relies on: a
bad date must not turn a working query into an error.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any

from pydantic import BeforeValidator

logger = logging.getLogger(__name__)

# A leading ISO 8601 date, optionally with a time and UTC offset. Anchored at the
# start and matched as a *prefix* so trailing junk is discarded rather than
# failing the whole value; the shape is enumerated rather than delegated to
# `fromisoformat` so that what we accept does not drift with the Python version
# (3.11+ reads bare "2024" as a date, which is far too loose for a bound).
_ISO_PREFIX = re.compile(
    r"\s*(\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)?"
    r")"
)


def clean_iso_date(value: Any) -> str | None:
    """The ISO date at the start of ``value``, or None if there isn't one.

    Salvages an LLM-mangled bound (``'2022-01-01},'`` -> ``'2022-01-01'``) and
    rejects anything without a recognizable leading date, so a filter is either
    correct or absent — never subtly wrong."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    match = _ISO_PREFIX.match(value)
    if match is None:
        return None
    return match.group(1)


def parse_iso_date(value: str | None, *, field: str = "date") -> datetime | None:
    """``value`` as a naive UTC datetime, or None when it isn't a usable date.

    Tolerates the trailing junk :data:`IsoDate` would have stripped, so callers
    holding a raw LLM value are safe too. Anything unreadable is logged and
    returns None — the caller drops the bound, which is why it must be visible.

    Offsets are normalized to naive UTC to match the ``effective_start_date`` DATETIME
    column, which stores UTC without a zone (see ``app.catalog.state``)."""
    if not value:
        return None
    cleaned = clean_iso_date(value)
    if cleaned is None:
        logger.warning(
            "Unreadable %s bound %r; dropping it. The query will answer over a "
            "wider period than asked for.", field, value,
        )
        return None
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Unparseable %s bound %r; dropping it.", field, value)
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


# Date-bound field type for the LLM-facing models. Sanitizing here rather than at
# each use site means the cleaned value is what gets echoed back to the user,
# cached, and logged — not just what reaches SQL.
IsoDate = Annotated[str | None, BeforeValidator(clean_iso_date)]


def exclusive_end(inclusive_end: str | None) -> str | None:
    """The half-open upper bound for a period ending on (and including)
    ``inclusive_end`` — i.e. the day after it.

    The catalog compares ``effective_start_date < %s``, so a bound has to be exclusive;
    users, however, say inclusive ends ("between Jan 1 and Dec 31", "up to the
    5th"). Doing that +1 day here rather than asking the LLM for it is the whole
    point: the model reliably copies a date the user typed but unreliably
    increments one, and when it forgets, the last day of the range silently
    disappears from the answer — or, for a single-day query where both bounds are
    the same date, every row does.

    Returns None for an unusable value, matching :func:`parse_iso_date`: the
    caller then has no upper bound, which is the pre-existing degrade path."""
    parsed = parse_iso_date(inclusive_end, field="date_to_inclusive")
    if parsed is None:
        return None
    return (parsed + timedelta(days=1)).date().isoformat()


def inclusive_end(exclusive_bound: str | None) -> str | None:
    """Inverse of :func:`exclusive_end`, for describing a range back to the user.

    A scope echoed as "between 2020-01-01 and 2022-01-01" reads as though 2022 is
    included when the bound is exclusive; naming the last day actually covered
    keeps the stated interpretation honest."""
    parsed = parse_iso_date(exclusive_bound, field="date_to")
    if parsed is None:
        return None
    return (parsed - timedelta(days=1)).date().isoformat()


def today_utc() -> date:
    """Today in UTC — the zone ``effective_start_date`` is stored in (see
    ``app.catalog.state``), so a bound derived from "today" lines up with the
    column it is compared against."""
    return datetime.now(timezone.utc).date()


def current_date_directive() -> str:
    """Prompt block anchoring relative date expressions to the real today.

    Every date-extracting prompt needs this: without it the model resolves "last
    6 months" or "this year" against its training data, which silently answers a
    window years away from the one asked for. That failure is invisible — the
    dates come back well-formed, just wrong.

    Called per request rather than folded into the module-level prompt constants
    on purpose: the API process can stay up for weeks, and a date captured at
    import would drift further from reality every day it ran. It is appended
    *after* the static prompt text so the long stable prefix stays byte-identical
    and remains prompt-cacheable."""
    today = today_utc()
    year = today.year
    return (
        "\n\n## Today's date\n"
        f"Today is {today:%A}, {today:%Y-%m-%d} (UTC). Resolve EVERY relative date "
        "expression against that date and never against your training data — "
        "'this year', 'last month', 'recently', 'the past six months' and "
        "'since March' are all meaningless without it.\n"
        f"- This year runs {year}-01-01 to {year}-12-31.\n"
        f"- Last year runs {year - 1}-01-01 to {year - 1}-12-31.\n"
        "- For a rolling window ('the last N days/months'), count back from "
        "today.\n"
        f"- A period running up to now ends {today:%Y-%m-%d}.\n"
        "If the request names no period at all, leave both dates null — do not "
        "default to the current year."
    )
