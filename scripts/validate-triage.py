"""Check one accession's msdial-unit-triage.v1 files before anything is planned or downloaded.

A triage file (schemas/msdial-unit-triage.v1.json, written by the msdial-unit-triage skill) records which
Catalog rows and archive members one analysis unit keeps. Every choice in it is a model inference. This
script is the deterministic stage that stands between those inferences and a plan: it reads the triage
files of one accession as a set and returns pass, hold or error.

    python scripts/validate-triage.py <triage dir>
        [--catalog <handoffs dir | catalog.json>]   Catalog rows (and handoff sha256) to check coverage against
        [--archive-listing <tsv>]...                 zip member listing(s): path, size, crc32 [, archive]
        [--ground-truth <ground_truth.json>]         score the assignment against a known answer (eval)
        [--json]

The triage directory holds one <unit_id>.json per unit. --catalog takes either the campaign's handoffs
directory (<unit_id>/handoff.json and samples.json, as msdial_catalog_reanalysis_handoff writes them) or a
catalog.json with {"units": [{"unit_id", "declared", "rows": [{"sample_id", "raw_file"}]}]}, the shape the
eval fixtures use. An archive listing is the archive-members TSV the download store keeps, or any TSV with
path, size and crc32 columns; it may be read remotely from the zip central directory before download.

Levels:
- error: the triage file breaks the contract (schema, coverage, a file in two units, byte-identical content
  settled by name, polarity or chromatography that disagrees with its unit, an unflagged MS1-only or mixed
  acquisition, ...). The triage must be redone; nothing may be planned from it.
- hold: the file is valid but something waits for a person: a unit that needs a person, abstained or is out
  of scope, flagged shared content, a member nobody accounted for.
- pass: every unit is ready and nothing waits.

Exit status: 0 pass, 1 hold, 2 error. The script reads only; it writes nothing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA_ID = "msdial-unit-triage.v1"
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "msdial-unit-triage.v1.json"

RAW_EXTENSIONS = (".raw", ".mzml", ".mzxml", ".mzdata", ".wiff", ".wiff2", ".d", ".lcd", ".cdf", ".abf", ".ibf", ".baf", ".tdf")
VENDOR_FOLDER = re.compile(r"(?i)^(.*?\.(?:d|raw))/.+")  # Agilent/Bruker .d and Waters .raw directories
DECLARED_SOURCES = {"catalog_row", "catalog_unit", "mwtab", "repository_page"}
WEAK_FIELDS = {"size", "size_bytes", "modified", "mtime", "timestamp", "crc32"}
MIN_DUPLICATE_BYTES = 1024  # AppleDouble stubs and empty files share CRCs without meaning anything

# Flags that keep a unit from being ready until a person has answered them.
BLOCKING_FLAGS = {
    "shared_content",
    "folder_header_disagreement",
    "polarity_switching",
    "ms1_only",
    "mixed_acquisition",
    "targeted_dda",
    "declared_technical_disputed",
    "name_token_disputed",
}


# --------------------------------------------------------------------------------------------- findings


@dataclass
class Finding:
    level: str  # "error" or "hold"
    check: str
    unit: str | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "check": self.check, "unit": self.unit, "message": self.message}


@dataclass
class Report:
    accession: str | None = None
    units: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    unit_status: dict[str, str] = field(default_factory=dict)
    score: dict[str, dict[str, int]] | None = None

    def add(self, level: str, check: str, unit: str | None, message: str) -> None:
        self.findings.append(Finding(level, check, unit, message))

    def unit_verdict(self, unit: str) -> str:
        levels = {f.level for f in self.findings if f.unit == unit}
        return "error" if "error" in levels else "hold" if "hold" in levels else "pass"

    @property
    def verdict(self) -> str:
        levels = {f.level for f in self.findings}
        return "error" if "error" in levels else "hold" if "hold" in levels else "pass"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "accession": self.accession,
            "verdict": self.verdict,
            "units": {u: {"status": self.unit_status.get(u), "verdict": self.unit_verdict(u)} for u in self.units},
            "findings": [f.as_dict() for f in self.findings],
        }
        if self.score is not None:
            out["score"] = self.score
        return out


# ------------------------------------------------------------------------------- minimal schema checker
# Only the keywords schemas/msdial-unit-triage.v1.json uses, so the result does not depend on which
# jsonschema package (if any) is installed. tests/test_validate_triage.py checks the schema itself against
# Draft 2020-12 when jsonschema is available.

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _type_ok(value: Any, name: str) -> bool:
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[name])


def schema_errors(value: Any, schema: dict[str, Any], root: dict[str, Any], where: str = "$") -> list[str]:
    if schema is True:
        return []
    errors: list[str] = []
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise ValueError(f"unsupported $ref {ref}")
        target: Any = root
        for part in ref[2:].split("/"):
            target = target[part]
        errors += schema_errors(value, target, root, where)
    for sub in schema.get("allOf", []):
        errors += schema_errors(value, sub, root, where)
    if "anyOf" in schema and not any(not schema_errors(value, sub, root, where) for sub in schema["anyOf"]):
        errors.append(f"{where}: matches none of the allowed forms")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{where}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} is not one of {schema['enum']}")
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(value, n) for n in names):
            errors.append(f"{where}: expected {'/'.join(names)}, got {type(value).__name__}")
            return errors
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{where}: must not be empty")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{where}: {value!r} does not match {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: must be >= {schema['minimum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{where}: needs at least {schema['minItems']} item(s)")
        if "items" in schema:
            for i, item in enumerate(value):
                errors += schema_errors(item, schema["items"], root, f"{where}[{i}]")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{where}: missing {key}")
        props = schema.get("properties", {})
        for key, item in value.items():
            if key in props:
                errors += schema_errors(item, props[key], root, f"{where}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{where}: unexpected field {key}")
    return errors


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------- inputs


def _norm_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def member_key(path: str) -> str:
    """Case-insensitive key; members inside a vendor folder collapse to the folder."""
    p = _norm_path(path)
    m = VENDOR_FOLDER.match(p)
    if m:
        p = m.group(1)
    return p.casefold()


def basename(path: str) -> str:
    return _norm_path(path).rsplit("/", 1)[-1]


def stem(name: str) -> str:
    return re.sub(r"(?i)\.(raw|mzml|mzxml|mzdata|wiff2?|d|lcd|cdf)$", "", basename(name))


def _name_key(raw_file: str) -> str:
    name = basename(raw_file)
    return ("stem:" + name.casefold()) if stem(name) == name else ("name:" + name.casefold())


def is_raw_like(path: str) -> bool:
    p = _norm_path(path)
    b = basename(p)
    if p.startswith("__MACOSX/") or b.startswith("._"):
        return False
    return b.casefold().endswith(RAW_EXTENSIONS) or VENDOR_FOLDER.match(p) is not None


@dataclass
class ListedMember:
    archive: str | None
    path: str
    size: int
    crc32: str


def read_listing(path: Path) -> list[ListedMember]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        fields = {name.lower(): name for name in (reader.fieldnames or [])}
        crc_field = fields.get("crc32") or fields.get("crc")
        if "path" not in fields or "size" not in fields or crc_field is None:
            raise ValueError(f"{path}: an archive listing needs path, size and crc32 columns")
        out = []
        for row in reader:
            if "type" in fields and row[fields["type"]] not in ("file", ""):
                continue
            out.append(
                ListedMember(
                    archive=row.get(fields["archive"]) if "archive" in fields else None,
                    path=_norm_path(row[fields["path"]]),
                    size=int(row[fields["size"]] or 0),
                    crc32=(row[crc_field] or "").upper(),
                )
            )
        return out


@dataclass
class CatalogUnit:
    unit_id: str
    rows: list[dict[str, Any]]
    declared: dict[str, Any]
    handoff_sha256: str | None


def read_catalog(path: Path) -> dict[str, CatalogUnit]:
    units: dict[str, CatalogUnit] = {}
    if path.is_dir():
        for handoff in sorted(path.glob("*/handoff.json")):
            unit_dir = handoff.parent
            raw = handoff.read_bytes()
            data = json.loads(raw.decode("utf-8"))
            samples_path = unit_dir / "samples.json"
            rows = json.loads(samples_path.read_text(encoding="utf-8")) if samples_path.exists() else data.get("sample_metadata") or []
            ts = data.get("technical_settings") or {}
            units[unit_dir.name] = CatalogUnit(
                unit_id=unit_dir.name,
                rows=rows,
                declared={k: ts.get(k) for k in ("chromatography", "ion_mode", "acquisition_mode")},
                handoff_sha256=hashlib.sha256(raw).hexdigest(),
            )
        return units
    data = json.loads(path.read_text(encoding="utf-8"))
    for unit in data["units"]:
        units[unit["unit_id"]] = CatalogUnit(
            unit_id=unit["unit_id"],
            rows=unit["rows"],
            declared=unit.get("declared") or {},
            handoff_sha256=unit.get("handoff_sha256"),
        )
    return units


# ------------------------------------------------------------------------------------ normalisation

_POLARITY_TOKEN = re.compile(r"(?i)(positive|negative|pos|neg)(?![a-z])")
_HILIC_TOKEN = re.compile(r"(?i)hilic")
_RP_TOKEN = re.compile(r"(?i)((?<![a-z])c(?:18|30|8)(?![0-9])|rplc|(?<![a-z])rp(?![a-z])|rp(?=pos|neg))")


def normalise_ion(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v.startswith("pos"):
        return "positive"
    if v.startswith("neg"):
        return "negative"
    if v in ("both", "positive and negative", "mixed"):
        return "both"
    if v in ("switching", "polarityswitching", "polarity switching", "polarity_switching"):
        return "switching"
    return "unknown"


def normalise_chromatography(value: Any) -> str:
    v = str(value or "").strip().lower()
    if "hilic" in v:
        return "HILIC"
    if v in ("rp", "rplc") or "reverse" in v or "c18" in v or "c8" in v or "c30" in v:
        return "RP"
    if "normal" in v:
        return "normal_phase"
    return "unknown" if not v or v == "unknown" else "other"


def token_polarity(path: str) -> str | None:
    found = {("positive" if m.group(1).lower().startswith("pos") else "negative") for m in _POLARITY_TOKEN.finditer(_norm_path(path))}
    return found.pop() if len(found) == 1 else None


def token_chromatography(path: str) -> str | None:
    p = _norm_path(path)
    found = set()
    if _HILIC_TOKEN.search(p):
        found.add("HILIC")
    if _RP_TOKEN.search(p):
        found.add("RP")
    return found.pop() if len(found) == 1 else None


# --------------------------------------------------------------------------------------- the checks


@dataclass
class Kept:
    """One file a unit keeps: a Catalog row or an added member."""

    unit: str
    key: str
    path: str  # member path, or the row's raw_file when the member is unknown
    item: dict[str, Any]
    is_row: bool


def _kept(unit: str, triage: dict[str, Any]) -> list[Kept]:
    out = []
    for row in triage.get("rows", []):
        path = row.get("member_path") or row.get("raw_file") or ""
        out.append(Kept(unit, member_key(path) if row.get("member_path") else "raw:" + stem(path).casefold(), path, row, True))
    for member in triage.get("added_members", []):
        out.append(Kept(unit, member_key(member["member_path"]), member["member_path"], member, False))
    return out


def _cleared(triage: dict[str, Any], code: str) -> bool:
    return any(a.get("code") == code for a in triage.get("person_answers", []))


def _flag_codes(triage: dict[str, Any]) -> set[str]:
    return {f["code"] for f in triage.get("flags", [])}


def validate(
    triage_dir: Path,
    catalog: dict[str, CatalogUnit] | None = None,
    listings: Iterable[ListedMember] = (),
) -> Report:
    report = Report()
    schema = load_schema()
    triages: dict[str, dict[str, Any]] = {}
    files = sorted(p for p in triage_dir.glob("*.json") if p.name != "index.json")
    if not files:
        report.add("error", "T-SET", None, f"no triage files in {triage_dir}")
        return report
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report.add("error", "T-SCHEMA", path.stem, f"{path.name} is not readable JSON: {exc}")
            continue
        unit = data.get("unit_id") if isinstance(data, dict) else None
        unit = unit if isinstance(unit, str) and unit else path.stem
        if unit in triages:
            report.add("error", "T-SET", unit, f"unit {unit} is triaged twice")
            continue
        report.units.append(unit)
        problems = schema_errors(data, schema, schema)
        if problems:
            for problem in problems[:20]:
                report.add("error", "T-SCHEMA", unit, f"{path.name}: {problem}")
            if len(problems) > 20:
                report.add("error", "T-SCHEMA", unit, f"{path.name}: {len(problems) - 20} more schema problem(s)")
            continue
        if path.stem != unit:
            report.add("error", "T-SET", unit, f"{path.name} holds unit {unit}; name the file {unit}.json")
        triages[unit] = data
        report.unit_status[unit] = data["status"]

    accessions = {t["accession"] for t in triages.values()}
    if len(accessions) > 1:
        report.add("error", "T-SET", None, f"one directory must hold one accession; found {sorted(accessions)}")
    report.accession = sorted(accessions)[0] if accessions else None

    listed = list(listings)
    by_key: dict[str, ListedMember] = {}
    for m in listed:
        by_key.setdefault(member_key(m.path), m)

    for unit, triage in triages.items():
        _check_unit(report, unit, triage, triages, catalog, by_key if listed else None)

    _check_cross_unit(report, triages, listed)
    if catalog is not None:
        missing = sorted(set(catalog) - set(triages))
        if missing and report.accession is not None:
            for unit in missing:
                report.add("hold", "T-SET", unit, f"Catalog unit {unit} has no triage file")
    return report


def _check_unit(
    report: Report,
    unit: str,
    t: dict[str, Any],
    triages: dict[str, dict[str, Any]],
    catalog: dict[str, CatalogUnit] | None,
    listed: dict[str, ListedMember] | None,
) -> None:
    status = t["status"]
    flags = _flag_codes(t)
    ut = t["unit_technical"]
    unit_ion, unit_chrom, unit_acq = ut["ion_mode"], ut["chromatography"], ut["acquisition_mode"]
    kept = _kept(unit, t)

    # T-COVER: every Catalog row kept or excluded, once.
    count = t["catalog_row_count"]
    seen: dict[int, str] = {}
    for where, items in (("rows", t["rows"]), ("excluded_rows", t["excluded_rows"])):
        for item in items:
            i = item["row_index"]
            if i >= count:
                report.add("error", "T-COVER", unit, f"{where} names row {i}, but the handoff has {count} rows")
            elif i in seen:
                report.add("error", "T-COVER", unit, f"row {i} ({item['sample_id']}) is in {seen[i]} and {where}")
            else:
                seen[i] = where
    missing = [i for i in range(count) if i not in seen]
    if missing:
        shown = ", ".join(str(i) for i in missing[:10]) + (" ..." if len(missing) > 10 else "")
        report.add("error", "T-COVER", unit, f"{len(missing)} Catalog row(s) neither kept nor excluded: {shown}")

    if catalog is not None:
        cu = catalog.get(unit)
        if cu is None:
            report.add("error", "T-COVER", unit, f"the Catalog has no unit {unit}")
        else:
            if cu.handoff_sha256 and cu.handoff_sha256 != t["catalog_handoff_sha256"]:
                report.add("error", "T-HANDOFF", unit, "catalog_handoff_sha256 does not match the Catalog handoff; triage again from the current handoff")
            if len(cu.rows) != count:
                report.add("error", "T-COVER", unit, f"catalog_row_count is {count}, the Catalog handoff has {len(cu.rows)} rows")
            for item in t["rows"] + t["excluded_rows"]:
                i = item["row_index"]
                if i < len(cu.rows):
                    row = cu.rows[i]
                    if row.get("sample_id") != item["sample_id"] or row.get("raw_file") != item["raw_file"]:
                        report.add("error", "T-COVER", unit, f"row {i} is {row.get('sample_id')}/{row.get('raw_file')} in the Catalog, not {item['sample_id']}/{item['raw_file']}")
            declared_chrom = normalise_chromatography(cu.declared.get("chromatography"))
            declared_ion = normalise_ion(cu.declared.get("ion_mode"))
            disputed = "declared_technical_disputed" in flags
            if declared_chrom not in ("unknown",) and unit_chrom != declared_chrom and not disputed:
                report.add("error", "T-DECL", unit, f"unit chromatography {unit_chrom} differs from the Catalog's {cu.declared.get('chromatography')!r} without a declared_technical_disputed flag")
            if declared_ion in ("positive", "negative") and unit_ion != declared_ion and not disputed:
                report.add("error", "T-DECL", unit, f"unit ion mode {unit_ion} differs from the Catalog's {cu.declared.get('ion_mode')!r} without a declared_technical_disputed flag")

    # T-EXCL: an exclusion that points at another unit must point at a real one.
    for item in t["excluded_rows"]:
        target = item.get("belongs_to_unit")
        if item["reason"] == "belongs_to_other_unit":
            if not target:
                report.add("error", "T-EXCL", unit, f"row {item['row_index']} is excluded as another unit's without naming the unit")
            elif target == unit:
                report.add("error", "T-EXCL", unit, f"row {item['row_index']} is excluded as belonging to this same unit")
            elif target not in triages:
                report.add("hold", "T-EXCL", unit, f"row {item['row_index']} is said to belong to {target}, which has no triage file here")
            elif not any(r["row_index"] == item["row_index"] or r["sample_id"] == item["sample_id"] for r in triages[target]["rows"]):
                report.add("hold", "T-EXCL", unit, f"row {item['row_index']} ({item['sample_id']}) is said to belong to {target}, but {target} does not keep it")

    # T-STATUS
    if status == "needs_person" and not t["needs_person"]:
        report.add("error", "T-STATUS", unit, "status needs_person with no question in needs_person")
    if status == "ready" and t["needs_person"]:
        report.add("error", "T-STATUS", unit, f"status ready while needs_person holds {len(t['needs_person'])} question(s)")
    if status == "abstained":
        if kept:
            report.add("error", "T-STATUS", unit, "an abstained unit keeps no rows or members")
        if not any(e["reason"] in ("no_signal_abstained", "needs_person") for e in t["excluded_rows"]) and count:
            report.add("error", "T-STATUS", unit, "an abstained unit must exclude its rows as no_signal_abstained")
    if status == "ready":
        if not kept:
            report.add("error", "T-STATUS", unit, "a ready unit keeps at least one file")
        if unit_ion in ("switching", "unknown"):
            report.add("error", "T-POL", unit, f"a ready unit cannot have ion mode {unit_ion}")
        if unit_chrom == "unknown":
            report.add("error", "T-CHROM", unit, "a ready unit cannot have unknown chromatography")
        for code in sorted(flags & BLOCKING_FLAGS):
            if not _cleared(t, code):
                report.add("error", "T-STATUS", unit, f"flag {code} keeps the unit from being ready until a person answers it (person_answers code {code})")
        if unit_ion == "both" and not t.get("children"):
            report.add("error", "T-POL", unit, "a ready unit with both polarities needs children, one per polarity and acquisition")
    else:
        questions = "; ".join(q["code"] for q in t["needs_person"])
        report.add("hold", "T-QUEUE", unit, f"status {status}" + (f": {questions}" if questions else ""))

    # Children partition the kept files, one polarity each.
    child_of: dict[str, dict[str, Any]] = {}
    for child in t.get("children", []):
        for p in child["member_paths"]:
            k = member_key(p)
            if k in child_of:
                report.add("error", "T-CHILD", unit, f"{p} is in children {child_of[k]['child_key']} and {child['child_key']}")
            child_of[k] = child
    if t.get("children"):
        kept_keys = {member_key(k.path) for k in kept}
        for k in kept:
            if member_key(k.path) not in child_of:
                report.add("error", "T-CHILD", unit, f"{k.path} is kept but in no child")
        for k in child_of:
            if k not in kept_keys:
                report.add("error", "T-CHILD", unit, f"child member {k} is not kept by the unit")

    # Per file: polarity, chromatography, acquisition, evidence, tokens, headers.
    acqs: set[str] = set()
    for k in kept:
        item = k.item
        label = k.path
        ion = item["ion_mode"]
        child = child_of.get(member_key(k.path))
        expected_ion = child["ion_mode"] if child else unit_ion
        if ion == "switching" and "polarity_switching" not in flags:
            report.add("error", "T-POL", unit, f"{label} is polarity switching without a polarity_switching flag")
        if expected_ion in ("positive", "negative") and ion in ("positive", "negative") and ion != expected_ion:
            report.add("error", "T-POL", unit, f"{label} is {ion}, its {'child' if child else 'unit'} is {expected_ion}")
        if expected_ion in ("positive", "negative") and ion in ("switching", "both"):
            report.add("error", "T-POL", unit, f"{label} is {ion} inside a {expected_ion} {'child' if child else 'unit'}")
        if ion == "unknown":
            if status == "ready":
                report.add("error", "T-POL", unit, f"{label} has unknown polarity in a ready unit")
        if child and child["acquisition_mode"] not in ("mixed", "unknown") and item["acquisition_mode"] != child["acquisition_mode"]:
            report.add("error", "T-CHILD", unit, f"{label} is {item['acquisition_mode']}, its child {child['child_key']} is {child['acquisition_mode']}")
        chrom = item["chromatography"]
        if chrom not in ("unknown",) and unit_chrom not in ("unknown",) and chrom != unit_chrom:
            report.add("error", "T-CHROM", unit, f"{label} is {chrom}, the unit is {unit_chrom}")
        if chrom == "unknown" and status == "ready":
            report.add("error", "T-CHROM", unit, f"{label} has unknown chromatography in a ready unit")
        acqs.add(item["acquisition_mode"])

        sources = {e["source"] for e in item["evidence"]}
        if item["basis"] == "declared" and not sources & DECLARED_SOURCES:
            report.add("error", "T-EVID", unit, f"{label} is marked declared but cites no repository declaration (catalog_row, catalog_unit, mwtab, repository_page)")
        if status == "ready" and all(e["source"] == "archive_listing" and e["field"].lower() in WEAK_FIELDS for e in item["evidence"]):
            report.add("error", "T-EVID", unit, f"{label} is kept on size or timestamp alone; abstain instead of guessing")

        if "name_token_disputed" not in flags:
            tok_ion = token_polarity(label)
            if tok_ion and ion in ("positive", "negative") and tok_ion != ion:
                report.add("error", "T-TOKEN", unit, f"{label}: its name says {tok_ion}, the triage says {ion} (flag name_token_disputed and ask a person if that is meant)")
            tok_chrom = token_chromatography(label)
            if tok_chrom and chrom in ("HILIC", "RP") and tok_chrom != chrom:
                report.add("error", "T-TOKEN", unit, f"{label}: its name says {tok_chrom}, the triage says {chrom} (flag name_token_disputed and ask a person if that is meant)")

        for e in item["evidence"]:
            if e["source"] != "raw_header":
                continue
            field_name = e["field"].lower()
            value = str(e["value"])
            disagrees = False
            if field_name in ("polarity", "ion_mode") and normalise_ion(value) in ("positive", "negative", "switching") and normalise_ion(value) != ion:
                disagrees = True
            if field_name in ("acquisition_mode", "method") and value.upper() not in ("", "UNKNOWN"):
                header_acq = {"FULLSCAN": "MS1", "MS1": "MS1"}.get(value.upper(), value.upper())
                if header_acq.replace("-", "_") not in (item["acquisition_mode"].upper(),):
                    disagrees = True
            if disagrees and not ("folder_header_disagreement" in flags and status != "ready" or _cleared(t, "folder_header_disagreement")):
                report.add("error", "T-HEADER", unit, f"{label}: the raw header says {e['field']}={value}, the triage says otherwise; a folder/header disagreement goes to a person")

        if listed is not None and item.get("member_path") and member_key(item["member_path"]) not in listed:
            report.add("error", "T-MEMBER", unit, f"{item['member_path']} is not in the archive listing")

    if "MS1" in acqs and "ms1_only" not in flags:
        report.add("error", "T-ACQ", unit, "MS1-only files are kept without an ms1_only flag")
    if "targeted_DDA" in acqs and "targeted_dda" not in flags:
        report.add("error", "T-ACQ", unit, "targeted DDA files are kept without a targeted_dda flag")
    if len(acqs - {"unknown"}) > 1 and "mixed_acquisition" not in flags:
        report.add("error", "T-ACQ", unit, f"kept files mix acquisition modes {sorted(acqs - {'unknown'})} without a mixed_acquisition flag")
    if "unknown" in acqs and status == "ready" and unit_acq == "unknown":
        report.add("error", "T-ACQ", unit, "a ready unit with unknown acquisition")

    # shared_with: reciprocal, and the paths are kept by both.
    for share in t["shared_with"]:
        other = triages.get(share["unit_id"])
        if other is None:
            report.add("error", "T-SHARED", unit, f"shared_with names {share['unit_id']}, which has no triage file here")
            continue
        mine = {member_key(p) for p in share["member_paths"]}
        theirs = set()
        for s in other["shared_with"]:
            if s["unit_id"] == unit:
                theirs |= {member_key(p) for p in s["member_paths"]}
        if mine - theirs:
            report.add("error", "T-SHARED", unit, f"{len(mine - theirs)} path(s) shared with {share['unit_id']} are not declared back by it")

    for answer in t.get("person_answers", []):
        if not answer["by"].strip():
            report.add("error", "T-PERSON", unit, "a person answer must name the person")


def _check_cross_unit(report: Report, triages: dict[str, dict[str, Any]], listed: list[ListedMember]) -> None:
    owners: dict[str, list[Kept]] = defaultdict(list)
    for unit, t in triages.items():
        for k in _kept(unit, t):
            owners[k.key].append(k)

    def declared_share(a: str, b: str, key: str) -> bool:
        def has(x: str, y: str) -> bool:
            return any(s["unit_id"] == y and key in {member_key(p) for p in s["member_paths"]} for s in triages[x]["shared_with"])

        return has(a, b) and has(b, a)

    # T-UNIQ: no file kept by two units unless both declare it. One finding per pair of units.
    undeclared: dict[tuple[str, str], list[str]] = defaultdict(list)
    for key, holders in owners.items():
        units = sorted({h.unit for h in holders})
        if len(holders) > len(units):
            report.add("error", "T-UNIQ", units[0], f"{holders[0].path} is kept twice by the same unit")
        for i, a in enumerate(units):
            for b in units[i + 1 :]:
                if not declared_share(a, b, key):
                    undeclared[(a, b)].append(holders[0].path)
    for (a, b), paths in sorted(undeclared.items()):
        report.add("error", "T-UNIQ", a, f"{len(paths)} file(s) kept by both {a} and {b} without shared_with on both (first: {sorted(paths)[0]})")

    # Identical kept file sets (the stage3b failure) are named once, on top of the per-file errors.
    sets: dict[frozenset[str], list[str]] = defaultdict(list)
    for unit, t in triages.items():
        keys = frozenset(k.key for k in _kept(unit, t))
        if keys:
            sets[keys].append(unit)
    for keys, units in sets.items():
        if len(units) > 1:
            report.add("error", "T-UNIQ", units[0], f"units {', '.join(sorted(units))} keep the identical set of {len(keys)} files")

    if not listed:
        return

    # T-DUP: byte-identical content (CRC32 + size) anywhere in the listing.
    content: dict[tuple[str, int], list[ListedMember]] = defaultdict(list)
    folders: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for m in listed:
        mk = member_key(m.path)
        if VENDOR_FOLDER.match(_norm_path(m.path)):
            inner = _norm_path(m.path)[len(mk) :].casefold()
            folders[mk].append((inner, m.crc32, m.size))
        elif m.size >= MIN_DUPLICATE_BYTES and m.crc32 and is_raw_like(m.path):
            content[(m.crc32, m.size)].append(m)
    twins: dict[str, set[str]] = defaultdict(set)
    for group in content.values():
        keys = {member_key(m.path) for m in group}
        if len(keys) > 1:
            for k in keys:
                twins[k] |= keys - {k}
    by_signature: dict[tuple, list[str]] = defaultdict(list)
    for folder, parts in folders.items():
        by_signature[tuple(sorted(parts))].append(folder)
    for group in by_signature.values():
        if len(group) > 1:
            for k in group:
                twins[k] |= set(group) - {k}

    owner_of = {key: sorted({h.unit for h in holders}) for key, holders in owners.items()}
    reported: set[tuple[str, frozenset[str]]] = set()
    for key, holders in owners.items():
        if key not in twins:
            continue
        for unit in sorted({h.unit for h in holders}):
            t = triages[unit]
            twin_units = sorted({u for tw in twins[key] for u in owner_of.get(tw, [])} - {unit})
            twin_inside = any(unit in owner_of.get(tw, []) for tw in twins[key])
            tag = (unit, frozenset(twin_units))
            settled = "shared_content" in _flag_codes(t) and (t["status"] != "ready" or _cleared(t, "shared_content"))
            if twin_inside and not settled:
                report.add("error", "T-DUP", unit, f"{holders[0].path} has a byte-identical twin kept by the same unit")
            if tag in reported:
                continue
            reported.add(tag)
            where = f"kept by {', '.join(twin_units)}" if twin_units else "elsewhere in the archive"
            if settled:
                report.add("hold", "T-DUP", unit, f"byte-identical content shared with files {where} (first: {holders[0].path}); flagged, waiting for a person")
            else:
                report.add("error", "T-DUP", unit, f"{holders[0].path} is byte-identical (CRC32 and size) to a file {where}; shared content is never settled by name: flag shared_content and send the unit to a person")

    # Raw-looking members that no unit kept or excluded. A row without a member path accounts for the
    # member of the same name, or of the same stem when the row's raw_file carries no extension.
    accounted: set[str] = set(owners)
    for t in triages.values():
        accounted |= {member_key(e["member_path"]) for e in t.get("excluded_members", [])}
        for item in t["rows"] + t["excluded_rows"]:
            if item.get("member_path"):
                accounted.add(member_key(item["member_path"]))
            else:
                accounted.add(_name_key(item["raw_file"]))
    loose: list[str] = []
    for m in listed:
        if not is_raw_like(m.path):
            continue
        if member_key(m.path) in accounted or "name:" + basename(m.path).casefold() in accounted or "stem:" + stem(m.path).casefold() in accounted:
            continue
        loose.append(m.path)
    loose = sorted(set(member_key(p) for p in loose))
    if loose:
        shown = ", ".join(loose[:5]) + (" ..." if len(loose) > 5 else "")
        report.add("hold", "T-MEMBER", None, f"{len(loose)} raw member(s) in the archive that no unit kept or excluded: {shown}")


# ----------------------------------------------------------------------------------------- scoring


def score(triage_dir: Path, ground_truth: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Per unit: correct, wrong_included, wrongly_left_out, against ground_truth.json (eval fixtures)."""
    unchecked = {member_key(m["path"]) for m in ground_truth.get("unchecked_members", [])}
    unchecked_names = {basename(m["path"]).casefold() for m in ground_truth.get("unchecked_members", [])}
    overrides = {k: _norm_path(v) for k, v in ground_truth.get("row_member_overrides", {}).items()}
    out: dict[str, dict[str, int]] = {}
    for unit in ground_truth["units"]:
        uid = unit["unit_id"]
        truth = {member_key(m["path"]) for m in unit["members"]}
        truth_by_name: dict[str, str] = {}
        for m in unit["members"]:
            truth_by_name.setdefault(basename(m["path"]).casefold(), member_key(m["path"]))
            truth_by_name.setdefault(stem(m["path"]).casefold(), member_key(m["path"]))
        path = triage_dir / f"{uid}.json"
        kept: set[str] = set()
        other: set[str] = set()
        if path.exists():
            t = json.loads(path.read_text(encoding="utf-8"))
            for row in t.get("rows", []):
                if row.get("member_path"):
                    kept.add(member_key(row["member_path"]))
                    continue
                raw = row["raw_file"]
                if raw in overrides:
                    kept.add(member_key(overrides[raw]))
                    continue
                name = basename(raw).casefold()
                hit = truth_by_name.get(name) or truth_by_name.get(stem(raw).casefold())
                if hit:
                    kept.add(hit)
                elif name not in unchecked_names:
                    other.add("raw:" + name)
            for member in t.get("added_members", []):
                kept.add(member_key(member["member_path"]))
        kept -= unchecked
        out[uid] = {
            "correct": len(kept & truth),
            "wrong_included": len(kept - truth) + len(other),
            "wrongly_left_out": len(truth - kept),
        }
    return out


# ---------------------------------------------------------------------------------------------- CLI


def _render(report: Report) -> str:
    lines = [f"triage {report.accession or '?'}: {report.verdict.upper()}"]
    for unit in report.units:
        lines.append(f"  {unit}: status {report.unit_status.get(unit, '?')}, {report.unit_verdict(unit)}")
    shown: dict[tuple[str, str, str | None], int] = defaultdict(int)
    for f in report.findings:
        group = (f.level, f.check, f.unit)
        shown[group] += 1
        if shown[group] <= 10:
            lines.append(f"  [{f.level}] {f.check} {f.unit or '-'}: {f.message}")
    for (level, check, unit), n in shown.items():
        if n > 10:
            lines.append(f"  [{level}] {check} {unit or '-'}: ... {n - 10} more (all of them with --json)")
    if report.score is not None:
        lines.append("  score against ground truth:")
        for unit, s in report.score.items():
            lines.append(f"    {unit}: correct {s['correct']}, wrong_included {s['wrong_included']}, wrongly_left_out {s['wrongly_left_out']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("triage_dir", type=Path)
    parser.add_argument("--catalog", type=Path, help="handoffs directory or catalog.json")
    parser.add_argument("--archive-listing", type=Path, action="append", default=[], help="archive member TSV (repeatable)")
    parser.add_argument("--ground-truth", type=Path, help="ground_truth.json to score against")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    if not args.triage_dir.is_dir():
        print(f"not a directory: {args.triage_dir}", file=sys.stderr)
        return 2
    catalog = read_catalog(args.catalog) if args.catalog else None
    listed: list[ListedMember] = []
    for path in args.archive_listing:
        listed += read_listing(path)
    report = validate(args.triage_dir, catalog, listed)
    if args.ground_truth:
        report.score = score(args.triage_dir, json.loads(args.ground_truth.read_text(encoding="utf-8")))
    print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False) if args.json else _render(report))
    return {"pass": 0, "hold": 1, "error": 2}[report.verdict]


if __name__ == "__main__":
    sys.exit(main())
