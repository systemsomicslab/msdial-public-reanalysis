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
from datetime import datetime, timezone
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
    ("uE", "MTBLS5", "LC-MS", "DDA", "Positive", "Enabled", "Agilent 6560 Ion Mobility Q-TOF", None, [("E.d", "raw", GB, "https://x/E.d.zip")]),
    ("uF", "MTBLS6", "LC-MS", "DDA", "Positive", "Unknown", "Bruker timsTOF Pro", None, [("F.d", "raw", GB, "https://x/F.zip")]),
    ("uG", "MTBLSPRE", "LC-MS", "DDA", "Positive", "Unknown", "Agilent", None, [("G.mzML", "raw", GB, "https://x/G.mzML")]),
    ("uH", "MTBLS8", "LC-MS", "DDA", "Negative", "Unknown", "Agilent", None, [("H.mzData", "raw", GB, "https://x/H.mzData")]),
    ("uI", "MTBLS9", "LC-MS", "DDA", "Positive", "Unknown", "Agilent", 0, [("I.mzML", "raw", GB, "https://x/I.mzML")]),
    ("uJ", "MTBLS10", "GC-MS", "DDA", "Positive", "Unknown", "Agilent", None, [("J.mzML", "raw", GB, "https://x/J.mzML")]),
    ("uK", "MTBLS11", "LC-MS", "Unknown", "Positive", "Unknown", "Agilent", None, [("K.mzXML", "raw", GB, "https://x/K.mzXML")]),
    ("uL", "MTBLS12", "LC-MS", "DDA", "Both", "Unknown", "Agilent", None, [("L.mzML", "raw", GB, "https://x/L.mzML")]),
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

    def rt_manifest(self, overrides: dict | None = None) -> dict:
        """A declared-pool manifest planned at a fixed moment, with the campaign policy a --policy file would give."""
        catalog = ports.read_only_catalog(self.database)
        try:
            return plan.build_manifest(
                catalog, pool="declared", campaign_id="test-rt", analysis_purpose="annotation",
                workspace_root=self.workspace_root, raw_retention_policy="keep",
                pins={"catalog": {"version": "0.6.1"}, "libraries": []}, profile=None,
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


LOCAL_SUPPORT_KEY = "automatic rt correction local support rt window"  # MsdialWorkbench #826
RUN_WIDE_KEY = "execute automatic rt correction for alignment"  # MsdialWorkbench #810


class AutomaticRtCorrectionPinTests(unittest.TestCase):
    """The campaign runs automatic alignment RT correction with #826's local outlier test (decided 2026-10-07).
    A profile that turns it on is approvable only with a Console pin that implements it."""

    RT_PROFILE = {"schema": plan.PROFILE_SCHEMA,
                  "answers": {"library_strategy": "existing", "execute_automatic_rt_correction": True,
                              "automatic_rt_correction_maximum_anchors": 12},
                  "by_ion_mode": {"Positive": {"libraries": {"msp_paths": ["library:P.msp"]}}}}

    def manifest(self, profile: dict, console: dict) -> dict:
        pins = json.loads(json.dumps(APPROVABLE_PINS))
        pins["console"] = console
        return {"pins": pins, "profile": json.loads(json.dumps(profile)), "units": [{"unit_key": "uA"}],
                "raw_retention_policy": "keep"}

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
        manifest = self.manifest(profile, {**APPROVABLE_PINS["console"], "automatic_rt_correction": generation})
        manifest["policy"] = policy.CampaignPolicy().as_dict()
        return manifest

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

    def test_a_manifest_approved_before_the_pin_keeps_its_profile(self) -> None:
        legacy = {key: value for key, value in policy.CampaignPolicy().as_dict().items() if not key.startswith("automatic_rt")}
        off = json.loads(json.dumps(self.RT_PROFILE))
        off["answers"]["execute_automatic_rt_correction"] = False
        off["answers"]["automatic_rt_correction_maximum_anchors"] = 6
        manifest = self.manifest(off, {**APPROVABLE_PINS["console"], "automatic_rt_correction": policy.AUTOMATIC_RT_NONE})
        manifest["policy"] = legacy
        self.assertFalse(policy.automatic_rt_correction_pinned(legacy))
        self.assertEqual(plan.approval_problems(manifest, ["1", "3", "4"]), [])
        self.assertTrue(policy.automatic_rt_correction_pinned(policy.CampaignPolicy().as_dict()))
        self.assertFalse(policy.automatic_rt_correction_pinned(
            policy.CampaignPolicy.from_dict({"automatic_rt_correction": False}).as_dict()))

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


if __name__ == "__main__":
    unittest.main()
