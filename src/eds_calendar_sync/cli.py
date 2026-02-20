"""
Command-line interface for EDS Calendar Sync.
"""

import sys
import logging
from dataclasses import dataclass, field
from pathlib import Path
from configparser import ConfigParser
from typing import Dict, Optional
from typing_extensions import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .models import DEFAULT_STATE_DB, DEFAULT_CONFIG, SyncConfig, CalendarSyncError
from .db import migrate_calendar_ids_in_db
from .eds_client import get_calendar_display_info
from .sync import CalendarSynchronizer

# ---------------------------------------------------------------------------
# Typer app
# ---------------------------------------------------------------------------

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Bidirectional calendar sync between work and personal calendars via EDS.",
)

console = Console()


# ---------------------------------------------------------------------------
# Global state shared across subcommands
# ---------------------------------------------------------------------------

@dataclass
class _State:
    config_path: Path = field(default_factory=lambda: DEFAULT_CONFIG)
    state_db: Path = field(default_factory=lambda: DEFAULT_STATE_DB)
    verbose: bool = False


state = _State()


@app.callback()
def _global(
    config: Annotated[
        Path,
        typer.Option("--config", "-c", help=f"Config file path (default: {DEFAULT_CONFIG})"),
    ] = DEFAULT_CONFIG,
    state_db: Annotated[
        Path,
        typer.Option("--state-db", help=f"State DB path (default: {DEFAULT_STATE_DB})"),
    ] = DEFAULT_STATE_DB,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose debug output"),
    ] = False,
) -> None:
    state.config_path = config
    state.state_db = state_db
    state.verbose = verbose
    _setup_logging(verbose)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False, console=console)],
    )


def _load_config_file(config_path: Path) -> Dict[str, str]:
    if not config_path.exists():
        return {}
    parser = ConfigParser()
    parser.read(config_path)
    if "calendar-sync" not in parser:
        return {}
    return dict(parser["calendar-sync"])


def _build_config(
    work_calendar: Optional[str],
    personal_calendar: Optional[str],
    to_personal: bool,
    to_work: bool,
    dry_run: bool,
    refresh: bool,
    clear: bool,
    yes: bool,
) -> SyncConfig:
    if to_personal and to_work:
        raise typer.BadParameter("--to-personal and --to-work are mutually exclusive")

    config_file = _load_config_file(state.config_path)
    work_id = work_calendar or config_file.get("work_calendar_id")
    personal_id = personal_calendar or config_file.get("personal_calendar_id")

    if not work_id or not personal_id:
        console.print(
            "[bold red]Error:[/] Work and personal calendar IDs must be provided via "
            "[cyan]--work-calendar[/]/[cyan]--personal-calendar[/] or in the config file."
        )
        raise typer.Exit(1)

    if to_personal:
        direction = "to-personal"
    elif to_work:
        direction = "to-work"
    else:
        direction = "both"

    return SyncConfig(
        work_calendar_id=work_id,
        personal_calendar_id=personal_id,
        state_db_path=state.state_db,
        dry_run=dry_run,
        refresh=refresh,
        verbose=state.verbose,
        sync_direction=direction,
        clear=clear,
        yes=yes,
    )


def _run_sync(cfg: SyncConfig) -> None:
    """Core sync runner: display panel, confirm, run, show results."""
    work_name, work_account, work_uid = get_calendar_display_info(cfg.work_calendar_id)
    personal_name, personal_account, personal_uid = get_calendar_display_info(
        cfg.personal_calendar_id
    )

    # -- Info panel ----------------------------------------------------------
    direction_label = {
        "both": "[cyan]↔ Bidirectional[/]",
        "to-personal": "[cyan]→ Work → Personal[/]",
        "to-work": "[cyan]← Personal → Work[/]",
    }[cfg.sync_direction]

    work_display = work_name + (f" ({work_account})" if work_account else "")
    personal_display = personal_name + (f" ({personal_account})" if personal_account else "")

    if cfg.clear:
        op_line = Text("CLEAR (remove all synced events, no resync)", style="bold red")
    elif cfg.refresh:
        op_line = Text("REFRESH (remove synced events then resync)", style="bold yellow")
    else:
        op_line = Text("SYNC", style="bold green")

    info = Text()
    info.append("  Work:      ", style="bold")
    info.append(f"{work_display}\n")
    info.append(f"             {work_uid}\n", style="dim")
    info.append("  Personal:  ", style="bold")
    info.append(f"{personal_display}\n")
    info.append(f"             {personal_uid}\n", style="dim")
    info.append("  Direction: ")
    info.append_text(Text.from_markup(direction_label))
    info.append("\n  Operation: ")
    info.append_text(op_line)
    if cfg.dry_run:
        info.append("\n  Mode:      ")
        info.append("DRY RUN", style="bold magenta")

    console.print(Panel(info, title="[bold]EDS Calendar Sync[/bold]"))

    # -- Confirmation --------------------------------------------------------
    if not cfg.yes and not cfg.dry_run:
        typer.confirm("Proceed?", abort=True)

    # -- Run -----------------------------------------------------------------
    try:
        stats = CalendarSynchronizer(cfg).run()
    except CalendarSyncError as e:
        console.print(f"[bold red]Sync failed:[/] {e}")
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted by user[/]")
        raise typer.Exit(130)
    except Exception as e:
        console.print_exception()
        console.print(f"[bold red]Unexpected error:[/] {e}")
        raise typer.Exit(1)

    # -- Results table -------------------------------------------------------
    results = Table.grid(padding=(0, 2))
    results.add_column(style="bold")
    results.add_column(justify="right")
    results.add_row("Added", str(stats.added))
    results.add_row("Modified", str(stats.modified))
    results.add_row("Deleted", str(stats.deleted))
    error_val = Text(str(stats.errors))
    if stats.errors == 0:
        error_val.append(" ✓", style="green")
    else:
        error_val.stylize("bold red")
    results.add_row("Errors", error_val)

    console.print(Panel(results, title="[bold]Results[/bold]", expand=False))

    if stats.errors:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Subcommands: sync / refresh / clear share the same options
# ---------------------------------------------------------------------------

_WORK_OPT = Annotated[
    Optional[str],
    typer.Option("--work-calendar", "-w", help="Work calendar EDS UID (overrides config)"),
]
_PERS_OPT = Annotated[
    Optional[str],
    typer.Option("--personal-calendar", "-p", help="Personal calendar EDS UID (overrides config)"),
]
_TO_PERS = Annotated[bool, typer.Option("--to-personal", help="One-way: work → personal only")]
_TO_WORK = Annotated[bool, typer.Option("--to-work", help="One-way: personal → work only")]
_DRY_RUN = Annotated[bool, typer.Option("--dry-run", "-n", help="Preview changes without applying")]
_YES = Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")]


@app.command()
def sync(
    work_calendar: _WORK_OPT = None,
    personal_calendar: _PERS_OPT = None,
    to_personal: _TO_PERS = False,
    to_work: _TO_WORK = False,
    dry_run: _DRY_RUN = False,
    yes: _YES = False,
) -> None:
    """Synchronise calendars (bidirectional by default)."""
    _run_sync(_build_config(work_calendar, personal_calendar, to_personal, to_work,
                            dry_run=dry_run, refresh=False, clear=False, yes=yes))


@app.command()
def refresh(
    work_calendar: _WORK_OPT = None,
    personal_calendar: _PERS_OPT = None,
    to_personal: _TO_PERS = False,
    to_work: _TO_WORK = False,
    dry_run: _DRY_RUN = False,
    yes: _YES = False,
) -> None:
    """Remove synced events then re-sync from scratch."""
    _run_sync(_build_config(work_calendar, personal_calendar, to_personal, to_work,
                            dry_run=dry_run, refresh=True, clear=False, yes=yes))


@app.command()
def clear(
    work_calendar: _WORK_OPT = None,
    personal_calendar: _PERS_OPT = None,
    to_personal: _TO_PERS = False,
    to_work: _TO_WORK = False,
    dry_run: _DRY_RUN = False,
    yes: _YES = False,
) -> None:
    """Remove all synced events without re-syncing."""
    _run_sync(_build_config(work_calendar, personal_calendar, to_personal, to_work,
                            dry_run=dry_run, refresh=False, clear=True, yes=yes))


# ---------------------------------------------------------------------------
# Subcommand: migrate
# ---------------------------------------------------------------------------

@app.command()
def migrate(
    old_work: Annotated[
        Optional[str], typer.Option(help="Old work calendar UID")
    ] = None,
    new_work: Annotated[
        Optional[str], typer.Option(help="New work calendar UID")
    ] = None,
    old_personal: Annotated[
        Optional[str], typer.Option(help="Old personal calendar UID")
    ] = None,
    new_personal: Annotated[
        Optional[str], typer.Option(help="New personal calendar UID")
    ] = None,
    dry_run: _DRY_RUN = False,
) -> None:
    """Update calendar IDs in state DB after GOA reconnection."""
    work_pair = (old_work, new_work)
    pers_pair = (old_personal, new_personal)

    if not (all(work_pair) or all(pers_pair)):
        console.print(
            "[bold red]Error:[/] At least one fully specified pair is required:\n"
            "  [cyan]--old-work[/] UID [cyan]--new-work[/] UID, and/or\n"
            "  [cyan]--old-personal[/] UID [cyan]--new-personal[/] UID"
        )
        raise typer.Exit(1)

    for label, pair in [("work", work_pair), ("personal", pers_pair)]:
        if any(pair) and not all(pair):
            console.print(
                f"[bold red]Error:[/] Both [cyan]--old-{label}[/] and "
                f"[cyan]--new-{label}[/] must be given together."
            )
            raise typer.Exit(1)

    state_db_path = state.state_db
    if not state_db_path.exists():
        console.print(f"[bold red]Error:[/] State database not found: {state_db_path}")
        raise typer.Exit(1)

    # Info panel
    info = Text()
    info.append("  State DB:  ", style="bold")
    info.append(f"{state_db_path}\n")
    if all(work_pair):
        info.append("  Work:      ", style="bold")
        info.append(f"{work_pair[0]} → {work_pair[1]}\n")
    if all(pers_pair):
        info.append("  Personal:  ", style="bold")
        info.append(f"{pers_pair[0]} → {pers_pair[1]}\n")
    if dry_run:
        info.append("  Mode:      ", style="bold")
        info.append("DRY RUN", style="bold magenta")

    console.print(Panel(info, title="[bold]Calendar ID Migration[/bold]"))

    work_rows, pers_rows = migrate_calendar_ids_in_db(
        state_db_path,
        old_work, new_work,
        old_personal, new_personal,
        dry_run,
    )

    prefix = "[DRY RUN] Would update" if dry_run else "Updated"
    results = Table.grid(padding=(0, 2))
    results.add_column(style="bold")
    results.add_column(justify="right")
    if all(work_pair):
        results.add_row("Work records", f"{prefix} {work_rows}")
    if all(pers_pair):
        results.add_row("Personal records", f"{prefix} {pers_rows}")

    console.print(Panel(results, title="[bold]Results[/bold]", expand=False))

    if work_rows == 0 and pers_rows == 0:
        console.print("[yellow]Warning:[/] No matching records found — verify the old UIDs.")

    if not dry_run:
        console.print(
            "\n[bold]Next steps:[/]\n"
            "  1. Update [cyan]~/.config/eds-calendar-sync.conf[/] with the new UIDs\n"
            "  2. Run [cyan]eds-calendar-sync sync --dry-run[/] to verify everything works"
        )


# ---------------------------------------------------------------------------
# Subcommand: calendars
# ---------------------------------------------------------------------------

@app.command()
def calendars() -> None:
    """List all configured EDS calendars."""
    import gi
    gi.require_version('EDataServer', '1.2')
    from gi.repository import EDataServer

    from .debug import list_calendars as _list_calendars

    registry = EDataServer.SourceRegistry.new_sync(None)
    _list_calendars(registry, console)


# ---------------------------------------------------------------------------
# Subcommand: inspect
# ---------------------------------------------------------------------------

@app.command()
def inspect(
    calendar_uid: Annotated[str, typer.Argument(help="Calendar UID to inspect")],
    title: Annotated[
        Optional[str], typer.Option(help="Filter by SUMMARY substring (case-insensitive)")
    ] = None,
    uid: Annotated[
        Optional[str], typer.Option(help="Filter by UID substring (case-insensitive)")
    ] = None,
    no_raw: Annotated[bool, typer.Option("--no-raw", help="Omit the raw iCal block")] = False,
    exceptions_only: Annotated[
        bool, typer.Option("--exceptions-only", help="Show only exception VEVENTs (have RECURRENCE-ID)")
    ] = False,
    masters_only: Annotated[
        bool, typer.Option("--masters-only", help="Show only master VEVENTs (no RECURRENCE-ID)")
    ] = False,
) -> None:
    """Inspect / debug events in a calendar."""
    import gi
    gi.require_version('EDataServer', '1.2')
    gi.require_version('ECal', '2.0')
    gi.require_version('ICalGLib', '3.0')
    from gi.repository import EDataServer, ECal, ICalGLib

    from .debug import dump_event

    registry = EDataServer.SourceRegistry.new_sync(None)
    source = registry.ref_source(calendar_uid)
    if not source:
        console.print(f"[bold red]Error:[/] Calendar [cyan]{calendar_uid}[/] not found.")
        raise typer.Exit(1)

    console.print(
        f"[bold]Calendar:[/] {source.get_display_name()} "
        f"[dim]({calendar_uid})[/dim]"
    )

    client = ECal.Client.connect_sync(source, ECal.ClientSourceType.EVENTS, 30, None)
    _, objects = client.get_object_list_sync("#t", None)
    console.print(f"[bold]Events:[/] {len(objects)} total")

    title_filter = title.lower() if title else None
    uid_filter = uid.lower() if uid else None

    count = 0
    for obj in objects:
        comp = ICalGLib.Component.new_from_string(obj) if isinstance(obj, str) else obj

        vevent = comp
        if comp.isa() == ICalGLib.ComponentKind.VCALENDAR_COMPONENT:
            vevent = comp.get_first_component(ICalGLib.ComponentKind.VEVENT_COMPONENT)
            if not vevent:
                continue

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

        if exceptions_only and not has_rid:
            continue
        if masters_only and has_rid:
            continue

        count += 1
        dump_event(vevent, console, show_raw=not no_raw)

    console.print(f"\n[bold]Matched {count} event(s)[/bold]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app()
