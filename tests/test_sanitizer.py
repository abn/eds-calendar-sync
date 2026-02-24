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


def _make_vevent(uid: str, extra_lines: list[str] = (), summary: str = "Test Event") -> str:
    """Return a minimal VEVENT string with optional extra property lines."""
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SUMMARY:{summary}",
        f"DTSTART:{_DTSTART}",
        f"DTEND:{_DTEND}",
        f"DTSTAMP:{_DTSTAMP}",
    ]
    lines.extend(extra_lines)
    lines.append("END:VEVENT")
    return "\r\n".join(lines) + "\r\n"


def _sanitize(ical: str, mode: str = "normal", source_uid: str | None = None) -> ICalGLib.Component:
    """Call EventSanitizer.sanitize with a fresh UUID and return the component."""
    return EventSanitizer.sanitize(ical, str(uuid.uuid4()), mode=mode, source_uid=source_uid)


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

    def test_strips_description(self):
        result = _sanitize(_make_vevent("SD1", ["DESCRIPTION:Secret meeting notes"]))
        assert not _has_property(result, ICalGLib.PropertyKind.DESCRIPTION_PROPERTY)

    def test_strips_location(self):
        result = _sanitize(_make_vevent("SL1", ["LOCATION:Conference Room 42"]))
        assert not _has_property(result, ICalGLib.PropertyKind.LOCATION_PROPERTY)

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
