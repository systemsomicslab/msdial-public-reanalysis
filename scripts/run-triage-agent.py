"""Run the msdial-unit-triage agent on public-repository accessions and write each payload's decision record.

docs/llm-assisted-curation.md sections 2.1 to 2.3 (the "Context" paragraph and the envelope). For each accession:

1. PREPARE (read only, before download). Every Catalog analysis unit of the accession gets a Catalog handoff,
   made with msdial_catalog_reanalysis_handoff without a Class proposal, copied into
   <campaign dir>\\triage\\<accession>\\inputs\\<unit_id>\\ (handoff.json with its samples.json, files.json and
   inputs.json, the sidecar paths in the copy pointing at the copies, which Interactive reads later; the session
   reads the sidecars beside the copy, never those paths). Every zip archive the handoffs name is listed
   from its central directory (Interactive's remote_archive.list_remote_zip, the code behind
   msdial_list_remote_archive; never a download) into inputs\\<archive>.members.tsv. For a Metabolomics Workbench
   unit, the analysis's mwTab text is fetched into inputs\\<analysis id>.mwtab.txt. A text that holds an absolute
   path is made portable before the session (release_safe_text), its input naming the original's sha256. A listing
   or an mwTab text that cannot be had is left out and said so; the validator then holds.
   The digest (scripts/triage-digest.py at HEAD) is then written to inputs\\digest.md: the source facts of every input,
   compact, with no inference; the session reads it first and the raw inputs where it cuts something.
2. RENDER prompts/unit-triage-task.v2.md (at git HEAD of this repository) into inputs\\<unit_id>\\prompt.md, with the
   input and output directories relative to the session's working directory and the spec file to write.
3. LAUNCH the Claude Code CLI headless in a clean configuration (CLEAN_FLAGS below): a fresh working directory
   outside every code checkout holding only the skill (SKILL.md at HEAD) and the spec schema it names
   (schemas/msdial-unit-triage-spec.v1.json at HEAD), the inputs and an empty output folder as
   sibling directories added with --add-dir, no settings files (--restricted), CLAUDE.md and auto memory disabled,
   hooks disabled, only the MCP servers the skill's allowed-tools name (--strict-mcp-config: the spec check server,
   scripts/triage-spec-server.py at HEAD, written to <session>\\code\\ with the expander and the validator), the
   built-in tools limited to the skill's, every other tool of those servers removed (--disallowedTools, from the
   server's own tools/list), and the transcript taken as stream JSON. --effort is passed through when given. The
   session's directory is <campaign dir>\\triage-sessions\\<accession>\\<stamp>\\ (code, work, inputs, out, harness). The
   prompt is written on its own thread, so --timeout runs from the launch. One run holds <triage dir>\\.triage.lock
   throughout.
4. ACCEPT, whole or not at all. A session that did not end in success, timed out, wrote anything but the spec it was
   asked for into its output folder (a payload, a decision record or a review log above all: code writes the payloads,
   and only the wrapper writes the records and the log), wrote outside it, read outside its working directory, inputs
   and output folder, wrote a spec holding an absolute path, an e-mail address, a secret or the operator's name, or
   changed its inputs or working directory has nothing accepted. The spec is then EXPANDED (scripts/
   expand-triage-spec.py at HEAD) into one msdial-unit-triage.v2 payload per unit, decided_at the session's end and the
   model the transcript names; any X-* finding refuses the session the same way (harness\\expansion.json lists them).
5. RECORD <unit_id>.record.json (msdial-llm-decision-record.v1) from the harness transcript, not from anything the
   model says about itself. The transcript is scrubbed (secrets, e-mail addresses, the operator's names and handles
   removed, paths portable), and every tool call in order with its arguments and its result (stored as
   inputs\\<unit_id>\\calls\\<seq>.json) is read from the scrubbed bytes; with every input offered, the skill and
   template at the commit, the wrapper (wrapper.repo_commit null when this file is not HEAD's), the rendered prompt,
   the harness version, the model id, the flags and the configuration. What the configuration achieved is checked
   from the transcript's init message (tools, MCP servers and their status, plugins, skills, working directory), the
   debug log (hooks registered, memory off) and the ancestors of the session directory (context files); anything
   short of clean, a missing debug log included, is written as "not clean: <why>", which the validator holds
   (T-CONTEXT). For a clean run the scrubbed transcript, once check-release-redaction.py finds nothing in it, is
   released as transcripts\\<accession>-<stamp>.redacted.jsonl and named in the record; the unscrubbed one stays in
   the session's harness folder (transcript.unredacted_sha256). The tool results, the transcript, the records and the
   payloads are committed all or nothing, never over an existing file. Each record binds the spec, the expander and the
   digest script by sha256 and commit, and the expansion context (derivation), which validate-triage.py re-runs (T-SPEC,
   T-DIGEST).
6. VALIDATE: scripts/validate-triage.py --emit-selection on the accession, with inputs\\ as the Catalog and
   inputs\\*.tsv as the listings: its validator_verdict entries enter the review log. Its report is kept in the
   session's harness folder; a validator that cannot run is an error verdict.

REVISE (--revise <unit_id>=<seq>, one accession): the unit's current payload is revised because of review-log entry
<seq> (a person_answer or a validator_verdict about it or a payload it supersedes; one entry causes one revision). The
session gets the inputs the current record binds, the current spec, the current payload and the cause line, under
prompts/unit-triage-revision.v2.md, and writes <accession>.spec.r<N>.json, a whole spec. Its expansion may change only
the units named (X-SCOPE, against every unit's current payload); each named unit's payload is <unit_id>.r<M>.json with a
record whose supersedes is the current decision, and the wrapper appends the review log's revision entry. A payload that
was not expanded from a spec (a skill 1.1.0 payload) is not revised this way: its accession is triaged again in a new
campaign directory.

The wrapper never writes a person's answer (scripts/record-person-answer.py does, for a named person), never calls a
tool with confirmed=true, never saves a Class, never downloads raw data and never runs MS-DIAL.

    python -I scripts/run-triage-agent.py --campaign-dir <dir> (--accession ST002283 ... | --accessions-file <file>)
        [--revise <unit_id>=<seq>]... [--dry-run] [--max-parallel 1] [--model <id>] [--effort <level>] [--timeout 3600]
        [--analysis-purpose <text>] [--claude <executable>] [--mcp-config <json>] [--catalog-database <sqlite>]
        [--interactive-root <dir>] [--session-root <dir>] [--redact-term <text>]...

Exit status: 0 every accession prepared (dry run) or triaged with a validator pass; 1 a validator hold; 2 a validator
error; 3 a session or a preparation that failed or was refused.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import unquote, urlsplit

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from campaign import curated  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("validate_triage_for_wrapper", SCRIPTS / "validate-triage.py")
assert _SPEC and _SPEC.loader
validator = importlib.util.module_from_spec(_SPEC)
sys.modules["validate_triage_for_wrapper"] = validator
_SPEC.loader.exec_module(validator)

SKILL_NAME = "msdial-unit-triage"
SKILL_PATH = ".claude/skills/msdial-unit-triage/SKILL.md"
# The schema the session's working directory holds beside the skill: the spec the agent writes. The payload schema
# (msdial-unit-triage.v2) is code's, not the agent's.
SCHEMA_PATH = "schemas/msdial-unit-triage-spec.v1.json"
PAYLOAD_SCHEMA_PATH = "schemas/msdial-unit-triage.v2.json"
SPEC_SCHEMA_ID = "msdial-unit-triage-spec.v1"
TEMPLATE_PATH = "prompts/unit-triage-task.v2.md"
REVISION_TEMPLATE_PATH = "prompts/unit-triage-revision.v2.md"
WRAPPER_PATH = "scripts/run-triage-agent.py"
EXPANDER_PATH = "scripts/expand-triage-spec.py"
DIGEST_PATH = "scripts/triage-digest.py"
SPEC_SERVER_PATH = "scripts/triage-spec-server.py"
SPEC_SERVER = "msdial-triage-spec"
# The code a session's spec check and expansion run, at HEAD, written to <session>\code\ (the expander and the digest
# script read the validator and the schemas beside them).
CODE_FILES = (EXPANDER_PATH, DIGEST_PATH, SPEC_SERVER_PATH, "scripts/validate-triage.py", "schemas/msdial-unit-triage.v1.json",
              PAYLOAD_SCHEMA_PATH, SCHEMA_PATH)
SPEC_KEY = "<spec>"
DIGEST_INPUT = "inputs/digest.md"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
TRANSCRIPTS = "transcripts"
LOCK_NAME = ".triage.lock"
VALIDATOR_PATH = SCRIPTS / "validate-triage.py"
HARNESS = "claude-code"
RECORD_SCHEMA = "msdial-llm-decision-record.v1"
SAMPLING = "harness default, not exposed"
WORKING_DIRECTORY = ("a fresh directory outside every code checkout holding only the skill "
                     f"({SKILL_PATH}) and the spec schema it names ({SCHEMA_PATH}); the inputs (../inputs) and the output folder (../out) are sibling directories "
                     "added with --add-dir")
MW_REPOSITORY = "metabolomics_workbench"
MWTAB_URL = "https://www.metabolomicsworkbench.org/rest/study/analysis_id/{analysis}/mwtab/txt"
USER_AGENT = "msdial-public-reanalysis-triage-wrapper/1"
LISTING_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 3600.0

# Settings passed with --settings: no hook runs, whatever a settings file the harness still reads would add.
SESSION_SETTINGS = {"disableAllHooks": True}
# Environment set for the session: no CLAUDE.md of any scope, no auto memory, no claude.ai connectors.
SESSION_ENV = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
}
# Inherited variables a launching Claude Code session sets for itself; the triage session must not take them over.
# Authentication and provider selection (CLAUDE_CODE_OAUTH_TOKEN, CLAUDE_CODE_USE_*) and the Git Bash path stay.
_ENV_KEEP = re.compile(r"^CLAUDE_CODE_(OAUTH_TOKEN|USE_[A-Z]+|GIT_BASH_PATH)$")
_ENV_DROP = re.compile(r"^(CLAUDECODE|CLAUDE_CODE_.+|CLAUDE_AGENT_SDK_.+)$")

PAYLOAD_NAME = re.compile(r"^(?P<unit>[A-Za-z0-9][A-Za-z0-9._-]*)\.json$")
CONTEXT_FILE_NAMES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude/CLAUDE.md", ".claude/settings.json",
                      ".claude/settings.local.json", ".mcp.json")


class WrapperError(RuntimeError):
    """A preparation or launch that cannot go on; the accession is reported failed and nothing is accepted."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def long_path(path: Path | str) -> Path:
    """The resolved long form of a path (Windows 8.3 short names expanded): the CLI compares the paths a tool names with
    its working directories as text, so a short-name directory refuses every long-name read (found 2026-11-18)."""
    return Path(os.path.realpath(str(path)))


# ---------------------------------------------------------------------------------------------- the code at HEAD

@dataclass
class CodeSnapshot:
    """The skill and the prompt templates a session runs with, at one commit of this repository, and the wrapper that
    writes the records. `root` is a released code folder the validator reads in place of git (validate-triage.py
    --code-root), or None for git. `wrapper_commit` is the commit only when the running wrapper is the one it holds
    (line endings aside); an edited working tree records null, and the validator holds such a record."""

    commit: str
    skill: bytes
    template: bytes
    root: Path | None = None
    wrapper: bytes = b""
    wrapper_commit: str | None = None
    revision_template: bytes | None = None
    schema: bytes = b""
    files: dict[str, bytes] = field(default_factory=dict)  # CODE_FILES at the commit

    @property
    def wrapper_identity(self) -> dict[str, Any]:
        return {"path": WRAPPER_PATH, "sha256": sha256_bytes(self.wrapper), "repo_commit": self.wrapper_commit}

    @property
    def skill_version(self) -> str:
        version = validator.skill_version(self.skill.decode("utf-8", "replace"))
        if not version:
            raise WrapperError("the skill at the commit names no version (Skill version: **x.y.z**)")
        return version

    @property
    def allowed_tools(self) -> list[str]:
        tools = validator.skill_allowed_tools(self.skill.decode("utf-8", "replace"))
        if not tools:
            raise WrapperError("the skill at the commit lists no allowed-tools; a session cannot be restricted to them")
        return tools

    def materialise(self, folder: Path) -> Path:
        """CODE_FILES written under `folder` (a session's code\\), the tree the expander, the digest script and the spec
        check server run from."""
        missing = [path for path in CODE_FILES if path not in self.files]
        if missing:
            raise WrapperError(f"the code at the commit lacks {', '.join(missing)}")
        for relative in CODE_FILES:
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.files[relative])
        return folder

    def identity(self, path: str, version: str) -> dict[str, Any]:
        """A CODE_FILES script as a record binds it: path, version, sha256 at the commit, the commit, and the sha256 of
        each file it reads beside itself (validate-triage.py SPEC_CODE_TREE), which the validator checks before it re-runs
        the script."""
        return {"path": path, "version": version, "sha256": sha256_bytes(self.files[path]), "repo_commit": self.commit,
                "tree": {relative: sha256_bytes(self.files[relative]) for relative in validator.SPEC_CODE_TREE}}


def load_module(folder: Path, relative: str) -> Any:
    """A script of a materialised code tree, loaded under a name of its own."""
    name = f"triage_session_{sha256_bytes((str(folder) + relative).encode('utf-8'))[:16]}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, folder / relative)
    if spec is None or spec.loader is None:
        raise WrapperError(f"{relative} cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def code_from_head(repo: Path = REPO_ROOT) -> CodeSnapshot:
    """SKILL.md and the template exactly as git HEAD holds them (the bytes the record's sha256 names)."""
    def git(*args: str) -> bytes:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=60, check=False)
        if done.returncode != 0:
            raise WrapperError(f"git {' '.join(args)} failed: {done.stderr.decode('utf-8', 'replace').strip()[:200]}")
        return done.stdout

    try:
        commit = git("rev-parse", "HEAD").decode("ascii").strip()
        held = git("show", f"{commit}:{WRAPPER_PATH}")
        running = Path(__file__).read_bytes()
        same = running.replace(b"\r\n", b"\n") == held.replace(b"\r\n", b"\n")
        return CodeSnapshot(commit, git("show", f"{commit}:{SKILL_PATH}"), git("show", f"{commit}:{TEMPLATE_PATH}"),
                            wrapper=held if same else running, wrapper_commit=commit if same else None,
                            revision_template=git("show", f"{commit}:{REVISION_TEMPLATE_PATH}"),
                            schema=git("show", f"{commit}:{SCHEMA_PATH}"),
                            files={path: git("show", f"{commit}:{path}") for path in CODE_FILES})
    except (OSError, subprocess.SubprocessError) as error:
        raise WrapperError(f"git could not be run: {error}") from error


def render_prompt(template: bytes, **values: str) -> bytes:
    text = template.decode("utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    left = sorted(set(re.findall(r"\{\{([a-z0-9_]+)\}\}", text)))
    if left:
        raise WrapperError(f"the prompt template has placeholders the wrapper does not fill: {', '.join(left)}")
    return text.encode("utf-8")


# ------------------------------------------------------------------------------------------ redaction and paths

_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|cookie|credential|private[_-]?key)")
_SECRET_VALUES = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SEP = r"(?:\\\\|\\|/)"
_DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:" + _SEP + r"(?:[^\\/\r\n\t\"'<>|*?:,;]*?" + _SEP + r")*"
                         r"[^\\/\s\"'<>|*?:,;)\]}]*")
_UNC_PATH = re.compile(r"(?<![A-Za-z0-9:])(?:\\\\\\\\|\\\\)[A-Za-z0-9._$-]+(?:" + _SEP + r"[^\\/\s\"'<>|*?:,;)\]}]+)+")
# A POSIX home or user profile path (/home/<name>/..., /Users/<name>/...), which the release check refuses as well.
_PROFILE_PATH = re.compile(r"(?<![A-Za-z0-9._~-])/(?:home|Users)/[^/\s\"'<>|*?:,;)\]}]+(?:/[^/\s\"'<>|*?:,;)\]}]*)*")


def _term_kept(term: str) -> bool:
    """A name part long enough to mean a person: three letters in ASCII; any length in a script without spaces between
    words (most Japanese and Chinese family and given names are one or two characters)."""
    return len(term) >= 3 or (len(term) >= 1 and any(ord(char) > 127 for char in term))


def _term_pattern(term: str) -> re.Pattern[str]:
    """A name as a word: not inside a longer ASCII word (Roebuck keeps Roe), but next to digits, '_' or '-' it still
    counts (roe2026, workshop_jroe). A non-ASCII name is matched wherever it appears."""
    if any(ord(char) > 127 for char in term):
        return re.compile(re.escape(term), re.IGNORECASE)
    return re.compile(r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])", re.IGNORECASE)


@dataclass
class Scrubber:
    """What leaves the session's own machine in a record or a stored tool result: secrets, e-mail addresses and the
    operator's names removed, and every absolute path made portable (the session's folders relative to its working
    directory, anything else <external>/<file name>). Pure over its arguments."""

    roots: list[tuple[str, str]]  # (absolute prefix, its replacement), longest first
    terms: list[str] = field(default_factory=list)

    @staticmethod
    def for_session(session: Path, terms: Iterable[str] = ()) -> "Scrubber":
        roots: list[tuple[str, str]] = []
        for given in {str(session), str(long_path(session))}:
            base = given.rstrip("\\/")
            for sub, relative in (("work", "."), ("inputs", "../inputs"), ("out", "../out"), ("harness", "../harness"),
                                  ("", "..")):
                prefix = base + ("\\" + sub if sub else "")
                for variant in {prefix, prefix.replace("\\", "/"), prefix.replace("\\", "\\\\")}:
                    roots.append((variant, relative))
        roots.sort(key=lambda item: -len(item[0]))
        cleaned = sorted({t for t in (str(x).strip() for x in terms) if _term_kept(t)}, key=lambda t: -len(t))
        return Scrubber(roots, cleaned)

    def paths(self, value: str) -> str:
        """Only the path rule: the session's folders relative, any other absolute path <external>/<file name>."""
        out = value
        for prefix, relative in self.roots:
            out = re.sub(re.escape(prefix) + r"(?=" + _SEP + r"|\b|$)", lambda _m, r=relative: r, out,
                         flags=re.IGNORECASE)
        out = _DRIVE_PATH.sub(lambda m: "<external>/" + _basename(m.group(0)), out)
        out = _UNC_PATH.sub(lambda m: "<external>/" + _basename(m.group(0)), out)
        return _PROFILE_PATH.sub(lambda m: "<external>/" + _basename(m.group(0)), out)

    def found(self, value: str) -> list[str]:
        """What `value` holds that a released file must not: an absolute path, an e-mail address, a secret, an operator's
        name. The kinds only, never the matched text."""
        kinds = []
        if curated._holds_absolute_path(value.encode("utf-8")):  # noqa: SLF001 - the release's own path rules
            kinds.append("an absolute path")
        if _EMAIL.search(value):
            kinds.append("an e-mail address")
        if any(pattern.search(value) for pattern in _SECRET_VALUES):
            kinds.append("a secret")
        if any(_term_pattern(term).search(value) for term in self.terms):
            kinds.append("an operator's name")
        return kinds

    def text(self, value: str) -> str:
        out = value
        for pattern in _SECRET_VALUES:
            out = pattern.sub("<redacted>", out)
        out = self.paths(out)
        out = _EMAIL.sub("<redacted-email>", out)
        for term in self.terms:
            out = _term_pattern(term).sub("<redacted>", out)
        return out

    def value(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, str):
            if key is not None and _SECRET_KEY.search(key) and value:
                return "<redacted>"
            return self.text(value)
        if isinstance(value, Mapping):
            return {str(k): self.value(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value


def _basename(path: str) -> str:
    parts = [part for part in re.split(r"\\\\|\\|/", path.rstrip("\\/")) if part]
    return parts[-1] if parts else "path"


def operator_terms(extra: Iterable[str] = ()) -> list[str]:
    """The operator's identifiers a record must not hold: the account name, the home folder's name, git's user.name and
    user.email of this repository, each with its words, and any --redact-term."""
    found: set[str] = set()

    def add(text: str | None) -> None:
        text = str(text or "").strip()
        if not text:
            return
        found.add(text)
        # An e-mail address is removed whole (and by its local part); its domain names an institution, not a person.
        local = text.split("@", 1)[0] if "@" in text else text
        parts = [part for part in re.split(r"[\s\\/._-]+", local) if part]
        found.update(part for part in parts if _term_kept(part))
        # The handles people make of a name: initial and surname (jroe), surname and initial, both run together, in
        # either order, since a name may be written family name first.
        words = [part for part in parts if part.isascii() and part.isalpha() and len(part) >= 2]
        if len(words) >= 2:
            for first, last in ((words[0], words[-1]), (words[-1], words[0])):
                for handle in (first[0] + last, last + first[0], first + last):
                    if len(handle) >= 4:
                        found.add(handle)

    for name in ("USERNAME", "USER", "LOGNAME"):
        add(os.environ.get(name))
    add(Path.home().name)
    for key in ("user.name", "user.email"):
        try:
            done = subprocess.run(["git", "-C", str(REPO_ROOT), "config", key], capture_output=True, timeout=30,
                                  check=False)
            if done.returncode == 0:
                add(done.stdout.decode("utf-8", "replace"))
        except (OSError, subprocess.SubprocessError):
            pass
    for item in extra:
        add(item)
    # Words too common to mean a person (a host name prefix such as DESKTOP, the word "users").
    return sorted(term for term in found if term.casefold() not in {"desktop", "users", "user", "admin", "com", "org"})


# -------------------------------------------------------------------------------------------- the Catalog side

class LocalCatalog:
    """The Catalog read in-process: units by accession on a read-only connection, handoffs with
    msdial_catalog_reanalysis_handoff (without a Class proposal; it writes only its handoff files)."""

    def __init__(self, database: Path) -> None:
        from campaign import ports  # noqa: PLC0415
        from msdial_repository_catalog import mcp_server  # noqa: PLC0415

        self.database = Path(database)
        self._ports = ports
        self._tools = mcp_server
        self.version = _distribution_version("msdial-repository-catalog")

    def units(self, accession: str) -> list[dict[str, Any]]:
        catalog = self._ports.read_only_catalog(self.database)
        try:
            found = catalog.search(accessions=[accession], limit=500)
        finally:
            catalog.close()
        return [{"unit_id": str(item["unit_id"]), "repository": str(item.get("repository") or ""),
                 "source_subrecord_id": str(item.get("source_subrecord_id") or "")}
                for item in found if str(item.get("accession") or "").casefold() == accession.casefold()]

    def handoff(self, unit_id: str) -> dict[str, Any]:
        return self._tools.msdial_catalog_reanalysis_handoff(unit_id=unit_id, database=str(self.database))


def _distribution_version(name: str) -> str | None:
    try:
        import importlib.metadata as metadata  # noqa: PLC0415

        return metadata.version(name)
    except Exception:  # noqa: BLE001
        return None


class InteractiveLister:
    """Interactive's remote zip listing (msdial_app.remote_archive.list_remote_zip, the code behind
    msdial_list_remote_archive): the central directory over HTTP Range requests, never the archive."""

    def __init__(self, root: Path | None) -> None:
        self.root = Path(root) if root else None

    def __call__(self, url: str, expected_size: int | None) -> dict[str, Any]:
        if self.root is not None and str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))
        from msdial_app.remote_archive import RemoteArchiveError, list_remote_zip  # noqa: PLC0415

        try:
            return list_remote_zip(url, max_bytes=LISTING_MAX_BYTES, expected_size_bytes=expected_size)
        except RemoteArchiveError as error:
            return error.as_result()


def fetch_text(url: str, timeout: float = 60.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - a fixed https URL
        if getattr(response, "status", 200) != 200:
            raise OSError(f"HTTP {response.status}")
        return response.read()


# ------------------------------------------------------------------------------------------------ preparation

@dataclass
class Offered:
    """One file offered to the session, under the triage directory's inputs\\ (path relative to the triage dir)."""

    path: str
    kind: str  # handoff, samples, files, analysis_inputs, listing, mwtab, digest; for a revision bound, prior_decision,
    # review_entry, prior_spec
    unit: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass
class Preparation:
    accession: str
    units: list[dict[str, Any]]
    offered: list[Offered]
    notes: list[str]


_SIDECARS = (("sample_table_path", "samples.json", "samples"), ("file_manifest_path", "files.json", "files"),
             ("analysis_input_manifest_path", "inputs.json", "analysis_inputs"))


def prepare_inputs(accession: str, staging: Path, final_inputs: Path, catalog: Any, lister: Callable | None,
                   fetcher: Callable[[str], bytes] | None) -> Preparation:
    """Write the accession's inputs into `staging` (later moved to `final_inputs`, the path the copies' sidecar paths
    name). Nothing here downloads raw data or writes to the Catalog beyond its handoff files."""
    notes: list[str] = []
    units = catalog.units(accession)
    if not units:
        raise WrapperError(f"the Catalog holds no analysis unit of {accession}")
    offered: list[Offered] = []
    archives: dict[str, int | None] = {}
    for unit in units:
        unit_id = unit["unit_id"]
        if not PAYLOAD_NAME.match(unit_id + ".json"):
            raise WrapperError(f"unit id {unit_id!r} cannot name a file")
        response = catalog.handoff(unit_id)
        original_path = Path(str(response.get("handoff_path") or ""))
        try:
            original = original_path.read_bytes()
            handoff = json.loads(original.decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise WrapperError(f"the Catalog handoff of {unit_id} cannot be read: {error}") from error
        retrieved = now()
        folder = staging / unit_id
        folder.mkdir(parents=True, exist_ok=True)
        sidecars: list[Offered] = []
        for key, name, kind in _SIDECARS:
            source = Path(str(handoff.get(key) or ""))
            if not str(handoff.get(key) or "").strip() or not source.is_file():
                continue
            shutil.copyfile(source, folder / name)
            handoff[key] = str(final_inputs / unit_id / name)
            sidecars.append(Offered(f"inputs/{unit_id}/{name}", kind, unit_id,
                                    {"source": f"msdial_catalog_reanalysis_handoff {key}"}))
        # The copy the triage binds: the Catalog's values, its sidecar paths pointing at the frozen copies, so a later
        # comparison with the handoff taken at run time compares the sidecars' contents, not the Catalog's live files.
        (folder / "handoff.json").write_text(json.dumps(handoff, ensure_ascii=False, indent=2), encoding="utf-8")
        offered.append(Offered(f"inputs/{unit_id}/handoff.json", "handoff", unit_id,
                               {"source": "msdial_catalog_reanalysis_handoff", "retrieved_at": retrieved,
                                "catalog_version": getattr(catalog, "version", None),
                                "original_sha256": sha256_bytes(original)}))
        offered.extend(sidecars)
        scope = handoff.get("download_scope") if isinstance(handoff.get("download_scope"), dict) else {}
        for item in [*(scope.get("bundle_urls") or []), *(scope.get("objects") or [])]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if re.search(r"(?i)\.zip(?:$|\?)", url):
                size = item.get("bytes", item.get("known_bytes"))
                archives.setdefault(url, int(size) if isinstance(size, int) and size > 0 else None)
        analysis = str(unit.get("source_subrecord_id") or handoff.get("source_subrecord_id") or "")
        if str(unit.get("repository") or handoff.get("repository")) == MW_REPOSITORY and re.fullmatch(r"AN[0-9]+", analysis):
            url = MWTAB_URL.format(analysis=analysis)
            if fetcher is None:
                notes.append(f"{analysis}: no mwTab fetcher; the mwTab text is not offered")
            else:
                try:
                    data = fetcher(url)
                    released, extra = release_safe_text(data)
                    if released is None:
                        notes.append(f"{analysis}: the mwTab text holds an absolute path that could not be made portable; "
                                     "it is not offered")
                    else:
                        (staging / f"{analysis}.mwtab.txt").write_bytes(released)
                        offered.append(Offered(f"inputs/{analysis}.mwtab.txt", "mwtab", unit_id,
                                               {"source_url": url, "retrieved_at": now(), **extra}))
                except Exception as error:  # noqa: BLE001
                    notes.append(f"{analysis}: the mwTab text could not be fetched ({type(error).__name__}: {error}); "
                                 "it is not offered")
    for url, size in sorted(archives.items()):
        name = _archive_name(url)
        if lister is None:
            notes.append(f"{name}: no archive lister; the listing is not offered and the validator holds")
            continue
        listing = lister(url, size)
        if not listing.get("ok", True) or not isinstance(listing.get("members"), list):
            notes.append(f"{name}: the remote listing was refused ({listing.get('reason') or 'unknown'}: "
                         f"{str(listing.get('message') or '')[:200]}); the validator holds")
            continue
        stem = re.sub(r"(?i)\.zip$", "", name)
        target = staging / f"{stem}.members.tsv"
        released, extra = release_safe_text(_listing_tsv(name, listing["members"]))
        if released is None:
            notes.append(f"{name}: the listing holds an absolute path that could not be made portable; it is not offered "
                         "and the validator holds")
            continue
        target.write_bytes(released)
        provenance = listing.get("provenance") if isinstance(listing.get("provenance"), dict) else {}
        tool = provenance.get("tool") if isinstance(provenance.get("tool"), dict) else {}
        offered.append(Offered(f"inputs/{target.name}", "listing", None, {**extra, "retrieval": {
            "method": "remote_central_directory",
            "tool": " ".join(str(x) for x in (tool.get("name") or "msdial_app.remote_archive.list_remote_zip",
                                                 tool.get("version")) if x),
            "url": url,
            "archive_bytes": _int_or_none(listing.get("size_bytes", provenance.get("size_bytes"))),
            "etag": provenance.get("etag"),
            "last_modified": provenance.get("last_modified"),
            "range_bytes_read": _int_or_none(provenance.get("bytes_fetched")),
        }}))
    if not archives:
        notes.append("the handoffs name no zip archive; no listing is offered and the validator holds")
    return Preparation(accession, units, offered, notes)


_TEXT_REDACTION = "absolute paths replaced by <external>/<file name> before the session"


def release_safe_text(data: bytes) -> tuple[bytes | None, dict[str, str]]:
    """A repository text (an mwTab text, an archive listing) as the session may read it. It is released byte for byte
    and bound by its sha256, so it is made portable before the agent reads it, never after: a drive letter, UNC or user
    profile path a submitter wrote becomes <external>/<file name>, and the provenance names the original's sha256.
    (None, {}) when a path is left that the rule cannot make portable."""
    if not curated._holds_absolute_path(data):  # noqa: SLF001 - the release's own path rules
        return data, {}
    text = data.decode("utf-8", "replace")
    out = _PROFILE_PATH.sub(lambda m: "<external>/" + _basename(m.group(0)),
                            _UNC_PATH.sub(lambda m: "<external>/" + _basename(m.group(0)),
                                          _DRIVE_PATH.sub(lambda m: "<external>/" + _basename(m.group(0)), text)))
    released = out.encode("utf-8")
    if curated._holds_absolute_path(released):  # noqa: SLF001
        return None, {}
    return released, {"original_sha256": sha256_bytes(data), "redaction": _TEXT_REDACTION}


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _archive_name(url: str) -> str:
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "archive.zip"
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def _listing_tsv(archive: str, members: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow(["archive", "path", "size", "crc32"])
    for member in members:
        if member.get("is_dir") or str(member.get("path") or "").endswith("/"):
            continue
        writer.writerow([archive, member.get("path"), int(member.get("size") or 0), str(member.get("crc32") or "").upper()])
    return buffer.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------------------------- the MCP servers

def load_server_configs(path: Path | None) -> dict[str, dict[str, Any]]:
    """{name: config} from --mcp-config ({"mcpServers": {...}}), else the user-scope servers of ~/.claude.json."""
    source = Path(path) if path else Path.home() / ".claude.json"
    try:
        data = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise WrapperError(f"the MCP server configuration {source.name} cannot be read: {error}") from error
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return {str(k): dict(v) for k, v in (servers or {}).items() if isinstance(v, dict)}


def _module_version(config: Mapping[str, Any]) -> str | None:
    """For a server run as `<python> -m <module>` that names no version of its own (FastMCP's default), the version of
    the distribution that provides the module, read with that interpreter."""
    args = [str(a) for a in config.get("args") or []]
    if "-m" not in args or args.index("-m") + 1 >= len(args):
        return None
    module = args[args.index("-m") + 1].split(".", 1)[0]
    script = ("import importlib.metadata as m, sys; d = m.packages_distributions().get(sys.argv[1]) or []; "
              "print(m.version(d[0]) if d else '')")
    try:
        done = subprocess.run([str(config["command"]), "-I", "-c", script, module], capture_output=True, timeout=60,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    text = done.stdout.decode("utf-8", "replace").strip()
    return f"{text} (package)" if done.returncode == 0 and text else None


def probe_mcp(config: Mapping[str, Any], timeout: float = 90.0) -> dict[str, Any]:
    """The server's own answer to initialize and tools/list over stdio: {version, tools, problem}. The tools it lists
    beyond the skill's are passed as --disallowedTools, which removes them from the session (checked 2026-11-18)."""
    out: dict[str, Any] = {"version": None, "tools": None, "problem": ""}
    if str(config.get("type") or "stdio") != "stdio" or not config.get("command"):
        return {**out, "problem": "not a stdio server; its tools could not be listed"}
    env = {**os.environ, **{str(k): str(v) for k, v in (config.get("env") or {}).items()}}
    try:
        process = subprocess.Popen([str(config["command"]), *[str(a) for a in config.get("args") or []]],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    except OSError as error:
        return {**out, "problem": f"the server could not be started: {error}"}
    lines: list[dict[str, Any]] = []
    ready = threading.Condition()

    def read() -> None:
        assert process.stdout is not None
        for raw in process.stdout:
            try:
                message = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            with ready:
                lines.append(message)
                ready.notify_all()

    threading.Thread(target=read, daemon=True).start()
    deadline = time.monotonic() + timeout

    def send(message: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        process.stdin.flush()

    def answer(ident: int) -> dict[str, Any] | None:
        with ready:
            while True:
                for message in lines:
                    if message.get("id") == ident and ("result" in message or "error" in message):
                        return message
                left = deadline - time.monotonic()
                if left <= 0 or process.poll() is not None:
                    return None
                ready.wait(min(left, 0.5))

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "run-triage-agent", "version": "1"}}})
        reply = answer(1)
        if reply is None or "result" not in reply:
            return {**out, "problem": "the server did not answer initialize"}
        info = reply["result"].get("serverInfo") or {}
        out["version"] = str(info.get("version") or "").strip() or _module_version(config)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools: list[str] = []
        cursor = None
        ident = 2
        while True:
            send({"jsonrpc": "2.0", "id": ident, "method": "tools/list", "params": ({"cursor": cursor} if cursor else {})})
            reply = answer(ident)
            if reply is None or "result" not in reply:
                return {**out, "problem": "the server did not answer tools/list"}
            tools += [str(t.get("name")) for t in reply["result"].get("tools") or [] if t.get("name")]
            cursor = reply["result"].get("nextCursor")
            ident += 1
            if not cursor:
                break
        out["tools"] = tools
        return out
    except OSError as error:
        return {**out, "problem": f"the server's stdio failed: {error}"}
    finally:
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=30)
        if process.stdout:
            process.stdout.close()


# ----------------------------------------------------------------------------------------------- the harness

def find_claude(given: str | None) -> list[str]:
    """The command that runs the CLI. A .py file is run with this interpreter (the tests' fake harness). The npm shim
    (claude, claude.cmd) is replaced by the native claude.exe beside it, so arguments are not re-parsed by cmd.exe."""
    if given:
        path = Path(given)
        if not path.is_file():
            found = shutil.which(given)
            if not found:
                raise WrapperError(f"--claude {path.name}: no such executable")
            path = Path(found)
        if path.suffix.lower() == ".py":
            return [sys.executable, "-I", str(path)]
        return [str(path)]
    for name in ("claude.exe", "claude"):
        found = shutil.which(name)
        if not found:
            continue
        native = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if Path(found).suffix.lower() in ("", ".cmd", ".ps1", ".bat") and native.is_file():
            return [str(native)]
        return [found]
    raise WrapperError("the claude executable was not found; pass --claude")


def _slashes(scrubbed: str, original: str) -> str:
    """A path the scrubber made portable, with forward slashes (a flag's value as the record shows it)."""
    return scrubbed.replace("\\", "/") if scrubbed != original else scrubbed


def harness_version(command: list[str]) -> str:
    try:
        done = subprocess.run([*command, "--version"], capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise WrapperError(f"the harness could not be run: {error}") from error
    text = done.stdout.decode("utf-8", "replace").strip()
    match = re.match(r"^\s*([0-9][0-9A-Za-z.+-]*)", text)
    if done.returncode != 0 or not match:
        raise WrapperError(f"`claude --version` gave no version ({text[:120]!r})")
    return match.group(1)


def session_environment() -> tuple[dict[str, str], list[str]]:
    env = {key: value for key, value in os.environ.items() if not (_ENV_DROP.match(key) and not _ENV_KEEP.match(key))}
    dropped = sorted(key for key in os.environ if key not in env)
    env.update(SESSION_ENV)
    return env, dropped


@dataclass
class Session:
    root: Path
    work: Path
    inputs: Path
    out: Path
    harness: Path
    session_id: str
    argv: list[str]
    portable_flags: list[str]
    servers: list[dict[str, Any]]
    allowed: list[str]
    denied: list[str]


def ancestor_problems(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """(why the directory is not outside every checkout, the context files an ancestor holds). A context file found
    is listed with its sha256: the session's flags tell the harness not to load it, but a clean run has none to load."""
    problems: list[str] = []
    files: list[dict[str, str]] = []
    home = long_path(Path.home())
    for ancestor in [path, *path.parents]:
        if (ancestor / ".git").exists():
            problems.append(f"the session directory lies inside a git checkout ({ancestor.name or ancestor})")
        for name in CONTEXT_FILE_NAMES:
            # ~/.claude is the user configuration (user CLAUDE.md and settings), which --restricted and
            # CLAUDE_CODE_DISABLE_CLAUDE_MDS leave unread, also for a session under the home folder (a temp folder):
            # checked 2026-11-18 with CLI 2.1.318, whose debug log said "project memory is off" and whose first request
            # was the same size with and without the environment settings, far below the user CLAUDE.md's size.
            if name.startswith(".claude/") and long_path(ancestor) == home:
                continue
            candidate = ancestor / name
            if candidate.is_file():
                files.append({"path": f"<ancestor>/{ancestor.name or 'root'}/{name}",
                              "sha256": sha256_bytes(candidate.read_bytes())})
    return problems, files


def spec_server_config(code_dir: Path, inputs: Path, out: Path, work: Path, skill_version: str) -> dict[str, Any]:
    """The spec check server of one session (scripts/triage-spec-server.py from the session's code tree)."""
    return {"type": "stdio", "command": sys.executable,
            "args": ["-I", str(code_dir / SPEC_SERVER_PATH), "--inputs", str(inputs), "--out", str(out), "--work", str(work),
                     "--skill-version", skill_version]}


def build_session(root: Path, code: CodeSnapshot, triage_inputs: Path, offered: list[Offered],
                  servers: Mapping[str, Mapping[str, Any]], claude: list[str], model: str | None,
                  probe: Callable[[Mapping[str, Any]], dict[str, Any]], effort: str | None = None,
                  code_dir: Path | None = None) -> Session:
    root = long_path(root)
    work, inputs, out, harness = root / "work", root / "inputs", root / "out", root / "harness"
    for folder in (work, inputs, out, harness):
        folder.mkdir(parents=True, exist_ok=False)
    skill = work / SKILL_PATH
    skill.parent.mkdir(parents=True)
    skill.write_bytes(code.skill)
    # The spec schema the skill names, at the same commit: without it the agent writes field names from the skill's
    # prose, and they are refused (pilot of 2026-11-18).
    if code.schema:
        schema = work / SCHEMA_PATH
        schema.parent.mkdir(parents=True)
        schema.write_bytes(code.schema)
    for item in offered:
        source = triage_inputs.parent / item.path
        target = root / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    allowed = code.allowed_tools
    wanted = sorted({validator.tool_server(t) for t in allowed} - {None})
    if SPEC_SERVER in wanted:
        if code_dir is None:
            raise WrapperError(f"the skill allows {SPEC_SERVER}, but no code tree was written for it")
        servers = {**servers, SPEC_SERVER: spec_server_config(long_path(code_dir), inputs, out, work, code.skill_version)}
    missing = [name for name in wanted if name not in servers]
    if missing:
        raise WrapperError(f"no configuration for the MCP server(s) the skill allows: {', '.join(missing)}")
    recorded: list[dict[str, Any]] = []
    denied: list[str] = []
    for name in wanted:
        answer = probe(servers[name])
        if answer.get("tools") is None:
            raise WrapperError(f"the tools of MCP server {name} could not be listed ({answer.get('problem')}); without "
                               "them the tools the skill does not allow cannot be removed from the session")
        denied += [f"mcp__{name}__{tool}" for tool in answer["tools"]
                   if not validator.tool_allowed(f"mcp__{name}__{tool}", allowed)]
        recorded.append({"name": name, "version": answer.get("version"), "tools": answer["tools"]})
    (harness / "mcp.json").write_text(json.dumps({"mcpServers": {name: servers[name] for name in wanted}}, indent=1),
                                      encoding="utf-8")
    (harness / "settings.json").write_text(json.dumps(SESSION_SETTINGS), encoding="utf-8")
    builtin = [t for t in allowed if validator.tool_server(t) is None]
    session_id = str(uuid.uuid4())
    flags: list[tuple[str, ...]] = [
        ("-p",), ("--output-format", "stream-json"), ("--verbose",), ("--restricted",),
        ("--settings", str(harness / "settings.json")), ("--strict-mcp-config",),
        ("--mcp-config", str(harness / "mcp.json")), ("--tools", ",".join(builtin)),
        ("--allowedTools", ",".join(allowed)),
    ]
    if denied:
        flags.append(("--disallowedTools", ",".join(denied)))
    flags += [("--permission-mode", "dontAsk"), ("--permission-prompts", "none"), ("--disable-slash-commands",),
              ("--no-session-persistence",), ("--session-id", session_id),
              ("--debug-file", str(harness / "debug.log")), ("--add-dir", str(inputs), str(out))]
    if model:
        flags.append(("--model", model))
    if effort:
        flags.append(("--effort", effort))
    argv = [*claude, *[part for flag in flags for part in flag]]
    scrub = Scrubber.for_session(root)
    portable = [_slashes(scrub.text(part), part) for flag in flags for part in flag]
    portable[portable.index("--session-id") + 1] = "<session_id>"
    return Session(root, work, inputs, out, harness, session_id, argv, portable, recorded, allowed, denied)


@dataclass
class Outcome:
    exit_code: int | None
    timed_out: bool
    started_at: str
    finished_at: str
    transcript: bytes


def launch(session: Session, prompt: bytes, timeout: float) -> Outcome:
    env, dropped = session_environment()
    (session.harness / "environment.json").write_text(json.dumps({"set": SESSION_ENV, "dropped": dropped}, indent=1),
                                                     encoding="utf-8")
    transcript_path = session.harness / "transcript.jsonl"
    started = now()
    kwargs: dict[str, Any] = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    with transcript_path.open("wb") as stdout, (session.harness / "stderr.txt").open("wb") as stderr:
        process = subprocess.Popen(session.argv, cwd=str(session.work), stdin=subprocess.PIPE, stdout=stdout,
                                   stderr=stderr, env=env, **kwargs)
        timed_out = False

        def feed() -> None:
            # The prompt goes in on its own thread: a pipe holds a few KB, and a CLI that does not read its stdin would
            # otherwise block the write before the timeout clock below has started.
            try:
                assert process.stdin is not None
                process.stdin.write(prompt)
                process.stdin.close()
            except (OSError, ValueError):
                pass

        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(process)
        writer.join(timeout=30)
    return Outcome(process.returncode, timed_out, started, now(), transcript_path.read_bytes())


def _kill_tree(process: subprocess.Popen) -> None:
    """Stop the session this wrapper started, with its children (MCP servers), and nothing else."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=60,
                           check=False)
        else:
            import signal  # noqa: PLC0415

            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()


# ------------------------------------------------------------------------------------------- the transcript

@dataclass
class Call:
    ident: str
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None = None


@dataclass
class Transcript:
    init: dict[str, Any] | None
    calls: list[Call]
    result: dict[str, Any] | None
    models: list[str]
    problems: list[str]


def parse_transcript(data: bytes) -> Transcript:
    """The harness's stream JSON: its init message, every tool_use block in order (once per id) with the tool_result
    that answers it, its final result message and the models its assistant messages name."""
    init = None
    result = None
    calls: dict[str, Call] = {}
    order: list[str] = []
    models: list[str] = []
    problems: list[str] = []
    for number, line in enumerate(data.decode("utf-8-sig", "replace").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            problems.append(f"line {number} of the transcript is not JSON")
            continue
        if not isinstance(message, dict):
            continue
        kind = message.get("type")
        if kind == "system" and message.get("subtype") == "init" and init is None:
            init = message
        elif kind == "result":
            result = message
        body = message.get("message") if isinstance(message.get("message"), dict) else {}
        if kind == "assistant" and body.get("model") and body["model"] not in models:
            models.append(str(body["model"]))
        for block in body.get("content") if isinstance(body.get("content"), list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                if block["id"] not in calls:
                    order.append(block["id"])
                    calls[block["id"]] = Call(block["id"], str(block.get("name") or ""),
                                              dict(block.get("input") or {}))
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                calls[block["tool_use_id"]].result = block
    return Transcript(init, [calls[i] for i in order], result, models, problems)


def assess_context(transcript: Transcript, session: Session, ancestors: tuple[list[str], list[dict[str, str]]],
                   debug_log: str) -> tuple[dict[str, Any], list[str]]:
    """agent.context as the transcript shows it was achieved, and the notes behind a configuration that is not clean."""
    problems: list[str] = list(ancestors[0])
    init = transcript.init or {}
    if not transcript.init:
        problems.append("the transcript has no init message, so the tools, servers and plugins loaded are unknown")
    else:
        # What the init does not say was not checked, and what was not checked is not clean.
        for key in ("tools", "mcp_servers", "plugins"):
            if not isinstance(init.get(key), list):
                problems.append(f"the init message does not list {key}, so they were not checked")
    allowed = set(session.allowed)
    offered_tools = [str(t) for t in init.get("tools") or []]
    extra_tools = [t for t in offered_tools if not validator.tool_allowed(t, allowed)]
    if extra_tools:
        problems.append("the harness offered tools the skill does not list: " + ", ".join(sorted(extra_tools)))
    servers = [s for s in init.get("mcp_servers") or [] if isinstance(s, dict)]
    wanted = {s["name"] for s in session.servers}
    extra_servers = sorted({str(s.get("name")) for s in servers} - wanted)
    if extra_servers:
        problems.append("MCP servers the skill does not allow were loaded: " + ", ".join(extra_servers))
    status = {str(s.get("name")): str(s.get("status") or "") for s in servers}
    for name in sorted(wanted):
        if transcript.init and status.get(name) != "connected":
            problems.append(f"the MCP server {name} the skill allows was {status.get(name) or 'not loaded'}, not connected")
    plugins = [p for p in init.get("plugins") or [] if isinstance(p, dict)]
    foreign = [str(p.get("name")) for p in plugins if str(p.get("path") or "") != "builtin"]
    if foreign:
        problems.append("plugins outside the harness were loaded: " + ", ".join(foreign))
    for key in ("skills", "slash_commands"):
        if init.get(key):
            problems.append(f"the harness loaded {key}: " + ", ".join(str(x) for x in init[key][:10]))
    cwd = str(init.get("cwd") or "")
    if transcript.init and cwd and long_path(cwd) != long_path(session.work):
        problems.append("the session ran in another working directory")
    # Hooks and memory as the harness's debug log states them (CLI 2.1.318: "Registered 0 hooks from 5 plugins", and
    # "project memory is off" from the context loader). A log that does not say is not taken as a clean answer.
    hooks: list[str] = []
    registered = [int(n) for n in re.findall(r"Registered (\d+) hooks?\b", debug_log)]
    if not debug_log.strip():
        problems.append("the harness wrote no debug log, so the hooks and memory were not checked")
    elif not registered:
        problems.append("the debug log names no hook registration, so the hooks were not checked")
    elif any(registered):
        hooks.append(f"{max(registered)} hook(s) registered (harness debug log)")
        problems.append("hooks were registered")
    if re.search(r"project memory is off", debug_log):
        memory = "disabled"
    else:
        memory = "not observed: the debug log does not say that memory is off"
        if debug_log.strip():
            problems.append("the debug log does not say that memory is off")
    context_files = ancestors[1]
    if context_files:
        problems.append(f"{len(context_files)} context file(s) lie in an ancestor of the session directory")
    loaded = {str(s.get("name")): s for s in servers if status.get(str(s.get("name"))) == "connected"}
    mcp_servers = [{"name": s["name"], "version": s.get("version")} for s in session.servers if s["name"] in loaded]
    builtin = sorted(str(p.get("name")) for p in plugins if str(p.get("path") or "") == "builtin")
    flags = [*session.portable_flags,
             *[f"env {key}={value}" for key, value in SESSION_ENV.items()],
             "settings " + json.dumps(SESSION_SETTINGS, separators=(",", ":")),
             *[f"harness built-in plugin {name}" for name in builtin]]
    context = {
        "configuration": "clean" if not problems else "not clean: " + "; ".join(problems),
        "working_directory": WORKING_DIRECTORY,
        "context_files": context_files,
        "memory": memory,
        "hooks": hooks,
        "mcp_servers": mcp_servers,
        "harness_flags": flags,
    }
    return context, problems


# -------------------------------------------------------------------------------------------- acceptance

def tree_digest(folder: Path) -> dict[str, str]:
    return {p.relative_to(folder).as_posix(): sha256_bytes(p.read_bytes())
            for p in sorted(folder.rglob("*")) if p.is_file()}


_READ_TOOLS = ("Read", "Glob", "Grep", "LS", "NotebookRead")
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def _within(path: Path, roots: Iterable[Path]) -> bool:
    """Whether `path` is one of `roots` or lies under one, compared as resolved long paths (case folded on Windows)."""
    target = os.path.normcase(str(long_path(path)))
    for root in roots:
        base = os.path.normcase(str(long_path(root)))
        if target == base or target.startswith(base.rstrip("\\/") + os.sep):
            return True
    return False


def _read_targets(call: "Call", work: Path) -> list[tuple[str, Path]]:
    """The paths a read or search call names: (as given, resolved against the working directory). A glob pattern counts
    from its first wildcard back."""
    named = [str(call.arguments.get(key) or "") for key in ("file_path", "path", "notebook_path")]
    pattern = str(call.arguments.get("pattern") or "")
    if validator.bare_tool(call.name) == "Glob" and pattern:
        named.append(re.split(r"[*?\[{]", pattern, maxsplit=1)[0] or ".")
    out = []
    for text in named:
        if not text.strip():
            continue
        try:
            path = Path(text)
            out.append((text, path if path.is_absolute() else work / path))
        except (OSError, ValueError):
            out.append((text, Path("<unreadable path>")))
    return out


def acceptance_problems(session: Session, outcome: Outcome, transcript: Transcript, expected: Mapping[str, str],
                        inputs_before: dict[str, str], work_before: dict[str, str],
                        scrub: "Scrubber | None" = None) -> tuple[list[str], dict[str, Path]]:
    """(why nothing is accepted, the payloads that would be, by unit). `expected` maps each payload file name the session
    may write to its unit. A session is accepted whole or not at all: a payload holding an absolute path, an e-mail
    address, a secret or an operator's name refuses it, since a payload is released byte for byte and made immutable
    here; so does a read outside the session's working directory, inputs and output folder."""
    problems: list[str] = []
    if outcome.timed_out:
        problems.append("the session timed out and was stopped")
    if outcome.exit_code != 0:
        problems.append(f"the harness exited with status {outcome.exit_code}")
    result = transcript.result or {}
    if not transcript.result:
        problems.append("the transcript has no result message")
    elif result.get("is_error") or result.get("subtype") != "success":
        problems.append(f"the session ended in {result.get('subtype')!r}" + (" (is_error)" if result.get("is_error") else ""))
    payloads: dict[str, Path] = {}
    for path in sorted(session.out.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(session.out).as_posix()
        if relative.endswith(".record.json") or relative == curated.REVIEW_LOG:
            problems.append(f"the model wrote {relative}; only the wrapper writes a decision record and the review log")
        elif relative in expected:
            payloads[expected[relative]] = path
            if scrub is not None:
                try:
                    kinds = scrub.found(path.read_bytes().decode("utf-8", "replace"))
                except OSError as error:
                    kinds = [f"unreadable bytes ({type(error).__name__})"]
                if kinds:
                    problems.append(f"the payload {relative} holds {', '.join(kinds)}; a payload is released byte for "
                                    "byte and never rewritten")
        else:
            problems.append(f"the model wrote {relative}, which is not a payload this session was asked for")
    readable = (session.work, session.inputs, session.out)
    for call in transcript.calls:
        if validator.bare_tool(call.name) in _READ_TOOLS:
            for text, path in _read_targets(call, session.work):
                if not _within(path, readable):
                    problems.append(f"tool call {call.name} read outside the session's folders "
                                    f"({_basename(text) or 'a path'}); nothing outside them is evidence")
            continue
        if validator.bare_tool(call.name) not in _WRITE_TOOLS:
            continue
        target = str(call.arguments.get("file_path") or call.arguments.get("notebook_path") or "")
        try:
            written = Path(target) if Path(target).is_absolute() else session.work / target
            inside = bool(target) and long_path(written).parent == long_path(session.out)
        except (OSError, ValueError):
            inside = False
        if not inside:
            problems.append(f"tool call {call.name} wrote outside the output folder ({Path(target).name or target})")
    if tree_digest(session.inputs) != inputs_before:
        problems.append("the session's inputs changed during the session")
    if tree_digest(session.work) != work_before:
        problems.append("the session's working directory changed during the session")
    return problems, payloads


# ----------------------------------------------------------------------------------------------- the record

_ROLES = {"handoff": "catalog_handoff", "samples": "catalog_samples", "files": "catalog_files",
          "analysis_inputs": "catalog_analysis_inputs"}
DIGEST_SOURCE = "scripts/triage-digest.py over the other inputs, before the session"
TRANSCRIPT_REDACTION = "run-triage-agent.py scrubber; check-release-redaction.py v2"


def _input_entry(item: Offered, role: str, triage_dir: Path, source: str | None = None) -> dict[str, Any]:
    data = (triage_dir / item.path).read_bytes()
    out: dict[str, Any] = {"role": role, "path": item.path, "sha256": sha256_bytes(data), "bytes": len(data)}
    if source:
        out["source"] = source
    return out


def record_inputs(unit_id: str, triage_dir: Path, offered: list[Offered]) -> list[dict[str, Any]]:
    """Every input offered to the session, as a record binds it: the unit's own handoff and sidecars first, then the
    listings, the unit's mwTab text, and the other units' handoffs, sidecars and mwTab texts."""
    def entry(item: Offered, role: str) -> dict[str, Any]:
        out = _input_entry(item, role, triage_dir)
        if item.kind == "handoff":
            out["source"] = "msdial_catalog_reanalysis_handoff" if item.unit == unit_id else \
                f"msdial_catalog_reanalysis_handoff (unit {item.unit})"
            if item.provenance.get("catalog_version"):
                out["catalog_version"] = str(item.provenance["catalog_version"])
            out["retrieved_at"] = item.provenance["retrieved_at"]
        elif item.kind in _ROLES:
            out["source"] = f"{item.provenance['source']}" + ("" if item.unit == unit_id else f" (unit {item.unit})")
        elif item.kind == "listing":
            out["retrieval"] = item.provenance["retrieval"]
        elif item.kind == "mwtab":
            out["source_url"] = item.provenance["source_url"]
            out["retrieved_at"] = item.provenance["retrieved_at"]
        elif item.kind == "digest":
            out["source"] = DIGEST_SOURCE
        for key in ("original_sha256", "redaction"):
            if item.provenance.get(key):
                out[key] = item.provenance[key]
        return out

    own = [entry(i, _ROLES[i.kind]) for i in offered if i.unit == unit_id and i.kind in _ROLES]
    listings = [entry(i, "archive_listing") for i in offered if i.kind == "listing"]
    texts = [entry(i, "metadata_text") for i in offered if i.kind == "mwtab" and i.unit == unit_id]
    others = [entry(i, "other") for i in offered if i.unit not in (unit_id, None) and i.kind in _ROLES]
    other_texts = [entry(i, "metadata_text") for i in offered if i.kind == "mwtab" and i.unit not in (unit_id, None)]
    digest = [entry(i, "triage_digest") for i in offered if i.kind == "digest"]
    return [*own, *listings, *texts, *digest, *others, *other_texts]


def redact_transcript(data: bytes, scrub: Scrubber) -> bytes:
    """The harness transcript as it may be released: every JSON line with its strings scrubbed (secrets, e-mail
    addresses, the operator's names, absolute paths made portable), compact; a line that is not JSON scrubbed as text.
    The record's tool_calls and stored results are read from these bytes, so they are what the released transcript
    shows by construction."""
    lines = []
    for line in data.decode("utf-8-sig", "replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            lines.append(scrub.text(line))
            continue
        lines.append(json.dumps(scrub.value(value), ensure_ascii=False, separators=(",", ":")))
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


def stored_calls(transcript: Transcript, calls_dir: str) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """(the record's tool_calls, the result files to write under `calls_dir`, by path relative to the triage dir). Each
    result is {tool_use_id, tool, is_error, content} as the (redacted) transcript shows it."""
    entries: list[dict[str, Any]] = []
    files: dict[str, bytes] = {}
    for seq, call in enumerate(transcript.calls, start=1):
        entry: dict[str, Any] = {"seq": seq, "tool": call.name, "arguments": call.arguments,
                                 "status": "error", "result_path": None, "result_sha256": None}
        if call.result is not None:
            stored = {"tool_use_id": call.ident, "tool": call.name, "is_error": bool(call.result.get("is_error")),
                      "content": call.result.get("content")}
            data = json.dumps(stored, ensure_ascii=False, indent=1).encode("utf-8")
            path = f"{calls_dir}/{seq}.json"
            files[path] = data
            entry.update(status="error" if stored["is_error"] else "ok", result_path=path, result_sha256=sha256_bytes(data))
        entries.append(entry)
    return entries, files


@dataclass
class SessionResult:
    """An accepted session: what every record of it shares."""

    outcome: Outcome
    transcript: Transcript  # parsed from the redacted bytes
    payloads: dict[str, Path]
    context: dict[str, Any]
    scrub: Scrubber
    version: str
    released: dict[str, Any]  # the record's transcript block
    transcript_file: tuple[str, bytes] | None  # (path relative to the triage dir, bytes) when released


def model_id(transcript: Transcript) -> str:
    """The model a session ran, as its transcript names it (the init message's, else the first assistant message's)."""
    return str((transcript.init or {}).get("model") or (transcript.models[:1] or ["unknown"])[0])


def build_record(*, unit_id: str, accession: str, payload_name: str, data: bytes, supersedes: str | None,
                 inputs: list[dict[str, Any]], code: CodeSnapshot, template_path: str, template: bytes, prompt: bytes,
                 prompt_path: str, calls: list[dict[str, Any]], session: Session, result: SessionResult,
                 derivation: dict[str, Any] | None = None) -> bytes:
    """The record's bytes for the payload `data` (to be `payload_name` beside it). Refused (WrapperError) when the record
    would hold an absolute path, since it is released byte for byte."""
    try:
        document = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        document = {}
    if not isinstance(document, dict):
        document = {}
    rationale = document.get("rationale") if isinstance(document.get("rationale"), str) else ""
    transcript = result.transcript
    record = {
        "schema": RECORD_SCHEMA,
        "decision_id": sha256_bytes(data),
        "kind": "unit_triage",
        "accession": accession,
        "unit_id": unit_id,
        "payload": {"path": payload_name, "schema": str(document.get("schema") or "unknown"), "sha256": sha256_bytes(data)},
        "supersedes": supersedes,
        "inputs": inputs,
        "skill": {"name": SKILL_NAME, "version": code.skill_version, "path": SKILL_PATH,
                  "sha256": sha256_bytes(code.skill), "repo_commit": code.commit},
        "wrapper": code.wrapper_identity,
        "prompt": {"template_path": template_path, "template_sha256": sha256_bytes(template),
                   "rendered_path": prompt_path, "rendered_sha256": sha256_bytes(prompt)},
        "agent": {"harness": HARNESS, "harness_version": result.version,
                  "model_id": model_id(transcript),
                  "sampling": SAMPLING, "session_id": str((transcript.init or {}).get("session_id") or session.session_id),
                  "context": result.context},
        "started_at": result.outcome.started_at,
        "finished_at": result.outcome.finished_at,
        "tool_calls": calls,
        "transcript": result.released,
        "rationale": result.scrub.text(rationale.strip()) or "The payload states no unit-level rationale.",
    }
    if derivation is not None:
        record["derivation"] = derivation
    text = json.dumps(record, ensure_ascii=False, indent=1).encode("utf-8")
    if curated._holds_absolute_path(text):  # noqa: SLF001 - the release's own path rules
        raise WrapperError(f"the decision record of {unit_id} would hold an absolute path; neither it nor its payload "
                           "was written")
    return text


def commit_files(triage_dir: Path, files: list[tuple[str, bytes]]) -> None:
    """Every file of an accepted session (tool results, the released transcript, the records, the payloads), each
    never over an existing file, all or nothing: on any failure the files this call wrote are removed again and the
    error is raised, so a rerun finds the accession as it was."""
    written: list[Path] = []
    try:
        for relative, data in files:
            target = triage_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                written.append(target)
                handle.write(data)
    except BaseException as error:
        left = []
        for path in reversed(written):
            try:
                path.unlink()
            except OSError:
                left.append(path.name)
        raise WrapperError(f"the commit failed ({type(error).__name__}: {error}); nothing was kept"
                           + (f", except {', '.join(left)}, which could not be removed" if left else "")) from error


# ------------------------------------------------------------------------------------------------ the driver

@dataclass
class Ports:
    catalog: Any
    lister: Callable[[str, int | None], dict[str, Any]] | None
    fetcher: Callable[[str], bytes] | None
    servers: Mapping[str, Mapping[str, Any]]
    claude: list[str]
    code: CodeSnapshot
    probe: Callable[[Mapping[str, Any]], dict[str, Any]] = probe_mcp
    harness: str | None = None  # `claude --version`, read once before any accession is prepared


@dataclass
class Options:
    campaign_dir: Path
    session_root: Path
    dry_run: bool
    model: str | None
    timeout: float
    analysis_purpose: str | None
    redact_terms: list[str]
    revise: dict[str, int] = field(default_factory=dict)  # unit id -> the review log seq that causes its revision
    effort: str | None = None


def existing_payloads(triage_dir: Path) -> list[str]:
    return sorted(p.name for p in triage_dir.glob("*.json") if validator.is_payload_file(p.name))


def existing_decisions(triage_dir: Path) -> list[str]:
    """The payloads and the decision records of a triage directory: either one means the accession was triaged, or a
    commit was left half done, and a fresh triage never writes beside it."""
    return sorted(p.name for p in triage_dir.glob("*.json") if p.name != "index.json")


class TriageLock:
    """<triage dir>\\.triage.lock, created exclusively for one run of one accession (fresh or revision), so that two
    runs, in one process or two, never prepare, move or commit in the same directory at once. NTFS folds case, so
    ST002747 and st002747 are one directory and one lock."""

    def __init__(self, triage_dir: Path, stamp: str) -> None:
        self.path = triage_dir / LOCK_NAME
        self.stamp = stamp
        self.held = False

    def __enter__(self) -> "TriageLock":
        try:
            handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise WrapperError(f"another run holds {self.path.name} in this triage directory; if none is running, a "
                               "run was stopped hard and the lock may be removed by hand") from error
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "stamp": self.stamp, "at": now()}))
        self.held = True
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass


def run_session(session: Session, prompt: bytes, expected: Mapping[str, str], accession: str, stamp: str,
                options: Options, ports: Ports, summary: dict[str, Any]) -> SessionResult | None:
    """Launch the prepared session, accept it whole or refuse it, and make its transcript releasable. None when refused
    (summary["problems"] and harness\\failure.json say why)."""
    version = ports.harness or harness_version(ports.claude)
    inputs_before, work_before = tree_digest(session.inputs), tree_digest(session.work)
    outcome = launch(session, prompt, options.timeout)
    raw = parse_transcript(outcome.transcript)
    summary["exit_code"] = outcome.exit_code
    summary["harness_version"] = version
    reported = str((raw.init or {}).get("claude_code_version") or "")
    if reported and reported != version:
        summary["notes"].append(f"the transcript names harness {reported}; `--version` said {version}")
    scrub = Scrubber.for_session(session.root, operator_terms(options.redact_terms))
    problems, payloads = acceptance_problems(session, outcome, raw, expected, inputs_before, work_before, scrub)
    if problems:
        (session.harness / "failure.json").write_text(json.dumps({"problems": problems, "at": now()}, indent=1),
                                                      encoding="utf-8")
        summary["problems"] = problems
        summary["status"] = "refused"
        return None
    debug = (session.harness / "debug.log").read_text(encoding="utf-8", errors="replace") \
        if (session.harness / "debug.log").is_file() else ""
    context, _notes = assess_context(raw, session, ancestor_problems(session.root), debug)
    summary["configuration"] = context["configuration"]
    summary["notes"] += raw.problems
    redacted = redact_transcript(outcome.transcript, scrub)
    transcript = parse_transcript(redacted)
    (session.harness / "transcript.redacted.jsonl").write_bytes(redacted)
    released: dict[str, Any] = {"sha256": None, "released_path": None,
                                "unredacted_sha256": sha256_bytes(outcome.transcript), "redaction": None}
    transcript_file = None
    name = f"{TRANSCRIPTS}/{accession}-{stamp}.redacted.jsonl"
    why = ""
    if context["configuration"] != "clean":
        why = "the configuration was not clean, and such a transcript is not released"
    else:
        findings = curated.redaction_check().check_file(session.harness / "transcript.redacted.jsonl", name, (),
                                                        scrub.terms)
        if findings:
            why = "the redacted transcript fails check-release-redaction.py (" + \
                  ", ".join(sorted({item["rule"] for item in findings})) + ")"
    if why:
        summary["notes"].append(f"the transcript is not released: {why}; the validator holds")
    else:
        released.update(sha256=sha256_bytes(redacted), released_path=name, redaction=TRANSCRIPT_REDACTION)
        transcript_file = (name, redacted)
    return SessionResult(outcome, transcript, payloads, context, scrub, version, released, transcript_file)


def triage_accession(accession: str, options: Options, ports: Ports) -> dict[str, Any]:
    summary: dict[str, Any] = {"accession": accession, "status": "failed", "dry_run": options.dry_run, "notes": []}
    triage_dir = long_path(options.campaign_dir) / "triage" / accession
    summary["triage_dir"] = str(triage_dir)
    if not options.revise:
        already = existing_decisions(triage_dir) if triage_dir.is_dir() else []
        if already:
            summary["problems"] = [f"{accession} is already triaged ({', '.join(already[:5])}); a payload is immutable: "
                                   "a re-triage is a revision (--revise <unit_id>=<review log seq>)"]
            return summary
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    session_root = long_path(options.session_root) / accession / stamp
    summary["session_dir"] = str(session_root)
    ancestors = ancestor_problems(session_root.parent)
    if ancestors[0]:
        summary["problems"] = ancestors[0]
        return summary
    try:
        triage_dir.mkdir(parents=True, exist_ok=True)
        with TriageLock(triage_dir, stamp):
            if options.revise:
                return revise_accession(accession, triage_dir, session_root, stamp, options, ports, summary)
            return _first_triage(accession, triage_dir, session_root, stamp, options, ports, summary)
    except WrapperError as error:
        summary["problems"] = [str(error)]
        return summary
    except Exception as error:  # noqa: BLE001 - one accession's failure never stops the others
        summary["problems"] = [f"{type(error).__name__}: {error}"]
        return summary
    finally:
        leftover = triage_dir / f".inputs-{stamp}"
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)


def _first_triage(accession: str, triage_dir: Path, session_root: Path, stamp: str, options: Options, ports: Ports,
                  summary: dict[str, Any]) -> dict[str, Any]:
    already = existing_decisions(triage_dir)
    if already:
        summary["problems"] = [f"{accession} is already triaged ({', '.join(already[:5])}); a payload is immutable: a "
                               "re-triage is a revision (--revise <unit_id>=<review log seq>)"]
        return summary
    final_inputs = triage_dir / "inputs"
    staging = triage_dir / f".inputs-{stamp}"
    preparation = prepare_inputs(accession, staging, final_inputs, ports.catalog, ports.lister, ports.fetcher)
    if final_inputs.exists():
        aside = long_path(options.session_root) / accession / f"superseded-inputs-{stamp}"
        aside.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(final_inputs), str(aside))
        summary["notes"].append(f"earlier inputs (no payload bound them) moved to {aside}")
    staging.rename(final_inputs)
    summary["notes"] += preparation.notes
    units = [u["unit_id"] for u in preparation.units]
    summary["units"] = units
    code_dir = ports.code.materialise(long_path(session_root) / "code")
    digest = load_module(code_dir, DIGEST_PATH)
    data = digest.build_digest(final_inputs)
    if curated._holds_absolute_path(data):  # noqa: SLF001 - the release's own path rules
        raise WrapperError("the digest would hold an absolute path; it is released byte for byte")
    (final_inputs / "digest.md").write_bytes(data)
    preparation.offered.append(Offered(DIGEST_INPUT, "digest", None, {"source": DIGEST_SOURCE,
                                                                      "version": str(digest.DIGEST_VERSION)}))
    spec_name = f"{accession}.spec.json"
    prompt = render_prompt(ports.code.template, accession=accession, unit_ids=", ".join(units),
                           input_dir="../inputs", output_dir="../out", spec_name=spec_name,
                           analysis_purpose=options.analysis_purpose if options.analysis_purpose else "null")
    for unit in units:
        (final_inputs / unit).mkdir(parents=True, exist_ok=True)
        (final_inputs / unit / "prompt.md").write_bytes(prompt)
    session = _prepare_session(session_root, final_inputs, preparation.offered, preparation.notes, prompt, ports,
                               options, summary, code_dir)
    if options.dry_run:
        summary["status"] = "prepared"
        return summary
    result = run_session(session, prompt, {spec_name: SPEC_KEY}, accession, stamp, options, ports, summary)
    if result is None:
        return summary
    expanded = expand_session(session, result, code_dir, final_inputs, spec_name, options.analysis_purpose, ports.code,
                              summary)
    if expanded is None:
        return summary
    spec_bytes, payloads, derivation = expanded
    files: list[tuple[str, bytes]] = []
    if result.transcript_file is not None:
        files.append(result.transcript_file)
    files.append((spec_name, spec_bytes))
    derivation["digest"] = {"path": DIGEST_INPUT, "sha256": sha256_bytes(data),
                            "script": ports.code.identity(DIGEST_PATH, str(digest.DIGEST_VERSION))}
    accepted = []
    for unit in units:
        payload = payloads[unit]
        calls, call_files = stored_calls(result.transcript, f"inputs/{unit}/calls")
        record = build_record(unit_id=unit, accession=accession, payload_name=f"{unit}.json", data=payload, supersedes=None,
                              inputs=record_inputs(unit, triage_dir, preparation.offered), code=ports.code,
                              template_path=TEMPLATE_PATH, template=ports.code.template, prompt=prompt,
                              prompt_path=f"inputs/{unit}/prompt.md", calls=calls, session=session, result=result,
                              derivation=derivation)
        files += [*call_files.items(), (f"{unit}{validator.RECORD_SUFFIX}", record), (f"{unit}.json", payload)]
        accepted.append(unit)
    commit_files(triage_dir, files)
    summary["accepted"] = accepted
    summary["spec"] = {"path": spec_name, "bytes": len(spec_bytes)}
    return _validate_after(triage_dir, ports, session, summary)


def expand_session(session: Session, result: SessionResult, code_dir: Path, inputs_dir: Path, spec_name: str,
                   purpose: str | None, code: CodeSnapshot, summary: dict[str, Any], *,
                   revise: frozenset[str] | None = None,
                   current: dict[str, dict[str, Any]] | None = None) -> tuple[bytes, dict[str, bytes], dict[str, Any]] | None:
    """The accepted session's spec expanded into the payloads (expand-triage-spec.py of the session's code tree), with the
    record's derivation block; None, with the session refused (harness\\expansion.json, summary problems), on any
    finding. The context is the record's: decided_at its finished_at, model its model_id."""
    if SPEC_KEY not in result.payloads:
        summary["problems"] = [f"the session wrote no spec ({spec_name}); nothing is accepted"]
        summary["status"] = "refused"
        return None
    spec_bytes = result.payloads[SPEC_KEY].read_bytes()
    expander = load_module(code_dir, EXPANDER_PATH)
    context = {"decided_at": result.outcome.finished_at, "model": model_id(result.transcript), "agent": HARNESS,
               "skill_version": code.skill_version, "purpose": purpose}
    ctx = expander.ExpansionContext(decided_at=context["decided_at"], model=context["model"], agent=context["agent"],
                                    skill_version=context["skill_version"], purpose=purpose, revise=revise, current=current)
    expansion = expander.expand(spec_bytes, expander.inputs_from_dir(inputs_dir), ctx, spec_name=spec_name)
    report = {"spec": spec_name, "expander_version": str(expander.EXPANDER_VERSION), "ok": expansion.ok,
              "findings": [f.as_dict() for f in expansion.findings], "counts": expansion.counts,
              "payload_bytes": {u: len(b) for u, b in expansion.payloads.items()}}
    (session.harness / "expansion.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    if not expansion.ok:
        shown = [f"{f.code} {f.unit or '-'}: {f.message}" for f in expansion.findings[:10]]
        summary["problems"] = [f"the spec {spec_name} does not expand ({len(expansion.findings)} finding(s)); nothing is "
                               "accepted", *shown]
        summary["status"] = "refused"
        return None
    for unit, data in expansion.payloads.items():
        if curated._holds_absolute_path(data):  # noqa: SLF001
            summary["problems"] = [f"the payload expanded for {unit} would hold an absolute path; nothing is accepted"]
            summary["status"] = "refused"
            return None
    derivation = {"spec": {"path": spec_name, "schema": SPEC_SCHEMA_ID, "sha256": sha256_bytes(spec_bytes),
                           "bytes": len(spec_bytes)},
                  "expander": code.identity(EXPANDER_PATH, str(expander.EXPANDER_VERSION)),
                  "context": context}
    summary["expansion"] = {"findings": 0, "counts": expansion.counts}
    return spec_bytes, expansion.payloads, derivation



def _prepare_session(session_root: Path, triage_inputs: Path, offered: list[Offered], notes: list[str], prompt: bytes,
                     ports: Ports, options: Options, summary: dict[str, Any], code_dir: Path | None = None) -> Session:
    session = build_session(session_root, ports.code, triage_inputs, offered, ports.servers, ports.claude, options.model,
                            ports.probe, options.effort, code_dir)
    (session.harness / "prompt.md").write_bytes(prompt)
    launch_plan = {"argv": session.argv, "cwd": str(session.work), "env_set": SESSION_ENV,
                   "settings": SESSION_SETTINGS, "denied_tools": session.denied, "servers": session.servers,
                   "skill_commit": ports.code.commit, "wrapper": ports.code.wrapper_identity,
                   "offered": [o.path for o in offered], "preparation_notes": notes,
                   "handoff_original_sha256": {o.unit: o.provenance.get("original_sha256")
                                               for o in offered if o.kind == "handoff"}}
    (session.harness / "launch.json").write_text(json.dumps(launch_plan, indent=1, ensure_ascii=False), encoding="utf-8")
    summary["harness_flags"] = session.portable_flags
    summary["command"] = session.argv
    return session


def _validate_after(triage_dir: Path, ports: Ports, session: Session, summary: dict[str, Any]) -> dict[str, Any]:
    """Validate the committed accession (with --emit-selection, so its verdicts enter the review log). The payloads are
    already committed: a validator that fails is reported as an error verdict, never as a failed triage."""
    report = run_validation(triage_dir, ports.code)
    (session.harness / "validator.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    summary["verdict"] = report.get("verdict")
    summary["status"] = "triaged"
    return summary


# --------------------------------------------------------------------------------------------- revisions

def _revision_number(path: Path) -> int:
    match = re.match(r"^.+\.r(?P<n>[0-9]+)\.json$", path.name)
    return int(match.group("n")) if match else 0


def revise_accession(accession: str, triage_dir: Path, session_root: Path, stamp: str, options: Options, ports: Ports,
                     summary: dict[str, Any]) -> dict[str, Any]:
    """Revise units of a triaged accession (section 2.4): each named unit's current payload is revised in one session
    because of one review-log entry (a person's answer, or a validator verdict), about that payload or one it
    supersedes. The session gets the inputs the current payload's record binds, unchanged (their sha256 checked), with
    the current spec (inputs\\spec.r<N>\\previous.spec.json), each current payload (inputs\\<unit>\\r<M>\\previous.json)
    and its cause entry as the review log holds it (cause.json), and writes the whole spec again as
    <accession>.spec.r<N>.json. Its expansion may change only the units named (X-SCOPE). Accepted payloads are committed as
    <unit>.r<M>.json with records whose supersedes is the current decision, and the review log gets one revision entry
    per unit, {supersedes, new_decision_id, cause: {kind, seq}}."""
    if ports.code.revision_template is None:
        raise WrapperError(f"the revision prompt {REVISION_TEMPLATE_PATH} is not available at the commit")
    log, broken = validator.read_review_log(triage_dir)
    if broken:
        raise WrapperError(f"{curated.REVIEW_LOG}'s chain does not hold ({broken[0]}); nothing is revised")
    lines = [line for line in (triage_dir / curated.REVIEW_LOG).read_bytes().split(b"\n") if line.strip()] if log else []
    by_seq = {entry.get("seq"): (entry, lines[index]) for index, entry in enumerate(log)}
    supersedes: dict[str, str | None] = {}
    for record_file in triage_dir.glob(f"*{validator.RECORD_SUFFIX}"):
        try:
            named = json.loads(record_file.read_bytes().decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if isinstance(named, dict) and isinstance(named.get("decision_id"), str):
            supersedes[named["decision_id"]] = named.get("supersedes")
    inputs_dir = triage_dir / "inputs"
    specs = sorted((int(m.group("n") or 0), p) for p in triage_dir.glob("*.json")
                   for m in [validator.SPEC_FILE.match(p.name)] if m and m.group("accession") == accession)
    if not specs:
        raise WrapperError(f"{accession} has no spec ({accession}.spec.json): its payloads were not expanded from one, so "
                           "they are not revised by a spec; triage the accession again in a new campaign directory")
    spec_number = specs[-1][0] + 1
    spec_name = f"{accession}.spec.r{spec_number}.json"
    current: dict[str, dict[str, Any]] = {}
    for path in validator.current_triage_files(triage_dir):
        try:
            current[validator.triage_file_unit(path)] = json.loads(path.read_bytes().decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise WrapperError(f"{path.name} is not readable JSON ({type(error).__name__})") from error
    plan: list[dict[str, Any]] = []
    offered: dict[str, Offered] = {}
    base_record: dict[str, Any] | None = None
    for unit, seq in sorted(options.revise.items()):
        path = curated.payload_path(triage_dir, unit)
        if path is None:
            raise WrapperError(f"{unit} has no payload in {accession} to revise")
        data = path.read_bytes()
        decision = sha256_bytes(data)
        if not isinstance(current.get(unit, {}).get("derivation"), dict):
            raise WrapperError(f"{path.name} was not expanded from a spec (skill 1.1.0 or earlier); a revision rewrites the "
                               "spec, so triage the accession again in a new campaign directory")
        try:
            record = json.loads(curated.record_path(path).read_bytes().decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise WrapperError(f"{path.name} has no readable decision record ({type(error).__name__}); the inputs it "
                               "rested on cannot be offered again") from error
        base_record = base_record or record
        found = by_seq.get(seq)
        if found is None:
            raise WrapperError(f"{curated.REVIEW_LOG} has no entry {seq}")
        cause, cause_line = found
        if cause.get("kind") not in ("person_answer", "validator_verdict"):
            raise WrapperError(f"review log entry {seq} is a {cause.get('kind')}; a revision is caused by a person_answer "
                               "or a validator_verdict")
        lineage = validator.lineage(decision, supersedes, log)
        if cause.get("decision_id") not in lineage:
            raise WrapperError(f"review log entry {seq} is about another decision than {path.name} or one it supersedes")
        used = [e for e in log if e.get("kind") == "revision" and isinstance(e.get("cause"), dict)
                and e["cause"].get("seq") == seq and e.get("supersedes") in lineage]
        if used:
            raise WrapperError(f"review log entry {seq} already caused a revision of {unit} (entry {used[0].get('seq')}); "
                               "one cause revises once")
        number = _revision_number(path) + 1
        if (triage_dir / f"{unit}.r{number}.json").exists():
            raise WrapperError(f"{unit}.r{number}.json exists already")
        folder = inputs_dir / unit / f"r{number}"
        if folder.exists():
            aside = long_path(options.session_root) / accession / f"superseded-{unit}-r{number}-{stamp}"
            aside.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(folder), str(aside))
            summary["notes"].append(f"{unit}: earlier r{number} inputs (no payload bound them) moved to {aside}")
        for item in record.get("inputs") or []:
            bound = str(item.get("path") or "")
            source = triage_dir / bound
            if not bound.startswith("inputs/") or not source.is_file() or sha256_bytes(source.read_bytes()) != item.get("sha256"):
                raise WrapperError(f"{path.name}'s record binds {bound}, which is not there as it was bound")
            offered.setdefault(bound, Offered(bound, "digest" if item.get("role") == "triage_digest" else "bound", None, {}))
        folder.mkdir(parents=True)
        (folder / "previous.json").write_bytes(data)
        (folder / "cause.json").write_bytes(cause_line + b"\n")
        prior = Offered(f"inputs/{unit}/r{number}/previous.json", "prior_decision", unit,
                        {"source": f"{path.name} (decision {decision})"})
        entry = Offered(f"inputs/{unit}/r{number}/cause.json", "review_entry", unit,
                        {"source": f"{curated.REVIEW_LOG} seq {seq} ({cause.get('kind')})"})
        offered[prior.path] = prior
        offered[entry.path] = entry
        plan.append({"unit": unit, "current": path, "decision": decision, "record": record, "seq": seq,
                     "kind": cause.get("kind"), "number": number, "prior": prior, "entry": entry,
                     "purpose": current[unit].get("purpose")})
    previous_spec = specs[-1][1]
    spec_folder = inputs_dir / f"spec.r{spec_number}"
    if spec_folder.exists():
        aside = long_path(options.session_root) / accession / f"superseded-spec-r{spec_number}-{stamp}"
        aside.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(spec_folder), str(aside))
    spec_folder.mkdir(parents=True)
    (spec_folder / "previous.spec.json").write_bytes(previous_spec.read_bytes())
    prior_spec = Offered(f"inputs/spec.r{spec_number}/previous.spec.json", "prior_spec", None,
                         {"source": f"{previous_spec.name} (sha256 {sha256_bytes(previous_spec.read_bytes())})"})
    offered[prior_spec.path] = prior_spec
    units = [item["unit"] for item in plan]
    summary["units"] = units
    purpose = options.analysis_purpose or next((str(p["purpose"]) for p in plan if p["purpose"]), None)
    bullets = "\n".join(
        f"- `{p['unit']}`: its current payload is `../inputs/{p['unit']}/r{p['number']}/previous.json` "
        f"({p['current'].name}), revised because of `../inputs/{p['unit']}/r{p['number']}/cause.json` (review log entry "
        f"{p['seq']}, a {p['kind']})." for p in plan)
    prompt = render_prompt(ports.code.revision_template, accession=accession, unit_ids=", ".join(units),
                           input_dir="../inputs", output_dir="../out", analysis_purpose=purpose or "null",
                           spec_name=spec_name, previous_spec=f"../{prior_spec.path}", revisions=bullets)
    for item in plan:
        (inputs_dir / item["unit"] / f"r{item['number']}" / "prompt.md").write_bytes(prompt)
    code_dir = ports.code.materialise(long_path(session_root) / "code")
    session = _prepare_session(session_root, inputs_dir, list(offered.values()), [], prompt, ports, options, summary,
                               code_dir)
    if options.dry_run:
        summary["status"] = "prepared"
        return summary
    result = run_session(session, prompt, {spec_name: SPEC_KEY}, accession, stamp, options, ports, summary)
    if result is None:
        return summary
    expanded = expand_session(session, result, code_dir, inputs_dir, spec_name, purpose, ports.code, summary,
                              revise=frozenset(units), current=current)
    if expanded is None:
        return summary
    spec_bytes, payloads, derivation = expanded
    expander = load_module(code_dir, EXPANDER_PATH)
    unchanged = [p["unit"] for p in plan if expander.comparable(json.loads(payloads[p["unit"]].decode("utf-8")))
                 == expander.comparable(current[p["unit"]])]
    if unchanged:
        summary["problems"] = [f"the revision leaves {', '.join(unchanged)} as it was; a revision changes what its cause "
                               "settles", "a revision session is accepted whole or not at all"]
        summary["status"] = "refused"
        return summary
    if base_record is not None and isinstance((base_record.get("derivation") or {}).get("digest"), dict):
        derivation["digest"] = base_record["derivation"]["digest"]
    files: list[tuple[str, bytes]] = []
    if result.transcript_file is not None:
        files.append(result.transcript_file)
    files.append((spec_name, spec_bytes))
    revised = []
    for p in plan:
        unit, number = p["unit"], p["number"]
        data = payloads[unit]
        own = [dict(item) for item in p["record"].get("inputs") or []]
        own += [_input_entry(p["prior"], "prior_decision", triage_dir, p["prior"].provenance["source"]),
                _input_entry(p["entry"], "review_entry", triage_dir, p["entry"].provenance["source"]),
                _input_entry(prior_spec, "prior_decision", triage_dir, prior_spec.provenance["source"])]
        named = {item["path"] for item in own}
        own += [_input_entry(item, "other", triage_dir, item.provenance.get("source") or None)
                for path, item in offered.items() if path not in named]
        calls, call_files = stored_calls(result.transcript, f"inputs/{unit}/r{number}/calls")
        record = build_record(unit_id=unit, accession=accession, payload_name=f"{unit}.r{number}.json", data=data,
                              supersedes=p["decision"], inputs=own, code=ports.code,
                              template_path=REVISION_TEMPLATE_PATH, template=ports.code.revision_template, prompt=prompt,
                              prompt_path=f"inputs/{unit}/r{number}/prompt.md", calls=calls, session=session,
                              result=result, derivation=derivation)
        files += [*call_files.items(), (f"{unit}.r{number}{validator.RECORD_SUFFIX}", record),
                  (f"{unit}.r{number}.json", data)]
        revised.append((p, sha256_bytes(data)))
    commit_files(triage_dir, files)
    at = now()
    for p, new_id in revised:
        curated.append_review_log(triage_dir, "revision", p["decision"], at, supersedes=p["decision"],
                                  new_decision_id=new_id, cause={"kind": p["kind"], "seq": p["seq"]})
    summary["accepted"] = [f"{p['unit']}.r{p['number']}" for p, _ in revised]
    summary["spec"] = {"path": spec_name, "bytes": len(spec_bytes)}
    return _validate_after(triage_dir, ports, session, summary)



def run_validation(triage_dir: Path, code: CodeSnapshot) -> dict[str, Any]:
    """scripts/validate-triage.py --json --emit-selection on the accession, with inputs\\ as the Catalog and inputs\\*.tsv
    as the listings: its validator_verdict entries enter the review log, so the plan's review_log_head_sha256 covers
    them. A validator that cannot run, times out or does not report is an error verdict, never a pass."""
    catalog, listings = curated.accession_sources(triage_dir)
    command = [sys.executable, "-I", str(VALIDATOR_PATH), str(triage_dir), "--json", "--emit-selection"]
    if catalog is not None:
        command += ["--catalog", str(catalog)]
    for listing in listings:
        command += ["--archive-listing", str(listing)]
    if code.root is not None:
        command += ["--code-root", str(code.root)]
    try:
        done = subprocess.run(command, capture_output=True, timeout=900, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return {"verdict": "error", "exit_code": None, "problem": f"the validator did not run: {type(error).__name__}: {error}"}
    try:
        report = json.loads(done.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"verdict": "error", "exit_code": done.returncode,
                "problem": done.stderr.decode("utf-8", "replace")[-500:]}
    expected = {"pass": 0, "hold": 1, "error": 2}.get(str(report.get("verdict")))
    if expected != done.returncode:
        report["verdict"] = "error"
    report["exit_code"] = done.returncode
    return report


def read_accessions(values: list[str] | None, file: Path | None) -> list[str]:
    found: list[str] = []
    for value in values or []:
        found += [part for part in re.split(r"[\s,]+", value) if part]
    if file is not None:
        for line in Path(file).read_text(encoding="utf-8-sig").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                found.append(line.split()[0])
    seen: list[str] = []
    folded: set[str] = set()
    for accession in found:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", accession):
            raise SystemExit(f"not an accession: {accession!r}")
        # One accession once, whatever its case: the Catalog's search folds case, and so does NTFS.
        if accession.casefold() not in folded:
            folded.add(accession.casefold())
            seen.append(accession)
    if not seen:
        raise SystemExit("name at least one accession (--accession or --accessions-file)")
    return seen


def read_revisions(values: list[str] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values or []:
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)=([1-9][0-9]*)", value.strip())
        if not match:
            raise SystemExit(f"--revise takes <unit_id>=<review log seq>, not {value!r}")
        if match.group(1) in out:
            raise SystemExit(f"--revise names {match.group(1)} twice")
        out[match.group(1)] = int(match.group(2))
    return out


def default_ports(args: argparse.Namespace) -> Ports:
    servers = load_server_configs(args.mcp_config)
    catalog_config = servers.get("msdial-repository-catalog") or {}
    database = args.catalog_database or (catalog_config.get("env") or {}).get("MSDIAL_REPOSITORY_CATALOG") \
        or os.environ.get("MSDIAL_REPOSITORY_CATALOG")
    if not database:
        raise WrapperError("no Catalog database: pass --catalog-database, or configure msdial-repository-catalog")
    interactive = args.interactive_root or os.environ.get("MSDIAL_INTERACTIVE_ROOT")
    if not interactive:
        script = next((Path(a) for a in (servers.get("msdial-interactive") or {}).get("args") or []
                       if str(a).endswith(".py")), None)
        interactive = str(script.parent.parent) if script else None
    return Ports(catalog=LocalCatalog(Path(database)), lister=InteractiveLister(Path(interactive) if interactive else None),
                 fetcher=fetch_text, servers=servers, claude=find_claude(args.claude), code=code_from_head())


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--accession", action="append")
    parser.add_argument("--accessions-file", type=Path)
    parser.add_argument("--revise", action="append", metavar="UNIT_ID=SEQ",
                        help="revise this unit of the one accession named, because of this review-log entry (a "
                             "person_answer or a validator_verdict about its current payload); repeatable")
    parser.add_argument("--dry-run", action="store_true", help="prepare the inputs, the prompt and the session; do not "
                                                                 "launch the model")
    parser.add_argument("--max-parallel", type=int, default=1)
    parser.add_argument("--model", help="model for the session (default: the CLI's default, recorded from the transcript)")
    parser.add_argument("--effort", choices=EFFORTS, help="passed to `claude --effort` (default: unset, the CLI's default); "
                                                          "recorded in the harness flags")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per session")
    parser.add_argument("--analysis-purpose", help="the person's analysis_purpose, verbatim (default: null; a revision "
                                                   "keeps the revised payload's purpose)")
    parser.add_argument("--claude", help="the claude executable (default: found on PATH)")
    parser.add_argument("--mcp-config", type=Path, help='{"mcpServers": {...}} (default: ~/.claude.json user scope)')
    parser.add_argument("--catalog-database", help="the Catalog sqlite (default: the catalog server's configuration)")
    parser.add_argument("--interactive-root", help="the MS-DIAL Interactive checkout whose listing code is used")
    parser.add_argument("--session-root", type=Path, help="where session folders go (default: <campaign dir>/triage-sessions)")
    parser.add_argument("--redact-term", action="append", default=[], help="a word removed from records (repeatable)")
    args = parser.parse_args(argv)
    if args.max_parallel < 1:
        parser.error("--max-parallel must be at least 1")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def main(argv: list[str] | None = None, *, ports: Ports | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    accessions = read_accessions(args.accession, args.accessions_file)
    revise = read_revisions(args.revise)
    if revise and len(accessions) != 1:
        raise SystemExit("--revise names units of one accession; name exactly one --accession")
    options = Options(campaign_dir=args.campaign_dir, session_root=args.session_root or args.campaign_dir / "triage-sessions",
                      dry_run=args.dry_run, model=args.model, timeout=args.timeout,
                      analysis_purpose=args.analysis_purpose, redact_terms=list(args.redact_term), revise=revise,
                      effort=args.effort)
    try:
        ports = ports or default_ports(args)
        if not options.dry_run and ports.harness is None:
            # Once, before any accession is prepared: a harness that cannot run fails the batch here, not after every
            # accession's Catalog handoffs, listings and mwTab texts.
            ports.harness = harness_version(ports.claude)
    except WrapperError as error:
        print(json.dumps([{"accession": accession, "status": "failed", "problems": [str(error)]}
                          for accession in accessions], ensure_ascii=False))
        return 3
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_parallel) as pool:
        summaries = list(pool.map(lambda a: triage_accession(a, options, ports), accessions))
    print(json.dumps(summaries, indent=1, ensure_ascii=False))
    worst = 0
    for item in summaries:
        if item["status"] in ("failed", "refused"):
            worst = max(worst, 3)
        elif item["status"] == "triaged":
            worst = max(worst, {"pass": 0, "hold": 1}.get(str(item.get("verdict")), 2))
    return worst


if __name__ == "__main__":
    sys.exit(main())
