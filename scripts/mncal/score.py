"""Chunked, resumable pair scoring (Design §2, §3, §7).

One work unit is a run of anchor representatives of one adduct (sorted by precursor). For each anchor a
and every partner b after it within 400 Da with a different block, the worker computes the prefilter
bound, the Tanimoto, the class and lipid relations and, for pairs passing the prefilter, every kernel.

Each unit writes, under <run>/pairs/<mode>/:
  <unit>.kept.parquet   pairs with any kernel score >= 0.3, Tanimoto >= 0.4, or the same direct parent
                        (so every positive under every truth definition is kept), with all columns;
  <unit>.mces.parquet   the unit's bottom-k candidates per MCES stratum (ion mode x dprec bin x Tanimoto
                        bin) by a seed-fixed pair hash; merged later into a simple random sample per
                        stratum whose size is min(N_h, n_h);
  <unit>.hist.npz       score x Tanimoto 0.01-bin histograms of the pairs not kept, per (dprec bin, kernel);
  <unit>.json           counts (pairs in U and prefilter failures per dprec bin x instrument pair), written
                        last, so a unit with a .json is complete and is skipped on resume.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from . import pairs as P

KEEP_SCORE = 0.3
KEEP_TANIMOTO = 0.4
ARTEFACT_BASES = ("msdial.moddot.legacy",)
ARTEFACT_SAMPLE = 0.01
CHUNK = 64

_STATE: dict = {}


def _unit_name(adduct: str, start: int) -> str:
    safe = adduct.replace("[", "").replace("]", "").replace("+", "p").replace("-", "m")
    return f"{safe}_{start:07d}"


def plan_units(reps, chunk: int = CHUNK) -> list[tuple[str, int, int]]:
    units = []
    for adduct in sorted(reps["adduct"].unique()):
        idx = np.flatnonzero(reps["adduct"].to_numpy() == adduct)
        for s in range(0, idx.shape[0], chunk):
            units.append((adduct, s, min(s + chunk, idx.shape[0])))
    return units


def _init_worker(reps_path: str, blocks_path: str, fps_path: str, kernel_names: list[str], seed: int,
                 mces_k: int) -> None:
    from .common import below_normal_priority, limit_threads

    limit_threads()
    below_normal_priority()
    import pandas as pd

    from .kernels import default_kernels

    reps = pd.read_parquet(reps_path)
    blocks = pd.read_parquet(blocks_path)
    fps = np.load(fps_path)
    _STATE.update(reps=reps, blocks=blocks, fps=fps, kernels=default_kernels(kernel_names), seed=seed,
                  mces_k=mces_k, groups={})


def _group(adduct: str) -> dict:
    """Per-adduct arrays, prepared kernel inputs and token matrices, cached per worker."""
    if adduct in _STATE["groups"]:
        return _STATE["groups"][adduct]
    reps, blocks, fps = _STATE["reps"], _STATE["blocks"], _STATE["fps"]
    g = reps[reps["adduct"] == adduct].reset_index(drop=True)
    block_index = {b: i for i, b in enumerate(blocks["block"])}
    bidx = np.array([block_index[b] for b in g["block"]], dtype=np.int64)
    prec = g["precursor_mz"].to_numpy(np.float64)
    peaks = [np.column_stack([np.asarray(m, float), np.asarray(i, float)]) for m, i in zip(g["mz"], g["intensity"])]
    # Prefilter tokens from peaks >= 1% of base: every minimum-match kernel here (all MN preps have
    # prep.rel = 1, and the matchms reference filters at 1%) drops the others before matching.
    mz_lists = [pk[pk[:, 1] >= pk[:, 1].max() * P.PREFILTER_REL, 0] if pk.shape[0] else pk[:, 0]
                for pk in peaks]
    X, Z = P.token_matrices(prec, mz_lists)
    prepared = {k.short: [k.prepare(p, pk) for p, pk in zip(prec, peaks)] for k in _STATE["kernels"]}
    def codes(values) -> np.ndarray:
        """Integer codes per value; None/NaN -> -1 (keeps per-pair arrays numeric and small)."""
        index: dict = {}
        out = np.full(len(values), -1, np.int32)
        for i, v in enumerate(values):
            if v is None or (isinstance(v, float) and v != v):
                continue
            out[i] = index.setdefault(v, len(index))
        return out

    ce = g["ce_bin"].to_numpy(object)
    data = {
        "reps": g, "bidx": bidx, "prec": prec, "X": X, "Z": Z, "prepared": prepared,
        "keys": P.rep_keys(list(g["rep_id"])),
        "parent": codes(list(blocks["direct_parent"].to_numpy(object)[bidx])),
        "lipid": blocks["is_lipid"].to_numpy(np.int8)[bidx],
        "struct_ok": blocks["structure_ok"].to_numpy(bool)[bidx],
        "inst": codes(list(g["instrument_class"])),
        "ce": np.where(ce == "CE_UNKNOWN", -1, codes(list(ce))).astype(np.int32),
        "rep_id": g["rep_id"].to_numpy(object), "block": g["block"].to_numpy(object),
    }
    _STATE["groups"][adduct] = data
    return data


def _tanimoto(bidx_a: np.ndarray, bidx_b: np.ndarray) -> np.ndarray:
    """Tanimoto for every (a, b) cell via unique-block intersections (BLAS on 0/1 floats)."""
    fps = _STATE["fps"]
    ua, ia = np.unique(bidx_a, return_inverse=True)
    ub, ib = np.unique(bidx_b, return_inverse=True)
    bits_a = np.unpackbits(fps[ua].view(np.uint8), axis=1, bitorder="little").astype(np.float32)
    bits_b = np.unpackbits(fps[ub].view(np.uint8), axis=1, bitorder="little").astype(np.float32)
    inter = bits_a @ bits_b.T
    na, nb = bits_a.sum(axis=1), bits_b.sum(axis=1)
    union = na[:, None] + nb[None, :] - inter
    with np.errstate(invalid="ignore", divide="ignore"):
        t = np.where(union > 0, inter / union, 0.0)
    return t[ia][:, ib]


def _reference_short() -> str:
    """The modified-cosine kernel whose matched count defines strata for kernels that report none."""
    shorts = [k.short for k in _STATE["kernels"]]
    for name in ("primary", "matchms"):
        if name in shorts:
            return name
    return next(k.short for k in _STATE["kernels"] if k.reports_matches)


def _controls(g: dict, start: int, end: int):
    import pandas as pd

    bidx = g["bidx"]
    rows = []
    for a in range(start, end):
        same = np.flatnonzero(bidx == bidx[a])
        for b in same[same > a]:
            rec = {"a": g["reps"]["rep_id"].iat[a], "b": g["reps"]["rep_id"].iat[b],
                   "block": g["reps"]["block"].iat[a], "delta_mz": g["prec"][b] - g["prec"][a]}
            for kernel in _STATE["kernels"]:
                prep = g["prepared"][kernel.short]
                rec[f"s_{kernel.short}"], rec[f"m_{kernel.short}"] = kernel.score(prep[a], prep[b])
            rows.append(rec)
    return pd.DataFrame(rows) if rows else None


def run_unit(adduct: str, start: int, end: int, out_dir: str) -> dict:
    out = Path(out_dir)
    name = _unit_name(adduct, start)
    done = out / f"{name}.json"
    if done.exists():
        return json.loads(done.read_text(encoding="utf-8"))
    t0 = time.time()
    g = _group(adduct)
    prec, n = g["prec"], g["prec"].shape[0]
    hi = int(np.searchsorted(prec, prec[end - 1] + P.MAX_DPREC, side="right"))
    cols = np.arange(start + 1, hi)
    rows = slice(start, end)
    ub_full = P.prefilter_upper_bound(g["X"], g["Z"], rows, cols) if cols.size else np.zeros((end - start, 0))
    # Partners of anchor a are a+1 .. hi_a (precursor-sorted), so the pair list is built from ranges;
    # no (rows x cols) index grid and no per-pair object arrays are materialised.
    hi_each = np.searchsorted(prec, prec[start:end] + P.MAX_DPREC, side="right")
    lengths = np.maximum(hi_each - (np.arange(start, end) + 1), 0)
    a = np.repeat(np.arange(start, end, dtype=np.int32), lengths)
    offsets = np.arange(lengths.sum(), dtype=np.int64) - np.repeat(np.cumsum(lengths) - lengths, lengths)
    b = (a.astype(np.int64) + 1 + offsets).astype(np.int32)
    del offsets
    mask = g["bidx"][a] != g["bidx"][b]
    a, b = a[mask], b[mask]
    col_idx = b - (start + 1)
    ub = ub_full[a - start, col_idx]
    struct = g["struct_ok"][a] & g["struct_ok"][b]
    delta = prec[b] - prec[a]
    dbin = P.dprec_bin_index(delta)
    tan_full = _tanimoto(g["bidx"][start:end], g["bidx"][cols]) if cols.size else np.zeros((end - start, 0))
    tan = np.where(struct, tan_full[a - start, col_idx], np.nan)
    del ub_full, tan_full, col_idx
    pa, pb = g["parent"][a], g["parent"][b]
    same_parent = np.where((pa >= 0) & (pb >= 0), (pa == pb).astype(np.int8), np.int8(-1)).astype(np.int8)
    lipid_pair = (g["lipid"][a] + g["lipid"][b]).astype(np.int8)
    inst_same = g["inst"][a] == g["inst"][b]
    ce_a, ce_b = g["ce"][a], g["ce"][b]
    ce_pair = np.where((ce_a < 0) | (ce_b < 0), 2, np.where(ce_a == ce_b, 0, 1))
    passed = ub >= 2
    scores, matched = {}, {}
    # Kernels that report matched peaks run on every prefilter pass. A kernel that reports none
    # (entropy) enters only cells with >= 2 matched peaks under the reference count, so it runs only on
    # those pairs and is NaN elsewhere.
    reference = _reference_short()
    ordered = sorted(_STATE["kernels"], key=lambda k: (not k.reports_matches, k.short != reference))
    for kernel in ordered:
        s = np.zeros(a.shape[0], np.float64)
        m = np.zeros(a.shape[0], np.int32)
        prep = g["prepared"][kernel.short]
        todo = passed if kernel.reports_matches else (passed & (matched[reference] >= 2))
        if not kernel.reports_matches:
            s[passed & ~todo] = np.nan
        for i in np.flatnonzero(todo):
            s[i], m[i] = kernel.score(prep[a[i]], prep[b[i]])
        scores[kernel.short], matched[kernel.short] = s, m
    scores = {k.short: scores[k.short] for k in _STATE["kernels"]}
    matched = {k.short: matched[k.short] for k in _STATE["kernels"]}
    # Keep rule: any non-artefact kernel >= 0.3, Tanimoto >= 0.4 or the same direct parent (every positive
    # under every truth is kept). msdial.moddot.legacy scores >= 0.3 on about half of all pairs (the
    # near-1 artefact), so pairs kept only because of it are a seed-fixed Poisson sample with
    # probability ARTEFACT_SAMPLE and weight 1/ARTEFACT_SAMPLE (w_keep), which keeps its cutoffs unbiased.
    any_high = np.zeros(a.shape[0], bool)
    artefact_high = np.zeros(a.shape[0], bool)
    for kernel in _STATE["kernels"]:
        high = np.nan_to_num(scores[kernel.short], nan=0.0) >= KEEP_SCORE
        if kernel.identity.startswith(ARTEFACT_BASES):
            artefact_high |= high
        else:
            any_high |= high
    keep_main = any_high | (np.nan_to_num(tan, nan=0.0) >= KEEP_TANIMOTO) | (same_parent == 1)
    u_artefact = P.pair_uniform(g["keys"][a], g["keys"][b], _STATE["seed"] + 1)
    sampled = artefact_high & ~keep_main & (u_artefact < ARTEFACT_SAMPLE)
    keep = keep_main | sampled
    w_keep = np.where(sampled, 1.0 / ARTEFACT_SAMPLE, 1.0)

    import pandas as pd

    # Numeric columns for every pair; identifiers are attached only to the rows written out.
    frame = pd.DataFrame({
        "ia": a, "ib": b,
        "delta_mz": delta, "dbin": dbin, "prefilter_ub": ub.astype(np.int32),
        "prefilter_pass": passed, "structure_ok": struct, "tanimoto": tan, "same_parent": same_parent,
        "lipid_pair": lipid_pair, "homologue": P.homologue_flag(delta, same_parent),
        "inst_same": inst_same, "ce_pair": ce_pair.astype(np.int8),
    })
    for short in scores:
        frame[f"s_{short}"] = scores[short]
        frame[f"m_{short}"] = matched[short]
    frame["w_keep"] = w_keep
    frame["u"] = P.pair_uniform(g["keys"][a], g["keys"][b], _STATE["seed"])
    frame["tbin"] = np.where(struct, P.tanimoto_bin_index(np.nan_to_num(tan)), -1).astype(np.int8)
    def with_ids(rows):
        rows = rows.copy()
        ia, ib = rows.pop("ia").to_numpy(), rows.pop("ib").to_numpy()
        rows.insert(0, "a", g["rep_id"][ia])
        rows.insert(1, "b", g["rep_id"][ib])
        rows.insert(2, "block_a", g["block"][ia])
        rows.insert(3, "block_b", g["block"][ib])
        rows.insert(4, "adduct", adduct)
        return rows

    out.mkdir(parents=True, exist_ok=True)
    with_ids(frame[keep]).to_parquet(out / f"{name}.kept.parquet", index=False)
    k_low, k_high, whole_from = _STATE["mces_k"]
    sub = frame[struct]
    cand = pd.concat([P.bottom_k(sub[sub["tbin"] < whole_from], ["dbin", "tbin"], k_low),
                      P.bottom_k(sub[sub["tbin"] >= whole_from], ["dbin", "tbin"], k_high)])
    del sub
    with_ids(cand).to_parquet(out / f"{name}.mces.parquet", index=False)
    hist = {}
    drop = ~keep & struct
    t_idx = np.clip((np.nan_to_num(tan[drop]) * 100).astype(int), 0, 100)
    for short, s in scores.items():
        s_idx = np.clip((np.nan_to_num(s[drop]) * 100).astype(int), 0, 100)
        h = np.zeros((7, 101, 101), np.int64)
        np.add.at(h, (dbin[drop], s_idx, t_idx), 1)
        hist[short] = h
    np.savez_compressed(out / f"{name}.hist.npz", **hist)
    counts = {}
    for d in range(7):
        for same in (True, False):
            sel = (dbin == d) & (inst_same == same)
            key = f"{P.DPREC_BINS[d]}|{'same' if same else 'cross'}"
            counts[key] = {"pairs": int(sel.sum()), "prefilter_fail": int((sel & ~passed).sum()),
                           "structure_fail": int((sel & ~struct).sum())}
    kernel_counts = {}
    for short, s in scores.items():
        m = matched[short]
        kernel_counts[short] = {"ge2_matched": int(np.count_nonzero(m >= 2)),
                                "ge2_matched_score_ge_0.9": int(np.count_nonzero((m >= 2) & (s >= 0.9))),
                                "scored_hist": np.bincount(np.clip((np.nan_to_num(s[passed]) * 100).astype(int),
                                                                   0, 100), minlength=101).tolist()}
    # Positive controls: same block, same adduct, other instrument class (Design §2: reported only).
    ctrl = _controls(g, start, end)
    if ctrl is not None:
        ctrl.to_parquet(out / f"{name}.controls.parquet", index=False)
    tb = frame["tbin"].to_numpy()
    mces_strata = {f"{P.DPREC_BINS[d]}|{t}": int(np.count_nonzero(struct & (dbin == d) & (tb == t)))
                   for d in range(7) for t in range(5)}
    record = {"unit": name, "adduct": adduct, "start": start, "end": end, "pairs": int(a.shape[0]),
              "scored": int(passed.sum()), "kept": int(keep.sum()), "counts": counts,
              "mces_strata_N": mces_strata, "kernel_counts": kernel_counts,
              "controls": 0 if ctrl is None else int(len(ctrl)),
              "seconds": round(time.time() - t0, 3), "pid": os.getpid()}
    tmp = done.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    os.replace(tmp, done)
    return record


def score_all(reps_path: Path, blocks_path: Path, fps_path: Path, out_dir: Path, kernel_names: list[str],
              *, workers: int, seed: int, mces_k: int, max_units: int | None = None,
              progress=print) -> list[dict]:
    import pandas as pd

    from .common import MAX_WORKERS

    workers = max(1, min(workers, MAX_WORKERS))
    reps = pd.read_parquet(reps_path, columns=["adduct", "precursor_mz"])
    units = plan_units(reps)
    if max_units is not None:
        units = units[:max_units]
    results = []
    init = (str(reps_path), str(blocks_path), str(fps_path), kernel_names, seed, mces_k)
    if workers == 1:
        _init_worker(*init)
        for i, u in enumerate(units):
            results.append(run_unit(*u, str(out_dir)))
            progress(f"unit {i + 1}/{len(units)} {results[-1]['unit']} pairs={results[-1]['pairs']}")
        return results
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=init) as pool:
        futures = {pool.submit(run_unit, *u, str(out_dir)): u for u in units}
        for i, fut in enumerate(as_completed(futures)):
            results.append(fut.result())
            if (i + 1) % 20 == 0 or i + 1 == len(units):
                progress(f"{i + 1}/{len(units)} units")
    return sorted(results, key=lambda r: r["unit"])
