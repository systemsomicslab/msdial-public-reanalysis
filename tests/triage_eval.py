"""The triage eval set: eight Metabolomics Workbench accessions with a known answer.

tests/fixtures/triage-eval/<accession>/ holds, for each accession the 2026-11-15 prototype triaged:

- catalog.json         the Catalog units (public analysis ids) with their declared technical settings and
                       sample rows (sample_id, raw_file), as the handoffs listed them;
- archive-members.tsv  the study archive's member listing: archive, path, size, crc32;
- ground_truth.json    which members belong to which unit, as the checker agents established it from raw
                       headers, mzML source paths, elution order and mwTab, plus the status a pre-download
                       triage should reach and the questions and flags it should raise.

build_good() turns a ground truth into the triage files a correct pre-download triage would write, so the
validator can be run on known-good files and on known-bad mutations of them. An agent's own triage of the
same accessions can be scored with scripts/validate-triage.py --ground-truth.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "triage-eval"
ACCESSIONS = ("ST002798", "ST002796", "ST004606", "ST003964", "ST002283", "ST002747", "ST004544", "ST003128")

# Where the unit assignment is stated by the repository itself (a Catalog row attribute or the mwTab
# Samples line), a correct triage marks its rows declared; elsewhere it rests on names (inferred).
DECLARED_BY = {
    "ST002798": ("catalog_row", "Mode"),
    "ST002796": ("catalog_row", "Mode"),
    "ST004606": ("catalog_row", "Chromatography ion mode"),
    "ST003128": ("mwtab", "MS_METABOLITE_DATA Samples"),
}


def fixture_dir(accession: str) -> Path:
    return FIXTURES / accession


def load(accession: str) -> tuple[dict[str, Any], dict[str, Any], Path]:
    d = fixture_dir(accession)
    catalog = json.loads((d / "catalog.json").read_text(encoding="utf-8"))
    truth = json.loads((d / "ground_truth.json").read_text(encoding="utf-8"))
    return catalog, truth, d / "archive-members.tsv"


def _stem(name: str) -> str:
    return re.sub(r"(?i)\.(raw|mzml|mzxml|wiff2?|d)$", "", name.replace("\\", "/").rsplit("/", 1)[-1])


def _evidence(accession: str, path: str, ion: str, chrom: str) -> tuple[str, list[dict[str, Any]]]:
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    evidence: list[dict[str, Any]] = []
    basis = "inferred"
    if accession in DECLARED_BY:
        source, field = DECLARED_BY[accession]
        evidence.append({"source": source, "field": field, "value": f"{chrom} {ion}"})
        basis = "declared"
    if folder:
        evidence.append({"source": "folder_token", "field": "folder", "value": folder})
    evidence.append({"source": "filename_token", "field": "name", "value": path.rsplit("/", 1)[-1]})
    return basis, evidence


def catalog_sha(unit: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(unit, sort_keys=True).encode("utf-8")).hexdigest()


def build_good(accession: str) -> dict[str, dict[str, Any]]:
    """The triage files (unit id -> document) a correct pre-download triage writes for this accession."""
    catalog, truth, _ = load(accession)
    owner: dict[str, str] = {}
    info: dict[str, dict[str, Any]] = {}
    for unit in truth["units"]:
        for m in unit["members"]:
            owner[m["path"]] = unit["unit_id"]
            info[m["path"]] = m
    excluded = {m["path"]: m["reason"] for m in truth["excluded_members"]}
    unchecked = {m["path"]: m["note"] for m in truth["unchecked_members"]}
    all_paths = list(owner) + list(excluded) + list(unchecked)
    by_name: dict[str, list[str]] = {}
    for p in all_paths:
        by_name.setdefault(p.rsplit("/", 1)[-1].casefold(), []).append(p)
        by_name.setdefault(_stem(p).casefold(), []).append(p)
    overrides = truth.get("row_member_overrides", {})

    def resolve(raw_file: str) -> str | None:
        if raw_file in overrides:
            return overrides[raw_file]
        hits = by_name.get(raw_file.casefold()) or by_name.get(_stem(raw_file).casefold()) or []
        hits = sorted(set(hits), key=lambda p: (p not in owner, p.lower().endswith(".mzxml"), p))
        return hits[0] if hits else None

    docs: dict[str, dict[str, Any]] = {}
    truth_units = {u["unit_id"]: u for u in truth["units"]}
    for index, cu in enumerate(catalog["units"]):
        uid = cu["unit_id"]
        tu = truth_units[uid]
        status = tu["expected_status_pre_download"]
        chrom = tu["chromatography"]
        rows, excluded_rows, linked = [], [], set()
        for i, row in enumerate(cu["rows"]):
            path = resolve(row["raw_file"])
            base = {"row_index": i, "sample_id": row["sample_id"], "raw_file": row["raw_file"]}
            if status == "abstained":
                excluded_rows.append({**base, "member_path": path, "reason": "no_signal_abstained", "belongs_to_unit": None,
                                      "detail": "Names carry no polarity or chromatography signal; abstained before download."})
            elif path is not None and owner.get(path) == uid:
                m = info[path]
                basis, evidence = _evidence(accession, path, m["ion_mode"], chrom)
                rows.append({**base, "member_path": path, "archive": m["archive"], "role": m["role"], "ion_mode": m["ion_mode"],
                             "chromatography": chrom, "acquisition_mode": m["acquisition_mode"], "basis": basis, "evidence": evidence})
                linked.add(path)
            elif path is not None and path in owner:
                excluded_rows.append({**base, "member_path": path, "reason": "belongs_to_other_unit", "belongs_to_unit": owner[path],
                                      "detail": f"Kept by {owner[path]}."})
            elif path is not None and excluded.get(path) == "duplicate_content":
                excluded_rows.append({**base, "member_path": path, "reason": "duplicate_content", "belongs_to_unit": None,
                                      "detail": "Byte-identical (CRC32 and size) to a member of another folder; left for a person."})
            elif path is not None and path in excluded:
                excluded_rows.append({**base, "member_path": path, "reason": "requires_conversion" if excluded[path] == "requires_conversion" else "other",
                                      "belongs_to_unit": None, "detail": f"Member excluded: {excluded[path]}."})
            else:
                excluded_rows.append({**base, "member_path": None, "reason": "not_in_archive", "belongs_to_unit": None,
                                      "detail": "No archive member carries this name."})
        added = []
        if status != "abstained":
            for m in tu["members"]:
                if m["path"] in linked:
                    continue
                basis, evidence = _evidence(accession, m["path"], m["ion_mode"], chrom)
                if basis == "declared":
                    evidence = evidence[1:]
                    basis = "inferred"
                added.append({"member_path": m["path"], "archive": m["archive"], "role": m["role"], "ion_mode": m["ion_mode"],
                              "chromatography": chrom, "acquisition_mode": m["acquisition_mode"], "basis": basis, "evidence": evidence})
        excluded_members = []
        if index == 0:
            excluded_members += [{"member_path": p, "reason": r, "belongs_to_unit": None, "detail": f"Left out: {r}."}
                                 for p, r in excluded.items()]
        excluded_members += [{"member_path": p, "reason": "other", "belongs_to_unit": None, "detail": note}
                             for p, note in unchecked.items() if index == 0]
        children = []
        if tu["ion_mode"] == "both" and status != "abstained":
            groups: dict[tuple[str, str], list[str]] = {}
            for item in rows + added:
                groups.setdefault((item["ion_mode"], item["acquisition_mode"]), []).append(item["member_path"])
            for (ion, acq), paths in sorted(groups.items()):
                children.append({"child_key": f"{ion[:3]}-{acq.lower()}", "ion_mode": ion, "acquisition_mode": acq, "member_paths": paths})
        flags = [{"code": code, "certainty": "confirmed" if code == "shared_content" else "suspected",
                  "detail": f"{code} raised from pre-download evidence."} for code in tu["expected_flags"]]
        questions = [{"code": code, "question": f"Decide {code.replace('_', ' ')} for {uid}."} for code in tu["expected_needs_person"]]
        doc = {
            "schema": "msdial-unit-triage.v1",
            "repository": catalog["repository"],
            "accession": accession,
            "unit_id": uid,
            "source_subrecord_id": cu["source_subrecord_id"],
            "catalog_handoff_sha256": catalog_sha(cu),
            "catalog_row_count": len(cu["rows"]),
            "standing": "model_inference",
            "phase": "pre_download",
            "decided_by": {"agent": "eval-fixture", "model": "ground-truth", "skill": "msdial-unit-triage", "skill_version": "1.0.0"},
            "decided_at": "2026-11-15T00:00:00Z",
            "purpose": None,
            "unit_technical": {"ion_mode": tu["ion_mode"], "chromatography": chrom, "acquisition_mode": tu["acquisition_mode"],
                               "declared": {k: cu["declared"].get(k) for k in ("chromatography", "ion_mode", "acquisition_mode")}},
            "status": status,
            "rows": rows,
            "added_members": added,
            "excluded_rows": excluded_rows,
            "excluded_members": excluded_members,
            "shared_with": [],
            "needs_person": questions,
            "flags": flags,
        }
        if children:
            doc["children"] = children
        docs[uid] = doc
    return docs


def to_v2(docs: dict[str, dict[str, Any]], skill_version: str = "1.1.0") -> dict[str, dict[str, Any]]:
    """The same triage written as msdial-unit-triage.v2 (skill 1.1.0): a confidence and a rationale on every kept and
    excluded row and member and on the unit, and no person_answers (a person's answer is a review-log entry). A kept file
    resting on a declaration is high confidence, one resting on names medium; an exclusion is high."""
    out = copy.deepcopy(docs)
    for doc in out.values():
        doc["schema"] = "msdial-unit-triage.v2"
        doc["decided_by"]["skill_version"] = skill_version
        doc.pop("person_answers", None)
        for item in [*doc["rows"], *doc.get("added_members", [])]:
            item["confidence"] = "high" if item["basis"] == "declared" else "medium"
            first = item["evidence"][0]
            item["rationale"] = f"Kept: {first['source']} {first['field']} reads {first['value']}."
        for item in [*doc["excluded_rows"], *doc.get("excluded_members", [])]:
            item["confidence"] = "high"
            item["rationale"] = f"Left out ({item['reason']}): {item['detail']}"
        doc["confidence"] = "high" if doc["status"] == "ready" else "medium"
        doc["rationale"] = f"{doc['unit_id']} is {doc['status']}: {len(doc['rows'])} row(s) kept, {len(doc['excluded_rows'])} left out."
    return out


def write(docs: dict[str, dict[str, Any]], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for uid, doc in docs.items():
        (directory / f"{uid}.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    return directory


def stage3b_style(accession: str) -> dict[str, dict[str, Any]]:
    """What stage3b did: every unit keeps every one of its Catalog rows (the whole study), marked ready."""
    catalog, truth, _ = load(accession)
    docs = build_good(accession)
    if any(d["status"] == "abstained" for d in docs.values()):
        # Give the abstained units' rows a member and a polarity so they can be "kept".
        info = {m["path"]: m for u in truth["units"] for m in u["members"]}
        for d in docs.values():
            for e in d["excluded_rows"]:
                if e["member_path"] in info:
                    m = info[e["member_path"]]
                    basis, evidence = _evidence(accession, m["path"], m["ion_mode"], d["unit_technical"]["chromatography"])
                    d["rows"].append({"row_index": e["row_index"], "sample_id": e["sample_id"], "raw_file": e["raw_file"],
                                      "member_path": m["path"], "archive": m["archive"], "role": m["role"], "ion_mode": m["ion_mode"],
                                      "chromatography": d["unit_technical"]["chromatography"], "acquisition_mode": m["acquisition_mode"],
                                      "basis": basis, "evidence": evidence})
    template = {r["raw_file"]: r for d in docs.values() for r in d["rows"]}
    out = {}
    for cu in catalog["units"]:
        uid = cu["unit_id"]
        d = copy.deepcopy(docs[uid])
        kept, left = [], []
        for i, row in enumerate(cu["rows"]):
            if row["raw_file"] in template:
                r = copy.deepcopy(template[row["raw_file"]])
                r["row_index"] = i
                r["chromatography"] = d["unit_technical"]["chromatography"]
                if d["unit_technical"]["ion_mode"] in ("positive", "negative"):
                    r["ion_mode"] = d["unit_technical"]["ion_mode"]
                kept.append(r)
            else:
                left.extend(e for e in d["excluded_rows"] if e["row_index"] == i)
        d["rows"] = kept
        d["excluded_rows"] = left
        d["added_members"] = []  # stage3b admitted files by row name only
        d["status"] = "ready"
        d["needs_person"] = []
        d["flags"] = []
        d.pop("children", None)
        out[uid] = d
    return out
