"""PRE-2, RET-1 and DSK-1 for a campaign that deletes raw data, and scripts/verify-download-store.py.

The user decided on 2026-09-30 that a campaign's raw data are deleted once every MS-DIAL output is
present and the mzTab-M validates, whatever the gate's verdict; that a unit that failed is retried twice
and then deleted; and that skipped and excluded units are deleted too. One campaign approval covering
boundary 5 stands in for each deletion's confirmation. The raw headers that decide which units run are
read by an extractor that must be a verified build of a pinned pair of commits, and every repository
object is fetched once per accession into a store whose files units link to.

These tests pin what the gate says of each of those records: which extractor read the headers (PRE-2),
whether a deletion had the authority and the reason it needed (RET-1), what a unit's raw tree occupies
when its files are links (DSK-1), and what the store holds that nothing accounts for (the store checker).
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import test_verify_split_and_progress as split  # noqa: E402  (loads the gate)

verifier = split.verifier
_status, _check, _write, _edit = split._status, split._check, split._write, split._edit
_SPEC = importlib.util.spec_from_file_location("verify_download_store",
                                               TESTS.parent / "scripts" / "verify-download-store.py")
assert _SPEC and _SPEC.loader
store_checker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(store_checker)

CURRENT_PIN = verifier.EXTRACTOR_PINNED_BUILDS[0]
OLDER_PIN = verifier.EXTRACTOR_PINNED_BUILDS[1]
PLANNED_PIN = next(entry for entry in verifier.EXTRACTOR_PINNED_BUILDS if entry["state"] != "built")
SHA = "e" * 64
APPROVAL = "campaign-2026-10-01"


def _extractor(pin: dict | None = CURRENT_PIN, *, sha256: str = SHA, status: str = "verified",
               pinned: bool | None = None) -> dict:
    """An extractor record as Interactive's _extractor_record writes it (0.5.17 and later)."""
    return {
        "path": "C:\\synthetic\\RawMetadataConsoleApp.exe", "size_bytes": 19968,
        "modified_at": "2026-10-01T00:00:00+00:00", "sha256": sha256, "inventory_sha256": "f" * 64,
        "file_count": 120, "provenance_status": status, "provenance_path": "C:\\synthetic\\record.json",
        "msrawdataworkbench_commit": (pin or {}).get("msrawdataworkbench", ""),
        "msdialworkbench_commit": (pin or {}).get("MsdialWorkbench", ""),
        "product_version": "1.0.0+x",
        "pinned": (bool(pin) and pin["state"] == "built" and status == "verified") if pinned is None else pinned,
        "pin_state": (pin or {}).get("state", ""), "selected_from": "setting",
    }


def _crossing(boundary, *, unit: str = "unit", retention: str = "delete_after_validated_output",
              entry_point: str = "msdial_cleanup_repository_raw",
              validated_at: str = "2026-10-01T00:00:00+00:00") -> dict:
    """A boundary crossing as campaign_authorization.validate returns it and the unit manifest keeps it."""
    return {
        "schema": "msdial-campaign-authorization.v1", "approval_id": APPROVAL, "campaign_id": "campaign",
        "manifest_digest": "sha256:" + "a" * 64, "authorization_path": "C:\\synthetic\\authorization.json",
        "authorization_sha256": "b" * 64, "raw_retention_policy": retention, "boundary": boundary,
        "unit_id": unit, "covered_as": "listed", "entry_point": entry_point,
        "validated_at": validated_at,
    }


def _mztab(output: Path) -> Path:
    """The run's mzTab-M, where finalisation leaves it."""
    output.mkdir(parents=True, exist_ok=True)
    path = output / "AlignResult-1.mzTab"
    path.write_text("MTD\tmzTab-version\t2.0.0-M\n", encoding="ascii")
    return path


def _outputs(output: Path, planned: tuple[str, ...] = ("s0.mdpeak",), written: tuple[str, ...] | None = None) -> Path:
    """A finished run's output: its mzTab-M, the exports its run manifest planned, and those it wrote.

    Every planned export is written unless `written` names fewer, as MS-DIAL leaves a file it could not read.
    """
    for name in planned if written is None else written:
        (output / name).write_bytes(b"peak")
    _write(output / "run-manifest.json", {"expected_analysis_exports": [str(output / name) for name in planned]})
    return _mztab(output)


def _unit(root: Path, name: str = "unit", *, raw: bool = True, mztab: bool = False, outputs: bool = False,
          **manifest) -> Path:
    """One unit workspace under an accession directory, with a raw tree holding one input.

    mztab writes the mzTab-M alone, and outputs a finished run's output (_outputs).
    """
    workspace = root / name
    (workspace / "output").mkdir(parents=True)
    if outputs:
        _outputs(workspace / "output")
    elif mztab:
        _mztab(workspace / "output")
    candidate = workspace / "raw" / "data" / "s0.mzML"
    if raw:
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"x" * 10)
    record = {
        "project": {"analysis_unit_id": name, "repository": "metabolights"},
        "execution_allowed": True,
        "raw_directory": str(workspace / "raw"),
        "workspace": str(workspace),
        "input_candidates": [str(candidate)],
        "downloads": [{"path": str(candidate), "sha256": "ab" * 32}],
        "raw_retention_policy": "delete_after_validated_output",
    }
    record.update(manifest)
    _write(workspace / "provenance" / "run-manifest.json", record)
    return workspace


# What run finalisation records for one mzTab-M that passed (mztab_validation.validate_mztab_files).
VALIDATED = {"finalized_at": "2026-10-01T01:00:00+00:00", "cleanup_allowed": True,
             "mztab_validation": {"status": "passed", "summary": {"status": "passed", "passed": 1, "warnings": 0,
                                                                  "failed": 0, "file_count": 1}}}


def _ret1(workspace: Path):
    return _check(verifier.verify(workspace, "before-publish"), "RET-1")


# ---------------------------------------------------------------------------------------------
# PRE-2
# ---------------------------------------------------------------------------------------------

class ExtractorIdentityTests(unittest.TestCase):
    def _gate(self, temporary: str, extractor: dict | None, *, started_at: str = "", **manifest):
        preflight = {"exit_code": 0, "summary": {"per_file": []}}
        if extractor is not None:
            preflight["extractor"] = extractor
        if started_at:
            preflight["started_at"] = started_at
        workspace = _unit(Path(temporary), raw_metadata_preflight=preflight, **manifest)
        return _check(verifier.verify(workspace, "before-production"), "PRE-2")

    def test_a_legacy_record_without_a_checksum_warns(self) -> None:
        """MTBLS2207's record: path, size and mtime only. A WARN, which --strict does not hold."""
        with tempfile.TemporaryDirectory() as temporary:
            check = self._gate(temporary, {"path": "C:\\x\\RawMetadataConsoleApp.exe", "size_bytes": 19968,
                                           "modified_at": "2026-09-06T07:23:42+00:00"})

        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("path, size and modification time only", check.detail)
        self.assertFalse(check.strict_failure)

    def test_a_verified_build_of_the_current_pin_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            check = self._gate(temporary, _extractor(), campaign_authorizations=[_crossing(4)])

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual("built", check.evidence["gate_pin_state"])
        self.assertNotIn("C:\\synthetic", json.dumps(check.evidence), "the evidence names no location")

    def test_a_verified_build_of_an_older_built_pin_passes_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            check = self._gate(temporary, _extractor(OLDER_PIN))

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIn("not the current pin", check.detail)

    def test_the_5f60446_build_is_current_and_the_a12293c61_build_still_passes(self) -> None:
        """Interactive 0.5.35 pinned 5f60446; a unit read by the a12293c61 build before it stays a PASS."""
        current = verifier._extractor_pin("5f604462d7bd61141bf764ca04ce52bf3f6452c9", "f0583493a44e73723f53ae312e33955f62052dd7")
        previous = verifier._extractor_pin("a12293c612a4e29b23d1d584f1c19556d76863f6", "f0583493a44e73723f53ae312e33955f62052dd7")
        self.assertEqual(CURRENT_PIN, current)
        self.assertEqual("built", previous["state"])
        with tempfile.TemporaryDirectory() as temporary:
            now = self._gate(temporary, _extractor(current), campaign_authorizations=[_crossing(4)])
        with tempfile.TemporaryDirectory() as temporary:
            before = self._gate(temporary, _extractor(previous), campaign_authorizations=[_crossing(4)])

        self.assertEqual(verifier.PASS, now.status, now.detail)
        self.assertNotIn("not the current pin", now.detail)
        self.assertEqual(verifier.PASS, before.status, before.detail)
        self.assertIn("not the current pin (5f604462d)", before.detail)

    def test_a_stale_or_dirty_build_is_refused(self) -> None:
        for status in ("stale_mismatch", "dirty_source"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                check = self._gate(temporary, _extractor(status=status, pinned=False))

            self.assertEqual(verifier.FAIL, check.status, check.detail)

    def test_an_unpinned_build_warns_outside_a_campaign_and_is_refused_in_one(self) -> None:
        cases = (("planned pair", _extractor(PLANNED_PIN)), ("no pin", _extractor(None)),
                 ("no build record", _extractor(CURRENT_PIN, status="absent", pinned=False)))
        for name, extractor in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                outside = self._gate(temporary, extractor)
            with self.subTest(name, campaign=True), tempfile.TemporaryDirectory() as temporary:
                inside = self._gate(temporary, extractor, campaign_authorizations=[_crossing(1)])
            with self.subTest(name, disposition=True), tempfile.TemporaryDirectory() as temporary:
                decided = self._gate(temporary, extractor, campaign_disposition={
                    "schema": "msdial-campaign-disposition.v1", "disposition": "run", "applied": True})

            self.assertEqual(verifier.WARN, outside.status, outside.detail)
            self.assertEqual(verifier.FAIL, inside.status, inside.detail)
            self.assertEqual(verifier.FAIL, decided.status, decided.detail)

    def test_a_pin_the_mirror_does_not_know_is_not_a_fact_about_the_unit(self) -> None:
        """Interactive may pin a build before the gate's mirror is updated; that is a WARN, never a FAIL."""
        unknown = {"msrawdataworkbench": "1" * 40, "MsdialWorkbench": "2" * 40, "state": "built"}
        with tempfile.TemporaryDirectory() as temporary:
            check = self._gate(temporary, _extractor(unknown, pinned=True), campaign_authorizations=[_crossing(1)])

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("mirror", check.detail)

    def test_a_part_read_by_another_build_than_its_split_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = split.SplitFixture(Path(temporary))
            _edit(fixture.parent_manifest, raw_metadata_preflight={"exit_code": 0, "summary": {},
                                                                   "extractor": _extractor(sha256="1" * 64)})
            _edit(fixture.part_manifest(0), raw_metadata_preflight={"exit_code": 0, "summary": {},
                                                                    "extractor": _extractor()})
            _edit(fixture.part_manifest(1), raw_metadata_preflight={"exit_code": 0, "summary": {},
                                                                    "extractor": _extractor(sha256="1" * 64)})
            differs, same = (_check(fixture.gate(part), "PRE-2") for part in (0, 1))

        self.assertEqual(verifier.WARN, differs.status, differs.detail)
        self.assertIn("two different builds", differs.detail)
        self.assertEqual(verifier.PASS, same.status, same.detail)

    def test_a_disposition_decided_by_another_build_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            check = self._gate(temporary, _extractor(), campaign_disposition={
                "disposition": "run", "applied": True, "extractor": {"sha256": "1" * 64}})

        self.assertEqual(verifier.WARN, check.status, check.detail)

    def test_no_preflight_is_not_owed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary))
            check = _check(verifier.verify(workspace, "before-production"), "PRE-2")

        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertFalse(check.required)

    def test_pre2_judges_b2(self) -> None:
        judged = next(checks for code, _name, _meaning, checks in verifier.COMPLETION_STAGES if code == "B2")
        self.assertIn("PRE-2", judged)

    # A unit is judged by whether a campaign acts on its read, as Interactive recorded when it decided the
    # disposition, and not by the crossings it carries now.

    UNRECORDED_BUILD = staticmethod(lambda: _extractor(CURRENT_PIN, status="absent", pinned=False))
    READ_AT = "2026-09-20T00:00:00+00:00"
    LATER = ("2026-10-01T00:00:00+00:00", "2026-10-01T00:05:00+00:00")

    def test_a_unit_adopted_by_a_campaign_after_its_preflight_warns(self) -> None:
        """MTBLS2207's shape: crossings 3 and 4 made after a read whose disposition was advice only."""
        later = [_crossing(3, validated_at=self.LATER[0]), _crossing(4, validated_at=self.LATER[1])]
        with tempfile.TemporaryDirectory() as temporary:
            check = self._gate(temporary, self.UNRECORDED_BUILD(), started_at=self.READ_AT,
                               campaign_authorizations=later,
                               campaign_disposition={"schema": "msdial-campaign-disposition.v1",
                                                     "disposition": "run", "applied": False})

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertFalse(check.evidence["under_campaign"])
        self.assertEqual("disposition_advice", check.evidence["campaign_basis"])
        self.assertEqual(["3", "4"], check.evidence["boundaries_crossed_after_preflight"])
        self.assertIn("applied false", check.detail)
        self.assertIn("crossed boundaries 3, 4", check.detail)

    def test_a_disposition_a_campaign_applied_to_an_earlier_read_is_refused(self) -> None:
        """classify_preflight applies a campaign's disposition to a read made before it: the campaign acts on it."""
        later = [_crossing(3, validated_at=self.LATER[0])]
        with tempfile.TemporaryDirectory() as temporary:
            check = self._gate(temporary, self.UNRECORDED_BUILD(), started_at=self.READ_AT,
                               campaign_authorizations=later,
                               campaign_disposition={"disposition": "run", "applied": True,
                                                     "campaign": {"approval_id": APPROVAL,
                                                                  "basis": "authorization_recorded"}})

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertEqual("disposition_applied", check.evidence["campaign_basis"])
        self.assertIn(f"Campaign approval {APPROVAL} applied the disposition", check.detail)

    def test_without_a_disposition_only_the_crossings_before_the_read_count(self) -> None:
        """A split parent gets no disposition: its crossings are ordered against the preflight, as instants."""
        cases = (
            ("before", "2026-09-19T23:59:00+00:00", self.READ_AT, verifier.FAIL, "crossing_before_preflight"),
            ("at the same instant", "2026-09-20T09:00:00+09:00", self.READ_AT, verifier.FAIL,
             "crossing_before_preflight"),
            ("after, with an offset", "2026-09-20T09:01:00+09:00", self.READ_AT, verifier.WARN,
             "crossing_after_preflight"),
            ("no start recorded", self.LATER[0], "", verifier.FAIL, "crossing_unordered"),
        )
        for name, validated_at, started_at, expected, basis in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                check = self._gate(temporary, self.UNRECORDED_BUILD(), started_at=started_at,
                                   campaign_authorizations=[_crossing(4, validated_at=validated_at)])

            self.assertEqual(expected, check.status, check.detail)
            self.assertEqual(basis, check.evidence["campaign_basis"])


def _pinned_builds(source: str) -> list[dict]:
    """Interactive's PINNED_BUILDS, read from its source without importing or running it."""
    tree = ast.parse(source)
    names: dict[str, str] = {}
    table = None
    for node in tree.body:
        targets, value = [], None
        if isinstance(node, ast.Assign):
            targets, value = [item.id for item in node.targets if isinstance(item, ast.Name)], node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets, value = [node.target.id], node.value
        for name in targets:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                names[name] = value.value
            if name == "PINNED_BUILDS":
                table = value
    if not isinstance(table, (ast.Tuple, ast.List)):
        raise ValueError("PINNED_BUILDS is not a literal table")

    def value_of(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return names[node.id]
        raise ValueError(ast.dump(node))

    rows = []
    for element in table.elts:
        row = {value_of(key): value_of(value) for key, value in zip(element.keys, element.values)}
        rows.append({key: row[key] for key in (names["RAW_TREE"], names["COMMON_TREE"], "state")})
    return rows


class ExtractorPinMirrorTests(unittest.TestCase):
    """The gate's EXTRACTOR_PINNED_BUILDS is Interactive's PINNED_BUILDS, at the commit it names."""

    MODULE = "msdial_app/raw_metadata_extractor.py"

    def _interactive(self) -> Path:
        root = Path(os.environ.get("MSDIAL_INTERACTIVE_APP") or r"D:\0_SourceCode\msdial_interactive_app")
        if not (root / ".git").exists():
            self.skipTest(f"no Interactive checkout at {root} (set MSDIAL_INTERACTIVE_APP)")
        return root

    def _git(self, root: Path, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)

    def test_the_parser_reads_a_table_written_with_names(self) -> None:
        source = ('A = "raw"\nB = "common"\nBUILT = "built"\n'
                  'PINNED_BUILDS: tuple = ({A: "1" * 0 or "11", B: "22", "state": BUILT, "note": "x"},)\n')
        with self.assertRaises(ValueError):
            _pinned_builds(source)  # an expression is not a literal: the mirror must not guess
        source = ('RAW_TREE = "raw"\nCOMMON_TREE = "common"\n'
                  'PINNED_BUILDS = ({RAW_TREE: "11", COMMON_TREE: "22", "state": "built"},)\n')
        self.assertEqual([{"raw": "11", "common": "22", "state": "built"}], _pinned_builds(source))

    def test_the_mirror_is_interactives_table_at_the_commit_it_names(self) -> None:
        root = self._interactive()
        shown = self._git(root, "show", f"{verifier.EXTRACTOR_PINS_MIRRORED_FROM}:{self.MODULE}")
        if shown.returncode != 0:
            self.skipTest(f"Interactive {verifier.EXTRACTOR_PINS_MIRRORED_FROM} is not in {root}")
        self.assertEqual(_pinned_builds(shown.stdout), [dict(entry) for entry in verifier.EXTRACTOR_PINNED_BUILDS])

    def test_an_interactive_checkout_past_the_mirror_has_not_moved_its_pins(self) -> None:
        """Checked against the checkout's HEAD whenever it descends from the mirrored commit."""
        root = self._interactive()
        descends = self._git(root, "merge-base", "--is-ancestor", verifier.EXTRACTOR_PINS_MIRRORED_FROM, "HEAD")
        if descends.returncode != 0:
            self.skipTest(f"the checkout's HEAD does not descend from {verifier.EXTRACTOR_PINS_MIRRORED_FROM}")
        shown = self._git(root, "show", f"HEAD:{self.MODULE}")
        self.assertEqual(0, shown.returncode, shown.stderr)
        self.assertEqual(_pinned_builds(shown.stdout), [dict(entry) for entry in verifier.EXTRACTOR_PINNED_BUILDS],
                         "Interactive's PINNED_BUILDS moved: mirror it again and name the commit")


# ---------------------------------------------------------------------------------------------
# RET-1
# ---------------------------------------------------------------------------------------------

class RetentionUnderACampaignTests(unittest.TestCase):
    def test_a_validated_unit_deleted_under_the_campaign_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary), status="raw_cleaned", raw_cleaned_at="2026-10-01T02:00:00+00:00",
                              campaign_authorizations=[_crossing(4), _crossing(5)], outputs=True, **VALIDATED)
            shutil.rmtree(workspace / "raw")
            check = _ret1(workspace)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIn(APPROVAL, check.detail)
        self.assertEqual("validated", check.evidence["justification"])

    def test_the_gates_other_verdicts_do_not_enter(self) -> None:
        """Deletion follows the outputs, whatever the gate says of them: here ELIG-1 fails."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary), status="raw_cleaned", execution_allowed=False,
                              downloads=[], campaign_authorizations=[_crossing(5)], outputs=True, **VALIDATED)
            shutil.rmtree(workspace / "raw")
            report = verifier.verify(workspace, "all")

        self.assertEqual(verifier.FAIL, _status(report, "ELIG-1"))
        self.assertEqual(verifier.PASS, _status(report, "RET-1"))

    def test_a_failed_unit_deleted_after_its_retries_passes_on_its_failure_record(self) -> None:
        failures = [{"reason": "Console exited 1", "exit_code": 1, "recorded_at": f"2026-10-01T0{i}:00:00+00:00"}
                    for i in range(3)]
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary), status="discarded", discarded_at="2026-10-01T04:00:00+00:00",
                              run_failures=failures,
                              campaign_authorizations=[_crossing(5, entry_point="campaign_runner.discard")])
            shutil.rmtree(workspace / "raw")
            check = _ret1(workspace)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual("failed", check.evidence["justification"])

    def test_a_skipped_or_excluded_unit_passes_on_its_disposition_or_the_runners_record(self) -> None:
        for name, extra, record in (
                ("skip", {"campaign_disposition": {"disposition": "skip", "applied": True,
                                                   "reasons": ["acquisition_unresolved"]}}, None),
                ("exclude", {"campaign_disposition": {"disposition": "exclude", "applied": True,
                                                      "reasons": ["ion_mobility_out_of_scope"]}}, None),
                ("runner", {}, {"schema": "msdial-campaign-unit-record.v1", "state": "failed",
                                "terminal_reason": "gate_before_production_failed"})):
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                workspace = _unit(Path(temporary), status="discarded",
                                  campaign_authorizations=[_crossing(5)], **extra)
                if record:
                    _write(workspace / "campaign-record.json", record)
                shutil.rmtree(workspace / "raw")
                check = _ret1(workspace)

            self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_a_deletion_with_no_reason_on_record_warns(self) -> None:
        for crossings in ([_crossing(5)], []):
            with self.subTest(campaign=bool(crossings)), tempfile.TemporaryDirectory() as temporary:
                workspace = _unit(Path(temporary), status="discarded", campaign_authorizations=crossings)
                shutil.rmtree(workspace / "raw")
                check = _ret1(workspace)

            self.assertEqual(verifier.WARN, check.status, check.detail)

    def test_raw_cleaned_while_the_tree_is_present_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary), status="raw_cleaned", campaign_authorizations=[_crossing(5)],
                              **VALIDATED)
            check = _ret1(workspace)

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("still on disk", check.detail)

    def test_a_tree_deleted_without_an_authorization_or_a_confirmation_is_refused(self) -> None:
        for policy in ("delete_after_validated_output", "keep"):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as temporary:
                workspace = _unit(Path(temporary), status="mztab_validated", raw_retention_policy=policy,
                                  campaign_authorizations=[_crossing(3), _crossing(4)], **VALIDATED)
                shutil.rmtree(workspace / "raw")
                check = _ret1(workspace)

            self.assertEqual(verifier.FAIL, check.status, check.detail)
            self.assertIn("without an authorization or a confirmation", check.detail)

    def test_an_authorized_deletion_whose_completion_was_not_written_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary), status="cleanup_pending_confirmation",
                              campaign_authorizations=[_crossing(5)], **VALIDATED)
            shutil.rmtree(workspace / "raw")
            gone = _ret1(workspace)
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary), status="cleanup_pending_confirmation",
                              campaign_authorizations=[_crossing(5)], **VALIDATED)
            present = _ret1(workspace)

        self.assertEqual(verifier.WARN, gone.status, gone.detail)
        self.assertEqual(verifier.WARN, present.status, present.detail)
        self.assertIn("did not complete", present.detail)

    def test_an_approval_that_keeps_raw_data_authorizes_no_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary), status="raw_cleaned",
                              campaign_authorizations=[_crossing(5, retention="keep")], **VALIDATED)
            shutil.rmtree(workspace / "raw")
            check = _ret1(workspace)

        self.assertEqual(verifier.FAIL, check.status, check.detail)

    def test_a_cleanup_of_a_run_that_did_not_validate_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary), status="raw_cleaned", finalized_at="2026-10-01T01:00:00+00:00",
                              mztab_validation={"summary": {"failed": 2}})
            shutil.rmtree(workspace / "raw")
            check = _ret1(workspace)

        self.assertEqual(verifier.FAIL, check.status, check.detail)

    def test_a_confirmed_cleanup_without_a_campaign_still_passes(self) -> None:
        """Outside a campaign raw_cleaned is written only on a person's confirmed=true, as before."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary), status="raw_cleaned", outputs=True, **VALIDATED)
            shutil.rmtree(workspace / "raw")
            check = _ret1(workspace)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIn("confirmation", check.detail)

    def test_a_live_store_claim_after_the_release_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = _unit(root, status="raw_cleaned", campaign_authorizations=[_crossing(5)], outputs=True,
                              **VALIDATED)
            shutil.rmtree(workspace / "raw")
            StoreBuilder(root).object("a" * 64, "s0.mzML", url="https://x/s0.mzML", claims={"unit": "materialized"})
            check = _ret1(workspace)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual({"materialized": 1}, check.evidence["store_claims"])

    def test_a_validated_record_without_its_mztab_on_disk_justifies_no_deletion(self) -> None:
        """Validated is B7's fact, which needs the mzTab-M in output as well as the manifest's word for it."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary), status="raw_cleaned", raw_cleaned_at="2026-10-01T02:00:00+00:00",
                              campaign_authorizations=[_crossing(4), _crossing(5)], **VALIDATED)
            shutil.rmtree(workspace / "raw")
            report = verifier.verify(workspace, "all")

        check = _check(report, "RET-1")
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("no mzTab-M is in its output", check.detail)
        self.assertFalse(report.progress["stages"]["B7"])

    def test_a_validation_that_checked_no_file_justifies_no_deletion(self) -> None:
        cases = (
            ("no file", {"status": "warning", "summary": {"status": "warning", "passed": 0, "warnings": 1,
                                                          "failed": 0, "file_count": 0}}, "file_count 0"),
            ("status failed", {"status": "failed", "summary": {"status": "failed", "passed": 0, "failed": 0,
                                                               "file_count": 0}}, "status is failed"),
            ("nothing counted", {"summary": {"failed": 0}}, "records no file it checked"),
        )
        for name, validation, said in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                workspace = _unit(Path(temporary), status="raw_cleaned", finalized_at="2026-10-01T01:00:00+00:00",
                                  mztab_validation=validation, campaign_authorizations=[_crossing(5)], outputs=True)
                shutil.rmtree(workspace / "raw")
                check = _ret1(workspace)

            self.assertEqual(verifier.FAIL, check.status, check.detail)
            self.assertIn(said, check.detail)

    def test_a_validation_with_warnings_only_is_a_validated_run(self) -> None:
        """Finalisation calls a run validated when it checked a file or more and none failed."""
        warned = {"status": "warning", "summary": {"status": "warning", "passed": 0, "warnings": 1, "failed": 0,
                                                   "file_count": 1}}
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary), status="raw_cleaned", finalized_at="2026-10-01T01:00:00+00:00",
                              mztab_validation=warned, campaign_authorizations=[_crossing(5)], outputs=True)
            shutil.rmtree(workspace / "raw")
            report = verifier.verify(workspace, "all")

        check = _check(report, "RET-1")
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual("validated", check.evidence["justification"])
        self.assertTrue(report.progress["stages"]["B7"])

    def test_a_cleanup_whose_outputs_are_incomplete_is_refused(self) -> None:
        """Every MS-DIAL output present and the mzTab-M validated: a validated mzTab-M alone is not enough.

        MS-DIAL skips a file it cannot read without saying so, and finalisation reads only the mzTab-M.
        """
        cases = (
            ("an export absent", {"planned": ("s0.mdpeak", "s1.mdpeak"), "written": ("s0.mdpeak",)},
             "1 of the 2 exports its run planned are absent (s1.mdpeak)"),
            ("no .mdpeak", {"planned": (), "written": ()}, "no .mdpeak is in its output"),
            ("no run manifest", None, "run manifest is absent"),
        )
        for name, written, said in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                workspace = _unit(Path(temporary), status="raw_cleaned", raw_cleaned_at="2026-10-01T02:00:00+00:00",
                                  campaign_authorizations=[_crossing(4), _crossing(5)], mztab=True, **VALIDATED)
                if written is not None:
                    _outputs(workspace / "output", **written)
                shutil.rmtree(workspace / "raw")
                report = verifier.verify(workspace, "all")

            check = _check(report, "RET-1")
            self.assertEqual(verifier.FAIL, check.status, check.detail)
            self.assertIn(said, check.detail)
            self.assertNotEqual("validated", check.evidence["justification"])
            self.assertTrue(report.progress["stages"]["B7"], "the mzTab-M itself is validated")
            if name == "an export absent":
                self.assertEqual(verifier.FAIL, _status(report, "EXP-1"))
                self.assertEqual(1, check.evidence["absent_export_count"])
                self.assertTrue(check.evidence["absent_exports"][0].endswith("s1.mdpeak"))

    def test_a_validated_unit_with_incomplete_outputs_still_holding_its_raw_is_not_due(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary), status="mztab_validated", **VALIDATED)
            _outputs(workspace / "output", ("s0.mdpeak", "s1.mdpeak"), ("s0.mdpeak",))
            check = _ret1(workspace)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("not yet due", check.detail)

    def test_a_campaign_units_deletion_without_a_boundary_5_crossing_warns(self) -> None:
        """A confirmed=true records no crossing, and neither does a component that forgot its approval."""
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary), status="raw_cleaned", raw_cleaned_at="2026-10-01T02:00:00+00:00",
                              campaign_authorizations=[_crossing(1), _crossing(3), _crossing(4)], outputs=True,
                              **VALIDATED)
            shutil.rmtree(workspace / "raw")
            check = _ret1(workspace)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("boundaries 1, 3, 4 and none for boundary 5", check.detail)
        self.assertNotIn("a person's confirmation:", check.detail)
        self.assertEqual("validated", check.evidence["justification"])

    def test_a_confirmed_cleanup_does_not_say_who_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary), status="raw_cleaned", outputs=True, **VALIDATED)
            shutil.rmtree(workspace / "raw")
            check = _ret1(workspace)

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIn("who gave it is not recorded", check.evidence["authority"])

    def test_a_claim_opened_after_the_deletion_is_not_left_over(self) -> None:
        """A re-run's pre-claim reopens the released claim; it is a new consumer, not the tree that went."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = _unit(root, status="raw_cleaned", raw_cleaned_at="2026-09-01T02:00:00+00:00",
                              campaign_authorizations=[_crossing(5)], outputs=True, **VALIDATED)
            shutil.rmtree(workspace / "raw")
            builder = StoreBuilder(root)
            builder.object("a" * 64, "s0.mzML", url="https://x/s0.mzML")
            builder.claim("https://x/s0.mzML", "unit", "pending", claimed_at="2026-09-02T00:00:00+00:00",
                          history=[{"state": "released", "released_at": "2026-09-01T03:00:00+00:00"}])
            reopened = _ret1(workspace)
            builder.claim("https://x/s0.mzML", "unit", "materialized", object_id="a" * 16,
                          claimed_at="2026-09-01T00:00:00+00:00")
            left_over = _ret1(workspace)

        self.assertEqual(verifier.PASS, reopened.status, reopened.detail)
        self.assertEqual(1, reopened.evidence["store_claims_opened_after_deletion"])
        self.assertEqual(verifier.WARN, left_over.status, left_over.detail)
        self.assertIn("still live", left_over.detail)


class ReleasedSplitParentTests(unittest.TestCase):
    """The parent's raw tree is released once every part has ended, recorded as its raw_release."""

    def _released(self, temporary: str, *, listed=("unit-dda", "unit-dia"), state: str = "deleted",
                  remove: bool = True) -> split.SplitFixture:
        fixture = split.SplitFixture(Path(temporary))
        for part in range(2):
            _edit(fixture.part_manifest(part), status="raw_cleaned", raw_released_by=str(fixture.parent_manifest),
                  raw_cleaned_at="2026-10-01T05:00:00+00:00", raw_retention_policy="delete_after_validated_output",
                  **VALIDATED)
            _outputs(fixture.part_roots[part] / "output")
        _edit(fixture.parent_manifest, raw_retention_policy="delete_after_validated_output",
              campaign_authorizations=[_crossing(5, entry_point="cleanup_split_parent")],
              raw_release={"schema": "msdial-split-parent-raw-release.v1", "state": state, "kind": "released",
                           "authorized_by": {"approval_id": APPROVAL}, "deleted_at": "2026-10-01T05:00:00+00:00",
                           "parts": [{"analysis_unit_id": item, "status_at_release": "mztab_validated"}
                                     for item in listed]})
        if remove:
            shutil.rmtree(fixture.parent_root / "raw")
        return fixture

    def test_both_parts_of_a_released_parent_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._released(temporary)
            reports = [verifier.verify(root, "all") for root in fixture.part_roots]
            parent = _check(verifier.verify(fixture.parent_root, "before-publish"), "RET-1")

        for report in reports:
            self.assertEqual(verifier.PASS, _status(report, "RET-1"), _check(report, "RET-1").detail)
            self.assertEqual(verifier.PASS, _status(report, "SPL-1"))
            dsk = _check(report, "DSK-1")
            self.assertEqual(verifier.NOT_EVALUABLE, dsk.status)
            self.assertFalse(dsk.required)
            self.assertTrue(report.progress["stages"]["B1"], "a released tree is not a lost download")
        self.assertEqual(verifier.PASS, parent.status, parent.detail)

    def test_a_part_the_release_does_not_list_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._released(temporary, listed=("unit-dda",))
            listed, missing = (_check(verifier.verify(root, "before-publish"), "RET-1") for root in fixture.part_roots)
            parent = _check(verifier.verify(fixture.parent_root, "before-publish"), "RET-1")

        self.assertEqual(verifier.PASS, listed.status, listed.detail)
        self.assertEqual(verifier.WARN, missing.status, missing.detail)
        self.assertIn("does not list unit-dia", missing.detail)
        self.assertEqual(verifier.WARN, parent.status, parent.detail)

    def test_a_part_whose_outputs_are_incomplete_is_judged_as_one_that_failed(self) -> None:
        """A validated mzTab-M beside a missing export is in effect a failure: released after its retries it
        passes on its failure record, and without one it warns, naming the export."""
        failures = [{"reason": "an export is missing", "recorded_at": f"2026-10-01T0{i}:00:00+00:00"} for i in range(3)]
        for name, extra, expected in (("retried", {"run_failures": failures}, verifier.PASS),
                                      ("no failure record", {}, verifier.WARN)):
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                fixture = self._released(temporary)
                _edit(fixture.part_manifest(0), status="mztab_validated", **extra)
                _outputs(fixture.part_roots[0] / "output", ("s0.mdpeak", "s1.mdpeak"), ("s0.mdpeak",))
                check = _check(verifier.verify(fixture.part_roots[0], "before-publish"), "RET-1")

            self.assertEqual(expected, check.status, check.detail)
            if expected == verifier.PASS:
                self.assertEqual("failed", check.evidence["justification"])
            else:
                self.assertEqual("", check.evidence["justification"])
                self.assertIn("1 of the 2 exports its run planned are absent (s1.mdpeak)", check.detail)

    def test_a_part_claiming_raw_cleaned_while_its_tree_is_present_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._released(temporary, state="deleting", remove=False)
            check = _check(verifier.verify(fixture.part_roots[0], "before-publish"), "RET-1")

        self.assertEqual(verifier.FAIL, check.status, check.detail)

    def test_a_release_recorded_as_deleted_while_the_tree_is_present_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._released(temporary, remove=False)
            for part in range(2):
                _edit(fixture.part_manifest(part), status="mztab_validated")
            parent = _check(verifier.verify(fixture.parent_root, "before-publish"), "RET-1")
            part = _check(verifier.verify(fixture.part_roots[0], "before-publish"), "RET-1")

        self.assertEqual(verifier.FAIL, parent.status, parent.detail)
        self.assertEqual(verifier.FAIL, part.status, part.detail)


# ---------------------------------------------------------------------------------------------
# DSK-1
# ---------------------------------------------------------------------------------------------

class StoreBuilder:
    """An accession's download store, laid out and recorded as Interactive's download_store.py does."""

    def __init__(self, accession: Path) -> None:
        self.root = accession / "_dl"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]

    def object(self, sha256: str, name: str, *, url: str = "", body: bytes = b"object-bytes", tree: dict | None = None,
               claims: dict | None = None, index: bool = True, state: str = "ready", entry: bool = True,
               **extra) -> Path:
        object_id = sha256[:16]
        directory = self.root / "o" / object_id
        rows = []
        if state != "collected":
            (directory / "obj").mkdir(parents=True)
            (directory / "obj" / name).write_bytes(body)
            rows.append(("obj/" + name, directory / "obj" / name))
            for relative, content in (tree or {}).items():
                path = directory / "t" / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                rows.append(("t/" + relative, path))
        directory.mkdir(parents=True, exist_ok=True)
        key = self.key(url) if url else ""
        if entry:
            record = {"schema": "msdial-download-store-object.v1", "object_id": object_id, "state": state,
                      "name": name, "size_bytes": len(body), "sha256": sha256, "md5": "c" * 32,
                      "urls": [{"url": url, "url_key": key, "name": name}] if url else []}
            record.update(extra)
            _write(directory / "entry.json", record)
        lines = ["path\tsize\tcrc\tmtime_ns"] + [
            f"{relative}\t{path.stat().st_size}\t\t{path.stat().st_mtime_ns}" for relative, path in rows]
        (directory / "members.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if url and index:
            _write(self.root / "index" / f"{key}.json", {"schema": "msdial-download-store-index.v1", "url": url,
                                                          "url_key": key, "object_id": object_id, "history": []})
        for unit, claim_state in (claims or {}).items():
            self.claim(url, unit, claim_state, object_id=object_id)
        return directory

    def claim(self, url: str, unit: str, state: str, *, object_id: str | None = None,
              claimed_at: str = "2026-10-01T00:00:00+00:00", history: list | None = None) -> Path:
        key = self.key(url)
        path = self.root / "claims" / key / f"{unit}.json"
        record = {"schema": "msdial-download-store-claim.v1", "url": url, "url_key": key, "unit_id": unit,
                  "state": state, "object_id": object_id, "claimed_at": claimed_at, "history": history or []}
        if state == "released":
            record.update(release_reason="raw_cleaned", released_at="2026-10-01T06:00:00+00:00")
        _write(path, record)
        return path


class InodeAccountingTests(unittest.TestCase):
    def test_a_file_record_with_two_names_is_counted_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary), raw_retention_policy="keep")
            data = workspace / "raw" / "data"
            (data / "big.mzML").write_bytes(b"y" * 1000)
            os.link(data / "big.mzML", data / "same.mzML")
            check = _check(verifier.verify(workspace, "before-publish"), "DSK-1")

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(1010, check.evidence["total_bytes"])
        self.assertEqual(2010, check.evidence["logical_bytes"])
        self.assertEqual(0, check.evidence["linked_from_store_bytes"])

    def test_the_bytes_linked_from_the_store_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = _unit(root, raw_retention_policy="keep")
            directory = StoreBuilder(root).object("d" * 64, "study.zip", url="https://x/study.zip",
                                                  tree={"a.mzML": b"a" * 300, "b.mzML": b"b" * 700},
                                                  claims={"unit": "materialized"})
            data = workspace / "raw" / "data"
            for name in ("a.mzML", "b.mzML"):
                os.link(directory / "t" / name, data / name)
            check = _check(verifier.verify(workspace, "before-publish"), "DSK-1")

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(1010, check.evidence["total_bytes"])
        self.assertEqual(1000, check.evidence["linked_from_store_bytes"])
        self.assertEqual(2, check.evidence["linked_from_store_files"])
        self.assertEqual(10, check.evidence["unit_only_bytes"])
        self.assertIn("linked from the accession store", check.detail)

    def test_an_archive_and_its_extraction_still_warn_counted_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = _unit(Path(temporary), raw_retention_policy="keep")
            downloads = workspace / "raw" / "downloads"
            downloads.mkdir()
            (downloads / "study.zip").write_bytes(b"z" * 500)
            check = _check(verifier.verify(workspace, "before-publish"), "DSK-1")

        self.assertEqual(verifier.WARN, check.status)
        self.assertEqual(510, check.evidence["total_bytes"])


# ---------------------------------------------------------------------------------------------
# scripts/verify-download-store.py
# ---------------------------------------------------------------------------------------------

def _run_store_checker(path: Path) -> tuple[int, dict]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = store_checker.main([str(path), "--json"])
    return code, (json.loads(out.getvalue()) if out.getvalue() else {})


def _kinds(result: dict, severity: str | None = None) -> list[str]:
    return [item["kind"] for store in result.get("stores", []) for item in store["findings"]
            if severity is None or item["severity"] == severity]


class DownloadStoreCheckerTests(unittest.TestCase):
    def test_a_store_every_object_of_which_is_accounted_for_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _unit(root, "unit-a", status="mztab_validated")
            builder = StoreBuilder(root)
            builder.object("1" * 64, "a.zip", url="https://x/a.zip", tree={"a.mzML": b"a"},
                           claims={"unit-a": "materialized"})
            builder.object("2" * 64, "b.zip", url="https://x/b.zip", claims={"unit-b": "pending"})
            code, result = _run_store_checker(root)

        self.assertEqual(0, code, result)
        self.assertEqual([], _kinds(result, "fail"))
        self.assertEqual(["pre_claimed"], _kinds(result, "info"))

    def test_an_orphan_object_is_flagged(self) -> None:
        for name, kwargs in (("no record", {"entry": False, "url": "https://x/o.zip"}),
                             ("nothing reaches it", {"url": "https://x/o.zip", "index": False})):
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                StoreBuilder(Path(temporary)).object("3" * 64, "o.zip", **kwargs)
                code, result = _run_store_checker(Path(temporary))

            self.assertEqual(2, code)
            self.assertIn("orphan_object", _kinds(result, "fail"))

    def test_a_live_claim_on_a_terminal_unit_is_flagged(self) -> None:
        for status in ("raw_cleaned", "discarded"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _unit(root, "unit-a", raw=False, status=status)
                StoreBuilder(root).object("4" * 64, "a.zip", url="https://x/a.zip", claims={"unit-a": "materialized"})
                code, result = _run_store_checker(root)

            self.assertEqual(2, code)
            self.assertEqual(["live_claim_on_terminal_unit"], _kinds(result, "fail"))

    def test_a_claim_opened_after_the_deletion_is_a_new_consumer(self) -> None:
        """download_store.claim reopens a released claim, as a batch pre-claim for a re-run of the unit does."""
        released = [{"state": "released", "object_id": "4" * 16, "release_reason": "raw_cleaned",
                     "released_at": "2026-09-01T03:00:00+00:00"}]
        for status in ("raw_cleaned", "discarded"):
            for name, claimed_at in (("claimed after", "2026-09-02T00:00:00+00:00"),
                                     ("dated by the release it reopened", "")):
                with self.subTest(status=status, record=name), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    _unit(root, "unit-a", raw=False, status=status, **{f"{status}_at": "2026-09-01T02:00:00+00:00"})
                    builder = StoreBuilder(root)
                    builder.object("4" * 64, "a.zip", url="https://x/a.zip")
                    builder.claim("https://x/a.zip", "unit-a", "pending", claimed_at=claimed_at, history=released)
                    code, result = _run_store_checker(root)

                self.assertEqual(0, code, result)
                self.assertEqual(["claimed_after_release"], _kinds(result, "info"))

    def test_a_claim_opened_before_the_deletion_is_left_over(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _unit(root, "unit-a", raw=False, status="raw_cleaned", raw_cleaned_at="2026-09-01T02:00:00+00:00")
            builder = StoreBuilder(root)
            builder.object("4" * 64, "a.zip", url="https://x/a.zip")
            builder.claim("https://x/a.zip", "unit-a", "materialized", object_id="4" * 16,
                          claimed_at="2026-09-01T00:00:00+00:00")
            code, result = _run_store_checker(root)

        self.assertEqual(2, code)
        self.assertEqual(["live_claim_on_terminal_unit"], _kinds(result, "fail"))

    def test_a_released_split_parent_releases_its_claims_too(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _unit(root, "unit", raw=False, status="split_by_acquisition",
                  raw_release={"state": "deleted", "parts": []})
            StoreBuilder(root).object("4" * 64, "a.zip", url="https://x/a.zip", claims={"unit": "pending"})
            code, result = _run_store_checker(root)

        self.assertEqual(2, code)
        self.assertEqual(["live_claim_on_terminal_unit"], _kinds(result, "fail"))

    def test_an_object_no_unit_ever_claimed_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            StoreBuilder(Path(temporary)).object("5" * 64, "a.zip", url="https://x/a.zip")
            code, result = _run_store_checker(Path(temporary))

        self.assertEqual(2, code)
        self.assertEqual(["no_releasing_unit"], _kinds(result, "fail"))

    def test_a_released_object_is_left_for_gc_without_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _unit(root, "unit-a", raw=False, status="raw_cleaned")
            StoreBuilder(root).object("6" * 64, "a.zip", url="https://x/a.zip", claims={"unit-a": "released"})
            code, result = _run_store_checker(root)

        self.assertEqual(0, code, result)
        self.assertEqual([], _kinds(result, "fail"))

    def test_tombstones_are_listed_and_a_false_one_is_refused(self) -> None:
        under = {"approval_id": APPROVAL, "boundary": 5, "units": [{"unit_id": "unit-a"}]}
        with tempfile.TemporaryDirectory() as temporary:
            builder = StoreBuilder(Path(temporary))
            builder.object("7" * 64, "a.zip", url="https://x/a.zip", state="collected", collected_under=under,
                           collected_at="2026-10-01T07:00:00+00:00",
                           release_record=[{"unit_id": "unit-a", "state": "released"}])
            code, result = _run_store_checker(Path(temporary))
        self.assertEqual(0, code, result)
        tombstones = result["stores"][0]["tombstones"]
        self.assertEqual([APPROVAL], [item["approval_id"] for item in tombstones])
        self.assertEqual(["unit-a"], tombstones[0]["released_by"])

        with tempfile.TemporaryDirectory() as temporary:
            builder = StoreBuilder(Path(temporary))
            directory = builder.object("8" * 64, "a.zip", url="https://x/a.zip", state="collected", collected_under={})
            (directory / "obj").mkdir()
            (directory / "obj" / "a.zip").write_bytes(b"left behind")
            code, result = _run_store_checker(Path(temporary))
        self.assertEqual(2, code)
        self.assertEqual({"tombstone_with_bytes", "tombstone_without_approval"}, set(_kinds(result, "fail")))

    def test_an_abandoned_partial_and_a_lapsed_lock_are_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            builder = StoreBuilder(Path(temporary))
            key = builder.key("https://x/p.zip")
            (builder.root / "partial").mkdir()
            (builder.root / "partial" / f"{key}.part").write_bytes(b"half")
            builder.claim("https://x/p.zip", "unit-a", "released")
            (builder.root / "locks").mkdir()
            lock = builder.root / "locks" / f"u-{key}.lock"
            lock.write_text("{}", encoding="utf-8")
            old = time.time() - 3600
            os.utime(lock, (old, old))
            code, result = _run_store_checker(Path(temporary))

        self.assertEqual(0, code, result)
        self.assertEqual({"abandoned_partial", "stale_lock"}, set(_kinds(result, "warn")))

    def test_a_linked_file_written_in_place_is_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _unit(root, "unit-a", status="mztab_validated")
            directory = StoreBuilder(root).object("9" * 64, "a.zip", url="https://x/a.zip", tree={"a.mzML": b"a"},
                                                  claims={"unit-a": "materialized"})
            (directory / "t" / "a.mzML").write_bytes(b"rewritten by a reader")
            code, result = _run_store_checker(root)

        self.assertEqual(0, code, result)
        self.assertEqual(["modified_in_place"], _kinds(result, "warn"))

    def test_a_workspace_root_is_searched_and_no_store_is_exit_3(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accession = root / "metabolights" / "MTBLS1"
            StoreBuilder(accession).object("5" * 64, "a.zip", url="https://x/a.zip")
            code, result = _run_store_checker(root)
            self.assertEqual(2, code)
            self.assertEqual(1, len(result["stores"]))
            empty = root / "empty"
            empty.mkdir()
            code, _ = _run_store_checker(empty)
        self.assertEqual(3, code)

    def test_the_checker_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _unit(root, "unit-a", raw=False, status="raw_cleaned")
            StoreBuilder(root).object("4" * 64, "a.zip", url="https://x/a.zip", claims={"unit-a": "materialized"})
            before = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in root.rglob("*")}
            _run_store_checker(root)
            after = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in root.rglob("*")}

        self.assertEqual(before, after)


class StoreCheckerWhileACampaignRunsTests(unittest.TestCase):
    """The checker is run beside a campaign: by a runner with stdout piped, and while the store works."""

    def test_a_name_the_console_code_page_cannot_encode_still_gets_a_verdict(self) -> None:
        """A piped stdout takes the code page: cp932 on a Japanese Windows has no e-acute."""
        name = "donn\u00e9es_\u690d\u7269_\u03a9.zip"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            StoreBuilder(root).object("1" * 64, name, url=f"https://x/{name}")  # no claim: a FAIL naming it
            env = dict(os.environ, PYTHONIOENCODING="cp932", PYTHONDONTWRITEBYTECODE="1")
            env.pop("PYTHONUTF8", None)
            runs = {mode: subprocess.run([sys.executable, str(TESTS.parent / "scripts" / "verify-download-store.py"),
                                          str(root), *([mode] if mode else [])], capture_output=True, env=env)
                    for mode in ("", "--json")}

        for mode, run in runs.items():
            self.assertEqual(2, run.returncode, f"{mode or 'text'}: {run.stderr.decode('cp932', 'replace')[-400:]}")
        self.assertIn(b"donn\\xe9es_", runs[""].stdout)
        result = json.loads(runs["--json"].stdout.decode("ascii"))
        self.assertEqual(["no_releasing_unit"], _kinds(result, "fail"))
        self.assertIn(name, result["stores"][0]["findings"][0]["detail"])

    def test_files_that_go_between_the_listing_and_the_stat_are_not_a_crash(self) -> None:
        """A c-<key> lock comes and goes around every claim write; gc unlinks trees; installs drop partials."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder = StoreBuilder(root)
            directory = builder.object("2" * 64, "a.zip", url="https://x/a.zip", tree={"a.mzML": b"a"},
                                       claims={"unit-a": "released"})
            for name in ("locks", "partial"):
                (builder.root / name).mkdir()
            churned = [builder.root / "locks" / f"c-{index:04d}.lock" for index in range(20)] \
                + [builder.root / "partial" / f"{index:024d}.part" for index in range(10)] \
                + [directory / "t" / f"extra-{index}.mzML" for index in range(10)]
            stop = threading.Event()

            def churn(paths: list[Path]) -> None:
                while not stop.is_set():
                    for path in paths:
                        with contextlib.suppress(OSError):
                            path.write_bytes(b"{}")
                            path.unlink()

            threads = [threading.Thread(target=churn, args=(churned[index::4],)) for index in range(4)]
            for thread in threads:
                thread.start()
            errors = []
            try:
                for _ in range(150):
                    try:
                        store_checker.StoreCheck(builder.root).run()
                    except Exception as error:  # noqa: BLE001 - the crash is what is tested for
                        errors.append(f"{type(error).__name__}: {error}")
            finally:
                stop.set()
                for thread in threads:
                    thread.join()

        self.assertEqual([], errors[:3])

    def test_the_gates_store_walk_survives_a_file_gc_removes(self) -> None:
        """DSK-1 walks the store's object trees for the records a unit links to, while gc may unlink them."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = _unit(root, raw_retention_policy="keep")
            directory = StoreBuilder(root).object("3" * 64, "study.zip", url="https://x/study.zip",
                                                  tree={"a.mzML": b"a" * 300}, claims={"unit": "materialized"})
            os.link(directory / "t" / "a.mzML", workspace / "raw" / "data" / "a.mzML")
            gone = directory / "t" / "gone.mzML"
            real_is_file = Path.is_file

            def listed_then_gone(self: Path) -> bool:
                # gone.mzML is listed and reported a file, then unlinked before its stat: gc at that instant.
                return True if self == gone else real_is_file(self)

            real_rglob = Path.rglob

            def rglob(self: Path, pattern: str):
                yield from real_rglob(self, pattern)
                if self == directory / "t":
                    yield gone

            with mock.patch.object(Path, "is_file", listed_then_gone), mock.patch.object(Path, "rglob", rglob):
                check = _check(verifier.verify(workspace, "before-publish"), "DSK-1")

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(300, check.evidence["linked_from_store_bytes"])

    def _lock(self, builder: StoreBuilder, name: str, *, age: float = 0.0) -> None:
        (builder.root / "locks").mkdir(exist_ok=True)
        path = builder.root / "locks" / f"{name}.lock"
        path.write_text("{}", encoding="utf-8")
        if age:
            os.utime(path, (time.time() - age, time.time() - age))

    def test_bytes_an_install_has_placed_and_not_yet_recorded_are_busy_not_orphaned(self) -> None:
        """_install places obj/<name> and then writes entry.json, both under o-<id>."""
        for name, age, kinds in (("fresh lock", 0.0, ["object_busy"]), ("lapsed lock", 3600.0, ["orphan_object"])):
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                builder = StoreBuilder(Path(temporary))
                builder.object("4" * 64, "o.zip", url="https://x/o.zip", entry=False)
                self._lock(builder, "o-" + "4" * 16, age=age)
                code, result = _run_store_checker(Path(temporary))

            self.assertEqual(kinds, _kinds(result, "info" if age == 0.0 else "fail"))
            self.assertEqual(0 if age == 0.0 else 2, code, result)

    def test_an_object_whose_url_is_being_pointed_at_it_is_busy_not_orphaned(self) -> None:
        """A fetch installs the object, then points the URL's index and records the claim, under u-<key>."""
        with tempfile.TemporaryDirectory() as temporary:
            builder = StoreBuilder(Path(temporary))
            builder.object("5" * 64, "o.zip", url="https://x/o.zip", index=False)
            self._lock(builder, "u-" + builder.key("https://x/o.zip"))
            code, result = _run_store_checker(Path(temporary))

        self.assertEqual(0, code, result)
        self.assertEqual(["object_busy"], _kinds(result, "info"))

    def test_records_written_while_the_object_was_checked_are_read_again(self) -> None:
        """The fetch finished between the checker's reading of the index and its reading of the object."""
        with tempfile.TemporaryDirectory() as temporary:
            builder = StoreBuilder(Path(temporary))
            url = "https://x/o.zip"
            builder.object("6" * 64, "o.zip", url=url, index=False)

            class FetchEndsNow(store_checker.StoreCheck):
                def _held(self, names):
                    # The lock is free by the time it is looked at, and the fetch's records are written.
                    _write(builder.root / "index" / f"{builder.key(url)}.json",
                           {"schema": "msdial-download-store-index.v1", "url": url, "url_key": builder.key(url),
                            "object_id": "6" * 16, "history": []})
                    builder.claim(url, "unit-a", "materialized", object_id="6" * 16)
                    return ""

            result = FetchEndsNow(builder.root).run()

        self.assertEqual([], [item["kind"] for item in result["findings"] if item["severity"] == "fail"])
        self.assertEqual(["object_busy"], [item["kind"] for item in result["findings"] if item["severity"] == "info"])

    def test_a_claim_whose_release_is_under_way_is_busy_not_left_over(self) -> None:
        """The deletion is recorded first, and then each of the unit's claims is released under c-<key>."""
        url = "https://x/a.zip"
        long_ago = "2026-09-01T02:00:00+00:00"
        just_now = datetime.now(timezone.utc).isoformat()
        for name, deleted_at, lock_age, severity in (("its c-<key> lock fresh", long_ago, 0.0, "info"),
                                                     ("deleted seconds ago", just_now, None, "info"),
                                                     ("long deleted, the lock lapsed", long_ago, 3600.0, "fail")):
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _unit(root, "unit-a", raw=False, status="raw_cleaned", raw_cleaned_at=deleted_at)
                builder = StoreBuilder(root)
                builder.object("8" * 64, "a.zip", url=url)
                builder.claim(url, "unit-a", "materialized", object_id="8" * 16, claimed_at="2026-09-01T00:00:00+00:00")
                if lock_age is not None:
                    self._lock(builder, "c-" + builder.key(url), age=lock_age)
                code, result = _run_store_checker(root)

            if severity == "info":
                self.assertEqual(0, code, result)
                self.assertEqual(["object_busy"], _kinds(result, "info"))
                busy = next(item for item in result["stores"][0]["findings"] if item["kind"] == "object_busy")
                self.assertEqual("live_claim_on_terminal_unit", busy["deferred"])
            else:
                self.assertEqual(2, code, result)
                self.assertIn("live_claim_on_terminal_unit", _kinds(result, "fail"))

    def test_a_claim_released_while_it_was_checked_is_busy(self) -> None:
        url = "https://x/a.zip"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _unit(root, "unit-a", raw=False, status="raw_cleaned", raw_cleaned_at="2026-09-01T02:00:00+00:00")
            builder = StoreBuilder(root)
            builder.object("9" * 64, "a.zip", url=url)
            builder.claim(url, "unit-a", "materialized", object_id="9" * 16, claimed_at="2026-09-01T00:00:00+00:00")

            class ReleaseEndsNow(store_checker.StoreCheck):
                def _held(self, names):
                    # The release wrote the claim between the checker's first reading of it and the lock's.
                    builder.claim(url, "unit-a", "released", object_id="9" * 16)
                    return ""

            result = ReleaseEndsNow(builder.root).run()

        self.assertEqual([], [item["kind"] for item in result["findings"] if item["severity"] == "fail"])
        self.assertEqual(["object_busy"], [item["kind"] for item in result["findings"] if item["severity"] == "info"])

    def test_a_tombstone_installed_again_is_busy_while_its_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            builder = StoreBuilder(Path(temporary))
            directory = builder.object("7" * 64, "a.zip", url="https://x/a.zip", state="collected",
                                       collected_under={"approval_id": APPROVAL, "boundary": 5})
            (directory / "obj").mkdir()
            (directory / "obj" / "a.zip").write_bytes(b"installed again")
            self._lock(builder, "o-" + "7" * 16)
            code, result = _run_store_checker(Path(temporary))

        self.assertEqual(0, code, result)
        self.assertEqual(["object_busy"], _kinds(result, "info"))


if __name__ == "__main__":
    unittest.main()
