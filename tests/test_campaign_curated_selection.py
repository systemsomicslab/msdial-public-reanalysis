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
import re
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


def aligned_docs(accession: str = ACCESSION, docs: dict | None = None, *, with_rows: bool = True) -> tuple[dict, dict]:
    """(triage files, Catalog rows by unit): build_good's triage files (or `docs`), each made from the handoff the fake
    Catalog writes for its unit, so its catalog_handoff_sha256 is that handoff's sha256 (WS0 decision 7 binds it byte
    for byte). `with_rows` False leaves the handoff without sample rows, as a World given none writes it."""
    docs = docs if docs is not None else te.build_good(accession)
    fixture = json.loads((te.fixture_dir(accession) / "catalog.json").read_text(encoding="utf-8"))
    rows = {unit["unit_id"]: unit["rows"] for unit in fixture["units"]}
    for unit_id, doc in docs.items():
        handoff = campaign_fakes.fake_handoff(unit_id, campaign_fakes.World.class_digest(unit_id),
                                              rows[unit_id] if with_rows else None)
        doc["catalog_handoff_sha256"] = hashlib.sha256(campaign_fakes.handoff_bytes(handoff)).hexdigest()
    return docs, rows


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
            validation={"verdict": "pass", "validator_sha256": "v" * 64}, catalog=None)
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

def synthetic_record(path: Path, triage: dict, *, listing_rows: list[tuple[str, int, str]] | None = None) -> Path:
    """A decision record (msdial-llm-decision-record.v1) beside a triage, as the wrapper writes it: payload.sha256 is the
    triage's, and its archive_listing input (inputs\\<unit_id>.members.tsv, hashed) gives the selection crc32 and size."""
    folder = path.parent
    inputs = folder / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    tsv = inputs / f"{triage['unit_id']}.members.tsv"
    rows = listing_rows or [(str(row["member_path"]), 1000 + index, f"{index + 1:08X}")
                            for index, row in enumerate(triage.get("rows") or [])]
    tsv.write_text("archive\tpath\tsize\tcrc32\n" + "".join(f"ACC1_rawdata.zip\t{member}\t{size}\t{crc}\n"
                                                           for member, size, crc in rows), encoding="utf-8")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    record = curated.record_path(path)
    record.write_text(json.dumps({
        "schema": "msdial-llm-decision-record.v1", "decision_id": sha, "kind": "unit_triage", "accession": "ACC1",
        "unit_id": triage["unit_id"], "payload": {"path": path.name, "schema": triage["schema"], "sha256": sha},
        "supersedes": None, "agent": {"harness": "synthetic-harness", "model_id": "synthetic-model"},
        "inputs": [{"role": "archive_listing", "path": f"inputs/{tsv.name}",
                    "sha256": hashlib.sha256(tsv.read_bytes()).hexdigest(), "bytes": tsv.stat().st_size}],
    }), encoding="utf-8")
    return record


def curated_world(root: Path, units: dict[str, dict], *, groups: dict | None = None) -> campaign_fakes.World:
    """A World whose approved manifest plans `units` (unit key: {catalog, child, workspace, folder, record, handoff_sha256})
    from triage files. Each triage is made from the handoff the fake Catalog writes for its unit (its sha256, unless
    handoff_sha256 names another), keeps two rows, and is ready; `record` writes its decision record with an archive
    listing beside it, as the framework does. The planned selection_sha256 is msdial-selection-canonical.v1 over the
    listing the record binds, as the plan computes it."""
    folder = root / "triage" / "ACC1"
    folder.mkdir(parents=True, exist_ok=True)
    planned = []
    rows_of: dict[str, list[dict]] = {}
    for key, item in units.items():
        catalog_id = item.get("catalog", key)
        member_folder = item.get("folder", catalog_id)
        rows = [{"sample_id": f"{catalog_id}-S1", "raw_file": "S1"}, {"sample_id": f"{catalog_id}-S2", "raw_file": "S2"}]
        rows_of[catalog_id] = rows
        handoff = campaign_fakes.fake_handoff(catalog_id, campaign_fakes.World.class_digest(catalog_id), rows)
        handoff_sha = item.get("handoff_sha256") or hashlib.sha256(campaign_fakes.handoff_bytes(handoff)).hexdigest()
        path = folder / f"{catalog_id}.json"
        if not path.is_file():
            triage = {
                "schema": "msdial-unit-triage.v1", "accession": "ACC1", "unit_id": catalog_id, "status": "ready",
                "catalog_handoff_sha256": handoff_sha, "catalog_row_count": len(rows),
                "unit_technical": {"ion_mode": "positive", "chromatography": "RP", "acquisition_mode": "DDA"},
                "rows": [{"row_index": index, "sample_id": row["sample_id"], "raw_file": row["raw_file"],
                          "member_path": f"{member_folder}/{row['raw_file']}.raw", "role": "sample", "ion_mode": "positive",
                          "acquisition_mode": "DDA", "basis": "declared"} for index, row in enumerate(rows)],
                "added_members": [], "excluded_rows": [],
            }
            if item.get("child"):
                triage["children"] = [{"child_key": item["child"], "ion_mode": "positive", "acquisition_mode": "DDA",
                                       "member_paths": [row["member_path"] for row in triage["rows"]]}]
            path.write_text(json.dumps(triage), encoding="utf-8")
        triage = json.loads(path.read_text(encoding="utf-8"))
        record = curated.record_path(path)
        if item.get("record") and not record.is_file():
            synthetic_record(path, triage)
        listing = curated.record_inputs(record, hashlib.sha256(path.read_bytes()).hexdigest())["listing"] if item.get("record") else {}
        planned.append({
            "unit_id": catalog_id, "unit_key": key, "accession": "ACC1", "workspace_name": item.get("workspace", key),
            "curated_selection": {
                "triage_path": str(path), "triage_relative_path": f"ACC1/{path.name}",
                "triage_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "triage_record_sha256": hashlib.sha256(record.read_bytes()).hexdigest() if item.get("record") else None,
                "catalog_handoff_sha256": triage["catalog_handoff_sha256"], "catalog_rows_sha256": curated.rows_sha256(rows),
                "child_key": item.get("child"), "selection_sha256": curated.selection_sha256(triage, listing, item.get("child")),
                "validator": {"script_sha256": "v" * 64, "verdict": "pass", "report_sha256": "r" * 64},
            },
        })
    catalog_units = {key: item.get("catalog", key) for key, item in units.items()}
    return campaign_fakes.World(root, list(units), manifest={"units": planned}, catalog_units=catalog_units,
                                download_groups=groups, handoff_rows=rows_of)


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
        # WS0 decision 2: the copy is handoffs\<unit_key>\<unit_id>.json, where Interactive looks for its record.
        self.assertTrue(sent["curated_selection_path"].endswith(str(Path("handoffs") / "u1" / "u1.json")))
        original = world.root / "catalog-data" / "handoffs" / "u1.json"
        triage = json.loads((self.root / "triage" / "ACC1" / "u1.json").read_text(encoding="utf-8"))
        # WS0 decision 7: the triaged handoff's sha256, with the Catalog's bytes as taken, kept beside the copy.
        self.assertEqual(sent["original_handoff_sha256"], triage["catalog_handoff_sha256"])
        self.assertEqual(sent["original_handoff_sha256"], hashlib.sha256(original.read_bytes()).hexdigest())
        self.assertTrue(sent["original_handoff_path"].endswith("handoff.original.json"))
        self.assertEqual(sent["curated_selection_validator"]["verdict"], "pass")
        self.assertNotIn("workspace_unit_key", sent)
        self.assertNotIn("decision_record_path", sent, "no record was approved")
        checked = events(world, "triage_checked")[0]["detail"]
        self.assertEqual((checked["verdict"], checked["handoff_identity"]), ("pass", "handoff_identical"))
        self.assertFalse(Path(checked["copy_path"]).is_absolute(), "ledger paths are workspace-relative")
        applied = events(world, "curated_selection_applied")
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["detail"]["standing"], curated.ACCEPTED_DECISION)
        self.assertTrue(applied[0]["detail"]["selection_sha256_equal"])
        self.assertEqual(applied[0]["detail"]["plan_selection_sha256"], applied[0]["detail"]["interactive_selection_sha256"])

    def test_a_triage_changed_after_approval_stops_the_unit_at_the_copy(self) -> None:
        world = curated_world(self.root, {"u1": {}})
        (self.root / "triage" / "ACC1" / "u1.json").write_text('{"edited": true}', encoding="utf-8")
        world.run()
        unit = unit_row(world, "u1")
        self.assertEqual((unit["state"], unit["terminal_reason"]), ("stopped_policy_drift", "triage_digest_changed"))
        self.assertEqual(world.interactive.download_starts, [])
        # Refused at the copy itself, not only by the recheck at download start: the unit never reached handoff_ready.
        checked = events(world, "triage_checked")[0]["detail"]
        self.assertEqual((checked["verdict"], checked["reason"]), ("refused", "triage_digest_changed"))
        with world.open() as book:
            self.assertNotIn("handoff_ready", [row["to_state"] for row in book.transitions("u1")])

    def test_a_handoff_whose_bytes_changed_is_held_for_a_person(self) -> None:
        """WS0 decision 7: the triaged handoff is bound byte for byte; one whose bytes changed (even with the same rows)
        is a hold, never a pass, and nothing is downloaded."""
        world = curated_world(self.root, {"u1": {"handoff_sha256": "f" * 64}})
        with world.open() as book:
            world.runner(book).run(max_iterations=60)
        unit = unit_row(world, "u1")
        self.assertEqual(unit["state"], "contract_held")
        self.assertEqual(events(world, "contract_held")[-1]["detail"]["contract"], "triage_handoff_changed")
        checked = events(world, "triage_checked")[0]["detail"]
        self.assertEqual((checked["verdict"], checked["reason"], checked["rows_identical"]), ("held", "triage_handoff_changed", True))
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
        self.assertEqual(verdict[0]["detail"]["verdict"], "passed")
        self.assertEqual([item["verdict"] for item in verdict[0]["detail"]["unit_verdicts"]], ["passed", "passed"])
        self.assertFalse(any(Path(path).is_absolute() for path in verdict[0]["detail"]["manifest_paths"]))

    def test_a_unit_the_cross_unit_check_refuses_ends_with_no_retry(self) -> None:
        world = curated_world(self.root, {"u1": {}, "u2": {}}, groups={"g": ["u1", "u2"]})
        world.interactive.cross_unit_refusals = {"u2": ("cross_unit_duplicate_inputs", "D")}
        world.run()
        self.assertEqual(unit_row(world, "u1")["state"], "done")
        unit = unit_row(world, "u2")
        self.assertEqual((unit["state"], unit["terminal_reason"], unit["failures"]), ("failed", policy.ATTRIBUTION_REFUSED, 0))
        self.assertEqual(json.loads(unit["terminal_detail"])["reason_code"], "cross_unit_duplicate_inputs")
        refusal = [item for item in events(world, "terminal_refusal") if item["unit_key"] == "u2"][0]["detail"]
        self.assertEqual(refusal["rule"], "D")

    def test_a_sibling_that_ended_before_its_manifest_leaves_a_single_member(self) -> None:
        """WS0 decision 6: with one surviving member the tool is not called; the verdict is not_applicable."""
        world = curated_world(self.root, {"u1": {}, "u2": {}}, groups={"g": ["u1", "u2"]})
        (self.root / "triage" / "ACC1" / "u2.json").write_text('{"edited": true}', encoding="utf-8")
        world.run()
        self.assertEqual(unit_row(world, "u2")["terminal_reason"], "triage_digest_changed")
        self.assertEqual(unit_row(world, "u1")["state"], "done")
        self.assertEqual(world.interactive.cross_unit_calls, [])
        verdict = events(world, "cross_unit_verdict")[0]["detail"]
        self.assertEqual((verdict["verdict"], verdict["reason"], verdict["units"]), ("not_applicable", "single_member", ["u1"]))
        drift = [item for item in events(world, "terminal_refusal") if item["unit_key"] == "u2"]
        self.assertEqual(drift[0]["detail"]["reason_code"], "triage_digest_changed", "policy drift is in the ledger")

    def test_a_reply_in_another_shape_is_never_a_pass(self) -> None:
        world = curated_world(self.root, {"u1": {}, "u2": {}}, groups={"g": ["u1", "u2"]})
        real = world.interactive.cross_unit_check

        def old_shape(*, manifest_paths):
            reply = real(manifest_paths=manifest_paths)
            for item in reply["units"]:
                item["verdict"] = "pass"
            reply["verdict"] = "pass"
            return reply

        world.interactive.cross_unit_check = old_shape
        with world.open() as book:
            world.runner(book).run(max_iterations=200)
        self.assertIn("contract_held", {unit_row(world, key)["state"] for key in ("u1", "u2")})
        self.assertFalse(world.interactive.console_starts)

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
        self.assertEqual((found["verdict"], found["disagree"], found["disagreements_by_field"]), ("disagree", 1, {"ion_mode": 1}))
        # The items are Interactive's, copied: nested per field, with the result and its reasons.
        first = found["files"][0]
        self.assertEqual((first["ion_mode"]["triage"], first["ion_mode"]["header"], first["ion_mode"]["result"]),
                         ("positive", "Negative", "disagree"))
        self.assertEqual(first["reasons"], ["polarity"])
        self.assertFalse((self.root / "triage" / "ACC1" / curated.REVIEW_LOG).exists(), "no header verdict in the review log")
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
        self.assertTrue(sent["curated_selection_path"].endswith(str(Path("handoffs") / "u1-pos" / "u1.json")))
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
        self.assertFalse(re.search(r'"(?:[A-Za-z]:[\\/]|\\\\\\\\)', target.read_text(encoding="utf-8")),
                         "no drive-letter or UNC path in the released ledger")
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


# ---- the second review of PR #60 (2026-11-16) -------------------------------------------------------------------------

def run_until(world: campaign_fakes.World, key: str, state: str, limit: int = 400) -> None:
    """One runner process that stops as soon as `key` reaches `state` (a runner that stops there and is started again)."""
    with world.open() as book:
        runner = world.runner(book)
        # The step of `state` is not made: the process ends with the unit there.
        setattr(runner, "_state_" + state, lambda _unit: False)
        runner.reconcile()
        for _ in range(limit):
            if book.unit(key)["state"] == state:
                return
            if not runner.iterate():
                world.clock.sleep(31)
        raise AssertionError(f"{key} never reached {state}: {book.unit(key)['state']}")


def request(world: campaign_fakes.World, action: str, key: str, reason: str, by: str) -> None:
    with world.open() as book:
        book.add_request(action, key, reason, by, world.runner(book).stamp())


class ApprovedSelectionIsDurableTests(_Root):
    """Whether a unit runs a curated selection is what the approval recorded in the ledger, not what a later read of
    campaign-manifest.json happens to give (finding 1 of the second review)."""

    def test_a_manifest_changed_after_the_handoff_never_lets_the_unit_download_as_a_plain_one(self) -> None:
        world = curated_world(self.root, {"u1": {"workspace": "u1@c2"}})
        run_until(world, "u1", "handoff_ready")
        Path(world.campaign["manifest_path"]).write_text('{"units": []}', encoding="utf-8")
        world.run()
        unit = unit_row(world, "u1")
        self.assertEqual((unit["state"], unit["terminal_reason"]), ("stopped_policy_drift", "manifest_digest_changed"))
        self.assertEqual(world.interactive.download_starts, [], "never downloaded without its selection")
        self.assertEqual(world.interactive.curated_downloads, [])

    def test_a_manifest_changed_after_the_download_holds_the_unit_with_its_raw_data(self) -> None:
        world = curated_world(self.root, {"u1": {}})
        run_until(world, "u1", "downloaded")
        approved = Path(world.campaign["manifest_path"]).read_bytes()
        Path(world.campaign["manifest_path"]).write_text('{"units": []}', encoding="utf-8")
        with world.open() as book:
            world.runner(book).run(max_iterations=60)
        self.assertEqual(unit_row(world, "u1")["state"], "contract_held")
        self.assertEqual(events(world, "contract_held")[-1]["detail"]["contract"], "manifest_digest_changed")
        self.assertFalse([call for call in world.interactive.calls if call[0] == "preflight"])
        # The approved bytes back, and the recheck runs the unit on its selection.
        Path(world.campaign["manifest_path"]).write_bytes(approved)
        request(world, "recheck_held", "u1", "the approved manifest is restored", "Test Person")
        world.run()
        self.assertEqual(unit_row(world, "u1")["state"], "done")

    def test_an_edited_manifest_naming_the_same_triage_is_not_the_approved_one(self) -> None:
        # The same triage sha256, another workspace: only the digest tells it from the approved manifest.
        world = curated_world(self.root, {"u1": {}})
        path = Path(world.campaign["manifest_path"])
        edited = json.loads(path.read_text(encoding="utf-8"))
        edited["units"][0]["workspace_name"] = "u1-elsewhere"
        path.write_bytes(policy.canonical_json(edited))
        world.run()
        unit = unit_row(world, "u1")
        self.assertEqual((unit["state"], unit["terminal_reason"]), ("stopped_policy_drift", "manifest_digest_changed"))
        self.assertEqual(world.interactive.download_starts, [])

    def test_a_manifest_that_cannot_be_read_for_a_moment_is_read_again(self) -> None:
        world = curated_world(self.root, {"u1": {}})
        failures = {"left": 2}
        with world.open() as book:
            runner = world.runner(book)
            original = runner._read_manifest_bytes

            def flaky(path: Path) -> bytes:
                if failures["left"]:
                    failures["left"] -= 1
                    raise PermissionError(13, "The process cannot access the file because it is being used by another process")
                return original(path)

            runner._read_manifest_bytes = flaky
            runner.run(until_idle=True, max_iterations=2000)
        self.assertEqual(unit_row(world, "u1")["state"], "done")
        self.assertEqual(failures["left"], 0)
        self.assertTrue(world.interactive.curated_downloads[0].get("curated_selection_path"))
        with world.open() as book:
            waits = [json.loads(row["detail_json"]) for row in book.transitions("u1") if row["to_state"] == "waiting_retry"]
        self.assertTrue(any("manifest_unreadable" in item for item in waits))

    def test_the_approval_records_each_curated_unit_and_its_workspace(self) -> None:
        world = curated_world(self.root, {"u1-pos": {"catalog": "u1", "child": "pos", "workspace": "u1-pos@c2"}})
        recorded = events(world, "triage_recorded")
        self.assertEqual([(item["unit_key"], item["detail"]["workspace_name"]) for item in recorded], [("u1-pos", "u1-pos@c2")])


class LateSiblingTests(_Root):
    """A unit that reaches the cross-unit check after its group was judged is checked after the units that already
    passed it (finding 2 of the second review)."""

    def world(self) -> campaign_fakes.World:
        world = curated_world(self.root, {"u1": {"folder": "shared"}, "u2": {"folder": "shared"}}, groups={"g": ["u1", "u2"]})
        world.scripts["u2"] = campaign_fakes.UnitScript(downloads=["fail"] * 10)
        world.run()
        self.assertEqual([unit_row(world, key)["state"] for key in ("u1", "u2")], ["done", "failed"])
        world.scripts["u2"] = campaign_fakes.UnitScript(downloads=["ok"])
        return world

    def test_a_retried_sibling_with_the_same_inputs_is_refused_and_the_unit_that_ran_stands(self) -> None:
        world = self.world()
        request(world, "retry", "u2", "network fixed", "operator")
        world.run()
        u2 = unit_row(world, "u2")
        self.assertEqual((u2["state"], u2["terminal_reason"], u2["failures"]), ("failed", policy.ATTRIBUTION_REFUSED, 0))
        self.assertEqual(json.loads(u2["terminal_detail"])["reason_code"], "cross_unit_duplicate_inputs")
        self.assertEqual(unit_row(world, "u1")["state"], "done")
        self.assertNotIn(("u2", "run"), world.interactive.console_starts)
        last = world.interactive.cross_unit_calls[-1]
        self.assertEqual([world.interactive._unit_of_manifest(path) for path in last], ["u1", "u2"],
                         "the unit that already ran is first")

    def test_a_verdict_against_a_unit_that_already_ran_refuses_the_newcomer(self) -> None:
        world = self.world()
        world.interactive.cross_unit_refusals = {"u1": ("cross_unit_polarity_contradiction", "P")}
        request(world, "retry", "u2", "network fixed", "operator")
        world.run()
        u2 = unit_row(world, "u2")
        self.assertEqual((u2["state"], u2["terminal_reason"]), ("failed", policy.ATTRIBUTION_REFUSED))
        self.assertEqual(json.loads(u2["terminal_detail"])["reason_code"], "cross_unit_polarity_contradiction")
        self.assertNotIn(("u2", "run"), world.interactive.console_starts)
        self.assertEqual(events(world, "cross_unit_verdict")[-1]["detail"]["refused"]["u2"]["with"], "u1")


class ReconciliationAndPersonTests(_Root):
    def test_a_curated_unit_without_a_header_reconciliation_is_held_unrun(self) -> None:
        world = curated_world(self.root, {"u1": {}})
        world.scripts["u1"] = campaign_fakes.UnitScript(reconciliation="none")
        with world.open() as book:
            world.runner(book).run(max_iterations=200)
        self.assertEqual(unit_row(world, "u1")["state"], "contract_held")
        self.assertEqual(events(world, "contract_held")[-1]["detail"]["contract"], "curated_selection_reconciliation_not_recorded")
        self.assertFalse(world.interactive.console_starts)

    def test_a_persons_answer_to_a_header_hold_reaches_the_ledger_and_the_review_log(self) -> None:
        world = curated_world(self.root, {"u1": {}})
        world.scripts["u1"] = campaign_fakes.UnitScript(reconciliation="disagree")
        world.run()
        self.assertEqual(unit_row(world, "u1")["state"], "disposition_held")
        with world.open() as book:
            book.add_request("skip", "u1", "the header is right, the triage polarity is wrong", "Test Person",
                             world.runner(book).stamp(), question_code="triage_header_disagreement")
        world.run()
        self.assertEqual(unit_row(world, "u1")["state"], "skipped")
        target = world.directory / curated.DECISION_LEDGER
        with world.open() as book:
            curated.export_decision_ledger(target, book.events())
        lines = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
        answers = [line for line in lines if line["kind"] == "person_answer"]
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["standing"], curated.PERSON_ANSWER)
        self.assertEqual({key: answers[0]["detail"][key] for key in ("by", "question_code", "answer", "channel", "action",
                                                                     "state_before", "state_after")},
                         {"by": "Test Person", "question_code": "triage_header_disagreement",
                          "answer": "the header is right, the triage polarity is wrong", "channel": "conversation",
                          "action": "skip", "state_before": "disposition_held", "state_after": "discarding"})
        self.assertEqual(curated.verify_decision_ledger(target), [])
        log = [json.loads(line) for line in (self.root / "triage" / "ACC1" / curated.REVIEW_LOG).read_text(encoding="utf-8").splitlines()]
        # Only the kinds and fields section 2.4 allows: no header_reconciliation, and a person_answer of four fields.
        self.assertEqual([entry["kind"] for entry in log], ["person_answer"])
        self.assertEqual(set(log[0]) - {"seq", "at", "kind", "decision_id", "prev_sha256"},
                         {"by", "question_code", "answer", "channel"})
        self.assertEqual((log[0]["by"], log[0]["question_code"]), ("Test Person", "triage_header_disagreement"))

    def test_a_request_without_a_question_code_is_the_operators_not_a_persons_answer(self) -> None:
        world = curated_world(self.root, {"u1": {}})
        world.scripts["u1"] = campaign_fakes.UnitScript(reconciliation="disagree")
        world.run()
        request(world, "skip", "u1", "cleanup", "")
        world.run()
        self.assertEqual(unit_row(world, "u1")["state"], "skipped")
        self.assertEqual(events(world, "person_answer"), [])
        self.assertEqual(events(world, "operator_request")[0]["detail"]["action"], "skip")
        self.assertNotIn("operator_request", curated.DECISION_KINDS)
        self.assertFalse((self.root / "triage" / "ACC1" / curated.REVIEW_LOG).exists())
        with world.open() as book, self.assertRaises(campaign_fakes.ledger_module.LedgerError):
            book.add_request("skip", "u1", "an answer", "", world.runner(book).stamp(), question_code="triage_header_disagreement")
        with self.assertRaises(curated.TriageError):
            curated.append_review_log(self.root, "header_reconciliation", "a" * 64, "2026-11-16T00:00:00+00:00", verdict="agree")
        with self.assertRaises(curated.TriageError):
            curated.append_review_log(self.root, "person_answer", "a" * 64, "2026-11-16T00:00:00+00:00", by="", question_code="q",
                                      answer="a", channel="conversation")


class DecisionRecordTests(_Root):
    def test_a_triage_with_its_decision_record_runs_and_the_record_is_checked(self) -> None:
        world = curated_world(self.root, {"u1": {"record": True}})
        world.run()
        self.assertEqual(unit_row(world, "u1")["state"], "done")
        checked = events(world, "triage_checked")[0]["detail"]
        self.assertTrue(checked["record_copy_path"].endswith("u1/u1.record.json"))
        sent = world.interactive.curated_downloads[0]
        # WS0 decision 2: the original record, beside the inputs\ it binds, named explicitly.
        self.assertEqual(Path(sent["decision_record_path"]), self.root / "triage" / "ACC1" / "u1.record.json")
        applied = events(world, "curated_selection_applied")[0]["detail"]
        self.assertTrue(applied["selection_sha256_equal"])

    def test_a_selection_sha256_that_differs_from_the_plan_holds_the_unit(self) -> None:
        world = curated_world(self.root, {"u1": {"record": True}})
        world.interactive.curated_recorded_selection = "e" * 64
        with world.open() as book:
            world.runner(book).run(max_iterations=60)
        self.assertEqual(unit_row(world, "u1")["state"], "contract_held")
        self.assertEqual(events(world, "contract_held")[-1]["detail"]["contract"], "curated_selection_selection_mismatch")
        applied = events(world, "curated_selection_applied")[0]["detail"]
        self.assertFalse(applied["selection_sha256_equal"])

    def test_a_decision_record_changed_after_approval_stops_the_unit(self) -> None:
        world = curated_world(self.root, {"u1": {"record": True}})
        (self.root / "triage" / "ACC1" / "u1.record.json").write_text('{"model": "another-model"}', encoding="utf-8")
        world.run()
        unit = unit_row(world, "u1")
        self.assertEqual((unit["state"], unit["terminal_reason"]), ("stopped_policy_drift", "triage_record_digest_changed"))
        self.assertEqual(world.interactive.download_starts, [])

    def test_a_lease_that_recorded_another_triage_holds_the_unit(self) -> None:
        world = curated_world(self.root, {"u1": {}})
        world.interactive.curated_recorded_sha = "f" * 64
        with world.open() as book:
            world.runner(book).run(max_iterations=60)
        self.assertEqual(unit_row(world, "u1")["state"], "contract_held")
        self.assertEqual(events(world, "contract_held")[0]["detail"]["contract"], "curated_selection_not_recorded")


class PlanContrastTests(_Root):
    def test_a_unit_whose_contrast_loses_a_side_is_not_planned(self) -> None:
        triage_root = write_triage(self.root)
        docs = te.build_good(ACCESSION)
        remaining = [{"unit_id": key, "accession": ACCESSION, "repository": "metabolomics_workbench"} for key in UNITS]
        decisions = {key: {"kind": "abstention", "assignments": [[row["sample_id"], "All"] for row in docs[key]["rows"]]}
                     for key in UNITS}
        # AN004454's Class contrasts its kept samples with samples the triage dropped: the cut leaves one side.
        decisions["AN004454"] = {"kind": "proposal", "assignments": [[row["sample_id"], "Treated"] for row in docs["AN004454"]["rows"]]
                                 + [["dropped-sample-1", "Control"], ["dropped-sample-2", "Control"]]}
        excluded: list[dict] = []
        kept, _entries, record = plan._plan_from_triage(triage_root, remaining, decisions, excluded, lambda _message: None)
        self.assertEqual(sorted(unit["unit_id"] for unit in kept), ["AN004455", "AN004456", "AN004457"])
        reasons = {item["unit_id"]: item for item in excluded}
        self.assertEqual(reasons["AN004454"]["reason"], curated.CLASS_CONTRAST_LOST)
        self.assertIn("Control", reasons["AN004454"]["detail"])
        self.assertEqual(record["accessions"][ACCESSION]["verdict"], "pass")


class PlanSourceTests(unittest.TestCase):
    def test_plan_compiles_with_warnings_as_errors(self) -> None:
        done = subprocess.run([sys.executable, "-I", "-W", "error", "-c",
                               "import pathlib, sys; compile(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'), sys.argv[1], 'exec')",
                               str(SCRIPTS / "campaign" / "plan.py")], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)


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
        triage_root = write_triage(self.root, aligned_docs()[0])
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
        digest = world.digest  # the World records plan.decision_events at its approval, as approve does
        world.run()
        self.assertEqual({unit_row(world, key)["state"] for key in keys}, {"done"})
        self.assertEqual(len(world.interactive.cross_unit_calls), 1)
        self.assertEqual(len(world.interactive.cross_unit_calls[0]), 4)
        self.assertEqual({item["curated_selection_path"].rsplit("handoffs", 1)[1][1:].split("\\")[0].split("/")[0]
                          for item in world.interactive.curated_downloads}, set(keys))
        checked = [item["detail"] for item in events(world, "triage_checked")]
        self.assertEqual({item["handoff_identity"] for item in checked}, {"handoff_identical"})
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
        docs = aligned_docs()[0]
        docs["AN004457"]["rows"] = docs["AN004457"]["rows"][:-1]
        te.write({"AN004457": docs["AN004457"]}, bundle / "triage" / ACCESSION)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(replay_cli.main(["--bundle", str(bundle), "--workspace-root", str(self.root / "w"), "--level", "selection",
                                              "--out", str(self.root / "r2.json")]), 1)
            shutil.copyfile(triage_root / ACCESSION / "AN004457.json", bundle / "triage" / ACCESSION / "AN004457.json")
            self.assertEqual(replay_cli.main(["--bundle", str(bundle), "--workspace-root", str(self.root / "w"), "--level", "inputs",
                                              "--out", str(self.root / "r3.json")]), 2)
        # The release bundle of section 5.3 (WS0 decision 9): release projections, the triage and the ledger, replayed
        # as a third party would, with no absolute path in what is released.
        release = self.root / "release"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = runner_cli.main(["--workspace-root", str(world.workspace_root), "release", "--campaign", "test-campaign",
                                    "--out", str(release)])
        self.assertEqual(code, runner_cli.EXIT_OK, err.getvalue())
        index = json.loads((release / "release-index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["approved_manifest_digest"], digest)
        self.assertEqual(index["manifests"][0]["original_sha256"], digest)
        released = (release / "campaign-manifest.release.json").read_text(encoding="utf-8")
        self.assertFalse(re.search(r'"(?:[A-Za-z]:[\\/]|\\\\\\\\)', released), "the release projection holds no absolute path")
        report = replay_cli.replay(release, self.root / "w", "selection", out=self.root / "r4.json")
        self.assertEqual((report["bundle"], report["result"]), ("release", replay_cli.EQUAL), json.dumps(report, indent=1))
        report = replay_cli.replay(release, self.root / "w", "inputs", out=self.root / "r5.json")
        self.assertEqual(report["result"], replay_cli.NOT_EVALUABLE)
        tampered = json.loads(released)
        tampered["units"][0]["curated_selection"]["selection_sha256"] = "0" * 64
        (release / "campaign-manifest.release.json").write_bytes(curated.canonical_json(tampered))
        report = replay_cli.replay(release, self.root / "w", "selection", out=self.root / "r6.json")
        self.assertEqual(report["result"], replay_cli.DIFFERENT)
        campaign = [row for row in report["units"] if row["unit_key"] == "(campaign)"][0]
        self.assertEqual({item["check"]: item["result"] for item in campaign["checks"]}["release_projection_sha256"],
                         replay_cli.DIFFERENT)

    def test_records_and_revisions_are_planned_and_replayed_with_their_review_log(self) -> None:
        triage_root = write_triage(self.root)
        folder = triage_root / ACCESSION
        record = folder / "AN004454.record.json"
        payload = folder / "AN004454.json"
        listing = folder / "inputs" / "archive-members.tsv"
        record.write_text(json.dumps({
            "schema": "msdial-llm-decision-record.v1", "decision_id": hashlib.sha256(payload.read_bytes()).hexdigest(),
            "kind": "unit_triage", "accession": ACCESSION, "unit_id": "AN004454",
            "payload": {"path": payload.name, "schema": "msdial-unit-triage.v1", "sha256": hashlib.sha256(payload.read_bytes()).hexdigest()},
            "agent": {"harness": "synthetic-harness", "model_id": "synthetic-model"},
            "inputs": [{"role": "archive_listing", "path": "inputs/archive-members.tsv",
                        "sha256": hashlib.sha256(listing.read_bytes()).hexdigest(), "bytes": listing.stat().st_size}]}),
            encoding="utf-8")
        revised = json.loads((folder / "AN004455.json").read_text(encoding="utf-8"))
        revised["notes"] = "revision 2: the same files, a reworded note"
        (folder / "AN004455.r2.json").write_text(json.dumps(revised), encoding="utf-8")
        curated.append_review_log(folder, "validator_verdict", "a" * 64, "2026-11-16T00:00:00+00:00", tool="validate-triage.py",
                                  code_commit="0" * 40, verdict="pass", findings_sha256="f" * 64, findings=[])
        head = curated.review_log_head(folder)
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
        units = {unit["unit_key"]: unit["curated_selection"] for unit in manifest["units"]}
        self.assertEqual(sorted(units), list(UNITS), json.dumps(manifest["exclusions"], indent=1))
        self.assertEqual(units["AN004454"]["triage_record_sha256"], hashlib.sha256(record.read_bytes()).hexdigest())
        # The record binds the listing, which gives AN004454's selection its crc32 and size; the others bind none.
        self.assertEqual(units["AN004454"]["selection_listings"][0]["path"], "inputs/archive-members.tsv")
        self.assertEqual(units["AN004456"]["selection_listings"], [])
        triage = json.loads(payload.read_text(encoding="utf-8"))
        bound = curated.record_inputs(record, hashlib.sha256(payload.read_bytes()).hexdigest())["listing"]
        self.assertEqual(units["AN004454"]["selection_sha256"], curated.selection_sha256(triage, bound))
        self.assertNotEqual(units["AN004454"]["selection_sha256"], curated.selection_sha256(triage, {}))
        self.assertEqual(units["AN004455"]["triage_relative_path"], f"{ACCESSION}/AN004455.r2.json")
        self.assertEqual(units["AN004456"]["review_log_head_sha256"], head)
        # The approval's entry follows the head the plan recorded.
        curated.append_review_log(folder, "digest_approval", units["AN004454"]["triage_sha256"], "2026-11-16T00:00:01+00:00",
                                  manifest_digest="sha256:" + "1" * 64, approval_id="A1", by="Synthetic Person")
        bundle = self.root / "bundle"
        bundle.mkdir()
        data = plan.canonical_bytes(manifest)
        (bundle / "campaign-manifest.json").write_bytes(data)
        (bundle / "campaign-manifest.summary.json").write_text(json.dumps({"manifest_digest": plan.digest_of(data)}), encoding="utf-8")
        shutil.copytree(triage_root, bundle / "triage")
        released = bundle / "triage" / ACCESSION

        def replayed() -> dict:
            return replay_cli.replay(bundle, self.root / "w", "selection", out=self.root / "r.json")

        def results(report: dict, unit: str) -> dict:
            return {item["check"]: item["result"] for row in report["units"] if row["unit_key"] == unit for item in row["checks"]}

        report = replayed()
        self.assertEqual(report["result"], replay_cli.EQUAL, json.dumps(report, indent=1))
        self.assertEqual(results(report, "AN004454")["triage_record_sha256"], replay_cli.EQUAL)
        self.assertEqual(results(report, "AN004454")["review_log_chain"], replay_cli.EQUAL)
        # A rationale-only edit changes no selection, and the replay still finds it.
        original = (released / "AN004456.json").read_bytes()
        edited = json.loads(original.decode("utf-8"))
        edited["notes"] = "an edited rationale"
        (released / "AN004456.json").write_text(json.dumps(edited), encoding="utf-8")
        report = replayed()
        self.assertEqual(report["result"], replay_cli.DIFFERENT)
        self.assertEqual({check for check, result in results(report, "AN004456").items() if result != replay_cli.EQUAL},
                         {"triage_sha256"})
        (released / "AN004456.json").write_bytes(original)
        # A decision record that names another model is not the one approved.
        kept = record.read_bytes()
        (released / "AN004454.record.json").write_text(json.dumps({"model": "another-model"}), encoding="utf-8")
        report = replayed()
        self.assertEqual(results(report, "AN004454")["triage_record_sha256"], replay_cli.DIFFERENT)
        (released / "AN004454.record.json").unlink()
        self.assertEqual(results(replayed(), "AN004454")["triage_record_sha256"], replay_cli.NOT_EVALUABLE)
        (released / "AN004454.record.json").write_bytes(kept)
        # A review log whose entry the plan recorded was rewritten breaks the chain.
        log = released / curated.REVIEW_LOG
        lines = log.read_bytes().split(b"\n")
        self.assertIn(b'"verdict":"pass"', lines[0])
        lines[0] = lines[0].replace(b'"verdict":"pass"', b'"verdict":"hold"')
        log.write_bytes(b"\n".join(lines))
        report = replayed()
        self.assertEqual(report["result"], replay_cli.DIFFERENT)
        self.assertEqual(results(report, "AN004456")["review_log_chain"], replay_cli.DIFFERENT)

    def test_a_whole_study_selection_that_reached_the_runner_is_refused_by_rule_d(self) -> None:
        """Defence in depth: the stage3b-style selection of ST002283 (every unit keeps the same rows) never passes the
        validator; were it planned anyway, the cross-unit check refuses every unit after the first."""
        docs = aligned_docs("ST002283", te.stage3b_style("ST002283"), with_rows=False)[0]
        triage_root = write_triage(self.root, docs, accession="ST002283")
        self.assertEqual(curated.run_validator(triage_root / "ST002283")["verdict"], "error")
        planned = []
        for unit_id, doc in docs.items():
            path = triage_root / "ST002283" / f"{unit_id}.json"
            planned.append({"unit_id": unit_id, "unit_key": unit_id, "accession": "ST002283", "workspace_name": unit_id,
                            "curated_selection": {"triage_path": str(path), "triage_relative_path": f"ST002283/{unit_id}.json",
                                                  "triage_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                                  "catalog_handoff_sha256": doc["catalog_handoff_sha256"],
                                                  "catalog_rows_sha256": curated.rows_sha256([]), "child_key": None,
                                                  "selection_sha256": curated.selection_sha256(doc, {})}})
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
