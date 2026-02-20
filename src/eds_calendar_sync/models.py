"""
Pure data models — no EDS or sqlite imports.
"""

from dataclasses import dataclass
from pathlib import Path

DEFAULT_STATE_DB = Path.home() / ".local/share/eds-calendar-sync-state.db"
DEFAULT_CONFIG = Path.home() / ".config/eds-calendar-sync.conf"


class CalendarSyncError(Exception):
    """Base exception for calendar sync errors."""

    pass


@dataclass
class SyncConfig:
    """Configuration for calendar sync operation."""

    work_calendar_id: str
    personal_calendar_id: str
    state_db_path: Path
    dry_run: bool = False
    refresh: bool = False
    verbose: bool = False
    sync_direction: str = "both"  # 'both', 'to-personal', 'to-work'
    clear: bool = False
    yes: bool = False  # Auto-confirm without prompting


@dataclass
class SyncStats:
    """Statistics for sync operation."""

    added: int = 0
    modified: int = 0
    deleted: int = 0
    errors: int = 0
