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
    if kind == "tanimoto07":
        frame = kept[kept["structure_ok"]]
        return frame, (frame["tanimoto"].to_numpy() >= 0.7).astype(float), np.ones(len(frame))
    if kind == "same_parent":
        frame = kept[kept["same_parent"] >= 0]
        return frame, (frame["same_parent"].to_numpy() == 1).astype(float), np.ones(len(frame))
    raise ValueError(kind)


def _fmt_count(n: float, suppress_small: bool) -> object:
    n = int(round(n))
    if suppress_small and 1 <= n < 10:
        return "<10"
    return n


def cell_rows(frame: pd.DataFrame, y: np.ndarray, w: np.ndarray, *, kernel: dict, truth: str, bias: str,
              base: dict, matched_col: str, min_matched: int, B: int, seed: int,
              suppress_small: bool, parents: tuple[np.ndarray, np.ndarray]) -> list[dict]:
    s_col = f"s_{kernel['short']}"
    sel = frame[matched_col].to_numpy() >= min_matched
    if bias == "no_lipid_lipid":
        sel &= frame["lipid_pair"].to_numpy() != 2
    f = frame[sel]
    ys, ws = y[sel], w[sel]
    score = f[s_col].to_numpy(float)
    classes = (parents[0][sel], parents[1][sel]) if bias == "class_balanced" else None
    c = M.curve(score, ys, ws, classes=classes)
    if not truth.startswith("mces"):
        below = M.GRID < EXHAUSTIVE_MIN_T - 1e-9
        for key in ("precision", "low", "high", "recall", "n_eff"):
            c[key][below] = np.nan
    n_analog = float(np.sum(ws * ys))
    rows = []
    for p in TARGETS:
        idx = M.select_cutoff(c, p)
        row = dict(base, kernel_identity=kernel["identity"], implementation=kernel["implementation"],
                   min_matched=min_matched, truth_definition=truth, bias_view=bias, target_precision=p,
                   n_analog_total=_fmt_count(n_analog, suppress_small), bootstrap_b=B,
                   population=_fmt_count(len(f), suppress_small))
        boot = None
        while idx is not None:
            t = float(M.GRID[idx])
            boot = M.bootstrap_at(score, ys, ws, t, f["block_a"].to_numpy(), f["block_b"].to_numpy(),
                                  classes=classes, B=B, seed=seed)
            if math.isfinite(boot[0][0]) and boot[0][0] >= p:
                break
            nxt = np.flatnonzero((c["low"] >= p) & (c["n_above"] >= M.MIN_N_ABOVE) & (np.arange(len(M.GRID)) > idx))
            idx = int(nxt[0]) if nxt.size else None
        if idx is None:
            row.update(threshold=None, precision=None, precision_ci_low=None, precision_ci_high=None,
                       recall=None, recall_ci_low=None, recall_ci_high=None, n_pairs_above=None, estimable=0,
                       wilson_low=None, n_eff_above=None)
        else:
            (plo, phi), (rlo, rhi) = boot
            row.update(threshold=float(M.GRID[idx]), precision=float(c["precision"][idx]), precision_ci_low=plo,
                       precision_ci_high=phi, recall=float(c["recall"][idx]), recall_ci_low=rlo,
                       recall_ci_high=rhi, n_pairs_above=_fmt_count(c["n_above"][idx], suppress_small),
                       estimable=1, wilson_low=float(c["low"][idx]), n_eff_above=float(c["n_eff"][idx]))
        rows.append(row)
    return rows


def build_table(kept: pd.DataFrame, sample: pd.DataFrame, kernels: list[dict], *, library: str, ion_mode: str,
                parent_of: dict[str, str | None], ref_matched_short: str, B: int = 1000, seed: int = 0,
                suppress_small: bool = False, adducts: bool = True, instrument: bool = True,
                progress=print) -> pd.DataFrame:
    rows: list[dict] = []
    for truth in TRUTHS:
        frame, y, w = truth_population(truth, kept, sample)
        if frame.empty:
            continue
        pa = np.array([parent_of.get(b) or "unknown" for b in frame["block_a"]], dtype=object)
        pb = np.array([parent_of.get(b) or "unknown" for b in frame["block_b"]], dtype=object)
        dbins = frame["dbin"].to_numpy()
        addu = frame["adduct"].to_numpy()
        inst = frame["inst_same"].to_numpy()
        cells = [("all", "all", "all", np.ones(len(frame), bool))]
        cells += [("all", P.DPREC_BINS[d], "all", dbins == d) for d in range(7)]
        if adducts:
            cells += [(a, "all", "all", addu == a) for a in sorted(set(addu))]
        if instrument:
            cells += [("all", "all", "same", inst), ("all", "all", "cross", ~inst)]
        for kernel in kernels:
            matched_col = f"m_{kernel['short']}" if kernel["reports_matches"] else f"m_{ref_matched_short}"
            for adduct, dbin, ipair, mask in cells:
                if not mask.any():
                    continue
                base = {"library": library, "ion_mode": ion_mode, "adduct": adduct, "dprec_bin": dbin,
                        "instrument_pair": ipair}
                sub, ys, ws = frame[mask], y[mask], w[mask]
                for min_matched in P.MIN_MATCHED_FLOORS:
                    for bias in BIAS_VIEWS:
                        rows.extend(cell_rows(sub, ys, ws, kernel=kernel, truth=truth, bias=bias, base=base,
                                              matched_col=matched_col, min_matched=min_matched, B=B, seed=seed,
                                              suppress_small=suppress_small, parents=(pa[mask], pb[mask])))
            progress(f"calibrate {ion_mode} {truth} {kernel['short']}: {len(rows)} rows")
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
