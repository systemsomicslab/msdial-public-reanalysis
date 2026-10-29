"""A vendor companion the Console needs, and a polarity read from a file name: the user's decisions of 2026-10-29.

The verification campaign v4-both-20261029 met both. MB-POST MPST000063 deposited 20 SCIEX .wiff and no .wiff.scan,
and each part of its polarity split failed at its diagnostic start ("WIFF.SCAN is not accessible next to ...") and
was retried: such a start is now an exclusion, never retried (Interactive 0.5.53 also excludes the input at its
disposition). MB-POST MPST000048's ZenoTOF .wiff2 cannot be read by the pinned extractor, so Interactive 0.5.53 reads
each one's polarity from its file name, on record; the gate's ACQ-1 accepts exactly such a recorded inference for an
input whose header could not be read, and nothing else.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes as fakes  # noqa: E402  (puts scripts/ on the path)
import test_campaign_contract as contract  # noqa: E402  (puts Interactive and the Catalog on the path)
import test_campaign_machine as machine_tests  # noqa: E402
from campaign import policy  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("verify_run_invariants_names", TESTS.parent / "scripts" / "verify-run-invariants.py")
assert _SPEC and _SPEC.loader
gate = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_run_invariants_names"] = gate
_SPEC.loader.exec_module(gate)

WIFF_SCAN_REFUSAL = {
    "ok": False, "reason": "validation_error", "http_status": 400, "error_type": "MsdialRequestError",
    "detail": "WIFF.SCAN is not accessible next to F:\\x\\raw\\data\\200925_Reelin_WT05_Pos.wiff. Use Add original "
              "files, Add original folder, or Add path so the original WIFF directory remains directly accessible.",
}


class AStartRefusedForTheUnitsOwnData(unittest.TestCase):
    def test_the_policy_reads_it_as_an_exclusion(self) -> None:
        self.assertEqual("vendor_companion_missing", policy.console_start_exclusion(WIFF_SCAN_REFUSAL)["reason"])
        for other in ({**WIFF_SCAN_REFUSAL, "detail": "Missing output root."}, {**WIFF_SCAN_REFUSAL, "reason": "exception"},
                      {"ok": True}, None):
            self.assertIsNone(policy.console_start_exclusion(other))


class TheRunnerExcludesItWithoutRetrying(machine_tests.Base):
    def test_a_diagnostic_start_refused_for_a_missing_wiff_scan_ends_excluded(self) -> None:
        world = self.world(("u1", "u2"))
        original = world.interactive.start_diagnostic

        def refuse(**arguments):
            if "u1" in str(arguments.get("input_path") or ""):
                return dict(WIFF_SCAN_REFUSAL)
            return original(**arguments)

        world.interactive.start_diagnostic = refuse
        book = self.finish(world)
        unit = book.unit("u1")

        self.assertEqual("excluded", unit["state"])
        self.assertEqual(0, int(unit["failures"]))
        self.assertEqual("vendor_companion_missing", json.loads(unit["terminal_detail"])["reason"])
        starts = [row for row in book.attempts("u1") if row["step"] == "diagnostic_start"]
        self.assertEqual(["blocked"], [row["outcome"] for row in starts], "never retried")
        self.assertEqual("done", book.unit("u2")["state"])


class TheNameRuleIsInteractives(unittest.TestCase):
    def test_the_rule_is_one_in_the_gate_and_interactive(self) -> None:
        try:
            from msdial_app import raw_metadata_preflight as interactive
        except ImportError:
            interactive = None
        if interactive is None or not hasattr(interactive, "file_name_polarity"):
            self.skipTest(
                "SKIPPED THE CHECK OF THE FILE-NAME POLARITY RULE: MSDIAL_INTERACTIVE_ROOT "
                f"({contract.INTERACTIVE_ROOT}) has no raw_metadata_preflight.file_name_polarity. Point "
                "MSDIAL_INTERACTIVE_ROOT at Interactive 0.5.53 or later to compare the gate's rule with Interactive's.")
        for name in ("FILE_NAME_POLARITY_RULE", "FILE_NAME_POLARITY_TOKENS", "FILE_NAME_POLARITY_SIGNS",
                     "FILE_NAME_POLARITY_EXEMPTING"):
            self.assertEqual(getattr(interactive, name), getattr(gate, name), name)
        for name in ("241202_Tomita_18_neg.wiff2", "S_Pos.wiff", "S1_ESI+.wiff2", "S1(-).raw", "x_pos_neg.raw",
                     "Neg_Ctrl_1.raw", "QC_pos_1.wiff2", "S1.wiff2", "pos/S1.wiff2", "position_1.raw",
                     # The review of #86: M1, L1 and L2's example names.
                     "LC-ESI-QTOF_S1.wiff2", "S1_ESI-MS_pos.wiff2", "S1(-)MS.raw", "S1_ESI-_2.wiff2",
                     "QC_pos_01.wiff2", "pos_QC.wiff2", "Blank_neg_1.wiff2", "S_pos_control.wiff2"):
            self.assertEqual(interactive.file_name_polarity(name), gate.file_name_polarity(name), name)
        root = "D:/w/raw/data"
        for path in (f"{root}/POS/ER_neg_01.wiff2", f"{root}/NEG/ER_neg_01.wiff2", f"{root}/POS/ER_pos_01.wiff2",
                     "D:/POS/w/ER_neg_01.wiff2"):
            self.assertEqual(interactive.file_name_polarity_reading(path, root),
                             gate.file_name_polarity_reading(path, root), path)
        self.assertEqual(gate.file_name_polarity("LC-ESI-QTOF_S1.wiff2"), None)
        self.assertEqual(gate.file_name_polarity("S1_ESI-MS_pos.wiff2"), ("Positive", "pos"))
        self.assertEqual(gate.file_name_polarity("QC_pos_01.wiff2"), ("Positive", "pos"))
        self.assertIsNone(gate.file_name_polarity(f"{root}/POS/ER_neg_01.wiff2", root))


class ACQ1AcceptsOnlyARecordedFileNameInference(unittest.TestCase):
    PATH = "D:\\w\\raw\\data\\241202_Tomita_18_neg.wiff2"

    def unit(self, inference: dict | None, *, declared: str = "Both", run: str = "Negative") -> tuple[dict, list[dict]]:
        record = {"file": self.PATH, "outcome": "unsupported_format", "acquisition_mode": "", "polarity": "",
                  "format": "sciex_wiff2", "header_console_acquisition_type": None, "console_acquisition_type": "DDA",
                  "console_acquisition_basis": "declaration"}
        disposition = {"schema": "msdial-campaign-disposition.v1", "disposition": "run", "reasons": [],
                       "warnings": ["acquisition_declared_only"], "excluded_inputs": [], "split_key": None,
                       "applied": True, **({"polarity_inferences": [inference]} if inference else {})}
        manifest = {
            "project": {"ion_mode": run, "acquisition_mode": "DDA", "separation": "LC-MS", "repository_metadata": {
                "catalog_handoff": {"technical_settings": {"ion_mode": declared, "acquisition_mode": "DDA"}}}},
            "input_candidates": [self.PATH], "campaign_disposition": disposition,
            "raw_metadata_preflight": {"summary": {"per_file": [record]}},
        }
        return manifest, [{"file_path": self.PATH, "file_name": "241202_Tomita_18_neg", "acquisition_type": "DDA"}]

    def acq1(self, manifest: dict, rows: list[dict]):
        report = gate.Report(Path(tempfile.gettempdir()))
        gate.check_acquisition_type_is_the_headers(report, manifest, "", rows, "")
        (check,) = [item for item in report.checks if item.check_id == "ACQ-1"]
        return check

    INFERENCE = {"path": PATH, "polarity": "Negative", "polarity_source": "file_name", "token": "neg",
                 "rule": "unreadable_header_polarity_from_file_name_2026_10_29"}

    def test_a_recorded_inference_is_a_warning_that_names_it(self) -> None:
        check = self.acq1(*self.unit(dict(self.INFERENCE)))

        self.assertEqual(gate.WARN, check.status, check.detail)
        self.assertIn("its file name's token 'neg'", " ".join(check.evidence["warnings"]))

    def test_no_inference_and_a_wrong_one_fail(self) -> None:
        for inference, words in (
            (None, "no file-name polarity inference is recorded"),
            ({**self.INFERENCE, "polarity": "Positive"}, "its name states Negative, not the recorded Positive"),
            ({**self.INFERENCE, "rule": "something_else"}, "recorded under 'something_else'"),
            ({**self.INFERENCE, "polarity_source": "declaration"}, "recorded from another source"),
        ):
            with self.subTest(words=words):
                check = self.acq1(*self.unit(inference))
                self.assertEqual(gate.FAIL, check.status)
                self.assertIn(words, check.detail)
        check = self.acq1(*self.unit(dict(self.INFERENCE), run="Positive"))
        self.assertEqual(gate.FAIL, check.status)
        self.assertIn("the unit runs as Positive", check.detail)

    def test_outside_a_campaign_nothing_is_judged(self) -> None:
        # Review of #86, M2: an accession-level run, no Catalog handoff and no binding disposition.
        manifest, rows = self.unit(None)
        manifest.pop("campaign_disposition")
        manifest["project"].pop("repository_metadata")
        check = self.acq1(manifest, rows)

        self.assertNotEqual(gate.FAIL, check.status, check.detail)

    def test_a_folder_that_states_the_other_polarity_fails_the_inference(self) -> None:
        manifest, rows = self.unit(dict(self.INFERENCE))
        manifest["input_directory"] = r"D:\w"
        check = self.acq1(manifest, rows)
        self.assertNotEqual(gate.FAIL, check.status, "raw/data names no polarity")

        path = r"D:\w\POS\241202_Tomita_18_neg.wiff2"
        manifest, rows = self.unit({**self.INFERENCE, "path": path})
        manifest["input_directory"] = r"D:\w"
        manifest["raw_metadata_preflight"]["summary"]["per_file"][0]["file"] = path
        manifest["input_candidates"] = [path]
        rows[0]["file_path"] = path
        check = self.acq1(manifest, rows)
        self.assertEqual(gate.FAIL, check.status)
        self.assertIn("its name states no single polarity", check.detail)

    def test_a_single_declared_polarity_needs_no_inference(self) -> None:
        check = self.acq1(*self.unit(None, declared="Negative"))

        self.assertNotEqual(gate.FAIL, check.status, check.detail)


class ELIG1AcceptsOnlyARecordedUntargetedInference(unittest.TestCase):
    """MPST000048 leaves untargeted unset, and no header can show it: Interactive 0.5.53 infers it from the declared
    DDA where the unit runs on its declaration alone (user decision, 2026-10-29); ELIG-1 holds that record."""

    INFERENCE = {"basis": "declared_acquisition", "declared_acquisition_mode": "DDA",
                 "rule": "untargeted_inferred_from_declared_acquisition"}

    def elig1(self, warnings: list[str], inference: dict | None = None, untargeted: object = None,
              settings_untargeted: bool = True):
        settings = {"ion_mode": "Both", "acquisition_mode": "DDA", **({"untargeted": untargeted} if settings_untargeted else {})}
        manifest = {
            "execution_allowed": True, "status": "preflight_passed",
            "project": {"untargeted": True, "repository_metadata": {"catalog_handoff": {"technical_settings": settings}}},
            "campaign_disposition": {"schema": "msdial-campaign-disposition.v1", "disposition": "run", "reasons": [],
                                     "warnings": warnings, "excluded_inputs": [], "split_key": None, "applied": True,
                                     **({"untargeted_inference": inference} if inference else {})},
        }
        report = gate.Report(Path(tempfile.gettempdir()))
        gate.check_execution_allowed(report, manifest, "")
        (check,) = report.checks
        return check

    def test_the_recorded_inferences_pass(self) -> None:
        self.assertEqual(gate.PASS, self.elig1(["acquisition_declared_only", "untargeted_inferred_from_declared_acquisition"],
                                               dict(self.INFERENCE)).status)
        self.assertEqual(gate.PASS, self.elig1(["untargeted_inferred_from_headers"]).status)
        self.assertEqual(gate.PASS, self.elig1([], untargeted=True).status, "declared untargeted")
        self.assertEqual(gate.PASS, self.elig1([], settings_untargeted=False).status, "a handoff that records none")

    def test_anything_else_fails(self) -> None:
        for warnings, inference, words in (
            ([], None, "no campaign disposition records the inference"),
            (["untargeted_inferred_from_declared_acquisition"], dict(self.INFERENCE), "without a declared-only run"),
            (["acquisition_declared_only", "untargeted_inferred_from_declared_acquisition"],
             {**self.INFERENCE, "declared_acquisition_mode": "MRM"}, "without a declared-only run"),
        ):
            with self.subTest(words=words, warnings=warnings):
                check = self.elig1(warnings, inference)
                self.assertEqual(gate.FAIL, check.status)
                self.assertIn(words, check.detail)


class TheEncodingRuleAcceptsAWiffWithoutItsScanAsUnreadable(unittest.TestCase):
    """Review of #86, L3: Interactive 0.5.53's lease passes over a .wiff without its .wiff.scan for its sample's next
    encoding, unused as vendor_companion_missing; the gate's recomputation of the rule takes that reason for a vendor
    file only."""

    def problems(self, used: str, unused: list[tuple[str, str]]) -> list[str]:
        choice = {"rule": "one_encoding_per_sample_2026_10_09", "used": used,
                  "unused": [{"path": path, "reason": reason} for path, reason in unused]}
        return gate._encoding_rule_problems(gate._read_encoding_choice(choice, "test"), True)

    def test_the_reason_holds_for_a_vendor_file_only(self) -> None:
        self.assertEqual([], self.problems("S1.mzML", [("S1.wiff", "vendor_companion_missing")]))
        self.assertTrue(self.problems("S1.wiff", [("S1.mzML", "vendor_companion_missing")]))


if __name__ == "__main__":
    unittest.main()
