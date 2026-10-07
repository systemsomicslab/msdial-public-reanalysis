"""Where the gate reads a run's outputs: the manifest's output_directory, and a unit's superseded runs.

Interactive 0.5.29 (PR #62) prepares a new production run of a finished unit in a new folder,
<workspace>\\output-run-<n>, which the manifest's output_directory then names, and moves the finished run's
records - its output_directory, status, finalisation and mzTab-M validation among them - into an entry of
superseded_runs. The old folder and its files are left as they were. Review r6-62 found the gate still read
<workspace>\\output by a fixed path, so after a new run it judged the finished run's folder against the new
run's records.

These tests pin that the gate reads the folder output_directory names, kept inside the workspace (one
outside it makes the workspace unusable, exit 3), falls back to <workspace>\\output for a manifest that names
none, and that RET-1 judges a raw deletion by the current run only. Interactive PR #62 at 52b470b holds the
raw data while a new run prepared after a validated run has not validated (prepared, running or failed): cleanup
and discard both refuse, approved or not. Review r7-31 found RET-1 passing such a deletion on the superseded
run's validated outputs; RET-1 now refuses it, naming the superseded run the raw data were held for.
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
_edit = release._edit


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

    def _discarded_after_a_new_run(self, root: Path, superseded: list, current: str = "output-run-2",
                                   **manifest) -> Path:
        """A unit discarded under the campaign runner's approval with its current run in `current`."""
        record = {"status": "discarded", "discarded_at": "2026-10-07T04:00:00+00:00",
                  "campaign_authorizations": [_crossing(5, entry_point="campaign_runner.discard")],
                  "output_directory": str(root / "unit" / current), "superseded_runs": superseded}
        record.update(manifest)
        workspace = _unit(root, outputs=True, **record)
        (workspace / current).mkdir(exist_ok=True)
        shutil.rmtree(workspace / "raw")
        return workspace

    def test_a_deletion_while_the_new_run_has_not_validated_is_refused(self) -> None:
        """The new run in output-run-2 has not validated; the finished run in output did. Review r7-31: the gate
        passed this discard on the superseded run's outputs, which Interactive 52b470b refuses to discard."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = self._discarded_after_a_new_run(root, [_superseded(root / "unit")])
            check = _ret1(workspace)

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertEqual(verifier.HELD_FOR_NEW_RUN, check.evidence["justification"])
        self.assertEqual("its superseded run 0 (superseded_runs[0])", check.evidence["held_superseded_run"])
        self.assertEqual("output", check.evidence["held_superseded_output"])
        self.assertIn("superseded_runs[0]", check.detail)
        self.assertIn("output-run-2", check.detail)
        self.assertIn("held", check.detail)

    def test_the_held_run_named_is_the_latest_superseded_run_that_validated(self) -> None:
        """Two finished runs in output and output-run-2, the current one in output-run-3."""
        for broken, expected in ((False, "superseded_runs[1]"), (True, "superseded_runs[0]")):
            with self.subTest(latest_unvalidated=broken), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                # The latest finished run: validated, or failed validation in its records and incomplete on disk.
                latest = _superseded(root / "unit", "output-run-2", **(
                    {"status": "validation_failed", "cleanup_allowed": False} if broken else {}))
                workspace = self._discarded_after_a_new_run(root, [_superseded(root / "unit"), latest],
                                                            current="output-run-3")
                (workspace / "output-run-2").mkdir()
                _outputs(workspace / "output-run-2", planned=("s0.mdpeak", "s1.mdpeak"),
                         written=("s0.mdpeak",) if broken else None)
                check = _ret1(workspace)

            self.assertEqual(verifier.FAIL, check.status, check.detail)
            self.assertIn(expected, check.detail)

    def test_the_hold_comes_before_a_failure_or_a_disposition(self) -> None:
        """A new run that failed and a campaign skip each justify a deletion without a validated superseded run;
        while one validated, neither does."""
        failures = [{"reason": "the Console exited 1", "recorded_at": "2026-10-07T03:00:00+00:00"}]
        cases = {
            "a recorded run failure": {"run_failures": failures},
            "a campaign skip": {"campaign_disposition": {"applied": True, "disposition": "skip",
                                                         "reasons": ["test"]}},
        }
        for name, extra in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                workspace = self._discarded_after_a_new_run(root, [_superseded(root / "unit")], **extra)
                held = _ret1(workspace)
                # The same unit with no superseded run: the failure or the skip justifies the deletion.
                _edit(workspace / "provenance" / "run-manifest.json", superseded_runs=[])
                plain = _ret1(workspace)

            self.assertEqual(verifier.FAIL, held.status, held.detail)
            self.assertEqual(verifier.HELD_FOR_NEW_RUN, held.evidence["justification"])
            self.assertEqual(verifier.PASS, plain.status, plain.detail)

    def test_approved_or_not_the_deletion_is_refused(self) -> None:
        """No campaign crossing: a confirmed=true discard is refused the same way."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = self._discarded_after_a_new_run(root, [_superseded(root / "unit")],
                                                        campaign_authorizations=[])
            check = _ret1(workspace)

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("confirmed=true", check.evidence["authority"])
        self.assertEqual(verifier.HELD_FOR_NEW_RUN, check.evidence["justification"])

    def test_a_superseded_run_validated_by_its_records_alone_holds_the_raw_data(self) -> None:
        """Interactive holds on the superseded run's records (a cleanup-ready status or cleanup_allowed), even
        where its folder no longer shows every output."""
        for name, record in (("cleanup-ready status", {"cleanup_allowed": False}),
                             ("cleanup_allowed", {"status": "raw_cleaned"})):
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                workspace = self._discarded_after_a_new_run(root, [_superseded(root / "unit", **record)])
                (workspace / "output" / "s0.mdpeak").unlink()
                check = _ret1(workspace)

            self.assertEqual(verifier.FAIL, check.status, check.detail)
            self.assertIn("by its records", check.detail)

    def test_a_cleanup_whose_current_run_is_incomplete_is_refused_naming_the_held_run(self) -> None:
        """The current run's records validated but an export is absent, so it has not validated; an older run did."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = _unit(root, status="raw_cleaned", raw_cleaned_at="2026-10-07T05:00:00+00:00",
                              campaign_authorizations=[_crossing(5)], outputs=True,
                              output_directory=str(root / "unit" / "output-run-2"),
                              superseded_runs=[_superseded(root / "unit")], **VALIDATED)
            (workspace / "output-run-2").mkdir()
            _outputs(workspace / "output-run-2", planned=("s0.mdpeak", "s1.mdpeak"), written=("s0.mdpeak",))
            shutil.rmtree(workspace / "raw")
            check = _ret1(workspace)

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertEqual(verifier.HELD_FOR_NEW_RUN, check.evidence["justification"])
        self.assertIn("1 of the 2 exports its run planned are absent", check.evidence["unvalidated_because"])
        self.assertIn("superseded_runs[0]", check.detail)

    def test_a_cleanup_after_the_new_run_validated_passes_on_that_run(self) -> None:
        """Once the new run validates, the cleanup is judged by it like any other; no superseded run is named."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = _unit(root, status="raw_cleaned", raw_cleaned_at="2026-10-07T05:00:00+00:00",
                              campaign_authorizations=[_crossing(5)], outputs=True,
                              output_directory=str(root / "unit" / "output-run-2"),
                              superseded_runs=[_superseded(root / "unit")], **VALIDATED)
            (workspace / "output-run-2").mkdir()
            _outputs(workspace / "output-run-2")
            shutil.rmtree(workspace / "raw")
            check = _ret1(workspace)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual("validated", check.evidence["justification"])
        self.assertNotIn("superseded", check.detail)

    def test_a_split_part_released_while_its_new_run_has_not_validated_is_refused(self) -> None:
        """Split part or not: the part's raw tree went in its parent's release while its new run had not validated."""
        with tempfile.TemporaryDirectory() as temporary:
            fixture = release.ReleasedSplitParentTests()._released(temporary)
            part = fixture.part_roots[0]
            listed = _ret1(part)
            _edit(fixture.part_manifest(0), status="preflight_passed", cleanup_allowed=False,
                  output_directory=str(part / "output-run-2"), superseded_runs=[_superseded(part)])
            (part / "output-run-2").mkdir()
            held = _ret1(part)

        self.assertEqual(verifier.PASS, listed.status, listed.detail)
        self.assertEqual(verifier.FAIL, held.status, held.detail)
        self.assertEqual(verifier.HELD_FOR_NEW_RUN, held.evidence["justification"])
        self.assertIn("superseded_runs[0]", held.detail)

    def test_a_superseded_run_whose_records_did_not_validate_justifies_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = self._discarded_after_a_new_run(
                root, [_superseded(root / "unit", status="validation_failed", cleanup_allowed=False,
                                   mztab_validation={"summary": {"failed": 1}})])
            check = _ret1(workspace)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual("", check.evidence["justification"])

    def test_raw_still_held_on_disk_is_not_refused(self) -> None:
        """The hold kept: the new run has not validated, the raw tree is present, nothing was deleted."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = _unit(root, status="preflight_passed", outputs=True,
                              output_directory=str(root / "unit" / "output-run-2"),
                              superseded_runs=[_superseded(root / "unit")])
            (workspace / "output-run-2").mkdir()
            check = _ret1(workspace)

        self.assertEqual(verifier.PASS, check.status, check.detail)

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
