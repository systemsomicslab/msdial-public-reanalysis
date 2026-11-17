"""Synthetic decision records (msdial-llm-decision-record.v1) for the tests, as the wrapper writes them.

docs/llm-assisted-curation.md section 2.3: the wrapper that launched the agent writes <payload>.record.json beside each
payload, from the harness's transcript, after the session ends. scripts/validate-triage.py recomputes every sha256 it
names: the payload, each input under inputs\\, SKILL.md and the prompt template at repo_commit, the rendered prompt,
each tool result and the released transcript. These helpers write a record that holds, so a test can break one thing
at a time.

Two sources of the code a record names:
- Code.from_root(folder): a released-code folder (--code-root / validate(code_root=...)) holding the working tree's
  SKILL.md and prompt template, for tests that call the validator directly;
- Code.from_head(): git HEAD of this repository, for tests whose validator runs as the plan runs it (no --code-root).
  It raises unittest.SkipTest where git or HEAD's files are not available (a git archive copy of the tree).

Everything here is synthetic: public accession ids only, no person, no machine path in a record.
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
TEMPLATE_PATH = "prompts/unit-triage-task.v1.md"
CATALOG_SERVER = "msdial-repository-catalog"
CLEAN_CONTEXT = {
    "configuration": "clean", "working_directory": "<empty directory holding only the skill>", "context_files": [],
    "memory": "disabled", "hooks": [], "mcp_servers": [{"name": CATALOG_SERVER, "version": "0.6.1"}],
    "harness_flags": ["--setting-sources none"],
}


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
    """The skill and prompt template a record names, at its commit."""

    commit: str
    skill: bytes
    template: bytes
    root: Path | None = None  # the code root to hand the validator, or None for git

    @property
    def skill_version(self) -> str:
        match = re.search(r"Skill version: \*\*([0-9.]+)\*\*", self.skill.decode("utf-8"))
        assert match, "SKILL.md names no version"
        return match.group(1)

    @staticmethod
    def from_root(folder: Path, *, skill: bytes | None = None, template: bytes | None = None,
                  commit: str = "c" * 40) -> "Code":
        """A released-code folder holding the working tree's SKILL.md and template (or the bytes given)."""
        skill = skill if skill is not None else (ROOT / SKILL_PATH).read_bytes()
        template = template if template is not None else (ROOT / TEMPLATE_PATH).read_bytes()
        for relative, data in ((SKILL_PATH, skill), (TEMPLATE_PATH, template)):
            target = Path(folder) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return Code(commit, skill, template, Path(folder))

    @staticmethod
    def from_head() -> "Code":
        head = _git("rev-parse", "HEAD")
        commit = head.decode("ascii").strip() if head else ""
        skill = _git("show", f"{commit}:{SKILL_PATH}") if commit else None
        template = _git("show", f"{commit}:{TEMPLATE_PATH}") if commit else None
        if not commit or skill is None or template is None:
            raise unittest.SkipTest("git HEAD of this repository, with the skill and the prompt template, is not available")
        return Code(commit, skill, template, None)


def render(template: bytes, **values: str) -> bytes:
    text = template.decode("utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text.encode("utf-8")


def write_record(
    payload: Path,
    code: Code,
    *,
    inputs: Iterable[Mapping[str, Any]] = (),
    tool_calls: Iterable[tuple[str, Mapping[str, Any], Any]] = (),
    supersedes: str | None = None,
    context: Mapping[str, Any] | None = None,
    transcript: Mapping[str, Any] | None = None,
    **overrides: Any,
) -> Path:
    """Write <payload stem>.record.json beside `payload`, holding everything the validator recomputes.

    `inputs` are {role, path (relative to the payload's folder), ...}; sha256 and bytes are filled from the file unless
    given. `tool_calls` are (tool, arguments, result): each result is stored as inputs\\<unit_id>\\calls\\<seq>.json.
    `overrides` replace top-level fields of the record as written."""
    payload = Path(payload)
    folder = payload.parent
    data = payload.read_bytes()
    triage = json.loads(data.decode("utf-8"))
    unit = triage["unit_id"]
    unit_inputs = folder / "inputs" / unit
    unit_inputs.mkdir(parents=True, exist_ok=True)
    prompt = render(code.template, accession=triage["accession"], unit_ids=unit, input_dir="inputs",
                    output_dir=".", analysis_purpose="null")
    revision = re.match(r"^.+\.(r[0-9]+)$", payload.stem)
    prompt_name = f"prompt.{revision.group(1)}.md" if revision else "prompt.md"
    (unit_inputs / prompt_name).write_bytes(prompt)
    bound = []
    for item in inputs:
        entry = dict(item)
        content = (folder / entry["path"]).read_bytes()
        entry.setdefault("sha256", sha(content))
        entry.setdefault("bytes", len(content))
        bound.append(entry)
    calls = []
    for seq, (tool, arguments, result) in enumerate(tool_calls, start=1):
        calls_dir = unit_inputs / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)
        stored = json.dumps(result, indent=1).encode("utf-8")
        (calls_dir / f"{seq}.json").write_bytes(stored)
        calls.append({"seq": seq, "tool": tool, "arguments": dict(arguments), "status": "ok",
                      "result_path": f"inputs/{unit}/calls/{seq}.json", "result_sha256": sha(stored)})
    record = {
        "schema": "msdial-llm-decision-record.v1", "decision_id": sha(data), "kind": "unit_triage",
        "accession": triage["accession"], "unit_id": unit,
        "payload": {"path": payload.name, "schema": triage["schema"], "sha256": sha(data)},
        "supersedes": supersedes, "inputs": bound,
        "skill": {"name": "msdial-unit-triage", "version": code.skill_version, "path": SKILL_PATH,
                  "sha256": sha(code.skill), "repo_commit": code.commit},
        "prompt": {"template_path": TEMPLATE_PATH, "template_sha256": sha(code.template),
                   "rendered_path": f"inputs/{unit}/{prompt_name}", "rendered_sha256": sha(prompt)},
        "agent": {"harness": "synthetic-harness", "harness_version": "0.0.0", "model_id": "synthetic-model",
                  "sampling": "harness default, not exposed", "session_id": "synthetic-session",
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
