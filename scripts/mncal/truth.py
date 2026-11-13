"""MCES sample assembly and computation (Design §4).

The per-unit bottom-k candidates are merged into one simple random sample per stratum
(ion mode x dprec bin x Tanimoto bin): the n_h pairs with the smallest seed-fixed pair hash, with
n_h = min(N_h, k_low) below the "whole" Tanimoto bins and min(N_h, k_high) from them up. The HT weight of a
sampled pair is N_h / n_h. MCES runs in a process pool (<= 16 workers, BelowNormal, one CBC thread each)
with results checkpointed per chunk, so the job resumes.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from . import pairs as P


def stratum_totals(unit_records: list[dict]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for record in unit_records:
        for key, value in record["mces_strata_N"].items():
            totals[key] = totals.get(key, 0) + value
    return totals


def merge_sample(pairs_dir: Path, unit_records: list[dict], k_low: int, k_high: int,
                 whole_from: int) -> pd.DataFrame:
    files = sorted(pairs_dir.glob("*.mces.parquet"))
    cand = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True) if files else pd.DataFrame()
    if cand.empty:
        return cand
    totals = stratum_totals(unit_records)
    parts = []
    for (d, t), group in cand.groupby(["dbin", "tbin"], sort=True):
        k = k_high if t >= whole_from else k_low
        chosen = group.sort_values("u", kind="mergesort").head(k).copy()
        N = totals.get(f"{P.DPREC_BINS[d]}|{t}", 0)
        chosen["mces_N_h"] = N
        chosen["mces_n_h"] = len(chosen)
        chosen["ht_weight"] = N / len(chosen) if len(chosen) else np.nan
        parts.append(chosen)
    return pd.concat(parts, ignore_index=True)


_SMILES: dict[str, str] = {}


def _init(smiles: dict[str, str], tmp_dir: str) -> None:
    from .structures import mces_worker_init

    mces_worker_init(tmp_dir)
    _SMILES.update(smiles)


def _mces_chunk(rows: list[tuple[str, str]], out_path: str) -> str:
    from .structures import mces_pair

    out = Path(out_path)
    if out.exists():
        return out_path
    results = []
    for block_a, block_b in rows:
        t0 = time.time()
        try:
            distance, mode, _ = mces_pair(_SMILES[block_a], _SMILES[block_b])
            error = None
        except Exception as exc:  # recorded per pair, never fatal for the chunk
            distance, mode, error = np.nan, -1, f"{type(exc).__name__}"
        results.append({"block_a": block_a, "block_b": block_b, "mces": distance, "mces_mode": mode,
                        "mces_seconds": round(time.time() - t0, 4), "mces_error": error})
    frame = pd.DataFrame(results)
    tmp = out.with_suffix(".tmp")
    frame.to_parquet(tmp, index=False)
    tmp.replace(out)
    return out_path


def load_previous(dirs: list[Path], smiles: dict[str, str], previous_smiles: list[dict[str, str]]) -> pd.DataFrame:
    """MCES results of earlier runs, kept only for block pairs whose standard SMILES are unchanged."""
    frames = []
    for d, prev in zip(dirs, previous_smiles):
        for f in sorted(Path(d).glob("*.parquet")):
            m = pd.read_parquet(f)
            m = m[m["mces_error"].isna()]
            same = [smiles.get(x) is not None and smiles.get(x) == prev.get(x) and
                    smiles.get(y) is not None and smiles.get(y) == prev.get(y)
                    for x, y in zip(m["block_a"], m["block_b"])]
            frames.append(m[np.asarray(same, bool)])
    if not frames:
        return pd.DataFrame(columns=["block_a", "block_b", "mces", "mces_mode", "mces_seconds", "mces_error"])
    out = pd.concat(frames, ignore_index=True)
    return out.drop_duplicates(["block_a", "block_b"])


def compute_mces(sample: pd.DataFrame, block_smiles_std: dict[str, str], out_dir: Path, *, workers: int,
                 tmp_dir: Path, chunk: int = 50, limit: int | None = None, known: pd.DataFrame | None = None,
                 progress=print) -> pd.DataFrame:
    from .common import MAX_WORKERS

    out_dir.mkdir(parents=True, exist_ok=True)
    pairs = sample[["block_a", "block_b"]].drop_duplicates()
    pairs = pairs.assign(lo=np.minimum(pairs.block_a, pairs.block_b), hi=np.maximum(pairs.block_a, pairs.block_b))
    pairs = pairs[["lo", "hi"]].drop_duplicates().sort_values(["lo", "hi"])
    reused = pd.DataFrame()
    if known is not None and not known.empty:
        key = set(zip(known["block_a"], known["block_b"]))
        hit = np.array([(lo, hi) in key for lo, hi in zip(pairs["lo"], pairs["hi"])], bool)
        wanted = set(map(tuple, pairs[hit].to_numpy().tolist()))
        reused = known[[k in wanted for k in zip(known["block_a"], known["block_b"])]]
        pairs = pairs[~hit]
    pairs = pairs.to_numpy().tolist()
    if limit is not None:
        pairs = pairs[:limit]
    chunks = [pairs[i:i + chunk] for i in range(0, len(pairs), chunk)]
    needed = {b for p in pairs for b in p}
    smiles = {b: block_smiles_std[b] for b in needed}
    workers = max(1, min(workers, MAX_WORKERS))
    # A chunk is named by the hash of its own pair list, so a resume with another chunk size or another
    # reused set never reads a stale chunk.
    import hashlib

    paths = [str(out_dir / ("mces_" + hashlib.sha256("\n".join(f"{x}\t{y}" for x, y in c).encode()).hexdigest()[:16]
                            + ".parquet")) for c in chunks]
    todo = [(c, p) for c, p in zip(chunks, paths) if not Path(p).exists()]
    t0 = time.time()
    if todo:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(smiles, str(tmp_dir))) as pool:
            futures = [pool.submit(_mces_chunk, c, p) for c, p in todo]
            for i, fut in enumerate(as_completed(futures)):
                fut.result()
                if (i + 1) % 10 == 0 or i + 1 == len(futures):
                    progress(f"mces {i + 1}/{len(futures)} chunks, {time.time() - t0:.0f}s")
    frames = [pd.read_parquet(p) for p in paths if Path(p).exists()]
    if not reused.empty:
        frames.append(reused.assign(mces_reused=True))
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if "mces_reused" in out:
        out["mces_reused"] = out["mces_reused"].fillna(False).astype(bool)
    out.attrs["chunks_computed_now"] = len(todo)
    out.attrs["reused_pairs"] = int(len(reused))
    return out


def attach_mces(sample: pd.DataFrame, mces: pd.DataFrame) -> pd.DataFrame:
    lo = np.minimum(sample.block_a, sample.block_b)
    hi = np.maximum(sample.block_a, sample.block_b)
    keyed = sample.assign(lo=lo, hi=hi)
    m = mces.rename(columns={"block_a": "lo", "block_b": "hi"})
    return keyed.merge(m, on=["lo", "hi"], how="left").drop(columns=["lo", "hi"])


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")
