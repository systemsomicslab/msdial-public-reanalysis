"""Stop the runner dead at every commit it makes, start a new one from the ledger, and compare.

A campaign runs for weeks through reboots, so the one property that matters most is that a runner
stopped at any committed transition resumes to the same end as one never stopped: the same states and
reasons, the same counted failures, the same boundary crossings under the same approval - and never a
second download or a second Console for what the first runner had already started. The fake backend
outlives the runner, as the real one does, and keeps its jobs and its unit manifests.

Each run is stopped both just before a commit (the transaction is lost, as with a crash before COMMIT)
and just after it (the transaction is durable and nothing after it ran).
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes as fakes  # noqa: E402
from campaign import ledger, policy  # noqa: E402

UNITS = ("u1", "u2", "u3")


def scripted(world: fakes.World) -> None:
    world.scripts["u1"] = fakes.UnitScript(downloads=["fail", "ok"], runs=["lose_reply"])
    world.scripts["u2"] = fakes.UnitScript(disposition="split", split_modes=("DDA", "AIF"))
    world.scripts["u3"] = fakes.UnitScript(disposition="exclude", ticks=1)


def outcome(world: fakes.World) -> dict:
    with world.open() as book:
        units = {
            unit["unit_key"]: (unit["state"], unit["terminal_reason"], unit["failures"], unit["raw_disposition"],
                               unit["outputs_produced"], unit["minimum_peak_height"])
            for unit in book.units()
        }
        crossings = {
            key: [(row["to_state"], row["boundary"], row["approval_id"]) for row in book.transitions(key) if row["boundary"]]
            for key in units
        }
        counted = {key: sum(1 for row in book.attempts(key) if row["counted"]) for key in units}
        open_attempts = sum(len(book.open_attempts(key)) for key in units)
        slot = book.slot()
    return {
        "units": units,
        "crossings": crossings,
        "counted": counted,
        "open_attempts": open_attempts,
        "slot": slot,
        "downloads": sorted(world.interactive.download_starts),
        "consoles": sorted(world.interactive.console_starts),
        # Which unit each run record is for; job ids are numbered in the order jobs were made, and a runner
        # that comes back after an hour picks a retried unit up sooner than one that never stopped.
        "catalog_runs": sorted(run_id.split(":")[0] for run_id in world.catalog.runs),
        "class_saves": sorted(item["unit_id"] for item in world.catalog.saved),
        # Why jobs were cancelled; a resumed runner may send a cancel again, which Interactive ignores.
        "cancel_reasons": sorted({reason for _job, reason in world.interactive.cancels}),
    }


class FloorDisk(fakes.FakeDisk):
    """Free space drops below the download floor while a download that holds on (outcome "hold") has moved
    some bytes and has not been asked to stop."""

    def __init__(self, world: fakes.World) -> None:
        super().__init__()
        self.world = world

    def usage(self, _path: str) -> tuple[int, int]:
        for job in self.world.interactive.jobs.values():
            if (job["kind"] == "download" and job["outcome"] == "hold" and job["status"] in ("queued", "running")
                    and not job.get("cancel") and job["received"] >= 2000):
                return 50 * 1000**3, self.total
        return self.free, self.total


def cancels_scripted(world: fakes.World) -> None:
    """The three cancels: a stalled download, an operator's skip of a running Console, the disk floor."""
    world.scripts["c1"] = fakes.UnitScript(downloads=["stall", "ok"])
    world.scripts["c2"] = fakes.UnitScript(runs=["hold"])
    world.scripts["c3"] = fakes.UnitScript(downloads=["hold", "ok"])
    world.disk = FloorDisk(world)
    asked = {"skip": False}
    start_run = world.interactive.start_run

    def start_run_then_skip(**arguments):
        result = start_run(**arguments)
        if "c2" in arguments["answers"]["workflow_overrides"]["repository_run_manifest"] and not asked["skip"]:
            asked["skip"] = True
            with contextlib.closing(sqlite3.connect(world.ledger_path)) as connection:
                connection.execute(
                    "INSERT INTO request(at, action, unit_key, reason, requested_by) VALUES (?, 'skip', 'c2', 'not this one', 'Test Person')",
                    (policy.iso(world.clock.now()),),
                )
                connection.commit()
        return result

    world.interactive.start_run = start_run_then_skip


class ResumeTests(unittest.TestCase):
    units: tuple[str, ...] = UNITS
    script = staticmethod(scripted)

    def fresh(self, directory: str) -> fakes.World:
        world = fakes.World(Path(directory), list(self.units))
        self.script(world)
        return world

    def baseline(self) -> tuple[dict, int]:
        with tempfile.TemporaryDirectory() as directory:
            world = self.fresh(directory)
            commits = []

            def count(phase: str) -> None:
                if phase == "after":
                    commits.append(1)

            world.run(commit_hook=count)
            return outcome(world), len(commits)

    def crash_and_resume(self, at: int, phase: str) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            world = self.fresh(directory)
            seen = []

            def crash(when: str) -> None:
                if when == phase:
                    seen.append(1)
                    if len(seen) == at:
                        raise fakes.Crash()

            try:
                world.run(commit_hook=crash)
            except fakes.Crash:
                pass
            else:
                self.fail(f"the run made fewer than {at} commits")
            # A reboot takes a while, and the backend's jobs finish meanwhile: nothing but the unit
            # manifest then says that a start the runner never recorded did happen.
            world.clock.sleep(3600)
            world.run()
            result = outcome(world)
            # The same catalog writes happen on resume; saving twice is idempotent in the real Catalog, and
            # it is the set of units saved that must match.
            result["class_saves"] = sorted(set(result["class_saves"]))
            return result

    def test_a_crash_at_every_commit_resumes_to_the_same_end(self) -> None:
        expected, commits = self.baseline()
        self.assertGreater(commits, 60)
        self.assertEqual(expected["open_attempts"], 0)
        self.assertIsNone(expected["slot"])
        self.assertEqual(expected["units"]["u1"][:3], ("done", "outputs_produced", 1))
        self.assertEqual(expected["units"]["u2"][0], "split_done")
        self.assertEqual(expected["units"]["u3"][:2], ("excluded", "preflight_exclude"))
        for phase in ("before", "after"):
            for at in range(1, commits + 1):
                with self.subTest(phase=phase, commit=at):
                    self.assertEqual(self.crash_and_resume(at, phase), expected)

    def test_a_cancel_recorded_and_never_sent_is_sent_after_a_restart(self) -> None:
        """The runner records why it cancels before it asks, so a resumed runner knows the reason; one that
        died between the two sends the cancel at the job's next poll."""
        with tempfile.TemporaryDirectory() as directory:
            world = fakes.World(Path(directory), ["u1"])
            world.scripts["u1"] = fakes.UnitScript(downloads=["stall", "ok"])

            def crash_after_the_record(phase: str) -> None:
                if phase != "after" or world.interactive.cancels:
                    return
                with contextlib.closing(sqlite3.connect(world.ledger_path)) as connection:
                    if connection.execute("SELECT COUNT(*) FROM campaign_event WHERE kind = 'cancel_requested'").fetchone()[0]:
                        raise fakes.Crash()

            with self.assertRaises(fakes.Crash):
                world.run(commit_hook=crash_after_the_record)
            self.assertEqual(world.interactive.cancels, [])
            world.clock.sleep(60)
            world.run()
            with world.open() as book:
                self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 1))
                self.assertEqual(len(book.events("cancel_requested")), 1)
            self.assertEqual([reason for _job, reason in world.interactive.cancels], ["stalled"])

    def test_an_ambiguous_console_start_is_adopted_after_a_restart(self) -> None:
        """The start call reached the backend, and the runner died before it wrote down the job."""
        with tempfile.TemporaryDirectory() as directory:
            world = fakes.World(Path(directory), ["u1"])
            original = world.interactive.start_run

            def start_then_die(**arguments):
                original(**arguments)
                raise fakes.Crash()

            world.interactive.start_run = start_then_die
            with self.assertRaises(fakes.Crash):
                world.run()
            with world.open() as book:
                self.assertEqual(book.unit("u1")["state"], "prepared")
                self.assertEqual([row["step"] for row in book.open_attempts("u1")], ["run_start"])
            world.interactive.start_run = original
            world.run()
            with world.open() as book:
                unit = book.unit("u1")
                self.assertEqual((unit["state"], unit["failures"]), ("done", 0))
                adopted = [row for row in book.attempts("u1") if row["step"] == "run_start"]
                self.assertEqual(len(adopted), 1)
                self.assertIn('"adopted": true', adopted[0]["detail_json"])
                self.assertEqual([run["job_id"] for run in book.console_runs("u1") if run["kind"] == "production"],
                                 [unit["run_job_id"]])
            self.assertEqual(world.interactive.console_starts.count(("u1", "run")), 1, "no second Console")

    def test_a_start_that_never_reached_the_backend_is_made_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            world = fakes.World(Path(directory), ["u1"])
            original = world.interactive.download

            def die_before(**arguments):
                raise fakes.Crash()

            world.interactive.download = die_before
            with self.assertRaises(fakes.Crash):
                world.run()
            world.interactive.download = original
            world.run()
            with world.open() as book:
                self.assertEqual(book.unit("u1")["state"], "done")
                steps = [(row["step"], row["outcome"]) for row in book.attempts("u1") if row["step"] == "download_start"]
                self.assertEqual(steps, [("download_start", "interrupted"), ("download_start", "ok")])
            self.assertEqual(world.interactive.download_starts, ["u1"])

    def test_a_second_runner_cannot_take_a_live_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            world = fakes.World(Path(directory), ["u1"])
            with world.open() as first, world.open() as second:
                self.assertTrue(first.take_lock(1, 1.0, "h", "2026-10-01T00:00:00+00:00", holder_alive=lambda _r: True)[0])
                self.assertFalse(second.take_lock(2, 2.0, "h", "2026-10-01T00:00:00+00:00", holder_alive=lambda _r: True)[0])


class CancelResumeTests(ResumeTests):
    """The crash at every commit again, on the three paths that cancel a job: a stall, a skip, the floor."""

    units = ("c1", "c2", "c3")
    script = staticmethod(cancels_scripted)

    def test_a_crash_at_every_commit_resumes_to_the_same_end(self) -> None:
        expected, commits = self.baseline()
        self.assertEqual(expected["units"]["c1"][:3], ("done", "outputs_produced", 1))
        self.assertEqual(expected["units"]["c2"][:4], ("skipped", "operator_skip", 0, "discarded"))
        self.assertEqual(expected["units"]["c3"][:3], ("done", "outputs_produced", 0))
        self.assertEqual(expected["downloads"], ["c1", "c1", "c2", "c3", "c3"])
        self.assertEqual(expected["cancel_reasons"], ["disk", "skip", "stalled"])
        self.assertEqual(expected["open_attempts"], 0)
        for phase in ("before", "after"):
            for at in range(1, commits + 1):
                with self.subTest(phase=phase, commit=at):
                    self.assertEqual(self.crash_and_resume(at, phase), expected)

    # The other tests are the base class's; they do not depend on the scenario and run once there.
    test_a_cancel_recorded_and_never_sent_is_sent_after_a_restart = None
    test_an_ambiguous_console_start_is_adopted_after_a_restart = None
    test_a_start_that_never_reached_the_backend_is_made_again = None
    test_a_second_runner_cannot_take_a_live_campaign = None


if __name__ == "__main__":
    unittest.main()
