"""Download-only units (plan --download-only-held, the author's decision of 2026-11-19).

"In Pool A, doubtful or hard units may be downloaded and kept without a run, provided each is recorded with its reason",
and mzXML-only units are downloaded and kept, their conversion to mzML coming later. These tests hold: which triage
reasons become download-only and which stay excluded, the totals and the summary that count them apart, the runner's
path download -> kept with no preflight and no Console, raw data never deleted for a kept unit (a sibling's cleanup in a
shared download group included), and the ledger's new state.

Only public accession and analysis names (the eval fixtures' ST002747) and synthetic names are used. Nothing is
downloaded and no Console runs.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes as fakes  # noqa: E402  (puts scripts/ on the path)
import test_campaign_contract as contract  # noqa: E402
import test_campaign_curated_selection as cs  # noqa: E402
import triage_eval as te  # noqa: E402
from campaign import curated, ledger, machine, plan, policy, ports  # noqa: E402

URL = "https://repository.example/download/group-shared.tar"


@unittest.skipUnless(contract.AVAILABLE, "the Interactive and Catalog checkouts are not where this test looks")
class PlanTests(cs._Root):
    def setUp(self) -> None:
        super().setUp()
        self.database = self.root / "catalog" / "catalog.sqlite"
        self.database.parent.mkdir()
        cs.build_catalog(self.database)

    def manifest(self, triage_dir: Path | None, *, held: bool = True) -> dict:
        catalog = ports.read_only_catalog(self.database)
        try:
            return plan.build_manifest(
                catalog, pool="declared", campaign_id="held", analysis_purpose="annotation",
                workspace_root=self.workspace_root, raw_retention_policy="delete_after_validated_output",
                pins={"catalog": {"version": "0.6.1"}, "libraries": [], "interactive": {"version": "0.5.69"}},
                profile=None, campaign_policy=policy.CampaignPolicy(),
                class_decision=lambda unit_id: ports.decide_class(catalog, unit_id, "annotation"),
                catalog_database=str(self.database), triage_dir=triage_dir, download_only_held=held,
            )
        finally:
            catalog.close()

    def test_units_of_an_accession_that_holds_are_planned_download_only_with_their_reasons(self) -> None:
        docs = te.build_good(cs.ACCESSION)
        docs["AN004456"]["status"] = "needs_person"
        docs["AN004456"]["needs_person"] = [{"code": "polarity_switching", "question": "Run pos and neg apart?"}]
        docs["AN004456"]["flags"] = [{"code": "polarity_switching", "certainty": "suspected", "detail": "switching"},
                                     {"code": "targeted_dda", "certainty": "suspected", "detail": "informational"}]
        manifest = self.manifest(cs.write_triage(self.root, docs))
        units = {unit["unit_id"]: unit for unit in manifest["units"]}
        self.assertEqual(sorted(units), list(cs.UNITS))
        self.assertEqual(manifest["exclusions"], [])
        for key, unit in units.items():
            self.assertNotIn("curated_selection", unit, "a download-only unit downloads the handoff's whole file list")
            block = unit["download_only"]
            self.assertEqual(block["plan_reason"], curated.TRIAGE_NOT_PASS)
            self.assertIn(curated.TRIAGE_NOT_PASS, block["reasons"])
            self.assertIn(f"validator:{block['validator_verdict']}", block["reasons"])
            self.assertEqual(block["triage_relative_path"], f"{cs.ACCESSION}/{key}.json")
        held = units["AN004456"]["download_only"]
        self.assertEqual(held["triage_status"], "needs_person")
        self.assertEqual(held["question_codes"], ["polarity_switching"])
        self.assertEqual(held["blocking_flags"], ["polarity_switching"], "an informational flag is not a blocking one")
        for code in ("status:needs_person", "question:polarity_switching", "blocking_flag:polarity_switching"):
            self.assertIn(code, held["reasons"])
        totals = manifest["totals"]
        self.assertEqual((totals["planned_units"], totals["run_units"]), (4, 0))
        self.assertEqual(totals["download_only"]["units"], 4)
        self.assertEqual(totals["download_only"]["by_plan_reason"], {curated.TRIAGE_NOT_PASS: 4})
        self.assertEqual(totals["download_only"]["raw_data"], "kept")
        self.assertEqual(totals["download_only"]["distinct_objects"], 1)
        self.assertEqual(manifest["selection"]["download_only_from"], list(curated.DOWNLOAD_ONLY_FROM))
        text = plan.summary_text(manifest, "sha256:" + "0" * 64)
        self.assertIn("DOWNLOAD-ONLY 4 unit(s), run units 0", text)
        self.assertIn("never boundary 4 (a run) or 5 (a deletion) for them", text)
        self.assertIn("a download-only unit's raw data are kept", text)
        self.assertIn("AN004456: triage_not_pass", text)
        events = plan.decision_events(manifest, "sha256:" + "0" * 64, {"approval_id": "A", "covers": ["1", "3", "4", "5"]})
        planned = [key for kind, _detail, key in events if kind == "download_only_planned"]
        self.assertEqual(sorted(planned), list(cs.UNITS))
        approved = [detail for kind, detail, _key in events if kind == "manifest_approved"][0]
        self.assertEqual(approved["download_only"], sorted(cs.UNITS))

    def test_a_unit_without_a_triage_is_planned_download_only(self) -> None:
        manifest = self.manifest(self.root / "empty-triage")
        self.assertEqual(manifest["exclusions"], [])
        self.assertEqual({unit["download_only"]["reasons"][0] for unit in manifest["units"]}, {curated.TRIAGE_MISSING})
        self.assertEqual(manifest["totals"]["download_only"]["by_plan_reason"], {curated.TRIAGE_MISSING: 4})

    def test_without_the_option_the_held_units_stay_excluded(self) -> None:
        manifest = self.manifest(cs.write_triage(self.root, listing=False), held=False)
        self.assertEqual(manifest["units"], [])
        self.assertNotIn("download_only", manifest["totals"])
        self.assertNotIn("download_only_from", manifest["selection"])

    def test_the_option_needs_the_triage(self) -> None:
        with self.assertRaisesRegex(plan.PlanError, "--triage-dir"):
            self.manifest(None)

    def test_a_unit_whose_own_triage_excludes_it_stays_excluded_in_an_accession_that_holds(self) -> None:
        docs = te.build_good(cs.ACCESSION)
        docs["AN004457"]["status"] = "excluded"
        docs["AN004457"]["exclusion"] = {"reason": "no_files", "detail": "no raw file"}
        manifest = self.manifest(cs.write_triage(self.root, docs, listing=False))
        self.assertEqual({item["unit_id"]: item["reason"] for item in manifest["exclusions"]},
                         {"AN004457": curated.TRIAGE_NOT_PASS})
        self.assertEqual(sorted(unit["unit_id"] for unit in manifest["units"]), ["AN004454", "AN004455", "AN004456"])


class RecordTests(unittest.TestCase):
    def test_only_missing_and_not_pass_become_download_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for reason in curated.EXCLUSION_REASONS:
                record = curated.download_only_record(accession_dir=root / "ACC", triage_root=root, unit_id="U1",
                                                      reason=reason, detail="", validation=None)
                self.assertEqual(record is not None, reason in (curated.TRIAGE_MISSING, curated.TRIAGE_NOT_PASS), reason)

    def test_an_mzxml_only_unit_out_of_scope_says_why(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ACC").mkdir()
            (root / "ACC" / "U1.json").write_text(json.dumps({
                "schema": "msdial-unit-triage.v2", "unit_id": "U1", "status": "out_of_scope", "needs_person": [],
                "flags": [], "excluded_rows": [{"reason": "requires_conversion"}, {"reason": "requires_conversion"}]}),
                encoding="utf-8")
            record = curated.download_only_record(accession_dir=root / "ACC", triage_root=root, unit_id="U1",
                                                  reason=curated.TRIAGE_NOT_PASS, detail="hold",
                                                  validation={"verdict": "hold"})
        self.assertEqual(record["reasons"], ["triage_not_pass", "validator:hold", "status:out_of_scope",
                                             "excluded_rows:requires_conversion"])
        self.assertEqual(record["excluded_row_reasons"], {"requires_conversion": 2})


def held_manifest(keys: list[str]) -> dict:
    return {"units": [{"unit_id": key, "unit_key": key, "accession": "ACC1", "download_only": {
        "reasons": ["triage_not_pass", "validator:hold", "status:needs_person", "question:polarity_switching"],
        "plan_reason": "triage_not_pass", "validator_verdict": "hold", "triage_status": "needs_person",
        "question_codes": ["polarity_switching"], "blocking_flags": [], "excluded_row_reasons": {},
        "triage_relative_path": f"ACC1/{key}.json", "triage_sha256": "t" * 64}} for key in keys]}


class RunnerTests(unittest.TestCase):
    def world(self, units, held, **options) -> fakes.World:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        return fakes.World(Path(self.directory.name), list(units), manifest=held_manifest(list(held)), **options)

    def book(self, world: fakes.World) -> ledger.Ledger:
        book = world.open()
        self.addCleanup(book.close)
        return book

    def test_a_download_only_unit_is_downloaded_and_kept_without_a_preflight_or_a_console(self) -> None:
        world = self.world(["u1", "u2"], ["u1"])
        world.gate.exits.update(pre_cleanup=0, final=0)
        world.run()
        book = self.book(world)
        kept, ran = book.unit("u1"), book.unit("u2")
        self.assertEqual((kept["state"], kept["terminal_reason"], kept["raw_disposition"]),
                         (ledger.KEPT, ledger.DOWNLOAD_ONLY_KEPT, "kept"))
        self.assertEqual((ran["state"], ran["raw_disposition"]), ("done", "released"))
        preflighted = [Path(call["manifest_path"]).parent.parent.name
                       for name, call in world.interactive.calls if name == "preflight"]
        self.assertEqual(preflighted, ["u2"])
        self.assertNotIn("u1", {unit for unit, _kind in world.interactive.console_starts})
        manifest = world.interactive.store.read(kept["manifest_path"])
        self.assertTrue(Path(manifest["raw_directory"]).is_dir(), "the kept unit's raw data are still there")
        self.assertNotIn(kept["manifest_path"], [call.get("manifest_path") for name, call in world.interactive.calls
                                                 if name in ("cleanup", "discard")])
        boundaries = {row["boundary"] for row in book.transitions("u1") if row["boundary"]}
        self.assertEqual(boundaries, {"1", "3"}, "a download-only unit crosses no run and no deletion boundary")
        events = [json.loads(row["detail_json"]) for row in book.events("download_only_kept")]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reasons"][0], "triage_not_pass")
        self.assertEqual(events[0]["downloaded_bytes"], kept["downloaded_bytes"])
        self.assertEqual(len(events[0]["raw_directories"]), 1)
        self.assertFalse(Path(events[0]["raw_directories"][0]).is_absolute(), "decision-ledger paths are workspace-relative")
        status = machine.summary(book)
        self.assertEqual(status[ledger.KEPT]["unit_keys"], ["u1"])
        self.assertIn("question:polarity_switching", status[ledger.KEPT]["reasons"])
        document, _tsv = machine.export_status(book)
        row = {item["unit_key"]: item for item in document["units"]}["u1"]
        self.assertIn(machine.KEPT_WARNING, row["warnings"])
        self.assertEqual(machine.remaining_work(book), [])
        record = json.loads((Path(kept["workspace"]) / "campaign-record.json").read_text(encoding="utf-8"))
        self.assertEqual((record["state"], record["raw_disposition"]), (ledger.KEPT, "kept"))

    def test_a_run_unit_sharing_the_download_never_takes_the_kept_units_copy(self) -> None:
        for order in (["u1", "u2"], ["u2", "u1"]):
            with self.subTest(order=order):
                world = self.world(order, ["u1"], download_groups={"group-shared": order})
                world.gate.exits.update(pre_cleanup=0, final=0)
                world.run()
                book = self.book(world)
                self.assertEqual((book.unit("u1")["state"], book.unit("u2")["state"]), (ledger.KEPT, "done"))
                self.assertEqual(len(world.interactive.fetches), 1, "the shared object is fetched once")
                self.assertEqual(world.interactive.claims[(URL, "u1")]["state"], "materialized")
                self.assertIn(URL, world.interactive.objects, "the store keeps what the kept unit's raw tree holds")
                raw = Path(world.interactive.store.read(book.unit("u1")["manifest_path"])["raw_directory"])
                self.assertTrue(raw.is_dir())

    def test_any_ending_after_the_download_keeps_the_raw_data(self) -> None:
        world = self.world(["u1"], ["u1"])
        world.run()
        with world.open() as book:
            runner = world.runner(book)
            unit = dict(book.unit("u1"))
            unit["pending_terminal"] = "skipped"
            raw, detail, boundary = runner._discard_raw(unit)
        self.assertEqual((raw, boundary), ("kept", None))
        self.assertIn("download-only", detail)

    def test_a_kept_unit_is_not_retried_into_a_run(self) -> None:
        world = self.world(["u1"], ["u1"])
        world.run()
        with world.open() as book:
            book.add_request("retry", "u1", "try it", "Test Person", "2026-11-20T00:00:00+00:00")
        world.run()
        book = self.book(world)
        self.assertEqual(book.unit("u1")["state"], ledger.KEPT)
        self.assertEqual([name for name, _call in world.interactive.calls if name == "preflight"], [])


if __name__ == "__main__":
    unittest.main()
