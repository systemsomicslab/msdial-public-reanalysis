"""The gate on the user's answer of 2026-10-08 to the extra question: a readable twin runs as the sample whose own
mzML cannot be decoded ("A: 読める方をそのサンプルとして使う").

Interactive 0.5.36 (msdial-interactive-app#69) takes it so. Where the unit admitted a sample's mzML (a sample row
names it, exactly or by a pairing rule), RawDataHandler cannot decode it, and an unpaired twin of that sample can be
read (a vendor file, folder or container, or in a campaign an mzXML the convert stage converts), the twin the
encoding order takes runs as THAT SAMPLE'S OWN input, paired to its row and in its Class, never as an unattributed
member. Its records:

- the twin's lineage row carries replaces_undecodable {path (the mzML on disk), reason undecodable_mzml, rule
  readable_twin_runs_as_the_sample_2026_10_08, other_paths where the unit admitted the mzML in two folders}, and the
  sample of the mzML it replaces;
- the mzML is a lease-excluded row (unsupported_mzml_encoding) whose exclusion, like its excluded_input_candidates
  entry, names the input that runs in its place (replaced_by);
- unattributed_members.left_out lists the twin as analysed_for_an_admitted_sample, with stands_for the mzML and
  stands_for_reason undecodable_mzml, and unattributed_members.replaced_undecodable lists the mzML (path, reason
  undecodable_mzml, replaced_by the twin's member path, and replaced_by_input the input that runs, relative to the
  data root, or replacement_excluded where the twin did not run).

INP-1 (blocks_run) accepts the twin where those records agree, and FAILs a twin, or anything else, that reaches the
run without them. PAIR-1 (record_only) lists each twin and holds the record itself. CONV-1 holds a converted twin to
the mzXML the record names.

The fixtures are test_verify_gate_decisions_2026_10_08's (FolderBranchUnit, an undeclared unit in one unit-scoped
archive).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import test_verify_gate_decisions_2026_10_08 as d08  # noqa: E402

d07 = d08.d07
earlier = d08.earlier
verifier = d08.verifier
_check = d08._check

TWIN_RULE = "readable_twin_runs_as_the_sample_2026_10_08"


class _TwinUnit(d08._Unit):
    """build_input_lineage of 0.5.36: a twin that replaces an undecodable mzML takes that mzML's sample."""

    def _attribute_lineage(self) -> None:
        super()._attribute_lineage()
        lineage = self.manifest["input_lineage"]
        names: dict[str, set[str]] = {}
        for sample in self.manifest["project"]["sample_metadata"]:
            names.setdefault(str(sample.get("raw_file") or "").casefold(), set()).add(sample["sample_id"])
        for row in lineage["excluded"]:
            pairing = row.get("name_pairing")
            if pairing and not row["sample_id"]:
                matched = names.get(pairing["declared_raw_file"].casefold()) or set()
                row["sample_id"] = next(iter(matched)) if len(matched) == 1 else ""
        samples = {Path(row["path"]).name.casefold(): row.get("sample_id") or "" for row in lineage["excluded"]}
        for row in lineage["rows"]:
            replaces = row.get("replaces_undecodable")
            if isinstance(replaces, dict):
                row["sample_id"] = samples.get(Path(str(replaces.get("path") or "")).name.casefold(), "")


def _undeclared(unit: d07._UnattributedUnit) -> None:
    project = unit.manifest["project"]
    project["analysis_inputs"] = []
    project["analysis_inputs_declared"] = False
    project.pop("analysis_input_count", None)


def _plain_twin(unit: d07._UnattributedUnit, name: str) -> str:
    """A readable member of the archive, admitted as an input (no name_pairing of its own)."""
    download = unit._download(unit.data / name, name.encode("utf-8"), verified=True)
    path = download["path"]
    unit.manifest["input_candidates"].append(path)
    unit.manifest["input_lineage"]["rows"].append(
        {"path": path, "kind": "file", "declared_names": [], "sample_id": "", "file_name": "",
         "source": {"url": download["source_url"], "download_path": path},
         "checksums": {"sha256": download["sha256"], "md5": download["md5"], "declared": download["md5"],
                       "declared_algorithm": "md5", "declared_verified": True}})
    return path


def _row(unit: d07._UnattributedUnit, path: str, part: str = "rows") -> dict:
    return next(row for row in unit.manifest["input_lineage"][part] if row["path"] == path)


def _twin_unit(temporary: str, *, twin: str = "S1.raw", converted: bool = False, mzml: str = "S1.mzML",
               record: bool = True, shared: int = 1) -> "tuple[_TwinUnit, str, str]":
    """An undeclared unit of two samples in one archive: S2.mzML runs; S1's mzML cannot be decoded, and its readable
    twin runs as S1 (a vendor file, or with converted an mzXML converted by the campaign's convert stage), with
    every record 0.5.36 writes of it. Returns the unit, the twin's input path and the mzML's path."""
    unit = _TwinUnit(temporary)
    unit.file("S2.mzML", "S2", "B")
    undecodable = unit.lease_excluded(mzml, "S1", "A")
    _undeclared(unit)
    if converted:
        twin = Path(twin).stem + ".mzXML"
        path = d08._converted_member(unit, twin, "A", unattributed=False)
    else:
        path = _plain_twin(unit, twin)
    _row(unit, path)["replaces_undecodable"] = {"path": undecodable, "reason": "undecodable_mzml", "rule": TWIN_RULE}
    for item in unit.manifest["excluded_input_candidates"]:
        if item["path"] == undecodable:
            item["replaced_by"] = path
    _row(unit, undecodable, "excluded")["exclusion"]["replaced_by"] = path
    earlier._archive(unit, ["S2.mzML", mzml, twin], shared=shared)
    unit._attribute_lineage()
    if record:
        unit.manifest["unattributed_members"] = {
            "rule": d07.MEMBER_RULE, "applied": shared == 1, "count": 0, "members": [], "paths": [],
            **({"scope": "bundle_urls_unit_scoped"} if shared == 1 else {"reason": "shared_archive"}),
            "left_out": [d08._left(twin, "analysed_for_an_admitted_sample", stands_for=mzml,
                                   stands_for_reason="undecodable_mzml")],
            "left_out_count": 1,
            "replaced_undecodable": [{
                "member_name": Path(mzml).name, "path": mzml, "reason": "undecodable_mzml", "replaced_by": twin,
                "replaced_by_input": verifier._aif_input_key(path, unit.data)}],
        }
    return unit, path, undecodable


def _gate(unit: d07._UnattributedUnit):
    unit.prepare()
    return unit.gate()


def _record(unit: d07._UnattributedUnit) -> dict:
    return unit.manifest["unattributed_members"]


class ReadableTwinRunsAsTheSampleTests(unittest.TestCase):
    """The twin runs as the sample whose own mzML cannot be decoded, in its Class, where the records say so."""

    def test_a_vendor_twin_runs_as_the_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _path, _mzml = _twin_unit(temporary)
            report = _gate(unit)

        statuses = {check.check_id: check.status for check in report.checks}
        self.assertEqual([], report.run_blocked_by, {key: statuses.get(key) for key in report.run_blocked_by})
        self.assertEqual(verifier.PASS, statuses["CLS-2"], _check(report, "CLS-2").detail)
        self.assertEqual(verifier.PASS, statuses["CONV-1"], _check(report, "CONV-1").detail)
        inp1 = _check(report, "INP-1")
        self.assertEqual(verifier.NOT_EVALUABLE, inp1.status, inp1.detail)
        self.assertIn("1 input(s) run as the sample whose own mzML RawDataHandler cannot decode, the readable twin "
                      f"taken in its place under the rule {TWIN_RULE}", inp1.detail)
        self.assertIn("S1.raw for S1.mzML (sample S1)", inp1.detail)
        self.assertEqual([{"input": "S1.raw", "member": "S1.raw", "replaces": ["S1.mzML"], "sample_id": "S1"}],
                         inp1.evidence["undecodable_mzml_twins"]["readable_twins"])
        pair1 = _check(report, "PAIR-1")
        self.assertEqual(verifier.WARN, pair1.status, pair1.detail)
        self.assertIn("S1.raw for S1.mzML (sample S1)", pair1.detail)
        self.assertEqual(["S1.raw"], [item["input"] for item in pair1.evidence["readable_twins"]])

    def test_the_twin_is_in_the_samples_class(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, path, _mzml = _twin_unit(temporary)
            rows = unit.prepare()
        self.assertEqual("A", next(row["class_id"] for row in rows if row["input_path"] == path))

    def test_a_converted_mzxml_twin_runs_as_the_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _path, _mzml = _twin_unit(temporary, converted=True)
            report = _gate(unit)

        statuses = {check.check_id: check.status for check in report.checks}
        self.assertEqual([], report.run_blocked_by, {key: statuses.get(key) for key in report.run_blocked_by})
        conv1 = _check(report, "CONV-1")
        self.assertEqual(verifier.PASS, conv1.status, conv1.detail)
        self.assertIn("1 converted input(s) run as the sample whose own mzML RawDataHandler cannot decode, each the "
                      "conversion of the twin the record names (S1.mzML from S1.mzXML for S1.mzML)", conv1.detail)
        self.assertEqual(["S1.mzML from S1.mzXML for S1.mzML"], conv1.evidence["converted_readable_twins"])
        inp1 = _check(report, "INP-1")
        self.assertEqual(verifier.NOT_EVALUABLE, inp1.status, inp1.detail)
        self.assertEqual("S1.mzXML", inp1.evidence["undecodable_mzml_twins"]["readable_twins"][0]["member"])

    def test_a_twin_of_a_prefixed_mzml_runs_as_that_members_sample(self) -> None:
        """The twin carries the pairing of the mzML it replaces; PAIR-1 lists it by its own name as well."""
        with tempfile.TemporaryDirectory() as temporary:
            unit, path, mzml = _twin_unit(temporary, twin="x_S1.raw", mzml="x_S1.mzML")
            pairing = {"paired_by": "prefixed_member_name", "declared_raw_file": "S1.mzML", "member_name": "x_S1.mzML"}
            for item in unit.manifest["project"]["sample_metadata"]:
                if item["sample_id"] == "S1":
                    item["raw_file"] = "S1.mzML"
            _row(unit, mzml, "excluded")["name_pairing"] = dict(pairing)
            _row(unit, path)["name_pairing"] = dict(pairing)
            report = _gate(unit)

        self.assertEqual([], report.run_blocked_by)
        pair1 = _check(report, "PAIR-1")
        self.assertEqual(verifier.WARN, pair1.status, pair1.detail)
        self.assertEqual([("x_S1.mzML", "S1.mzML", "x_S1.raw")],
                         [(item["member_name"], item["declared_raw_file"], item.get("input"))
                          for item in pair1.evidence["pairings"] if not item.get("excluded_by_the_lease")])
        self.assertIn("x_S1.raw for x_S1.mzML (sample S1)", pair1.detail)

    def test_a_twin_beside_unattributed_members_runs_as_the_sample(self) -> None:
        """ST001264's unit, with a sample whose own mzML cannot be decoded beside its readable twin."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = d08._unit(temporary)
            unit.__class__ = _TwinUnit
            mzml = unit.lease_excluded("S1.mzML", "S1", "All")
            _undeclared(unit)
            path = _plain_twin(unit, "S1.raw")
            _row(unit, path)["replaces_undecodable"] = {"path": mzml, "reason": "undecodable_mzml", "rule": TWIN_RULE}
            unit.manifest["excluded_input_candidates"][0]["replaced_by"] = path
            _row(unit, mzml, "excluded")["exclusion"]["replaced_by"] = path
            _record(unit).update(
                left_out=[d08._left("S1.raw", "analysed_for_an_admitted_sample", stands_for="S1.mzML",
                                    stands_for_reason="undecodable_mzml")], left_out_count=1,
                replaced_undecodable=[{"member_name": "S1.mzML", "path": "S1.mzML", "reason": "undecodable_mzml",
                                       "replaced_by": "S1.raw", "replaced_by_input": "S1.raw"}])
            report = _gate(unit)

        statuses = {check.check_id: check.status for check in report.checks}
        self.assertEqual([], report.run_blocked_by, {key: statuses.get(key) for key in report.run_blocked_by})
        self.assertEqual(verifier.WARN, statuses["PAIR-1"], _check(report, "PAIR-1").detail)
        self.assertIn("S1.raw for S1.mzML (sample S1)", _check(report, "PAIR-1").detail)


class WithoutTheRecordTheTwinIsRefusedTests(unittest.TestCase):
    """A twin, or the mzML it replaces, that reaches the run without the records that let it, stops the run."""

    def _refused(self, change, *said: str, check_id: str = "INP-1", **options) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, path, mzml = _twin_unit(temporary, **options)
            change(unit, path, mzml)
            report = _gate(unit)
            check = _check(report, check_id)
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        for text in said:
            self.assertIn(text, check.detail)
        self.assertIn(check_id, report.run_blocked_by)

    def test_no_record_at_all(self) -> None:
        self._refused(lambda unit, _path, _mzml: unit.manifest.pop("unattributed_members"),
                      "S1.raw runs for S1.mzML, and unattributed_members.left_out does not list it as "
                      "analysed_for_an_admitted_sample for that mzML with stands_for_reason undecodable_mzml",
                      "S1.raw runs for S1.mzML, which unattributed_members.replaced_undecodable does not list")

    def test_left_out_names_another_mzml(self) -> None:
        def change(unit, _path, _mzml):
            _record(unit)["left_out"][0]["stands_for"] = "S9.mzML"
        self._refused(change, "unattributed_members.left_out does not list it as analysed_for_an_admitted_sample")

    def test_replaced_undecodable_names_another_input(self) -> None:
        def change(unit, _path, _mzml):
            _record(unit)["replaced_undecodable"][0]["replaced_by_input"] = "S9.raw"
        self._refused(change, "S1.raw runs for S1.mzML as S1.raw, and unattributed_members.replaced_undecodable names "
                              "replaced_by 'S1.raw', replaced_by_input 'S9.raw'")

    def test_the_record_says_the_twin_did_not_run(self) -> None:
        def change(unit, _path, _mzml):
            entry = _record(unit)["replaced_undecodable"][0]
            entry.pop("replaced_by_input")
            entry["replacement_excluded"] = "not_an_input"
        self._refused(change, "and replacement_excluded 'not_an_input'")

    def test_the_twin_runs_unattributed(self) -> None:
        def change(unit, path, _mzml):
            _row(unit, path)["name_pairing"] = {"paired_by": "unattributed_member", "member_name": "S1.raw"}
        self._refused(change, "S1.raw runs as an unattributed member, where a twin that replaces S1.mzML runs as "
                              "that mzML's sample")

    def test_the_twin_runs_as_another_sample(self) -> None:
        def change(unit, path, _mzml):
            unit._attribute_lineage = lambda: None
            _row(unit, path)["sample_id"] = "S2"
        self._refused(change, "S1.raw runs as sample S2, and S1.mzML, the mzML it replaces, is sample S1's")

    def test_the_mzml_runs_beside_its_twin(self) -> None:
        def change(unit, _path, mzml):
            unit.manifest["excluded_input_candidates"] = []
            unit.manifest["input_candidates"].append(mzml)
            unit.manifest["input_lineage"]["rows"].append(
                {key: value for key, value in _row(unit, mzml, "excluded").items() if key != "exclusion"})
        self._refused(change, "S1.mzML reaches the run beside S1.raw, the twin that replaces it")

    def test_the_mzml_was_not_excluded_as_undecodable(self) -> None:
        def change(unit, _path, mzml):
            unit.manifest["excluded_input_candidates"][0]["reason"] = "ion_mobility_out_of_scope"
            _row(unit, mzml, "excluded")["exclusion"]["reason"] = "ion_mobility_out_of_scope"
        self._refused(change, "S1.raw runs for S1.mzML, which is no input the lease excluded as undecodable "
                              "(unsupported_mzml_encoding)")

    def test_the_lease_names_another_input_in_the_mzmls_place(self) -> None:
        def change(unit, _path, mzml):
            _row(unit, mzml, "excluded")["exclusion"]["replaced_by"] = str(unit.data / "S9.raw")
        self._refused(change, "the lease's record of S1.mzML names", "as replaced_by, not S1.raw")

    def test_another_rule(self) -> None:
        def change(unit, path, _mzml):
            _row(unit, path)["replaces_undecodable"]["rule"] = "largest_file"
        self._refused(change, "S1.raw replaces S1.mzML with reason 'undecodable_mzml' and rule 'largest_file'")

    def test_a_member_the_record_runs_for_an_undecodable_mzml_without_its_lineage(self) -> None:
        def change(unit, path, _mzml):
            _row(unit, path).pop("replaces_undecodable")
        self._refused(change, "S1.raw runs for S1.mzML, an undecodable mzML by unattributed_members.left_out, and its "
                              "lineage row does not say it replaces it (replaces_undecodable)")

    def test_a_converted_twin_the_record_names_otherwise_fails_conv1(self) -> None:
        def change(unit, _path, _mzml):
            _record(unit)["replaced_undecodable"][0]["replaced_by"] = "S1.raw"
        self._refused(change, "S1.mzML: it runs for S1.mzML, its sample's undecodable mzML, as the conversion of "
                              "S1.mzXML, and unattributed_members.replaced_undecodable names S1.raw as the twin",
                      check_id="CONV-1", converted=True)

    def test_a_declared_units_twin_that_runs_is_refused(self) -> None:
        """Interactive 0.5.36 runs no twin for a declared mzML (an open question for the user); one that runs
        reached the run without the record, which says the member is not named by the declaration."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = _TwinUnit(temporary)
            unit.file("S2.mzML", "S2", "B")
            mzml = unit.lease_excluded("S1.mzML", "S1", "A")
            path = _plain_twin(unit, "S1.raw")
            _row(unit, path)["replaces_undecodable"] = {"path": mzml, "reason": "undecodable_mzml", "rule": TWIN_RULE}
            earlier._archive(unit, ["S2.mzML", "S1.mzML", "S1.raw"])
            unit.manifest["unattributed_members"] = {
                "rule": d07.MEMBER_RULE, "applied": False, "count": 0, "members": [], "paths": [],
                "reason": "catalog_declared_inputs", "left_out_count": 1,
                "left_out": [d08._left("S1.raw", "not_named_by_the_catalog_declaration", twin_of="S1.mzML",
                                       twin_of_reason="undecodable_mzml")]}
            report = _gate(unit)
            check = _check(report, "INP-1")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("S1.raw (not_named_by_the_catalog_declaration)", check.detail)
        self.assertIn("S1.raw runs for S1.mzML, and unattributed_members.left_out does not list it", check.detail)
        self.assertIn("INP-1", report.run_blocked_by)

    def test_the_declared_units_record_alone_is_accepted(self) -> None:
        """The same declared unit as Interactive leaves it: the twin is left out, on record, and does not run."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = _TwinUnit(temporary)
            unit.file("S2.mzML", "S2", "B")
            unit.lease_excluded("S1.mzML", "S1", "A")
            earlier._archive(unit, ["S2.mzML", "S1.mzML", "S1.raw"])
            (unit.data / "S1.raw").write_bytes(b"raw")
            unit.manifest["unattributed_members"] = {
                "rule": d07.MEMBER_RULE, "applied": False, "count": 0, "members": [], "paths": [],
                "reason": "catalog_declared_inputs", "left_out_count": 1,
                "left_out": [d08._left("S1.raw", "not_named_by_the_catalog_declaration", twin_of="S1.mzML",
                                       twin_of_reason="undecodable_mzml")]}
            report = _gate(unit)

        statuses = {check.check_id: check.status for check in report.checks}
        self.assertEqual([], report.run_blocked_by, {key: statuses.get(key) for key in report.run_blocked_by})
        self.assertEqual(verifier.PASS, statuses["INP-1"], _check(report, "INP-1").detail)
        self.assertEqual(verifier.PASS, statuses["PAIR-1"], _check(report, "PAIR-1").detail)


class TheRecordSaysWhatRanTests(unittest.TestCase):
    """PAIR-1 (record_only) holds the record of the replaced mzML and of every left-out member."""

    def _record_fails(self, change, *said: str, **options) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, path, mzml = _twin_unit(temporary, **options)
            change(unit, path, mzml)
            report = _gate(unit)
            check = _check(report, "PAIR-1")
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        for text in said:
            self.assertIn(text, check.detail)
        self.assertEqual(verifier.RECORD_ONLY, check.run_policy)
        self.assertNotIn("PAIR-1", report.run_blocked_by)
        self.assertEqual([], report.run_blocked_by)

    def test_an_entry_that_says_neither_what_ran_nor_why_nothing_did(self) -> None:
        def change(unit, _path, _mzml):
            _record(unit)["replaced_undecodable"].append(
                {"member_name": "S3.mzML", "path": "S3.mzML", "reason": "undecodable_mzml", "replaced_by": "S3.raw"})
        self._record_fails(change, "unattributed_members.replaced_undecodable: S3.mzML gives neither "
                                   "replaced_by_input nor replacement_excluded")

    def test_an_entry_naming_an_input_nothing_replaces_it_by(self) -> None:
        def change(unit, _path, _mzml):
            _record(unit)["replaced_undecodable"].append(
                {"member_name": "S3.mzML", "path": "S3.mzML", "reason": "undecodable_mzml", "replaced_by": "S3.raw",
                 "replaced_by_input": "S3.raw"})
        self._record_fails(change, "S3.mzML names S3.raw as the input that runs in its place, and no input row of "
                                   "the lineage replaces it (replaces_undecodable)")

    def test_a_left_out_twin_whose_mzml_is_not_listed(self) -> None:
        def change(unit, _path, _mzml):
            _record(unit)["left_out"].append(d08._left("S3.raw", "analysed_for_an_admitted_sample",
                                                        stands_for="S3.mzML", stands_for_reason="undecodable_mzml"))
        self._record_fails(change, "S3.raw stands for 'S3.mzML', which unattributed_members.replaced_undecodable "
                                   "does not list")

    def test_an_unknown_stands_for_reason(self) -> None:
        def change(unit, _path, _mzml):
            _record(unit)["left_out"].append(d08._left("S3.raw", "analysed_for_an_admitted_sample",
                                                        stands_for="S3.mzML", stands_for_reason="smaller"))
        self._record_fails(change, "S3.raw is left out with stands_for_reason 'smaller', not undecodable_mzml")

    def test_a_left_out_member_without_its_reasons_fields(self) -> None:
        cases = {
            "copy without chosen_by": (d08._left("RAW/S1.raw", "copy_of_the_chosen_member", chosen="S1.raw"),
                                       "RAW/S1.raw (copy_of_the_chosen_member without the copy chosen"),
            "copy chosen by size": (d08._left("RAW/S1.raw", "copy_of_the_chosen_member", chosen="S1.raw",
                                              chosen_by="size"), "chosen_by 'size'"),
            "mzXML twin without twin_of": (d08._left("S4.raw", "admitted_mzxml_not_converted"),
                                           "S4.raw (admitted_mzxml_not_converted without twin_of"),
        }
        for name, (entry, said) in cases.items():
            with self.subTest(name):
                self._record_fails(lambda unit, _path, _mzml, entry=entry: _record(unit)["left_out"].append(entry),
                                   "the record of the archive members the lease left out falls short", said)

    def test_the_new_reasons_said_in_full_are_accepted(self) -> None:
        entries = [
            d08._left("RAW/S1.raw", "copy_of_the_chosen_member", chosen="S1.raw", chosen_by="nearest_the_data_root"),
            d08._left("S4.raw", "admitted_mzxml_not_converted", twin_of="S4.mzXML", twin_of_reason="requires_conversion"),
            d08._left("S5.mzML", "undecodable_mzml"),
            d08._left("S6.mzML", "chosen_other_encoding", chosen="S6.raw", chosen_by="undecodable_mzml_set_aside"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            unit, _path, _mzml = _twin_unit(temporary)
            _record(unit)["left_out"].extend(entries)
            report = _gate(unit)
        self.assertEqual([], report.run_blocked_by)
        self.assertEqual(verifier.WARN, _check(report, "PAIR-1").status, _check(report, "PAIR-1").detail)

    def test_a_twin_whose_conversion_failed_is_said_in_conv1(self) -> None:
        """The record says the twin did not run (replacement_excluded): the sample has no input, and CONV-1 says so
        beside the conversion that did not complete."""
        with tempfile.TemporaryDirectory() as temporary:
            unit, path, mzml = _twin_unit(temporary, converted=True)
            record = unit.manifest["input_conversions"]["records"][0]
            record.update(status="failed", error="validation failed")
            unit.manifest["input_candidates"].remove(path)
            unit.manifest["input_lineage"]["rows"].remove(_row(unit, path))
            unit.manifest["excluded_input_candidates"][0].pop("replaced_by")
            _row(unit, mzml, "excluded")["exclusion"].pop("replaced_by")
            entry = _record(unit)["replaced_undecodable"][0]
            entry.pop("replaced_by_input")
            entry["replacement_excluded"] = "mzxml_conversion_failed"
            report = _gate(unit)

        conv1 = _check(report, "CONV-1")
        self.assertEqual(verifier.WARN, conv1.status, conv1.detail)
        self.assertIn("The record says the readable twin of S1.mzML (mzxml_conversion_failed) did not run, so that "
                      "sample, whose own mzML cannot be decoded, has no input.", conv1.detail)
        self.assertEqual(verifier.NOT_EVALUABLE, _check(report, "INP-1").status, _check(report, "INP-1").detail)
        self.assertNotEqual(verifier.FAIL, _check(report, "PAIR-1").status, _check(report, "PAIR-1").detail)


def _silent(unit: d07._UnattributedUnit, path: str, mzml: str, *, keep_replaced_by: bool = False) -> None:
    """Every record of the twin gone, as no 0.5.36 lease writes it, and the twin still runs as the mzML's sample."""
    _row(unit, path).pop("replaces_undecodable")
    if not keep_replaced_by:
        _row(unit, mzml, "excluded")["exclusion"].pop("replaced_by")
        for item in unit.manifest["excluded_input_candidates"]:
            item.pop("replaced_by", None)
    attribute = unit._attribute_lineage

    def attributed() -> None:
        attribute()
        _row(unit, path)["sample_id"] = "S1"
    unit._attribute_lineage = attributed


class TheTwinIsAnEncodingOfTheSampleTests(unittest.TestCase):
    """The twin is a same-name encoding of the mzML it replaces (review of gate #37 at 681790d). Every record of it
    comes from one grouping in Interactive, so records that agree with each other do not show it."""

    def test_a_twin_of_another_name_is_refused_whatever_its_records_say(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _path, _mzml = _twin_unit(temporary, twin="S9.raw")
            report = _gate(unit)
            check = _check(report, "INP-1")
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("S9.raw runs for S1.mzML as its sample, and is no encoding of that mzML's sample: the rule takes "
                      "a twin of the same name, in folders that agree but for the words naming an encoding",
                      check.detail)
        self.assertIn("INP-1", report.run_blocked_by)

    def test_a_converted_twin_of_another_name_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, _path, _mzml = _twin_unit(temporary, twin="S9.raw", converted=True)
            report = _gate(unit)
            check = _check(report, "INP-1")
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("S9.mzXML runs for S1.mzML as its sample, and is no encoding of that mzML's sample",
                      check.detail)
        self.assertIn("INP-1", report.run_blocked_by)

    def test_a_twin_in_a_folder_named_for_its_encoding_is_accepted(self) -> None:
        """RAW/S1.raw and S1.mzML are one sample's: a folder of encoding words alone is no place of its own."""
        with tempfile.TemporaryDirectory() as temporary:
            unit, _path, _mzml = _twin_unit(temporary, twin="RAW/S1.raw")
            report = _gate(unit)
        statuses = {check.check_id: check.status for check in report.checks}
        self.assertEqual([], report.run_blocked_by, {key: statuses.get(key) for key in report.run_blocked_by})
        self.assertNotIn("is no encoding of", _check(report, "INP-1").detail)

    def test_a_twin_of_the_name_in_another_place_is_refused(self) -> None:
        """POS/S1.raw is another place's S1, not the root S1.mzML's sample."""
        with tempfile.TemporaryDirectory() as temporary:
            unit, _path, _mzml = _twin_unit(temporary, twin="POS/S1.raw")
            report = _gate(unit)
            check = _check(report, "INP-1")
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("POS/S1.raw runs for S1.mzML as its sample, and is no encoding of that mzML's sample",
                      check.detail)

    def test_the_grouping_mirrors_interactives(self) -> None:
        sample = verifier._encoding_sample
        self.assertEqual(sample("S1.mzML"), sample("S1.raw"))
        self.assertEqual(sample("S1.mzML"), sample("mzXML/S1.mzXML"))
        self.assertEqual(sample("NEG_mzML/S1.mzML"), sample("NEG_RAW/S1.d"))
        self.assertNotEqual(sample("S1.mzML"), sample("S9.raw"))
        self.assertNotEqual(sample("POS/S1.mzML"), sample("NEG/S1.raw"))


class AnInputRunningSilentlyForAnUndecodableMzmlTests(unittest.TestCase):
    """An input that runs as an undecodable mzML's sample, as an encoding of it, with every record of the
    replacement missing (review of gate #37 at 681790d): nothing of the twin's records is there to find it by."""

    def test_no_record_at_all_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, path, mzml = _twin_unit(temporary, record=False)
            _silent(unit, path, mzml)
            report = _gate(unit)
            check = _check(report, "INP-1")
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("S1.raw runs as sample S1, an encoding of S1.mzML, that sample's mzML the lease excluded as "
                      "undecodable (unsupported_mzml_encoding), and nothing records that it replaces it: its lineage "
                      "row carries no replaces_undecodable", check.detail)
        self.assertIn("INP-1", report.run_blocked_by)

    def test_a_converted_input_with_no_record_is_refused(self) -> None:
        """The mzML the conversion wrote carries the sample's mzML's name; the mzXML it was read from does not."""
        with tempfile.TemporaryDirectory() as temporary:
            unit, path, mzml = _twin_unit(temporary, record=False, converted=True)
            _silent(unit, path, mzml)
            report = _gate(unit)
            check = _check(report, "INP-1")
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("S1.mzXML runs as sample S1, an encoding of S1.mzML", check.detail)

    def test_the_lease_naming_an_input_its_row_does_not_say_it_replaces_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, path, mzml = _twin_unit(temporary, record=False)
            _silent(unit, path, mzml, keep_replaced_by=True)
            _row(unit, path)["name_pairing"] = {"paired_by": "prefixed_member_name", "declared_raw_file": "S1.raw",
                                                "member_name": "S1.raw"}
            report = _gate(unit)
            check = _check(report, "INP-1")
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("the lease's record of S1.mzML, an mzML it excluded as undecodable, names S1.raw as the input "
                      "that runs in its place, and S1.raw's lineage row does not say it replaces it", check.detail)

    def _not_silent(self, change) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit, path, mzml = _twin_unit(temporary, record=False)
            _silent(unit, path, mzml)
            change(unit, path)
            report = _gate(unit)
            check = _check(report, "INP-1")
        self.assertNotIn("nothing records that it replaces it", check.detail)
        self.assertNotIn("replaces_undecodable", check.detail)

    def test_a_sample_row_naming_the_input_itself_is_its_own_basis(self) -> None:
        """A row that names S1 without an extension pairs S1.raw by its own name: the unit admitted it itself, the
        undecodable mzML is excluded and the rest of the unit runs."""
        def change(unit, _path):
            for item in unit.manifest["project"]["sample_metadata"]:
                if item["sample_id"] == "S1":
                    item["raw_file"] = "S1"
        self._not_silent(change)

    def test_its_own_inferred_pairing_is_its_own_basis(self) -> None:
        def change(unit, path):
            _row(unit, path)["name_pairing"] = {"paired_by": "prefixed_member_name", "declared_raw_file": "S1",
                                                "member_name": "S1.raw"}
        self._not_silent(change)

    def test_another_samples_input_is_not_held_here(self) -> None:
        """S2.mzML runs as S2, which no undecodable mzML is: nothing to say of it."""
        with tempfile.TemporaryDirectory() as temporary:
            unit, path, mzml = _twin_unit(temporary, record=False)
            _silent(unit, path, mzml)
            problems = verifier._silent_twin_problems(
                unit.manifest, [row for row in unit.manifest["input_lineage"]["rows"] if row["path"] != path],
                {verifier._path_key(row["path"]) for row in unit.manifest["input_lineage"]["rows"]})
        self.assertEqual([], problems)


class SplitPartTests(unittest.TestCase):
    """A split part reads its raw owner's record of the mzML its twin replaces."""

    def test_the_record_is_read_through_the_raw_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = {"input_directory": str(Path(temporary) / "raw" / "data"), "unattributed_members": {
                "replaced_undecodable": [{"member_name": "S1.mzML", "path": "S1.mzML", "reason": "undecodable_mzml",
                                          "replaced_by": "S1.raw", "replaced_by_input": "S1.raw"}]}}
            path = Path(temporary) / "parent-run-manifest.json"
            path.write_text(json.dumps(parent), encoding="utf-8")
            part = {"split_from": {"manifest_path": str(path)}}
            self.assertEqual(["S1.mzML"], [item["path"] for item in verifier._record_replaced(part)])


if __name__ == "__main__":
    unittest.main()
