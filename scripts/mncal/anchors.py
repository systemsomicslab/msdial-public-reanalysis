"""Anchor sampling and the anchor pilot that sizes a VS21 run (Design §2, "Sizing").

VS21 is too large to enumerate (about 1e10 same-adduct pairs within 400 Da), so a VS21 run scores A anchor
representatives against every representative in U. A is set from a pilot of 500 anchors per ion mode by the
rule

    wall time = pairs_after_prefilter x t_pair / 16 <= 8 h per ion mode.

Anchor selection (`select_anchors`):
- Eligible anchors are representatives whose block has a structure.
- Every eligible representative gets a seed-fixed hash. At most 200 per ClassyFire direct parent are kept
  (the 200 lowest hashes; an unknown parent is one class), which curbs lipid dominance.
- The n anchors with the lowest hashes are taken from that capped pool. This is proportional allocation, in
  expectation, over the strata (adduct, instrument class, direct parent); the realised composition is recorded.

Pilot (`run_pilot`), per anchor:
- partners are every representative of the same adduct with |Delta prec| <= 400 Da and a different block, in
  both directions (U as in score-pairs, seen from the anchor);
- the pilot counts pairs in U and pairs passing the prefilter (the same exact superset of ">= 2 matched");
- it scores a seed-fixed `score_fraction` of the passing pairs with every kernel and records the kernel seconds.
  Kernel inputs are prepared lazily, only for representatives that are scored, so worker memory stays small at
  VS21 scale; preparation time is recorded apart from scoring time.

The result is aggregate only: counts per anchor summarised by mode, t_pair, and the A that meets the rule.
"""

from __future__ import annotations

import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

from . import pairs as P

PILOT_ANCHORS = 500
MAX_PER_PARENT = 200
WALL_HOURS = 8.0
WORKERS = 16

_S: dict = {}


def select_anchors(reps, blocks, n: int, seed: int, max_per_parent: int = MAX_PER_PARENT):
    """Row indices (into reps) of the anchors, in hash order; see the module docstring."""
    import pandas as pd

    from .common import stable_hash01

    info = blocks.set_index("block")
    ok = reps["block"].map(info["structure_ok"]).fillna(False).to_numpy(bool)
    parent = reps["block"].map(info["direct_parent"]).astype(object).where(lambda s: s.notna(), "unknown")
    h = np.array([stable_hash01("anchor", r, seed=seed) for r in reps["rep_id"]])
    frame = pd.DataFrame({"row": np.arange(len(reps)), "h": h, "parent": parent.to_numpy(object)})[ok]
    frame = frame.sort_values(["parent", "h"], kind="mergesort")
    capped = frame.groupby("parent", sort=False).head(max_per_parent)
    return capped.sort_values("h", kind="mergesort")["row"].to_numpy()[:n]


def _init(reps_path: str, kernel_names: list[str], seed: int, score_fraction: float) -> None:
    from .common import below_normal_priority, limit_threads

    limit_threads()
    below_normal_priority()
    import pandas as pd

    from .kernels import default_kernels

    _S.update(reps=pd.read_parquet(reps_path), kernels=default_kernels(kernel_names), seed=seed,
              fraction=score_fraction, groups={})


def _group(adduct: str) -> dict:
    if adduct in _S["groups"]:
        return _S["groups"][adduct]
    reps = _S["reps"]
    rows = np.flatnonzero(reps["adduct"].to_numpy() == adduct)
    g = reps.iloc[rows].sort_values("precursor_mz", kind="mergesort")
    prec = g["precursor_mz"].to_numpy(np.float64)
    peaks = [np.column_stack([np.asarray(m, float), np.asarray(i, float)]) for m, i in zip(g["mz"], g["intensity"])]
    mz_lists = [pk[pk[:, 1] >= pk[:, 1].max() * P.PREFILTER_REL, 0] if pk.shape[0] else pk[:, 0] for pk in peaks]
    X, Z = P.token_matrices(prec, mz_lists)
    data = {"row": g.index.to_numpy(), "prec": prec, "peaks": peaks, "X": X, "Z": Z,
            "block": g["block"].to_numpy(object), "keys": P.rep_keys(list(g["rep_id"])),
            "prepared": {k.short: {} for k in _S["kernels"]}}
    data["pos_of_row"] = {int(r): i for i, r in enumerate(data["row"])}
    _S["groups"][adduct] = data
    return data


def _prepared(g: dict, kernel, i: int):
    cache = g["prepared"][kernel.short]
    if i not in cache:
        cache[i] = kernel.prepare(g["prec"][i], g["peaks"][i])
    return cache[i]


def _anchor(adduct: str, rep_row: int) -> dict:
    g = _group(adduct)
    i = g["pos_of_row"][int(rep_row)]
    prec = g["prec"]
    lo = int(np.searchsorted(prec, prec[i] - P.MAX_DPREC, side="left"))
    hi = int(np.searchsorted(prec, prec[i] + P.MAX_DPREC, side="right"))
    cols = np.arange(lo, hi)
    cols = cols[(cols != i) & (g["block"][cols] != g["block"][i])]
    t0 = time.perf_counter()
    ub = P.prefilter_upper_bound(g["X"], g["Z"], slice(i, i + 1), cols)[0] if cols.size else np.zeros(0)
    t_prefilter = time.perf_counter() - t0
    passed = cols[ub >= 2]
    u = P.pair_uniform(np.repeat(g["keys"][i], passed.size), g["keys"][passed], _S["seed"] + 7)
    sample = passed[u < _S["fraction"]]
    t_prep = 0.0
    t_kernel = {}
    for kernel in _S["kernels"]:
        t0 = time.perf_counter()
        prep_a = _prepared(g, kernel, i)
        preps = [_prepared(g, kernel, j) for j in sample]
        t_prep += time.perf_counter() - t0
        t0 = time.perf_counter()
        for pb in preps:
            kernel.score(prep_a, pb)
        t_kernel[kernel.short] = time.perf_counter() - t0
    return {"adduct": adduct, "pairs_u": int(cols.size), "pairs_pass": int(passed.size),
            "pairs_scored": int(sample.size), "t_prefilter": t_prefilter, "t_prepare": t_prep,
            "t_kernel": t_kernel}


def _batch(adduct: str, rows: list[int]) -> list[dict]:
    return [_anchor(adduct, r) for r in rows]


def run_pilot(reps_path, anchor_rows: np.ndarray, adducts: np.ndarray, kernel_names: list[str], *, seed: int,
              score_fraction: float, workers: int, progress=print) -> list[dict]:
    from .common import MAX_WORKERS

    workers = max(1, min(workers, MAX_WORKERS))
    jobs: dict[str, list[int]] = {}
    for r, a in zip(anchor_rows, adducts):
        jobs.setdefault(str(a), []).append(int(r))
    batches = [(a, rows[k:k + 8]) for a, rows in sorted(jobs.items()) for k in range(0, len(rows), 8)]
    out: list[dict] = []
    with ProcessPoolExecutor(max_workers=workers, initializer=_init,
                             initargs=(str(reps_path), kernel_names, seed, score_fraction)) as pool:
        futures = [pool.submit(_batch, a, rows) for a, rows in batches]
        for k, f in enumerate(as_completed(futures), 1):
            out.extend(f.result())
            if k % 10 == 0 or k == len(futures):
                progress(f"anchor pilot: {k}/{len(futures)} batches")
    return out


def summarise(results: list[dict], kernel_names: list[str], *, wall_hours: float = WALL_HOURS,
              workers: int = WORKERS) -> dict:
    """t_pair (seconds per scored pair, all kernels) and the A that keeps the anchor run within the wall time."""
    n = len(results)
    if not n:
        return {"anchors": 0}
    pass_ = np.array([r["pairs_pass"] for r in results], float)
    u = np.array([r["pairs_u"] for r in results], float)
    scored = sum(r["pairs_scored"] for r in results)
    t_kernel = {k: sum(r["t_kernel"].get(k, 0.0) for r in results) for k in kernel_names}
    t_pair = sum(t_kernel.values()) / scored if scored else None
    t_prefilter_per_anchor = float(np.mean([r["t_prefilter"] for r in results]))
    per_anchor_seconds = (pass_.mean() * t_pair + t_prefilter_per_anchor) if t_pair else None
    a_max = int(wall_hours * 3600 * workers / per_anchor_seconds) if per_anchor_seconds else None
    return {
        "anchors": n,
        "pairs_u_per_anchor": {"mean": float(u.mean()), "median": float(np.median(u)), "p90": float(np.quantile(u, 0.9))},
        "pairs_pass_per_anchor": {"mean": float(pass_.mean()), "median": float(np.median(pass_)),
                                  "p90": float(np.quantile(pass_, 0.9))},
        "prefilter_pass_share": float(pass_.sum() / u.sum()) if u.sum() else None,
        "pairs_scored": int(scored),
        "t_pair_seconds_all_kernels": t_pair,
        "t_pair_seconds_by_kernel": {k: (v / scored if scored else None) for k, v in t_kernel.items()},
        "t_prefilter_seconds_per_anchor": t_prefilter_per_anchor,
        "t_prepare_seconds_total": float(sum(r["t_prepare"] for r in results)),
        "seconds_per_anchor_single_worker": per_anchor_seconds,
        "rule": f"A x seconds_per_anchor / {workers} <= {wall_hours} h",
        "anchors_max_within_rule": a_max,
    }
