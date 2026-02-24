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
from eds_calendar_sync.sync.utils import build_orphan_index
from eds_calendar_sync.sync.utils import compute_hash
from eds_calendar_sync.sync.utils import compute_source_fingerprint
from eds_calendar_sync.sync.utils import has_valid_occurrences
from eds_calendar_sync.sync.utils import is_event_cancelled
from eds_calendar_sync.sync.utils import is_free_time
from eds_calendar_sync.sync.utils import is_not_found_error
from eds_calendar_sync.sync.utils import parse_component
from eds_calendar_sync.sync.utils import strip_exdates_for_dates


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
        work_events_list = work_client.get_all_events()
        work_events: dict[str, ICalGLib.Component] = {}

        # First pass: collect master VEVENTs
        for _obj in work_events_list:
            _comp = parse_component(_obj)
            if _comp.get_first_property(ICalGLib.PropertyKind.RECURRENCEID_PROPERTY):
                continue
            _uid = _comp.get_uid()
            if _uid:
                work_events[_uid] = _comp

        # Second pass: analysis of exception VEVENTs.
        # Build a map from work UID → set of YYYYMMDD dates that have a valid
        # (non-managed, non-cancelled, non-free) exception VEVENT.  Exchange
        # stores every explicitly-defined recurring occurrence as both an EXDATE
        # in the master VEVENT and a separate exception VEVENT with RECURRENCE-ID.
        # Those "phantom" EXDATEs suppress GNOME Calendar display even though the
        # occurrences are real meetings.  We strip them when writing to personal.
        work_valid_exception_dates: dict[str, set[str]] = {}
        for _obj in work_events_list:
            _comp = parse_component(_obj)
            _rid_prop = _comp.get_first_property(ICalGLib.PropertyKind.RECURRENCEID_PROPERTY)
            if not _rid_prop:
                continue  # master VEVENT — handled above
            if EventSanitizer.is_managed_event(_comp):
                continue
            if is_event_cancelled(_comp):
                continue
            if is_free_time(_comp):
                continue
            _uid = _comp.get_uid()
            if not _uid:
                continue
            try:
                _rid_t = _rid_prop.get_recurrenceid()
                _rid_date = f"{_rid_t.get_year():04d}{_rid_t.get_month():02d}{_rid_t.get_day():02d}"
                # Detect genuinely rescheduled occurrences (DTSTART date ≠ RECURRENCE-ID date).
                _dts_prop = _comp.get_first_property(ICalGLib.PropertyKind.DTSTART_PROPERTY)
                _is_rescheduled = False
                if _dts_prop:
                    _dts = _dts_prop.get_dtstart()
                    _dts_date = f"{_dts.get_year():04d}{_dts.get_month():02d}{_dts.get_day():02d}"
                    _is_rescheduled = _dts_date != _rid_date
                if _is_rescheduled:
                    # Rescheduled: treat as a standalone event keyed by compound UID.
                    _rid_str = _rid_t.as_ical_string()
                    _compound_uid = f"{_uid}::RID::{_rid_str}"
                    work_events[_compound_uid] = _comp
                else:
                    # Non-rescheduled: record the date so we can strip the phantom EXDATE.
                    work_valid_exception_dates.setdefault(_uid, set()).add(_rid_date)
            except Exception:
                pass

        # Process each work event
        logger.info(f"Processing {len(work_events)} work events...")
        work_uids_seen: set[str] = set()

        for work_uid, comp in work_events.items():
            base_uid = comp.get_uid()

            # Skip events we created ourselves (managed events in the work
            # calendar are "Busy" blocks synced from personal by --only-to-work
            # or --both).  Re-syncing them to personal would create circular
            # duplicates and trigger a UNIQUE constraint violation.
            if EventSanitizer.is_managed_event(comp):
                logger.debug(f"Skipping managed event: {base_uid}")
                continue

            # Skip cancelled events entirely.
            if is_event_cancelled(comp):
                logger.debug(f"Skipping cancelled event: {base_uid}")
                continue

            # Skip transparent (free-time) events.
            if is_free_time(comp):
                logger.debug(f"Skipping transparent (free-time) event: {base_uid}")
                continue

            # Skip recurring events where every occurrence is excluded by EXDATE.
            # Check against the stripped iCal so that series whose EXDATEs
            # are all "phantom" (covered by valid exception VEVENTs) are
            # not incorrectly skipped.
            _valid_ex_dates = work_valid_exception_dates.get(work_uid, set())
            if _valid_ex_dates:
                _stripped = ICalGLib.Component.new_from_string(
                    strip_exdates_for_dates(comp.as_ical_string(), _valid_ex_dates)
                )
                _has_valid = has_valid_occurrences(_stripped)
            else:
                _has_valid = has_valid_occurrences(comp)

            if not _has_valid:
                logger.debug(
                    f"Skipping empty recurring series "
                    f"(all occurrences excluded by EXDATE): {base_uid}"
                )
                continue

            work_uids_seen.add(work_uid)

            ical_str = comp.as_ical_string()
            # Strip phantom EXDATEs before hashing and syncing.
            if work_uid in work_valid_exception_dates:
                ical_str = strip_exdates_for_dates(ical_str, work_valid_exception_dates[work_uid])

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
