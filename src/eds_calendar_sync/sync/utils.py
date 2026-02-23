"""
Stateless event-inspection helpers.
"""

import hashlib
import re

import gi

gi.require_version("GLib", "2.0")
gi.require_version("ICalGLib", "3.0")
from gi.repository import GLib
from gi.repository import ICalGLib

# Regex to extract the UNTIL date from an RRULE string as YYYYMMDD.
# Works for both date-only (UNTIL=20260316) and UTC datetime
# (UNTIL=20260316T100000Z) — we only need the date portion.
_RRULE_UNTIL_RE = re.compile(r"UNTIL=(\d{8})")

# E_CAL_CLIENT_ERROR_OBJECT_NOT_FOUND = 1  (from e-cal-client-error-quark)
_EDS_NOT_FOUND_CODE = 1
_EDS_CLIENT_ERROR_DOMAIN = "e-cal-client-error-quark"

# The M365 backend (e-m365-error-quark) embeds the Exchange EWS error name in the
# message string rather than mapping it to a fixed quark code.
_M365_ERROR_DOMAIN = "e-m365-error-quark"
_M365_NOT_FOUND_MSG = "ErrorItemNotFound"


def is_not_found_error(e: Exception) -> bool:
    """Return True when EDS reports that a calendar object does not exist.

    This distinguishes an externally-deleted event (which is harmless and
    should be handled silently) from genuine modify/delete failures.

    Covers both the generic EDS client quark (e-cal-client-error-quark code 1)
    and the M365 backend quark (e-m365-error-quark, which embeds the Exchange
    error name "ErrorItemNotFound" in the message text).
    """
    if isinstance(e, GLib.Error):
        domain = e.domain or ""
        if e.code == _EDS_NOT_FOUND_CODE and _EDS_CLIENT_ERROR_DOMAIN in domain:
            return True
        if _M365_ERROR_DOMAIN in domain and _M365_NOT_FOUND_MSG in (e.message or ""):
            return True
    return "object not found" in str(e).lower()


def compute_hash(ical_string: str) -> str:
    """
    Generate SHA256 hash of iCal content for change detection.

    Normalizes the content by removing volatile server-added properties
    to prevent false change detection.
    """
    comp = ICalGLib.Component.new_from_string(ical_string)

    # Properties that servers often add/modify and should be ignored for change detection
    volatile_props = [
        ICalGLib.PropertyKind.DTSTAMP_PROPERTY,  # Timestamp when event was created/modified
        ICalGLib.PropertyKind.LASTMODIFIED_PROPERTY,  # Last modification time
        ICalGLib.PropertyKind.CREATED_PROPERTY,  # Creation time
        ICalGLib.PropertyKind.SEQUENCE_PROPERTY,  # Sequence number for updates
    ]

    def normalize_vevent(event):
        """Remove volatile properties from a VEVENT."""
        for prop_kind in volatile_props:
            prop = event.get_first_property(prop_kind)
            while prop:
                event.remove_property(prop)
                prop = event.get_first_property(prop_kind)

    # Normalize all VEVENT components
    if comp.isa() == ICalGLib.ComponentKind.VCALENDAR_COMPONENT:
        event = comp.get_first_component(ICalGLib.ComponentKind.VEVENT_COMPONENT)
        while event:
            normalize_vevent(event)
            event = comp.get_next_component(ICalGLib.ComponentKind.VEVENT_COMPONENT)
    elif comp.isa() == ICalGLib.ComponentKind.VEVENT_COMPONENT:
        normalize_vevent(comp)

    normalized_ical = comp.as_ical_string()
    return hashlib.sha256(normalized_ical.encode("utf-8")).hexdigest()


def parse_component(obj) -> ICalGLib.Component:
    """Handle both string and native Component objects from EDS API."""
    if isinstance(obj, str):
        return ICalGLib.Component.new_from_string(obj)
    return obj


def has_valid_occurrences(comp: ICalGLib.Component) -> bool:
    """Return False if a recurring event expands to zero non-excluded occurrences.

    Exchange rejects creating a recurring series that has all its occurrences
    excluded by EXDATE (ErrorItemNotFound), so we detect and skip such events
    before attempting creation.

    Returns True for non-recurring events and on any API error (safe fallback).
    """
    check = comp
    if comp.isa() == ICalGLib.ComponentKind.VCALENDAR_COMPONENT:
        check = comp.get_first_component(ICalGLib.ComponentKind.VEVENT_COMPONENT)
        if not check:
            return True

    rrule_prop = check.get_first_property(ICalGLib.PropertyKind.RRULE_PROPERTY)
    if not rrule_prop:
        return True  # Non-recurring event always has a valid "occurrence"

    # Collect excluded dates as YYYYMMDD strings for quick lookup
    exdates = set()
    prop = check.get_first_property(ICalGLib.PropertyKind.EXDATE_PROPERTY)
    while prop:
        try:
            t = prop.get_exdate()
            if t and not t.is_null_time():
                exdates.add(f"{t.get_year():04d}{t.get_month():02d}{t.get_day():02d}")
        except Exception:
            pass
        prop = check.get_next_property(ICalGLib.PropertyKind.EXDATE_PROPERTY)

    if not exdates:
        return True  # No exclusions → series has occurrences

    # Expand the recurrence rule and check whether any occurrence falls
    # outside the EXDATE set.  Cap at 500 iterations as a safety measure.
    try:
        rule = rrule_prop.get_rrule()
        dtstart = check.get_dtstart()

        # When DTSTART carries a TZID (datetime) but UNTIL in the RRULE is
        # a date-only value, libical's RecurIterator does not reliably stop
        # at UNTIL — it emits spurious occurrences past the series end.
        # Parse UNTIL from the raw RRULE string — more reliable than
        # rule.get_until() which may silently fail in some libical-glib builds.
        until_str = None
        try:
            m = _RRULE_UNTIL_RE.search(rrule_prop.get_value_as_string() or "")
            if m:
                until_str = m.group(1)
        except Exception:
            pass

        it = ICalGLib.RecurIterator.new(rule, dtstart)
        for _ in range(500):
            occ = it.next()
            if occ is None or occ.is_null_time():
                break
            occ_key = f"{occ.get_year():04d}{occ.get_month():02d}{occ.get_day():02d}"
            if until_str and occ_key > until_str:
                break  # Past UNTIL — no further occurrences in this series
            if occ_key not in exdates:
                return True  # Found at least one valid occurrence
    except Exception:
        return True  # On any API error assume valid — don't silently skip

    return False  # Every expanded occurrence is in EXDATE


def is_event_cancelled(comp: ICalGLib.Component) -> bool:
    """Return True if the event's STATUS is CANCELLED.

    Cancelled events are not synced: they no longer block time, and
    Exchange rejects creating STATUS:CANCELLED items via CreateItem
    (it tries to cancel an existing meeting that does not exist in the
    target calendar, returning ErrorItemNotFound).
    """
    check = comp
    if comp.isa() == ICalGLib.ComponentKind.VCALENDAR_COMPONENT:
        check = comp.get_first_component(ICalGLib.ComponentKind.VEVENT_COMPONENT)
        if not check:
            return False
    status_prop = check.get_first_property(ICalGLib.PropertyKind.STATUS_PROPERTY)
    if not status_prop:
        return False
    try:
        return status_prop.get_status() == ICalGLib.PropertyStatus.CANCELLED
    except (AttributeError, TypeError):
        val = status_prop.get_value_as_string() or ""
        return val.strip().upper() == "CANCELLED"


def is_free_time(comp: ICalGLib.Component) -> bool:
    """Return True if the event is transparent (does not block time).

    TRANSP:TRANSPARENT means the event does not show the user as busy.
    In Exchange this is set automatically when you decline a meeting, and
    can also be set manually on informational/optional events.  Either
    way, transparent events should not be mirrored as busy blocks in the
    personal calendar.

    The iCal default (no TRANSP property) is OPAQUE, which blocks time.
    """
    check = comp
    if comp.isa() == ICalGLib.ComponentKind.VCALENDAR_COMPONENT:
        check = comp.get_first_component(ICalGLib.ComponentKind.VEVENT_COMPONENT)
        if not check:
            return False
    transp_prop = check.get_first_property(ICalGLib.PropertyKind.TRANSP_PROPERTY)
    if not transp_prop:
        return False  # Default is OPAQUE — event blocks time
    try:
        return transp_prop.get_transp() == ICalGLib.PropertyTransp.TRANSPARENT
    except (AttributeError, TypeError):
        val = transp_prop.get_value_as_string() or ""
        return val.strip().upper() == "TRANSPARENT"
