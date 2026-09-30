"""SUM-1 read through the input lineage, and SUM-2's wording for inputs that came out of an archive.

661 of the 839 declared-pool units are Metabolomics Workbench units, and each one's only published
checksum is the MD5 of the archive its raw files are packed in. SUM-1 had no clause that let a
verified archive vouch for what came out of it, so every one of them was refused. Interactive now
writes an input_lineage table (msdial-input-lineage.v1), one row per input, and the staged lease an
archive_extractions record with the archive's member listing; SUM-1 resolves each input through
them. The fixtures below are built the way Interactive writes those records: build_input_lineage and
_archive_source in repository_reanalysis.py, extract_archive's record and listing in archives.py, and
convert_mzxml_to_mzml's record in mzxml_conversion.py.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _ROOT / "scripts" / "verify-run-invariants.py"
_SPEC = importlib.util.spec_from_file_location("verify_run_invariants_lineage", _MODULE_PATH)
assert _SPEC and _SPEC.loader
verifier = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_run_invariants_lineage"] = verifier
_SPEC.loader.exec_module(verifier)

LISTING_COLUMNS = ("path", "type", "size", "crc32", "modified", "depth", "archive", "member_name", "disposition")
MD5 = "0123456789abcdef0123456789abcdef"
# What build_input_lineage writes, and the wording SUM-2 permits for inputs out of an archive.
LINEAGE_SCHEMA = "msdial-input-lineage.v1"
ARCHIVE_WORDING = "extracted from an archive whose published MD5 matched"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check(report, check_id: str):
    matching = [check for check in report.checks if check.check_id == check_id]
    assert len(matching) == 1, f"{check_id} ran {len(matching)} times"
    return matching[0]


def _archive_source(download: dict) -> dict:
    """What build_input_lineage records of the archive an input came out of."""
    return {
        "url": download.get("source_url", ""),
        "download_path": download.get("path", ""),
        "sha256": download.get("sha256", ""),
        "md5": download.get("md5", ""),
        "declared_checksum": download.get("declared_checksum", ""),
        "declared_checksum_verified": True if download.get("declared_checksum_verified") else None,
    }


class LineageUnit:
    """One unit's workspace, with a manifest carrying input_lineage as Interactive writes it."""

    def __init__(self, temporary: str, repository: str = "metabolomics_workbench") -> None:
        self.root = Path(temporary) / "unit"
        self.data = self.root / "raw" / "data"
        self.data.mkdir(parents=True)
        (self.root / "output").mkdir()
        self.provenance = self.root / "provenance"
        self.provenance.mkdir()
        self.manifest = {
            "schema": "msdial-public-reanalysis-run.v1",
            "project": {"analysis_unit_id": "unit", "repository": repository, "files": []},
            "workspace": str(self.root),
            "raw_directory": str(self.root / "raw"),
            "input_directory": str(self.data),
            "downloads": [],
            "extracted_files": [],
            "allowlist_checksum_validation": {"required": False, "verified": 0, "skipped": 0},
            "input_candidates": [],
            "input_lineage": {"schema": LINEAGE_SCHEMA, "rows": []},
            "execution_allowed": True,
        }

    # -- downloads -------------------------------------------------------------------------------

    def download(self, name: str, data: bytes, *, md5: str = MD5, verified: bool = True,
                 archive: bool = False) -> dict:
        folder = "downloads" if archive else "data"
        path = self.root / "raw" / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        download = {"path": str(path), "source_url": f"https://example.org/{name}", "size_bytes": len(data),
                    "sha256": _sha256(data), "md5": hashlib.md5(data).hexdigest(), "declared_checksum": md5}
        if md5 and verified:
            download["declared_checksum_verified"] = True
        self.manifest["downloads"].append(download)
        self.manifest["project"]["files"].append({"name": name, "checksum": md5})
        return download

    def validated(self) -> None:
        """What the allow-list validator records once it has compared every checksum the unit declares."""
        files = self.manifest["project"]["files"]
        declared = sum(1 for item in files if item.get("checksum"))
        self.manifest["allowlist_checksum_validation"] = {"required": declared > 0, "verified": declared,
                                                          "skipped": len(files) - declared}

    def archive(self, name: str, members: dict[str, bytes], *, md5: str = MD5, verified: bool = True,
                listed: dict[str, bytes] | None = None, rejected: list | None = None,
                extra_rows: list[str] | None = None) -> dict:
        """An archive downloaded, extracted into the data directory and recorded with its listing."""
        download = self.download(name, b"PK\x03\x04" + name.encode(), md5=md5, verified=verified, archive=True)
        for member, data in members.items():
            target = self.data / member
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        rows = ["\t".join(LISTING_COLUMNS)]
        for member, data in (members if listed is None else listed).items():
            rows.append("\t".join((member, "file", str(len(data)), "00000000", "", "1", name, member, "extracted")))
        rows.extend(extra_rows or [])
        listing = ("\n".join(rows) + "\n").encode("utf-8")
        tsv = self.provenance / f"archive-members-{download['sha256'][:12]}.tsv"
        tsv.write_bytes(listing)
        self.manifest.setdefault("archive_extractions", []).append({
            "schema": "msdial-archive-extraction.v1",
            "archive_name": name,
            "archive_path": download["path"],
            "archive_sha256": download["sha256"],
            "destination": str(self.data),
            "rejected_members": list(rejected or []),
            "crc_verified": True,
            "members_tsv": {"path": str(tsv), "sha256": _sha256(listing), "rows": len(rows) - 1},
            "nested": [],
            "nested_skipped": [],
        })
        return download

    # -- inputs ----------------------------------------------------------------------------------

    def _row(self, path: Path, kind: str, source: dict, checksums: dict | None = None) -> dict:
        row = {"path": str(path), "kind": kind, "declared_names": [], "sample_id": "", "file_name": "",
               "source": source, "checksums": checksums or {}}
        self.manifest["input_candidates"].append(str(path))
        self.manifest["input_lineage"]["rows"].append(row)
        return row

    def extracted_input(self, member: str, download: dict, checksums: dict | None = None) -> dict:
        path = self.data / member
        self.manifest["extracted_files"].append(str(path))
        return self._row(path, "extracted_member", {"archive": _archive_source(download), "member": member},
                         checksums)

    def container_input(self, folder: str, *downloads: dict) -> dict:
        return self._row(self.data / folder, "archived_container",
                         {"archives": [_archive_source(item) for item in downloads]})

    def file_input(self, download: dict) -> dict:
        checksums = {
            "sha256": download["sha256"], "md5": download["md5"], "declared": download["declared_checksum"],
            "declared_algorithm": "md5" if download.get("declared_checksum_verified") else "",
            "declared_verified": True if download.get("declared_checksum_verified") else None,
        }
        return self._row(Path(download["path"]), "file",
                         {"url": download["source_url"], "download_path": download["path"]}, checksums)

    def folder_input(self, folder: str, members: list[dict]) -> dict:
        return self._row(self.data / folder, "vendor_folder", {"objects": len(members)},
                         {"member_objects": len(members), "members_sha256": "ab" * 32})

    def converted_input(self, source: Path, *, source_sha256: str | None = None, status: str = "converted",
                        source_row: dict | None = None) -> tuple[dict, dict]:
        """source converted to raw/converted/<stem>.mzML, recorded as convert_mzxml_to_mzml records it."""
        output = self.root / "raw" / "converted" / (source.stem + ".mzML")
        output.parent.mkdir(parents=True, exist_ok=True)
        written = b"<mzML/>" + source.name.encode()
        output.write_bytes(written)
        read = source.read_bytes()
        record = {
            "schema": "msdial-mzxml-conversion.v1",
            "status": status,
            "error": None if status == "converted" else "ConversionError: truncated",
            "converter": {"name": "MS-DIAL Interactive mzXML to mzML converter", "version": "0.5.14"},
            "source": {"path": str(source), "relative_path": source.name, "name": source.name,
                       "bytes": len(read), "sha256": _sha256(read), "sha1": hashlib.sha1(read).hexdigest()},
            "output": {"path": str(output), "bytes": len(written), "sha256": _sha256(written)},
            "validation": {"status": "passed"},
        }
        conversions = self.manifest.setdefault("input_conversions",
                                               {"schema": "msdial-input-conversion.v1", "records": []})
        conversions["records"].append(record)
        reference = {"source_path": str(source),
                     "source_sha256": _sha256(read) if source_sha256 is None else source_sha256}
        if source_row is not None:
            reference["source_row"] = source_row
        row = self._row(output, "converted", {"conversion": reference}, {"sha256": _sha256(written)})
        return row, record

    # -- the gate --------------------------------------------------------------------------------

    def write(self) -> Path:
        (self.provenance / "run-manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        return self.root

    def gate(self, stage: str = "before-production"):
        return verifier.verify(self.write(), stage)

    def sum1(self):
        return _check(self.gate(), "SUM-1")

    def publish(self, text: str):
        (self.root / "output" / "MS_DIAL_Materials_and_Methods.txt").write_text(text, encoding="utf-8")
        return _check(self.gate("before-publish"), "SUM-2")


def _workbench_archive_unit(temporary: str, **archive_options) -> LineageUnit:
    """ST000001.zip holding two samples, both analysed: the shape of the 661 Workbench units."""
    unit = LineageUnit(temporary)
    members = {"ST000001/S1.mzML": b"one", "ST000001/S2.mzML": b"two"}
    download = unit.archive("ST000001.zip", members, **archive_options)
    for member in members:
        unit.extracted_input(member, download)
    return unit


# ---------------------------------------------------------------------------------------------
# The archive basis
# ---------------------------------------------------------------------------------------------

class ArchiveBasisTests(unittest.TestCase):
    def test_a_workbench_archive_unit_rests_on_the_archive_md5(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _workbench_archive_unit(temporary)
            check = unit.sum1()
            kind, _detail, _evidence = verifier._checksum_basis(unit.manifest)

        self.assertEqual("archive_verified", kind)
        self.assertEqual(verifier.WARN, check.status)
        self.assertEqual("archive_verified", check.evidence["basis"])
        self.assertEqual({"archive_verified": 2}, check.evidence["inputs_by_basis"])
        self.assertEqual(1, check.evidence["archives_verified"])
        self.assertIn(ARCHIVE_WORDING, check.detail)
        self.assertIn("no artifact may call them checksum-verified", check.detail)

    def test_the_archive_basis_is_a_warning_until_the_user_decides_it_passes(self) -> None:
        """Decision pending (2026-09-30). Either way the unit exits 0; the constant is the switch."""
        self.assertEqual(verifier.WARN, verifier.ARCHIVE_VERIFIED_STATUS)
        original = verifier.ARCHIVE_VERIFIED_STATUS
        verifier.ARCHIVE_VERIFIED_STATUS = verifier.PASS
        try:
            with tempfile.TemporaryDirectory() as temporary:
                check = _workbench_archive_unit(temporary).sum1()
        finally:
            verifier.ARCHIVE_VERIFIED_STATUS = original
        self.assertEqual(verifier.PASS, check.status)

    def test_an_unverified_archive_md5_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            check = _workbench_archive_unit(temporary, verified=False).sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"archive_md5_unverified": 2}, check.evidence["uncovered_reasons"])

    def test_an_archive_that_declares_no_checksum_is_a_lost_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            check = _workbench_archive_unit(temporary, md5="").sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"declaration_lost": 2}, check.evidence["uncovered_reasons"])

    def test_an_input_missing_from_the_listing_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            check = _workbench_archive_unit(temporary, listed={"ST000001/S1.mzML": b"one"}).sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual(1, check.evidence["inputs_uncovered"])
        self.assertEqual("missing_from_listing", check.evidence["uncovered"][0]["reason"])
        self.assertEqual("S2.mzML", check.evidence["uncovered"][0]["input"])

    def test_a_listed_name_that_was_not_extracted_does_not_count(self) -> None:
        """A nested archive expanded in place is listed as expanded_archive, not as a file on disk."""
        with tempfile.TemporaryDirectory() as temporary:
            extra = ["\t".join(("ST000001/S2.mzML", "file", "3", "0", "", "1", "ST000001.zip", "S2.mzML",
                                "dropped_metadata"))]
            check = _workbench_archive_unit(temporary, listed={"ST000001/S1.mzML": b"one"},
                                            extra_rows=extra).sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"missing_from_listing": 1}, check.evidence["uncovered_reasons"])

    def test_rejected_members_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            check = _workbench_archive_unit(
                temporary, rejected=[{"path": "../evil.txt", "reason": "traversal"}]).sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"members_rejected": 2}, check.evidence["uncovered_reasons"])

    def test_a_member_rejected_from_a_nested_archive_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _workbench_archive_unit(temporary)
            unit.manifest["archive_extractions"][0]["nested"] = [
                {"archive_name": "inner.zip", "rejected_members": [{"path": "CON", "reason": "reserved_name"}],
                 "nested": []}]
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"members_rejected": 2}, check.evidence["uncovered_reasons"])

    def test_a_listing_changed_after_its_sha256_was_recorded_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _workbench_archive_unit(temporary, listed={"ST000001/S1.mzML": b"one"})
            tsv = Path(unit.manifest["archive_extractions"][0]["members_tsv"]["path"])
            tsv.write_text(tsv.read_text(encoding="utf-8")
                           + "ST000001/S2.mzML\tfile\t3\t0\t\t1\tST000001.zip\tS2.mzML\textracted\n",
                           encoding="utf-8")
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"listing_unreadable": 2}, check.evidence["uncovered_reasons"])
        self.assertIn("has changed since its sha256 was recorded", check.detail)

    def test_an_absent_listing_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _workbench_archive_unit(temporary)
            Path(unit.manifest["archive_extractions"][0]["members_tsv"]["path"]).unlink()
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"listing_unreadable": 2}, check.evidence["uncovered_reasons"])

    def test_a_listing_is_read_in_the_units_provenance_when_the_workspace_moved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _workbench_archive_unit(temporary)
            members = unit.manifest["archive_extractions"][0]["members_tsv"]
            members["path"] = str(Path(temporary) / "elsewhere" / "provenance" / Path(members["path"]).name)
            check = unit.sum1()

        self.assertEqual(verifier.WARN, check.status)

    def test_an_archive_without_an_extraction_record_is_refused(self) -> None:
        """The published MD5 vouches for the archive; only its listing says what came out of it."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = _workbench_archive_unit(temporary)
            del unit.manifest["archive_extractions"]
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"no_extraction_record": 2}, check.evidence["uncovered_reasons"])

    def test_an_archive_the_downloads_do_not_record_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _workbench_archive_unit(temporary)
            for row in unit.manifest["input_lineage"]["rows"]:
                row["source"]["archive"].update(sha256="ef" * 32, download_path=str(unit.root / "other.zip"))
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"archive_not_downloaded": 2}, check.evidence["uncovered_reasons"])

    def test_a_member_whose_own_checksum_was_verified_needs_no_archive_listing(self) -> None:
        """MB-POST: the tar declares no checksum of its own; each member's checksum is compared."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary, repository="mb_post")
            download = unit.download("MPST000007.tar", b"tar", md5="", archive=True)
            unit.manifest["project"]["files"].append({"name": "a.lcd", "checksum": MD5})
            unit.validated()
            (unit.data / "a.lcd").write_bytes(b"a")
            unit.extracted_input("a.lcd", download, {"declared": MD5, "declared_algorithm": "md5",
                                                     "declared_verified": True})
            check = unit.sum1()

        self.assertEqual(verifier.PASS, check.status)
        self.assertEqual({"verified": 1}, check.evidence["inputs_by_basis"])

    def test_a_verified_checksum_the_repository_never_declared_is_refused(self) -> None:
        """A row's word that a checksum was verified is held against what the repository published."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary, repository="mb_post")
            download = unit.download("MPST000007.tar", b"tar", md5="", archive=True)
            (unit.data / "a.lcd").write_bytes(b"a")
            unit.extracted_input("a.lcd", download, {"declared": MD5, "declared_algorithm": "md5",
                                                     "declared_verified": True})
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"declared_checksum_unknown": 1}, check.evidence["uncovered_reasons"])

    def test_verified_files_beside_archive_members_rest_on_the_archive(self) -> None:
        """A unit rests on its weakest input."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = _workbench_archive_unit(temporary)
            unit.file_input(unit.download("S3.mzML", b"three"))
            check = unit.sum1()

        self.assertEqual(verifier.WARN, check.status)
        self.assertEqual({"archive_verified": 2, "verified": 1}, check.evidence["inputs_by_basis"])

    def test_a_container_folder_in_the_listing_is_covered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary)
            download = unit.archive("ST000002.zip", {"ST000002/S1.d/AcqData/MSScan.bin": b"scan"})
            unit.container_input("ST000002/S1.d", download)
            check = unit.sum1()

        self.assertEqual(verifier.WARN, check.status)
        self.assertEqual("archive_verified", check.evidence["basis"])

    def test_a_container_folder_the_listing_does_not_hold_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary)
            download = unit.archive("ST000002.zip", {"ST000002/S1.d/AcqData/MSScan.bin": b"scan"})
            (unit.data / "ST000002" / "S2.d").mkdir()
            unit.container_input("ST000002/S2.d", download)
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"missing_from_listing": 1}, check.evidence["uncovered_reasons"])

    def test_a_metabolights_archive_rests_on_its_download_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary, repository="metabolights")
            download = unit.archive("S1.raw.zip", {"S1.raw/_FUNC001.DAT": b"dat"}, md5="")
            unit.container_input("S1.raw", download)
            check = unit.sum1()

        self.assertEqual(verifier.WARN, check.status)
        self.assertEqual("download_sha256", check.evidence["basis"])

    def test_a_metabolights_archive_extracted_before_the_record_existed_keeps_the_legacy_reading(self) -> None:
        """Interactive 0.5.13 wrote input_lineage but extracted without an archive_extractions record."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary, repository="metabolights")
            download = unit.archive("study.zip", {"a.mzML": b"a"}, md5="")
            del unit.manifest["archive_extractions"]
            unit.extracted_input("a.mzML", download)
            check = unit.sum1()

        self.assertEqual(verifier.WARN, check.status)
        self.assertEqual("download_sha256", check.evidence["basis"])


# ---------------------------------------------------------------------------------------------
# Files, folders and the table itself
# ---------------------------------------------------------------------------------------------

class FileBasisTests(unittest.TestCase):
    def test_a_download_whose_md5_was_verified_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary)
            unit.file_input(unit.download("S1.mzML", b"one"))
            check = unit.sum1()

        self.assertEqual(verifier.PASS, check.status)
        self.assertEqual("verified", check.evidence["basis"])

    def test_a_download_whose_declared_checksum_was_not_verified_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary)
            unit.file_input(unit.download("S1.mzML", b"one", verified=False))
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"declared_checksum_unverified": 1}, check.evidence["uncovered_reasons"])

    def test_a_metabolights_file_rests_on_its_download_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary, repository="metabolights")
            unit.file_input(unit.download("S1.mzML", b"one", md5=""))
            check = unit.sum1()

        self.assertEqual(verifier.WARN, check.status)
        self.assertEqual("download_sha256", check.evidence["basis"])
        self.assertIn("No artifact may describe these inputs as checksum-verified", check.detail)

    def test_a_partial_declaration_is_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary, repository="metabolights")
            unit.file_input(unit.download("S1.mzML", b"one"))
            unit.file_input(unit.download("S2.mzML", b"two", md5=""))
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"partial_declaration": 1}, check.evidence["uncovered_reasons"])

    def test_a_lineage_sha256_the_download_record_does_not_give_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary, repository="metabolights")
            row = unit.file_input(unit.download("S1.mzML", b"one", md5=""))
            row["checksums"]["sha256"] = "ef" * 32
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"sha256_disagrees": 1}, check.evidence["uncovered_reasons"])

    def test_a_file_the_lease_did_not_download_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary, repository="metabolights")
            (unit.data / "old.mzML").write_bytes(b"old")
            unit._row(unit.data / "old.mzML", "file", {"origin": "not_downloaded_by_this_lease"})
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("the lease did not download it", check.detail)

    def test_a_vendor_folder_of_verified_downloads_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary)
            members = [unit.download(f"S1.raw/{name}", name.encode()) for name in ("_FUNC001.DAT", "_extern.inf")]
            unit.folder_input("S1.raw", members)
            check = unit.sum1()

        self.assertEqual(verifier.PASS, check.status)

    def test_a_vendor_folder_with_an_unverified_member_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary)
            members = [unit.download("S1.raw/_FUNC001.DAT", b"dat"),
                       unit.download("S1.raw/_extern.inf", b"inf", verified=False)]
            unit.folder_input("S1.raw", members)
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("_extern.inf", check.detail)

    def test_a_vendor_folder_whose_members_the_validator_verified_passes(self) -> None:
        """A sha256 is not compared at download, only by the allow-list validator, which counts it."""
        for verified, expected in ((2, verifier.PASS), (1, verifier.FAIL)):
            with self.subTest(verified=verified), tempfile.TemporaryDirectory() as temporary:
                unit = LineageUnit(temporary)
                members = [unit.download(f"S1.raw/{name}", name.encode(), md5=_sha256(name.encode()), verified=False)
                           for name in ("_FUNC001.DAT", "_extern.inf")]
                unit.manifest["allowlist_checksum_validation"] = {"required": True, "verified": verified,
                                                                  "skipped": 0}
                unit.folder_input("S1.raw", members)
                check = unit.sum1()

                self.assertEqual(expected, check.status)

    def test_a_vendor_folder_whose_row_counts_other_members_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary)
            members = [unit.download("S1.raw/_FUNC001.DAT", b"dat")]
            row = unit.folder_input("S1.raw", members)
            row["checksums"]["member_objects"] = 3
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"member_count_disagrees": 1}, check.evidence["uncovered_reasons"])

    def test_an_input_with_no_lineage_row_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _workbench_archive_unit(temporary)
            unit.manifest["input_candidates"].append(str(unit.data / "unlisted.mzML"))
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"no_lineage_row": 1}, check.evidence["uncovered_reasons"])

    def test_two_different_rows_for_one_input_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _workbench_archive_unit(temporary)
            rows = unit.manifest["input_lineage"]["rows"]
            rows.append({**rows[0], "kind": "file"})
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"ambiguous_lineage_row": 1}, check.evidence["uncovered_reasons"])

    def test_a_lineage_table_of_another_shape_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _workbench_archive_unit(temporary)
            unit.manifest["input_lineage"] = {"schema": "msdial-input-lineage.v0", "rows": []}
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn(LINEAGE_SCHEMA, check.detail)

    def test_a_lineage_kind_the_gate_does_not_read_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _workbench_archive_unit(temporary)
            unit.manifest["input_lineage"]["rows"][0]["kind"] = "symlink"
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"unknown_lineage_kind": 1}, check.evidence["uncovered_reasons"])

    def test_records_of_the_wrong_shape_are_refused_not_a_traceback(self) -> None:
        shapes = [
            ("rows", lambda unit: unit.manifest["input_lineage"]["rows"].extend([7, "row", None])),
            ("source", lambda unit: unit.manifest["input_lineage"]["rows"][0].update(source=["archive"])),
            ("checksums", lambda unit: unit.manifest["input_lineage"]["rows"][0].update(checksums="md5")),
            ("archives", lambda unit: unit.manifest["input_lineage"]["rows"][0].update(
                kind="archived_container", source={"archives": "ST000001.zip"})),
            ("extractions", lambda unit: unit.manifest.update(archive_extractions={"records": []})),
            ("listing", lambda unit: unit.manifest["archive_extractions"][0].update(members_tsv="listing.tsv")),
            ("nested", lambda unit: unit.manifest["archive_extractions"][0].update(nested=[None, 3])),
            ("conversions", lambda unit: unit.manifest.update(input_conversions="none")),
        ]
        for name, damage in shapes:
            with self.subTest(shape=name), tempfile.TemporaryDirectory() as temporary:
                unit = _workbench_archive_unit(temporary)
                damage(unit)
                check = unit.sum1()

                self.assertIn(check.status, (verifier.WARN, verifier.FAIL))

    def test_a_split_part_is_judged_by_its_parents_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _workbench_archive_unit(temporary)
            parent = unit.write() / "provenance" / "run-manifest.json"
            part = Path(temporary) / "unit-dda"
            (part / "output").mkdir(parents=True)
            (part / "provenance").mkdir()
            (part / "provenance" / "run-manifest.json").write_text(json.dumps({
                "project": {"analysis_unit_id": "unit-dda"},
                "input_candidates": unit.manifest["input_candidates"][:1],
                "split_from": {"manifest_path": str(parent), "analysis_unit_id": "unit"},
            }), encoding="utf-8")
            check = _check(verifier.verify(part, "before-production"), "SUM-1")

        self.assertEqual(verifier.WARN, check.status)
        self.assertEqual("unit", check.evidence["inherited_from"])
        self.assertEqual("archive_verified", check.evidence["basis"])


MD5_B = "fedcba9876543210fedcba9876543210"


def _claim(checksum: str, algorithm: str = "md5") -> dict:
    """A lineage row's word that its declared checksum was verified."""
    return {"declared": checksum, "declared_algorithm": algorithm, "declared_verified": True}


class RowClaimTests(unittest.TestCase):
    """A row's word that its checksum was verified holds only where the record that verified it says so."""

    def test_a_member_claim_the_validator_never_made_is_refused(self) -> None:
        """MB-POST with a validator that compared nothing: a member's claim is not borne out."""
        for claimed, reason in ((MD5, "declared_checksum_unknown"), (MD5_B, "declared_checksum_unaccounted")):
            with self.subTest(claimed=claimed), tempfile.TemporaryDirectory() as temporary:
                unit = LineageUnit(temporary, repository="mb_post")
                download = unit.download("MPST000007.tar", b"tar", md5="", archive=True)
                unit.manifest["project"]["files"] += [{"name": "a.lcd", "checksum": MD5},
                                                      {"name": "b.lcd", "checksum": MD5_B}]
                unit.manifest["allowlist_checksum_validation"] = {"required": False, "verified": 0, "skipped": 2}
                (unit.data / "b.lcd").write_bytes(b"b")
                # MD5 is a.lcd's checksum, not b.lcd's; MD5_B is b.lcd's, but nothing compared it.
                unit.extracted_input("b.lcd", download, _claim(claimed))
                check = unit.sum1()
                legacy = {key: value for key, value in unit.manifest.items() if key != "input_lineage"}

                self.assertEqual(verifier.FAIL, check.status)
                self.assertEqual({reason: 1}, check.evidence["uncovered_reasons"])
                # The legacy reading of the same unit refuses it too.
                self.assertEqual("insufficient", verifier._checksum_basis(legacy)[0])

    def test_a_member_claiming_its_archives_checksum_is_refused(self) -> None:
        """The archive's MD5 vouches for its members only through the archive basis, as a WARN."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = _workbench_archive_unit(temporary)
            for row in unit.manifest["input_lineage"]["rows"]:
                row["checksums"] = _claim(MD5)
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"archive_checksum_as_own": 2}, check.evidence["uncovered_reasons"])

    def test_an_mb_post_member_keeps_the_checksum_its_tar_carries(self) -> None:
        """MB-POST's tar download carries its first member's declared value, never compared with the tar."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary, repository="mb_post")
            download = unit.download("MPST000007.tar", b"tar", md5=MD5, verified=False, archive=True)
            unit.manifest["project"]["files"][0]["checksum"] = ""
            unit.manifest["project"]["files"].append({"name": "a.lcd", "checksum": MD5})
            unit.validated()
            (unit.data / "a.lcd").write_bytes(b"a")
            unit.extracted_input("a.lcd", download, _claim(MD5))
            check = unit.sum1()

        self.assertEqual(verifier.PASS, check.status)

    def test_a_file_row_claiming_verified_without_a_download_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _workbench_archive_unit(temporary)
            ghost = unit.data / "ghost.mzML"
            ghost.write_bytes(b"never downloaded")
            unit._row(ghost, "file", {"url": "", "download_path": str(ghost)}, {"sha256": "", **_claim(MD5)})
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"not_downloaded": 1}, check.evidence["uncovered_reasons"])

    def test_a_file_claim_its_download_did_not_make_is_refused(self) -> None:
        """The download compared nothing, and the validator's count does not say it compared this."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary)
            row = unit.file_input(unit.download("S1.mzML", b"one", verified=False))
            row["checksums"].update(_claim(MD5))
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"declared_checksum_unaccounted": 1}, check.evidence["uncovered_reasons"])

    def test_a_file_claim_to_another_checksum_than_its_download_verified_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary)
            row = unit.file_input(unit.download("S1.mzML", b"one"))
            row["checksums"].update(_claim(MD5_B))
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"declared_checksum_unknown": 1}, check.evidence["uncovered_reasons"])

    def test_a_file_whose_declared_sha256_the_validator_compared_passes(self) -> None:
        """The download compares only an MD5; a declared sha256 is compared by the validator."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary)
            declared = _sha256(b"one")
            row = unit.file_input(unit.download("S1.mzML", b"one", md5=declared, verified=False))
            unit.validated()
            row["checksums"].update(_claim(declared, "sha256"))
            check = unit.sum1()

        self.assertEqual(verifier.PASS, check.status)
        self.assertEqual("verified", check.evidence["basis"])

    def test_a_file_row_naming_another_download_is_refused(self) -> None:
        """S2 was never downloaded; its row borrows S1's verified download."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary)
            s1 = unit.download("S1.mzML", b"one")
            s2 = unit.data / "S2.mzML"
            s2.write_bytes(b"two, from an old workspace")
            unit._row(s2, "file", {"url": s1["source_url"], "download_path": s1["path"]}, {"sha256": s1["sha256"]})
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"download_path_mismatch": 1}, check.evidence["uncovered_reasons"])
        self.assertIn("S1.mzML", check.detail)


class LegacyManifestTests(unittest.TestCase):
    def test_a_manifest_without_the_table_is_read_as_before(self) -> None:
        """The same Workbench unit, written before input_lineage existed, is refused as it always was."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = _workbench_archive_unit(temporary)
            del unit.manifest["input_lineage"]
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertNotIn("lineage_schema", check.evidence)
        self.assertIn("inputs_without_download_sha256", check.evidence)


# ---------------------------------------------------------------------------------------------
# Conversion: the second link
# ---------------------------------------------------------------------------------------------

def _converted_workbench_unit(temporary: str, **archive_options) -> tuple[LineageUnit, dict, dict]:
    """ST000003.zip holding an mzXML, the only encoding of its sample, converted before analysis."""
    unit = LineageUnit(temporary)
    unit.archive("ST000003.zip", {"ST000003/S1.mzXML": b"<mzXML/>"}, **archive_options)
    row, record = unit.converted_input(unit.data / "ST000003" / "S1.mzXML")
    return unit, row, record


class ConversionLineageTests(unittest.TestCase):
    def test_an_mzxml_inside_a_workbench_archive_is_covered_through_both_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, _record = _converted_workbench_unit(temporary)
            check = unit.sum1()

        self.assertEqual(verifier.WARN, check.status)
        self.assertEqual("archive_verified", check.evidence["basis"])
        self.assertEqual(1, check.evidence["converted_inputs"])
        self.assertIn("converted from an mzXML", check.detail)

    def test_an_mzxml_the_archive_listing_does_not_hold_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, _record = _converted_workbench_unit(temporary, listed={"ST000003/other.mzXML": b"x"})
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"source_not_traced": 1}, check.evidence["uncovered_reasons"])

    def test_an_mzxml_from_an_archive_whose_md5_was_not_verified_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, _record = _converted_workbench_unit(temporary, verified=False)
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"archive_md5_unverified": 1}, check.evidence["uncovered_reasons"])

    def test_a_row_naming_other_source_bytes_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary)
            unit.archive("ST000003.zip", {"ST000003/S1.mzXML": b"<mzXML/>"})
            unit.converted_input(unit.data / "ST000003" / "S1.mzXML", source_sha256="ef" * 32)
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"source_sha256_mismatch": 1}, check.evidence["uncovered_reasons"])

    def test_a_conversion_that_read_another_size_than_the_listing_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, record = _converted_workbench_unit(temporary)
            record["source"]["bytes"] += 1
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"source_size_mismatch": 1}, check.evidence["uncovered_reasons"])

    def test_a_failed_conversion_covers_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary)
            unit.archive("ST000003.zip", {"ST000003/S1.mzXML": b"<mzXML/>"})
            unit.converted_input(unit.data / "ST000003" / "S1.mzXML", status="failed")
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"conversion_not_completed": 1}, check.evidence["uncovered_reasons"])

    def test_a_converted_input_with_no_conversion_record_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, _record = _converted_workbench_unit(temporary)
            unit.manifest["input_conversions"]["records"] = []
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"no_conversion_record": 1}, check.evidence["uncovered_reasons"])

    def test_a_converted_file_other_than_the_one_recorded_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, row, _record = _converted_workbench_unit(temporary)
            row["checksums"]["sha256"] = "ef" * 32
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"sha256_disagrees": 1}, check.evidence["uncovered_reasons"])

    def test_a_downloaded_mzxml_rests_on_its_own_verified_md5(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary)
            download = unit.download("S1.mzXML", b"<mzXML/>")
            unit.converted_input(Path(download["path"]))
            check = unit.sum1()

        self.assertEqual(verifier.PASS, check.status)
        self.assertEqual("verified", check.evidence["basis"])

    def test_a_conversion_of_other_bytes_than_were_downloaded_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary)
            download = unit.download("S1.mzXML", b"<mzXML/>")
            source = Path(download["path"])
            source.write_bytes(b"<mzXML>changed</mzXML>")
            unit.converted_input(source)
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"source_sha256_mismatch": 1}, check.evidence["uncovered_reasons"])

    def test_the_source_row_a_conversion_carries_is_the_one_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary, repository="mb_post")
            download = unit.download("MPST000009.tar", b"tar", md5="", archive=True)
            unit.manifest["project"]["files"].append({"name": "S1.mzXML", "checksum": MD5})
            unit.validated()
            source = unit.data / "S1.mzXML"
            source.write_bytes(b"<mzXML/>")
            source_row = {"path": str(source), "kind": "extracted_member",
                          "source": {"archive": _archive_source(download), "member": "S1.mzXML"},
                          "checksums": {"declared": MD5, "declared_algorithm": "md5", "declared_verified": True}}
            unit.converted_input(source, source_row=source_row)
            check = unit.sum1()

        self.assertEqual(verifier.PASS, check.status)
        self.assertEqual(1, check.evidence["converted_inputs"])

    def test_a_carried_source_row_for_another_mzxml_is_refused(self) -> None:
        """The conversion read b.mzXML, which nothing verified; the row it carries is a.mzXML's."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary, repository="mb_post")
            download = unit.download("MPST000009.tar", b"tar", md5="", archive=True)
            unit.manifest["project"]["files"].append({"name": "a.mzXML", "checksum": MD5})
            unit.validated()
            a = unit.data / "a.mzXML"
            a.write_bytes(b"<mzXML>a</mzXML>")
            b = unit.data / "b.mzXML"
            b.write_bytes(b"<mzXML>b</mzXML>")
            source_row = {"path": str(a), "kind": "extracted_member",
                          "source": {"archive": _archive_source(download), "member": "a.mzXML"},
                          "checksums": _claim(MD5)}
            unit.converted_input(b, source_row=source_row)
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"source_row_mismatch": 1}, check.evidence["uncovered_reasons"])
        self.assertIn("a.mzXML", check.detail)

    def test_a_carried_source_row_other_than_the_tables_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary, repository="mb_post")
            download = unit.download("MPST000009.tar", b"tar", md5="", archive=True)
            unit.manifest["project"]["files"].append({"name": "S1.mzXML", "checksum": MD5})
            unit.validated()
            source = unit.data / "S1.mzXML"
            source.write_bytes(b"<mzXML/>")
            table_row = {"path": str(source), "kind": "extracted_member",
                         "source": {"archive": _archive_source(download), "member": "S1.mzXML"}, "checksums": {}}
            unit.manifest["input_lineage"]["rows"].append(table_row)
            unit.converted_input(source, source_row={**table_row, "checksums": _claim(MD5)})
            check = unit.sum1()

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"source_row_mismatch": 1}, check.evidence["uncovered_reasons"])

    def test_the_tables_row_for_the_source_is_followed(self) -> None:
        """Without a carried row, the table's own row for the mzXML is what covers it."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary, repository="mb_post")
            download = unit.download("MPST000009.tar", b"tar", md5="", archive=True)
            unit.manifest["project"]["files"].append({"name": "S1.mzXML", "checksum": MD5})
            unit.validated()
            source = unit.data / "S1.mzXML"
            source.write_bytes(b"<mzXML/>")
            unit.manifest["input_lineage"]["rows"].append(
                {"path": str(source), "kind": "extracted_member",
                 "source": {"archive": _archive_source(download), "member": "S1.mzXML"}, "checksums": _claim(MD5)})
            unit.converted_input(source)
            check = unit.sum1()

        self.assertEqual(verifier.PASS, check.status)
        self.assertEqual(1, check.evidence["converted_inputs"])

    def test_a_conversion_of_other_bytes_than_the_validator_compared_is_refused(self) -> None:
        """A member is not hashed in the lineage; the declared checksum compared is held to the record."""
        for algorithm, digest in (("sha256", hashlib.sha256), ("sha1", hashlib.sha1)):
            with self.subTest(algorithm=algorithm), tempfile.TemporaryDirectory() as temporary:
                unit = LineageUnit(temporary, repository="mb_post")
                download = unit.download("MPST000009.tar", b"tar", md5="", archive=True)
                declared = digest(b"<mzXML/>").hexdigest()
                unit.manifest["project"]["files"].append({"name": "S1.mzXML", "checksum": declared})
                unit.validated()
                source = unit.data / "S1.mzXML"
                source.write_bytes(b"<mzXML>changed after it was compared</mzXML>")
                source_row = {"path": str(source), "kind": "extracted_member",
                              "source": {"archive": _archive_source(download), "member": "S1.mzXML"},
                              "checksums": _claim(declared, algorithm)}
                unit.converted_input(source, source_row=source_row)
                check = unit.sum1()

                self.assertEqual(verifier.FAIL, check.status)
                self.assertEqual({"source_checksum_mismatch": 1}, check.evidence["uncovered_reasons"])


# ---------------------------------------------------------------------------------------------
# SUM-2: what may be said of inputs that came out of an archive
# ---------------------------------------------------------------------------------------------

class ArchiveWordingTests(unittest.TestCase):
    def test_the_permitted_wording_passes(self) -> None:
        sentences = [
            "Raw files were extracted from an archive whose published MD5 matched.",
            "Raw files were extracted from an archive whose published MD5 checksum matched.",
            "The inputs were extracted from the archives whose published MD5s matched at download.",
            "Raw files were extracted from archives whose published MD5 checksums matched.",
        ]
        for sentence in sentences:
            with self.subTest(sentence=sentence), tempfile.TemporaryDirectory() as temporary:
                check = _workbench_archive_unit(temporary).publish(sentence)

                self.assertEqual(verifier.PASS, check.status)
                self.assertEqual(1, check.evidence["permitted_wording_count"])
                self.assertEqual(0, check.evidence["to_read_count"])

    def test_calling_archive_inputs_checksum_verified_is_refused(self) -> None:
        claims = [
            "All raw files were checksum-verified.",
            "Raw files were extracted from an archive whose published MD5 matched, and their checksums "
            "were verified.",
            "Input integrity was verified against the published MD5 checksums.",
        ]
        for sentence in claims:
            with self.subTest(sentence=sentence), tempfile.TemporaryDirectory() as temporary:
                check = _workbench_archive_unit(temporary).publish(sentence)

                self.assertEqual(verifier.FAIL, check.status)
                self.assertIn(ARCHIVE_WORDING, check.detail)

    def test_a_negation_beside_archive_inputs_is_for_a_person_to_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            check = _workbench_archive_unit(temporary).publish(
                "The inputs were not checksum-verified; they were extracted from an archive whose published MD5 "
                "matched.")

        self.assertEqual(verifier.WARN, check.status)
        self.assertEqual(1, check.evidence["to_read_count"])

    def test_the_archive_wording_is_a_claim_where_no_checksum_was_published(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary, repository="metabolights")
            unit.file_input(unit.download("S1.mzML", b"one", md5=""))
            check = unit.publish("Raw files were extracted from an archive whose published MD5 matched.")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("no archive's published MD5 was compared", check.evidence["claims"][0])

    def test_a_download_sha256_publication_keeps_its_evidence(self) -> None:
        """No archive: the check reports exactly what it reported before the archive basis existed."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary, repository="metabolights")
            unit.file_input(unit.download("S1.mzML", b"one", md5=""))
            check = unit.publish("Peaks were aligned across 6 samples.")

        self.assertEqual(verifier.PASS, check.status)
        self.assertNotIn("permitted_wording_count", check.evidence)

    def test_verified_inputs_owe_no_wording(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = LineageUnit(temporary)
            unit.file_input(unit.download("S1.mzML", b"one"))
            check = unit.publish("All raw files were checksum-verified.")

        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertFalse(check.required)


if __name__ == "__main__":
    unittest.main()
