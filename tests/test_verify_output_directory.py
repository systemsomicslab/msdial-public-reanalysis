"""Where the gate reads a run's outputs: the manifest's output_directory, and a unit's superseded runs.

Interactive 0.5.29 (PR #62) prepares a new production run of a finished unit in a new folder,
<workspace>\\output-run-<n>, which the manifest's output_directory then names, and moves the finished run's
records - its output_directory, status, finalisation and mzTab-M validation among them - into an entry of
superseded_runs. The old folder and its files are left as they were. Review r6-62 found the gate still read
<workspace>\\output by a fixed path, so after a new run it judged the finished run's folder against the new
run's records.

These tests pin that the gate reads the folder output_directory names, kept inside the workspace (one
outside it makes the workspace unusable, exit 3), falls back to <workspace>\\output for a manifest that names
none, and that RET-1's deletion justification rests on the latest validated run.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import test_verify_extractor_and_release as release  # noqa: E402  (loads the gate)
import test_verify_readings as readings  # noqa: E402

verifier = release.verifier
_unit, _outputs, _mztab, _crossing, _write = release._unit, release._outputs, release._mztab, release._crossing, release._write
VALIDATED = release.VALIDATED
_ret1 = release._ret1


def _prepared(output: Path) -> None:
    """What a production prepare leaves in a run's folder before the Console runs (B5)."""
    output.mkdir(parents=True, exist_ok=True)
    (output / "analysis_files.csv").write_text("file_path,file_name,type,class,injection_order,batch\n",
                                               encoding="utf-8")
    (output / "method.txt").write_text("#Data type\n", encoding="utf-8")
    _write(output / "run-manifest.json", {"expected_analysis_exports": []})


def _superseded(workspace: Path, folder: str = "output", **record) -> dict:
    """A finished run's records as start_new_production_run copies them into superseded_runs."""
    entry = {"status": "mztab_validated", "execution_allowed": True, "cleanup_allowed": True,
             "output_directory": str(workspace / folder), "superseded_at": "2026-10-07T00:00:00+00:00",
             "superseded_by": "msdial_prepare_repository_reanalysis", **VALIDATED}
    entry.update(record)
    return entry


def _main(*arguments: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = verifier.main(list(arguments))
    return code, err.getvalue()


class OutputFolderTests(unittest.TestCase):
    def test_a_manifest_without_output_directory_reads_workspace_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary))
            _prepared(workspace / "output")
            report = verifier.verify(workspace, "all")

        self.assertTrue(report.progress["stages"]["B5"])

    def test_the_folder_output_directory_names_is_read_and_not_workspace_output(self) -> None:
        """A new run in output-run-2: the finished run's prepared folder in output no longer counts."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            new = _unit(root, "new", output_directory=str(root / "new" / "output-run-2"),
                        superseded_runs=[_superseded(root / "new")])
            _prepared(new / "output")
            (new / "output-run-2").mkdir()
            before = verifier.verify(new, "all")
            _prepared(new / "output-run-2")
            after = verifier.verify(new, "all")

        self.assertFalse(before.progress["stages"]["B5"], "output, the finished run's folder, is not read")
        self.assertTrue(after.progress["stages"]["B5"])

    def test_a_relative_output_directory_is_read_against_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary), output_directory="output-run-2")
            _prepared(workspace / "output-run-2")
            report = verifier.verify(workspace, "all")

        self.assertTrue(report.progress["stages"]["B5"])

    def test_a_moved_workspace_reads_the_folder_at_its_place_in_the_recorded_workspace(self) -> None:
        """A copied workspace's manifest still names the original location; the folder is read in the copy."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            elsewhere = root / "moved-away" / "unit"
            workspace = _unit(root, workspace=str(elsewhere), output_directory=str(elsewhere / "output-run-2"))
            _prepared(workspace / "output-run-2")
            report = verifier.verify(workspace, "all")

        self.assertTrue(report.progress["stages"]["B5"])

    def test_a_folder_outside_the_workspace_makes_it_unusable(self) -> None:
        cases = {
            "another unit's output": lambda root: str(root / "other" / "output"),
            "a parent-relative path": lambda root: str(Path("..") / "other" / "output"),
            "the workspace itself": lambda root: str(root / "unit"),
        }
        for name, declared in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                workspace = _unit(root, output_directory=declared(root))
                _prepared(root / "other" / "output")
                with self.assertRaises(verifier.OutputOutsideWorkspace):
                    verifier.verify(workspace, "all")
                code, err = _main(str(workspace), "--json")

            self.assertEqual(3, code)
            self.assertIn("output_directory", err)

    def test_a_superseded_run_outside_the_workspace_makes_it_unusable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = _unit(root, output_directory=str(root / "unit" / "output-run-2"),
                              superseded_runs=[_superseded(root / "other")])
            with self.assertRaises(verifier.OutputOutsideWorkspace):
                verifier.verify(workspace, "before-publish")
            code, _err = _main(str(workspace), "--stage", "before-publish")

        self.assertEqual(3, code)

    def test_record_reading_refuses_the_workspace_as_unusable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = _unit(root, output_directory=str(root / "other" / "output"))
            code, _out, err = readings._record(workspace, "--check", "QA-1")

        self.assertEqual(3, code)
        self.assertIn("output_directory", err)


class DeletionAfterANewRunTests(unittest.TestCase):
    def test_a_current_run_validated_in_output_run_2_justifies_the_cleanup(self) -> None:
        """The finished run's old folder lacks an mzTab-M; the current run's folder has every output."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = _unit(root, status="raw_cleaned", raw_cleaned_at="2026-10-07T02:00:00+00:00",
                              campaign_authorizations=[_crossing(5)],
                              output_directory=str(root / "unit" / "output-run-2"),
                              superseded_runs=[_superseded(root / "unit", status="validation_failed",
                                                           mztab_validation={"summary": {"failed": 1}})],
                              **VALIDATED)
            (workspace / "output-run-2").mkdir()
            _outputs(workspace / "output-run-2")
            shutil.rmtree(workspace / "raw")
            check = _ret1(workspace)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual("validated", check.evidence["justification"])
        self.assertIn("output-run-2", check.detail)

    def test_validated_outputs_in_the_old_folder_do_not_stand_for_the_current_run(self) -> None:
        """The current run's records say validated, its folder has no mzTab-M; output has one from the old run,
        whose own record failed validation. Read at the fixed path, the old folder justified this cleanup."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = _unit(root, status="raw_cleaned", raw_cleaned_at="2026-10-07T02:00:00+00:00",
                              campaign_authorizations=[_crossing(5)], outputs=True,
                              output_directory=str(root / "unit" / "output-run-2"),
                              superseded_runs=[_superseded(root / "unit", status="validation_failed",
                                                           mztab_validation={"summary": {"failed": 1}})],
                              **VALIDATED)
            (workspace / "output-run-2").mkdir()
            shutil.rmtree(workspace / "raw")
            check = _ret1(workspace)

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("no mzTab-M is in its output", check.evidence["unvalidated_because"])

    def test_a_superseded_validated_run_justifies_a_deletion_after_the_new_run_failed(self) -> None:
        """The new run in output-run-2 failed and was not retried; the finished run in output validated."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = _unit(root, status="discarded", discarded_at="2026-10-07T04:00:00+00:00",
                              campaign_authorizations=[_crossing(5, entry_point="campaign_runner.discard")],
                              outputs=True, output_directory=str(root / "unit" / "output-run-2"),
                              superseded_runs=[_superseded(root / "unit")])
            (workspace / "output-run-2").mkdir()
            shutil.rmtree(workspace / "raw")
            check = _ret1(workspace)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual("validated", check.evidence["justification"])
        self.assertIn("superseded_runs[0]", check.detail)
        self.assertIn("output-run-2", check.detail)

    def test_the_latest_validated_run_is_the_one_named(self) -> None:
        """Two finished runs in output and output-run-2, the current one in output-run-3."""
        for broken, expected in ((False, "superseded_runs[1]"), (True, "superseded_runs[0]")):
            with self.subTest(latest_incomplete=broken), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                workspace = _unit(root, status="discarded", discarded_at="2026-10-07T04:00:00+00:00",
                                  campaign_authorizations=[_crossing(5, entry_point="campaign_runner.discard")],
                                  outputs=True, output_directory=str(root / "unit" / "output-run-3"),
                                  superseded_runs=[_superseded(root / "unit"),
                                                   _superseded(root / "unit", "output-run-2")])
                (workspace / "output-run-2").mkdir()
                # One planned export the Console did not write leaves output-run-2 incomplete.
                _outputs(workspace / "output-run-2", planned=("s0.mdpeak", "s1.mdpeak"),
                         written=("s0.mdpeak",) if broken else None)
                (workspace / "output-run-3").mkdir()
                shutil.rmtree(workspace / "raw")
                check = _ret1(workspace)

            self.assertEqual(verifier.PASS, check.status, check.detail)
            self.assertIn(expected, check.detail)

    def test_a_superseded_run_whose_records_did_not_validate_justifies_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = _unit(root, status="discarded", discarded_at="2026-10-07T04:00:00+00:00",
                              campaign_authorizations=[_crossing(5, entry_point="campaign_runner.discard")],
                              outputs=True, output_directory=str(root / "unit" / "output-run-2"),
                              superseded_runs=[_superseded(root / "unit", status="validation_failed",
                                                           mztab_validation={"summary": {"failed": 1}})])
            (workspace / "output-run-2").mkdir()
            shutil.rmtree(workspace / "raw")
            check = _ret1(workspace)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual("", check.evidence["justification"])

    def test_raw_still_present_is_judged_by_the_current_runs_folder(self) -> None:
        """Validated records and every output in output-run-2: the deletion is due, as for output."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = _unit(root, status="mztab_validated", output_directory=str(root / "unit" / "output-run-2"),
                              superseded_runs=[_superseded(root / "unit")], **VALIDATED)
            (workspace / "output-run-2").mkdir()
            _outputs(workspace / "output-run-2", planned=("s0.mdpeak", "s1.mdpeak"), written=("s0.mdpeak",))
            _outputs(workspace / "output")
            check = _ret1(workspace)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("1 of the 2 exports its run planned are absent", check.detail)


if __name__ == "__main__":
    unittest.main()
