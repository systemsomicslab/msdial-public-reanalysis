"""Each before-production check's run policy: what its FAIL does to a campaign unit's MS-DIAL run.

The user's rule of 2026-10-01: only a check whose failure breaks the MS-DIAL results stops a campaign
unit's run, and the unit then counts as failed; any other FAIL is recorded and the unit runs. The user
named ELIG-1, ACQ-1, SUM-1, CNT-1 and INP-1 as the first kind and CLS-1, CLS-2, CLS-3, ORD-1 and PKH-1
as the second, and a FAIL stops the run "only for" the first, so the checks the rule does not name are
recorded too, as the campaign contract reads it. The gate states the class of every before-production
check in --json (run_policy), and none for a check after production, and the FAILs that stop the run
(run_blocked_by); the campaign runner reads them. The fixtures are test_verify_lineage_built_csv's, a
unit as Interactive's folder branch prepares it.
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
    """Each before-production check says whether its FAIL stops a campaign unit's run (2026-10-01)."""

    USER_BLOCKS = {"ELIG-1", "ACQ-1", "SUM-1", "CNT-1", "INP-1"}
    USER_RECORDS = {"CLS-1", "CLS-2", "CLS-3", "ORD-1", "PKH-1"}

    def test_every_before_production_check_carries_its_class(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = verifier.verify(FolderBranchUnit(temporary).write(), "before-production")

        produced = {check.check_id for check in report.checks}
        self.assertEqual(set(verifier.RUN_POLICY), produced, "every check, and only these, is classed")
        for check in report.checks:
            self.assertIn(check.as_dict()["run_policy"], (verifier.BLOCKS_RUN, verifier.RECORD_ONLY), check.check_id)

    def test_a_fail_stops_the_run_only_for_the_checks_the_user_named(self) -> None:
        """'Only for checks that break results: ELIG-1, ACQ-1, SUM-1, CNT-1 and INP-1.' The others the rule
        names are recorded, and so is every check it does not name (the campaign contract's reading)."""
        blocking = {check_id for check_id, policy in verifier.RUN_POLICY.items() if policy == verifier.BLOCKS_RUN}
        self.assertEqual(self.USER_BLOCKS, blocking)
        for check_id in self.USER_RECORDS | {"ID-1", "SPL-1", "PRE-1", "PRE-2", "CONV-1"}:
            self.assertEqual(verifier.RECORD_ONLY, verifier.RUN_POLICY[check_id], check_id)

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

    def test_a_blocking_check_left_not_evaluable_stops_no_run(self) -> None:
        """Only a FAIL stops a run; a blocks_run check this stage owed and could not evaluate is a strict
        failure, which strict_failures names."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1.mzML", "S1", "A")
            unit.prepare()
            del unit.manifest["allowlist_checksum_validation"]
            report = unit.gate()

        self.assertEqual(verifier.NOT_EVALUABLE, _check(report, "SUM-1").status)
        self.assertEqual([], report.run_blocked_by)
        self.assertIn("SUM-1", [check.check_id for check in report.strict_failures])

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
        self.assertIn("FAILS THAT STOP A CAMPAIGN RUN (run_policy blocks_run): ELIG-1", text)

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
