"""Expand an agent-written triage spec into one msdial-unit-triage.v2 payload per unit, deterministically.

The agent decides; code transcribes (docs/llm-assisted-curation.md section 2.2). The agent writes one compact spec per
accession, <accession>.spec.json (schemas/msdial-unit-triage-spec.v1.json): groups of Catalog rows and archive members
described by a closed match vocabulary, each with one disposition, one set of file fields, one confidence and one
rationale, exceptions for single items, and per unit its status, flags and questions. This script turns it, over the
hashed inputs alone, into the v2 payloads every downstream consumer reads, byte for byte the same on every machine.
validate-triage.py re-runs it (T-SPEC) and refuses a payload that is not its spec's image.

    python -I scripts/expand-triage-spec.py --spec <file> --inputs <dir> --decided-at <iso> --model <id>
        --skill-version <x.y.z> [--agent claude-code] [--purpose <text>] [--out <dir>] [--json]

<dir> is a triage directory's inputs\\ (<unit_id>\\handoff.json with samples.json beside it, <analysis id>.mwtab.txt,
*.tsv archive listings). Exit status: 0 expanded (payloads written to --out when given), 2 findings (nothing written).

Library: expand(spec_bytes, inputs, ctx, spec_name=...) -> Expansion(payloads {unit_id: bytes}, findings [Finding]).
It is pure: no I/O beyond its arguments, no network, no clock, no randomness. Findings (each refuses the expansion):

  X-SCHEMA            the spec breaks schemas/msdial-unit-triage-spec.v1.json, or a rule the schema cannot state
  X-UNITS             the spec's units are not the units of the inputs
  X-UNKNOWN-REF       a group, unit, child or exception target that does not exist
  X-EXCEPTION         an exception that is not one item: a row exception that targets by index without naming exactly
                      one unit (indices are per unit), or one that covers more than one row of a unit's table
  X-UNCOVERED-ROW     a Catalog row of a unit that no row group and no exception covers
  X-OVERLAP-ROW       a Catalog row that two row groups cover
  X-UNCOVERED-MEMBER  a raw-like listed member no row resolves to that no member group covers
  X-OVERLAP-MEMBER    such a member that two member groups cover
  X-EMPTY-GROUP       a group, a flag's or question's groups, or a child that holds nothing
  X-UNRESOLVED        a kept row whose file resolves to no listed member (within the group's resolve.within)
  X-AMBIGUOUS         a kept row whose file resolves to more than one listed member
  X-PLACEHOLDER       an evidence placeholder with no value for an item
  X-HETERO            a kept group (or exception) whose items carry differing name tokens, extensions or top folders,
                      with no "heterogeneous" reason; for acquisition a name without a token differs from one with
  X-DUP-NOKEPT        a shared_content flag whose byte-identical groups (of the items it names, else of the unit's own
                      kept and held items) have no member any unit keeps or holds for a person (excluded needs_person);
                      copies only among members excluded for good are duplicate_copies_excluded
  X-CHILD             a kept item of a unit with children that is in no child, or a child key the unit lacks
  X-SCOPE             a revision whose expansion changes a unit the revision does not name
  X-PAYLOAD-SCHEMA    an expanded payload that breaks schemas/msdial-unit-triage.v2.json (an expander bug)
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

EXPANDER_VERSION = "1.0.0"
SPEC_SCHEMA_ID = "msdial-unit-triage-spec.v1"
PAYLOAD_SCHEMA_ID = "msdial-unit-triage.v2"
SKILL_NAME = "msdial-unit-triage"
CODE_ROOT = Path(__file__).resolve().parent.parent
FINDINGS_SHOWN = 200


def _validator(code_root: Path = CODE_ROOT) -> Any:
    """scripts/validate-triage.py of the same code tree: its name tokens, resolver, twin groups and schema checker are
    the ones the expansion uses, so the payload is read the way the validator reads it."""
    name = f"validate_triage_for_expander_{hashlib.sha256(str(code_root).encode('utf-8')).hexdigest()[:12]}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, code_root / "scripts" / "validate-triage.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


vt = _validator()


@dataclass
class Finding:
    code: str
    message: str
    unit: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "unit": self.unit, "message": self.message}


@dataclass
class UnitInput:
    unit_id: str
    handoff: dict[str, Any]
    handoff_sha256: str
    rows: list[dict[str, Any]]


@dataclass
class ExpansionInputs:
    """What an expansion reads: each unit's handoff copy (its sha256) and sample rows, the archive listings, and the
    MS_METABOLITE_DATA Samples line of each analysis's mwTab text."""

    units: dict[str, UnitInput]
    listed: list[Any]  # validate-triage ListedMember
    mwtab_samples: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class ExpansionContext:
    decided_at: str
    model: str
    skill_version: str
    agent: str = "claude-code"
    purpose: str | None = None
    # A revision: the units it names, and every unit's current payload (an unnamed unit must expand to it, decision
    # fields aside: X-SCOPE).
    revise: frozenset[str] | None = None
    current: dict[str, dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"decided_at": self.decided_at, "model": self.model, "agent": self.agent,
                "skill_version": self.skill_version, "purpose": self.purpose}


@dataclass
class Expansion:
    payloads: dict[str, bytes]
    findings: list[Finding]
    counts: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.findings


# ------------------------------------------------------------------------------------------------ reading inputs

_SAMPLES_LINE = re.compile(r"^Samples\t(.*)$", re.M)


def mwtab_sample_names(text: str) -> set[str]:
    """The sample names an mwTab text's MS_METABOLITE_DATA (or NMR_..., or any data block) Samples line(s) name."""
    names: set[str] = set()
    for match in _SAMPLES_LINE.finditer(text.replace("\r\n", "\n")):
        names |= {part.strip() for part in match.group(1).split("\t") if part.strip()}
    return names


def inputs_from_files(files: Mapping[str, bytes]) -> ExpansionInputs:
    """ExpansionInputs from {path relative to the inputs directory: bytes}: <unit>/handoff.json and <unit>/samples.json
    (else the handoff's sample_metadata), *.tsv listings at the top, <analysis id>.mwtab.txt at the top. Anything else
    (prompts, stored tool results, revision folders, the digest) is not read."""
    norm = {str(k).replace("\\", "/").strip("/"): v for k, v in files.items()}
    units: dict[str, UnitInput] = {}
    listed: list[Any] = []
    mwtab: dict[str, set[str]] = {}
    for path in sorted(norm):
        parts = path.split("/")
        if len(parts) == 2 and parts[1] == "handoff.json":
            data = norm[path]
            handoff = json.loads(data.decode("utf-8-sig"))
            samples = norm.get(f"{parts[0]}/samples.json")
            rows = json.loads(samples.decode("utf-8-sig")) if samples is not None else handoff.get("sample_metadata") or []
            units[parts[0]] = UnitInput(parts[0], handoff, hashlib.sha256(data).hexdigest(), list(rows))
        elif len(parts) == 1 and path.casefold().endswith(".tsv"):
            listed += _read_listing_bytes(norm[path])
        elif len(parts) == 1 and path.endswith(".mwtab.txt"):
            mwtab[path[: -len(".mwtab.txt")]] = mwtab_sample_names(norm[path].decode("utf-8", "replace"))
    return ExpansionInputs(units, listed, mwtab)


def read_input_files(inputs_dir: Path) -> dict[str, bytes]:
    """The files of an inputs directory inputs_from_files reads, by relative path."""
    inputs_dir = Path(inputs_dir)
    found: dict[str, bytes] = {}
    for path in sorted(inputs_dir.glob("*/handoff.json")) + sorted(inputs_dir.glob("*/samples.json")):
        found[path.relative_to(inputs_dir).as_posix()] = path.read_bytes()
    for path in sorted(inputs_dir.glob("*.tsv")) + sorted(inputs_dir.glob("*.mwtab.txt")):
        found[path.name] = path.read_bytes()
    return found


def inputs_from_dir(inputs_dir: Path) -> ExpansionInputs:
    return inputs_from_files(read_input_files(inputs_dir))


def _read_listing_bytes(data: bytes) -> list[Any]:
    import csv  # noqa: PLC0415
    import io  # noqa: PLC0415

    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")), delimiter="\t")
    fields = {name.lower(): name for name in (reader.fieldnames or [])}
    crc_field = fields.get("crc32") or fields.get("crc")
    if "path" not in fields or "size" not in fields or crc_field is None:
        raise ValueError("an archive listing needs path, size and crc32 columns")
    out = []
    for row in reader:
        if "type" in fields and row[fields["type"]] not in ("file", ""):
            continue
        out.append(vt.ListedMember(archive=row.get(fields["archive"]) if "archive" in fields else None,
                                   path=vt._norm_path(row[fields["path"]]), size=int(row[fields["size"]] or 0),  # noqa: SLF001
                                   crc32=(row[crc_field] or "").upper()))
    return out


# ------------------------------------------------------------------------------------------------ the listing

class Listing:
    """The raw-like members by key (vendor folders collapsed, case folded), as the validator keys them."""

    def __init__(self, listed: list[Any]):
        self.resolver = vt.Resolver(m.path for m in listed)
        self.keys = sorted(self.resolver.members)
        self.archive: dict[str, str | None] = {}
        for m in listed:
            key = vt.member_key(m.path)
            if key in self.resolver.members:
                self.archive.setdefault(key, m.archive)
        self.twins = vt.content_twins(listed)
        self._scoped: dict[tuple[tuple[str, ...], str], Any] = {}

    def path(self, key: str) -> str:
        return self.resolver.original.get(key, key)

    def scoped(self, within: tuple[str, ...]) -> Any:
        """A resolver over the members under the folder prefixes `within` (all members when empty)."""
        if not within:
            return self.resolver
        index = (within, "")
        if index not in self._scoped:
            prefixes = tuple(_folder(w) for w in within)
            self._scoped[index] = vt.Resolver(self.path(k) for k in self.keys
                                              if self.path(k).casefold().startswith(prefixes))
        return self._scoped[index]


def _folder(text: str) -> str:
    return vt._norm_path(text).casefold() + "/"  # noqa: SLF001


def resolution(resolver: Any, name: str, exact_only: bool = False) -> tuple[str, list[str]]:
    """(kind, member keys): exact (one member of that file name), stem (one member of that stem, preferring a form that
    needs no conversion), ambiguous (the candidates), none."""
    if not name:
        return "none", []
    by_name = sorted(resolver.by_name.get(vt.basename(name).casefold(), set()))
    if len(by_name) == 1:
        return "exact", by_name
    if exact_only:
        return ("ambiguous", by_name) if by_name else ("none", [])
    found = resolver.resolve(name)
    if found:
        return "stem", [found]
    hits = sorted(resolver.hits(name))
    return ("ambiguous", hits) if hits else ("none", [])


# ------------------------------------------------------------------------------------------------ matching

def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else [str(v) for v in value]


def text_matches(spec: Any, value: Any) -> bool:
    if value is None:
        return False
    text = str(value)
    if isinstance(spec, dict):
        return re.search(spec["regex"], text) is not None
    return text in _strings(spec)


def _ext(key: str) -> str:
    base = vt.basename(key)
    return ("." + base.rsplit(".", 1)[-1].casefold()) if "." in base else ""


def tokens(path: str) -> dict[str, Any]:
    return {"polarity": vt.token_polarity(path), "chromatography": vt.token_chromatography(path),
            "acquisition": vt.token_acquisition(path)}


def polarity_tokens(path: str) -> tuple[str, ...]:
    """Every polarity a path's tokens name (validate-triage.py's pattern), where token_polarity keeps only a single one."""
    return tuple(sorted({"positive" if m.group(1).lower().startswith("pos") else "negative"
                         for m in vt._POLARITY_TOKEN.finditer(vt._norm_path(path))}))  # noqa: SLF001


def chromatography_tokens(path: str) -> tuple[str, ...]:
    norm = vt._norm_path(path)  # noqa: SLF001
    return tuple(name for name, pattern in (("HILIC", vt._HILIC_TOKEN), ("RP", vt._RP_TOKEN)) if pattern.search(norm))  # noqa: SLF001


def token_matches(spec: Mapping[str, Any], path: str) -> bool:
    found = tokens(path)
    if "polarity" in spec and found["polarity"] != spec["polarity"]:
        return False
    if "chromatography" in spec and found["chromatography"] != spec["chromatography"]:
        return False
    if "acquisition" in spec:
        wanted = {s.casefold() for s in _strings(spec["acquisition"])}
        if not wanted & {t.casefold() for t in found["acquisition"]}:
            return False
    return True


def member_matches(spec: Mapping[str, Any], key: str, listing: Listing) -> bool:
    path = listing.path(key)
    folded = path.casefold()
    if "glob" in spec and not any(fnmatch.fnmatchcase(folded, g.casefold()) for g in _strings(spec["glob"])):
        return False
    if "regex" in spec and not any(re.search(r, path) for r in _strings(spec["regex"])):
        return False
    if "folder" in spec and not any(folded.startswith(_folder(f)) for f in _strings(spec["folder"])):
        return False
    if "ext" in spec:
        wanted = {("." + e.casefold().lstrip(".")) for e in _strings(spec["ext"])}
        if _ext(key) not in wanted:
            return False
    if "token" in spec and not token_matches(spec["token"], path):
        return False
    if "paths" in spec and key not in {vt.member_key(p) for p in spec["paths"]}:
        return False
    if "twin" in spec and bool(key in listing.twins) != bool(spec["twin"]):
        return False
    return True


def _indices(items: Iterable[Any]) -> set[int]:
    out: set[int] = set()
    for item in items:
        if isinstance(item, int):
            out.add(item)
        else:
            low, high = (int(x) for x in str(item).split("-", 1))
            out |= set(range(low, high + 1))
    return out


def _attribute(row: Mapping[str, Any], name: str) -> Any:
    attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
    if name in attributes:
        return attributes[name]
    return row.get(name) if name not in ("attributes", "related_files") else None


@dataclass
class RowView:
    unit: str
    index: int
    row: dict[str, Any]
    kind: str  # exact, stem, ambiguous, none (over the whole listing)
    candidates: list[str]

    @property
    def sample_id(self) -> str:
        return str(self.row.get("sample_id") or "")

    @property
    def raw_file(self) -> str:
        return str(self.row.get("raw_file") or "")


def row_matches(spec: Mapping[str, Any], view: RowView, listing: Listing, inputs: ExpansionInputs) -> bool:
    if "unit" in spec and view.unit not in _strings(spec["unit"]):
        return False
    if "rows" in spec and (view.unit != spec["rows"]["unit"] or view.index not in _indices(spec["rows"]["indices"])):
        return False
    if "sample_id" in spec and not text_matches(spec["sample_id"], view.sample_id):
        return False
    if "raw_file" in spec and not text_matches(spec["raw_file"], view.raw_file):
        return False
    for name, wanted in (spec.get("attribute") or {}).items():
        if not text_matches(wanted, _attribute(view.row, name)):
            return False
    if "mwtab_samples" in spec and not any(view.sample_id in inputs.mwtab_samples.get(a, set())
                                           for a in _strings(spec["mwtab_samples"])):
        return False
    if "resolution" in spec and view.kind not in _strings(spec["resolution"]):
        return False
    if "resolved_member" in spec and not any(member_matches(spec["resolved_member"], k, listing) for k in view.candidates):
        return False
    if "token" in spec:
        path = listing.path(view.candidates[0]) if len(view.candidates) == 1 else view.raw_file
        if not token_matches(spec["token"], path):
            return False
    return True


# ------------------------------------------------------------------------------------------------ placeholders

_PLACEHOLDER = re.compile(r"\{([a-z_]+)(?::([^{}]+))?\}")


def fill(value: Any, item: Mapping[str, Any], view: RowView | None, inputs: ExpansionInputs) -> tuple[Any, list[str]]:
    """The evidence value with its placeholders filled for one item, and the placeholders that had no value."""
    if not isinstance(value, str) or "{" not in value:
        return value, []
    missing: list[str] = []
    member = item.get("member_path")

    def one(match: re.Match[str]) -> str:
        name, arg = match.group(1), match.group(2)
        got: Any = None
        if name == "attribute" and view is not None and arg:
            got = _attribute(view.row, arg)
        elif name == "sample_id" and view is not None:
            got = view.sample_id or None
        elif name == "raw_file" and view is not None:
            got = view.raw_file or None
        elif name == "member_path":
            got = member
        elif name == "folder" and member:
            got = vt._norm_path(member).rsplit("/", 1)[0] if "/" in vt._norm_path(member) else None  # noqa: SLF001
        elif name == "token" and arg in ("polarity", "chromatography", "acquisition"):
            source = member or (view.raw_file if view is not None else None)
            if source:
                token = tokens(source)[arg]
                got = "+".join(token) if isinstance(token, tuple) else token
                got = got or None
        elif name == "mwtab_samples" and view is not None and arg:
            got = arg if view.sample_id in inputs.mwtab_samples.get(arg, set()) else None
        else:
            return match.group(0) if name not in ("attribute", "sample_id", "raw_file", "member_path", "folder",
                                                  "token", "mwtab_samples") else _missing(match.group(0))
        if got is None or got == "":
            return _missing(match.group(0))
        return str(got)

    def _missing(text: str) -> str:
        missing.append(text)
        return text

    return _PLACEHOLDER.sub(one, value), missing


# ------------------------------------------------------------------------------------------------ the expansion

ROW_REASONS = {"belongs_to_other_unit", "no_signal_abstained", "duplicate_content", "out_of_scope_ms1_only",
               "polarity_switching", "requires_conversion", "not_in_archive", "needs_person", "other"}
# A member keeps the spec's reason, so the validator's T-EXCL guards (needs_person in a unit that asks, an MS1-only or
# polarity-switching exclusion with its flag) hold for members as for rows. not_in_archive is never a member's reason
# (a member is in the archive): X-SCHEMA.
MEMBER_REASONS = {"not_raw_data", "archive_metadata", "alternative_format_copy", "requires_conversion",
                  "duplicate_content", "belongs_to_other_unit", "no_signal_abstained", "out_of_scope",
                  "out_of_scope_ms1_only", "polarity_switching", "needs_person", "other"}


def row_reason(reason: str) -> str:
    return reason if reason in ROW_REASONS else "other"


def member_reason(reason: str) -> str:
    return reason if reason in MEMBER_REASONS else "other"


def _keepers(disposition: Mapping[str, Any]) -> list[str]:
    keep = disposition.get("keep")
    if keep is None:
        return []
    return [keep] if isinstance(keep, str) else list(keep)


def _child(disposition: Mapping[str, Any], unit: str) -> str | None:
    child = disposition.get("child")
    if isinstance(child, dict):
        return child.get(unit)
    return child


@dataclass
class Rule:
    """A group, or an exception resolved against the group it names."""

    id: str
    applies_to: str
    disposition: dict[str, Any]
    file: dict[str, Any] | None
    evidence: list[dict[str, Any]]
    confidence: str
    rationale: str
    within: tuple[str, ...] = ()
    exact_only: bool = False
    heterogeneous: str | None = None
    member_path: str | None = None  # an exception's named member
    is_exception: bool = False


@dataclass
class Item:
    """One row of one unit's table, or one unreferenced member, with the rule that covers it."""

    unit: str | None
    rule: Rule
    view: RowView | None = None
    key: str | None = None  # the member it is (rows: resolved; None when it resolves to nothing single)


class Expander:
    def __init__(self, spec: dict[str, Any], inputs: ExpansionInputs, ctx: ExpansionContext, spec_name: str,
                 spec_sha256: str, code_root: Path = CODE_ROOT):
        self.spec = spec
        self.inputs = inputs
        self.ctx = ctx
        self.spec_name = spec_name
        self.spec_sha256 = spec_sha256
        self.code_root = code_root
        self.findings: list[Finding] = []
        self.listing = Listing(inputs.listed)
        self.units = sorted(inputs.units)

    def find(self, code: str, message: str, unit: str | None = None) -> None:
        self.findings.append(Finding(code, message, unit))

    # -- rules ---------------------------------------------------------------------------------------------------
    def rules(self) -> tuple[dict[str, Rule], dict[str, dict[str, Any]], dict[str, Rule]]:
        groups: dict[str, Rule] = {}
        raw_groups: dict[str, dict[str, Any]] = {}
        for g in self.spec["groups"]:
            if g["id"] in groups:
                self.find("X-SCHEMA", f"group id {g['id']} is used twice")
                continue
            if g["applies_to"] == "rows":
                problems = vt.schema_errors(g["match"], self.schema["$defs"]["row_match"], self.schema, f"group {g['id']}.match")
            else:
                problems = vt.schema_errors(g["match"], self.schema["$defs"]["member_match"], self.schema, f"group {g['id']}.match")
                if "resolve" in g:
                    problems.append(f"group {g['id']}: resolve is for row groups")
            for problem in problems:
                self.find("X-SCHEMA", problem)
            self._check_regexes(g["match"], f"group {g['id']}")
            self._check_disposition(g["disposition"], f"group {g['id']}", g.get("file"), g.get("evidence"))
            if g["applies_to"] == "members" and g["disposition"].get("exclude") == "not_in_archive":
                self.find("X-SCHEMA", f"group {g['id']}: a member is in the archive; not_in_archive is a row's reason")
            resolve = g.get("resolve") or {}
            groups[g["id"]] = Rule(g["id"], g["applies_to"], dict(g["disposition"]), g.get("file"), list(g.get("evidence") or []),
                                   g["confidence"], g["rationale"], tuple(_strings(resolve.get("within"))),
                                   resolve.get("by") == "exact", g.get("heterogeneous"))
            raw_groups[g["id"]] = g
        exceptions: dict[str, Rule] = {}
        for e in self.spec.get("exceptions") or []:
            if e["id"] in groups or e["id"] in exceptions:
                self.find("X-SCHEMA", f"exception id {e['id']} is used twice or names a group id")
                continue
            if ("row" in e) == ("member" in e):
                self.find("X-SCHEMA", f"exception {e['id']} names exactly one of row and member")
                continue
            base = groups.get(e.get("group") or "")
            if e.get("group") and base is None:
                self.find("X-UNKNOWN-REF", f"exception {e['id']} names group {e['group']}, which the spec does not have")
                continue
            applies = "rows" if "row" in e else "members"
            if "index" in (e.get("row") or {}) and len(_strings(e.get("unit"))) != 1:
                self.find("X-EXCEPTION", f"exception {e['id']} targets row index {e['row']['index']}, which is per unit, "
                          "without naming exactly one unit; name the unit, or target the row by sample_id or raw_file")
                continue
            if base is not None and base.applies_to != applies:
                self.find("X-UNKNOWN-REF", f"exception {e['id']} is for {applies} but names {base.applies_to} group {base.id}")
                continue
            disposition = dict(e.get("disposition") or (base.disposition if base else {}))
            if not disposition:
                self.find("X-SCHEMA", f"exception {e['id']} names neither a group nor a disposition")
                continue
            file = dict(base.file or {}) if base is not None else {}
            file.update(e.get("file") or {})
            evidence = list(e.get("evidence") or (base.evidence if base else []))
            self._check_disposition(disposition, f"exception {e['id']}", file or None, evidence)
            if applies == "members" and disposition.get("exclude") == "not_in_archive":
                self.find("X-SCHEMA", f"exception {e['id']}: a member is in the archive; not_in_archive is a row's reason")
            if "member_path" in e and applies != "rows":
                self.find("X-SCHEMA", f"exception {e['id']}: member_path is for a row exception")
            exceptions[e["id"]] = Rule(e["id"], applies, disposition, file or None, evidence, e["confidence"], e["rationale"],
                                       base.within if base else (), base.exact_only if base else False, None,
                                       e.get("member_path"), True)
        return groups, raw_groups, exceptions

    def _check_regexes(self, match: Mapping[str, Any], where: str) -> None:
        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "regex":
                        for pattern in _strings(item):
                            try:
                                re.compile(pattern)
                            except re.error as error:
                                self.find("X-SCHEMA", f"{where}: regex {pattern!r} does not compile ({error})")
                    else:
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(match)

    def _check_disposition(self, disposition: Mapping[str, Any], where: str, file: Any, evidence: Any) -> None:
        keepers = _keepers(disposition)
        for unit in keepers:
            if unit not in self.inputs.units:
                self.find("X-UNKNOWN-REF", f"{where} keeps for unit {unit}, which is not a unit of the inputs")
        if keepers:
            if len(keepers) > 1 and not disposition.get("shared_reason"):
                self.find("X-SCHEMA", f"{where} is kept by {len(keepers)} units without a shared_reason")
            if len(set(keepers)) != len(keepers):
                self.find("X-SCHEMA", f"{where} names a unit twice in keep")
            if not file:
                self.find("X-SCHEMA", f"{where} is kept but gives no file fields")
            if not evidence:
                self.find("X-SCHEMA", f"{where} is kept but cites no evidence")
        elif "shared_reason" in disposition or "child" in disposition:
            self.find("X-SCHEMA", f"{where}: child and shared_reason are for a kept group")

    # -- the item table --------------------------------------------------------------------------------------------
    def expand(self) -> dict[str, bytes]:
        self.schema = json.loads((self.code_root / "schemas" / f"{SPEC_SCHEMA_ID}.json").read_text(encoding="utf-8"))
        for problem in vt.schema_errors(self.spec, self.schema, self.schema)[:FINDINGS_SHOWN]:
            self.find("X-SCHEMA", problem)
        if self.findings:
            return {}
        accession = {str(u.handoff.get("accession") or "") for u in self.inputs.units.values()} - {""}
        if accession and self.spec["accession"] not in accession:
            self.find("X-UNITS", f"the spec is for {self.spec['accession']}, the inputs for {', '.join(sorted(accession))}")
        named = set(self.spec["units"])
        missing, extra = sorted(set(self.units) - named), sorted(named - set(self.units))
        if missing:
            self.find("X-UNITS", f"the spec has no entry for unit(s) {', '.join(missing)}")
        if extra:
            self.find("X-UNITS", f"the spec names unit(s) {', '.join(extra)}, which the inputs do not hold")
        if not self.units:
            self.find("X-UNITS", "the inputs hold no unit (no <unit_id>/handoff.json)")
        groups, raw_groups, exceptions = self.rules()
        if self.findings:
            return {}
        self.groups, self.exceptions = groups, exceptions
        rule_of = {**groups, **exceptions}

        # Rows: per unit, one rule each.
        rows: dict[str, list[Item]] = {}
        hits: dict[str, int] = {gid: 0 for gid in rule_of}
        uncovered: dict[str, list[str]] = {}
        overlap: dict[str, list[str]] = {}
        unplaced: set[str] = set()  # the files of rows no rule placed: reported once, as rows, not again as members
        row_exceptions = [(e, exceptions[e["id"]]) for e in self.spec.get("exceptions") or [] if e["id"] in exceptions and "row" in e]
        for unit in self.units:
            for e, rule in row_exceptions:
                covered = [index for index, row in enumerate(self.inputs.units[unit].rows)
                           if self._targets_row(e, RowView(unit, index, row, "none", []))]
                if len(covered) > 1:
                    self.find("X-EXCEPTION", f"exception {rule.id} covers {len(covered)} rows of this unit's table "
                              f"({', '.join(str(i) for i in covered[:6])}); an exception is one item, a group covers several",
                              unit)
        if self.findings:
            return {}
        for unit in self.units:
            items: list[Item] = []
            for index, row in enumerate(self.inputs.units[unit].rows):
                kind, candidates = resolution(self.listing.resolver, str(row.get("raw_file") or ""))
                view = RowView(unit, index, row, kind, candidates)
                special = [r for e, r in row_exceptions if self._targets_row(e, view)]
                if len(special) > 1:
                    self.find("X-OVERLAP-ROW", f"row {index} ({view.sample_id}) is named by exceptions "
                              f"{', '.join(r.id for r in special)}", unit)
                    continue
                if special:
                    rule = special[0]
                else:
                    matched = [g for g in groups.values() if g.applies_to == "rows"
                               and row_matches(raw_groups[g.id]["match"], view, self.listing, self.inputs)]
                    if not matched:
                        uncovered.setdefault(unit, []).append(f"{index} ({view.sample_id})")
                        unplaced.update(candidates[:1] if len(candidates) == 1 else [])
                        continue
                    if len(matched) > 1:
                        overlap.setdefault(unit, []).append(f"{index} ({view.sample_id}: {', '.join(g.id for g in matched)})")
                        unplaced.update(candidates[:1] if len(candidates) == 1 else [])
                        continue
                    rule = matched[0]
                hits[rule.id] += 1
                items.append(Item(unit, rule, view, None))
            rows[unit] = items
        for unit, shown in uncovered.items():
            self.find("X-UNCOVERED-ROW", f"{len(shown)} row(s) no row group or exception covers: {', '.join(shown[:12])}"
                      + (" ..." if len(shown) > 12 else ""), unit)
        for unit, shown in overlap.items():
            self.find("X-OVERLAP-ROW", f"{len(shown)} row(s) covered by more than one group: {', '.join(shown[:8])}"
                      + (" ..." if len(shown) > 8 else ""), unit)

        # Each covered row's file: by its rule's resolution (an exception's member_path first).
        referenced: set[str] = set(unplaced)
        unresolved: dict[str, list[str]] = {}
        ambiguous: dict[str, list[str]] = {}
        no_listing = not self.listing.keys
        for unit, items in rows.items():
            for item in items:
                view = item.view
                assert view is not None
                if item.rule.member_path:
                    key = vt.member_key(item.rule.member_path)
                    if key not in self.listing.resolver.members:
                        self.find("X-UNRESOLVED", f"exception {item.rule.id} names {item.rule.member_path}, which is not a "
                                  "raw-like member of the listing", unit)
                        continue
                    item.key = key
                else:
                    kind, found = resolution(self.listing.scoped(item.rule.within), view.raw_file, item.rule.exact_only)
                    item.key = found[0] if len(found) == 1 else None
                    if self._kept_in(item.rule, unit) and not no_listing and item.key is None:
                        where = f" within {', '.join(item.rule.within)}" if item.rule.within else ""
                        (ambiguous if found else unresolved).setdefault(unit, []).append(
                            f"{view.index} ({view.raw_file}{where}" + (f": {len(found)} members" if found else "") + ")")
                if item.key:
                    referenced.add(item.key)
        for unit, shown in unresolved.items():
            self.find("X-UNRESOLVED", f"{len(shown)} kept row(s) resolve to no listed member: {', '.join(shown[:10])}"
                      + (" ..." if len(shown) > 10 else "") + "; name the member with an exception or exclude the row", unit)
        for unit, shown in ambiguous.items():
            self.find("X-AMBIGUOUS", f"{len(shown)} kept row(s) resolve to more than one member: {', '.join(shown[:10])}"
                      + (" ..." if len(shown) > 10 else "") + "; narrow the group's resolve.within or name the member", unit)

        # Members no row resolves to: one member rule each.
        members: list[Item] = []
        member_exceptions = {vt.member_key(e["member"]): exceptions[e["id"]] for e in self.spec.get("exceptions") or []
                             if "member" in e and e["id"] in exceptions}
        for key, rule in member_exceptions.items():
            if key not in self.listing.resolver.members:
                self.find("X-UNKNOWN-REF", f"exception {rule.id} names member {key}, which is not a raw-like member of the listing")
            elif key in referenced:
                self.find("X-UNKNOWN-REF", f"exception {rule.id} names member {self.listing.path(key)}, which a row resolves to; "
                          "a row exception covers it")
        loose, crowded = [], []
        for key in self.listing.keys:
            if key in referenced:
                continue
            if key in member_exceptions:
                rule = member_exceptions[key]
            else:
                matched = [g for g in groups.values() if g.applies_to == "members"
                           and member_matches(raw_groups[g.id]["match"], key, self.listing)]
                if not matched:
                    loose.append(self.listing.path(key))
                    continue
                if len(matched) > 1:
                    crowded.append(f"{self.listing.path(key)} ({', '.join(g.id for g in matched)})")
                    continue
                rule = matched[0]
            hits[rule.id] += 1
            members.append(Item(None, rule, None, key))
        if loose:
            self.find("X-UNCOVERED-MEMBER", f"{len(loose)} raw-like member(s) no row resolves to and no member group covers: "
                      f"{', '.join(loose[:10])}" + (" ..." if len(loose) > 10 else ""))
        if crowded:
            self.find("X-OVERLAP-MEMBER", f"{len(crowded)} member(s) covered by more than one group: {', '.join(crowded[:8])}"
                      + (" ..." if len(crowded) > 8 else ""))
        for rid, count in hits.items():
            if count == 0:
                what = "exception" if rid in exceptions else "group"
                self.find("X-EMPTY-GROUP" if what == "group" else "X-UNKNOWN-REF",
                          f"{what} {rid} matches nothing in any unit")

        # Unit-level references.
        for unit, entry in self.spec["units"].items():
            if unit not in self.inputs.units:
                continue
            refs = [*(g for x in entry.get("exclude") or [] for g in x["groups"]),
                    *(g for f in entry.get("flags") or [] for g in f.get("groups") or []),
                    *(g for q in entry.get("needs_person") or [] for g in q.get("groups") or []),
                    *(g for s in entry.get("shared_with") or [] for g in s["groups"])]
            for ref in refs:
                if ref not in rule_of:
                    self.find("X-UNKNOWN-REF", f"unit {unit} names group {ref}, which the spec does not have", unit)
            for share in entry.get("shared_with") or []:
                if share["unit_id"] not in self.inputs.units:
                    self.find("X-UNKNOWN-REF", f"unit {unit} shares with {share['unit_id']}, which is not a unit", unit)
            for x in entry.get("exclude") or []:
                if x["reason"] == "belongs_to_other_unit":
                    self.find("X-SCHEMA", f"unit {unit}: an exclude entry gives its own reason, never belongs_to_other_unit", unit)
                if x["reason"] == "not_in_archive" and any(rule_of[g].applies_to == "members" for g in x["groups"] if g in rule_of):
                    self.find("X-SCHEMA", f"unit {unit}: an exclude entry gives not_in_archive for a member group; a member "
                              "is in the archive", unit)
        for flag in self.spec.get("shared_flags") or []:
            for unit in flag["units"]:
                if unit not in self.inputs.units:
                    self.find("X-UNKNOWN-REF", f"a shared flag {flag['code']} names unit {unit}, which is not a unit")
            for ref in flag.get("groups") or []:
                if ref not in rule_of:
                    self.find("X-UNKNOWN-REF", f"a shared flag {flag['code']} names group {ref}, which the spec does not have")
        if self.findings:
            return {}

        self._hetero(rows, members)
        payloads = {unit: self._payload(unit, rows[unit], members) for unit in self.units}
        self._dup_nokept(payloads)
        self._scope(payloads)
        out: dict[str, bytes] = {}
        payload_schema = json.loads((self.code_root / "schemas" / f"{PAYLOAD_SCHEMA_ID}.json").read_text(encoding="utf-8"))
        for unit, document in payloads.items():
            for problem in vt.schema_errors(document, payload_schema, payload_schema)[:20]:
                self.find("X-PAYLOAD-SCHEMA", problem, unit)
            out[unit] = (json.dumps(document, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
        self.documents = payloads
        return out

    def _targets_row(self, entry: Mapping[str, Any], view: RowView) -> bool:
        if "unit" in entry and view.unit not in _strings(entry["unit"]):
            return False
        target = entry.get("row") or {}
        if "index" in target and view.index != target["index"]:
            return False
        if "sample_id" in target and view.sample_id != target["sample_id"]:
            return False
        if "raw_file" in target and view.raw_file != target["raw_file"]:
            return False
        return True

    def _excluded_by_unit(self, rule: Rule, unit: str) -> dict[str, Any] | None:
        for entry in self.spec["units"][unit].get("exclude") or []:
            if rule.id in entry["groups"]:
                return entry
        return None

    def _kept_in(self, rule: Rule, unit: str) -> bool:
        return unit in _keepers(rule.disposition) and self._excluded_by_unit(rule, unit) is None

    # -- checks over the whole expansion ---------------------------------------------------------------------------
    def _hetero(self, rows: dict[str, list[Item]], members: list[Item]) -> None:
        paths: dict[str, list[str]] = {}
        for items in rows.values():
            for item in items:
                if not _keepers(item.rule.disposition):
                    continue
                path = self.listing.path(item.key) if item.key else (item.view.raw_file if item.view else "")
                paths.setdefault(item.rule.id, [])
                if path not in paths[item.rule.id]:
                    paths[item.rule.id].append(path)
        for item in members:
            if not _keepers(item.rule.disposition):
                continue
            paths.setdefault(item.rule.id, []).append(self.listing.path(item.key or ""))
        for rid, found in paths.items():
            rule = self.groups.get(rid) or self.exceptions[rid]
            if rule.heterogeneous:
                continue
            problems = []
            # Per item, every token its path carries; items whose token sets differ make the group heterogeneous. For
            # acquisition a name with no token differs from one with a token: a group of untokened DDA files does not
            # sweep in a QC_MS1 or a _tDDA file under its own acquisition_mode, confidence and rationale.
            polarity = sorted({p for p in (polarity_tokens(x) for x in found) if p})
            chromatography = sorted({c for c in (chromatography_tokens(x) for x in found) if c})
            acquisition = sorted({vt.token_acquisition(x) for x in found})
            extensions = sorted({_ext(vt.member_key(x)) for x in found})
            tops = sorted({vt._norm_path(x).split("/", 1)[0] if "/" in vt._norm_path(x) else "" for x in found})  # noqa: SLF001
            if len(polarity) > 1:
                problems.append("polarity tokens " + "; ".join("+".join(p) for p in polarity[:6]))
            if len(chromatography) > 1:
                problems.append("chromatography tokens " + "; ".join("+".join(c) for c in chromatography[:6]))
            if len(acquisition) > 1:
                problems.append("acquisition tokens " + "; ".join("+".join(a) or "(none)" for a in acquisition[:6]))
            if len(extensions) > 1:
                problems.append(f"extensions {', '.join(e or '(none)' for e in extensions)}")
            if len(tops) > 1:
                problems.append(f"top folders {', '.join(t or '(root)' for t in tops[:6])}")
            if problems:
                self.find("X-HETERO", f"kept group {rid} mixes {'; '.join(problems)}; split the group, or state why in "
                          "\"heterogeneous\" (and flag what makes the run inhomogeneous)")

    def _dup_nokept(self, payloads: dict[str, dict[str, Any]]) -> None:
        # validate-triage.py's T-DUP test, on the expanded payloads: each flag is judged on the files it is about (its
        # groups, else the unit's shared_content questions' groups, else the unit's own kept and held files).
        for unit, has_twins in vt.shared_content_without_keepers(payloads, self.listing.twins, self.listing.resolver):
            self.find("X-DUP-NOKEPT", "shared_content is flagged, but no unit keeps or holds for a person (excluded "
                      "needs_person) a file of the byte-identical groups the flag is about"
                      + ("" if has_twins else " (none of those files has a twin)")
                      + "; copies only among files excluded for good are duplicate_copies_excluded (informational)", unit)

    def _scope(self, payloads: dict[str, dict[str, Any]]) -> None:
        if self.ctx.revise is None or self.ctx.current is None:
            return
        for unit, document in payloads.items():
            if unit in self.ctx.revise:
                continue
            current = self.ctx.current.get(unit)
            if current is None or comparable(current) != comparable(document):
                self.find("X-SCOPE", "the revision changes this unit, which it does not name; revise only the units the "
                          "task names and leave the others' groups as they are", unit)

    # -- one unit's payload ----------------------------------------------------------------------------------------
    def _evidence(self, rule: Rule, item: dict[str, Any], view: RowView | None, unit: str) -> list[dict[str, Any]]:
        out = []
        for e in rule.evidence:
            value, missing = fill(e["value"], item, view, self.inputs)
            if missing:
                label = item.get("member_path") or item.get("raw_file")
                self.find("X-PLACEHOLDER", f"{rule.id}: {', '.join(missing)} has no value for {label}", unit)
            out.append({"source": e["source"], "field": e["field"], "value": value})
        return out

    def _exclusion(self, rule: Rule, unit: str, is_row: bool) -> tuple[str, str | None, str, str, str]:
        """(reason, belongs_to_unit, detail, confidence, rationale) of a rule's item in a unit that does not keep it."""
        own = self._excluded_by_unit(rule, unit)
        if own is not None:
            reason = own["reason"]
            return ((row_reason if is_row else member_reason)(reason), None, own["detail"],
                    own.get("confidence") or rule.confidence, own.get("rationale") or rule.rationale)
        keepers = _keepers(rule.disposition)
        if keepers:
            owner = keepers[0]
            return ("belongs_to_other_unit", owner, f"Kept by {owner} ({'exception' if rule.is_exception else 'group'} "
                    f"{rule.id}).", rule.confidence, f"Belongs to {owner}: {rule.rationale}")
        reason = rule.disposition["exclude"]
        return ((row_reason if is_row else member_reason)(reason), None, rule.disposition["detail"], rule.confidence,
                rule.rationale)

    def _payload(self, unit: str, rows: list[Item], members: list[Item]) -> dict[str, Any]:
        entry = self.spec["units"][unit]
        source = self.inputs.units[unit]
        handoff = source.handoff
        kept_rows, excluded_rows, added, excluded_members = [], [], [], []
        by_rule: dict[str, dict[str, list[Any]]] = {}

        def note(rule_id: str, kind: str, value: Any) -> None:
            by_rule.setdefault(rule_id, {"rows": [], "paths": [], "kept_paths": []})[kind].append(value)

        for item in sorted(rows, key=lambda i: i.view.index if i.view else 0):
            view = item.view
            assert view is not None
            member = self.listing.path(item.key) if item.key else None
            note(item.rule.id, "rows", view.index)
            if self._kept_in(item.rule, unit):
                f = item.rule.file or {}
                kept = {"row_index": view.index, "sample_id": view.sample_id, "raw_file": view.raw_file,
                        "member_path": member, "archive": self.listing.archive.get(item.key) if item.key else None,
                        "role": f["role"], "ion_mode": f["ion_mode"], "chromatography": f["chromatography"],
                        "acquisition_mode": f["acquisition_mode"], "basis": f["basis"]}
                kept["evidence"] = self._evidence(item.rule, kept, view, unit)
                kept.update(confidence=item.rule.confidence, rationale=item.rule.rationale, rule_id=item.rule.id)
                kept_rows.append(kept)
                note(item.rule.id, "kept_paths", member or view.raw_file)
            else:
                reason, owner, detail, confidence, rationale = self._exclusion(item.rule, unit, True)
                excluded_rows.append({"row_index": view.index, "sample_id": view.sample_id, "raw_file": view.raw_file,
                                      "member_path": member, "reason": reason, "belongs_to_unit": owner, "detail": detail,
                                      "confidence": confidence, "rationale": rationale, "rule_id": item.rule.id})
        for item in sorted(members, key=lambda i: (i.key or "", self.listing.path(i.key or ""))):
            path = self.listing.path(item.key or "")
            note(item.rule.id, "paths", path)
            if self._kept_in(item.rule, unit):
                f = item.rule.file or {}
                kept = {"member_path": path, "archive": self.listing.archive.get(item.key or ""), "role": f["role"],
                        "ion_mode": f["ion_mode"], "chromatography": f["chromatography"],
                        "acquisition_mode": f["acquisition_mode"], "basis": f["basis"]}
                kept["evidence"] = self._evidence(item.rule, kept, None, unit)
                kept.update(confidence=item.rule.confidence, rationale=item.rule.rationale, rule_id=item.rule.id)
                added.append(kept)
                note(item.rule.id, "kept_paths", path)
            else:
                reason, owner, detail, confidence, rationale = self._exclusion(item.rule, unit, False)
                excluded_members.append({"member_path": path, "archive": self.listing.archive.get(item.key or ""),
                                         "reason": reason, "belongs_to_unit": owner, "detail": detail,
                                         "confidence": confidence, "rationale": rationale, "rule_id": item.rule.id})

        def refs(groups: list[str] | None, where: str, *, kept_only: bool = False) -> dict[str, Any]:
            if not groups:
                return {}
            indices = sorted({i for g in groups for i in by_rule.get(g, {}).get("rows", [])})
            paths = sorted({p for g in groups for p in by_rule.get(g, {}).get("kept_paths" if kept_only else "paths", [])},
                           key=lambda p: (p.casefold(), p))
            if kept_only:
                if not paths:
                    self.find("X-EMPTY-GROUP", f"{where} names group(s) {', '.join(groups)}, which this unit keeps nothing of", unit)
                return {"member_paths": paths}
            if not indices and not paths:
                self.find("X-EMPTY-GROUP", f"{where} names group(s) {', '.join(groups)}, which hold nothing in this unit", unit)
            return {"row_indices": indices, "member_paths": paths}

        # Children: the kept items by their disposition's child key.
        children = []
        declared = {c["child_key"]: c for c in entry.get("children") or []}
        child_paths: dict[str, list[str]] = {key: [] for key in declared}
        for kept in [*kept_rows, *added]:
            rule = self.groups.get(kept["rule_id"]) or self.exceptions[kept["rule_id"]]
            key = _child(rule.disposition, unit)
            path = kept.get("member_path") or kept.get("raw_file")
            if key is None:
                if declared:
                    self.find("X-CHILD", f"{path} is kept ({rule.id}) but in no child of a unit that has children", unit)
                continue
            if key not in declared:
                self.find("X-CHILD", f"{rule.id} puts {path} in child {key}, which unit {unit} does not declare", unit)
                continue
            child_paths[key].append(path)
        for key, child in declared.items():
            if not child_paths[key]:
                self.find("X-CHILD", f"child {key} holds no kept file", unit)
                continue
            children.append({"child_key": key, "ion_mode": child["ion_mode"], "acquisition_mode": child["acquisition_mode"],
                             "member_paths": sorted(set(child_paths[key]), key=lambda p: (p.casefold(), p))})

        # shared_with: a group several units keep, reciprocally, then what the unit names.
        shared = []
        for rule in [*self.groups.values(), *self.exceptions.values()]:
            keepers = _keepers(rule.disposition)
            if len(keepers) > 1 and unit in keepers and self._kept_in(rule, unit):
                paths = by_rule.get(rule.id, {}).get("kept_paths", [])
                for other in keepers:
                    if other != unit and self._kept_in(rule, other) and paths:
                        shared.append({"unit_id": other, "member_paths": sorted(set(paths), key=lambda p: (p.casefold(), p)),
                                       "reason": rule.disposition["shared_reason"]})
        for share in entry.get("shared_with") or []:
            shared.append({"unit_id": share["unit_id"], **refs(share["groups"], f"shared_with {share['unit_id']}", kept_only=True),
                           "reason": share["reason"]})

        flags = []
        for flag in [*(f for f in self.spec.get("shared_flags") or [] if unit in f["units"]), *(entry.get("flags") or [])]:
            out = {"code": flag["code"], "certainty": flag["certainty"], "detail": flag["detail"]}
            if flag.get("with_unit"):
                out["with_unit"] = flag["with_unit"]
            out.update(refs(flag.get("groups"), f"flag {flag['code']}"))
            flags.append(out)
        questions = []
        for question in entry.get("needs_person") or []:
            out = {"code": question["code"], "question": question["question"]}
            out.update(refs(question.get("groups"), f"question {question['code']}"))
            questions.append(out)

        ts = handoff.get("technical_settings") if isinstance(handoff.get("technical_settings"), dict) else {}
        technical = {"ion_mode": entry["unit_technical"]["ion_mode"], "chromatography": entry["unit_technical"]["chromatography"]}
        if entry["unit_technical"].get("chromatography_detail"):
            technical["chromatography_detail"] = entry["unit_technical"]["chromatography_detail"]
        technical["acquisition_mode"] = entry["unit_technical"]["acquisition_mode"]
        technical["declared"] = {k: (None if ts.get(k) is None else str(ts.get(k)))
                                 for k in ("chromatography", "ion_mode", "acquisition_mode", "target_omics")}
        document: dict[str, Any] = {"schema": PAYLOAD_SCHEMA_ID}
        if handoff.get("repository"):
            document["repository"] = str(handoff["repository"])
        document.update({
            "accession": self.spec["accession"],
            "unit_id": unit,
            "source_subrecord_id": None if handoff.get("source_subrecord_id") is None else str(handoff["source_subrecord_id"]),
            "catalog_handoff_sha256": source.handoff_sha256,
            "catalog_row_count": len(source.rows),
            "standing": "model_inference",
            "phase": self.spec["phase"],
            "decided_by": {"agent": self.ctx.agent, "model": self.ctx.model, "skill": SKILL_NAME,
                           "skill_version": self.ctx.skill_version},
            "decided_at": self.ctx.decided_at,
            "purpose": self.ctx.purpose,
            "unit_technical": technical,
            "status": entry["status"],
            "confidence": entry["confidence"],
            "rationale": entry["rationale"],
            "rows": kept_rows,
            "added_members": added,
            "excluded_rows": excluded_rows,
            "excluded_members": excluded_members,
        })
        if children:
            document["children"] = children
        document.update({"shared_with": shared, "needs_person": questions, "flags": flags,
                         "evidence_sources": [{"source": e["source"], "detail": e["detail"]} for e in
                                              [*(self.spec.get("evidence_sources") or []), *(entry.get("evidence_sources") or [])]]})
        if entry.get("notes"):
            document["notes"] = entry["notes"]
        document["derivation"] = {"spec": self.spec_name, "spec_sha256": self.spec_sha256, "expander_version": EXPANDER_VERSION}
        return document


def comparable(document: Mapping[str, Any]) -> bytes:
    """A payload without what changes from one session to the next (when and by whom, and the spec it came from): two
    payloads that compare equal make the same selection, flags and questions with the same reasons."""
    value = {k: v for k, v in document.items() if k not in ("decided_at", "decided_by", "derivation")}
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def expand(spec_bytes: bytes, inputs: ExpansionInputs, ctx: ExpansionContext, *, spec_name: str,
           code_root: Path = CODE_ROOT) -> Expansion:
    """The payloads a spec expands to over the inputs, or the findings that refuse it."""
    try:
        spec = json.loads(spec_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as error:
        return Expansion({}, [Finding("X-SCHEMA", f"{spec_name} is not readable JSON: {error}")])
    if not isinstance(spec, dict):
        return Expansion({}, [Finding("X-SCHEMA", f"{spec_name} is not one JSON object")])
    expander = Expander(spec, inputs, ctx, spec_name, hashlib.sha256(spec_bytes).hexdigest(), code_root)
    try:
        payloads = expander.expand()
    except re.error as error:
        expander.find("X-SCHEMA", f"a regex does not compile: {error}")
        payloads = {}
    counts: dict[str, dict[str, int]] = {}
    for unit, document in getattr(expander, "documents", {}).items():
        counts[unit] = {"rows_kept": len(document["rows"]), "rows_excluded": len(document["excluded_rows"]),
                        "members_kept": len(document["added_members"]), "members_excluded": len(document["excluded_members"]),
                        "children": len(document.get("children") or []), "flags": len(document["flags"]),
                        "questions": len(document["needs_person"])}
    if expander.findings:
        return Expansion({}, expander.findings, counts)
    return Expansion(payloads, [], counts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True, help="the triage directory's inputs\\")
    parser.add_argument("--decided-at", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--skill-version", required=True)
    parser.add_argument("--agent", default="claude-code")
    parser.add_argument("--purpose")
    parser.add_argument("--out", type=Path, help="write <unit_id>.json here (never over an existing file)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    ctx = ExpansionContext(args.decided_at, args.model, args.skill_version, args.agent, args.purpose)
    result = expand(args.spec.read_bytes(), inputs_from_dir(args.inputs), ctx, spec_name=args.spec.name)
    report = {"expander_version": EXPANDER_VERSION, "ok": result.ok, "findings": [f.as_dict() for f in result.findings],
              "counts": result.counts,
              "payloads": {u: hashlib.sha256(b).hexdigest() for u, b in result.payloads.items()}}
    if result.ok and args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        for unit, data in result.payloads.items():
            with (args.out / f"{unit}.json").open("xb") as handle:
                handle.write(data)
    if args.json:
        print(json.dumps(report, indent=1, ensure_ascii=False))
    else:
        print(f"{args.spec.name}: {'expanded' if result.ok else 'refused'} ({len(result.payloads)} payload(s))")
        for f in result.findings[:FINDINGS_SHOWN]:
            print(f"  [{f.code}] {f.unit or '-'}: {f.message}")
    return 0 if result.ok else 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
