"""LIB-3: a run that searched an in-silico (predicted) MSP library reports its matches as in-silico evidence, never as
identifications (the author's decisions of 2026-10-25 and 2026-10-28).

The fixtures are written as Interactive 0.5.48 and an MS-DIAL Console with the predicted-MSP semantics (#850) write
them for a four-tier run: method.txt and msp_annotator_settings.tsv; the alignment's mzTab-M (databases, summary
rows), candidate table, lipid RT review list and QA matrix; a per-file peak table; and, for a campaign unit, the
runner's copy of the approval. Every library, annotator and compound is synthetic, and the folder the libraries sit in
must never reach the report.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes  # noqa: E402,F401  (puts scripts/ on the path)
import test_verify_inputs_and_acquisition as inputs  # noqa: E402  (loads the gate)

verifier = inputs.verifier
_check, _write = inputs._check, inputs._write

ACQUIRED = "Synthetic-acquired-pos.msp"
PREDICTED = "Synthetic-predicted-pos.msp"
LBM = "Synthetic.lbm2"
PRIVATE_FOLDER = "private library folder"
SMH = ("SMH", "SML_ID", "database_identifier", "chemical_name", "reliability", "opt_global_evidence_source",
       "opt_global_lipid_rt_outcome")
CANDIDATE_HEADER = ("alignment_master_id", "candidate_rank", "is_representative", "annotator_id", "inchikey",
                    "annotation_tag_vs1", "is_reference_matched", "evidence_source")


class InSilicoRun:
    """A finished four-tier Positive run, each artifact changeable before write()."""

    def __init__(self, root: Path) -> None:
        self.workspace = root / "unit"
        self.output = self.workspace / "output"
        self.output.mkdir(parents=True)
        (self.workspace / "provenance").mkdir()
        self.folder = root / PRIVATE_FOLDER
        # The MSP annotator rows: (id, library, priority, library kind, suggestions); None leaves a column out.
        self.rows = [["msp_high_quality", ACQUIRED, 3, "acquired", "True"],
                     ["msp_low_quality", ACQUIRED, 2, "acquired", "True"],
                     ["msp_in_silico", PREDICTED, 1, "predicted", "False"]]
        self.suggestions_column = True
        # mzTab-M's databases: (label, prefix, version).
        self.databases = [("User-defined MSP library file", "MspDB_1_acquired", ACQUIRED),
                          ("User-defined in-silico MSP library file", "MspDB_2_predicted", PREDICTED),
                          ("MS-DIAL LipidsMsMs database", "LbmDB", LBM)]
        # mzTab-M's summary rows: (identifier, reliability, evidence source, lipid RT outcome).
        self.summary = [("MspDB_1_acquired:A1", "430", "ReferenceSpectrum", "NotEvaluable"),
                        ("LbmDB:PC 34:1", "420", "RuleBased", "Confirmed"),
                        ("MspDB_2_predicted:P1", "430", "InSilico", "NotEvaluable"),
                        ("MspDB_2_predicted:P2", "430", "InSilico", "NotEvaluable"),
                        ("null", "999", "null", "null")]
        # Whether mzTab-M writes the lipid RT outcome column, and the candidate table's columns.
        self.outcome_column = True
        self.candidate_header = CANDIDATE_HEADER
        # The candidate table's rows (None writes no table): candidate_header's values.
        self.candidates: "list[tuple] | None" = [
            ("10", "1", "true", "msp_high_quality", "AAAAAAAAAAAAAA-UHFFFAOYSA-N", "430", "true", "ReferenceSpectrum"),
            ("10", "2", "false", "msp_in_silico", "AAAAAAAAAAAAAA-UHFFFAOYSA-N", "430", "true", "InSilico"),
            ("11", "1", "true", "msp_in_silico", "BBBBBBBBBBBBBB-UHFFFAOYSA-N", "430", "true", "InSilico"),
            ("12", "1", "true", "msp_in_silico", "CCCCCCCCCCCCCC-UHFFFAOYSA-N", "430", "true", "InSilico"),
        ]
        # The lipid RT check's review list (None writes none): (alignment ID, outcome).
        self.review: "list[tuple] | None" = [("10", "Confirmed")]
        # The QA matrix (None writes none): (alignment ID, Reference matched).
        self.qa: "list[tuple] | None" = [("10", "TRUE"), ("11", "TRUE"), ("12", "FALSE")]
        # The per-file peak table: (name, annotator, evidence source, code); None leaves the peak table out.
        self.peaks: "list[tuple] | None" = [("Acquired one", "msp_high_quality", "ReferenceSpectrum", "430"),
                                            ("PC 34:1", f"LbmDB: {LBM}", "RuleBased", "420"),
                                            ("Predicted one", "msp_in_silico", "InSilico", "430"),
                                            ("Unknown", "", "", "999")]
        # The runner's copy of the approval (None writes none), with the in-silico library's pin and contract row.
        self.approval: "dict | None" = {
            "libraries": [{"name": ACQUIRED, "sha256": "a" * 64, "role": "msp", "ion_modes": ["Positive"],
                           "library_kind": "acquired"},
                          {"name": PREDICTED, "sha256": "b" * 64, "role": "msp", "ion_modes": ["Positive"],
                           "library_kind": "predicted"}],
            "annotation": {"Positive": {"profile_id": "lipid-rule-msp-high-low-insilico-v1", "msp_rows": [
                {"annotator_id": "msp_in_silico", "library": PREDICTED, "priority": 1, "library_kind": "predicted",
                 "suggestions": False}]}}}

    def write(self) -> Path:
        table = self.output / "msp_annotator_settings.tsv"
        header = ["annotator_id", "msp_file_path", "priority", "library_kind"] + (
            ["suggestions"] if self.suggestions_column else []) + ["target_omics"]
        lines = ["\t".join(header)]
        for annotator, library, priority, kind, suggestions in self.rows:
            values = [annotator, str(self.folder / library), str(priority), kind or ""]
            values += [suggestions or ""] if self.suggestions_column else []
            lines.append("\t".join(values + ["Metabolomics"]))
        table.write_text("\n".join(lines) + "\n", encoding="utf-8")
        (self.output / "method.txt").write_text(
            f"MSP annotator settings file path: {table}\nIon mode: Positive\nTarget omics: Lipidomics\n", encoding="utf-8")
        mztab = ["MTD\tmzTab-version\t2.0.0-M"]
        for index, (label, prefix, version) in enumerate(self.databases, start=1):
            mztab += [f"MTD\tdatabase[{index}]\t[,, {label}, ]", f"MTD\tdatabase[{index}]-prefix\t{prefix}",
                      f"MTD\tdatabase[{index}]-version\t{version}", f"MTD\tdatabase[{index}]-uri\tnull"]
        width = len(SMH) if self.outcome_column else len(SMH) - 1
        mztab.append("\t".join(SMH[:width]))
        for index, (identifier, code, evidence, outcome) in enumerate(self.summary, start=1):
            mztab.append("\t".join(("SML", str(index), identifier, "compound", code, evidence, outcome)[:width]))
        (self.output / "AlignResult-1.mzTab").write_text("\n".join(mztab) + "\n", encoding="utf-8")
        if self.candidates is not None:
            (self.output / "AlignResult-1.mdcandidate.tsv").write_text(
                "\n".join(["\t".join(self.candidate_header)] + ["\t".join(row) for row in self.candidates]) + "\n",
                encoding="utf-8")
        if self.review is not None:
            (self.output / "AlignResult-1_lipidrt_review.tsv").write_text(
                "\n".join(["ID\tName\tOutcome"] + [f"{key}\tcompound\t{outcome}" for key, outcome in self.review]) + "\n",
                encoding="utf-8")
        if self.qa is not None:
            (self.output / "AlignResult-1.qa.tsv").write_text(
                "\n".join(["ID\tFile\tReference matched"] + [f"{key}\tF1\t{flag}" for key, flag in self.qa]) + "\n",
                encoding="utf-8")
        if self.peaks is not None:
            rows = ["Peak ID\tName\tComment\tAnnotation tag (VS1.0)\tEvidence source"]
            for index, (name, annotator, evidence, code) in enumerate(self.peaks):
                comment = f"Annotation method: {annotator}; " if annotator else ""
                rows.append(f"{index}\t{name}\t{comment}\t{code}\t{evidence}")
            (self.output / "F1.mdpeak").write_text("\n".join(rows) + "\n", encoding="utf-8")
        if self.approval is not None:
            _write(self.workspace / "provenance" / "campaign-authorization.json", {
                "schema": "msdial-campaign-authorization.v1", "approval_id": "approval-1", **self.approval})
        return self.workspace


class Base(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run_ = InSilicoRun(Path(directory.name).resolve())

    def lib3(self):
        workspace = self.run_.write()
        report = verifier.Report(workspace)
        verifier.check_in_silico_matches_are_not_identifications(report, workspace, self.run_.output)
        check = _check(report, "LIB-3")
        text = json.dumps(check.as_dict())
        self.assertNotIn(PRIVATE_FOLDER, text, "LIB-3 names files, never their locations")
        for name in ("Acquired one", "Predicted one", "AAAAAAAAAAAAAA"):
            self.assertNotIn(name, text, "no compound name or InChIKey")
        return check


class InSilicoTierTests(Base):
    def test_a_four_tier_run_at_code_430_passes_and_records_the_code(self) -> None:
        check = self.lib3()
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(check.evidence["clauses"], {letter: verifier.PASS for letter in "abcde"})
        self.assertEqual(check.evidence["confidence_codes_seen"], {"430": 2})
        self.assertIn("before the MS-DIAL change", check.evidence["confidence_code_basis"]["430"])
        self.assertEqual((check.evidence["in_silico_annotators"], check.evidence["in_silico_libraries"]),
                         (["msp_in_silico"], [PREDICTED]))
        self.assertEqual((check.evidence["alignment_in_silico_matches"], check.evidence["peak_in_silico_names"],
                          check.evidence["peak_identifications"]), (2, 1, 2))
        # The QA matrix's Reference matched cell of feature 11, which an in-silico match represents: said, not judged.
        self.assertEqual(check.evidence["qa_reference_matched_cells_of_in_silico_features"], 1)
        self.assertIn("recorded, not judged", check.detail)
        self.assertIsNone(check.run_policy, "a check after production stops no run")

    def test_code_450_the_authors_code_for_an_accepted_in_silico_match_passes(self) -> None:
        self.run_.summary = [(identifier, "450" if evidence == "InSilico" else code, evidence, outcome)
                             for identifier, code, evidence, outcome in self.run_.summary]
        self.run_.candidates = [row[:5] + ("450" if row[3] == "msp_in_silico" else row[5],) + row[6:]
                                for row in self.run_.candidates]
        check = self.lib3()
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(check.evidence["confidence_codes_seen"], {"450": 2})
        self.assertIn("2026-10-28", check.evidence["confidence_code_basis"]["450"])

    def test_a_run_without_an_in_silico_tier_is_not_applicable(self) -> None:
        self.run_.rows = [row for row in self.run_.rows if row[0] != "msp_in_silico"]
        for row in self.run_.rows:
            row[3] = None
        self.run_.suggestions_column = False
        self.run_.approval = None
        check = self.lib3()
        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertFalse(check.required)
        self.assertIs(check.evidence["applicable"], False)
        self.assertIn("Not applicable", check.detail)

    def test_a_tier_named_by_the_approval_alone_is_held_to_it(self) -> None:
        """The approval names the in-silico row and library, and the run searched neither: (c) FAILs."""
        self.run_.rows = [row for row in self.run_.rows if row[0] != "msp_in_silico"]
        check = self.lib3()
        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("(c) the approval states the in-silico annotator msp_in_silico, and the run's MSP settings have no "
                      "such row", check.detail)
        self.assertIn(f"(c) {PREDICTED} is named as the in-silico library, and no MSP annotator row searches it",
                      check.detail)


class ClauseTests(Base):
    def test_a_reference_spectrum_or_rule_based_in_silico_match_fails_a(self) -> None:
        self.run_.summary[2] = ("MspDB_2_predicted:P1", "430", "ReferenceSpectrum", "NotEvaluable")
        self.run_.candidates[2] = ("11", "1", "true", "msp_in_silico", "BBBBBBBBBBBBBB-UHFFFAOYSA-N", "410", "true",
                                   "RuleBased")
        check = self.lib3()
        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual(check.evidence["clauses"]["a"], verifier.FAIL)
        self.assertIn("(a) AlignResult-1.mzTab: 1 summary row(s) of the in-silico database carry ReferenceSpectrum",
                      check.detail)
        self.assertIn("(a) AlignResult-1.mdcandidate.tsv: 1 in-silico candidate(s) carry RuleBased", check.detail)

    def test_an_accepted_in_silico_match_coded_otherwise_than_430_or_450_fails_a(self) -> None:
        self.run_.summary[2] = ("MspDB_2_predicted:P1", "410", "InSilico", "NotEvaluable")
        check = self.lib3()
        self.assertEqual(check.evidence["clauses"]["a"], verifier.FAIL)
        self.assertIn("carry an accepted match coded 410 with evidence InSilico", check.detail)

    def test_an_in_silico_library_declared_as_an_acquired_one_or_the_reverse_fails_b(self) -> None:
        self.run_.databases[1] = ("User-defined MSP library file", "MspDB_2_predicted", PREDICTED)
        check = self.lib3()
        self.assertEqual(check.evidence["clauses"]["b"], verifier.FAIL)
        self.assertIn(f"(b) AlignResult-1.mzTab declares the in-silico library {PREDICTED} as \"User-defined MSP "
                      "library file\"", check.detail)
        self.setUp()
        self.run_.databases[0] = ("User-defined in-silico MSP library file", "MspDB_1_acquired", ACQUIRED)
        check = self.lib3()
        self.assertIn(f"(b) AlignResult-1.mzTab declares the acquired library {ACQUIRED} as \"User-defined in-silico "
                      "MSP library file\"", check.detail)
        self.setUp()
        del self.run_.databases[1]
        check = self.lib3()
        self.assertIn(f"(b) AlignResult-1.mzTab declares no database for the in-silico library {PREDICTED}", check.detail)

    def test_an_in_silico_row_with_suggestions_or_searched_as_acquired_fails_c(self) -> None:
        for change, said in (
                (lambda run: run.rows[2].__setitem__(4, "True"), "states suggestions True, not False"),
                (lambda run: setattr(run, "suggestions_column", False), "states no suggestions setting"),
                (lambda run: run.rows[2].__setitem__(3, "acquired"), "searches an in-silico library with library kind "
                 "acquired, which the Console reads as acquired")):
            with self.subTest(said=said):
                self.setUp()
                change(self.run_)
                check = self.lib3()
                self.assertEqual(check.evidence["clauses"]["c"], verifier.FAIL, check.detail)
                self.assertIn(f"(c) msp_in_silico ({PREDICTED}) {said}", check.detail)

    def test_a_feature_named_from_an_in_silico_suggestion_fails_c(self) -> None:
        """A weak comparison against predicted spectra is InSilico coded 530: a suggestion, not an accepted match."""
        self.run_.summary.append(("MspDB_2_predicted:P3", "530", "InSilico", "NotEvaluable"))
        self.run_.summary.append(("MspDB_2_predicted:P4", "530", "PrecursorOnly", "NotEvaluable"))
        self.run_.candidates.append(("13", "1", "true", "msp_in_silico", "", "530", "false", "PrecursorOnly"))
        check = self.lib3()
        self.assertEqual(check.evidence["clauses"]["c"], verifier.FAIL)
        self.assertEqual(check.evidence["clauses"]["a"], verifier.PASS, "a suggestion is (c), not a wrong source")
        self.assertIn("(c) AlignResult-1.mzTab: 2 summary row(s) of the in-silico database are no accepted in-silico "
                      "match, names from a suggestion", check.detail)
        self.assertIn("(c) AlignResult-1.mdcandidate.tsv: 1 feature(s) are represented by an in-silico candidate that is "
                      "no accepted match", check.detail)
        self.assertEqual(check.evidence["confidence_codes_seen"], {"430": 2})

    def test_an_in_silico_match_tagged_lipid_rt_confirmed_fails_d(self) -> None:
        self.run_.summary[3] = ("MspDB_2_predicted:P2", "430", "InSilico", "Confirmed")
        self.run_.review.append(("11", "Confirmed"))
        check = self.lib3()
        self.assertEqual(check.evidence["clauses"]["d"], verifier.FAIL)
        self.assertIn("(d) AlignResult-1.mzTab: 1 summary row(s) of the in-silico database are tagged lipid-RT Confirmed",
                      check.detail)
        self.assertIn("(d) AlignResult-1_lipidrt_review.tsv: 1 feature(s) an in-silico match represents are Confirmed",
                      check.detail)
        # Feature 10's Confirmed is the acquired match's, which an in-silico candidate does not represent.
        self.assertNotIn("2 feature(s)", check.detail)

    def test_an_in_silico_name_counted_as_an_identification_fails_e(self) -> None:
        self.run_.peaks[2] = ("Predicted one", "msp_in_silico", "ReferenceSpectrum", "430")
        check = self.lib3()
        self.assertEqual(check.evidence["clauses"]["e"], verifier.FAIL)
        self.assertIn("(e) F1.mdpeak: 1 peak(s) an in-silico annotator named carry ReferenceSpectrum, which Interactive "
                      "counts as identifications", check.detail)

    def test_what_cannot_be_read_is_not_evaluable_and_required(self) -> None:
        self.run_.peaks = None
        check = self.lib3()
        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertTrue(check.required)
        self.assertEqual(check.evidence["clauses"]["e"], verifier.NOT_EVALUABLE)
        self.assertIn("(e) no .mdpeak peak table is in the output", check.detail)
        # Without the candidate table, (a) is read from mzTab-M alone.
        self.setUp()
        self.run_.candidates = None
        self.run_.review = None
        check = self.lib3()
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(check.evidence["sources"]["a"], ["AlignResult-1.mzTab"])

    def test_without_an_outcome_column_the_review_list_needs_the_candidate_table(self) -> None:
        """Review 1: (d) passed on an mzTab-M that writes no lipid RT outcome while a review list it could not join
        stood beside it."""
        self.run_.outcome_column = False
        self.run_.candidates = None
        check = self.lib3()
        self.assertEqual(check.evidence["clauses"]["d"], verifier.NOT_EVALUABLE, check.detail)
        self.assertIn("(d) mzTab-M writes no opt_global_lipid_rt_outcome", check.detail)
        # With no review list either, the check tagged nothing.
        self.setUp()
        self.run_.outcome_column = False
        self.run_.candidates = self.run_.review = None
        check = self.lib3()
        self.assertEqual(check.evidence["clauses"]["d"], verifier.PASS, check.detail)
        self.assertEqual(check.evidence["sources"]["d"], ["no lipid RT outcome or review list was written, so the check tagged nothing"])

    def test_a_candidate_table_without_its_columns_is_set_aside_and_mztab_read(self) -> None:
        self.run_.candidates = [row[:5] + row[6:] for row in self.run_.candidates]
        self.run_.candidate_header = tuple(name for name in CANDIDATE_HEADER if name != "annotation_tag_vs1")
        check = self.lib3()
        self.assertEqual(check.evidence["clauses"]["a"], verifier.PASS, check.detail)
        self.assertEqual(check.evidence["sources"]["a"], ["AlignResult-1.mzTab"])

    def test_without_the_method_the_tier_the_approval_names_is_still_held(self) -> None:
        """Review 2: the ion mode came from method.txt alone, so a run without it read no approved in-silico tier and
        said none was named. Unread settings leave (c) not evaluable instead."""
        workspace = self.run_.write()
        (self.run_.output / "method.txt").unlink()
        report = verifier.Report(workspace)
        verifier.check_in_silico_matches_are_not_identifications(report, workspace, self.run_.output)
        check = _check(report, "LIB-3")
        self.assertIs(check.evidence["applicable"], True)
        self.assertEqual(check.evidence["in_silico_libraries"], [PREDICTED])
        self.assertEqual(check.evidence["clauses"]["c"], verifier.NOT_EVALUABLE)
        self.assertTrue(check.required)

    def test_a_summary_row_is_judged_by_its_first_identifier(self) -> None:
        self.run_.summary[0] = ("MspDB_1_acquired:A1|MspDB_2_predicted:P9", "430", "ReferenceSpectrum", "NotEvaluable")
        check = self.lib3()
        self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_the_gate_runs_it_after_the_run(self) -> None:
        workspace = self.run_.write()
        report = verifier.verify(workspace, "after-run")
        check = _check(report, "LIB-3")
        self.assertEqual((check.stage, check.status), ("after-run", verifier.PASS), check.detail)
        self.assertNotIn("run_policy", check.as_dict())
        self.assertIn("LIB-3", dict((code, keys) for code, _name, _meaning, keys in verifier.COMPLETION_STAGES)["B7"])
        self.assertNotIn("LIB-3", [check.check_id for check in verifier.verify(workspace, "before-production").checks])


if __name__ == "__main__":
    unittest.main()
