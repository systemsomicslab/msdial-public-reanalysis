"""Check every file of a release bundle for what must never be released (docs/llm-assisted-curation.md section 5.3).

    python -I scripts/check-release-redaction.py <bundle> [--library-name <file name>]... [--limit N] [--json]

The bundle is what campaign-runner.py release writes (release-index.json, the release projections, the triage
directory, decision-ledger.jsonl), with whatever is added beside it before deposit: redistributed MS-DIAL outputs,
the redacted transcripts of clean configuration runs, the code. Every file is read, whatever its name or type, and
refused for:

- drive_letter_path: a drive letter path (C:\\..., C:/..., and the JSON escape C:\\\\...). A URL is no drive letter:
  the letter before ':' must not follow another letter or digit.
- unc_path: a UNC path (\\\\host\\share), as text or as a JSON escape.
- user_profile_path: a user profile path (.../Users/<name>/..., /home/<name>/..., Documents and Settings).
- library_path: a library file named with a path. A library is named by file name and sha256 only
  (<library:<name> sha256=<sha256>>, the release projection's form): a file name ending .msp, .lbm, .lbm2 or .lbm3,
  or any name the bundle itself names in a <library:...> token or given with --library-name, that follows a path
  separator is refused. <external>/<name>, the projection's form for a file outside the workspace, names no path and
  is allowed; so is a URL.
- context_block (transcripts only): a block the harness inserts from a context file or from memory (a CLAUDE.md,
  CLAUDE.local.md, AGENTS.md or MEMORY.md shown as "Contents of ...", the harness's preamble to them, or the auto
  memory index). A transcript is a file under a transcripts folder, a *.redacted.jsonl, or a *transcript*.jsonl. A
  clean configuration run loads none, so such a block means the transcript is not of a clean run, or was not redacted.

Each rule is reported once per file (its first match): {file (bundle-relative, forward slashes), rule, at (byte
offset), line}. Files are read in chunks, as bytes mapped one to one onto characters (latin-1), so a file of any size
or encoding is read without failing; a UTF-16 file (with its byte order mark) is read as UTF-16.

campaign-runner.py release applies this check (through scripts/campaign/curated.release_redaction_findings) to the
bundle it writes and writes no release-index.json for a bundle with a finding. Run it again over the whole deposit
after anything is added beside the bundle.

Exit status: 0 nothing found; 1 a finding; 2 the bundle cannot be read. Read only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator

TOOL = "check-release-redaction.py"
VERSION = "v1"
CHUNK = 8 * 1024 * 1024
OVERLAP = 4096

# The three path rules (section 5.3), as text and as JSON escapes. The release command decided with the same three
# which triage inputs it projects (curated._holds_absolute_path).
PATH_RULES = (
    ("drive_letter_path", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:(?:\\\\|\\|/)")),
    ("unc_path", re.compile(r"(?:^|[\s\"'=(\[,])(?:\\\\\\\\|\\\\)[A-Za-z0-9._$-]+(?:\\\\|\\)[A-Za-z0-9._$-]+")),
    ("user_profile_path", re.compile(
        r"(?i)(?:^|[\\/\"'\s])(?:Users|home|Documents and Settings)(?:\\\\|\\|/)[^\\/\"'\s]+(?:\\\\|\\|/)")),
)
LIBRARY_EXTENSIONS = (".msp", ".lbm", ".lbm2", ".lbm3")
_LIBRARY_FILE = re.compile(r"(?i)(?:\\\\|\\|/)([^\\/\"'\s<>|*?:]+\.(?:msp|lbm|lbm2|lbm3))(?![A-Za-z0-9])")
_LIBRARY_TOKEN = re.compile(r"<library:([^<>\s]+) sha256=[0-9A-Fa-f]{64}>")
_EXTERNAL = "<external>"
_TOKEN_STOP = set(" \t\r\n\"'<>()[]{},=")
# What the harness inserts into a conversation from a context file or from memory.
CONTEXT_MARKERS = (
    re.compile(r"Contents of [^\r\n]{0,400}?(?:CLAUDE(?:\.local)?\.md|AGENTS\.md|MEMORY\.md)"),
    re.compile(r"Codebase and user instructions are shown below"),
    re.compile(r"user's private global instructions"),
    re.compile(r"auto-memory, persists across conversations"),
    re.compile(r"(?m)^# claudeMd|\\n# claudeMd"),
)


def _decode(data: bytes) -> str:
    """Bytes as characters one to one (latin-1), so offsets are byte offsets and nothing fails to decode."""
    return data.decode("latin-1")


def path_findings(text: str) -> list[tuple[str, int]]:
    """(rule, offset) of the first match of each path rule in `text`."""
    found = []
    for rule, pattern in PATH_RULES:
        match = pattern.search(text)
        if match:
            found.append((rule, match.start()))
    return found


def holds_absolute_path(data: bytes) -> bool:
    """Whether `data` holds a drive letter, UNC or user profile path, as text or as a JSON escape."""
    text = data.decode("utf-8", "replace")
    return any(pattern.search(text) for _rule, pattern in PATH_RULES)


def _token_start(text: str, index: int) -> int:
    floor = max(0, index - 2048)
    while index > floor and text[index - 1] not in _TOKEN_STOP:
        index -= 1
    return index


def library_path_finding(text: str, names: Iterable[str] = ()) -> int | None:
    """The offset of the first library file named with a path, or None. A library file is one with a library extension,
    or one of `names`; it is named with a path when a path separator comes right before it, unless that separator
    follows <external> (the projection's form for a file outside the workspace) or the token is a URL."""
    candidates: list[int] = []
    for match in _LIBRARY_FILE.finditer(text):
        candidates.append(match.start())
    for name in {str(item) for item in names if str(item).strip()}:
        pattern = re.compile(r"(?:\\\\|\\|/)" + re.escape(name) + r"(?![A-Za-z0-9._-])", re.IGNORECASE)
        candidates.extend(match.start() for match in pattern.finditer(text))
    for start in sorted(set(candidates)):
        if text.endswith(_EXTERNAL, 0, start) or text.endswith(_EXTERNAL + "\\", 0, start):
            continue
        token = text[_token_start(text, start):start]
        if "://" in token:
            continue
        return start
    return None


def context_block_finding(text: str) -> int | None:
    """The offset of the first block the harness inserts from a context file or memory, or None."""
    starts = [match.start() for pattern in CONTEXT_MARKERS for match in [pattern.search(text)] if match]
    return min(starts) if starts else None


def is_transcript(relative: str) -> bool:
    parts = relative.replace("\\", "/").split("/")
    name = parts[-1].lower()
    return ("transcripts" in (part.lower() for part in parts[:-1]) or name.endswith(".redacted.jsonl")
            or (name.endswith(".jsonl") and "transcript" in name))


def _chunks(path: Path) -> Iterator[tuple[int, str]]:
    """(byte offset, text) of the file in overlapping chunks; a UTF-16 file (BOM) is decoded whole and re-encoded."""
    with path.open("rb") as handle:
        head = handle.read(2)
        if head in (b"\xff\xfe", b"\xfe\xff"):
            data = head + handle.read()
            yield 0, _decode(data.decode("utf-16", "replace").encode("utf-8"))
            return
        handle.seek(0)
        offset = 0
        tail = b""
        while True:
            block = handle.read(CHUNK)
            if not block:
                if offset == 0 and not tail:
                    yield 0, ""
                return
            data = tail + block
            yield offset - len(tail), _decode(data)
            offset += len(block)
            tail = block[-OVERLAP:]


def library_names_in(bundle: Path) -> set[str]:
    """The file names the bundle names in <library:<name> sha256=...> tokens (the pinned libraries)."""
    names: set[str] = set()
    for path in _files(bundle):
        for _offset, text in _chunks(path):
            names.update(match.group(1) for match in _LIBRARY_TOKEN.finditer(text))
    return names


def _files(bundle: Path) -> list[Path]:
    return [path for path in sorted(Path(bundle).rglob("*")) if path.is_file()]


def check_file(path: Path, relative: str, library_names: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The findings of one file: each rule once, at its first match."""
    names = sorted(set(library_names))
    transcript = is_transcript(relative)
    first: dict[str, int] = {}
    newlines_before: dict[str, int] = {}
    lines_seen = 0
    for start, text in _chunks(path):
        hits: list[tuple[str, int]] = list(path_findings(text))
        library = library_path_finding(text, names)
        if library is not None:
            hits.append(("library_path", library))
        if transcript:
            context = context_block_finding(text)
            if context is not None:
                hits.append(("context_block", context))
        for rule, at in hits:
            if rule not in first:
                first[rule] = start + at
                newlines_before[rule] = text.count("\n", 0, at) + lines_seen
        # Lines before the next chunk's start: the next chunk begins OVERLAP bytes before this one's end.
        lines_seen += text.count("\n", 0, max(0, len(text) - OVERLAP))
    order = [rule for rule, _pattern in PATH_RULES] + ["library_path", "context_block"]
    return [{"file": relative, "rule": rule, "at": first[rule], "line": newlines_before[rule] + 1}
            for rule in order if rule in first]


def check_bundle(bundle: Path, *, library_names: Iterable[str] = (), limit: int | None = None) -> list[dict[str, Any]]:
    """Every file of `bundle` against every rule: [{file, rule, at, line}], at most `limit` when given. The library
    names checked are `library_names` and those the bundle names in <library:...> tokens."""
    bundle = Path(bundle)
    names = set(library_names) | library_names_in(bundle)
    findings: list[dict[str, Any]] = []
    for path in _files(bundle):
        for finding in check_file(path, path.relative_to(bundle).as_posix(), names):
            findings.append(finding)
            if limit is not None and len(findings) >= limit:
                return findings
    return findings


def script_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def report(bundle: Path, *, library_names: Iterable[str] = (), limit: int | None = None) -> dict[str, Any]:
    findings = check_bundle(bundle, library_names=library_names, limit=limit)
    return {"tool": TOOL, "version": VERSION, "script_sha256": script_sha256(), "files": len(_files(bundle)),
            "verdict": "refused" if findings else "pass", "findings": findings}


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundle")
    parser.add_argument("--library-name", action="append", default=[],
                        help="a library file name (beyond the bundle's own <library:...> tokens) never to be released with a path")
    parser.add_argument("--limit", type=int, default=None, help="stop after this many findings")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    bundle = Path(args.bundle)
    if not bundle.is_dir():
        print(f"{bundle} is not a directory.", file=sys.stderr)
        return 2
    try:
        result = report(bundle, library_names=args.library_name, limit=args.limit)
    except OSError as error:
        print(f"The bundle could not be read: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=1, ensure_ascii=False))
    else:
        print(f"{TOOL} {VERSION}: {result['verdict']} ({result['files']} files, {len(result['findings'])} findings)")
        for item in result["findings"]:
            print(f"  {item['file']}:{item['line']} {item['rule']}")
    return 1 if result["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
