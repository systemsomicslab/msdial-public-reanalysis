"""Homologue-aware structural truth (tier `mces3_homologue`, homologue_rules_v1).

Two structures are homologues when they have the same scaffold up to repeat units. The author decided this
on 2026-11-15, after the VS20 run showed that MCES <= 3 rejects PFAS CF2 series and alkyl and lipid chain
series, which dominate the high-score pairs. The tier is

    y = (MCES <= 3) or homologue(a, b)

and `mces3` itself is unchanged.

Repeat-unit skeleton of one standardised structure (stereo removed):

- A repeat-unit atom is one of:
  - CH2: aliphatic, acyclic carbon, no charge, two hydrogens, two heavy neighbours, single bonds only;
  - CF2: aliphatic, acyclic carbon, no charge, no hydrogen, two terminal F and two other heavy neighbours,
    single bonds only (its F atoms belong to the unit);
  - oxa: acyclic ether oxygen, no charge, single bonds, both neighbours CH2 units (the O of C2H4O).
- Adjacent unit atoms form a run when both are CH2, both are CF2, or one is CH2 and the other oxa. CH2 and CF2
  never share a run, so a fluorotelomer keeps its CF2 run and its CH2 run apart.
- Each run is replaced by one dummy atom labelled by its kind: 1 = CH2 only, 2 = CF2 only, 3 = CH2 with oxa.
  The canonical SMILES of the result is the skeleton. The block also records its CH2, CF2 and oxa unit counts.

Two blocks are homologues when their skeletons are equal and the unit counts differ by exactly one unit kind:

- `series`: dCH2 != 0 with dCF2 = dO = 0; or dCF2 != 0 with dCH2 = dO = 0; or dO != 0 with dCH2 = 2 dO and
  dCF2 = 0 (C2H4O units).
- `redistribution`: every count equal. The same units are spread differently over the chains, for example
  PC 16:0/18:1 against PC 18:0/16:1, or a double bond moved along a chain. Different blocks with equal
  skeletons and equal counts can only differ in where their runs sit.

Every other combination is not a homologue (`none`), and so is every block without a skeleton.
Terminal CH3 and CF3 groups are not units, so methanol and ethanol are not homologues, while ethanol and
propanol are.
"""

from __future__ import annotations

RULES_VERSION = "homologue_rules_v1"
KIND_CH2, KIND_CF2, KIND_OXA_RUN = 1, 2, 3


def _unit_kinds(mol) -> dict[int, str]:
    from rdkit import Chem

    kinds: dict[int, str] = {}
    for atom in mol.GetAtoms():
        if atom.IsInRing() or atom.GetFormalCharge() != 0 or atom.GetIsAromatic():
            continue
        if any(b.GetBondType() != Chem.BondType.SINGLE for b in atom.GetBonds()):
            continue
        if atom.GetSymbol() != "C":
            continue
        heavy = [n for n in atom.GetNeighbors()]
        fluor = [n for n in heavy if n.GetSymbol() == "F" and n.GetDegree() == 1]
        other = [n for n in heavy if not (n.GetSymbol() == "F" and n.GetDegree() == 1)]
        if atom.GetTotalNumHs() == 2 and len(heavy) == 2 and not fluor:
            kinds[atom.GetIdx()] = "CH2"
        elif atom.GetTotalNumHs() == 0 and len(fluor) == 2 and len(other) == 2:
            kinds[atom.GetIdx()] = "CF2"
    for atom in mol.GetAtoms():
        if (atom.GetSymbol() == "O" and not atom.IsInRing() and atom.GetFormalCharge() == 0
                and atom.GetDegree() == 2 and all(b.GetBondType() == Chem.BondType.SINGLE for b in atom.GetBonds())
                and all(kinds.get(n.GetIdx()) == "CH2" for n in atom.GetNeighbors())):
            kinds[atom.GetIdx()] = "O"
    return kinds


def _joins(k1: str, k2: str) -> bool:
    return (k1 == k2 and k1 in ("CH2", "CF2")) or {k1, k2} == {"CH2", "O"}


def skeleton(smiles: str) -> tuple[str | None, int, int, int]:
    """(skeleton SMILES, n_CH2, n_CF2, n_oxa) of a standardised SMILES; (None, 0, 0, 0) if it does not parse."""
    from rdkit import Chem, RDLogger

    RDLogger.DisableLog("rdApp.*")
    mol = Chem.MolFromSmiles(smiles) if smiles else None
    if mol is None:
        return None, 0, 0, 0
    Chem.RemoveStereochemistry(mol)
    kinds = _unit_kinds(mol)
    counts = {"CH2": 0, "CF2": 0, "O": 0}
    for k in kinds.values():
        counts[k] += 1
    if not kinds:
        return Chem.MolToSmiles(mol), 0, 0, 0
    # runs: connected components of unit atoms under _joins
    run_of: dict[int, int] = {}
    runs: list[list[int]] = []
    for start in sorted(kinds):
        if start in run_of:
            continue
        stack, members = [start], []
        run_of[start] = len(runs)
        while stack:
            i = stack.pop()
            members.append(i)
            for n in mol.GetAtomWithIdx(i).GetNeighbors():
                j = n.GetIdx()
                if j in kinds and j not in run_of and _joins(kinds[i], kinds[j]):
                    run_of[j] = len(runs)
                    stack.append(j)
        runs.append(members)
    fluorines = {n.GetIdx() for i, k in kinds.items() if k == "CF2"
                 for n in mol.GetAtomWithIdx(i).GetNeighbors() if n.GetSymbol() == "F" and n.GetDegree() == 1}
    rw = Chem.RWMol()
    new_index: dict[int, int] = {}
    for atom in mol.GetAtoms():
        i = atom.GetIdx()
        if i in kinds or i in fluorines:
            continue
        a = Chem.Atom(atom.GetAtomicNum())
        a.SetFormalCharge(atom.GetFormalCharge())
        a.SetNumExplicitHs(atom.GetTotalNumHs())
        a.SetNoImplicit(True)
        a.SetIsAromatic(atom.GetIsAromatic())
        new_index[i] = rw.AddAtom(a)
    run_atom: list[int] = []
    for members in runs:
        member_kinds = {kinds[i] for i in members}
        label = KIND_CF2 if member_kinds == {"CF2"} else (KIND_OXA_RUN if "O" in member_kinds else KIND_CH2)
        d = Chem.Atom(0)
        d.SetIsotope(label)
        d.SetNoImplicit(True)
        run_atom.append(rw.AddAtom(d))

    def node(i: int) -> int | None:
        if i in fluorines:
            return None
        return run_atom[run_of[i]] if i in kinds else new_index[i]

    seen = set()
    for bond in mol.GetBonds():
        a, b = node(bond.GetBeginAtomIdx()), node(bond.GetEndAtomIdx())
        if a is None or b is None or a == b:
            continue
        key = (min(a, b), max(a, b))
        if key in seen:
            continue
        seen.add(key)
        rw.AddBond(a, b, bond.GetBondType())
        if bond.GetIsAromatic():
            rw.GetBondBetweenAtoms(a, b).SetIsAromatic(True)
    out = rw.GetMol()
    try:
        out.UpdatePropertyCache(strict=False)
        text = Chem.MolToSmiles(out)
    except Exception:  # RDKit raises several unrelated types
        return None, 0, 0, 0
    return text, counts["CH2"], counts["CF2"], counts["O"]


def relation(sk_a, n_a, sk_b, n_b) -> str:
    """'series', 'redistribution' or 'none' for two skeletons with their (CH2, CF2, oxa) counts."""
    if sk_a is None or sk_b is None or sk_a != sk_b:
        return "none"
    d_ch2, d_cf2, d_o = (n_a[0] - n_b[0]), (n_a[1] - n_b[1]), (n_a[2] - n_b[2])
    if d_ch2 == d_cf2 == d_o == 0:
        return "redistribution"
    if d_o == 0 and ((d_ch2 != 0 and d_cf2 == 0) or (d_cf2 != 0 and d_ch2 == 0)):
        return "series"
    if d_o != 0 and d_cf2 == 0 and d_ch2 == 2 * d_o:
        return "series"
    return "none"


def block_table(blocks):
    """Per block: skeleton, unit counts and an integer skeleton code (-1 without a structure)."""
    import pandas as pd

    rows = []
    for block, smiles, ok in zip(blocks["block"], blocks["smiles_std"], blocks["structure_ok"]):
        sk, ch2, cf2, oxa = skeleton(smiles) if ok else (None, 0, 0, 0)
        rows.append({"block": block, "skeleton": sk, "n_ch2": ch2, "n_cf2": cf2, "n_oxa": oxa})
    table = pd.DataFrame(rows)
    codes, _ = pd.factorize(table["skeleton"], use_na_sentinel=True)
    table["skeleton_code"] = codes.astype("int64")
    return table


def pair_relation(table, block_a, block_b):
    """Vectorised relation for pairs of blocks: an int8 array, 0 none, 1 series, 2 redistribution."""
    import numpy as np
    import pandas as pd

    idx = pd.Index(table["block"].astype(str))
    ia = idx.get_indexer(pd.Series(block_a).astype(str))
    ib = idx.get_indexer(pd.Series(block_b).astype(str))
    code = table["skeleton_code"].to_numpy()
    n = table[["n_ch2", "n_cf2", "n_oxa"]].to_numpy(np.int64)
    ok = (ia >= 0) & (ib >= 0)
    ia_, ib_ = np.where(ok, ia, 0), np.where(ok, ib, 0)
    same = ok & (code[ia_] == code[ib_]) & (code[ia_] >= 0)
    d = n[ia_] - n[ib_]
    d_ch2, d_cf2, d_o = d[:, 0], d[:, 1], d[:, 2]
    redistribution = same & (d_ch2 == 0) & (d_cf2 == 0) & (d_o == 0)
    series = same & ~redistribution & (
        ((d_o == 0) & (((d_ch2 != 0) & (d_cf2 == 0)) | ((d_cf2 != 0) & (d_ch2 == 0))))
        | ((d_o != 0) & (d_cf2 == 0) & (d_ch2 == 2 * d_o)))
    return np.where(series, 1, np.where(redistribution, 2, 0)).astype(np.int8)
