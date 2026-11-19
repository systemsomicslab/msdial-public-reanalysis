"""Structural truth per InChIKey first block (Design §4).

- Structure: the block's modal SMILES, then RDKit largest fragment and ``rdMolStandardize.Uncharger``.
  Failures are recorded and their pairs excluded.
- Tanimoto: Morgan radius 2, 2048 bits; identity ``rdkit.morgan2_2048.tanimoto@<rdkit version>``.
- MCES: ``myopic_mces`` with threshold 10 (distances <= 10 exact, larger values are bounds); identity
  ``myopic_mces@<ver>;threshold=10;solver=<name>``.
- Class: ClassyFire direct parent from MS-FINDER's ``InchikeyClassyfireDB-VS5.icd`` (full key, then first
  block), falling back to the MSP ONTOLOGY; the source is recorded per block.
"""

from __future__ import annotations

import csv
import os
from collections import Counter
from pathlib import Path

import numpy as np

FP_BITS = 2048
FP_WORDS = FP_BITS // 64
MCES_THRESHOLD = 10
MCES_SOLVER = "COIN_CMD"
LIPID_MAP_FILE = Path(__file__).with_name("lipid_direct_parents_v1.tsv")


def tanimoto_identity() -> str:
    import rdkit

    return f"rdkit.morgan2_2048.tanimoto@{rdkit.__version__}"


def mces_identity() -> str:
    from importlib import metadata

    return f"myopic_mces@{metadata.version('myopic-mces')};threshold={MCES_THRESHOLD};solver={MCES_SOLVER}"


def modal(values: list[str]) -> str | None:
    """Most frequent value; ties go to the lexicographically smallest, so the choice is reproducible."""
    if not values:
        return None
    counts = Counter(values)
    top = max(counts.values())
    return min(v for v, c in counts.items() if c == top)


def standardize(smiles: str):
    """Return (standard SMILES, mol) or (None, reason)."""
    from rdkit import Chem, RDLogger
    from rdkit.Chem.MolStandardize import rdMolStandardize

    RDLogger.DisableLog("rdApp.*")
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None, "smiles_parse_failed"
    try:
        mol = rdMolStandardize.LargestFragmentChooser().choose(mol)
        mol = rdMolStandardize.Uncharger().uncharge(mol)
        Chem.SanitizeMol(mol)
    except Exception as error:  # RDKit raises several unrelated types
        return None, f"standardize_failed:{type(error).__name__}"
    return Chem.MolToSmiles(mol), mol


def morgan_words(mol) -> np.ndarray:
    """Morgan-2 2048-bit fingerprint packed into 32 uint64 words."""
    from rdkit.Chem import rdFingerprintGenerator

    generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=FP_BITS)
    bits = np.zeros(FP_BITS, dtype=np.uint8)
    on = list(generator.GetFingerprint(mol).GetOnBits())
    bits[on] = 1
    return np.packbits(bits, bitorder="little").view(np.uint64).copy()


def tanimoto_rows(fp_a: np.ndarray, fp_b: np.ndarray) -> np.ndarray:
    """Row-wise Tanimoto of two (n, 32) uint64 arrays; 0/0 is 0."""
    inter = np.bitwise_count(fp_a & fp_b).sum(axis=1, dtype=np.int64)
    union = np.bitwise_count(fp_a | fp_b).sum(axis=1, dtype=np.int64)
    out = np.zeros(inter.shape[0], dtype=np.float64)
    nz = union > 0
    out[nz] = inter[nz] / union[nz]
    return out


def tanimoto_one_to_many(fp: np.ndarray, fps: np.ndarray) -> np.ndarray:
    return tanimoto_rows(np.broadcast_to(fp, fps.shape), fps)


def load_icd(path: str | Path) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    """(full key -> (direct parent, CHEMONTID), first block -> modal entry of its full keys)."""
    full: dict[str, tuple[str, str]] = {}
    by_block: dict[str, list[tuple[str, str]]] = {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        next(reader, None)
        for row in reader:
            if len(row) < 3 or not row[0]:
                continue
            key = row[0].strip().upper()
            entry = (row[1].strip(), row[2].strip())
            full[key] = entry
            by_block.setdefault(key.split("-")[0], []).append(entry)
    block_entry = {}
    for block, entries in by_block.items():
        counts = Counter(entries)
        top = max(counts.values())
        block_entry[block] = min(e for e, c in counts.items() if c == top)
    return full, block_entry


def direct_parent(block: str, inchikeys: list[str], ontologies: list[str], icd) -> tuple[str | None, str]:
    """(direct parent name, source) with source in icd_full_key | icd_first_block | msp_ontology | none."""
    if icd is not None:
        full, by_block = icd
        hits = [full[k.upper()][0] for k in inchikeys if k and k.upper() in full]
        if hits:
            return modal(hits), "icd_full_key"
        if block in by_block:
            return by_block[block][0], "icd_first_block"
    names = [o for o in ontologies if o]
    if names:
        return modal(names), "msp_ontology"
    return None, "none"


def load_lipid_map(path: str | Path = LIPID_MAP_FILE) -> dict[str, int]:
    """Direct-parent name -> 1 (lipid) or 0 (not a lipid). Names absent from the map are not lipids."""
    out: dict[str, int] = {}
    if not Path(path).exists():
        return out
    with open(path, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader((line for line in handle if not line.startswith("#")), delimiter="\t"):
            out[row["direct_parent"].strip()] = int(row["is_lipid"])
    return out


def build_blocks(block_smiles: dict[str, list[str]], block_keys: dict[str, list[tuple[str, str]]],
                 icd_path: str | Path | None, lipid_map: dict[str, int] | None = None):
    """One row per block: standard SMILES, fingerprint words, direct parent with source, lipid flag."""
    icd = load_icd(icd_path) if icd_path and Path(icd_path).exists() else None
    lipid_map = load_lipid_map() if lipid_map is None else lipid_map
    rows, fps, failures = [], [], Counter()
    for block in sorted(block_smiles):
        smiles = modal(block_smiles[block])
        std, mol = standardize(smiles) if smiles else (None, "no_smiles")
        keys = block_keys.get(block, [])
        parent, source = direct_parent(block, [k for k, _ in keys], [o for _, o in keys], icd)
        if std is None:
            failures[mol] += 1
            fp = np.zeros(FP_WORDS, dtype=np.uint64)
        else:
            fp = morgan_words(mol)
        rows.append({"block": block, "smiles_modal": smiles, "smiles_std": std,
                     "structure_ok": std is not None, "direct_parent": parent, "parent_source": source,
                     "is_lipid": int(lipid_map.get(parent, 0)) if parent else 0})
        fps.append(fp)
    return rows, np.vstack(fps) if fps else np.zeros((0, FP_WORDS), np.uint64), dict(failures)


def mces_pair(smiles_a: str, smiles_b: str, threshold: int = MCES_THRESHOLD) -> tuple[float, int, float]:
    """(distance, compute_mode, seconds). Distances above the threshold are lower bounds (mode != 1 ILP)."""
    import myopic_mces

    _, distance, seconds, mode = myopic_mces.MCES(
        smiles_a, smiles_b, threshold=threshold, solver=MCES_SOLVER,
        solver_options={"msg": False, "threads": 1})
    return float(distance), int(mode), float(seconds)


def mces_worker_init(tmp_dir: str | None = None) -> None:
    """CBC writes its model files to TMP; keep them under the run directory, not C:."""
    from .common import below_normal_priority, limit_threads

    limit_threads()
    below_normal_priority()
    if tmp_dir:
        os.makedirs(tmp_dir, exist_ok=True)
        os.environ["TMP"] = os.environ["TEMP"] = os.environ["TMPDIR"] = tmp_dir
        import tempfile

        tempfile.tempdir = tmp_dir
