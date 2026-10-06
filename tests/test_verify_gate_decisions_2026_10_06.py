"""The gate on the user's decisions of 2026-10-06: the threshold-step fallback (PKH-1), the approved samples a
download never delivered (CLS-2), and the name pairings the lease inferred (PAIR-1).

PKH-1. The threshold is searched in the instrument-family step (100 for QTOF-type, 1,000 for FT data), and
only when the zero-threshold count is above 6,000 and no threshold at that step lands in 3,000-6,000 at a
step ten times finer, never finer than that. Interactive 0.5.28 records the fallback in the estimate
(threshold_step, coarse_threshold_step, step_fallback, fallback_reason, within_target_range); a
diagnostic recorded before carries threshold_step and within_target_range alone, as the pilot's
MTBLS1572 and MTBKS281 records do, and is still read.

CLS-2. Metabolomics Workbench ST001264 declares 31 sample rows, and its archive holds BioRec1-3 behind a
prefix and 28 members no row is named after (review of msdial-interactive-app#58, finding 1). The lease
admits the three, and the 28 rows reach no lineage row and no input. They are undelivered, not missing:
a separate evidence key and a WARN, read from the lineage and never from the CSV record.

PAIR-1. Every lineage row whose name_pairing says it was paired by inference (prefixed_member_name,
leading_identifier_token) is listed as a WARN with its declared and member names, under the warning code
input_names_paired_by_inference, so each one is on record in the gate report.

The fixture is test_verify_lineage_built_csv's FolderBranchUnit, a unit as Interactive's folder branch
prepares it.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import test_verify_lineage_built_csv as built  # noqa: E402  (loads the gate as verify_run_invariants_built_csv)

verifier = built.verifier
FolderBranchUnit = built.FolderBranchUnit
_check = built._check


def _method(unit: FolderBranchUnit, threshold: object) -> None:
    (unit.output / "method.txt").write_text(
        f"Smoothing method: TimeBasedLinearWeightedMovingAverage\nMinimum peak height: {threshold}\n", encoding="utf-8")


def _diagnostic(threshold: int, *, count: int, estimated: int, step: int, within: bool, **rule) -> dict:
    """A peak_height_diagnostics entry as Interactive's record_peak_height_diagnostic writes it: every field
    of the estimate unedited under "estimate", and some copied beside it."""
    estimate = {"minimum_peak_height": threshold, "target_peak_count_min": 3000, "target_peak_count_max": 6000,
                "estimated_peak_count": estimated, "diagnostic_peak_count": count, "threshold_step": step,
                "within_target_range": within, "method": "quantized height-range search", **rule}
    return {"recorded_at": "2026-10-06T10:00:00+09:00", "job_id": "job", "diagnostic_run_directory": "",
            "representative": {"file_name": "QC_05", "instrument_family": "QTOF", "threshold_step": step},
            "estimate": estimate, "minimum_peak_height": threshold, "diagnostic_peak_count": count,
            "estimated_peak_count": estimated, "threshold_step": step, "method": "quantized height-range search"}


class ThresholdStepFallbackTests(unittest.TestCase):
    """PKH-1 reads and reports the step rule of 2026-10-06, and still reads a diagnostic recorded before it."""

    def _pkh1(self, *diagnostics: dict, threshold: object = None):
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("a.mzML", "a")
            unit.manifest["peak_height_diagnostics"] = list(diagnostics)
            _method(unit, diagnostics[-1]["minimum_peak_height"] if threshold is None else threshold)
            return _check(unit.gate(), "PKH-1")

    def test_a_diagnostic_recorded_before_the_rule_is_still_read(self) -> None:
        """MTBLS1572 as the pilot recorded it: step 100, 700 keeps 4,678 of 8,521, in range."""
        check = self._pkh1(_diagnostic(700, count=8521, estimated=4678, step=100, within=True))

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertFalse(check.evidence["step_rule_recorded"])
        self.assertEqual(100, check.evidence["threshold_step"])
        self.assertTrue(check.evidence["within_target_range"])
        self.assertNotIn("step_fallback", check.evidence)
        self.assertIn("step 100", check.detail)

    def test_an_earlier_estimate_outside_the_range_is_to_be_read(self) -> None:
        """MTBKS281 as the pilot recorded it: step 100, 100 keeps 338 of 20,057, and nothing said so."""
        check = self._pkh1(_diagnostic(100, count=20057, estimated=338, step=100, within=False))

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("predates the step fallback", check.detail)
        self.assertFalse(check.evidence["within_target_range"])
        self.assertEqual(1, len(check.evidence["step_rule_notes"]))

    def test_the_family_step_in_range_passes(self) -> None:
        check = self._pkh1(_diagnostic(200, count=35678, estimated=3517, step=100, within=True,
                                       coarse_threshold_step=100, step_fallback=False, fallback_reason=""))

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertTrue(check.evidence["step_rule_recorded"])
        self.assertIs(False, check.evidence["step_fallback"])
        self.assertEqual(100, check.evidence["coarse_threshold_step"])

    def test_a_fallback_that_lands_in_range_passes_and_says_so(self) -> None:
        """Demo bruker-compact_dda: no step of 100 is in range (100 keeps 1,932); step 10 gives 30 and 4,722."""
        reason = "no threshold at step 100 keeps 3,000-6,000 peaks"
        check = self._pkh1(_diagnostic(30, count=10168, estimated=4722, step=10, within=True,
                                       coarse_threshold_step=100, step_fallback=True, fallback_reason=reason))

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIs(True, check.evidence["step_fallback"])
        self.assertEqual(10, check.evidence["threshold_step"])
        self.assertEqual(100, check.evidence["coarse_threshold_step"])
        self.assertEqual(reason, check.evidence["fallback_reason"])
        self.assertIn("falling back from the instrument-family step 100", check.detail)
        self.assertIn(reason, check.detail)

    def test_a_fourier_transform_fallback_is_to_100(self) -> None:
        check = self._pkh1(_diagnostic(8600, count=9880, estimated=4497, step=100, within=True,
                                       coarse_threshold_step=1000, step_fallback=True, fallback_reason="r"))

        self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_a_fine_step_that_still_misses_the_range_is_recorded_and_to_be_read(self) -> None:
        """Demo waters-premier_mse_pos: step 10 gives 10 and 522 peaks, still below 3,000."""
        check = self._pkh1(_diagnostic(10, count=21578, estimated=522, step=10, within=False,
                                       coarse_threshold_step=100, step_fallback=True, fallback_reason="r"))

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("even the finer step 10", check.detail)
        self.assertIs(False, check.evidence["within_target_range"])
        self.assertNotIn("step_rule_broken", check.evidence)

    def test_a_fallback_finer_than_the_rule_allows_is_refused(self) -> None:
        """Never finer than the family step divided by ten: step 1 reaches the noise floor."""
        check = self._pkh1(_diagnostic(2, count=21578, estimated=4123, step=1, within=True,
                                       coarse_threshold_step=100, step_fallback=True, fallback_reason="r"))

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("nothing finer", check.evidence["step_rule_broken"][0])
        self.assertEqual(verifier.RECORD_ONLY, check.run_policy)

    def test_a_finer_step_with_no_fallback_recorded_is_refused(self) -> None:
        check = self._pkh1(_diagnostic(30, count=10168, estimated=4722, step=10, within=True,
                                       coarse_threshold_step=100, step_fallback=False, fallback_reason=""))

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("no step fallback is recorded", check.detail)

    def test_a_fallback_where_no_threshold_was_needed_is_refused(self) -> None:
        check = self._pkh1(_diagnostic(0, count=4893, estimated=4893, step=100, within=True,
                                       coarse_threshold_step=1000, step_fallback=True, fallback_reason="r"))

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("keeps the threshold at 0", check.detail)

    def test_a_fallback_that_records_no_family_step_is_refused(self) -> None:
        for coarse in (None, 0):
            with self.subTest(coarse=coarse):
                check = self._pkh1(_diagnostic(30, count=10168, estimated=4722, step=10, within=True,
                                               coarse_threshold_step=coarse, step_fallback=True, fallback_reason="r"))
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn("not the instrument-family step it fell back from", check.detail)

    def test_a_fallback_without_a_reason_is_to_be_read(self) -> None:
        check = self._pkh1(_diagnostic(20, count=22601, estimated=3627, step=10, within=True,
                                       coarse_threshold_step=100, step_fallback=True))

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("records no reason", check.detail)

    def test_a_zero_threshold_below_the_range_is_the_contract_not_a_miss(self) -> None:
        """At most 6,000 peaks at zero threshold keeps 0, whatever the count; it is said, not warned."""
        check = self._pkh1(_diagnostic(0, count=2100, estimated=2100, step=100, within=False,
                                       coarse_threshold_step=100, step_fallback=False, fallback_reason=""))

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIn("below 3,000-6,000", check.detail)

    def test_fields_copied_beside_the_estimate_are_read(self) -> None:
        item = _diagnostic(30, count=10168, estimated=4722, step=10, within=True)
        item.update(coarse_threshold_step=100, step_fallback=True, fallback_reason="r")
        check = self._pkh1(item)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIs(True, check.evidence["step_fallback"])

    def test_the_rule_is_read_from_the_diagnostic_that_produced_the_threshold(self) -> None:
        """A later diagnostic whose threshold did not run says nothing of the one that did."""
        ran = _diagnostic(30, count=10168, estimated=4722, step=10, within=True,
                          coarse_threshold_step=100, step_fallback=True, fallback_reason="r")
        later = _diagnostic(1, count=10168, estimated=4500, step=1, within=True,
                            coarse_threshold_step=100, step_fallback=True, fallback_reason="r")
        check = self._pkh1(ran, later, threshold=30)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(10, check.evidence["threshold_step"])


def _paired_unit(temporary: str, rows: int, delivered: "dict[int, str]") -> FolderBranchUnit:
    """A unit whose Catalog declared no analysis inputs, shaped like ST001264: sample rows named BioRec<n>
    or Sample<n>, and archive members admitted only where the lease paired one with a row's raw file."""
    unit = _PairingUnit(temporary)
    for number, member in delivered.items():
        unit.file(member, f"Biorec{number}", "All")
    project = unit.manifest["project"]
    project["analysis_inputs"] = []
    project["analysis_inputs_declared"] = False
    project.pop("analysis_input_count", None)
    by_member = {member: number for number, member in delivered.items()}
    for item in project["sample_metadata"]:
        item["raw_file"] = f"BioRec{by_member[item['raw_file']]}.raw"
    for row in unit.manifest["input_lineage"]["rows"]:
        member = Path(row["path"]).name
        row["declared_names"] = []
        # As build_input_lineage writes it (msdial-interactive-app#58 at 7ecfa99): a key only for a pairing
        # by leading identifier token.
        row["name_pairing"] = {"paired_by": "prefixed_member_name",
                               "declared_raw_file": f"BioRec{by_member[member]}.raw", "member_name": member}
    for number in range(1, rows - len(delivered) + 1):
        project["sample_metadata"].append({"sample_id": f"Sample{number}", "raw_file": f"Sample{number}"})
        project["class_proposal"]["assignments"].append({"sample_id": f"Sample{number}", "class_label": "All"})
        unit.labels[f"Sample{number}"] = "All"
    return unit


class _PairingUnit(FolderBranchUnit):
    """build_input_lineage of msdial-interactive-app#58: a row the lease paired by inference takes the sample
    of the declared raw file it was paired with (name_pairing), where its own name gives none."""

    def _attribute_lineage(self) -> None:
        super()._attribute_lineage()
        names: dict[str, set[str]] = {}
        for sample in self.manifest["project"]["sample_metadata"]:
            names.setdefault(str(sample.get("raw_file") or "").casefold(), set()).add(sample["sample_id"])
        for row in self.manifest["input_lineage"]["rows"]:
            pairing = row.get("name_pairing")
            if pairing and not row["sample_id"]:
                matched = names.get(pairing["declared_raw_file"].casefold()) or set()
                row["sample_id"] = next(iter(matched)) if len(matched) == 1 else ""


class UndeliveredSampleTests(unittest.TestCase):
    """CLS-2: an approved sample no lineage row names and no input carries the name of was never delivered."""

    ST001264 = {1: "021518_387057_CSHp_BioRec1.raw", 2: "021518_387057_CSHp_BioRec2.raw",
                3: "021518_387057_CSHp_BioRec3.raw"}

    def test_st001264_runs_3_of_31_and_the_28_undelivered_warn(self) -> None:
        """Finding 1 of the review of msdial-interactive-app#58: 28 study rows reach no input."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = _paired_unit(temporary, 31, self.ST001264)
            rows = unit.prepare()
            # What the CSV writer says of them is its own account, and it is not what CLS-2 reads.
            unit.manifest["analysis_csv"]["samples_without_input"] = []
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(3, len(rows))
        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual(28, check.evidence["undelivered_count"])
        self.assertEqual(10, len(check.evidence["undelivered_samples"]))
        self.assertEqual(["Sample1"], check.evidence["undelivered_samples"]["Sample1"])
        self.assertNotIn("missing", check.evidence)
        self.assertIn("the run covers 3 of the 31 approved samples", check.detail)
        self.assertEqual(verifier.RECORD_ONLY, check.run_policy)

    def test_without_a_lineage_a_sample_with_no_row_is_still_missing(self) -> None:
        """Older manifests: nothing says the sample was never delivered, so it stays missing."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("a.mzML", "a")
            unit.manifest["project"]["sample_metadata"].append({"sample_id": "z", "raw_file": "z.mzML"})
            unit.manifest["project"]["class_proposal"]["assignments"].append({"sample_id": "z", "class_label": "A"})
            unit.prepare()
            with_lineage = _check(unit.gate(), "CLS-2")
            del unit.manifest["input_lineage"]
            without_lineage = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.WARN, with_lineage.status, with_lineage.detail)
        self.assertEqual({"z": ["z.mzML"]}, with_lineage.evidence["undelivered_samples"])
        self.assertEqual(verifier.FAIL, without_lineage.status, without_lineage.detail)
        self.assertEqual(["z"], without_lineage.evidence["missing"])

    def test_a_row_the_csv_writer_dropped_is_missing_whatever_its_record_says(self) -> None:
        """The lineage names the sample: it was delivered, and its absence from the CSV is the writer's."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("a.mzML", "a")
            unit.file("b.mzML", "b")
            rows = unit.prepare()
            unit.write_csv(rows[:1])
            unit.manifest["analysis_csv"]["samples_without_input"] = ["b"]
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertEqual(["b"], check.evidence["missing"])
        self.assertNotIn("undelivered_samples", check.evidence)

    def test_a_sample_an_input_candidate_carries_the_name_of_is_not_undelivered(self) -> None:
        """No lineage row names z, but an input the lease admitted is z.mzML: z reached the run's inputs."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("a.mzML", "a")
            unit.manifest["project"]["sample_metadata"].append({"sample_id": "z", "raw_file": "z.mzML"})
            unit.manifest["project"]["class_proposal"]["assignments"].append({"sample_id": "z", "class_label": "A"})
            unit.prepare()
            unit.manifest["input_candidates"].append(str(unit.data / "sub" / "z.mzML"))
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertEqual(["z"], check.evidence["missing"])

    def test_a_sample_a_lineage_row_names_by_its_paired_raw_file_is_not_undelivered(self) -> None:
        """The pairing names the sample even where the CSV lost the row: that is missing, not undelivered."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = _paired_unit(temporary, 3, self.ST001264)
            rows = unit.prepare()
            unit.write_csv(rows[1:])
            dropped = next(row for row in unit.manifest["input_lineage"]["rows"] if row["path"] == rows[0]["input_path"])
            dropped["sample_id"] = ""
            unit.write()
            unit._attribute_lineage = lambda: None
            check = _check(verifier.verify(unit.root, "before-production"), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertEqual(["Biorec1"], check.evidence["missing"])
        self.assertNotIn("undelivered_samples", check.evidence)

    def test_a_run_of_no_approved_sample_is_still_refused(self) -> None:
        """a's input was excluded and z was never delivered: the Console reads no grouping at all."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            path = unit.file("a.mzML", "a")
            unit.exclude(path)
            unit.manifest["project"]["sample_metadata"].append({"sample_id": "z", "raw_file": "z.mzML"})
            unit.manifest["project"]["class_proposal"]["assignments"].append({"sample_id": "z", "class_label": "A"})
            unit.prepare()
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("None of the 2 approved sample(s) is analysed", check.detail)
        self.assertIn("never delivered", check.detail)
        self.assertEqual(1, check.evidence["undelivered_count"])


class InferredPairingTests(unittest.TestCase):
    """PAIR-1: every input the lease paired with a declared name by inference is listed as a WARN."""

    def test_no_pairing_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("a.mzML", "a")
            unit.prepare()
            check = _check(unit.gate(), "PAIR-1")

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(1, check.evidence["inputs"])

    def test_each_inferred_pairing_is_listed_with_its_declared_and_member_names(self) -> None:
        """The two rules of msdial-interactive-app#58: a prefix (ST001264) and a leading identifier token."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = _paired_unit(temporary, 3, {1: "021518_387057_CSHp_BioRec1.raw"})
            member = unit.file("VV_13_HEpG2_C1_exp344_pos.raw", "VV_13", "All")
            row = next(row for row in unit.manifest["input_lineage"]["rows"] if row["path"] == member)
            row["name_pairing"] = {"paired_by": "leading_identifier_token", "key": "VV_13",
                                   "declared_raw_file": "VV_13_HEpG2_C1_pos.raw",
                                   "member_name": "VV_13_HEpG2_C1_exp344_pos.raw"}
            unit.prepare()
            report = unit.gate()
            check = _check(report, "PAIR-1")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual("input_names_paired_by_inference", check.evidence["warning"])
        self.assertEqual(2, check.evidence["paired_by_inference"])
        self.assertEqual({"leading_identifier_token": 1, "prefixed_member_name": 1}, check.evidence["by_rule"])
        listed = {entry["member_name"]: entry for entry in check.evidence["pairings"]}
        self.assertEqual({"paired_by": "prefixed_member_name", "declared_raw_file": "BioRec1.raw",
                          "member_name": "021518_387057_CSHp_BioRec1.raw", "sample_id": "Biorec1"},
                         listed["021518_387057_CSHp_BioRec1.raw"])
        self.assertEqual("VV_13", listed["VV_13_HEpG2_C1_exp344_pos.raw"]["key"])
        self.assertEqual("VV_13_HEpG2_C1_pos.raw", listed["VV_13_HEpG2_C1_exp344_pos.raw"]["declared_raw_file"])
        self.assertIn("VV_13_HEpG2_C1_exp344_pos.raw as VV_13_HEpG2_C1_pos.raw (leading_identifier_token, key VV_13)",
                      check.detail)
        self.assertIn("021518_387057_CSHp_BioRec1.raw as BioRec1.raw (prefixed_member_name)", check.detail)
        self.assertEqual(verifier.RECORD_ONLY, check.run_policy)
        self.assertNotIn("PAIR-1", report.run_blocked_by)

    def test_a_pairing_the_lease_excluded_is_listed_too(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("a.mzML", "a")
            path = unit.lease_excluded("x_b.mzML", "b")
            row = next(row for row in unit.manifest["input_lineage"]["excluded"] if row["path"] == path)
            row["name_pairing"] = {"paired_by": "prefixed_member_name", "declared_raw_file": "b.mzML",
                                   "member_name": "x_b.mzML"}
            unit.prepare()
            check = _check(unit.gate(), "PAIR-1")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertTrue(check.evidence["pairings"][0]["excluded_by_the_lease"])

    def test_a_pairing_by_a_rule_the_gate_does_not_know_is_listed_apart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            path = unit.file("a.mzML", "a")
            unit.manifest["input_lineage"]["rows"][0]["name_pairing"] = {
                "paired_by": "fuzzy_match", "declared_raw_file": "A1.mzML", "member_name": Path(path).name}
            unit.prepare()
            check = _check(unit.gate(), "PAIR-1")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual([], check.evidence["pairings"])
        self.assertEqual("fuzzy_match", check.evidence["pairings_by_unknown_rule"][0]["paired_by"])
        self.assertIn("a rule this gate does not know (fuzzy_match)", check.detail)

    def test_a_manifest_with_no_lineage_owes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("a.mzML", "a")
            unit.prepare()
            del unit.manifest["input_lineage"]
            report = unit.gate()
            check = _check(report, "PAIR-1")

        self.assertEqual(verifier.NOT_EVALUABLE, check.status, check.detail)
        self.assertFalse(check.required)
        self.assertNotIn("PAIR-1", [item.check_id for item in report.strict_failures])


if __name__ == "__main__":
    unittest.main()
