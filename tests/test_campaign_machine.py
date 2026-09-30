"""The campaign state machine against fake ports: every rule the user set, one scenario each.

The rules (2026-09-30): a unit failure never stops the campaign; a failed unit is retried twice and then
its raw data are deleted; raw data are deleted once the outputs are produced and the mzTab-M validates,
whatever the gate says, with the verdict recorded; skipped and excluded units' raw data are deleted; one
Console at a time and nothing fetched ahead unless prefetch says so; stall detection, never an outer
limit; a short disk pauses. Nothing here downloads a byte or starts a Console.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes as fakes  # noqa: E402
from campaign import ledger, machine, policy, ports  # noqa: E402


class Base(unittest.TestCase):
    def world(self, units=("u1",), **options) -> fakes.World:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        return fakes.World(Path(self.directory.name), list(units), **options)

    def finish(self, world: fakes.World, **options):
        world.run(**options)
        book = world.open()
        self.addCleanup(book.close)
        return book

    @staticmethod
    def boundaries(book: ledger.Ledger, unit: str) -> list[str]:
        return [row["boundary"] for row in book.transitions(unit) if row["boundary"]]

    def step_until(self, world: fakes.World, book: ledger.Ledger, runner: machine.Runner, predicate, limit: int = 400) -> None:
        for _ in range(limit):
            runner.iterate()
            if predicate():
                return
            world.clock.sleep(30)
        self.fail("the condition was never reached")


class HappyPathTests(Base):
    def test_a_declared_unit_reaches_done_with_every_record(self) -> None:
        world = self.world()
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["terminal_reason"]), ("done", "outputs_produced"))
        self.assertEqual(unit["raw_disposition"], "released")
        self.assertIn("gate pre_cleanup exit 4", unit["raw_detail"], "the verdict is recorded beside the deletion")
        self.assertEqual(self.boundaries(book, "u1"), ["3", "1", "3", "4", "4", "5"])
        self.assertTrue(all(row["approval_id"] == "approval-1" for row in book.transitions("u1") if row["boundary"]))
        self.assertEqual([row["point"] for row in book.gate_verdicts("u1")], ["before_production", "pre_cleanup", "final"])
        saved = world.catalog.saved[0]
        self.assertEqual(saved["ratification"], {
            "approval_id": "approval-1", "manifest_digest": world.digest, "authorization_sha256": fakes.SHA["auth"],
            "campaign_id": "test-campaign", "proposal_id": world.class_digest("u1"),
        })
        run = next(iter(world.catalog.runs.values()))
        self.assertEqual(run["run_id"], f"u1:{unit['run_job_id']}")
        self.assertEqual((run["status"], run["gate_verdict"], run["gate_exit_code"]), ("outputs_produced", "held", 4))
        self.assertEqual(run["mztab_path"], "output/result.mztab")
        record = json.loads((Path(unit["workspace"]) / "campaign-record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["report_terms"], [policy.OUTPUTS_PRODUCED])
        self.assertFalse(record["completed"])
        self.assertEqual([item["boundary"] for item in record["boundary_crossings"]], ["3", "1", "3", "4", "4", "5"])
        self.assertTrue((Path(unit["workspace"]) / "provenance" / "campaign-authorization.json").is_file())
        self.assertFalse((Path(unit["workspace"]) / "raw").exists())
        self.assertIsNone(book.slot(), "the Console slot is free at the end")

    def test_the_run_is_given_the_approved_answers(self) -> None:
        world = self.world()
        self.finish(world)
        run = next(arguments for name, arguments in world.interactive.calls if name == "run")
        answers = run["answers"]
        self.assertEqual(answers["minimum_peak_height"], 1200.0)
        self.assertEqual(answers["smoothing_method"], "TimeBasedLinearWeightedMovingAverage")
        self.assertEqual(answers["console_path"], world.pins["console"]["path"])
        self.assertEqual(answers["libraries"]["msp_paths"], [world.libraries[fakes.POSITIVE_MSP]])
        self.assertTrue(answers["workflow_overrides"]["repository_run_manifest"].replace("\\", "/")
                        .endswith("/u1/provenance/run-manifest.json"), "Interactive's seed about the unit is kept")
        self.assertEqual((run["timeout_seconds"], run["idle_timeout_seconds"]), (0.0, 6 * 3600.0),
                         "stall detection only: the Console watch, no outer limit")
        diagnostic = next(arguments for name, arguments in world.interactive.calls if name == "diagnostic")
        self.assertNotIn("minimum_peak_height", diagnostic["answers"])

    def test_a_clean_final_gate_is_completed(self) -> None:
        world = self.world()
        world.gate.exits.update(pre_cleanup=0, final=0)
        book = self.finish(world)
        self.assertEqual(book.unit("u1")["terminal_reason"], "completed")
        self.assertEqual(machine.summary(book)[policy.COMPLETED], 1)
        self.assertEqual(next(iter(world.catalog.runs.values()))["status"], "completed")

    def test_raw_data_go_whatever_the_gate_verdict(self) -> None:
        world = self.world()
        world.gate.exits.update(before_production=2, pre_cleanup=2, final=2)
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["raw_disposition"]), ("done", "released"))
        self.assertIn("exit 2", unit["raw_detail"])

    def test_a_fourier_transform_instrument_steps_by_1000(self) -> None:
        world = self.world()
        with world.open() as book:
            book.connection.execute("UPDATE unit SET instrument = 'Thermo Orbitrap Exploris 480'")
        book = self.finish(world)
        self.assertEqual((book.unit("u1")["threshold_step"], book.unit("u1")["minimum_peak_height"]), (1000, 12000.0))
        self.assertIn(("estimate", {"job_id": book.unit("u1")["diagnostic_job_id"], "step": 1000}), world.interactive.calls)

    def test_the_campaign_keeps_raw_data_when_its_retention_says_so(self) -> None:
        world = self.world(retention="keep", covers=("1", "3", "4", "split"))
        book = self.finish(world)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["raw_disposition"]), ("done", "kept"))
        self.assertNotIn("5", self.boundaries(book, "u1"))


class FailureTests(Base):
    def test_a_failed_unit_is_retried_twice_then_its_raw_data_go(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(downloads=["fail", "fail", "fail"])
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"], unit["raw_disposition"]), ("failed", 3, "discarded"))
        self.assertEqual(unit["terminal_reason"], "download_failed")
        self.assertEqual(self.boundaries(book, "u1")[-1], "5")
        self.assertEqual(world.interactive.download_starts.count("u1"), 3)
        counted = [row for row in book.attempts("u1") if row["counted"]]
        self.assertEqual(len(counted), 3)
        self.assertEqual(book.unit("u2")["state"], "done", "a unit failure never stops the campaign")

    def test_a_retry_that_succeeds(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(downloads=["fail", "ok"])
        book = self.finish(world)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 1))
        waited = [row for row in book.transitions("u1") if row["to_state"] == "waiting_retry"]
        self.assertEqual(len(waited), 1)

    def test_a_stalled_download_is_cancelled_and_retried(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(downloads=["stall", "ok"])
        book = self.finish(world)
        self.assertEqual(len(world.interactive.cancels), 1)
        self.assertEqual(world.interactive.cancels[0][1], "stalled")
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 1))
        self.assertIn("stalled", [row["outcome"] for row in book.attempts("u1")])

    def test_a_console_idle_timeout_is_retried_then_fails(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(runs=["timeout", "timeout", "timeout"])
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"], unit["raw_disposition"]), ("failed", 3, "discarded"))
        self.assertEqual([row["outcome"] for row in book.attempts("u1") if row["counted"]], ["timeout"] * 3)
        self.assertEqual(world.interactive.console_starts.count(("u1", "run")), 3)
        self.assertEqual(world.interactive.console_starts.count(("u1", "diagnostic")), 1, "the diagnostic is not rerun")
        self.assertEqual(book.unit("u2")["state"], "done")

    def test_outputs_that_do_not_validate_are_a_failure(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(runs=["invalid", "ok"])
        book = self.finish(world)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 1))

    def test_a_lost_reply_is_adopted_not_restarted(self) -> None:
        for outcome in ("lose_reply", "busy_reply"):
            with self.subTest(outcome):
                world = self.world()
                world.scripts["u1"] = fakes.UnitScript(runs=[outcome])
                book = self.finish(world)
                self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 0))
                self.assertEqual(world.interactive.console_starts.count(("u1", "run")), 1)

    def test_a_skip_request_cancels_the_running_console(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(ticks=50)
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "running")
        book.add_request("skip", "u1", "the operator's reason", "Test Person", runner.stamp())
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] in ledger.TERMINAL_STATES)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["terminal_reason"], unit["raw_disposition"]), ("skipped", "operator_skip", "discarded"))
        self.assertIn((unit["run_job_id"], "skip"), world.interactive.cancels)
        self.assertEqual(unit["failures"], 0)
        self.step_until(world, book, runner, lambda: book.unit("u2")["state"] == "done")


class InterruptionTests(Base):
    def test_a_backend_restart_interrupts_the_run_without_counting(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(ticks=10)
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "running")
        world.interactive.restart()
        runner.run(until_idle=True, max_iterations=2000)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"], unit["interruptions"]), ("done", 0, 1))
        self.assertEqual(world.interactive.console_starts.count(("u1", "run")), 2)
        self.assertIn("interrupted", [row["outcome"] for row in book.attempts("u1") if not row["counted"]])

    def test_a_download_the_backend_forgot_is_fetched_again_without_counting(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(ticks=10)
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "downloading")
        world.interactive.forget()
        runner.run(until_idle=True, max_iterations=2000)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"], unit["interruptions"]), ("done", 0, 1))
        self.assertEqual(world.interactive.download_starts.count("u1"), 2)

    def test_interruptions_count_once_past_their_budget(self) -> None:
        world = self.world(policy_values={"max_interruptions": 1})
        world.scripts["u1"] = fakes.UnitScript(ticks=10)
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        for _ in range(2):
            self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "running")
            world.interactive.restart()
            runner.iterate()
        runner.run(until_idle=True, max_iterations=2000)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"], unit["interruptions"]), ("done", 1, 1))


class StepErrorTests(Base):
    def test_a_poll_that_raises_keeps_the_console_slot_until_it_gives_the_job_up(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(ticks=100)
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "running")
        original = world.interactive.job
        world.interactive.job = lambda job_id: (_ for _ in ()).throw(RuntimeError("unreadable reply"))
        for _ in range(machine.Runner.POLL_ERROR_LIMIT - 1):
            runner.iterate()
            self.assertEqual(book.unit("u1")["state"], "running")
            self.assertIsNotNone(book.slot(), "the Console may still run: its slot is kept")
        self.assertEqual(len(book.events("poll_error")), 1)
        runner.iterate()
        world.interactive.job = original
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["resume_state"], unit["failures"]), ("waiting_retry", "prepared", 1))
        self.assertIn((unit["run_job_id"], "poll_error"), world.interactive.cancels)
        runner.run(until_idle=True, max_iterations=2000)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])

    def test_an_error_in_a_split_parent_is_looked_at_again_not_restarted(self) -> None:
        world = self.world(("u1",))
        world.scripts["u1"] = fakes.UnitScript(disposition="split", ticks=5)
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "split_parent")
        original = runner._state_split_parent
        runner._state_split_parent = lambda unit: (_ for _ in ()).throw(RuntimeError("broken"))
        runner.iterate()
        self.assertEqual(book.unit("u1")["state"], "split_parent")
        self.assertEqual(book.unit("u1")["failures"], 0)
        runner._state_split_parent = original
        runner.run(until_idle=True, max_iterations=2000)
        self.assertEqual(book.unit("u1")["state"], "split_done")


class DispositionTests(Base):
    def test_skipped_and_excluded_units_lose_their_raw_data_and_the_campaign_goes_on(self) -> None:
        world = self.world(("u1", "u2", "u3"))
        world.scripts["u1"] = fakes.UnitScript(disposition="skip")
        world.scripts["u2"] = fakes.UnitScript(disposition="exclude")
        book = self.finish(world)
        for key, state in (("u1", "skipped"), ("u2", "excluded")):
            unit = book.unit(key)
            self.assertEqual((unit["state"], unit["raw_disposition"]), (state, "discarded"))
            self.assertEqual(unit["terminal_reason"], f"preflight_{'skip' if state == 'skipped' else 'exclude'}")
            self.assertEqual(json.loads(unit["disposition_json"])["reasons"], ["test_skip" if state == "skipped" else "test_exclude"])
            self.assertTrue((Path(unit["workspace"]) / "campaign-record.json").is_file())
        self.assertEqual(book.unit("u3")["state"], "done")
        self.assertFalse([start for start in world.interactive.console_starts if start[0] in ("u1", "u2")])

    def test_a_missing_disposition_pauses_the_campaign_instead_of_deciding(self) -> None:
        for kind in ("none", "malformed"):
            with self.subTest(kind):
                world = self.world(("u1", "u2"))
                world.scripts["u1"] = fakes.UnitScript(disposition=kind)
                book = self.finish(world, max_iterations=40)
                self.assertEqual(book.runner()["pause_kind"], "fault")
                self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("downloaded", 0))
                self.assertEqual(book.unit("u2")["state"], "pending", "nothing else starts on a broken contract")

    def test_a_disposition_from_another_extractor_pauses_on_the_pin(self) -> None:
        world = self.world()
        world.extractor_sha = "f" * 64
        book = self.finish(world, max_iterations=20)
        self.assertEqual(book.runner()["pause_kind"], "pin")
        self.assertEqual(book.unit("u1")["state"], "downloaded")

    def test_a_split_parent_is_released_once_its_last_part_ends(self) -> None:
        for supported in (False, True):
            with self.subTest(split_parent_release=supported):
                world = self.world(("u1", "u2"))
                world.scripts["u1"] = fakes.UnitScript(disposition="split")
                world.interactive.split_release_supported = supported
                book = self.finish(world)
                parent = book.unit("u1")
                self.assertEqual((parent["state"], parent["role"]), ("split_done", "split_parent"))
                self.assertEqual(parent["raw_disposition"], "released" if supported else "kept")
                parts = [unit for unit in book.units() if unit["parent_unit_key"] == "u1"]
                self.assertEqual([unit["unit_key"] for unit in parts], ["u1-DDA", "u1-SWATH"])
                self.assertTrue(all(unit["state"] == "done" and unit["raw_disposition"] == "deferred_to_parent" for unit in parts))
                self.assertIn("split", self.boundaries(book, "u1"))
                starts = [start for start in world.interactive.console_starts if start[0].startswith("u1")]
                self.assertEqual(starts, [("u1-DDA", "diagnostic"), ("u1-DDA", "run"), ("u1-SWATH", "diagnostic"), ("u1-SWATH", "run")])
                self.assertFalse([name for name, _ in world.interactive.calls if name in ("cleanup",) and "u1-" in _["manifest_path"]])
                self.assertEqual(book.unit("u2")["state"], "done")
                release = [row["seq"] for row in book.transitions("u1") if row["to_state"] == "split_done"][0]
                ended = [row["seq"] for unit in parts for row in book.transitions(unit["unit_key"]) if row["to_state"] == "done"]
                self.assertGreater(release, max(ended))


class ApprovalTests(Base):
    def test_a_changed_class_stops_the_unit_and_saves_nothing(self) -> None:
        world = self.world(("u1", "u2"))
        world.catalog.drift.add("u1")
        book = self.finish(world)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["terminal_reason"]), ("stopped_policy_drift", "class_digest_changed"))
        self.assertEqual([item["unit_id"] for item in world.catalog.saved], ["u2"])
        self.assertNotIn("u1", world.interactive.download_starts)

    def test_a_revoked_approval_stops_before_the_call(self) -> None:
        world = self.world(("u1",))
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "handoff_ready")
        book.revoke("approval-1", "Test Person", "stop the campaign", runner.stamp())
        runner.run(until_idle=True, max_iterations=20)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["terminal_reason"]), ("stopped_no_approval", "approval_missing"))
        self.assertEqual(world.interactive.download_starts, [], "no download under a revoked approval")
        self.assertEqual(book.open_attempts("u1"), [])

    def test_a_blocked_download_is_interactives_exclusion(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(downloads=["blocked"])
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["terminal_reason"], unit["raw_disposition"]), ("excluded", "download_blocked", "none"))
        self.assertEqual(book.unit("u2")["state"], "done")


class ScheduleTests(Base):
    def test_one_unit_in_hand_with_no_prefetch(self) -> None:
        world = self.world(("u1", "u2", "u3"))
        seen = []
        original = world.interactive.download

        def download(**arguments):
            with contextlib.closing(sqlite3.connect(world.ledger_path)) as connection:
                seen.append(dict(connection.execute("SELECT unit_key, state FROM unit").fetchall()))
            return original(**arguments)

        world.interactive.download = download
        self.finish(world)
        for states in seen:
            busy = [key for key, state in states.items() if state not in ("pending", *ledger.TERMINAL_STATES)]
            self.assertEqual(len(busy), 1, states)

    def test_prefetch_one_downloads_while_a_console_runs(self) -> None:
        world = self.world(("u1", "u2"), policy_values={"prefetch": 1})
        world.scripts["u1"] = fakes.UnitScript(ticks=6)
        seen = []
        original = world.interactive.download

        def download(**arguments):
            with contextlib.closing(sqlite3.connect(world.ledger_path)) as connection:
                seen.append(dict(connection.execute("SELECT unit_key, state FROM unit").fetchall()))
            return original(**arguments)

        world.interactive.download = download
        book = self.finish(world)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])
        self.assertNotIn(seen[1]["u1"], ("pending", *ledger.TERMINAL_STATES), "u2 was fetched ahead")
        with world.open() as check:
            runs = check.console_runs()
        spans = [(row["requested_at"], row["ended_at"]) for row in runs]
        for (start, end), (next_start, _) in zip(spans, spans[1:]):
            self.assertLessEqual(end, next_start, "one Console at a time")


class DiskTests(Base):
    def test_a_short_disk_pauses_and_resumes(self) -> None:
        world = self.world()
        world.disk.free = 600 * 1000**3
        book = self.finish(world, max_iterations=10)
        self.assertEqual(book.runner()["pause_kind"], "disk")
        self.assertEqual(book.unit("u1")["state"], "handoff_ready")
        self.assertEqual(world.interactive.download_starts, [])
        self.assertEqual([event["action"] for event in book.disk_events()], ["pause"])
        book.close()
        world.disk.free = 10 * 1000**4
        book = self.finish(world)
        self.assertEqual(book.unit("u1")["state"], "done")
        self.assertEqual(book.unit("u1")["failures"], 0)
        self.assertEqual([event["action"] for event in book.disk_events()][:3], ["pause", "resume", "admit"])

    def test_a_unit_no_volume_could_hold_waits_and_the_campaign_goes_on(self) -> None:
        world = self.world(("u1", "u2"))
        with world.open() as book:
            book.connection.execute("UPDATE unit SET known_bytes = ? WHERE unit_key = 'u1'", (30 * 1000**4,))
        book = self.finish(world)
        self.assertEqual(book.unit("u1")["state"], "deferred_disk", "no per-unit size limit: deferred, never ended")
        self.assertEqual(book.unit("u2")["state"], "done")
        self.assertIn("never_fits", [event["action"] for event in book.disk_events()])

    def test_a_download_of_unknown_size_is_stopped_at_the_floor_without_counting(self) -> None:
        world = self.world(size_known=False, known_bytes=0)
        world.scripts["u1"] = fakes.UnitScript(ticks=20)
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "downloading")
        world.disk.free = 50 * 1000**3
        self.step_until(world, book, runner, lambda: book.runner()["pause_kind"] == "disk")
        self.assertEqual(book.unit("u1")["state"], "handoff_ready")
        self.assertIn("cancel_download", [event["action"] for event in book.disk_events()])
        world.disk.free = 10 * 1000**4
        runner.run(until_idle=True, max_iterations=2000)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 0))
        self.assertEqual(world.interactive.download_starts.count("u1"), 2)

    def test_a_held_raw_deletion_is_retried_then_the_raw_data_are_kept(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(cleanup="blocked")
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["raw_disposition"], unit["failures"]), ("done", "kept", 0))
        self.assertIn("finalisation_held", unit["raw_detail"])
        self.assertEqual([name for name, _ in world.interactive.calls].count("cleanup"), 3)


class PinTests(Base):
    def test_a_changed_pin_pauses_until_it_is_restored(self) -> None:
        world = self.world(("u1", "u2"))
        world.pin_source.values["console"]["inventory_sha256"] = "9" * 64
        book = self.finish(world, max_iterations=10)
        self.assertEqual(book.runner()["pause_kind"], "pin")
        self.assertEqual(book.unit("u1")["state"], "pending", "the next unit is not started on another Console")
        self.assertIn("console.inventory_sha256", book.runner()["pause_reason"])
        book.close()
        world.pin_source.values["console"]["inventory_sha256"] = world.pins["console"]["inventory_sha256"]
        book = self.finish(world)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])


class PrivacyTests(Base):
    def test_no_private_library_location_leaves_the_call_to_interactive(self) -> None:
        world = self.world(("u1",))
        world.scripts["u1"] = fakes.UnitScript(runs=["fail", "ok"])
        book = self.finish(world)
        needle = str(world.private_directory).casefold()
        self.assertIn(needle, json.dumps([arguments for _name, arguments in world.interactive.calls]).casefold().replace("\\\\", "\\"))
        dump = []
        for (table,) in book.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            dump.extend(json.dumps(list(row), default=str) for row in book.connection.execute(f"SELECT * FROM {table}"))
        self.assertGreater(len(dump), 40)
        text = "\n".join(dump).casefold().replace("\\\\", "\\")
        self.assertFalse(needle in text or "private libraries" in text, "a private library location reached the ledger")
        for path in world.workspace_root.rglob("*"):
            if path.is_file() and path.suffix in (".json", ".csv", ".tsv"):
                content = path.read_text(encoding="utf-8", errors="replace").casefold()
                self.assertFalse("private libraries" in content, f"a private library location reached {path.name}")


class GatePortTests(unittest.TestCase):
    def test_the_gate_is_run_strict_and_never_records_a_reading(self) -> None:
        gate = ports.GatePort(gate_commit="c" * 40)
        for point in ("before_production", "pre_cleanup", "final"):
            command = gate.command("D:/analysis/unit", point)
            self.assertIn("--strict", command)
            self.assertIn("--json", command)
            self.assertTrue(command[1].endswith("verify-run-invariants.py"))
            self.assertFalse(any("record-reading" in part for part in command))
        source = "".join((ports.GATE_SCRIPT.parent / "campaign" / name).read_text(encoding="utf-8")
                         for name in ("ports.py", "machine.py", "policy.py", "ledger.py", "plan.py"))
        self.assertNotIn("record-reading.py", source.replace("never runs record-reading.py", ""))

    def test_the_gate_report_is_kept_with_its_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "gate.py"
            script.write_text(
                "import json, sys\n"
                "print(json.dumps({'checks': [{'check_id': 'SUM-1', 'status': 'WARN'}, {'check_id': 'READ-1', 'status': 'not_evaluable'}],"
                " 'strict_failures': ['READ-1'], 'progress': {'stage_reached': 'B10'}}))\n"
                "sys.exit(4)\n",
                encoding="utf-8",
            )
            gate = ports.GatePort(python=sys.executable, script=script, timeout=60, gate_commit="c" * 40)
            report = Path(directory) / "reports" / "01-final.json"
            verdict = gate.run(directory, "final", report)
            self.assertEqual((verdict["outcome"], verdict["exit_code"]), ("ran", 4))
            self.assertEqual(verdict["strict_hold_ids"], ["READ-1"])
            self.assertEqual(verdict["warn_ids"], ["SUM-1"])
            self.assertEqual(verdict["stage_reached"], "B10")
            self.assertTrue(report.is_file())


if __name__ == "__main__":
    unittest.main()
