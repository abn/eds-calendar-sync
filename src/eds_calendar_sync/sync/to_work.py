"""
Personal→Work one-way sync.
"""

import uuid
from typing import Dict, Set

import gi
gi.require_version('GLib', '2.0')
from gi.repository import GLib

from ..models import CalendarSyncError, SyncConfig, SyncStats
from ..eds_client import EDSCalendarClient
from ..db import StateDatabase
from ..sanitizer import EventSanitizer
from .refresh import perform_refresh_to_work
from .utils import compute_hash, parse_component


def _process_creates_to_work(
    config: SyncConfig,
    stats: SyncStats,
    logger,
    personal_uid: str,
    ical_str: str,
    obj_hash: str,
    work_client: EDSCalendarClient,
    state_db: StateDatabase,
):
    """Handle creation of new events in work calendar from personal."""
    work_uid = str(uuid.uuid4())

    if config.dry_run:
        logger.info(f"[DRY RUN] Would CREATE event: {personal_uid} -> {work_uid}")
        stats.added += 1
        return

    try:
        # Use 'busy' mode sanitization for personal → work
        sanitized = EventSanitizer.sanitize(ical_str, work_uid, mode='busy')

        if config.verbose:
            sanitized_str = sanitized.as_ical_string()
            logger.debug(f"Sanitized iCal:\n{sanitized_str}")

        # Create event and get the ACTUAL UID assigned by the server
        actual_work_uid = work_client.create_event(sanitized)

        # Use the actual UID if returned, otherwise fall back to our generated one
        if actual_work_uid:
            work_uid = actual_work_uid
            logger.debug(f"Server assigned UID: {work_uid}")

        # Fetch the event back to get the actual stored version and compute both hashes
        personal_hash = compute_hash(ical_str)
        created_work = work_client.get_event(work_uid)
        if created_work:
            work_hash = compute_hash(created_work.as_ical_string())
        else:
            # Fallback if fetch fails
            work_hash = compute_hash(sanitized.as_ical_string())

        # source=work, target=personal, origin='target' (event originated from personal calendar)
        state_db.insert_bidirectional(work_uid, personal_uid, work_hash, personal_hash, 'target')
        stats.added += 1
        logger.debug(f"Created event {personal_uid} as {work_uid} in work calendar")
    except (GLib.Error, CalendarSyncError) as e:
        logger.error(f"Failed to create event {personal_uid}: {e}")
        stats.errors += 1


def _process_updates_to_work(
    config: SyncConfig,
    stats: SyncStats,
    logger,
    personal_uid: str,
    ical_str: str,
    obj_hash: str,
    work_uid: str,
    work_client: EDSCalendarClient,
    state_db: StateDatabase,
):
    """Handle updates to existing events in work calendar from personal."""
    if config.dry_run:
        logger.info(f"[DRY RUN] Would UPDATE event: {personal_uid} (work: {work_uid})")
        stats.modified += 1
        return

    try:
        # Use 'busy' mode sanitization for personal → work
        sanitized = EventSanitizer.sanitize(ical_str, work_uid, mode='busy')
        work_client.modify_event(sanitized)

        # Fetch the event back to get the actual stored version
        personal_hash = compute_hash(ical_str)
        updated_work = work_client.get_event(work_uid)
        if updated_work:
            work_hash = compute_hash(updated_work.as_ical_string())
        else:
            # Fallback if fetch fails
            work_hash = compute_hash(sanitized.as_ical_string())

        state_db.update_hashes(work_uid, personal_uid, work_hash, personal_hash)
        stats.modified += 1
        logger.debug(f"Updated event {personal_uid} in work calendar")
    except (GLib.Error, CalendarSyncError) as e:
        # If modify fails, try recreating
        logger.warning(f"Modify failed for {personal_uid}, attempting recreate: {e}")
        try:
            # First delete the old event
            try:
                work_client.remove_event(work_uid)
            except Exception:
                pass  # May already be gone

            # Create new with fresh UUID (will be rewritten by server)
            new_uid = str(uuid.uuid4())
            sanitized = EventSanitizer.sanitize(ical_str, new_uid, mode='busy')
            actual_uid = work_client.create_event(sanitized)

            # Update state with new UID if returned
            if actual_uid:
                new_uid = actual_uid

            # Fetch back and compute both hashes
            personal_hash = compute_hash(ical_str)
            created_work = work_client.get_event(new_uid)
            if created_work:
                work_hash = compute_hash(created_work.as_ical_string())
            else:
                work_hash = compute_hash(sanitized.as_ical_string())

            # Update state DB with new work UID (source=work, target=personal, origin='target')
            state_db.delete_by_pair(work_uid, personal_uid)
            state_db.insert_bidirectional(new_uid, personal_uid, work_hash, personal_hash, 'target')
            stats.modified += 1
            logger.debug(f"Recreated event {personal_uid} as {new_uid} in work calendar")
        except (GLib.Error, CalendarSyncError) as e2:
            logger.error(f"Failed to update/recreate event {personal_uid}: {e2}")
            stats.errors += 1


def _process_deletions_to_work(
    config: SyncConfig,
    stats: SyncStats,
    logger,
    state: Dict[str, Dict[str, str]],
    personal_uids_seen: Set[str],
    work_client: EDSCalendarClient,
    state_db: StateDatabase,
):
    """Handle deletion of events removed from personal calendar."""
    for personal_uid in list(state.keys()):
        if personal_uid not in personal_uids_seen:
            work_uid = state[personal_uid]['source_uid']

            if config.dry_run:
                logger.info(
                    f"[DRY RUN] Would DELETE event: {personal_uid} (work: {work_uid})"
                )
                stats.deleted += 1
                continue

            logger.debug(f"Attempting to delete work event with UID: {work_uid}")
            try:
                work_client.remove_event(work_uid)
                logger.debug(
                    f"Successfully deleted event {personal_uid} (work: {work_uid})"
                )
            except (GLib.Error, CalendarSyncError) as e:
                logger.error(f"Failed to delete {work_uid}: {e}")
                stats.errors += 1
                continue

            state_db.delete_by_pair(work_uid, personal_uid)
            stats.deleted += 1


def run_one_way_to_work(
    config: SyncConfig,
    stats: SyncStats,
    logger,
    work_client: EDSCalendarClient,
    personal_client: EDSCalendarClient,
    state_db: StateDatabase,
):
    """Execute one-way synchronization (personal → work)."""
    try:
        # Handle refresh mode
        if config.refresh:
            perform_refresh_to_work(config, stats, logger, work_client, state_db)

        # Load current state keyed by personal (target) UID
        logger.info("Loading sync state...")
        state = state_db.get_all_state_by_target()

        # Fetch personal events (source)
        logger.info("Fetching personal events...")
        personal_events = personal_client.get_all_events()
        personal_uids_seen: Set[str] = set()

        # Process each personal event
        logger.info(f"Processing {len(personal_events)} personal events...")
        for obj in personal_events:
            comp = parse_component(obj)
            personal_uid = comp.get_uid()

            # Skip events we created ourselves (managed events in the personal
            # calendar are copies of work events, synced by --only-to-personal
            # or --both).  Re-syncing them to work would create circular duplicates.
            if EventSanitizer.is_managed_event(comp):
                logger.debug(f"Skipping managed event: {personal_uid}")
                continue

            personal_uids_seen.add(personal_uid)

            ical_str = comp.as_ical_string()
            obj_hash = compute_hash(ical_str)

            if personal_uid not in state:
                # CREATE in work calendar
                _process_creates_to_work(
                    config, stats, logger,
                    personal_uid, ical_str, obj_hash, work_client, state_db
                )
            elif obj_hash != state[personal_uid]['hash']:
                # UPDATE in work calendar
                _process_updates_to_work(
                    config, stats, logger,
                    personal_uid, ical_str, obj_hash,
                    state[personal_uid]['source_uid'],
                    work_client, state_db
                )

        # Process deletions
        logger.info("Checking for deletions...")
        _process_deletions_to_work(
            config, stats, logger, state, personal_uids_seen, work_client, state_db
        )

        # Commit changes
        if not config.dry_run:
            state_db.commit()

    except CalendarSyncError as e:
        logger.error(f"Sync failed: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise
