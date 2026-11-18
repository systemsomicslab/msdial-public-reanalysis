"""scripts/validate-triage.py and the msdial-unit-triage eval set.

The stage3b campaign (2026-11-15) ran every unit of a multi-unit accession on the whole study's files. The
fix is an agent triage before download (the msdial-unit-triage skill), checked by a deterministic validator
before anything is planned. These tests run the validator on the triage a correct agent would write for the
eight accessions of the prototype (tests/fixtures/triage-eval, ground truth from the checker agents), and on
known-bad triage files built from the failure modes the prototype and stage3b showed.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(TESTS))
import decision_records as records  # noqa: E402
import triage_eval as te  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("validate_triage", ROOT / "scripts" / "validate-triage.py")
assert _SPEC and _SPEC.loader
vt = importlib.util.module_from_spec(_SPEC)
sys.modules["validate_triage"] = vt
_SPEC.loader.exec_module(vt)

# What a correct pre-download triage should reach, per accession and unit.
EXPECTED = {
    "ST002798": ("hold", {"AN004552": "needs_person", "AN004553": "needs_person"}),
    "ST002796": ("hold", {"AN004548": "needs_person", "AN004549": "needs_person"}),
    "ST004606": ("pass", {"AN007766": "ready", "AN007767": "ready", "AN007768": "ready", "AN007769": "ready"}),
    "ST003964": ("pass", {"AN006515": "ready", "AN006516": "ready", "AN006517": "ready", "AN006518": "ready"}),
    "ST002283": ("hold", {"AN003728": "ready", "AN003729": "needs_person", "AN003730": "ready", "AN003731": "needs_person"}),
    "ST002747": ("pass", {"AN004454": "ready", "AN004455": "ready", "AN004456": "ready", "AN004457": "ready"}),
    "ST004544": ("hold", {"AN007633": "abstained", "AN007634": "abstained"}),
    "ST003128": ("hold", {"AN005128": "needs_person", "AN005129": "needs_person", "AN005130": "ready"}),
}


class TriageCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._n = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_validator(self, accession: str, docs: dict, *, listing: bool = True, catalog: bool = True):
        self._n += 1
        directory = te.write(docs, self.tmp / f"{accession}-{self._n}")
        _, _, listing_path = te.load(accession)
        report = vt.validate(
            directory,
            vt.read_catalog(te.fixture_dir(accession) / "catalog.json") if catalog else None,
            vt.read_listing(listing_path) if listing else [],
        )
        return report, directory

    def checks(self, report, level: str = "error", unit: str | None = "*") -> set[str]:
        return {f.check for f in report.findings if f.level == level and (unit == "*" or f.unit == unit)}


class EvalSetTest(TriageCase):
    def test_fixtures_are_complete_and_public_only(self) -> None:
        for accession in te.ACCESSIONS:
            catalog, truth, listing = te.load(accession)
            self.assertEqual(catalog["accession"], accession)
            self.assertTrue(listing.exists())
            self.assertEqual({u["unit_id"] for u in catalog["units"]}, set(EXPECTED[accession][1]))
            for unit in catalog["units"]:
                self.assertRegex(unit["unit_id"], r"^AN\d{6}$")
            text = "".join(p.read_text(encoding="utf-8") for p in te.fixture_dir(accession).iterdir())
            for private in ("F:\\", "D:\\", "C:\\", "Users", "VS20", "VS21", ".msp", ".lbm2"):
                self.assertNotIn(private, text, f"{accession} fixture mentions {private!r}")

    def test_ground_truth_counts_match_the_checker(self) -> None:
        checker = {  # 'correct' per unit as the checker agents reported it
            "AN004552": 118, "AN004553": 102, "AN004548": 138, "AN004549": 136,
            "AN007766": 56, "AN007767": 52, "AN007768": 56, "AN007769": 52,
            "AN006515": 12, "AN006516": 12, "AN006517": 12, "AN006518": 12,
            "AN003728": 44, "AN003729": 0, "AN003730": 44, "AN003731": 44,
            "AN004454": 13, "AN004455": 13, "AN004456": 13, "AN004457": 13,
            "AN007633": 33, "AN007634": 33, "AN005128": 24, "AN005129": 24, "AN005130": 24,
        }
        seen = {}
        for accession in te.ACCESSIONS:
            _, truth, _ = te.load(accession)
            members = [m["path"] for u in truth["units"] for m in u["members"]]
            self.assertEqual(len(members), len(set(members)), f"{accession}: a member is in two units")
            for unit in truth["units"]:
                seen[unit["unit_id"]] = len(unit["members"])
        self.assertEqual(seen, checker)

    def test_known_good_triage_reaches_the_expected_verdict(self) -> None:
        for accession in te.ACCESSIONS:
            with self.subTest(accession=accession):
                report, _ = self.run_validator(accession, te.build_good(accession))
                verdict, statuses = EXPECTED[accession]
                self.assertEqual(self.checks(report), set(), [f.as_dict() for f in report.findings if f.level == "error"])
                self.assertEqual(report.verdict, verdict)
                self.assertEqual(report.unit_status, statuses)
                for unit, status in statuses.items():
                    self.assertEqual(report.unit_verdict(unit), "pass" if status == "ready" else "hold")

    def test_known_good_triage_scores_like_the_ground_truth(self) -> None:
        for accession in te.ACCESSIONS:
            with self.subTest(accession=accession):
                _, truth, _ = te.load(accession)
                _, directory = self.run_validator(accession, te.build_good(accession))
                scores = vt.score(directory, truth)
                for unit in truth["units"]:
                    s = scores[unit["unit_id"]]
                    self.assertEqual(s["wrong_included"], 0)
                    if unit["expected_status_pre_download"] == "abstained":
                        # Abstaining is right before download; the files stay to be assigned after preflight.
                        self.assertEqual((s["correct"], s["wrongly_left_out"]), (0, len(unit["members"])))
                    else:
                        self.assertEqual((s["correct"], s["wrongly_left_out"]), (len(unit["members"]), 0))

    def test_a_campaign_payload_named_by_its_catalog_unit_id_is_scored_by_its_analysis_id(self) -> None:
        # A campaign names a payload <catalog unit id>.json and carries the public analysis id as
        # source_subrecord_id; the ground truth names the analysis id (pilot of 2026-11-18).
        for accession in ("ST002798", "ST002283"):
            with self.subTest(accession=accession):
                _, truth, _ = te.load(accession)
                _, directory = self.run_validator(accession, te.build_good(accession))
                expected = vt.score(directory, truth)
                for n, path in enumerate(sorted(directory.glob("AN*.json"))):
                    data = json.loads(path.read_text(encoding="utf-8"))
                    data["source_subrecord_id"] = path.stem
                    (directory / f"{n:020x}.json").write_text(json.dumps(data), encoding="utf-8")
                    path.unlink()
                self.assertEqual(vt.score(directory, truth), expected)

    def test_stage3b_style_whole_study_per_unit_is_refused_for_every_accession(self) -> None:
        for accession in te.ACCESSIONS:
            with self.subTest(accession=accession):
                report, _ = self.run_validator(accession, te.stage3b_style(accession))
                self.assertEqual(report.verdict, "error")
                self.assertTrue(any(f.check == "T-UNIQ" and "identical set" in f.message for f in report.findings))
                self.assertTrue(all(report.unit_verdict(u) == "error" for u in report.units))

    def test_schema_is_valid_draft_2020_12_and_agrees_with_the_built_in_checker(self) -> None:
        try:
            import jsonschema  # type: ignore
        except ImportError:  # pragma: no cover - the built-in checker is what the validator uses
            self.skipTest("jsonschema is not installed")
        schema = vt.load_schema()
        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator(schema)
        good = te.build_good("ST002283")["AN003731"]
        self.assertEqual(list(validator.iter_errors(good)), [])
        self.assertEqual(vt.schema_errors(good, schema, schema), [])
        bad = copy.deepcopy(good)
        del bad["excluded_rows"][0]["reason"]
        bad["standing"] = "accepted_decision"
        bad["rows"][0]["basis"] = "guessed"
        self.assertTrue(list(validator.iter_errors(bad)))
        self.assertEqual(len(vt.schema_errors(bad, schema, schema)), 3)


def keep_hilic_neg_by_folder(docs: dict) -> dict:
    """ST002283 as the prototype got it wrong: AN003729 keeps the Human_Hilic_Neg folder as HILIC negative."""
    hilic = docs["AN003729"]
    kept_rows, left = [], []
    for e in hilic["excluded_rows"]:
        if e["reason"] == "duplicate_content":
            kept_rows.append({"row_index": e["row_index"], "sample_id": e["sample_id"], "raw_file": e["raw_file"],
                              "member_path": e["member_path"], "archive": "ST002283_AB_Human_Bone.zip", "role": "sample",
                              "ion_mode": "negative", "chromatography": "HILIC", "acquisition_mode": "DDA", "basis": "inferred",
                              "evidence": [{"source": "folder_token", "field": "folder", "value": "Human_Hilic_Neg"}]})
        else:
            left.append(e)
    hilic["rows"], hilic["excluded_rows"] = kept_rows, left
    hilic["added_members"] = [{"member_path": f"AB_Human_Bone/Human_Hilic_Neg/{name}_Hilic_Neg.mzML", "archive": "ST002283_AB_Human_Bone.zip",
                               "role": "qc", "ion_mode": "negative", "chromatography": "HILIC", "acquisition_mode": "DDA", "basis": "inferred",
                               "evidence": [{"source": "folder_token", "field": "folder", "value": "Human_Hilic_Neg"}]} for name in ("QC", "QC2", "QC3")]
    hilic["status"], hilic["needs_person"], hilic["flags"] = "ready", [], []
    return docs


def move_row(docs: dict, raw_file: str, src: str, dst: str, **changes) -> dict:
    """Move the kept row naming raw_file from unit src to unit dst (dst's row of the same name), as dst's row."""
    row = next(r for r in docs[src]["rows"] if r["raw_file"] == raw_file)
    docs[src]["rows"].remove(row)
    target = next(e for e in docs[dst]["excluded_rows"] if e["raw_file"] == raw_file)
    docs[dst]["excluded_rows"].remove(target)
    docs[src]["excluded_rows"].append({"row_index": row["row_index"], "sample_id": row["sample_id"], "raw_file": raw_file,
                                       "member_path": row["member_path"], "reason": "belongs_to_other_unit", "belongs_to_unit": dst,
                                       "detail": "moved"})
    docs[dst]["rows"].append(dict(copy.deepcopy(row), row_index=target["row_index"], **changes))
    return docs


class PrototypeFailureModeTest(TriageCase):
    def test_st002283_duplicate_content_settled_by_folder_name_is_an_error(self) -> None:
        """The prototype kept Human_Hilic_Neg for the HILIC-negative unit at medium confidence."""
        docs = keep_hilic_neg_by_folder(te.build_good("ST002283"))
        hilic = docs["AN003729"]
        report, directory = self.run_validator("ST002283", docs)
        self.assertIn("T-DUP", self.checks(report, unit="AN003729"))
        self.assertEqual(report.unit_verdict("AN003729"), "error")
        _, truth, _ = te.load("ST002283")
        self.assertEqual(vt.score(directory, truth)["AN003729"], {"correct": 0, "wrong_included": 43, "wrongly_left_out": 0,
                                                                     "held_for_person": 0})

        # Flagged and sent to a person, the same selection is a hold, not an error.
        hilic["status"] = "needs_person"
        hilic["needs_person"] = [{"code": "shared_content", "question": "Which unit, if any, do the shared negative files belong to?"}]
        hilic["flags"] = [{"code": "shared_content", "certainty": "confirmed", "detail": "Same CRC32 and size as Human_Lipid_Neg."}]
        report, _ = self.run_validator("ST002283", docs)
        self.assertEqual(self.checks(report), set())
        self.assertIn("T-DUP", self.checks(report, "hold", "AN003729"))

    def test_copies_held_for_a_person_are_scored_as_held_not_left_out(self) -> None:
        """Skill 1.2.0 holds a unit's shared copies for a person (needs_person) instead of keeping them; the 2026-11-18
        re-pilot of ST002283 did so for AN003731, which the score had counted as 43 files wrongly left out."""
        docs = te.build_good("ST002283")
        lipid = docs["AN003731"]
        held = [r for r in lipid["rows"] if "Human_Lipid_Neg/" in r["member_path"] and "Blank" not in r["member_path"]]
        self.assertTrue(held)
        lipid["rows"] = [r for r in lipid["rows"] if r not in held]
        lipid["excluded_rows"] = [*lipid.get("excluded_rows", []), *[
            {"row_index": r["row_index"], "sample_id": r["sample_id"], "raw_file": r["raw_file"],
             "member_path": r["member_path"], "reason": "needs_person", "detail": "Held for the shared_content question."}
            for r in held]]
        _, directory = self.run_validator("ST002283", docs)
        _, truth, _ = te.load("ST002283")
        counts = vt.score(directory, truth)["AN003731"]
        self.assertEqual(counts["held_for_person"], len(held))
        self.assertEqual(counts["wrongly_left_out"], 0)
        self.assertEqual(counts["correct"] + counts["held_for_person"],
                         len(next(u for u in truth["units"] if u["unit_id"] == "AN003731")["members"]))

    def test_st004544_assignment_on_file_size_alone_is_an_error(self) -> None:
        """Names carry no signal; the size pattern is not evidence to assign on."""
        docs = te.stage3b_style("ST004544")
        _, truth, _ = te.load("ST004544")
        for unit in truth["units"]:
            mine = {m["path"] for m in unit["members"]}
            d = docs[unit["unit_id"]]
            rows = [r for r in d["rows"] if r["member_path"] in mine]
            d["excluded_rows"] = [{"row_index": r["row_index"], "sample_id": r["sample_id"], "raw_file": r["raw_file"], "member_path": r["member_path"],
                                   "reason": "belongs_to_other_unit", "belongs_to_unit": next(u["unit_id"] for u in truth["units"] if u["unit_id"] != unit["unit_id"]),
                                   "detail": "Other suffix parity."} for r in d["rows"] if r["member_path"] not in mine]
            for r in rows:
                r["acquisition_mode"] = "unknown"
                r["basis"] = "inferred"
                r["evidence"] = [{"source": "archive_listing", "field": "size", "value": 35000000}]
            d["rows"] = rows
            d["unit_technical"]["acquisition_mode"] = "DDA"
        report, _ = self.run_validator("ST004544", docs)
        self.assertIn("T-EVID", self.checks(report, unit="AN007633"))
        self.assertIn("T-EVID", self.checks(report, unit="AN007634"))

    def test_ms1_only_and_mixed_acquisition_must_be_flagged(self) -> None:
        docs = te.build_good("ST002798")
        docs["AN004552"]["flags"] = [f for f in docs["AN004552"]["flags"] if f["code"] not in ("ms1_only", "mixed_acquisition")]
        report, _ = self.run_validator("ST002798", docs)
        messages = [f.message for f in report.findings if f.check == "T-ACQ" and f.unit == "AN004552"]
        self.assertTrue(any("MS1-only" in m for m in messages))
        # Children of one acquisition mode each split the unit's acquisition (P2, 2026-11-18): not a mix.
        self.assertFalse(any("mix acquisition modes" in m for m in messages))
        # Without the children, the kept files mix modes in one run.
        docs["AN004552"].pop("children")
        report, _ = self.run_validator("ST002798", docs)
        messages = [f.message for f in report.findings if f.check == "T-ACQ" and f.unit == "AN004552"]
        self.assertTrue(any("mix acquisition modes" in m for m in messages))

    def test_targeted_dda_must_be_flagged(self) -> None:
        docs = te.build_good("ST002796")
        docs["AN004549"]["flags"] = [f for f in docs["AN004549"]["flags"] if f["code"] != "targeted_dda"]
        report, _ = self.run_validator("ST002796", docs)
        self.assertIn("T-ACQ", self.checks(report, unit="AN004549"))

    def test_polarity_switching_unit_cannot_be_ready(self) -> None:
        docs = te.build_good("ST003128")
        d = docs["AN005128"]
        d["status"], d["needs_person"] = "ready", []
        d["flags"] = [f for f in d["flags"] if f["code"] != "ms1_only"]
        d["unit_technical"]["ion_mode"] = "switching"
        report, _ = self.run_validator("ST003128", docs)
        self.assertIn("T-POL", self.checks(report, unit="AN005128"))
        self.assertIn("T-STATUS", self.checks(report, unit="AN005128"))  # the polarity_switching flag still blocks it

    def test_folder_header_disagreement_goes_to_a_person(self) -> None:
        """ST002796: HILICpos QCDIA labelled AIF by the header; the folder (variable-window DIA) was right."""
        docs = te.build_good("ST002796")
        d = docs["AN004549"]
        d["phase"] = "post_preflight"
        for r in d["rows"]:
            if "HILICpos/MS2/DIA/QCDIA" in (r["member_path"] or ""):
                r["evidence"].append({"source": "raw_header", "field": "acquisition_mode", "value": "AIF"})
        for m in d["added_members"]:
            if "HILICpos/MS2/DIA/QCDIA" in m["member_path"]:
                m["evidence"].append({"source": "raw_header", "field": "acquisition_mode", "value": "AIF"})
        report, _ = self.run_validator("ST002796", docs)
        self.assertIn("T-HEADER", self.checks(report, unit="AN004549"))
        d["flags"].append({"code": "folder_header_disagreement", "certainty": "confirmed", "detail": "Header heuristic says AIF; 10 variable windows."})
        d["needs_person"].append({"code": "folder_header_disagreement", "question": "Folder says DIA, header says AIF: which?"})
        report, _ = self.run_validator("ST002796", docs)
        self.assertEqual(self.checks(report), set())

    def test_raw_header_polarity_against_the_row_needs_the_flag(self) -> None:
        docs = te.build_good("ST004606")
        docs["AN007768"]["rows"][0]["evidence"].append({"source": "raw_header", "field": "polarity", "value": "Positive"})
        report, _ = self.run_validator("ST004606", docs)
        self.assertIn("T-HEADER", self.checks(report, unit="AN007768"))


class ContractTest(TriageCase):
    def test_a_file_in_two_units_needs_shared_with_on_both(self) -> None:
        docs = te.build_good("ST003128")
        stolen = copy.deepcopy(docs["AN005130"]["rows"][0])
        target = docs["AN005129"]
        target["excluded_rows"] = [e for e in target["excluded_rows"] if e["row_index"] != stolen["row_index"]]
        stolen["ion_mode"], stolen["acquisition_mode"] = "negative", "MS1"
        target["rows"].append(stolen)
        report, _ = self.run_validator("ST003128", docs)
        self.assertIn("T-UNIQ", self.checks(report))
        share = {"unit_id": "AN005130", "member_paths": [stolen["member_path"]], "reason": "test"}
        target["shared_with"] = [share]
        report, _ = self.run_validator("ST003128", docs)
        self.assertIn("T-SHARED", self.checks(report, unit="AN005129"))
        self.assertIn("T-UNIQ", self.checks(report))
        docs["AN005130"]["shared_with"] = [{"unit_id": "AN005129", "member_paths": [stolen["member_path"]], "reason": "test"}]
        report, _ = self.run_validator("ST003128", docs)
        self.assertNotIn("T-UNIQ", self.checks(report))
        self.assertNotIn("T-SHARED", self.checks(report))

    def test_every_catalog_row_is_kept_or_excluded_once(self) -> None:
        docs = te.build_good("ST004606")
        docs["AN007766"]["excluded_rows"].pop()
        report, _ = self.run_validator("ST004606", docs)
        self.assertIn("T-COVER", self.checks(report, unit="AN007766"))
        docs = te.build_good("ST004606")
        row = docs["AN007767"]["rows"][0]
        docs["AN007767"]["excluded_rows"].append({"row_index": row["row_index"], "sample_id": row["sample_id"], "raw_file": row["raw_file"],
                                                  "reason": "other", "belongs_to_unit": None, "detail": "twice"})
        report, _ = self.run_validator("ST004606", docs)
        self.assertIn("T-COVER", self.checks(report, unit="AN007767"))

    def test_rows_must_match_the_catalog(self) -> None:
        docs = te.build_good("ST003964")
        docs["AN006515"]["rows"][0]["sample_id"] = "renamed"
        report, _ = self.run_validator("ST003964", docs)
        self.assertIn("T-COVER", self.checks(report, unit="AN006515"))
        docs = te.build_good("ST003964")
        docs["AN006516"]["catalog_row_count"] += 1
        docs["AN006516"]["excluded_rows"].append({"row_index": 48, "sample_id": "x", "raw_file": "x.raw", "reason": "other",
                                                  "belongs_to_unit": None, "detail": "extra"})
        report, _ = self.run_validator("ST003964", docs)
        self.assertIn("T-COVER", self.checks(report, unit="AN006516"))

    def test_excluded_row_needs_a_reason(self) -> None:
        docs = te.build_good("ST004606")
        del docs["AN007769"]["excluded_rows"][0]["reason"]
        report, _ = self.run_validator("ST004606", docs)
        self.assertIn("T-SCHEMA", self.checks(report, unit="AN007769"))
        docs = te.build_good("ST004606")
        docs["AN007769"]["excluded_rows"][0]["belongs_to_unit"] = None
        report, _ = self.run_validator("ST004606", docs)
        self.assertIn("T-EXCL", self.checks(report, unit="AN007769"))

    def test_polarity_must_match_the_unit_and_the_name(self) -> None:
        docs = te.build_good("ST004606")
        moved = docs["AN007769"]["rows"].pop(0)  # an RP NEG file, its name carries _NEG_
        docs["AN007769"]["excluded_rows"].append({"row_index": moved["row_index"], "sample_id": moved["sample_id"], "raw_file": moved["raw_file"],
                                                  "member_path": moved["member_path"], "reason": "belongs_to_other_unit",
                                                  "belongs_to_unit": "AN007767", "detail": "moved"})
        target = docs["AN007767"]
        target["excluded_rows"] = [e for e in target["excluded_rows"] if e["row_index"] != moved["row_index"]]
        target["rows"].append(dict(moved))
        report, _ = self.run_validator("ST004606", docs)
        self.assertIn("T-POL", self.checks(report, unit="AN007767"))
        target["rows"][-1]["ion_mode"] = "positive"
        report, _ = self.run_validator("ST004606", docs)
        self.assertNotIn("T-POL", self.checks(report, unit="AN007767"))
        self.assertIn("T-TOKEN", self.checks(report, unit="AN007767"))

    def test_chromatography_must_match_the_unit_and_the_catalog(self) -> None:
        docs = te.build_good("ST002747")
        hilic = docs["AN004454"]["rows"][0]
        hilic_copy = dict(hilic, chromatography="HILIC")
        docs["AN004456"]["rows"].append(dict(hilic_copy, row_index=hilic["row_index"]))
        docs["AN004456"]["excluded_rows"] = [e for e in docs["AN004456"]["excluded_rows"] if e["row_index"] != hilic["row_index"]]
        report, _ = self.run_validator("ST002747", docs)
        self.assertIn("T-CHROM", self.checks(report, unit="AN004456"))
        docs = te.build_good("ST002747")
        docs["AN004457"]["unit_technical"]["chromatography"] = "HILIC"
        report, _ = self.run_validator("ST002747", docs)
        self.assertIn("T-DECL", self.checks(report, unit="AN004457"))
        docs["AN004457"]["flags"].append({"code": "declared_technical_disputed", "certainty": "suspected", "detail": "test"})
        report, _ = self.run_validator("ST002747", docs)
        self.assertNotIn("T-DECL", self.checks(report, unit="AN004457"))

    def test_status_rules(self) -> None:
        docs = te.build_good("ST002798")
        docs["AN004552"]["status"] = "ready"
        report, _ = self.run_validator("ST002798", docs)
        self.assertIn("T-STATUS", self.checks(report, unit="AN004552"))
        docs = te.build_good("ST004606")
        docs["AN007766"]["status"] = "needs_person"
        report, _ = self.run_validator("ST004606", docs)
        self.assertIn("T-STATUS", self.checks(report, unit="AN007766"))
        docs = te.build_good("ST004544")
        good = te.build_good("ST004606")["AN007766"]["rows"][0]
        docs["AN007633"]["added_members"] = [{k: good[k] for k in ("archive", "role", "ion_mode", "chromatography", "acquisition_mode", "basis", "evidence")}
                                             | {"member_path": "QC1_2_1.mzML"}]
        report, _ = self.run_validator("ST004544", docs)
        self.assertIn("T-STATUS", self.checks(report, unit="AN007633"))

    def test_a_person_answer_lets_a_flagged_unit_be_ready(self) -> None:
        docs = te.build_good("ST002798")
        for d in docs.values():
            d["status"], d["needs_person"] = "ready", []
            d["person_answers"] = [{"by": "Reviewer A", "answered_at": "2026-11-16T09:00:00Z", "code": code,
                                    "answer": "Keep MS1-only dilution files; run one child per polarity and acquisition."}
                                   for code in ("ms1_only", "mixed_acquisition")]
        report, _ = self.run_validator("ST002798", docs)
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])
        for d in docs.values():
            del d["children"]
        report, _ = self.run_validator("ST002798", docs)
        self.assertIn("T-POL", self.checks(report))  # both polarities need children to be ready

    def test_children_partition_the_kept_files(self) -> None:
        docs = te.build_good("ST002796")
        child = docs["AN004548"]["children"][0]
        dropped = child["member_paths"].pop()
        report, _ = self.run_validator("ST002796", docs)
        self.assertIn("T-CHILD", self.checks(report, unit="AN004548"))
        child["member_paths"].append(dropped)
        other = docs["AN004548"]["children"][-1]
        other["member_paths"].append(child["member_paths"][0])
        report, _ = self.run_validator("ST002796", docs)
        self.assertIn("T-CHILD", self.checks(report, unit="AN004548"))

    def test_unaccounted_raw_members_hold(self) -> None:
        docs = te.build_good("ST003964")
        for d in docs.values():
            d["excluded_members"] = []
        report, _ = self.run_validator("ST003964", docs)
        self.assertEqual(report.verdict, "hold")
        self.assertIn("T-MEMBER", self.checks(report, "hold", None))

    def test_member_must_be_in_the_listing(self) -> None:
        docs = te.build_good("ST003964")
        docs["AN006517"]["rows"][0]["member_path"] = "160_0003_raw/not-there.raw"
        report, _ = self.run_validator("ST003964", docs)
        self.assertIn("T-MEMBER", self.checks(report, unit="AN006517"))

    def test_handoff_sha256_is_checked_against_a_handoffs_directory(self) -> None:
        docs = te.build_good("ST003128")
        catalog, _, _ = te.load("ST003128")
        handoffs = self.tmp / "handoffs"
        for unit in catalog["units"]:
            d = handoffs / unit["unit_id"]
            d.mkdir(parents=True)
            raw = json.dumps({"analysis_unit_id": unit["unit_id"], "technical_settings": unit["declared"]}).encode("utf-8")
            (d / "handoff.json").write_bytes(raw)
            (d / "samples.json").write_text(json.dumps(unit["rows"]), encoding="utf-8")
            docs[unit["unit_id"]]["catalog_handoff_sha256"] = hashlib.sha256(raw).hexdigest()
        docs["AN005130"]["catalog_handoff_sha256"] = "0" * 64
        directory = te.write(docs, self.tmp / "sha")
        report = vt.validate(directory, vt.read_catalog(handoffs), [])
        self.assertEqual(self.checks(report, unit="AN005130"), {"T-HANDOFF"})
        self.assertNotIn("T-HANDOFF", self.checks(report, unit="AN005129"))

    def test_one_directory_one_accession_and_file_named_by_unit(self) -> None:
        docs = te.build_good("ST004606")
        docs["AN007766"]["accession"] = "ST000001"
        report, _ = self.run_validator("ST004606", docs, catalog=False, listing=False)
        self.assertIn("T-SET", self.checks(report))
        directory = te.write(te.build_good("ST004606"), self.tmp / "named")
        (directory / "AN007766.json").rename(directory / "wrong-name.json")
        report = vt.validate(directory)
        self.assertIn("T-SET", self.checks(report, unit="AN007766"))

    def test_decision_records_are_not_triages_and_a_revision_supersedes_its_unit(self) -> None:
        """The campaign layout (scripts/campaign/curated.py) keeps <unit>.record.json, the decision record, beside the
        payload, and a revision as <unit>.r<N>.json: neither makes the set an error, and the highest revision is the one
        judged (review of PR #60, 2026-11-16)."""
        directory = te.write(te.build_good("ST002747"), self.tmp / "layout")
        catalog = vt.read_catalog(te.fixture_dir("ST002747") / "catalog.json")
        listing = vt.read_listing(te.load("ST002747")[2])
        code = records.Code.from_root(self.tmp / "code")
        bound = records.copy_listing(te.load("ST002747")[2], directory)
        records.write_record(directory / "AN004454.json", code, inputs=[bound])
        revised = json.loads((directory / "AN004455.json").read_text(encoding="utf-8"))
        revised["notes"] = "revision 2: the same files, a reworded note"
        (directory / "AN004455.r2.json").write_text(json.dumps(revised), encoding="utf-8")
        records.write_record(directory / "AN004455.r2.json", code, inputs=[bound])
        report = vt.validate(directory, catalog, listing, code_root=code.root)
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])
        # A record that is not an envelope is not passed over: it is an error, never silently ignored.
        (directory / "AN004454.record.json").write_text(json.dumps({"schema": "msdial-llm-decision-record.v1"}), encoding="utf-8")
        self.assertIn("T-RECORD", self.checks(vt.validate(directory, catalog, listing, code_root=code.root), unit="AN004454"))
        records.write_record(directory / "AN004454.json", code, inputs=[bound])
        self.assertEqual(sorted(report.units), ["AN004454", "AN004455", "AN004456", "AN004457"])
        self.assertEqual([path.name for path in vt.current_triage_files(directory) if path.name.startswith("AN004455")],
                         ["AN004455.r2.json"])
        # The revision is what is judged: a broken r3 errors although the base and r2 are good.
        broken = dict(revised, rows=revised["rows"][:-1])
        (directory / "AN004455.r3.json").write_text(json.dumps(broken), encoding="utf-8")
        report = vt.validate(directory, catalog, listing)
        self.assertIn("T-COVER", self.checks(report, unit="AN004455"))
        # A revision that holds another unit is still misnamed.
        (directory / "AN004455.r3.json").unlink()
        (directory / "AN004457.r2.json").write_text((directory / "AN004456.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.assertIn("T-SET", self.checks(vt.validate(directory, catalog, listing)))

    def test_command_line_exit_codes(self) -> None:
        def run(accession: str, docs: dict, *extra: str) -> tuple[int, str]:
            directory = te.write(docs, self.tmp / f"cli-{accession}-{len(extra)}-{id(docs)}")
            d = te.fixture_dir(accession)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = vt.main([str(directory), "--catalog", str(d / "catalog.json"), "--archive-listing", str(d / "archive-members.tsv"), *extra])
            return code, out.getvalue()

        code, text = run("ST004606", te.build_good("ST004606"))
        self.assertEqual(code, 0)
        self.assertIn("PASS", text)
        code, text = run("ST002283", te.build_good("ST002283"), "--ground-truth", str(te.fixture_dir("ST002283") / "ground_truth.json"), "--json")
        self.assertEqual(code, 1)
        report = json.loads(text)
        self.assertEqual(report["verdict"], "hold")
        self.assertEqual(report["score"]["AN003731"], {"correct": 44, "wrong_included": 0, "wrongly_left_out": 0,
                                                       "held_for_person": 0})
        code, text = run("ST003128", te.stage3b_style("ST003128"))
        self.assertEqual(code, 2)
        self.assertIn("ERROR", text)


class FalsePassTest(TriageCase):
    """Wrong triage files that passed before the review of PR #58, and the one right layout that held."""

    def test_without_a_listing_shared_content_cannot_be_excluded_so_ready_holds(self) -> None:
        docs = keep_hilic_neg_by_folder(te.build_good("ST002283"))
        report, _ = self.run_validator("ST002283", docs, listing=False)
        self.assertEqual(report.unit_verdict("AN003729"), "hold")
        self.assertIn("T-DUP", self.checks(report, "hold", "AN003729"))
        report, _ = self.run_validator("ST004606", te.build_good("ST004606"), listing=False)
        self.assertEqual(report.verdict, "hold")
        self.assertEqual(self.checks(report), set())
        self.assertTrue(all(report.unit_verdict(u) == "hold" for u in report.units))

    def test_without_a_catalog_nothing_passes(self) -> None:
        docs = te.build_good("ST004606")
        docs["AN007766"]["excluded_rows"] = docs["AN007766"]["excluded_rows"][:-20]
        docs["AN007766"]["catalog_row_count"] -= 20
        report, _ = self.run_validator("ST004606", docs, catalog=False)
        self.assertEqual(report.verdict, "hold")
        self.assertIn("T-COVER", self.checks(report, "hold", "AN007766"))

    def test_a_row_without_member_path_is_the_same_file_as_the_member(self) -> None:
        for listing in (True, False):
            with self.subTest(listing=listing):
                docs = te.build_good("ST004606")
                neg = docs["AN007768"]
                row = next(e for e in neg["excluded_rows"] if e["raw_file"] == "20250128_FK_1-1.mzML")
                neg["excluded_rows"].remove(row)
                kept = dict(copy.deepcopy(docs["AN007766"]["rows"][0]), row_index=row["row_index"], member_path=None, ion_mode="negative")
                neg["rows"].append(kept)
                report, _ = self.run_validator("ST004606", docs, listing=listing)
                self.assertIn("T-UNIQ", self.checks(report))
                # Declared on both units, by raw_file or by member path, the shared file is accepted.
                for named in ("20250128_FK_1-1.mzML", "data/20250128_FK_1-1.mzML"):
                    kept["ion_mode"] = "positive"
                    docs["AN007768"]["shared_with"] = [{"unit_id": "AN007766", "member_paths": [named], "reason": "test"}]
                    docs["AN007766"]["shared_with"] = [{"unit_id": "AN007768", "member_paths": ["data/20250128_FK_1-1.mzML"], "reason": "test"}]
                    report, _ = self.run_validator("ST004606", docs, listing=listing)
                    self.assertNotIn("T-UNIQ", self.checks(report), named)
                    self.assertNotIn("T-SHARED", self.checks(report), named)

    def test_a_kept_row_the_listing_cannot_resolve_is_refused_in_a_ready_unit(self) -> None:
        docs = te.build_good("ST004606")
        docs["AN007766"]["rows"][0]["member_path"] = None
        report, _ = self.run_validator("ST004606", docs)
        self.assertEqual(report.verdict, "pass")  # 20250128_FK_1-1.mzML names exactly one member
        docs["AN007766"]["rows"][0]["raw_file"] = "nowhere.mzML"
        report, _ = self.run_validator("ST004606", docs, catalog=False)
        self.assertIn("T-MEMBER", self.checks(report, unit="AN007766"))

    def test_a_single_polarity_unit_cannot_carry_the_other_polarity_in_a_child(self) -> None:
        docs = te.build_good("ST004606")
        for row in list(docs["AN007768"]["rows"]):
            move_row(docs, row["raw_file"], "AN007768", "AN007766")
        pos = docs["AN007766"]
        pos["children"] = [
            {"child_key": f"{ion[:3]}-aif", "ion_mode": ion, "acquisition_mode": "AIF",
             "member_paths": [r["member_path"] for r in pos["rows"] if r["ion_mode"] == ion]}
            for ion in ("positive", "negative")
        ]
        report, _ = self.run_validator("ST004606", docs)
        self.assertEqual(report.unit_verdict("AN007766"), "error")
        self.assertTrue(any(f.check == "T-POL" and "child" in f.message for f in report.findings if f.unit == "AN007766"))

    def test_a_unit_that_runs_cannot_keep_mzxml(self) -> None:
        docs = te.build_good("ST002747")
        row = next(r for r in docs["AN004457"]["rows"] if r["raw_file"] == "Inf1_infected_NA_1__025")
        row["member_path"] = row["member_path"][: -len(".raw")] + ".mzXML"
        report, _ = self.run_validator("ST002747", docs)
        self.assertIn("T-FORMAT", self.checks(report, unit="AN004457"))
        docs["AN004457"]["status"] = "out_of_scope"
        report, _ = self.run_validator("ST002747", docs)
        self.assertNotIn("T-FORMAT", self.checks(report, unit="AN004457"))

    def test_an_exclusion_reason_must_agree_with_the_listing(self) -> None:
        docs = te.build_good("ST004606")
        row = docs["AN007766"]["rows"].pop(0)
        for d in docs.values():
            d["excluded_rows"] = [e for e in d["excluded_rows"] if e["raw_file"] != row["raw_file"]]
            d["excluded_rows"].append({"row_index": row["row_index"], "sample_id": row["sample_id"], "raw_file": row["raw_file"],
                                       "member_path": None, "reason": "not_in_archive", "belongs_to_unit": None, "detail": "said absent"})
        report, _ = self.run_validator("ST004606", docs)
        self.assertEqual(report.verdict, "error")
        self.assertTrue(all("T-EXCL" in self.checks(report, unit=u) for u in docs))
        docs = te.build_good("ST004606")
        e = docs["AN007767"]["excluded_rows"][0]
        e["reason"], e["belongs_to_unit"] = "duplicate_content", None
        report, _ = self.run_validator("ST004606", docs)
        self.assertIn("T-EXCL", self.checks(report, unit="AN007767"))

    def test_belongs_to_other_unit_is_checked_by_file_not_row_index(self) -> None:
        docs = te.build_good("ST004606")
        neg = docs["AN007768"]
        # Give AN007768 a different row order: its kept row 56 and its excluded row 0 trade indexes.
        kept = next(r for r in neg["rows"] if r["row_index"] == 56)
        left = next(e for e in neg["excluded_rows"] if e["row_index"] == 0)
        kept["row_index"], left["row_index"] = 0, 56
        pos = docs["AN007766"]
        row = pos["rows"].pop(0)  # 20250128_FK_1-1, row 0
        pos["excluded_rows"].append({"row_index": 0, "sample_id": row["sample_id"], "raw_file": row["raw_file"], "member_path": row["member_path"],
                                     "reason": "belongs_to_other_unit", "belongs_to_unit": "AN007768", "detail": "said to be AN007768's"})
        report, _ = self.run_validator("ST004606", docs, catalog=False)
        # The hold is for row 0 itself. The index swap above also leaves AN007768's row 56 pointing nowhere,
        # so a bare "some T-EXCL on AN007766" would pass even if the check went back to row indexes.
        excl = [f.message for f in report.findings if f.check == "T-EXCL" and f.level == "hold" and f.unit == "AN007766"]
        self.assertTrue(any(m.startswith("row 0 (20250128_FK_1-1, 20250128_FK_1-1.mzML) is said to belong to AN007768, but AN007768 does not keep") for m in excl), excl)

    def test_a_handoffs_folder_with_several_accessions(self) -> None:
        handoffs = self.tmp / "handoffs"
        all_docs = {}
        for accession in ("ST004606", "ST003128"):
            docs = te.build_good(accession)
            catalog, _, _ = te.load(accession)
            for unit in catalog["units"]:
                d = handoffs / unit["unit_id"]
                d.mkdir(parents=True)
                raw = json.dumps({"accession": accession, "analysis_unit_id": unit["unit_id"], "technical_settings": unit["declared"]}).encode("utf-8")
                (d / "handoff.json").write_bytes(raw)
                (d / "samples.json").write_text(json.dumps(unit["rows"]), encoding="utf-8")
                docs[unit["unit_id"]]["catalog_handoff_sha256"] = hashlib.sha256(raw).hexdigest()
            all_docs[accession] = docs
        _, _, listing = te.load("ST004606")
        report = vt.validate(te.write(all_docs["ST004606"], self.tmp / "triage" / "ST004606"), vt.read_catalog(handoffs), vt.read_listing(listing))
        self.assertEqual(report.verdict, "pass", [f.as_dict() for f in report.findings])
        # A unit of the same accession with no triage file still holds.
        del all_docs["ST004606"]["AN007769"]
        report = vt.validate(te.write(all_docs["ST004606"], self.tmp / "triage" / "ST004606-short"), vt.read_catalog(handoffs), vt.read_listing(listing))
        self.assertEqual({f.unit for f in report.findings if f.check == "T-SET"}, {"AN007769"})


INF25 = "Inf1_infected_NA_1__025"
INF25_RAW = f"VIAH002_003_WorkbenchUpload/Lipids/Neg/{INF25}.raw"


def messages(report, check: str, unit: str, level: str = "error") -> list[str]:
    return [f.message for f in report.findings if f.check == check and f.unit == unit and f.level == level]


class SecondReviewTest(TriageCase):
    """Wrong triage files that still passed after the first review's fixes (5ba5b4d), and the branches those
    fixes added without a test."""

    def test_name_tokens_are_read_from_the_member_a_raw_file_only_row_resolves_to(self) -> None:
        """ST002747: a Lipids/Neg file moved into the RP-positive unit, with and without its member_path."""
        for named in (True, False):
            with self.subTest(member_path=named):
                docs = te.build_good("ST002747")
                changes = {"ion_mode": "positive"} if named else {"ion_mode": "positive", "member_path": None}
                move_row(docs, INF25, "AN004457", "AN004456", **changes)
                for unit in ("AN004454", "AN004455"):
                    for e in docs[unit]["excluded_rows"]:
                        if e["raw_file"] == INF25:
                            e["belongs_to_unit"] = "AN004456"
                report, _ = self.run_validator("ST002747", docs)
                tokens = messages(report, "T-TOKEN", "AN004456")
                self.assertTrue(any(f"{INF25_RAW} says negative, the triage says positive" in m for m in tokens), tokens)
                self.assertEqual(report.unit_verdict("AN004456"), "error")

    def test_a_ready_unit_cannot_drop_a_row_for_a_reason_its_own_state_contradicts(self) -> None:
        """ST004606: AN007766's row 0 dropped by every (ready) unit for a reason none of them can have."""
        for reason in ("needs_person", "no_signal_abstained", "polarity_switching", "out_of_scope_ms1_only", "requires_conversion"):
            with self.subTest(reason=reason):
                docs = te.build_good("ST004606")
                row = docs["AN007766"]["rows"].pop(0)
                for d in docs.values():
                    d["excluded_rows"] = [e for e in d["excluded_rows"] if e["raw_file"] != row["raw_file"]]
                    d["excluded_rows"].append({"row_index": row["row_index"], "sample_id": row["sample_id"], "raw_file": row["raw_file"],
                                               "member_path": row["member_path"], "reason": reason, "belongs_to_unit": None, "detail": "dropped"})
                report, _ = self.run_validator("ST004606", docs)
                self.assertEqual(report.verdict, "error")
                for unit in docs:
                    excl = messages(report, "T-EXCL", unit)
                    self.assertTrue(any(f"excluded as {reason}" in m for m in excl), (unit, excl))

    def test_requires_conversion_is_checked_against_the_listing_for_a_raw_file_only_row(self) -> None:
        docs = te.build_good("ST004606")
        row = docs["AN007766"]["rows"].pop(0)
        docs["AN007766"]["excluded_rows"].append({"row_index": row["row_index"], "sample_id": row["sample_id"], "raw_file": row["raw_file"],
                                                  "member_path": None, "reason": "requires_conversion", "belongs_to_unit": None, "detail": "said mzXML"})
        report, _ = self.run_validator("ST004606", docs)
        self.assertTrue(any("data/20250128_FK_1-1.mzML, which MS-DIAL reads" in m for m in messages(report, "T-EXCL", "AN007766")))
        # An mzXML member excluded as requires_conversion is right (ST002747's good triage does it 78 times),
        # and an mzML member excluded so is not.
        docs = te.build_good("ST002747")
        report, _ = self.run_validator("ST002747", docs)
        self.assertEqual(report.verdict, "pass")
        mzxml = next(e for e in docs["AN004454"]["excluded_members"] if e["reason"] == "requires_conversion")
        docs["AN004454"]["excluded_members"].append(dict(mzxml, member_path=INF25_RAW))
        report, _ = self.run_validator("ST002747", docs)
        self.assertTrue(any(f"member {INF25_RAW} is excluded as requires_conversion" in m for m in messages(report, "T-EXCL", "AN004454")))

    def test_a_flagged_and_answered_unit_may_exclude_for_its_flag(self) -> None:
        """ST003128: AN005128 carries polarity_switching and ms1_only; AN005129 only ms1_only."""
        docs = te.build_good("ST003128")
        own = docs["AN005128"]["excluded_rows"]
        own[0]["reason"], own[0]["belongs_to_unit"] = "polarity_switching", None
        own[1]["reason"], own[1]["belongs_to_unit"] = "out_of_scope_ms1_only", None
        theirs = docs["AN005129"]["excluded_rows"]
        theirs[0]["reason"], theirs[0]["belongs_to_unit"] = "out_of_scope_ms1_only", None
        report, _ = self.run_validator("ST003128", docs)
        self.assertEqual(messages(report, "T-EXCL", "AN005128"), [])
        self.assertEqual(messages(report, "T-EXCL", "AN005129"), [])
        theirs[1]["reason"], theirs[1]["belongs_to_unit"] = "polarity_switching", None
        report, _ = self.run_validator("ST003128", docs)
        self.assertTrue(any("without a polarity_switching flag" in m for m in messages(report, "T-EXCL", "AN005129")))

    def test_one_acquisition_is_not_kept_twice_as_vendor_file_and_mzml_copy(self) -> None:
        """ST003964: the mzml/ folder holds an mzML copy of every Thermo .raw."""
        for unit in ("AN006515", "AN006516"):
            with self.subTest(unit=unit):
                docs = te.build_good("ST003964")
                d = docs[unit]
                raw = d["rows"][0]
                copy_path = "mzml/" + raw["member_path"].rsplit("/", 1)[-1][: -len(".raw")] + ".mzML"
                d["added_members"].append({k: raw[k] for k in ("archive", "role", "ion_mode", "chromatography", "acquisition_mode", "basis", "evidence")}
                                          | {"member_path": copy_path})
                report, _ = self.run_validator("ST003964", docs)
                self.assertEqual(report.unit_verdict(unit), "error")
                self.assertTrue(any("kept twice, in 2 formats" in m and copy_path in m for m in messages(report, "T-UNIQ", unit)))
                listed_as_copy = any(e["member_path"] == copy_path for e in d["excluded_members"])
                self.assertEqual(listed_as_copy, unit == "AN006515")
                both = [m for m in messages(report, "T-EXCL", unit) if "and also kept" in m]
                self.assertEqual(bool(both), listed_as_copy, both)

    def test_an_excluded_member_said_to_be_another_units_is_checked_against_that_unit(self) -> None:
        """ST002283: AN003728 drops its blank as AN003729's, which does not keep it."""
        blank = "AB_Human_Bone/Human_Hilic_Pos/Blank_Hilic_Pos.mzML"
        docs = te.build_good("ST002283")
        pos = docs["AN003728"]
        member = next(m for m in pos["added_members"] if m["member_path"] == blank)
        pos["added_members"].remove(member)
        pos["excluded_members"].append({"member_path": blank, "archive": member["archive"], "reason": "belongs_to_other_unit",
                                        "belongs_to_unit": "AN003729", "detail": "said to be AN003729's"})
        report, _ = self.run_validator("ST002283", docs)
        self.assertNotEqual(report.unit_verdict("AN003728"), "pass")
        self.assertIn(f"member {blank} is said to belong to AN003729, but AN003729 does not keep that file",
                      messages(report, "T-EXCL", "AN003728", "hold"))
        pos["excluded_members"][-1]["belongs_to_unit"] = None
        report, _ = self.run_validator("ST002283", docs)
        self.assertTrue(any("without naming the unit" in m for m in messages(report, "T-EXCL", "AN003728")))
        # Kept by the unit it is said to belong to, the exclusion stands.
        pos["excluded_members"][-1]["belongs_to_unit"] = "AN003729"
        docs["AN003729"]["added_members"].append(member)
        report, _ = self.run_validator("ST002283", docs)
        self.assertEqual(messages(report, "T-EXCL", "AN003728", "hold") + messages(report, "T-EXCL", "AN003728"), [])

    def test_the_catalog_putting_the_unit_in_another_accession_is_an_error(self) -> None:
        docs = te.build_good("ST004606")
        docs["AN007766"]["accession"] = "ST000001"
        report, _ = self.run_validator("ST004606", docs)
        self.assertIn("the Catalog puts unit AN007766 in ST004606, the triage in ST000001", messages(report, "T-COVER", "AN007766"))

    def test_a_raw_file_only_row_that_resolves_to_mzxml_is_refused(self) -> None:
        """ST002747 with only the Lipids/Neg mzXML of Inf1_infected_NA_1__025 in the listing."""
        docs = te.build_good("ST002747")
        row = next(r for r in docs["AN004457"]["rows"] if r["raw_file"] == INF25)
        row["member_path"] = None
        _, _, listing_path = te.load("ST002747")
        listing = [m for m in vt.read_listing(listing_path) if vt.stem(m.path) != INF25 or m.path.endswith("Lipids/Neg/" + INF25 + ".mzXML")]
        report = vt.validate(te.write(docs, self.tmp / "mzxml-only"), vt.read_catalog(te.fixture_dir("ST002747") / "catalog.json"), listing)
        formats = messages(report, "T-FORMAT", "AN004457")
        self.assertTrue(any(m.startswith(f"{INF25} (VIAH002_003_WorkbenchUpload/Lipids/Neg/{INF25}.mzXML) is mzXML") for m in formats), formats)

    def test_a_row_the_index_cannot_resolve_joins_each_same_stem_member_without_merging_them(self) -> None:
        """Without a listing, an mzML copy beside the .raw makes the bare name Inf1_infected_NA_1__025 ambiguous."""
        copy_path = f"VIAH002_003_WorkbenchUpload/Other/{INF25}.mzML"
        docs = te.build_good("ST002747")
        next(r for r in docs["AN004457"]["rows"] if r["raw_file"] == INF25)["member_path"] = None
        raw_row = te.build_good("ST002747")["AN004457"]["rows"]
        raw_row = next(r for r in raw_row if r["raw_file"] == INF25)
        fields = {k: raw_row[k] for k in ("archive", "role", "ion_mode", "chromatography", "acquisition_mode", "basis", "evidence")}
        docs["AN004456"]["added_members"].append(fields | {"member_path": INF25_RAW})
        docs["AN004454"]["excluded_members"].append({"member_path": copy_path, "archive": "ST002747_rawdata.zip",
                                                    "reason": "alternative_format_copy", "belongs_to_unit": None, "detail": "test copy"})
        report, _ = self.run_validator("ST002747", docs, listing=False)
        uniq = [f.message for f in report.findings if f.check == "T-UNIQ"]
        self.assertTrue(any("kept by both AN004456 and AN004457" in m for m in uniq), uniq)
        # The copy kept by a third unit: the unresolved row may be either file, the two members are not one.
        docs["AN004454"]["excluded_members"].pop()
        docs["AN004455"]["added_members"].append(fields | {"member_path": copy_path})
        report, _ = self.run_validator("ST002747", docs, listing=False)
        uniq = [f.message for f in report.findings if f.check == "T-UNIQ"]
        self.assertTrue(any("kept by both AN004455 and AN004457" in m for m in uniq), uniq)
        self.assertTrue(any("kept by both AN004456 and AN004457" in m for m in uniq), uniq)
        self.assertFalse(any("kept by both AN004455 and AN004456" in m for m in uniq), uniq)


class TheClassIsNotTheTriagesTest(TriageCase):
    """WS0 decision F (2026-11-17): the triage runs before any Class is saved and binds the evidence it read; the Class
    binds through its own ratification afterwards. The validator reads no class_proposal, and refuses a triage whose
    evidence, or whose decision record's inputs, cite the Class."""

    ACCESSION = "ST004606"

    def validate(self, docs: dict, name: str):
        directory = te.write(docs, self.tmp / name)
        _, _, listing_path = te.load(self.ACCESSION)
        return vt.validate(directory, vt.read_catalog(te.fixture_dir(self.ACCESSION) / "catalog.json"),
                           vt.read_listing(listing_path)), directory

    def test_a_good_triage_cites_no_class(self) -> None:
        report, _ = self.validate(te.build_good(self.ACCESSION), "good")
        self.assertEqual(report.verdict, "pass")
        self.assertFalse([f for f in report.findings if f.check == "T-CLASS"])

    def test_evidence_citing_the_class_proposal_is_refused(self) -> None:
        unit = "AN007766"
        cases = {
            "a field naming the proposal": {"source": "catalog_unit", "field": "class_proposal.assignments", "value": "QC"},
            "a field naming a class label": {"source": "catalog_row", "field": "class_label", "value": "Infected"},
            "a value quoting the proposal": {"source": "catalog_unit", "field": "grouping",
                                             "value": "class proposal groups this file with the QC samples"},
            "the ratification": {"source": "catalog_unit", "field": "ratification.campaign_id", "value": "campaign-a"},
        }
        for label, citation in cases.items():
            with self.subTest(label):
                docs = te.build_good(self.ACCESSION)
                docs[unit]["rows"][0]["evidence"].append(dict(citation))
                report, _ = self.validate(docs, f"row-{len(label)}-{label[:6].replace(' ', '_')}")
                self.assertTrue(messages(report, "T-CLASS", unit), label)
                self.assertEqual(report.unit_verdict(unit), "error")
        docs = te.build_good(self.ACCESSION)
        docs[unit]["evidence_sources"] = [*docs[unit].get("evidence_sources", []),
                                          {"source": "catalog_unit", "detail": "the handoff's class_proposal assignments"}]
        report, _ = self.validate(docs, "sources")
        self.assertTrue(messages(report, "T-CLASS", unit))

    def test_a_decision_record_binding_the_class_is_refused(self) -> None:
        unit = "AN007766"
        for label, extra, refused in (
                ("a class_proposal input", {"role": "class_proposal", "path": "inputs/AN007766/class.json"}, True),
                ("a selector into the proposal", {"role": "catalog_handoff", "path": "inputs/AN007766/handoff.json",
                                                  "selector": "$.class_proposal.assignments"}, True),
                ("the listing and the handoff only", {"role": "catalog_handoff", "path": "inputs/AN007766/handoff.json"},
                 False)):
            with self.subTest(label):
                docs = te.build_good(self.ACCESSION)
                directory = te.write(docs, self.tmp / f"record-{int(refused)}-{len(label)}")
                (directory / f"{unit}.record.json").write_text(json.dumps({
                    "schema": "msdial-llm-decision-record.v1", "kind": "unit_triage", "unit_id": unit,
                    "inputs": [{"role": "archive_listing", "path": "inputs/archive-members.tsv", "sha256": "a" * 64},
                               {**extra, "sha256": "b" * 64}]}), encoding="utf-8")
                _, _, listing_path = te.load(self.ACCESSION)
                report = vt.validate(directory, vt.read_catalog(te.fixture_dir(self.ACCESSION) / "catalog.json"),
                                     vt.read_listing(listing_path))
                self.assertEqual(bool(messages(report, "T-CLASS", unit)), refused, label)

    def test_the_validator_reads_no_class_proposal(self) -> None:
        """Two handoffs that differ only in their Class read as the same Catalog unit, and the script names
        class_proposal in no key it reads."""
        import ast

        tree = ast.parse((ROOT / "scripts" / "validate-triage.py").read_text(encoding="utf-8"))
        keys = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertFalse({"class_proposal", "assignments", "ratification", "ratified_by"} & keys)
        rows = [{"sample_id": "S1", "raw_file": "S1"}]
        read = []
        for name, proposal in (("none", None), ("ratified", {"proposal_id": "c", "status": "accepted",
                                                             "assignments": [{"sample_id": "S1", "class_label": "B"}],
                                                             "ratified_by": "appr", "ratification": {"approval_id": "appr"}})):
            unit_dir = self.tmp / f"handoffs-{name}" / "AN1"
            unit_dir.mkdir(parents=True)
            (unit_dir / "handoff.json").write_text(json.dumps({
                "accession": "ACC1", "sample_metadata": rows, "class_proposal": proposal,
                "technical_settings": {"chromatography": "RP", "ion_mode": "Positive", "acquisition_mode": "DDA"}}),
                encoding="utf-8")
            unit = vt.read_catalog(unit_dir.parent)["AN1"]
            read.append((unit.rows, unit.declared, unit.accession))
        self.assertEqual(read[0], read[1])

    def test_the_skill_tells_the_agent_not_to_read_the_class(self) -> None:
        skill = (ROOT / ".claude" / "skills" / "msdial-unit-triage" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("class_proposal", skill)
        self.assertIn("T-CLASS", skill)


if __name__ == "__main__":
    unittest.main()
