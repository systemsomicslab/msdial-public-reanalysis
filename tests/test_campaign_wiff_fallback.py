"""The runner's switch from a unit's .wiff2 to the .wiff beside them after a Console job failed (the user's decision of 2026-10-31).

The positive part of MB-POST MPST000048 (ZenoTOF 7600, DDA) ran every sample from its .wiff2, as the shared encoding rule
says, and its production run hung three times out of three in the .wiff2 reader, each time on other files. The .wiff stays
the fallback: where a diagnostic or production run of a unit or split part fails or times out and the unit has an attempt
left, the runner asks Interactive 0.5.55 (msdial_switch_repository_unit_to_wiff) to switch its inputs, and Interactive
switches a DDA unit whose every .wiff2 has its .wiff and the .wiff's .wiff.scan beside it, once, on record. The attempt that
failed on the .wiff2 counts as usual; the unit then resumes at its metadata preparation, so the analysis CSV, the
diagnostic and its threshold and the run bundle are made again on the .wiff, and the remaining attempts run on them. A DIA
unit, a .wiff without its .wiff.scan and an Interactive without the tool leave the retry exactly as it was.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes as fakes  # noqa: E402  (puts scripts/ on the path)
import test_campaign_machine as machine_tests  # noqa: E402
from campaign import machine, policy  # noqa: E402


class WiffFallbackTests(machine_tests.Base):
    def _calls(self, world: fakes.World, name: str) -> list[dict]:
        return [arguments for called, arguments in world.interactive.calls if called == name]

    def _console_kinds(self, world: fakes.World, unit: str) -> list[str]:
        return [kind for key, kind in world.interactive.console_starts if key == unit]

    def _switch_attempts(self, book, unit: str) -> list[dict]:
        return [row for row in book.attempts(unit) if row["step"] == machine.WIFF_FALLBACK_STEP]

    def test_a_failed_run_switches_once_and_the_retry_is_prepared_diagnosed_and_run_on_the_wiff(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(wiff2=True, runs=["timeout", "timeout", "ok"])
        book = self.finish(world)
        unit = book.unit("u1")

        # Two attempts failed, the first on the .wiff2 and counted as usual; the third ran on the .wiff.
        self.assertEqual((unit["state"], unit["failures"]), ("done", 2))
        self.assertEqual(["timeout", "timeout"], [row["outcome"] for row in book.attempts("u1") if row["counted"]])
        switches = self._calls(world, "switch_to_wiff")
        self.assertEqual(1, len(switches), "the switch is asked once: the second failure is on the .wiff already")
        self.assertEqual([[row["job_id"] for row in book.console_runs("u1") if row["kind"] == "production"][0]],
                         switches[0]["failed_job_ids"])
        # Metadata, the diagnostic and the run are made again after the switch, and the CSV is the .wiff's.
        prepared = self._calls(world, "prepare_metadata")
        self.assertEqual(2, len(prepared))
        self.assertTrue(all(item.endswith(".wiff2") for item in prepared[0]["input_candidates"]))
        self.assertTrue(all(item.endswith(".wiff") for item in prepared[1]["input_candidates"]))
        self.assertEqual(["diagnostic", "run", "diagnostic", "run", "run"], self._console_kinds(world, "u1"))
        resumed = [row["to_state"] for row in book.transitions("u1")]
        first_retry = resumed.index("waiting_retry")
        self.assertEqual("preflighted", resumed[first_retry + 1], "resumed at the metadata preparation")
        self.assertEqual("prepared", resumed[resumed.index("waiting_retry", first_retry + 1) + 1],
                         "a later failure on the .wiff retries the run as before")
        # On record: the attempt, the event, the failed attempt's detail, the unit record and the Catalog's runs.
        (switch,) = self._switch_attempts(book, "u1")
        self.assertEqual(("ok", 0), (switch["outcome"], switch["counted"]))
        summary = json.loads(switch["detail_json"])[machine.WIFF_FALLBACK]
        self.assertEqual(policy.WIFF_FALLBACK_RULE, summary["rule"])
        self.assertEqual([("S1.wiff2", "S1.wiff"), ("S2.wiff2", "S2.wiff")],
                         [(item["from"], item["to"]) for item in summary["swaps"]])
        self.assertEqual(1, len([row for row in book.events(machine.WIFF_FALLBACK) if row["unit_key"] == "u1"]))
        failed = next(row for row in book.attempts("u1") if row["counted"])
        self.assertEqual(policy.WIFF_FALLBACK_RULE, json.loads(failed["detail_json"])[machine.WIFF_FALLBACK]["rule"])
        # The unit record and the Catalog's runs read the switch from the manifest, with its boundary-4 crossing.
        record = json.loads((Path(unit["workspace"]) / "campaign-record.json").read_text(encoding="utf-8"))
        last = world.catalog.runs[f"u1:{unit['run_job_id']}"]
        for written in (record["encoding_fallback"], last["provenance"][machine.WIFF_FALLBACK]):
            self.assertEqual({key: summary[key] for key in ("rule", "swaps", "failed_jobs")},
                             {key: written[key] for key in ("rule", "swaps", "failed_jobs")})
            self.assertEqual((4, policy.WIFF_FALLBACK_ENTRY_POINT),
                             (written["campaign_authorization"]["boundary"], written["campaign_authorization"]["entry_point"]))
            self.assertEqual("DDA", written["wiff_headers"][0]["header_console_acquisition_type"])
        manifest = world.interactive.store.read(unit["manifest_path"])
        self.assertEqual(policy.WIFF_FALLBACK_RULE, manifest["encoding_fallback"]["rule"])
        self.assertEqual(["C:/fake/RawMetadataConsoleApp.exe"], world.interactive.switch_extractors,
                         "the pinned extractor reads the .wiff headers")

    def test_a_failed_diagnostic_switches_too(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(wiff2=True, diagnostics=["fail", "ok"])
        book = self.finish(world)

        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 1))
        self.assertEqual(1, len(self._calls(world, "switch_to_wiff")))
        self.assertEqual(2, len(self._calls(world, "prepare_metadata")))
        self.assertEqual(["diagnostic", "diagnostic", "run"], self._console_kinds(world, "u1"))

    def _unswitched(self, script: fakes.UnitScript, *, supported: bool = True) -> tuple:
        world = self.world()
        world.scripts["u1"] = script
        world.interactive.wiff_fallback_supported = supported
        book = self.finish(world)
        unit = book.unit("u1")
        self.assertEqual((unit["state"], unit["failures"]), ("done", 1))
        self.assertEqual(1, len(self._calls(world, "prepare_metadata")), "nothing is prepared again")
        self.assertEqual(["diagnostic", "run", "run"], self._console_kinds(world, "u1"), "the run is retried as before")
        self.assertFalse(book.events(machine.WIFF_FALLBACK))
        self.assertNotIn(machine.WIFF_FALLBACK, world.catalog.runs[f"u1:{unit['run_job_id']}"]["provenance"])
        record = json.loads((Path(unit["workspace"]) / "campaign-record.json").read_text(encoding="utf-8"))
        self.assertIsNone(record["encoding_fallback"])
        manifest = world.interactive.store.read(unit["manifest_path"])
        self.assertNotIn("encoding_fallback", manifest)
        return world, book

    def test_a_dia_unit_is_never_switched(self) -> None:
        world, book = self._unswitched(fakes.UnitScript(wiff2=True, acquisition="SWATH", runs=["timeout", "ok"]))
        (asked,) = self._switch_attempts(book, "u1")
        self.assertEqual(("blocked", 0), (asked["outcome"], asked["counted"]))
        self.assertEqual("not_dda", json.loads(asked["detail_json"])["reason"])

    def test_a_wiff_without_its_scan_is_never_switched_to(self) -> None:
        world, book = self._unswitched(fakes.UnitScript(wiff2=True, wiff_scan=False, runs=["fail", "ok"]))
        (asked,) = self._switch_attempts(book, "u1")
        self.assertEqual("wiff_scan_missing", json.loads(asked["detail_json"])["reason"])

    def test_an_interactive_without_the_tool_retries_as_before(self) -> None:
        world, book = self._unswitched(fakes.UnitScript(wiff2=True, runs=["timeout", "ok"]), supported=False)
        (asked,) = self._switch_attempts(book, "u1")
        self.assertEqual(("blocked", "unsupported"), (asked["outcome"], json.loads(asked["detail_json"])["reason"]))
        self.assertFalse(book.events("contract_held"), "a missing tool holds nothing")

    def test_a_unit_of_no_wiff2_is_not_asked(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(runs=["timeout", "ok"])
        book = self.finish(world)
        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 1))
        self.assertEqual([], self._calls(world, "switch_to_wiff"))
        self.assertEqual([], self._switch_attempts(book, "u1"))

    def test_no_switch_is_asked_for_an_attempt_that_will_not_be_retried(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(wiff2=True, acquisition="SWATH", runs=["timeout", "timeout", "timeout"])
        book = self.finish(world)
        self.assertEqual("failed", book.unit("u1")["state"])
        self.assertEqual(2, len(self._calls(world, "switch_to_wiff")), "the third failure has no attempt after it")

    def test_a_switch_whose_reply_was_lost_is_found_in_the_manifest(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(wiff2=True, runs=["timeout", "ok"])
        world.interactive.lose_switch_reply = True
        book = self.finish(world)

        self.assertEqual((book.unit("u1")["state"], book.unit("u1")["failures"]), ("done", 1))
        (switch,) = self._switch_attempts(book, "u1")
        self.assertEqual("ok", switch["outcome"])
        self.assertEqual(2, len(self._calls(world, "prepare_metadata")), "the unit is prepared again on the .wiff")

    def test_a_split_part_switches_on_its_own(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(wiff2=True, disposition="split", split_modes=("dda-pos", "dda-neg"))
        world.scripts["u1-dda-pos"] = fakes.UnitScript(wiff2=True, runs=["timeout", "ok"])
        world.scripts["u1-dda-neg"] = fakes.UnitScript(wiff2=True, runs=["ok"])
        # The parent's release checks that the parts hold its inputs, each once (review of #88): a switched part's
        # .wiff are read as the parent's .wiff2 they stand in for.
        world.interactive.split_release_supported = True
        book = self.finish(world)

        positive, negative = book.unit("u1-dda-pos"), book.unit("u1-dda-neg")
        self.assertEqual(("done", 1), (positive["state"], positive["failures"]))
        self.assertEqual(("done", 0), (negative["state"], negative["failures"]))
        self.assertEqual([positive["manifest_path"]], [item["manifest_path"] for item in self._calls(world, "switch_to_wiff")])
        self.assertTrue(world.interactive.store.read(positive["manifest_path"]).get("encoding_fallback"))
        self.assertFalse(world.interactive.store.read(negative["manifest_path"]).get("encoding_fallback"))
        self.assertFalse(world.interactive.store.read(book.unit("u1")["manifest_path"]).get("encoding_fallback"),
                         "the parent never runs, and is never switched")
        self.assertEqual(["diagnostic", "run", "diagnostic", "run"], self._console_kinds(world, "u1-dda-pos"))
        self.assertEqual(["diagnostic", "run"], self._console_kinds(world, "u1-dda-neg"))
        self.assertEqual(1, len(self._switch_attempts(book, "u1-dda-pos")))
        self.assertEqual([], self._switch_attempts(book, "u1-dda-neg"))
        parent = book.unit("u1")
        self.assertEqual((parent["state"], parent["raw_disposition"]), ("split_done", "released"),
                         "the parent's raw data are released once its parts end, a switched one included")

    def test_a_part_switched_from_its_raw_owners_downloads_and_none_without_them(self) -> None:
        """A part's manifest carries no downloads: the .wiff's provenance is its raw owner's record (review of #88)."""
        for downloaded, switched in ((True, True), (False, False)):
            with self.subTest(downloaded=downloaded):
                world = self.world()
                world.scripts["u1"] = fakes.UnitScript(wiff2=True, disposition="split", split_modes=("dda-pos", "dda-neg"),
                                                       wiff_downloaded=downloaded)
                world.scripts["u1-dda-pos"] = fakes.UnitScript(wiff2=True, runs=["timeout", "ok"])
                book = self.finish(world)
                part = world.interactive.store.read(book.unit("u1-dda-pos")["manifest_path"])
                self.assertNotIn("downloads", part)
                self.assertEqual(switched, bool(part.get("encoding_fallback")))
                (asked,) = self._switch_attempts(book, "u1-dda-pos")
                self.assertEqual("ok" if switched else "blocked", asked["outcome"])

    def test_a_header_that_does_not_support_it_leaves_the_retry_as_it_was(self) -> None:
        world, book = self._unswitched(fakes.UnitScript(wiff2=True, wiff_header="two_samples", runs=["timeout", "ok"]))
        (asked,) = self._switch_attempts(book, "u1")
        self.assertEqual("wiff_header_refused", json.loads(asked["detail_json"])["reason"])

    def test_a_lost_job_after_a_switch_is_prepared_again_before_any_console_starts(self) -> None:
        """Review of #88: a switch on record, and the run's job lost to a backend restart before the runner read its end.
        The interruption resumes the unit at prepared, whose CSV and threshold name the .wiff2; it goes back to its
        metadata preparation instead, and only then runs."""
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(wiff2=True, runs=["hold", "ok"], ticks=4)
        book = world.open()
        self.addCleanup(book.close)
        runner = world.runner(book)
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "running")
        unit = book.unit("u1")
        world.interactive.switch_to_wiff(manifest_path=unit["manifest_path"], failed_job_ids=[unit["run_job_id"]],
                                         authorization_path="")
        world.interactive.forget()
        self.step_until(world, book, runner, lambda: book.unit("u1")["state"] == "done", limit=800)

        transitions = [row["to_state"] for row in book.transitions("u1")]
        self.assertIn("preflighted", transitions[transitions.index("running") + 1:])
        self.assertEqual(2, len(self._calls(world, "prepare_metadata")))
        prepared = self._calls(world, "prepare_metadata")[1]["input_candidates"]
        self.assertTrue(all(item.endswith(".wiff") for item in prepared))
        self.assertEqual(["diagnostic", "run", "diagnostic", "run"], self._console_kinds(world, "u1"))

    def test_an_adoption_found_again_says_nothing_again(self) -> None:
        world = self.world()
        world.scripts["u1"] = fakes.UnitScript(wiff2=True, runs=["timeout", "ok"])
        world.interactive.lose_switch_reply = True
        book = self.finish(world)
        runner = world.runner(book)
        unit = book.unit("u1")
        job = json.loads(self._switch_attempts(book, "u1")[0]["detail_json"])["job_id"]
        self.assertTrue(runner._wiff_fallback(unit, job_id=job, step="run", outcome="timeout"))
        self.assertTrue(runner._wiff_fallback(unit, job_id=job, step="run", outcome="timeout"))
        self.assertEqual(1, len(book.events(machine.WIFF_FALLBACK)))
        self.assertEqual(1, len(self._switch_attempts(book, "u1")))


if __name__ == "__main__":
    unittest.main()
