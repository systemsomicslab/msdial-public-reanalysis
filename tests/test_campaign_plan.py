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
from unittest import mock

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


# Pins an approvable plan records: a verified build of a pinned extractor, clean checkouts.
APPROVABLE_PINS = {
    "console": {"path": "C:/tools/MSDIALCUI.exe", "exists": True, "binary_sha256": "1" * 64},
    "extractor": {"path": "C:/tools/RawMetadataConsoleApp.exe", "exists": True, "binary_sha256": "2" * 64,
                  "inventory_sha256": "3" * 64, "provenance_status": "verified", "pinned": True},
    "libraries": [{"name": "P.msp", "sha256": "4" * 64, "bytes": 8}],
}
CODE_PINS = {
    "interactive": {"version": "0.5.19", "commit": "a" * 40, "dirty": False, "lease_uses_store": False},
    "catalog": {"version": "0.6.1", "commit": "b" * 40, "dirty": False},
    "gate": {"commit": "c" * 40, "dirty": False},
}
APPROVABLE_PINS.update(CODE_PINS)


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
        # uG's own workspace, of an earlier run no campaign made (as MTBLS2207's a22083b091a0ccd04489 is).
        legacy = self.workspace_root / "metabolights" / "MTBLSPRE" / "uG" / "provenance"
        legacy.mkdir(parents=True)
        (legacy / "run-manifest.json").write_text("{}", encoding="utf-8")

    def manifest(self, pool: str, replan: dict | None = None) -> dict:
        catalog = ports.read_only_catalog(self.database)
        try:
            return plan.build_manifest(
                catalog, pool=pool, campaign_id=f"test-{pool}", analysis_purpose="annotation", workspace_root=self.workspace_root,
                raw_retention_policy="delete_after_validated_output", pins={"catalog": {"version": "0.6.1"}, "libraries": []},
                profile=None, campaign_policy=policy.CampaignPolicy(),
                class_decision=lambda unit_id: ports.decide_class(catalog, unit_id, "annotation"),
                catalog_database=str(self.database), replan=replan,
            )
        finally:
            catalog.close()

    def campaign_workspace(self, repository: str, accession: str, unit_id: str, campaign_id: str) -> Path:
        """A unit workspace as the runner makes one: the authorization copy first, in provenance."""
        workspace = self.workspace_root / repository / accession / unit_id
        (workspace / "provenance").mkdir(parents=True)
        (workspace / "provenance" / "campaign-authorization.json").write_text(
            json.dumps({"schema": plan.AUTHORIZATION_SCHEMA, "campaign_id": campaign_id, "approval_id": "A0"}), encoding="utf-8")
        return workspace

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

    def test_a_campaigns_workspace_holds_back_only_its_own_unit(self) -> None:
        """uB and uC share ST000002. A campaign that took uB made its workspace; uC is planned as before,
        where the whole accession used to be excluded for good once any unit of it had run."""
        self.campaign_workspace("metabolomics_workbench", "ST000002", "uB", "earlier")
        (self.workspace_root / "metabolomics_workbench" / "ST000002" / "_dl").mkdir()  # Interactive's download store
        manifest = self.manifest("declared")
        exclusions = {item["unit_id"]: item for item in manifest["exclusions"]}
        self.assertEqual(exclusions["uB"]["reason"], "campaign_workspace")
        self.assertIn("made by campaign earlier", exclusions["uB"]["detail"])
        self.assertIn("uC", [unit["unit_id"] for unit in manifest["units"]])
        self.assertEqual(exclusions["uG"]["reason"], "preexisting_workspace")
        self.assertIn("uG, made by no campaign", exclusions["uG"]["detail"])

    def test_an_earlier_runs_workspace_holds_back_only_its_own_unit(self) -> None:
        """A workspace no campaign made excludes the unit it is the workspace of, or of one of whose split
        parts it is (<unit>-<part>, as MTBLS2207's -dda and -dia are), and no other unit of its accession."""
        accession = self.workspace_root / "metabolights" / "MTBLS1"
        (accession / "an-earlier-unit" / "provenance").mkdir(parents=True)
        self.assertIn("uA", [unit["unit_id"] for unit in self.manifest("declared")["units"]])
        (accession / "uA-dda" / "provenance").mkdir(parents=True)
        exclusions = {item["unit_id"]: item for item in self.manifest("declared")["exclusions"]}
        self.assertEqual(exclusions["uA"]["reason"], "preexisting_workspace")
        self.assertIn("uA-dda", exclusions["uA"]["detail"])

    def test_an_accession_level_workspace_is_listed_not_an_exclusion(self) -> None:
        """MTBLS341, ST002419 and MPST000008 hold an earlier accession-level run in the accession folder itself;
        their units' workspaces are folders of their own beside it, so the units are planned, and the
        accession is named in the manifest and in the text a person approves."""
        for accession in ("MTBLS1", "MTBLS5"):  # uA is planned; uE is excluded for ion mobility anyway
            (self.workspace_root / "metabolights" / accession / "provenance").mkdir(parents=True)
        manifest = self.manifest("declared")
        self.assertIn("uA", [unit["unit_id"] for unit in manifest["units"]])
        self.assertEqual(manifest["legacy_accession_workspaces"], ["metabolights/MTBLS1"])
        text = plan.summary_text(manifest, "sha256:" + "0" * 64)
        self.assertIn("planned beside an earlier accession-level workspace, left as it is: 1 accessions (metabolights/MTBLS1)", text)

    def test_a_revoked_campaigns_unfinished_units_are_planned_again(self) -> None:
        world = campaign_fakes.World(self.root, ["uB", "uC"])  # its ledger is analysis\_campaigns\test-campaign
        with world.open() as book:
            book.connection.execute("UPDATE unit SET state = 'failed', terminal_reason = 'download_failed' WHERE unit_key = 'uB'")
            book.connection.execute("UPDATE unit SET state = 'done', terminal_reason = 'outputs_produced' WHERE unit_key = 'uC'")
        for unit in ("uB", "uC"):
            self.campaign_workspace("metabolomics_workbench", "ST000002", unit, "test-campaign")
        with self.assertRaises(plan.PlanError, msg="a live campaign's units are its own"):
            plan.replan_states(self.workspace_root, ["test-campaign"])
        code, _out, err = self.cli("plan", "--campaign", "again", "--pool", "declared", "--purpose", "annotation",
                                   "--retention", "keep", "--catalog", str(self.database), "--replan-from", "test-campaign")
        self.assertEqual(code, runner_cli.EXIT_REFUSED)
        self.assertIn("not revoked", err)
        reasons = {item["unit_id"]: item["reason"] for item in self.manifest("declared")["exclusions"]}
        self.assertEqual((reasons["uB"], reasons["uC"]), ("campaign_workspace", "campaign_workspace"))
        with world.open() as book:
            book.revoke("approval-1", "Test Person", "the pins changed; planning again", "2026-10-02T00:00:00+00:00")
        replan = plan.replan_states(self.workspace_root, ["test-campaign"])
        manifest = self.manifest("declared", replan=replan)
        units = {unit["unit_id"]: unit for unit in manifest["units"]}
        self.assertEqual(units["uB"]["replanned_from"], {"campaign_id": "test-campaign", "state": "failed"})
        exclusions = {item["unit_id"]: item for item in manifest["exclusions"]}
        self.assertEqual(exclusions["uC"]["reason"], "campaign_workspace", "a unit that ended done is not run again")
        self.assertIn("where the unit is done", exclusions["uC"]["detail"])
        self.assertEqual(manifest["replanned_from"], ["test-campaign"])

    def test_the_summary_prints_the_transfer_the_lease_makes(self) -> None:
        manifest = self.manifest("declared")
        # The declared pool of the 2026-09-30 dry run: 12.82 TB fetched unit by unit, 6.99 TB distinct.
        manifest["totals"].update(per_unit_known_bytes=12_824_780_345_034, distinct_bytes_known=6_993_877_500_559)
        manifest["pins"]["interactive"] = {"version": "0.5.19", "lease_uses_store": False}
        text = plan.summary_text(manifest, "sha256:" + "0" * 64)
        self.assertIn("transfer and disk: at least 12.82 TB, each unit fetching its own copy", text)
        self.assertIn("Interactive 0.5.19's lease does not share objects", text)
        self.assertIn("distinct bytes, once the lease shares objects through the download store: 6.99 TB", text)
        # Once the lease fetches through the store (plan item 15), the distinct bytes are the transfer.
        manifest["pins"]["interactive"]["lease_uses_store"] = True
        text = plan.summary_text(manifest, "sha256:" + "0" * 64)
        self.assertIn("transfer and disk: at least 6.99 TB of known size, each shared object fetched once", text)
        self.assertIn("without the store the units would fetch 12.82 TB", text)

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
        for version in ("0.5.9", "0.6.0"):  # 0.6.1 chose mzXML over an unreadable twin: other Class digests
            with self.subTest(version), self.assertRaises(plan.PlanError):
                plan.build_manifest(
                    catalog, pool="declared", campaign_id="c", analysis_purpose="p", workspace_root=self.workspace_root,
                    raw_retention_policy="keep", pins={"catalog": {"version": version}}, profile=None,
                    campaign_policy=policy.CampaignPolicy(), class_decision=lambda unit: {}, catalog_database="x",
                )

    def test_a_dirty_checkout_or_an_unpinned_extractor_is_not_approvable(self) -> None:
        manifest = self.manifest("declared")
        manifest["pins"].update(APPROVABLE_PINS)
        manifest["profile"] = {"schema": plan.PROFILE_SCHEMA, "answers": {}, "by_ion_mode": {}}
        self.assertEqual(plan.approval_problems(manifest, ["1", "3", "4", "5"]), [], "the approvable baseline")
        cases = {
            "gate dirty": ("gate", {"commit": "c" * 40, "dirty": True}, "the gate checkout has uncommitted changes"),
            "interactive unreadable": ("interactive", {"version": "0.5.19", "commit": "", "dirty": None, "error": "not a git repository"},
                                       "the interactive checkout's state could not be read (not a git repository)"),
            "extractor unpinned": ("extractor", {**APPROVABLE_PINS["extractor"], "pinned": False},
                                   "not a verified build of one of Interactive's PINNED_BUILDS"),
            "extractor stale": ("extractor", {**APPROVABLE_PINS["extractor"], "provenance_status": "stale_mismatch"},
                                "provenance stale_mismatch"),
        }
        for name, (pin, value, words) in cases.items():
            with self.subTest(name):
                changed = json.loads(json.dumps(manifest))
                changed["pins"][pin] = value
                self.assertIn(words, "; ".join(plan.approval_problems(changed, ["1", "3", "4", "5"])))

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
        # The synthetic extractor has no build record, and this test's checkouts may be mid-edit: the pins
        # an approvable plan needs are read as a verified, pinned build and clean checkouts.
        extractor = {**APPROVABLE_PINS["extractor"], "path": str(tools / "RawMetadataConsoleApp.exe")}
        with mock.patch.object(ports.PinReader, "extractor", lambda _self: dict(extractor)), \
                mock.patch.object(ports.PinReader, "code", lambda _self: json.loads(json.dumps(CODE_PINS))):
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
        self.assertIn("While a unit is held it keeps running", out)
        # A request waits in the ledger for a runner's loop, and the command says so when none runs.
        code, out, _err = self.cli("retry", "--campaign", "c1", "--unit", "uA", "--reason", "why")
        self.assertEqual(code, 0)
        self.assertIn("no runner is running, so nothing acts on it until one is started", out)

    def test_each_operator_request_is_recorded_under_its_action(self) -> None:
        for command, action in (("skip", "skip"), ("retry", "retry"), ("release-held", "release_held")):
            with self.subTest(command=command):
                args = runner_cli.parser().parse_args([command, "--campaign", "c1", "--unit", "u1", "--reason", "why"])
                self.assertEqual((args.handler, args.action), (runner_cli.command_request, action))
        with tempfile.TemporaryDirectory() as directory:
            book = ledger.Ledger(Path(directory) / "ledger.sqlite", durable=False)
            self.addCleanup(book.close)
            book.connection.execute(
                "INSERT INTO unit(unit_key, catalog_unit_id, repository, accession, order_index, state, state_since, updated_at) "
                "VALUES ('u1', 'u1', 'r', 'a', 0, 'pending', 'now', 'now')")
            self.assertGreater(book.add_request("release_held", "u1", "why", "Test", "now"), 0, "the ledger takes it")
            book.close()

    def test_the_scheduled_command_keeps_a_path_with_a_space_one_word(self) -> None:
        line = runner_cli.schedule_task_command(r"C:\Program Files\Python314\python.exe", r"D:\code\scripts\campaign-runner.py", "c1")
        self.assertIn(r'/TR "\"C:\Program Files\Python314\python.exe\" \"D:\code\scripts\campaign-runner.py\" run --campaign c1 --until-idle"', line)
        self.assertIn("/SC ONLOGON", line)
        self.assertNotIn('""', line, "no unescaped quote inside /TR")

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
