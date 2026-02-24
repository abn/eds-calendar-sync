"""
In-memory fake calendar client for testing.

Duck-type-compatible stand-in for EDSCalendarClient.  No EDS daemon or network
connection is required — events are kept in a plain dict keyed by UID.
"""

import gi

gi.require_version("ICalGLib", "3.0")
from gi.repository import ICalGLib


class FakeCalendarClient:
    """In-memory stub that satisfies the EDSCalendarClient duck-type contract."""

    def __init__(self, initial_events: dict[str, str] | None = None):
        # uid → ical_str  (VEVENT strings)
        self._events: dict[str, str] = dict(initial_events or {})
        self.creates: list[str] = []
        self.modifies: list[str] = []
        self.removes: list[str] = []

    # ------------------------------------------------------------------ #
    # Internal helpers                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _uid_from_component(comp: ICalGLib.Component) -> str | None:
        """Extract the UID from a VEVENT or VCALENDAR component."""
        if comp.isa() == ICalGLib.ComponentKind.VCALENDAR_COMPONENT:
            vevent = comp.get_first_component(ICalGLib.ComponentKind.VEVENT_COMPONENT)
            return vevent.get_uid() if vevent else None
        return comp.get_uid()

    # ------------------------------------------------------------------ #
    # EDSCalendarClient interface                                           #
    # ------------------------------------------------------------------ #

    def get_all_events(self) -> list:
        """Return all stored events as iCal strings (parse_component handles strings)."""
        return list(self._events.values())

    def create_event(self, component: ICalGLib.Component) -> str | None:
        """Store component and return its UID (simulating the server-assigned UID)."""
        uid = self._uid_from_component(component)
        ical_str = component.as_ical_string()
        if uid:
            self._events[uid] = ical_str
        self.creates.append(uid)
        return uid

    def modify_event(self, component: ICalGLib.Component):
        """Overwrite the stored iCal for the component's UID."""
        uid = self._uid_from_component(component)
        ical_str = component.as_ical_string()
        if uid:
            self._events[uid] = ical_str
        self.modifies.append(uid)

    def remove_event(self, uid: str):
        """Delete the event with the given UID."""
        self._events.pop(uid, None)
        self.removes.append(uid)

    def get_event(self, uid: str) -> ICalGLib.Component | None:
        """Return the stored event as an ICalGLib.Component, or None if not found."""
        ical_str = self._events.get(uid)
        if ical_str is None:
            return None
        return ICalGLib.Component.new_from_string(ical_str)

    # ------------------------------------------------------------------ #
    # Test helpers                                                          #
    # ------------------------------------------------------------------ #

    @property
    def event_count(self) -> int:
        return len(self._events)

    def has_uid(self, uid: str) -> bool:
        return uid in self._events

    def reset_counters(self):
        """Clear the create/modify/remove lists between sync runs."""
        self.creates.clear()
        self.modifies.clear()
        self.removes.clear()
