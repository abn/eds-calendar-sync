# Quick Start Guide

Get up and running with EDS Calendar Sync in 5 minutes.

## Prerequisites

- Fedora Linux with GNOME Desktop
- GNOME Calendar configured with:
  - Work calendar (Exchange/Outlook/Microsoft 365)
  - Personal calendar (Google Calendar)

## Installation

```bash
# Install system GObject Introspection libraries (usually pre-installed on Fedora with GNOME)
sudo dnf install python3-gobject evolution-data-server

# Install into your user path (recommended)
pip install --user -e .
```

Alternatively, use Poetry (Poetry 2.x required):

```bash
pipx install poetry
poetry install   # creates a venv with system-site-packages enabled for gi access
poetry shell     # activate, then run eds-calendar-sync directly
```

## Configuration

### 1. Find Your Calendar UIDs

```bash
eds-calendar-sync calendars
```

Output example:
```
 Display Name / UID                        Account                  Mode
 ────────────────────────────────────────────────────────────────────────────
 Work Calendar                             work.user@company.com    Read-write
 d19280dcbb91f8ebcdbbb2adb7d502bc1d866fda
 Personal                                  user@gmail.com           Read-write
 02e0b7e48f4e0dbfb2c91861a8e184a75617e193
```

The UID is displayed on its own line so it is always fully visible and easy to copy,
even in narrow terminals.

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

### First Run — Interactive Wizard (Recommended)

Run `sync` with no arguments to launch a guided setup wizard:

```bash
eds-calendar-sync sync
```

The wizard asks you to pick your work calendar, personal calendar, and sync
direction (↔ bidirectional / → work→personal / ← personal→work) from numbered
lists. Config file values are shown as hints so you can quickly re-select them.

To preview changes without making them:

```bash
eds-calendar-sync sync --dry-run
```

Once you're happy, run it for real — the wizard selections serve as confirmation
so no extra prompt is shown.

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

1. **Check sync status** — shows configured calendars, tracked event counts, and last sync time:
   ```bash
   eds-calendar-sync status
   ```

2. **Check your Personal calendar** (in GNOME Calendar or Google Calendar web):
   - You should see busy blocks from your Work calendar
   - Titles are preserved
   - Details, locations, and attendees are stripped

3. **View logs**:
   ```bash
   journalctl --user -u eds-calendar-sync.service -f
   ```

4. **Check next run time**:
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
# Check overall status first
eds-calendar-sync status

# Run with verbose logging
eds-calendar-sync --verbose sync --dry-run

# Check logs
journalctl --user -u eds-calendar-sync.service -n 100
```

### Reset everything and start over

```bash
# Stop timer
systemctl --user stop eds-calendar-sync.timer

# Do a full refresh (removes synced events and resyncs)
eds-calendar-sync refresh --dry-run   # Check first
eds-calendar-sync refresh --yes       # Execute
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
| Declined recurring instances (RECURRENCE-ID + date in master EXDATE) | ⏭ Skipped | ⏭ Skipped |
| Free-time events (TRANSP:TRANSPARENT) | ⏭ Skipped | ⏭ Skipped |
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
