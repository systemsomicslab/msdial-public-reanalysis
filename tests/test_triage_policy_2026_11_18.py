"""The triage policy of 2026-11-18 (skill 1.3.0): what the stage4 triage run asked a person, decided once as rules.

docs/llm-assisted-curation.md section 2.2. The stage4 triage run held every accession: 47 of 48 units asked about
MS1-only files, mixed acquisition, file evidence against the Catalog, profile-mode injections, shared copies and units
with no files. The person's decision of 2026-11-18 took the recommended answers; these tests hold the rules that encode
them, on synthetic accessions (ST000001 and AN000001 style ids only):

- P1: MS1-only files are excluded as ms1_only_no_msms with a confirmed informational flag ms1_only_excluded; never at
  low confidence and never on a file whose name says product-ion spectra (a DDA, DIA or MSe token, or an MS/MS word
  such as MSMS, MS2 or AutoMSMS); a unit left with no MS/MS file is excluded (no_product_ion_files), which is not a
  hold, but only when it drops no file with product-ion spectra for another reason.
- P2: children of one acquisition mode each are not a mix; a mix inside one run still needs mixed_acquisition.
- P4: declared_technical_disputed is informational: a ready unit may read its files against the Catalog with it.
- P5: byte-identical copies in two arms' places exclude both units (shared_content_unassigned), with the
  shared_content flag kept as evidence and no X-DUP-NOKEPT.
- no_files and no_sample_metadata; the excluded status's own checks (no blocking flag; a no_files unit whose files
  may sit in the archive under other names holds); the plan's triage_unit_excluded and its counts by reason; the
  digest's MS-level words and method folders; a payload of a skill before 1.3.0 judged by its own rules.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(ROOT / "scripts"))
from campaign import curated, plan  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ex = _load("expand_triage_spec_for_policy_tests", ROOT / "scripts" / "expand-triage-spec.py")
vt = _load("validate_triage_for_policy_tests", ROOT / "scripts" / "validate-triage.py")
dg = _load("triage_digest_for_policy_tests", ROOT / "scripts" / "triage-digest.py")

ACC = "ST000001"
U1, U2 = "AN000001", "AN000002"
CTX = ex.ExpansionContext(decided_at="2026-11-18T20:00:00+09:00", model="synthetic-model", skill_version="1.3.0")
NAME_EV = [{"source": "filename_token", "field": "name", "value": "{member_path}"}]


def inputs(units: dict[str, tuple[str, str, list[dict]]], listing: list[tuple[str, int, str]]) -> dict[str, bytes]:
    """{unit: (chromatography, ion mode, rows)} and the listing (path, size, crc32) as an inputs directory's files."""
    out: dict[str, bytes] = {}
    for unit, (chrom, ion, rows) in units.items():
        handoff = {"repository": "metabolomics_workbench", "accession": ACC, "analysis_unit_id": unit,
                   "source_subrecord_id": unit, "technical_settings": {"chromatography": chrom, "ion_mode": ion,
                                                                      "acquisition_mode": "DDA", "target_omics": "Lipidomics"}}
        out[f"{unit}/handoff.json"] = json.dumps(handoff, indent=2).encode("utf-8")
        out[f"{unit}/samples.json"] = json.dumps(rows, indent=2).encode("utf-8")
    lines = ["archive\tpath\tsize\tcrc32"] + [f"{ACC}.zip\t{p}\t{s}\t{c}" for p, s, c in listing]
    out[f"{ACC}.members.tsv"] = ("\n".join(lines) + "\n").encode("utf-8")
    return out


def rows(*names: str) -> list[dict]:
    return [{"sample_id": n, "raw_file": f"{n}.mzML", "attributes": {}} for n in names]


def expand(spec: dict, files: dict[str, bytes]):
    return ex.expand(json.dumps(spec, indent=1).encode("utf-8"), ex.inputs_from_files(files), CTX,
                     spec_name=f"{ACC}.spec.json")


def validate(files: dict[str, bytes], payloads: dict[str, dict]) -> "vt.Report":
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        source = directory / "inputs"
        for relative, data in files.items():
            (source / relative).parent.mkdir(parents=True, exist_ok=True)
            (source / relative).write_bytes(data)
        for unit, document in payloads.items():
            (directory / f"{unit}.json").write_text(json.dumps(document, indent=1), encoding="utf-8")
        return vt.validate(directory, vt.read_catalog(source), vt.read_listing(source / f"{ACC}.members.tsv"),
                           payload_only=True)


def errors(report, check: str | None = None, unit: str | None = None) -> list[str]:
    return [f.message for f in report.findings if f.level == "error" and (check is None or f.check == check)
            and (unit is None or f.unit == unit)]


class PolicyCase(unittest.TestCase):
    files: dict[str, bytes]

    def expanded(self, spec: dict) -> dict[str, dict]:
        result = expand(spec, self.files)
        self.assertTrue(result.ok, [f.as_dict() for f in result.findings])
        return {u: json.loads(b) for u, b in result.payloads.items()}

    def judged(self, spec: dict):
        return validate(self.files, self.expanded(spec))


# ---- P1 and P4: MS1-only sample runs, MS/MS pools, the Catalog's declared DDA ------------------------------------

MS1_LISTING = [
    ("POS/S01_posMS.mzML", 3000001, "00000001"),
    ("POS/S02_posMS.mzML", 3000002, "00000002"),
    ("POS/Pool_posMSMS.mzML", 3000003, "00000003"),
    ("POS/Pool_posMSProfile.mzML", 9000004, "00000004"),
    ("POS/S99_posMSMS.mzML", 3000005, "00000005"),
    ("POS/QC_posDDA.mzML", 3000006, "00000006"),
    ("NEG/S01_negMS.mzML", 3000011, "00000011"),
    ("NEG/S02_negMS.mzML", 3000012, "00000012"),
]


def ms1_spec() -> dict:
    ms1 = {"exclude": "ms1_only_no_msms", "detail": "MS1-only run: mwTab says the sample runs are MS1 data, and the name's "
           "MS word contrasts with the study's MSMS pools."}
    return {
        "schema": "msdial-unit-triage-spec.v1", "accession": ACC, "phase": "pre_download",
        "structure": "POS/ holds the positive arm, NEG/ the negative; sample runs are MS1 (MS), pools MS/MS (MSMS).",
        "groups": [
            {"id": "RPOS", "applies_to": "rows", "match": {"unit": U1}, "disposition": ms1, "confidence": "high",
             "rationale": "The sample runs are MS1 only, against the study's MSMS pools."},
            {"id": "RNEG", "applies_to": "rows", "match": {"unit": U2}, "disposition": ms1, "confidence": "high",
             "rationale": "The sample runs are MS1 only."},
            {"id": "POOL", "applies_to": "members", "match": {"paths": ["POS/Pool_posMSMS.mzML"]},
             "file": {"role": "qc", "ion_mode": "positive", "chromatography": "RP", "acquisition_mode": "DDA", "basis": "inferred"},
             "evidence": NAME_EV, "disposition": {"keep": U1}, "confidence": "medium",
             "rationale": "The positive pooled MS/MS injection."},
            {"id": "PROF", "applies_to": "members", "match": {"paths": ["POS/Pool_posMSProfile.mzML"]},
             "disposition": {"exclude": "ms1_only_no_msms", "detail": "Profile-mode MS1 injection of the pool."},
             "confidence": "high", "rationale": "MSProfile against the MSMS pool of the same sample."},
            {"id": "EXTRA", "applies_to": "members", "match": {"paths": ["POS/S99_posMSMS.mzML"]},
             "disposition": {"exclude": "no_sample_metadata", "detail": "A sample no Catalog row or Samples line names."},
             "confidence": "high", "rationale": "Samples need metadata."},
            {"id": "QCDDA", "applies_to": "members", "match": {"paths": ["POS/QC_posDDA.mzML"]},
             "disposition": {"exclude": "no_sample_metadata", "detail": "A QC injection of another submission."},
             "confidence": "medium", "rationale": "No row or folder ties it to this study's units."},
        ],
        "units": {
            U1: {"status": "ready", "confidence": "high", "rationale": "Only the pooled MS/MS injection carries product-ion spectra.",
                 "unit_technical": {"ion_mode": "positive", "chromatography": "RP", "acquisition_mode": "DDA"},
                 "flags": [{"code": "ms1_only_excluded", "certainty": "confirmed", "detail": "mwTab MS_COMMENTS: runs are MS1 data.",
                            "groups": ["RPOS", "PROF"]},
                           {"code": "declared_technical_disputed", "certainty": "suspected",
                            "detail": "The Catalog declares DDA for every file; the sample runs are MS1."}]},
            U2: {"status": "excluded", "confidence": "high", "rationale": "No negative MS/MS run is deposited.",
                 "exclusion": {"reason": "no_product_ion_files", "detail": "Every negative run is MS1 only."},
                 "unit_technical": {"ion_mode": "negative", "chromatography": "RP", "acquisition_mode": "DDA"},
                 "flags": [{"code": "ms1_only_excluded", "certainty": "confirmed", "detail": "mwTab: runs are MS1 data.",
                            "groups": ["RNEG"]}]},
        },
    }


class MS1OnlyTests(PolicyCase):
    def setUp(self) -> None:
        self.files = inputs({U1: ("Reversed phase", "Positive", rows("S01_posMS", "S02_posMS")),
                             U2: ("Reversed phase", "Negative", rows("S01_negMS", "S02_negMS"))}, MS1_LISTING)

    def test_ms1_only_files_are_left_out_and_nothing_is_asked(self) -> None:
        payloads = self.expanded(ms1_spec())
        report = validate(self.files, payloads)
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])
        self.assertEqual(report.unit_status, {U1: "ready", U2: "excluded"})
        one, two = payloads[U1], payloads[U2]
        self.assertEqual({r["reason"] for r in one["excluded_rows"]}, {"ms1_only_no_msms"})
        self.assertEqual({m["member_path"]: m["reason"] for m in one["excluded_members"]},
                         {"POS/Pool_posMSProfile.mzML": "ms1_only_no_msms", "POS/S99_posMSMS.mzML": "no_sample_metadata",
                          "POS/QC_posDDA.mzML": "no_sample_metadata"})
        self.assertEqual([m["member_path"] for m in one["added_members"]], ["POS/Pool_posMSMS.mzML"])
        self.assertEqual(two["exclusion"], {"reason": "no_product_ion_files", "detail": "Every negative run is MS1 only."})
        self.assertEqual(list(two).index("exclusion"), list(two).index("status") + 1)
        self.assertNotIn("exclusion", one)
        schema = vt.load_schema(vt.SCHEMA_V2_ID)
        for document in payloads.values():
            self.assertEqual(vt.schema_errors(document, schema, schema), [])

    def test_the_ms1_exclusion_needs_its_flag(self) -> None:
        spec = ms1_spec()
        spec["units"][U1]["flags"] = [f for f in spec["units"][U1]["flags"] if f["code"] != "ms1_only_excluded"]
        found = errors(self.judged(spec), "T-EXCL", U1)
        self.assertTrue(any("without an ms1_only_excluded flag" in m for m in found), found)

    def test_a_weak_ms1_reading_keeps_the_file(self) -> None:
        spec = ms1_spec()
        spec["groups"][0]["confidence"] = "low"
        found = errors(self.judged(spec), "T-EXCL", U1)
        self.assertTrue(any("at low confidence" in m and "ms1_only_suspected" in m for m in found), found)

    def test_a_file_named_dda_is_not_left_out_as_ms1_only(self) -> None:
        spec = ms1_spec()
        spec["groups"][5]["disposition"] = {"exclude": "ms1_only_no_msms", "detail": "MS1 only."}
        found = errors(self.judged(spec), "T-EXCL", U1)
        self.assertTrue(any("QC_posDDA" in m and "its name says DDA" in m for m in found), found)

    def test_ms1_only_suspected_is_informational(self) -> None:
        spec = ms1_spec()
        spec["units"][U1]["flags"].append({"code": "ms1_only_suspected", "certainty": "suspected",
                                           "detail": "A name alone; the header preflight decides."})
        self.assertEqual(self.judged(spec).verdict, "pass")

    def test_declared_technical_disputed_no_longer_holds(self) -> None:
        # P4: the files say HILIC against the Catalog's reversed phase; flagged, the unit is ready and passes.
        spec = ms1_spec()
        spec["groups"][2]["file"]["chromatography"] = "HILIC"
        spec["units"][U1]["unit_technical"]["chromatography"] = "HILIC"
        report = self.judged(spec)
        self.assertEqual((report.verdict, report.unit_status[U1]), ("pass", "ready"), [f.as_dict() for f in report.findings])
        # Without the flag the difference from the declaration is refused (T-DECL).
        spec["units"][U1]["flags"] = [f for f in spec["units"][U1]["flags"] if f["code"] != "declared_technical_disputed"]
        self.assertTrue(errors(self.judged(spec), "T-DECL", U1))

    def test_an_exclusion_goes_with_the_excluded_status_only(self) -> None:
        spec = ms1_spec()
        del spec["units"][U2]["exclusion"]
        result = expand(spec, self.files)
        self.assertIn("X-SCHEMA", {f.code for f in result.findings})
        spec = ms1_spec()
        spec["units"][U1]["exclusion"] = {"reason": "no_files", "detail": "Not so."}
        result = expand(spec, self.files)
        self.assertIn("X-SCHEMA", {f.code for f in result.findings})

    def test_an_excluded_unit_keeps_asks_and_holds_nothing(self) -> None:
        payloads = self.expanded(ms1_spec())
        one = copy.deepcopy(payloads[U1])
        one["status"], one["exclusion"] = "excluded", {"reason": "no_product_ion_files", "detail": "Not so."}
        found = errors(validate(self.files, {**payloads, U1: one}), "T-STATUS", U1)
        self.assertTrue(any("keeps no file" in m for m in found), found)
        two = copy.deepcopy(payloads[U2])
        two["needs_person"] = [{"code": "other", "question": "Anything?"}]
        found = errors(validate(self.files, {**payloads, U2: two}), "T-STATUS", U2)
        self.assertTrue(any("asks nothing" in m for m in found), found)
        two = copy.deepcopy(payloads[U2])
        two["exclusion"]["reason"] = "shared_content_unassigned"
        found = errors(validate(self.files, {**payloads, U2: two}), "T-STATUS", U2)
        self.assertTrue(any("without a shared_content flag" in m for m in found), found)
        two = copy.deepcopy(payloads[U2])
        for item in [*two["excluded_rows"], *two["excluded_members"]]:
            if item["reason"] == "ms1_only_no_msms":
                item["reason"] = "other"
        found = errors(validate(self.files, {**payloads, U2: two}), "T-STATUS", U2)
        self.assertTrue(any("leaves no file out as ms1_only_no_msms" in m for m in found), found)
        one = copy.deepcopy(payloads[U1])
        one["exclusion"] = {"reason": "no_files", "detail": "Not so."}
        found = errors(validate(self.files, {**payloads, U1: one}), "T-STATUS", U1)
        self.assertTrue(any("only an excluded unit names one" in m for m in found), found)

    def test_no_sample_metadata_is_a_members_reason(self) -> None:
        payloads = self.expanded(ms1_spec())
        one = copy.deepcopy(payloads[U1])
        one["excluded_rows"][0]["reason"] = "no_sample_metadata"
        found = errors(validate(self.files, {**payloads, U1: one}), "T-SCHEMA", U1)
        self.assertTrue(any("no_sample_metadata" in m for m in found), found)

    def test_the_plan_leaves_an_excluded_unit_out_with_its_reason(self) -> None:
        payloads = self.expanded(ms1_spec())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "triage"
            accession_dir = root / ACC
            accession_dir.mkdir(parents=True)
            for unit, document in payloads.items():
                (accession_dir / f"{unit}.json").write_text(json.dumps(document, indent=1), encoding="utf-8")
            entries, reason, detail = curated.unit_entries(accession_dir=accession_dir, triage_root=root, unit_id=U2,
                                                           validation={"verdict": "pass"}, catalog=None)
            self.assertEqual((entries, reason), ([], curated.TRIAGE_UNIT_EXCLUDED))
            # The detail counts what the unit left out, by reason, so the plan's reader sees which files went.
            self.assertEqual(detail, "no_product_ion_files: Every negative run is MS1 only. (left out: rows "
                             "ms1_only_no_msms 2; members belongs_to_other_unit 1, ms1_only_no_msms 1, no_sample_metadata 2)")
            # An accession that does not pass is still triage_not_pass, whatever its units' statuses.
            _, reason, _ = curated.unit_entries(accession_dir=accession_dir, triage_root=root, unit_id=U2,
                                                validation={"verdict": "hold"}, catalog=None)
            self.assertEqual(reason, curated.TRIAGE_NOT_PASS)


# ---- P2: mixed acquisition split into children ------------------------------------------------------------------

def children_spec(dda_child: str = "dda", dia_child: str = "dia") -> dict:
    def group(gid: str, folder: str, mode: str, child: str) -> dict:
        return {"id": gid, "applies_to": "rows", "match": {"resolved_member": {"folder": folder}},
                "file": {"role": "sample", "ion_mode": "positive", "chromatography": "RP", "acquisition_mode": mode,
                         "basis": "inferred"},
                "evidence": [{"source": "folder_token", "field": "folder", "value": "{folder}"}],
                "disposition": {"keep": U1, "child": child}, "confidence": "high", "rationale": f"The {mode} folder."}

    return {
        "schema": "msdial-unit-triage-spec.v1", "accession": ACC, "phase": "pre_download",
        "structure": "DDA/ and DIA/ hold one positive reversed-phase study acquired twice.",
        "groups": [group("GDDA", "DDA", "DDA", dda_child), group("GDIA", "DIA", "DIA", dia_child)],
        "units": {U1: {"status": "ready", "confidence": "high", "rationale": "One child per acquisition mode.",
                       "unit_technical": {"ion_mode": "positive", "chromatography": "RP", "acquisition_mode": "mixed"},
                       "children": [{"child_key": "dda", "ion_mode": "positive", "acquisition_mode": "DDA"},
                                    {"child_key": "dia", "ion_mode": "positive", "acquisition_mode": "DIA"}]}},
    }


class ChildrenTests(PolicyCase):
    def setUp(self) -> None:
        self.files = inputs({U1: ("Reversed phase", "Positive", rows("S1_pos_DDA", "S2_pos_DDA", "S1_pos_DIA", "S2_pos_DIA"))},
                            [("DDA/S1_pos_DDA.mzML", 3000001, "00000001"), ("DDA/S2_pos_DDA.mzML", 3000002, "00000002"),
                             ("DIA/S1_pos_DIA.mzML", 3000003, "00000003"), ("DIA/S2_pos_DIA.mzML", 3000004, "00000004")])

    def test_children_of_one_mode_each_are_not_a_mix(self) -> None:
        report = self.judged(children_spec())
        self.assertEqual((report.verdict, report.unit_status), ("pass", {U1: "ready"}), [f.as_dict() for f in report.findings])

    def test_a_mix_inside_one_child_still_needs_the_flag(self) -> None:
        spec = children_spec(dia_child="dda")
        spec["units"][U1]["children"] = [{"child_key": "dda", "ion_mode": "positive", "acquisition_mode": "mixed"}]
        found = errors(self.judged(spec), "T-ACQ", U1)
        self.assertTrue(any("child dda's files mix acquisition modes ['DDA', 'DIA']" in m for m in found), found)


# ---- P5 and no_files ---------------------------------------------------------------------------------------------

def shared_spec() -> dict:
    copies = {"exclude": "shared_content_unassigned", "detail": "Byte-identical to the other arm's copy; reassigned after download."}
    return {
        "schema": "msdial-unit-triage-spec.v1", "accession": ACC, "phase": "pre_download",
        "structure": "HILIC_NEG/ and LIPID_NEG/ hold byte-identical sample files.",
        "groups": [
            {"id": "HCOPY", "applies_to": "rows", "match": {"unit": U1, "resolved_member": {"twin": True}},
             "disposition": copies, "confidence": "high", "rationale": "Each file has a CRC32-and-size twin in the other arm's folder."},
            {"id": "LCOPY", "applies_to": "rows", "match": {"unit": U2, "resolved_member": {"twin": True}},
             "disposition": copies, "confidence": "high", "rationale": "Each file has a CRC32-and-size twin in the other arm's folder."},
            {"id": "LBLANK", "applies_to": "rows", "match": {"unit": U2, "sample_id": {"regex": "^Blank"}},
             "disposition": {"exclude": "other", "detail": "The unit is excluded; its unique blank is not run alone."},
             "confidence": "high", "rationale": "A blank alone is no run."},
        ],
        "units": {
            U1: {"status": "excluded", "confidence": "high", "rationale": "Its files are byte-identical to the other arm's.",
                 "exclusion": {"reason": "shared_content_unassigned", "detail": "Copies in two arms' folders."},
                 "unit_technical": {"ion_mode": "negative", "chromatography": "HILIC", "acquisition_mode": "DDA"},
                 "flags": [{"code": "shared_content", "certainty": "confirmed", "groups": ["HCOPY"],
                            "detail": "Same CRC32 and size as LIPID_NEG/; the mzML sourceFile would settle it."}]},
            U2: {"status": "excluded", "confidence": "high", "rationale": "Its files are byte-identical to the other arm's.",
                 "exclusion": {"reason": "shared_content_unassigned", "detail": "Copies in two arms' folders."},
                 "unit_technical": {"ion_mode": "negative", "chromatography": "RP", "acquisition_mode": "DDA"},
                 "flags": [{"code": "shared_content", "certainty": "confirmed", "groups": ["LCOPY"],
                            "detail": "Same CRC32 and size as HILIC_NEG/; the mzML sourceFile would settle it."}]},
        },
    }


class SharedContentTests(PolicyCase):
    def setUp(self) -> None:
        self.files = inputs({U1: ("HILIC", "Negative", rows("S1_Hilic_Neg", "S2_Hilic_Neg")),
                             U2: ("Reversed phase", "Negative", rows("S1_Lipid_Neg", "S2_Lipid_Neg", "Blank_Lipid_Neg"))},
                            [("HILIC_NEG/S1_Hilic_Neg.mzML", 4000001, "0000AA01"), ("HILIC_NEG/S2_Hilic_Neg.mzML", 4000002, "0000AA02"),
                             ("LIPID_NEG/S1_Lipid_Neg.mzML", 4000001, "0000AA01"), ("LIPID_NEG/S2_Lipid_Neg.mzML", 4000002, "0000AA02"),
                             ("LIPID_NEG/Blank_Lipid_Neg.mzML", 4000009, "0000AA09")])

    def test_both_arms_are_excluded_and_nothing_is_asked(self) -> None:
        payloads = self.expanded(shared_spec())
        report = validate(self.files, payloads)
        self.assertEqual((report.verdict, report.unit_status), ("pass", {U1: "excluded", U2: "excluded"}),
                         [f.as_dict() for f in report.findings])
        self.assertEqual({r["reason"] for r in payloads[U1]["excluded_rows"]}, {"shared_content_unassigned"})
        self.assertEqual([f["code"] for f in payloads[U2]["flags"]], ["shared_content"])

    def test_the_copies_count_as_held_for_the_shared_content_rule(self) -> None:
        payloads = self.expanded(shared_spec())
        with tempfile.TemporaryDirectory() as folder:
            tsv = Path(folder) / "listing.tsv"
            tsv.write_bytes(self.files[f"{ACC}.members.tsv"])
            listed = vt.read_listing(tsv)
        resolver = vt.Resolver(m.path for m in listed)
        held = vt.held_keys(payloads, resolver)
        self.assertEqual(sorted(held), ["hilic_neg/s1_hilic_neg.mzml", "hilic_neg/s2_hilic_neg.mzml",
                                        "lipid_neg/s1_lipid_neg.mzml", "lipid_neg/s2_lipid_neg.mzml"])
        # D2: the shared_content flags are about copies the units hold, so neither is refused as about nothing.
        self.assertEqual(vt.shared_content_without_keepers(payloads, vt.content_twins(listed), resolver), [])

    def test_the_flag_is_kept_as_evidence(self) -> None:
        spec = shared_spec()
        spec["units"][U1]["flags"] = []
        found = errors(self.judged(spec), "T-STATUS", U1)
        self.assertTrue(any("without a shared_content flag" in m for m in found), found)

    def test_the_reason_belongs_to_an_excluded_unit(self) -> None:
        spec = shared_spec()
        spec["units"][U1] = {**spec["units"][U1], "status": "needs_person",
                             "needs_person": [{"code": "shared_content", "question": "Which arm?", "groups": ["HCOPY"]}]}
        del spec["units"][U1]["exclusion"]
        found = errors(self.judged(spec), "T-EXCL", U1)
        self.assertTrue(any("not excluded as shared_content_unassigned" in m for m in found), found)

    def test_a_copy_without_a_twin_is_not_shared_content_unassigned(self) -> None:
        spec = shared_spec()
        spec["groups"][2]["disposition"] = {"exclude": "shared_content_unassigned", "detail": "Not a copy."}
        found = errors(self.judged(spec), "T-EXCL", U2)
        self.assertTrue(any("Blank_Lipid_Neg" in m and "no byte-identical twin" in m for m in found), found)


class NoFilesTests(PolicyCase):
    def setUp(self) -> None:
        self.files = inputs({U1: ("Reversed phase", "Positive", rows("S1_pos", "S2_pos")),
                             U2: ("Reversed phase", "Negative", rows("S1_neg", "S2_neg"))},
                            [("POS/S1_pos.mzML", 3000001, "00000001"), ("POS/S2_pos.mzML", 3000002, "00000002")])

    def spec(self) -> dict:
        return {
            "schema": "msdial-unit-triage-spec.v1", "accession": ACC, "phase": "pre_download",
            "structure": "Only the positive arm is deposited.",
            "groups": [
                {"id": "POS", "applies_to": "rows", "match": {"unit": U1},
                 "file": {"role": "sample", "ion_mode": "positive", "chromatography": "RP", "acquisition_mode": "DDA",
                          "basis": "declared"},
                 "evidence": [{"source": "catalog_unit", "field": "ion_mode", "value": "Positive"}],
                 "disposition": {"keep": U1}, "confidence": "high", "rationale": "The positive files."},
                {"id": "NEG", "applies_to": "rows", "match": {"unit": U2, "resolution": "none"},
                 "disposition": {"exclude": "not_in_archive", "detail": "No negative file is in the archive."},
                 "confidence": "high", "rationale": "The listing holds no negative run."},
            ],
            "units": {
                U1: {"status": "ready", "confidence": "high", "rationale": "The positive arm.",
                     "unit_technical": {"ion_mode": "positive", "chromatography": "RP", "acquisition_mode": "DDA"}},
                U2: {"status": "excluded", "confidence": "high", "rationale": "No negative raw file was deposited.",
                     "exclusion": {"reason": "no_files", "detail": "The archive holds positive runs only."},
                     "unit_technical": {"ion_mode": "negative", "chromatography": "RP", "acquisition_mode": "DDA"}},
            },
        }

    def test_a_unit_with_no_raw_file_is_excluded_not_asked(self) -> None:
        report = self.judged(self.spec())
        self.assertEqual((report.verdict, report.unit_status), ("pass", {U1: "ready", U2: "excluded"}),
                         [f.as_dict() for f in report.findings])

    def test_no_files_means_no_file_of_its_own(self) -> None:
        payloads = self.expanded(self.spec())
        two = copy.deepcopy(payloads[U2])
        two["excluded_rows"][0]["reason"] = "other"
        found = errors(validate(self.files, {**payloads, U2: two}), "T-STATUS", U2)
        self.assertTrue(any("excluded as no_files" in m for m in found), found)


# ---- the sets, the schemas and the plan -------------------------------------------------------------------------

class ContractTests(unittest.TestCase):
    def test_flag_sets(self) -> None:
        self.assertNotIn("declared_technical_disputed", vt.BLOCKING_FLAGS)
        self.assertLessEqual({"declared_technical_disputed", "ms1_only_excluded", "ms1_only_suspected",
                              "segmented_acquisition"}, vt.INFORMATIONAL_FLAGS)
        self.assertFalse(vt.BLOCKING_FLAGS & vt.INFORMATIONAL_FLAGS)
        for schema_id in (vt.SCHEMA_V2_ID,):
            schema = vt.load_schema(schema_id)
            self.assertLessEqual(vt.BLOCKING_FLAGS | vt.INFORMATIONAL_FLAGS, set(schema["$defs"]["flag_code"]["enum"]))
        spec_schema = json.loads((ROOT / "schemas" / "msdial-unit-triage-spec.v1.json").read_text(encoding="utf-8"))
        self.assertLessEqual(vt.BLOCKING_FLAGS | vt.INFORMATIONAL_FLAGS, set(spec_schema["$defs"]["flag_code"]["enum"]))

    def test_unit_exclusion_reasons_agree(self) -> None:
        self.assertEqual(curated.UNIT_EXCLUSION_REASONS, vt.UNIT_EXCLUSION_REASONS)
        schema = vt.load_schema(vt.SCHEMA_V2_ID)
        self.assertEqual(tuple(schema["properties"]["exclusion"]["properties"]["reason"]["enum"]), vt.UNIT_EXCLUSION_REASONS)
        self.assertIn("excluded", schema["properties"]["status"]["enum"])
        spec_schema = json.loads((ROOT / "schemas" / "msdial-unit-triage-spec.v1.json").read_text(encoding="utf-8"))
        unit = spec_schema["$defs"]["unit"]["properties"]
        self.assertEqual(tuple(unit["exclusion"]["properties"]["reason"]["enum"]), vt.UNIT_EXCLUSION_REASONS)
        self.assertIn("excluded", unit["status"]["enum"])

    def test_item_reasons_reach_the_payload(self) -> None:
        schema = vt.load_schema(vt.SCHEMA_V2_ID)
        row_reasons = set(schema["$defs"]["excluded_row"]["properties"]["reason"]["enum"])
        member_reasons = set(schema["$defs"]["excluded_member"]["properties"]["reason"]["enum"])
        self.assertEqual(ex.ROW_REASONS, row_reasons)
        self.assertEqual(ex.MEMBER_REASONS, member_reasons)
        self.assertLessEqual({"ms1_only_no_msms", "shared_content_unassigned"}, row_reasons)
        self.assertLessEqual({"ms1_only_no_msms", "shared_content_unassigned", "no_sample_metadata"}, member_reasons)

    def test_the_plan_knows_the_exclusion(self) -> None:
        self.assertIn(curated.TRIAGE_UNIT_EXCLUDED, plan.TRIAGE_EXCLUSION_REASONS)
        self.assertIn(curated.TRIAGE_UNIT_EXCLUDED, plan.__doc__)


# ---- the digest: MS-level words and method folders --------------------------------------------------------------

class DigestTests(unittest.TestCase):
    def test_ms_level_words(self) -> None:
        cases = {
            "Inj001_S1_CSHposMS_6530B": [("MS-only", "MS")],
            "Inj072_Pool_CSHposMSMS_6530B": [("MS/MS", "MSMS")],
            "Inj029_PoolA_CSHposMSProfile": [("MS-only", "MSProfile")],
            "QC_MS1_01": [("MS-only", "MS1")],
            "Pool_ddMS2_top10": [("MS/MS", "MS2")],
            "Sample_MSDIAL_x": [],
            "Professor_1": [],
            "Media_01": [],
        }
        for name, expected in cases.items():
            with self.subTest(name):
                self.assertEqual(dg.ms_level_words(name), expected)

    def test_the_digest_shows_the_contrast_and_the_method_folders(self) -> None:
        files = inputs({U1: ("Reversed phase", "Positive", rows("S1_posMS", "S2_posMS"))},
                       [("RAW/S1_posMS.d/AcqData/CORE_pos_MSb_1.m/DaMethod", 100, "00000001"),
                        ("RAW/S1_posMS.d/AcqData/MSScan.bin", 3000001, "00000002"),
                        ("RAW/S2_posMS.d/AcqData/CORE_pos_MSb_1.m/DaMethod", 100, "00000003"),
                        ("RAW/S2_posMS.d/AcqData/MSScan.bin", 3000002, "00000004"),
                        ("RAW/Pool_posMSMS.d/AcqData/CORE_pos_MSMSb_1.m/DaMethod", 100, "00000005"),
                        ("RAW/Pool_posMSMS.d/AcqData/MSScan.bin", 3000003, "00000006")])
        text = dg.build_digest_from_files(files).decode("utf-8")
        self.assertIn("## MS-level words and acquisition method folders", text)
        self.assertIn("Raw-like names with an MS-only word (2): MS 2 (e.g. S1_posMS.d, S2_posMS.d).", text)
        self.assertIn("Raw-like names with an MS/MS word (1): MSMS 1 (e.g. Pool_posMSMS.d).", text)
        self.assertIn("Method folder CORE_pos_MSb_1.m: in 2 vendor folder(s) (e.g. S1_posMS.d); words MS.", text)
        self.assertIn("Method folder CORE_pos_MSMSb_1.m: in 1 vendor folder(s) (e.g. Pool_posMSMS.d); words MSMS.", text)
        self.assertEqual(dg.DIGEST_VERSION, "1.1.0")

    def test_waters_and_sciex_product_ion_words(self) -> None:
        # MSe, HDMSe and SONAR are DIA with product-ion spectra; MSn and IDA name MS/MS. None is the MS-only word MS.
        cases = {
            "QC_MSe_01": [("MS/MS", "MSe")],
            "S1_SONAR_MSe": [("MS/MS", "MSe"), ("MS/MS", "SONAR")],
            "S1_HDMSe": [("MS/MS", "MSe")],
            "S1_MSn": [("MS/MS", "MSn")],
            "S1_IDA_pos": [("MS/MS", "IDA")],
            "S1_posMS": [("MS-only", "MS")],
            "Mesa_01": [],
        }
        for name, expected in cases.items():
            with self.subTest(name):
                self.assertEqual(sorted(dg.ms_level_words(name)), sorted(expected))
        # One table: the digest reads validate-triage.py's, which the P1 guard reads too.
        self.assertEqual(dg.ms_level_words.__qualname__, vt.ms_level_words.__qualname__)
        self.assertNotIn("_MS_LEVEL_WORDS", vars(dg))
        self.assertEqual(vt.token_acquisition("QC_MSe_01.raw"), ("MSe",))
        self.assertEqual(vt.TOKEN_MODES["MSe"], "DIA")
        for name in ("NEG/S02_negMSMS.mzML", "CSH_posMSMS_QC01.d", "Pool_MS2_neg.raw", "x_AutoMSMS.d", "S1_MSe.raw"):
            with self.subTest(name):
                self.assertTrue(vt.product_ion_words(name))
        self.assertEqual(vt.product_ion_words("S1_posMS.mzML"), [])
        self.assertIsNotNone(vt.acquisition_token_problem("Pool_posMSMS.mzML", "MS1"))


# ---- review fixes: what an excluded unit may leave out, and which flags it may carry ------------------------------

def ms1_unit_excluded(spec: dict) -> dict:
    """ms1_spec with the positive unit excluded as no_product_ion_files too."""
    spec["units"][U1] = {"status": "excluded", "confidence": "high", "rationale": "No positive MS/MS run is kept.",
                         "exclusion": {"reason": "no_product_ion_files", "detail": "Every positive run is MS1 only."},
                         "unit_technical": {"ion_mode": "positive", "chromatography": "RP", "acquisition_mode": "DDA"},
                         "flags": [{"code": "ms1_only_excluded", "certainty": "confirmed", "detail": "mwTab: MS1 data.",
                                    "groups": ["RPOS", "PROF", "POOL"]}]}
    return spec


class ExcludedUnitReviewTests(PolicyCase):
    def setUp(self) -> None:
        self.files = inputs({U1: ("Reversed phase", "Positive", rows("S01_posMS", "S02_posMS")),
                             U2: ("Reversed phase", "Negative", rows("S01_negMS", "S02_negMS"))}, MS1_LISTING)

    def test_an_msms_named_file_is_not_left_out_as_ms1_only(self) -> None:
        # The study's only MS/MS pool, dropped as MS1-only, would take the whole unit out of the campaign.
        spec = ms1_unit_excluded(ms1_spec())
        spec["groups"][2] = {"id": "POOL", "applies_to": "members", "match": {"paths": ["POS/Pool_posMSMS.mzML"]},
                             "disposition": {"exclude": "ms1_only_no_msms", "detail": "MS1."}, "confidence": "medium",
                             "rationale": "Read as MS1."}
        report = self.judged(spec)
        self.assertNotEqual(report.verdict, "pass")
        found = errors(report, "T-EXCL")
        self.assertTrue(any("Pool_posMSMS" in m and "its name says MSMS" in m for m in found), found)

    def test_a_suspected_ms1_reading_does_not_leave_a_file_out(self) -> None:
        spec = ms1_spec()
        spec["units"][U1]["flags"][0]["certainty"] = "suspected"
        found = errors(self.judged(spec), "T-EXCL", U1)
        self.assertTrue(any("without a confirmed ms1_only_excluded flag" in m for m in found), found)

    def test_an_excluded_unit_cannot_carry_a_blocking_flag(self) -> None:
        for code in ("name_token_disputed", "polarity_switching", "folder_header_disagreement", "mixed_acquisition"):
            with self.subTest(code):
                spec = ms1_spec()
                spec["units"][U2]["flags"].append({"code": code, "certainty": "suspected", "detail": "In doubt."})
                found = errors(self.judged(spec), "T-STATUS", U2)
                self.assertTrue(any(f"carries the blocking flag {code}" in m for m in found), found)

    def test_no_product_ion_files_leaves_its_rows_out_for_good_reasons_only(self) -> None:
        payloads = self.expanded(ms1_spec())
        two = copy.deepcopy(payloads[U2])
        two["excluded_rows"][1]["reason"], two["excluded_rows"][1]["detail"] = "other", "Not needed."
        found = errors(validate(self.files, {**payloads, U2: two}), "T-STATUS", U2)
        self.assertTrue(any("row 1 (S02_negMS.mzML) is left out as other" in m for m in found), found)

    def test_no_product_ion_files_holds_when_an_msms_member_is_dropped(self) -> None:
        spec = ms1_unit_excluded(ms1_spec())
        spec["groups"][2] = {"id": "POOL", "applies_to": "members", "match": {"paths": ["POS/Pool_posMSMS.mzML"]},
                             "disposition": {"exclude": "other", "detail": "Not used."}, "confidence": "medium",
                             "rationale": "Left out."}
        spec["units"][U1]["flags"][0]["groups"] = ["RPOS", "PROF"]
        report = self.judged(spec)
        self.assertEqual(report.verdict, "hold", [f.as_dict() for f in report.findings])
        holds = [f.message for f in report.findings if f.level == "hold" and f.check == "T-STATUS" and f.unit == U1]
        self.assertTrue(any("Pool_posMSMS.mzML (left out as other) is named MSMS" in m for m in holds), holds)
        # The negative unit's own exclusion is untouched: the positive pools carry the other arm's polarity.
        self.assertFalse([f for f in report.findings if f.level != "note" and f.unit == U2])

    def test_waters_mse_samples_are_not_ms1_only(self) -> None:
        files = inputs({U1: ("Reversed phase", "Positive", rows("S1_MSe", "S2_MSe"))},
                       [("S1_MSe.raw", 5000001, "00000001"), ("S2_MSe.raw", 5000002, "00000002"),
                        ("Pool_MSMS.raw", 5000003, "00000003")])
        spec = {
            "schema": "msdial-unit-triage-spec.v1", "accession": ACC, "phase": "pre_download",
            "structure": "MSe sample runs and one MSMS pool.",
            "groups": [
                {"id": "S", "applies_to": "rows", "match": {"unit": U1},
                 "disposition": {"exclude": "ms1_only_no_msms", "detail": "MS against the MSMS pool."},
                 "confidence": "high", "rationale": "Read as MS1 only."},
                {"id": "P", "applies_to": "members", "match": {"paths": ["Pool_MSMS.raw"]},
                 "disposition": {"exclude": "no_sample_metadata", "detail": "No row names it."},
                 "confidence": "high", "rationale": "Samples need metadata."}],
            "units": {U1: {"status": "excluded", "confidence": "high", "rationale": "No MS/MS run.",
                           "exclusion": {"reason": "no_product_ion_files", "detail": "MS1 only."},
                           "unit_technical": {"ion_mode": "positive", "chromatography": "RP", "acquisition_mode": "DDA"},
                           "flags": [{"code": "ms1_only_excluded", "certainty": "confirmed", "detail": "Names.",
                                      "groups": ["S"]}]}},
        }
        self.files = files
        report = self.judged(spec)
        self.assertNotEqual(report.verdict, "pass")
        found = errors(report, "T-EXCL", U1)
        self.assertTrue(any("S1_MSe" in m and "MSe" in m.split("its name says", 1)[-1] for m in found), found)

    def test_no_files_holds_when_the_archive_has_files_under_other_names(self) -> None:
        # The Catalog writes the names without the polarity infix the archive carries.
        self.files = inputs({U1: ("Reversed phase", "Positive", rows("Inj001_S1_CSHMSMS_6530B", "Inj002_S2_CSHMSMS_6530B"))},
                            [("raw/Inj001_S1_CSHposMSMS_6530B.mzML", 3000001, "00000001"),
                             ("raw/Inj002_S2_CSHposMSMS_6530B.mzML", 3000002, "00000002")])
        spec = {"schema": "msdial-unit-triage-spec.v1", "accession": ACC, "phase": "pre_download",
                "structure": "One folder of raw runs.",
                "groups": [
                    {"id": "R", "applies_to": "rows", "match": {"unit": U1, "resolution": "none"},
                     "disposition": {"exclude": "not_in_archive", "detail": "No file of that name."},
                     "confidence": "high", "rationale": "Not listed."},
                    {"id": "M", "applies_to": "members", "match": {"paths": ["raw/Inj001_S1_CSHposMSMS_6530B.mzML",
                                                                            "raw/Inj002_S2_CSHposMSMS_6530B.mzML"]},
                     "disposition": {"exclude": "no_sample_metadata", "detail": "No row names it."},
                     "confidence": "high", "rationale": "No row."}],
                "units": {U1: {"status": "excluded", "confidence": "high", "rationale": "No file.",
                               "exclusion": {"reason": "no_files", "detail": "The rows name no archived file."},
                               "unit_technical": {"ion_mode": "positive", "chromatography": "RP",
                                                  "acquisition_mode": "DDA"}}}}
        report = self.judged(spec)
        self.assertEqual(report.verdict, "hold", [f.as_dict() for f in report.findings])
        holds = [f.message for f in report.findings if f.level == "hold" and f.check == "T-STATUS"]
        self.assertTrue(any("excluded as no_files" in m and "Inj001_S1_CSHposMSMS_6530B" in m for m in holds), holds)

    def test_the_plan_counts_members_left_out_by_reason(self) -> None:
        payloads = self.expanded(ms1_spec())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "triage"
            accession_dir = root / ACC
            accession_dir.mkdir(parents=True)
            for unit, document in payloads.items():
                (accession_dir / f"{unit}.json").write_text(json.dumps(document, indent=1), encoding="utf-8")
            entries, reason, _ = curated.unit_entries(accession_dir=accession_dir, triage_root=root, unit_id=U1,
                                                      validation={"verdict": "pass"}, catalog=None)
        self.assertIsNone(reason)
        chosen = entries[0]["curated_selection"]
        self.assertEqual(chosen["members_excluded"], 3)
        self.assertEqual(chosen["members_excluded_by_reason"], {"ms1_only_no_msms": 1, "no_sample_metadata": 2})


class SharedContentReviewTests(PolicyCase):
    def test_copies_inside_one_arm_are_not_shared_content_unassigned(self) -> None:
        # The unit's twins are its own files: a D2 copy, not another arm's.
        self.files = inputs({U1: ("HILIC", "Negative", rows("S1_Hilic_Neg", "S2_Hilic_Neg")),
                             U2: ("Reversed phase", "Negative", rows("S1_Lipid_Neg", "S2_Lipid_Neg"))},
                            [("HILIC_NEG/S1_Hilic_Neg.mzML", 4000001, "0000AA01"),
                             ("HILIC_NEG/S2_Hilic_Neg.mzML", 4000001, "0000AA01"),
                             ("LIPID_NEG/S1_Lipid_Neg.mzML", 4000003, "0000AA03"),
                             ("LIPID_NEG/S2_Lipid_Neg.mzML", 4000004, "0000AA04")])
        self.assertIn("X-DUP-NOKEPT", {f.code for f in expand(self.one_arm_spec(), self.files).findings})

    def test_copies_in_a_backup_folder_are_not_shared_content_unassigned(self) -> None:
        self.files = inputs({U1: ("HILIC", "Negative", rows("S1_Hilic_Neg", "S2_Hilic_Neg")),
                             U2: ("Reversed phase", "Negative", rows("S1_Lipid_Neg", "S2_Lipid_Neg"))},
                            [("HILIC_NEG/S1_Hilic_Neg.mzML", 4000001, "0000AA01"),
                             ("HILIC_NEG/S2_Hilic_Neg.mzML", 4000002, "0000AA02"),
                             ("backup/S1_Hilic_Neg_copy.mzML", 4000001, "0000AA01"),
                             ("backup/S2_Hilic_Neg_copy.mzML", 4000002, "0000AA02"),
                             ("LIPID_NEG/S1_Lipid_Neg.mzML", 4000003, "0000AA03"),
                             ("LIPID_NEG/S2_Lipid_Neg.mzML", 4000004, "0000AA04")])
        spec = self.one_arm_spec()
        spec["groups"].append({"id": "BK", "applies_to": "members",
                               "match": {"paths": ["backup/S1_Hilic_Neg_copy.mzML", "backup/S2_Hilic_Neg_copy.mzML"]},
                               "disposition": {"exclude": "duplicate_content", "detail": "Backup copies."},
                               "confidence": "high", "rationale": "A backup folder."})
        self.assertIn("X-DUP-NOKEPT", {f.code for f in expand(spec, self.files).findings})
        # The validator refuses it too, on payloads that bypass the expander (the flag written by hand).
        spec["units"][U1]["flags"] = []
        payloads = self.expanded(spec)
        payloads[U1]["flags"] = [{"code": "shared_content", "certainty": "confirmed", "row_indices": [0, 1],
                                  "detail": "Same CRC32 and size as the backup folder."}]
        found = errors(validate(self.files, payloads), unit=U1)
        self.assertTrue(any("none of its byte-identical twins is in another arm's place" in m for m in found), found)

    @staticmethod
    def one_arm_spec() -> dict:
        spec = shared_spec()
        spec["groups"][1] = {"id": "LKEEP", "applies_to": "rows", "match": {"unit": U2},
                             "file": {"role": "sample", "ion_mode": "negative", "chromatography": "RP",
                                      "acquisition_mode": "DDA", "basis": "declared"},
                             "evidence": [{"source": "catalog_unit", "field": "ion_mode", "value": "Negative"}],
                             "disposition": {"keep": U2}, "confidence": "high", "rationale": "The lipid arm."}
        del spec["groups"][2]
        spec["units"][U2] = {"status": "ready", "confidence": "high", "rationale": "The lipid arm.",
                             "unit_technical": {"ion_mode": "negative", "chromatography": "RP", "acquisition_mode": "DDA"}}
        return spec


class LegacyPayloadTests(PolicyCase):
    def setUp(self) -> None:
        self.files = inputs({U1: ("Reversed phase", "Positive", rows("S01_posMS", "S02_posMS")),
                             U2: ("Reversed phase", "Negative", rows("S01_negMS", "S02_negMS"))}, MS1_LISTING)

    def test_declared_technical_disputed_blocks_a_payload_of_an_earlier_skill(self) -> None:
        payloads = self.expanded(ms1_spec())
        self.assertEqual(validate(self.files, payloads).verdict, "pass")
        one = copy.deepcopy(payloads[U1])
        one["decided_by"]["skill_version"] = "1.2.0"
        self.assertTrue(vt.legacy_policy(one))
        self.assertFalse(vt.legacy_policy(payloads[U1]))
        self.assertIn("declared_technical_disputed", vt.blocking_flags_for(one))
        found = errors(validate(self.files, {**payloads, U1: one}), "T-STATUS", U1)
        self.assertTrue(any("declared_technical_disputed" in m for m in found), found)


if __name__ == "__main__":
    unittest.main()
