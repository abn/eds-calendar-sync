"""
Unit tests for EventSanitizer in eds_calendar_sync.sanitizer.

Each test exercises one coherent behaviour of sanitize() / is_managed_event()
in isolation, using bare VEVENT strings (no VCALENDAR wrapper) to keep
construction simple.  The returned component is inspected via ICalGLib APIs.
"""

import uuid

import gi

gi.require_version("ICalGLib", "3.0")
from gi.repository import ICalGLib

from eds_calendar_sync.sanitizer import EventSanitizer
from eds_calendar_sync.sync.utils import compute_source_fingerprint

# ---------------------------------------------------------------------------
# iCal helpers
# ---------------------------------------------------------------------------

_DTSTART = "20260301T100000Z"
_DTEND = "20260301T110000Z"
_DTSTAMP = "20260224T000000Z"

# Minimal VALARM sub-component strings (no trailing \r\n — _make_vevent adds separators).
_VALARM_DISPLAY = (
    "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT15M\r\nDESCRIPTION:Reminder\r\nEND:VALARM"
)
_VALARM_EMAIL = (
    "BEGIN:VALARM\r\n"
    "ACTION:EMAIL\r\n"
    "TRIGGER:-PT30M\r\n"
    "SUMMARY:Alarm\r\n"
    "DESCRIPTION:Meeting reminder\r\n"
    "END:VALARM"
)


def _make_vevent(
    uid: str,
    extra_lines: list[str] = (),
    summary: str = "Test Event",
    subcomponents: list[str] = (),
) -> str:
    """Return a minimal VEVENT string with optional extra property lines and sub-components.

    subcomponents: list of raw iCal sub-component strings (e.g. VALARM blocks) to
    embed before END:VEVENT.  Each entry should include its own BEGIN:/END: lines
    and use \\r\\n line endings internally, but must NOT end with a trailing \\r\\n
    (the separator is added here).
    """
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SUMMARY:{summary}",
        f"DTSTART:{_DTSTART}",
        f"DTEND:{_DTEND}",
        f"DTSTAMP:{_DTSTAMP}",
    ]
    lines.extend(extra_lines)
    result = "\r\n".join(lines)
    for sc in subcomponents:
        result += "\r\n" + sc
    result += "\r\nEND:VEVENT\r\n"
    return result


def _sanitize(
    ical: str,
    mode: str = "normal",
    source_uid: str | None = None,
    private_work_sync: bool = False,
    keep_reminders: bool = False,
) -> ICalGLib.Component:
    """Call EventSanitizer.sanitize with a fresh UUID and return the component."""
    return EventSanitizer.sanitize(
        ical,
        str(uuid.uuid4()),
        mode=mode,
        source_uid=source_uid,
        private_work_sync=private_work_sync,
        keep_reminders=keep_reminders,
    )


def _has_category(comp: ICalGLib.Component, value: str) -> bool:
    """Return True if any CATEGORIES property on comp equals value."""
    prop = comp.get_first_property(ICalGLib.PropertyKind.CATEGORIES_PROPERTY)
    while prop:
        if (prop.get_categories() or "") == value:
            return True
        prop = comp.get_next_property(ICalGLib.PropertyKind.CATEGORIES_PROPERTY)
    return False


def _has_property(comp: ICalGLib.Component, kind: ICalGLib.PropertyKind) -> bool:
    return comp.get_first_property(kind) is not None


# ---------------------------------------------------------------------------
# TestIsManagedEvent
# ---------------------------------------------------------------------------


class TestIsManagedEvent:
    def test_managed_marker_detected(self):
        """CATEGORIES:CALENDAR-SYNC-MANAGED → is_managed_event returns True."""
        comp = ICalGLib.Component.new_from_string(
            _make_vevent("MM1", ["CATEGORIES:CALENDAR-SYNC-MANAGED"])
        )
        assert EventSanitizer.is_managed_event(comp) is True

    def test_other_category(self):
        """A different CATEGORIES value is not recognised as managed."""
        comp = ICalGLib.Component.new_from_string(_make_vevent("OC1", ["CATEGORIES:WORK"]))
        assert EventSanitizer.is_managed_event(comp) is False

    def test_no_categories(self):
        """Event without any CATEGORIES property is not managed."""
        comp = ICalGLib.Component.new_from_string(_make_vevent("NC1"))
        assert EventSanitizer.is_managed_event(comp) is False

    def test_managed_in_second_categories(self):
        """Marker in a second CATEGORIES property is still detected."""
        comp = ICalGLib.Component.new_from_string(
            _make_vevent("MSC1", ["CATEGORIES:WORK", "CATEGORIES:CALENDAR-SYNC-MANAGED"])
        )
        assert EventSanitizer.is_managed_event(comp) is True

    def test_vcalendar_wrapper(self):
        """is_managed_event is called on the VEVENT (extracted from VCALENDAR), not on the
        VCALENDAR wrapper itself — verify that extraction + call returns True."""
        vevent_str = _make_vevent("VCW1", ["CATEGORIES:CALENDAR-SYNC-MANAGED"])
        vcal_str = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//Test//EN\r\n" + vevent_str + "END:VCALENDAR\r\n"
        )
        vcal = ICalGLib.Component.new_from_string(vcal_str)
        # Callers always pass the VEVENT, not the VCALENDAR — extract it here.
        vevent = vcal.get_first_component(ICalGLib.ComponentKind.VEVENT_COMPONENT)
        assert EventSanitizer.is_managed_event(vevent) is True


# ---------------------------------------------------------------------------
# TestSanitizePropertyStripping
# ---------------------------------------------------------------------------


class TestSanitizePropertyStripping:
    """Verify that sanitize() removes sensitive / protocol-specific properties."""

    def test_keeps_description_in_normal_mode(self):
        result = _sanitize(_make_vevent("SD1", ["DESCRIPTION:Secret meeting notes"]))
        assert _has_property(result, ICalGLib.PropertyKind.DESCRIPTION_PROPERTY)

    def test_keeps_location_in_normal_mode(self):
        result = _sanitize(_make_vevent("SL1", ["LOCATION:Conference Room 42"]))
        assert _has_property(result, ICalGLib.PropertyKind.LOCATION_PROPERTY)

    def test_strips_attendees(self):
        result = _sanitize(
            _make_vevent(
                "SA1",
                [
                    "ATTENDEE;CN=Alice:mailto:alice@example.com",
                    "ATTENDEE;CN=Bob:mailto:bob@example.com",
                ],
            )
        )
        assert not _has_property(result, ICalGLib.PropertyKind.ATTENDEE_PROPERTY)

    def test_strips_organizer(self):
        result = _sanitize(_make_vevent("SO1", ["ORGANIZER:mailto:host@example.com"]))
        assert not _has_property(result, ICalGLib.PropertyKind.ORGANIZER_PROPERTY)

    def test_strips_x_properties(self):
        result = _sanitize(
            _make_vevent(
                "SX1",
                [
                    "X-MS-OLK-SOMETHING:some-value",
                    "X-MICROSOFT-CDO-ALLDAYEVENT:FALSE",
                ],
            )
        )
        assert not _has_property(result, ICalGLib.PropertyKind.X_PROPERTY)

    def test_strips_status(self):
        result = _sanitize(_make_vevent("SS1", ["STATUS:CONFIRMED"]))
        assert not _has_property(result, ICalGLib.PropertyKind.STATUS_PROPERTY)

    def test_strips_recurrence_id(self):
        """RECURRENCE-ID is stripped so exception occurrences become standalone events."""
        result = _sanitize(_make_vevent("SR1", [f"RECURRENCE-ID:{_DTSTART}"]))
        assert not _has_property(result, ICalGLib.PropertyKind.RECURRENCEID_PROPERTY)

    def test_keeps_summary(self):
        result = _sanitize(_make_vevent("KS1", summary="My Important Meeting"))
        prop = result.get_first_property(ICalGLib.PropertyKind.SUMMARY_PROPERTY)
        assert prop is not None
        assert prop.get_summary() == "My Important Meeting"

    def test_keeps_dtstart_dtend(self):
        result = _sanitize(_make_vevent("KDT1"))
        assert _has_property(result, ICalGLib.PropertyKind.DTSTART_PROPERTY)
        assert _has_property(result, ICalGLib.PropertyKind.DTEND_PROPERTY)

    def test_keeps_rrule_exdate(self):
        """RRULE and EXDATE are not stripped (recurring series must survive)."""
        ical = (
            "BEGIN:VEVENT\r\n"
            "UID:KRE1\r\n"
            "SUMMARY:Recurring\r\n"
            f"DTSTART:{_DTSTART}\r\n"
            f"DTEND:{_DTEND}\r\n"
            f"DTSTAMP:{_DTSTAMP}\r\n"
            "RRULE:FREQ=DAILY;COUNT=5\r\n"
            # EXDATE on second occurrence (not DTSTART) to avoid advancement logic
            "EXDATE;VALUE=DATE:20260302\r\n"
            "END:VEVENT\r\n"
        )
        result = _sanitize(ical)
        assert _has_property(result, ICalGLib.PropertyKind.RRULE_PROPERTY)
        assert _has_property(result, ICalGLib.PropertyKind.EXDATE_PROPERTY)

    def test_uid_replaced(self):
        """The original UID is replaced with the new_uid argument."""
        new_uid = "brand-new-uid-" + str(uuid.uuid4())
        result = EventSanitizer.sanitize(_make_vevent("ORIG-UID"), new_uid)
        assert result.get_uid() == new_uid


# ---------------------------------------------------------------------------
# TestSanitizeModes
# ---------------------------------------------------------------------------


class TestSanitizeModes:
    def test_normal_mode_keeps_summary(self):
        """mode='normal' preserves the original SUMMARY."""
        result = _sanitize(_make_vevent("NMK1", summary="Real Meeting Title"), mode="normal")
        prop = result.get_first_property(ICalGLib.PropertyKind.SUMMARY_PROPERTY)
        assert prop is not None
        assert prop.get_summary() == "Real Meeting Title"

    def test_busy_mode_replaces_summary(self):
        """mode='busy' replaces SUMMARY with the literal string 'Busy'."""
        result = _sanitize(_make_vevent("BMR1", summary="Real Meeting Title"), mode="busy")
        prop = result.get_first_property(ICalGLib.PropertyKind.SUMMARY_PROPERTY)
        assert prop is not None
        assert prop.get_summary() == "Busy"


# ---------------------------------------------------------------------------
# TestSanitizePrivateWorkSync
# ---------------------------------------------------------------------------


class TestSanitizePrivateWorkSync:
    """Verify private_work_sync=True hides work event details in normal mode."""

    def test_private_strips_description(self):
        result = _sanitize(
            _make_vevent("PWD1", ["DESCRIPTION:Confidential notes"]),
            private_work_sync=True,
        )
        assert not _has_property(result, ICalGLib.PropertyKind.DESCRIPTION_PROPERTY)

    def test_private_strips_location(self):
        result = _sanitize(
            _make_vevent("PWL1", ["LOCATION:Executive Boardroom"]),
            private_work_sync=True,
        )
        assert not _has_property(result, ICalGLib.PropertyKind.LOCATION_PROPERTY)

    def test_private_replaces_summary(self):
        result = _sanitize(
            _make_vevent("PWS1", summary="Quarterly Review"),
            private_work_sync=True,
        )
        prop = result.get_first_property(ICalGLib.PropertyKind.SUMMARY_PROPERTY)
        assert prop is not None
        assert prop.get_summary() == "Work Commitment"

    def test_busy_still_strips_description(self):
        """mode='busy' must strip DESCRIPTION regardless of private_work_sync."""
        result = _sanitize(
            _make_vevent("BSD1", ["DESCRIPTION:Personal notes"]),
            mode="busy",
        )
        assert not _has_property(result, ICalGLib.PropertyKind.DESCRIPTION_PROPERTY)

    def test_busy_still_strips_location(self):
        """mode='busy' must strip LOCATION regardless of private_work_sync."""
        result = _sanitize(
            _make_vevent("BSL1", ["LOCATION:Home office"]),
            mode="busy",
        )
        assert not _has_property(result, ICalGLib.PropertyKind.LOCATION_PROPERTY)


# ---------------------------------------------------------------------------
# TestSanitizeManagedMarkers
# ---------------------------------------------------------------------------


class TestSanitizeManagedMarkers:
    def test_adds_managed_category(self):
        """CATEGORIES:CALENDAR-SYNC-MANAGED is present after sanitize."""
        result = _sanitize(_make_vevent("AMC1"))
        assert _has_category(result, "CALENDAR-SYNC-MANAGED")

    def test_adds_private_class(self):
        """CLASS:PRIVATE is added after sanitize."""
        result = _sanitize(_make_vevent("APC1"))
        cls_prop = result.get_first_property(ICalGLib.PropertyKind.CLASS_PROPERTY)
        assert cls_prop is not None
        val = cls_prop.get_value_as_string() or ""
        assert "PRIVATE" in val.upper()

    def test_adds_source_fingerprint(self):
        """source_uid is embedded as CATEGORIES:CALENDAR-SYNC-SRC-<hex16>."""
        source_uid = "work-event-uid-abc123"
        result = EventSanitizer.sanitize(
            _make_vevent("ASF1"), str(uuid.uuid4()), source_uid=source_uid
        )
        fingerprint = compute_source_fingerprint(source_uid)
        expected_category = f"CALENDAR-SYNC-SRC-{fingerprint}"
        assert _has_category(result, expected_category)

    def test_no_fingerprint_without_source_uid(self):
        """When source_uid is None, no CALENDAR-SYNC-SRC- category is added."""
        result = EventSanitizer.sanitize(_make_vevent("NFS1"), str(uuid.uuid4()), source_uid=None)
        prop = result.get_first_property(ICalGLib.PropertyKind.CATEGORIES_PROPERTY)
        while prop:
            cat = prop.get_categories() or ""
            assert not cat.startswith("CALENDAR-SYNC-SRC-"), (
                f"Unexpected fingerprint category: {cat}"
            )
            prop = result.get_next_property(ICalGLib.PropertyKind.CATEGORIES_PROPERTY)


# ---------------------------------------------------------------------------
# TestSanitizeAlarms
# ---------------------------------------------------------------------------


def _count_valarms(comp: ICalGLib.Component) -> int:
    """Return the number of VALARM sub-components on comp."""
    count = 0
    sub = comp.get_first_component(ICalGLib.ComponentKind.VALARM_COMPONENT)
    while sub:
        count += 1
        sub = comp.get_next_component(ICalGLib.ComponentKind.VALARM_COMPONENT)
    return count


class TestSanitizeAlarms:
    """VALARM sub-components are stripped by default; preserved with keep_reminders=True."""

    def test_alarm_stripped_by_default(self):
        """A single VALARM is removed when keep_reminders is not set (defaults to False)."""
        ical = _make_vevent("AL1", subcomponents=[_VALARM_DISPLAY])
        result = _sanitize(ical)
        assert _count_valarms(result) == 0

    def test_alarm_stripped_explicit_false(self):
        """A single VALARM is removed when keep_reminders=False is explicit."""
        ical = _make_vevent("AL2", subcomponents=[_VALARM_DISPLAY])
        result = _sanitize(ical, keep_reminders=False)
        assert _count_valarms(result) == 0

    def test_alarm_preserved_when_keep_reminders(self):
        """A single VALARM is kept when keep_reminders=True."""
        ical = _make_vevent("AL3", subcomponents=[_VALARM_DISPLAY])
        result = _sanitize(ical, keep_reminders=True)
        assert _count_valarms(result) == 1

    def test_multiple_alarms_all_stripped(self):
        """All VALARM sub-components are removed (not just the first) by default."""
        ical = _make_vevent("AL4", subcomponents=[_VALARM_DISPLAY, _VALARM_EMAIL])
        result = _sanitize(ical)
        assert _count_valarms(result) == 0

    def test_multiple_alarms_all_preserved(self):
        """All VALARM sub-components are kept when keep_reminders=True."""
        ical = _make_vevent("AL5", subcomponents=[_VALARM_DISPLAY, _VALARM_EMAIL])
        result = _sanitize(ical, keep_reminders=True)
        assert _count_valarms(result) == 2

    def test_no_alarm_no_error(self):
        """sanitize() with keep_reminders=False on an event with no VALARM is a no-op."""
        ical = _make_vevent("AL6")
        result = _sanitize(ical)
        assert _count_valarms(result) == 0

    def test_alarm_stripped_in_busy_mode(self):
        """VALARM is stripped in busy mode (personal→work) regardless of mode."""
        ical = _make_vevent("AL7", subcomponents=[_VALARM_DISPLAY])
        result = _sanitize(ical, mode="busy")
        assert _count_valarms(result) == 0

    def test_alarm_preserved_in_busy_mode_with_keep_reminders(self):
        """VALARM is preserved in busy mode when keep_reminders=True."""
        ical = _make_vevent("AL8", subcomponents=[_VALARM_DISPLAY])
        result = _sanitize(ical, mode="busy", keep_reminders=True)
        assert _count_valarms(result) == 1
