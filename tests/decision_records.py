"""Synthetic decision records (msdial-llm-decision-record.v1) for the tests, as the wrapper writes them.

docs/llm-assisted-curation.md section 2.3: the wrapper that launched the agent writes <payload>.record.json beside each
payload, from the harness's transcript, after the session ends. scripts/validate-triage.py recomputes every sha256 it
names: the payload, each input under inputs\\, SKILL.md, the prompt template and the wrapper at repo_commit, the
rendered prompt, each tool result and the released transcript, and compares the record's tool calls, model and session
with that transcript. These helpers write a record that holds, so a test can break one thing at a time.

Two sources of the code a record names:
- Code.from_root(folder): a released-code folder (--code-root / validate(code_root=...)) holding the working tree's
  SKILL.md, prompt templates and wrapper, for tests that call the validator directly;
- Code.from_head(): git HEAD of this repository, for tests whose validator runs as the plan runs it (no --code-root).
  It raises unittest.SkipTest where git or HEAD's files are not available (a git archive copy of the tree).

By default a record binds the unit's Catalog evidence (inputs/catalog.json, copied from the eval fixture of its
accession when the folder lacks one) and a released redacted transcript (transcripts/<payload stem>.redacted.jsonl)
that shows exactly its tool calls. Everything here is synthetic: public accession ids only, no person, no machine path
in a record.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = ".claude/skills/msdial-unit-triage/SKILL.md"
TEMPLATE_PATH = "prompts/unit-triage-task.v2.md"
REVISION_TEMPLATE_PATH = "prompts/unit-triage-revision.v2.md"
WRAPPER_PATH = "scripts/run-triage-agent.py"
SCHEMA_PATH = "schemas/msdial-unit-triage-spec.v1.json"
EXPANDER_PATH = "scripts/expand-triage-spec.py"
DIGEST_PATH = "scripts/triage-digest.py"
# What a session's code tree holds (run-triage-agent.py CODE_FILES), copied into a released-code folder as well.
CODE_FILES = (EXPANDER_PATH, DIGEST_PATH, "scripts/triage-spec-server.py", "scripts/validate-triage.py",
              "schemas/msdial-unit-triage.v1.json", "schemas/msdial-unit-triage.v2.json", SCHEMA_PATH)
FIXTURES = ROOT / "tests" / "fixtures" / "triage-eval"
CATALOG_SERVER = "msdial-repository-catalog"
SPEC_SERVER = "msdial-triage-spec"
CLEAN_CONTEXT = {
    "configuration": "clean", "working_directory": "<empty directory holding only the skill>", "context_files": [],
    "memory": "disabled", "hooks": [], "mcp_servers": [{"name": SPEC_SERVER, "version": "1.0.0"}],
    "harness_flags": ["--setting-sources none"],
}
MODEL = "synthetic-model"
SESSION = "synthetic-session"
AUTO = "auto"
CATALOG_ROLES = ("catalog_handoff", "catalog_samples", "catalog_unit")
# The skill version whose agent wrote the payload itself (msdial-unit-triage.v2 by hand). From 1.2.0 the agent writes a
# spec and a payload without a derivation is refused (T-SPEC), so tests of hand-written v2 payloads name this version:
# Code.from_root(..., skill_version=HAND_WRITTEN_SKILL_VERSION) and triage_eval.to_v2's default.
HAND_WRITTEN_SKILL_VERSION = "1.1.0"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(*args: str) -> bytes | None:
    try:
        done = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


@dataclass
class Code:
    """The skill, prompt templates and wrapper a record names, at its commit."""

    commit: str
    skill: bytes
    template: bytes
    root: Path | None = None  # the code root to hand the validator, or None for git
    wrapper: bytes = b""
    revision_template: bytes | None = None
    files: dict[str, bytes] | None = None  # CODE_FILES at the commit

    @property
    def skill_version(self) -> str:
        match = re.search(r"Skill version: \*\*([0-9.]+)\*\*", self.skill.decode("utf-8"))
        assert match, "SKILL.md names no version"
        return match.group(1)

    @staticmethod
    def from_root(folder: Path, *, skill: bytes | None = None, template: bytes | None = None,
                  commit: str = "c" * 40, skill_version: str | None = None) -> "Code":
        """A released-code folder holding the working tree's SKILL.md, templates and wrapper (or the bytes given). With
        `skill_version` the SKILL.md names that version instead (a synthetic skill of an earlier release)."""
        skill = skill if skill is not None else (ROOT / SKILL_PATH).read_bytes()
        if skill_version is not None:
            skill, count = re.subn(rb"Skill version: \*\*[0-9.]+\*\*", f"Skill version: **{skill_version}**".encode("utf-8"), skill)
            assert count == 1, "SKILL.md names no version"
        template = template if template is not None else (ROOT / TEMPLATE_PATH).read_bytes()
        wrapper = (ROOT / WRAPPER_PATH).read_bytes()
        revision = (ROOT / REVISION_TEMPLATE_PATH).read_bytes()
        files = {relative: (ROOT / relative).read_bytes() for relative in CODE_FILES}
        for relative, data in ((SKILL_PATH, skill), (TEMPLATE_PATH, template), (WRAPPER_PATH, wrapper),
                               (REVISION_TEMPLATE_PATH, revision), *files.items()):
            target = Path(folder) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return Code(commit, skill, template, Path(folder), wrapper, revision, files)

    @staticmethod
    def from_head() -> "Code":
        head = _git("rev-parse", "HEAD")
        commit = head.decode("ascii").strip() if head else ""
        skill = _git("show", f"{commit}:{SKILL_PATH}") if commit else None
        template = _git("show", f"{commit}:{TEMPLATE_PATH}") if commit else None
        wrapper = _git("show", f"{commit}:{WRAPPER_PATH}") if commit else None
        revision = _git("show", f"{commit}:{REVISION_TEMPLATE_PATH}") if commit else None
        files = {relative: _git("show", f"{commit}:{relative}") for relative in CODE_FILES} if commit else {}
        if not commit or skill is None or template is None or wrapper is None or any(v is None for v in files.values()):
            raise unittest.SkipTest("git HEAD of this repository, with the skill, the prompt template, the wrapper and the "
                                    "expander, is not available")
        return Code(commit, skill, template, None, wrapper, revision, files)

    def snapshot(self, module: Any) -> Any:
        """This code as scripts/run-triage-agent.py's CodeSnapshot (`module` is the loaded wrapper)."""
        return module.CodeSnapshot(self.commit, self.skill, self.template, self.root, wrapper=self.wrapper,
                                   wrapper_commit=self.commit, revision_template=self.revision_template,
                                   schema=(self.files or {}).get(SCHEMA_PATH) or (ROOT / SCHEMA_PATH).read_bytes(),
                                   files=dict(self.files or {r: (ROOT / r).read_bytes() for r in CODE_FILES}))


def render(template: bytes, **values: str) -> bytes:
    text = template.decode("utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text.encode("utf-8")


def stored_result(seq: int, tool: str, result: Any, is_error: bool = False) -> bytes:
    """A tool result as the wrapper stores it: {tool_use_id, tool, is_error, content}."""
    return json.dumps({"tool_use_id": f"toolu_{seq:03d}", "tool": tool, "is_error": is_error, "content": result},
                      indent=1).encode("utf-8")


def transcript_lines(calls: Iterable[tuple], *, model: str = MODEL, session: str = SESSION,
                     tools: Iterable[str] = ()) -> bytes:
    """A redacted stream-JSON transcript showing `calls` (tool, arguments, result[, is_error]) in order, as the wrapper
    releases it; `tools` are the tools its init message says the harness offered."""
    lines = [{"type": "system", "subtype": "init", "session_id": session, "model": model, "tools": list(tools),
              "mcp_servers": [], "plugins": []}]
    for seq, (tool, arguments, result, *error) in enumerate(calls, start=1):
        ident = f"toolu_{seq:03d}"
        lines.append({"type": "assistant", "message": {"model": model, "content": [
            {"type": "tool_use", "id": ident, "name": tool, "input": dict(arguments)}]}})
        lines.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": ident, "content": result, "is_error": bool(error and error[0])}]}})
    lines.append({"type": "result", "subtype": "success", "is_error": False, "session_id": session})
    return ("\n".join(json.dumps(line, separators=(",", ":")) for line in lines) + "\n").encode("utf-8")


def _catalog_input(folder: Path, accession: str) -> dict[str, Any] | None:
    target = Path(folder) / "inputs" / "catalog.json"
    if not target.is_file():
        source = FIXTURES / accession / "catalog.json"
        if not source.is_file():
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return {"role": "catalog_unit", "path": "inputs/catalog.json"}


def write_record(
    payload: Path,
    code: Code,
    *,
    inputs: Iterable[Mapping[str, Any]] = (),
    tool_calls: Iterable[tuple] = (),
    offered_tools: Iterable[str] = (),
    supersedes: str | None = None,
    context: Mapping[str, Any] | None = None,
    transcript: Mapping[str, Any] | None | str = AUTO,
    catalog: bool = True,
    **overrides: Any,
) -> Path:
    """Write <payload stem>.record.json beside `payload`, holding everything the validator recomputes.

    `inputs` are {role, path (relative to the payload's folder), ...}; sha256 and bytes are filled from the file unless
    given. Unless one of them binds Catalog evidence (or catalog=False), inputs/catalog.json is bound as catalog_unit.
    `tool_calls` are (tool, arguments, result[, is_error]): each result is stored as inputs\\<unit_id>\\calls\\<seq>.json. The
    transcript is released as transcripts/<payload stem>.redacted.jsonl unless `transcript` is given (None: none).
    `overrides` replace top-level fields of the record as written."""
    payload = Path(payload)
    folder = payload.parent
    data = payload.read_bytes()
    triage = json.loads(data.decode("utf-8"))
    unit = triage["unit_id"]
    unit_inputs = folder / "inputs" / unit
    unit_inputs.mkdir(parents=True, exist_ok=True)
    prompt = render(code.template, accession=triage["accession"], unit_ids=unit, input_dir="inputs",
                    output_dir=".", analysis_purpose="null", spec_name=f"{triage['accession']}.spec.json")
    revision = re.match(r"^.+\.(r[0-9]+)$", payload.stem)
    prompt_name = f"prompt.{revision.group(1)}.md" if revision else "prompt.md"
    (unit_inputs / prompt_name).write_bytes(prompt)
    given = [dict(item) for item in inputs]
    if catalog and not any(item.get("role") in CATALOG_ROLES for item in given):
        bound_catalog = _catalog_input(folder, triage["accession"])
        if bound_catalog is not None:
            given.insert(0, bound_catalog)
    bound = []
    for entry in given:
        content = (folder / entry["path"]).read_bytes()
        entry.setdefault("sha256", sha(content))
        entry.setdefault("bytes", len(content))
        bound.append(entry)
    calls = []
    listed = list(tool_calls)
    calls_name = f"calls.{revision.group(1)}" if revision else "calls"
    for seq, (tool, arguments, result, *error) in enumerate(listed, start=1):
        calls_dir = unit_inputs / calls_name
        calls_dir.mkdir(parents=True, exist_ok=True)
        is_error = bool(error and error[0])
        stored = stored_result(seq, tool, result, is_error)
        (calls_dir / f"{seq}.json").write_bytes(stored)
        calls.append({"seq": seq, "tool": tool, "arguments": dict(arguments), "status": "error" if is_error else "ok",
                      "result_path": f"inputs/{unit}/{calls_name}/{seq}.json", "result_sha256": sha(stored)})
    if transcript == AUTO:
        released = f"transcripts/{payload.stem}.redacted.jsonl"
        lines = transcript_lines(listed, tools=offered_tools)
        (folder / released).parent.mkdir(parents=True, exist_ok=True)
        (folder / released).write_bytes(lines)
        transcript = {"sha256": sha(lines), "released_path": released, "unredacted_sha256": "a" * 64,
                      "redaction": "synthetic"}
    record = {
        "schema": "msdial-llm-decision-record.v1", "decision_id": sha(data), "kind": "unit_triage",
        "accession": triage["accession"], "unit_id": unit,
        "payload": {"path": payload.name, "schema": triage["schema"], "sha256": sha(data)},
        "supersedes": supersedes, "inputs": bound,
        "skill": {"name": "msdial-unit-triage", "version": code.skill_version, "path": SKILL_PATH,
                  "sha256": sha(code.skill), "repo_commit": code.commit},
        "wrapper": {"path": WRAPPER_PATH, "sha256": sha(code.wrapper), "repo_commit": code.commit},
        "prompt": {"template_path": TEMPLATE_PATH, "template_sha256": sha(code.template),
                   "rendered_path": f"inputs/{unit}/{prompt_name}", "rendered_sha256": sha(prompt)},
        "agent": {"harness": "synthetic-harness", "harness_version": "0.0.0", "model_id": MODEL,
                  "sampling": "harness default, not exposed", "session_id": SESSION,
                  "context": dict(context if context is not None else CLEAN_CONTEXT)},
        "started_at": "2026-11-16T09:00:00+09:00", "finished_at": "2026-11-16T09:10:00+09:00",
        "tool_calls": calls, "transcript": dict(transcript) if transcript is not None else None,
        "rationale": "Synthetic decision record for a test.",
    }
    record.update(overrides)
    path = payload.with_name(payload.name[: -len(".json")] + ".record.json")
    path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    return path


def copy_listing(source: Path, folder: Path, name: str = "archive-members.tsv") -> dict[str, Any]:
    """Copy an archive listing under <folder>/inputs/ and return its archive_listing input."""
    target = Path(folder) / "inputs" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return {"role": "archive_listing", "path": f"inputs/{name}"}


def _module(name: str, path: Path) -> Any:
    import importlib.util  # noqa: PLC0415
    import sys  # noqa: PLC0415

    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FINISHED_AT = "2026-11-16T09:10:00+09:00"  # write_record's finished_at: the expansion's decided_at
# What the expander and the digest script read beside themselves (validate-triage.py SPEC_CODE_TREE), bound by sha256.
SPEC_CODE_TREE = ("scripts/validate-triage.py", "schemas/msdial-unit-triage.v1.json", "schemas/msdial-unit-triage.v2.json",
                  SCHEMA_PATH)


def tree(files: Mapping[str, bytes]) -> dict[str, str]:
    """A derivation code_file's tree: the sha256 of each SPEC_CODE_TREE file at the commit."""
    return {relative: sha(files[relative]) for relative in SPEC_CODE_TREE}


def spec_triage(folder: Path, accession: str, code: Code, *, spec_bytes: bytes | None = None, digest: bool = True,
                record: bool = True, **record_options: Any) -> Path:
    """A triage directory as the wrapper commits it under skill 1.2.0: the eval accession's inputs (handoffs, samples,
    listing, inputs/digest.md), <accession>.spec.json (the eval spec unless `spec_bytes`), the payloads the expander
    makes of it, and records whose derivation binds the spec, the expander and the digest script of `code` (its root,
    or this tree) with the context the record states (decided_at its finished_at, model its model_id)."""
    import triage_eval as te  # noqa: PLC0415

    folder = Path(folder)
    inputs = te.write_inputs(accession, folder)
    root = code.root or ROOT
    expander = _module(f"expander_for_records_{sha(str(root).encode())[:12]}", root / EXPANDER_PATH)
    digest_module = _module(f"digest_for_records_{sha(str(root).encode())[:12]}", root / DIGEST_PATH)
    spec_bytes = spec_bytes if spec_bytes is not None else te.spec_path(accession).read_bytes()
    spec_name = f"{accession}.spec.json"
    (folder / spec_name).write_bytes(spec_bytes)
    digest_bytes = digest_module.build_digest(inputs)
    if digest:
        (inputs / "digest.md").write_bytes(digest_bytes)
    context = {"decided_at": FINISHED_AT, "model": MODEL, "agent": "claude-code", "skill_version": code.skill_version,
               "purpose": None}
    ctx = expander.ExpansionContext(**context)
    result = expander.expand(spec_bytes, expander.inputs_from_dir(inputs), ctx, spec_name=spec_name)
    assert result.ok, [f.as_dict() for f in result.findings]
    for unit, data in result.payloads.items():
        (folder / f"{unit}.json").write_bytes(data)
    files = code.files or {r: (ROOT / r).read_bytes() for r in CODE_FILES}
    derivation: dict[str, Any] = {
        "spec": {"path": spec_name, "schema": "msdial-unit-triage-spec.v1", "sha256": sha(spec_bytes), "bytes": len(spec_bytes)},
        "expander": {"path": EXPANDER_PATH, "version": expander.EXPANDER_VERSION, "sha256": sha(files[EXPANDER_PATH]),
                     "repo_commit": code.commit, "tree": tree(files)},
        "context": context,
    }
    if digest:
        derivation["digest"] = {"path": "inputs/digest.md", "sha256": sha(digest_bytes),
                                "script": {"path": DIGEST_PATH, "version": digest_module.DIGEST_VERSION,
                                           "sha256": sha(files[DIGEST_PATH]), "repo_commit": code.commit,
                                           "tree": tree(files)}}
    if record:
        units = sorted(result.payloads)
        listing = next(p.name for p in inputs.glob("*.tsv"))
        for unit in units:
            bound = [{"role": "catalog_handoff", "path": f"inputs/{unit}/handoff.json"},
                     {"role": "catalog_samples", "path": f"inputs/{unit}/samples.json"},
                     {"role": "archive_listing", "path": f"inputs/{listing}"}]
            if digest:
                bound.append({"role": "triage_digest", "path": "inputs/digest.md"})
            for other in units:
                if other != unit:
                    bound += [{"role": "other", "path": f"inputs/{other}/handoff.json"},
                              {"role": "other", "path": f"inputs/{other}/samples.json"}]
            options = dict(record_options)
            options.setdefault("derivation", derivation)
            write_record(folder / f"{unit}.json", code, inputs=bound, **options)
    return folder


def revise_spec(folder: Path, accession: str, code: Code, spec_bytes: bytes, units: Iterable[str]) -> Path:
    """A revision as the wrapper commits it, on a spec_triage directory: <accession>.spec.r<N>.json, and for each unit
    named <unit>.r<M>.json (the unit's next revision) with a record that supersedes its current payload and binds the
    revision spec. The expansion is not checked against the units the revision does not name (that is X-SCOPE, which
    the wrapper runs and validate-triage.py re-checks)."""
    import triage_eval as te  # noqa: PLC0415,F401

    folder = Path(folder)
    root = code.root or ROOT
    expander = _module(f"expander_for_records_{sha(str(root).encode())[:12]}", root / EXPANDER_PATH)
    numbers = [int(m.group(1) or 0) for p in folder.glob(f"{accession}.spec*.json")
               for m in [re.match(rf"^{re.escape(accession)}\.spec(?:\.r([0-9]+))?\.json$", p.name)] if m]
    spec_name = f"{accession}.spec.r{max(numbers, default=0) + 1}.json"
    (folder / spec_name).write_bytes(spec_bytes)
    context = {"decided_at": FINISHED_AT, "model": MODEL, "agent": "claude-code", "skill_version": code.skill_version,
               "purpose": None}
    result = expander.expand(spec_bytes, expander.inputs_from_dir(folder / "inputs"), expander.ExpansionContext(**context),
                             spec_name=spec_name)
    assert result.ok, [f.as_dict() for f in result.findings]
    for unit in units:
        current = sorted(folder.glob(f"{unit}.r*.json"))
        current = [p for p in current if not p.name.endswith(".record.json")]
        base = folder / f"{unit}.json"
        previous = max(current, key=lambda p: int(p.stem.rsplit(".r", 1)[1])) if current else base
        number = int(previous.stem.rsplit(".r", 1)[1]) + 1 if previous != base else 1
        record = json.loads(previous.with_name(previous.name[: -len(".json")] + ".record.json").read_text(encoding="utf-8"))
        target = folder / f"{unit}.r{number}.json"
        target.write_bytes(result.payloads[unit])
        derivation = dict(record["derivation"])
        derivation["spec"] = {"path": spec_name, "schema": "msdial-unit-triage-spec.v1", "sha256": sha(spec_bytes),
                              "bytes": len(spec_bytes)}
        derivation["context"] = context
        inputs = [{"role": item["role"], "path": item["path"]} for item in record["inputs"]]
        write_record(target, code, inputs=inputs, supersedes=sha(previous.read_bytes()), derivation=derivation,
                     catalog=False)
    return folder
