"""Estimators, intervals, isotonic calibration and the cutoff rule (Design §5).

E1 (pairwise): precision(t) = sum w 1[s>=t] y / sum w 1[s>=t]; recall(t) = sum w 1[s>=t] y / sum w y, with
w = 1 under exhaustive truth and w = N_h / n_h (Horvitz-Thompson, simple random sample per stratum) under
sampled MCES truth.

Cutoff rule: t*(p) is the smallest t on the 0.01 grid with lower95(precision(t)) >= p and n(s>=t) >= 30,
where lower95 is the Wilson bound at the Kish effective size, and the cluster-bootstrap lower bound at t*
must also reach p (otherwise t moves up the grid). A cell without such t is "not estimable"; nothing is
extrapolated.

Cluster bootstrap: pairs that share a compound are dependent, so each replicate draws a Poisson(1)
multiplier per InChIKey first block and weights a pair by the product of its two blocks' multipliers
(a dyadic cluster bootstrap); B = 1000 with a fixed seed.
"""

from __future__ import annotations

import math

import numpy as np

Z95 = 1.959963984540054
GRID = np.round(np.arange(0.0, 1.0001, 0.01), 2)
MIN_N_ABOVE = 30


def wilson(successes: float, n: float, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval; (nan, nan) when n == 0."""
    if n <= 0:
        return math.nan, math.nan
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def kish_n(weights: np.ndarray) -> float:
    s1 = float(np.sum(weights))
    s2 = float(np.sum(np.square(weights)))
    return 0.0 if s2 == 0 else s1 * s1 / s2


def weighted_wilson(y: np.ndarray, w: np.ndarray) -> tuple[float, float, float]:
    """(estimate, low, high) for a weighted proportion, Wilson at the Kish effective size."""
    total = float(np.sum(w))
    if total <= 0:
        return math.nan, math.nan, math.nan
    p = float(np.sum(w * y)) / total
    n_eff = kish_n(w)
    low, high = wilson(p * n_eff, n_eff)
    return p, low, high


def ht_total(y: np.ndarray, weights: np.ndarray) -> float:
    """Horvitz-Thompson estimate of a population total from a sample with inclusion weights 1/pi."""
    return float(np.sum(np.asarray(y, float) * np.asarray(weights, float)))


def pava(y: np.ndarray, w: np.ndarray | None = None) -> np.ndarray:
    """Pool-adjacent-violators: the non-decreasing least-squares fit of y (already ordered by score)."""
    y = np.asarray(y, dtype=np.float64)
    w = np.ones_like(y) if w is None else np.asarray(w, dtype=np.float64)
    values, weights, sizes = [], [], []
    for yi, wi in zip(y, w):
        values.append(yi)
        weights.append(wi)
        sizes.append(1)
        while len(values) > 1 and values[-2] > values[-1]:
            wsum = weights[-2] + weights[-1]
            merged = (values[-2] * weights[-2] + values[-1] * weights[-1]) / wsum if wsum > 0 else values[-1]
            sizes[-2] += sizes[-1]
            values[-2], weights[-2] = merged, wsum
            values.pop(), weights.pop(), sizes.pop()
    return np.repeat(values, sizes)


def isotonic_curve(score: np.ndarray, y: np.ndarray, w: np.ndarray, bins: int = 100):
    """P(analog | s) by PAVA on 0.01 score bins; returns (bin centres, fitted, bin weights)."""
    idx = np.clip((np.asarray(score) * bins).astype(int), 0, bins - 1)
    wsum = np.bincount(idx, weights=w, minlength=bins)
    ysum = np.bincount(idx, weights=w * y, minlength=bins)
    present = wsum > 0
    rate = np.zeros(bins)
    rate[present] = ysum[present] / wsum[present]
    fitted = np.full(bins, np.nan)
    fitted[present] = pava(rate[present], wsum[present])
    return (np.arange(bins) + 0.5) / bins, fitted, wsum


def class_balance_weights(classes_a: np.ndarray, classes_b: np.ndarray,
                          base: np.ndarray | None = None) -> np.ndarray:
    """Multipliers that give every direct-parent class the same total weight over the given pairs.

    A pair counts for each endpoint's class with half weight: multiplier = (1/n_a + 1/n_b) / 2, where n_c is
    the (base-weighted) number of endpoint incidences of class c among the pairs. With base weights w the
    pair's final weight is w * multiplier, and each class then totals 1 (half from each endpoint).
    Unknown classes form their own class.
    """
    a = np.asarray(classes_a, dtype=object)
    b = np.asarray(classes_b, dtype=object)
    n = a.shape[0]
    base = np.ones(n) if base is None else np.asarray(base, float)
    labels, inv = np.unique(np.concatenate([a, b]).astype(str), return_inverse=True)
    counts = np.bincount(inv, weights=np.concatenate([base, base]), minlength=labels.shape[0])
    with np.errstate(divide="ignore"):
        inv_counts = np.where(counts > 0, 1.0 / counts, 0.0)
    return 0.5 * (inv_counts[inv[:n]] + inv_counts[inv[n:]])


def curve(score: np.ndarray, y: np.ndarray, w: np.ndarray, grid: np.ndarray = GRID,
          classes: tuple[np.ndarray, np.ndarray] | None = None) -> dict[str, np.ndarray]:
    """Precision, recall, n_above, Wilson bounds on the grid. With `classes`, class-balanced weights are
    recomputed among the pairs above each threshold (macro view); recall then uses weights over all
    positives of the cell."""
    score = np.nan_to_num(np.asarray(score, float), nan=-1.0)
    y = np.asarray(y, float)
    w = np.asarray(w, float)
    out = {k: np.full(grid.shape[0], np.nan) for k in ("precision", "low", "high", "recall", "n_eff")}
    out["n_above"] = np.zeros(grid.shape[0], np.int64)
    if classes is not None:
        pos_mask = y > 0
        wpos = class_balance_weights(classes[0][pos_mask], classes[1][pos_mask], w[pos_mask]) * w[pos_mask]
        denom = float(np.sum(wpos))
    else:
        denom = float(np.sum(w * y))
    if classes is None:
        # Cumulative sums over the score-descending order give every grid point in one pass.
        order = np.argsort(-score, kind="stable")
        s_sorted = score[order]
        cw = np.cumsum(w[order])
        cwy = np.cumsum((w * y)[order])
        cw2 = np.cumsum(np.square(w[order]))
        for i, t in enumerate(grid):
            n_above = int(np.searchsorted(-s_sorted, -(t - 1e-12), side="right"))
            out["n_above"][i] = n_above
            if n_above == 0:
                continue
            sw, swy, sw2 = cw[n_above - 1], cwy[n_above - 1], cw2[n_above - 1]
            if sw <= 0:
                continue
            p = swy / sw
            n_eff = sw * sw / sw2
            low, high = wilson(p * n_eff, n_eff)
            out["precision"][i], out["low"][i], out["high"][i], out["n_eff"][i] = p, low, high, n_eff
            out["recall"][i] = swy / denom if denom > 0 else np.nan
        return out
    for i, t in enumerate(grid):
        above = score >= t - 1e-12
        n_above = int(np.count_nonzero(above))
        out["n_above"][i] = n_above
        if n_above == 0:
            continue
        wa = w[above]
        if classes is not None:
            wa = wa * class_balance_weights(classes[0][above], classes[1][above], w[above])
        p, low, high = weighted_wilson(y[above], wa)
        out["precision"][i], out["low"][i], out["high"][i] = p, low, high
        out["n_eff"][i] = kish_n(wa)
        if classes is not None:
            num = float(np.sum(wpos[above[pos_mask]]))
        else:
            num = float(np.sum(w[above] * y[above]))
        out["recall"][i] = num / denom if denom > 0 else np.nan
    return out


def bootstrap_at(score, y, w, t, cluster_a, cluster_b, *, classes=None, B: int = 1000, seed: int = 0):
    """Dyadic cluster bootstrap of (precision, recall) at threshold t; returns 2.5/97.5 percentiles."""
    score = np.nan_to_num(np.asarray(score, float), nan=-1.0)
    y, w = np.asarray(y, float), np.asarray(w, float)
    labels, inv = np.unique(np.concatenate([np.asarray(cluster_a, str), np.asarray(cluster_b, str)]),
                            return_inverse=True)
    n = score.shape[0]
    ia, ib = inv[:n], inv[n:]
    rng = np.random.default_rng(seed)
    above = score >= t - 1e-12
    pos = y > 0
    rel = above | pos
    ia, ib, y_r, w_r, above_r, pos_r = ia[rel], ib[rel], y[rel], w[rel], above[rel], pos[rel]
    if classes is not None:
        cl, cinv = np.unique(np.concatenate([np.asarray(classes[0], str)[rel], np.asarray(classes[1], str)[rel]]),
                             return_inverse=True)
        n_rel = ia.shape[0]
        cia, cib = cinv[:n_rel], cinv[n_rel:]
    precision, recall = np.empty(B), np.empty(B)
    step = 32                                   # replicates per vectorised batch (memory ~ step x pairs)
    for r0 in range(0, B, step):
        nb = min(step, B - r0)
        mult = rng.poisson(1.0, (nb, labels.shape[0])).astype(np.float64)
        m = mult[:, ia] * mult[:, ib] * w_r                  # (nb, n_rel)
        if classes is not None:
            # Class-balanced weights inside each replicate: n_c = weighted endpoint incidences of class c
            # among the pairs of the set (above t for precision, positives for recall).
            def balanced(mask):
                mm = m * mask
                n_c = np.zeros((nb, cl.shape[0]))
                for k in range(nb):
                    n_c[k] = np.bincount(cia, weights=mm[k], minlength=cl.shape[0]) + \
                        np.bincount(cib, weights=mm[k], minlength=cl.shape[0])
                with np.errstate(divide="ignore", invalid="ignore"):
                    inv = np.where(n_c > 0, 1.0 / n_c, 0.0)
                return mm * 0.5 * (inv[:, cia] + inv[:, cib])
            wa = balanced(above_r)
            wp = balanced(pos_r)
            den_p = wa.sum(axis=1)
            den_r = wp.sum(axis=1)
            num_p = (wa * y_r).sum(axis=1)
            num_r = (wp * above_r).sum(axis=1)
        else:
            den_p = (m * above_r).sum(axis=1)
            num_p = (m * (above_r * y_r)).sum(axis=1)
            den_r = (m * y_r).sum(axis=1)
            num_r = (m * (above_r & pos_r)).sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            precision[r0:r0 + nb] = np.where(den_p > 0, num_p / den_p, np.nan)
            recall[r0:r0 + nb] = np.where(den_r > 0, num_r / den_r, np.nan)

    def pct(x):
        x = x[np.isfinite(x)]
        return (math.nan, math.nan) if x.size == 0 else (float(np.percentile(x, 2.5)),
                                                         float(np.percentile(x, 97.5)))

    return pct(precision), pct(recall)


def select_cutoff(c: dict[str, np.ndarray], p: float, grid: np.ndarray = GRID,
                  min_n: int = MIN_N_ABOVE) -> int | None:
    """Index of the smallest grid t with Wilson lower >= p and n_above >= min_n, else None."""
    ok = (c["low"] >= p) & (c["n_above"] >= min_n)
    ok &= np.isfinite(c["low"])
    hits = np.flatnonzero(ok)
    return int(hits[0]) if hits.size else None
