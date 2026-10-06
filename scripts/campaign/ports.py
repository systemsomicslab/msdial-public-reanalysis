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
InteractivePort.discard: until Interactive exposes an authorized discard (plan item 14, found by its
signature the day it lands), the port makes the same check msdial_cleanup_repository_raw makes for a
cleanup - campaign_authorization.authorize for boundary 5, then record_campaign_authorization - before it
calls discard_download_lease, and only once discard_download_lease's own refusals are known not to
apply, as the cleanup records its crossing only for a preview that is ready. Nothing here deletes a
unit's output or its mzTab-M, and nothing works around a refusal that protects them.

Fake ports with the same methods drive tests/test_campaign_machine.py and test_campaign_resume.py; no
test downloads anything or starts a Console.
"""

from __future__ import annotations

import base64
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
            "path", "exists", "binary_sha256", "inventory_sha256", "provenance_status", "pinned", "pin_state",
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
            "interactive": {"version": msdial_app.__version__, **git_state(interactive_root),
                            "lease_uses_store": lease_uses_store()},
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


class _ToolRefusal(Exception):
    """An ok:false reply from an approval-taking tool, carried to the one place that reads refusals."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(str(result.get("detail") or result.get("reason") or "refused"))
        self.result = result


def lease_uses_store(tools: Any = None) -> bool:
    """Whether Interactive's lease fetches through its accession download store (plan item 15), which is what
    makes the distinct bytes, not the per-unit sum, the transfer. Read from the tool it adds."""
    if tools is None:
        from msdial_app import mcp_server as tools
    return hasattr(tools, "msdial_download_store_status")


def default_extractor_path(interactive_root: str | Path) -> Path:
    """Where the newest built pin of Interactive's PINNED_BUILDS is, beside the Interactive checkout:
    <parent>\\RawMetadataExtractor-<raw>-<common>\\msrawdataworkbench\\...\\RawMetadataConsoleApp.exe."""
    from msdial_app import raw_metadata_extractor as extractor

    built = next(item for item in extractor.PINNED_BUILDS if item["state"] == extractor.PIN_BUILT)
    root = extractor.extractor_build_root(Path(interactive_root).resolve().parent, built[extractor.RAW_TREE], built[extractor.COMMON_TREE])
    return extractor.extractor_output_directory(root / extractor.RAW_TREE) / extractor.EXTRACTOR_BINARY


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

    def _takes(self, name: str, parameter: str) -> bool:
        function = getattr(self.tools, name, None)
        return function is not None and parameter in inspect.signature(function).parameters

    def capabilities(self) -> dict[str, Any]:
        """What this Interactive offers the runner, read from its code: names and signatures, nothing run."""
        import msdial_app

        return {
            "version": msdial_app.__version__,
            "classify_preflight": hasattr(self.rr, "classify_preflight"),
            # 0.5.17: the preflight takes the approval, and a disposition says whether it was applied.
            "preflight_authorization": self._takes("msdial_repository_raw_metadata_preflight", "campaign_authorization_path"),
            "disposition_hold": hasattr(self.rr, "disposition_hold"),
            "authorized_cleanup": self._takes("msdial_cleanup_repository_raw", "campaign_authorization_path"),
            "authorized_discard": self._authorized_discard() is not None,
            "split_parent_release": hasattr(self.rr, "cleanup_split_parent"),
            "cancel_job": hasattr(self.tools, "msdial_cancel_job"),
            "lease_uses_store": lease_uses_store(self.tools),
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
            # The bytes Interactive would have to move: with size_limit:exceeded the machine compares them
            # with what the volume could ever hold instead of asking again.
            return {
                "ok": False, "reason": "blocked", "blocking_reasons": list(preview.get("blocking_reasons") or []),
                "required_download_bytes": preview.get("required_download_bytes"),
                "maximum_gb": preview.get("maximum_gb"),
            }
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
        # The extractor has no outer limit here: Interactive's own per-chunk limits are authoritative. The
        # approval makes the unit a campaign unit (0.5.17): its disposition is applied, and only the pinned,
        # verified extractor is run.
        return self._call(
            "msdial_repository_raw_metadata_preflight",
            manifest_path=manifest_path, extractor_path=extractor_path, max_inputs=0, confirm_untargeted=False,
            campaign_authorization_path=authorization_path,
        )

    def classify(self, *, manifest_path: str, authorization_path: str) -> dict[str, Any]:
        """Interactive's classify_preflight: decide and apply the disposition of a unit whose preflight is
        recorded, without reading a header again. {"held": {...}} when disposition_hold holds the unit."""
        function = getattr(self.rr, "classify_preflight", None)
        if function is None:
            return {"ok": False, "reason": "unsupported", "detail": "Interactive has no classify_preflight (0.5.17)."}
        try:
            result = function(Path(manifest_path), campaign_authorization_path=authorization_path)
        except self.ca.CampaignAuthorizationError as error:
            return {"ok": False, "reason": "campaign_authorization_refused", "codes": list(error.codes), "detail": str(error)}
        except Exception as error:  # noqa: BLE001
            return _exception_result(error, "classify_preflight")
        return {"ok": True, "applied": result.get("applied") is True, "held": result.get("held"),
                "disposition": result.get("disposition")}

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

    def discard_blockers(self, manifest_path: Path, manifest: Mapping[str, Any]) -> list[dict[str, str]]:
        """What discard_download_lease would refuse on, in the order it checks, with Interactive's own tests.

        Interactive's discard has no preview that checks anything (confirmed=false returns before its
        checks), so the fallback reads them here first: a boundary-5 crossing records a deletion that is
        going to happen, not one Interactive then refuses. The codes are policy.DISCARD_BLOCKERS. The first,
        console_live, is the port's own: discard_download_lease does not look for a Console still reading
        the raw tree, such as one a backend restart left running.
        """
        from msdial_app.mztab_validation import find_mztab_files
        from msdial_app.run_finalisation import raw_deletion_holds

        blockers = self._console_blockers(manifest_path)
        status = str(manifest.get("status") or "")
        if status in {"mztab_validated", "completed", "raw_cleaned"}:
            blockers.append({"code": "validated_status", "detail": f"the unit's run is {status}; the normal cleanup applies"})
        if status == "downloading":
            owner = self.rr.lease_owner_state(dict(manifest))
            if owner.get("state") != "gone":
                blockers.append({"code": "lease_live", "detail": str(owner.get("reason") or "the lease may still run")})
        if find_mztab_files(Path(str(manifest.get("output_directory") or ""))):
            blockers.append({"code": "mztab_output_exists",
                             "detail": "the run left an mzTab-M; Interactive refuses to discard such a unit's raw data"})
        raw = Path(str(manifest.get("raw_directory") or "")).resolve()
        workspace = Path(str(manifest.get("workspace") or "")).resolve()
        if raw.parent != workspace or raw.name != "raw":
            blockers.append({"code": "raw_outside_workspace", "detail": "the raw directory is not <workspace>\\raw"})
        holds = raw_deletion_holds(manifest_path, dict(manifest))
        if holds:
            blockers.append({"code": "finalisation_held", "detail": f"{len(holds)} finalisation hold(s) on the raw directory"})
        return blockers

    def _console_blockers(self, manifest_path: Path) -> list[dict[str, str]]:
        attempt = self.live_attempt(str(manifest_path))
        if attempt is None:
            return []
        return [{"code": "console_live",
                 "detail": f"run attempt {attempt.get('attempt_id') or '?'} of job {attempt.get('job_id') or 'unrecorded'} "
                           "may still have its MS-DIAL Console running"}]

    # discard_download_lease's refusals, by the words it raises them with, as DISCARD_BLOCKERS codes: what an
    # approval-taking discard (plan item 14) says when it will not delete, so the machine can tell a refusal
    # that waiting mends from one it does not.
    _DISCARD_REFUSALS = (
        ("mzTab-M output exists", "mztab_output_exists"),
        ("must use the normal cleanup", "validated_status"),
        ("still downloading", "lease_live"),
        ("outside the expected project workspace", "raw_outside_workspace"),
        ("finalisation_held", "finalisation_held"),
    )

    def _authorized_discard(self) -> Callable[[Path, str], dict[str, Any]] | None:
        """Interactive's own discard that takes the campaign approval (plan item 14), found by its signature:
        an MCP tool msdial_discard_repository_raw, or discard_download_lease with a campaign_authorization
        parameter. It checks the approval and records the crossing itself. None until it exists."""
        if self._takes("msdial_discard_repository_raw", "campaign_authorization_path"):
            def tool(path: Path, authorization: str) -> dict[str, Any]:
                result = self._call("msdial_discard_repository_raw", manifest_path=str(path), confirmed=False,
                                    campaign_authorization_path=authorization)
                if result.get("ok") is False:
                    raise _ToolRefusal(result)
                return result

            return tool
        parameters = inspect.signature(self.rr.discard_download_lease).parameters
        for key in ("campaign_authorization_path", "campaign_authorization"):
            if key in parameters:
                return lambda path, authorization, key=key: self.rr.discard_download_lease(path, **{key: authorization})
        return None

    def discard(
        self, *, manifest_path: str, authorization_path: str, unit_id: str, parent_unit_id: str = "",
    ) -> dict[str, Any]:
        """Delete the raw data of a unit that produced no validated output, under boundary 5.

        Interactive's approval-taking discard when it has one. Until then the documented fallback: the
        finalisation holds are retried first (Interactive moving MS-DIAL's containers out of the raw tree into
        output, as its cleanup does), then discard_download_lease's own refusals are read, and only a discard
        that will happen is authorized, recorded and made. It never deletes anything but the raw tree, and
        never the unit's output or its mzTab-M: a failed run that left an mzTab-M keeps its raw data
        ({"deleted": false, "blockers": ["mztab_output_exists"]}), with no crossing recorded.
        """
        path = Path(manifest_path)
        authorized = self._authorized_discard()
        try:
            live = self._console_blockers(path)
            if live:
                # Not under a Console that may still read the raw tree, whichever discard would delete it.
                return {"ok": True, "deleted": False, "blockers": [item["code"] for item in live],
                        "detail": "; ".join(item["detail"] for item in live)}
            if authorized is not None:
                result = authorized(path, authorization_path)
            else:
                from msdial_app.run_finalisation import raw_deletion_holds, resolve_finalisation_holds

                manifest = self.rr.read_manifest(path)
                if raw_deletion_holds(path, dict(manifest)):
                    resolve_finalisation_holds(path)
                    if hasattr(self.rr, "refresh_retained_artifacts"):
                        self.rr.refresh_retained_artifacts(path)
                    manifest = self.rr.read_manifest(path)
                blockers = self.discard_blockers(path, manifest)
                if blockers:
                    return {"ok": True, "deleted": False, "blockers": [item["code"] for item in blockers],
                            "detail": "; ".join(item["detail"] for item in blockers)}
                crossing = self.ca.authorize(
                    authorization_path, unit_id, 5, entry_point="campaign_runner.discard",
                    parent_unit_id=parent_unit_id,
                    raw_retention_policy=str(manifest.get("raw_retention_policy") or "keep"),
                )
                self.rr.record_campaign_authorization(path, crossing or {})
                result = self.rr.discard_download_lease(path, confirmed=True)
        except self.ca.CampaignAuthorizationError as error:
            return {"ok": False, "reason": "campaign_authorization_refused", "codes": list(error.codes), "detail": str(error)}
        except _ToolRefusal as refusal:
            return self._discard_refusal(refusal.result, str(refusal.result.get("detail") or ""))
        except Exception as error:  # noqa: BLE001
            return self._discard_refusal(_exception_result(error, "discard_download_lease"), str(error))
        if not result.get("deleted"):
            # A preview that was not ready, as the approval-taking cleanup answers one.
            text = "; ".join(str(item) for item in result.get("blockers") or []) or str(result.get("message") or "")
            return {"ok": True, "deleted": False, "blockers": [code for words, code in self._DISCARD_REFUSALS if words in text],
                    "detail": text or "not deleted"}
        return {"ok": True, "deleted": True, "detail": result.get("raw_directory")}

    def _discard_refusal(self, result: dict[str, Any], text: str) -> dict[str, Any]:
        codes = [code for words, code in self._DISCARD_REFUSALS if words in text]
        if codes and policy.classify_result(result) not in (policy.REFUSED, policy.CONTRACT):
            return {"ok": True, "deleted": False, "blockers": codes, "detail": text}
        return result

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
        """The verdict of one gate run. report_parsed is true only where the gate's output parsed as its
        --json report; policy.gate_report_problem reads every other ending (a timeout, a gate that could not
        be started or crashed, exit 3, output that is not a report) as no usable report, which holds a unit
        before production and is only recorded after it."""
        verdict: dict[str, Any] = {"stage": GATE_STAGES[point], "strict": True, "gate_commit": self.gate_commit,
                                   "report_parsed": False}
        try:
            completed = subprocess.run(
                self.command(workspace, point), capture_output=True, timeout=self.timeout,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired:
            return {**verdict, "outcome": "timeout", "detail": f"The gate did not finish within {self.timeout:g} s."}
        except OSError as error:
            return {**verdict, "outcome": "error", "not_started": True, "detail": str(error)}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_bytes(completed.stdout)
        known = completed.returncode in (0, 2, 3, 4)
        verdict.update(
            outcome="ran" if known else "error",
            exit_code=completed.returncode if known else None,
            report_path=str(report_path),
            report_sha256=hashlib.sha256(completed.stdout).hexdigest(),
        )
        stderr = completed.stderr.decode("utf-8", errors="replace")[-2000:]
        try:
            report = json.loads(completed.stdout.decode("utf-8"))
        except (ValueError, RecursionError):
            report = None
        if not known or not isinstance(report, Mapping) or not isinstance(report.get("checks"), list):
            # The ledger keeps only the exit codes the gate defines; any other is said here.
            said = "" if known else f"The gate exited {completed.returncode}. "
            verdict["detail"] = (said + stderr).strip() or "The gate's output is not a report."
            return verdict
        verdict["report_parsed"] = True
        checks = [item for item in report.get("checks") or [] if isinstance(item, Mapping)]

        def ids(status: str) -> list[str]:
            # The gate writes its statuses lowercase ("fail", "warn").
            return sorted({str(item.get("check_id")) for item in checks if str(item.get("status") or "").casefold() == status})

        verdict["fail_ids"] = ids("fail")
        verdict["warn_ids"] = ids("warn")
        verdict["strict_hold_ids"] = list(report.get("strict_failures") or [])
        verdict["stage_reached"] = (report.get("progress") or {}).get("stage_reached")
        verdict["blocking_fail_ids"], verdict["run_policy_source"] = policy.run_blocking_failures(report)
        verdict["blocking_unevaluated_ids"] = policy.run_blocking_unevaluated(report)
        verdict["run_policy_mismatches"] = policy.run_policy_mismatches(report)
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

def backend_creation_flags() -> int:
    """The Windows creation flags of the campaign backend: a console of its own, with no window.

    Not DETACHED_PROCESS. A process without a console makes Windows build a new console, with a visible
    window, for every console program it starts, and Windows ignores CREATE_NO_WINDOW beside
    DETACHED_PROCESS. Each git call behind /api/config then took about 0.4 s instead of 0.03 s when measured
    on 2026-10-06 (3 s in a traced copy of the pilot's backend), and /api/config also starts the Console
    (--version, rtcorrection --help) for every candidate; the pilot's backend missed the 60 s start window
    on 2026-10-03. A windowless console is inherited by git, the Console, 7-Zip and the extractor, and its
    own process group keeps the runner's Ctrl+C from reaching it.
    """
    return subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW


# How the runner starts the backend (run --backend-launch). "wmi" has Windows Management Instrumentation create
# a broker (backend_launch.py) that starts the backend and exits, so the backend is nobody's child and sits
# outside the runner's job object; "child" starts it as the runner's own child; "auto" tries "wmi" and falls
# back to "child", recording why.
BACKEND_LAUNCH_METHODS = ("auto", "wmi", "child")
BACKEND_BROKER = Path(__file__).resolve().with_name("backend_launch.py")


class BackendLaunchError(Exception):
    """A start that did not happen. may_have_started: the broker may have started a backend that did not report
    back, so a second start would only collide with it on the port; record then says what is known of that
    start (the broker's process id and creation time), for the supervisor to wait for it."""

    def __init__(self, message: str, *, may_have_started: bool = False, record: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.may_have_started = may_have_started
        self.record = dict(record or {})


def _powershell() -> str:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate) if candidate.is_file() else "powershell.exe"


def _powershell_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def wmi_create_process(command_line: str, cwd: str, *, timeout: float = 60.0) -> tuple[int | None, str]:
    """Create a process through WMI's Win32_Process.Create, called with PowerShell's Invoke-CimMethod.

    The process is created by the WMI provider host, not by the caller: it is not the caller's child and it
    is in none of the caller's job objects (checked on 2026-10-06: a process created this way from inside
    the Claude app's job reported IsProcessInJob false). Its window is hidden (Win32_ProcessStartup
    ShowWindow 0). Returns (process id, "") or (None, why not).
    """
    script = "\n".join((
        "$ErrorActionPreference = 'Stop'",
        "$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow = [uint16]0}",
        "$arguments = @{CommandLine = " + _powershell_literal(command_line) + "; CurrentDirectory = "
        + _powershell_literal(cwd) + "; ProcessStartupInformation = $startup}",
        "$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments $arguments",
        "Write-Output ('{0} {1}' -f $result.ReturnValue, $result.ProcessId)",
    ))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        completed = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, f"PowerShell could not call Win32_Process.Create: {type(error).__name__}: {error}"
    words = (completed.stdout or "").strip().split()
    if completed.returncode != 0 or len(words) < 2:
        message = " ".join((completed.stderr or completed.stdout or "").split())[:300]
        return None, f"Win32_Process.Create through PowerShell failed (exit code {completed.returncode}): {message}"
    if words[-2] != "0":
        return None, f"Win32_Process.Create returned {words[-2]}"
    try:
        return int(words[-1]), ""
    except ValueError:
        return None, f"Win32_Process.Create gave no process id: {words[-1]!r}"


def _process_alive(pid: Any, created_at: float | None = None) -> bool | None:
    try:
        from msdial_app.process_liveness import process_is_alive
    except ImportError:
        try:
            import psutil

            process = psutil.Process(int(pid))
            if created_at is not None and abs(process.create_time() - float(created_at)) > 1.0:
                return False
            return process.is_running()
        except ImportError:
            return None
        except Exception:  # noqa: BLE001 - psutil.NoSuchProcess and friends: the process is gone
            return False
    return process_is_alive(pid, created_at)


def _process_created_at(pid: int) -> float | None:
    try:
        from msdial_app.process_liveness import process_created_at
    except ImportError:
        try:
            import psutil

            return psutil.Process(int(pid)).create_time()
        except Exception:  # noqa: BLE001 - unreadable
            return None
    return process_created_at(pid)


def process_in_job(pid: int | None) -> bool | None:
    """Whether a process sits in a Windows job object (None: not Windows, or not readable). A process in the
    Claude app's job, or a scheduled task's, ends when that job is closed or terminated."""
    if os.name != "nt" or not pid:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        kernel32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            result = wintypes.BOOL()
            if not kernel32.IsProcessInJob(handle, None, ctypes.byref(result)):
                return None
            return bool(result.value)
        finally:
            kernel32.CloseHandle(handle)
    except (ImportError, OSError, AttributeError):
        return None


def listening_pid(port: int) -> int | None:
    """The process listening on a local TCP port, if psutil can say."""
    try:
        import psutil

        for connection in psutil.net_connections(kind="tcp"):
            if connection.status == psutil.CONN_LISTEN and connection.laddr and connection.laddr.port == int(port):
                return connection.pid or None
    except Exception:  # noqa: BLE001 - ImportError, AccessDenied: not knowable here
        return None
    return None


def process_descends_from(pid: Any, ancestor: Any, ancestor_created_at: float | None = None, *, depth: int = 4) -> bool | None:
    """Whether process `pid` is `ancestor` or was started by it, directly or through up to `depth` launchers.

    The backend's command names the runner's Python, and that Python can be a launcher rather than the
    interpreter: a venv's Scripts\\python.exe and pythonw.exe, and py.exe, start the real interpreter as their
    child and wait for it. The process the runner (or the broker) started is then the launcher, and the port is
    held by its child (2026-10-07 review of PR #30: a venv runner rejected its own backend). Each parent is
    read from the child's recorded parent process id, which Windows keeps after the parent has exited, so a
    broker that has exited still counts; a parent created after its child is a reused process id, not the
    parent. None: not knowable here (no psutil, or access denied)."""
    if not pid or not ancestor:
        return None
    try:
        import psutil
    except ImportError:
        return None
    ancestor = int(ancestor)
    try:
        process = psutil.Process(int(pid))
        if process.pid == ancestor:
            return ancestor_created_at is None or abs(process.create_time() - float(ancestor_created_at)) <= 1.0
        for _ in range(depth):
            parent_pid = process.ppid()
            created = process.create_time()
            if parent_pid == ancestor:
                return ancestor_created_at is None or float(ancestor_created_at) <= created + 1.0
            if not parent_pid:
                return False
            try:
                parent = psutil.Process(parent_pid)
            except psutil.NoSuchProcess:
                return False
            if parent.create_time() > created + 1.0:
                return False
            process = parent
        return False
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, OSError):
        return None


class MemoryStartState:
    """BackendSupervisor's start state held in memory: for a supervisor with no ledger (tests, a probe)."""

    def __init__(self) -> None:
        self.value: dict[str, Any] = {}

    def load(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.value))

    def save(self, value: Mapping[str, Any]) -> None:
        self.value = json.loads(json.dumps(dict(value)))


class LedgerStartState:
    """BackendSupervisor's start state kept in the campaign ledger (Ledger.backend_start_state): its consecutive
    start failures, the time before which it starts no backend, and the start it still waits for. The scheduled
    task starts a new runner after each one that exits, and that runner must honour them (2026-10-07 review of
    PR #30)."""

    def __init__(self, book: Any) -> None:
        self.book = book

    def load(self) -> dict[str, Any]:
        return self.book.backend_start_state()

    def save(self, value: Mapping[str, Any]) -> None:
        self.book.set_backend_start_state(value)


def _iso_epoch(value: float) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat(timespec="seconds")


class BackendSupervisor:
    """The standalone Interactive backend the campaign's jobs run in, on a port of its own.

    A port and a job registry of its own (MSDIAL_INTERACTIVE_JOBS_FILE) keep an interactive Claude
    session from overwriting the campaign's job records or restarting its backend.

    IT OUTLIVES ITS LAUNCHER (2026-10-06). On Windows it is started through WMI (launch_method "wmi", the
    default "auto" falls back to "child"): a broker created by Win32_Process.Create starts it and exits, so
    the backend is not the runner's child and is in none of the runner's job objects. A tree kill of the
    runner (Task Scheduler's Stop, a tool's timeout) does not reach it, and neither does the end of the job
    the runner was started in: the Claude app's, which does not allow breakaway and is force-closed when the
    app updates, or a scheduled task's. A runner that stops leaves its Console running, and the next runner
    reattaches to the job instead of orphaning a run hours from its end. The backend keeps the windowless
    console and process group of backend_creation_flags. Its process id, creation time and launch method are
    written to backend-launch.json beside the job registry and reported in the backend_started event.

    ITS OWN BACKEND IS KNOWN BY THE PROCESS TREE (2026-10-07). The process the runner or the broker started
    can be a launcher (a venv's python.exe, py.exe) whose child holds the port. A listener that is the
    started process, or descends from it (or, where the broker never reported, from the broker), is this
    runner's backend: its process id and creation time replace the launcher's in the launch record, which
    keeps the launcher's as launcher_pid. Any other listener is refused, and named.

    READINESS is /api/agent/status (Interactive's job summary, which carries app_version and probes nothing),
    polled for start_timeout seconds. /api/config, which starts the Console and git for every Console
    candidate, is then read for the checkout the backend runs from, with a deadline of its own, config_timeout,
    counted from the moment the status answered (2026-10-07: it was cut to what was left of the start
    deadline). A request that timed out is not sent again over it; one that failed at once is retried until
    the deadline. A backend that answers the status but not /api/config is reported as exactly that.

    A backend that already answers is reused, and ensure() says how it was started where that is knowable
    (origin()): by this campaign's runner (the launch record matches its process id and creation time, or it
    descends from the recorded process) or not, and whether it sits in a job object. Starts that fail
    max_start_failures times in a row are not tried again for retry_after seconds, and the refusal names
    each failure. A start that has not answered yet is waited for again rather than started twice.

    THAT START STATE LIVES IN THE LEDGER (2026-10-07). The consecutive failures, the time before which no
    start is tried and the start still waited for are read from `state` (LedgerStartState: a meta row of the
    campaign ledger) at every ensure() and written back at every change, because the scheduled task starts a
    new runner process after each one that exits, and run exits when its first ensure() is not usable: in
    memory they lasted one runner, so the pause never engaged and a slow start was started again. Without a
    `state` they are kept in memory (MemoryStartState).

    The backend's own output stream is discarded unless log_directory is given (run --backend-log). It
    is Interactive's, not the runner's: an uncaught traceback there can quote a file location, and no
    private library location may reach a log the runner writes. What each job did stays in the job
    registry and the unit manifest.
    """

    def __init__(
        self, *, python: str, interactive_root: Path, host: str, port: int, jobs_file: Path,
        workspace_root: str, log_directory: Path | None = None, launch_method: str = "auto",
        start_timeout: float = 300.0, config_timeout: float = 120.0, max_start_failures: int = 3,
        retry_after: float = 3600.0, state: Any = None,
    ) -> None:
        if launch_method not in BACKEND_LAUNCH_METHODS:
            raise ValueError(f"launch_method must be one of {', '.join(BACKEND_LAUNCH_METHODS)}")
        self.python = python
        self.interactive_root = Path(interactive_root)
        self.host = host
        self.port = int(port)
        self.jobs_file = Path(jobs_file)
        self.workspace_root = str(workspace_root)
        self.log_directory = Path(log_directory) if log_directory is not None else None
        self.launch_method = launch_method
        self.start_timeout = float(start_timeout)
        self.config_timeout = float(config_timeout)
        self.max_start_failures = int(max_start_failures)
        self.retry_after = float(retry_after)
        self.state = state if state is not None else MemoryStartState()
        self.failures: list[dict[str, str]] = []
        # Wall-clock seconds (time.time), not monotonic: it is read again by another runner process.
        self._retry_at = 0.0
        self._pending: dict[str, Any] | None = None
        self._child: subprocess.Popen | None = None
        self._reported_origin: tuple[Any, Any] | None = None
        self.last_get_error: str | None = None

    @property
    def start_failures(self) -> int:
        return len(self.failures)

    @property
    def launch_record_path(self) -> Path:
        return self.jobs_file.parent / "backend-launch.json"

    def command(self) -> list[str]:
        return [self.python, str(self.interactive_root / "app.py"), "--host", self.host, "--port", str(self.port), "--no-browser"]

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        environment = dict(os.environ if base is None else base)
        environment["MSDIAL_INTERACTIVE_JOBS_FILE"] = str(self.jobs_file)
        environment["MSDIAL_REPOSITORY_WORKSPACE_ROOT"] = self.workspace_root
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return environment

    def _get(self, path: str, timeout: float) -> dict[str, Any] | None:
        """A JSON object from the backend, or None; last_get_error then says why ("timeout" for a request that
        timed out, which the caller does not send again over the same deadline)."""
        self.last_get_error = None
        try:
            with urllib.request.urlopen(f"http://{self.host}:{self.port}{path}", timeout=timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            self.last_get_error = f"HTTP {error.code}"
            return None
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            timed_out = isinstance(error, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout))
            self.last_get_error = "timeout" if timed_out else f"{type(reason).__name__}: {reason}"
            return None
        except ValueError:
            self.last_get_error = "a reply that is not JSON"
            return None
        if not isinstance(value, dict):
            self.last_get_error = "a reply that is not a JSON object"
            return None
        return value

    def status(self) -> dict[str, Any] | None:
        """Interactive's /api/agent/status: whether a backend answers at all. It summarizes the job registry
        and starts no program, so it answers within a second of the server's start."""
        value = self._get("/api/agent/status?limit=0", 5)
        return value if value is not None and "app_version" in value else None

    def config(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Interactive's /api/config, which says which checkout the backend runs from. It starts the Console and
        git for every Console candidate it finds, so it is given config_timeout, not a few seconds."""
        return self._get("/api/config", self.config_timeout if timeout is None else timeout)

    def read_config(self) -> tuple[dict[str, Any] | None, str]:
        """/api/config within a deadline of its own (config_timeout from now): (config, "") or (None, what
        happened, as a phrase). A request that timed out is not sent again, since the backend goes on with
        the abandoned one (the Console and git probes would overlap); one that failed at once is retried."""
        deadline = time.monotonic() + self.config_timeout
        why = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.last_get_error = None
            config = self.config(timeout=remaining)
            if config is not None:
                return config, ""
            error = self.last_get_error
            if error is None or error == "timeout":
                why = ""
                break
            why = error
            if deadline - time.monotonic() <= 1.0:
                break
            time.sleep(1)
        if why:
            return None, f"gave no usable answer within {self.config_timeout:g} s (last: {why})"
        return None, f"did not answer within {self.config_timeout:g} s"

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

    # ---- the start state, kept in the ledger ----

    def _load_state(self) -> None:
        value = self.state.load() or {}
        self.failures = [dict(item) for item in value.get("failures") or [] if isinstance(item, dict)]
        try:
            self._retry_at = float(value.get("retry_at_epoch") or 0.0)
        except (TypeError, ValueError):
            self._retry_at = 0.0
        pending = value.get("pending")
        self._pending = dict(pending) if isinstance(pending, dict) else None

    def _save_state(self) -> None:
        self.state.save({
            "failures": self.failures, "retry_at_epoch": self._retry_at,
            "retry_at": _iso_epoch(self._retry_at) if self.start_failures >= self.max_start_failures else None,
            "pending": self._pending,
        })

    # ---- how a backend was started ----

    def _read_launch_record(self) -> dict[str, Any]:
        try:
            value = json.loads(self.launch_record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_launch_record(self, record: Mapping[str, Any]) -> None:
        try:
            write_json_atomic(self.launch_record_path, dict(record))
        except OSError:
            pass

    @staticmethod
    def _owns(record: Mapping[str, Any], listener: int) -> bool | None:
        """Whether the process listening on the port is the backend this launch record names: that process,
        or one it started (through a launcher), or, where the broker never reported, one the broker started.
        None: not knowable here."""
        if record.get("pid") and listener == record.get("pid"):
            return True
        answer: bool | None = False
        for pid_key, created_key in (("pid", "process_created_at"), ("broker_pid", "broker_created_at")):
            if not record.get(pid_key):
                continue
            found = process_descends_from(listener, record[pid_key], record.get(created_key))
            if found:
                return True
            if found is None:
                answer = None
        return answer

    def _adopt_listener(self, record: dict[str, Any], listener: int) -> dict[str, Any]:
        """The launch record names the process that holds the port: the launcher it was started through, if
        any, is kept as launcher_pid."""
        if record.get("pid") == listener:
            return record
        adopted = dict(record)
        if record.get("pid"):
            adopted["launcher_pid"] = record.get("pid")
            adopted["launcher_created_at"] = record.get("process_created_at")
        adopted["pid"] = listener
        adopted["process_created_at"] = _process_created_at(listener)
        in_job = process_in_job(listener)
        if in_job is not None:
            adopted["in_job"] = in_job
        self._write_launch_record(adopted)
        return adopted

    def origin(self, pid: int | None = None) -> dict[str, Any]:
        """How the backend listening on the campaign port was started, as far as this machine can tell."""
        listener = listening_pid(self.port) if pid is None else pid
        record = self._read_launch_record()
        result: dict[str, Any] = {"pid": listener}
        if listener is None:
            result.update(how="unknown", note="the process listening on the port could not be read")
            return result
        created = _process_created_at(listener)
        recorded = record.get("process_created_at")
        same = record.get("pid") == listener and (
            created is None or recorded is None or abs(float(created) - float(recorded)) <= 1.0
        )
        if not same and record and record.get("pid") != listener and self._owns(record, listener):
            # Started by this runner through a launcher (a record written before 2026-10-07 names the launcher).
            # Or by the broker of a start that never reported (no backend process id recorded).
            same, recorded = True, created
            if record.get("pid"):
                result["launcher_pid"] = record["pid"]
            else:
                result["broker_pid"] = record.get("broker_pid")
        if same:
            result.update(how=record.get("method") or "unknown", by="this campaign's runner",
                          started_at=record.get("started_at"), process_created_at=recorded)
            if record.get("launcher_pid"):
                result["launcher_pid"] = record["launcher_pid"]
            if record.get("wmi_failure"):
                result["wmi_failure"] = record["wmi_failure"]
        else:
            result.update(
                how="unknown", process_created_at=created,
                note=("not started by this campaign's runner"
                      + (f" (its launch record names pid {record.get('pid')})" if record.get("pid") else " (it has no launch record)")
                      + ": how it was started is not known. A backend started by hand or before 2026-10-06 may have"
                        " no windowless console, and one started from a Claude session ends with the Claude app."),
            )
        in_job = process_in_job(listener)
        if in_job is not None:
            result["in_job"] = in_job
        if in_job:
            result["warning"] = "it sits in a job object, so it ends when whatever owns that job ends (the Claude app, a scheduled task)"
        return result

    # ---- starting it ----

    def ensure(self) -> dict[str, Any]:
        self._load_state()
        if self.status() is not None:
            return self._reuse()
        if self._pending is not None:
            if self._pending_expired(self._pending):
                self._pending = None
                self._save_state()
            elif self._alive(self._pending) is not False:
                return self._await(self._pending)
            else:
                self._pending = None
                self._save_state()
        now = time.time()
        if self.start_failures >= self.max_start_failures:
            if now < self._retry_at:
                return {"ok": False, "started": False, "start_failures": self.start_failures,
                        "retry_at": _iso_epoch(self._retry_at), "detail": self._refusal(now)}
            self.failures = []
            self._retry_at = 0.0
            self._save_state()
        try:
            record = self._launch()
        except BackendLaunchError as error:
            if error.may_have_started:
                # The broker may yet start a backend: wait for it, from the ledger, instead of starting another.
                self._pending = {**error.record, "launched_at_epoch": time.time()}
            return self._failed(str(error), started=False)
        return self._await(record)

    def _pending_expired(self, record: Mapping[str, Any]) -> bool:
        """A start whose backend's process id is not known (the broker never reported) is waited for only
        start_timeout seconds from its launch: there is no process to ask whether it still runs."""
        if record.get("pid"):
            return False
        try:
            launched = float(record.get("launched_at_epoch") or 0.0)
        except (TypeError, ValueError):
            launched = 0.0
        return time.time() - launched >= self.start_timeout

    def _reuse(self) -> dict[str, Any]:
        origin = self.origin()
        result: dict[str, Any] = {"ok": False, "started": False, "pid": origin.get("pid"), "origin": origin}
        key = (origin.get("pid"), origin.get("how"))
        if key != self._reported_origin:
            self._reported_origin = key
            result["origin_new"] = True
        config, why = self.read_config()
        if config is None:
            result["detail"] = (f"A backend answers /api/agent/status on port {self.port}, but its /api/config {why} "
                                f"(--backend-config-timeout).")
            return result
        problems = self.check(config)
        result.update(ok=not problems, detail="; ".join(problems))
        if not problems and (self.failures or self._pending is not None or self._retry_at):
            # A usable backend answers: the failures in a row end, and nothing is waited for any more.
            self.failures, self._retry_at, self._pending = [], 0.0, None
            self._save_state()
        return result

    def _alive(self, record: Mapping[str, Any]) -> bool | None:
        if not record.get("pid"):
            return None
        child = self._child
        if record.get("method") == "child" and child is not None and child.pid == record.get("pid"):
            return child.poll() is None
        return _process_alive(record.get("pid"), record.get("process_created_at"))

    def _refusal(self, now: float) -> str:
        reasons = "; ".join(f"{index}. {failure['reason']}" for index, failure in enumerate(self.failures, 1))
        wait = int(max(0.0, self._retry_at - now))
        return (f"The campaign backend could not be started {self.start_failures} times in a row ({reasons}). "
                f"No start is tried for another {wait} s (until {_iso_epoch(self._retry_at)}); until then the runner "
                f"pauses for the backend.")

    def _failed(self, reason: str, *, started: bool, record: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self.failures.append({"at": _now().isoformat(timespec="seconds"), "reason": reason})
        result: dict[str, Any] = {"ok": False, "started": started, "start_failures": self.start_failures, "detail": reason}
        if record is not None:
            result.update({key: record.get(key) for key in ("pid", "process_created_at", "method") if record.get(key) is not None})
        if self.start_failures >= self.max_start_failures:
            now = time.time()
            self._retry_at = now + self.retry_after
            result["detail"] = self._refusal(now)
            result["retry_at"] = _iso_epoch(self._retry_at)
        self._save_state()
        return result

    def _await(self, record: dict[str, Any]) -> dict[str, Any]:
        begun = time.monotonic()
        deadline = begun + self.start_timeout
        if not record.get("pid"):
            # Only the broker is known: wait no longer than start_timeout from its launch.
            try:
                launched = float(record.get("launched_at_epoch") or time.time())
            except (TypeError, ValueError):
                launched = time.time()
            deadline = begun + max(0.0, launched + self.start_timeout - time.time())
        name = (f"The backend (pid {record.get('pid')}, started by {record.get('method')})" if record.get("pid") else
                f"The backend the broker (pid {record.get('broker_pid')}) may have started")
        while time.monotonic() < deadline:
            if self.status() is not None:
                listener = listening_pid(self.port)
                if listener is not None and listener != record.get("pid"):
                    owns = self._owns(record, listener)
                    if not owns:
                        self._pending = None
                        return self._failed(
                            f"Port {self.port} is answered by pid {listener}, which "
                            + ("is not the backend this runner started nor a process it started"
                               if owns is False else "could not be traced to the backend this runner started")
                            + f" (pid {record.get('pid') or 'unknown'}"
                            + (f", broker pid {record.get('broker_pid')}" if record.get("broker_pid") else "") + ").",
                            started=True, record=record)
                    record = self._adopt_listener(record, listener)
                self._pending = None
                self._save_state()
                config, why = self.read_config()
                if config is None:
                    return self._failed(f"{name} answers /api/agent/status, but its /api/config {why} "
                                        f"(--backend-config-timeout); it is left running, and the next check reads "
                                        f"/api/config again.", started=True, record=record)
                problems = self.check(config)
                if problems:
                    return self._failed("; ".join(problems), started=True, record=record)
                self.failures, self._retry_at = [], 0.0
                self._save_state()
                self._reported_origin = (record.get("pid"), record.get("method"))
                result = {"ok": True, "started": True, "detail": "", "seconds": round(time.monotonic() - begun, 1)}
                result.update({key: record[key] for key in ("pid", "process_created_at", "launcher_pid", "method", "in_job",
                                                            "wmi_failure") if record.get(key) is not None})
                return result
            if self._alive(record) is False:
                self._pending = None
                return self._failed(f"{name} exited before it answered.", started=True, record=record)
            time.sleep(1)
        if not record.get("pid"):
            self._pending = None
            return self._failed(f"{name} did not answer within {self.start_timeout:g} s of the broker's launch "
                                f"(--backend-start-timeout); the next check may start one.", started=True, record=record)
        self._pending = record
        return self._failed(f"{name} did not answer within {self.start_timeout:g} s (--backend-start-timeout); it is "
                            f"left running and waited for again at the next check, by this runner or the next.",
                            started=True, record=record)

    def _log_path(self) -> Path | None:
        if self.log_directory is None:
            return None
        self.log_directory.mkdir(parents=True, exist_ok=True)
        return self.log_directory / f"backend-{_now().strftime('%Y%m%dT%H%M%SZ')}.log"

    def _launch(self) -> dict[str, Any]:
        self.jobs_file.parent.mkdir(parents=True, exist_ok=True)
        method = self.launch_method if os.name == "nt" else "child"
        wmi_failure = None
        record: dict[str, Any] | None = None
        if method in ("auto", "wmi"):
            try:
                record = self._launch_wmi()
            except BackendLaunchError as error:
                if error.may_have_started:
                    # What is known of a start that may have happened, for origin() to recognise its backend.
                    error.record.update(started_at=_now().isoformat(timespec="seconds"), port=self.port,
                                        interactive_root=str(self.interactive_root))
                    self._write_launch_record(error.record)
                    raise
                if method == "wmi":
                    raise
                wmi_failure = str(error)
        if record is None:
            record = self._launch_child()
            if wmi_failure:
                record["wmi_failure"] = wmi_failure
        record.update(started_at=_now().isoformat(timespec="seconds"), port=self.port,
                      interactive_root=str(self.interactive_root))
        self._write_launch_record(record)
        return record

    def _launch_child(self) -> dict[str, Any]:
        log_path = self._log_path()
        log: Any = open(log_path, "ab") if log_path is not None else open(os.devnull, "ab")
        try:
            process = subprocess.Popen(
                self.command(), cwd=str(self.interactive_root), env=self.environment(), stdout=log,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                creationflags=backend_creation_flags() if os.name == "nt" else 0, close_fds=True,
            )
        except OSError as error:
            raise BackendLaunchError(f"The backend could not be started: {error}") from error
        finally:
            log.close()
        self._child = process
        record: dict[str, Any] = {"method": "child", "pid": process.pid, "process_created_at": _process_created_at(process.pid)}
        in_job = process_in_job(process.pid)
        if in_job is not None:
            record["in_job"] = in_job
        return record

    def _launch_wmi(self) -> dict[str, Any]:
        pythonw = Path(self.python).with_name("pythonw.exe")
        if not pythonw.is_file():
            raise BackendLaunchError(f"there is no pythonw.exe beside {Path(self.python).name}, to start the broker without a window")
        stamp = f"{_now().strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"
        spec_path = self.jobs_file.parent / f"backend-launch-{stamp}.spec.json"
        result_path = self.jobs_file.parent / f"backend-launch-{stamp}.result.json"
        log_path = self._log_path()
        # The spec holds the runner's environment; the broker deletes it as soon as it has read it.
        write_json_atomic(spec_path, {
            "command": self.command(), "cwd": str(self.interactive_root), "environment": self.environment(),
            "creationflags": backend_creation_flags(), "log": str(log_path) if log_path is not None else None,
            "result": str(result_path),
        })
        broker_created_at = None
        try:
            broker, why = wmi_create_process(
                subprocess.list2cmdline([str(pythonw), str(BACKEND_BROKER), str(spec_path)]), str(self.interactive_root),
            )
            if broker is None:
                raise BackendLaunchError(why)
            broker_created_at = _process_created_at(broker)
            deadline = time.monotonic() + 30
            while not result_path.is_file():
                if time.monotonic() > deadline:
                    raise BackendLaunchError(
                        f"the broker WMI created (pid {broker}) reported nothing within 30 s", may_have_started=True,
                        record={"method": "wmi", "pid": None, "broker_pid": broker, "broker_created_at": broker_created_at})
                time.sleep(0.2)
            reported = json.loads(result_path.read_text(encoding="utf-8"))
        finally:
            for path in (spec_path, result_path):
                try:
                    path.unlink()
                except OSError:
                    pass
        if reported.get("error") or not reported.get("pid"):
            raise BackendLaunchError(f"the broker could not start the backend: {reported.get('error')}")
        record: dict[str, Any] = {"method": "wmi", "pid": int(reported["pid"]), "broker_pid": broker,
                                  "broker_created_at": broker_created_at,
                                  "process_created_at": reported.get("process_created_at")}
        if record["process_created_at"] is None:
            record["process_created_at"] = _process_created_at(record["pid"])
        if reported.get("in_job") is not None:
            record["in_job"] = bool(reported["in_job"])
        return record


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

