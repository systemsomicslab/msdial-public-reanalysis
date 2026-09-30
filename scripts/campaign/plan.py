"""The campaign manifest: which units, in what order, pinned to what, built read-only from the Catalog.

ONE MANIFEST PER POOL. The declared pool comes first: LC-MS units whose repository record declares DDA,
DIA, AIF or SWATH acquisition, does not say the study is targeted, and names one polarity. The
acquisition-unknown pool is its own manifest, and its own approval: the same rules with the acquisition
mode left Unknown by the repository, which the raw headers settle after download (Interactive's
campaign_disposition says whether each unit then runs).

WHAT IS LEFT OUT, AND WHY, IS PART OF WHAT IS APPROVED. Every selected unit the plan will not run is
listed with its reason:

- no_files: the Catalog lists no raw file for it, so there is nothing to download (111 declared units);
- ion_mobility: ion mobility Enabled, or a TIMS, Synapt, Vion, 6560 or Cyclic instrument. This campaign
  is LC-MS only; LC-IM-MS is excluded this time (decided 2026-09-30);
- preexisting_workspace: <workspace_root>\\<repository>\\<accession> already exists (MTBLS2207, MTBLS341,
  ST002419, MPST000007, MPST000008). The runner never writes into a workspace it did not make;
- mzdata_only: every analysis file is mzData, which MS-DIAL cannot read and nothing here converts
  (mzXML is converted by Interactive and runs);
- no_download_object: files are listed, but none carries a URL;
- class_undecided: the Catalog neither proposed a Class nor recorded an abstention.

THE BYTES are the Catalog's download_plan (Catalog 0.6.0): distinct objects, each fetched once, with
size_known false for an object the repository listed without a size. Such a unit's bytes are a lower
bound, never 0, and the disk guard reserves room for it (policy.DiskPolicy).

THE DIGEST is sha256 over the manifest's canonical JSON (sorted keys, no spaces, UTF-8), and the file on
disk is exactly those bytes, so Interactive's campaign_authorization can check a manifest against the
digest a person approved by hashing the file. Nothing in it names a private library's location:
libraries are pinned by file name, sha256 and size.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import policy

MANIFEST_SCHEMA = "msdial-campaign-manifest.v1"
PROFILE_SCHEMA = "msdial-campaign-profile.v1"
AUTHORIZATION_SCHEMA = "msdial-campaign-authorization.v1"
POOLS = ("declared", "acquisition_unknown")
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
EXCLUSION_REASONS = (
    "no_files", "ion_mobility", "preexisting_workspace", "mzdata_only", "no_download_object", "class_undecided",
)
ION_MOBILITY_INSTRUMENT = re.compile(r"tims|synapt|vion|6560|cyclic", re.IGNORECASE)
MZDATA_SUFFIXES = (".mzdata", ".mzdata.xml")
ANALYSIS_ROLES = ("raw", "converted")
ARCHIVE_KINDS = frozenset({"archive", "bundle"})
# A drive path, a UNC path or a file:// URI: none belongs in a manifest a person approves and Interactive
# copies into every unit's provenance.
_ABSOLUTE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|\\\\[^\\\s]|file://", re.IGNORECASE)
MINIMUM_CATALOG_VERSION = (0, 6, 0)


class PlanError(ValueError):
    """The plan cannot be made as asked; nothing was written."""


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", str(text or ""))[:3])


def canonical_bytes(manifest: Mapping[str, Any]) -> bytes:
    return policy.canonical_json(manifest)


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---- selection and exclusions --------------------------------------------------------------------------

def select_units(connection: sqlite3.Connection, pool: str) -> list[dict[str, Any]]:
    """Every unit of the pool, with what the exclusions read. Read-only."""
    if pool not in _POOL_WHERE:
        raise PlanError(f"Unknown pool {pool!r}; choose one of {', '.join(POOLS)}.")
    rows = connection.execute(
        "SELECT u.unit_id, s.repository, s.accession, u.instrument, u.ion_mode, u.acquisition_mode, "
        "u.ion_mobility, u.untargeted, u.target_omics, u.chromatography, "
        "(SELECT COUNT(*) FROM raw_file r WHERE r.unit_id = u.unit_id) AS file_count "
        f"FROM analysis_unit u JOIN study s ON s.study_id = u.study_id WHERE {_POOL_WHERE[pool]} "
        "ORDER BY s.repository, s.accession, u.unit_id"
    ).fetchall()
    units = [dict(zip(("unit_id", "repository", "accession", "instrument", "ion_mode", "acquisition_mode",
                       "ion_mobility", "untargeted", "target_omics", "chromatography", "file_count"), row))
             for row in rows]
    suffixes: dict[str, list[str]] = {}
    for unit_id, path in connection.execute(
        f"SELECT r.unit_id, r.path FROM raw_file r JOIN analysis_unit u ON u.unit_id = r.unit_id "
        f"JOIN study s ON s.study_id = u.study_id WHERE {_POOL_WHERE[pool]} "
        f"AND r.role IN ({', '.join('?' for _ in ANALYSIS_ROLES)})",
        ANALYSIS_ROLES,
    ):
        suffixes.setdefault(str(unit_id), []).append(str(path).casefold().rstrip("/\\"))
    for unit in units:
        unit["analysis_paths"] = suffixes.get(unit["unit_id"], [])
    return units


def exclusion_reasons(unit: Mapping[str, Any], workspace_root: Path) -> list[str]:
    """Every static reason this unit will not run, in EXCLUSION_REASONS order."""
    reasons = []
    if not unit["file_count"]:
        reasons.append("no_files")
    if str(unit["ion_mobility"] or "") == "Enabled" or ION_MOBILITY_INSTRUMENT.search(str(unit["instrument"] or "")):
        reasons.append("ion_mobility")
    if (workspace_root / str(unit["repository"]) / str(unit["accession"])).is_dir():
        reasons.append("preexisting_workspace")
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


# ---- the manifest ---------------------------------------------------------------------------------------

def build_manifest(
    catalog: Any,
    *,
    pool: str,
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
) -> dict[str, Any]:
    """The manifest for one pool. `catalog` is a read-only Catalog; nothing is written anywhere."""
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
    selected = select_units(catalog.connection, pool)
    say(f"{len(selected)} units selected for the {pool} pool")
    excluded: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for unit in selected:
        reasons = exclusion_reasons(unit, workspace_root)
        if reasons:
            excluded.append(_exclusion(unit, reasons))
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
        units.append({
            "unit_id": unit["unit_id"],
            "repository": unit["repository"],
            "accession": unit["accession"],
            "order_index": order_index,
            "selection_basis": pool,
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
    totals = _totals(selected, excluded, units, used_groups, objects)
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
        "selection": {"pool": pool, "rule": SELECTION_RULES[pool], "exclusion_reasons": list(EXCLUSION_REASONS)},
        "policy": campaign_policy.as_dict(),
        "profile": dict(profile) if profile is not None else None,
        "pins": dict(pins),
        "units": units,
        "groups": used_groups,
        "exclusions": excluded,
        "totals": totals,
    }


def _exclusion(unit: Mapping[str, Any], reasons: list[str], detail: str = "") -> dict[str, Any]:
    record = {
        "unit_id": unit["unit_id"], "repository": unit["repository"], "accession": unit["accession"],
        "reason": reasons[0], "reasons": reasons,
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
    for unit in units:
        kinds[unit["class_kind"]] = kinds.get(unit["class_kind"], 0) + 1
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

def approval_problems(manifest: Mapping[str, Any], covers: Iterable[str]) -> list[str]:
    """Why a manifest cannot be approved as it stands, or nothing."""
    problems = []
    libraries = manifest.get("pins", {}).get("libraries") or []
    if not libraries:
        problems.append("the manifest pins no library: plan again with --resources")
    problems.extend(profile_problems(manifest.get("profile"), [item["name"] for item in libraries]))
    for name in ("console", "extractor"):
        pin = manifest.get("pins", {}).get(name) or {}
        if not pin.get("exists") or not pin.get("binary_sha256"):
            problems.append(f"the manifest pins no {name} binary")
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
    totals = manifest["totals"]

    def tb(value: int) -> str:
        return f"{value / 1000**4:.2f} TB"

    lines = [
        f"Campaign {manifest['campaign_id']} ({manifest['pool']} pool): {SELECTION_RULES[manifest['pool']]}",
        f"  selected {totals['selected_units']}, excluded {totals['excluded_units']}, planned {totals['planned_units']}",
    ]
    for reason, count in totals["excluded_by_reason"].items():
        lines.append(f"    excluded {reason}: {count}")
    lines += [
        f"  download groups {totals['download_groups']}, distinct objects {totals['distinct_objects']}",
        f"  distinct bytes: {tb(totals['distinct_bytes_known'])} of known size; lower bound {tb(totals['distinct_bytes_lower_bound'])}",
        f"  unknown sizes: {totals['unknown_size_objects']} objects "
        f"({totals['empty_digest_objects']} declare the digest of zero bytes), {totals['units_of_unknown_size']} units",
        f"  units with archives {totals['units_with_archives']}; Class {totals['class_kinds']}",
        f"  manifest digest {digest}",
    ]
    return "\n".join(lines)
