"""
Refresh and clear operations — remove synced events from calendars.
"""

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from eds_calendar_sync.db import StateDatabase
from eds_calendar_sync.eds_client import EDSCalendarClient
from eds_calendar_sync.models import CalendarSyncError
from eds_calendar_sync.models import SyncConfig
from eds_calendar_sync.models import SyncStats
from eds_calendar_sync.sanitizer import EventSanitizer
from eds_calendar_sync.sync.utils import parse_component


def perform_refresh(
    config: SyncConfig,
    stats: SyncStats,
    logger,
    personal_client: EDSCalendarClient,
    state_db: StateDatabase,
):
    """Delete only synced events we created in personal calendar, leaving other events untouched."""
    logger.warning("REFRESH MODE: Removing synced events and clearing state...")

    # Get all events we've created (tracked in state DB)
    state = state_db.get_all_state()
    personal_uids_to_delete = [s["target_uid"] for s in state.values()]

    # If state DB is empty, fall back to metadata scanning
    if len(personal_uids_to_delete) == 0:
        logger.info("State database empty, scanning personal calendar for managed events...")
        personal_events = personal_client.get_all_events()
        for obj in personal_events:
            comp = parse_component(obj)
            if EventSanitizer.is_managed_event(comp):
                personal_uids_to_delete.append(comp.get_uid())

        if len(personal_uids_to_delete) > 0:
            logger.info(f"Found {len(personal_uids_to_delete)} managed events via metadata scan")
        else:
            logger.info("No managed events found - calendars are clean")

    if config.dry_run:
        logger.info(
            f"[DRY RUN] Would delete {len(personal_uids_to_delete)} synced events "
            f"from personal calendar"
        )
        logger.info("[DRY RUN] Would clear state database")
        for uid in personal_uids_to_delete:
            logger.debug(f"[DRY RUN] Would delete: {uid}")
        return

    # Remove only events WE created (in state DB)
    deleted_count = 0
    for personal_uid in personal_uids_to_delete:
        try:
            personal_client.remove_event(personal_uid)
            deleted_count += 1
            logger.debug(f"Deleted synced event: {personal_uid}")
        except (GLib.Error, CalendarSyncError) as e:
            logger.debug(f"Failed to remove {personal_uid}: {e}")

    # Clear state database
    state_db.clear_all()
    state_db.commit()

    logger.info(f"Refresh complete: Removed {deleted_count} synced events (other events preserved)")


def perform_refresh_two_way(
    config: SyncConfig,
    stats: SyncStats,
    logger,
    work_client: EDSCalendarClient,
    personal_client: EDSCalendarClient,
    state_db: StateDatabase,
):
    """Delete only synced events we created in both calendars, leaving other events untouched."""
    logger.warning("REFRESH MODE (TWO-WAY): Removing synced events from both calendars...")

    # Get all sync pairs
    state_records = state_db.get_all_state_bidirectional()

    work_uids_to_delete = []
    personal_uids_to_delete = []

    for record in state_records:
        work_uid = record["source_uid"]
        personal_uid = record["target_uid"]
        origin = record["origin"]

        if origin == "source":  # Work→Personal sync (we created event in personal)
            personal_uids_to_delete.append(personal_uid)
        elif origin == "target":  # Personal→Work sync (we created event in work)
            work_uids_to_delete.append(work_uid)

    # If state DB is empty, fall back to metadata scanning
    if len(work_uids_to_delete) == 0 and len(personal_uids_to_delete) == 0:
        logger.info("State database empty, scanning calendars for managed events...")

        # Scan work calendar
        work_events = work_client.get_all_events()
        for obj in work_events:
            comp = parse_component(obj)
            if EventSanitizer.is_managed_event(comp):
                work_uids_to_delete.append(comp.get_uid())

        # Scan personal calendar
        personal_events = personal_client.get_all_events()
        for obj in personal_events:
            comp = parse_component(obj)
            if EventSanitizer.is_managed_event(comp):
                personal_uids_to_delete.append(comp.get_uid())

        if len(work_uids_to_delete) > 0 or len(personal_uids_to_delete) > 0:
            logger.info(
                f"Found {len(work_uids_to_delete)} work events, "
                f"{len(personal_uids_to_delete)} personal events via metadata scan"
            )
        else:
            logger.info("No managed events found - calendars are clean")

    if config.dry_run:
        logger.info(
            f"[DRY RUN] Would delete {len(work_uids_to_delete)} synced events from work calendar"
        )
        logger.info(
            f"[DRY RUN] Would delete {len(personal_uids_to_delete)} synced events "
            f"from personal calendar"
        )
        logger.info("[DRY RUN] Would clear state database")
        return

    # Remove events WE created in work calendar
    work_deleted = 0
    for work_uid in work_uids_to_delete:
        try:
            work_client.remove_event(work_uid)
            work_deleted += 1
            logger.debug(f"Deleted synced event from work: {work_uid}")
        except (GLib.Error, CalendarSyncError) as e:
            logger.debug(f"Failed to remove work event {work_uid}: {e}")

    # Remove events WE created in personal calendar
    personal_deleted = 0
    for personal_uid in personal_uids_to_delete:
        try:
            personal_client.remove_event(personal_uid)
            personal_deleted += 1
            logger.debug(f"Deleted synced event from personal: {personal_uid}")
        except (GLib.Error, CalendarSyncError) as e:
            logger.debug(f"Failed to remove personal event {personal_uid}: {e}")

    # Clear state database
    state_db.clear_all()
    state_db.commit()

    logger.info(
        f"Refresh complete: Removed {work_deleted} work events, "
        f"{personal_deleted} personal events (other events preserved)"
    )


def perform_refresh_to_work(
    config: SyncConfig,
    stats: SyncStats,
    logger,
    work_client: EDSCalendarClient,
    state_db: StateDatabase,
):
    """Delete only synced events in work calendar, leaving other events untouched."""
    logger.warning("REFRESH MODE: Removing synced events from work calendar...")

    # Get all events we've created in work calendar (tracked in state DB)
    state = state_db.get_all_state_by_target()
    work_uids_to_delete = [s["source_uid"] for s in state.values()]

    # If state DB is empty, fall back to metadata scanning
    if len(work_uids_to_delete) == 0:
        logger.info("State database empty, scanning work calendar for managed events...")
        work_events = work_client.get_all_events()
        for obj in work_events:
            comp = parse_component(obj)
            if EventSanitizer.is_managed_event(comp):
                work_uids_to_delete.append(comp.get_uid())

        if len(work_uids_to_delete) > 0:
            logger.info(f"Found {len(work_uids_to_delete)} managed events via metadata scan")
        else:
            logger.info("No managed events found - work calendar is clean")

    if config.dry_run:
        logger.info(
            f"[DRY RUN] Would delete {len(work_uids_to_delete)} synced events from work calendar"
        )
        logger.info("[DRY RUN] Would clear state database")
        for uid in work_uids_to_delete:
            logger.debug(f"[DRY RUN] Would delete: {uid}")
        return

    # Remove only events WE created (in state DB)
    deleted_count = 0
    for work_uid in work_uids_to_delete:
        try:
            work_client.remove_event(work_uid)
            deleted_count += 1
            logger.debug(f"Deleted synced event from work: {work_uid}")
        except (GLib.Error, CalendarSyncError) as e:
            logger.debug(f"Failed to remove {work_uid}: {e}")

    # Clear state database
    state_db.clear_all()
    state_db.commit()

    logger.info(f"Refresh complete: Removed {deleted_count} synced events from work calendar")


def perform_clear(
    config: SyncConfig,
    stats: SyncStats,
    logger,
    work_client: EDSCalendarClient,
    personal_client: EDSCalendarClient,
    state_db: StateDatabase,
):
    """Remove all synced events we created by checking metadata."""
    logger.warning("CLEAR MODE: Removing all synced events created by this tool...")

    work_managed = []
    personal_managed = []

    # Scan calendars based on sync direction
    if config.sync_direction in ("both", "to-work"):
        # We create events in work calendar when syncing to work
        logger.info("Scanning work calendar for managed events...")
        work_events = work_client.get_all_events()
        for obj in work_events:
            comp = parse_component(obj)
            if EventSanitizer.is_managed_event(comp):
                work_managed.append(comp.get_uid())

    if config.sync_direction in ("both", "to-personal"):
        # We create events in personal calendar when syncing to personal
        logger.info("Scanning personal calendar for managed events...")
        personal_events = personal_client.get_all_events()
        for obj in personal_events:
            comp = parse_component(obj)
            if EventSanitizer.is_managed_event(comp):
                personal_managed.append(comp.get_uid())

    total_to_delete = len(work_managed) + len(personal_managed)

    # Report findings based on what we scanned
    if total_to_delete > 0:
        parts = []
        if config.sync_direction in ("both", "to-work") and len(work_managed) > 0:
            parts.append(f"{len(work_managed)} in work calendar")
        if config.sync_direction in ("both", "to-personal") and len(personal_managed) > 0:
            parts.append(f"{len(personal_managed)} in personal calendar")
        logger.info(f"Found {' and '.join(parts)} managed events")
    else:
        logger.info("No managed events found")

    if config.dry_run:
        logger.info(f"[DRY RUN] Would delete {total_to_delete} total managed events")
        logger.info("[DRY RUN] Would clear state database")
        if config.verbose:
            for uid in work_managed:
                logger.debug(f"[DRY RUN] Would delete work event: {uid}")
            for uid in personal_managed:
                logger.debug(f"[DRY RUN] Would delete personal event: {uid}")
        return

    # Delete managed events from work calendar (if applicable)
    work_deleted = 0
    if config.sync_direction in ("both", "to-work"):
        for uid in work_managed:
            try:
                work_client.remove_event(uid)
                work_deleted += 1
                logger.debug(f"Deleted work event: {uid}")
            except (GLib.Error, CalendarSyncError) as e:
                logger.error(f"Failed to delete work event {uid}: {e}")
                stats.errors += 1

    # Delete managed events from personal calendar (if applicable)
    personal_deleted = 0
    if config.sync_direction in ("both", "to-personal"):
        for uid in personal_managed:
            try:
                personal_client.remove_event(uid)
                personal_deleted += 1
                logger.debug(f"Deleted personal event: {uid}")
            except (GLib.Error, CalendarSyncError) as e:
                logger.error(f"Failed to delete personal event {uid}: {e}")
                stats.errors += 1

    # Clear state database
    state_db.clear_all()
    state_db.commit()

    logger.info(
        f"Clear complete: Removed {work_deleted} work events, {personal_deleted} personal events"
    )
    stats.deleted = work_deleted + personal_deleted
