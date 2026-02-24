# Calendar Knowledge Base

Accumulated knowledge about iCalendar standards, Evolution Data Server (EDS), Microsoft Exchange/M365, Google Calendar, and the quirks encountered while building this sync tool. This is a living reference — add to it when new issues are discovered.

---

## Table of Contents

1. [iCalendar (RFC 5545) Fundamentals](#1-icalendar-rfc-5545-fundamentals)
2. [Recurring Events and RRULE / EXDATE Mechanics](#2-recurring-events-and-rrule--exdate-mechanics)
3. [Microsoft Exchange / M365 Behaviour and Restrictions](#3-microsoft-exchange--m365-behaviour-and-restrictions)
4. [Google Calendar Behaviour](#4-google-calendar-behaviour)
5. [Evolution Data Server (EDS) Internals](#5-evolution-data-server-eds-internals)
6. [libical-glib Quirks](#6-libical-glib-quirks)
7. [Cross-Provider Sync Pitfalls](#7-cross-provider-sync-pitfalls)
8. [Property-Level Reference: What Survives Round-Trips](#8-property-level-reference-what-survives-round-trips)
9. [Change Detection and Hashing](#9-change-detection-and-hashing)
10. [Idempotency and Crash Safety](#10-idempotency-and-crash-safety)

---

## 1. iCalendar (RFC 5545) Fundamentals

### VCALENDAR vs VEVENT

- A full iCal document is a `VCALENDAR` component that wraps one or more `VEVENT`, `VTODO`, or `VJOURNAL` sub-components.
- EDS sometimes returns bare `VEVENT` strings (not wrapped in `VCALENDAR`), and sometimes returns full `VCALENDAR` wrappers. Code must handle both.
- When calling `ECal.Client.get_object_list_sync("#t", None)`, the returned objects may be native `ICalGLib.Component` objects or raw strings depending on the EDS version.

### UID

- Every event must have a globally unique `UID`. The same UID ties together a master recurring event and all its exception VEVENTs.
- `UID` is the primary key for most operations. Sync tools must track UID mappings because providers frequently rewrite UIDs on import.

### RECURRENCE-ID

- An exception occurrence of a recurring event carries `RECURRENCE-ID` set to the original scheduled start of the occurrence being overridden.
- The master VEVENT has no `RECURRENCE-ID`; exception VEVENTs do.
- Exception VEVENTs share the same `UID` as the master.
- When syncing to a target calendar that does not have the master series, `RECURRENCE-ID` must be stripped — otherwise the CalDAV/EWS backend rejects the create with "ExpandSeries can only be performed against a series".

### STATUS

- `STATUS:CANCELLED` means the event (or occurrence) was cancelled.
- `STATUS:TENTATIVE` means not yet confirmed.
- `STATUS:CONFIRMED` is the normal state.
- Exchange interprets `STATUS:CANCELLED` on a CreateItem call as a meeting cancellation response, causing `ErrorItemNotFound` because the referenced meeting does not exist in the target calendar.

### TRANSP

- `TRANSP:OPAQUE` (default, property may be absent) — event blocks time; user shows as "Busy".
- `TRANSP:TRANSPARENT` — event does not block time; user shows as "Free".
- Exchange automatically sets `TRANSP:TRANSPARENT` when you **accept** a meeting as "tentative" or decline it.
- A declined meeting is typically `TRANSP:TRANSPARENT` — it still appears in the calendar but you are not shown as busy. Such events should not be mirrored as busy blocks in a secondary calendar.

### CLASS (Event Privacy)

- `CLASS:PUBLIC` — visible to all (default if absent).
- `CLASS:PRIVATE` — Exchange/M365 shows the event as "Private Appointment" (hides title and details from other users). Google Calendar marks it as "Private" visibility.
- `CLASS:CONFIDENTIAL` — treated similarly to PRIVATE on most servers.
- Setting `CLASS:PRIVATE` on synced events prevents co-workers from seeing the mirrored personal events.

### METHOD (VCALENDAR-level)

- `METHOD` appears on the `VCALENDAR` wrapper, not on `VEVENT`.
- `METHOD:REQUEST` — this is a meeting invitation.
- `METHOD:CANCEL` — this is a cancellation of an existing meeting.
- `METHOD:REPLY` — this is a reply to a meeting invitation.
- When `METHOD` is present, Exchange treats a CalDAV/EWS create call as a meeting-flow operation and attempts to match the event to an existing meeting in the target calendar, failing with `ErrorItemNotFound` if no match exists. **Always strip `METHOD` before creating events in a target calendar.**

### VALARM

- `VALARM` sub-components attach reminders/alarms to an event.
- Syncing `VALARM` to a target calendar causes duplicate notifications (one from the source, one from the target).
- Should be stripped by default unless the user explicitly opts in (`--keep-reminders`).

### Volatile Properties (change noise)

Properties that servers routinely add/modify on every sync and should be excluded from content-based change detection:

| Property | Purpose |
|---|---|
| `DTSTAMP` | When the iCal object was generated/sent |
| `LAST-MODIFIED` | When the event was last modified |
| `CREATED` | When the event was first created |
| `SEQUENCE` | Revision counter incremented on each update |

These properties can differ between two otherwise identical events and must not drive change detection hashing.

---

## 2. Recurring Events and RRULE / EXDATE Mechanics

### RRULE Structure

- `RRULE` defines a recurrence pattern, e.g. `RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=20260601`.
- Key fields: `FREQ`, `INTERVAL`, `BYDAY`, `COUNT`, `UNTIL`, `BYMONTHDAY`, etc.
- `UNTIL` specifies the last date (inclusive) the series can produce occurrences.
- `COUNT` specifies the total number of occurrences.

### EXDATE

- `EXDATE` excludes specific dates from a recurring series.
- One `EXDATE` property can carry one or more comma-separated dates.
- There can be multiple `EXDATE` properties on one VEVENT.
- Formats: `EXDATE;VALUE=DATE:20260216` (date only) or `EXDATE;TZID=Europe/Berlin:20260216T110000` (datetime with timezone).

### Declined Recurring Instances (Exchange-specific)

Exchange represents a declined recurring instance with **two** iCal artefacts:

1. **Master VEVENT**: The declined occurrence date is added to the master's `EXDATE` list.
2. **Exception VEVENT**: A separate VEVENT with the same `UID` and `RECURRENCE-ID` pointing to the declined date. The `SUMMARY` is often prefixed with `"Declined: "`. **Exchange does NOT set `TRANSP:TRANSPARENT` on these exception VEVENTs**, making them indistinguishable from legitimate rescheduled occurrences by TRANSP alone.

**Detection algorithm**: A declined instance exception VEVENT can be identified when:
- It has a `RECURRENCE-ID` whose date appears in the master VEVENT's `EXDATE` set, AND
- Its `DTSTART` falls on the same date as its `RECURRENCE-ID` (ruling out genuinely rescheduled occurrences, which have a different `DTSTART`).

**Impact**: Such exception VEVENTs should be skipped during sync; the master VEVENT (with its updated `EXDATE`) is synced normally and already excludes the declined occurrence.

### DTSTART Excluded by EXDATE

Exchange may still render a recurring series' first occurrence even when `DTSTART` itself is in the `EXDATE` list. The reliable fix is to advance `DTSTART` (and `DTEND` by the same duration) to the first non-excluded occurrence before sending the event to Exchange.

### Empty Recurring Series

A recurring series where **every** occurrence produced by `RRULE` is listed in `EXDATE` (i.e. the series expands to zero valid instances) is invalid from Exchange's perspective. Exchange rejects creating such a series with `ErrorItemNotFound`. These events must be detected and skipped before any create attempt.

### EXDATE Format Mismatch (Exchange)

Exchange ignores `EXDATE;VALUE=DATE:YYYYMMDD` (date-only format) when `DTSTART` carries a `TZID`. The two formats don't match in Exchange's internal comparison and the excluded occurrences still appear. **Fix**: normalise all date-only `EXDATE` properties to datetime-with-TZID format matching `DTSTART`, e.g. `EXDATE;TZID=Europe/Berlin:20260216T110000`.

---

## 3. Microsoft Exchange / M365 Behaviour and Restrictions

### UID Rewriting

Microsoft 365 / Exchange Online **rewrites the UID** of every event on import. The UID you send is not the UID you get back. This breaks any sync tool that relies on UID stability for tracking. **Mitigation**: Always fetch the event back after creating it to obtain the server-assigned UID and store that in the state database.

### CATEGORIES Property

- M365 preserves `CATEGORIES` through round-trips.
- M365 **strips** `X-*` extension properties and `COMMENT` fields.
- This makes `CATEGORIES` the only reliable mechanism to embed custom metadata (e.g. a managed-event marker) that survives Exchange round-trips.

### X-Property Rejection (ErrorItemNotFound on create)

Exchange embeds many vendor-specific `X-*` properties in events:
- `X-MS-OLK-*` — Outlook-specific extensions
- `X-MICROSOFT-CDO-*` — CDO (Collaboration Data Objects) extensions
- `X-MS-EXCHANGE-ORGANIZATION-*` — Exchange organisation metadata

These properties reference internal Exchange objects in the **source tenant**. When you try to create an event containing them in a **different Exchange tenant** (or against a different calendar backend), Exchange attempts to resolve those references and fails with `ErrorItemNotFound`. **Always strip all `X-*` properties before creating in a target calendar.**

### ErrorItemNotFound on Create — Root Causes

| Cause | Explanation | Fix |
|---|---|---|
| `STATUS:CANCELLED` present | Exchange treats this as a meeting cancellation response, tries to match an existing meeting, finds none | Strip `STATUS` before create; skip `CANCELLED` events entirely |
| All RRULE occurrences covered by `EXDATE` | Exchange rejects creating a series with zero valid instances | Detect and skip empty series before create |
| `RECURRENCE-ID` present without master series | Exchange tries to expand the series in the target calendar, which doesn't exist | Strip `RECURRENCE-ID` to make the exception a standalone event |
| Vendor `X-*` properties referencing source-tenant objects | Exchange tries to resolve cross-tenant references | Strip all `X-*` properties |
| `METHOD:CANCEL` or `METHOD:REQUEST` in `VCALENDAR` | Exchange treats the create as a meeting-flow operation | Strip `METHOD` from the `VCALENDAR` wrapper |
| DTSTART has `TZID` but RRULE `UNTIL` is date-only | libical's `RecurIterator` may emit spurious post-UNTIL occurrences, slipping past the empty-series check | Parse `UNTIL` from raw iCal string and cap the iterator manually |

### ExpandSeries Error on Create

`ExpandSeries can only be performed against a series` — returned when an exception VEVENT (containing `RECURRENCE-ID`) is created in a target calendar that does not have the master recurring series. Fix: strip `RECURRENCE-ID` before creating in the target calendar.

### Private Appointments

`CLASS:PRIVATE` is respected by Exchange/M365: the event shows as "Private Appointment" to other users with calendar read access. They see there is a blocked slot but not the title or details. This is the recommended way to prevent mirrored events from leaking titles.

### GOA Reconnection — UID Changes

When a GNOME Online Accounts (GOA) connection for an Exchange/Google account is removed and re-added, EDS assigns **new calendar UIDs** to the sources. The state database contains the old UIDs which no longer resolve. The `migrate` subcommand handles this: it finds all UIDs in the DB that no longer exist in EDS and prompts for their replacements.

---

## 4. Google Calendar Behaviour

### CalDAV Compliance

Google Calendar exposes a CalDAV interface. It is generally more standards-compliant than Exchange EWS.

### CLASS:PRIVATE

Google Calendar honours `CLASS:PRIVATE` by marking the event as "Private" visibility — other users with calendar read access see a placeholder ("busy") but not the event details.

### ORGANIZER Restriction

CalDAV services (including Google) reject creating events where the `ORGANIZER` field contains someone other than the authenticated user. This causes phantom calendar invitations to be sent to the listed organizer. **Always strip `ORGANIZER` (and `ATTENDEE`) before creating in a target calendar.**

### CATEGORIES

Google Calendar preserves `CATEGORIES` through CalDAV round-trips, making it a reliable metadata carrier alongside Exchange.

---

## 5. Evolution Data Server (EDS) Internals

### Architecture

EDS is a GNOME system daemon (`evolution-data-server`) that provides a local cache and unified API for multiple calendar and addressbook backends (Exchange EWS, Google CalDAV, local files, etc.). Applications talk to EDS via D-Bus; EDS handles the protocol-specific syncing to the remote provider in the background.

### Source Registry

- `EDataServer.SourceRegistry` is the central registry of all configured calendar/addressbook sources.
- Each source has a **UID** (a hash string like `d19280dcbb91f8ebcdbbb2adb7d502bc1d866fda`) and a **display name**.
- Sources are organised in parent/child hierarchies: a GOA account is the parent; individual calendars (main calendar, contacts, etc.) are children.
- When a GOA account is removed and re-added, new parent and child sources are created with new UIDs.

### ECal.Client

- `ECal.Client.connect_sync(source, ECal.ClientSourceType.EVENTS, timeout, None)` opens a synchronous connection to a calendar.
- `get_object_list_sync("#t", None)` fetches all events. `"#t"` (boolean true) is the correct s-expression for "all events". An empty string `""` is invalid on newer EDS versions and causes the calendar factory to block for the full sync timeout.
- `create_object_sync(component, ECal.OperationFlags.NONE, None)` creates a new event. Returns `(success, out_uid)` — `out_uid` is the server-assigned UID (which may differ from the UID in the component, especially with M365).
- `modify_object_sync(component, ECal.ObjModType.THIS, ECal.OperationFlags.NONE, None)` modifies an existing event. `ObjModType.THIS` modifies only the specified occurrence (for recurring events); use `ALL` to modify the master.
- `remove_object_sync(uid, rid, ECal.ObjModType.THIS, ECal.OperationFlags.NONE, None)` removes an event. `rid=None` for non-recurring events.

### Error Domains

EDS uses GLib error domains:

| Domain | Description |
|---|---|
| `e-cal-client-error-quark` | Generic EDS calendar client errors. Code `1` = object not found. |
| `e-m365-error-quark` | Exchange M365 backend errors. The Exchange error name (e.g. `ErrorItemNotFound`) is embedded in the `message` string, not the code. |

### Calendar Writability

Some EDS calendars are read-only (e.g. "Birthdays & Anniversaries"). Attempting to create/modify/delete in a read-only calendar raises a GLib error. Check writability before attempting writes.

### SQLite + systemd Sandbox

When running under systemd with `ProtectHome=read-only`, the `ReadWritePaths` directive must cover the **directory** containing the SQLite database, not just the database file itself. SQLite creates auxiliary journal/WAL files (`-journal`, `-wal`, `-shm`) in the same directory as the main file during write commits. If only the file is writable (not its parent directory), reads succeed but writes fail mid-transaction with "unable to open database file".

```ini
# Wrong — journal files cannot be created
ReadWritePaths=%h/.local/share/eds-calendar-sync-state.db

# Correct — parent directory gives SQLite room to create auxiliary files
ReadWritePaths=%h/.local/share %h/.cache/evolution
```

---

## 6. libical-glib Quirks

libical-glib is the Python-accessible GObject wrapper around libical. It has several silent failure modes discovered through debugging.

### `Property.get_exdate()` Returns null_time for DATE-only EXDATEs

When an `EXDATE` property uses the `VALUE=DATE` form (e.g. `EXDATE;VALUE=DATE:20260216`), `prop.get_exdate()` may return a `null_time` value (not `None`) in some libical builds instead of the actual date. This silently defeats any EXDATE-based logic.

**Detection**: Check `t.is_null_time()` — a truthful `None` check is not sufficient.

**Workaround**: When `get_exdate()` returns null_time (or the collected exdate set is empty despite EXDATE properties being present), fall back to parsing the EXDATE lines from the raw iCal string via regex:

```python
_EXDATE_DATE_RE = re.compile(r"^EXDATE[^:\n]*:(\d{8})", re.MULTILINE)
for m in _EXDATE_DATE_RE.finditer(ical_string):
    exdates.add(m.group(1))  # YYYYMMDD
```

### `component.as_ical_string()` on a Child VEVENT May Fail

Calling `as_ical_string()` on a child `VEVENT` component obtained via `get_first_component()` can raise or return an empty string in some libical-glib builds. This silently breaks any logic that depends on parsing the child's string representation (e.g. EXDATE regex fallback, RRULE UNTIL extraction).

**Workaround**: Always call `as_ical_string()` on the **root component** (the `VCALENDAR` wrapper, or the component passed in at the top level) rather than on a child obtained via `get_first_component()`.

```python
# BAD — child as_ical_string() may silently return "" in some builds
child_vevent = comp.get_first_component(ICalGLib.ComponentKind.VEVENT_COMPONENT)
ical_str = child_vevent.as_ical_string()  # may be empty!

# GOOD — use the root component
ical_str = comp.as_ical_string()  # comp is VCALENDAR or top-level VEVENT
```

### `RecurIterator.new(rule, dtstart)` Fails with TZID not in libical DB

If `dtstart` carries a `TZID` for a timezone that is not in libical's built-in timezone database, `ICalGLib.RecurIterator.new(rule, dtstart)` may raise an exception. This silently converts a "does this series have valid occurrences?" check into an incorrect "yes, assume valid" fallback.

**Workaround**: Create a **floating** (timezone-free) copy of `dtstart` for the iterator. Since we only compare `YYYYMMDD` strings, timezone precision is not needed:

```python
dtstart_for_iter = ICalGLib.Time.new_from_string(
    f"{y:04d}{mo:02d}{d:02d}T{h:02d}{mi:02d}{s:02d}"
    # No TZID suffix → floating time
)
it = ICalGLib.RecurIterator.new(rule, dtstart_for_iter)
```

### RRULE UNTIL with TZID DTSTART — Iterator Does Not Stop

When `DTSTART` carries a `TZID` (datetime) but the `RRULE`'s `UNTIL` is a **date-only** value (e.g. `UNTIL=20260316`), the `RecurIterator` may not reliably stop at the `UNTIL` boundary — it can emit spurious occurrences past the series end date.

**Workaround**: Extract `UNTIL` from the raw iCal string via regex (using the root component string, as above) and cap the iteration loop manually:

```python
_RRULE_UNTIL_RE = re.compile(r"UNTIL=(\d{8})")
m = _RRULE_UNTIL_RE.search(comp.as_ical_string() or "")
until_str = m.group(1) if m else None

for _ in range(500):
    occ = it.next()
    if occ is None or occ.is_null_time():
        break
    occ_key = f"{occ.get_year():04d}{occ.get_month():02d}{occ.get_day():02d}"
    if until_str and occ_key > until_str:
        break  # Past UNTIL — stop here
```

### `rule.get_until()` May Silently Fail

The `ICalGLib.Recurrence.get_until()` accessor can silently fail (return null or incorrect data) for `UNTIL` values that are date-only strings in some libical-glib builds. Always prefer parsing `UNTIL` from the raw iCal string.

### Iteration Safety Cap

Always cap `RecurIterator` loops at a fixed maximum (e.g. 500 iterations). An infinite or very large recurrence series could otherwise loop indefinitely. 500 iterations is sufficient to detect whether any occurrence escapes the `EXDATE` set; for UNTIL-bounded series, the UNTIL check fires first anyway.

---

## 7. Cross-Provider Sync Pitfalls

### UID Rewriting Breaks Naive Sync

Microsoft 365 rewrites every event's UID on import. A sync tool that uses UID equality to detect existing events on the target side will re-create duplicates on every sync run. The solution is a local state database keyed on the source UID, storing the server-assigned target UID returned at creation time.

### Loop Prevention

When syncing bidirectionally, the tool must distinguish events it created from events that exist natively. Without this guard, a work event would be mirrored to personal, and the personal copy would then be mirrored back to work, creating an infinite loop of duplicates.

**Implementation**: Tag all created events with a recognisable `CATEGORIES` value (e.g. `CALENDAR-SYNC-MANAGED`). Check for this tag before processing any event in the sync loop — managed events are always skipped in both directions.

X-properties cannot be used for tagging because Exchange strips them on round-trip. `COMMENT` is also stripped. `CATEGORIES` survives Exchange and Google CalDAV round-trips and is the reliable choice.

### Source Fingerprint for Orphan Recovery

After a crash between creating an event and committing the state DB record, the next run would see an untracked managed event in the target calendar. Without a link back to the source event, the tool would create a second duplicate.

**Implementation**: Embed a `CATEGORIES:CALENDAR-SYNC-SRC-<fingerprint>` property on every created event, where `<fingerprint>` is the first 16 hex characters of SHA-256(`source_uid`). On startup, scan for managed events lacking a DB record (orphan scan) and build a `fingerprint → target_uid` index. When a create would be attempted, check the index first; if a match exists, register the existing event instead of creating a new one.

### Hash-Based Change Detection (Not UID-Based)

Since UIDs change on import, change detection must be based on a content hash of the event, not on UID comparison. The hash is computed after stripping volatile properties (DTSTAMP, LAST-MODIFIED, CREATED, SEQUENCE) to avoid false positives from server-side noise.

**Separate hashes for source and target**: The source event (work) and the target event (personal, which has been sanitized/transformed) have different content. Two separate hashes must be stored: one for the source event as fetched, one for the target event as fetched back after creation. This is needed to detect:
- Source changes: work event changed → update personal mirror
- Target tampering: personal mirror was manually edited → revert to authoritative content

### Compound UID for Exception VEVENTs (One-way Sync)

In one-way sync (work → personal), both a master VEVENT and its exception VEVENTs have the same `UID` but represent distinct events. The state DB has a `UNIQUE` constraint on `source_uid`. Storing both under the same UID would cause a constraint violation.

**Solution**: Build a compound state key: `{base_uid}::RID::{recurrence_id_string}` for exception VEVENTs, and plain `{base_uid}` for master VEVENTs.

### ORGANIZER and ATTENDEE Must Be Stripped

Leaving `ORGANIZER` set to someone else when creating via CalDAV causes the server to send meeting invitation emails. Leaving `ATTENDEE` intact may cause phantom responses. Always strip both before creating in any target calendar.

### VALARM Duplication

If a work event has a reminder and you copy it to the personal calendar with `VALARM` intact, the user gets notified twice: once from the work calendar and once from the personal calendar. Strip `VALARM` by default; provide an opt-in flag if the user wants reminders on the synced copy.

---

## 8. Property-Level Reference: What Survives Round-Trips

### Microsoft Exchange / M365 (EWS via EDS e-m365 backend)

| Property | Survives M365 round-trip? | Notes |
|---|---|---|
| `SUMMARY` | Yes | |
| `DTSTART`, `DTEND` | Yes | |
| `RRULE`, `EXDATE`, `RDATE` | Yes | |
| `CATEGORIES` | Yes | Reliable metadata carrier; multiple `CATEGORIES` props allowed |
| `CLASS` | Yes | PRIVATE → "Private Appointment" |
| `UID` | No — rewritten | Server rewrites on import |
| `DESCRIPTION` | Yes | |
| `LOCATION` | Yes | |
| `ORGANIZER` | Partially | May trigger meeting-invite flow |
| `ATTENDEE` | Partially | May trigger invite/response emails |
| `X-*` properties | No — stripped | All vendor extension props removed |
| `COMMENT` | No — stripped | |
| `VALARM` sub-components | Partially | Preserved but may behave differently |
| `METHOD` (VCALENDAR) | Triggers EWS meeting flow | Must be stripped before create |
| `STATUS:CANCELLED` | Triggers cancellation flow | Must skip/strip; causes `ErrorItemNotFound` on create |
| `RECURRENCE-ID` | Triggers series expansion | Strip to make standalone event |
| `SEQUENCE` | Volatile | Ignored for change detection |
| `DTSTAMP`, `LAST-MODIFIED`, `CREATED` | Volatile | Server overwrites; ignore for change detection |

### Google Calendar (CalDAV)

| Property | Survives Google round-trip? | Notes |
|---|---|---|
| `SUMMARY` | Yes | |
| `DTSTART`, `DTEND` | Yes | |
| `RRULE`, `EXDATE` | Yes | |
| `CATEGORIES` | Yes | |
| `CLASS` | Yes | PRIVATE → "Private" visibility |
| `UID` | Generally preserved | CalDAV spec; Google usually keeps it |
| `DESCRIPTION` | Yes | |
| `LOCATION` | Yes | |
| `ORGANIZER` | May trigger invite | Strip to avoid phantom invites |
| `ATTENDEE` | May trigger invite | Strip |
| `VALARM` | Yes | |
| `X-*` properties | Mostly stripped | |

---

## 9. Change Detection and Hashing

### Algorithm

1. Fetch the event iCal string from EDS.
2. Parse it with `ICalGLib.Component.new_from_string()`.
3. Remove all volatile properties (`DTSTAMP`, `LAST-MODIFIED`, `CREATED`, `SEQUENCE`) from every `VEVENT` in the component.
4. Serialize back with `comp.as_ical_string()`.
5. Compute SHA-256 of the UTF-8 bytes.

### Two-Hash Model (Bidirectional Sync)

For each sync pair, store:
- `source_hash`: SHA-256 of the work event as fetched from EDS (volatile props stripped)
- `target_hash`: SHA-256 of the personal event as fetched back after create/modify (volatile props stripped)

On each sync cycle:
- If `current_source_hash != stored_source_hash`: source changed → push update to target
- If `current_target_hash != stored_target_hash`: target was manually edited → revert to source

The source calendar is always authoritative; manual edits to the target are overwritten.

### Fetching Back After Create/Modify

The target calendar (especially M365) may transform the event during create/modify (adding server-specific properties, rewriting fields). To get the correct `target_hash`, always fetch the event back from EDS immediately after a successful create/modify:

```python
actual_uid = client.create_event(sanitized)   # may rewrite UID
created = client.get_event(actual_uid)
target_hash = compute_hash(created.as_ical_string())
```

This ensures the stored hash matches what the server actually stored, preventing false "modified" detections on the next sync cycle.

---

## 10. Idempotency and Crash Safety

### The Problem

A sync run may crash (or be killed) after creating an event in the target calendar but before committing the state DB record. On the next run, the tool sees no DB record for the source event and tries to create a new target event — resulting in a duplicate.

### Source Fingerprint Index

**On every new create**, embed `CATEGORIES:CALENDAR-SYNC-SRC-<fingerprint>` in the target event (where `fingerprint = sha256(source_uid)[:16]`).

**At startup**, perform an orphan scan:
1. Fetch all events from the target calendar.
2. For each managed event (has `CATEGORIES:CALENDAR-SYNC-MANAGED`) that also has a `CALENDAR-SYNC-SRC-*` category and has no state DB record, add it to the orphan index: `fingerprint → target_uid`.

**Before creating**, check the orphan index:
- If `sha256(source_uid)[:16]` is in the index, an orphaned target event already exists.
- Register it in the state DB instead of creating a new one.

### Upsert Semantics for State DB Inserts

The `INSERT ... ON CONFLICT DO UPDATE` (upsert) pattern means that even if a DB record already exists from a previous partial run, the insert succeeds by updating the existing record. This prevents `UNIQUE` constraint errors on retry after a crash.

### Per-Event Commits

Commit the state DB after each successful create/modify/delete, not once at the end of the sync run. This ensures that any crash leaves the DB in a consistent state for all events processed so far, minimizing the number of orphans that need recovery on the next run.

---

## Appendix A: iCal Property Quick Reference

| Property | RFC Location | Notes |
|---|---|---|
| `BEGIN:VCALENDAR` / `END:VCALENDAR` | RFC 5545 §3.6 | Outer wrapper |
| `BEGIN:VEVENT` / `END:VEVENT` | RFC 5545 §3.6.1 | Event component |
| `UID` | RFC 5545 §3.8.4.7 | Globally unique identifier |
| `SUMMARY` | RFC 5545 §3.8.1.12 | Event title |
| `DTSTART` | RFC 5545 §3.8.2.4 | Start date/time |
| `DTEND` | RFC 5545 §3.8.2.2 | End date/time |
| `DURATION` | RFC 5545 §3.8.2.5 | Duration (alternative to DTEND) |
| `RRULE` | RFC 5545 §3.8.5.3 | Recurrence rule |
| `EXDATE` | RFC 5545 §3.8.5.1 | Excluded recurrence dates |
| `RDATE` | RFC 5545 §3.8.5.2 | Additional recurrence dates |
| `RECURRENCE-ID` | RFC 5545 §3.8.4.4 | Identifies overridden occurrence |
| `STATUS` | RFC 5545 §3.8.1.11 | TENTATIVE / CONFIRMED / CANCELLED |
| `TRANSP` | RFC 5545 §3.8.2.7 | OPAQUE (busy) or TRANSPARENT (free) |
| `CLASS` | RFC 5545 §3.8.1.3 | PUBLIC / PRIVATE / CONFIDENTIAL |
| `CATEGORIES` | RFC 5545 §3.8.1.2 | Freeform category strings |
| `DESCRIPTION` | RFC 5545 §3.8.1.5 | Event notes/body |
| `LOCATION` | RFC 5545 §3.8.1.7 | Meeting location |
| `ORGANIZER` | RFC 5545 §3.8.4.3 | Meeting organizer |
| `ATTENDEE` | RFC 5545 §3.8.4.1 | Attendee list |
| `ATTACH` | RFC 5545 §3.8.1.1 | Attachments |
| `URL` | RFC 5545 §3.8.4.6 | Related URL |
| `VALARM` | RFC 5545 §3.6.6 | Alarm/reminder sub-component |
| `METHOD` | RFC 5546 §2.1 | iTIP method (VCALENDAR-level) |
| `DTSTAMP` | RFC 5545 §3.8.7.2 | When the iCal object was created |
| `LAST-MODIFIED` | RFC 5545 §3.8.7.3 | When the event was last modified |
| `CREATED` | RFC 5545 §3.8.7.1 | When the event was first created |
| `SEQUENCE` | RFC 5545 §3.8.7.4 | Revision counter |

---

## Appendix B: Known Error Messages

| Error String | Source | Meaning and Fix |
|---|---|---|
| `e-m365-error-quark: Cannot create calendar object: ErrorItemNotFound (2)` | Exchange M365 EDS backend | See §3 for causes. Most common: STATUS:CANCELLED, empty recurring series, X-properties, METHOD present. |
| `ExpandSeries can only be performed against a series. (400)` | Exchange EWS | RECURRENCE-ID present without master series. Strip RECURRENCE-ID. |
| `e-cal-client-error-quark` code 1 | EDS generic | Object not found (event was externally deleted). Handle silently. |
| `unable to open database file` | SQLite via EDS systemd service | ReadWritePaths covers file but not parent dir. Fix: point ReadWritePaths at parent directory. |
| `Read-only calendars can't be modified` | EDS | Calendar is read-only (e.g. Birthdays). Check writability before sync. |

---

*Last updated: 2026-02-24*
