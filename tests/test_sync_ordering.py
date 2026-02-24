"""
Integration tests: sync ordering consistency.

Verifies that running --only-to-work, --only-to-personal, and --both in any
order behaves correctly — specifically that a prior one-way run does not cause
the subsequent --both to double-sync events or miss events.

All tests use FakeCalendarClient (in-memory) + a real SQLite StateDatabase so
the actual sync functions run end-to-end without needing an EDS daemon.

Setup for every scenario:
    Work calendar  : W1, W2  (two original work events)
    Personal calendar: P1, P2  (two original personal events)

After a full bidirectional sync there should be:
    Work calendar  : W1, W2, W_m_P1, W_m_P2  (originals + managed mirrors of P1/P2)
    Personal calendar: P1, P2, P_m_W1, P_m_W2  (originals + managed mirrors of W1/W2)
"""

from eds_calendar_sync.models import SyncStats
from eds_calendar_sync.sync.to_personal import run_one_way_to_personal
from eds_calendar_sync.sync.to_work import run_one_way_to_work
from eds_calendar_sync.sync.two_way import run_two_way
from tests.conftest import make_vevent
from tests.fake_client import FakeCalendarClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _work_client() -> FakeCalendarClient:
    return FakeCalendarClient(
        {
            "W1": make_vevent("W1", "Work Meeting 1"),
            "W2": make_vevent("W2", "Work Meeting 2"),
        }
    )


def _personal_client() -> FakeCalendarClient:
    return FakeCalendarClient(
        {
            "P1": make_vevent("P1", "Personal Event 1"),
            "P2": make_vevent("P2", "Personal Event 2"),
        }
    )


def _run_to_work(config, logger, work_client, personal_client, state_db) -> SyncStats:
    stats = SyncStats()
    run_one_way_to_work(config, stats, logger, work_client, personal_client, state_db)
    return stats


def _run_to_personal(config, logger, work_client, personal_client, state_db) -> SyncStats:
    stats = SyncStats()
    run_one_way_to_personal(config, stats, logger, work_client, personal_client, state_db)
    return stats


def _run_both(config, logger, work_client, personal_client, state_db) -> SyncStats:
    stats = SyncStats()
    run_two_way(config, stats, logger, work_client, personal_client, state_db)
    return stats


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_to_work_then_both_only_syncs_personal_direction(state_db, sync_config, sync_logger):
    """
    --only-to-work followed by --both must:
      - recognise the P→W pairs in Phase 1 (no-op),
      - create personal mirrors of W1/W2 in Phase 2,
      - skip P1/P2 in Phase 3 (already in personal_uids_processed).

    Net result: --both creates exactly 2 events (personal mirrors of W1, W2)
    and does not touch the work calendar.
    """
    work_client = _work_client()
    personal_client = _personal_client()

    # Step 1: --only-to-work  (P1, P2 → work as managed "Busy" blocks)
    stats1 = _run_to_work(sync_config, sync_logger, work_client, personal_client, state_db)
    assert stats1.added == 2, f"Expected 2 creates from --only-to-work, got {stats1.added}"
    assert stats1.errors == 0

    # work now has W1, W2, W_m_P1, W_m_P2
    assert work_client.event_count == 4
    # personal still has only P1, P2
    assert personal_client.event_count == 2

    # Reset operation counters before the second run
    work_client.reset_counters()
    personal_client.reset_counters()

    # Step 2: --both
    stats2 = _run_both(sync_config, sync_logger, work_client, personal_client, state_db)

    assert stats2.added == 2, (
        f"--both after --only-to-work should create exactly 2 personal mirrors "
        f"(for W1 and W2), got {stats2.added}"
    )
    assert stats2.modified == 0
    assert stats2.deleted == 0
    assert stats2.errors == 0

    # personal should now have P1, P2 + mirrors of W1, W2
    assert personal_client.event_count == 4
    # work must be unchanged (W1, W2, W_m_P1, W_m_P2)
    assert work_client.event_count == 4
    assert len(work_client.creates) == 0, "Phase 3 should have skipped P1/P2 (already processed)"


def test_to_personal_then_both_only_syncs_work_direction(state_db, sync_config, sync_logger):
    """
    --only-to-personal followed by --both must:
      - recognise the W→P pairs in Phase 1 (no-op),
      - skip W1/W2 in Phase 2 (already in work_uids_processed),
      - create work mirrors of P1/P2 in Phase 3.

    Net result: --both creates exactly 2 events (work mirrors of P1, P2)
    and does not touch the personal calendar.
    """
    work_client = _work_client()
    personal_client = _personal_client()

    # Step 1: --only-to-personal  (W1, W2 → personal as managed copies)
    stats1 = _run_to_personal(sync_config, sync_logger, work_client, personal_client, state_db)
    assert stats1.added == 2, f"Expected 2 creates from --only-to-personal, got {stats1.added}"
    assert stats1.errors == 0

    # personal now has P1, P2, P_m_W1, P_m_W2
    assert personal_client.event_count == 4
    # work still has only W1, W2
    assert work_client.event_count == 2

    work_client.reset_counters()
    personal_client.reset_counters()

    # Step 2: --both
    stats2 = _run_both(sync_config, sync_logger, work_client, personal_client, state_db)

    assert stats2.added == 2, (
        f"--both after --only-to-personal should create exactly 2 work mirrors "
        f"(for P1 and P2), got {stats2.added}"
    )
    assert stats2.modified == 0
    assert stats2.deleted == 0
    assert stats2.errors == 0

    # work should now have W1, W2 + mirrors of P1, P2
    assert work_client.event_count == 4
    # personal must be unchanged (P1, P2, P_m_W1, P_m_W2)
    assert personal_client.event_count == 4
    assert len(personal_client.creates) == 0, (
        "Phase 2 should have skipped W1/W2 (already processed)"
    )


def test_both_twice_is_noop(state_db, sync_config, sync_logger):
    """
    Running --both twice: the second run must be a complete no-op.

    After the first run all four cross-pairs are recorded in the DB.
    The second run's Phase 1 finds matching hashes for every pair and
    marks all UIDs as processed; Phases 2 and 3 have nothing left to do.
    """
    work_client = _work_client()
    personal_client = _personal_client()

    # First --both run: creates all 4 cross-pairs
    stats1 = _run_both(sync_config, sync_logger, work_client, personal_client, state_db)
    assert stats1.added == 4, (
        f"First --both should create 4 mirrors (W1→P, W2→P, P1→W, P2→W), got {stats1.added}"
    )
    assert stats1.errors == 0

    work_client.reset_counters()
    personal_client.reset_counters()

    # Second --both run: must be a complete no-op
    stats2 = _run_both(sync_config, sync_logger, work_client, personal_client, state_db)

    assert stats2.added == 0, f"Second --both should add nothing, got {stats2.added}"
    assert stats2.modified == 0
    assert stats2.deleted == 0
    assert stats2.errors == 0

    # No calendar operations should have been issued
    assert len(work_client.creates) == 0
    assert len(work_client.modifies) == 0
    assert len(work_client.removes) == 0
    assert len(personal_client.creates) == 0
    assert len(personal_client.modifies) == 0
    assert len(personal_client.removes) == 0


def test_mixed_one_ways_then_both_is_noop(state_db, sync_config, sync_logger):
    """
    Running --only-to-personal then --only-to-work then --both:
    --both must be a no-op because all events are already synced in both directions.

    After the two one-way runs:
      DB holds 4 records: (W1,P_m_W1,source), (W2,P_m_W2,source),
                          (W_m_P1,P1,target), (W_m_P2,P2,target).
    --both Phase 1 processes all 4 pairs and marks all UIDs as processed.
    Phases 2 and 3 find no unprocessed, non-managed events.
    """
    work_client = _work_client()
    personal_client = _personal_client()

    # Step 1: --only-to-personal (W1, W2 → personal)
    stats1 = _run_to_personal(sync_config, sync_logger, work_client, personal_client, state_db)
    assert stats1.added == 2
    assert stats1.errors == 0

    # Step 2: --only-to-work (P1, P2 → work)
    # Note: P_m_W1 and P_m_W2 are managed → skipped by is_managed_event guard
    stats2 = _run_to_work(sync_config, sync_logger, work_client, personal_client, state_db)
    assert stats2.added == 2
    assert stats2.errors == 0

    # All four cross-calendar pairs are now synced
    assert work_client.event_count == 4  # W1, W2, W_m_P1, W_m_P2
    assert personal_client.event_count == 4  # P1, P2, P_m_W1, P_m_W2

    work_client.reset_counters()
    personal_client.reset_counters()

    # Step 3: --both must be a complete no-op
    stats3 = _run_both(sync_config, sync_logger, work_client, personal_client, state_db)

    assert stats3.added == 0, f"--both after both one-ways should add nothing, got {stats3.added}"
    assert stats3.modified == 0
    assert stats3.deleted == 0
    assert stats3.errors == 0

    assert len(work_client.creates) == 0
    assert len(personal_client.creates) == 0
