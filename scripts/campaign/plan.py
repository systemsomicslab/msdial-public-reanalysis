"""The campaign manifest: which units, in what order, pinned to what, built read-only from the Catalog.

ONE MANIFEST PER POOL. The declared pool comes first: LC-MS units whose repository record declares DDA,
DIA, AIF or SWATH acquisition, does not say the study is targeted, and names one polarity. The
acquisition-unknown pool is its own manifest, and its own approval: the same rules with the acquisition
mode left Unknown by the repository, which the raw headers settle after download (Interactive's
campaign_disposition says whether each unit then runs).

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

THE AUTOMATIC RT CORRECTION IS STATED IN WHAT A PERSON APPROVES. The campaign runs it on, with at most 12 anchors,
#826's local window (the Console's 1.5 min) and an uncorrected fallback after an anchor-selection failure, Blanks
interpolated only by a recorded injection order (decided 2026-10-07). A --policy override may change that, and is
not refused for it, but the manifest names the fields the override set (policy_overrides) and keeps the
correction as the summary states it, words included (automatic_rt_correction), so the digest covers what the
person read; where it differs from the decision the summary says so first, in capitals. A manifest planned before
that record carries no statement, and approve refuses it wherever its policy names the correction's fields or its
profile turns the correction, or the anchor-library RT correction, on, telling the person to plan again
(automatic_rt_statement_missing_problems). The statement also says where the profile turns the anchor-library
correction on, and every other setting of the automatic correction the profile gives; under the policy's pin a
profile that gives any of those is refused, since the decision is #826 with Interactive's defaults.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import policy

MANIFEST_SCHEMA = "msdial-campaign-manifest.v1"
PROFILE_SCHEMA = "msdial-campaign-profile.v1"
AUTHORIZATION_SCHEMA = "msdial-campaign-authorization.v1"
POOLS = ("declared", "acquisition_unknown")
# A manifest of named units from both pools (plan --units), each held to its own pool's rules.
PILOT = "pilot"
_POOL_WHERE = {
    "declared": (
        "u.separation = 'LC-MS' AND u.acquisition_mode IN ('DDA', 'DIA', 'AIF', 'SWATH') "
        "AND (u.untargeted IS NULL OR u.untargeted <> 0) AND u.ion_mode IN ('Positive', 'Negative')"
    ),
    "acquisition_unknown": (
        "u.separation = 'LC-MS' AND u.acquisition_mode = 'Unknown' "
        "AND (u.untargeted IS NULL OR u.untargeted <> 0) AND u.ion_mode IN ('Positive', 'Negative')"
    ),
}
SELECTION_RULES = {
    "declared": "separation LC-MS; acquisition DDA, DIA, AIF or SWATH; untargeted not false; one polarity",
    "acquisition_unknown": "separation LC-MS; acquisition Unknown (read from the raw headers); untargeted not false; one polarity",
}
SELECTION_RULES[PILOT] = "the analysis units named with --units, each held to the rules of the pool it is in"
EXCLUSION_REASONS = (
    "no_files", "ion_mobility", "preexisting_workspace", "campaign_workspace", "mzdata_only", "no_download_object",
    "class_undecided",
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
    ion_mobility: Mapping[str, Any] | None = None,
) -> list[str]:
    """Every static reason this unit will not run, in EXCLUSION_REASONS order.

    `ion_mobility` is ion_mobility_reading() of the unit's Catalog record; without it the unit is not excluded
    for ion mobility, since the plan's row holds neither its sample rows nor its inputs (unread_ion_mobility)."""
    reasons = []
    if not unit["file_count"]:
        reasons.append("no_files")
    if ion_mobility is not None and ion_mobility["excluded"]:
        reasons.append("ion_mobility")
    workspace, _detail = workspace_exclusion(unit, workspace_root, replan)
    if workspace:
        reasons.append(workspace)
    paths = unit.get("analysis_paths") or []
    if paths and all(path.endswith(MZDATA_SUFFIXES) for path in paths):
        reasons.append("mzdata_only")
    return reasons


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
    for text in _strings(profile):
        if _ABSOLUTE.search(text):
            problems.append("the profile names a location; name libraries as library:<file name>")
            break
    missing = sorted(library_references(profile) - set(library_names))
    if missing:
        problems.append(f"the profile names libraries the plan did not pin: {', '.join(missing)}")
    return problems


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
) -> dict[str, Any]:
    """The manifest for one pool, or for a pilot of named units (`unit_ids`, pool "pilot"). `catalog` is a
    read-only Catalog; nothing is written anywhere.

    `replan` is replan_states() of the prior campaigns whose unfinished units may be planned again.
    `ion_mobility_evidence` is the Catalog's helper, None for the fallback rule, or DETECT to look it up.
    `policy_overrides` names the policy fields a --policy file set, so the manifest can say what it changed.
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
    for unit in selected:
        try:
            record: Mapping[str, Any] = catalog.get_unit(unit["unit_id"])
        except Exception as error:  # noqa: BLE001 - not read, so not excluded for it; the manifest says so
            mobility[unit["unit_id"]] = unread_ion_mobility(f"{type(error).__name__}: {error}")
        else:
            mobility[unit["unit_id"]] = ion_mobility_reading(record, evidence)
        reasons = exclusion_reasons(unit, workspace_root, replan, ion_mobility=mobility[unit["unit_id"]])
        if reasons:
            _reason, found = workspace_exclusion(unit, workspace_root, replan)
            said = [f"ion_mobility: {mobility[unit['unit_id']]['detail']}"] if "ion_mobility" in reasons else []
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
        "raw_retention_policy": raw_retention_policy,
        "catalog": {"database": catalog_database, "version": catalog_version},
        "selection": {
            "pool": pool, "rule": SELECTION_RULES[pool],
            "exclusion_reasons": list(PILOT_EXCLUSION_REASONS if pool == PILOT else EXCLUSION_REASONS),
            # Which reading of ion mobility excluded units: the Catalog's ion_mobility_evidence, or the
            # fallback rule without it (option A, 2026-10-03).
            "ion_mobility_evidence": "fallback" if evidence is None else "catalog",
            **({"units": requested, "pools": {name: SELECTION_RULES[name] for name in POOLS}} if pool == PILOT else {}),
        },
        "policy": campaign_policy.as_dict(),
        # The policy fields a --policy file set (empty: the default policy), and the automatic RT correction as
        # the summary states it, words included, so the digest a person approves covers both.
        "policy_overrides": policy_overrides,
        "automatic_rt_correction": automatic_rt_correction_record(campaign_policy.as_dict(), profile, policy_overrides),
        "profile": dict(profile) if profile is not None else None,
        "pins": dict(pins),
        "units": units,
        "groups": used_groups,
        "exclusions": excluded,
        "totals": totals,
        # Planned units whose accession folder is an earlier accession-level run's workspace: their own
        # workspaces are made beside it, and it is left as it is.
        "legacy_accession_workspaces": legacy,
        **({"replanned_from": sorted(replan)} if replan else {}),
    }


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
    cannot be approved, or nothing.

    Its digest covers no words about the correction, so a person who approves it has read none. That is refused
    wherever what the digest does cover could run the correction, or records a decision about it:
    - a recorded policy that names any of the fields decided on 2026-10-07 (automatic_rt_correction, its anchors
      or its fallback): the plan was made by a runner that pins the correction, on or deliberately off, and the
      summary a person read said neither (nor that "off" differs from the decision);
    - a profile that turns the correction on anywhere, as Interactive reads it (automatic_rt_correction_requested):
      its units would run corrected, unpinned and with no fallback, and nobody read that either;
    - a profile that turns the anchor-library RT correction on anywhere (execute_rt_correction, read the same way):
      Interactive runs that correction, with the profile's anchor library, whatever the policy says, and the
      plan of that time stated it nowhere.
    A manifest whose policy names none of those fields and whose profile turns neither correction on stays
    approvable: every unit runs with no RT correction, Interactive's default for both, which is what the runner
    does with any policy recorded before 2026-10-07 (policy.automatic_rt_correction_pinned), and nothing it covers
    says otherwise. Settings of the automatic correction (its anchors, window or other settings) without its switch
    change nothing that runs.
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
        return []
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
        "libraries": [{"name": item["name"], "sha256": item["sha256"]} for item in manifest["pins"]["libraries"]],
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
    per-unit sum, and the distinct bytes are what sharing would move."""
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
    unread = [unit["unit_id"] for unit in manifest.get("units") or []
              if (unit.get("ion_mobility_reading") or {}).get("evidence") == "not_read"]
    if unread:
        lines.append(f"  ion mobility not read for {len(unread)} planned units whose Catalog record could not be read, "
                     f"so not excluded for it ({', '.join(unread[:5])}{', ...' if len(unread) > 5 else ''})")
    lower = " at least" if totals["units_of_unknown_size"] else ""
    lines.append(f"  download groups {totals['download_groups']}, distinct objects {totals['distinct_objects']}")
    if (manifest.get("pins", {}).get("interactive") or {}).get("lease_uses_store") is True:
        lines += [
            f"  transfer and disk:{lower} {tb(totals['distinct_bytes_known'])} of known size, each shared object fetched "
            f"once through Interactive {version}'s download store; lower bound {tb(totals['distinct_bytes_lower_bound'])}",
            f"  without the store the units would fetch {tb(totals['per_unit_known_bytes'])}",
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
    lines += correction["statement"]
    lines.append(multi_energy_aif_text((manifest.get("pins") or {}).get("console") or {}))
    lines.append(f"  manifest digest {digest}")
    return "\n".join(lines)


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
