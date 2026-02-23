"""
Work→Personal one-way sync.
"""

import uuid

import gi

gi.require_version("ICalGLib", "3.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib
from gi.repository import ICalGLib

from eds_calendar_sync.db import StateDatabase
from eds_calendar_sync.eds_client import EDSCalendarClient
from eds_calendar_sync.models import CalendarSyncError
from eds_calendar_sync.models import SyncConfig
from eds_calendar_sync.models import SyncStats
from eds_calendar_sync.sanitizer import EventSanitizer
from eds_calendar_sync.sync.refresh import perform_refresh
from eds_calendar_sync.sync.utils import _EXDATE_DATE_RE
from eds_calendar_sync.sync.utils import build_orphan_index
from eds_calendar_sync.sync.utils import compute_hash
from eds_calendar_sync.sync.utils import compute_source_fingerprint
from eds_calendar_sync.sync.utils import has_valid_occurrences
from eds_calendar_sync.sync.utils import is_event_cancelled
from eds_calendar_sync.sync.utils import is_free_time
from eds_calendar_sync.sync.utils import is_not_found_error
from eds_calendar_sync.sync.utils import parse_component


def _process_creates(
    config: SyncConfig,
    stats: SyncStats,
    logger,
    work_uid: str,
    ical_str: str,
    obj_hash: str,
    personal_client: EDSCalendarClient,
    state_db: StateDatabase,
    orphan_index: dict[str, str] | None = None,
):
    """Handle creation of new events in personal calendar."""
    personal_uid = str(uuid.uuid4())

    if config.dry_run:
        logger.info(f"[DRY RUN] Would CREATE event: {work_uid} -> {personal_uid}")
        stats.added += 1
        return

    sanitized = None

    # Check orphan index: a previous crash may have created this event in the
    # target calendar without committing the DB record.  Recover by registering
    # the existing event instead of creating a duplicate.
    if orphan_index is not None:
        fingerprint = compute_source_fingerprint(work_uid)
        if fingerprint in orphan_index:
            existing_uid = orphan_index[fingerprint]
            logger.info(f"Recovering orphan: {work_uid} → {existing_uid}")
            work_hash = compute_hash(ical_str)
            recovered = personal_client.get_event(existing_uid)
            if recovered:
                personal_hash = compute_hash(recovered.as_ical_string())
            else:
                personal_hash = work_hash
            state_db.insert_bidirectional(
                work_uid, existing_uid, work_hash, personal_hash, "source"
            )
            state_db.commit()
            stats.added += 1
            return

    try:
        sanitized = EventSanitizer.sanitize(
            ical_str,
            personal_uid,
            keep_reminders=config.keep_reminders,
            source_uid=work_uid,
        )

        # Debug: Show sanitized output
        if config.verbose:
            sanitized_str = sanitized.as_ical_string()
            logger.debug(f"Sanitized iCal:\n{sanitized_str}")

        # Create event and get the ACTUAL UID assigned by the server
        # (Microsoft 365 will rewrite the UID, so we must use what's returned)
        actual_personal_uid = personal_client.create_event(sanitized)

        # Use the actual UID if returned, otherwise fall back to our generated one
        if actual_personal_uid:
            personal_uid = actual_personal_uid
            logger.debug(f"Server assigned UID: {personal_uid}")

        # Fetch the event back to get the actual stored version and compute both hashes
        work_hash = compute_hash(ical_str)
        created_personal = personal_client.get_event(personal_uid)
        if created_personal:
            personal_hash = compute_hash(created_personal.as_ical_string())
        else:
            # Fallback if fetch fails
            personal_hash = compute_hash(sanitized.as_ical_string())

        state_db.insert_bidirectional(work_uid, personal_uid, work_hash, personal_hash, "source")
        state_db.commit()
        stats.added += 1
        logger.debug(f"Created event {work_uid} as {personal_uid}")
    except (GLib.Error, CalendarSyncError) as e:
        if sanitized is not None:
            logger.warning(
                f"Sanitized iCal for failed event {work_uid}:\n{sanitized.as_ical_string()}"
            )
        logger.error(f"Failed to create event {work_uid}: {e}")
        stats.errors += 1


def _process_updates(
    config: SyncConfig,
    stats: SyncStats,
    logger,
    work_uid: str,
    ical_str: str,
    obj_hash: str,
    personal_uid: str,
    personal_client: EDSCalendarClient,
    state_db: StateDatabase,
):
    """Handle updates to existing events in personal calendar."""
    if config.dry_run:
        logger.info(f"[DRY RUN] Would UPDATE event: {work_uid} (personal: {personal_uid})")
        stats.modified += 1
        return

    try:
        sanitized = EventSanitizer.sanitize(
            ical_str,
            personal_uid,
            keep_reminders=config.keep_reminders,
            source_uid=work_uid,
        )
        personal_client.modify_event(sanitized)

        # Fetch the event back to get the actual stored version
        work_hash = compute_hash(ical_str)
        updated_personal = personal_client.get_event(personal_uid)
        if updated_personal:
            personal_hash = compute_hash(updated_personal.as_ical_string())
        else:
            # Fallback if fetch fails
            personal_hash = compute_hash(sanitized.as_ical_string())

        state_db.update_hashes(work_uid, personal_uid, work_hash, personal_hash)
        state_db.commit()
        stats.modified += 1
        logger.debug(f"Updated event {work_uid}")
    except (GLib.Error, CalendarSyncError) as e:
        # "Object not found" means the personal event was deleted externally between
        # syncs.  That is a normal occurrence; recreate it silently at DEBUG level.
        # Any other modify failure is unexpected and warrants a WARNING.
        if is_not_found_error(e):
            logger.debug(
                f"Personal event {personal_uid} no longer exists (externally deleted);"
                f" recreating for work event {work_uid}"
            )
            already_gone = True
        else:
            logger.warning(f"Modify failed for {work_uid}, attempting recreate: {e}")
            already_gone = False

        try:
            if not already_gone:
                # Try to remove the stale copy before replacing it
                try:
                    personal_client.remove_event(personal_uid)
                except Exception:
                    pass  # May already be gone

            # Create new with fresh UUID (will be rewritten by server)
            new_uid = str(uuid.uuid4())
            sanitized = EventSanitizer.sanitize(
                ical_str,
                new_uid,
                keep_reminders=config.keep_reminders,
                source_uid=work_uid,
            )
            actual_uid = personal_client.create_event(sanitized)

            # Update state with new UID if returned
            if actual_uid:
                new_uid = actual_uid

            # Fetch back and compute both hashes
            work_hash = compute_hash(ical_str)
            created_personal = personal_client.get_event(new_uid)
            if created_personal:
                personal_hash = compute_hash(created_personal.as_ical_string())
            else:
                personal_hash = compute_hash(sanitized.as_ical_string())

            # Update state DB with new personal UID
            state_db.delete(work_uid)
            state_db.insert_bidirectional(work_uid, new_uid, work_hash, personal_hash, "source")
            state_db.commit()
            stats.modified += 1
            logger.debug(f"Recreated event {work_uid} as {new_uid}")
        except (GLib.Error, CalendarSyncError) as e2:
            logger.error(f"Failed to update/recreate event {work_uid}: {e2}")
            stats.errors += 1


def _process_deletions(
    config: SyncConfig,
    stats: SyncStats,
    logger,
    state: dict[str, dict[str, str]],
    work_uids_seen: set[str],
    personal_client: EDSCalendarClient,
    state_db: StateDatabase,
):
    """Handle deletion of events removed from work calendar."""
    for work_uid in list(state.keys()):
        if work_uid not in work_uids_seen:
            personal_uid = state[work_uid]["target_uid"]

            if config.dry_run:
                logger.info(f"[DRY RUN] Would DELETE event: {work_uid} (personal: {personal_uid})")
                stats.deleted += 1
                continue

            logger.debug(f"Attempting to delete personal event with UID: {personal_uid}")
            try:
                personal_client.remove_event(personal_uid)
                logger.debug(f"Successfully deleted event {work_uid} (personal: {personal_uid})")
            except (GLib.Error, CalendarSyncError) as e:
                if is_not_found_error(e):
                    # Already gone externally — state DB cleanup still needed
                    logger.debug(
                        f"Personal event {personal_uid} already gone (externally deleted);"
                        f" cleaning up state for work event {work_uid}"
                    )
                else:
                    logger.error(f"Failed to delete {personal_uid}: {e}")
                    stats.errors += 1
                    continue

            state_db.delete(work_uid)
            state_db.commit()
            stats.deleted += 1


def run_one_way_to_personal(
    config: SyncConfig,
    stats: SyncStats,
    logger,
    work_client: EDSCalendarClient,
    personal_client: EDSCalendarClient,
    state_db: StateDatabase,
):
    """Execute one-way synchronization (work → personal)."""
    try:
        # Handle refresh mode
        if config.refresh:
            perform_refresh(config, stats, logger, personal_client, state_db)

        # Load current state
        logger.info("Loading sync state...")
        state = state_db.get_all_state()

        # Pre-sync orphan scan: find managed events in the personal calendar
        # that lack a DB record (created by a previous run that crashed before commit).
        logger.info("Scanning personal calendar for orphaned managed events...")
        orphan_index = build_orphan_index(personal_client, state_db, logger)

        # Fetch work events
        logger.info("Fetching work events...")
        work_events = work_client.get_all_events()
        work_uids_seen: set[str] = set()

        # Pre-scan: collect each master VEVENT's EXDATE set.
        # Exchange represents a declined recurring instance by:
        #   (a) adding the declined date to the master VEVENT's EXDATE
        #   (b) creating an exception VEVENT (same UID, RECURRENCE-ID)
        # Exchange does NOT set TRANSP:TRANSPARENT on these exceptions.
        # We read the master EXDATEs here so the main loop can detect
        # and skip exception VEVENTs that represent declined instances.
        master_exdates_by_uid: dict[str, set[str]] = {}
        for _obj in work_events:
            _comp = parse_component(_obj)
            # Only master VEVENTs (no RECURRENCE-ID)
            if _comp.get_first_property(ICalGLib.PropertyKind.RECURRENCEID_PROPERTY):
                continue
            _uid = _comp.get_uid()
            if not _uid:
                continue
            _exdates: set[str] = set()
            _ed = _comp.get_first_property(ICalGLib.PropertyKind.EXDATE_PROPERTY)
            while _ed:
                try:
                    _t = _ed.get_exdate()
                    if _t and not _t.is_null_time():
                        _exdates.add(f"{_t.get_year():04d}{_t.get_month():02d}{_t.get_day():02d}")
                except Exception:
                    pass
                _ed = _comp.get_next_property(ICalGLib.PropertyKind.EXDATE_PROPERTY)
            # Fallback for VALUE=DATE EXDATEs that return null_time via
            # get_exdate() in some libical-glib builds.
            if not _exdates:
                try:
                    for _em in _EXDATE_DATE_RE.finditer(_comp.as_ical_string() or ""):
                        _exdates.add(_em.group(1))
                except Exception:
                    pass
            if _exdates:
                master_exdates_by_uid[_uid] = _exdates

        # Process each work event
        logger.info(f"Processing {len(work_events)} work events...")
        for obj in work_events:
            comp = parse_component(obj)
            base_uid = comp.get_uid()

            # Skip events we created ourselves (managed events in the work
            # calendar are "Busy" blocks synced from personal by --only-to-work
            # or --both).  Re-syncing them to personal would create circular
            # duplicates and trigger a UNIQUE constraint violation.
            if EventSanitizer.is_managed_event(comp):
                logger.debug(f"Skipping managed event: {base_uid}")
                continue

            # Skip cancelled events entirely.  They no longer block time
            # and Exchange rejects creating them via CreateItem.  Their
            # absence from work_uids_seen will cause _process_deletions
            # to remove any previously synced copy from the personal
            # calendar.
            if is_event_cancelled(comp):
                logger.debug(f"Skipping cancelled event: {base_uid}")
                continue

            # Skip transparent (free-time) events — they do not block the
            # user's time so they should not appear as busy in the personal
            # calendar.  Exchange marks declined meetings as transparent.
            if is_free_time(comp):
                logger.debug(f"Skipping transparent (free-time) event: {base_uid}")
                continue

            # Skip recurring events where every occurrence is excluded
            # by EXDATE (the series expands to zero instances).  Exchange
            # rejects creating such an empty series with ErrorItemNotFound.
            if not has_valid_occurrences(comp):
                logger.debug(
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

            # Skip exception VEVENTs that represent declined or removed
            # instances.  Exchange records a declined series instance by:
            # (a) adding the declined date to the master's EXDATE, and
            # (b) creating an exception VEVENT (same UID + RECURRENCE-ID).
            # Exchange does NOT set TRANSP:TRANSPARENT on these.
            # We detect them by checking that the exception's RECURRENCE-ID
            # date is in the master's EXDATE AND the exception's DTSTART
            # falls on the same date (ruling out genuinely rescheduled
            # occurrences, which have a different DTSTART date).
            if rid_prop:
                try:
                    _rid_t = rid_prop.get_recurrenceid()
                    _rid_date = (
                        f"{_rid_t.get_year():04d}{_rid_t.get_month():02d}{_rid_t.get_day():02d}"
                    )
                    if _rid_date in master_exdates_by_uid.get(base_uid, set()):
                        _dts_prop = comp.get_first_property(ICalGLib.PropertyKind.DTSTART_PROPERTY)
                        if _dts_prop:
                            _dts = _dts_prop.get_dtstart()
                            _dts_date = (
                                f"{_dts.get_year():04d}{_dts.get_month():02d}{_dts.get_day():02d}"
                            )
                            if _dts_date == _rid_date:
                                logger.debug(
                                    f"Skipping declined/removed exception {base_uid} on {_rid_date}"
                                )
                                continue
                except Exception:
                    pass

            work_uids_seen.add(work_uid)

            ical_str = comp.as_ical_string()
            obj_hash = compute_hash(ical_str)

            if work_uid not in state:
                # CREATE
                _process_creates(
                    config,
                    stats,
                    logger,
                    work_uid,
                    ical_str,
                    obj_hash,
                    personal_client,
                    state_db,
                    orphan_index=orphan_index,
                )
            elif obj_hash != state[work_uid]["hash"]:
                # UPDATE
                _process_updates(
                    config,
                    stats,
                    logger,
                    work_uid,
                    ical_str,
                    obj_hash,
                    state[work_uid]["target_uid"],
                    personal_client,
                    state_db,
                )

        # Process deletions
        logger.info("Checking for deletions...")
        _process_deletions(config, stats, logger, state, work_uids_seen, personal_client, state_db)

        # Commit changes
        if not config.dry_run:
            state_db.commit()

    except CalendarSyncError as e:
        logger.error(f"Sync failed: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise
