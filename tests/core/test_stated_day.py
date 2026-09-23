"""Dates as external sources state them, read to a calendar day or not at all.

Web retrieval reads dates written by other people's software. An unreadable one
must come back as None — an unknown date is honest, a guessed one is not.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.core.dates import stated_day


@pytest.mark.parametrize("value, expected", [
    ("2025-04-09", "2025-04-09"),
    ("2025-04-10T09:00:00+05:30", "2025-04-10"),
    ("2026-06-29T00:00:00Z", "2026-06-29"),
    ("Mon, 29 Jun 2026 09:00:00 GMT", "2026-06-29"),
    ("D:20180820120000+05'30'", "2018-08-20"),
    ("D:201808", "2018-08-01"),
    ("D:2018", "2018-01-01"),
    (datetime(2019, 6, 13, 10, 0), "2019-06-13"),
    # Written out on the page, as TERI's Drupal theme shows it.
    ("09 Apr 2025", "2025-04-09"),
    ("9th April 2025", "2025-04-09"),
    ("April 9, 2025", "2025-04-09"),
    ("Sept. 3, 2024", "2024-09-03"),
    ("Posted on 29 June 2026", "2026-06-29"),
])
def test_the_forms_sources_use_are_read_to_the_day(value, expected):
    assert stated_day(value) == expected


@pytest.mark.parametrize("value", [None, "", "   ", "last Tuesday", "2025", "D:20181340",
                                   12345, "not a date at all",
                                   "April 2025",          # a period, not a day
                                   "31 Feb 2025",         # no such day
                                   "12 Smarch 2025"])     # no such month
def test_anything_unreadable_is_none(value):
    assert stated_day(value) is None
