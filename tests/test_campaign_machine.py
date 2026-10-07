"""The campaign state machine against fake ports: every rule the user set, one scenario each.

The rules (2026-09-30): a unit failure never stops the campaign; a failed unit is retried twice and then
its raw data are deleted; raw data are deleted once the outputs are produced and the mzTab-M validates,
whatever the gate says, with the verdict recorded; skipped and excluded units' raw data are deleted; one
Console at a time and nothing fetched ahead unless prefetch says so; stall detection, never an outer
limit; a short disk pauses. Nothing here downloads a byte or starts a Console.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes as fakes  # noqa: E402
from campaign import ledger, machine, policy, ports  # noqa: E402

GB = 1000**3
TB = 1000**4
# A reply of Interactive's that does not parse, as its wrapper reports one: it holds the unit (2026-10-03).
UNREADABLE = {"ok": False, "reason": "validation_error", "error_type": "JSONDecodeError",
              "detail": "Expecting value: line 1 column 1 (char 0)"}
# Two ways a backend does not answer, as Interactive's wrapper reports them: they pause the campaign (2026-10-03).
SILENCES = {
    "timeout": {"ok": False, "reason": "os_error", "error_type": "TimeoutError", "detail": "timed out"},
    "refused": {"ok": False, "reason": "backend_unavailable", "detail": "Could not connect"},
}


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

    def assert_contract_held(self, world: fakes.World, book: ledger.Ledger, key: str, problem: str,
                             resume_state: str = "downloaded") -> None:
        """One unit held for a reply or a record of Interactive's that the runner cannot read: "stop" is per unit
        (the user's rule of 2026-10-02), so it is held, uncounted with its raw data kept, and warned about,
        and nothing pauses the campaign."""
        unit = book.unit(key)
        self.assertEqual((unit["state"], unit["resume_state"]), ("contract_held", resume_state))
        self.assertEqual((unit["failures"], unit["interruptions"]), (0, 0), "neither a retry nor a failure")
        self.assertFalse([row for row in book.attempts(key) if row["counted"]])
        self.assertEqual(unit["raw_disposition"], "present")
        self.assertTrue((Path(unit["workspace"]) / "raw").is_dir(), "the raw data are kept")
        self.assertFalse([name for name, arguments in world.interactive.calls
                          if name in ("discard", "cleanup") and arguments.get("manifest_path") == unit["manifest_path"]])
        self.assertEqual(world.interactive.console_starts.count((key, "run")), 0, "the unit is not run")
        events = [json.loads(row["detail_json"]) for row in book.events("contract_held") if row["unit_key"] == key]
        self.assertTrue(events and events[-1]["contract"] == problem, events)
        self.assertIn("raw data are kept", events[-1]["warning"])
        self.assertFalse(book.runner()["paused"], "one unit's record pauses nothing")
        document, tsv = machine.export_status(book)
        row = next(item for item in document["units"] if item["unit_key"] == key)
        self.assertEqual(len(row["warnings"]), 1)
        self.assertIn(f"contract: {problem}", row["warnings"][0])
        line = next(line for line in tsv.splitlines() if line.split("\t")[0] == key)
        self.assertIn("raw data are kept", line.split("\t")[machine.TSV_COLUMNS.index("warnings")])
        self.assertIn(key, document["summary"]["contract_held"]["unit_keys"])


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

    def test_a_fourier_transform_family_steps_by_1000(self) -> None:
        """Interactive 0.5.28 reads the family from the file and searches its step: the runner asks once, with no
        step, and takes it."""
        world = self.world()
        world.instrument_family = "Fourier-transform MS"
        with world.open() as book:
            book.connection.execute("UPDATE unit SET instrument = 'Thermo Orbitrap Exploris 480'")
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["threshold_step"], unit["coarse_threshold_step"], unit["minimum_peak_height"]),
                         (1000, 1000, 12000.0))
        asked = [arguments["step"] for name, arguments in world.interactive.calls if name == "estimate"]
        self.assertEqual([0], asked, "no step of the runner's own")

    def test_interactives_family_decides_over_the_catalogs_text(self) -> None:
        """A multi-platform study's Catalog text names a Q Exactive, but Interactive reads the unit's file as
        QTOF-type (review of msdial-interactive-app#61, 2026-10-07). Interactive searches 100, and a step the
        runner asked for would only be recorded there, never searched: the runner asks for none, runs the unit
        at Interactive's step and notes the Catalog's, and never holds the unit for it."""
        world = self.world()
        world.instrument_family_source = "vendor_format"
        with world.open() as book:
            book.connection.execute("UPDATE unit SET instrument = 'Thermo Q Exactive; SCIEX TripleTOF 6600'")
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual(unit["state"], "done")
        self.assertEqual((unit["threshold_step"], unit["coarse_threshold_step"], unit["minimum_peak_height"]),
                         (100, 100, 1200.0))
        asked = [arguments["step"] for name, arguments in world.interactive.calls if name == "estimate"]
        self.assertEqual([0], asked, "never asked again at the Catalog's step of 1,000")
        diagnosed = [json.loads(row["detail_json"]) for row in book.transitions("u1") if row["to_state"] == "diagnosed"]
        self.assertEqual(1, len(diagnosed))
        family = diagnosed[0]["instrument_family"]
        self.assertEqual((family["instrument_family"], family["instrument_family_source"]), ("QTOF", "vendor_format"))
        self.assertIn("would give 1000", family["note"])

    def test_an_estimate_inconsistent_with_its_own_family_holds_the_unit(self) -> None:
        """An estimate that records a Fourier-transform family but searched 100 first is no step the rule gives
        for the family it records: held for a person, as any reply the runner cannot read."""
        world = self.world()
        world.interactive.estimate_patch = {"instrument_family": "Fourier-transform MS"}
        world.run(max_iterations=200)
        book = world.open()
        self.addCleanup(book.close)
        unit = book.unit("u1")
        self.assertEqual(unit["state"], "contract_held")
        asked = [arguments["step"] for name, arguments in world.interactive.calls if name == "estimate"]
        self.assertNotIn(1000, asked, "the runner asks for no step of its own")
        said = [row[0] for row in book.connection.execute("SELECT detail_json FROM attempt WHERE unit_key = 'u1'")]
        self.assertTrue(any("where its family step is 1000" in item for item in said), said)

    def test_an_interactive_before_0_5_28_is_asked_at_the_catalogs_step(self) -> None:
        """An Interactive before 0.5.28 searched the step it was asked for and labelled every mzML QTOF: for it
        alone, the Catalog's Orbitrap text still asks for 1,000."""
        world = self.world()
        world.interactive.legacy_estimate = True
        with world.open() as book:
            book.connection.execute("UPDATE unit SET instrument = 'Thermo Orbitrap Exploris 480'")
        book = self.finish(world)
        self.assertEqual((book.unit("u1")["threshold_step"], book.unit("u1")["minimum_peak_height"]), (1000, 12000.0))
        self.assertIn(("estimate", {"job_id": book.unit("u1")["diagnostic_job_id"], "step": 1000}), world.interactive.calls)

    def test_the_step_an_estimate_fell_back_to_is_recorded(self) -> None:
        """Interactive 0.5.28: no multiple of 100 lands in range, and the estimate falls back to step 10. The
        ledger, campaign-record.json and the catalog's run provenance record the step used, the family step
        searched first and the fallback; and since the estimate searched the policy's family step first, it is
        asked for once, not again at the step it already searched."""
        world = self.world()
        world.interactive.step_fallback = True
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["threshold_step"], unit["coarse_threshold_step"], unit["step_fallback"],
                          unit["fallback_reason"], unit["minimum_peak_height"]),
                         (10, 100, 1, "no_coarse_step_in_range", 120.0))
        asked = [arguments for name, arguments in world.interactive.calls if name == "estimate"]
        self.assertEqual([0], [item["step"] for item in asked], "no second request at the step already searched")
        record = json.loads((Path(unit["workspace"]) / "campaign-record.json").read_text(encoding="utf-8"))
        self.assertEqual((record["threshold_step"], record["coarse_threshold_step"], record["step_fallback"],
                          record["fallback_reason"]), (10, 100, True, "no_coarse_step_in_range"))
        provenance = next(iter(world.catalog.runs.values()))["provenance"]
        self.assertEqual((provenance["threshold_step"], provenance["coarse_threshold_step"], provenance["step_fallback"]),
                         (10, 100, True))

    def test_a_fourier_transform_fallback_is_to_100(self) -> None:
        world = self.world()
        world.interactive.step_fallback = True
        world.instrument_family = "Fourier-transform MS"
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["threshold_step"], unit["coarse_threshold_step"], unit["step_fallback"]), (100, 1000, 1))
        self.assertEqual([0], [arguments["step"] for name, arguments in world.interactive.calls if name == "estimate"])

    def test_an_estimate_without_the_step_rule_is_recorded_as_no_fallback(self) -> None:
        """An Interactive before 0.5.28 records threshold_step alone: the step asked for, with no fallback."""
        world = self.world()
        world.interactive.legacy_estimate = True
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["threshold_step"], unit["coarse_threshold_step"], unit["step_fallback"],
                          unit["fallback_reason"]), (100, 100, 0, None))

    def test_an_estimate_at_a_step_the_rule_does_not_give_holds_the_unit(self) -> None:
        """A fallback to step 1 is no step the rule gives: the unit is not run at it, and is held for a person."""
        world = self.world()
        world.interactive.estimate_patch = {"threshold_step": 1, "coarse_threshold_step": 100, "step_fallback": True}
        world.run(max_iterations=200)
        book = world.open()
        self.addCleanup(book.close)
        unit = book.unit("u1")
        self.assertEqual(unit["state"], "contract_held")
        self.assertIsNone(unit["threshold_step"])
        self.assertFalse([name for name, _ in world.interactive.calls if name == "run"])
        said = [row[0] for row in book.connection.execute("SELECT detail_json FROM attempt WHERE unit_key = 'u1'")]
        self.assertTrue(any("not one the step rule gives" in item for item in said), "the hold's attempt says why")

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


class OrphanConsoleTests(Base):
    """A backend restart on Windows leaves its Console running (no job object). The restarted backend calls
    the job interrupted, and its single-flight refuses another Console on the unit, naming the orphan."""

    def running(self, world: fakes.World):
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "running")
        world.interactive.restart(orphan_consoles=True)
        return book, runner

    def test_an_orphan_whose_backend_is_gone_is_stopped_before_the_unit_is_retried(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(runs=["hold", "ok"])  # the first Console never ends by itself
        book, runner = self.running(world)
        orphan = book.unit("u1")["run_job_id"]
        runner.run(until_idle=True, max_iterations=3000)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"], unit["interruptions"], unit["raw_disposition"]),
                         ("done", 0, 1, "released"))
        self.assertEqual(world.interactive.kills, [orphan])
        self.assertNotEqual(unit["run_job_id"], orphan, "the orphan's job is not adopted")
        calls = world.interactive.calls
        kill = [index for index, (name, _) in enumerate(calls) if name == "kill_orphan"][0]
        second = [index for index, (name, arguments) in enumerate(calls) if name == "run" and arguments["unit"] == "u1"][1]
        self.assertLess(kill, second, "stopped before the retry")
        self.assertEqual(world.interactive.overlapping_starts, [], "no Console started beside the orphan")
        self.assertEqual([json.loads(event["detail_json"])["killed"] for event in book.events("orphan_killed")], [True])
        self.assertEqual(book.unit("u2")["state"], "done")

    def test_an_orphan_whose_backend_still_runs_is_waited_for(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(ticks=40)
        world.interactive.orphan_backend_alive = True
        book, runner = self.running(world)
        runner.run(until_idle=True, max_iterations=3000)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"], unit["interruptions"]), ("done", 0, 1))
        self.assertEqual(world.interactive.kills, [])
        self.assertEqual(world.interactive.overlapping_starts, [], "the second Console started once the orphan ended")
        self.assertEqual(world.interactive.console_starts.count(("u1", "run")), 2)

    def test_nothing_is_retried_failed_or_deleted_under_an_orphan_that_will_not_stop(self) -> None:
        world = self.world(("u1", "u2"), policy_values={"prefetch": 1})
        world.scripts["u1"] = fakes.UnitScript(runs=["hold", "ok"])
        world.interactive.orphan_unkillable = True
        book, runner = self.running(world)
        started = list(world.interactive.console_starts)
        since = world.clock.now()
        runner.run(max_iterations=600)
        kills = [name for name, _ in world.interactive.calls].count("kill_orphan")
        self.assertLessEqual(kills, (world.clock.now() - since).total_seconds() / runner.policy.busy_retry_seconds + 1,
                             "a kill that may wait for the process is not tried at every poll")
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"], unit["interruptions"]), ("running", 0, 0))
        self.assertTrue((Path(unit["workspace"]) / "raw" / "data" / "S1.mzML").is_file())
        self.assertNotIn("discard", [name for name, _ in world.interactive.calls])
        self.assertEqual(world.interactive.console_starts, started, "u2 waits for the Console slot too")
        self.assertIn(book.unit("u2")["state"], ("metadata_prepared", "prepared"))
        self.assertEqual(world.interactive.overlapping_starts, [])
        self.assertEqual(len(book.events("orphan_killed")), 1, "recorded once, not at every poll")
        world.interactive.orphans.clear()  # the orphan ends at last
        runner.run(until_idle=True, max_iterations=3000)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])
        self.assertEqual(world.interactive.overlapping_starts, [])

    def test_a_unit_busy_reply_naming_an_ended_job_is_not_adopted(self) -> None:
        world = self.world()
        original = world.interactive.start_run
        refused = []

        def start_run(**arguments):
            if not refused:
                refused.append(True)
                world.interactive.jobs["rn-old"] = {"id": "rn-old", "kind": "run", "status": "interrupted", "unit": "u1",
                                                    "manifest_path": "", "done_at": world.clock.now(), "outcome": "ok"}
                return {"ok": False, "reason": "unit_busy", "live_job_id": "rn-old"}
            return original(**arguments)

        world.interactive.start_run = start_run
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"]), ("done", 0))
        self.assertNotIn("rn-old", [row["job_id"] for row in book.console_runs("u1")])
        self.assertIn(("run_start", "busy", 0), [(row["step"], row["outcome"], row["counted"]) for row in book.attempts("u1")])


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

    def test_a_missing_or_malformed_disposition_holds_the_unit_and_the_others_go_on(self) -> None:
        """Where one unit's record used to pause the whole campaign until an operator resumed it, it holds that
        unit: "stop" is per unit (2026-10-02). Nothing is decided in Interactive's place meanwhile."""
        for kind, problem in (("none", "no_disposition"), ("malformed", "disposition_malformed")):
            with self.subTest(kind):
                world = self.world(("u1", "u2"))
                world.scripts["u1"] = fakes.UnitScript(disposition=kind)
                book = world.open()
                self.addCleanup(book.close)
                runner = world.runner(book)
                self.step_until(world, book, runner, lambda: book.unit("u2")["state"] == "done")
                self.assert_contract_held(world, book, "u1", problem)
                self.assertIsNone(book.unit("u1")["disposition_json"], "the runner decides nothing itself")
                self.assertFalse(book.events("paused"))
                # Interactive writes a record the runner reads: the recheck, which comes by itself, makes the
                # preflight again and the unit goes on.
                world.scripts["u1"].disposition = "run"
                runner.run(until_idle=True, max_iterations=2000)
                self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 0))
                manifest = book.unit("u1")["manifest_path"]
                self.assertEqual(len([name for name, arguments in world.interactive.calls
                                      if name == "preflight" and arguments["manifest_path"] == manifest]), 2)

    def test_a_disposition_from_another_extractor_holds_the_unit_until_a_recheck_reads_the_pinned_one(self) -> None:
        world = self.world(("u1", "u2"))
        world.extractor_sha = "f" * 64
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u2")["state"] == "contract_held")
        for key in ("u1", "u2"):
            self.assert_contract_held(world, book, key, "disposition_from_another_extractor")
        self.assertEqual(len([name for name, _ in world.interactive.calls if name == "preflight"]), 2,
                         "each unit's preflight once: a hold is not rechecked before it is due")
        self.assertEqual([json.loads(row["detail_json"])["extractor_sha256"] for row in book.events("contract_broken")],
                         ["f" * 64] * 2)
        self.assertFalse(book.events("paused"))
        # Interactive runs the pinned extractor again: the next recheck, which nobody has to ask for, reads it.
        world.extractor_sha = fakes.SHA["extractor"]
        runner.run(until_idle=True, max_iterations=4000)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])
        self.assertFalse(book.events("resumed"))

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

    def test_held_raw_data_go_once_interactives_discard_accepts_them(self) -> None:
        """Plan item 14 lands: Interactive's discard now accepts a failed run that left an mzTab-M. The next
        runner start looks at the held raw data again and deletes them; the unit stays failed."""
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(runs=["invalid", "invalid", "invalid"])
        world.run()
        world.interactive.discard_blockers = lambda manifest: []
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"], unit["raw_disposition"]), ("failed", 3, "discarded"))
        self.assertFalse((Path(unit["workspace"]) / "raw").exists())
        self.assertEqual(self.boundaries(book, "u1")[-1], "5")
        self.assertTrue((Path(unit["workspace"]) / "output" / "result.mztab").is_file(), "the output is never touched")
        record = json.loads((Path(unit["workspace"]) / "campaign-record.json").read_text(encoding="utf-8"))
        self.assertEqual((record["state"], record["raw_disposition"]), ("failed", "discarded"))
        self.assertEqual(machine.summary(book)["raw_held"]["units"], 0)
        self.assertEqual(world.interactive.console_starts.count(("u1", "run")), 3, "not run again")

    def test_a_hold_that_clears_is_released_at_the_periodic_recheck(self) -> None:
        world = self.world(("u1", "u2"), policy_values={"prefetch": 1, "held_recheck_seconds": 3600.0})
        world.scripts["u1"] = fakes.UnitScript(cleanup="blocked")  # a finalisation hold that keeps refusing
        world.scripts["u2"] = fakes.UnitScript(ticks=300)  # u2 runs for hours after u1 has ended
        with world.open() as book:
            runner = world.runner(book)
            self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "done", limit=2000)
            self.assertEqual(book.unit("u1")["raw_disposition"], "held")
            world.scripts["u1"].cleanup = "ok"  # the hold clears
            runner.run(until_idle=True, max_iterations=5000)
            unit = book.unit("u1")
            self.assertEqual((unit["state"], unit["raw_disposition"]), ("done", "released"))
            released = [row["at"] for row in book.transitions("u1") if row["boundary"] == "5"][-1]
            u2_done = [row["at"] for row in book.transitions("u2") if row["to_state"] == "done"][0]
            self.assertLess(released, u2_done, "by the runner that ran on, not only at a start")

    def test_an_operator_releases_held_raw_data_without_running_the_unit_again(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(runs=["invalid", "invalid", "invalid"])
        with world.open() as book:
            runner = world.runner(book)
            runner.run(until_idle=True, max_iterations=2000)
            self.assertEqual(book.unit("u1")["raw_disposition"], "held")
            book.add_request("release_held", "u1", "item 14 is in", "Test Person", runner.stamp())
            book.add_request("release_held", "u2", "nothing there", "Test Person", runner.stamp())
            runner.iterate()
            first = [row["handled_detail"] for row in book.connection.execute("SELECT handled_detail FROM request ORDER BY request_id")]
            self.assertTrue(first[0].startswith("still held"), first)
            self.assertTrue(first[1].startswith("nothing held"), first)
            world.interactive.discard_blockers = lambda manifest: []
            book.add_request("release_held", "u1", "item 14 is in", "Test Person", runner.stamp())
            runner.iterate()
            detail = book.connection.execute("SELECT handled_detail FROM request ORDER BY request_id DESC").fetchone()[0]
            self.assertTrue(detail.startswith("released (discarded)"), detail)
            unit = book.unit("u1")
            self.assertEqual((unit["state"], unit["raw_disposition"]), ("failed", "discarded"))
            self.assertEqual((world.interactive.download_starts.count("u1"), world.interactive.console_starts.count(("u1", "run"))),
                             (1, 3))

    def test_a_split_parent_held_for_want_of_a_release_is_released_once_interactive_has_one(self) -> None:
        world = self.world(("u1",))
        world.scripts["u1"] = fakes.UnitScript(disposition="split")
        world.run()
        world.interactive.split_release_supported = True
        book = self.finish(world)
        parent = book.unit("u1")
        self.assertEqual((parent["state"], parent["raw_disposition"]), ("split_done", "released"))
        self.assertEqual(self.boundaries(book, "u1")[-1], "5")

    def test_held_raw_data_no_longer_fill_the_disk_for_good(self) -> None:
        """Each raw tree takes 510 GB of a volume with a 1 TB reserve: u1's held raw data leave u2 no room.
        Once Interactive can delete them, they go, and u2 and u3 run."""
        world = self.world(("u1", "u2", "u3"))

        class RawDisk(fakes.FakeDisk):
            def usage(self_inner, _path):
                present = [path for path in world.workspace_root.rglob("raw") if path.is_dir() and any(path.rglob("*.mzML"))]
                return 1520 * GB - 510 * GB * len(present), 20 * TB

        world.disk = RawDisk()
        world.scripts["u1"] = fakes.UnitScript(runs=["invalid", "invalid", "invalid"])
        book = self.finish(world, max_iterations=4000)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2", "u3")], ["failed", "deferred_disk", "pending"])
        self.assertEqual(book.runner()["pause_kind"], "disk")
        book.close()
        world.interactive.discard_blockers = lambda manifest: []
        book = self.finish(world, max_iterations=4000)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2", "u3")], ["failed", "done", "done"])
        self.assertEqual(book.unit("u1")["raw_disposition"], "discarded")


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

    def test_a_retry_is_given_credit_for_the_partial_download_it_holds(self) -> None:
        """u1 (1.2 TB) fits a 4 TB volume and fails after 1.1 TB arrived, kept for the resume. The retry needs
        what is still to come, not its whole need on top of the bytes already there."""
        world = self.world(("u1", "u2", "u3"), known_bytes=int(1.2 * TB))
        world.disk = PartialDisk(world, free=int(3.5 * TB), total=4 * TB, sizes={"u1": int(1.1 * TB)})
        world.scripts["u1"] = fakes.UnitScript(downloads=["fail", "ok"])
        with world.open() as book:
            book.connection.execute("UPDATE unit SET known_bytes = ? WHERE unit_key IN ('u2', 'u3')", (10 * GB,))
        book = self.finish(world, max_iterations=5000)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2", "u3")], ["done"] * 3)
        self.assertEqual((book.unit("u1")["failures"], world.interactive.download_starts.count("u1")), (1, 2))
        self.assertIsNone(book.runner()["pause_kind"])

    def test_a_unit_stopped_at_the_floor_counts_what_arrived_and_the_campaign_goes_idle(self) -> None:
        """A download of unknown size outgrows the volume and is stopped at the free-space floor. Its size is
        at least what arrived; it does not fit beside what fills the volume, nothing left in the campaign
        frees space, so the disk pause stands and run --until-idle returns. Space freed, it resumes."""
        world = self.world(("u1", "u2", "u3"), size_known=False, known_bytes=0)
        world.disk = PartialDisk(world, free=800 * GB, total=4 * TB, sizes={"u1": 750 * GB})
        world.scripts["u1"] = fakes.UnitScript(downloads=["hold", "ok"])  # bytes keep coming until cancelled
        with world.open() as book:
            book.connection.execute("UPDATE unit SET known_bytes = ?, size_known = 1 WHERE unit_key IN ('u2', 'u3')", (5 * GB,))
            book.connection.commit()
            runner = world.runner(book)
            runner.run(until_idle=True, max_iterations=3000)
            hours = (world.clock.now() - fakes.FakeClock().now()).total_seconds() / 3600
            unit = book.unit("u1")
            self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2", "u3")], ["deferred_disk", "pending", "pending"])
            self.assertEqual((unit["known_bytes"], unit["failures"]), (750 * GB, 0))
            verdict = runner._disk_verdict(unit)
            self.assertEqual((verdict.held, verdict.need), (750 * GB, int(750 * GB * 1.66) + 200 * GB))
            self.assertEqual(book.runner()["pause_kind"], "disk")
            self.assertTrue(runner.idle())
            self.assertLess(hours, 2, "run --until-idle returned")
        world.disk.free += 2 * TB  # an operator frees space
        book = self.finish(world, max_iterations=5000)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2", "u3")], ["done"] * 3)
        self.assertEqual(world.interactive.download_starts.count("u1"), 2)

    def test_a_volume_that_runs_short_as_an_archive_expands_defers_the_unit(self) -> None:
        """Interactive's extraction guard (a 20 GB reserve of its own) ends the lease with ArchiveError
        insufficient_disk_space once the fetch has completed, when the free-space floor no longer watches it.
        That is a short disk: the unit waits, uncounted, for room for the expansion, and the campaign pauses."""
        for message, recorded, declared in (
            ("ST1.zip expands to 4000000000000 bytes; 3900000000000 are free and 20000000000 are held in reserve.",
             {"declared_bytes": 4 * TB, "free_bytes": int(3.9 * TB)}, 4 * TB),
            ("Free space fell below the 20000000000-byte reserve while ST1.tar.gz expanded.", {}, 0),
            ("7-Zip was stopped while extracting ST1.7z: insufficient_disk_space.", {"elapsed_seconds": 5.0}, 0),
        ):
            with self.subTest(message=message):
                world = self.world(("u1", "u2"))
                with world.open() as book:
                    book.connection.execute("UPDATE unit SET has_archive = 1")
                world.scripts["u1"] = fakes.UnitScript(downloads=["corrupt", "ok"])
                finish = world.interactive._finish

                def short(job, outcome, finish=finish, message=message, recorded=recorded, world=world):
                    finish(job, outcome)
                    if job["kind"] == "download" and job["unit"] == "u1" and outcome == "corrupt":
                        # As Interactive records it: str(error) in the job, the ArchiveError's record in the manifest.
                        job["error"] = message
                        world.interactive._update(job["manifest_path"], lambda manifest: manifest.update(download_failure={
                            "reason": message, "error_type": "ArchiveError", "stage": "extract", "archive_failure": {
                                "reason": "insufficient_disk_space", "message": message, "rejected_members": [],
                                "detail": recorded}}))

                world.interactive._finish = short
                with world.open() as book:
                    world.runner(book).run(max_iterations=200)
                    unit = book.unit("u1")
                    self.assertEqual((unit["state"], unit["failures"]), ("deferred_disk", 0))
                    self.assertGreaterEqual(unit["known_bytes"], declared)
                    self.assertEqual([(row["outcome"], row["counted"]) for row in book.attempts("u1") if row["step"] == "download"],
                                     [("blocked", 0)])
                    self.assertEqual(book.runner()["pause_kind"], "disk")
                    self.assertTrue((Path(unit["workspace"]) / "raw").is_dir(), "its raw data stay for the resume")
                world.disk.free = 19 * TB
                book = self.finish(world)
                self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])
                self.assertEqual(book.unit("u1")["failures"], 0)


class PartialDisk(fakes.FakeDisk):
    """A volume on which each named unit's download takes `sizes[unit]` bytes from the start of its lease
    until its raw tree goes: what arrived, kept in .part files for the resume when the lease fails."""

    def __init__(self, world: fakes.World, free: int, total: int, sizes: dict[str, int]) -> None:
        super().__init__(free, total)
        self.world = world
        self.sizes = sizes

    def _taken(self, unit_key: str) -> int:
        raw = None
        for job in self.world.interactive.jobs.values():
            if job.get("unit") == unit_key and job["kind"] == "download":
                if job["status"] in ("queued", "running"):
                    return self.sizes[unit_key]
                raw = Path(job["manifest_path"]).parent.parent / "raw"
        return self.sizes[unit_key] if raw is not None and raw.is_dir() else 0

    def usage(self, _path: str) -> tuple[int, int]:
        return self.free - sum(self._taken(key) for key in self.sizes), self.total

    def tree_bytes(self, path: str) -> int:
        location = Path(path)
        if location.name == "raw" and location.parent.name in self.sizes:
            return self._taken(location.parent.name)
        return super().tree_bytes(path)


class EndStepErrorTests(Base):
    """An error in a step of a unit's end. Interactive's deletion and the Catalog's run record raise it here: a
    gate that raises after the run is no error, its missing report only recorded (2026-10-02)."""

    def test_an_error_ending_a_unit_is_retried_later_without_counting(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(downloads=["fail", "fail", "fail"])
        broken = {"now": True}
        original = world.interactive.discard

        def discard(**arguments):
            if arguments["unit_id"] == "u1" and broken["now"]:
                raise OSError("the raw tree is held by another process")
            return original(**arguments)

        world.interactive.discard = discard
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
        original = world.interactive.discard

        def discard(**arguments):
            if left["errors"] > 0:
                left["errors"] -= 1
                raise OSError("transient")
            return original(**arguments)

        world.interactive.discard = discard
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
        original = world.catalog.record_run

        def record_run(**values):
            # The unit's end completes its last run's record with the final gate's verdict.
            if "gate_verdict" in values and left["errors"] > 0:
                left["errors"] -= 1
                raise PermissionError("the file is held by another process")
            return original(**values)

        world.catalog.record_run = record_run
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

    def test_a_backend_that_does_not_answer_is_the_fourth_pause_and_lifts_by_itself(self) -> None:
        """The user's default of 2026-10-03: an Interactive backend that does not answer pauses the whole campaign,
        named as such in the status export, and the pause lifts by itself at the fault recheck. A poll that times
        out is that silence, never the unit's failure or a job given up for lost."""
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(ticks=40)
        silent = {"now": False}
        original = world.interactive.job
        world.interactive.job = lambda job_id: (
            {"ok": False, "reason": "os_error", "error_type": "TimeoutError", "detail": "timed out"}
            if silent["now"] else original(job_id)
        )
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] in machine.IN_FLIGHT)
        silent["now"] = True
        self.step_until(world, book, runner, lambda: bool(book.runner()["paused"]))
        self.assertEqual(book.runner()["pause_kind"], "backend")
        summary = machine.export_status(book)[0]["summary"]
        self.assertEqual(summary["paused"]["name"], "an Interactive backend that does not answer")
        self.assertTrue(summary["paused"]["lifts"].startswith("by itself, at the fault recheck"))
        self.assertEqual(sorted(summary["campaign_pauses"]), ["backend", "disk", "outage", "pin"])
        self.assertEqual([row for row in book.attempts() if row["counted"]], [])
        world.clock.sleep(1800)
        runner.iterate()
        self.assertEqual(book.runner()["pause_kind"], "backend", "it waits for the recheck")
        silent["now"] = False
        runner.run(until_idle=True, max_iterations=4000)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])
        self.assertEqual([(book.unit(key)["failures"], book.unit(key)["interruptions"]) for key in ("u1", "u2")], [(0, 0)] * 2)
        resumed = [json.loads(row["detail_json"]) for row in book.events("resumed")]
        self.assertEqual([item["was"]["pause_kind"] for item in resumed], ["backend"])
        self.assertIsNone(machine.summary(book)["paused"])

    def test_an_estimate_the_backend_does_not_answer_is_the_pause_not_the_units_failure(self) -> None:
        """The estimate after a completed diagnostic is a call to the backend like any other: one that times out or
        is refused pauses the campaign and counts nothing (the user's default of 2026-10-03), nothing more is asked
        of the backend until the fault recheck, and the same diagnostic's estimate is asked for again then. Read as
        estimate_unavailable before, it was counted, and after three the unit failed and its raw data went."""
        for name, silence in SILENCES.items():
            with self.subTest(silence=name):
                world = self.world(("u1", "u2"))
                original = world.interactive.estimate
                silent = {"now": True}
                asked = []

                def estimate(world=world, original=original, silent=silent, asked=asked, silence=silence, **arguments):
                    if world.interactive._unit_of_manifest(arguments["manifest_path"]) == "u1":
                        asked.append(arguments["step"])
                        if silent["now"]:
                            return dict(silence)
                    return original(**arguments)

                world.interactive.estimate = estimate
                book = world.open()
                self.addCleanup(book.close)
                runner = world.runner(book)
                self.step_until(world, book, runner, lambda: bool(book.runner()["paused"]) or book.unit("u1")["failures"] > 0)
                unit = book.unit("u1")
                self.assertEqual((unit["state"], unit["failures"], unit["interruptions"]), ("diagnosing", 0, 0))
                self.assertEqual(book.runner()["pause_kind"], "backend")
                self.assertEqual([row for row in book.attempts() if row["counted"]], [])
                self.assertEqual([row["outcome"] for row in book.console_runs("u1")], ["completed"], "the diagnostic ended")
                self.assertIsNone(book.slot())
                for _ in range(40):  # twenty minutes of the pause, short of the hourly recheck
                    runner.iterate()
                    world.clock.sleep(30)
                self.assertEqual(len(asked), 1, "the estimate waits for the recheck")
                silent["now"] = False
                runner.run(until_idle=True, max_iterations=4000)
                self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])
                self.assertEqual((book.unit("u1")["failures"], book.unit("u1")["interruptions"]), (0, 0))
                self.assertEqual(world.interactive.console_starts.count(("u1", "diagnostic")), 1, "the same diagnostic")
                resumed = [json.loads(row["detail_json"]) for row in book.events("resumed")]
                self.assertEqual([item["was"]["pause_kind"] for item in resumed], ["backend"])

    def test_an_estimate_the_backend_does_not_answer_for_a_job_it_forgot_is_the_pause_not_an_interruption(self) -> None:
        """The backend no longer knows the diagnostic's job, so its output is asked for the estimate, and that call
        times out: the campaign pauses. Read as a lost job before, the diagnostic was run again as interrupted."""
        world = self.world(("u1", "u2"))
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "diagnosing")
        world.clock.sleep(300)
        world.interactive.settle()
        diagnostic = book.unit("u1")["diagnostic_job_id"]
        self.assertEqual(world.interactive.jobs[diagnostic]["status"], "completed")
        original_job, original_estimate = world.interactive.job, world.interactive.estimate
        silent = {"now": True}
        world.interactive.job = lambda job_id: (
            {"ok": False, "reason": "job_not_found", "http_status": 404} if job_id == diagnostic else original_job(job_id))
        world.interactive.estimate = lambda **arguments: (
            dict(SILENCES["timeout"]) if silent["now"] and arguments["job_id"] == diagnostic else original_estimate(**arguments))
        self.step_until(world, book, runner, lambda: bool(book.runner()["paused"]) or book.unit("u1")["interruptions"] > 0)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"], unit["interruptions"]), ("diagnosing", 0, 0))
        self.assertEqual(book.runner()["pause_kind"], "backend")
        silent["now"] = False
        runner.run(until_idle=True, max_iterations=4000)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])
        self.assertEqual((book.unit("u1")["failures"], book.unit("u1")["interruptions"]), (0, 0))
        self.assertEqual(world.interactive.console_starts.count(("u1", "diagnostic")), 1, "the forgotten job's own estimate")


class OutageTests(Base):
    def test_a_repository_outage_pauses_the_campaign_instead_of_failing_its_units(self) -> None:
        units = [f"u{index:02d}" for index in range(20)]
        world = self.world(units)
        world.outage = True
        with world.open() as book:
            world.runner(book).run(max_iterations=3000)
            self.assertEqual([key for key in units if book.unit(key)["state"] == "failed"], [])
            self.assertEqual(book.runner()["pause_kind"], "outage")
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

    def test_one_download_groups_failing_object_is_not_an_outage(self) -> None:
        """The units of one download group share objects: a 503 on one shared study archive is that object's
        failure, however many units it serves, and the units after the group run."""
        world = self.world(("u1", "u2", "u3", "u4", "u5"))
        for key in ("u1", "u2", "u3"):
            world.scripts[key] = fakes.UnitScript(downloads=["fail"])
        with world.open() as book:
            book.connection.execute("UPDATE unit SET download_group_id = 'group-0' WHERE unit_key IN ('u1', 'u2', 'u3')")
        book = self.finish(world, max_iterations=5000)
        self.assertEqual([(book.unit(key)["state"], book.unit(key)["failures"]) for key in ("u1", "u2", "u3")],
                         [("failed", 3)] * 3)
        self.assertEqual([book.unit(key)["state"] for key in ("u4", "u5")], ["done", "done"])
        self.assertEqual(book.events("paused"), [])

    def test_a_probe_of_another_group_tells_failing_groups_from_an_outage(self) -> None:
        """Three groups whose own objects keep failing look like an outage once. The recheck tries a unit of
        another group first; its download finishes, so the three are failures of their own and end, and
        the campaign reaches its end instead of re-pausing on the same three units for ever."""
        world = self.world(("u1", "u2", "u3", "u4", "u5"))
        for key in ("u1", "u2", "u3"):
            world.scripts[key] = fakes.UnitScript(downloads=["fail"])
        book = self.finish(world, max_iterations=20000)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2", "u3", "u4", "u5")], ["failed"] * 3 + ["done"] * 2)
        self.assertEqual([book.unit(key)["failures"] for key in ("u1", "u2", "u3")], [3, 3, 3])
        self.assertIsNone(book.runner()["pause_kind"])
        first_trip = min(row["attempt_id"] for row in book.attempts() if row["outcome"] == "fault")
        after = [row["unit_key"] for row in book.attempts() if row["attempt_id"] > first_trip and row["step"] == "download_start"]
        self.assertEqual(after[0], "u4", "the recheck probes a unit of another group first")
        self.assertEqual(len(book.events("paused")), 1, "failures that outlived u4's download are the groups' own")
        self.assertEqual([book.unit(key)["interruptions"] for key in ("u1", "u2", "u3")], [0, 0, 1])

    def test_an_outage_of_one_repository_lets_the_others_units_run(self) -> None:
        units = [f"u{index}" for index in range(1, 9)]
        world = self.world(units)
        with world.open() as book:
            book.connection.execute("UPDATE unit SET repository = 'metabolomics_workbench' WHERE unit_key IN ('u2', 'u4', 'u6', 'u8')")
        world.outage_repositories = {"metabolights"}
        with world.open() as book:
            world.runner(book).run(max_iterations=1200)  # about ten hours
            states = {key: book.unit(key)["state"] for key in units}
            self.assertEqual([states[key] for key in ("u2", "u4", "u6", "u8")], ["done"] * 4,
                             "a download that finishes elsewhere ends no streak of this repository, and runs")
            self.assertEqual([key for key in units if states[key] == "failed"], [])
            self.assertLessEqual(sum(book.unit(key)["failures"] for key in units), 2)
            interruptions = [book.unit(key)["interruptions"] for key in ("u1", "u3", "u5", "u7")]
            self.assertGreaterEqual(min(interruptions), 1, "every unit of the outage takes its turn")
            self.assertLessEqual(max(interruptions) - min(interruptions), 1, f"in turn: {interruptions}")
        world.outage_repositories = set()
        book = self.finish(world, max_iterations=20000)
        self.assertEqual({book.unit(key)["state"] for key in units}, {"done"})

    def test_what_the_lease_wrote_into_the_manifest_tells_an_outage(self) -> None:
        """Interactive 0.5.18 retries a stalled or lost transfer and then fails the job with str(error) as its
        error; the type and retryable are in the manifest's download_failure only. Read from there, a
        repository whose transfers all hang is an outage, not eight failed units."""
        units = [f"u{index:02d}" for index in range(8)]
        world = self.world(units)
        world.outage = True
        world.network_error = {"job": "The transfer was interrupted after three attempts.",
                               "failure": {"reason": "The transfer was interrupted after three attempts.",
                                           "error_type": "DownloadInterrupted", "retryable": True}}
        with world.open() as book:
            world.runner(book).run(max_iterations=2000)
            self.assertEqual([key for key in units if book.unit(key)["state"] == "failed"], [])
            self.assertEqual(book.runner()["pause_kind"], "outage")
            fault = next(row for row in book.attempts() if row["outcome"] == "fault")
            self.assertEqual(json.loads(fault["detail_json"])["failure"]["error_type"], "DownloadInterrupted")


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

    def test_the_gates_run_policy_adds_to_the_users_list_and_never_takes_from_it(self) -> None:
        for fails, rules, blocks, mismatch in (
            (["ORD-1"], {"ORD-1": policy.RECORD_ONLY, "ELIG-1": policy.BLOCKS_RUN}, False, False),
            (["CLS-1"], {"CLS-1": policy.BLOCKS_RUN}, True, False),
            # The user's rule stops the run on a SUM-1 FAIL whatever the gate states, and the disagreement is said.
            (["SUM-1"], {"SUM-1": policy.RECORD_ONLY}, True, True),
            (["CLS-2"], {"CLS-2": "BLOCKS-RUN"}, True, True),
        ):
            with self.subTest(fails=fails, rules=rules):
                world = self.world()
                world.gate.fails["before_production"] = fails
                world.gate.run_policy = rules
                book = self.finish(world)
                self.assertEqual(book.unit("u1")["state"], "failed" if blocks else "done")
                self.assertEqual(world.interactive.console_starts.count(("u1", "run")), 0 if blocks else 1)
                verdict = [row for row in book.gate_verdicts("u1") if row["point"] == "before_production"][0]
                self.assertEqual(verdict["run_policy_source"], "gate")
                self.assertEqual(bool(book.events("run_policy_mismatch")), mismatch)

    def test_the_checks_placed_on_2026_10_02_fail_the_unit_and_spl1_does_not(self) -> None:
        for check, blocks in (("ID-1", True), ("PRE-2", True), ("CONV-1", True), ("SPL-1", False)):
            with self.subTest(check=check):
                world = self.world(("u1", "u2"))
                world.gate.fails[("u1", "before_production")] = [check]
                book = self.finish(world)
                unit = book.unit("u1")
                if blocks:
                    self.assertEqual((unit["state"], unit["failures"], unit["raw_disposition"]), ("failed", 3, "discarded"))
                    self.assertEqual(self.production_starts(world, "u1"), 0)
                else:
                    self.assertEqual((unit["state"], unit["failures"]), ("done", 0))
                    self.assertEqual(self.production_starts(world, "u1"), 1)
                self.assertEqual(book.unit("u2")["state"], "done", "the stop is the unit's, never the runner's")

    def test_a_blocking_check_left_unevaluated_where_required_fails_the_unit_as_a_fail_does(self) -> None:
        world = self.world(("u1", "u2"))
        world.gate.unevaluated[("u1", "before_production")] = ["SUM-1"]
        world.gate.unevaluated[("u2", "before_production")] = ["PKH-1"]
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"], unit["raw_disposition"]), ("failed", 3, "discarded"))
        self.assertEqual(self.production_starts(world, "u1"), 0)
        verdicts = [row for row in book.gate_verdicts("u1") if row["point"] == "before_production"]
        self.assertEqual({row["blocking_unevaluated_ids_json"] for row in verdicts}, {'["SUM-1"]'})
        self.assertEqual({row["blocking_fail_ids_json"] for row in verdicts}, {"[]"})
        counted = [json.loads(row["detail_json"]) for row in book.attempts("u1") if row["counted"]]
        self.assertEqual([item["blocking_unevaluated_ids"] for item in counted], [["SUM-1"]] * 3)
        record = json.loads((Path(unit["workspace"]) / "campaign-record.json").read_text(encoding="utf-8"))
        self.assertIn(["SUM-1"], [item["blocking_unevaluated_ids"] for item in record["gate_verdicts"]])
        self.assertEqual((book.unit("u2")["state"], book.unit("u2")["failures"]), ("done", 0),
                         "a record_only check left unevaluated stops nothing")


class GateHoldTests(Base):
    """Before production, a gate that gives no usable report holds the unit (the user's rule of 2026-10-02): it
    is not run, its raw data are kept, it is not counted as a retry or a failure, it is warned about in the
    ledger and the status export, and the other units go on. A recheck runs the gate again."""

    ENDINGS = {"timeout": policy.GATE_TIMEOUT, "error": policy.GATE_ERROR, "exit_3": policy.GATE_UNUSABLE,
               "unparsable": policy.GATE_UNPARSABLE, "raise": policy.GATE_NOT_RUN}

    def held(self, ending: str = "timeout", units=("u1", "u2")):
        """u1 held at its before-production gate, and every other unit done. A held unit is not idle, so the
        runner is stepped to that point rather than run --until-idle, which would stay for the recheck."""
        world = self.world(units)
        world.gate.no_report[("u1", "before_production")] = ending
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        others = [key for key in units if key != "u1"]
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "gate_held"
                        and all(book.unit(key)["state"] == "done" for key in others))
        return world, book, runner

    def assert_held(self, world: fakes.World, book: ledger.Ledger, problem: str) -> None:
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["resume_state"]), ("gate_held", "diagnosed"))
        self.assertEqual((unit["failures"], unit["interruptions"]), (0, 0), "neither a retry nor a failure")
        self.assertEqual(unit["raw_disposition"], "present")
        self.assertTrue((Path(unit["workspace"]) / "raw").is_dir(), "the raw data are kept")
        self.assertEqual(world.interactive.console_starts.count(("u1", "run")), 0, "the unit is not run")
        self.assertFalse([row for row in book.attempts("u1") if row["counted"]])
        # By the unit's manifest: a temporary directory's random name can hold "u1" too.
        self.assertFalse([name for name, arguments in world.interactive.calls
                          if name in ("discard", "cleanup") and arguments.get("manifest_path") == unit["manifest_path"]])
        events = [json.loads(row["detail_json"]) for row in book.events("gate_held") if row["unit_key"] == "u1"]
        self.assertTrue(events and events[-1]["no_report"] == problem, events)
        self.assertIn("held", events[-1]["warning"])
        document, tsv = machine.export_status(book)
        row = next(item for item in document["units"] if item["unit_key"] == "u1")
        self.assertEqual(len(row["warnings"]), 1)
        self.assertIn(f"no report: {problem}", row["warnings"][0])
        line = next(line for line in tsv.splitlines() if line.split("\t")[0] == "u1")
        self.assertIn("raw data are kept", line.split("\t")[machine.TSV_COLUMNS.index("warnings")])
        self.assertEqual(document["summary"]["gate_held"]["unit_keys"], ["u1"])
        self.assertEqual(book.unit("u2")["state"], "done", "the other units go on")

    def test_every_gate_ending_without_a_usable_report_holds_the_unit(self) -> None:
        for ending, problem in self.ENDINGS.items():
            with self.subTest(ending=ending):
                world, book, _runner = self.held(ending)
                self.assert_held(world, book, problem)
                attempt = next(row for row in book.attempts("u1") if row["step"] == "prepare_run")
                self.assertEqual((attempt["outcome"], attempt["counted"]), ("fault", 0))
                self.assertFalse(book.runner()["paused"], "the campaign is not paused")

    def test_a_gate_the_runner_never_started_holds_the_unit(self) -> None:
        """_gate giving no verdict at all for before_production, as for a gate point the policy left out."""
        world = self.world(("u1", "u2"))
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        original = runner._gate

        def gate(unit, point):
            return None if (unit["unit_key"], point) == ("u1", "before_production") else original(unit, point)

        runner._gate = gate
        self.step_until(world, book, runner, lambda: book.unit("u2")["state"] == "done")
        self.assert_held(world, book, policy.GATE_NOT_RUN)

    def test_a_held_unit_is_rechecked_when_the_runner_starts_again(self) -> None:
        world, book, _runner = self.held()
        world.gate.no_report.clear()
        runner = world.runner(book)  # the next start
        runner.run(until_idle=True, max_iterations=2000)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"], unit["raw_disposition"]), ("done", 0, "released"))
        starts = [json.loads(row["detail_json"]).get("gate_recheck") for row in book.transitions("u1")]
        self.assertIn("runner_start", starts)
        verdicts = [row["outcome"] for row in book.gate_verdicts("u1") if row["point"] == "before_production"]
        self.assertEqual(verdicts, ["timeout", "ran"])
        self.assertEqual(machine.summary(book)["gate_held"]["units"], 0)

    def test_a_held_unit_is_rechecked_every_few_hours_and_held_again_while_no_report_comes(self) -> None:
        world, book, runner = self.held()
        runs = world.gate.runs.count(("u1", "before_production"))
        due = policy.parse_iso(book.unit("u1")["next_attempt_at"])
        runner.run(until_idle=True, max_iterations=50)
        self.assertEqual(world.gate.runs.count(("u1", "before_production")), runs, "nothing is rechecked before it is due")
        self.assertFalse(runner.idle(), "a held unit is not idle: its recheck comes by itself")
        self.step_until(world, book, runner, lambda: world.gate.runs.count(("u1", "before_production")) == runs + 1,
                        limit=1000)
        self.assertGreaterEqual(world.clock.now(), due, "the recheck came when it was due, by itself")
        self.assert_held(world, book, policy.GATE_TIMEOUT)
        world.gate.no_report.clear()
        runner.run(until_idle=True, max_iterations=2000)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 0))
        self.assertTrue(runner.idle())

    def test_run_until_idle_stays_for_a_held_unit_and_rechecks_it_by_itself(self) -> None:
        """With a held unit counted idle, run --until-idle, the scheduled task's command, returned once only held
        units remained, and the recheck every few hours waited for the next logon."""
        world = self.world(("u1", "u2"))
        world.gate.no_report[("u1", "before_production")] = "timeout"
        original = world.gate.run

        def gate(workspace, point, report_path):
            verdict = original(workspace, point, report_path)
            if (Path(workspace).name, point) == ("u1", "before_production"):
                world.gate.no_report.clear()  # the gate's next run gives a report
            return verdict

        world.gate.run = gate
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        started = world.clock.now()
        runner.run(until_idle=True, max_iterations=5000)
        self.assertTrue(runner.idle())
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])
        self.assertEqual([row["outcome"] for row in book.gate_verdicts("u1") if row["point"] == "before_production"],
                         ["timeout", "ran"])
        self.assertGreaterEqual((world.clock.now() - started).total_seconds(), runner.policy.held_recheck_seconds)
        self.assertEqual(book.unit("u1")["failures"], 0)

    def test_a_held_split_part_keeps_the_runner_as_any_held_unit_does(self) -> None:
        """A held split part leaves its parent in split_parent, which was never idle, while an ordinary held unit
        was: run --until-idle then returned for one and never for the other. One rule now holds for both: the
        runner stays, polling, rechecks every held_recheck_seconds, and returns once the part has ended."""
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(disposition="split")
        world.gate.no_report[("u1-DDA", "before_production")] = "timeout"
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        def states() -> dict[str, str]:
            return {unit["unit_key"]: unit["state"] for unit in book.units()}

        self.step_until(world, book, runner, lambda: {key: states().get(key) for key in ("u1-DDA", "u1-SWATH", "u2")}
                        == {"u1-DDA": "gate_held", "u1-SWATH": "done", "u2": "done"}, limit=1000)
        self.assertEqual(book.unit("u1")["state"], "split_parent")
        self.assertFalse(runner.idle())
        held_at = world.clock.now()
        runner.run(until_idle=True, max_iterations=3000)
        self.assertEqual(book.unit("u1-DDA")["state"], "gate_held", "still held: the gate still gives no report")
        elapsed = (world.clock.now() - held_at).total_seconds()
        rechecks = world.gate.runs.count(("u1-DDA", "before_production")) - 1
        self.assertGreaterEqual(rechecks, 2, "rechecked by itself")
        self.assertLessEqual(rechecks, elapsed // runner.policy.held_recheck_seconds, "never sooner than due")
        world.gate.no_report.clear()
        runner.run(until_idle=True, max_iterations=2000)
        self.assertTrue(runner.idle())
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u1-DDA", "u1-SWATH", "u2")],
                         ["split_done", "done", "done", "done"])

    def test_an_operator_recheck_runs_the_gate_again_now(self) -> None:
        world, book, runner = self.held()
        world.gate.no_report.clear()
        book.add_request("recheck_held", "u2", "not held", "Test Person", runner.stamp())
        book.add_request("recheck_held", "u1", "the gate is mended", "Test Person", runner.stamp())
        asked = world.clock.now()
        runner.run(until_idle=True, max_iterations=2000)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 0))
        self.assertLess((world.clock.now() - asked).total_seconds(), runner.policy.held_recheck_seconds, "now, not when due")
        handled = [row[0] for row in book.connection.execute("SELECT handled_detail FROM request ORDER BY request_id")]
        self.assertEqual(handled, ["nothing held for a recheck: the unit is done", "gate recheck brought forward"])

    def test_the_recheck_held_command_asks_for_every_held_unit(self) -> None:
        world = self.world(("u1", "u2", "u3"))
        world.gate.no_report.update({("u1", "before_production"): "timeout", ("u2", "before_production"): "exit_3"})
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: [book.unit(key)["state"] for key in ("u1", "u2", "u3")]
                        == ["gate_held", "gate_held", "done"])
        spec = importlib.util.spec_from_file_location("campaign_runner_cli", TESTS.parent / "scripts" / "campaign-runner.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            code = cli.main(["--workspace-root", str(world.workspace_root), "recheck-held", "--campaign", "test-campaign"])
        self.assertEqual(code, 0)
        self.assertIn("recheck_held u2", printed.getvalue())
        # A request is read only by a runner's loop: with none running, the command says one must be started.
        self.assertIn("no runner is running, so nothing acts on it until one is started", printed.getvalue())
        self.assertNotIn("acts on it at its next step", printed.getvalue())
        self.assertEqual([(row["action"], row["unit_key"]) for row in book.pending_requests()],
                         [("recheck_held", "u1"), ("recheck_held", "u2")])
        taken, _holder = book.take_lock(4242, 1.0, "another-host", policy.iso(datetime.now(timezone.utc)),
                                        holder_alive=lambda record: False)
        self.assertTrue(taken)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            code = cli.main(["--workspace-root", str(world.workspace_root), "recheck-held", "--campaign", "test-campaign",
                             "--unit", "u1"])
        self.assertEqual(code, 0)
        self.assertIn("runner pid 4242 on another-host acts on it at its next step", printed.getvalue())
        book.release_lock(4242, policy.iso(datetime.now(timezone.utc)))
        world.gate.no_report.clear()
        runner.run(until_idle=True, max_iterations=2000)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2", "u3")], ["done"] * 3)

    def test_a_missing_report_after_the_run_is_only_recorded(self) -> None:
        world = self.world(("u1",))
        world.gate.no_report.update(pre_cleanup="timeout", final="raise")
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["terminal_reason"], unit["failures"]), ("done", "outputs_produced", 0))
        self.assertEqual(unit["raw_disposition"], "released", "the deletion rule holds whatever the gate says")
        self.assertIsNone(unit["gate_exit_pre"])
        events = [json.loads(row["detail_json"]) for row in book.events("gate_no_report")]
        self.assertEqual([(item["point"], item["no_report"]) for item in events],
                         [("pre_cleanup", policy.GATE_TIMEOUT), ("final", policy.GATE_NOT_RUN)])
        self.assertEqual([row["point"] for row in book.gate_verdicts("u1")], ["before_production", "pre_cleanup", "final"])
        self.assertFalse(book.events("gate_held"))


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

    def test_a_disposition_nothing_applies_holds_the_unit(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(applied=False, classify_applies=False)
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u2")["state"] == "done")
        self.assert_contract_held(world, book, "u1", "disposition_not_applied")
        self.assertFalse([start for start in world.interactive.console_starts if start[0] == "u1"])

    def test_the_preflight_is_given_the_approval(self) -> None:
        world = self.world()
        self.finish(world)
        preflight = next(arguments for name, arguments in world.interactive.calls if name == "preflight")
        self.assertTrue(preflight["authorization_path"].endswith(str(Path("u1") / "provenance" / "campaign-authorization.json")))

    def test_an_extractor_interactive_refuses_holds_each_unit_instead_of_failing_it(self) -> None:
        """A refusal every unit meets alike holds each unit in turn, with its raw data, and pauses nothing: no
        raw data are deleted for a fault that is not the unit's, and the disk guard is what stops new
        downloads should it last (DiskTests)."""
        world = self.world(("u1", "u2"))
        world.extractor_refused = True
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u2")["state"] == "contract_held")
        for key in ("u1", "u2"):
            self.assert_contract_held(world, book, key, "raw_metadata_extractor_refused")
            attempt = next(row for row in book.attempts(key) if row["step"] == "preflight")
            self.assertEqual((attempt["outcome"], attempt["counted"]), ("fault", 0))
        self.assertEqual(len([name for name, _ in world.interactive.calls if name == "preflight"]), 2)
        # Mended, and an operator asks for the recheck at once rather than wait for it.
        world.extractor_refused = False
        for key in ("u1", "u2"):
            book.add_request("recheck_held", key, "the extractor is accepted again", "Test Person", runner.stamp())
        asked = world.clock.now()
        runner.run(until_idle=True, max_iterations=2000)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])
        self.assertLess((world.clock.now() - asked).total_seconds(), runner.policy.held_recheck_seconds)
        handled = [row[0] for row in book.connection.execute("SELECT handled_detail FROM request ORDER BY request_id")]
        self.assertEqual(handled, ["contract recheck brought forward"] * 2)

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
    """A deletion this Interactive cannot make as called leaves that unit's raw data held, looked at again at every
    start and every few hours, and the campaign goes on: one unit's deletion pauses nothing ("stop" is per unit,
    2026-10-02), where it used to pause the whole campaign until an operator resumed it."""

    def test_a_cleanup_this_interactive_cannot_make_holds_the_raw_data_and_the_campaign_goes_on(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(cleanup="unsupported")
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["terminal_reason"], unit["failures"]), ("done", "outputs_produced", 0))
        self.assertEqual(unit["raw_disposition"], "held", "deleted by the rules, not by this Interactive: held")
        self.assertIn("unsupported", unit["raw_detail"])
        self.assertTrue((Path(unit["workspace"]) / "raw").is_dir())
        self.assertFalse(book.events("paused"))
        self.assertEqual(book.unit("u2")["state"], "done")
        self.assertEqual(machine.summary(book)["raw_held"]["unit_keys"], ["u1"])
        # An Interactive whose cleanup takes the approval: the next runner start deletes them.
        world.scripts["u1"].cleanup = "ok"
        book.close()
        book = self.finish(world)
        self.assertEqual(book.unit("u1")["raw_disposition"], "released")
        self.assertFalse((Path(unit["workspace"]) / "raw").exists())

    def test_a_discard_this_interactive_cannot_make_holds_the_raw_data_and_the_unit_ends(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(disposition="skip")
        original = world.interactive.discard
        supported = {"now": False}

        def discard(**arguments):
            if not supported["now"]:
                return {"ok": False, "reason": "unsupported", "detail": "discard_download_lease takes no campaign approval"}
            return original(**arguments)

        world.interactive.discard = discard
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["raw_disposition"], unit["failures"]), ("skipped", "held", 0))
        self.assertIn("unsupported", unit["raw_detail"])
        self.assertTrue((Path(unit["workspace"]) / "raw").is_dir())
        self.assertFalse(book.events("paused"))
        self.assertEqual(book.unit("u2")["state"], "done")
        supported["now"] = True
        book.close()
        book = self.finish(world)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["raw_disposition"]), ("skipped", "discarded"))


class ContractHoldTests(Base):
    """"Stop" is per unit, never the runner (the user's rule of 2026-10-02): a reply or a record of Interactive's
    that the runner cannot read or act on for one unit holds that unit (contract_held), as a gate that gives no
    report does, and pauses nothing. What pauses the whole campaign is what every unit meets alike."""

    def test_a_start_reply_of_another_shape_holds_the_unit_at_its_step(self) -> None:
        world = self.world(("u1", "u2"))
        original = world.interactive.start_diagnostic
        broken = {"now": True}

        def start_diagnostic(**arguments):
            manifest = arguments["answers"]["workflow_overrides"]["repository_run_manifest"]
            if broken["now"] and world.interactive._unit_of_manifest(manifest) == "u1":
                return {"ok": False, "reason": "malformed", "detail": "a reply of another shape"}
            return original(**arguments)

        world.interactive.start_diagnostic = start_diagnostic
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u2")["state"] == "done")
        self.assert_contract_held(world, book, "u1", "malformed", resume_state="metadata_prepared")
        self.assertEqual([row["outcome"] for row in book.console_runs("u1")], ["not_started"])
        self.assertIsNone(book.slot(), "the Console slot is free for the other units")
        broken["now"] = False
        book.add_request("retry", "u1", "Interactive answers in the shape again", "Test Person", runner.stamp())
        runner.run(until_idle=True, max_iterations=2000)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 0))
        handled = [row[0] for row in book.connection.execute("SELECT handled_detail FROM request ORDER BY request_id")]
        self.assertEqual(handled, ["contract recheck brought forward"])

    def test_a_reply_that_does_not_parse_holds_the_unit(self) -> None:
        """The user's default of 2026-10-03: an Interactive reply the runner cannot read holds the unit, as a gate
        that gives no report does. Read as a failure before, it was retried twice and the unit's raw data went."""
        world = self.world(("u1", "u2"))
        original = world.interactive.prepare_metadata
        broken = {"now": True}

        def prepare_metadata(**arguments):
            if broken["now"] and world.interactive._unit_of_manifest(arguments["manifest_path"]) == "u1":
                return {"ok": False, "reason": "validation_error", "error_type": "JSONDecodeError",
                        "detail": "Expecting value: line 1 column 1 (char 0)"}
            return original(**arguments)

        world.interactive.prepare_metadata = prepare_metadata
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u2")["state"] == "done")
        self.assert_contract_held(world, book, "u1", "unreadable_reply", resume_state="preflighted")
        broken["now"] = False
        runner.run(until_idle=True, max_iterations=4000)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 0))

    def test_an_extractor_interactive_does_not_find_holds_the_unit_unless_its_pin_changed(self) -> None:
        for pin_changed in (False, True):
            with self.subTest(pin_changed=pin_changed):
                world = self.world(("u1", "u2"))
                original = world.interactive.preflight
                missing = {"now": True}

                def preflight(world=world, original=original, missing=missing, pin_changed=pin_changed, **arguments):
                    if missing["now"] and world.interactive._unit_of_manifest(arguments["manifest_path"]) == "u1":
                        if pin_changed:
                            world.pin_source.values["extractor"]["binary_sha256"] = "e" * 64
                        return {"completed": False, "extractor_found": False}
                    return original(**arguments)

                world.interactive.preflight = preflight
                book = world.open()
                self.addCleanup(book.close)
                runner = world.runner(book)
                if pin_changed:
                    # The binary the pin names is gone: a pin change, which every unit meets, pauses the campaign
                    # and lifts by itself once the pin matches again.
                    self.step_until(world, book, runner, lambda: bool(book.runner()["paused"]))
                    self.assertEqual(book.runner()["pause_kind"], "pin")
                    self.assertEqual(book.unit("u1")["state"], "downloaded")
                    self.assertFalse(book.events("contract_held"))
                    world.pin_source.values["extractor"]["binary_sha256"] = world.pins["extractor"]["binary_sha256"]
                else:
                    self.step_until(world, book, runner, lambda: book.unit("u2")["state"] == "done")
                    self.assert_contract_held(world, book, "u1", "extractor_not_found")
                missing["now"] = False
                runner.run(until_idle=True, max_iterations=2000)
                self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])
                self.assertEqual(book.unit("u1")["failures"], 0)

    def test_a_contract_held_unit_is_rechecked_when_the_runner_starts_again(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(disposition="none")
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u2")["state"] == "done")
        self.assert_contract_held(world, book, "u1", "no_disposition")
        world.scripts["u1"].disposition = "run"
        started = world.clock.now()
        world.runner(book).run(until_idle=True, max_iterations=2000)  # the next start
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 0))
        self.assertLess((world.clock.now() - started).total_seconds(), policy.CampaignPolicy().held_recheck_seconds)
        sources = [json.loads(row["detail_json"]).get("contract_recheck") for row in book.transitions("u1")]
        self.assertIn("runner_start", sources)
        self.assertEqual(machine.summary(book)["contract_held"]["units"], 0)

    # ---- the user's default of 2026-10-03 for the estimate and the job polls ----------------------------------

    def test_an_estimate_reply_that_does_not_parse_holds_the_unit_at_its_diagnostic(self) -> None:
        """Read as estimate_unavailable before, it was counted, retried twice and the unit's raw data went. Held at
        diagnosing, the recheck asks the same diagnostic for its estimate rather than running another."""
        world = self.world(("u1", "u2"))
        original = world.interactive.estimate
        broken = {"now": True}

        def estimate(**arguments):
            if broken["now"] and world.interactive._unit_of_manifest(arguments["manifest_path"]) == "u1":
                return dict(UNREADABLE)
            return original(**arguments)

        world.interactive.estimate = estimate
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u2")["state"] == "done")
        self.assert_contract_held(world, book, "u1", "unreadable_reply", resume_state="diagnosing")
        self.assertEqual([row["outcome"] for row in book.console_runs("u1")], ["completed"])
        broken["now"] = False
        runner.run(until_idle=True, max_iterations=4000)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 0))
        self.assertEqual(world.interactive.console_starts.count(("u1", "diagnostic")), 1)

    def test_a_run_poll_reply_that_does_not_parse_is_never_read_as_an_interruption(self) -> None:
        """The run job's poll does not parse while its Console runs: the unit waits for the Console, keeping the
        slot, and then reads the manifest, which says the run finalized. Read as a lost job before, the run that
        ended meanwhile was taken for interrupted and a second production Console was started on the unit."""
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(ticks=40)
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "running")
        original = world.interactive.job
        run_job = book.unit("u1")["run_job_id"]
        world.interactive.job = lambda job_id: dict(UNREADABLE) if job_id == run_job else original(job_id)
        for _ in range(20):  # ten minutes of a twenty-minute run
            runner.iterate()
            world.clock.sleep(30)
        self.assertEqual(book.unit("u1")["state"], "running", "the Console is waited for")
        self.assertEqual(book.slot()["unit_key"], "u1", "keeping the Console slot")
        runner.run(until_idle=True, max_iterations=3000)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u2")], ["done", "done"])
        self.assertEqual(world.interactive.console_starts.count(("u1", "run")), 1)
        self.assertEqual((book.unit("u1")["failures"], book.unit("u1")["interruptions"]), (0, 0))
        self.assertEqual(world.interactive.overlapping_starts, [])
        said = [json.loads(row["detail_json"]) for row in book.events("poll_unreadable") if row["unit_key"] == "u1"]
        self.assertEqual([(item["job_id"], item["contract"]) for item in said], [(run_job, "unreadable_reply")])

    def test_a_run_poll_reply_that_does_not_parse_holds_the_unit_once_its_console_has_ended(self) -> None:
        """The Console ended without a finalized run and the poll still does not parse: nothing says how the run
        ended, so the unit is held at running, its Console run ended as unknown and no attempt recorded. The
        recheck reads the job's own end, here a failure, which counts as any failed run does."""
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(ticks=4, runs=["fail", "ok"])
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "running")
        original = world.interactive.job
        run_job = book.unit("u1")["run_job_id"]
        broken = {"now": True}
        world.interactive.job = lambda job_id: dict(UNREADABLE) if broken["now"] and job_id == run_job else original(job_id)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] != "running")
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["resume_state"]), ("contract_held", "running"))
        self.assertEqual((unit["failures"], unit["interruptions"]), (0, 0))
        self.assertEqual(unit["run_job_id"], run_job, "the recheck polls the same job")
        self.assertEqual([row["outcome"] for row in book.console_runs("u1") if row["kind"] == "production"], ["unknown"])
        self.assertIsNone(book.slot())
        self.assertNotIn(f"u1:{run_job}", world.catalog.runs, "how the run ended is not known yet")
        events = [json.loads(row["detail_json"]) for row in book.events("contract_held") if row["unit_key"] == "u1"]
        self.assertEqual([(item["step"], item["contract"]) for item in events], [("running_poll", "unreadable_reply")])
        self.assertFalse(book.runner()["paused"])
        broken["now"] = False
        runner.run(until_idle=True, max_iterations=4000)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"], unit["interruptions"]), ("done", 1, 0))
        self.assertEqual(world.catalog.runs[f"u1:{run_job}"]["status"], "failed")
        steps = [(row["step"], row["outcome"], row["counted"]) for row in book.attempts("u1")]
        held = steps.index(("running_poll", "fault", 0))
        self.assertEqual(steps[held + 1], ("run", "failed", 1))
        self.assertEqual(world.interactive.console_starts.count(("u1", "run")), 2)

    def test_a_download_poll_reply_that_does_not_parse_holds_the_unit_once_its_lease_is_gone(self) -> None:
        """While the lease lives the unit waits for it; once a backend restart leaves the manifest saying
        downloading with no lease, a reply that could not be read says nothing of the job, so the unit is held at
        downloading. Read as a lost job before, it was interrupted and fetched again."""
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(ticks=40)
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "downloading")
        original = world.interactive.job
        download = book.unit("u1")["download_job_id"]
        broken = {"now": True}
        world.interactive.job = lambda job_id: dict(UNREADABLE) if broken["now"] and job_id == download else original(job_id)
        for _ in range(10):
            runner.iterate()
            world.clock.sleep(30)
        self.assertEqual(book.unit("u1")["state"], "downloading", "the live lease is waited for")
        world.interactive.restart()
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] != "downloading")
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["resume_state"]), ("contract_held", "downloading"))
        self.assertEqual((unit["failures"], unit["interruptions"]), (0, 0))
        self.assertEqual(world.interactive.download_starts, ["u1"], "nothing is fetched again")
        events = [json.loads(row["detail_json"]) for row in book.events("contract_held") if row["unit_key"] == "u1"]
        self.assertEqual([(item["step"], item["contract"]) for item in events], [("downloading_poll", "unreadable_reply")])
        self.step_until(world, book, runner, lambda: book.unit("u2")["state"] == "done")
        broken["now"] = False
        runner.run(until_idle=True, max_iterations=4000)
        unit = book.unit("u1")
        # The recheck reads the job's own end: the restart interrupted it, which is retried without counting.
        self.assertEqual((unit["state"], unit["failures"], unit["interruptions"]), ("done", 0, 1))
        self.assertEqual(world.interactive.download_starts.count("u1"), 2)


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
        original = world.interactive.discard

        def discard(**arguments):
            if broken["now"]:
                raise OSError("the raw tree is held by another process")
            return original(**arguments)

        world.interactive.discard = discard
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


class AutomaticRtCorrectionTests(Base):
    """Automatic alignment RT correction (decided 2026-10-07): the runner pins it on with 12 anchors over the profile,
    and a unit whose Console cannot select anchors runs again without it, on record, instead of failing three
    identical runs and losing its raw data with no output."""

    @staticmethod
    def starts(world: fakes.World, kind: str) -> list[dict]:
        return [call["answers"] for name, call in world.interactive.calls if name == kind]

    @staticmethod
    def record(book: ledger.Ledger, unit: str) -> dict:
        return json.loads((Path(book.unit(unit)["workspace"]) / "campaign-record.json").read_text(encoding="utf-8"))

    def test_every_console_start_is_sent_the_pinned_correction(self) -> None:
        world = self.world()
        book = self.finish(world)
        self.assertEqual(book.unit("u1")["state"], "done")
        for kind in ("diagnostic", "run"):
            with self.subTest(kind):
                (answers,) = self.starts(world, kind)
                # The diagnostic is sent them too: Interactive's prepare_tuning_run turns the correction off for it.
                self.assertIs(answers["execute_automatic_rt_correction"], True)
                self.assertEqual(answers["automatic_rt_correction_maximum_anchors"], 12)
        self.assertEqual(self.record(book, "u1")["automatic_rt_correction"],
                         {"maximum_anchors": 12, "anchor_selection_failed": False, "fallback_uncorrected": False})
        self.assertNotIn(("job_log", {"job_id": book.unit("u1")["run_job_id"]}), world.interactive.calls)

    def test_a_unit_whose_console_finds_too_few_anchors_runs_again_without_the_correction(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(runs=["rt_fail", "ok"])
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"], unit["raw_disposition"]), ("done", 1, "released"))
        runs = [call for name, call in world.interactive.calls if name == "run" and call["unit"] == "u1"]
        first, second = (call["answers"] for call in runs)
        self.assertIs(first["execute_automatic_rt_correction"], True)
        self.assertIs(second["execute_automatic_rt_correction"], False)
        self.assertEqual(second["automatic_rt_correction_maximum_anchors"], 12)
        failed = [row for row in book.attempts("u1") if row["step"] == "run" and row["outcome"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIs(json.loads(failed[0]["detail_json"])[machine.AUTOMATIC_RT_FAILED], True)
        self.assertTrue(failed[0]["counted"], "the failed run still counts")
        # The Console's line sat above 40 lines of finalisation, so the job's kept log was read.
        self.assertEqual([name for name, _call in world.interactive.calls].count("job_log"), 1)
        self.assertEqual(self.record(book, "u1")["automatic_rt_correction"],
                         {"maximum_anchors": 12, "anchor_selection_failed": True, "fallback_uncorrected": True})
        document, tsv = machine.export_status(book)
        rows = {row["unit_key"]: row for row in document["units"]}
        self.assertIn(machine.AUTOMATIC_RT_FALLBACK_WARNING, rows["u1"]["warnings"])
        self.assertEqual(rows["u2"]["warnings"], [])
        self.assertIn("automatic RT correction off", tsv)
        # Another unit keeps the correction.
        (other,) = [call["answers"] for name, call in world.interactive.calls if name == "run" and call["unit"] == "u2"]
        self.assertIs(other["execute_automatic_rt_correction"], True)

    def test_without_the_fallback_the_unit_fails_as_any_other(self) -> None:
        world = self.world(policy_values={"automatic_rt_correction_fallback": False})
        world.scripts["u1"] = fakes.UnitScript(runs=["rt_fail", "rt_fail", "rt_fail"])
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"]), ("failed", 3))
        self.assertEqual([answers["execute_automatic_rt_correction"] for answers in self.starts(world, "run")], [True] * 3)
        self.assertEqual(self.record(book, "u1")["automatic_rt_correction"],
                         {"maximum_anchors": 12, "anchor_selection_failed": True, "fallback_uncorrected": False})

    def test_another_failure_keeps_the_correction(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(runs=["fail", "ok"])
        book = self.finish(world)
        self.assertEqual(book.unit("u1")["state"], "done")
        self.assertEqual([answers["execute_automatic_rt_correction"] for answers in self.starts(world, "run")], [True, True])
        self.assertFalse(self.record(book, "u1")["automatic_rt_correction"]["anchor_selection_failed"])

    def test_a_campaign_approved_before_the_decision_is_not_pinned(self) -> None:
        world = self.world(legacy_policy=True)
        self.assertNotIn("automatic_rt_correction", world.campaign["policy"])
        world.scripts["u1"] = fakes.UnitScript(runs=["rt_fail", "ok"])
        book = self.finish(world)
        for answers in self.starts(world, "run"):
            self.assertNotIn("execute_automatic_rt_correction", answers)
            self.assertNotIn("automatic_rt_correction_maximum_anchors", answers)
        self.assertIsNone(self.record(book, "u1")["automatic_rt_correction"])
        self.assertNotIn("job_log", [name for name, _call in world.interactive.calls])

    def test_the_policy_refuses_a_maximum_below_the_consoles_minimum(self) -> None:
        for value in (2, 12.0, True, "12"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.CampaignPolicy.from_dict({"automatic_rt_correction_maximum_anchors": value})
        self.assertEqual(policy.CampaignPolicy().automatic_rt_correction_maximum_anchors, 12)


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

    def test_a_required_blocking_check_left_unevaluated_is_read_from_the_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "gate.py"
            script.write_text(
                "import json, sys\n"
                "print(json.dumps({'checks': ["
                "{'check_id': 'SUM-1', 'stage': 'before-production', 'status': 'not_evaluable', 'required': True, 'run_policy': 'blocks_run'},"
                " {'check_id': 'INP-1', 'stage': 'before-production', 'status': 'not_evaluable', 'required': False, 'run_policy': 'blocks_run'},"
                " {'check_id': 'PKH-1', 'stage': 'before-production', 'status': 'not_evaluable', 'required': True, 'run_policy': 'record_only'}],"
                " 'strict_failures': ['SUM-1', 'PKH-1'], 'run_blocked_by': ['SUM-1']}))\n"
                "sys.exit(4)\n",
                encoding="utf-8",
            )
            gate = ports.GatePort(python=sys.executable, script=script, timeout=60, gate_commit="c" * 40)
            verdict = gate.run(directory, "before_production", Path(directory) / "reports" / "01-before_production.json")
        self.assertIsNone(policy.gate_report_problem(verdict))
        self.assertEqual((verdict["blocking_fail_ids"], verdict["blocking_unevaluated_ids"]), ([], ["SUM-1"]))

    def test_a_gate_that_gave_no_report_is_said_so(self) -> None:
        """The endings of a gate that leave the runner no report: the user's rule holds the unit on each before
        production (2026-10-02). The ledger keeps only the exit codes the gate defines; the others are said."""
        endings = {
            "exit 3, nothing printed": ("import sys\nsys.stderr.write('not a directory: x')\nsys.exit(3)\n",
                                        policy.GATE_UNUSABLE, "not a directory"),
            "a crash": ("raise SystemExit('Traceback: boom')\n", policy.GATE_ERROR, "The gate exited 1"),
            "not JSON": ("print('[PASS] ID-1')\n", policy.GATE_UNPARSABLE, "not a report"),
            "JSON that is no report": ("print('[1, 2]')\nraise SystemExit(2)\n", policy.GATE_UNPARSABLE, "not a report"),
        }
        for name, (source, problem, said) in endings.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                script = Path(directory) / "gate.py"
                script.write_text(source, encoding="utf-8")
                gate = ports.GatePort(python=sys.executable, script=script, timeout=60, gate_commit="c" * 40)
                verdict = gate.run(directory, "before_production", Path(directory) / "reports" / "01.json")
                self.assertFalse(verdict["report_parsed"])
                self.assertEqual(policy.gate_report_problem(verdict), problem, verdict)
                self.assertIn(said, verdict["detail"])
                self.assertIn(verdict.get("exit_code"), (None, 0, 2, 3, 4), "the ledger's CHECK takes it")
        gate = ports.GatePort(python=str(Path(tempfile.gettempdir()) / "no-such-python.exe"), timeout=60, gate_commit="c" * 40)
        verdict = gate.run(tempfile.gettempdir(), "before_production", Path(tempfile.gettempdir()) / "unused.json")
        self.assertEqual(policy.gate_report_problem(verdict), policy.GATE_NOT_RUN, "a gate the runner could not start")


if __name__ == "__main__":
    unittest.main()
