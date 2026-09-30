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

GB = 1000**3


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
                self.assertEqual(book.runner()["pause_kind"], "contract")
                self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("downloaded", 0))
                self.assertEqual(book.unit("u2")["state"], "pending", "nothing else starts on a broken contract")

    def test_a_disposition_from_another_extractor_pauses_until_an_operator_resumes(self) -> None:
        world = self.world(("u1", "u2"))
        world.extractor_sha = "f" * 64
        book = self.finish(world, max_iterations=40)
        self.assertEqual(book.runner()["pause_kind"], "contract")
        self.assertEqual(book.unit("u1")["state"], "downloaded")
        preflights = [name for name, _ in world.interactive.calls if name == "preflight"]
        self.assertEqual(len(preflights), 1, "the pause does not lift itself and run the preflight again")
        self.assertEqual(len(book.events("resumed")), 0)
        self.assertEqual(book.unit("u2")["state"], "pending")
        # The operator looked into it; the preflight then runs again, under the pinned extractor this time.
        world.extractor_sha = fakes.SHA["extractor"]
        self.assertTrue(book.resume(policy.iso(world.clock.now()), kinds=["contract"]))
        book.close()
        book = self.finish(world)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])

    def test_a_split_parent_is_released_once_its_last_part_ends(self) -> None:
        for supported in (False, True):
            with self.subTest(split_parent_release=supported):
                world = self.world(("u1", "u2"))
                world.scripts["u1"] = fakes.UnitScript(disposition="split")
                world.interactive.split_release_supported = supported
                book = self.finish(world)
                parent = book.unit("u1")
                self.assertEqual((parent["state"], parent["role"]), ("split_done", "split_parent"))
                self.assertEqual(parent["raw_disposition"], "released" if supported else "held",
                                 "raw data the rules delete and Interactive cannot yet are held, not kept")
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
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["resume_state"]), ("deferred_disk", "handoff_ready"),
                         "out of the hand while it waits")
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
        self.assertEqual(book.unit("u1")["state"], "deferred_disk")
        self.assertIn("cancel_download", [event["action"] for event in book.disk_events()])
        world.disk.free = 10 * 1000**4
        runner.run(until_idle=True, max_iterations=2000)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 0))
        self.assertEqual(world.interactive.download_starts.count("u1"), 2)

    def test_a_raw_deletion_interactive_keeps_refusing_is_retried_then_held(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(cleanup="blocked")
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["raw_disposition"], unit["failures"]), ("done", "held", 0))
        self.assertIn("finalisation_held", unit["raw_detail"])
        self.assertEqual([name for name, _ in world.interactive.calls].count("cleanup"), 3)
        self.assertEqual(machine.summary(book)["raw_held"]["unit_keys"], ["u1"])


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


class RawAwareDisk(fakes.FakeDisk):
    """Free space falls by 10 GB for every unit whose raw data are still on the volume."""

    def __init__(self, world: fakes.World, free: int, total: int) -> None:
        super().__init__(free, total)
        self.world = world

    def usage(self, _path: str) -> tuple[int, int]:
        present = [path for path in self.world.workspace_root.rglob("raw") if path.is_dir() and any(path.rglob("*.mzML"))]
        return self.free - 10 * GB * len(present), self.total


class HeldRawTests(Base):
    def test_a_run_that_never_validates_ends_failed_with_its_raw_data_held(self) -> None:
        """Interactive discards no raw data beside an mzTab-M, and cleans up only a validated run: the raw
        data are held, said so once, and no boundary-5 crossing is recorded for a deletion that did not happen."""
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(runs=["invalid", "invalid", "invalid"])
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"], unit["raw_disposition"]), ("failed", 3, "held"))
        self.assertIn("mztab_output_exists", unit["raw_detail"])
        self.assertTrue((Path(unit["workspace"]) / "raw" / "data" / "S1.mzML").is_file(), "the raw data are still there")
        self.assertNotIn("5", self.boundaries(book, "u1"))
        discards = [arguments for name, arguments in world.interactive.calls if name == "discard" and arguments["unit_id"] == "u1"]
        self.assertEqual(len(discards), 1, "a refusal that waiting does not change is not asked again")
        held = machine.summary(book)["raw_held"]
        self.assertEqual((held["units"], held["unit_keys"], held["downloaded_bytes"]), (1, ["u1"], 1000))
        record = json.loads((Path(unit["workspace"]) / "campaign-record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["raw_disposition"], "held")
        self.assertEqual(book.unit("u2")["state"], "done")

    def test_a_failed_unit_whose_outputs_validated_is_cleaned_up_not_discarded(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(runs=["late_fail", "late_fail", "late_fail"])
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["raw_disposition"]), ("failed", "released"))
        self.assertEqual(self.boundaries(book, "u1")[-1], "5")
        self.assertFalse((Path(unit["workspace"]) / "raw").exists())
        names = [name for name, _ in world.interactive.calls]
        self.assertIn("cleanup", names)
        self.assertNotIn("discard", names)


class DiskHandTests(Base):
    def test_a_unit_that_does_not_fit_leaves_the_hand_to_the_retry_that_frees_the_space(self) -> None:
        """u1 fails its run and waits to retry with its raw data on the volume; u2 does not fit beside them.
        u2 waiting in the hand would keep u1's retry out for ever; deferred, it lets the retry run."""
        world = self.world(("u1", "u2"))
        world.disk = RawAwareDisk(world, free=520 * GB, total=2 * 1000**4)  # reserve max(500 GB, 5 %) = 500 GB
        world.scripts["u1"] = fakes.UnitScript(runs=["fail", "ok"])
        book = self.finish(world, max_iterations=2000)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])
        self.assertIn("deferred_disk", [row["to_state"] for row in book.transitions("u2")])
        self.assertIsNone(book.runner()["pause_kind"])
        self.assertEqual([event["action"] for event in book.disk_events() if event["unit_key"] == "u2"][:3],
                         ["pause", "resume", "admit"])

    def test_new_work_waits_behind_a_unit_deferred_for_disk(self) -> None:
        world = self.world(("u1", "u2"))
        world.disk.free = 600 * GB  # under the 1 TB reserve of a 20 TB volume
        book = self.finish(world, max_iterations=20)
        self.assertEqual((book.unit("u1")["state"], book.unit("u2")["state"]), ("deferred_disk", "pending"))
        self.assertEqual([item["unit_id"] for item in world.catalog.saved], ["u1"], "nothing new starts on a short disk")
        self.assertEqual(book.runner()["pause_kind"], "disk")

    def test_interactives_size_limit_defers_the_unit_and_is_asked_once(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(required_bytes=30 * 1000**4)  # a 20 TB volume never holds it
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["known_bytes"], unit["failures"]), ("deferred_disk", 30 * 1000**4, 0))
        self.assertEqual([arguments for name, arguments in world.interactive.calls
                          if name == "download" and "u1" in arguments["handoff_path"]].__len__(), 1)
        self.assertEqual([(row["step"], row["outcome"], row["counted"]) for row in book.attempts("u1")
                          if row["step"] == "download_start"], [("download_start", "blocked", 0)])
        self.assertIn("never_fits", [event["action"] for event in book.disk_events()])
        self.assertEqual(book.unit("u2")["state"], "done")

    def test_the_download_bound_is_what_the_volume_holds_not_what_is_free(self) -> None:
        world = self.world(size_known=False, known_bytes=0)
        world.disk.free = 1300 * GB  # 300 GB above the reserve: enough to start, less than the unit may need
        book = self.finish(world)
        bound = next(arguments["maximum_gb"] for name, arguments in world.interactive.calls if name == "download")
        self.assertEqual(bound, (20 * 1000**4 - 1000**4) / GB)
        self.assertEqual(book.unit("u1")["state"], "done")

    def test_a_lease_that_reaches_the_bound_is_a_short_disk_not_a_failure(self) -> None:
        world = self.world(("u1", "u2"), size_known=False, known_bytes=0)
        world.scripts["u1"] = fakes.UnitScript(remote_bytes=25 * 1000**4)
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"]), ("deferred_disk", 0))
        self.assertEqual([row for row in book.attempts("u1") if row["counted"]], [])
        self.assertEqual([row["outcome"] for row in book.attempts("u1") if row["step"] == "download"], ["blocked"])
        self.assertGreater(unit["known_bytes"], 19 * 1000**4)
        self.assertEqual(world.interactive.download_starts.count("u1"), 1, "not retried at once into the same limit")
        self.assertEqual(book.unit("u2")["state"], "done")

    def test_an_overdue_retry_waiting_for_the_hand_does_not_make_the_loop_spin(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(runs=["fail", "ok"])
        world.scripts["u2"] = fakes.UnitScript(ticks=2000)
        polls = []
        original = world.interactive.job

        def job(job_id):
            polls.append(world.clock.now())
            return original(job_id)

        world.interactive.job = job
        with world.open() as book:
            world.runner(book).run(max_iterations=600)
            self.assertEqual(book.unit("u1")["state"], "waiting_retry")
        gaps = sorted((later - earlier).total_seconds() for earlier, later in zip(polls[-200:], polls[-199:]))
        self.assertGreaterEqual(gaps[len(gaps) // 2], 29.0)


class EndStepErrorTests(Base):
    def test_an_error_ending_a_unit_is_retried_later_without_counting(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(downloads=["fail", "fail", "fail"])
        broken = {"now": True}
        original = world.gate.run

        def gate(workspace, point, report_path):
            if Path(workspace).name == "u1" and point == "final" and broken["now"]:
                raise OSError("the report could not be written")
            return original(workspace, point, report_path)

        world.gate.run = gate
        with world.open() as book:
            world.runner(book).run(max_iterations=400)
            unit = book.unit("u1")
            self.assertEqual((unit["state"], unit["resume_state"], unit["pending_terminal"]), ("waiting_retry", "discarding", "failed"))
            self.assertEqual(unit["failures"], 3, "the end's errors are not the unit's")
            self.assertLess(len(book.transitions("u1")), 40, "no loop without a pause")
            self.assertGreater(world.clock.slept, 0)
            self.assertEqual(book.unit("u2")["state"], "done", "the hand is free meanwhile")
            self.assertFalse([row for row in book.attempts("u1") if row["step"] == "discarding_step" and row["counted"]])
        broken["now"] = False
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["terminal_reason"], unit["raw_disposition"]), ("failed", "download_failed", "discarded"))

    def test_an_excluded_unit_whose_end_errors_is_still_excluded(self) -> None:
        world = self.world(("u1",))
        world.scripts["u1"] = fakes.UnitScript(disposition="exclude")
        left = {"errors": 4}
        original = world.gate.run

        def gate(workspace, point, report_path):
            if point == "final" and left["errors"] > 0:
                left["errors"] -= 1
                raise OSError("transient")
            return original(workspace, point, report_path)

        world.gate.run = gate
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "waiting_retry")
        # An operator's retry brings the wait forward; what the unit is ending as stays.
        book.add_request("retry", "u1", "try the end again now", "Test Person", runner.stamp())
        runner.run(until_idle=True, max_iterations=2000)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["terminal_reason"], unit["failures"]), ("excluded", "preflight_exclude", 0))
        self.assertEqual(unit["raw_disposition"], "discarded")

    def test_a_unit_with_outputs_is_never_failed_by_an_error_at_its_end(self) -> None:
        world = self.world(("u1",))
        left = {"errors": 5}
        original = world.gate.run

        def gate(workspace, point, report_path):
            if point == "final" and left["errors"] > 0:
                left["errors"] -= 1
                raise PermissionError("the file is held by another process")
            return original(workspace, point, report_path)

        world.gate.run = gate
        book = self.finish(world, max_iterations=4000)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["terminal_reason"], unit["failures"]), ("done", "outputs_produced", 0))
        self.assertEqual(unit["raw_disposition"], "released", "what became of the raw data is not rewritten")
        self.assertEqual(len([row for row in book.attempts("u1") if row["step"] == "releasing_step"]), 5)


class FaultTests(Base):
    def test_the_fault_recheck_comes_an_hour_after_the_first_fault(self) -> None:
        """Two jobs in flight whose polls fail in turn used to move paused_at every poll, so the hourly
        recheck, the only place the backend is restarted, never came."""
        world = self.world(("u1", "u2"), policy_values={"prefetch": 1})
        world.scripts["u1"] = fakes.UnitScript(ticks=400)
        world.scripts["u2"] = fakes.UnitScript(ticks=400)
        down = {"now": False}
        ensured = []

        class Backend:
            def ensure(self_inner) -> dict:
                ensured.append(world.clock.now())
                down["now"] = False
                return {"ok": True, "started": True, "pid": 1}

        original = world.interactive.job
        world.interactive.job = lambda job_id: (
            {"ok": False, "reason": "backend_unavailable", "detail": "Could not connect"} if down["now"] else original(job_id)
        )
        ports_ = world.ports()
        ports_.backend = Backend()
        with world.open() as book:
            runner = machine.Runner(book, ports_, resources={"libraries": world.libraries})
            self.step_until(world, book, runner,
                            lambda: all(book.unit(key)["state"] in machine.IN_FLIGHT for key in ("u1", "u2")))
            down["now"] = True
            started = world.clock.now()
            for _ in range(360):
                runner.iterate()
                world.clock.sleep(30)
            self.assertEqual(len(ensured), 1)
            self.assertLessEqual((ensured[0] - started).total_seconds(), 3600 + 60)
            self.assertEqual(len(book.events("paused")), 1)
            runner.run(until_idle=True, max_iterations=4000)
            self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])


class OutageTests(Base):
    def test_a_repository_outage_pauses_the_campaign_instead_of_failing_its_units(self) -> None:
        units = [f"u{index:02d}" for index in range(20)]
        world = self.world(units)
        world.outage = True
        with world.open() as book:
            world.runner(book).run(max_iterations=3000)
            self.assertEqual([key for key in units if book.unit(key)["state"] == "failed"], [])
            self.assertEqual(book.runner()["pause_kind"], "fault")
            counted = sum(1 for row in book.attempts() if row["counted"])
            self.assertEqual(counted, 2, "only the failures before the outage was recognised count")
            faults = [row for row in book.attempts() if row["outcome"] == "fault"]
            self.assertGreater(len(faults), 5, "the download is tried again at every recheck, uncounted")
        world.outage = False
        book = self.finish(world, max_iterations=20000)
        self.assertEqual({book.unit(key)["state"] for key in units}, {"done"})
        self.assertLessEqual(max(book.unit(key)["failures"] for key in units), 1)

    def test_failures_of_the_units_own_objects_are_not_an_outage(self) -> None:
        world = self.world(("u1", "u2", "u3"))
        for key in ("u1", "u2", "u3"):
            world.scripts[key] = fakes.UnitScript(downloads=["corrupt"])
        book = self.finish(world)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2", "u3")], ["failed"] * 3)
        self.assertEqual(book.events("paused"), [])


class BeforeProductionGateTests(Base):
    """A before-production FAIL stops the unit's run only for a check that breaks results (2026-10-01)."""

    def production_starts(self, world: fakes.World, unit: str) -> int:
        return world.interactive.console_starts.count((unit, "run"))

    def test_a_fail_that_breaks_results_fails_the_unit_and_its_raw_data_go(self) -> None:
        world = self.world(("u1", "u2"))
        world.gate.fails[("u1", "before_production")] = ["INP-1", "CLS-1"]
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"], unit["raw_disposition"]), ("failed", 3, "discarded"))
        self.assertEqual(json.loads(unit["terminal_detail"])["reason"], "before_production_gate_failed")
        self.assertEqual(self.production_starts(world, "u1"), 0, "no MS-DIAL run on inputs that break results")
        verdicts = [row for row in book.gate_verdicts("u1") if row["point"] == "before_production"]
        self.assertEqual(len(verdicts), 3, "each attempt's verdict is recorded with the failure it caused")
        self.assertEqual({row["blocking_fail_ids_json"] for row in verdicts}, {'["INP-1"]'})
        self.assertEqual({row["run_policy_source"] for row in verdicts}, {"runner_default"})
        self.assertEqual([row["outcome"] for row in book.attempts("u1") if row["counted"]], ["failed"] * 3)
        self.assertEqual(book.unit("u2")["state"], "done", "the campaign goes on")

    def test_a_record_only_fail_is_recorded_and_the_unit_runs(self) -> None:
        world = self.world()
        world.gate.fails["before_production"] = ["CLS-1", "CLS-2", "CLS-3", "ORD-1", "PKH-1"]
        book = self.finish(world)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 0))
        verdict = [row for row in book.gate_verdicts("u1") if row["point"] == "before_production"][0]
        self.assertEqual(json.loads(verdict["fail_ids_json"]), ["CLS-1", "CLS-2", "CLS-3", "ORD-1", "PKH-1"])
        self.assertEqual(json.loads(verdict["blocking_fail_ids_json"]), [])
        self.assertEqual(self.production_starts(world, "u1"), 1)

    def test_the_gates_own_run_policy_decides_when_it_states_one(self) -> None:
        for fails, rules, blocks in (
            (["SUM-1"], {"SUM-1": policy.RECORD_ONLY, "ELIG-1": policy.BLOCKS_RUN}, False),
            (["CLS-1"], {"CLS-1": policy.BLOCKS_RUN}, True),
        ):
            with self.subTest(fails=fails, rules=rules):
                world = self.world()
                world.gate.fails["before_production"] = fails
                world.gate.run_policy = rules
                book = self.finish(world)
                self.assertEqual(book.unit("u1")["state"], "failed" if blocks else "done")
                verdict = [row for row in book.gate_verdicts("u1") if row["point"] == "before_production"][0]
                self.assertEqual(verdict["run_policy_source"], "gate")


class AppliedDispositionTests(Base):
    """Interactive 0.5.17: only a disposition applied under the approval is acted on."""

    def test_a_disposition_recorded_as_advice_is_applied_by_classify_preflight(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(applied=False)
        book = self.finish(world)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 0))
        classified = [arguments for name, arguments in world.interactive.calls if name == "classify"]
        self.assertEqual(len(classified), 1)
        self.assertTrue(classified[0]["authorization_path"].endswith("campaign-authorization.json"))
        self.assertTrue(json.loads(book.unit("u1")["disposition_json"])["applied"])
        self.assertEqual(len([name for name, _ in world.interactive.calls if name == "preflight"]), 1,
                         "the headers are not read again")

    def test_a_disposition_nothing_applies_pauses_the_campaign(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(applied=False, classify_applies=False)
        book = self.finish(world, max_iterations=40)
        self.assertEqual(book.runner()["pause_kind"], "contract")
        self.assertIn("not applied", book.runner()["pause_reason"])
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("downloaded", 0))
        self.assertEqual(book.unit("u2")["state"], "pending")
        self.assertFalse([start for start in world.interactive.console_starts if start[0] == "u1"])

    def test_the_preflight_is_given_the_approval(self) -> None:
        world = self.world()
        self.finish(world)
        preflight = next(arguments for name, arguments in world.interactive.calls if name == "preflight")
        self.assertTrue(preflight["authorization_path"].endswith(str(Path("u1") / "provenance" / "campaign-authorization.json")))

    def test_an_extractor_interactive_refuses_pauses_instead_of_failing_units(self) -> None:
        world = self.world(("u1", "u2"))
        world.extractor_refused = True
        book = self.finish(world, max_iterations=60)
        self.assertEqual(book.runner()["pause_kind"], "contract")
        self.assertIn("raw_metadata_extractor_refused", book.runner()["pause_reason"])
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("downloaded", 0))
        self.assertEqual(book.unit("u1")["raw_disposition"], "present", "no raw data are deleted for the campaign's fault")
        self.assertEqual(len([name for name, _ in world.interactive.calls if name == "preflight"]), 1)
        self.assertEqual(book.unit("u2")["state"], "pending")
        world.extractor_refused = False
        self.assertTrue(book.resume(policy.iso(world.clock.now()), kinds=["contract"]))
        book.close()
        book = self.finish(world)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])

    def test_a_unit_whose_run_may_still_go_is_waited_for_uncounted(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(held="run_in_progress")
        with world.open() as book:
            world.runner(book).run(max_iterations=40)
            unit = book.unit("u1")
            self.assertEqual((unit["state"], unit["resume_state"], unit["failures"]), ("waiting_retry", "downloaded", 0))
            self.assertIn("busy", [row["outcome"] for row in book.attempts("u1")])
        world.scripts["u1"].held = ""
        book = self.finish(world)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 0))

    def test_a_unit_held_for_good_is_its_failure(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(held="past_preflight")
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"]), ("failed", 3))
        self.assertEqual(json.loads(unit["terminal_detail"])["detail"]["held"]["reason"], "past_preflight")


class ContractDeletionTests(Base):
    def test_a_cleanup_this_interactive_cannot_make_pauses_and_keeps_the_raw_data(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(cleanup="unsupported")
        book = self.finish(world, max_iterations=80)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["raw_disposition"], unit["failures"]), ("gated", "present", 0))
        self.assertEqual(book.runner()["pause_kind"], "contract")
        self.assertEqual(book.unit("u2")["state"], "pending")
        self.assertTrue((Path(unit["workspace"]) / "raw").is_dir())
        world.scripts["u1"].cleanup = "ok"
        self.assertTrue(book.resume(policy.iso(world.clock.now()), kinds=["contract"]))
        book.close()
        book = self.finish(world)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])
        self.assertEqual(book.unit("u1")["raw_disposition"], "released")


class SharedDownloadTests(Base):
    def test_a_download_waiting_for_a_shared_object_is_neither_stalled_nor_ended(self) -> None:
        """Plan item 15: a unit's lease waits while another fetches an object they share, moving no bytes of
        its own, for longer than the stall window here."""
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(downloads=["shared"], ticks=120)  # an hour; the stall window is 30 min
        book = self.finish(world)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 0))
        self.assertEqual(world.interactive.cancels, [])
        self.assertEqual(world.interactive.download_starts, ["u1"])


class RunRecordTests(Base):
    def test_every_production_attempt_is_recorded_as_it_ends(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(runs=["timeout", "invalid", "ok"])
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"]), ("done", 2))
        jobs = [row["job_id"] for row in book.console_runs("u1") if row["kind"] == "production"]
        self.assertEqual(sorted(world.catalog.runs), sorted(f"u1:{job}" for job in jobs))
        first, second, last = (world.catalog.runs[f"u1:{job}"] for job in jobs)
        self.assertEqual((first["status"], second["status"]), ("timed_out", "outputs_not_validated"))
        self.assertNotIn("gate_verdict", first, "an attempt's own end, not the unit's")
        self.assertEqual((last["status"], last["gate_verdict"], last["gate_exit_code"]), ("outputs_produced", "held", 4))
        self.assertEqual(last["run_id"], f"u1:{unit['run_job_id']}")

    def test_a_failed_units_runs_are_all_recorded(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(runs=["fail", "timeout", "fail"])
        book = self.finish(world)
        self.assertEqual(book.unit("u1")["state"], "failed")
        statuses = [world.catalog.runs[f"u1:{row['job_id']}"]["status"] for row in book.console_runs("u1") if row["kind"] == "production"]
        self.assertEqual(statuses, ["failed", "timed_out", "failed"])
        last = world.catalog.runs[f"u1:{book.unit('u1')['run_job_id']}"]
        self.assertEqual(last["gate_verdict"], "held", "the unit's end completes its last run's record")


class RequestTests(Base):
    def test_a_skip_does_not_relabel_a_unit_that_is_ending(self) -> None:
        world = self.world(("u1",))
        world.scripts["u1"] = fakes.UnitScript(downloads=["fail", "fail", "fail"])
        broken = {"now": True}
        original = world.gate.run

        def gate(workspace, point, report_path):
            if point == "final" and broken["now"]:
                raise OSError("the report could not be written")
            return original(workspace, point, report_path)

        world.gate.run = gate
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["resume_state"] == "discarding")
        book.add_request("skip", "u1", "not this one", "Test Person", runner.stamp())
        runner.iterate()
        self.assertIn("not skipped: the unit is ending (failed)", book.connection.execute(
            "SELECT handled_detail FROM request").fetchone()[0])
        broken["now"] = False
        runner.run(until_idle=True, max_iterations=2000)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["terminal_reason"]), ("failed", "download_failed"))


class RedactionTests(Base):
    def test_what_the_gate_and_a_job_say_is_redacted_before_it_is_recorded(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(runs=["fail", "ok"])
        location = world.libraries[fakes.POSITIVE_MSP]
        world.gate.detail = f"Traceback: could not open {location}"
        finish = world.interactive._finish

        def finish_quoting_the_library(job, outcome):
            finish(job, outcome)
            if job["kind"] == "run" and job.get("status") == "failed":
                job["error"] = f"MSP not readable: {location}"

        world.interactive._finish = finish_quoting_the_library
        book = self.finish(world)
        self.assertEqual(book.unit("u1")["state"], "done")
        dump = []
        for (table,) in book.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            dump.extend(json.dumps(list(row), default=str) for row in book.connection.execute(f"SELECT * FROM {table}"))
        text = "\n".join(dump).casefold().replace("\\\\", "\\")
        self.assertIn("<library:", text, "the job's error and the gate's detail were recorded, redacted")
        self.assertNotIn(str(world.private_directory).casefold(), text)


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
                "print(json.dumps({'checks': [{'check_id': 'SUM-1', 'status': 'warn'}, {'check_id': 'READ-1', 'status': 'not_evaluable'},"
                " {'check_id': 'CLS-2', 'status': 'fail'}, {'check_id': 'CNT-1', 'status': 'fail'}],"
                " 'strict_failures': ['READ-1'], 'progress': {'stage_reached': 'B10'}}))\n"
                "sys.exit(4)\n",
                encoding="utf-8",
            )
            gate = ports.GatePort(python=sys.executable, script=script, timeout=60, gate_commit="c" * 40)
            report = Path(directory) / "reports" / "01-final.json"
            verdict = gate.run(directory, "final", report)
            self.assertEqual((verdict["outcome"], verdict["exit_code"]), ("ran", 4))
            self.assertEqual(verdict["strict_hold_ids"], ["READ-1"])
            self.assertEqual(verdict["warn_ids"], ["SUM-1"], "the gate's statuses are lowercase")
            self.assertEqual(verdict["fail_ids"], ["CLS-2", "CNT-1"])
            self.assertEqual((verdict["blocking_fail_ids"], verdict["run_policy_source"]), (["CNT-1"], "runner_default"))
            self.assertEqual(verdict["stage_reached"], "B10")
            self.assertTrue(report.is_file())


if __name__ == "__main__":
    unittest.main()
