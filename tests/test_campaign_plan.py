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
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes  # noqa: E402,F401  (puts scripts/ on the path)
import test_campaign_contract as contract  # noqa: E402  (puts Interactive and the Catalog on the path)
from campaign import ledger, machine, plan, policy, ports  # noqa: E402

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
    ("uE", "MTBLS5", "LC-MS", "DDA", "Positive", "Enabled", "Agilent 6560 Ion Mobility Q-TOF", None, [("E.d", "raw", GB, "https://x/E.d.zip")]),
    ("uF", "MTBLS6", "LC-MS", "DDA", "Positive", "Unknown", "Bruker timsTOF Pro", None, [("F.d", "raw", GB, "https://x/F.zip")]),
    ("uG", "MTBLSPRE", "LC-MS", "DDA", "Positive", "Unknown", "Agilent", None, [("G.mzML", "raw", GB, "https://x/G.mzML")]),
    ("uH", "MTBLS8", "LC-MS", "DDA", "Negative", "Unknown", "Agilent", None, [("H.mzData", "raw", GB, "https://x/H.mzData")]),
    ("uI", "MTBLS9", "LC-MS", "DDA", "Positive", "Unknown", "Agilent", 0, [("I.mzML", "raw", GB, "https://x/I.mzML")]),
    ("uJ", "MTBLS10", "GC-MS", "DDA", "Positive", "Unknown", "Agilent", None, [("J.mzML", "raw", GB, "https://x/J.mzML")]),
    ("uK", "MTBLS11", "LC-MS", "Unknown", "Positive", "Unknown", "Agilent", None, [("K.mzXML", "raw", GB, "https://x/K.mzXML")]),
    # An ion mode the Catalog could not read: no pool admits it (Both is, since 2026-10-29).
    ("uL", "MTBLS12", "LC-MS", "DDA", "Unknown", "Unknown", "Agilent", None, [("L.mzML", "raw", GB, "https://x/L.mzML")]),
    ("uM", "MTBLS13", "LC-MS", "DDA", "Positive", "Unknown", "Agilent", None, [("M.mzML", "raw", GB, "")]),
]


def add_unit(
    path: Path, unit: str, accession: str, *, acquisition: str = "DIA", ion_mode: str = "Negative",
    mobility: str = "Unknown", instrument: str = "", separation: str = "LC-MS", rows: dict | None = None,
    folders: tuple = (), study_text: str = "",
) -> None:
    """One more unit for a test: `rows` are the attributes every sample row carries, and each folder is a vendor
    container (path, the member that tells its format), one sample each, as MetaboBank lists them."""
    connection = sqlite3.connect(path)
    with connection:
        study = f"study-{accession}"
        connection.execute(
            "INSERT OR IGNORE INTO study(study_id, repository, accession, source_hash, title, description) "
            "VALUES (?, 'metabobank', ?, 'h', 'A lipidome atlas', ?)", (study, accession, study_text))
        connection.execute(
            "INSERT INTO analysis_unit(unit_id, study_id, source_subrecord_id, separation, acquisition_mode, ion_mode, "
            "ion_mobility, instrument, signature, target_omics) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Lipidomics')",
            (unit, study, unit, separation, acquisition, ion_mode, mobility, instrument, unit))
        for index, (folder, member) in enumerate(folders):
            sample = f"pk-{unit}-{index}"
            connection.execute("INSERT INTO sample(sample_pk, unit_id, sample_id, raw_file) VALUES (?, ?, ?, ?)",
                               (sample, unit, f"{unit}-S{index}", folder + "/"))
            for field, value in (rows or {}).items():
                connection.execute(
                    "INSERT INTO sample_attribute(attribute_id, sample_pk, field_name, normalized_field, raw_value) "
                    "VALUES (?, ?, ?, ?, ?)", (f"{sample}-{field}", sample, field, field.casefold(), value))
            for name in (member, "analysis.sqlite"):
                connection.execute(
                    "INSERT INTO raw_file(file_id, unit_id, path, role, size_bytes, download_url, sample_id) "
                    "VALUES (?, ?, ?, 'raw', ?, ?, '')",
                    (f"f-{unit}-{index}-{name}", unit, f"{folder}/{name}", GB, f"https://x/{unit}/{folder}/{name}"))
    connection.close()


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
# A Console of MsdialWorkbench #826, as the plan reads one: the campaign policy pins automatic RT correction on.
CONSOLE_826 = b"MZ console" + "automatic rt correction local support rt window".encode("utf-16-le")
APPROVABLE_PINS = {
    "console": {"path": "C:/tools/MSDIALCUI.exe", "exists": True, "binary_sha256": "1" * 64,
                "automatic_rt_correction": policy.AUTOMATIC_RT_LOCAL_SUPPORT},
    "extractor": {"path": "C:/tools/RawMetadataConsoleApp.exe", "exists": True, "binary_sha256": "2" * 64,
                  "inventory_sha256": "3" * 64, "provenance_status": "verified", "pinned": True},
    "libraries": [{"name": "P.msp", "sha256": "4" * 64, "bytes": 8}],
}
CODE_PINS = {
    # 0.5.52: the plan refuses an Interactive before the CID/HCD-only rule and the split's part polarity (2026-10-29).
    "interactive": {"version": "0.5.52", "commit": "a" * 40, "dirty": False, "lease_uses_store": False},
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

    def manifest(self, pool: str | None, replan: dict | None = None, **values) -> dict:
        catalog = ports.read_only_catalog(self.database)
        try:
            return plan.build_manifest(
                catalog, pool=pool, campaign_id=f"test-{pool or 'pilot'}", analysis_purpose="annotation",
                workspace_root=self.workspace_root, raw_retention_policy="delete_after_validated_output",
                pins={"catalog": {"version": "0.6.1"}, "libraries": []}, profile=None, campaign_policy=policy.CampaignPolicy(),
                class_decision=lambda unit_id: ports.decide_class(catalog, unit_id, "annotation"),
                catalog_database=str(self.database), replan=replan, **values,
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

    def test_the_manifest_carries_the_cautions_on_its_workspace_root(self) -> None:
        """Review 3: only a hand-built manifest tested the capitalised caution, so a build_manifest that dropped it
        would approve a root on C:, in the profile or in a temporary folder with no word of it, unseen."""
        root = self.workspace_root.resolve()
        environment = {"SystemDrive": root.drive, "USERPROFILE": str(self.root.resolve()), "TEMP": str(root), "TMP": ""}
        with mock.patch.dict(os.environ, environment):
            manifest = self.manifest("declared")
        self.assertEqual(manifest["workspace_root_cautions"],
                         [f"on the system drive {root.drive}", "inside the user profile", "inside a temporary folder"])
        self.assertIn(f"  THE WORKSPACE ROOT IS ON THE SYSTEM DRIVE {root.drive.upper()}, INSIDE THE USER PROFILE AND "
                      "INSIDE A TEMPORARY FOLDER", plan.summary_text(manifest, "sha256:" + "0" * 64))
        with mock.patch.dict(os.environ, {"SystemDrive": "Q:", "USERPROFILE": "", "TEMP": "", "TMP": ""}):
            self.assertEqual(self.manifest("declared")["workspace_root_cautions"], [])

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
        self.assertEqual(manifest["totals"]["selected_units"], 9, "untargeted, GC-MS, an unknown ion mode and Unknown acquisition are not selected")
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

    def test_what_interactive_keeps_beside_the_repositories_changes_no_unit(self) -> None:
        """The library-copy store, the Console TEMP and the viewing restores sit beside the repositories
        (Interactive 0.5.37): the plan reads none of them as a repository, an accession or a unit's workspace."""
        before = self.manifest("declared")
        for name in ("_library_copies/o/5f330f52fee4c4ee0dbbf99dcdff47f3/claims", "_tmp/job1/provenance",
                     "_viewing/r-abc123/backup", "_library_copies/metabolights/MTBLS1/uA/provenance"):
            (self.workspace_root / name).mkdir(parents=True)
        after = self.manifest("declared")
        self.assertEqual([unit["unit_id"] for unit in before["units"]], [unit["unit_id"] for unit in after["units"]])
        self.assertEqual(before["exclusions"], after["exclusions"])
        self.assertEqual(before["legacy_accession_workspaces"], after["legacy_accession_workspaces"])

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
        # A lease that fetches through the store (plan item 15), and nothing that claims a shared object for a unit
        # that has not started: an earlier sibling's cleanup may delete it, so the distinct bytes are only the least.
        manifest["pins"]["interactive"]["lease_uses_store"] = True
        text = plan.summary_text(manifest, "sha256:" + "0" * 64)
        self.assertNotIn("fetched once", text)
        self.assertIn("transfer and disk: at least 6.99 TB of known size where no shared object is fetched twice, up to "
                      "12.82 TB", text)
        self.assertIn("the next unit of its group may fetch it again", text)
        # And once the runner claims a group's objects for every sibling before the first of them downloads
        # (2026-10-24), the distinct bytes are the transfer.
        manifest["pins"]["interactive"]["pre_claim_downloads"] = True
        text = plan.summary_text(manifest, "sha256:" + "0" * 64)
        self.assertIn("transfer and disk: at least 6.99 TB of known size, each shared object fetched once", text)
        self.assertIn("the runner claims the group's objects in the store for every unit of it still to download", text)
        self.assertIn("without the store the units would fetch 12.82 TB", text)
        manifest["pins"]["interactive"]["lease_uses_store"] = False
        self.assertNotIn("fetched once", plan.summary_text(manifest, "sha256:" + "0" * 64))

    def rt_manifest(self, overrides: dict | None = None, profile: dict | None = None) -> dict:
        """A declared-pool manifest planned at a fixed moment, with the campaign policy a --policy file would give."""
        catalog = ports.read_only_catalog(self.database)
        try:
            return plan.build_manifest(
                catalog, pool="declared", campaign_id="test-rt", analysis_purpose="annotation",
                workspace_root=self.workspace_root, raw_retention_policy="keep",
                pins={"catalog": {"version": "0.6.1"}, "libraries": []}, profile=profile,
                campaign_policy=policy.CampaignPolicy.from_dict(overrides), policy_overrides=sorted(overrides or {}),
                class_decision=lambda unit_id: ports.decide_class(catalog, unit_id, "annotation"),
                catalog_database=str(self.database), now=datetime(2026, 10, 8, tzinfo=timezone.utc),
            )
        finally:
            catalog.close()

    def test_the_summary_states_the_automatic_rt_correction_the_default_policy_pins(self) -> None:
        manifest = self.rt_manifest()
        record = manifest["automatic_rt_correction"]
        self.assertEqual(manifest["policy_overrides"], [])
        self.assertEqual((record["pinned"], record["correction"], record["maximum_anchors"], record["local_support_rt_window_min"],
                          record["local_support_rt_window_source"], record["fallback_uncorrected"], record["source"]),
                         (True, True, 12, 1.5, "console_default", True, "default_policy"))
        self.assertEqual(record["differs_from_decision"], [])
        text = plan.summary_text(manifest, "sha256:" + "0" * 64)
        self.assertIn("automatic RT correction: ON, pinned by the campaign policy over the profile; "
                      "from the default campaign policy", text)
        self.assertIn("maximum anchors 12; local window 1.5 min (the Console's default; not sent)", text)
        self.assertIn("fallback ON: after an anchor-selection failure the unit's next attempts run uncorrected", text)
        self.assertIn("Blanks: interpolated by analytical order only where an injection order was recorded", text)
        self.assertNotIn("!!", text)
        self.assertNotIn("DIFFERS", text)

    def test_a_policy_override_of_the_correction_is_said_first_and_changes_the_digest(self) -> None:
        default = plan.digest_of(plan.canonical_bytes(self.rt_manifest()))
        cases = {
            "correction off": ({"automatic_rt_correction": False}, "automatic_rt_correction false, decided true",
                               "NOT PINNED by the campaign policy; from a --policy override of automatic_rt_correction"),
            "Interactive's 6 anchors": ({"automatic_rt_correction_maximum_anchors": 6},
                                        "automatic_rt_correction_maximum_anchors 6, decided 12", "maximum anchors 6;"),
            "no fallback": ({"automatic_rt_correction_fallback": False}, "automatic_rt_correction_fallback false, decided true",
                            "fallback OFF: a unit whose anchors cannot be selected is retried"),
        }
        for name, (overrides, difference, words) in cases.items():
            with self.subTest(name):
                manifest = self.rt_manifest({**overrides, "prefetch": 1})
                record = manifest["automatic_rt_correction"]
                self.assertEqual(record["source"], "policy_override")
                self.assertEqual(record["overridden_fields"], sorted(overrides), "the override's correction fields, named")
                self.assertEqual(manifest["policy_overrides"], sorted({**overrides, "prefetch": 1}))
                self.assertEqual(record["differs_from_decision"], [difference])
                lines = plan.summary_text(manifest, "sha256:" + "0" * 64).splitlines()
                self.assertTrue(lines[1].startswith("  !! AUTOMATIC RT CORRECTION DIFFERS FROM THE DECISION OF 2026-10-07"),
                                "said straight after the campaign's own line, before anything else")
                self.assertIn(difference, lines[1])
                self.assertIn(words, "\n".join(lines))
                self.assertNotEqual(plan.digest_of(plan.canonical_bytes(manifest)), default)
                self.assertEqual(plan.approval_problems(manifest, ["1", "3", "4"]),
                                 plan.approval_problems(self.rt_manifest(), ["1", "3", "4"]),
                                 "a person may decide otherwise: the override is said, not refused")
        # An override that restates the decision is named, and changes the digest, but differs from nothing.
        same = self.rt_manifest({"automatic_rt_correction_maximum_anchors": 12})
        self.assertEqual(same["automatic_rt_correction"]["differs_from_decision"], [])
        self.assertIn("from a --policy override of automatic_rt_correction_maximum_anchors",
                      plan.summary_text(same, "sha256:" + "0" * 64))
        self.assertNotEqual(plan.digest_of(plan.canonical_bytes(same)), default)
        # An override of other fields only leaves the correction the default policy's.
        other = self.rt_manifest({"prefetch": 1})["automatic_rt_correction"]
        self.assertEqual((other["source"], other["overridden_fields"]), ("default_policy", []))

    def test_the_acquisition_unknown_pool_is_its_own_manifest(self) -> None:
        manifest = self.manifest("acquisition_unknown")
        self.assertEqual([unit["unit_id"] for unit in manifest["units"]], ["uK"], "mzXML is converted and runs")
        self.assertEqual(manifest["units"][0]["selection_basis"], "acquisition_unknown")

    # ---- ion mobility, from the unit's own evidence (option A, 2026-10-03) ------------------------------------

    def add_ion_mobility_units(self) -> None:
        """The user's cases: MTBKS217, a Xevo G2 QTOF flagged Enabled only by the abstract its study shares;
        MTBKS220, BAF beside TDF with rows naming a timsTOF; and a unit whose every container is TDF."""
        add_unit(self.database, "uS", "MTBKS217", mobility="Enabled", instrument="Waters Acquity UPLC system",
                 rows={"Parameter Value[Instrument]": "Xevo G2 QTOF MS (Waters, Milford, MA, USA)"},
                 folders=(("raw/s1.raw", "_FUNC001.DAT"),), study_text="The MS-DIAL 4 lipidome atlas, with ion mobility")
        add_unit(self.database, "uX", "MTBKS220", mobility="Enabled", instrument="Bruker Elute UHPLC system",
                 rows={"Parameter Value[Instrument]": "hybrid trapped ion mobility-quadrupole time-of-flight (timsTOF Pro)"},
                 folders=(("raw/off_1.d", "analysis.baf"), ("raw/on_1.d", "analysis.tdf")))
        add_unit(self.database, "uT", "MTBKS900", instrument="Bruker Elute UHPLC system",
                 rows={"Parameter Value[Instrument]": "timsTOF Pro 2"}, folders=(("raw/t1.d", "analysis.tdf"),))

    def test_ion_mobility_is_read_from_the_units_own_evidence(self) -> None:
        """Without the Catalog's helper: excluded only where the unit's instrument or a row's instrument field
        names an ion-mobility instrument and no input is a container that cannot hold ion mobility."""
        self.add_ion_mobility_units()
        manifest = self.manifest("declared", ion_mobility_evidence=None)
        self.assertEqual(manifest["selection"]["ion_mobility_evidence"], "fallback")
        units = {unit["unit_id"]: unit for unit in manifest["units"]}
        exclusions = {item["unit_id"]: item for item in manifest["exclusions"]}
        self.assertIn("uS", units, "a mention in the study's text is not the unit's evidence")
        self.assertEqual(units["uS"]["ion_mobility_reading"]["state"], "unknown")
        self.assertIn("uX", units, "an ion-mobility instrument beside BAF reaches Interactive's header check")
        self.assertEqual(units["uX"]["ion_mobility_reading"]["state"], "mixed")
        self.assertIn("bruker_baf 1, bruker_tdf 1", units["uX"]["ion_mobility_reading"]["detail"])
        self.assertEqual(exclusions["uT"]["reason"], "ion_mobility", "rows naming a timsTOF, and only TDF")
        self.assertIn("timsTOF Pro 2", exclusions["uT"]["detail"])
        self.assertIn("none of its inputs is a Bruker BAF or TSF container", exclusions["uT"]["detail"])
        for unit in ("uE", "uF"):  # instruments that name a 6560 and a timsTOF
            self.assertEqual(exclusions[unit]["reason"], "ion_mobility")
        self.assertNotIn("ion_mobility_reading", units["uA"], "nothing named ion mobility")
        text = plan.summary_text(manifest, "sha256:" + "0" * 64)
        self.assertIn("ion mobility read from the unit's instrument, its rows' instrument fields", text)

    def test_the_catalogs_ion_mobility_evidence_decides_where_it_is_given(self) -> None:
        """Only "enabled" from unit-level sources excludes; "mixed", "unknown" and the study's text alone pass,
        and an answer the plan cannot read is the fallback rule's to decide, said so."""
        self.add_ion_mobility_units()
        answers = {
            "uS": {"state": "enabled", "sources": [{"level": "study", "field": "description", "value": "ion mobility"}]},
            "uX": {"state": "mixed", "sources": [{"level": "row", "field": "Parameter Value[Instrument]", "value": "timsTOF"}]},
            "uT": {"state": "enabled", "sources": [{"level": "row", "field": "Parameter Value[Instrument]", "value": "timsTOF Pro 2"}]},
            "uF": {"state": "unknown", "sources": []},
            "uE": "enabled",
        }
        asked = []

        def evidence(unit):
            asked.append(unit["unit_id"])
            return answers.get(unit["unit_id"], {"state": "disabled", "sources": []})

        manifest = self.manifest("declared", ion_mobility_evidence=evidence)
        self.assertEqual(manifest["selection"]["ion_mobility_evidence"], "catalog")
        self.assertIn("uS", asked, "the helper is given the Catalog's own unit")
        units = {unit["unit_id"]: unit for unit in manifest["units"]}
        exclusions = {item["unit_id"]: item for item in manifest["exclusions"]}
        self.assertEqual(sorted(item for item in ("uS", "uX", "uF") if item in units), ["uF", "uS", "uX"])
        self.assertIn("the study's text only", units["uS"]["ion_mobility_reading"]["detail"])
        self.assertEqual(units["uX"]["ion_mobility_reading"], {
            "evidence": "catalog", "state": "mixed",
            "detail": "the Catalog's ion_mobility_evidence: mixed (row Parameter Value[Instrument]: timsTOF): "
                      "Interactive's header check decides each file"})
        self.assertEqual(exclusions["uT"]["reason"], "ion_mobility")
        self.assertIn("enabled from the unit's own evidence (row Parameter Value[Instrument]: timsTOF Pro 2)", exclusions["uT"]["detail"])
        # An answer of another shape: the fallback rule decides (uE's instrument names a 6560), and says why.
        self.assertEqual(exclusions["uE"]["reason"], "ion_mobility")
        self.assertIn("could not be read (an answer of another shape)", exclusions["uE"]["detail"])

    def test_the_catalogs_answer_names_its_sources(self) -> None:
        """The Catalog's helper names its sources (row_instrument, assay_parameter, container_format, study_text)
        and says why: the unit's own are every source but the study's text, a container's format among them."""
        self.add_ion_mobility_units()
        add_unit(self.database, "uC9", "MTBKS901", instrument="Bruker Elute UHPLC system", folders=(("raw/c.d", "analysis.tdf"),))
        answers = {
            "uS": {"state": "unknown", "source": "study_text", "sources": ["study_text"],
                   "reason": "only the study's text mentions ion mobility, which is not evidence about this unit"},
            "uX": {"state": "mixed", "source": "row_instrument", "sources": ["row_instrument", "container_format"],
                   "reason": "ion-mobility evidence (row_instrument, container_format) beside data without it"},
            "uT": {"state": "enabled", "source": "row_instrument", "sources": ["row_instrument", "container_format"],
                   "reason": "the unit's own evidence says ion mobility (row_instrument, container_format)"},
            "uC9": {"state": "enabled", "source": "container_format", "sources": ["container_format"], "reason": ""},
            "uF": {"state": "none", "source": "container_format", "sources": ["container_format"],
                   "reason": "the unit's own evidence says no ion mobility (container_format)"},
        }
        manifest = self.manifest("declared", ion_mobility_evidence=lambda unit: answers.get(
            unit["unit_id"], {"state": "unknown", "source": None, "sources": [], "reason": "nothing says"}))
        units = {unit["unit_id"]: unit for unit in manifest["units"]}
        exclusions = {item["unit_id"]: item for item in manifest["exclusions"]}
        self.assertEqual({key: exclusions[key]["reason"] for key in ("uT", "uC9")}, {"uT": "ion_mobility", "uC9": "ion_mobility"})
        self.assertIn("(row_instrument; container_format); the unit's own evidence says ion mobility", exclusions["uT"]["detail"])
        self.assertEqual({key: units[key]["ion_mobility_reading"]["state"] for key in ("uS", "uX")}, {"uS": "unknown", "uX": "mixed"})
        self.assertIn("only the study's text mentions ion mobility", units["uS"]["ion_mobility_reading"]["detail"])
        self.assertIn("uF", units, "the helper's none is taken over the instrument's name")
        self.assertNotIn("ion_mobility_reading", units["uF"])

    def test_the_catalogs_helper_is_found_by_import(self) -> None:
        import types

        found = types.ModuleType("msdial_repository_catalog.ion_mobility")
        found.ion_mobility_evidence = lambda unit: {"state": "disabled", "sources": []}
        with mock.patch.dict(sys.modules, {"msdial_repository_catalog.ion_mobility": found}):
            self.assertIs(plan.catalog_ion_mobility_evidence(), found.ion_mobility_evidence)
            manifest = self.manifest("declared")
        self.assertEqual(manifest["selection"]["ion_mobility_evidence"], "catalog")
        self.assertIn("uF", [unit["unit_id"] for unit in manifest["units"]], "the helper's disabled is taken")
        with mock.patch.object(plan, "ION_MOBILITY_EVIDENCE_MODULES", ("no_such_module_here",)):
            self.assertIsNone(plan.catalog_ion_mobility_evidence())

    def test_a_unit_whose_catalog_record_cannot_be_read_is_not_excluded_for_ion_mobility_on_its_row(self) -> None:
        """The plan's own row holds neither a unit's sample rows nor its inputs. Read alone, as the plan used to read
        it where Catalog.get_unit raised, MTBKS219's timsTOF beside its BAF folders was ion mobility alone, and the
        approvable manifest said none of its inputs was a BAF container, unread. Not read, the unit is not excluded
        for it, with or without the Catalog's helper, and the manifest and its summary say so. Its Class decision
        reads the same record: where that fails as well, the unit is excluded as class_undecided, with the error."""
        self.add_ion_mobility_units()
        add_unit(self.database, "uY", "MTBKS219", mobility="Enabled", instrument="Bruker timsTOF Pro",
                 rows={"Parameter Value[Instrument]": "timsTOF Pro"},
                 folders=(("raw/off_1.d", "analysis.baf"), ("raw/on_1.d", "analysis.tdf")))
        from msdial_repository_catalog.storage import Catalog

        real = Catalog.get_unit
        for helper in (False, True):
            with self.subTest(helper=helper):
                reads = {"uY": 0, "uT": 0}

                def get_unit(catalog, unit_id, *args, reads=reads, **kwargs):
                    if unit_id in reads:
                        reads[unit_id] += 1
                        # uY's record is read for ion mobility only once; uT's never.
                        if unit_id == "uT" or reads[unit_id] == 1:
                            raise sqlite3.OperationalError("database is locked")
                    return real(catalog, unit_id, *args, **kwargs)

                asked = []

                def evidence(unit):
                    asked.append(unit["unit_id"])
                    return {"state": "enabled" if unit["unit_id"] == "uY" else "mixed", "sources": ["row_instrument"]}

                with mock.patch.object(Catalog, "get_unit", get_unit):
                    manifest = self.manifest("declared", ion_mobility_evidence=evidence if helper else None)
                units = {unit["unit_id"]: unit for unit in manifest["units"]}
                exclusions = {item["unit_id"]: item for item in manifest["exclusions"]}
                self.assertIn("uY", units, f"excluded: {exclusions.get('uY')}")
                reading = units["uY"]["ion_mobility_reading"]
                self.assertEqual((reading["evidence"], reading["state"]), ("not_read", "unknown"))
                self.assertIn("(OperationalError: database is locked)", reading["detail"])
                self.assertNotIn("none of its inputs", reading["detail"])
                self.assertNotIn("uY", asked, "the helper is never given the plan's row")
                self.assertEqual(units["uX"]["ion_mobility_reading"]["evidence"], "catalog" if helper else "fallback")
                self.assertEqual(exclusions["uT"]["reason"], "class_undecided")
                self.assertIn("database is locked", exclusions["uT"]["detail"])
                text = plan.summary_text(manifest, "sha256:" + "0" * 64)
                self.assertIn("ion mobility not read for 1 planned units whose Catalog record could not be read, "
                              "so not excluded for it (uY)", text)
        self.assertEqual(plan.exclusion_reasons({"file_count": 1, "analysis_paths": [], "repository": "metabobank",
                                                 "accession": "MTBKS219", "unit_id": "uY",
                                                 "instrument": "Bruker timsTOF Pro"}, self.workspace_root), [],
                         "without a reading of the unit's record, no exclusion for ion mobility")

    # ---- a pilot: named units from both pools (2026-10-03) ---------------------------------------------------

    def test_a_pilot_plans_exactly_the_named_units_each_under_its_pools_rules(self) -> None:
        manifest = self.manifest(None, unit_ids=["uK", "uA", "uF", "uJ", "uA"], ion_mobility_evidence=None)
        self.assertEqual(manifest["pool"], plan.PILOT)
        self.assertEqual(manifest["selection"]["units"], ["uK", "uA", "uF", "uJ"])
        self.assertEqual(set(manifest["selection"]["pools"]), set(plan.POOLS))
        self.assertEqual(manifest["selection"]["exclusion_reasons"][0], "not_in_pool")
        basis = {unit["unit_id"]: unit["selection_basis"] for unit in manifest["units"]}
        self.assertEqual(basis, {"uA": "declared", "uK": "acquisition_unknown"})
        exclusions = {item["unit_id"]: item for item in manifest["exclusions"]}
        self.assertEqual((exclusions["uF"]["reason"], exclusions["uF"]["selection_basis"]), ("ion_mobility", "declared"))
        self.assertEqual((exclusions["uJ"]["reason"], exclusions["uJ"]["selection_basis"]), ("not_in_pool", None))
        self.assertIn("separation GC-MS", exclusions["uJ"]["detail"])
        totals = manifest["totals"]
        self.assertEqual((totals["selected_units"], totals["planned_units"], totals["excluded_units"]), (4, 2, 2))
        self.assertEqual(totals["planned_by_pool"], {"acquisition_unknown": 1, "declared": 1})
        text = plan.summary_text(manifest, "sha256:" + "0" * 64)
        self.assertIn("(pilot pool): the analysis units named with --units", text)
        self.assertIn("planned by pool: acquisition_unknown 1, declared 1", text)
        self.assertIn("excluded not_in_pool: 1", text)

    def test_a_pilot_refuses_a_unit_the_catalog_does_not_know_and_a_pool_beside_it(self) -> None:
        with self.assertRaisesRegex(plan.PlanError, "no analysis unit uNOPE"):
            self.manifest(None, unit_ids=["uA", "uNOPE"])
        with self.assertRaises(plan.PlanError):
            self.manifest("declared", unit_ids=["uA"])
        with self.assertRaises(plan.PlanError):
            self.manifest(None)

    def test_the_units_of_a_pilot_are_read_from_a_list_or_a_file(self) -> None:
        listed = self.root / "pilot_units.json"
        listed.write_text(json.dumps(["uA", "uK"]), encoding="utf-8")
        lines = self.root / "pilot_units.txt"
        lines.write_text("# the pilot\nuA\nuK, uA\n", encoding="utf-8")
        for value in (str(listed), str(lines), "uA, uK", "uA,uK,"):
            with self.subTest(value=value):
                self.assertEqual(plan.read_unit_list(value), ["uA", "uK"])
        wrapped = self.root / "pilot.json"
        wrapped.write_text(json.dumps({"units": ["uK"]}), encoding="utf-8")
        self.assertEqual(plan.read_unit_list(str(wrapped)), ["uK"])
        wrapped.write_text(json.dumps({"units": [1, 2]}), encoding="utf-8")
        with self.assertRaises(plan.PlanError):
            plan.read_unit_list(str(wrapped))
        with self.assertRaises(plan.PlanError):
            plan.read_unit_list(" , ")

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
        (tools / "MSDIALCUI.exe").write_bytes(CONSOLE_826)
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
        # Review of 2026-10-25: the words a person approves name the destination and the retention (boundary 1), and
        # the plan says which of the option and the variable named the root.
        self.assertIn(f"  workspace root {self.workspace_root}: every unit's raw data, MS-DIAL outputs and records, and "
                      "this campaign's ledger, are written under it", out)
        # Review 2: boundary 5 covers the raw data of a unit that ends without validated outputs too, and the words say so.
        self.assertIn("  raw retention delete_after_validated_output: each unit's raw data are deleted when the unit ends "
                      "(boundary 5): once its outputs are validated, and also when it ends without validated outputs "
                      "(failed after its retries, excluded or skipped), so they are not kept for diagnosis", out)
        self.assertIn(f"Workspace root {self.workspace_root}, as --workspace-root names it.", err)

        code, _out, err = self.cli("approve", "--campaign", "c1", "--digest", "sha256:" + "0" * 64,
                                   "--approval-id", "A1", "--by", "Test Person", "--statement", "Yes.", "--covers", "1,3,4,5,split")
        self.assertEqual(code, runner_cli.EXIT_REFUSED)
        self.assertFalse((directory / "ledger.sqlite").exists())
        code, _out, err = self.cli("approve", "--campaign", "c1", "--digest", digest, "--approval-id", "A1",
                                   "--by", "Test Person", "--statement", "Yes.", "--covers", "1,3,4,5,split,6")
        self.assertEqual(code, runner_cli.EXIT_REFUSED, "boundary 6 is never covered")
        # The same manifest as a plan made before the RT statement wrote it: the policy pins the correction, and the
        # digest a person would approve covers no words about it, so approve refuses it and says to plan again.
        planned = manifest_path.read_bytes()
        earlier = json.loads(planned)
        earlier.pop("automatic_rt_correction")
        earlier.pop("policy_overrides")
        earlier_digest = plan.write_manifest(manifest_path, earlier)
        code, _out, err = self.cli("approve", "--campaign", "c1", "--digest", earlier_digest, "--approval-id", "A1",
                                   "--by", "Test Person", "--statement", "Yes.", "--covers", "1,3,4,5,split")
        self.assertEqual(code, runner_cli.EXIT_REFUSED)
        self.assertIn("carries no automatic RT correction statement", err)
        self.assertIn("plan again", err)
        self.assertFalse((directory / "ledger.sqlite").exists())
        self.assertFalse((directory / "campaign-authorization.json").exists())
        manifest_path.write_bytes(planned)
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

    def test_a_pilot_is_planned_and_approved_from_the_command_line(self) -> None:
        """plan --units needs no --pool, writes one manifest of both pools' units, and its approval makes a
        ledger whose campaign is the pilot."""
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "MSDIALCUI.exe").write_bytes(CONSOLE_826)
        (tools / "P.msp").write_text("NAME: x\n", encoding="utf-8")
        resources = self.root / "campaign-resources.local.json"
        resources.write_text(json.dumps({"schema": ports.RESOURCES_SCHEMA, "libraries": {"P.msp": str(tools / "P.msp")}}),
                             encoding="utf-8")
        profile = self.root / "profile.json"
        profile.write_text(json.dumps({"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "existing"},
                                       "by_ion_mode": {"Positive": {"libraries": {"msp_paths": ["library:P.msp"]}}}}),
                           encoding="utf-8")
        units = self.root / "pilot_units.json"
        units.write_text(json.dumps(["uA", "uK", "uF"]), encoding="utf-8")
        with self.assertRaises(SystemExit, msg="a pool or a pilot's units, one of them"):
            self.cli("plan", "--campaign", "p0", "--purpose", "annotation", "--retention", "keep")
        with self.assertRaises(SystemExit):
            self.cli("plan", "--campaign", "p0", "--pool", "declared", "--units", "uA", "--purpose", "a", "--retention", "keep")
        self.assertEqual(self.cli("plan", "--campaign", "p0", "--units", "uA,uNOPE", "--purpose", "a", "--retention", "keep",
                                  "--catalog", str(self.database))[0], runner_cli.EXIT_REFUSED, "a name the Catalog does not know")
        # A dry run may name the manifest's file; its summary sits beside it, and no campaign directory is made.
        dry = self.root / "dry" / "pilot-manifest.json"
        code, out, err = self.cli("plan", "--campaign", "p0", "--units", "uA,uK", "--purpose", "a", "--retention", "keep",
                                  "--catalog", str(self.database), "--out", str(dry))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(dry.read_text(encoding="utf-8"))["pool"], "pilot")
        summary = json.loads((self.root / "dry" / "pilot-manifest.summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["approval_covers"], ["1", "3", "4", "split"], "raw data kept: boundary 5 is not covered")
        self.assertNotIn("boundary 5", " ".join(summary["approval_problems"]))
        self.assertFalse((self.workspace_root / "_campaigns" / "p0").exists())
        extractor = {**APPROVABLE_PINS["extractor"], "path": str(tools / "RawMetadataConsoleApp.exe")}
        with mock.patch.object(ports.PinReader, "extractor", lambda _self: dict(extractor)), \
                mock.patch.object(ports.PinReader, "code", lambda _self: json.loads(json.dumps(CODE_PINS))):
            code, out, err = self.cli(
                "plan", "--campaign", "pilot-1", "--units", str(units), "--purpose", "annotation", "--retention", "keep",
                "--catalog", str(self.database), "--console", str(tools / "MSDIALCUI.exe"), "--resources", str(resources),
                "--profile", str(profile),
            )
        self.assertEqual(code, 0, err)
        self.assertNotIn("Not approvable", err)
        self.assertIn("planned by pool: acquisition_unknown 1, declared 1", out)
        directory = self.workspace_root / "_campaigns" / "pilot-1"
        manifest = json.loads((directory / "campaign-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["pool"], manifest["selection"]["units"]), ("pilot", ["uA", "uK", "uF"]))
        digest = "sha256:" + hashlib.sha256((directory / "campaign-manifest.json").read_bytes()).hexdigest()
        code, _out, err = self.cli("approve", "--campaign", "pilot-1", "--digest", digest, "--approval-id", "P1",
                                   "--by", "Test Person", "--statement", "Start the pilot.", "--covers", "1,3,4,split")
        self.assertEqual(code, 0, err)
        with ledger.Ledger(directory / "ledger.sqlite") as book:
            self.assertEqual(book.campaign()["pool"], "pilot")
            self.assertEqual(sorted(unit["unit_key"] for unit in book.units()), ["uA", "uK"])

    def plan_approvable(self, campaign: str) -> tuple[Path, str]:
        """Plan an approvable declared-pool campaign from the command line: (its directory, its manifest digest)."""
        tools = self.root / f"tools-{campaign}"
        tools.mkdir()
        (tools / "MSDIALCUI.exe").write_bytes(CONSOLE_826)
        (tools / "P.msp").write_text("NAME: x\n", encoding="utf-8")
        (tools / "N.msp").write_text("NAME: x\n", encoding="utf-8")
        resources = tools / "campaign-resources.local.json"
        resources.write_text(json.dumps({"schema": ports.RESOURCES_SCHEMA, "libraries": {
            "P.msp": str(tools / "P.msp"), "N.msp": str(tools / "N.msp")}}), encoding="utf-8")
        profile = tools / "profile.json"
        profile.write_text(json.dumps({"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "existing"},
                                       "by_ion_mode": {"Positive": {"libraries": {"msp_paths": ["library:P.msp"]}},
                                                       "Negative": {"libraries": {"msp_paths": ["library:N.msp"]}}}}),
                           encoding="utf-8")
        extractor = {**APPROVABLE_PINS["extractor"], "path": str(tools / "RawMetadataConsoleApp.exe")}
        with mock.patch.object(ports.PinReader, "extractor", lambda _self: dict(extractor)), \
                mock.patch.object(ports.PinReader, "code", lambda _self: json.loads(json.dumps(CODE_PINS))):
            code, _out, err = self.cli(
                "plan", "--campaign", campaign, "--pool", "declared", "--purpose", "annotation",
                "--retention", "delete_after_validated_output", "--catalog", str(self.database),
                "--console", str(tools / "MSDIALCUI.exe"), "--resources", str(resources), "--profile", str(profile),
            )
        self.assertEqual(code, 0, err)
        self.assertNotIn("Not approvable", err)
        directory = self.workspace_root / "_campaigns" / campaign
        return directory, "sha256:" + hashlib.sha256((directory / "campaign-manifest.json").read_bytes()).hexdigest()

    def approve(self, campaign: str, digest: str, approval_id: str = "A1") -> tuple[int, str, str]:
        return self.cli("approve", "--campaign", campaign, "--digest", digest, "--approval-id", approval_id,
                        "--by", "Test Person", "--statement", "Yes, run it.", "--covers", "1,3,4,5,split")

    def test_approve_takes_the_empty_ledger_an_earlier_verify_env_left(self) -> None:
        """The defect of 2026-10-15: verify-env before approve made an empty ledger (schema and first rows only), and
        approve then refused the campaign as already approved. approve now takes such a ledger as absent and records
        the approval into it."""
        directory, digest = self.plan_approvable("c-empty")
        path = directory / "ledger.sqlite"
        ledger.Ledger(path).close()  # what a runner before 2026-10-15 left when verify-env opened the campaign
        self.assertEqual(ledger.ledger_state(path)["state"], ledger.LEDGER_EMPTY)
        code, _out, err = self.cli("verify-env", "--campaign", "c-empty")
        self.assertEqual(code, runner_cli.EXIT_REFUSED, err)
        self.assertIn("has no approval yet", err)
        code, out, err = self.approve("c-empty", digest)
        self.assertEqual(code, 0, err)
        self.assertIn("The ledger was already there, empty", out)
        with ledger.Ledger(path) as book:
            self.assertEqual(book.campaign()["manifest_digest"], digest)
            self.assertEqual(book.approval()["approval_id"], "A1")
            self.assertEqual(sorted(unit["unit_key"] for unit in book.units()), ["uA", "uB", "uC"])
            events = book.events("empty_ledger_approved")
            self.assertEqual(len(events), 1)
            self.assertEqual(json.loads(events[0]["detail_json"])["approval_id"], "A1")
        self.assertTrue((directory / "campaign-authorization.json").is_file())
        code, out, _err = self.cli("status", "--campaign", "c-empty")
        self.assertEqual((code, json.loads(out)["states"]), (0, {"pending": 3}))

    def test_plan_takes_an_empty_ledger_as_no_approval(self) -> None:
        directory, first = self.plan_approvable("c-replan")
        ledger.Ledger(directory / "ledger.sqlite").close()
        code, _out, err = self.cli("plan", "--campaign", "c-replan", "--pool", "declared", "--purpose", "p",
                                   "--retention", "keep", "--catalog", str(self.database))
        self.assertEqual(code, 0, err)
        replanned = "sha256:" + hashlib.sha256((directory / "campaign-manifest.json").read_bytes()).hexdigest()
        self.assertNotEqual(replanned, first, "the manifest was planned again")
        self.assertEqual(ledger.ledger_state(directory / "ledger.sqlite")["state"], ledger.LEDGER_EMPTY)

    def test_approve_still_refuses_a_ledger_that_holds_a_campaign_or_units(self) -> None:
        directory, digest = self.plan_approvable("c-held")
        path = directory / "ledger.sqlite"
        self.assertEqual(self.approve("c-held", digest)[0], 0)
        authorization = (directory / "campaign-authorization.json").read_bytes()
        code, _out, err = self.approve("c-held", digest, "A2")
        self.assertEqual(code, runner_cli.EXIT_REFUSED)
        self.assertIn("Campaign c-held already has a ledger and an approval", err)
        self.assertIn("holds 1 campaign, 1 approval and 3 units", err)
        self.assertEqual((directory / "campaign-authorization.json").read_bytes(), authorization, "not rewritten")
        with ledger.Ledger(path) as book:
            self.assertEqual(book.approval()["approval_id"], "A1")
        self.assertEqual(self.cli("plan", "--campaign", "c-held", "--pool", "declared", "--purpose", "p", "--retention",
                                  "keep", "--catalog", str(self.database))[0], runner_cli.EXIT_REFUSED)
        # A ledger with units of no campaign is no empty ledger either; neither is one SQLite cannot read.
        directory, digest = self.plan_approvable("c-units")
        with ledger.Ledger(directory / "ledger.sqlite") as book:
            book.connection.execute(
                "INSERT INTO unit(unit_key, catalog_unit_id, repository, accession, order_index, state, state_since, "
                "updated_at) VALUES ('stray', 'stray', 'r', 'a', 0, 'pending', 'now', 'now')")
        code, _out, err = self.approve("c-units", digest)
        self.assertEqual(code, runner_cli.EXIT_REFUSED)
        self.assertIn("holds 0 campaigns, 0 approvals and 1 unit", err)
        self.assertFalse((directory / "campaign-authorization.json").exists(), "nothing was recorded")
        directory, digest = self.plan_approvable("c-broken")
        (directory / "ledger.sqlite").write_bytes(b"not a database " * 100)
        code, _out, err = self.approve("c-broken", digest)
        self.assertEqual(code, runner_cli.EXIT_REFUSED)
        self.assertIn("cannot be read", err)
        self.assertFalse((directory / "campaign-authorization.json").exists())

    def assert_approval_stands(self, directory: Path, digest: str, events: int) -> None:
        """The ledger holds the approval, and campaign-authorization.json is the record its campaign row names."""
        authorization = directory / "campaign-authorization.json"
        self.assertTrue(authorization.is_file(), "the authorization record the ledger names stays")
        with ledger.Ledger(directory / "ledger.sqlite") as book:
            self.assertEqual(book.campaign()["manifest_digest"], digest)
            self.assertEqual(book.campaign()["authorization_sha256"], ports.sha256_file(authorization))
            self.assertEqual(book.campaign()["authorization_path"], str(authorization))
            self.assertEqual(len(book.events("empty_ledger_approved")), events)

    @staticmethod
    def create_failing(phase: str):
        """Ledger.create, with its transaction's commit raising a disk error "before" its COMMIT (rolled back) or
        "after" it (committed)."""
        original = ledger.Ledger.create

        def create(book, *arguments, **options):
            def hook(at: str) -> None:
                if at == phase:
                    raise sqlite3.OperationalError("disk I/O error")

            book.commit_hook = hook
            return original(book, *arguments, **options)

        return create

    def test_approve_records_the_empty_ledgers_event_with_its_approval_or_nothing(self) -> None:
        """Review of 2026-10-15: approve wrote empty_ledger_approved in a transaction of its own after create had
        committed, and took a failure of that write for an approval that recorded nothing. It deleted
        campaign-authorization.json, which the committed ledger names, said "nothing was recorded", and a second
        approve was refused. The event is now in create's transaction, and an error is weighed against what the
        ledger holds."""
        # The failure the review made: Ledger.event refusing. approve no longer writes through it.
        directory, digest = self.plan_approvable("c-event")
        ledger.Ledger(directory / "ledger.sqlite").close()
        with mock.patch.object(ledger.Ledger, "event", side_effect=sqlite3.OperationalError("database is locked")):
            code, out, err = self.approve("c-event", digest)
        self.assertEqual(code, 0, err)
        self.assertIn("The ledger was already there, empty", out)
        self.assert_approval_stands(directory, digest, events=1)

        # A failure inside create's transaction: the approval and its event are rolled back together.
        directory, digest = self.plan_approvable("c-rollback")
        path = directory / "ledger.sqlite"
        ledger.Ledger(path).close()
        with mock.patch.object(ledger.Ledger, "create", self.create_failing("before")):
            code, out, err = self.approve("c-rollback", digest)
        self.assertEqual(code, runner_cli.EXIT_REFUSED)
        self.assertIn("disk I/O error; nothing was recorded", err)
        self.assertEqual(out, "")
        self.assertEqual(ledger.ledger_state(path)["state"], ledger.LEDGER_EMPTY)
        with ledger.Ledger(path) as book:
            self.assertEqual(book.events(), [], "the event went with the approval")
        self.assertFalse((directory / "campaign-authorization.json").exists())
        code, _out, err = self.approve("c-rollback", digest)
        self.assertEqual(code, 0, err)
        self.assert_approval_stands(directory, digest, events=1)

        # An error after create's COMMIT: the approval stands, its record with it, and approve says so.
        directory, digest = self.plan_approvable("c-after")
        ledger.Ledger(directory / "ledger.sqlite").close()
        with mock.patch.object(ledger.Ledger, "create", self.create_failing("after")):
            code, out, err = self.approve("c-after", digest)
        self.assertEqual(code, 0, err)
        self.assertIn(f"Approval A1 of {digest} recorded for 3 units", out)
        self.assertIn("an error came after it was committed: disk I/O error", err)
        self.assertNotIn("nothing was recorded", err)
        self.assert_approval_stands(directory, digest, events=1)
        code, _out, err = self.approve("c-after", digest, "A2")
        self.assertEqual(code, runner_cli.EXIT_REFUSED)
        self.assertIn("already has a ledger and an approval", err)
        self.assert_approval_stands(directory, digest, events=1)

        # Where the ledger cannot be read again to tell, the authorization record is left as it is.
        directory, digest = self.plan_approvable("c-unknown")
        unreadable = sqlite3.OperationalError("database is locked")
        with mock.patch.object(ledger.Ledger, "create", self.create_failing("before")), \
                mock.patch.object(ledger, "recorded_authorization", side_effect=unreadable):
            code, _out, err = self.approve("c-unknown", digest)
        self.assertEqual(code, runner_cli.EXIT_REFUSED)
        self.assertIn("could not be read again to tell whether it was (database is locked)", err)
        self.assertTrue((directory / "campaign-authorization.json").is_file(), "left in place")

    def test_a_correction_split_by_ion_mode_is_said_first_in_the_plan(self) -> None:
        profile = {"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "existing"},
                   "by_ion_mode": {"Positive": {"execute_automatic_rt_correction": True}}}
        manifest = self.rt_manifest({"automatic_rt_correction": False}, profile)
        text = plan.summary_text(manifest, "sha256:" + "0" * 64).splitlines()
        self.assertIn("DIFFERS FROM THE DECISION OF 2026-10-07", text[1])
        self.assertIn("!! AUTOMATIC RT CORRECTION DIFFERS BY ION MODE (not pinned by the campaign policy): "
                      "Positive units ON (set by by_ion_mode.Positive), Negative units OFF", text[2])
        both = json.loads(json.dumps(profile))
        both["answers"]["execute_automatic_rt_correction"] = True
        uniform = self.rt_manifest({"automatic_rt_correction": False}, both)
        self.assertNotIn("BY ION MODE", plan.summary_text(uniform, "sha256:" + "0" * 64))
        self.assertNotEqual(plan.digest_of(plan.canonical_bytes(manifest)), plan.digest_of(plan.canonical_bytes(uniform)))

    def test_the_plan_command_states_a_policy_override_of_the_correction(self) -> None:
        overrides = self.root / "policy.json"
        overrides.write_text(json.dumps({"automatic_rt_correction_maximum_anchors": 6}), encoding="utf-8")
        dry = self.root / "dry" / "rt-manifest.json"
        code, out, err = self.cli("plan", "--campaign", "rt0", "--pool", "declared", "--purpose", "a", "--retention", "keep",
                                  "--catalog", str(self.database), "--out", str(dry), "--policy", str(overrides))
        self.assertEqual(code, 0, err)
        self.assertIn("!! AUTOMATIC RT CORRECTION DIFFERS FROM THE DECISION OF 2026-10-07", out.splitlines()[1])
        self.assertIn("from a --policy override of automatic_rt_correction_maximum_anchors", out)
        self.assertIn("Automatic RT correction differs from the decision of 2026-10-07: "
                      "automatic_rt_correction_maximum_anchors 6, decided 12", err)
        manifest = json.loads(dry.read_text(encoding="utf-8"))
        self.assertEqual(manifest["policy_overrides"], ["automatic_rt_correction_maximum_anchors"])
        summary = json.loads((self.root / "dry" / "rt-manifest.summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["automatic_rt_correction"], manifest["automatic_rt_correction"])
        self.assertEqual(summary["summary_text"].strip(), out.strip())
        self.assertIn(summary["manifest_digest"], summary["summary_text"])
        code, out, err = self.cli("plan", "--campaign", "rt1", "--pool", "declared", "--purpose", "a", "--retention", "keep",
                                  "--catalog", str(self.database), "--out", str(self.root / "dry" / "rt1.json"))
        self.assertEqual(code, 0, err)
        self.assertIn("automatic RT correction: ON, pinned by the campaign policy over the profile; "
                      "from the default campaign policy", out)
        self.assertNotIn("DIFFERS", out)
        self.assertNotIn("differs from the decision", err)
        self.assertNotIn("differs by ion mode", err)
        # With the correction off, a profile that turns it on for one ion mode is said on stderr as well.
        overrides.write_text(json.dumps({"automatic_rt_correction": False}), encoding="utf-8")
        profile = self.root / "split-profile.json"
        profile.write_text(json.dumps({"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "existing"},
                                       "by_ion_mode": {"Positive": {"execute_automatic_rt_correction": True}}}),
                           encoding="utf-8")
        code, out, err = self.cli("plan", "--campaign", "rt2", "--pool", "declared", "--purpose", "a", "--retention", "keep",
                                  "--catalog", str(self.database), "--out", str(self.root / "dry" / "rt2.json"),
                                  "--policy", str(overrides), "--profile", str(profile))
        self.assertEqual(code, 0, err)
        self.assertIn("!! AUTOMATIC RT CORRECTION DIFFERS BY ION MODE", out.splitlines()[2])
        self.assertIn("Automatic RT correction differs by ion mode: Positive units ON (set by by_ion_mode.Positive), "
                      "Negative units OFF (not set; Interactive's default is off)", err)

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

    def test_the_scheduled_task_keeps_a_path_with_a_space_one_word(self) -> None:
        """The task is registered from its XML definition (2026-10-06), whose Command and Arguments are separate
        elements: a path with a space is quoted inside Arguments and needs no escaping for schtasks."""
        line = runner_cli.schedule_task_command("c1", r"D:\an analysis\task.xml")
        self.assertEqual(line, r'schtasks /Create /TN "MSDIAL-campaign-c1" /XML "D:\an analysis\task.xml" /F')
        definition = runner_cli.schedule_task_xml(
            python=r"C:\Program Files\Python314\python.exe", script=r"D:\code\scripts\campaign-runner.py", campaign="c1",
            user="HOST\\someone", workspace_root=r"D:\analysis", interactive_root=r"D:\i", catalog_root=r"D:\c",
            start="2026-10-06T00:00:00")
        self.assertIn(r"<Command>C:\Program Files\Python314\python.exe</Command>", definition)
        self.assertIn(r'<Arguments>"D:\code\scripts\campaign-runner.py" --workspace-root "D:\analysis"', definition)
        self.assertIn("run --campaign c1 --until-idle", definition)

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

    def plan_tiered(self, campaign: str, loads_profile_lbm: bool) -> tuple[int, str, str, Path, Path]:
        """Plan a tiered_lipid_msp campaign whose profile names an LBM2, from the command line, with the pinned
        Interactive's tiered strategy said to load that LBM2 or not (plan.tiered_lbm_from_answers): (exit code, the
        summary, stderr, the campaign directory, the folder of the synthetic libraries)."""
        tools = self.root / f"tools-{campaign}"
        tools.mkdir()
        (tools / "MSDIALCUI.exe").write_bytes(CONSOLE_826)
        for name in ("P.msp", "N.msp", "Approved.lbm2", "Replaced.lbm2"):
            (tools / name).write_text(f"synthetic {name}\n", encoding="utf-8")
        resources = tools / "campaign-resources.local.json"
        resources.write_text(json.dumps({"schema": ports.RESOURCES_SCHEMA, "libraries": {
            name: str(tools / name) for name in ("P.msp", "N.msp", "Approved.lbm2", "Replaced.lbm2")}}), encoding="utf-8")
        profile = tools / "profile.json"
        profile.write_text(json.dumps({
            "schema": plan.PROFILE_SCHEMA,
            "answers": {"library_strategy": "tiered_lipid_msp", "libraries": {"lbm_path": "library:Approved.lbm2"}},
            "by_ion_mode": {"Positive": {"libraries": {"msp_paths": ["library:P.msp"]}},
                            "Negative": {"libraries": {"msp_paths": ["library:N.msp"]}}}}), encoding="utf-8")
        extractor = {**APPROVABLE_PINS["extractor"], "path": str(tools / "RawMetadataConsoleApp.exe")}
        with mock.patch.object(ports.PinReader, "extractor", lambda _self: dict(extractor)), \
                mock.patch.object(ports.PinReader, "code", lambda _self: json.loads(json.dumps(CODE_PINS))), \
                mock.patch.object(plan, "tiered_lbm_from_answers", return_value=loads_profile_lbm):
            code, out, err = self.cli(
                "plan", "--campaign", campaign, "--pool", "declared", "--purpose", "annotation",
                "--retention", "delete_after_validated_output", "--catalog", str(self.database),
                "--console", str(tools / "MSDIALCUI.exe"), "--resources", str(resources), "--profile", str(profile),
            )
        return code, out, err, self.workspace_root / "_campaigns" / campaign, tools

    def test_the_plan_pins_only_the_libraries_the_profile_names(self) -> None:
        """2026-10-24: the resource map may locate more libraries than the profile names (an LBM2 the profile
        replaced); only the profile's are pinned and approved, each with its role and the ion modes that load it, and
        the summary lists the others as left unpinned and states what the pinned Interactive annotates with. The
        pinned Interactive is said to load the profile's LBM2 here; whether the real one does is probed apart
        (review of 2026-10-25)."""
        from msdial_app import annotation_pipeline

        code, out, err, directory, tools = self.plan_tiered("c-libraries", loads_profile_lbm=True)
        self.assertEqual(code, 0, err)
        self.assertNotIn("Not approvable", err)
        manifest = json.loads((directory / "campaign-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([(item["name"], item["role"], item["ion_modes"]) for item in manifest["pins"]["libraries"]], [
            ("Approved.lbm2", "lbm", ["Positive", "Negative"]), ("N.msp", "msp", ["Negative"]), ("P.msp", "msp", ["Positive"])])
        self.assertEqual(manifest["annotation"]["libraries_not_pinned"], ["Replaced.lbm2"])
        self.assertIn("  libraries in the resource map left unpinned, since the profile names none of them: Replaced.lbm2", out)
        profile_id = annotation_pipeline.tiered_lcms_annotation("Approved.lbm2", "P.msp")["profile_id"]
        self.assertEqual(manifest["annotation"]["by_ion_mode"]["Positive"]["profile_id"], profile_id)
        self.assertIn(f"  annotation of Negative units: library strategy tiered_lipid_msp; annotation profile {profile_id} "
                      "(the pinned Interactive's annotation_pipeline); the profile's LBM Approved.lbm2; LBM cutoffs weighted/simple/reverse ",
                      out)
        stated = manifest["annotation"]["by_ion_mode"]["Negative"]
        self.assertTrue(stated["lbm_cutoffs"] and stated["lbm_cutoffs_source"], "read from the pinned Interactive's code")
        self.assertNotIn(str(tools), json.dumps(manifest), "the manifest names libraries, never their location")
        # Review 3: the plan itself puts the template pin into the manifest (the runner's tests take it from fakes),
        # for both ion modes, since the profile's answers name no template.
        from msdial_app import agent_workflow

        template, whose = plan.lcms_parameter_template(agent_workflow)
        digest = hashlib.sha256(template.read_bytes()).hexdigest()
        self.assertEqual(manifest["pins"]["template"], {
            "name": template.name, "exists": True, "sha256": digest, "bytes": template.stat().st_size,
            "named_by": whose, "ion_modes": ["Positive", "Negative"]})
        self.assertIn(f"  LC-MS parameter template {template.name}{plan.TEMPLATE_WHOSE[whose]}, pinned by its sha256 "
                      f"{digest[:12]}...: a change to it pauses the campaign", out)
        digest = "sha256:" + hashlib.sha256((directory / "campaign-manifest.json").read_bytes()).hexdigest()
        code, _out, err = self.approve("c-libraries", digest)
        self.assertEqual(code, 0, err)
        record = json.loads((directory / "campaign-authorization.json").read_text(encoding="utf-8"))
        self.assertEqual([(item["name"], item["role"], item["ion_modes"]) for item in record["libraries"]],
                         [(item["name"], item["role"], item["ion_modes"]) for item in manifest["pins"]["libraries"]],
                         "Interactive's authorization takes the role and the ion modes; it refuses only a location")
        self.assertNotIn("Replaced.lbm2", json.dumps(record))
        # The annotation contract the gate's LIB-2 holds each unit's method to (review of 2026-10-25): the pinned
        # Interactive's profile, the LBM's cutoffs and priority, and its MSP rows, by name and never by location.
        tiered = annotation_pipeline.tiered_lcms_annotation("Approved.lbm2", "N.msp")
        negative = record["annotation"]["Negative"]
        self.assertEqual(record["annotation"], plan.annotation_contract(manifest))
        self.assertEqual((negative["profile_id"], negative["lbm"], negative["lbm_cutoffs"], negative["lbm_priority"]),
                         (profile_id, "Approved.lbm2", stated["lbm_cutoffs"], tiered["lbm_annotator"]["priority"]))
        self.assertEqual([(row["annotator_id"], row["library"], row["priority"], row["weighted_dot_product_cutoff"],
                           row["library_kind"]) for row in negative["msp_rows"]],
                         [(row["annotator_id"], "N.msp", row["priority"], row["weighted_dot_product_cutoff"], "acquired")
                          for row in tiered["msp_annotators"]])
        self.assertEqual({row["library"] for row in record["annotation"]["Positive"]["msp_rows"]}, {"P.msp"})
        self.assertNotIn(str(tools), json.dumps(record))
        self.assertIn(f"    MSP annotators of Negative units, as that profile sets them: {tiered['msp_annotators'][0]['annotator_id']} "
                      f"(N.msp; priority {tiered['msp_annotators'][0]['priority']}", out)

    def test_a_plan_whose_interactive_loads_its_official_lbm_is_not_approvable(self) -> None:
        """Review of 2026-10-25: every Interactive before 0.5.47 loaded its catalog's official LBM under
        tiered_lipid_msp, whatever LBM2 the answers named, and the plan approved the profile's all the same; each unit
        then downloaded and ran its diagnostic before LIB-2 stopped it. The plan now says so and refuses it."""
        code, out, err, directory, _tools = self.plan_tiered("c-official", loads_profile_lbm=False)
        self.assertEqual(code, 0, err)
        refusal = ("the pinned Interactive's tiered_lipid_msp strategy loads its library catalog's official LBM, not the "
                   "profile's Approved.lbm2, for Negative units: pin an Interactive that loads the LBM its answers name")
        self.assertIn("Not approvable", err)
        self.assertIn(refusal, err)
        self.assertIn("the profile's LBM Approved.lbm2, WHICH THE PINNED INTERACTIVE DOES NOT LOAD: its tiered_lipid_msp "
                      "strategy loads its library catalog's official LBM in its place", out)
        manifest = json.loads((directory / "campaign-manifest.json").read_text(encoding="utf-8"))
        self.assertIs(manifest["annotation"]["by_ion_mode"]["Positive"]["lbm_from_profile"], False)
        digest = "sha256:" + hashlib.sha256((directory / "campaign-manifest.json").read_bytes()).hexdigest()
        code, _out, err = self.approve("c-official", digest)
        self.assertEqual(code, runner_cli.EXIT_REFUSED)
        self.assertIn(refusal, err)
        self.assertFalse((directory / "ledger.sqlite").exists())
        self.assertFalse((directory / "campaign-authorization.json").exists())

    def test_the_plan_says_the_variable_named_its_root(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {runner_cli.WORKSPACE_ROOT_VARIABLE: str(self.workspace_root)}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = runner_cli.main([
                "--interactive-root", str(contract.INTERACTIVE_ROOT), "--catalog-root", str(contract.CATALOG_ROOT),
                "plan", "--campaign", "c-variable", "--pool", "declared", "--purpose", "a", "--retention", "keep",
                "--catalog", str(self.database)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn(f"Workspace root {self.workspace_root}, as MSDIAL_REPOSITORY_WORKSPACE_ROOT names it.", err.getvalue())
        self.assertIn(f"  workspace root {self.workspace_root}: ", out.getvalue())
        self.assertIn("  raw retention keep: each unit's raw data are kept", out.getvalue())
        self.assertTrue((self.workspace_root / "_campaigns" / "c-variable" / "campaign-manifest.json").is_file())


class LibraryPinTests(unittest.TestCase):
    """Which libraries the plan pins, and what the manifest states of their annotation (2026-10-24)."""

    PROFILE = {"schema": plan.PROFILE_SCHEMA,
               "answers": {"library_strategy": "tiered_lipid_msp", "libraries": {"lbm_path": "library:A.lbm2"}},
               "by_ion_mode": {"Positive": {"libraries": {"msp_paths": ["library:P.msp"]}},
                               "Negative": {"libraries": {"msp_paths": ["library:N.msp"], "lbm_path": "library:B.lbm2"},
                                            "workflow_overrides": {"lbm_reverse_dot_product": 0.3}}}}
    CODE = {"tiered": lambda lbm, msp: {"profile_id": f"tiered-{lbm}-{msp}", "msp_annotators": []},
            "lbm_cutoffs": {"weighted": 0.1, "simple": 0.1, "reverse": 0.1}, "lbm_cutoffs_source": "the template",
            "tiered_lbm_from_answers": True, "problems": []}

    def test_only_the_libraries_the_profile_names_are_pinned_with_their_ion_modes(self) -> None:
        pinned, unpinned = plan.split_libraries(self.PROFILE, ["P.msp", "N.msp", "A.lbm2", "B.lbm2", "Z.lbm2"])
        self.assertEqual((pinned, unpinned), (["A.lbm2", "B.lbm2", "N.msp", "P.msp"], ["Z.lbm2"]))
        pins = plan.library_pins([{"name": name, "sha256": "0" * 64, "bytes": 1} for name in pinned], self.PROFILE)
        self.assertEqual({item["name"]: (item["role"], item["ion_modes"]) for item in pins}, {
            "A.lbm2": ("lbm", ["Positive"]), "B.lbm2": ("lbm", ["Negative"]),
            "N.msp": ("msp", ["Negative"]), "P.msp": ("msp", ["Positive"])},
            "by_ion_mode replaces the LBM the answers name for Negative units, as Runner.answers merges them")
        self.assertEqual(plan.split_libraries(None, ["P.msp"]), ([], ["P.msp"]))

    def test_the_annotation_statement_is_the_pinned_interactives_and_the_profiles(self) -> None:
        record = plan.annotation_record(self.PROFILE, [{"name": "A.lbm2", "role": "lbm", "ion_modes": ["Positive"]}],
                                        ["Z.lbm2"], self.CODE)
        positive, negative = record["by_ion_mode"]["Positive"], record["by_ion_mode"]["Negative"]
        self.assertEqual((positive["profile_id"], positive["lbm"], positive["lbm_cutoffs"], positive["lbm_cutoffs_source"]),
                         ("tiered-A.lbm2-P.msp", "A.lbm2", {"weighted": 0.1, "simple": 0.1, "reverse": 0.1}, "the template"))
        self.assertEqual((negative["profile_id"], negative["lbm_cutoffs"]),
                         ("tiered-B.lbm2-N.msp", {"weighted": 0.1, "simple": 0.1, "reverse": 0.3}))
        self.assertIn("the profile's workflow_overrides (reverse)", negative["lbm_cutoffs_source"])
        text = "\n".join(record["statement"])
        self.assertIn("  libraries pinned, the ones the profile names: A.lbm2 (lbm; Positive)", text)
        self.assertIn("left unpinned, since the profile names none of them: Z.lbm2", text)
        self.assertIn("annotation of Negative units: library strategy tiered_lipid_msp; annotation profile "
                      "tiered-B.lbm2-N.msp (the pinned Interactive's annotation_pipeline); the profile's LBM B.lbm2; LBM cutoffs "
                      "weighted/simple/reverse 0.1/0.1/0.3 from the profile's workflow_overrides (reverse)", text)
        # Another strategy has no annotation profile, and what the pinned Interactive's code lacks is said.
        existing = {**self.PROFILE, "answers": {**self.PROFILE["answers"], "library_strategy": "existing"}}
        record = plan.annotation_record(existing, [], [], {"tiered": None, "lbm_cutoffs": None, "problems": ["no code"]})
        text = "\n".join(record["statement"])
        self.assertIn("annotation of Positive units: library strategy existing; annotation profile none (library "
                      "strategy existing); the profile's LBM A.lbm2; LBM cutoffs weighted/simple/reverse not readable", text)
        self.assertIn("  not read from the pinned Interactive: no code", text)
        self.assertIn("  libraries pinned: none", text)

    def test_a_manifest_planned_before_the_statement_says_so(self) -> None:
        manifest = {"campaign_id": "c", "pool": "declared", "totals": {
            "selected_units": 0, "excluded_units": 0, "planned_units": 0, "excluded_by_reason": {}, "units_of_unknown_size": 0,
            "download_groups": 0, "distinct_objects": 0, "per_unit_known_bytes": 0, "distinct_bytes_known": 0,
            "distinct_bytes_lower_bound": 0, "unknown_size_objects": 0, "empty_digest_objects": 0,
            "units_with_archives": 0, "class_kinds": {}}, "pins": {}, "policy": {}, "profile": None}
        text = plan.summary_text(manifest, "sha256:" + "0" * 64)
        self.assertIn("libraries and annotation: not stated (the manifest was planned before the plan stated them)", text)
        self.assertIn("  workspace root: not stated\n  raw retention not stated\n", text)
        self.assertIsNone(plan.annotation_contract(manifest))
        self.assertEqual(plan.annotation_problems(manifest), [])

    def test_the_tiered_profiles_lbm_priority_and_msp_rows_are_stated_by_name(self) -> None:
        """Review of 2026-10-25: the contract the authorization carries to LIB-2 holds every MSP row's thresholds
        and priority and its library kind, by file name, as the pinned Interactive's profile sets them."""
        def tiered(lbm, msp):
            row = {"msp_file_path": msp, "target_omics": "Metabolomics", "use_rt_scoring": False, "evidence_tier": "t"}
            return {"profile_id": "p1", "lbm_annotator": {"lbm_file_path": lbm, "priority": 3, "rt_tolerance": 100},
                    "msp_annotators": [{**row, "annotator_id": "strict", "priority": 2, "weighted_dot_product_cutoff": 0.6},
                                       {**row, "annotator_id": "predicted", "priority": 1, "library_kind": "InSilico",
                                        "msp_file_path": "E:\\private\\" + msp}]}

        record = plan.annotation_record(self.PROFILE, [], [], {**self.CODE, "tiered": tiered})
        positive = record["by_ion_mode"]["Positive"]
        self.assertEqual(positive["lbm_priority"], 3)
        self.assertEqual(positive["msp_rows"], [
            {"annotator_id": "strict", "library": "P.msp", "priority": 2, "weighted_dot_product_cutoff": 0.6,
             "use_rt_scoring": False, "target_omics": "Metabolomics", "library_kind": "acquired"},
            {"annotator_id": "predicted", "library": "P.msp", "priority": 1, "use_rt_scoring": False,
             "target_omics": "Metabolomics", "library_kind": "predicted"}])
        text = "\n".join(record["statement"])
        self.assertIn("LBM cutoffs weighted/simple/reverse 0.1/0.1/0.1 from the template; LBM priority 3", text)
        self.assertIn("    MSP annotators of Positive units, as that profile sets them: strict (P.msp; priority 2; MS1/MS2 "
                      "tolerance ?/?; weighted/simple/reverse 0.6/?/?;", text)
        self.assertIn("predicted (P.msp; priority 1;", text)
        manifest = {"annotation": record}
        contract_ = plan.annotation_contract(manifest)
        self.assertEqual(sorted(contract_), ["Negative", "Positive"])
        self.assertEqual(contract_["Positive"], {key: positive[key] for key in plan.ANNOTATION_CONTRACT_KEYS})
        self.assertNotIn("private", json.dumps(contract_), "names, never locations")
        self.assertEqual(plan.annotation_problems(manifest), [])

    def test_the_lbm_the_pinned_interactive_does_not_load_or_cannot_be_probed_is_said_and_refused(self) -> None:
        for loads, said, refused in (
                (False, ", WHICH THE PINNED INTERACTIVE DOES NOT LOAD", "loads its library catalog's official LBM, not the "
                 "profile's A.lbm2, for Positive units"),
                (None, ", which the pinned Interactive may not load: whether its tiered_lipid_msp strategy loads the LBM "
                 "the answers name could not be read", "whether the pinned Interactive's tiered_lipid_msp strategy loads "
                 "the profile's LBM A.lbm2 for Positive units could not be read")):
            with self.subTest(loads=loads):
                record = plan.annotation_record(self.PROFILE, [], [], {**self.CODE, "tiered_lbm_from_answers": loads})
                self.assertIs(record["by_ion_mode"]["Positive"]["lbm_from_profile"], loads)
                self.assertIn("the profile's LBM A.lbm2" + said, "\n".join(record["statement"]))
                problems = plan.annotation_problems({"annotation": record})
                self.assertEqual(len(problems), 2, problems)
                self.assertIn(refused, problems[1])
        # Another strategy, or the tiered one with no LBM named, is not probed.
        no_lbm = {"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "tiered_lipid_msp"},
                  "by_ion_mode": {"Positive": {"libraries": {"msp_paths": ["library:P.msp"]}}}}
        existing = {**self.PROFILE, "answers": {**self.PROFILE["answers"], "library_strategy": "existing"}}
        for profile in (no_lbm, existing):
            record = plan.annotation_record(profile, [], [], {**self.CODE, "tiered_lbm_from_answers": False})
            self.assertNotIn("lbm_from_profile", record["by_ion_mode"]["Positive"])
        self.assertEqual(plan.annotation_problems({"annotation": plan.annotation_record(existing, [], [], self.CODE)}), [])

    def test_a_strategy_that_loads_a_library_the_profile_does_not_name_is_said_and_refused(self) -> None:
        """Review 2: the plan pins only the libraries the profile names, so a library the strategy loads besides them
        (its catalog's official LBM under the tiered strategy given no libraries.lbm_path, its official library under
        the official strategy) is never approved, and LIB-2 would stop every unit after its download and diagnostic.
        The profile id of such a tiered profile was planned and approved all the same."""
        no_lbm = {"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "tiered_lipid_msp"},
                  "by_ion_mode": {"Positive": {"libraries": {"msp_paths": ["library:P.msp"]}},
                                  "Negative": {"libraries": {"msp_paths": ["library:N.msp"]}}}}
        official = {"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "official"},
                    "by_ion_mode": {"Positive": {"library_strategy": "tiered_lipid_msp",
                                                 "libraries": {"lbm_path": "library:A.lbm2", "msp_paths": ["library:P.msp"]}}}}
        named = ("name the library in the profile as library:<file name> (the LBM2 as libraries.lbm_path), and plan "
                 "again")
        # Review 3: the official strategy loads its catalog's library whatever the profile names, so naming one does
        # not clear the refusal; its remedy is the tiered strategy.
        tiered = ("use library strategy tiered_lipid_msp with the LBM2 named as libraries.lbm_path, as library:<file "
                  "name>, since the official strategy loads its catalog's library whatever the profile names, and plan "
                  "again")
        for profile, mode, loads, remedy in (
                (no_lbm, "Positive", "its library catalog's official LBM (the tiered_lipid_msp strategy, given no "
                                     "libraries.lbm_path)", named),
                (official, "Negative", "its library catalog's official library (library strategy official)", tiered)):
            with self.subTest(strategy=profile["answers"]["library_strategy"]):
                record = plan.annotation_record(profile, [], [], self.CODE)
                self.assertEqual(record["by_ion_mode"][mode]["unnamed_library"], loads)
                self.assertIn(f"the pinned Interactive loads {loads}, WHICH THE PLAN DOES NOT PIN: it pins only the "
                              "libraries the profile names", "\n".join(record["statement"]))
                problems = plan.annotation_problems({"annotation": record})
                self.assertIn(f"the pinned Interactive loads {loads} for {mode} units, which the plan does not pin, "
                              "since it pins only the libraries the profile names, so LIB-2 would stop every unit: "
                              f"{remedy}", "\n".join(problems))
        record = plan.annotation_record(official, [], [], self.CODE)
        self.assertNotIn("unnamed_library", record["by_ion_mode"]["Positive"], "the tiered profile given its LBM2")
        self.assertEqual(len(plan.annotation_problems({"annotation": record})), 1, "the official Negative units only")
        # Naming the library under the official strategy does not clear the refusal; following its remedy does.
        renamed = json.loads(json.dumps(official))
        renamed["answers"]["libraries"] = {"lbm_path": "library:A.lbm2", "msp_paths": ["library:N.msp"]}
        self.assertEqual(len(plan.annotation_problems({"annotation": plan.annotation_record(renamed, [], [], self.CODE)})), 1)
        renamed["answers"]["library_strategy"] = "tiered_lipid_msp"
        self.assertEqual(plan.annotation_problems({"annotation": plan.annotation_record(renamed, [], [], self.CODE)}), [])
        # Review 3: a unit runs in the ion mode its raw data give (a Mixed split's part, a header that overrides the
        # declaration) and takes that mode's answers, so a mode no unit is declared in is refused all the same.
        for units in ([{"ion_mode": "Positive"}], [{"ion_mode": "Negative"}], [{"ion_mode": "Both"}], []):
            with self.subTest(units=units):
                self.assertEqual(len(plan.annotation_problems({"annotation": record, "units": units})), 1)

    @staticmethod
    def two_tiers(lbm, msp):
        row = {"msp_file_path": msp, "target_omics": "Metabolomics", "rt_tolerance": 0.5, "use_rt_scoring": False,
               "use_rt_filtering": False}
        return {"profile_id": "p1", "lbm_annotator": {"lbm_file_path": lbm, "priority": 3},
                "msp_annotators": [{**row, "annotator_id": "strict", "priority": 2},
                                   {**row, "annotator_id": "broad", "priority": 1}]}

    def test_the_answers_retention_time_use_is_applied_to_the_msp_rows_as_the_pinned_interactive_applies_it(self) -> None:
        """Review 2: Interactive's _workflow applies the answers' retention-time use to every MSP row right after the
        tiered profile sets them (agent_workflow._apply_retention_time_use, from 0.5.45), and the contract was read
        from the profile alone: it stated False/False and 0.5 where the run wrote True/True and the answers' tolerance,
        so LIB-2 would have failed every unit of a profile that answers true, and the plan approved it."""
        applied = []

        def retention_time_use(state, answers):
            # As agent_workflow._apply_retention_time_use rewrites MSP rows from 0.5.45.
            applied.append(answers)
            use = bool(answers["use_retention_time_for_annotation"])
            for row in state["msp_annotators"]:
                row["use_rt_scoring"] = row["use_rt_filtering"] = use
                if use and answers.get("retention_time_tolerance") is not None:
                    row["rt_tolerance"] = float(answers["retention_time_tolerance"])

        profile = json.loads(json.dumps(self.PROFILE))
        profile["answers"].update(use_retention_time_for_annotation=True, retention_time_tolerance=0.3)
        code = {**self.CODE, "tiered": self.two_tiers, "retention_time_use": retention_time_use}
        record = plan.annotation_record(profile, [], [], code)
        rows = record["by_ion_mode"]["Positive"]["msp_rows"]
        self.assertEqual([(row["annotator_id"], row["rt_tolerance"], row["use_rt_scoring"], row["use_rt_filtering"])
                          for row in rows], [("strict", 0.3, True, True), ("broad", 0.3, True, True)])
        self.assertEqual({answers["retention_time_tolerance"] for answers in applied}, {0.3}, "the ion mode's answers")
        text = "\n".join(record["statement"])
        self.assertIn("    MSP annotators of Positive units, as that profile sets them, with the answers' retention-time "
                      "use applied as the pinned Interactive's workflow applies it: strict (P.msp; priority 2;", text)
        self.assertIn("RT scoring/filtering True/True within 0.3 min;", text)
        self.assertEqual(plan.annotation_contract({"annotation": record})["Negative"]["msp_rows"][0]["rt_tolerance"], 0.3)
        # Answers that give no retention-time use need nothing applied: the profile's rows as they are.
        record = plan.annotation_record(self.PROFILE, [], [], {**code, "retention_time_use": None})
        self.assertEqual([(row["rt_tolerance"], row["use_rt_scoring"]) for row in record["by_ion_mode"]["Positive"]["msp_rows"]],
                         [(0.5, False), (0.5, False)])
        self.assertNotIn("not read from the pinned Interactive", "\n".join(record["statement"]))
        # Answers that give one, where how Interactive applies it cannot be read: no rows are stated, and that is said.
        for unread in (None, lambda state, answers: float("not a number")):
            with self.subTest(unread=unread):
                record = plan.annotation_record(profile, [], [], {**code, "retention_time_use": unread})
                self.assertIsNone(record["by_ion_mode"]["Positive"]["msp_rows"])
                self.assertEqual(record["by_ion_mode"]["Positive"]["profile_id"], "p1")
                self.assertIn("so the rows are not stated", "\n".join(record["statement"]))
                # Review 3: LIB-2 would compare no rows, so the manifest is not approvable.
                self.assertIn("the plan could not read its MSP annotator rows for Positive units from the pinned "
                              "Interactive (the summary says why), so LIB-2 would not hold the method to them",
                              "\n".join(plan.annotation_problems({"annotation": record})))

    @unittest.skipUnless(contract.AVAILABLE, "the Interactive checkout is not where this test looks")
    def test_the_contract_rows_are_the_rows_the_pinned_interactives_workflow_leaves(self) -> None:
        """Review 2: no test held the contract to the MSP rows Interactive's own _workflow leaves for the method, its
        library strategy and then its retention-time use on the same answers; the gate's tests state both sides. Here
        the real Interactive writes them (its catalog stood in for, nothing read), for each retention-time answer."""
        from msdial_app import agent_workflow

        found = plan.interactive_annotation()
        official = {"id": "lipidomics", "kind": "lbm", "local_path": "Synthetic-official.lbm2", "record_id": "r",
                    "record_url": "", "doi": "", "license": "", "downloaded": True}
        for extra in ({}, {"use_retention_time_for_annotation": False}, {"use_retention_time_for_annotation": True},
                      {"use_retention_time_for_annotation": True, "retention_time_tolerance": 0.3}):
            with self.subTest(**extra):
                profile = {"schema": plan.PROFILE_SCHEMA,
                           "answers": {"library_strategy": "tiered_lipid_msp",
                                       "libraries": {"lbm_path": "library:Synthetic.lbm2"}, **extra},
                           "by_ion_mode": {"Positive": {"libraries": {"msp_paths": ["library:SyntheticPositive.msp"]}}}}
                stated = plan.annotation_record(profile, [], [], found)["by_ion_mode"]["Positive"]["msp_rows"]
                answers = {"library_strategy": "tiered_lipid_msp", **extra,
                           "libraries": {"lbm_path": "Synthetic.lbm2", "msp_paths": ["SyntheticPositive.msp"]}}
                state: dict = {"library_provenance": []}
                with mock.patch.object(agent_workflow, "catalog_status", return_value=[dict(official)]):
                    agent_workflow._apply_libraries(state, {}, answers)
                agent_workflow._apply_retention_time_use(state, answers)
                self.assertTrue(state["msp_annotators"])
                self.assertEqual(stated, [plan.msp_row_contract(row) for row in state["msp_annotators"]])

    def test_the_probe_reads_which_lbm_the_tiered_strategy_loads_and_consults_no_catalog(self) -> None:
        consulted = []

        def catalog_status():
            consulted.append(1)
            return [{"id": "lipidomics", "kind": "lbm", "local_path": "Official.lbm2"}]

        def before_0_5_47(state, _loaded, answers):
            # The catalog's official LBM, whatever the answers name.
            state["lbm_path"] = next(item for item in old.catalog_status() if item["id"] == "lipidomics")["local_path"]

        def from_0_5_47(state, _loaded, answers):
            state["lbm_path"] = os.path.abspath(answers["libraries"]["lbm_path"])

        old = types.SimpleNamespace(catalog_status=catalog_status, _apply_libraries=before_0_5_47)
        self.assertIs(plan.tiered_lbm_from_answers(old), False)
        self.assertIs(old.catalog_status, catalog_status, "the catalog is put back")
        self.assertEqual(consulted, [], "the real catalog is never consulted")
        self.assertIs(plan.tiered_lbm_from_answers(types.SimpleNamespace(_apply_libraries=from_0_5_47)), True)

    def test_a_template_the_answers_name_comes_before_the_settings_one_and_overrides_before_both(self) -> None:
        """Review of 2026-10-25: Interactive's _workflow takes the answers' template_path before its settings'; the
        plan read the settings' alone. Review 3: the profile names it as library:<file name>, which the plan read
        unresolved, so it named no file and Interactive's own template's cutoffs were stated under the answers' name;
        it is read where the resource map locates it, as Runner.answers resolves it. Overrides that set some cutoffs
        take the others from that template, which Interactive applies them over, not from the settings' one."""
        profile = json.loads(json.dumps(self.PROFILE))
        profile["by_ion_mode"]["Positive"]["template_path"] = "library:answers-template.txt"
        profile["by_ion_mode"]["Negative"]["template_path"] = "library:answers-template.txt"
        located = os.path.join("synthetic", "resources", "answers-template.txt")
        read = []

        def template_cutoffs(answer):
            read.append(answer)
            return ({"weighted": 0.2, "simple": 0.2, "reverse": 0.4},
                    f"the parameter template {Path(answer).name}, which the answers name")

        code = {**self.CODE, "template_cutoffs": template_cutoffs}
        record = plan.annotation_record(profile, [], [], code, {"answers-template.txt": located})
        positive, negative = record["by_ion_mode"]["Positive"], record["by_ion_mode"]["Negative"]
        self.assertEqual((positive["lbm_cutoffs"], positive["lbm_cutoffs_source"]),
                         ({"weighted": 0.2, "simple": 0.2, "reverse": 0.4},
                          "the parameter template answers-template.txt, which the answers name"))
        self.assertEqual((negative["lbm_cutoffs"], negative["lbm_cutoffs_source"]),
                         ({"weighted": 0.2, "simple": 0.2, "reverse": 0.3},
                          "the profile's workflow_overrides (reverse), the others from the parameter template "
                          "answers-template.txt, which the answers name"),
                         "the profile's workflow_overrides over the template the answers name")
        self.assertEqual(read, [located, located], "the template's location, never the unresolved reference")
        self.assertNotIn("synthetic", json.dumps(record), "the location is read, and stated nowhere")
        self.assertEqual(plan.annotation_problems({"annotation": record}), [])
        # A template the resource map does not locate is not read (nor Interactive's own in its place): no cutoffs are
        # stated, that is said, and the manifest is not approvable.
        read.clear()
        record = plan.annotation_record(profile, [], [], code, {})
        self.assertEqual(read, [])
        self.assertIsNone(record["by_ion_mode"]["Positive"]["lbm_cutoffs"])
        self.assertIn("the parameter template the answers name (answers-template.txt) is not one the resource map "
                      "locates, so its LBM cutoffs were not read", "\n".join(record["statement"]))
        self.assertIn("the plan could not read its LBM cutoffs for Negative units from the pinned Interactive",
                      "\n".join(plan.annotation_problems({"annotation": record})))
        # One named other than as a library is held by no pin, so the profile is refused.
        relative = json.loads(json.dumps(profile))
        relative["by_ion_mode"]["Negative"]["template_path"] = "answers-template.txt"
        names = ["A.lbm2", "B.lbm2", "P.msp", "N.msp", "answers-template.txt"]
        self.assertEqual(plan.template_path_problems(profile), [])
        self.assertNotIn("template_path", "\n".join(plan.profile_problems(profile, names)))
        self.assertIn("the answers of Negative units name a parameter template (template_path) other than as "
                      "library:<file name>, which no pin holds", "\n".join(plan.profile_problems(relative, names)))

    def test_the_summary_says_which_template_each_ion_mode_takes_and_what_holds_it(self) -> None:
        """Review 3: the summary said the template pin held the run's template for a profile whose answers named
        another, which only the library pin holds."""
        manifest = {"pins": {"template": {"name": "t.txt", "sha256": "ab" * 32, "named_by": "settings",
                                          "ion_modes": ["Positive"]}},
                    "profile": json.loads(json.dumps(self.PROFILE))}
        manifest["profile"]["by_ion_mode"]["Negative"]["template_path"] = "library:lab-template.txt"
        self.assertEqual(plan.template_pin_text(manifest), [
            "  LC-MS parameter template of Negative units lab-template.txt, which the answers name, pinned as a library "
            "by its sha256: a change to it pauses the campaign before the next unit or Console start",
            "  LC-MS parameter template of Positive units t.txt, which Interactive's settings name, pinned by its sha256 "
            "abababababab...: a change to it pauses the campaign before the next unit or Console start"])
        manifest["profile"]["answers"]["template_path"] = "library:lab-template.txt"
        manifest["pins"]["template"] = {"named_by": "answers", "ion_modes": []}
        self.assertEqual(plan.template_pin_text(manifest), [
            "  LC-MS parameter template of Positive and Negative units lab-template.txt, which the answers name, pinned "
            "as a library by its sha256: a change to it pauses the campaign before the next unit or Console start"])
        manifest["profile"]["answers"]["template_path"] = "lab-template.txt"
        self.assertIn("other than as library:<file name>: NOT PINNED", plan.template_pin_text(manifest)[0])

    def test_a_tiered_contract_the_plan_could_not_read_is_not_approvable(self) -> None:
        """Review 3: annotation_record leaves a contract part unstated where the pinned Interactive could not be read,
        and LIB-2 compares only what is stated, so an approval of it let cutoff, priority or MSP-row drift pass."""
        def unreadable(lbm, msp):
            raise RuntimeError("synthetic")

        for code, unread in (({**self.CODE, "tiered": unreadable}, "its annotation profile, its MSP annotator rows"),
                             ({**self.CODE, "tiered": None}, "its annotation profile, its MSP annotator rows"),
                             ({**self.CODE, "lbm_cutoffs": None}, "its LBM cutoffs")):
            with self.subTest(unread=unread):
                problems = plan.annotation_problems({"annotation": plan.annotation_record(self.PROFILE, [], [], code)})
                self.assertEqual(len(problems), 2, problems)
                self.assertIn(f"the plan could not read {unread} for Negative units from the pinned Interactive",
                              problems[0])
        # Another strategy states no tiered contract, and a manifest planned before the contract says nothing.
        existing = {**self.PROFILE, "answers": {**self.PROFILE["answers"], "library_strategy": "existing"}}
        self.assertEqual(plan.annotation_problems({"annotation": plan.annotation_record(
            existing, [], [], {**self.CODE, "tiered": None, "lbm_cutoffs": None})}), [])
        self.assertEqual(plan.annotation_problems({"annotation": {"by_ion_mode": {
            "Positive": {"library_strategy": "tiered_lipid_msp", "lbm_from_profile": True}}}}), [])

    @unittest.skipUnless(contract.AVAILABLE, "the Interactive checkout is not where this test looks")
    def test_the_pinned_interactive_is_read_as_its_workflow_reads_it(self) -> None:
        """Against the real Interactive: the probe answers and puts its catalog back, an Interactive before 0.5.47 is
        found to load its official LBM, and a template the answers name is read before the settings' one."""
        import msdial_app
        from msdial_app import agent_workflow

        catalog = agent_workflow.catalog_status
        loads = plan.tiered_lbm_from_answers(agent_workflow)
        self.assertIsInstance(loads, bool)
        self.assertIs(agent_workflow.catalog_status, catalog)
        version = tuple(int(part) for part in str(msdial_app.__version__).split(".")[:3])
        if version <= (0, 5, 46):
            self.assertIs(loads, False, "tiered_lipid_msp loaded the catalog's LBM before 0.5.47")
        if not isinstance(getattr(agent_workflow, "LBM_TEMPLATE_CUTOFFS", None), dict):
            self.skipTest("this Interactive carries no template's LBM cutoffs into the method")
        own = Path(agent_workflow.RESOURCES) / plan.DEFAULT_TEMPLATE
        with tempfile.TemporaryDirectory() as directory:
            templates = {}
            for name, value in (("settings.txt", "0.15"), ("answers.txt", "0.25")):
                templates[name] = Path(directory) / name
                templates[name].write_text(own.read_text(encoding="utf-8-sig").replace(
                    "Reverse dot product cutoff for LBM-based annotation: 0.1",
                    f"Reverse dot product cutoff for LBM-based annotation: {value}"), encoding="utf-8")
            with mock.patch.object(agent_workflow, "load_user_settings",
                                   return_value={"template_path": str(templates["settings.txt"])}):
                found = plan.interactive_annotation()
            self.assertIs(found["tiered_lbm_from_answers"], loads)
            self.assertEqual((found["lbm_cutoffs"]["reverse"], found["lbm_cutoffs_source"]),
                             (0.15, "the parameter template settings.txt, which Interactive's settings name"))
            self.assertEqual(found["template_cutoffs"](str(templates["answers.txt"])),
                             ({"weighted": 0.1, "simple": 0.1, "reverse": 0.25},
                              "the parameter template answers.txt, which the answers name"))
            self.assertEqual(found["template_cutoffs"](str(Path(directory) / "missing.txt"))[1],
                             f"the parameter template {plan.DEFAULT_TEMPLATE}, Interactive's own",
                             "an answer naming no file falls back to Interactive's own, not to the settings'")

    @unittest.skipUnless(contract.AVAILABLE, "the Interactive checkout is not where this test looks")
    def test_the_console_pin_records_the_version_its_build_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            console = Path(directory) / "MSDIALCUI.exe"
            console.write_bytes(b"MZ console")
            reader = ports.PinReader(console_path=str(console), extractor_path="", libraries={})
            self.assertNotIn("version", reader.console(), "no build provenance, no version")
            sidecar = Path(directory) / "msdial-console-build-provenance.json"
            recorded = {"binary_version_output": "5.5.260101 / commit 0123abc45", "git_head": "0123abc45" + "0" * 31}
            # Review 2: a build record of other bytes (one left beside a rebuilt or replaced Console) gives no version.
            for stale in ({}, {"binary_sha256": "f" * 64}):
                sidecar.write_text(json.dumps({**recorded, **stale}), encoding="utf-8")
                pin = reader.console()
                self.assertIs(pin["provenance_matches"], False)
                self.assertNotIn("version", pin)
                self.assertEqual(machine._console_version(pin), "", "nor its commit")
            sidecar.write_text(json.dumps({**recorded, "binary_sha256": hashlib.sha256(b"MZ console").hexdigest()}),
                               encoding="utf-8")
            pin = reader.console()
            self.assertIs(pin["provenance_matches"], True)
            self.assertEqual(pin["version"], "5.5.260101 / commit 0123abc45")
            self.assertEqual(machine._console_version(pin), "5.5.260101 / commit 0123abc45")
            self.assertEqual(policy.pin_differences({"console": pin}, {"console": {**pin, "version": "other"}}), [],
                             "the version is recorded, not an identity: the bytes are")

    @unittest.skipUnless(contract.AVAILABLE, "the Interactive checkout is not where this test looks")
    def test_the_template_the_cutoffs_come_from_is_pinned_by_its_bytes(self) -> None:
        """Review 2: the plan reads the LBM cutoffs from the template Interactive's settings name, which its GUI's path
        save rewrites, and no pin covered it. The pin names the same template the plan reads (and its own where the
        settings name none or a file that is not there), by name and sha256, never by location; changed bytes are a
        pin difference."""
        from msdial_app import agent_workflow

        own = Path(agent_workflow.RESOURCES) / plan.DEFAULT_TEMPLATE
        reader = ports.PinReader(console_path="", extractor_path="", libraries={})
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "lab settings" / "synthetic-template.txt"
            settings.parent.mkdir()
            settings.write_bytes(own.read_bytes())
            for named, expected in ((None, ("interactive", own)), (str(Path(directory) / "missing.txt"), ("interactive", own)),
                                    (str(settings), ("settings", settings))):
                with self.subTest(named=named), mock.patch.object(
                        agent_workflow, "load_user_settings", return_value={"template_path": named} if named else {}):
                    pin = reader.template()
                    found = plan.interactive_annotation()
                self.assertEqual((pin["named_by"], pin["name"], pin["sha256"]),
                                 (expected[0], expected[1].name, hashlib.sha256(expected[1].read_bytes()).hexdigest()))
                if isinstance(getattr(agent_workflow, "LBM_TEMPLATE_CUTOFFS", None), dict):
                    self.assertIn(f"the parameter template {expected[1].name}", found["lbm_cutoffs_source"],
                                  "the plan reads its cutoffs from the template the pin names")
                self.assertNotIn("lab settings", json.dumps(pin), "a name and a sha256, never a location")
            # What the runner compares before each unit and Console start (PinReader.current).
            with mock.patch.object(agent_workflow, "load_user_settings", return_value={"template_path": str(settings)}), \
                    mock.patch.multiple(ports.PinReader, console=lambda _self: {}, extractor=lambda _self: {},
                                        code=lambda _self: {}):
                before = reader.current()["template"]
                settings.write_bytes(own.read_bytes() + b"\nsynthetic change\n")
                after = reader.current()["template"]
            self.assertEqual(policy.pin_differences({"template": before}, {"template": after}), ["template.sha256"])
            # Review 3: given the profile, the pin names the ion modes whose units take this template; where every
            # mode's answers name one (pinned as a library), no unit takes it and its bytes are not pinned, so a change
            # to it pauses nothing.
            profile = {"schema": plan.PROFILE_SCHEMA, "answers": {},
                       "by_ion_mode": {"Negative": {"template_path": "library:synthetic-lab-template.txt"}}}
            with mock.patch.object(agent_workflow, "load_user_settings", return_value={"template_path": str(settings)}):
                pin = reader.template(profile)
                self.assertEqual((pin["ion_modes"], pin["name"], pin["named_by"]),
                                 (["Positive"], settings.name, "settings"))
                profile["answers"]["template_path"] = "library:synthetic-lab-template.txt"
                pin = reader.template(profile)
            self.assertEqual(pin, {"named_by": "answers", "ion_modes": []})
            self.assertEqual(policy.pin_differences({"template": pin}, {"template": after}), [])
            self.assertNotIn("ion_modes", after, "read as it is, without the profile, for the comparison")

    def test_a_root_on_the_system_drive_in_the_profile_or_in_temp_is_said_first(self) -> None:
        """Review 2: the runner refuses no root on C:, in the user profile or in a temporary folder (its tests plan in
        temporary folders), and setup-windows.ps1 checks only the root it is given, so the words a person approves
        say where such a root lies, in capitals, in a manifest whose digest covers them."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            environment = {"SystemDrive": base.drive, "USERPROFILE": str(base / "profile"), "TEMP": str(base / "profile" / "tmp"),
                           "TMP": str(base / "other-tmp")}
            cautions = plan.workspace_root_cautions
            self.assertEqual(cautions(base / "profile" / "tmp" / "analysis", environment),
                             [f"on the system drive {base.drive}", "inside the user profile", "inside a temporary folder"])
            self.assertEqual(cautions(str(base / "OTHER-TMP" / "x"), environment),
                             [f"on the system drive {base.drive}", "inside a temporary folder"], "in another case")
            self.assertEqual(cautions(base / "profiles" / "analysis", {**environment, "SystemDrive": "Q:"}), [],
                             "a folder whose name begins like the profile's is not inside it")
            self.assertEqual(cautions("", environment), [])
        manifest = {"campaign_id": "c", "pool": "declared", "totals": {
            "selected_units": 0, "excluded_units": 0, "planned_units": 0, "excluded_by_reason": {}, "units_of_unknown_size": 0,
            "download_groups": 0, "distinct_objects": 0, "per_unit_known_bytes": 0, "distinct_bytes_known": 0,
            "distinct_bytes_lower_bound": 0, "unknown_size_objects": 0, "empty_digest_objects": 0,
            "units_with_archives": 0, "class_kinds": {}}, "pins": {}, "policy": {}, "profile": None,
            "workspace_root": "C:\\analysis",
            "workspace_root_cautions": ["on the system drive C:", "inside the user profile", "inside a temporary folder"]}
        text = plan.summary_text(manifest, "sha256:" + "0" * 64)
        self.assertIn("  workspace root C:\\analysis: every unit's raw data, MS-DIAL outputs and records, and this "
                      "campaign's ledger, are written under it\n  THE WORKSPACE ROOT IS ON THE SYSTEM DRIVE C:, INSIDE THE "
                      "USER PROFILE AND INSIDE A TEMPORARY FOLDER, where the project keeps no campaign data: approve it "
                      "only if that is meant\n  raw retention", text)
        self.assertIn("  LC-MS parameter template: not pinned", text)
        manifest["workspace_root_cautions"] = []
        manifest["pins"] = {"template": {"name": "t.txt", "sha256": "ab" * 32, "named_by": "settings"}}
        text = plan.summary_text(manifest, "sha256:" + "0" * 64)
        self.assertNotIn("THE WORKSPACE ROOT IS", text)
        self.assertIn("  LC-MS parameter template t.txt, which Interactive's settings name, pinned by its sha256 "
                      "abababababab...: a change to it pauses the campaign before the next unit or Console start", text)


LOCAL_SUPPORT_KEY = "automatic rt correction local support rt window"  # MsdialWorkbench #826
RUN_WIDE_KEY = "execute automatic rt correction for alignment"  # MsdialWorkbench #810


class AutomaticRtCorrectionPinTests(unittest.TestCase):
    """The campaign runs automatic alignment RT correction with #826's local outlier test (decided 2026-10-07).
    A profile that turns it on is approvable only with a Console pin that implements it."""

    RT_PROFILE = {"schema": plan.PROFILE_SCHEMA,
                  "answers": {"library_strategy": "existing", "execute_automatic_rt_correction": True,
                              "automatic_rt_correction_maximum_anchors": 12},
                  "by_ion_mode": {"Positive": {"libraries": {"msp_paths": ["library:P.msp"]}}}}

    def manifest(self, profile: dict, console: dict, recorded_policy: dict | None = None) -> dict:
        """A manifest as the plan writes it now: with the automatic RT correction statement its digest covers."""
        pins = json.loads(json.dumps(APPROVABLE_PINS))
        pins["console"] = console
        built = {"pins": pins, "profile": json.loads(json.dumps(profile)), "units": [{"unit_key": "uA"}],
                 "raw_retention_policy": "keep", "policy_overrides": []}
        if recorded_policy is not None:
            built["policy"] = json.loads(json.dumps(recorded_policy))
        built["automatic_rt_correction"] = plan.automatic_rt_correction_record(built.get("policy"), built["profile"], [])
        return built

    @staticmethod
    def planned_before_the_statement(manifest: dict) -> dict:
        """The same manifest as a plan made before #36 wrote it: no statement, no policy_overrides."""
        legacy = json.loads(json.dumps(manifest))
        legacy.pop("automatic_rt_correction")
        legacy.pop("policy_overrides")
        return legacy

    def test_the_generation_is_read_from_the_method_keys_in_the_assembly(self) -> None:
        cases = {
            "826, as .NET stores it": (b"MZ" + RUN_WIDE_KEY.encode("utf-16-le") + LOCAL_SUPPORT_KEY.encode("utf-16-le"),
                                        policy.AUTOMATIC_RT_LOCAL_SUPPORT),
            "826, in UTF-8": (b"MZ" + LOCAL_SUPPORT_KEY.encode("utf-8"), policy.AUTOMATIC_RT_LOCAL_SUPPORT),
            "810 alone": (b"MZ" + RUN_WIDE_KEY.encode("utf-16-le"), policy.AUTOMATIC_RT_RUN_WIDE),
            "neither": (b"MZ console", policy.AUTOMATIC_RT_NONE),
            # The title-case label is MsdialCore.dll's; a Console beside a newer core does not carry the key.
            "the label only": (b"MZ" + "Automatic RT correction local support RT window".encode("utf-16-le"),
                               policy.AUTOMATIC_RT_NONE),
        }
        for name, (assembly, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(ports.automatic_rt_correction_generation(assembly), expected)

    def test_an_rt_correction_profile_needs_a_console_of_826(self) -> None:
        base = {**APPROVABLE_PINS["console"], "assembly_sha256": "1" * 64, "inventory_sha256": "1" * 64}
        base.pop("automatic_rt_correction")
        covers = ["1", "3", "4"]
        ok = self.manifest(self.RT_PROFILE, {**base, "automatic_rt_correction": policy.AUTOMATIC_RT_LOCAL_SUPPORT})
        self.assertEqual(plan.approval_problems(ok, covers), [])
        cases = {
            "810 alone": (policy.AUTOMATIC_RT_RUN_WIDE, "implements MsdialWorkbench #810's run-wide outlier test only"),
            "neither": (policy.AUTOMATIC_RT_NONE, "implements no automatic RT correction"),
            "a pin read before the field existed": (None, "does not record which correction"),
        }
        for name, (generation, words) in cases.items():
            with self.subTest(name):
                console = dict(base)
                if generation is not None:
                    console["automatic_rt_correction"] = generation
                problems = plan.approval_problems(self.manifest(self.RT_PROFILE, console), covers)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(words, problems[0])
                self.assertIn("automatic RT correction on", problems[0])

    def test_a_profile_without_rt_correction_takes_any_console(self) -> None:
        off = json.loads(json.dumps(self.RT_PROFILE))
        off["answers"]["execute_automatic_rt_correction"] = "false"
        for profile in (off, {"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "existing"}, "by_ion_mode": {}}):
            with self.subTest(profile=profile["answers"]):
                self.assertFalse(plan.automatic_rt_correction_requested(profile))
                manifest = self.manifest(profile, {**APPROVABLE_PINS["console"], "automatic_rt_correction": policy.AUTOMATIC_RT_NONE})
                self.assertEqual(plan.approval_problems(manifest, ["1", "3", "4"]), [])

    def test_rt_correction_is_found_wherever_the_profile_turns_it_on(self) -> None:
        for name, profile in {
            "an ion mode": {"answers": {}, "by_ion_mode": {"Negative": {"execute_automatic_rt_correction": "true"}}},
            "an override": {"answers": {"workflow_overrides": {"execute_automatic_rt_correction": "on"}}, "by_ion_mode": {}},
            "an ion mode's override": {"answers": {}, "by_ion_mode": {"Positive": {"workflow_overrides": {"execute_automatic_rt_correction": 1}}}},
        }.items():
            with self.subTest(name):
                self.assertTrue(plan.automatic_rt_correction_requested(profile))
        self.assertFalse(plan.automatic_rt_correction_requested(None))
        self.assertFalse(plan.automatic_rt_correction_requested({"answers": {"automatic_rt_correction_maximum_anchors": 12}}))

    def pinned(self, profile: dict, generation: str = policy.AUTOMATIC_RT_LOCAL_SUPPORT) -> dict:
        """A manifest whose campaign policy pins the correction, as every plan made since 2026-10-07 records it."""
        return self.manifest(profile, {**APPROVABLE_PINS["console"], "automatic_rt_correction": generation},
                             policy.CampaignPolicy().as_dict())

    def test_the_campaign_policy_pins_the_correction_and_needs_a_console_of_826(self) -> None:
        plain = {"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "existing"}, "by_ion_mode": {}}
        self.assertEqual(plan.approval_problems(self.pinned(plain), ["1", "3", "4"]), [],
                         "a profile need not state what the policy pins")
        self.assertEqual(plan.approval_problems(self.pinned(self.RT_PROFILE), ["1", "3", "4"]), [],
                         "nor is one refused for stating it as pinned")
        for generation, words in ((policy.AUTOMATIC_RT_RUN_WIDE, "#810's run-wide"), (policy.AUTOMATIC_RT_NONE, "no automatic RT")):
            with self.subTest(generation):
                problems = plan.approval_problems(self.pinned(plain, generation), ["1", "3", "4"])
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("the campaign policy pins automatic RT correction on", problems[0])
                self.assertIn(words, problems[0])

    def test_a_profile_that_says_otherwise_than_the_pin_is_not_approvable(self) -> None:
        cases = {
            "correction off": ({"answers": {"execute_automatic_rt_correction": False}}, "turns automatic RT correction off (answers)"),
            "off for an ion mode's override": (
                {"by_ion_mode": {"Negative": {"workflow_overrides": {"execute_automatic_rt_correction": "false"}}}},
                "(by_ion_mode.Negative.workflow_overrides)"),
            "Interactive's default of 6 anchors": ({"answers": {"automatic_rt_correction_maximum_anchors": 6}}, "pins 12"),
            "the anchor-library correction": ({"answers": {"execute_rt_correction": True}}, "the automatic correction alone"),
            "the run-wide test only": ({"answers": {"automatic_rt_correction_local_support_rt_window": 0}}, "default window of 1.5 min"),
            # The runner sets Blank interpolation for each unit from its recorded order, whatever the profile says.
            "Blank interpolation": (
                {"answers": {"automatic_rt_correction_interpolate_blanks_by_analytical_order": True}},
                "the runner sets it for each unit"),
        }
        for name, (parts, words) in cases.items():
            with self.subTest(name):
                profile = {"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "existing"}, "by_ion_mode": {}}
                for key, value in parts.items():
                    profile[key] = {**profile[key], **value}
                problems = plan.approval_problems(self.pinned(profile), ["1", "3", "4"])
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(words, problems[0])
        stated = {"schema": plan.PROFILE_SCHEMA, "by_ion_mode": {}, "answers": {
            "library_strategy": "existing", "execute_automatic_rt_correction": "true", "execute_rt_correction": False,
            "automatic_rt_correction_maximum_anchors": "12", "automatic_rt_correction_local_support_rt_window": 1.5}}
        self.assertEqual(plan.approval_problems(self.pinned(stated), ["1", "3", "4"]), [])

    def test_the_statement_follows_the_policy_and_the_profile(self) -> None:
        stated = plan.automatic_rt_correction_record(
            policy.CampaignPolicy().as_dict(),
            {"answers": {"automatic_rt_correction_local_support_rt_window": 1.5}}, [])
        self.assertEqual((stated["local_support_rt_window_min"], stated["local_support_rt_window_source"]), (1.5, "profile"))
        self.assertIn("local window 1.5 (answers) min as the profile states it", "\n".join(stated["statement"]))
        # Not pinned, each unit runs as its profile says, and the statement says what that is.
        unpinned = policy.CampaignPolicy.from_dict({"automatic_rt_correction": False}).as_dict()
        off = plan.automatic_rt_correction_record(unpinned, None, ["automatic_rt_correction"])
        self.assertEqual(off["correction"], False)
        self.assertIn("Each unit runs as the profile says: OFF", off["statement"][1])
        on = plan.automatic_rt_correction_record(unpinned, {"answers": {"execute_automatic_rt_correction": True,
                                                                        "automatic_rt_correction_maximum_anchors": 8}},
                                                 ["automatic_rt_correction"])
        self.assertEqual(on["correction"], True)
        text = "\n".join(on["statement"])
        self.assertIn("Each unit runs as the profile says: ON", text)
        self.assertIn("maximum anchors 8 (answers)", text)
        self.assertIn("fallback OFF: the runner falls back to an uncorrected run only under the campaign policy's pin", text)
        self.assertIn("Blanks: Interactive's default", text)
        # A policy recorded before 2026-10-07 differs from the decision, and which fields were overridden is unknown.
        legacy = {key: value for key, value in policy.CampaignPolicy().as_dict().items() if not key.startswith("automatic_rt")}
        record = plan.manifest_automatic_rt_correction({"policy": legacy, "profile": None})
        self.assertEqual((record["pinned"], record["source"], len(record["differs_from_decision"])), (False, "unrecorded", 3))
        self.assertTrue(record["statement"][0].startswith("  !! AUTOMATIC RT CORRECTION DIFFERS"))

    def test_an_unpinned_correction_is_stated_for_each_ion_mode(self) -> None:
        unpinned = policy.CampaignPolicy.from_dict({"automatic_rt_correction": False}).as_dict()
        on = {"execute_automatic_rt_correction": True}
        off = {"execute_automatic_rt_correction": False}
        cases = {
            "on for Positive only": (
                {"by_ion_mode": {"Positive": on}},
                "Positive units ON (set by by_ion_mode.Positive), Negative units OFF (not set; Interactive's default is off)"),
            "on, off for Negative": (
                {"answers": on, "by_ion_mode": {"Negative": off}},
                "Positive units ON (set by answers), Negative units OFF (set by by_ion_mode.Negative)"),
            "workflow_overrides win": (
                {"answers": {**on, "workflow_overrides": off}, "by_ion_mode": {
                    "Positive": on, "Negative": {"workflow_overrides": on}}},
                "Positive units OFF (set by answers.workflow_overrides), "
                "Negative units ON (set by by_ion_mode.Negative.workflow_overrides)"),
        }
        for name, (profile, words) in cases.items():
            with self.subTest(name):
                record = plan.automatic_rt_correction_record(unpinned, profile, ["automatic_rt_correction"])
                self.assertEqual(record["correction"], "by_ion_mode")
                lines = record["statement"]
                self.assertTrue(lines[0].startswith("  !! AUTOMATIC RT CORRECTION DIFFERS FROM THE DECISION"), lines)
                self.assertEqual(lines[1], "  !! AUTOMATIC RT CORRECTION DIFFERS BY ION MODE (not pinned by the "
                                           "campaign policy): " + words)
                self.assertIn("Each unit runs as the profile says for its ion mode: " + words, lines[2])
                self.assertNotIn("Each unit runs as the profile says: ON", "\n".join(lines))
                on_mode = next(mode for mode, item in record["correction_by_ion_mode"].items() if item["correction"])
                self.assertTrue(lines[3].startswith(f"    for the {on_mode} units: maximum anchors"), lines)
        # The same setting in both modes is one state, said once, with where each mode's came from.
        both = plan.automatic_rt_correction_record(unpinned, {"answers": on, "by_ion_mode": {"Positive": on}}, [])
        self.assertEqual(both["correction"], True)
        self.assertNotIn("BY ION MODE", "\n".join(both["statement"]))
        self.assertIn("Each unit runs as the profile says: ON, Positive units ON (set by by_ion_mode.Positive), "
                      "Negative units ON (set by answers)", "\n".join(both["statement"]))

    def test_the_ion_mode_statement_follows_how_the_runner_merges_the_profile(self) -> None:
        from campaign import machine
        on, off = {"execute_automatic_rt_correction": True}, {"execute_automatic_rt_correction": "false"}
        profiles = [
            {"by_ion_mode": {"Positive": on}},
            {"answers": on, "by_ion_mode": {"Negative": off}},
            {"answers": {**off, "workflow_overrides": on}, "by_ion_mode": {"Positive": {"workflow_overrides": off}}},
            {"answers": {"workflow_overrides": {"x": 1}}, "by_ion_mode": {"Negative": {**on, "workflow_overrides": {}}}},
            {"answers": {}, "by_ion_mode": {}},
            # Interactive applies workflow_overrides unconverted and reads them by truthiness: these run ON.
            {"answers": {"workflow_overrides": {"execute_automatic_rt_correction": "false"}}, "by_ion_mode": {}},
            {"answers": off, "by_ion_mode": {"Negative": {"workflow_overrides": {"execute_automatic_rt_correction": "off"}},
                                             "Positive": {"workflow_overrides": {"execute_automatic_rt_correction": 2}}}},
            {"answers": on, "by_ion_mode": {"Positive": {"workflow_overrides": {"execute_automatic_rt_correction": ""}},
                                            "Negative": {"workflow_overrides": {"execute_automatic_rt_correction": 0}}}},
        ]
        for profile in profiles:
            stated = plan.automatic_rt_correction_by_ion_mode(profile)
            for mode in plan.ION_MODES:
                with self.subTest(profile=profile, mode=mode):
                    # As Runner.answers merges the profile for a unit. Interactive then converts the answer with
                    # agent_workflow._as_bool, applies workflow_overrides unconverted (state.update(overrides)), and
                    # runs the correction where bool(state.get("execute_automatic_rt_correction", False)).
                    merged = machine._merge(dict(profile.get("answers") or {}), dict(profile["by_ion_mode"].get(mode) or {}))
                    state = {"execute_automatic_rt_correction": plan._true(merged.get("execute_automatic_rt_correction", False))}
                    state.update(dict(merged.get("workflow_overrides") or {}))
                    runs = bool(state.get("execute_automatic_rt_correction", False))
                    self.assertEqual(stated[mode]["correction"], runs)

    def test_a_workflow_overrides_string_is_stated_as_interactive_runs_it_and_refused(self) -> None:
        # Interactive applies workflow_overrides unconverted, after it converts the answers, and runs the correction
        # where bool(state["execute_automatic_rt_correction"]): the string "false" there runs it ON.
        unpinned = policy.CampaignPolicy.from_dict({"automatic_rt_correction": False}).as_dict()
        written = {"schema": plan.PROFILE_SCHEMA, "by_ion_mode": {}, "answers": {
            "library_strategy": "existing", "workflow_overrides": {"execute_automatic_rt_correction": "false"}}}
        self.assertTrue(plan.automatic_rt_correction_requested(written))
        record = plan.automatic_rt_correction_record(unpinned, written, ["automatic_rt_correction"])
        self.assertEqual(record["correction"], True)
        text = "\n".join(record["statement"])
        self.assertIn("Each unit runs as the profile says: ON (set by answers.workflow_overrides: 'false', which "
                      "Interactive applies unconverted and reads as true)", text)
        self.assertNotIn(": OFF", " | ".join(line for line in record["statement"] if "anchor-library" not in line))

        def manifest(profile: dict, generation: str) -> dict:
            return self.manifest(profile, {**APPROVABLE_PINS["console"], "automatic_rt_correction": generation}, unpinned)

        # A Console of 826: the words are refused, since they say the opposite of the run.
        problems = plan.approval_problems(manifest(written, policy.AUTOMATIC_RT_LOCAL_SUPPORT), ["1", "3", "4"])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("execute_automatic_rt_correction to 'false', which Interactive applies unconverted and reads "
                      "as true (answers.workflow_overrides): write true or false there", problems[0])
        # A Console of 810 alone: the run asks for the correction, so the Console check is made too.
        problems = plan.approval_problems(manifest(written, policy.AUTOMATIC_RT_RUN_WIDE), ["1", "3", "4"])
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("the profile turns automatic RT correction on, and the pinned Console implements "
                      "MsdialWorkbench #810's run-wide outlier test only", "; ".join(problems))
        # The anchor-library switch is read the same way.
        anchor = json.loads(json.dumps(written))
        anchor["answers"]["workflow_overrides"] = {"execute_rt_correction": "no"}
        problems = plan.approval_problems(manifest(anchor, policy.AUTOMATIC_RT_NONE), ["1", "3", "4"])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("execute_rt_correction to 'no', which Interactive applies unconverted and reads as true", problems[0])
        # JSON false there is false to Interactive too: stated OFF, not refused, any Console.
        plain = json.loads(json.dumps(written))
        plain["answers"]["workflow_overrides"] = {"execute_automatic_rt_correction": False}
        self.assertFalse(plan.automatic_rt_correction_requested(plain))
        record = plan.automatic_rt_correction_record(unpinned, plain, ["automatic_rt_correction"])
        self.assertIn("Each unit runs as the profile says: OFF (set by answers.workflow_overrides)", "\n".join(record["statement"]))
        self.assertEqual(plan.approval_problems(manifest(plain, policy.AUTOMATIC_RT_NONE), ["1", "3", "4"]), [])

    def test_under_the_pin_a_workflow_overrides_switch_is_refused_since_it_would_beat_the_fallback(self) -> None:
        # Runner.answers writes the pin and the uncorrected fallback as an answer; Interactive applies
        # workflow_overrides over it, so even true there would keep a unit whose anchors failed corrected.
        for name, overrides, words in (
            ("true", {"execute_automatic_rt_correction": True}, "in a workflow_overrides (answers.workflow_overrides) to true;"),
            ("the string false", {"execute_automatic_rt_correction": "false"},
             "(answers.workflow_overrides) to 'false', which Interactive applies unconverted and reads as true;"),
            ("the anchor-library switch as a string", {"execute_rt_correction": "false"},
             "turns the anchor-library RT correction on (answers.workflow_overrides: 'false', which Interactive"),
        ):
            with self.subTest(name):
                profile = {"schema": plan.PROFILE_SCHEMA, "by_ion_mode": {}, "answers": {
                    "library_strategy": "existing", "workflow_overrides": overrides}}
                problems = plan.approval_problems(self.pinned(profile), ["1", "3", "4"])
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(words, problems[0])
        self.assertIn("uncorrected fallback", plan.approval_problems(self.pinned(
            {"schema": plan.PROFILE_SCHEMA, "by_ion_mode": {}, "answers": {
                "library_strategy": "existing", "workflow_overrides": {"execute_automatic_rt_correction": True}}}),
            ["1", "3", "4"])[0])

    def test_a_statement_that_does_not_match_the_policy_is_not_approvable(self) -> None:
        plain = {"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "existing"}, "by_ion_mode": {}}
        manifest = self.pinned(plain)
        manifest["policy_overrides"] = []
        manifest["automatic_rt_correction"] = plan.automatic_rt_correction_record(manifest["policy"], plain, [])
        self.assertEqual(plan.approval_problems(manifest, ["1", "3", "4"]), [])
        manifest["policy"]["automatic_rt_correction_maximum_anchors"] = 6
        problems = plan.approval_problems(manifest, ["1", "3", "4"])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("automatic RT correction statement does not match its policy and profile", problems[0])

    def test_a_manifest_planned_before_the_pin_is_not_approvable(self) -> None:
        """A policy recorded before 2026-10-07 does not pin the correction, but a manifest planned with it carries no
        statement, and approve refuses it (R2-4, 2026-10-08): plan again."""
        legacy = {key: value for key, value in policy.CampaignPolicy().as_dict().items() if not key.startswith("automatic_rt")}
        off = json.loads(json.dumps(self.RT_PROFILE))
        off["answers"]["execute_automatic_rt_correction"] = False
        off["answers"]["automatic_rt_correction_maximum_anchors"] = 6
        manifest = self.planned_before_the_statement(self.manifest(
            off, {**APPROVABLE_PINS["console"], "automatic_rt_correction": policy.AUTOMATIC_RT_NONE}, legacy))
        self.assertFalse(policy.automatic_rt_correction_pinned(legacy))
        problems = plan.approval_problems(manifest, ["1", "3", "4"])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("carries no automatic RT correction statement", problems[0])
        self.assertIn("plan again (campaign-runner.py plan)", problems[0])
        self.assertTrue(policy.automatic_rt_correction_pinned(policy.CampaignPolicy().as_dict()))
        self.assertFalse(policy.automatic_rt_correction_pinned(
            policy.CampaignPolicy.from_dict({"automatic_rt_correction": False}).as_dict()))

    def test_a_manifest_planned_before_the_statement_is_refused_where_it_could_run_the_correction(self) -> None:
        """The review of #36: approve accepted a manifest whose digest never covered an RT statement, one planned
        with the correction turned off included. Now it is refused, and the person is told to plan again; the problem
        names where the policy names the fields decided on 2026-10-07 or the profile turns the correction on (a
        manifest with none of those is refused too, R2-4)."""
        plain = {"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "existing"}, "by_ion_mode": {}}
        console = {**APPROVABLE_PINS["console"], "automatic_rt_correction": policy.AUTOMATIC_RT_LOCAL_SUPPORT}
        legacy = {key: value for key, value in policy.CampaignPolicy().as_dict().items() if not key.startswith("automatic_rt")}
        unpinned = policy.CampaignPolicy.from_dict({"automatic_rt_correction": False}).as_dict()
        cases = {
            "the policy pins it on": (policy.CampaignPolicy().as_dict(), plain, "its campaign policy pins it on"),
            "the policy turns it off": (unpinned, plain, "its campaign policy turns it off"),
            "a policy from before 2026-10-07, a profile turning it on": (
                legacy, self.RT_PROFILE, "and its profile turns it on"),
            "a profile turning it on in an ion mode's override": (
                legacy, {**plain, "by_ion_mode": {"Negative": {"workflow_overrides": {"execute_automatic_rt_correction": True}}}},
                "and its profile turns it on"),
            # The review of #36 (round 2): Interactive runs the anchor-library correction for every unit, and the
            # digest covers no words saying so.
            "a policy from before 2026-10-07, a profile turning the anchor-library correction on": (
                legacy, {**plain, "answers": {**plain["answers"], "execute_rt_correction": True,
                                              "rt_correction_anchor_path": "library:P.msp"}},
                "and its profile turns the anchor-library RT correction on"),
            "the anchor-library correction for an ion mode": (
                legacy, {**plain, "by_ion_mode": {"Positive": {"execute_rt_correction": "true"}}},
                "and its profile turns the anchor-library RT correction on"),
            # Only one of the fields: still a policy that records a decision about the correction.
            "the anchors alone": ({**legacy, "automatic_rt_correction_maximum_anchors": 12}, plain,
                                  "its campaign policy records it (automatic_rt_correction_maximum_anchors)"),
        }
        for name, (recorded, profile, words) in cases.items():
            with self.subTest(name):
                planned = self.manifest(profile, console, recorded)
                self.assertEqual(plan.approval_problems(planned, ["1", "3", "4"]), [], "approvable as the plan writes it now")
                problems = plan.approval_problems(self.planned_before_the_statement(planned), ["1", "3", "4"])
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("carries no automatic RT correction statement", problems[0])
                self.assertIn(words, problems[0])
                self.assertTrue(problems[0].endswith("plan again (campaign-runner.py plan), read the automatic RT "
                                                     "correction in the new summary, and approve the new digest"))
        # A statement written as null is no statement.
        nulled = self.manifest(plain, console, policy.CampaignPolicy().as_dict())
        nulled["automatic_rt_correction"] = None
        self.assertIn("carries no automatic RT correction statement", "; ".join(plan.approval_problems(nulled, ["1", "3", "4"])))

    def test_a_manifest_planned_before_the_statement_that_runs_uncorrected_is_refused_too(self) -> None:
        """R2-4 (the user's answer of 2026-10-08): the default is correction ON, so there is no legacy exception. A
        manifest with no automatic RT correction statement is refused even where its policy (from before 2026-10-07)
        names none of the fields and its profile turns neither correction on, and the person is told to plan again;
        the same policy and profile planned now state the correction, and the default policy states it ON."""
        legacy = {key: value for key, value in policy.CampaignPolicy().as_dict().items() if not key.startswith("automatic_rt")}
        console = {**APPROVABLE_PINS["console"], "automatic_rt_correction": policy.AUTOMATIC_RT_NONE}
        for name, profile in {
            "no RT setting": {"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "existing"}, "by_ion_mode": {}},
            "turned off": {"schema": plan.PROFILE_SCHEMA, "by_ion_mode": {}, "answers": {
                "library_strategy": "existing", "execute_automatic_rt_correction": "false"}},
            # Settings without the switch, and the anchor-library correction turned off: still no statement.
            "settings without the switch": {"schema": plan.PROFILE_SCHEMA, "by_ion_mode": {}, "answers": {
                "library_strategy": "existing", "execute_rt_correction": False,
                "automatic_rt_correction_outlier_mad_threshold": 1000, "automatic_rt_correction_maximum_anchors": 6}},
        }.items():
            with self.subTest(name):
                manifest = self.planned_before_the_statement(self.manifest(profile, console, legacy))
                missing = plan.automatic_rt_statement_missing_problems(manifest)
                self.assertEqual(len(missing), 1, missing)
                self.assertIn("carries no automatic RT correction statement", missing[0])
                self.assertIn("its units would run uncorrected, against the campaign's default of correction ON", missing[0])
                self.assertTrue(missing[0].endswith("plan again (campaign-runner.py plan), read the automatic RT "
                                                    "correction in the new summary, and approve the new digest"))
                self.assertEqual(plan.approval_problems(manifest, ["1", "3", "4"]), missing)
        # Any manifest with no statement is refused, whatever else it carries: no policy, no profile, nothing.
        self.assertEqual(len(plan.automatic_rt_statement_missing_problems({})), 1)
        # Planned now with the default policy, the manifest states the correction ON.
        plain = {"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "existing"}, "by_ion_mode": {}}
        local = {**APPROVABLE_PINS["console"], "automatic_rt_correction": policy.AUTOMATIC_RT_LOCAL_SUPPORT}
        planned = self.manifest(plain, local, policy.CampaignPolicy().as_dict())
        self.assertIs(planned["automatic_rt_correction"]["correction"], True)
        self.assertTrue(planned["automatic_rt_correction"]["pinned"])
        self.assertEqual(plan.approval_problems(planned, ["1", "3", "4"]), [])

    def test_a_workflow_overrides_or_ion_mode_that_is_not_an_object_is_refused(self) -> None:
        """The review of #36 (round 3): the gate reads a workflow_overrides only as an object, but Runner.answers
        takes dict(answers.get("workflow_overrides") or {}), so a list of pairs ran settings no statement named and
        no refusal saw, and an ion mode's null replaced the answers' workflow_overrides for that mode's units."""
        from campaign import machine
        plain = {"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "existing"}, "by_ion_mode": {}}
        console = {**APPROVABLE_PINS["console"], "automatic_rt_correction": policy.AUTOMATIC_RT_LOCAL_SUPPORT}
        legacy = {key: value for key, value in policy.CampaignPolicy().as_dict().items() if not key.startswith("automatic_rt")}
        unpinned = policy.CampaignPolicy.from_dict({"automatic_rt_correction": False}).as_dict()
        pairs = [["automatic_rt_correction_outlier_mad_threshold", 1000], ["execute_automatic_rt_correction", True]]
        cases = {
            # (1) Pinned: the outlier threshold and the switch, applied over the pinned answer and its fallback.
            "pairs under the pin": (
                policy.CampaignPolicy().as_dict(), {**plain, "answers": {**plain["answers"], "workflow_overrides": pairs}},
                False, "Positive", {"automatic_rt_correction_outlier_mad_threshold": 1000,
                                    "execute_automatic_rt_correction": True},
                "answers.workflow_overrides is [["),
            # (2) Before the statement: the anchor-library correction in every unit, with no words about it.
            "pairs before the statement": (
                legacy, {**plain, "answers": {**plain["answers"], "rt_correction_anchor_path": "library:P.msp",
                                              "workflow_overrides": [["execute_rt_correction", True]]}},
                True, "Negative", {"execute_rt_correction": True}, "answers.workflow_overrides is [["),
            # (3) Unpinned and stated OFF: the Positive null wipes the answers' OFF override, so the answer's ON runs.
            "an ion mode's null": (
                unpinned, {**plain, "answers": {**plain["answers"], "execute_automatic_rt_correction": True,
                                                "workflow_overrides": {"execute_automatic_rt_correction": False}},
                           "by_ion_mode": {"Positive": {"workflow_overrides": None}}},
                False, "Positive", {}, "by_ion_mode.Positive.workflow_overrides is null"),
        }
        for name, (recorded, profile, before, mode, runner_overrides, words) in cases.items():
            with self.subTest(name):
                # What Runner.answers would hand Interactive for a unit of that mode: not what the gate reads.
                merged = machine._merge(dict(profile["answers"]), dict(profile["by_ion_mode"].get(mode) or {}))
                self.assertEqual(dict(merged.get("workflow_overrides") or {}), runner_overrides)
                manifest = self.manifest(profile, console, recorded)
                if before:
                    manifest = self.planned_before_the_statement(manifest)
                problems = plan.approval_problems(manifest, ["1", "3", "4"])
                self.assertTrue(any(words in problem and problem.endswith("write an object, or leave it out")
                                    for problem in problems), problems)
        # The third case's statement said OFF for Positive, where the runner would have run the correction.
        self.assertFalse(plan.automatic_rt_correction_by_ion_mode(cases["an ion mode's null"][1])["Positive"]["correction"])
        # An ion mode that is not an object is refused too: the runner would dict() a list of pairs.
        for value, words in (([["execute_automatic_rt_correction", True]], 'by_ion_mode.Negative is [["'),
                             (None, "by_ion_mode.Negative is null")):
            with self.subTest(mode_value=value):
                problems = plan.profile_problems({**plain, "by_ion_mode": {"Negative": value}}, ["P.msp"])
                self.assertTrue(any(words in problem and problem.endswith("write an object") for problem in problems),
                                problems)
        # An object, empty or not, and no workflow_overrides at all, are refused for nothing here.
        for profile in (plain, {**plain, "answers": {**plain["answers"], "workflow_overrides": {}}},
                        {**plain, "by_ion_mode": {"Positive": {"workflow_overrides": {"x": 1}}, "Negative": {}}}):
            with self.subTest(profile=profile):
                self.assertEqual(plan.profile_shape_problems(profile), [])
                self.assertEqual(plan.profile_problems(profile, ["P.msp"]), [])

    def test_under_the_pin_every_other_automatic_setting_is_refused(self) -> None:
        """The review of #36 (round 2): a profile's outlier threshold, reference file, coverage or tolerance runs in
        every unit, since the runner pins none of them, and the pinned statement named none of them."""
        plain = {"schema": plan.PROFILE_SCHEMA, "answers": {"library_strategy": "existing"}, "by_ion_mode": {}}
        cases = {
            "the outlier threshold": ({"answers": {"automatic_rt_correction_outlier_mad_threshold": 1000}},
                                      "automatic_rt_correction_outlier_mad_threshold 1000 (answers)"),
            "the reference file": ({"answers": {"automatic_rt_correction_reference_file_id": 0}},
                                   "automatic_rt_correction_reference_file_id 0 (answers)"),
            "the sample coverage": ({"answers": {"automatic_rt_correction_minimum_sample_coverage": 0.05}},
                                    "automatic_rt_correction_minimum_sample_coverage 0.05 (answers)"),
            "a workflow_overrides tolerance": (
                {"answers": {"workflow_overrides": {"automatic_rt_correction_match_rt_tolerance": 5.0}}},
                "automatic_rt_correction_match_rt_tolerance 5.0 (answers.workflow_overrides)"),
            "an ion mode's least anchors": ({"by_ion_mode": {"Negative": {"automatic_rt_correction_minimum_anchors": 4}}},
                                            "automatic_rt_correction_minimum_anchors 4 (by_ion_mode.Negative)"),
            # Even Interactive's own default: the decision is Interactive's defaults, which the profile leaves alone.
            "Interactive's default, written out": ({"answers": {"automatic_rt_correction_outlier_mad_threshold": 3.5}},
                                                   "automatic_rt_correction_outlier_mad_threshold 3.5 (answers)"),
        }
        for name, (parts, words) in cases.items():
            with self.subTest(name):
                profile = json.loads(json.dumps(plain))
                for key, value in parts.items():
                    profile[key] = {**profile[key], **value}
                manifest = self.pinned(profile)
                problems = plan.approval_problems(manifest, ["1", "3", "4"])
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(words, problems[0])
                self.assertIn("every other automatic RT correction setting is Interactive's default", problems[0])
                self.assertTrue(problems[0].endswith("leave it out of the profile"))
                # And the words say what the profile gives, so nothing is approved unread even where it is refused.
                self.assertIn("    other settings as the profile states them: " + words, manifest["automatic_rt_correction"]["statement"])
                self.assertEqual(len(manifest["automatic_rt_correction"]["other_settings"]), 1)
        stated = self.pinned(plain)["automatic_rt_correction"]
        self.assertEqual(stated["other_settings"], [])
        self.assertIn("    other settings Interactive's defaults", stated["statement"])

    def test_an_unpinned_correction_states_the_other_settings_the_profile_gives(self) -> None:
        unpinned = policy.CampaignPolicy.from_dict({"automatic_rt_correction": False}).as_dict()
        on = {"execute_automatic_rt_correction": True}
        tuned = {"schema": plan.PROFILE_SCHEMA, "by_ion_mode": {}, "answers": {
            "library_strategy": "existing", **on, "automatic_rt_correction_outlier_mad_threshold": 1000,
            "workflow_overrides": {"automatic_rt_correction_reference_file_id": 0}}}
        record = plan.automatic_rt_correction_record(unpinned, tuned, ["automatic_rt_correction"])
        self.assertIn("    other settings as the profile states them: automatic_rt_correction_outlier_mad_threshold 1000 "
                      "(answers), automatic_rt_correction_reference_file_id 0 (answers.workflow_overrides)", record["statement"])
        self.assertEqual([item["key"] for item in record["other_settings"]],
                         ["automatic_rt_correction_outlier_mad_threshold", "automatic_rt_correction_reference_file_id"])
        # Stated, not refused: unpinned, each unit runs as its profile says, and the words say what that is.
        manifest = self.manifest(tuned, {**APPROVABLE_PINS["console"], "automatic_rt_correction": policy.AUTOMATIC_RT_LOCAL_SUPPORT},
                                 unpinned)
        self.assertEqual(plan.approval_problems(manifest, ["1", "3", "4"]), [])
        # For one ion mode, the line says which units it applies to.
        split = plan.automatic_rt_correction_record(unpinned, {"by_ion_mode": {"Positive": {
            **on, "automatic_rt_correction_rt_bin_width": 0.2}}}, ["automatic_rt_correction"])
        self.assertIn("    for the Positive units: other settings as the profile states them: "
                      "automatic_rt_correction_rt_bin_width 0.2 (by_ion_mode.Positive)", split["statement"])
        plain = plan.automatic_rt_correction_record(unpinned, {"answers": on}, ["automatic_rt_correction"])
        self.assertIn("    other settings Interactive's defaults", plain["statement"])
        # Off, the settings change nothing that runs, and the statement does not dwell on them.
        off = plan.automatic_rt_correction_record(unpinned, {"answers": {"automatic_rt_correction_rt_bin_width": 0.2}},
                                                  ["automatic_rt_correction"])
        self.assertNotIn("other settings", "\n".join(off["statement"]))

    def test_the_statement_says_where_the_anchor_library_correction_runs(self) -> None:
        """The review of #36 (round 2): an unpinned plan whose profile turns the anchor-library correction on said
        "OFF" and nothing more, while every unit ran that correction."""
        unpinned = policy.CampaignPolicy.from_dict({"automatic_rt_correction": False}).as_dict()
        console = {**APPROVABLE_PINS["console"], "automatic_rt_correction": policy.AUTOMATIC_RT_NONE}
        library = {"schema": plan.PROFILE_SCHEMA, "by_ion_mode": {}, "answers": {
            "library_strategy": "existing", "execute_rt_correction": True, "rt_correction_anchor_path": "library:P.msp"}}
        record = plan.automatic_rt_correction_record(unpinned, library, ["automatic_rt_correction"])
        lines = record["statement"]
        self.assertEqual(lines[-2], "  !! THE ANCHOR-LIBRARY RT CORRECTION RUNS for the Positive and Negative units "
                                    "(the decision of 2026-10-07 is the automatic correction alone)")
        self.assertEqual(lines[-1], "  anchor-library RT correction (execute_rt_correction): no policy field decides it. "
                                    "Each unit runs as the profile says: ON (set by answers)")
        self.assertEqual({mode: item["correction"] for mode, item in record["anchor_library_rt_correction_by_ion_mode"].items()},
                         {"Positive": True, "Negative": True})
        # Stated, so approvable unpinned; the digest covers the words.
        self.assertEqual(plan.approval_problems(self.manifest(library, console, unpinned), ["1", "3", "4"]), [])
        # For one ion mode.
        negative = plan.automatic_rt_correction_record(
            unpinned, {"by_ion_mode": {"Negative": {"execute_rt_correction": True}}}, ["automatic_rt_correction"])
        self.assertIn("  !! THE ANCHOR-LIBRARY RT CORRECTION RUNS for the Negative units", negative["statement"][-2])
        self.assertIn("Each unit runs as the profile says for its ion mode: Positive units OFF (not set; Interactive's "
                      "default is off), Negative units ON (set by by_ion_mode.Negative)", negative["statement"][-1])
        # Off everywhere, it says so, with no warning, pinned or not.
        for recorded in (unpinned, policy.CampaignPolicy().as_dict()):
            with self.subTest(pinned=recorded["automatic_rt_correction"]):
                quiet = plan.automatic_rt_correction_record(recorded, {"answers": {"execute_rt_correction": False}}, [])
                self.assertEqual(quiet["statement"][-1], "  anchor-library RT correction (execute_rt_correction): no "
                                 "policy field decides it. Each unit runs as the profile says: OFF (set by answers)")
                self.assertNotIn("ANCHOR-LIBRARY RT CORRECTION RUNS", "\n".join(quiet["statement"]))
        # A statement written before these words (one planned on an earlier commit of #36) no longer matches.
        manifest = self.manifest(library, console, unpinned)
        manifest["automatic_rt_correction"]["statement"] = manifest["automatic_rt_correction"]["statement"][:-2]
        self.assertIn("does not match its policy and profile", "; ".join(plan.approval_problems(manifest, ["1", "3", "4"])))

    @unittest.skipUnless(contract.AVAILABLE, "the Interactive checkout is not where this test looks")
    def test_every_automatic_setting_interactive_writes_is_known_by_its_prefix(self) -> None:
        from msdial_app import workflow
        keys = set(workflow.AUTOMATIC_RT_CORRECTION_DEFAULTS) | {workflow.AUTOMATIC_RT_CORRECTION_LOCAL_SUPPORT_RT_WINDOW}
        for key in sorted(keys):
            with self.subTest(key):
                self.assertTrue(key.startswith(plan.AUTOMATIC_RT_SETTING_PREFIX))
                named = key in (plan.AUTOMATIC_RT_ANCHORS_ANSWER, plan.AUTOMATIC_RT_WINDOW_ANSWER, policy.AUTOMATIC_RT_BLANK_ANSWER)
                self.assertEqual(plan.automatic_rt_tuning_key(key), not named)

    @unittest.skipUnless(contract.AVAILABLE, "the Interactive checkout is not where this test looks")
    def test_the_console_pin_records_the_generation_of_its_assembly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            console = Path(directory) / "MSDIALCUI.exe"
            console.write_bytes(b"MZ" + RUN_WIDE_KEY.encode("utf-16-le"))
            reader = ports.PinReader(console_path=str(console), extractor_path="", libraries={})
            self.assertEqual(reader.console()["automatic_rt_correction"], policy.AUTOMATIC_RT_RUN_WIDE)
            # A rebuilt Console is read again, as its hashes are.
            console.write_bytes(b"MZ" + RUN_WIDE_KEY.encode("utf-16-le") + LOCAL_SUPPORT_KEY.encode("utf-16-le") + b"!")
            self.assertEqual(reader.console()["automatic_rt_correction"], policy.AUTOMATIC_RT_LOCAL_SUPPORT)


class InSilicoTierContractTests(unittest.TestCase):
    """2026-10-28, found preparing verification V3: a profile naming an in-silico MSP (libraries.in_silico_msp_paths,
    the fourth tier) was planned with the three-tier contract, since the plan called the pinned Interactive's tiered
    profile without it, while that Interactive (0.5.48) writes the four-tier method; LIB-2 then failed every unit after
    its download and diagnostic, and the plan exited 0 with no approval problem. The libraries are synthetic names."""

    PROFILE = {"schema": plan.PROFILE_SCHEMA,
               "answers": {"library_strategy": "tiered_lipid_msp", "libraries": {"lbm_path": "library:Synthetic.lbm2"}},
               "by_ion_mode": {"Positive": {"libraries": {"msp_paths": ["library:SyntheticPositive.msp"],
                                                          "in_silico_msp_paths": ["library:SyntheticPredictedPos.msp"]}},
                               "Negative": {"libraries": {"msp_paths": ["library:SyntheticNegative.msp"],
                                                          "in_silico_msp_paths": ["library:SyntheticPredictedNeg.msp"]}}}}
    CODE = {"lbm_cutoffs": {"weighted": 0.1, "simple": 0.1, "reverse": 0.1}, "lbm_cutoffs_source": "the template",
            "tiered_lbm_from_answers": True, "problems": []}

    @staticmethod
    def four_tiers(lbm, msp, lbm_annotator=None, in_silico_path=None):
        """A tiered profile shaped as Interactive 0.5.48's (annotation_pipeline.tiered_lcms_annotation)."""
        row = {"msp_file_path": msp, "target_omics": "Metabolomics", "rt_tolerance": 0.5, "use_rt_scoring": False,
               "use_rt_filtering": False}
        if not in_silico_path:
            return {"profile_id": "three", "lbm_annotator": {"lbm_file_path": lbm, "priority": 3},
                    "msp_annotators": [{**row, "annotator_id": "strict", "priority": 2},
                                       {**row, "annotator_id": "broad", "priority": 1}]}
        return {"profile_id": "four", "lbm_annotator": {"lbm_file_path": lbm, "priority": 4},
                "msp_annotators": [{**row, "annotator_id": "strict", "priority": 3, "library_kind": "acquired"},
                                   {**row, "annotator_id": "broad", "priority": 2, "library_kind": "acquired"},
                                   {**row, "msp_file_path": in_silico_path, "annotator_id": "predicted", "priority": 1,
                                    "library_kind": "predicted", "suggestions": False, "rt_fixed": True}]}

    def test_the_in_silico_msp_the_profile_names_makes_the_four_tier_contract(self) -> None:
        record = plan.annotation_record(self.PROFILE, [], [], {**self.CODE, "tiered": self.four_tiers})
        positive = record["by_ion_mode"]["Positive"]
        self.assertEqual((positive["profile_id"], positive["lbm_priority"], positive["in_silico_libraries"]),
                         ("four", 4, ["SyntheticPredictedPos.msp"]))
        self.assertEqual([(row["annotator_id"], row["library"], row["priority"], row["library_kind"], row.get("suggestions"))
                          for row in positive["msp_rows"]],
                         [("strict", "SyntheticPositive.msp", 3, "acquired", None),
                          ("broad", "SyntheticPositive.msp", 2, "acquired", None),
                          ("predicted", "SyntheticPredictedPos.msp", 1, "predicted", False)])
        self.assertNotIn("rt_fixed", positive["msp_rows"][2], "only what LIB-2 compares is stated")
        self.assertEqual(record["by_ion_mode"]["Negative"]["msp_rows"][2]["library"], "SyntheticPredictedNeg.msp")
        text = "\n".join(record["statement"])
        self.assertIn("the profile's LBM Synthetic.lbm2; the profile's in-silico MSP SyntheticPredictedPos.msp; LBM cutoffs", text)
        self.assertIn("predicted (SyntheticPredictedPos.msp; priority 1;", text)
        self.assertIn("target omics Metabolomics; predicted; suggestions False)", text)
        self.assertIn("target omics Metabolomics; acquired)", text, "a row that gives no suggestions states none")
        self.assertEqual(plan.annotation_problems({"annotation": record}), [])
        contract_ = plan.annotation_contract({"annotation": record})
        self.assertEqual(contract_["Positive"]["msp_rows"][2]["suggestions"], False)
        # A profile without an in-silico MSP is planned as before, with the three-tier profile.
        three = json.loads(json.dumps(self.PROFILE))
        for mode in ("Positive", "Negative"):
            del three["by_ion_mode"][mode]["libraries"]["in_silico_msp_paths"]
        record = plan.annotation_record(three, [], [], {**self.CODE, "tiered": self.four_tiers})
        self.assertEqual(record["by_ion_mode"]["Positive"]["profile_id"], "three")
        self.assertNotIn("in_silico_libraries", record["by_ion_mode"]["Positive"])
        self.assertNotIn("in-silico", "\n".join(record["statement"]))

    def test_an_interactive_whose_tiered_profile_takes_no_in_silico_msp_is_refused(self) -> None:
        """An Interactive before 0.5.48 has no in_silico_path: it would run three tiers while the answers and pins name
        the fourth. The plan states no contract and refuses it, rather than approving the three-tier one; so it does
        where whether the profile takes one cannot be read."""
        def three_tiers(lbm, msp, lbm_annotator=None):
            return self.four_tiers(lbm, msp)

        for code, said in (({**self.CODE, "tiered": three_tiers}, "WHICH THE PINNED INTERACTIVE DOES NOT SEARCH: its "
                            "tiered profile (annotation_pipeline.tiered_lcms_annotation) takes no in-silico MSP (no "
                            "in_silico_path), so it would run three tiers"),
                           ({**self.CODE, "tiered": self.four_tiers}, "which the pinned Interactive may not search: "
                            "whether its tiered profile takes an in-silico MSP could not be read")):
            with self.subTest(said=said[:20]), mock.patch.object(
                    plan, "tiered_takes_in_silico", return_value=None if code["tiered"] is self.four_tiers else False):
                record = plan.annotation_record(self.PROFILE, [], [], code)
                positive = record["by_ion_mode"]["Positive"]
                self.assertEqual((positive["profile_id"], positive["msp_rows"], positive["lbm_priority"]), (None, None, None))
                self.assertIn(said, "\n".join(record["statement"]))
                problems = plan.annotation_problems({"annotation": record})
                self.assertEqual(len(problems), 2, problems)
                self.assertIn("the profile names the in-silico MSP SyntheticPredictedNeg.msp for Negative units, and the "
                              "pinned Interactive cannot be held to searching it: ", problems[0])
                self.assertIn("pin an Interactive whose tiered profile searches an in-silico MSP (0.5.48 or later), or "
                              "leave libraries.in_silico_msp_paths out, and plan again", problems[1])
                self.assertFalse(any("could not read its annotation profile" in problem for problem in problems),
                                 "the refusal says why, once")
        self.assertIs(plan.tiered_takes_in_silico(three_tiers), False)
        self.assertIs(plan.tiered_takes_in_silico(self.four_tiers), True)
        self.assertIsNone(plan.tiered_takes_in_silico(max), "a builtin whose signature cannot be read")

    def test_an_in_silico_msp_for_another_strategy_or_two_for_one_ion_mode_are_refused(self) -> None:
        """Interactive's own plan refuses both: only tiered_lipid_msp searches an in-silico MSP, and it searches one."""
        other = json.loads(json.dumps(self.PROFILE))
        other["answers"]["library_strategy"] = "existing"
        problems = plan.annotation_problems({"annotation": plan.annotation_record(
            other, [], [], {**self.CODE, "tiered": self.four_tiers})})
        self.assertIn("the profile names an in-silico MSP (SyntheticPredictedPos.msp) for Positive units, which only the "
                      "tiered_lipid_msp strategy searches, and their strategy is existing", "\n".join(problems))
        two = json.loads(json.dumps(self.PROFILE))
        two["by_ion_mode"]["Positive"]["libraries"]["in_silico_msp_paths"].append("library:SyntheticPredictedOther.msp")
        problems = plan.annotation_problems({"annotation": plan.annotation_record(
            two, [], [], {**self.CODE, "tiered": self.four_tiers})})
        self.assertEqual(problems, ["the profile names 2 in-silico MSPs (SyntheticPredictedOther.msp, "
                                    "SyntheticPredictedPos.msp) for Positive units, and the tiered_lipid_msp profile "
                                    "searches one: name one per ion mode under by_ion_mode, and plan again"])

    def test_an_msp_named_bare_or_for_the_other_ion_mode_is_refused(self) -> None:
        """Review 1: an in-silico MSP named other than as library:<file name> was neither pinned nor stated, so the
        plan stated the three-tier contract while Interactive searched it; and Interactive refuses a file named for
        the other ion mode (agent_workflow._name_polarity)."""
        bare = json.loads(json.dumps(self.PROFILE))
        bare["by_ion_mode"]["Positive"]["libraries"]["in_silico_msp_paths"] = ["SyntheticPredictedPos.msp"]
        self.assertIn("the answers of Positive units name libraries.in_silico_msp_paths other than as library:<file "
                      "name>, which the plan can neither pin nor state: name each as library:<file name>",
                      plan.profile_problems(bare, ["Synthetic.lbm2", "SyntheticPositive.msp", "SyntheticNegative.msp",
                                                   "SyntheticPredictedNeg.msp"]))
        swapped = json.loads(json.dumps(self.PROFILE))
        swapped["by_ion_mode"]["Positive"]["libraries"]["in_silico_msp_paths"] = ["library:Synthetic_predicted_neg.msp"]
        problems = plan.annotation_problems({"annotation": plan.annotation_record(
            swapped, [], [], {**self.CODE, "tiered": self.four_tiers})})
        self.assertEqual(problems, ["the in-silico MSP Synthetic_predicted_neg.msp is named for Negative ion mode and the "
                                    "profile gives it to Positive units, which Interactive would refuse for every unit "
                                    "that runs in that mode: give each ion mode its own file under by_ion_mode, and "
                                    "plan again"])
        self.assertEqual(plan.name_polarity("x-POS.msp"), "Positive")
        self.assertEqual(plan.name_polarity("SyntheticPredictedPos.msp"), "", "a token, not a substring")

    def test_what_interactive_refuses_of_an_in_silico_msp_without_reading_it_is_refused(self) -> None:
        """Review 2: the plan approved answers the pinned Interactive's _in_silico_library_problems refuses at its
        prepare, after each unit's download and diagnostic: a name given twice, a file that is no .msp, and the
        negative-mode preprocessing step for a positive-mode unit, an unknown step, or a step without the file."""
        def positive_libraries(profile):
            return profile["by_ion_mode"]["Positive"]["libraries"]

        def negative_libraries(profile):
            return profile["by_ion_mode"]["Negative"]["libraries"]

        def step_alone(profile):
            negative_libraries(profile).pop("in_silico_msp_paths")
            negative_libraries(profile)["in_silico_msp_preprocessing"] = ["precursor_plus_1008_peak_removed"]

        cases = {
            "twice": (lambda profile: positive_libraries(profile)["in_silico_msp_paths"].append(
                "library:SyntheticPredictedPos.msp"),
                "the profile names 2 in-silico MSPs (SyntheticPredictedPos.msp) for Positive units"),
            "suffix": (lambda profile: positive_libraries(profile).__setitem__(
                "in_silico_msp_paths", ["library:SyntheticPredictedPos.msp2"]),
                "the in-silico MSP SyntheticPredictedPos.msp2 for Positive units is no .msp file"),
            "positive step": (lambda profile: profile["answers"]["libraries"].__setitem__(
                "in_silico_msp_preprocessing", ["precursor_plus_1008_peak_removed"]),
                "the answers of Positive units name the in-silico preprocessing precursor_plus_1008_peak_removed"),
            "unknown step": (lambda profile: negative_libraries(profile).__setitem__(
                "in_silico_msp_preprocessing", ["synthetic_step"]),
                "the answers of Negative units name in-silico preprocessing synthetic_step"),
            "step alone": (step_alone,
                           "the answers of Negative units name libraries.in_silico_msp_preprocessing without an "
                           "in-silico MSP"),
        }
        steps = {"precursor_plus_1008_peak_removed"}
        for name, (change, said) in cases.items():
            with self.subTest(name), mock.patch.object(plan, "in_silico_preprocessing_steps", return_value=steps):
                profile = json.loads(json.dumps(self.PROFILE))
                change(profile)
                problems = plan.annotation_problems({"annotation": plan.annotation_record(
                    profile, [], [], {**self.CODE, "tiered": self.four_tiers})})
                self.assertIn(said, "\n".join(problems))
        # The negative-mode step given for Negative units only is what Interactive takes.
        profile = json.loads(json.dumps(self.PROFILE))
        negative_libraries(profile)["in_silico_msp_preprocessing"] = ["precursor_plus_1008_peak_removed"]
        with mock.patch.object(plan, "in_silico_preprocessing_steps", return_value=steps):
            self.assertEqual(plan.annotation_problems({"annotation": plan.annotation_record(
                profile, [], [], {**self.CODE, "tiered": self.four_tiers})}), [])

    @unittest.skipUnless(contract.AVAILABLE, "the Interactive checkout is not where this test looks")
    def test_the_preprocessing_steps_are_the_pinned_interactives(self) -> None:
        from msdial_app import annotation_pipeline

        self.assertEqual(plan.in_silico_preprocessing_steps(), set(annotation_pipeline.IN_SILICO_PREPROCESSING_STEPS))
        self.assertIn(plan.NEGATIVE_ONLY_PREPROCESSING, plan.in_silico_preprocessing_steps())

    def test_the_pins_state_each_msps_library_kind_and_the_authorization_carries_it(self) -> None:
        names = ["Synthetic.lbm2", "SyntheticNegative.msp", "SyntheticPositive.msp", "SyntheticPredictedNeg.msp",
                 "SyntheticPredictedPos.msp"]
        pins = plan.library_pins([{"name": name, "sha256": "0" * 64, "bytes": 1} for name in names], self.PROFILE)
        self.assertEqual({item["name"]: item.get("library_kind") for item in pins}, {
            "Synthetic.lbm2": None, "SyntheticNegative.msp": "acquired", "SyntheticPositive.msp": "acquired",
            "SyntheticPredictedNeg.msp": "predicted", "SyntheticPredictedPos.msp": "predicted"})
        record = plan.annotation_record(self.PROFILE, pins, [], {**self.CODE, "tiered": self.four_tiers})
        self.assertIn("SyntheticPredictedPos.msp (msp, in-silico (predicted); Positive)", record["statement"][0])
        with tempfile.TemporaryDirectory() as directory:
            manifest = {"campaign_id": "c1", "raw_retention_policy": "keep", "units": [{"unit_id": "u1"}],
                        "pins": {"libraries": pins}, "annotation": record}
            path = Path(directory) / "campaign-manifest.json"
            digest = plan.write_manifest(path, manifest)
            authorization = plan.authorization_record(
                manifest, path, digest, approval_id="A-1", approved_by="Test Person",
                approved_at="2026-10-28T00:00:00+00:00", statement="Approved.", covers=["1"])
        self.assertEqual({item["name"]: item.get("library_kind") for item in authorization["libraries"]},
                         {item["name"]: item.get("library_kind") for item in pins})
        # An MSP named both as acquired and as in-silico has no one kind: not pinned with one, and refused.
        both = json.loads(json.dumps(self.PROFILE))
        both["by_ion_mode"]["Negative"]["libraries"]["in_silico_msp_paths"] = ["library:SyntheticNegative.msp"]
        pins = plan.library_pins([{"name": "SyntheticNegative.msp", "sha256": "0" * 64, "bytes": 1}], both)
        self.assertNotIn("library_kind", pins[0])
        self.assertIn("the profile names SyntheticNegative.msp both in libraries.msp_paths (acquired) and in "
                      "libraries.in_silico_msp_paths (in-silico): name each MSP in one of them",
                      plan.profile_problems(both, names))

    @unittest.skipUnless(contract.AVAILABLE, "the Interactive checkout is not where this test looks")
    def test_the_four_tier_contract_is_what_the_pinned_interactives_workflow_writes(self) -> None:
        """Against the real Interactive: the contract the plan states for a profile naming an in-silico MSP is the
        profile, LBM priority and MSP rows its own _apply_libraries and _apply_retention_time_use leave for the method,
        for each retention-time answer (the in-silico row keeps no RT whatever is answered)."""
        from msdial_app import agent_workflow, annotation_pipeline

        found = plan.interactive_annotation()
        if not plan.tiered_takes_in_silico(found["tiered"]):
            self.skipTest("the Interactive checkout is older than its four-tier profile (0.5.48)")
        official = {"id": "lipidomics", "kind": "lbm", "local_path": "Synthetic-official.lbm2", "record_id": "r",
                    "record_url": "", "doi": "", "license": "", "downloaded": True}
        for extra in ({}, {"use_retention_time_for_annotation": False}, {"use_retention_time_for_annotation": True},
                      {"use_retention_time_for_annotation": True, "retention_time_tolerance": 0.3}):
            with self.subTest(**extra):
                profile = {"schema": plan.PROFILE_SCHEMA,
                           "answers": {"library_strategy": "tiered_lipid_msp",
                                       "libraries": {"lbm_path": "library:Synthetic.lbm2"}, **extra},
                           "by_ion_mode": {"Positive": {"libraries": {
                               "msp_paths": ["library:SyntheticPositive.msp"],
                               "in_silico_msp_paths": ["library:SyntheticPredictedPos.msp"]}}}}
                item = plan.annotation_record(profile, [], [], found)["by_ion_mode"]["Positive"]
                answers = {"library_strategy": "tiered_lipid_msp", **extra,
                           "libraries": {"lbm_path": "Synthetic.lbm2", "msp_paths": ["SyntheticPositive.msp"],
                                         "in_silico_msp_paths": ["SyntheticPredictedPos.msp"]}}
                state: dict = {"library_provenance": []}
                with mock.patch.object(agent_workflow, "catalog_status", return_value=[dict(official)]):
                    agent_workflow._apply_libraries(state, {}, answers)
                agent_workflow._apply_retention_time_use(state, answers)
                self.assertEqual(item["profile_id"], state["annotation_pipeline_profile"])
                self.assertEqual(item["profile_id"], annotation_pipeline.TIERED_LCMS_IN_SILICO_PROFILE_ID)
                self.assertEqual(item["lbm_priority"], state["lbm_annotator"]["priority"])
                self.assertEqual(item["msp_rows"], [plan.msp_row_contract(row) for row in state["msp_annotators"]])
                predicted = [row for row in item["msp_rows"] if row["library_kind"] == "predicted"]
                self.assertEqual([(row["library"], row["suggestions"], row["use_rt_scoring"]) for row in predicted],
                                 [("SyntheticPredictedPos.msp", False, False)])
                self.assertEqual(plan.annotation_problems({"annotation": {"by_ion_mode": {"Positive": item}}}), [])


if __name__ == "__main__":
    unittest.main()
