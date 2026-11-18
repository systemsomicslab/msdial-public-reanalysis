"""Record a named person's answer to a question a triage left open, before the plan (docs/llm-assisted-curation.md 2.2, 2.4).

A v2 triage that carries a blocking flag, asks a needs_person question, or keeps a file inferred at low confidence in a
ready unit does not pass validate-triage.py until the accession's review-log.jsonl holds a person_answer about that
payload with the matching question_code. The runner writes such answers only for units already in an approved
campaign; a unit whose triage does not pass is never planned, so its answer is recorded here.

The answer is a person's, never the agent's. Run this with --by only after that person has read the question in the
conversation and given their answer; --by is that person (a name or the initials they agreed to publish), --answer
their words, verbatim. Without --by, nothing is recorded: the questions of the unit's current payload are shown.

    python -I scripts/record-person-answer.py <campaign dir>\\triage\\<accession> --unit <unit_id>
        shows the current payload's open questions, flags and low-confidence kept files; records nothing
    python -I scripts/record-person-answer.py <triage dir> --unit <unit_id> --question-code <code> \\
        --by "<name>" --answer "<their words>" [--decision-id <sha256>]
        appends one person_answer entry (channel conversation) through curated.append_review_log

The question code must be one the payload raises: a blocking flag's code, a needs_person question's code, or
inferred_low_confidence when a kept file is inferred at low confidence. The entry names the current payload (its
highest revision); --decision-id, when given, must be that payload's sha256, so an answer given about what was shown
is never recorded against a payload that changed since. A needs_person unit stays needs_person: the answer lets a
revision (run-triage-agent.py --revise <unit_id>=<seq>) make it ready.

Exit status: 0 shown or recorded; 2 refused (nothing written); 3 the triage directory is not usable.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from campaign import curated  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("validate_triage_for_answers", SCRIPTS / "validate-triage.py")
assert _SPEC and _SPEC.loader
validator = importlib.util.module_from_spec(_SPEC)
sys.modules["validate_triage_for_answers"] = validator
_SPEC.loader.exec_module(validator)

CHANNEL = curated.PERSON_ANSWER_CHANNEL


def questions(payload: dict[str, Any]) -> dict[str, str]:
    """{question code: what it asks} for the payload: its blocking flags, its needs_person questions, and
    inferred_low_confidence when a ready unit keeps a file inferred at low confidence."""
    out: dict[str, str] = {}
    for flag in payload.get("flags") or []:
        if isinstance(flag, dict) and flag.get("code") in validator.BLOCKING_FLAGS:
            out[str(flag["code"])] = str(flag.get("detail") or "a blocking flag")
    for question in payload.get("needs_person") or []:
        if isinstance(question, dict) and question.get("code"):
            out[str(question["code"])] = str(question.get("question") or "a question for a person")
    low = [str(item.get("member_path") or item.get("raw_file")) for item in
           [*(payload.get("rows") or []), *(payload.get("added_members") or [])]
           if isinstance(item, dict) and item.get("basis") == "inferred" and item.get("confidence") == "low"]
    if low:
        out[validator.LOW_CONFIDENCE_CODE] = ("kept on an inferred reading at low confidence: " + ", ".join(low[:10])
                                              + (" ..." if len(low) > 10 else ""))
    return out


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("triage_dir", type=Path)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--question-code")
    parser.add_argument("--by", help="the person who answered (a name or the initials they agreed to publish)")
    parser.add_argument("--answer", help="their answer, verbatim")
    parser.add_argument("--decision-id", help="the sha256 of the payload the person was shown")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    triage_dir = args.triage_dir
    if not triage_dir.is_dir():
        print(f"not a directory: {triage_dir}", file=sys.stderr)
        return 3
    payload_path = curated.payload_path(triage_dir, args.unit)
    if payload_path is None:
        print(f"no payload of {args.unit} in {triage_dir.name}", file=sys.stderr)
        return 3
    data = payload_path.read_bytes()
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as error:
        print(f"{payload_path.name} is not readable JSON: {error}", file=sys.stderr)
        return 3
    decision_id = hashlib.sha256(data).hexdigest()
    asked = questions(payload if isinstance(payload, dict) else {})
    shown = {"unit": args.unit, "payload": payload_path.name, "decision_id": decision_id,
             "status": payload.get("status") if isinstance(payload, dict) else None, "questions": asked}
    if not args.by:
        if args.question_code or args.answer:
            print("an answer is recorded only with --by naming the person who gave it; nothing was recorded",
                  file=sys.stderr)
            return 2
        lines = [f"{payload_path.name} ({decision_id}), status {shown['status']}:"]
        lines += [f"  {code}: {text}" for code, text in asked.items()] or ["  no open question"]
        print(json.dumps(shown, indent=1, ensure_ascii=False) if args.json else "\n".join(lines))
        return 0
    problems = []
    if not str(args.by).strip():
        problems.append("--by names nobody")
    if not args.question_code:
        problems.append("--question-code is required with --by")
    elif args.question_code not in asked:
        problems.append(f"{payload_path.name} raises no question {args.question_code!r} "
                        f"({', '.join(asked) or 'it raises none'})")
    if not str(args.answer or "").strip():
        problems.append("--answer (the person's words, verbatim) is required")
    if args.decision_id and args.decision_id.casefold() != decision_id:
        problems.append(f"--decision-id {args.decision_id} is not the current payload {payload_path.name} ({decision_id})")
    _log, broken = validator.read_review_log(triage_dir)
    if broken:
        problems.append(f"{curated.REVIEW_LOG}'s chain does not hold ({broken[0]})")
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print("nothing was recorded", file=sys.stderr)
        return 2
    entry = curated.append_review_log(triage_dir, "person_answer", decision_id,
                                      datetime.now().astimezone().isoformat(timespec="seconds"),
                                      by=str(args.by).strip(), question_code=args.question_code, answer=args.answer,
                                      channel=CHANNEL)
    print(json.dumps(entry, indent=1, ensure_ascii=False) if args.json else
          f"recorded review log entry {entry['seq']}: {args.question_code} on {payload_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
