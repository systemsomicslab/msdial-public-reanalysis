# MN calibration (WS4) and analog search (WS7)

`scripts/mncal/` measures how spectral similarity relates to structural similarity, so that "score >= t
means a structural analog with precision p" is a measured, kernel-specific statement, and then uses the
measured cutoff to search experimental spectra against a reference library. The package contains no
library content. Calibration ground truth is library-vs-library: the public MS-DIAL VS20 MSP pair first
(CC BY 4.0; Zenodo [21901200](https://doi.org/10.5281/zenodo.21901200) and
[21904103](https://doi.org/10.5281/zenodo.21904103)), then a private library locally, which leaves
aggregates only.

## Environment

The D5 environment `annotation-py312` (Python 3.12; rdkit 2026.9.2, matchms 0.34.0, myopic-mces 2.3.0,
numpy 2.5.4, scipy 1.16.3, pandas 3.1.1, pyarrow 26.0.1) is frozen in `envs/annotation-py312.lock.txt`.
`msdial_spectrum_catalog` is imported read-only from a detached pin worktree whose commit is recorded in
every `run.json`.

```
set PYTHONPATH=scripts;<catalog pin>\src
<annotation-py312>\Scripts\python.exe -m mncal <subcommand> ...
```

## Pipeline

| Step | Subcommand | What it does |
|---|---|---|
| 0 | (catalog) `msdial-spectrum-catalog ingest-reference-library <db> <msp> --library-name ... --no-consensus` | Reference DB built by the catalog's own MSP parser. |
| 1 | `build-reps` | Eligible records (InChIKey block, SMILES, adduct in [M+H]+, [M+Na]+, [M+NH4]+, [M-H]-, [M+FA-H]-, [M+CH3COO]-, >= 3 peaks after the primary prep); one representative per (block, adduct, instrument class), most peaks >= 1% of base, ties to the lowest record ordinal. Per block: modal SMILES, RDKit largest fragment + uncharger, Morgan-2 2048-bit fingerprint, ClassyFire direct parent (MS-FINDER `.icd` by full key, then first block, then the MSP ONTOLOGY; source recorded), lipid flag (`lipid_direct_parents_v1.tsv`). |
| 2 | `check-kernels` | Primary identity round-trip through WS2's `canonical_kernel_id`, equality with WS3's reported string, `similarity-fixtures/` parity (abs 1e-9). |
| 3 | `score-pairs` | Pair universe U (same ion mode and adduct, different block, abs(dprec) <= 400 Da, all instrument combinations); prefilter; kernels; Tanimoto; class and lipid relations. Chunked, resumable, <= 16 workers at BelowNormal priority. |
| 4 | `structure-truth` | myopic MCES (threshold 10) on a stratified simple random sample. `--reuse-mces <run>` reuses an earlier run's results for block pairs whose standard SMILES are unchanged (the sample depends only on representatives, seed and caps, never on kernels). |
| 4b | `homologue-truth` | Repeat-unit skeleton and unit counts per block (`homologue_blocks_<mode>.parquet`; local only in a private run, because it holds structure-derived SMILES) and the relation counts over the MCES sample. Seconds per mode. |
| 5 | `calibrate` | `cutoff_table.tsv`, curves, sanity numbers; `table_sha256` in `run.json`. `--truths` / `--views` recompute part of the table and replace or add its rows. |
| 6 | `report` | `report.md` from `run.json` and the table. |
| - | `guard` | VS21 leak scanner (strict, or `--allow-neighbour-identity` for WS7). |

### Prefilter

Every peak at or above 1% of the base peak and at or below precursor + 0.05 contributes a fragment frame
floor(m) and a loss frame floor(prec - m). A pair is scored when min(count_a(b), count_b(a)) >= 2, where
count_a(b) is the number of a's peak-frame entries with a b frame within +/- 1. Two peaks that match within
0.025 Da as fragments or under the precursor shift have frames within +/- 1, and a one-to-one assignment of k
matched peaks uses k distinct peaks on each side, so the bound is an exact superset of ">= 2 matched peaks"
for every minimum-match kernel evaluated (all MN preps have `prep.rel = 1`; the matchms reference filters at
1%). Pairs failing it get matched = 0 and score = 0 and are counted per stratum. A randomised test compares
the bound with an exact maximum bipartite matching.

### Kernels and identities (charter C3, C4)

Scores are stored in each identity's native convention; cosines are never squared.

| Role | Identity |
|---|---|
| Primary (WS3 C# `ModifiedCosine` at MS-DIAL defaults) | `gnps.modified_cosine?assign=greedy&ipow=1&mzpow=0&prep=mn&prep.above=0.025&prep.abs=0&prep.frame=0&prep.maxmz=10000&prep.minmz=0&prep.pow=1&prep.precwin=17&prep.rel=1&prep.scale=100&prep.topk=0&prep.topkwin=50&tol=0.025` |
| Current MS-DIAL MN default | `msdial.bonanza.v1?prep=mn&prep.above=0.05&prep.abs=0&prep.frame=1&prep.maxmz=10000&prep.minmz=0&prep.pow=0.5&prep.precwin=0&prep.rel=1&prep.scale=100&prep.topk=0&prep.topkwin=50&tol=0.025` |
| Near-1 artefact, documented | `msdial.moddot.legacy?` + the same parameters as Bonanza |
| Non-shift baseline | `entropy.li2021?bin=0.05&prep=none&weighting=none` |
| Sensitivity only | the primary with `prep.pow=0.5` (full run; the author asked for it); with `prep.above=0.02&...&tol=0.02`; with `assign=optimal` (Python only); WS2's GNPS-like K4, `gnps.modified_cosine?assign=greedy&ipow=1&mzpow=0&prep=mn&prep.above=0.05&prep.abs=0&prep.frame=0&prep.maxmz=10000&prep.minmz=0&prep.pow=0.5&prep.precwin=17&prep.rel=1&prep.scale=100&prep.topk=6&prep.topkwin=50&tol=0.025` (Python only). The last three and the matchms reference run on a 10% block subsample. |
| External reference (run records and `cutoff_table.tsv` only, never `spectrum_similarity.method`) | `matchms.modified_cosine?above=0.025&class=ModifiedCosineGreedy&ipow=1&mzpow=0&norm=max&precwin=17&rel=1&tol=0.025&version=0.34.0` |

The matchms reference applies, in this order: drop peaks below 1% of base, drop peaks above precursor +
0.025, drop peaks within 17 Da of the precursor, scale to max 1; then matchms 0.34.0
`ModifiedCosineGreedy(tolerance=0.025, mz_power=0, intensity_power=1)`. It calls matchms's own numba cores
(`collect_peak_pairs`, `score_best_matches`) on arrays, skipping the Spectrum object overhead; a test checks
it equals `ModifiedCosineGreedy.pair` exactly.

`entropy.li2021` is scored by a numpy port of the catalog's `similarity.entropy_similarity` (the function WS2
wraps); it is tested against that function to 1e-9 and is checked against `similarity-fixtures/` once they
are in the catalog pin. The port is offered to WS2. A kernel that reports no matched peaks (entropy) is
stratified by the run's modified-cosine matched count and is scored only on pairs with >= 2 such matches.

### Structural truth and estimands

- Tanimoto: `rdkit.morgan2_2048.tanimoto@<rdkit>`, every scored pair.
- MCES: `myopic_mces@<ver>;threshold=10;solver=COIN_CMD`, on a simple random sample per stratum (ion mode x
  dprec bin x Tanimoto bin), n_h = min(N_h, 500) below Tanimoto 0.6 and min(N_h, 2000) from 0.6 (the spec takes these bins whole; at VS20 scale that is about 80k pairs per mode, so they are capped), chosen by a
  seed-fixed pair hash (bottom-k); HT weight N_h / n_h.
- Class: same ClassyFire direct parent.
- Analog definitions: MCES <= 3 (primary), <= 2, <= 5; Tanimoto >= 0.7; same direct parent; and, since
  2026-11-15, the homologue-aware tier `mces3_homologue` (below).
- `mces3_homologue` (`homologue_rules_v1`, `scripts/mncal/homologue.py`, step `homologue-truth`): y = MCES <= 3,
  or the two blocks are homologues, on the MCES sample with its HT weights. `mces3` is unchanged. The author
  decided this on 2026-11-15 after the VS20 run: MCES <= 3 counts a PFAS CF2 step as three edits, and an
  alkyl or lipid chain step as one edit per CH2, so the series that dominate high-score pairs fail it.
  - Repeat-unit skeleton of a block (standard SMILES, stereo removed):
    - unit atoms are acyclic, uncharged, single-bonded CH2 (two heavy neighbours), CF2 (two terminal F and
      two other heavy neighbours) and ether O between two CH2 units (the O of C2H4O);
    - adjacent CH2, adjacent CF2, and CH2 next to such an O form one run, but a CH2 and a CF2 never share a run;
    - each run becomes one dummy atom labelled CH2, CF2 or CH2 with O;
    - the canonical SMILES of the result is the skeleton, and the block keeps its unit counts.
    Terminal CH3 and CF3 are not units.
  - Homologues have equal skeletons and unit counts that differ in one unit kind only (`series`: CH2, CF2,
    or C2H4O, the last as dCH2 = 2 dO), or that do not differ at all (`redistribution`: the same units spread
    differently over chains, such as PC 16:0/18:1 and PC 18:0/16:1, or a double bond moved along a chain).
  - Tests cover PFCA, PFSA and fluorotelomer CF2 series, fatty acid, acylcarnitine and PC CH2 series,
    glycol C2H4O series, and non-homologues: head-group changes, unsaturation, terminal methyl, ring size and
    sulfonate against carboxylate.
- E1 precision(t) and recall(t) with w = 1 (exhaustive) or HT weights (MCES). Bias views: all pairs;
  lipid-lipid pairs excluded; class-balanced (each direct-parent class gets equal total weight, a pair
  counting half for each endpoint's class).
- t*(p): the smallest t on the 0.01 grid with Wilson lower 95% bound (at the Kish effective size) >= p and
  n(s >= t) >= 30, and the dyadic cluster-bootstrap lower bound (Poisson(1) multiplier per block, product
  over the two blocks, B = 1000, fixed seed) also >= p; otherwise "not estimable". Bootstrap intervals are
  bias-corrected percentile intervals (BC, no acceleration term) in every view and truth since 2026-11-16 (WS0):
  - z0 = Phi^-1(share of replicates below the point estimate, ties counting half, clipped to
    [0.5/B, 1 - 0.5/B]);
  - the bounds are the replicate quantiles at Phi(2 z0 -/+ 1.96);
  - with z0 = 0 this is the plain 2.5/97.5 percentile used before;
  - `run.json` records `interval_method` (`bc_percentile`), and a partial rerun refuses a table computed with
    another method.
- The class-balanced view of the MCES-sample truths (mces2, mces3, mces5, mces3_homologue) is never
  estimable (WS0, 2026-11-16).
  - Its rows carry `not_estimable_reason = class_balanced_sparse_sample` and the minimum and median sampled
    pairs per direct-parent class (`class_pairs_min`, `class_pairs_median`).
  - They carry no estimate or interval.
  - Why: about 38k sampled pairs spread over thousands of classes, so a class rests on one to three sampled
    pairs. The VS20 point estimates fell outside their BC intervals, or the intervals collapsed.
  - Every other non-estimable row has the reason `target_not_reached`.
  - The exhaustive truths in the class-balanced view are unaffected.
- In the class-balanced
  view a replicate averages over the observed class set (the classes present above t for precision, the
  classes with a positive for recall); a class whose blocks all drew a zero multiplier keeps its observed
  ratio instead of dropping out (since ce59877; before it, single-block classes left about 37% of
  replicates and the recall interval could exclude the point estimate). For exhaustive truth the
  grid starts at 0.30, because pair shards keep every pair with any score >= 0.3, Tanimoto >= 0.4 or the same
  direct parent (all positives), and the rest only as score x Tanimoto histograms. The score rule
  ignores `msdial.moddot.legacy`, which scores >= 0.3 on about half of all pairs (its near-1 artefact);
  pairs kept only because of it form a seed-fixed 1% Poisson sample with weight 100 (`w_keep`), so its
  cutoffs stay unbiased and every other kernel's cells are unchanged.
- Computation: each cell's curve and bootstrap take one pass over its pairs. A score's bin is the number of
  grid thresholds it reaches, so every threshold's sums are suffix sums of per-bin sums; the class-balanced
  view decomposes over per-bin class and class-pair sums. One bootstrap pass from the smallest candidate t
  gives the interval at every t above it, with the same replicate draws (per block, sorted label order,
  batches of 32) as a separate evaluation at each t. `tests/mncal_*_reference.py` freeze the earlier
  per-threshold code, and the tests require the new path to reproduce its `cutoff_table.tsv` exactly
  outside the class-balanced view. `calibrate --views class_balanced` recomputes one view and replaces its
  rows, keeping the previous table as `cutoff_table.<sha12>.tsv` and a `view_reruns` entry in run.json.
- The benchmark id of a cutoff is `mn-calibration/<run-id>` plus `table_sha256` from `run.json`.

### cutoff_table.tsv

Columns: kernel_identity, implementation, library, ion_mode, adduct, dprec_bin, min_matched,
instrument_pair, truth_definition, bias_view, target_precision, threshold, precision, precision_ci_low,
precision_ci_high, recall, recall_ci_low, recall_ci_high, n_pairs_above, n_analog_total, estimable, then the
trailing wilson_low, n_eff_above, bootstrap_b, population, not_estimable_reason, class_pairs_min, class_pairs_median. `min_matched = k` is a floor (>= k matched
peaks). Rows pooled over adducts or instrument pairs carry `all`. In a private run, counts 1-9 are written
`<10`.

### Truth vocabulary (proposed to WS1, 2026-11-15)

`truth_definition` in `cutoff_table.tsv` and in `mn_evidence_proposal` takes one of `mces3`, `mces2`,
`mces5`, `tanimoto07`, `same_parent` and, new, `mces3_homologue`.

- The overlay DDL does not change, because the column is free text.
- What changes is the vocabulary that WS8's claim derivation reads. WS1 owns that vocabulary, and WS0
  routes the change.
- A consumer that knows only the five earlier values must treat `mces3_homologue` as unknown and must not
  map it to `mces3`.
- The rules file version (`homologue_rules_v1`) is recorded in `run.json` (step `homologue-truth`).

## WS7 decisions (author, 2026-11-15)

- **Kernel: pow05.** The identity is
  `gnps.modified_cosine?assign=greedy&ipow=1&mzpow=0&prep=mn&prep.above=0.025&prep.abs=0&prep.frame=0&prep.maxmz=10000&prep.minmz=0&prep.pow=0.5&prep.precwin=17&prep.rel=1&prep.scale=100&prep.topk=0&prep.topkwin=50&tol=0.025`.
  - It is recorded as a **deviation** from the pre-registered primary (`prep.pow=1`), which reached no
    tanimoto07 target in VS20 positive mode and reached negative-mode targets only at t = 1.00.
  - The primary's C# parity (O3) does not carry over: no C# kernel has been checked against `prep.pow=0.5`.
    A pow05 cutoff therefore holds for the Python kernel only, until WS3 reports parity.
- **Sensitivity kernel in the reports: bonanza.** The identity is `msdial.bonanza.v1?...` as above.
- **Target precision: 0.7 in both ion modes**, on tanimoto07 truth. Once its cutoffs exist, the
  `mces3_homologue` tier is reported alongside.

## WS7 overlay (`mn-overlay-v1`)

The overlay DB `mbpost-devset-mn.local-vs21.sqlite` is initialised with the catalog schema
(`storage.initialize`); these two sidecar tables are then created verbatim, and every tool run records
`"overlay_schema": "mn-overlay-v1"` in `parameters_json`. A test compares `PRAGMA table_info` with this
block.

<!-- mn-overlay-v1 DDL: verbatim from the WS4/WS7 spec, Design §6 -->

```sql
CREATE TABLE mn_edge_annotation (                -- one row per WS7 spectrum_similarity row
  similarity_id TEXT PRIMARY KEY REFERENCES spectrum_similarity(similarity_id),
  query_spectrum_id TEXT NOT NULL,               -- = spectrum_similarity.subject_id_a (dev-catalog URN)
  reference_spectrum_id TEXT NOT NULL REFERENCES reference_spectrum(reference_spectrum_id),
                                                 -- = spectrum_similarity.subject_id_b (opaque join key)
  neighbour_name TEXT,                           -- VS21 record NAME of the neighbour (O2: publishable,
                                                 -- citing VS21 by name + sha256); NULL if the record has none
  neighbour_inchikey TEXT,                       -- full InChIKey of the neighbour (O2: publishable); NULL if none
  neighbour_library_id TEXT NOT NULL REFERENCES reference_library(library_id),
                                                 -- the VS21 pos/neg row (name + sha256) the identity is cited from
  ion_mode TEXT NOT NULL,                        -- Positive | Negative, per query record
  query_adduct TEXT NOT NULL,
  adduct_assumed INTEGER NOT NULL CHECK (adduct_assumed IN (0,1)),   -- 1 when EVIDENCE=null
  query_precursor_mz REAL NOT NULL,
  neighbour_precursor_mz REAL NOT NULL,
  delta_mz REAL NOT NULL,                        -- signed: query − neighbour
  dprec_bin TEXT NOT NULL CHECK (dprec_bin IN ('B0','B1','B2','B3','B4','B5','B6')),   -- §2, on |delta_mz|
  shift_label TEXT NOT NULL,                     -- one rule label, 'a+b' for a sum of two,
                                                 -- 'ambiguous', 'isomer' (B0) or 'uninterpreted'
  label_ambiguous INTEGER NOT NULL CHECK (label_ambiguous IN (0,1)),
  fitting_labels_json TEXT NOT NULL,             -- JSON array of every fitting rule label or sum, sorted;
                                                 -- [] for isomer/uninterpreted
  shift_rules_sha256 TEXT NOT NULL,              -- sha256 of mass_shift_rules_v1.tsv
  same_compound_as_sl_hit INTEGER NOT NULL CHECK (same_compound_as_sl_hit IN (0,1)),
  edge_rank INTEGER NOT NULL,                    -- 1..5 within the query, by score then similarity_id
  implied_formula TEXT                           -- neighbour formula + delta; local only; NULL if none
);

CREATE TABLE mn_evidence_proposal (              -- one row per edge with same_compound_as_sl_hit = 0
  proposal_id TEXT PRIMARY KEY,                  -- make_id("mn-evidence-proposal", similarity_id)
  similarity_id TEXT NOT NULL UNIQUE REFERENCES mn_edge_annotation(similarity_id),
  spectrum_id TEXT NOT NULL,                     -- = spectrum_similarity.subject_id_a
  method TEXT NOT NULL,                          -- full C3 kernel identity = spectrum_similarity.method
  score_convention TEXT NOT NULL,                -- = spectrum_similarity.score_convention (C4)
  score REAL NOT NULL,                           -- = spectrum_similarity.score, unsquared
  matched_peak_count INTEGER NOT NULL,           -- = spectrum_similarity.matched_peak_count
  ion_mode TEXT NOT NULL,
  dprec_bin TEXT NOT NULL,
  min_matched INTEGER NOT NULL CHECK (min_matched IN (2,3,6)),   -- cutoff-table floor used
  cutoff_benchmark_id TEXT NOT NULL,             -- mn-calibration/<run-id>
  cutoff_table_sha256 TEXT NOT NULL,
  truth_definition TEXT NOT NULL,
  bias_view TEXT NOT NULL,
  target_precision REAL NOT NULL,
  cutoff_estimable INTEGER NOT NULL CHECK (cutoff_estimable IN (0,1)),
  threshold REAL,                                -- NULL when the cell is not estimable
  precision_ci_low REAL,                         -- of that cell; NULL when not estimable
  evidence_value_json TEXT NOT NULL,             -- the six MN value fields, below
  source_reference_spectrum_id TEXT NOT NULL REFERENCES reference_spectrum(reference_spectrum_id),
  source_tool_run_id TEXT NOT NULL REFERENCES annotation_tool_run(tool_run_id),
  passed INTEGER NOT NULL CHECK (passed IN (0,1))   -- 1 iff cutoff_estimable = 1 and score >= threshold
);
```

`evidence_value_json` holds the six MN value fields of `smb-v2-consensus.json`: `network_score` (= score),
`score_convention`, `connected_node_id` (the opaque reference_spectrum URN), `connected_node_level`
(`reference_library`), `mass_shift` (= delta_mz) and `shared_peak_count` (= matched_peak_count).
`neighbour_name` and `neighbour_inchikey` are the publishable subset of the neighbour's record (charter O2,
citing the library by file name and sha256); spectra, SMILES, formulas, ontology, `implied_formula`, the
record text and the library file and path stay private.

## Mass-shift rules

`scripts/mncal/mass_shift_rules_v1.tsv` (public). The signed delta = prec_query - prec_neighbour is matched
against each rule as +mass or -mass, against n*CH2 (n <= 10), and against sums of two signed rules, within
max(0.005 Da, 10 ppm of the larger precursor). Fits are deduplicated by net composition; more than one
composition is `ambiguous` (for example SO3 vs HPO3); none gives `isomer` (abs(delta) <= 0.01) or
`uninterpreted`.

## Privacy rules (charter §4)

- Library outputs are identified by file name and sha256, never by path.
- Private-library runs (`build-reps --private`) keep representatives, pair shards and samples under
  `<run>\_local-vs21\`; only aggregates leave it, and `mncal guard` (strict) must pass on `report.md`,
  `cutoff_table.tsv` and `run.json` before anything is shared.
- Databases holding private reference rows are named `*.local-vs21.sqlite` and live under the annotation
  root. Nothing produced by a run is committed.

## Rerun

```
REM VS20, full (author-started job; about 1-2 h of 16 workers for positive mode)
python -m mncal build-reps --run <annotation>\mn-calibration\vs20-<yyyymmdd>-<sha7> --db vs20-pos.sqlite --db vs20-neg.sqlite --msp <pos msp> --msp <neg msp> --icd InchikeyClassyfireDB-VS5.icd
python -m mncal check-kernels --run <run>
python -m mncal score-pairs --run <run> --workers 16
python -m mncal structure-truth --run <run> --workers 16
python -m mncal calibrate --run <run> --curves
python -m mncal report --run <run>
REM 1% seed-fixed rerun (acceptance criterion 4): the same with build-reps --block-fraction 0.01
```

## Tests

```
set PYTHONPATH=scripts;<catalog pin>\src
<annotation-py312>\Scripts\python.exe -B -m unittest discover -s tests -p "test_mncal_*.py"
```

Under the repository's default interpreter the `test_mncal_*` classes report as skipped (they need rdkit,
matchms, pyarrow and myopic_mces).
