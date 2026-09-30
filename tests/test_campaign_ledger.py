"""The campaign ledger's schema: the rules that hold whatever code writes to it.

Each test writes around the Ledger class where it can, with plain SQL, because a CHECK or a trigger is
only worth having if it refuses the row the class would never have written.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes as fakes  # noqa: E402
from campaign import ledger  # noqa: E402

NOW = "2026-10-01T00:00:00+00:00"


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.world = fakes.World(Path(self.directory.name), ["u1", "u2"])
        self.book = self.world.open()

    def tearDown(self) -> None:
        self.book.close()
        self.directory.cleanup()

    def sql(self, statement: str, *values: object) -> None:
        self.book.connection.execute(statement, values)

    def test_opening_twice_keeps_the_schema_and_the_rows(self) -> None:
        self.book.close()
        with ledger.Ledger(self.world.ledger_path) as again:
            self.assertEqual(len(again.units()), 2)
            self.assertEqual(again.campaign()["campaign_id"], "test-campaign")
        self.book = self.world.open()

    def test_one_campaign_per_ledger_and_it_is_fixed(self) -> None:
        with self.assertRaisesRegex(sqlite3.IntegrityError, "one campaign per ledger"):
            self.sql(
                "INSERT INTO campaign VALUES ('other', ?, 'declared', 'm', ?, 'p', 'w', 'keep', 'c', 'a', ?, '{}', '{}', '{}')",
                NOW, self.world.digest, "0" * 64,
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "fixed"):
            self.sql("UPDATE campaign SET analysis_purpose = 'something else'")

    def test_an_approval_is_of_this_manifest_and_never_covers_2_or_6(self) -> None:
        with self.assertRaisesRegex(sqlite3.IntegrityError, "manifest digest"):
            self.sql(
                "INSERT INTO approval(approval_id, campaign_id, manifest_digest, approved_by, approved_at, statement, "
                "covers_json, recorded_at) VALUES ('x', 'test-campaign', ?, 'p', ?, 's', '[\"1\"]', ?)",
                "sha256:" + "1" * 64, NOW, NOW,
            )
        for boundary in ("2", "6"):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "never covered"):
                self.sql(
                    "INSERT INTO approval(approval_id, campaign_id, manifest_digest, approved_by, approved_at, statement, "
                    "covers_json, recorded_at) VALUES (?, 'test-campaign', ?, 'p', ?, 's', ?, ?)",
                    f"a{boundary}", self.world.digest, NOW, f'["1", "{boundary}"]', NOW,
                )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "never deleted"):
            self.sql("DELETE FROM approval")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "only be revoked"):
            self.sql("UPDATE approval SET covers_json = '[\"1\", \"3\", \"4\", \"5\", \"split\", \"2\"]'")

    def test_a_boundary_crossing_names_a_live_covering_approval(self) -> None:
        # The triggers read the approval before the CHECK sees the row; any of the three refuses it.
        with self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK|live approval|does not cover"):
            self.sql("INSERT INTO transition(unit_key, at, to_state, boundary) VALUES ('u1', ?, 'class_settled', '3')", NOW)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "does not cover"):
            self.book.close()
            other = fakes.World(Path(self.directory.name) / "b", ["u1"], covers=("1", "3"))
            self.book = other.open()
            self.sql(
                "INSERT INTO transition(unit_key, at, to_state, boundary, approval_id) VALUES ('u1', ?, 'running', '4', 'approval-1')",
                NOW,
            )

    def test_a_revoked_approval_refuses_the_next_crossing(self) -> None:
        self.book.transition("u1", "class_settled", NOW, boundary="3")
        self.book.revoke("approval-1", "Test Person", "stop", NOW)
        with self.assertRaises(ledger.ApprovalMissing):
            self.book.transition("u1", "handoff_ready", NOW)  # no boundary: allowed
            self.book.transition("u1", "downloading", NOW, boundary="1")
        self.assertEqual(self.book.unit("u1")["state"], "handoff_ready")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "live approval"):
            self.sql(
                "INSERT INTO transition(unit_key, at, to_state, boundary, approval_id) VALUES ('u1', ?, 'downloading', '1', 'approval-1')",
                NOW,
            )

    def test_transitions_and_closed_attempts_are_append_only(self) -> None:
        self.book.transition("u1", "class_settled", NOW, boundary="3")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.sql("UPDATE transition SET to_state = 'done'")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.sql("DELETE FROM transition")
        attempt = self.book.open_attempt("u1", "handoff", NOW)
        self.book.close_attempt(attempt, "failed", NOW, counted=True)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "final"):
            self.sql("UPDATE attempt SET outcome = 'ok' WHERE attempt_id = ?", attempt)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "never deleted"):
            self.sql("DELETE FROM attempt")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK"):
            self.sql("INSERT INTO attempt(unit_key, step, started_at, ended_at, outcome, counted) VALUES ('u1', 's', ?, ?, 'ok', 1)", NOW, NOW)

    def test_one_console_at_a_time(self) -> None:
        first = self.book.open_console_run("u1", "diagnostic", "0" * 64, 0, 3600, NOW)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "one Console at a time"):
            self.book.open_console_run("u2", "diagnostic", "0" * 64, 0, 3600, NOW)
        self.assertEqual(len(self.book.console_runs()), 1, "the refused run left no row behind")
        self.book.end_console_run(first, "completed", NOW)
        self.book.open_console_run("u2", "diagnostic", "0" * 64, 0, 3600, NOW)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "singleton"):
            self.sql("DELETE FROM console_slot")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK|UNIQUE"):
            self.sql("INSERT INTO console_slot(id) VALUES (2)")

    def test_state_invariants(self) -> None:
        cases = [
            ("UPDATE unit SET state = 'done' WHERE unit_key = 'u1'", "a terminal state carries its reason"),
            ("UPDATE unit SET state = 'waiting_retry', resume_state = 'pending' WHERE unit_key = 'u1'", "a retry says when"),
            ("UPDATE unit SET state = 'queued' WHERE unit_key = 'u1'", "a waiting state says what it resumes"),
            ("UPDATE unit SET state = 'discarding' WHERE unit_key = 'u1'", "a discard says what the unit ends as"),
            ("UPDATE unit SET state = 'no_such_state' WHERE unit_key = 'u1'", "states are listed"),
            ("UPDATE unit SET outputs_produced = 1, raw_disposition = 'discarded' WHERE unit_key = 'u1'",
             "validated outputs are never discarded"),
            ("UPDATE unit SET role = 'split_part' WHERE unit_key = 'u1'", "a part names its parent"),
            ("UPDATE unit SET threshold_step = 250 WHERE unit_key = 'u1'", "the step is 100 or 1000"),
            ("UPDATE unit SET gate_exit_final = 1 WHERE unit_key = 'u1'", "the gate exits 0, 2, 3 or 4"),
        ]
        for statement, why in cases:
            with self.subTest(why), self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK"):
                self.sql(statement)

    def test_a_stale_transition_is_refused_and_writes_nothing(self) -> None:
        with self.assertRaises(ledger.LedgerError):
            self.book.transition("u1", "downloading", NOW, expect_from="handoff_ready", boundary="1")
        self.assertEqual(self.book.unit("u1")["state"], "pending")
        self.assertEqual(len(self.book.transitions("u1")), 1)

    def test_a_transition_and_its_records_commit_together(self) -> None:
        attempt = self.book.open_attempt("u1", "class", NOW)

        def crash(phase: str) -> None:
            if phase == "before":
                raise fakes.Crash()

        self.book.commit_hook = crash
        with self.assertRaises(fakes.Crash):
            self.book.transition("u1", "class_settled", NOW, boundary="3", close_attempt=(attempt, "ok", False, {}),
                                 new_attempt={"step": "x", "outcome": "ok", "counted": False})
        self.book.commit_hook = None
        self.assertEqual(self.book.unit("u1")["state"], "pending")
        self.assertEqual([row["attempt_id"] for row in self.book.open_attempts("u1")], [attempt])
        self.assertEqual(len(self.book.attempts("u1")), 1)

    def test_the_lock_is_taken_over_only_from_a_dead_or_silent_holder(self) -> None:
        taken, _ = self.book.take_lock(100, 1.0, "host", NOW, holder_alive=lambda record: True)
        self.assertTrue(taken)
        taken, holder = self.book.take_lock(200, 2.0, "host", NOW, holder_alive=lambda record: True)
        self.assertFalse(taken)
        self.assertEqual(holder["pid"], 100)
        taken, holder = self.book.take_lock(200, 2.0, "host", NOW, holder_alive=lambda record: False)
        self.assertTrue(taken)
        self.assertEqual(holder["pid"], 200)
        self.assertEqual(len(self.book.events("runner_started")), 2)

    def test_a_pause_names_its_kind_and_reason(self) -> None:
        with self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK"):
            self.sql("UPDATE runner SET paused = 1")
        self.book.pause("disk", "short", NOW)
        self.assertFalse(self.book.resume(NOW, kinds=["pin"]))
        self.assertTrue(self.book.resume(NOW, kinds=["disk"]))
        self.assertEqual(self.book.runner()["paused"], 0)

    def test_unknown_columns_are_refused(self) -> None:
        with self.assertRaises(ledger.LedgerError):
            self.book.update("u1", NOW, library_path="somewhere")


if __name__ == "__main__":
    unittest.main()
