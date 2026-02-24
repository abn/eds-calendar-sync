"""
CalendarSynchronizer — thin orchestrator that delegates to sync submodules.
"""

import logging

import gi

gi.require_version("EDataServer", "1.2")
from gi.repository import EDataServer

from eds_calendar_sync.db import StateDatabase
from eds_calendar_sync.eds_client import EDSCalendarClient
from eds_calendar_sync.models import SyncConfig
from eds_calendar_sync.models import SyncStats
from eds_calendar_sync.sync.refresh import perform_clear
from eds_calendar_sync.sync.to_personal import run_one_way_to_personal
from eds_calendar_sync.sync.to_work import run_one_way_to_work
from eds_calendar_sync.sync.two_way import run_two_way


class CalendarSynchronizer:
    """Main synchronization engine."""

    def __init__(self, config: SyncConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.stats = SyncStats()

    def run(self) -> SyncStats:
        """Execute the synchronization process."""
        self.logger.info("Connecting to Evolution Data Server...")
        registry = EDataServer.SourceRegistry.new_sync(None)

        work_client = EDSCalendarClient(registry, self.config.work_calendar_id)
        personal_client = EDSCalendarClient(registry, self.config.personal_calendar_id)

        work_client.connect()
        personal_client.connect()

        # Auto-detect work account email for PARTSTAT=DECLINED detection.
        # config-file value (set in cli.py) takes precedence if already present.
        if self.config.work_account_email is None:
            detected = work_client.get_account_email()
            if detected:
                self.logger.debug("Auto-detected work account email: %s", detected)
                self.config.work_account_email = detected

        with StateDatabase(
            self.config.state_db_path,
            self.config.work_calendar_id,
            self.config.personal_calendar_id,
        ) as state_db:
            state_db.migrate_if_needed(self.config.refresh or self.config.clear)

            args = (self.config, self.stats, self.logger, work_client, personal_client, state_db)

            if self.config.clear:
                perform_clear(*args)
            elif self.config.sync_direction == "both":
                run_two_way(*args)
            elif self.config.sync_direction == "to-personal":
                run_one_way_to_personal(*args)
            elif self.config.sync_direction == "to-work":
                run_one_way_to_work(*args)

        return self.stats
