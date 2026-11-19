"""VS21 leak scanner (acceptance criterion 7).

Strict mode (default; WS4 outputs, which are library-vs-library aggregates) reports:
  - InChIKeys ([A-Z]{14}-[A-Z]{10}-[A-Z]);
  - SMILES-like tokens;
  - any string from the VS21 NAME set (loaded in memory from the MSPs or a reference DB given on the command
    line; nothing about it is written anywhere);
  - drive and UNC paths;
  - aggregate count cells with 1 <= n < 10 (TSV, markdown tables, JSON keys named n, n_*, *_count, count).

``--allow-neighbour-identity`` (WS7 aggregate report and handback, charter O2): InChIKeys and VS21 names
pass only when the file carries a citation line naming a VS21 file by name together with its full sha256,
and only when each one equals a neighbour_name / neighbour_inchikey of an overlay edge or an SL-hit identity
supplied with --extra-identities. SMILES, paths and small cells are still reported.

Findings are printed with a masked excerpt, never the full matched text. Exit status: 0 clean, 1 findings,
2 usage error.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

INCHIKEY = re.compile(r"(?<![A-Z])[A-Z]{14}-[A-Z]{10}-[A-Z](?![A-Z])")
DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|\\\\[A-Za-z0-9_.$-]+\\")
VS21_FILES = {
    "MSMS-Private-pos-VS21.msp": "0fa0a96bdf1ebbd86349883cb96bb1ff9e327eb6ba7650ee3434056aacb4804f",
    "MSMS-Private-neg-VS21.msp": "7e2581ab31461b5a9102158a6eaf73d0a1702b8564dd75a9281c9b86dc4d132f",
}
_SMILES_CHARS = re.compile(r"^[A-Za-z0-9@+\-\[\]\(\)=#$\\/%.:*]+$")
_ATOM = re.compile(r"Cl|Br|[CNOSPFI]|[cnosp]")
_TOKEN_SPLIT = re.compile(r"[\s|`,;\"'<>{}]+")
COUNT_KEY = re.compile(r"^(n|count|n_.*|.*_count|pairs|n\(.*\))$", re.IGNORECASE)
MIN_NAME_LENGTH = 6
MAX_NAME_WORDS = 12


@dataclass
class Finding:
    path: str
    line: int
    kind: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}: {self.excerpt}"


def mask(text: str) -> str:
    text = text.strip()
    return (text[:3] + "***") if len(text) > 3 else "***"


def looks_like_smiles(token: str) -> bool:
    token = token.strip(".,:;")
    if len(token) < 5 or not _SMILES_CHARS.match(token):
        return False
    if token.startswith("[M") or token.startswith("M+") or token.startswith("M-"):
        return False                       # adducts such as [M+CH3COO]-
    if re.fullmatch(r"[\d.\-+:/%]+", token):
        return False
    if not re.search(r"[()=#\[\]]|[A-Za-z]\d", token):
        return False
    atoms = _ATOM.findall(token)
    letters = re.findall(r"[A-Za-z]", token)
    if len(atoms) < 3:
        return False
    # Words such as "Positive" or identifiers such as "B0-B6" fail this: SMILES letters are atoms.
    return len("".join(atoms)) >= 0.6 * len(letters)


def normalize_name(name: str) -> str:
    return " ".join(name.lower().split())


def load_names_from_msp(paths: list[str]) -> set[str]:
    names: set[str] = set()
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line[:5].upper() == "NAME:":
                    name = normalize_name(line[5:])
                    if len(name) >= MIN_NAME_LENGTH:
                        names.add(name)
    return names


def load_names_from_db(paths: list[str]) -> set[str]:
    names: set[str] = set()
    for path in paths:
        connection = sqlite3.connect("file:" + Path(path).resolve().as_posix() + "?mode=ro", uri=True)
        try:
            for (name,) in connection.execute("SELECT record_name FROM reference_spectrum "
                                              "WHERE record_name IS NOT NULL"):
                name = normalize_name(name)
                if len(name) >= MIN_NAME_LENGTH:
                    names.add(name)
        finally:
            connection.close()
    return names


def allowed_from_overlay(path: str) -> tuple[set[str], set[str]]:
    connection = sqlite3.connect("file:" + Path(path).resolve().as_posix() + "?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT neighbour_name, neighbour_inchikey FROM mn_edge_annotation").fetchall()
    finally:
        connection.close()
    names = {normalize_name(n) for n, _ in rows if n}
    keys = {k.strip().upper() for _, k in rows if k}
    return names, keys


def load_extra_identities(path: str) -> tuple[set[str], set[str]]:
    """TSV with columns name and inchikey (SL-hit identities of dev-set queries; local file)."""
    names, keys = set(), set()
    with open(path, encoding="utf-8") as handle:
        header = handle.readline().rstrip("\n").split("\t")
        for line in handle:
            row = dict(zip(header, line.rstrip("\n").split("\t")))
            if row.get("name"):
                names.add(normalize_name(row["name"]))
            if row.get("inchikey"):
                keys.add(row["inchikey"].strip().upper())
    return names, keys


def has_citation(text: str) -> bool:
    return any(name in text and sha in text for name, sha in VS21_FILES.items())


def _name_hits(line: str, names: set[str]) -> list[str]:
    words = normalize_name(re.sub(r"[|`*]", " ", line)).split(" ")
    hits = []
    for i in range(len(words)):
        for j in range(i + 1, min(len(words), i + MAX_NAME_WORDS) + 1):
            candidate = " ".join(words[i:j]).strip(".,;:()")
            if len(candidate) >= MIN_NAME_LENGTH and candidate in names:
                hits.append(candidate)
    return hits


def _small_counts_json(obj, path: str, out: list[Finding], trail: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and COUNT_KEY.match(str(k)) \
                    and 1 <= v < 10 and float(v).is_integer():
                out.append(Finding(path, 0, "small_count_cell", f"{trail}.{k}={int(v)}"))
            else:
                _small_counts_json(v, path, out, f"{trail}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _small_counts_json(v, path, out, f"{trail}[{i}]")


def _small_counts_table(lines: list[str], path: str, sep: str, out: list[Finding]) -> None:
    header: list[str] | None = None
    for no, line in enumerate(lines, 1):
        if sep == "|":
            if not line.strip().startswith("|"):
                header = None
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c):
                continue
        else:
            cells = line.rstrip("\n").split("\t")
        if header is None:
            header = cells
            continue
        for name, cell in zip(header, cells):
            if COUNT_KEY.match(name.strip().replace(" ", "_")) and re.fullmatch(r"[1-9]", cell.strip()):
                out.append(Finding(path, no, "small_count_cell", f"{name}={cell.strip()}"))


def scan_text(path: str, text: str, *, names: set[str], identity_mode: bool,
              allowed_names: set[str], allowed_keys: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    cited = has_citation(text)
    lines = text.splitlines()
    for no, line in enumerate(lines, 1):
        for match in INCHIKEY.finditer(line):
            key = match.group(0)
            if identity_mode and cited and key in allowed_keys:
                continue
            kind = "inchikey" if not identity_mode else (
                "inchikey_without_vs21_citation" if not cited else "inchikey_not_an_edge_neighbour")
            findings.append(Finding(path, no, kind, mask(key)))
        for match in DRIVE_PATH.finditer(line):
            findings.append(Finding(path, no, "drive_path", mask(line[match.start():match.start() + 12])))
        for token in _TOKEN_SPLIT.split(line):
            if token and looks_like_smiles(token):
                findings.append(Finding(path, no, "smiles_like", mask(token)))
        if names:
            for hit in _name_hits(line, names):
                if identity_mode and cited and hit in allowed_names:
                    continue
                findings.append(Finding(path, no, "vs21_name", mask(hit)))
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        try:
            _small_counts_json(json.loads(text), path, findings)
        except json.JSONDecodeError:
            findings.append(Finding(path, 0, "unparseable_json", ""))
    elif suffix in (".tsv", ".txt"):
        _small_counts_table(lines, path, "\t", findings)
    elif suffix == ".md":
        _small_counts_table(lines, path, "|", findings)
    return findings


def collect_files(targets: list[str]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        p = Path(target)
        if p.is_dir():
            for name in ("report.md", "cutoff_table.tsv", "run.json", "aggregate_report.md"):
                files.extend(sorted(p.glob(name)))
            files.extend(sorted(p.glob("*.md")))
        elif p.exists():
            files.append(p)
    seen, out = set(), []
    for f in files:
        if f.resolve() not in seen:
            seen.add(f.resolve())
            out.append(f)
    return out


def run(args) -> int:
    names: set[str] = set()
    if args.vs21_msp:
        names |= load_names_from_msp(args.vs21_msp)
    if args.vs21_db:
        names |= load_names_from_db(args.vs21_db)
    if not names and not args.no_names:
        print("guard: give --vs21-msp or --vs21-db for the VS21 NAME check, or --no-names to skip it "
              "explicitly", file=sys.stderr)
        return 2
    allowed_names, allowed_keys = set(), set()
    if args.allow_neighbour_identity:
        if not args.overlay and not args.extra_identities:
            print("guard: --allow-neighbour-identity needs --overlay and/or --extra-identities", file=sys.stderr)
            return 2
        for overlay in args.overlay or []:
            n, k = allowed_from_overlay(overlay)
            allowed_names |= n
            allowed_keys |= k
        for extra in args.extra_identities or []:
            n, k = load_extra_identities(extra)
            allowed_names |= n
            allowed_keys |= k
    files = collect_files(args.targets)
    if not files:
        print("guard: nothing to scan", file=sys.stderr)
        return 2
    findings: list[Finding] = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        findings.extend(scan_text(str(f.name), text, names=names, identity_mode=args.allow_neighbour_identity,
                                  allowed_names=allowed_names, allowed_keys=allowed_keys))
    mode = "allow-neighbour-identity" if args.allow_neighbour_identity else "strict"
    print(f"guard ({mode}): {len(files)} file(s), {len(findings)} finding(s); "
          f"name check {'on (' + str(len(names)) + ' names)' if names else 'SKIPPED (--no-names)'}")
    for finding in findings:
        print("  " + str(finding))
    return 1 if findings else 0
