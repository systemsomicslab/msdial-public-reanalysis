"""Adapters between the state machine and the code that does the work: Interactive, the Catalog, the gate.

IN-PROCESS WHERE THE AGENT PATH IS IN-PROCESS, HTTP WHERE IT IS HTTP. The runner imports
msdial_app.mcp_server and calls its tool functions directly: in the installed MCP SDK
MCPServer().tool()(f) returns f, so every guard an agent's call meets (handoff consistency, download
blockers, the campaign-authorization check, the ok:false result shapes) is the same code here
(tests/test_campaign_contract.py pins this). Those tool functions already split the work the way the
agent path does. The download, the split, the diagnostic, the production run, QA and publication are
jobs of the standalone Interactive backend and reach it over HTTP on the campaign's own port; the
raw-header preflight, the analysis CSV and the raw cleanup run in the calling process, as they do in
the MCP process.

ONE MANIFEST WRITER PER STEP (review contradiction 12). A unit's run-manifest.json is written by the
backend while its job runs and by this process for the in-process steps, never both at once: the
machine calls an in-process step only when the unit has no live job, and Interactive's manifest writes
are atomic and locked (repository_reanalysis.update_manifest). Split-parent release is triggered here
only; the server's post-run hook records a pending plan under a campaign.

THE CAMPAIGN AUTHORIZATION IS PASSED, NEVER confirmed=True. Every entry point that would otherwise need
confirmed=true is given campaign_authorization_path, and Interactive validates the approval and writes
the crossing into the unit manifest before it acts. The one exception is written out in
InteractivePort.discard: until Interactive exposes an authorized discard (plan item 14), the port makes
the same check msdial_cleanup_repository_raw makes for a cleanup - campaign_authorization.authorize
for boundary 5, then record_campaign_authorization - before it calls discard_download_lease.

Fake ports with the same methods drive tests/test_campaign_machine.py and test_campaign_resume.py; no
test downloads anything or starts a Console.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import policy

RESOURCES_SCHEMA = "msdial-campaign-resources.v1"
GATE_SCRIPT = Path(__file__).resolve().parents[1] / "verify-run-invariants.py"
GATE_ROOT = Path(__file__).resolve().parents[2]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sha256_file(path: str | Path, chunk: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def write_json_atomic(path: str | Path, value: Any, *, canonical: bool = False) -> Path:
    """Write JSON through a temporary file and os.replace, so a crash leaves the old file or the new one."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (
        policy.canonical_json(value)
        if canonical
        else (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    temporary = target.with_name(target.name + f".{os.getpid()}.tmp")
    with open(temporary, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    return target


def git_state(root: str | Path) -> dict[str, Any]:
    """HEAD and whether tracked files differ from it. Untracked files do not make a checkout dirty."""
    def git(*arguments: str) -> str:
        # --no-optional-locks: reading a checkout's state must not rewrite its index.
        completed = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), *arguments], capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or f"git {' '.join(arguments)} failed")
        return completed.stdout.strip()

    try:
        return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain", "--untracked-files=no"))}
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        return {"commit": "", "dirty": None, "error": str(error)}


# ---- the local resource map ------------------------------------------------------------------------

def load_resources(path: str | Path) -> dict[str, Any]:
    """The git-ignored map from a library's file name to where it is on this machine.

    Its only readers are the plan (to hash each library once) and the runner (to hand Interactive the
    files it opens). It is never copied: the manifest, the ledger, the authorization record and every
    unit's provenance name libraries by file name and sha256 only.
    """
    location = Path(path).expanduser()
    try:
        record = json.loads(location.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ValueError(f"The resource map {location.name} could not be read: {error}") from error
    if not isinstance(record, dict) or record.get("schema") != RESOURCES_SCHEMA:
        raise ValueError(f"The resource map's schema is not {RESOURCES_SCHEMA!r}.")
    libraries = record.get("libraries")
    if not isinstance(libraries, dict) or not libraries:
        raise ValueError("The resource map names no library.")
    resolved: dict[str, str] = {}
    for name, value in libraries.items():
        library = Path(str(value)).expanduser()
        if library.name != name:
            raise ValueError(f"Library {name!r} is mapped to a file of another name; name a library by its file name.")
        if not library.is_file():
            raise ValueError(f"Library {name} is not a file where the resource map says it is.")
        resolved[name] = str(library.resolve())
    return {"libraries": resolved}


# ---- pins ------------------------------------------------------------------------------------------

def _binary_inventory(directory: Path, digest: Callable[[str], tuple[str, int]] | None = None) -> str:
    """sha256 over every assembly under a Console folder: relative path, size and content digest.

    Assemblies only, so a log the Console leaves beside itself does not read as a changed pin.
    """
    entries = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".dll", ".exe"}:
            sha, size = digest(str(path)) if digest else (sha256_file(path), path.stat().st_size)
            entries.append({"path": path.relative_to(directory).as_posix(), "size": size, "sha256": sha})
    return hashlib.sha256(policy.canonical_json(entries)).hexdigest()


class PinReader:
    """Reads the identities a campaign pins, the same way at plan time and before every unit.

    Nothing here starts the Console or the extractor: their identity is their bytes (and, for the
    extractor, the build record Interactive's raw_metadata_extractor module verifies).
    """

    def __init__(
        self,
        *,
        console_path: str,
        extractor_path: str,
        libraries: Mapping[str, str],
        interactive_root: Path | None = None,
        catalog_root: Path | None = None,
        gate_root: Path = GATE_ROOT,
    ) -> None:
        self.console_path = str(console_path)
        self.extractor_path = str(extractor_path)
        self.libraries = dict(libraries)
        self.interactive_root = interactive_root
        self.catalog_root = catalog_root
        self.gate_root = gate_root
        self._hash_cache: dict[str, tuple[tuple[int, int], str]] = {}

    def _cached_sha256(self, path: str) -> tuple[str, int]:
        stat = os.stat(path)
        key = (stat.st_size, stat.st_mtime_ns)
        cached = self._hash_cache.get(path)
        if cached and cached[0] == key:
            return cached[1], stat.st_size
        digest = sha256_file(path)
        self._hash_cache[path] = (key, digest)
        return digest, stat.st_size

    def console(self) -> dict[str, Any]:
        from msdial_app.workflow import console_assembly_path

        path = Path(self.console_path).expanduser().resolve()
        if not path.is_file():
            return {"path": str(path), "exists": False}
        assembly = console_assembly_path(path).resolve()
        record: dict[str, Any] = {
            "path": str(path),
            "exists": True,
            "binary_sha256": self._cached_sha256(str(path))[0],
            "assembly_sha256": self._cached_sha256(str(assembly))[0],
            # Re-read before every unit, so each file is hashed again only when its size or time moved.
            "inventory_sha256": _binary_inventory(path.parent, self._cached_sha256),
        }
        sidecar = path.parent / "msdial-console-build-provenance.json"
        if sidecar.is_file():
            try:
                recorded = json.loads(sidecar.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                recorded = {}
            record["recorded_git_head"] = str(recorded.get("git_head") or "")
            record["provenance_matches"] = recorded.get("binary_sha256") == record["assembly_sha256"]
        return record

    def extractor(self) -> dict[str, Any]:
        from msdial_app.raw_metadata_extractor import inspect_raw_metadata_extractor

        inspection = inspect_raw_metadata_extractor(self.extractor_path)
        keys = (
            "path", "exists", "binary_sha256", "inventory_sha256", "provenance_status", "pinned",
            "msrawdataworkbench_commit", "msdialworkbench_commit", "product_version",
        )
        return {key: inspection.get(key) for key in keys if key in inspection}

    def library_identities(self) -> list[dict[str, Any]]:
        result = []
        for name, path in sorted(self.libraries.items()):
            digest, size = self._cached_sha256(path)
            result.append({"name": name, "sha256": digest, "bytes": size})
        return result

    def code(self) -> dict[str, Any]:
        import msdial_app
        import msdial_repository_catalog

        interactive_root = self.interactive_root or Path(msdial_app.__file__).resolve().parents[1]
        catalog_root = self.catalog_root or Path(msdial_repository_catalog.__file__).resolve().parents[2]
        return {
            "interactive": {"version": msdial_app.__version__, **git_state(interactive_root)},
            "catalog": {"version": getattr(msdial_repository_catalog, "__version__", ""), **git_state(catalog_root)},
            "gate": git_state(self.gate_root),
        }

    def current(self) -> dict[str, Any]:
        return {
            "console": self.console(),
            "extractor": self.extractor(),
            "libraries": self.library_identities(),
            **self.code(),
        }


# ---- Interactive -----------------------------------------------------------------------------------

def _exception_result(error: BaseException, tool: str) -> dict[str, Any]:
    """What a tool raised past its own structured-error wrapper (a RuntimeError, say), as ok:false."""
    return {
        "ok": False,
        "reason": "exception",
        "error_type": type(error).__name__,
        "detail": str(error) or type(error).__name__,
        "tool": tool,
    }


class InteractivePort:
    """The Interactive entry points the machine uses, on one campaign backend (host, port)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8766) -> None:
        from msdial_app import campaign_authorization, mcp_server, repository_reanalysis

        self.tools = mcp_server
        self.rr = repository_reanalysis
        self.ca = campaign_authorization
        self.host = host
        self.port = int(port)

    # A step that names a boundary passes the approval, and a tool that cannot take one is a contract
    # mismatch, never a quiet fallback to asking for a confirmation that will not come.
    def _call(self, name: str, *, optional: Mapping[str, Any] | None = None, **arguments: Any) -> dict[str, Any]:
        function = getattr(self.tools, name, None)
        if function is None:
            return {"ok": False, "reason": "unsupported", "detail": f"Interactive has no tool {name}.", "tool": name}
        accepted = inspect.signature(function).parameters
        for key, value in (optional or {}).items():
            if key in accepted:
                arguments[key] = value
        if "host" in accepted:
            arguments.setdefault("host", self.host)
        if "port" in accepted:
            arguments.setdefault("port", self.port)
        missing = [key for key in arguments if key not in accepted]
        if missing:
            return {
                "ok": False, "reason": "unsupported", "tool": name,
                "detail": f"{name} takes no {', '.join(missing)}; this Interactive predates the campaign contract.",
            }
        try:
            result = function(**arguments)
        except Exception as error:  # noqa: BLE001 - every escape is reported, never raised into the loop
            return _exception_result(error, name)
        return result if isinstance(result, dict) else {"ok": False, "reason": "malformed", "detail": repr(result)}

    def capabilities(self) -> dict[str, Any]:
        import msdial_app

        discard = inspect.signature(self.rr.discard_download_lease).parameters
        return {
            "version": msdial_app.__version__,
            "classify_preflight": hasattr(self.rr, "classify_preflight"),
            "authorized_discard": bool({"campaign_authorization", "campaign_authorization_path"} & set(discard)),
            "split_parent_release": hasattr(self.rr, "cleanup_split_parent"),
            "cancel_job": hasattr(self.tools, "msdial_cancel_job"),
        }

    def download(
        self, *, repository: str, accession: str, workspace_root: str, maximum_gb: float,
        raw_retention_policy: str, handoff_path: str, analysis_purpose: str, authorization_path: str,
    ) -> dict[str, Any]:
        result = self._call(
            "msdial_download_repository_raw",
            repository=repository, accession=accession, workspace_root=workspace_root,
            maximum_gb=float(maximum_gb), raw_retention_policy=raw_retention_policy, allow_preflight=True,
            confirmed=False, analysis_unit_handoff_path=handoff_path, analysis_purpose=analysis_purpose,
            campaign_authorization_path=authorization_path,
        )
        if result.get("ok") is False:
            return result
        if result.get("started") and result.get("job_id"):
            return {"ok": True, "job_id": str(result["job_id"]), "campaign_authorization": result.get("campaign_authorization")}
        preview = result.get("preview") or {}
        if result.get("blocked"):
            return {"ok": False, "reason": "blocked", "blocking_reasons": list(preview.get("blocking_reasons") or [])}
        return {"ok": False, "reason": "not_authorized", "detail": str(result.get("message") or "The download did not start.")}

    def job(self, job_id: str) -> dict[str, Any]:
        result = self._call("msdial_interactive_job", job_id=job_id, detail=True, log_lines=20)
        if result.get("ok") is False:
            if result.get("http_status") == 404:
                return {"ok": False, "reason": "job_not_found", "detail": result.get("detail")}
            return result
        result.pop("logs", None)
        return {"ok": True, **result}

    def cancel(self, job_id: str, reason: str) -> dict[str, Any]:
        return self._call("msdial_cancel_job", job_id=job_id, reason=reason)

    def read_manifest(self, manifest_path: str) -> dict[str, Any] | None:
        path = Path(str(manifest_path or ""))
        if not str(manifest_path or "").strip() or not path.is_file():
            return None
        try:
            return self.rr.read_manifest(path)
        except (OSError, ValueError):
            return None

    def preflight(self, *, manifest_path: str, extractor_path: str, authorization_path: str) -> dict[str, Any]:
        # The extractor has no outer limit here: Interactive's own per-chunk limits are authoritative.
        return self._call(
            "msdial_repository_raw_metadata_preflight",
            manifest_path=manifest_path, extractor_path=extractor_path, max_inputs=0, confirm_untargeted=False,
            optional={"campaign_authorization_path": authorization_path},
        )

    def split(self, *, manifest_path: str, authorization_path: str) -> dict[str, Any]:
        return self._call(
            "msdial_split_repository_unit", manifest_path=manifest_path, confirmed=False,
            campaign_authorization_path=authorization_path,
        )

    def prepare_metadata(self, *, manifest_path: str, authorization_path: str) -> dict[str, Any]:
        return self._call(
            "msdial_prepare_repository_reanalysis", manifest_path=manifest_path, confirmed=False,
            allow_partial_mapping=False, campaign_authorization_path=authorization_path,
        )

    def start_diagnostic(
        self, *, input_path: str, answers: dict[str, Any], authorization_path: str,
        timeout_seconds: float, idle_timeout_seconds: float,
    ) -> dict[str, Any]:
        return self._call(
            "msdial_start_peak_count_diagnostic", input_path=input_path, answers=answers, representative_file="",
            confirmed=False, campaign_authorization_path=authorization_path,
            timeout_seconds=float(timeout_seconds), idle_timeout_seconds=float(idle_timeout_seconds),
        )

    def estimate(self, *, job_id: str, manifest_path: str, minimum: int, maximum: int, step: int) -> dict[str, Any]:
        return self._call(
            "msdial_estimate_peak_height", job_id=job_id, target_peak_count_min=int(minimum),
            target_peak_count_max=int(maximum), threshold_step=int(step), manifest_path=manifest_path,
        )

    def prepare_guided(self, *, input_path: str, answers: dict[str, Any]) -> dict[str, Any]:
        return self._call("msdial_prepare_guided_analysis", input_path=input_path, answers=answers)

    def start_run(
        self, *, input_path: str, answers: dict[str, Any], authorization_path: str,
        timeout_seconds: float, idle_timeout_seconds: float,
    ) -> dict[str, Any]:
        return self._call(
            "msdial_start_guided_analysis", input_path=input_path, answers=answers, confirmed=False,
            campaign_authorization_path=authorization_path, timeout_seconds=float(timeout_seconds),
            idle_timeout_seconds=float(idle_timeout_seconds),
        )

    def qa(self, *, manifest_path: str) -> dict[str, Any]:
        return self._call("msdial_generate_lcms_qa", manifest_path=manifest_path)

    def publication(self, *, manifest_path: str, run_qa: bool) -> dict[str, Any]:
        return self._call("msdial_generate_publication_report", manifest_path=manifest_path, run_qa=bool(run_qa))

    def data_handoff(self, *, job_id: str) -> dict[str, Any]:
        return self._call("msdial_interactive_create_handoff", job_id=job_id)

    def cleanup(self, *, manifest_path: str, authorization_path: str) -> dict[str, Any]:
        result = self._call(
            "msdial_cleanup_repository_raw", manifest_path=manifest_path, confirmed=False,
            campaign_authorization_path=authorization_path,
        )
        if result.get("ok") is False:
            return result
        if result.get("deleted"):
            return {"ok": True, "deleted": True, "detail": result.get("raw_directory")}
        return {"ok": True, "deleted": False, "blockers": list(result.get("blockers") or []), "detail": result.get("message")}

    def discard(
        self, *, manifest_path: str, authorization_path: str, unit_id: str, parent_unit_id: str = "",
    ) -> dict[str, Any]:
        """Delete the raw data of a unit that produced no validated output, under boundary 5."""
        path = Path(manifest_path)
        parameters = inspect.signature(self.rr.discard_download_lease).parameters
        try:
            if "campaign_authorization_path" in parameters:
                result = self.rr.discard_download_lease(path, campaign_authorization_path=authorization_path)
            elif "campaign_authorization" in parameters:
                result = self.rr.discard_download_lease(path, campaign_authorization=authorization_path)
            else:
                manifest = self.rr.read_manifest(path)
                crossing = self.ca.authorize(
                    authorization_path, unit_id, 5, entry_point="campaign_runner.discard",
                    parent_unit_id=parent_unit_id,
                    raw_retention_policy=str(manifest.get("raw_retention_policy") or "keep"),
                )
                self.rr.record_campaign_authorization(path, crossing or {})
                result = self.rr.discard_download_lease(path, confirmed=True)
        except self.ca.CampaignAuthorizationError as error:
            return {"ok": False, "reason": "campaign_authorization_refused", "codes": list(error.codes), "detail": str(error)}
        except Exception as error:  # noqa: BLE001
            return _exception_result(error, "discard_download_lease")
        return {"ok": True, "deleted": bool(result.get("deleted")), "detail": result.get("raw_directory")}

    def release_split_parent(self, *, manifest_path: str, authorization_path: str) -> dict[str, Any]:
        function = getattr(self.rr, "cleanup_split_parent", None)
        if function is None:
            return {"ok": False, "reason": "unsupported", "detail": "Interactive has no split-parent release yet (plan item 14)."}
        parameters = inspect.signature(function).parameters
        key = "campaign_authorization_path" if "campaign_authorization_path" in parameters else "campaign_authorization"
        try:
            result = function(Path(manifest_path), **{key: authorization_path})
        except Exception as error:  # noqa: BLE001
            return _exception_result(error, "cleanup_split_parent")
        return {"ok": True, "deleted": bool((result or {}).get("deleted")), "detail": result}

    def live_attempt(self, manifest_path: str) -> dict[str, Any] | None:
        """The unit's run attempt whose Console may still be running, read from its manifest."""
        if not manifest_path or not Path(manifest_path).is_file():
            return None
        return self.rr.live_run_attempt(manifest_path)

    def lease_state(self, manifest: Mapping[str, Any]) -> str:
        """alive, gone or unknown, for a lease its manifest records as downloading."""
        return str(self.rr.lease_owner_state(dict(manifest)).get("state") or "unknown")

    def backend_alive(self, attempt: Mapping[str, Any]) -> bool | None:
        """Whether the backend process that opened a run attempt is still running."""
        from msdial_app.process_liveness import process_is_alive

        backend = attempt.get("backend") or {}
        return process_is_alive(backend.get("pid"), backend.get("process_created_at"))

    def kill_orphan(self, attempt: Mapping[str, Any]) -> bool:
        """Stop a Console whose backend is gone, if its recorded process is still that Console."""
        pid = attempt.get("console_pid")
        if not pid:
            return False
        return kill_process_tree(int(pid), attempt.get("console_process_created_at"))


# ---- the Catalog -----------------------------------------------------------------------------------

def read_only_catalog(database: str | Path):
    """A Catalog on a read-only connection: no schema is created or migrated, nothing can be written.

    Catalog(path) migrates the schema on its first query; a plan must leave the catalog as it found it.
    """
    from msdial_repository_catalog.schema import SCHEMA_VERSION
    from msdial_repository_catalog.storage import Catalog

    class ReadOnlyCatalog(Catalog):
        def __init__(self, path: Path) -> None:  # noqa: D401 - replaces the writing constructor
            self.path = Path(path).expanduser().resolve()
            if not self.path.is_file():
                raise FileNotFoundError(f"No catalog database at {self.path}")
            self.connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA query_only = ON")
            self.fts_enabled = False

        def initialize(self) -> None:
            row = self.connection.execute("SELECT version FROM schema_info LIMIT 1").fetchone()
            if row is None or int(row[0]) > SCHEMA_VERSION:
                raise RuntimeError(f"Catalog schema {row[0] if row else '?'} cannot be read by this Catalog code.")

    return ReadOnlyCatalog(Path(database))


def decide_class(catalog: Any, unit_id: str, purpose: str) -> dict[str, Any]:
    """The Class decision the Catalog makes for a unit: a proposal from a declared factor, else its
    abstention. The plan pins its proposal id as the unit's approved Class digest; the runner asks again
    before saving and stops the unit if the answer changed. The same function answers both times."""
    from msdial_repository_catalog import class_selection

    unit = catalog.get_unit(unit_id)
    proposal, decision = class_selection.automatic_class_proposal(unit, purpose)
    if proposal is not None:
        return {
            "kind": "proposal", "proposal_id": proposal.proposal_id, "selected_fields": list(proposal.selected_fields),
            "assignment_count": len(proposal.assignments), "reason": decision.get("reason", ""),
        }
    record, decision = class_selection.abstention_record(unit, purpose)
    if record is None:
        raise RuntimeError(f"The Catalog neither proposed nor abstained for unit {unit_id}.")
    return {
        "kind": "abstention", "proposal_id": record.proposal_id, "selected_fields": [],
        "assignment_count": len(record.assignments), "reason": decision.get("reason", ""),
    }


def copy_handoff(response: Mapping[str, Any], destination: Path) -> dict[str, Any]:
    """Copy a Catalog handoff and its sidecars into the campaign, and point the copy at the copies.

    The Catalog rewrites catalog-data\\handoffs\\<unit>.json and its sidecars on every call, so the file
    Interactive reads later must be the campaign's own copy of what was approved (review
    contradiction 16). Returns the copy's path and the sha256 of every original and copy.
    """
    destination.mkdir(parents=True, exist_ok=True)
    original = Path(str(response["handoff_path"]))
    payload = json.loads(original.read_text(encoding="utf-8-sig"))
    record: dict[str, Any] = {"original_sha256": sha256_file(original), "sidecars": {}}
    for key, name in (
        ("file_manifest_path", "files.json"),
        ("sample_table_path", "samples.json"),
        ("analysis_input_manifest_path", "inputs.json"),
    ):
        source = Path(str(payload.get(key) or response.get(key) or ""))
        if not str(payload.get(key) or "").strip() or not source.is_file():
            continue
        target = destination / name
        shutil.copyfile(source, target)
        payload[key] = str(target.resolve())
        record["sidecars"][key] = {"original_sha256": sha256_file(source), "copy_sha256": sha256_file(target)}
    copy = write_json_atomic(destination / "handoff.json", payload)
    record["copy_path"] = str(copy.resolve())
    record["copy_sha256"] = sha256_file(copy)
    return record


class CatalogPort:
    def __init__(self, database: str | Path) -> None:
        from msdial_repository_catalog import campaign_lock
        from msdial_repository_catalog import mcp_server as tools

        self.database = str(Path(database).expanduser().resolve())
        self.tools = tools
        self.lock_module = campaign_lock

    def class_decision(self, unit_id: str, purpose: str) -> dict[str, Any]:
        """The Class the Catalog would record for a unit now: a declared factor, else its abstention."""
        catalog = read_only_catalog(self.database)
        try:
            return decide_class(catalog, unit_id, purpose)
        finally:
            catalog.close()

    def save_class(self, *, unit_id: str, purpose: str, kind: str, ratification: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self.tools.msdial_catalog_save_class_proposal(
                unit_id=unit_id, purpose=purpose, selected_fields=[], assignments_json="", rationale="",
                confirmed=False, database=self.database, abstain=(kind == "abstention"), ratification=ratification,
            )
        except Exception as error:  # noqa: BLE001
            return _exception_result(error, "msdial_catalog_save_class_proposal")
        if result.get("saved"):
            proposal = result.get("proposal") or {}
            return {"ok": True, "proposal_id": str(proposal.get("proposal_id") or ""), "ratification": result.get("ratification")}
        if result.get("ratification_refused"):
            return {"ok": False, "reason": "campaign_authorization_refused", "codes": result["ratification_refused"],
                    "detail": result.get("message")}
        return {"ok": False, "reason": "not_saved", "detail": result.get("message") or "The Class decision was not saved."}

    def handoff(self, *, unit_id: str, class_proposal_id: str) -> dict[str, Any]:
        try:
            result = self.tools.msdial_catalog_reanalysis_handoff(
                unit_id=unit_id, class_proposal_id=class_proposal_id, database=self.database
            )
        except Exception as error:  # noqa: BLE001
            return _exception_result(error, "msdial_catalog_reanalysis_handoff")
        return {"ok": True, **result}

    def record_run(self, **values: Any) -> dict[str, Any]:
        try:
            result = self.tools.msdial_catalog_record_analysis_run(database=self.database, **values)
        except Exception as error:  # noqa: BLE001
            return _exception_result(error, "msdial_catalog_record_analysis_run")
        if result.get("recorded"):
            return {"ok": True, **result}
        return {"ok": False, "reason": "not_recorded", "detail": result.get("message")}

    def copy_handoff(self, response: Mapping[str, Any], destination: Path) -> dict[str, Any]:
        return copy_handoff(response, destination)

    def lock(self, approval_id: str, campaign_id: str) -> dict[str, Any]:
        """Hold the catalog for the campaign; release a lock this approval left when its runner died."""
        status = self.lock_module.campaign_lock_status(self.database)
        if status.get("locked") and status.get("approval_id") == approval_id and status.get("owner") == "dead":
            self.lock_module.release_campaign_lock(self.database, approval_id)
        return self.lock_module.acquire_campaign_lock(self.database, approval_id, campaign_id=campaign_id)

    def unlock(self, approval_id: str) -> dict[str, Any]:
        return self.lock_module.release_campaign_lock(self.database, approval_id)


# ---- the gate --------------------------------------------------------------------------------------

GATE_STAGES = {"before_production": "before-production", "pre_cleanup": "all", "final": "all"}


class GatePort:
    """Runs verify-run-invariants.py --strict --json as a subprocess and keeps its report.

    It never runs record-reading.py. Boundary 6, a person's reading of the sentences a check left for
    them, is never a runner's: exit 4 with READ-1 held is a state the ledger stores, not one it clears.
    """

    def __init__(self, *, python: str = sys.executable, script: Path = GATE_SCRIPT, timeout: float = 1200.0,
                 gate_commit: str = "") -> None:
        self.python = python
        self.script = script
        self.timeout = timeout
        self.gate_commit = gate_commit or git_state(GATE_ROOT).get("commit", "")

    def command(self, workspace: str, point: str) -> list[str]:
        return [self.python, str(self.script), str(workspace), "--stage", GATE_STAGES[point], "--strict", "--json"]

    def run(self, workspace: str, point: str, report_path: Path) -> dict[str, Any]:
        verdict: dict[str, Any] = {"stage": GATE_STAGES[point], "strict": True, "gate_commit": self.gate_commit}
        try:
            completed = subprocess.run(
                self.command(workspace, point), capture_output=True, timeout=self.timeout,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired:
            return {**verdict, "outcome": "timeout", "detail": f"The gate did not finish within {self.timeout:g} s."}
        except OSError as error:
            return {**verdict, "outcome": "error", "detail": str(error)}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_bytes(completed.stdout)
        verdict.update(
            outcome="ran" if completed.returncode in (0, 2, 3, 4) else "error",
            exit_code=completed.returncode if completed.returncode in (0, 2, 3, 4) else None,
            report_path=str(report_path),
            report_sha256=hashlib.sha256(completed.stdout).hexdigest(),
        )
        try:
            report = json.loads(completed.stdout.decode("utf-8"))
        except ValueError:
            verdict["detail"] = completed.stderr.decode("utf-8", errors="replace")[-2000:]
            return verdict
        checks = report.get("checks") or []
        verdict["fail_ids"] = sorted({item["check_id"] for item in checks if item.get("status") == "FAIL"})
        verdict["warn_ids"] = sorted({item["check_id"] for item in checks if item.get("status") == "WARN"})
        verdict["strict_hold_ids"] = list(report.get("strict_failures") or [])
        verdict["stage_reached"] = (report.get("progress") or {}).get("stage_reached")
        return verdict


# ---- the disk, the clock -----------------------------------------------------------------------------

class LocalDisk:
    def usage(self, path: str) -> tuple[int, int]:
        target = Path(path)
        while not target.exists() and target.parent != target:
            target = target.parent
        usage = shutil.disk_usage(target)
        return int(usage.free), int(usage.total)

    def tree_bytes(self, path: str) -> int:
        total = 0
        root = str(path)
        if os.name == "nt" and not root.startswith("\\\\?\\"):
            root = "\\\\?\\" + str(Path(path).resolve())
        for directory, _folders, files in os.walk(root, onerror=lambda _error: None):
            for name in files:
                try:
                    total += os.stat(os.path.join(directory, name), follow_symlinks=False).st_size
                except OSError:
                    continue
        return total


class SystemClock:
    def now(self) -> datetime:
        return _now()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, float(seconds)))


# ---- the campaign backend ----------------------------------------------------------------------------

class BackendSupervisor:
    """The standalone Interactive backend the campaign's jobs run in, on a port of its own.

    A port and a job registry of its own (MSDIAL_INTERACTIVE_JOBS_FILE) keep an interactive Claude
    session from overwriting the campaign's job records or restarting its backend. It is started
    detached, so a runner that stops leaves its Console running: the runner reattaches to the job when
    it comes back, instead of orphaning a run hours from its end.

    The backend's own output stream is discarded unless log_directory is given (run --backend-log). It
    is Interactive's, not the runner's: an uncaught traceback there can quote a file location, and no
    private library location may reach a log the runner writes. What each job did stays in the job
    registry and the unit manifest.
    """

    def __init__(
        self, *, python: str, interactive_root: Path, host: str, port: int, jobs_file: Path,
        workspace_root: str, log_directory: Path | None = None,
    ) -> None:
        self.python = python
        self.interactive_root = Path(interactive_root)
        self.host = host
        self.port = int(port)
        self.jobs_file = Path(jobs_file)
        self.workspace_root = str(workspace_root)
        self.log_directory = Path(log_directory) if log_directory is not None else None
        self.start_failures = 0

    def command(self) -> list[str]:
        return [self.python, str(self.interactive_root / "app.py"), "--host", self.host, "--port", str(self.port), "--no-browser"]

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        environment = dict(os.environ if base is None else base)
        environment["MSDIAL_INTERACTIVE_JOBS_FILE"] = str(self.jobs_file)
        environment["MSDIAL_REPOSITORY_WORKSPACE_ROOT"] = self.workspace_root
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return environment

    def config(self) -> dict[str, Any] | None:
        try:
            with urllib.request.urlopen(f"http://{self.host}:{self.port}/api/config", timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def check(self, config: Mapping[str, Any]) -> list[str]:
        """Why the listener on the campaign port is not this campaign's backend, if it is not."""
        import msdial_app

        problems = []
        if str(config.get("app_version") or "") != msdial_app.__version__:
            problems.append(
                f"it runs Interactive {config.get('app_version')!r} and the runner imported {msdial_app.__version__!r}"
            )
        root = str(config.get("root") or "")
        if root and Path(root).resolve() != self.interactive_root.resolve():
            problems.append(f"it runs from another checkout ({root})")
        return problems

    def ensure(self) -> dict[str, Any]:
        config = self.config()
        if config is not None:
            problems = self.check(config)
            return {"ok": not problems, "started": False, "detail": "; ".join(problems)}
        if self.start_failures >= 3:
            return {"ok": False, "started": False, "detail": "The campaign backend could not be started three times."}
        self.jobs_file.parent.mkdir(parents=True, exist_ok=True)
        if self.log_directory is not None:
            self.log_directory.mkdir(parents=True, exist_ok=True)
            log: Any = open(self.log_directory / f"backend-{_now().strftime('%Y%m%dT%H%M%SZ')}.log", "ab")
        else:
            log = open(os.devnull, "ab")
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            process = subprocess.Popen(
                self.command(), cwd=str(self.interactive_root), env=self.environment(), stdout=log,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True,
            )
        except OSError as error:
            self.start_failures += 1
            return {"ok": False, "started": False, "detail": str(error)}
        finally:
            log.close()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            config = self.config()
            if config is not None:
                problems = self.check(config)
                self.start_failures = 0 if not problems else self.start_failures + 1
                return {"ok": not problems, "started": True, "pid": process.pid, "detail": "; ".join(problems)}
            if process.poll() is not None:
                break
            time.sleep(1)
        self.start_failures += 1
        return {"ok": False, "started": True, "pid": process.pid, "detail": "The backend did not answer within 60 s."}


def kill_process_tree(pid: int, created_at: float | None = None) -> bool:
    """Stop an orphaned Console and everything it started, if it is still the process recorded.

    Windows reuses process ids, so the creation time the run attempt recorded must match.
    """
    try:
        import psutil
    except ImportError:
        return False
    try:
        process = psutil.Process(int(pid))
        if created_at is not None and abs(process.create_time() - float(created_at)) > 1.0:
            return False
        children = process.children(recursive=True)
        for child in children:
            child.kill()
        process.kill()
        psutil.wait_procs([process, *children], timeout=10)
        return True
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, OSError):
        return False


def runner_process_identity() -> tuple[int, float | None, str]:
    from msdial_app.process_liveness import process_created_at

    return os.getpid(), process_created_at(), socket.gethostname()


def runner_alive(record: Mapping[str, Any], now: datetime, stale_seconds: float) -> bool:
    """Whether a recorded runner still holds the campaign: its process lives and its heartbeat is fresh.

    One liveness helper for the store lock and the runner lock (review correction 13):
    msdial_app.process_liveness, which reads psutil or OpenProcess with the process creation time.
    """
    from msdial_app.process_liveness import process_is_alive

    heartbeat = policy.parse_iso(record.get("heartbeat_at"))
    if heartbeat is None or (now - heartbeat).total_seconds() > stale_seconds:
        return False
    if record.get("host") and record.get("host") != socket.gethostname():
        return True
    return process_is_alive(record.get("pid"), record.get("process_created_at")) is not False

