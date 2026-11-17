"""Run the msdial-unit-triage agent on public-repository accessions and write each payload's decision record.

docs/llm-assisted-curation.md sections 2.1 to 2.3 (the "Context" paragraph and the envelope). For each accession:

1. PREPARE (read only, before download). Every Catalog analysis unit of the accession gets a Catalog handoff,
   made with msdial_catalog_reanalysis_handoff without a Class proposal, copied into
   <campaign dir>\\triage\\<accession>\\inputs\\<unit_id>\\ (handoff.json with its samples.json, files.json and
   inputs.json, the sidecar paths in the copy pointing at the copies). Every zip archive the handoffs name is listed
   from its central directory (Interactive's remote_archive.list_remote_zip, the code behind
   msdial_list_remote_archive; never a download) into inputs\\<archive>.members.tsv. For a Metabolomics Workbench
   unit, the analysis's mwTab text is fetched into inputs\\<analysis id>.mwtab.txt. A listing or an mwTab text that
   cannot be had is left out and said so; the validator then holds.
2. RENDER prompts/unit-triage-task.v1.md (at git HEAD of this repository) into inputs\\<unit_id>\\prompt.md, with the
   input and output directories relative to the session's working directory.
3. LAUNCH the Claude Code CLI headless in a clean configuration (CLEAN_FLAGS below): a fresh working directory
   outside every code checkout holding only the skill (SKILL.md at HEAD), the inputs and an empty output folder as
   sibling directories added with --add-dir, no settings files (--restricted), CLAUDE.md and auto memory disabled,
   hooks disabled, only the MCP servers the skill's allowed-tools name (--strict-mcp-config), the built-in tools
   limited to the skill's, every other tool of those servers removed (--disallowedTools, from the server's own
   tools/list), and the transcript taken as stream JSON. The session's directory is
   <campaign dir>\\triage-sessions\\<accession>\\<stamp>\\ (work, inputs, out, harness).
4. ACCEPT. A session that did not end in success, timed out, wrote anything but <unit_id>.json payloads into its
   output folder (a decision record or a review log above all: only the wrapper and the runner write those), wrote
   outside it, or changed its inputs or working directory has no payload accepted. Otherwise each payload is copied,
   byte for byte, to <campaign dir>\\triage\\<accession>\\<unit_id>.json (never over an existing file).
5. RECORD <unit_id>.record.json (msdial-llm-decision-record.v1) from the harness transcript, not from anything the
   model says about itself: every tool call in order with its arguments (secrets, e-mail addresses and the operator's
   names removed, paths portable), each result stored as inputs\\<unit_id>\\calls\\<seq>.json with its sha256, every
   input offered with its sha256 and bytes, the skill and template at the commit, the rendered prompt, the harness
   version, the model id, the flags and the configuration. What the configuration achieved is checked from the
   transcript's init message (tools, MCP servers, plugins, skills, working directory), the debug log (hooks) and the
   ancestors of the session directory (context files); anything short of clean is written as "not clean: <why>",
   which the validator holds (T-CONTEXT). The transcript itself stays in the session's harness folder, unreleased;
   the record names its sha256 (transcript.unredacted_sha256). A released redacted copy is not written yet
   (transcript.sha256 and released_path are null): the redactor that would write it, and pass it through
   scripts/check-release-redaction.py before the record names its sha256, is still to be built.
6. VALIDATE: scripts/validate-triage.py on the accession, with inputs\\ as the Catalog and inputs\\*.tsv as the
   listings. Its report is kept in the session's harness folder.

The wrapper never writes a person's answer or the review log, never calls a tool with confirmed=true, never saves a
Class, never downloads raw data and never runs MS-DIAL.

    python -I scripts/run-triage-agent.py --campaign-dir <dir> (--accession ST002283 ... | --accessions-file <file>)
        [--dry-run] [--max-parallel 1] [--model <id>] [--timeout 3600] [--analysis-purpose <text>]
        [--claude <executable>] [--mcp-config <json>] [--catalog-database <sqlite>] [--interactive-root <dir>]
        [--session-root <dir>] [--redact-term <text>]...

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
TEMPLATE_PATH = "prompts/unit-triage-task.v1.md"
VALIDATOR_PATH = SCRIPTS / "validate-triage.py"
HARNESS = "claude-code"
RECORD_SCHEMA = "msdial-llm-decision-record.v1"
SAMPLING = "harness default, not exposed"
WORKING_DIRECTORY = ("a fresh directory outside every code checkout holding only the skill "
                     f"({SKILL_PATH}); the inputs (../inputs) and the output folder (../out) are sibling directories "
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
    """The skill and the prompt template a session runs with, at one commit of this repository. `root` is a released
    code folder the validator reads in place of git (validate-triage.py --code-root), or None for git."""

    commit: str
    skill: bytes
    template: bytes
    root: Path | None = None

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


def code_from_head(repo: Path = REPO_ROOT) -> CodeSnapshot:
    """SKILL.md and the template exactly as git HEAD holds them (the bytes the record's sha256 names)."""
    def git(*args: str) -> bytes:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=60, check=False)
        if done.returncode != 0:
            raise WrapperError(f"git {' '.join(args)} failed: {done.stderr.decode('utf-8', 'replace').strip()[:200]}")
        return done.stdout

    try:
        commit = git("rev-parse", "HEAD").decode("ascii").strip()
        return CodeSnapshot(commit, git("show", f"{commit}:{SKILL_PATH}"), git("show", f"{commit}:{TEMPLATE_PATH}"))
    except (OSError, subprocess.SubprocessError) as error:
        raise WrapperError(f"git could not be run: {error}") from error


def render_prompt(template: bytes, **values: str) -> bytes:
    text = template.decode("utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    left = sorted(set(re.findall(r"\{\{([a-z_]+)\}\}", text)))
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
_DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:" + _SEP + r"(?:[^\\/\r\n\"'<>|*?:]*?" + _SEP + r")*"
                         r"[^\\/\s\"'<>|*?:,;)\]}]*")
_UNC_PATH = re.compile(r"(?<![A-Za-z0-9:])(?:\\\\\\\\|\\\\)[A-Za-z0-9._$-]+(?:" + _SEP + r"[^\\/\s\"'<>|*?:,;)\]}]+)+")


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
        cleaned = sorted({t for t in (str(x).strip() for x in terms) if len(t) >= 3}, key=lambda t: -len(t))
        return Scrubber(roots, cleaned)

    def text(self, value: str) -> str:
        out = value
        for pattern in _SECRET_VALUES:
            out = pattern.sub("<redacted>", out)
        for prefix, relative in self.roots:
            out = re.sub(re.escape(prefix) + r"(?=" + _SEP + r"|\b|$)", lambda _m, r=relative: r, out,
                         flags=re.IGNORECASE)
        out = _DRIVE_PATH.sub(lambda m: "<external>/" + _basename(m.group(0)), out)
        out = _UNC_PATH.sub(lambda m: "<external>/" + _basename(m.group(0)), out)
        out = _EMAIL.sub("<redacted-email>", out)
        for term in self.terms:
            out = re.sub(r"(?i)(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", "<redacted>", out)
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
        for part in re.split(r"[\s\\/._-]+", local):
            if len(part) >= 3:
                found.add(part)

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
    kind: str  # handoff, samples, files, analysis_inputs, listing, mwtab
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
                    (staging / f"{analysis}.mwtab.txt").write_bytes(data)
                    offered.append(Offered(f"inputs/{analysis}.mwtab.txt", "mwtab", unit_id,
                                           {"source_url": url, "retrieved_at": now()}))
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
        target.write_bytes(_listing_tsv(name, listing["members"]))
        provenance = listing.get("provenance") if isinstance(listing.get("provenance"), dict) else {}
        tool = provenance.get("tool") if isinstance(provenance.get("tool"), dict) else {}
        offered.append(Offered(f"inputs/{target.name}", "listing", None, {"retrieval": {
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


def build_session(root: Path, code: CodeSnapshot, triage_inputs: Path, offered: list[Offered],
                  servers: Mapping[str, Mapping[str, Any]], claude: list[str], model: str | None,
                  probe: Callable[[Mapping[str, Any]], dict[str, Any]]) -> Session:
    root = long_path(root)
    work, inputs, out, harness = root / "work", root / "inputs", root / "out", root / "harness"
    for folder in (work, inputs, out, harness):
        folder.mkdir(parents=True, exist_ok=False)
    skill = work / SKILL_PATH
    skill.parent.mkdir(parents=True)
    skill.write_bytes(code.skill)
    for item in offered:
        source = triage_inputs.parent / item.path
        target = root / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    allowed = code.allowed_tools
    wanted = sorted({validator.tool_server(t) for t in allowed} - {None})
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
        try:
            assert process.stdin is not None
            process.stdin.write(prompt)
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(process)
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
    hooks: list[str] = []
    registered = [int(n) for n in re.findall(r"Registered (\d+) hooks", debug_log)]
    if any(registered):
        hooks.append(f"{max(registered)} hook(s) registered (harness debug log)")
        problems.append("hooks were registered")
    context_files = ancestors[1]
    if context_files:
        problems.append(f"{len(context_files)} context file(s) lie in an ancestor of the session directory")
    loaded = {str(s.get("name")): s for s in servers}
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
        "memory": "disabled",
        "hooks": hooks,
        "mcp_servers": mcp_servers,
        "harness_flags": flags,
    }
    return context, problems


# -------------------------------------------------------------------------------------------- acceptance

def tree_digest(folder: Path) -> dict[str, str]:
    return {p.relative_to(folder).as_posix(): sha256_bytes(p.read_bytes())
            for p in sorted(folder.rglob("*")) if p.is_file()}


def acceptance_problems(session: Session, outcome: Outcome, transcript: Transcript, units: list[str],
                        inputs_before: dict[str, str], work_before: dict[str, str]) -> tuple[list[str], dict[str, Path]]:
    """(why nothing is accepted, the payloads that would be). A session is accepted whole or not at all."""
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
        match = PAYLOAD_NAME.match(relative)
        if relative.endswith(".record.json") or relative == curated.REVIEW_LOG:
            problems.append(f"the model wrote {relative}; only the wrapper writes a decision record and only the runner "
                            "the review log")
        elif match and match.group("unit") in units:
            payloads[match.group("unit")] = path
        else:
            problems.append(f"the model wrote {relative}, which is not the payload of a unit of this session")
    for call in transcript.calls:
        if validator.bare_tool(call.name) not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
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


def record_inputs(unit_id: str, triage_dir: Path, offered: list[Offered]) -> list[dict[str, Any]]:
    """Every input offered to the session, as a record binds it: the unit's own handoff and sidecars first, then the
    listings, the unit's mwTab text, and the other units' handoffs, sidecars and mwTab texts."""
    def entry(item: Offered, role: str) -> dict[str, Any]:
        data = (triage_dir / item.path).read_bytes()
        out: dict[str, Any] = {"role": role, "path": item.path, "sha256": sha256_bytes(data), "bytes": len(data)}
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
        return out

    own = [entry(i, _ROLES[i.kind]) for i in offered if i.unit == unit_id and i.kind in _ROLES]
    listings = [entry(i, "archive_listing") for i in offered if i.kind == "listing"]
    texts = [entry(i, "metadata_text") for i in offered if i.kind == "mwtab" and i.unit == unit_id]
    others = [entry(i, "other") for i in offered if i.unit not in (unit_id, None) and i.kind in _ROLES]
    other_texts = [entry(i, "metadata_text") for i in offered if i.kind == "mwtab" and i.unit not in (unit_id, None)]
    return [*own, *listings, *texts, *others, *other_texts]


def store_calls(unit_id: str, triage_dir: Path, transcript: Transcript, scrub: Scrubber) -> list[dict[str, Any]]:
    folder = triage_dir / "inputs" / unit_id / "calls"
    calls: list[dict[str, Any]] = []
    for seq, call in enumerate(transcript.calls, start=1):
        entry: dict[str, Any] = {"seq": seq, "tool": call.name, "arguments": scrub.value(call.arguments),
                                 "status": "error", "result_path": None, "result_sha256": None}
        if call.result is not None:
            stored = {"tool_use_id": call.ident, "tool": call.name, "is_error": bool(call.result.get("is_error")),
                      "content": scrub.value(call.result.get("content"))}
            data = json.dumps(stored, ensure_ascii=False, indent=1).encode("utf-8")
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{seq}.json").write_bytes(data)
            entry.update(status="error" if stored["is_error"] else "ok", result_path=f"inputs/{unit_id}/calls/{seq}.json",
                         result_sha256=sha256_bytes(data))
        calls.append(entry)
    return calls


def build_record(*, unit_id: str, accession: str, triage_dir: Path, data: bytes, code: CodeSnapshot,
                 prompt: bytes, offered: list[Offered], transcript: Transcript, outcome: Outcome, session: Session,
                 context: dict[str, Any], version: str, scrub: Scrubber) -> bytes:
    """The record's bytes for the payload `data` (to be <unit_id>.json beside it). The tool results are stored under
    inputs\\<unit_id>\\calls\\ here; nothing else is written. Refused (WrapperError) when the record would hold an
    absolute path, since it is released byte for byte."""
    try:
        document = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        document = {}
    if not isinstance(document, dict):
        document = {}
    rationale = document.get("rationale") if isinstance(document.get("rationale"), str) else ""
    record = {
        "schema": RECORD_SCHEMA,
        "decision_id": sha256_bytes(data),
        "kind": "unit_triage",
        "accession": accession,
        "unit_id": unit_id,
        "payload": {"path": f"{unit_id}.json", "schema": str(document.get("schema") or "unknown"), "sha256": sha256_bytes(data)},
        "supersedes": None,
        "inputs": record_inputs(unit_id, triage_dir, offered),
        "skill": {"name": SKILL_NAME, "version": code.skill_version, "path": SKILL_PATH,
                  "sha256": sha256_bytes(code.skill), "repo_commit": code.commit},
        "prompt": {"template_path": TEMPLATE_PATH, "template_sha256": sha256_bytes(code.template),
                   "rendered_path": f"inputs/{unit_id}/prompt.md", "rendered_sha256": sha256_bytes(prompt)},
        "agent": {"harness": HARNESS, "harness_version": version,
                  "model_id": str((transcript.init or {}).get("model") or (transcript.models[:1] or ["unknown"])[0]),
                  "sampling": SAMPLING, "session_id": str((transcript.init or {}).get("session_id") or session.session_id),
                  "context": context},
        "started_at": outcome.started_at,
        "finished_at": outcome.finished_at,
        "tool_calls": store_calls(unit_id, triage_dir, transcript, scrub),
        "transcript": {"sha256": None, "released_path": None, "unredacted_sha256": sha256_bytes(outcome.transcript),
                       "redaction": None},
        "rationale": scrub.text(rationale.strip()) or "The payload states no unit-level rationale.",
    }
    text = json.dumps(record, ensure_ascii=False, indent=1).encode("utf-8")
    if curated._holds_absolute_path(text):  # noqa: SLF001 - the release's own path rules
        raise WrapperError(f"the decision record of {unit_id} would hold an absolute path; neither it nor its payload "
                           "was written")
    return text


def commit(triage_dir: Path, unit_id: str, payload: bytes, record: bytes) -> None:
    """The payload, byte for byte, and its record beside it; neither ever over an existing file."""
    for name, data in ((f"{unit_id}.json", payload), (f"{unit_id}{validator.RECORD_SUFFIX}", record)):
        with (triage_dir / name).open("xb") as handle:
            handle.write(data)


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


@dataclass
class Options:
    campaign_dir: Path
    session_root: Path
    dry_run: bool
    model: str | None
    timeout: float
    analysis_purpose: str | None
    redact_terms: list[str]


_PREPARE_LOCK = threading.Lock()


def existing_payloads(triage_dir: Path) -> list[str]:
    return sorted(p.name for p in triage_dir.glob("*.json")
                  if not p.name.endswith(validator.RECORD_SUFFIX) and p.name != "index.json")


def triage_accession(accession: str, options: Options, ports: Ports) -> dict[str, Any]:
    summary: dict[str, Any] = {"accession": accession, "status": "failed", "dry_run": options.dry_run, "notes": []}
    triage_dir = long_path(options.campaign_dir) / "triage" / accession
    summary["triage_dir"] = str(triage_dir)
    already = existing_payloads(triage_dir) if triage_dir.is_dir() else []
    if already:
        summary["problems"] = [f"{accession} is already triaged ({', '.join(already[:5])}); a payload is immutable, and "
                               "a re-triage is a revision, which this wrapper does not write"]
        return summary
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    session_root = long_path(options.session_root) / accession / stamp
    summary["session_dir"] = str(session_root)
    ancestors = ancestor_problems(session_root.parent)
    if ancestors[0]:
        summary["problems"] = ancestors[0]
        return summary
    try:
        with _PREPARE_LOCK:
            triage_dir.mkdir(parents=True, exist_ok=True)
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
        prompt = render_prompt(ports.code.template, accession=accession, unit_ids=", ".join(units),
                               input_dir="../inputs", output_dir="../out",
                               analysis_purpose=options.analysis_purpose if options.analysis_purpose else "null")
        for unit in units:
            (final_inputs / unit).mkdir(parents=True, exist_ok=True)
            (final_inputs / unit / "prompt.md").write_bytes(prompt)
        session = build_session(session_root, ports.code, final_inputs, preparation.offered, ports.servers,
                                ports.claude, options.model, ports.probe)
        (session.harness / "prompt.md").write_bytes(prompt)
        launch_plan = {"argv": session.argv, "cwd": str(session.work), "env_set": SESSION_ENV,
                       "settings": SESSION_SETTINGS, "denied_tools": session.denied, "servers": session.servers,
                       "skill_commit": ports.code.commit, "offered": [o.path for o in preparation.offered],
                       "preparation_notes": preparation.notes,
                       "handoff_original_sha256": {o.unit: o.provenance.get("original_sha256")
                                                   for o in preparation.offered if o.kind == "handoff"}}
        (session.harness / "launch.json").write_text(json.dumps(launch_plan, indent=1, ensure_ascii=False),
                                                     encoding="utf-8")
        summary["harness_flags"] = session.portable_flags
        summary["command"] = session.argv
        if options.dry_run:
            summary["status"] = "prepared"
            return summary
        version = harness_version(ports.claude)
        inputs_before, work_before = tree_digest(session.inputs), tree_digest(session.work)
        outcome = launch(session, prompt, options.timeout)
        transcript = parse_transcript(outcome.transcript)
        summary["exit_code"] = outcome.exit_code
        summary["harness_version"] = version
        reported = str((transcript.init or {}).get("claude_code_version") or "")
        if reported and reported != version:
            summary["notes"].append(f"the transcript names harness {reported}; `--version` said {version}")
        problems, payloads = acceptance_problems(session, outcome, transcript, units, inputs_before, work_before)
        if problems:
            (session.harness / "failure.json").write_text(json.dumps({"problems": problems, "at": now()}, indent=1),
                                                          encoding="utf-8")
            summary["problems"] = problems
            summary["status"] = "refused"
            return summary
        debug = (session.harness / "debug.log").read_text(encoding="utf-8", errors="replace") \
            if (session.harness / "debug.log").is_file() else ""
        context, context_notes = assess_context(transcript, session, ancestor_problems(session.root), debug)
        summary["configuration"] = context["configuration"]
        scrub = Scrubber.for_session(session.root, operator_terms(options.redact_terms))
        summary["notes"] += transcript.problems
        built: dict[str, tuple[bytes, bytes]] = {}
        for unit in units:
            if unit not in payloads:
                summary["notes"].append(f"{unit}: the session wrote no payload")
                continue
            data = payloads[unit].read_bytes()
            built[unit] = (data, build_record(unit_id=unit, accession=accession, triage_dir=triage_dir, data=data,
                                              code=ports.code, prompt=prompt, offered=preparation.offered,
                                              transcript=transcript, outcome=outcome, session=session, context=context,
                                              version=version, scrub=scrub))
        for unit, (data, record) in built.items():
            commit(triage_dir, unit, data, record)
        summary["accepted"] = list(built)
        report = run_validation(triage_dir, ports.code)
        (session.harness / "validator.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        summary["verdict"] = report.get("verdict")
        summary["status"] = "triaged"
        return summary
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


def run_validation(triage_dir: Path, code: CodeSnapshot) -> dict[str, Any]:
    catalog, listings = curated.accession_sources(triage_dir)
    command = [sys.executable, "-I", str(VALIDATOR_PATH), str(triage_dir), "--json"]
    if catalog is not None:
        command += ["--catalog", str(catalog)]
    for listing in listings:
        command += ["--archive-listing", str(listing)]
    if code.root is not None:
        command += ["--code-root", str(code.root)]
    done = subprocess.run(command, capture_output=True, timeout=900, check=False)
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
    for accession in found:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", accession):
            raise SystemExit(f"not an accession: {accession!r}")
        if accession not in seen:
            seen.append(accession)
    if not seen:
        raise SystemExit("name at least one accession (--accession or --accessions-file)")
    return seen


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
    parser.add_argument("--dry-run", action="store_true", help="prepare the inputs, the prompt and the session; do not "
                                                                 "launch the model")
    parser.add_argument("--max-parallel", type=int, default=1)
    parser.add_argument("--model", help="model for the session (default: the CLI's default, recorded from the transcript)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per session")
    parser.add_argument("--analysis-purpose", help="the person's analysis_purpose, verbatim (default: null)")
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
    options = Options(campaign_dir=args.campaign_dir, session_root=args.session_root or args.campaign_dir / "triage-sessions",
                      dry_run=args.dry_run, model=args.model, timeout=args.timeout,
                      analysis_purpose=args.analysis_purpose, redact_terms=list(args.redact_term))
    try:
        ports = ports or default_ports(args)
    except WrapperError as error:
        print(json.dumps({"status": "failed", "problems": [str(error)]}, ensure_ascii=False))
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
