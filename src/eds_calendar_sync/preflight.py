"""
Preflight checks run before sync to catch common misconfigurations early.
"""

import logging
import sqlite3

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from eds_calendar_sync.models import SyncConfig

logger = logging.getLogger(__name__)

_OFFLINE_KEYWORDS = frozenset(
    {
        "offline",
        "network",
        "transport",
        "unreachable",
        "not connected",
        "no route",
        "authentication failed",
        "connection refused",
        "temporary failure",
    }
)


def run_preflight_checks(cfg: SyncConfig, console: Console) -> bool:
    """Return True if sync may proceed; print issues and return False otherwise."""
    import gi

    gi.require_version("ECal", "2.0")
    gi.require_version("EDataServer", "1.2")
    from gi.repository import ECal
    from gi.repository import EDataServer
    from gi.repository import GLib

    issues: list[tuple[str, str, str]] = []  # (label, detail, hint)

    # 1. EDS registry reachable
    try:
        registry = EDataServer.SourceRegistry.new_sync(None)
    except Exception as e:
        logger.error("EDS registry unreachable: %s", e)
        issues.append(
            (
                "EDS registry",
                str(e),
                "Is evolution-data-server running?",
            )
        )
        _print_issues(issues, console)
        return False

    # 2 & 3. Calendar UID exists + connectable
    for uid, label in (
        (cfg.work_calendar_id, "Work calendar"),
        (cfg.personal_calendar_id, "Personal calendar"),
    ):
        source = registry.ref_source(uid)
        if source is None:
            logger.error("Calendar UID not found in EDS: %s", uid)
            issues.append(
                (
                    label,
                    f"UID not found: {uid}",
                    "Run: eds-calendar-sync migrate",
                )
            )
            continue

        try:
            ECal.Client.connect_sync(source, ECal.ClientSourceType.EVENTS, 5, None)
        except GLib.Error as e:
            msg = e.message or str(e)
            logger.error("Cannot connect to %s calendar (%s): %s", label, uid, msg)
            if any(kw in msg.lower() for kw in _OFFLINE_KEYWORDS):
                # Try to find the parent account name for a better hint
                account_name = _get_parent_display_name(registry, source)
                if account_name:
                    hint = f"Account '{account_name}' appears offline — check GNOME Online Accounts"
                else:
                    hint = "Calendar appears offline — check GNOME Online Accounts"
            else:
                hint = msg
            issues.append((label, f"Connection failed: {msg}", hint))

    # 4. State DB parent dir writable + DB readable if it exists
    db_path = cfg.state_db_path
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("Cannot create state DB directory %s: %s", db_path.parent, e)
        issues.append(
            (
                "State database",
                f"{db_path}: {e}",
                f"Check permissions on {db_path.parent}",
            )
        )
    else:
        if db_path.exists():
            try:
                conn = sqlite3.connect(db_path)
                conn.execute("SELECT 1")
                # Also verify write access: BEGIN IMMEDIATE acquires a write lock
                # and requires creating a journal file in the same directory.
                # This catches ProtectHome=read-only with a file-only ReadWritePaths.
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("ROLLBACK")
                conn.close()
            except sqlite3.Error as e:
                logger.error("State DB not readable/writable (%s): %s", db_path, e)
                issues.append(
                    (
                        "State database",
                        f"{db_path}: {e}",
                        f"Check permissions on {db_path.parent} "
                        f"(journal files must be creatable alongside the DB); "
                        f"if using a systemd service, ensure ReadWritePaths "
                        f"covers the parent directory, not just the file",
                    )
                )

    if issues:
        _print_issues(issues, console)
        return False

    return True


def _get_parent_display_name(registry, source) -> str:
    """Return the display name of the source's parent account, or empty string."""
    parent_uid = source.get_parent()
    if not parent_uid:
        return ""
    parent_source = registry.ref_source(parent_uid)
    if not parent_source:
        return ""
    return parent_source.get_display_name() or ""


def _print_issues(issues: list[tuple[str, str, str]], console: Console) -> None:
    body = Text()
    for i, (label, detail, hint) in enumerate(issues):
        if i:
            body.append("\n")
        body.append(f"  \u2717  {label}: ", style="bold red")
        body.append(detail, style="bold red")
        body.append(f"\n       \u2192 {hint}", style="yellow")

    console.print(Panel(body, title="[bold red]Preflight checks failed[/bold red]"))
