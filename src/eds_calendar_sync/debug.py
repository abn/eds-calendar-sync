"""
Debug/inspect tools for EDS calendar events.

Importable functions:
  list_calendars(registry, console)  — render a Rich table of all calendars
  dump_event(vevent, console, show_raw=True)  — render one event in a Rich Panel
"""

import gi
gi.require_version('EDataServer', '1.2')
gi.require_version('ECal', '2.0')
gi.require_version('ICalGLib', '3.0')
from gi.repository import EDataServer, ECal, ICalGLib, GLib  # noqa: F401

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text


def list_calendars(registry, console: Console) -> None:
    """Render all configured EDS calendars as a Rich table."""
    sources = registry.list_sources(EDataServer.SOURCE_EXTENSION_CALENDAR)

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Display Name", style="bold")
    table.add_column("Account")
    table.add_column("Mode")
    table.add_column("UID", style="dim")

    for source in sources:
        name = source.get_display_name() or "(unnamed)"
        uid = source.get_uid() or ""
        parent = source.get_parent()
        account = ""
        if parent:
            parent_source = registry.ref_source(parent)
            if parent_source:
                account = parent_source.get_display_name() or ""
        try:
            client = ECal.Client.connect_sync(source, ECal.ClientSourceType.EVENTS, 5, None)
            mode = "Read-write" if not client.is_readonly() else "Read-only"
            mode_style = "green" if not client.is_readonly() else "yellow"
        except Exception:
            mode = "Unknown"
            mode_style = "red"

        table.add_row(name, account, Text(mode, style=mode_style), uid)

    console.print(table)


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


def dump_event(vevent, console: Console, show_raw: bool = True) -> None:
    """Render a single VEVENT as a Rich Panel."""
    uid = vevent.get_uid() or "(no UID)"
    summary = fmt_prop(vevent, ICalGLib.PropertyKind.SUMMARY_PROPERTY,
                       lambda p: p.get_summary()) or "(no summary)"
    rid = fmt_prop(vevent, ICalGLib.PropertyKind.RECURRENCEID_PROPERTY,
                   lambda p: p.get_value_as_string())
    transp = fmt_prop(vevent, ICalGLib.PropertyKind.TRANSP_PROPERTY,
                      lambda p: p.get_value_as_string())
    status = fmt_prop(vevent, ICalGLib.PropertyKind.STATUS_PROPERTY,
                      lambda p: p.get_value_as_string())
    dtstart = fmt_prop(vevent, ICalGLib.PropertyKind.DTSTART_PROPERTY,
                       lambda p: p.get_value_as_string())
    dtend = fmt_prop(vevent, ICalGLib.PropertyKind.DTEND_PROPERTY,
                     lambda p: p.get_value_as_string())
    rrule = fmt_prop(vevent, ICalGLib.PropertyKind.RRULE_PROPERTY,
                     lambda p: p.get_value_as_string())
    exdates = collect_multi(vevent, ICalGLib.PropertyKind.EXDATE_PROPERTY,
                            lambda p: p.get_value_as_string())

    lines = Text()

    def row(label: str, value) -> None:
        if value is None:
            return
        lines.append(f"  {label:<14}: ", style="bold cyan")
        lines.append(f"{value}\n")

    row("SUMMARY", summary)
    row("UID", uid)
    row("RECURRENCE-ID", rid)
    row("DTSTART", dtstart)
    row("DTEND", dtend)
    if rrule:
        row("RRULE", rrule)
    for ex in exdates:
        row("EXDATE", ex)
    row("TRANSP", transp)
    row("STATUS", status)

    # X-properties
    x_prop = vevent.get_first_property(ICalGLib.PropertyKind.X_PROPERTY)
    while x_prop:
        name = x_prop.get_x_name() or ''
        val = x_prop.get_x() or x_prop.get_value_as_string() or ''
        lines.append(f"  {name:<14}: ", style="bold cyan")
        lines.append(f"{val}\n")
        x_prop = vevent.get_next_property(ICalGLib.PropertyKind.X_PROPERTY)

    # Attendees
    attendees = vevent.get_first_property(ICalGLib.PropertyKind.ATTENDEE_PROPERTY)
    while attendees:
        val = attendees.get_attendee() or ''
        ps_p = attendees.get_first_parameter(ICalGLib.ParameterKind.PARTSTAT_PARAMETER)
        partstat = ps_p.get_partstat() if ps_p else None
        rl_p = attendees.get_first_parameter(ICalGLib.ParameterKind.ROLE_PARAMETER)
        role = rl_p.get_role() if rl_p else None
        lines.append(f"  {'ATTENDEE':<14}: ", style="bold cyan")
        lines.append(f"{val}  PARTSTAT={partstat}  ROLE={role}\n")
        attendees = vevent.get_next_property(ICalGLib.PropertyKind.ATTENDEE_PROPERTY)

    console.print(Panel(lines, title=f"[bold]{summary}[/bold]", expand=False))

    if show_raw:
        raw = vevent.as_ical_string()
        console.print(Panel(
            Syntax(raw, "ical", theme="monokai", word_wrap=True),
            title="Raw iCal",
            expand=False,
        ))
