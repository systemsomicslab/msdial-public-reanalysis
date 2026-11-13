"""<run>/report.md: a factual summary of one calibration run, generated from run.json and cutoff_table.tsv.

The text holds identities, counts and thresholds only. For a private (VS21) run every count below 10 is
written as "<10" and no record-level value appears, so the file passes ``mncal guard`` in strict mode.
"""

from __future__ import annotations

import csv
from pathlib import Path

from . import common as C


def _n(value, private: bool) -> str:
    if isinstance(value, (int, float)) and private and 1 <= value < 10:
        return "<10"
    return f"{value:,}" if isinstance(value, int) else str(value)


def _fmt(x, digits: int = 3) -> str:
    if x in (None, ""):
        return "–"
    try:
        return f"{float(x):.{digits}f}"
    except ValueError:
        return str(x)


def write_run_report(run: Path) -> Path:
    record = C.read_json(run / "run.json")
    private = record.get("private", False)
    steps = record.get("steps", {})
    lines = [f"# MN calibration run {record['run_id']}", "",
             f"Benchmark id: `{record['benchmark_id']}`; cutoff table sha256: "
             f"`{steps.get('calibrate', {}).get('table_sha256', 'not computed')}`.", ""]
    code, pin, env = record.get("code", {}), record.get("catalog_pin", {}), record.get("environment", {})
    lines += ["## Provenance", "",
              f"- code commit `{code.get('commit')}`{' (dirty tree)' if code.get('dirty') else ''}",
              f"- catalog pin `{pin.get('name')}` at `{pin.get('commit')}`",
              f"- Python {env.get('python')}; " + ", ".join(f"{k} {v}" for k, v in env.get("packages", {}).items()),
              ""]
    br = steps.get("build-reps", {})
    if br:
        lines += ["## Libraries", ""]
        for lib in br.get("libraries", []):
            lines.append(f"- {lib.get('file_name', lib['library_name'])} ({lib['ion_mode']}): sha256 "
                         f"`{lib.get('file_sha256') or lib['sha256']}`"
                         + (f", md5 `{lib['md5']}` (Zenodo md5 match: {lib.get('md5_matches_zenodo')})"
                            if lib.get("md5") else "")
                         + (f", {lib.get('license')}" if lib.get("license") else ""))
        lines += ["", f"Block subsample fraction {br.get('block_fraction')} (seed {br.get('seed')}).", "",
                  "## Representatives", "", "| mode | records | eligible | representatives | blocks |",
                  "|---|---|---|---|---|"]
        for mode, s in br.get("selection", {}).items():
            lines.append(f"| {mode} | {_n(s['records'], private)} | {_n(s['eligible_records'], private)} | "
                         f"{_n(s['representatives'], private)} | {_n(s['blocks'], private)} |")
        lines.append("")
    sp = steps.get("score-pairs", {})
    if sp:
        lines += ["## Kernels", ""]
        for k in sp.get("kernels", []):
            parity = k.get("parity", {}).get("status", "n/a")
            lines.append(f"- `{k['identity']}` ({k['implementation']}, {k['score_convention']}; parity: {parity})")
        lines += ["", "## Pairs", "", "| mode | pairs in U | prefilter pass (scored) | kept shards | wall s |",
                  "|---|---|---|---|---|"]
        for mode, s in sp.get("summary", {}).items():
            lines.append(f"| {mode} | {_n(s['pairs'], private)} | {_n(s['scored'], private)} | "
                         f"{_n(s['kept'], private)} | {s['wall_seconds']} |")
        lines.append("")
    st = steps.get("structure-truth", {})
    if st:
        lines += ["## MCES", "", f"Identity `{st.get('mces_identity')}`.", ""]
        for mode, s in st.get("summary", {}).items():
            lines.append(f"- {mode}: sample {_n(s['sample_pairs'], private)} pairs, computed "
                         f"{_n(s['mces_computed'], private)}; {s.get('pairs_per_second')} block pairs/s "
                         f"with {st.get('workers')} workers; errors {_n(s['errors'], private)}")
        lines.append("")
    cal = steps.get("calibrate", {})
    table = run / "cutoff_table.tsv"
    if cal and table.exists():
        lines += ["## Headline cutoffs", "",
                  "All pairs, all adducts and instrument pairs, all Δprec bins, ≥ 2 matched peaks, target "
                  "precision 0.8.", "",
                  "| kernel | mode | truth | view | t* | precision [95% CI] | recall [95% CI] | n above |",
                  "|---|---|---|---|---|---|---|---|"]
        with open(table, encoding="utf-8") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                if (row["adduct"], row["dprec_bin"], row["instrument_pair"], row["min_matched"],
                        row["target_precision"]) != ("all", "all", "all", "2", "0.800000"):
                    continue
                kernel = row["kernel_identity"].split("?")[0]
                if row["estimable"] != "1":
                    lines.append(f"| {kernel} | {row['ion_mode']} | {row['truth_definition']} | "
                                 f"{row['bias_view']} | not estimable | – | – | – |")
                    continue
                lines.append(
                    f"| {kernel} | {row['ion_mode']} | {row['truth_definition']} | {row['bias_view']} | "
                    f"{_fmt(row['threshold'], 2)} | {_fmt(row['precision'])} [{_fmt(row['precision_ci_low'])}, "
                    f"{_fmt(row['precision_ci_high'])}] | {_fmt(row['recall'])} [{_fmt(row['recall_ci_low'])}, "
                    f"{_fmt(row['recall_ci_high'])}] | {row['n_pairs_above']} |")
        lines.append("")
        lines += ["## Sanity", ""]
        for mode, kernels in cal.get("sanity", {}).items():
            for short, s in kernels.items():
                if isinstance(s, dict):
                    lines.append(f"- {mode} {short}: share ≥ 0.9 among ≥ 2 matched (all U) "
                                 f"{_fmt(s.get('share_ge_0.9_among_ge2_matched_all'))}; median different-block "
                                 f"(all U, 0.01 bin) {_fmt(s.get('median_different_block_all_U_bin'), 2)}; "
                                 f"median same-block control {_fmt(s.get('median_same_block_control'))}")
                else:
                    lines.append(f"- {mode} {short}: {_fmt(s)}")
        lines.append("")
    path = run / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
