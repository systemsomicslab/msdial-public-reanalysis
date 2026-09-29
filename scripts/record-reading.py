"""Record that a person read the sentences a gate check left for them, and what they concluded.

SUM-2 and QA-1 hand some sentences to a person instead of judging them: a checksum phrasing that a
rule set aside, a QA sentence that is not Interactive's own. The user decided on 2026-09-28 that a
run with such sentences counts as completed only once a person's reading of them is recorded; READ-1
in verify-run-invariants.py reads the record.

The reading is a person's, not the agent's. Run this with --digest only after the person has read
the sentences it shows and has said what they found; --by is that person.

Usage:
    python scripts/record-reading.py <unit-workspace> --check QA-1
        shows the sentences and their digest, and records nothing
    python scripts/record-reading.py <unit-workspace> --check QA-1 --digest sha256:... \\
        --by "Name" --conclusion accepted|rejected [--note "..."]
        records the reading in <unit-workspace>/provenance/readings.json

The digest must be the one shown: a text changed since it was shown gives another digest, and the
reading is refused. Exit codes: 0 shown or recorded, 2 refused, 3 the workspace is unusable.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

GATE = Path(__file__).resolve().parent / "verify-run-invariants.py"


def _gate():
    spec = importlib.util.spec_from_file_location("verify_run_invariants", GATE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("verify_run_invariants", module)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workspace")
    parser.add_argument("--check", required=True, choices=("QA-1", "SUM-2"))
    parser.add_argument("--digest", help="the digest shown for the sentences read")
    parser.add_argument("--by", help="the person who read them")
    parser.add_argument("--conclusion", choices=("accepted", "rejected"))
    parser.add_argument("--note", default="")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass

    workspace = Path(args.workspace).expanduser()
    if not (workspace / "output").is_dir():
        print(f"not a unit workspace (no output/): {workspace}", file=sys.stderr)
        return 3
    gate = _gate()
    report = gate.verify(workspace, "before-publish")
    check = next((item for item in report.checks if item.check_id == args.check), None)
    if check is None or "to_read_count" not in check.evidence:
        print(f"{args.check} did not judge this workspace ({check.status if check else 'not run'}): "
              f"{check.detail if check else ''}", file=sys.stderr)
        return 2
    to_read = check.evidence.get("to_read") or []
    count = check.evidence.get("to_read_count", 0)
    digest = check.evidence.get("to_read_digest", "")
    if not count:
        print(f"{args.check} leaves no sentence for a person to read.")
        return 0 if not args.digest else 2
    print(f"{args.check}: {count} sentence(s) for a person to read (digest {digest})")
    for index, item in enumerate(to_read, start=1):
        print(f"  {index}. [{item.get('source')}] {item.get('sentence')}")
    if count > len(to_read):
        print(f"  ... and {count - len(to_read)} more; see the gate's --json output")
    if not args.digest:
        print("Nothing recorded. Once the person has read these and said what they found, run again with "
              "--digest, --by and --conclusion.")
        return 0
    if args.digest != digest:
        print(f"refused: the sentences now have digest {digest}, not {args.digest}; the text changed since it was "
              "shown, so it must be read again", file=sys.stderr)
        return 2
    if not args.by or not args.by.strip() or not args.conclusion:
        print("refused: --by (the person who read them) and --conclusion are required to record", file=sys.stderr)
        return 2

    path = workspace / "provenance" / gate.READINGS_FILE
    if path.exists():
        record, reason = gate._read_json(path)
        if record is None or record.get("schema") != gate.READINGS_SCHEMA or not isinstance(record.get("readings"), list):
            print(f"refused: {path} is not a {gate.READINGS_SCHEMA} record ({reason or 'wrong schema'})",
                  file=sys.stderr)
            return 2
    else:
        record = {"schema": gate.READINGS_SCHEMA, "readings": []}
    record["readings"].append({
        "check_id": args.check,
        "digest": digest,
        "sentence_count": count,
        "sources": sorted({str(item.get("source")) for item in to_read}),
        "read_by": args.by.strip(),
        "read_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "conclusion": args.conclusion,
        "note": args.note.strip(),
        "gate_sha256": hashlib.sha256(GATE.read_bytes()).hexdigest(),
    })
    _write(path, record)
    print(f"recorded: {args.check} read by {args.by.strip()}, {args.conclusion}, in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
