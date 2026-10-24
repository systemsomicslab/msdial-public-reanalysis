"""LIB-2: a campaign unit's run loads the libraries its approval names, with the bytes it names.

The MB-POST preflight units of 2026-10-14 and -15 were approved with one LBM2 and loaded the official one:
Interactive's tiered_lipid_msp strategy took its catalog's LBM over the answer's, and the runner's records named the
approved file. LIB-2 reads what the method loads (method.txt and the MSP annotator settings it names), hashes each
file, and holds it to the approval copy in provenance, to the answers the unit was given, and to the other records;
since the review of 2026-10-25 it also holds the method to the annotation the approval states for the unit's ion mode
(profile, LBM cutoffs and priority, MSP annotator rows).

The fixtures are written as Interactive and the runner write them: the runner's copy of the approval in provenance;
Interactive's method.txt, msp_annotator_settings.tsv, workflow-settings.json, run manifest and guided-answers.json in
output. The libraries are synthetic files in a folder whose name must never reach the report.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes  # noqa: E402,F401  (puts scripts/ on the path)
import test_verify_inputs_and_acquisition as inputs  # noqa: E402  (loads the gate)
from campaign import plan  # noqa: E402

verifier = inputs.verifier
_check, _write = inputs._check, inputs._write

APPROVED_LBM = "Synthetic-approved.lbm2"
OFFICIAL_LBM = "Synthetic-official.lbm2"
POSITIVE_MSP = "Synthetic-pos.msp"
NEGATIVE_MSP = "Synthetic-neg.msp"
OTHER_MSP = "Synthetic-other.msp"
PRIVATE_FOLDER = "private library folder"
TSV_HEADER = ("annotator_id", "msp_file_path", "priority", "rt_tolerance", "ms1_tolerance", "ms2_tolerance",
              "weighted_dot_product_cutoff", "simple_dot_product_cutoff", "reverse_dot_product_cutoff",
              "matched_peaks_percentage_cutoff", "minimum_spectrum_match", "use_retention_information_for_scoring",
              "use_retention_information_for_filtering", "target_omics", "evidence_tier")
PROFILE_ID = "lipid-rule-msp-high-low-v1"
# The settings of the tiered profile's two MSP rows, as Interactive's annotation_pipeline sets and writes them.
TIER_SETTINGS = {
    "msp_high_quality": {"priority": 2, "rt_tolerance": 0.5, "ms1_tolerance": 0.01, "ms2_tolerance": 0.05,
                         "weighted_dot_product_cutoff": 0.6, "simple_dot_product_cutoff": 0.6,
                         "reverse_dot_product_cutoff": 0.8, "matched_peaks_percentage_cutoff": 0.0,
                         "minimum_spectrum_match": 3, "use_rt_scoring": False, "use_rt_filtering": False},
    "msp_low_quality": {"priority": 1, "rt_tolerance": 0.5, "ms1_tolerance": 0.01, "ms2_tolerance": 0.25,
                        "weighted_dot_product_cutoff": 0.5, "simple_dot_product_cutoff": 0.5,
                        "reverse_dot_product_cutoff": 0.5, "matched_peaks_percentage_cutoff": 0.0,
                        "minimum_spectrum_match": 1, "use_rt_scoring": False, "use_rt_filtering": False},
}


def _sha(name: str) -> str:
    return hashlib.sha256(f"synthetic {name}\n".encode()).hexdigest()


def _contract(msp: str) -> dict:
    """An ion mode's annotation contract as the plan writes it into the approval (plan.annotation_contract)."""
    return {"profile_id": PROFILE_ID, "lbm": APPROVED_LBM, "lbm_cutoffs": {"weighted": 0.1, "simple": 0.1, "reverse": 0.1},
            "lbm_priority": 3,
            "msp_rows": [{"annotator_id": annotator, "library": msp, **settings, "target_omics": "Metabolomics",
                          "library_kind": "acquired"} for annotator, settings in TIER_SETTINGS.items()]}


class LibraryUnit:
    """A Negative-mode campaign unit before production, as the runner and Interactive leave it: what each record
    names can be changed before write()."""

    def __init__(self, root: Path) -> None:
        self.workspace = root / "analysis" / "mb_post" / "MPST-SYN" / "unit-syn"
        self.output = self.workspace / "output"
        self.output.mkdir(parents=True)
        (self.workspace / "provenance").mkdir()
        self.folder = root / PRIVATE_FOLDER
        self.folder.mkdir()
        for name in (APPROVED_LBM, OFFICIAL_LBM, POSITIVE_MSP, NEGATIVE_MSP, OTHER_MSP):
            (self.folder / name).write_bytes(f"synthetic {name}\n".encode())
        # What the plan pins now: the libraries the profile names, with their roles and ion modes.
        self.approval: "list[dict] | None" = [
            {"name": APPROVED_LBM, "sha256": _sha(APPROVED_LBM), "role": "lbm", "ion_modes": ["Positive", "Negative"]},
            {"name": NEGATIVE_MSP, "sha256": _sha(NEGATIVE_MSP), "role": "msp", "ion_modes": ["Negative"]},
            {"name": POSITIVE_MSP, "sha256": _sha(POSITIVE_MSP), "role": "msp", "ion_modes": ["Positive"]},
        ]
        # What the approval states each ion mode's units annotate with (None: an approval made before it stated any).
        self.annotation: "dict | None" = {"Negative": _contract(NEGATIVE_MSP), "Positive": _contract(POSITIVE_MSP)}
        self.ion_mode = "Negative"
        self.target_omics = "Lipidomics"
        # What method.txt loads, and each MSP annotator row: (annotator id, library, target omics), with the settings
        # TIER_SETTINGS gives its id as row_settings changes them; a library_kind column only where a test writes one.
        self.lbm = APPROVED_LBM
        self.rows = [("msp_high_quality", NEGATIVE_MSP, "Metabolomics"), ("msp_low_quality", NEGATIVE_MSP, "Metabolomics")]
        self.row_settings: dict = {}
        self.library_kinds: dict = {}
        # The annotation profile, LBM priority and LBM cutoffs (weighted, simple, reverse) method.txt states.
        self.method_profile = PROFILE_ID
        self.method_lbm_priority = "3"
        self.method_cutoffs = ("0.1", "0.1", "0.1")
        # What workflow-settings.json and the run manifest name, where a test makes them differ from the method.
        self.settings_lbm: "str | None" = None
        self.prepared_sha: dict = {}
        # The answers the runner gave the unit (output/guided-answers.json); None writes none.
        self.answers: "dict | None" = {"library_strategy": "tiered_lipid_msp", "target_omics": "Lipidomics",
                                       "libraries": {"lbm_path": APPROVED_LBM, "msp_paths": [NEGATIVE_MSP],
                                                     "msp_version": "VS-synthetic"}}

    def location(self, name: str) -> str:
        return str(self.folder / name)

    def write(self) -> Path:
        if self.approval is not None:
            _write(self.workspace / "provenance" / "campaign-authorization.json", {
                "schema": "msdial-campaign-authorization.v1", "approval_id": "approval-1", "campaign_id": "c1",
                "manifest_digest": "sha256:" + "0" * 64, "covers": [1, 3, 4, 5, "split"], "units": ["unit-syn"],
                "raw_retention_policy": "delete_after_validated_output", "libraries": self.approval,
                **({"annotation": self.annotation} if self.annotation is not None else {})})
        _write(self.workspace / "provenance" / "run-manifest.json", {
            "schema": "msdial-public-reanalysis-run.v1",
            "project": {"repository": "mb_post", "accession": "MPST-SYN", "analysis_unit_id": "unit-syn",
                        "ion_mode": self.ion_mode},
            "workspace": str(self.workspace), "output_directory": str(self.output)})
        table = self.output / "msp_annotator_settings.tsv"
        header = TSV_HEADER + (("library_kind",) if self.library_kinds else ())
        lines = ["\t".join(header)]
        columns = dict(verifier.MSP_ROW_COLUMNS)
        for index, (annotator, library, omics) in enumerate(self.rows, start=1):
            settings = {**TIER_SETTINGS.get(annotator, {"priority": 3 - index}), **self.row_settings.get(annotator, {})}
            values = {columns[key]: str(value) for key, value in settings.items()}
            values.update({"annotator_id": annotator, "msp_file_path": self.location(library), "target_omics": omics,
                           "evidence_tier": "tier", "library_kind": self.library_kinds.get(annotator, "")})
            lines.append("\t".join(values.get(column, "0") for column in header))
        table.write_text("\n".join(lines) + "\n", encoding="ascii")
        weighted, simple, reverse = self.method_cutoffs
        (self.output / "method.txt").write_text("\n".join([
            f"MSP annotator settings file path: {table}",
            f"Annotation pipeline profile: {self.method_profile}",
            f"LBM annotator priority: {self.method_lbm_priority}",
            f"Ion mode: {self.ion_mode}",
            f"Target omics: {self.target_omics}",
            "Msp file path: ",
            f"Lbm file path: {self.location(self.lbm) if self.lbm else ''}",
            "Text DB file path: ",
            f"Weighted dot product cutoff for LBM-based annotation: {weighted}",
            f"Simple dot product cutoff for LBM-based annotation: {simple}",
            f"Reverse dot product cutoff for LBM-based annotation: {reverse}",
        ]) + "\n", encoding="utf-8")
        settings_lbm = self.settings_lbm or self.lbm
        _write(self.output / "workflow-settings.json", {
            "target_omics": self.target_omics, "lbm_path": self.location(settings_lbm) if settings_lbm else "",
            "lbm_annotator": {"lbm_file_path": self.location(settings_lbm) if settings_lbm else "", "priority": 3},
            "msp_path": "",
            "msp_annotators": [{"annotator_id": annotator, "msp_file_path": self.location(library), "target_omics": omics}
                               for annotator, library, omics in self.rows],
        })
        loaded = sorted({library for _annotator, library, _omics in self.rows} | ({settings_lbm} if settings_lbm else set()))
        _write(self.output / "run-manifest.json", {"libraries": [
            {"name": name, "filename": name, "role": "lbm" if name.endswith(".lbm2") else "msp",
             "sha256": self.prepared_sha.get(name, _sha(name)), "private": True} for name in loaded]})
        if self.answers is not None:
            _write(self.output / "guided-answers.json", {
                "schema": "msdial-interactive.guided-answers.v1", "sharing": "local_only",
                "answers": {**self.answers, "libraries": {
                    key: ([self.location(item) for item in value] if isinstance(value, list) else
                          self.location(value) if key.endswith("path") else value)
                    for key, value in self.answers["libraries"].items()}}})
        return self.workspace


class Base(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.unit = LibraryUnit(Path(directory.name).resolve())

    def lib2(self):
        report = verifier.verify(self.unit.write(), "before-production")
        check = _check(report, "LIB-2")
        self.assertNotIn(PRIVATE_FOLDER, json.dumps(check.as_dict()), "LIB-2 names files, never their locations")
        return report, check


class LoadedLibrariesTests(Base):
    def test_a_unit_that_loads_the_approved_libraries_passes(self) -> None:
        report, check = self.lib2()
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(verifier.BLOCKS_RUN, check.as_dict()["run_policy"])
        self.assertEqual({(item["name"], item["role"], item["sha256"]) for item in check.evidence["loaded"]},
                         {(APPROVED_LBM, "lbm", _sha(APPROVED_LBM)), (NEGATIVE_MSP, "msp", _sha(NEGATIVE_MSP))})
        self.assertEqual(check.evidence["lbm_cutoffs"], {"weighted": "0.1", "simple": "0.1", "reverse": "0.1"})
        self.assertEqual((check.evidence["target_omics"], check.evidence["expected_from"]), ("Lipidomics", "answers"))
        self.assertEqual(check.evidence["annotation_compared"],
                         ["annotation profile", "LBM cutoffs", "LBM priority", "MSP annotator rows"])
        self.assertNotIn("LIB-2", report.run_blocked_by)

    def test_the_official_lbm_loaded_in_place_of_the_approved_one_fails(self) -> None:
        """The preflight's substitution: the profile names one LBM2, Interactive loads another and records it
        consistently in its own files. The approval may name only the profile's libraries (as the plan pins them now)
        or every mapped library, the official LBM2 included: either way the run is not the approved one."""
        for name, approval in {
            "pins of the profile's libraries": None,
            "pins of every mapped library, without roles": [
                {"name": item, "sha256": _sha(item)} for item in (APPROVED_LBM, OFFICIAL_LBM, NEGATIVE_MSP, POSITIVE_MSP)],
        }.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                self.unit = LibraryUnit(Path(directory).resolve())
                if approval is not None:
                    self.unit.approval = approval
                self.unit.lbm = OFFICIAL_LBM
                report, check = self.lib2()
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn(f"the answers this unit was given name {APPROVED_LBM} as the LBM, and method.txt loads "
                              f"{OFFICIAL_LBM}", check.detail)
                self.assertEqual(approval is None, f"{OFFICIAL_LBM} is loaded as the LBM, and the approval does not "
                                 "name it" in check.detail)
                self.assertIn("LIB-2", report.run_blocked_by)

    def test_without_the_answers_the_approvals_ion_modes_say_which_lbm(self) -> None:
        self.unit.answers = None
        self.unit.approval.append({"name": OFFICIAL_LBM, "sha256": _sha(OFFICIAL_LBM), "role": "lbm", "ion_modes": []})
        self.unit.lbm = OFFICIAL_LBM
        _report, check = self.lib2()
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn(f"the approval names for Negative units {APPROVED_LBM} as the LBM, and method.txt loads "
                      f"{OFFICIAL_LBM}", check.detail)
        self.assertEqual(check.evidence["expected_from"], "approval_ion_modes")

    def test_which_of_two_approved_lbms_is_the_units_is_not_guessed(self) -> None:
        self.unit.answers = None
        self.unit.approval = [{"name": item, "sha256": _sha(item)} for item in (APPROVED_LBM, OFFICIAL_LBM, NEGATIVE_MSP)]
        report, check = self.lib2()
        self.assertEqual(verifier.NOT_EVALUABLE, check.status, check.detail)
        self.assertTrue(check.required)
        self.assertIn("neither the answers this unit was given nor the approval says which one", check.detail)
        self.assertIn("LIB-2", report.run_blocked_by, "left not evaluable where the stage owed it, it stops the run")

    def test_other_bytes_under_an_approved_name_fail(self) -> None:
        (self.unit.folder / APPROVED_LBM).write_bytes(b"rebuilt in place\n")
        self.unit.prepared_sha[NEGATIVE_MSP] = "f" * 64
        _report, check = self.lib2()
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        rebuilt = hashlib.sha256(b"rebuilt in place\n").hexdigest()
        self.assertIn(f"{APPROVED_LBM} hashes to {rebuilt[:12]}..., and the approval names {_sha(APPROVED_LBM)[:12]}...",
                      check.detail)
        self.assertIn(f"the run manifest records {NEGATIVE_MSP} as {'f' * 12}..., not the approved", check.detail)

    def test_a_library_the_approval_does_not_name_fails(self) -> None:
        self.unit.rows.append(("msp_extra", OTHER_MSP, "Metabolomics"))
        self.unit.answers["libraries"]["msp_paths"] = [NEGATIVE_MSP, OTHER_MSP]
        _report, check = self.lib2()
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn(f"{OTHER_MSP} is loaded as the MSP library, and the approval does not name it", check.detail)
        self.assertIn(f"the answers name {NEGATIVE_MSP}, {OTHER_MSP} as the MSP library, and the approval names "
                      f"{NEGATIVE_MSP} for Negative units", check.detail)

    def test_a_library_the_answers_name_and_the_method_drops_fails(self) -> None:
        """Interactive's tiered strategy used only the first MSP path and dropped the rest without a word."""
        self.unit.answers["libraries"]["msp_paths"] = [NEGATIVE_MSP, POSITIVE_MSP]
        _report, check = self.lib2()
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn(f"the answers this unit was given name {NEGATIVE_MSP}, {POSITIVE_MSP} as the MSP library, and "
                      f"method.txt loads {NEGATIVE_MSP}", check.detail)

    def test_records_that_name_another_library_than_the_method_fail(self) -> None:
        self.unit.settings_lbm = OFFICIAL_LBM
        _report, check = self.lib2()
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn(f"workflow-settings.json names {OFFICIAL_LBM} as the LBM, and method.txt loads {APPROVED_LBM}",
                      check.detail)
        self.assertIn(f"the run manifest records {OFFICIAL_LBM} as the LBM, and method.txt loads {APPROVED_LBM}",
                      check.detail)

    def test_an_msp_row_without_its_own_target_omics_fails(self) -> None:
        self.unit.rows[1] = ("msp_low_quality", NEGATIVE_MSP, "")
        _report, check = self.lib2()
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn(f"1 MSP annotator row(s) state no target_omics, so the Console scores them as the project's "
                      f"Lipidomics: msp_low_quality ({NEGATIVE_MSP})", check.detail)

    def test_a_target_omics_other_than_the_answers_fails(self) -> None:
        self.unit.target_omics = "Metabolomics"
        _report, check = self.lib2()
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("the answers give Target omics Lipidomics, and method.txt states Metabolomics", check.detail)

    def test_a_library_neither_readable_nor_recorded_is_not_evaluable(self) -> None:
        (self.unit.folder / NEGATIVE_MSP).unlink()
        self.unit.prepared_sha[NEGATIVE_MSP] = ""
        report, check = self.lib2()
        self.assertEqual(verifier.NOT_EVALUABLE, check.status, check.detail)
        self.assertIn(f"{NEGATIVE_MSP} could not be read to hash it", check.detail)
        self.assertIn("LIB-2", report.run_blocked_by)

    def test_a_library_moved_away_is_held_to_its_prepared_record(self) -> None:
        (self.unit.folder / NEGATIVE_MSP).unlink()
        _report, check = self.lib2()
        self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_a_unit_without_a_campaign_approval_is_not_applicable(self) -> None:
        self.unit.approval = None
        self.unit.lbm = OFFICIAL_LBM
        report, check = self.lib2()
        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertFalse(check.required)
        self.assertIs(check.evidence["applicable"], False)
        self.assertIn("Not applicable", check.detail)
        self.assertNotIn("LIB-2", report.run_blocked_by)
        self.assertNotIn("LIB-2", [item.check_id for item in report.strict_failures])

    def test_a_campaign_unit_without_its_method_is_not_evaluable(self) -> None:
        workspace = self.unit.write()
        (self.unit.output / "method.txt").unlink()
        report = verifier.verify(workspace, "before-production")
        check = _check(report, "LIB-2")
        self.assertEqual((check.status, check.required), (verifier.NOT_EVALUABLE, True))
        self.assertIn("LIB-2", report.run_blocked_by)


class ApprovedAnnotationTests(Base):
    """Review of 2026-10-25: LIB-2 read the method's LBM cutoffs and only recorded them, so 13 preflight units ran
    0.15/0.15/0.3 against the 0.1/0.1/0.1 of the two that followed, and a template changed after the plan would have
    passed the same way. The method is now held to what the approval states for the unit's ion mode."""

    def assert_fails(self, *words: str) -> None:
        report, check = self.lib2()
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        for text in words:
            self.assertIn(text, check.detail)
        self.assertIn("LIB-2", report.run_blocked_by)

    def test_lbm_cutoffs_other_than_the_approved_ones_fail(self) -> None:
        self.unit.method_cutoffs = ("0.15", "0.15", "0.3")
        self.assert_fails("method.txt states LBM cutoffs weighted/simple/reverse 0.15/0.15/0.3, and the approval states "
                          "0.1/0.1/0.1 for Negative units")

    def test_one_cutoff_off_fails_and_the_same_values_spelled_otherwise_pass(self) -> None:
        self.unit.method_cutoffs = ("0.1", "0.1", "0.11")
        self.assert_fails("weighted/simple/reverse 0.1/0.1/0.11, and the approval states 0.1/0.1/0.1")
        self.unit.method_cutoffs = ("0.10", "1e-1", ".1")
        self.unit.method_lbm_priority = "3.0"
        _report, check = self.lib2()
        self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_another_annotation_profile_or_lbm_priority_fails(self) -> None:
        self.unit.method_profile = "lipid-rule-msp-high-low-insilico-v1"
        self.unit.method_lbm_priority = "1"
        self.assert_fails(f"method.txt states annotation pipeline profile lipid-rule-msp-high-low-insilico-v1, and the "
                          f"approval states {PROFILE_ID} for Negative units",
                          "method.txt states LBM annotator priority 1, and the approval states 3 for Negative units")

    def test_an_msp_row_with_other_thresholds_priority_or_library_kind_fails(self) -> None:
        self.unit.row_settings = {"msp_high_quality": {"weighted_dot_product_cutoff": 0.99, "ms2_tolerance": 5},
                                  "msp_low_quality": {"priority": 9, "use_rt_scoring": True}}
        self.unit.library_kinds = {"msp_high_quality": "predicted"}
        self.assert_fails(
            "the MSP annotator msp_high_quality states ms2_tolerance 5, approved 0.05; weighted_dot_product_cutoff 0.99, "
            "approved 0.6; library kind predicted, approved acquired",
            "the MSP annotator msp_low_quality states priority 9, approved 1; use_rt_scoring True, approved False")

    def test_a_predicted_library_approved_as_such_passes_whatever_spelling_the_console_reads(self) -> None:
        for row in self.unit.annotation["Negative"]["msp_rows"]:
            row["library_kind"] = "predicted"
        self.unit.library_kinds = {"msp_high_quality": "InSilico", "msp_low_quality": "predictedmsp"}
        _report, check = self.lib2()
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.unit.library_kinds = {"msp_high_quality": "in silico", "msp_low_quality": "predicted"}
        self.assert_fails("the MSP annotator msp_high_quality states library kind acquired, approved predicted")

    def test_an_msp_annotator_the_approval_does_not_state_or_one_missing_fails(self) -> None:
        self.unit.rows.append(("msp_in_silico", NEGATIVE_MSP, "Metabolomics"))
        self.assert_fails("the run's MSP annotators are msp_high_quality, msp_in_silico, msp_low_quality, and the "
                          "approval states msp_high_quality, msp_low_quality for Negative units")
        self.unit.rows = self.unit.rows[:1]
        self.assert_fails("the run's MSP annotators are msp_high_quality, and the approval states msp_high_quality, "
                          "msp_low_quality for Negative units")

    def test_the_contract_of_the_units_own_ion_mode_is_the_one_held(self) -> None:
        """A Negative unit is held to the Negative contract: the Positive one names another MSP and other cutoffs."""
        self.unit.annotation["Positive"]["lbm_cutoffs"] = {"weighted": 0.15, "simple": 0.15, "reverse": 0.3}
        _report, check = self.lib2()
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.unit.annotation["Negative"]["msp_rows"][0]["library"] = POSITIVE_MSP
        self.assert_fails(f"the MSP annotator msp_high_quality states library {NEGATIVE_MSP}, approved {POSITIVE_MSP}")

    def test_an_approval_that_states_no_annotation_holds_the_method_to_nothing(self) -> None:
        """An approval made before the plan stated the annotation contract: what it does not state is not compared."""
        self.unit.annotation = None
        self.unit.method_cutoffs = ("0.15", "0.15", "0.3")
        _report, check = self.lib2()
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(check.evidence["annotation_compared"], [])
        self.assertIn("the approval states no annotation to hold the method to", check.detail)
        self.unit.annotation = {"Negative": {"profile_id": None, "lbm": APPROVED_LBM, "lbm_cutoffs": None,
                                             "lbm_priority": None, "msp_rows": None}}
        _report, check = self.lib2()
        self.assertEqual((check.status, check.evidence["annotation_compared"]), (verifier.PASS, []))

    def test_rows_of_an_msp_settings_file_that_cannot_be_read_are_not_compared(self) -> None:
        workspace = self.unit.write()
        (self.unit.output / "msp_annotator_settings.tsv").write_text("annotator_id\tpriority\nmsp_high_quality\t2\n",
                                                                     encoding="ascii")
        report = verifier.verify(workspace, "before-production")
        check = _check(report, "LIB-2")
        self.assertIn("LIB-2", report.run_blocked_by, "what it loads is not known, so the run stops")
        self.assertNotIn("MSP annotator rows", check.evidence["annotation_compared"])
        self.assertNotIn("the run's MSP annotators are", check.detail)

    def test_the_gate_reads_the_settings_the_plan_states(self) -> None:
        """The gate stands alone, so it repeats the plan's row settings and library kinds; they are held equal."""
        self.assertEqual([setting for setting, _column in verifier.MSP_ROW_COLUMNS], list(plan.MSP_ROW_SETTINGS))
        self.assertEqual(verifier.PREDICTED_LIBRARY_KINDS, plan.PREDICTED_LIBRARY_KINDS)
        self.assertEqual(set(_contract(NEGATIVE_MSP)), set(plan.ANNOTATION_CONTRACT_KEYS))


if __name__ == "__main__":
    unittest.main()
