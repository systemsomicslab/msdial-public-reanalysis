"""Tests for the unattended-run invariant checker.

Each test builds the smallest workspace that exhibits one failure and asserts the verdict for the
one check that failure concerns. The fixtures are synthetic on purpose: the checker has to work on a
workspace nobody has curated, which is the situation an unattended loop is always in.

The last test is the one that matters most. A checker that reports PASS for an artifact it could not
read would defeat its own purpose, so the absence of every optional artifact must produce
NOT_EVALUABLE and never PASS.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "verify-run-invariants.py"
_SPEC = importlib.util.spec_from_file_location("verify_run_invariants", _MODULE_PATH)
assert _SPEC and _SPEC.loader
verifier = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_run_invariants"] = verifier
_SPEC.loader.exec_module(verifier)


UNIT_ID = "6f27431da49ec82f3734"
CSV_COLUMNS = [
    "file_path", "file_name", "file_type", "class_id",
    "acquisition_type", "batch_order", "analytical_order", "factor",
]


def _samples(count: int, *, classes: int = 2, grouped: bool = True) -> list[dict]:
    """count samples spread over `classes` groups, either grouped or interleaved."""
    rows = []
    for index in range(count):
        group = index // max(1, count // classes) if grouped else index % classes
        group = min(group, classes - 1)
        rows.append({
            "file_path": f"D:\\\\raw\\\\sample_{index + 1}.lcd",
            "file_name": f"sample_{index + 1}",
            "file_type": "Sample",
            "class_id": f"Strain{group + 1}",
            "acquisition_type": "DDA",
            "batch_order": "1",
            "analytical_order": str(index + 1),
            "factor": "",
        })
    return rows


class WorkspaceBuilder:
    """A unit workspace on disk, built one artifact at a time."""

    def __init__(self, root: Path, *, unit_id: str = UNIT_ID) -> None:
        self.root = root / unit_id
        (self.root / "provenance").mkdir(parents=True)
        (self.root / "output").mkdir(parents=True)

    @property
    def output(self) -> Path:
        return self.root / "output"

    def provenance(self, *, inputs: int = 6, unit_id: str | None = UNIT_ID,
                   execution_allowed: bool = True, verified: int | None = None,
                   skipped: int = 0, preflight: dict | None = None) -> "WorkspaceBuilder":
        project = {"repository": "mb_post", "accession": "MPST000007"}
        if unit_id is not None:
            project["analysis_unit_id"] = unit_id
        manifest = {
            "schema": "msdial-public-reanalysis-run.v1",
            "project": project,
            "execution_allowed": execution_allowed,
            "input_candidates": [f"sample_{i + 1}.lcd" for i in range(inputs)],
            "allowlist_checksum_validation": {
                "required": True,
                "verified": inputs if verified is None else verified,
                "skipped": skipped,
            },
        }
        if preflight is not None:
            manifest["raw_metadata_preflight"] = preflight
        (self.root / "provenance" / "run-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8")
        return self

    def analysis_csv(self, rows: list[dict]) -> "WorkspaceBuilder":
        path = self.output / "analysis_files.csv"
        with path.open("w", encoding="ascii", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        return self

    def run_manifest(self, rows: list[dict], *, produce: bool = True) -> "WorkspaceBuilder":
        exports = [str(self.output / f"{row['file_name']}.mdpeak") for row in rows]
        (self.output / "run-manifest.json").write_text(json.dumps({
            "source_files": [row["file_path"] for row in rows],
            "expected_analysis_exports": exports,
        }), encoding="utf-8")
        if produce:
            for path in exports:
                Path(path).write_text("Peak ID\n", encoding="ascii")
        return self

    def mztab(self, *, runs: int = 6, trailing_tab: bool = False) -> "WorkspaceBuilder":
        header = ["SEH", "SME_ID", "evidence_input_id", "chemical_name", "rank"]
        lines = [f"MTD\tms_run[{i + 1}]-location\tfile://x" for i in range(runs)]
        lines.append("\t".join(header))
        for i in range(3):
            row = "\t".join(["SME", str(i + 1), str(i + 1), "Compound", "1"])
            lines.append(row + ("\t" if trailing_tab else ""))
        (self.output / "AlignResult-1.mzTab").write_text("\n".join(lines) + "\n", encoding="ascii")
        return self

    def publication(self, *, run_order_status: str | None = "pass",
                    provenance_warning_for: str | None = None) -> "WorkspaceBuilder":
        checks = [
            {"metric": "median_qc_rsd_percent", "value": None, "status": "not_assessed"},
        ]
        if run_order_status is not None:
            checks.append({
                "metric": "run_order_intensity_correlation",
                "value": 0.0747,
                "status": run_order_status,
            })
        warnings = []
        if provenance_warning_for:
            warnings.append(
                f"No persistent identifier was recorded for {provenance_warning_for}."
            )
        (self.output / "MS_DIAL_publication_report.json").write_text(json.dumps({
            "qa_assessment": {
                "status": "pass",
                "passed": 1,
                "evaluated": 1 if run_order_status else 0,
                "checks": checks,
            },
            "library_provenance_warnings": warnings,
        }), encoding="utf-8")
        return self

    def workflow_settings(self, *, library: str, doi: str | None) -> "WorkspaceBuilder":
        entry = {"path": f"D:\\\\lib\\\\{library}", "version": "1"}
        if doi:
            entry["doi"] = doi
        (self.output / "workflow-settings.json").write_text(
            json.dumps({"library_provenance": [entry]}), encoding="utf-8")
        return self


def _status(report, check_id: str) -> str:
    matching = [check for check in report.checks if check.check_id == check_id]
    assert matching, f"{check_id} was not evaluated at all"
    assert len(matching) == 1, f"{check_id} ran {len(matching)} times; each check must run once"
    return matching[0].status


class SampleCountTests(unittest.TestCase):
    def test_a_truncated_analysis_csv_is_refused(self):
        # The failure this whole file exists for: the tuning diagnostic rewrites the production
        # analysis CSV to its single representative, and every later stage is self-consistent about
        # the wrong study.
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6)
            builder.analysis_csv(rows[:1])
            report = verifier.verify(builder.root, "before-production")
            self.assertEqual(verifier.FAIL, _status(report, "CNT-1"))
            self.assertFalse(report.ok)

    def test_counts_that_agree_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6)
            builder.analysis_csv(rows).run_manifest(rows)
            report = verifier.verify(builder.root, "before-production")
            self.assertEqual(verifier.PASS, _status(report, "CNT-1"))

    def test_a_console_that_skipped_files_is_refused_after_the_run(self):
        # MS-DIAL Console can exit 0 having skipped an input it could not read. Nothing server-side
        # compares produced against expected.
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6)
            builder.analysis_csv(rows).run_manifest(rows)
            (builder.output / f"{rows[-1]['file_name']}.mdpeak").unlink()
            report = verifier.verify(builder.root, "after-run")
            self.assertEqual(verifier.FAIL, _status(report, "CNT-1"))
            self.assertEqual(verifier.FAIL, _status(report, "EXP-1"))

    def test_a_single_count_is_not_evaluable(self):
        with tempfile.TemporaryDirectory() as directory:
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6)
            report = verifier.verify(builder.root, "before-production")
            self.assertEqual(verifier.NOT_EVALUABLE, _status(report, "CNT-1"))


class IdentityAndEligibilityTests(unittest.TestCase):
    def test_an_accession_scoped_workspace_is_refused(self):
        # The older layout wrote results per accession and declared the same manifest schema, so
        # the schema string cannot separate the two generations.
        with tempfile.TemporaryDirectory() as directory:
            builder = WorkspaceBuilder(Path(directory)).provenance(unit_id=None)
            report = verifier.verify(builder.root, "before-production")
            self.assertEqual(verifier.FAIL, _status(report, "ID-1"))

    def test_a_mismatched_unit_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            builder = WorkspaceBuilder(Path(directory)).provenance(unit_id="some-other-unit")
            report = verifier.verify(builder.root, "before-production")
            self.assertEqual(verifier.FAIL, _status(report, "ID-1"))

    def test_an_ineligible_unit_is_refused(self):
        # execution_allowed is written by the server and read by nothing; reading it here is what
        # makes it a gate again.
        with tempfile.TemporaryDirectory() as directory:
            builder = WorkspaceBuilder(Path(directory)).provenance(execution_allowed=False)
            report = verifier.verify(builder.root, "before-production")
            self.assertEqual(verifier.FAIL, _status(report, "ELIG-1"))

    def test_partial_checksum_coverage_is_refused(self):
        # {"required": true, "verified": 1, "skipped": 5} reads to a boolean scan exactly like full
        # coverage.
        with tempfile.TemporaryDirectory() as directory:
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6, verified=1, skipped=5)
            report = verifier.verify(builder.root, "before-production")
            self.assertEqual(verifier.FAIL, _status(report, "SUM-1"))

    def test_an_unread_header_permits_the_run_but_not_the_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            builder = WorkspaceBuilder(Path(directory)).provenance(
                preflight={"exit_code": 1, "summary": None})
            report = verifier.verify(builder.root, "before-production")
            self.assertEqual(verifier.WARN, _status(report, "PRE-1"))


class RunOrderTests(unittest.TestCase):
    def test_a_synthesized_order_warns_but_does_not_stop_the_run(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6, classes=2, grouped=True)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6).analysis_csv(rows)
            report = verifier.verify(builder.root, "before-production")
            self.assertEqual(verifier.WARN, _status(report, "ORD-1"))

    def test_asserting_a_drift_metric_on_a_synthesized_order_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6, classes=2, grouped=True)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6).analysis_csv(rows)
            builder.publication(run_order_status="pass")
            report = verifier.verify(builder.root, "before-publish")
            self.assertEqual(verifier.FAIL, _status(report, "ORD-2"))

    def test_a_synthesized_order_with_the_metric_suppressed_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6, classes=2, grouped=True)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6).analysis_csv(rows)
            builder.publication(run_order_status=None)
            report = verifier.verify(builder.root, "before-publish")
            self.assertEqual(verifier.PASS, _status(report, "ORD-2"))

    def test_an_interleaved_order_is_not_treated_as_synthesized(self):
        # The same 1..N sequence is not evidence of fabrication when the classes are interleaved,
        # which is what a real randomised injection sequence looks like.
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6, classes=2, grouped=False)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6).analysis_csv(rows)
            builder.publication(run_order_status="pass")
            report = verifier.verify(builder.root, "before-publish")
            self.assertEqual(verifier.PASS, _status(report, "ORD-2"))


class MztabTests(unittest.TestCase):
    def test_a_whole_section_width_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6)
            builder.analysis_csv(rows).run_manifest(rows).mztab(runs=6, trailing_tab=True)
            report = verifier.verify(builder.root, "after-run")
            self.assertEqual(verifier.FAIL, _status(report, "TAB-1"))

    def test_a_clean_mztab_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6)
            builder.analysis_csv(rows).run_manifest(rows).mztab(runs=6, trailing_tab=False)
            report = verifier.verify(builder.root, "after-run")
            self.assertEqual(verifier.PASS, _status(report, "TAB-1"))
            self.assertEqual(verifier.PASS, _status(report, "TAB-2"))

    def test_a_run_count_below_the_approved_samples_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6)
            builder.analysis_csv(rows).run_manifest(rows).mztab(runs=4)
            report = verifier.verify(builder.root, "after-run")
            self.assertEqual(verifier.FAIL, _status(report, "TAB-2"))


class PublicationTests(unittest.TestCase):
    def test_a_provenance_warning_contradicting_recorded_provenance_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6)
            builder.workflow_settings(library="public-library.msp", doi="10.5281/zenodo.1")
            builder.publication(run_order_status=None, provenance_warning_for="public-library.msp")
            report = verifier.verify(builder.root, "before-publish")
            self.assertEqual(verifier.FAIL, _status(report, "LIB-1"))

    def test_a_warning_for_a_library_carrying_no_identifier_is_not_a_contradiction(self):
        # The check must fire on a contradiction, not on the warning itself. A library with no doi,
        # no source, no record_url and no version is one the reporter is right about.
        with tempfile.TemporaryDirectory() as directory:
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6)
            (builder.output / "workflow-settings.json").write_text(
                json.dumps({"library_provenance": [{"path": "D:\\\\lib\\\\unknown.msp"}]}),
                encoding="utf-8")
            builder.publication(run_order_status=None, provenance_warning_for="unknown.msp")
            report = verifier.verify(builder.root, "before-publish")
            self.assertEqual(verifier.PASS, _status(report, "LIB-1"))

    def test_unassessable_qa_criteria_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6)
            builder.publication(run_order_status="pass")
            report = verifier.verify(builder.root, "before-publish")
            self.assertEqual(verifier.WARN, _status(report, "QA-1"))


class AbsenceTests(unittest.TestCase):
    def test_an_empty_workspace_never_reports_pass(self):
        # The property that makes the checker safe to run at any point: an artifact that is not
        # there yet is NOT_EVALUABLE, never PASS. A checker that passes what it cannot read would
        # certify an unattended run on the strength of files that do not exist.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / UNIT_ID
            (root / "provenance").mkdir(parents=True)
            (root / "output").mkdir(parents=True)
            report = verifier.verify(root, "all")
            self.assertGreater(len(report.checks), 0)
            self.assertNotIn(verifier.PASS, {check.status for check in report.checks})
            for check in report.checks:
                self.assertIn(check.status, (verifier.NOT_EVALUABLE, verifier.FAIL), check.check_id)

    def test_every_check_runs_at_most_once_per_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6)
            builder.analysis_csv(rows).run_manifest(rows).mztab(runs=6)
            builder.workflow_settings(library="lib.msp", doi="10.5281/zenodo.1").publication(
                run_order_status=None)
            report = verifier.verify(builder.root, "all")
            ids = [check.check_id for check in report.checks]
            duplicated = sorted({name for name in ids if ids.count(name) > 1})
            # CNT-1 is deliberately evaluated twice, once per stage, because the artifacts it reads
            # may change between them. Nothing else may repeat.
            self.assertEqual(["CNT-1"], duplicated)


if __name__ == "__main__":
    unittest.main()
