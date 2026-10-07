"""The gate on the user's decisions of 2026-10-07: AIF run as SWATH (ACQ-1), and archive members included
unattributed (INP-1, CLS-1, CLS-2, CLS-3, PAIR-1). Interactive 0.5.31 writes the records read here.

ACQ-1. A single-collision-energy AIF unit runs as SWATH-type in the Console (ST004304 gives identical results;
MTBKS281 matches its 30 eV collection). The campaign disposition records it as aif_run_as_swath
{"collision_energies": [one energy], "rule": "single_ce_aif_as_swath_2026_10_07"}, and each such file's per-file
record keeps header_console_acquisition_type "AIF" beside console_acquisition_type "SWATH" and
console_acquisition_basis "aif_single_ce_as_swath". The sanctioned mapping is a WARN naming the rule; anything
short of that record is still a FAIL. A multi-energy AIF unit is held, never run as SWATH.

UNATTRIBUTED MEMBERS. A unit whose archive is unit-scoped is analysed "even by force": ST001264 has 31 members
and 31 sample rows, and only the three BioRec rows pair; the 28 "..._Youn_saN.raw" members are included, each a
lineage row with name_pairing {"paired_by": "unattributed_member", "member_name"}, its stem as sample_id and no
sample row, a CSV row in the abstention Class (or "Unattributed"), and manifest.unattributed_members
{"count", "members", "rule": "unit_scoped_archive_2026_10_07"}, with the warning code
unattributed_members_included. Each check reads them as explained inputs: a WARN listing them, never a FAIL for
being there, never an approved sample.

The fixtures are test_verify_lineage_built_csv's FolderBranchUnit and test_verify_inputs_and_acquisition's Unit.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import test_verify_gate_decisions_2026_10_06 as earlier  # noqa: E402
import test_verify_inputs_and_acquisition as acquisition  # noqa: E402

verifier = earlier.verifier
FolderBranchUnit = earlier.FolderBranchUnit
_check = earlier._check

RULE = "single_ce_aif_as_swath_2026_10_07"
MEMBER_RULE = "unit_scoped_archive_2026_10_07"
ST001264 = earlier.UndeliveredSampleTests.ST001264
YOUN = [f"021518_387057_CSHp_Youn_sa{number}.raw" for number in range(1, 29)]


# ---- ACQ-1: AIF run as SWATH ------------------------------------------------------------------------------


class AifRunAsSwathTests(unittest.TestCase):
    """ACQ-1 accepts an AIF header run as SWATH only where the binding disposition records the single-energy rule
    and the file's record says that rule decided it; the row is then a WARN naming the rule."""

    acq = acquisition.verifier

    def _acq1(self, *, energies=(30.0,), rule: str = RULE, basis: str = "aif_single_ce_as_swath",
              applied: bool = True, record_field: bool = True, files: int = 1, extra_warning: bool = False):
        with tempfile.TemporaryDirectory() as temporary:
            unit = acquisition.Unit(temporary)
            names = [f"S{index}.mzML" for index in range(files)]
            paths = unit.inputs(names + (["W.mzML"] if extra_warning else []))
            records = [acquisition._record(path, "AIF", "SWATH", confidence=0.9, console_acquisition_basis=basis,
                                           header_console_acquisition_type="AIF",
                                           header_console_acquisition_basis="header",
                                           collision_energies=list(energies))
                       for path in paths[:files]]
            if extra_warning:
                records.append(acquisition._record(paths[-1], "DIA", "SWATH", console_acquisition_basis="declaration",
                                                   header_console_acquisition_type=None))
            unit.preflight(records)
            disposition = acquisition._disposition([], applied=applied)
            if record_field:
                disposition["aif_run_as_swath"] = {"collision_energies": list(energies), "rule": rule}
            unit.manifest["campaign_disposition"] = disposition
            unit.csv(unit.rows(["SWATH"] * len(paths)))
            return _check(self.acq.verify(unit.write(), "before-production"), "ACQ-1")

    def test_a_single_energy_aif_unit_run_as_swath_is_a_warning_naming_the_rule(self) -> None:
        """ST004304 and MTBKS281 (30 eV) as Interactive 0.5.31 records them."""
        check = self._acq1(files=2)

        self.assertEqual(self.acq.WARN, check.status, check.detail)
        self.assertIn(RULE, check.detail)
        self.assertIn("2 of the 2 row(s) run as SWATH where their header gives AIF", check.detail)
        self.assertIn("here 30 eV", check.detail)
        self.assertEqual(2, check.evidence["sanctioned_mappings"])
        self.assertEqual(RULE, check.evidence["sanctioned_rule"])
        self.assertEqual({"collision_energies": [30.0], "rule": RULE}, check.evidence["aif_run_as_swath"])
        self.assertEqual({"sanctioned": 2}, check.evidence["sources"])
        self.assertEqual(self.acq.BLOCKS_RUN, check.run_policy)

    def test_one_energy_recorded_twice_is_still_one(self) -> None:
        check = self._acq1(energies=(30.0, 30.004))

        self.assertEqual(self.acq.WARN, check.status, check.detail)

    def test_a_multi_energy_aif_unit_run_as_swath_is_refused(self) -> None:
        """A multi-energy AIF unit is held for a patched Console; its record sanctions nothing."""
        check = self._acq1(energies=(10.0, 30.0))

        self.assertEqual(self.acq.FAIL, check.status, check.detail)
        self.assertIn("records 2 collision energies", check.detail)
        self.assertNotIn("sanctioned_mappings", check.evidence)

    def test_no_energy_recorded_sanctions_nothing(self) -> None:
        check = self._acq1(energies=())

        self.assertEqual(self.acq.FAIL, check.status, check.detail)
        self.assertIn("records 0 collision energies", check.detail)

    def test_another_rule_name_sanctions_nothing(self) -> None:
        check = self._acq1(rule="some_other_rule")

        self.assertEqual(self.acq.FAIL, check.status, check.detail)
        self.assertIn("names the rule 'some_other_rule'", check.detail)

    def test_a_file_whose_record_another_basis_decided_is_refused(self) -> None:
        check = self._acq1(basis="declaration")

        self.assertEqual(self.acq.FAIL, check.status, check.detail)
        self.assertIn("console_acquisition_basis is 'declaration', not aif_single_ce_as_swath", check.detail)

    def test_a_disposition_not_applied_sanctions_nothing(self) -> None:
        check = self._acq1(applied=False)

        self.assertEqual(self.acq.FAIL, check.status, check.detail)
        self.assertIn("no binding campaign disposition records aif_run_as_swath", check.detail)

    def test_without_the_record_the_row_is_refused(self) -> None:
        check = self._acq1(record_field=False)

        self.assertEqual(self.acq.FAIL, check.status, check.detail)
        self.assertIn("the campaign disposition records no aif_run_as_swath", check.detail)

    def test_the_sanctioned_rows_and_other_warnings_are_both_said(self) -> None:
        check = self._acq1(extra_warning=True)

        self.assertEqual(self.acq.WARN, check.status, check.detail)
        self.assertIn(RULE, check.detail)
        self.assertIn("No row runs against its header verdict, but 1 of the 2 rest on something else", check.detail)
        self.assertEqual({"sanctioned": 1, "declaration": 1}, check.evidence["sources"])


# ---- the unattributed members of a unit-scoped archive ----------------------------------------------------


class _UnattributedUnit(earlier._PairingUnit):
    """A unit as Interactive 0.5.31's lease leaves it: the paired members as before, and each member no sample row
    pairs with an input of its own, named by its stem and attributed to no sample row."""

    def _attribute_lineage(self) -> None:
        FolderBranchUnit._attribute_lineage(self)
        names: dict[str, set[str]] = {}
        for sample in self.manifest["project"]["sample_metadata"]:
            names.setdefault(str(sample.get("raw_file") or "").casefold(), set()).add(sample["sample_id"])
        for row in self.manifest["input_lineage"]["rows"]:
            pairing = row.get("name_pairing")
            if verifier._is_unattributed(row):
                row["sample_id"] = Path(row["path"]).stem
                row["sample_row"] = None
            elif pairing and not row["sample_id"]:
                matched = names.get(pairing["declared_raw_file"].casefold()) or set()
                row["sample_id"] = next(iter(matched)) if len(matched) == 1 else ""

    def member(self, name: str, label: str) -> str:
        download = self._download(self.data / name, name.encode("utf-8"), verified=True)
        path = download["path"]
        self.manifest["input_candidates"].append(path)
        self.manifest["input_lineage"]["rows"].append(
            {"path": path, "kind": "file", "declared_names": [], "sample_id": "", "file_name": "",
             "source": {"url": download["source_url"], "download_path": path},
             "checksums": {"sha256": download["sha256"], "md5": download["md5"], "declared": download["md5"],
                           "declared_algorithm": "md5", "declared_verified": True},
             "name_pairing": {"paired_by": "unattributed_member", "member_name": name}})
        self.labels[Path(name).stem] = label
        self._attribute_lineage()
        return path

    def record(self, members: "list[str]", *, count: "int | None" = None) -> None:
        self.manifest["unattributed_members"] = {"count": len(members) if count is None else count,
                                                 "members": list(members), "rule": MEMBER_RULE}
        self.manifest["warnings"] = ["unattributed_members_included"]


def _abstention(unit: FolderBranchUnit, label: str = "All") -> None:
    proposal = unit.manifest["project"]["class_proposal"]
    proposal["selected_fields"] = []
    proposal["contrast_definition"] = {"kind": "abstention", "class_label": label, "reason": "no_declared_factor"}


def _st001264(temporary: str, *, abstention: bool = True, label: "str | None" = None, record: bool = True,
              shared: int = 1) -> _UnattributedUnit:
    """ST001264 under 0.5.31: 31 sample rows, BioRec1-3 paired behind a prefix, Youn_sa1..28 unattributed."""
    # earlier._paired_unit's ST001264, built on this unit's class.
    unit = _UnattributedUnit(temporary)
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
    for number in range(1, 29):
        project["sample_metadata"].append({"sample_id": f"Sample{number}", "raw_file": f"Sample{number}"})
        project["class_proposal"]["assignments"].append({"sample_id": f"Sample{number}", "class_label": "All"})
        unit.labels[f"Sample{number}"] = "All"
    if abstention:
        _abstention(unit)
    for name in YOUN:
        unit.member(name, label or ("All" if abstention else "Unattributed"))
    earlier._archive(unit, [*ST001264.values(), *YOUN], shared=shared)
    if record:
        unit.record(YOUN)
    return unit


class UnattributedMembersTests(unittest.TestCase):
    """ST001264 under Interactive 0.5.31: the 28 unattributed members are explained inputs in every check."""

    def _report(self, **options):
        with tempfile.TemporaryDirectory() as temporary:
            unit = _st001264(temporary, **options)
            rows = unit.prepare()
            return unit, rows, unit.gate()

    def test_st001264_runs_with_its_28_members_and_no_check_refuses_them(self) -> None:
        _unit, rows, report = self._report()

        self.assertEqual(31, len(rows))
        self.assertEqual(["All"], sorted({row["class_id"] for row in rows}))
        statuses = {check.check_id: check.status for check in report.checks}
        for check_id in ("CLS-1", "CLS-2", "CLS-3", "PAIR-1"):
            self.assertEqual(verifier.WARN, statuses[check_id], _check(report, check_id).detail)
        self.assertEqual([], report.run_blocked_by)

    def test_cls2_lists_them_and_no_approved_sample_is_missing_or_undelivered(self) -> None:
        _unit, _rows, report = self._report()
        check = _check(report, "CLS-2")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual(28, check.evidence["unattributed_rows"])
        self.assertEqual(28, check.evidence["samples_without_attributed_input_count"])
        self.assertEqual(["Sample1"], check.evidence["samples_without_attributed_input"]["Sample1"])
        self.assertEqual(28, check.evidence["unattributed_members"]["count"])
        self.assertEqual("unattributed_members_included", check.evidence["warning"])
        for key in ("missing", "unapproved", "undelivered_samples", "delivered_unpaired_samples"):
            self.assertNotIn(key, check.evidence)
        self.assertIn(MEMBER_RULE, check.detail)
        self.assertIn("manifest.unattributed_members.count 28", check.detail)
        self.assertIn("021518_387057_CSHp_Youn_sa1.raw", check.detail)
        self.assertIn("the run covers 3 of the 31 approved samples by attribution", check.detail)

    def test_without_a_proposal_abstention_their_class_is_unattributed(self) -> None:
        _unit, rows, report = self._report(abstention=False)
        check = _check(report, "CLS-2")

        self.assertEqual({"All": 3, "Unattributed": 28},
                         {label: sum(1 for row in rows if row["class_id"] == label) for label in ("All", "Unattributed")})
        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("in Class 'Unattributed'", check.detail)
        self.assertEqual(verifier.WARN, _check(report, "CLS-1").status)
        self.assertEqual({"Unattributed": 28}, _check(report, "CLS-1").evidence["unattributed_classes"])

    def test_a_member_in_another_class_is_a_class_mismatch(self) -> None:
        for abstention, label in ((True, "Unattributed"), (False, "All"), (True, "Sample")):
            with self.subTest(abstention=abstention, label=label):
                _unit, _rows, report = self._report(abstention=abstention, label=label)
                check = _check(report, "CLS-2")
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn("28 sample(s) carry a different Class", check.detail)
                self.assertIn(f"executed {label!r}, where an unattributed member runs in", check.evidence["differing"][0])
                self.assertEqual([], check.evidence["unapproved"])
                self.assertEqual([], check.evidence["missing"])

    def test_cls1_lists_them_with_the_recorded_count(self) -> None:
        _unit, _rows, report = self._report()
        check = _check(report, "CLS-1")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual(28, check.evidence["unattributed_rows"])
        self.assertEqual({"All": 28}, check.evidence["unattributed_classes"])
        self.assertIn("28 of the 31 rows are archive members no sample row pairs with", check.detail)
        self.assertIn("(manifest.unattributed_members.count 28)", check.detail)
        self.assertEqual(verifier.RECORD_ONLY, check.run_policy)

    def test_cls3_a_ratified_abstention_beside_them_is_a_warning(self) -> None:
        _unit, _rows, report = self._report()
        check = _check(report, "CLS-3")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("ratified abstention", check.detail)
        self.assertIn("which no assignment of the ratified record covers", check.detail)
        self.assertTrue(check.evidence["abstention"])
        self.assertEqual(28, check.evidence["unattributed_members"]["lineage_rows"])

    def test_cls3_an_unratified_record_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _st001264(temporary)
            unit.manifest["project"]["class_proposal"]["status"] = "proposed"
            unit.prepare()
            check = _check(unit.gate(), "CLS-3")

        self.assertEqual(verifier.FAIL, check.status, check.detail)

    def test_pair1_lists_the_inferred_pairings_and_the_unattributed_members(self) -> None:
        _unit, _rows, report = self._report()
        check = _check(report, "PAIR-1")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual(["input_names_paired_by_inference", "unattributed_members_included"],
                         check.evidence["warnings"])
        self.assertEqual(3, check.evidence["paired_by_inference"])
        self.assertEqual(28, check.evidence["unattributed_members"]["count"])
        self.assertEqual(28, check.evidence["unattributed_members"]["lineage_rows"])
        self.assertEqual(10, len(check.evidence["unattributed_members"]["members"]))
        self.assertEqual(MEMBER_RULE, check.evidence["unattributed_members"]["rule"])
        self.assertIn("28 input(s) are archive members no sample row pairs with", check.detail)

    def test_pair1_unattributed_members_alone_are_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _UnattributedUnit(temporary)
            unit.file("a.mzML", "a")
            unit.member("q.mzML", "Unattributed")
            unit.manifest["project"]["download_scope"] = {"kind": "unit_files"}
            unit.record(["q.mzML"])
            unit.prepare()
            check = _check(unit.gate(), "PAIR-1")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual("unattributed_members_included", check.evidence["warning"])
        self.assertEqual(0, check.evidence["paired_by_inference"])

    def test_pair1_refuses_a_record_short_of_the_rule(self) -> None:
        cases = {
            "no record": (lambda unit: unit.manifest.pop("unattributed_members"), "records no unattributed_members"),
            "count": (lambda unit: unit.manifest["unattributed_members"].update(count=27),
                      "unattributed_members.count is 27, and the lineage holds 28"),
            "rule": (lambda unit: unit.manifest["unattributed_members"].update(rule="other"),
                     "unattributed_members.rule is 'other'"),
            "members": (lambda unit: unit.manifest["unattributed_members"]["members"].pop(),
                        "unattributed_members.members does not list 021518_387057_CSHp_Youn_sa28.raw"),
            "manifest warning": (lambda unit: unit.manifest.update(warnings=[]),
                                 "the manifest's warnings do not carry unattributed_members_included"),
            "disposition warning": (lambda unit: unit.exclude(str(unit.data / "nothing.mzML")),
                                    "the campaign disposition's warnings do not carry unattributed_members_included"),
            "shared": (lambda unit: unit.manifest["project"]["download_scope"].update(bundle_shared_unit_count=2),
                       "the download is shared with other units"),
            "no scope": (lambda unit: unit.manifest["project"].pop("download_scope"),
                         "the manifest records no download_scope"),
        }
        for name, (change, said) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                unit = _st001264(temporary)
                unit.prepare()
                change(unit)
                report = unit.gate()
                check = _check(report, "PAIR-1")
                self.assertEqual(verifier.FAIL, check.status, check.detail)
                self.assertIn(said, check.detail)
                self.assertEqual(verifier.RECORD_ONLY, check.run_policy)
                self.assertNotIn("PAIR-1", report.run_blocked_by)

    def test_a_disposition_that_carries_the_warning_is_on_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = _st001264(temporary)
            unit.prepare()
            unit.exclude(str(unit.data / "nothing.mzML"))
            unit.manifest["campaign_disposition"]["warnings"] = ["unattributed_members_included"]
            check = _check(unit.gate(), "PAIR-1")

        self.assertEqual(verifier.WARN, check.status, check.detail)


class UnattributedInputsTests(unittest.TestCase):
    """INP-1: unattributed members are input candidates beside the declared inputs the lease attributed."""

    def _unit(self, temporary: str, *, members: "tuple[str, ...]" = ("q_member.mzML",), declared_extra: int = 1,
              scope: "dict | None" = None) -> _UnattributedUnit:
        unit = _UnattributedUnit(temporary)
        unit.file("a.mzML", "a")
        for index in range(declared_extra):
            unit._declare(f"z{index}.mzML", "file", f"z{index}", "A")
        for name in members:
            unit.member(name, "Unattributed")
        unit.manifest["project"]["download_scope"] = scope if scope is not None else {"kind": "unit_files"}
        unit.record(list(members))
        return unit

    def test_the_declared_inputs_account_for_the_attributed_candidates_and_the_members_are_listed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = self._unit(temporary)
            unit.prepare()
            report = unit.gate()
            check = _check(report, "INP-1")

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertEqual(1, check.evidence["counts"]["unattributed input_candidates"])
        self.assertEqual(1, check.evidence["unattributed_members"]["count"])
        self.assertIn("account for the 1 input candidate(s) the lease attributed", check.detail)
        self.assertIn("q_member.mzML", check.detail)
        self.assertIn(MEMBER_RULE, check.detail)
        self.assertNotIn("INP-1", report.run_blocked_by)

    def test_more_members_than_declared_inputs_left_unpaired_is_still_explained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = self._unit(temporary, members=("m1.mzML", "m2.mzML", "m3.mzML"))
            unit.prepare()
            check = _check(unit.gate(), "INP-1")

        self.assertEqual(verifier.WARN, check.status, check.detail)

    def test_attributed_candidates_beyond_the_declaration_still_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = self._unit(temporary, declared_extra=0)
            # A second attributed input the Catalog never declared.
            unit._input(str(unit._download(unit.data / "b.mzML", b"b", verified=True)["path"]), "file",
                        {}, {}, declared_names=["b.mzML"])
            unit.prepare()
            report = unit.gate()
            check = _check(report, "INP-1")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("1 of them unattributed members", check.detail)
        self.assertIn("INP-1", report.run_blocked_by)

    def test_members_of_a_download_shared_with_other_units_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = self._unit(temporary, scope={"bundle_shared_unit_count": 3,
                                                "bundle_urls": [{"url": "u", "shared_unit_count": 3}]})
            unit.prepare()
            report = unit.gate()
            check = _check(report, "INP-1")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("shared with other units (shared_unit_count up to 3)", check.detail)
        self.assertIn("INP-1", report.run_blocked_by)

    def test_without_unattributed_members_a_short_lease_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = self._unit(temporary, members=())
            unit.manifest.pop("unattributed_members")
            unit.prepare()
            check = _check(unit.gate(), "INP-1")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertNotIn("unattributed_members", check.evidence)


class UnitScopeTests(unittest.TestCase):
    """_unit_scoped reads the Catalog's download_scope as the rule of 2026-10-07 does."""

    def test_the_scope_readings(self) -> None:
        cases = [
            ({"kind": "unit_files"}, True),
            ({"kind": "accession_bundle_with_file_allowlist", "bundle_shared_unit_count": 1,
              "bundle_urls": [{"url": "a", "shared_unit_count": 1}]}, True),
            ({"kind": "accession_bundle_with_file_allowlist", "bundle_shared_unit_count": 2,
              "bundle_urls": [{"url": "a", "shared_unit_count": 2}]}, False),
            ({"bundle_urls": [{"url": "a", "shared_unit_count": 1}, {"url": "b"}]}, None),
            ({}, None),
        ]
        for scope, expected in cases:
            with self.subTest(scope=scope):
                manifest = {"project": {"download_scope": scope}} if scope else {"project": {}}
                self.assertIs(expected, verifier._unit_scoped(manifest)[0])


if __name__ == "__main__":
    unittest.main()
