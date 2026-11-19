"""Representative selection from a reference database built by the catalog's own MSP parser (Design §2).

The database is produced by ``msdial-spectrum-catalog ingest-reference-library <db> <msp> --library-name
... --no-consensus``; this module only reads it (``?mode=ro``).

Representative: one per (InChIKey first block, adduct, instrument class), the record with the most peaks
at or above 1% of its base peak, ties broken by the lowest record ordinal. Eligibility: an InChIKey first
block, a SMILES string, an adduct in ``ADDUCTS`` and at least three peaks after the primary kernel's
preprocessing (``eligibility_peaks``). Whether the SMILES parses is checked per block in structures.py,
and a block whose structure fails is excluded from every pair (recorded, never silently dropped).
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ADDUCTS = {
    "Positive": ("[M+H]+", "[M+Na]+", "[M+NH4]+"),
    "Negative": ("[M-H]-", "[M+FA-H]-", "[M+CH3COO]-"),
}
ALL_ADDUCTS = frozenset(a for values in ADDUCTS.values() for a in values)

# The primary identity's preprocessing (Design §3): rel 1%, peaks above precursor + 0.025 removed,
# precursor +/- 17 Da removed, no framing, no top-k. Used here only to decide eligibility (>= 3 peaks);
# it is not a kernel.
ELIGIBILITY_PREP = {"rel": 1.0, "above": 0.025, "precwin": 17.0}
MIN_PEAKS = 3


@dataclass
class SelectionReport:
    records: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    candidates: int = 0
    representatives: int = 0

    def exclude(self, reason: str) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + 1


def open_ro(path: str | Path) -> sqlite3.Connection:
    uri = "file:" + Path(path).resolve().as_posix() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def peaks_from_payload(blob: bytes) -> np.ndarray:
    payload = json.loads(zlib.decompress(blob).decode("utf-8"))
    peaks = np.asarray(payload.get("peaks", []), dtype=np.float64).reshape(-1, 2)
    if peaks.shape[0] > 1 and np.any(np.diff(peaks[:, 0]) < 0):
        peaks = peaks[np.argsort(peaks[:, 0], kind="stable")]
    return peaks


def relevant_peak_count(peaks: np.ndarray, rel: float = 1.0) -> int:
    if peaks.shape[0] == 0:
        return 0
    base = peaks[:, 1].max()
    return int(np.count_nonzero(peaks[:, 1] >= base * rel * 0.01))


def eligibility_peaks(peaks: np.ndarray, precursor_mz: float, prep: dict = ELIGIBILITY_PREP) -> np.ndarray:
    if peaks.shape[0] == 0:
        return peaks
    base = peaks[:, 1].max()
    keep = (peaks[:, 1] >= base * prep["rel"] * 0.01) & (peaks[:, 0] <= precursor_mz + prep["above"])
    keep &= np.abs(peaks[:, 0] - precursor_mz) > prep["precwin"]
    return peaks[keep]


def library_row(connection: sqlite3.Connection) -> dict:
    rows = connection.execute(
        "SELECT library_id, library_name, library_version, sha256, byte_size, record_count, license, "
        "source_uri FROM reference_library").fetchall()
    if len(rows) != 1:
        raise ValueError(f"expected exactly one reference_library row, found {len(rows)}")
    return dict(rows[0])


def select_representatives(db_path: str | Path, *, block_fraction: float = 1.0, seed: int = 0,
                           ion_mode: str | None = None) -> tuple[list[dict], SelectionReport, dict]:
    """Return representative rows (with peaks), the selection report and the library row.

    ``block_fraction`` < 1 keeps a seed-fixed subsample of InChIKey first blocks (the 1% rerun of
    acceptance criterion 4); the subsample is a property of the block, so every adduct and instrument
    class of a kept block stays together.
    """
    from .common import stable_hash01

    report = SelectionReport()
    connection = open_ro(db_path)
    try:
        library = library_row(connection)
        best: dict[tuple[str, str, str], tuple[int, int, str]] = {}
        query = ("SELECT r.reference_spectrum_id, r.library_record_index, r.inchikey_skeleton, r.smiles, "
                 "r.precursor_mz, r.precursor_type, r.ion_mode, r.instrument_class, b.payload "
                 "FROM reference_spectrum r JOIN spectrum_blob b ON b.payload_sha256 = r.payload_sha256")
        for row in connection.execute(query):
            report.records += 1
            if ion_mode is not None and row["ion_mode"] != ion_mode:
                report.exclude("other_ion_mode")
                continue
            block = row["inchikey_skeleton"]
            if not block:
                report.exclude("no_inchikey_block")
                continue
            if block_fraction < 1.0 and stable_hash01("block", block, seed=seed) >= block_fraction:
                report.exclude("not_in_block_subsample")
                continue
            if not row["smiles"]:
                report.exclude("no_smiles")
                continue
            adduct = row["precursor_type"]
            mode = row["ion_mode"]
            if mode not in ADDUCTS or adduct not in ADDUCTS[mode]:
                report.exclude("adduct_out_of_scope")
                continue
            precursor = row["precursor_mz"]
            peaks = peaks_from_payload(row["payload"])
            if eligibility_peaks(peaks, precursor).shape[0] < MIN_PEAKS:
                report.exclude("fewer_than_3_peaks_after_prep")
                continue
            report.candidates += 1
            key = (block, adduct, row["instrument_class"] or "UNKNOWN")
            rank = (-relevant_peak_count(peaks), int(row["library_record_index"]),
                    row["reference_spectrum_id"])
            if key not in best or rank < best[key]:
                best[key] = rank
        chosen = {value[2] for value in best.values()}
        reps = []
        detail = ("SELECT r.*, b.payload FROM reference_spectrum r JOIN spectrum_blob b "
                  "ON b.payload_sha256 = r.payload_sha256 WHERE r.reference_spectrum_id = ?")
        for spectrum_id in sorted(chosen):
            row = connection.execute(detail, (spectrum_id,)).fetchone()
            reps.append(_rep_from_row(row))
    finally:
        connection.close()
    reps.sort(key=lambda r: (r["ion_mode"], r["adduct"], r["precursor_mz"], r["record_index"]))
    report.representatives = len(reps)
    return reps, report, library


def _rep_from_row(row: sqlite3.Row) -> dict:
    from msdial_spectrum_catalog.reference_library import collision_energy_bin

    peaks = peaks_from_payload(row["payload"])
    return {
        "rep_id": row["reference_spectrum_id"],
        "record_index": int(row["library_record_index"]),
        "block": row["inchikey_skeleton"],
        "inchikey": row["inchikey"],
        "record_name": row["record_name"],
        "smiles": row["smiles"],
        "formula": row["formula"],
        "ontology": row["ontology"],
        "ion_mode": row["ion_mode"],
        "adduct": row["precursor_type"],
        "instrument_class": row["instrument_class"] or "UNKNOWN",
        "ce_bin": collision_energy_bin(row["collision_energy_value"]),
        "precursor_mz": float(row["precursor_mz"]),
        "mz": peaks[:, 0].tolist(),
        "intensity": peaks[:, 1].tolist(),
    }


def block_smiles(db_path: str | Path, blocks: set[str] | None = None) -> dict[str, list[str]]:
    """Every SMILES string recorded for each block, so the block structure is the modal SMILES."""
    connection = open_ro(db_path)
    try:
        out: dict[str, list[str]] = {}
        for row in connection.execute(
                "SELECT inchikey_skeleton AS block, smiles FROM reference_spectrum "
                "WHERE inchikey_skeleton IS NOT NULL AND smiles IS NOT NULL"):
            if blocks is None or row["block"] in blocks:
                out.setdefault(row["block"], []).append(row["smiles"])
        return out
    finally:
        connection.close()


def block_inchikeys(db_path: str | Path, blocks: set[str] | None = None) -> dict[str, list[str]]:
    connection = open_ro(db_path)
    try:
        out: dict[str, list[str]] = {}
        for row in connection.execute(
                "SELECT inchikey_skeleton AS block, inchikey, ontology FROM reference_spectrum "
                "WHERE inchikey_skeleton IS NOT NULL"):
            if blocks is None or row["block"] in blocks:
                out.setdefault(row["block"], []).append((row["inchikey"], row["ontology"]))
        return out
    finally:
        connection.close()


def reps_to_table(reps: list[dict]):
    import pyarrow as pa

    columns = {key: [r[key] for r in reps] for key in reps[0]} if reps else {}
    schema = pa.schema([
        ("rep_id", pa.string()), ("record_index", pa.int64()), ("block", pa.string()),
        ("inchikey", pa.string()), ("record_name", pa.string()), ("smiles", pa.string()),
        ("formula", pa.string()), ("ontology", pa.string()), ("ion_mode", pa.string()),
        ("adduct", pa.string()), ("instrument_class", pa.string()), ("ce_bin", pa.string()),
        ("precursor_mz", pa.float64()), ("mz", pa.list_(pa.float64())), ("intensity", pa.list_(pa.float64())),
    ])
    if not reps:
        return schema.empty_table()
    return pa.table(columns, schema=schema)


def selection_digest(reps: list[dict]) -> str:
    from .common import sha256_text

    return sha256_text("\n".join(f"{r['rep_id']}\t{r['block']}\t{r['adduct']}\t{r['instrument_class']}"
                                 for r in sorted(reps, key=lambda r: r["rep_id"])))
