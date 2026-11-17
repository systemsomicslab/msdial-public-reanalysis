"""Walk a campaign's review logs and its decision ledger, and refuse what breaks them (docs/llm-assisted-curation.md 2.4).

    python -I scripts/verify-decision-log.py <campaign dir | release bundle> [--triage-dir <dir>] [--json]

The directory is a campaign directory as the runner keeps it (campaign-manifest.json, decision-ledger.jsonl, and the
triage directory the manifest names, else triage\\ beside it) or a release bundle (campaign-manifest.release.json,
decision-ledger.jsonl and triage\\, release-index.json). --triage-dir names the triage directory where neither holds it.
Every accession folder of the triage directory (triage\\<accession>\\) is read: its payloads (every *.json except the
decision records <name>.record.json and index.json, superseded revisions included; a decision_id is the sha256 of a
payload's bytes), the records beside them (for supersedes) and review-log.jsonl.

Refused, each as a finding of level "error":

- L-CHAIN: a review log line that does not parse, is not an object, counts seq out of order or does not chain to the
  line before it (prev_sha256 is the sha256 of that line's bytes, null for the first).
- L-ENTRY: a review log entry of a kind section 2.4 does not name, or with a field outside its kind's row.
- L-PAYLOAD: a decision_id (or a revision's supersedes or new_decision_id) that is no payload of the accession.
- L-CAUSE: a revision whose cause names no entry of the stated kind: a person_answer or validator_verdict at that
  seq of the same review log, before the revision and for this decision or one it supersedes; or a header_reconciliation
  at that seq of the decision ledger, for the revised unit or one of its children.
- L-FLAG: a revision that removes a blocking flag the superseded payload carried (validate-triage.py BLOCKING_FLAGS)
  when no revision entry of the new decision has as its cause a named person's person_answer with that question_code.
- D-CHAIN: a decision ledger line that does not parse, counts seq out of order, does not chain, or has no standing.
- D-ENTRY: a ledger line of a kind the export does not write (curated.DECISION_KINDS).
- D-PATH: a ledger line that holds an absolute path (a drive letter, UNC or user profile path).
- D-TRIAGE: a triage_recorded line whose triage_sha256 is no payload's sha256, or whose triage_relative_path names a
  file that does not hash to it.
- D-HEAD: a triage_validated line whose review_log_head_sha256 is not the sha256 of a line of its accession's review
  log (null passes: the log was empty when the plan was made).

A campaign without a decision ledger is checked on its review logs alone, and says so (a "note"); a ledger that names
a triage while no triage directory is found is refused (D-TRIAGE).

Exit status: 0 nothing refused; 1 something refused; 2 the directory cannot be read. Read only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Mapping

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from campaign import curated  # noqa: E402


def _load(name: str, file_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file_name)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


validator = _load("validate_triage_for_decision_log", "validate-triage.py")
redaction = curated.redaction_check()

TOOL = "verify-decision-log.py"
REVIEW_LOG_CAUSES = ("person_answer", "validator_verdict")
LEDGER_CAUSES = ("header_reconciliation",)
_KEY_SEPARATORS = "-_.@+"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lines(path: Path) -> list[bytes]:
    return [line for line in Path(path).read_bytes().split(b"\n") if line.strip()]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


class Report:
    def __init__(self) -> None:
        self.findings: list[dict[str, Any]] = []
        self.accessions: dict[str, dict[str, Any]] = {}
        self.ledger: dict[str, Any] = {}

    def add(self, level: str, check: str, where: str, message: str, line: int | None = None) -> None:
        item: dict[str, Any] = {"level": level, "check": check, "where": where, "message": message}
        if line is not None:
            item["line"] = line
        self.findings.append(item)

    @property
    def verdict(self) -> str:
        return "refused" if any(item["level"] == "error" for item in self.findings) else "pass"

    def as_dict(self) -> dict[str, Any]:
        return {"tool": TOOL, "verdict": self.verdict, "accessions": self.accessions, "ledger": self.ledger,
                "findings": self.findings}


# ---- where things are -----------------------------------------------------------------------------------------------

def triage_root(directory: Path, override: Path | None = None) -> Path | None:
    """The triage directory: --triage-dir; in a release bundle, its triage\\; else the one the campaign manifest names
    (absolute, or relative to the directory), where it exists; else <directory>\\triage."""
    if override is not None:
        return Path(override)
    if (directory / curated.RELEASE_INDEX).is_file() and (directory / "triage").is_dir():
        return directory / "triage"  # a release bundle holds its triage directory as triage\
    for name in ("campaign-manifest.json", "campaign-manifest.release.json"):
        manifest = _read_json(directory / name)
        named = str(((manifest or {}).get("triage") or {}).get("directory") or "") if isinstance(manifest, dict) else ""
        if named and not named.startswith("<external>"):
            path = Path(named)
            path = path if path.is_absolute() else directory / path
            if path.is_dir():
                return path
    fallback = directory / "triage"
    return fallback if fallback.is_dir() else None


def accession_dirs(root: Path | None) -> list[Path]:
    if root is None or not root.is_dir():
        return []
    return sorted(path for path in root.iterdir()
                  if path.is_dir() and path.name != "inputs"
                  and ((path / curated.REVIEW_LOG).is_file() or any(_payload_paths(path))))


def _payload_paths(folder: Path) -> list[Path]:
    return [path for path in sorted(folder.glob("*.json"))
            if path.name != "index.json" and not path.name.endswith(validator.RECORD_SUFFIX)]


class Accession:
    """One accession folder: its payloads by decision id, the supersedes its records name, and its review log."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.payloads: dict[str, tuple[Path, dict[str, Any] | None]] = {}
        self.supersedes: dict[str, str | None] = {}
        for path in _payload_paths(folder):
            data = path.read_bytes()
            decision_id = sha256_bytes(data)
            try:
                value = json.loads(data.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError):
                value = None
            self.payloads[decision_id] = (path, value if isinstance(value, dict) else None)
            record = _read_json(curated.record_path(path))
            if isinstance(record, dict) and isinstance(record.get("supersedes"), str):
                self.supersedes[decision_id] = record["supersedes"]
        self.log_path = folder / curated.REVIEW_LOG
        self.lines: list[bytes] = _lines(self.log_path) if self.log_path.is_file() else []
        self.line_sha256 = [sha256_bytes(line) for line in self.lines]
        self.entries: list[dict[str, Any] | None] = []
        for line in self.lines:
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                value = None
            self.entries.append(value if isinstance(value, dict) else None)

    @property
    def log(self) -> list[dict[str, Any]]:
        return [entry for entry in self.entries if entry is not None]


# ---- the review logs --------------------------------------------------------------------------------------------------

def check_review_log(report: Report, accession: Accession, ledger: list[dict[str, Any] | None]) -> None:
    where = f"{accession.folder.name}/{curated.REVIEW_LOG}"
    previous = None
    for index, (line, entry) in enumerate(zip(accession.lines, accession.entries), start=1):
        if entry is None:
            report.add("error", "L-CHAIN", where, f"line {index} does not parse as a JSON object", index)
        else:
            if entry.get("seq") != index:
                report.add("error", "L-CHAIN", where, f"line {index} has seq {entry.get('seq')}", index)
            if entry.get("prev_sha256") != previous:
                report.add("error", "L-CHAIN", where, f"line {index} does not chain to line {index - 1}", index)
        previous = sha256_bytes(line)
    by_seq = {entry.get("seq"): entry for entry in accession.log}
    for index, entry in enumerate(accession.entries, start=1):
        if entry is None:
            continue
        kind = entry.get("kind")
        fields = curated.REVIEW_LOG_KINDS.get(kind) if isinstance(kind, str) else None
        if fields is None:
            report.add("error", "L-ENTRY", where, f"line {index} is of kind {kind!r}, which the review log does not keep "
                       f"({', '.join(curated.REVIEW_LOG_KINDS)})", index)
            continue
        extra = sorted(set(entry) - {"seq", "at", "kind", "decision_id", "prev_sha256"} - set(fields))
        if extra:
            report.add("error", "L-ENTRY", where, f"line {index} ({kind}) carries {', '.join(extra)}, outside its kind's "
                       f"fields ({', '.join(fields)})", index)
        named = {"decision_id": entry.get("decision_id")}
        if kind == "revision":
            named.update({"supersedes": entry.get("supersedes"), "new_decision_id": entry.get("new_decision_id")})
        for field, value in named.items():
            if not isinstance(value, str) or value not in accession.payloads:
                report.add("error", "L-PAYLOAD", where, f"line {index} ({kind}) names {field} {value}, which is no "
                           "payload of this accession", index)
        if kind == "revision":
            _check_cause(report, accession, where, index, entry, by_seq, ledger)
    _check_dropped_flags(report, accession, where, by_seq)


def _unit_of(accession: Accession, decision_id: Any) -> str | None:
    found = accession.payloads.get(decision_id) if isinstance(decision_id, str) else None
    if found is None:
        return None
    path, value = found
    return str((value or {}).get("unit_id") or validator.triage_file_unit(path))


def _key_of_unit(key: Any, unit: str | None) -> bool:
    key = str(key or "")
    return bool(unit) and (key == unit or (key.startswith(unit) and key[len(unit):len(unit) + 1] in tuple(_KEY_SEPARATORS)))


def _check_cause(report: Report, accession: Accession, where: str, index: int, entry: Mapping[str, Any],
                 by_seq: Mapping[Any, Mapping[str, Any]], ledger: list[dict[str, Any] | None]) -> None:
    cause = entry.get("cause") if isinstance(entry.get("cause"), Mapping) else {}
    kind, seq = cause.get("kind"), cause.get("seq")
    new_id = entry.get("new_decision_id")
    ids = validator.lineage(str(new_id), accession.supersedes, accession.log) if isinstance(new_id, str) else set()
    if kind in REVIEW_LOG_CAUSES:
        cited = by_seq.get(seq)
        if cited is None or cited.get("kind") != kind:
            report.add("error", "L-CAUSE", where, f"line {index} (revision) names as its cause the {kind} at seq {seq}, "
                       f"and the review log holds no {kind} there", index)
        elif not isinstance(seq, int) or seq >= index:
            report.add("error", "L-CAUSE", where, f"line {index} (revision) names as its cause the {kind} at seq {seq}, "
                       "which comes after it", index)
        elif cited.get("decision_id") not in ids:
            report.add("error", "L-CAUSE", where, f"line {index} (revision) names as its cause the {kind} at seq {seq}, "
                       "which is about a decision this revision does not supersede", index)
    elif kind in LEDGER_CAUSES:
        line = ledger[seq - 1] if isinstance(seq, int) and 1 <= seq <= len(ledger) else None
        unit = _unit_of(accession, entry.get("supersedes"))
        if line is None or line.get("kind") != kind or line.get("seq") != seq:
            report.add("error", "L-CAUSE", where, f"line {index} (revision) names as its cause the ledger's {kind} at seq "
                       f"{seq}, and the decision ledger holds no {kind} there"
                       + ("" if ledger else " (there is no decision ledger)"), index)
        elif not _key_of_unit(line.get("unit_key"), unit):
            report.add("error", "L-CAUSE", where, f"line {index} (revision) names as its cause the ledger's {kind} at seq "
                       f"{seq}, which is about {line.get('unit_key')}, not {unit}", index)
    else:
        report.add("error", "L-CAUSE", where, f"line {index} (revision) names a cause of kind {kind!r}; a revision is "
                   f"caused by a {', a '.join(REVIEW_LOG_CAUSES + LEDGER_CAUSES)}", index)


def _check_dropped_flags(report: Report, accession: Accession, where: str, by_seq: Mapping[Any, Mapping[str, Any]]) -> None:
    """A revision may drop a blocking flag of the payload it supersedes only on a named person's answer to it: some
    revision entry of the new decision must have as its cause a person_answer with that question_code, about this
    decision or one it supersedes (as validate-triage.py T-REVISION reads it)."""
    revisions = [(index, entry) for index, entry in enumerate(accession.entries, start=1)
                 if entry is not None and entry.get("kind") == "revision"]
    for index, entry in revisions:
        new_id, old_id = entry.get("new_decision_id"), entry.get("supersedes")
        if new_id not in accession.payloads or old_id not in accession.payloads:
            continue  # L-PAYLOAD said so
        ids = validator.lineage(str(new_id), accession.supersedes, accession.log)
        answered = set()
        for _index, other in revisions:
            if other.get("new_decision_id") != new_id:
                continue
            cause = other.get("cause") if isinstance(other.get("cause"), Mapping) else {}
            answer = by_seq.get(cause.get("seq"))
            if (cause.get("kind") == "person_answer" and answer is not None and answer.get("kind") == "person_answer"
                    and answer.get("decision_id") in ids and str(answer.get("by") or "").strip()
                    and isinstance(answer.get("seq"), int) and answer["seq"] < int(other.get("seq") or 0)):
                answered.add(str(answer.get("question_code")))
        dropped = validator._blocking(accession.payloads[old_id][1]) - validator._blocking(accession.payloads[new_id][1])  # noqa: SLF001
        for code in sorted(dropped - answered):
            report.add("error", "L-FLAG", where, f"line {index} (revision) drops the blocking flag {code} that "
                       f"{accession.payloads[old_id][0].name} carried, and no revision of "
                       f"{accession.payloads[new_id][0].name} is caused by a person's answer to {code}", index)


# ---- the decision ledger ----------------------------------------------------------------------------------------------

def read_ledger(path: Path) -> tuple[list[bytes], list[dict[str, Any] | None]]:
    lines = _lines(path) if path.is_file() else []
    entries: list[dict[str, Any] | None] = []
    for line in lines:
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            value = None
        entries.append(value if isinstance(value, dict) else None)
    return lines, entries


def check_ledger(report: Report, path: Path, lines: list[bytes], entries: list[dict[str, Any] | None],
                 root: Path | None, accessions: Mapping[str, Accession]) -> None:
    where = path.name
    previous = None
    for index, (line, entry) in enumerate(zip(lines, entries), start=1):
        if entry is None:
            report.add("error", "D-CHAIN", where, f"line {index} does not parse as a JSON object", index)
        else:
            if entry.get("seq") != index:
                report.add("error", "D-CHAIN", where, f"line {index} has seq {entry.get('seq')}", index)
            if entry.get("prev_sha256") != previous:
                report.add("error", "D-CHAIN", where, f"line {index} does not chain to line {index - 1}", index)
            if entry.get("standing") not in curated.STANDINGS:
                report.add("error", "D-CHAIN", where, f"line {index} has no standing ({entry.get('standing')!r})", index)
            if entry.get("kind") not in curated.DECISION_KINDS:
                report.add("error", "D-ENTRY", where, f"line {index} is of kind {entry.get('kind')!r}, which the decision "
                           "ledger does not export", index)
        rules = [rule for rule, _at in redaction.path_findings(line.decode("latin-1"))]
        if rules:
            report.add("error", "D-PATH", where, f"line {index} holds an absolute path ({', '.join(rules)}); a ledger line "
                       "never does", index)
        previous = sha256_bytes(line)
    payload_shas = {sha: accession for accession in accessions.values() for sha in accession.payloads}
    folder_of: dict[str, str] = {}
    for index, entry in enumerate(entries, start=1):
        if entry is None:
            continue
        detail = entry.get("detail") if isinstance(entry.get("detail"), Mapping) else {}
        key = str(entry.get("unit_key"))
        if entry.get("kind") == "triage_recorded":
            sha = detail.get("triage_sha256")
            relative = str(detail.get("triage_relative_path") or "")
            if relative:
                folder_of[key] = relative.replace("\\", "/").rsplit("/", 1)[0] if "/" in relative.replace("\\", "/") else ""
            if root is None:
                report.add("error", "D-TRIAGE", where, f"line {index} (triage_recorded, {key}) names triage {sha}, and no "
                           "triage directory was found", index)
                continue
            if not isinstance(sha, str) or sha not in payload_shas:
                report.add("error", "D-TRIAGE", where, f"line {index} (triage_recorded, {key}) names triage {sha}, which "
                           "is no payload's sha256", index)
                continue
            if relative:
                named = root / relative
                actual = sha256_bytes(named.read_bytes()) if named.is_file() else None
                if actual != sha:
                    report.add("error", "D-TRIAGE", where, f"line {index} (triage_recorded, {key}) names {relative}, which "
                               + ("is not there" if actual is None else f"hashes to {actual}, not {sha}"), index)
        elif entry.get("kind") == "triage_validated":
            head = detail.get("review_log_head_sha256")
            if head is None:
                continue
            name = folder_of.get(key)
            if name is None:
                name = str(detail.get("accession") or "")
            accession = accessions.get(name)
            if accession is None or head not in accession.line_sha256:
                report.add("error", "D-HEAD", where, f"line {index} (triage_validated, {key}) names review log head "
                           f"{head}, which is no line of {name or '(no accession)'}/{curated.REVIEW_LOG}", index)


# ---- the walk ---------------------------------------------------------------------------------------------------------

def verify(directory: Path, triage_dir: Path | None = None) -> Report:
    report = Report()
    directory = Path(directory)
    root = triage_root(directory, triage_dir)
    accessions = {folder.name: Accession(folder) for folder in accession_dirs(root)}
    ledger_path = directory / curated.DECISION_LEDGER
    lines, entries = read_ledger(ledger_path)
    for name, accession in accessions.items():
        check_review_log(report, accession, entries)
        report.accessions[name] = {"payloads": len(accession.payloads), "review_log_lines": len(accession.lines),
                                   "review_log_head_sha256": accession.line_sha256[-1] if accession.line_sha256 else None}
    if ledger_path.is_file():
        check_ledger(report, ledger_path, lines, entries, root, accessions)
    else:
        report.add("note", "D-CHAIN", ledger_path.name, "there is no decision ledger; only the review logs were checked")
    report.ledger = {"present": ledger_path.is_file(), "lines": len(lines),
                     "head_sha256": sha256_bytes(lines[-1]) if lines else None,
                     "triage_directory": None if root is None else (
                         root.relative_to(directory).as_posix() if root.is_relative_to(directory) else "<external>/" + root.name)}
    return report


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", help="a campaign directory or a release bundle")
    parser.add_argument("--triage-dir", default=None, help="the triage directory, where the campaign does not name it")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"{directory} is not a directory.", file=sys.stderr)
        return 2
    try:
        report = verify(directory, Path(args.triage_dir) if args.triage_dir else None)
    except OSError as error:
        print(f"The decision log could not be read: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report.as_dict(), indent=1, ensure_ascii=False))
    else:
        print(f"{TOOL}: {report.verdict} ({len(report.accessions)} accessions, {report.ledger['lines']} ledger lines)")
        for item in report.findings:
            print(f"  [{item['level']}] {item['check']} {item['where']}: {item['message']}")
    return 1 if report.verdict == "refused" else 0


if __name__ == "__main__":
    sys.exit(main())
