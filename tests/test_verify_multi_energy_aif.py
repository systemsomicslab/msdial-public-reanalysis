"""ACQ-1 on a multi-energy AIF unit run as AIF with a Console that has MsdialWorkbench#825 (Interactive 0.5.34).

#825 deconvolutes a multi-energy AIF file once per energy and represents each peak by the energy of its MS/MS
reference-spectrum match, else the energy with the most product ions. Interactive 0.5.34 (msdial-interactive-app
#67) releases the hold aif_multi_ce_awaiting_console only where the Console it decided for has #825: the binding
disposition is "run", AIF, and records aif_multi_ce_run {"collision_energies", "rule":
"multi_ce_aif_with_console_825"} beside multi_energy_aif_console (its probe of that Console: capability,
available, probe, assembly_sha256); each input's record carries console_acquisition_basis
"aif_multi_ce_console_825" and its own ms2_collision_energies. The Console that runs is the one
output/run-manifest.json records (console.assembly_sha256, assembly_path).

ACQ-1 PASSes such a unit only where those records say so and the Console that runs is shown to have #825. A
single-energy AIF unit is still expected as SWATH; a multi-energy one on a Console without #825 is refused.
Inputs whose energy sets differ run as they are, on record (user decision, 2026-10-08; Interactive 0.5.36): each
row is held to the set aif_collision_energies_by_input records for its file, and ACQ-1 WARNs, stopping no run.
The fixtures are test_verify_inputs_and_acquisition's Unit; the Console assemblies are a few bytes written here.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import test_verify_inputs_and_acquisition as acquisition  # noqa: E402

verifier = acquisition.verifier
_check = acquisition._check

RULE = "multi_ce_aif_with_console_825"
BASIS = "aif_multi_ce_console_825"
CAPABILITY = "multi_energy_aif_representative_collision_energy"
MARKERS = ("The collision-energy files of ", " nor a collision-energy file exists.")
DIFFER = "aif_energy_sets_differ_between_inputs"


def _assembly(path: Path, markers: "tuple[str, ...]") -> str:
    """A stand-in Console assembly carrying these #825 messages as .NET literals (UTF-16LE); its sha256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ\x00" + b"\x00".join(marker.encode("utf-16-le") for marker in markers) + b"\x00end")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _probe(sha: str, *, available: bool = True) -> dict:
    """multi_energy_aif_console as Interactive 0.5.34's workflow.multi_energy_aif_console records it."""
    return {"capability": CAPABILITY, "available": available, "console_path": "", "console_source": "argument",
            "console_assembly": "MSDIALCUI.exe", "assembly_sha256": sha,
            "probe": "multi_energy_aif_marker" if available else "marker_absent"}


class MultiEnergyAifTests(unittest.TestCase):
    """A multi-energy AIF unit runs as AIF only under multi_ce_aif_with_console_825 on a Console with #825."""

    def _acq1(self, *, energies=(10.0, 20.0), row_energies=None, rule: str = RULE, basis: str = BASIS,
              record_run: bool = True, probe: "str | bool" = "same", console_markers=MARKERS,
              manifest: bool = True, files: int = 2, hold: bool = False, disposition: bool = True,
              rows: "str | None" = None, console_type: str = "AIF", by_input: "dict | None" = None,
              sets_differ: bool = False, names: "list[str] | None" = None,
              key_scheme: "str | None" = "path_relative_to_input_directory"):
        """probe: "same" (the probe read the Console the run manifest records), "other" (another Console with
        #825), "absent" (the probe found no #825 in the Console the run manifest records), False (no probe).
        sets_differ: aif_multi_ce_run records energy_sets_differ true and collision_energy_sets, and the
        disposition the warning (Interactive 0.5.36, the run-as-is decision of 2026-10-08); by_input: the
        disposition's aif_collision_energies_by_input, keyed as Interactive 0.5.36 keys it, by each input's path
        relative to the data root (by default, each row's own energies), and key_scheme its
        aif_collision_energies_by_input_key (None: not recorded). names: the inputs under the data root."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = acquisition.Unit(temporary)
            names = names or [f"S{index}.mzML" for index in range(files)]
            files = len(names)
            paths = unit.inputs(names)
            own = list(row_energies) if row_energies is not None else [list(energies)] * files
            records = [acquisition._record(path, "AIF", console_type, confidence=0.9,
                                           console_acquisition_basis=basis,
                                           header_console_acquisition_type="AIF",
                                           header_console_acquisition_basis="header",
                                           **({"ms2_collision_energies": values} if values is not None else {}))
                       for path, values in zip(paths, own)]
            unit.preflight(records)
            console = Path(temporary) / "console" / "MSDIALCUI.exe"
            sha = _assembly(console, console_markers)
            if disposition:
                record = acquisition._disposition([], kind="skip" if hold else "run")
                if hold:
                    record.update(hold=True, reasons=["aif_multi_ce_awaiting_console"])
                if record_run:
                    record["aif_multi_ce_run"] = {"collision_energies": list(energies), "rule": rule}
                if sets_differ:
                    recorded = by_input if by_input is not None else {
                        Path(path).relative_to(unit.data).as_posix(): values
                        for path, values in zip(paths, own) if values is not None}
                    counted: dict = {}
                    for values in recorded.values():
                        counted[tuple(values)] = counted.get(tuple(values), 0) + 1
                    record["aif_multi_ce_run"].update(energy_sets_differ=True, collision_energy_sets=[
                        {"collision_energies": list(values), "file_count": count}
                        for values, count in sorted(counted.items())])
                    record["aif_collision_energies_by_input"] = recorded
                    if key_scheme is not None:
                        record["aif_collision_energies_by_input_key"] = key_scheme
                    record["warnings"] = [DIFFER]
                if probe == "same":
                    record["multi_energy_aif_console"] = _probe(sha)
                elif probe == "other":
                    record["multi_energy_aif_console"] = _probe("ab" * 32)
                elif probe == "absent":
                    record["multi_energy_aif_console"] = _probe(sha, available=False)
                unit.manifest["campaign_disposition"] = record
            if manifest:
                acquisition._write(unit.output / "run-manifest.json", {"console": {
                    "path": str(console), "binary_sha256": sha, "assembly_path": str(console), "assembly_sha256": sha}})
            unit.csv(unit.rows([rows or "AIF"] * files))
            return _check(verifier.verify(unit.write(), "before-production"), "ACQ-1")

    def test_a_multi_energy_aif_unit_on_the_825_console_passes(self) -> None:
        check = self._acq1()

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertIn("2 of them run as multi-energy AIF (10, 20 eV) under " + RULE, check.detail)
        self.assertIn("the disposition's probe found #825", check.detail)
        self.assertIn("most product ions", check.detail)
        multi = check.evidence["multi_energy_aif"]
        self.assertEqual([10.0, 20.0], multi["collision_energies"])
        self.assertTrue(multi["console_shown"])
        self.assertEqual("disposition_probe", multi["console"]["shown_by"])
        self.assertEqual({"collision_energies": [10.0, 20.0], "rule": RULE}, multi["aif_multi_ce_run"])
        self.assertEqual(verifier.BLOCKS_RUN, check.run_policy)

    def test_the_gate_reads_the_console_when_the_probe_read_another(self) -> None:
        check = self._acq1(probe="other")

        self.assertEqual(verifier.PASS, check.status, check.detail)
        self.assertEqual("gate_read_the_assembly", check.evidence["multi_energy_aif"]["console"]["shown_by"])

    def test_a_console_without_825_is_refused(self) -> None:
        """The probe found no #825, and neither does the gate in the Console that runs."""
        check = self._acq1(probe="absent", console_markers=())

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("has no MsdialWorkbench#825", check.detail)
        self.assertIn("none of #825's markers", check.detail)
        self.assertFalse(check.evidence["multi_energy_aif"]["console_shown"])

    def test_a_pre_825_patch_build_with_one_marker_is_refused(self) -> None:
        """A local AIF patch build from before #825's representative-energy rule carries the second marker only."""
        check = self._acq1(probe="other", console_markers=MARKERS[1:])

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("only some of #825's markers", check.detail)

    def test_a_probe_of_another_console_is_not_enough_where_the_running_one_lacks_825(self) -> None:
        check = self._acq1(probe="other", console_markers=())

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("the disposition's probe read another Console", check.detail)

    def test_no_run_manifest_shows_no_console(self) -> None:
        check = self._acq1(manifest=False)

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("no run manifest records the Console that runs it", check.detail)

    def test_a_held_unit_records_no_run_and_is_refused(self) -> None:
        """aif_multi_ce_awaiting_console: the disposition holds the unit and records no aif_multi_ce_run."""
        check = self._acq1(hold=True, record_run=False, probe="absent", basis="header")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("the campaign disposition records no aif_multi_ce_run", check.detail)
        self.assertIn("aif_multi_ce_awaiting_console", check.detail)

    def test_another_rule_name_sanctions_nothing(self) -> None:
        check = self._acq1(rule="some_other_rule")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("names the rule 'some_other_rule'", check.detail)

    def test_one_recorded_energy_is_no_multi_energy_run(self) -> None:
        check = self._acq1(energies=(10.0,))

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("records 1 collision energies, not two or more", check.detail)

    def test_a_row_another_basis_decided_is_refused(self) -> None:
        check = self._acq1(basis="header")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn(f"console_acquisition_basis is 'header', not {BASIS}", check.detail)

    def test_inputs_whose_energies_differ_are_refused_without_the_run_as_is_record(self) -> None:
        """#825 chooses among one file's energies; only the recorded decision of 2026-10-08 runs differing sets
        (an Interactive 0.5.34-0.5.35 record, or one that lost energy_sets_differ, does not)."""
        check = self._acq1(energies=(10.0, 20.0, 40.0), row_energies=[[10.0, 20.0], [10.0, 40.0]])

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("never across files", check.detail)
        self.assertIn("does not record that the inputs' sets differ (energy_sets_differ", check.detail)


class DifferingEnergySetsTests(unittest.TestCase):
    """Inputs whose energy sets differ run as AIF as they are, on record (user decision, 2026-10-08, answer 6;
    Interactive 0.5.36): ACQ-1 holds each row to its own recorded set and reports the differing sets as a WARN,
    which stops no run. Every other refusal stays."""

    UNION = (10.0, 20.0, 40.0)
    SETS = [[10.0, 20.0], [20.0, 40.0]]
    _acq1 = MultiEnergyAifTests._acq1

    def test_differing_sets_on_the_825_console_are_recorded_and_run(self) -> None:
        check = self._acq1(energies=self.UNION, row_energies=self.SETS, sets_differ=True)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertFalse(check.blocks_run)
        self.assertEqual(verifier.BLOCKS_RUN, check.run_policy)
        self.assertIn("record 2 different set(s) of MS2 collision energies (10, 20 eV in 1 row(s); 20, 40 eV in 1 "
                      "row(s))", check.detail)
        self.assertIn("its own representative collision energy", check.detail)
        self.assertIn(f"user decision, 2026-10-08; {DIFFER}", check.detail)
        self.assertIn("stops no run", check.detail)
        self.assertIn("the disposition's probe found #825", check.detail)
        multi = check.evidence["multi_energy_aif"]
        self.assertIs(multi["energy_sets_differ"], True)
        self.assertEqual("record_only", multi["energy_sets_differ_run_policy"])
        self.assertEqual("run_as_is_2026_10_08", multi["decision"])
        self.assertEqual([{"collision_energies": [10.0, 20.0], "rows": 1},
                          {"collision_energies": [20.0, 40.0], "rows": 1}], multi["collision_energy_sets"])
        self.assertEqual(2, len(multi["recorded_collision_energy_sets"]))
        self.assertIs(multi["disposition_warning"], True)

    def test_the_whole_gate_still_lets_the_run_go(self) -> None:
        """A WARN of ACQ-1 is no FAIL: the before-production report blocks nothing on it."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = acquisition.Unit(temporary)
            paths = unit.inputs(["S0.mzML", "S1.mzML"])
            unit.preflight([acquisition._record(path, "AIF", "AIF", console_acquisition_basis=BASIS,
                                                header_console_acquisition_type="AIF",
                                                header_console_acquisition_basis="header",
                                                ms2_collision_energies=values)
                            for path, values in zip(paths, self.SETS)])
            console = Path(temporary) / "console" / "MSDIALCUI.exe"
            sha = _assembly(console, MARKERS)
            record = acquisition._disposition([])
            record.update(aif_multi_ce_run={"collision_energies": list(self.UNION), "rule": RULE,
                                            "energy_sets_differ": True, "collision_energy_sets": []},
                          aif_collision_energies_by_input={"S0.mzML": self.SETS[0], "S1.mzML": self.SETS[1]},
                          multi_energy_aif_console=_probe(sha), warnings=[DIFFER])
            unit.manifest["campaign_disposition"] = record
            acquisition._write(unit.output / "run-manifest.json", {"console": {
                "path": str(console), "assembly_path": str(console), "assembly_sha256": sha}})
            unit.csv(unit.rows(["AIF", "AIF"]))
            report = verifier.verify(unit.write(), "before-production")

        check = _check(report, "ACQ-1")
        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertNotIn("ACQ-1", " ".join(report.run_blocked_by))

    def test_one_energy_each_but_not_the_same_one_runs(self) -> None:
        check = self._acq1(energies=(20.0, 40.0), row_energies=[[20.0], [40.0]], sets_differ=True)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIn("20 eV in 1 row(s); 40 eV in 1 row(s)", check.detail)

    def test_a_single_energy_input_beside_a_multi_energy_one_runs(self) -> None:
        check = self._acq1(energies=(10.0, 20.0), row_energies=[[10.0, 20.0], [20.0]], sets_differ=True)

        self.assertEqual(verifier.WARN, check.status, check.detail)

    def test_the_sets_are_keyed_by_the_records_file_name(self) -> None:
        """The CSV's file_name is the stem; Interactive keys aif_collision_energies_by_input by the record's file, under
        the data root (an input in the root itself by its file name)."""
        check = self._acq1(energies=self.UNION, row_energies=self.SETS, sets_differ=True,
                           by_input={"S0": self.SETS[0], "S1": self.SETS[1]})

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("records no set for S0.mzML (its path relative to the data root", check.detail)


class InputsOfOneNameInTwoFoldersTests(unittest.TestCase):
    """Second-round answer 2 of 2026-10-08 (Interactive 0.5.36, raw_metadata_preflight.aif_input_key): the per-input
    sets are keyed by each input's path relative to the data root, so POS/QC_01.mzML and NEG/QC_01.mzML each keep
    their own set, and ACQ-1 holds each row to its own."""

    NAMES = ["POS/QC_01.mzML", "NEG/QC_01.mzML"]
    UNION = (10.0, 20.0, 40.0)
    SETS = [[10.0, 20.0], [20.0, 40.0]]
    _acq1 = MultiEnergyAifTests._acq1

    def test_two_inputs_of_one_name_in_two_folders_each_keep_their_own_set(self) -> None:
        check = self._acq1(energies=self.UNION, row_energies=self.SETS, sets_differ=True, names=self.NAMES)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertFalse(check.blocks_run)
        self.assertIn("10, 20 eV in 1 row(s); 20, 40 eV in 1 row(s)", check.detail)
        self.assertIn("by its path relative to the data root", check.detail)
        multi = check.evidence["multi_energy_aif"]
        self.assertEqual("path_relative_to_input_directory", multi["aif_collision_energies_by_input_key"])

    def test_keys_are_compared_without_case_and_with_either_separator(self) -> None:
        for keys in (["pos/qc_01.mzml", "NEG/QC_01.MZML"], ["POS\\QC_01.mzML", "NEG\\QC_01.mzML"]):
            with self.subTest(keys=keys):
                check = self._acq1(energies=self.UNION, row_energies=self.SETS, sets_differ=True, names=self.NAMES,
                                   by_input=dict(zip(keys, self.SETS)))
                self.assertEqual(verifier.WARN, check.status, check.detail)

    def test_a_record_keyed_by_basename_is_refused(self) -> None:
        """A basename names both inputs: no set can be told to be either one's."""
        check = self._acq1(energies=self.UNION, row_energies=self.SETS, sets_differ=True, names=self.NAMES,
                           by_input={"QC_01.mzML": self.SETS[1]})

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        failures = " ".join(check.evidence["failures"])
        self.assertIn("records no set for POS/QC_01.mzML", failures)
        self.assertIn("records no set for NEG/QC_01.mzML", failures)

    def test_each_input_is_held_to_its_own_folders_set(self) -> None:
        """The sets recorded the other way round: the NEG input records POS's set."""
        check = self._acq1(energies=self.UNION, row_energies=self.SETS, sets_differ=True, names=self.NAMES,
                           by_input={"POS/QC_01.mzML": self.SETS[0], "NEG/QC_01.mzML": self.SETS[0]})

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("QC_01: records 20, 40 eV, not the 10, 20 eV the campaign disposition's "
                      "aif_collision_energies_by_input records for NEG/QC_01.mzML", check.detail)
        self.assertNotIn("POS/QC_01.mzML", check.detail)

    def test_a_record_that_names_another_key_scheme_is_not_read(self) -> None:
        check = self._acq1(energies=self.UNION, row_energies=self.SETS, sets_differ=True, names=self.NAMES,
                           key_scheme="basename")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("is keyed by 'basename' (aif_collision_energies_by_input_key)", check.detail)

    def test_a_record_that_states_no_key_scheme_is_read_by_relative_path(self) -> None:
        check = self._acq1(energies=self.UNION, row_energies=self.SETS, sets_differ=True, names=self.NAMES,
                           key_scheme=None)

        self.assertEqual(verifier.WARN, check.status, check.detail)
        self.assertIsNone(check.evidence["multi_energy_aif"]["aif_collision_energies_by_input_key"])

    def test_the_key_as_interactive_gives_it(self) -> None:
        root = str(Path(tempfile.gettempdir()) / "unit" / "raw" / "data")
        self.assertEqual("POS/QC_01.mzML", verifier._aif_input_key(str(Path(root) / "POS" / "QC_01.mzML"), root))
        self.assertEqual("QC_01.mzML", verifier._aif_input_key(str(Path(root) / "QC_01.mzML"), root))
        self.assertEqual("../converted/QC_01.mzML",
                         verifier._aif_input_key(str(Path(root).parent / "converted" / "QC_01.mzML"), root))
        self.assertEqual("POS/QC_01.mzML", verifier._aif_input_key("POS\\QC_01.mzML", root))
        self.assertEqual("C:/x/POS/QC_01.mzML", verifier._aif_input_key("C:\\x\\POS\\QC_01.mzML", ""))

    def test_a_split_part_without_its_own_data_root_reads_its_parents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = {"input_directory": str(Path(temporary) / "raw" / "data")}
            path = Path(temporary) / "parent-run-manifest.json"
            path.write_text(json.dumps(parent), encoding="utf-8")
            part = {"split_from": {"manifest_path": str(path)}}
            self.assertEqual(parent["input_directory"], verifier._data_root(part))
            self.assertEqual("own", verifier._data_root({"input_directory": "own", "split_from": {}}))

    def test_a_row_whose_energies_are_not_its_recorded_set_is_refused(self) -> None:
        check = self._acq1(energies=self.UNION, row_energies=self.SETS, sets_differ=True,
                           by_input={"S0.mzML": [10.0, 20.0], "S1.mzML": [10.0, 40.0]})

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("S1: records 20, 40 eV, not the 10, 40 eV", check.detail)

    def test_a_set_outside_the_units_energies_is_refused(self) -> None:
        check = self._acq1(energies=(10.0, 20.0), row_energies=self.SETS, sets_differ=True)

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("S1: records 20, 40 eV, which are not all among the unit's 10, 20 eV", check.detail)

    def test_an_input_with_no_recorded_energy_is_still_refused(self) -> None:
        check = self._acq1(energies=self.UNION, row_energies=[[10.0, 20.0], None], sets_differ=True)

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("S1: records no ms2_collision_energies", check.detail)
        self.assertIn("aif_collision_energy_unrecorded", check.detail)

    def test_a_console_without_825_is_still_refused(self) -> None:
        check = self._acq1(energies=self.UNION, row_energies=self.SETS, sets_differ=True, probe="absent",
                           console_markers=())

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("has no MsdialWorkbench#825", check.detail)

    def test_a_held_unit_is_still_refused(self) -> None:
        """Interactive 0.5.34-0.5.35 held it (aif_collision_energies_differ_between_inputs), recording no run."""
        check = self._acq1(energies=self.UNION, row_energies=self.SETS, hold=True, record_run=False)

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("the campaign disposition records no aif_multi_ce_run", check.detail)

    def test_outside_a_campaign_differing_rows_are_still_refused(self) -> None:
        """No disposition records the decision there."""
        check = self._acq1(row_energies=self.SETS, disposition=False)

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("record different MS2 collision energies", check.detail)

    def test_a_single_energy_unit_is_still_no_multi_energy_run(self) -> None:
        check = self._acq1(energies=(20.0,), row_energies=[[20.0], [20.0]], sets_differ=True)

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("records 1 collision energies, not two or more", check.detail)

    def test_an_input_with_no_recorded_energy_is_refused(self) -> None:
        """aif_collision_energy_unrecorded holds such a unit with or without #825."""
        check = self._acq1(row_energies=[[10.0, 20.0], None])

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("no ms2_collision_energies", check.detail)

    def test_energies_equal_to_a_tenth_of_an_ev_are_the_same(self) -> None:
        check = self._acq1(row_energies=[[10.0, 20.0], [10.02, 19.98]])

        self.assertEqual(verifier.PASS, check.status, check.detail)

    def test_multi_energy_rows_recorded_without_a_disposition_still_need_the_console(self) -> None:
        """Outside a campaign no disposition decides; the rows' energies must agree and the Console have #825."""
        passed = self._acq1(disposition=False)
        refused = self._acq1(disposition=False, console_markers=())

        self.assertEqual(verifier.PASS, passed.status, passed.detail)
        self.assertEqual(verifier.FAIL, refused.status, refused.detail)
        self.assertIn("has no MsdialWorkbench#825", refused.detail)

    def test_a_multi_energy_unit_run_as_swath_is_still_refused(self) -> None:
        """The disposition says AIF; the row's SWATH is no type the record decided."""
        check = self._acq1(rows="SWATH")

        self.assertEqual(verifier.FAIL, check.status, check.detail)


class SingleEnergyAifTests(unittest.TestCase):
    """A single-energy AIF unit is still expected as SWATH, whatever the Console."""

    def test_a_single_energy_aif_row_run_as_aif_is_refused_even_on_the_825_console(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unit = acquisition.Unit(temporary)
            paths = unit.inputs(["S0.mzML"])
            unit.preflight([acquisition._record(paths[0], "AIF", "SWATH", console_acquisition_basis="aif_single_ce_as_swath",
                                                header_console_acquisition_type="AIF",
                                                header_console_acquisition_basis="header",
                                                ms2_collision_energies=[30.0])])
            sha = _assembly(Path(temporary) / "console" / "MSDIALCUI.exe", MARKERS)
            record = acquisition._disposition([])
            record["aif_run_as_swath"] = {"collision_energies": [30.0], "rule": "single_ce_aif_as_swath_2026_10_07"}
            record["multi_energy_aif_console"] = _probe(sha)
            unit.manifest["campaign_disposition"] = record
            unit.csv(unit.rows(["AIF"]))
            check = _check(verifier.verify(unit.write(), "before-production"), "ACQ-1")

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("its record decided SWATH, and the Console will deconvolute it as AIF", check.detail)


class MirroredConstantsTests(unittest.TestCase):
    """The gate mirrors Interactive's names; a rename on one side must be made on the other."""

    def test_the_names(self) -> None:
        self.assertEqual(RULE, verifier.AIF_MULTI_CE_RULE)
        self.assertEqual(BASIS, verifier.AIF_MULTI_CE_BASIS)
        self.assertEqual(CAPABILITY, verifier.MULTI_ENERGY_AIF_CAPABILITY)
        self.assertEqual(MARKERS, verifier.MULTI_ENERGY_AIF_CONSOLE_MARKERS)

    def test_against_interactive_where_it_is_importable(self) -> None:
        """The checkout test_campaign_contract reads (MSDIAL_INTERACTIVE_ROOT, else its default)."""
        root = Path(os.environ.get("MSDIAL_INTERACTIVE_ROOT") or r"D:\0_SourceCode\msdial_interactive_app")
        if (root / "msdial_app").is_dir() and str(root) not in sys.path:
            sys.path.insert(0, str(root))
        try:
            from msdial_app import raw_metadata_preflight, workflow
        except ImportError:
            self.skipTest("Interactive is not importable here")
        if not hasattr(raw_metadata_preflight, "AIF_MULTI_CE_RULE"):
            self.skipTest("this Interactive predates 0.5.34")
        self.assertEqual(raw_metadata_preflight.AIF_MULTI_CE_RULE, verifier.AIF_MULTI_CE_RULE)
        self.assertEqual(raw_metadata_preflight.AIF_MULTI_CE_BASIS, verifier.AIF_MULTI_CE_BASIS)
        self.assertEqual(workflow.MULTI_ENERGY_AIF_CAPABILITY, verifier.MULTI_ENERGY_AIF_CAPABILITY)
        self.assertEqual(tuple(workflow.MULTI_ENERGY_AIF_CONSOLE_MARKERS), verifier.MULTI_ENERGY_AIF_CONSOLE_MARKERS)
        if hasattr(raw_metadata_preflight, "AIF_CE_SETS_DIFFER_RECORDED"):  # Interactive 0.5.36 on
            self.assertEqual(raw_metadata_preflight.AIF_CE_SETS_DIFFER_RECORDED, verifier.AIF_CE_SETS_DIFFER_WARNING)
        if hasattr(raw_metadata_preflight, "aif_input_key"):  # Interactive 0.5.36, keyed by relative path
            self.assertEqual(raw_metadata_preflight.AIF_CE_BY_INPUT_KEY, verifier.AIF_CE_BY_INPUT_KEY)
            root = str(Path(tempfile.gettempdir()) / "unit" / "raw" / "data")
            for path in (str(Path(root) / "POS" / "QC_01.mzML"), str(Path(root) / "QC_01.mzML"),
                         str(Path(root).parent / "converted" / "a" / "QC_01.mzML"), "POS\\QC_01.mzML",
                         "QC_01.mzML"):
                for directory in (root, None, ""):
                    self.assertEqual(raw_metadata_preflight.aif_input_key(path, directory),
                                     verifier._aif_input_key(path, directory), (path, directory))

    def test_the_differing_sets_names(self) -> None:
        self.assertEqual(DIFFER, verifier.AIF_CE_SETS_DIFFER_WARNING)
        self.assertEqual("energy_sets_differ", verifier.AIF_CE_SETS_DIFFER_FIELD)
        self.assertEqual("collision_energy_sets", verifier.AIF_CE_SETS_FIELD)
        self.assertEqual("aif_collision_energies_by_input", verifier.AIF_CE_BY_INPUT_FIELD)
        self.assertEqual("aif_collision_energies_by_input_key", verifier.AIF_CE_BY_INPUT_KEY_FIELD)
        self.assertEqual("path_relative_to_input_directory", verifier.AIF_CE_BY_INPUT_KEY)
        self.assertEqual(verifier.RECORD_ONLY, verifier.AIF_CE_SETS_DIFFER_POLICY)


if __name__ == "__main__":
    unittest.main()
