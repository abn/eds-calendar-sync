"""
Command-line interface for EDS Calendar Sync.
"""

import logging
from configparser import ConfigParser
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from eds_calendar_sync.db import get_all_calendar_ids
from eds_calendar_sync.db import migrate_calendar_id
from eds_calendar_sync.db import query_status_all_pairs
from eds_calendar_sync.eds_client import get_calendar_display_info
from eds_calendar_sync.models import DEFAULT_CONFIG
from eds_calendar_sync.models import DEFAULT_STATE_DB
from eds_calendar_sync.models import CalendarSyncError
from eds_calendar_sync.models import SyncConfig
from eds_calendar_sync.sync import CalendarSynchronizer

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


def _load_config_file(config_path: Path) -> dict[str, str]:
    if not config_path.exists():
        return {}
    parser = ConfigParser()
    parser.read(config_path)
    if "calendar-sync" not in parser:
        return {}
    return dict(parser["calendar-sync"])


def _build_config(
    work_calendar: str | None,
    personal_calendar: str | None,
    to_personal: bool,
    to_work: bool,
    dry_run: bool,
    refresh: bool,
    clear: bool,
    yes: bool,
    keep_reminders: bool = False,
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
        keep_reminders=keep_reminders,
    )


def _run_sync(cfg: SyncConfig) -> None:
    """Core sync runner: display panel, confirm, run, show results."""
    from eds_calendar_sync.preflight import run_preflight_checks

    if not run_preflight_checks(cfg, console):
        raise typer.Exit(1)

    work_name, work_account, work_uid = get_calendar_display_info(cfg.work_calendar_id)
    personal_name, personal_account, personal_uid = get_calendar_display_info(
        cfg.personal_calendar_id
    )

    # -- Info panel ----------------------------------------------------------
    if cfg.clear:
        direction_key = "  Target:    "
        direction_label = {
            "both": "[cyan]both calendars[/] [dim](all managed events)[/dim]",
            "to-personal": "[cyan]personal calendar only[/]"
            " [dim](events created by work→personal sync)[/dim]",
            "to-work": "[cyan]work calendar only[/]"
            " [dim](events created by personal→work sync)[/dim]",
        }[cfg.sync_direction]
    else:
        direction_key = "  Direction: "
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
    info.append(direction_key, style="bold")
    info.append_text(Text.from_markup(direction_label))
    info.append("\n  Operation: ")
    info.append_text(op_line)
    if cfg.keep_reminders:
        info.append("\n  Reminders: ")
        info.append("preserved", style="yellow")
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
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted by user[/]")
        raise typer.Exit(130) from None
    except Exception as e:
        console.print_exception()
        console.print(f"[bold red]Unexpected error:[/] {e}")
        raise typer.Exit(1) from e

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
    str | None,
    typer.Option("--work-calendar", "-w", help="Work calendar EDS UID (overrides config)"),
]
_PERS_OPT = Annotated[
    str | None,
    typer.Option("--personal-calendar", "-p", help="Personal calendar EDS UID (overrides config)"),
]
_TO_PERS = Annotated[bool, typer.Option("--to-personal", help="One-way: work → personal only")]
_TO_WORK = Annotated[bool, typer.Option("--to-work", help="One-way: personal → work only")]
# clear uses different flag names to avoid the directional confusion: we're describing
# *which calendar to clean up*, not which direction events should flow.
_CLEAR_PERS = Annotated[
    bool,
    typer.Option(
        "--personal",
        help="Remove managed events from the personal calendar only"
        " (those created by work→personal sync)",
    ),
]
_CLEAR_WORK = Annotated[
    bool,
    typer.Option(
        "--work",
        help="Remove managed events from the work calendar only"
        " (those created by personal→work sync)",
    ),
]
_DRY_RUN = Annotated[bool, typer.Option("--dry-run", "-n", help="Preview changes without applying")]
_YES = Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")]
_KEEP_REMINDERS = Annotated[
    bool,
    typer.Option(
        "--keep-reminders",
        help=(
            "Preserve VALARM reminders on synced events "
            "(stripped by default to avoid duplicate notifications)"
        ),
    ),
]


@app.command()
def sync(
    work_calendar: _WORK_OPT = None,
    personal_calendar: _PERS_OPT = None,
    to_personal: _TO_PERS = False,
    to_work: _TO_WORK = False,
    dry_run: _DRY_RUN = False,
    yes: _YES = False,
    keep_reminders: _KEEP_REMINDERS = False,
) -> None:
    """Synchronise calendars (bidirectional by default).

    With no arguments, launches an interactive wizard to select the
    calendar pair and sync direction.
    """
    if work_calendar is None and personal_calendar is None and not to_personal and not to_work:
        work_calendar, personal_calendar, direction = _interactive_sync_setup()
        to_personal = direction == "to-personal"
        to_work = direction == "to-work"
        yes = True  # wizard selections serve as confirmation

    _run_sync(
        _build_config(
            work_calendar,
            personal_calendar,
            to_personal,
            to_work,
            dry_run=dry_run,
            refresh=False,
            clear=False,
            yes=yes,
            keep_reminders=keep_reminders,
        )
    )


@app.command()
def refresh(
    work_calendar: _WORK_OPT = None,
    personal_calendar: _PERS_OPT = None,
    to_personal: _TO_PERS = False,
    to_work: _TO_WORK = False,
    dry_run: _DRY_RUN = False,
    yes: _YES = False,
    keep_reminders: _KEEP_REMINDERS = False,
) -> None:
    """Remove synced events then re-sync from scratch."""
    _run_sync(
        _build_config(
            work_calendar,
            personal_calendar,
            to_personal,
            to_work,
            dry_run=dry_run,
            refresh=True,
            clear=False,
            yes=yes,
            keep_reminders=keep_reminders,
        )
    )


@app.command()
def clear(
    work_calendar: _WORK_OPT = None,
    personal_calendar: _PERS_OPT = None,
    personal: _CLEAR_PERS = False,
    work: _CLEAR_WORK = False,
    dry_run: _DRY_RUN = False,
    yes: _YES = False,
) -> None:
    """Remove managed events from calendars without re-syncing.

    By default clears managed events from [bold]both[/bold] calendars.
    Use [cyan]--personal[/] or [cyan]--work[/] to restrict to one calendar.
    """
    _run_sync(
        _build_config(
            work_calendar,
            personal_calendar,
            to_personal=personal,
            to_work=work,
            dry_run=dry_run,
            refresh=False,
            clear=True,
            yes=yes,
        )
    )


# ---------------------------------------------------------------------------
# EDS / interactive helpers  (used by sync wizard and migrate)
# ---------------------------------------------------------------------------


def _load_eds_calendars():
    """
    Initialise EDS and return (registry, entries).
    entries: list of (name, account, mode, mode_style, uid)
    """
    import gi

    gi.require_version("ECal", "2.0")
    gi.require_version("EDataServer", "1.2")
    from gi.repository import ECal
    from gi.repository import EDataServer

    registry = EDataServer.SourceRegistry.new_sync(None)
    sources = registry.list_sources(EDataServer.SOURCE_EXTENSION_CALENDAR)
    entries = []
    for source in sources:
        sname = source.get_display_name() or "(unnamed)"
        suid = source.get_uid() or ""
        parent = source.get_parent()
        account = ""
        if parent:
            psrc = registry.ref_source(parent)
            if psrc:
                account = psrc.get_display_name() or ""
        try:
            client = ECal.Client.connect_sync(source, ECal.ClientSourceType.EVENTS, 5, None)
            mode = "Read-write" if not client.is_readonly() else "Read-only"
            mode_style = "green" if not client.is_readonly() else "yellow"
        except Exception:
            mode = "Unknown"
            mode_style = "red"
        entries.append((sname, account, mode, mode_style, suid))
    return registry, entries


def _print_picker_table(entries) -> None:
    """Print a numbered EDS calendar table for interactive selection."""
    picker = Table(show_header=True, header_style="bold cyan")
    picker.add_column("#", style="bold", justify="right", width=3)
    picker.add_column("Display Name / UID", min_width=36, overflow="fold")
    picker.add_column("Account")
    picker.add_column("Mode")
    for i, (sname, account, mode, mode_style, suid) in enumerate(entries, 1):
        name_cell = Text()
        name_cell.append(sname, style="bold")
        name_cell.append("\n")
        name_cell.append(suid, style="dim")
        picker.add_row(str(i), name_cell, account, Text(mode, style=mode_style))
    console.print(picker)


def _pick_calendar(
    entries, allow_skip: bool = False, prompt: str = "Select replacement"
) -> str | None:
    """
    Prompt the user to choose a calendar from entries by number.
    Returns the chosen UID, or None if the user enters 0 and allow_skip is True.
    """
    n = len(entries)
    suffix = " (0 to skip)" if allow_skip else ""
    choice = typer.prompt(f"{prompt} [1-{n}]{suffix}", type=int)
    if allow_skip and choice == 0:
        return None
    if choice < 1 or choice > n:
        console.print("[bold red]Error:[/] Invalid selection.")
        raise typer.Exit(1)
    return entries[choice - 1][4]


def _interactive_sync_setup() -> tuple[str, str, str]:
    """
    Three-step wizard: pick work calendar, personal calendar, sync direction.
    Returns (work_id, personal_id, direction) where direction is one of
    'both', 'to-personal', 'to-work'.
    """
    config_file = _load_config_file(state.config_path)
    config_work = config_file.get("work_calendar_id")
    config_personal = config_file.get("personal_calendar_id")

    _, eds_entries = _load_eds_calendars()
    if not eds_entries:
        console.print("[bold red]Error:[/] No EDS calendars found.")
        raise typer.Exit(1)

    uid_to_idx = {suid: i + 1 for i, (_, _, _, _, suid) in enumerate(eds_entries)}

    # -- Step 1: work calendar ------------------------------------------------
    console.rule("[bold]Step 1 of 3 — Work calendar[/bold]")
    if config_work:
        hint = uid_to_idx.get(config_work)
        hint_str = f"#{hint}  {config_work}" if hint else f"{config_work} [red](not in EDS)[/red]"
        console.print(f"  [dim]Config: {hint_str}[/dim]\n")
    _print_picker_table(eds_entries)
    console.print()
    work_id = _pick_calendar(eds_entries, prompt="Select work calendar")

    # -- Step 2: personal calendar --------------------------------------------
    console.rule("[bold]Step 2 of 3 — Personal calendar[/bold]")
    if config_personal:
        hint = uid_to_idx.get(config_personal)
        hint_str = (
            f"#{hint}  {config_personal}" if hint else f"{config_personal} [red](not in EDS)[/red]"
        )
        console.print(f"  [dim]Config: {hint_str}[/dim]\n")
    _print_picker_table(eds_entries)
    console.print()
    personal_id = _pick_calendar(eds_entries, prompt="Select personal calendar")

    # -- Step 3: direction ----------------------------------------------------
    console.rule("[bold]Step 3 of 3 — Sync direction[/bold]")
    dir_table = Table(show_header=False, box=None, padding=(0, 2))
    dir_table.add_column("#", style="bold", justify="right", width=3)
    dir_table.add_column()
    dir_table.add_row("1", "[cyan]↔[/]  Bidirectional [dim](default)[/dim]")
    dir_table.add_row("2", "[cyan]→[/]  Work → Personal only")
    dir_table.add_row("3", "[cyan]←[/]  Personal → Work only")
    console.print(dir_table)
    console.print()
    dir_choice = typer.prompt("Select direction [1-3]", type=int, default=1)
    direction = {1: "both", 2: "to-personal", 3: "to-work"}.get(dir_choice)
    if direction is None:
        console.print("[bold red]Error:[/] Invalid selection.")
        raise typer.Exit(1)

    console.print()
    return work_id, personal_id, direction


_NEXT_STEPS = (
    "\n[bold]Next steps:[/]\n"
    "  1. Update [cyan]~/.config/eds-calendar-sync.conf[/] with the new UID(s)\n"
    "  2. Run [cyan]eds-calendar-sync sync --dry-run[/] to verify everything works"
)


# ---------------------------------------------------------------------------
# Subcommand: migrate
# ---------------------------------------------------------------------------


@app.command()
def migrate(
    old: Annotated[
        str | None,
        typer.Argument(help="Calendar UID to replace (audit mode if omitted)"),
    ] = None,
    new: Annotated[
        str | None,
        typer.Argument(help="Replacement calendar UID (interactive picker if omitted)"),
    ] = None,
    dry_run: _DRY_RUN = False,
) -> None:
    """Replace a calendar UID everywhere in the state DB.

    [bold]No arguments[/bold]: audits all UIDs in the state DB against EDS,
    flags any that no longer resolve, and lets you pick a replacement
    for each missing UID one at a time.

    [bold]OLD only[/bold]: shows an interactive EDS calendar picker for the replacement.

    [bold]OLD NEW[/bold]: replaces immediately without prompting.

    Useful after a GOA reconnection assigns a new EDS UID to a calendar.
    """
    state_db_path = state.state_db
    if not state_db_path.exists():
        console.print(f"[bold red]Error:[/] State database not found: {state_db_path}")
        raise typer.Exit(1)

    # ---- Audit mode (no arguments) ----------------------------------------
    if old is None:
        all_uids = get_all_calendar_ids(state_db_path)
        if not all_uids:
            console.print("[yellow]State database is empty — nothing to audit.[/]")
            return

        _, eds_entries = _load_eds_calendars()
        eds_by_uid = {suid: (sname, account) for sname, account, _, _, suid in eds_entries}

        audit = Table(show_header=True, header_style="bold cyan")
        audit.add_column("UID", min_width=36, overflow="fold")
        audit.add_column("Name")
        audit.add_column("Account")
        audit.add_column("Status")

        missing: list[str] = []
        for uid in all_uids:
            if uid in eds_by_uid:
                sname, account = eds_by_uid[uid]
                audit.add_row(uid, sname, account, Text("✓ Active", style="green"))
            else:
                audit.add_row(
                    uid,
                    Text("(not found)", style="dim"),
                    "",
                    Text("✗ Missing", style="bold red"),
                )
                missing.append(uid)

        console.print(Panel(audit, title="[bold]Calendar UID Audit[/bold]"))

        if not missing:
            console.print("[green]All calendar UIDs resolve correctly.[/]")
            return

        console.print(
            f"\n[bold yellow]{len(missing)} unresolved UID(s)[/bold yellow] — "
            "select a replacement for each (0 to skip).\n"
        )

        migrated_any = False
        for uid in missing:
            console.rule(f"[dim]{uid}[/dim]")
            console.print()
            _print_picker_table(eds_entries)
            console.print()
            new_uid = _pick_calendar(eds_entries, allow_skip=True)
            if new_uid is None:
                console.print("[dim]Skipped.[/dim]\n")
                continue
            rows = migrate_calendar_id(state_db_path, uid, new_uid, dry_run)
            prefix = "Would update" if dry_run else "Updated"
            console.print(
                f"  {prefix} [bold]{rows}[/bold] record(s): "
                f"[dim]{uid}[/dim] → [cyan]{new_uid}[/cyan]\n"
            )
            migrated_any = True

        if migrated_any and not dry_run:
            console.print(_NEXT_STEPS)
        return

    # ---- Single UID mode (old given, new optional) ------------------------
    if new is None:
        _, eds_entries = _load_eds_calendars()
        if not eds_entries:
            console.print("[bold red]Error:[/] No EDS calendars found.")
            raise typer.Exit(1)
        _print_picker_table(eds_entries)
        console.print()
        new = _pick_calendar(eds_entries)
        console.print()

    info = Text()
    info.append("  State DB:  ", style="bold")
    info.append(f"{state_db_path}\n")
    info.append("  Old UID:   ", style="bold")
    info.append(f"{old}\n")
    info.append("  New UID:   ", style="bold")
    info.append(f"{new}\n")
    if dry_run:
        info.append("  Mode:      ", style="bold")
        info.append("DRY RUN", style="bold magenta")

    console.print(Panel(info, title="[bold]Calendar ID Migration[/bold]"))

    rows = migrate_calendar_id(state_db_path, old, new, dry_run)

    prefix = "Would update" if dry_run else "Updated"
    results = Table.grid(padding=(0, 2))
    results.add_column(style="bold")
    results.add_column(justify="right")
    results.add_row("Records matched", f"{prefix} {rows}")

    console.print(Panel(results, title="[bold]Results[/bold]", expand=False))

    if rows == 0:
        console.print("[yellow]Warning:[/] No matching records found — verify the old UID.")

    if not dry_run:
        console.print(_NEXT_STEPS)


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------


@app.command()
def status() -> None:
    """Show sync configuration and state database summary."""
    from datetime import datetime

    # -- Configuration section -----------------------------------------------
    config_exists = state.config_path.exists()
    db_exists = state.state_db.exists()

    cfg_info = Text()
    cfg_info.append("  Config:   ", style="bold")
    cfg_info.append(str(state.config_path) + " ")
    cfg_info.append(
        "✓" if config_exists else "(not found)", style="green" if config_exists else "red"
    )
    cfg_info.append("\n  State DB: ", style="bold")
    cfg_info.append(str(state.state_db) + " ")
    cfg_info.append("✓" if db_exists else "(not found)", style="green" if db_exists else "yellow")

    # Try to resolve the configured calendar pair's display names via EDS.
    config_file = _load_config_file(state.config_path)
    work_id = config_file.get("work_calendar_id")
    personal_id = config_file.get("personal_calendar_id")

    if work_id or personal_id:
        cfg_info.append("\n")

    if work_id:
        try:
            work_name, work_account, _ = get_calendar_display_info(work_id)
            work_display = work_name + (f" ({work_account})" if work_account else "")
        except Exception:
            work_display = work_id
        cfg_info.append("\n  Work:     ", style="bold")
        cfg_info.append(work_display + "\n")
        cfg_info.append(f"            {work_id}", style="dim")

    if personal_id:
        try:
            pers_name, pers_account, _ = get_calendar_display_info(personal_id)
            pers_display = pers_name + (f" ({pers_account})" if pers_account else "")
        except Exception:
            pers_display = personal_id
        cfg_info.append("\n  Personal: ", style="bold")
        cfg_info.append(pers_display + "\n")
        cfg_info.append(f"            {personal_id}", style="dim")

    console.print(Panel(cfg_info, title="[bold]EDS Calendar Sync — Status[/bold]"))

    # -- State DB section ----------------------------------------------------
    rows = query_status_all_pairs(state.state_db)

    if not rows:
        if not db_exists:
            console.print(
                "[yellow]No state database yet — run[/] "
                "[cyan]eds-calendar-sync sync[/] "
                "[yellow]to create it.[/]"
            )
        else:
            console.print("[yellow]State database is empty — no syncs recorded yet.[/]")
        return

    # Group rows by (work_calendar_id, personal_calendar_id) pair
    pairs = {}
    for row in rows:
        key = (row["work_calendar_id"], row["personal_calendar_id"])
        pairs.setdefault(key, []).append(row)

    for (w_id, p_id), pair_rows in pairs.items():
        # Resolve display names (best-effort; fall back to short UID on failure)
        def _short(uid: str) -> str:
            return uid[:16] + "…" if len(uid) > 16 else uid

        try:
            w_name, w_acct, _ = get_calendar_display_info(w_id)
            w_label = w_name + (f" ({w_acct})" if w_acct else "")
        except Exception:
            w_label = _short(w_id)

        try:
            p_name, p_acct, _ = get_calendar_display_info(p_id)
            p_label = p_name + (f" ({p_acct})" if p_acct else "")
        except Exception:
            p_label = _short(p_id)

        is_configured_pair = w_id == work_id and p_id == personal_id
        pair_title = f"[bold]{w_label}[/bold] ↔ [bold]{p_label}[/bold]"
        if is_configured_pair:
            pair_title += "  [green](configured)[/green]"

        table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 2))
        table.add_column("Direction")
        table.add_column("Tracked", justify="right")
        table.add_column("Last sync")

        overall_last_sync = 0
        for row in pair_rows:
            direction = "Work → Personal" if row["origin"] == "source" else "Personal → Work"
            ts = row["last_sync_at"] or 0
            overall_last_sync = max(overall_last_sync, ts)
            last_sync_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "—"
            table.add_row(direction, str(row["count"]), last_sync_str)

        console.print(Panel(table, title=pair_title, expand=False))


# ---------------------------------------------------------------------------
# Subcommand: calendars
# ---------------------------------------------------------------------------


@app.command()
def calendars() -> None:
    """List all configured EDS calendars."""
    import gi

    gi.require_version("EDataServer", "1.2")
    from gi.repository import EDataServer

    from eds_calendar_sync.debug import list_calendars as _list_calendars

    registry = EDataServer.SourceRegistry.new_sync(None)
    _list_calendars(registry, console)


# ---------------------------------------------------------------------------
# Subcommand: inspect
# ---------------------------------------------------------------------------


@app.command()
def inspect(
    calendar_uid: Annotated[str, typer.Argument(help="Calendar UID to inspect")],
    title: Annotated[
        str | None, typer.Option(help="Filter by SUMMARY substring (case-insensitive)")
    ] = None,
    uid: Annotated[
        str | None, typer.Option(help="Filter by UID substring (case-insensitive)")
    ] = None,
    no_raw: Annotated[bool, typer.Option("--no-raw", help="Omit the raw iCal block")] = False,
    exceptions_only: Annotated[
        bool,
        typer.Option("--exceptions-only", help="Show only exception VEVENTs (have RECURRENCE-ID)"),
    ] = False,
    masters_only: Annotated[
        bool, typer.Option("--masters-only", help="Show only master VEVENTs (no RECURRENCE-ID)")
    ] = False,
) -> None:
    """Inspect / debug events in a calendar."""
    import gi

    gi.require_version("EDataServer", "1.2")
    gi.require_version("ECal", "2.0")
    gi.require_version("ICalGLib", "3.0")
    from gi.repository import ECal
    from gi.repository import EDataServer
    from gi.repository import ICalGLib

    from eds_calendar_sync.debug import dump_event

    registry = EDataServer.SourceRegistry.new_sync(None)
    source = registry.ref_source(calendar_uid)
    if not source:
        console.print(f"[bold red]Error:[/] Calendar [cyan]{calendar_uid}[/] not found.")
        raise typer.Exit(1)

    console.print(f"[bold]Calendar:[/] {source.get_display_name()} [dim]({calendar_uid})[/dim]")

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
            summary = (sp.get_summary() or "") if sp else ""
            if title_filter not in summary.lower():
                continue

        if uid_filter:
            if uid_filter not in (vevent.get_uid() or "").lower():
                continue

        has_rid = vevent.get_first_property(ICalGLib.PropertyKind.RECURRENCEID_PROPERTY) is not None

        if exceptions_only and not has_rid:
            continue
        if masters_only and has_rid:
            continue

        count += 1
        dump_event(vevent, console, show_raw=not no_raw)

    console.print(f"\n[bold]Matched {count} event(s)[/bold]")


# ---------------------------------------------------------------------------
# Subcommand: verify
# ---------------------------------------------------------------------------


@app.command()
def verify(
    work_calendar: _WORK_OPT = None,
    personal_calendar: _PERS_OPT = None,
    weeks: Annotated[
        int, typer.Option("--weeks", help="Number of weeks to check (default: 4)")
    ] = 4,
    from_date: Annotated[
        str | None,
        typer.Option("--from-date", help="Start date YYYY-MM-DD (default: today)"),
    ] = None,
) -> None:
    """Verify sync completeness: check that expected events appear in both calendars.

    Fetches [bold]--weeks[/] weeks of events from work and personal calendars,
    applies the same eligibility guards as sync, and reports any missing,
    stale, or orphaned events.

    Exits with code 1 if any issues are found.
    """
    from datetime import date
    from datetime import timedelta

    from eds_calendar_sync.verify import run_verify

    config_file = _load_config_file(state.config_path)
    work_id = work_calendar or config_file.get("work_calendar_id")
    personal_id = personal_calendar or config_file.get("personal_calendar_id")
    if not work_id or not personal_id:
        console.print("[bold red]Error:[/] Work and personal calendar IDs required.")
        raise typer.Exit(1)

    if from_date:
        try:
            window_start = date.fromisoformat(from_date)
        except ValueError:
            console.print(f"[bold red]Error:[/] Invalid date: {from_date!r}")
            raise typer.Exit(1) from None
    else:
        window_start = date.today()
    window_end = window_start + timedelta(weeks=weeks)

    ok = run_verify(
        work_calendar_id=work_id,
        personal_calendar_id=personal_id,
        state_db_path=state.state_db,
        console=console,
        window_start=window_start,
        window_end=window_end,
    )
    if not ok:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    app()
