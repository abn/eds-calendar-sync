#!/usr/bin/env python3
"""
Debug tool: inspect EDS calendar events.

Usage:
    # List all configured calendars
    ./debug-calendar.py --list

    # Dump all events in a calendar
    ./debug-calendar.py <calendar-uid>

    # Filter by title substring (case-insensitive)
    ./debug-calendar.py <calendar-uid> --title "team meeting"

    # Filter by UID substring
    ./debug-calendar.py <calendar-uid> --uid "AAMkA"

    # Hide the raw iCal block (show summary only)
    ./debug-calendar.py <calendar-uid> --title "foo" --no-raw

    # Show only events with RECURRENCE-ID (exception VEVENTs)
    ./debug-calendar.py <calendar-uid> --exceptions-only

    # Show only master events (no RECURRENCE-ID)
    ./debug-calendar.py <calendar-uid> --masters-only
"""

import sys
import argparse
import gi
gi.require_version('EDataServer', '1.2')
gi.require_version('ECal', '2.0')
gi.require_version('ICalGLib', '3.0')
from gi.repository import EDataServer, ECal, ICalGLib, GLib


def list_calendars(registry):
    sources = registry.list_sources(EDataServer.SOURCE_EXTENSION_CALENDAR)
    print(f"{'Display Name':<35} {'Account':<25} {'Mode':<12} {'UID'}")
    print("-" * 112)
    for source in sources:
        name = source.get_display_name() or "(unnamed)"
        uid  = source.get_uid() or ""
        parent = source.get_parent()
        account = ""
        if parent:
            parent_source = registry.ref_source(parent)
            if parent_source:
                account = parent_source.get_display_name() or ""
        try:
            client = ECal.Client.connect_sync(source, ECal.ClientSourceType.EVENTS, 5, None)
            mode = "Read-only" if client.is_readonly() else "Read-write"
        except Exception:
            mode = "Unknown"
        print(f"{name:<35} {account:<25} {mode:<12} {uid}")


def fmt_prop(vevent, kind, getter):
    prop = vevent.get_first_property(kind)
    if not prop:
        return None
    try:
        return getter(prop)
    except Exception:
        return prop.get_value_as_string()


def collect_multi(vevent, kind, getter):
    """Collect all values for a repeating property (e.g. EXDATE, ATTENDEE)."""
    results = []
    prop = vevent.get_first_property(kind)
    while prop:
        try:
            results.append(getter(prop))
        except Exception:
            v = prop.get_value_as_string()
            if v:
                results.append(v)
        prop = vevent.get_next_property(kind)
    return results


def dump_event(vevent, show_raw=True):
    uid     = vevent.get_uid() or "(no UID)"
    summary = fmt_prop(vevent, ICalGLib.PropertyKind.SUMMARY_PROPERTY,
                       lambda p: p.get_summary())
    rid     = fmt_prop(vevent, ICalGLib.PropertyKind.RECURRENCEID_PROPERTY,
                       lambda p: p.get_value_as_string())
    transp  = fmt_prop(vevent, ICalGLib.PropertyKind.TRANSP_PROPERTY,
                       lambda p: p.get_value_as_string())
    status  = fmt_prop(vevent, ICalGLib.PropertyKind.STATUS_PROPERTY,
                       lambda p: p.get_value_as_string())
    dtstart = fmt_prop(vevent, ICalGLib.PropertyKind.DTSTART_PROPERTY,
                       lambda p: p.get_value_as_string())
    dtend   = fmt_prop(vevent, ICalGLib.PropertyKind.DTEND_PROPERTY,
                       lambda p: p.get_value_as_string())
    rrule   = fmt_prop(vevent, ICalGLib.PropertyKind.RRULE_PROPERTY,
                       lambda p: p.get_value_as_string())
    exdates = collect_multi(vevent, ICalGLib.PropertyKind.EXDATE_PROPERTY,
                            lambda p: p.get_value_as_string())

    print(f"SUMMARY      : {summary}")
    print(f"UID          : {uid}")
    print(f"RECURRENCE-ID: {rid}")
    print(f"DTSTART      : {dtstart}")
    print(f"DTEND        : {dtend}")
    if rrule:
        print(f"RRULE        : {rrule}")
    for ex in exdates:
        print(f"EXDATE       : {ex}")
    print(f"TRANSP       : {transp}")
    print(f"STATUS       : {status}")

    # X-properties
    x_prop = vevent.get_first_property(ICalGLib.PropertyKind.X_PROPERTY)
    while x_prop:
        name = x_prop.get_x_name() or ''
        val  = x_prop.get_x() or x_prop.get_value_as_string() or ''
        print(f"  {name}: {val}")
        x_prop = vevent.get_next_property(ICalGLib.PropertyKind.X_PROPERTY)

    # Attendees
    attendees = vevent.get_first_property(ICalGLib.PropertyKind.ATTENDEE_PROPERTY)
    while attendees:
        val = attendees.get_attendee() or ''
        ps_p = attendees.get_first_parameter(ICalGLib.ParameterKind.PARTSTAT_PARAMETER)
        partstat = ps_p.get_partstat() if ps_p else None
        rl_p = attendees.get_first_parameter(ICalGLib.ParameterKind.ROLE_PARAMETER)
        role = rl_p.get_role() if rl_p else None
        print(f"  ATTENDEE: {val}  PARTSTAT={partstat}  ROLE={role}")
        attendees = vevent.get_next_property(ICalGLib.PropertyKind.ATTENDEE_PROPERTY)

    if show_raw:
        print("\n--- Raw iCal ---")
        print(vevent.as_ical_string())


def main():
    parser = argparse.ArgumentParser(
        description="Inspect EDS calendar events for debugging.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("calendar_uid", nargs="?", help="Calendar UID to inspect")
    parser.add_argument("--list", action="store_true",
                        help="List all configured calendars and exit")
    parser.add_argument("--title", metavar="STR",
                        help="Filter events whose SUMMARY contains STR (case-insensitive)")
    parser.add_argument("--uid", metavar="STR",
                        help="Filter events whose UID contains STR (case-insensitive)")
    parser.add_argument("--no-raw", action="store_true",
                        help="Omit the raw iCal block from output")
    parser.add_argument("--exceptions-only", action="store_true",
                        help="Show only exception VEVENTs (have RECURRENCE-ID)")
    parser.add_argument("--masters-only", action="store_true",
                        help="Show only master VEVENTs (no RECURRENCE-ID)")

    args = parser.parse_args()

    registry = EDataServer.SourceRegistry.new_sync(None)

    if args.list:
        list_calendars(registry)
        return

    if not args.calendar_uid:
        parser.error("calendar_uid is required (or use --list to see available calendars)")

    source = registry.ref_source(args.calendar_uid)
    if not source:
        print(f"ERROR: Calendar {args.calendar_uid} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Calendar : {source.get_display_name()} ({args.calendar_uid})")
    client = ECal.Client.connect_sync(source, ECal.ClientSourceType.EVENTS, 30, None)
    _, objects = client.get_object_list_sync("#t", None)
    print(f"Events   : {len(objects)} total")

    title_filter = args.title.lower() if args.title else None
    uid_filter   = args.uid.lower()   if args.uid   else None

    count = 0
    for obj in objects:
        comp = ICalGLib.Component.new_from_string(obj) if isinstance(obj, str) else obj

        vevent = comp
        if comp.isa() == ICalGLib.ComponentKind.VCALENDAR_COMPONENT:
            vevent = comp.get_first_component(ICalGLib.ComponentKind.VEVENT_COMPONENT)
            if not vevent:
                continue

        # Apply filters
        if title_filter:
            sp = vevent.get_first_property(ICalGLib.PropertyKind.SUMMARY_PROPERTY)
            summary = (sp.get_summary() or '') if sp else ''
            if title_filter not in summary.lower():
                continue

        if uid_filter:
            if uid_filter not in (vevent.get_uid() or '').lower():
                continue

        has_rid = vevent.get_first_property(
            ICalGLib.PropertyKind.RECURRENCEID_PROPERTY
        ) is not None

        if args.exceptions_only and not has_rid:
            continue
        if args.masters_only and has_rid:
            continue

        count += 1
        print(f"\n{'='*70}")
        dump_event(vevent, show_raw=not args.no_raw)

    print(f"\n{'-'*70}")
    print(f"Matched {count} event(s)")


if __name__ == "__main__":
    main()
