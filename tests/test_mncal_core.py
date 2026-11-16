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
class CalibrateEquivalenceTests(unittest.TestCase):
    """The one-pass calibrate path against the frozen per-threshold code it replaced (c2ea815)."""

    @classmethod
    def setUpClass(cls):
        tests_dir = str(Path(__file__).resolve().parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        cls.M = _import("mncal.metrics")
        cls.CAL = _import("mncal.calibrate")
        import importlib

        cls.REF_M = importlib.import_module("mncal_metrics_reference")
        cls.REF_CAL = importlib.import_module("mncal_calibrate_reference")

    @staticmethod
    def _population(seed, n=4000, blocks=400, classes=12):
        import numpy as np

        rng = np.random.default_rng(seed)
        s = rng.random(n)
        s[::7] = np.round(s[::7], 2)                         # scores exactly on grid points
        s[::53] = np.nan
        y = (rng.random(n) < np.nan_to_num(s, nan=0.2) ** 2).astype(float)
        w = rng.choice([1.0, 1.0, 1.0, 3.5, 100.0], n)
        a = rng.integers(0, blocks, n)
        b = (a + 1 + rng.integers(0, blocks - 1, n)) % blocks
        block_a = np.array([f"BLK{v:05d}" for v in a])
        block_b = np.array([f"BLK{v:05d}" for v in b])
        parent = np.array([f"class{v % classes}" if v % 17 else "unknown" for v in range(blocks)])
        return s, y, w, block_a, block_b, parent[a], parent[b]

    def _close(self, x, y, tol=1e-9):
        import numpy as np

        x, y = np.asarray(x, float), np.asarray(y, float)
        self.assertEqual(x.shape, y.shape)
        both_nan = np.isnan(x) & np.isnan(y)
        self.assertTrue(np.all(both_nan | (np.abs(x - y) <= tol * np.maximum(1.0, np.abs(y)))),
                        f"max diff {np.nanmax(np.abs(x - y))}")

    def test_curve_matches_reference_plain_and_class_balanced(self):
        for seed in (0, 1):
            s, y, w, _, _, pa, pb = self._population(seed)
            for classes in (None, (pa, pb)):
                new = self.M.curve(s, y, w, classes=classes)
                ref = self.REF_M.curve(s, y, w, classes=classes)
                self.assertEqual(list(new["n_above"]), list(ref["n_above"]))
                for key in ("precision", "low", "high", "recall", "n_eff"):
                    self._close(new[key], ref[key])

    def test_bootstrap_matches_reference_at_every_threshold_above_the_start(self):
        import numpy as np

        s, y, w, ba, bb, pa, pb = self._population(2, n=2500, blocks=250)
        # The plain view only: the class-balanced view changed on purpose (fixed class set, see
        # ClassBalancedBootstrapTests).
        for classes in (None,):
            i0 = 40
            new = self.M.bootstrap_curve(s, y, w, i0, ba, bb, classes=classes, B=70, seed=3, interval="percentile")
            for i in (40, 41, 55, 70, 90, 100):
                (plo, phi), (rlo, rhi) = self.REF_M.bootstrap_at(s, y, w, float(self.M.GRID[i]), ba, bb,
                                                                 classes=classes, B=70, seed=3)
                self._close([new["precision_low"][i], new["precision_high"][i], new["recall_low"][i],
                             new["recall_high"][i]], [plo, phi, rlo, rhi])
            self.assertTrue(np.all(np.isnan(new["precision_low"][:i0])))

    def test_integer_string_rank_codes_draw_the_same_replicates_as_strings(self):
        s, y, w, ba, bb, pa, pb = self._population(4, n=2000, blocks=150)
        ca, cb, _ = self.CAL.block_codes(ba, bb)
        by_codes = self.M.bootstrap_curve(s, y, w, 30, ca, cb, classes=(pa, pb), B=40, seed=5)
        by_strings = self.M.bootstrap_curve(s, y, w, 30, ba, bb, classes=(pa, pb), B=40, seed=5)
        for key in by_codes:
            self._close(by_codes[key], by_strings[key], tol=0)

    def test_build_table_matches_reference(self):
        import numpy as np
        import pandas as pd

        rng = np.random.default_rng(7)
        n, blocks = 6000, 300
        names = np.array([f"BLK{v:05d}" for v in range(blocks)])
        a = rng.integers(0, blocks, n)
        b = (a + 1 + rng.integers(0, blocks - 1, n)) % blocks
        tan = rng.random(n)
        s1 = np.clip(tan + rng.normal(0, 0.25, n), 0, 1)
        s1[::9] = np.round(s1[::9], 2)
        s2 = np.clip(tan + rng.normal(0, 0.35, n), 0, 1)
        categories = pd.Index(names)
        kept = pd.DataFrame({
            "block_a": pd.Categorical(names[a], categories=categories),
            "block_b": pd.Categorical(names[b], categories=categories),
            "adduct": pd.Categorical(rng.choice(["[M+H]+", "[M+Na]+"], n)),
            "dbin": rng.integers(0, 7, n), "inst_same": rng.random(n) < 0.6,
            "lipid_pair": rng.integers(0, 3, n), "structure_ok": rng.random(n) < 0.97,
            "tanimoto": tan, "same_parent": rng.integers(-1, 2, n),
            "w_keep": np.where(rng.random(n) < 0.1, 100.0, 1.0),
            "s_k1": s1, "m_k1": rng.integers(0, 12, n), "s_k2": s2, "m_k2": rng.integers(0, 12, n)})
        sample = kept.sample(1500, random_state=1).reset_index(drop=True)
        sample["block_a"] = sample["block_a"].astype(str)           # the MCES sample carries plain strings
        sample["block_b"] = sample["block_b"].astype(str)
        sample["mces"] = np.round((1 - sample["tanimoto"]) * 12, 0)
        sample["mces_mode"] = 1
        sample["ht_weight"] = rng.choice([1.0, 2.5, 40.0], len(sample))
        parent_of = {nm: (None if i % 11 == 0 else f"class{i % 9}") for i, nm in enumerate(names)}
        kernels = [{"short": "k1", "identity": "test.k1", "implementation": "t", "reports_matches": True},
                   {"short": "k2", "identity": "test.k2", "implementation": "t", "reports_matches": False}]
        args = dict(library="SYN", ion_mode="Positive", parent_of=parent_of, ref_matched_short="k1", B=60, seed=0,
                    progress=lambda *_: None)
        new = self.CAL.build_table(kept, sample, kernels, interval="percentile", **args)
        ref = self.REF_CAL.build_table(kept, sample, kernels, **args)
        self.assertGreater(int((ref["estimable"] == 1).sum()), 20)
        # class_balanced rows changed on purpose (fixed class set per replicate); every other row is unchanged
        # new trailing columns (not_estimable_reason, class_pairs_*) are dropped for the comparison
        keep = lambda t: t[t["bias_view"] != "class_balanced"][self.REF_CAL.COLUMNS].reset_index(drop=True)
        ref_kept = keep(ref)
        # sparse rows hold None, which turns integer count columns to float in the full frame; compare values
        self.assertEqual(self.REF_CAL.canonical_tsv(keep(new).astype(ref_kept.dtypes.to_dict())),
                         self.REF_CAL.canonical_tsv(ref_kept))
        self.assertEqual(int((new["bias_view"] == "class_balanced").sum()),
                         int((ref["bias_view"] == "class_balanced").sum()))
        sparse = new[(new["bias_view"] == "class_balanced") & new["truth_definition"].str.startswith("mces")]
        self.assertGreater(len(sparse), 0)
        self.assertTrue((sparse["estimable"] == 0).all())
        self.assertTrue((sparse["not_estimable_reason"] == "class_balanced_sparse_sample").all())
        for col in ("threshold", "precision", "precision_ci_low", "recall", "n_analog_total"):
            self.assertTrue(sparse[col].isna().all(), col)
        self.assertTrue((sparse["class_pairs_min"].astype(float) >= 1).all())
        exhaustive_cb = new[(new["bias_view"] == "class_balanced") & ~new["truth_definition"].str.startswith("mces")]
        self.assertGreater(int((exhaustive_cb["estimable"] == 1).sum()), 0)
        self.assertFalse((new["not_estimable_reason"] == "class_balanced_sparse_sample")
                         [new["bias_view"] != "class_balanced"].any())
        unreached = new[(new["estimable"] == 0) & (new["not_estimable_reason"] != "class_balanced_sparse_sample")]
        self.assertTrue((unreached["not_estimable_reason"] == "target_not_reached").all())

    def test_class_pair_counts(self):
        import numpy as np

        counts = self.CAL.class_pair_counts(np.array([0, 0, 1, 2]), np.array([0, 1, 1, 0]))
        # class 0: pairs 0, 1, 3; class 1: pairs 1, 2; class 2: pair 3
        self.assertEqual(sorted(counts.tolist()), [1, 2, 3])


@unittest.skipUnless(D5, "D5 env only")
class ClassBalancedBootstrapTests(unittest.TestCase):
    """The class-balanced bootstrap keeps the observed class set in every replicate (2026-11-15)."""

    @classmethod
    def setUpClass(cls):
        tests_dir = str(Path(__file__).resolve().parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        cls.M = _import("mncal.metrics")
        import importlib

        cls.REF_M = importlib.import_module("mncal_metrics_reference")

    @staticmethod
    def _brute(M, s, y, w, t, ba, bb, pa, pb, B, seed):
        """Replicate by replicate, from the definition: the same Poisson draws as bootstrap_curve; per class
        ratios above t; a class of the observed set that is absent from a replicate keeps its observed ratio."""
        import numpy as np

        labels, inv = np.unique(np.concatenate([ba, bb]), return_inverse=True)
        ia, ib = inv[:len(ba)], inv[len(ba):]
        above = np.nan_to_num(s, nan=-1.0) >= t - 1e-12
        classes = sorted(set(pa) | set(pb))

        def sums(weight):
            n, yy, p = {}, {}, {}
            for k in range(len(s)):
                for c in (pa[k], pb[k]):
                    if above[k]:
                        n[c] = n.get(c, 0.0) + weight[k]
                        yy[c] = yy.get(c, 0.0) + weight[k] * y[k]
                    p[c] = p.get(c, 0.0) + weight[k] * y[k]
            return n, yy, p

        n0, y0, p0 = sums(w)
        set_p = [c for c in classes if n0.get(c, 0) > 0]
        set_r = [c for c in classes if p0.get(c, 0) > 0]
        rng = np.random.default_rng(seed)
        prec, rec = [], []
        for r0 in range(0, B, 32):
            mult = rng.poisson(1.0, (min(32, B - r0), labels.shape[0])).astype(float)
            for m in mult:
                n, yy, p = sums(m[ia] * m[ib] * w)
                prec.append(np.mean([yy[c] / n[c] if n.get(c, 0) > 0 else y0[c] / n0[c] for c in set_p]))
                rec.append(np.mean([yy.get(c, 0.0) / p[c] if p.get(c, 0) > 0 else y0.get(c, 0.0) / p0[c]
                                    for c in set_r]))
        return np.percentile(prec, [2.5, 97.5]), np.percentile(rec, [2.5, 97.5])

    def test_matches_the_definition_replicate_by_replicate(self):
        import numpy as np

        s, y, w, ba, bb, pa, pb = CalibrateEquivalenceTests._population(8, n=600, blocks=90, classes=30)
        i = 60
        t = float(self.M.GRID[i])
        new = self.M.bootstrap_curve(s, y, w, 50, ba, bb, classes=(pa, pb), B=64, seed=2, interval="percentile")
        (plo, phi), (rlo, rhi) = self._brute(self.M, s, y, w, t, ba, bb, pa, pb, B=64, seed=2)
        for got, want in ((new["precision_low"][i], plo), (new["precision_high"][i], phi),
                          (new["recall_low"][i], rlo), (new["recall_high"][i], rhi)):
            self.assertAlmostEqual(float(got), float(want), places=9)

    def test_single_block_classes_no_longer_drop_out(self):
        """40 classes held by one block each, none with a positive above t, beside 10 large classes: the
        dropped-class bootstrap put the point recall below its interval; the fixed class set contains it."""
        import numpy as np

        rng = np.random.default_rng(11)
        big = [f"B{c:02d}_{j:02d}" for c in range(10) for j in range(12)]
        small = [f"S{c:02d}" for c in range(40)]
        cls = {b: b[:3] for b in big} | {b: b for b in small}
        rows = []
        for c in range(10):
            members = [b for b in big if b.startswith(f"B{c:02d}")]
            for _ in range(60):
                a, b = rng.choice(members, 2, replace=False)
                positive = rng.random() < 0.7
                score = rng.choice([0.95, 0.2]) if positive else 0.1
                rows.append((a, b, score, float(positive)))
        for sb in small:
            for _ in range(3):
                partner = rng.choice(big)
                rows.append((sb, partner, 0.2, 1.0))           # positives, all below t
        ba = np.array([r[0] for r in rows])
        bb = np.array([r[1] for r in rows])
        s = np.array([r[2] for r in rows])
        y = np.array([r[3] for r in rows])
        w = np.ones(len(rows))
        pa = np.array([cls[b] for b in ba])
        pb = np.array([cls[b] for b in bb])
        i = int(np.argmin(np.abs(self.M.GRID - 0.9)))
        point = self.M.curve(s, y, w, classes=(pa, pb))["recall"][i]
        (_, _), (old_lo, old_hi) = self.REF_M.bootstrap_at(s, y, w, float(self.M.GRID[i]), ba, bb,
                                                            classes=(pa, pb), B=200, seed=0)
        new = self.M.bootstrap_curve(s, y, w, i, ba, bb, classes=(pa, pb), B=200, seed=0, interval="percentile")
        self.assertLess(point, old_lo)                          # the defect, reproduced on the frozen code
        self.assertLessEqual(new["recall_low"][i], point)
        self.assertGreaterEqual(new["recall_high"][i], point)


@unittest.skipUnless(D5, "D5 env only")
class BcIntervalTests(unittest.TestCase):
    """Bias-corrected percentile intervals (2026-11-16, WS0's decision for every view and truth)."""

    @classmethod
    def setUpClass(cls):
        cls.M = _import("mncal.metrics")

    def test_reduces_to_the_plain_percentile_when_the_point_is_the_median(self):
        import numpy as np

        rng = np.random.default_rng(0)
        base = rng.normal(size=(200, 5))
        reps = np.concatenate([np.zeros((1, 5)), base, -base], axis=0)   # symmetric about 0, 0 itself once
        theta = np.zeros(5)
        lo, hi = self.M.bc_interval(reps, theta)
        plo, phi = np.nanpercentile(reps, [2.5, 97.5], axis=0)
        np.testing.assert_allclose(lo, plo, atol=1e-12)
        np.testing.assert_allclose(hi, phi, atol=1e-12)

    def test_matches_the_definition(self):
        import numpy as np
        from scipy.stats import norm

        rng = np.random.default_rng(1)
        reps = rng.gamma(2.0, 1.0, size=(500, 4))
        reps[rng.random(reps.shape) < 0.05] = np.nan
        theta = np.array([1.2, 2.0, 2.9, np.nan])
        lo, hi = self.M.bc_interval(reps, theta)
        for g in range(3):
            col = reps[:, g][np.isfinite(reps[:, g])]
            n = col.size
            share = np.clip((np.sum(col < theta[g]) + 0.5 * np.sum(col == theta[g])) / n, 0.5 / n, 1 - 0.5 / n)
            z0 = norm.ppf(share)
            want = np.quantile(col, [norm.cdf(2 * z0 - 1.959963984540054), norm.cdf(2 * z0 + 1.959963984540054)])
            self.assertAlmostEqual(lo[g], want[0], places=10)
            self.assertAlmostEqual(hi[g], want[1], places=10)
        plo, phi = np.nanpercentile(reps[:, 3], [2.5, 97.5])     # no point estimate: plain percentile
        self.assertAlmostEqual(lo[3], plo, places=12)
        self.assertAlmostEqual(hi[3], phi, places=12)

    def test_small_class_ratio_estimator_point_inside_bc_but_not_plain(self):
        """30 classes of 4 blocks with 4 positive pairs each, plus one negative pair of weight 5 to a block of
        its own: the negative vanishes from a replicate whenever that block draws 0, so each class ratio is
        skewed upwards. The point lies below the plain percentile interval and inside the BC interval."""
        import numpy as np

        rng = np.random.default_rng(0)
        rows = []
        for c in range(30):
            blocks = [f"C{c:02d}_{j}" for j in range(4)]
            for _ in range(4):
                a, b = rng.choice(blocks, 2, replace=False)
                rows.append((a, b, 1.0, 1.0, c))
            rows.append((rng.choice(blocks), f"H{c:02d}_0", 0.0, 5.0, c))
        ba = np.array([r[0] for r in rows])
        bb = np.array([r[1] for r in rows])
        y = np.array([r[2] for r in rows])
        w = np.array([r[3] for r in rows])
        cls = np.array([f"class{r[4]:02d}" for r in rows])
        s = np.full(len(rows), 0.95)
        i = int(np.argmin(np.abs(self.M.GRID - 0.9)))
        point = self.M.curve(s, y, w, classes=(cls, cls))["precision"][i]
        plain = self.M.bootstrap_curve(s, y, w, i, ba, bb, classes=(cls, cls), B=400, seed=0, interval="percentile")
        bc = self.M.bootstrap_curve(s, y, w, i, ba, bb, classes=(cls, cls), B=400, seed=0, interval="bc")
        self.assertLess(point, plain["precision_low"][i])
        self.assertLessEqual(bc["precision_low"][i], point)
        self.assertGreaterEqual(bc["precision_high"][i], point)


@unittest.skipUnless(D5, "D5 env only")
class AnchorPilotTests(unittest.TestCase):
    """Anchor selection (cap per direct parent, structure required, seed-fixed) and the sizing rule."""

    @classmethod
    def setUpClass(cls):
        cls.AN = _import("mncal.anchors")

    def _frames(self):
        import pandas as pd

        blocks = pd.DataFrame({"block": [f"B{i:03d}" for i in range(300)],
                               "structure_ok": [i % 10 != 0 for i in range(300)],
                               "direct_parent": ["Lipid" if i < 250 else (None if i % 2 else "Other") for i in range(300)],
                               "is_lipid": [int(i < 250) for i in range(300)]})
        reps = pd.DataFrame({"rep_id": [f"r{i}" for i in range(600)], "block": [f"B{i // 2:03d}" for i in range(600)],
                             "adduct": ["[M+H]+"] * 600, "instrument_class": ["QTOF"] * 600})
        return reps, blocks

    def test_cap_per_parent_structure_and_determinism(self):
        reps, blocks = self._frames()
        rows = self.AN.select_anchors(reps, blocks, 1000, 7, max_per_parent=200)
        info = blocks.set_index("block")
        chosen = reps.iloc[rows]
        self.assertTrue(chosen["block"].map(info["structure_ok"]).all())
        parents = chosen["block"].map(info["direct_parent"]).fillna("unknown")
        self.assertEqual(int((parents == "Lipid").sum()), 200)          # 450 eligible lipid reps, capped
        self.assertEqual(len(set(rows)), len(rows))
        again = self.AN.select_anchors(reps, blocks, 1000, 7, max_per_parent=200)
        self.assertEqual(list(rows), list(again))
        other = self.AN.select_anchors(reps, blocks, 1000, 8, max_per_parent=200)
        self.assertNotEqual(list(rows), list(other))
        first = self.AN.select_anchors(reps, blocks, 50, 7, max_per_parent=200)
        self.assertEqual(list(first), list(rows[:50]))                   # n takes the lowest hashes

    def test_summary_rule(self):
        res = [{"pairs_u": 100, "pairs_pass": 80, "pairs_scored": 4, "t_prefilter": 0.01, "t_prepare": 0.5,
                "t_kernel": {"k1": 0.002, "k2": 0.002}} for _ in range(10)]
        s = self.AN.summarise(res, ["k1", "k2"], wall_hours=8.0, workers=16)
        self.assertAlmostEqual(s["t_pair_seconds_all_kernels"], 0.001)
        self.assertAlmostEqual(s["seconds_per_anchor_single_worker"], 80 * 0.001 + 0.01)
        self.assertEqual(s["anchors_max_within_rule"], int(8 * 3600 * 16 / 0.09))
        self.assertAlmostEqual(s["prefilter_pass_share"], 0.8)


@unittest.skipUnless(D5, "D5 env only")
class HomologueTruthTests(unittest.TestCase):
    """homologue_rules_v1: PFAS CF2 series, alkyl and lipid CH2 series, C2H4O series, and non-homologues."""

    S = {
        "PFHxA": "OC(=O)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)F",
        "PFOA": "OC(=O)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)F",
        "PFDA": "OC(=O)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)F",
        "PFBS": "OS(=O)(=O)C(F)(F)C(F)(F)C(F)(F)C(F)(F)F",
        "PFOS": "OS(=O)(=O)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)F",
        "6:2 FTOH": "OCCC(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)F",
        "8:2 FTOH": "OCCC(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)F",
        "8:2 FTSA": "OS(=O)(=O)CCC(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)C(F)(F)F",
        "FA 16:0": "CCCCCCCCCCCCCCCC(=O)O",
        "FA 18:0": "CCCCCCCCCCCCCCCCCC(=O)O",
        "FA 18:1 n-9": "CCCCCCCC/C=C\\CCCCCCCC(=O)O",
        "FA 18:1 n-7": "CCCCCC/C=C\\CCCCCCCCCC(=O)O",
        "FA 18:2": "CCCCC/C=C\\C/C=C\\CCCCCCCC(=O)O",
        "PC 16:0/18:1": "CCCCCCCCCCCCCCCC(=O)OCC(COP(=O)(O)OCC[N+](C)(C)C)OC(=O)CCCCCCC/C=C\\CCCCCCCC",
        "PC 18:0/18:1": "CCCCCCCCCCCCCCCCCC(=O)OCC(COP(=O)(O)OCC[N+](C)(C)C)OC(=O)CCCCCCC/C=C\\CCCCCCCC",
        "PC 18:0/16:1": "CCCCCCCCCCCCCCCCCC(=O)OCC(COP(=O)(O)OCC[N+](C)(C)C)OC(=O)CCCCCCC/C=C\\CCCCCC",
        "PE 16:0/18:1": "CCCCCCCCCCCCCCCC(=O)OCC(COP(=O)(O)OCCN)OC(=O)CCCCCCC/C=C\\CCCCCCCC",
        "CAR 10:0": "CCCCCCCCCC(=O)OC(CC(=O)O)C[N+](C)(C)C",
        "CAR 12:0": "CCCCCCCCCCCC(=O)OC(CC(=O)O)C[N+](C)(C)C",
        "triethylene glycol": "OCCOCCOCCO",
        "tetraethylene glycol": "OCCOCCOCCOCCO",
        "1,5-pentanediol": "OCCCCCO",
        "methanol": "CO", "ethanol": "CCO", "propanol": "CCCO",
        "toluene": "Cc1ccccc1", "ethylbenzene": "CCc1ccccc1", "propylbenzene": "CCCc1ccccc1",
        "glucose": "OCC1OC(O)C(O)C(O)C1O", "cyclohexane": "C1CCCCC1", "cycloheptane": "C1CCCCCC1",
    }
    EXPECT = [
        ("PFHxA", "PFOA", "series"), ("PFOA", "PFDA", "series"), ("PFBS", "PFOS", "series"),
        ("PFOA", "PFOS", "none"), ("6:2 FTOH", "8:2 FTOH", "series"), ("8:2 FTOH", "8:2 FTSA", "none"),
        ("PFOA", "8:2 FTOH", "none"),
        ("FA 16:0", "FA 18:0", "series"), ("FA 18:0", "FA 18:1 n-9", "none"),
        ("FA 18:1 n-9", "FA 18:1 n-7", "redistribution"), ("FA 18:1 n-9", "FA 18:2", "none"),
        ("PC 16:0/18:1", "PC 18:0/18:1", "series"), ("PC 16:0/18:1", "PC 18:0/16:1", "redistribution"),
        ("PC 16:0/18:1", "PE 16:0/18:1", "none"), ("CAR 10:0", "CAR 12:0", "series"),
        ("triethylene glycol", "tetraethylene glycol", "series"),
        ("triethylene glycol", "1,5-pentanediol", "none"),
        ("methanol", "ethanol", "none"), ("ethanol", "propanol", "series"),
        ("toluene", "ethylbenzene", "none"), ("ethylbenzene", "propylbenzene", "series"),
        ("glucose", "cyclohexane", "none"), ("cyclohexane", "cycloheptane", "none"),
    ]

    @classmethod
    def setUpClass(cls):
        cls.H = _import("mncal.homologue")

    def test_expected_relations(self):
        sk = {k: self.H.skeleton(v) for k, v in self.S.items()}
        for a, b, want in self.EXPECT:
            got = self.H.relation(sk[a][0], sk[a][1:], sk[b][0], sk[b][1:])
            self.assertEqual(got, want, f"{a} vs {b}")
            self.assertEqual(self.H.relation(sk[b][0], sk[b][1:], sk[a][0], sk[a][1:]), want, f"{b} vs {a}")

    def test_cf2_units_carry_their_fluorines_and_terminal_groups_are_kept(self):
        sk, ch2, cf2, oxa = self.H.skeleton(self.S["PFOA"])
        self.assertEqual((ch2, cf2, oxa), (0, 6, 0))
        self.assertIn("C(F)(F)F", sk)                      # CF3 is not a unit
        sk, ch2, cf2, oxa = self.H.skeleton(self.S["6:2 FTOH"])
        self.assertEqual((ch2, cf2, oxa), (2, 5, 0))         # CH2 and CF2 runs stay apart

    def test_stereo_is_ignored_and_unparsable_input_gives_no_skeleton(self):
        a = self.H.skeleton("C[C@H](N)C(=O)O")
        b = self.H.skeleton("C[C@@H](N)C(=O)O")
        self.assertEqual(a, b)
        self.assertEqual(self.H.skeleton("not a smiles"), (None, 0, 0, 0))
        self.assertEqual(self.H.relation(None, (0, 0, 0), None, (0, 0, 0)), "none")

    def test_pair_relation_matches_relation(self):
        import itertools

        import numpy as np
        import pandas as pd

        names = sorted(self.S)
        blocks = pd.DataFrame({"block": names, "smiles_std": [self.S[n] for n in names],
                               "structure_ok": [True] * len(names)})
        blocks.loc[blocks["block"] == "glucose", "structure_ok"] = False
        table = self.H.block_table(blocks)
        pairs = list(itertools.combinations(names, 2))
        got = self.H.pair_relation(table, [a for a, _ in pairs], [b for _, b in pairs])
        sk = table.set_index("block")
        code = {"none": 0, "series": 1, "redistribution": 2}
        for (a, b), g in zip(pairs, got):
            ra, rb = sk.loc[a], sk.loc[b]
            want = self.H.relation(ra.skeleton if ra.skeleton_code >= 0 else None,
                                   (ra.n_ch2, ra.n_cf2, ra.n_oxa),
                                   rb.skeleton if rb.skeleton_code >= 0 else None, (rb.n_ch2, rb.n_cf2, rb.n_oxa))
            self.assertEqual(int(g), code[want], f"{a} vs {b}")
        self.assertTrue(np.all(got[[i for i, (a, b) in enumerate(pairs) if "glucose" in (a, b)]] == 0))

    def test_truth_population_is_mces3_or_homologue(self):
        import numpy as np
        import pandas as pd

        CAL = _import("mncal.calibrate")
        sample = pd.DataFrame({"mces": [1.0, 5.0, 5.0, 12.0, np.nan], "mces_mode": [1, 1, 1, 4, -1],
                               "homologue": np.array([0, 1, 0, 2, 1], np.int8),
                               "ht_weight": [1.0, 2.0, 3.0, 4.0, 5.0]})
        frame, y, w = CAL.truth_population("mces3_homologue", None, sample)
        self.assertEqual(list(y), [1.0, 1.0, 0.0, 1.0])
        self.assertEqual(list(w), [1.0, 2.0, 3.0, 4.0])
        frame, y, _ = CAL.truth_population("mces3", None, sample)
        self.assertEqual(list(y), [1.0, 0.0, 0.0, 0.0])
        empty, y0, _ = CAL.truth_population("mces3_homologue", None, sample.drop(columns="homologue"))
        self.assertTrue(empty.empty)


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
