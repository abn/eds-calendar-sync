"""Integration tests for time-only rescheduled exception detection.

Tests that an exception VEVENT with RECURRENCE-ID date == DTSTART date but
different time (same-day time-shift) is treated as a rescheduled standalone
event rather than a non-rescheduled phantom-EXDATE occurrence.

Three scenarios:
  1. Same date AND same time  → non-rescheduled (phantom EXDATE stripped, 1 personal event)
  2. Same date, different time → rescheduled (standalone exception, 2 personal events)
  3. Different date             → rescheduled (standalone exception, 2 personal events)
"""

from eds_calendar_sync.models import SyncStats
from eds_calendar_sync.sync.two_way import run_two_way
from tests.fake_client import FakeCalendarClient

# Original occurrence slot: 2026-03-03 10:30 UTC
_ORIGINAL_TIME = "20260303T103000Z"
_ORIGINAL_END = "20260303T113000Z"

# Same-day time-shift: 2026-03-03 11:30 UTC
_SHIFTED_TIME = "20260303T113000Z"
_SHIFTED_END = "20260303T123000Z"

# Different-week reschedule: 2026-03-10 10:30 UTC
_DIFFERENT_DATE_TIME = "20260310T103000Z"
_DIFFERENT_DATE_END = "20260310T113000Z"


def _make_master_vevent(uid: str) -> str:
    """Weekly recurring meeting with the first occurrence EXDATE'd (Exchange pattern)."""
    return (
        f"BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:Weekly 1:1\r\n"
        f"DTSTART:{_ORIGINAL_TIME}\r\n"
        f"DTEND:{_ORIGINAL_END}\r\n"
        f"DTSTAMP:20260224T000000Z\r\n"
        f"RRULE:FREQ=WEEKLY;COUNT=4\r\n"
        f"EXDATE:{_ORIGINAL_TIME}\r\n"
        f"END:VEVENT\r\n"
    )


def _make_exception_vevent(uid: str, rid: str, dtstart: str, dtend: str) -> str:
    """Exception VEVENT with the given RECURRENCE-ID and DTSTART."""
    return (
        f"BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:Weekly 1:1\r\n"
        f"RECURRENCE-ID:{rid}\r\n"
        f"DTSTART:{dtstart}\r\n"
        f"DTEND:{dtend}\r\n"
        f"DTSTAMP:20260224T000000Z\r\n"
        f"END:VEVENT\r\n"
    )


def _run(config, logger, work_client, personal_client, state_db) -> SyncStats:
    stats = SyncStats()
    run_two_way(config, stats, logger, work_client, personal_client, state_db)
    return stats


class TestExceptionRescheduleDetection:
    """Verify rescheduled-exception detection covers date AND time components."""

    def test_same_date_same_time_is_not_rescheduled(self, state_db, sync_config, sync_logger):
        """RECURRENCE-ID date and time == DTSTART → non-rescheduled.

        The phantom EXDATE in the master is stripped so GNOME Calendar shows the
        normal recurring occurrence.  Only the master is synced to personal.
        """
        uid = "EXC_SAME_TIME"
        work_client = FakeCalendarClient(
            {
                uid: _make_master_vevent(uid),
                uid + "_exc": _make_exception_vevent(
                    uid, rid=_ORIGINAL_TIME, dtstart=_ORIGINAL_TIME, dtend=_ORIGINAL_END
                ),
            }
        )
        personal_client = FakeCalendarClient({})

        _run(sync_config, sync_logger, work_client, personal_client, state_db)

        assert len(personal_client.creates) == 1, (
            "Non-rescheduled exception (same date+time) must produce exactly 1 personal event "
            "(master with phantom EXDATE stripped)"
        )

    def test_same_date_different_time_is_rescheduled(self, state_db, sync_config, sync_logger):
        """RECURRENCE-ID date == DTSTART date but time differs → rescheduled.

        Both the master (EXDATE intact, suppressing the original slot) and the
        rescheduled exception (as a standalone event at the new time) must be
        synced to personal.
        """
        uid = "EXC_TIME_SHIFT"
        work_client = FakeCalendarClient(
            {
                uid: _make_master_vevent(uid),
                uid + "_exc": _make_exception_vevent(
                    uid, rid=_ORIGINAL_TIME, dtstart=_SHIFTED_TIME, dtend=_SHIFTED_END
                ),
            }
        )
        personal_client = FakeCalendarClient({})

        _run(sync_config, sync_logger, work_client, personal_client, state_db)

        assert len(personal_client.creates) == 2, (
            "Time-shifted exception on same date must produce 2 personal events "
            "(master + standalone rescheduled exception)"
        )

    def test_different_date_is_rescheduled(self, state_db, sync_config, sync_logger):
        """RECURRENCE-ID date != DTSTART date → rescheduled (original behaviour).

        Both the master (EXDATE intact) and the rescheduled exception (standalone)
        must be synced to personal.
        """
        uid = "EXC_DIFF_DATE"
        work_client = FakeCalendarClient(
            {
                uid: _make_master_vevent(uid),
                uid + "_exc": _make_exception_vevent(
                    uid,
                    rid=_ORIGINAL_TIME,
                    dtstart=_DIFFERENT_DATE_TIME,
                    dtend=_DIFFERENT_DATE_END,
                ),
            }
        )
        personal_client = FakeCalendarClient({})

        _run(sync_config, sync_logger, work_client, personal_client, state_db)

        assert len(personal_client.creates) == 2, (
            "Different-date rescheduled exception must produce 2 personal events "
            "(master + standalone rescheduled exception)"
        )
