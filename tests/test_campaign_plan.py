"""The campaign plan: which units, why the others are left out, in what order, and the approval of it.

A synthetic Catalog database stands in for the real one; it is built with the Catalog's own schema and
read back through the same read-only connection the plan uses, so nothing here depends on the live
catalog, and the plan provably writes nothing to it. The command line is driven end to end from `plan`
to `approve`, with a synthetic Console, extractor and libraries.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes  # noqa: E402,F401  (puts scripts/ on the path)
import test_campaign_contract as contract  # noqa: E402  (puts Interactive and the Catalog on the path)
from campaign import ledger, plan, policy, ports  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("campaign_runner", TESTS.parent / "scripts" / "campaign-runner.py")
assert _SPEC and _SPEC.loader
runner_cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner_cli)

GB = 1000**3
# unit id, accession, separation, acquisition, ion mode, ion mobility, instrument, untargeted, files
# (path, role, bytes, url)
UNITS = [
    ("uA", "MTBLS1", "LC-MS", "DDA", "Positive", "Unknown", "Agilent 6545 Q-TOF", None,
     [("A1.mzML", "raw", 3 * GB, "https://x/A1.mzML"), ("A2.mzML", "raw", 3 * GB, "https://x/A2.mzML")]),
    ("uB", "ST000002", "LC-MS", "AIF", "Negative", "Disabled", "Thermo Q Exactive", 1,
     [("B.raw", "shared_raw_archive", 0, "https://x/ST000002.zip")]),
    ("uC", "ST000002", "LC-MS", "SWATH", "Positive", "Unknown", "SCIEX 6600", None,
     [("C.wiff", "shared_raw_archive", 0, "https://x/ST000002.zip"), ("C2.wiff", "raw", 2 * GB, "https://x/C2.wiff")]),
    ("uD", "ST000004", "LC-MS", "DDA", "Positive", "Unknown", "Waters Xevo", None, []),
    ("uE", "MTBLS5", "LC-MS", "DDA", "Positive", "Enabled", "Agilent 6546", None, [("E.d", "raw", GB, "https://x/E.d.zip")]),
    ("uF", "MTBLS6", "LC-MS", "DDA", "Positive", "Unknown", "Bruker timsTOF Pro", None, [("F.d", "raw", GB, "https://x/F.zip")]),
    ("uG", "MTBLSPRE", "LC-MS", "DDA", "Positive", "Unknown", "Agilent", None, [("G.mzML", "raw", GB, "https://x/G.mzML")]),
    ("uH", "MTBLS8", "LC-MS", "DDA", "Negative", "Unknown", "Agilent", None, [("H.mzData", "raw", GB, "https://x/H.mzData")]),
    ("uI", "MTBLS9", "LC-MS", "DDA", "Positive", "Unknown", "Agilent", 0, [("I.mzML", "raw", GB, "https://x/I.mzML")]),
    ("uJ", "MTBLS10", "GC-MS", "DDA", "Positive", "Unknown", "Agilent", None, [("J.mzML", "raw", GB, "https://x/J.mzML")]),
    ("uK", "MTBLS11", "LC-MS", "Unknown", "Positive", "Unknown", "Agilent", None, [("K.mzXML", "raw", GB, "https://x/K.mzXML")]),
    ("uL", "MTBLS12", "LC-MS", "DDA", "Both", "Unknown", "Agilent", None, [("L.mzML", "raw", GB, "https://x/L.mzML")]),
    ("uM", "MTBLS13", "LC-MS", "DDA", "Positive", "Unknown", "Agilent", None, [("M.mzML", "raw", GB, "")]),
]


def build_catalog(path: Path) -> None:
    from msdial_repository_catalog.storage import Catalog

    with Catalog(path) as catalog:
        catalog.initialize()
    connection = sqlite3.connect(path)
    with connection:
        studies = {}
        for unit, accession, separation, acquisition, ion, mobility, instrument, untargeted, files in UNITS:
            repository = "metabolomics_workbench" if accession.startswith("ST") else "metabolights"
            if accession not in studies:
                studies[accession] = f"study-{accession}"
                connection.execute(
                    "INSERT INTO study(study_id, repository, accession, source_hash) VALUES (?, ?, ?, 'h')",
                    (studies[accession], repository, accession),
                )
            connection.execute(
                "INSERT INTO analysis_unit(unit_id, study_id, source_subrecord_id, separation, acquisition_mode, ion_mode, "
                "ion_mobility, instrument, untargeted, signature, target_omics) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Metabolomics')",
                (unit, studies[accession], unit, separation, acquisition, ion, mobility, instrument, untargeted, unit),
            )
            for index, (name, role, size, url) in enumerate(files):
                sample = f"{unit}-S{index}"
                connection.execute(
                    "INSERT INTO sample(sample_pk, unit_id, sample_id, raw_file) VALUES (?, ?, ?, ?)",
                    (f"pk-{sample}", unit, sample, name),
                )
                connection.execute(
                    "INSERT INTO raw_file(file_id, unit_id, path, role, size_bytes, download_url, sample_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (f"f-{unit}-{index}", unit, name, role, size, url, sample),
                )
    connection.close()


@unittest.skipUnless(contract.AVAILABLE, "the Interactive and Catalog checkouts are not where this test looks")
class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / "catalog" / "catalog.sqlite"
        self.database.parent.mkdir()
        build_catalog(self.database)
        self.workspace_root = self.root / "analysis"
        (self.workspace_root / "metabolights" / "MTBLSPRE").mkdir(parents=True)

    def manifest(self, pool: str) -> dict:
        catalog = ports.read_only_catalog(self.database)
        try:
            return plan.build_manifest(
                catalog, pool=pool, campaign_id=f"test-{pool}", analysis_purpose="annotation", workspace_root=self.workspace_root,
                raw_retention_policy="delete_after_validated_output", pins={"catalog": {"version": "0.6.1"}, "libraries": []},
                profile=None, campaign_policy=policy.CampaignPolicy(),
                class_decision=lambda unit_id: ports.decide_class(catalog, unit_id, "annotation"),
                catalog_database=str(self.database),
            )
        finally:
            catalog.close()

    def test_the_declared_pool_and_its_exclusions(self) -> None:
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        manifest = self.manifest("declared")
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before, "the plan writes nothing to the catalog")
        reasons = {item["unit_id"]: item["reason"] for item in manifest["exclusions"]}
        self.assertEqual(reasons, {
            "uD": "no_files", "uE": "ion_mobility", "uF": "ion_mobility", "uG": "preexisting_workspace",
            "uH": "mzdata_only", "uM": "no_download_object",
        })
        planned = [unit["unit_id"] for unit in manifest["units"]]
        self.assertEqual(sorted(planned), ["uA", "uB", "uC"])
        self.assertEqual(manifest["totals"]["selected_units"], 9, "untargeted, GC-MS, Both and Unknown are not selected")
        self.assertEqual(abs(planned.index("uB") - planned.index("uC")), 1, "a download group's units run one after another")
        units = {unit["unit_id"]: unit for unit in manifest["units"]}
        self.assertEqual(units["uB"]["group_id"], units["uC"]["group_id"])
        self.assertNotEqual(units["uA"]["group_id"], units["uB"]["group_id"])
        self.assertFalse(units["uC"]["size_known"], "a listed size of 0 is unknown, never free")
        self.assertTrue(units["uA"]["size_known"])
        self.assertTrue(units["uB"]["has_archive"])
        self.assertEqual(units["uA"]["known_bytes"], 6 * GB)
        self.assertEqual([unit["order_index"] for unit in manifest["units"]], [0, 1, 2])
        self.assertTrue(all(unit["approved_class_digest"] for unit in manifest["units"]))
        totals = manifest["totals"]
        self.assertEqual((totals["planned_units"], totals["download_groups"], totals["distinct_objects"]), (3, 2, 4))
        self.assertEqual((totals["distinct_bytes_known"], totals["unknown_size_objects"], totals["units_of_unknown_size"]),
                         (8 * GB, 1, 2))

    def test_the_acquisition_unknown_pool_is_its_own_manifest(self) -> None:
        manifest = self.manifest("acquisition_unknown")
        self.assertEqual([unit["unit_id"] for unit in manifest["units"]], ["uK"], "mzXML is converted and runs")
        self.assertEqual(manifest["units"][0]["selection_basis"], "acquisition_unknown")

    def test_the_class_digest_is_the_catalogs_own_decision(self) -> None:
        manifest = self.manifest("declared")
        catalog = ports.read_only_catalog(self.database)
        try:
            for unit in manifest["units"]:
                self.assertEqual(ports.decide_class(catalog, unit["unit_id"], "annotation")["proposal_id"], unit["approved_class_digest"])
        finally:
            catalog.close()

    def test_an_old_catalog_is_refused(self) -> None:
        catalog = ports.read_only_catalog(self.database)
        self.addCleanup(catalog.close)
        with self.assertRaises(plan.PlanError):
            plan.build_manifest(
                catalog, pool="declared", campaign_id="c", analysis_purpose="p", workspace_root=self.workspace_root,
                raw_retention_policy="keep", pins={"catalog": {"version": "0.5.9"}}, profile=None,
                campaign_policy=policy.CampaignPolicy(), class_decision=lambda unit: {}, catalog_database="x",
            )

    def test_profiles(self) -> None:
        good = {"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "existing"},
                "by_ion_mode": {"Positive": {"libraries": {"msp_paths": ["library:P.msp"]}}}}
        self.assertEqual(plan.profile_problems(good, ["P.msp"]), [])
        self.assertTrue(plan.profile_problems(good, []))
        located = json.loads(json.dumps(good))
        located["by_ion_mode"]["Positive"]["libraries"]["msp_paths"] = [r"E:\libs\P.msp"]
        self.assertIn("names a location", " ".join(plan.profile_problems(located, ["P.msp"])))
        self.assertTrue(plan.profile_problems(None, []))

    def cli(self, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = runner_cli.main([
                "--workspace-root", str(self.workspace_root), "--interactive-root", str(contract.INTERACTIVE_ROOT),
                "--catalog-root", str(contract.CATALOG_ROOT), *arguments,
            ])
        return code, out.getvalue(), err.getvalue()

    def test_plan_then_approve_from_the_command_line(self) -> None:
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "MSDIALCUI.exe").write_bytes(b"MZ console")
        (tools / "RawMetadataConsoleApp.exe").write_bytes(b"MZ extractor")
        vault = self.root / "vault of private things"
        vault.mkdir()
        for name in ("P.msp", "N.msp"):
            (vault / name).write_text("NAME: x\n", encoding="utf-8")
        resources = self.root / "campaign-resources.local.json"
        resources.write_text(json.dumps({"schema": ports.RESOURCES_SCHEMA, "libraries": {
            "P.msp": str(vault / "P.msp"), "N.msp": str(vault / "N.msp")}}), encoding="utf-8")
        profile = self.root / "profile.json"
        profile.write_text(json.dumps({"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "existing"},
                                       "by_ion_mode": {"Positive": {"libraries": {"msp_paths": ["library:P.msp"]}},
                                                       "Negative": {"libraries": {"msp_paths": ["library:N.msp"]}}}}),
                           encoding="utf-8")
        code, out, err = self.cli(
            "plan", "--campaign", "c1", "--pool", "declared", "--purpose", "annotation",
            "--retention", "delete_after_validated_output", "--catalog", str(self.database),
            "--console", str(tools / "MSDIALCUI.exe"), "--extractor", str(tools / "RawMetadataConsoleApp.exe"),
            "--resources", str(resources), "--profile", str(profile),
        )
        self.assertEqual(code, 0, err)
        directory = self.workspace_root / "_campaigns" / "c1"
        manifest_path = directory / "campaign-manifest.json"
        text = manifest_path.read_text(encoding="utf-8")
        self.assertNotIn("vault of private things", text, "the manifest names libraries, never their location")
        digest = "sha256:" + hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        self.assertIn(digest, out)
        self.assertNotIn("Not approvable", err)

        code, _out, err = self.cli("approve", "--campaign", "c1", "--digest", "sha256:" + "0" * 64,
                                   "--approval-id", "A1", "--by", "Test Person", "--statement", "Yes.", "--covers", "1,3,4,5,split")
        self.assertEqual(code, runner_cli.EXIT_REFUSED)
        self.assertFalse((directory / "ledger.sqlite").exists())
        code, _out, err = self.cli("approve", "--campaign", "c1", "--digest", digest, "--approval-id", "A1",
                                   "--by", "Test Person", "--statement", "Yes.", "--covers", "1,3,4,5,split,6")
        self.assertEqual(code, runner_cli.EXIT_REFUSED, "boundary 6 is never covered")
        code, out, err = self.cli("approve", "--campaign", "c1", "--digest", digest, "--approval-id", "A1",
                                  "--by", "Test Person", "--statement", "Yes, run it.", "--covers", "1,3,4,5,split")
        self.assertEqual(code, 0, err)
        with ledger.Ledger(directory / "ledger.sqlite") as book:
            self.assertEqual(sorted(unit["unit_key"] for unit in book.units()), ["uA", "uB", "uC"])
            self.assertEqual(book.approval()["approval_id"], "A1")
            self.assertEqual(book.campaign()["manifest_digest"], digest)
        record = json.loads((directory / "campaign-authorization.json").read_text(encoding="utf-8"))
        self.assertEqual((record["approval_id"], record["manifest_digest"], sorted(record["units"])), ("A1", digest, ["uA", "uB", "uC"]))
        self.assertNotIn("vault", json.dumps(record))
        self.assertEqual(self.cli("approve", "--campaign", "c1", "--digest", digest, "--approval-id", "A2", "--by", "x",
                                  "--statement", "y", "--covers", "1")[0], runner_cli.EXIT_REFUSED, "one approval per campaign")
        self.assertEqual(self.cli("plan", "--campaign", "c1", "--pool", "declared", "--purpose", "p", "--retention", "keep",
                                  "--catalog", str(self.database))[0], runner_cli.EXIT_REFUSED, "an approved manifest is not replaced")
        code, out, _err = self.cli("status", "--campaign", "c1")
        self.assertEqual(json.loads(out)["states"], {"pending": 3})
        code, out, _err = self.cli("schedule-command", "--campaign", "c1")
        self.assertIn("schtasks /Create", out)
        self.assertIn("runs none of them", out)

    def test_a_manifest_without_libraries_or_profile_is_not_approvable(self) -> None:
        code, _out, err = self.cli(
            "plan", "--campaign", "c2", "--pool", "declared", "--purpose", "annotation", "--retention", "keep",
            "--catalog", str(self.database),
        )
        self.assertEqual(code, 0)
        self.assertIn("Not approvable", err)
        digest = json.loads((self.workspace_root / "_campaigns" / "c2" / "campaign-manifest.summary.json").read_text())["manifest_digest"]
        code, _out, err = self.cli("approve", "--campaign", "c2", "--digest", digest, "--approval-id", "A", "--by", "x",
                                   "--statement", "y", "--covers", "1,3,4")
        self.assertEqual(code, runner_cli.EXIT_REFUSED)
        self.assertIn("pins no library", err)


if __name__ == "__main__":
    unittest.main()
