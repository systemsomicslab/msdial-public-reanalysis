"""Replay a campaign's curated selections from its frozen decision files, without any language model.

A campaign that planned from triage files (campaign-runner.py plan --triage-dir) carries, per unit, the triage file's
sha256, the validator's verdict and the sha256 of the selection and of the analysis inputs Interactive declares from
it (scripts/campaign/curated.py). This script recomputes them from the released files alone, as a third party would,
and says where they are equal (docs/llm-assisted-curation.md, section 3). Nothing here runs or imports a model client
(tests/test_campaign_curated_selection.py asserts it): the LLM's part is its frozen output, kept as data.

    python scripts/replay-campaign.py --bundle <campaign dir or release copy> --workspace-root <absolute path>
        --level selection | inputs | outputs  [--unit <unit key>]  [--libraries <dir>]  [--out <report>]  [--json]

The bundle holds campaign-manifest.json, the digest the person approved (campaign-authorization.json, or the plan's
campaign-manifest.summary.json), and the triage directory: <bundle>\\triage\\<accession>\\..., or the directory the
manifest names. Levels, each including the one before:

- selection: the bundle only, no network and no raw data. The manifest's sha256 equals the approved digest; each
  unit's triage file hashes to the recorded triage_sha256, and its decision record (<name>.record.json, which names
  the model, prompt and skill that made it) to the recorded triage_record_sha256; the accession's review log is one
  unbroken hash chain that holds the head the plan recorded (review_log_head_sha256); scripts/validate-triage.py
  gives the recorded verdict on each accession; the selection and the analysis inputs recompute to the recorded
  selection_sha256 and analysis_inputs_sha256. The rebuilt inputs are written to
  <out dir>\\replay-inputs\\<unit key>.json.
- inputs: the raw archives' members, which the third party downloaded and extracted under
  <workspace root>\\<repository>\\<accession>\\: each selected member is found and checked against the listing's size
  and CRC32; and where the bundle keeps the unit's run manifest (units\\<unit key>\\run-manifest.json), the
  analysis_csv.canonical_sha256, parameter_file_sha256 and replay_projection_sha256 it records equal those of the run
  manifest the third party's Interactive wrote for the same unit
  (<workspace root>\\<repository>\\<accession>\\<workspace>\\provenance\\run-manifest.json), and the analysis CSV beside
  it recomputes to its canonical sha256.
- outputs: the MS-DIAL text outputs the bundle records (units\\<unit key>\\output-checksums.tsv: path, sha256) equal those
  under the third party's <workspace>\\output. A unit whose pinned libraries are not all in --libraries (by sha256) is
  not evaluable at this level (not_evaluable: private_library): the bundle names a private library by its sha256 and
  never holds it.

Exit status: 0 every compared value is equal, 1 something differs, 2 something could not be evaluated (never 0 for a
check that could not run). It writes replay-report.json (one row per unit, expected and replayed value of each hash).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import zlib
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from campaign import curated  # noqa: E402

LEVELS = ("selection", "inputs", "outputs")
EQUAL, DIFFERENT, NOT_EVALUABLE = "equal", "different", "not_evaluable"
EXIT = {EQUAL: 0, DIFFERENT: 1, NOT_EVALUABLE: 2}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Row:
    """One unit's comparisons."""

    def __init__(self, unit_key: str) -> None:
        self.unit_key = unit_key
        self.checks: list[dict[str, Any]] = []

    def compare(self, name: str, expected: Any, replayed: Any, *, why: str = "") -> None:
        if expected is None or replayed is None:
            self.checks.append({"check": name, "result": NOT_EVALUABLE, "expected": expected, "replayed": replayed,
                                "why": why or ("nothing recorded to compare" if expected is None else "not recomputed")})
        else:
            self.checks.append({"check": name, "result": EQUAL if expected == replayed else DIFFERENT,
                                "expected": expected, "replayed": replayed})

    def not_evaluable(self, name: str, why: str) -> None:
        self.checks.append({"check": name, "result": NOT_EVALUABLE, "why": why})

    @property
    def result(self) -> str:
        results = {item["result"] for item in self.checks}
        return DIFFERENT if DIFFERENT in results else NOT_EVALUABLE if NOT_EVALUABLE in results or not results else EQUAL

    def as_dict(self) -> dict[str, Any]:
        return {"unit_key": self.unit_key, "result": self.result, "checks": self.checks}


def approved_digest(bundle: Path) -> str | None:
    for name, key in (("campaign-authorization.json", "manifest_digest"), ("campaign-manifest.summary.json", "manifest_digest")):
        path = bundle / name
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig")).get(key)
            except ValueError:
                continue
            if value:
                return str(value)
    return None


def triage_root(bundle: Path, manifest: dict[str, Any]) -> Path:
    local = bundle / "triage"
    if local.is_dir():
        return local
    return Path(str((manifest.get("triage") or {}).get("directory") or local))


def check_record(row: Row, chosen: dict[str, Any], payload: Path) -> None:
    """The decision record beside the triage is the one the person approved: its sha256 equals the manifest's
    triage_record_sha256. None approved and none released is equal; one approved and not released is not evaluable;
    one released that was not approved, or another one, differs."""
    record = curated.record_path(payload)
    expected = chosen.get("triage_record_sha256")
    found = _sha(record.read_bytes()) if record.is_file() else None
    if expected is None and found is None:
        row.checks.append({"check": "triage_record_sha256", "result": EQUAL, "expected": None, "replayed": None,
                           "why": "no decision record was approved, and the bundle holds none"})
    elif found is None:
        row.not_evaluable("triage_record_sha256", f"the bundle holds no {record.name}")
    else:
        row.checks.append({"check": "triage_record_sha256", "result": EQUAL if expected == found else DIFFERENT,
                           "expected": expected, "replayed": found})


def check_review_log(row: Row, chosen: dict[str, Any], folder: Path) -> None:
    """The accession's review log is one unbroken chain (curated.verify_review_log), and the head the plan recorded is
    one of its lines: the entries a person could read when approving are the ones released, with only later entries
    (the approval, the header comparisons, a person's answers) after them."""
    path = folder / curated.REVIEW_LOG
    expected = chosen.get("review_log_head_sha256")
    if not path.is_file():
        if expected is None:
            row.checks.append({"check": "review_log_chain", "result": EQUAL, "expected": None, "replayed": None,
                               "why": "the plan recorded no review log, and the bundle holds none"})
        else:
            row.not_evaluable("review_log_chain", f"the bundle holds no {curated.REVIEW_LOG} for {folder.name}")
        return
    problems, line_sha256 = curated.verify_review_log(path)
    if expected is not None and expected not in line_sha256:
        problems.append(f"the head the plan recorded ({str(expected)[:12]}) is no line of the chain")
    row.checks.append({"check": "review_log_chain", "result": DIFFERENT if problems else EQUAL, "expected": expected,
                       "replayed": line_sha256[-1] if line_sha256 else None, **({"why": "; ".join(problems[:5])} if problems else {})})


def replay_selection(bundle: Path, manifest: dict[str, Any], units: list[dict[str, Any]], out_dir: Path,
                     rows: dict[str, Row]) -> None:
    root = triage_root(bundle, manifest)
    verdicts: dict[str, dict[str, Any]] = {}
    for unit in units:
        key = str(unit.get("unit_key", unit["unit_id"]))
        row = rows[key]
        chosen = unit["curated_selection"]
        payload = root / str(chosen.get("triage_relative_path") or "")
        if not payload.is_file():
            row.not_evaluable("triage_sha256", f"the bundle holds no {chosen.get('triage_relative_path')}")
            continue
        raw = payload.read_bytes()
        row.compare("triage_sha256", chosen.get("triage_sha256"), _sha(raw))
        folder = payload.parent
        check_record(row, chosen, payload)
        check_review_log(row, chosen, folder)
        accession = folder.name
        if accession not in verdicts:
            verdicts[accession] = curated.run_validator(folder)
        validation = verdicts[accession]
        row.compare("validator_verdict", (chosen.get("validator") or {}).get("verdict"), validation.get("verdict"))
        row.compare("validator_sha256", (chosen.get("validator") or {}).get("script_sha256"), validation.get("validator_sha256"))
        triage = json.loads(raw.decode("utf-8"))
        child = None
        if chosen.get("child_key"):
            child = next((item for item in curated.children(triage) if str(item.get("child_key")) == chosen["child_key"]), None)
            if child is None:
                row.not_evaluable("selection_sha256", f"the triage has no child {chosen['child_key']}")
                continue
        _catalog, listing_paths = curated.accession_sources(folder)
        listing = curated.read_listings(listing_paths)
        chosen_items = curated.selection(triage, listing, child)
        inputs = curated.analysis_inputs(triage, child)
        row.compare("selection_sha256", chosen.get("selection_sha256"), _sha(curated.canonical(chosen_items)))
        row.compare("analysis_inputs_sha256", chosen.get("analysis_inputs_sha256"), _sha(curated.canonical(inputs)))
        target = out_dir / "replay-inputs" / f"{key}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(json.dumps({"unit_key": key, "selection": chosen_items, "analysis_inputs": inputs},
                                      ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8"))


def _find_member(base: Path, member: str) -> Path | None:
    candidate = base / member
    if candidate.is_file():
        return candidate
    tail = member.replace("\\", "/").rstrip("/")
    hits = [item for item in base.rglob(Path(tail).name) if item.as_posix().endswith(tail)] if base.is_dir() else []
    return hits[0] if len(hits) == 1 else None


def _crc32(path: Path) -> str:
    value = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            value = zlib.crc32(chunk, value)
    return f"{value & 0xFFFFFFFF:08X}"


def _get(record: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        value: Any = record
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is not None:
            return value
    return None


def canonical_csv_sha256(csv_path: Path, raw_dir: Path) -> str:
    """The analysis CSV's canonical sha256 (framework section 3): every file path rewritten relative to the unit's raw
    directory, forward slashes, LF line endings, UTF-8 without BOM."""
    text = csv_path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    prefixes = sorted({str(raw_dir), str(raw_dir).replace("\\", "/"), str(raw_dir.resolve()), str(raw_dir.resolve()).replace("\\", "/")},
                      key=len, reverse=True)
    out_lines = []
    for line in text.split("\n"):
        cells = next(csv.reader([line])) if line else []
        fixed = []
        for cell in cells:
            value = cell
            for prefix in prefixes:
                if value.casefold().startswith(prefix.casefold()):
                    value = value[len(prefix):].lstrip("\\/")
                    break
            fixed.append(value.replace("\\", "/") if value != cell else value)
        out_lines.append(",".join(fixed) if cells else "")
    return _sha("\n".join(out_lines).encode("utf-8"))


def replay_inputs(bundle: Path, workspace_root: Path, manifest: dict[str, Any], units: list[dict[str, Any]],
                  out_dir: Path, rows: dict[str, Row]) -> None:
    for unit in units:
        key = str(unit.get("unit_key", unit["unit_id"]))
        row = rows[key]
        target = out_dir / "replay-inputs" / f"{key}.json"
        if not target.is_file():
            row.not_evaluable("members", "the selection could not be rebuilt")
            continue
        selection = json.loads(target.read_text(encoding="utf-8"))["selection"]
        base = workspace_root / str(unit["repository"]) / str(unit["accession"])
        missing, wrong = [], []
        for item in selection:
            found = _find_member(base, str(item.get("member_path") or ""))
            if found is None:
                missing.append(item.get("member_path"))
                continue
            if item.get("size") is not None and found.stat().st_size != int(item["size"]):
                wrong.append(f"{item['member_path']}: size {found.stat().st_size}, listed {item['size']}")
            elif item.get("crc32") and _crc32(found) != str(item["crc32"]).upper():
                wrong.append(f"{item['member_path']}: CRC32 differs from the listing")
        if missing:
            row.not_evaluable("members", f"{len(missing)} selected member(s) not found under {base}: {', '.join(map(str, missing[:5]))}")
        else:
            row.checks.append({"check": "members", "result": DIFFERENT if wrong else EQUAL, "files": len(selection),
                               **({"differences": wrong[:20]} if wrong else {})})
        recorded_path = bundle / "units" / key / "run-manifest.json"
        workspace = base / str(unit.get("workspace_name") or unit["unit_id"])
        replayed_path = workspace / "provenance" / "run-manifest.json"
        if not recorded_path.is_file():
            row.not_evaluable("run_manifest", f"the bundle keeps no units/{key}/run-manifest.json")
            continue
        if not replayed_path.is_file():
            row.not_evaluable("run_manifest", f"no replayed run manifest at {replayed_path}")
            continue
        recorded = json.loads(recorded_path.read_text(encoding="utf-8-sig"))
        replayed = json.loads(replayed_path.read_text(encoding="utf-8-sig"))
        for name, paths in (("analysis_csv.canonical_sha256", ("analysis_csv.canonical_sha256",)),
                            ("parameter_file_sha256", ("parameter_file_sha256", "parameter_file.canonical_sha256", "parameter_file.sha256")),
                            ("replay_projection_sha256", ("replay_projection_sha256",))):
            row.compare(name, _get(recorded, *paths), _get(replayed, *paths),
                        why=f"{name} is not recorded by the Interactive that ran (0.5.69 records it)")
        csv_path = _get(replayed, "analysis_csv.path", "analysis_csv_path", "input_path")
        if csv_path and Path(str(csv_path)).is_file():
            row.compare("analysis_csv_recomputed", _get(recorded, "analysis_csv.canonical_sha256"),
                        canonical_csv_sha256(Path(str(csv_path)), workspace / "raw"))


def replay_outputs(bundle: Path, workspace_root: Path, manifest: dict[str, Any], units: list[dict[str, Any]],
                   libraries: Path | None, rows: dict[str, Row]) -> None:
    held: set[str] = set()
    if libraries is not None and libraries.is_dir():
        held = {_sha(item.read_bytes()) for item in libraries.iterdir() if item.is_file()}
    pinned = [item for item in (manifest.get("pins") or {}).get("libraries") or [] if isinstance(item, dict)]
    absent = [str(item.get("name")) for item in pinned if item.get("sha256") not in held]
    for unit in units:
        key = str(unit.get("unit_key", unit["unit_id"]))
        row = rows[key]
        if absent:
            row.not_evaluable("outputs", f"private_library: {len(absent)} pinned librar{'y' if len(absent) == 1 else 'ies'} "
                                         f"not held by sha256 ({', '.join(absent[:3])})")
            continue
        recorded = bundle / "units" / key / "output-checksums.tsv"
        if not recorded.is_file():
            row.not_evaluable("outputs", f"the bundle records no units/{key}/output-checksums.tsv")
            continue
        workspace = workspace_root / str(unit["repository"]) / str(unit["accession"]) / str(unit.get("workspace_name") or unit["unit_id"])
        with recorded.open(encoding="utf-8", newline="") as handle:
            for line in csv.DictReader(handle, delimiter="\t"):
                found = workspace / "output" / str(line.get("path") or "")
                row.compare(f"output:{line.get('path')}", line.get("sha256"), _sha(found.read_bytes()) if found.is_file() else None,
                            why="not found in the replayed output")


def replay(bundle: Path, workspace_root: Path, level: str, unit_key: str | None = None, libraries: Path | None = None,
           out: Path | None = None) -> dict[str, Any]:
    manifest_path = bundle / "campaign-manifest.json"
    data = manifest_path.read_bytes()
    manifest = json.loads(data.decode("utf-8"))
    digest = "sha256:" + _sha(data)
    units = [unit for unit in manifest.get("units") or [] if isinstance(unit.get("curated_selection"), dict)
             and (unit_key is None or str(unit.get("unit_key", unit["unit_id"])) == unit_key)]
    out_dir = (out or bundle / "replay-report.json").parent
    rows = {str(unit.get("unit_key", unit["unit_id"])): Row(str(unit.get("unit_key", unit["unit_id"]))) for unit in units}
    campaign = Row("(campaign)")
    campaign.compare("manifest_digest", approved_digest(bundle), digest, why="the bundle holds no approved digest")
    if not units:
        campaign.not_evaluable("units", "no unit of this manifest carries a curated selection" + (f" named {unit_key}" if unit_key else ""))
    replay_selection(bundle, manifest, units, out_dir, rows)
    if level in ("inputs", "outputs"):
        replay_inputs(bundle, workspace_root, manifest, units, out_dir, rows)
    if level == "outputs":
        replay_outputs(bundle, workspace_root, manifest, units, libraries, rows)
    every = [campaign] + list(rows.values())
    results = {row.result for row in every}
    overall = DIFFERENT if DIFFERENT in results else NOT_EVALUABLE if NOT_EVALUABLE in results else EQUAL
    report = {"schema": "msdial-campaign-replay-report.v1", "level": level, "result": overall,
              "manifest_digest": digest, "campaign_id": manifest.get("campaign_id"),
              "units": [row.as_dict() for row in every]}
    target = out or bundle / "replay-report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--level", choices=LEVELS, required=True)
    parser.add_argument("--unit")
    parser.add_argument("--libraries", type=Path, help="a folder holding the pinned libraries, matched by sha256 (outputs)")
    parser.add_argument("--out", type=Path, help="where to write replay-report.json (default: in the bundle)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.workspace_root.is_absolute():
        print("--workspace-root must be an absolute path.", file=sys.stderr)
        return 2
    if not (args.bundle / "campaign-manifest.json").is_file():
        print(f"{args.bundle} holds no campaign-manifest.json.", file=sys.stderr)
        return 2
    report = replay(args.bundle, args.workspace_root, args.level, args.unit, args.libraries, args.out)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"replay {args.level}: {report['result'].upper()} ({len(report['units']) - 1} unit(s))")
        for row in report["units"]:
            bad = [item for item in row["checks"] if item["result"] != EQUAL]
            print(f"  {row['unit_key']}: {row['result']}" + (f" ({'; '.join(item['check'] + ': ' + str(item.get('why') or item['result']) for item in bad[:3])})" if bad else ""))
    return EXIT[report["result"]]


if __name__ == "__main__":
    sys.exit(main())
