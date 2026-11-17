"""Check one accession's msdial-unit-triage files (v1 or v2) and their decision records before anything is planned.

A triage file (schemas/msdial-unit-triage.v2.json, written by the msdial-unit-triage skill; v1 files stay readable)
records which Catalog rows and archive members one analysis unit keeps. Every choice in it is a model inference. This
script is the deterministic stage that stands between those inferences and a plan: it reads the triage files of one
accession as a set, with the decision record (msdial-llm-decision-record.v1) beside each and the accession's
review-log.jsonl, and returns pass, hold or error (docs/llm-assisted-curation.md sections 2.2 to 2.4).

    python scripts/validate-triage.py <triage dir>
        [--catalog <handoffs dir | catalog.json>]   Catalog rows (and handoff sha256) to check coverage against
        [--archive-listing <tsv>]...                 zip member listing(s): path, size, crc32 [, archive]
        [--ground-truth <ground_truth.json>]         score the assignment against a known answer (eval)
        [--code-root <dir>]                          the released code (skill, prompt templates) at its commit,
                                                     read in place of git at each record's repo_commit
        [--release-bundle <dir>]                     the release bundle the triage dir sits in (found on its own
                                                     when <dir>/release-index.json is two levels up)
        [--emit-selection]                           add each unit's selection to the report and append a
                                                     validator_verdict per decision to the review log
        [--json]

The triage directory holds one <unit_id>.json per unit, all of one accession (in a campaign,
<campaign dir>/triage/<accession>/). A unit redone is kept as a revision, <unit_id>.r<N>.json, and the highest
N is the one checked (it supersedes <unit_id>.json and every earlier revision); a decision record beside a
triage (<name>.record.json, the envelope naming the model and prompt) is not a triage.
--catalog takes either the campaign's handoffs directory
(<unit_id>/handoff.json and samples.json, as msdial_catalog_reanalysis_handoff writes them; it may span many
accessions, and only the units of the triaged accession are checked) or a catalog.json with
{"accession", "units": [{"unit_id", "declared", "rows": [{"sample_id", "raw_file"}]}]}, the shape the eval
fixtures use. An archive listing is the archive-members TSV the download store keeps, or any TSV with path,
size and crc32 columns; it may be read remotely from the zip central directory before download.

A check that cannot run is not passed: without --catalog every unit holds (rows not checked against the
handoff), and without --archive-listing every unit holds (byte-identical content not ruled out).

A kept row may name its file by member_path or only by raw_file. Both resolve to one key (the listing, or
without one the member paths the triage files name, by exact name, then stem), so the same file is the
same file in every check, and its name tokens and format are read from the member it resolves to.

THE DECISION RECORD (section 2.3). Beside each payload the wrapper writes <name>.record.json. This script recomputes,
refusing any mismatch (T-RECORD): decision_id and payload.sha256 from the payload's bytes; the sha256 and bytes of
every inputs[] item, read relative to the record; skill.sha256 from SKILL.md at repo_commit (git show in this
repository, or --code-root), and the skill's version; prompt.template_sha256 at repo_commit and
prompt.rendered_sha256; wrapper.sha256 at its repo_commit; every tool_calls[].result_sha256; transcript.sha256 of the
released redacted transcript. It refuses a tool call that is not on the skill's allowed-tools list, and a tool_calls
list that differs from the calls the released transcript shows in order, server-qualified name, arguments or stored
result (T-TOOLS); a model_id or session_id the transcript does not name (T-RECORD); and a payload whose
catalog_handoff_sha256 is not the catalog_handoff input its record binds (T-HANDOFF). A context that is not clean
(configuration, context_files, memory, hooks, an MCP server the skill does not allow or one the calls use unlisted,
empty harness_flags, a claude-code session without the clean flags) is a hold (T-CONTEXT). It fails closed: a check
that cannot run holds (the skill, the template or the wrapper not readable at repo_commit, a wrapper that was not a
commit's, a v2 payload without a record, a record without a released transcript (T-TRANSCRIPT), a model 'unknown', a
tool call without a stored result, a record that binds no Catalog evidence or no archive listing, a listing the record
binds that was not given or one given that no record binds). A v1 payload may have no record; it is reported
rationale_not_recorded (a note, which changes no verdict).

THE REVIEW LOG (section 2.4). For a v2 payload, a ready unit that carries a blocking flag passes only when
review-log.jsonl holds a person_answer whose question_code is the flag's code and whose decision_id is this payload or
one it supersedes (by its record's supersedes, or a revision entry); v1 payloads keep their person_answers. A revision
that drops a blocking flag the payload it supersedes carried passes only when the cause of its revision entry cites
such a person_answer (T-REVISION). A review log whose hash chain is broken is an error (T-LOG).

RELEASE-BUNDLE MODE (sections 3 and 5.3). In a release bundle a triage input that held an absolute path (a Catalog
handoff) is released as a projection: release-index.json's triage_inputs names its original_sha256 and the
release_sha256 of the released bytes. Such a file is checked through the index: its bytes must hash to
release_sha256, and original_sha256 must be the sha256 the record (or, for --catalog, the triage) names.

Levels:
- error: the triage file breaks the contract (schema, coverage, a file in two units, byte-identical content
  settled by name, polarity or chromatography that disagrees with its unit or child, an unflagged MS1-only or
  mixed acquisition, a kept mzXML or mzData file, one acquisition kept twice in two formats, a member both
  kept and excluded, an exclusion reason the listing or the unit's own status and flags contradict, evidence or a
  decision-record input that cites the Class proposal (T-CLASS: the triage runs before the Class and never binds it,
  WS0 decision F), a decision record that does not hash to what it names, a tool the skill does not allow, ...).
  The triage must be redone; nothing may be planned from it.
- hold: the file is valid but something waits for a person: a unit that needs a person, abstained or is out
  of scope, flagged shared content, a member nobody accounted for, a kept file inferred at low confidence in a ready
  unit without a person's inferred_low_confidence answer (T-CONFIDENCE), a context that was not clean, a check that
  could not run.
- pass: every unit is ready and nothing waits.
- note: recorded, never a verdict (rationale_not_recorded).

Exit status: 0 pass, 1 hold, 2 error. The script reads only, except that --emit-selection appends validator_verdict
entries to the accession's review-log.jsonl (through scripts/campaign/curated.append_review_log); a release bundle is
never written to.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_ID = "msdial-unit-triage.v1"
SCHEMA_V2_ID = "msdial-unit-triage.v2"
SCHEMA_PATH = REPO_ROOT / "schemas" / "msdial-unit-triage.v1.json"
SCHEMA_PATHS = {SCHEMA_ID: SCHEMA_PATH, SCHEMA_V2_ID: REPO_ROOT / "schemas" / "msdial-unit-triage.v2.json"}
RECORD_SCHEMA_ID = "msdial-llm-decision-record.v1"
RECORD_SCHEMA_PATH = REPO_ROOT / "schemas" / "msdial-llm-decision-record.v1.json"
SKILL_NAME = "msdial-unit-triage"
REVIEW_LOG = "review-log.jsonl"
RELEASE_INDEX = "release-index.json"
TOOL_NAME = "validate-triage.py"

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
# The question code of a person's answer that lets a ready unit rest on a kept file inferred at low confidence
# (T-CONFIDENCE): the person looked at it (scripts/record-person-answer.py, section 2.2).
LOW_CONFIDENCE_CODE = "inferred_low_confidence"
# What a claude-code session must have run with for its context to count as clean (scripts/run-triage-agent.py
# CLEAN_FLAGS and its environment and settings, as agent.context.harness_flags lists them).
CLAUDE_CODE_CLEAN_FLAGS = (
    "--restricted", "--strict-mcp-config", "--tools", "--allowedTools", "--permission-mode", "dontAsk",
    "--disable-slash-commands", "--no-session-persistence", "env CLAUDE_CODE_DISABLE_CLAUDE_MDS=1",
    "env CLAUDE_CODE_DISABLE_AUTO_MEMORY=1", 'settings {"disableAllHooks":true}',
)
# The roles a unit triage's record binds its Catalog evidence under.
CATALOG_ROLES = ("catalog_handoff", "catalog_samples", "catalog_unit")


# --------------------------------------------------------------------------------------------- findings


@dataclass
class Finding:
    level: str  # "error", "hold", or "note" (recorded, never a verdict)
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
    payloads: dict[str, dict[str, Any]] = field(default_factory=dict)  # unit -> payload file name, schema, decision_id
    extra: dict[str, Any] = field(default_factory=dict)  # --emit-selection: selection, review_log

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
            # The schema only: a payload's decision_id would tie every unit's report_sha256 to every other unit's bytes
            # (the plan records the report's sha256 per unit). --emit-selection reports the decision ids.
            "units": {u: {"status": self.unit_status.get(u), "verdict": self.unit_verdict(u),
                          "schema": self.payloads.get(u, {}).get("schema")} for u in self.units},
            "findings": [f.as_dict() for f in self.findings],
        }
        if self.score is not None:
            out["score"] = self.score
        out.update(self.extra)
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


def load_schema(schema_id: str = SCHEMA_ID) -> dict[str, Any]:
    """The JSON schema of a payload version (msdial-unit-triage.v1 or .v2) or of the decision record."""
    path = RECORD_SCHEMA_PATH if schema_id == RECORD_SCHEMA_ID else SCHEMA_PATHS[schema_id]
    return json.loads(path.read_text(encoding="utf-8"))


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
    accession: str | None = None
    problem: str | None = None  # a handoff released as a projection whose bytes are not the ones the index names


def read_catalog(path: Path, bundle: "ReleaseBundle | None" = None) -> dict[str, CatalogUnit]:
    """The Catalog units a handoffs directory or a catalog.json holds. In a release bundle a handoff.json released as a
    projection (release-index.json triage_inputs) stands for its original: its sha256 is the index's original_sha256,
    once its bytes are checked against the index's release_sha256 (a mismatch is the unit's `problem`)."""
    units: dict[str, CatalogUnit] = {}
    if path.is_dir():
        for handoff in sorted(path.glob("*/handoff.json")):
            unit_dir = handoff.parent
            raw = handoff.read_bytes()
            data = json.loads(raw.decode("utf-8-sig"))
            handoff_sha = hashlib.sha256(raw).hexdigest()
            problem = None
            entry = bundle.projected(handoff) if bundle is not None else None
            if entry is not None:
                if handoff_sha != entry["release_sha256"]:
                    problem = (f"{handoff.name} of {unit_dir.name} hashes to {handoff_sha}, not the release index's "
                               f"release_sha256 {entry['release_sha256']}; its original sha256 is not taken on trust")
                handoff_sha = entry["original_sha256"]
            samples_path = unit_dir / "samples.json"
            rows = json.loads(samples_path.read_text(encoding="utf-8")) if samples_path.exists() else data.get("sample_metadata") or []
            ts = data.get("technical_settings") or {}
            units[unit_dir.name] = CatalogUnit(
                unit_id=unit_dir.name,
                rows=rows,
                declared={k: ts.get(k) for k in ("chromatography", "ion_mode", "acquisition_mode")},
                handoff_sha256=handoff_sha,
                accession=data.get("accession"),
                problem=problem,
            )
        return units
    data = json.loads(path.read_text(encoding="utf-8"))
    for unit in data["units"]:
        units[unit["unit_id"]] = CatalogUnit(
            unit_id=unit["unit_id"],
            rows=unit["rows"],
            declared=unit.get("declared") or {},
            handoff_sha256=unit.get("handoff_sha256"),
            accession=unit.get("accession") or data.get("accession"),
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


CONVERSION_EXTENSIONS = (".mzxml", ".mzdata")


def _key_stem(key: str) -> str:
    return key[4:] if key.startswith("raw:") else stem(key).casefold()


class Resolver:
    """Gives every file one key, whether a triage names it by member path or only by the row's raw_file.

    The index is the archive listing when there is one, and otherwise every member path the triage files
    name. A name resolves to a member by its exact file name, then by its stem, then by its stem among
    members that need no conversion; anything else stays "raw:<stem>" (unresolved).
    """

    def __init__(self, members: Iterable[str]):
        self.members: set[str] = set()
        self.original: dict[str, str] = {}  # key -> the member path as the listing or triage spells it
        self.by_name: dict[str, set[str]] = defaultdict(set)
        self.by_stem: dict[str, set[str]] = defaultdict(set)
        for path in members:
            if not is_raw_like(path):
                continue
            key = member_key(path)
            self.members.add(key)
            norm = _norm_path(path)
            folder = VENDOR_FOLDER.match(norm)
            self.original.setdefault(key, folder.group(1) if folder else norm)
            self.by_name[basename(key)].add(key)
            self.by_stem[stem(key).casefold()].add(key)

    def hits(self, name: str) -> set[str]:
        """Every indexed member the name could mean: same file name, or failing that, same stem."""
        return set(self.by_name.get(basename(name).casefold(), ())) or set(self.by_stem.get(stem(name).casefold(), ()))

    def resolve(self, name: str) -> str | None:
        found = self.by_name.get(basename(name).casefold(), set())
        if len(found) == 1:
            return next(iter(found))
        found = self.by_stem.get(stem(name).casefold(), set())
        if len(found) > 1:
            found = {k for k in found if not k.endswith(CONVERSION_EXTENSIONS)}
        return next(iter(found)) if len(found) == 1 else None

    def key(self, path: str) -> tuple[str, bool]:
        """(key, resolved) for a member path or a bare file name."""
        mk = member_key(path)
        if mk in self.members or "/" in _norm_path(path):
            return mk, True
        found = self.resolve(path)
        return (found, True) if found else ("raw:" + stem(path).casefold(), False)


@dataclass
class Kept:
    """One file a unit keeps: a Catalog row or an added member."""

    unit: str
    key: str
    path: str  # member path, or the row's raw_file when the member is unknown
    item: dict[str, Any]
    is_row: bool
    resolved: bool = True  # False: a row with no member_path whose raw_file names no single member
    member: str | None = None  # the member path the file resolves to (None when unresolved)

    @property
    def stem(self) -> str:
        return _key_stem(self.key)


def _kept(unit: str, triage: dict[str, Any], resolver: Resolver) -> list[Kept]:
    out = []
    for row in triage.get("rows", []):
        if row.get("member_path"):
            out.append(Kept(unit, member_key(row["member_path"]), row["member_path"], row, True, True, row["member_path"]))
        else:
            key, resolved = resolver.key(row.get("raw_file") or "")
            member = resolver.original.get(key, key) if resolved else None
            out.append(Kept(unit, key, row.get("raw_file") or "", row, True, resolved, member))
    for member in triage.get("added_members", []):
        out.append(Kept(unit, member_key(member["member_path"]), member["member_path"], member, False, True, member["member_path"]))
    return out


def same_file(kept: Kept, path: str, resolver: Resolver) -> bool:
    """Does path (a member path or a bare file name) name the file kept is? Unresolved names match by stem."""
    key, resolved = resolver.key(path)
    if key == kept.key:
        return True
    return (not resolved or not kept.resolved) and _key_stem(key) == kept.stem


def _extension(key: str) -> str:
    m = re.search(r"(?i)\.(raw|mzml|mzxml|mzdata|wiff2?|d|lcd|cdf)$", key)
    return m.group(1).casefold() if m else ""


def _runnable_form(item: dict[str, Any], resolver: Resolver) -> str | None:
    """For an exclusion as requires_conversion: the form of the file MS-DIAL reads as it is, or None.

    A member path is read as it is named. A row named only by raw_file is read through the index: the
    member it resolves to (which prefers a vendor or mzML form over mzXML and mzData), else every member of
    its name or stem, else the raw_file's own extension.
    """
    def runnable(path: str) -> bool:
        return is_raw_like(path) and not _norm_path(path).casefold().endswith(CONVERSION_EXTENSIONS)

    if item.get("member_path"):
        return item["member_path"] if runnable(item["member_path"]) else None
    name = item.get("raw_file") or ""
    key = resolver.resolve(name)
    if key is not None:
        return resolver.original.get(key, key) if runnable(key) else None
    hits = resolver.hits(name)
    if hits:
        return None if any(not runnable(h) for h in hits) else resolver.original.get(min(hits), min(hits))
    return name if runnable(name) else None


def _triage_member_paths(triages: dict[str, dict[str, Any]]) -> list[str]:
    paths = []
    for t in triages.values():
        for item in [*t.get("rows", []), *t.get("excluded_rows", []), *t.get("added_members", []), *t.get("excluded_members", [])]:
            if item.get("member_path"):
                paths.append(item["member_path"])
        for child in t.get("children", []):
            paths += child["member_paths"]
    return paths


def _flag_codes(triage: dict[str, Any]) -> set[str]:
    return {f["code"] for f in triage.get("flags", [])}


_REVISION = re.compile(r"^(?P<unit>.+)\.r(?P<n>[0-9]+)$")
RECORD_SUFFIX = ".record.json"


def triage_file_unit(path: Path) -> str:
    """The unit a triage file is named for: <unit>.json, or a revision <unit>.r<N>.json."""
    match = _REVISION.match(path.stem)
    return match.group("unit") if match else path.stem


def current_triage_files(triage_dir: Path) -> list[Path]:
    """The triage files of a directory as the campaign keeps them (scripts/campaign/curated.py): every *.json but
    index.json and the decision records (<name>.record.json, the envelope beside a payload), and of a unit with
    revisions (<unit>.r<N>.json) only the highest, which supersedes <unit>.json and every earlier revision."""
    current: dict[str, tuple[int, Path]] = {}
    for path in sorted(triage_dir.glob("*.json")):
        if path.name == "index.json" or path.name.endswith(RECORD_SUFFIX):
            continue
        match = _REVISION.match(path.stem)
        unit, number = (match.group("unit"), int(match.group("n"))) if match else (path.stem, 0)
        if unit not in current or number > current[unit][0]:
            current[unit] = (number, path)
    return sorted(path for _number, path in current.values())


# ------------------------------------------------------------------------------------ the Class is not the triage's
# WS0 decision F (2026-11-17): the triage runs before any Class is saved and binds the evidence it read (the Catalog's
# rows, files and listing); the Class binds through its own ratification afterwards. A triage that cites the Class
# (the handoff's class_proposal: its assignments, labels, contrast, status or ratification) would make the selection
# depend on a grouping the triage does not bind, so it is an error: the triage is redone without it.
CLASS_CONTENT = re.compile(r"(?i)class[ _.-]?proposal|class[ _.-]?assignment|class_label|ratified_by|ratification")


def _citations(value: Any) -> Iterable[dict[str, Any]]:
    """Every evidence citation in a triage document: an object naming a source (evidence, evidence_sources)."""
    if isinstance(value, dict):
        if isinstance(value.get("source"), str):
            yield value
        for item in value.values():
            yield from _citations(item)
    elif isinstance(value, list):
        for item in value:
            yield from _citations(item)


def class_citations(triage: Any) -> list[str]:
    """What in a triage document cites the Class: an evidence citation whose field, value or detail names the
    class_proposal or its content. The validator reads no class_proposal itself."""
    found: list[str] = []
    for citation in _citations(triage):
        for key in ("field", "value", "detail"):
            text = citation.get(key)
            if isinstance(text, str) and CLASS_CONTENT.search(text):
                found.append(f"evidence from {citation.get('source')} cites the Class in its {key} ({text[:80]!r})")
                break
    return found


def record_class_citations(triage_path: Path) -> list[str]:
    """What the decision record beside a triage (<name>.record.json) binds of the Class: an input whose role, path or
    selector names the class_proposal. Only the inputs are read; a record that is missing or not JSON is left to the
    plan's own checks."""
    record = triage_path.with_name(triage_path.name[: -len(".json")] + RECORD_SUFFIX)
    try:
        value = json.loads(record.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    found: list[str] = []
    for item in (value.get("inputs") if isinstance(value, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        for key, text in item.items():
            if key not in ("sha256", "bytes") and isinstance(text, str) and CLASS_CONTENT.search(text):
                found.append(f"its decision record {record.name} binds an input whose {key} names the Class ({text[:80]!r})")
                break
    return found


# ------------------------------------------------------------------------------ the decision record (section 2.3)
# The model writes only the payload; the wrapper writes the envelope from the harness's transcript. Everything the
# envelope names by sha256 is recomputed here from the stored bytes. Nothing here runs a model or reads a Class.


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> bytes:
    """Sorted keys, no spaces, UTF-8 (scripts/campaign/curated.canonical): the form every digest here is taken over."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


class CodeSource:
    """The repository files a record names at its repo_commit (SKILL.md, the prompt template).

    By default `git show <commit>:<path>` in the repository this script belongs to, so a record is checked against the
    exact bytes it names; with --code-root, the file at <code root>/<path> (the released code, at its commit, for a third
    party without the repository). read() gives (bytes, None) or (None, why it could not be read)."""

    def __init__(self, code_root: Path | None = None, repo: Path = REPO_ROOT):
        self.code_root = Path(code_root) if code_root is not None else None
        self.repo = repo
        self._cache: dict[tuple[str, str], tuple[bytes | None, str | None]] = {}

    def read(self, commit: str, path: str) -> tuple[bytes | None, str | None]:
        relative = str(path or "").replace("\\", "/").strip("/")
        if not relative or _ABSOLUTE.match(str(path)) or ".." in relative.split("/"):
            return None, f"{path!r} is not a path relative to the repository"
        key = (commit, relative)
        if key not in self._cache:
            self._cache[key] = self._read(commit, relative)
        return self._cache[key]

    def _read(self, commit: str, relative: str) -> tuple[bytes | None, str | None]:
        if self.code_root is not None:
            try:
                return (self.code_root / relative).read_bytes(), None
            except OSError as error:
                return None, f"{relative} is not in the code root ({type(error).__name__})"
        if not re.fullmatch(r"[0-9a-f]{40}", str(commit or "")):
            return None, f"repo_commit {commit!r} is not a commit id"
        try:
            done = subprocess.run(["git", "-C", str(self.repo), "show", f"{commit}:{relative}"],
                                  capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            return None, f"git could not run ({type(error).__name__})"
        if done.returncode != 0:
            return None, f"{relative} at {commit[:12]} is not in this repository"
        return done.stdout, None


def code_commit(repo: Path = REPO_ROOT) -> str | None:
    """The commit this validator runs at: HEAD, when the running script is the one HEAD holds (line endings aside);
    None when it is not a commit's (an edited working tree) or git cannot say."""
    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, timeout=60, check=False)
        if head.returncode != 0:
            return None
        commit = head.stdout.decode("ascii", "replace").strip()
        shown = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:scripts/validate-triage.py"],
                               capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    running = Path(__file__).read_bytes().replace(b"\r\n", b"\n")
    if shown.returncode != 0 or shown.stdout.replace(b"\r\n", b"\n") != running:
        return None
    return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else None


def skill_frontmatter(text: str) -> dict[str, Any]:
    """The YAML front matter of a SKILL.md, as far as this contract reads it: scalars, and lists written either inline
    (a, b) or as '- item' lines. Nothing beyond that is parsed."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, Any] = {}
    key: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        item = re.match(r"^\s+-\s+(.*\S)\s*$", line)
        if item and key is not None:
            if not isinstance(out.get(key), list):
                out[key] = []
            out[key].append(item.group(1).strip("'\""))
            continue
        pair = re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", line)
        if pair:
            key, value = pair.group(1), pair.group(2)
            out[key] = value.strip("'\"") if value else None
    return out


def skill_allowed_tools(text: str) -> list[str] | None:
    """The skill's allowed-tools, in order, or None when it names none."""
    value = skill_frontmatter(text).get("allowed-tools")
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    return list(value) if value else None


def skill_version(text: str) -> str | None:
    match = re.search(r"Skill version: \*\*([0-9]+\.[0-9]+\.[0-9]+)\*\*", text)
    return match.group(1) if match else None


_MCP_TOOL = re.compile(r"^mcp__(?P<server>.+?)__(?P<tool>.+)$")


def tool_server(name: str) -> str | None:
    match = _MCP_TOOL.match(name)
    return match.group("server") if match else None


def bare_tool(name: str) -> str:
    """A tool's own name: mcp__<server>__<tool> is <tool>; a permission rule's '(...)' is dropped."""
    base = str(name or "").split("(", 1)[0].strip()
    match = _MCP_TOOL.match(base)
    return match.group("tool") if match else base


def tool_allowed(name: str, allowed: Iterable[str]) -> bool:
    """A tool call against the skill's allowed-tools: a server-qualified name (mcp__<server>__<tool>) must be listed as
    it is; a bare name (as a record may write an MCP tool, msdial_catalog_get_analysis_unit) matches a listed tool of
    that name."""
    allowed_bases = [str(item).split("(", 1)[0].strip() for item in allowed]
    base = str(name or "").split("(", 1)[0].strip()
    if _MCP_TOOL.match(base):
        return base in allowed_bases
    return base in allowed_bases or base in {bare_tool(item) for item in allowed_bases}


def transcript_tool_names(data: bytes) -> list[str]:
    """The tool calls a harness transcript (JSON lines) shows, in order: every {"type": "tool_use", "name": ...} block,
    once per id."""
    names: list[str] = []
    seen: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "tool_use" and isinstance(value.get("name"), str):
                ident = value.get("id")
                if not (isinstance(ident, str) and ident in seen):
                    if isinstance(ident, str):
                        seen.add(ident)
                    names.append(value["name"])
                return
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for line in data.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            walk(json.loads(line))
        except ValueError:
            continue
    return names


class ReleaseBundle:
    """A release bundle (section 5.3): its root and the triage inputs release-index.json says were released as
    projections, by bundle-relative path."""

    def __init__(self, root: Path):
        self.root = Path(root)
        index = json.loads((self.root / RELEASE_INDEX).read_text(encoding="utf-8-sig"))
        self.inputs: dict[str, dict[str, str]] = {}
        for item in index.get("triage_inputs") or []:
            if isinstance(item, dict) and item.get("release"):
                self.inputs[str(item["release"]).replace("\\", "/")] = {
                    "original_sha256": str(item.get("original_sha256") or "").removeprefix("sha256:").casefold(),
                    "release_sha256": str(item.get("release_sha256") or "").removeprefix("sha256:").casefold(),
                }

    @staticmethod
    def find(triage_dir: Path) -> "ReleaseBundle | None":
        """The bundle a triage directory (<bundle>/triage/<accession>) sits in, or None."""
        root = Path(triage_dir).resolve().parent.parent
        return ReleaseBundle(root) if (root / RELEASE_INDEX).is_file() else None

    def projected(self, path: Path) -> dict[str, str] | None:
        try:
            relative = Path(path).resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return None
        return self.inputs.get(relative)


@dataclass
class Provenance:
    """What the record checks need beyond the triage directory."""

    code: CodeSource = field(default_factory=CodeSource)
    bundle: ReleaseBundle | None = None
    # The sha256 of each --archive-listing file given (None when the caller gave listings without their files), and
    # the listings the records bind, so that the listings checked are the ones the decisions rested on.
    listing_sha256: set[str] | None = None
    bound_listings: set[str] = field(default_factory=set)

    def code_matches(self, data: bytes, expected: str) -> bool:
        """`data` (a file the record names at its commit) hashes to `expected`; from a released code folder, also once
        its line endings are LF, since a checkout with core.autocrlf writes CRLF where the commit holds LF."""
        if sha256_bytes(data) == expected:
            return True
        return self.code.code_root is not None and sha256_bytes(data.replace(b"\r\n", b"\n")) == expected


def transcript_events(data: bytes) -> dict[str, Any]:
    """A harness transcript (stream JSON lines) as a record is checked against it: {init, calls: [{id, name, input,
    result}] in order, once per id, each tool_result paired with its call, models (the init's and every assistant
    message's)}."""
    init: dict[str, Any] | None = None
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    models: list[str] = []
    for line in data.decode("utf-8-sig", "replace").splitlines():
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("type") == "system" and message.get("subtype") == "init" and init is None:
            init = message
            if message.get("model") and str(message["model"]) not in models:
                models.append(str(message["model"]))
        body = message.get("message") if isinstance(message.get("message"), dict) else {}
        if message.get("type") == "assistant" and body.get("model") and str(body["model"]) not in models:
            models.append(str(body["model"]))
        for block in body.get("content") if isinstance(body.get("content"), list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                if block["id"] not in calls:
                    order.append(block["id"])
                    calls[block["id"]] = {"id": block["id"], "name": str(block.get("name") or ""),
                                          "input": block.get("input") if isinstance(block.get("input"), dict) else {},
                                          "result": None}
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                calls[block["tool_use_id"]]["result"] = block
    return {"init": init, "calls": [calls[i] for i in order], "models": models}


def _file_check(report: "Report", unit: str, record: Path, label: str, path_text: Any, expected_sha: Any,
                expected_bytes: Any, bundle: ReleaseBundle | None, check: str = "T-RECORD") -> bytes | None:
    """One file a record names, against the sha256 (and bytes) it records. A file released as a projection is checked
    through the release index. Returns its bytes when they could be read."""
    text = str(path_text or "")
    if not text:
        report.add("error", check, unit, f"{record.name}: {label} names no path")
        return None
    if _ABSOLUTE.match(text):
        report.add("error", check, unit, f"{record.name}: {label} is an absolute path; a record names its files relative "
                   "to itself, because it is released byte for byte")
        return None
    location = record.parent / text
    try:
        data = location.read_bytes()
    except OSError:
        report.add("error", check, unit, f"{record.name}: {label} {text} is not there; the decision cannot be checked "
                   "against what it rested on")
        return None
    expected = str(expected_sha or "").casefold()
    entry = bundle.projected(location) if bundle is not None else None
    if entry is not None:
        if sha256_bytes(data) != entry["release_sha256"]:
            report.add("error", check, unit, f"{record.name}: {label} {text} is released as a projection, but hashes to "
                       f"{sha256_bytes(data)}, not the release index's release_sha256 {entry['release_sha256']}")
        elif entry["original_sha256"] != expected:
            report.add("error", check, unit, f"{record.name}: {label} {text}: the release index's original_sha256 "
                       f"{entry['original_sha256']} is not the {expected or '(none)'} the record names")
        return data
    if sha256_bytes(data) != expected:
        report.add("error", check, unit, f"{record.name}: {label} {text} hashes to {sha256_bytes(data)}; the record names "
                   f"{expected or '(none)'}")
    if expected_bytes is not None and len(data) != expected_bytes:
        report.add("error", check, unit, f"{record.name}: {label} {text} is {len(data)} bytes; the record names {expected_bytes}")
    return data


def check_record(report: "Report", unit: str, triage_dir: Path, payload_path: Path, payload_bytes: bytes,
                 payload: dict[str, Any], provenance: Provenance) -> dict[str, Any] | None:
    """The decision record beside one payload (section 2.3). Returns the record when it could be read as one."""
    record_path = payload_path.with_name(payload_path.name[: -len(".json")] + RECORD_SUFFIX)
    v2 = payload.get("schema") == SCHEMA_V2_ID
    if not record_path.is_file():
        if v2:
            report.add("hold", "T-RECORD", unit, f"{payload_path.name} has no decision record ({record_path.name}); what it "
                       "rested on, the skill, the prompt and the tool calls cannot be checked")
        return None
    try:
        record = json.loads(record_path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        report.add("error", "T-RECORD", unit, f"{record_path.name} is not readable JSON: {error}")
        return None
    schema = load_schema(RECORD_SCHEMA_ID)
    problems = schema_errors(record, schema, schema)
    if problems:
        for problem in problems[:20]:
            report.add("error", "T-RECORD", unit, f"{record_path.name}: {problem}")
        if len(problems) > 20:
            report.add("error", "T-RECORD", unit, f"{record_path.name}: {len(problems) - 20} more schema problem(s)")
        return None
    bundle = provenance.bundle
    sha = sha256_bytes(payload_bytes)
    named = record["payload"]
    if record["decision_id"] != sha:
        report.add("error", "T-RECORD", unit, f"{record_path.name}: decision_id {record['decision_id']} is not the payload's "
                   f"sha256 {sha}")
    if named["sha256"] != sha:
        report.add("error", "T-RECORD", unit, f"{record_path.name}: payload.sha256 {named['sha256']} is not the payload's "
                   f"sha256 {sha}")
    if named["path"] != payload_path.name:
        report.add("error", "T-RECORD", unit, f"{record_path.name}: payload.path {named['path']!r} is not {payload_path.name}")
    if named["schema"] != payload.get("schema"):
        report.add("error", "T-RECORD", unit, f"{record_path.name}: payload.schema {named['schema']!r} is not the payload's "
                   f"{payload.get('schema')!r}")
    if record["kind"] != "unit_triage":
        report.add("error", "T-RECORD", unit, f"{record_path.name}: kind {record['kind']!r} beside a triage payload")
    if record["accession"] != payload.get("accession") or record["unit_id"] != payload.get("unit_id"):
        report.add("error", "T-RECORD", unit, f"{record_path.name}: names {record['accession']}/{record['unit_id']}, the "
                   f"payload {payload.get('accession')}/{payload.get('unit_id')}")
    if record["supersedes"] == sha:
        report.add("error", "T-RECORD", unit, f"{record_path.name}: a decision cannot supersede itself")

    for index, item in enumerate(record["inputs"]):
        _file_check(report, unit, record_path, f"inputs[{index}] ({item['role']})", item["path"], item["sha256"],
                    item["bytes"], bundle)

    # What the decision rested on must be bound: the unit's Catalog evidence and the archive listing. A record that
    # binds neither states nothing a third party can check, so it never passes.
    if record["kind"] == "unit_triage":
        roles = [item["role"] for item in record["inputs"]]
        if not set(roles) & set(CATALOG_ROLES):
            report.add("hold", "T-RECORD", unit, f"{record_path.name} binds no Catalog evidence ({', '.join(CATALOG_ROLES)}); "
                       "the rows the triage covers cannot be traced to what it read")
        listings = [item for item in record["inputs"] if item["role"] == "archive_listing"]
        if not listings:
            report.add("hold", "T-RECORD", unit, f"{record_path.name} binds no archive listing; the selection's crc32 and "
                       "size, and the byte-identical content check, have no bound source")
        for item in listings:
            provenance.bound_listings.add(str(item["sha256"]).casefold())
            if provenance.listing_sha256 is not None and str(item["sha256"]).casefold() not in provenance.listing_sha256:
                report.add("hold", "T-RECORD", unit, f"{record_path.name} binds the listing {item['path']}, which is not "
                           "among the --archive-listing files given; the checks ran on listings the decision did not rest on")
        own = [item for item in record["inputs"] if item["role"] == "catalog_handoff"
               and str(item["path"]).replace("\\", "/") == f"inputs/{unit}/handoff.json"]
        if own and payload.get("catalog_handoff_sha256") and own[0]["sha256"] != payload["catalog_handoff_sha256"]:
            report.add("error", "T-HANDOFF", unit, f"{payload_path.name} names catalog_handoff_sha256 "
                       f"{payload['catalog_handoff_sha256']}, but the handoff its record binds ({own[0]['path']}) is "
                       f"{own[0]['sha256']}")

    # The wrapper that wrote the record, at its commit: its acceptance, context and redaction rules made the record's
    # statements about the session.
    wrapper = record["wrapper"]
    if wrapper["repo_commit"] is None:
        report.add("hold", "T-RECORD", unit, f"{record_path.name}: the wrapper {wrapper['path']} that wrote it was not a "
                   "commit's (an edited working tree), so the rules behind its tool_calls and context cannot be identified")
    else:
        wrapper_bytes, why = provenance.code.read(wrapper["repo_commit"], wrapper["path"])
        if wrapper_bytes is None:
            report.add("hold", "T-RECORD", unit, f"{record_path.name}: the wrapper cannot be read at its commit ({why})")
        elif not provenance.code_matches(wrapper_bytes, wrapper["sha256"]):
            report.add("error", "T-RECORD", unit, f"{record_path.name}: {wrapper['path']} at {wrapper['repo_commit'][:12]} "
                       f"hashes to {sha256_bytes(wrapper_bytes)}; the record names {wrapper['sha256']}")

    # The skill at its commit: its bytes, its version and the tools it allows.
    skill = record["skill"]
    allowed: list[str] | None = None
    if skill["name"] != SKILL_NAME:
        report.add("error", "T-RECORD", unit, f"{record_path.name}: skill {skill['name']!r} is not {SKILL_NAME}")
    skill_bytes, why = provenance.code.read(skill["repo_commit"], skill["path"])
    if skill_bytes is None:
        report.add("hold", "T-RECORD", unit, f"{record_path.name}: SKILL.md cannot be read at repo_commit ({why}); the "
                   "skill and the tools it allows were not checked")
    else:
        text = skill_bytes.decode("utf-8", "replace")
        if not provenance.code_matches(skill_bytes, skill["sha256"]):
            report.add("error", "T-RECORD", unit, f"{record_path.name}: {skill['path']} at {skill['repo_commit'][:12]} hashes "
                       f"to {sha256_bytes(skill_bytes)}; the record names {skill['sha256']}")
        version = skill_version(text)
        if version != skill["version"]:
            report.add("error", "T-RECORD", unit, f"{record_path.name}: the skill at its commit is version {version}, the "
                       f"record names {skill['version']}")
        written_by = (payload.get("decided_by") or {}).get("skill_version")
        if v2 and written_by != skill["version"]:
            report.add("error", "T-RECORD", unit, f"{record_path.name}: the payload was written by skill version "
                       f"{written_by}, the record names {skill['version']}")
        allowed = skill_allowed_tools(text)

    # The prompt: the template at the commit, the rendered prompt beside the record.
    prompt = record["prompt"]
    template, why = provenance.code.read(skill["repo_commit"], prompt["template_path"])
    if template is None:
        report.add("hold", "T-RECORD", unit, f"{record_path.name}: the prompt template cannot be read at repo_commit ({why})")
    elif not provenance.code_matches(template, prompt["template_sha256"]):
        report.add("error", "T-RECORD", unit, f"{record_path.name}: {prompt['template_path']} at its commit hashes to "
                   f"{sha256_bytes(template)}; the record names {prompt['template_sha256']}")
    _file_check(report, unit, record_path, "the rendered prompt", prompt["rendered_path"], prompt["rendered_sha256"], None,
                bundle)

    # The tool calls: in order, each allowed by the skill, each result as stored.
    calls = record["tool_calls"]
    for index, call in enumerate(calls, start=1):
        if call["seq"] != index:
            report.add("error", "T-TOOLS", unit, f"{record_path.name}: tool_calls[{index - 1}] has seq {call['seq']}; the "
                       "calls are numbered 1, 2, ... in the order the transcript shows them")
        if allowed is not None and not tool_allowed(call["tool"], allowed):
            report.add("error", "T-TOOLS", unit, f"{record_path.name}: tool call {call['seq']} uses {call['tool']}, which "
                       f"the skill does not allow ({', '.join(allowed)})")
        if call["result_path"] is not None or call["result_sha256"] is not None or call["status"] == "ok":
            _file_check(report, unit, record_path, f"the result of tool call {call['seq']}", call["result_path"],
                        call["result_sha256"], None, bundle, check="T-TOOLS")
        else:
            report.add("hold", "T-TOOLS", unit, f"{record_path.name}: tool call {call['seq']} ({call['tool']}) has no "
                       "stored result; what it returned to the agent cannot be checked")
    if allowed is None and calls and skill_bytes is not None:
        report.add("hold", "T-TOOLS", unit, f"{record_path.name}: the skill at its commit lists no allowed-tools, so its "
                   f"{len(calls)} tool call(s) cannot be checked against it")

    # The released redacted transcript: the harness's own account, which tool_calls, the model and the session must
    # match. Without it they are the wrapper's statements, which nobody else can check, so the decision holds.
    transcript = record["transcript"] or {}
    released = transcript.get("released_path")
    if not released or not transcript.get("sha256"):
        report.add("hold", "T-TRANSCRIPT", unit, f"{record_path.name} names no released transcript; its tool_calls, model "
                   "and session are the wrapper's statements, not checked against the harness's own record")
    else:
        candidates = [record_path.parent / released, Path(triage_dir).parent.parent / released]
        if bundle is not None:
            candidates.append(bundle.root / released)
        found = None if _ABSOLUTE.match(str(released)) else next((p for p in candidates if p.is_file()), None)
        if found is None:
            report.add("hold", "T-TRANSCRIPT", unit, f"{record_path.name}: the redacted transcript {released} is not here; "
                       "its sha256 and the tool calls it shows were not checked")
        else:
            data = found.read_bytes()
            if sha256_bytes(data) != str(transcript.get("sha256") or ""):
                report.add("error", "T-RECORD", unit, f"{record_path.name}: the released transcript {released} hashes to "
                           f"{sha256_bytes(data)}; the record names {transcript.get('sha256')}")
            else:
                compare_transcript(report, unit, record_path, record, data, allowed)

    # The context: clean, or a hold.
    context = record["agent"]["context"]
    if context["configuration"] != "clean":
        report.add("hold", "T-CONTEXT", unit, f"{record_path.name}: the agent ran in configuration "
                   f"{context['configuration']!r}, not clean")
    if context["context_files"]:
        shown_files = ", ".join(item["path"] for item in context["context_files"][:3])
        report.add("hold", "T-CONTEXT", unit, f"{record_path.name}: the harness loaded {len(context['context_files'])} "
                   f"context file(s) ({shown_files}); a clean run loads none")
        for item in context["context_files"]:
            location = record_path.parent / item["path"]
            if not _ABSOLUTE.match(item["path"]) and location.is_file() and sha256_bytes(location.read_bytes()) != item["sha256"]:
                report.add("error", "T-RECORD", unit, f"{record_path.name}: context file {item['path']} hashes to "
                           f"{sha256_bytes(location.read_bytes())}; the record names {item['sha256']}")
    if context["memory"] != "disabled":
        report.add("hold", "T-CONTEXT", unit, f"{record_path.name}: memory was {context['memory']!r}, not disabled")
    if context["hooks"]:
        report.add("hold", "T-CONTEXT", unit, f"{record_path.name}: the harness ran {len(context['hooks'])} hook(s)")
    if allowed is not None:
        servers = {tool_server(item.split("(", 1)[0].strip()) for item in allowed} - {None}
        extra = sorted({item["name"] for item in context["mcp_servers"]} - servers)
        if extra:
            report.add("hold", "T-CONTEXT", unit, f"{record_path.name}: MCP server(s) {', '.join(extra)} were loaded, which "
                       "the skill does not allow")
    called = sorted({tool_server(str(call["tool"]).split("(", 1)[0].strip()) for call in calls} - {None})
    unlisted = [name for name in called if name not in {item["name"] for item in context["mcp_servers"]}]
    if unlisted:
        report.add("hold", "T-CONTEXT", unit, f"{record_path.name}: tool calls use MCP server(s) {', '.join(unlisted)}, "
                   "which agent.context.mcp_servers does not list; the servers the session ran with are not stated")
    flags = list(context["harness_flags"])
    if not flags:
        report.add("hold", "T-CONTEXT", unit, f"{record_path.name}: agent.context.harness_flags is empty; how the session "
                   "was made clean is not stated")
    elif record["agent"]["harness"] == "claude-code":
        missing = [flag for flag in CLAUDE_CODE_CLEAN_FLAGS if flag not in flags]
        if missing:
            report.add("hold", "T-CONTEXT", unit, f"{record_path.name}: the claude-code session's flags lack "
                       f"{', '.join(missing)}, which a clean configuration sets")
    return record


def compare_transcript(report: "Report", unit: str, record_path: Path, record: dict[str, Any], data: bytes,
                       allowed: list[str] | None) -> None:
    """The record against the released transcript: every tool call in order with its server-qualified name, its
    arguments and its stored result; every call the transcript shows on the skill's allowed list; the model and the
    session the transcript names."""
    shown = transcript_events(data)
    calls = record["tool_calls"]
    names = [item["name"] for item in shown["calls"]]
    listed = [str(call["tool"]) for call in calls]
    if allowed is not None:
        for name in sorted(set(names)):
            if not tool_allowed(name, allowed):
                report.add("error", "T-TOOLS", unit, f"{record_path.name}: the released transcript shows a call to {name}, "
                           f"which the skill does not allow")
    if names != listed:
        report.add("error", "T-TOOLS", unit, f"{record_path.name}: tool_calls ({len(listed)}: "
                   f"{', '.join(bare_tool(n) for n in listed[:8]) or 'none'}) differ from the {len(names)} tool call(s) the "
                   f"released transcript shows ({', '.join(bare_tool(n) for n in names[:8]) or 'none'})")
    else:
        for call, seen in zip(calls, shown["calls"]):
            if canonical_json(call["arguments"]) != canonical_json(seen["input"]):
                report.add("error", "T-TOOLS", unit, f"{record_path.name}: the arguments of tool call {call['seq']} are not "
                           "the ones the released transcript shows")
            result = seen["result"]
            if result is None:
                if call["result_path"] is not None:
                    report.add("error", "T-TOOLS", unit, f"{record_path.name}: tool call {call['seq']} has a stored result, "
                               "but the released transcript shows none")
                continue
            stored = None
            if call["result_path"] and not _ABSOLUTE.match(str(call["result_path"])):
                try:
                    stored = json.loads((record_path.parent / call["result_path"]).read_bytes().decode("utf-8-sig"))
                except (OSError, UnicodeDecodeError, ValueError):
                    stored = None
            if not isinstance(stored, dict) or "content" not in stored:
                report.add("error", "T-TOOLS", unit, f"{record_path.name}: the stored result of tool call {call['seq']} is "
                           "not readable as {tool_use_id, tool, is_error, content}")
            elif (canonical_json(stored.get("content")) != canonical_json(result.get("content"))
                  or bool(stored.get("is_error")) != bool(result.get("is_error"))
                  or stored.get("tool_use_id") != seen["id"]):
                report.add("error", "T-TOOLS", unit, f"{record_path.name}: the stored result of tool call {call['seq']} is "
                           "not the result the released transcript shows")
            elif bool(stored.get("is_error")) != (call["status"] == "error"):
                report.add("error", "T-TOOLS", unit, f"{record_path.name}: tool call {call['seq']} is {call['status']}, its "
                           "result says otherwise")
    agent = record["agent"]
    if agent["model_id"] == "unknown" or not shown["models"]:
        report.add("hold", "T-RECORD", unit, f"{record_path.name}: the model is not known (the record names "
                   f"{agent['model_id']!r}, the transcript {', '.join(shown['models']) or 'none'})")
    elif agent["model_id"] not in shown["models"]:
        report.add("error", "T-RECORD", unit, f"{record_path.name}: model_id {agent['model_id']!r} is not a model the "
                   f"released transcript names ({', '.join(shown['models'])})")
    init_session = (shown["init"] or {}).get("session_id")
    if init_session and str(init_session) != agent["session_id"]:
        report.add("error", "T-RECORD", unit, f"{record_path.name}: session_id {agent['session_id']!r} is not the session "
                   "the released transcript names")


# ------------------------------------------------------------------------------ the review log (section 2.4)


def read_review_log(triage_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """(the entries of the accession's review-log.jsonl, why its hash chain does not hold). No log is no entry."""
    path = Path(triage_dir) / REVIEW_LOG
    if not path.is_file():
        return [], []
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    previous = None
    for index, line in enumerate([item for item in path.read_bytes().split(b"\n") if item.strip()], start=1):
        try:
            entry = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            problems.append(f"line {index} does not parse")
            entry = None
        if isinstance(entry, dict):
            if entry.get("seq") != index:
                problems.append(f"line {index} has seq {entry.get('seq')}")
            if entry.get("prev_sha256") != previous:
                problems.append(f"line {index} does not chain to line {index - 1}")
            entries.append(entry)
        elif entry is not None:
            problems.append(f"line {index} is not an object")
        previous = sha256_bytes(line)
    return entries, problems


def _revision_entries(log: list[dict[str, Any]], decision_id: str) -> list[dict[str, Any]]:
    return [e for e in log if e.get("kind") == "revision" and e.get("new_decision_id") == decision_id]


def lineage(decision_id: str, supersedes: dict[str, str | None], log: list[dict[str, Any]]) -> set[str]:
    """This decision and every one it supersedes, by the records' supersedes and the review log's revision entries."""
    found = {decision_id}
    queue = [decision_id]
    while queue:
        current = queue.pop()
        earlier = {supersedes.get(current)} | {e.get("supersedes") for e in _revision_entries(log, current)}
        for item in earlier:
            if isinstance(item, str) and item and item not in found:
                found.add(item)
                queue.append(item)
    return found


def answered_codes(decision_id: str, payload: dict[str, Any], supersedes: dict[str, str | None],
                   log: list[dict[str, Any]]) -> set[str]:
    """The question codes a named person answered for this payload or one it supersedes (review-log person_answer
    entries); a v1 payload's own person_answers count as well."""
    ids = lineage(decision_id, supersedes, log)
    codes = {str(e.get("question_code")) for e in log
             if e.get("kind") == "person_answer" and e.get("decision_id") in ids and str(e.get("by") or "").strip()
             and str(e.get("question_code") or "").strip()}
    if payload.get("schema") != SCHEMA_V2_ID:
        codes |= {str(a.get("code")) for a in payload.get("person_answers", []) if str(a.get("by") or "").strip()}
    return codes


def _blocking(payload: dict[str, Any] | None) -> set[str]:
    return {f.get("code") for f in (payload or {}).get("flags", []) if isinstance(f, dict)} & BLOCKING_FLAGS


def check_revision(report: "Report", unit: str, path: Path, decision_id: str, payload: dict[str, Any],
                   payloads: dict[str, tuple[Path, dict[str, Any] | None]], supersedes: dict[str, str | None],
                   log: list[dict[str, Any]], unit_files: list[tuple[int, Path]]) -> None:
    """A v2 revision may drop a blocking flag of the payload it supersedes only on a person's answer: the cause of its
    revision entry must be a person_answer with that flag's question_code, for this decision or one it supersedes."""
    earlier = {supersedes.get(decision_id)} | {e.get("supersedes") for e in _revision_entries(log, decision_id)}
    earlier = {item for item in earlier if isinstance(item, str) and item}
    if not earlier:
        # No record or log entry names what it revises: the revision before it by name (<unit>.r<N-1>.json, ...).
        match = _REVISION.match(path.stem)
        number = int(match.group("n")) if match else 0
        lower = [(n, p) for n, p in unit_files if n < number]
        if not lower:
            return
        before = max(lower)[1]
        earlier = {sha for sha, (p, _) in payloads.items() if p == before}
    ids = lineage(decision_id, supersedes, log)
    by_seq = {e.get("seq"): e for e in log}
    cited = set()
    for entry in _revision_entries(log, decision_id):
        cause = entry.get("cause") if isinstance(entry.get("cause"), dict) else {}
        answer = by_seq.get(cause.get("seq"))
        if (cause.get("kind") == "person_answer" and answer is not None and answer.get("kind") == "person_answer"
                and answer.get("decision_id") in ids and str(answer.get("by") or "").strip()):
            cited.add(str(answer.get("question_code")))
    for sha in sorted(earlier):
        if sha not in payloads:
            report.add("hold", "T-REVISION", unit, f"{path.name} supersedes {sha}, which is not in this directory; the flags "
                       "it dropped cannot be checked")
            continue
        dropped = _blocking(payloads[sha][1]) - _blocking(payload)
        for code in sorted(dropped - cited):
            report.add("error", "T-REVISION", unit, f"{path.name} drops the blocking flag {code} that "
                       f"{payloads[sha][0].name} carried, but no revision entry of the review log cites a person's answer "
                       f"to {code} as its cause; a flag is cleared by a person, not by a revision")


def _payload_files(triage_dir: Path) -> dict[str, list[tuple[int, Path]]]:
    """Every payload of the directory, current or superseded, by unit: [(revision number, path)], 0 for <unit>.json."""
    out: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    for path in sorted(triage_dir.glob("*.json")):
        if path.name == "index.json" or path.name.endswith(RECORD_SUFFIX):
            continue
        match = _REVISION.match(path.stem)
        unit, number = (match.group("unit"), int(match.group("n"))) if match else (path.stem, 0)
        out[unit].append((number, path))
    return {unit: sorted(items) for unit, items in out.items()}


def validate(
    triage_dir: Path,
    catalog: dict[str, CatalogUnit] | None = None,
    listings: Iterable[ListedMember] = (),
    *,
    code_root: Path | None = None,
    bundle: ReleaseBundle | None = None,
    listing_files: Iterable[Path] | None = None,
) -> Report:
    """The accession's triage files as a set, with their decision records and review log.

    `code_root` is the released code (SKILL.md, the prompt templates) read in place of git at each record's repo_commit;
    `bundle` the release bundle the directory sits in (found on its own when <triage dir>/../../release-index.json
    exists), through whose index projected inputs are checked. `listing_files` are the files `listings` were read from
    (the CLI's --archive-listing): each record's archive_listing inputs must be among them, and each of them bound by a
    record (T-RECORD, T-DUP); without them that tie is not checked."""
    report = Report()
    triage_dir = Path(triage_dir)
    if bundle is None:
        bundle = ReleaseBundle.find(triage_dir)
    given = None if listing_files is None else {sha256_bytes(Path(p).read_bytes()): Path(p) for p in listing_files}
    provenance = Provenance(CodeSource(code_root), bundle, None if given is None else set(given))
    triages: dict[str, dict[str, Any]] = {}
    files = current_triage_files(triage_dir)
    if not files:
        report.add("error", "T-SET", None, f"no triage files in {triage_dir}")
        return report

    # Every payload of the directory, superseded ones included, by sha256, with what its record says it supersedes.
    all_files = _payload_files(triage_dir)
    payloads: dict[str, tuple[Path, dict[str, Any] | None]] = {}
    supersedes: dict[str, str | None] = {}
    for items in all_files.values():
        for _number, path in items:
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            sha = sha256_bytes(raw)
            try:
                value = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError):
                value = None
            payloads[sha] = (path, value if isinstance(value, dict) else None)
            try:
                named = json.loads(path.with_name(path.name[: -len(".json")] + RECORD_SUFFIX).read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, ValueError):
                named = None
            if isinstance(named, dict) and isinstance(named.get("supersedes"), str):
                supersedes[sha] = named["supersedes"]
    log, log_problems = read_review_log(triage_dir)
    for problem in log_problems:
        report.add("error", "T-LOG", None, f"{REVIEW_LOG}: {problem}; the answers and revisions it records cannot be trusted")
    answered: dict[str, set[str]] = {}

    for path in files:
        try:
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            report.add("error", "T-SCHEMA", path.stem, f"{path.name} is not readable JSON: {exc}")
            continue
        unit = data.get("unit_id") if isinstance(data, dict) else None
        unit = unit if isinstance(unit, str) and unit else triage_file_unit(path)
        if unit in triages:
            report.add("error", "T-SET", unit, f"unit {unit} is triaged twice")
            continue
        report.units.append(unit)
        decision_id = sha256_bytes(raw)
        schema_id = data.get("schema") if isinstance(data, dict) else None
        report.payloads[unit] = {"payload": path.name, "schema": schema_id, "decision_id": decision_id}
        if schema_id not in SCHEMA_PATHS:
            report.add("error", "T-SCHEMA", unit, f"{path.name}: schema {schema_id!r} is not one of {sorted(SCHEMA_PATHS)}")
            continue
        schema = load_schema(schema_id)
        problems = schema_errors(data, schema, schema)
        if problems:
            for problem in problems[:20]:
                report.add("error", "T-SCHEMA", unit, f"{path.name}: {problem}")
            if len(problems) > 20:
                report.add("error", "T-SCHEMA", unit, f"{path.name}: {len(problems) - 20} more schema problem(s)")
            continue
        if triage_file_unit(path) != unit:
            report.add("error", "T-SET", unit, f"{path.name} holds unit {unit}; name the file {unit}.json (or {unit}.r<N>.json)")
        triages[unit] = data
        report.unit_status[unit] = data["status"]
        for citation in class_citations(data) + record_class_citations(path):
            report.add("error", "T-CLASS", unit, f"{path.name}: {citation}; the triage binds the Catalog's rows, files and "
                       "listing and never the Class, which runs after it and binds through its own ratification "
                       "(WS0 decision F)")
        if schema_id == SCHEMA_ID:
            report.add("note", "T-RATIONALE", unit, f"rationale_not_recorded: {path.name} is {SCHEMA_ID}, which carries no "
                       "confidence or rationale")
        check_record(report, unit, triage_dir, path, raw, data, provenance)
        answered[unit] = answered_codes(decision_id, data, supersedes, log)
        if schema_id == SCHEMA_V2_ID:
            check_revision(report, unit, path, decision_id, data, payloads, supersedes, log, all_files.get(unit, []))

    accessions = {t["accession"] for t in triages.values()}
    if len(accessions) > 1:
        report.add("error", "T-SET", None, f"one directory must hold one accession; found {sorted(accessions)}")
    report.accession = sorted(accessions)[0] if accessions else None

    listed = list(listings)
    by_key: dict[str, ListedMember] = {}
    for m in listed:
        by_key.setdefault(member_key(m.path), m)
    resolver = Resolver(m.path for m in listed) if listed else Resolver(_triage_member_paths(triages))

    if catalog is not None:
        # A campaign's handoffs directory spans many accessions; only this accession's units count here.
        catalog = {u: cu for u, cu in catalog.items() if u in triages or (cu.accession is not None and cu.accession == report.accession)}

    for unit, triage in triages.items():
        _check_unit(report, unit, triage, triages, catalog, by_key if listed else None, resolver, answered[unit])

    _check_cross_unit(report, triages, listed, resolver, answered)
    if given is not None and provenance.bound_listings:
        for digest, path in sorted(given.items(), key=lambda item: item[1].name):
            if digest not in provenance.bound_listings:
                report.add("hold", "T-DUP", None, f"the listing {path.name} was given, but no decision record binds it; "
                           "the coverage checks ran on a listing no decision rested on")
    if catalog is not None:
        missing = sorted(set(catalog) - set(triages))
        if missing and report.accession is not None:
            for unit in missing:
                report.add("hold", "T-SET", unit, f"Catalog unit {unit} has no triage file")
    # Checks that could not run hold every unit: what was not checked is not passed.
    for unit in triages:
        if catalog is None:
            report.add("hold", "T-COVER", unit, "no Catalog was given (--catalog): the rows were not checked against the handoff")
        if not listed:
            report.add("hold", "T-DUP", unit, "no archive listing was given (--archive-listing): byte-identical content and members no unit accounts for cannot be ruled out; a person decides whether to go on without one")
    return report


def _check_unit(
    report: Report,
    unit: str,
    t: dict[str, Any],
    triages: dict[str, dict[str, Any]],
    catalog: dict[str, CatalogUnit] | None,
    listed: dict[str, ListedMember] | None,
    resolver: Resolver,
    answered: set[str] | None = None,
) -> None:
    answered = set(answered or ())
    v2 = t.get("schema") == SCHEMA_V2_ID
    status = t["status"]
    flags = _flag_codes(t)
    ut = t["unit_technical"]
    unit_ion, unit_chrom, unit_acq = ut["ion_mode"], ut["chromatography"], ut["acquisition_mode"]
    kept = _kept(unit, t, resolver)

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
        elif cu.accession is not None and cu.accession != t["accession"]:
            report.add("error", "T-COVER", unit, f"the Catalog puts unit {unit} in {cu.accession}, the triage in {t['accession']}")
        elif cu.problem:
            report.add("error", "T-HANDOFF", unit, cu.problem)
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

    # T-EXCL: an exclusion that points at another unit must point at a real one that keeps the file, whether
    # the exclusion is of a Catalog row or of an archive member.
    for item in t["excluded_rows"] + t.get("excluded_members", []):
        target = item.get("belongs_to_unit")
        is_row = "row_index" in item
        what = f"row {item['row_index']} ({item['sample_id']}, {item['raw_file']})" if is_row else f"member {item['member_path']}"
        if item["reason"] == "belongs_to_other_unit":
            if not target:
                report.add("error", "T-EXCL", unit, f"{what} is excluded as another unit's without naming the unit")
            elif target == unit:
                report.add("error", "T-EXCL", unit, f"{what} is excluded as belonging to this same unit")
            elif target not in triages:
                report.add("hold", "T-EXCL", unit, f"{what} is said to belong to {target}, which has no triage file here")
            else:
                # Row indexes belong to each unit's own handoff, so the target is checked by file, not by index.
                path = item.get("member_path") or item["raw_file"]
                theirs = _kept(target, triages[target], resolver)
                same_row = (lambda k: k.is_row and k.item["sample_id"] == item["sample_id"] and k.item["raw_file"] == item["raw_file"]) if is_row else (lambda k: False)
                if not any(same_row(k) or same_file(k, path, resolver) for k in theirs):
                    report.add("hold", "T-EXCL", unit, f"{what} is said to belong to {target}, but {target} does not keep that file")

    # T-EXCL: a reason that contradicts the unit's own state drops a file with nobody asked.
    questions = {q["code"] for q in t["needs_person"]}
    for item in t["excluded_rows"] + t.get("excluded_members", []):
        reason = item["reason"]
        what = f"row {item['row_index']} ({item['sample_id']}, {item['raw_file']})" if "row_index" in item else f"member {item['member_path']}"
        if reason == "needs_person" and (status == "ready" or not t["needs_person"]):
            report.add("error", "T-EXCL", unit, f"{what} is excluded as needs_person, but the unit is {status} with {len(t['needs_person'])} question(s) for a person; ask the question or give the real reason")
        elif reason == "no_signal_abstained" and (status == "ready" or (status != "abstained" and "no_signal" not in questions)):
            report.add("error", "T-EXCL", unit, f"{what} is excluded as no_signal_abstained, but the unit is {status} with no no_signal question")
        elif reason == "polarity_switching" and "polarity_switching" not in flags:
            report.add("error", "T-EXCL", unit, f"{what} is excluded as polarity_switching without a polarity_switching flag")
        elif reason == "out_of_scope_ms1_only" and "ms1_only" not in flags:
            report.add("error", "T-EXCL", unit, f"{what} is excluded as out_of_scope_ms1_only without an ms1_only flag")
        elif reason == "requires_conversion":
            runnable = _runnable_form(item, resolver)
            if runnable:
                report.add("error", "T-EXCL", unit, f"{what} is excluded as requires_conversion, but it is {runnable}, which MS-DIAL reads (requires_conversion is for mzXML and mzData)")

    # A file is kept or excluded by a unit, not both; and one acquisition is not kept twice in two formats.
    for e in t.get("excluded_members", []):
        both = [k for k in kept if same_file(k, e["member_path"], resolver)]
        if both:
            report.add("error", "T-EXCL", unit, f"{e['member_path']} is in excluded_members ({e['reason']}) and also kept ({both[0].path})")
    formats: dict[str, dict[str, str]] = defaultdict(dict)
    for k in kept:
        if k.resolved:
            formats[k.stem].setdefault(_extension(k.key), k.member or k.path)
    for paths in formats.values():
        if len(paths) > 1:
            shown_paths = sorted(paths.values())
            report.add("error", "T-UNIQ", unit, f"one acquisition is kept twice, in {len(paths)} formats: {', '.join(shown_paths)}; keep one and exclude the others as alternative_format_copy")

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
            if code not in answered:
                where = (f"a person_answer in {REVIEW_LOG} with question_code {code} for this payload or one it supersedes"
                         if v2 else f"person_answers code {code}")
                report.add("error", "T-STATUS", unit, f"flag {code} keeps the unit from being ready until a person answers it ({where})")
        if v2 and LOW_CONFIDENCE_CODE not in answered:
            for item in [*t["rows"], *t.get("added_members", [])]:
                if item.get("basis") == "inferred" and item.get("confidence") == "low":
                    label = item.get("member_path") or item.get("raw_file")
                    report.add("hold", "T-CONFIDENCE", unit, f"{label} is kept on an inferred reading at low confidence; a "
                               "ready unit does not rest on it until a person has looked (a person_answer in "
                               f"{REVIEW_LOG} with question_code {LOW_CONFIDENCE_CODE})")
        if unit_ion == "both" and not t.get("children"):
            report.add("error", "T-POL", unit, "a ready unit with both polarities needs children, one per polarity and acquisition")
    else:
        questions = "; ".join(q["code"] for q in t["needs_person"])
        report.add("hold", "T-QUEUE", unit, f"status {status}" + (f": {questions}" if questions else ""))

    # Children partition the kept files, one polarity each.
    child_of: dict[str, dict[str, Any]] = {}
    child_paths: dict[str, str] = {}
    for child in t.get("children", []):
        if unit_ion in ("positive", "negative") and child["ion_mode"] != unit_ion:
            report.add("error", "T-POL", unit, f"child {child['child_key']} is {child['ion_mode']}, the unit is {unit_ion}; only a unit of both polarities has children of two")
        for p in child["member_paths"]:
            k = resolver.key(p)[0]
            if k in child_of:
                report.add("error", "T-CHILD", unit, f"{p} is in children {child_of[k]['child_key']} and {child['child_key']}")
            child_of[k] = child
            child_paths[k] = p

    def child_for(k: Kept) -> dict[str, Any] | None:
        if k.key in child_of:
            return child_of[k.key]
        hits = [child_of[c] for c, p in child_paths.items() if same_file(k, p, resolver)]
        return hits[0] if len(hits) == 1 else None

    if t.get("children"):
        for k in kept:
            if child_for(k) is None:
                report.add("error", "T-CHILD", unit, f"{k.path} is kept but in no child")
        for c, p in child_paths.items():
            if not any(k.key == c or same_file(k, p, resolver) for k in kept):
                report.add("error", "T-CHILD", unit, f"child member {p} is not kept by the unit")

    # Per file: polarity, chromatography, acquisition, format, evidence, tokens, headers.
    acqs: set[str] = set()
    for k in kept:
        item = k.item
        label = k.path
        ion = item["ion_mode"]
        child = child_for(k)
        # A row named only by raw_file is read through the member it resolves to: its folder and extension count.
        names = [label] + ([k.member] if k.member and k.member != label else [])
        shown = label if len(names) == 1 else f"{label} ({k.member})"
        if status != "out_of_scope" and any(x.casefold().endswith(CONVERSION_EXTENSIONS) for x in (k.path, k.key)):
            report.add("error", "T-FORMAT", unit, f"{shown} is mzXML or mzData, which MS-DIAL does not read; exclude it as requires_conversion (a reviewed conversion to mzML is a separate step)")
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
            for name in names:
                tok_ion = token_polarity(name)
                if tok_ion and ion in ("positive", "negative") and tok_ion != ion:
                    report.add("error", "T-TOKEN", unit, f"{shown}: {name} says {tok_ion}, the triage says {ion} (flag name_token_disputed and ask a person if that is meant)")
                    break
            for name in names:
                tok_chrom = token_chromatography(name)
                if tok_chrom and chrom in ("HILIC", "RP") and tok_chrom != chrom:
                    report.add("error", "T-TOKEN", unit, f"{shown}: {name} says {tok_chrom}, the triage says {chrom} (flag name_token_disputed and ask a person if that is meant)")
                    break

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
            if disagrees and not ("folder_header_disagreement" in flags and status != "ready" or "folder_header_disagreement" in answered):
                report.add("error", "T-HEADER", unit, f"{label}: the raw header says {e['field']}={value}, the triage says otherwise; a folder/header disagreement goes to a person")

        if listed is not None and item.get("member_path") and member_key(item["member_path"]) not in listed:
            report.add("error", "T-MEMBER", unit, f"{item['member_path']} is not in the archive listing")
        if listed is not None and not k.resolved:
            report.add("error" if status == "ready" else "hold", "T-MEMBER", unit,
                       f"row {item['row_index']} ({label}) is kept with no member_path, and its name resolves to no single listed member; resolve it or exclude it")

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
        mine = {resolver.key(p)[0] for p in share["member_paths"]}
        theirs = set()
        for s in other["shared_with"]:
            if s["unit_id"] == unit:
                theirs |= {resolver.key(p)[0] for p in s["member_paths"]}
        if mine - theirs:
            report.add("error", "T-SHARED", unit, f"{len(mine - theirs)} path(s) shared with {share['unit_id']} are not declared back by it")

    for answer in t.get("person_answers", []):
        if not answer["by"].strip():
            report.add("error", "T-PERSON", unit, "a person answer must name the person")


def _check_cross_unit(report: Report, triages: dict[str, dict[str, Any]], listed: list[ListedMember], resolver: Resolver,
                      answered: dict[str, set[str]] | None = None) -> None:
    answered = answered or {}
    owners: dict[str, list[Kept]] = defaultdict(list)
    for unit, t in triages.items():
        for k in _kept(unit, t, resolver):
            owners[k.key].append(k)

    # One group per file. A row the index could not resolve ("raw:<stem>") may be any kept member of that
    # stem, so it joins the group of each of them; the members themselves stay distinct files (two units
    # keeping batch1/x.mzML and batch2/x.mzML do not share a file because a third names only "x").
    members_of_stem: dict[str, list[str]] = defaultdict(list)
    for key in owners:
        if not key.startswith("raw:"):
            members_of_stem[_key_stem(key)].append(key)
    files: dict[str, list[Kept]] = defaultdict(list)
    for key, holders in owners.items():
        targets = members_of_stem.get(_key_stem(key), []) if key.startswith("raw:") else [key]
        for target in targets or [key]:
            files[target] += holders

    def canonical(key: str) -> str:
        """The one kept member an unresolved name can mean, if there is exactly one."""
        targets = members_of_stem.get(_key_stem(key), []) if key.startswith("raw:") else []
        return targets[0] if len(targets) == 1 else key

    def declared_share(a: str, b: str, holders: list[Kept]) -> bool:
        def has(x: str, y: str) -> bool:
            mine = [h for h in holders if h.unit == x]
            return any(s["unit_id"] == y and any(same_file(h, p, resolver) for h in mine for p in s["member_paths"]) for s in triages[x]["shared_with"])

        return has(a, b) and has(b, a)

    # T-UNIQ: no file kept by two units unless both declare it. One finding per pair of units.
    undeclared: dict[tuple[str, str], list[str]] = defaultdict(list)
    for holders in files.values():
        units = sorted({h.unit for h in holders})
        if len(holders) > len(units):
            report.add("error", "T-UNIQ", units[0], f"{holders[0].path} is kept twice by the same unit")
        for i, a in enumerate(units):
            for b in units[i + 1 :]:
                if not declared_share(a, b, holders):
                    undeclared[(a, b)].append(holders[0].path)
    for (a, b), paths in sorted(undeclared.items()):
        report.add("error", "T-UNIQ", a, f"{len(paths)} file(s) kept by both {a} and {b} without shared_with on both (first: {sorted(paths)[0]})")

    # Identical kept file sets (the stage3b failure) are named once, on top of the per-file errors.
    sets: dict[frozenset[str], list[str]] = defaultdict(list)
    for unit, t in triages.items():
        keys = frozenset(canonical(k.key) for k in _kept(unit, t, resolver))
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
            settled = "shared_content" in _flag_codes(t) and (t["status"] != "ready" or "shared_content" in answered.get(unit, set()))
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

    # T-EXCL against the listing: an exclusion reason the listing contradicts drops a file silently.
    for unit, t in triages.items():
        for item in t["excluded_rows"] + t.get("excluded_members", []):
            name = item.get("member_path") or item.get("raw_file") or ""
            what = f"row {item['row_index']} ({name})" if "row_index" in item else name
            if item["reason"] == "not_in_archive":
                there = {member_key(item["member_path"])} & resolver.members if item.get("member_path") else resolver.hits(name)
                if there:
                    report.add("error", "T-EXCL", unit, f"{what} is excluded as not_in_archive, but the listing has {sorted(there)[0]}")
            elif item["reason"] == "duplicate_content":
                key = member_key(item["member_path"]) if item.get("member_path") else resolver.resolve(name)
                if key is None or key not in twins:
                    report.add("error", "T-EXCL", unit, f"{what} is excluded as duplicate_content, but the listing shows no byte-identical twin (CRC32 and size) for it")

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
    """Per unit: correct, wrong_included, wrongly_left_out, against ground_truth.json (eval fixtures).

    A ground-truth unit is named by its public analysis id. The eval fixtures' payloads are named the same way; a
    campaign's are named by the Catalog unit id and carry the analysis id as source_subrecord_id, so the current
    payload whose source_subrecord_id is the analysis id is the one scored when no <analysis id>.json exists."""
    by_subrecord: dict[str, Path] = {}
    for current in current_triage_files(triage_dir):
        try:
            subrecord = json.loads(current.read_text(encoding="utf-8")).get("source_subrecord_id")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(subrecord, str) and subrecord:
            by_subrecord.setdefault(subrecord, current)
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
        if not path.exists() and uid in by_subrecord:
            path = by_subrecord[uid]
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


# ------------------------------------------------------------------------------ --emit-selection (section 2.4)


def _curated():
    """scripts/campaign/curated.py, the runner's reader and the review log's only writer."""
    scripts = str(Path(__file__).resolve().parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from campaign import curated  # noqa: PLC0415

    return curated


def emit_selection(report: Report, triage_dir: Path, *, at: str | None = None, commit: str | None = None,
                   append: bool = True) -> dict[str, Any]:
    """Each judged decision's selection and its validator_verdict entry in the review log.

    The selection is msdial-selection-canonical.v1 over the archive listing the unit's decision record binds
    (curated.record_inputs, as the plan and Interactive read it; none without a record), per unit and per child. The
    verdict entry (tool, code_commit, verdict, findings_sha256, findings) is appended once per decision through
    curated.append_review_log, the unit's findings with the accession's own; an entry that would repeat the decision's
    last verdict entry (same code_commit, verdict and findings) is not appended again. A review log whose chain is
    broken is not appended to."""
    curated = _curated()
    triage_dir = Path(triage_dir)
    at = at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    log, problems = read_review_log(triage_dir)
    last: dict[str, dict[str, Any]] = {}
    for entry in log:
        if entry.get("kind") == "validator_verdict":
            last[str(entry.get("decision_id"))] = entry
    selection: dict[str, Any] = {}
    appended, repeated = [], []
    for unit in report.units:
        info = report.payloads.get(unit)
        if not info:
            continue
        decision_id = info["decision_id"]
        items = [{"check": f.check, "level": f.level, "message": f.message} for f in report.findings if f.unit in (unit, None)]
        levels = {item["level"] for item in items}
        verdict = "error" if "error" in levels else "hold" if "hold" in levels else "pass"
        findings_sha256 = sha256_bytes(canonical_json(items))
        block: dict[str, Any] = {"decision_id": decision_id, "verdict": verdict,
                                 "selection_canonical_form": curated.SELECTION_CANONICAL_FORM}
        path = triage_dir / info["payload"]
        if unit in report.unit_status:
            triage = json.loads(path.read_bytes().decode("utf-8"))
            record = curated.record_path(path)
            bound = curated.record_inputs(record, decision_id) if record.is_file() else None
            if bound is not None and bound["problem"]:
                block.update(selection_sha256=None, problem=bound["problem"])
            else:
                listing = bound["listing"] if bound is not None else {}
                block.update(
                    record=record.name if bound is not None else None,
                    selection_listings=(bound or {}).get("archive_listings") or [],
                    selection_count=len(curated.selection_canonical(triage, listing)),
                    selection_sha256=curated.selection_sha256(triage, listing),
                    children={str(child["child_key"]): curated.selection_sha256(triage, listing, str(child["child_key"]))
                              for child in triage.get("children") or []},
                )
        else:
            block.update(selection_sha256=None, problem="the payload is not a valid triage")
        selection[unit] = block
        previous = last.get(decision_id)
        if previous is not None and (previous.get("code_commit"), previous.get("verdict"), previous.get("findings_sha256")) \
                == (commit, verdict, findings_sha256):
            repeated.append(unit)
            continue
        if append and not problems:
            curated.append_review_log(triage_dir, "validator_verdict", decision_id, at, tool=TOOL_NAME, code_commit=commit,
                                      verdict=verdict, findings_sha256=findings_sha256, findings=items)
            appended.append(unit)
    review_log: dict[str, Any] = {"path": REVIEW_LOG, "appended": appended, "already_recorded": repeated,
                                  "head_sha256": curated.review_log_head(triage_dir)}
    if problems:
        review_log["refused"] = f"{REVIEW_LOG}'s chain does not hold ({problems[0]}); nothing was appended"
    elif not append:
        review_log["refused"] = "a release bundle is never written to; nothing was appended"
    report.extra["selection"] = selection
    report.extra["review_log"] = review_log
    return report.extra


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("triage_dir", type=Path)
    parser.add_argument("--catalog", type=Path, help="handoffs directory or catalog.json")
    parser.add_argument("--archive-listing", type=Path, action="append", default=[], help="archive member TSV (repeatable)")
    parser.add_argument("--ground-truth", type=Path, help="ground_truth.json to score against")
    parser.add_argument("--code-root", type=Path, help="the released code (skill, prompt templates) at the records' commit, "
                        "read in place of git")
    parser.add_argument("--release-bundle", type=Path, help="the release bundle the triage directory sits in (found on "
                        "its own when <triage dir>/../../release-index.json exists)")
    parser.add_argument("--emit-selection", action="store_true", help="add each unit's selection to the report and append "
                        "a validator_verdict per decision to the accession's review-log.jsonl")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    if not args.triage_dir.is_dir():
        print(f"not a directory: {args.triage_dir}", file=sys.stderr)
        return 2
    bundle = None
    if args.release_bundle is not None:
        if not (args.release_bundle / RELEASE_INDEX).is_file():
            print(f"not a release bundle (no {RELEASE_INDEX}): {args.release_bundle}", file=sys.stderr)
            return 2
        bundle = ReleaseBundle(args.release_bundle)
    else:
        bundle = ReleaseBundle.find(args.triage_dir)
    catalog = read_catalog(args.catalog, bundle) if args.catalog else None
    listed: list[ListedMember] = []
    for path in args.archive_listing:
        listed += read_listing(path)
    report = validate(args.triage_dir, catalog, listed, code_root=args.code_root, bundle=bundle,
                      listing_files=args.archive_listing)
    if args.ground_truth:
        report.score = score(args.triage_dir, json.loads(args.ground_truth.read_text(encoding="utf-8")))
    if args.emit_selection:
        emit_selection(report, args.triage_dir, commit=code_commit(), append=bundle is None)
    print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False) if args.json else _render(report))
    if args.emit_selection and bundle is not None:
        print("--emit-selection does not write into a release bundle; the review log was left as it is", file=sys.stderr)
        return 2
    return {"pass": 0, "hold": 1, "error": 2}[report.verdict]


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
