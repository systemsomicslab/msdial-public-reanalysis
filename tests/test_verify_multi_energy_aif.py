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
The fixtures are test_verify_inputs_and_acquisition's Unit; the Console assemblies are a few bytes written here.
"""

from __future__ import annotations

import hashlib
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
              rows: "str | None" = None, console_type: str = "AIF"):
        """probe: "same" (the probe read the Console the run manifest records), "other" (another Console with
        #825), "absent" (the probe found no #825 in the Console the run manifest records), False (no probe)."""
        with tempfile.TemporaryDirectory() as temporary:
            unit = acquisition.Unit(temporary)
            paths = unit.inputs([f"S{index}.mzML" for index in range(files)])
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

    def test_inputs_whose_energies_differ_are_refused(self) -> None:
        """#825 chooses among one file's energies; Interactive holds a unit whose inputs differ."""
        check = self._acq1(energies=(10.0, 20.0, 40.0), row_energies=[[10.0, 20.0], [10.0, 40.0]])

        self.assertEqual(verifier.FAIL, check.status, check.detail)
        self.assertIn("never across files", check.detail)

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


if __name__ == "__main__":
    unittest.main()
