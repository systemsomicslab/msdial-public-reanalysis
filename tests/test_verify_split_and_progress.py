"""Split units, repositories without checksums, stages not reached, and progress apart from verdict.

MetaboLights MTBLS2207 was the first unit split by raw-header acquisition mode into parts that
share their parent's raw tree. Run against those parts the gate called their raw data released,
turned the parent's checksum FAIL into an absence, and reported a unit nobody had run yet as two
records disagreeing. It also refused every MetaboLights unit outright, because that repository
declares no checksums at all.

The trial evaluation that follows from it (user decision, 2026-09-25) reports how far a run got
separately from whether it was right, and derives the first from the artifacts on disk.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "verify-run-invariants.py"
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


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class SplitFixture:
    """A parent that downloaded three files and two parts holding two and one of them."""

    def __init__(self, root: Path, *, declared: str = "", hashed: bool = True,
                 part_inputs: tuple[list[int], list[int]] = ([0, 1], [2])) -> None:
        self.root = root
        parent_root = root / "unit"
        data = parent_root / "raw" / "data"
        data.mkdir(parents=True)
        self.inputs = []
        for index in range(3):
            path = data / f"s{index}.mzML"
            path.write_bytes(b"x")
            self.inputs.append(str(path))
        self.parent_manifest = parent_root / "provenance" / "run-manifest.json"
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
                "raw_directory": str(parent_root / "raw"),
                "raw_owned_by": str(self.parent_manifest),
                "raw_retention_policy": "keep",
            })
        _write(self.parent_manifest, {
            "project": {
                "analysis_unit_id": "unit",
                "files": [{"name": f"FILES/s{i}.mzML", "checksum": declared if i == 0 else ""}
                          for i in range(3)],
            },
            "status": "split_by_acquisition",
            "execution_allowed": False,
            "input_candidates": list(self.inputs),
            "downloads": [{"path": item, "sha256": "ab" * 32 if hashed else "", "declared_checksum": ""}
                          for item in self.inputs],
            "allowlist_checksum_validation": {"required": bool(declared), "verified": 1 if declared else 0,
                                              "skipped": 2 if declared else 3},
            "split_into": parts,
            "raw_retention_policy": "keep",
        })

    def gate(self, part: int, stage: str = "before-production"):
        return verifier.verify(self.part_roots[part], stage)


class ChecksumPolicyTests(unittest.TestCase):
    def test_a_repository_that_declares_no_checksum_is_a_warning_not_a_refusal(self) -> None:
        """The user's decision of 2026-09-25: MetaboLights units proceed on the download sha256."""
        with tempfile.TemporaryDirectory() as temporary:
            report = SplitFixture(Path(temporary)).gate(0)

        check = _check(report, "SUM-1")
        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("No artifact may describe these inputs as checksum-verified", check.detail)
        self.assertEqual("unit", check.evidence["inherited_from"])
        self.assertEqual(3, check.evidence["downloads_with_sha256"])

    def test_a_partial_declaration_is_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = SplitFixture(Path(temporary), declared="cd" * 32).gate(0)

        self.assertEqual(verifier.FAIL, _status(report, "SUM-1"))

    def test_no_declared_checksum_and_no_download_hash_is_refused(self) -> None:
        """Without the download-time hash there is no integrity record at all to rest on."""
        with tempfile.TemporaryDirectory() as temporary:
            report = SplitFixture(Path(temporary), hashed=False).gate(0)

        self.assertEqual(verifier.FAIL, _status(report, "SUM-1"))

    def test_the_evidence_keeps_what_the_validation_record_said(self) -> None:
        """The record's "required" used to bind to the report's own parameter and vanish."""
        with tempfile.TemporaryDirectory() as temporary:
            report = SplitFixture(Path(temporary), declared="cd" * 32).gate(0)

        self.assertIn("any_checksum_verified", _check(report, "SUM-1").evidence)


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

    def test_an_unsplit_unit_owes_no_split_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plain"
            (root / "output").mkdir(parents=True)
            _write(root / "provenance" / "run-manifest.json", {"project": {"analysis_unit_id": "plain"}})
            report = verifier.verify(root, "before-production")

        check = _check(report, "SPL-1")
        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertFalse(check.required)

    def test_a_part_reads_its_raw_tree_at_the_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = SplitFixture(Path(temporary)).gate(0, "before-publish")

        self.assertEqual(verifier.PASS, _status(report, "RET-1"))
        dsk = _check(report, "DSK-1")
        self.assertEqual(verifier.NOT_EVALUABLE, dsk.status)
        self.assertFalse(dsk.required, "a part's storage is accounted once, at its owner")

    def test_the_parent_is_named_as_a_raw_owner_not_a_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SplitFixture(Path(temporary))
            report = verifier.verify(fixture.parent_manifest.parent.parent, "before-production")

        check = _check(report, "ELIG-1")
        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("Gate the parts instead", check.detail)


class StageNotReachedTests(unittest.TestCase):
    """A unit nobody has run yet is not two records disagreeing."""

    def _prepared(self, temporary: str) -> Path:
        fixture = SplitFixture(Path(temporary))
        output = fixture.part_roots[0] / "output"
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
        return fixture.part_roots[0]

    def test_after_run_checks_on_an_unrun_unit_are_not_reached(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._prepared(temporary)
            report = verifier.verify(workspace, "after-run")

        for check_id in ("CNT-1", "EXP-1", "MTH-1"):
            check = _check(report, check_id)
            self.assertEqual(verifier.NOT_EVALUABLE, check.status, check_id)
            self.assertTrue(check.required, f"{check_id} must still refuse under --strict")
        self.assertIn("No production run has started", _check(report, "EXP-1").detail)

    def test_strict_still_refuses_an_unrun_unit_after_the_run_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._prepared(temporary)
            code = verifier.main([str(workspace), "--stage", "after-run", "--strict"])

        self.assertEqual(4, code)

    def test_a_run_that_skipped_files_is_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._prepared(temporary)
            (workspace / "output" / "s0.mdpeak").write_text("Peak ID\n", encoding="ascii")
            report = verifier.verify(workspace, "after-run")

        self.assertEqual(verifier.FAIL, _status(report, "EXP-1"))
        self.assertEqual(verifier.FAIL, _status(report, "CNT-1"))

    def test_finalisation_is_not_reached_when_nothing_ran(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._prepared(temporary)
            report = verifier.verify(workspace, "before-publish")

        check = _check(report, "FIN-1")
        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertTrue(check.required)


class ProgressTests(unittest.TestCase):
    """How far a run got, from its artifacts, reported apart from the verdict."""

    def test_a_prepared_unit_reports_the_stage_it_reached_despite_a_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = StageNotReachedTests()._prepared(temporary)
            manifest = workspace / "provenance" / "run-manifest.json"
            record = json.loads(manifest.read_text(encoding="utf-8"))
            diagnostics = workspace / "diagnostics" / "job1"
            diagnostics.mkdir(parents=True)
            record["project"]["class_proposal"] = {"status": "accepted", "assignments": []}
            record["peak_height_diagnostics"] = [{"diagnostic_run_directory": str(diagnostics)}]
            manifest.write_text(json.dumps(record), encoding="utf-8")
            report = verifier.verify(workspace, "all")

        self.assertFalse(report.ok, "the unit has not run, so the full gate refuses it")
        self.assertEqual("B5 production_prepared", report.progress["stage_reached"])
        self.assertEqual("B6 production_run_done", report.progress["next_stage"])

    def test_an_empty_workspace_reached_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "empty"
            (root / "output").mkdir(parents=True)
            (root / "provenance").mkdir()
            report = verifier.verify(root, "all")

        self.assertEqual("B0 none", report.progress["stage_reached"])

    def test_artifacts_past_a_gap_are_reported_out_of_order(self) -> None:
        """A publication bundle beside an unfinalised manifest must not hide behind the gap."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = StageNotReachedTests()._prepared(temporary)
            output = workspace / "output"
            for name in ("MS_DIAL_publication_report.json", "MS_DIAL_Materials_and_Methods.txt",
                         "Supplementary_Table_MS_DIAL.tsv"):
                (output / name).write_text("{}", encoding="ascii")
            report = verifier.verify(workspace, "all")

        self.assertIn("B9 publication_artifacts", report.progress["present_out_of_order"])

    def test_the_json_report_carries_progress_and_checks_by_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = SplitFixture(Path(temporary)).gate(0, "all")
            payload = report.as_dict()

        self.assertIn("stage_reached", payload["progress"])
        self.assertEqual("warn", payload["checks_by_stage"]["B1"]["SUM-1"])
        self.assertIn("SPL-1", payload["checks_by_stage"]["B2"])


if __name__ == "__main__":
    unittest.main()
