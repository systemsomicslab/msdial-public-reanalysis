"""scripts/expand-triage-spec.py: the agent's compact spec expanded into msdial-unit-triage.v2 payloads by code.

docs/llm-assisted-curation.md section 2.2. The agent decides (groups, dispositions, exceptions, unit status, flags,
questions); the expander transcribes, deterministically, over the hashed inputs only. Checked here: a golden expansion
of a synthetic accession, byte determinism, every X-* finding, exceptions, the automatic belongs_to_other_unit, a group
kept by two units, children and flag groups, rule_id and the v2 schema on every payload; and that the hand-written
specs of the eight eval accessions (tests/fixtures/triage-eval-specs/, test data only) expand to payloads that score no
wrong inclusion and reach the expected pre-download status. Synthetic ids (ST000001, AN000001, ...) apart from the eval
fixtures.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(TESTS))
import triage_eval as te  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ex = _load("expand_triage_spec_under_test", ROOT / "scripts" / "expand-triage-spec.py")
vt = _load("validate_triage_for_expander_tests", ROOT / "scripts" / "validate-triage.py")
from test_validate_triage import EXPECTED  # noqa: E402

ACC = "ST000001"
U1, U2 = "AN000001", "AN000002"
CTX = ex.ExpansionContext(decided_at="2026-11-18T10:00:00+09:00", model="synthetic-model", skill_version="1.2.0")
ROWS = [
    {"sample_id": "S01_HILIC_pos", "raw_file": "S01_HILIC_pos.mzML", "attributes": {"Mode": "HILIC Positive"}},
    {"sample_id": "S01_RP_neg", "raw_file": "S01_RP_neg.mzML", "attributes": {"Mode": "RP Negative"}},
    {"sample_id": "S02_HILIC_pos", "raw_file": "S02_HILIC_pos.mzML", "attributes": {"Mode": "HILIC Positive"}},
    {"sample_id": "S02_RP_neg", "raw_file": "S02_RP_neg.mzML", "attributes": {"Mode": "RP Negative"}},
    {"sample_id": "X_Lip_neg", "raw_file": "X_Lip_neg.mzML", "attributes": {"Mode": "RP Negative"}},
    {"sample_id": "Missing_pos", "raw_file": "Missing_pos.mzML", "attributes": {"Mode": "HILIC Positive"}},
]
LISTING = [
    ("HILIC_POS/S01_HILIC_pos.mzML", 2000001, "00000001"),
    ("HILIC_POS/S02_HILIC_pos.mzML", 2000002, "00000002"),
    ("HILIC_POS/QC_HILIC_pos.mzML", 2000003, "00000003"),
    ("HILIC_POS/S01_HILIC_pos.mzXML", 3000001, "000000A1"),
    ("COPY/S01_HILIC_pos.mzXML", 3000001, "000000A1"),
    ("RP_NEG/S01_RP_neg.mzML", 2000011, "00000011"),
    ("RP_NEG/S02_RP_neg.mzML", 2000012, "00000012"),
    ("RP_NEG/X__LipNeg.mzML", 2000013, "00000013"),
    ("RP_NEG/notes.xlsx", 5000, "000000FF"),
    ("SHARED/Blank_01.mzML", 2000021, "00000021"),
]
EV = [{"source": "catalog_row", "field": "Mode", "value": "{attribute:Mode}"},
      {"source": "folder_token", "field": "folder", "value": "{folder}"}]


def files(listing=LISTING, rows=ROWS) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for unit, chrom, ion in ((U1, "HILIC", "Positive"), (U2, "Reversed phase", "Negative")):
        handoff = {"repository": "metabolomics_workbench", "accession": ACC, "analysis_unit_id": unit,
                   "source_subrecord_id": unit, "technical_settings": {"chromatography": chrom, "ion_mode": ion,
                                                                      "acquisition_mode": "DDA", "target_omics": "Metabolomics"}}
        out[f"{unit}/handoff.json"] = json.dumps(handoff, indent=2).encode("utf-8")
        out[f"{unit}/samples.json"] = json.dumps(rows, indent=2).encode("utf-8")
    lines = ["archive\tpath\tsize\tcrc32"] + [f"{ACC}.zip\t{p}\t{s}\t{c}" for p, s, c in listing]
    out[f"{ACC}.members.tsv"] = ("\n".join(lines) + "\n").encode("utf-8")
    return out


def spec() -> dict:
    return {
        "schema": "msdial-unit-triage-spec.v1", "accession": ACC, "phase": "pre_download",
        "structure": "HILIC_POS holds the HILIC positive arm, RP_NEG the reversed-phase negative arm.",
        "evidence_sources": [{"source": "archive_listing", "detail": "Listing read before download."}],
        "groups": [
            {"id": "G1", "applies_to": "rows", "match": {"attribute": {"Mode": "HILIC Positive"}, "resolved_member": {"folder": "HILIC_POS"}},
             "file": {"role": "sample", "ion_mode": "positive", "chromatography": "HILIC", "acquisition_mode": "DDA", "basis": "declared"},
             "evidence": EV, "disposition": {"keep": U1}, "confidence": "high", "rationale": "Mode is HILIC Positive and the file sits under HILIC_POS/."},
            {"id": "G2", "applies_to": "rows", "match": {"resolved_member": {"folder": "RP_NEG"}},
             "file": {"role": "sample", "ion_mode": "negative", "chromatography": "RP", "acquisition_mode": "DDA", "basis": "declared"},
             "evidence": EV, "disposition": {"keep": U2}, "confidence": "high", "rationale": "Mode is RP Negative and the file sits under RP_NEG/."},
            {"id": "G3", "applies_to": "rows", "match": {"resolution": "none", "sample_id": {"regex": "^Missing_"}},
             "disposition": {"exclude": "not_in_archive", "detail": "No member carries this name."},
             "confidence": "high", "rationale": "The listing has no file of this name."},
            {"id": "M1", "applies_to": "members", "match": {"folder": "HILIC_POS", "regex": "/QC_"},
             "file": {"role": "qc", "ion_mode": "positive", "chromatography": "HILIC", "acquisition_mode": "DDA", "basis": "inferred"},
             "evidence": [{"source": "folder_token", "field": "folder", "value": "{folder}"}],
             "disposition": {"keep": U1}, "confidence": "medium", "rationale": "A pooled QC in the HILIC positive folder."},
            {"id": "M2", "applies_to": "members", "match": {"ext": ".mzxml", "folder": "HILIC_POS"},
             "disposition": {"exclude": "requires_conversion", "detail": "mzXML is not an MS-DIAL input."},
             "confidence": "high", "rationale": "An mzXML copy beside the mzML."},
            {"id": "M3", "applies_to": "members", "match": {"folder": "COPY", "twin": True},
             "disposition": {"exclude": "duplicate_content", "detail": "Byte-identical to an excluded mzXML."},
             "confidence": "high", "rationale": "A second copy of an excluded file."},
            {"id": "BL", "applies_to": "members", "match": {"paths": ["SHARED/Blank_01.mzML"]},
             "file": {"role": "blank", "ion_mode": "unknown", "chromatography": "unknown", "acquisition_mode": "unknown", "basis": "inferred"},
             "evidence": [{"source": "filename_token", "field": "name", "value": "{member_path}"}],
             "disposition": {"keep": [U1, U2], "shared_reason": "One blank serves both arms."},
             "confidence": "low", "rationale": "A blank in a shared folder."},
        ],
        "exceptions": [
            {"id": "E1", "row": {"sample_id": "X_Lip_neg"}, "member_path": "RP_NEG/X__LipNeg.mzML", "group": "G2",
             "file": {"basis": "inferred"},
             "evidence": [{"source": "filename_token", "field": "name drift", "value": "{raw_file} is {member_path}"}],
             "confidence": "medium", "rationale": "Name drift: the member drops the separator before Neg."},
        ],
        "units": {
            U1: {"status": "needs_person", "confidence": "high", "rationale": "The HILIC positive files.",
                 "unit_technical": {"ion_mode": "positive", "chromatography": "HILIC", "acquisition_mode": "DDA"},
                 "flags": [{"code": "other", "certainty": "suspected", "detail": "A blank of unknown polarity is shared.", "groups": ["BL"]}],
                 "needs_person": [{"code": "other", "question": "Does the shared blank belong to this arm?", "groups": ["BL"]}]},
            U2: {"status": "needs_person", "confidence": "high", "rationale": "The reversed-phase negative files.",
                 "unit_technical": {"ion_mode": "negative", "chromatography": "RP", "acquisition_mode": "DDA"},
                 "needs_person": [{"code": "other", "question": "Does the shared blank belong to this arm?", "groups": ["BL"]}]},
        },
    }


def run(document: dict, *, ctx=CTX, input_files=None) -> "ex.Expansion":
    data = json.dumps(document, indent=1).encode("utf-8")
    return ex.expand(data, ex.inputs_from_files(input_files or files()), ctx, spec_name=f"{ACC}.spec.json")


def codes(result) -> set[str]:
    return {f.code for f in result.findings}


def shuffled(value, rng: random.Random):
    if isinstance(value, dict):
        items = list(value.items())
        rng.shuffle(items)
        return {k: shuffled(v, rng) for k, v in items}
    if isinstance(value, list):
        return [shuffled(v, rng) for v in value]
    return value


class GoldenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = run(spec())
        self.assertTrue(self.result.ok, [f.as_dict() for f in self.result.findings])
        self.docs = {u: json.loads(b) for u, b in self.result.payloads.items()}

    def test_golden_expansion(self) -> None:
        one, two = self.docs[U1], self.docs[U2]
        self.assertEqual([r["row_index"] for r in one["rows"]], [0, 2])
        self.assertEqual([r["row_index"] for r in two["rows"]], [1, 3, 4])
        self.assertEqual(one["rows"][0]["evidence"], [{"source": "catalog_row", "field": "Mode", "value": "HILIC Positive"},
                                                      {"source": "folder_token", "field": "folder", "value": "HILIC_POS"}])
        self.assertEqual(one["rows"][0]["member_path"], "HILIC_POS/S01_HILIC_pos.mzML")
        self.assertEqual(one["rows"][0]["archive"], f"{ACC}.zip")
        # Another unit's group: belongs_to_other_unit, with the keeper named and its rationale inherited.
        theirs = next(e for e in one["excluded_rows"] if e["row_index"] == 1)
        self.assertEqual((theirs["reason"], theirs["belongs_to_unit"], theirs["detail"], theirs["rationale"], theirs["rule_id"]),
                         ("belongs_to_other_unit", U2, f"Kept by {U2} (group G2).",
                          "Belongs to AN000002: Mode is RP Negative and the file sits under RP_NEG/.", "G2"))
        missing = next(e for e in one["excluded_rows"] if e["row_index"] == 5)
        self.assertEqual((missing["reason"], missing["member_path"], missing["rule_id"]), ("not_in_archive", None, "G3"))
        # The exception: its member, its own evidence and rationale, the group's file fields.
        drift = next(r for r in two["rows"] if r["row_index"] == 4)
        self.assertEqual((drift["member_path"], drift["rule_id"], drift["ion_mode"], drift["confidence"]),
                         ("RP_NEG/X__LipNeg.mzML", "E1", "negative", "medium"))
        self.assertEqual(drift["evidence"][0]["value"], "X_Lip_neg.mzML is RP_NEG/X__LipNeg.mzML")
        self.assertEqual(next(e for e in one["excluded_rows"] if e["row_index"] == 4)["detail"], f"Kept by {U2} (exception E1).")
        # Members: the QC kept by one unit, the shared blank by both with reciprocal shared_with, exclusions in every unit.
        self.assertEqual([m["member_path"] for m in one["added_members"]], ["HILIC_POS/QC_HILIC_pos.mzML", "SHARED/Blank_01.mzML"])
        self.assertEqual([m["member_path"] for m in two["added_members"]], ["SHARED/Blank_01.mzML"])
        self.assertEqual(one["shared_with"], [{"unit_id": U2, "member_paths": ["SHARED/Blank_01.mzML"], "reason": "One blank serves both arms."}])
        self.assertEqual(two["shared_with"], [{"unit_id": U1, "member_paths": ["SHARED/Blank_01.mzML"], "reason": "One blank serves both arms."}])
        for doc in (one, two):
            reasons = {e["member_path"]: e["reason"] for e in doc["excluded_members"]}
            self.assertEqual(reasons["HILIC_POS/S01_HILIC_pos.mzXML"], "requires_conversion")
            self.assertEqual(reasons["COPY/S01_HILIC_pos.mzXML"], "duplicate_content")
            self.assertNotIn("RP_NEG/notes.xlsx", reasons, "a non-raw member is not covered")
        # Flags and questions name their groups as paths and indices.
        self.assertEqual(one["flags"][0]["member_paths"], ["SHARED/Blank_01.mzML"])
        self.assertEqual(one["flags"][0]["row_indices"], [])
        # Code wrote what the agent used to copy.
        handoff = files()[f"{U1}/handoff.json"]
        self.assertEqual(one["catalog_handoff_sha256"], hashlib.sha256(handoff).hexdigest())
        self.assertEqual((one["catalog_row_count"], one["decided_at"], one["decided_by"]["model"], one["decided_by"]["skill_version"]),
                         (6, CTX.decided_at, "synthetic-model", "1.2.0"))
        self.assertEqual(one["unit_technical"]["declared"], {"chromatography": "HILIC", "ion_mode": "Positive",
                                                              "acquisition_mode": "DDA", "target_omics": "Metabolomics"})
        self.assertEqual(one["derivation"]["spec"], f"{ACC}.spec.json")
        self.assertEqual(one["derivation"]["expander_version"], ex.EXPANDER_VERSION)

    def test_every_item_carries_a_rule_id_and_the_v2_schema_holds(self) -> None:
        schema = vt.load_schema(vt.SCHEMA_V2_ID)
        ids = {g["id"] for g in spec()["groups"]} | {e["id"] for e in spec()["exceptions"]}
        for doc in self.docs.values():
            self.assertEqual(vt.schema_errors(doc, schema, schema), [])
            for key in ("rows", "added_members", "excluded_rows", "excluded_members"):
                for item in doc[key]:
                    self.assertIn(item["rule_id"], ids)

    def test_the_payloads_pass_the_validators_payload_checks(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            inputs = directory / "inputs"
            for relative, data in files().items():
                (inputs / relative).parent.mkdir(parents=True, exist_ok=True)
                (inputs / relative).write_bytes(data)
            for unit, data in self.result.payloads.items():
                (directory / f"{unit}.json").write_bytes(data)
            report = vt.validate(directory, vt.read_catalog(inputs), vt.read_listing(inputs / f"{ACC}.members.tsv"),
                                 payload_only=True)
            self.assertFalse([f.as_dict() for f in report.findings if f.level == "error"])
            self.assertEqual(report.unit_status, {U1: "needs_person", U2: "needs_person"})

    def test_byte_determinism(self) -> None:
        again = run(spec())
        self.assertEqual(again.payloads, self.result.payloads)
        rng = random.Random(7)
        for _ in range(3):
            mixed = run(shuffled(spec(), rng))
            # The spec's bytes (and so derivation.spec_sha256) differ; nothing else does, key order included.
            for unit, data in mixed.payloads.items():
                expected = self.result.payloads[unit].replace(self.docs[unit]["derivation"]["spec_sha256"].encode(),
                                                              json.loads(data)["derivation"]["spec_sha256"].encode())
                self.assertEqual(data, expected, "dict order of the spec changes nothing")
        reordered = files()
        reordered = dict(reversed(list(reordered.items())))
        self.assertEqual(run(spec(), input_files=reordered).payloads, self.result.payloads)

    def test_children_take_the_kept_items_of_their_key(self) -> None:
        document = spec()
        document["groups"][1]["disposition"]["child"] = "neg_DDA"
        document["exceptions"][0]["disposition"] = {"keep": U2, "child": "neg_DDA"}
        document["groups"][6]["disposition"]["child"] = {U2: "neg_DDA", U1: "pos_DDA"}
        document["groups"][0]["disposition"]["child"] = "pos_DDA"
        document["groups"][3]["disposition"]["child"] = "pos_DDA"
        document["units"][U2]["children"] = [{"child_key": "neg_DDA", "ion_mode": "negative", "acquisition_mode": "DDA"}]
        document["units"][U1]["children"] = [{"child_key": "pos_DDA", "ion_mode": "positive", "acquisition_mode": "DDA"}]
        result = run(document)
        self.assertTrue(result.ok, [f.as_dict() for f in result.findings])
        two = json.loads(result.payloads[U2])
        self.assertEqual(two["children"], [{"child_key": "neg_DDA", "ion_mode": "negative", "acquisition_mode": "DDA", "member_paths": [
            "RP_NEG/S01_RP_neg.mzML", "RP_NEG/S02_RP_neg.mzML", "RP_NEG/X__LipNeg.mzML", "SHARED/Blank_01.mzML"]}])
        # A kept item outside every child of a unit that has children, or a child the unit lacks, is refused.
        document["groups"][6]["disposition"]["child"] = "neg_DDA"
        self.assertIn("X-CHILD", codes(run(document)))


class FindingTests(unittest.TestCase):
    def assertFinds(self, document: dict, code: str, input_files=None, ctx=CTX) -> list:
        result = run(document, input_files=input_files, ctx=ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.payloads, {}, "nothing is expanded when anything is found")
        self.assertIn(code, codes(result), [f.as_dict() for f in result.findings])
        return [f for f in result.findings if f.code == code]

    def test_schema(self) -> None:
        document = spec()
        del document["structure"]
        self.assertFinds(document, "X-SCHEMA")
        document = spec()
        document["groups"][6]["disposition"].pop("shared_reason")
        self.assertFinds(document, "X-SCHEMA")
        document = spec()
        document["groups"][0]["match"] = {"sample_id": {"regex": "(unclosed"}}
        self.assertFinds(document, "X-SCHEMA")

    def test_units(self) -> None:
        document = spec()
        del document["units"][U2]
        self.assertFinds(document, "X-UNITS")

    def test_uncovered_and_overlapping_rows(self) -> None:
        document = spec()
        document["groups"] = [g for g in document["groups"] if g["id"] != "G3"]
        [finding] = self.assertFinds(document, "X-UNCOVERED-ROW")[:1]
        self.assertIn("5 (Missing_pos)", finding.message)
        document = spec()
        document["groups"].append({**copy.deepcopy(document["groups"][0]), "id": "G9",
                                   "match": {"sample_id": {"regex": "^S01_"}}})
        self.assertFinds(document, "X-OVERLAP-ROW")

    def test_uncovered_and_overlapping_members(self) -> None:
        document = spec()
        document["groups"] = [g for g in document["groups"] if g["id"] != "M2"]
        self.assertFinds(document, "X-UNCOVERED-MEMBER")
        document = spec()
        document["groups"].append({**copy.deepcopy(document["groups"][4]), "id": "M9", "match": {"ext": [".mzXML"]}})
        self.assertFinds(document, "X-OVERLAP-MEMBER")

    def test_empty_group_and_unknown_reference(self) -> None:
        document = spec()
        document["groups"].append({**copy.deepcopy(document["groups"][2]), "id": "G8", "match": {"sample_id": "nothing"}})
        self.assertFinds(document, "X-EMPTY-GROUP")
        document = spec()
        document["units"][U1]["flags"][0]["groups"] = ["NOPE"]
        self.assertFinds(document, "X-UNKNOWN-REF")
        document = spec()
        document["exceptions"][0]["group"] = "NOPE"
        self.assertFinds(document, "X-UNKNOWN-REF")

    def test_unresolved_and_ambiguous(self) -> None:
        document = spec()
        document["exceptions"] = []
        document["groups"].append({**copy.deepcopy(document["groups"][1]), "id": "G4", "match": {"sample_id": {"regex": "^X_"}}})
        self.assertFinds(document, "X-UNRESOLVED")
        listing = LISTING + [("OTHER/S02_HILIC_pos.mzML", 2000099, "00000099")]
        document = spec()
        document["groups"].append({"id": "M4", "applies_to": "members", "match": {"folder": "OTHER"},
                                   "disposition": {"exclude": "other", "detail": "A stray copy."}, "confidence": "high",
                                   "rationale": "Outside the arm folders."})
        document["groups"][0]["match"] = {"attribute": {"Mode": "HILIC Positive"}, "resolution": ["exact", "ambiguous"]}
        self.assertFinds(document, "X-AMBIGUOUS", input_files=files(listing))
        # Narrowing where the group's rows are looked up settles it.
        document["groups"][0]["resolve"] = {"within": "HILIC_POS"}
        result = run(document, input_files=files(listing))
        self.assertTrue(result.ok, [f.as_dict() for f in result.findings])
        self.assertEqual(json.loads(result.payloads[U1])["rows"][1]["member_path"], "HILIC_POS/S02_HILIC_pos.mzML")

    def test_placeholder_without_a_value(self) -> None:
        document = spec()
        document["groups"][0]["evidence"] = [{"source": "catalog_row", "field": "Batch", "value": "{attribute:Batch}"}]
        self.assertFinds(document, "X-PLACEHOLDER")

    def test_heterogeneous_group(self) -> None:
        listing = LISTING + [("HILIC_POS/QC_HILIC_neg.mzML", 2000098, "00000098")]
        document = spec()
        [finding] = self.assertFinds(document, "X-HETERO", input_files=files(listing))
        self.assertIn("kept group M1 mixes polarity tokens negative+positive; positive", finding.message)
        document["groups"][3]["heterogeneous"] = "A negative QC was filed with the positive arm."
        self.assertNotIn("X-HETERO", codes(run(document, input_files=files(listing))))

    def test_shared_content_without_a_kept_twin(self) -> None:
        document = spec()
        document["units"][U1]["flags"].append({"code": "shared_content", "certainty": "confirmed",
                                                "detail": "Two mzXML copies are identical."})
        self.assertFinds(document, "X-DUP-NOKEPT")
        document["units"][U1]["flags"][-1]["code"] = "duplicate_copies_excluded"
        self.assertTrue(run(document).ok)

    def test_revision_scope(self) -> None:
        base = run(spec())
        current = {u: json.loads(b) for u, b in base.payloads.items()}
        document = spec()
        document["units"][U1]["status"] = "ready"
        document["units"][U1]["flags"] = []
        document["units"][U1]["needs_person"] = []
        document["groups"][6]["confidence"] = "medium"
        ctx = ex.ExpansionContext(decided_at="2026-11-19T10:00:00+09:00", model="synthetic-model", skill_version="1.2.0",
                                  revise=frozenset({U1}), current=current)
        [finding] = self.assertFinds(document, "X-SCOPE", ctx=ctx)
        self.assertEqual(finding.unit, U2, "the shared blank's confidence changes the unit the revision does not name")
        document["groups"][6]["confidence"] = "low"
        result = run(document, ctx=ctx)
        self.assertTrue(result.ok, [f.as_dict() for f in result.findings])
        self.assertEqual(ex.comparable(json.loads(result.payloads[U2])), ex.comparable(current[U2]))

    def test_the_cli_writes_nothing_on_findings(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            inputs = Path(folder) / "inputs"
            for relative, data in files().items():
                (inputs / relative).parent.mkdir(parents=True, exist_ok=True)
                (inputs / relative).write_bytes(data)
            bad = spec()
            del bad["units"][U2]
            for document, expected in ((spec(), 0), (bad, 2)):
                path = Path(folder) / f"{ACC}.spec.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                out = Path(folder) / f"out{expected}"
                import contextlib
                import io
                with contextlib.redirect_stdout(io.StringIO()):
                    code = ex.main(["--spec", str(path), "--inputs", str(inputs), "--decided-at", CTX.decided_at,
                                    "--model", "m", "--skill-version", "1.2.0", "--out", str(out), "--json"])
                self.assertEqual(code, expected)
                self.assertEqual(sorted(p.name for p in out.glob("*.json")) if out.exists() else [],
                                 [f"{U1}.json", f"{U2}.json"] if expected == 0 else [])


class EvalSpecTests(unittest.TestCase):
    """The spec language expresses every eval layout: each hand-written spec expands to payloads that keep no wrong file,
    leave nothing out (an abstained unit apart, which keeps nothing before headers exist) and reach the expected
    pre-download status and verdict."""

    def test_every_eval_accession(self) -> None:
        self.assertEqual(sorted(p.name for p in te.SPECS.glob("*.spec.json")), sorted(f"{a}.spec.json" for a in te.ACCESSIONS))
        for accession in te.ACCESSIONS:
            with self.subTest(accession):
                result = ex.expand(te.spec_path(accession).read_bytes(), ex.inputs_from_files(te.input_files(accession)),
                                   CTX, spec_name=f"{accession}.spec.json")
                self.assertTrue(result.ok, [f.as_dict() for f in result.findings])
                catalog, truth, listing = te.load(accession)
                with tempfile.TemporaryDirectory() as folder:
                    directory = Path(folder)
                    for unit, data in result.payloads.items():
                        (directory / f"{unit}.json").write_bytes(data)
                    report = vt.validate(directory, vt.read_catalog(te.fixture_dir(accession) / "catalog.json"),
                                         vt.read_listing(listing), payload_only=True)
                    verdict, statuses = EXPECTED[accession]
                    self.assertFalse([f.as_dict() for f in report.findings if f.level == "error"])
                    self.assertEqual((report.verdict, report.unit_status), (verdict, statuses))
                    score = vt.score(directory, truth)
                expected = {u["unit_id"]: u["expected_status_pre_download"] for u in truth["units"]}
                for unit, counts in score.items():
                    self.assertEqual(counts["wrong_included"], 0, unit)
                    if expected[unit] == "abstained":
                        self.assertEqual(counts["correct"], 0, unit)
                    else:
                        self.assertEqual(counts["wrongly_left_out"], 0, unit)
                for unit, data in result.payloads.items():
                    doc = json.loads(data)
                    truth_unit = next(u for u in truth["units"] if u["unit_id"] == unit)
                    self.assertEqual(doc["status"], truth_unit["expected_status_pre_download"])
                    self.assertTrue(set(truth_unit["expected_needs_person"]) <= {q["code"] for q in doc["needs_person"]}, unit)
                    self.assertTrue(set(truth_unit["expected_flags"]) <= {f["code"] for f in doc["flags"]}, unit)
                self.assertLess(len(te.spec_path(accession).read_bytes()), 25000, "a spec scales with structure, not rows")


if __name__ == "__main__":
    unittest.main()
