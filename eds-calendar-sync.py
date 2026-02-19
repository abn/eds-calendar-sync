#!/usr/bin/env python3
"""
EDS Calendar Synchronizer

Synchronizes events between calendars via Evolution Data Server,
stripping sensitive information and maintaining privacy while blocking availability.

Supports:
- Bidirectional sync (default) - syncs both directions
- One-way sync (--only-to-personal or --only-to-work)

Usage:
    # Bidirectional sync (default)
    eds-calendar-sync.py --work-calendar WORK_UID --personal-calendar PERSONAL_UID

    # One-way sync: work → personal only
    eds-calendar-sync.py --work-calendar WORK_UID --personal-calendar PERSONAL_UID --only-to-personal

    # One-way sync: personal → work only
    eds-calendar-sync.py --work-calendar WORK_UID --personal-calendar PERSONAL_UID --only-to-work

    # Refresh: remove synced events and resync (preserves non-synced events)
    eds-calendar-sync.py --work-calendar WORK_UID --personal-calendar PERSONAL_UID --refresh

    # Clear: remove all synced events
    eds-calendar-sync.py --work-calendar WORK_UID --personal-calendar PERSONAL_UID --clear

    # Use config file
    eds-calendar-sync.py --config ~/.config/eds-calendar-sync.conf
"""

import sys
import os
import sqlite3
import hashlib
import uuid
import argparse
import logging
from pathlib import Path
from typing import Optional, Dict, Set, Tuple
from dataclasses import dataclass
from configparser import ConfigParser

import gi
gi.require_version('EDataServer', '1.2')
gi.require_version('ECal', '2.0')
gi.require_version('ICalGLib', '3.0')

from gi.repository import EDataServer, ECal, ICalGLib, GLib

# Constants
DEFAULT_STATE_DB = Path.home() / ".local/share/eds-calendar-sync-state.db"
DEFAULT_CONFIG = Path.home() / ".config/eds-calendar-sync.conf"

@dataclass
class SyncConfig:
    """Configuration for calendar sync operation."""
    work_calendar_id: str
    personal_calendar_id: str
    state_db_path: Path
    dry_run: bool = False
    refresh: bool = False
    verbose: bool = False
    sync_direction: str = 'both'  # 'both', 'to-personal', 'to-work'
    clear: bool = False
    yes: bool = False  # Auto-confirm without prompting

@dataclass
class SyncStats:
    """Statistics for sync operation."""
    added: int = 0
    modified: int = 0
    deleted: int = 0
    errors: int = 0


class CalendarSyncError(Exception):
    """Base exception for calendar sync errors."""
    pass


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


class StateDatabase:
    """Manages SQLite state database for sync tracking."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def connect(self):
        """Initialize and connect to the state database."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row  # Enable column access by name
        self._init_schema()

    def _init_schema(self):
        """Create the sync_state table if it doesn't exist."""
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS sync_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_uid TEXT NOT NULL,
                target_uid TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                target_hash TEXT NOT NULL,
                origin TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                last_sync_at INTEGER NOT NULL,
                UNIQUE(source_uid, target_uid)
            )
        ''')
        self.conn.commit()

    def get_all_state(self) -> Dict[str, Dict[str, str]]:
        """Retrieve all sync state records (one-way compatibility)."""
        cursor = self.conn.execute(
            "SELECT source_uid, target_uid, source_hash FROM sync_state"
        )
        return {
            row[0]: {'target_uid': row[1], 'hash': row[2]}
            for row in cursor.fetchall()
        }

    def get_all_state_bidirectional(self) -> list:
        """Retrieve all sync state records for bidirectional sync."""
        cursor = self.conn.execute('''
            SELECT id, source_uid, target_uid, source_hash, target_hash, origin, created_at, last_sync_at
            FROM sync_state
        ''')
        return cursor.fetchall()

    def get_by_source_uid(self, source_uid: str) -> Optional[sqlite3.Row]:
        """Get state record by source UID."""
        cursor = self.conn.execute(
            "SELECT * FROM sync_state WHERE source_uid = ? LIMIT 1",
            (source_uid,)
        )
        return cursor.fetchone()

    def get_by_target_uid(self, target_uid: str) -> Optional[sqlite3.Row]:
        """Get state record by target UID."""
        cursor = self.conn.execute(
            "SELECT * FROM sync_state WHERE target_uid = ? LIMIT 1",
            (target_uid,)
        )
        return cursor.fetchone()

    def insert(self, source_uid: str, target_uid: str, content_hash: str):
        """Insert a new sync state record (one-way compatibility)."""
        import time
        timestamp = int(time.time())
        self.conn.execute('''
            INSERT INTO sync_state
            (source_uid, target_uid, source_hash, target_hash, origin, created_at, last_sync_at)
            VALUES (?, ?, ?, ?, 'source', ?, ?)
        ''', (source_uid, target_uid, content_hash, content_hash, timestamp, timestamp))

    def insert_bidirectional(self, source_uid: str, target_uid: str,
                            source_hash: str, target_hash: str, origin: str):
        """Insert new bidirectional sync record."""
        import time
        timestamp = int(time.time())
        self.conn.execute('''
            INSERT INTO sync_state
            (source_uid, target_uid, source_hash, target_hash, origin, created_at, last_sync_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (source_uid, target_uid, source_hash, target_hash, origin, timestamp, timestamp))

    def update_hash(self, source_uid: str, content_hash: str):
        """Update the hash for an existing record (one-way compatibility)."""
        import time
        self.conn.execute(
            "UPDATE sync_state SET source_hash = ?, target_hash = ?, last_sync_at = ? WHERE source_uid = ?",
            (content_hash, content_hash, int(time.time()), source_uid)
        )

    def update_hashes(self, source_uid: str, target_uid: str, source_hash: str, target_hash: str):
        """Update both hashes after successful sync."""
        import time
        self.conn.execute('''
            UPDATE sync_state
            SET source_hash = ?, target_hash = ?, last_sync_at = ?
            WHERE source_uid = ? AND target_uid = ?
        ''', (source_hash, target_hash, int(time.time()), source_uid, target_uid))

    def delete(self, source_uid: str):
        """Delete a sync state record (one-way compatibility)."""
        self.conn.execute(
            "DELETE FROM sync_state WHERE source_uid = ?",
            (source_uid,)
        )

    def delete_by_pair(self, source_uid: str, target_uid: str):
        """Delete by the (source_uid, target_uid) pair."""
        self.conn.execute(
            "DELETE FROM sync_state WHERE source_uid = ? AND target_uid = ?",
            (source_uid, target_uid)
        )

    def clear_all(self):
        """Remove all state records (for refresh)."""
        self.conn.execute("DELETE FROM sync_state")

    def commit(self):
        """Commit pending transactions."""
        if self.conn:
            self.conn.commit()

    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None


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
    def sanitize(cls, ical_string: str, new_uid: str, mode: str = 'normal') -> ICalGLib.Component:
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
            if mode == 'busy':
                cls._remove_all_properties(event, ICalGLib.PropertyKind.SUMMARY_PROPERTY)
                event.add_property(ICalGLib.Property.new_summary("Busy"))

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


class CalendarSynchronizer:
    """Main synchronization engine."""

    def __init__(self, config: SyncConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.stats = SyncStats()

    def _setup_clients(self) -> Tuple[EDSCalendarClient, EDSCalendarClient]:
        """Initialize and connect to work and personal calendars."""
        self.logger.info("Connecting to Evolution Data Server...")
        registry = EDataServer.SourceRegistry.new_sync(None)

        work_client = EDSCalendarClient(registry, self.config.work_calendar_id)
        personal_client = EDSCalendarClient(registry, self.config.personal_calendar_id)

        work_client.connect()
        personal_client.connect()

        return work_client, personal_client

    def _compute_hash(self, ical_string: str) -> str:
        """
        Generate SHA256 hash of iCal content for change detection.

        Normalizes the content by removing volatile server-added properties
        to prevent false change detection.
        """
        # Parse the component
        comp = ICalGLib.Component.new_from_string(ical_string)

        # Properties that servers often add/modify and should be ignored for change detection
        volatile_props = [
            ICalGLib.PropertyKind.DTSTAMP_PROPERTY,      # Timestamp when event was created/modified
            ICalGLib.PropertyKind.LASTMODIFIED_PROPERTY,  # Last modification time
            ICalGLib.PropertyKind.CREATED_PROPERTY,       # Creation time
            ICalGLib.PropertyKind.SEQUENCE_PROPERTY,      # Sequence number for updates
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

        # Compute hash of normalized content
        normalized_ical = comp.as_ical_string()
        return hashlib.sha256(normalized_ical.encode('utf-8')).hexdigest()

    def _parse_component(self, obj) -> ICalGLib.Component:
        """Handle both string and native Component objects from EDS API."""
        if isinstance(obj, str):
            return ICalGLib.Component.new_from_string(obj)
        return obj

    def _has_valid_occurrences(self, comp: ICalGLib.Component) -> bool:
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
                    exdates.add(
                        f"{t.get_year():04d}{t.get_month():02d}{t.get_day():02d}"
                    )
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
            it = ICalGLib.RecurIterator.new(rule, dtstart)
            for _ in range(500):
                occ = it.next()
                if occ is None or occ.is_null_time():
                    break
                occ_key = (
                    f"{occ.get_year():04d}{occ.get_month():02d}{occ.get_day():02d}"
                )
                if occ_key not in exdates:
                    return True  # Found at least one valid occurrence
        except Exception:
            return True  # On any API error assume valid — don't silently skip

        return False  # Every expanded occurrence is in EXDATE

    def _is_event_cancelled(self, comp: ICalGLib.Component) -> bool:
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
            val = status_prop.get_value_as_string() or ''
            return val.strip().upper() == 'CANCELLED'

    def _is_free_time(self, comp: ICalGLib.Component) -> bool:
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
            val = transp_prop.get_value_as_string() or ''
            return val.strip().upper() == 'TRANSPARENT'

    def _perform_refresh(self, personal_client: EDSCalendarClient, state_db: StateDatabase):
        """Delete only synced events we created, leaving other events untouched."""
        self.logger.warning("REFRESH MODE: Removing synced events and clearing state...")

        # Get all events we've created (tracked in state DB)
        state = state_db.get_all_state()
        personal_uids_to_delete = [s['target_uid'] for s in state.values()]

        # If state DB is empty, fall back to metadata scanning
        if len(personal_uids_to_delete) == 0:
            self.logger.info("State database empty, scanning personal calendar for managed events...")
            personal_events = personal_client.get_all_events()
            for obj in personal_events:
                comp = self._parse_component(obj)
                if EventSanitizer.is_managed_event(comp):
                    personal_uids_to_delete.append(comp.get_uid())

            if len(personal_uids_to_delete) > 0:
                self.logger.info(f"Found {len(personal_uids_to_delete)} managed events via metadata scan")
            else:
                self.logger.info("No managed events found - calendars are clean")

        if self.config.dry_run:
            self.logger.info(f"[DRY RUN] Would delete {len(personal_uids_to_delete)} synced events from personal calendar")
            self.logger.info("[DRY RUN] Would clear state database")
            for uid in personal_uids_to_delete:
                self.logger.debug(f"[DRY RUN] Would delete: {uid}")
            return

        # Remove only events WE created (in state DB)
        deleted_count = 0
        for personal_uid in personal_uids_to_delete:
            try:
                personal_client.remove_event(personal_uid)
                deleted_count += 1
                self.logger.debug(f"Deleted synced event: {personal_uid}")
            except (GLib.Error, CalendarSyncError) as e:
                self.logger.debug(f"Failed to remove {personal_uid}: {e}")

        # Clear state database
        state_db.clear_all()
        state_db.commit()

        self.logger.info(f"Refresh complete: Removed {deleted_count} synced events (other events preserved)")

    def _perform_refresh_two_way(
        self,
        work_client: EDSCalendarClient,
        personal_client: EDSCalendarClient,
        state_db: StateDatabase
    ):
        """Delete only synced events we created in both calendars, leaving other events untouched."""
        self.logger.warning("REFRESH MODE (TWO-WAY): Removing synced events from both calendars...")

        # Get all sync pairs
        state_records = state_db.get_all_state_bidirectional()

        work_uids_to_delete = []
        personal_uids_to_delete = []

        for record in state_records:
            work_uid = record['source_uid']
            personal_uid = record['target_uid']
            origin = record['origin']

            if origin == 'source':  # Work→Personal sync (we created event in personal)
                personal_uids_to_delete.append(personal_uid)
            elif origin == 'target':  # Personal→Work sync (we created event in work)
                work_uids_to_delete.append(work_uid)

        # If state DB is empty, fall back to metadata scanning
        if len(work_uids_to_delete) == 0 and len(personal_uids_to_delete) == 0:
            self.logger.info("State database empty, scanning calendars for managed events...")

            # Scan work calendar
            work_events = work_client.get_all_events()
            for obj in work_events:
                comp = self._parse_component(obj)
                if EventSanitizer.is_managed_event(comp):
                    work_uids_to_delete.append(comp.get_uid())

            # Scan personal calendar
            personal_events = personal_client.get_all_events()
            for obj in personal_events:
                comp = self._parse_component(obj)
                if EventSanitizer.is_managed_event(comp):
                    personal_uids_to_delete.append(comp.get_uid())

            if len(work_uids_to_delete) > 0 or len(personal_uids_to_delete) > 0:
                self.logger.info(
                    f"Found {len(work_uids_to_delete)} work events, "
                    f"{len(personal_uids_to_delete)} personal events via metadata scan"
                )
            else:
                self.logger.info("No managed events found - calendars are clean")

        if self.config.dry_run:
            self.logger.info(
                f"[DRY RUN] Would delete {len(work_uids_to_delete)} synced events from work calendar"
            )
            self.logger.info(
                f"[DRY RUN] Would delete {len(personal_uids_to_delete)} synced events from personal calendar"
            )
            self.logger.info("[DRY RUN] Would clear state database")
            return

        # Remove events WE created in work calendar
        work_deleted = 0
        for work_uid in work_uids_to_delete:
            try:
                work_client.remove_event(work_uid)
                work_deleted += 1
                self.logger.debug(f"Deleted synced event from work: {work_uid}")
            except (GLib.Error, CalendarSyncError) as e:
                self.logger.debug(f"Failed to remove work event {work_uid}: {e}")

        # Remove events WE created in personal calendar
        personal_deleted = 0
        for personal_uid in personal_uids_to_delete:
            try:
                personal_client.remove_event(personal_uid)
                personal_deleted += 1
                self.logger.debug(f"Deleted synced event from personal: {personal_uid}")
            except (GLib.Error, CalendarSyncError) as e:
                self.logger.debug(f"Failed to remove personal event {personal_uid}: {e}")

        # Clear state database
        state_db.clear_all()
        state_db.commit()

        self.logger.info(
            f"Refresh complete: Removed {work_deleted} work events, "
            f"{personal_deleted} personal events (other events preserved)"
        )

    def _perform_clear(
        self,
        work_client: EDSCalendarClient,
        personal_client: EDSCalendarClient,
        state_db: StateDatabase
    ):
        """Remove all synced events we created by checking metadata."""
        self.logger.warning("CLEAR MODE: Removing all synced events created by this tool...")

        work_managed = []
        personal_managed = []

        # Scan calendars based on sync direction
        if self.config.sync_direction in ('both', 'to-work'):
            # We create events in work calendar when syncing to work
            self.logger.info("Scanning work calendar for managed events...")
            work_events = work_client.get_all_events()
            for obj in work_events:
                comp = self._parse_component(obj)
                if EventSanitizer.is_managed_event(comp):
                    work_managed.append(comp.get_uid())

        if self.config.sync_direction in ('both', 'to-personal'):
            # We create events in personal calendar when syncing to personal
            self.logger.info("Scanning personal calendar for managed events...")
            personal_events = personal_client.get_all_events()
            for obj in personal_events:
                comp = self._parse_component(obj)
                if EventSanitizer.is_managed_event(comp):
                    personal_managed.append(comp.get_uid())

        total_to_delete = len(work_managed) + len(personal_managed)

        # Report findings based on what we scanned
        if total_to_delete > 0:
            parts = []
            if self.config.sync_direction in ('both', 'to-work') and len(work_managed) > 0:
                parts.append(f"{len(work_managed)} in work calendar")
            if self.config.sync_direction in ('both', 'to-personal') and len(personal_managed) > 0:
                parts.append(f"{len(personal_managed)} in personal calendar")
            self.logger.info(f"Found {' and '.join(parts)} managed events")
        else:
            self.logger.info("No managed events found")

        if self.config.dry_run:
            self.logger.info(f"[DRY RUN] Would delete {total_to_delete} total managed events")
            self.logger.info("[DRY RUN] Would clear state database")
            if self.config.verbose:
                for uid in work_managed:
                    self.logger.debug(f"[DRY RUN] Would delete work event: {uid}")
                for uid in personal_managed:
                    self.logger.debug(f"[DRY RUN] Would delete personal event: {uid}")
            return

        # Delete managed events from work calendar (if applicable)
        work_deleted = 0
        if self.config.sync_direction in ('both', 'to-work'):
            for uid in work_managed:
                try:
                    work_client.remove_event(uid)
                    work_deleted += 1
                    self.logger.debug(f"Deleted work event: {uid}")
                except (GLib.Error, CalendarSyncError) as e:
                    self.logger.error(f"Failed to delete work event {uid}: {e}")
                    self.stats.errors += 1

        # Delete managed events from personal calendar (if applicable)
        personal_deleted = 0
        if self.config.sync_direction in ('both', 'to-personal'):
            for uid in personal_managed:
                try:
                    personal_client.remove_event(uid)
                    personal_deleted += 1
                    self.logger.debug(f"Deleted personal event: {uid}")
                except (GLib.Error, CalendarSyncError) as e:
                    self.logger.error(f"Failed to delete personal event {uid}: {e}")
                    self.stats.errors += 1

        # Clear state database
        state_db.clear_all()
        state_db.commit()

        self.logger.info(
            f"Clear complete: Removed {work_deleted} work events, "
            f"{personal_deleted} personal events"
        )
        self.stats.deleted = work_deleted + personal_deleted

    def _process_creates(
        self,
        work_uid: str,
        ical_str: str,
        obj_hash: str,
        personal_client: EDSCalendarClient,
        state_db: StateDatabase
    ):
        """Handle creation of new events in personal calendar."""
        personal_uid = str(uuid.uuid4())

        if self.config.dry_run:
            self.logger.info(
                f"[DRY RUN] Would CREATE event: {work_uid} -> {personal_uid}"
            )
            self.stats.added += 1
            return

        try:
            sanitized = EventSanitizer.sanitize(ical_str, personal_uid)

            # Debug: Show sanitized output
            if self.config.verbose:
                sanitized_str = sanitized.as_ical_string()
                self.logger.debug(f"Sanitized iCal:\n{sanitized_str}")

            # Create event and get the ACTUAL UID assigned by the server
            # (Microsoft 365 will rewrite the UID, so we must use what's returned)
            actual_personal_uid = personal_client.create_event(sanitized)

            # Use the actual UID if returned, otherwise fall back to our generated one
            if actual_personal_uid:
                personal_uid = actual_personal_uid
                self.logger.debug(f"Server assigned UID: {personal_uid}")

            # Fetch the event back to get the actual stored version and compute both hashes
            work_hash = self._compute_hash(ical_str)
            created_personal = personal_client.get_event(personal_uid)
            if created_personal:
                personal_hash = self._compute_hash(created_personal.as_ical_string())
            else:
                # Fallback if fetch fails
                personal_hash = self._compute_hash(sanitized.as_ical_string())

            state_db.insert_bidirectional(work_uid, personal_uid, work_hash, personal_hash, 'source')
            self.stats.added += 1
            self.logger.debug(f"Created event {work_uid} as {personal_uid}")
        except (GLib.Error, CalendarSyncError) as e:
            if 'sanitized' in locals():
                self.logger.warning(
                    f"Sanitized iCal for failed event {work_uid}:\n"
                    f"{sanitized.as_ical_string()}"
                )
            self.logger.error(f"Failed to create event {work_uid}: {e}")
            self.stats.errors += 1

    def _process_updates(
        self,
        work_uid: str,
        ical_str: str,
        obj_hash: str,
        personal_uid: str,
        personal_client: EDSCalendarClient,
        state_db: StateDatabase
    ):
        """Handle updates to existing events in personal calendar."""
        if self.config.dry_run:
            self.logger.info(
                f"[DRY RUN] Would UPDATE event: {work_uid} (personal: {personal_uid})"
            )
            self.stats.modified += 1
            return

        try:
            sanitized = EventSanitizer.sanitize(ical_str, personal_uid)
            personal_client.modify_event(sanitized)

            # Fetch the event back to get the actual stored version
            work_hash = self._compute_hash(ical_str)
            updated_personal = personal_client.get_event(personal_uid)
            if updated_personal:
                personal_hash = self._compute_hash(updated_personal.as_ical_string())
            else:
                # Fallback if fetch fails
                personal_hash = self._compute_hash(sanitized.as_ical_string())

            state_db.update_hashes(work_uid, personal_uid, work_hash, personal_hash)
            self.stats.modified += 1
            self.logger.debug(f"Updated event {work_uid}")
        except (GLib.Error, CalendarSyncError) as e:
            # If modify fails, try recreating
            self.logger.warning(f"Modify failed for {work_uid}, attempting recreate: {e}")
            try:
                # First delete the old event
                try:
                    personal_client.remove_event(personal_uid)
                except:
                    pass  # May already be gone

                # Create new with fresh UUID (will be rewritten by server)
                new_uid = str(uuid.uuid4())
                sanitized = EventSanitizer.sanitize(ical_str, new_uid)
                actual_uid = personal_client.create_event(sanitized)

                # Update state with new UID if returned
                if actual_uid:
                    new_uid = actual_uid

                # Fetch back and compute both hashes
                work_hash = self._compute_hash(ical_str)
                created_personal = personal_client.get_event(new_uid)
                if created_personal:
                    personal_hash = self._compute_hash(created_personal.as_ical_string())
                else:
                    personal_hash = self._compute_hash(sanitized.as_ical_string())

                # Update state DB with new personal UID
                state_db.delete(work_uid)
                state_db.insert_bidirectional(work_uid, new_uid, work_hash, personal_hash, 'source')
                self.stats.modified += 1
                self.logger.debug(f"Recreated event {work_uid} as {new_uid}")
            except (GLib.Error, CalendarSyncError) as e2:
                self.logger.error(f"Failed to update/recreate event {work_uid}: {e2}")
                self.stats.errors += 1

    def _process_deletions(
        self,
        state: Dict[str, Dict[str, str]],
        work_uids_seen: Set[str],
        personal_client: EDSCalendarClient,
        state_db: StateDatabase
    ):
        """Handle deletion of events removed from work calendar."""
        for work_uid in list(state.keys()):
            if work_uid not in work_uids_seen:
                personal_uid = state[work_uid]['target_uid']

                if self.config.dry_run:
                    self.logger.info(
                        f"[DRY RUN] Would DELETE event: {work_uid} (personal: {personal_uid})"
                    )
                    self.stats.deleted += 1
                    continue

                self.logger.debug(f"Attempting to delete personal event with UID: {personal_uid}")
                try:
                    personal_client.remove_event(personal_uid)
                    self.logger.debug(f"Successfully deleted event {work_uid} (personal: {personal_uid})")
                except (GLib.Error, CalendarSyncError) as e:
                    self.logger.error(f"Failed to delete {personal_uid}: {e}")
                    self.stats.errors += 1
                    continue

                state_db.delete(work_uid)
                self.stats.deleted += 1

    def _perform_refresh_to_work(self, work_client: EDSCalendarClient, state_db: StateDatabase):
        """Delete only synced events in work calendar, leaving other events untouched."""
        self.logger.warning("REFRESH MODE: Removing synced events from work calendar...")

        # Get all events we've created in work calendar (tracked in state DB)
        state = state_db.get_all_state()
        work_uids_to_delete = [s['target_uid'] for s in state.values()]

        # If state DB is empty, fall back to metadata scanning
        if len(work_uids_to_delete) == 0:
            self.logger.info("State database empty, scanning work calendar for managed events...")
            work_events = work_client.get_all_events()
            for obj in work_events:
                comp = self._parse_component(obj)
                if EventSanitizer.is_managed_event(comp):
                    work_uids_to_delete.append(comp.get_uid())

            if len(work_uids_to_delete) > 0:
                self.logger.info(f"Found {len(work_uids_to_delete)} managed events via metadata scan")
            else:
                self.logger.info("No managed events found - work calendar is clean")

        if self.config.dry_run:
            self.logger.info(f"[DRY RUN] Would delete {len(work_uids_to_delete)} synced events from work calendar")
            self.logger.info("[DRY RUN] Would clear state database")
            for uid in work_uids_to_delete:
                self.logger.debug(f"[DRY RUN] Would delete: {uid}")
            return

        # Remove only events WE created (in state DB)
        deleted_count = 0
        for work_uid in work_uids_to_delete:
            try:
                work_client.remove_event(work_uid)
                deleted_count += 1
                self.logger.debug(f"Deleted synced event from work: {work_uid}")
            except (GLib.Error, CalendarSyncError) as e:
                self.logger.debug(f"Failed to remove {work_uid}: {e}")

        # Clear state database
        state_db.clear_all()
        state_db.commit()

        self.logger.info(f"Refresh complete: Removed {deleted_count} synced events from work calendar")

    def _process_creates_to_work(
        self,
        personal_uid: str,
        ical_str: str,
        obj_hash: str,
        work_client: EDSCalendarClient,
        state_db: StateDatabase
    ):
        """Handle creation of new events in work calendar from personal."""
        work_uid = str(uuid.uuid4())

        if self.config.dry_run:
            self.logger.info(
                f"[DRY RUN] Would CREATE event: {personal_uid} -> {work_uid}"
            )
            self.stats.added += 1
            return

        try:
            # Use 'busy' mode sanitization for personal → work
            sanitized = EventSanitizer.sanitize(ical_str, work_uid, mode='busy')

            if self.config.verbose:
                sanitized_str = sanitized.as_ical_string()
                self.logger.debug(f"Sanitized iCal:\n{sanitized_str}")

            # Create event and get the ACTUAL UID assigned by the server
            actual_work_uid = work_client.create_event(sanitized)

            # Use the actual UID if returned, otherwise fall back to our generated one
            if actual_work_uid:
                work_uid = actual_work_uid
                self.logger.debug(f"Server assigned UID: {work_uid}")

            # Fetch the event back to get the actual stored version and compute both hashes
            personal_hash = self._compute_hash(ical_str)
            created_work = work_client.get_event(work_uid)
            if created_work:
                work_hash = self._compute_hash(created_work.as_ical_string())
            else:
                # Fallback if fetch fails
                work_hash = self._compute_hash(sanitized.as_ical_string())

            # Note: source=personal, target=work, origin='target' (originated from target/personal calendar)
            state_db.insert_bidirectional(personal_uid, work_uid, personal_hash, work_hash, 'target')
            self.stats.added += 1
            self.logger.debug(f"Created event {personal_uid} as {work_uid} in work calendar")
        except (GLib.Error, CalendarSyncError) as e:
            self.logger.error(f"Failed to create event {personal_uid}: {e}")
            self.stats.errors += 1

    def _process_updates_to_work(
        self,
        personal_uid: str,
        ical_str: str,
        obj_hash: str,
        work_uid: str,
        work_client: EDSCalendarClient,
        state_db: StateDatabase
    ):
        """Handle updates to existing events in work calendar from personal."""
        if self.config.dry_run:
            self.logger.info(
                f"[DRY RUN] Would UPDATE event: {personal_uid} (work: {work_uid})"
            )
            self.stats.modified += 1
            return

        try:
            # Use 'busy' mode sanitization for personal → work
            sanitized = EventSanitizer.sanitize(ical_str, work_uid, mode='busy')
            work_client.modify_event(sanitized)

            # Fetch the event back to get the actual stored version
            personal_hash = self._compute_hash(ical_str)
            updated_work = work_client.get_event(work_uid)
            if updated_work:
                work_hash = self._compute_hash(updated_work.as_ical_string())
            else:
                # Fallback if fetch fails
                work_hash = self._compute_hash(sanitized.as_ical_string())

            state_db.update_hashes(personal_uid, work_uid, personal_hash, work_hash)
            self.stats.modified += 1
            self.logger.debug(f"Updated event {personal_uid} in work calendar")
        except (GLib.Error, CalendarSyncError) as e:
            # If modify fails, try recreating
            self.logger.warning(f"Modify failed for {personal_uid}, attempting recreate: {e}")
            try:
                # First delete the old event
                try:
                    work_client.remove_event(work_uid)
                except:
                    pass  # May already be gone

                # Create new with fresh UUID (will be rewritten by server)
                new_uid = str(uuid.uuid4())
                sanitized = EventSanitizer.sanitize(ical_str, new_uid, mode='busy')
                actual_uid = work_client.create_event(sanitized)

                # Update state with new UID if returned
                if actual_uid:
                    new_uid = actual_uid

                # Fetch back and compute both hashes
                personal_hash = self._compute_hash(ical_str)
                created_work = work_client.get_event(new_uid)
                if created_work:
                    work_hash = self._compute_hash(created_work.as_ical_string())
                else:
                    work_hash = self._compute_hash(sanitized.as_ical_string())

                # Update state DB with new work UID
                state_db.delete(personal_uid)
                state_db.insert_bidirectional(personal_uid, new_uid, personal_hash, work_hash, 'target')
                self.stats.modified += 1
                self.logger.debug(f"Recreated event {personal_uid} as {new_uid} in work calendar")
            except (GLib.Error, CalendarSyncError) as e2:
                self.logger.error(f"Failed to update/recreate event {personal_uid}: {e2}")
                self.stats.errors += 1

    def _process_deletions_to_work(
        self,
        state: Dict[str, Dict[str, str]],
        personal_uids_seen: Set[str],
        work_client: EDSCalendarClient,
        state_db: StateDatabase
    ):
        """Handle deletion of events removed from personal calendar."""
        for personal_uid in list(state.keys()):
            if personal_uid not in personal_uids_seen:
                work_uid = state[personal_uid]['target_uid']

                if self.config.dry_run:
                    self.logger.info(
                        f"[DRY RUN] Would DELETE event: {personal_uid} (work: {work_uid})"
                    )
                    self.stats.deleted += 1
                    continue

                self.logger.debug(f"Attempting to delete work event with UID: {work_uid}")
                try:
                    work_client.remove_event(work_uid)
                    self.logger.debug(f"Successfully deleted event {personal_uid} (work: {work_uid})")
                except (GLib.Error, CalendarSyncError) as e:
                    self.logger.error(f"Failed to delete {work_uid}: {e}")
                    self.stats.errors += 1
                    continue

                state_db.delete(personal_uid)
                self.stats.deleted += 1

    def _run_one_way_to_work(self) -> SyncStats:
        """Execute one-way synchronization (personal → work)."""
        try:
            # Connect to calendars
            work_client, personal_client = self._setup_clients()

            # Open state database
            with StateDatabase(self.config.state_db_path) as state_db:
                # Handle refresh mode
                if self.config.refresh:
                    self._perform_refresh_to_work(work_client, state_db)

                # Load current state
                self.logger.info("Loading sync state...")
                state = state_db.get_all_state()

                # Fetch personal events (source)
                self.logger.info("Fetching personal events...")
                personal_events = personal_client.get_all_events()
                personal_uids_seen = set()

                # Process each personal event
                self.logger.info(f"Processing {len(personal_events)} personal events...")
                for obj in personal_events:
                    comp = self._parse_component(obj)
                    personal_uid = comp.get_uid()
                    personal_uids_seen.add(personal_uid)

                    ical_str = comp.as_ical_string()
                    obj_hash = self._compute_hash(ical_str)

                    if personal_uid not in state:
                        # CREATE in work calendar
                        self._process_creates_to_work(
                            personal_uid, ical_str, obj_hash, work_client, state_db
                        )
                    elif obj_hash != state[personal_uid]['hash']:
                        # UPDATE in work calendar
                        self._process_updates_to_work(
                            personal_uid, ical_str, obj_hash,
                            state[personal_uid]['target_uid'],
                            work_client, state_db
                        )

                # Process deletions
                self.logger.info("Checking for deletions...")
                self._process_deletions_to_work(
                    state, personal_uids_seen, work_client, state_db
                )

                # Commit changes
                if not self.config.dry_run:
                    state_db.commit()

        except CalendarSyncError as e:
            self.logger.error(f"Sync failed: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}", exc_info=True)
            raise

        return self.stats

    def run_two_way(self) -> SyncStats:
        """Execute bidirectional synchronization."""
        try:
            # Connect to calendars
            work_client, personal_client = self._setup_clients()

            # Open state database
            with StateDatabase(self.config.state_db_path) as state_db:
                # Handle refresh mode
                if self.config.refresh:
                    self._perform_refresh_two_way(work_client, personal_client, state_db)
                self.logger.info("Loading sync state...")
                state_records = state_db.get_all_state_bidirectional()

                # Fetch all events from both calendars
                self.logger.info("Fetching work events...")
                work_events_list = work_client.get_all_events()
                work_events = {}  # uid -> component
                for obj in work_events_list:
                    comp = self._parse_component(obj)
                    work_events[comp.get_uid()] = comp

                self.logger.info("Fetching personal events...")
                personal_events_list = personal_client.get_all_events()
                personal_events = {}  # uid -> component
                for obj in personal_events_list:
                    comp = self._parse_component(obj)
                    personal_events[comp.get_uid()] = comp

                self.logger.info(
                    f"Processing {len(work_events)} work events, "
                    f"{len(personal_events)} personal events, "
                    f"{len(state_records)} sync pairs..."
                )

                # Track which events we've processed
                work_uids_processed = set()
                personal_uids_processed = set()

                # Phase 1: Process existing sync pairs
                for state_record in state_records:
                    work_uid = state_record['source_uid']  # 'source' maps to 'work' in DB
                    personal_uid = state_record['target_uid']  # 'target' maps to 'personal' in DB

                    self._process_sync_pair(
                        state_record, work_events, personal_events,
                        work_client, personal_client, state_db
                    )

                    work_uids_processed.add(work_uid)
                    personal_uids_processed.add(personal_uid)

                # Phase 2: Process new work events (not yet synced)
                for work_uid, work_comp in work_events.items():
                    if work_uid not in work_uids_processed:
                        self._process_new_work_event(
                            work_uid, work_comp, personal_client, state_db
                        )

                # Phase 3: Process new personal events (not yet synced)
                for personal_uid, personal_comp in personal_events.items():
                    if personal_uid not in personal_uids_processed:
                        self._process_new_personal_event(
                            personal_uid, personal_comp, work_client, state_db
                        )

                # Commit changes
                if not self.config.dry_run:
                    state_db.commit()

        except CalendarSyncError as e:
            self.logger.error(f"Sync failed: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}", exc_info=True)
            raise

        return self.stats

    def _process_sync_pair(
        self,
        state_record: sqlite3.Row,
        work_events: Dict[str, ICalGLib.Component],
        personal_events: Dict[str, ICalGLib.Component],
        work_client: EDSCalendarClient,
        personal_client: EDSCalendarClient,
        state_db: StateDatabase
    ):
        """Process an existing sync pair (check for changes/deletions)."""
        work_uid = state_record['source_uid']  # DB uses 'source' for work
        personal_uid = state_record['target_uid']  # DB uses 'target' for personal
        origin = state_record['origin']
        stored_work_hash = state_record['source_hash']
        stored_personal_hash = state_record['target_hash']

        work_exists = work_uid in work_events
        personal_exists = personal_uid in personal_events

        # Handle deletions
        if not work_exists and not personal_exists:
            # Both deleted, just clean up state
            self.logger.debug(f"Both events deleted: {work_uid} <-> {personal_uid}")
            if not self.config.dry_run:
                state_db.delete_by_pair(work_uid, personal_uid)
            return

        if not work_exists:
            # Work event deleted
            if origin == 'source':  # DB uses 'source' for work origin
                # Work was authoritative, delete personal
                if self.config.dry_run:
                    self.logger.info(
                        f"[DRY RUN] [WORK→PERSONAL] Would DELETE: {personal_uid} (work deleted)"
                    )
                    self.stats.deleted += 1
                else:
                    try:
                        personal_client.remove_event(personal_uid)
                        self.logger.debug(f"Deleted personal event {personal_uid} (work deleted)")
                        self.stats.deleted += 1
                    except (GLib.Error, CalendarSyncError) as e:
                        self.logger.error(f"Failed to delete personal {personal_uid}: {e}")
                        self.stats.errors += 1

            # Clean up state
            if not self.config.dry_run:
                state_db.delete_by_pair(work_uid, personal_uid)
            return

        if not personal_exists:
            # Personal event deleted
            if origin == 'target':  # DB uses 'target' for personal origin
                # Personal was authoritative, delete work
                if self.config.dry_run:
                    self.logger.info(
                        f"[DRY RUN] [PERSONAL→WORK] Would DELETE: {work_uid} (personal deleted)"
                    )
                    self.stats.deleted += 1
                else:
                    try:
                        work_client.remove_event(work_uid)
                        self.logger.debug(f"Deleted work event {work_uid} (personal deleted)")
                        self.stats.deleted += 1
                    except (GLib.Error, CalendarSyncError) as e:
                        self.logger.error(f"Failed to delete work {work_uid}: {e}")
                        self.stats.errors += 1

            # Clean up state
            if not self.config.dry_run:
                state_db.delete_by_pair(work_uid, personal_uid)
            return

        # Both events exist - check for updates
        work_comp = work_events[work_uid]
        personal_comp = personal_events[personal_uid]

        work_ical = work_comp.as_ical_string()
        personal_ical = personal_comp.as_ical_string()

        current_work_hash = self._compute_hash(work_ical)
        current_personal_hash = self._compute_hash(personal_ical)

        # Debug: Log hash mismatches
        if self.config.verbose:
            if current_work_hash != stored_work_hash:
                self.logger.debug(f"Work hash mismatch for {work_uid}")
                self.logger.debug(f"  Stored: {stored_work_hash}")
                self.logger.debug(f"  Current: {current_work_hash}")
            if current_personal_hash != stored_personal_hash:
                self.logger.debug(f"Personal hash mismatch for {personal_uid}")
                self.logger.debug(f"  Stored: {stored_personal_hash}")
                self.logger.debug(f"  Current: {current_personal_hash}")

        if origin == 'source':  # DB uses 'source' for work origin
            # Work is authoritative - sync work→personal if EITHER changed
            # This ensures manual edits to personal are overwritten
            work_changed = current_work_hash != stored_work_hash
            personal_changed = current_personal_hash != stored_personal_hash

            if work_changed or personal_changed:
                reason = []
                if work_changed:
                    reason.append("work changed")
                if personal_changed:
                    reason.append("personal manually edited")
                reason_str = ", ".join(reason)

                if self.config.dry_run:
                    self.logger.info(
                        f"[DRY RUN] [WORK→PERSONAL] Would UPDATE: {work_uid} -> {personal_uid} ({reason_str})"
                    )
                    self.stats.modified += 1
                else:
                    try:
                        sanitized = EventSanitizer.sanitize(work_ical, personal_uid, mode='normal')
                        personal_client.modify_event(sanitized)

                        # Fetch the event back to get the actual stored version
                        # (server may have added/modified properties)
                        updated_personal = personal_client.get_event(personal_uid)
                        if updated_personal:
                            new_personal_ical = updated_personal.as_ical_string()
                            new_personal_hash = self._compute_hash(new_personal_ical)
                        else:
                            # Fallback if fetch fails
                            new_personal_ical = sanitized.as_ical_string()
                            new_personal_hash = self._compute_hash(new_personal_ical)

                        state_db.update_hashes(work_uid, personal_uid, current_work_hash, new_personal_hash)

                        self.stats.modified += 1
                        self.logger.debug(f"Updated personal {personal_uid} from work {work_uid} ({reason_str})")
                    except (GLib.Error, CalendarSyncError) as e:
                        self.logger.error(f"Failed to update personal {personal_uid}: {e}")
                        self.stats.errors += 1

        elif origin == 'target':  # DB uses 'target' for personal origin
            # Personal is authoritative - sync personal→work if EITHER changed
            # This ensures manual edits to work are overwritten
            personal_changed = current_personal_hash != stored_personal_hash
            work_changed = current_work_hash != stored_work_hash

            if personal_changed or work_changed:
                reason = []
                if personal_changed:
                    reason.append("personal changed")
                if work_changed:
                    reason.append("work manually edited")
                reason_str = ", ".join(reason)

                if self.config.dry_run:
                    self.logger.info(
                        f"[DRY RUN] [PERSONAL→WORK] Would UPDATE: {personal_uid} -> {work_uid} ({reason_str})"
                    )
                    self.stats.modified += 1
                else:
                    try:
                        sanitized = EventSanitizer.sanitize(personal_ical, work_uid, mode='busy')
                        work_client.modify_event(sanitized)

                        # Fetch the event back to get the actual stored version
                        # (server may have added/modified properties)
                        updated_work = work_client.get_event(work_uid)
                        if updated_work:
                            new_work_ical = updated_work.as_ical_string()
                            new_work_hash = self._compute_hash(new_work_ical)
                        else:
                            # Fallback if fetch fails
                            new_work_ical = sanitized.as_ical_string()
                            new_work_hash = self._compute_hash(new_work_ical)

                        state_db.update_hashes(work_uid, personal_uid, new_work_hash, current_personal_hash)

                        self.stats.modified += 1
                        self.logger.debug(f"Updated work {work_uid} from personal {personal_uid} ({reason_str})")
                    except (GLib.Error, CalendarSyncError) as e:
                        self.logger.error(f"Failed to update work {work_uid}: {e}")
                        self.stats.errors += 1

    def _process_new_work_event(
        self,
        work_uid: str,
        work_comp: ICalGLib.Component,
        personal_client: EDSCalendarClient,
        state_db: StateDatabase
    ):
        """Handle creation of new event in personal calendar from work."""
        personal_uid = str(uuid.uuid4())
        work_ical = work_comp.as_ical_string()

        if self.config.dry_run:
            self.logger.info(
                f"[DRY RUN] [WORK→PERSONAL] Would CREATE: {work_uid} -> {personal_uid}"
            )
            self.stats.added += 1
            return

        try:
            sanitized = EventSanitizer.sanitize(work_ical, personal_uid, mode='normal')

            if self.config.verbose:
                self.logger.debug(f"Sanitized iCal:\n{sanitized.as_ical_string()}")

            # Create event and get actual UID
            actual_personal_uid = personal_client.create_event(sanitized)
            if actual_personal_uid:
                personal_uid = actual_personal_uid
                self.logger.debug(f"Server assigned UID: {personal_uid}")

            # Fetch the event back to get the actual stored version
            work_hash = self._compute_hash(work_ical)
            created_personal = personal_client.get_event(personal_uid)
            if created_personal:
                personal_hash = self._compute_hash(created_personal.as_ical_string())
            else:
                # Fallback if fetch fails
                personal_hash = self._compute_hash(sanitized.as_ical_string())

            state_db.insert_bidirectional(work_uid, personal_uid, work_hash, personal_hash, 'source')
            self.stats.added += 1
            self.logger.debug(f"Created personal event {personal_uid} from work {work_uid}")
        except (GLib.Error, CalendarSyncError) as e:
            self.logger.error(f"Failed to create personal event from {work_uid}: {e}")
            self.stats.errors += 1

    def _process_new_personal_event(
        self,
        personal_uid: str,
        personal_comp: ICalGLib.Component,
        work_client: EDSCalendarClient,
        state_db: StateDatabase
    ):
        """Handle creation of new event in work calendar from personal."""
        work_uid = str(uuid.uuid4())
        personal_ical = personal_comp.as_ical_string()

        if self.config.dry_run:
            self.logger.info(
                f"[DRY RUN] [PERSONAL→WORK] Would CREATE: {personal_uid} -> {work_uid}"
            )
            self.stats.added += 1
            return

        try:
            sanitized = EventSanitizer.sanitize(personal_ical, work_uid, mode='busy')

            if self.config.verbose:
                self.logger.debug(f"Sanitized iCal (busy mode):\n{sanitized.as_ical_string()}")

            # Create event and get actual UID
            actual_work_uid = work_client.create_event(sanitized)
            if actual_work_uid:
                work_uid = actual_work_uid
                self.logger.debug(f"Server assigned UID: {work_uid}")

            # Fetch the event back to get the actual stored version
            personal_hash = self._compute_hash(personal_ical)
            created_work = work_client.get_event(work_uid)
            if created_work:
                work_hash = self._compute_hash(created_work.as_ical_string())
            else:
                # Fallback if fetch fails
                work_hash = self._compute_hash(sanitized.as_ical_string())

            state_db.insert_bidirectional(work_uid, personal_uid, work_hash, personal_hash, 'target')
            self.stats.added += 1
            self.logger.debug(f"Created work event {work_uid} from personal {personal_uid}")
        except (GLib.Error, CalendarSyncError) as e:
            self.logger.error(f"Failed to create work event from {personal_uid}: {e}")
            self.stats.errors += 1

    def run(self) -> SyncStats:
        """Execute the synchronization process."""
        # Handle --clear mode (removes all managed events)
        if self.config.clear:
            try:
                work_client, personal_client = self._setup_clients()
                with StateDatabase(self.config.state_db_path) as state_db:
                    self._perform_clear(work_client, personal_client, state_db)
                return self.stats
            except CalendarSyncError as e:
                self.logger.error(f"Clear failed: {e}")
                raise
            except Exception as e:
                self.logger.error(f"Unexpected error: {e}", exc_info=True)
                raise

        # Dispatch based on sync direction
        if self.config.sync_direction == 'both':
            return self.run_two_way()
        elif self.config.sync_direction == 'to-personal':
            return self._run_one_way_to_personal()
        elif self.config.sync_direction == 'to-work':
            return self._run_one_way_to_work()
        else:
            raise CalendarSyncError(f"Invalid sync direction: {self.config.sync_direction}")

    def _run_one_way_to_personal(self) -> SyncStats:
        """Execute one-way synchronization (work → personal)."""
        try:
            # Connect to calendars
            work_client, personal_client = self._setup_clients()

            # Open state database
            with StateDatabase(self.config.state_db_path) as state_db:
                # Handle refresh mode
                if self.config.refresh:
                    self._perform_refresh(personal_client, state_db)

                # Load current state
                self.logger.info("Loading sync state...")
                state = state_db.get_all_state()

                # Fetch work events
                self.logger.info("Fetching work events...")
                work_events = work_client.get_all_events()
                work_uids_seen = set()

                # Process each work event
                self.logger.info(f"Processing {len(work_events)} work events...")
                for obj in work_events:
                    comp = self._parse_component(obj)
                    base_uid = comp.get_uid()

                    # Skip cancelled events entirely.  They no longer block time
                    # and Exchange rejects creating them via CreateItem.  Their
                    # absence from work_uids_seen will cause _process_deletions
                    # to remove any previously synced copy from the personal
                    # calendar.
                    if self._is_event_cancelled(comp):
                        self.logger.debug(f"Skipping cancelled event: {base_uid}")
                        continue

                    # Skip transparent (free-time) events — they do not block the
                    # user's time so they should not appear as busy in the personal
                    # calendar.  Exchange marks declined meetings as transparent.
                    if self._is_free_time(comp):
                        self.logger.debug(f"Skipping transparent (free-time) event: {base_uid}")
                        continue

                    # Skip recurring events where every occurrence is excluded
                    # by EXDATE (the series expands to zero instances).  Exchange
                    # rejects creating such an empty series with ErrorItemNotFound.
                    if not self._has_valid_occurrences(comp):
                        self.logger.debug(
                            f"Skipping empty recurring series "
                            f"(all occurrences excluded by EXDATE): {base_uid}"
                        )
                        continue

                    # Exception occurrences of a recurring series share the same
                    # UID as the master VEVENT but carry a RECURRENCE-ID property.
                    # Build a compound state key so master and each exception are
                    # tracked independently (avoids duplicate-UID collisions).
                    rid_prop = comp.get_first_property(ICalGLib.PropertyKind.RECURRENCEID_PROPERTY)
                    if rid_prop:
                        rid_str = rid_prop.get_recurrenceid().as_ical_string()
                        work_uid = f"{base_uid}::RID::{rid_str}"
                    else:
                        work_uid = base_uid

                    work_uids_seen.add(work_uid)

                    ical_str = comp.as_ical_string()
                    obj_hash = self._compute_hash(ical_str)

                    if work_uid not in state:
                        # CREATE
                        self._process_creates(
                            work_uid, ical_str, obj_hash, personal_client, state_db
                        )
                    elif obj_hash != state[work_uid]['hash']:
                        # UPDATE
                        self._process_updates(
                            work_uid, ical_str, obj_hash,
                            state[work_uid]['target_uid'],
                            personal_client, state_db
                        )

                # Process deletions
                self.logger.info("Checking for deletions...")
                self._process_deletions(
                    state, work_uids_seen, personal_client, state_db
                )

                # Commit changes
                if not self.config.dry_run:
                    state_db.commit()

        except CalendarSyncError as e:
            self.logger.error(f"Sync failed: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}", exc_info=True)
            raise

        return self.stats


def load_config_file(config_path: Path) -> Dict[str, str]:
    """Load configuration from INI file."""
    if not config_path.exists():
        return {}

    parser = ConfigParser()
    parser.read(config_path)

    if 'calendar-sync' not in parser:
        return {}

    return dict(parser['calendar-sync'])


def setup_logging(verbose: bool):
    """Configure logging output."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(levelname)s: %(message)s'
    )


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Calendar synchronization via Evolution Data Server (bidirectional by default)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Bidirectional sync (default)
  %(prog)s --work-calendar abc123 --personal-calendar xyz789

  # One-way sync: work → personal only
  %(prog)s --work-calendar abc123 --personal-calendar xyz789 --only-to-personal

  # One-way sync: personal → work only
  %(prog)s --work-calendar abc123 --personal-calendar xyz789 --only-to-work

  # Dry run to see what would happen
  %(prog)s --work-calendar abc123 --personal-calendar xyz789 --dry-run

  # Refresh: remove synced events and resync
  %(prog)s --work-calendar abc123 --personal-calendar xyz789 --refresh

  # Clear: remove all synced events
  %(prog)s --work-calendar abc123 --personal-calendar xyz789 --clear

  # Use configuration file
  %(prog)s --config ~/.config/eds-calendar-sync.conf
        """
    )

    parser.add_argument(
        '--work-calendar',
        help='EDS calendar UID for work calendar'
    )

    parser.add_argument(
        '--personal-calendar',
        help='EDS calendar UID for personal calendar'
    )

    parser.add_argument(
        '--config',
        type=Path,
        default=DEFAULT_CONFIG,
        help=f'Configuration file path (default: {DEFAULT_CONFIG})'
    )

    parser.add_argument(
        '--state-db',
        type=Path,
        default=DEFAULT_STATE_DB,
        help=f'State database path (default: {DEFAULT_STATE_DB})'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without making changes'
    )

    parser.add_argument(
        '--refresh',
        action='store_true',
        help='Remove only synced events we created and resync (preserves other events)'
    )

    # Direction flags (mutually exclusive)
    direction_group = parser.add_mutually_exclusive_group()
    direction_group.add_argument(
        '--only-to-personal',
        action='store_true',
        help='One-way sync: work → personal only (default is bidirectional)'
    )
    direction_group.add_argument(
        '--only-to-work',
        action='store_true',
        help='One-way sync: personal → work only (default is bidirectional)'
    )

    parser.add_argument(
        '--clear',
        action='store_true',
        help='Remove all synced events created by this tool (uses metadata to identify)'
    )

    parser.add_argument(
        '--yes', '-y',
        action='store_true',
        help='Automatically confirm sync without prompting (always on for --dry-run)'
    )

    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose debug output'
    )

    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_arguments()
    setup_logging(args.verbose)

    logger = logging.getLogger(__name__)

    # Load config file if it exists
    config_file = load_config_file(args.config)

    # Determine work and personal calendar IDs (CLI args override config file)
    work_id = args.work_calendar or config_file.get('work_calendar_id')
    personal_id = args.personal_calendar or config_file.get('personal_calendar_id')

    if not work_id or not personal_id:
        logger.error(
            "Work and personal calendar IDs must be provided via --work-calendar/--personal-calendar "
            "or in configuration file"
        )
        sys.exit(1)

    # Determine sync direction
    if args.only_to_personal:
        sync_direction = 'to-personal'
    elif args.only_to_work:
        sync_direction = 'to-work'
    else:
        sync_direction = 'both'  # Default is bidirectional

    # Build configuration
    config = SyncConfig(
        work_calendar_id=work_id,
        personal_calendar_id=personal_id,
        state_db_path=args.state_db,
        dry_run=args.dry_run,
        refresh=args.refresh,
        verbose=args.verbose,
        sync_direction=sync_direction,
        clear=args.clear,
        yes=args.yes
    )

    # Get calendar display information
    work_name, work_account, work_uid = get_calendar_display_info(config.work_calendar_id)
    personal_name, personal_account, personal_uid = get_calendar_display_info(config.personal_calendar_id)

    # Display configuration
    logger.info("=" * 60)
    logger.info("EDS Calendar Sync")
    logger.info("=" * 60)

    # Format calendar display with account info
    work_display = f"{work_name}"
    if work_account:
        work_display += f" ({work_account})"
    logger.info(f"Work Calendar:     {work_display}")
    logger.info(f"                   UID: {work_uid}")

    personal_display = f"{personal_name}"
    if personal_account:
        personal_display += f" ({personal_account})"
    logger.info(f"Personal Calendar: {personal_display}")
    logger.info(f"                   UID: {personal_uid}")

    logger.info(f"State Database:    {config.state_db_path}")

    if config.clear:
        logger.info(f"Operation:         CLEAR (remove all synced events)")
    else:
        # Display sync direction
        direction_display = {
            'both': 'BIDIRECTIONAL (work ↔ personal)',
            'to-personal': 'ONE-WAY (work → personal)',
            'to-work': 'ONE-WAY (personal → work)'
        }
        logger.info(f"Sync Direction:    {direction_display[config.sync_direction]}")
        if config.refresh:
            logger.info(f"Refresh:           YES (remove synced events and resync)")

    logger.info(f"Mode:              {'DRY RUN' if config.dry_run else 'LIVE'}")
    logger.info("=" * 60)

    # Confirmation prompt (skip if --yes or --dry-run)
    if not config.yes and not config.dry_run:
        try:
            response = input("Proceed with sync? [y/N]: ").strip().lower()
            if response not in ('y', 'yes'):
                logger.info("Sync cancelled by user")
                sys.exit(0)
        except EOFError:
            # Non-interactive mode without --yes flag
            logger.error("Cannot prompt for confirmation in non-interactive mode. Use --yes to proceed.")
            sys.exit(1)

    # Run synchronization
    try:
        sync = CalendarSynchronizer(config)
        stats = sync.run()

        logger.info("=" * 60)
        logger.info("Sync Complete!")
        logger.info(f"  Added:    {stats.added}")
        logger.info(f"  Modified: {stats.modified}")
        logger.info(f"  Deleted:  {stats.deleted}")
        logger.info(f"  Errors:   {stats.errors}")
        logger.info("=" * 60)

        sys.exit(0 if stats.errors == 0 else 1)

    except CalendarSyncError as e:
        logger.error(f"Sync failed: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
