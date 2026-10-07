"""The campaign runner on a disposition that holds (the AIF rule of 2026-10-07, Interactive 0.5.31).

A multi-collision-energy AIF unit is HELD until a patched Console exists: not run, its raw data kept, not counted
as a failure. Interactive's campaign_disposition says so: disposition "skip", hold true, and the reason
aif_multi_ce_awaiting_console. The runner holds the unit (disposition_held) and the other units go on; status
counts it; nothing rechecks it by itself, and an operator's recheck-held lifts it by making its preflight again.
Nothing here downloads a byte or starts a Console.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes as fakes  # noqa: E402
from campaign import ledger, machine, policy  # noqa: E402
from test_campaign_machine import Base  # noqa: E402

REASON = "aif_multi_ce_awaiting_console"


def _cli():
    spec = importlib.util.spec_from_file_location("campaign_runner_cli_hold", TESTS.parent / "scripts" / "campaign-runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DispositionHoldTests(Base):
    def held(self, units=("u1", "u2")):
        world = self.world(units)
        world.scripts["u1"] = fakes.UnitScript(disposition="aif_hold")
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        runner.run(until_idle=True, max_iterations=2000)
        return world, book, runner

    def preflights(self, world: fakes.World, book: ledger.Ledger, key: str) -> int:
        manifest = book.unit(key)["manifest_path"]
        return len([name for name, arguments in world.interactive.calls
                    if name == "preflight" and arguments["manifest_path"] == manifest])

    def assert_held(self, world: fakes.World, book: ledger.Ledger, key: str) -> None:
        unit = book.unit(key)
        self.assertEqual((unit["state"], unit["resume_state"]), ("disposition_held", "downloaded"))
        self.assertIsNone(unit["next_attempt_at"], "no recheck comes by itself")
        self.assertIsNone(unit["terminal_reason"])
        self.assertEqual((unit["failures"], unit["interruptions"]), (0, 0), "neither a retry nor a failure")
        self.assertFalse([row for row in book.attempts(key) if row["counted"]])
        self.assertEqual(unit["raw_disposition"], "present")
        self.assertTrue((Path(unit["workspace"]) / "raw").is_dir(), "the raw data are kept")
        self.assertFalse([name for name, arguments in world.interactive.calls
                          if name in ("discard", "cleanup") and arguments.get("manifest_path") == unit["manifest_path"]])
        self.assertFalse([start for start in world.interactive.console_starts if start[0] == key], "the unit is not run")
        record = json.loads(unit["disposition_json"])
        self.assertEqual((record["disposition"], record["hold"], record["reasons"]), ("skip", True, [REASON]))
        self.assertFalse(book.runner()["paused"], "one unit's hold pauses nothing")

    def test_a_multi_energy_aif_unit_is_held_with_its_raw_data_and_the_others_go_on(self) -> None:
        world, book, runner = self.held()

        self.assert_held(world, book, "u1")
        self.assertEqual(book.unit("u2")["state"], "done")
        events = [json.loads(row["detail_json"]) for row in book.events("disposition_held") if row["unit_key"] == "u1"]
        self.assertEqual(events[-1]["reasons"], [REASON])
        self.assertIn("raw data are kept", events[-1]["warning"])
        self.assertTrue(runner.idle(), "a disposition hold keeps no runner going")
        self.assertEqual(machine.remaining_work(book), [], "nor makes the hourly start a campaign with work")

    def test_status_counts_the_held_unit_apart(self) -> None:
        _world, book, _runner = self.held()
        summary = machine.summary(book)

        self.assertEqual(summary["disposition_held"]["units"], 1)
        self.assertEqual(summary["disposition_held"]["unit_keys"], ["u1"])
        self.assertEqual(summary["disposition_held"]["reasons"], {REASON: 1})
        self.assertEqual(summary["disposition_held"]["recheck_asked"], 0)
        self.assertIn("patched Console", summary["disposition_held"]["warning"])
        self.assertEqual(summary["states"]["disposition_held"], 1)
        self.assertEqual(summary["raw_disposition"]["present"], 1)
        self.assertEqual(summary["gate_held"]["units"], 0)
        document, tsv = machine.export_status(book)
        row = next(item for item in document["units"] if item["unit_key"] == "u1")
        self.assertEqual(len(row["warnings"]), 1)
        self.assertIn(f"(disposition: {REASON})", row["warnings"][0])
        line = next(line for line in tsv.splitlines() if line.split("\t")[0] == "u1")
        self.assertIn("raw data are kept", line.split("\t")[machine.TSV_COLUMNS.index("warnings")])

    def test_nothing_rechecks_the_hold_by_itself(self) -> None:
        world, book, runner = self.held()
        world.clock.sleep(runner.policy.held_recheck_seconds * 3)
        runner.run(until_idle=True, max_iterations=500)
        machine.Runner(book, world.ports(), resources={"libraries": world.libraries}).run(until_idle=True, max_iterations=500)

        self.assert_held(world, book, "u1")
        self.assertEqual(self.preflights(world, book, "u1"), 1, "neither the clock nor a runner start asks again")

    def test_an_operator_recheck_makes_the_preflight_again_and_the_new_disposition_decides(self) -> None:
        world, book, runner = self.held()
        world.scripts["u1"].disposition = "run"
        book.add_request("recheck_held", "u1", "the patched Console is pinned", "Test Person", runner.stamp())
        runner.run(until_idle=True, max_iterations=2000)

        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 0))
        self.assertEqual(self.preflights(world, book, "u1"), 2)
        handled = [row[0] for row in book.connection.execute("SELECT handled_detail FROM request ORDER BY request_id")]
        self.assertEqual(handled, ["disposition recheck brought forward"])

    def test_a_recheck_that_is_held_again_waits_again(self) -> None:
        world, book, runner = self.held()
        book.add_request("retry", "u1", "ask Interactive again", "Test Person", runner.stamp())
        runner.run(until_idle=True, max_iterations=2000)

        self.assert_held(world, book, "u1")
        self.assertEqual(self.preflights(world, book, "u1"), 2)
        self.assertEqual(machine.summary(book)["disposition_held"]["recheck_asked"], 0)

    def discards(self, world: fakes.World, book: ledger.Ledger, key: str) -> list:
        manifest = book.unit(key)["manifest_path"]
        return [arguments["release_disposition_hold"] for name, arguments in world.interactive.calls
                if name == "discard" and arguments["manifest_path"] == manifest]

    def test_the_fake_refuses_a_held_discard_without_the_release_as_interactive_does(self) -> None:
        world, book, _runner = self.held()
        unit = book.unit("u1")
        refused = world.interactive.discard(manifest_path=unit["manifest_path"], authorization_path="a", unit_id="u1")

        self.assertEqual((refused["deleted"], refused["blockers"]), (False, ["disposition_held"]))
        self.assertTrue(policy.discard_blocked_for_good(refused["blockers"]))
        self.assertTrue((Path(unit["workspace"]) / "raw").is_dir())

    def test_an_operator_skip_releases_the_hold_and_discards_the_raw_data(self) -> None:
        """The agreed contract of 2026-10-07: only an operator's explicit decision lifts the hold, and the skip
        is that decision. Its discard passes release_disposition_hold; Interactive records
        disposition_hold_released_by operator_skip. Before, the runner called the discard without it, so a
        real Interactive refused it; it was retried and the raw data then held, rechecked for good."""
        world, book, runner = self.held()
        book.add_request("skip", "u1", "not waiting for the Console", "Test Person", runner.stamp())
        runner.run(until_idle=True, max_iterations=2000)

        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["raw_disposition"]), ("skipped", "discarded"))
        self.assertEqual(self.discards(world, book, "u1"), [True])
        self.assertFalse((Path(unit["workspace"]) / "raw").exists())
        manifest = world.interactive.store.read(unit["manifest_path"])
        self.assertEqual((manifest["status"], manifest["disposition_hold_released_by"]), ("discarded", "operator_skip"))
        self.assertIs(json.loads(unit["terminal_detail"])["release_disposition_hold"], True)
        self.assertIn("disposition hold released", unit["raw_detail"])
        handled = [row[0] for row in book.connection.execute("SELECT handled_detail FROM request ORDER BY request_id")]
        self.assertEqual(handled, ["skipped; the disposition hold is released (operator_skip)"])
        self.assertEqual(machine.remaining_work(book), [], "nothing is left to recheck")

    def test_no_other_discard_releases_a_hold(self) -> None:
        """A held unit sent to its discard without an operator's release (a ledger whose skip predates it) keeps
        its raw data as the hold decided, and is not held for a recheck that would ask for the same refused
        discard every few hours and keep the hourly start running."""
        world, book, runner = self.held()
        unit = book.unit("u1")
        book.transition("u1", "discarding", runner.stamp(), expect_from="disposition_held", pending_terminal="skipped",
                        terminal_detail=json.dumps({"reason": "operator_skip", "detail": "before the release"}))
        runner.run(until_idle=True, max_iterations=2000)

        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["raw_disposition"]), ("skipped", "kept"))
        self.assertEqual(self.discards(world, book, "u1"), [False])
        self.assertTrue((Path(unit["workspace"]) / "raw").is_dir())
        self.assertIn("campaign disposition holds the unit", unit["raw_detail"])
        self.assertEqual(machine.remaining_work(book), [])

    def test_an_interactive_that_cannot_release_a_hold_leaves_the_raw_data_held(self) -> None:
        """Interactive 0.5.31 as first written (68f1cc0): the hold, and no release_disposition_hold. The skip's
        discard is a contract the runner cannot meet: the raw data are held, never discarded without the release."""
        world, book, runner = self.held()
        world.interactive.release_hold_supported = False
        book.add_request("skip", "u1", "not waiting for the Console", "Test Person", runner.stamp())
        runner.run(until_idle=True, max_iterations=2000)

        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["raw_disposition"]), ("skipped", "held"))
        self.assertTrue((Path(unit["workspace"]) / "raw").is_dir())
        self.assertNotIn(False, self.discards(world, book, "u1"), "never a discard without the release")
        world.interactive.release_hold_supported = True
        book.add_request("release_held", "u1", "Interactive takes the release now", "Test Person", runner.stamp())
        runner.run(until_idle=True, max_iterations=2000)
        self.assertEqual(book.unit("u1")["raw_disposition"], "discarded")
        self.assertFalse((Path(unit["workspace"]) / "raw").exists())

    def test_the_recheck_held_command_asks_for_the_disposition_held_units_only_when_told(self) -> None:
        world = self.world(("u1", "u2", "u3"))
        world.scripts["u1"] = fakes.UnitScript(disposition="aif_hold")
        world.gate.no_report[("u2", "before_production")] = "timeout"
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: [book.unit(key)["state"] for key in ("u1", "u2", "u3")]
                        == ["disposition_held", "gate_held", "done"])
        cli = _cli()
        root = ["--workspace-root", str(world.workspace_root)]
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(cli.main([*root, "recheck-held", "--campaign", "test-campaign"]), 0)
        self.assertIn("recheck_held u2", printed.getvalue())
        self.assertNotIn("recheck_held u1", printed.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(cli.main([*root, "recheck-held", "--campaign", "test-campaign", "--disposition-held"]), 0)
        self.assertIn("recheck_held u1", printed.getvalue())
        self.assertNotIn("recheck_held u2", printed.getvalue())
        self.assertEqual([(row["action"], row["unit_key"]) for row in book.pending_requests()],
                         [("recheck_held", "u2"), ("recheck_held", "u1")])
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(cli.main([*root, "status", "--campaign", "test-campaign"]), 0)
        self.assertEqual(json.loads(printed.getvalue())["disposition_held"]["units"], 1)

    def test_a_held_split_part_holds_its_parent_and_its_raw_data_and_keeps_no_runner(self) -> None:
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(disposition="split")
        world.scripts["u1-DDA"] = fakes.UnitScript(disposition="aif_hold")
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        runner.run(until_idle=True, max_iterations=3000)

        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u1-DDA", "u1-SWATH", "u2")],
                         ["split_parent", "disposition_held", "done", "done"])
        self.assertTrue(runner.idle())
        self.assertEqual(machine.remaining_work(book), [])
        self.assertFalse([name for name, _ in world.interactive.calls if name in ("release_split_parent", "discard")
                          and "u1" in str(_.get("manifest_path", "")) and "u1-" not in str(_.get("manifest_path", ""))],
                         "the parent keeps the raw data its held part needs")
        world.scripts["u1-DDA"].disposition = "run"
        book.add_request("recheck_held", "u1-DDA", "the patched Console is pinned", "Test Person", runner.stamp())
        self.assertEqual(machine.remaining_work(book), ["1 operator request(s) wait for a runner"])
        runner.run(until_idle=True, max_iterations=3000)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u1-DDA")], ["split_done", "done"])

    def test_an_operator_skip_of_a_held_split_part_releases_it_and_then_the_parent(self) -> None:
        """A held split part keeps its parent's raw data until it is released or run. Its skip releases it: the
        part's discard passes release_disposition_hold (deleting nothing; its raw data are its parent's), and
        the parent's release then goes ahead, told of the release too. Before, the part's skip called nothing,
        and the parent's release was refused for the held part for good."""
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(disposition="split")
        world.scripts["u1-DDA"] = fakes.UnitScript(disposition="aif_hold")
        world.interactive.split_release_supported = True
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        runner.run(until_idle=True, max_iterations=3000)
        self.assertEqual(book.unit("u1-DDA")["state"], "disposition_held")
        parent_raw = Path(world.interactive.store.read(book.unit("u1")["manifest_path"])["raw_directory"])
        self.assertTrue(parent_raw.is_dir())

        book.add_request("skip", "u1-DDA", "not waiting for the Console", "Test Person", runner.stamp())
        runner.run(until_idle=True, max_iterations=3000)

        part = book.unit("u1-DDA")
        self.assertEqual((part["state"], part["raw_disposition"]), ("skipped", "deferred_to_parent"))
        self.assertEqual(self.discards(world, book, "u1-DDA"), [True])
        manifest = world.interactive.store.read(part["manifest_path"])
        self.assertEqual((manifest["status"], manifest["disposition_hold_released_by"]), ("discarded", "operator_skip"))
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["raw_disposition"]), ("split_done", "released"))
        self.assertEqual([arguments["release_disposition_hold"] for name, arguments in world.interactive.calls
                          if name == "release_split_parent"], [True])
        self.assertFalse(parent_raw.exists())
        self.assertEqual(machine.remaining_work(book), [])

    def two_held_parts(self):
        """A split parent u1 whose two parts, u1-DDA and u1-SWATH, Interactive's disposition both holds."""
        world = self.world(("u1",))
        world.scripts["u1"] = fakes.UnitScript(disposition="split")
        world.scripts["u1-DDA"] = fakes.UnitScript(disposition="aif_hold")
        world.scripts["u1-SWATH"] = fakes.UnitScript(disposition="aif_hold")
        world.interactive.split_release_supported = True
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        runner.run(until_idle=True, max_iterations=3000)
        self.assertEqual([book.unit(key)["state"] for key in ("u1", "u1-DDA", "u1-SWATH")],
                         ["split_parent", "disposition_held", "disposition_held"])
        parent_raw = Path(world.interactive.store.read(book.unit("u1")["manifest_path"])["raw_directory"])
        self.assertTrue(parent_raw.is_dir())
        return world, book, runner, parent_raw

    def end_held_without_a_skip(self, world: fakes.World, book: ledger.Ledger, runner: machine.Runner, key: str) -> None:
        """The operator rechecks the part, and its preflight never answers (the backend breaks every time): the
        runner ends it failed after its retries, while Interactive's manifest keeps the hold it had."""
        real = world.interactive.preflight
        manifest = book.unit(key)["manifest_path"]

        def broken(**arguments):
            if arguments["manifest_path"] == manifest:
                raise RuntimeError("backend fault")
            return real(**arguments)

        world.interactive.preflight = broken
        book.add_request("recheck_held", key, "try again", "Test Person", runner.stamp())
        self.step_until(world, book, runner, lambda: book.unit(key)["state"] in ledger.TERMINAL_STATES, limit=4000)
        world.interactive.preflight = real
        self.assertEqual(book.unit(key)["state"], "failed")
        self.assertNotIn(machine.RELEASE_HOLD, json.loads(book.unit(key)["terminal_detail"] or "{}"))
        self.assertTrue(world.interactive.held_by_disposition(world.interactive.store.read(manifest)),
                        "Interactive still holds the part")

    def parent_releases(self, world: fakes.World, book: ledger.Ledger) -> list:
        manifest = book.unit("u1")["manifest_path"]
        return [(name, arguments["release_disposition_hold"]) for name, arguments in world.interactive.calls
                if name in ("release_split_parent", "discard") and arguments["manifest_path"] == manifest]

    def test_the_fake_release_lifts_every_held_part_as_interactive_does(self) -> None:
        """What makes the parent's flag dangerous: Interactive f225e9b's cleanup_split_parent, told
        release_disposition_hold, ends every part its disposition still holds as hold_released, recording
        disposition_hold_released_by operator_skip on each, skipped or not; without it, a held part refuses."""
        world, book, _runner, parent_raw = self.two_held_parts()
        parent = book.unit("u1")["manifest_path"]
        refused = world.interactive.release_split_parent(manifest_path=parent, authorization_path="a")
        self.assertEqual((refused["deleted"], refused["blockers"]), (False, ["disposition_held"]))
        self.assertTrue(parent_raw.is_dir())

        released = world.interactive.release_split_parent(manifest_path=parent, authorization_path="a",
                                                          release_disposition_hold=True)
        self.assertTrue(released["deleted"])
        for key in ("u1-DDA", "u1-SWATH"):
            manifest = world.interactive.store.read(book.unit(key)["manifest_path"])
            self.assertEqual(manifest["disposition_hold_released_by"], "operator_skip")

    def test_one_skipped_part_does_not_release_another_held_part_or_the_parent(self) -> None:
        """Review r9-32 of PR #32. Two held parts; u1-SWATH ends failed while still held, and the operator skips
        u1-DDA only. Before, the parent's release passed release_disposition_hold because ANY part carried an
        operator's skip, and Interactive then lifted u1-SWATH's hold too, recording operator_skip for a part no
        operator skipped, and deleted the raw tree u1-SWATH still needs. Now the flag goes only when every held
        part has its own skip: nothing is asked of Interactive for the parent, its raw tree is kept, and the
        parent is reported as waiting for u1-SWATH."""
        world, book, runner, parent_raw = self.two_held_parts()
        self.end_held_without_a_skip(world, book, runner, "u1-SWATH")
        self.assertEqual(book.unit("u1")["state"], "split_parent", "u1-DDA is held, so the parent has not ended")

        book.add_request("skip", "u1-DDA", "not waiting for the Console", "Test Person", runner.stamp())
        runner.run(until_idle=True, max_iterations=3000)

        dda = book.unit("u1-DDA")
        self.assertEqual((dda["state"], dda["raw_disposition"]), ("skipped", "deferred_to_parent"))
        self.assertEqual(self.discards(world, book, "u1-DDA"), [True], "the skipped part's own release")
        self.assertEqual(world.interactive.store.read(dda["manifest_path"])["disposition_hold_released_by"], "operator_skip")

        swath = world.interactive.store.read(book.unit("u1-SWATH")["manifest_path"])
        self.assertNotIn("disposition_hold_released_by", swath, "no operator skipped u1-SWATH")
        self.assertTrue(world.interactive.held_by_disposition(swath))
        self.assertEqual(self.discards(world, book, "u1-SWATH"), [])

        parent = book.unit("u1")
        self.assertEqual((parent["state"], parent["raw_disposition"]), ("split_done", "kept"))
        self.assertEqual(self.parent_releases(world, book), [], "neither released nor discarded, with or without the flag")
        self.assertTrue(parent_raw.is_dir(), "the parent's raw tree stays for u1-SWATH")
        self.assertIn("waiting for part u1-SWATH", parent["raw_detail"])
        self.assertEqual([json.loads(row["detail_json"])["parts"] for row in book.events("split_parent_waits_for_held_part")],
                         [["u1-SWATH"]])
        self.assertEqual(machine.remaining_work(book), [], "no recheck loop: only u1-SWATH's release changes the answer")

    def test_every_held_part_skipped_releases_the_parent_with_the_flag(self) -> None:
        """The rule's other side: once every held part has its own operator's skip, the parent's release goes
        ahead, told of the release."""
        world, book, runner, parent_raw = self.two_held_parts()
        book.add_request("skip", "u1-DDA", "not waiting for the Console", "Test Person", runner.stamp())
        runner.run(until_idle=True, max_iterations=3000)
        self.assertEqual(book.unit("u1")["state"], "split_parent", "u1-SWATH is still held")
        self.assertEqual(self.parent_releases(world, book), [])
        self.assertTrue(parent_raw.is_dir())

        book.add_request("skip", "u1-SWATH", "not waiting for the Console", "Test Person", runner.stamp())
        runner.run(until_idle=True, max_iterations=3000)

        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["raw_disposition"]), ("split_done", "discarded"))
        self.assertEqual(self.parent_releases(world, book), [("discard", True)])
        self.assertFalse(parent_raw.exists())
        for key in ("u1-DDA", "u1-SWATH"):
            self.assertEqual(self.discards(world, book, key), [True])

    def test_a_part_whose_recheck_ran_is_no_longer_held_and_keeps_no_parent_waiting(self) -> None:
        """A part whose recheck Interactive decided anew (run) is no longer held, so it does not keep the parent
        waiting: with the other held part skipped, the parent's release goes ahead."""
        world, book, runner, parent_raw = self.two_held_parts()
        world.scripts["u1-SWATH"].disposition = "run"
        book.add_request("recheck_held", "u1-SWATH", "the patched Console is pinned", "Test Person", runner.stamp())
        runner.run(until_idle=True, max_iterations=3000)
        self.assertEqual(book.unit("u1-SWATH")["state"], "done")
        book.add_request("skip", "u1-DDA", "not waiting for the Console", "Test Person", runner.stamp())
        runner.run(until_idle=True, max_iterations=3000)

        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["raw_disposition"]), ("split_done", "released"))
        self.assertEqual(self.parent_releases(world, book), [("release_split_parent", True)])
        self.assertFalse(parent_raw.exists())


class HoldDispositionRecordTests(unittest.TestCase):
    """policy.read_disposition reads Interactive 0.5.31's hold, and refuses one of another shape."""

    @staticmethod
    def record(**changes) -> dict:
        return {"schema": policy.DISPOSITION_SCHEMA, "disposition": "skip", "reasons": [REASON], "warnings": [],
                "excluded_inputs": [], "split_key": None, "decided_at": "2026-10-07T00:00:00+00:00",
                "extractor": {"sha256": "a" * 64}, "applied": True, "hold": True, **changes}

    def test_a_held_skip_is_held(self) -> None:
        disposition = policy.read_disposition({"campaign_disposition": self.record()})
        self.assertTrue(disposition.held)
        self.assertIs(disposition.as_dict()["hold"], True)
        self.assertEqual(policy.HOLD_FOR_CONSOLE, REASON)

    def test_a_disposition_before_0_5_31_holds_nothing(self) -> None:
        record = self.record()
        del record["hold"]
        disposition = policy.read_disposition({"campaign_disposition": record})
        self.assertFalse(disposition.held)
        self.assertIs(disposition.as_dict()["hold"], False)

    def test_a_hold_of_another_shape_is_refused(self) -> None:
        for changes, said in (({"hold": "yes"}, "hold is neither true nor false"),
                              ({"disposition": "run", "reasons": []}, "hold is true on a 'run' disposition")):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(policy.DispositionError, said):
                    policy.read_disposition({"campaign_disposition": self.record(**changes)})


if __name__ == "__main__":
    unittest.main()
