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
    Unknown classes form their own class. Classes may be strings or integer codes.
    """
    ca, cb, n_classes = _codes(classes_a, classes_b)
    n = ca.shape[0]
    base = np.ones(n) if base is None else np.asarray(base, float)
    counts = np.bincount(np.concatenate([ca, cb]), weights=np.concatenate([base, base]), minlength=n_classes)
    with np.errstate(divide="ignore"):
        inv_counts = np.where(counts > 0, 1.0 / counts, 0.0)
    return 0.5 * (inv_counts[ca] + inv_counts[cb])


def grid_bins(score: np.ndarray, grid: np.ndarray = GRID) -> np.ndarray:
    """Bin k of a score is the number of grid thresholds it reaches (s >= t - 1e-12); NaN counts as below 0.

    A pair in bin k is above grid[i] exactly when k > i, so every grid point's sums are suffix sums of
    per-bin sums, and one O(n) pass replaces one pass per threshold.
    """
    s = np.nan_to_num(np.asarray(score, float), nan=-1.0)
    return np.searchsorted(grid - 1e-12, s, side="right").astype(np.int64)


def _suffix(a: np.ndarray, axis: int = 0) -> np.ndarray:
    """Suffix sums along an axis: out[k] = sum over j >= k."""
    return np.flip(np.cumsum(np.flip(a, axis=axis), axis=axis), axis=axis)


def _codes(classes_a, classes_b) -> tuple[np.ndarray, np.ndarray, int]:
    """Integer class codes for the two endpoints; integer input is used as is."""
    a, b = np.asarray(classes_a), np.asarray(classes_b)
    if a.dtype.kind in "iu" and b.dtype.kind in "iu":
        n = int(max(a.max(initial=-1), b.max(initial=-1))) + 1
        return a.astype(np.int64), b.astype(np.int64), max(n, 1)
    labels, inv = np.unique(np.concatenate([a, b]).astype(str), return_inverse=True)
    return inv[:a.shape[0]].astype(np.int64), inv[a.shape[0]:].astype(np.int64), max(labels.shape[0], 1)


def curve(score: np.ndarray, y: np.ndarray, w: np.ndarray, grid: np.ndarray = GRID,
          classes: tuple[np.ndarray, np.ndarray] | None = None,
          bins: np.ndarray | None = None) -> dict[str, np.ndarray]:
    """Precision, recall, n_above, Wilson bounds on the grid. With `classes`, class-balanced weights are
    recomputed among the pairs above each threshold (macro view); recall then uses weights over all
    positives of the cell.

    Every quantity is a suffix sum of per-bin sums (see grid_bins). For the class-balanced view the pair
    weight above t is w (1/n_a(t) + 1/n_b(t)) / 2, so its sums decompose over classes and class pairs:
      sum wa      = K(t) / 2                     K = classes with n_c(t) > 0
      sum wa y    = sum_c Y_c(t) / n_c(t) / 2
      sum wa^2    = (sum_c Q_c(t) / n_c(t)^2 + 2 sum_(c,d) X_cd(t) / (n_c(t) n_d(t))) / 4
    with n_c, Y_c, Q_c the endpoint-incidence sums of w, w y and w^2, and X_cd the pair sums of w^2.
    `classes` may be strings or integer codes; `bins` may be passed when already computed.
    """
    y = np.asarray(y, float)
    w = np.asarray(w, float)
    b = grid_bins(score, grid) if bins is None else np.asarray(bins, np.int64)
    G = grid.shape[0]
    nb = G + 1
    out = {k: np.full(G, np.nan) for k in ("precision", "low", "high", "recall", "n_eff")}
    cnt = _suffix(np.bincount(b, minlength=nb).astype(np.int64))[1:]
    out["n_above"] = cnt.astype(np.int64)
    if classes is None:
        denom = float(np.sum(w * y))
        sw = _suffix(np.bincount(b, weights=w, minlength=nb))[1:]
        swy = _suffix(np.bincount(b, weights=w * y, minlength=nb))[1:]
        sw2 = _suffix(np.bincount(b, weights=w * w, minlength=nb))[1:]
        for i in range(G):
            if cnt[i] == 0 or sw[i] <= 0:
                continue
            p = swy[i] / sw[i]
            n_eff = sw[i] * sw[i] / sw2[i]
            low, high = wilson(p * n_eff, n_eff)
            out["precision"][i], out["low"][i], out["high"][i], out["n_eff"][i] = p, low, high, n_eff
            out["recall"][i] = swy[i] / denom if denom > 0 else np.nan
        return out

    ca, cb, C = _codes(*classes)
    pos = y > 0
    wpos = class_balance_weights(ca[pos], cb[pos], w[pos]) * w[pos]
    denom = float(np.sum(wpos))
    rec_num = _suffix(np.bincount(b[pos], weights=wpos, minlength=nb))[1:]

    def per_bin_class(values):
        flat = np.bincount(b * C + ca, weights=values, minlength=nb * C) + \
            np.bincount(b * C + cb, weights=values, minlength=nb * C)
        return _suffix(flat.reshape(nb, C))

    N = per_bin_class(w)
    Y = per_bin_class(w * y)
    Q = per_bin_class(w * w)
    cp_u, cp_inv = np.unique(ca * C + cb, return_inverse=True)
    X = _suffix(np.bincount(b * cp_u.shape[0] + cp_inv, weights=w * w,
                            minlength=nb * cp_u.shape[0]).reshape(nb, cp_u.shape[0]))
    cp_a, cp_b = cp_u // C, cp_u % C
    for i in range(G):
        if cnt[i] == 0:
            continue
        n = N[i + 1]
        with np.errstate(divide="ignore"):
            inv = np.where(n > 0, 1.0 / n, 0.0)
        s1 = 0.5 * float(np.count_nonzero(n > 0))
        if s1 <= 0:
            continue
        s1y = 0.5 * float(Y[i + 1] @ inv)
        s2 = 0.25 * (float(Q[i + 1] @ (inv * inv)) + 2.0 * float(X[i + 1] @ (inv[cp_a] * inv[cp_b])))
        p = s1y / s1
        n_eff = 0.0 if s2 == 0 else s1 * s1 / s2
        low, high = wilson(p * n_eff, n_eff)
        out["precision"][i], out["low"][i], out["high"][i], out["n_eff"][i] = p, low, high, n_eff
        out["recall"][i] = rec_num[i] / denom if denom > 0 else np.nan
    return out


def _cluster_index(cluster_a, cluster_b) -> tuple[np.ndarray, np.ndarray, int]:
    """Dense cluster indices in the sorted order of the cluster labels present.

    Integer codes must already sort like the labels they stand for (calibrate passes string-rank codes),
    so a replicate draws the same multiplier for the same block as a string-labelled call would.
    """
    a, b = np.asarray(cluster_a), np.asarray(cluster_b)
    if a.dtype.kind in "iu" and b.dtype.kind in "iu":
        both = np.concatenate([a, b]).astype(np.int64)
        present = np.flatnonzero(np.bincount(both, minlength=1) > 0)
        lookup = np.full(int(both.max(initial=0)) + 1, -1, np.int64)
        lookup[present] = np.arange(present.shape[0])
        return lookup[a], lookup[b], present.shape[0]
    labels, inv = np.unique(np.concatenate([a.astype(str), b.astype(str)]), return_inverse=True)
    return inv[:a.shape[0]], inv[a.shape[0]:], labels.shape[0]


def _quantile_cols(reps: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Per-column quantile q[g] of the finite values of reps[:, g], numpy's default linear interpolation."""
    G = reps.shape[1]
    out = np.full(G, np.nan)
    srt = np.sort(reps, axis=0)                                   # NaN sorts last
    n = np.isfinite(reps).sum(axis=0)
    ok = (n > 0) & np.isfinite(q)
    if not ok.any():
        return out
    cols = np.flatnonzero(ok)
    h = q[cols] * (n[cols] - 1)
    lo = np.floor(h).astype(np.int64)
    hi = np.minimum(lo + 1, n[cols] - 1)
    frac = h - lo
    a, b = srt[lo, cols], srt[hi, cols]
    out[cols] = a + (b - a) * frac
    return out


def bc_interval(reps: np.ndarray, theta: np.ndarray, level: float = 0.95) -> tuple[np.ndarray, np.ndarray]:
    """Bias-corrected (BC) percentile interval per column (Efron 1981; no acceleration term).

    z0 = Phi^-1(share of finite replicates below the point estimate, ties counting half), clipped to
    [0.5/n, 1 - 0.5/n] so that it stays finite; the bounds are the replicate quantiles at
    Phi(2 z0 -/+ z_(1-alpha/2)). With z0 = 0 this is the plain percentile interval. A column whose point
    estimate is not finite falls back to the plain percentile interval.
    """
    from scipy.special import ndtr, ndtri

    reps = np.asarray(reps, float)
    theta = np.asarray(theta, float)
    alpha = (1.0 - level) / 2.0
    finite = np.isfinite(reps)
    n = finite.sum(axis=0)
    tol = 1e-12 * np.maximum(1.0, np.abs(np.nan_to_num(theta)))
    with np.errstate(invalid="ignore"):
        below = (finite & (reps < theta - tol)).sum(axis=0)
        ties = (finite & (np.abs(reps - theta) <= tol)).sum(axis=0)
    share = np.where(n > 0, (below + 0.5 * ties) / np.maximum(n, 1), 0.5)
    share = np.clip(share, 0.5 / np.maximum(n, 1), 1.0 - 0.5 / np.maximum(n, 1))
    z0 = np.where(np.isfinite(theta) & (n > 0), ndtri(share), 0.0)
    q_lo = ndtr(2.0 * z0 + ndtri(alpha))
    q_hi = ndtr(2.0 * z0 + ndtri(1.0 - alpha))
    q_lo = np.where(z0 == 0.0, alpha, q_lo)                      # exactly the plain percentile when z0 = 0
    q_hi = np.where(z0 == 0.0, 1.0 - alpha, q_hi)
    return _quantile_cols(reps, q_lo), _quantile_cols(reps, q_hi)


def bootstrap_curve(score, y, w, i0: int, cluster_a, cluster_b, *, classes=None, B: int = 1000, seed: int = 0,
                    grid: np.ndarray = GRID, bins: np.ndarray | None = None,
                    interval: str = "bc") -> dict[str, np.ndarray]:
    """Dyadic cluster bootstrap of (precision, recall) at every grid index >= i0, in one pass.

    Returns 95% interval arrays (NaN below i0): bias-corrected percentile (`interval="bc"`, the default since
    2026-11-16, see bc_interval) or plain 2.5/97.5 percentiles (`"percentile"`, the method before).
    The point estimate BC centres on is the multiplier-1 evaluation of the same formulas, which equals
    curve()'s estimate. A replicate draws one Poisson(1) multiplier per
    cluster present in the population, in sorted label order and batches of 32, so for any t = grid[i],
    i >= i0, the interval equals a replicate-by-replicate evaluation at t alone with the same seed.
    Only pairs above grid[i0] or positive enter the sums; the others are zero for every i >= i0.

    Class-balanced view: the class set is fixed per replicate. The point estimate averages a per-class ratio
    over the K(t) classes present above t (precision) or over the K_P classes with a positive (recall). A
    replicate averages over the same classes; a class whose blocks all drew a zero multiplier contributes its
    observed ratio instead of dropping out. (Until 2026-11-15 such classes dropped out, so a class held by a
    single block vanished from about 37% of replicates and the interval shifted away from the point estimate.)
    """
    import warnings

    y, w = np.asarray(y, float), np.asarray(w, float)
    b = grid_bins(score, grid) if bins is None else np.asarray(bins, np.int64)
    G = grid.shape[0]
    nb = G + 1
    ia, ib, n_labels = _cluster_index(cluster_a, cluster_b)
    rng = np.random.default_rng(seed)
    pos = y > 0
    rel = (b > i0) | pos
    ia, ib, y_r, w_r = ia[rel], ib[rel], y[rel], w[rel]
    # Compact bins: everything at or below bin i0 (positives only) folds into bin 0, bin j is bin i0 + j, so
    # bin 1 + (i - i0) holds the pairs first reached at grid index i.
    b_r = np.maximum(b[rel] - i0, 0)
    nb = G + 1 - i0
    n_rel = ia.shape[0]
    precision = np.full((B, G), np.nan)
    recall = np.full((B, G), np.nan)
    sl = slice(1, nb)
    if classes is not None:
        ca, cb, _ = _codes(*classes)
        present, compact = np.unique(np.concatenate([ca[rel], cb[rel]]), return_inverse=True)
        C = max(present.shape[0], 1)
        ca, cb = compact[:n_rel], compact[n_rel:]
        # endpoint incidences sorted by (bin, class): one reduceat per replicate batch gives the per-bin,
        # per-class sums of the replicate weights
        key = np.concatenate([b_r * C + ca, b_r * C + cb])
        pair_of = np.concatenate([np.arange(n_rel), np.arange(n_rel)])
    else:
        C = 1
        key = b_r
        pair_of = np.arange(n_rel)
    order = np.argsort(key, kind="stable")
    key_sorted = key[order]
    pair_of = pair_of[order]
    starts = np.flatnonzero(np.r_[True, key_sorted[1:] != key_sorted[:-1]]) if key_sorted.size else np.zeros(0, int)
    keys = key_sorted[starts]
    ia_s, ib_s, w_s, y_s = ia[pair_of], ib[pair_of], w_r[pair_of], y_r[pair_of]
    if interval not in ("bc", "percentile"):
        raise ValueError(f"interval={interval!r}")
    theta_p = np.full(G, np.nan)
    theta_r = np.full(G, np.nan)
    if classes is None and n_rel:
        flat0 = np.zeros(nb)
        flat0y = np.zeros(nb)
        flat0[keys] = np.add.reduceat(w_s, starts)
        flat0y[keys] = np.add.reduceat(w_s * y_s, starts)
        N0p, Y0p = _suffix(flat0), _suffix(flat0y)
        with np.errstate(divide="ignore", invalid="ignore"):
            theta_p[i0:] = np.where(N0p[sl] > 0, Y0p[sl] / N0p[sl], np.nan)
            theta_r[i0:] = np.where(Y0p[0] > 0, Y0p[sl] / Y0p[0], np.nan)
    if classes is not None and n_rel:
        # observed per-class sums (multiplier 1): the fixed class sets and the ratios an absent class keeps
        flat0_m = np.zeros(nb * C)
        flat0_y = np.zeros(nb * C)
        flat0_m[keys] = np.add.reduceat(w_s, starts)
        flat0_y[keys] = np.add.reduceat(w_s * y_s, starts)
        N0 = _suffix(flat0_m.reshape(nb, C), axis=0)
        Y0 = _suffix(flat0_y.reshape(nb, C), axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            n0 = N0[sl]
            present0 = n0 > 0                                    # (thresholds, classes)
            k0 = present0.sum(axis=1).astype(float)
            ratio0_p = np.where(present0, Y0[sl] / np.where(present0, n0, 1.0), 0.0)
            P0 = Y0[0]
            pos0 = P0 > 0
            kp0 = float(pos0.sum())
            ratio0_r = np.where(pos0[None, :], Y0[sl] / np.where(pos0, P0, 1.0)[None, :], 0.0)
            theta_p[i0:] = np.where(k0 > 0, ratio0_p.sum(axis=1) / np.maximum(k0, 1.0), np.nan)
            theta_r[i0:] = ratio0_r.sum(axis=1) / kp0 if kp0 > 0 else np.nan
    step = 32
    for r0 in range(0, B, step):
        nbat = min(step, B - r0)
        mult = rng.poisson(1.0, (nbat, n_labels)).astype(np.float64)
        if n_rel == 0:
            continue
        m = mult[:, ia_s] * mult[:, ib_s] * w_s                 # (nbat, incidences), sorted by key
        flat_m = np.zeros((nbat, nb * C))
        flat_y = np.zeros((nbat, nb * C))
        flat_m[:, keys] = np.add.reduceat(m, starts, axis=1)
        flat_y[:, keys] = np.add.reduceat(m * y_s, starts, axis=1)
        N = _suffix(flat_m.reshape(nbat, nb, C), axis=1)
        Y = _suffix(flat_y.reshape(nbat, nb, C), axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            if classes is not None:
                # precision(t) = sum_c Y_c(t) / n_c(t) / K(t); recall(t) = sum_c Y_c(t) / P_c / K_P, P_c over
                # every positive (the halves cancel). K(t) and K_P are the observed class sets; a class absent
                # from the replicate keeps its observed ratio.
                P = Y[:, 0, :]
                invP = np.where(P > 0, 1.0 / P, 0.0)
                n = N[:, sl, :]
                inv = np.where(n > 0, 1.0 / n, 0.0)
                num_p = np.einsum("rgc,rgc->rg", Y[:, sl, :], inv) + \
                    np.einsum("rgc,gc->rg", ((n <= 0) & present0[None]).astype(float), ratio0_p)
                precision[r0:r0 + nbat, i0:] = np.where(k0[None, :] > 0, num_p / np.maximum(k0, 1.0)[None, :],
                                                        np.nan)
                num_r = np.einsum("rgc,rc->rg", Y[:, sl, :], invP) + \
                    np.einsum("rc,gc->rg", ((P <= 0) & pos0[None]).astype(float), ratio0_r)
                recall[r0:r0 + nbat, i0:] = num_r / kp0 if kp0 > 0 else np.nan
            else:
                N, Y = N[:, :, 0], Y[:, :, 0]
                den_r = Y[:, 0]
                precision[r0:r0 + nbat, i0:] = np.where(N[:, sl] > 0, Y[:, sl] / N[:, sl], np.nan)
                recall[r0:r0 + nbat, i0:] = np.where(den_r[:, None] > 0, Y[:, sl] / den_r[:, None], np.nan)
    out = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)          # all-NaN columns stay NaN
        if interval == "bc":
            out["precision_low"], out["precision_high"] = bc_interval(precision, theta_p)
            out["recall_low"], out["recall_high"] = bc_interval(recall, theta_r)
        else:
            out["precision_low"], out["precision_high"] = np.nanpercentile(precision, [2.5, 97.5], axis=0)
            out["recall_low"], out["recall_high"] = np.nanpercentile(recall, [2.5, 97.5], axis=0)
    for key in out:
        out[key][:i0] = np.nan
    return out


def bootstrap_at(score, y, w, t, cluster_a, cluster_b, *, classes=None, B: int = 1000, seed: int = 0,
                 grid: np.ndarray = GRID, interval: str = "bc"):
    """Dyadic cluster bootstrap of (precision, recall) at grid threshold t; returns 2.5/97.5 percentiles."""
    i = int(np.argmin(np.abs(grid - t)))
    if abs(float(grid[i]) - float(t)) > 1e-9:
        raise ValueError(f"t={t} is not a grid point")
    c = bootstrap_curve(score, y, w, i, cluster_a, cluster_b, classes=classes, B=B, seed=seed, grid=grid,
                        interval=interval)
    return ((float(c["precision_low"][i]), float(c["precision_high"][i])),
            (float(c["recall_low"][i]), float(c["recall_high"][i])))


def select_cutoff(c: dict[str, np.ndarray], p: float, grid: np.ndarray = GRID,
                  min_n: int = MIN_N_ABOVE) -> int | None:
    """Index of the smallest grid t with Wilson lower >= p and n_above >= min_n, else None."""
    ok = (c["low"] >= p) & (c["n_above"] >= min_n)
    ok &= np.isfinite(c["low"])
    hits = np.flatnonzero(ok)
    return int(hits[0]) if hits.size else None
