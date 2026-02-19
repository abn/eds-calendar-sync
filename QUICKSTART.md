# Quick Start Guide

Get up and running with EDS Calendar Sync in 5 minutes.

## Prerequisites

- Fedora Linux with GNOME Desktop
- GNOME Calendar configured with:
  - Work calendar (Exchange/Outlook/Microsoft 365)
  - Personal calendar (Google Calendar)

## Installation

```bash
# Install required packages (usually pre-installed on Fedora with GNOME)
sudo dnf install python3-gobject evolution-data-server

# Make scripts executable
chmod +x eds-calendar-sync.py list-calendars.py

# Copy to user bin directory (optional)
mkdir -p ~/.local/bin
cp eds-calendar-sync.py list-calendars.py ~/.local/bin/
```

## Configuration

### 1. Find Your Calendar UIDs

```bash
./list-calendars.py
```

Output example:
```
1. Work Calendar
   UID:     d19280dcbb91f8ebcdbbb2adb7d502bc1d866fda
   Account: Microsoft 365
   Enabled: Yes
   Mode:    Read-write

2. Personal
   UID:     02e0b7e48f4e0dbfb2c91861a8e184a75617e193
   Account: Google
   Enabled: Yes
   Mode:    Read-write
```

### 2. Create Configuration File

```bash
mkdir -p ~/.config
cp eds-calendar-sync.conf.example ~/.config/eds-calendar-sync.conf
nano ~/.config/eds-calendar-sync.conf
```

Update with your calendar UIDs:

```ini
[calendar-sync]
work_calendar_id = d19280dcbb91f8ebcdbbb2adb7d502bc1d866fda
personal_calendar_id = 02e0b7e48f4e0dbfb2c91861a8e184a75617e193
```

## Usage

### First Run — Dry Run (Recommended)

See what will happen without making changes:

```bash
eds-calendar-sync.py --dry-run
```

Expected output:
```
============================================================
EDS Calendar Sync
============================================================
Work Calendar:     Company Calendar (work.user@company.com)
                   UID: d19280dcbb91f8ebcdbbb2adb7d502bc1d866fda
Personal Calendar: Personal Calendar (personal.user@gmail.com)
                   UID: 02e0b7e48f4e0dbfb2c91861a8e184a75617e193
State Database:    /home/user/.local/share/eds-calendar-sync-state.db
Sync Direction:    BIDIRECTIONAL (work ↔ personal)
Mode:              DRY RUN
============================================================
[DRY RUN] Would CREATE event: Meeting with Team -> <uuid>
...
============================================================
Sync Complete!
  Added:    N
  Modified: 0
  Deleted:  0
  Errors:   0
============================================================
```

### Actual Sync

Once you're satisfied with the dry run:

```bash
eds-calendar-sync.py
```

The tool will show configuration and prompt for confirmation before making any changes.

### Automatic Syncing

Install the systemd timer to sync every 15 minutes:

```bash
# Copy service files
mkdir -p ~/.config/systemd/user
cp systemd/eds-calendar-sync.service ~/.config/systemd/user/
cp systemd/eds-calendar-sync.timer ~/.config/systemd/user/

# Reload systemd and enable the timer
systemctl --user daemon-reload
systemctl --user enable --now eds-calendar-sync.timer

# Verify it's running
systemctl --user list-timers eds-calendar-sync.timer
```

## Verify It's Working

1. **Check your Personal calendar** (in GNOME Calendar or Google Calendar web):
   - You should see busy blocks from your Work calendar
   - Titles are preserved
   - Details, locations, and attendees are stripped

2. **View logs**:
   ```bash
   journalctl --user -u eds-calendar-sync.service -f
   ```

3. **Check next run time**:
   ```bash
   systemctl --user list-timers eds-calendar-sync.timer
   ```

## Troubleshooting

### No calendars found

```bash
# Check if Evolution Data Server is running
systemctl --user status evolution-source-registry

# Verify calendars in GNOME Calendar
gnome-calendar
```

### Sync fails

```bash
# Run with verbose logging
eds-calendar-sync.py --verbose --dry-run

# Check logs
journalctl --user -u eds-calendar-sync.service -n 100
```

### Reset everything and start over

```bash
# Stop timer
systemctl --user stop eds-calendar-sync.timer

# Do a full refresh (removes synced events and resyncs)
eds-calendar-sync.py --refresh --dry-run  # Check first
eds-calendar-sync.py --refresh --yes       # Execute
```

## What Gets Synced?

| Property | Work → Personal | Personal → Work |
|----------|-----------------|-----------------|
| Event Title | ✅ Kept | ❌ Replaced with "Busy" |
| Start/End Time | ✅ Kept | ✅ Kept |
| Recurrence Rules | ✅ Kept | ✅ Kept |
| Description | ❌ Removed | ❌ Removed |
| Location | ❌ Removed | ❌ Removed |
| Attendees | ❌ Removed | ❌ Removed |
| Organizer | ❌ Removed | ❌ Removed |
| Reminders | ❌ Removed | ❌ Removed |
| Status / X-properties | ❌ Removed | ❌ Removed |
| Cancelled events | ⏭ Skipped | ⏭ Skipped |
| Declined / free-time events (TRANSP:TRANSPARENT) | ⏭ Skipped | ⏭ Skipped |
| Empty recurring series | ⏭ Skipped | ⏭ Skipped |

## Daily Usage

Once configured, the sync runs automatically every 15 minutes. You don't need to do anything!

- Work events appear on your personal calendar within 15 minutes
- Personal events appear in work calendar as "Busy"
- Updated events sync automatically
- Deleted events are removed from the mirror calendar
- All sensitive details remain private

## Need Help?

See the full [README.md](README.md) for detailed documentation.
