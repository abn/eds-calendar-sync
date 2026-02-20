"""
SQLite state persistence for calendar sync tracking.
"""

import sqlite3
import time
import logging
from pathlib import Path
from typing import Optional, Dict

from .models import CalendarSyncError


class StateDatabase:
    """Manages SQLite state database for sync tracking."""

    def __init__(self, db_path: Path, work_calendar_id: str, personal_calendar_id: str):
        self.db_path = db_path
        self.work_calendar_id = work_calendar_id
        self.personal_calendar_id = personal_calendar_id
        self.conn: Optional[sqlite3.Connection] = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def connect(self):
        """Initialize and connect to the state database."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row  # Enable column access by name
        self._init_schema()

    def _init_schema(self):
        """Create the sync_state table if it doesn't exist (new schema)."""
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS sync_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_calendar_id TEXT NOT NULL,
                personal_calendar_id TEXT NOT NULL,
                source_uid TEXT NOT NULL,
                target_uid TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                target_hash TEXT NOT NULL,
                origin TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                last_sync_at INTEGER NOT NULL,
                UNIQUE(work_calendar_id, personal_calendar_id, source_uid)
            )
        ''')
        self.conn.commit()

    def migrate_if_needed(self, is_refresh_or_clear: bool):
        """
        Detect and handle schema migration from the old single-pair format.

        Old schema had no work_calendar_id/personal_calendar_id columns and used
        an inverted source/target convention for --only-to-work records.

        If old schema is detected:
        - Rebuilds the table with the new schema.
        - Assigns the current calendar pair to all existing rows (assumes they
          all belong to this pair — correct for any single-pair user).
        - If inverted records (origin='target') are present and this is not a
          refresh/clear run, raises CalendarSyncError asking the user to run
          --refresh first (those records may be in the old inverted convention
          and cannot be safely kept without re-verification).
        - On a refresh/clear run, deletes inverted records so the refresh
          fallback scan can rebuild state cleanly from calendar metadata.
        """
        logger = logging.getLogger(__name__)

        # Detect old schema by checking for the work_calendar_id column
        cursor = self.conn.execute("PRAGMA table_info(sync_state)")
        columns = {row['name'] for row in cursor.fetchall()}
        if 'work_calendar_id' in columns:
            return  # Already on new schema, nothing to do

        logger.info("Migrating state database to new schema (adding calendar pair columns)...")

        # Count inverted records before rebuilding
        inverted_count = self.conn.execute(
            "SELECT COUNT(*) FROM sync_state WHERE origin = 'target'"
        ).fetchone()[0]
        total_count = self.conn.execute(
            "SELECT COUNT(*) FROM sync_state"
        ).fetchone()[0]

        # Rebuild the table with the new schema, carrying over existing rows
        # with empty calendar pair placeholders (filled in below).
        self.conn.executescript('''
            CREATE TABLE sync_state_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_calendar_id TEXT NOT NULL,
                personal_calendar_id TEXT NOT NULL,
                source_uid TEXT NOT NULL,
                target_uid TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                target_hash TEXT NOT NULL,
                origin TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                last_sync_at INTEGER NOT NULL,
                UNIQUE(work_calendar_id, personal_calendar_id, source_uid)
            );
            INSERT INTO sync_state_new
                (work_calendar_id, personal_calendar_id,
                 source_uid, target_uid,
                 source_hash, target_hash, origin,
                 created_at, last_sync_at)
                SELECT '', '', source_uid, target_uid,
                       source_hash, target_hash, origin,
                       created_at, last_sync_at
                FROM sync_state;
            DROP TABLE sync_state;
            ALTER TABLE sync_state_new RENAME TO sync_state;
        ''')

        # Handle inverted (origin='target') records from old --only-to-work runs
        if inverted_count > 0:
            if is_refresh_or_clear:
                # Delete them so the refresh fallback scan rebuilds state cleanly
                self.conn.execute("DELETE FROM sync_state WHERE origin = 'target'")
                logger.warning(
                    f"Migration: deleted {inverted_count} old sync record(s) with "
                    f"origin='target' that could not be safely migrated. "
                    f"A calendar scan will be used to find managed events to clean up."
                )
            else:
                # Assign pair IDs to origin='source' rows so they're not lost,
                # then raise — the user must run --refresh to handle the rest.
                remaining = total_count - inverted_count
                if remaining > 0:
                    self.conn.execute(
                        "UPDATE sync_state SET work_calendar_id = ?, personal_calendar_id = ? "
                        "WHERE origin = 'source'",
                        (self.work_calendar_id, self.personal_calendar_id)
                    )
                self.conn.commit()
                raise CalendarSyncError(
                    f"State database schema has been updated. "
                    f"Found {inverted_count} record(s) with origin='target' that "
                    f"may be in an old inverted format and cannot be safely migrated "
                    f"automatically.\n"
                    f"Please run with --refresh to clean up and re-sync, "
                    f"or --clear to remove all synced events."
                )

        # Assign the current calendar pair to all remaining rows
        if total_count > 0:
            self.conn.execute(
                "UPDATE sync_state SET work_calendar_id = ?, personal_calendar_id = ?",
                (self.work_calendar_id, self.personal_calendar_id)
            )
            kept = total_count - inverted_count
            logger.info(
                f"Migration complete: kept {kept} existing record(s), "
                f"assigned to current calendar pair."
            )
        else:
            logger.info("Migration complete: no existing records to migrate.")

        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Query methods — all scoped to the current (work, personal) pair     #
    # ------------------------------------------------------------------ #

    def get_all_state(self) -> Dict[str, Dict[str, str]]:
        """Retrieve work→personal sync records for this calendar pair.

        Returns records with origin='source' only (work-originated events).
        Keyed by source_uid (work event UID).
        """
        cursor = self.conn.execute(
            "SELECT source_uid, target_uid, source_hash FROM sync_state "
            "WHERE work_calendar_id = ? AND personal_calendar_id = ? AND origin = 'source'",
            (self.work_calendar_id, self.personal_calendar_id)
        )
        return {
            row[0]: {'target_uid': row[1], 'hash': row[2]}
            for row in cursor.fetchall()
        }

    def get_all_state_by_target(self) -> Dict[str, Dict[str, str]]:
        """Retrieve personal→work sync records for this calendar pair.

        Returns records with origin='target' only (personal-originated events).
        Keyed by target_uid (personal event UID).
        """
        cursor = self.conn.execute(
            "SELECT source_uid, target_uid, target_hash FROM sync_state "
            "WHERE work_calendar_id = ? AND personal_calendar_id = ? AND origin = 'target'",
            (self.work_calendar_id, self.personal_calendar_id)
        )
        return {
            row[1]: {'source_uid': row[0], 'hash': row[2]}
            for row in cursor.fetchall()
        }

    def get_all_state_bidirectional(self) -> list:
        """Retrieve all sync state records for this calendar pair."""
        cursor = self.conn.execute(
            "SELECT id, source_uid, target_uid, source_hash, target_hash, "
            "origin, created_at, last_sync_at FROM sync_state "
            "WHERE work_calendar_id = ? AND personal_calendar_id = ?",
            (self.work_calendar_id, self.personal_calendar_id)
        )
        return cursor.fetchall()

    def get_by_source_uid(self, source_uid: str) -> Optional[sqlite3.Row]:
        """Get state record by source UID for this calendar pair."""
        cursor = self.conn.execute(
            "SELECT * FROM sync_state "
            "WHERE work_calendar_id = ? AND personal_calendar_id = ? "
            "AND source_uid = ? LIMIT 1",
            (self.work_calendar_id, self.personal_calendar_id, source_uid)
        )
        return cursor.fetchone()

    def get_by_target_uid(self, target_uid: str) -> Optional[sqlite3.Row]:
        """Get state record by target UID for this calendar pair."""
        cursor = self.conn.execute(
            "SELECT * FROM sync_state "
            "WHERE work_calendar_id = ? AND personal_calendar_id = ? "
            "AND target_uid = ? LIMIT 1",
            (self.work_calendar_id, self.personal_calendar_id, target_uid)
        )
        return cursor.fetchone()

    def insert(self, source_uid: str, target_uid: str, content_hash: str):
        """Insert a new sync state record (one-way compatibility)."""
        timestamp = int(time.time())
        self.conn.execute(
            "INSERT INTO sync_state "
            "(work_calendar_id, personal_calendar_id, "
            " source_uid, target_uid, source_hash, target_hash, "
            " origin, created_at, last_sync_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'source', ?, ?)",
            (self.work_calendar_id, self.personal_calendar_id,
             source_uid, target_uid, content_hash, content_hash,
             timestamp, timestamp)
        )

    def insert_bidirectional(self, source_uid: str, target_uid: str,
                             source_hash: str, target_hash: str, origin: str):
        """Insert new bidirectional sync record for this calendar pair."""
        timestamp = int(time.time())
        self.conn.execute(
            "INSERT INTO sync_state "
            "(work_calendar_id, personal_calendar_id, "
            " source_uid, target_uid, source_hash, target_hash, "
            " origin, created_at, last_sync_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.work_calendar_id, self.personal_calendar_id,
             source_uid, target_uid, source_hash, target_hash,
             origin, timestamp, timestamp)
        )

    def update_hash(self, source_uid: str, content_hash: str):
        """Update the hash for an existing record (one-way compatibility)."""
        self.conn.execute(
            "UPDATE sync_state SET source_hash = ?, target_hash = ?, last_sync_at = ? "
            "WHERE work_calendar_id = ? AND personal_calendar_id = ? AND source_uid = ?",
            (content_hash, content_hash, int(time.time()),
             self.work_calendar_id, self.personal_calendar_id, source_uid)
        )

    def update_hashes(self, source_uid: str, target_uid: str, source_hash: str, target_hash: str):
        """Update both hashes after successful sync."""
        self.conn.execute(
            "UPDATE sync_state "
            "SET source_hash = ?, target_hash = ?, last_sync_at = ? "
            "WHERE work_calendar_id = ? AND personal_calendar_id = ? "
            "AND source_uid = ? AND target_uid = ?",
            (source_hash, target_hash, int(time.time()),
             self.work_calendar_id, self.personal_calendar_id,
             source_uid, target_uid)
        )

    def delete(self, source_uid: str):
        """Delete a sync state record by source UID for this calendar pair."""
        self.conn.execute(
            "DELETE FROM sync_state "
            "WHERE work_calendar_id = ? AND personal_calendar_id = ? AND source_uid = ?",
            (self.work_calendar_id, self.personal_calendar_id, source_uid)
        )

    def delete_by_pair(self, source_uid: str, target_uid: str):
        """Delete by the (source_uid, target_uid) pair for this calendar pair."""
        self.conn.execute(
            "DELETE FROM sync_state "
            "WHERE work_calendar_id = ? AND personal_calendar_id = ? "
            "AND source_uid = ? AND target_uid = ?",
            (self.work_calendar_id, self.personal_calendar_id, source_uid, target_uid)
        )

    def clear_all(self):
        """Remove all state records for this calendar pair (for refresh/clear)."""
        self.conn.execute(
            "DELETE FROM sync_state "
            "WHERE work_calendar_id = ? AND personal_calendar_id = ?",
            (self.work_calendar_id, self.personal_calendar_id)
        )

    def commit(self):
        """Commit pending transactions."""
        if self.conn:
            self.conn.commit()

    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None


def migrate_calendar_ids_in_db(
    db_path: Path,
    old_work_id,
    new_work_id,
    old_personal_id,
    new_personal_id,
    dry_run: bool,
) -> tuple:
    """
    Replace calendar IDs in all state records.

    Used after a GOA reconnection changes EDS calendar UIDs. Operates directly on the
    raw schema without the pair-scoped StateDatabase machinery.

    Returns (work_rows_changed, personal_rows_changed).
    """
    conn = sqlite3.connect(db_path)
    work_rows = personal_rows = 0
    try:
        if old_work_id and new_work_id:
            cur = conn.execute(
                "SELECT COUNT(*) FROM sync_state WHERE work_calendar_id = ?", (old_work_id,)
            )
            work_rows = cur.fetchone()[0]
            if not dry_run:
                conn.execute(
                    "UPDATE sync_state SET work_calendar_id = ? WHERE work_calendar_id = ?",
                    (new_work_id, old_work_id),
                )
        if old_personal_id and new_personal_id:
            cur = conn.execute(
                "SELECT COUNT(*) FROM sync_state WHERE personal_calendar_id = ?",
                (old_personal_id,)
            )
            personal_rows = cur.fetchone()[0]
            if not dry_run:
                conn.execute(
                    "UPDATE sync_state SET personal_calendar_id = ? "
                    "WHERE personal_calendar_id = ?",
                    (new_personal_id, old_personal_id),
                )
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return work_rows, personal_rows
