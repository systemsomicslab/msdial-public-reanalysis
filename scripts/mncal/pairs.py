"""Pair universe, prefilter, strata and samplers (Design §2).

Pair universe U: unordered pairs of representatives with the same ion mode and adduct, different InChIKey
first blocks, and |delta precursor| <= 400 Da; every instrument-class combination is kept.

Prefilter (an exact superset of ">= 2 matched peaks" for every shift-aware kernel with tolerance <= 0.025
Da that drops peaks above precursor + 0.05): every peak at or below precursor + 0.05 contributes a
fragment frame floor(m) and a loss frame floor(prec - m). Two peaks that match as fragments (|dm| <= tol)
or under the precursor shift (|d loss| <= tol) have frames within +/- 1. For a pair (a, b), count_a(b) is
the number of a's peak-frame entries with a b frame within +/- 1, summed over both namespaces; it counts
every a-peak that has any candidate partner (a peak may be counted once per namespace, which only loosens
the bound). Any one-to-one assignment with k matched peaks uses k distinct a-peaks and k distinct b-peaks,
each with a partner, so k <= min(count_a(b), count_b(a)). Pairs with that minimum < 2 are assigned
matched = 0 and score = 0 for every kernel that requires a minimum match, and are counted per stratum.

The frames are built from peaks at or above 1% of the base peak (PREFILTER_REL), because every
minimum-match kernel evaluated here removes the others before matching (prep.rel = 1 for every MN prep;
the matchms reference filters at 1%), so the bound stays exact for them. entropy.li2021 has no minimum
match; its scores on prefilter-failed pairs are never used, because every cutoff cell requires >= 2
matched peaks under the run's modified-cosine reference count.
"""

from __future__ import annotations

import numpy as np
import scipy.sparse as sp

MAX_DPREC = 400.0
PREFILTER_ABOVE = 0.05
PREFILTER_REL = 0.01   # fraction of the base peak; every minimum-match kernel's prep has rel >= 1%
DPREC_BINS = ("B0", "B1", "B2", "B3", "B4", "B5", "B6")
# Upper edges (inclusive) of B0..B6 on |delta precursor| in Da.
DPREC_EDGES = (0.01, 2.1, 16.1, 50.0, 100.0, 200.0, 400.0)
MATCHED_BINS = ("2", "3-5", "6-10", ">10")
MIN_MATCHED_FLOORS = (2, 3, 6)
TANIMOTO_BINS = ("0-0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "0.8-1")
CH2 = 14.01565
HOMOLOGUE_TOL = 0.005


def dprec_bin_index(delta) -> np.ndarray:
    """Index into DPREC_BINS for |delta|; -1 beyond 400 Da. B0 is <= 0.01, B1 is (0.01, 2.1], ..."""
    d = np.abs(np.asarray(delta, dtype=np.float64))
    idx = np.searchsorted(np.asarray(DPREC_EDGES), d, side="left")
    return np.where(d <= DPREC_EDGES[-1], idx, -1).astype(np.int8)


def dprec_bin(delta: float) -> str | None:
    i = int(dprec_bin_index([delta])[0])
    return None if i < 0 else DPREC_BINS[i]


def matched_bin_index(matched) -> np.ndarray:
    """-1 below 2 matched peaks, else index into MATCHED_BINS."""
    m = np.asarray(matched)
    return np.select([m < 2, m == 2, m <= 5, m <= 10], [-1, 0, 1, 2], 3).astype(np.int8)


def tanimoto_bin_index(t) -> np.ndarray:
    t = np.asarray(t, dtype=np.float64)
    return np.minimum((t / 0.2).astype(np.int64), 4).astype(np.int8)


def homologue_flag(delta, same_parent) -> np.ndarray:
    """|delta| ~ k * CH2 for 1 <= |k| <= 10 within 0.005 Da, and the same direct parent."""
    d = np.abs(np.asarray(delta, dtype=np.float64))
    k = np.rint(d / CH2)
    near = (k >= 1) & (k <= 10) & (np.abs(d - k * CH2) <= HOMOLOGUE_TOL)
    return near & (np.asarray(same_parent) == 1)


# ------------------------------------------------------------------------------------------ prefilter
def frame_tokens(precursor_mz: float, mz: np.ndarray, frame_offset: int) -> tuple[np.ndarray, np.ndarray]:
    """(fragment frames, loss frames) for peaks at or below precursor + 0.05; one entry per peak."""
    mz = np.asarray(mz, dtype=np.float64)
    mz = mz[mz <= precursor_mz + PREFILTER_ABOVE]
    fragment = np.floor(mz).astype(np.int64)
    loss = np.floor(precursor_mz - mz).astype(np.int64) + frame_offset
    return fragment, loss


def token_matrices(precursors: np.ndarray, mz_lists: list[np.ndarray]) -> tuple[sp.csr_matrix, sp.csr_matrix]:
    """X: per-rep peak-frame counts (fragment and loss namespaces in one column space).
    Z: per-rep indicator of frames within +/- 1 of any of its frames, same column space."""
    max_mz = max((float(np.max(m)) for m in mz_lists if len(m)), default=0.0)
    width = int(np.floor(max(max_mz, float(np.max(precursors)) if len(precursors) else 0.0))) + 4
    offset = width + 4           # loss block after the fragment block (+/-1 spreads never cross); loss >= -1
    n_cols = offset + width + 4
    rows_x, cols_x, rows_z, cols_z = [], [], [], []
    for i, (p, mz) in enumerate(zip(precursors, mz_lists)):
        fragment, loss = frame_tokens(p, mz, offset + 1)
        cols = np.concatenate([fragment + 1, loss])
        rows_x.append(np.full(cols.shape[0], i, np.int64))
        cols_x.append(cols)
        uniq = np.unique(cols)
        spread = np.unique(np.concatenate([uniq - 1, uniq, uniq + 1]))
        rows_z.append(np.full(spread.shape[0], i, np.int64))
        cols_z.append(spread)
    n = len(mz_lists)
    rx, cx = (np.concatenate(rows_x), np.concatenate(cols_x)) if n else (np.zeros(0, int), np.zeros(0, int))
    rz, cz = (np.concatenate(rows_z), np.concatenate(cols_z)) if n else (np.zeros(0, int), np.zeros(0, int))
    X = sp.csr_matrix((np.ones(rx.shape[0], np.float32), (rx, cx)), shape=(n, n_cols))
    X.sum_duplicates()
    Z = sp.csr_matrix((np.ones(rz.shape[0], np.float32), (rz, cz)), shape=(n, n_cols))
    return X, Z


def prefilter_upper_bound(X: sp.csr_matrix, Z: sp.csr_matrix, rows: slice, cols: np.ndarray) -> np.ndarray:
    """Dense (len(rows), len(cols)) upper bound on matched peaks between rows and cols."""
    a_to_b = (X[rows] @ Z[cols].T).toarray()
    b_to_a = (Z[rows] @ X[cols].T).toarray()
    return np.minimum(a_to_b, b_to_a)


def brute_force_upper_bound(prec_a, mz_a, prec_b, mz_b, tol: float = 0.025) -> int:
    """Reference for tests: max number of a-peaks and b-peaks that have any partner."""
    mz_a = np.asarray([m for m in mz_a if m <= prec_a + PREFILTER_ABOVE])
    mz_b = np.asarray([m for m in mz_b if m <= prec_b + PREFILTER_ABOVE])
    if mz_a.size == 0 or mz_b.size == 0:
        return 0
    frag = np.abs(mz_a[:, None] - mz_b[None, :]) <= tol
    loss = np.abs((prec_a - mz_a)[:, None] - (prec_b - mz_b)[None, :]) <= tol
    ok = frag | loss
    return int(min(ok.any(axis=1).sum(), ok.any(axis=0).sum()))


def max_bipartite_matches(prec_a, mz_a, prec_b, mz_b, tol: float = 0.025) -> int:
    """Exact maximum one-to-one matching size under fragment-or-shift compatibility (tests only)."""
    from scipy.sparse.csgraph import maximum_bipartite_matching

    mz_a = np.asarray([m for m in mz_a if m <= prec_a + PREFILTER_ABOVE])
    mz_b = np.asarray([m for m in mz_b if m <= prec_b + PREFILTER_ABOVE])
    if mz_a.size == 0 or mz_b.size == 0:
        return 0
    frag = np.abs(mz_a[:, None] - mz_b[None, :]) <= tol
    loss = np.abs((prec_a - mz_a)[:, None] - (prec_b - mz_b)[None, :]) <= tol
    graph = sp.csr_matrix((frag | loss).astype(np.int8))
    match = maximum_bipartite_matching(graph, perm_type="column")
    return int(np.count_nonzero(match >= 0))


# ------------------------------------------------------------------------------------------ hashing
_M1 = np.uint64(0xBF58476D1CE4E5B9)
_M2 = np.uint64(0x94D049BB133111EB)


def splitmix64(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.uint64)
    with np.errstate(over="ignore"):
        x = x + np.uint64(0x9E3779B97F4A7C15)
        x = (x ^ (x >> np.uint64(30))) * _M1
        x = (x ^ (x >> np.uint64(27))) * _M2
        return x ^ (x >> np.uint64(31))


def rep_keys(rep_ids: list[str]) -> np.ndarray:
    import hashlib

    return np.array([int.from_bytes(hashlib.blake2b(r.encode(), digest_size=8).digest(), "big")
                     for r in rep_ids], dtype=np.uint64)


def pair_uniform(key_a: np.ndarray, key_b: np.ndarray, seed: int) -> np.ndarray:
    """Seed-fixed uniform in [0, 1) per unordered pair (symmetric in a, b)."""
    lo, hi = np.minimum(key_a, key_b), np.maximum(key_a, key_b)
    with np.errstate(over="ignore"):
        h = splitmix64(splitmix64(lo ^ np.uint64(seed)) + hi)
    return (h >> np.uint64(11)).astype(np.float64) / float(1 << 53)


def bottom_k(frame, key_cols: list[str], k: int, by: str = "u"):
    """Keep the k rows with the smallest `by` per stratum (simple random sample without replacement)."""
    if frame.empty:
        return frame
    ordered = frame.sort_values(key_cols + [by], kind="mergesort")
    return ordered.groupby(key_cols, sort=False, observed=True).head(k)
