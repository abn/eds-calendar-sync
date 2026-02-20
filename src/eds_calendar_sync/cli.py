"""
Command-line interface for EDS Calendar Sync.
"""

import sys
import logging
import argparse
from pathlib import Path
from configparser import ConfigParser
from typing import Dict

from .models import DEFAULT_STATE_DB, DEFAULT_CONFIG, SyncConfig, CalendarSyncError
from .db import migrate_calendar_ids_in_db
from .eds_client import get_calendar_display_info
from .sync import CalendarSynchronizer


def load_config_file(config_path: Path) -> Dict[str, str]:
    """Load configuration from INI file."""
    if not config_path.exists():
        return {}

    parser = ConfigParser()
    parser.read(config_path)

    if 'calendar-sync' not in parser:
        return {}

    return dict(parser['calendar-sync'])


def setup_logging(verbose: bool):
    """Configure logging output."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(levelname)s: %(message)s'
    )


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Calendar synchronization via Evolution Data Server (bidirectional by default)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Bidirectional sync (default)
  %(prog)s --work-calendar abc123 --personal-calendar xyz789

  # One-way sync: work → personal only
  %(prog)s --work-calendar abc123 --personal-calendar xyz789 --only-to-personal

  # One-way sync: personal → work only
  %(prog)s --work-calendar abc123 --personal-calendar xyz789 --only-to-work

  # Dry run to see what would happen
  %(prog)s --work-calendar abc123 --personal-calendar xyz789 --dry-run

  # Refresh: remove synced events and resync
  %(prog)s --work-calendar abc123 --personal-calendar xyz789 --refresh

  # Clear: remove all synced events
  %(prog)s --work-calendar abc123 --personal-calendar xyz789 --clear

  # Use configuration file
  %(prog)s --config ~/.config/eds-calendar-sync.conf

  # After GOA reconnection: update calendar IDs in state DB
  %(prog)s --migrate-calendar-ids \\
      --old-work-calendar OLD_UID --new-work-calendar NEW_UID \\
      [--old-personal-calendar OLD_UID --new-personal-calendar NEW_UID] \\
      [--dry-run]
        """
    )

    parser.add_argument(
        '--work-calendar',
        help='EDS calendar UID for work calendar'
    )

    parser.add_argument(
        '--personal-calendar',
        help='EDS calendar UID for personal calendar'
    )

    parser.add_argument(
        '--config',
        type=Path,
        default=DEFAULT_CONFIG,
        help=f'Configuration file path (default: {DEFAULT_CONFIG})'
    )

    parser.add_argument(
        '--state-db',
        type=Path,
        default=DEFAULT_STATE_DB,
        help=f'State database path (default: {DEFAULT_STATE_DB})'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without making changes'
    )

    parser.add_argument(
        '--refresh',
        action='store_true',
        help='Remove only synced events we created and resync (preserves other events)'
    )

    # Direction flags (mutually exclusive)
    direction_group = parser.add_mutually_exclusive_group()
    direction_group.add_argument(
        '--only-to-personal',
        action='store_true',
        help='One-way sync: work → personal only (default is bidirectional)'
    )
    direction_group.add_argument(
        '--only-to-work',
        action='store_true',
        help='One-way sync: personal → work only (default is bidirectional)'
    )

    parser.add_argument(
        '--clear',
        action='store_true',
        help='Remove all synced events created by this tool (uses metadata to identify)'
    )

    parser.add_argument(
        '--yes', '-y',
        action='store_true',
        help='Automatically confirm sync without prompting (always on for --dry-run)'
    )

    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose debug output'
    )

    parser.add_argument(
        '--migrate-calendar-ids',
        action='store_true',
        help='Update calendar IDs in the state DB after GOA reconnection changed UIDs'
    )
    parser.add_argument(
        '--old-work-calendar',
        metavar='UID',
        help='Old work calendar UID to replace (use with --migrate-calendar-ids)'
    )
    parser.add_argument(
        '--new-work-calendar',
        metavar='UID',
        help='New work calendar UID (use with --migrate-calendar-ids)'
    )
    parser.add_argument(
        '--old-personal-calendar',
        metavar='UID',
        help='Old personal calendar UID to replace (use with --migrate-calendar-ids)'
    )
    parser.add_argument(
        '--new-personal-calendar',
        metavar='UID',
        help='New personal calendar UID (use with --migrate-calendar-ids)'
    )

    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_arguments()
    setup_logging(args.verbose)

    logger = logging.getLogger(__name__)

    # Handle --migrate-calendar-ids before normal sync setup (no EDS connection needed)
    if args.migrate_calendar_ids:
        work_pair = (args.old_work_calendar, args.new_work_calendar)
        pers_pair = (args.old_personal_calendar, args.new_personal_calendar)

        # Validate: at least one fully specified pair
        if not (all(work_pair) or all(pers_pair)):
            logger.error(
                "--migrate-calendar-ids requires at least one fully specified pair:\n"
                "  --old-work-calendar UID --new-work-calendar UID, and/or\n"
                "  --old-personal-calendar UID --new-personal-calendar UID"
            )
            sys.exit(1)
        # Validate: no half-specified pair
        for label, pair in [('work', work_pair), ('personal', pers_pair)]:
            if any(pair) and not all(pair):
                logger.error(
                    f"Both --old-{label}-calendar and --new-{label}-calendar "
                    f"must be given together"
                )
                sys.exit(1)

        state_db_path = args.state_db
        logger.info("=" * 60)
        logger.info("Calendar ID Migration")
        logger.info("=" * 60)
        logger.info(f"State Database: {state_db_path}")
        if all(work_pair):
            logger.info(f"Work calendar:  {work_pair[0]} -> {work_pair[1]}")
        if all(pers_pair):
            logger.info(f"Personal:       {pers_pair[0]} -> {pers_pair[1]}")
        logger.info(f"Mode:           {'DRY RUN' if args.dry_run else 'LIVE'}")
        logger.info("=" * 60)

        if not state_db_path.exists():
            logger.error(f"State database not found: {state_db_path}")
            sys.exit(1)

        work_rows, pers_rows = migrate_calendar_ids_in_db(
            state_db_path,
            args.old_work_calendar, args.new_work_calendar,
            args.old_personal_calendar, args.new_personal_calendar,
            args.dry_run,
        )

        prefix = "[DRY RUN] Would update" if args.dry_run else "Updated"
        if all(work_pair):
            logger.info(f"{prefix} {work_rows} record(s) for work_calendar_id")
        if all(pers_pair):
            logger.info(f"{prefix} {pers_rows} record(s) for personal_calendar_id")
        if work_rows == 0 and pers_rows == 0:
            logger.warning("No matching records found — verify the old UIDs are correct")

        if not args.dry_run:
            logger.info("")
            logger.info("Next steps:")
            logger.info("  1. Update ~/.config/eds-calendar-sync.conf with the new UIDs")
            logger.info("  2. Run a normal sync to verify everything is working")
        sys.exit(0)

    # Load config file if it exists
    config_file = load_config_file(args.config)

    # Determine work and personal calendar IDs (CLI args override config file)
    work_id = args.work_calendar or config_file.get('work_calendar_id')
    personal_id = args.personal_calendar or config_file.get('personal_calendar_id')

    if not work_id or not personal_id:
        logger.error(
            "Work and personal calendar IDs must be provided via "
            "--work-calendar/--personal-calendar or in configuration file"
        )
        sys.exit(1)

    # Determine sync direction
    if args.only_to_personal:
        sync_direction = 'to-personal'
    elif args.only_to_work:
        sync_direction = 'to-work'
    else:
        sync_direction = 'both'  # Default is bidirectional

    # Build configuration
    config = SyncConfig(
        work_calendar_id=work_id,
        personal_calendar_id=personal_id,
        state_db_path=args.state_db,
        dry_run=args.dry_run,
        refresh=args.refresh,
        verbose=args.verbose,
        sync_direction=sync_direction,
        clear=args.clear,
        yes=args.yes
    )

    # Get calendar display information
    work_name, work_account, work_uid = get_calendar_display_info(config.work_calendar_id)
    personal_name, personal_account, personal_uid = get_calendar_display_info(
        config.personal_calendar_id
    )

    # Display configuration
    logger.info("=" * 60)
    logger.info("EDS Calendar Sync")
    logger.info("=" * 60)

    # Format calendar display with account info
    work_display = f"{work_name}"
    if work_account:
        work_display += f" ({work_account})"
    logger.info(f"Work Calendar:     {work_display}")
    logger.info(f"                   UID: {work_uid}")

    personal_display = f"{personal_name}"
    if personal_account:
        personal_display += f" ({personal_account})"
    logger.info(f"Personal Calendar: {personal_display}")
    logger.info(f"                   UID: {personal_uid}")

    logger.info(f"State Database:    {config.state_db_path}")

    if config.clear:
        logger.info("Operation:         CLEAR (remove all synced events)")
    else:
        # Display sync direction
        direction_display = {
            'both': 'BIDIRECTIONAL (work ↔ personal)',
            'to-personal': 'ONE-WAY (work → personal)',
            'to-work': 'ONE-WAY (personal → work)'
        }
        logger.info(f"Sync Direction:    {direction_display[config.sync_direction]}")
        if config.refresh:
            logger.info("Refresh:           YES (remove synced events and resync)")

    logger.info(f"Mode:              {'DRY RUN' if config.dry_run else 'LIVE'}")
    logger.info("=" * 60)

    # Confirmation prompt (skip if --yes or --dry-run)
    if not config.yes and not config.dry_run:
        try:
            response = input("Proceed with sync? [y/N]: ").strip().lower()
            if response not in ('y', 'yes'):
                logger.info("Sync cancelled by user")
                sys.exit(0)
        except EOFError:
            # Non-interactive mode without --yes flag
            logger.error(
                "Cannot prompt for confirmation in non-interactive mode. "
                "Use --yes to proceed."
            )
            sys.exit(1)

    # Run synchronization
    try:
        sync = CalendarSynchronizer(config)
        stats = sync.run()

        logger.info("=" * 60)
        logger.info("Sync Complete!")
        logger.info(f"  Added:    {stats.added}")
        logger.info(f"  Modified: {stats.modified}")
        logger.info(f"  Deleted:  {stats.deleted}")
        logger.info(f"  Errors:   {stats.errors}")
        logger.info("=" * 60)

        sys.exit(0 if stats.errors == 0 else 1)

    except CalendarSyncError as e:
        logger.error(f"Sync failed: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)
