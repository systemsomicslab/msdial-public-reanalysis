"""FIN-3: MS-DIAL's loaded-library copy left the output and the library-copy store keeps it, once.

<project>_Loaded.msp2.dbs is MS-DIAL's serialised copy of every library a run loaded: with the private VS21
pair, a copy of the private library. Interactive 0.5.37 moves a campaign unit's copy into
<workspace root>/_library_copies, once per content, and records the object; before it the copy was deleted. A
restore for viewing links the object back into the output, under a viewing_restore hold that FIN-2 reads.

Also here: the names Interactive keeps beside the repositories (_library_copies, _tmp, _viewing) are never
taken for a repository by the download-store verifier, nor for a unit's workspace by the campaign plan.

The fixtures are written as Interactive writes them: run_finalisation's loaded_library_copy record and holds,
project_restore's viewing_restore hold, and library_copy_store's object layout (o/<id>/entry.json and
library-copy.dbs).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import test_verify_inputs_and_acquisition as inputs  # noqa: E402  (loads the gate)

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

    def test_it_is_judged_with_the_production_run(self) -> None:
        [b6] = [stage for stage in verifier.COMPLETION_STAGES if stage[0] == "B6"]
        self.assertIn("FIN-3", b6[3])


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
