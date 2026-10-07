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

The review of gate PR #31 (2026-10-07) found that CLS-2 called files the download delivered but the lease left
unpaired "never delivered" (ST004304's QC-D5-C-.mzML; ST001264's Youn_sa1..28.raw), that PAIR-1 on a split
part listed its sibling's pairings, and that PKH-1 let a step finer than 10 pass when the diagnostic recorded
10 as its family step. CLS-2 now reads what was delivered from the archive member listings and the downloads,
and a delivered, unpaired sample is a FAIL under delivered_unpaired_samples; PAIR-1 keeps to a part's own
inputs; PKH-1 holds the absolute floor (10 QTOF-type, 100 FT) and reports the production run's peak counts
where Interactive records them. ACQ-1 follows rule B2 (header first, 2026-10-06, msdial-interactive-app#62).

The fixture is test_verify_lineage_built_csv's FolderBranchUnit, a unit as Interactive's folder branch
prepares it.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertTrue(any("nothing finer" in item for item in check.evidence["step_rule_broken"]),
                        check.evidence["step_rule_broken"])
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

    def test_rows_nothing_delivered_could_be_warn_as_undelivered(self) -> None:
        """28 study rows reach no input, and the delivery holds nothing else: the three files delivered are the
        three paired. (The real ST001264 archive holds 28 more members; see DeliveredButUnpairedTests.)"""
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


def _archive(unit: FolderBranchUnit, members: "list[str]", *, shared: int = 1, corrupt: bool = False) -> None:
    """An archive download and the extraction record Interactive's archives.py writes for it: a member listing
    (archive-members-<sha>.tsv, its sha256 recorded), every member extracted."""
    archive = unit.root / "_dl" / "obj" / "bundle.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"PK")
    unit.manifest["downloads"].append({"path": str(archive), "size_bytes": 2,
                                       "archive": {"format": "zip", "name_format": "zip", "signature": "zip"}})
    lines = ["\t".join(verifier.ARCHIVE_LISTING_COLUMNS)]
    lines += [f"{member}\tfile\t100\t00000000\t2026-01-01 00:00:00\t1\tbundle.zip\t{member}\textracted"
              for member in members]
    lines.append("notes.txt\tfile\t5\t00000000\t2026-01-01 00:00:00\t1\tbundle.zip\tnotes.txt\textracted")
    data = ("\n".join(lines) + "\n").encode("utf-8")
    listing = unit.root / "provenance" / "archive-members-abcdef012345.tsv"
    listing.write_bytes(data)
    unit.manifest["archive_extractions"] = [{
        "archive_name": "bundle.zip", "archive_sha256": "0" * 64,
        "members_tsv": {"path": str(listing), "rows": len(lines) - 1,
                        "sha256": "f" * 64 if corrupt else hashlib.sha256(data).hexdigest()}}]
    unit.manifest["project"]["download_scope"] = {"bundle_shared_unit_count": shared}


class DeliveredButUnpairedTests(unittest.TestCase):
    """CLS-2: a sample whose file the download delivered and the lease did not pair is a mapping failure (FAIL,
    delivered_unpaired_samples); one the delivery holds nothing for stays undelivered (WARN)."""

    ST001264 = UndeliveredSampleTests.ST001264

    def test_st004304_a_member_named_loosely_after_the_sample_is_delivered_and_unpaired(self) -> None:
        """ST004304: the archive holds QC-D5-C-.mzML, marked extracted; the lease paired nothing with QC-D5-C."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("QC-D5-A.mzML", "QC-D5-A")
            unit.manifest["project"]["sample_metadata"].append({"sample_id": "QC-D5-C", "raw_file": "QC-D5-C.mzML"})
            unit.manifest["project"]["class_proposal"]["assignments"].append({"sample_id": "QC-D5-C", "class_label": "A"})
            _archive(unit, ["QC-D5-A.mzML", "QC-D5-C-.mzML"])
            unit.prepare()
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertEqual({"QC-D5-C": ["QC-D5-C-.mzML"]}, check.evidence["delivered_unpaired_samples"])
        self.assertEqual(1, check.evidence["delivered_unpaired_count"])
        self.assertEqual(["QC-D5-C-.mzML"], check.evidence["unpaired_delivered_files"])
        self.assertNotIn("undelivered_samples", check.evidence)
        self.assertNotIn("missing", check.evidence)
        self.assertIn("QC-D5-C as QC-D5-C-.mzML", check.detail)
        self.assertIn("a name-pairing failure, not a missing download", check.detail)
        self.assertIn("the run covers 1 of the 2 approved samples", check.detail)
        self.assertEqual(verifier.RECORD_ONLY, check.run_policy)

    def test_st001264_the_28_rows_beside_28_unpaired_members_are_delivered_and_unpaired(self) -> None:
        """ST001264: the archive holds BioRec1-3 behind a prefix, which the lease paired, and Youn_sa1..28.raw,
        which carry no row's name. Which member is which row is not the gate's to say; that they are there is."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = _paired_unit(temporary, 31, self.ST001264)
            _archive(unit, [*self.ST001264.values(),
                            *(f"021518_387057_CSHp_Youn_sa{number}.raw" for number in range(1, 29))])
            unit.prepare()
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertEqual(28, check.evidence["delivered_unpaired_count"])
        self.assertEqual([], check.evidence["delivered_unpaired_samples"]["Sample1"])
        self.assertEqual(28, check.evidence["unpaired_delivered_file_count"])
        self.assertIn("021518_387057_CSHp_Youn_sa1.raw", check.evidence["unpaired_delivered_files"])
        self.assertNotIn("undelivered_samples", check.evidence)
        self.assertEqual(31, check.evidence["delivered_files"])

    def test_a_download_shared_with_other_units_leaves_unnamed_members_to_them(self) -> None:
        """Unpaired members of a bundle other units share may be theirs: a sample none is named after stays
        undelivered, and one a member is named after is still unpaired."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("a.mzML", "a")
            for sample in ("z", "y"):
                unit.manifest["project"]["sample_metadata"].append({"sample_id": sample, "raw_file": f"{sample}.mzML"})
                unit.manifest["project"]["class_proposal"]["assignments"].append({"sample_id": sample, "class_label": "A"})
            _archive(unit, ["a.mzML", "y_.mzML", "other_unit_1.mzML"], shared=2)
            unit.prepare()
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertEqual({"y": ["y_.mzML"]}, check.evidence["delivered_unpaired_samples"])
        self.assertEqual({"z": ["z.mzML"]}, check.evidence["undelivered_samples"])
        self.assertTrue(check.evidence["download_shared_with_other_units"])

    def test_an_archive_that_holds_nothing_else_leaves_the_sample_undelivered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("a.mzML", "a")
            unit.manifest["project"]["sample_metadata"].append({"sample_id": "z", "raw_file": "z.mzML"})
            unit.manifest["project"]["class_proposal"]["assignments"].append({"sample_id": "z", "class_label": "A"})
            _archive(unit, ["a.mzML"])
            unit.prepare()
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual({"z": ["z.mzML"]}, check.evidence["undelivered_samples"])
        self.assertEqual([], check.evidence["unpaired_delivered_files"], "notes.txt is no input")
        self.assertNotIn("delivered_unpaired_samples", check.evidence)

    def test_a_member_inside_a_vendor_folder_is_that_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("a.mzML", "a")
            unit.manifest["project"]["sample_metadata"].append({"sample_id": "Z", "raw_file": "Z.d"})
            unit.manifest["project"]["class_proposal"]["assignments"].append({"sample_id": "Z", "class_label": "A"})
            _archive(unit, ["a.mzML", "data/Z.d/AcqData/MSScan.bin", "data/Z.d/AcqData/MSPeak.bin"])
            unit.prepare()
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertEqual({"Z": ["Z.d"]}, check.evidence["delivered_unpaired_samples"])
        self.assertEqual(["Z.d"], check.evidence["unpaired_delivered_files"])

    def test_a_delivery_that_cannot_be_read_establishes_nothing(self) -> None:
        """An archive whose member listing changed since its sha256 was recorded: neither delivered nor
        undelivered is known, and the sample is missing, as before the lineage."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("a.mzML", "a")
            unit.manifest["project"]["sample_metadata"].append({"sample_id": "z", "raw_file": "z.mzML"})
            unit.manifest["project"]["class_proposal"]["assignments"].append({"sample_id": "z", "class_label": "A"})
            _archive(unit, ["a.mzML"], corrupt=True)
            unit.prepare()
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertEqual(["z"], check.evidence["missing"])
        self.assertIn("has changed since its sha256 was recorded", check.evidence["delivery_not_established"])
        self.assertNotIn("undelivered_samples", check.evidence)

    def test_the_csv_record_is_not_what_decides_delivery(self) -> None:
        """samples_without_input is the CSV writer's own account, and says nothing of what was delivered."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("QC-D5-A.mzML", "QC-D5-A")
            unit.manifest["project"]["sample_metadata"].append({"sample_id": "QC-D5-C", "raw_file": "QC-D5-C.mzML"})
            unit.manifest["project"]["class_proposal"]["assignments"].append({"sample_id": "QC-D5-C", "class_label": "A"})
            _archive(unit, ["QC-D5-A.mzML", "QC-D5-C-.mzML"])
            unit.prepare()
            unit.manifest["analysis_csv"]["samples_without_input"] = []
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("QC-D5-C", check.evidence["delivered_unpaired_samples"])


def _never_shipped(unit: FolderBranchUnit, sample: str = "NeverShipped", raw_file: str = "NeverShipped.raw") -> None:
    """An approved sample the repository never shipped: a sample row and a Class assignment, and no download."""
    unit.manifest["project"]["sample_metadata"].append({"sample_id": sample, "raw_file": raw_file})
    unit.manifest["project"]["class_proposal"]["assignments"].append({"sample_id": sample, "class_label": "A"})


class DownloadsThatAreNoArchiveTests(unittest.TestCase):
    """CLS-2 counts a vendor folder downloaded file by file as one input, and a companion file as none (review of
    gate PR #31, round 4, finding 1). Before, every _FUNC001.DAT of a MetaboBank Waters .raw folder and every
    .wiff.scan of a SCIEX unit was an unpaired container, and an approved sample never shipped FAILed as
    delivered but unpaired."""

    def test_a_waters_folder_fetched_file_by_file_is_one_input(self) -> None:
        """MTBKS217 / MTBKS281: the downloads are Lm1.raw/_FUNC001.DAT, Lm1.raw/_extern.inf, ...; the input is
        Lm1.raw, which the lease paired. A sample no file was delivered for is undelivered, a WARN."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.folder("raw/Lm1.raw", "Lm1")
            unit.folder("raw/Lm2.raw", "Lm2")
            _never_shipped(unit)
            unit.prepare()
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual({"NeverShipped": ["NeverShipped.raw"]}, check.evidence["undelivered_samples"])
        self.assertEqual([], check.evidence["unpaired_delivered_files"])
        self.assertNotIn("delivered_unpaired_samples", check.evidence)
        self.assertEqual(2, check.evidence["delivered_files"], "two folders, not four member files")

    def test_a_wiff_scan_companion_is_no_input(self) -> None:
        """MTBKS236: DEN_1.wiff and DEN_1.wiff.scan are both downloads; the .scan is the .wiff's companion."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            for name in ("DEN_1", "DEN_2"):
                unit.file(f"raw/{name}.wiff", name)
                unit._download(unit.data / "raw" / f"{name}.wiff.scan", name.encode("utf-8") * 2, verified=True)
            _never_shipped(unit, "DEN_9", "DEN_9.wiff")
            unit.prepare()
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual({"DEN_9": ["DEN_9.wiff"]}, check.evidence["undelivered_samples"])
        self.assertEqual([], check.evidence["unpaired_delivered_files"])
        self.assertEqual(2, check.evidence["delivered_files"])

    def test_a_waters_folder_delivered_and_left_unpaired_is_still_a_fail(self) -> None:
        """The folder is one input either way: delivered file by file and paired with nothing, it is unpaired."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.folder("raw/Lm1.raw", "Lm1")
            for member in ("_FUNC001.DAT", "_extern.inf", "SystemSettings.xml"):
                unit._download(unit.data / "raw" / "Lm9_.raw" / member, member.encode("utf-8"), verified=True)
            _never_shipped(unit, "Lm9", "Lm9.raw")
            unit.prepare()
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertEqual({"Lm9": ["Lm9_.raw"]}, check.evidence["delivered_unpaired_samples"])
        self.assertEqual(["Lm9_.raw"], check.evidence["unpaired_delivered_files"])
        self.assertEqual(2, check.evidence["delivered_files"])

    def test_the_container_a_download_is_or_is_inside(self) -> None:
        root = r"D:\ws\metabobank\MTBKS281\u\raw"
        cases = {
            rf"{root}\data\raw\Lm1.raw\_FUNC001.DAT": "data/raw/Lm1.raw",
            rf"{root}\data\raw\Lm1.raw\SystemSettings.xml": "data/raw/Lm1.raw",
            rf"{root}\data\raw\DEN_1.wiff": "data/raw/DEN_1.wiff",
            rf"{root}\data\raw\DEN_1.wiff.scan": "",
            rf"{root}\data\QC_01.mzML": "data/QC_01.mzML",
            rf"{root}\data\X.d\AcqData\MSScan.bin": "data/X.d",
            rf"{root}\README.txt": "",
        }
        for path, container in cases.items():
            with self.subTest(path=path):
                self.assertEqual(container, verifier._download_container(path, root))
        # Below the raw directory only: a parent folder with a container suffix is not the input.
        self.assertEqual("data/a.mzML", verifier._download_container(r"D:\x.d\raw\data\a.mzML", r"D:\x.d\raw"))


class StepFloorTests(unittest.TestCase):
    """PKH-1 holds the absolute floor (10 QTOF-type, 100 FT), not the family step a diagnostic records."""

    def _pkh1(self, *diagnostics: dict, instrument: str = "", production: "list[dict] | None" = None):
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("a.mzML", "a")
            unit.manifest["peak_height_diagnostics"] = list(diagnostics)
            if instrument:
                unit.manifest["project"]["repository_metadata"] = {
                    "catalog_handoff": {"technical_settings": {"instrument": instrument}}}
            if production is not None:
                unit.manifest["production_peak_counts"] = production
            _method(unit, diagnostics[-1]["minimum_peak_height"])
            return _check(unit.gate(), "PKH-1")

    def test_a_family_step_of_10_falling_back_to_1_is_refused(self) -> None:
        """The review's probe: an estimate asked for at step 10 records 10 as its family step and falls back
        to 1 (threshold 2, the noise floor). It passed, being the recorded family step divided by ten."""
        check = self._pkh1(_diagnostic(2, count=21578, estimated=4123, step=1, within=True,
                                       coarse_threshold_step=10, step_fallback=True,
                                       fallback_reason="no_coarse_step_in_range"))

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        broken = " | ".join(check.evidence["step_rule_broken"])
        self.assertIn("finer than the floor of 10 for QTOF-type data", broken)
        self.assertIn("neither 100 (QTOF-type) nor 1,000 (Fourier-transform)", broken)
        self.assertEqual(10, check.evidence["step_floor"])
        self.assertEqual(verifier.RECORD_ONLY, check.run_policy)

    def test_a_family_step_of_10_with_no_fallback_is_refused(self) -> None:
        check = self._pkh1(_diagnostic(20, count=21578, estimated=3600, step=10, within=True,
                                       coarse_threshold_step=10, step_fallback=False, fallback_reason=None))

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("neither 100", " ".join(check.evidence["step_rule_broken"]))

    def test_a_fourier_transform_unit_never_goes_below_100(self) -> None:
        """An Orbitrap published as mzML, which Interactive labels QTOF: the Catalog's instrument says FT. A
        search at 100 that falls back to 10 breaks the floor of 100."""
        check = self._pkh1(_diagnostic(30, count=10168, estimated=4722, step=10, within=True,
                                       coarse_threshold_step=100, step_fallback=True, fallback_reason="r"),
                           instrument="Thermo Q Exactive HF hybrid Orbitrap")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("finer than the floor of 100 for Fourier-transform data (the unit's instrument is Thermo Q "
                      "Exactive HF hybrid Orbitrap)", " ".join(check.evidence["step_rule_broken"]))
        self.assertEqual("fourier", check.evidence["instrument_family_for_step"])

    def test_a_fourier_transform_fallback_to_100_passes_and_a_qtof_one_to_10_too(self) -> None:
        for instrument, coarse, step, threshold in (("Thermo Orbitrap Exploris 480", 1000, 100, 8600),
                                                    ("Bruker impact II UHR-TOF", 100, 10, 30)):
            with self.subTest(instrument=instrument):
                check = self._pkh1(_diagnostic(threshold, count=10168, estimated=4722, step=step, within=True,
                                               coarse_threshold_step=coarse, step_fallback=True, fallback_reason="r"),
                                   instrument=instrument)
                self.assertEqual(verifier.PASS, check.status, check.detail)
                self.assertNotIn("step_rule_broken", check.evidence)

    def test_a_fourier_transform_unit_searched_at_100_is_to_be_read(self) -> None:
        check = self._pkh1(_diagnostic(200, count=35678, estimated=3517, step=100, within=True,
                                       coarse_threshold_step=100, step_fallback=False, fallback_reason=None),
                           instrument="Thermo Q Exactive Orbitrap")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("take 1,000", check.detail)

    def test_the_production_runs_peak_counts_are_reported_where_recorded(self) -> None:
        """Interactive 0.5.28 appends production_peak_counts after each run; PKH-1 reports it and judges nothing."""
        record = {"schema": "msdial-production-peak-counts.v1", "job_id": "run1", "run_complete": True,
                  "minimum_peak_height": 200.0, "estimated_peak_count": 3517, "representative_file_name": "QC_05",
                  "representative_peak_count": 2810, "representative_to_estimate_ratio": 0.799,
                  "representative_within_target_range": False, "file_count": 4, "files_counted": 4,
                  "peak_count_min": 2500, "peak_count_median": 2800, "peak_count_max": 3100, "peak_count_total": 11200,
                  "files": [{"file_name": "QC_05", "peak_count": 2810}]}
        check = self._pkh1(_diagnostic(200, count=35678, estimated=3517, step=100, within=True,
                                       coarse_threshold_step=100, step_fallback=False, fallback_reason=None),
                           production=[record])

        self.assertEqual(verifier.PASS, check.status, check.detail)
        production = check.evidence["production_peak_counts"]
        self.assertEqual(2810, production["representative_peak_count"])
        self.assertTrue(production["matches_method_threshold"])
        self.assertNotIn("files", production)
        self.assertIn("kept 2810 peaks in the representative file against the estimate of 3517", check.detail)

    def test_no_production_record_reports_nothing_of_one(self) -> None:
        check = self._pkh1(_diagnostic(200, count=35678, estimated=3517, step=100, within=True))

        self.assertNotIn("production_peak_counts", check.evidence)


def _with_family(diagnostic: dict, family: str, source: str) -> dict:
    """The family Interactive 0.5.28 records with a diagnostic, on its representative and on the record."""
    diagnostic["representative"].update(instrument_family=family, instrument_family_source=source)
    diagnostic.update(instrument_family=family, instrument_family_source=source)
    return diagnostic


class FamilyFromTheFileTests(unittest.TestCase):
    """PKH-1 takes the instrument family from the file first, as Interactive 0.5.28 does
    (representative_instrument_family), and the Catalog's instrument text only where the file says nothing
    (review of gate PR #31, round 4, finding 2)."""

    _pkh1 = StepFloorTests._pkh1
    MULTI_PLATFORM = "Thermo Q Exactive; SCIEX TripleTOF 6600"

    def _fallback_to_10(self) -> dict:
        return _diagnostic(30, count=10168, estimated=4722, step=10, within=True,
                           coarse_threshold_step=100, step_fallback=True, fallback_reason="no_coarse_step_in_range")

    def test_a_vendor_format_qtof_is_not_overruled_by_the_catalogs_text(self) -> None:
        """A SCIEX .wiff in a study whose Catalog lists a Q Exactive too: Interactive keeps it QTOF, searches at
        100 and falls back to 10, which the QTOF floor allows."""
        check = self._pkh1(_with_family(self._fallback_to_10(), "QTOF", "vendor_format"),
                           instrument=self.MULTI_PLATFORM)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual("qtof", check.evidence["instrument_family_for_step"])
        self.assertEqual(10, check.evidence["step_floor"])
        self.assertNotIn("step_rule_broken", check.evidence)
        self.assertNotIn("step_rule_notes", check.evidence)

    def test_an_mzml_header_naming_a_tof_is_not_overruled_either(self) -> None:
        check = self._pkh1(_with_family(_diagnostic(200, count=35678, estimated=3517, step=100, within=True,
                                                    coarse_threshold_step=100, step_fallback=False,
                                                    fallback_reason=None),
                                        "QTOF", "mzml_instrument_configuration"),
                           instrument="Thermo Q Exactive Orbitrap")

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual("qtof", check.evidence["instrument_family_for_step"])
        self.assertNotIn("take 1,000", check.detail)

    def test_a_format_default_gives_way_to_the_catalogs_fourier_transform_text(self) -> None:
        """An mzML whose header names no instrument: Interactive itself lets the declared instrument decide, so a
        fallback to 10 on an Orbitrap unit is below the floor of 100."""
        check = self._pkh1(_with_family(self._fallback_to_10(), "QTOF", "format_default"),
                           instrument="Thermo Q Exactive HF hybrid Orbitrap")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertEqual("fourier", check.evidence["instrument_family_for_step"])
        self.assertIn("finer than the floor of 100", " ".join(check.evidence["step_rule_broken"]))

    def test_a_recorded_fourier_transform_family_stands_whatever_the_catalog_says(self) -> None:
        check = self._pkh1(_with_family(self._fallback_to_10(), "Fourier-transform MS", "mzml_instrument_configuration"),
                           instrument="Bruker impact II UHR-TOF")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertEqual("fourier", check.evidence["instrument_family_for_step"])
        self.assertIn("from mzml_instrument_configuration", " ".join(check.evidence["step_rule_broken"]))

    def test_an_unknown_family_from_the_file_leaves_the_catalog_to_decide(self) -> None:
        check = self._pkh1(_with_family(self._fallback_to_10(), "Unknown", "vendor_format"),
                           instrument="Thermo Orbitrap Exploris 480")

        self.assertEqual("fourier", check.evidence["instrument_family_for_step"])

    def test_a_multi_platform_unit_as_the_runner_now_records_it_passes(self) -> None:
        """A SCIEX .wiff in a study whose Catalog lists a Q Exactive too, diagnosed as the runner does it since
        Interactive 0.5.28 (msdial-interactive-app#61): asked for no step, Interactive searched its QTOF family's
        100 and the threshold is a multiple of 100. Nothing to read: the Catalog's text decides nothing here."""
        check = self._pkh1(_with_family(_diagnostic(200, count=35678, estimated=3517, step=100, within=True,
                                                    coarse_threshold_step=100, step_fallback=False,
                                                    fallback_reason=None, requested_threshold_step=None,
                                                    requested_step_disposition=None),
                                        "QTOF", "vendor_format"),
                           instrument=self.MULTI_PLATFORM)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual("qtof", check.evidence["instrument_family_for_step"])
        self.assertNotIn("step_rule_notes", check.evidence)

    def test_a_search_not_at_the_recorded_familys_step_is_a_note_naming_it(self) -> None:
        """A record whose search began at 1,000 on a family it records as QTOF from the file. Interactive 0.5.28
        never makes one (it always searches its family's step first and only records a requested step, and the
        runner asks for none); a build that searched a requested step did, #61 before its review. The floor is
        the QTOF one, and the note says the search was not the rule's."""
        check = self._pkh1(_with_family(_diagnostic(3000, count=35678, estimated=3517, step=1000, within=True,
                                                    coarse_threshold_step=1000, step_fallback=False,
                                                    fallback_reason=None),
                                        "QTOF", "vendor_format"),
                           instrument=self.MULTI_PLATFORM)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual("qtof", check.evidence["instrument_family_for_step"])
        self.assertNotIn("step_rule_broken", check.evidence)
        self.assertIn("searched first at step 1,000, where QTOF-type data", check.detail)
        self.assertIn("whose step is 100", check.detail)
        self.assertIn("not made by its rule", check.detail)

    def test_a_search_at_the_recorded_familys_step_is_not_called_off_rule(self) -> None:
        """A format default kept QTOF and searched at its own family's 100, where the gate's reading of the
        Catalog's text makes it Fourier-transform data for the floor. Interactive 0.5.28 reads that text with the
        same tokens and would have named the family Fourier-transform from it, so this record comes from a
        diagnostic that saw no such text (synthetic here). The note says 1,000 is the step for that data, and
        does not call the search one Interactive's rule did not make: the search was the rule's for the family
        recorded."""
        check = self._pkh1(_with_family(_diagnostic(200, count=35678, estimated=3517, step=100, within=True,
                                                    coarse_threshold_step=100, step_fallback=False,
                                                    fallback_reason=None),
                                        "QTOF", "format_default"),
                           instrument="Thermo Q Exactive HF hybrid Orbitrap")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual("fourier", check.evidence["instrument_family_for_step"])
        self.assertIn("searched first at step 100, where Fourier-transform data", check.detail)
        self.assertNotIn("not made by its rule", check.detail)


class InstrumentTextAsInteractiveReadsItTests(unittest.TestCase):
    """PKH-1 reads the Catalog's instrument text with Interactive 0.5.28's own tokens
    (workflow.instrument_family_from_text, msdial-interactive-app#61), word boundaries included, so an HPLC
    column beside the instrument names no Fourier-transform analyser (review of gate PR #31, round 5).

    The name lists are Interactive's own (tests/test_instrument_family.py at origin/feat/threshold-step-fallback
    f3df524): every name it calls Fourier-transform or FT-ICR the gate calls Fourier-transform, and every name it
    calls neither the gate does not."""

    _pkh1 = StepFloorTests._pkh1
    FOURIER = [
        "Q Exactive", "Q Exactive HF", "Q Exactive HF-X", "Q Exactive Plus", "Exactive", "Exactive Plus",
        "Orbitrap Exploris 480", "Orbitrap Fusion", "Orbitrap Fusion Lumos", "Orbitrap Eclipse", "Orbitrap Ascend",
        "Orbitrap Astral", "LTQ Orbitrap", "LTQ Orbitrap XL", "LTQ Orbitrap Velos", "Orbitrap Velos Pro",
        "Orbitrap Elite", "Orbitrap ID-X", "Orbitrap IQ-X", "orbitrap",
        "Thermo Fusion Tribrid Orbitrap", "Thermo Q Exactive HF hybrid Orbitrap", "Thermo Q Exactive Orbitrap",
        "LC, Nexera X2 (Shimadzu Co.); MS, Q Exactive HF (Thermo Fisher Scientific Inc.)",
        "Thermo Scientific Orbitrap ID-X Tribrid", "Thermo Exploris 240", "Thermo IQ-X tribrid",
    ]
    FT_ICR = ["solariX", "solariX XR", "apex ultra", "APEX-Qe", "Bruker APEX-Qe 9.4T", "scimaX", "LTQ FT",
              "LTQ FT Ultra", "fourier transform ion cyclotron resonance mass spectrometer"]
    NOT_FOURIER = [
        "LTQ", "LTQ Velos", "Velos Plus", "Velos Pro", "LTQ XL", "TSQ Altis", "TSQ Quantiva", "ISQ", "Stellar",
        "EVOQ Elite", "maXis", "impact II", "timsTOF Pro", "Xevo G2-XS QTof", "Synapt G2-Si", "TripleTOF 5600",
        "QTRAP 6500", "6545 Q-TOF LC/MS", "LCMS-9030", "time-of-flight",
        "Bruker maXis UHR-ToF", "Bruker impact II UHR-TOF", "Waters Xevo G2 QTof", "AB SCIEX TripleTOF 5600+",
        "Waters Acquity UPLC", "Nexera X2 (Shimadzu)", "Agilent 1100 HPLC (Agilent Technologies)",
        "Bruker Elute UHPLC system",
        "Agilent Zorbax Eclipse Plus C18", "Phenomenex Synergi Fusion-RP",
    ]
    # Column names in the forms a submitter writes them, beside a QTOF.
    COLUMNS_BESIDE_A_QTOF = [
        "Agilent 6545 Q-TOF; Zorbax Eclipse Plus C18", "Agilent 6545 Q-TOF; Phenomenex Synergi Fusion-RP",
        "SCIEX TripleTOF 6600, Zorbax Eclipse XDB-C18", "Waters Xevo G2-XS QTof (Eclipse Plus C8 column)",
        "Bruker impact II; Synergi 4 um Fusion RP 80A", "Agilent 6550 iFunnel Q-TOF; ZORBAX ECLIPSE PLUS",
    ]

    def test_interactives_fourier_transform_and_ft_icr_names_are_fourier_transform(self) -> None:
        for name in self.FOURIER + self.FT_ICR:
            with self.subTest(name=name):
                self.assertIsNotNone(verifier.FOURIER_INSTRUMENT.search(name))

    def test_interactives_other_names_and_column_names_are_not(self) -> None:
        for name in self.NOT_FOURIER + self.COLUMNS_BESIDE_A_QTOF:
            with self.subTest(name=name):
                self.assertIsNone(verifier.FOURIER_INSTRUMENT.search(name))

    def test_a_fourier_transform_instrument_beside_a_column_is_still_one(self) -> None:
        for name in ("Orbitrap Fusion Lumos; Zorbax Eclipse Plus C18", "Q Exactive HF; Synergi Fusion-RP",
                     "Orbitrap Eclipse Tribrid", "Thermo Fusion Lumos", "Bruker solariX; Eclipse Plus C18"):
            with self.subTest(name=name):
                self.assertIsNotNone(verifier.FOURIER_INSTRUMENT.search(name))

    def test_the_runner_reads_the_same_tokens(self) -> None:
        import campaign_fakes  # noqa: F401  (puts scripts/ on the path)
        from campaign import policy

        self.assertEqual(verifier.FOURIER_INSTRUMENT.pattern, policy._FOURIER_INSTRUMENT.pattern)
        self.assertEqual(verifier.FOURIER_INSTRUMENT.flags, policy._FOURIER_INSTRUMENT.flags)

    def test_the_reviewers_case_a_format_default_qtof_falling_back_to_10_passes(self) -> None:
        """Round 5's probe: a Bruker .d or an mzML whose header names no instrument, Catalog text "Agilent 6545
        Q-TOF; Zorbax Eclipse Plus C18". Interactive keeps it QTOF, searches 100 and falls back to 10. The gate
        read "eclipse" as an Orbitrap, held a floor of 100 and called the fallback broken: a FAIL."""
        for instrument in ("Agilent 6545 Q-TOF; Zorbax Eclipse Plus C18",
                           "Agilent 6545 Q-TOF; Phenomenex Synergi Fusion-RP"):
            with self.subTest(instrument=instrument):
                check = self._pkh1(_with_family(_diagnostic(30, count=10168, estimated=4722, step=10, within=True,
                                                            coarse_threshold_step=100, step_fallback=True,
                                                            fallback_reason="no_coarse_step_in_range"),
                                                "QTOF", "format_default"),
                                   instrument=instrument)

                self.assertEqual(verifier.PASS, check.status, check.detail)
                self.assertEqual("qtof", check.evidence["instrument_family_for_step"])
                self.assertEqual(10, check.evidence["step_floor"])
                self.assertNotIn("step_rule_broken", check.evidence)

    def test_the_reviewers_case_at_step_100_leaves_nothing_to_read(self) -> None:
        """The same unit with no fallback: it was a WARN note ("where Fourier-transform data take 1,000")."""
        check = self._pkh1(_with_family(_diagnostic(200, count=35678, estimated=3517, step=100, within=True,
                                                    coarse_threshold_step=100, step_fallback=False,
                                                    fallback_reason=None),
                                        "QTOF", "format_default"),
                           instrument="Agilent 6545 Q-TOF; Zorbax Eclipse Plus C18")

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertNotIn("step_rule_notes", check.evidence)
        self.assertNotIn("take 1,000", check.detail)

    def test_a_record_with_no_family_beside_a_column_is_qtof_type_too(self) -> None:
        """A diagnostic from before 0.5.28 records no family source; the Catalog's text decides, and a column
        name in it decides nothing."""
        check = self._pkh1(_diagnostic(30, count=10168, estimated=4722, step=10, within=True,
                                       coarse_threshold_step=100, step_fallback=True, fallback_reason="r"),
                           instrument="SCIEX TripleTOF 6600, Zorbax Eclipse XDB-C18")

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual("qtof", check.evidence["instrument_family_for_step"])

    def test_a_format_default_beside_an_orbitrap_named_with_a_column_still_takes_the_ft_floor(self) -> None:
        check = self._pkh1(_with_family(_diagnostic(30, count=10168, estimated=4722, step=10, within=True,
                                                    coarse_threshold_step=100, step_fallback=True,
                                                    fallback_reason="no_coarse_step_in_range"),
                                        "QTOF", "format_default"),
                           instrument="Orbitrap Fusion Lumos; Zorbax Eclipse Plus C18")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertEqual("fourier", check.evidence["instrument_family_for_step"])
        self.assertIn("finer than the floor of 100", " ".join(check.evidence["step_rule_broken"]))


class SplitPartPairingTests(unittest.TestCase):
    """PAIR-1 on a split part lists only that part's own pairings and inputs (review of gate PR #31, finding 2)."""

    def test_a_part_does_not_carry_its_siblings_pairings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = FolderBranchUnit(temporary, "parent")
            positive = parent.file("pos_A.raw", "A")
            negative = parent.file("x_neg_B.raw", "B")
            row = next(row for row in parent.manifest["input_lineage"]["rows"] if row["path"] == negative)
            row["name_pairing"] = {"paired_by": "prefixed_member_name", "declared_raw_file": "neg_B.raw",
                                   "member_name": "x_neg_B.raw"}
            parent_root = parent.write()
            part = {"split_from": {"manifest_path": str(parent_root / "provenance" / "run-manifest.json")},
                    "input_candidates": [positive], "input_lineage": {"rows": [], "excluded": []}}
            sibling = dict(part, input_candidates=[negative])
            checks = {}
            for name, manifest in (("part", part), ("sibling", sibling)):
                report = verifier.Report(Path(temporary) / name)
                verifier.check_inferred_name_pairings_are_listed(report, manifest, "")
                checks[name] = report.checks[0]

        self.assertEqual(verifier.PASS, checks["part"].status, checks["part"].detail)
        self.assertEqual(1, checks["part"].evidence["inputs"])
        self.assertEqual(verifier.WARN, checks["sibling"].status, checks["sibling"].detail)
        self.assertEqual(1, checks["sibling"].evidence["inputs"])
        self.assertEqual(["x_neg_B.raw"], [entry["member_name"] for entry in checks["sibling"].evidence["pairings"]])

    def test_a_part_reads_its_own_lineage_rows_too(self) -> None:
        """Interactive's split copies the parent's rows of the part's own inputs into the part (inherited_from)."""
        with tempfile.TemporaryDirectory() as temporary:
            parent = FolderBranchUnit(temporary, "parent")
            negative = parent.file("x_neg_B.raw", "B")
            parent.file("pos_A.raw", "A")
            row = next(row for row in parent.manifest["input_lineage"]["rows"] if row["path"] == negative)
            row["name_pairing"] = {"paired_by": "prefixed_member_name", "declared_raw_file": "neg_B.raw",
                                   "member_name": "x_neg_B.raw"}
            parent_root = parent.write()
            part = {"split_from": {"manifest_path": str(parent_root / "provenance" / "run-manifest.json")},
                    "input_candidates": [negative], "input_lineage": {"rows": [dict(row)], "excluded": []}}
            report = verifier.Report(Path(temporary) / "part")
            verifier.check_inferred_name_pairings_are_listed(report, part, "")

        check = report.checks[0]
        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual(1, check.evidence["inputs"], "the part's row and the parent's copy of it are one input")
        self.assertEqual(1, check.evidence["paired_by_inference"])


import test_verify_inputs_and_acquisition as acquisition  # noqa: E402


class HeaderFirstAcquisitionTests(unittest.TestCase):
    """ACQ-1 under rule B2 (2026-10-06, msdial-interactive-app#62 at 4802a98): the header's Console type binds
    every row at any confidence; the declaration is named only where the header gave no Console type."""

    verifier = acquisition.verifier

    def _acq1(self, header: str, header_console: "str | None", console: "str | None", csv_type: str, *,
              basis: str = "header", confidence: float = 0.75, declared: "str | None" = None,
              declared_source: "str | None" = None, has_ms2: bool = True, disposition_extra=None):
        with tempfile.TemporaryDirectory() as temporary:
            unit = acquisition.Unit(temporary)
            paths = unit.inputs(["S0.mzML"])
            record = acquisition._record(paths[0], header, console, confidence=confidence,
                                         console_acquisition_basis=basis,
                                         header_console_acquisition_type=header_console,
                                         header_console_acquisition_basis="header" if header_console else "")
            record["has_ms2"] = has_ms2
            unit.preflight([record])
            disposition = acquisition._disposition([])
            if declared is not None:
                disposition["declared"] = {"acquisition_mode": declared, "ion_mode": "Negative",
                                           "separation": "LC-MS", "untargeted": True}
            if declared_source is not None:
                disposition["declared_acquisition_source"] = declared_source
            disposition.update((disposition_extra(paths[0]) if callable(disposition_extra) else disposition_extra) or {})
            unit.manifest["campaign_disposition"] = disposition
            unit.csv(unit.rows([csv_type]))
            return _check(self.verifier.verify(unit.write(), "before-production"), "ACQ-1")

    def test_mtbls1572_dda_headers_run_as_swath_are_refused(self) -> None:
        """MTBLS1572: six DDA headers below 0.8, which the old disposition kept as the declared SWATH."""
        check = self._acq1("DDA", "DDA", "SWATH", "SWATH", basis="declaration", confidence=0.75, declared="DIA")

        self.assertEqual(self.verifier.FAIL, check.status, check.detail)
        self.assertIn("S0: its header gives DDA, and the Console will deconvolute it as SWATH", check.detail)

    def test_a_header_runs_as_itself_at_any_confidence(self) -> None:
        check = self._acq1("DDA", "DDA", "DDA", "DDA", confidence=0.3, declared="DIA",
                           declared_source="catalog_keyword_inference",
                           disposition_extra={"declared_vs_header": [
                               {"file": "S0", "declared": "DIA", "header": "DDA", "confidence": 0.3, "decided": "DDA",
                                "basis": "header", "declaration_source": "catalog_keyword_inference"}]})

        self.assertEqual(self.verifier.PASS, check.status, check.detail)
        self.assertEqual({"header": 1}, check.evidence["sources"])
        self.assertEqual(1, check.evidence["header_overrides_declaration"])
        self.assertEqual({"catalog_keyword_inference": 1}, check.evidence["declaration_sources"])

    def test_a_row_against_the_type_the_record_decided_is_refused(self) -> None:
        check = self._acq1("DIA", None, "AIF", "SWATH", basis="declaration", declared="AIF")

        self.assertEqual(self.verifier.FAIL, check.status, check.detail)
        self.assertIn("S0: its record decided AIF, and the Console will deconvolute it as SWATH", check.detail)

    def test_the_declaration_where_the_header_gave_no_console_type_is_a_warning(self) -> None:
        """A DIA header whose single isolation target left SWATH and AIF open: the declared SWATH runs."""
        check = self._acq1("DIA", None, "SWATH", "SWATH", basis="declaration", declared="DIA")

        self.assertEqual(self.verifier.WARN, check.status, check.detail)
        self.assertEqual({"declaration": 1}, check.evidence["sources"])
        self.assertIn("no Console acquisition type", check.detail)

    def test_a_declaration_the_header_agrees_with_is_not_a_warning(self) -> None:
        check = self._acq1("AIF", "AIF", "AIF", "AIF", basis="declaration", declared="AIF")

        self.assertEqual(self.verifier.PASS, check.status, check.detail)

    def test_an_aif_header_run_as_swath_is_refused_while_no_mapping_is_sanctioned(self) -> None:
        check = self._acq1("AIF", "AIF", "SWATH", "SWATH", basis="declaration", declared="DIA",
                           disposition_extra={"sanctioned_acquisition_mappings": [
                               {"file": "S0", "header": "AIF", "runs_as": "SWATH"}]})

        self.assertEqual({}, self.verifier.SANCTIONED_ACQUISITION_MAPPINGS, "nothing is sanctioned yet")
        self.assertEqual(self.verifier.FAIL, check.status, check.detail)
        self.assertIn("its header gives AIF, and the Console will deconvolute it as SWATH", check.detail)

    def test_a_sanctioned_mapping_is_accepted_only_where_the_disposition_records_it_for_the_file(self) -> None:
        """The table a later rule (AIF run as SWATH, pending with the user) will fill; its field is that rule's."""
        with mock.patch.dict(self.verifier.SANCTIONED_ACQUISITION_MAPPINGS,
                             {("AIF", "SWATH"): "sanctioned_acquisition_mappings"}):
            for file, status in ((None, self.verifier.PASS), ("elsewhere.mzML", self.verifier.FAIL)):
                with self.subTest(file=file):
                    check = self._acq1("AIF", "AIF", "SWATH", "SWATH", basis="declaration", declared="AIF",
                                       disposition_extra=lambda path, file=file: {"sanctioned_acquisition_mappings": [
                                           {"file": file or path, "header": "AIF", "runs_as": "SWATH"}]})
                    self.assertEqual(status, check.status, check.detail)
                    if status == self.verifier.PASS:
                        self.assertEqual(1, check.evidence["sanctioned_mappings"])

    def test_an_ms1_only_file_folded_into_dda_in_a_declared_dia_unit_is_refused(self) -> None:
        """MTBKS217's z_014nn under its old disposition: rule B2 excludes such files."""
        for source, said in ((None, "the disposition predates Interactive 0.5.29"),
                             ("catalog_keyword_inference", "declared by catalog_keyword_inference")):
            with self.subTest(source=source):
                check = self._acq1("FullScan", None, "DDA", "DDA", basis="folded_ms1_only", declared="DIA",
                                   declared_source=source, has_ms2=False)
                self.assertEqual(self.verifier.FAIL, check.status, check.detail)
                self.assertIn("ms1_only_in_declared_dia_unit", check.detail)
                self.assertIn(said, check.detail)

    def test_an_ms1_only_file_folded_into_dda_elsewhere_is_a_warning(self) -> None:
        check = self._acq1("FullScan", None, "DDA", "DDA", basis="folded_ms1_only", declared="DDA", has_ms2=False)

        self.assertEqual(self.verifier.WARN, check.status, check.detail)
        self.assertEqual({"folded_ms1_only": 1}, check.evidence["sources"])

    def test_a_legacy_record_is_read_from_its_acquisition_mode(self) -> None:
        """A record from before header_console_acquisition_type: a DDA header gives DDA, at any confidence."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = acquisition.Unit(temporary)
            paths = unit.inputs(["S0.mzML"])
            unit.preflight([acquisition._record(paths[0], "DDA", "SWATH", confidence=0.5,
                                                console_acquisition_basis="declaration")])
            unit.manifest["campaign_disposition"] = acquisition._disposition([])
            unit.csv(unit.rows(["SWATH"]))
            check = _check(self.verifier.verify(unit.write(), "before-production"), "ACQ-1")

        self.assertEqual(self.verifier.FAIL, check.status, check.detail)
        self.assertIn("S0: its header gives DDA, and the Console will deconvolute it as SWATH", check.detail)


if __name__ == "__main__":
    unittest.main()
