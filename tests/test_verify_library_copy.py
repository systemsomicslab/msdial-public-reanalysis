"""FIN-3: MS-DIAL's loaded-library copy left the output and the library-copy store keeps it, once.

<project>_Loaded.msp2.dbs is MS-DIAL's serialised copy of every library a run loaded: with the private VS21
pair, a copy of the private library. Interactive 0.5.37 moves a campaign unit's copy into
<workspace root>/_library_copies, once per content, and records the object; before it the copy was deleted. A
restore for viewing links the object back into the output, under a viewing_restore hold that FIN-2 reads.

Also here: the names Interactive keeps beside the repositories (_library_copies, _tmp, _viewing) are never
taken for a repository by the download-store verifier, nor for a unit's workspace by the campaign plan; and a
restore for viewing, which recreates a released raw tree's paths under its hold, is read by RET-1 and B1 as the
release still standing.

The fixtures are written as Interactive writes them: run_finalisation's loaded_library_copy record and holds,
project_restore's viewing_restore hold, and library_copy_store's object layout (o/<id>/entry.json and
library-copy.dbs).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import test_verify_inputs_and_acquisition as inputs  # noqa: E402  (loads the gate)
import test_verify_extractor_and_release as release  # noqa: E402

verifier = inputs.verifier
_check, _write = inputs._check, inputs._write
_SPEC = importlib.util.spec_from_file_location("verify_download_store_library_copy",
                                               TESTS.parent / "scripts" / "verify-download-store.py")
assert _SPEC and _SPEC.loader
store_checker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(store_checker)

OBJECT = "5f330f52fee4c4ee0dbbf99dcdff47f3"
DBS = "Project-2610090530_Loaded.msp2.dbs"


def _copy(outcome: str = "stored", object_id: str = OBJECT, **extra) -> dict:
    """One copy as run_finalisation records it under loaded_library_copy.copies."""
    return {"name": DBS, "size_bytes": 1234, "sha256": "1" * 64, "normalized_sha256": "2" * 64,
            "content_key": object_id + "0" * 32, "content_key_kind": "zip_entries", "recognised": True,
            "object_id": object_id, "outcome": outcome, **extra}


def _hold(step: str, blocks: list[str], **extra) -> dict:
    return {"id": "0" * 32, "blocks": blocks, "step": step, "job_id": "run1", "reason": f"{step} stands",
            "recorded_at": "2026-10-12T00:00:00+00:00", **extra}


class _Unit:
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.workspace = self.root / "metabolights" / "MTBLS-SYN" / "unit-syn"
        self.output = self.workspace / "output"
        self.output.mkdir(parents=True)
        self.manifest = {
            "schema": "msdial-public-reanalysis-run.v1",
            "project": {"repository": "metabolights", "accession": "MTBLS-SYN", "analysis_unit_id": "unit-syn"},
            "workspace": str(self.workspace),
            "output_directory": str(self.output),
            "finalisation_holds": [],
        }

    def tearDown(self) -> None:
        self.directory.cleanup()

    def finalised(self, copies: "list[dict] | None" = None, campaign: bool = True, **finalisation) -> None:
        record = {"schema": "msdial-console-run-finalisation.v1", "job_id": "run1",
                  "campaign_approval_recorded": campaign, **finalisation}
        if copies is not None:
            record["loaded_library_copy"] = {"schema": "msdial-loaded-library-copy.v1", "store": "_library_copies",
                                             "copies": copies, "errors": []}
        self.manifest["console_run_finalisation"] = record

    def stored(self, object_id: str = OBJECT, size: int = 1234, record_size: int = 1234) -> None:
        directory = self.root / "_library_copies" / "o" / object_id
        _write(directory / "entry.json", {"schema": "msdial-library-copy-object.v1", "object_id": object_id,
                                          "size_bytes": record_size})
        (directory / "library-copy.dbs").write_bytes(b"x" * size)

    def check(self, check_id: str = "FIN-3"):
        _write(self.workspace / "provenance" / "run-manifest.json", self.manifest)
        return _check(verifier.verify(self.workspace, "before-publish"), check_id)


class TheCopyIsKeptOnce(_Unit, unittest.TestCase):
    def test_a_stored_copy_whose_object_the_store_holds_passes(self) -> None:
        self.finalised([_copy()])
        self.stored()
        check = self.check()

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIn(OBJECT, check.detail)

    def test_an_object_the_store_does_not_hold_here_is_a_warning_not_a_failure(self) -> None:
        self.finalised([_copy("deduplicated")])
        check = self.check()
        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("may be on another machine", check.detail)

        self.stored(size=10)
        self.assertEqual(verifier.WARN, self.check().status, "a file whose size is not its record's")

    def test_the_store_is_found_from_the_lease_when_the_workspace_says_nothing(self) -> None:
        self.finalised([_copy()])
        self.stored()
        self.manifest["project"] = {"analysis_unit_id": "unit-syn"}
        self.assertEqual(verifier.WARN, self.check().status)
        self.assertIn("could not be located", self.check().detail)
        self.manifest["download_cache"] = {"workspace_root": str(self.root)}
        self.assertEqual(verifier.PASS, self.check().status)

    def test_a_copy_deleted_before_the_store_warns_until_an_object_is_attached(self) -> None:
        self.finalised(None, loaded_library_copies_deleted=[{"name": DBS, "size_bytes": 1234, "sha256": "1" * 64}])
        check = self.check()
        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("before 0.5.37", check.detail)

        self.stored()
        self.manifest["library_copy_records"] = [{"schema": "msdial-library-copy-record.v1", "action": "attach",
                                                  "job_id": "run1", "object_id": OBJECT, "outcome": "attached",
                                                  "verified_by": "library_and_settings_identity"}]
        self.assertEqual(verifier.PASS, self.check().status)

    def test_a_copy_a_retry_deleted_before_the_store_warns_as_deleted_not_as_a_stopped_finalisation(self) -> None:
        # Up to 0.5.36 a copy held at finalisation was recorded there as none, and the retry that deleted it named
        # it only in its hold's resolution.
        self.finalised(None, loaded_library_copies_deleted=[])
        self.manifest["finalisation_hold_resolutions"] = [
            {"hold": "h0", "step": "loaded_library_copy", "job_id": "run0", "deleted": [{"name": "Earlier.dbs"}]},
            {"hold": "h1", "step": "loaded_library_copy", "job_id": "run1", "resolved_at": "2026-10-02T00:00:00+00:00",
             "deleted": [{"name": DBS, "size_bytes": 1234, "sha256": "1" * 64}]},
        ]
        check = self.check()
        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("before 0.5.37", check.detail)
        self.assertNotIn("stopped before that step", check.detail)
        self.assertEqual([DBS], [item["name"] for item in check.evidence["deleted_before_the_store"]])

        self.manifest["finalisation_hold_resolutions"].pop()
        self.assertEqual(verifier.NOT_EVALUABLE, self.check().status, "another run's deletion is not this run's")

    def test_a_quarantined_copy_warns(self) -> None:
        self.finalised([_copy("quarantined")])
        check = self.check()
        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("quarantined", check.detail)

    def test_a_copy_left_in_the_output_fails_held_or_not(self) -> None:
        self.finalised([])
        (self.output / DBS).write_bytes(b"a copy of the private library")
        check = self.check()
        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("no viewing restore stands", check.detail)

        self.manifest["finalisation_holds"] = [_hold("loaded_library_copy", ["sharing"])]
        check = self.check()
        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("see FIN-2", check.detail)

    def test_a_copy_a_viewing_restore_linked_back_warns_and_fin2_holds_sharing(self) -> None:
        self.finalised([_copy()])
        self.stored()
        (self.output / DBS).write_bytes(b"x" * 1234)
        self.manifest["finalisation_holds"] = [_hold("viewing_restore", ["sharing", "raw_deletion"],
                                                     restore_id="abc123", record="_viewing/r-abc123/restore.local.json")]
        check = self.check()
        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("abc123", check.detail)
        self.assertEqual(verifier.FAIL, self.check("FIN-2").status)

    def test_a_run_without_a_campaign_or_a_record_is_not_evaluable_and_not_required(self) -> None:
        (self.output / DBS).write_bytes(b"kept by design")
        self.finalised(None, campaign=False)
        check = self.check()
        self.assertEqual((verifier.NOT_EVALUABLE, False), (check.status, check.required))
        del self.manifest["console_run_finalisation"]
        check = self.check()
        self.assertEqual((verifier.NOT_EVALUABLE, False), (check.status, check.required))

    def test_a_run_that_wrote_no_copy_passes(self) -> None:
        self.finalised([])
        self.assertEqual(verifier.PASS, self.check().status)

    def test_a_finalisation_that_could_not_store_a_copy_and_records_none_warns(self) -> None:
        self.finalised([])
        self.manifest["console_run_finalisation"]["loaded_library_copy"]["errors"] = [
            f"{DBS}: PermissionError [WinError 5] Access is denied: _library_copies/o/{OBJECT}/entry.json"]
        check = self.check()
        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("could not store", check.detail)

    def test_a_copy_left_for_the_store_and_not_yet_recorded_there_warns_until_repaired(self) -> None:
        self.finalised([_copy("pending_repair", store_error="PermissionError [WinError 5] Access is denied")])
        self.stored()
        check = self.check()
        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("status --repair", check.detail)

        self.finalised([_copy("stored", repaired_at="2026-10-13T00:00:00+00:00")])
        self.assertEqual(verifier.PASS, self.check().status)

    def test_copies_that_are_not_the_runs_own_are_not_judged(self) -> None:
        self.finalised([_copy()])
        self.stored()
        record = {"schema": "msdial-library-copy-record.v1", "job_id": "run1"}
        self.manifest["library_copy_records"] = [
            # MS-DIAL's GUI saved other settings during a restore, and the store set its copy aside.
            {**record, **_copy("quarantined", "9" * 32), "action": "undo_gui_copy", "restore_id": "abc123"},
            # A Console of the run left a copy in its TEMP, stored on retry under its temporary name.
            {**record, **_copy(), "name": "tmpA1B2.tmp", "action": "console_temp"},
            # A GUI copy whose install stopped, completed by the store's repair.
            {**record, **_copy("stored", "8" * 32), "action": "repair", "how": "undo_gui_copy"},
        ]
        check = self.check()

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual([OBJECT], [item["object_id"] for item in check.evidence["copies"]])
        self.assertEqual(3, len(check.evidence["other_copies"]))

    def test_it_is_judged_with_the_production_run(self) -> None:
        [b6] = [stage for stage in verifier.COMPLETION_STAGES if stage[0] == "B6"]
        self.assertIn("FIN-3", b6[3])


class TheRestoredTreeIsStillReleased(unittest.TestCase):
    """A restore for viewing copies a run's containers back to a released tree's paths, under its hold."""

    HOLD = _hold("viewing_restore", ["sharing", "raw_deletion"], restore_id="abc123",
                 record="_viewing/r-abc123/restore.local.json")

    def _released(self, temporary: str, restored: bool) -> Path:
        workspace = release._unit(Path(temporary), status="raw_cleaned", raw_cleaned_at="2026-10-01T02:00:00+00:00",
                                  campaign_authorizations=[release._crossing(5)], outputs=True, **release.VALIDATED)
        shutil.rmtree(workspace / "raw")
        if restored:
            # What the restore writes: the directories the .mddata names, with copies of the containers, no input.
            (workspace / "raw" / "data").mkdir(parents=True)
            (workspace / "raw" / "data" / "s0_20261091730.dcl").write_bytes(b"a container copy")
        return workspace

    def test_a_released_unit_under_its_restore_warns_and_reads_as_released(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._released(temporary, restored=True)
            inputs._write(workspace / "provenance" / "run-manifest.json", {
                **json.loads((workspace / "provenance" / "run-manifest.json").read_text(encoding="utf-8")),
                "finalisation_holds": [self.HOLD]})
            report = verifier.verify(workspace, "before-publish")
        check = _check(report, "RET-1")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("viewing restore abc123", check.detail)
        self.assertEqual([{"unit": "unit", "restore_id": "abc123"}], check.evidence["viewing_restores"])
        self.assertTrue(report.progress["stages"]["B1"], "a restored tree is still a released one")

    def test_the_same_tree_with_no_restore_standing_fails_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = verifier.verify(self._released(temporary, restored=True), "before-publish")
        check = _check(report, "RET-1")
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("still on disk", check.detail)
        self.assertFalse(report.progress["stages"]["B1"])

    def test_a_part_restored_into_its_released_parents_tree_explains_the_parent_and_its_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = release.split.SplitFixture(Path(temporary))
            for part in range(2):
                release._edit(fixture.part_manifest(part), status="raw_cleaned",
                              raw_released_by=str(fixture.parent_manifest), raw_cleaned_at="2026-10-01T05:00:00+00:00",
                              raw_retention_policy="delete_after_validated_output", **release.VALIDATED)
                release._outputs(fixture.part_roots[part] / "output")
            release._edit(fixture.parent_manifest, raw_retention_policy="delete_after_validated_output",
                          campaign_authorizations=[release._crossing(5, entry_point="cleanup_split_parent")],
                          raw_release={"schema": "msdial-split-parent-raw-release.v1", "state": "deleted",
                                       "kind": "released", "authorized_by": {"approval_id": release.APPROVAL},
                                       "deleted_at": "2026-10-01T05:00:00+00:00",
                                       "parts": [{"analysis_unit_id": item, "status_at_release": "mztab_validated"}
                                                 for item in ("unit-dda", "unit-dia")]})
            # As Interactive's split writes it: each part with its manifest's path.
            recorded = json.loads(fixture.parent_manifest.read_text(encoding="utf-8"))["split_into"]
            release._edit(fixture.parent_manifest, split_into=[
                {**item, "manifest_path": str(fixture.part_manifest(index))} for index, item in enumerate(recorded)])
            shutil.rmtree(fixture.parent_root / "raw")
            (fixture.parent_root / "raw" / "data").mkdir(parents=True)
            (fixture.parent_root / "raw" / "data" / "s0_20261091730.dcl").write_bytes(b"a container copy")
            release._edit(fixture.part_manifest(0), finalisation_holds=[self.HOLD])
            parent = _check(verifier.verify(fixture.parent_root, "before-publish"), "RET-1")
            sibling = _check(verifier.verify(fixture.part_roots[1], "before-publish"), "RET-1")

        for check in (parent, sibling):
            self.assertEqual(verifier.WARN, check.status, check.detail)
            self.assertIn("viewing restore abc123 of unit-dda", check.detail)


class TheNamesBesideTheRepositories(unittest.TestCase):
    def test_the_download_store_verifier_takes_none_of_them_for_a_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("_library_copies", "_tmp", "_viewing"):
                (root / name / "x" / "_dl").mkdir(parents=True)
            (root / "metabolights" / "MTBLS-SYN" / "_dl").mkdir(parents=True)
            found = store_checker.stores_under(root)
        self.assertEqual([root / "metabolights" / "MTBLS-SYN" / "_dl"], found)

    def test_the_gate_reserves_what_interactive_reserves(self) -> None:
        self.assertEqual({"_dl", "_campaigns", "_library_copies", "_tmp", "_viewing"},
                         set(verifier.RESERVED_WORKSPACE_NAMES))


if __name__ == "__main__":
    unittest.main()
