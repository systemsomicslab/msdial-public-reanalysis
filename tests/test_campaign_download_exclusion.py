"""A unit Interactive excludes at its download's attribute stage: the contract of 2026-10-15.

Where every declared analysis input a download lacks is a declared archive the extract stage expanded that holds no
file MS-DIAL opens, Interactive raises DeclaredInputsUnsupportedError, and the failed lease's download_failure carries
"retryable": false and "disposition": {"decision": "exclude", "reason": "declared_inputs_unsupported_format",
"inputs": [...]}. The runner ends such a unit excluded, not failed: nothing is counted, it is not retried, and its raw
data are deleted under boundary 5 as for every exclusion. The same failure without a disposition, as an Interactive
before the contract records it, is still a failure, retried twice and then failed. Nothing here downloads a byte.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes as fakes  # noqa: E402
import test_campaign_resume as resume  # noqa: E402  (the module, so that its own tests are not collected here)
from campaign import ledger, machine, policy  # noqa: E402
from test_campaign_machine import Base  # noqa: E402

REASON = "declared_inputs_unsupported_format"


def exclusion_scripted(world: fakes.World) -> None:
    """x1's download is excluded at the attribute stage; x2's fails once without a disposition, then finishes."""
    world.scripts["x1"] = fakes.UnitScript(downloads=["unsupported_inputs"])
    world.scripts["x2"] = fakes.UnitScript(downloads=["unsupported_inputs_legacy", "ok"])


class DownloadExclusionTests(Base):
    def assert_excluded(self, world: fakes.World, book: ledger.Ledger, key: str, downloads: int = 1) -> None:
        unit = book.unit(key)
        self.assertEqual((unit["state"], unit["terminal_reason"]), ("excluded", REASON))
        self.assertEqual((unit["failures"], unit["interruptions"]), (0, 0), "Interactive's decision is no failure")
        detail = json.loads(unit["terminal_detail"])
        self.assertEqual(detail["reason"], REASON)
        self.assertEqual(detail["inputs"], fakes.unsupported_inputs_failure(key)["disposition"]["inputs"],
                         "the inputs as Interactive gave them")
        self.assertEqual(world.interactive.download_starts.count(key), downloads, "it is not downloaded again")
        self.assertFalse([row for row in book.transitions(key) if row["to_state"] == "waiting_retry"], "never retried")
        self.assertFalse([row for row in book.attempts(key) if row["counted"]], "nothing counted against it")
        ended = [row for row in book.attempts(key) if row["step"] == "download" and row["outcome"] != "ok"]
        self.assertEqual([(row["outcome"], row["counted"]) for row in ended], [("blocked", 0)])
        self.assertEqual(json.loads(ended[0]["detail_json"])["excluded"], REASON)
        # Deleted under the approval, as every exclusion is: Interactive's discard, boundary 5.
        self.assertEqual(unit["raw_disposition"], "discarded")
        self.assertEqual(self.boundaries(book, key)[-1], "5")
        self.assertTrue(all(row["approval_id"] == "approval-1" for row in book.transitions(key) if row["boundary"]))
        self.assertIn(("discard", unit["manifest_path"]),
                      [(name, arguments.get("manifest_path")) for name, arguments in world.interactive.calls])
        self.assertFalse((Path(unit["workspace"]) / "raw").exists(), "its raw data are gone")
        self.assertEqual(world.interactive.console_starts.count((key, "run")), 0)
        record = json.loads((Path(unit["workspace"]) / "campaign-record.json").read_text(encoding="utf-8"))
        self.assertIn(REASON, json.dumps(record))

    def test_an_exclusion_at_the_attribute_stage_ends_the_unit_excluded_and_its_raw_data_go(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(downloads=["unsupported_inputs", "ok"])
        book = self.finish(world)
        self.assert_excluded(world, book, "u1")
        self.assertEqual(book.unit("u2")["state"], "done", "the campaign goes on")
        self.assertFalse(book.events("paused"))
        # Status and export name the reason.
        summary = machine.summary(book)
        self.assertEqual(summary["excluded_by_reason"], {REASON: 1})
        self.assertEqual(summary["states"]["excluded"], 1)
        document, tsv = machine.export_status(book)
        row = next(item for item in document["units"] if item["unit_key"] == "u1")
        self.assertEqual(row["terminal_reason"], REASON)
        self.assertEqual(json.loads(row["terminal_detail"])["inputs"][0]["declared"], "FILES/batch1.zip")
        line = next(line for line in tsv.splitlines() if line.split("\t")[0] == "u1")
        self.assertEqual(line.split("\t")[machine.TSV_COLUMNS.index("terminal_reason")], REASON)
        self.assertEqual(machine.remaining_work(book), [], "an excluded unit whose raw data went is no work left")

    def test_without_a_disposition_the_same_failure_is_retried_and_fails_as_before(self) -> None:
        """An Interactive before the contract writes the plain ValueError: the runner cannot tell it from any other
        attribute failure, so the unit is retried twice and then failed, its raw data deleted."""
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(downloads=["unsupported_inputs_legacy"] * 3)
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["terminal_reason"], unit["failures"]), ("failed", "download_failed", 3))
        self.assertEqual(world.interactive.download_starts.count("u1"), 3)
        self.assertEqual(len([row for row in book.transitions("u1") if row["to_state"] == "waiting_retry"]), 2)
        self.assertEqual(len([row for row in book.attempts("u1") if row["counted"]]), 3)
        self.assertEqual(unit["raw_disposition"], "discarded")
        self.assertEqual(machine.summary(book)["excluded_by_reason"], {})
        self.assertEqual(book.unit("u2")["state"], "done")

    def test_a_failure_without_a_disposition_is_retried_and_an_exclusion_after_it_still_excludes(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(downloads=["unsupported_inputs_legacy", "unsupported_inputs"])
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["terminal_reason"], unit["failures"]), ("excluded", REASON, 1),
                         "the first failure was counted, the exclusion is not")
        self.assertEqual(world.interactive.download_starts.count("u1"), 2)
        self.assertEqual(unit["raw_disposition"], "discarded")

    def test_a_lost_job_whose_manifest_records_the_exclusion_is_excluded(self) -> None:
        """The backend no longer knows the job (_download_lost): the unit manifest's download_failed record, with its
        disposition, says how the download ended."""
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(downloads=["unsupported_inputs"], ticks=10)
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "downloading")
        world.clock.sleep(30 * 20)
        world.interactive.settle()
        manifest = world.interactive.store.read(book.unit("u1")["manifest_path"])
        self.assertEqual(manifest["status"], "download_failed")
        world.interactive.jobs.clear()
        runner.run(until_idle=True, max_iterations=2000)
        self.assert_excluded(world, book, "u1")
        lost = next(row for row in book.attempts("u1") if row["step"] == "download" and row["outcome"] == "blocked")
        self.assertEqual(json.loads(lost["detail_json"])["job"], "not in the backend's registry")
        self.assertEqual(book.unit("u2")["state"], "done")

    def test_a_disposition_the_job_itself_carries_is_read_where_the_manifest_has_none(self) -> None:
        """Should Interactive pass the failed lease's record on to the job, the runner reads it there too."""
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(downloads=["unsupported_inputs"], ticks=10)
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "downloading")
        world.clock.sleep(30 * 20)
        world.interactive.settle()
        path = book.unit("u1")["manifest_path"]
        manifest = world.interactive.store.read(path)
        failure = manifest.pop("download_failure")
        world.interactive.store.write(path, manifest)
        world.interactive.jobs[book.unit("u1")["download_job_id"]]["failure"] = failure
        runner.run(until_idle=True, max_iterations=2000)
        self.assert_excluded(world, book, "u1")

    def test_the_campaign_that_keeps_raw_data_keeps_an_excluded_units(self) -> None:
        world = self.world(retention="keep", covers=("1", "3", "4", "split"))
        world.scripts["u1"] = fakes.UnitScript(downloads=["unsupported_inputs"])
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["terminal_reason"], unit["failures"]), ("excluded", REASON, 0))
        self.assertEqual(unit["raw_disposition"], "kept")
        self.assertNotIn("5", self.boundaries(book, "u1"))
        self.assertFalse([name for name, _arguments in world.interactive.calls if name == "discard"])

    def test_an_exclusion_ends_a_repositorys_run_of_network_failures(self) -> None:
        """The excluded download fetched every byte: the server worked, so a network failure before it and one after
        it are not two groups failing in a row (an outage at outage_units 2)."""
        world = self.world(("u1", "u2", "u3"), policy_values={"outage_units": 2})
        world.scripts["u1"] = fakes.UnitScript(downloads=["fail", "ok"])
        world.scripts["u2"] = fakes.UnitScript(downloads=["unsupported_inputs"])
        world.scripts["u3"] = fakes.UnitScript(downloads=["fail", "ok"])
        book = self.finish(world, max_iterations=5000)
        self.assertEqual(book.unit("u2")["state"], "excluded")
        self.assertFalse([row for row in book.events("paused")
                          if json.loads(row["detail_json"]).get("pause_kind") == "outage"], "no outage was read")
        self.assertFalse([row for row in book.attempts() if row["outcome"] == "fault"])
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u3")], ["done", "done"])
        self.assertEqual([book.unit(key)["failures"] for key in ("u1", "u3")], [1, 1])


class ExclusionResumeTests(resume.ResumeTests):
    """The crash at every commit (tests/test_campaign_resume.py), on an exclusion and a failure without one: a runner
    stopped anywhere resumes to the same end, without a second download of the excluded unit."""

    units = ("x1", "x2")
    script = staticmethod(exclusion_scripted)

    def test_a_crash_at_every_commit_resumes_to_the_same_end(self) -> None:
        expected, commits = self.baseline()
        self.assertEqual(expected["units"]["x1"][:4], ("excluded", REASON, 0, "discarded"))
        self.assertEqual(expected["units"]["x2"][:3], ("done", "outputs_produced", 1))
        self.assertEqual(expected["downloads"], ["x1", "x2", "x2"])
        self.assertEqual(expected["counted"], {"x1": 0, "x2": 1})
        self.assertEqual(expected["crossings"]["x1"][-1][:2], ("excluded", "5"))
        for phase in ("before", "after"):
            for at in range(1, commits + 1):
                with self.subTest(phase=phase, commit=at):
                    self.assertEqual(self.crash_and_resume(at, phase), expected)

    # The other tests are the base class's; they do not depend on the scenario and run once there.
    test_a_cancel_recorded_and_never_sent_is_sent_after_a_restart = None
    test_an_ambiguous_console_start_is_adopted_after_a_restart = None
    test_a_start_that_never_reached_the_backend_is_made_again = None
    test_a_second_runner_cannot_take_a_live_campaign = None


class DownloadExclusionPolicyTests(unittest.TestCase):
    def test_only_a_disposition_whose_decision_is_exclude_is_read(self) -> None:
        excluded = fakes.unsupported_inputs_failure("u1")
        self.assertEqual(policy.download_exclusion({"failure": excluded}),
                         {"reason": REASON, "inputs": excluded["disposition"]["inputs"]})
        for detail in (
            {}, {"failure": None}, {"failure": "text"}, {"failure": fakes.unsupported_inputs_failure("u1", disposition=False)},
            {"failure": {**excluded, "disposition": None}}, {"failure": {**excluded, "disposition": "exclude"}},
            {"failure": {**excluded, "disposition": {**excluded["disposition"], "decision": "skip"}}},
            {"failure": {**excluded, "disposition": {**excluded["disposition"], "decision": "Exclude"}}},
            # A disposition at the top of the job's record is not the failed lease's.
            {"disposition": excluded["disposition"]},
        ):
            with self.subTest(detail=detail):
                self.assertIsNone(policy.download_exclusion(detail))

    def test_a_reason_or_inputs_it_does_not_name_are_recorded_as_such(self) -> None:
        self.assertEqual(policy.download_exclusion({"failure": {"disposition": {"decision": "exclude"}}}),
                         {"reason": policy.DOWNLOAD_EXCLUDE_UNSTATED, "inputs": []})
        self.assertEqual(policy.download_exclusion({"failure": {"disposition": {
            "decision": "exclude", "reason": "other_reason", "inputs": "FILES/x.zip"}}}),
            {"reason": "other_reason", "inputs": []})

    def test_the_exclusion_is_not_read_as_a_network_failure_or_a_short_disk(self) -> None:
        detail = {"error": fakes.unsupported_inputs_failure("u1")["reason"], "failure": fakes.unsupported_inputs_failure("u1")}
        self.assertFalse(policy.network_failure(detail))
        self.assertIsNone(policy.lease_size_limit(detail))
        self.assertIsNone(policy.extraction_disk_short(detail))


if __name__ == "__main__":
    unittest.main()
