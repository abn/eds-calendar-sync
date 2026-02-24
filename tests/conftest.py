"""
Shared pytest fixtures and iCal helpers.
"""

import logging

import pytest

from eds_calendar_sync.db import StateDatabase
from eds_calendar_sync.models import SyncConfig
from eds_calendar_sync.models import SyncStats

WORK_CAL_ID = "work-calendar-test"
PERSONAL_CAL_ID = "personal-calendar-test"


def make_vevent(uid: str, summary: str = "Test Event") -> str:
    """Return a minimal, valid VEVENT iCal string (no VCALENDAR wrapper)."""
    return (
        f"BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"DTSTART:20260301T100000Z\r\n"
        f"DTEND:20260301T110000Z\r\n"
        f"DTSTAMP:20260224T000000Z\r\n"
        f"END:VEVENT\r\n"
    )


def make_cancelled_vevent(uid: str) -> str:
    """Return a VEVENT with STATUS:CANCELLED."""
    return (
        f"BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:Cancelled Event\r\n"
        f"DTSTART:20260301T100000Z\r\n"
        f"DTEND:20260301T110000Z\r\n"
        f"DTSTAMP:20260224T000000Z\r\n"
        f"STATUS:CANCELLED\r\n"
        f"END:VEVENT\r\n"
    )


def make_transparent_vevent(uid: str) -> str:
    """Return a VEVENT with TRANSP:TRANSPARENT (free/show-as-available)."""
    return (
        f"BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:Free Time Event\r\n"
        f"DTSTART:20260301T100000Z\r\n"
        f"DTEND:20260301T110000Z\r\n"
        f"DTSTAMP:20260224T000000Z\r\n"
        f"TRANSP:TRANSPARENT\r\n"
        f"END:VEVENT\r\n"
    )


def make_managed_vevent(uid: str) -> str:
    """Return a VEVENT with CATEGORIES:CALENDAR-SYNC-MANAGED (created by our sync tool)."""
    return (
        f"BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:Managed Event\r\n"
        f"DTSTART:20260301T100000Z\r\n"
        f"DTEND:20260301T110000Z\r\n"
        f"DTSTAMP:20260224T000000Z\r\n"
        f"CATEGORIES:CALENDAR-SYNC-MANAGED\r\n"
        f"END:VEVENT\r\n"
    )


def make_recurring_vevent(uid: str, count: int = 3, exdates: tuple = ()) -> str:
    """Return a VEVENT with RRULE;FREQ=DAILY;COUNT=<count> and optional EXDATE lines.

    ``exdates`` should be an iterable of YYYYMMDD strings.  Each is written
    as an EXDATE;VALUE=DATE line so the fallback string-parsing path in
    has_valid_occurrences() is exercised alongside the normal get_exdate() path.
    """
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        "SUMMARY:Recurring Event",
        "DTSTART:20260301T100000Z",
        "DTEND:20260301T110000Z",
        "DTSTAMP:20260224T000000Z",
        f"RRULE:FREQ=DAILY;COUNT={count}",
    ]
    for d in exdates:
        lines.append(f"EXDATE;VALUE=DATE:{d}")
    lines.append("END:VEVENT")
    return "\r\n".join(lines) + "\r\n"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test_state.db"


@pytest.fixture
def state_db(db_path):
    with StateDatabase(db_path, WORK_CAL_ID, PERSONAL_CAL_ID) as db:
        yield db


@pytest.fixture
def sync_config(db_path):
    return SyncConfig(
        work_calendar_id=WORK_CAL_ID,
        personal_calendar_id=PERSONAL_CAL_ID,
        state_db_path=db_path,
        dry_run=False,
        verbose=False,
    )


@pytest.fixture
def sync_logger():
    return logging.getLogger("test_sync")


@pytest.fixture
def sync_stats():
    return SyncStats()
