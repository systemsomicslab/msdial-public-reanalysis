"""scripts/triage-digest.py: the facts a triage reads, compact, built by code from the inputs alone.

docs/llm-assisted-curation.md section 2.2. Checked: the same inputs give the same bytes (in any file order); the twin
groups are validate-triage.py's T-DUP grouping; the mwTab method lines (MS_COMMENTS) and the factors the Catalog does
not ingest are shown; no absolute path leaves the inputs; and nothing but the inputs it names changes it. Synthetic
fixtures and the eval set.
"""

from __future__ import annotations

import importlib.util
import json
import re
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


dg = _load("triage_digest_under_test", ROOT / "scripts" / "triage-digest.py")
vt = dg.vt

MWTAB = """#METABOLOMICS WORKBENCH synthetic
SUBJECT_SAMPLE_FACTORS           \t-\tS01_pos\tMode:HILIC Positive | Ratio:1;10\tMS_mode=MS1; RAW_FILE_NAME=S01_pos.mzML
SUBJECT_SAMPLE_FACTORS           \t-\tS02_neg\tMode:RP Negative | Ratio:1;10\tMS_mode=MS1; RAW_FILE_NAME=S02_neg.mzML
CH:CHROMATOGRAPHY_TYPE           \tHILIC
CH:COLUMN_NAME                   \tSynthetic Amide (100 x 2.1mm)
AN:ANALYSIS_TYPE                 \tMS
MS:ION_MODE                      \tPOSITIVE
MS:MS_COMMENTS                   \tFull scan for the samples; pooled QC by
MS:MS_COMMENTS                   \tinclusion-list DDA.
SP:SAMPLEPREP_SUMMARY            \t""" + "x" * 700 + """
MS_METABOLITE_DATA_START
Samples\tS01_pos
MS_METABOLITE_DATA_END
"""


def synthetic_files() -> dict[str, bytes]:
    rows = [{"sample_id": "S01_pos", "raw_file": "S01_pos.mzML", "attributes": {"Mode": "HILIC Positive", "Ratio": "1", "Batch": "b1", "id": "SA1"}},
            {"sample_id": "S02_neg", "raw_file": "S02_neg.mzML", "attributes": {"Mode": "RP Negative", "Ratio": "1", "Batch": "b1", "id": "SA2"}},
            {"sample_id": "S03_neg", "raw_file": "S03_neg.mzML", "attributes": {"Mode": "RP Negative", "Ratio": "1", "Batch": "b1", "id": "SA3"}},
            {"sample_id": "S04_x", "raw_file": "S04_x.mzML", "attributes": {"Mode": "RP Negative", "Ratio": "1", "Batch": "b1", "id": "SA4"}}]
    files: dict[str, bytes] = {}
    for unit, chrom in (("AN000001", "HILIC"), ("AN000002", "Reversed phase")):
        handoff = {"accession": "ST000001", "analysis_unit_id": unit, "source_subrecord_id": unit,
                   "sample_table_path": "C:\\Users\\someone\\catalog\\samples.json",
                   "technical_settings": {"chromatography": chrom, "ion_mode": "Both", "acquisition_mode": "DDA"}}
        files[f"{unit}/handoff.json"] = json.dumps(handoff).encode("utf-8")
        files[f"{unit}/samples.json"] = json.dumps(rows).encode("utf-8")
    files["AN000001.mwtab.txt"] = MWTAB.encode("utf-8")
    listing = ["archive\tpath\tsize\tcrc32",
               "ST000001.zip\tHILIC/S01_pos.mzML\t2000000\tAAAA0001",
               "ST000001.zip\tRP/S02_neg.mzML\t2000001\tAAAA0002",
               "ST000001.zip\tCOPY/S02_neg.mzML\t2000001\tAAAA0002",
               "ST000001.zip\tRP/S03_neg.mzML\t2000002\tAAAA0003",
               "ST000001.zip\tOTHER/S03_neg.mzML\t2000003\tAAAA0004",
               "ST000001.zip\tRP/notes.xlsx\t2000\tAAAA0005",
               "ST000001.zip\t__MACOSX/RP/._S02_neg.mzML\t4096\tAAAA0006"]
    files["ST000001.members.tsv"] = ("\n".join(listing) + "\n").encode("utf-8")
    return files


class DigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = dg.build_digest_from_files(synthetic_files())
        self.text = self.data.decode("utf-8")

    def test_deterministic_in_any_file_order(self) -> None:
        self.assertEqual(dg.build_digest_from_files(synthetic_files()), self.data)
        self.assertEqual(dg.build_digest_from_files(dict(reversed(list(synthetic_files().items())))), self.data)

    def test_rows_tokens_resolution_and_factors(self) -> None:
        self.assertIn("Row tables identical (sample_id and raw_file, in order): AN000001, AN000002.", self.text)
        self.assertIn("Catalog attributes with one value in every row: Batch=b1; Ratio=1.", self.text)
        self.assertIn("Catalog attributes unique to every row (identifiers, not shown): id.", self.text)
        row = next(line for line in self.text.splitlines() if line.startswith("S01_pos |"))
        self.assertIn("Mode=HILIC Positive", row)
        self.assertIn("mw Ratio=1;10; MS_mode=MS1", row, "the Catalog's Ratio is cut at ';' and it lacks MS_mode")
        self.assertIn("S:AN000001", row)
        self.assertIn("pos/HILIC/-", row)
        self.assertIn("exact HILIC/S01_pos.mzML", row)
        ambiguous = next(line for line in self.text.splitlines() if line.startswith("S03_neg |"))
        self.assertIn("ambiguous:2 OTHER/S03_neg.mzML ; RP/S03_neg.mzML", ambiguous)
        missing = next(line for line in self.text.splitlines() if line.startswith("S04_x |"))
        self.assertTrue(missing.endswith("none"))
        self.assertIn("Rows that resolve to no member (1): S04_x (S04_x.mzML).", self.text)

    def test_mwtab_extract(self) -> None:
        self.assertIn("MS:MS_COMMENTS\tFull scan for the samples; pooled QC by inclusion-list DDA.", self.text)
        self.assertIn("CH:CHROMATOGRAPHY_TYPE\tHILIC", self.text)
        self.assertRegex(self.text, r"SP:SAMPLEPREP_SUMMARY\tx{600} \[cut; AN000001\.mwtab\.txt line \d+\]")
        self.assertIn("### AN000001 (Samples line: 1 name(s))", self.text)

    def test_twin_groups_are_the_validators(self) -> None:
        listed = dg._listing(synthetic_files()["ST000001.members.tsv"])  # noqa: SLF001
        twins = vt.content_twins(listed)
        self.assertEqual(set(twins), {"copy/s02_neg.mzml", "rp/s02_neg.mzml"})
        self.assertIn("### COPY/ and RP/: 1 pair(s), 1 with the same file name", self.text)
        self.assertIn("T1: S02_neg.mzML = S02_neg.mzML", self.text)
        self.assertIn("S02_neg.mzML [twin:T1]", self.text, "two members of that name: no row resolves to either")
        self.assertIn("S01_pos.mzML [row]", self.text)
        for accession in te.ACCESSIONS:
            with self.subTest(accession):
                files = te.input_files(accession)
                text = dg.build_digest_from_files(files).decode("utf-8")
                listed = dg._listing(files[f"{accession}.members.tsv"])  # noqa: SLF001
                twins = vt.content_twins(listed)
                marked = set(re.findall(r"\[twin:(T\d+)\]", text))
                groups = {frozenset({k} | v) for k, v in twins.items()}
                self.assertEqual(len(marked), len(groups))

    def test_metadata_and_non_raw_members(self) -> None:
        self.assertIn("Archive metadata (__MACOSX/, ._*): 1 member(s), not listed below.", self.text)
        self.assertIn("notes.xlsx [nonraw]", self.text)
        self.assertNotIn("._S02_neg", self.text)

    def test_no_absolute_path_and_only_the_inputs_count(self) -> None:
        self.assertNotIn("C:\\", self.text)
        self.assertNotIn("Users", self.text)
        with tempfile.TemporaryDirectory() as folder:
            inputs = Path(folder) / "inputs"
            for relative, data in synthetic_files().items():
                (inputs / relative).parent.mkdir(parents=True, exist_ok=True)
                (inputs / relative).write_bytes(data)
            self.assertEqual(dg.build_digest(inputs), self.data)
            # Prompts, stored tool results, revision folders and the digest itself are not inputs of the digest.
            (inputs / "AN000001" / "prompt.md").write_text("prompt", encoding="utf-8")
            (inputs / "AN000001" / "calls").mkdir()
            (inputs / "AN000001" / "calls" / "1.json").write_text("{}", encoding="utf-8")
            (inputs / "AN000001" / "r1").mkdir()
            (inputs / "AN000001" / "r1" / "previous.json").write_text("{}", encoding="utf-8")
            (inputs / "digest.md").write_bytes(self.data)
            self.assertEqual(dg.build_digest(inputs), self.data)

    def test_small_members_and_nested_archives_are_marked(self) -> None:
        # Review of the token diet (2026-11-18): a 3 kB injection among 500 MB files, and raw data shipped in an inner
        # zip, reach the agent as evidence, not as a folder's size range and a [nonraw] line.
        files = synthetic_files()
        extra = ["ST000001.zip\tPOS/A_pos.raw\t524288000\tBBBB0001", "ST000001.zip\tPOS/B_pos.raw\t524288001\tBBBB0002",
                 "ST000001.zip\tPOS/C_pos.raw\t524288002\tBBBB0003", "ST000001.zip\tPOS/D_pos.raw\t3072\tBBBB0004",
                 "ST000001.zip\tNEG.zip\t104857600\tBBBB0005"]
        files["ST000001.members.tsv"] += ("\n".join(extra) + "\n").encode("utf-8")
        text = dg.build_digest_from_files(files).decode("utf-8")
        self.assertIn("D_pos.raw [small:3.0 kB]", text)
        self.assertIn("A_pos.raw\n", text)
        self.assertIn("NEG.zip [archive]", text)
        self.assertNotIn("NEG.zip [nonraw]", text)
        self.assertIn("notes.xlsx [nonraw]", text)

    def test_acquisition_tokens_start_a_word(self) -> None:
        self.assertEqual(vt.token_acquisition("RP_POS/SpentMedia_03_pos.raw"), ())
        self.assertEqual(vt.token_acquisition("media1_pos.raw"), ())
        self.assertEqual(vt.token_acquisition("Claudia_Comedia_Encyclopedia.raw"), ())
        self.assertEqual(vt.token_acquisition("Media_Blank_DDA_pos.raw"), ("DDA",))
        self.assertEqual(vt.token_acquisition("S01_posDDA.raw"), ("DDA",))
        self.assertEqual(vt.token_acquisition("QC_tddMS2_pos.raw"), ("tDDA",))
        self.assertEqual(vt.token_acquisition("QC_ddMS2_pos.raw"), ("DDA",))
        self.assertEqual(vt.token_acquisition("Pool_HERMES.raw"), ("tDDA",))
        for name, token in (("S01_PRM.raw", "PRM"), ("S01_tMS2.raw", "tMS2"), ("S01_SRM.raw", "SRM"), ("S01_MRM.raw", "MRM"),
                            ("S01_SIM_pos.raw", "SIM")):
            self.assertEqual(vt.token_acquisition(name), (token,), name)
            self.assertIn(token, vt.TARGETED_TOKENS)
        self.assertEqual(vt.token_acquisition("Simvastatin_DIA.raw"), ("DIA",))

    def test_the_cli(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            inputs = Path(folder) / "inputs"
            for relative, data in synthetic_files().items():
                (inputs / relative).parent.mkdir(parents=True, exist_ok=True)
                (inputs / relative).write_bytes(data)
            out = Path(folder) / "digest.md"
            self.assertEqual(dg.main(["--inputs", str(inputs), "--out", str(out)]), 0)
            self.assertEqual(out.read_bytes(), self.data)


if __name__ == "__main__":
    unittest.main()
