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
