"""Mass-shift labelling of analog edges (WS7, Design §6).

The signed delta = prec_query - prec_neighbour is matched against every rule as +mass or -mass and
against every sum of two signed rules, within max(0.005 Da, 10 ppm of the larger precursor). Fits are
deduplicated by net elemental composition, so +C2H4, +2CH2 and +CH2+CH2 are one hypothesis. More than one
distinct composition is "ambiguous" (for example SO3 vs HPO3); none gives "isomer" in B0 and
"uninterpreted" otherwise.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from itertools import combinations_with_replacement
from pathlib import Path

RULES_FILE = Path(__file__).with_name("mass_shift_rules_v1.tsv")
ABS_TOL = 0.005
PPM_TOL = 10.0
MAX_CH2 = 10
MONO = {"C": 12.0, "H": 1.00782503207, "N": 14.0030740048, "O": 15.99491461956, "P": 30.97376163,
        "S": 31.97207100}


def parse_formula(formula: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for element, count in re.findall(r"([A-Z][a-z]?)(\d*)", formula):
        out[element] = out.get(element, 0) + (int(count) if count else 1)
    return out


def formula_mass(composition: dict[str, int]) -> float:
    return sum(MONO[e] * n for e, n in composition.items())


def format_composition(composition: dict[str, int]) -> str:
    """Hill-like order (C, H, then alphabetical), signed counts kept, zeros dropped."""
    keys = [k for k in ("C", "H") if composition.get(k)] + sorted(
        k for k in composition if k not in ("C", "H") and composition[k])
    return "".join(f"{k}{composition[k]}" for k in keys)


@dataclass(frozen=True)
class Rule:
    label: str
    composition: tuple[tuple[str, int], ...]
    mass: float
    category: str


def load_rules(path: str | Path = RULES_FILE) -> list[Rule]:
    rules = []
    with open(path, encoding="utf-8", newline="") as handle:
        lines = [line for line in handle if not line.startswith("#")]
    for row in csv.DictReader(lines, delimiter="\t"):
        comp = parse_formula(row["delta_formula"])
        rules.append(Rule(row["label"], tuple(sorted(comp.items())), float(row["mono_mass"]), row["category"]))
    ch2 = next(r for r in rules if r.label == "CH2")
    for n in range(2, MAX_CH2 + 1):
        comp = {e: c * n for e, c in ch2.composition}
        rules.append(Rule(f"{n}CH2", tuple(sorted(comp.items())), round(ch2.mass * n, 5), "homologation"))
    return rules


def rules_sha256(path: str | Path = RULES_FILE) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _signed(rules: list[Rule]):
    for rule in rules:
        for sign in (1, -1):
            yield (("+" if sign > 0 else "-") + rule.label, sign, rule)


def tolerance(prec_a: float, prec_b: float) -> float:
    return max(ABS_TOL, PPM_TOL * 1e-6 * max(prec_a, prec_b))


@dataclass(frozen=True)
class ShiftLabel:
    shift_label: str
    label_ambiguous: int
    fitting_labels: tuple[str, ...]

    @property
    def fitting_labels_json(self) -> str:
        return json.dumps(list(self.fitting_labels))


def label_shift(prec_query: float, prec_neighbour: float, rules: list[Rule] | None = None) -> ShiftLabel:
    rules = load_rules() if rules is None else rules
    delta = prec_query - prec_neighbour
    tol = tolerance(prec_query, prec_neighbour)
    if abs(delta) <= 0.01:
        return ShiftLabel("isomer", 0, ())
    signed = list(_signed(rules))
    tsv_labels = {r.label for r in rules if not r.label[0].isdigit()}
    fits: dict[str, list[tuple[int, int, str]]] = {}   # composition -> [(n_terms, generated, label)]

    def add(terms):
        comp: dict[str, int] = {}
        for _, sign, rule in terms:
            for e, c in rule.composition:
                comp[e] = comp.get(e, 0) + sign * c
        key = format_composition(comp)
        if not key:
            return
        label = "".join(t[0] for t in terms)
        generated = sum(t[2].label not in tsv_labels for t in terms)
        fits.setdefault(key, []).append((len(terms), generated, label))

    for term in signed:
        if abs(term[1] * term[2].mass - delta) <= tol:
            add([term])
    for t1, t2 in combinations_with_replacement(signed, 2):
        if abs(t1[1] * t1[2].mass + t2[1] * t2[2].mass - delta) <= tol:
            add(sorted([t1, t2], key=lambda t: t[0]))
    if not fits:
        return ShiftLabel("uninterpreted", 0, ())
    # One representative label per composition: fewest terms, then rules from the TSV over generated
    # n*CH2 rules, then lexicographic.
    best = sorted(min(v) for v in fits.values())
    labels = tuple(sorted(label for _, _, label in best))
    if len(best) == 1:
        return ShiftLabel(best[0][2], 0, labels)
    return ShiftLabel("ambiguous", 1, labels)
