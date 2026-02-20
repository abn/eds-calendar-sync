# EDS Calendar Sync — Agent Reference

## 1. Overview

`eds-calendar-sync` is a standalone Python utility that synchronizes calendar events between a
"Work" calendar and a "Personal" calendar via the Evolution Data Server (EDS) local cache.

Primary goals:

1. **Bidirectional availability blocking** — keep both calendars aware of busy times from the
   other, without leaking sensitive details.
2. **Privacy/sanitization** — strip corporate data (descriptions, attendees, locations, alarms)
   before writing to the personal calendar; strip personal details before writing to the work
   calendar (appearing only as "Busy").
3. **Platform stability** — bypass Microsoft Exchange Web Services (EWS) limitations (UID
   rewriting, Organizer restrictions) and Google CalDAV strictness by operating on the EDS local
   cache rather than the remote servers directly.


## 2. Architecture

### 2.1 Component Model

| Role | Calendar | Access |
|------|----------|--------|
| Work | Exchange/Outlook (EDS cache) | Read + Write |
| Personal | Google/GNOME Online Accounts (EDS cache) | Read + Write |
| State | SQLite3 (`~/.local/share/eds-calendar-sync-state.db`) | Read + Write |
| Config | INI file (`~/.config/eds-calendar-sync.conf`) | Read |

### 2.2 Data Flow

```
[Exchange server] ←→ [EDS Work cache]
                            ↕ (bidirectional by default)
                     [Python Engine]
                            ↕
[Google CalDAV]   ←→ [EDS Personal cache]
```

The engine operates entirely on the local EDS caches. EDS handles syncing those caches with the
remote servers independently.

### 2.3 Key Classes

| Class | Responsibility |
|-------|---------------|
| `SyncConfig` | Dataclass holding all runtime configuration |
| `SyncStats` | Counters: added, modified, deleted, errors |
| `StateDatabase` | SQLite wrapper — CRUD for sync state records |
| `EDSCalendarClient` | EDS connection wrapper — connect, get, create, modify, remove events |
| `EventSanitizer` | Sanitization logic — strip properties, set managed marker, apply mode |
| `CalendarSynchronizer` | Main sync engine — dispatches to one-way or bidirectional flows |


## 3. Sync Modes

### 3.1 Bidirectional (default)

Both calendars are synced with each other. Each event has an `origin` that determines which
calendar is authoritative for updates:

- **Work → Personal** (`origin = 'source'`): work events are copied to personal with `normal`
  sanitization (title preserved). Work is authoritative; manual edits to the personal copy are
  overwritten on next sync.
- **Personal → Work** (`origin = 'target'`): personal events are copied to work with `busy`
  sanitization (title replaced with "Busy"). Personal is authoritative; manual edits to the work
  copy are overwritten on next sync.

Deletion semantics:
- If the **authoritative** calendar's event is deleted, the copy is also deleted.
- If only the **copy** is deleted, nothing happens (it will be recreated on next sync if the
  original still exists — or the pair record is cleaned from state).

### 3.2 One-Way: Work → Personal (`--to-personal`)

Work events are synced into the personal calendar using `normal` sanitization. Personal events are
ignored. The personal calendar is treated as a pure write target.

### 3.3 One-Way: Personal → Work (`--to-work`)

Personal events are synced into the work calendar using `busy` sanitization. Work events are
ignored. The work calendar is treated as a pure write target.


## 4. Sanitization

All events written by the tool are sanitized before being written to the destination calendar.

### 4.1 Properties Always Stripped

| Property | Reason |
|----------|--------|
| `DESCRIPTION` | Contains sensitive/private meeting content |
| `LOCATION` | May contain sensitive addresses |
| `ATTACH` | File attachments with potentially sensitive data |
| `URL` | Meeting links (Teams, Zoom, etc.) |
| `ORGANIZER` | Prevents "User is not organizer" errors (EWS error 10500) |
| `ATTENDEE` | Prevents phantom email notifications to colleagues |
| `VALARM` sub-components | Prevents duplicate notifications on the user's device |

### 4.2 Sanitization Modes

| Mode | Used when | SUMMARY handling |
|------|-----------|-----------------|
| `normal` | Work → Personal | Preserved as-is |
| `busy` | Personal → Work | Replaced with `"Busy"` |

### 4.3 Properties Preserved

Everything not listed above is kept: `SUMMARY` (in `normal` mode), `DTSTART`, `DTEND`, `RRULE`,
`EXDATE`, `STATUS`, `TRANSP`, `CLASS`, and any other properties.

### 4.4 Added Properties

| Property | Value | Purpose |
|----------|-------|---------|
| `UID` | Fresh UUIDv4 | Disconnect from source tracking; avoids MS365 UID rewriting issues |
| `CATEGORIES` | `CALENDAR-SYNC-MANAGED` | Identifies the event as managed by this tool |

`CALENDAR-SYNC-MANAGED` is stored in the standard `CATEGORIES` property (not an X-property)
because Microsoft 365 strips X-properties and `COMMENT` during sync, which would break managed
event detection after a round-trip through Exchange.

### 4.5 Server UID Rewriting

After creating an event, the script always uses the UID returned by `create_object_sync()` rather
than the one it generated. Microsoft 365 (EWS) rewrites UIDs assigned by external clients; using
the server-assigned UID ensures the state database stays accurate.


## 5. State Management

### 5.1 Database Location

Default: `~/.local/share/eds-calendar-sync-state.db`
Override: `--state-db PATH`

### 5.2 Schema

```sql
CREATE TABLE IF NOT EXISTS sync_state (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    work_calendar_id     TEXT    NOT NULL,  -- EDS source UID of the work calendar
    personal_calendar_id TEXT    NOT NULL,  -- EDS source UID of the personal calendar
    source_uid           TEXT    NOT NULL,  -- work event UID (or UID::RID::date for exceptions)
    target_uid           TEXT    NOT NULL,  -- personal event UID
    source_hash          TEXT    NOT NULL,  -- hash of the work event iCal
    target_hash          TEXT    NOT NULL,  -- hash of the personal event iCal (sanitized version)
    origin               TEXT    NOT NULL,  -- 'source' = work authoritative, 'target' = personal authoritative
    created_at           INTEGER NOT NULL,  -- Unix timestamp of first sync
    last_sync_at         INTEGER NOT NULL,  -- Unix timestamp of last sync
    UNIQUE(work_calendar_id, personal_calendar_id, source_uid)
);
```

Records are partitioned by `(work_calendar_id, personal_calendar_id)` so a single DB can safely
serve multiple calendar pairs without interference. `clear` and `refresh` only affect the pair
currently specified via flags / config.

### 5.3 Change Detection and Volatile Property Normalization

Hashes are computed with `SHA256` over the normalized iCal string. Before hashing, the following
volatile server-added properties are removed to prevent false-positive change detection:

- `DTSTAMP`
- `LAST-MODIFIED`
- `CREATED`
- `SEQUENCE`

Both the source and target hashes are stored independently. On update, the target event is fetched
back from EDS after writing (to capture any server-added properties) and its hash is stored.

### 5.4 Fallback: Metadata Scan

When `refresh` or `clear` is requested and the state database is empty (e.g. was deleted or
migrated), the tool falls back to scanning calendars for events carrying the
`CALENDAR-SYNC-MANAGED` category and treats those as managed events.


## 6. Operational Modes

### 6.1 Normal Sync (default)

Connects to both calendars, loads state, processes creates/updates/deletes, commits.

### 6.2 Dry Run (`--dry-run`)

Logs all operations that *would* be performed without making any changes. Automatically skips the
confirmation prompt.

### 6.3 Refresh (`refresh` subcommand)

Before syncing, removes only the managed events we created (identified via state DB, falling back
to CATEGORIES scan if state is empty), clears the state DB, then proceeds with a normal sync as if
starting fresh. Non-managed events are untouched.

Respects direction:
- `both` (default): removes managed events from both calendars
- `--to-personal`: removes managed events from personal calendar only
- `--to-work`: removes managed events from work calendar only

### 6.4 Clear (`clear` subcommand)

Removes all managed events we created (identified via CATEGORIES scan) and clears the state DB.
Does **not** resync afterward.

Scope flags (distinct from the `--to-personal`/`--to-work` direction flags used by `sync`/`refresh`):
- `--personal`: remove managed events from the personal calendar only (created by work→personal sync)
- `--work`: remove managed events from the work calendar only (created by personal→work sync)
- *(no flag)*: remove from both calendars (default)

### 6.5 Status (`status` subcommand)

Read-only. Renders two sections without touching EDS event data:

1. **Configuration panel** — config file path and state DB path with existence indicators;
   configured calendar UIDs resolved to display names via EDS (best-effort; falls back to
   truncated UID if EDS is unavailable or the source is not found).
2. **Per-pair state panels** — one panel per `(work_calendar_id, personal_calendar_id)` pair
   found in the DB. Each panel shows a table with direction (`Work → Personal` /
   `Personal → Work`), tracked event count, and last-sync timestamp. The currently configured
   pair (from the config file) is labelled `(configured)`.

Uses `query_status_all_pairs()` in `db.py` — a raw `sqlite3` query that does not instantiate
`StateDatabase` and does not require knowing the calendar pair in advance.

### 6.6 Interactive Sync Wizard (`sync` with no arguments)

When `sync` is invoked with no calendar or direction flags, a three-step wizard runs:

1. **Work calendar** — numbered EDS calendar picker (config file value shown as hint)
2. **Personal calendar** — same picker
3. **Sync direction** — `1 ↔ Bidirectional` (default) / `2 → Work→Personal` / `3 ← Personal→Work`

Wizard selections imply `--yes`, so the normal "Proceed?" prompt is suppressed.
The wizard is bypassed whenever any of `--work-calendar`, `--personal-calendar`,
`--to-personal`, or `--to-work` is present.

### 6.7 Migrate (`migrate` subcommand)

Updates calendar source UIDs in the state DB after a GOA reconnection. Operates
directly on the raw SQLite schema (no `StateDatabase` context needed) and replaces
the old UID in **both** `work_calendar_id` and `personal_calendar_id` columns.

Three modes:

| Invocation | Behaviour |
|-----------|-----------|
| `migrate` (no args) | **Audit mode**: queries all distinct UIDs from the DB, resolves each against EDS, displays an audit table with ✓ Active / ✗ Missing status, then for each missing UID shows the EDS picker and prompts for a replacement (0 to skip) |
| `migrate OLD` | Shows EDS picker, user selects replacement |
| `migrate OLD NEW` | Replaces immediately without prompting |

All modes accept `--dry-run`.

### 6.8 Confirmation Prompt

By default, the tool displays sync configuration and prompts `Proceed? [y/N]` before making
changes. This is skipped automatically when:
- `--yes` / `-y` is passed
- `--dry-run` is active
- The interactive sync wizard was used (wizard selections serve as confirmation)


## 7. CLI Reference

```
eds-calendar-sync [--config PATH] [--state-db PATH] [--verbose] COMMAND [OPTIONS]
```

### Global options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--config` / `-c PATH` | path | `~/.config/eds-calendar-sync.conf` | INI config file |
| `--state-db PATH` | path | `~/.local/share/eds-calendar-sync-state.db` | SQLite state DB |
| `--verbose` / `-v` | flag | off | Enable debug logging |

### Subcommands

| Command | Description |
|---------|-------------|
| `sync` | Synchronise calendars (interactive wizard when called with no args) |
| `refresh` | Remove synced events then re-sync |
| `clear` | Remove managed events without re-syncing |
| `migrate [OLD [NEW]]` | Update calendar IDs in state DB (audit/interactive/direct modes) |
| `status` | Show config, resolved calendar names, and per-pair DB summary |
| `calendars` | List all configured EDS calendars |
| `inspect UID` | Inspect / debug events in a calendar |

### Options for `sync` / `refresh`

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--work-calendar UID` | `-w` | str | — | EDS source UID for work calendar |
| `--personal-calendar UID` | `-p` | str | — | EDS source UID for personal calendar |
| `--to-personal` | | flag | off | One-way: work → personal only |
| `--to-work` | | flag | off | One-way: personal → work only |
| `--dry-run` | `-n` | flag | off | Preview changes without writing |
| `--yes` | `-y` | flag | off | Skip confirmation prompt |
| `--keep-reminders` | | flag | off | Preserve VALARM reminders (stripped by default) |

`--to-personal` and `--to-work` are mutually exclusive. Calling `sync` with none of
`--work-calendar`, `--personal-calendar`, `--to-personal`, `--to-work` launches the
interactive wizard (see §6.6).

### Options for `clear`

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--work-calendar UID` | `-w` | str | — | EDS source UID for work calendar |
| `--personal-calendar UID` | `-p` | str | — | EDS source UID for personal calendar |
| `--personal` | | flag | off | Clear personal calendar only (work→personal events) |
| `--work` | | flag | off | Clear work calendar only (personal→work events) |
| `--dry-run` | `-n` | flag | off | Preview changes without writing |
| `--yes` | `-y` | flag | off | Skip confirmation prompt |

`--personal` and `--work` are mutually exclusive; omitting both clears both calendars.

### Options for `migrate`

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `OLD` (positional) | str | — | Calendar UID to replace (omit for audit mode) |
| `NEW` (positional) | str | — | Replacement UID (omit for interactive picker) |
| `--dry-run` | flag | off | Preview without writing |

### Options for `inspect`

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `CALENDAR_UID` (positional) | str | required | EDS source UID to inspect |
| `--title TEXT` | str | — | Filter events by SUMMARY substring |
| `--uid TEXT` | str | — | Filter events by UID substring |
| `--no-raw` | flag | off | Omit raw iCal output |
| `--exceptions-only` | flag | off | Show only RECURRENCE-ID exception events |
| `--masters-only` | flag | off | Show only master (non-exception) events |

### 7.1 Config File Format

```ini
[calendar-sync]
work_calendar_id = <EDS_SOURCE_UID>
personal_calendar_id = <EDS_SOURCE_UID>
```

CLI flags override config file values. The config file is silently skipped if it does not exist.


## 8. Output Format

Rich-formatted output via `rich` panels and tables. Sync results are displayed in a panel at the
end of every run:

```
╭──────── Results ────────╮
│  Added      N           │
│  Modified   N           │
│  Deleted    N           │
│  Errors     N  ✓        │
╰─────────────────────────╯
```

Log output (INFO/DEBUG from sync modules) is rendered via `rich.logging.RichHandler`.

Exit code is `0` on success, `1` if any errors occurred or on fatal failure, `130` on
`KeyboardInterrupt`.


## 9. Dependencies

| Library | GI Version | Purpose |
|---------|-----------|---------|
| `EDataServer` | 1.2 | Source registry, calendar source enumeration |
| `ECal` | 2.0 | Calendar client operations (connect, CRUD) |
| `ICalGLib` | 3.0 | iCalendar parsing and component manipulation |
| `GLib` | — | GLib error handling |

Python stdlib: `sqlite3`, `hashlib`, `uuid`, `configparser`, `logging`, `pathlib`.

Third-party: `typer` (CLI framework), `rich` (terminal output, logging handler).


## 10. Known Limitations and Design Decisions

- **EDS only**: The tool operates entirely through the local EDS cache. It does not communicate
  with Exchange or Google directly; those sync jobs are handled by GNOME Online Accounts / EDS
  backends.
- **EWS UID rewriting**: Microsoft 365 rewrites UIDs when events are created via EWS. The script
  always captures the server-assigned UID from `create_object_sync()` to stay in sync with what
  Exchange actually stores.
- **CATEGORIES instead of X-properties**: Microsoft 365 strips X-properties and COMMENT during
  EWS sync. The `CALENDAR-SYNC-MANAGED` marker is stored in the standard `CATEGORIES` property
  which survives the round-trip.
- **No bidirectional conflict resolution**: In bidirectional mode, the `origin` field is fixed at
  creation time. If both calendars' hashes change simultaneously, the authoritative calendar wins
  unconditionally; no three-way merge is attempted.
- **EDS sexp query**: Empty string `""` is an invalid sexp on Fedora 43+ EDS and causes 30-second
  timeouts. The correct "match all" sexp is `"#t"` (boolean true).
