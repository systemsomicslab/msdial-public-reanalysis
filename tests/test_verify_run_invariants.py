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
                    provenance_warning_for: str | None = None,
                    qa_prose: bool = True) -> "WorkspaceBuilder":
        checks = [
            {"metric": "median_qc_rsd_percent", "label": "Median QC feature RSD", "value": None,
             "status": "not_assessed"},
        ]
        if run_order_status is not None:
            checks.append({
                "metric": "run_order_intensity_correlation",
                "label": "Absolute run-order/intensity correlation",
                "value": 0.0747,
                "status": run_order_status,
            })
        warnings = []
        if provenance_warning_for:
            warnings.append(
                f"No persistent identifier was recorded for {provenance_warning_for}."
            )
        evaluated = [item["label"] for item in checks if item["status"] != "not_assessed"]
        passed = sum(1 for item in checks if item["status"] == "pass")
        (self.output / "MS_DIAL_publication_report.json").write_text(json.dumps({
            "qa_assessment": {
                "status": "pass",
                "passed": passed,
                "evaluated": len(evaluated),
                "checks": checks,
            },
            # The QA matrix summary the counts in the prose are compared with.
            "qa_report": {"summary": {"sample_count": 6, "alignment_spot_count": 3,
                                      "category_counts": {"Sample": 6, "QC": 0, "Blank": 0}}},
            "library_provenance_warnings": warnings,
        }), encoding="utf-8")
        if qa_prose:
            # What Interactive 0.5.1 and later write, so QA-1 has the prose it reads.
            if evaluated:
                lead = (f"Of {len(checks)} prespecified QA criteria, {len(evaluated)} could be evaluated "
                        f"({'; '.join(evaluated)}), and {passed} of them {'was' if passed == 1 else 'were'} met.")
            else:
                lead = f"None of the {len(checks)} prespecified QA criteria could be evaluated."
            (self.output / "MS_DIAL_QA_Results.txt").write_text(
                lead + " The other 1 (Median QC feature RSD) could not be assessed because the run had "
                "0 QC injection(s), where at least three are needed.\n", encoding="utf-8")
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

    def test_run_level_exports_are_not_counted_as_samples(self):
        # With automatic alignment RT correction (MsdialWorkbench #810), Interactive lists the two
        # audit TSVs among the expected exports. They are one per run, not one per file.
        audit = ("automatic_alignment_rt_correction_summary.tsv", "automatic_alignment_rt_correction_anchors.tsv")
        for name, keep in {"every file": 6, "a file skipped": 5}.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                rows = _samples(6)
                builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6)
                builder.analysis_csv(rows).run_manifest(rows)
                manifest_path = builder.output / "run-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                extra = [str(builder.output / item) for item in audit]
                manifest["expected_analysis_exports"] += extra
                manifest["expected_automatic_rt_correction_exports"] = extra
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                for path in extra:
                    Path(path).write_text("x\n", encoding="ascii")
                for row in rows[keep:]:
                    (builder.output / f"{row['file_name']}.mdpeak").unlink()
                report = verifier.verify(builder.root, "after-run")
                check = next(item for item in report.checks if item.check_id == "CNT-1")
                self.assertEqual(verifier.PASS if keep == 6 else verifier.FAIL, check.status, check.detail)
                self.assertEqual(sorted(audit), sorted(check.evidence["run_level_exports"]))
                self.assertEqual(verifier.PASS if keep == 6 else verifier.FAIL, _status(report, "EXP-1"))

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

    def test_files_of_different_classes_at_one_time_are_judged_by_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6, classes=2, grouped=True)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6).analysis_csv(rows)
            orders = {row["file_name"]: int(row["analytical_order"]) for row in rows}
            times = {name: f"2020-01-{order:02d}T00:00:00+00:00" for name, order in orders.items()}
            times["sample_4"] = times["sample_3"]  # the last Strain1 and the first Strain2
            _record_header_order(builder, orders, times=times)
            builder.publication(run_order_status="pass")
            self.assertEqual(verifier.WARN, _status(verifier.verify(builder.root, "before-production"), "ORD-1"))
            self.assertEqual(verifier.FAIL, _status(verifier.verify(builder.root, "before-publish"), "ORD-2"))

    def test_tied_rows_in_either_order_and_padded_ranks_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6, classes=2, grouped=False)
            orders = {row["file_name"]: int(row["analytical_order"]) for row in rows}
            times = {name: f"2020-01-{order:02d}T00:00:00+00:00" for name, order in orders.items()}
            times["sample_2"] = times["sample_4"] = "2020-01-02T00:00:00+00:00"  # same class, one time
            orders["sample_2"], orders["sample_3"], orders["sample_4"] = 3, 4, 2
            times["sample_3"] = "2020-01-03T00:00:00+00:00"
            for row in rows:
                row["analytical_order"] = f"{orders[row['file_name']]:02d}"
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6).analysis_csv(rows)
            _record_header_order(builder, orders, times=times)
            self.assertEqual(verifier.PASS, _status(verifier.verify(builder.root, "before-production"), "ORD-1"))

    def test_a_missing_file_is_found_even_when_it_shares_a_time(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6, classes=2, grouped=True)
            orders = {row["file_name"]: int(row["analytical_order"]) for row in rows}
            times = {name: f"2020-01-{order:02d}T00:00:00+00:00" for name, order in orders.items()}
            times["sample_6"] = times["sample_5"]
            rows = [row for row in rows if row["file_name"] != "sample_5"]
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6).analysis_csv(rows)
            _record_header_order(builder, orders, times=times)
            self.assertEqual(verifier.FAIL, _status(verifier.verify(builder.root, "before-production"), "ORD-1"))

    def test_mixed_offsets_and_partly_malformed_records_are_refused(self):
        rows = _samples(4, classes=2, grouped=False)
        orders = {row["file_name"]: int(row["analytical_order"]) for row in rows}
        tied = {name: "2020-01-01T00:00:00+00:00" for name in orders}
        cases = {
            "mixed offsets": dict(times={**{n: f"2020-01-0{o}T00:00:00+00:00" for n, o in orders.items()},
                                          "sample_1": "2020-01-01T00:00:00"}),
            "unreadable time among ties": dict(times={**tied, "sample_1": "yesterday"}),
        }
        for name, options in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                builder = WorkspaceBuilder(Path(directory)).provenance(inputs=4).analysis_csv(rows)
                _record_header_order(builder, orders, **options)
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

    # Interactive 0.5.4 records what the order was taken from, and withholds the run-order criterion
    # against the file listing (decided 2026-09-29).
    UNRECORDED = "the injection order was not recorded for every file"

    def _recorded(self, directory, source, *, withheld=False, asserted=True, edit_csv=False, checks=None):
        rows = _samples(6, classes=2, grouped=False)
        builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6).analysis_csv(rows)
        manifest_path = builder.root / "provenance" / "run-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["analytical_order"] = {
            "derived_from": None, "order_source": source,
            # An edited CSV here is one whose first two files swapped places since the record.
            "files": [{"file": f"{row['file_name']}.lcd",
                       "analytical_order": {1: 2, 2: 1}.get(int(row["analytical_order"]), int(row["analytical_order"]))
                       if edit_csv else int(row["analytical_order"])} for row in rows],
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        builder.publication(run_order_status="pass" if asserted else None)
        if withheld:
            path = builder.output / "MS_DIAL_publication_report.json"
            report_json = json.loads(path.read_text(encoding="utf-8"))
            report_json["qa_assessment"]["checks"].append({
                "metric": "run_order_intensity_correlation", "label": "Absolute run-order/intensity correlation",
                "value": None, "status": "not_assessed", "reason": self.UNRECORDED})
            path.write_text(json.dumps(report_json), encoding="utf-8")
        if checks is not None:
            path = builder.output / "MS_DIAL_publication_report.json"
            report_json = json.loads(path.read_text(encoding="utf-8"))
            report_json["qa_assessment"]["checks"] = checks
            path.write_text(json.dumps(report_json), encoding="utf-8")
        return next(item for item in verifier.verify(builder.root, "before-publish").checks if item.check_id == "ORD-2")

    def test_a_drift_metric_on_a_recorded_listing_is_refused_whatever_the_classes_do(self):
        # Interleaved, so the row-number heuristic alone would pass it.
        for source, words in {"listing": "file listing", "embedded": "file names"}.items():
            with self.subTest(source), tempfile.TemporaryDirectory() as directory:
                check = self._recorded(directory, source)
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn(words, check.detail)

    def test_a_recorded_listing_with_the_criterion_withheld_passes(self):
        for source in ("listing", "embedded"):
            with self.subTest(source), tempfile.TemporaryDirectory() as directory:
                check = self._recorded(directory, source, withheld=True, asserted=False)
                self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_withholding_the_criterion_for_an_order_that_was_recorded_is_refused(self):
        for source in ("repository_sample_table", "raw_header_acquisition_start_time"):
            with self.subTest(source), tempfile.TemporaryDirectory() as directory:
                check = self._recorded(directory, source, withheld=True, asserted=False)
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn(source, check.detail)

    def test_a_record_the_csv_no_longer_carries_is_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            check = self._recorded(directory, "listing", edit_csv=True)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIsNone(check.evidence.get("recorded_source"))

    def test_an_order_the_repository_declared_is_recorded_whatever_the_classes_do(self):
        # Grouped rows 1..N: the row-number rule alone would call the order synthesized.
        rows = _samples(6, classes=2, grouped=True)
        with tempfile.TemporaryDirectory() as directory:
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6).analysis_csv(rows)
            manifest_path = builder.root / "provenance" / "run-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["analytical_order"] = {"derived_from": None, "order_source": "repository_sample_table",
                                            "files": [{"file": f"{row['file_name']}.lcd",
                                                       "analytical_order": int(row["analytical_order"])} for row in rows]}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            builder.publication(run_order_status="pass")
            check = next(item for item in verifier.verify(builder.root, "before-publish").checks if item.check_id == "ORD-2")

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIn("sample table declares", check.detail)

    def test_a_dropped_file_or_renumbered_ranks_keep_the_recorded_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = _samples(6, classes=2, grouped=False)
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6)
            kept = [dict(row, analytical_order=str(int(row["analytical_order"]) * 10)) for row in rows[1:]]
            builder.analysis_csv(kept)
            manifest_path = builder.root / "provenance" / "run-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["analytical_order"] = {"derived_from": None, "order_source": "listing",
                                            "files": [{"file": f"{row['file_name']}.lcd",
                                                       "analytical_order": int(row["analytical_order"])} for row in rows]}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            builder.publication(run_order_status="pass")
            check = next(item for item in verifier.verify(builder.root, "before-publish").checks if item.check_id == "ORD-2")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertEqual("listing", check.evidence["recorded_source"])

    def test_a_report_whose_checks_are_not_a_list_is_not_read(self):
        for checks in (5, True, {"run_order_intensity_correlation": {}}):
            with self.subTest(checks=checks), tempfile.TemporaryDirectory() as directory:
                check = self._recorded(directory, "listing", checks=checks)
                self.assertEqual(verifier.NOT_EVALUABLE, check.status, check.detail)

    def test_a_rank_int_does_not_read_is_not_a_rank(self):
        self.assertIsNone(verifier._as_rank("²"))
        self.assertEqual(3, verifier._as_rank(" 3 "))

    def test_qa1_knows_the_reason_and_what_it_is_the_reason_for(self):
        self.assertEqual(("unrecorded order", None), verifier._qa_reason_kind(self.UNRECORDED + "."))
        self.assertEqual({"run_order_intensity_correlation"}, verifier.QA_REASON_METRICS["unrecorded order"])


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

    def test_prose_naming_the_unassessable_qa_criteria_passes(self):
        # QA-1 used to WARN on every run with an unassessable criterion, whatever its prose said.
        # It reads the prose now; tests/test_verify_qa_prose.py holds the cases.
        with tempfile.TemporaryDirectory() as directory:
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6)
            builder.publication(run_order_status="pass")
            report = verifier.verify(builder.root, "before-publish")
            self.assertEqual(verifier.PASS, _status(report, "QA-1"))

    def test_qa_with_no_prose_to_compare_is_not_evaluable(self):
        with tempfile.TemporaryDirectory() as directory:
            builder = WorkspaceBuilder(Path(directory)).provenance(inputs=6)
            builder.publication(run_order_status="pass", qa_prose=False)
            report = verifier.verify(builder.root, "before-publish")
            self.assertEqual(verifier.NOT_EVALUABLE, _status(report, "QA-1"))


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


class AbstentionTests(unittest.TestCase):
    """CLS-3 and B3 for a unit whose Catalog selection abstained (decided 2026-09-28).

    The abstention is saved and ratified like a proposal: no field selected, every sample in the one
    Class "All", the reason in the contrast definition.
    """

    def _workspace(self, temporary: str, *, labels=None, fields=None, status="accepted",
                   contrast=None, unit_id=None, split_from=None) -> Path:
        rows = _samples(4)
        for row in rows:
            row["class_id"] = "All"
        labels = labels or ["All"] * len(rows)
        builder = WorkspaceBuilder(Path(temporary)).provenance(inputs=len(rows))
        manifest_path = builder.root / "provenance" / "run-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["project"]["class_proposal"] = {
            "proposal_id": "a1", "status": status, "model": "catalog-declared-factor-selection",
            "selected_fields": fields or [],
            "contrast_definition": contrast if contrast is not None else
            {"kind": "abstention", "reason": "no_usable_declared_factor", "class_label": "All"},
            "assignments": [{"sample_id": row["file_name"], "class_label": label} for row, label in zip(rows, labels)],
            "warnings": ["No Class was proposed: factors were declared but none of them groups these samples."],
        }
        if unit_id is not None:
            manifest["project"]["class_proposal"]["unit_id"] = unit_id
        if split_from is not None:
            manifest["split_from"] = split_from
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        builder.analysis_csv(rows)
        return builder.root

    def _b3(self, workspace: Path) -> bool:
        manifest = json.loads((workspace / "provenance" / "run-manifest.json").read_text(encoding="utf-8"))
        return verifier.completion_progress(workspace, manifest, workspace / "output")["stages"]["B3"]

    def test_a_ratified_abstention_settles_the_class(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary)
            report = verifier.verify(workspace, "before-production")
            settled = self._b3(workspace)
        with tempfile.TemporaryDirectory() as temporary:
            unsettled = self._b3(self._workspace(temporary, status="proposed"))

        check = next(item for item in report.checks if item.check_id == "CLS-3")
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIn("ratified abstention (no_usable_declared_factor)", check.detail)
        self.assertEqual(verifier.PASS, _status(report, "CLS-2"))
        # B3 is reached on the ratified abstention and not on the same record unratified.
        self.assertTrue(settled)
        self.assertFalse(unsettled)

    def test_an_abstention_whose_class_is_not_the_one_it_names_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = verifier.verify(self._workspace(temporary, labels=["Control"] * 4), "before-production")

        check = next(item for item in report.checks if item.check_id == "CLS-3")
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("is not the Class it names", check.detail)

    def test_an_abstention_for_a_reason_the_catalog_does_not_give_warns(self) -> None:
        for name, contrast in {
            "no reason": {"kind": "abstention", "class_label": "All"},
            "an unknown reason": {"kind": "abstention", "reason": "agent_preferred_none", "class_label": "All"},
        }.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                report = verifier.verify(self._workspace(temporary, contrast=contrast), "before-production")
                self.assertEqual(verifier.WARN, _status(report, "CLS-3"))

    def test_a_class_record_saved_for_another_unit_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = verifier.verify(self._workspace(temporary, unit_id="0123456789abcdef0123"), "before-production")

        check = next(item for item in report.checks if item.check_id == "CLS-3")
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("settles nothing here", check.detail)

    def test_a_split_part_carries_its_parents_class_record(self) -> None:
        for name, options in {
            "this unit's record": dict(unit_id=UNIT_ID),
            "the parent's record": dict(unit_id="parent00000000000000",
                                        split_from={"analysis_unit_id": "parent00000000000000"}),
        }.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                report = verifier.verify(self._workspace(temporary, **options), "before-production")
                self.assertEqual(verifier.PASS, _status(report, "CLS-3"))

    def _published(self, temporary: str, *, run_order_status) -> Path:
        workspace = self._workspace(temporary)
        builder = WorkspaceBuilder.__new__(WorkspaceBuilder)
        builder.root = workspace
        builder.publication(run_order_status=run_order_status)
        return workspace

    def test_a_drift_metric_on_one_classs_row_number_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = verifier.verify(self._published(temporary, run_order_status="pass"), "before-publish")

        check = next(item for item in report.checks if item.check_id == "ORD-2")
        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("statement about file order", check.detail)
        self.assertNotIn("not a restatement", check.detail)

    def test_one_classs_row_number_with_no_drift_metric_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = verifier.verify(self._published(temporary, run_order_status=None), "before-publish")

        check = next(item for item in report.checks if item.check_id == "ORD-2")
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIn("is the row number", check.detail)

    def test_an_abstention_that_groups_the_samples_is_refused(self) -> None:
        for name, options in {"two Classes": dict(labels=["All", "All", "All", "Other"]),
                              "a field selected": dict(fields=["Factor Value[Batch]"])}.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                report = verifier.verify(self._workspace(temporary, **options), "before-production")
                self.assertEqual(verifier.FAIL, _status(report, "CLS-3"))

    def test_an_unratified_abstention_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = verifier.verify(self._workspace(temporary, status="proposed"), "before-production")

        self.assertEqual(verifier.FAIL, _status(report, "CLS-3"))


class ThresholdProvenanceTests(unittest.TestCase):
    """PKH-1. The threshold the Console reads is one this unit's own diagnostic produced."""

    def _workspace(self, temporary: str, *, diagnostics: list[dict] | None,
                   written: str | None, method_text: str | None = None) -> Path:
        builder = WorkspaceBuilder(Path(temporary)).provenance()
        manifest_path = builder.root / "provenance" / "run-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if diagnostics is not None:
            manifest["peak_height_diagnostics"] = diagnostics
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        if method_text is not None:
            (builder.output / "method.txt").write_text(method_text, encoding="utf-8")
        elif written is not None:
            (builder.output / "method.txt").write_text(
                f"Smoothing method: LinearWeightedMovingAverage\nMinimum peak height: {written}\n",
                encoding="utf-8")
        return builder.root

    def _pkh1(self, method_text: str, measured: int = 500):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary, diagnostics=[{"minimum_peak_height": measured}],
                                        written=None, method_text=method_text)
            report = verifier.verify(workspace, "before-production")
        return next(item for item in report.checks if item.check_id == "PKH-1")

    def test_the_threshold_is_the_one_the_console_applies(self) -> None:
        """The Console applies the last value it can read; the first line is not the one that runs."""
        cases = {
            "measured first, another later": ("Minimum peak height: 500\nMinimum peak height: 700\n", verifier.FAIL),
            "another first, measured later": ("Minimum peak height: 700\nMinimum peak height: 500\n", verifier.WARN),
            "separated by '='": ("Minimum peak height = 500\n", verifier.PASS),
            "a space before the colon": ("Minimum peak height : 500\n", verifier.PASS),
            "a blank line after it": ("Minimum peak height: 500\nMinimum peak height:\n", verifier.PASS),
            "an unreadable line after it": ("Minimum peak height: 500\nMinimum peak height: high\n", verifier.WARN),
            "a comment after it": ("Minimum peak height: 500\n# Minimum peak height: 700\n", verifier.PASS),
            "quoted": ('Minimum peak height: "500"\n', verifier.PASS),
            "stated twice alike": ("Minimum peak height: 500\nMinimum peak height: 500\n", verifier.PASS),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name):
                check = self._pkh1(text)
                self.assertEqual(expected, check.status, check.detail)

    def test_a_first_line_that_does_not_run_is_named(self) -> None:
        check = self._pkh1("Minimum peak height: 500\nMinimum peak height: 700\n")

        self.assertEqual("700", check.evidence["method_threshold"])
        self.assertEqual(["500", "700"], check.evidence["stated"])

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

    def test_a_key_record_of_another_method_file_is_not_read(self) -> None:
        import hashlib
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._workspace(temporary, None)
            method = workspace / "output" / "method.txt"
            method.write_text("Minimum peak height: 500\n", encoding="utf-8")
            own = hashlib.sha256(method.read_bytes()).hexdigest()
            outcomes = {}
            for name, record in {
                "this file": {"schema": "msdial-method-file-keys.v1", "method_file_sha256": own,
                              "applied": ["Minimum peak height"], "unusable": [], "unrecognised": []},
                "another file": {"schema": "msdial-method-file-keys.v1", "method_file_sha256": "0" * 64,
                                 "applied": ["Minimum peak height"], "unusable": [], "unrecognised": []},
                "another schema": {"schema": "msdial-method-file-keys.v2", "method_file_sha256": own,
                                   "applied": ["Minimum peak height"], "unusable": [], "unrecognised": []},
            }.items():
                (workspace / "output" / "method.keys.json").write_text(json.dumps(record), encoding="utf-8")
                outcomes[name] = _status(verifier.verify(workspace, "after-run"), "MTH-1")

        self.assertEqual(verifier.PASS, outcomes["this file"])
        self.assertEqual(verifier.NOT_EVALUABLE, outcomes["another file"])
        self.assertEqual(verifier.NOT_EVALUABLE, outcomes["another schema"])

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
            # The run's own record of its library, which every run writes and SEC-1 reads to know what a
            # private library would look like. Without one it cannot pass; see SharedArtifactTests.
            (builder.output / "workflow-settings.json").write_text(json.dumps({"library_provenance": [
                {"path": "Q:\\libraries\\private-VS20.msp", "license": "institutional/private"}]}), encoding="utf-8")
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


# A private library as the production one is kept: outside any user profile, on a drive of its own,
# under a directory with a space in its name. Synthetic, like the library's name and bytes: no real
# location or file name of a private library belongs in a test.
PRIVATE_MSP = "Q:\\SyntheticLab Libraries\\msp\\Synthetic-Private-pos.msp"
PRIVATE_BYTES = b"NAME: synthetic compound\nPRECURSORMZ: 100.0\nNum Peaks: 0\n\n"
PUBLIC_LBM = "Q:\\public-libraries\\1234\\Synthetic-Public.lbm2"
POLICY = "msdial-interactive.shared-paths.v1"


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _zip(path: Path, members: dict[str, "str | bytes"]) -> Path:
    import zipfile

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def _workbook(**sheets: str) -> bytes:
    """The shape of an xlsx that matters here: a zip whose sheet XML holds the cell text."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                         'package/2006/content-types"/>')
        for name, text in sheets.items():
            archive.writestr(f"xl/worksheets/{name}.xml",
                             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                             'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                             f'<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>{text}</t></is></c></row>'
                             "</sheetData></worksheet>")
    return buffer.getvalue()


class SharedArtifactTests(unittest.TestCase):
    """SEC-1, widened. A private library kept off the user profile, and every artifact that leaves.

    The check used to match only a user-profile path in four publication files, so a library kept on a
    data drive or a share passed it wherever its location was written: the Console's mzTab-M, the
    workflow bundle, the workbook inside the publication bundle. It now knows each private library by
    the unit's own records, reads every artifact built to be shared, and grades any other absolute path
    by whether the artifact declares Interactive's shared-path policy.
    """

    def _unit(self, temporary: str, *, private: "dict | None" = None, settings_extra: "dict | None" = None,
              policy: bool = False, report_extra: "dict | None" = None) -> WorkspaceBuilder:
        builder = WorkspaceBuilder(Path(temporary)).provenance()
        private_record = private if private is not None else {
            "path": PRIVATE_MSP, "version": "", "source": "", "doi": "", "license": "institutional/private"}
        public_record = {"path": PUBLIC_LBM, "version": "1234", "source": "https://zenodo.org/records/1234",
                         "doi": "10.5281/zenodo.1234", "license": "CC BY 4.0"}
        settings = {"library_provenance": [private_record, public_record], "lbm_path": PUBLIC_LBM,
                    "msp_annotators": [{"msp_file_path": private_record.get("path", PRIVATE_MSP)}]}
        settings.update(settings_extra or {})
        (builder.output / "workflow-settings.json").write_text(json.dumps(settings), encoding="utf-8")
        (builder.output / "run-manifest.json").write_text(json.dumps({"libraries": [
            dict(private_record, sha256=_sha256(PRIVATE_BYTES), size=len(PRIVATE_BYTES)),
            dict(public_record, sha256="ab" * 32, size=785186493),
        ]}), encoding="utf-8")
        record = {"shared_path_policy": POLICY} if policy else {}
        record.update(report_extra or {})
        (builder.output / "MS_DIAL_publication_report.json").write_text(json.dumps(record), encoding="utf-8")
        return builder

    def _sec1(self, builder: WorkspaceBuilder):
        report = verifier.verify(builder.root, "before-publish")
        matching = [check for check in report.checks if check.check_id == "SEC-1"]
        self.assertEqual(1, len(matching))
        return matching[0]

    @staticmethod
    def _mztab(builder: WorkspaceBuilder, *, database_uri: str, location: str = "null",
               smiles: str = "CCOC1=C(O)C=C(\\C=C\\C)C=C1", extra: str = "null") -> None:
        (builder.output / "AlignResult-1.mzTab").write_text("\n".join([
            "MTD\tmzTab-version\t2.0.0-M",
            f"MTD\tms_run[1]-location\t{location}",
            "MTD\tdatabase[1]\t[,, User-defined MSP library file, ]",
            "MTD\tdatabase[1]-prefix\tMspDB_1_Synthetic-Private-pos",
            "MTD\tdatabase[1]-version\tSynthetic-Private-pos.msp",
            f"MTD\tdatabase[1]-uri\t{database_uri}",
            f"MTD\tcustom[1]\t[,, library file sha256, Synthetic-Private-pos.msp sha256:{_sha256(PRIVATE_BYTES)}]",
            "SMH\tSML_ID\tdatabase_identifier\tchemical_formula\tsmiles\tinchi\tchemical_name\topt_global_note",
            f"SML\t1\tnull\tC11H14O2\t{smiles}\tInChI=1S/C11H14O2/c1-3-5-9-6-7-10(12)11(8-9)13-4-2/h3,5-8,12H,4H2,1-2H3"
            f"\ttrans-2-Ethoxy-5-(1-propenyl)phenol\t{extra}",
        ]) + "\n", encoding="utf-8")

    def _assert_quotes_no_location(self, check) -> None:
        """The gate's report is itself a record that may be kept and shared."""
        written = json.dumps(check.as_dict()).casefold()
        for fragment in ("syntheticlab", "synthetic%20lab", "q:\\\\", "q:/"):
            self.assertNotIn(fragment, written)

    # -- patterns -------------------------------------------------------------------------------------

    def test_every_encoding_of_an_absolute_path_is_recognised(self) -> None:
        cases = {
            "drive, raw": ("drive-letter path", "D:\\x\\y.msp"),
            "drive, JSON-escaped": ("drive-letter path", '"D:\\\\x\\\\y"'),
            "drive, forward slashes": ("drive-letter path", "D:/x/y"),
            "drive, percent-encoded": ("drive-letter path", "D%3A%5Cx"),
            "file URI, the Console's form": ("file URI", "file://D:/x/y.msp"),
            "file URI to a share": ("file URI", "file:////nas/share/x"),
            "UNC, raw": ("UNC path", "\\\\nas\\share\\x.msp"),
            "UNC, JSON-escaped": ("UNC path", '"\\\\\\\\nas\\\\share\\\\x.msp"'),
            "UNC, after a tab": ("UNC path", "path\t\\\\nas\\share\\x.msp"),
            "smb": ("UNC path", "smb://nas/x"),
        }
        for name, (kind, text) in cases.items():
            with self.subTest(name):
                self.assertEqual([kind], [found for found, _match in verifier._sec1_paths(text)])

    def test_structures_identifiers_and_a_named_library_are_not_paths(self) -> None:
        smiles = "C/C=C\\C(\\C\\CC"
        for text in (smiles, json.dumps({"smiles": smiles}), f"SML\t1\t{smiles}\tnull",
                     "InChI=1S/C11H14O2/c1-3-5-9-6-7-10(12)11(8-9)13-4-2/h3,5-8,12H,4H2,1-2H3/b5-3+",
                     "https://doi.org/10.5281/zenodo.21904103", "[MS, MS:1003082, MS-DIAL, 5.5.260926]",
                     "MSMS-Public_all-neg-VS20.msp  sha256:0123456789abcdef  41230 records",
                     '<worksheet xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'):
            with self.subTest(text):
                self.assertEqual([], verifier._sec1_paths(text))

    # -- a private library --------------------------------------------------------------------------

    def test_a_private_library_location_in_the_mztab_database_uri_is_refused(self) -> None:
        """What the pinned Console writes for every library it loaded, in every run."""
        with tempfile.TemporaryDirectory() as temporary:
            builder = self._unit(temporary, policy=True)
            self._mztab(builder, database_uri="file://Q:/SyntheticLab Libraries/msp/Synthetic-Private-pos.msp")
            check = self._sec1(builder)

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("AlignResult-1.mzTab: the location of private library Synthetic-Private-pos.msp",
                      " ".join(check.evidence["fails"]))
        self._assert_quotes_no_location(check)

    def test_the_same_unit_with_the_location_redacted_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            builder = self._unit(temporary, policy=True, report_extra={"libraries": [
                {"filename": "Synthetic-Private-pos.msp", "sha256": _sha256(PRIVATE_BYTES), "distribution": "private"}]})
            self._mztab(builder, database_uri="null",
                        location="https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public/MTBLS1/a.mzML")
            (builder.output / "MS_DIAL_Materials_and_Methods.txt").write_text(
                "Spectra were searched against Synthetic-Private-pos.msp, a private laboratory library that is not "
                f"distributed (SHA-256 {_sha256(PRIVATE_BYTES)}).\n", encoding="utf-8")
            check = self._sec1(builder)

        self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_the_location_is_refused_in_every_encoding_a_writer_uses(self) -> None:
        import urllib.parse

        forward = PRIVATE_MSP.replace("\\", "/")
        forms = {
            "backslashes": PRIVATE_MSP,
            "doubled backslashes": json.dumps({"msp_file_path": PRIVATE_MSP}),
            "forward slashes": forward,
            "file URI": "file:///" + forward.replace(" ", "%20"),
            "%20": PRIVATE_MSP.replace(" ", "%20"),
            "percent-encoded": urllib.parse.quote(PRIVATE_MSP, safe=""),
            "upper case": PRIVATE_MSP.upper(),
        }
        for name, text in forms.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                builder = self._unit(temporary)
                (builder.output / "Supplementary_Table_MS_DIAL.tsv").write_text(
                    f"Library provenance\tLibrary 1\tpath\t{text}\n", encoding="utf-8")
                check = self._sec1(builder)

                # Refused although the table predates the policy: a private location is never graded.
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn("the location of private library Synthetic-Private-pos.msp", check.detail)
                self._assert_quotes_no_location(check)

    def test_the_location_is_found_in_the_workflow_bundle_and_the_nested_workbook(self) -> None:
        places = {
            "workflow bundle": ("msdial-workflow-bundle.zip",
                                {"msp_annotator_settings.tsv": f"msp_file_path\n{PRIVATE_MSP}\n"}),
            "workbook in the bundle": ("MS_DIAL_publication_reporting_bundle.zip",
                                       {"Supplementary_Table_MS_DIAL.xlsx": _workbook(sheet3=PRIVATE_MSP)}),
        }
        for name, (bundle, members) in places.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                builder = self._unit(temporary)
                _zip(builder.output / bundle, members)
                check = self._sec1(builder)

                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn("the location of private library", check.detail)

    def test_the_private_file_name_inside_a_path_is_refused_and_the_bare_name_is_not(self) -> None:
        cases = {
            "a relative path": ("libs\\Synthetic-Private-pos.msp", verifier.FAIL),
            "beside the method": ("./Synthetic-Private-pos.msp", verifier.FAIL),
            "a bare name and checksum": (f"Synthetic-Private-pos.msp\tsha256:{_sha256(PRIVATE_BYTES)}", verifier.PASS),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                builder = self._unit(temporary, policy=True)
                (builder.output / "Supplementary_Table_MS_DIAL.tsv").write_text(
                    f"Library provenance\tLibrary 1\tname\t{text}\n", encoding="utf-8")
                check = self._sec1(builder)

                self.assertEqual(expected, check.status, check.detail)

    def test_the_directory_of_a_private_library_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            builder = self._unit(temporary)
            (builder.output / "MS_DIAL_Materials_and_Methods.txt").write_text(
                "Libraries were read from Q:\\SyntheticLab Libraries\\msp.\n", encoding="utf-8")
            check = self._sec1(builder)

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("the directory of private library Synthetic-Private-pos.msp", check.detail)

    def test_a_directory_shared_with_a_public_library_says_nothing_about_the_private_one(self) -> None:
        """Otherwise every path to the public library would be refused as the private one's."""
        public = "Q:\\SyntheticLab Libraries\\msp\\zenodo\\Synthetic-Public.msp"
        with tempfile.TemporaryDirectory() as temporary:
            builder = self._unit(temporary, settings_extra={"library_provenance": [
                {"path": PRIVATE_MSP, "license": "institutional/private"},
                {"path": public, "doi": "10.5281/zenodo.1", "license": "CC BY 4.0"}]})
            (builder.output / "Supplementary_Table_MS_DIAL.tsv").write_text(
                f"Library provenance\tLibrary 2\tpath\t{public}\n", encoding="utf-8")
            check = self._sec1(builder)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual(1, check.evidence["directory_rules_not_used"])

    def test_a_library_that_records_no_doi_or_source_is_private(self) -> None:
        """Interactive's default licence for a user's own library is 'institutional/private', and a record
        that says nothing is no evidence that the library may be shared."""
        records = {
            "no licence, no identifier": {"path": PRIVATE_MSP},
            "a public-sounding licence but no identifier": {"path": PRIVATE_MSP, "license": "CC BY 4.0"},
            "an identifier and a private distribution": {"path": PRIVATE_MSP, "doi": "10.1/x",
                                                         "distribution": "private"},
        }
        for name, record in records.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                builder = self._unit(temporary, private=record)
                (builder.output / "Supplementary_Table_MS_DIAL.tsv").write_text(PRIVATE_MSP + "\n", encoding="utf-8")
                check = self._sec1(builder)

                self.assertEqual(verifier.FAIL, check.status, check.detail)

    def test_a_library_loaded_with_no_provenance_record_is_private(self) -> None:
        """library_provenance can lag the libraries the Console was told to load."""
        loaded = "Q:\\SyntheticLab Libraries\\msp\\Synthetic-Private-neg.msp"
        with tempfile.TemporaryDirectory() as temporary:
            builder = self._unit(temporary, settings_extra={"msp_annotators": [{"msp_file_path": loaded}]})
            (builder.output / "Supplementary_Table_MS_DIAL.tsv").write_text(loaded + "\n", encoding="utf-8")
            check = self._sec1(builder)

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("private library Synthetic-Private-neg.msp", check.detail)

    # -- library content in a bundle ---------------------------------------------------------------

    def test_a_library_file_or_the_consoles_copy_of_one_in_any_bundle_is_refused(self) -> None:
        cases = {
            "the Console's copy": ("MS_DIAL_publication_reporting_bundle.zip", "Project-1_Loaded.msp2.dbs"),
            "an MSP": ("MS_DIAL_publication_reporting_bundle.zip", "libs/Synthetic-Private-pos.msp"),
            "an LBM": ("msdial-workflow-bundle.zip", "Synthetic-Public.lbm2"),
            "a local bundle": ("msdial-project-artifacts.zip", "Project-1_Loaded.msp2.dbs"),
        }
        for name, (bundle, member) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                builder = self._unit(temporary, policy=True)
                _zip(builder.output / bundle, {member: b"\x00binary"})
                check = self._sec1(builder)

                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn("a library file, or MS-DIAL's copy of one, as a member", check.detail)

    def test_a_local_only_file_is_refused_in_a_shared_bundle_but_not_in_a_local_one(self) -> None:
        for bundle, expected in (("MS_DIAL_publication_reporting_bundle.zip", verifier.FAIL),
                                 ("msdial-workflow-bundle.zip", verifier.FAIL),
                                 ("msdial-project-artifacts.zip", verifier.PASS)):
            with self.subTest(bundle), tempfile.TemporaryDirectory() as temporary:
                builder = self._unit(temporary, policy=True)
                _zip(builder.output / bundle, {"Project-1.mdproject": b"\x00", "datamining-handoff.json": "{}"})
                check = self._sec1(builder)

                self.assertEqual(expected, check.status, check.detail)

    def test_a_member_holding_a_librarys_bytes_is_refused_under_any_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            builder = self._unit(temporary, policy=True)
            _zip(builder.output / "MS_DIAL_publication_reporting_bundle.zip", {"supplement/data.bin": PRIVATE_BYTES})
            check = self._sec1(builder)

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("the content of library Synthetic-Private-pos.msp", check.detail)

    # -- the policy, and artifacts written before it ---------------------------------------------------

    def test_a_path_in_the_nested_workbook_warns_in_a_legacy_bundle_and_fails_under_the_policy(self) -> None:
        for policy, expected in ((False, verifier.WARN), (True, verifier.FAIL)):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as temporary:
                builder = self._unit(temporary, policy=policy)
                _zip(builder.output / "MS_DIAL_publication_reporting_bundle.zip",
                     {"Supplementary_Table_MS_DIAL.xlsx": _workbook(sheet3="D:\\data\\sample.mzML")})
                check = self._sec1(builder)

                self.assertEqual(expected, check.status, check.detail)
                self.assertIn("Supplementary_Table_MS_DIAL.xlsx:xl/worksheets/sheet3.xml: drive-letter path",
                              " ".join(check.evidence.get("fails", []) + check.evidence.get("warns", [])))

    def test_the_top_level_workbook_and_qa_text_follow_the_report(self) -> None:
        artifacts = {"Supplementary_Table_MS_DIAL.xlsx": _workbook(sheet1="D:\\data\\sample.mzML"),
                     "MS_DIAL_QA_Results.txt": "The QA matrix is D:\\ws\\output\\AlignResult-1.qa.tsv.\n"}
        for name, content in artifacts.items():
            for policy, expected in ((False, verifier.WARN), (True, verifier.FAIL)):
                with self.subTest(name, policy=policy), tempfile.TemporaryDirectory() as temporary:
                    builder = self._unit(temporary, policy=policy)
                    if isinstance(content, bytes):
                        (builder.output / name).write_bytes(content)
                    else:
                        (builder.output / name).write_text(content, encoding="utf-8")
                    check = self._sec1(builder)

                    self.assertEqual(expected, check.status, check.detail)

    def test_the_workflow_bundle_declares_the_policy_for_itself(self) -> None:
        """It is written before the run, long before the report that declares for the publication files."""
        for declared, expected in ((False, verifier.WARN), (True, verifier.FAIL)):
            with self.subTest(declared=declared), tempfile.TemporaryDirectory() as temporary:
                builder = self._unit(temporary)
                members = {"analysis_files.csv": "file_path,file_name\nD:\\ws\\raw\\data\\a.mzML,a\n"}
                if declared:
                    members["SHARED-PATHS.json"] = json.dumps({"policy": POLICY, "libraries": []})
                _zip(builder.output / "msdial-workflow-bundle.zip", members)
                check = self._sec1(builder)

                self.assertEqual(expected, check.status, check.detail)

    def test_unc_paths_warn_in_a_legacy_artifact_and_fail_under_the_policy(self) -> None:
        texts = {"JSON": json.dumps({"note": "\\\\synthetic-host\\share\\msp\\x.msp"}),
                 "TSV": "path\t\\\\synthetic-host\\share\\msp\\x.msp\n",
                 "file URI": "file:////synthetic-host/share/msp/x.msp\n"}
        for name, text in texts.items():
            for policy, expected in ((False, verifier.WARN), (True, verifier.FAIL)):
                with self.subTest(name, policy=policy), tempfile.TemporaryDirectory() as temporary:
                    builder = self._unit(temporary, policy=policy)
                    (builder.output / "Supplementary_Table_MS_DIAL.tsv").write_text(text, encoding="utf-8")
                    check = self._sec1(builder)

                    self.assertEqual(expected, check.status, check.detail)

    def test_anything_placed_in_share_is_held_to_the_policy(self) -> None:
        cases = {"a residual path": ("results.mzTab", "MTD\tms_run[1]-location\tfile://D:/ws/raw/a.mzML\n"),
                 "a library": ("Synthetic-Public.lbm2", "\x00")}
        for name, (file_name, text) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                builder = self._unit(temporary)
                (builder.root / "share").mkdir()
                (builder.root / "share" / file_name).write_text(text, encoding="utf-8")
                check = self._sec1(builder)

                self.assertEqual(verifier.FAIL, check.status, check.detail)

    def test_a_user_profile_path_is_refused_in_a_legacy_mztab_without_naming_the_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            builder = self._unit(temporary)
            self._mztab(builder, database_uri="file:///C:/Users/Someone/AppData/Local/libraries/public.msp")
            check = self._sec1(builder)

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("AlignResult-1.mzTab: user-profile path", check.detail)
        self.assertNotIn("someone", json.dumps(check.as_dict()).casefold())

    # -- the mzTab-M and the handoff -------------------------------------------------------------------

    def test_the_mztab_structure_columns_are_not_read(self) -> None:
        """Belt and braces: the patterns already pass a SMILES; a value the Console never writes as a
        structure is used here to show the column is not read at all, and the row's other columns are."""
        with tempfile.TemporaryDirectory() as temporary:
            builder = self._unit(temporary, policy=True)
            self._mztab(builder, database_uri="null", smiles="\\\\looks\\like-a-share")
            clean = self._sec1(builder)
            self._mztab(builder, database_uri="null", extra="\\\\synthetic-host\\share\\x.msp")
            other = self._sec1(builder)

        self.assertEqual(verifier.PASS, clean.status, clean.detail)
        self.assertEqual(verifier.WARN, other.status, other.detail)

    def test_the_consoles_file_uris_warn_because_nothing_in_the_mztab_can_declare_a_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            builder = self._unit(temporary, policy=True)
            self._mztab(builder, database_uri="null", location="file://D:/ws/raw/data/a.mzML")
            check = self._sec1(builder)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("AlignResult-1.mzTab: file URI (e.g. file://.../a.mzML)", check.evidence["warns"])

    def test_the_handoff_is_read_for_a_private_library_only(self) -> None:
        """It hands the next agent this machine's paths on purpose; a private library is not one of them."""
        for with_library, expected in ((False, verifier.PASS), (True, verifier.FAIL)):
            with self.subTest(with_library=with_library), tempfile.TemporaryDirectory() as temporary:
                builder = self._unit(temporary)
                handoff = {"job": {"run_directory": "D:\\ws\\output", "log_tail": ["Loaded D:\\ws\\output\\a.mdpeak"]}}
                if with_library:
                    handoff["job"]["log_tail"].append(f"Loaded {PRIVATE_MSP}")
                (builder.output / "datamining-handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
                (builder.output / "MS_DIAL_publication_report.json").unlink()
                check = self._sec1(builder)

                self.assertEqual(expected, check.status, check.detail)

    # -- what could not be applied ---------------------------------------------------------------------

    def test_unreadable_library_records_warn_that_the_private_library_rules_were_not_applied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            builder = self._unit(temporary, policy=True)
            (builder.output / "workflow-settings.json").write_text("{not json", encoding="utf-8")
            (builder.output / "run-manifest.json").unlink()
            (builder.output / "Supplementary_Table_MS_DIAL.tsv").write_text("clean\n", encoding="utf-8")
            check = self._sec1(builder)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("the private-library rules were not applied", check.detail)

    def test_no_library_record_at_all_is_not_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            builder = WorkspaceBuilder(Path(temporary)).provenance()
            (builder.output / "Supplementary_Table_MS_DIAL.tsv").write_text("clean\n", encoding="utf-8")
            check = self._sec1(builder)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("neither workflow-settings.json nor run-manifest.json", check.detail)

    # -- regression ------------------------------------------------------------------------------------

    def test_an_mtbls2207_shaped_legacy_unit_warns_and_does_not_fail(self) -> None:
        """MTBLS2207's two parts: public libraries with DOIs, and pre-policy artifacts full of drive paths.

        The real parts must still exit 0 under --stage all --strict; this is their shape, kept here so the
        suite holds it without the data.
        """
        raw = "D:\\analysis\\metabolights\\MTBLS2207\\unit\\raw\\data\\NIST1950_neg_ID_01.mzML"
        msp = "D:\\analysis-libraries\\21904103\\MSMS-Public_all-neg-VS20.msp"
        lbm = "D:\\analysis-libraries\\21904324\\Synthetic-LC25.lbm2"
        records = [{"path": msp, "version": "21904103", "source": "https://zenodo.org/records/21904103",
                    "doi": "10.5281/zenodo.21904103", "license": "CC BY 4.0"},
                   {"path": lbm, "version": "21904324", "source": "https://zenodo.org/records/21904324",
                    "doi": "10.5281/zenodo.21904324", "license": "CC BY 4.0"}]
        with tempfile.TemporaryDirectory() as temporary:
            builder = WorkspaceBuilder(Path(temporary)).provenance()
            output = builder.output
            settings = {"library_provenance": records, "lbm_path": lbm, "msp_annotators": [{"msp_file_path": msp}],
                        "console_path": "D:\\0_SourceCode\\console\\MSDIALCUI.exe", "files": [{"file_path": raw}]}
            (output / "workflow-settings.json").write_text(json.dumps(settings), encoding="utf-8")
            (output / "run-manifest.json").write_text(json.dumps({"libraries": [
                dict(records[0], sha256="55" * 32, size=50818071), dict(records[1], sha256="7c" * 32, size=785186493)]}),
                encoding="utf-8")
            report = json.dumps({"workflow": settings, "library_provenance_warnings": []})
            table = f"Data\tfile_path\t{raw}\tLocal absolute path; review before publication\nLibrary\tpath\t{msp}\n"
            (output / "MS_DIAL_publication_report.json").write_text(report, encoding="utf-8")
            (output / "Supplementary_Table_MS_DIAL.tsv").write_text(table, encoding="utf-8")
            (output / "Supplementary_Table_MS_DIAL.xlsx").write_bytes(_workbook(sheet1=raw, sheet3=msp))
            (output / "MS_DIAL_Materials_and_Methods.txt").write_text(
                "Spectra were annotated against MSMS-Public_all-neg-VS20.msp "
                "(https://doi.org/10.5281/zenodo.21904103).\n", encoding="utf-8")
            _zip(output / "MS_DIAL_publication_reporting_bundle.zip", {
                "MS_DIAL_publication_report.json": report, "Supplementary_Table_MS_DIAL.tsv": table,
                "Supplementary_Table_MS_DIAL.xlsx": _workbook(sheet1=raw, sheet3=msp)})
            _zip(output / "msdial-workflow-bundle.zip", {
                "analysis_files.csv": f"file_path,file_name\n{raw},NIST1950_neg_ID_01\n",
                "workflow-settings.json": json.dumps(settings), "REPRODUCE.txt": "C:\\path\\to\\MSDIALCUI.exe\n"})
            _zip(output / "msdial-project-artifacts.zip", {"Project-1.mdproject": b"\x00"})
            (output / "datamining-handoff.json").write_text(json.dumps({"run_directory": str(output)}), encoding="utf-8")
            self._mztab(builder, location="file://" + raw.replace("\\", "/"),
                        database_uri="file://" + msp.replace("\\", "/"))
            check = self._sec1(builder)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual([], check.evidence.get("fails", []))
        kinds = {entry.split(": ", 1)[1].split(" x")[0].split(" (e.g.")[0] for entry in check.evidence["warns"]}
        self.assertEqual({"drive-letter path", "file URI"}, kinds)
        self.assertEqual({"public"}, {library["distribution"] for library in check.evidence["libraries"]})


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
