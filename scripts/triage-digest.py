"""Write the triage digest of one accession's inputs: the source facts a triage reads, compact, with no inference.

run-triage-agent.py writes inputs\\digest.md after the handoffs, mwTab texts and listings are frozen, offers it to the
session (record input role triage_digest) and binds it; validate-triage.py rebuilds it from the inputs the record binds,
with this script at its commit, and refuses a digest that differs (T-DIGEST). The agent reads the digest first and the
raw inputs where the digest cuts something or is not clear; the raw inputs win where the two disagree.

    python -I scripts/triage-digest.py --inputs <inputs dir> [--out <file>]

What it holds (docs/llm-assisted-curation.md section 2.2):
  a) the units: declared technical settings, row count, samples sha256, which units share one row table;
  b) one row table for the accession, keyed by sample_id: the row index per unit, raw_file where it is not sample_id
     plus an extension, the Catalog attributes that vary (constant ones once above), the mwTab factors and additional
     data where they differ from the Catalog, the analyses whose Samples line names the row, the name tokens (polarity,
     chromatography, acquisition, read with validate-triage.py's patterns) and the resolution to a member (exact, stem,
     ambiguous:n or none, as validate-triage.py resolves a name);
  c) per analysis the mwTab CH:, MS:, SP:SAMPLEPREP_SUMMARY and AN:ANALYSIS_TYPE lines and the Samples-line count, a
     value over 600 characters cut with its line number, and an extract equal to another's given once;
  d) the listing by folder (count, extensions, size range) with each member marked [row] (some row resolves to it),
     [twin:Tn], [small:<size>] (a raw-like member under a tenth of its folder's median raw-like size: a failed or cut
     injection is evidence), [archive] (an archive inside the archive, unopened: its files are not in the listing) and
     [nonraw];
  e) the byte-identical groups Tn (CRC32 and size, validate-triage.py's T-DUP code path), pairs between two folders
     summarised by folder pair;
  f) rows that resolve to no member or to more than one, and raw-like members no row names;
  g) (1.1.0) the words in raw-like names and in vendor acquisition method folders that name an MS level, as written
     (MS-only words such as MS, MS1, FullScan, Prof, Profile; MS/MS words such as MSMS, MS2, ddMS2, MSe, HDMSe,
     SONAR, MSn, IDA; validate-triage.py's table), counted with
     examples, and each method folder (<name>.m inside a .d) with the number of vendor folders that hold it: the
     contrast between MS-only and MS/MS runs of one study is evidence the triage policy of 2026-11-18 reads (P1).
Deterministic: the same inputs give the same bytes on every machine. No absolute path, no inference, no proposed group.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

DIGEST_VERSION = "1.1.0"
CODE_ROOT = Path(__file__).resolve().parent.parent
CUT = 600


def _validator(code_root: Path = CODE_ROOT) -> Any:
    name = f"validate_triage_for_digest_{hashlib.sha256(str(code_root).encode('utf-8')).hexdigest()[:12]}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, code_root / "scripts" / "validate-triage.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


vt = _validator()


def read_input_files(inputs_dir: Path) -> dict[str, bytes]:
    """The files the digest is built from, by path relative to the inputs directory."""
    inputs_dir = Path(inputs_dir)
    found: dict[str, bytes] = {}
    for path in sorted(inputs_dir.glob("*/handoff.json")) + sorted(inputs_dir.glob("*/samples.json")):
        found[path.relative_to(inputs_dir).as_posix()] = path.read_bytes()
    for path in sorted(inputs_dir.glob("*.tsv")) + sorted(inputs_dir.glob("*.mwtab.txt")):
        found[path.name] = path.read_bytes()
    return found


def _listing(data: bytes) -> list[Any]:
    import csv  # noqa: PLC0415
    import io  # noqa: PLC0415

    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")), delimiter="\t")
    fields = {name.lower(): name for name in (reader.fieldnames or [])}
    crc = fields.get("crc32") or fields.get("crc")
    out = []
    if "path" not in fields or "size" not in fields or crc is None:
        return out
    for row in reader:
        if "type" in fields and row[fields["type"]] not in ("file", ""):
            continue
        out.append(vt.ListedMember(archive=row.get(fields["archive"]) if "archive" in fields else None,
                                   path=vt._norm_path(row[fields["path"]]), size=int(row[fields["size"]] or 0),  # noqa: SLF001
                                   crc32=(row[crc] or "").upper()))
    return out


# ------------------------------------------------------------------------------------------------ mwTab

_KEY = re.compile(r"^([A-Z]{2}:[A-Z0-9_]+|SUBJECT_SAMPLE_FACTORS)\s*\t(.*)$")
_EXTRACT = re.compile(r"^(CH:|MS:|SP:SAMPLEPREP_SUMMARY$|AN:ANALYSIS_TYPE$)")


def parse_mwtab(name: str, text: str) -> dict[str, Any]:
    """{extract: [(key, value)], samples: [names], factors: {sample: {key: value}}} of one mwTab text."""
    lines = text.replace("\r\n", "\n").split("\n")
    extract: list[tuple[str, str]] = []
    first_line: dict[str, int] = {}
    joined: dict[str, list[str]] = defaultdict(list)
    order: list[str] = []
    factors: dict[str, dict[str, str]] = {}
    samples: list[str] = []
    for number, line in enumerate(lines, start=1):
        if line.startswith("Samples\t"):
            samples += [p.strip() for p in line.split("\t")[1:] if p.strip()]
            continue
        match = _KEY.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if key == "SUBJECT_SAMPLE_FACTORS":
            parts = value.split("\t")
            if len(parts) < 2:
                continue
            sample = parts[1].strip()
            found: dict[str, str] = {}
            for pair in (parts[2] if len(parts) > 2 else "").split(" | "):
                if ":" in pair:
                    k, v = pair.split(":", 1)
                    found[k.strip()] = v.strip()
            for pair in (parts[3] if len(parts) > 3 else "").split(";"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    found[k.strip()] = v.strip()
            factors.setdefault(sample, found)
            continue
        if not _EXTRACT.match(key):
            continue
        if key not in joined:
            order.append(key)
            first_line[key] = number
        joined[key].append(value)
    for key in order:
        value = " ".join(v for v in joined[key] if v)
        if len(value) > CUT:
            value = value[:CUT].rstrip() + f" [cut; {name} line {first_line[key]}]"
        extract.append((key, value))
    return {"extract": extract, "samples": samples, "factors": factors}


# ------------------------------------------------------------------------------------------------ the digest

def _short_ion(value: str | None) -> str:
    return {"positive": "pos", "negative": "neg"}.get(value or "", "-")


def _tokens(path: str) -> str:
    acquisition = "+".join(vt.token_acquisition(path)) or "-"
    return f"{_short_ion(vt.token_polarity(path))}/{vt.token_chromatography(path) or '-'}/{acquisition}"


def _cell(value: Any) -> str:
    return str(value).replace("\n", " ").replace("|", "/").strip()


def _size(n: int) -> str:
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("kB", 1 << 10)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} B"


def _ext(path: str) -> str:
    base = vt.basename(path)
    return ("." + base.rsplit(".", 1)[-1]) if "." in base else "(none)"


# Words that name an MS level (section g): validate-triage.py holds the table, which its P1 guard reads too.
ms_level_words = vt.ms_level_words
_METHOD_FOLDER = re.compile(r"(?i)^(.*?\.d)/(?:[^/]+/)*?([^/]+\.m)/")


def _attributes(row: Mapping[str, Any]) -> dict[str, str]:
    attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
    out = {str(k): _cell(v) for k, v in attributes.items() if v not in (None, "")}
    for key, value in row.items():
        if key not in ("sample_id", "raw_file", "attributes", "related_files") and isinstance(value, (str, int, float)):
            out.setdefault(str(key), _cell(value))
    return out


def build_digest_from_files(files: Mapping[str, bytes]) -> bytes:
    norm = {str(k).replace("\\", "/").strip("/"): v for k, v in files.items()}
    units: dict[str, dict[str, Any]] = {}
    listed: list[Any] = []
    archives: dict[str, list[Any]] = defaultdict(list)
    mwtab: dict[str, dict[str, Any]] = {}
    for path in sorted(norm):
        parts = path.split("/")
        if len(parts) == 2 and parts[1] == "handoff.json":
            handoff = json.loads(norm[path].decode("utf-8-sig"))
            samples = norm.get(f"{parts[0]}/samples.json")
            rows = json.loads(samples.decode("utf-8-sig")) if samples is not None else handoff.get("sample_metadata") or []
            units[parts[0]] = {"handoff": handoff, "rows": list(rows),
                               "samples_sha256": hashlib.sha256(samples).hexdigest() if samples is not None else None}
        elif len(parts) == 1 and path.casefold().endswith(".tsv"):
            members = _listing(norm[path])
            listed += members
            archives[path] = members
        elif len(parts) == 1 and path.endswith(".mwtab.txt"):
            analysis = path[: -len(".mwtab.txt")]
            mwtab[analysis] = parse_mwtab(path, norm[path].decode("utf-8", "replace"))
    accession = sorted({str(u["handoff"].get("accession") or "") for u in units.values()} - {""})
    out: list[str] = [f"# Triage digest: {', '.join(accession) or '(no accession)'}", "",
                      f"triage-digest {DIGEST_VERSION}. Source facts read from the inputs beside this file, with no inference "
                      "and no proposed grouping. The raw inputs win where they disagree with this digest (say so in notes). "
                      "Name tokens are read with validate-triage.py's patterns; a row's file resolves to a listed member as "
                      "validate-triage.py resolves a name (exact file name, else the stem, preferring a form that needs no "
                      "conversion).", ""]

    # a) units
    out += ["## Units", "", "| | unit | analysis | chromatography | ion mode | acquisition | target omics | instrument | rows | samples sha256 |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    tables: dict[str, list[str]] = defaultdict(list)
    alias = {unit: f"U{n}" for n, unit in enumerate(sorted(units), start=1)}
    for unit in sorted(units):
        info = units[unit]
        ts = info["handoff"].get("technical_settings") if isinstance(info["handoff"].get("technical_settings"), dict) else {}
        out.append(f"| {alias[unit]} | {unit} | {_cell(info['handoff'].get('source_subrecord_id') or '-')} | {_cell(ts.get('chromatography') or '-')} "
                   f"| {_cell(ts.get('ion_mode') or '-')} | {_cell(ts.get('acquisition_mode') or '-')} "
                   f"| {_cell(ts.get('target_omics') or '-')} | {_cell(ts.get('instrument') or '-')} | {len(info['rows'])} "
                   f"| {(info['samples_sha256'] or '-')[:16]} |")
        key = json.dumps([[r.get("sample_id"), r.get("raw_file")] for r in info["rows"]], ensure_ascii=False)
        tables[key].append(unit)
    out.append("")
    for members in tables.values():
        if len(members) > 1:
            out.append(f"Row tables identical (sample_id and raw_file, in order): {', '.join(members)}.")
    if len(tables) == len(units) and len(units) > 1:
        out.append("Every unit has its own row table.")
    out.append("")

    # the listing and resolution
    resolver = vt.Resolver(m.path for m in listed)
    twins = vt.content_twins(listed)
    groups: list[list[str]] = []
    seen: set[str] = set()
    for key in sorted(twins):
        if key in seen:
            continue
        component, queue = set(), [key]
        while queue:
            current = queue.pop()
            if current in component:
                continue
            component.add(current)
            queue += sorted(twins.get(current, set()) - component)
        seen |= component
        groups.append(sorted(component))
    twin_id = {k: f"T{n}" for n, group in enumerate(groups, start=1) for k in group}

    def display(key: str) -> str:
        return resolver.original.get(key, key)

    # b) rows
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for unit in sorted(units):
        for index, row in enumerate(units[unit]["rows"]):
            ident = (str(row.get("sample_id") or ""), str(row.get("raw_file") or ""))
            entry = rows.setdefault(ident, {"row": row, "index": {}})
            entry["index"][unit] = index
    one_table = len(tables) == 1
    attrs = {ident: _attributes(e["row"]) for ident, e in rows.items()}
    names = sorted({k for a in attrs.values() for k in a})
    constant, identifiers, varying = {}, [], []
    for name in names:
        values = [a.get(name) for a in attrs.values()]
        present = [v for v in values if v is not None]
        if len(present) == len(values) and len(set(present)) == 1:
            constant[name] = present[0]
        elif len(rows) > 3 and len(present) == len(values) and len(set(present)) == len(values) \
                and not all(attrs[ident].get(name) in ident for ident in attrs):
            identifiers.append(name)
        else:
            varying.append(name)
    factors: dict[str, dict[str, str]] = {}
    for analysis in sorted(mwtab):
        for sample, found in mwtab[analysis]["factors"].items():
            factors.setdefault(sample, found)
    shown_factors: dict[tuple[str, str], dict[str, str]] = {}
    for ident, entry in rows.items():
        sample, raw = ident
        found = factors.get(sample) or {}
        diff = {}
        for k, v in found.items():
            if k == "RAW_FILE_NAME" and vt.basename(v) in (raw, vt.basename(raw)):
                continue
            if attrs[ident].get(k) != _cell(v):
                diff[k] = _cell(v)
        shown_factors[ident] = diff
    factor_constant = {}
    if rows and all(factors.get(s) for s, _ in rows):
        for k in sorted({k for d in shown_factors.values() for k in d}):
            values = {d.get(k) for d in shown_factors.values()}
            if len(values) == 1 and None not in values:
                factor_constant[k] = values.pop()
    analyses = {a: set(mwtab[a]["samples"]) for a in sorted(mwtab) if mwtab[a]["samples"]}

    out += ["## Rows", "", f"{len(rows)} distinct row(s) (sample_id and raw_file) over the units' tables."]
    if constant:
        out.append("Catalog attributes with one value in every row: " + "; ".join(f"{k}={v}" for k, v in constant.items()) + ".")
    if identifiers:
        out.append("Catalog attributes unique to every row (identifiers, not shown): " + ", ".join(identifiers) + ".")
    if factor_constant:
        out.append("mwTab sample factors and data (SUBJECT_SAMPLE_FACTORS) the Catalog lacks or reads otherwise, the same "
                   "for every row: " + "; ".join(f"{k}={v}" for k, v in factor_constant.items()) + ".")
    if mwtab and not factors:
        out.append("The mwTab texts carry no SUBJECT_SAMPLE_FACTORS lines.")
    if not mwtab:
        out.append("No mwTab text is among the inputs.")
    elif not analyses:
        out.append("No mwTab text has a Samples line.")
    out += ["Columns: sample_id | row index (" + ("the same in every unit" if one_table else "per unit alias, units without the row left out")
            + ") | raw_file when it is not sample_id plus an extension | Catalog attributes that vary | mw: mwTab factors "
            "and data that differ from the Catalog | S: analyses whose Samples line names the sample | name tokens "
            "polarity/chromatography/acquisition of the member (else of raw_file) | resolution and member.", "", "```text"]
    unresolved, ambiguous_rows = [], []
    referenced: set[str] = set()
    for ident, entry in rows.items():
        sample, raw = ident
        if one_table:
            index = str(next(iter(entry["index"].values())))
        else:
            index = " ".join(f"{alias[u]}={entry['index'][u]}" for u in sorted(units) if u in entry["index"])
        stem = vt.stem(raw)
        cells = [sample, index]
        cells.append("" if raw == sample or stem == sample else f"raw_file={raw}")
        cells.append("; ".join(f"{k}={attrs[ident][k]}" for k in varying if k in attrs[ident]
                               and attrs[ident][k] not in (raw, sample)))
        diff = {k: v for k, v in shown_factors[ident].items() if k not in factor_constant}
        cells.append(("mw " + "; ".join(f"{k}={v}" for k, v in diff.items())) if diff else "")
        named = [a for a, names_ in analyses.items() if sample in names_]
        cells.append(("S:" + ",".join(named)) if named else ("S:-" if analyses else ""))
        by_name = sorted(resolver.by_name.get(vt.basename(raw).casefold(), set()))
        found = resolver.resolve(raw)
        if len(by_name) == 1:
            kind, keys = "exact", by_name
        elif found:
            kind, keys = "stem", [found]
        else:
            keys = sorted(resolver.hits(raw))
            kind = f"ambiguous:{len(keys)}" if keys else "none"
        path = display(keys[0]) if len(keys) == 1 else raw
        cells.append(_tokens(path))
        if kind == "none":
            cells.append("none")
            unresolved.append(f"{sample} ({raw})")
        elif kind.startswith("ambiguous"):
            cells.append(kind + " " + " ; ".join(display(k) for k in keys))
            ambiguous_rows.append(f"{sample} ({raw}): " + " ; ".join(display(k) for k in keys))
        else:
            cells.append(f"{kind} {display(keys[0])}" + (f" [twin:{twin_id[keys[0]]}]" if keys[0] in twin_id else ""))
            referenced.add(keys[0])
        out.append(" | ".join(_cell(c) for c in cells).rstrip(" |"))
    out += ["```", ""]

    # c) mwTab extracts
    out += ["## mwTab extracts", ""]
    if not mwtab:
        out += ["(none)", ""]
    shown: dict[str, str] = {}
    for analysis in sorted(mwtab):
        body = "\n".join(f"{k}\t{v}" for k, v in mwtab[analysis]["extract"])
        count = len(mwtab[analysis]["samples"])
        out.append(f"### {analysis} (Samples line: {count} name(s))")
        if body in shown:
            out += ["", f"Same CH:, MS:, SP:SAMPLEPREP_SUMMARY and AN:ANALYSIS_TYPE lines as {shown[body]}.", ""]
            continue
        shown[body] = analysis
        out += ["", "```text", body or "(no CH:, MS: or SP:SAMPLEPREP_SUMMARY line)", "```", ""]

    # d) listing
    out += ["## Archive listing", ""]
    if not listed:
        out += ["No archive listing is among the inputs.", ""]
    for name in sorted(archives):
        members = archives[name]
        out.append(f"{name}: {len(members)} member(s), {_size(sum(m.size for m in members))}.")
    metadata = [m for m in listed if vt._norm_path(m.path).startswith("__MACOSX/") or vt.basename(m.path).startswith("._")]  # noqa: SLF001
    if metadata:
        out.append(f"Archive metadata (__MACOSX/, ._*): {len(metadata)} member(s), not listed below.")
    out.append("")
    if listed:
        out.append("Marks: [row] a row resolves to it; [twin:Tn] byte-identical group; [small:<size>] a raw-like member "
                   "under a tenth of its folder's median raw-like size; [archive] an archive inside the archive, unopened "
                   "(its files are not listed here, so a row may resolve to none of them and still be inside it); "
                   "[nonraw] not raw data.")
        out.append("")
    skipped = {id(m) for m in metadata}
    by_folder: dict[str, list[Any]] = defaultdict(list)
    for m in listed:
        if id(m) in skipped:
            continue
        key = vt.member_key(m.path)
        shown_path = display(key) if key in resolver.members else vt._norm_path(m.path)  # noqa: SLF001
        folder = shown_path.rsplit("/", 1)[0] if "/" in shown_path else "(root)"
        by_folder[folder].append((shown_path, key, m))
    for folder in sorted(by_folder, key=lambda f: (f.casefold(), f)):
        items = by_folder[folder]
        unique: dict[str, tuple[str, str, int]] = {}
        for shown_path, key, m in items:
            size = unique.get(shown_path, (shown_path, key, 0))[2] + m.size
            unique[shown_path] = (shown_path, key, size)
        sizes = [u[2] for u in unique.values()]
        raw_sizes = sorted(u[2] for u in unique.values() if vt.is_raw_like(u[0]))
        median = raw_sizes[len(raw_sizes) // 2] if len(raw_sizes) >= 3 else 0
        exts = sorted({_ext(u[0]) for u in unique.values()})
        out.append(f"### {folder}/ ({len(unique)} member(s); {', '.join(exts)}; {_size(min(sizes))} to {_size(max(sizes))})")
        out.append("")
        out.append("```text")
        for shown_path, key, member_size in sorted(unique.values(), key=lambda u: (u[0].casefold(), u[0])):
            marks = []
            if key in referenced:
                marks.append("[row]")
            if key in twin_id:
                marks.append(f"[twin:{twin_id[key]}]")
            if vt.is_raw_like(shown_path) and member_size * 10 < median:
                marks.append(f"[small:{_size(member_size)}]")
            if shown_path.casefold().endswith(vt.NESTED_ARCHIVE_EXTENSIONS) and not vt.is_raw_like(shown_path):
                marks.append("[archive]")
            elif not vt.is_raw_like(shown_path):
                marks.append("[nonraw]")
            out.append(vt.basename(shown_path) + ((" " + " ".join(marks)) if marks else ""))
        out += ["```", ""]

    # e) byte-identical groups
    out += ["## Byte-identical groups (CRC32 and size)", ""]
    if not listed:
        out += ["No listing: byte-identical content cannot be ruled out.", ""]
    elif not groups:
        out += ["None.", ""]
    else:
        pairs: dict[tuple[str, str], list[tuple[str, str, str]]] = defaultdict(list)
        others = []
        for group in groups:
            paths = [display(k) for k in group]
            folders = [p.rsplit("/", 1)[0] if "/" in p else "(root)" for p in paths]
            if len(group) == 2 and folders[0] != folders[1]:
                order = sorted(zip(folders, paths))
                pairs[(order[0][0], order[1][0])].append((twin_id[group[0]], order[0][1], order[1][1]))
            else:
                others.append((twin_id[group[0]], paths))
        for (a, b), found in sorted(pairs.items()):
            same = sum(1 for _, x, y in found if vt.basename(x) == vt.basename(y))
            out.append(f"### {a}/ and {b}/: {len(found)} pair(s), {same} with the same file name")
            out += ["", "```text"]
            for tid, x, y in found:
                out.append(f"{tid}: {vt.basename(x)} = {vt.basename(y)}")
            out += ["```", ""]
        if others:
            out += ["### Other groups", "", "```text"]
            for tid, paths in others:
                out.append(f"{tid}: " + " = ".join(paths))
            out += ["```", ""]

    # g) MS-level words and vendor method folders
    out += ["## MS-level words and acquisition method folders", ""]
    names: dict[str, set[tuple[str, str]]] = {}
    for key in sorted(resolver.members):
        shown_name = vt.basename(display(key))
        words = set(ms_level_words(vt.stem(shown_name)))
        if words:
            names[shown_name] = words
    methods: dict[str, set[str]] = defaultdict(set)
    for m in listed:
        match = _METHOD_FOLDER.match(vt._norm_path(m.path))  # noqa: SLF001
        if match:
            methods[match.group(2)].add(match.group(1))
    if not names and not methods:
        out += ["No raw-like name and no method folder carries an MS-level word.", ""]
    for kind in ("MS-only", "MS/MS"):
        labels: dict[str, list[str]] = defaultdict(list)
        for shown_name, words in sorted(names.items(), key=lambda kv: (kv[0].casefold(), kv[0])):
            for word_kind, label in sorted(words):
                if word_kind == kind:
                    labels[label].append(shown_name)
        if labels:
            total = sum(1 for words in names.values() if any(k == kind for k, _ in words))
            out.append(f"Raw-like names with an {kind} word ({total}): "
                       + "; ".join(f"{label} {len(found)} (e.g. {', '.join(found[:3])})" for label, found in sorted(labels.items())) + ".")
    for method in sorted(methods, key=lambda m: (m.casefold(), m)):
        folders = sorted(methods[method], key=lambda f: (f.casefold(), f))
        words = sorted({label for _, label in ms_level_words(method)})
        out.append(f"Method folder {method}: in {len(folders)} vendor folder(s) (e.g. {vt.basename(folders[0])})"
                   + (f"; words {', '.join(words)}" if words else "") + ".")
    if names or methods:
        out.append("")

    # f) what does not pair
    out += ["## Rows and members that do not pair", ""]
    out.append(f"Rows that resolve to no member ({len(unresolved)}): " + ("; ".join(unresolved) if unresolved else "none") + ".")
    out.append(f"Rows that resolve to more than one member ({len(ambiguous_rows)}): "
               + ("; ".join(ambiguous_rows) if ambiguous_rows else "none") + ".")
    loose = [display(k) for k in sorted(resolver.members) if k not in referenced]
    loose_folders: dict[str, int] = defaultdict(int)
    for path in loose:
        loose_folders[path.rsplit("/", 1)[0] if "/" in path else "(root)"] += 1
    out.append(f"Raw-like members no row resolves to ({len(loose)}): "
               + ("; ".join(f"{f}/ {n}" for f, n in sorted(loose_folders.items())) if loose else "none")
               + ". They are the members without [row] above.")
    out.append("")
    return ("\n".join(out)).encode("utf-8")


def build_digest(inputs_dir: Path) -> bytes:
    return build_digest_from_files(read_input_files(inputs_dir))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    data = build_digest(args.inputs)
    if args.out is not None:
        args.out.write_bytes(data)
    else:
        sys.stdout.buffer.write(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
