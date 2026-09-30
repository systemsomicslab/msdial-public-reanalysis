"""CONV-1: an input converted from mzXML is the recorded, validated conversion.

MS-DIAL opens neither mzXML nor mzData. Interactive's mzxml_conversion.py (0.5.11) writes a sample
whose only readable encoding is mzXML as mzML under raw/converted, re-reads what it wrote and compares
every spectrum with the mzXML, and keeps a record of each file in input_conversions. SUM-1 follows a
converted input back to the mzXML it was read from (test_verify_input_lineage.py); CONV-1 holds the
conversion itself to its record. The fixtures are test_verify_input_lineage's, whose records are
written as convert_mzxml_to_mzml writes them; records the converter wrote for synthetic mzXML files
were checked against CONV-1 in the same way when it was written.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import test_verify_input_lineage as lineage  # noqa: E402  (loads the gate as verify_run_invariants_lineage)

verifier = lineage.verifier
CSV_COLUMNS = ("file_path", "file_name", "file_type", "class_id", "acquisition_type", "batch_order",
               "analytical_order", "factor")


def _csv(unit: lineage.LineageUnit, paths: list[str] | None = None) -> None:
    """The analysis CSV the preparer writes: one row per input candidate, unless told otherwise."""
    rows = unit.manifest["input_candidates"] if paths is None else paths
    with (unit.root / "output" / "analysis_files.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for order, path in enumerate(rows, start=1):
            writer.writerow((path, Path(path).stem, "Sample", "A", "DDA", 1, order, 1))


def _conv1(unit: lineage.LineageUnit):
    return lineage._check(unit.gate(), "CONV-1")


def _downloaded_conversion(temporary: str) -> tuple[lineage.LineageUnit, dict, dict]:
    """S1.mzXML downloaded with its MD5 verified, the only encoding of its sample, converted before analysis."""
    unit = lineage.LineageUnit(temporary)
    download = unit.download("S1.mzXML", b"<mzXML/>")
    row, record = unit.converted_input(Path(download["path"]))
    _csv(unit)
    return unit, row, record


def _relocate(unit: lineage.LineageUnit, row: dict, record: dict, destination: Path) -> None:
    """Move a converted output elsewhere, and every record of it with it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(record["output"]["path"], destination)
    candidates = unit.manifest["input_candidates"]
    candidates[candidates.index(record["output"]["path"])] = str(destination)
    record["output"]["path"] = row["path"] = str(destination)
    _csv(unit)


# ---------------------------------------------------------------------------------------------
# No conversion
# ---------------------------------------------------------------------------------------------

class NoConversionTests(unittest.TestCase):
    def test_a_unit_that_converted_nothing_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = lineage._workbench_archive_unit(temporary)
            _csv(unit)
            check = _conv1(unit)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIn("No input of the unit was converted", check.detail)
        self.assertEqual(0, check.evidence["records"])

    def test_a_manifest_written_before_the_lineage_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = lineage._workbench_archive_unit(temporary)
            del unit.manifest["input_lineage"]
            _csv(unit)
            check = _conv1(unit)

        self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_an_mzxml_or_mzdata_input_is_refused(self) -> None:
        """SUM-1 covers the file; nothing else said MS-DIAL cannot open it."""
        for name in ("S1.mzXML", "S1.mzdata", "S1.mzData.xml"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                unit = lineage.LineageUnit(temporary)
                unit.file_input(unit.download(name, b"<mzXML/>"))
                _csv(unit)
                report = unit.gate()
                check = lineage._check(report, "CONV-1")

                self.assertEqual(verifier.PASS, lineage._check(report, "SUM-1").status)
                self.assertEqual(verifier.FAIL, check.status)
                self.assertIn("1 input candidate(s) are mzXML or mzData", check.detail)
                self.assertIn("1 analysis-CSV row(s) open an mzXML or mzData file", check.detail)

    def test_an_mzdata_candidate_the_campaign_disposition_excluded_is_not_refused(self) -> None:
        """Interactive leaves an excluded input among the candidates and out of the CSV; MS-DIAL never opens it."""
        for disposed in (True, False):
            with self.subTest(disposed=disposed), tempfile.TemporaryDirectory() as temporary:
                unit = lineage._workbench_archive_unit(temporary)
                _csv(unit)
                excluded = unit.file_input(unit.download("S3.mzData", b"<mzData/>"))
                if disposed:
                    unit.manifest["campaign_disposition"] = {
                        "schema": "msdial-campaign-disposition.v1", "disposition": "run", "reasons": [],
                        "warnings": [], "excluded_inputs": [{"path": excluded["path"], "reason": "unsupported_encoding"}],
                        "split_key": None,
                    }
                check = _conv1(unit)

                if disposed:
                    self.assertEqual(verifier.PASS, check.status, check.detail)
                    self.assertEqual(1, check.evidence["unreadable_candidates_excluded"])
                    self.assertIn("none of its 2 input candidate(s) MS-DIAL opens", check.detail)
                    self.assertIn("excluded 1 mzXML or mzData candidate(s)", check.detail)
                else:
                    self.assertEqual(verifier.FAIL, check.status)
                    self.assertIn("(S3.mzData)", check.detail)

    def test_an_mzxml_row_in_the_csv_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, record = _downloaded_conversion(temporary)
            _csv(unit, [record["source"]["path"]])
            check = _conv1(unit)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("analysis-CSV row(s) open an mzXML", check.detail)
        self.assertNotIn("input candidate(s) are mzXML", check.detail)


# ---------------------------------------------------------------------------------------------
# A converted input
# ---------------------------------------------------------------------------------------------

class ConvertedInputTests(unittest.TestCase):
    def test_a_recorded_validated_conversion_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, _record = _downloaded_conversion(temporary)
            check = _conv1(unit)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual((1, 1, 2), (check.evidence["converted"], check.evidence["rehashed"],
                                     check.evidence["spectra_validated"]))
        self.assertIn("1 of the unit's 1 input candidate(s) are mzML converted from mzXML", check.detail)

    def test_an_mzxml_inside_a_workbench_archive_is_covered_through_both_links(self) -> None:
        """The archive's MD5 vouches for the mzXML (SUM-1), and the record for the mzML (CONV-1)."""
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, _record = lineage._converted_workbench_unit(temporary)
            _csv(unit)
            report = unit.gate()
            sum1, conv1 = lineage._check(report, "SUM-1"), lineage._check(report, "CONV-1")
            stages = report.as_dict()["checks_by_stage"]

        self.assertEqual(verifier.WARN, sum1.status, sum1.detail)
        self.assertEqual("archive_verified", sum1.evidence["basis"])
        self.assertEqual(1, sum1.evidence["converted_inputs"])
        self.assertEqual(verifier.PASS, conv1.status, conv1.detail)
        self.assertEqual(1, conv1.evidence["rehashed"])
        self.assertEqual({"SUM-1": verifier.WARN, "CONV-1": verifier.PASS}, stages["B1"])

    def test_an_output_outside_the_raw_tree_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, row, record = _downloaded_conversion(temporary)
            _relocate(unit, row, record, unit.root / "output" / "S1.mzML")
            check = _conv1(unit)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("S1.mzML: it lies outside the raw tree", check.detail)

    def test_an_output_changed_since_it_was_recorded_is_refused(self) -> None:
        for label, data, expected in (("same size", b"<mzML/>S1.mzXMX", "no longer those whose sha256"),
                                      ("other size", b"<mzML/>", "holds 7 bytes where its record gives 15")):
            with self.subTest(label), tempfile.TemporaryDirectory() as temporary:
                unit, _row, record = _downloaded_conversion(temporary)
                Path(record["output"]["path"]).write_bytes(data)
                check = _conv1(unit)

                self.assertEqual(verifier.FAIL, check.status)
                self.assertIn(expected, check.detail)

    def test_an_output_absent_while_the_raw_tree_is_present_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, record = _downloaded_conversion(temporary)
            Path(record["output"]["path"]).unlink()
            check = _conv1(unit)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("absent while the raw tree is present", check.detail)

    def test_an_output_released_with_the_raw_tree_keeps_its_recorded_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, _record = _downloaded_conversion(temporary)
            shutil.rmtree(unit.root / "raw")
            check = _conv1(unit)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual((0, 1), (check.evidence["rehashed"], check.evidence["released_with_raw_tree"]))
        self.assertIn("released with the raw tree", check.detail)

    def test_an_output_is_rehashed_where_a_moved_workspace_now_keeps_it(self) -> None:
        """The records name where the raw tree was; its bytes are read where it is now."""
        for changed in (False, True):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                unit, row, record = _downloaded_conversion(temporary)
                before = Path(temporary) / "moved-from" / "unit" / "raw"
                recorded = before / "converted" / "S1.mzML"
                unit.manifest["raw_directory"] = str(before)
                unit.manifest["input_candidates"] = [str(recorded)]
                record["output"]["path"] = row["path"] = str(recorded)
                _csv(unit)
                if changed:
                    (unit.root / "raw" / "converted" / "S1.mzML").write_bytes(b"<mzML/>S1.mzXMX")
                check = _conv1(unit)

                if changed:
                    self.assertEqual(verifier.FAIL, check.status)
                    self.assertIn("no longer those whose sha256", check.detail)
                else:
                    self.assertEqual(verifier.PASS, check.status, check.detail)
                    self.assertEqual(1, check.evidence["rehashed"])

    def test_a_record_without_the_output_sha256_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, record = _downloaded_conversion(temporary)
            del record["output"]["sha256"]
            check = _conv1(unit)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("keeps no sha256 of the mzML it wrote", check.detail)

    def test_a_record_that_does_not_name_its_converter_is_refused(self) -> None:
        for field in ("name", "version", "module_sha256"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                unit, _row, record = _downloaded_conversion(temporary)
                del record["converter"][field]
                check = _conv1(unit)

                self.assertEqual(verifier.FAIL, check.status)
                self.assertIn(f"does not identify the converter that wrote it (no {field})", check.detail)

    def test_a_conversion_whose_validation_did_not_pass_is_refused(self) -> None:
        cases = {
            "absent": (lambda record: record.pop("validation"), "keeps no validation"),
            "failed": (lambda record: record["validation"].update(status="failed", problem_count=1),
                       "did not pass (status 'failed'"),
            "a problem": (lambda record: record["validation"].update(problem_count=1), "1 problem(s)"),
            "another schema": (lambda record: record["validation"].update(schema="other.v1"),
                               "validation is not an msdial-mzxml-conversion-validation.v1 record"),
            "not every spectrum": (lambda record: record["validation"].update(spectra_compared=1),
                                   "compared 1 spectra, and its record counts 2 written"),
        }
        for label, (edit, expected) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as temporary:
                unit, _row, record = _downloaded_conversion(temporary)
                edit(record)
                check = _conv1(unit)

                self.assertEqual(verifier.FAIL, check.status)
                self.assertIn(expected, check.detail)

    def test_a_record_of_another_schema_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, record = _downloaded_conversion(temporary)
            record["schema"] = "msconvert-run.v1"
            check = _conv1(unit)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("is not an msdial-mzxml-conversion.v1 record", check.detail)


# ---------------------------------------------------------------------------------------------
# Polarity
# ---------------------------------------------------------------------------------------------

def _scan_without_polarity(record: dict, *, imputed: bool = False, value: str = "negative") -> None:
    """The MS2 scan recorded no polarity. Imputed, it was given ``value`` as convert_mzxml_to_mzml records it."""
    record["counts"]["polarity_recorded"] = {"absent": 1, "negative": 1}
    if not imputed:
        record["counts"]["polarity"] = {"negative": 1, "unrecorded": 1}
        return
    polarity = Counter({"negative": 1})
    polarity[value] += 1
    record["counts"]["polarity"] = dict(sorted(polarity.items()))
    record["options"]["impute_polarity"] = value
    record["inferences"].append({
        "kind": "polarity_imputation", "value": value,
        "basis": "the analysis unit's declared ion mode, supplied by the caller", "source": "repository_declared",
        "spectra": 1, "summary": f"{value} polarity imputed from the declared ion mode for 1 spectra",
    })


class PolarityTests(unittest.TestCase):
    def test_a_spectrum_left_without_a_polarity_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, record = _downloaded_conversion(temporary)
            _scan_without_polarity(record)
            check = _conv1(unit)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("1 of its spectra carry no polarity and none was imputed", check.detail)

    def test_an_imputed_polarity_passes_and_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, record = _downloaded_conversion(temporary)
            unit.manifest["project"]["ion_mode"] = "Negative"
            _scan_without_polarity(record, imputed=True)
            check = _conv1(unit)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(1, check.evidence["imputed_polarity_spectra"])
        self.assertEqual({"polarity_imputation": 1}, check.evidence["inferences"])
        self.assertIn("none left without a polarity (1 by imputing the declared ion mode)", check.detail)

    def test_an_imputation_that_leaves_a_scan_unaccounted_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, record = _downloaded_conversion(temporary)
            _scan_without_polarity(record, imputed=True)
            record["counts"]["polarity_recorded"] = {"absent": 2}
            check = _conv1(unit)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("2 of its mzXML scans record no polarity, and its record imputes one to 1", check.detail)

    def test_an_imputation_other_than_the_declared_ion_mode_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, record = _downloaded_conversion(temporary)
            unit.manifest["project"]["ion_mode"] = "Negative"
            _scan_without_polarity(record, imputed=True, value="positive")
            check = _conv1(unit)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("imputes positive polarity where the unit's declared ion mode is negative", check.detail)

    def test_a_record_without_polarity_counts_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, record = _downloaded_conversion(temporary)
            del record["counts"]["polarity"]
            check = _conv1(unit)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("keeps no polarity counts", check.detail)


# ---------------------------------------------------------------------------------------------
# What the records account for
# ---------------------------------------------------------------------------------------------

class RecordAccountingTests(unittest.TestCase):
    def test_an_input_in_the_converted_directory_without_a_record_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, _record = _downloaded_conversion(temporary)
            unit.manifest["input_conversions"]["records"] = []
            check = _conv1(unit)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("in the raw tree's converted directory are the output of no conversion record (S1.mzML)",
                      check.detail)

    def test_two_records_for_one_output_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, record = _downloaded_conversion(temporary)
            unit.manifest["input_conversions"]["records"].append(json.loads(json.dumps(record)))
            check = _conv1(unit)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("S1.mzML: 2 conversion records name it as their output", check.detail)

    def test_a_conversion_that_did_not_complete_and_is_an_input_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = lineage.LineageUnit(temporary)
            download = unit.download("S1.mzXML", b"<mzXML/>")
            unit.converted_input(Path(download["path"]), status="failed")
            _csv(unit)
            check = _conv1(unit)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("it is an input, and its conversion record's status is 'failed'", check.detail)

    def test_a_conversion_that_did_not_complete_and_is_no_input_is_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = lineage._workbench_archive_unit(temporary)
            download = unit.download("S3.mzXML", b"<mzXML/>")
            row, _record = unit.converted_input(Path(download["path"]), status="failed")
            unit.manifest["input_candidates"].remove(row["path"])
            unit.manifest["input_lineage"]["rows"].remove(row)
            _csv(unit)
            check = _conv1(unit)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual(1, check.evidence["failed"])
        self.assertIn("S3.mzML: ConversionError: truncated", check.detail)
        self.assertIn("analysed by nothing", check.detail)

    def test_conversions_of_the_wrong_shape_are_refused_not_a_traceback(self) -> None:
        for label, value, expected in (("a string", "none", "neither a list of conversion records"),
                                       ("a number", {"records": [42]}, "conversion record 1 names no output")):
            with self.subTest(label), tempfile.TemporaryDirectory() as temporary:
                unit, _row, _record = _downloaded_conversion(temporary)
                unit.manifest["input_conversions"] = value
                check = _conv1(unit)

                self.assertEqual(verifier.FAIL, check.status)
                self.assertIn(expected, check.detail)

    def test_a_split_part_answers_for_its_own_inputs(self) -> None:
        """The parent converted both parts' files; a part is judged by the records of its own inputs."""
        for broken, expected in (("S2", verifier.PASS), ("S1", verifier.FAIL)):
            with self.subTest(broken=broken), tempfile.TemporaryDirectory() as temporary:
                parent = lineage.LineageUnit(temporary)
                rows = {}
                for name in ("S1", "S2"):
                    download = parent.download(f"{name}.mzXML", f"<mzXML>{name}</mzXML>".encode())
                    rows[name] = parent.converted_input(Path(download["path"]))
                rows[broken][1]["validation"]["status"] = "failed"
                parent_manifest = parent.write() / "provenance" / "run-manifest.json"
                part = Path(temporary) / "part"
                (part / "provenance").mkdir(parents=True)
                (part / "output").mkdir()
                own = rows["S1"][0]
                (part / "provenance" / "run-manifest.json").write_text(json.dumps({
                    "schema": "msdial-public-reanalysis-run.v1",
                    "project": {"analysis_unit_id": "part", "repository": "metabolomics_workbench"},
                    "split_from": {"manifest_path": str(parent_manifest), "analysis_unit_id": "unit"},
                    "raw_owned_by": str(parent_manifest), "raw_directory": str(parent.root / "raw"),
                    "input_candidates": [own["path"]],
                    "input_lineage": {"schema": lineage.LINEAGE_SCHEMA, "rows": [own]},
                }), encoding="utf-8")
                with (part / "output" / "analysis_files.csv").open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(CSV_COLUMNS)
                    writer.writerow((own["path"], "S1", "Sample", "A", "DDA", 1, 1, 1))
                check = lineage._check(verifier.verify(part, "before-production"), "CONV-1")

                self.assertEqual(expected, check.status, check.detail)
                self.assertEqual((2, 1), (check.evidence["records"], check.evidence["judged"]))


class UnevaluableTests(unittest.TestCase):
    def test_no_manifest_is_not_evaluable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unit"
            (root / "provenance").mkdir(parents=True)
            (root / "output").mkdir()
            check = lineage._check(verifier.verify(root, "before-production"), "CONV-1")

        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertTrue(check.required)

    def test_no_analysis_csv_is_not_evaluable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, _record = _downloaded_conversion(temporary)
            (unit.root / "output" / "analysis_files.csv").unlink()
            check = _conv1(unit)

        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertIn("analysis_files.csv is absent", check.detail)


if __name__ == "__main__":
    unittest.main()
