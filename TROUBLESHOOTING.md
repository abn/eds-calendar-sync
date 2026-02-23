# Troubleshooting

## Preflight checks

Before every sync, `sync`, `refresh`, and `clear` run a quick preflight that validates:

1. **EDS registry reachable** — evolution-data-server is running
2. **Calendar UIDs exist** — both UIDs resolve in EDS
3. **Calendars connectable** — EDS can open a client session for each calendar
4. **State DB accessible** — the parent directory is writable and, if the DB already exists, it is readable and writable

When any check fails you will see a panel like:

```
╭─────────────── Preflight checks failed ───────────────╮
│   ✗  Work calendar: UID not found: abc123             │
│        → Run: eds-calendar-sync migrate               │
│   ✗  Personal calendar: Connection failed: …          │
│        → Account 'user@gmail.com' appears offline     │
│          — check GNOME Online Accounts                │
╰───────────────────────────────────────────────────────╯
```

The sync exits immediately so you can fix the problem before any changes are made.

Common causes and fixes:

| Failing check | Likely cause | Fix |
|---|---|---|
| EDS registry unreachable | `evolution-data-server` not running | Start GNOME Calendar or run `evolution-data-server &` |
| UID not found | Calendar was removed/re-added via GOA | Run `eds-calendar-sync migrate` |
| Calendar offline | GOA account disconnected | Open **Settings → Online Accounts** and reconnect |
| State DB not writable | Wrong permissions on `~/.local/share/` | Check directory permissions |

## Calendar Not Writable

```
Error: Read-only calendars can't be modified
```

**Solution**: Verify that your calendar is not read-only. Check with `eds-calendar-sync calendars`. Some calendars (like "Birthdays") are read-only.

## Duplicate Categories Error

```
Error: Duplicate category entries
```

This was a bug fixed in v1.0. Update to latest version. The fix ensures existing CATEGORIES are removed before adding the sync marker.

## Spurious Modifications

If sync keeps showing "Modified: N" when nothing changed, this was a bug fixed in v1.0. The tool now correctly tracks separate hashes for work and personal events.

## "Modified" count after manually deleting a synced event

If you delete a synced event directly from the personal (or work) calendar, the next sync will detect the missing event, silently recreate it, and count it as **Modified 1**. This is expected: the sync treats the source calendar as authoritative and restores the mirror. No warning is printed.

To permanently remove a synced pair, delete the event from the **source** calendar (the one that originated it); the next sync will then remove the mirror and count it as **Deleted 1**.

## Cannot Prompt in Non-Interactive Mode

```
Error: Cannot prompt for confirmation in non-interactive mode. Use --yes to proceed.
```

**Solution**: Add `--yes` flag when running from scripts or systemd:
```bash
eds-calendar-sync sync --work-calendar UID --personal-calendar UID --yes
```

## Systemd Service Fails

```bash
# Check detailed error
systemctl --user status eds-calendar-sync.service
journalctl --user -u eds-calendar-sync.service -n 100

# Common issues:
# 1. Missing --yes flag - required for automation
# 2. Wrong ExecStart path - edit ~/.config/systemd/user/eds-calendar-sync.service
# 3. Config file missing - ensure ~/.config/eds-calendar-sync.conf exists
# 4. EDS not running - check GNOME Calendar is set up
```

## State Database Error: "unable to open database file"

```
ERROR  Failed to update/recreate event ...: State database error
       (/home/user/.local/share/eds-calendar-sync-state.db): unable to open database file
```

This error occurs when the service can **read** the database but cannot **write** to it. SQLite
needs to create journal or WAL files (e.g. `eds-calendar-sync-state.db-journal`) in the **same
directory** as the database file when committing a write. If only the file itself is writable (not
its parent directory), reads succeed but writes fail mid-sync.

**Cause**: The bundled service file uses `ProtectHome=read-only` with `ReadWritePaths` pointing at
the DB file. Older versions of the service file specified the file path directly instead of the
parent directory:

```ini
# Wrong — journal files cannot be created
ReadWritePaths=%h/.local/share/eds-calendar-sync-state.db ...

# Correct — parent directory gives SQLite room to create auxiliary files
ReadWritePaths=%h/.local/share %h/.cache/evolution
```

**Fix**: Update your installed service file:

```bash
cp systemd/eds-calendar-sync.service ~/.config/systemd/user/eds-calendar-sync.service
systemctl --user daemon-reload
systemctl --user restart eds-calendar-sync.service
```

## Exchange/M365 Create Errors

If you see errors like:

```
ERROR  Failed to create personal event from ...: e-m365-error-quark: Cannot create calendar object: ErrorItemNotFound (2)
ERROR  Failed to create event ...: ExpandSeries can only be performed against a series. (400)
```

These are Exchange-specific rejections. Common causes and what the tool does automatically:

| Exchange Error | Cause | Handling |
|---|---|---|
| `ExpandSeries can only be performed against a series` | RECURRENCE-ID present (exception occurrence without master series in target) | Stripped from sanitized event |
| `ErrorItemNotFound` (create) | `STATUS:CANCELLED`, or a recurring series where every RRULE occurrence is covered by EXDATE (Exchange rejects creating an empty series), or vendor X-properties referencing source-tenant objects | STATUS stripped; cancelled and empty-series events skipped before creation |
| `ErrorItemNotFound` (create) — edge case | DTSTART has a timezone (e.g. `TZID=Europe/Berlin`) but UNTIL in the RRULE is a **date-only** value; libical's recurrence iterator does not reliably stop at UNTIL, generating spurious post-UNTIL occurrences that slip past the empty-series check | Fixed: UNTIL is extracted from the raw iCal string and used to cap the iterator; a timezone-free (floating) copy of DTSTART is used to avoid TZID resolution failures |

If errors persist, run with `--verbose` (global flag) — the sanitized iCal is printed before each create attempt, showing exactly which properties will be sent to Exchange.

## Calendar UIDs Changed After GOA Reconnection

When a GNOME Online Accounts connection is removed and re-added, EDS may assign new UIDs to the
calendar sources. Use the `migrate` subcommand to update the state database without losing sync
history or triggering a full resync.

`migrate` has three modes:

**Audit mode (no arguments) — recommended after a GOA reconnect:**
```bash
# Scans state DB for all UIDs, flags any that no longer resolve in EDS,
# then lets you pick a replacement for each missing one interactively.
eds-calendar-sync migrate --dry-run   # preview
eds-calendar-sync migrate             # apply
```

**Single-UID mode — you know the old UID, pick the new one interactively:**
```bash
eds-calendar-sync migrate OLD_UID --dry-run
eds-calendar-sync migrate OLD_UID
```

**Direct mode — you know both UIDs:**
```bash
eds-calendar-sync migrate OLD_UID NEW_UID --dry-run
eds-calendar-sync migrate OLD_UID NEW_UID
```

Each migration replaces the old UID in both the work and personal calendar ID
columns, so a single command handles whichever role the calendar played.

After migrating, update your config file and verify:
```bash
nano ~/.config/eds-calendar-sync.conf
eds-calendar-sync sync --dry-run
```

## Verbose Debugging

Run with `--verbose` (before the subcommand) to see detailed operations:
```bash
eds-calendar-sync --verbose sync --work-calendar UID --personal-calendar UID --dry-run
```
