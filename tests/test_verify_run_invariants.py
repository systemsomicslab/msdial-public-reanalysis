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
        # The failure this whole file exists for: the tuning diagnostic used to rewrite the
        # production analysis CSV to its single representative (fixed in Interactive PR #16), and
        # every later stage was self-consistent about the wrong study.
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


def _record_header_order(builder: "WorkspaceBuilder", orders: dict[str, int],
                         times: dict[str, str] | None = None, record=None) -> None:
    """Add the analytical_order record Interactive writes when it ranks files by header time."""
    path = builder.root / "provenance" / "run-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["analytical_order"] = record if record is not None else {
        "derived_from": "raw_header_acquisition_start_time",
        "files": [
            {"file": f"{name}.lcd",
             "acquisition_start_time": (times or {}).get(name, f"2020-01-{order:02d}T00:00:00+00:00"),
             "analytical_order": order}
            for name, order in orders.items()
        ],
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")


class RunOrderTests(unittest.TestCase):
    def test_a_header_order_passes_even_when_it_looks_like_row_order(self):
        # Ranked from the raw headers, it happens to equal the row order and to keep classes in
        # blocks, which the row-number heuristic alone would have called synthesized.
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6, classes=2, grouped=True)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6).analysis_csv(rows)
            _record_header_order(builder, {row["file_name"]: int(row["analytical_order"]) for row in rows})
            report = verifier.verify(builder.root, "before-production")
            self.assertEqual(verifier.PASS, _status(report, "ORD-1"))

    def test_a_csv_that_departs_from_the_recorded_header_order_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6, classes=2, grouped=True)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6).analysis_csv(rows)
            recorded = {row["file_name"]: 7 - int(row["analytical_order"]) for row in rows}
            _record_header_order(builder, recorded)
            report = verifier.verify(builder.root, "before-production")
            self.assertEqual(verifier.FAIL, _status(report, "ORD-1"))

    def test_a_record_whose_times_order_nothing_is_judged_by_shape(self):
        # Every header at one time: the ranks are the listing, so this is still a synthesized
        # order, and a drift metric on it is still refused.
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6, classes=2, grouped=True)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6).analysis_csv(rows)
            orders = {row["file_name"]: int(row["analytical_order"]) for row in rows}
            _record_header_order(builder, orders, times={name: "1970-01-01T00:00:00+00:00" for name in orders})
            builder.publication(run_order_status="pass")
            self.assertEqual(verifier.WARN, _status(verifier.verify(builder.root, "before-production"), "ORD-1"))
            self.assertEqual(verifier.FAIL, _status(verifier.verify(builder.root, "before-publish"), "ORD-2"))

    def test_a_record_that_contradicts_its_own_times_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6, classes=2, grouped=True)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6).analysis_csv(rows)
            orders = {row["file_name"]: int(row["analytical_order"]) for row in rows}
            times = {name: f"2020-01-{7 - order:02d}T00:00:00+00:00" for name, order in orders.items()}
            _record_header_order(builder, orders, times=times)
            self.assertEqual(verifier.FAIL, _status(verifier.verify(builder.root, "before-production"), "ORD-1"))

    def test_a_duplicated_row_and_a_missing_file_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6, classes=2, grouped=False)
            orders = {row["file_name"]: int(row["analytical_order"]) for row in rows}
            rows[5] = dict(rows[0])
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6).analysis_csv(rows)
            _record_header_order(builder, orders)
            self.assertEqual(verifier.FAIL, _status(verifier.verify(builder.root, "before-production"), "ORD-1"))

    def test_a_malformed_record_is_a_verdict_not_a_traceback(self):
        for record in ("not an object", {"derived_from": "raw_header_acquisition_start_time", "files": ["a.lcd"]}):
            with self.subTest(record=record), tempfile.TemporaryDirectory() as directory:
                rows = _samples(6, classes=2, grouped=False)
                builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6).analysis_csv(rows)
                _record_header_order(builder, {}, record=record)
                builder.publication(run_order_status="pass")
                self.assertEqual(verifier.FAIL, _status(verifier.verify(builder.root, "before-production"), "ORD-1"))
                verifier.verify(builder.root, "before-publish")

    def test_a_drift_metric_on_a_header_order_is_not_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6, classes=2, grouped=True)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6).analysis_csv(rows)
            _record_header_order(builder, {row["file_name"]: int(row["analytical_order"]) for row in rows})
            builder.publication(run_order_status="pass")
            report = verifier.verify(builder.root, "before-publish")
            self.assertEqual(verifier.PASS, _status(report, "ORD-2"))

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


class StrictModeTests(unittest.TestCase):
    """A workspace where nothing happened must not answer the same as one that ran correctly.

    `ok` is "no check FAILed", and an empty workspace produces no FAILs at all: run against a
    directory holding an empty provenance/ and an empty output/, the gate reported 15
    not_evaluable, ok=True and exit 0, while the batch skill tells an agent that exit 0 "means
    every evaluated check passed". That is the one answer an unattended loop must never get wrong.
    """

    def test_an_empty_workspace_is_refused_under_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = WorkspaceBuilder(Path(temporary)).root

            report = verifier.verify(workspace, "all")

            self.assertTrue(report.ok, "no check FAILed, which is exactly the problem")
            self.assertTrue(report.strict_failures, "and every one of them was unevaluable")
            self.assertEqual(4, verifier.main([str(workspace), "--strict"]))

    def test_without_strict_the_old_answer_is_unchanged(self) -> None:
        """Deliberate. Existing callers keep their exit codes until they opt in."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = WorkspaceBuilder(Path(temporary)).root

            self.assertEqual(0, verifier.main([str(workspace)]))

    def test_an_absence_that_is_a_correct_state_does_not_refuse(self) -> None:
        """A released raw tree is the intended end state, not a gap in the evidence."""
        with tempfile.TemporaryDirectory() as temporary:
            builder = WorkspaceBuilder(Path(temporary))
            report = verifier.verify(builder.root, "before-publish")

        unevaluable = {check.check_id for check in report.strict_failures}
        self.assertNotIn("DSK-1", unevaluable)


class ExecutedClassTests(unittest.TestCase):
    """CLS-2 and CLS-3. The grouping that ran, and whether anyone ratified it."""

    def _workspace(self, temporary: str, *, assignments: list[dict], rows: list[dict],
                   status: str = "accepted", sample_metadata: list[dict] | None = None) -> Path:
        builder = WorkspaceBuilder(Path(temporary)).provenance(inputs=len(rows))
        manifest_path = builder.root / "provenance" / "run-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if sample_metadata is not None:
            manifest["project"]["sample_metadata"] = sample_metadata
        manifest["project"]["class_proposal"] = {
            "proposal_id": "p1", "status": status, "model": "catalog-declared-factor-selection",
            "selected_fields": ["Factor Value[Treatment]"], "assignments": assignments,
            "warnings": ["Class was chosen by the catalog without a person reading the study."],
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        builder.analysis_csv(rows)
        return builder.root

    def test_a_grouping_that_matches_its_proposal_passes(self) -> None:
        rows = _samples(4)
        assignments = [
            {"sample_id": row["file_name"], "class_label": row["class_id"]} for row in rows
        ]
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary, assignments=assignments, rows=rows)
            report = verifier.verify(workspace, "before-production")

        self.assertEqual(verifier.PASS, _status(report, "CLS-2"))

    def test_a_relabelled_sample_is_caught(self) -> None:
        """THE DEFECT. An approved proposal received and then ignored, the grouping re-derived."""
        rows = _samples(4)
        assignments = [
            {"sample_id": row["file_name"], "class_label": row["class_id"]} for row in rows
        ]
        assignments[0]["class_label"] = "something nobody approved"
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary, assignments=assignments, rows=rows)
            report = verifier.verify(workspace, "before-production")

        self.assertEqual(verifier.FAIL, _status(report, "CLS-2"))

    def test_a_sample_the_proposal_never_covered_is_caught(self) -> None:
        rows = _samples(4)
        assignments = [
            {"sample_id": row["file_name"], "class_label": row["class_id"]} for row in rows[:3]
        ]
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary, assignments=assignments, rows=rows)
            report = verifier.verify(workspace, "before-production")

        self.assertEqual(verifier.FAIL, _status(report, "CLS-2"))

    def _named_apart(self, rows: list[dict]) -> tuple[list[dict], list[dict]]:
        """MetaboLights' shape: a sample is named for what it is, and its file is named otherwise."""
        metadata = [
            {"sample_id": f"Sample {index}", "raw_file": f"FILES/{row['file_name']}.mzML"}
            for index, row in enumerate(rows)
        ]
        assignments = [
            {"sample_id": f"Sample {index}", "class_label": row["class_id"]}
            for index, row in enumerate(rows)
        ]
        return metadata, assignments

    def test_samples_named_apart_from_their_files_are_joined_through_raw_file(self) -> None:
        """MTBLS2207: "DDA E. coli" is M3T-Std_Ecoli_neg_DDA_1mz, and every Class was right."""
        rows = _samples(4)
        metadata, assignments = self._named_apart(rows)
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(
                temporary, assignments=assignments, rows=rows, sample_metadata=metadata)
            report = verifier.verify(workspace, "before-production")

        self.assertEqual(verifier.PASS, _status(report, "CLS-2"))

    def test_a_relabel_is_still_caught_through_raw_file(self) -> None:
        rows = _samples(4)
        metadata, assignments = self._named_apart(rows)
        assignments[2]["class_label"] = "something nobody approved"
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(
                temporary, assignments=assignments, rows=rows, sample_metadata=metadata)
            report = verifier.verify(workspace, "before-production")

        self.assertEqual(verifier.FAIL, _status(report, "CLS-2"))

    def test_a_sample_whose_file_is_not_in_the_csv_is_absent(self) -> None:
        rows = _samples(4)
        metadata, assignments = self._named_apart(rows)
        metadata[1]["raw_file"] = "FILES/a_file_nobody_prepared.mzML"
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(
                temporary, assignments=assignments, rows=rows, sample_metadata=metadata)
            report = verifier.verify(workspace, "before-production")

        self.assertEqual(verifier.FAIL, _status(report, "CLS-2"))

    def test_an_unratified_proposal_is_refused(self) -> None:
        """A machine-authored grouping's whole safety argument is that somebody ratified it."""
        rows = _samples(4)
        assignments = [
            {"sample_id": row["file_name"], "class_label": row["class_id"]} for row in rows
        ]
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(
                temporary, assignments=assignments, rows=rows, status="proposed")
            report = verifier.verify(workspace, "before-production")

        self.assertEqual(verifier.FAIL, _status(report, "CLS-3"))
        self.assertEqual(verifier.PASS, _status(report, "CLS-2"),
                         "the grouping is faithful; nobody agreed to it")


class ThresholdProvenanceTests(unittest.TestCase):
    """PKH-1. The threshold the Console reads is one this unit's own diagnostic produced."""

    def _workspace(self, temporary: str, *, diagnostics: list[dict] | None,
                   written: str | None) -> Path:
        builder = WorkspaceBuilder(Path(temporary)).provenance()
        manifest_path = builder.root / "provenance" / "run-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if diagnostics is not None:
            manifest["peak_height_diagnostics"] = diagnostics
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        if written is not None:
            (builder.output / "method.txt").write_text(
                f"Smoothing method: LinearWeightedMovingAverage\nMinimum peak height: {written}\n",
                encoding="utf-8")
        return builder.root

    def test_a_measured_threshold_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(
                temporary,
                diagnostics=[{"minimum_peak_height": 500, "diagnostic_peak_count": 41230,
                              "threshold_step": 100, "method": "quantized height-range search",
                              "representative": {"file_name": "QC_05.mzML"}}],
                written="500")
            report = verifier.verify(workspace, "before-production")

        self.assertEqual(verifier.PASS, _status(report, "PKH-1"))

    def test_the_same_threshold_written_as_a_real_number_is_the_same_threshold(self) -> None:
        """500 and 500.0 are one threshold. Whether the Console can parse it is MTH-1's question."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(
                temporary, diagnostics=[{"minimum_peak_height": 500}], written="500.0")
            report = verifier.verify(workspace, "before-production")

        self.assertEqual(verifier.PASS, _status(report, "PKH-1"))

    def test_a_threshold_no_diagnostic_produced_is_refused(self) -> None:
        """Typed in, inherited from the previous unit, or left at a default: all look like this."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(
                temporary, diagnostics=[{"minimum_peak_height": 500}], written="1000")
            report = verifier.verify(workspace, "before-production")

        self.assertEqual(verifier.FAIL, _status(report, "PKH-1"))

    def test_no_diagnostic_at_all_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary, diagnostics=None, written="500")
            report = verifier.verify(workspace, "before-production")

        self.assertEqual(verifier.FAIL, _status(report, "PKH-1"))

    def test_a_zero_threshold_is_a_threshold(self) -> None:
        """The contract keeps 0 when the diagnostic count is at most 6,000."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(
                temporary, diagnostics=[{"minimum_peak_height": 0}], written="0")
            report = verifier.verify(workspace, "before-production")

        self.assertEqual(verifier.PASS, _status(report, "PKH-1"))


class MethodKeyTests(unittest.TestCase):
    """MTH-1. Every parameter in the method file was one the Console could use."""

    def _workspace(self, temporary: str, record: dict | None) -> Path:
        builder = WorkspaceBuilder(Path(temporary)).provenance()
        builder.analysis_csv(_samples(6))
        builder.run_manifest(_samples(6))
        builder.mztab()
        if record is not None:
            (builder.output / "method.keys.json").write_text(json.dumps(record), encoding="utf-8")
        return builder.root

    def test_a_value_the_console_could_not_read_is_refused(self) -> None:
        """How every threshold this campaign chose was discarded before 2026-09-20."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary, {
                "schema": "msdial-method-file-keys.v1", "applied": ["Mass slice width"],
                "unusable": ["Minimum peak height: 500.0"], "unrecognised": [], "blank": [],
            })
            report = verifier.verify(workspace, "after-run")

        self.assertEqual(verifier.FAIL, _status(report, "MTH-1"))

    def test_an_unrecognised_key_warns_rather_than_refusing(self) -> None:
        """The shipped lipidomics template has 27 of them; failing would refuse every method file."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary, {
                "applied": ["Minimum peak height"], "unusable": [],
                "unrecognised": ["Sigma window value", "Process option"], "blank": [],
            })
            report = verifier.verify(workspace, "after-run")

        self.assertEqual(verifier.WARN, _status(report, "MTH-1"))

    def test_an_older_console_that_wrote_no_record_is_not_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary, None)
            report = verifier.verify(workspace, "after-run")

        self.assertEqual(verifier.NOT_EVALUABLE, _status(report, "MTH-1"))


class PrivatePathTests(unittest.TestCase):
    """SEC-1. Nothing meant for sharing carries a path from this machine.

    The first version of this check PASSED the real publication bundle on disk, whose supplementary
    table carries the private VS20 library path twenty-four times, because its character class had
    lost a backslash and could match no Windows path at all. A check that reports PASS on an
    artifact it cannot read is worse than no check, so the pattern is asserted directly here rather
    than only through a workspace.
    """

    def test_the_pattern_matches_the_shapes_that_actually_leak(self) -> None:
        pattern = verifier.PRIVATE_PATH_PATTERN
        self.assertTrue(pattern.findall(r"path\tC:\Users\Someone\AppData\Local\lib.msp"))
        self.assertTrue(pattern.findall(r'"path": "C:\\Users\\Someone\\AppData\\lib.msp"'))
        self.assertTrue(pattern.findall("MTD\tdatabase[1]-uri\tfile://C:/Users/A%20B/lib.msp"))
        self.assertTrue(pattern.findall("D:/Users/someone/libraries/private.msp"))

    def test_a_library_named_by_name_and_checksum_is_not_a_hit(self) -> None:
        """What the contract asks for instead must not itself trip the check."""
        clean = "MSMS-Public_all-neg-VS20.msp  sha256:0123456789abcdef  41230 records"
        self.assertEqual([], verifier.PRIVATE_PATH_PATTERN.findall(clean))

    def test_a_bundle_carrying_the_path_inside_a_member_is_refused(self) -> None:
        """Scanned by content. A check reading member NAMES passes this bundle."""
        import zipfile

        with tempfile.TemporaryDirectory() as temporary:
            builder = WorkspaceBuilder(Path(temporary)).provenance()
            bundle = builder.output / "MS_DIAL_publication_reporting_bundle.zip"
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr("MS_DIAL_Materials_and_Methods.txt", "MS-DIAL 5.5 was used.")
                archive.writestr(
                    "Supplementary_Table_MS_DIAL.tsv",
                    "Library provenance\tLibrary 2\tpath\t"
                    r"C:\Users\Someone\AppData\Local\libraries\private-VS20.msp",
                )
            report = verifier.verify(builder.root, "before-publish")

        self.assertEqual(verifier.FAIL, _status(report, "SEC-1"))

    def test_a_clean_bundle_passes_and_says_what_the_pass_means(self) -> None:
        import zipfile

        with tempfile.TemporaryDirectory() as temporary:
            builder = WorkspaceBuilder(Path(temporary)).provenance()
            bundle = builder.output / "MS_DIAL_publication_reporting_bundle.zip"
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr(
                    "Supplementary_Table_MS_DIAL.tsv",
                    "Library provenance\tLibrary 2\tname\tprivate-VS20.msp\tsha256\tabc123",
                )
            report = verifier.verify(builder.root, "before-publish")

        check = [item for item in report.checks if item.check_id == "SEC-1"][0]
        self.assertEqual(verifier.PASS, check.status)
        self.assertIn("not that no private data is present", check.detail)


class TerminalStateAndRetentionTests(unittest.TestCase):
    """FIN-1 and RET-1. A finished unit, and a retention decision the disk agrees with."""

    def _workspace(self, temporary: str, **manifest_extra) -> Path:
        builder = WorkspaceBuilder(Path(temporary)).provenance()
        manifest_path = builder.root / "provenance" / "run-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(manifest_extra)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return builder.root

    def test_a_finalised_unit_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(
                temporary, status="mztab_validated", finalized_at="2026-09-20T10:00:00+09:00",
                mztab_validation={"summary": {"failed": 0}}, raw_retention_policy="keep")
            (workspace / "raw").mkdir()
            (workspace / "raw" / "data.bin").write_bytes(b"0")
            report = verifier.verify(workspace, "before-publish")

        self.assertEqual(verifier.PASS, _status(report, "FIN-1"))
        self.assertEqual(verifier.PASS, _status(report, "RET-1"))

    def test_a_publishable_output_beside_an_unfinalised_manifest_is_refused(self) -> None:
        """The output directory is not evidence of finalisation; the finalisation record is."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary, status="preflight_unavailable")
            # MPST000007's state: a complete mzTab-M and publication report, never finalised.
            (workspace / "output" / "AlignResult-1.mzTab").write_text("MTD\n", encoding="ascii")
            (workspace / "output" / "MS_DIAL_publication_report.json").write_text("{}", encoding="ascii")
            report = verifier.verify(workspace, "before-publish")

        self.assertEqual(verifier.FAIL, _status(report, "FIN-1"))

    def test_a_failed_run_is_refused_at_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(
                temporary, status="run_failed", run_failures=[{"reason": "exit 1"}])
            report = verifier.verify(workspace, "before-publish")

        self.assertEqual(verifier.FAIL, _status(report, "FIN-1"))

    def test_a_missing_retention_policy_is_refused(self) -> None:
        """The deletion preview reads this field and would show a blank where the intent should be."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary, status="mztab_validated")
            report = verifier.verify(workspace, "before-publish")

        self.assertEqual(verifier.FAIL, _status(report, "RET-1"))

    def test_a_policy_nobody_can_act_on_is_refused(self) -> None:
        """"delete" is not a policy this codebase has; it normalises to keep and warns."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary, raw_retention_policy="delete")
            report = verifier.verify(workspace, "before-publish")

        self.assertEqual(verifier.FAIL, _status(report, "RET-1"))

    def test_a_validated_unit_that_asked_for_deletion_and_still_holds_its_raw_warns(self) -> None:
        """Not a failure. Deletion needs its own confirmation, which may not have been given yet."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(
                temporary, status="mztab_validated",
                raw_retention_policy="delete_after_validated_output")
            (workspace / "raw").mkdir()
            (workspace / "raw" / "data.bin").write_bytes(b"0")
            report = verifier.verify(workspace, "before-publish")

        self.assertEqual(verifier.WARN, _status(report, "RET-1"))


class BinaryIdentityTests(unittest.TestCase):
    """BIN-1. The software the mzTab-M attributes is the software the run recorded."""

    def _workspace(self, temporary: str, *, recorded: str, attributed: str) -> Path:
        builder = WorkspaceBuilder(Path(temporary)).provenance()
        rows = _samples(6)
        builder.analysis_csv(rows)
        builder.run_manifest(rows)
        manifest_path = builder.output / "run-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["msdial_console_version"] = recorded
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        builder.mztab()
        mztab = builder.output / "AlignResult-1.mzTab"
        mztab.write_text(
            f"MTD\tsoftware[1]\t[MS, MS:1003082, MS-DIAL, {attributed}]\n"
            + mztab.read_text(encoding="ascii"),
            encoding="ascii")
        return builder.root

    def test_agreement_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(
                temporary, recorded="5.5.260916", attributed="Msdial console 5.5.260916")
            report = verifier.verify(workspace, "after-run")

        self.assertEqual(verifier.PASS, _status(report, "BIN-1"))

    def test_a_batch_split_across_two_binaries_is_caught(self) -> None:
        """A rebuild or a repointed console between one unit and the next looks exactly like this."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(
                temporary, recorded="5.5.241113", attributed="Msdial console 5.5.260916")
            report = verifier.verify(workspace, "after-run")

        self.assertEqual(verifier.FAIL, _status(report, "BIN-1"))


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
