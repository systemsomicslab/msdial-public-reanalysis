"""CLS-2 on an analysis CSV Interactive built from the input lineage.

Interactive's folder branch (repository_analysis_rows) writes a repository unit's analysis CSV from its
input lineage, one row per analysis input, folder or file. Three of its renames reach the gate: an
input the Console's parser cannot read back (a comma, a quote, a character outside ASCII) is read
through an ASCII alias in raw/console-aliases, and its row's file_name is the alias's; two inputs that
share a stem are named apart by a digest (file_name_not_unique); and a projected Class label the
Console could not read back is folded to ASCII (analysis_csv.class_id_aliases). An input an applied
campaign disposition excluded, or one the lease excluded, is no row at all. CLS-2 joined rows to
samples by name and compared labels verbatim, so it refused every such unit while each was correct.

The fixtures are built as that branch writes them (feat/folder-inputs-and-csv-builder at d3ccfdc):
build_repository_analysis_rows for the rows, their aliases and names, create_console_aliases
for the junction or hard link, record_analysis_csv for the lineage rows' file_name, console_path and
console_alias and for analysis_csv, and classify_preflight's applied campaign_disposition.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _ROOT / "scripts" / "verify-run-invariants.py"
_SPEC = importlib.util.spec_from_file_location("verify_run_invariants_built_csv", _MODULE_PATH)
assert _SPEC and _SPEC.loader
verifier = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_run_invariants_built_csv"] = verifier
_SPEC.loader.exec_module(verifier)

CSV_COLUMNS = ["file_path", "file_name", "file_type", "class_id", "acquisition_type", "batch_order",
               "analytical_order", "factor"]
LINEAGE_SCHEMA = "msdial-input-lineage.v1"
ALIAS_DIRECTORY = "console-aliases"


def _check(report, check_id: str):
    matching = [check for check in report.checks if check.check_id == check_id]
    assert matching, f"{check_id} was not evaluated at all"
    assert len(matching) == 1, f"{check_id} ran {len(matching)} times"
    return matching[0]


# ---- Interactive's renames, as repository_analysis_rows makes them ------------------------------------

def _console_safe(value: str) -> bool:
    """workflow.console_safe_text: printable ASCII without a comma or a quote."""
    return all(" " <= character <= "~" for character in value) and "," not in value and '"' not in value


def _ascii_name(value: str, fallback: str = "input") -> str:
    folded = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    folded = re.sub(r"[^A-Za-z0-9._-]+", "_", folded).strip("._-")
    return (folded or fallback)[:60]


def _digest(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).casefold().encode("utf-8")).hexdigest()[:8]


def _safe_name(value: str) -> bool:
    return bool(value) and value == value.strip(" .") and _console_safe(value) and not any(
        character in value for character in '<>:/\\|?*')


def _class_token(value: str) -> str:
    """repository_metadata.class_token, which apply_class_proposal applies to every approved label."""
    text = unicodedata.normalize("NFKC", str(value).strip())
    text = re.sub(r"[\s_]+", "-", text.strip())
    text = "".join(character if character.isalnum() or character in ".+-" else "-" for character in text)
    return re.sub(r"-+", "-", text).strip("-")


def _class_aliases(labels: list[str]) -> dict[str, str]:
    """repository_analysis_rows._class_aliases: an ASCII token for each label the Console cannot read."""
    distinct = sorted(set(labels))
    aliases: dict[str, str] = {}
    taken = {label.casefold() for label in distinct if _console_safe(label)}
    number = 0
    for label in distinct:
        if _console_safe(label):
            aliases[label] = label
            continue
        folded = _class_token(unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii"))
        if not folded or folded.casefold() in taken:
            number += 1
            folded = f"Class{number}"
            while folded.casefold() in taken:
                number += 1
                folded = f"Class{number}"
        taken.add(folded.casefold())
        aliases[label] = folded
    return aliases


def _link(target: Path, link: Path) -> None:
    """create_console_aliases: a junction for a folder, a hard link for a file."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir():
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        except (ImportError, AttributeError):
            os.symlink(target, link, target_is_directory=True)
    else:
        os.link(target, link)


class FolderBranchUnit:
    """One campaign unit as Interactive's folder branch leaves it before production.

    Each input is downloaded with its MD5 verified (a file) or assembled from member objects that each
    were (a vendor folder), declared by the Catalog with its sample, and given a lineage row naming that
    sample. prepare() then writes the CSV and its records as the branch does.
    """

    def __init__(self, temporary: str, unit_id: str = "unit") -> None:
        self.root = Path(temporary) / unit_id
        self.raw = self.root / "raw"
        self.data = self.raw / "data"
        self.data.mkdir(parents=True)
        (self.root / "output").mkdir()
        (self.root / "provenance").mkdir()
        self.labels: dict[str, str] = {}
        self.manifest: dict = {
            "schema": "msdial-public-reanalysis-run.v1",
            "project": {
                "analysis_unit_id": unit_id, "repository": "metabobank", "accession": "MTBKS999",
                "files": [], "analysis_inputs": [], "analysis_inputs_declared": True, "sample_metadata": [],
                "class_proposal": {"proposal_id": "p1", "unit_id": unit_id, "status": "accepted",
                                   "model": "catalog-declared-factor-selection",
                                   "selected_fields": ["Sample type"], "assignments": []},
            },
            "workspace": str(self.root),
            "raw_directory": str(self.raw),
            "input_directory": str(self.data),
            "execution_allowed": True,
            "downloads": [],
            "allowlist_checksum_validation": {"required": True, "verified": 0, "skipped": 0},
            "input_candidates": [],
            "input_lineage": {"schema": LINEAGE_SCHEMA, "rows": [], "excluded": []},
        }

    @property
    def output(self) -> Path:
        return self.root / "output"

    def _download(self, path: Path, data: bytes, *, verified: bool) -> dict:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        md5 = hashlib.md5(data).hexdigest()
        download = {"path": str(path), "source_url": f"https://example.org/{path.name}", "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(), "md5": md5, "declared_checksum": md5}
        if verified:
            download["declared_checksum_verified"] = True
        self.manifest["downloads"].append(download)
        self.manifest["project"]["files"].append(
            {"name": path.relative_to(self.data).as_posix(), "checksum": md5})
        validation = self.manifest["allowlist_checksum_validation"]
        validation["verified" if verified else "skipped"] += 1
        return download

    def _declare(self, relative: str, kind: str, sample: str, label: str, **extra) -> None:
        project = self.manifest["project"]
        project["analysis_inputs"].append({"path": relative, "kind": kind, "sample_id": sample, **extra})
        project["analysis_input_count"] = len(project["analysis_inputs"])
        project["sample_metadata"].append({"sample_id": sample, "raw_file": relative})
        project["class_proposal"]["assignments"].append({"sample_id": sample, "class_label": label})
        self.labels[sample] = label

    def file(self, relative: str, sample: str, label: str = "A", *, verified: bool = True) -> str:
        download = self._download(self.data / relative, relative.encode("utf-8"), verified=verified)
        self._declare(relative, "file", sample, label)
        checksums = {"sha256": download["sha256"], "md5": download["md5"], "declared": download["declared_checksum"],
                     "declared_algorithm": "md5" if verified else "", "declared_verified": True if verified else None}
        return self._input(download["path"], "file", sample,
                           {"url": download["source_url"], "download_path": download["path"]}, checksums)

    def folder(self, relative: str, sample: str, label: str = "A") -> str:
        """A Waters .raw folder assembled from two member objects."""
        members = ["_FUNC001.DAT", "_extern.inf"]
        for member in members:
            self._download(self.data / relative / member, f"{relative}/{member}".encode("utf-8"), verified=True)
        self._declare(relative, "vendor_folder", sample, label, suffix=".raw", format="waters_raw",
                      member_count=len(members))
        return self._input(str(self.data / relative), "vendor_folder", sample, {"objects": len(members)},
                           {"member_objects": len(members)})

    def _input(self, path: str, kind: str, sample: str, source: dict, checksums: dict) -> str:
        self.manifest["input_candidates"].append(path)
        self.manifest["input_lineage"]["rows"].append(
            {"path": path, "kind": kind, "declared_names": [], "sample_id": sample, "file_name": "",
             "source": source, "checksums": checksums})
        return path

    def exclude(self, path: str, reason: str = "ion_mobility_out_of_scope", *, applied: bool = True) -> None:
        """classify_preflight's disposition: run the unit, less this input, which stays a candidate."""
        disposition = self.manifest.setdefault("campaign_disposition", {
            "schema": "msdial-campaign-disposition.v1", "disposition": "run", "reasons": [], "warnings": [],
            "excluded_inputs": [], "split_key": None, "decided_at": "2026-10-01T09:00:00+09:00",
            "declared_vs_header": [], "detail": [], "applied": applied})
        disposition["excluded_inputs"].append({"path": path, "reason": reason})

    def lease_excluded(self, relative: str, sample: str, label: str = "A") -> str:
        """An mzML whose arrays RawDataHandler cannot decode: declared, downloaded, and no candidate."""
        download = self._download(self.data / relative, b"<mzML/>", verified=True)
        self._declare(relative, "file", sample, label)
        self.manifest.setdefault("excluded_input_candidates", []).append(
            {"path": download["path"], "reason": "unsupported_mzml_encoding"})
        self.manifest["input_lineage"]["excluded"].append(
            {"path": download["path"], "kind": "file", "sample_id": sample,
             "exclusion": {"reason": "unsupported_mzml_encoding"}})
        return download["path"]

    # -- what msdial_prepare_repository_reanalysis does with it ---------------------------------------

    def prepare(self) -> list[dict]:
        """build_repository_analysis_rows, create_console_aliases, write_analysis_csv and record_analysis_csv."""
        disposition = self.manifest.get("campaign_disposition") or {}
        excluded = {item["path"]: item["reason"] for item in disposition.get("excluded_inputs") or []} \
            if disposition.get("applied") is True else {}
        excluded.update({item["path"]: item["reason"]
                         for item in self.manifest.get("excluded_input_candidates") or []})
        lineage = {row["path"]: row for row in self.manifest["input_lineage"]["rows"]}
        candidates = sorted(self.manifest["input_candidates"], key=str.lower)
        rows = []
        for position, candidate in enumerate([item for item in candidates if item not in excluded], start=1):
            path = Path(candidate)
            sample = lineage[candidate]["sample_id"]
            row = {"file_path": candidate, "file_name": path.stem, "file_type": "Sample",
                   "class_id": _class_token(self.labels[sample]) or "Sample", "acquisition_type": "DDA",
                   "batch_order": 1, "analytical_order": position, "factor": 1, "input_path": candidate,
                   "console_alias": None}
            reasons = [reason for reason, safe in (("path_not_console_safe", _console_safe(candidate)),
                                                   ("name_not_console_safe", _safe_name(path.stem))) if not safe]
            if reasons:
                stem = f"{_ascii_name(path.stem)}-{_digest(path)}"
                suffix = "." + _ascii_name(path.suffix[1:], "") if path.suffix[1:] else ""
                row["file_name"] = stem
                row["file_path"] = str(self.raw / ALIAS_DIRECTORY / f"{stem}{suffix}")
                row["console_alias"] = {"path": row["file_path"], "kind": "junction" if path.is_dir() else "hardlink",
                                        "target": candidate, "reasons": reasons}
            rows.append(row)
        names: dict[str, list[dict]] = {}
        for row in rows:
            names.setdefault(row["file_name"].casefold(), []).append(row)
        for group in names.values():
            if len(group) > 1:
                for row in group:
                    if row["console_alias"] is None:
                        row["file_name"] = f"{row['file_name']}-{_digest(Path(row['input_path']))}"
                        row["file_name_reason"] = "file_name_not_unique"
        labels = _class_aliases([row["class_id"] for row in rows])
        for row in rows:
            row["class_id"] = labels.get(row["class_id"], row["class_id"])
            if row["console_alias"]:
                _link(Path(row["input_path"]), Path(row["file_path"]))
        self.write_csv(rows)
        by_input = {row["input_path"]: row for row in rows}
        for item in self.manifest["input_lineage"]["rows"]:
            row = by_input.get(item["path"])
            if row is None:
                item["file_name"] = ""
                continue
            item.update(file_name=row["file_name"], console_path=row["file_path"], acquisition_type="DDA")
            if row.get("file_name_reason"):
                item["file_name_reason"] = row["file_name_reason"]
            if row["console_alias"]:
                item["console_alias"] = dict(row["console_alias"])
        self.manifest["analysis_csv"] = {
            "schema": "msdial-repository-analysis-csv.v1", "status": "written", "built_from": "input_lineage",
            "path": str(self.output / "analysis_files.csv"), "rows": len(rows),
            "class_id_aliases": {label: alias for label, alias in labels.items() if label != alias},
            "samples_without_input": [],
            "excluded_inputs": [{"path": path, "reason": reason,
                                 "sample_id": (lineage.get(path) or self._lease_row(path)).get("sample_id", "")}
                                for path, reason in sorted(excluded.items())],
        }
        return rows

    def _lease_row(self, path: str) -> dict:
        return next((row for row in self.manifest["input_lineage"]["excluded"] if row["path"] == path), {})

    def write_csv(self, rows: list[dict]) -> None:
        with (self.output / "analysis_files.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows({name: row.get(name, "") for name in CSV_COLUMNS} for row in rows)

    def write(self) -> Path:
        (self.root / "provenance" / "run-manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False), encoding="utf-8")
        return self.root

    def gate(self, stage: str = "before-production"):
        return verifier.verify(self.write(), stage)


# ---- CLS-2 -----------------------------------------------------------------------------------------


class AliasedRowsInClassTests(unittest.TestCase):
    """CLS-2 joins a row to its sample through the input it opens and that input's lineage row."""

    def test_an_aliased_file_is_the_sample_its_input_is(self) -> None:
        """THE DEFECT (wave-3 review, finding 5): '1 approved sample(s) are absent from the CSV, 1 CSV row(s)
        were never approved' for a unit whose every row carried its approved Class."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1,rep1.mzML", "S1,rep1", "Control")
            unit.file("b.mzML", "b", "Treated")
            rows = unit.prepare()
            report = unit.gate()

        aliased = _aliased(rows)
        self.assertTrue(aliased["file_name"].startswith("S1_rep1-"), aliased["file_name"])
        check = _check(report, "CLS-2")
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(2, check.evidence["joined_through_lineage"])
        for check_id in ("INP-1", "CNT-1", "SUM-1"):
            self.assertEqual(verifier.PASS, _check(report, check_id).status, _check(report, check_id).detail)

    def test_an_aliased_folder_is_the_sample_its_input_is(self) -> None:
        """A Waters .raw folder whose name the Console cannot read is given to it through a junction."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.folder("raw/葉_1.raw", "Leaf 1", "Leaf")
            unit.folder("raw/Root_1.raw", "Root 1", "Root")
            rows = unit.prepare()
            alias = Path(rows[1]["file_path"])
            report = unit.gate()
            self.assertTrue(os.path.samefile(alias, rows[1]["input_path"]), "the junction is the folder")

        check = _check(report, "CLS-2")
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(verifier.PASS, _check(report, "SUM-1").status, _check(report, "SUM-1").detail)

    def test_inputs_sharing_a_stem_are_told_apart_by_their_lineage(self) -> None:
        """file_name_not_unique: neg/x.mzML and pos/x.mzML are x-<digest> and x-<digest> in the CSV."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("neg/x.mzML", "x negative", "A")
            unit.file("pos/x.mzML", "x positive", "B")
            rows = unit.prepare()
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual({"file_name_not_unique"}, {row.get("file_name_reason") for row in rows})
        self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_a_label_the_console_reads_otherwise_is_compared_as_it_was_written(self) -> None:
        """A label is projected ('Wild type' as 'Wild-type') and, outside ASCII, folded (class_id_aliases)."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1.mzML", "S1", "Wild type")
            unit.file("S2.mzML", "S2", "変異体")
            unit.file("S3.mzML", "S3", "Café")
            rows = unit.prepare()
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(["Wild-type", "Class1", "Cafe"], [row["class_id"] for row in rows])
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual({"Wild type": "Wild-type", "変異体": "Class1", "Café": "Cafe"}, check.evidence["written_as"])
        self.assertIn("written as the Console reads them", check.detail)
        self.assertNotIn("class_id_aliases_not_a_fold", check.evidence)

    def test_a_label_the_projection_leaves_empty_runs_as_sample(self) -> None:
        """apply_class_proposal writes class_token(label) or 'Sample': '×' has no letter or digit to keep."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1.mzML", "S1", "×")
            unit.file("S2.mzML", "S2", "B")
            rows = unit.prepare()
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(["Sample", "B"], [row["class_id"] for row in rows])
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual({"×": "Sample"}, check.evidence["written_as"])

    def test_a_sample_whose_input_the_disposition_excluded_is_reported_excluded(self) -> None:
        """Confirmed in wave 3: one disposition-excluded input reported '1 approved sample(s) are absent'."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.folder("raw/S1.raw", "S1", "A")
            unit.folder("raw/S2.raw", "S2", "B")
            unit.exclude(unit.file("raw/IM1.d", "IM1", "B"))
            rows = unit.prepare()
            report = unit.gate()

        self.assertEqual(2, len(rows))
        check = _check(report, "CLS-2")
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual({"IM1": ["IM1.d"]}, check.evidence["excluded_samples"])
        self.assertIn("1 approved sample(s) are not analysed", check.detail)
        self.assertEqual(verifier.PASS, _check(report, "INP-1").status, _check(report, "INP-1").detail)

    def test_a_sample_whose_mzml_the_lease_excluded_is_reported_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1.mzML", "S1", "A")
            unit.file("S2.mzML", "S2", "B")
            unit.lease_excluded("S3.mzML", "S3", "B")
            unit.prepare()
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual({"S3": ["S3.mzML"]}, check.evidence["excluded_samples"])

    def test_without_the_csv_record_an_excluded_input_is_found_by_its_lineage_or_its_name(self) -> None:
        for keep_lineage in (True, False):
            with self.subTest(keep_lineage=keep_lineage), tempfile.TemporaryDirectory() as temporary:
                unit = FolderBranchUnit(temporary)
                unit.file("S1.mzML", "S1", "A")
                unit.exclude(unit.file("IM1.d", "IM1", "B"))
                unit.prepare()
                del unit.manifest["analysis_csv"]
                if not keep_lineage:
                    for row in unit.manifest["input_lineage"]["rows"]:
                        row["sample_id"] = ""
                    unit.manifest["project"]["sample_metadata"][1]["raw_file"] = "FILES/IM1.d.zip"
                check = _check(unit.gate(), "CLS-2")

                self.assertEqual(verifier.PASS, check.status, check.detail)
                self.assertEqual({"IM1": ["IM1.d"]}, check.evidence["excluded_samples"])


class RealMismatchesStillFailTests(unittest.TestCase):
    """What the aliases, the names and the exclusions must not excuse."""

    def _unit(self, temporary: str) -> tuple[FolderBranchUnit, list[dict]]:
        unit = FolderBranchUnit(temporary)
        unit.file("S1,rep1.mzML", "S1,rep1", "Control")
        unit.file("b.mzML", "b", "Treated")
        return unit, unit.prepare()

    def test_an_aliased_row_with_another_class_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, rows = self._unit(temporary)
            _aliased(rows)["class_id"] = "Treated"
            unit.write_csv(rows)
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("S1,rep1 (" + _aliased(rows)["file_name"] + "): approved 'Control', executed 'Treated'",
                      check.evidence["differing"])

    def test_an_exclusion_only_the_csv_record_claims_excludes_nothing(self) -> None:
        """analysis_csv is the CSV writer's own record: a sample it drops and calls excluded is absent,
        beside one the disposition did exclude."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1,rep1.mzML", "S1,rep1", "Control")
            unit.file("b.mzML", "b", "Treated")
            unit.exclude(unit.file("IM1.d", "IM1", "Treated"))
            rows = unit.prepare()
            unit.write_csv([row for row in rows if not row["console_alias"]])
            unit.manifest["analysis_csv"]["excluded_inputs"].append(
                {"path": _aliased(rows)["input_path"], "reason": "ion_mobility_out_of_scope", "sample_id": "S1,rep1"})
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual(["S1,rep1"], check.evidence["missing"])
        self.assertEqual({"IM1": ["IM1.d"]}, check.evidence["excluded_samples"])

    def test_a_fold_that_merges_two_approved_classes_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1.mzML", "S1", "対照")
            unit.file("S2.mzML", "S2", "処理")
            rows = unit.prepare()
            for row in rows:
                row["class_id"] = "Class1"
            unit.manifest["analysis_csv"]["class_id_aliases"] = {"対照": "Class1", "処理": "Class1"}
            unit.write_csv(rows)
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("'Class1' carries approved ['処理', '対照']", check.evidence["regrouped"])

    def test_an_alias_record_that_is_no_fold_excuses_nothing(self) -> None:
        """class_id_aliases is the CSV writer's own record. Interactive leaves a label the Console reads back
        as it is and folds any other to its ASCII fold or a number, so a record that swaps 'Control' and
        'Treated', or gives '対照' another approved label's name, is no fold, and the rows it would excuse
        carry another Class."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1.mzML", "S1", "Control")
            unit.file("S2.mzML", "S2", "Treated")
            unit.file("S3.mzML", "S3", "対照")
            rows = unit.prepare()
            rows[0]["class_id"], rows[1]["class_id"], rows[2]["class_id"] = "Treated", "Control", "Control"
            unit.write_csv(rows)
            unit.manifest["analysis_csv"]["class_id_aliases"] = {
                "Control": "Treated", "Treated": "Control", "対照": "Control"}
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual(3, len(check.evidence["differing"]), check.evidence["differing"])
        self.assertEqual({"Control": "Treated", "Treated": "Control", "対照": "Control"},
                         check.evidence["class_id_aliases_not_a_fold"])

    def test_a_projection_to_sample_that_meets_an_approved_sample_class_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1.mzML", "S1", "×")
            unit.file("S2.mzML", "S2", "Sample")
            unit.prepare()
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("'Sample' carries approved ['Sample', '×']", check.evidence["regrouped"])

    def test_the_csv_record_does_not_outrank_the_lease_on_whose_input_was_excluded(self) -> None:
        """A CSV that dropped S1's row and named S1 as the sample of IM1's excluded input excused S1."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1.mzML", "S1", "A")
            unit.file("S2.mzML", "S2", "B")
            unit.exclude(unit.file("IM1.d", "IM1", "B"))
            rows = unit.prepare()
            unit.write_csv([row for row in rows if row["file_name"] != "S1"])
            for item in unit.manifest["analysis_csv"]["excluded_inputs"]:
                item["sample_id"] = "S1"
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual(["S1"], check.evidence["missing"])
        self.assertEqual({"IM1": ["IM1.d"]}, check.evidence["excluded_samples"])

    def test_an_exclusion_does_not_excuse_a_sample_whose_other_input_runs(self) -> None:
        """IM1 is an ion-mobility .d the disposition excluded and an mzML that runs; the mzML's row is gone."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1.mzML", "S1", "A")
            unit.file("IM1.mzML", "IM1", "B")
            unit.exclude(unit.file("IM1.d", "IM1", "B"))
            # One sample of two inputs: its row and assignment are the sample table's once.
            unit.manifest["project"]["sample_metadata"].pop()
            unit.manifest["project"]["class_proposal"]["assignments"].pop()
            rows = unit.prepare()
            unit.write_csv([row for row in rows if row["file_name"] != "IM1"])
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual(["IM1"], check.evidence["missing"])
        self.assertNotIn("excluded_samples", check.evidence)

    def test_a_unit_whose_every_sample_was_excluded_runs_no_grouping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.exclude(unit.file("IM1.d", "IM1", "B"))
            unit.exclude(unit.file("IM2.d", "IM2", "A"))
            rows = unit.prepare()
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual([], rows)
        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("None of the 2 approved sample(s) is analysed", check.detail)
        self.assertEqual({"IM1": ["IM1.d"], "IM2": ["IM2.d"]}, check.evidence["excluded_samples"])

    def test_a_projection_that_merges_two_approved_classes_is_refused(self) -> None:
        """'A B' and 'A-B' are two Classes approved, and class_token writes both as 'A-B'."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1.mzML", "S1", "A B")
            unit.file("S2.mzML", "S2", "A-B")
            rows = unit.prepare()
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(["A-B", "A-B"], [row["class_id"] for row in rows])
        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("merged or split", check.detail)

    def test_a_projection_that_splits_one_approved_class_is_refused(self) -> None:
        """Each spelling alone is a rendering of 'Wild type'; together they are two Classes where one was approved."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = FolderBranchUnit(temporary)
            unit.file("S1.mzML", "S1", "Wild type")
            unit.file("S2.mzML", "S2", "Wild type")
            rows = unit.prepare()
            rows[0]["class_id"] = "Wild type"
            unit.write_csv(rows)
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("approved 'Wild type' runs as ['Wild type', 'Wild-type']", check.evidence["regrouped"])

    def test_a_lineage_row_recording_another_file_name_says_nothing_of_the_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, rows = self._unit(temporary)
            _lineage_row(unit, _aliased(rows)["input_path"])["file_name"] = "another_csv_name"
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual(["S1,rep1"], check.evidence["missing"])
        self.assertEqual([_aliased(rows)["file_name"]], check.evidence["unapproved"])

    def test_an_alias_that_is_another_file_stands_for_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, rows = self._unit(temporary)
            alias = Path(_aliased(rows)["file_path"])
            alias.unlink()
            alias.write_bytes(b"another file")
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual(["S1,rep1"], check.evidence["missing"])

    def test_a_row_the_lineage_gives_to_a_sample_nobody_approved_is_unapproved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, rows = self._unit(temporary)
            _lineage_row(unit, next(row["input_path"] for row in rows if row["file_name"] == "b"))["sample_id"] = (
                "someone else")
            check = _check(unit.gate(), "CLS-2")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual(["b"], check.evidence["missing"])
        self.assertEqual(["b"], check.evidence["unapproved"])


def _aliased(rows: list[dict]) -> dict:
    """The one row read through a Console alias."""
    (row,) = [row for row in rows if row["console_alias"]]
    return row


def _lineage_row(unit: FolderBranchUnit, path: str) -> dict:
    return next(row for row in unit.manifest["input_lineage"]["rows"] if row["path"] == path)


if __name__ == "__main__":
    unittest.main()
