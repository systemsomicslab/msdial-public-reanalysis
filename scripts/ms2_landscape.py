"""Read-only MS/MS landscape report for completed reanalysis campaign units.

Use a read-only SQLite URI for the ledger. Never read library spectra or run
analysis; all spectral input comes from the existing unit output MSP files.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

TIERS = ("LBM2", "VS21 strict", "VS21 relaxed", "GLACIER in-silico", "unassigned")
PEAK_ID = re.compile(rb"\|PEAKID=(\d+)")
ADDUCT_CHARGE = re.compile(r"\](\d*)([+-])$")
RAW_EXT = (".lcd", ".mzml", ".wiff", ".wiff2", ".raw", ".d", ".abf", ".qgd")


class SpectrumSample(NamedTuple):
    campaign: str
    study_type: str
    unit: str
    spot: int
    peaks: list[tuple[float, float]]
    tier: str
    class_name: str
    precursor_mz: float | None
    charge: int | None


def read_tsv(path: Path):
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        yield from csv.DictReader(handle, delimiter="\t")


def ledger_done(path: Path) -> set[str]:
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        rows = conn.execute("SELECT unit_key, to_state FROM transition ORDER BY seq").fetchall()
    except sqlite3.OperationalError as exc:
        # Immutable is safe only if no uncheckpointed WAL contains newer states.
        wal = Path(str(path) + "-wal")
        if wal.exists() and wal.stat().st_size:
            raise RuntimeError("Cannot read active ledger in read-only mode; "
                               "wait for checkpoint/stop rather than use stale states") from exc
        conn = sqlite3.connect(path.resolve().as_uri() + "?immutable=1", uri=True)
        rows = conn.execute("SELECT unit_key, to_state FROM transition ORDER BY seq").fetchall()
    finally:
        if "conn" in locals():
            conn.close()
    latest = dict(rows)
    return {key for key, state in latest.items() if state == "done"}


def completed_units(root: Path, campaigns: list[str]):
    """Yield unique completed units; never infer completion from files on disk."""
    seen = set()
    for campaign in campaigns:
        folder = root / "_campaigns" / campaign
        manifest = json.loads((folder / "campaign-manifest.json").read_text(encoding="utf-8"))
        done = ledger_done(folder / "ledger.sqlite")
        for unit in manifest["units"]:
            key = unit["unit_id"]
            if key not in done:
                continue
            if key in seen:
                raise ValueError(f"Completed unit {key} appears in more than one campaign")
            seen.add(key)
            study_type = "hydrophilic" if campaign == "stage1b-hilic-20261028" else "lipidomics"
            yield campaign, study_type, unit


def read_msp(path: Path):
    """Yield peak ID, peak count, bytes, peaks, annotation, precursor m/z and charge."""
    header = {}
    peaks = []
    size = 0
    left = 0

    def emit():
        match = PEAK_ID.search(header.get("comment", b""))
        if match:
            precursor = header.get("precursormz", b"")
            try:
                precursor_mz = float(precursor)
            except ValueError:
                precursor_mz = None
            adduct = header.get("precursortype", b"").decode("ascii", "ignore").strip()
            charge_match = ADDUCT_CHARGE.search(adduct)
            charge = int(charge_match.group(1) or 1) if charge_match else None
            return (int(match.group(1)), int(header.get("num peaks", b"0")), size,
                    peaks, header.get("ontology", b"").decode("utf-8", "replace").strip(),
                    header.get("name", b"").decode("utf-8", "replace").strip(),
                    precursor_mz if precursor_mz is not None and math.isfinite(precursor_mz) else None,
                    charge)
        return None

    with path.open("rb") as handle:
        for raw in handle:
            if raw.upper().startswith(b"NAME:"):
                if header:
                    record = emit()
                    if record:
                        yield record
                header = {"name": raw.partition(b":")[2].strip()}
                peaks = []
                size = len(raw)
                left = 0
                continue
            if not header:
                continue
            size += len(raw)
            if left:
                left -= 1
                values = raw.split()
                if len(values) >= 2:
                    try:
                        peaks.append((float(values[0]), float(values[1])))
                    except ValueError:
                        pass
                continue
            key, sep, value = raw.strip().partition(b":")
            if sep:
                name = key.decode("ascii", "ignore").lower()
                if name in ("comment", "ontology", "num peaks", "precursormz", "precursortype"):
                    header[name] = value.strip()
                if name == "num peaks":
                    left = int(value.strip())
        if header:
            record = emit()
            if record:
                yield record


def tier_map(output: Path) -> dict[str, str]:
    mapping = {}
    path = output / "msp_annotator_settings.tsv"
    if path.exists():
        for row in read_tsv(path):
            evidence = row.get("evidence_tier", "").lower()
            if row.get("library_kind", "").lower() == "predicted":
                tier = "GLACIER in-silico"
            elif "high" in evidence:
                tier = "VS21 strict"
            elif "low" in evidence:
                tier = "VS21 relaxed"
            else:
                continue
            mapping[row["annotator_id"]] = tier
    return mapping


def candidate_tier(row: dict, mapping: dict[str, str]) -> str:
    tag = row.get("annotation_tag_vs1", "")
    annotator = row.get("annotator_id", "").lower()
    evidence = row.get("evidence_source", "")
    if annotator.startswith("lbmdb:") and tag in {"410", "420", "430"} and evidence == "RuleBased":
        return "LBM2"
    tier = mapping.get(row.get("annotator_id", ""), "unassigned")
    if tier == "GLACIER in-silico" and tag == "450" and evidence == "InSilico":
        return tier
    if tier in {"VS21 strict", "VS21 relaxed"} and tag == "430" and evidence == "ReferenceSpectrum":
        return tier
    return "unassigned"


def compound_class(tier: str, ontology: str, name: str) -> str:
    klass = ontology.strip()
    if klass.lower() not in {"", "null", "none", "unknown", "n/a"}:
        return klass
    if tier == "LBM2":
        prefix = re.split(r"[ (]", name.strip())[0]
        if prefix and not prefix.lower().startswith("unknown"):
            return prefix
    return "unclassified"


def classification(output: Path, stem: str, warnings: list[str]):
    path = output / f"{stem}.mdcandidate.tsv"
    mapping = tier_map(output)
    if not path.exists():
        warnings.append(f"Missing {path}")
        return {}, {}
    tiers, names = {}, {}
    for row in read_tsv(path):
        if row.get("is_representative", "").lower() != "true":
            continue
        spot = int(row["alignment_master_id"])
        if spot in tiers:
            warnings.append(f"Duplicate representative candidate: {path} ID {spot}")
        tiers[spot] = candidate_tier(row, mapping)
        names[spot] = (row.get("ontology", ""), row.get("name", ""))
    return tiers, names


def raw_group(member: str) -> str | None:
    parts = member.replace("\\", "/").split("/")
    for idx, part in enumerate(parts):
        lower = part.lower()
        if lower.endswith(".wiff.scan"):
            return "/".join(parts[:idx] + [part[:-5]])
        if lower.endswith(RAW_EXT):
            return "/".join(parts[:idx + 1])
    return None


def raw_inventory(provenance: Path, warnings: list[str]) -> dict[str, int]:
    groups = defaultdict(int)
    paths = sorted(provenance.glob("archive-members-*.tsv"))
    if not paths:
        warnings.append(f"Missing archive-members TSV in {provenance}")
    for path in paths:
        for row in read_tsv(path):
            if row.get("type") != "file":
                continue
            group = raw_group(row.get("path", ""))
            if group:
                groups[group] += int(row.get("size") or 0)
    return dict(groups)


def analysed_sizes(output: Path, inventory: dict[str, int], warnings: list[str]):
    analysis = output / "analysis_files.csv"
    if not analysis.exists():
        warnings.append(f"Missing {analysis}")
        return 0, 0, set()
    selected = set()
    with analysis.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            raw_name = Path(row["file_path"]).name.lower()
            matches = [name for name in inventory if Path(name).name.lower() == raw_name]
            if len(matches) != 1:
                warnings.append(f"Raw size unresolved ({len(matches)} matches): {raw_name}")
            else:
                selected.add(matches[0])
    return len(selected), sum(inventory[name] for name in selected), selected


def choose_spectrum(heap, campaign, study_type, unit, record, tier, class_name,
                    cap, min_peaks):
    spot, n, _, peaks, _, _, precursor_mz, charge = record
    if n < min_peaks or not peaks:
        return
    rank = int.from_bytes(hashlib.sha256(f"{unit}:{spot}".encode()).digest()[:8], "big")
    sample = SpectrumSample(campaign, study_type, unit, spot, peaks, tier, class_name,
                            precursor_mz, charge)
    item = (-rank, len(heap), sample)
    if len(heap) < cap:
        heapq.heappush(heap, item)
    elif rank < -heap[0][0]:
        heapq.heapreplace(heap, item)


def unit_report(root: Path, meta: dict, campaign: str, study_type: str, cap: int,
                min_peaks: int, heaps: dict, eligible: Counter, classes: Counter,
                warnings: list[str]):
    accession, unit = meta["accession"], meta["unit_id"]
    output = root / "mb_post" / accession / unit / "output"
    provenance = output.parent / "provenance"
    matches = sorted(output.glob("AlignResult-*.mdpeakid.tsv"), key=lambda p: p.stat().st_mtime)
    if not matches:
        warnings.append(f"Missing alignment peak ID matrix: {output}")
        return None, {}
    stem = matches[-1].name.removesuffix(".mdpeakid.tsv")
    tiers, annotations = classification(output, stem, warnings)
    row = {"campaign": campaign, "study_type": study_type,
           "accession": accession, "unit": unit,
           "ion_mode": meta.get("ion_mode", ""), "acquisition": meta.get("acquisition_mode", "")}
    inventory = raw_inventory(provenance, warnings)
    row["raw_archive_file_count"] = len(inventory)
    row["raw_archive_bytes"] = sum(inventory.values())
    row["raw_analysed_file_count"], row["raw_analysed_bytes"], used = analysed_sizes(
        output, inventory, warnings)
    counts = Counter()
    class_for_spot = {}
    height = output / f"{stem}_Height.txt"
    if not height.exists():
        warnings.append(f"Missing {height}")
    else:
        with height.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
            for line in handle:
                if line.startswith("Alignment ID\t"):
                    reader = csv.DictReader(handle, fieldnames=line.rstrip("\r\n").split("\t"),
                                            delimiter="\t")
                    for feature in reader:
                        counts["features"] += 1
                        spot = int(feature["Alignment ID"])
                        tier = tiers.get(spot, "unassigned")
                        ontology, name = annotations.get(spot, ("", ""))
                        klass = compound_class(tier, ontology or feature.get("Ontology") or "",
                                               name or feature.get("Metabolite name", ""))
                        class_for_spot[spot] = klass
                        if tier != "unassigned":
                            classes[(tier, klass)] += 1
                    break
    rep = output / f"{stem}.mdmsp"
    msms_features = set()
    if not rep.exists():
        warnings.append(f"Missing {rep}")
    else:
        for record in read_msp(rep):
            spot, n, byte_count, _, _, _, _, _ = record
            if n <= 0:
                continue
            counts["representative_spectra"] += 1
            counts["representative_peaks"] += n
            counts["representative_bytes"] += byte_count
            msms_features.add(spot)
            if n >= min_peaks and record[3]:
                eligible[row["ion_mode"]] += 1
            choose_spectrum(heaps[row["ion_mode"]], campaign, study_type, unit, record,
                            tiers.get(spot, "unassigned"),
                            class_for_spot.get(spot, "unclassified"), cap, min_peaks)
    links = defaultdict(lambda: defaultdict(list))
    for link in read_tsv(matches[-1]):
        spot = int(link["alignment_master_id"])
        for sample, value in link.items():
            if sample == "alignment_master_id" or not value or value == "-1":
                continue
            links[sample][int(value)].append((spot, tiers.get(spot, "unassigned")))
    for sample, by_peak in links.items():
        path = output / f"{sample}.mdmsp"
        if not path.exists():
            warnings.append(f"Missing {path}")
            continue
        found = set()
        for peak_id, n, byte_count, _, _, _, _, _ in read_msp(path):
            if peak_id not in by_peak:
                continue
            found.add(peak_id)
            if n <= 0:
                continue
            for spot, tier in by_peak[peak_id]:
                msms_features.add(spot)
                counts["linked_spectra"] += 1
                counts["linked_peaks"] += n
                counts["linked_bytes"] += byte_count
                counts[f"tier_{tier}"] += 1
        missing_ids = set(by_peak) - found
        if missing_ids:
            warnings.append(f"Missing {len(missing_ids)} peak IDs in {path}")
    row["features_with_msms"] = len(msms_features)
    for field in ("features", "representative_spectra", "representative_peaks",
                  "representative_bytes", "linked_spectra", "linked_peaks", "linked_bytes"):
        row[field] = counts[field]
    for tier in TIERS:
        row[f"linked_{tier}"] = counts[f"tier_{tier}"]
    return row, inventory


def write_csv(path: Path, rows: list[dict]):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def spectrum_vector(peaks, precursor_mz, max_mz=2000):
    """Combine separately normalized nominal fragment and neutral-loss blocks."""
    fragments, losses = defaultdict(float), defaultdict(float)
    valid_precursor = precursor_mz is not None and math.isfinite(precursor_mz) and precursor_mz > 0
    for mz, intensity in peaks:
        if not math.isfinite(mz) or not math.isfinite(intensity) or intensity <= 0:
            continue
        fragment_bin = math.floor(mz + 0.5)
        if 1 <= fragment_bin <= max_mz:
            fragments[fragment_bin] += intensity
        if valid_precursor:
            loss_bin = math.floor(precursor_mz - mz + 0.5)
            if 1 <= loss_bin <= max_mz:
                losses[loss_bin] += intensity
    result = {}
    blocks = [(fragments, 0), (losses, max_mz)]
    nonempty = sum(bool(block) for block, _ in blocks)
    if not nonempty:
        return result
    for block, offset in blocks:
        norm = math.sqrt(sum(value for value in block.values()))
        if norm:
            weight = 1 / math.sqrt(nonempty)
            for mass, intensity in block.items():
                result[offset + mass - 1] = math.sqrt(intensity) / norm * weight
    return result


def make_plots(out: Path, rows: list[dict], classes: Counter, heaps: dict,
               eligible: Counter, max_plot: int, min_peaks: int, embedding_method: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from scipy.sparse import csr_matrix
    from scipy.sparse.linalg import svds

    data = rows[:-1]
    sums = {tier: sum(int(r[f"linked_{tier}"]) for r in data) for tier in TIERS}
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(list(sums), list(sums.values()), color=["#2a9d8f", "#457b9d", "#8ab17d", "#e9c46a", "#a8a8a8"])
    ax.set_ylabel("Linked experimental MS/MS spectra")
    ax.tick_params(axis="x", rotation=20)
    fig.tight_layout()
    fig.savefig(out / "tier_counts.png", dpi=180)
    fig.savefig(out / "tier_counts.svg")
    plt.close(fig)
    assigned_tiers = TIERS[:-1]
    top_names = set()
    for tier in assigned_tiers:
        per_tier = Counter({klass: n for (t, klass), n in classes.items()
                            if t == tier and klass != "unclassified"})
        top_names.update(klass for klass, _ in per_tier.most_common(3))
    top_names = sorted(top_names)
    palette = plt.colormaps["tab20"].colors
    fig, ax = plt.subplots(figsize=(12, 5))
    left = np.zeros(len(assigned_tiers))
    denominators = np.array([sum(n for (t, _), n in classes.items() if t == tier) or 1
                             for tier in assigned_tiers])
    for index, klass in enumerate(top_names + ["other"]):
        values = np.array([sum(n for (t, c), n in classes.items()
                               if t == tier and (c == klass if klass != "other" else c not in top_names))
                           for tier in assigned_tiers]) / denominators * 100
        ax.barh(assigned_tiers, values, left=left, label=klass,
                color="#aeb8bd" if klass == "other" else palette[index % len(palette)])
        left += values
    ax.set_xlim(0, 100)
    ax.set_xlabel("Share of assigned alignment features within each tier (%)")
    ax.legend(fontsize=7, ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.2))
    fig.tight_layout()
    fig.savefig(out / "classes_by_tier.png", dpi=180)
    fig.savefig(out / "classes_by_tier.svg")
    plt.close(fig)
    total = rows[-1]
    labels = ["Raw files analysed", "Alignment features", "Representative MS/MS", "Tier-assigned features"]
    values = [int(total["raw_analysed_file_count"]), int(total["features"]),
              int(total["representative_spectra"]), sum(n for (t, _), n in classes.items() if t != "unassigned")]
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.barh(labels[::-1], values[::-1], color="#277da1")
    ax.set_xscale("symlog")
    ax.set_xlabel("Count (different entities; not a conversion rate)")
    fig.tight_layout()
    fig.savefig(out / "raw_to_annotation_counts.png", dpi=180)
    fig.savefig(out / "raw_to_annotation_counts.svg")
    plt.close(fig)
    parameters = {"embedding_requested": embedding_method,
                  "metric": "cosine for UMAP", "nominal_bin_da": 1,
                  "fragment_mz_bins": "1..2000", "neutral_loss_bins": "1..2000",
                  "neutral_loss_da": "precursor m/z minus fragment m/z for singly charged precursors only; nonpositive losses excluded",
                  "dimension": 4000,
                  "intensity": "sum within nominal bins, square root, L2-normalize each nonempty block, equal block weights",
                  "autoencoder": "not used; deterministic 4000-dimensional baseline",
                  "seed": 17,
                  "max_spectra_per_polarity": max_plot, "polarity_separated": True,
                  "campaigns": list(dict.fromkeys(row["campaign"] for row in data)),
                  "input": f"representative experimental spectra with >={min_peaks} peaks",
                  "minimum_embedding_peaks": min_peaks,
                  "class_plot_max_distinct": 8,
                  "class_plot_min_count": "max(20, ceil(2% of plotted spectra))"}
    for polarity, heap in heaps.items():
        samples = [item[2] for item in heap]
        if len(samples) < 3:
            continue
        indices, indptr, values = [], [0], []
        retained = []
        for sample in samples:
            bins = spectrum_vector(sample.peaks,
                                   sample.precursor_mz if sample.charge == 1 else None)
            if not bins:
                continue
            retained.append(sample)
            for mass, intensity in sorted(bins.items()):
                indices.append(mass)
                values.append(intensity)
            indptr.append(len(indices))
        samples = retained
        if len(samples) < 3:
            continue
        vectors = csr_matrix((values, indices, indptr), shape=(len(samples), 4000))
        if embedding_method == "svd":
            u, s, _ = svds(vectors, k=2, random_state=17)
            coordinates = u * s
            method = "Truncated SVD"
        else:
            try:
                import umap
            except ImportError:
                if embedding_method == "umap":
                    raise RuntimeError("UMAP was requested but umap-learn is not installed") from None
                u, s, _ = svds(vectors, k=2, random_state=17)
                coordinates = u * s
                method = "Truncated SVD"
            else:
                coordinates = umap.UMAP(n_components=2, metric="cosine", random_state=17,
                                        n_jobs=1).fit_transform(vectors)
                method = "UMAP"
                parameters["umap_version"] = umap.__version__
        parameters[f"{polarity}_method"] = method
        parameters[f"{polarity}_eligible_spectra"] = eligible[polarity]
        parameters[f"{polarity}_spectra_plotted"] = len(samples)
        parameters[f"{polarity}_sampled"] = eligible[polarity] > len(heap)
        parameters[f"{polarity}_missing_precursor"] = sum(s.precursor_mz is None for s in samples)
        parameters[f"{polarity}_neutral_loss_excluded_non_single_charge"] = sum(
            s.charge != 1 for s in samples)
        write_csv(out / f"embedding_coordinates_{polarity}.csv", [
            {"campaign": sample.campaign, "study_type": sample.study_type,
             "unit": sample.unit, "alignment_spot_id": sample.spot,
             "precursor_mz": sample.precursor_mz, "precursor_charge": sample.charge,
             "tier": sample.tier, "class": sample.class_name,
             "coordinate_1": float(point[0]), "coordinate_2": float(point[1])}
            for sample, point in zip(samples, coordinates)])
        counts = Counter(s.class_name for s in samples)
        minimum = max(20, math.ceil(len(samples) * 0.02))
        prominent = {name for name, count in counts.most_common(8) if count >= minimum}
        classes_for_plot = [s.class_name if s.class_name in prominent else "other"
                            for s in samples]
        for coloring, labels in (("tier", [s.tier for s in samples]),
                                 ("class", classes_for_plot),
                                 ("study_type", [s.study_type for s in samples])):
            fig, ax = plt.subplots(figsize=(9, 7))
            frequency = Counter(labels)
            for label, count in frequency.most_common():
                mask = np.array([value == label for value in labels])
                ax.scatter(coordinates[mask, 0], coordinates[mask, 1], s=4,
                           alpha=0.55, label=f"{label} (n={count})", rasterized=True)
            ax.set_title(f"{polarity}: fragment + neutral-loss MS/MS ({method}, n={len(samples):,})")
            ax.set_xlabel(f"{method} 1")
            ax.set_ylabel(f"{method} 2")
            ax.legend(loc="best", fontsize=7, markerscale=2, ncol=2)
            fig.tight_layout()
            fig.savefig(out / f"spectrum_embedding_{polarity}_{coloring}.png", dpi=180)
            fig.savefig(out / f"spectrum_embedding_{polarity}_{coloring}.svg")
            plt.close(fig)
    (out / "plot_parameters.json").write_text(json.dumps(parameters, indent=2) + "\n", encoding="utf-8")


def write_meeting_summary(out: Path, rows: list[dict], classes: Counter):
    total = rows[-1]
    campaigns = ", ".join(dict.fromkeys(row["campaign"] for row in rows[:-1]))
    study_types = {row["study_type"] for row in rows[:-1]}
    plot = json.loads((out / "plot_parameters.json").read_text(encoding="utf-8"))
    gib = lambda value: int(value) / 1024 ** 3
    mib = lambda value: int(value) / 1024 ** 2
    lines = ["# MS/MS landscape: 会議用要約", "",
             f"対象: {campaigns}。完了した解析単位 {len(rows) - 1} 件。", "",
             "## 入力とスペクトル", "",
             f"- 元アーカイブ: {total['raw_archive_file_count']:,} ファイル、"
             f"{gib(total['raw_archive_bytes']):,.2f} GiB（group_idで重複排除）。",
             f"- 解析した生データ: {total['raw_analysed_file_count']:,} ファイル、"
             f"{gib(total['raw_analysed_bytes']):,.2f} GiB。",
             f"- 代表MS/MS: {total['representative_spectra']:,} 件、"
             f"{total['representative_peaks']:,} ピーク、"
             f"{mib(total['representative_bytes']):,.2f} MiB。",
             f"- サンプル別に接続されたMS/MS: {total['linked_spectra']:,} 件、"
             f"{total['linked_peaks']:,} ピーク、"
             f"{mib(total['linked_bytes']):,.2f} MiB。",
             f"- UMAP配置: Positive {plot.get('Positive_spectra_plotted', 0):,} 件、"
             f"Negative {plot.get('Negative_spectra_plotted', 0):,} 件。"
             f"最小ピーク数 {plot['minimum_embedding_peaks']}。", "",
             "## アノテーション階層（サンプル別接続スペクトル数）", ""]
    for tier in TIERS:
        lines.append(f"- {tier}: {total[f'linked_{tier}']:,}")
    lines += ["", "VS21 relaxedは候補レベルの一致であり、同定ではありません。"
              "GLACIERはin-silicoのエビデンスであり、同定ではありません。", "",
              "## 階層別の主なクラス", "",
              "以下はクラスが付いたalignment featureの件数であり、上記のスペクトル数とは異なります。", ""]
    for tier in TIERS[:-1]:
        top = Counter({klass: n for (t, klass), n in classes.items()
                       if t == tier and klass != "unclassified"})
        values = ", ".join(f"{klass} {n:,}" for klass, n in top.most_common(5))
        unclassified = classes[(tier, "unclassified")]
        lines.append(f"- {tier}: {values or '該当なし'}"
                     + (f"（クラス未分類 {unclassified:,} 件は順位から除外）"
                        if unclassified else ""))
    lines += ["", "## スライドに用いる図", "",
              "- `tier_counts.png`: アノテーション階層別の接続スペクトル数。",
              "- `classes_by_tier.png`: 各階層の主要クラス構成。",
              "- `spectrum_embedding_Positive_tier.png` / "
              "`spectrum_embedding_Negative_tier.png`: アノテーション階層別の配置。", "",
              "2次元マップは探索的な可視化であり、類似度スコアではありません。", ""]
    if len(study_types) > 1:
        lines.insert(-2, "- `spectrum_embedding_Positive_study_type.png` / "
                     "`spectrum_embedding_Negative_study_type.png`: 測定系別のMS/MS配置。")
    else:
        lines.insert(-2, "測定系の比較図は作成されていますが、今回の対象は1種類だけなので比較には使えません。")
    (out / "MEETING_SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--campaign", action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-plot-spectra", type=int, default=5000)
    parser.add_argument("--min-embedding-peaks", type=int, default=3)
    parser.add_argument("--embedding-method", choices=("auto", "umap", "svd"), default="auto")
    args = parser.parse_args()
    if args.max_plot_spectra < 3:
        parser.error("--max-plot-spectra must be >= 3")
    if args.min_embedding_peaks < 1:
        parser.error("--min-embedding-peaks must be >= 1")
    root, out = args.root.resolve(), args.out.resolve()
    if out == root or root in out.parents:
        parser.error("Report output must be outside the analysis root")
    out.mkdir(parents=True, exist_ok=True)
    warnings, rows = [], []
    classes, eligible, heaps = Counter(), Counter(), defaultdict(list)
    seen_groups, archive_totals = set(), {}
    for campaign, study_type, unit in completed_units(root, args.campaign):
        row, inventory = unit_report(root, unit, campaign, study_type,
                                     args.max_plot_spectra, args.min_embedding_peaks,
                                     heaps, eligible, classes, warnings)
        if row is None:
            continue
        group = unit.get("group_id") or unit["unit_id"]
        if group not in seen_groups:
            archive_totals[group] = inventory
            seen_groups.add(group)
        rows.append(row)
    if not rows:
        raise RuntimeError("No completed units with alignment output were found")
    fields = list(rows[0])
    total = {field: "" for field in fields}
    total.update({"accession": "TOTAL (archives deduplicated by group_id)", "unit": str(len(rows))})
    for field in fields:
        if field in {"raw_archive_file_count", "raw_archive_bytes"}:
            total[field] = (sum(len(v) for v in archive_totals.values()) if field.endswith("count")
                            else sum(sum(v.values()) for v in archive_totals.values()))
        elif field not in {"campaign", "study_type", "accession", "unit", "ion_mode", "acquisition"}:
            total[field] = sum(int(row[field]) for row in rows)
    rows.append(total)
    write_csv(out / "summary.csv", rows)
    write_csv(out / "classes_by_tier.csv",
              [{"tier": tier, "class": klass, "feature_count": n}
               for (tier, klass), n in sorted(classes.items())] or
              [{"tier": "unassigned", "class": "none", "feature_count": 0}])
    make_plots(out, rows, classes, heaps, eligible, args.max_plot_spectra,
               args.min_embedding_peaks,
               args.embedding_method)
    write_meeting_summary(out, rows, classes)
    write_csv(out / "missing_or_ambiguous_inputs.csv",
              [{"issue": issue} for issue in warnings] or [{"issue": "none"}])
    timestamp = datetime.now(timezone.utc).isoformat()
    (out / "README.md").write_text(
        f"# MS/MS landscape\n\nRead at {timestamp} UTC from `{root}`. "
        f"Included {len(rows)-1} ledger-`done` units in campaigns "
        f"{', '.join(f'`{name}`' for name in args.campaign)}.\n\n"
        "## Reproduce\n\n```powershell\n"
        'python scripts/ms2_landscape.py --root "<analysis-root>" '
        + " ".join(f"--campaign {name}" for name in args.campaign)
        + f' --out "<separate-report-directory>" --embedding-method {args.embedding_method} '
        f'--max-plot-spectra {args.max_plot_spectra} '
        f'--min-embedding-peaks {args.min_embedding_peaks}\n```\n\n'
        "Install `umap-learn`, `scipy`, and `matplotlib` in an isolated Python environment "
        "to reproduce the UMAP figures. Use `--embedding-method umap` to fail rather "
        "than silently substitute SVD when UMAP is missing.\n\n"
        "## Counting and caveats\n\n"
        "- Source ledgers are opened read-only; an uncheckpointed WAL is never ignored.\n"
        "- `study_type` is assigned from the campaign name: Stage 1b HILIC is "
        "hydrophilic; the other selected campaigns are lipidomics. It is not "
        "inferred from spectra or chromatography text.\n"
        "- Raw sizes come from archive-member inventories. The TOTAL archive count "
        "deduplicates shared `group_id` archives; per-unit archive bytes repeat for siblings.\n"
        "- Linked spectra are alignment-to-sample peak links, not library spectra. "
        "MSP bytes include headers and peak text; zero-peak records do not count as MS/MS.\n"
        "- An annotator ID alone is insufficient for a tier: precursor-only and weak "
        "evidence are unassigned. GLACIER is in-silico evidence, not identification.\n"
        f"- Experimental representative spectra with at least {args.min_embedding_peaks} "
        "peak(s) feed the "
        "embedding. If necessary, deterministic sampling limits each polarity to "
        f"{args.max_plot_spectra} spectra. Nominal fragment and neutral-loss bins are "
        "square-root transformed and block-normalized. Neutral-loss bins are used "
        "only for singly charged precursors with known m/z; other spectra use a "
        "fragment-only vector. UMAP uses cosine distance when available; the SVD "
        "fallback is explicitly labelled and is not UMAP. No autoencoder was trained. "
        "The 2-D view is exploratory, not an identification or similarity score.\n"
        "- One- or two-peak spectra have little shape information and can form "
        "low-information clusters when included.\n"
        "- No private library spectra or paths are read or included. See "
        "`plot_parameters.json` and `missing_or_ambiguous_inputs.csv`.\n",
        encoding="utf-8")
    print(f"{len(rows)-1} done units; {rows[-1]['representative_spectra']} representative and "
          f"{rows[-1]['linked_spectra']} linked MS/MS spectra; {len(warnings)} warnings")


if __name__ == "__main__":
    main()
