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

THE SELECTION (msdial-selection-canonical.v1, WS0 decision 1). docs/llm-assisted-curation.md section 3.1 is normative,
and Interactive 0.5.69 implements it as msdial_app.curated_selection.selection_canonical. This module carries a copy of
that reference implementation unchanged (selection_canonical, selection_sha256, parse_archive_listing below), so the
plan and scripts/replay-campaign.py run without importing Interactive; tests/test_campaign_interactive_integration.py
asserts that the copy gives Interactive's value and the doc's two worked-example hashes. crc32 and size come only from
the archive listing(s) the unit's decision record binds (its inputs[] with role archive_listing, merged in order),
never from the triage and never from a download; without a record, or a record binding none, both are null. Interactive
compares the selection_sha256 the approved manifest names with its own before any byte moves
(curated_selection_selection_mismatch), so the plan must compute it exactly as Interactive does. The analysis inputs
are what Interactive declares from the selection ({kind: "file", path: member_path, sample_id, archive}, sorted by
path); their sha256 is analysis_inputs_sha256, the runner's own record. A third party recomputes both from the
released files.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from collections import Counter
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
# The triage's decision record names another payload, or an input it binds (an archive listing, the Catalog handoff)
# is missing or holds other bytes: Interactive refuses such a unit (curated_selection_record_mismatch), so the plan
# leaves it out before a person approves it.
TRIAGE_RECORD_MISMATCH = "triage_record_mismatch"
CLASS_CONTRAST_LOST = "class_contrast_lost_after_triage"
# The triage left the unit out by the policy of 2026-11-18 (status excluded: no file with product-ion spectra once its
# MS1-only files are left out, no raw file in the deposit, or byte-identical copies under two arms' folders until a
# post-download reassignment exists); the detail names the triage's own reason (UNIT_EXCLUSION_REASONS).
TRIAGE_UNIT_EXCLUDED = "triage_unit_excluded"
UNIT_EXCLUSION_REASONS = ("no_product_ion_files", "no_files", "shared_content_unassigned")
EXCLUSION_REASONS = (TRIAGE_MISSING, TRIAGE_UNREADABLE, TRIAGE_NOT_PASS, TRIAGE_MEMBER_UNRESOLVED, TRIAGE_RECORD_MISMATCH,
                     CLASS_CONTRAST_LOST, TRIAGE_UNIT_EXCLUDED)
SELECTION_CANONICAL_FORM = "msdial-selection-canonical.v1"
ANALYSIS_CSV_CANONICAL_FORM = "msdial-analysis-csv-canonical.v1"
# The review log's kinds and the fields each carries (docs/llm-assisted-curation.md section 2.4, WS0 decision 8). The
# runner writes only digest_approval and person_answer; the gate and header verdicts are ledger events, never here.
REVIEW_LOG_KINDS = {
    "validator_verdict": ("tool", "code_commit", "verdict", "findings_sha256", "findings"),
    "person_answer": ("by", "question_code", "answer", "channel"),
    "revision": ("supersedes", "new_decision_id", "cause"),
    "digest_approval": ("manifest_digest", "approval_id", "by"),
}

# The kinds of campaign event that make up the decision ledger, and the standing of what each records
# (docs/llm-assisted-curation.md; the integration contract's B6). An event's detail carries its own standing.
SOURCE_FACT, MODEL_INFERENCE, DETERMINISTIC_CHECK, ACCEPTED_DECISION, PERSON_ANSWER = (
    "source_fact", "model_inference", "deterministic_check", "accepted_decision", "person_answer")
STANDINGS = (SOURCE_FACT, MODEL_INFERENCE, DETERMINISTIC_CHECK, ACCEPTED_DECISION, PERSON_ANSWER)
DECISION_KINDS = (
    "triage_recorded", "triage_validated", "manifest_approved", "unit_reopened", "triage_checked",
    "curated_selection_applied", "header_reconciliation", "cross_unit_verdict", "terminal_refusal", "person_answer",
    # A download-only unit (plan --download-only-held, 2026-11-19): planned so under the approval, and kept once downloaded.
    "download_only_planned", "download_only_kept",
)
# The fields of a person_answer (a named person's answer to a question the triage or a gate raised): only a request
# that carries a question_code and a named person becomes one (WS0 decision 8).
PERSON_ANSWER_CHANNEL = "conversation"

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


# ---- msdial-selection-canonical.v1: a copy of the doc's section 3.1 reference implementation ---------------------
# Kept as Interactive 0.5.69 has it (msdial_app.curated_selection), so that the plan, the runner, Interactive and
# replay-campaign.py give one value for one unit. Do not edit one side alone: the integration tests compare the two.

def canonical_json(value: Any) -> bytes:
    """Sorted keys, no spaces, UTF-8, no ASCII escaping: the one form every selection hash is taken over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _selection_member(path: Any) -> str:
    return str(path or "").replace("\\", "/").strip().strip("/")


def _child_of(triage: Mapping[str, Any], child_key: str) -> dict[str, Any] | None:
    for child in triage.get("children") or []:
        if isinstance(child, dict) and str(child.get("child_key") or "") == child_key:
            return child
    return None


def selection_canonical(
    triage: Mapping[str, Any],
    listing: Mapping[str, Mapping[str, Any]] | None = None,
    child: Mapping[str, Any] | str | None = None,
) -> list[dict[str, Any]]:
    """The selection in SELECTION_CANONICAL_FORM (docs/llm-assisted-curation.md section 3.1): every item the triage keeps,
    its rows[] and its added_members[], one entry each, {unit_id (bare, also for a child), child_key, member_path,
    archive_name, crc32 and size (from `listing` only), sample_row_index}, sorted by unit_id, child_key (null as ''),
    member_path, sample_row_index (null as -1), then canonical JSON.

    `listing` is {case-folded member path: {archive, crc32, size}} from the decision record's archive listing(s)
    (record_inputs); {} or None when it binds none. `child` is one item of triage["children"], its child_key (an
    unknown key raises ValueError), or None for the whole unit."""
    if isinstance(listing, str):
        raise TypeError("selection_canonical(triage, listing, child): listing is a mapping, not a child key.")
    listing = listing or {}
    if isinstance(child, str):
        if not child:
            child = None
        else:
            found = _child_of(triage, child)
            if found is None:
                raise ValueError(f"The triage has no child {child!r}.")
            child = found
    child_key = None if child is None else str(child["child_key"])
    allowed = None if child is None else {_selection_member(p).casefold() for p in child.get("member_paths") or []}
    kept = [(item, item.get("row_index")) for item in triage.get("rows") or []]
    kept += [(item, None) for item in triage.get("added_members") or []]
    entries = []
    for item, row_index in kept:
        member = _selection_member(item.get("member_path"))
        if allowed is not None and member.casefold() not in allowed:
            continue
        listed = (listing.get(member.casefold()) or {}) if member else {}
        crc32, size = listed.get("crc32"), listed.get("size")
        entries.append({
            "unit_id": str(triage["unit_id"]),
            "child_key": child_key,
            "member_path": member or None,
            "archive_name": item.get("archive") or listed.get("archive") or None,
            "crc32": None if crc32 in (None, "") else str(crc32).upper().zfill(8),
            "size": None if size in (None, "") else int(size),
            "sample_row_index": None if row_index is None else int(row_index),
        })
    return sorted(entries, key=lambda e: (e["unit_id"], e["child_key"] or "", e["member_path"] or "",
                                          -1 if e["sample_row_index"] is None else e["sample_row_index"],
                                          canonical_json(e)))


def selection_sha256(
    triage: Mapping[str, Any],
    listing: Mapping[str, Mapping[str, Any]] | None = None,
    child: Mapping[str, Any] | str | None = None,
) -> str:
    """sha256 of the canonical JSON of selection_canonical(triage, listing, child)."""
    return sha256_bytes(canonical_json(selection_canonical(triage, listing, child)))


def parse_archive_listing(data: bytes, listing: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """An archive listing TSV (columns archive, path, size, crc32; others ignored) as selection_canonical reads it:
    {case-folded member path: {archive, crc32, size}}. The first row of a path wins; `listing` is extended in place when
    given. Raises ValueError for a size that is not a whole number."""
    found: dict[str, dict[str, Any]] = listing if listing is not None else {}
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")), delimiter="\t")
    for record in reader:
        member = _selection_member(record.get("path"))
        if not member:
            continue
        size = str(record.get("size") or "").strip()
        if size and not size.isdigit():
            raise ValueError(f"The archive listing gives size {size!r} for {member}.")
        found.setdefault(member.casefold(), {
            "archive": str(record.get("archive") or "").strip() or None,
            "crc32": str(record.get("crc32") or "").strip() or None,
            "size": int(size) if size else None,
        })
    return found


_ABSOLUTE_CELL = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


def _canonical_cell(cell: str, raw_directory: str) -> str:
    text = cell.strip()
    if not _ABSOLUTE_CELL.match(text) or not raw_directory:
        return cell
    path = text.replace("\\", "/")
    raw = str(raw_directory).replace("\\", "/").rstrip("/")
    unit = raw.rsplit("/", 1)[0] if "/" in raw else raw
    if path.casefold() == raw.casefold():
        return "."
    if path.casefold().startswith(raw.casefold() + "/"):
        return path[len(raw) + 1:]
    if path.casefold().startswith(unit.casefold() + "/"):
        return "../" + path[len(unit) + 1:]
    return "<external>/" + path.rsplit("/", 1)[-1]


def canonical_analysis_csv(data: bytes, raw_directory: str) -> bytes:
    """An analysis CSV in ANALYSIS_CSV_CANONICAL_FORM (section 3.2), as Interactive 0.5.69 computes it: every absolute
    path relative to the unit's raw directory (elsewhere in the unit workspace as ../<path>, outside it as
    <external>/<name>), forward slashes, blank lines dropped, LF, UTF-8 without a byte-order mark; cells split on ','."""
    text = data.decode("utf-8-sig")
    lines = [",".join(_canonical_cell(cell, raw_directory) for cell in line.split(","))
             for line in text.splitlines() if line.strip()]
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---- the decision record a triage is bound to -----------------------------------------------------------------------

def record_inputs(record: Path, triage_sha256: str) -> dict[str, Any]:
    """What a decision record (msdial-llm-decision-record.v1) binds, read as Interactive reads it
    (curated_selection.read_decision_record): {listing, archive_listings [{path, sha256, entries}], catalog_handoff
    ({path, sha256} or None), sha256, problem}. `problem` is "" where the record holds; otherwise it says why Interactive
    would refuse the unit (curated_selection_record_mismatch): a record that is not JSON, names another payload, or binds
    an input that is missing or holds other bytes. Inputs are read relative to the record's own directory."""
    out: dict[str, Any] = {"listing": {}, "archive_listings": [], "catalog_handoff": None, "sha256": None, "problem": ""}
    try:
        data = Path(record).read_bytes()
        value = json.loads(data.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return {**out, "problem": f"the decision record {Path(record).name} cannot be read as JSON: {error}"}
    out["sha256"] = sha256_bytes(data)
    if not isinstance(value, dict):
        return {**out, "problem": f"the decision record {Path(record).name} is not one JSON object"}
    payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
    named = str(payload.get("sha256") or "").strip().casefold()
    if named != triage_sha256:
        return {**out, "problem": f"the decision record {Path(record).name} names payload {named or '(none)'}, not the "
                                  f"triage's {triage_sha256}"}
    listing: dict[str, dict[str, Any]] = {}
    for item in [entry for entry in value.get("inputs") or [] if isinstance(entry, dict)]:
        role = str(item.get("role") or "")
        if role not in ("archive_listing", "catalog_handoff"):
            continue
        text = str(item.get("path") or "").strip()
        expected = str(item.get("sha256") or "").strip().casefold()
        if not text or not expected:
            return {**out, "problem": f"the decision record binds a {role} input without a path and sha256"}
        location = Path(text) if Path(text).is_absolute() else Path(record).parent / text
        if role == "catalog_handoff":
            # Resolved, and read only where Interactive compares a handoff copy with it; recorded here as named.
            out["catalog_handoff"] = {"path": text, "sha256": expected}
            continue
        try:
            content = location.read_bytes()
        except OSError as error:
            return {**out, "problem": f"the archive listing {text} the decision record binds cannot be read: {error}"}
        if sha256_bytes(content) != expected:
            return {**out, "problem": f"the archive listing {text} hashes to {sha256_bytes(content)}; the record names {expected}"}
        before = len(listing)
        try:
            parse_archive_listing(content, listing)
        except (UnicodeDecodeError, ValueError) as error:
            return {**out, "problem": f"the archive listing {text} cannot be read: {error}"}
        out["archive_listings"].append({"path": text, "sha256": expected, "entries": len(listing) - before})
    out["listing"] = listing
    return out


# ---- paths a released record may hold ----------------------------------------------------------------------------

_ABSOLUTE_TEXT = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/(?!/))")


def portable_path(text: str, root: str | Path | None) -> str:
    """A path as a released record may hold it (WS0 decision 8, section 5.3): under `root`, relative to it with forward
    slashes; any other absolute path, <external>/<file name>; anything else as it is."""
    value = str(text or "")
    if not _ABSOLUTE_TEXT.match(value):
        return value
    path = value.replace("\\", "/")
    base = str(root or "").replace("\\", "/").rstrip("/")
    if base and (path.casefold() == base.casefold() or path.casefold().startswith(base.casefold() + "/")):
        return path[len(base):].lstrip("/") or "."
    return "<external>/" + path.rstrip("/").rsplit("/", 1)[-1]


def portable(value: Any, root: str | Path | None) -> Any:
    """`value` with every string that is an absolute path made portable (portable_path), recursively."""
    if isinstance(value, str):
        return portable_path(value, root)
    if isinstance(value, Mapping):
        return {key: portable(item, root) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [portable(item, root) for item in value]
    return value


def release_projection(manifest: Any, workspace_root: str | Path, base: str | Path,
                       libraries: Mapping[str, str] | None = None) -> Any:
    """A manifest as it is released (section 5.3, `<name>.release.json`): every absolute path under the workspace root
    made relative to `base` (the unit's directory; the campaign directory for the campaign manifest), any other path
    reduced to <external>/<file name>, and every library path named by file name and sha256 (`libraries`: {file name:
    sha256}) as <library:<name> sha256=<sha256>>. Pure."""
    root = str(workspace_root or "").replace("\\", "/").rstrip("/")
    anchor = str(base or "").replace("\\", "/").rstrip("/")
    named = {str(name).casefold(): str(sha) for name, sha in (libraries or {}).items()}

    def one(text: str) -> str:
        if not _ABSOLUTE_TEXT.match(text):
            return text
        path = text.replace("\\", "/")
        name = path.rstrip("/").rsplit("/", 1)[-1]
        if name.casefold() in named:
            return f"<library:{name} sha256={named[name.casefold()]}>"
        if root and path.casefold().startswith(root.casefold() + "/"):
            relative = os.path.relpath(path, anchor or root).replace("\\", "/")
            return relative
        return "<external>/" + name

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return one(value)
        if isinstance(value, Mapping):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [walk(item) for item in value]
        return value

    return walk(manifest)


RELEASE_INDEX = "release-index.json"
RELEASE_INDEX_SCHEMA = "msdial-release-index.v1"

# The redaction check of section 5.3 is scripts/check-release-redaction.py; the release command calls it here, on the
# bundle it writes, and decides with its three path rules (drive letter, UNC, user profile; as text and as JSON escapes)
# which triage inputs it projects.
REDACTION_SCRIPT = Path(__file__).resolve().parent.parent / "check-release-redaction.py"
_REDACTION_MODULE: Any = None


def redaction_check() -> Any:
    """scripts/check-release-redaction.py, loaded once as a module."""
    global _REDACTION_MODULE
    if _REDACTION_MODULE is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("check_release_redaction", REDACTION_SCRIPT)
        if spec is None or spec.loader is None:
            raise TriageError(f"{REDACTION_SCRIPT.name} cannot be loaded.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _REDACTION_MODULE = module
    return _REDACTION_MODULE


def release_redaction_findings(bundle: Path, limit: int = 20, library_names: Iterable[str] = ()) -> list[dict[str, Any]]:
    """scripts/check-release-redaction.py over every file of `bundle` (section 5.3): a drive letter, UNC or user profile
    path, a library file named with a path, and in a transcript a block from a context file or memory.
    [{file (bundle-relative), rule, at (byte offset), line}], at most `limit`."""
    return redaction_check().check_bundle(Path(bundle), library_names=library_names, limit=limit)


def _holds_absolute_path(data: bytes) -> bool:
    return bool(redaction_check().holds_absolute_path(data))


def _protected_triage_file(relative: str) -> bool:
    """A triage-directory file whose own bytes the manifest, a record or the replay binds (the payloads and their
    revisions, the decision records, the review log): it is released byte for byte, never projected."""
    name = relative.rsplit("/", 1)[-1]
    if name == REVIEW_LOG or name.endswith(".record.json"):
        return True
    return "/inputs/" not in f"/{relative}" and name.endswith(".json")


def write_release(
    *, campaign_dir: Path, out: Path, workspace_root: str | Path, approved_digest: str,
    run_manifests: Iterable[tuple[str, Path, Path]] = (), libraries: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Write the release bundle section 5.3 defines into `out` (an empty or new folder), which scripts/replay-campaign.py
    replays: campaign-manifest.release.json and units\\<unit key>\\run-manifest.release.json (release_projection, as
    canonical JSON), the triage directory the manifest names (as triage\\), decision-ledger.jsonl as it is, and
    release-index.json, which lists per manifest the sha256 of its original (checkable only here) and of its projection,
    with the digest the person approved. `run_manifests` are (unit key, run manifest path, unit workspace).

    THE TRIAGE INPUTS (WS0 decision C, 2026-11-17). The triage directory is copied byte for byte, except a JSON file
    under an inputs\\ folder that holds an absolute path: the Catalog writes its handoff's sidecar paths
    (file_manifest_path, sample_table_path, analysis_input_manifest_path) as resolved drive-letter paths, which the
    redaction check refuses. Such a file is released as its release_projection (canonical JSON, paths relative to its
    own folder or <external>/<name>) at the same path, and release-index.json's triage_inputs lists it with the sha256
    of its original bytes (the value the triage's catalog_handoff_sha256 and the decision record bind, kept with the
    original bytes on the producing machine) and of the released bytes. A payload, a decision record or the review log
    that holds an absolute path cannot be projected without breaking the hashes the replay checks, so the bundle is
    refused. Last, the whole bundle is checked by scripts/check-release-redaction.py (release_redaction_findings, with the
    pinned libraries' names); a bundle with a finding is refused and gets no release-index.json."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise TriageError(f"{out} is not empty; a release bundle is written into an empty folder.")
    entries = []
    triage_inputs: list[dict[str, Any]] = []

    def project(name: str, source: Path, base: Path, target: str) -> None:
        data = source.read_bytes()
        value = json.loads(data.decode("utf-8-sig"))
        released = canonical_json(release_projection(value, workspace_root, base, libraries))
        path = out / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(released)
        entries.append({"name": name, "release": target, "original_sha256": "sha256:" + sha256_bytes(data),
                        "release_sha256": sha256_bytes(released)})

    manifest_path = Path(campaign_dir) / "campaign-manifest.json"
    project("campaign-manifest.json", manifest_path, Path(campaign_dir), "campaign-manifest.release.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    for unit_key, path, workspace in run_manifests:
        if Path(path).is_file():
            project(f"units/{unit_key}/run-manifest.json", Path(path), Path(workspace),
                    f"units/{unit_key}/run-manifest.release.json")
    triage = Path(str((manifest.get("triage") or {}).get("directory") or ""))
    if str(triage) and triage.is_dir():
        for source in sorted(triage.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(triage).as_posix()
            target = out / "triage" / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            data = source.read_bytes()
            if not _holds_absolute_path(data):
                target.write_bytes(data)
                continue
            if _protected_triage_file(relative) or not source.name.endswith(".json"):
                raise TriageError(
                    f"triage/{relative} holds an absolute path, and its bytes are bound as they are (a payload, a "
                    "decision record, the review log, or a file that is not JSON): it cannot be released projected; "
                    "write it again without the path.")
            try:
                value = json.loads(data.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError) as error:
                raise TriageError(f"triage/{relative} holds an absolute path and is not JSON: {error}") from error
            released = canonical_json(release_projection(value, workspace_root, source.parent, libraries))
            target.write_bytes(released)
            triage_inputs.append({"name": f"triage/{relative}", "release": f"triage/{relative}",
                                  "original_sha256": sha256_bytes(data), "release_sha256": sha256_bytes(released),
                                  "projection": "release_projection"})
    ledger = Path(campaign_dir) / DECISION_LEDGER
    if ledger.is_file():
        (out / DECISION_LEDGER).write_bytes(ledger.read_bytes())
    findings = release_redaction_findings(out, library_names=list((libraries or {}).keys()))
    if findings:
        raise TriageError("The release bundle fails the redaction check and is not complete (no release-index.json): "
                          + "; ".join(f"{item['file']} ({item['rule']})" for item in findings[:5]))
    index = {"schema": RELEASE_INDEX_SCHEMA, "approved_manifest_digest": approved_digest, "manifests": entries,
             "triage_inputs": triage_inputs}
    (out / RELEASE_INDEX).write_bytes(canonical_json(index))
    return index


def released_triage_inputs(bundle: Path) -> dict[str, dict[str, Any]]:
    """The triage inputs a release bundle holds as projections (release-index.json triage_inputs), by bundle-relative
    path: {name, release, original_sha256, release_sha256}. {} for a campaign directory or a bundle without any."""
    index = Path(bundle) / RELEASE_INDEX
    if not index.is_file():
        return {}
    value = json.loads(index.read_text(encoding="utf-8-sig"))
    return {str(item["release"]): dict(item) for item in value.get("triage_inputs") or []
            if isinstance(item, Mapping) and item.get("release")}


def released_catalog(bundle: Path, accession_dir: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """For an accession of a release bundle whose inputs\\<unit_id>\\handoff.json were released as projections: the
    catalog.json form of the same Catalog rows (validate-triage --catalog reads it as it reads a handoffs directory),
    each projected handoff's sha256 the original's that the release index names, once its released bytes are checked
    against the index's release_sha256. (None, []) where nothing was projected; (None, problems) where a released
    projection is not the bytes the index names (the replay then differs; the original's sha256 is never taken on
    trust)."""
    bundle = Path(bundle)
    projected = released_triage_inputs(bundle)
    inputs = Path(accession_dir) / "inputs"
    handoffs = sorted(inputs.glob("*/handoff.json")) if inputs.is_dir() else []
    # A triage folder outside the bundle (a campaign directory replayed with its triage kept elsewhere) holds nothing
    # released as a projection: its handoffs are read as they are, never refused for where they are.
    named = {path: (projected.get(path.relative_to(bundle).as_posix()) if path.is_relative_to(bundle) else None)
             for path in handoffs}
    if not any(named.values()):
        return None, []
    units, problems = [], []
    for path, entry in named.items():
        data = path.read_bytes()
        if entry is not None and sha256_bytes(data) != str(entry.get("release_sha256") or ""):
            problems.append(f"{path.relative_to(bundle).as_posix()} hashes to {sha256_bytes(data)}, not the release "
                            f"index's {entry.get('release_sha256')}")
            continue
        value = json.loads(data.decode("utf-8-sig"))
        technical = value.get("technical_settings") or {}
        units.append({"unit_id": path.parent.name, "rows": handoff_rows(path),
                      "declared": {key: technical.get(key) for key in ("chromatography", "ion_mode", "acquisition_mode")},
                      "handoff_sha256": str(entry["original_sha256"]) if entry is not None else sha256_bytes(data),
                      "accession": value.get("accession")})
    return ({"units": units} if not problems else None), problems


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


# ---- the triaged handoff against the one taken at run time (WS0 decision F, 2026-11-17) -----------------------------
# Interactive 0.5.69's curated_selection.verify_handoff_copy binds the handoff a runner passes to the triaged original by
# value. Decision F (superseding decision A's ratification-only scope): the triage binds the evidence it read; the Class
# binds through its own ratification. The triage runs before any Class is saved (it reads the Catalog's rows, files and
# listing, never the Class), so the handoff it names normally carries no Class or an unratified one, and this runner's
# boundary-3 save (_state_pending) settles and ratifies the Class afterwards. So the comparison leaves out what the
# Class decides, exactly as msdial_repository_catalog's msdial_catalog_reanalysis_handoff derives it (4ffa0ff):
#
# - "keys": the whole class_proposal object (the proposal, assignments, contrast, status, ratification, ratified_by);
# - "blocking_reason_codes": the blocking_reasons codes the builder adds from the Class alone. blocking_reasons is
#   compared with only these removed: any other code (technical_metadata:*, analysis_input:*, an unknown
#   class_proposal:*) appearing or disappearing is still a difference;
# - "readiness_keys": next_action and ready_for_download_planning, left out only where the remaining (non-Class)
#   blocking_reasons are equal on both sides, and compared like any key otherwise.
#
# Everything else is compared: the files, the sample rows, the technical settings, the sidecars' contents, the unit and
# accession, every other top-level key. HANDOFF_COPY_REWRITTEN_KEYS and HANDOFF_CLASS_KEYS are Interactive's, copied
# so the runner decides at its handoff step exactly what Interactive will decide at the download;
# tests/test_campaign_interactive_integration.py asserts both equal Interactive's own. The Class itself is bound by
# class_binding below (the copy's proposal is the approved one, accepted and ratified under this campaign's approval)
# and by _state_pending's class-digest drift stop.
HANDOFF_COPY_REWRITTEN_KEYS = ("file_manifest_path", "sample_table_path", "analysis_input_manifest_path")
HANDOFF_CLASS_KEYS: dict[str, tuple[str, ...]] = {
    "keys": ("class_proposal",),
    "blocking_reason_codes": ("class_proposal:missing", "class_proposal:not_accepted", "class_proposal:other_unit"),
    "readiness_keys": ("next_action", "ready_for_download_planning"),
}


def handoff_class_split(blocking_reasons: Any) -> tuple[Any, list[str]]:
    """blocking_reasons without the Class-derived codes (HANDOFF_CLASS_KEYS), and those codes in their order. A value
    that is not a list is returned as it is, with no Class code: it is compared whole (as in Interactive)."""
    if not isinstance(blocking_reasons, list):
        return blocking_reasons, []
    codes = set(HANDOFF_CLASS_KEYS["blocking_reason_codes"])
    return ([item for item in blocking_reasons if str(item) not in codes],
            [str(item) for item in blocking_reasons if str(item) in codes])


def handoff_differing_keys(original: Mapping[str, Any], copy: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """The top-level keys in which `copy` is not `original` as a triage binds it, and the Class record: Interactive's
    curated_selection.handoff_differing_keys, the same rule over the same keys. The record is {excluded_keys,
    excluded_blocking_reason_codes, readiness_keys, readiness_left_out, original, copy (each {class_proposal,
    class_blocking_reasons, and the readiness keys' values}), differs (whether the two class_proposal values differ)}.
    Pure; the sidecars are handoff_difference's."""
    left_out = set(HANDOFF_COPY_REWRITTEN_KEYS) | set(HANDOFF_CLASS_KEYS["keys"])
    readiness = HANDOFF_CLASS_KEYS["readiness_keys"]
    original_rest, original_codes = handoff_class_split(original.get("blocking_reasons"))
    copy_rest, copy_codes = handoff_class_split(copy.get("blocking_reasons"))
    readiness_left_out = canonical_json(original_rest) == canonical_json(copy_rest)
    differing: list[str] = []
    for key in sorted((set(original) | set(copy)) - left_out):
        if key in readiness and readiness_left_out:
            continue
        same = (readiness_left_out if key == "blocking_reasons"
                else canonical_json(original.get(key)) == canonical_json(copy.get(key)))
        if not same:
            differing.append(key)

    def side(handoff: Mapping[str, Any], codes: list[str]) -> dict[str, Any]:
        return {"class_proposal": handoff.get("class_proposal"), "class_blocking_reasons": codes,
                **{key: handoff.get(key) for key in readiness}}

    return differing, {
        "excluded_keys": list(HANDOFF_CLASS_KEYS["keys"]),
        "excluded_blocking_reason_codes": list(HANDOFF_CLASS_KEYS["blocking_reason_codes"]),
        "readiness_keys": list(readiness),
        "readiness_left_out": readiness_left_out,
        "original": side(original, original_codes),
        "copy": side(copy, copy_codes),
        "differs": canonical_json(original.get("class_proposal")) != canonical_json(copy.get("class_proposal")),
    }


def _sidecar_value(value: Any) -> tuple[str | None, bytes | None]:
    text = str(value or "").strip()
    path = Path(text).expanduser() if text else None
    if path is None or not path.is_file():
        return None, None
    content = path.read_bytes()
    try:
        return sha256_bytes(content), canonical_json(json.loads(content.decode("utf-8-sig")))
    except (UnicodeDecodeError, ValueError):
        return sha256_bytes(content), content


def handoff_difference(triaged: Path, current: Path) -> dict[str, Any]:
    """Whether the handoff taken now is the triaged one in every value Interactive compares (verify_handoff_copy):
    {"same", "differing_keys", "sidecar" (the first sidecar whose contents differ or is missing on a side, or None),
    "problem", "class" (handoff_differing_keys's record, or None where a handoff cannot be read)}. Pure over the two
    files and their sidecars."""
    out: dict[str, Any] = {"same": False, "differing_keys": [], "sidecar": None, "problem": "", "class": None}
    try:
        original = json.loads(Path(triaged).read_bytes().decode("utf-8-sig"))
        now = json.loads(Path(current).read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        return {**out, "problem": f"a handoff cannot be read: {type(error).__name__}"}
    if not isinstance(original, dict) or not isinstance(now, dict):
        return {**out, "problem": "a handoff is not one JSON object"}
    out["differing_keys"], out["class"] = handoff_differing_keys(original, now)
    for key in HANDOFF_COPY_REWRITTEN_KEYS:
        if not (original.get(key) or now.get(key)):
            continue
        _sha, original_value = _sidecar_value(original.get(key))
        _sha, now_value = _sidecar_value(now.get(key))
        if original_value is None or now_value is None or original_value != now_value:
            out["sidecar"] = key
            out["problem"] = f"the {key} sidecar " + ("is missing on a side" if original_value is None or now_value is None
                                                      else "holds other contents")
            break
    out["same"] = not out["differing_keys"] and out["sidecar"] is None and not out["problem"]
    return out


# What the runner's boundary-3 save (_state_pending) passes as the ratification, and so what the copy's Class must
# carry (msdial_repository_catalog's ratification_record stores them as passed): the approval, the manifest digest,
# the authorization and the campaign. The proposal id is checked against the approved Class digest.
CLASS_RATIFICATION_KEYS = ("approval_id", "manifest_digest", "authorization_sha256", "campaign_id")
ACCEPTED_CLASS_STATUS = "accepted"


def class_binding(handoff: Mapping[str, Any], *, unit_id: str, approved_proposal_id: str,
                  ratification: Mapping[str, Any]) -> dict[str, Any]:
    """Whether the Class a handoff copy carries is the one this campaign ratified (WS0 decision F): the approved
    proposal (its id is the approved Class digest), saved for this unit, accepted, and ratified under this campaign's
    approval (ratified_by and the ratification record name it), a ratified abstention included. Returns {"bound",
    "reason", "detail", ...}: reason "" where bound; "class_digest_changed" for another proposal (the drift stop);
    "class_not_ratified" for no proposal, or the approved one saved for another unit, not accepted, or not ratified
    under this approval. A handoff with no class_proposal key at all (the Catalog always writes the key, null where no
    proposal is named) carries no Class either, so it is class_not_ratified too, never bound: a copy edited to drop the
    key, its class_proposal:* codes and its readiness would otherwise run with no Class and no Class blocker."""
    out: dict[str, Any] = {"bound": False, "reason": "", "detail": ""}
    if "class_proposal" not in handoff:
        return {**out, "reason": "class_not_ratified", "class_proposal_key": False,
                "detail": "the handoff carries no class_proposal key, so no Class"}
    proposal = handoff.get("class_proposal")
    if not isinstance(proposal, Mapping):
        return {**out, "reason": "class_not_ratified", "detail": "the handoff carries no Class proposal"}
    found_id = str(proposal.get("proposal_id") or "")
    if found_id != str(approved_proposal_id or ""):
        return {**out, "reason": "class_digest_changed", "approved": approved_proposal_id, "now": found_id,
                "detail": "the handoff carries a Class proposal other than the approved one"}
    problems: list[str] = []
    if str(proposal.get("unit_id") or "") != unit_id:
        problems.append(f"it was saved for unit {proposal.get('unit_id')!r}")
    if str(proposal.get("status") or "") != ACCEPTED_CLASS_STATUS:
        problems.append(f"it reads {proposal.get('status')!r}, not {ACCEPTED_CLASS_STATUS!r}")
    if str(proposal.get("ratified_by") or "") != str(ratification.get("approval_id") or ""):
        problems.append("its ratified_by is not this campaign's approval")
    record = proposal.get("ratification")
    if not isinstance(record, Mapping):
        problems.append("it carries no ratification record")
    else:
        for key in CLASS_RATIFICATION_KEYS:
            if str(record.get(key) or "") != str(ratification.get(key) or ""):
                problems.append(f"its ratification's {key} is not this campaign's")
        if str(record.get("proposal_id") or "") != found_id:
            problems.append("its ratification names another proposal")
    if problems:
        return {**out, "reason": "class_not_ratified", "problems": problems,
                "detail": "the handoff's Class is the approved proposal but is not accepted and ratified under this "
                          "campaign's approval: " + "; ".join(problems)}
    return {**out, "bound": True, "abstention": bool(isinstance(record, Mapping) and record.get("abstention"))}


def triaged_handoff_candidates(triage_path: Path, catalog_unit_id: str, record_sha256: str | None,
                               triage_sha256: str) -> list[Path]:
    """Where the bytes a triage was made from may be kept: the decision record's catalog_handoff input (read relative to
    the record, as Interactive reads it), then the accession's inputs\\<unit_id>\\handoff.json (section 2.1)."""
    found: list[Path] = []
    if record_sha256:
        record = record_path(Path(triage_path))
        named = (record_inputs(record, triage_sha256) or {}).get("catalog_handoff") if record.is_file() else None
        if isinstance(named, Mapping) and named.get("path"):
            location = Path(str(named["path"]))
            found.append(location if location.is_absolute() else record.parent / location)
    found.append(Path(triage_path).parent / "inputs" / str(catalog_unit_id) / "handoff.json")
    return found


# ---- a workspace name Interactive accepts (WS0 low, 2026-11-17) -----------------------------------------------------
# Interactive 0.5.69's repository_reanalysis.validate_workspace_unit_key, which a child (<unit_id>-<child_key>) or a
# reopened unit (<unit_key>@<campaign_id>) meets as its workspace_unit_key: checked at plan time, so a name Interactive
# would refuse at the download (a plain ValueError reply, retried as an ordinary failure) is never approved.
_WORKSPACE_UNIT_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}")
_WORKSPACE_UNIT_KEY_SEPARATORS = "-_.@+"


def workspace_name_problem(name: str, catalog_unit_id: str) -> str:
    """Why Interactive would refuse `name` as the workspace of Catalog unit `catalog_unit_id`, or "" where it accepts it.
    (Interactive's reserved names all start with '_', which the first character already refuses.)"""
    text = str(name or "").strip()
    unit = str(catalog_unit_id or "").strip()
    if not _WORKSPACE_UNIT_KEY.fullmatch(text) or ".." in text or text.endswith("."):
        return (f"{text!r} is not one folder name of letters, digits and '. _ - @ +' (at most 128 characters, no '..', "
                "not ending in '.')")
    if not unit or not (text == unit or (text.startswith(unit) and text[len(unit)] in _WORKSPACE_UNIT_KEY_SEPARATORS)):
        return f"{text!r} does not start with the Catalog unit id {unit!r} and a separator"
    return ""


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


def run_validator(accession_dir: Path, *, python: str | None = None, timeout: float = 600.0,
                  catalog_override: Path | None = None) -> dict[str, Any]:
    """scripts/validate-triage.py --json on one accession's directory, with the Catalog and listings its inputs\\
    hold: {verdict, exit_code, report, report_sha256, validator_sha256, catalog_source, listings}. `catalog_override`
    is a catalog.json read in place of inputs\\ (the replay's, for a release bundle whose handoffs were released as
    projections: released_catalog).

    A verdict other than pass, a report that does not parse, or a validator that cannot run is never taken for a
    pass: verdict is "error" then, with why."""
    catalog, listings = accession_sources(accession_dir)
    if catalog_override is not None:
        catalog = Path(catalog_override)
    command = [python or sys.executable, "-I", str(VALIDATOR), str(accession_dir), "--json"]
    if catalog is not None:
        command += ["--catalog", str(catalog)]
    for listing in listings:
        command += ["--archive-listing", str(listing)]
    record: dict[str, Any] = {
        "validator_sha256": validator_sha256(),
        "catalog_source": None if catalog is None else (
            "released_catalog" if catalog_override is not None else catalog.relative_to(accession_dir).as_posix()),
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


def selection(triage: Mapping[str, Any], listing: Mapping[str, Mapping[str, Any]] | None,
              child: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """The unit's (or one child's) selection in msdial-selection-canonical.v1 (selection_canonical)."""
    return selection_canonical(triage, listing or {}, child)


def bound_listing(payload: Path, triage_sha256: str) -> dict[str, Any]:
    """record_inputs of the decision record beside a payload (<name>.record.json), or, without one, an empty listing:
    the listing a selection is computed over is the one the record binds, never a TSV that merely lies in inputs\\."""
    record = record_path(payload)
    if not record.is_file():
        return {"listing": {}, "archive_listings": [], "catalog_handoff": None, "sha256": None, "problem": "",
                "record": None}
    return {**record_inputs(record, triage_sha256), "record": record.name}


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
    *, accession_dir: Path, triage_root: Path, unit_id: str, validation: Mapping[str, Any], catalog: Path | None,
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
    if triage.get("status") == "excluded":
        exclusion = triage.get("exclusion") if isinstance(triage.get("exclusion"), Mapping) else {}
        # What the unit left out, by reason, so that a reader of the plan sees which files went and why.
        left = {"rows": _counts(triage.get("excluded_rows") or [], "reason"),
                "members": _counts(triage.get("excluded_members") or [], "reason")}
        counts = "; ".join(f"{kind} " + ", ".join(f"{reason} {n}" for reason, n in found.items())
                           for kind, found in left.items() if found)
        return [], TRIAGE_UNIT_EXCLUDED, (f"{exclusion.get('reason')}: {exclusion.get('detail')}"
                                          + (f" (left out: {counts})" if counts else ""))
    unresolved = unresolved_rows(triage)
    if unresolved:
        return [], TRIAGE_MEMBER_UNRESOLVED, f"{len(unresolved)} kept row(s) name no archive member: {', '.join(unresolved[:5])}"
    record = record_path(payload)
    bound = bound_listing(payload, sha256_bytes(raw))
    if bound["problem"]:
        return [], TRIAGE_RECORD_MISMATCH, bound["problem"]
    listing = bound["listing"]
    rows = catalog_rows(catalog, unit_id)
    kept = kept_items(triage)
    dropped = list(triage.get("excluded_rows") or [])
    base = {
        "schema": str(triage["schema"]),
        "triage_path": str(payload.resolve()),
        "triage_relative_path": payload.resolve().relative_to(triage_root.resolve()).as_posix(),
        "triage_sha256": sha256_bytes(raw),
        "triage_record_sha256": sha256_file(record) if record.is_file() else None,
        # The listing(s) the record binds, which give the selection its crc32 and size (section 3.1).
        "selection_listings": list(bound["archive_listings"]),
        "selection_canonical_form": SELECTION_CANONICAL_FORM,
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
        "members_excluded_by_reason": _counts(triage.get("excluded_members") or [], "reason"),
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
                "selection_sha256": sha256_bytes(canonical_json(chosen)),
                "analysis_inputs_sha256": sha256_bytes(canonical(inputs)),
            },
            "triage": triage,
        })
    return entries, None, ""


# ---- a unit held for a person, planned download-only (plan --download-only-held) ------------------------------------

# The plan reasons a unit may be planned download-only for instead of being left out (the author's decision of
# 2026-11-19: in Pool A a doubtful or hard unit may be downloaded and kept without a run, provided each is recorded with
# its reason; an mzXML-only unit is downloaded and kept, its conversion to mzML comes later). Every other reason
# (triage_unreadable, triage_member_unresolved, triage_record_mismatch, class_contrast_lost_after_triage,
# triage_unit_excluded) still leaves the unit out.
DOWNLOAD_ONLY_FROM = (TRIAGE_MISSING, TRIAGE_NOT_PASS)
# The triage statuses that wait for a person: a unit planned download-only for one of them says so in its reasons.
HELD_STATUSES = ("needs_person", "abstained", "out_of_scope")
_VALIDATOR_MODULE: Any = None


def validator_module() -> Any:
    """scripts/validate-triage.py, loaded once as a module (its blocking flags, blocking_flags_for)."""
    global _VALIDATOR_MODULE
    if _VALIDATOR_MODULE is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("validate_triage", VALIDATOR)
        if spec is None or spec.loader is None:
            raise TriageError(f"{VALIDATOR.name} cannot be loaded.")
        module = importlib.util.module_from_spec(spec)
        # Its dataclasses look their module up in sys.modules as they are made.
        sys.modules.setdefault(spec.name, module)
        spec.loader.exec_module(module)
        _VALIDATOR_MODULE = module
    return _VALIDATOR_MODULE


def download_only_record(
    *, accession_dir: Path, triage_root: Path, unit_id: str, reason: str, detail: str,
    validation: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """The download_only block of a unit the plan would leave out as `reason` (DOWNLOAD_ONLY_FROM), or None where it
    stays out: a reason not in DOWNLOAD_ONLY_FROM, or a triage whose own status is excluded (the triage policy of
    2026-11-18 leaves such a unit out whatever its accession's verdict).

    reasons lists, as codes: the plan reason, the validator's verdict (validator:<verdict>), the triage's status
    (status:<status>), each question for a person (question:<code>), each blocking flag (blocking_flag:<code>) and each
    reason rows were left out for (excluded_rows:<reason>, requires_conversion and polarity_switching among them). The
    rest of the block is the same, structured, with the triage's path and sha256 where there is one. Nothing here is a
    decision: it records why the unit is not run, for the person who will decide."""
    if reason not in DOWNLOAD_ONLY_FROM:
        return None
    reasons = [reason]
    verdict = None if validation is None else str(validation.get("verdict") or "error")
    if verdict is not None:
        reasons.append(f"validator:{verdict}")
    block: dict[str, Any] = {
        "plan_reason": reason, "detail": detail, "validator_verdict": verdict, "triage_status": None,
        "question_codes": [], "blocking_flags": [], "excluded_row_reasons": {}, "triage_relative_path": None,
        "triage_sha256": None,
    }
    payload = payload_path(accession_dir, unit_id) if accession_dir.is_dir() else None
    triage: Any = None
    if payload is not None:
        raw = payload.read_bytes()
        try:
            triage = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            triage = None
        block["triage_relative_path"] = payload.resolve().relative_to(triage_root.resolve()).as_posix()
        block["triage_sha256"] = sha256_bytes(raw)
    if isinstance(triage, Mapping) and triage.get("unit_id") == unit_id:
        status = str(triage.get("status") or "")
        if status == "excluded":
            return None
        block["triage_status"] = status or None
        questions = sorted({str(item.get("code")) for item in triage.get("needs_person") or []
                            if isinstance(item, Mapping) and item.get("code")})
        flags = {str(item.get("code")) for item in triage.get("flags") or [] if isinstance(item, Mapping) and item.get("code")}
        try:
            blocking = sorted(flags & set(validator_module().blocking_flags_for(dict(triage))))
        except Exception:  # noqa: BLE001 - the validator's own list unreadable: every flag is named, none dropped
            blocking = sorted(flags)
        rows = _counts(triage.get("excluded_rows") or [], "reason")
        block.update(question_codes=questions, blocking_flags=blocking, excluded_row_reasons=rows)
        if status:
            reasons.append(f"status:{status}")
        reasons += [f"question:{code}" for code in questions]
        reasons += [f"blocking_flag:{code}" for code in blocking]
        reasons += [f"excluded_rows:{code}" for code in rows]
    return {"reasons": list(dict.fromkeys(reasons)), **block}


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
    previous line's bytes, null for the first), with the kind's fields. Never rewrites a line.

    Only the kinds and fields section 2.4 allows (REVIEW_LOG_KINDS, WS0 decision 8): anything else raises TriageError
    and writes nothing, so a header verdict or an operator's request can never land here."""
    allowed = REVIEW_LOG_KINDS.get(kind)
    if allowed is None:
        raise TriageError(f"{kind} is not a review log kind ({', '.join(REVIEW_LOG_KINDS)}).")
    extra = sorted(set(fields) - set(allowed))
    if extra:
        raise TriageError(f"a {kind} entry carries {', '.join(allowed)}, not {', '.join(extra)}.")
    if kind == "person_answer" and not (str(fields.get("by") or "").strip() and str(fields.get("question_code") or "").strip()):
        raise TriageError("a person_answer names the person (by) and the question it answers (question_code).")
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


def verify_review_log(path: Path) -> tuple[list[str], list[str]]:
    """(why the review log's chain does not hold, the sha256 of each of its lines): each line parses, its seq counts up
    from 1, and its prev_sha256 is the sha256 of the line before it (null for the first)."""
    problems: list[str] = []
    shas: list[str] = []
    previous = None
    for index, line in enumerate([item for item in Path(path).read_bytes().split(b"\n") if item.strip()], start=1):
        try:
            entry = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            entry = None
            problems.append(f"line {index} does not parse")
        if isinstance(entry, dict):
            if entry.get("seq") != index:
                problems.append(f"line {index} has seq {entry.get('seq')}")
            if entry.get("prev_sha256") != previous:
                problems.append(f"line {index} does not chain to line {index - 1}")
        previous = sha256_bytes(line)
        shas.append(previous)
    return problems, shas


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
