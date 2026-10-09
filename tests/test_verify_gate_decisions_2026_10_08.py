"""The gate on the user's second-round answers of 2026-10-08 about an archive's unpaired members (answer 3).

Interactive 0.5.36 (msdial-interactive-app#69) takes them so: in a campaign an unpaired mzXML is converted like any
other mzXML input (the rule of 2026-09-30), and the mzML written from it is an unattributed input whose lineage row
is the conversion (source.conversion.source_path the mzXML) and whose name_pairing.member_name is the mzXML's
basename, listed in unattributed_members.converted. Opposite-polarity names and a shared archive's members are left
out, on record, in unattributed_members.left_out. Which one of a sample's encodings runs is the user's one rule of
2026-10-09, tested in test_verify_gate_one_encoding_rule_2026_10_09; the case-by-case answers it superseded (the
encoding order among unpaired members, chosen_other_encoding, a twin analysed for an admitted sample) are gone.

INP-1 (blocks_run) accepts what is recorded, and FAILs where the run holds what that record does not let it hold: a
member the record leaves out, or a converted unattributed input that is not the conversion of the member it names.
PAIR-1 (record_only) holds the record itself.

The fixtures are test_verify_gate_decisions_2026_10_07's ST001264-like unit (test_verify_lineage_built_csv's
FolderBranchUnit), with conversion records written as test_verify_input_lineage's LineageUnit writes them.
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
import test_verify_gate_decisions_2026_10_07 as d07  # noqa: E402

earlier = d07.earlier
verifier = d07.verifier
_check = d07._check
ST001264 = d07.ST001264


class _Unit(d07._UnattributedUnit):
    """The 0.5.31 unit, whose converted inputs lie beside the data root (raw/converted), where no declaration is."""

    def _declared_sample(self, path: str) -> str:
        if not Path(path).is_relative_to(self.data):
            return ""
        return super()._declared_sample(path)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _converted_member(unit: d07._UnattributedUnit, name: str, label: str = "All", *,
                      unattributed: bool = True) -> str:
    """An unpaired mzXML member, converted to raw/converted/<stem>.mzML by the campaign's convert stage, and the
    mzML an unattributed input: its lineage row the conversion, named after the mzXML (Interactive 0.5.36)."""
    source = unit.data / name
    read = name.encode("utf-8")
    unit._download(source, read, verified=True)
    output = unit.raw / "converted" / (Path(name).stem + ".mzML")
    output.parent.mkdir(parents=True, exist_ok=True)
    written = b"<mzML/>" + read
    output.write_bytes(written)
    record = {
        "schema": "msdial-mzxml-conversion.v1", "status": "converted", "error": None,
        "converter": {"name": "MS-DIAL Interactive mzXML to mzML converter", "version": "0.5.36",
                      "module_sha256": "5f" * 32},
        "options": {"impute_polarity": None},
        "source": {"path": str(source), "relative_path": name, "name": Path(name).name, "bytes": len(read),
                   "sha256": _sha256(read)},
        "output": {"path": str(output), "bytes": len(written), "sha256": _sha256(written)},
        "inferences": [], "deviations": [], "warnings": [],
        "counts": {"spectra": 2, "polarity": {"positive": 2}, "polarity_recorded": {"positive": 2}},
        "validation": {"schema": "msdial-mzxml-conversion-validation.v1", "status": "passed", "spectra_compared": 2,
                       "problems": [], "problem_count": 0},
    }
    conversions = unit.manifest.setdefault("input_conversions", {"schema": "msdial-input-conversion.v1",
                                                                 "records": []})
    conversions["records"].append(record)
    unit.manifest["input_candidates"].append(str(output))
    row = {"path": str(output), "kind": "converted", "declared_names": [], "sample_id": "", "file_name": "",
           "source": {"conversion": {"source_path": str(source), "source_relative_path": name,
                                     "source_sha256": _sha256(read), "output_sha256": _sha256(written),
                                     "record": len(conversions["records"]) - 1}},
           "checksums": {"sha256": _sha256(written)}}
    if unattributed:
        row["name_pairing"] = {"paired_by": "unattributed_member", "member_name": Path(name).name}
    unit.manifest["input_lineage"]["rows"].append(row)
    unit.labels[Path(name).stem] = label
    unit._attribute_lineage()
    return str(output)


def _unit(temporary: str, *, plain: "tuple[str, ...]" = ("U_8.raw",), converted: "tuple[str, ...]" = ("U_9.mzXML",),
          on_disk: "tuple[str, ...]" = (), left_out: "list[dict] | None" = None,
          converted_record: "list[str] | None" = None) -> d07._UnattributedUnit:
    """ST001264's shape under 0.5.36: BioRec1-3 paired behind a prefix, `plain` members unattributed as they are,
    `converted` mzXML members converted to their unattributed input, `on_disk` members the archive holds and the
    lease left out (left_out records them), all in one unit-scoped archive, and the record of all of it."""
    unit = _Unit(temporary)
    for number, member in ST001264.items():
        unit.file(member, f"Biorec{number}", "All")
    project = unit.manifest["project"]
    project["analysis_inputs"] = []
    project["analysis_inputs_declared"] = False
    project.pop("analysis_input_count", None)
    by_member = {member: number for number, member in ST001264.items()}
    for item in project["sample_metadata"]:
        item["raw_file"] = f"BioRec{by_member[item['raw_file']]}.raw"
    for row in unit.manifest["input_lineage"]["rows"]:
        member = Path(row["path"]).name
        row["declared_names"] = []
        row["name_pairing"] = {"paired_by": "prefixed_member_name",
                               "declared_raw_file": f"BioRec{by_member[member]}.raw", "member_name": member}
    d07._abstention(unit)
    for name in plain:
        unit.member(name, "All")
    for name in converted:
        _converted_member(unit, name)
    for name in on_disk:
        path = unit.data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode("utf-8"))
    earlier._archive(unit, [*ST001264.values(), *plain, *converted, *on_disk], shared=1)
    members = [*plain, *converted]
    unit.manifest["unattributed_members"] = {
        "rule": d07.MEMBER_RULE, "applied": True, "count": len(members),
        "members": sorted(Path(item).name for item in members), "paths": sorted(members),
        **({"converted": sorted(converted if converted_record is None else converted_record)}
           if converted or converted_record else {}),
        **({"left_out": left_out, "left_out_count": len(left_out)} if left_out else {}),
    }
    unit.manifest["warnings"] = ["unattributed_members_included"]
    return unit


def _gate(unit: d07._UnattributedUnit):
    unit.prepare()
    return unit.gate()


def _left(path: str, reason: str, **extra) -> dict:
    return {"member_name": Path(path).name, "path": path, "reason": reason, **extra}


class ConvertedUnpairedMzxmlTests(unittest.TestCase):
    """An unpaired mzXML converted to mzML is an unattributed input like any other, where the records show it."""

    def test_a_converted_unpaired_mzxml_runs_as_an_unattributed_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _unit(temporary)
            report = _gate(unit)

        statuses = {check.check_id: check.status for check in report.checks}
        self.assertEqual([], report.run_blocked_by, {key: statuses.get(key) for key in report.run_blocked_by})
        self.assertEqual(verifier.PASS, statuses["CONV-1"], _check(report, "CONV-1").detail)
        inp1 = _check(report, "INP-1")
        self.assertEqual(verifier.NOT_EVALUABLE, inp1.status, inp1.detail)
        self.assertIn("none is a member it leaves out, and each converted unattributed input is the conversion of "
                      "the mzXML it names", inp1.detail)
        self.assertEqual(1, inp1.evidence["unpaired_members"]["converted_unattributed_inputs"])
        pair1 = _check(report, "PAIR-1")
        self.assertEqual(verifier.WARN, pair1.status, pair1.detail)
        self.assertIn("1 of them converted from mzXML, as any mzXML input of a campaign is (U_9.mzXML)", pair1.detail)
        self.assertEqual(["U_9.mzXML"], pair1.evidence["unattributed_members"]["converted"])
        self.assertIn("U_9.mzXML", pair1.evidence["unattributed_members"]["members"])

    def test_the_converted_record_must_list_it_on_record_only(self) -> None:
        for name, record, said in (
                ("unlisted", [], "unattributed_members.converted does not list U_9.mzXML"),
                ("another", ["U_8.raw", "U_9.mzXML"], "unattributed_members.converted lists U_8.raw, which no "
                                                      "converted unattributed input")):
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                unit = _unit(temporary, converted_record=record)
                if not record:
                    unit.manifest["unattributed_members"].pop("converted", None)
                report = _gate(unit)
                check = _check(report, "PAIR-1")
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn(said, check.detail)
                self.assertEqual(verifier.RECORD_ONLY, check.run_policy)
                self.assertNotIn("PAIR-1", report.run_blocked_by)
                self.assertEqual([], report.run_blocked_by)

    def test_a_converted_input_that_names_another_member_blocks_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _unit(temporary)
            for row in unit.manifest["input_lineage"]["rows"]:
                if row["kind"] == "converted":
                    row["name_pairing"]["member_name"] = "U_7.mzXML"
            unit.manifest["unattributed_members"]["members"] = ["U_7.mzXML", "U_8.raw"]
            report = _gate(unit)
            check = _check(report, "INP-1")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("U_9.mzML is converted from U_9.mzXML, and its name_pairing names U_7.mzXML", check.detail)
        self.assertIn("INP-1", report.run_blocked_by)

    def test_a_converted_input_read_from_no_mzxml_blocks_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _unit(temporary)
            for row in unit.manifest["input_lineage"]["rows"]:
                if row["kind"] == "converted":
                    conversion = row["source"]["conversion"]
                    conversion["source_path"] = conversion["source_path"][: -len(".mzXML")] + ".mzData"
                    row["name_pairing"]["member_name"] = "U_9.mzData"
            report = _gate(unit)
            check = _check(report, "INP-1")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("is converted from U_9.mzData, which is no mzXML", check.detail)

    def test_an_mzxml_the_record_leaves_out_that_was_converted_and_runs_blocks_the_run(self) -> None:
        """Outside a campaign the lease leaves an unpaired mzXML out (requires_conversion); one that runs anyway
        reached the run without the record that would let it."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = _unit(temporary, left_out=[_left("U_9.mzXML", "requires_conversion")])
            report = _gate(unit)
            check = _check(report, "INP-1")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("1 input(s) are archive members that unattributed_members.left_out leaves out, and they reach "
                      "the run all the same: U_9.mzXML (requires_conversion)", check.detail)
        self.assertIn("INP-1", report.run_blocked_by)


class LeftOutMembersTests(unittest.TestCase):
    """What unattributed_members.left_out leaves out of the unit does not run, and each entry says why."""

    def test_an_opposite_polarity_member_that_runs_blocks_the_run(self) -> None:
        left = [_left("U_8_neg.raw", "polarity_token_contradicts_ion_mode")]
        with tempfile.TemporaryDirectory() as temporary:
            unit = _unit(temporary, plain=("U_8.raw", "U_8_neg.raw"), left_out=left)
            report = _gate(unit)
            check = _check(report, "INP-1")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("U_8_neg.raw (polarity_token_contradicts_ion_mode)", check.detail)

    def test_a_left_out_member_that_is_not_there_is_on_record(self) -> None:
        left = [_left("U_7_neg.raw", "polarity_token_contradicts_ion_mode")]
        with tempfile.TemporaryDirectory() as temporary:
            unit = _unit(temporary, on_disk=("U_7_neg.raw",), left_out=left)
            report = _gate(unit)

        self.assertEqual([], report.run_blocked_by)
        inp1 = _check(report, "INP-1")
        self.assertEqual({"polarity_token_contradicts_ion_mode": 1},
                         inp1.evidence["unpaired_members"]["left_out"]["reasons"])
        pair1 = _check(report, "PAIR-1")
        self.assertEqual(verifier.WARN, pair1.status, pair1.detail)
        self.assertIn("1 other member(s) left out, on record (polarity_token_contradicts_ion_mode 1)", pair1.detail)

    def test_a_left_out_record_that_does_not_say_why_fails_pair1_on_record_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _unit(temporary, on_disk=("U_7.mzML",), left_out=[{"member_name": "U_7.mzML", "path": "U_7.mzML"}])
            report = _gate(unit)
            check = _check(report, "PAIR-1")
        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("unattributed_members.left_out does not say why it leaves out U_7.mzML (no reason)", check.detail)
        self.assertEqual([], report.run_blocked_by)

    def test_a_superseded_left_out_reason_is_no_basis_to_run(self) -> None:
        """chosen_other_encoding, from a lease before the one rule of 2026-10-09, still leaves the member out: it
        reaching the run blocks it, and the rule's own record is what would let one of two encodings run."""
        left = [_left("U_8.mzML", "chosen_other_encoding", chosen="U_8.raw", chosen_by="encoding_order")]
        with tempfile.TemporaryDirectory() as temporary:
            unit = _unit(temporary, plain=("U_8.raw", "U_8.mzML"), left_out=left)
            report = _gate(unit)
            check = _check(report, "INP-1")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("U_8.mzML (chosen_other_encoding)", check.detail)
        self.assertIn("1 sample(s) reach the run in more than one file", check.detail)
        self.assertIn("INP-1", report.run_blocked_by)


class DeclaredUnitTests(unittest.TestCase):
    """Where a declaration stands beside unattributed members, INP-1 holds the run to the same record."""

    def test_a_left_out_member_that_runs_fails_beside_a_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = d07._UnattributedUnit(temporary)
            unit.file("a.mzML", "a")
            unit._declare("z0.mzML", "file", "z0", "A")
            unit.member("q.mzML", "Unattributed")
            unit.member("q.mzXML.mzML", "Unattributed")
            unit.manifest["project"]["download_scope"] = {"kind": "unit_files"}
            unit.record(["q.mzML", "q.mzXML.mzML"])
            unit.manifest["unattributed_members"]["left_out"] = [
                _left("q.mzXML.mzML", "polarity_token_contradicts_ion_mode")]
            report = _gate(unit)
            check = _check(report, "INP-1")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("q.mzXML.mzML (polarity_token_contradicts_ion_mode)", check.detail)
        self.assertIn("INP-1", report.run_blocked_by)


class SplitPartTests(unittest.TestCase):
    """A split part reads its raw owner's left_out beside its own."""

    def test_a_split_part_reads_its_parents_left_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = {"unattributed_members": {"left_out": [_left("U_8.mzML", "polarity_token_contradicts_ion_mode")]}}
            path = Path(temporary) / "parent-run-manifest.json"
            path.write_text(json.dumps(parent), encoding="utf-8")
            part = {"split_from": {"manifest_path": str(path)},
                    "unattributed_members": {"left_out": [_left("U_9.mzML", "stem_of_several_sample_rows")]}}
            left = verifier._record_left_out(part)

        self.assertEqual(["U_9.mzML", "U_8.mzML"], [item["path"] for item in left])


if __name__ == "__main__":
    unittest.main()
