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

    def test_an_operator_skip_ends_it_as_any_skip(self) -> None:
        world, book, runner = self.held()
        book.add_request("skip", "u1", "not waiting for the Console", "Test Person", runner.stamp())
        runner.run(until_idle=True, max_iterations=2000)

        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["raw_disposition"]), ("skipped", "discarded"))

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
