"""mncal command line: build-reps, score-pairs, structure-truth, calibrate, report, check-kernels, guard.

Run with the D5 interpreter and PYTHONPATH=<worktree>\\scripts;<catalog pin>\\src. Every step reads and
extends <run>/run.json (charter C7): inputs with sha256, code commit, catalog pin, environment, parameters.
Private (VS21) runs keep representatives, pairs and samples under <run>/_local-vs21/ and record libraries by
file name and sha256 only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import common as C

MODES = {"Positive": "pos", "Negative": "neg"}


# ------------------------------------------------------------------------------------------- run.json
def _run_json(run: Path) -> dict:
    path = run / "run.json"
    return C.read_json(path) if path.exists() else {}


def _update_run(run: Path, section: str, payload: dict) -> None:
    record = _run_json(run)
    record.setdefault("run_id", run.name)
    record.setdefault("benchmark_id", f"mn-calibration/{run.name}")
    record["code"] = C.code_commit()
    record["catalog_pin"] = C.catalog_pin()
    record["environment"] = C.environment_record()
    record.setdefault("steps", {})[section] = payload | {"finished_at": C.now_iso()}
    C.write_json(run / "run.json", record)


def _data_dir(run: Path) -> Path:
    private = _run_json(run).get("private", False)
    return run / "_local-vs21" if private else run


# ------------------------------------------------------------------------------------------- build-reps
def cmd_build_reps(args) -> int:
    import numpy as np

    from . import library as L
    from . import structures as S

    run = Path(args.run)
    record = _run_json(run)
    if record.get("steps", {}).get("build-reps") and not args.force:
        print("build-reps already done; --force to redo")
        return 0
    record.update(private=bool(args.private), library_label=args.library_label)
    run.mkdir(parents=True, exist_ok=True)
    C.write_json(run / "run.json", record)
    data = _data_dir(run)
    data.mkdir(parents=True, exist_ok=True)
    libraries, selection = [], {}
    for db in args.db:
        t0 = time.time()
        reps, report, lib = L.select_representatives(db, block_fraction=args.block_fraction, seed=args.seed)
        modes = sorted({r["ion_mode"] for r in reps})
        if len(modes) != 1:
            print(f"{Path(db).name}: expected one ion mode, found {modes}", file=sys.stderr)
            return 2
        mode = MODES[modes[0]]
        blocks = {r["block"] for r in reps}
        rows, fps, failures = S.build_blocks(L.block_smiles(db, blocks), L.block_inchikeys(db, blocks), args.icd)
        import pandas as pd

        L.reps_to_table(reps).to_pandas().to_parquet(data / f"reps_{mode}.parquet", index=False)
        pd.DataFrame(rows).to_parquet(data / f"blocks_{mode}.parquet", index=False)
        np.save(data / f"fps_{mode}.npy", fps)
        entry = {k: lib[k] for k in ("library_name", "library_version", "sha256", "byte_size", "record_count",
                                     "license", "source_uri")}
        entry["ion_mode"] = modes[0]
        if args.msp:
            match = [m for m in args.msp if f"-{mode}-" in Path(m).name.lower()]
            for msp in match[:1]:
                entry["file_name"] = Path(msp).name
                entry["md5"] = C.md5_file(msp)
                entry["file_sha256"] = C.sha256_file(msp)
                zr = Path(msp).with_name("zenodo-record.json")
                if zr.exists():
                    z = C.read_json(zr)
                    entry["zenodo"] = {"record_id": z.get("record_id"), "doi": z.get("doi"), "md5": z.get("md5")}
                    entry["md5_matches_zenodo"] = z.get("md5") == entry["md5"]
        libraries.append(entry)
        from collections import Counter

        by_adduct = Counter((r["adduct"], r["instrument_class"]) for r in reps)
        parent_sources = Counter(r["parent_source"] for r in rows)
        selection[mode] = {
            "records": report.records, "excluded": report.excluded, "eligible_records": report.candidates,
            "representatives": report.representatives, "blocks": len(rows),
            "blocks_structure_failed": failures, "parent_source": dict(parent_sources),
            "reps_by_adduct_instrument": {f"{a}|{i}": n for (a, i), n in sorted(by_adduct.items())},
            "selection_digest": L.selection_digest(reps), "seconds": round(time.time() - t0, 1),
        }
        print(f"{mode}: {report.representatives} representatives over {len(rows)} blocks "
              f"({time.time() - t0:.0f}s)")
    icd_info = None
    if args.icd and Path(args.icd).exists():
        icd_info = {"file_name": Path(args.icd).name, "sha256": C.sha256_file(args.icd)}
    from .structures import LIPID_MAP_FILE, tanimoto_identity

    _update_run(run, "build-reps", {
        "libraries": libraries, "selection": selection, "block_fraction": args.block_fraction, "seed": args.seed,
        "icd": icd_info, "tanimoto_identity": tanimoto_identity(),
        "lipid_map": {"file": LIPID_MAP_FILE.name, "sha256": C.sha256_text_file(LIPID_MAP_FILE)
                      if LIPID_MAP_FILE.exists() else None},
        "eligibility": {"adducts": sorted(L.ALL_ADDUCTS), "min_peaks_after_prep": L.MIN_PEAKS,
                        "prep": L.ELIGIBILITY_PREP},
    })
    return 0


# ------------------------------------------------------------------------------------------- check-kernels
def cmd_check_kernels(args) -> int:
    from . import kernels as K

    result: dict = {"ws2_available": K.ws2_available(), "primary": K.PRIMARY}
    if K.ws2_available():
        from msdial_spectrum_catalog import similarity_mn as mn

        result["primary_round_trip"] = mn.canonical_kernel_id(K.PRIMARY) == K.PRIMARY
    else:
        result["primary_round_trip"] = "blocked: WS2 similarity_mn not in the catalog pin"
    if args.ws3_identity:
        result["primary_equals_ws3"] = args.ws3_identity.strip() == K.PRIMARY
    kernels = K.default_kernels(args.kernels)
    result["kernels"] = [{"identity": k.identity, "implementation": k.implementation,
                          "score_convention": k.score_convention, "parity": k.parity} for k in kernels]
    fixtures = args.fixtures
    if fixtures is None:
        import msdial_spectrum_catalog

        fixtures = Path(msdial_spectrum_catalog.__file__).resolve().parents[2] / "similarity-fixtures"
    result["fixture_parity"] = K.fixture_parity(kernels, fixtures)
    print(json.dumps(result, indent=2))
    if args.run:
        _update_run(Path(args.run), "check-kernels", result)
    bad = [i for i, v in result["fixture_parity"].items() if v.get("status") == "fail"]
    return 1 if bad else 0


# ------------------------------------------------------------------------------------------- score-pairs
def cmd_score_pairs(args) -> int:
    from . import kernels as K
    from . import score as SC

    run = Path(args.run)
    data = _data_dir(run)
    free = C.free_ram_gb()
    if free is not None and free < args.min_free_ram_gb:
        print(f"free RAM {free:.0f} GB < {args.min_free_ram_gb} GB; not starting (C8)", file=sys.stderr)
        return 3
    kernels = K.default_kernels(args.kernels)
    summary = dict(_run_json(run).get("steps", {}).get("score-pairs", {}).get("summary", {}))
    for mode in args.modes:
        t0 = time.time()
        out = data / "pairs" / mode
        results = SC.score_all(data / f"reps_{mode}.parquet", data / f"blocks_{mode}.parquet",
                               data / f"fps_{mode}.npy", out, args.kernels or [k.short for k in kernels],
                               workers=args.workers, seed=args.seed,
                               mces_k=(args.mces_k_low, args.mces_k_high, args.mces_whole_from),
                               max_units=args.max_units)
        summary[mode] = {"units": len(results), "pairs": sum(r["pairs"] for r in results),
                         "scored": sum(r["scored"] for r in results), "kept": sum(r["kept"] for r in results),
                         "wall_seconds": round(time.time() - t0, 1),
                         "cpu_seconds_in_units": round(sum(r["seconds"] for r in results), 1)}
        print(f"{mode}: {summary[mode]}")
    _update_run(run, "score-pairs", {
        "kernels": [{"short": k.short, "identity": k.identity, "implementation": k.implementation,
                     "score_convention": k.score_convention, "reports_matches": k.reports_matches,
                     "parity": k.parity} for k in kernels],
        "workers": args.workers, "seed": args.seed, "priority": "BelowNormal",
        "keep_rule": {"any_score_ge": SC.KEEP_SCORE, "tanimoto_ge": SC.KEEP_TANIMOTO, "same_parent": True},
        "mces_sampling": {"k_low": args.mces_k_low, "k_high": args.mces_k_high,
                          "whole_from_tanimoto_bin": args.mces_whole_from},
        "max_units": args.max_units, "summary": summary,
    })
    return 0


def _unit_records(pairs_dir: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(pairs_dir.glob("*.json"))]


# ------------------------------------------------------------------------------------------- structure-truth
def cmd_structure_truth(args) -> int:
    import pandas as pd

    from . import truth as T
    from .structures import mces_identity

    run = Path(args.run)
    data = _data_dir(run)
    record = _run_json(run)
    sp = record["steps"]["score-pairs"]["mces_sampling"]
    previous = record["steps"].get("structure-truth", {}).get("summary", {})
    summary = dict(previous)             # one invocation per mode must not drop the other mode's record
    for mode in args.modes:
        pairs_dir = data / "pairs" / mode
        records = _unit_records(pairs_dir)
        sample = T.merge_sample(pairs_dir, records, sp["k_low"], sp["k_high"], sp["whole_from_tanimoto_bin"])
        blocks = pd.read_parquet(data / f"blocks_{mode}.parquet")
        smiles = dict(zip(blocks["block"], blocks["smiles_std"]))
        t0 = time.time()
        mces = T.compute_mces(sample, smiles, data / "mces" / mode, workers=args.workers,
                              tmp_dir=data / "_tmp" / mode, limit=args.limit)
        wall = time.time() - t0
        joined = T.attach_mces(sample, mces) if not mces.empty else sample.assign(mces=float("nan"), mces_mode=-1)
        joined.to_parquet(data / f"mces_sample_{mode}.parquet", index=False)
        strata = sample.groupby(["dbin", "tbin"]).agg(N_h=("mces_N_h", "first"), n_h=("mces_n_h", "first"))
        computed = int(joined["mces"].notna().sum()) if "mces" in joined else 0
        summary[mode] = {"sample_pairs": int(len(sample)), "mces_computed": computed,
                         "unique_block_pairs": int(len(mces)), "wall_seconds": round(wall, 1),
                         "pairs_per_second": round(len(mces) / wall, 3) if wall > 0 and len(mces) else None,
                         "median_seconds_per_pair": float(mces["mces_seconds"].median()) if len(mces) else None,
                         "errors": int(mces["mces_error"].notna().sum()) if len(mces) else 0,
                         "strata": {f"{int(d)}|{int(t)}": {"N_h": int(r.N_h), "n_h": int(r.n_h)}
                                    for (d, t), r in strata.iterrows()},
                         "limit": args.limit}
        if mces.attrs.get("chunks_computed_now", 0) == 0 and mode in previous:
            # Nothing new was computed: keep the throughput measured when the work was done.
            for key in ("wall_seconds", "pairs_per_second"):
                summary[mode][key] = previous[mode].get(key)
        print(f"{mode}: {json.dumps({k: v for k, v in summary[mode].items() if k != 'strata'})}")
    _update_run(run, "structure-truth", {"mces_identity": mces_identity(), "workers": args.workers,
                                         "summary": summary})
    return 0


# ------------------------------------------------------------------------------------------- calibrate
def cmd_calibrate(args) -> int:
    import numpy as np
    import pandas as pd

    from . import calibrate as CAL

    run = Path(args.run)
    data = _data_dir(run)
    record = _run_json(run)
    private = record.get("private", False)
    kernels = record["steps"]["score-pairs"]["kernels"]
    if args.only_kernels:
        kernels = [k for k in kernels if k["short"] in args.only_kernels]
    ref_short = next((k["short"] for k in kernels if k["short"] in ("primary", "matchms")), kernels[0]["short"])
    tables, sanity, counts = [], {}, {}
    for mode in args.modes:
        pairs_dir = data / "pairs" / mode
        kept = pd.concat([pd.read_parquet(f) for f in sorted(pairs_dir.glob("*.kept.parquet"))], ignore_index=True)
        sample_path = data / f"mces_sample_{mode}.parquet"
        sample = pd.read_parquet(sample_path) if sample_path.exists() else kept.iloc[0:0].assign(
            mces=np.nan, mces_mode=-1, ht_weight=np.nan)
        if "mces" not in sample:
            sample = sample.assign(mces=np.nan, mces_mode=-1)
        blocks = pd.read_parquet(data / f"blocks_{mode}.parquet")
        parent_of = dict(zip(blocks["block"], blocks["direct_parent"]))
        ion_mode = {"pos": "Positive", "neg": "Negative"}[mode]
        table = CAL.build_table(kept, sample, kernels, library=record.get("library_label", "VS20"),
                                ion_mode=ion_mode, parent_of=parent_of, ref_matched_short=ref_short,
                                B=args.bootstrap, seed=args.seed, suppress_small=private)
        tables.append(table)
        ctrl_files = sorted(pairs_dir.glob("*.controls.parquet"))
        controls = pd.concat([pd.read_parquet(f) for f in ctrl_files], ignore_index=True) if ctrl_files else None
        sanity[mode] = CAL.sanity(kept, kernels, controls)
        records = _unit_records(pairs_dir)
        for k in kernels:
            kc = [r["kernel_counts"][k["short"]] for r in records if k["short"] in r.get("kernel_counts", {})]
            ge2 = sum(x["ge2_matched"] for x in kc)
            hi = sum(x["ge2_matched_score_ge_0.9"] for x in kc)
            hist = np.sum([x["scored_hist"] for x in kc], axis=0) if kc else np.zeros(101)
            fails = sum(sum(v["prefilter_fail"] for v in r["counts"].values()) for r in records)
            full = hist.copy()
            full[0] += fails
            cum = np.cumsum(full)
            median_bin = int(np.searchsorted(cum, cum[-1] / 2.0)) if cum[-1] else None
            sanity[mode][k["short"]].update({
                "share_ge_0.9_among_ge2_matched_all": (hi / ge2) if ge2 else None, "n_ge2_matched_all": ge2,
                "median_different_block_all_U_bin": None if median_bin is None else median_bin / 100.0})
        counts[mode] = _stratum_counts(records)
    table = pd.concat(tables, ignore_index=True)
    text = CAL.canonical_tsv(table)
    out = run / "cutoff_table.tsv"
    out.write_text(text, encoding="utf-8", newline="\n")
    digest = C.sha256_text(text)
    if "primary" in {k["short"] for k in kernels} and "matchms" in {k["short"] for k in kernels}:
        for mode in args.modes:
            kept = pd.concat([pd.read_parquet(f, columns=["s_primary", "s_matchms"])
                              for f in sorted((data / "pairs" / mode).glob("*.kept.parquet"))])
            from scipy.stats import spearmanr

            sanity[mode]["spearman_matchms_vs_primary_kept"] = float(spearmanr(kept.s_matchms, kept.s_primary)[0])
    if args.curves:
        _curves(run, data, kernels, args.modes, ref_short)
    _update_run(run, "calibrate", {"table": out.name, "table_sha256": digest, "rows": int(len(table)),
                                   "estimable_rows": int((table["estimable"] == 1).sum()),
                                   "bootstrap_b": args.bootstrap, "seed": args.seed,
                                   "matched_reference_for_non_matching_kernels": ref_short,
                                   "sanity": sanity, "stratum_counts": counts})
    print(f"cutoff_table.tsv rows={len(table)} sha256={digest}")
    return 0


def _stratum_counts(records: list[dict]) -> dict:
    out: dict = {}
    for r in records:
        for key, v in r["counts"].items():
            k = f"{r['adduct']}|{key}"
            slot = out.setdefault(k, {"pairs": 0, "prefilter_fail": 0, "structure_fail": 0})
            for f in slot:
                slot[f] += v[f]
    return out


def _curves(run: Path, data: Path, kernels: list[dict], modes: list[str], ref_short: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd

    from . import calibrate as CAL
    from . import metrics as M

    out = run / "curves"
    out.mkdir(exist_ok=True)
    for mode in modes:
        kept = pd.concat([pd.read_parquet(f) for f in sorted((data / "pairs" / mode).glob("*.kept.parquet"))],
                         ignore_index=True)
        sample_path = data / f"mces_sample_{mode}.parquet"
        sample = pd.read_parquet(sample_path) if sample_path.exists() else kept.iloc[0:0]
        for truth in ("mces3", "tanimoto07", "same_parent"):
            if truth.startswith("mces") and ("mces" not in sample or sample["mces"].notna().sum() == 0):
                continue
            frame, y, w = CAL.truth_population(truth, kept, sample)
            fig, ax = plt.subplots(figsize=(6, 4.5))
            for k in kernels:
                mcol = f"m_{k['short']}" if k["reports_matches"] else f"m_{ref_short}"
                sel = frame[mcol].to_numpy() >= 2
                c = M.curve(frame[f"s_{k['short']}"].to_numpy(float)[sel], y[sel], w[sel])
                lo = 0.0 if truth.startswith("mces") else CAL.EXHAUSTIVE_MIN_T
                g = M.GRID >= lo
                ax.plot(M.GRID[g], c["precision"][g], label=f"{k['short']} precision")
                ax.plot(M.GRID[g], c["recall"][g], ls="--", label=f"{k['short']} recall")
                centres, fitted, _ = M.isotonic_curve(np.nan_to_num(frame[f"s_{k['short']}"].to_numpy(float)[sel]),
                                                      y[sel], w[sel])
                ax.plot(centres, fitted, ls=":", label=f"{k['short']} P(analog|s) PAVA")
            ax.set_xlabel("score threshold t")
            ax.set_ylim(0, 1)
            ax.set_title(f"{mode} {truth}, >=2 matched, all pairs")
            ax.legend(fontsize=7)
            fig.tight_layout()
            fig.savefig(out / f"{mode}_{truth}.png", dpi=120)
            plt.close(fig)


# ------------------------------------------------------------------------------------------- report
def cmd_report(args) -> int:
    from .report import write_run_report

    path = write_run_report(Path(args.run))
    print(path)
    return 0


# ------------------------------------------------------------------------------------------- guard
def cmd_guard(args) -> int:
    from . import guard

    return guard.run(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mncal")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build-reps", help="Select representatives and per-block structures")
    p.add_argument("--run", required=True)
    p.add_argument("--db", action="append", required=True, help="reference DB (catalog ingest-reference-library)")
    p.add_argument("--msp", action="append", help="the MSP files, for md5/sha256 provenance only (paths not recorded)")
    p.add_argument("--icd", help="InchikeyClassyfireDB-VS5.icd")
    p.add_argument("--library-label", default="VS20")
    p.add_argument("--private", action="store_true", help="VS21: data under _local-vs21, aggregates only")
    p.add_argument("--block-fraction", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=20261113)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_build_reps)

    p = sub.add_parser("check-kernels", help="Identity round-trip and similarity-fixtures parity")
    p.add_argument("--run")
    p.add_argument("--kernels", nargs="*")
    p.add_argument("--fixtures")
    p.add_argument("--ws3-identity", help="the primary identity string WS3 reports")
    p.set_defaults(func=cmd_check_kernels)

    p = sub.add_parser("score-pairs", help="Enumerate, prefilter and score pairs (chunked, resumable)")
    p.add_argument("--run", required=True)
    p.add_argument("--modes", nargs="+", default=["pos", "neg"])
    p.add_argument("--kernels", nargs="*")
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--seed", type=int, default=20261113)
    p.add_argument("--mces-k-low", type=int, default=500)
    p.add_argument("--mces-k-high", type=int, default=2000)
    p.add_argument("--mces-whole-from", type=int, default=3, help="Tanimoto bin index from which bins are taken whole (capped)")
    p.add_argument("--max-units", type=int)
    p.add_argument("--min-free-ram-gb", type=float, default=64.0)
    p.set_defaults(func=cmd_score_pairs)

    p = sub.add_parser("structure-truth", help="MCES on the stratified sample (resumable)")
    p.add_argument("--run", required=True)
    p.add_argument("--modes", nargs="+", default=["pos", "neg"])
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--limit", type=int, help="compute only the first N block pairs (throughput pilot)")
    p.set_defaults(func=cmd_structure_truth)

    p = sub.add_parser("calibrate", help="Cutoff table, curves, sanity numbers")
    p.add_argument("--run", required=True)
    p.add_argument("--modes", nargs="+", default=["pos", "neg"])
    p.add_argument("--bootstrap", type=int, default=1000)
    p.add_argument("--seed", type=int, default=20261113)
    p.add_argument("--only-kernels", nargs="*")
    p.add_argument("--curves", action="store_true")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("report", help="Write <run>/report.md from run.json and the cutoff table")
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("guard", help="VS21 leak scanner")
    p.add_argument("targets", nargs="+")
    p.add_argument("--allow-neighbour-identity", action="store_true")
    p.add_argument("--vs21-msp", action="append")
    p.add_argument("--vs21-db", action="append")
    p.add_argument("--no-names", action="store_true")
    p.add_argument("--overlay", action="append")
    p.add_argument("--extra-identities", action="append")
    p.set_defaults(func=cmd_guard)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "guard":
        C.below_normal_priority()          # C8: every heavy step runs at BelowNormal, workers included
    return int(args.func(args) or 0)
