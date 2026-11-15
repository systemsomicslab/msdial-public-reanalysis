"""The Stage-1 concordance report (the author's decision D-B of 2026-10-28): per unit, how often an in-silico
library's top candidate shares the InChIKey's first block with an accepted acquired reference match. Aggregates only.
The candidate tables are synthetic, as Interactive's export_annotation_candidates writes them."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("report_insilico_concordance",
                                               TESTS.parent / "scripts" / "report-insilico-concordance.py")
report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report)

HEADER = ("alignment_master_id", "candidate_rank", "is_representative", "annotator_id", "inchikey",
          "is_reference_matched", "evidence_source")
KEY = {letter: letter * 14 + "-UHFFFAOYSA-N" for letter in "ABCDEFG"}


def _unit(root: Path, candidates: "list[tuple] | None", predicted: bool = True) -> Path:
    output = root / "unit" / "output"
    output.mkdir(parents=True)
    rows = ["annotator_id\tmsp_file_path\tpriority\tlibrary_kind\tsuggestions",
            "msp_high_quality\tacquired.msp\t3\tacquired\tTrue", "msp_low_quality\tacquired.msp\t2\tacquired\tTrue"]
    if predicted:
        rows.append("msp_in_silico\tpredicted.msp\t1\tpredicted\tFalse")
    (output / "msp_annotator_settings.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    if candidates is not None:
        (output / "AlignResult-1.mdcandidate.tsv").write_text(
            "\n".join(["\t".join(HEADER)] + ["\t".join(row) for row in candidates]) + "\n", encoding="utf-8")
    return output.parent


class ConcordanceTests(unittest.TestCase):
    CANDIDATES = [
        # Feature 1: strict match A; the in-silico top candidate agrees.
        ("1", "1", "true", "msp_high_quality", KEY["A"], "true", "ReferenceSpectrum"),
        ("1", "2", "false", "msp_in_silico", KEY["A"], "true", "InSilico"),
        # Feature 2: strict match B; the top in-silico candidate is C, the second agrees.
        ("2", "1", "true", "msp_high_quality", KEY["B"], "true", "ReferenceSpectrum"),
        ("2", "3", "false", "msp_in_silico", KEY["B"], "true", "InSilico"),
        ("2", "2", "false", "msp_in_silico", KEY["C"], "true", "InSilico"),
        # Feature 3: a broad match D only; the in-silico candidate disagrees.
        ("3", "1", "true", "msp_low_quality", KEY["D"], "true", "ReferenceSpectrum"),
        ("3", "2", "false", "msp_in_silico", KEY["E"], "true", "InSilico"),
        # Feature 4: a strict match with no in-silico candidate.
        ("4", "1", "true", "msp_high_quality", KEY["F"], "true", "ReferenceSpectrum"),
        # Feature 5: represented by an in-silico match; no acquired one.
        ("5", "1", "true", "msp_in_silico", KEY["G"], "true", "InSilico"),
        # Feature 6: a strict match whose in-silico candidate carries no comparable key.
        ("6", "1", "true", "msp_high_quality", KEY["A"], "true", "ReferenceSpectrum"),
        ("6", "2", "false", "msp_in_silico", "", "true", "InSilico"),
    ]

    def test_the_concordance_is_counted_against_the_strict_and_any_acquired_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = report.report_unit(_unit(Path(directory), self.CANDIDATES))
        self.assertEqual(result["status"], "read")
        (table,) = result["tables"]
        self.assertEqual((table["features"], table["in_silico_representatives"], table["strict_annotator"]),
                         (6, 1, "msp_high_quality"))
        self.assertEqual(table["against_strict"], {
            "reference_matched": 4, "with_in_silico_candidate": 3, "comparable": 2, "top_agrees": 1, "any_agrees": 2,
            "top_agreement_rate": 0.5, "any_agreement_rate": 1.0})
        self.assertEqual({key: table["against_any_acquired"][key] for key in ("reference_matched", "comparable",
                                                                              "top_agrees", "any_agrees")},
                         {"reference_matched": 5, "comparable": 3, "top_agrees": 1, "any_agrees": 2})

    def test_it_prints_aggregates_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            unit = _unit(Path(directory), self.CANDIDATES)
            for flags in ([], ["--json"]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(report.main([str(unit), *flags]), 0)
                text = out.getvalue()
                for letter in "ABCDEFG":
                    self.assertNotIn(letter * 14, text, "no InChIKey is printed")
                self.assertNotIn(directory, text, "no location is printed")
            self.assertEqual(json.loads(out.getvalue())["schema"], "msdial-insilico-concordance.v1")

    def test_without_a_candidate_table_or_an_in_silico_tier_it_says_why(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = report.report_unit(_unit(Path(directory) / "a", None))
            self.assertEqual(result["status"], "not_evaluable")
            self.assertIn("export_annotation_candidates true", result["reason"])
            result = report.report_unit(_unit(Path(directory) / "b", self.CANDIDATES, predicted=False))
            self.assertEqual(result["reason"], "the run searched no in-silico (predicted) MSP library")
            self.assertEqual(report.main([str(Path(directory) / "missing")]), 3)


if __name__ == "__main__":
    unittest.main()
