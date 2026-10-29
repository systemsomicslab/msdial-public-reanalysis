"""The user's decisions of 2026-10-29: Both units are in the pools, and only CID/HCD data run.

A unit whose Catalog record says Both polarities is admitted: Interactive reads each file's polarity from its raw
header and splits the unit into single-polarity parts (each part's ledger row carries its own polarity), excludes a
file whose scans switch polarity, and runs the rest. Every reference spectrum is CID/HCD-derived, so the plan excludes
a unit whose own evidence names another fragmentation method, read as the ion-mobility option A reads evidence: the
study's text never excludes a unit, since MB-POST MPST000010 holds an EAD unit ("EAD (14 eV)", 86f50459) beside OAD
and CID ones. A unit only some of whose files are named so is planned, and Interactive 0.5.52's lease keeps those
files out; the gate's INP-1 holds both sides of that.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import campaign_fakes as fakes  # noqa: E402  (puts scripts/ on the path)
import test_campaign_contract as contract  # noqa: E402  (puts Interactive and the Catalog on the path)
import test_campaign_machine as machine_tests  # noqa: E402
import test_campaign_plan as plan_tests  # noqa: E402
import test_verify_conversion as conversion_tests  # noqa: E402
from campaign import plan, policy, ports  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("verify_run_invariants_cid", TESTS.parent / "scripts" / "verify-run-invariants.py")
assert _SPEC and _SPEC.loader
gate = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_run_invariants_cid"] = gate
_SPEC.loader.exec_module(gate)

GB = plan_tests.GB
KEYWORDS = "CID, OAD, lipidomics, lipid isomer, LC-MS, marmoset"


def add_files_unit(path: Path, unit: str, accession: str, files: list[str], *, ion_mode: str = "Positive",
                   acquisition: str = "DDA", label: str = "", instrument: str = "", rows: dict | None = None,
                   evidence: tuple = ()) -> None:
    """A unit of plain files, one sample row each, every row carrying `rows`; `evidence` is (field, value) rows of
    analysis_unit_evidence. The study is MB-POST's, shared by every unit of one accession."""
    connection = sqlite3.connect(path)
    with connection:
        study = f"study-{accession}"
        connection.execute(
            "INSERT OR IGNORE INTO study(study_id, repository, accession, source_hash, title, description) "
            "VALUES (?, 'mb_post', ?, 'h', 'A lipidome study', 'Lipid isomers by CID and OAD')", (study, accession))
        connection.execute(
            "INSERT INTO analysis_unit(unit_id, study_id, source_subrecord_id, label, separation, acquisition_mode, "
            "ion_mode, ion_mobility, instrument, signature, target_omics) "
            "VALUES (?, ?, ?, ?, 'LC-MS', ?, ?, 'Unknown', ?, ?, 'Lipidomics')",
            (unit, study, unit, label, acquisition, ion_mode, instrument, unit))
        for index, name in enumerate(files):
            sample = f"pk-{unit}-{index}"
            sample_id = Path(name).stem
            connection.execute("INSERT INTO sample(sample_pk, unit_id, sample_id, raw_file) VALUES (?, ?, ?, ?)",
                               (sample, unit, sample_id, name))
            for field, value in (rows or {}).items():
                connection.execute(
                    "INSERT INTO sample_attribute(attribute_id, sample_pk, field_name, normalized_field, raw_value) "
                    "VALUES (?, ?, ?, ?, ?)", (f"{sample}-{field}", sample, field, field.casefold(), value))
            connection.execute(
                "INSERT INTO raw_file(file_id, unit_id, path, role, size_bytes, download_url, sample_id) "
                "VALUES (?, ?, ?, 'raw', ?, ?, ?)", (f"f-{unit}-{index}", unit, name, GB, f"https://x/{unit}/{name}", sample_id))
        for number, (field, value) in enumerate(evidence):
            connection.execute(
                "INSERT INTO analysis_unit_evidence(evidence_id, unit_id, source, source_field, source_value, method, "
                "confidence) VALUES (?, ?, 'https://repository.massbank.jp/api/projects/x', ?, ?, 'declared', 1.0)",
                (f"e-{unit}-{number}", unit, field, value))
    connection.close()


class NamesAreReadByAWholeToken(unittest.TestCase):
    def test_the_plan_and_the_gate_read_names_alike(self) -> None:
        cases = {
            "20240111_EquiSPLASH_2ugmL_OAD_NEG_ddMS_10eV_3_009.lcd": ["OAD"],
            "20240510_Marmoset_4_HC_5fold_EAD14eV_POS_1.wiff2": ["EAD"],
            "raw/run_etd.d/": ["ETD"],
            "20240130_ISD_NEG_1_006.lcd": [],
            "raw/Liver_1_timsON_pos_1-2_1_5219.d/95c44b45-8ead-43e8-afd1-de952d13d4ae_1.mcf": [],
            "load_curve.raw": [],
            "CID_30eV.raw": [],
            # Identifiers name nothing (review L1): a hyphenated GUID, an all-hexadecimal run holding a digit.
            "raw/x.d/95c44b45-ead1-43e8-afd1-de952d13d4ae_1.mcf": [],
            "S_ead1.raw": [],
            "ecd2_scan.raw": [],
            "S_EAD_1.raw": ["EAD"],
        }
        for name, methods in cases.items():
            with self.subTest(name=name):
                self.assertEqual(methods, plan.file_name_methods(name))
                self.assertEqual(methods, gate._name_methods(name))

    def test_the_rule_is_interactives(self) -> None:
        try:
            from msdial_app import fragmentation
        except ImportError:
            self.skipTest(
                "SKIPPED THE THREE-WAY CHECK OF THE CID/HCD-ONLY RULE: MSDIAL_INTERACTIVE_ROOT "
                f"({contract.INTERACTIVE_ROOT}) holds no msdial_app/fragmentation.py. Point MSDIAL_INTERACTIVE_ROOT at "
                "an Interactive 0.5.52 or later (msdial-interactive-app#85) to compare the plan's, the gate's and "
                "Interactive's token rule and capability table.")
        for module in (plan, gate):
            for name in ("NON_CID_METHODS", "GUID_PATTERN", "METHOD_WORDS_PATTERN", "WORD_METHODS",
                         "METHOD_STATEMENT_FIELD_PATTERN", "INSTRUMENT_FIELD_PATTERN"):
                self.assertEqual(getattr(fragmentation, name), getattr(module, name), f"{module.__name__}.{name}")
        self.assertEqual((plan.NOT_CID_HCD, plan.CID_HCD_ONLY_RULE), (fragmentation.NOT_CID_HCD, fragmentation.CID_HCD_ONLY_RULE))
        self.assertEqual(gate.NOT_CID_HCD, fragmentation.NOT_CID_HCD)
        # One capability table, in three places (user decision, 2026-10-29).
        for module in (plan, gate):
            self.assertEqual(module.METHOD_INSTRUMENTS, fragmentation.METHOD_INSTRUMENTS)
            self.assertEqual(module.INSTRUMENT_MODEL_WORDS, fragmentation.INSTRUMENT_MODEL_WORDS)
            self.assertEqual(module.UNRECORDED_INSTRUMENTS, fragmentation.UNRECORDED_INSTRUMENTS)
        for method, instrument in (("EAD", "Thermo Scientific Exactive"), ("EAD", "Nexera/ZenoTof 7600"),
                                   ("OAD", "LCMS-9030 Shimadzu"), ("ETD", "Thermo Fisher"), ("ECD", "Agilent 6545")):
            self.assertEqual(fragmentation.instrument_can(method, instrument), plan.instrument_can(method, instrument))
            self.assertEqual(fragmentation.instrument_can(method, instrument), gate.instrument_can(method, instrument))
        for name in ("a_EThcD_b.raw", "8ead4b2c9.raw", "x-UVPD.mzML", "S_oad_1.lcd", "S_ead1.raw",
                     "95c44b45-ead1-43e8-afd1-de952d13d4ae_1.mcf", "x_EDD_y.d"):
            self.assertEqual(fragmentation.methods_in_file_name(name), plan.file_name_methods(name), name)
            self.assertEqual(fragmentation.methods_in_file_name(name), gate.methods_in_file_name(name), name)
        for values in ({"analyticalCondition / Fragmentation method": "OAD"}, {"Extract Name": "X_EAD_1"},
                       {"Dissociation": "electron-activated dissociation"}, {"Instrument collision cell": "ETD"}):
            self.assertEqual(fragmentation.stated_methods(values), plan.stated_methods(values), values)
            self.assertEqual(fragmentation.stated_methods(values), gate.stated_methods(values), values)


@unittest.skipUnless(contract.AVAILABLE, "the Interactive and Catalog checkouts are not where this test looks")
class ThePlan(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / "catalog.sqlite"
        plan_tests.build_catalog(self.database)
        self.workspace_root = self.root / "analysis"

    def manifest(self, pool: str | None = "declared", interactive: str = "0.5.52", **values) -> dict:
        catalog = ports.read_only_catalog(self.database)
        pins = {"catalog": {"version": "0.6.1"}, "libraries": [],
                **({"interactive": {"version": interactive}} if interactive else {})}
        try:
            return plan.build_manifest(
                catalog, pool=pool, campaign_id="test-cid", analysis_purpose="annotation",
                workspace_root=self.workspace_root, raw_retention_policy="keep", pins=pins, profile=None,
                campaign_policy=policy.CampaignPolicy(),
                class_decision=lambda unit_id: ports.decide_class(catalog, unit_id, "annotation"),
                catalog_database=str(self.database), **values,
            )
        finally:
            catalog.close()

    def test_a_both_unit_is_in_both_pools(self) -> None:
        add_files_unit(self.database, "zeno", "MPST000044", ["S1_pos.wiff2", "S1_neg.wiff2"], ion_mode="Both")
        add_files_unit(self.database, "lcd", "MPST000052", ["L1.lcd", "L2.lcd"], ion_mode="Both", acquisition="Unknown")
        declared = self.manifest("declared")
        unknown = self.manifest("acquisition_unknown")

        self.assertIn("zeno", [unit["unit_id"] for unit in declared["units"]])
        self.assertIn("lcd", [unit["unit_id"] for unit in unknown["units"]])
        self.assertEqual("Both", next(unit for unit in declared["units"] if unit["unit_id"] == "zeno")["ion_mode"])
        self.assertIn("one polarity, or Both", declared["selection"]["rule"])
        text = plan.summary_text(declared, "sha256:" + "0" * 64)
        self.assertIn("1 planned unit(s) declare Both polarities", text)
        self.assertIn("a file whose scans switch polarity is excluded", text)

    def add_mpst000010(self) -> None:
        """MPST000010 as the Catalog holds it: every unit's rows carry the study's keywords and its software preset;
        the EAD unit's label names it, the OAD units' rows do, and a third unit is CID by everything it says."""
        shared = {"keywords": KEYWORDS, "softwareSetting / Preset name": "MS-DIAL for OAD"}
        add_files_unit(self.database, "u86f5", "MPST000010", ["M_EAD14eV_POS_1.wiff2", "M_EAD18eV_POS_1.wiff2"],
                       label="EAD (14 eV)", instrument="ZenoTOF 7600",
                       rows={**shared, "analyticalCondition / Fragmentation method": "EAD"},
                       evidence=(("analyticalCondition/presetName", "EAD (14 eV); EAD (18 eV)"),))
        add_files_unit(self.database, "ud809", "MPST000010",
                       ["E_OAD_NEG_ddMS_10eV_1.lcd", "E_OAD_NEG_ddMS_10eV_2.lcd", "20240130_ISD_NEG_1_006.lcd"],
                       ion_mode="Negative", label="High-throughput analysis for method optimization (10 eV, neg)",
                       instrument="LCMS-9030", rows={**shared, "analyticalCondition / Fragmentation method": "OAD"})
        add_files_unit(self.database, "ucid", "MPST000010", ["C_CID_POS_1.lcd", "C_CID_POS_2.lcd"],
                       label="Lipidome analysis (pos)", instrument="LCMS-9030",
                       rows={**shared, "analyticalCondition / Fragmentation method": "CID"})

    def test_the_units_own_evidence_excludes_it_and_the_studys_text_never_does(self) -> None:
        self.add_mpst000010()
        manifest = self.manifest("declared")
        exclusions = {item["unit_id"]: item for item in manifest["exclusions"]}
        units = {unit["unit_id"]: unit for unit in manifest["units"]}

        self.assertEqual("fragmentation_not_cid_hcd", exclusions["u86f5"]["reason"])
        self.assertIn("label: EAD (14 eV)", exclusions["u86f5"]["detail"])
        self.assertIn("CID/HCD-derived", exclusions["u86f5"]["detail"])
        self.assertEqual("fragmentation_not_cid_hcd", exclusions["ud809"]["reason"],
                         "every sample row names OAD, so every file is OAD, the ISD file too")
        self.assertIn("3 of its 3 sample row(s)", exclusions["ud809"]["detail"])
        # The keywords and the software preset name OAD in every unit's rows: the study's text, not the unit's.
        self.assertIn("ucid", units)
        self.assertNotIn("fragmentation_reading", units["ucid"])
        self.assertEqual("cid_hcd_only_2026_10_29", manifest["selection"]["fragmentation_rule"])
        self.assertIn("fragmentation_not_cid_hcd", manifest["selection"]["exclusion_reasons"])
        self.assertIn("CID/HCD only (user decision, 2026-10-29)", plan.summary_text(manifest, "sha256:" + "0" * 64))

    def test_an_instruments_model_name_is_no_evidence(self) -> None:
        # MTBLS196: "LTQ Orbitrap Velos ETD" in the unit's instrument, its evidence and every row says what the
        # instrument can do, not what was done (user decision, 2026-10-29).
        model = "Thermo Scientific LTQ Orbitrap Velos ETD"
        add_files_unit(self.database, "velos", "MTBLS196X", ["F1.raw", "F2.raw"], instrument=model,
                       rows={"Parameter Value[Instrument]": model}, evidence=(("Instrument", model),))
        units = {unit["unit_id"]: unit for unit in self.manifest("declared")["units"]}

        self.assertIn("velos", units)
        self.assertNotIn("fragmentation_reading", units["velos"])

    def test_a_name_the_instrument_could_not_produce_is_recorded_and_runs(self) -> None:
        # MTBLS2372: "AA_EAD_A_n" files and rows on a Thermo Exactive; EAD there is a sample code.
        files = ["FILES/RAW_FILES/AA_EAD_A_1.raw", "FILES/RAW_FILES/AA_EAD_A_2.raw"]
        add_files_unit(self.database, "exactive", "MTBLS2372X", files, instrument="Thermo Scientific Exactive",
                       rows={"Extract Name": "AA_EAD_A_1", "MS Assay Name": "AA_EAD_A_1"})
        # A row that states the method is an explicit statement, which no instrument is asked to confirm.
        add_files_unit(self.database, "stated", "MTBLS2372Y", ["S1.raw"], instrument="Thermo Scientific Exactive",
                       rows={"analyticalCondition / Fragmentation method": "EAD"})
        add_files_unit(self.database, "zeno", "MPST000096", ["Z_EAD14eV_1.wiff2", "Z_EAD14eV_2.wiff2"],
                       instrument="Nexera/ZenoTof 7600")
        manifest = self.manifest("declared")
        units = {unit["unit_id"]: unit for unit in manifest["units"]}
        exclusions = {item["unit_id"]: item for item in manifest["exclusions"]}

        reading = units["exactive"]["fragmentation_reading"]
        self.assertEqual((0, 2, "Thermo Scientific Exactive"),
                         (reading["named_files"], reading["not_credible_files"], reading["instrument"]))
        self.assertIn("not credible on this instrument", reading["detail"])
        self.assertEqual("fragmentation_not_cid_hcd", exclusions["zeno"]["reason"], "a ZenoTOF can fragment by EAD")
        self.assertEqual("fragmentation_not_cid_hcd", exclusions["stated"]["reason"])

    def test_a_study_of_one_unit_whose_keywords_name_a_method_runs(self) -> None:
        add_files_unit(self.database, "solo", "MPST000099", ["S1.lcd", "S2.lcd"], rows={"keywords": "EAD, lipidomics"})
        manifest = self.manifest("declared")

        self.assertIn("solo", [unit["unit_id"] for unit in manifest["units"]])

    def test_every_file_named_so_excludes_the_unit(self) -> None:
        add_files_unit(self.database, "allnamed", "MPST000098", ["A_EAD_1.wiff2", "A_ETD_2.wiff2"])
        exclusions = {item["unit_id"]: item for item in self.manifest("declared")["exclusions"]}

        self.assertEqual("fragmentation_not_cid_hcd", exclusions["allnamed"]["reason"])
        self.assertIn("2 of its 2 analysis file(s) are named so", exclusions["allnamed"]["detail"])

    def test_some_files_named_so_plan_the_unit_for_an_interactive_that_keeps_them_out(self) -> None:
        add_files_unit(self.database, "partly", "MPST000097", ["P_CID_1.lcd", "P_OAD_2.lcd", "P_CID_3.lcd"])
        manifest = self.manifest("declared", interactive="0.5.52")
        unit = next(unit for unit in manifest["units"] if unit["unit_id"] == "partly")

        self.assertEqual(
            {"rule": "cid_hcd_only_2026_10_29", "evidence": "catalog", "methods": ["OAD"], "named_files": 1,
             "analysis_files": 3, "named_file_names": ["P_OAD_2.lcd"], "not_credible_files": 0, "instrument": "",
             "named_samples": 0, "samples": 3, "stated_samples": 0, "stated_files": 0},
            {key: value for key, value in unit["fragmentation_reading"].items() if key != "detail"})
        self.assertIn("kept out by Interactive's lease", unit["fragmentation_reading"]["detail"])
        self.assertIn("1 planned unit(s) run without some files", plan.summary_text(manifest, "sha256:" + "0" * 64))
        with self.assertRaisesRegex(plan.PlanError, "needs 0.5.52 or later.*1 planned unit"):
            self.manifest("declared", interactive="0.5.50")

    def test_an_interactive_before_0_5_52_is_refused_whatever_the_units(self) -> None:
        # Review L2: refused unconditionally, not only where a unit's files are named so.
        with self.assertRaisesRegex(plan.PlanError, "The pinned Interactive is 0.5.51; the campaign needs 0.5.52"):
            self.manifest("declared", interactive="0.5.51")
        self.assertTrue(self.manifest("declared", interactive="0.5.52")["units"])

    def test_rows_that_state_the_method_keep_their_files_out(self) -> None:
        # Review M2: some rows state EAD; their files are planned to be kept out, the rest runs.
        add_files_unit(self.database, "rows", "MPST000095", ["R1.wiff2", "R2.wiff2", "R3.wiff2"],
                       instrument="Nexera/ZenoTof 7600")
        connection = sqlite3.connect(self.database)
        with connection:
            connection.execute(
                "INSERT INTO sample_attribute(attribute_id, sample_pk, field_name, normalized_field, raw_value) "
                "VALUES ('stated-r2', 'pk-rows-1', 'analyticalCondition / Fragmentation method', 'x', 'EAD')")
        connection.close()
        manifest = self.manifest("declared")
        unit = next(unit for unit in manifest["units"] if unit["unit_id"] == "rows")
        reading = unit["fragmentation_reading"]

        self.assertEqual((1, ["R2.wiff2"], 1, 1), (reading["named_files"], reading["named_file_names"],
                                                    reading["stated_samples"], reading["stated_files"]))
        self.assertIn("the 1 file(s) of the 1 row(s) that state the method are kept out by Interactive's lease",
                      reading["detail"])

    def test_a_statement_every_unit_of_a_study_makes_is_still_the_units(self) -> None:
        # Review M3: the study-shared filter reads names, never a field that states the method.
        for unit in ("ea", "eb"):
            add_files_unit(self.database, unit, "MPST000094", [f"{unit}1.wiff2", f"{unit}2.wiff2"],
                           instrument="ZenoTOF 7600", rows={"analyticalCondition / Fragmentation method": "EAD"})
        exclusions = {item["unit_id"]: item["reason"] for item in self.manifest("declared")["exclusions"]}

        self.assertEqual({"ea": "fragmentation_not_cid_hcd", "eb": "fragmentation_not_cid_hcd"},
                         {key: exclusions.get(key) for key in ("ea", "eb")})


class TheGate(unittest.TestCase):
    # A lease of Interactive 0.5.52 records what the rule read; the gate judges only such a lease (review L2).
    CAMPAIGN = {"campaign_authorizations": [{"approval_id": "A1"}],
                "fragmentation_check": {"schema": "msdial-fragmentation-check.v1", "rule": "cid_hcd_only_2026_10_29"}}

    def test_a_named_input_that_reaches_the_run_fails(self) -> None:
        manifest = {**self.CAMPAIGN, "input_candidates": [r"D:\w\raw\data\S1_CID.lcd", r"D:\w\raw\data\S2_OAD_NEG.lcd"]}
        rows = [{"file_path": r"D:\w\raw\data\S1_CID.lcd"}, {"file_path": r"D:\w\raw\data\S2_OAD_NEG.lcd"}]

        problems = gate._not_cid_hcd_problems(manifest, manifest, rows)

        self.assertEqual(1, len(problems))
        self.assertIn("S2_OAD_NEG.lcd: OAD", problems[0])
        before = {key: value for key, value in manifest.items() if key != "fragmentation_check"}
        self.assertEqual([], gate._not_cid_hcd_problems(before, None, rows),
                         "a lease before 0.5.52 applied no such rule, and is gated as it always was")

    def test_an_input_kept_out_without_evidence_that_holds_fails(self) -> None:
        manifest = {**self.CAMPAIGN, "input_candidates": [r"D:\w\S1.lcd"], "excluded_input_candidates": [
            {"path": r"D:\w\S2_EAD14eV.wiff2", "reason": "fragmentation_not_cid_hcd", "evidence": "file_name"},
            {"path": r"D:\w\S3.mzML", "reason": "fragmentation_not_cid_hcd", "evidence": "mzml_activation",
             "methods": ["electron activated dissociation"]},
        ]}
        self.assertEqual([], gate._not_cid_hcd_problems(manifest, manifest, [{"file_path": r"D:\w\S1.lcd"}]))

        manifest["excluded_input_candidates"].append(
            {"path": r"D:\w\S4.wiff2", "reason": "fragmentation_not_cid_hcd", "evidence": "file_name"})
        problems = gate._not_cid_hcd_problems(manifest, manifest, [{"file_path": r"D:\w\S1.lcd"}])
        self.assertEqual(1, len(problems))
        self.assertIn("S4.wiff2 (evidence file_name)", problems[0])

    def test_a_name_is_held_to_the_units_instrument(self) -> None:
        handoff = {"repository_metadata": {"catalog_handoff": {"technical_settings": {
            "instrument": "Thermo Scientific Exactive"}}}}
        manifest = {**self.CAMPAIGN, "project": handoff, "input_candidates": [r"D:\w\AA_EAD_A_1.raw"],
                    "excluded_input_candidates": [
                        {"path": r"D:\w\AA_EAD_A_2.raw", "reason": "fragmentation_not_cid_hcd", "evidence": "file_name"}]}

        problems = gate._not_cid_hcd_problems(manifest, manifest, [{"file_path": r"D:\w\AA_EAD_A_1.raw"}])

        self.assertEqual(1, len(problems), "the running file is no problem; the one kept out on its name is")
        self.assertIn("AA_EAD_A_2.raw (evidence file_name)", problems[0])

    def test_a_sample_whose_row_states_the_method_does_not_run(self) -> None:
        stated = {"analyticalCondition / Fragmentation method": "EAD"}
        project = {"sample_metadata": [{"sample_id": "S1", "values": {}}, {"sample_id": "S2", "values": stated}]}
        manifest = {**self.CAMPAIGN, "project": project, "input_candidates": [r"D:\w\S1.wiff2"],
                    "excluded_input_candidates": [{"path": r"D:\w\S2.wiff2", "reason": "fragmentation_not_cid_hcd",
                                                   "evidence": "sample_row", "methods": ["EAD"]}]}
        self.assertEqual([], gate._not_cid_hcd_problems(manifest, manifest, [{"file_path": r"D:\w\S1.wiff2",
                                                                             "sample_id": "S1"}]))

        problems = gate._not_cid_hcd_problems(manifest, manifest, [{"file_path": r"D:\w\S2.wiff2", "sample_id": "S2"}])
        self.assertEqual(1, len(problems))
        self.assertIn("sample(s) whose row states", problems[0])
        # A CSV row that carries no sample_id is its input's lineage row's sample.
        manifest["input_lineage"] = {"rows": [{"path": r"D:\w\S2.wiff2", "sample_id": "S2"}]}
        problems = gate._not_cid_hcd_problems(manifest, manifest, [{"file_path": r"D:\w\S2.wiff2"}])
        self.assertEqual(1, len(problems))

    def test_conv1_leaves_out_a_conversion_the_disposition_excluded(self) -> None:
        # Review L3: a Both unit's mzXML whose scans record no polarity converts with none, and the disposition
        # excludes it as polarity_unresolved; CONV-1 does not fail the unit for it.
        with tempfile.TemporaryDirectory() as temporary:
            unit, _row, record = conversion_tests._downloaded_conversion(temporary)
            conversion_tests._scan_without_polarity(record)
            conversion_tests._declare(unit.manifest, handoff="Both")
            conversion_tests._csv(unit, paths=[])
            failing = conversion_tests._conv1(unit)
            unit.manifest["campaign_disposition"] = {
                "schema": "msdial-campaign-disposition.v1", "disposition": "run", "reasons": [], "warnings": [],
                "excluded_inputs": [{"path": record["output"]["path"], "reason": "polarity_unresolved"}],
                "split_key": None,
            }
            check = conversion_tests._conv1(unit)

        self.assertEqual(gate.FAIL, failing.status)
        self.assertNotEqual(gate.FAIL, check.status, check.detail)
        self.assertEqual(1, check.evidence["excluded_by_disposition"])

    def test_a_part_of_a_both_unit_is_grouped_as_its_lease_grouped_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "run-manifest.json"
            parent.write_text(gate.json.dumps({
                "project": {"ion_mode": "Both", "repository_metadata": {
                    "catalog_handoff": {"technical_settings": {"ion_mode": "Both"}}}},
                "raw_metadata_preflight": {"summary": {}},
            }), encoding="utf-8")
            part = {"project": {"ion_mode": "Positive"}, "split_from": {"manifest_path": str(parent)}}

            self.assertEqual("", gate._unit_polarity(part), "the lease read the Both declaration and grouped by none")


class TheRunner(machine_tests.Base):
    def test_each_part_of_a_unit_split_by_polarity_carries_its_own_polarity(self) -> None:
        world = self.world(("u1",))
        world.scripts["u1"] = fakes.UnitScript(disposition="split", split_modes=("dda-pos", "dda-neg"))
        book = self.finish(world)
        parts = {unit["unit_key"]: unit for unit in book.units() if unit["parent_unit_key"] == "u1"}

        self.assertEqual({"u1-dda-pos": "Positive", "u1-dda-neg": "Negative"},
                         {key: unit["ion_mode"] for key, unit in parts.items()})
        self.assertTrue(all(unit["state"] == "done" for unit in parts.values()))


@unittest.skipUnless(contract.AVAILABLE, "the Interactive and Catalog checkouts are not where this test looks")
class TheSplitContract(unittest.TestCase):
    """Review M1: the runner reads each part's polarity from the real tool's projection of a real split."""

    def test_a_real_polarity_split_reaches_the_runners_part_record(self) -> None:
        from msdial_app import mcp_server, repository_reanalysis

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "mb_post" / "MPST1" / "uboth"
            data = root / "raw" / "data"
            data.mkdir(parents=True)
            (root / "provenance").mkdir()
            files = []
            for name in ("S1_pos.wiff2", "S1_neg.wiff2"):
                (data / name).write_bytes(b"x")
                files.append(str(data / name))
            per_file = [{"file": item, "acquisition_mode": "DDA", "ms_levels": [1, 2], "outcome": "ok",
                         "polarity": "Positive" if "_pos" in Path(item).name else "Negative"} for item in files]
            path = root / "provenance" / "run-manifest.json"
            path.write_text(json.dumps({
                "schema": "msdial-public-reanalysis-run.v1", "status": "preflight_review_required", "workspace": str(root),
                "raw_directory": str(root / "raw"), "input_directory": str(data), "input_candidates": files,
                "project": {"analysis_unit_id": "uboth", "repository": "mb_post", "accession": "MPST1", "ion_mode": "Both",
                            "acquisition_mode": "DDA", "separation": "LC-MS", "untargeted": True,
                            "sample_metadata": [{"sample_id": Path(item).stem, "raw_file": Path(item).name}
                                                for item in files]},
                "raw_metadata_preflight": {"summary": {"coverage": {"complete": True}, "acquisition_mode": "DDA",
                                                       "per_file": per_file}},
            }), encoding="utf-8")
            split = repository_reanalysis.split_unit_by_acquisition(path, confirmed=True)
            with mock.patch.object(mcp_server, "_request_json", return_value=split):
                result = ports.InteractivePort(port=8766).split(manifest_path=str(path), authorization_path="a.json")

        if "polarity" not in (result.get("parts") or [{}])[0]:
            self.skipTest("SKIPPED: MSDIAL_INTERACTIVE_ROOT points at an Interactive whose split tool returns no part "
                          "polarity (before 0.5.52, msdial-interactive-app#85)")
        self.assertEqual({"uboth-dda-pos": "Positive", "uboth-dda-neg": "Negative"},
                         {part["analysis_unit_id"]: part["polarity"] for part in result["parts"]})
        # The fake answers in the same shape as the real tool.
        self.assertTrue({"polarity", "split_key"} <= set(result["parts"][0]))


if __name__ == "__main__":
    unittest.main()
