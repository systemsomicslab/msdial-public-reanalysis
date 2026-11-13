"""mncal (WS4/WS7) unit tests: prefilter, strata, estimators, cutoff rule, kernels, shifts, guard.

Every class runs only in the D5 environment (rdkit, matchms, pyarrow, myopic_mces importable); under the
repository's default interpreter they are reported as skipped. mncal is imported inside setUpClass, never at
module level. Fixtures are synthetic; no library content is used.
"""

from __future__ import annotations

import io
import json
import math
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib.util import find_spec
from pathlib import Path

D5 = all(find_spec(m) for m in ("rdkit", "matchms", "pyarrow", "myopic_mces"))
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _import(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import importlib

    return importlib.import_module(name)


def _random_spectrum(rng, precursor=None, n=None):
    import numpy as np

    precursor = float(precursor if precursor is not None else rng.uniform(150, 900))
    n = int(n if n is not None else rng.integers(3, 25))
    mz = np.sort(rng.uniform(40, precursor + 0.04, n))
    inten = rng.uniform(1, 1000, n)
    return precursor, np.column_stack([mz, inten])


@unittest.skipUnless(D5, "D5 env only")
class PrefilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.P = _import("mncal.pairs")

    def test_superset_of_two_matched_peaks_randomised(self):
        import numpy as np

        P = self.P
        rng = np.random.default_rng(7)
        specs = []
        for _ in range(120):
            prec, pk = _random_spectrum(rng)
            specs.append((prec, pk))
        # Plant shared fragments and shared losses so many pairs truly match.
        for i in range(0, 120, 2):
            prec_a, a = specs[i]
            prec_b, b = specs[i + 1]
            shift = prec_b - prec_a
            b[:2, 0] = a[:2, 0] + rng.uniform(-0.024, 0.024, 2)                 # fragment matches
            if a.shape[0] > 3 and b.shape[0] > 3 and a[3, 0] + shift > 0:
                b[3, 0] = a[3, 0] + shift + rng.uniform(-0.02, 0.02)          # loss match
            specs[i + 1] = (prec_b, b[np.argsort(b[:, 0])])
        precs = np.array([s[0] for s in specs])
        mzs = [s[1][:, 0] for s in specs]
        X, Z = P.token_matrices(precs, mzs)
        ub = P.prefilter_upper_bound(X, Z, slice(0, len(specs)), np.arange(len(specs)))
        violations, positives = 0, 0
        for i in range(len(specs)):
            for j in range(i + 1, len(specs)):
                exact = P.max_bipartite_matches(precs[i], mzs[i], precs[j], mzs[j])
                brute = P.brute_force_upper_bound(precs[i], mzs[i], precs[j], mzs[j])
                positives += exact >= 2
                self.assertGreaterEqual(brute, exact)
                if exact >= 2 and ub[i, j] < 2:
                    violations += 1
                self.assertGreaterEqual(ub[i, j], min(exact, 2))
        self.assertEqual(violations, 0)
        self.assertGreater(positives, 30)

    def test_dprec_bin_edges(self):
        P = self.P
        cases = {0.0: "B0", 0.01: "B0", 0.0101: "B1", 2.1: "B1", 2.1001: "B2", 16.1: "B2", 16.11: "B3",
                 50.0: "B3", 50.01: "B4", 100.0: "B4", 100.5: "B5", 200.0: "B5", 200.01: "B6", 400.0: "B6",
                 -16.1: "B2"}
        for delta, expected in cases.items():
            self.assertEqual(P.dprec_bin(delta), expected, delta)
        self.assertIsNone(P.dprec_bin(400.001))

    def test_matched_and_homologue_flags(self):
        import numpy as np

        P = self.P
        self.assertEqual(list(P.matched_bin_index([0, 1, 2, 3, 5, 6, 10, 11])), [-1, -1, 0, 1, 1, 2, 2, 3])
        flags = P.homologue_flag([14.01565, 28.0333, 28.040, 154.172, 0.0], [1, 1, 1, 1, 1])
        self.assertEqual(list(flags), [True, True, False, False, False])
        self.assertFalse(P.homologue_flag([14.01565], [0])[0])

    def test_pair_uniform_is_symmetric_and_seeded(self):
        import numpy as np

        P = self.P
        keys = P.rep_keys(["a", "b", "c"])
        u1 = P.pair_uniform(keys[[0, 1]], keys[[1, 2]], 5)
        u2 = P.pair_uniform(keys[[1, 2]], keys[[0, 1]], 5)
        np.testing.assert_array_equal(u1, u2)
        self.assertFalse(np.array_equal(u1, P.pair_uniform(keys[[0, 1]], keys[[1, 2]], 6)))
        self.assertTrue(((u1 >= 0) & (u1 < 1)).all())


@unittest.skipUnless(D5, "D5 env only")
class RepresentativeTests(unittest.TestCase):
    MSP = """NAME: compound one
PRECURSORMZ: 200.1
PRECURSORTYPE: [M+H]+
IONMODE: Positive
INSTRUMENTTYPE: LC-ESI-QTOF
INCHIKEY: AAAAAAAAAAAAAA-UHFFFAOYSA-N
SMILES: CCO
Num Peaks: 3
50.0 100
60.0 50
70.0 20

NAME: compound one again, more peaks, later ordinal
PRECURSORMZ: 200.1
PRECURSORTYPE: [M+H]+
IONMODE: Positive
INSTRUMENTTYPE: LC-ESI-QTOF
INCHIKEY: AAAAAAAAAAAAAA-UHFFFAOYSA-N
SMILES: CCO
Num Peaks: 4
50.0 100
60.0 50
70.0 20
80.0 10

NAME: compound one tie, same peaks, later ordinal
PRECURSORMZ: 200.1
PRECURSORTYPE: [M+H]+
IONMODE: Positive
INSTRUMENTTYPE: LC-ESI-QTOF
INCHIKEY: AAAAAAAAAAAAAA-UHFFFAOYSA-N
SMILES: CCO
Num Peaks: 4
51.0 100
61.0 50
71.0 20
81.0 10

NAME: compound two too few peaks
PRECURSORMZ: 250.1
PRECURSORTYPE: [M+H]+
IONMODE: Positive
INSTRUMENTTYPE: Orbitrap
INCHIKEY: BBBBBBBBBBBBBB-UHFFFAOYSA-N
SMILES: CCN
Num Peaks: 3
50.0 100
240.0 50
250.1 20

NAME: compound three dimer
PRECURSORMZ: 301.1
PRECURSORTYPE: [2M+H]+
IONMODE: Positive
INCHIKEY: CCCCCCCCCCCCCC-UHFFFAOYSA-N
SMILES: CCC
Num Peaks: 3
50.0 100
60.0 50
70.0 20
"""

    @classmethod
    def setUpClass(cls):
        if find_spec("msdial_spectrum_catalog") is None:
            raise unittest.SkipTest("msdial_spectrum_catalog pin not on PYTHONPATH")
        cls.L = _import("mncal.library")
        from msdial_spectrum_catalog.reference_library import ingest_reference_library

        cls.tmp = tempfile.TemporaryDirectory()
        msp = Path(cls.tmp.name) / "synthetic.msp"
        msp.write_text(cls.MSP, encoding="utf-8")
        cls.db = Path(cls.tmp.name) / "ref.sqlite"
        report = ingest_reference_library(cls.db, msp, library_name="synthetic", consensus=False)
        assert report.valid, report.errors

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_selection_is_deterministic_and_ties_take_lowest_ordinal(self):
        reps1, report, _ = self.L.select_representatives(self.db)
        reps2, _, _ = self.L.select_representatives(self.db)
        self.assertEqual(self.L.selection_digest(reps1), self.L.selection_digest(reps2))
        self.assertEqual(len(reps1), 1)
        self.assertEqual(reps1[0]["record_index"], 1)          # 4 peaks beat 3; ordinal 1 beats ordinal 2
        self.assertEqual(report.excluded.get("fewer_than_3_peaks_after_prep"), 1)
        self.assertEqual(report.excluded.get("adduct_out_of_scope"), 1)


@unittest.skipUnless(D5, "D5 env only")
class MetricTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.M = _import("mncal.metrics")

    def test_wilson_known_values(self):
        low, high = self.M.wilson(8, 10)
        self.assertAlmostEqual(low, 0.4902, places=3)
        self.assertAlmostEqual(high, 0.9433, places=3)
        self.assertTrue(all(math.isnan(v) for v in self.M.wilson(0, 0)))

    def test_ht_estimator_is_unbiased_under_stratified_srs(self):
        import numpy as np

        rng = np.random.default_rng(3)
        strata = [rng.random(2000) < 0.05, rng.random(300) < 0.4, rng.random(50) < 0.9]
        truth = sum(s.sum() for s in strata)
        estimates = []
        for _ in range(3000):
            total = 0.0
            for s, n in zip(strata, (100, 60, 50)):
                n = min(n, s.shape[0])
                idx = rng.choice(s.shape[0], n, replace=False)
                total += self.M.ht_total(s[idx], np.full(n, s.shape[0] / n))
            estimates.append(total)
        self.assertAlmostEqual(float(np.mean(estimates)) / truth, 1.0, delta=0.01)

    def test_pava_is_monotone_least_squares(self):
        import numpy as np

        fit = self.M.pava(np.array([0.1, 0.5, 0.3, 0.2, 0.8, 0.7]))
        np.testing.assert_allclose(fit, [0.1, 1 / 3, 1 / 3, 1 / 3, 0.75, 0.75])
        self.assertTrue(np.all(np.diff(fit) >= 0))

    def test_cutoff_not_estimable_and_estimable(self):
        import numpy as np

        M = self.M
        rng = np.random.default_rng(0)
        s = rng.random(400)
        y = (s > 0.2).astype(float)            # precise at every t above 0.2
        c = M.curve(s, y, np.ones_like(s))
        idx = M.select_cutoff(c, 0.9)
        self.assertIsNotNone(idx)
        self.assertGreaterEqual(c["low"][idx], 0.9)                 # the rule holds at t*
        self.assertTrue(idx == 0 or c["low"][idx - 1] < 0.9)        # and t* is the smallest such t
        few = M.curve(s[:20], y[:20], np.ones(20))
        self.assertIsNone(M.select_cutoff(few, 0.5))          # n(s >= t) < 30 everywhere
        noise = M.curve(s, (rng.random(400) < 0.3).astype(float), np.ones_like(s))
        self.assertIsNone(M.select_cutoff(noise, 0.9))

    def test_bootstrap_interval_contains_estimate(self):
        import numpy as np

        rng = np.random.default_rng(1)
        n = 3000
        s = rng.random(n)
        y = (rng.random(n) < s).astype(float)
        blocks_a = rng.integers(0, 300, n).astype(str)
        blocks_b = rng.integers(0, 300, n).astype(str)
        (plo, phi), (rlo, rhi) = self.M.bootstrap_at(s, y, np.ones(n), 0.7, blocks_a, blocks_b, B=200, seed=1)
        above = s >= 0.7
        p = y[above].mean()
        self.assertLess(plo, p)
        self.assertGreater(phi, p)
        r = y[above].sum() / y.sum()
        self.assertLess(rlo, r)
        self.assertGreater(rhi, r)

    def test_class_balanced_weights_equalise_classes(self):
        import numpy as np

        w = self.M.class_balance_weights(np.array(["A", "A", "A", "B"]), np.array(["A", "A", "A", "B"]))
        total_a = w[:3].sum()
        total_b = w[3]
        self.assertAlmostEqual(total_a, total_b)


@unittest.skipUnless(D5, "D5 env only")
class KernelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.K = _import("mncal.kernels")

    def test_matchms_direct_path_equals_matchms_pair(self):
        import numpy as np
        from matchms.similarity import ModifiedCosineGreedy

        K = self.K
        ref = ModifiedCosineGreedy(tolerance=0.025, mz_power=0.0, intensity_power=1.0)
        rng = np.random.default_rng(11)
        for _ in range(300):
            pa, a = _random_spectrum(rng)
            pb, b = _random_spectrum(rng, precursor=pa + rng.choice([0.0, 0.01, 14.01565, 15.9949, 120.3]))
            b[: min(3, b.shape[0]), 0] = a[: min(3, b.shape[0]), 0]
            b = b[np.argsort(b[:, 0])]
            sa, sb = K.matchms_spectrum(pa, a), K.matchms_spectrum(pb, b)
            if len(sa.peaks) == 0 or len(sb.peaks) == 0:
                continue
            want = ref.pair(sa, sb)
            got = K.matchms_pair_arrays(pa, K._matchms_filter(pa, a), pb, K._matchms_filter(pb, b))
            self.assertEqual(got[0], float(want["score"]))
            self.assertEqual(got[1], int(want["matches"]))

    def test_entropy_port_equals_catalog_function(self):
        if find_spec("msdial_spectrum_catalog") is None:
            self.skipTest("msdial_spectrum_catalog pin not on PYTHONPATH")
        import numpy as np
        from msdial_spectrum_catalog.similarity import entropy_similarity

        K = self.K
        rng = np.random.default_rng(5)
        for _ in range(500):
            pa, a = _random_spectrum(rng)
            pb, b = _random_spectrum(rng)
            b[:2, 0] = a[:2, 0] + rng.uniform(-0.03, 0.03, 2)
            b = b[np.argsort(b[:, 0])]
            want = entropy_similarity([tuple(p) for p in a], [tuple(p) for p in b], 0.05)
            got = K.entropy_port_score(K.entropy_port_prepare(pa, a), K.entropy_port_prepare(pb, b))
            self.assertLessEqual(abs(got - want), 1e-9)

    def test_identity_strings(self):
        K = self.K
        self.assertEqual(
            K.PRIMARY,
            "gnps.modified_cosine?assign=greedy&ipow=1&mzpow=0&prep=mn&prep.above=0.025&prep.abs=0"
            "&prep.frame=0&prep.maxmz=10000&prep.minmz=0&prep.pow=1&prep.precwin=17&prep.rel=1"
            "&prep.scale=100&prep.topk=0&prep.topkwin=50&tol=0.025")
        for variant in (K.SENS_POW05, K.SENS_TOL002, K.SENS_OPTIMAL):
            self.assertNotEqual(variant, K.PRIMARY)
        self.assertIn("prep.above=0.02&", K.SENS_TOL002)
        self.assertTrue(K.matchms_identity().startswith("matchms.modified_cosine?"))


@unittest.skipUnless(D5, "D5 env only")
class ShiftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.S = _import("mncal.shifts")
        cls.rules = cls.S.load_rules()

    def test_rule_masses_match_formulas(self):
        for rule in self.rules:
            mass = self.S.formula_mass(dict(rule.composition))
            self.assertAlmostEqual(mass, rule.mass, delta=2e-4, msg=rule.label)

    def test_single_and_sum_labels(self):
        S = self.S
        self.assertEqual(S.label_shift(316.0, 300.00509, self.rules).shift_label, "+O")
        self.assertEqual(S.label_shift(300.0, 315.99491, self.rules).shift_label, "-O")
        homolog = S.label_shift(328.0313, 300.0, self.rules)
        self.assertEqual(homolog.shift_label, "+C2H4")          # +C2H4, +2CH2, +CH2+CH2 are one composition
        self.assertEqual(homolog.label_ambiguous, 0)
        self.assertEqual(S.label_shift(300.0, 300.004, self.rules).shift_label, "isomer")
        self.assertEqual(S.label_shift(337.1234, 300.0, self.rules).shift_label, "uninterpreted")

    def test_so3_hpo3_ambiguity(self):
        label = self.S.label_shift(379.9616, 300.0, self.rules)    # SO3 79.95682 vs HPO3 79.96633
        self.assertEqual(label.shift_label, "ambiguous")
        self.assertEqual(label.label_ambiguous, 1)
        self.assertIn("+SO3", label.fitting_labels)
        self.assertIn("+HPO3", label.fitting_labels)
        self.assertEqual(json.loads(label.fitting_labels_json), sorted(label.fitting_labels))


@unittest.skipUnless(D5, "D5 env only")
class GuardTests(unittest.TestCase):
    FAKE_KEY = "ABCDEFGHIJKLMN-OPQRSTUVWX-N"

    @classmethod
    def setUpClass(cls):
        cls.G = _import("mncal.guard")
        cls.cli = _import("mncal.cli")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        names = cls.root / "names.msp"
        names.write_text("NAME: Synthetic testoside A\nNum Peaks: 0\n\nNAME: Other compound B\n", encoding="utf-8")
        cls.names = str(names)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _run(self, *argv) -> int:
        with redirect_stdout(io.StringIO()):
            return self.cli.main(["guard", *argv])

    def _write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_strict_flags_inchikey_smiles_path_name_and_small_cells(self):
        clean = self._write("clean.md", "# Report\n\n| bin | n_pairs |\n|---|---|\n| B0 | 120 |\n\n"
                            "kernel gnps.modified_cosine?assign=greedy&tol=0.025 adduct [M+CH3COO]-\n")
        self.assertEqual(self._run(clean, "--vs21-msp", self.names), 0)
        for text in (f"key {self.FAKE_KEY}\n", "smiles CC(=O)Oc1ccccc1C(=O)O\n", "at D:\\data\\x.msp\n",
                     "neighbour Synthetic testoside A here\n", "| bin | n_pairs |\n|---|---|\n| B0 | 7 |\n"):
            self.assertEqual(self._run(self._write("dirty.md", text), "--vs21-msp", self.names), 1, text)

    def test_strict_requires_name_source_or_explicit_skip(self):
        clean = self._write("clean2.md", "nothing\n")
        self.assertEqual(self._run(clean), 2)
        self.assertEqual(self._run(clean, "--no-names"), 0)

    def test_small_count_cells_in_tsv_and_json(self):
        tsv = self._write("cutoff_table.tsv", "kernel\tn_pairs_above\nk\t5\n")
        self.assertEqual(self._run(tsv, "--no-names"), 1)
        ok = self._write("ok.tsv", "kernel\tn_pairs_above\nk\t<10\n")
        self.assertEqual(self._run(ok, "--no-names"), 0)
        js = self._write("run.json", json.dumps({"steps": {"x": {"n_pairs": 3}}}))
        self.assertEqual(self._run(js, "--no-names"), 1)

    def test_identity_mode_requires_citation_and_edge_membership(self):
        import sqlite3

        overlay = self.root / "overlay.local-vs21.sqlite"
        con = sqlite3.connect(overlay)
        con.execute("CREATE TABLE mn_edge_annotation (neighbour_name TEXT, neighbour_inchikey TEXT)")
        con.execute("INSERT INTO mn_edge_annotation VALUES ('Synthetic testoside A', ?)", (self.FAKE_KEY,))
        con.commit()
        con.close()
        sha = self.G.VS21_FILES["MSMS-Private-pos-VS21.msp"]
        cite = f"Neighbours cite MSMS-Private-pos-VS21.msp (sha256 {sha}).\n"
        args = ["--allow-neighbour-identity", "--overlay", str(overlay), "--vs21-msp", self.names]
        with_cite = self._write("agg.md", cite + f"| Synthetic testoside A | {self.FAKE_KEY} |\n")
        self.assertEqual(self._run(with_cite, *args), 0)
        no_cite = self._write("agg2.md", f"| Synthetic testoside A | {self.FAKE_KEY} |\n")
        self.assertEqual(self._run(no_cite, *args), 1)
        stranger = self._write("agg3.md", cite + "| x | ZYXWVUTSRQPONM-LKJIHGFEDC-N |\n")
        self.assertEqual(self._run(stranger, *args), 1)
        other_name = self._write("agg4.md", cite + "Other compound B\n")
        self.assertEqual(self._run(other_name, *args), 1)
        smiles = self._write("agg5.md", cite + "CC(=O)Oc1ccccc1C(=O)O\n")
        self.assertEqual(self._run(smiles, *args), 1)


@unittest.skipUnless(D5, "D5 env only")
class OverlayDdlTests(unittest.TestCase):
    EDGE = ["similarity_id", "query_spectrum_id", "reference_spectrum_id", "neighbour_name", "neighbour_inchikey",
            "neighbour_library_id", "ion_mode", "query_adduct", "adduct_assumed", "query_precursor_mz",
            "neighbour_precursor_mz", "delta_mz", "dprec_bin", "shift_label", "label_ambiguous",
            "fitting_labels_json", "shift_rules_sha256", "same_compound_as_sl_hit", "edge_rank", "implied_formula"]
    PROPOSAL = ["proposal_id", "similarity_id", "spectrum_id", "method", "score_convention", "score",
                "matched_peak_count", "ion_mode", "dprec_bin", "min_matched", "cutoff_benchmark_id",
                "cutoff_table_sha256", "truth_definition", "bias_view", "target_precision", "cutoff_estimable",
                "threshold", "precision_ci_low", "evidence_value_json", "source_reference_spectrum_id",
                "source_tool_run_id", "passed"]

    def test_documented_ddl_creates_the_mn_overlay_v1_tables_on_the_catalog_schema(self):
        if find_spec("msdial_spectrum_catalog") is None:
            self.skipTest("msdial_spectrum_catalog pin not on PYTHONPATH")
        import re
        import sqlite3

        from msdial_spectrum_catalog.storage import initialize

        doc = (Path(__file__).resolve().parents[1] / "docs" / "mn-calibration.md").read_text(encoding="utf-8")
        ddl = re.search(r"```sql\n(.*?)```", doc, re.S).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "overlay.local-vs21.sqlite"
            initialize(db)
            con = sqlite3.connect(db)
            try:
                con.executescript(ddl)
                edge = [r[1] for r in con.execute("PRAGMA table_info(mn_edge_annotation)")]
                proposal = [r[1] for r in con.execute("PRAGMA table_info(mn_evidence_proposal)")]
            finally:
                con.close()
        self.assertEqual(edge, self.EDGE)
        self.assertEqual(proposal, self.PROPOSAL)


if __name__ == "__main__":
    unittest.main()
