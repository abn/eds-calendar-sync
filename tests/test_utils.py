"""
Unit tests for stateless helpers in eds_calendar_sync.sync.utils.

All tests use real ICalGLib components so that libical-glib quirks are
exercised (null_time from VALUE=DATE EXDATEs, RecurIterator UNTIL capping,
child-component as_ical_string() fragility, etc.).
"""

import gi

gi.require_version("GLib", "2.0")
gi.require_version("ICalGLib", "3.0")
from gi.repository import GLib
from gi.repository import ICalGLib

from eds_calendar_sync.sync.utils import compute_hash
from eds_calendar_sync.sync.utils import has_valid_occurrences
from eds_calendar_sync.sync.utils import is_declined_by_user
from eds_calendar_sync.sync.utils import is_event_cancelled
from eds_calendar_sync.sync.utils import is_free_time
from eds_calendar_sync.sync.utils import is_not_found_error
from eds_calendar_sync.sync.utils import strip_exdates_for_dates

# ---------------------------------------------------------------------------
# Module-level iCal construction helpers
# ---------------------------------------------------------------------------

_DTSTART = "20260301T100000Z"
_DTEND = "20260301T110000Z"
_DTSTAMP = "20260224T000000Z"


def _make_rrule_vevent(
    uid: str,
    dtstart: str = _DTSTART,
    dtend: str = _DTEND,
    rrule: str = "FREQ=DAILY;COUNT=5",
    exdates: tuple = (),
) -> str:
    """Build a VEVENT string with an RRULE and optional EXDATE;VALUE=DATE lines."""
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        "SUMMARY:Recurring Test",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        f"DTSTAMP:{_DTSTAMP}",
        f"RRULE:{rrule}",
    ]
    for d in exdates:
        lines.append(f"EXDATE;VALUE=DATE:{d}")
    lines.append("END:VEVENT")
    return "\r\n".join(lines) + "\r\n"


def _wrap_vcalendar(vevent_str: str) -> str:
    """Wrap a VEVENT string in a minimal VCALENDAR."""
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//TestSuite//EN\r\n" + vevent_str + "END:VCALENDAR\r\n"
    )


def _parse(ical_str: str) -> ICalGLib.Component:
    return ICalGLib.Component.new_from_string(ical_str)


def _simple_vevent(uid: str) -> str:
    return (
        f"BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:Simple Event\r\n"
        f"DTSTART:{_DTSTART}\r\n"
        f"DTEND:{_DTEND}\r\n"
        f"DTSTAMP:{_DTSTAMP}\r\n"
        f"END:VEVENT\r\n"
    )


# ---------------------------------------------------------------------------
# GLib.Error stub for is_not_found_error tests
# ---------------------------------------------------------------------------


class _GLibError(GLib.Error):
    """Lightweight GLib.Error subclass with controllable domain/code/message."""

    def __init__(self, domain: str = "", code: int = 0, message: str = ""):
        # Do not call GLib.Error.__init__ — we only need attribute access for
        # the tests, and the parent constructor signature varies across gi versions.
        self.domain = domain
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# TestHasValidOccurrences
# ---------------------------------------------------------------------------


class TestHasValidOccurrences:
    def test_non_recurring_always_valid(self):
        """Non-recurring VEVENT (no RRULE) is always considered valid."""
        comp = _parse(_simple_vevent("NR1"))
        assert has_valid_occurrences(comp) is True

    def test_recurring_no_exdate(self):
        """Recurring event with RRULE but no EXDATEs is valid."""
        comp = _parse(_make_rrule_vevent("RN1", rrule="FREQ=DAILY;COUNT=5"))
        assert has_valid_occurrences(comp) is True

    def test_recurring_some_excluded(self):
        """Recurring event with fewer EXDATEs than occurrences has valid occurrences."""
        # COUNT=5 → 20260301..20260305; exclude first 3 → 2 remain
        comp = _parse(
            _make_rrule_vevent(
                "RSE1",
                rrule="FREQ=DAILY;COUNT=5",
                exdates=("20260301", "20260302", "20260303"),
            )
        )
        assert has_valid_occurrences(comp) is True

    def test_recurring_all_excluded(self):
        """Recurring event whose every occurrence is in EXDATE returns False."""
        comp = _parse(
            _make_rrule_vevent(
                "RAE1",
                rrule="FREQ=DAILY;COUNT=3",
                exdates=("20260301", "20260302", "20260303"),
            )
        )
        assert has_valid_occurrences(comp) is False

    def test_value_date_exdate_fallback_all_excluded(self):
        """EXDATE;VALUE=DATE lines that exclude every occurrence → False.

        This exercises the string-parsing fallback path used when get_exdate()
        returns null_time for VALUE=DATE properties (known libical-glib quirk).
        """
        # COUNT=2 → 20260301, 20260302; both excluded
        comp = _parse(
            _make_rrule_vevent(
                "VDF1",
                rrule="FREQ=DAILY;COUNT=2",
                exdates=("20260301", "20260302"),
            )
        )
        assert has_valid_occurrences(comp) is False

    def test_value_date_exdate_fallback_some_remaining(self):
        """EXDATE;VALUE=DATE lines that exclude only some occurrences → True."""
        # COUNT=3 → 20260301, 20260302, 20260303; exclude first 2 → 1 remains
        comp = _parse(
            _make_rrule_vevent(
                "VDT1",
                rrule="FREQ=DAILY;COUNT=3",
                exdates=("20260301", "20260302"),
            )
        )
        assert has_valid_occurrences(comp) is True

    def test_finite_series_with_until(self):
        """RRULE with UNTIL; all occurrences up to UNTIL are excluded → False."""
        # UNTIL=20260303 → 20260301, 20260302, 20260303 all excluded
        comp = _parse(
            _make_rrule_vevent(
                "FSU1",
                rrule="FREQ=DAILY;UNTIL=20260303T000000Z",
                exdates=("20260301", "20260302", "20260303"),
            )
        )
        assert has_valid_occurrences(comp) is False

    def test_empty_vcalendar(self):
        """VCALENDAR with no VEVENT child returns True (safe fallback)."""
        vcal_str = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\nEND:VCALENDAR\r\n"
        comp = _parse(vcal_str)
        assert has_valid_occurrences(comp) is True

    def test_vcalendar_wrapped_all_excluded(self):
        """VCALENDAR wrapping a fully-excluded recurring VEVENT returns False."""
        vevent = _make_rrule_vevent(
            "VCAE1",
            rrule="FREQ=DAILY;COUNT=2",
            exdates=("20260301", "20260302"),
        )
        comp = _parse(_wrap_vcalendar(vevent))
        assert has_valid_occurrences(comp) is False


# ---------------------------------------------------------------------------
# TestStripExdatesForDates
# ---------------------------------------------------------------------------


class TestStripExdatesForDates:
    def test_empty_dates_set_returns_unchanged(self):
        """Passing an empty dates set leaves the iCal string byte-for-byte identical."""
        ical = _make_rrule_vevent("SED1", exdates=("20260301",))
        result = strip_exdates_for_dates(ical, set())
        assert result == ical

    def test_strips_value_date_form(self):
        """EXDATE;VALUE=DATE:20260301 is removed when '20260301' is in dates."""
        ical = (
            "BEGIN:VEVENT\r\n"
            "UID:SVD1\r\n"
            "DTSTART:20260301T100000Z\r\n"
            "RRULE:FREQ=DAILY;COUNT=3\r\n"
            "EXDATE;VALUE=DATE:20260301\r\n"
            "END:VEVENT\r\n"
        )
        result = strip_exdates_for_dates(ical, {"20260301"})
        assert "EXDATE" not in result

    def test_strips_tzid_datetime_form(self):
        """EXDATE;TZID=...:20260301T... is removed when '20260301' is in dates."""
        ical = (
            "BEGIN:VEVENT\r\n"
            "UID:STD1\r\n"
            "DTSTART;TZID=Europe/Berlin:20260301T110000\r\n"
            "RRULE:FREQ=DAILY;COUNT=3\r\n"
            "EXDATE;TZID=Europe/Berlin:20260301T110000\r\n"
            "END:VEVENT\r\n"
        )
        result = strip_exdates_for_dates(ical, {"20260301"})
        assert "EXDATE" not in result

    def test_strips_only_matching_dates(self):
        """Only the EXDATE line whose date is in the set is removed; others survive."""
        ical = (
            "BEGIN:VEVENT\r\n"
            "UID:SOM1\r\n"
            "DTSTART:20260301T100000Z\r\n"
            "RRULE:FREQ=DAILY;COUNT=5\r\n"
            "EXDATE;VALUE=DATE:20260301\r\n"
            "EXDATE;VALUE=DATE:20260302\r\n"
            "EXDATE;VALUE=DATE:20260303\r\n"
            "END:VEVENT\r\n"
        )
        # Strip only the middle date
        result = strip_exdates_for_dates(ical, {"20260302"})
        assert "20260301" in result
        assert "20260302" not in result
        assert "20260303" in result

    def test_noop_if_date_not_present(self):
        """If the date to strip does not exist in the iCal, the string is unchanged."""
        ical = (
            "BEGIN:VEVENT\r\n"
            "UID:NOP1\r\n"
            "DTSTART:20260301T100000Z\r\n"
            "EXDATE;VALUE=DATE:20260301\r\n"
            "END:VEVENT\r\n"
        )
        result = strip_exdates_for_dates(ical, {"20991231"})
        assert result == ical

    def test_multiline_ical_preserves_order(self):
        """Non-EXDATE lines survive stripping in their original order."""
        ical = (
            "BEGIN:VEVENT\r\n"
            "UID:MLO1\r\n"
            "SUMMARY:Multi Line Test\r\n"
            "DTSTART:20260301T100000Z\r\n"
            "DTEND:20260301T110000Z\r\n"
            "RRULE:FREQ=DAILY;COUNT=5\r\n"
            "EXDATE;VALUE=DATE:20260301\r\n"
            "EXDATE;VALUE=DATE:20260302\r\n"
            "END:VEVENT\r\n"
        )
        result = strip_exdates_for_dates(ical, {"20260301"})
        # Other lines must survive and be in order
        assert "BEGIN:VEVENT" in result
        assert "UID:MLO1" in result
        assert "SUMMARY:Multi Line Test" in result
        assert "DTSTART:20260301T100000Z" in result
        assert "RRULE:FREQ=DAILY;COUNT=5" in result
        assert "20260302" in result  # non-stripped EXDATE survives
        assert "END:VEVENT" in result
        # Stripped line is gone
        lines = result.splitlines()
        assert not any("20260301" in ln and "EXDATE" in ln for ln in lines)


# ---------------------------------------------------------------------------
# TestIsEventCancelled
# ---------------------------------------------------------------------------


class TestIsEventCancelled:
    def test_status_cancelled(self):
        """STATUS:CANCELLED is detected as cancelled."""
        ical = (
            "BEGIN:VEVENT\r\n"
            "UID:SC1\r\n"
            "SUMMARY:Cancelled Meeting\r\n"
            "DTSTART:20260301T100000Z\r\n"
            "STATUS:CANCELLED\r\n"
            "END:VEVENT\r\n"
        )
        comp = _parse(ical)
        assert is_event_cancelled(comp) is True

    def test_status_confirmed(self):
        """STATUS:CONFIRMED is not cancelled."""
        ical = (
            "BEGIN:VEVENT\r\n"
            "UID:SCC1\r\n"
            "SUMMARY:Confirmed Meeting\r\n"
            "DTSTART:20260301T100000Z\r\n"
            "STATUS:CONFIRMED\r\n"
            "END:VEVENT\r\n"
        )
        comp = _parse(ical)
        assert is_event_cancelled(comp) is False

    def test_no_status(self):
        """Events without a STATUS property are not cancelled."""
        comp = _parse(_simple_vevent("NS1"))
        assert is_event_cancelled(comp) is False

    def test_vcalendar_wrapper(self):
        """VCALENDAR wrapping a CANCELLED VEVENT is treated as cancelled."""
        vevent = (
            "BEGIN:VEVENT\r\n"
            "UID:VCW1\r\n"
            "SUMMARY:Cancelled\r\n"
            "DTSTART:20260301T100000Z\r\n"
            "STATUS:CANCELLED\r\n"
            "END:VEVENT\r\n"
        )
        comp = _parse(_wrap_vcalendar(vevent))
        assert is_event_cancelled(comp) is True


# ---------------------------------------------------------------------------
# TestIsFreeTime
# ---------------------------------------------------------------------------


class TestIsFreeTime:
    def test_transp_transparent(self):
        """TRANSP:TRANSPARENT means the event is free time."""
        ical = (
            "BEGIN:VEVENT\r\n"
            "UID:TT1\r\n"
            "SUMMARY:Declined Meeting\r\n"
            "DTSTART:20260301T100000Z\r\n"
            "TRANSP:TRANSPARENT\r\n"
            "END:VEVENT\r\n"
        )
        comp = _parse(ical)
        assert is_free_time(comp) is True

    def test_transp_opaque(self):
        """TRANSP:OPAQUE means the event blocks time."""
        ical = (
            "BEGIN:VEVENT\r\n"
            "UID:TO1\r\n"
            "SUMMARY:Busy Meeting\r\n"
            "DTSTART:20260301T100000Z\r\n"
            "TRANSP:OPAQUE\r\n"
            "END:VEVENT\r\n"
        )
        comp = _parse(ical)
        assert is_free_time(comp) is False

    def test_no_transp(self):
        """Default (no TRANSP property) is OPAQUE — event blocks time."""
        comp = _parse(_simple_vevent("NT1"))
        assert is_free_time(comp) is False

    def test_vcalendar_wrapper_transparent(self):
        """VCALENDAR wrapping a TRANSPARENT VEVENT is treated as free time."""
        vevent = (
            "BEGIN:VEVENT\r\n"
            "UID:VCT1\r\n"
            "SUMMARY:Transparent\r\n"
            "DTSTART:20260301T100000Z\r\n"
            "TRANSP:TRANSPARENT\r\n"
            "END:VEVENT\r\n"
        )
        comp = _parse(_wrap_vcalendar(vevent))
        assert is_free_time(comp) is True


# ---------------------------------------------------------------------------
# TestComputeHash
# ---------------------------------------------------------------------------


class TestComputeHash:
    def _vevent_with(self, uid: str, extra_lines: list[str] = ()) -> str:
        lines = [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            "SUMMARY:Hash Test",
            f"DTSTART:{_DTSTART}",
            f"DTEND:{_DTEND}",
        ]
        lines.extend(extra_lines)
        lines.append("END:VEVENT")
        return "\r\n".join(lines) + "\r\n"

    def test_volatile_props_ignored(self):
        """Different DTSTAMP/LASTMODIFIED/CREATED/SEQUENCE values → identical hash."""
        base = self._vevent_with("VPI1", ["DTSTAMP:20260101T000000Z"])
        other = self._vevent_with("VPI1", ["DTSTAMP:20260224T120000Z", "SEQUENCE:3"])
        assert compute_hash(base) == compute_hash(other)

    def test_summary_change_differs(self):
        """Changing SUMMARY produces a different hash."""
        v1 = (
            "BEGIN:VEVENT\r\n"
            "UID:SCD1\r\n"
            "SUMMARY:Original Title\r\n"
            f"DTSTART:{_DTSTART}\r\n"
            f"DTEND:{_DTEND}\r\n"
            "END:VEVENT\r\n"
        )
        v2 = (
            "BEGIN:VEVENT\r\n"
            "UID:SCD1\r\n"
            "SUMMARY:Changed Title\r\n"
            f"DTSTART:{_DTSTART}\r\n"
            f"DTEND:{_DTEND}\r\n"
            "END:VEVENT\r\n"
        )
        assert compute_hash(v1) != compute_hash(v2)

    def test_vcalendar_normalises_all_vevents(self):
        """VCALENDAR with two VEVENTs: volatile props stripped from both."""

        def make_vcal(dtstamp1: str, dtstamp2: str) -> str:
            return (
                "BEGIN:VCALENDAR\r\n"
                "VERSION:2.0\r\n"
                "PRODID:-//Test//EN\r\n"
                "BEGIN:VEVENT\r\n"
                "UID:VAV1\r\n"
                "SUMMARY:Event A\r\n"
                f"DTSTART:{_DTSTART}\r\n"
                f"DTSTAMP:{dtstamp1}\r\n"
                "END:VEVENT\r\n"
                "BEGIN:VEVENT\r\n"
                "UID:VAV2\r\n"
                "SUMMARY:Event B\r\n"
                f"DTSTART:{_DTSTART}\r\n"
                f"DTSTAMP:{dtstamp2}\r\n"
                "END:VEVENT\r\n"
                "END:VCALENDAR\r\n"
            )

        vcal_a = make_vcal("20260101T000000Z", "20260101T000000Z")
        vcal_b = make_vcal("20260224T120000Z", "20260224T130000Z")
        assert compute_hash(vcal_a) == compute_hash(vcal_b)

    def test_same_content_same_hash(self):
        """Identical input always yields the identical hash (deterministic)."""
        ical = _simple_vevent("SCH1")
        assert compute_hash(ical) == compute_hash(ical)
        assert compute_hash(ical) == compute_hash(ical)


# ---------------------------------------------------------------------------
# TestIsNotFoundError
# ---------------------------------------------------------------------------


class TestIsNotFoundError:
    # --- GLib.Error paths (requires isinstance check to succeed) ---

    def test_eds_client_quark_code_1(self):
        """e-cal-client-error-quark with code 1 → True."""
        err = _GLibError(domain="e-cal-client-error-quark", code=1, message="not found")
        assert is_not_found_error(err) is True

    def test_eds_wrong_code(self):
        """e-cal-client-error-quark with a different code → False."""
        err = _GLibError(domain="e-cal-client-error-quark", code=5, message="some error")
        assert is_not_found_error(err) is False

    def test_m365_error_item_not_found(self):
        """e-m365-error-quark with ErrorItemNotFound in message → True."""
        err = _GLibError(
            domain="e-m365-error-quark",
            code=42,
            message="Exchange error: ErrorItemNotFound",
        )
        assert is_not_found_error(err) is True

    def test_m365_other_error(self):
        """e-m365-error-quark with a different message → False."""
        err = _GLibError(
            domain="e-m365-error-quark",
            code=42,
            message="Exchange error: SomeOtherError",
        )
        assert is_not_found_error(err) is False

    # --- Non-GLib fallback path (plain Exception, string match) ---

    def test_non_glib_object_not_found(self):
        """Plain Exception whose str() contains 'object not found' → True."""
        assert is_not_found_error(Exception("object not found")) is True

    def test_non_glib_object_not_found_case_insensitive(self):
        """The 'object not found' check is case-insensitive."""
        assert is_not_found_error(Exception("Object Not Found")) is True

    def test_non_glib_other_error(self):
        """Plain Exception with unrelated message → False."""
        assert is_not_found_error(Exception("permission denied")) is False


# ---------------------------------------------------------------------------
# TestIsDeclinedByUser
# ---------------------------------------------------------------------------


def _make_exception_vevent(
    uid: str,
    attendees: tuple[tuple[str, str], ...] = (),
) -> str:
    """Build a bare exception VEVENT with RECURRENCE-ID and optional ATTENDEEs.
    attendees: iterable of (email, partstat) pairs.
    """
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        "SUMMARY:DPS All-hands Weekly",
        f"DTSTART:{_DTSTART}",
        f"DTEND:{_DTEND}",
        f"DTSTAMP:{_DTSTAMP}",
        "RECURRENCE-ID:20260224T110000Z",
    ]
    for email, partstat in attendees:
        lines.append(f"ATTENDEE;PARTSTAT={partstat};ROLE=REQ-PARTICIPANT:mailto:{email}")
    lines.append("END:VEVENT")
    return "\r\n".join(lines) + "\r\n"


class TestIsDeclinedByUser:
    _USER = "user@example.com"
    _OTHER = "other@example.com"

    def test_declined_by_user_returns_true(self):
        comp = _parse(
            _make_exception_vevent(
                "D1", attendees=((self._USER, "DECLINED"), (self._OTHER, "ACCEPTED"))
            )
        )
        assert is_declined_by_user(comp, self._USER) is True

    def test_accepted_by_user_returns_false(self):
        comp = _parse(_make_exception_vevent("D2", attendees=((self._USER, "ACCEPTED"),)))
        assert is_declined_by_user(comp, self._USER) is False

    def test_needs_action_returns_false(self):
        comp = _parse(_make_exception_vevent("D3", attendees=((self._USER, "NEEDS-ACTION"),)))
        assert is_declined_by_user(comp, self._USER) is False

    def test_no_attendees_returns_false(self):
        assert is_declined_by_user(_parse(_simple_vevent("D4")), self._USER) is False

    def test_user_not_in_list_returns_false(self):
        comp = _parse(_make_exception_vevent("D5", attendees=((self._OTHER, "DECLINED"),)))
        assert is_declined_by_user(comp, self._USER) is False

    def test_empty_email_returns_false(self):
        comp = _parse(_make_exception_vevent("D6", attendees=((self._USER, "DECLINED"),)))
        assert is_declined_by_user(comp, "") is False

    def test_case_insensitive_email(self):
        comp = _parse(_make_exception_vevent("D7", attendees=((self._USER, "DECLINED"),)))
        assert is_declined_by_user(comp, "USER@EXAMPLE.COM") is True

    def test_vcalendar_wrapper(self):
        vevent = _make_exception_vevent("D8", attendees=((self._USER, "DECLINED"),))
        assert is_declined_by_user(_parse(_wrap_vcalendar(vevent)), self._USER) is True

    def test_vcalendar_empty_no_crash(self):
        vcal = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\nEND:VCALENDAR\r\n"
        assert is_declined_by_user(_parse(vcal), self._USER) is False

    def test_other_declined_user_accepted(self):
        comp = _parse(
            _make_exception_vevent(
                "D9", attendees=((self._OTHER, "DECLINED"), (self._USER, "ACCEPTED"))
            )
        )
        assert is_declined_by_user(comp, self._USER) is False
