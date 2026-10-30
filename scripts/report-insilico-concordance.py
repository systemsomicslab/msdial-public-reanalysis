"""Count, per unit, how often an in-silico library's top candidate agrees with an acquired reference match.

The author's decision D-B of 2026-10-28: the in-silico tier's names are kept, as in-silico evidence and not
identifications, and the concordance between its top candidate and the acquired MSP's matches is collected on Stage 1
before Stage 2 is decided. This reads one finished run's alignment candidate table (AlignResult-*.mdcandidate.tsv,
written where the run's answers set export_annotation_candidates true) and its MSP annotator settings, and reports,
for each candidate table:

- features: the alignment's features the candidate table lists (those with at least one candidate);
- acquired_reference_matched / strict_reference_matched: features with an accepted match from any acquired MSP
  annotator, and from the strict one (the acquired annotator of the highest priority);
- of those, with_in_silico_candidate: features the in-silico annotator also listed a candidate for, and comparable:
  those where both sides carry an InChIKey of at least 14 characters;
- top_agrees: comparable features whose top in-silico candidate (lowest candidate_rank) shares the InChIKey's first
  block (14 characters) with an accepted acquired match, and any_agrees: those where any listed in-silico candidate
  does, with the rates over comparable features;
- in_silico_representatives: features whose representative is an in-silico candidate.

Where the in-silico row's suggestions are false (the campaign's four-tier profile), the candidate table lists only its
accepted matches, so the in-silico candidates compared are those that passed the tier's thresholds; a probe run with
suggestions true lists every candidate the Console scored.

Aggregates only: no compound name, InChIKey or library location is printed. Nothing is written. It reads what the run
wrote and judges nothing; the gate's LIB-3 holds how in-silico matches are reported.

Usage:
    python scripts/report-insilico-concordance.py <unit-workspace or output folder> [...] [--json]

Exit codes: 0 every argument was read (a unit without a candidate table or an in-silico tier is reported as not
evaluable, with why), 3 an argument is not a directory.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path, PureWindowsPath

# MS-DIAL's text exports carry each peak's MS/MS spectrum in one field, and a ZenoTOF DDA spectrum can be longer
# than Python's default csv field limit of 131,072 characters (MB-POST MPST000048, V5 of 2026-10-29: up to 270,713).
# Raised for the process, to the largest value a C long holds on Windows, as MS-DIAL Interactive 0.5.54 raises it.
csv.field_size_limit(2**31 - 1)

# The library kinds the Console reads as an in-silico (predicted) MSP, as the gate and the plan read them.
PREDICTED_LIBRARY_KINDS = frozenset({"predicted", "insilico", "computed", "generated", "predictedmsp"})
INCHIKEY_FIRST_BLOCK = 14


def _flag(value: object) -> bool:
    return str(value or "").strip().casefold() == "true"


def _number(value: object, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def _method_fields(method: Path) -> dict:
    """method.txt's keys, case-folded, as the Console reads them (key before the first ':' or '=')."""
    fields = {}
    for line in method.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if len(line) < 2 or line.lstrip().startswith("#"):
            continue
        separators = [index for index in (line.find(":"), line.find("=")) if index >= 0]
        if separators:
            at = min(separators)
            fields[line[:at].strip().casefold()] = line[at + 1:].strip().strip("\"'")
    return fields


def annotators(output: Path) -> "tuple[dict | None, str]":
    """The run's MSP annotators by kind: {"acquired": [ids, highest priority first], "predicted": [ids]}, or None and
    why. Read from the MSP annotator settings method.txt names, else msp_annotator_settings.tsv in the output."""
    table = output / "msp_annotator_settings.tsv"
    method = output / "method.txt"
    if method.is_file():
        named = _method_fields(method).get("msp annotator settings file path", "")
        if named:
            candidate = Path(named) if Path(named).is_absolute() else method.parent / named
            table = candidate if candidate.is_file() else table
    if not table.is_file():
        return None, "no MSP annotator settings were found"
    try:
        rows = _rows(table)
    except (OSError, ValueError, csv.Error) as exc:
        return None, f"the MSP annotator settings could not be read ({type(exc).__name__})"
    kinds: dict = {"acquired": [], "predicted": []}
    for row in sorted(rows, key=lambda row: -_number(row.get("priority"), 0.0)):
        annotator = str(row.get("annotator_id") or "").strip()
        if annotator:
            predicted = str(row.get("library_kind") or "").strip().casefold() in PREDICTED_LIBRARY_KINDS
            kinds["predicted" if predicted else "acquired"].append(annotator)
    return kinds, ""


def _first_block(inchikey: object) -> str:
    text = str(inchikey or "").strip()
    return text[:INCHIKEY_FIRST_BLOCK].upper() if len(text) >= INCHIKEY_FIRST_BLOCK else ""


def _rate(part: int, whole: int) -> "float | None":
    return round(part / whole, 4) if whole else None


def concordance(rows: list[dict], acquired: list[str], predicted: list[str]) -> dict:
    """The aggregates of one candidate table (see the module's docstring)."""
    strict = acquired[0] if acquired else ""
    features: dict = {}
    for index, row in enumerate(rows):
        features.setdefault(str(row.get("alignment_master_id") or "").strip(), []).append((index, row))
    counts = {"features": len(features), "acquired_reference_matched": 0, "in_silico_representatives": 0}
    against: dict = {}
    for scope, accepted_from in (("strict", {strict} - {""}), ("acquired", set(acquired))):
        against[scope] = {"reference_matched": 0, "with_in_silico_candidate": 0, "comparable": 0, "top_agrees": 0,
                          "any_agrees": 0}
    for listed in features.values():
        if any(row.get("annotator_id") in predicted and _flag(row.get("is_representative")) for _i, row in listed):
            counts["in_silico_representatives"] += 1
        in_silico = sorted(((_number(row.get("candidate_rank"), float("inf")), index, row) for index, row in listed
                            if row.get("annotator_id") in predicted), key=lambda item: (item[0], item[1]))
        for scope, accepted_from in (("strict", {strict} - {""}), ("acquired", set(acquired))):
            accepted = [row for _i, row in listed
                        if row.get("annotator_id") in accepted_from and _flag(row.get("is_reference_matched"))]
            if not accepted:
                continue
            tally = against[scope]
            tally["reference_matched"] += 1
            if not in_silico:
                continue
            tally["with_in_silico_candidate"] += 1
            keys = {_first_block(row.get("inchikey")) for row in accepted} - {""}
            top = _first_block(in_silico[0][2].get("inchikey"))
            if not keys or not top:
                continue
            tally["comparable"] += 1
            tally["top_agrees"] += top in keys
            tally["any_agrees"] += any(_first_block(item[2].get("inchikey")) in keys for item in in_silico)
    counts["acquired_reference_matched"] = against["acquired"]["reference_matched"]
    for tally in against.values():
        tally["top_agreement_rate"] = _rate(tally["top_agrees"], tally["comparable"])
        tally["any_agreement_rate"] = _rate(tally["any_agrees"], tally["comparable"])
    return {**counts, "strict_annotator": strict, "against_strict": against["strict"],
            "against_any_acquired": against["acquired"]}


def report_unit(argument: Path) -> dict:
    """One unit's report: each candidate table's aggregates, or why there are none."""
    output = argument / "output" if (argument / "output").is_dir() else argument
    unit = argument.resolve().parent.name if output == argument and argument.name.casefold() == "output" else argument.name
    result: dict = {"unit": unit, "status": "not_evaluable", "tables": []}
    kinds, why = annotators(output)
    if kinds is None:
        result["reason"] = why
        return result
    result["in_silico_annotators"] = kinds["predicted"]
    result["acquired_annotators"] = kinds["acquired"]
    if not kinds["predicted"]:
        result["reason"] = "the run searched no in-silico (predicted) MSP library"
        return result
    if not kinds["acquired"]:
        result["reason"] = "the run searched no acquired MSP library to compare with"
        return result
    tables = sorted(output.glob("AlignResult-*.mdcandidate.tsv"))
    if not tables:
        result["reason"] = ("the run exported no annotation candidates (AlignResult-*.mdcandidate.tsv): the answers must "
                            "set export_annotation_candidates true for the concordance to be collected")
        return result
    for table in tables:
        try:
            rows = _rows(table)
        except (OSError, ValueError, csv.Error) as exc:
            result["tables"].append({"table": table.name, "status": "not_evaluable",
                                     "reason": f"it could not be read ({type(exc).__name__})"})
            continue
        missing = {"alignment_master_id", "annotator_id", "inchikey", "is_reference_matched", "is_representative",
                   "candidate_rank"} - set(rows[0] if rows else {})
        if rows and missing:
            result["tables"].append({"table": table.name, "status": "not_evaluable",
                                     "reason": f"it has no {', '.join(sorted(missing))} column"})
            continue
        result["tables"].append({"table": table.name, "status": "read",
                                 **concordance(rows, kinds["acquired"], kinds["predicted"])})
    if any(item["status"] == "read" for item in result["tables"]):
        result["status"] = "read"
        result.pop("reason", None)
    else:
        result["reason"] = "no candidate table could be read"
    return result


def _words(unit: dict) -> list[str]:
    lines = [f"{unit['unit']}: {unit['status']}" + (f" ({unit['reason']})" if unit.get("reason") else "")]
    for table in unit["tables"]:
        if table["status"] != "read":
            lines.append(f"  {table['table']}: not evaluable ({table['reason']})")
            continue
        lines.append(f"  {table['table']}: {table['features']} features, {table['in_silico_representatives']} "
                     "represented by an in-silico candidate")
        for scope, words in (("against_strict", f"strict ({table['strict_annotator']})"),
                             ("against_any_acquired", "any acquired")):
            tally = table[scope]
            lines.append(f"    {words} reference matches {tally['reference_matched']}; with an in-silico candidate "
                         f"{tally['with_in_silico_candidate']}; comparable {tally['comparable']}; top candidate agrees "
                         f"{tally['top_agrees']} (rate {tally['top_agreement_rate']}); any listed candidate agrees "
                         f"{tally['any_agrees']} (rate {tally['any_agreement_rate']})")
    return lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("units", nargs="+", help="unit workspaces or their output folders")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)
    units = []
    for text in args.units:
        path = Path(text).expanduser()
        if not path.is_dir():
            print(f"not a directory: {PureWindowsPath(text).name}", file=sys.stderr)
            return 3
        units.append(report_unit(path))
    if args.json:
        print(json.dumps({"schema": "msdial-insilico-concordance.v1", "units": units}, ensure_ascii=True, indent=2))
    else:
        print("\n".join(line for unit in units for line in _words(unit)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
