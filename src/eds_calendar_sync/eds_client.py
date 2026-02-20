"""
Evolution Data Server calendar connectivity wrapper.
"""

from typing import Optional, Tuple

import gi
gi.require_version('EDataServer', '1.2')
gi.require_version('ECal', '2.0')
gi.require_version('ICalGLib', '3.0')
from gi.repository import EDataServer, ECal, ICalGLib, GLib

from .models import CalendarSyncError


def get_calendar_display_info(calendar_uid: str) -> Tuple[str, str, str]:
    """
    Get human-readable information about a calendar.

    Returns:
        Tuple of (display_name, account_name, uid)
    """
    try:
        registry = EDataServer.SourceRegistry.new_sync(None)
        source = registry.ref_source(calendar_uid)

        if not source:
            return ("Unknown Calendar", "", calendar_uid)

        display_name = source.get_display_name() or "Unnamed Calendar"

        # Get parent account information
        parent_uid = source.get_parent()
        if parent_uid:
            parent_source = registry.ref_source(parent_uid)
            if parent_source:
                account_name = parent_source.get_display_name() or ""
            else:
                account_name = ""
        else:
            account_name = ""

        return (display_name, account_name, calendar_uid)
    except Exception as e:
        return (f"Error: {e}", "", calendar_uid)


class EDSCalendarClient:
    """Wrapper for Evolution Data Server calendar operations."""

    def __init__(self, registry: EDataServer.SourceRegistry, calendar_uid: str):
        self.registry = registry
        self.calendar_uid = calendar_uid
        self.client: Optional[ECal.Client] = None

    def connect(self, timeout: int = 10):
        """Connect to the specified calendar in EDS."""
        source = self.registry.ref_source(self.calendar_uid)
        if not source:
            raise CalendarSyncError(
                f"Calendar with UID '{self.calendar_uid}' not found in EDS"
            )

        try:
            self.client = ECal.Client.connect_sync(
                source,
                ECal.ClientSourceType.EVENTS,
                timeout,
                None
            )
        except GLib.Error as e:
            raise CalendarSyncError(
                f"Failed to connect to calendar {self.calendar_uid}: {e.message}"
            )

    def get_all_events(self) -> list:
        """Retrieve all events from the calendar."""
        if not self.client:
            raise CalendarSyncError("Client not connected")

        try:
            # "#t" (boolean true) is the correct sexp for "all events".
            # An empty string "" is invalid on newer EDS versions and causes
            # the calendar factory to block for the full sync timeout.
            _, objects = self.client.get_object_list_sync("#t", None)
            return objects
        except GLib.Error as e:
            raise CalendarSyncError(f"Failed to fetch events: {e.message}")

    def create_event(self, component: ICalGLib.Component) -> Optional[str]:
        """Create a new event in the calendar."""
        if not self.client:
            raise CalendarSyncError("Client not connected")

        success, out_uid = self.client.create_object_sync(
            component,
            ECal.OperationFlags.NONE,
            None
        )
        if not success:
            raise CalendarSyncError("Failed to create event")
        return out_uid

    def modify_event(self, component: ICalGLib.Component):
        """Modify an existing event in the calendar."""
        if not self.client:
            raise CalendarSyncError("Client not connected")

        success = self.client.modify_object_sync(
            component,
            ECal.ObjModType.THIS,
            ECal.OperationFlags.NONE,
            None
        )
        if not success:
            raise CalendarSyncError("Failed to modify event")

    def remove_event(self, uid: str):
        """Remove an event from the calendar."""
        if not self.client:
            raise CalendarSyncError("Client not connected")

        success = self.client.remove_object_sync(
            uid,
            None,  # rid (recurrence-id)
            ECal.ObjModType.THIS,
            ECal.OperationFlags.NONE,
            None  # cancellable
        )
        if not success:
            raise CalendarSyncError(f"Failed to remove event {uid}")

    def get_event(self, uid: str) -> Optional[ICalGLib.Component]:
        """Retrieve a single event by UID."""
        if not self.client:
            raise CalendarSyncError("Client not connected")

        try:
            success, icalcomp = self.client.get_object_sync(uid, None, None)
            if success and icalcomp:
                # Handle both string and Component returns
                if isinstance(icalcomp, str):
                    return ICalGLib.Component.new_from_string(icalcomp)
                else:
                    return icalcomp
        except GLib.Error:
            pass
        return None
