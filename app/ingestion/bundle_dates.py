"""Which date(s) each Drupal bundle *means* — declared once, per bundle.

A Drupal record carries several dates and which ones matter depends entirely on
what kind of content it is. A news item has one date; a completed project has a
start *and* an end; a research paper states only a year; an article has nothing
but its creation stamp. There is no algorithm that can work that out, so the
knowledge is **data, declared once** in :data:`BUNDLE_DATE_FIELDS`, and the
resolution logic below is generic:

    bundle -> ordered list of fields -> extract -> normalise -> effective date(s)

A bundle maps to **one or more** fields, in order:

    (start,)          a single-date bundle
    (start, end)      a bundle whose content covers a period

Neither is a special case of the other — the same loop resolves each configured
field the same way, and the only thing a second field adds is the range
validation in :func:`_with_range`. Adding a bundle, or giving an existing bundle
an end date, is one row in that table. No branch in this module, and none in the
ingestion pipeline, names a bundle.

**What "effective date" means here.** It is the date the *content* is about, as
the CMS declares it per content type — not a claim that the publisher stated a
publication date on that day. For ``news``, ``press_release``, ``report`` and
``research_papers`` the two coincide. For ``completed_projects``,
``ongoing_projects`` and ``events`` the configured field is a project start or an
event date, which the site treats as that item's date and which this module
therefore applies. :mod:`app.ingestion.source_dates` still records what each
field *is* (:data:`~app.ingestion.source_dates.FIELD_ROLES`), and that
classification travels with the decision so an auditor can see which of the two
cases a given document is.

**The start date stays primary.** It is what reaches ``effective_start_date`` and what
every ranking, ordering and filtering path already reads. The end date is
retained beside it as business metadata — it is what a date-*range* question
would need — but nothing in retrieval acts on it yet, deliberately.

**Nothing is invented.** A record either carries a date its source states, or it
carries no date at all and is flagged and counted as such by the pipeline. An end
date is never manufactured from a start, and a start is never manufactured from
an end. The one fallback is the record's own ``created`` stamp, which is a real
date the source states about the record and is what this column has always held.

The parallel module for the other half of the problem is
:mod:`app.ingestion.date_resolution`, which decides an attached PDF's date. It
does not re-derive anything: it inherits what this module produced for the PDF's
parent page, both endpoints together.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from app.ingestion.source_dates import (
    FIELD_ROLES,
    Precision,
    as_stored_date,
    classify,
    is_plausible,
    to_ist_date,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BUNDLE_DATE_FIELDS",
    "CREATED",
    "EffectiveDate",
    "RangeIssue",
    "Source",
    "describe",
    "end_field_for",
    "fields_for",
    "inherited",
    "precision_of",
    "resolve_effective_dates",
]

#: The sentinel a bundle maps to when its own creation stamp is the answer. Not
#: a real Drupal field name, and deliberately spelled the same as the JSON:API
#: attribute it stands for so the mapping reads as the business rule does.
CREATED = "created"

#: Where a resolved date came from. These are the values that reach
#: ``documents.date_source`` (VARCHAR(16)).
#:
#: ``created``       the record's own creation stamp — its bundle maps to
#:                   ``created``, or the configured field had nothing usable.
#: ``cms_field``     the bundle's configured date field stated it.
#: ``parent_page``   an attachment inheriting its Drupal page's resolved dates.
#: ``document_text`` a publication statement quoted and verified inside a PDF
#:                   (set by :mod:`app.ingestion.date_resolution`, not here).
#: ``document_copyright`` a copyright year the PDF states, corroborated by its
#:                   own DocInfo creation year (same module). Separate from
#:                   ``document_text`` because the evidence is weaker and
#:                   coarser: a copyright line establishes a year, a
#:                   publication statement establishes a day. Labelling the
#:                   first as the second would make a claim the evidence does
#:                   not carry.
#: ``document_title`` a date the file's own link text, filename or internal
#:                   title states (:mod:`app.ingestion.title_dates`). Its own
#:                   value rather than ``document_text`` because the evidence is
#:                   a *name*, not a sentence: "Annual Report 2024-25" is a
#:                   strong claim about a year and no claim at all about a day,
#:                   and a reader must be able to tell it from a quoted
#:                   publication statement.
#: ``no_evidence``   there is no date. One of several PDFs on a page whose own
#:                   date is only its Drupal creation stamp, and the file states
#:                   nothing about itself. Recorded rather than left NULL so
#:                   "we looked and found nothing" is distinguishable from "this
#:                   row predates the resolver", which is what
#:                   ``reconcile.date_checks.unrecorded_date_source`` alarms on.
Source = Literal[
    "created", "cms_field", "parent_page", "document_text", "document_copyright",
    "document_title", "no_evidence",
]

#: What is wrong with a record's date *range*, when something is. ``None`` means
#: either a well-formed range or no range at all — the two cases a reviewer never
#: has to look at. Stored on ``{state}_date_decision.range_issue`` (VARCHAR(24)).
#:
#: ``inverted``          the start is after the end. Never silently swapped.
#: ``end_invalid``       the end field held something that is not a usable date.
#: ``end_without_start`` the end is usable and the start is not, so the start
#:                       falls back and the range is incomplete rather than lost.
RangeIssue = Literal["inverted", "end_invalid", "end_without_start"]

#: ``bundle -> the ordered fields that carry its dates``.
#:
#: The first field is the effective/start date; a second, where present, is the
#: end of the period the content covers. Every bundle and every field here was
#: verified against the live JSON:API: the bundle exists, the attribute exists on
#: its records, and its values are single-valued scalars of the shape declared.
#:
#: ``services`` is not in the supplied mapping but *is* crawled
#: (:data:`app.core.corpus.DEFAULT_BUNDLES`) and carries no date-like field at
#: all. It is declared as ``created`` rather than omitted so that
#: "a crawled bundle nobody has classified" stays a meaningful alarm
#: (``reconcile.date_checks.unmapped_bundle_dates``) instead of firing on the
#: same bundle forever — the same reason ``FIELD_ROLES`` declares the fields it
#: refuses.
#:
#: ``block_content:basic`` is crawled too and is deliberately absent: it is not
#: a node bundle, it has no ``created`` attribute, and
#: ``drupal_extractor._created_at`` already resolves it to ``revision_created``.
#: It resolves through the unmapped default, which is that same stamp.
BUNDLE_DATE_FIELDS: dict[str, tuple[str, ...]] = {
    # ---- the record's own creation stamp is the date the site displays ----
    "article": (CREATED,),
    "page": (CREATED,),
    "feature_articles": (CREATED,),
    "policy_brief": (CREATED,),
    "videos": (CREATED,),
    "infographics": (CREATED,),
    "people": (CREATED,),
    "services": (CREATED,),  # not in the supplied map; see above

    # ---- a stated publication date ----
    "news": ("field_news_date",),
    "press_release": ("field_pressrelease_date",),
    # Live values carry +05:30 offsets and real clock times, unlike the
    # +00:00 IST-midnight shape every other date field uses. `to_ist_date`
    # normalises both. Null on 2 of the 8 live records, which is what makes the
    # empty-field fallback load-bearing rather than defensive.
    "report": ("field_report_date",),
    # An integer year on the wire (2012-2019 observed), not a string. Year
    # precision, taken from FIELD_ROLES: stored as 1 January *as a marker*, and
    # every reader is expected to check `start_precision` before
    # rendering the day.
    "research_papers": ("field_rpaper_year",),

    # ---- the date the content is about, which this site treats as its date ----
    # These are classified `range_start`/`range_end` in `source_dates.FIELD_ROLES`: a
    # project's start and a conference's date are not statements about when a
    # page was written. The business requirement is that they are nevertheless
    # the effective date for their bundles, so they are applied here — and the
    # role travels on the decision (`EffectiveDate.field_role`) so the
    # distinction stays visible to an auditor rather than being erased.
    "completed_projects": ("field_completed_start_date",
                           "field_completed_end_date"),
    "events": ("field_event_start_date", "field_event_end_date"),
    # Single-field on purpose. `field_ongoing_end_date` does not exist: Drupal
    # answers a filter on it with "the field does not exist", and the attribute
    # is absent from all 595 published records. Which is also the semantically
    # right answer — an *ongoing* project has no end date.
    "ongoing_projects": ("field_ongoing_start_date",),
}

#: Bundles already logged as unmapped, so a corpus-wide crawl of an unknown
#: bundle costs one line rather than one per document.
_warned_unmapped: set[str] = set()


def precision_of(field: str | None) -> Precision:
    """How precise a field's values are.

    Read from :data:`~app.ingestion.source_dates.FIELD_ROLES`, which already
    declares it for every field this module maps, rather than restated per
    bundle. Precision is a property of the *field* — ``field_rpaper_year`` holds
    a year whichever bundle points at it — so declaring it twice would be two
    facts that can disagree.
    """
    if not field or field == CREATED:
        return "day"
    entry = FIELD_ROLES.get(field)
    return entry[1] if entry else "day"


@dataclass(frozen=True)
class EffectiveDate:
    """One document's date(s), and everything needed to explain them.

    ``value`` is the effective/start date and the only field that reaches
    ``effective_start_date``; ``end_value`` is the end of the period the content covers,
    and is ``None`` for a single-date bundle and for a range whose end was
    missing or unusable. The rest is provenance: it answers "why does this
    document carry these dates?" without a reader re-deriving anything.

    ``fields`` and ``raw_values`` are parallel tuples in the configured order,
    so a bundle that one day carries three dates needs no new attributes here.
    """

    #: The stored string — midnight UTC on the resolved calendar day — or None
    #: when the source offered nothing at all. The document's **primary** date:
    #: everything that ranks, orders or filters reads this one.
    start_value: str | None
    start_precision: Precision
    source: Source
    #: Which rule produced this outcome. One of: ``bundle_date_field``,
    #: ``bundle_date_field_year_only``, ``bundle_created``, ``field_empty``,
    #: ``field_invalid``, ``bundle_unmapped``, ``no_bundle``, ``no_date``,
    #: ``inherited_from_parent``.
    rule: str
    #: The end of the period. **Never manufactured** — absent unless the bundle
    #: declares an end field and that field held a usable date.
    end_value: str | None = None
    #: Precision of :attr:`end_value`. None exactly when there is no end.
    end_precision: Precision | None = None
    bundle: str | None = None
    #: The configured fields, in order, or ``("created",)``. Never empty for a
    #: mapped bundle, so the audit row can name a field even when it held
    #: nothing.
    fields: tuple[str, ...] = ()
    #: What each of those fields actually contained, verbatim, for the audit
    #: trail. Parallel to :attr:`fields`, and kept even when the value was
    #: rejected — a value that could not be used is exactly what a reviewer
    #: needs to see.
    raw_values: tuple[Any, ...] = ()
    #: What :mod:`app.ingestion.source_dates` says the *start* field is —
    #: ``date``, ``range_start``, ``range_end``, ``sort_key``, ``not_a_date``.
    #: Recorded, never acted on: the mapping decides which field to read, this
    #: only reports what that field turned out to be.
    field_role: str = "not_a_date"
    #: What is wrong with the range, when something is. See :data:`RangeIssue`.
    range_issue: RangeIssue | None = None

    @property
    def start_field(self) -> str | None:
        return self.fields[0] if self.fields else None

    @property
    def end_field(self) -> str | None:
        """The configured end field, or None for a single-date bundle.

        Configured, not resolved: a bundle with an end field whose value was
        empty still names it, which is what lets the audit row say *which* field
        was empty.
        """
        return self.fields[1] if len(self.fields) > 1 else None

    @property
    def start_raw(self) -> Any:
        return self.raw_values[0] if self.raw_values else None

    @property
    def end_raw(self) -> Any:
        return self.raw_values[1] if len(self.raw_values) > 1 else None

    @property
    def has_range(self) -> bool:
        """Did this record resolve to a usable period?"""
        return self.end_value is not None

    @property
    def end_missing(self) -> bool:
        """Does the bundle declare an end field that produced no date?

        Distinguishes "this content type has no end" (a research paper) from
        "this one should have had an end and does not" (a completed project) —
        the two look identical from ``end_value`` alone, and only the second is
        worth reporting.
        """
        return self.end_field is not None and self.end_value is None

    @property
    def from_bundle_field(self) -> bool:
        """Did the bundle's configured field actually supply the start date?

        The question the PDF path asks: when the page's date is a value the CMS
        states about that content type, the page is authoritative and there is
        nothing for the document-reading resolver to improve on.
        """
        return self.source == "cms_field" and self.rule.startswith("bundle_date_field")


def fields_for(bundle: str | None) -> tuple[str, ...]:
    """The configured date fields for ``bundle``, in order. Empty if unmapped."""
    return BUNDLE_DATE_FIELDS.get(bundle or "", ())


def end_field_for(bundle: str | None) -> str | None:
    """The configured end field for ``bundle``, or None if it has only one."""
    fields = fields_for(bundle)
    return fields[1] if len(fields) > 1 else None


@dataclass(frozen=True)
class _Point:
    """One resolved endpoint: the value, how precise it is, and what was read."""

    value: date | None
    precision: Precision
    raw: Any
    #: ``ok`` | ``empty`` | ``invalid`` — why there is no value, when there is none.
    status: str


def _read(field: str, metadata: dict[str, Any] | None) -> _Point:
    """Resolve one configured field. The same treatment for start and for end.

    This is what makes a range bundle not a special case: both endpoints get
    identical extraction, IST normalisation, plausibility bounds and the
    bare-year precision downgrade, and the caller only has to compare them.
    """
    declared = precision_of(field)
    raw = (metadata or {}).get(field)
    if raw in (None, "", [], {}):
        return _Point(None, declared, raw, "empty")
    value = to_ist_date(raw)
    if not is_plausible(value):
        return _Point(None, declared, raw, "invalid")
    # A bare year in a field declared to hold full dates. `to_ist_date` reads it
    # as 1 January, which is the right *value* and the wrong *precision*: storing
    # it as a day would claim a day the source never gave, which is the one thing
    # every other guard in this system exists to prevent. So the value stands and
    # the precision is downgraded to what the source actually supports.
    precision: Precision = "year" if declared == "day" and _is_bare_year(raw) else declared
    return _Point(value, precision, raw, "ok")


def resolve_effective_dates(
    bundle: str | None,
    created: str | None,
    metadata: dict[str, Any] | None,
) -> EffectiveDate:
    """**The single decision.** One record's effective date(s), and why.

    Ingestion, the attachment path and the backfill all call this, because two
    copies of a conditional rule drift — a re-ingested document would then get a
    different date than the backfill gave it.

    The fallback ladder is deliberate and exhaustive; see
    ``docs/ingestion/bundle-date-capture-plan.md`` §8 and Revision 2 §4. Every
    rung that is not the happy path falls back to ``created`` for the *start*,
    which is a real date the source states about the record and is what this
    column has always held. The alternative — no date — makes a document
    invisible to every date filter rather than merely mis-ordered, which is
    strictly worse. An **end** date has no such fallback: it is either stated or
    absent.
    """
    fields = fields_for(bundle)

    if not fields:
        if bundle and bundle not in _warned_unmapped:
            _warned_unmapped.add(bundle)
            logger.info(
                "Bundle %r has no configured date field; its records keep their "
                "created stamp. Declare it in "
                "app.ingestion.bundle_dates.BUNDLE_DATE_FIELDS if it should use "
                "one.", bundle,
            )
        return _from_created(bundle, created, (),
                             rule="bundle_unmapped" if bundle else "no_bundle")

    if fields[0] == CREATED:
        return _from_created(bundle, created, fields, rule="bundle_created")

    start = _read(fields[0], metadata)
    end = _read(fields[1], metadata) if len(fields) > 1 else None
    raw_values = tuple(p.raw for p in (start, end) if p is not None)

    if start.status != "ok":
        if start.status == "empty":
            logger.info(
                "Bundle %r states its date in %s, which is empty on this record; "
                "keeping the created stamp.", bundle, fields[0],
            )
            rule = "field_empty"
        else:
            logger.warning(
                "Bundle %r states its date in %s, whose value %r is not a usable "
                "date; keeping the created stamp.", bundle, fields[0], start.raw,
            )
            rule = "field_invalid"
        resolved = _from_created(bundle, created, fields, rule=rule,
                                 raw_values=raw_values)
        if end is not None and end.value is not None:
            # The end is real and the start is not. Preserving it keeps the only
            # thing the source did state, and the flag keeps the record honest
            # about the range being incomplete.
            #
            # But only where the result is a range that reads forwards. The
            # fallback start is the record's *creation* stamp, and on this corpus
            # that is usually 2017 while the project it describes ended in 2000 —
            # storing both would put a backwards range in the column, which
            # `reconcile.date_checks.inverted_date_range` would then correctly
            # report as "something else wrote this". A creation stamp is not a
            # range endpoint, so the honest outcome is to keep the start alone and
            # let the review row carry the end: `raw_values` and the evidence
            # sentence both still name it, so nothing is lost to a reviewer.
            keeps_order = (
                resolved.start_value is None
                or str(resolved.start_value)[:10] <= as_stored_date(end.value)[:10]
            )
            logger.warning(
                "Bundle %r has a usable %s but no usable %s; the range is "
                "incomplete%s.", bundle, fields[1], fields[0],
                "" if keeps_order else " and the end precedes the fallback start, "
                                       "so it is not stored",
            )
            from dataclasses import replace

            resolved = replace(
                resolved,
                end_value=as_stored_date(end.value) if keeps_order else None,
                end_precision=end.precision if keeps_order else None,
                range_issue="end_without_start",
            )
        return resolved

    if start.precision != precision_of(fields[0]):
        logger.info(
            "Bundle %r states its date in %s, which holds only the year %r; "
            "recording it at year precision.", bundle, fields[0], start.raw,
        )

    return _with_range(
        EffectiveDate(
            start_value=as_stored_date(start.value),
            start_precision=start.precision,
            source="cms_field",
            rule=("bundle_date_field_year_only"
                  if start.precision != precision_of(fields[0])
                  else "bundle_date_field"),
            bundle=bundle,
            fields=fields,
            raw_values=raw_values,
            field_role=classify(fields[0]),
        ),
        start=start,
        end=end,
    )


def _with_range(
    resolved: EffectiveDate, *, start: _Point, end: _Point | None
) -> EffectiveDate:
    """Attach the end date to a resolved start, or record why it was not attached.

    The comparison is between **normalised calendar dates**, never raw values.
    Measured on the live corpus: by raw string 13 events and 5 completed projects
    look inverted, and after normalisation only 2 and 2 are — the rest differ
    solely in the time component and land on the same Indian calendar day, or had
    a ``1970-01-01`` end that plausibility already removed. Comparing raw strings
    would have sent eleven correct records to a review queue.

    ``start == end`` is valid, not a defect: 784 of 1,094 events are a one-day
    event whose two fields hold the same day.
    """
    from dataclasses import replace

    if end is None:
        return resolved
    if end.status == "empty":
        # A start without an end is a valid partial range: a project that has a
        # start date and no recorded finish. `end_missing` reports it; it is not
        # an issue a person has to settle.
        return resolved
    if end.status == "invalid":
        logger.warning(
            "Bundle %r has %s = %r, which is not a usable date; the end of the "
            "range is dropped.", resolved.bundle, resolved.end_field, end.raw,
        )
        return replace(resolved, range_issue="end_invalid")
    if start.value is not None and end.value is not None and start.value > end.value:
        # Never swapped. A record whose two dates contradict each other is a CMS
        # defect, and guessing which one is wrong would bury it.
        logger.warning(
            "Bundle %r has %s (%s) after %s (%s); the end of the range is "
            "dropped and the record is flagged for review.",
            resolved.bundle, resolved.start_field, start.value,
            resolved.end_field, end.value,
        )
        return replace(resolved, range_issue="inverted")
    return replace(
        resolved,
        end_value=as_stored_date(end.value),
        end_precision=end.precision,
    )


def _is_bare_year(raw: Any) -> bool:
    """Is this value a four-digit year and nothing more?

    ``True``/``False`` are excluded explicitly: ``bool`` is a subclass of ``int``
    in Python, and ``str(True)`` is not four digits — but the guard is written
    down rather than relied on, because a CMS boolean reaching a date field is
    exactly the kind of bad data this corpus already holds.
    """
    if isinstance(raw, bool):
        return False
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    text = str(raw if not isinstance(raw, float) else int(raw)).strip()
    return len(text) == 4 and text.isdigit()


def _from_created(
    bundle: str | None,
    created: str | None,
    fields: tuple[str, ...],
    *,
    rule: str,
    raw_values: tuple[Any, ...] = (),
) -> EffectiveDate:
    """The fallback rung. ``created`` is passed through **verbatim**.

    Not re-normalised through ``to_ist_date``/``as_stored_date``: a creation
    stamp is a real point in time with a real clock reading, and flattening it to
    midnight would throw away the intra-day ordering that is the only thing
    separating the 646 completed projects sharing one import date. That is also
    exactly what this column held before this module existed, so a bundle mapped
    to ``created`` is bit-for-bit unchanged.

    No end date is ever produced here. A creation stamp is a point, not a period.
    """
    return EffectiveDate(
        start_value=created,
        start_precision="day",
        source="created",
        rule="no_date" if not created else rule,
        bundle=bundle,
        fields=fields,
        raw_values=raw_values,
        # The creation stamp is a real date the source states about the record;
        # `classify` would call it `not_a_date`, which is the right answer for an
        # undeclared *field* and the wrong one for this. Where a configured field
        # exists but gave nothing, the field's own role still applies — it is the
        # field that disappointed, and the audit row should say which.
        field_role=("created_stamp" if not fields or fields[0] == CREATED
                    else classify(fields[0])),
    )


def inherited(parent: EffectiveDate) -> EffectiveDate:
    """``parent``'s dates as an attached file's own.

    **Both endpoints, and both precisions, carry over unchanged.** A PDF hanging
    off a research paper is a year-precision document too, and rendering its
    1 January as a day would invent a January publication just as surely on the
    file as on the page; a PDF hanging off a completed project covers the same
    period the project did. Only the source changes, to say where they came from.

    Every PDF on a page gets this same object, whether the page holds one or
    twelve: the parent's dates are resolved once and propagated, never
    recalculated per file.
    """
    return EffectiveDate(
        start_value=parent.start_value,
        start_precision=parent.start_precision,
        source="parent_page",
        rule="inherited_from_parent",
        end_value=parent.end_value,
        end_precision=parent.end_precision,
        bundle=parent.bundle,
        fields=parent.fields,
        raw_values=parent.raw_values,
        field_role=parent.field_role,
        range_issue=parent.range_issue,
    )


def _range_clause(resolved: EffectiveDate) -> str:
    """The sentence fragment describing the end of the range, if there is one.

    Start and end are named separately and never collapsed into one ambiguous
    string: "why does this run to 2022?" is a different question from "why does
    it start in 2020", and a reviewer has to be able to answer both.
    """
    if resolved.end_field is None:
        return ""
    if resolved.range_issue == "inverted":
        return (f" Its {resolved.end_field} is {resolved.end_raw!r}, which falls "
                f"*before* the start; the end of the range was dropped rather "
                f"than swapped, and this record needs review.")
    if resolved.range_issue == "end_invalid":
        return (f" Its {resolved.end_field} is {resolved.end_raw!r}, which is not "
                f"a usable date; the end of the range was dropped.")
    if resolved.range_issue == "end_without_start":
        return (f" Its {resolved.end_field} is {resolved.end_raw!r}, which is "
                f"usable and has been kept, so the range runs to "
                f"{str(resolved.end_value)[:10]} from a start nobody stated.")
    if resolved.end_value is None:
        return (f" Its {resolved.end_field} is empty, so the period has a start "
                f"and no recorded end.")
    return (f" Its {resolved.end_field} is {resolved.end_raw!r}, so the period "
            f"runs to {str(resolved.end_value)[:10]}.")


def _role_note(role: str) -> str:
    """The clause that says what kind of field this date came out of.

    Only added where it changes how the date should be read. A ``date`` field is
    the CMS stating the content's date outright and needs no gloss; the two range
    roles do, because the value is one end of a period rather than a point.
    """
    return {
        # `created_stamp` and `date` need no gloss: the reason sentence already
        # says where the value came from, and neither reading is surprising.
        "range_start": (" That field opens the period the content covers, so the "
                        "date is when the work or event began."),
        "range_end": (" That field closes the period the content covers, so the "
                      "date is when the work or event ended."),
        "sort_key": (" That field is a listing sort key and says nothing about "
                     "the content; no bundle should be mapped to it."),
        "not_a_date": (" That field is not a date field, so nothing should be "
                       "dated by it."),
    }.get(role, "")


def describe(
    resolved: EffectiveDate,
    *,
    title: str | None = None,
    url: str | None = None,
    for_attachment: bool = False,
) -> str:
    """A sentence explaining these dates, for the decision table's evidence column.

    The point of the whole provenance chain: asked "why does this PDF have the
    date 2022?", the stored row has to answer "because it is attached to the
    research_papers page 'X', whose field_rpaper_year is 2022" — without anyone
    re-running ingestion to find out. Where the bundle carries a range, the start
    field and the end field are named and valued **separately**.

    ``for_attachment`` expects the **parent page's** resolution, not the
    attachment's own: the sentence describes where the dates came from, and the
    inherited copy has by construction had that answer replaced with "from its
    parent". ``title`` and ``url`` are the parent's.
    """
    where = f" ({url})" if url else ""
    page = f"the {resolved.bundle or 'unknown'} page {title!r}{where}" if title \
        else f"its {resolved.bundle or 'unknown'} page{where}"
    lead = f"Inherited from {page}, whose " if for_attachment else "The "

    if resolved.from_bundle_field:
        body = (f"{resolved.bundle} bundle states its date in "
                f"{resolved.start_field} ({resolved.start_raw!r})")
        if for_attachment:
            body = f"{resolved.start_field} is {resolved.start_raw!r}"
        note = _role_note(resolved.field_role)
        return (f"{lead}{body}; the effective date is {str(resolved.start_value)[:10]}."
                f"{_range_clause(resolved)}{note}")

    reasons = {
        "bundle_created": (f"{resolved.bundle} bundle takes its date from the "
                           f"record's created stamp"),
        "field_empty": (f"{resolved.bundle} bundle states its date in "
                        f"{resolved.start_field}, which is empty on this record, "
                        f"so the created stamp stands"),
        "field_invalid": (f"{resolved.bundle} bundle states its date in "
                          f"{resolved.start_field}, whose value "
                          f"{resolved.start_raw!r} is not a usable date, so the "
                          f"created stamp stands"),
        "bundle_unmapped": (f"{resolved.bundle} bundle has no configured date "
                            f"field, so the created stamp stands"),
        "no_bundle": "record has no bundle, so the created stamp stands",
        "no_date": (f"{resolved.bundle} bundle offered no date and the record "
                    f"has no created stamp, so it is undated"),
    }
    body = reasons.get(resolved.rule, f"date was resolved by {resolved.rule}")
    tail = _range_clause(resolved)
    if for_attachment:
        return f"Inherited from {page}, where {body[0].lower()}{body[1:]}.{tail}"
    return f"The {body}.{tail}"
