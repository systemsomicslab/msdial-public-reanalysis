"""The gate on a unit switched from its .wiff2 to its .wiff after a Console failure (the user's decision of 2026-10-31).

The one encoding rule, held jointly with the Catalog, runs a sample from its .wiff2 wherever its .wiff lies beside it. The
positive part of MB-POST MPST000048 hung three times out of three in the .wiff2 reader while its files were read in
parallel, and Interactive 0.5.55 (msdial_app.wiff_fallback) now switches such a DDA unit to its .wiff, once, on record:
manifest.encoding_fallback (rule wiff2_to_wiff_after_console_failure_2026_10_31, the failed jobs, every swap, the input
candidates before) and an encoding_fallback on each .wiff's lineage row. A run of the .wiff without that record is a
departure from the rule, and INP-1 refuses it as before; with it, INP-1 accounts for each .wiff2 by its .wiff, ACQ-1 reads
each .wiff through its .wiff2's header record, and SPL-1 holds a switched part to its parent's record by its candidates
before the switch. A record that does not hold (a job that did not fail, a DIA unit, swaps that are not exactly what
changed, a .wiff2 left running) accepts nothing.

The fixture is test_verify_lineage_built_csv's FolderBranchUnit, a unit as Interactive's folder branch prepares it.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import test_verify_lineage_built_csv as built  # noqa: E402  (loads the gate as verify_run_invariants_built_csv)

verifier = built.verifier
_check = built._check
RULE = "wiff2_to_wiff_after_console_failure_2026_10_31"
STEMS = ("241202_Tomita_18_pos", "241202_Tomita_19_pos")


class WiffUnit(built.FolderBranchUnit):
    """A ZenoTOF DDA unit deposited as S.wiff2, S.wiff and S.wiff.scan, declared and run on its .wiff2."""

    def __init__(self, temporary: str, unit_id: str = "unit") -> None:
        super().__init__(temporary, unit_id)
        self.manifest["project"]["acquisition_mode"] = "DDA"
        for stem in STEMS:
            self.file(f"{stem}.wiff2", stem)
            self._download(self.data / f"{stem}.wiff", f"{stem}.wiff".encode(), verified=True)
            self._download(self.data / f"{stem}.wiff.scan", f"{stem}.wiff.scan".encode(), verified=True)
        self.manifest["run_attempts"] = [
            {"attempt_id": "a1", "job_id": "dg1", "kind": "tuning", "ended_at": "2026-10-30T00:10:00+00:00",
             "exit_code": 0, "reason": "exited"},
            {"attempt_id": "a2", "job_id": "rn1", "kind": "run", "ended_at": "2026-10-30T08:00:00+00:00",
             "exit_code": -3, "reason": "idle_timeout"},
        ]

    def _attribute_lineage(self) -> None:
        """build_input_lineage's attribution, and a .wiff's row keeps its .wiff2's sample as Interactive's switch
        writes it (the row is the switch's, not the lease's)."""
        super()._attribute_lineage()
        for row in self.manifest["input_lineage"]["rows"]:
            fallback = row.get("encoding_fallback")
            if isinstance(fallback, dict):
                row["sample_id"] = fallback["in_place_of_row"]["sample_id"]

    def switch(self, *, record: bool = True) -> None:
        """msdial_switch_repository_unit_to_wiff as Interactive 0.5.55 writes it; without ``record``, the inputs are
        switched and nothing says why."""
        before = list(self.manifest["input_candidates"])
        downloads = {item["path"]: item for item in self.manifest["downloads"]}
        swaps, rows = [], []
        for row in self.manifest["input_lineage"]["rows"]:
            source = row["path"]
            target = source[: -len(".wiff2")] + ".wiff"
            download = downloads[target]
            swaps.append({"from": source, "to": target, "companion": target + ".scan",
                          "from_relative": Path(source).name, "to_relative": Path(target).name,
                          "sample_id": row["sample_id"]})
            new = {"path": target, "kind": "file", "declared_names": [Path(target).name], "sample_id": row["sample_id"],
                   "file_name": "", "source": {"url": download["source_url"], "download_path": target},
                   "checksums": {"sha256": download["sha256"], "md5": download["md5"],
                                 "declared": download["declared_checksum"], "declared_algorithm": "md5",
                                 "declared_verified": True}}
            if record:
                new["encoding_fallback"] = {"rule": RULE, "in_place_of": source, "stands_for": source,
                                            "in_place_of_row": copy.deepcopy(row)}
            rows.append(new)
        self.manifest["input_lineage"]["rows"] = rows
        self.manifest["input_candidates"] = [item[: -len(".wiff2")] + ".wiff" for item in before]
        if record:
            self.manifest["encoding_fallback"] = {
                "schema": "msdial-encoding-fallback.v1", "rule": RULE, "switched_at": "2026-10-31T00:00:00+00:00",
                "acquisition_mode": "DDA", "swaps": swaps, "input_candidates_before": before,
                "failed_jobs": [{"job_id": "rn1", "kind": "run", "failure": "idle_timeout", "reason": "idle_timeout",
                                 "exit_code": -3}],
            }
        if not record:
            # Without its record, nothing names the .wiff2 the run left behind.
            for row in rows:
                row.pop("encoding_fallback", None)


def _gate(change=None, *, switch: bool = True, record: bool = True, before=None):
    with tempfile.TemporaryDirectory() as temporary:
        unit = WiffUnit(temporary)
        if before is not None:
            before(unit)
        if switch:
            unit.switch(record=record)
        if change is not None:
            change(unit)
        unit.prepare()
        return unit.gate()


class AWiffRunInPlaceOfItsWiff2(unittest.TestCase):
    def test_the_wiff2_run_passes_as_it_always_did(self) -> None:
        report = _gate(switch=False)
        self.assertEqual(verifier.PASS, _check(report, "INP-1").status, _check(report, "INP-1").detail)

    def test_the_wiff_run_on_record_passes_and_says_so(self) -> None:
        report = _gate()
        check = _check(report, "INP-1")
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIn(RULE, check.detail)
        for check_id in ("CLS-2", "SUM-1", "CNT-1"):
            self.assertEqual(verifier.PASS, _check(report, check_id).status, (check_id, _check(report, check_id).detail))

    def test_the_wiff_run_without_the_record_is_refused(self) -> None:
        report = _gate(record=False)
        check = _check(report, "INP-1")
        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn(f"{STEMS[0]}.wiff2 beside {STEMS[0]}.wiff", check.detail)

    def test_a_record_that_does_not_hold_accepts_nothing(self) -> None:
        def job_not_failed(unit) -> None:
            unit.manifest["encoding_fallback"]["failed_jobs"][0]["job_id"] = "dg1"

        def unknown_job(unit) -> None:
            unit.manifest["encoding_fallback"]["failed_jobs"][0]["job_id"] = "rn9"

        def dia(unit) -> None:
            unit.manifest["project"]["acquisition_mode"] = "SWATH"

        def another_rule(unit) -> None:
            unit.manifest["encoding_fallback"]["rule"] = "one_encoding_per_sample_2026_10_09"

        def more_than_the_swaps(unit) -> None:
            unit.manifest["encoding_fallback"]["input_candidates_before"].append(str(unit.data / "extra.wiff2"))

        def no_scan(unit) -> None:
            unit.manifest["encoding_fallback"]["swaps"][1]["companion"] = ""

        def row_says_nothing(unit) -> None:
            unit.manifest["input_lineage"]["rows"][0].pop("encoding_fallback")

        cases = {
            "did not fail or time out": job_not_failed, "record no Console job rn9": unknown_job,
            "the unit is not DDA": dia, "names the rule": another_rule,
            "not those before the switch with exactly its swaps made": more_than_the_swaps,
            "names no .wiff.scan beside its .wiff": no_scan, "does not say it runs in place of": row_says_nothing,
        }
        for said, change in cases.items():
            with self.subTest(said):
                check = _check(_gate(change), "INP-1")
                self.assertEqual(verifier.FAIL, check.status)
                self.assertIn("manifest.encoding_fallback does not hold", check.detail)
                self.assertIn(said, check.detail)

    def test_a_wiff2_left_running_beside_a_switched_one_is_refused(self) -> None:
        def half(unit) -> None:
            first = unit.manifest["encoding_fallback"]["swaps"][0]
            unit.manifest["encoding_fallback"]["swaps"] = [first]
            unit.manifest["input_candidates"][1] = unit.manifest["encoding_fallback"]["input_candidates_before"][1]
            row = unit.manifest["input_lineage"]["rows"][1]
            unit.manifest["input_lineage"]["rows"][1] = row["encoding_fallback"]["in_place_of_row"]

        check = _check(_gate(half), "INP-1")
        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("still run beside the switched ones", check.detail)

    def test_the_rules_choice_of_the_wiff2_is_met_by_its_wiff(self) -> None:
        """A sample whose .wiff2 the one encoding rule used over its mzML runs as its .wiff: the choice is met, the
        mzML stays unused, and without the record the choice's file runs on nothing."""
        def mzml(unit) -> None:
            project = unit.manifest["project"]
            for stem in STEMS:
                unit._download(unit.data / f"{stem}.mzML", f"{stem}.mzML".encode(), verified=True)
                # The Catalog declares both encodings of the sample, and the lease's rule uses one.
                project["analysis_inputs"].append({"path": f"{stem}.mzML", "kind": "file", "sample_id": stem})
                project["analysis_input_count"] = len(project["analysis_inputs"])
                choice = {"rule": verifier.ENCODING_RULE, "used": f"{stem}.wiff2",
                          "unused": [{"path": f"{stem}.mzML", "reason": verifier.LOWER_IN_ENCODING_ORDER}]}
                unit.manifest.setdefault("encoding_choices", []).append(choice)
                row = next(item for item in unit.manifest["input_lineage"]["rows"]
                           if Path(item["path"]).name == f"{stem}.wiff2")
                row["encoding_choice"] = dict(choice)

        unswitched = _gate(before=mzml, switch=False)
        self.assertEqual(verifier.PASS, _check(unswitched, "INP-1").status, _check(unswitched, "INP-1").detail)
        switched = _check(_gate(before=mzml), "INP-1")
        self.assertEqual(verifier.PASS, switched.status, switched.detail)
        refused = _check(_gate(before=mzml, record=False), "INP-1")
        self.assertEqual(verifier.FAIL, refused.status)
        self.assertIn("its sample runs on nothing", refused.detail)


class ReadAsItsWiff2(unittest.TestCase):
    def test_each_wiff_is_read_through_its_wiff2s_header_record(self) -> None:
        def preflight(unit) -> None:
            unit.manifest["raw_metadata_preflight"] = {"summary": {"per_file": [
                {"file": path, "outcome": "unsupported_format", "format": "sciex_wiff2", "acquisition_mode": "",
                 "polarity": "", "header_console_acquisition_type": None, "console_acquisition_type": "DDA"}
                for path in unit.manifest["input_candidates"]]}}

        provenance = None
        with tempfile.TemporaryDirectory() as temporary:
            unit = WiffUnit(temporary)
            preflight(unit)
            unit.switch()
            provenance = json.loads(json.dumps(unit.manifest))
            records = verifier._per_file_records(provenance)
            for stem in STEMS:
                record = records.get(verifier._path_key(str(unit.data / f"{stem}.wiff")))
                self.assertIsNotNone(record)
                self.assertTrue(record["file"].endswith(f"{stem}.wiff2"))
            provenance["encoding_fallback"]["failed_jobs"][0]["job_id"] = "dg1"
            records = verifier._per_file_records(provenance)
            self.assertNotIn(verifier._path_key(str(unit.data / f"{STEMS[0]}.wiff")), records,
                             "a record that does not hold reads nothing through the .wiff2")


class ASwitchedSplitPart(unittest.TestCase):
    def _part(self, temporary: str, *, record: bool = True):
        root = Path(temporary)
        part = WiffUnit(temporary, "unit-dda-pos")
        parent = root / "unit" / "provenance" / "run-manifest.json"
        parent.parent.mkdir(parents=True)
        parent.write_text(json.dumps({
            "project": {"analysis_unit_id": "unit"}, "status": "split_by_acquisition", "execution_allowed": False,
            "input_candidates": list(part.manifest["input_candidates"]), "raw_directory": str(part.raw),
            "split_into": [{"analysis_unit_id": "unit-dda-pos", "input_candidates": list(part.manifest["input_candidates"])}],
        }), encoding="utf-8")
        part.manifest.update(split_from={"manifest_path": str(parent), "analysis_unit_id": "unit"},
                             raw_owned_by=str(parent))
        part.switch(record=record)
        return part

    def test_a_part_switched_on_record_still_partitions_its_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = verifier.Report(Path(temporary))
            verifier.check_split_part_partitions_its_parent(report, self._part(temporary).manifest, "")
        check = _check(report, "SPL-1")
        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIn(RULE, check.detail)

    def test_a_part_switched_without_the_record_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = verifier.Report(Path(temporary))
            verifier.check_split_part_partitions_its_parent(report, self._part(temporary, record=False).manifest, "")
        check = _check(report, "SPL-1")
        self.assertEqual(verifier.FAIL, check.status)
        self.assertIn("differ from the parent's record of it", check.detail)


if __name__ == "__main__":
    unittest.main()
