"""
iCal event sanitization — strips sensitive data before syncing.
"""

import datetime

import gi

gi.require_version("ICalGLib", "3.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: F401 (GLib kept for version side-effect)
from gi.repository import ICalGLib  # noqa: F401 (GLib kept for version side-effect)


class EventSanitizer:
    """Handles sanitization of calendar events per privacy spec."""

    @staticmethod
    def is_managed_event(component: ICalGLib.Component) -> bool:
        """Check if an event was created by our sync tool."""
        # Check CATEGORIES property for our marker
        # (X-properties and COMMENT are stripped by Microsoft 365, so we use CATEGORIES)
        prop = component.get_first_property(ICalGLib.PropertyKind.CATEGORIES_PROPERTY)
        while prop:
            categories = prop.get_categories()
            if categories and "CALENDAR-SYNC-MANAGED" in categories:
                return True
            prop = component.get_next_property(ICalGLib.PropertyKind.CATEGORIES_PROPERTY)
        return False

    @staticmethod
    def _remove_all_properties(component: ICalGLib.Component, prop_kind: ICalGLib.PropertyKind):
        """Remove all instances of a specific property from a component."""
        prop = component.get_first_property(prop_kind)
        while prop:
            component.remove_property(prop)
            prop = component.get_first_property(prop_kind)

    @staticmethod
    def _remove_all_components(component: ICalGLib.Component, comp_kind: ICalGLib.ComponentKind):
        """Remove all sub-components of a specific kind."""
        subcomp = component.get_first_component(comp_kind)
        while subcomp:
            component.remove_component(subcomp)
            subcomp = component.get_first_component(comp_kind)

    @classmethod
    def sanitize(cls, ical_string: str, new_uid: str, mode: str = "normal") -> ICalGLib.Component:
        """
        Parse an iCal string, replace UID, and strip sensitive data.

        Args:
            ical_string: Raw iCal data from source calendar
            new_uid: New UUID to assign to the event
            mode: 'normal' = source→target (strip details, keep title)
                  'busy' = target→source (strip everything, title becomes "Busy")

        Returns:
            Sanitized ICalGLib.Component ready for target calendar
        """
        comp = ICalGLib.Component.new_from_string(ical_string)

        # Properties to strip for security/privacy
        # Note: We strip these specific properties and retain everything else
        # (SUMMARY, DTSTART, DTEND, RRULE, EXDATE, etc. are kept by default)
        #
        # RECURRENCE-ID is stripped to make each event standalone in the target
        # calendar. Exception occurrences from recurring series (which carry
        # RECURRENCE-ID) are created as ordinary one-off events so the target
        # Exchange/CalDAV backend does not reject them with "ExpandSeries can
        # only be performed against a series".
        strip_props = [
            ICalGLib.PropertyKind.DESCRIPTION_PROPERTY,
            ICalGLib.PropertyKind.LOCATION_PROPERTY,
            ICalGLib.PropertyKind.ATTACH_PROPERTY,
            ICalGLib.PropertyKind.URL_PROPERTY,
            ICalGLib.PropertyKind.ORGANIZER_PROPERTY,
            ICalGLib.PropertyKind.ATTENDEE_PROPERTY,
            ICalGLib.PropertyKind.RECURRENCEID_PROPERTY,
            # Strip STATUS so Exchange creates the event as a plain appointment
            # rather than attempting to process it as a meeting response.
            # STATUS:CANCELLED events are skipped entirely before sanitize.
            ICalGLib.PropertyKind.STATUS_PROPERTY,
            # Strip all vendor/server-specific X- extension properties.
            # Exchange embeds X-MS-OLK-*, X-MICROSOFT-CDO-*, and
            # X-MS-EXCHANGE-ORGANIZATION-* properties that can reference
            # internal Exchange objects in the source tenant.  Keeping them
            # causes ErrorItemNotFound when the target Exchange server tries
            # to resolve those references.
            ICalGLib.PropertyKind.X_PROPERTY,
        ]

        def sanitize_vevent(event):
            """Sanitize a single VEVENT component."""
            # Replace UID to disconnect from source tracking
            cls._remove_all_properties(event, ICalGLib.PropertyKind.UID_PROPERTY)
            event.add_property(ICalGLib.Property.new_uid(new_uid))

            # Strip security/protocol-sensitive properties
            for prop_kind in strip_props:
                cls._remove_all_properties(event, prop_kind)

            # Remove alarms to prevent duplicate notifications
            cls._remove_all_components(event, ICalGLib.ComponentKind.VALARM_COMPONENT)

            # For 'busy' mode, replace title with "Busy"
            if mode == "busy":
                cls._remove_all_properties(event, ICalGLib.PropertyKind.SUMMARY_PROPERTY)
                event.add_property(ICalGLib.Property.new_summary("Busy"))

            # Advance DTSTART (and DTEND) to the first occurrence not excluded
            # by EXDATE, when DTSTART itself falls on an excluded date.
            # Exchange does not reliably suppress a series occurrence when the
            # EXDATE coincides with DTSTART — it may still render that first
            # occurrence in the calendar.  Moving the series start to the next
            # valid date avoids the ambiguity entirely.
            _dts_prop = event.get_first_property(ICalGLib.PropertyKind.DTSTART_PROPERTY)
            _rrule_prop = event.get_first_property(ICalGLib.PropertyKind.RRULE_PROPERTY)
            if _dts_prop and _rrule_prop:
                _exdates: set[str] = set()
                _ed_p = event.get_first_property(ICalGLib.PropertyKind.EXDATE_PROPERTY)
                while _ed_p:
                    try:
                        _et = _ed_p.get_exdate()
                        if _et and not _et.is_null_time():
                            _exdates.add(
                                f"{_et.get_year():04d}{_et.get_month():02d}{_et.get_day():02d}"
                            )
                    except Exception:
                        pass
                    _ed_p = event.get_next_property(ICalGLib.PropertyKind.EXDATE_PROPERTY)
                if _exdates:
                    _dts = _dts_prop.get_dtstart()
                    _dts_date = f"{_dts.get_year():04d}{_dts.get_month():02d}{_dts.get_day():02d}"
                    if _dts_date in _exdates:
                        try:
                            # Compute event duration so DTEND can be shifted by the
                            # same amount as DTSTART.
                            _dte_prop = event.get_first_property(
                                ICalGLib.PropertyKind.DTEND_PROPERTY
                            )
                            if _dte_prop:
                                _dte = _dte_prop.get_dtend()
                                _dts_py = datetime.datetime(
                                    _dts.get_year(),
                                    _dts.get_month(),
                                    _dts.get_day(),
                                    _dts.get_hour(),
                                    _dts.get_minute(),
                                    _dts.get_second(),
                                )
                                _dte_py = datetime.datetime(
                                    _dte.get_year(),
                                    _dte.get_month(),
                                    _dte.get_day(),
                                    _dte.get_hour(),
                                    _dte.get_minute(),
                                    _dte.get_second(),
                                )
                                _dur = _dte_py - _dts_py
                            else:
                                _dte_prop = None
                                _dur = datetime.timedelta(hours=1)

                            # Find first RRULE occurrence not in EXDATE.
                            _rule = _rrule_prop.get_rrule()
                            _it = ICalGLib.RecurIterator.new(_rule, _dts)
                            for _ in range(500):
                                _occ = _it.next()
                                if _occ is None or _occ.is_null_time():
                                    break
                                _occ_date = (
                                    f"{_occ.get_year():04d}"
                                    f"{_occ.get_month():02d}"
                                    f"{_occ.get_day():02d}"
                                )
                                if _occ_date not in _exdates:
                                    # Build new DTSTART / DTEND strings, preserving TZID.
                                    _tzid_param = _dts_prop.get_first_parameter(
                                        ICalGLib.ParameterKind.TZID_PARAMETER
                                    )
                                    _occ_py = datetime.datetime(
                                        _occ.get_year(),
                                        _occ.get_month(),
                                        _occ.get_day(),
                                        _occ.get_hour(),
                                        _occ.get_minute(),
                                        _occ.get_second(),
                                    )
                                    _new_dte_py = _occ_py + _dur
                                    if _tzid_param:
                                        _tz = _tzid_param.get_tzid()
                                        _new_dts_str = (
                                            f"DTSTART;TZID={_tz}:"
                                            f"{_occ_py.year:04d}{_occ_py.month:02d}"
                                            f"{_occ_py.day:02d}"
                                            f"T{_occ_py.hour:02d}{_occ_py.minute:02d}"
                                            f"{_occ_py.second:02d}"
                                        )
                                        _new_dte_str = (
                                            f"DTEND;TZID={_tz}:"
                                            f"{_new_dte_py.year:04d}{_new_dte_py.month:02d}"
                                            f"{_new_dte_py.day:02d}"
                                            f"T{_new_dte_py.hour:02d}{_new_dte_py.minute:02d}"
                                            f"{_new_dte_py.second:02d}"
                                        )
                                    else:
                                        _new_dts_str = (
                                            f"DTSTART:"
                                            f"{_occ_py.year:04d}{_occ_py.month:02d}"
                                            f"{_occ_py.day:02d}"
                                            f"T{_occ_py.hour:02d}{_occ_py.minute:02d}"
                                            f"{_occ_py.second:02d}Z"
                                        )
                                        _new_dte_str = (
                                            f"DTEND:"
                                            f"{_new_dte_py.year:04d}{_new_dte_py.month:02d}"
                                            f"{_new_dte_py.day:02d}"
                                            f"T{_new_dte_py.hour:02d}{_new_dte_py.minute:02d}"
                                            f"{_new_dte_py.second:02d}Z"
                                        )
                                    cls._remove_all_properties(
                                        event, ICalGLib.PropertyKind.DTSTART_PROPERTY
                                    )
                                    event.add_property(
                                        ICalGLib.Property.new_from_string(_new_dts_str)
                                    )
                                    if _dte_prop:
                                        cls._remove_all_properties(
                                            event, ICalGLib.PropertyKind.DTEND_PROPERTY
                                        )
                                        event.add_property(
                                            ICalGLib.Property.new_from_string(_new_dte_str)
                                        )
                                    break
                        except Exception:
                            pass  # On any error, leave DTSTART unchanged

            # Normalise EXDATE;VALUE=DATE to EXDATE;TZID=<tz>:<datetime>.
            # Exchange ignores date-only EXDATEs when DTSTART carries a TZID —
            # the two formats don't match in Exchange's internal comparison so
            # the excluded occurrences still appear in the calendar.
            # Re-read DTSTART here to pick up any advancement done above.
            _cur_dts_prop = event.get_first_property(ICalGLib.PropertyKind.DTSTART_PROPERTY)
            if _cur_dts_prop:
                _tz_norm_p = _cur_dts_prop.get_first_parameter(
                    ICalGLib.ParameterKind.TZID_PARAMETER
                )
                if _tz_norm_p:
                    _tz_norm = _tz_norm_p.get_tzid()
                    _dts_norm = _cur_dts_prop.get_dtstart()
                    _hms_norm = (
                        f"T{_dts_norm.get_hour():02d}"
                        f"{_dts_norm.get_minute():02d}"
                        f"{_dts_norm.get_second():02d}"
                    )
                    _date_exdates: set[str] = set()
                    _ed_n = event.get_first_property(ICalGLib.PropertyKind.EXDATE_PROPERTY)
                    while _ed_n:
                        try:
                            _ed_t = _ed_n.get_exdate()
                            if _ed_t and not _ed_t.is_null_time() and _ed_t.is_date():
                                _date_exdates.add(
                                    f"{_ed_t.get_year():04d}"
                                    f"{_ed_t.get_month():02d}"
                                    f"{_ed_t.get_day():02d}"
                                )
                                event.remove_property(_ed_n)
                                _ed_n = event.get_first_property(
                                    ICalGLib.PropertyKind.EXDATE_PROPERTY
                                )
                                continue
                        except Exception:
                            pass
                        _ed_n = event.get_next_property(ICalGLib.PropertyKind.EXDATE_PROPERTY)
                    for _d in sorted(_date_exdates):
                        event.add_property(
                            ICalGLib.Property.new_from_string(
                                f"EXDATE;TZID={_tz_norm}:{_d}{_hms_norm}"
                            )
                        )

            # Add metadata to identify this as a managed event
            # Use CATEGORIES property (X-properties and COMMENT are stripped by Microsoft 365)
            # First remove any existing CATEGORIES to avoid duplicates
            cls._remove_all_properties(event, ICalGLib.PropertyKind.CATEGORIES_PROPERTY)
            categories_prop = ICalGLib.Property.new_categories("CALENDAR-SYNC-MANAGED")
            event.add_property(categories_prop)

            # Mark event as private so other users with read access to the
            # target calendar cannot see its title or details.
            # CLASS:PRIVATE is honoured by both Exchange/M365 ("Private
            # Appointment") and Google Calendar ("Private" visibility).
            cls._remove_all_properties(event, ICalGLib.PropertyKind.CLASS_PROPERTY)
            event.add_property(ICalGLib.Property.new_from_string("CLASS:PRIVATE"))

        # Check if comp is a VCALENDAR or a VEVENT directly
        if comp.isa() == ICalGLib.ComponentKind.VCALENDAR_COMPONENT:
            # Strip the METHOD property from the VCALENDAR wrapper.
            # METHOD:CANCEL or METHOD:REQUEST causes Exchange to treat the
            # create request as a meeting response and attempt to look up the
            # original meeting in the target calendar, resulting in
            # ErrorItemNotFound when the meeting doesn't exist there.
            cls._remove_all_properties(comp, ICalGLib.PropertyKind.METHOD_PROPERTY)

            # Process all VEVENT components inside VCALENDAR
            event = comp.get_first_component(ICalGLib.ComponentKind.VEVENT_COMPONENT)
            while event:
                sanitize_vevent(event)
                event = comp.get_next_component(ICalGLib.ComponentKind.VEVENT_COMPONENT)
        elif comp.isa() == ICalGLib.ComponentKind.VEVENT_COMPONENT:
            # It's already a VEVENT, sanitize it directly
            sanitize_vevent(comp)

        return comp
