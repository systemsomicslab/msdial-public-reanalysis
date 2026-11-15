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


class PrototypeFailureModeTest(TriageCase):
    def test_st002283_duplicate_content_settled_by_folder_name_is_an_error(self) -> None:
        """The prototype kept Human_Hilic_Neg for the HILIC-negative unit at medium confidence."""
        docs = te.build_good("ST002283")
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
        report, directory = self.run_validator("ST002283", docs)
        self.assertIn("T-DUP", self.checks(report, unit="AN003729"))
        self.assertEqual(report.unit_verdict("AN003729"), "error")
        _, truth, _ = te.load("ST002283")
        self.assertEqual(vt.score(directory, truth)["AN003729"], {"correct": 0, "wrong_included": 43, "wrongly_left_out": 0})

        # Flagged and sent to a person, the same selection is a hold, not an error.
        hilic["status"] = "needs_person"
        hilic["needs_person"] = [{"code": "shared_content", "question": "Which unit, if any, do the shared negative files belong to?"}]
        hilic["flags"] = [{"code": "shared_content", "certainty": "confirmed", "detail": "Same CRC32 and size as Human_Lipid_Neg."}]
        report, _ = self.run_validator("ST002283", docs)
        self.assertEqual(self.checks(report), set())
        self.assertIn("T-DUP", self.checks(report, "hold", "AN003729"))

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
        self.assertEqual(report["score"]["AN003731"], {"correct": 44, "wrong_included": 0, "wrongly_left_out": 0})
        code, text = run("ST003128", te.stage3b_style("ST003128"))
        self.assertEqual(code, 2)
        self.assertIn("ERROR", text)


if __name__ == "__main__":
    unittest.main()
