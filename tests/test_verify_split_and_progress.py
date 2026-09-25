"""Split units, repositories without checksums, stages not reached, and progress apart from verdict.

MetaboLights MTBLS2207 was the first unit split by raw-header acquisition mode into parts that
share their parent's raw tree. Run against those parts the gate called their raw data released,
turned the parent's checksum FAIL into an absence, and reported a unit nobody had run yet as two
records disagreeing. It also refused every MetaboLights unit outright, because that repository
publishes no checksums at all.

The trial evaluation that follows from it (user decision, 2026-09-25) reports how far a run got
separately from whether it was right, and derives the first from the artifacts on disk.

The second half of this file is the adversarial review of that change: every case below where a
reviewer showed the first version reporting success, or weakening a refusal, is pinned here.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _ROOT / "scripts" / "verify-run-invariants.py"
_SPEC = importlib.util.spec_from_file_location("verify_run_invariants_split", _MODULE_PATH)
assert _SPEC and _SPEC.loader
verifier = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_run_invariants_split"] = verifier
_SPEC.loader.exec_module(verifier)


def _status(report, check_id: str) -> str:
    matching = [check for check in report.checks if check.check_id == check_id]
    assert matching, f"{check_id} was not evaluated at all"
    assert len(matching) == 1, f"{check_id} ran {len(matching)} times; each check must run once"
    return matching[0].status


def _check(report, check_id: str):
    return next(check for check in report.checks if check.check_id == check_id)


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _edit(path: Path, **changes) -> None:
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(changes)
    path.write_text(json.dumps(record), encoding="utf-8")


def _mztab(path: Path, *, malformed: bool = False) -> None:
    lines = [f"MTD\tms_run[{i + 1}]-location\tfile://x" for i in range(2)]
    lines.append("SEH\tSME_ID\tevidence_input_id\tchemical_name\trank")
    for i in range(3):
        row = "\t".join(["SME", str(i + 1), str(i + 1), "Compound", "1"])
        lines.append(row + ("\t" if malformed else ""))
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


class SplitFixture:
    """A MetaboLights parent that downloaded three files, split into parts holding two and one."""

    def __init__(self, root: Path, *, declared: str = "", hashed: bool = True,
                 repository: str = "metabolights",
                 part_inputs: tuple[list[int], list[int]] = ([0, 1], [2])) -> None:
        self.root = root
        self.parent_root = root / "unit"
        data = self.parent_root / "raw" / "data"
        data.mkdir(parents=True)
        self.inputs = []
        for index in range(3):
            path = data / f"s{index}.mzML"
            path.write_bytes(b"x")
            self.inputs.append(str(path))
        self.parent_manifest = self.parent_root / "provenance" / "run-manifest.json"
        parts = []
        self.part_roots = []
        for suffix, indices in zip(("dda", "dia"), part_inputs):
            part_root = root / f"unit-{suffix}"
            (part_root / "output").mkdir(parents=True)
            self.part_roots.append(part_root)
            part_inputs_paths = [self.inputs[i] for i in indices]
            parts.append({"analysis_unit_id": f"unit-{suffix}", "input_candidates": part_inputs_paths})
            _write(part_root / "provenance" / "run-manifest.json", {
                "project": {"analysis_unit_id": f"unit-{suffix}"},
                "execution_allowed": True,
                "input_candidates": part_inputs_paths,
                "split_from": {"manifest_path": str(self.parent_manifest), "analysis_unit_id": "unit"},
                "raw_directory": str(self.parent_root / "raw"),
                "raw_owned_by": str(self.parent_manifest),
                "raw_retention_policy": "keep",
            })
        _write(self.parent_manifest, {
            "project": {
                "analysis_unit_id": "unit",
                "repository": repository,
                "files": [{"name": f"FILES/s{i}.mzML", "checksum": declared if i == 0 else ""}
                          for i in range(3)],
            },
            "status": "split_by_acquisition",
            "execution_allowed": False,
            "input_candidates": list(self.inputs),
            "raw_directory": str(self.parent_root / "raw"),
            "downloads": [{"path": item, "sha256": "ab" * 32 if hashed else "", "declared_checksum": ""}
                          for item in self.inputs],
            "allowlist_checksum_validation": {"required": bool(declared), "verified": 1 if declared else 0,
                                              "skipped": 2 if declared else 3},
            "split_into": parts,
            "raw_retention_policy": "keep",
        })

    def part_manifest(self, part: int) -> Path:
        return self.part_roots[part] / "provenance" / "run-manifest.json"

    def gate(self, part: int, stage: str = "before-production"):
        return verifier.verify(self.part_roots[part], stage)


def _prepared(temporary: str) -> tuple[SplitFixture, Path]:
    """A split part with its production bundle written and nothing run."""
    fixture = SplitFixture(Path(temporary))
    workspace = fixture.part_roots[0]
    output = workspace / "output"
    (output / "analysis_files.csv").write_text(
        "file_path,file_name,file_type,class_id,acquisition_type,batch_order,analytical_order,factor\n"
        + "".join(f"{path},s{i},Sample,A,DDA,1,{i + 1},1\n" for i, path in enumerate(fixture.inputs[:2])),
        encoding="ascii",
    )
    (output / "method.txt").write_text("Minimum peak height: 0\n", encoding="ascii")
    _write(output / "run-manifest.json", {
        "source_files": fixture.inputs[:2],
        "expected_analysis_exports": [str(output / f"s{i}.mdpeak") for i in range(2)],
    })
    return fixture, workspace


# ---------------------------------------------------------------------------------------------
# SUM-1 and SUM-2: decision A
# ---------------------------------------------------------------------------------------------

class ChecksumPolicyTests(unittest.TestCase):
    def test_a_repository_that_publishes_no_checksum_is_a_warning_not_a_refusal(self) -> None:
        """The user's decision of 2026-09-25: MetaboLights units proceed on the download sha256."""
        with tempfile.TemporaryDirectory() as temporary:
            report = SplitFixture(Path(temporary)).gate(0)

        check = _check(report, "SUM-1")
        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("No artifact may describe these inputs as checksum-verified", check.detail)
        self.assertEqual("unit", check.evidence["inherited_from"])
        self.assertEqual(0, check.evidence["inputs_without_download_sha256"])

    def test_a_partial_declaration_is_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = SplitFixture(Path(temporary), declared="cd" * 32).gate(0)

        self.assertEqual(verifier.FAIL, _status(report, "SUM-1"))

    def test_no_declared_checksum_and_no_download_hash_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = SplitFixture(Path(temporary), hashed=False).gate(0)

        self.assertEqual(verifier.FAIL, _status(report, "SUM-1"))

    def test_an_empty_record_from_a_repository_that_publishes_checksums_is_a_lost_declaration(self) -> None:
        """Only a repository recorded as publishing none earns the WARN; elsewhere it was lost."""
        with tempfile.TemporaryDirectory() as temporary:
            report = SplitFixture(Path(temporary), repository="metabobank").gate(0)

        check = _check(report, "SUM-1")
        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("lost on the way in", check.detail)

    def test_a_checksum_declared_at_download_is_not_waved_through(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SplitFixture(Path(temporary))
            record = json.loads(fixture.parent_manifest.read_text(encoding="utf-8"))
            record["downloads"][0]["declared_checksum"] = "cd" * 16
            fixture.parent_manifest.write_text(json.dumps(record), encoding="utf-8")
            report = fixture.gate(0)

        self.assertEqual(verifier.FAIL, _status(report, "SUM-1"))

    def test_an_input_without_a_download_hash_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SplitFixture(Path(temporary))
            record = json.loads(fixture.parent_manifest.read_text(encoding="utf-8"))
            record["downloads"] = record["downloads"][:2]
            fixture.parent_manifest.write_text(json.dumps(record), encoding="utf-8")
            report = fixture.gate(0)

        check = _check(report, "SUM-1")
        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual(1, check.evidence["inputs_without_download_sha256"])

    def test_a_validation_record_without_counts_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SplitFixture(Path(temporary))
            _edit(fixture.parent_manifest, allowlist_checksum_validation={})
            report = fixture.gate(0)

        self.assertEqual(verifier.FAIL, _status(report, "SUM-1"))

    def test_every_declared_file_verified_passes_even_with_sidecars(self) -> None:
        """MTBKS236: 74 declared md5s, 37 of them .wiff.scan sidecars that are not inputs."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "wiff"
            (root / "output").mkdir(parents=True)
            _write(root / "provenance" / "run-manifest.json", {
                "project": {"analysis_unit_id": "wiff", "repository": "metabobank", "files": [
                    {"name": name, "checksum": "ab" * 16}
                    for name in ("a.wiff", "a.wiff.scan", "b.wiff", "b.wiff.scan")]},
                "input_candidates": ["a.wiff", "b.wiff"],
                "allowlist_checksum_validation": {"required": True, "verified": 4, "skipped": 0},
            })
            report = verifier.verify(root, "before-production")

        self.assertEqual(verifier.PASS, _status(report, "SUM-1"))

    def test_the_evidence_keeps_what_the_validation_record_said(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = SplitFixture(Path(temporary), declared="cd" * 32).gate(0)

        self.assertIn("any_checksum_verified", _check(report, "SUM-1").evidence)

    def test_a_published_checksum_claim_is_refused_when_nothing_was_compared(self) -> None:
        """Decision A's second clause, where the claim would be made."""
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SplitFixture(Path(temporary))
            output = fixture.part_roots[0] / "output"
            (output / "MS_DIAL_Materials_and_Methods.txt").write_text(
                "All raw files were checksum-verified before processing.", encoding="utf-8")
            report = fixture.gate(0, "before-publish")

        self.assertEqual(verifier.FAIL, _status(report, "SUM-2"))

    def test_publication_without_the_claim_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SplitFixture(Path(temporary))
            output = fixture.part_roots[0] / "output"
            (output / "MS_DIAL_Materials_and_Methods.txt").write_text(
                "Raw files were downloaded from MetaboLights and recorded by sha256.", encoding="utf-8")
            report = fixture.gate(0, "before-publish")

        self.assertEqual(verifier.PASS, _status(report, "SUM-2"))


# ---------------------------------------------------------------------------------------------
# SPL-1, ELIG-1, RET-1, DSK-1: split runs
# ---------------------------------------------------------------------------------------------

class SplitPartTests(unittest.TestCase):
    def test_parts_that_partition_their_parent_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = SplitFixture(Path(temporary)).gate(1)

        self.assertEqual(verifier.PASS, _status(report, "SPL-1"))

    def test_a_file_in_two_parts_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = SplitFixture(Path(temporary), part_inputs=([0, 1], [1, 2])).gate(0)

        self.assertEqual(verifier.FAIL, _status(report, "SPL-1"))

    def test_a_file_in_no_part_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = SplitFixture(Path(temporary), part_inputs=([0], [2])).gate(0)

        self.assertEqual(verifier.FAIL, _status(report, "SPL-1"))

    def test_the_same_files_written_differently_are_the_same_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SplitFixture(Path(temporary))
            _edit(fixture.part_manifest(0), input_candidates=[
                item.replace("\\", "/").upper() if sys.platform == "win32" else item
                for item in fixture.inputs[:2]
            ])
            report = fixture.gate(0)

        self.assertEqual(verifier.PASS, _status(report, "SPL-1"))

    def test_a_part_claiming_another_units_raw_tree_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SplitFixture(Path(temporary))
            elsewhere = Path(temporary) / "other" / "provenance" / "run-manifest.json"
            _edit(fixture.part_manifest(0), raw_owned_by=str(elsewhere),
                  raw_directory=str(Path(temporary) / "other" / "raw"))
            report = fixture.gate(0)

        self.assertEqual(verifier.FAIL, _status(report, "SPL-1"))

    def test_a_split_record_without_a_parent_path_is_not_judged_on_the_part_itself(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SplitFixture(Path(temporary))
            _edit(fixture.part_manifest(0), split_from={"analysis_unit_id": "unit"})
            report = fixture.gate(0)

        for check_id in ("SPL-1", "SUM-1"):
            check = _check(report, check_id)
            self.assertEqual(verifier.NOT_EVALUABLE, check.status, check_id)
            self.assertIn("no manifest_path", check.detail)

    def test_a_parent_manifest_that_is_not_an_object_does_not_crash_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SplitFixture(Path(temporary))
            fixture.parent_manifest.write_text("[]", encoding="utf-8")
            report = fixture.gate(0, "all")

        self.assertEqual(verifier.NOT_EVALUABLE, _status(report, "SPL-1"))
        self.assertEqual(verifier.NOT_EVALUABLE, _status(report, "SUM-1"))

    def test_an_unsplit_unit_owes_no_split_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plain"
            (root / "output").mkdir(parents=True)
            _write(root / "provenance" / "run-manifest.json", {"project": {"analysis_unit_id": "plain"}})
            report = verifier.verify(root, "before-production")

        check = _check(report, "SPL-1")
        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertFalse(check.required)

    def test_a_part_reads_its_raw_tree_and_policy_at_the_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SplitFixture(Path(temporary))
            report = fixture.gate(0, "before-publish")
            self.assertEqual(verifier.PASS, _status(report, "RET-1"))
            _edit(fixture.parent_manifest, raw_retention_policy="nonsense")
            changed = fixture.gate(0, "before-publish")

        dsk = _check(report, "DSK-1")
        self.assertEqual(verifier.NOT_EVALUABLE, dsk.status)
        self.assertFalse(dsk.required, "a part's storage is accounted once, at its owner")
        self.assertEqual(verifier.FAIL, _status(changed, "RET-1"),
                         "the owner's policy governs the owner's tree, not the part's copy of it")

    def test_the_parent_is_named_as_a_raw_owner_not_a_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SplitFixture(Path(temporary))
            report = verifier.verify(fixture.parent_root, "before-production")

        check = _check(report, "ELIG-1")
        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("Gate the parts instead", check.detail)

    def test_a_split_parent_is_refused_even_if_marked_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SplitFixture(Path(temporary))
            _edit(fixture.parent_manifest, execution_allowed=True)
            report = verifier.verify(fixture.parent_root, "before-production")

        self.assertEqual(verifier.FAIL, _status(report, "ELIG-1"))


# ---------------------------------------------------------------------------------------------
# stages not reached, and runs that were attempted
# ---------------------------------------------------------------------------------------------

class StageNotReachedTests(unittest.TestCase):
    def test_after_run_checks_on_an_unrun_unit_are_not_reached(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _fixture, workspace = _prepared(temporary)
            report = verifier.verify(workspace, "after-run")

        for check_id in ("CNT-1", "EXP-1", "MTH-1"):
            check = _check(report, check_id)
            self.assertEqual(verifier.NOT_EVALUABLE, check.status, check_id)
            self.assertTrue(check.required, f"{check_id} must still refuse under --strict")
        self.assertIn("the manifest records no attempt", _check(report, "EXP-1").detail)

    def test_strict_still_refuses_an_unrun_unit_after_the_run_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _fixture, workspace = _prepared(temporary)
            code = verifier.main([str(workspace), "--stage", "after-run", "--strict"])

        self.assertEqual(4, code)

    def test_a_run_that_skipped_files_is_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _fixture, workspace = _prepared(temporary)
            (workspace / "output" / "s0.mdpeak").write_text("Peak ID\n", encoding="ascii")
            report = verifier.verify(workspace, "after-run")

        self.assertEqual(verifier.FAIL, _status(report, "EXP-1"))
        self.assertEqual(verifier.FAIL, _status(report, "CNT-1"))

    def test_a_recorded_crash_that_wrote_nothing_is_a_failed_run(self) -> None:
        """The Console dies before its first export; the server records run_failed."""
        with tempfile.TemporaryDirectory() as temporary:
            _fixture, workspace = _prepared(temporary)
            _edit(workspace / "provenance" / "run-manifest.json", status="run_failed",
                  run_failures=[{"exit_code": 1}])
            after_run = verifier.verify(workspace, "after-run")
            publish = verifier.verify(workspace, "before-publish")

        self.assertEqual(verifier.FAIL, _status(after_run, "EXP-1"))
        self.assertEqual(verifier.FAIL, _status(after_run, "CNT-1"))
        self.assertEqual(verifier.FAIL, _status(publish, "FIN-1"))

    def test_a_finalisation_that_failed_validation_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _fixture, workspace = _prepared(temporary)
            _edit(workspace / "provenance" / "run-manifest.json", status="validation_failed",
                  finalized_at="2026-09-25T10:00:00+09:00", mztab_validation={"summary": {"failed": 1}})
            report = verifier.verify(workspace, "before-publish")

        self.assertEqual(verifier.FAIL, _status(report, "FIN-1"))

    def test_finalisation_is_not_reached_when_nothing_ran(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _fixture, workspace = _prepared(temporary)
            report = verifier.verify(workspace, "before-publish")

        check = _check(report, "FIN-1")
        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertTrue(check.required)

    def test_every_terminal_status_counts_as_finalised(self) -> None:
        for status in ("mztab_validated", "cleanup_pending_confirmation", "raw_cleaned", "completed"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                _fixture, workspace = _prepared(temporary)
                (workspace / "output" / "s0.mdpeak").write_text("Peak ID\n", encoding="ascii")
                _edit(workspace / "provenance" / "run-manifest.json", status=status,
                      finalized_at="2026-09-25T10:00:00+09:00", mztab_validation={"summary": {"failed": 0}})
                report = verifier.verify(workspace, "before-publish")
                self.assertEqual(verifier.PASS, _status(report, "FIN-1"))


# ---------------------------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------------------------

class ProgressTests(unittest.TestCase):
    def _diagnosed(self, temporary: str) -> Path:
        _fixture, workspace = _prepared(temporary)
        diagnostics = workspace / "diagnostics" / "job1"
        diagnostics.mkdir(parents=True)
        manifest = workspace / "provenance" / "run-manifest.json"
        record = json.loads(manifest.read_text(encoding="utf-8"))
        record["project"]["class_proposal"] = {"status": "accepted", "assignments": []}
        record["peak_height_diagnostics"] = [{"diagnostic_run_directory": str(diagnostics)}]
        manifest.write_text(json.dumps(record), encoding="utf-8")
        return workspace

    def _finished(self, temporary: str, **manifest) -> Path:
        workspace = self._diagnosed(temporary)
        output = workspace / "output"
        for i in range(2):
            (output / f"s{i}.mdpeak").write_text("Peak ID\n", encoding="ascii")
        _mztab(output / "AlignResult-1.mzTab")
        (output / "AlignResult-1.qa.tsv").write_text("metric\tvalue\n", encoding="ascii")
        for name in ("MS_DIAL_publication_report.json", "MS_DIAL_Materials_and_Methods.txt",
                     "Supplementary_Table_MS_DIAL.tsv"):
            (output / name).write_text("{}", encoding="ascii")
        values = {"status": "mztab_validated", "finalized_at": "2026-09-25T10:00:00+09:00",
                  "mztab_validation": {"summary": {"failed": 0}},
                  "retained_artifact_inventory": [], "raw_retention_policy": "keep"}
        values.update(manifest)
        _edit(workspace / "provenance" / "run-manifest.json", **values)
        return workspace

    def test_a_prepared_unit_reports_the_stage_it_reached_despite_a_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = verifier.verify(self._diagnosed(temporary), "all")

        self.assertFalse(report.ok and not report.strict_failures)
        self.assertEqual("B5 production_prepared", report.progress["stage_reached"])
        self.assertEqual("B6 production_run_done", report.progress["next_stage"])

    def test_a_finished_unit_reaches_the_last_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = verifier.verify(self._finished(temporary), "all")

        self.assertEqual("B10 retention_recorded", report.progress["stage_reached"])

    def test_a_failed_validation_is_not_reported_as_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._finished(temporary, status="validation_failed",
                                       mztab_validation={"summary": {"failed": 1}})
            report = verifier.verify(workspace, "all")

        self.assertEqual("B6 production_run_done", report.progress["stage_reached"])

    def test_a_rerun_that_failed_after_validating_is_not_reported_as_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._finished(temporary, status="run_failed", run_failures=[{"exit_code": 1}])
            report = verifier.verify(workspace, "all")

        self.assertEqual("B6 production_run_done", report.progress["stage_reached"])

    def test_released_raw_data_is_the_intended_end_not_a_lost_download(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._finished(temporary, status="raw_cleaned",
                                       raw_retention_policy="delete_after_validated_output")
            _edit(workspace / "provenance" / "run-manifest.json", raw_retention_policy="delete_after_validated_output")
            shutil.rmtree(Path(temporary) / "unit" / "raw")
            report = verifier.verify(workspace, "all")

        self.assertEqual("B10 retention_recorded", report.progress["stage_reached"])

    def test_a_run_by_a_console_without_the_key_record_still_ran(self) -> None:
        """MPST000007: thirty .mdpeak files and an mzTab-M from a Console older than the key record."""
        with tempfile.TemporaryDirectory() as temporary:
            report = verifier.verify(self._finished(temporary), "all")

        self.assertTrue(report.progress["stages"]["B6"])

    def test_a_diagnostic_record_without_a_directory_is_not_a_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._diagnosed(temporary)
            record = json.loads((workspace / "provenance" / "run-manifest.json").read_text(encoding="utf-8"))
            record["peak_height_diagnostics"][-1]["diagnostic_run_directory"] = ""
            (workspace / "provenance" / "run-manifest.json").write_text(json.dumps(record), encoding="utf-8")
            report = verifier.verify(workspace, "all")

        self.assertFalse(report.progress["stages"]["B4"])

    def test_every_status_the_class_check_ratifies_settles_the_class(self) -> None:
        for status in ("accepted", "Confirmed", "approved"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                workspace = self._diagnosed(temporary)
                manifest = workspace / "provenance" / "run-manifest.json"
                record = json.loads(manifest.read_text(encoding="utf-8"))
                record["project"]["class_proposal"]["status"] = status
                manifest.write_text(json.dumps(record), encoding="utf-8")
                report = verifier.verify(workspace, "all")
                self.assertTrue(report.progress["stages"]["B3"])
                self.assertEqual(verifier.PASS, _status(report, "CLS-3"))

    def test_an_empty_workspace_reached_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "empty"
            (root / "output").mkdir(parents=True)
            (root / "provenance").mkdir()
            report = verifier.verify(root, "all")

        self.assertEqual("B0 none", report.progress["stage_reached"])

    def test_artifacts_past_a_gap_are_reported_out_of_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _fixture, workspace = _prepared(temporary)
            output = workspace / "output"
            for name in ("MS_DIAL_publication_report.json", "MS_DIAL_Materials_and_Methods.txt",
                         "Supplementary_Table_MS_DIAL.tsv"):
                (output / name).write_text("{}", encoding="ascii")
            report = verifier.verify(workspace, "all")

        self.assertIn("B9 publication_artifacts", report.progress["present_out_of_order"])

    def test_a_check_that_ran_twice_is_reported_by_its_worst_verdict(self) -> None:
        """A re-run leaves two AlignResult files; one failing must not read as a pass."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._finished(temporary)
            _mztab(workspace / "output" / "AlignResult-0.mzTab", malformed=True)
            report = verifier.verify(workspace, "all")
            payload = report.as_dict()

        statuses = [check.status for check in report.checks if check.check_id == "TAB-1"]
        self.assertIn(verifier.FAIL, statuses)
        self.assertEqual(verifier.FAIL, payload["checks_by_stage"]["B7"]["TAB-1"])

    def test_the_json_report_carries_progress_and_checks_by_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = SplitFixture(Path(temporary)).gate(0, "all")
            payload = report.as_dict()

        self.assertIn("stage_reached", payload["progress"])
        self.assertEqual("warn", payload["checks_by_stage"]["B1"]["SUM-1"])
        self.assertIn("SPL-1", payload["checks_by_stage"]["B2"])


class SecondReviewTests(unittest.TestCase):
    """The second adversarial round: each case a reviewer showed the fixes getting wrong."""

    def _declared_unit(self, temporary: str, *, files: list[str], inputs: list[str],
                       downloads: list[str] | None = None, extracted: list[str] | None = None) -> Path:
        root = Path(temporary) / "declared"
        data = root / "raw" / "data"
        (root / "output").mkdir(parents=True)
        data.mkdir(parents=True)
        paths = []
        for name in inputs:
            path = data / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
            paths.append(str(path))
        _write(root / "provenance" / "run-manifest.json", {
            "project": {"analysis_unit_id": "declared", "repository": "mb_post",
                        "files": [{"name": name, "checksum": "ab" * 16} for name in files]},
            "input_directory": str(data),
            "input_candidates": paths,
            "downloads": [{"path": str(root / "raw" / "downloads" / name), "sha256": "cd" * 32}
                          for name in downloads or []],
            "extracted_files": [str(data / name) for name in extracted or []],
            "allowlist_checksum_validation": {"required": True, "verified": len(files), "skipped": 0},
        })
        return root

    def test_an_input_no_declared_checksum_covers_is_refused(self) -> None:
        """Every declared file verified is not every input checked."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self._declared_unit(temporary, files=["a.lcd", "b.lcd"], inputs=["a.lcd", "b.lcd", "c.mzML"])
            report = verifier.verify(root, "before-production")

        check = _check(report, "SUM-1")
        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual(1, check.evidence["inputs_without_verified_checksum"])

    def test_verified_files_with_no_input_recorded_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._declared_unit(temporary, files=["a.lcd"], inputs=[])
            report = verifier.verify(root, "before-production")

        self.assertEqual(verifier.FAIL, _status(report, "SUM-1"))

    def test_inputs_inside_an_extracted_folder_are_traced_to_their_declarations(self) -> None:
        """MPST000007's shape: declared 0555_1_neg.lcd, analysed MB-POST_files_MPST000007.0/0555_1_neg.lcd."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self._declared_unit(temporary, files=["0555_1_neg.lcd", "0555_2_neg.lcd"],
                                       inputs=["MB-POST_files.0/0555_1_neg.lcd", "MB-POST_files.0/0555_2_neg.lcd"])
            report = verifier.verify(root, "before-production")

        self.assertEqual(verifier.PASS, _status(report, "SUM-1"))

    def test_a_vendor_directory_whose_members_are_declared_is_covered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._declared_unit(temporary, files=["S1.raw/_FUNC001.DAT", "S1.raw/_extern.inf"],
                                       inputs=["S1.raw"])
            report = verifier.verify(root, "before-production")

        self.assertEqual(verifier.PASS, _status(report, "SUM-1"))

    def test_a_declared_archive_does_not_vouch_for_what_lies_beside_it(self) -> None:
        """The validator checks a declared archive only where it sits unextracted: nothing came out."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self._declared_unit(temporary, files=["a.lcd", "notes.zip"], inputs=["a.lcd", "c.lcd"])
            report = verifier.verify(root, "before-production")

        self.assertEqual(verifier.FAIL, _status(report, "SUM-1"))

    def test_a_declared_name_does_not_vouch_for_a_deeper_file_of_the_same_name(self) -> None:
        """A tar holding ROOT/a.lcd and ROOT/neg/a.lcd: only the first was checked."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self._declared_unit(temporary, files=["a.lcd"], inputs=["ROOT/a.lcd", "ROOT/neg/a.lcd"])
            report = verifier.verify(root, "before-production")

        check = _check(report, "SUM-1")
        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual(1, check.evidence["inputs_without_verified_checksum"])

    def test_a_metabolights_archive_unit_rests_on_the_archive_hash(self) -> None:
        """The allow-list names the archive, so extracted_files is empty; the archive's sha256 covers it."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "zip"
            data = root / "raw" / "data"
            data.mkdir(parents=True)
            (root / "output").mkdir()
            inputs = []
            for name in ("a.mzML", "b.mzML"):
                (data / name).write_bytes(b"x")
                inputs.append(str(data / name))
            _write(root / "provenance" / "run-manifest.json", {
                "project": {"analysis_unit_id": "zip", "repository": "metabolights",
                            "files": [{"name": "FILES/study.zip", "checksum": ""}]},
                "input_directory": str(data),
                "input_candidates": inputs,
                "downloads": [{"path": str(root / "raw" / "downloads" / "study.zip"), "sha256": "cd" * 32,
                               "declared_checksum": ""}],
                "extracted_files": [],
                "allowlist_checksum_validation": {"required": False, "verified": 0, "skipped": 1},
            })
            report = verifier.verify(root, "before-production")

        self.assertEqual(verifier.WARN, _status(report, "SUM-1"))

    def test_extracted_files_that_are_not_a_list_do_not_crash_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SplitFixture(Path(temporary))
            _edit(fixture.parent_manifest, extracted_files=7)
            report = fixture.gate(0, "all")

        self.assertIn(_status(report, "SUM-1"), (verifier.WARN, verifier.FAIL))

    def _published(self, temporary: str, sentence: str):
        fixture = SplitFixture(Path(temporary))
        output = fixture.part_roots[0] / "output"
        (output / "MS_DIAL_Materials_and_Methods.txt").write_text(sentence, encoding="utf-8")
        return fixture.gate(0, "before-publish")

    def test_every_common_way_of_claiming_a_verification_is_caught(self) -> None:
        claims = [
            "Raw files were checksum-verified.",
            "All MD5 checksums verified.",
            "Checksums were verified for every raw file.",
            "The downloader verified checksums for every raw file.",
            "Checksum verification: passed (11/11).",
            "File integrity was verified before processing.",
            "MD5 checksums were verified against the repository.",
            "All raw files passed checksum verification.",
            "Raw file integrity was verified.",
            "File integrity was confirmed by MD5 checksum comparison.",
            "Each file's checksum was validated against MetaboLights.",
            "Inputs were verified using SHA-256 checksums.",
        ]
        for sentence in claims:
            with self.subTest(sentence=sentence), tempfile.TemporaryDirectory() as temporary:
                self.assertEqual(verifier.FAIL, _status(self._published(temporary, sentence), "SUM-2"))

    def test_a_claim_is_caught_however_a_clause_before_it_is_phrased(self) -> None:
        """The third review: a negation elsewhere in the sentence must not excuse the claim."""
        claims = [
            "No raw file failed checksum verification.",
            "Although MetaboLights does not publish checksums for every study, all raw files here were checksum-verified.",
            "None of the 11 input files failed MD5 checksum validation.",
            "All files, including the MSP library, were checksum-verified.",
            "Raw data were downloaded without modification and their MD5 checksums were verified.",
            "No download errors occurred, and all checksums were verified.",
            "The inputs were not only downloaded from MetaboLights but also checksum-verified.",
            "The library checksum was not compared, but every raw file checksum was verified.",
            "Neither blanks nor QCs were excluded, and the input checksums were verified against the repository.",
            "Raw files were not checksum-verified by MetaboLights, but their integrity was verified by the downloader.",
            "Don't read too much into it: every checksum was verified.",
            "All downloaded files and the spectral library were checksum-verified.",
        ]
        for sentence in claims:
            with self.subTest(sentence=sentence), tempfile.TemporaryDirectory() as temporary:
                self.assertEqual(verifier.FAIL, _status(self._published(temporary, sentence), "SUM-2"))

    def test_an_honest_disclosure_or_a_library_statement_is_not_a_claim(self) -> None:
        disclosures = [
            "MetaboLights publishes no checksums, so the inputs were not checksum-verified; integrity "
            "rests on the sha256 recorded at download.",
            "Raw files were not checksum-verified, because MetaboLights publishes no checksums.",
            "Input integrity was not verified against published checksums.",
            "No input could be verified against the repository checksum, since none is published.",
            "These inputs are not integrity-verified.",
            "No artifact may describe these inputs as checksum-verified.",
            "The library file was MD5-verified against its Zenodo record.",
            "An md5-verified download from Zenodo supplied the MSP.",
            "The inputs weren\u2019t checksum-verified.",
        ]
        for sentence in disclosures:
            with self.subTest(sentence=sentence), tempfile.TemporaryDirectory() as temporary:
                check = _check(self._published(temporary, sentence), "SUM-2")
                # Not refused, and not passed either: a rule set it aside, so a person reads it.
                self.assertEqual(verifier.WARN, check.status)
                self.assertIn("read them", check.detail)

    def test_publication_with_no_checksum_phrasing_at_all_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            check = _check(self._published(temporary, "Peaks were aligned across 6 samples."), "SUM-2")

        self.assertEqual(verifier.PASS, check.status)

    def test_a_recorded_failure_is_named_as_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _fixture, workspace = _prepared(temporary)
            _edit(workspace / "provenance" / "run-manifest.json", status="run_failed",
                  run_failures=[{"exit_code": 3}])
            report = verifier.verify(workspace, "after-run")

        self.assertIn("recorded as failed", _check(report, "EXP-1").detail)
        self.assertIn("last exit code 3", _check(report, "EXP-1").detail)
        self.assertIn("recorded as failed", _check(report, "MTH-1").detail)
        self.assertNotIn("reported success", _check(report, "EXP-1").detail)

    def test_a_retried_unit_is_not_called_failed_by_its_history(self) -> None:
        """run_failures is appended to and never cleared; only run_failed says the latest run failed."""
        with tempfile.TemporaryDirectory() as temporary:
            _fixture, workspace = _prepared(temporary)
            (workspace / "output" / "s0.mdpeak").write_text("Peak ID\n", encoding="ascii")
            _edit(workspace / "provenance" / "run-manifest.json", status="mztab_validated",
                  finalized_at="2026-09-25T10:00:00+09:00", mztab_validation={"summary": {"failed": 0}},
                  run_failures=[{"exit_code": 3}])
            report = verifier.verify(workspace, "after-run")

        detail = _check(report, "EXP-1").detail
        self.assertIn("skipped silently", detail)
        self.assertIn("1 earlier failed attempt(s) are recorded", detail)
        self.assertNotIn("recorded as failed", _check(report, "MTH-1").detail)

    def test_a_run_failures_field_that_is_not_a_list_does_not_crash_the_gate(self) -> None:
        for value in (1, -1, 1.5, True):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                _fixture, workspace = _prepared(temporary)
                _edit(workspace / "provenance" / "run-manifest.json", run_failures=value)
                verifier.verify(workspace, "all")

    def test_a_part_whose_owner_cannot_be_read_has_no_known_raw_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SplitFixture(Path(temporary))
            fixture.parent_manifest.unlink()
            report = fixture.gate(0, "before-publish")

        check = _check(report, "RET-1")
        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertTrue(check.required)

    def test_a_tree_gone_without_a_confirmed_cleanup_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "gone"
            (root / "output").mkdir(parents=True)
            _write(root / "provenance" / "run-manifest.json", {
                "project": {"analysis_unit_id": "gone"},
                "status": "cleanup_pending_confirmation", "raw_retention_policy": "delete_after_validated_output",
            })
            report = verifier.verify(root, "before-publish")

        check = _check(report, "RET-1")
        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("no confirmed cleanup is recorded", check.detail)

    def test_a_deletion_awaiting_confirmation_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "pending"
            (root / "output").mkdir(parents=True)
            (root / "raw").mkdir()
            (root / "raw" / "data.bin").write_bytes(b"0")
            _write(root / "provenance" / "run-manifest.json", {
                "project": {"analysis_unit_id": "pending"},
                "status": "cleanup_pending_confirmation", "raw_retention_policy": "delete_after_validated_output",
            })
            report = verifier.verify(root, "before-publish")

        self.assertEqual(verifier.WARN, _status(report, "RET-1"))

    def test_raw_data_discarded_after_a_failed_validation_is_not_a_release(self) -> None:
        # Only the confirmed cleanup, which records raw_cleaned, releases a tree; a validated unit
        # whose tree simply vanished went without the confirmation deletion needs.
        for status, failed, released in (("discarded", 1, False), ("mztab_validated", 0, False)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                fixture, workspace = _prepared(temporary)
                # The owner's policy is the one that decides; the part's copy says the same here.
                _edit(fixture.parent_manifest, raw_retention_policy="delete_after_validated_output")
                _edit(workspace / "provenance" / "run-manifest.json", status=status,
                      finalized_at="2026-09-25T10:00:00+09:00", mztab_validation={"summary": {"failed": failed}},
                      retained_artifact_inventory=[], raw_retention_policy="delete_after_validated_output")
                shutil.rmtree(Path(temporary) / "unit" / "raw")
                report = verifier.verify(workspace, "all")
                self.assertEqual(released, report.progress["stages"]["B1"])


class TrialManifestMirrorsTheGateTests(unittest.TestCase):
    """The trial manifest describes the stages; the gate defines them. They must not drift."""

    def test_part_b_stages_match_the_gates_completion_stages(self) -> None:
        manifest = json.loads(
            (_ROOT / "trials" / "2026-09-20-ten-unit-trial-manifest.json").read_text(encoding="utf-8"))
        described = [(item["id"], item["stage"], item["judged_by"])
                     for item in manifest["evaluation"]["part_b_stages"]]
        defined = [(code, name, list(checks)) for code, name, _meaning, checks in verifier.COMPLETION_STAGES]
        self.assertEqual(defined, described)


if __name__ == "__main__":
    unittest.main()
