"""Folder and container names in CLS-2, the declared inputs (INP-1), the acquisition type (ACQ-1),
AIF collision energies (AIF-1) and the run finalisation's holds (FIN-2).

The campaign counts a vendor folder (Waters .raw, Agilent or Bruker .d) as one data file, and a
container published packed (X.d.zip) as the container. The sample metadata names them as raw/x.raw/
or x.d.zip while the analysis CSV names them x, and CLS-2 called every such sample absent. The
Catalog declares what MS-DIAL opens (analysis_inputs, one per file, folder or packed container), and
nothing compared it with what the lease found. The Console reads each row's acquisition_type and
turns anything it cannot parse, "DIA" among them, into DDA without a word. And Interactive 0.5.15
records a finalisation step it could not do as a hold in the unit manifest, which the gate did not
read.

The fixtures are built the way Interactive writes these records: the per-file preflight record of
_summarize_raw_metadata plus the shared contract's console_acquisition_type, the extractor's own
raw-metadata-preflight.json (msdial.raw-metadata.v1), the Catalog handoff's analysis_inputs, and the
holds of run_finalisation.finalise_console_run.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _ROOT / "scripts" / "verify-run-invariants.py"
_SPEC = importlib.util.spec_from_file_location("verify_run_invariants_inputs", _MODULE_PATH)
assert _SPEC and _SPEC.loader
verifier = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_run_invariants_inputs"] = verifier
_SPEC.loader.exec_module(verifier)

CSV_COLUMNS = ["file_path", "file_name", "file_type", "class_id", "acquisition_type", "batch_order",
               "analytical_order", "factor"]


def _check(report, check_id: str):
    matching = [check for check in report.checks if check.check_id == check_id]
    assert matching, f"{check_id} was not evaluated at all"
    assert len(matching) == 1, f"{check_id} ran {len(matching)} times; each check must run once"
    return matching[0]


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class Unit:
    """One unit's workspace: a manifest, its inputs under raw/data, and the analysis CSV."""

    def __init__(self, temporary: str, unit_id: str = "unit") -> None:
        self.root = Path(temporary) / unit_id
        self.data = self.root / "raw" / "data"
        self.data.mkdir(parents=True)
        (self.root / "output").mkdir()
        self.manifest = {
            "schema": "msdial-public-reanalysis-run.v1",
            "project": {"analysis_unit_id": unit_id, "repository": "metabolights"},
            "workspace": str(self.root),
            "raw_directory": str(self.root / "raw"),
            "input_directory": str(self.data),
            "execution_allowed": True,
            "input_candidates": [],
        }
        self.extractor: list[dict] = []

    @property
    def output(self) -> Path:
        return self.root / "output"

    def inputs(self, names: list[str]) -> list[str]:
        paths = [str(self.data / name) for name in names]
        for path in paths:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            if path.casefold().endswith((".raw", ".d")):
                Path(path).mkdir(exist_ok=True)
            else:
                Path(path).write_bytes(b"x")
        self.manifest["input_candidates"] = paths
        return paths

    def csv(self, rows: list[dict]) -> "Unit":
        with (self.output / "analysis_files.csv").open("w", encoding="ascii", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows({"file_type": "Sample", "class_id": "A", "batch_order": "1", "factor": "1",
                              **row} for row in rows)
        return self

    def rows(self, acquisition: "str | list[str]" = "DDA") -> list[dict]:
        paths = self.manifest["input_candidates"]
        types = acquisition if isinstance(acquisition, list) else [acquisition] * len(paths)
        return [{"file_path": path, "file_name": Path(path.rstrip("\\/")).stem, "acquisition_type": kind,
                 "analytical_order": str(index + 1)} for index, (path, kind) in enumerate(zip(paths, types))]

    def preflight(self, per_file: list[dict], *, extractor: list[dict] | None = None) -> "Unit":
        """The block run_raw_metadata_preflight writes, and the extractor's output it names."""
        output = self.root / "provenance" / "raw-metadata-preflight.json"
        self.manifest["raw_metadata_preflight"] = {
            "exit_code": 0, "output": str(output),
            "summary": {"acquisition_mode": "DDA", "per_file": per_file},
        }
        if extractor is not None:
            _write(output, extractor)
        return self

    def write(self) -> Path:
        _write(self.root / "provenance" / "run-manifest.json", self.manifest)
        return self.root


def _record(path: str, mode: str, console: "str | None | bool" = False, **extra) -> dict:
    """A per-file preflight record; console=False leaves console_acquisition_type out (a legacy record)."""
    record = {"file": path, "acquisition_mode": mode, "confidence": 0.9, "polarity": "Negative",
              "ms_levels": [1, 2], **extra}
    if console is not False:
        record.update({"console_acquisition_type": console, "has_ms1": True, "has_ms2": True,
                       "has_ion_mobility": False, "method_source": "VendorHeader", "reader_created_files": []})
    return record


def _extracted(path: str, method: str, *, windows: int = 0, energies: list | None = None) -> dict:
    """One record of the extractor's msdial.raw-metadata.v1 output."""
    return {
        "schemaVersion": "msdial.raw-metadata.v1",
        "source": {"filePath": path, "fileName": Path(path).stem},
        "acquisition": {
            "method": {"value": method, "source": "SpectrumHeader", "confidence": 0.8, "evidence": ""},
            "isolationWindowTargets": [100.0 + 20 * index for index in range(windows)],
            "collisionEnergies": [30.0] if energies is None else energies,
            "msLevels": [1, 2],
        },
    }


def _disposition(excluded: list[dict], *, applied: bool = True, kind: str = "run",
                 declared_vs_header: list[dict] | None = None) -> dict:
    """campaign_disposition as Interactive's classify_preflight records it: applied only for a campaign unit."""
    return {"schema": "msdial-campaign-disposition.v1", "disposition": kind, "reasons": [], "warnings": [],
            "excluded_inputs": excluded, "split_key": None, "decided_at": "2026-09-30T12:00:00+09:00",
            "extractor": {"sha256": "", "inventory_sha256": "", "provenance_status": "verified", "pinned": True},
            "declared_vs_header": declared_vs_header or [], "detail": [], "applied": applied}


# ---- CLS-2 -----------------------------------------------------------------------------------------


class FolderAndContainerNamesTests(unittest.TestCase):
    """CLS-2 joins a sample to its CSV row through the raw_file the sample metadata records."""

    def _cls2(self, raw_files: list[str], names: list[str], *, labels: list[str] | None = None):
        with tempfile.TemporaryDirectory() as temporary:
            unit = Unit(temporary)
            unit.inputs(names)
            unit.manifest["project"].update({
                "sample_metadata": [{"sample_id": f"Sample {i}", "raw_file": raw} for i, raw in enumerate(raw_files)],
                "class_proposal": {"status": "accepted", "assignments": [
                    {"sample_id": f"Sample {i}", "class_label": (labels or ["A"] * len(names))[i]}
                    for i in range(len(names))]},
            })
            unit.csv(unit.rows())
            return _check(verifier.verify(unit.write(), "before-production"), "CLS-2")

    def test_a_folder_named_with_a_trailing_slash_maps_to_its_stem(self) -> None:
        """THE REGRESSION: 'raw/x.raw/' split to an empty name, and every folder sample was absent."""
        check = self._cls2(["raw/S1.raw/", "raw/S2.raw/"], ["S1.raw", "S2.raw"])

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(2, check.evidence["joined_through_raw_file"])

    def test_an_archived_container_maps_to_its_stem(self) -> None:
        """MTBLS719 and MTBLS7260: 'x.d.zip' was reduced to 'x.d', and the CSV calls it 'x'."""
        check = self._cls2(["FILES/S1.d.zip", "FILES/S2.raw.zip"], ["S1.d", "S2.raw"])

        self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_a_folder_sample_with_another_class_is_still_caught(self) -> None:
        check = self._cls2(["raw/S1.raw/", "raw/S2.raw/"], ["S1.raw", "S2.raw"], labels=["A", "B"])

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("Sample 1 (S2): approved 'B', executed 'A'", check.evidence["differing"])

    def test_the_name_forms_strip_the_slash_the_archive_and_the_container_in_that_order(self) -> None:
        self.assertEqual("x", verifier._csv_name_forms("raw/x.raw/")[1])
        self.assertIn("x", verifier._csv_name_forms("x.d.zip"))
        self.assertIn("x", verifier._csv_name_forms("FILES\\x.mzML.gz"))
        self.assertIn("x", verifier._csv_name_forms("x.tar.gz"))
        # Most specific first: a stem with a dot of its own is kept before its last part is cut.
        forms = verifier._csv_name_forms("Sample.1.mzML")
        self.assertLess(forms.index("Sample.1"), forms.index("Sample") if "Sample" in forms else len(forms))
        self.assertEqual(["x.raw", "x"], verifier._csv_name_forms("x.raw"))
        self.assertEqual([], verifier._csv_name_forms("/"))


# ---- INP-1 -----------------------------------------------------------------------------------------


def _declared(count: int) -> list[dict]:
    """The Catalog's analysis_inputs, one per vendor folder."""
    return [{"path": f"S{index}.raw", "kind": "vendor_folder", "suffix": ".raw", "format": "waters",
             "member_count": 34} for index in range(count)]


class DeclaredInputsTests(unittest.TestCase):
    """INP-1. The Catalog's analysis inputs, the lease's input candidates and the CSV rows are one set."""

    def _inp1(self, *, declared: int | None, candidates: int, rows: int | None = None, **project):
        with tempfile.TemporaryDirectory() as temporary:
            unit = Unit(temporary)
            unit.inputs([f"S{index}.raw" for index in range(candidates)])
            if declared is not None:
                unit.manifest["project"]["analysis_inputs"] = _declared(declared)
            unit.manifest["project"].update(project)
            if rows is not None:
                unit.csv(unit.rows()[:rows])
            return _check(verifier.verify(unit.write(), "before-production"), "INP-1")

    def test_twelve_declared_twelve_found_twelve_rows_passes(self) -> None:
        check = self._inp1(declared=12, candidates=12, rows=12)

        self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_a_folder_counted_by_its_members_is_refused(self) -> None:
        """The inflated unit: 477 rows where the Catalog declared 12 samples."""
        check = self._inp1(declared=12, candidates=477, rows=477)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertEqual({"analysis_inputs": 12, "input_candidates": 477, "analysis_files.csv rows": 477},
                         check.evidence["counts"])

    def test_a_row_dropped_from_the_csv_is_refused(self) -> None:
        check = self._inp1(declared=12, candidates=12, rows=11)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("11 row(s) for 12 input candidate(s)", check.detail)

    def test_a_declaration_whose_list_was_lost_is_refused(self) -> None:
        """The handoff's response omits the list; only its file holds it."""
        check = self._inp1(declared=None, candidates=12, rows=12, analysis_input_count=12,
                           analysis_inputs=[], analysis_inputs_declared=True)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("lists none", check.detail)

    def test_a_count_the_list_does_not_hold_is_refused(self) -> None:
        check = self._inp1(declared=11, candidates=11, rows=11, analysis_input_count=12)

        self.assertEqual(verifier.FAIL, check.status)

    def test_without_a_declaration_it_is_not_required(self) -> None:
        check = self._inp1(declared=None, candidates=12, rows=12)

        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertFalse(check.required)

    def test_a_declared_unit_owes_its_csv(self) -> None:
        check = self._inp1(declared=12, candidates=12, rows=None)

        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertTrue(check.required)

    def test_inputs_the_disposition_excluded_are_accounted_for(self) -> None:
        """Excluded inputs that are no candidate; the shared contract's v1 shape, which does not say applied, binds."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = Unit(temporary)
            unit.inputs([f"S{index}.raw" for index in range(10)])
            unit.manifest["project"]["analysis_inputs"] = _declared(12)
            unit.manifest["campaign_disposition"] = {
                "schema": "msdial-campaign-disposition.v1", "disposition": "run", "reasons": [], "warnings": [],
                "excluded_inputs": [{"path": str(unit.data / f"IM{index}.d"), "reason": "ion_mobility"}
                                    for index in range(2)],
                "split_key": None,
            }
            unit.csv(unit.rows())
            check = _check(verifier.verify(unit.write(), "before-production"), "INP-1")

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(2, check.evidence["counts"]["excluded_inputs"])

    def test_a_row_naming_an_input_the_disposition_excluded_is_refused(self) -> None:
        """An excluded input left among the candidates runs if the CSV lists it; the counts alone agree."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = Unit(temporary)
            paths = unit.inputs([f"S{index}.raw" for index in range(11)] + ["IM0.d"])
            unit.manifest["project"]["analysis_inputs"] = _declared(12)
            unit.manifest["campaign_disposition"] = {
                "schema": "msdial-campaign-disposition.v1", "disposition": "run", "reasons": [], "warnings": [],
                "excluded_inputs": [{"path": paths[-1], "reason": "ion_mobility"}], "split_key": None,
            }
            unit.csv(unit.rows())
            check = _check(verifier.verify(unit.write(), "before-production"), "INP-1")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("1 CSV row(s) name an input the campaign disposition excluded (IM0)", check.detail)

    def test_an_input_excluded_among_the_candidates_is_no_row(self) -> None:
        """THE SHAPE INTERACTIVE WRITES: classify_preflight excludes an input where it finds it, among the
        candidates, and leaves it there; the CSV is the candidates less it. INP-1 and CNT-1 both refused it."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = Unit(temporary)
            paths = unit.inputs([f"S{index}.raw" for index in range(11)] + ["IM0.d"])
            unit.manifest["project"]["analysis_inputs"] = _declared(12)
            unit.manifest["campaign_disposition"] = _disposition(
                [{"path": paths[-1], "reason": "ion_mobility_out_of_scope"}])
            unit.csv(unit.rows()[:11])
            report = verifier.verify(unit.write(), "before-production")

        check = _check(report, "INP-1")
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(1, check.evidence["counts"]["excluded input_candidates"])
        self.assertIn("less the 1 the campaign disposition excluded, are the 11 CSV rows", check.detail)
        count = _check(report, "CNT-1")
        self.assertEqual(verifier.PASS, count.status, count.detail)
        self.assertEqual(["IM0.d"], count.evidence["excluded_input_candidates"])

    def test_a_row_still_missing_beside_an_excluded_candidate_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = Unit(temporary)
            paths = unit.inputs([f"S{index}.raw" for index in range(11)] + ["IM0.d"])
            unit.manifest["project"]["analysis_inputs"] = _declared(12)
            unit.manifest["campaign_disposition"] = _disposition(
                [{"path": paths[-1], "reason": "ion_mobility_out_of_scope"}])
            unit.csv(unit.rows()[:10])
            report = verifier.verify(unit.write(), "before-production")

        self.assertEqual(verifier.FAIL, _check(report, "INP-1").status)
        self.assertIn("10 row(s) for 12 input candidate(s), 1 of them excluded", _check(report, "INP-1").detail)
        self.assertEqual(verifier.FAIL, _check(report, "CNT-1").status)

    def test_a_disposition_that_was_not_applied_excludes_nothing(self) -> None:
        """A unit outside a campaign: classify_preflight records its disposition with applied false, and
        Interactive's execution gate ignores it, so the CSV lists the input, and must."""
        for rows, expected in ((12, verifier.PASS), (11, verifier.FAIL)):
            with self.subTest(rows=rows), tempfile.TemporaryDirectory() as temporary:
                unit = Unit(temporary)
                paths = unit.inputs([f"S{index}.raw" for index in range(12)])
                unit.manifest["project"]["analysis_inputs"] = _declared(12)
                unit.manifest["campaign_disposition"] = _disposition(
                    [{"path": paths[-1], "reason": "raw_header_unreadable"}], applied=False)
                unit.csv(unit.rows()[:rows])
                report = verifier.verify(unit.write(), "before-production")

                self.assertEqual(expected, _check(report, "INP-1").status, _check(report, "INP-1").detail)
                self.assertEqual(expected, _check(report, "CNT-1").status)

    def _split_excluding(self, temporary: str, *, into_part: bool = False) -> tuple[Path, Path]:
        """A parent of three folders and one ion-mobility input its applied split disposition excluded.

        Interactive's plan_acquisition_split leaves the excluded input among the parent's candidates and puts
        it in no part (split_excluded_inputs); into_part puts it in the first part all the same.
        """
        parent = Unit(temporary, "unit")
        paths = parent.inputs(["S0.raw", "S1.raw", "S2.raw", "IM0.d"])
        parent.manifest["project"]["analysis_inputs"] = _declared(3) + [
            {"path": "IM0.d", "kind": "vendor_folder", "suffix": ".d", "format": "bruker", "member_count": 9}]
        excluded = [{"path": paths[3], "reason": "ion_mobility_out_of_scope"}]
        groups = [paths[:2] + (paths[3:] if into_part else []), paths[2:3]]
        parent_manifest = parent.root / "provenance" / "run-manifest.json"
        parent.manifest.update(
            status="split_by_acquisition", execution_allowed=False, split_excluded_inputs=excluded,
            campaign_disposition=_disposition(excluded, kind="split"),
            split_into=[{"analysis_unit_id": f"unit-{mode}", "input_candidates": group}
                        for mode, group in zip(("dda", "dia"), groups)])
        parent.write()
        workspaces = []
        for mode, group in zip(("dda", "dia"), groups):
            part = Unit(temporary, f"unit-{mode}")
            part.manifest["project"] = dict(parent.manifest["project"], analysis_unit_id=f"unit-{mode}")
            part.manifest.update(input_candidates=group, raw_directory=str(parent.root / "raw"),
                                 raw_owned_by=str(parent_manifest),
                                 split_from={"manifest_path": str(parent_manifest), "analysis_unit_id": "unit"})
            part.csv([row for row in part.rows() if not row["file_path"].endswith("IM0.d")])
            workspaces.append(part.write())
        return workspaces[0], workspaces[1]

    def test_an_input_the_parent_excluded_is_in_no_part(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first, second = self._split_excluding(temporary)
            reports = [verifier.verify(workspace, "before-production") for workspace in (first, second)]

        for report in reports:
            partition = _check(report, "SPL-1")
            self.assertEqual(verifier.PASS, partition.status, partition.detail)
            self.assertIn("holding the parent's 3 inputs exactly once, the 1 its campaign disposition excluded in none",
                          partition.detail)
            self.assertEqual(verifier.PASS, _check(report, "INP-1").status, _check(report, "INP-1").detail)
            self.assertEqual(verifier.PASS, _check(report, "CNT-1").status, _check(report, "CNT-1").detail)

    def test_a_part_that_holds_an_input_the_parent_excluded_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first, _ = self._split_excluding(temporary, into_part=True)
            check = _check(verifier.verify(first, "before-production"), "SPL-1")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("the parts hold 1 input(s) the parent's campaign disposition excluded (IM0.d)", check.detail)

    def _split(self, temporary: str, *, declared: int, own: int | None = None) -> Path:
        """A parent of three folders split into parts of two and one.

        Each part's project is a copy of its parent's; with own, its analysis_inputs are only its own
        samples' (the first own of the parent's), as the split carries them where the Catalog declared them.
        """
        parent = Unit(temporary, "unit")
        paths = parent.inputs(["S0.raw", "S1.raw", "S2.raw"])
        parent.manifest["project"]["analysis_inputs"] = _declared(declared)
        parent.manifest.update(status="split_by_acquisition", execution_allowed=False)
        parent_manifest = parent.root / "provenance" / "run-manifest.json"
        parent.write()
        part = Unit(temporary, "unit-dda")
        part.manifest["project"] = dict(parent.manifest["project"], analysis_unit_id="unit-dda")
        if own is not None:
            part.manifest["project"]["analysis_inputs"] = _declared(declared)[:own]
        part.manifest.update(input_candidates=paths[:2], raw_directory=str(parent.root / "raw"),
                             split_from={"manifest_path": str(parent_manifest), "analysis_unit_id": "unit"})
        part.csv(part.rows())
        return part.write()

    def test_a_part_that_declares_its_own_samples_inputs_is_held_to_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            check = _check(verifier.verify(self._split(temporary, declared=3, own=2), "before-production"), "INP-1")

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(2, check.evidence["counts"]["part analysis_inputs"])
        self.assertIn("this part declares 2 of its own samples'", check.detail)

    def test_a_parts_list_is_not_held_to_the_count_it_copied_from_its_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self._split(temporary, declared=3, own=2)
            manifest_path = workspace / "provenance" / "run-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["project"]["analysis_input_count"] = 3
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            check = _check(verifier.verify(workspace, "before-production"), "INP-1")

        self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_a_part_whose_own_declaration_lost_an_input_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            check = _check(verifier.verify(self._split(temporary, declared=3, own=1), "before-production"), "INP-1")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("the part declares 1 analysis input(s) of its own samples and holds 2", check.detail)

    def test_a_split_part_is_judged_against_its_parents_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            check = _check(verifier.verify(self._split(temporary, declared=3), "before-production"), "INP-1")

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual(3, check.evidence["counts"]["parent input_candidates"])

    def test_a_split_parent_that_found_other_inputs_is_refused_in_its_part(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            check = _check(verifier.verify(self._split(temporary, declared=5), "before-production"), "INP-1")

        self.assertEqual(verifier.FAIL, check.status)


# ---- ACQ-1 -----------------------------------------------------------------------------------------


class AcquisitionTypeTests(unittest.TestCase):
    """ACQ-1. Each CSV row runs as the Console acquisition type its file's header gives."""

    def _acq1(self, modes: list[tuple[str, "str | None | bool"]], csv_types: list[str], *,
              extractor: list[tuple[str, int]] | None = None, names: list[str] | None = None):
        """modes: per file (header acquisition_mode, console_acquisition_type or False for a legacy record);
        extractor: per file (method, isolation targets) the extractor wrote, or None for no output file."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = Unit(temporary)
            paths = unit.inputs(names or [f"S{index}.mzML" for index in range(len(modes))])
            unit.preflight(
                [_record(path, mode, console) for path, (mode, console) in zip(paths, modes)],
                extractor=None if extractor is None else [
                    _extracted(path, method, windows=windows) for path, (method, windows) in zip(paths, extractor)],
            )
            unit.csv(unit.rows(csv_types))
            return _check(verifier.verify(unit.write(), "before-production"), "ACQ-1")

    def test_rows_that_match_their_console_types_pass(self) -> None:
        check = self._acq1([("DDA", "DDA"), ("DIA", "SWATH"), ("AIF", "AIF")], ["DDA", "SWATH", "AIF"],
                           extractor=[("DDA", 40), ("DIA", 22), ("AIF", 1)])

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual({"console_acquisition_type": 3}, check.evidence["basis"])
        self.assertEqual({"header": 3}, check.evidence["sources"])

    def test_a_dia_row_is_refused_because_the_console_reads_it_as_dda(self) -> None:
        check = self._acq1([("DIA", "SWATH")], ["DIA"])

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("acquisition_type 'DIA' is not DDA, SWATH or AIF, and the Console reads it as DDA", check.detail)

    def test_an_mse_file_written_as_dda_is_refused(self) -> None:
        """Waters MSe is all-ion: its header says AIF, and a DDA row deconvolutes it as DDA."""
        check = self._acq1([("AIF", "AIF")], ["DDA"], extractor=[("AIF", 1)], names=["MSe_1.raw"])

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("MSe_1: its header gives AIF, and the Console will deconvolute it as DDA", check.detail)

    def test_a_row_other_than_its_console_type_is_refused(self) -> None:
        check = self._acq1([("DIA", "SWATH")], ["AIF"])

        self.assertEqual(verifier.FAIL, check.status)

    def test_a_windowed_dia_mapped_to_aif_is_refused_by_the_extractors_own_record(self) -> None:
        """Interactive's mapping and its CSV agree, and the extractor recorded 22 isolation windows."""
        check = self._acq1([("DIA", "AIF")], ["AIF"], extractor=[("DIA", 22)])

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("22 isolation targets", check.detail)

    def test_a_record_that_never_resolved_dia_is_refused(self) -> None:
        check = self._acq1([("DIA", "DIA")], ["SWATH"])

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("never resolved", check.detail)

    def test_a_file_whose_header_gives_no_console_type_warns(self) -> None:
        """A Waters DDA folder read as Unknown runs on its repository declaration."""
        check = self._acq1([("Unknown", None)], ["DDA"], names=["R00_1_FDDAn.raw"])

        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("R00_1_FDDAn", check.detail)

    def test_a_contract_value_spelt_otherwise_is_refused(self) -> None:
        """The Console would read 'swath' as SWATH; the column takes DDA, SWATH or AIF and nothing else."""
        check = self._acq1([("DIA", "SWATH")], ["swath"])

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("reads it as SWATH", check.detail)

    def test_a_legacy_dia_record_is_settled_by_the_extractors_windows(self) -> None:
        """MTBLS2207's DIA part: 'DIA' per file, 41 or 42 isolation targets, SWATH in the CSV."""
        check = self._acq1([("DIA", False)] * 2, ["SWATH"] * 2, extractor=[("DIA", 42), ("DIA", 41)])

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual({"acquisition_mode": 2}, check.evidence["basis"])

    def test_a_legacy_dia_record_without_windows_warns(self) -> None:
        check = self._acq1([("DIA", False)], ["SWATH"])

        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("predates console_acquisition_type", check.detail)

    def test_a_legacy_dda_header_run_as_swath_is_refused(self) -> None:
        check = self._acq1([("DDA", False)], ["SWATH"])

        self.assertEqual(verifier.FAIL, check.status)

    def test_a_legacy_dia_header_run_as_dda_is_refused(self) -> None:
        check = self._acq1([("DIA", False)], ["DDA"])

        self.assertEqual(verifier.FAIL, check.status)

    def test_a_row_no_header_record_names_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = Unit(temporary)
            paths = unit.inputs(["S0.mzML", "S1.mzML"])
            unit.preflight([_record(paths[0], "DDA", "DDA")])
            unit.csv(unit.rows())
            check = _check(verifier.verify(unit.write(), "before-production"), "ACQ-1")

        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("S1: no raw-header record names it", check.detail)

    def test_without_a_preflight_it_is_not_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = Unit(temporary)
            unit.inputs(["S0.mzML"])
            unit.csv(unit.rows())
            check = _check(verifier.verify(unit.write(), "before-production"), "ACQ-1")

        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertFalse(check.required)
        self.assertIn("Every row's acquisition_type is one the Console parses (DDA 1)", check.detail)

    def _unread(self, values: list[str], **project) -> "verifier.Check":
        """A unit whose mode came from the repository, with no per-file raw-header record."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = Unit(temporary)
            unit.inputs([f"S{index}.mzML" for index in range(len(values))])
            unit.manifest["project"].update(project)
            unit.csv(unit.rows(values))
            return _check(verifier.verify(unit.write(), "before-production"), "ACQ-1")

    def test_without_a_preflight_a_value_outside_the_console_types_is_still_refused(self) -> None:
        """MTBLS341, ST002419 and MPST000007 never needed a header read; the Console reads 'DIA' as DDA all the same."""
        for value, read_as in (("DIA", "DDA"), ("SWATH ", "SWATH"), ("", "DDA"), ("MSE", "DDA"), ("None", "None")):
            with self.subTest(value=value):
                check = self._unread(["DDA", value])

                self.assertEqual(verifier.FAIL, check.status)
                self.assertTrue(check.required)
                self.assertIn(f"S1: acquisition_type {value!r} is not DDA, SWATH or AIF, and the Console reads it as "
                              f"{read_as}", check.detail)
                self.assertIn("Of the 2 row(s), 1 carry an acquisition_type other than DDA, SWATH or AIF", check.detail)

    def test_a_gcms_unit_may_run_as_none(self) -> None:
        """ST002419: a GC-MS unit's CSV says None, which the Console parses; no header comparison is made."""
        check = self._unread(["None"] * 2, separation="GC-MS", acquisition_mode="FullScan")
        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertFalse(check.required)
        self.assertIn("GC-MS unit", check.detail)

        with tempfile.TemporaryDirectory() as temporary:
            unit = Unit(temporary)
            paths = unit.inputs(["G0.cdf"])
            unit.manifest["project"].update(separation="GC-MS", acquisition_mode="FullScan")
            unit.preflight([_record(paths[0], "FullScan", False)])
            unit.csv(unit.rows(["None"]))
            check = _check(verifier.verify(unit.write(), "before-production"), "ACQ-1")
        self.assertEqual(verifier.NOT_EVALUABLE, check.status, check.detail)
        self.assertFalse(check.required)

        check = self._unread(["DIA"], separation="GC-MS")
        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("is not DDA, SWATH, AIF or None", check.detail)

    def test_an_lc_unit_run_as_none_is_refused(self) -> None:
        check = self._acq1([("DDA", "DDA")], ["None"])

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("'None' is not DDA, SWATH or AIF, and the Console reads it as None", check.detail)

    def _decided(self, header: str, confidence: "float | None", console: "str | None", csv_type: str, *,
                 basis: str = "declaration", windows: int | None = None, applied: bool = True,
                 declared: str | None = None) -> "verifier.Check":
        """One file whose Console type a campaign disposition decided (_apply_disposition), as Interactive
        records it: console_acquisition_type and its basis on the per-file record, and, where the header
        disagreed with the declaration, a declared_vs_header entry."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = Unit(temporary)
            paths = unit.inputs(["S0.mzML"])
            unit.preflight([_record(paths[0], header, console, confidence=confidence,
                                    console_acquisition_basis=basis)],
                           extractor=None if windows is None else [_extracted(paths[0], header, windows=windows)])
            disagreement = {"file": paths[0], "declared": declared, "header": header, "confidence": confidence,
                            "decided": declared}
            unit.manifest["campaign_disposition"] = _disposition(
                [], applied=applied, declared_vs_header=[] if declared is None else [disagreement])
            unit.csv(unit.rows([csv_type]))
            return _check(verifier.verify(unit.write(), "before-production"), "ACQ-1")

    def test_a_weak_header_the_disposition_kept_the_declaration_over_is_a_warning(self) -> None:
        """classify_preflight keeps the repository's declaration over a header below 0.8 that contradicts it,
        and Interactive's execution gate admits the decided type. Whether a weak header should outrank the
        declaration is a scientific decision; ACQ-1 names it, and refused every such unit."""
        for header, confidence, console, declared, windows in (("DIA", 0.5, "DDA", "DDA", 22),
                                                               ("DDA", 0.75, "SWATH", "DIA", None)):
            with self.subTest(header=header):
                check = self._decided(header, confidence, console, console, windows=windows, declared=declared)

                self.assertEqual(verifier.WARN, check.status, check.detail)
                self.assertIn(f"S0: its header says {header}, and the Console will deconvolute it as {console}, at "
                              f"confidence {confidence:.2f}, below the 0.8 at which a header replaces the repository's "
                              f"declaration, so the campaign disposition kept the declared {declared}", check.detail)
                self.assertEqual({"declaration": 1}, check.evidence["sources"])

    def test_a_contradicting_header_of_no_recorded_confidence_does_not_outrank_the_declaration(self) -> None:
        """classify_preflight reads a missing confidence as 0, so the declaration it kept is named, not refused."""
        check = self._decided("DIA", None, "DDA", "DDA", declared="DDA")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("at no recorded confidence, short of the 0.8", check.detail)

    def test_a_confident_header_the_declaration_contradicts_is_refused(self) -> None:
        check = self._decided("DIA", 0.9, "DDA", "DDA", declared="DDA")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("S0: its header says DIA, and the Console will deconvolute it as DDA", check.detail)

    def test_a_contradiction_by_the_header_the_type_came_from_is_refused_at_any_confidence(self) -> None:
        check = self._decided("DIA", 0.5, "AIF", "AIF", basis="header_no_isolation", windows=22)

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("22 isolation targets", check.detail)

    def test_a_type_taken_from_the_declaration_is_not_called_the_headers(self) -> None:
        """A Waters DDA read as Unknown at 0.30: the disposition assigns the declared DDA, and ACQ-1 said
        'All 1 row(s) run as the acquisition type their raw header gives'."""
        check = self._decided("Unknown", 0.3, "DDA", "DDA")

        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("S0: its DDA is the repository's declaration, which the campaign disposition kept where its "
                      "header gives Unknown at confidence 0.30", check.detail)
        self.assertIn("1 from the repository declaration", check.detail)
        self.assertEqual({"declaration": 1}, check.evidence["sources"])

    def test_an_ms1_only_file_folded_into_a_dda_run_is_a_warning(self) -> None:
        check = self._decided("FullScan", 0.9, "DDA", "DDA", basis="folded_ms1_only")

        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("folded it into the DDA run", check.detail)
        self.assertEqual({"folded_ms1_only": 1}, check.evidence["sources"])

    def test_a_campaign_row_with_no_console_type_is_refused(self) -> None:
        """Still unknown: do not run (user decision, 2026-09-30). Outside a campaign it stays a warning."""
        check = self._decided("DIA", 0.9, None, "SWATH", basis="")
        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("still unknown does not run", check.detail)

        check = self._decided("DIA", 0.9, None, "SWATH", basis="", applied=False)
        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("its header gives no Console acquisition type", check.detail)

    def test_a_split_part_reads_the_header_verdicts_it_carried_from_its_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = Unit(temporary)
            paths = unit.inputs(["S0.d", "S1.d"])
            unit.manifest["header_verdicts_from_parent"] = [_record(path, "DDA", "DDA") for path in paths]
            unit.csv(unit.rows(["DDA", "SWATH"]))
            check = _check(verifier.verify(unit.write(), "before-production"), "ACQ-1")

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("S1: its header gives DDA", check.detail)

    def _aliased(self, temporary: str, link: str) -> "verifier.Check":
        """S,0.mzML reaches the Console as raw/console-aliases/S_0-1a2b3c4d.mzML; link: hard, other or none."""
        unit = Unit(temporary)
        paths = unit.inputs(["S,0.mzML", "S1.mzML"])
        unit.preflight([_record(path, "AIF", "AIF") for path in paths])
        alias = unit.root / "raw" / "console-aliases" / "S_0-1a2b3c4d.mzML"
        alias.parent.mkdir(parents=True)
        if link == "hard":
            os.link(paths[0], alias)
        elif link == "other":
            alias.write_bytes(b"another file")
        unit.manifest["input_lineage"] = {"schema": "msdial-input-lineage.v1", "rows": [
            {"path": paths[0], "kind": "file", "console_path": str(alias),
             "console_alias": {"path": str(alias), "kind": "hardlink", "target": paths[0]}},
            {"path": paths[1], "kind": "file", "console_path": paths[1]}]}
        rows = unit.rows(["AIF", "DDA"])
        rows[0].update(file_path=str(alias), file_name=alias.stem)
        unit.csv(rows)
        return _check(verifier.verify(unit.write(), "before-production"), "ACQ-1")

    def test_a_row_naming_a_console_alias_is_judged_by_the_input_it_stands_for(self) -> None:
        """A name the Console's CSV parser cannot read back is given to it through raw/console-aliases."""
        for link in ("hard", "none"):
            with self.subTest(link=link), tempfile.TemporaryDirectory() as temporary:
                check = self._aliased(temporary, link)

                self.assertEqual(verifier.FAIL, check.status)
                self.assertEqual({"console_acquisition_type": 2}, check.evidence["basis"], "the alias found its record")
                self.assertEqual(1, len(check.evidence["failures"]))
                self.assertIn("S1: its header gives AIF", check.detail)

    def test_an_alias_that_is_another_file_stands_for_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            check = self._aliased(temporary, "other")

        self.assertEqual({"console_acquisition_type": 1, "no_record": 1}, check.evidence["basis"])
        self.assertIn("S_0-1a2b3c4d: no raw-header record names it", " ".join(check.evidence["warnings"]))


# ---- AIF-1 -----------------------------------------------------------------------------------------


class AifCollisionEnergyTests(unittest.TestCase):
    """AIF-1. A WARN, never a FAIL, for an AIF file the Console cannot deconvolute (decided 2026-09-30)."""

    def _aif1(self, energies: list[list | None], *, csv_types: list[str] | None = None, log: str | None = None,
              log_tail: list[str] | None = None, per_file: list[list] | None = None):
        with tempfile.TemporaryDirectory() as temporary:
            unit = Unit(temporary)
            paths = unit.inputs([f"AIF_{index}.d" for index in range(len(energies))])
            records = [_record(path, "AIF", "AIF", **({"collision_energies": per_file[index]} if per_file else {}))
                       for index, path in enumerate(paths)]
            unit.preflight(records, extractor=[_extracted(path, "AIF", energies=values)
                                               for path, values in zip(paths, energies) if values is not None])
            unit.csv(unit.rows(csv_types or ["AIF"] * len(paths)))
            if log is not None:
                (unit.output / "msdial-console.log").write_text(log, encoding="utf-8")
            if log_tail is not None:
                unit.manifest["run_failures"] = [{"reason": "exit 1", "exit_code": 1, "log_tail": log_tail}]
            return _check(verifier.verify(unit.write(), "after-run"), "AIF-1")

    def test_an_aif_file_with_an_empty_target_list_warns_and_is_named(self) -> None:
        """The Console skips it without a line: no MS2 deconvolution at all."""
        check = self._aif1([[10.0, 20.0], []])

        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("AIF_1", check.detail)
        self.assertNotIn("AIF_0", check.detail)
        self.assertEqual(["AIF_1"], check.evidence["without_usable_target"])

    def test_the_consoles_line_in_a_kept_log_warns_and_names_the_one_aif_file(self) -> None:
        check = self._aif1([[10.0]], log="Start processing..\nNo correct CE information in AIF-MSDEC\n")

        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("1 time(s)", check.detail)
        self.assertIn("for AIF_0", check.detail)
        self.assertEqual(["msdial-console.log"], check.evidence["console_logs_read"])

    def test_the_consoles_line_in_a_failed_runs_log_tail_warns(self) -> None:
        check = self._aif1([[10.0], [20.0]], log_tail=["No correct CE information in AIF-MSDEC"] * 2)

        self.assertEqual(verifier.WARN, check.status)
        self.assertEqual(2, check.evidence["console_log_lines"])
        self.assertIn("neither the log nor the records say which", check.detail)

    def test_the_line_is_attributed_to_the_file_whose_targets_include_one_at_or_below_zero(self) -> None:
        check = self._aif1([[10.0], [20.0]], per_file=[[10.0], [0.0, 20.0]],
                           log="No correct CE information in AIF-MSDEC\n")

        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("for AIF_1", check.detail)

    def test_aif_files_with_targets_pass(self) -> None:
        check = self._aif1([[10.0, 20.0], [30.0]])

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIn("no Console log is kept", check.detail)

    def test_a_target_the_console_rounds_to_zero_is_no_target(self) -> None:
        check = self._aif1([[0.004]])

        self.assertEqual(verifier.WARN, check.status)

    def test_it_is_never_a_failure_and_never_required(self) -> None:
        for energies, log in (([[]], "No correct CE information in AIF-MSDEC\n"), ([None], None), ([[]], None)):
            with self.subTest(energies=energies, log=log):
                check = self._aif1(energies, log=log)
                self.assertNotEqual(verifier.FAIL, check.status)
                if check.status == verifier.NOT_EVALUABLE:
                    self.assertFalse(check.required)

    def test_the_count_on_the_per_file_record_stands_for_the_extractors_file(self) -> None:
        """Interactive records collision_energy_count, the extractor's energies above 0, where no list is kept."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = Unit(temporary)
            paths = unit.inputs(["AIF_0.d", "AIF_1.d"])
            unit.preflight([_record(path, "AIF", "AIF", collision_energy_count=count)
                            for path, count in zip(paths, (0, 2))], extractor=None)
            unit.csv(unit.rows(["AIF", "AIF"]))
            check = _check(verifier.verify(unit.write(), "after-run"), "AIF-1")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual(["AIF_0"], check.evidence["without_usable_target"])
        self.assertEqual([], check.evidence["targets_unrecorded"])

    def test_targets_recorded_nowhere_are_not_evaluable(self) -> None:
        check = self._aif1([None])

        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertIn("AIF_0", check.detail)

    def test_a_unit_with_no_aif_file_is_not_evaluable_and_not_required(self) -> None:
        check = self._aif1([[]], csv_types=["SWATH"])

        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertFalse(check.required)


# ---- FIN-2 -----------------------------------------------------------------------------------------


def _hold(blocks: list[str], step: str, reason: str) -> dict:
    """A hold as run_finalisation._hold writes it."""
    return {"id": "0" * 32, "blocks": blocks, "step": step, "job_id": "job-1", "reason": reason,
            "recorded_at": "2026-09-30T12:00:00+09:00"}


class FinalisationHoldTests(unittest.TestCase):
    """FIN-2. A standing sharing hold refuses publication; a raw-deletion hold is for a person to read."""

    def _fin2(self, **manifest):
        with tempfile.TemporaryDirectory() as temporary:
            unit = Unit(temporary)
            unit.manifest.update(manifest)
            return _check(verifier.verify(unit.write(), "before-publish"), "FIN-2")

    def test_an_unredacted_mztab_blocks_publication(self) -> None:
        check = self._fin2(finalisation_holds=[_hold(
            ["sharing"], "mztab_redaction",
            "the mzTab-M could not be rewritten; it may still name a library or raw location of this machine")])

        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("mztab_redaction of run job-1", check.detail)

    def test_a_library_copy_still_in_the_output_blocks_publication(self) -> None:
        check = self._fin2(finalisation_holds=[_hold(
            ["sharing"], "loaded_library_copy", "MS-DIAL's copy of every loaded library is still in the output")])

        self.assertEqual(verifier.FAIL, check.status)

    def test_a_finalisation_that_stopped_blocks_both_and_is_a_failure(self) -> None:
        check = self._fin2(finalisation_holds=[_hold(["sharing", "raw_deletion"], "finalisation",
                                                     "finalisation stopped: OSError: locked")])

        self.assertEqual(verifier.FAIL, check.status)

    def test_containers_left_beside_the_inputs_warn(self) -> None:
        check = self._fin2(finalisation_holds=[_hold(
            ["raw_deletion"], "msdial_intermediates",
            "2 container(s) could not be moved out of the raw directory, and deleting it would delete them")])

        self.assertEqual(verifier.WARN, check.status)
        self.assertIn("block raw deletion", check.detail)

    def test_no_standing_hold_passes(self) -> None:
        check = self._fin2(finalisation_holds=[], finalisation_hold_resolutions=[{"hold": "1", "step": "x"}],
                           console_run_finalisation={"schema": "msdial-console-run-finalisation.v1", "holds": []})

        self.assertEqual(verifier.PASS, check.status)
        self.assertIn("1 earlier hold(s) were resolved on retry", check.detail)

    def test_a_legacy_manifest_is_not_evaluable_and_not_required(self) -> None:
        check = self._fin2()

        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertFalse(check.required)

    def test_a_hold_that_does_not_say_what_it_blocks_is_not_a_pass(self) -> None:
        check = self._fin2(finalisation_holds=[{"step": "mztab_redaction"}])

        self.assertEqual(verifier.NOT_EVALUABLE, check.status)
        self.assertTrue(check.required)


if __name__ == "__main__":
    unittest.main()
