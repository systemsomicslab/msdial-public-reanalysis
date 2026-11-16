"""Curated selection: the frozen triage files a campaign plans from, and the records it keeps of them.

WHY THIS EXISTS. On 2026-11-15 the stage3b campaign was stopped: every unit of a multi-unit accession had run on
the whole study's files. The author approved an agent-based triage before download (the msdial-unit-triage skill,
schemas/msdial-unit-triage.v1.json, scripts/validate-triage.py). This module is the runner's side of it: it reads
the triage files of a campaign (<campaign dir>\\triage\\<accession>\\<unit_id>.json), has the validator judge each
accession's set, and turns a passing triage into the fields a plan manifest carries, so the digest a person approves
covers the selection itself (docs/llm-assisted-curation.md, section 2.5).

WHAT IT NEVER DOES. It runs no model and imports no model client: everything here is deterministic over files and
their sha256, which is what lets scripts/replay-campaign.py rebuild a unit's MS-DIAL inputs from the frozen decision
files without any LLM. It writes nothing but the review log entries and the decision ledger export it is asked to
append; it never edits a triage file, a handoff or a Catalog.

LAYOUT OF ONE ACCESSION (the framework's section 2.1):

    <triage dir>\\<accession>\\
        <unit_id>.json            the payload (msdial-unit-triage.v1 or v2); a revision is <unit_id>.r<N>.json
        <unit_id>.record.json     the decision record (envelope), when one was written
        inputs\\                   what the agent read: the Catalog handoffs (<unit_id>\\handoff.json and
                                  samples.json) or a catalog.json, and the archive listing(s) (*.tsv)
        review-log.jsonl          append only, hash chained

THE SELECTION. A unit's selection is the canonical JSON (sorted keys, no spaces, UTF-8) of the list
[{unit_id, archive_name, member_path, crc32, size, sample_row_index, child_key}], one item per kept row and added
member, sorted by (unit_id, child_key, member_path); crc32 and size come from the archive listing (null where the
listing does not name the member). Its sha256 is selection_sha256. The analysis inputs are what Interactive declares
from it ({kind: "file", path: member_path, sample_id, archive}, sorted by path); their sha256 is
analysis_inputs_sha256. A third party recomputes both from the released files.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

TRIAGE_SCHEMAS = ("msdial-unit-triage.v1", "msdial-unit-triage.v2")
VALIDATOR = Path(__file__).resolve().parent.parent / "validate-triage.py"
SELECTION_SCHEMA = "msdial-curated-selection.v1"
REOPEN_SCHEMA = "msdial-campaign-reopen.v1"
REVIEW_LOG = "review-log.jsonl"
DECISION_LEDGER = "decision-ledger.jsonl"

# The reasons a plan from triage files leaves a unit out (plan.TRIAGE_EXCLUSION_REASONS reads them).
TRIAGE_MISSING = "triage_missing"
TRIAGE_NOT_PASS = "triage_not_pass"
TRIAGE_UNREADABLE = "triage_unreadable"
TRIAGE_MEMBER_UNRESOLVED = "triage_member_unresolved"
CLASS_CONTRAST_LOST = "class_contrast_lost_after_triage"
EXCLUSION_REASONS = (TRIAGE_MISSING, TRIAGE_UNREADABLE, TRIAGE_NOT_PASS, TRIAGE_MEMBER_UNRESOLVED, CLASS_CONTRAST_LOST)

# The kinds of campaign event that make up the decision ledger, and the standing of what each records
# (docs/llm-assisted-curation.md; the integration contract's B6). An event's detail carries its own standing.
SOURCE_FACT, MODEL_INFERENCE, DETERMINISTIC_CHECK, ACCEPTED_DECISION, PERSON_ANSWER = (
    "source_fact", "model_inference", "deterministic_check", "accepted_decision", "person_answer")
STANDINGS = (SOURCE_FACT, MODEL_INFERENCE, DETERMINISTIC_CHECK, ACCEPTED_DECISION, PERSON_ANSWER)
DECISION_KINDS = (
    "triage_recorded", "triage_validated", "manifest_approved", "unit_reopened", "triage_checked",
    "curated_selection_applied", "header_reconciliation", "cross_unit_verdict", "terminal_refusal", "person_answer",
)

_NON_SAMPLE_CLASS = re.compile(r"(?i)^(blank|blk|qc|pooled[ _-]?qc|pool|solvent|system[ _-]?blank|all)s?$")
_REASON_CODE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
_REVISION = re.compile(r"^(?P<unit>.+)\.r(?P<n>[0-9]+)\.json$")


class TriageError(ValueError):
    """A triage file or directory that cannot be read as the contract says."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def canonical(value: Any) -> bytes:
    """Sorted keys, no spaces, UTF-8: the serialisation every digest here is taken over."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").strip("/")


# ---- reading one accession --------------------------------------------------------------------------------------

def payload_path(accession_dir: Path, unit_id: str) -> Path | None:
    """The unit's current payload: <unit_id>.json, or its highest revision <unit_id>.r<N>.json. None when absent."""
    base = accession_dir / f"{unit_id}.json"
    revisions = []
    for path in accession_dir.glob(f"{unit_id}.r*.json"):
        match = _REVISION.match(path.name)
        if match and match.group("unit") == unit_id and not path.name.endswith(".record.json"):
            revisions.append((int(match.group("n")), path))
    if revisions:
        return max(revisions)[1]
    return base if base.is_file() else None


def record_path(payload: Path) -> Path:
    """The decision record (envelope) beside a payload: <name>.record.json."""
    return payload.with_name(payload.name[: -len(".json")] + ".record.json")


def review_log_head(accession_dir: Path) -> str | None:
    """sha256 of the review log's last line (its bytes without the newline), or None when there is no entry."""
    path = accession_dir / REVIEW_LOG
    if not path.is_file():
        return None
    lines = [line for line in path.read_bytes().split(b"\n") if line.strip()]
    return sha256_bytes(lines[-1]) if lines else None


def accession_sources(accession_dir: Path) -> tuple[Path | None, list[Path]]:
    """(the Catalog source for the validator's --catalog, the archive listings) the accession's inputs\\ hold.

    The Catalog source is inputs\\ itself where it holds <unit_id>\\handoff.json (the handoffs the agent read,
    byte for byte), else inputs\\catalog.json (the eval fixtures' shape); the listings are inputs\\*.tsv."""
    inputs = accession_dir / "inputs"
    if not inputs.is_dir():
        return None, []
    catalog: Path | None = None
    if any(inputs.glob("*/handoff.json")):
        catalog = inputs
    elif (inputs / "catalog.json").is_file():
        catalog = inputs / "catalog.json"
    return catalog, sorted(inputs.glob("*.tsv"))


@dataclass
class Listing:
    """One archive member as the listing gives it."""

    archive: str | None
    path: str
    size: int | None
    crc32: str | None


def read_listings(paths: Iterable[Path]) -> dict[str, Listing]:
    """Members by their case-folded path, from archive-member TSVs (archive, path, size, crc32)."""
    out: dict[str, Listing] = {}
    for path in paths:
        with Path(path).open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            fields = {name.lower(): name for name in (reader.fieldnames or [])}
            crc = fields.get("crc32") or fields.get("crc")
            for row in reader:
                if "type" in fields and row[fields["type"]] not in ("file", ""):
                    continue
                member = _norm(row[fields["path"]]) if "path" in fields else ""
                if not member:
                    continue
                size = row.get(fields["size"]) if "size" in fields else None
                out.setdefault(member.casefold(), Listing(
                    archive=(row.get(fields["archive"]) or None) if "archive" in fields else None, path=member,
                    size=int(size) if str(size or "").strip().isdigit() else None,
                    crc32=(str(row[crc]).upper() or None) if crc else None))
    return out


def catalog_rows(catalog: Path | None, unit_id: str) -> list[dict[str, Any]] | None:
    """The unit's Catalog sample rows as the triage was made against them, or None when the source has none."""
    if catalog is None:
        return None
    if catalog.is_dir():
        unit_dir = catalog / unit_id
        if not (unit_dir / "handoff.json").is_file():
            return None
        return handoff_rows(unit_dir / "handoff.json")
    data = json.loads(catalog.read_text(encoding="utf-8"))
    for unit in data.get("units") or []:
        if unit.get("unit_id") == unit_id:
            return list(unit.get("rows") or [])
    return None


def handoff_rows(handoff: Path) -> list[dict[str, Any]]:
    """A handoff's sample rows: samples.json beside it, else its sample_metadata (the validator reads them so)."""
    samples = handoff.parent / "samples.json"
    if samples.is_file():
        return list(json.loads(samples.read_text(encoding="utf-8-sig")))
    data = json.loads(handoff.read_text(encoding="utf-8-sig"))
    return list(data.get("sample_metadata") or [])


def rows_sha256(rows: Sequence[Mapping[str, Any]] | None) -> str | None:
    """sha256 of the rows a triage's row_index names: [[sample_id, raw_file], ...] in handoff order.

    The validator ties every row_index to exactly these two fields (T-COVER), so two handoffs with the same rows
    are the same thing to a triage even where the Catalog rewrote other parts of the file (its paths, its Class)."""
    if rows is None:
        return None
    return sha256_bytes(canonical([[str(row.get("sample_id") or ""), str(row.get("raw_file") or "")] for row in rows]))


# ---- the validator ------------------------------------------------------------------------------------------------

def validator_sha256() -> str:
    return sha256_file(VALIDATOR)


def run_validator(accession_dir: Path, *, python: str | None = None, timeout: float = 600.0) -> dict[str, Any]:
    """scripts/validate-triage.py --json on one accession's directory, with the Catalog and listings its inputs\\
    hold: {verdict, exit_code, report, report_sha256, validator_sha256, catalog_source, listings}.

    A verdict other than pass, a report that does not parse, or a validator that cannot run is never taken for a
    pass: verdict is "error" then, with why."""
    catalog, listings = accession_sources(accession_dir)
    command = [python or sys.executable, "-I", str(VALIDATOR), str(accession_dir), "--json"]
    if catalog is not None:
        command += ["--catalog", str(catalog)]
    for listing in listings:
        command += ["--archive-listing", str(listing)]
    record: dict[str, Any] = {
        "validator_sha256": validator_sha256(),
        "catalog_source": None if catalog is None else catalog.relative_to(accession_dir).as_posix(),
        "listings": [{"name": item.relative_to(accession_dir).as_posix(), "sha256": sha256_file(item)} for item in listings],
    }
    try:
        done = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return {**record, "verdict": "error", "exit_code": None, "report": None, "report_sha256": None,
                "problem": f"the validator did not run: {type(error).__name__}: {error}"}
    stdout = done.stdout or b""
    try:
        report = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {**record, "verdict": "error", "exit_code": done.returncode, "report": None, "report_sha256": None,
                "problem": "the validator's report did not parse: " + (done.stderr or b"").decode("utf-8", "replace")[-300:]}
    verdict = str(report.get("verdict") or "error")
    expected = {"pass": 0, "hold": 1, "error": 2}.get(verdict)
    if expected is None or expected != done.returncode:
        verdict = "error"
    return {**record, "verdict": verdict, "exit_code": done.returncode, "report": report,
            "report_sha256": sha256_bytes(canonical(report))}


def finding_lines(report: Mapping[str, Any] | None, limit: int = 5) -> list[str]:
    findings = [item for item in (report or {}).get("findings") or [] if isinstance(item, Mapping)]
    lines = [f"[{item.get('level')}] {item.get('check')} {item.get('unit') or '-'}: {item.get('message')}" for item in findings[:limit]]
    if len(findings) > limit:
        lines.append(f"... {len(findings) - limit} more")
    return lines


# ---- one unit's selection -----------------------------------------------------------------------------------------

def kept_items(triage: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every file the unit keeps: its rows (with their row_index) and its added members."""
    out = []
    for row in triage.get("rows") or []:
        out.append({"member_path": row.get("member_path"), "archive": row.get("archive"), "sample_id": row.get("sample_id"),
                    "row_index": row.get("row_index"), "raw_file": row.get("raw_file"), "role": row.get("role"),
                    "ion_mode": row.get("ion_mode"), "acquisition_mode": row.get("acquisition_mode"),
                    "basis": row.get("basis"), "confidence": row.get("confidence")})
    for member in triage.get("added_members") or []:
        out.append({"member_path": member.get("member_path"), "archive": member.get("archive"), "sample_id": None,
                    "row_index": None, "raw_file": None, "role": member.get("role"), "ion_mode": member.get("ion_mode"),
                    "acquisition_mode": member.get("acquisition_mode"), "basis": member.get("basis"),
                    "confidence": member.get("confidence")})
    return out


def children(triage: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in triage.get("children") or [] if isinstance(item, Mapping)]


def in_child(item: Mapping[str, Any], child: Mapping[str, Any] | None) -> bool:
    if child is None:
        return True
    paths = {_norm(path).casefold() for path in child.get("member_paths") or []}
    return _norm(str(item.get("member_path") or "")).casefold() in paths


def selection(triage: Mapping[str, Any], listing: Mapping[str, Listing], child: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """The unit's (or one child's) selection, as canonical items (see the module docstring)."""
    unit_id = str(triage.get("unit_id") or "")
    child_key = None if child is None else str(child.get("child_key") or "")
    items = []
    for item in kept_items(triage):
        if not in_child(item, child):
            continue
        member = _norm(str(item.get("member_path") or ""))
        listed = listing.get(member.casefold()) if member else None
        items.append({
            "unit_id": unit_id, "child_key": child_key, "member_path": member or None,
            "archive_name": item.get("archive") or (listed.archive if listed else None),
            "crc32": listed.crc32 if listed else None, "size": listed.size if listed else None,
            "sample_row_index": item.get("row_index"),
        })
    items.sort(key=lambda entry: (entry["unit_id"], entry["child_key"] or "", entry["member_path"] or "",
                                  -1 if entry["sample_row_index"] is None else int(entry["sample_row_index"])))
    return items


def analysis_inputs(triage: Mapping[str, Any], child: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """What Interactive declares from the selection (the integration contract's A2): one file input per kept file."""
    inputs = [{"kind": "file", "path": _norm(str(item["member_path"])), "sample_id": item.get("sample_id"),
               "archive": item.get("archive"), "row_index": item.get("row_index")}
              for item in kept_items(triage) if item.get("member_path") and in_child(item, child)]
    return sorted(inputs, key=lambda entry: (entry["path"].casefold(), entry["path"]))


def unresolved_rows(triage: Mapping[str, Any]) -> list[str]:
    """Kept rows that name no archive member: Interactive refuses them (curated_selection_member_unresolved)."""
    return [f"row {row.get('row_index')} ({row.get('raw_file')})" for row in triage.get("rows") or [] if not row.get("member_path")]


def class_on_rows(decision: Mapping[str, Any], triage: Mapping[str, Any], child: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The approved Class cut to the kept rows (the integration contract's B2 interim): the Catalog saves a Class for
    the whole unit only, so the plan keeps that digest and records here what the cut leaves. A declared contrast that
    loses a side excludes the unit (class_contrast_lost_after_triage)."""
    assignments = decision.get("assignments")
    if not isinstance(assignments, list):
        return {"evaluated": False, "why": "the Class decision lists no assignment"}
    label_of = {str(sample): str(label) for sample, label in assignments}
    kept = [str(item["sample_id"]) for item in kept_items(triage) if item.get("sample_id") is not None and in_child(item, child)]
    before = Counter(label_of.values())
    after = Counter(label_of[sample] for sample in kept if sample in label_of)
    contrast_before = sorted(label for label in before if not _NON_SAMPLE_CLASS.match(label))
    contrast_after = sorted(label for label in after if not _NON_SAMPLE_CLASS.match(label))
    lost = sorted(set(contrast_before) - set(contrast_after))
    intact = decision.get("kind") != "proposal" or len(contrast_before) < 2 or len(contrast_after) >= 2
    return {
        "evaluated": True, "kind": decision.get("kind"),
        "counts_before": dict(sorted(before.items())), "counts_after": dict(sorted(after.items())),
        "unassigned_kept_rows": sum(1 for sample in kept if sample not in label_of),
        "lost_classes": lost, "contrast_intact": bool(intact),
    }


def _counts(items: Iterable[Mapping[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(item.get(key)) for item in items if item.get(key) is not None).items()))


def unit_entries(
    *, accession_dir: Path, triage_root: Path, unit_id: str, validation: Mapping[str, Any],
    listing: Mapping[str, Listing], catalog: Path | None,
) -> tuple[list[dict[str, Any]], str | None, str]:
    """(campaign unit entries, exclusion reason or None, detail) for one Catalog unit planned from its triage.

    One entry for a unit without children; one per child for a unit split into single-polarity, single-acquisition
    children, keyed <unit_id>-<child_key> as a split part is. Every entry carries the curated_selection block the
    manifest records; the plan adds the rest of the unit's fields."""
    payload = payload_path(accession_dir, unit_id)
    if payload is None:
        return [], TRIAGE_MISSING, f"no triage file {accession_dir.name}/{unit_id}.json"
    raw = payload.read_bytes()
    try:
        triage = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        return [], TRIAGE_UNREADABLE, f"{payload.name} is not readable JSON: {error}"
    if not isinstance(triage, dict) or triage.get("schema") not in TRIAGE_SCHEMAS or triage.get("unit_id") != unit_id:
        return [], TRIAGE_UNREADABLE, f"{payload.name} is not a triage of {unit_id} ({', '.join(TRIAGE_SCHEMAS)})"
    if validation.get("verdict") != "pass":
        said = "; ".join(finding_lines(validation.get("report"))) or str(validation.get("problem") or "")
        return [], TRIAGE_NOT_PASS, f"validate-triage.py says {validation.get('verdict')} for {accession_dir.name}" + (f": {said}" if said else "")
    unresolved = unresolved_rows(triage)
    if unresolved:
        return [], TRIAGE_MEMBER_UNRESOLVED, f"{len(unresolved)} kept row(s) name no archive member: {', '.join(unresolved[:5])}"
    record = record_path(payload)
    rows = catalog_rows(catalog, unit_id)
    kept = kept_items(triage)
    dropped = list(triage.get("excluded_rows") or [])
    base = {
        "schema": str(triage["schema"]),
        "triage_path": str(payload.resolve()),
        "triage_relative_path": payload.resolve().relative_to(triage_root.resolve()).as_posix(),
        "triage_sha256": sha256_bytes(raw),
        "triage_record_sha256": sha256_file(record) if record.is_file() else None,
        "review_log_head_sha256": review_log_head(accession_dir),
        "catalog_handoff_sha256": triage.get("catalog_handoff_sha256"),
        "catalog_rows_sha256": rows_sha256(rows),
        "catalog_row_count": triage.get("catalog_row_count"),
        "validator": {"script_sha256": validation.get("validator_sha256"), "verdict": validation.get("verdict"),
                      "report_sha256": validation.get("report_sha256")},
        "decided_by": dict(triage.get("decided_by") or {}),
        "phase": triage.get("phase"),
        "standing": "model_inference",
        "unit_technical": {key: (triage.get("unit_technical") or {}).get(key)
                           for key in ("ion_mode", "chromatography", "acquisition_mode")},
        "flags": sorted({str(flag.get("code")) for flag in triage.get("flags") or [] if isinstance(flag, Mapping)}),
        "rows_dropped": len(dropped),
        "rows_dropped_by_reason": _counts(dropped, "reason"),
        "members_excluded": len(triage.get("excluded_members") or []),
    }
    entries = []
    for child in children(triage) or [None]:
        mine = [item for item in kept if in_child(item, child)]
        chosen = selection(triage, listing, child)
        inputs = analysis_inputs(triage, child)
        entries.append({
            "child": child,
            "curated_selection": {
                **base,
                # The contract's name for the same value, read by Interactive's campaign check (A2).
                "sha256": base["triage_sha256"],
                "child_key": None if child is None else str(child.get("child_key")),
                "ion_mode": (child or {}).get("ion_mode") or base["unit_technical"]["ion_mode"],
                "acquisition_mode": (child or {}).get("acquisition_mode") or base["unit_technical"]["acquisition_mode"],
                "rows_kept": sum(1 for item in mine if item.get("row_index") is not None),
                "members_added": sum(1 for item in mine if item.get("row_index") is None),
                "kept_by_basis": _counts(mine, "basis"),
                "kept_by_confidence": _counts(mine, "confidence"),
                "kept_by_role": _counts(mine, "role"),
                "selection_count": len(chosen),
                "selection_sha256": sha256_bytes(canonical(chosen)),
                "analysis_inputs_sha256": sha256_bytes(canonical(inputs)),
            },
            "triage": triage,
        })
    return entries, None, ""


# ---- --reopen ------------------------------------------------------------------------------------------------------

def read_reopen(path: Path) -> dict[str, dict[str, Any]]:
    """The units of `plan --reopen <tsv>` (columns unit_key, reason_code, evidence_path; '#' starts a comment):
    {unit_key: {reason_code, evidence_path, evidence_sha256}}. Only these done units of a campaign named by
    --replan-from are planned again, each into a workspace of its own; the earlier outputs are never changed."""
    path = Path(path)
    out: dict[str, dict[str, Any]] = {}
    lines = [line for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise TriageError(f"{path.name} names no unit to reopen.")
    reader = csv.DictReader(lines, delimiter="\t")
    missing = {"unit_key", "reason_code", "evidence_path"} - set(reader.fieldnames or [])
    if missing:
        raise TriageError(f"{path.name} needs the columns unit_key, reason_code and evidence_path (missing {', '.join(sorted(missing))}).")
    for row in reader:
        key = str(row.get("unit_key") or "").strip()
        code = str(row.get("reason_code") or "").strip()
        evidence = str(row.get("evidence_path") or "").strip()
        if not key:
            continue
        if not _REASON_CODE.match(code):
            raise TriageError(f"{path.name}: unit {key} has no reason code (lower-case letters, digits and '_').")
        evidence_path = (path.parent / evidence) if evidence and not Path(evidence).is_absolute() else Path(evidence)
        if not evidence or not evidence_path.is_file():
            raise TriageError(f"{path.name}: unit {key}'s evidence {evidence or '(none)'} is not a file.")
        if key in out:
            raise TriageError(f"{path.name} names unit {key} twice.")
        out[key] = {"reason_code": code, "evidence_path": str(evidence_path.resolve()), "evidence_sha256": sha256_file(evidence_path)}
    if not out:
        raise TriageError(f"{path.name} names no unit to reopen.")
    return out


# ---- the review log ------------------------------------------------------------------------------------------------

def append_review_log(accession_dir: Path, kind: str, decision_id: str | None, at: str, **fields: Any) -> dict[str, Any]:
    """Append one entry to the accession's review-log.jsonl: seq, at, kind, decision_id and prev_sha256 (sha256 of the
    previous line's bytes, null for the first), with the kind's fields. Never rewrites a line."""
    path = accession_dir / REVIEW_LOG
    previous = path.read_bytes().split(b"\n") if path.is_file() else []
    lines = [line for line in previous if line.strip()]
    seq = 1
    if lines:
        try:
            seq = int(json.loads(lines[-1].decode("utf-8")).get("seq") or len(lines)) + 1
        except (UnicodeDecodeError, ValueError):
            seq = len(lines) + 1
    entry = {**fields, "seq": seq, "at": at, "kind": kind, "decision_id": decision_id,
             "prev_sha256": sha256_bytes(lines[-1]) if lines else None}
    data = canonical(entry)
    prefix = b"" if not previous or previous[-1] == b"" else b"\n"
    with path.open("ab") as handle:
        handle.write(prefix + data + b"\n")
    return entry


# ---- the decision ledger export -----------------------------------------------------------------------------------

def decision_lines(events: Iterable[Mapping[str, Any]]) -> list[bytes]:
    """The decision ledger's lines from the campaign's events (DECISION_KINDS), in event order, each chained to the one
    before it by prev_sha256 (sha256 of that line's bytes)."""
    lines: list[bytes] = []
    for event in events:
        if event.get("kind") not in DECISION_KINDS:
            continue
        detail = event.get("detail_json")
        detail = json.loads(detail) if isinstance(detail, str) else dict(detail or {})
        standing = detail.pop("standing", None)
        entry = {"seq": len(lines) + 1, "event_seq": event.get("seq"), "at": event.get("at"), "kind": event.get("kind"),
                 "unit_key": event.get("unit_key"), "standing": standing, "detail": detail,
                 "prev_sha256": sha256_bytes(lines[-1]) if lines else None}
        lines.append(canonical(entry))
    return lines


def export_decision_ledger(path: Path, events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Write the decision ledger, append only: lines already on disk must be exactly the ones the ledger renders, and
    only the new ones are appended. Raises TriageError, writing nothing, where a line on disk differs."""
    lines = decision_lines(events)
    existing = [line for line in path.read_bytes().split(b"\n") if line.strip()] if path.is_file() else []
    for index, line in enumerate(existing):
        if index >= len(lines) or line != lines[index]:
            raise TriageError(f"{path.name} line {index + 1} is not what the campaign ledger records; nothing was written.")
    new = lines[len(existing):]
    if new or not path.is_file():
        with path.open("ab") as handle:
            handle.write(b"".join(line + b"\n" for line in new))
    return {"path": str(path), "lines": len(lines), "appended": len(new),
            "head_sha256": sha256_bytes(lines[-1]) if lines else None}


def verify_decision_ledger(path: Path) -> list[str]:
    """Why the exported decision ledger's chain does not hold, or nothing."""
    problems = []
    previous = None
    for index, line in enumerate([item for item in Path(path).read_bytes().split(b"\n") if item.strip()], start=1):
        try:
            entry = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            problems.append(f"line {index} does not parse")
            previous = sha256_bytes(line)
            continue
        if entry.get("seq") != index:
            problems.append(f"line {index} has seq {entry.get('seq')}")
        if entry.get("prev_sha256") != previous:
            problems.append(f"line {index} does not chain to line {index - 1}")
        if entry.get("standing") not in STANDINGS:
            problems.append(f"line {index} has no standing")
        previous = sha256_bytes(line)
    return problems
