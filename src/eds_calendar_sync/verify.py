"""
Post-sync audit: verify that expected events appear in both calendars.
"""

import logging
import re
from datetime import date
from pathlib import Path

import gi

gi.require_version("EDataServer", "1.2")
gi.require_version("ICalGLib", "3.0")
from gi.repository import EDataServer
from gi.repository import ICalGLib
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from eds_calendar_sync.db import StateDatabase
from eds_calendar_sync.eds_client import EDSCalendarClient
from eds_calendar_sync.eds_client import get_calendar_display_info
from eds_calendar_sync.sanitizer import EventSanitizer
from eds_calendar_sync.sync.utils import compute_hash
from eds_calendar_sync.sync.utils import has_valid_occurrences
from eds_calendar_sync.sync.utils import is_event_cancelled
from eds_calendar_sync.sync.utils import is_free_time
from eds_calendar_sync.sync.utils import parse_component
from eds_calendar_sync.sync.utils import strip_exdates_for_dates

_logger = logging.getLogger(__name__)

# Duplicate private regex constants from utils.py to avoid importing private names.
_RRULE_UNTIL_RE = re.compile(r"UNTIL=(\d{8})")
_EXDATE_DATE_RE = re.compile(r"^EXDATE[^:\n]*:(\d{8})", re.MULTILINE)


def _has_occurrence_in_window(
    comp: ICalGLib.Component, window_start: date, window_end: date
) -> bool:
    """Return True if the event has at least one non-excluded occurrence in the date window.

    Mirrors the RecurIterator pattern from has_valid_occurrences() in utils.py,
    but instead of checking for any valid occurrence we check for an occurrence
    that falls within [window_start, window_end].

    Returns True on any API error (safe fallback — keeps the event visible).
    """
    win_start_str = window_start.strftime("%Y%m%d")
    win_end_str = window_end.strftime("%Y%m%d")

    try:
        # 1. Resolve VEVENT from VCALENDAR wrapper if needed.
        check = comp
        if comp.isa() == ICalGLib.ComponentKind.VCALENDAR_COMPONENT:
            check = comp.get_first_component(ICalGLib.ComponentKind.VEVENT_COMPONENT)
            if not check:
                return True  # safe fallback

        # 2. No RRULE → check if DTSTART falls in the window.
        rrule_prop = check.get_first_property(ICalGLib.PropertyKind.RRULE_PROPERTY)
        if not rrule_prop:
            try:
                dtstart = check.get_dtstart()
                dtstart_str = (
                    f"{dtstart.get_year():04d}{dtstart.get_month():02d}{dtstart.get_day():02d}"
                )
                return win_start_str <= dtstart_str <= win_end_str
            except Exception:
                return True

        # 3. Recurring event — expand and search.

        # a. Collect EXDATEs (same two-path fallback as has_valid_occurrences).
        exdates: set[str] = set()
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
            # Fallback: parse the root component's iCal string directly.
            try:
                for m in _EXDATE_DATE_RE.finditer(comp.as_ical_string() or ""):
                    exdates.add(m.group(1))
            except Exception:
                pass

        # b. Build floating dtstart for RecurIterator (TZID-safety workaround).
        dtstart = check.get_dtstart()
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

        # c. Parse UNTIL from iCal string (same rationale as has_valid_occurrences).
        until_str = None
        try:
            m = _RRULE_UNTIL_RE.search(comp.as_ical_string() or "")
            if m:
                until_str = m.group(1)
        except Exception:
            pass

        # d. Iterate recurrences; cap at 1000 for safety.
        rule = rrule_prop.get_rrule()
        it = ICalGLib.RecurIterator.new(rule, dtstart_for_iter)
        for _ in range(1000):
            occ = it.next()
            if occ is None or occ.is_null_time():
                break
            occ_key = f"{occ.get_year():04d}{occ.get_month():02d}{occ.get_day():02d}"
            if until_str and occ_key > until_str:
                break  # Past UNTIL — series has ended
            if occ_key in exdates:
                continue
            if occ_key < win_start_str:
                continue  # Before window — keep looking
            if occ_key <= win_end_str:
                return True  # In window
            # occ_key > win_end_str — past window end; no future occurrence can match
            return False

    except Exception:
        return True  # Safe fallback: include the event

    return False


def _get_summary(comp: ICalGLib.Component) -> str:
    """Extract SUMMARY from a VEVENT or VCALENDAR component."""
    check = comp
    if comp.isa() == ICalGLib.ComponentKind.VCALENDAR_COMPONENT:
        check = comp.get_first_component(ICalGLib.ComponentKind.VEVENT_COMPONENT)
        if not check:
            return "(no summary)"
    sp = check.get_first_property(ICalGLib.PropertyKind.SUMMARY_PROPERTY)
    return (sp.get_summary() or "(no summary)") if sp else "(no summary)"


def _get_date_str(comp: ICalGLib.Component) -> str:
    """Extract DTSTART as YYYY-MM-DD from a VEVENT or VCALENDAR component."""
    check = comp
    if comp.isa() == ICalGLib.ComponentKind.VCALENDAR_COMPONENT:
        check = comp.get_first_component(ICalGLib.ComponentKind.VEVENT_COMPONENT)
        if not check:
            return "?"
    dp = check.get_first_property(ICalGLib.PropertyKind.DTSTART_PROPERTY)
    if not dp:
        return "?"
    try:
        t = dp.get_dtstart()
        return f"{t.get_year():04d}-{t.get_month():02d}-{t.get_day():02d}"
    except Exception:
        return "?"


def _short_uid(uid: str, max_len: int = 40) -> str:
    """Truncate a UID for display."""
    if len(uid) <= max_len:
        return uid
    return uid[:max_len] + "…"


def run_verify(
    work_calendar_id: str,
    personal_calendar_id: str,
    state_db_path: Path,
    console: Console,
    window_start: date,
    window_end: date,
) -> bool:
    """Verify sync completeness between work and personal calendars.

    Fetches events from both calendars, applies the same eligibility guards
    as sync, cross-references the state DB, and reports issues.

    Returns True if all eligible work events are confirmed in the personal
    calendar (exit code 0), False if any issues are found (exit code 1).
    """
    # ------------------------------------------------------------------
    # Step 1 — Connect & fetch
    # ------------------------------------------------------------------
    registry = EDataServer.SourceRegistry.new_sync(None)
    work_client = EDSCalendarClient(registry, work_calendar_id)
    personal_client = EDSCalendarClient(registry, personal_calendar_id)
    work_client.connect()
    personal_client.connect()

    work_name, work_account, _ = get_calendar_display_info(work_calendar_id)
    personal_name, personal_account, _ = get_calendar_display_info(personal_calendar_id)
    work_display = work_name + (f" ({work_account})" if work_account else "")
    personal_display = personal_name + (f" ({personal_account})" if personal_account else "")

    info = Text()
    info.append("  Work:      ", style="bold")
    info.append(f"{work_display}\n")
    info.append("  Personal:  ", style="bold")
    info.append(f"{personal_display}\n")
    info.append("  Window:    ", style="bold")
    info.append(f"{window_start} → {window_end}")
    console.print(Panel(info, title="[bold]EDS Calendar Sync — Verify[/bold]"))

    work_events_list = work_client.get_all_events()
    personal_events_list = personal_client.get_all_events()

    # ------------------------------------------------------------------
    # Step 2 — Build work event map (mirrors two_way.py lines 477–533)
    # ------------------------------------------------------------------
    work_events: dict[str, ICalGLib.Component] = {}
    for obj in work_events_list:
        comp = parse_component(obj)
        # Keep only master VEVENTs (no RECURRENCE-ID).
        if comp.get_first_property(ICalGLib.PropertyKind.RECURRENCEID_PROPERTY):
            continue
        uid = comp.get_uid()
        if uid:
            work_events[uid] = comp

    # Build map from work UID → set of YYYYMMDD dates that have a valid
    # (non-managed, non-cancelled, non-free) exception VEVENT.
    # Also detect rescheduled exceptions and add them with compound keys.
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

    _logger.debug(
        "Verify: %d work events total (%d rescheduled exceptions), %d UIDs with phantom EXDATEs",
        len(work_events),
        sum(1 for k in work_events if "::RID::" in k),
        len(work_valid_exception_dates),
    )

    # ------------------------------------------------------------------
    # Step 3 — Apply eligibility guards + window filter
    # ------------------------------------------------------------------
    eligible_work: dict[str, ICalGLib.Component] = {}
    for uid, comp in work_events.items():
        if EventSanitizer.is_managed_event(comp):
            continue
        if is_event_cancelled(comp):
            continue
        if is_free_time(comp):
            continue
        if not has_valid_occurrences(comp):
            continue
        if not _has_occurrence_in_window(comp, window_start, window_end):
            continue
        eligible_work[uid] = comp

    _logger.debug(
        "Verify: %d eligible work events in window %s to %s",
        len(eligible_work),
        window_start,
        window_end,
    )

    # ------------------------------------------------------------------
    # Step 4 — Build personal map (masters only, all personal events)
    # ------------------------------------------------------------------
    personal_events: dict[str, ICalGLib.Component] = {}
    for obj in personal_events_list:
        comp = parse_component(obj)
        if comp.get_first_property(ICalGLib.PropertyKind.RECURRENCEID_PROPERTY):
            continue
        uid = comp.get_uid()
        if uid:
            personal_events[uid] = comp

    # ------------------------------------------------------------------
    # Step 5 — Load state DB
    # ------------------------------------------------------------------
    with StateDatabase(state_db_path, work_calendar_id, personal_calendar_id) as state_db:
        all_state_records = state_db.get_all_state_bidirectional()

    # Build lookup dicts from state records.
    # origin='source' records: work event → personal managed copy
    #   source_uid = work event UID, target_uid = personal managed event UID
    # origin='target' records: personal event → work managed copy
    #   source_uid = personal event UID, target_uid = work managed event UID
    work_to_personal: dict[str, object] = {}  # work_uid → row
    db_by_personal_uid: dict[str, object] = {}  # personal_uid → row (managed copies only)
    personal_to_work_db: dict[str, object] = {}  # personal_uid → row (user events synced to work)
    for row in all_state_records:
        if row["origin"] == "source":
            work_to_personal[row["source_uid"]] = row
            db_by_personal_uid[row["target_uid"]] = row
        elif row["origin"] == "target":
            personal_to_work_db[row["source_uid"]] = row

    # ------------------------------------------------------------------
    # Step 6 — Compute delta
    # ------------------------------------------------------------------
    missing: list[tuple[str, ICalGLib.Component]] = []
    orphaned_db: list[tuple[str, ICalGLib.Component, str]] = []
    stale: list[tuple[str, ICalGLib.Component, str]] = []
    ok_count = 0

    orphaned_personal: list[tuple[str, ICalGLib.Component]] = []
    orphaned_source: list[tuple[str, ICalGLib.Component, str]] = []
    # Personal → work direction (origin='target' records)
    p2w_target_gone: list[tuple[str, ICalGLib.Component, str]] = []
    p2w_ok_count = 0
    p2w_total = 0

    # Work → personal direction (primary)
    for uid, comp in eligible_work.items():
        if uid not in work_to_personal:
            missing.append((uid, comp))
            continue

        row = work_to_personal[uid]
        personal_uid = row["target_uid"]

        if personal_uid not in personal_events:
            orphaned_db.append((uid, comp, personal_uid))
            continue

        # Hash check: compare current work event hash with stored source_hash.
        try:
            base_uid = uid.split("::RID::")[0] if "::RID::" in uid else uid
            dates_to_strip = work_valid_exception_dates.get(base_uid, set())
            current_ical = comp.as_ical_string()
            stripped_ical = strip_exdates_for_dates(current_ical, dates_to_strip)
            current_hash = compute_hash(stripped_ical)
            if current_hash != row["source_hash"]:
                stale.append((uid, comp, personal_uid))
                continue
        except Exception as e:
            _logger.debug("Verify: hash computation failed for %s: %s", uid, e)

        ok_count += 1

    # Personal managed-copy cross-check: detect orphaned personal copies of work events.
    # These are origin='source' managed events that have lost their DB record or work source.
    for personal_uid, comp in personal_events.items():
        if not EventSanitizer.is_managed_event(comp):
            continue
        if not _has_occurrence_in_window(comp, window_start, window_end):
            continue

        if personal_uid not in db_by_personal_uid:
            orphaned_personal.append((personal_uid, comp))
            continue

        row = db_by_personal_uid[personal_uid]
        expected_work_uid = row["source_uid"]
        if expected_work_uid not in work_events:
            orphaned_source.append((personal_uid, comp, expected_work_uid))

    # Personal → work direction: check origin='target' records (user-created personal events
    # that have been synced to the work calendar). For each, verify the work copy still exists.
    for personal_uid, row in personal_to_work_db.items():
        if personal_uid not in personal_events:
            continue  # personal source deleted; no window to filter on, skip silently
        comp = personal_events[personal_uid]
        if not _has_occurrence_in_window(comp, window_start, window_end):
            continue

        p2w_total += 1
        work_target_uid = row["target_uid"]
        if work_target_uid not in work_events:
            p2w_target_gone.append((personal_uid, comp, work_target_uid))
        else:
            p2w_ok_count += 1

    # ------------------------------------------------------------------
    # Step 7 — Display results
    # ------------------------------------------------------------------
    total_eligible = len(eligible_work)
    issues = bool(
        missing or orphaned_db or stale or orphaned_personal or orphaned_source or p2w_target_gone
    )

    if not issues:
        console.print(
            f"[bold green]✓[/] All [bold]{total_eligible}[/bold] work event(s) "
            f"confirmed in personal calendar."
        )
        if p2w_total > 0:
            console.print(
                f"[bold green]✓[/] All [bold]{p2w_total}[/bold] personal event(s) "
                f"confirmed synced to work calendar."
            )
        return True

    if missing:
        t = Table(
            title="[bold red]MISSING[/] — eligible work events never synced to personal",
            show_header=True,
            header_style="bold",
        )
        t.add_column("Summary", overflow="fold", min_width=30)
        t.add_column("Date", width=12)
        t.add_column("Work UID", overflow="fold")
        for uid, comp in missing:
            t.add_row(_get_summary(comp), _get_date_str(comp), _short_uid(uid))
        console.print(t)

    if orphaned_db:
        t = Table(
            title="[bold yellow]ORPHANED_DB[/] — DB record exists but personal event was deleted",
            show_header=True,
            header_style="bold",
        )
        t.add_column("Summary", overflow="fold", min_width=30)
        t.add_column("Date", width=12)
        t.add_column("Work UID", overflow="fold")
        t.add_column("Expected personal UID", overflow="fold")
        for uid, comp, personal_uid in orphaned_db:
            t.add_row(
                _get_summary(comp),
                _get_date_str(comp),
                _short_uid(uid),
                _short_uid(personal_uid),
            )
        console.print(t)

    if stale:
        t = Table(
            title="[bold cyan]STALE[/] — work event changed since last sync",
            show_header=True,
            header_style="bold",
        )
        t.add_column("Summary", overflow="fold", min_width=30)
        t.add_column("Date", width=12)
        t.add_column("Work UID", overflow="fold")
        t.add_column("Personal UID", overflow="fold")
        for uid, comp, personal_uid in stale:
            t.add_row(
                _get_summary(comp),
                _get_date_str(comp),
                _short_uid(uid),
                _short_uid(personal_uid),
            )
        console.print(t)

    if orphaned_personal:
        t = Table(
            title="[bold magenta]ORPHANED_PERSONAL[/] — managed personal event with no DB record",
            show_header=True,
            header_style="bold",
        )
        t.add_column("Summary", overflow="fold", min_width=30)
        t.add_column("Date", width=12)
        t.add_column("Personal UID", overflow="fold")
        for personal_uid, comp in orphaned_personal:
            t.add_row(_get_summary(comp), _get_date_str(comp), _short_uid(personal_uid))
        console.print(t)

    if orphaned_source:
        t = Table(
            title="[bold red]ORPHANED_SOURCE[/] — work source deleted but personal copy remains",
            show_header=True,
            header_style="bold",
        )
        t.add_column("Summary", overflow="fold", min_width=30)
        t.add_column("Date", width=12)
        t.add_column("Personal UID", overflow="fold")
        t.add_column("Expected work UID", overflow="fold")
        for personal_uid, comp, expected_work_uid in orphaned_source:
            t.add_row(
                _get_summary(comp),
                _get_date_str(comp),
                _short_uid(personal_uid),
                _short_uid(expected_work_uid),
            )
        console.print(t)

    if p2w_target_gone:
        t = Table(
            title="[bold red]P2W_TARGET_GONE[/] — work copy deleted for a personal event",
            show_header=True,
            header_style="bold",
        )
        t.add_column("Summary", overflow="fold", min_width=30)
        t.add_column("Date", width=12)
        t.add_column("Personal UID", overflow="fold")
        t.add_column("Expected work UID", overflow="fold")
        for personal_uid, comp, work_target_uid in p2w_target_gone:
            t.add_row(
                _get_summary(comp),
                _get_date_str(comp),
                _short_uid(personal_uid),
                _short_uid(work_target_uid),
            )
        console.print(t)

    total_issues = (
        len(missing)
        + len(orphaned_db)
        + len(stale)
        + len(orphaned_personal)
        + len(orphaned_source)
        + len(p2w_target_gone)
    )
    summary = f"\n[bold]{ok_count}/{total_eligible}[/bold] work event(s) OK"
    if p2w_total > 0:
        summary += f"\n[bold]{p2w_ok_count}/{p2w_total}[/bold] personal event(s) synced to work OK"
    summary += f"\n[bold red]{total_issues}[/bold red] issue(s) found."
    console.print(summary)
    return False
