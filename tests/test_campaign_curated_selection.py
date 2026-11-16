"""Planning and running from frozen triage files (the integration contract's B, 2026-11-16).

The stage3b campaign was stopped on 2026-11-15 after multi-unit accessions ran every unit on the whole study's files.
These tests hold the runner's side of the fix: a plan from triage files (only a validator pass is planned, every unit
carries its curated selection, the digest covers it), --reopen of done units into workspaces of their own, the triage
check at the handoff copy, the curated selection passed to Interactive, the terminal handling of refusals no retry
can change, the cross-unit barrier, the decision ledger and the replay without any language model.

Only public accession and analysis names (the eval fixtures' ST002747 and ST002283) and synthetic names are used.
Nothing is downloaded, no Console runs, and the Catalog is a synthetic database read through the plan's read-only
connection.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes  # noqa: E402  (puts scripts/ on the path)
import test_campaign_contract as contract  # noqa: E402  (puts Interactive and the Catalog on the path)
import triage_eval as te  # noqa: E402
from campaign import curated, plan, policy, ports  # noqa: E402

SCRIPTS = TESTS.parent / "scripts"
_SPEC = importlib.util.spec_from_file_location("campaign_runner_cli", SCRIPTS / "campaign-runner.py")
assert _SPEC and _SPEC.loader
runner_cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner_cli)
_REPLAY = importlib.util.spec_from_file_location("replay_campaign", SCRIPTS / "replay-campaign.py")
assert _REPLAY and _REPLAY.loader
replay_cli = importlib.util.module_from_spec(_REPLAY)
_REPLAY.loader.exec_module(replay_cli)

ACCESSION = "ST002747"
UNITS = ("AN004454", "AN004455", "AN004456", "AN004457")
ARCHIVE_URL = "https://www.metabolomicsworkbench.org/data/ST002747_rawdata.zip"


def write_triage(root: Path, docs: dict | None = None, *, accession: str = ACCESSION, listing: bool = True) -> Path:
    """<root>\\triage\\<accession>\\ as a campaign keeps it: the triage files, and the Catalog rows and archive listing the
    agent read under inputs\\. Returns the triage root."""
    triage_root = root / "triage"
    folder = triage_root / accession
    te.write(docs if docs is not None else te.build_good(accession), folder)
    inputs = folder / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(te.fixture_dir(accession) / "catalog.json", inputs / "catalog.json")
    if listing:
        shutil.copyfile(te.fixture_dir(accession) / "archive-members.tsv", inputs / "archive-members.tsv")
    return triage_root


def build_catalog(path: Path, accession: str = ACCESSION) -> None:
    """A synthetic Catalog with the accession's units and sample rows, as catalog.json lists them, each unit downloading
    the study's one archive."""
    from msdial_repository_catalog.storage import Catalog

    with Catalog(path) as catalog:
        catalog.initialize()
    fixture = json.loads((te.fixture_dir(accession) / "catalog.json").read_text(encoding="utf-8"))
    connection = sqlite3.connect(path)
    with connection:
        study = f"study-{accession}"
        connection.execute("INSERT INTO study(study_id, repository, accession, source_hash) VALUES (?, 'metabolomics_workbench', ?, 'h')",
                           (study, accession))
        for unit in fixture["units"]:
            declared = unit["declared"]
            connection.execute(
                "INSERT INTO analysis_unit(unit_id, study_id, source_subrecord_id, separation, acquisition_mode, ion_mode, "
                "ion_mobility, instrument, signature, target_omics, chromatography) VALUES (?, ?, ?, 'LC-MS', ?, ?, 'Unknown', "
                "'Thermo Q Exactive', ?, 'Metabolomics', ?)",
                (unit["unit_id"], study, unit["unit_id"], declared["acquisition_mode"], declared["ion_mode"], unit["unit_id"],
                 declared["chromatography"]))
            for index, row in enumerate(unit["rows"]):
                connection.execute("INSERT INTO sample(sample_pk, unit_id, sample_id, raw_file) VALUES (?, ?, ?, ?)",
                                   (f"pk-{unit['unit_id']}-{index}", unit["unit_id"], row["sample_id"], row["raw_file"]))
            connection.execute(
                "INSERT INTO raw_file(file_id, unit_id, path, role, size_bytes, download_url, sample_id) "
                "VALUES (?, ?, 'ST002747_rawdata.zip', 'shared_raw_archive', ?, ?, '')",
                (f"f-{unit['unit_id']}", unit["unit_id"], 3_500_000_000, ARCHIVE_URL))
    connection.close()


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace_root = self.root / "analysis"


# ---- the plan ------------------------------------------------------------------------------------------------------

@unittest.skipUnless(contract.AVAILABLE, "the Interactive and Catalog checkouts are not where this test looks")
class PlanFromTriageTests(_Root):
    def setUp(self) -> None:
        super().setUp()
        self.database = self.root / "catalog" / "catalog.sqlite"
        self.database.parent.mkdir()
        build_catalog(self.database)

    def manifest(self, triage_dir: Path | None, *, campaign_id: str = "triaged", replan=None, reopen=None) -> dict:
        catalog = ports.read_only_catalog(self.database)
        try:
            return plan.build_manifest(
                catalog, pool="declared", campaign_id=campaign_id, analysis_purpose="annotation",
                workspace_root=self.workspace_root, raw_retention_policy="keep",
                pins={"catalog": {"version": "0.6.1"}, "libraries": [], "interactive": {"version": "0.5.69"}},
                profile=None, campaign_policy=policy.CampaignPolicy(),
                class_decision=lambda unit_id: ports.decide_class(catalog, unit_id, "annotation"),
                catalog_database=str(self.database), triage_dir=triage_dir, replan=replan, reopen=reopen,
            )
        finally:
            catalog.close()

    def test_a_passing_triage_plans_each_unit_on_its_own_files(self) -> None:
        triage_root = write_triage(self.root)
        manifest = self.manifest(triage_root)
        units = {unit["unit_key"]: unit for unit in manifest["units"]}
        self.assertEqual(sorted(units), list(UNITS))
        self.assertEqual(manifest["triage"]["accessions"][ACCESSION]["verdict"], "pass")
        kept_sets = []
        for key, unit in units.items():
            chosen = unit["curated_selection"]
            self.assertEqual(chosen["validator"]["verdict"], "pass")
            self.assertEqual(chosen["triage_relative_path"], f"{ACCESSION}/{key}.json")
            self.assertEqual(chosen["triage_sha256"], hashlib.sha256((triage_root / ACCESSION / f"{key}.json").read_bytes()).hexdigest())
            self.assertEqual(chosen["sha256"], chosen["triage_sha256"])
            self.assertEqual(chosen["rows_kept"] + chosen["rows_dropped"], 52, "every Catalog row kept or dropped")
            self.assertEqual(chosen["selection_count"], 13)
            self.assertEqual(chosen["standing"], "model_inference")
            self.assertIsNone(chosen["review_log_head_sha256"])
            self.assertTrue(unit["class_on_triaged_rows"]["evaluated"])
            self.assertEqual(unit["workspace_name"], key)
            triage = json.loads((triage_root / ACCESSION / f"{key}.json").read_text(encoding="utf-8"))
            kept_sets.append(frozenset(item["member_path"] for item in curated.kept_items(triage)))
        self.assertEqual(len(set(kept_sets)), 4, "four disjoint selections, not the whole study four times")
        self.assertFalse(set.intersection(*[set(item) for item in kept_sets]))
        text = plan.summary_text(manifest, "sha256:" + "0" * 64)
        self.assertIn("CURATED SELECTION", text)
        for key in UNITS:
            self.assertIn(f"    {key} ({ACCESSION}): kept ", text)
        record = plan.authorization_record(manifest, self.root / "m.json", "sha256:" + "0" * 64, approval_id="A", approved_by="P",
                                           approved_at="2026-11-16T00:00:00+00:00", statement="s", covers=["1", "3", "4"])
        self.assertEqual(sorted(record["units"]), list(UNITS))
        self.assertEqual([row["unit_key"] for row in plan.ledger_rows(manifest)[0]], [unit["unit_key"] for unit in manifest["units"]])

    def test_a_triage_that_does_not_pass_excludes_the_accessions_units(self) -> None:
        # Without the archive listing the validator holds every unit: byte-identical content is not ruled out.
        held = self.manifest(write_triage(self.root / "held", listing=False))
        self.assertEqual(held["units"], [])
        self.assertEqual(held["triage"]["accessions"][ACCESSION]["verdict"], "hold")
        reasons = {item["unit_id"]: item for item in held["exclusions"]}
        for key in UNITS:
            self.assertEqual(reasons[key]["reason"], curated.TRIAGE_NOT_PASS)
            self.assertIn("validate-triage.py says hold", reasons[key]["detail"])
        # A row both kept and excluded breaks the contract: error, and nothing is planned from it.
        docs = te.build_good(ACCESSION)
        first = docs["AN004456"]["rows"][0]
        docs["AN004456"]["excluded_rows"].append({key: first[key] for key in ("row_index", "sample_id", "raw_file", "member_path")}
                                                 | {"reason": "other", "belongs_to_unit": None, "detail": "both"})
        errored = self.manifest(write_triage(self.root / "error", docs))
        self.assertEqual(errored["units"], [])
        self.assertEqual(errored["triage"]["accessions"][ACCESSION]["verdict"], "error")
        self.assertIn("[error] T-COVER AN004456", {item["unit_id"]: item for item in errored["exclusions"]}["AN004456"]["detail"])
        self.assertEqual(errored["totals"]["excluded_by_reason"], {curated.TRIAGE_NOT_PASS: 4})

    def test_a_unit_without_a_triage_file_is_left_out(self) -> None:
        manifest = self.manifest(self.root / "empty-triage")
        self.assertEqual({item["reason"] for item in manifest["exclusions"]}, {curated.TRIAGE_MISSING})
        self.assertEqual(manifest["selection"]["triage_exclusion_reasons"], list(plan.TRIAGE_EXCLUSION_REASONS))

    def test_the_digest_changes_when_a_triage_changes(self) -> None:
        first = self.manifest(write_triage(self.root / "a"))
        docs = te.build_good(ACCESSION)
        docs["AN004455"]["notes"] = "A reworded note: the same files."
        second = self.manifest(write_triage(self.root / "b", docs))
        for manifest in (first, second):
            manifest["created_at"] = "fixed"
            manifest["triage"]["directory"] = "fixed"
            for unit in manifest["units"]:
                unit["curated_selection"]["triage_path"] = "fixed"
        self.assertNotEqual(plan.digest_of(plan.canonical_bytes(first)), plan.digest_of(plan.canonical_bytes(second)))
        a = {unit["unit_key"]: unit["curated_selection"] for unit in first["units"]}
        b = {unit["unit_key"]: unit["curated_selection"] for unit in second["units"]}
        self.assertNotEqual(a["AN004455"]["triage_sha256"], b["AN004455"]["triage_sha256"])
        self.assertEqual(a["AN004455"]["selection_sha256"], b["AN004455"]["selection_sha256"], "the same files")
        self.assertEqual(a["AN004454"], b["AN004454"])

    def test_a_kept_row_without_its_member_is_left_out(self) -> None:
        docs = te.build_good(ACCESSION)
        docs["AN004454"]["rows"][0]["member_path"] = None
        manifest = self.manifest(write_triage(self.root, docs))
        reasons = {item["unit_id"]: item["reason"] for item in manifest["exclusions"]}
        # The validator resolves the row by its raw_file and passes; Interactive needs the member path itself.
        self.assertEqual(reasons.get("AN004454"), curated.TRIAGE_MEMBER_UNRESOLVED)
        self.assertNotIn("AN004454", [unit["unit_key"] for unit in manifest["units"]])

    def test_reopen_plans_only_the_listed_done_units_into_workspaces_of_their_own(self) -> None:
        world = campaign_fakes.World(self.root, ["AN004456", "AN004457"])  # analysis\_campaigns\test-campaign
        with world.open() as book:
            book.connection.execute("UPDATE unit SET state = 'done', terminal_reason = 'outputs_produced'")
        for unit in ("AN004456", "AN004457"):
            workspace = self.workspace_root / "metabolomics_workbench" / ACCESSION / unit / "provenance"
            workspace.mkdir(parents=True)
            (workspace / "campaign-authorization.json").write_text(json.dumps(
                {"schema": plan.AUTHORIZATION_SCHEMA, "campaign_id": "test-campaign", "approval_id": "A0"}), encoding="utf-8")
            (workspace / "run-manifest.json").write_text('{"status": "mztab_validated"}', encoding="utf-8")
        with world.open() as book:
            book.revoke("approval-1", "Test Person", "whole-study inputs", "2026-11-15T00:00:00+00:00")
        replan = plan.replan_states(self.workspace_root, ["test-campaign"])
        evidence = self.root / "contaminated.tsv"
        evidence.write_text("accession\tunit_id\nST002747\tAN004456\n", encoding="utf-8")
        listing = self.root / "reopen.tsv"
        listing.write_text("unit_key\treason_code\tevidence_path\nAN004456\twhole_study_inputs_2026_11_15\tcontaminated.tsv\n",
                           encoding="utf-8")
        reopen = curated.read_reopen(listing)
        triage_root = write_triage(self.root)
        manifest = self.manifest(triage_root, campaign_id="again", replan=replan, reopen=reopen)
        units = {unit["unit_key"]: unit for unit in manifest["units"]}
        self.assertEqual(units["AN004456"]["workspace_name"], "AN004456@again")
        self.assertEqual(units["AN004456"]["reopened_from"]["reason_code"], "whole_study_inputs_2026_11_15")
        self.assertEqual(units["AN004456"]["reopened_from"]["state"], "done")
        self.assertEqual(units["AN004456"]["reopened_from"]["evidence_sha256"], hashlib.sha256(evidence.read_bytes()).hexdigest())
        exclusions = {item["unit_id"]: item["reason"] for item in manifest["exclusions"]}
        self.assertEqual(exclusions["AN004457"], "campaign_workspace", "a done unit not listed stays done")
        self.assertIn("REOPENED AN004456", plan.summary_text(manifest, "sha256:" + "0" * 64))
        events = plan.decision_events(manifest, "sha256:" + "1" * 64, {"approval_id": "A1", "approved_by": "P", "covers": ["1"]})
        self.assertIn(("unit_reopened", "AN004456"), [(kind, key) for kind, _detail, key in events])
        # The earlier run's workspace is not where the new one goes.
        self.assertTrue((self.workspace_root / "metabolomics_workbench" / ACCESSION / "AN004456" / "provenance" / "run-manifest.json").is_file())
        with self.assertRaises(plan.PlanError):
            self.manifest(None, campaign_id="again", replan=replan, reopen=reopen)  # no triage: the same files again
        with self.assertRaises(plan.PlanError):
            self.manifest(triage_root, campaign_id="again", replan=None, reopen=reopen)
        listing.write_text("unit_key\treason_code\tevidence_path\nAN004454\twhole_study_inputs_2026_11_15\tcontaminated.tsv\n",
                           encoding="utf-8")
        with self.assertRaises(plan.PlanError):
            self.manifest(triage_root, campaign_id="again", replan=replan, reopen=curated.read_reopen(listing))

    def test_an_interactive_before_0_5_69_cannot_be_approved_for_a_curated_selection(self) -> None:
        manifest = self.manifest(write_triage(self.root))
        manifest["pins"]["interactive"] = {"version": "0.5.67"}
        self.assertTrue(any("curated selection" in item for item in plan.approval_problems(manifest, ["1", "3", "4"])))
        manifest["pins"]["interactive"] = {"version": "0.5.69"}
        self.assertFalse(any("curated selection" in item for item in plan.approval_problems(manifest, ["1", "3", "4"])))

    def cli(self, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = runner_cli.main(["--workspace-root", str(self.workspace_root), "--interactive-root", str(contract.INTERACTIVE_ROOT),
                                    "--catalog-root", str(contract.CATALOG_ROOT), *arguments])
        return code, out.getvalue(), err.getvalue()

    def test_plan_from_the_command_line_as_a_dry_run(self) -> None:
        triage_root = write_triage(self.root)
        common = ("plan", "--campaign", "dry", "--pool", "declared", "--purpose", "annotation", "--retention", "keep",
                  "--catalog", str(self.database))
        code, out, err = self.cli(*common, "--triage-dir", str(triage_root), "--out", str(self.root / "dry"))
        self.assertEqual(code, runner_cli.EXIT_OK, err)
        manifest = json.loads((self.root / "dry" / "campaign-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(unit["unit_key"] for unit in manifest["units"]), list(UNITS))
        self.assertIn("CURATED SELECTION", out)
        reopen = self.root / "reopen.tsv"
        reopen.write_text(f"unit_key\treason_code\tevidence_path\nAN004456\twhole_study_inputs_2026_11_15\t{reopen.name}\n", encoding="utf-8")
        code, _out, err = self.cli(*common, "--triage-dir", str(triage_root), "--reopen", str(reopen), "--out", str(self.root / "dry2"))
        self.assertEqual(code, runner_cli.EXIT_REFUSED)
        self.assertIn("--replan-from", err)
        code, _out, err = self.cli(*common, "--triage-dir", str(self.root / "no such folder"), "--out", str(self.root / "dry3"))
        self.assertEqual(code, runner_cli.EXIT_REFUSED)


class ReopenListTests(_Root):
    def test_the_reopen_list_needs_a_reason_and_evidence(self) -> None:
        evidence = self.root / "e.tsv"
        evidence.write_text("x\n", encoding="utf-8")
        listing = self.root / "r.tsv"
        for text in ("unit_key\treason_code\nu1\tcode\n", "unit_key\treason_code\tevidence_path\nu1\t\te.tsv\n",
                     "unit_key\treason_code\tevidence_path\nu1\tcode\tmissing.tsv\n",
                     "unit_key\treason_code\tevidence_path\nu1\tcode\te.tsv\nu1\tcode\te.tsv\n", "# nothing\n"):
            listing.write_text(text, encoding="utf-8")
            with self.assertRaises(curated.TriageError, msg=text):
                curated.read_reopen(listing)
        listing.write_text("unit_key\treason_code\tevidence_path\nu1\twhole_study_inputs_2026_11_15\te.tsv\n", encoding="utf-8")
        self.assertEqual(curated.read_reopen(listing)["u1"]["reason_code"], "whole_study_inputs_2026_11_15")


class ChildrenTests(_Root):
    def test_a_triage_with_children_becomes_one_campaign_unit_per_child(self) -> None:
        folder = self.root / "triage" / "ACC1"
        folder.mkdir(parents=True)
        triage = {
            "schema": "msdial-unit-triage.v1", "accession": "ACC1", "unit_id": "uB", "catalog_handoff_sha256": "0" * 64,
            "catalog_row_count": 3, "status": "ready", "decided_by": {"agent": "a", "model": "m", "skill": "msdial-unit-triage",
                                                                     "skill_version": "1.0.0"},
            "unit_technical": {"ion_mode": "both", "chromatography": "RP", "acquisition_mode": "DDA"},
            "rows": [{"row_index": 0, "sample_id": "S1", "raw_file": "S1_pos", "member_path": "pos/S1_pos.raw", "role": "sample",
                      "ion_mode": "positive", "acquisition_mode": "DDA", "basis": "inferred"},
                     {"row_index": 1, "sample_id": "S1n", "raw_file": "S1_neg", "member_path": "neg/S1_neg.raw", "role": "sample",
                      "ion_mode": "negative", "acquisition_mode": "DDA", "basis": "inferred"}],
            "added_members": [], "excluded_rows": [{"row_index": 2, "reason": "requires_conversion"}],
            "children": [{"child_key": "pos-dda", "ion_mode": "positive", "acquisition_mode": "DDA", "member_paths": ["pos/S1_pos.raw"]},
                         {"child_key": "neg-dda", "ion_mode": "negative", "acquisition_mode": "DDA", "member_paths": ["neg/S1_neg.raw"]}],
        }
        (folder / "uB.json").write_text(json.dumps(triage), encoding="utf-8")
        entries, reason, _detail = curated.unit_entries(
            accession_dir=folder, triage_root=self.root / "triage", unit_id="uB",
            validation={"verdict": "pass", "validator_sha256": "v" * 64}, listing={}, catalog=None)
        self.assertIsNone(reason)
        for entry in entries:
            entry["class_on_triaged_rows"] = curated.class_on_rows({"kind": "abstention", "assignments": [["S1", "All"], ["S1n", "All"]]},
                                                                   entry["triage"], entry["child"])
        unit = {"unit_id": "uB", "accession": "ACC1", "ion_mode": "Both", "acquisition_mode": "DDA"}
        planned = plan._curated_units(unit, entries, "c1", None, None)
        self.assertEqual([item["unit_key"] for item in planned], ["uB-pos-dda", "uB-neg-dda"])
        self.assertEqual([item["ion_mode"] for item in planned], ["Positive", "Negative"])
        self.assertEqual([item["curated_selection"]["rows_kept"] for item in planned], [1, 1])
        self.assertEqual(len({item["curated_selection"]["selection_sha256"] for item in planned}), 2)
        self.assertEqual(planned[0]["class_on_triaged_rows"]["counts_after"], {"All": 1})

    def test_a_contrast_that_loses_a_side_is_reported(self) -> None:
        triage = {"rows": [{"row_index": 0, "sample_id": "A1", "member_path": "a.raw"}, {"row_index": 1, "sample_id": "QC1", "member_path": "q.raw"}]}
        decision = {"kind": "proposal", "assignments": [["A1", "Treated"], ["B1", "Control"], ["QC1", "QC"]]}
        cut = curated.class_on_rows(decision, triage)
        self.assertFalse(cut["contrast_intact"])
        self.assertEqual(cut["lost_classes"], ["Control"])
        self.assertTrue(curated.class_on_rows({"kind": "abstention", "assignments": [["A1", "All"]]}, triage)["contrast_intact"])


# ---- the runner -----------------------------------------------------------------------------------------------------

def curated_world(root: Path, units: dict[str, dict], *, groups: dict | None = None) -> campaign_fakes.World:
    """A World whose approved manifest plans `units` (unit key: {catalog, child, workspace}) from triage files."""
    folder = root / "triage" / "ACC1"
    folder.mkdir(parents=True, exist_ok=True)
    planned = []
    for key, item in units.items():
        catalog_id = item.get("catalog", key)
        path = folder / f"{catalog_id}.json"
        if not path.is_file():
            path.write_text(json.dumps({"schema": "msdial-unit-triage.v1", "unit_id": catalog_id, "rows": [
                {"row_index": 0, "sample_id": f"{catalog_id}-S1", "member_path": f"{item.get('folder', catalog_id)}/S1.raw", "role": "sample"},
                {"row_index": 1, "sample_id": f"{catalog_id}-S2", "member_path": f"{item.get('folder', catalog_id)}/S2.raw", "role": "sample"}]}),
                encoding="utf-8")
        planned.append({
            "unit_id": catalog_id, "unit_key": key, "accession": "ACC1", "workspace_name": item.get("workspace", key),
            "curated_selection": {
                "triage_path": str(path), "triage_relative_path": f"ACC1/{path.name}",
                "triage_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "triage_record_sha256": None,
                "catalog_handoff_sha256": "0" * 64, "catalog_rows_sha256": item.get("rows_sha256", curated.rows_sha256([])),
                "child_key": item.get("child"), "selection_sha256": "5" * 64,
            },
        })
    catalog_units = {key: item.get("catalog", key) for key, item in units.items()}
    return campaign_fakes.World(root, list(units), manifest={"units": planned}, catalog_units=catalog_units,
                                download_groups=groups)


def unit_row(world: campaign_fakes.World, key: str) -> dict:
    with world.open() as book:
        return book.unit(key)


def events(world: campaign_fakes.World, kind: str | None = None) -> list[dict]:
    with world.open() as book:
        return [{**row, "detail": json.loads(row["detail_json"])} for row in book.events(kind)]


class RunnerTests(_Root):
    def test_the_curated_selection_reaches_the_download_and_the_unit_runs(self) -> None:
        world = curated_world(self.root, {"u1": {}})
        world.run()
        self.assertEqual(unit_row(world, "u1")["state"], "done")
        sent = world.interactive.curated_downloads[0]
        self.assertTrue(sent["curated_selection_path"].endswith(str(Path("handoffs") / "u1" / "triage.json")))
        original = world.root / "catalog-data" / "handoffs" / "u1.json"
        self.assertEqual(sent["original_handoff_sha256"], hashlib.sha256(original.read_bytes()).hexdigest())
        self.assertNotIn("workspace_unit_key", sent)
        checked = events(world, "triage_checked")[0]["detail"]
        self.assertEqual((checked["verdict"], checked["handoff_identity"]), ("pass", "rows_identical"))
        applied = events(world, "curated_selection_applied")
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["detail"]["standing"], curated.ACCEPTED_DECISION)

    def test_a_triage_changed_after_approval_stops_the_unit_at_the_copy(self) -> None:
        world = curated_world(self.root, {"u1": {}})
        (self.root / "triage" / "ACC1" / "u1.json").write_text('{"edited": true}', encoding="utf-8")
        world.run()
        unit = unit_row(world, "u1")
        self.assertEqual((unit["state"], unit["terminal_reason"]), ("stopped_policy_drift", "triage_digest_changed"))
        self.assertEqual(world.interactive.download_starts, [])

    def test_a_handoff_whose_rows_changed_stops_the_unit(self) -> None:
        world = curated_world(self.root, {"u1": {"rows_sha256": "f" * 64}})
        world.run()
        unit = unit_row(world, "u1")
        self.assertEqual((unit["state"], unit["terminal_reason"]), ("stopped_policy_drift", "triage_handoff_rows_changed"))
        self.assertEqual(world.interactive.download_starts, [])

    def test_a_manifest_that_is_not_the_approved_one_stops_every_unit(self) -> None:
        world = curated_world(self.root, {"u1": {}})
        Path(world.campaign["manifest_path"]).write_text('{"units": []}', encoding="utf-8")
        world.run()
        unit = unit_row(world, "u1")
        self.assertEqual((unit["state"], unit["terminal_reason"]), ("stopped_policy_drift", "manifest_digest_changed"))

    def test_an_interactive_that_cannot_apply_the_selection_holds_the_unit_unrun(self) -> None:
        world = curated_world(self.root, {"u1": {}})
        world.interactive.curated_supported = False
        with world.open() as book:
            world.runner(book).run(max_iterations=40)
        self.assertEqual(unit_row(world, "u1")["state"], "contract_held")
        self.assertEqual(world.interactive.download_starts, [])

    def test_a_selection_the_lease_did_not_record_holds_the_unit_before_its_preflight(self) -> None:
        world = curated_world(self.root, {"u1": {}})
        world.interactive.curated_recorded = False
        with world.open() as book:
            world.runner(book).run(max_iterations=60)
        unit = unit_row(world, "u1")
        self.assertEqual(unit["state"], "contract_held")
        self.assertFalse([call for call in world.interactive.calls if call[0] == "preflight"])
        held = events(world, "contract_held")[0]["detail"]
        self.assertEqual(held["contract"], "curated_selection_not_recorded")

    def test_a_refusal_after_the_fetch_ends_the_unit_with_no_retry(self) -> None:
        world = curated_world(self.root, {"u1": {}})
        world.scripts["u1"] = campaign_fakes.UnitScript(downloads=["coverage_refused", "ok", "ok"])
        world.run()
        unit = unit_row(world, "u1")
        self.assertEqual((unit["state"], unit["terminal_reason"], unit["failures"]), ("failed", policy.ATTRIBUTION_REFUSED, 0))
        self.assertEqual(json.loads(unit["terminal_detail"])["reason_code"], "attribution_coverage_below_minimum")
        self.assertEqual(world.interactive.download_starts, ["u1"], "never fetched again")
        self.assertEqual(events(world, "terminal_refusal")[0]["detail"]["reason_code"], "attribution_coverage_below_minimum")

    def test_the_coverage_gate_refusal_ends_a_plain_unit_too(self) -> None:
        world = campaign_fakes.World(self.root, ["u1"])
        world.scripts["u1"] = campaign_fakes.UnitScript(downloads=["coverage_refused", "ok", "ok"])
        world.run()
        unit = unit_row(world, "u1")
        self.assertEqual((unit["state"], unit["terminal_reason"], unit["failures"]), ("failed", policy.ATTRIBUTION_REFUSED, 0))

    def test_a_refusal_before_any_byte_ends_the_unit_with_no_retry(self) -> None:
        world = curated_world(self.root, {"u1": {}})
        world.scripts["u1"] = campaign_fakes.UnitScript(downloads=["refused_curated", "ok", "ok"])
        world.run()
        unit = unit_row(world, "u1")
        self.assertEqual((unit["state"], unit["terminal_reason"], unit["failures"]), ("failed", policy.ATTRIBUTION_REFUSED, 0))
        self.assertEqual(json.loads(unit["terminal_detail"])["reason_code"], "curated_selection_not_approved")
        with world.open() as book:
            self.assertEqual(len([row for row in book.attempts("u1") if row["step"] == "download_start"]), 1)

    def test_not_retryable_reads_only_its_own_codes(self) -> None:
        self.assertIsNone(policy.not_retryable({"ok": False, "reason": "exception", "detail": "HTTP 503"}))
        self.assertIsNone(policy.not_retryable({"failure": {"reason_code": "attribution_coverage_below_minimum"}}),
                          "a lease record must say retryable false")
        self.assertIsNone(policy.not_retryable({"ok": False, "reason": "unsupported",
                                                "detail": "takes no curated_selection_path; this Interactive predates the contract"}))
        self.assertEqual(policy.not_retryable({"failure": {"reason_code": "cross_unit_new_rule", "retryable": False}})["reason_code"],
                         "cross_unit_new_rule")
        self.assertIsNone(policy.not_retryable({"ok": False, "retryable": True, "detail": "curated_selection_not_approved"}))

    def test_the_group_waits_for_every_preflight_and_is_checked_once(self) -> None:
        world = curated_world(self.root, {"u1": {}, "u2": {}}, groups={"g": ["u1", "u2"]})
        world.run()
        self.assertEqual([unit_row(world, key)["state"] for key in ("u1", "u2")], ["done", "done"])
        self.assertEqual(len(world.interactive.cross_unit_calls), 1)
        self.assertEqual(len(world.interactive.cross_unit_calls[0]), 2)
        with world.open() as book:
            waits = [json.loads(row["detail_json"]) for row in book.transitions("u1") if row["to_state"] == "waiting_retry"]
        self.assertTrue(any(item.get("waiting_for_group_preflight") == ["u2"] for item in waits))
        verdict = events(world, "cross_unit_verdict")
        self.assertEqual(len(verdict), 1)
        self.assertEqual(verdict[0]["detail"]["verdict"], "pass")

    def test_a_unit_the_cross_unit_check_refuses_ends_with_no_retry(self) -> None:
        world = curated_world(self.root, {"u1": {}, "u2": {}}, groups={"g": ["u1", "u2"]})
        world.interactive.cross_unit_refusals = {"u2": ("cross_unit_duplicate_inputs", "D")}
        world.run()
        self.assertEqual(unit_row(world, "u1")["state"], "done")
        unit = unit_row(world, "u2")
        self.assertEqual((unit["state"], unit["terminal_reason"], unit["failures"]), ("failed", policy.ATTRIBUTION_REFUSED, 0))
        self.assertEqual(json.loads(unit["terminal_detail"])["reason_code"], "cross_unit_duplicate_inputs")

    def test_an_interactive_without_the_cross_unit_check_holds_the_group(self) -> None:
        world = curated_world(self.root, {"u1": {}, "u2": {}}, groups={"g": ["u1", "u2"]})
        world.interactive.cross_unit_supported = False
        with world.open() as book:
            world.runner(book).run(max_iterations=200)
        self.assertIn("contract_held", {unit_row(world, key)["state"] for key in ("u1", "u2")})
        self.assertFalse(world.interactive.console_starts)

    def test_a_header_that_disagrees_with_the_triage_holds_the_unit_for_a_person(self) -> None:
        world = curated_world(self.root, {"u1": {}})
        world.scripts["u1"] = campaign_fakes.UnitScript(reconciliation="disagree")
        world.run()
        self.assertEqual(unit_row(world, "u1")["state"], "disposition_held")
        found = events(world, "header_reconciliation")[0]["detail"]
        self.assertEqual((found["verdict"], found["disagreements"], found["fields"]), ("disagree", 1, ["ion_mode"]))
        agreeing = curated_world(self.root / "agree", {"u1": {}})
        agreeing.scripts["u1"] = campaign_fakes.UnitScript(reconciliation="agree")
        agreeing.run()
        self.assertEqual(unit_row(agreeing, "u1")["state"], "done")
        self.assertEqual(events(agreeing, "header_reconciliation")[0]["detail"]["verdict"], "agree")

    def test_a_child_and_a_reopened_unit_get_workspaces_of_their_own(self) -> None:
        world = curated_world(self.root, {"u1-pos": {"catalog": "u1", "child": "pos", "workspace": "u1-pos@c2"}})
        world.run()
        unit = unit_row(world, "u1-pos")
        self.assertEqual(unit["state"], "done")
        self.assertTrue(unit["workspace"].endswith("u1-pos@c2"))
        sent = world.interactive.curated_downloads[0]
        self.assertEqual((sent["curated_selection_child_key"], sent["workspace_unit_key"]), ("pos", "u1-pos@c2"))
        self.assertTrue(unit["manifest_path"].startswith(unit["workspace"]))

    def test_the_decision_ledger_is_exported_append_only_and_chained(self) -> None:
        world = curated_world(self.root, {"u1": {}, "u2": {}}, groups={"g": ["u1", "u2"]})
        world.run()
        target = self.root / "decision-ledger.jsonl"
        with world.open() as book:
            first = curated.export_decision_ledger(target, book.events())
        lines = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
        kinds = [line["kind"] for line in lines]
        for kind in ("triage_checked", "curated_selection_applied", "header_reconciliation", "cross_unit_verdict"):
            self.assertIn(kind, kinds)
        self.assertTrue(all(line["standing"] in curated.STANDINGS for line in lines))
        self.assertEqual(curated.verify_decision_ledger(target), [])
        with world.open() as book:
            book.event("person_answer", "2026-11-16T00:00:00+00:00", {"standing": curated.PERSON_ANSWER, "by": "Synthetic Person",
                                                                      "answer": "accept the header"}, unit_key="u1")
            second = curated.export_decision_ledger(target, book.events())
        self.assertEqual((second["appended"], second["lines"]), (1, first["lines"] + 1))
        tampered = target.read_text(encoding="utf-8").replace("cross_unit_verdict", "cross_unit_verdicT", 1)
        target.write_text(tampered, encoding="utf-8")
        with world.open() as book, self.assertRaises(curated.TriageError):
            curated.export_decision_ledger(target, book.events())

    def test_the_decisions_command_writes_beside_the_manifest(self) -> None:
        world = curated_world(self.root, {"u1": {}})
        world.run()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = runner_cli.main(["--workspace-root", str(world.workspace_root), "decisions", "--campaign", "test-campaign"])
        self.assertEqual(code, runner_cli.EXIT_OK, err.getvalue())
        self.assertTrue((world.directory / curated.DECISION_LEDGER).is_file())
        self.assertEqual(json.loads(out.getvalue())["chain_problems"], [])

    def test_the_review_log_is_hash_chained(self) -> None:
        folder = self.root / "ACC1"
        folder.mkdir()
        first = curated.append_review_log(folder, "digest_approval", "a" * 64, "2026-11-16T00:00:00+00:00",
                                          manifest_digest="sha256:" + "1" * 64, approval_id="A1", by="Synthetic Person")
        second = curated.append_review_log(folder, "digest_approval", "b" * 64, "2026-11-16T00:00:01+00:00",
                                           manifest_digest="sha256:" + "1" * 64, approval_id="A1", by="Synthetic Person")
        lines = (folder / curated.REVIEW_LOG).read_bytes().split(b"\n")
        self.assertIsNone(first["prev_sha256"])
        self.assertEqual((second["seq"], second["prev_sha256"]), (2, hashlib.sha256(lines[0]).hexdigest()))
        self.assertEqual(curated.review_log_head(folder), hashlib.sha256(lines[1]).hexdigest())


# ---- end to end, and the replay ---------------------------------------------------------------------------------------

@unittest.skipUnless(contract.AVAILABLE, "the Interactive and Catalog checkouts are not where this test looks")
class EndToEndTests(_Root):
    """ST002747 from its triage files to four runs on four disjoint selections, with the fakes for Interactive; then
    the decision ledger and the replay of the selections without any model. No byte is downloaded."""

    def setUp(self) -> None:
        super().setUp()
        self.database = self.root / "catalog" / "catalog.sqlite"
        self.database.parent.mkdir()
        build_catalog(self.database)

    def test_st002747_from_triage_to_runs_ledger_and_replay(self) -> None:
        triage_root = write_triage(self.root)
        # 1. The validator passes the set.
        self.assertEqual(curated.run_validator(triage_root / ACCESSION)["verdict"], "pass")
        # 2. The dry-run plan: four units, each with its rows.
        catalog = ports.read_only_catalog(self.database)
        try:
            manifest = plan.build_manifest(
                catalog, pool="declared", campaign_id="test-campaign", analysis_purpose="annotation",
                workspace_root=self.workspace_root, raw_retention_policy="keep",
                pins={"catalog": {"version": "0.6.1"}, "libraries": [], "interactive": {"version": "0.5.69"}},
                profile=None, campaign_policy=policy.CampaignPolicy(),
                class_decision=lambda unit_id: ports.decide_class(catalog, unit_id, "annotation"),
                catalog_database=str(self.database), triage_dir=triage_root)
        finally:
            catalog.close()
        keys = [unit["unit_key"] for unit in manifest["units"]]
        self.assertEqual(sorted(keys), list(UNITS))
        self.assertEqual({unit["curated_selection"]["selection_count"] for unit in manifest["units"]}, {13})
        self.assertEqual(len({unit["group_id"] for unit in manifest["units"]}), 1, "one archive serves all four")
        # 3-4. The runner, with the fakes: each download carries its selection; the group is checked once and passes.
        world = campaign_fakes.World(self.root / "run", keys, manifest=manifest, download_groups={"g": keys})
        fixture = json.loads((te.fixture_dir(ACCESSION) / "catalog.json").read_text(encoding="utf-8"))
        world.handoff_rows = {unit["unit_id"]: unit["rows"] for unit in fixture["units"]}
        digest = world.digest
        with world.open() as book:
            for kind, detail, key in plan.decision_events(manifest, digest, world.approval):
                book.event(kind, "2026-10-01T00:00:00+00:00", detail, unit_key=key)
        world.run()
        self.assertEqual({unit_row(world, key)["state"] for key in keys}, {"done"})
        self.assertEqual(len(world.interactive.cross_unit_calls), 1)
        self.assertEqual(len(world.interactive.cross_unit_calls[0]), 4)
        self.assertEqual({item["curated_selection_path"].rsplit("handoffs", 1)[1][1:].split("\\")[0].split("/")[0]
                          for item in world.interactive.curated_downloads}, set(keys))
        checked = [item["detail"] for item in events(world, "triage_checked")]
        self.assertEqual({item["handoff_identity"] for item in checked}, {"rows_identical"})
        # 5. The decision ledger.
        target = world.directory / curated.DECISION_LEDGER
        with world.open() as book:
            curated.export_decision_ledger(target, book.events())
        kinds = [json.loads(line)["kind"] for line in target.read_text(encoding="utf-8").splitlines()]
        expected = {"triage_recorded": 4, "triage_validated": 4, "manifest_approved": 1, "triage_checked": 4,
                    "curated_selection_applied": 4, "header_reconciliation": 4, "cross_unit_verdict": 1}
        self.assertEqual({kind: kinds.count(kind) for kind in expected}, expected)
        self.assertEqual(kinds[:9], ["triage_recorded", "triage_validated"] * 4 + ["manifest_approved"])
        self.assertEqual(curated.verify_decision_ledger(target), [])
        # The replay of the selections, from the bundle alone, without any model.
        (world.directory / "campaign-manifest.summary.json").write_text(json.dumps({"manifest_digest": digest}), encoding="utf-8")
        report = replay_cli.replay(world.directory, self.root / "replay-workspace", "selection")
        self.assertEqual(report["result"], replay_cli.EQUAL, json.dumps(report, indent=1))
        rebuilt = json.loads((world.directory / "replay-inputs" / "AN004456.json").read_text(encoding="utf-8"))
        self.assertEqual(len(rebuilt["analysis_inputs"]), 13)
        self.assertTrue(all(item["path"].startswith("VIAH002_003_WorkbenchUpload/") for item in rebuilt["analysis_inputs"]))
        # A changed decision file is a difference; a level whose data are not there is not evaluable, never equal.
        bundle = self.root / "bundle"
        shutil.copytree(world.directory, bundle, ignore=shutil.ignore_patterns("ledger.sqlite*"))
        shutil.copytree(triage_root, bundle / "triage")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(replay_cli.main(["--bundle", str(bundle), "--workspace-root", str(self.root / "w"), "--level", "selection",
                                              "--out", str(self.root / "r1.json")]), 0)
        docs = te.build_good(ACCESSION)
        docs["AN004457"]["rows"] = docs["AN004457"]["rows"][:-1]
        te.write({"AN004457": docs["AN004457"]}, bundle / "triage" / ACCESSION)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(replay_cli.main(["--bundle", str(bundle), "--workspace-root", str(self.root / "w"), "--level", "selection",
                                              "--out", str(self.root / "r2.json")]), 1)
            shutil.copyfile(triage_root / ACCESSION / "AN004457.json", bundle / "triage" / ACCESSION / "AN004457.json")
            self.assertEqual(replay_cli.main(["--bundle", str(bundle), "--workspace-root", str(self.root / "w"), "--level", "inputs",
                                              "--out", str(self.root / "r3.json")]), 2)

    def test_a_whole_study_selection_that_reached_the_runner_is_refused_by_rule_d(self) -> None:
        """Defence in depth: the stage3b-style selection of ST002283 (every unit keeps the same rows) never passes the
        validator; were it planned anyway, the cross-unit check refuses every unit after the first."""
        docs = te.stage3b_style("ST002283")
        triage_root = write_triage(self.root, docs, accession="ST002283")
        self.assertEqual(curated.run_validator(triage_root / "ST002283")["verdict"], "error")
        planned = []
        for unit_id, doc in docs.items():
            path = triage_root / "ST002283" / f"{unit_id}.json"
            planned.append({"unit_id": unit_id, "unit_key": unit_id, "accession": "ST002283", "workspace_name": unit_id,
                            "curated_selection": {"triage_path": str(path), "triage_relative_path": f"ST002283/{unit_id}.json",
                                                  "triage_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                                  "catalog_handoff_sha256": "0" * 64, "catalog_rows_sha256": curated.rows_sha256([]),
                                                  "child_key": None}})
        keys = list(docs)
        world = campaign_fakes.World(self.root / "run", keys, manifest={"units": planned}, download_groups={"g": keys})
        world.run()
        states = {key: unit_row(world, key) for key in keys}
        refused = [key for key, unit in states.items() if unit["terminal_reason"] == policy.ATTRIBUTION_REFUSED]
        # Four units keeping two identical pairs of sets: the second unit of each pair is refused.
        self.assertEqual(len(refused), 2)
        self.assertTrue(all(json.loads(states[key]["terminal_detail"])["reason_code"] == "cross_unit_duplicate_inputs" for key in refused))
        self.assertTrue(all(states[key]["failures"] == 0 for key in refused))


class NoModelClientTests(unittest.TestCase):
    MODEL_CLIENTS = ("anthropic", "openai", "google.generativeai", "google.genai", "mistralai", "cohere", "ollama",
                     "langchain", "litellm", "transformers")

    def test_the_replay_and_the_curated_module_import_no_model_client(self) -> None:
        for path in (SCRIPTS / "replay-campaign.py", SCRIPTS / "campaign" / "curated.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names |= {alias.name for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names.add(node.module)
            self.assertFalse({name for name in names if name.split(".")[0] in {item.split(".")[0] for item in self.MODEL_CLIENTS}},
                             f"{path.name} imports a model client")
        probe = ("import importlib.util, sys; spec = importlib.util.spec_from_file_location('r', sys.argv[1]); "
                 "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
                 f"print([n for n in sys.modules if n.split('.')[0] in {[item.split('.')[0] for item in self.MODEL_CLIENTS]!r}])")
        done = subprocess.run([sys.executable, "-I", "-c", probe, str(SCRIPTS / "replay-campaign.py")], capture_output=True, text=True, check=True)
        self.assertEqual(done.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
