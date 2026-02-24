"""
Stateless event-inspection helpers.
"""

import hashlib
import logging
import re
from typing import TYPE_CHECKING

import gi

_logger = logging.getLogger(__name__)

gi.require_version("GLib", "2.0")
gi.require_version("ICalGLib", "3.0")
from gi.repository import GLib
from gi.repository import ICalGLib

if TYPE_CHECKING:
    from eds_calendar_sync.db import StateDatabase
    from eds_calendar_sync.eds_client import EDSCalendarClient

# Regex to extract the UNTIL date from an RRULE string as YYYYMMDD.
# Works for both date-only (UNTIL=20260316) and UTC datetime
# (UNTIL=20260316T100000Z) — we only need the date portion.
_RRULE_UNTIL_RE = re.compile(r"UNTIL=(\d{8})")

# Regex to extract excluded dates from EXDATE lines in an iCal string.
# Matches both VALUE=DATE (EXDATE;VALUE=DATE:20260216) and TZID datetime
# (EXDATE;TZID=...:20260216T110000) forms — captures the YYYYMMDD prefix.
_EXDATE_DATE_RE = re.compile(r"^EXDATE[^:\n]*:(\d{8})", re.MULTILINE)

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

    # Collect excluded dates as YYYYMMDD strings for quick lookup.
    # Try the ICalGLib accessor first; fall back to parsing the component's
    # iCal string directly when get_exdate() returns null_time (a known
    # silent failure for EXDATE;VALUE=DATE properties in some libical-glib
    # builds).
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
        # Fallback: parse the component's iCal string.  Use the top-level
        # comp (VCALENDAR when available) rather than the child VEVENT check
        # — calling as_ical_string() on a child component obtained via
        # get_first_component() may raise or return an empty string in some
        # libical-glib builds, silently defeating the regex search.
        try:
            for m in _EXDATE_DATE_RE.finditer(comp.as_ical_string() or ""):
                exdates.add(m.group(1))
        except Exception:
            pass

    uid_for_log = check.get_uid() if check else "?"
    _logger.debug("has_valid_occurrences: uid=%s exdates=%s", uid_for_log, exdates)

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
        # Parse UNTIL from the top-level comp string (same rationale as the
        # EXDATE fallback above: child as_ical_string() may silently fail).
        until_str = None
        try:
            m = _RRULE_UNTIL_RE.search(comp.as_ical_string() or "")
            if m:
                until_str = m.group(1)
        except Exception:
            pass

        _logger.debug("has_valid_occurrences: until_str=%s", until_str)

        # Use a floating (timezone-free) copy of dtstart for RecurIterator.
        # If dtstart carries a TZID and that timezone is not in libical's
        # built-in database, RecurIterator.new() may raise, which the outer
        # except clause would catch and convert into an incorrect True return.
        # A floating copy has no timezone and always succeeds; we compare
        # only YYYYMMDD strings so timezone precision is not needed here.
        try:
            _y = dtstart.get_year()
            _mo = dtstart.get_month()
            _d = dtstart.get_day()
            _h = dtstart.get_hour()
            _mi = dtstart.get_minute()
            _s = dtstart.get_second()
            dtstart_for_iter = ICalGLib.Time.new_from_string(
                f"{_y:04d}{_mo:02d}{_d:02d}T{_h:02d}{_mi:02d}{_s:02d}"
            )
        except Exception:
            dtstart_for_iter = dtstart

        it = ICalGLib.RecurIterator.new(rule, dtstart_for_iter)
        for _ in range(500):
            occ = it.next()
            if occ is None or occ.is_null_time():
                break
            occ_key = f"{occ.get_year():04d}{occ.get_month():02d}{occ.get_day():02d}"
            if until_str and occ_key > until_str:
                break  # Past UNTIL — no further occurrences in this series
            if occ_key not in exdates:
                _logger.debug(
                    "has_valid_occurrences: found valid occurrence %s (not in exdates)",
                    occ_key,
                )
                return True  # Found at least one valid occurrence
    except Exception as _e:
        _logger.debug("has_valid_occurrences: iterator error: %s", _e)
        return True  # On any API error assume valid — don't silently skip

    _logger.debug("has_valid_occurrences: uid=%s is an empty series", uid_for_log)
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


def strip_exdates_for_dates(ical_str: str, dates: set[str]) -> str:
    """Return a copy of ical_str with EXDATE lines for the given dates removed.

    Exchange represents every explicitly-defined recurring occurrence as both
    an EXDATE in the master VEVENT and a separate exception VEVENT (RECURRENCE-ID).
    These "phantom" EXDATEs suppress GNOME Calendar display even though the
    exception VEVENTs represent real meetings.  This function strips those EXDATEs
    so that the RRULE expands the correct occurrences in the personal calendar.

    ``dates`` is a set of YYYYMMDD strings.  Handles both date-only and datetime
    EXDATE forms::

        EXDATE;VALUE=DATE:20260303
        EXDATE;TZID=Europe/Berlin:20260303T110000
    """
    if not dates:
        return ical_str
    lines = ical_str.splitlines(keepends=True)
    result = []
    for line in lines:
        m = _EXDATE_DATE_RE.match(line)
        if m and m.group(1) in dates:
            continue
        result.append(line)
    return "".join(result)


def compute_source_fingerprint(source_uid: str) -> str:
    """Return the 16-char hex SHA-256 fingerprint of source_uid."""
    return hashlib.sha256(source_uid.encode()).hexdigest()[:16]


def build_orphan_index(
    target_client: "EDSCalendarClient",
    state_db: "StateDatabase",
    logger,
) -> dict[str, str]:
    """Scan target calendar for managed events not recorded in the state DB.

    Returns a dict mapping source_fingerprint → target_uid for orphaned
    managed events (events created by a previous sync run that crashed
    before the DB record was committed).

    Events that already have a state record are excluded from the result.
    Events without a fingerprint (created before Fix 3 was deployed) are
    skipped because they cannot be linked back to a source event.
    """
    from eds_calendar_sync.sanitizer import EventSanitizer

    orphans: dict[str, str] = {}
    try:
        all_events = target_client.get_all_events()
    except Exception as e:
        logger.warning(f"Orphan scan: could not fetch events: {e}")
        return orphans

    for obj in all_events:
        comp = parse_component(obj)
        if not EventSanitizer.is_managed_event(comp):
            continue

        fingerprint = EventSanitizer.get_source_fingerprint(comp)
        if not fingerprint:
            continue  # Pre-Fix-3 event: no fingerprint, cannot link

        target_uid = comp.get_uid()
        if not target_uid:
            continue

        # Skip if already tracked in state DB.
        # Managed events in the personal calendar are stored as target_uid;
        # managed events in the work calendar are stored as source_uid.
        # Check both to handle either calendar.
        if (
            state_db.get_by_target_uid(target_uid) is not None
            or state_db.get_by_source_uid(target_uid) is not None
        ):
            continue

        orphans[fingerprint] = target_uid
        logger.debug(f"Orphan scan: found untracked managed event {target_uid} (fp={fingerprint})")

    if orphans:
        logger.info(f"Orphan scan: found {len(orphans)} untracked managed event(s) to recover")
    return orphans
