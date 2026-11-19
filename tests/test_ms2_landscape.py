"""Regression checks for the read-only meeting report parser."""

from __future__ import annotations

import sys
import tempfile
import unittest
import json
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from ms2_landscape import (candidate_tier, choose_spectrum, completed_units, compound_class, raw_group,
                           read_msp, spectrum_vector)  # noqa: E402


class LandscapeTests(unittest.TestCase):
    def test_multiple_campaigns_assign_study_type_and_reject_duplicate_done_unit(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            names = ("stage1-mbpost-20261028", "stage1b-hilic-20261028")
            for name, unit_id in zip(names, ("lipid-unit", "hilic-unit")):
                campaign = root / "_campaigns" / name
                campaign.mkdir(parents=True)
                (campaign / "campaign-manifest.json").write_text(
                    json.dumps({"units": [{"unit_id": unit_id}]}), encoding="utf-8")
                conn = sqlite3.connect(campaign / "ledger.sqlite")
                try:
                    conn.execute("CREATE TABLE transition (seq INTEGER, unit_key TEXT, to_state TEXT)")
                    conn.execute("INSERT INTO transition VALUES (1, ?, 'done')", (unit_id,))
                    conn.commit()
                finally:
                    conn.close()
            found = list(completed_units(root, list(names)))
            self.assertEqual([(name, study, unit["unit_id"]) for name, study, unit in found],
                             [(names[0], "lipidomics", "lipid-unit"),
                              (names[1], "hydrophilic", "hilic-unit")])
            second = root / "_campaigns" / names[1]
            (second / "campaign-manifest.json").write_text(
                json.dumps({"units": [{"unit_id": "lipid-unit"}]}), encoding="utf-8")
            conn = sqlite3.connect(second / "ledger.sqlite")
            try:
                conn.execute("INSERT INTO transition VALUES (2, 'lipid-unit', 'done')")
                conn.commit()
            finally:
                conn.close()
            with self.assertRaisesRegex(ValueError, "more than one campaign"):
                list(completed_units(root, list(names)))

    def test_embedding_peak_threshold_can_include_one_peak_spectrum(self):
        record = (7, 1, 10, [(100.0, 20.0)], "", "", 200.0, 1)
        heap = []
        choose_spectrum(heap, "campaign", "lipidomics", "unit", record,
                        "unassigned", "unclassified", 10, 3)
        self.assertFalse(heap)
        choose_spectrum(heap, "campaign", "lipidomics", "unit", record,
                        "unassigned", "unclassified", 10, 1)
        self.assertEqual(heap[0][2].spot, 7)

    def test_tiers_require_actual_msms_evidence(self):
        mapping = {"msp_high_quality": "VS21 strict", "msp_in_silico": "GLACIER in-silico"}
        row = {"annotator_id": "msp_high_quality", "annotation_tag_vs1": "530",
               "evidence_source": "PrecursorOnly"}
        self.assertEqual(candidate_tier(row, mapping), "unassigned")
        row.update(annotation_tag_vs1="430", evidence_source="ReferenceSpectrum")
        self.assertEqual(candidate_tier(row, mapping), "VS21 strict")
        row.update(annotator_id="LbmDB: test", evidence_source="RuleBased",
                   annotation_tag_vs1="410")
        self.assertEqual(candidate_tier(row, mapping), "LBM2")
        row.update(annotator_id="msp_in_silico", evidence_source="InSilico",
                   annotation_tag_vs1="450")
        self.assertEqual(candidate_tier(row, mapping), "GLACIER in-silico")

    def test_raw_group_includes_vendor_folder_and_wiff_sidecar(self):
        self.assertEqual(raw_group("outer/sample.d/AcqData/a.bin"), "outer/sample.d")
        self.assertEqual(raw_group("outer/sample.wiff.scan"), "outer/sample.wiff")
        self.assertIsNone(raw_group("outer/readme.txt"))

    def test_only_lipid_rules_infer_class_from_name(self):
        self.assertEqual(compound_class("LBM2", "null", "PC(16:0/18:1)"), "PC")
        self.assertEqual(compound_class("VS21 strict", "null", "MassSpecGymID0001"),
                         "unclassified")

    def test_msp_counts_record_bytes_and_peak_ids(self):
        content = (b"NAME: A\nPRECURSORMZ: 150.3\nPRECURSORTYPE: [M+H]+\n"
                   b"COMMENT: |PEAKID=7|\nNum Peaks: 2\n"
                   b"100.1 20\n101.1 30\n\n"
                   b"NAME: B\nPRECURSORMZ: 300.1\nPRECURSORTYPE: [M+2H]2+\n"
                   b"COMMENT: |PEAKID=8|\nNum Peaks: 0\n")
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / "test.mdmsp"
            path.write_bytes(content)
            records = list(read_msp(path))
        self.assertEqual([record[:2] for record in records], [(7, 2), (8, 0)])
        self.assertEqual(records[0][2] + records[1][2], len(content))
        self.assertEqual(records[0][3], [(100.1, 20.0), (101.1, 30.0)])
        self.assertEqual(records[0][6], 150.3)
        self.assertEqual(records[0][7], 1)
        self.assertEqual(records[1][7], 2)

    def test_fragment_and_loss_bins_are_distinct_and_normalized(self):
        vector = spectrum_vector([(100.2, 9), (100.3, 16), (150.1, 25),
                                  (220.0, 0)], 200.2)
        self.assertEqual(set(vector), {99, 149, 2049, 2099})
        self.assertAlmostEqual(sum(value * value for value in vector.values()), 1)
        self.assertAlmostEqual(vector[99], vector[2099])

    def test_missing_precursor_uses_fragments_only(self):
        vector = spectrum_vector([(1.49, 4), (1.50, 9), (2001, 100)], None)
        self.assertEqual(set(vector), {0, 1})
        self.assertAlmostEqual(sum(value * value for value in vector.values()), 1)

    def test_neutral_losses_exclude_nonpositive_and_out_of_range(self):
        vector = spectrum_vector([(100, 10), (200, 10), (250, 10)], 200)
        self.assertEqual(set(vector), {99, 199, 249, 2099})


if __name__ == "__main__":
    unittest.main()
