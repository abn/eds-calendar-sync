"""
Bidirectional sync.
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
from eds_calendar_sync.sync.refresh import perform_refresh_two_way
from eds_calendar_sync.sync.utils import build_orphan_index
from eds_calendar_sync.sync.utils import compute_hash
from eds_calendar_sync.sync.utils import compute_source_fingerprint
from eds_calendar_sync.sync.utils import has_valid_occurrences
from eds_calendar_sync.sync.utils import is_event_cancelled
from eds_calendar_sync.sync.utils import is_free_time
from eds_calendar_sync.sync.utils import parse_component
from eds_calendar_sync.sync.utils import strip_exdates_for_dates


def _process_new_work_event(
    config: SyncConfig,
    stats: SyncStats,
    logger,
    work_uid: str,
    work_comp: ICalGLib.Component,
    personal_client: EDSCalendarClient,
    state_db: StateDatabase,
    orphan_index: dict[str, str] | None = None,
    valid_exception_dates: set[str] | None = None,
):
    """Handle creation of new event in personal calendar from work."""
    personal_uid = str(uuid.uuid4())
    work_ical = work_comp.as_ical_string()
    # Strip phantom EXDATEs — Exchange adds every explicitly-defined occurrence
    # to the master's EXDATE list even when the occurrence is a real meeting
    # (represented by an exception VEVENT with RECURRENCE-ID).  Keeping those
    # EXDATEs in the personal calendar suppresses GNOME Calendar display.
    work_ical = strip_exdates_for_dates(work_ical, valid_exception_dates or set())

    if config.dry_run:
        logger.info(f"[DRY RUN] [WORK→PERSONAL] Would CREATE: {work_uid} -> {personal_uid}")
        stats.added += 1
        return

    # Check orphan index: a previous crash may have created this event in the
    # personal calendar without committing the DB record.
    if orphan_index is not None:
        fingerprint = compute_source_fingerprint(work_uid)
        if fingerprint in orphan_index:
            existing_uid = orphan_index[fingerprint]
            logger.info(f"Recovering orphan: {work_uid} → {existing_uid}")
            work_hash = compute_hash(work_ical)
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
            work_ical,
            personal_uid,
            mode="normal",
            keep_reminders=config.keep_reminders,
            source_uid=work_uid,
        )

        if config.verbose:
            logger.debug(f"Sanitized iCal:\n{sanitized.as_ical_string()}")

        # Create event and get actual UID
        actual_personal_uid = personal_client.create_event(sanitized)
        if actual_personal_uid:
            personal_uid = actual_personal_uid
            logger.debug(f"Server assigned UID: {personal_uid}")

        # Fetch the event back to get the actual stored version
        work_hash = compute_hash(work_ical)
        created_personal = personal_client.get_event(personal_uid)
        if created_personal:
            personal_hash = compute_hash(created_personal.as_ical_string())
        else:
            # Fallback if fetch fails
            personal_hash = compute_hash(sanitized.as_ical_string())

        state_db.insert_bidirectional(work_uid, personal_uid, work_hash, personal_hash, "source")
        state_db.commit()
        stats.added += 1
        logger.debug(f"Created personal event {personal_uid} from work {work_uid}")
    except (GLib.Error, CalendarSyncError) as e:
        logger.error(f"Failed to create personal event from {work_uid}: {e}")
        stats.errors += 1


def _process_new_personal_event(
    config: SyncConfig,
    stats: SyncStats,
    logger,
    personal_uid: str,
    personal_comp: ICalGLib.Component,
    work_client: EDSCalendarClient,
    state_db: StateDatabase,
    orphan_index: dict[str, str] | None = None,
):
    """Handle creation of new event in work calendar from personal."""
    work_uid = str(uuid.uuid4())
    personal_ical = personal_comp.as_ical_string()

    if config.dry_run:
        logger.info(f"[DRY RUN] [PERSONAL→WORK] Would CREATE: {personal_uid} -> {work_uid}")
        stats.added += 1
        return

    # Check orphan index: a previous crash may have created this event in the
    # work calendar without committing the DB record.
    if orphan_index is not None:
        fingerprint = compute_source_fingerprint(personal_uid)
        if fingerprint in orphan_index:
            existing_uid = orphan_index[fingerprint]
            logger.info(f"Recovering orphan: {personal_uid} → {existing_uid}")
            personal_hash = compute_hash(personal_ical)
            recovered = work_client.get_event(existing_uid)
            if recovered:
                work_hash = compute_hash(recovered.as_ical_string())
            else:
                work_hash = personal_hash
            state_db.insert_bidirectional(
                existing_uid, personal_uid, work_hash, personal_hash, "target"
            )
            state_db.commit()
            stats.added += 1
            return

    try:
        sanitized = EventSanitizer.sanitize(
            personal_ical,
            work_uid,
            mode="busy",
            keep_reminders=config.keep_reminders,
            source_uid=personal_uid,
        )

        if config.verbose:
            logger.debug(f"Sanitized iCal (busy mode):\n{sanitized.as_ical_string()}")

        # Create event and get actual UID
        actual_work_uid = work_client.create_event(sanitized)
        if actual_work_uid:
            work_uid = actual_work_uid
            logger.debug(f"Server assigned UID: {work_uid}")

        # Fetch the event back to get the actual stored version
        personal_hash = compute_hash(personal_ical)
        created_work = work_client.get_event(work_uid)
        if created_work:
            work_hash = compute_hash(created_work.as_ical_string())
        else:
            # Fallback if fetch fails
            work_hash = compute_hash(sanitized.as_ical_string())

        state_db.insert_bidirectional(work_uid, personal_uid, work_hash, personal_hash, "target")
        state_db.commit()
        stats.added += 1
        logger.debug(f"Created work event {work_uid} from personal {personal_uid}")
    except (GLib.Error, CalendarSyncError) as e:
        logger.error(f"Failed to create work event from {personal_uid}: {e}")
        stats.errors += 1


def _process_sync_pair(
    config: SyncConfig,
    stats: SyncStats,
    logger,
    state_record,
    work_events: dict[str, ICalGLib.Component],
    personal_events: dict[str, ICalGLib.Component],
    work_client: EDSCalendarClient,
    personal_client: EDSCalendarClient,
    state_db: StateDatabase,
    work_valid_exception_dates: dict[str, set[str]] | None = None,
):
    """Process an existing sync pair (check for changes/deletions)."""
    work_uid = state_record["source_uid"]  # DB uses 'source' for work
    personal_uid = state_record["target_uid"]  # DB uses 'target' for personal
    origin = state_record["origin"]
    stored_work_hash = state_record["source_hash"]
    stored_personal_hash = state_record["target_hash"]

    work_exists = work_uid in work_events
    personal_exists = personal_uid in personal_events

    # Handle deletions
    if not work_exists and not personal_exists:
        # Both deleted, just clean up state
        logger.debug(f"Both events deleted: {work_uid} <-> {personal_uid}")
        if not config.dry_run:
            state_db.delete_by_pair(work_uid, personal_uid)
            state_db.commit()
        return

    if not work_exists:
        # Work event deleted
        if origin == "source":
            # Work was authoritative → delete the personal mirror
            if config.dry_run:
                logger.info(
                    f"[DRY RUN] [WORK→PERSONAL] Would DELETE: {personal_uid} (work deleted)"
                )
                stats.deleted += 1
            else:
                try:
                    personal_client.remove_event(personal_uid)
                    logger.debug(f"Deleted personal event {personal_uid} (work deleted)")
                    stats.deleted += 1
                except (GLib.Error, CalendarSyncError) as e:
                    logger.error(f"Failed to delete personal {personal_uid}: {e}")
                    stats.errors += 1
            # Clean up state
            if not config.dry_run:
                state_db.delete_by_pair(work_uid, personal_uid)
                state_db.commit()
        else:
            # Personal was authoritative → work was deleted externally; recreate it.
            # (work_uid is already in work_uids_processed after this call returns,
            # so we must handle recreation here rather than deferring to Phase 2.)
            if config.dry_run:
                logger.info(
                    f"[DRY RUN] [PERSONAL→WORK] Would RECREATE: {work_uid} (work manually deleted)"
                )
                stats.added += 1
            else:
                state_db.delete_by_pair(work_uid, personal_uid)
                state_db.commit()
                _process_new_personal_event(
                    config,
                    stats,
                    logger,
                    personal_uid,
                    personal_events[personal_uid],
                    work_client,
                    state_db,
                )
        return

    if not personal_exists:
        # Personal event deleted
        if origin == "target":
            # Personal was authoritative → delete the work mirror
            if config.dry_run:
                logger.info(
                    f"[DRY RUN] [PERSONAL→WORK] Would DELETE: {work_uid} (personal deleted)"
                )
                stats.deleted += 1
            else:
                try:
                    work_client.remove_event(work_uid)
                    logger.debug(f"Deleted work event {work_uid} (personal deleted)")
                    stats.deleted += 1
                except (GLib.Error, CalendarSyncError) as e:
                    logger.error(f"Failed to delete work {work_uid}: {e}")
                    stats.errors += 1
            # Clean up state
            if not config.dry_run:
                state_db.delete_by_pair(work_uid, personal_uid)
                state_db.commit()
        else:
            # Work was authoritative → personal was deleted externally; recreate it.
            # (personal_uid is already in personal_uids_processed after this call
            # returns, so we must handle recreation here rather than deferring to
            # Phase 3.)
            if config.dry_run:
                logger.info(
                    f"[DRY RUN] [WORK→PERSONAL] Would RECREATE: "
                    f"{personal_uid} (personal manually deleted)"
                )
                stats.added += 1
            else:
                state_db.delete_by_pair(work_uid, personal_uid)
                state_db.commit()
                _process_new_work_event(
                    config,
                    stats,
                    logger,
                    work_uid,
                    work_events[work_uid],
                    personal_client,
                    state_db,
                    valid_exception_dates=(work_valid_exception_dates or {}).get(work_uid, set()),
                )
        return

    # Both events exist - check for updates
    work_comp = work_events[work_uid]
    personal_comp = personal_events[personal_uid]

    work_ical = work_comp.as_ical_string()
    personal_ical = personal_comp.as_ical_string()

    # Strip phantom EXDATEs before hashing and syncing.  Exchange adds every
    # explicitly-defined occurrence to the master's EXDATE list even when the
    # occurrence is a real meeting (exception VEVENT with RECURRENCE-ID).
    # We strip those dates so the personal calendar shows the correct occurrences.
    # Using the stripped iCal for the work hash ensures a one-time re-sync on the
    # first run after this fix (old hash was based on the unstripped iCal).
    work_ical = strip_exdates_for_dates(
        work_ical, (work_valid_exception_dates or {}).get(work_uid, set())
    )

    current_work_hash = compute_hash(work_ical)
    current_personal_hash = compute_hash(personal_ical)

    # Debug: Log hash mismatches
    if config.verbose:
        if current_work_hash != stored_work_hash:
            logger.debug(f"Work hash mismatch for {work_uid}")
            logger.debug(f"  Stored: {stored_work_hash}")
            logger.debug(f"  Current: {current_work_hash}")
        if current_personal_hash != stored_personal_hash:
            logger.debug(f"Personal hash mismatch for {personal_uid}")
            logger.debug(f"  Stored: {stored_personal_hash}")
            logger.debug(f"  Current: {current_personal_hash}")

    if origin == "source":  # DB uses 'source' for work origin
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

            if config.dry_run:
                logger.info(
                    f"[DRY RUN] [WORK→PERSONAL] Would UPDATE: "
                    f"{work_uid} -> {personal_uid} ({reason_str})"
                )
                stats.modified += 1
            else:
                try:
                    sanitized = EventSanitizer.sanitize(
                        work_ical,
                        personal_uid,
                        mode="normal",
                        keep_reminders=config.keep_reminders,
                        source_uid=work_uid,
                    )
                    personal_client.modify_event(sanitized)

                    # Fetch the event back to get the actual stored version
                    # (server may have added/modified properties)
                    updated_personal = personal_client.get_event(personal_uid)
                    if updated_personal:
                        new_personal_ical = updated_personal.as_ical_string()
                        new_personal_hash = compute_hash(new_personal_ical)
                    else:
                        # Fallback if fetch fails
                        new_personal_ical = sanitized.as_ical_string()
                        new_personal_hash = compute_hash(new_personal_ical)

                    state_db.update_hashes(
                        work_uid, personal_uid, current_work_hash, new_personal_hash
                    )
                    state_db.commit()
                    stats.modified += 1
                    logger.debug(
                        f"Updated personal {personal_uid} from work {work_uid} ({reason_str})"
                    )
                except (GLib.Error, CalendarSyncError) as e:
                    logger.error(f"Failed to update personal {personal_uid}: {e}")
                    stats.errors += 1

    elif origin == "target":  # DB uses 'target' for personal origin
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

            if config.dry_run:
                logger.info(
                    f"[DRY RUN] [PERSONAL→WORK] Would UPDATE: "
                    f"{personal_uid} -> {work_uid} ({reason_str})"
                )
                stats.modified += 1
            else:
                try:
                    sanitized = EventSanitizer.sanitize(
                        personal_ical,
                        work_uid,
                        mode="busy",
                        keep_reminders=config.keep_reminders,
                        source_uid=personal_uid,
                    )
                    work_client.modify_event(sanitized)

                    # Fetch the event back to get the actual stored version
                    # (server may have added/modified properties)
                    updated_work = work_client.get_event(work_uid)
                    if updated_work:
                        new_work_ical = updated_work.as_ical_string()
                        new_work_hash = compute_hash(new_work_ical)
                    else:
                        # Fallback if fetch fails
                        new_work_ical = sanitized.as_ical_string()
                        new_work_hash = compute_hash(new_work_ical)

                    state_db.update_hashes(
                        work_uid, personal_uid, new_work_hash, current_personal_hash
                    )
                    state_db.commit()
                    stats.modified += 1
                    logger.debug(
                        f"Updated work {work_uid} from personal {personal_uid} ({reason_str})"
                    )
                except (GLib.Error, CalendarSyncError) as e:
                    logger.error(f"Failed to update work {work_uid}: {e}")
                    stats.errors += 1


def run_two_way(
    config: SyncConfig,
    stats: SyncStats,
    logger,
    work_client: EDSCalendarClient,
    personal_client: EDSCalendarClient,
    state_db: StateDatabase,
):
    """Execute bidirectional synchronization."""
    try:
        # Handle refresh mode
        if config.refresh:
            perform_refresh_two_way(config, stats, logger, work_client, personal_client, state_db)

        logger.info("Loading sync state...")
        state_records = state_db.get_all_state_bidirectional()

        # Pre-sync orphan scans: find managed events in each calendar that
        # lack a DB record (created by a previous run that crashed before commit).
        logger.info("Scanning personal calendar for orphaned managed events...")
        personal_orphan_index = build_orphan_index(personal_client, state_db, logger)
        logger.info("Scanning work calendar for orphaned managed events...")
        work_orphan_index = build_orphan_index(work_client, state_db, logger)

        # Fetch all events from both calendars
        logger.info("Fetching work events...")
        work_events_list = work_client.get_all_events()
        work_events: dict[str, ICalGLib.Component] = {}
        for obj in work_events_list:
            comp = parse_component(obj)
            # Keep only master VEVENTs (no RECURRENCE-ID).  Exception VEVENTs
            # share the same UID as the master and would overwrite it.  Their
            # contribution is captured via work_valid_exception_dates below.
            if comp.get_first_property(ICalGLib.PropertyKind.RECURRENCEID_PROPERTY):
                continue
            work_events[comp.get_uid()] = comp

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
                work_valid_exception_dates.setdefault(_uid, set()).add(_rid_date)
            except Exception:
                pass
        if work_valid_exception_dates:
            logger.debug(
                f"Found valid exception dates for {len(work_valid_exception_dates)} work UIDs"
            )

        logger.info("Fetching personal events...")
        personal_events_list = personal_client.get_all_events()
        personal_events: dict[str, ICalGLib.Component] = {}
        for obj in personal_events_list:
            comp = parse_component(obj)
            if comp.get_first_property(ICalGLib.PropertyKind.RECURRENCEID_PROPERTY):
                continue
            personal_events[comp.get_uid()] = comp

        logger.info(
            f"Processing {len(work_events)} work events, "
            f"{len(personal_events)} personal events, "
            f"{len(state_records)} sync pairs..."
        )

        # Track which events we've processed
        work_uids_processed = set()
        personal_uids_processed = set()

        # Phase 1: Process existing sync pairs
        for state_record in state_records:
            work_uid = state_record["source_uid"]  # 'source' maps to 'work' in DB
            personal_uid = state_record["target_uid"]  # 'target' maps to 'personal' in DB

            _process_sync_pair(
                config,
                stats,
                logger,
                state_record,
                work_events,
                personal_events,
                work_client,
                personal_client,
                state_db,
                work_valid_exception_dates=work_valid_exception_dates,
            )

            work_uids_processed.add(work_uid)
            personal_uids_processed.add(personal_uid)

        # Phase 2: Process new work events (not yet synced)
        for work_uid, work_comp in work_events.items():
            if work_uid not in work_uids_processed:
                # Skip managed events — they are "Busy" blocks we created in
                # work from personal events.  Syncing them back to personal
                # would produce circular duplicates.
                if EventSanitizer.is_managed_event(work_comp):
                    logger.debug(f"Skipping managed work event: {work_uid}")
                    continue
                # Skip cancelled events — Exchange rejects creating them.
                if is_event_cancelled(work_comp):
                    logger.debug(f"Skipping cancelled work event: {work_uid}")
                    continue
                # Skip transparent (free-time) events — they don't block time
                # and should not appear as busy in the personal calendar.
                if is_free_time(work_comp):
                    logger.debug(f"Skipping transparent work event: {work_uid}")
                    continue
                # Skip recurring series where every occurrence is excluded
                # by EXDATE — Exchange rejects creating empty series.
                # Check against the stripped iCal so that series whose EXDATEs
                # are all "phantom" (covered by valid exception VEVENTs) are
                # not incorrectly skipped.
                _valid_ex_dates = work_valid_exception_dates.get(work_uid, set())
                if _valid_ex_dates:
                    _stripped = ICalGLib.Component.new_from_string(
                        strip_exdates_for_dates(work_comp.as_ical_string(), _valid_ex_dates)
                    )
                    _has_valid = has_valid_occurrences(_stripped)
                else:
                    _has_valid = has_valid_occurrences(work_comp)
                if not _has_valid:
                    logger.debug(f"Skipping empty recurring work event: {work_uid}")
                    continue
                _process_new_work_event(
                    config,
                    stats,
                    logger,
                    work_uid,
                    work_comp,
                    personal_client,
                    state_db,
                    orphan_index=personal_orphan_index,
                    valid_exception_dates=_valid_ex_dates,
                )

        # Phase 3: Process new personal events (not yet synced)
        for personal_uid, personal_comp in personal_events.items():
            if personal_uid not in personal_uids_processed:
                # Skip managed events — they are copies we created in personal
                # from work events.  Syncing them back to work would produce
                # circular duplicates.
                if EventSanitizer.is_managed_event(personal_comp):
                    logger.debug(f"Skipping managed personal event: {personal_uid}")
                    continue
                _process_new_personal_event(
                    config,
                    stats,
                    logger,
                    personal_uid,
                    personal_comp,
                    work_client,
                    state_db,
                    orphan_index=work_orphan_index,
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
