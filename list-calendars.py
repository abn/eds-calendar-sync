#!/usr/bin/env python3
"""
List all calendars available in Evolution Data Server.

This helper script displays all calendar sources registered with EDS,
showing their UIDs (needed for configuration) and display names.
"""

import sys
import gi

gi.require_version('EDataServer', '1.2')
gi.require_version('ECal', '2.0')

from gi.repository import EDataServer, ECal


def main():
    """List all available EDS calendar sources."""
    print("=" * 80)
    print("Evolution Data Server - Available Calendars")
    print("=" * 80)
    print()

    try:
        # Connect to EDS registry
        registry = EDataServer.SourceRegistry.new_sync(None)
    except Exception as e:
        print(f"Error: Failed to connect to Evolution Data Server: {e}", file=sys.stderr)
        sys.exit(1)

    # Get all calendar sources
    sources = registry.list_sources(EDataServer.SOURCE_EXTENSION_CALENDAR)

    if not sources:
        print("No calendars found in Evolution Data Server.")
        print("\nMake sure you have configured calendar accounts in GNOME Calendar")
        print("or Evolution.")
        sys.exit(0)

    for i, source in enumerate(sources, 1):
        uid = source.get_uid()
        display_name = source.get_display_name()
        enabled = source.get_enabled()

        # Try to get parent source (account) name
        parent = source.get_parent()
        parent_name = ""
        if parent:
            parent_source = registry.ref_source(parent)
            if parent_source:
                parent_name = parent_source.get_display_name()

        print(f"{i}. {display_name}")
        print(f"   UID:     {uid}")
        if parent_name:
            print(f"   Account: {parent_name}")
        print(f"   Enabled: {'Yes' if enabled else 'No'}")

        # Check if it's writable
        try:
            client = ECal.Client.connect_sync(
                source,
                ECal.ClientSourceType.EVENTS,
                5,
                None
            )
            readonly = client.is_readonly()
            print(f"   Mode:    {'Read-only' if readonly else 'Read-write'}")
        except Exception:
            print(f"   Mode:    Unknown (unable to connect)")

        print()

    print("=" * 80)
    print("\nTo use a calendar for syncing, copy its UID and add it to your config file:")
    print("  ~/.config/eds-calendar-sync.conf")
    print("\nOr use it directly with command-line options:")
    print("  eds-calendar-sync.py --work-calendar <WORK_UID> --personal-calendar <PERSONAL_UID>")
    print("=" * 80)


if __name__ == '__main__':
    main()
