"""The campaign runner with a Console that has MsdialWorkbench #825 (Interactive 0.5.34, msdial-interactive-app #67).

A multi-energy AIF unit was held (aif_multi_ce_awaiting_console) until a patched Console exists; #825 is that
Console. Interactive 0.5.34 decides such a unit for the Console its preflight is given, else its saved setting:
with #825 the disposition is "run", AIF, and records aif_multi_ce_run beside its probe of the Console
(multi_energy_aif_console); without #825 the hold stays. The runner sends the pinned Console's path, so the unit
is decided for the Console that will run it, and an operator's recheck-held of a held unit brings it through once
that Console has #825. The fakes decide as Interactive 0.5.34 does (campaign_fakes, disposition "aif_multi_ce").
Nothing here downloads a byte or starts a Console.
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes as fakes  # noqa: E402
import test_campaign_contract as contract  # noqa: E402  (puts Interactive and the Catalog on the path)
from campaign import ledger, machine, plan, policy, ports  # noqa: E402
from test_campaign_machine import Base  # noqa: E402

PINNED = "C:/fake/MSDIALCUI.exe"
RULE = "multi_ce_aif_with_console_825"
HOLD = "aif_multi_ce_awaiting_console"


class MultiEnergyAifRunnerTests(Base):
    def campaign(self, *, consoles_825=(PINNED,), saved="", takes_console=True):
        world = self.world(("u1", "u2"))
        world.scripts["u1"] = fakes.UnitScript(disposition="aif_multi_ce")
        world.consoles_825 = set(consoles_825)
        world.saved_console = saved
        world.preflight_takes_console = takes_console
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        runner.run(until_idle=True, max_iterations=2000)
        return world, book, runner

    def preflight_consoles(self, world: fakes.World, book: ledger.Ledger, key: str) -> list[str]:
        manifest = book.unit(key)["manifest_path"]
        return [arguments["console_path"] for name, arguments in world.interactive.calls
                if name == "preflight" and arguments["manifest_path"] == manifest]

    def test_the_preflight_is_decided_for_the_pinned_console(self) -> None:
        world, book, _runner = self.campaign()

        self.assertEqual(self.preflight_consoles(world, book, "u1"), [PINNED])
        self.assertEqual(book.unit("u1")["state"], "done")
        record = json.loads(book.unit("u1")["disposition_json"])
        self.assertEqual(record["disposition"], "run")
        self.assertEqual(record["aif_multi_ce_run"], {"collision_energies": [10.0, 20.0], "rule": RULE})
        self.assertIs(record["multi_energy_aif_console"]["available"], True)
        self.assertNotIn("console_path", record["multi_energy_aif_console"], "a local path stays out of the record")
        self.assertEqual(book.unit("u2")["state"], "done")

    def test_status_counts_the_multi_energy_aif_runs(self) -> None:
        _world, book, _runner = self.campaign()
        summary = machine.summary(book)

        self.assertEqual(summary["multi_energy_aif_runs"], {"units": 1, "unit_keys": ["u1"], "rule": RULE})
        self.assertEqual(summary["disposition_held"]["units"], 0)
        document, _tsv = machine.export_status(book)
        row = next(item for item in document["units"] if item["unit_key"] == "u1")
        self.assertEqual(row["disposition"]["aif_multi_ce_run"]["rule"], RULE)

    def test_the_preflight_attempt_records_the_rule(self) -> None:
        _world, book, _runner = self.campaign()
        attempts = [json.loads(row["detail_json"] or "{}") for row in book.attempts("u1") if row["step"] == "preflight"]

        self.assertEqual(attempts[-1]["aif_multi_ce_run"]["rule"], RULE)

    def test_a_pinned_console_without_825_holds_the_unit(self) -> None:
        world, book, _runner = self.campaign(consoles_825=())
        unit = book.unit("u1")

        self.assertEqual((unit["state"], unit["resume_state"]), ("disposition_held", "downloaded"))
        record = json.loads(unit["disposition_json"])
        self.assertEqual((record["disposition"], record["hold"], record["reasons"]), ("skip", True, [HOLD]))
        self.assertEqual(record["multi_energy_aif_console"]["probe"], "marker_absent")
        self.assertNotIn("aif_multi_ce_run", record)
        self.assertFalse([start for start in world.interactive.console_starts if start[0] == "u1"])
        transition = json.loads(book.transitions("u1")[-1]["detail_json"])
        self.assertEqual(transition["console_multi_energy_aif"], "marker_absent")
        self.assertEqual(machine.summary(book)["multi_energy_aif_runs"]["units"], 0)

    def test_an_operator_recheck_brings_a_held_unit_through_with_the_825_console(self) -> None:
        """Held when it was decided for another Console (Interactive's saved setting, as for a runner that sent
        no Console path); the recheck is decided for the pinned one, which has #825."""
        world, book, runner = self.campaign(saved="C:/old/MSDIALCUI.exe", takes_console=False)
        self.assertEqual(book.unit("u1")["state"], "disposition_held")
        world.preflight_takes_console = True
        book.add_request("recheck_held", "u1", "the #825 Console is pinned", "Test Person", runner.stamp())
        runner.run(until_idle=True, max_iterations=2000)

        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"]), ("done", 0))
        self.assertEqual(self.preflight_consoles(world, book, "u1"), [PINNED, PINNED])
        lifted = [json.loads(row["detail_json"]) for row in book.events(machine.DISPOSITION_HOLD_LIFTED)
                  if row["unit_key"] == "u1"]
        self.assertEqual(len(lifted), 1)
        self.assertEqual(lifted[0]["held_for"], [HOLD])
        self.assertEqual(lifted[0]["disposition"], "run")
        self.assertEqual(lifted[0]["aif_multi_ce_run"]["rule"], RULE)
        self.assertEqual(lifted[0]["console_multi_energy_aif"]["available"], True)
        self.assertEqual(machine.summary(book)["multi_energy_aif_runs"]["unit_keys"], ["u1"])

    def test_a_recheck_with_a_console_still_without_825_waits_again(self) -> None:
        world, book, runner = self.campaign(consoles_825=())
        book.add_request("recheck_held", "u1", "ask again", "Test Person", runner.stamp())
        runner.run(until_idle=True, max_iterations=2000)

        self.assertEqual(book.unit("u1")["state"], "disposition_held")
        self.assertFalse([row for row in book.events(machine.DISPOSITION_HOLD_LIFTED) if row["unit_key"] == "u1"])
        self.assertEqual(len(self.preflight_consoles(world, book, "u1")), 2)


class DispositionRecordTests(unittest.TestCase):
    def record(self, **extra) -> dict:
        return {"schema": policy.DISPOSITION_SCHEMA, "disposition": "run", "reasons": [], "warnings": [],
                "excluded_inputs": [], "split_key": None, "decided_at": "2026-10-08T00:00:00+09:00",
                "extractor": {"sha256": "a" * 64}, "applied": True, **extra}

    def test_the_multi_energy_fields_are_kept_and_the_probe_loses_its_path(self) -> None:
        probe = {"capability": "multi_energy_aif_representative_collision_energy", "available": True,
                 "console_path": r"D:\a\local\MSDIALCUI.exe", "console_source": "argument",
                 "console_assembly": "MSDIALCUI.exe", "assembly_sha256": "b" * 64, "probe": "multi_energy_aif_marker"}
        disposition = policy.read_disposition({"campaign_disposition": self.record(
            aif_multi_ce_run={"collision_energies": [10.0, 20.0], "rule": RULE}, multi_energy_aif_console=probe)})
        record = disposition.as_dict()

        self.assertEqual(record["aif_multi_ce_run"], {"collision_energies": [10.0, 20.0], "rule": RULE})
        self.assertEqual(set(record["multi_energy_aif_console"]), set(policy.MULTI_ENERGY_AIF_PROBE_FIELDS))
        self.assertNotIn("local", json.dumps(record))

    def test_a_record_without_them_is_as_before(self) -> None:
        record = policy.read_disposition({"campaign_disposition": self.record()}).as_dict()

        self.assertNotIn("aif_multi_ce_run", record)
        self.assertNotIn("multi_energy_aif_console", record)


class InteractivePortTests(unittest.TestCase):
    """The pinned Console's path reaches the preflight and classify_preflight only where Interactive takes it."""

    def port(self, tools, rr=None) -> ports.InteractivePort:
        port = object.__new__(ports.InteractivePort)
        port.tools, port.rr, port.ca, port.host, port.port = tools, rr, None, "127.0.0.1", 8766
        return port

    def test_the_preflight_sends_the_console_to_an_interactive_that_takes_it(self) -> None:
        seen = {}

        def preflight(manifest_path, extractor_path="", max_inputs=0, confirm_untargeted=False,
                      campaign_authorization_path="", console_path="", host="", port=0):
            seen.update(console_path=console_path)
            return {"completed": True}

        port = self.port(types.SimpleNamespace(msdial_repository_raw_metadata_preflight=preflight))
        port.preflight(manifest_path="m.json", extractor_path="x.exe", authorization_path="a.json", console_path=PINNED)
        self.assertEqual(seen, {"console_path": PINNED})

    def test_an_interactive_before_0_5_34_is_not_sent_it(self) -> None:
        def preflight(manifest_path, extractor_path="", max_inputs=0, confirm_untargeted=False,
                      campaign_authorization_path="", host="", port=0):
            return {"completed": True}

        port = self.port(types.SimpleNamespace(msdial_repository_raw_metadata_preflight=preflight))
        result = port.preflight(manifest_path="m.json", extractor_path="x.exe", authorization_path="a.json",
                                console_path=PINNED)
        self.assertEqual(result, {"completed": True})

    def test_classify_sends_the_console_only_where_it_is_taken(self) -> None:
        seen = []

        def new(manifest_path, campaign_authorization_path=None, *, console_path=None):
            seen.append(console_path)
            return {"applied": True}

        def old(manifest_path, campaign_authorization_path=None):
            seen.append("old")
            return {"applied": True}

        for function in (new, old):
            port = self.port(None, types.SimpleNamespace(classify_preflight=function))
            self.assertTrue(port.classify(manifest_path="m.json", authorization_path="a.json", console_path=PINNED)["ok"])
        self.assertEqual(seen, [PINNED, "old"])


class PinTests(unittest.TestCase):
    def test_the_plan_says_what_the_pinned_console_means_for_multi_energy_aif(self) -> None:
        self.assertIn(RULE, plan.multi_energy_aif_text({"multi_energy_aif": {"available": True, "probe": "multi_energy_aif_marker"}}))
        held = plan.multi_energy_aif_text({"multi_energy_aif": {"available": False, "probe": "marker_incomplete"}})
        self.assertIn("no MsdialWorkbench #825 (probe marker_incomplete)", held)
        self.assertIn(HOLD, held)
        self.assertIn("not probed", plan.multi_energy_aif_text({"multi_energy_aif": None}))

    @unittest.skipUnless(contract.AVAILABLE, "the Interactive checkout is not where this test looks")
    def test_the_console_pin_records_interactives_probe(self) -> None:
        from msdial_app import workflow

        with tempfile.TemporaryDirectory() as directory:
            console = Path(directory) / "MSDIALCUI.exe"
            console.write_bytes(b"MZ")
            reader = ports.PinReader(console_path=str(console), extractor_path="", libraries={})
            recorded = reader.console()["multi_energy_aif"]
            if not hasattr(workflow, "multi_energy_aif_console"):
                self.assertIsNone(recorded, "an Interactive before 0.5.34 has no probe")
                return
            self.assertEqual(recorded, {"available": False, "probe": "marker_absent"})
            console.write_bytes(b"MZ" + b"".join(marker.encode("utf-16-le")
                                                 for marker in workflow.MULTI_ENERGY_AIF_CONSOLE_MARKERS) + b"!")
            self.assertEqual(reader.console()["multi_energy_aif"], {"available": True, "probe": "multi_energy_aif_marker"})


if __name__ == "__main__":
    unittest.main()
