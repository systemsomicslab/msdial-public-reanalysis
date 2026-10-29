"""The campaign manifest: which units, in what order, pinned to what, built read-only from the Catalog.

ONE MANIFEST PER POOL. The declared pool comes first: LC-MS units whose repository record declares DDA,
DIA, AIF or SWATH acquisition, does not say the study is targeted, and names one polarity or Both. The
acquisition-unknown pool is its own manifest, and its own approval: the same rules with the acquisition
mode left Unknown by the repository, which the raw headers settle after download (Interactive's
campaign_disposition says whether each unit then runs).

A UNIT WHOSE RECORD SAYS BOTH POLARITIES IS ADMITTED (user decision, 2026-10-29). Interactive reads each file's
polarity from its raw header and splits the unit into single-polarity parts, each run like any single-polarity
unit; a file whose scans switch polarity inside the one file is excluded with its reason (unresolved mixed polarity
is outside the campaign's scope: polarity_switching), one whose header gives no polarity is excluded as
polarity_unresolved, and the rest of the unit runs.

A PILOT IS ONE MANIFEST OF NAMED UNITS (plan --units). It plans exactly the analysis units it is given,
from both pools, and holds each to the rules of the pool it is in: selection_basis says which, for every
planned unit and every exclusion. A named unit in neither pool is listed as not_in_pool, and a name the
Catalog does not know refuses the plan. The pilot the user started on 2026-10-03 is 15 units, raw data
kept.

WHAT IS LEFT OUT, AND WHY, IS PART OF WHAT IS APPROVED. Every selected unit the plan will not run is
listed with its reason:

- no_files: the Catalog lists no raw file for it, so there is nothing to download (111 declared units);
- ion_mobility: the unit's OWN evidence says ion mobility (option A, decided 2026-10-03). This campaign is
  LC-MS only; LC-IM-MS is excluded this time (decided 2026-09-30). Where the Catalog provides
  ion_mobility_evidence(unit), only its state "enabled" from unit-level sources excludes; "mixed",
  "unknown" and a mention in the study's text alone (a title or abstract many units share) pass to
  Interactive. Without that helper, a unit is excluded only when its instrument, or a row's instrument
  field, names a TIMS, Synapt, Vion, 6560 or Cyclic instrument AND none of its inputs is a vendor container
  that cannot hold ion mobility (Bruker BAF or TSF). A unit whose ion-mobility instrument sits beside
  such containers (MTBKS219 and MTBKS220: BAF beside TDF, rows naming a timsTOF) reaches Interactive,
  whose per-file header check and split exclude the ion-mobility files or parts. The Catalog's own
  ion_mobility column is not read: it says Enabled for MTBKS217, a Waters Xevo G2 QTOF, only because the
  abstract it shares with other units mentions ion mobility;
- preexisting_workspace: the unit's own workspace, <workspace_root>\\<repository>\\<accession>\\<unit>, or
  that of one of its split parts (<unit>-<part>), holds an earlier run no campaign made (MTBLS2207's
  a22083b091a0ccd04489 and its -dda and -dia parts; MPST000007's 6f27431da49ec82f3734). The runner never
  writes into a workspace it did not make. The test is the unit's, not its accession's: the other units
  of such an accession are planned, and an accession-level workspace of an earlier run (MTBLS341,
  ST002419, MPST000008, whose raw, provenance and output sit in the accession folder itself) holds back
  none of its units, whose workspaces are folders of their own beside it. Those accessions are listed
  in the manifest (legacy_accession_workspaces) and in the text a person approves;
- campaign_workspace: the unit's own workspace was made by an earlier campaign, and the plan was not
  asked to take that campaign's unfinished units again (--replan-from). A campaign's workspaces are
  known by the authorization copy the runner writes into provenance before anything else is written
  there, so the siblings of a unit a campaign touched are planned as before;
- fragmentation_not_cid_hcd: CID/HCD only (user decision, 2026-10-29). Every reference spectrum is CID/HCD-derived,
  so a unit is excluded where its OWN evidence names another fragmentation method (EAD, OAD, ECD, ETD, EThcD, EID,
  UVPD, IRMPD or the like), read as the ion-mobility option A reads its evidence: the unit's label, its instrument,
  its analysis_unit_evidence, every one of its sample rows' attributes, or every one of its analysis files' names
  (fragmentation_reading). The study's text alone never excludes a unit, since one study can hold EAD and CID units
  (MPST000010: unit 86f50459 is "EAD (14 eV)", its siblings CID or OAD): a study-level field (keywords, title,
  description, ...), or an attribute every unit of a study of several units carries alike, is study text. Absence
  of evidence means run. Where only some of a unit's files or sample rows name another method, the unit is planned
  and says so (fragmentation_reading), and Interactive 0.5.52's campaign lease keeps those files out by their
  names (fragmentation_not_cid_hcd), and by an mzML's activation terms; no vendor header records the method;
- mzdata_only: every analysis file is mzData, which MS-DIAL cannot read and nothing here converts
  (mzXML is converted by Interactive and runs);
- no_download_object: files are listed, but none carries a URL;
- class_undecided: the Catalog neither proposed a Class nor recorded an abstention;
- not_in_pool (a pilot only): a named unit that neither pool's rules select.

THE BYTES are the Catalog's download_plan (Catalog 0.6.0): distinct objects, each fetched once, with
size_known false for an object the repository listed without a size. Such a unit's bytes are a lower
bound, never 0, and the disk guard reserves room for it (policy.DiskPolicy).

THE DIGEST is sha256 over the manifest's canonical JSON (sorted keys, no spaces, UTF-8), and the file on
disk is exactly those bytes, so Interactive's campaign_authorization can check a manifest against the
digest a person approved by hashing the file. Nothing in it names a private library's location:
libraries are pinned by file name, sha256 and size.

THE LIBRARIES PINNED ARE THE ONES THE PROFILE NAMES (2026-10-24). The plan pins, and so the approval names, only the
resource map's libraries the profile names as library:<file name>, each with the role it is loaded in and the ion
modes whose units load it (library_pins); the map's other libraries are listed as left unpinned. The manifest also
states, for a unit of each ion mode, the annotation profile the pinned Interactive applies and the LBM score cutoffs
it writes into the method, read from that Interactive's code (annotation_record), and the summary says both. The plan
used to pin every mapped library, so a map that still locates the official LBM2 the profile replaced would have put
it into the approval.

THE AUTOMATIC RT CORRECTION IS STATED IN WHAT A PERSON APPROVES. The campaign runs it on, with at most 12 anchors,
#826's local window (the Console's 1.5 min) and an uncorrected fallback after an anchor-selection failure, Blanks
interpolated only by a recorded injection order (decided 2026-10-07). A --policy override may change that, and is
not refused for it, but the manifest names the fields the override set (policy_overrides) and keeps the
correction as the summary states it, words included (automatic_rt_correction), so the digest covers what the
person read; where it differs from the decision the summary says so first, in capitals. A manifest planned before
that record carries no statement, and approve refuses every such manifest, telling the person to plan again
(automatic_rt_statement_missing_problems): there is no exception for one that would run uncorrected, since the
user's answer of 2026-10-08 is that the default is correction ON, and a manifest planned now with the default
policy states it on. The statement also says where the profile turns the anchor-library
correction on, and every other setting of the automatic correction the profile gives; under the policy's pin a
profile that gives any of those is refused, since the decision is #826 with Interactive's defaults.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import inspect
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import policy

MANIFEST_SCHEMA = "msdial-campaign-manifest.v1"
PROFILE_SCHEMA = "msdial-campaign-profile.v1"
AUTHORIZATION_SCHEMA = "msdial-campaign-authorization.v1"
POOLS = ("declared", "acquisition_unknown")
# A manifest of named units from both pools (plan --units), each held to its own pool's rules.
PILOT = "pilot"
# The ion modes a pool admits. Both since 2026-10-29: each file's polarity is read from its raw header, and the unit is
# split into single-polarity parts (Interactive's campaign disposition and split key).
POOL_ION_MODES = ("Positive", "Negative", "Both")
_POOL_ION_MODES_SQL = ", ".join(f"'{mode}'" for mode in POOL_ION_MODES)
_POOL_WHERE = {
    "declared": (
        "u.separation = 'LC-MS' AND u.acquisition_mode IN ('DDA', 'DIA', 'AIF', 'SWATH') "
        "AND (u.untargeted IS NULL OR u.untargeted <> 0) AND u.ion_mode IN (" + _POOL_ION_MODES_SQL + ")"
    ),
    "acquisition_unknown": (
        "u.separation = 'LC-MS' AND u.acquisition_mode = 'Unknown' "
        "AND (u.untargeted IS NULL OR u.untargeted <> 0) AND u.ion_mode IN (" + _POOL_ION_MODES_SQL + ")"
    ),
}
_POLARITY_RULE = ("one polarity, or Both (each file's polarity read from its raw header, the unit split into "
                  "single-polarity parts, a file whose scans switch polarity excluded)")
SELECTION_RULES = {
    "declared": f"separation LC-MS; acquisition DDA, DIA, AIF or SWATH; untargeted not false; {_POLARITY_RULE}",
    "acquisition_unknown": ("separation LC-MS; acquisition Unknown (read from the raw headers); untargeted not false; "
                            f"{_POLARITY_RULE}"),
}
SELECTION_RULES[PILOT] = "the analysis units named with --units, each held to the rules of the pool it is in"
EXCLUSION_REASONS = (
    "no_files", "ion_mobility", "fragmentation_not_cid_hcd", "preexisting_workspace", "campaign_workspace",
    "mzdata_only", "no_download_object", "class_undecided",
)
PILOT_EXCLUSION_REASONS = ("not_in_pool",) + EXCLUSION_REASONS
# What an accession-level workspace of an earlier run keeps in the accession folder itself (Interactive's
# layout before analysis units: <accession>\\raw, provenance and output).
ACCESSION_WORKSPACE_PARTS = ("raw", "provenance", "output")
# A unit a prior campaign ended in one of these, or has a job running for, is not planned again.
NOT_REPLANNED = frozenset({"done", "split_done", "downloading", "diagnosing", "running"})
ION_MOBILITY_INSTRUMENT = re.compile(r"tims|synapt|vion|6560|cyclic", re.IGNORECASE)
# Vendor containers that cannot hold an ion-mobility separation, as the Catalog names an input's format:
# Bruker BAF, and TSF (a timsTOF's spectra with TIMS off). Waters .raw and Agilent .d can hold either.
NON_ION_MOBILITY_FORMATS = frozenset({"bruker_baf", "bruker_tsf"})
# Where the Catalog's ion_mobility_evidence(unit) lives (Catalog fix/ion-mobility-from-unit-evidence); the plan
# uses it where it imports, and the fallback rule without it.
ION_MOBILITY_EVIDENCE_MODULES = ("msdial_repository_catalog.ion_mobility",)
# A source of that helper's evidence is the unit's own when it names the unit, its rows (row_instrument),
# its assay (assay_parameter), its samples, its files, inputs or containers (container_format), its
# instrument or a parameter; study_text, or a source that names nothing, is not.
_UNIT_LEVELS = ("unit", "row", "assay", "sample", "file", "input", "container", "instrument", "parameter")
# Look the helper up (the default) rather than be given one or told there is none (None).
DETECT = object()
MZDATA_SUFFIXES = (".mzdata", ".mzdata.xml")
ANALYSIS_ROLES = ("raw", "converted")
ARCHIVE_KINDS = frozenset({"archive", "bundle"})
# A drive path, a UNC path or a file:// URI: none belongs in a manifest a person approves and Interactive
# copies into every unit's provenance.
_ABSOLUTE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|\\\\[^\\\s]|file://", re.IGNORECASE)
# Catalog 0.6.1 prefers a convertible mzXML to an unreadable twin and reads the empty digest as a known 0:
# both change which files a unit holds and so its Class digest, which the runner compares before saving.
MINIMUM_CATALOG_VERSION = (0, 6, 1)


class PlanError(ValueError):
    """The plan cannot be made as asked; nothing was written."""


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", str(text or ""))[:3])


def canonical_bytes(manifest: Mapping[str, Any]) -> bytes:
    return policy.canonical_json(manifest)


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---- selection and exclusions --------------------------------------------------------------------------

_UNIT_COLUMNS = (
    "unit_id", "repository", "accession", "instrument", "ion_mode", "acquisition_mode", "ion_mobility", "untargeted",
    "target_omics", "chromatography", "separation", "file_count",
)
_UNIT_SELECT = (
    "SELECT u.unit_id, s.repository, s.accession, u.instrument, u.ion_mode, u.acquisition_mode, "
    "u.ion_mobility, u.untargeted, u.target_omics, u.chromatography, u.separation, "
    "(SELECT COUNT(*) FROM raw_file r WHERE r.unit_id = u.unit_id) AS file_count "
    "FROM analysis_unit u JOIN study s ON s.study_id = u.study_id"
)


def _only(unit_ids: Sequence[str] | None) -> tuple[str, tuple[str, ...]]:
    if unit_ids is None:
        return "", ()
    names = tuple(str(item) for item in unit_ids)
    return f" AND u.unit_id IN ({', '.join('?' for _ in names) or 'NULL'})", names


def select_units(
    connection: sqlite3.Connection, pool: str, unit_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Every unit of the pool, or those of `unit_ids` the pool's rules select, with what the exclusions read
    and the pool they were selected under. Read-only."""
    if pool not in _POOL_WHERE:
        raise PlanError(f"Unknown pool {pool!r}; choose one of {', '.join(POOLS)}.")
    only, names = _only(unit_ids)
    rows = connection.execute(
        f"{_UNIT_SELECT} WHERE {_POOL_WHERE[pool]}{only} ORDER BY s.repository, s.accession, u.unit_id", names,
    ).fetchall()
    units = [{**dict(zip(_UNIT_COLUMNS, tuple(row))), "pool": pool} for row in rows]
    suffixes: dict[str, list[str]] = {}
    for unit_id, path in connection.execute(
        f"SELECT r.unit_id, r.path FROM raw_file r JOIN analysis_unit u ON u.unit_id = r.unit_id "
        f"JOIN study s ON s.study_id = u.study_id WHERE {_POOL_WHERE[pool]}{only} "
        f"AND r.role IN ({', '.join('?' for _ in ANALYSIS_ROLES)})",
        names + ANALYSIS_ROLES,
    ):
        suffixes.setdefault(str(unit_id), []).append(str(path).casefold().rstrip("/\\"))
    for unit in units:
        unit["analysis_paths"] = suffixes.get(unit["unit_id"], [])
    return units


def select_named_units(connection: sqlite3.Connection, unit_ids: Sequence[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A pilot's units: (those a pool selects, each under its pool's rules; those in neither pool).

    Raises PlanError for a name the Catalog does not know, so a mistyped id refuses the plan instead of
    shrinking it. Read-only."""
    requested = list(dict.fromkeys(str(item).strip() for item in unit_ids if str(item).strip()))
    if not requested:
        raise PlanError("--units names no analysis unit.")
    selected = [unit for pool in POOLS for unit in select_units(connection, pool, requested)]
    found = {unit["unit_id"] for unit in selected}
    missing = [name for name in requested if name not in found]
    only, names = _only(missing)
    outside = [
        {**dict(zip(_UNIT_COLUMNS, tuple(row))), "pool": None, "analysis_paths": []}
        for row in connection.execute(f"{_UNIT_SELECT} WHERE 1 = 1{only} ORDER BY s.repository, s.accession, u.unit_id", names)
    ] if missing else []
    unknown = sorted(set(missing) - {unit["unit_id"] for unit in outside})
    if unknown:
        raise PlanError(f"The Catalog has no analysis unit {', '.join(unknown[:10])}{' ...' if len(unknown) > 10 else ''}.")
    return selected, outside


def read_unit_list(value: str) -> list[str]:
    """The units of `plan --units`: a file that holds a JSON list of ids (or an object whose "units" or
    "unit_ids" is one), a file of ids separated by commas, blanks or lines ('#' starts a comment), or a
    comma-separated list on the command line."""
    text = str(value or "").strip()
    path = Path(text)
    if text and path.is_file():
        content = path.read_text(encoding="utf-8-sig")
        try:
            parsed = json.loads(content)
        except ValueError:
            tokens = re.split(r"[\s,]+", "\n".join(line.split("#", 1)[0] for line in content.splitlines()))
        else:
            if isinstance(parsed, Mapping):
                parsed = parsed.get("units", parsed.get("unit_ids"))
            if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
                raise PlanError(f"{path.name} is JSON but not a list of analysis unit ids.")
            tokens = parsed
    else:
        tokens = text.split(",")
    names = list(dict.fromkeys(token.strip() for token in tokens if token.strip()))
    if not names:
        raise PlanError("--units names no analysis unit.")
    return names


def campaign_of(workspace: Path) -> str | None:
    """The campaign that made a unit workspace, or None.

    The runner copies the campaign's authorization into <workspace>\\provenance before anything else is
    written there (the Class step), and the parts of a split get theirs as they are made.
    """
    try:
        record = json.loads((workspace / "provenance" / "campaign-authorization.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if isinstance(record, dict) and record.get("schema") == AUTHORIZATION_SCHEMA and str(record.get("campaign_id") or "").strip():
        return str(record["campaign_id"])
    return None


def replan_states(workspace_root: Path, campaign_ids: Iterable[str]) -> dict[str, dict[str, str]]:
    """{campaign id: {unit key: state}} of the prior campaigns whose unfinished units may be planned again.

    Only a campaign whose approval is revoked: a live one's units are its own, and its runner would take
    them up again beside the new campaign's. Read-only.
    """
    result: dict[str, dict[str, str]] = {}
    for campaign_id in campaign_ids:
        path = Path(workspace_root) / "_campaigns" / str(campaign_id) / "ledger.sqlite"
        if not path.is_file():
            raise PlanError(f"Campaign {campaign_id} has no ledger at {path}.")
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            approval = connection.execute("SELECT approval_id, revoked_at FROM approval").fetchone()
            if approval is None or approval[1] is None:
                raise PlanError(
                    f"Campaign {campaign_id}'s approval is not revoked; revoke it before its units are planned again."
                )
            result[str(campaign_id)] = {str(key): str(state) for key, state in connection.execute("SELECT unit_key, state FROM unit")}
        finally:
            connection.close()
    return result


def workspace_root_cautions(workspace_root: str | Path, environment: Mapping[str, str] | None = None) -> list[str]:
    """Where the workspace root lies that the project keeps no campaign data (C:, the user profile, a temporary
    folder), as the summary a person approves says it (review 2). The runner refuses a relative root, a network or
    device path and one inside a code checkout (campaign-runner.py, resolve_workspace_root), but not these: its tests
    plan in temporary folders, and an approval may name such a root knowingly, so the words say it instead.
    setup-windows.ps1 refuses them only for the root it is given, which no campaign reads."""
    environment = os.environ if environment is None else environment
    text = str(workspace_root or "").strip()
    if not text:
        return []

    def spelled(value: str) -> str:
        # Long names and one case, so a folder spelled with an 8.3 name or in another case is the same folder.
        return os.path.normcase(os.path.realpath(value))

    root = spelled(text)

    def inside(folder: str) -> bool:
        base = spelled(folder).rstrip("\\/")
        return bool(base) and (root == base or root.startswith(base + os.sep))

    cautions = []
    drive = PureWindowsPath(str(environment.get("SystemDrive") or "").strip()).drive
    if drive and PureWindowsPath(root).drive.casefold() == drive.casefold():
        cautions.append(f"on the system drive {drive}")
    profile = str(environment.get("USERPROFILE") or "").strip()
    if profile and inside(profile):
        cautions.append("inside the user profile")
    if any(inside(folder) for folder in {str(environment.get(name) or "").strip() for name in ("TEMP", "TMP")} if folder):
        cautions.append("inside a temporary folder")
    return cautions


def legacy_accession_workspace(workspace_root: Path, repository: str, accession: str) -> bool:
    """Whether the accession folder is itself the workspace of an earlier accession-level run. Read-only."""
    folder = Path(workspace_root) / str(repository) / str(accession)
    return any((folder / name).is_dir() for name in ACCESSION_WORKSPACE_PARTS)


def workspace_exclusion(
    unit: Mapping[str, Any], workspace_root: Path, replan: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[str | None, str]:
    """preexisting_workspace, campaign_workspace or None for this unit's workspace, with what was found.

    The unit's own workspace and its split parts' (<unit>-<part>): a run of the unit would make them
    again, and a part's id is its parent's with the part's key appended.
    """
    accession = workspace_root / str(unit["repository"]) / str(unit["accession"])
    if not accession.is_dir():
        return None, ""
    unit_id = str(unit["unit_id"])
    foreign = sorted(
        entry.name for entry in accession.iterdir()
        if entry.is_dir() and (entry.name == unit_id or entry.name.startswith(unit_id + "-")) and not campaign_of(entry)
    )
    if foreign:
        return "preexisting_workspace", f"{accession.name} holds {', '.join(foreign[:5])}, made by no campaign"
    own = accession / unit_id
    made_by = campaign_of(own) if own.is_dir() else None
    if made_by is None:
        return None, ""
    state = (replan or {}).get(made_by, {}).get(str(unit["unit_id"]))
    if state is not None and state not in NOT_REPLANNED:
        return None, ""
    if made_by not in (replan or {}):
        return "campaign_workspace", f"made by campaign {made_by}"
    return "campaign_workspace", f"made by campaign {made_by}, where the unit is {state or 'unknown'}"


# ---- ion mobility (option A, 2026-10-03) ------------------------------------------------------------------

def catalog_ion_mobility_evidence() -> Callable[[Mapping[str, Any]], Any] | None:
    """The Catalog's ion_mobility_evidence(unit) where this Catalog has it, else None (the fallback rule)."""
    for name in ION_MOBILITY_EVIDENCE_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        helper = getattr(module, "ion_mobility_evidence", None)
        if callable(helper):
            return helper
    return None


def _clip(value: Any, limit: int = 100) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _unit_level(source: Any) -> bool:
    if isinstance(source, Mapping):
        if isinstance(source.get("unit_level"), bool):
            return source["unit_level"]
        level = source.get("level") or source.get("scope") or ""
    else:
        level = str(source or "").split(":", 1)[0]
    level = str(level).strip().casefold()
    return bool(level) and not level.startswith("study") and level.startswith(_UNIT_LEVELS)


def _describe_source(source: Any) -> str:
    if not isinstance(source, Mapping):
        return _clip(source)
    level = source.get("level") or source.get("scope") or ""
    field = source.get("field") or source.get("source_field") or source.get("name") or ""
    value = source.get("value") or source.get("text") or source.get("source_value") or ""
    return _clip(" ".join(str(part) for part in (level, f"{field}:" if field else "", value) if part))


def _read_evidence(result: Any) -> dict[str, Any] | None:
    """The helper's answer as the plan reads it, or None where it cannot be read:
    {"state": "enabled" | "mixed" | "none" | "unknown", "sources": ["row_instrument", "assay_parameter",
    "container_format", "study_text", ...], "reason": "..."}; a source may also be {"level": ..., "field": ...,
    "value": ...}."""
    if not isinstance(result, Mapping) or not str(result.get("state") or "").strip():
        return None
    sources = result.get("sources", result.get("evidence")) or []
    if isinstance(sources, (str, Mapping)):
        sources = [sources]
    if not isinstance(sources, (list, tuple)):
        return None
    return {"state": str(result["state"]).strip().casefold(), "sources": list(sources),
            "unit_sources": [item for item in sources if _unit_level(item)],
            "reason": str(result.get("reason") or "") if isinstance(result.get("reason"), str) else ""}


def _fallback_ion_mobility(record: Mapping[str, Any]) -> dict[str, Any]:
    """Without the Catalog's helper: excluded only when the unit's instrument, or a row's instrument field,
    names an ion-mobility instrument and none of its inputs is a container that cannot hold ion mobility."""
    named: dict[tuple[str, str], int] = {}
    if ION_MOBILITY_INSTRUMENT.search(str(record.get("instrument") or "")):
        named[("instrument", str(record["instrument"]))] = 1
    for sample in record.get("samples") or []:
        for field, value in (sample.get("attributes") or {}).items():
            if "instrument" in str(field).casefold() and ION_MOBILITY_INSTRUMENT.search(str(value)):
                named[(str(field), str(value))] = named.get((str(field), str(value)), 0) + 1
    formats: dict[str, int] = {}
    for item in record.get("analysis_inputs") or []:
        for part in str(item.get("format") or "").split("+"):
            if part:
                formats[part] = formats.get(part, 0) + 1
    beside = any(name in NON_ION_MOBILITY_FORMATS for name in formats)
    column = str(record.get("ion_mobility") or "")
    if named:
        (field, value), rows = next(iter(named.items()))
        said = f"{field}{f' ({rows} rows)' if rows > 1 else ''} names {_clip(value, 80)!r}"
        if not beside:
            return {"excluded": True, "signal": True, "state": "enabled",
                    "detail": f"{said}, and none of its inputs is a Bruker BAF or TSF container"}
        containers = ", ".join(f"{name} {count}" for name, count in formats.items())
        return {"excluded": False, "signal": True, "state": "mixed",
                "detail": f"{said}, beside inputs that cannot hold ion mobility ({containers}): Interactive's "
                          "header check and split exclude the ion-mobility files or parts"}
    if column in ("Enabled", "Mixed"):
        return {"excluded": False, "signal": True, "state": "unknown",
                "detail": f"the Catalog's ion_mobility is {column}, but neither the unit's instrument nor a row's "
                          "instrument field names an ion-mobility instrument"}
    return {"excluded": False, "signal": False, "state": "unknown", "detail": ""}


def unread_ion_mobility(problem: str) -> dict[str, Any]:
    """The reading of a unit whose Catalog record could not be read (Catalog.get_unit raised): never excluded for
    ion mobility. The plan's own row holds neither the unit's sample rows nor its inputs, so read alone it would
    take a timsTOF named beside Bruker BAF folders (MTBKS219, MTBKS220) for ion mobility alone, which option A
    forbids at plan time. The unit passes to Interactive's header check, and the manifest says why (`signal`)."""
    return {"excluded": False, "evidence": "not_read", "state": "unknown", "signal": True,
            "detail": f"not read: the Catalog's record of the unit could not be read ({_clip(problem, 160)}), so it is "
                      "not excluded for ion mobility on the plan's own row; Interactive's header check decides each file"}


def ion_mobility_reading(record: Mapping[str, Any], evidence: Callable[[Mapping[str, Any]], Any] | None) -> dict[str, Any]:
    """Whether the unit is excluded for ion mobility, read from its own evidence (option A, 2026-10-03).

    `record` is the Catalog's unit (Catalog.get_unit); where that could not be read the plan takes
    unread_ion_mobility() instead, never its own row. With the Catalog's ion_mobility_evidence, only its state
    "enabled" from unit-level sources excludes; an answer the plan cannot read falls back to the rule without
    it, and says so. Returns {"excluded", "evidence" ("catalog", "fallback" or, from unread_ion_mobility,
    "not_read"), "state", "signal" (anything named ion mobility at all, or nothing read), "detail"}."""
    if evidence is not None:
        try:
            read = _read_evidence(evidence(record))
            problem = "" if read is not None else "an answer of another shape"
        except Exception as error:  # noqa: BLE001 - an answer the plan cannot read is the fallback's to decide
            read, problem = None, f"{type(error).__name__}: {error}"
        if read is not None:
            excluded = read["state"] == "enabled" and bool(read["unit_sources"])
            shown = read["unit_sources"] if excluded else read["sources"]
            said = "; ".join(_describe_source(item) for item in shown[:3])
            if excluded:
                detail = f"enabled from the unit's own evidence ({said})"
            elif read["state"] == "enabled":
                detail = f"enabled from the study's text only ({said}), which is not the unit's evidence"
            elif read["state"] in ("mixed", "unknown"):
                detail = f"{read['state']}{f' ({said})' if said else ''}: Interactive's header check decides each file"
            else:
                detail = f"{read['state']}{f' ({said})' if said else ''}"
            if read["reason"]:
                detail += f"; {_clip(read['reason'], 240)}"
            signal = excluded or read["state"] in ("enabled", "mixed") or str(record.get("ion_mobility") or "") in ("Enabled", "Mixed")
            return {"excluded": excluded, "evidence": "catalog", "state": read["state"], "signal": signal,
                    "detail": f"the Catalog's ion_mobility_evidence: {detail}"}
        fallback = _fallback_ion_mobility(record)
        return {**fallback, "evidence": "fallback", "signal": True,
                "detail": f"the Catalog's ion_mobility_evidence could not be read ({_clip(problem)}); "
                          f"{fallback['detail'] or 'nothing of the unit names ion mobility'}"}
    return {**_fallback_ion_mobility(record), "evidence": "fallback"}


def exclusion_reasons(
    unit: Mapping[str, Any], workspace_root: Path, replan: Mapping[str, Mapping[str, str]] | None = None,
    ion_mobility: Mapping[str, Any] | None = None, fragmentation: Mapping[str, Any] | None = None,
) -> list[str]:
    """Every static reason this unit will not run, in EXCLUSION_REASONS order.

    `ion_mobility` is ion_mobility_reading() of the unit's Catalog record; without it the unit is not excluded
    for ion mobility, since the plan's row holds neither its sample rows nor its inputs (unread_ion_mobility).
    `fragmentation` is fragmentation_reading() of the same record; without it the unit is not excluded for its
    fragmentation method (absence of evidence means run)."""
    reasons = []
    if not unit["file_count"]:
        reasons.append("no_files")
    if ion_mobility is not None and ion_mobility["excluded"]:
        reasons.append("ion_mobility")
    if fragmentation is not None and fragmentation["excluded"]:
        reasons.append(NOT_CID_HCD)
    workspace, _detail = workspace_exclusion(unit, workspace_root, replan)
    if workspace:
        reasons.append(workspace)
    paths = unit.get("analysis_paths") or []
    if paths and all(path.endswith(MZDATA_SUFFIXES) for path in paths):
        reasons.append("mzdata_only")
    return reasons


# ---- CID/HCD only (2026-10-29) -------------------------------------------------------------------------------

NOT_CID_HCD = "fragmentation_not_cid_hcd"
CID_HCD_ONLY_RULE = "cid_hcd_only_2026_10_29"
# The Interactive that keeps a unit's files out by their names (msdial_app.fragmentation): a unit planned with some of
# its files named so is refused to an earlier one, which would run them.
CID_HCD_ONLY_INTERACTIVE = (0, 5, 52)
# The methods a name may give, as Interactive's fragmentation.NON_CID_METHODS spells them: a whole token, no letter
# or digit before it and no letter after it (EAD14eV names EAD), and never inside a run of eight or more hexadecimal
# digits (a checksum, a GUID segment such as 8ead4b2c). CID, HCD, CAD and PQD are collisional and name nothing.
NON_CID_METHODS = ("EAD", "OAD", "ECD", "ETD", "EThcD", "ETciD", "NETD", "EDD", "EID", "UVPD", "IRMPD")
_CANONICAL_METHOD = {name.casefold(): name for name in NON_CID_METHODS}
_METHOD_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(sorted(NON_CID_METHODS, key=len, reverse=True)) + r")(?![A-Za-z])", re.IGNORECASE)
# Spelled-out names, in repository text only (a file name abbreviates).
_METHOD_WORDS = re.compile(
    r"electron[- ]activated dissociation|oxygen[- ]attachment dissociation|electron[- ]capture dissociation|"
    r"electron[- ]transfer dissociation|electron[- ]induced dissociation|ultraviolet photodissociation|"
    r"infrared multiphoton dissociation", re.IGNORECASE)
_WORD_METHOD = {"electron activated": "EAD", "oxygen attachment": "OAD", "electron capture": "ECD",
                "electron transfer": "ETD", "electron induced": "EID", "ultraviolet photodissociation": "UVPD",
                "infrared multiphoton": "IRMPD"}
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")
_HEX_RUN = re.compile(r"[0-9A-Fa-f]{8,}")
# A sample-attribute or evidence field that is the study's text, never the unit's (the study's keywords, title,
# description, abstract, summary, publication).
STUDY_TEXT_FIELD = re.compile(r"keyword|title|description|abstract|summary|publication", re.IGNORECASE)


def methods_named(text: Any, words: bool = False) -> list[str]:
    """The non-CID/HCD methods a text names by a whole token (and, with `words`, by a spelled-out name), in order and
    once each; [] for none."""
    value = str(text or "")
    found: list[str] = []
    for match in _METHOD_TOKEN.finditer(value):
        run = next((item for item in _ALNUM_RUN.finditer(value) if item.start() <= match.start() < item.end()), None)
        if run is not None and _HEX_RUN.fullmatch(run.group(0)):
            continue
        name = _CANONICAL_METHOD[match.group(1).casefold()]
        if name not in found:
            found.append(name)
    if words:
        for match in _METHOD_WORDS.finditer(value):
            spelled = re.sub(r"[- ]+", " ", match.group(0).casefold())
            name = next(code for start, code in _WORD_METHOD.items() if spelled.startswith(start))
            if name not in found:
                found.append(name)
    return found


def file_name_methods(path: Any) -> list[str]:
    """methods_named over a path's base name (a vendor folder's own name)."""
    text = str(path or "").rstrip("\\/")
    return methods_named(re.split(r"[\\/]", text)[-1] if text else "")


def study_shared_attributes(connection: sqlite3.Connection, study_id: str) -> set[tuple[str, str]]:
    """The (field, value) sample attributes every unit of a study of several units carries: the study's text copied
    into each unit's rows (MPST000010's keywords "CID, OAD, lipidomics, ..."), never one unit's. Read-only."""
    per_unit: dict[str, set[tuple[str, str]]] = {}
    for unit_id, field, value in connection.execute(
        "SELECT sm.unit_id, a.field_name, a.raw_value FROM sample_attribute a JOIN sample sm ON sm.sample_pk = a.sample_pk "
        "JOIN analysis_unit u ON u.unit_id = sm.unit_id WHERE u.study_id = ?", (str(study_id),),
    ):
        per_unit.setdefault(str(unit_id), set()).add((str(field), str(value)))
    units = connection.execute("SELECT COUNT(*) FROM analysis_unit WHERE study_id = ?", (str(study_id),)).fetchone()[0]
    if int(units or 0) < 2 or len(per_unit) < int(units):
        return set()
    return set.intersection(*per_unit.values())


def unit_evidence_rows(connection: sqlite3.Connection, unit_id: str) -> list[tuple[str, str, str]]:
    """The unit's analysis_unit_evidence rows (source, field, value). Read-only."""
    try:
        return [(str(source or ""), str(field or ""), str(value or "")) for source, field, value in connection.execute(
            "SELECT source, source_field, source_value FROM analysis_unit_evidence WHERE unit_id = ?", (str(unit_id),))]
    except sqlite3.Error:
        return []


def unread_fragmentation(problem: str) -> dict[str, Any]:
    """The reading of a unit whose Catalog record could not be read: never excluded for its fragmentation method."""
    return {"excluded": False, "evidence": "not_read", "signal": False, "methods": [], "named_files": [],
            "analysis_files": 0, "named_samples": 0, "samples": 0, "sources": [],
            "detail": f"not read: the Catalog's record of the unit could not be read ({_clip(problem, 160)}), so it is "
                      "not excluded for its fragmentation method; Interactive's lease reads each file's name"}


def fragmentation_reading(
    record: Mapping[str, Any], evidence_rows: Iterable[tuple[str, str, str]] = (),
    study_shared: Iterable[tuple[str, str]] | Callable[[], Iterable[tuple[str, str]]] = (),
) -> dict[str, Any]:
    """Whether the unit is excluded for its fragmentation method, read from its own evidence (CID/HCD only, 2026-10-29).

    The unit's label, its instrument and its analysis_unit_evidence values (`evidence_rows`, a study-text field
    aside) speak for the whole unit; a sample row's attributes for that row, and a file's name for that file. The
    unit is excluded where the first name a method, or every sample row does, or every analysis file's name does.
    An attribute in `study_shared` (study_shared_attributes, read only where a row names a method) or of a
    study-text field is the study's text, and decides nothing. Returns {"excluded", "evidence" ("catalog"),
    "signal" (anything of the unit named a method), "methods", "sources", "named_files" (the analysis files whose
    names name one, where not all do), "analysis_files", "named_samples", "samples", "detail"}."""
    unit_sources: list[str] = []
    methods: list[str] = []

    def note(found: list[str]) -> None:
        for name in found:
            if name not in methods:
                methods.append(name)

    for field, value in (("label", record.get("label")), ("instrument", record.get("instrument"))):
        found = methods_named(value, words=True)
        if found:
            note(found)
            unit_sources.append(f"{field}: {_clip(value, 80)}")
    for _source, field, value in evidence_rows:
        if STUDY_TEXT_FIELD.search(field):
            continue
        found = methods_named(value, words=True)
        if found:
            note(found)
            unit_sources.append(f"evidence {field}: {_clip(value, 80)}")
    samples = [item for item in record.get("samples") or [] if isinstance(item, Mapping)]
    shared: set[tuple[str, str]] | None = None
    named_samples = 0
    sample_sources: list[str] = []
    for sample in samples:
        hits = []
        for field, value in (sample.get("attributes") or {}).items():
            if STUDY_TEXT_FIELD.search(str(field)):
                continue
            found = methods_named(value, words=True)
            if not found:
                continue
            if shared is None:
                shared = set(study_shared() if callable(study_shared) else study_shared)
            if (str(field), str(value)) in shared:
                continue
            hits.append((str(field), str(value), found))
        if hits:
            named_samples += 1
            for field, value, found in hits:
                note(found)
                said = f"row {field}: {_clip(value, 80)}"
                if said not in sample_sources:
                    sample_sources.append(said)
    inputs = [str(item.get("path") or "") for item in record.get("analysis_inputs") or [] if isinstance(item, Mapping)]
    if not inputs:
        inputs = [str(item.get("path") or "") for item in record.get("files") or []
                  if isinstance(item, Mapping) and str(item.get("role") or "") in ANALYSIS_ROLES]
    inputs = [item for item in inputs if item.strip()]
    named_files = [item for item in inputs if file_name_methods(item)]
    for item in named_files:
        note(file_name_methods(item))
    every_sample = bool(samples) and named_samples == len(samples)
    every_file = bool(inputs) and len(named_files) == len(inputs)
    excluded = bool(unit_sources) or every_sample or every_file
    parts = []
    if unit_sources:
        parts.append("the unit's own record names it (" + "; ".join(unit_sources[:3]) + ")")
    if named_samples:
        parts.append(f"{named_samples} of its {len(samples)} sample row(s) name it ("
                     + "; ".join(sample_sources[:2]) + ")")
    if named_files:
        parts.append(f"{len(named_files)} of its {len(inputs)} analysis file(s) are named so ("
                     + ", ".join(Path(item.replace("\\", "/")).name for item in named_files[:3])
                     + (", ..." if len(named_files) > 3 else "") + ")")
    detail = ""
    if methods:
        detail = (f"{', '.join(methods)}: " + "; ".join(parts) + (
            ". Every reference spectrum is CID/HCD-derived (user decision, 2026-10-29)" if excluded else
            ". Not the whole unit: the files named so are kept out by Interactive's lease, and the rest runs"))
    return {
        "excluded": excluded, "evidence": "catalog", "signal": bool(methods), "methods": methods,
        "sources": unit_sources + sample_sources, "named_files": [] if excluded else named_files,
        "analysis_files": len(inputs), "named_samples": named_samples, "samples": len(samples), "detail": detail,
    }


# ---- the profile ----------------------------------------------------------------------------------------

def library_references(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value[len("library:"):]} if value.startswith("library:") else set()
    if isinstance(value, Mapping):
        return set().union(*(library_references(item) for item in value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(library_references(item) for item in value)) if value else set()
    return set()


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def profile_shape_problems(profile: Mapping[str, Any]) -> list[str]:
    """An ion mode's answers, or a workflow_overrides, that is present but not an object.

    The gate reads these only as objects (what it states and refuses about the RT corrections walks objects),
    but the runner does not: Runner.answers merges an ion mode's answers with dict(...) and then takes
    dict(answers.get("workflow_overrides") or {}). So a list of [key, value] pairs there would run settings the
    statement never names and no refusal sees, and a null (or any other non-object) for an ion mode's
    workflow_overrides would replace the answers' workflow_overrides for that mode's units, which would then run
    otherwise than stated. Neither has a meaning the gate states, so both are refused, null included.
    """
    problems = []
    answers = profile.get("answers") if isinstance(profile.get("answers"), Mapping) else {}
    by_mode = profile.get("by_ion_mode") if isinstance(profile.get("by_ion_mode"), Mapping) else {}
    places: list[tuple[str, Mapping[str, Any]]] = [("answers", answers)]
    for mode in sorted(by_mode, key=str):
        if isinstance(by_mode[mode], Mapping):
            places.append((f"by_ion_mode.{mode}", by_mode[mode]))
        else:
            problems.append(f"by_ion_mode.{mode} is {_clip(json.dumps(by_mode[mode], default=str))}: write an object")
    for where, settings in places:
        if "workflow_overrides" in settings and not isinstance(settings["workflow_overrides"], Mapping):
            value = _clip(json.dumps(settings["workflow_overrides"], default=str))
            problems.append(f"{where}.workflow_overrides is {value}, which the runner would apply as the gate "
                            "cannot read it: write an object, or leave it out")
    return problems


def profile_problems(profile: Mapping[str, Any] | None, library_names: Iterable[str]) -> list[str]:
    """Why a profile cannot be approved, or nothing.

    A profile holds the answers every unit's run shares: the library strategy, the annotation settings.
    It names libraries as "library:<file name>" and nothing by location, since it is part of the
    manifest a person approves and of the record copied into every unit's provenance.
    """
    if profile is None:
        return ["the manifest has no profile: plan again with --profile"]
    problems = []
    if profile.get("schema") != PROFILE_SCHEMA:
        problems.append(f"the profile's schema is not {PROFILE_SCHEMA}")
    if not isinstance(profile.get("answers") or {}, Mapping) or not isinstance(profile.get("by_ion_mode") or {}, Mapping):
        problems.append("the profile's answers and by_ion_mode are objects")
    unknown_modes = sorted(set(profile.get("by_ion_mode") or {}) - {"Positive", "Negative"})
    if unknown_modes:
        problems.append(f"by_ion_mode names {', '.join(unknown_modes)}; only Positive and Negative")
    problems.extend(profile_shape_problems(profile))
    for text in _strings(profile):
        if _ABSOLUTE.search(text):
            problems.append("the profile names a location; name libraries as library:<file name>")
            break
    missing = sorted(library_references(profile) - set(library_names))
    if missing:
        problems.append(f"the profile names libraries the plan did not pin: {', '.join(missing)}")
    problems.extend(template_path_problems(profile))
    problems.extend(msp_kind_problems(profile))
    return problems


# ---- the libraries the profile names, and how the pinned Interactive annotates with them --------------------

# The role a library is loaded in, by its suffix, as Interactive names it (sharing._role_of).
LIBRARY_ROLES = {".lbm": "lbm", ".lbm2": "lbm", ".msp": "msp", ".msp2": "msp"}
# The library strategy for which the pinned Interactive applies its tiered annotation profile (LBM, then a strict
# and a broad MSP tier: annotation_pipeline.tiered_lcms_annotation, from agent_workflow._apply_libraries).
TIERED_STRATEGY = "tiered_lipid_msp"
# The LBM score cutoffs, by the name the summary gives each and the state key Interactive's method writer reads.
LBM_CUTOFF_KEYS = (("weighted", "lbm_weighted_dot_product"), ("simple", "lbm_simple_dot_product"),
                   ("reverse", "lbm_reverse_dot_product"))
# Interactive's own LC-MS parameter template, used where neither the answers nor its settings name another.
DEFAULT_TEMPLATE = "msdial_console_param4lipidomics.txt"
# How the summary says whose a parameter template is (lcms_parameter_template).
TEMPLATE_WHOSE = {"interactive": ", Interactive's own", "answers": ", which the answers name",
                  "settings": ", which Interactive's settings name"}
# The settings of an MSP annotator row the manifest states and the authorization carries to the gate's LIB-2, by the
# key Interactive's annotation profile gives each (annotation_pipeline.tiered_lcms_annotation), besides its id, its
# library (by file name) and its library kind. Each is stated only where the profile's row gives it: of the tiered
# profile's rows, only the in-silico one gives suggestions (False, the author's decision of 2026-10-25).
MSP_ROW_SETTINGS = ("priority", "rt_tolerance", "ms1_tolerance", "ms2_tolerance", "weighted_dot_product_cutoff",
                    "simple_dot_product_cutoff", "reverse_dot_product_cutoff", "matched_peaks_percentage_cutoff",
                    "minimum_spectrum_match", "use_rt_scoring", "use_rt_filtering", "target_omics", "suggestions")
# Where an ion mode's answers name its acquired MSP and its in-silico (predicted) MSP, and the parameter by which the
# pinned Interactive's tiered profile takes the in-silico one (Interactive 0.5.48:
# annotation_pipeline.tiered_lcms_annotation(..., in_silico_path=...), the four-tier profile).
ACQUIRED_MSP_KEY, IN_SILICO_MSP_KEY = "msp_paths", "in_silico_msp_paths"
IN_SILICO_PARAMETER = "in_silico_path"
# The in-silico library's declared preprocessing, and the step Interactive refuses for a positive-mode run (the
# negative-mode records' +1.008 artefact; agent_workflow._in_silico_preprocessing_problems).
IN_SILICO_PREPROCESSING_KEY = "in_silico_msp_preprocessing"
NEGATIVE_ONLY_PREPROCESSING = "precursor_plus_1008_peak_removed"
# The library kinds the Console reads as an in-silico (predicted) MSP; any other, a blank one included, is acquired.
PREDICTED_LIBRARY_KINDS = frozenset({"predicted", "insilico", "computed", "generated", "predictedmsp"})
# What an ion mode's annotation contract holds (annotation_contract): what the gate's LIB-2 holds the method to.
ANNOTATION_CONTRACT_KEYS = ("profile_id", "lbm", "lbm_cutoffs", "lbm_priority", "msp_rows")
# The parts of a tiered ion mode's contract that LIB-2 compares only where they are stated, so the plan must have read
# them (annotation_problems, review 3), with the words the refusal gives each.
CONTRACT_PART_WORDS = (("profile_id", "its annotation profile"), ("lbm_cutoffs", "its LBM cutoffs"),
                       ("msp_rows", "its MSP annotator rows"))
# The libraries the probe of the tiered strategy names (tiered_lbm_from_answers); none of them exists or is read.
PROBE_LBM, PROBE_OFFICIAL_LBM, PROBE_MSP = ("campaign-plan-probe.lbm2", "campaign-plan-probe-official.lbm2",
                                            "campaign-plan-probe.msp")


def contract_part_unread(key: str, value: Any) -> bool:
    """Whether a part of an ion mode's contract was not read: no profile id, LBM cutoffs that are not all three
    stated, or no MSP rows (an empty list states that there are none)."""
    if key == "lbm_cutoffs":
        return not isinstance(value, Mapping) or any(value.get(name) is None for name, _state in LBM_CUTOFF_KEYS)
    if key == "msp_rows":
        return not isinstance(value, list)
    return not value


def library_role(name: str) -> str:
    return LIBRARY_ROLES.get(Path(str(name)).suffix.casefold(), "text")


def library_kind(value: Any) -> str:
    """An MSP's library kind as the Console reads it: predicted for an in-silico library, else acquired."""
    return "predicted" if str(value or "").strip().casefold() in PREDICTED_LIBRARY_KINDS else "acquired"


def ion_mode_answers(profile: Mapping[str, Any] | None, mode: str) -> dict[str, Any]:
    """The profile's answers for a unit of one ion mode, merged as Runner.answers merges them. The answer seed
    Interactive gives each unit, which it merges first, names no library."""
    from .machine import _merge

    profile = profile or {}
    answers = profile.get("answers") if isinstance(profile.get("answers"), Mapping) else {}
    by_mode = profile.get("by_ion_mode") if isinstance(profile.get("by_ion_mode"), Mapping) else {}
    extra = by_mode.get(mode) if isinstance(by_mode.get(mode), Mapping) else {}
    return _merge(dict(answers), dict(extra))


def library_ion_modes(profile: Mapping[str, Any] | None) -> dict[str, list[str]]:
    """Each library the profile names, with the ion modes whose units are given it (none for one named only outside
    the answers a unit is given)."""
    modes: dict[str, list[str]] = {name: [] for name in sorted(library_references(profile))}
    for mode in ION_MODES:
        for name in sorted(library_references(ion_mode_answers(profile, mode))):
            modes.setdefault(name, []).append(mode)
    return modes


def split_libraries(profile: Mapping[str, Any] | None, names: Iterable[str]) -> tuple[list[str], list[str]]:
    """The resource map's libraries the plan pins, which are the ones the profile names as library:<file name>, and
    the ones it leaves unpinned. Pinning every mapped library would approve, with the rest, a library no unit is to
    load, such as the official LBM2 the profile replaced while the map still locates it."""
    named = library_references(profile)
    names = sorted({str(name) for name in names})
    return [name for name in names if name in named], [name for name in names if name not in named]


def msp_library_kinds(profile: Mapping[str, Any] | None) -> dict[str, set[str]]:
    """The library kind each MSP the profile names is searched as, by file name: predicted where an ion mode's answers
    name it in libraries.in_silico_msp_paths (the in-silico tier), acquired where they name it in libraries.msp_paths.
    A name given both has both (msp_kind_problems)."""
    kinds: dict[str, set[str]] = {}
    for mode in ION_MODES:
        libraries = ion_mode_answers(profile, mode).get("libraries")
        libraries = libraries if isinstance(libraries, Mapping) else {}
        for key, kind in ((ACQUIRED_MSP_KEY, "acquired"), (IN_SILICO_MSP_KEY, "predicted")):
            for name in library_references(libraries.get(key)):
                kinds.setdefault(name, set()).add(kind)
    return kinds


def msp_kind_problems(profile: Mapping[str, Any] | None) -> list[str]:
    """An MSP the profile names both as an acquired library and as an in-silico one, for one ion mode (which
    Interactive's _in_silico_library_problems refuses too) or one each, since its pin can state one kind only; and an
    MSP named other than as library:<file name>."""
    both = sorted(name for name, kinds in msp_library_kinds(profile).items() if len(kinds) > 1)
    problems = [f"the profile names {name} both in libraries.{ACQUIRED_MSP_KEY} (acquired) and in "
                f"libraries.{IN_SILICO_MSP_KEY} (in-silico): name each MSP in one of them" for name in both]
    # An MSP named otherwise than as library:<file name> is neither pinned nor stated: the plan stated the three-tier
    # contract for an in-silico MSP named bare, while Interactive searched it as the fourth tier (review 1).
    for mode in ION_MODES:
        libraries = ion_mode_answers(profile, mode).get("libraries")
        libraries = libraries if isinstance(libraries, Mapping) else {}
        for key in (ACQUIRED_MSP_KEY, IN_SILICO_MSP_KEY):
            value = libraries.get(key)
            items = value if isinstance(value, (list, tuple)) else [value]
            if any(item not in (None, "") and _one_library(item) is None for item in items):
                problems.append(f"the answers of {mode} units name libraries.{key} other than as library:<file name>, "
                                "which the plan can neither pin nor state: name each as library:<file name>")
    return list(dict.fromkeys(problems))


def library_pins(identities: Iterable[Mapping[str, Any]], profile: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The pinned libraries' identities, each with the role it is loaded in and the ion modes whose units load it,
    which the authorization carries to each unit for the gate's LIB-2; an MSP the profile names as an acquired or an
    in-silico library also with that library kind (msp_library_kinds), so LIB-2 and the run records hold an in-silico
    library to being searched as predicted (2026-10-28)."""
    modes = library_ion_modes(profile)
    kinds = msp_library_kinds(profile)
    pins = []
    for item in identities:
        pin = {**dict(item), "role": library_role(item["name"]), "ion_modes": list(modes.get(item["name"], []))}
        kind = kinds.get(item["name"]) or set()
        if pin["role"] == "msp" and len(kind) == 1:
            pin["library_kind"] = next(iter(kind))
        pins.append(pin)
    return pins


def lcms_parameter_template(agent_workflow: Any, answer: Any = None) -> tuple[Path, str]:
    """The LC-MS parameter template the pinned Interactive's _workflow takes, and whose it is ("answers", "settings" or
    "interactive"): the answers' template_path, else the one its settings name now (load_user_settings), else its
    own; a path naming no file falls back to its own, as _existing_path does. `answer` is a location: a profile names
    its template as library:<file name>, which template_location resolves as Runner.answers does. The plan reads the
    template's LBM cutoffs from it. The pins hold the one a unit whose answers name none takes by its sha256
    (ports.PinReader.template), and one the answers name as a library, so each is held where it is read (review 3:
    the template pin held the settings' one for a profile whose answers named another)."""
    settings = agent_workflow.load_user_settings()
    default = Path(agent_workflow.RESOURCES) / DEFAULT_TEMPLATE
    template = agent_workflow._existing_path(answer or settings.get("template_path"), default)
    return template, ("interactive" if template == default.resolve() else "answers" if answer else "settings")


def _one_library(value: Any) -> str | None:
    """The file name a value names as library:<file name>, where it is exactly one such reference."""
    names = library_references(value)
    return next(iter(names)) if isinstance(value, str) and len(names) == 1 else None


def template_location(value: Any, library_paths: Mapping[str, str] | None) -> str | None:
    """Where the parameter template an ion mode's answers name as library:<file name> is on this machine, from the
    resource map, as Runner.answers resolves it before Interactive reads it; None for any other value, or a name the
    map does not locate. Read unresolved, "library:x.txt" names no file, and the plan stated Interactive's own
    template's cutoffs under the answers' name while the run read the library's (review 3)."""
    name = _one_library(value)
    location = (library_paths or {}).get(name) if name else None
    return str(location) if location else None


def answered_templates(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    """The template_path each ion mode's answers give, for the modes whose answers give one."""
    found = {mode: ion_mode_answers(profile, mode).get("template_path") for mode in ION_MODES}
    return {mode: value for mode, value in found.items() if value not in (None, "")}


def template_path_problems(profile: Mapping[str, Any] | None) -> list[str]:
    """An ion mode whose answers name a parameter template other than as library:<file name>. No pin holds such a
    template, and a relative path resolves against the backend's working directory, so the plan could neither read
    its cutoffs as the run does nor see it change (review 3)."""
    modes = [mode for mode, value in answered_templates(profile).items() if _one_library(value) is None]
    if not modes:
        return []
    return [f"the answers of {' and '.join(modes)} units name a parameter template (template_path) other than as "
            "library:<file name>, which no pin holds: name it as library:<file name>, which the plan pins as a "
            "library, or leave it out"]


def interactive_annotation() -> dict[str, Any]:
    """What the Interactive on the path (the plan's pinned one) does in annotation, read from its code, never
    restated here: its tiered annotation profile (tiered), the LBM score cutoffs it writes into the method where the
    profile sets none (lbm_cutoffs) and where they come from. An Interactive with agent_workflow.LBM_TEMPLATE_CUTOFFS
    carries the parameter template's cutoffs into the method; one without wrote its method writer's defaults
    (workflow.LBM_DOT_PRODUCT_CUTOFFS) over them. The template is the one Interactive's _workflow takes: the answers'
    template_path, else its settings', else its own. lbm_cutoffs are those of a unit whose answers name none, and
    template_cutoffs gives them for one whose answers name one (review of 2026-10-25: the plan read the settings'
    alone). tiered_lbm_from_answers says whether its tiered_lipid_msp strategy loads the LBM the answers name
    (tiered_lbm_from_answers()). retention_time_use is how its _workflow applies the answers' retention-time use to
    the annotator rows right after the library strategy (agent_workflow._apply_retention_time_use): it rewrites each
    MSP row's RT use and tolerance, which the tiered profile alone does not set (review 2). What its code does not
    have is None, and problems says why."""
    found: dict[str, Any] = {"tiered": None, "lbm_cutoffs": None, "lbm_cutoffs_source": "", "template_cutoffs": None,
                             "tiered_lbm_from_answers": None, "retention_time_use": None, "problems": []}
    try:
        from msdial_app import workflow
    except Exception as error:  # noqa: BLE001 - not read, so not stated; the summary says so
        found["problems"].append(f"msdial_app.workflow could not be imported ({type(error).__name__})")
        return found
    defaults = getattr(workflow, "LBM_DOT_PRODUCT_CUTOFFS", None)
    if isinstance(defaults, Mapping) and all(key in defaults for key, _state in LBM_CUTOFF_KEYS):
        found["lbm_cutoffs"] = {key: defaults[key] for key, _state in LBM_CUTOFF_KEYS}
        found["lbm_cutoffs_source"] = "Interactive's method writer (workflow.LBM_DOT_PRODUCT_CUTOFFS)"
    try:
        from msdial_app import agent_workflow

        carried = getattr(agent_workflow, "LBM_TEMPLATE_CUTOFFS", None)
        if isinstance(carried, Mapping):

            def template_cutoffs(answer: Any) -> tuple[dict[str, Any] | None, str]:
                # As agent_workflow._workflow takes an LC-MS template: the answers', else the settings', else its own.
                template, whose = lcms_parameter_template(agent_workflow, answer)
                lbm = workflow.load_parameter_template(template).get("lbm_annotator") or {}
                if not all(carried.get(state) in lbm for _key, state in LBM_CUTOFF_KEYS):
                    return None, ""
                return ({key: lbm[carried[state]] for key, state in LBM_CUTOFF_KEYS},
                        f"the parameter template {template.name}{TEMPLATE_WHOSE[whose]}")

            cutoffs, source = template_cutoffs(None)
            if cutoffs is not None:
                found["lbm_cutoffs"], found["lbm_cutoffs_source"] = cutoffs, source
            found["template_cutoffs"] = template_cutoffs
    except Exception as error:  # noqa: BLE001
        found["problems"].append(f"the parameter template's LBM cutoffs could not be read ({type(error).__name__})")
    try:
        from msdial_app.annotation_pipeline import tiered_lcms_annotation

        found["tiered"] = tiered_lcms_annotation
    except Exception as error:  # noqa: BLE001
        found["problems"].append(f"msdial_app.annotation_pipeline could not be imported ({type(error).__name__})")
    try:
        from msdial_app import agent_workflow

        found["tiered_lbm_from_answers"] = tiered_lbm_from_answers(agent_workflow)
    except Exception as error:  # noqa: BLE001
        found["problems"].append(f"whether its {TIERED_STRATEGY} strategy loads the LBM the answers name could not be "
                                 f"read ({type(error).__name__})")
    try:
        from msdial_app import agent_workflow

        if callable(getattr(agent_workflow, "_apply_retention_time_use", None)):
            found["retention_time_use"] = agent_workflow._apply_retention_time_use
    except Exception as error:  # noqa: BLE001
        found["problems"].append(f"how its workflow applies the answers' retention-time use could not be read "
                                 f"({type(error).__name__})")
    return found


def tiered_lbm_from_answers(agent_workflow: Any) -> bool:
    """Whether the pinned Interactive's tiered_lipid_msp strategy loads the LBM its answers name (libraries.lbm_path).

    Every Interactive before the fix of 2026-10-24 (0.5.47) loaded its library catalog's official LBM in its place,
    while the answers, and every record the runner built from them, named the profile's: the 15 MB-POST preflight
    units ran so, and the plan approved them as the profile's (review of 2026-10-25). The strategy is applied as
    agent_workflow._apply_libraries applies it, to answers naming a probe LBM2 and MSP, with the catalog it would
    consult standing in for one whose official LBM is a third probe name, and the LBM it loads is read back. None of
    the three exists, and nothing is read or written: the fixed strategy records an LBM2 it cannot find by name."""
    state: dict[str, Any] = {"library_provenance": []}
    answers = {"library_strategy": TIERED_STRATEGY, "libraries": {"lbm_path": PROBE_LBM, "msp_paths": [PROBE_MSP]}}
    official = {"id": "lipidomics", "kind": "lbm", "local_path": PROBE_OFFICIAL_LBM, "record_id": "probe",
                "record_url": "", "doi": "", "license": "", "downloaded": True}
    catalog = getattr(agent_workflow, "catalog_status", None)
    if catalog is not None:
        agent_workflow.catalog_status = lambda: [dict(official)]
    try:
        agent_workflow._apply_libraries(state, {}, answers)
    finally:
        if catalog is not None:
            agent_workflow.catalog_status = catalog
    return Path(str(state.get("lbm_path") or "")).name == PROBE_LBM


def msp_row_contract(row: Mapping[str, Any]) -> dict[str, Any]:
    """One MSP annotator row of Interactive's annotation profile as the approval states it: its id, its library by file
    name (never a location), the settings LIB-2 compares (MSP_ROW_SETTINGS) and its library kind as the Console reads
    it."""
    contract = {"annotator_id": str(row.get("annotator_id") or ""),
                "library": PureWindowsPath(str(row.get("msp_file_path") or "").replace("/", "\\")).name}
    contract.update({key: row[key] for key in MSP_ROW_SETTINGS if key in row})
    contract["library_kind"] = library_kind(row.get("library_kind"))
    return contract


class RetentionTimeUseUnread(Exception):
    """The answers give a retention-time use, and how the pinned Interactive applies it to the MSP rows could not be
    read: annotation_record states no rows then, and says why."""


def msp_rows_as_run(rows: Iterable[Any], answers: Mapping[str, Any], retention_time_use: Any) -> list[dict[str, Any]]:
    """The tiered profile's MSP rows as the pinned Interactive's _workflow leaves them, as the approval states each
    (msp_row_contract). _workflow applies the answers' retention-time use (use_retention_time_for_annotation and
    retention_time_tolerance) right after the library strategy (agent_workflow._apply_retention_time_use), and from
    Interactive 0.5.45 that rewrites every MSP row's RT scoring and filtering and, given a tolerance, its RT tolerance.
    Read from tiered_lcms_annotation alone, the contract stated the profile's False/False and 0.5 while the run wrote
    True/True and the answers' tolerance, so LIB-2 would have failed every unit of a profile that answers true (review
    2). The pinned Interactive's own function is applied to copies of the rows, so its code decides, never a
    restatement here. Answers that give no retention-time use need nothing applied; ones that give one when the
    function cannot be read, or cannot be applied, raise RetentionTimeUseUnread."""
    state: dict[str, Any] = {"msp_annotators": [copy.deepcopy(dict(row)) for row in rows if isinstance(row, Mapping)],
                             "text_annotators": []}
    if answers.get("use_retention_time_for_annotation") is not None:
        if not callable(retention_time_use):
            raise RetentionTimeUseUnread("how the pinned Interactive applies the answers' retention-time use to the MSP "
                                         "rows could not be read, so the rows are not stated")
        try:
            retention_time_use(state, copy.deepcopy(dict(answers)))
        except Exception as error:  # noqa: BLE001 - Interactive's _workflow would raise on the same answers
            raise RetentionTimeUseUnread(f"the answers' retention-time use could not be applied to the MSP rows as the "
                                         f"pinned Interactive applies it ({type(error).__name__}), so the rows are not "
                                         "stated") from error
    return [msp_row_contract(row) for row in state["msp_annotators"]]


def _msp_row_words(row: Mapping[str, Any]) -> str:
    def value(key: str) -> str:
        return str(row.get(key, "?"))

    return (f"{row['annotator_id']} ({row['library']}; priority {value('priority')}; MS1/MS2 tolerance "
            f"{value('ms1_tolerance')}/{value('ms2_tolerance')}; weighted/simple/reverse "
            f"{value('weighted_dot_product_cutoff')}/{value('simple_dot_product_cutoff')}/"
            f"{value('reverse_dot_product_cutoff')}; matched peaks {value('matched_peaks_percentage_cutoff')}, at least "
            f"{value('minimum_spectrum_match')}; RT scoring/filtering {value('use_rt_scoring')}/{value('use_rt_filtering')}"
            f" within {value('rt_tolerance')} min; target omics {value('target_omics')}; {row['library_kind']}"
            + (f"; suggestions {value('suggestions')}" if "suggestions" in row else "") + ")")


def name_polarity(name: str) -> str:
    """The ion mode a library's file name states by a "pos"/"positive" or "neg"/"negative" token, or "", as
    Interactive's agent_workflow._name_polarity reads it."""
    tokens = {token for token in re.split(r"[_\-.\s]+", str(name).casefold()) if token}
    positive, negative = bool(tokens & {"pos", "positive"}), bool(tokens & {"neg", "negative"})
    return "Positive" if positive and not negative else "Negative" if negative and not positive else ""


def in_silico_preprocessing_steps() -> "set[str] | None":
    """The preprocessing steps the pinned Interactive's Materials and Methods can state
    (annotation_pipeline.IN_SILICO_PREPROCESSING_STEPS), or None where it has none to read."""
    try:
        from msdial_app.annotation_pipeline import IN_SILICO_PREPROCESSING_STEPS
    except Exception:  # noqa: BLE001 - an Interactive without the in-silico tier; its refusal is said elsewhere
        return None
    return {str(step) for step in IN_SILICO_PREPROCESSING_STEPS}


def tiered_takes_in_silico(tiered: Any) -> bool | None:
    """Whether the pinned Interactive's tiered profile takes an in-silico MSP (its in_silico_path parameter,
    Interactive 0.5.48); None where its signature cannot be read. One without it builds the three-tier profile
    whatever the answers name, and its _apply_libraries ignores libraries.in_silico_msp_paths."""
    try:
        return IN_SILICO_PARAMETER in inspect.signature(tiered).parameters
    except (TypeError, ValueError):
        return None


def _named(value: Any) -> str:
    """The libraries a value names as library:<file name>, as one text."""
    return ", ".join(sorted(library_references(value)))


def annotation_record(
    profile: Mapping[str, Any] | None,
    pinned: Iterable[Mapping[str, Any]],
    not_pinned: Iterable[str],
    interactive: Any = DETECT,
    library_paths: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """What the manifest states of its libraries and their annotation, words included, so the digest a person
    approves covers them: the libraries the plan pinned (the profile's) and the resource map's others, left unpinned;
    and for a unit of each ion mode its library strategy, the annotation profile the pinned Interactive applies for
    it, its LBM and the LBM score cutoffs that Interactive writes into the method (interactive_annotation). The
    cutoffs a profile's workflow_overrides set are its own, over those of the template the unit takes; a template_path
    the answers name (library:<file name>) is read where the resource map (`library_paths`, read here and stated
    nowhere) locates it, as Interactive reads it once Runner.answers resolves it (review 3). `interactive` is
    interactive_annotation(), or DETECT to read it.

    Each ion mode's item is also the annotation contract the authorization carries to the gate's LIB-2
    (annotation_contract, review of 2026-10-25): the profile id, the LBM, its cutoffs and its priority, and each MSP
    annotator row the tiered profile sets, with the answers' retention-time use applied as the pinned Interactive's
    workflow applies it (msp_rows_as_run), so a method that drifts from what was approved, as the 0.15/0.15/0.3 LBM
    cutoffs of 13 preflight units did from 0.1/0.1/0.1, stops the run. A tiered strategy given an LBM in libraries
    also says whether the pinned Interactive loads it (lbm_from_profile, tiered_lbm_from_answers): one that loads its
    official LBM in its place, or cannot be probed, is not approvable (annotation_problems). So is a strategy that
    loads a library the profile does not name (unnamed_library): the official strategy, and the tiered one given no
    libraries.lbm_path, which loads its catalog's official LBM; the plan pins only what the profile names, so LIB-2
    would stop every unit (review 2).

    An in-silico MSP the answers name (libraries.in_silico_msp_paths, the fourth tier) is passed to the pinned
    Interactive's tiered profile as its _apply_libraries passes it (in_silico_path), so the contract is the four-tier
    profile it writes: the in-silico row predicted, with suggestions false (2026-10-28: the plan stated the three-tier
    profile, and LIB-2 failed every four-tier unit). Where that Interactive's tiered profile takes no in-silico MSP, no
    contract is stated and the plan is refused (in_silico_unsupported, annotation_problems)."""
    found = interactive_annotation() if interactive is DETECT else dict(interactive)
    problems = list(found.get("problems") or [])
    pinned = [dict(item) for item in pinned]
    not_pinned = sorted(str(item) for item in not_pinned)
    lines = []
    if pinned:
        lines.append("  libraries pinned, the ones the profile names: " + ", ".join(
            f"{item['name']} ({item.get('role') or library_role(item['name'])}"
            + (", in-silico (predicted)" if item.get("library_kind") == "predicted" else "")
            + (f"; {', '.join(item['ion_modes'])}" if item.get("ion_modes") else "") + ")" for item in pinned))
    else:
        lines.append("  libraries pinned: none")
    lines.append("  libraries in the resource map left unpinned, since the profile names none of them: "
                 + (", ".join(not_pinned) if not_pinned else "none"))
    by_mode: dict[str, dict[str, Any]] = {}
    if profile is None:
        lines.append("  annotation: the manifest has no profile")
    for mode in ION_MODES if profile is not None else ():
        answers = ion_mode_answers(profile, mode)
        libraries = answers.get("libraries") if isinstance(answers.get("libraries"), Mapping) else {}
        overrides = answers.get("workflow_overrides") if isinstance(answers.get("workflow_overrides"), Mapping) else {}
        strategy = str(answers.get("library_strategy") or "")
        lbm = _named(libraries.get("lbm_path")) or _named(overrides.get("lbm_path"))
        item: dict[str, Any] = {"library_strategy": strategy or None, "lbm": lbm or None, "profile_id": None,
                                "lbm_cutoffs": None, "lbm_cutoffs_source": "", "lbm_priority": None, "msp_rows": None}
        tiered = found.get("tiered")
        rows: list[dict[str, Any]] = []
        rt_answered = answers.get("use_retention_time_for_annotation") is not None
        # The in-silico MSP the answers name for the fourth tier (2026-10-28: the plan stated the three-tier contract
        # for a profile naming one, and LIB-2 failed every four-tier unit after its download and diagnostic).
        in_silico = sorted(library_references(libraries.get(IN_SILICO_MSP_KEY)))
        if in_silico:
            item["in_silico_libraries"] = in_silico
        # What Interactive's _in_silico_library_problems counts and reads without the file (review 2): its entries,
        # duplicates included, and the preprocessing steps the answers declare.
        entries = libraries.get(IN_SILICO_MSP_KEY)
        entries = [entry for entry in (entries if isinstance(entries, (list, tuple)) else [entries])
                   if entry not in (None, "")]
        if len(entries) > 1:
            item["in_silico_entries"] = len(entries)
        steps = libraries.get(IN_SILICO_PREPROCESSING_KEY)
        steps = [str(step) for step in (steps if isinstance(steps, (list, tuple)) else [steps]) if step not in (None, "")]
        if steps:
            item["in_silico_preprocessing"] = steps
        takes_in_silico = tiered_takes_in_silico(tiered) if callable(tiered) else None
        if strategy == TIERED_STRATEGY and callable(tiered) and in_silico and takes_in_silico is not True:
            # An Interactive whose tiered profile takes no in-silico MSP runs three tiers: no contract is stated, and
            # the plan is refused (annotation_problems), never approved as the three-tier contract.
            item["in_silico_unsupported"] = (
                f"its tiered profile (annotation_pipeline.tiered_lcms_annotation) takes no in-silico MSP (no "
                f"{IN_SILICO_PARAMETER}), so it would run three tiers" if takes_in_silico is False else
                "whether its tiered profile takes an in-silico MSP could not be read")
        elif strategy == TIERED_STRATEGY and callable(tiered):
            msp = sorted(library_references(libraries.get(ACQUIRED_MSP_KEY)))
            try:
                # As agent_workflow._apply_libraries calls it: the first in-silico MSP, by keyword.
                annotation = (tiered(lbm, msp[0] if msp else "", **{IN_SILICO_PARAMETER: in_silico[0]}) if in_silico
                              else tiered(lbm, msp[0] if msp else ""))
                item["profile_id"] = str(annotation.get("profile_id") or "") or None
                lbm_annotator = annotation.get("lbm_annotator")
                item["lbm_priority"] = lbm_annotator.get("priority") if isinstance(lbm_annotator, Mapping) else None
                if isinstance(annotation.get("msp_annotators"), list):
                    rows = msp_rows_as_run(annotation["msp_annotators"], answers, found.get("retention_time_use"))
                    item["msp_rows"] = rows
            except RetentionTimeUseUnread as error:
                # Rows the answers' RT use would rewrite unseen are not stated, so LIB-2 holds the method to none.
                problems.append(str(error))
            except Exception as error:  # noqa: BLE001 - not read, so not stated; the summary says so
                problems.append(f"the tiered annotation profile could not be read ({type(error).__name__})")
        if strategy == TIERED_STRATEGY and _named(libraries.get("lbm_path")):
            item["lbm_from_profile"] = found.get("tiered_lbm_from_answers")
        # A library the strategy loads that the profile does not name, which the plan cannot pin (split_libraries):
        # LIB-2 would stop every unit on it, after its download and diagnostic (review 2).
        if strategy == "official":
            item["unnamed_library"] = "its library catalog's official library (library strategy official)"
        elif strategy == TIERED_STRATEGY and not _named(libraries.get("lbm_path")):
            item["unnamed_library"] = (f"its library catalog's official LBM (the {TIERED_STRATEGY} strategy, given no "
                                       "libraries.lbm_path)")
        stated = {key: overrides[state] for key, state in LBM_CUTOFF_KEYS if state in overrides}
        template = answers.get("template_path")
        # The cutoffs of the template this unit takes: the settings' or Interactive's own (found["lbm_cutoffs"]), or
        # the one its answers name, which Interactive takes over its settings' (agent_workflow._workflow). One that
        # carries no template's cutoffs writes its method writer's whatever the template (found["lbm_cutoffs"]).
        base = dict(found["lbm_cutoffs"]) if found.get("lbm_cutoffs") else None
        base_source = str(found.get("lbm_cutoffs_source") or "")
        if template and callable(found.get("template_cutoffs")):
            base, base_source = None, ""
            location = template_location(template, library_paths)
            if location is None:
                named = _named(template) or "not as library:<file name>"
                problems.append(f"the parameter template the answers name ({named}) is not one the resource map "
                                "locates, so its LBM cutoffs were not read")
            else:
                try:
                    cutoffs, source = found["template_cutoffs"](location)
                except Exception as error:  # noqa: BLE001 - not read, so not stated; the summary says so
                    cutoffs, source = None, ""
                    problems.append(f"the LBM cutoffs of the template the answers name could not be read "
                                    f"({type(error).__name__})")
                if cutoffs:
                    base, base_source = dict(cutoffs), source
        if stated:
            # Interactive applies the overrides over the template the unit takes, so the others are that template's
            # (review 3: they were the settings' template's, whatever the answers named).
            item["lbm_cutoffs"] = {key: stated.get(key, (base or {}).get(key)) for key, _state in LBM_CUTOFF_KEYS}
            item["lbm_cutoffs_source"] = (f"the profile's workflow_overrides ({', '.join(sorted(stated))})"
                                          + (f", the others from {base_source}" if len(stated) < len(LBM_CUTOFF_KEYS)
                                             and base_source else ""))
        elif base:
            item["lbm_cutoffs"], item["lbm_cutoffs_source"] = base, base_source
        by_mode[mode] = item
        cutoffs = item["lbm_cutoffs"]
        loads = ""
        if "lbm_from_profile" in item and item["lbm_from_profile"] is False:
            loads = (", WHICH THE PINNED INTERACTIVE DOES NOT LOAD: its tiered_lipid_msp strategy loads its library "
                     "catalog's official LBM in its place")
        elif "lbm_from_profile" in item and item["lbm_from_profile"] is not True:
            loads = (", which the pinned Interactive may not load: whether its tiered_lipid_msp strategy loads the LBM "
                     "the answers name could not be read")
        if item.get("unnamed_library"):
            loads += (f", and the pinned Interactive loads {item['unnamed_library']}, WHICH THE PLAN DOES NOT PIN: it "
                      "pins only the libraries the profile names")
        if in_silico:
            loads += f"; the profile's in-silico MSP {', '.join(in_silico)}"
            if item.get("in_silico_unsupported"):
                loads += (", WHICH THE PINNED INTERACTIVE DOES NOT SEARCH: " if takes_in_silico is False else
                          ", which the pinned Interactive may not search: ") + item["in_silico_unsupported"]
            elif strategy != TIERED_STRATEGY:
                loads += f", WHICH ONLY THE {TIERED_STRATEGY} STRATEGY SEARCHES"
        lines.append(
            f"  annotation of {mode} units: library strategy {strategy or 'not stated'}; annotation profile "
            + (f"{item['profile_id']} (the pinned Interactive's annotation_pipeline)" if item["profile_id"] else
               "none" + (f" (library strategy {strategy})" if strategy else ""))
            + f"; the profile's LBM {lbm or 'none'}{loads}; LBM cutoffs weighted/simple/reverse "
            + ("/".join(str(cutoffs.get(key)) for key, _state in LBM_CUTOFF_KEYS) + f" from {item['lbm_cutoffs_source']}"
               if cutoffs else "not readable from the pinned Interactive's code")
            + (f"; LBM priority {item['lbm_priority']}" if item["lbm_priority"] is not None else "")
        )
        if rows:
            lines.append(f"    MSP annotators of {mode} units, as that profile sets them"
                         + (", with the answers' retention-time use applied as the pinned Interactive's workflow "
                            "applies it" if rt_answered else "") + ": "
                         + "; ".join(_msp_row_words(row) for row in rows))
    if problems:
        lines.append("  not read from the pinned Interactive: " + "; ".join(dict.fromkeys(problems)))
    return {"libraries_not_pinned": not_pinned, "by_ion_mode": by_mode, "statement": lines}


def annotation_problems(manifest: Mapping[str, Any]) -> list[str]:
    """Why the manifest's units would not annotate with the libraries its profile names: the pinned Interactive's
    tiered_lipid_msp strategy loads its library catalog's official LBM in place of the profile's, or whether it does
    could not be read (review of 2026-10-25); or the strategy loads a library the profile does not name, which the
    plan does not pin (unnamed_library, review 2). Approved, every unit would download and run its diagnostic before
    LIB-2 stopped it, and then fail. Every ion mode is read, whatever the units' Catalog ion modes: a unit runs in the
    mode its raw data give (a Mixed split's part, a header that overrides the declaration), and takes that mode's
    answers (review 3). A tiered ion mode whose contract the plan could not read in full is refused too: LIB-2
    compares only what the contract states, so cutoff, priority or MSP-row drift would pass it unseen (review 3).
    So is an in-silico MSP (libraries.in_silico_msp_paths) the pinned Interactive's tiered profile cannot be shown to
    search (no in_silico_path, Interactive before 0.5.48): it would run three tiers while the answers and pins name
    the fourth, and the plan stated the three-tier contract until 2026-10-28; or one named for another strategy, or
    more than one for an ion mode, which Interactive's own plan refuses.
    A manifest planned before the plan stated these says nothing either way."""
    annotation = manifest.get("annotation") if isinstance(manifest.get("annotation"), Mapping) else {}
    by_mode = annotation.get("by_ion_mode") if isinstance(annotation.get("by_ion_mode"), Mapping) else {}
    problems = []
    for mode in sorted(by_mode, key=str):
        item = by_mode[mode]
        if not isinstance(item, Mapping):
            continue
        if item.get("unnamed_library"):
            # The official strategy loads its catalog's library whatever the profile names (annotation_record).
            remedy = (f"use library strategy {TIERED_STRATEGY} with the LBM2 named as libraries.lbm_path, as "
                      "library:<file name>, since the official strategy loads its catalog's library whatever the "
                      "profile names" if item.get("library_strategy") == "official" else
                      "name the library in the profile as library:<file name> (the LBM2 as libraries.lbm_path)")
            problems.append(f"the pinned Interactive loads {item['unnamed_library']} for {mode} units, which the plan "
                            "does not pin, since it pins only the libraries the profile names, so LIB-2 would stop "
                            f"every unit: {remedy}, and plan again")
        in_silico = [str(name) for name in item.get("in_silico_libraries") or []]
        if in_silico and item.get("library_strategy") != TIERED_STRATEGY:
            # Interactive's plan refuses it (agent_workflow: in_silico_msp_paths is searched only by tiered_lipid_msp).
            problems.append(f"the profile names an in-silico MSP ({', '.join(in_silico)}) for {mode} units, which only "
                            f"the {TIERED_STRATEGY} strategy searches, and their strategy is "
                            f"{item.get('library_strategy') or 'not stated'}: use {TIERED_STRATEGY}, or leave "
                            f"libraries.{IN_SILICO_MSP_KEY} out, and plan again")
        elif max(len(in_silico), int(item.get("in_silico_entries") or 0)) > 1:
            # Interactive's tiered profile searches one, the run's ion mode's (_in_silico_library_problems), and
            # counts a name given twice twice.
            count = max(len(in_silico), int(item.get("in_silico_entries") or 0))
            problems.append(f"the profile names {count} in-silico MSPs ({', '.join(in_silico)}) for {mode} "
                            f"units, and the {TIERED_STRATEGY} profile searches one: name one per ion mode under "
                            "by_ion_mode, and plan again")
        for name in in_silico:
            # Interactive refuses a file its name says is for the other ion mode (agent_workflow._name_polarity),
            # and one that is no .msp.
            said = name_polarity(name)
            if said and said != str(mode):
                problems.append(f"the in-silico MSP {name} is named for {said} ion mode and the profile gives it to "
                                f"{mode} units, which Interactive would refuse for every unit that runs in that mode: "
                                "give each ion mode its own file under by_ion_mode, and plan again")
            if PureWindowsPath(name).suffix.casefold() != ".msp":
                problems.append(f"the in-silico MSP {name} for {mode} units is no .msp file, which the in-silico tier "
                                "reads, so Interactive would refuse it: name an .msp, and plan again")
        # As Interactive's tiered strategy reads them (_tiered_library_problems); another strategy reads none.
        steps = ([str(step) for step in item.get("in_silico_preprocessing") or []]
                 if item.get("library_strategy") == TIERED_STRATEGY else [])
        known =in_silico_preprocessing_steps() if steps else None
        unknown = [step for step in steps if known is not None and step not in known]
        if unknown:
            problems.append(f"the answers of {mode} units name in-silico preprocessing {', '.join(unknown)}, which the "
                            "pinned Interactive's Materials and Methods cannot state, so it would refuse it: name only "
                            f"{', '.join(sorted(known or []))}, and plan again")
        if steps and not in_silico:
            problems.append(f"the answers of {mode} units name libraries.{IN_SILICO_PREPROCESSING_KEY} without an "
                            "in-silico MSP, which Interactive refuses: leave it out, and plan again")
        if NEGATIVE_ONLY_PREPROCESSING in steps and str(mode) == "Positive":
            problems.append(f"the answers of Positive units name the in-silico preprocessing "
                            f"{NEGATIVE_ONLY_PREPROCESSING}, the negative-mode records' artefact, which Interactive "
                            "refuses for a positive-mode run: give it under by_ion_mode.Negative only, and plan again")
        if item.get("in_silico_unsupported"):
            # Approved, the run would load the three-tier profile while the answers and pins name the in-silico MSP,
            # and LIB-2 would stop every unit after its download and diagnostic (2026-10-28).
            problems.append(f"the profile names the in-silico MSP {', '.join(in_silico)} for {mode} units, and the "
                            f"pinned Interactive cannot be held to searching it: {item['in_silico_unsupported']}: "
                            "pin an Interactive whose tiered profile searches an in-silico MSP (0.5.48 or later), or "
                            f"leave libraries.{IN_SILICO_MSP_KEY} out, and plan again")
        unread = [words for key, words in CONTRACT_PART_WORDS if key in item and contract_part_unread(key, item[key])]
        # A contract left unread because the in-silico MSP cannot be searched is refused above, with its reason.
        if item.get("library_strategy") == TIERED_STRATEGY and unread and not item.get("in_silico_unsupported"):
            problems.append(f"the plan could not read {', '.join(unread)} for {mode} units from the pinned Interactive "
                            "(the summary says why), so LIB-2 would not hold the method to them: plan again once the "
                            "plan can read them")
        if "lbm_from_profile" not in item or item["lbm_from_profile"] is True:
            continue
        if item["lbm_from_profile"] is False:
            problems.append(f"the pinned Interactive's {TIERED_STRATEGY} strategy loads its library catalog's official "
                            f"LBM, not the profile's {item.get('lbm')}, for {mode} units: pin an Interactive that loads "
                            "the LBM its answers name, and plan again")
        else:
            problems.append(f"whether the pinned Interactive's {TIERED_STRATEGY} strategy loads the profile's LBM "
                            f"{item.get('lbm')} for {mode} units could not be read: plan again with an Interactive "
                            "the plan can probe")
    return problems


def annotation_contract(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]] | None:
    """What the manifest states each ion mode's units annotate with, as the authorization carries it to the gate's
    LIB-2 (review of 2026-10-25): the annotation profile id, the LBM by file name, its score cutoffs and priority, and
    each MSP annotator row (msp_row_contract), None where the manifest does not state one. Names only, never a
    location. None for a manifest planned before the plan stated its annotation."""
    annotation = manifest.get("annotation") if isinstance(manifest.get("annotation"), Mapping) else None
    by_mode = (annotation or {}).get("by_ion_mode")
    if not isinstance(by_mode, Mapping):
        return None
    return {str(mode): {key: item.get(key) for key in ANNOTATION_CONTRACT_KEYS}
            for mode, item in by_mode.items() if isinstance(item, Mapping)}


AUTOMATIC_RT_ANSWER = "execute_automatic_rt_correction"
AUTOMATIC_RT_ANCHORS_ANSWER = "automatic_rt_correction_maximum_anchors"
AUTOMATIC_RT_WINDOW_ANSWER = "automatic_rt_correction_local_support_rt_window"
ANCHOR_LIBRARY_RT_ANSWER = "execute_rt_correction"
# Every other setting of the automatic correction that Interactive reads from the answers (or a workflow_overrides)
# and writes into each unit's method: workflow.AUTOMATIC_RT_CORRECTION_DEFAULTS, the reference file, bin width, match
# tolerance, least anchors, sample coverage, intensity and peak-width quantiles, signal to noise, Gaussian
# similarity, ideal slope, outlier MAD threshold and centrality weight. They are known by their prefix, so a setting
# Interactive adds later is not missed (test_campaign_plan checks the prefix against Interactive's table).
AUTOMATIC_RT_SETTING_PREFIX = "automatic_rt_correction_"


def automatic_rt_tuning_key(key: str) -> bool:
    """Whether a profile key is a setting of the automatic correction other than those the statement names on
    their own (the anchors, the local window and Blank interpolation)."""
    return key.startswith(AUTOMATIC_RT_SETTING_PREFIX) and key not in (
        AUTOMATIC_RT_ANCHORS_ANSWER, AUTOMATIC_RT_WINDOW_ANSWER, policy.AUTOMATIC_RT_BLANK_ANSWER)


def _true(value: Any) -> bool:
    # As Interactive reads an answer (agent_workflow._as_bool).
    return value if isinstance(value, bool) else str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _in_overrides(where: str) -> bool:
    """Whether a setting found at `where` (as _profile_settings names it) sits directly in a workflow_overrides."""
    return where == "workflow_overrides" or where.endswith(".workflow_overrides")


def _setting_true(where: str, value: Any) -> bool:
    """Whether Interactive runs a boolean setting the profile gives at `where` as true. An answer goes through
    agent_workflow._as_bool, so "false" is false. A workflow_overrides value does not: Interactive applies
    workflow_overrides unconverted over its state (state.update(overrides)) and then reads the setting by its
    truthiness (workflow.py: bool(state.get("execute_automatic_rt_correction", False))), so the string "false",
    like any non-empty string or 2, is true there."""
    return bool(value) if _in_overrides(where) else _true(value)


def _override_reading(where: str, value: Any) -> str:
    """What a non-bool workflow_overrides value of a boolean setting means to Interactive, or "" for any other."""
    if not _in_overrides(where) or isinstance(value, bool):
        return ""
    return f"{value!r}, which Interactive applies unconverted and reads as {json.dumps(bool(value))}"


def automatic_rt_override_value_problems(profile: Mapping[str, Any] | None) -> list[str]:
    """A non-bool value for a correction switch in a workflow_overrides: Interactive applies it unconverted and
    reads it by truthiness, so the profile's words (the string "false") could say the opposite of the run."""
    return [
        f"the profile sets {key} to {_override_reading(where, value)} ({where}): write true or false there"
        for where, key, value in _profile_settings(profile)
        if key in (AUTOMATIC_RT_ANSWER, ANCHOR_LIBRARY_RT_ANSWER) and _override_reading(where, value)
    ]


def _profile_settings(profile: Mapping[str, Any] | None) -> Iterable[tuple[str, str, Any]]:
    """Every (where, key, value) the profile sets: in its answers, for an ion mode, or in a workflow_overrides
    beneath either (Interactive applies those over the answers)."""
    def walk(where: str, value: Any) -> Iterable[tuple[str, str, Any]]:
        if isinstance(value, Mapping):
            for key, item in value.items():
                yield where, str(key), item
                yield from walk(f"{where}.{key}", item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from walk(where, item)
    profile = profile or {}
    yield from walk("answers", profile.get("answers"))
    yield from walk("by_ion_mode", profile.get("by_ion_mode"))


def automatic_rt_correction_requested(profile: Mapping[str, Any] | None, key: str = AUTOMATIC_RT_ANSWER) -> bool:
    """Whether the profile turns automatic alignment RT correction (or, with key=ANCHOR_LIBRARY_RT_ANSWER, the
    anchor-library RT correction) on anywhere: in its answers, for an ion mode, or in a workflow_overrides
    beneath either."""
    return any(found == key and _setting_true(where, value) for where, found, value in _profile_settings(profile))


def automatic_rt_tuning_statements(profile: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Every setting of the automatic correction the profile gives other than the switch, the anchors, the window
    and Blank interpolation (automatic_rt_tuning_key), with where it gives it."""
    return [{"key": key, "where": where, "value": value}
            for where, key, value in _profile_settings(profile) if automatic_rt_tuning_key(key)]


def _tuned(statements: Sequence[Mapping[str, Any]]) -> str:
    return ", ".join(f"{item['key']} {item['value']!r} ({item['where']})" for item in statements)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def automatic_rt_profile_conflicts(profile: Mapping[str, Any] | None, campaign_policy: policy.CampaignPolicy) -> list[str]:
    """Where a profile says otherwise than the automatic RT correction the campaign policy pins. The runner's pin
    would win (Runner.answers), so such a profile would be approved for a method it does not run."""
    problems = []
    anchors = int(campaign_policy.automatic_rt_correction_maximum_anchors)
    for where, key, value in _profile_settings(profile):
        if key == AUTOMATIC_RT_ANSWER and _in_overrides(where):
            # Runner.answers writes the pin, and the uncorrected fallback, as an answer; Interactive applies
            # workflow_overrides over the answers, so a value here would decide every unit's run, fallback or not.
            reading = _override_reading(where, value) or json.dumps(value)
            problems.append(f"the profile sets {key} in a workflow_overrides ({where}) to {reading}; Interactive applies "
                            "workflow_overrides over the answer the runner pins and over its uncorrected fallback: leave "
                            "it to the campaign policy")
        elif key == AUTOMATIC_RT_ANSWER and not _true(value):
            problems.append(f"the profile turns automatic RT correction off ({where}), and the campaign policy pins it on")
        elif key == AUTOMATIC_RT_ANCHORS_ANSWER and _number(value) != anchors:
            problems.append(f"the profile sets {key} {value!r} ({where}), and the campaign policy pins {anchors}")
        elif key == ANCHOR_LIBRARY_RT_ANSWER and _setting_true(where, value):
            reading = _override_reading(where, value)
            problems.append(f"the profile turns the anchor-library RT correction on ({where}"
                            + (f": {reading}" if reading else "") + "); the campaign runs the automatic correction alone")
        elif key == AUTOMATIC_RT_WINDOW_ANSWER and _number(value) != policy.AUTOMATIC_RT_LOCAL_SUPPORT_RT_WINDOW:
            problems.append(f"the profile sets {key} {value!r} ({where}); the campaign runs #826's default window of "
                            f"{policy.AUTOMATIC_RT_LOCAL_SUPPORT_RT_WINDOW} min")
        elif key == policy.AUTOMATIC_RT_BLANK_ANSWER:
            problems.append(f"the profile sets {key} ({where}); the runner sets it for each unit from the analytical "
                            "order Interactive records (true only for a header or declared order)")
        elif automatic_rt_tuning_key(key):
            # The runner pins none of these, so the profile's value would run in every unit, and the decision of
            # 2026-10-07 (#826 with 12 anchors) is Interactive's defaults for them.
            problems.append(f"the profile sets {key} {value!r} ({where}); under the campaign policy's pin every "
                            "other automatic RT correction setting is Interactive's default, as decided on "
                            f"{AUTOMATIC_RT_DECISION_DATE}: leave it out of the profile")
    return problems


def automatic_rt_correction_problems(
    profile: Mapping[str, Any] | None, console: Mapping[str, Any] | None, recorded_policy: Mapping[str, Any] | None = None
) -> list[str]:
    """Why the manifest's automatic RT correction cannot run as approved, or nothing.

    The campaign runs it with MsdialWorkbench #826's local outlier test and 12 anchors (decided 2026-10-07). A
    policy that pins it (policy.automatic_rt_correction_pinned) must not meet a profile that says otherwise. A
    pinned or requested correction needs a Console of #826: Interactive refuses a Console without the correction
    only at each unit's run start, after the unit's download, and runs a Console of #810 alone with its run-wide
    test without a word, since no method-key record shows the difference.
    """
    problems = []
    pinned = policy.automatic_rt_correction_pinned(recorded_policy)
    if pinned:
        try:
            campaign_policy = policy.CampaignPolicy.from_dict(recorded_policy)
        except (TypeError, ValueError) as error:
            return [f"the manifest's campaign policy cannot be read: {error}"]
        if campaign_policy.automatic_rt_correction:
            problems.extend(automatic_rt_profile_conflicts(profile, campaign_policy))
    else:
        # Under the pin, automatic_rt_profile_conflicts refuses a workflow_overrides switch outright.
        problems.extend(automatic_rt_override_value_problems(profile))
    if not (pinned or automatic_rt_correction_requested(profile)):
        return problems
    turned_on = "the campaign policy pins automatic RT correction on" if pinned else "the profile turns automatic RT correction on"
    console = console or {}
    if not console.get("exists"):
        return problems  # "the manifest pins no console binary" says it
    generation = console.get("automatic_rt_correction")
    if generation is None:
        problems.append(f"{turned_on}, and the Console pin does not record which correction the Console implements: "
                        "plan again")
    elif generation != policy.AUTOMATIC_RT_LOCAL_SUPPORT:
        implements = ("MsdialWorkbench #810's run-wide outlier test only" if generation == policy.AUTOMATIC_RT_RUN_WIDE
                      else "no automatic RT correction")
        problems.append(f"{turned_on}, and the pinned Console implements {implements}, not #826's local outlier test: "
                        "pin a Console built with #826 and plan again")
    return problems


# What the user decided for the campaign's automatic alignment RT correction on 2026-10-07: on, at most 12 anchors,
# and an uncorrected fallback after an anchor-selection failure (the policy fields that carry it). The local window
# is #826's own default, which the runner does not send, and Blank interpolation is decided per unit by the order
# Interactive recorded; neither is a policy field.
AUTOMATIC_RT_DECISION_DATE = "2026-10-07"
AUTOMATIC_RT_DECISION = {
    "automatic_rt_correction": True,
    "automatic_rt_correction_maximum_anchors": 12,
    "automatic_rt_correction_fallback": True,
}
AUTOMATIC_RT_BLANK_RULE = (
    "interpolated by analytical order only where an injection order was recorded (the raw headers' acquisition "
    "start times or the repository's sample table); otherwise a Blank keeps its measured RTs")
_MISSING = object()


def _on(value: bool) -> str:
    return "ON" if value else "OFF"


def _profile_statements(profile: Mapping[str, Any] | None, key: str) -> list[dict[str, Any]]:
    return [{"where": where, "value": value} for where, found, value in _profile_settings(profile) if found == key]


def _stated(statements: Sequence[Mapping[str, Any]]) -> str:
    return ", ".join(f"{item['value']!r} ({item['where']})" for item in statements)


ION_MODES = ("Positive", "Negative")


def automatic_rt_correction_by_ion_mode(
    profile: Mapping[str, Any] | None, key: str = AUTOMATIC_RT_ANSWER
) -> dict[str, dict[str, Any]]:
    """Whether a unit of each ion mode runs the automatic RT correction under the profile alone (no policy pin),
    or, with key=ANCHOR_LIBRARY_RT_ANSWER, the anchor-library RT correction (which no policy pins), and which
    setting decides it (set_by; None where the profile does not set it and Interactive's default, off,
    applies). Runner.answers merges the profile's answers, then by_ion_mode for the unit's ion mode, nested
    objects key by key; Interactive then applies workflow_overrides over the answers. So the first of these that
    sets it decides: by_ion_mode.<mode>.workflow_overrides, answers.workflow_overrides, by_ion_mode.<mode>,
    answers. Interactive's answer seed does not set it. An answer is read as agent_workflow._as_bool reads it, a
    workflow_overrides value by its truthiness, as Interactive runs it (_setting_true); a non-bool value there is
    kept with what Interactive makes of it (read_as), so the statement says it."""
    profile = profile or {}
    answers = profile.get("answers") if isinstance(profile.get("answers"), Mapping) else {}
    by_mode = profile.get("by_ion_mode") if isinstance(profile.get("by_ion_mode"), Mapping) else {}
    result = {}
    for mode in ION_MODES:
        mode_answers = by_mode.get(mode) if isinstance(by_mode.get(mode), Mapping) else {}
        for where, settings in (
            (f"by_ion_mode.{mode}.workflow_overrides", mode_answers.get("workflow_overrides")),
            ("answers.workflow_overrides", answers.get("workflow_overrides")),
            (f"by_ion_mode.{mode}", mode_answers),
            ("answers", answers),
        ):
            if isinstance(settings, Mapping) and key in settings:
                value = settings[key]
                result[mode] = {"correction": _setting_true(where, value), "set_by": where}
                if _override_reading(where, value):
                    result[mode]["read_as"] = _override_reading(where, value)
                break
        else:
            result[mode] = {"correction": False, "set_by": None}
    return result


def automatic_rt_mode_state(item: Mapping[str, Any]) -> str:
    if not item["set_by"]:
        return _on(item["correction"]) + " (not set; Interactive's default is off)"
    read_as = f": {item['read_as']}" if item.get("read_as") else ""
    return _on(item["correction"]) + f" (set by {item['set_by']}{read_as})"


def _modes_split(modes: Mapping[str, Mapping[str, Any]]) -> bool:
    return 0 < sum(1 for mode in ION_MODES if modes[mode]["correction"]) < len(ION_MODES)


def _per_mode(modes: Mapping[str, Mapping[str, Any]]) -> str:
    return ", ".join(f"{mode} units {automatic_rt_mode_state(modes[mode])}" for mode in ION_MODES)


def _as_the_profile_says(modes: Mapping[str, Mapping[str, Any]]) -> str:
    """What a correction stated per ion mode runs as: "Each unit runs as the profile says" and what it says."""
    if _modes_split(modes):
        return f"Each unit runs as the profile says for its ion mode: {_per_mode(modes)}"
    if modes["Positive"]["set_by"] == modes["Negative"]["set_by"]:
        return f"Each unit runs as the profile says: {automatic_rt_mode_state(modes['Positive'])}"
    return f"Each unit runs as the profile says: {_on(modes['Positive']['correction'])}, {_per_mode(modes)}"


def automatic_rt_correction_record(
    recorded_policy: Mapping[str, Any] | None,
    profile: Mapping[str, Any] | None,
    overridden: Iterable[str] | None,
) -> dict[str, Any]:
    """The campaign's automatic RT correction as a person approves it: what runs, where it comes from, and where it
    differs from the decision of 2026-10-07, with the lines the plan prints (statement). The manifest keeps this
    record, so its digest covers the words a person read as well as the policy fields.

    `overridden` is the policy fields a --policy file named (None where nobody recorded them: a manifest planned
    before this record). An override is never refused here: a person may decide otherwise, and the text says so.
    """
    recorded = dict(recorded_policy or {})
    pinned = policy.automatic_rt_correction_pinned(recorded)
    fields = None if overridden is None else sorted(set(overridden) & set(AUTOMATIC_RT_DECISION))
    differences = []
    for key, decided in AUTOMATIC_RT_DECISION.items():
        value = recorded.get(key, _MISSING)
        if value is _MISSING:
            differences.append(f"{key} not recorded (a policy from before {AUTOMATIC_RT_DECISION_DATE}), "
                               f"decided {json.dumps(decided)}")
        elif value != decided or isinstance(value, bool) != isinstance(decided, bool):
            differences.append(f"{key} {json.dumps(value)}, decided {json.dumps(decided)}")
    windows = _profile_statements(profile, AUTOMATIC_RT_WINDOW_ANSWER)
    window_text = (f"local window {_stated(windows)} min as the profile states it" if windows else
                   f"local window {policy.AUTOMATIC_RT_LOCAL_SUPPORT_RT_WINDOW} min (the Console's default; not sent)")
    tuning = automatic_rt_tuning_statements(profile)
    tuning_text = ("other settings as the profile states them: " + _tuned(tuning) if tuning
                   else "other settings Interactive's defaults")
    # Interactive runs the anchor-library correction (a user-defined anchor library) wherever the profile turns it
    # on, pinned or not, and no policy field decides it: the words say where it runs.
    library_modes = automatic_rt_correction_by_ion_mode(profile, ANCHOR_LIBRARY_RT_ANSWER)
    library_on = [mode for mode in ION_MODES if library_modes[mode]["correction"]]
    library_lines = []
    if library_on:
        library_lines.append(f"  !! THE ANCHOR-LIBRARY RT CORRECTION RUNS for the {' and '.join(library_on)} units "
                             f"(the decision of {AUTOMATIC_RT_DECISION_DATE} is the automatic correction alone)")
    library_lines.append(f"  anchor-library RT correction ({ANCHOR_LIBRARY_RT_ANSWER}): no policy field decides it. "
                         + _as_the_profile_says(library_modes))
    if fields is None:
        source = "the manifest's recorded policy (which fields a --policy override named was not recorded)"
    elif fields:
        source = "a --policy override of " + ", ".join(fields)
    else:
        source = "the default campaign policy"
    record: dict[str, Any] = {
        "decision": {"date": AUTOMATIC_RT_DECISION_DATE, **AUTOMATIC_RT_DECISION},
        "pinned": pinned,
        "source": "unrecorded" if fields is None else ("policy_override" if fields else "default_policy"),
        "overridden_fields": fields,
        "differs_from_decision": differences,
        "other_settings": tuning,
        "anchor_library_rt_correction_by_ion_mode": library_modes,
    }
    lines = []
    if differences:
        lines.append(f"  !! AUTOMATIC RT CORRECTION DIFFERS FROM THE DECISION OF {AUTOMATIC_RT_DECISION_DATE} "
                     "(on, 12 anchors, uncorrected fallback): " + "; ".join(differences))
    if pinned:
        anchors = recorded.get("automatic_rt_correction_maximum_anchors")
        fallback = recorded.get("automatic_rt_correction_fallback") is True
        record.update({
            "correction": True, "maximum_anchors": anchors,
            "local_support_rt_window_min": (windows[0]["value"] if windows else policy.AUTOMATIC_RT_LOCAL_SUPPORT_RT_WINDOW),
            "local_support_rt_window_source": "profile" if windows else "console_default",
            "fallback_uncorrected": fallback, "blank_interpolation": AUTOMATIC_RT_BLANK_RULE,
        })
        lines += [
            f"  automatic RT correction: {_on(True)}, pinned by the campaign policy over the profile; from {source}",
            f"    maximum anchors {anchors}; {window_text}; MsdialWorkbench #826's local outlier test",
            f"    {tuning_text}",
            f"    fallback {_on(fallback)}: " + (
                "after an anchor-selection failure the unit's next attempts run uncorrected, and its record says so"
                if fallback else "a unit whose anchors cannot be selected is retried and ends as any failure does"),
            f"    Blanks: {AUTOMATIC_RT_BLANK_RULE}",
        ]
    else:
        # Each unit runs as the profile says for its ion mode, which need not be the same for both.
        modes = automatic_rt_correction_by_ion_mode(profile)
        on_modes = [mode for mode in ION_MODES if modes[mode]["correction"]]
        requested = bool(on_modes)
        split = _modes_split(modes)
        anchors = _profile_statements(profile, AUTOMATIC_RT_ANCHORS_ANSWER)
        blanks = _profile_statements(profile, policy.AUTOMATIC_RT_BLANK_ANSWER)
        record.update({
            "correction": "by_ion_mode" if split else requested, "correction_by_ion_mode": modes,
            "maximum_anchors": None, "local_support_rt_window_min": None,
            "fallback_uncorrected": False, "blank_interpolation": None,
            "profile_statements": {key: _profile_statements(profile, key) for key in (
                AUTOMATIC_RT_ANSWER, AUTOMATIC_RT_ANCHORS_ANSWER, AUTOMATIC_RT_WINDOW_ANSWER, policy.AUTOMATIC_RT_BLANK_ANSWER)},
        })
        if split:
            lines.append("  !! AUTOMATIC RT CORRECTION DIFFERS BY ION MODE (not pinned by the campaign policy): "
                         + _per_mode(modes))
        lines.append(f"  automatic RT correction: NOT PINNED by the campaign policy; from {source}. "
                     + _as_the_profile_says(modes))
        if requested:
            for_modes = "    for the " + " and ".join(on_modes) + " units: " if split else "    "
            lines += [
                for_modes + "maximum anchors " + (_stated(anchors) if anchors else "Interactive's default")
                + f"; {window_text}",
                for_modes + tuning_text,
                "    fallback OFF: the runner falls back to an uncorrected run only under the campaign policy's pin",
                "    Blanks: " + (_stated(blanks) if blanks else
                                  "Interactive's default (interpolated by analytical order, whatever the order was read from)"),
            ]
    record["statement"] = lines + library_lines
    return record


# ---- the manifest ---------------------------------------------------------------------------------------

def build_manifest(
    catalog: Any,
    *,
    pool: str | None = None,
    campaign_id: str,
    analysis_purpose: str,
    workspace_root: Path,
    raw_retention_policy: str,
    pins: Mapping[str, Any],
    profile: Mapping[str, Any] | None,
    campaign_policy: policy.CampaignPolicy,
    class_decision: Callable[[str], dict[str, Any]],
    catalog_database: str,
    now: datetime | None = None,
    progress: Callable[[str], None] | None = None,
    replan: Mapping[str, Mapping[str, str]] | None = None,
    unit_ids: Sequence[str] | None = None,
    ion_mobility_evidence: Any = DETECT,
    policy_overrides: Iterable[str] = (),
    libraries_not_pinned: Iterable[str] = (),
    annotation_code: Any = DETECT,
    library_paths: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The manifest for one pool, or for a pilot of named units (`unit_ids`, pool "pilot"). `catalog` is a
    read-only Catalog; nothing is written anywhere.

    `replan` is replan_states() of the prior campaigns whose unfinished units may be planned again.
    `ion_mobility_evidence` is the Catalog's helper, None for the fallback rule, or DETECT to look it up.
    `policy_overrides` names the policy fields a --policy file set, so the manifest can say what it changed.
    `libraries_not_pinned` names the resource map's libraries the profile does not name (split_libraries), which
    the manifest lists as left unpinned; `annotation_code` is what annotation_record reads of the pinned
    Interactive, or DETECT to read it; `library_paths` locates the pinned libraries, for annotation_record to read a
    parameter template the profile names as one, and goes into no manifest.
    """
    policy_overrides = sorted({str(item) for item in policy_overrides})
    if (unit_ids is None) == (pool is None or pool == PILOT):
        raise PlanError("Plan one pool, or a pilot of named units (--units), not both and not neither.")
    if not str(campaign_id or "").strip() or not re.fullmatch(r"[A-Za-z0-9._-]+", str(campaign_id)):
        raise PlanError("A campaign id is letters, digits, '.', '_' and '-'.")
    if not str(analysis_purpose or "").strip():
        raise PlanError("The campaign needs its analysis_purpose: what the reanalysis is for.")
    if raw_retention_policy not in ("keep", "delete_after_validated_output"):
        raise PlanError("raw_retention_policy is keep or delete_after_validated_output.")
    catalog_version = str((pins.get("catalog") or {}).get("version") or "")
    if catalog_version and _version(catalog_version) < MINIMUM_CATALOG_VERSION:
        # Class digests made by Catalog code older than one input per vendor container are not the
        # proposals the runner will be given (review contradiction 16).
        raise PlanError(f"Catalog {catalog_version} predates 0.6.0; its Class digests would not hold.")
    say = progress or (lambda _message: None)
    excluded: list[dict[str, Any]] = []
    if unit_ids is not None:
        pool = PILOT
        selected, outside = select_named_units(catalog.connection, unit_ids)
        requested = list(dict.fromkeys(str(item).strip() for item in unit_ids if str(item).strip()))
        for unit in outside:
            excluded.append(_exclusion(unit, ["not_in_pool"], detail=(
                f"separation {unit['separation']}, acquisition {unit['acquisition_mode']}, ion mode {unit['ion_mode']}, "
                f"untargeted {unit['untargeted']}: neither pool's rules select it"
            )))
        say(f"{len(selected)} of the {len(requested)} named units are in a pool")
    else:
        selected, outside, requested = select_units(catalog.connection, pool), [], []
        say(f"{len(selected)} units selected for the {pool} pool")
    evidence = catalog_ion_mobility_evidence() if ion_mobility_evidence is DETECT else ion_mobility_evidence
    candidates: list[dict[str, Any]] = []
    mobility: dict[str, dict[str, Any]] = {}
    fragmentation: dict[str, dict[str, Any]] = {}
    shared_by_study: dict[str, set[tuple[str, str]]] = {}

    def shared_of(study_id: str) -> Callable[[], set[tuple[str, str]]]:
        def read() -> set[tuple[str, str]]:
            if study_id not in shared_by_study:
                shared_by_study[study_id] = study_shared_attributes(catalog.connection, study_id) if study_id else set()
            return shared_by_study[study_id]
        return read

    for unit in selected:
        try:
            record: Mapping[str, Any] = catalog.get_unit(unit["unit_id"])
        except Exception as error:  # noqa: BLE001 - not read, so not excluded for it; the manifest says so
            mobility[unit["unit_id"]] = unread_ion_mobility(f"{type(error).__name__}: {error}")
            fragmentation[unit["unit_id"]] = unread_fragmentation(f"{type(error).__name__}: {error}")
        else:
            mobility[unit["unit_id"]] = ion_mobility_reading(record, evidence)
            fragmentation[unit["unit_id"]] = fragmentation_reading(
                record, unit_evidence_rows(catalog.connection, unit["unit_id"]),
                shared_of(str(record.get("study_id") or "")))
        reasons = exclusion_reasons(unit, workspace_root, replan, ion_mobility=mobility[unit["unit_id"]],
                                    fragmentation=fragmentation[unit["unit_id"]])
        if reasons:
            _reason, found = workspace_exclusion(unit, workspace_root, replan)
            said = [f"ion_mobility: {mobility[unit['unit_id']]['detail']}"] if "ion_mobility" in reasons else []
            if NOT_CID_HCD in reasons:
                said.append(f"{NOT_CID_HCD}: {fragmentation[unit['unit_id']]['detail']}")
            excluded.append(_exclusion(unit, reasons, detail="; ".join(said + ([found] if found else []))))
        else:
            candidates.append(unit)
    decisions: dict[str, dict[str, Any]] = {}
    remaining = []
    for index, unit in enumerate(candidates, start=1):
        try:
            decisions[unit["unit_id"]] = class_decision(unit["unit_id"])
        except Exception as error:  # noqa: BLE001 - one unit the Catalog cannot decide is excluded, with why
            excluded.append(_exclusion(unit, ["class_undecided"], detail=f"{type(error).__name__}: {error}"))
            continue
        remaining.append(unit)
        if index % 250 == 0:
            say(f"Class decided for {index} of {len(candidates)} units")
    download = catalog.download_plan([unit["unit_id"] for unit in remaining])
    per_unit = {item["unit_id"]: item for item in download["units"]}
    kinds: dict[str, set[str]] = {}
    for item in download["objects"]:
        for consumer in item.get("selected_consumer_unit_ids") or []:
            kinds.setdefault(consumer, set()).add(str(item.get("kind") or ""))
    included = []
    for unit in remaining:
        planned = per_unit.get(unit["unit_id"])
        if planned is None or not planned["object_count"]:
            excluded.append(_exclusion(unit, ["no_download_object"]))
            continue
        included.append((unit, planned))
    groups = {item["group_id"]: item for item in download["groups"]}
    group_order = sorted(
        {planned["group_id"] for _unit, planned in included},
        key=lambda group: (groups[group]["distinct_bytes_lower_bound"], group),
    )
    rank = {group: index for index, group in enumerate(group_order)}
    included.sort(key=lambda pair: (rank[pair[1]["group_id"]], pair[0]["repository"], pair[0]["accession"], pair[0]["unit_id"]))
    units = []
    for order_index, (unit, planned) in enumerate(included):
        decision = decisions[unit["unit_id"]]
        own = workspace_root / str(unit["repository"]) / str(unit["accession"]) / str(unit["unit_id"])
        prior = campaign_of(own) if own.is_dir() else None
        units.append({
            "unit_id": unit["unit_id"],
            "repository": unit["repository"],
            "accession": unit["accession"],
            "order_index": order_index,
            "selection_basis": unit["pool"],
            "group_id": planned["group_id"],
            "object_count": planned["object_count"],
            "known_bytes": planned["known_bytes"],
            "size_known": bool(planned["size_known"]),
            "unknown_size_objects": planned["unknown_size_objects"],
            "has_archive": bool(kinds.get(unit["unit_id"], set()) & ARCHIVE_KINDS),
            "instrument": str(unit["instrument"] or ""),
            "ion_mode": str(unit["ion_mode"] or ""),
            "acquisition_mode": str(unit["acquisition_mode"] or ""),
            "untargeted": unit["untargeted"],
            "target_omics": str(unit["target_omics"] or ""),
            "approved_class_digest": decision["proposal_id"],
            "class_kind": decision["kind"],
            "class_fields": list(decision.get("selected_fields") or []),
            "class_assignments": int(decision.get("assignment_count") or 0),
            **({"replanned_from": {"campaign_id": prior, "state": (replan or {}).get(prior, {}).get(unit["unit_id"])}}
               if prior else {}),
            # Something named ion mobility and the unit runs all the same (option A): what, and why it reaches
            # Interactive, whose header check decides each file.
            **({"ion_mobility_reading": {key: mobility[unit["unit_id"]][key] for key in ("evidence", "state", "detail")}}
               if mobility[unit["unit_id"]]["signal"] else {}),
            # Some of its files or rows name a fragmentation method other than CID/HCD, and not all (2026-10-29), or its
            # record could not be read for it: which, and that Interactive's lease keeps the files named so out.
            **({"fragmentation_reading": _fragmentation_entry(fragmentation[unit["unit_id"]])}
               if fragmentation[unit["unit_id"]]["signal"] or fragmentation[unit["unit_id"]]["evidence"] == "not_read"
               else {}),
        })
    used_groups = []
    for group in group_order:
        item = groups[group]
        members = [unit["unit_id"] for unit in units if unit["group_id"] == group]
        used_groups.append({
            "group_id": group,
            "unit_ids": members,
            "unit_count": len(members),
            "known_bytes": item["distinct_bytes_lower_bound"],
            "size_known": item["unknown_size_objects"] == 0 and not item.get("files_without_url"),
            "object_count": item["object_count"],
            "shared_object_count": item["shared_object_count"],
        })
    too_early = _cid_hcd_interactive_problem(pins, units)
    if too_early:
        raise PlanError(too_early)
    included_ids = {unit["unit_id"] for unit in units}
    objects = [item for item in download["objects"] if set(item.get("selected_consumer_unit_ids") or []) & included_ids]
    totals = _totals(selected + outside, excluded, units, used_groups, objects)
    legacy = sorted({
        f"{unit['repository']}/{unit['accession']}" for unit in units
        if legacy_accession_workspace(workspace_root, unit["repository"], unit["accession"])
    })
    excluded.sort(key=lambda item: (item["reason"], item["repository"], item["accession"], item["unit_id"]))
    return {
        "schema": MANIFEST_SCHEMA,
        "campaign_id": campaign_id,
        "pool": pool,
        "created_at": policy.iso(now or datetime.now(timezone.utc)),
        "analysis_purpose": analysis_purpose,
        "workspace_root": str(workspace_root),
        # Where the root lies that the project keeps no campaign data, as the summary says it first (review 2).
        "workspace_root_cautions": workspace_root_cautions(workspace_root),
        "raw_retention_policy": raw_retention_policy,
        "catalog": {"database": catalog_database, "version": catalog_version},
        "selection": {
            "pool": pool, "rule": SELECTION_RULES[pool],
            "exclusion_reasons": list(PILOT_EXCLUSION_REASONS if pool == PILOT else EXCLUSION_REASONS),
            # Which reading of ion mobility excluded units: the Catalog's ion_mobility_evidence, or the
            # fallback rule without it (option A, 2026-10-03).
            "ion_mobility_evidence": "fallback" if evidence is None else "catalog",
            # CID/HCD only (2026-10-29): the rule a unit's fragmentation reading was made under.
            "fragmentation_rule": CID_HCD_ONLY_RULE,
            **({"units": requested, "pools": {name: SELECTION_RULES[name] for name in POOLS}} if pool == PILOT else {}),
        },
        "policy": campaign_policy.as_dict(),
        # The policy fields a --policy file set (empty: the default policy), and the automatic RT correction as
        # the summary states it, words included, so the digest a person approves covers both.
        "policy_overrides": policy_overrides,
        "automatic_rt_correction": automatic_rt_correction_record(campaign_policy.as_dict(), profile, policy_overrides),
        "profile": dict(profile) if profile is not None else None,
        "pins": dict(pins),
        # The libraries pinned and left unpinned, and how the pinned Interactive annotates with them, as the summary
        # states it, words included, so the digest a person approves covers both.
        "annotation": annotation_record(profile, pins.get("libraries") or [], libraries_not_pinned, annotation_code,
                                        library_paths),
        "units": units,
        "groups": used_groups,
        "exclusions": excluded,
        "totals": totals,
        # Planned units whose accession folder is an earlier accession-level run's workspace: their own
        # workspaces are made beside it, and it is left as it is.
        "legacy_accession_workspaces": legacy,
        **({"replanned_from": sorted(replan)} if replan else {}),
    }


def _fragmentation_entry(reading: Mapping[str, Any]) -> dict[str, Any]:
    """What a planned unit's manifest entry says of its fragmentation reading: the rule, what was named, by what, and
    the analysis files Interactive's lease keeps out by their names (by base name, at most 20, with their count)."""
    files = list(reading.get("named_files") or [])
    return {
        "rule": CID_HCD_ONLY_RULE, "evidence": reading.get("evidence"), "methods": list(reading.get("methods") or []),
        "named_files": len(files), "analysis_files": int(reading.get("analysis_files") or 0),
        "named_file_names": [Path(item.replace("\\", "/")).name for item in files[:20]],
        "named_samples": int(reading.get("named_samples") or 0), "samples": int(reading.get("samples") or 0),
        "detail": str(reading.get("detail") or ""),
    }


def _cid_hcd_interactive_problem(pins: Mapping[str, Any], units: Iterable[Mapping[str, Any]]) -> str:
    """Why the pinned Interactive cannot run these units under the CID/HCD-only rule, or "": a unit some of whose files
    name another method needs an Interactive that keeps them out (CID_HCD_ONLY_INTERACTIVE), and one before it would
    run them. A version that cannot be read is not refused for it."""
    named = [str(unit["unit_id"]) for unit in units if (unit.get("fragmentation_reading") or {}).get("named_files")]
    version = _version(str((pins.get("interactive") or {}).get("version") or ""))
    if not named or not version or version >= CID_HCD_ONLY_INTERACTIVE:
        return ""
    return (f"{len(named)} unit(s) ({', '.join(named[:5])}{' ...' if len(named) > 5 else ''}) have analysis files whose "
            "names name a fragmentation method other than CID/HCD, which only Interactive "
            f"{'.'.join(str(part) for part in CID_HCD_ONLY_INTERACTIVE)} or later keeps out of the run; the pinned "
            f"Interactive is {(pins.get('interactive') or {}).get('version')}. Pin a later Interactive.")


def _exclusion(unit: Mapping[str, Any], reasons: list[str], detail: str = "") -> dict[str, Any]:
    record = {
        "unit_id": unit["unit_id"], "repository": unit["repository"], "accession": unit["accession"],
        "reason": reasons[0], "reasons": reasons, "selection_basis": unit.get("pool"),
    }
    if detail:
        record["detail"] = detail
    return record


def _totals(
    selected: list[dict[str, Any]], excluded: list[dict[str, Any]], units: list[dict[str, Any]],
    groups: list[dict[str, Any]], objects: list[dict[str, Any]],
) -> dict[str, Any]:
    by_reason: dict[str, int] = {}
    for item in excluded:
        by_reason[item["reason"]] = by_reason.get(item["reason"], 0) + 1
    unknown = [item for item in objects if not item["size_known"]]
    kinds: dict[str, int] = {}
    pools: dict[str, int] = {}
    for unit in units:
        kinds[unit["class_kind"]] = kinds.get(unit["class_kind"], 0) + 1
        pools[unit["selection_basis"]] = pools.get(unit["selection_basis"], 0) + 1
    return {
        "selected_units": len(selected),
        "excluded_units": len(excluded),
        "excluded_by_reason": dict(sorted(by_reason.items())),
        "planned_units": len(units),
        "download_groups": len(groups),
        "distinct_objects": len(objects),
        "distinct_bytes_known": sum(item["known_bytes"] for item in objects if item["size_known"]),
        "distinct_bytes_lower_bound": sum(item["known_bytes"] for item in objects),
        "unknown_size_objects": len(unknown),
        "empty_digest_objects": sum(
            1 for item in unknown if item.get("empty_digest_paths") == item.get("unknown_size_paths")
        ),
        "units_of_unknown_size": sum(1 for unit in units if not unit["size_known"]),
        "units_with_archives": sum(1 for unit in units if unit["has_archive"]),
        "per_unit_known_bytes": sum(unit["known_bytes"] for unit in units),
        "class_kinds": dict(sorted(kinds.items())),
        "planned_by_pool": dict(sorted(pools.items())),
    }


def write_manifest(path: Path, manifest: Mapping[str, Any]) -> str:
    """Write the manifest as its canonical bytes; returns its digest."""
    from .ports import write_json_atomic

    write_json_atomic(path, manifest, canonical=True)
    return digest_of(path.read_bytes())


def read_manifest(path: Path) -> tuple[dict[str, Any], str]:
    data = Path(path).read_bytes()
    return json.loads(data.decode("utf-8")), digest_of(data)


# ---- the approval ------------------------------------------------------------------------------------------

def automatic_rt_statement_missing_problems(manifest: Mapping[str, Any]) -> list[str]:
    """Why a manifest that carries no automatic RT correction statement (one planned before the plan stated it)
    cannot be approved: always one problem, telling the person to plan again.

    Its digest covers no words about the correction, so a person who approves it has read none. Every such manifest
    is refused, with no exception (the user's answer of 2026-10-08, R2-4: the default is correction ON, so a manifest
    that says nothing is not read as "uncorrected"; one planned now with the default policy states the correction
    on). The problem names, where there are any, what the digest does cover that bears on the correction:
    - a recorded policy that names any of the fields decided on 2026-10-07 (automatic_rt_correction, its anchors
      or its fallback), pinning the correction on, turning it off, or recording a setting of it;
    - a profile that turns the correction on anywhere, as Interactive reads it (automatic_rt_correction_requested);
    - a profile that turns the anchor-library RT correction on anywhere (execute_rt_correction, read the same way).
    Where none of those holds, the units would run with no RT correction, Interactive's default for both, against
    the campaign's default of correction ON, and nobody read that either.
    """
    recorded = manifest.get("policy") if isinstance(manifest.get("policy"), Mapping) else {}
    named = sorted(key for key in AUTOMATIC_RT_DECISION if key in recorded)
    reasons = []
    if named:
        if policy.automatic_rt_correction_pinned(recorded):
            state = "pins it on"
        elif recorded.get("automatic_rt_correction") is False:
            state = "turns it off"
        else:
            state = "records it"
        reasons.append(f"its campaign policy {state} ({', '.join(named)})")
    if automatic_rt_correction_requested(manifest.get("profile")):
        reasons.append("its profile turns it on")
    if automatic_rt_correction_requested(manifest.get("profile"), ANCHOR_LIBRARY_RT_ANSWER):
        reasons.append("its profile turns the anchor-library RT correction on")
    if not reasons:
        # What runs is no RT correction at all, which the campaign's default (correction ON) is not.
        reasons.append("nothing it records turns either RT correction on, so its units would run uncorrected, "
                       "against the campaign's default of correction ON")
    return [
        "the manifest carries no automatic RT correction statement (it was planned before the plan stated the "
        f"correction), and {' and '.join(reasons)}, so the digest a person approves covers no words saying how its "
        "units are RT corrected: plan again (campaign-runner.py plan), read the automatic RT correction in the new "
        "summary, and approve the new digest"
    ]


def approval_problems(manifest: Mapping[str, Any], covers: Iterable[str]) -> list[str]:
    """Why a manifest cannot be approved as it stands, or nothing."""
    problems = []
    libraries = manifest.get("pins", {}).get("libraries") or []
    if not libraries:
        problems.append("the manifest pins no library: plan again with --resources")
    problems.extend(profile_problems(manifest.get("profile"), [item["name"] for item in libraries]))
    problems.extend(annotation_problems(manifest))
    problems.extend(automatic_rt_correction_problems(
        manifest.get("profile"), manifest.get("pins", {}).get("console"), manifest.get("policy")
    ))
    stated = manifest.get("automatic_rt_correction")
    if stated is None:
        problems.extend(automatic_rt_statement_missing_problems(manifest))
    elif stated != automatic_rt_correction_record(
            manifest.get("policy"), manifest.get("profile"), manifest.get("policy_overrides") or []):
        # The words a person reads must be the policy and profile the runner reads.
        problems.append("the manifest's automatic RT correction statement does not match its policy and profile: plan again")
    for name in ("console", "extractor"):
        pin = manifest.get("pins", {}).get(name) or {}
        if not pin.get("exists") or not pin.get("binary_sha256"):
            problems.append(f"the manifest pins no {name} binary")
    extractor = manifest.get("pins", {}).get("extractor") or {}
    if extractor.get("exists") and extractor.get("binary_sha256") and not (
        extractor.get("provenance_status") == "verified" and extractor.get("pinned") is True
    ):
        # Interactive 0.5.17 refuses a campaign preflight by any other extractor, so every unit would stop there.
        problems.append(
            f"the extractor is not a verified build of one of Interactive's PINNED_BUILDS (provenance "
            f"{extractor.get('provenance_status') or 'unknown'}, pinned {extractor.get('pinned')})"
        )
    problems.extend(policy.pin_problems(manifest.get("pins") or {}))
    if not manifest.get("units"):
        problems.append("the manifest plans no unit")
    covered = {str(item) for item in covers}
    bad = sorted(covered - {"1", "3", "4", "5", "split"})
    if bad:
        problems.append(f"a campaign approval covers 1, 3, 4, 5 and split only; not {', '.join(bad)}")
    if "5" in covered and manifest.get("raw_retention_policy") != "delete_after_validated_output":
        problems.append("boundary 5 is covered only for a manifest whose retention is delete_after_validated_output")
    return problems


def authorization_record(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    digest: str,
    *,
    approval_id: str,
    approved_by: str,
    approved_at: str,
    statement: str,
    covers: Iterable[str],
) -> dict[str, Any]:
    """The msdial-campaign-authorization.v1 record Interactive and the Catalog check each crossing against.

    The approval id is the one the person gave when they approved; it is never made up here.
    """
    boundaries: list[Any] = []
    for item in covers:
        text = str(item).strip()
        boundaries.append(int(text) if text.isdecimal() else text)
    contract = annotation_contract(manifest)
    return {
        "schema": AUTHORIZATION_SCHEMA,
        "approval_id": approval_id,
        "campaign_id": manifest["campaign_id"],
        "manifest_digest": digest,
        "campaign_manifest_path": str(manifest_path),
        "approved_by": approved_by,
        "approved_at": approved_at,
        "statement": statement,
        "covers": boundaries,
        "units": [unit["unit_id"] for unit in manifest["units"]],
        "raw_retention_policy": manifest["raw_retention_policy"],
        # Each library by name and sha256, with the role it is loaded in and the ion modes whose units load it where
        # the plan recorded them (library_pins), and an MSP's library kind (acquired, or predicted for the in-silico
        # tier): the gate's LIB-2 and LIB-3 read them, and Interactive refuses only a location.
        "libraries": [{"name": item["name"], "sha256": item["sha256"],
                       **{key: item[key] for key in ("role", "ion_modes", "library_kind") if key in item}}
                      for item in manifest["pins"]["libraries"]],
        # What each ion mode's units annotate with, as the manifest states it (annotation_contract): LIB-2 holds the
        # method to it, so cutoffs, priorities or a profile other than the approved ones stop the run.
        **({"annotation": contract} if contract is not None else {}),
        "revoked_at": None,
    }


def ledger_rows(manifest: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The ledger's unit and download_group rows for an approved manifest."""
    units = [
        {
            "unit_key": unit["unit_id"], "catalog_unit_id": unit["unit_id"], "repository": unit["repository"],
            "accession": unit["accession"], "order_index": unit["order_index"], "download_group_id": unit["group_id"],
            "approved_class_digest": unit["approved_class_digest"], "class_kind": unit["class_kind"],
            "known_bytes": int(unit["known_bytes"] or 0), "size_known": int(bool(unit["size_known"])),
            "has_archive": int(bool(unit["has_archive"])), "instrument": unit["instrument"], "ion_mode": unit["ion_mode"],
        }
        for unit in manifest["units"]
    ]
    groups = [
        {"group_id": group["group_id"], "unit_count": group["unit_count"], "known_bytes": int(group["known_bytes"]),
         "size_known": bool(group["size_known"])}
        for group in manifest["groups"]
    ]
    return units, groups


def summary_text(manifest: Mapping[str, Any], digest: str) -> str:
    """What a person approves. The transfer is printed as the pinned Interactive's lease makes it, which is
    the approval quantity: until the lease fetches through the download store (pins.interactive
    .lease_uses_store, plan item 15) each unit fetches its own objects, so the bytes moved and held are the
    per-unit sum, and the distinct bytes are what sharing would move. Each shared object is said to be fetched
    once only where the runner also claims it for every sibling before the first of them downloads
    (pins.interactive.pre_claim_downloads, 2026-10-24); without that claim the distinct bytes are the least the
    transfer can be, and the per-unit sum the most."""
    totals = manifest["totals"]
    version = str((manifest.get("pins", {}).get("interactive") or {}).get("version") or "?")

    def tb(value: int) -> str:
        return f"{value / 1000**4:.2f} TB"

    correction = manifest_automatic_rt_correction(manifest)
    lines = [f"Campaign {manifest['campaign_id']} ({manifest['pool']} pool): {SELECTION_RULES[manifest['pool']]}"]
    # A correction that differs from the decision is said first, then again with the rest of the correction.
    lines += [line for line in correction["statement"] if line.lstrip().startswith("!!")]
    lines += [
        f"  selected {totals['selected_units']}, excluded {totals['excluded_units']}, planned {totals['planned_units']}",
    ]
    if manifest["pool"] == PILOT:
        lines.append("  planned by pool: " + (", ".join(
            f"{name} {count}" for name, count in (totals.get("planned_by_pool") or {}).items()) or "none"))
    for reason, count in totals["excluded_by_reason"].items():
        lines.append(f"    excluded {reason}: {count}")
    evidence = (manifest.get("selection") or {}).get("ion_mobility_evidence")
    if evidence:
        lines.append("  ion mobility read from " + (
            "the Catalog's ion_mobility_evidence" if evidence == "catalog"
            else "the unit's instrument, its rows' instrument fields and its inputs' container formats (no Catalog helper)"))
    if (manifest.get("selection") or {}).get("fragmentation_rule"):
        lines.append("  CID/HCD only (user decision, 2026-10-29): every reference spectrum is CID/HCD-derived, so a unit "
                     "whose own record, every sample row or every analysis file name names another fragmentation "
                     "method is excluded, and a file named so is kept out by Interactive's lease; absence of evidence "
                     "means run, and no vendor header records the method")
    partly = [unit for unit in manifest.get("units") or [] if (unit.get("fragmentation_reading") or {}).get("named_files")]
    if partly:
        lines.append(f"  {len(partly)} planned unit(s) run without some files whose names name another fragmentation "
                     "method: " + ", ".join(f"{unit['unit_id']} ({unit['fragmentation_reading']['named_files']} of "
                                            f"{unit['fragmentation_reading']['analysis_files']})" for unit in partly[:5])
                     + (", ..." if len(partly) > 5 else ""))
    both = [unit["unit_id"] for unit in manifest.get("units") or [] if unit.get("ion_mode") == "Both"]
    if both:
        lines.append(f"  {len(both)} planned unit(s) declare Both polarities: each file's polarity is read from its raw "
                     "header and the unit is split into single-polarity parts; a file whose scans switch polarity is "
                     "excluded (unresolved mixed polarity is out of scope) and the rest runs")
    unread = [unit["unit_id"] for unit in manifest.get("units") or []
              if (unit.get("ion_mobility_reading") or {}).get("evidence") == "not_read"]
    if unread:
        lines.append(f"  ion mobility not read for {len(unread)} planned units whose Catalog record could not be read, "
                     f"so not excluded for it ({', '.join(unread[:5])}{', ...' if len(unread) > 5 else ''})")
    # Where the raw data and outputs go, and whether the raw data stay (boundary 1 states both). The root is chosen per
    # campaign since 2026-10-24, and until the review of 2026-10-25 only the manifest named it, never these words.
    root = str(manifest.get("workspace_root") or "")
    lines.append(f"  workspace root {root}: every unit's raw data, MS-DIAL outputs and records, and this campaign's "
                 "ledger, are written under it" if root else "  workspace root: not stated")
    cautions = [str(item) for item in manifest.get("workspace_root_cautions") or []]
    if cautions:
        where = cautions[0] if len(cautions) == 1 else ", ".join(cautions[:-1]) + " and " + cautions[-1]
        lines.append(f"  THE WORKSPACE ROOT IS {where.upper()}, where the project keeps no campaign data: approve it only "
                     "if that is meant")
    retention = str(manifest.get("raw_retention_policy") or "")
    # Boundary 5 covers more than the validated case (review 2): the runner discards the raw data of a unit that ends
    # without validated outputs too (Runner._discard_raw), as decided on 2026-09-30.
    lines.append(f"  raw retention {retention or 'not stated'}" + (
        ": each unit's raw data are deleted when the unit ends (boundary 5): once its outputs are validated, and also "
        "when it ends without validated outputs (failed after its retries, excluded or skipped), so they are not kept "
        "for diagnosis" if retention == "delete_after_validated_output"
        else ": each unit's raw data are kept" if retention == "keep" else ""))
    lower = " at least" if totals["units_of_unknown_size"] else ""
    lines.append(f"  download groups {totals['download_groups']}, distinct objects {totals['distinct_objects']}")
    interactive = manifest.get("pins", {}).get("interactive") or {}
    if interactive.get("lease_uses_store") is True and interactive.get("pre_claim_downloads") is True:
        lines += [
            f"  transfer and disk:{lower} {tb(totals['distinct_bytes_known'])} of known size, each shared object fetched "
            f"once through Interactive {version}'s download store; lower bound {tb(totals['distinct_bytes_lower_bound'])}",
            "  before a unit of a download group downloads, the runner claims the group's objects in the store for every "
            "unit of it still to download, so no unit's cleanup deletes what a sibling has yet to read",
            f"  without the store the units would fetch {tb(totals['per_unit_known_bytes'])}",
        ]
    elif interactive.get("lease_uses_store") is True:
        # The store, and nothing that claims an object for a unit that has not started (a manifest planned before the
        # runner pre-claimed, 2026-10-24): an earlier sibling's cleanup may delete what the next one then fetches again.
        lines += [
            f"  transfer and disk:{lower} {tb(totals['distinct_bytes_known'])} of known size where no shared object is "
            f"fetched twice, up to {tb(totals['per_unit_known_bytes'])}: Interactive {version}'s lease fetches through "
            "the download store, but nothing claims an object for a unit that has not started, so once an earlier "
            "unit's raw data are deleted the next unit of its group may fetch it again",
        ]
    else:
        lines += [
            f"  transfer and disk:{lower} {tb(totals['per_unit_known_bytes'])}, each unit fetching its own copy of what "
            f"units share (Interactive {version}'s lease does not share objects)",
            f"  distinct bytes, once the lease shares objects through the download store: {tb(totals['distinct_bytes_known'])} "
            f"of known size; lower bound {tb(totals['distinct_bytes_lower_bound'])}",
        ]
    lines += [
        f"  unknown sizes: {totals['unknown_size_objects']} objects "
        f"({totals['empty_digest_objects']} declare the digest of zero bytes), {totals['units_of_unknown_size']} units",
        f"  units with archives {totals['units_with_archives']}; Class {totals['class_kinds']}",
    ]
    legacy = manifest.get("legacy_accession_workspaces") or []
    if legacy:
        lines.append(
            f"  planned beside an earlier accession-level workspace, left as it is: {len(legacy)} accessions "
            f"({', '.join(legacy[:5])}{', ...' if len(legacy) > 5 else ''})"
        )
    annotation = manifest.get("annotation")
    if isinstance(annotation, Mapping) and isinstance(annotation.get("statement"), list):
        lines += [str(line) for line in annotation["statement"]]
    else:
        lines.append("  libraries and annotation: not stated (the manifest was planned before the plan stated them)")
    lines += template_pin_text(manifest)
    lines += correction["statement"]
    lines.append(multi_energy_aif_text((manifest.get("pins") or {}).get("console") or {}))
    lines.append(f"  manifest digest {digest}")
    return "\n".join(lines)


def template_pin_text(manifest: Mapping[str, Any]) -> list[str]:
    """What the summary says of the LC-MS parameter templates the units take and what holds each: one the answers of
    an ion mode name, as library:<file name>, is pinned with the libraries; the template pin holds the one the other
    modes' units take (ports.PinReader.template). The template pin was said to be the run's template for a profile
    whose answers named another (review 3)."""
    profile = manifest.get("profile") if isinstance(manifest.get("profile"), Mapping) else None
    answered = {mode: str(value) for mode, value in answered_templates(profile).items()}
    lines = []
    for value in sorted(set(answered.values())):
        modes = " and ".join(mode for mode in ION_MODES if answered.get(mode) == value)
        name = _one_library(value)
        lines.append(f"  LC-MS parameter template of {modes} units {name}, which the answers name, pinned as a library "
                     "by its sha256: a change to it pauses the campaign before the next unit or Console start" if name
                     else f"  LC-MS parameter template of {modes} units {value}, which the answers name other than as "
                     "library:<file name>: NOT PINNED, so the plan is not approvable")
    others = [mode for mode in ION_MODES if mode not in answered]
    if not others:
        return lines
    template = (manifest.get("pins") or {}).get("template") or {}
    whom = f" of {' and '.join(others)} units" if answered else ""
    if template.get("sha256"):
        lines.append(f"  LC-MS parameter template{whom} {template.get('name')}"
                     f"{TEMPLATE_WHOSE.get(str(template.get('named_by')), '')}, pinned by its sha256 "
                     f"{str(template['sha256'])[:12]}...: a change to it pauses the campaign before the next unit or "
                     "Console start")
    else:
        lines.append(f"  LC-MS parameter template{whom}: not pinned, so a change to it is seen only by LIB-2, unit by unit")
    return lines


def manifest_automatic_rt_correction(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The manifest's automatic RT correction record (automatic_rt_correction_record), as the plan wrote it; for a
    manifest planned before the record, made from its recorded policy, which overrides it does not say."""
    record = manifest.get("automatic_rt_correction")
    if isinstance(record, Mapping) and isinstance(record.get("statement"), list):
        return dict(record)
    return automatic_rt_correction_record(manifest.get("policy"), manifest.get("profile"), manifest.get("policy_overrides"))


def multi_energy_aif_text(console: Mapping[str, Any]) -> str:
    """What the pinned Console means for a multi-energy AIF unit, as its pin records Interactive's probe for
    MsdialWorkbench #825 (ports.PinReader, multi_energy_aif). Never a reason to refuse the plan: without #825
    Interactive holds such a unit, which is correct, and with it Interactive runs it as AIF."""
    record = console.get("multi_energy_aif")
    if not isinstance(record, Mapping):
        return ("  multi-energy AIF: not probed (the pinned Interactive predates 0.5.34), so such units are held "
                f"({policy.HOLD_FOR_CONSOLE})")
    if record.get("available") is True:
        return ("  multi-energy AIF: the pinned Console has MsdialWorkbench #825, so a unit whose AIF inputs record the "
                f"same energies, more than one, runs as AIF ({policy.AIF_MULTI_CE_RULE})")
    return (f"  multi-energy AIF: the pinned Console has no MsdialWorkbench #825 (probe {record.get('probe') or 'none'}), "
            f"so such units are held ({policy.HOLD_FOR_CONSOLE})")
