"""SUM-1 and SUM-2 on a split part: its own inputs, through its own lineage rows, on its raw owner's evidence.

A part downloads nothing, so SUM-1 read everything on its raw owner, the parent: the downloads and archive records that
vouch for the bytes, and also the parent's input candidates and lineage rows. A part's own inputs were never resolved.
That passed unnoticed while a part's rows were copies of its parent's; it stopped being harmless with the .wiff2 to .wiff
switch of 2026-10-31 (Interactive 0.5.55, msdial-interactive-app#90), after which a switched part runs on .wiff files
whose own rows (kind extracted_member, the archive as source, the member's own declared checksum) nothing read, while
SUM-1 passed on the parent's .wiff2 rows. The gate now resolves the part's own candidates through the part's own rows,
with the owner's downloads, extractions, listings, declared checksums and validator counts as the evidence
(_checksum_subject). A part written before Interactive copied rows into it keeps the owner's reading.

The fixture is test_verify_input_lineage's LineageUnit, a unit's manifest as Interactive writes input_lineage.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import test_verify_input_lineage as lineage  # noqa: E402  (loads the gate as verify_run_invariants_lineage)

verifier = lineage.verifier
_check = lineage._check
_claim = lineage._claim

STEMS = ("241202_S1_neg", "241202_S2_neg")
FOLDER = "MB-POST_files_MPST.0"
MD5_B = "fedcba9876543210fedcba9876543210"


class SplitUnit:
    """A parent whose project archive held each sample's .wiff2, .wiff and .wiff.scan, and one part split from it.

    The parent's inputs are the .wiff2, as the one encoding rule takes them, each row claiming the member's own
    published MD5. The part is written by part(): its candidates and rows are whatever the test gives it.
    """

    def __init__(self, temporary: str, *, repository: str = "mb_post", tar_verified: bool = False) -> None:
        self.root = Path(temporary)
        self.unit = lineage.LineageUnit(temporary, repository=repository)
        members = {}
        for stem in STEMS:
            for suffix in (".wiff2", ".wiff", ".wiff.scan"):
                members[f"{FOLDER}/{stem}{suffix}"] = f"{stem}{suffix}".encode()
        self.md5s = {member: hashlib.md5(data).hexdigest() for member, data in members.items()}
        # MB-POST's tar carries a declared value that was never compared with the tar; a Workbench archive's was.
        self.download = self.unit.archive("MPST.tar", members, md5=lineage.MD5, verified=tar_verified)
        files = self.unit.manifest["project"]["files"]
        if not tar_verified:
            files[0]["checksum"] = ""
        files += [{"name": member.split("/", 1)[1], "checksum": md5} for member, md5 in self.md5s.items()]
        self.unit.validated()
        for stem in STEMS:
            member = f"{FOLDER}/{stem}.wiff2"
            self.unit.extracted_input(member, self.download, _claim(self.md5s[member]))
        self.parent = self.unit.write() / "provenance" / "run-manifest.json"
        self.part_root = self.root / "unit-dda-neg"

    def path(self, stem: str, suffix: str) -> str:
        return str(self.unit.data / FOLDER / f"{stem}{suffix}")

    def row(self, stem: str, suffix: str, checksums: dict | None = None) -> dict:
        """An extracted_member row of the part's own, as Interactive's lineage helper writes it for a .wiff."""
        member = f"{FOLDER}/{stem}{suffix}"
        return {"path": self.path(stem, suffix), "kind": "extracted_member", "declared_names": [f"{stem}{suffix}"],
                "sample_id": stem, "file_name": stem,
                "source": {"archive": lineage._archive_source(self.download), "member": member},
                "checksums": _claim(self.md5s[member]) if checksums is None else checksums}

    def part(self, candidates: list[str], rows: list[dict] | None) -> Path:
        manifest = {
            "project": {"analysis_unit_id": "unit-dda-neg"},
            "input_candidates": candidates,
            "input_directory": str(self.unit.data),
            "split_from": {"manifest_path": str(self.parent), "analysis_unit_id": "unit"},
            "raw_owned_by": str(self.parent),
        }
        if rows is not None:
            manifest["input_lineage"] = {"schema": lineage.LINEAGE_SCHEMA, "rows": rows,
                                         "inherited_from": str(self.parent)}
        (self.part_root / "output").mkdir(parents=True, exist_ok=True)
        (self.part_root / "provenance").mkdir(exist_ok=True)
        (self.part_root / "provenance" / "run-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return self.part_root

    def switched(self, **checksums_by_stem: dict) -> Path:
        """The part after the switch: each sample's .wiff its input, with a row of its own."""
        return self.part([self.path(stem, ".wiff") for stem in STEMS],
                         [self.row(stem, ".wiff", checksums_by_stem.get(stem)) for stem in STEMS])

    def sum1(self, workspace: Path):
        return _check(verifier.verify(workspace, "before-production"), "SUM-1")


class APartsOwnInputs(unittest.TestCase):
    def test_a_switched_parts_wiff_rows_are_confirmed_by_its_owners_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            split = SplitUnit(temporary)
            check = split.sum1(split.switched())

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual("own", check.evidence["lineage_rows_of"])
        self.assertEqual("unit", check.evidence["inherited_from"])
        self.assertEqual(len(STEMS), check.evidence["inputs"])
        self.assertEqual({"verified": len(STEMS)}, check.evidence["inputs_by_basis"])

    def test_a_wiff_row_claiming_a_checksum_the_owner_does_not_declare_is_refused(self) -> None:
        """The parent's .wiff2 rows pass; they no longer stand for the part's .wiff."""
        with tempfile.TemporaryDirectory() as temporary:
            split = SplitUnit(temporary)
            parent = split.sum1(split.unit.root)
            check = split.sum1(split.switched(**{STEMS[0]: _claim(MD5_B)}))

        self.assertEqual(verifier.PASS, parent.status, parent.detail)
        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"declared_checksum_unknown": 1}, check.evidence["uncovered_reasons"])
        self.assertEqual(f"{STEMS[0]}.wiff", check.evidence["uncovered"][0]["input"])

    def test_a_wiff_row_resting_on_the_unverified_tar_is_refused(self) -> None:
        """No claim of its own: the row falls to MB-POST's tar, whose declared value was never compared with it."""
        with tempfile.TemporaryDirectory() as temporary:
            split = SplitUnit(temporary)
            check = split.sum1(split.switched(**{STEMS[1]: {}}))

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"archive_md5_unverified": 1}, check.evidence["uncovered_reasons"])
        self.assertEqual(f"{STEMS[1]}.wiff", check.evidence["uncovered"][0]["input"])

    def test_a_candidate_the_parts_rows_omit_is_not_covered_by_the_parents_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            split = SplitUnit(temporary)
            workspace = split.part([split.path(STEMS[0], ".wiff"), split.path(STEMS[1], ".wiff2")],
                                   [split.row(STEMS[0], ".wiff")])
            check = split.sum1(workspace)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"no_lineage_row": 1}, check.evidence["uncovered_reasons"])
        self.assertEqual(f"{STEMS[1]}.wiff2", check.evidence["uncovered"][0]["input"])

    def test_an_older_part_without_rows_of_its_own_keeps_its_owners_reading(self) -> None:
        """Absent, or carrying only inherited_from: the owner's candidates and rows, as before."""
        for lineage_record in (None, {"inherited_from": "parent"}, {"schema": lineage.LINEAGE_SCHEMA, "rows": []}):
            with self.subTest(input_lineage=lineage_record), tempfile.TemporaryDirectory() as temporary:
                split = SplitUnit(temporary)
                workspace = split.part([split.path(STEMS[0], ".wiff2")], None)
                manifest_path = workspace / "provenance" / "run-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if lineage_record is not None:
                    manifest["input_lineage"] = lineage_record
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                check = split.sum1(workspace)

                self.assertEqual(verifier.PASS, check.status, check.detail)
                self.assertEqual("raw_owner", check.evidence["lineage_rows_of"])
                self.assertEqual(len(STEMS), check.evidence["inputs"], "the owner's candidates, as before")


class SUM2OnAPart(unittest.TestCase):
    def test_sum2_judges_the_artifacts_by_the_parts_own_basis(self) -> None:
        """The parent's .wiff2 rest on their own MD5; the part's .wiff only on the archive's, and may not be called
        checksum-verified."""
        with tempfile.TemporaryDirectory() as temporary:
            split = SplitUnit(temporary, repository="metabolomics_workbench", tar_verified=True)
            workspace = split.switched(**{stem: {} for stem in STEMS})
            sum1 = split.sum1(workspace)
            (workspace / "output" / "MS_DIAL_Materials_and_Methods.txt").write_text(
                "All raw files were checksum-verified.", encoding="utf-8")
            sum2 = _check(verifier.verify(workspace, "before-publish"), "SUM-2")

        self.assertEqual(verifier.ARCHIVE_VERIFIED_STATUS, sum1.status, sum1.detail)
        self.assertEqual("archive_verified", sum1.evidence["basis"])
        self.assertEqual(verifier.FAIL, sum2.status, sum2.detail)


if __name__ == "__main__":
    unittest.main()
