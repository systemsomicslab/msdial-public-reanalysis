"""Each before-production check's run policy: what its FAIL does to a campaign unit's MS-DIAL run.

The user's rule of 2026-10-01: only a check whose failure breaks the MS-DIAL results stops a campaign
unit's run, and the unit then counts as failed; any other FAIL is recorded and the unit runs. The user
named ELIG-1, ACQ-1, SUM-1, CNT-1 and INP-1 as the first kind and CLS-1, CLS-2, CLS-3, ORD-1 and PKH-1
as the second, and on 2026-10-02 placed the rest: ID-1, PRE-2 and CONV-1 with the first, SPL-1 and
PRE-1 (which never FAILs) with the second. A first-kind check left not evaluable on an artifact its
stage owed stops the run as its FAIL does (2026-10-02); one the stage owed nothing never does. The gate
states the class of every before-production check in --json (run_policy), and none for a check after
production, and the checks that stop the run (run_blocked_by); the campaign runner reads them. The
fixtures are test_verify_lineage_built_csv's, a unit as Interactive's folder branch prepares it.
"""

from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import test_verify_lineage_built_csv as built  # noqa: E402  (loads the gate as verify_run_invariants_built_csv)

verifier = built.verifier
FolderBranchUnit = built.FolderBranchUnit
_check = built._check
_MODULE_PATH = built._MODULE_PATH


class RunPolicyTests(unittest.TestCase):
    """Each before-production check says whether its FAIL stops a campaign unit's run (2026-10-01, 2026-10-02)."""

    USER_BLOCKS = {"ELIG-1", "ACQ-1", "SUM-1", "CNT-1", "INP-1", "ID-1", "PRE-2", "CONV-1"}
    # PAIR-1 (2026-10-06) lists every name pairing the lease inferred, which the user decided must always be
    # on record; it never FAILs and is recorded like PRE-1.
    USER_RECORDS = {"CLS-1", "CLS-2", "CLS-3", "ORD-1", "PKH-1", "SPL-1", "PRE-1", "PAIR-1"}

    def test_every_before_production_check_carries_its_class(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = verifier.verify(FolderBranchUnit(temporary).write(), "before-production")

        produced = {check.check_id for check in report.checks}
        self.assertEqual(set(verifier.RUN_POLICY), produced, "every check, and only these, is classed")
        for check in report.checks:
            self.assertIn(check.as_dict()["run_policy"], (verifier.BLOCKS_RUN, verifier.RECORD_ONLY), check.check_id)

    def test_a_fail_stops_the_run_only_for_the_checks_the_user_named(self) -> None:
        """'Only for checks that break results: ELIG-1, ACQ-1, SUM-1, CNT-1 and INP-1', and on 2026-10-02 ID-1,
        PRE-2 and CONV-1 with them. Every other before-production check is recorded, as the user placed it."""
        blocking = {check_id for check_id, policy in verifier.RUN_POLICY.items() if policy == verifier.BLOCKS_RUN}
        self.assertEqual(self.USER_BLOCKS, blocking)
        self.assertEqual(set(verifier.RUN_POLICY), self.USER_BLOCKS | self.USER_RECORDS, "the user placed every check")
        for check_id in self.USER_RECORDS:
            self.assertEqual(verifier.RECORD_ONLY, verifier.RUN_POLICY[check_id], check_id)

    def test_the_runner_holds_the_same_blocking_checks_as_the_gate(self) -> None:
        """The campaign runner blocks on its own list whatever a report states, so the two lists are one."""
        sys.path.insert(0, str(TESTS.parent / "scripts"))
        from campaign import policy as campaign_policy

        blocking = {check_id for check_id, policy in verifier.RUN_POLICY.items() if policy == verifier.BLOCKS_RUN}
        self.assertEqual(blocking, set(campaign_policy.BLOCKS_RUN_CHECKS))
        self.assertEqual(len(campaign_policy.BLOCKS_RUN_CHECKS), len(set(campaign_policy.BLOCKS_RUN_CHECKS)))

    def test_the_checks_the_user_placed_on_2026_10_02_stop_the_run_on_their_fail(self) -> None:
        """ID-1 (the manifest names another unit), PRE-2 (a stale extractor read the headers) and CONV-1 (an
        input MS-DIAL cannot open), each FAILed in a unit that is otherwise ready."""
        def stale_extractor(unit: FolderBranchUnit) -> None:
            unit.manifest["raw_metadata_preflight"] = {
                "extractor": {"sha256": "a" * 64, "provenance_status": verifier.EXTRACTOR_STALE,
                              "msrawdataworkbench_commit": "b" * 40, "msdialworkbench_commit": "c" * 40}}

        cases = {
            "ID-1": lambda unit: unit.manifest["project"].update(analysis_unit_id="another-unit"),
            "PRE-2": stale_extractor,
            "CONV-1": lambda unit: unit.file("S2.mzXML", "S2", "A"),
        }
        for check_id, spoil in cases.items():
            with self.subTest(check_id), tempfile.TemporaryDirectory() as temporary:
                unit = FolderBranchUnit(temporary)
                unit.file("S1.mzML", "S1", "A")
                spoil(unit)
                unit.prepare()
                report = unit.gate()
                self.assertEqual(verifier.FAIL, _check(report, check_id).status, _check(report, check_id).detail)
                self.assertEqual(verifier.BLOCKS_RUN, _check(report, check_id).as_dict()["run_policy"])
                self.assertIn(check_id, report.run_blocked_by)
                self.assertIn(check_id, report.as_dict()["run_blocked_by"])

    def test_spl1_and_pre1_are_recorded(self) -> None:
        """SPL-1's FAIL leaves the part's own results as they are, and PRE-1 reports no FAIL to place."""
        self.assertEqual(verifier.RECORD_ONLY, verifier.RUN_POLICY["SPL-1"])
        self.assertEqual(verifier.RECORD_ONLY, verifier.RUN_POLICY["PRE-1"])
        source = _function_source(_MODULE_PATH.read_text(encoding="utf-8"), "check_preflight_claim")
        self.assertNotIn("FAIL", source.replace("never FAILs", ""))

    def test_every_classed_check_says_why_in_its_description(self) -> None:
        source = _MODULE_PATH.read_text(encoding="utf-8")
        checks = [name for name, value in vars(verifier).items() if name.startswith("check_") and callable(value)]
        for check_id, policy in verifier.RUN_POLICY.items():
            with self.subTest(check_id):
                adding = [name for name in checks
                          if re.search(rf'report\.add\(\s*"{re.escape(check_id)}"', _function_source(source, name))]
                self.assertEqual(1, len(adding), adding)
                self.assertIn(f"RUN POLICY: {policy}", getattr(verifier, adding[0]).__doc__ or "")

    def test_later_stages_state_no_run_policy(self) -> None:
        """A check with no run left to stop states nothing: the campaign runner takes a stated policy it does
        not know, null included, as one that stops the run."""
        with tempfile.TemporaryDirectory() as temporary:
            report = verifier.verify(FolderBranchUnit(temporary).write(), "all")

        later = [check for check in report.checks if check.stage != "before-production"]
        self.assertTrue(later)
        self.assertEqual({None}, {check.run_policy for check in later})
        for check in later:
            self.assertNotIn("run_policy", check.as_dict(), check.check_id)
        self.assertIn("CNT-1", {check.check_id for check in later}, "CNT-1 runs again after the run")

    def test_a_report_of_every_stage_states_only_the_two_rules_once_per_check(self) -> None:
        """What a reader keying the stated policies by check_id gets from --stage all --json: each
        before-production check's class, and no other statement, so the after-run CNT-1 FAIL states nothing
        over the before-production CNT-1, and no after-run FAIL reads as stopping a run."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1.mzML", "S1", "A")
            unit.file("S2.mzML", "S2", "A")
            unit.prepare()
            workspace = unit.write()
            (unit.output / "S1.mdpeak").write_text("Peak ID\n", encoding="ascii")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                verifier.main([str(workspace), "--stage", "all", "--strict", "--json"])
            payload = json.loads(buffer.getvalue())

        stated: dict[str, list] = {}
        for item in payload["checks"]:
            if "run_policy" in item:
                stated.setdefault(item["check_id"], []).append(item["run_policy"])
        self.assertEqual(dict(verifier.RUN_POLICY), {check: rules[0] for check, rules in stated.items()})
        self.assertEqual({1}, {len(rules) for rules in stated.values()})
        failed_later = {item["check_id"] for item in payload["checks"]
                        if item["status"] == verifier.FAIL and item["stage"] != "before-production"}
        self.assertIn("CNT-1", failed_later)
        self.assertEqual([], payload["run_blocked_by"])

    def test_a_blocking_check_left_unevaluated_where_it_was_owed_stops_the_run(self) -> None:
        """2026-10-02: a blocks_run check this stage owed and could not evaluate stops the run as its FAIL
        does. It is a strict failure, which strict_failures names, and run_blocked_by names it too."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1.mzML", "S1", "A")
            unit.prepare()
            del unit.manifest["allowlist_checksum_validation"]
            workspace = unit.write()
            report = verifier.verify(workspace, "before-production")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = verifier.main([str(workspace), "--stage", "before-production", "--strict"])
            text = buffer.getvalue()

        self.assertEqual(verifier.NOT_EVALUABLE, _check(report, "SUM-1").status)
        self.assertIn("SUM-1", [check.check_id for check in report.strict_failures])
        self.assertEqual(["SUM-1"], report.run_blocked_by)
        self.assertEqual(["SUM-1"], report.as_dict()["run_blocked_by"])
        self.assertNotEqual(0, code)
        self.assertIn("CHECKS THAT STOP A CAMPAIGN RUN (run_policy blocks_run, FAILed or unevaluable where owed): SUM-1",
                      text)

    def test_a_check_left_unevaluated_where_nothing_was_owed_stops_no_run(self) -> None:
        """INP-1 for a unit that declares no analysis inputs is normal: not required, and no stop."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1.mzML", "S1", "A")
            unit.prepare()
            for key in ("analysis_inputs", "analysis_input_count", "analysis_inputs_declared"):
                unit.manifest["project"].pop(key, None)
            report = unit.gate()

        inp = _check(report, "INP-1")
        self.assertEqual((verifier.NOT_EVALUABLE, False), (inp.status, inp.required), inp.detail)
        self.assertNotIn("INP-1", [check.check_id for check in report.strict_failures])
        self.assertEqual([], report.run_blocked_by)

    def test_only_a_blocking_fail_stops_the_run(self) -> None:
        """ELIG-1 and CLS-3 both FAIL: the run is stopped by ELIG-1 alone."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1.mzML", "S1", "A")
            unit.prepare()
            unit.manifest["execution_allowed"] = False
            unit.manifest["project"]["class_proposal"]["status"] = "proposed"
            workspace = unit.write()
            report = verifier.verify(workspace, "before-production")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = verifier.main([str(workspace), "--stage", "before-production", "--strict", "--json"])
            payload = json.loads(buffer.getvalue())
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                verifier.main([str(workspace), "--stage", "before-production"])
            text = buffer.getvalue()

        self.assertEqual(verifier.FAIL, _check(report, "CLS-3").status)
        self.assertEqual(["ELIG-1"], report.run_blocked_by)
        self.assertEqual(2, code)
        self.assertEqual(["ELIG-1"], payload["run_blocked_by"])
        policies = {item["check_id"]: item["run_policy"] for item in payload["checks"]}
        self.assertEqual(verifier.BLOCKS_RUN, policies["ELIG-1"])
        self.assertEqual(verifier.RECORD_ONLY, policies["CLS-3"])
        self.assertIn("CHECKS THAT STOP A CAMPAIGN RUN (run_policy blocks_run, FAILed or unevaluable where owed): ELIG-1",
                      text)

    def test_a_record_only_fail_stops_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1.mzML", "S1", "A")
            unit.prepare()
            unit.manifest["project"]["class_proposal"]["status"] = "proposed"
            report = unit.gate()

        self.assertFalse(report.ok)
        self.assertEqual([], report.run_blocked_by)
        self.assertEqual([], report.as_dict()["run_blocked_by"])

    def test_a_fail_after_the_run_stops_no_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1.mzML", "S1", "A")
            unit.file("S2.mzML", "S2", "A")
            unit.prepare()
            (unit.output / "S1.mdpeak").write_text("Peak ID\n", encoding="ascii")
            report = unit.gate("after-run")

        count = next(check for check in report.checks if check.check_id == "CNT-1")
        self.assertEqual(verifier.FAIL, count.status)
        self.assertIsNone(count.run_policy)
        self.assertEqual([], report.run_blocked_by)


def _function_source(source: str, name: str) -> str:
    start = source.index(f"\ndef {name}(")
    following = source.find("\ndef ", start + 1)
    return source[start:following if following != -1 else len(source)]


if __name__ == "__main__":
    unittest.main()
