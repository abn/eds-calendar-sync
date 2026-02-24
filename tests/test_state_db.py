"""
Unit tests for StateDatabase — verify the three read paths used by the three
sync directions return the correct subset of records, and that upsert semantics
work correctly.
"""

from eds_calendar_sync.db import StateDatabase


class TestReadPaths:
    def test_bidirectional_sees_all_origins(self, state_db):
        """get_all_state_bidirectional() returns both origin='source' and origin='target' rows."""
        state_db.insert_bidirectional("W1", "P_m1", "hw1", "hpm1", "source")
        state_db.insert_bidirectional("W_m1", "P1", "hwm1", "hp1", "target")
        state_db.commit()

        rows = state_db.get_all_state_bidirectional()
        origins = {row["origin"] for row in rows}
        assert origins == {"source", "target"}
        assert len(rows) == 2

    def test_only_to_personal_sees_source_only(self, state_db):
        """get_all_state() (used by --only-to-personal) returns only origin='source' rows."""
        state_db.insert_bidirectional("W1", "P_m1", "hw1", "hpm1", "source")
        state_db.insert_bidirectional("W_m1", "P1", "hwm1", "hp1", "target")
        state_db.commit()

        result = state_db.get_all_state()
        assert set(result.keys()) == {"W1"}
        assert result["W1"]["target_uid"] == "P_m1"

    def test_only_to_work_sees_target_only(self, state_db):
        """get_all_state_by_target() (used by --only-to-work) returns only origin='target' rows."""
        state_db.insert_bidirectional("W1", "P_m1", "hw1", "hpm1", "source")
        state_db.insert_bidirectional("W_m1", "P1", "hwm1", "hp1", "target")
        state_db.commit()

        result = state_db.get_all_state_by_target()
        assert set(result.keys()) == {"P1"}
        assert result["P1"]["source_uid"] == "W_m1"


class TestUpsertSemantics:
    def test_upsert_on_conflict_updates_not_errors(self, state_db):
        """Inserting a duplicate source_uid upserts instead of raising UNIQUE error."""
        state_db.insert_bidirectional("W1", "P_m1_old", "old_hash", "old_hash", "source")
        state_db.commit()

        # Same source_uid (W1), different target_uid and hashes — should upsert silently
        state_db.insert_bidirectional("W1", "P_m1_new", "new_hash", "new_hash", "source")
        state_db.commit()

        rows = state_db.get_all_state_bidirectional()
        assert len(rows) == 1, "Expected exactly one row after upsert"
        assert rows[0]["target_uid"] == "P_m1_new"
        assert rows[0]["source_hash"] == "new_hash"

    def test_insert_preserves_created_at_on_conflict(self, state_db):
        """Upsert preserves the original created_at timestamp."""
        state_db.insert_bidirectional("W1", "P_m1", "h1", "h1", "source")
        state_db.commit()
        original_created_at = state_db.get_by_source_uid("W1")["created_at"]

        import time

        time.sleep(1.01)  # Ensure a different timestamp is possible

        state_db.insert_bidirectional("W1", "P_m1_v2", "h2", "h2", "source")
        state_db.commit()

        row = state_db.get_by_source_uid("W1")
        assert row["created_at"] == original_created_at, "created_at should be preserved on upsert"
        assert row["last_sync_at"] >= original_created_at


class TestCalendarPairScoping:
    def test_bidirectional_scoped_to_calendar_pair(self, state_db, db_path):
        """Records from a different calendar pair are invisible to the current pair."""
        state_db.insert_bidirectional("W1", "P_m1", "h1", "h2", "source")
        state_db.commit()

        with StateDatabase(db_path, "other-work", "other-personal") as other_db:
            rows = other_db.get_all_state_bidirectional()

        assert rows == [], "Different calendar pair must not see each other's records"

    def test_get_all_state_scoped(self, state_db, db_path):
        """get_all_state() is scoped to the current calendar pair."""
        state_db.insert_bidirectional("W1", "P_m1", "h1", "h2", "source")
        state_db.commit()

        with StateDatabase(db_path, "other-work", "other-personal") as other_db:
            result = other_db.get_all_state()

        assert result == {}


class TestDeletion:
    def test_delete_removes_row(self, state_db):
        """delete() removes the row for the given source_uid."""
        state_db.insert_bidirectional("W1", "P_m1", "h1", "h2", "source")
        state_db.commit()

        state_db.delete("W1")
        state_db.commit()

        rows = state_db.get_all_state_bidirectional()
        assert len(rows) == 0

    def test_delete_by_pair_removes_matching_row(self, state_db):
        """delete_by_pair() removes only the matching (source_uid, target_uid) row."""
        state_db.insert_bidirectional("W1", "P_m1", "h1", "h2", "source")
        state_db.insert_bidirectional("W2", "P_m2", "h3", "h4", "source")
        state_db.commit()

        state_db.delete_by_pair("W1", "P_m1")
        state_db.commit()

        rows = state_db.get_all_state_bidirectional()
        assert len(rows) == 1
        assert rows[0]["source_uid"] == "W2"
