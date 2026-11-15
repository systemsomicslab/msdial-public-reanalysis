"""Cutoff table, curves and sanity numbers from scored pairs and structural truth (Design §5).

Populations per truth definition:
  tanimoto07   every structure-resolved pair (exhaustive, w = 1); y = Tanimoto >= 0.7.
  same_parent  every pair whose two blocks both have a direct parent (exhaustive); y = same parent.
  mces2/3/5    the stratified MCES sample with HT weights N_h / n_h; y = MCES <= k (exact below 10).
Exhaustive cells read the kept pairs only, which hold every pair with any score >= 0.3 and every positive,
so precision and n above any t >= 0.3 and every recall are exact; the grid therefore starts at 0.30 for
exhaustive truth (thresholds below 0.3 are reported as not estimable, never extrapolated).

A min_matched = k cell holds the pairs with >= k matched peaks under the kernel's own matched count; a
kernel that reports none (entropy) uses the run's modified-cosine reference count, named in run.json.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import metrics as M
from . import pairs as P

TRUTHS = ("mces3", "mces2", "mces5", "tanimoto07", "same_parent")
BIAS_VIEWS = ("all", "no_lipid_lipid", "class_balanced")
TARGETS = (0.5, 0.7, 0.8, 0.9)
EXHAUSTIVE_MIN_T = 0.30
COLUMNS = ["kernel_identity", "implementation", "library", "ion_mode", "adduct", "dprec_bin", "min_matched",
           "instrument_pair", "truth_definition", "bias_view", "target_precision", "threshold", "precision",
           "precision_ci_low", "precision_ci_high", "recall", "recall_ci_low", "recall_ci_high",
           "n_pairs_above", "n_analog_total", "estimable",
           # trailing columns (not in the spec's list; consumers read the columns above by name)
           "wilson_low", "n_eff_above", "bootstrap_b", "population"]


def truth_population(kind: str, kept: pd.DataFrame, sample: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray,
                                                                                np.ndarray]:
    if kind.startswith("mces"):
        k = int(kind[4:])
        frame = sample[sample["mces"].notna() & (sample["mces_mode"] >= 0)]
        y = (frame["mces"].to_numpy() <= k).astype(float)
        return frame, y, frame["ht_weight"].to_numpy(float)
    def keep_weight(frame):
        # 1 for pairs kept by the main rule; 1/pi for pairs kept only by the moddot-artefact Poisson
        # sample. Those never are positives and score < 0.3 under every other kernel, so the weight
        # changes nothing for the other kernels' cells.
        return frame["w_keep"].to_numpy(float) if "w_keep" in frame else np.ones(len(frame))

    if kind == "tanimoto07":
        frame = kept[kept["structure_ok"]]
        return frame, (frame["tanimoto"].to_numpy() >= 0.7).astype(float), keep_weight(frame)
    if kind == "same_parent":
        frame = kept[kept["same_parent"] >= 0]
        return frame, (frame["same_parent"].to_numpy() == 1).astype(float), keep_weight(frame)
    raise ValueError(kind)


def _fmt_count(n: float, suppress_small: bool) -> object:
    n = int(round(n))
    if suppress_small and 1 <= n < 10:
        return "<10"
    return n


def _labels_codes(col) -> tuple[np.ndarray, np.ndarray]:
    s = col if isinstance(col, pd.Series) else pd.Series(col)
    if isinstance(s.dtype, pd.CategoricalDtype):
        return np.asarray(s.cat.categories.astype(str), dtype=str), s.cat.codes.to_numpy().astype(np.int64)
    labels, inv = np.unique(s.astype(str).to_numpy(dtype=str), return_inverse=True)
    return labels, inv.astype(np.int64)


def block_codes(col_a, col_b) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """String-rank codes of the two block columns: integer order equals the labels' string order, so the
    bootstrap draws the same multiplier per block as it would from the strings."""
    la, ca = _labels_codes(col_a)
    lb, cb = _labels_codes(col_b)
    labels = np.union1d(la, lb)
    return np.searchsorted(labels, la)[ca], np.searchsorted(labels, lb)[cb], labels


def cell_rows(score: np.ndarray, bins: np.ndarray, y: np.ndarray, w: np.ndarray,
              blocks: tuple[np.ndarray, np.ndarray], classes: tuple[np.ndarray, np.ndarray] | None, *,
              kernel: dict, truth: str, bias: str, base: dict, min_matched: int, B: int, seed: int,
              suppress_small: bool) -> list[dict]:
    """Rows of one cell (already restricted to its pairs, matched floor and bias view) for every target.

    The cutoff rule per target is unchanged: the smallest grid t with Wilson lower >= p and n >= 30, moved up
    the grid until the cluster-bootstrap lower bound also reaches p. One bootstrap pass from the smallest
    candidate t gives the interval at every t above it, identical to a separate evaluation at each t.
    """
    c = M.curve(score, y, w, classes=classes, bins=bins)
    if not truth.startswith("mces"):
        below = M.GRID < EXHAUSTIVE_MIN_T - 1e-9
        for key in ("precision", "low", "high", "recall", "n_eff"):
            c[key][below] = np.nan
    n_analog = float(np.sum(w * y))
    start = {p: M.select_cutoff(c, p) for p in TARGETS}
    found = [i for i in start.values() if i is not None]
    boot = None
    if found:
        boot = M.bootstrap_curve(score, y, w, min(found), blocks[0], blocks[1], classes=classes, B=B, seed=seed,
                                 bins=bins)
    rows = []
    for p in TARGETS:
        row = dict(base, kernel_identity=kernel["identity"], implementation=kernel["implementation"],
                   min_matched=min_matched, truth_definition=truth, bias_view=bias, target_precision=p,
                   n_analog_total=_fmt_count(n_analog, suppress_small), bootstrap_b=B,
                   population=_fmt_count(len(score), suppress_small))
        idx = start[p]
        while idx is not None:
            lo = boot["precision_low"][idx]
            if math.isfinite(lo) and lo >= p:
                break
            nxt = np.flatnonzero((c["low"] >= p) & (c["n_above"] >= M.MIN_N_ABOVE) & (np.arange(len(M.GRID)) > idx))
            idx = int(nxt[0]) if nxt.size else None
        if idx is None:
            row.update(threshold=None, precision=None, precision_ci_low=None, precision_ci_high=None,
                       recall=None, recall_ci_low=None, recall_ci_high=None, n_pairs_above=None, estimable=0,
                       wilson_low=None, n_eff_above=None)
        else:
            row.update(threshold=float(M.GRID[idx]), precision=float(c["precision"][idx]),
                       precision_ci_low=float(boot["precision_low"][idx]),
                       precision_ci_high=float(boot["precision_high"][idx]), recall=float(c["recall"][idx]),
                       recall_ci_low=float(boot["recall_low"][idx]), recall_ci_high=float(boot["recall_high"][idx]),
                       n_pairs_above=_fmt_count(c["n_above"][idx], suppress_small), estimable=1,
                       wilson_low=float(c["low"][idx]), n_eff_above=float(c["n_eff"][idx]))
        rows.append(row)
    return rows


def build_table(kept: pd.DataFrame, sample: pd.DataFrame, kernels: list[dict], *, library: str, ion_mode: str,
                parent_of: dict[str, str | None], ref_matched_short: str, B: int = 1000, seed: int = 0,
                suppress_small: bool = False, adducts: bool = True, instrument: bool = True,
                progress=print) -> pd.DataFrame:
    import time

    rows: list[dict] = []
    for truth in TRUTHS:
        frame, y, w = truth_population(truth, kept, sample)
        if frame.empty:
            continue
        t_truth = time.time()
        # Everything a cell needs as plain arrays, built once per truth: string-rank block codes (cluster
        # bootstrap), direct-parent class codes ("unknown" is a class of its own), cell keys.
        ba, bb, labels = block_codes(frame["block_a"], frame["block_b"])
        parent_labels = np.array([parent_of.get(b) or "unknown" for b in labels], dtype=str)
        _, parent_code = np.unique(parent_labels, return_inverse=True)
        pa, pb = parent_code[ba], parent_code[bb]
        dbins = frame["dbin"].to_numpy()
        addu = frame["adduct"].astype(str).to_numpy()
        inst = frame["inst_same"].to_numpy().astype(bool)
        not_lipid_pair = frame["lipid_pair"].to_numpy() != 2
        cells = [("all", "all", "all", np.ones(len(frame), bool))]
        cells += [("all", P.DPREC_BINS[d], "all", dbins == d) for d in range(7)]
        if adducts:
            cells += [(a, "all", "all", addu == a) for a in sorted(set(addu))]
        if instrument:
            cells += [("all", "all", "same", inst), ("all", "all", "cross", ~inst)]
        for kernel in kernels:
            t_kernel = time.time()
            matched_col = f"m_{kernel['short']}" if kernel["reports_matches"] else f"m_{ref_matched_short}"
            score = frame[f"s_{kernel['short']}"].to_numpy(float)
            bins = M.grid_bins(score)
            matched = frame[matched_col].to_numpy()
            for adduct, dbin, ipair, mask in cells:
                if not mask.any():
                    continue
                base = {"library": library, "ion_mode": ion_mode, "adduct": adduct, "dprec_bin": dbin,
                        "instrument_pair": ipair}
                for min_matched in P.MIN_MATCHED_FLOORS:
                    floor = mask & (matched >= min_matched)
                    for bias in BIAS_VIEWS:
                        sel = floor & not_lipid_pair if bias == "no_lipid_lipid" else floor
                        idx = np.flatnonzero(sel)
                        classes = (pa[idx], pb[idx]) if bias == "class_balanced" else None
                        rows.extend(cell_rows(score[idx], bins[idx], y[idx], w[idx], (ba[idx], bb[idx]), classes,
                                              kernel=kernel, truth=truth, bias=bias, base=base,
                                              min_matched=min_matched, B=B, seed=seed,
                                              suppress_small=suppress_small))
            progress(f"calibrate {ion_mode} {truth} {kernel['short']}: {len(rows)} rows, "
                     f"{len(frame)} pairs, {time.time() - t_kernel:.0f}s")
        progress(f"calibrate {ion_mode} {truth}: done in {time.time() - t_truth:.0f}s")
    table = pd.DataFrame(rows)
    for col in COLUMNS:
        if col not in table:
            table[col] = None
    return table[COLUMNS]


def canonical_tsv(table: pd.DataFrame) -> str:
    """Deterministic TSV text: fixed column order, sorted rows, floats at 6 decimals, empty for None."""
    def fmt(v):
        if v is None or (isinstance(v, float) and not math.isfinite(v)):
            return ""
        if isinstance(v, float):
            return f"{v:.6f}"
        return str(v)

    keys = ["kernel_identity", "ion_mode", "adduct", "dprec_bin", "instrument_pair", "min_matched",
            "truth_definition", "bias_view", "target_precision"]
    t = table.copy()
    t = t.sort_values(keys, kind="mergesort")
    lines = ["\t".join(COLUMNS)]
    for rec in t.to_dict("records"):
        lines.append("\t".join(fmt(rec[c]) for c in COLUMNS))
    return "\n".join(lines) + "\n"


def sanity(kept: pd.DataFrame, kernels: list[dict], controls: pd.DataFrame | None) -> dict:
    out: dict = {}
    for kernel in kernels:
        s = kept[f"s_{kernel['short']}"].to_numpy(float)
        m = kept[f"m_{kernel['short']}"].to_numpy() if kernel["reports_matches"] else None
        entry = {"identity": kernel["identity"]}
        if m is not None:
            sel = m >= 2
            entry["share_ge_0.9_among_ge2_matched_kept"] = float(np.mean(s[sel] >= 0.9)) if sel.any() else None
            entry["n_ge2_matched_kept"] = int(sel.sum())
        entry["median_different_block_kept"] = float(np.nanmedian(s)) if s.size else None
        if controls is not None and not controls.empty and f"s_{kernel['short']}" in controls:
            entry["median_same_block_control"] = float(np.nanmedian(controls[f"s_{kernel['short']}"]))
            entry["n_same_block_control"] = int(len(controls))
        out[kernel["short"]] = entry
    return out
