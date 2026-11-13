"""Kernel registry: identity string -> scorer (Design §3; charter C3, C4).

Rules this module enforces:

- Every score carries its full identity string and its ``score_convention``; cosine-family scores are
  stored as cosines, never squared (C4).
- WS2 identities (``msdial.*``, ``gnps.modified_cosine?...``, ``entropy.li2021?...``) are scored only by
  WS2's ``msdial_spectrum_catalog.similarity_mn`` imported from the pinned catalog worktree, and only after
  ``similarity-fixtures/`` parity (|delta| <= 1e-9). No local re-implementation is registered under a WS2
  identity.
- ``matchms.modified_cosine?...`` is an external reference: matchms 0.34.0 ``ModifiedCosineGreedy`` on
  explicitly filtered spectra. Per C3 it is named only in run records and ``cutoff_table.tsv``, never in
  ``spectrum_similarity.method``.
- ``entropy.li2021?bin=0.05&prep=none&weighting=none`` is scored by a numpy port (``mncal-numpy-port``)
  of the pinned catalog's ``similarity.entropy_similarity``, which is the exact function WS2's
  ``entropy_li2021`` wraps; the pure-Python original costs about 1 ms per pair. The port is tested against
  that function on VS20 pairs (|delta| <= 1e-9), must pass ``similarity-fixtures/`` once they are in the pin
  (``fixture_parity`` checks it), and is offered to WS2. Until then its parity status is ``pending``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

PRIMARY = ("gnps.modified_cosine?assign=greedy&ipow=1&mzpow=0&prep=mn&prep.above=0.025&prep.abs=0"
           "&prep.frame=0&prep.maxmz=10000&prep.minmz=0&prep.pow=1&prep.precwin=17&prep.rel=1"
           "&prep.scale=100&prep.topk=0&prep.topkwin=50&tol=0.025")
_LEGACY_PREP = ("prep=mn&prep.above=0.05&prep.abs=0&prep.frame=1&prep.maxmz=10000&prep.minmz=0&prep.pow=0.5"
                "&prep.precwin=0&prep.rel=1&prep.scale=100&prep.topk=0&prep.topkwin=50&tol=0.025")
BONANZA = "msdial.bonanza.v1?" + _LEGACY_PREP
MODDOT_LEGACY = "msdial.moddot.legacy?" + _LEGACY_PREP
ENTROPY = "entropy.li2021?bin=0.05&prep=none&weighting=none"
# Sensitivity variants (never the recommended identity, never used by WS7).
SENS_POW05 = PRIMARY.replace("prep.pow=1&", "prep.pow=0.5&")
SENS_TOL002 = PRIMARY.replace("prep.above=0.025", "prep.above=0.02").replace("tol=0.025", "tol=0.02")
SENS_OPTIMAL = PRIMARY.replace("assign=greedy", "assign=optimal")


def matchms_identity() -> str:
    from importlib import metadata

    return ("matchms.modified_cosine?above=0.025&class=ModifiedCosineGreedy&ipow=1&mzpow=0&norm=max"
            f"&precwin=17&rel=1&tol=0.025&version={metadata.version('matchms')}")


@dataclass
class Kernel:
    identity: str
    implementation: str          # python-ws2 | matchms | catalog-pin-interim | csharp
    score_convention: str
    prepare: Callable[[float, np.ndarray], Any]
    score: Callable[[Any, Any], tuple[float, int]]
    reports_matches: bool
    parity: dict = field(default_factory=dict)
    short: str = ""


# ---------------------------------------------------------------- matchms reference
def _matchms_filter(precursor_mz: float, peaks: np.ndarray) -> np.ndarray:
    if peaks.shape[0] == 0:
        return peaks
    base = peaks[:, 1].max()
    keep = (peaks[:, 1] >= base * 0.01) & (peaks[:, 0] <= precursor_mz + 0.025)
    keep &= np.abs(peaks[:, 0] - precursor_mz) > 17.0
    kept = peaks[keep]
    if kept.shape[0]:
        kept = kept.copy()
        kept[:, 1] = kept[:, 1] / kept[:, 1].max()
    return kept


MATCHMS_TOL = 0.025


def matchms_pair_arrays(prec_a: float, a: np.ndarray, prec_b: float, b: np.ndarray,
                        tol: float = MATCHMS_TOL) -> tuple[float, int]:
    """matchms 0.34.0 ModifiedCosineGreedy.pair(reference=a, query=b) on (n, 2) peak arrays.

    Mirrors ModifiedCosineGreedy.pair / CosineGreedy.pair line by line with matchms's own numba cores
    (collect_peak_pairs, score_best_matches), skipping only the Spectrum/Fragments object overhead (about
    90% of the time per pair). A test checks it equals Spectrum-based pair() on VS20 pairs.
    """
    from matchms.similarity.spectrum_similarity_functions import collect_peak_pairs, score_best_matches

    if a.shape[0] == 0 or b.shape[0] == 0:
        return 0.0, 0
    shift = prec_a - prec_b
    if abs(shift) <= tol:
        pairs = collect_peak_pairs(a, b, tol, shift=0.0, mz_power=0.0, intensity_power=1.0)
        if pairs is None:
            return 0.0, 0
        pairs = pairs[np.argsort(pairs[:, 2], kind="mergesort")[::-1], :]
    else:
        zero = collect_peak_pairs(a, b, tol, shift=0.0, mz_power=0.0, intensity_power=1.0)
        nonzero = collect_peak_pairs(a, b, tol, shift=shift, mz_power=0.0, intensity_power=1.0)
        zero = np.zeros((0, 3)) if zero is None else zero
        nonzero = np.zeros((0, 3)) if nonzero is None else nonzero
        pairs = np.concatenate((zero, nonzero), axis=0)
        if pairs.shape[0] == 0:
            return 0.0, 0
        pairs = pairs[np.argsort(pairs[:, 2], kind="mergesort")[::-1], :]
    value, matches = score_best_matches(pairs, a, b, 0.0, 1.0)
    return float(value), int(matches)


def matchms_spectrum(precursor_mz: float, peaks: np.ndarray):
    """The filtered matchms Spectrum, for the parity test against matchms's own pair()."""
    from matchms import Spectrum

    kept = _matchms_filter(precursor_mz, peaks)
    return Spectrum(mz=np.ascontiguousarray(kept[:, 0], dtype=float),
                    intensities=np.ascontiguousarray(kept[:, 1], dtype=float),
                    metadata={"precursor_mz": float(precursor_mz)}, metadata_harmonization=False)


def matchms_kernel() -> Kernel:
    def prepare(precursor_mz: float, peaks: np.ndarray):
        kept = _matchms_filter(precursor_mz, peaks)
        return float(precursor_mz), np.ascontiguousarray(kept, dtype=np.float64)

    def score(a, b) -> tuple[float, int]:
        return matchms_pair_arrays(a[0], a[1], b[0], b[1])

    return Kernel(matchms_identity(), "matchms", "cosine", prepare, score, True, short="matchms")


# ---------------------------------------------------------------- interim entropy (catalog pin)
def interim_entropy_kernel() -> Kernel:
    from msdial_spectrum_catalog.similarity import entropy_similarity

    def prepare(precursor_mz: float, peaks: np.ndarray):
        return [(float(m), float(i)) for m, i in peaks[np.argsort(peaks[:, 0], kind="stable")]]

    def score(a, b) -> tuple[float, int]:
        value = entropy_similarity(a, b, 0.05)
        return (float("nan") if value is None else float(value)), 0

    return Kernel(ENTROPY, "catalog-pin-interim", "entropy_similarity", prepare, score, False,
                  parity={"status": "pending", "reason": "WS2 similarity-fixtures not in the catalog pin"},
                  short="entropy")


def _entropy_bits(intensities: np.ndarray) -> float:
    positive = intensities[intensities > 0]
    total = positive.sum()
    if total <= 0:
        return 0.0
    share = positive / total
    return float(-(share * np.log2(share)).sum())


def _frames(mz: np.ndarray, width: float) -> np.ndarray:
    # int(mz / width) in Python truncates toward zero; m/z is non-negative, so floor is the same.
    return np.floor(mz / width).astype(np.int64)


def entropy_port_prepare(precursor_mz: float, peaks: np.ndarray, width: float = 0.05):
    if peaks.shape[0] == 0 or peaks[:, 1].sum() <= 0:
        return None
    frames = _frames(peaks[:, 0], width)
    uniq, inv = np.unique(frames, return_inverse=True)
    self_entropy = _entropy_bits(np.bincount(inv, weights=peaks[:, 1], minlength=uniq.shape[0]))
    return frames, peaks[:, 1] / peaks[:, 1].sum(), self_entropy


def entropy_port_score(a, b) -> float:
    """numpy port of the catalog pin's similarity.entropy_similarity (bin 0.05), equal to <= 1e-12."""
    if a is None or b is None:
        return float("nan")
    frames = np.concatenate([a[0], b[0]])
    weights = np.concatenate([a[1], b[1]])
    uniq, inv = np.unique(frames, return_inverse=True)
    merged = _entropy_bits(np.bincount(inv, weights=weights, minlength=uniq.shape[0]) * 0.5)
    similarity = 1.0 - (2.0 * merged - (a[2] + b[2])) * 0.5
    return min(max(similarity, 0.0), 1.0)


def entropy_port_kernel() -> Kernel:
    return Kernel(ENTROPY, "mncal-numpy-port", "entropy_similarity", entropy_port_prepare,
                  lambda a, b: (entropy_port_score(a, b), 0), False,
                  parity={"status": "pending",
                          "reason": "WS2 similarity-fixtures not in the catalog pin; the port is tested "
                                    "against the pin's similarity.entropy_similarity (|delta| <= 1e-9)"},
                  short="entropy")


# ---------------------------------------------------------------- WS2 kernels (when the pin has them)
def ws2_available() -> bool:
    try:
        import msdial_spectrum_catalog.similarity_mn  # noqa: F401
    except ImportError:
        return False
    return True


def ws2_kernel(identity: str, short: str) -> Kernel:
    from msdial_spectrum_catalog import similarity_mn as mn

    canonical = mn.canonical_kernel_id(identity)
    if canonical != identity:
        raise ValueError(f"kernel identity does not round-trip through canonical_kernel_id: {identity!r} -> "
                         f"{canonical!r}")
    base, params = mn.parse_kernel_id(identity)
    prep = None
    if params["prep"] == "mn":
        prep = mn.MnPrep(**{k[5:]: v for k, v in params.items() if k.startswith("prep.")})
    none_identity = mn.format_kernel_id(base, {k: v for k, v in params.items() if not k.startswith("prep")}
                                        | {"prep": "none"})

    def prepare(precursor_mz: float, peaks: np.ndarray):
        spectrum = mn.Spectrum(float(precursor_mz), tuple((float(m), float(i)) for m, i in peaks))
        return mn.preprocess(spectrum, prep) if prep is not None else spectrum

    def score(a, b) -> tuple[float, int]:
        result = mn.score(none_identity, a, b)
        value = float("nan") if result.score is None else float(result.score)
        return value, int(result.matched_peak_count)

    convention = mn.score(identity, mn.Spectrum(100.0, ((50.0, 1.0),)),
                          mn.Spectrum(100.0, ((50.0, 1.0),))).score_convention
    return Kernel(identity, "python-ws2", convention, prepare, score, not base.startswith("entropy"),
                  short=short)


# ---------------------------------------------------------------- registry
def default_kernels(names: list[str] | None = None) -> list[Kernel]:
    """Week-1 set without WS2: matchms reference + interim entropy. With WS2 in the pin: its kernels."""
    names = names or (["primary", "bonanza", "moddot", "entropy", "matchms"] if ws2_available()
                      else ["matchms", "entropy"])
    table = {
        "primary": lambda: ws2_kernel(PRIMARY, "primary"),
        "bonanza": lambda: ws2_kernel(BONANZA, "bonanza"),
        "moddot": lambda: ws2_kernel(MODDOT_LEGACY, "moddot"),
        "pow05": lambda: ws2_kernel(SENS_POW05, "pow05"),
        "tol002": lambda: ws2_kernel(SENS_TOL002, "tol002"),
        "optimal": lambda: ws2_kernel(SENS_OPTIMAL, "optimal"),
        "entropy": entropy_port_kernel,
        "entropy_reference": interim_entropy_kernel,
        "matchms": matchms_kernel,
    }
    unknown = [n for n in names if n not in table]
    if unknown:
        raise ValueError(f"unknown kernel names {unknown}; known: {sorted(table)}")
    return [table[n]() for n in names]


def fixture_parity(kernels: list[Kernel], fixtures_dir: str | Path | None) -> dict[str, dict]:
    """Check every WS2 kernel against similarity-fixtures/kernels*.json to |delta| <= abs_tol (1e-9).

    Fixture inputs are raw; the expected kernel_id names the prep. A kernel is checked against every
    expectation with the same base and assignment, whatever its prep, because the scorer applies the
    expectation's own prep. Returns {identity: {status, checked, failures}}.
    """
    out: dict[str, dict] = {}
    checked_impl = ("python-ws2", "mncal-numpy-port")
    if fixtures_dir is None or not Path(fixtures_dir).is_dir() or not ws2_available():
        for k in kernels:
            if k.implementation in checked_impl:
                out[k.identity + " [" + k.implementation + "]"] = {
                    "status": "blocked", "reason": "similarity-fixtures or WS2 module not in the catalog pin"}
        return out
    from msdial_spectrum_catalog import similarity_mn as mn

    cases = []
    for name in ("kernels.json", "kernels-vs20.json"):
        path = Path(fixtures_dir) / name
        if path.exists():
            cases.extend(json.loads(path.read_text(encoding="utf-8"))["cases"])
    for kernel in kernels:
        if kernel.implementation not in checked_impl:
            continue
        base, params = mn.parse_kernel_id(kernel.identity)
        checked, failures = 0, []
        for case in cases:
            a = mn.Spectrum(case["a"]["precursor_mz"], tuple(map(tuple, case["a"]["peaks"])))
            b = mn.Spectrum(case["b"]["precursor_mz"], tuple(map(tuple, case["b"]["peaks"])))
            for expected in case["expected"]:
                if "python" not in expected.get("implementations", ["python"]):
                    continue
                e_base, e_params = mn.parse_kernel_id(expected["kernel_id"])
                if e_base != base or e_params.get("assign") != params.get("assign"):
                    continue
                if kernel.implementation == "mncal-numpy-port":
                    if expected["kernel_id"] != kernel.identity:
                        continue
                    pa = np.asarray(case["a"]["peaks"], float).reshape(-1, 2)
                    pb = np.asarray(case["b"]["peaks"], float).reshape(-1, 2)
                    value = kernel.score(kernel.prepare(case["a"]["precursor_mz"], pa),
                                         kernel.prepare(case["b"]["precursor_mz"], pb))[0]
                    got_score = None if math.isnan(value) else value
                else:
                    got_score = mn.score(expected["kernel_id"], a, b).score
                want = expected["score"]
                checked += 1
                if want is None:
                    ok = got_score is None
                elif want == "NaN":
                    ok = got_score is not None and math.isnan(got_score)
                else:
                    ok = got_score is not None and abs(got_score - want) <= expected.get("abs_tol", 1e-9)
                if not ok:
                    failures.append({"case": case["id"], "kernel_id": expected["kernel_id"],
                                     "expected": want, "got": got_score})
        out[kernel.identity + " [" + kernel.implementation + "]"] = {
            "status": "pass" if checked and not failures else "fail", "checked": checked,
            "failures": failures[:20]}
    return out
