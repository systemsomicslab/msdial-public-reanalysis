"""Fake ports for the campaign runner's tests: a backend, a Catalog, a gate, a disk, a clock and pins.

Nothing here downloads a byte or starts a Console. The fake backend keeps what the real one keeps, where
the real one keeps it: jobs in a registry that outlives the runner process, and the unit manifest on
disk, with the lease owner, the run attempts and the boundary-4 crossing Interactive writes before a
job exists. That is what lets tests/test_campaign_resume.py stop the runner at any commit and check that
it resumes without a second download or a second Console.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from campaign import ledger as ledger_module  # noqa: E402
from campaign import machine, policy, ports  # noqa: E402

SHA = {name: hashlib.sha256(name.encode()).hexdigest() for name in ("console", "extractor", "positive", "negative", "lbm", "auth")}
POSITIVE_MSP = "SyntheticPositive.msp"
NEGATIVE_MSP = "SyntheticNegative.msp"
LBM = "Synthetic.lbm2"


class Crash(BaseException):
    """A runner process stopping dead at a commit. BaseException, so no step's handler catches it."""


class FakeClock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.slept = 0.0

    def now(self) -> datetime:
        return self.moment

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.moment += timedelta(seconds=max(1.0, float(seconds)))


class FakeDisk:
    def __init__(self, free: int = 10 * 1000**4, total: int = 20 * 1000**4) -> None:
        self.free = free
        self.total = total

    def usage(self, _path: str) -> tuple[int, int]:
        return self.free, self.total

    def tree_bytes(self, path: str) -> int:
        root = Path(path)
        if not root.exists():
            return 0
        return sum(item.stat().st_size for item in root.rglob("*") if item.is_file())


@dataclass
class UnitScript:
    """What the fake backend does for one unit, attempt by attempt."""

    # ok; fail (HTTP 503); corrupt (a checksum that does not match, not the network); stall; hold (bytes keep
    # arriving and the job never ends until it is cancelled); blocked; interrupt; shared (waiting for another
    # unit's lease to fetch a shared object, no bytes of its own, then ok)
    downloads: list[str] = field(default_factory=lambda: ["ok"])
    # run, split, skip, exclude, none, malformed; aif_hold (Interactive 0.5.31: a skip with hold true for a
    # multi-collision-energy AIF unit, aif_multi_ce_awaiting_console); aif_multi_ce (Interactive 0.5.34: such a unit
    # decided for the Console the preflight is given, else the saved one (World.saved_console): run as AIF under
    # multi_ce_aif_with_console_825 where that Console is in World.consoles_825, held as aif_hold otherwise)
    disposition: str = "run"
    # Whether the preflight applies its disposition (a campaign unit, Interactive 0.5.17), and whether
    # classify_preflight then does; held: what disposition_hold holds the unit for, if anything.
    applied: bool = True
    classify_applies: bool = True
    held: str = ""
    split_modes: tuple[str, ...] = ("DDA", "SWATH")
    diagnostics: list[str] = field(default_factory=lambda: ["ok"])  # ok, timeout, fail, lose_reply
    # ok, timeout, fail, invalid (an mzTab-M that does not validate), late_fail (the mzTab-M validated and
    # the job failed after), hold, lose_reply, busy_reply; rt_fail (the Console could not select anchors for
    # automatic RT correction: exit -1, no output, its line in the job's log above finalisation's)
    runs: list[str] = field(default_factory=lambda: ["ok"])
    cleanup: str = "ok"  # ok, blocked, unsupported (an Interactive whose cleanup takes no approval)
    # How long each job runs, in 30-second polls of fake time. A job ends when its time is up, whether or
    # not anyone is polling it, as a real job does while the runner is down.
    ticks: int = 2
    # What Interactive's size checks see: the preview's required_download_bytes (size_limit:exceeded above
    # maximum_gb) and the bytes the lease streams (it stops past maximum_gb). 0 is small.
    required_bytes: int = 0
    remote_bytes: int = 0
    # The analytical-order record the prepared metadata's preview carries (Interactive's with_order_source): its
    # order_source, or None for a record that names none; the key is left out when no_order_record.
    order_source: str | None = None
    no_order_record: bool = False
    # The warnings Interactive's guided plan validation raises (level warning; they stop nothing).
    plan_warnings: list[str] = field(default_factory=list)


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1, sort_keys=True), encoding="utf-8")


def _read(path: str | Path) -> dict[str, Any] | None:
    location = Path(str(path or ""))
    if not str(path or "").strip() or not location.is_file():
        return None
    return json.loads(location.read_text(encoding="utf-8"))


class ManifestStore:
    """The backend's unit manifests. Held in memory, because opening files is most of a test's time on
    Windows; each manifest file is created on disk once, so the runner's own existence checks see it."""

    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    @staticmethod
    def key(path: str | Path) -> str:
        return str(Path(str(path))).casefold()

    def write(self, path: str | Path, value: dict[str, Any]) -> None:
        location = Path(str(path))
        if self.key(path) not in self.values and not location.is_file():
            _write(location, {"note": "the fake backend holds this manifest in memory"})
        self.values[self.key(path)] = json.loads(json.dumps(value))

    def read(self, path: str | Path) -> dict[str, Any] | None:
        if not str(path or "").strip():
            return None
        value = self.values.get(self.key(path))
        return json.loads(json.dumps(value)) if value is not None else None


class FakeInteractive:
    def __init__(self, world: "World") -> None:
        self.world = world
        self.jobs: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.download_starts: list[str] = []
        self.console_starts: list[tuple[str, str]] = []
        self.cancels: list[tuple[str, str]] = []
        self.split_release_supported = False
        # Whether discard and release_split_parent take release_disposition_hold (the agreed contract of
        # 2026-10-07); False plays an Interactive 0.5.31 before it, which has the hold and no way to release it.
        self.release_hold_supported = True
        self.split_parts: dict[str, list[str]] = {}
        self.store = ManifestStore()
        self._counter = 0
        self._tries: dict[tuple[str, str], int] = {}
        # Consoles a backend restart left running (restart(orphan_consoles=True)), by job id: the unit and when
        # the Console ends by itself (None: never). Whether the backend that started one still runs, whether
        # kill_orphan can stop one, the orphans it stopped, and every Console start made while another Console
        # (a job's or an orphan's) was still running.
        self.orphans: dict[str, dict[str, Any]] = {}
        self.orphan_backend_alive = False
        self.orphan_unkillable = False
        self.kills: list[str] = []
        self.overlapping_starts: list[tuple[str, list[str]]] = []
        # Interactive 0.5.28's step rule (2026-10-06, msdial-interactive-app#61): the family step of the
        # instrument family it reads from the file (world.instrument_family) is always the coarse step, and a
        # requested step is only recorded (requested_threshold_step), never searched. step_fallback: no multiple
        # of the family step lands in range, and the estimate falls back to a tenth of it (threshold_step the
        # step used, coarse_threshold_step the family step searched first). legacy_estimate: an Interactive
        # before 0.5.28, which searches the step it is asked for (100 unasked) and records threshold_step
        # alone. estimate_patch overrides fields of every estimate.
        self.step_fallback = False
        self.legacy_estimate = False
        self.estimate_patch: dict[str, Any] = {}

    # ---- helpers ----
    def _job_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter:04d}"

    def _next(self, unit: str, kind: str, outcomes: list[str]) -> str:
        index = self._tries.get((unit, kind), 0)
        self._tries[(unit, kind)] = index + 1
        return outcomes[min(index, len(outcomes) - 1)]

    def _workspace(self, repository: str, accession: str, unit: str) -> Path:
        return self.world.workspace_root / repository / accession / unit

    def _unit_of_manifest(self, manifest_path: str) -> str:
        return str(((self.store.read(manifest_path) or {}).get("project") or {}).get("analysis_unit_id") or "")

    def _update(self, manifest_path: str | Path, change) -> dict[str, Any]:
        manifest = self.store.read(manifest_path) or {}
        change(manifest)
        self.store.write(manifest_path, manifest)
        return manifest

    def _stamp(self) -> str:
        return policy.iso(self.world.clock.now())

    # ---- ports interface ----
    def read_manifest(self, manifest_path: str) -> dict[str, Any] | None:
        return self.store.read(manifest_path)

    def download(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("download", arguments))
        unit = Path(arguments["handoff_path"]).parent.name
        script = self.world.scripts.setdefault(unit, UnitScript())
        outcome = self._next(unit, "download", script.downloads)
        if outcome == "blocked":
            return {"ok": False, "reason": "blocked", "blocking_reasons": ["analysis_input:container_shared_by_samples"]}
        maximum = int(float(arguments["maximum_gb"]) * 1000**3)
        if script.required_bytes > maximum:
            return {"ok": False, "reason": "blocked", "blocking_reasons": ["size_limit:exceeded"],
                    "required_download_bytes": script.required_bytes, "maximum_gb": arguments["maximum_gb"]}
        job = self._job_id("dl")
        workspace = self._workspace(arguments["repository"], arguments["accession"], unit)
        manifest_path = workspace / "provenance" / "run-manifest.json"
        self.store.write(manifest_path, {
            "status": "downloading", "project": {"analysis_unit_id": unit}, "workspace": str(workspace),
            "raw_directory": str(workspace / "raw"), "output_directory": str(workspace / "output"),
            "download_started_at": self._stamp(), "lease_owner": {"job_id": job},
            "raw_retention_policy": arguments["raw_retention_policy"],
            "campaign_authorizations": [{"boundary": 1, "job_id": job, "validated_at": self._stamp(),
                                         "entry_point": "msdial_download_repository_raw"}],
        })
        self.jobs[job] = {"id": job, "kind": "download", "status": "running", "done_at": self._due(script.ticks), "received": 0,
                          "outcome": outcome, "unit": unit, "manifest_path": str(manifest_path), "maximum_bytes": maximum,
                          "remote_bytes": script.remote_bytes, "repository": arguments["repository"]}
        self.download_starts.append(unit)
        return {"ok": True, "job_id": job}

    def _due(self, ticks: int) -> Any:
        return self.world.clock.now() + timedelta(seconds=30 * ticks)

    def settle(self) -> None:
        """End every job whose time is up: the backend works whether or not the runner is watching."""
        for job in list(self.jobs.values()):
            if job["status"] in ("queued", "running") and job["outcome"] not in ("stall", "hold") and self.world.clock.now() >= job["done_at"]:
                self._finish(job, "cancelled" if job.get("cancel") else job["outcome"])
        for job_id, orphan in list(self.orphans.items()):
            if orphan["until"] is not None and self.world.clock.now() >= orphan["until"]:
                del self.orphans[job_id]

    def restart(self, *, orphan_consoles: bool = False) -> None:
        """The backend process stops and starts again: what was running is marked interrupted, as
        server.py does on load, and its Consoles died with it - unless orphan_consoles. On Windows a Console
        is not stopped with the backend that started it (no job object), so it runs on, known only from the
        run attempt in the unit manifest (live_run_attempt), until it ends by itself or is stopped."""
        for job in self.jobs.values():
            if job["status"] in ("queued", "running"):
                job["status"] = "interrupted"
                if orphan_consoles and job["kind"] != "download":
                    self.orphans[job["id"]] = {"unit": job["unit"],
                                               "until": None if job["outcome"] == "hold" else job["done_at"]}

    def forget(self) -> None:
        """The registry no longer holds any job (it keeps only its newest hundred)."""
        self.restart()
        self.jobs.clear()

    def job(self, job_id: str) -> dict[str, Any]:
        self.settle()
        job = self.jobs.get(job_id)
        if job is None:
            return {"ok": False, "reason": "job_not_found", "http_status": 404}
        if job["status"] in ("queued", "running"):
            self._advance(job)
        if job["outcome"] == "shared" and job["status"] in ("queued", "running"):
            return {"ok": True, **{key: value for key, value in job.items() if key not in ("done_at", "outcome")},
                    "status": "waiting_for_shared_download"}
        return {"ok": True, **{key: value for key, value in job.items() if key not in ("done_at", "outcome", "logs")}}

    def job_log(self, job_id: str) -> list[str]:
        """The job's kept log, as InteractivePort.job_log reads it; the poll carries none of it."""
        self.calls.append(("job_log", {"job_id": job_id}))
        return list((self.jobs.get(job_id) or {}).get("logs") or [])

    def _advance(self, job: dict[str, Any]) -> None:
        if job.get("cancel"):
            self._finish(job, "cancelled")
            return
        if job["outcome"] == "stall":
            return
        if job["kind"] == "download" and job["outcome"] != "shared":
            job["received"] += 1000
            if (self.store.read(job["manifest_path"]) or {}).get("status") == "downloading":
                self._update(job["manifest_path"], lambda manifest: manifest.update(download_progress_at=self._stamp()))
        if self.world.clock.now() >= job["done_at"] and job["outcome"] != "hold":
            self._finish(job, job["outcome"])

    def _finish(self, job: dict[str, Any], outcome: str) -> None:
        manifest_path = job["manifest_path"]
        outcome = "ok" if outcome == "shared" else outcome
        if job["kind"] == "download":
            if outcome == "ok" and (self.world.outage or job.get("repository") in self.world.outage_repositories):
                outcome = "fail"
            partial = Path(manifest_path).parent.parent / "raw" / "data" / "S1.mzML.part"
            if outcome not in ("ok", "interrupt"):
                # A lease that fails or is cancelled keeps what arrived for the resume.
                partial.parent.mkdir(parents=True, exist_ok=True)
                partial.write_bytes(b"p" * 100)
            else:
                partial.unlink(missing_ok=True)
            if outcome == "ok" and job.get("remote_bytes", 0) > job.get("maximum_bytes", 0) > 0:
                # The lease streams to maximum_gb and stops (repository_reanalysis: the safety limit).
                error = f"Download exceeded the {job['maximum_bytes']}-byte safety limit."
                self._update(manifest_path, lambda manifest: manifest.update(
                    status="download_failed", download_failure={"reason": error, "error_type": "ValueError"}))
                job.update(status="failed", error=error)
                return
            if outcome == "ok":
                workspace = Path(manifest_path).parent.parent
                (workspace / "raw" / "data").mkdir(parents=True, exist_ok=True)
                (workspace / "raw" / "data" / "S1.mzML").write_bytes(b"x" * 1000)
                self._update(manifest_path, lambda manifest: manifest.update(status="raw_metadata_required", input_candidates=[]))
                job.update(status="completed", result={"manifest_path": manifest_path})
            elif outcome == "cancelled":
                self._update(manifest_path, lambda manifest: manifest.update(status="download_failed"))
                job.update(status="failed", stop_reason="cancelled", error="cancelled")
            elif outcome == "interrupt":
                job.update(status="interrupted")
            elif outcome == "corrupt":
                error = "Checksum mismatch for S1.mzML: the repository declares another md5."
                self._update(manifest_path, lambda manifest: manifest.update(
                    status="download_failed", download_failure={"reason": error, "error_type": "ValueError"}))
                job.update(status="failed", error=error)
            else:
                failure = dict(self.world.network_error["failure"])
                self._update(manifest_path, lambda manifest: manifest.update(status="download_failed", download_failure=failure))
                job.update(status="failed", error=self.world.network_error["job"])
            return
        exit_code = {"ok": 0, "invalid": 0, "timeout": -3, "cancelled": -4, "rt_fail": -1}.get(outcome, 1)
        kind = "tuning" if job["kind"] == "diagnostic" else "run"

        def close(manifest: dict[str, Any]) -> None:
            for item in manifest.get("run_attempts") or []:
                if item.get("job_id") == job["id"]:
                    item.update(ended_at=self._stamp(), exit_code=exit_code)
            if kind == "run" and outcome in ("ok", "late_fail"):
                output = Path(manifest["output_directory"])
                output.mkdir(parents=True, exist_ok=True)
                (output / "result.mztab").write_text("MTD\tmzTab-version\t2.0.0-M\n", encoding="utf-8")
                manifest.update(status="mztab_validated", cleanup_allowed=True,
                                finalized_run={"job_id": job["id"]},
                                mztab_validation={"files": [{"file": str(output / "result.mztab")}]})
            elif kind == "run" and outcome == "invalid":
                # The Console wrote its mzTab-M, and it did not validate.
                output = Path(manifest["output_directory"])
                output.mkdir(parents=True, exist_ok=True)
                (output / "result.mztab").write_text("MTD\tmzTab-version\t2.0.0-M\n", encoding="utf-8")
                manifest.update(status="validation_failed", cleanup_allowed=False)

        self._update(manifest_path, close)
        if outcome == "rt_fail":
            job["logs"] = (["Automatic alignment RT correction: selecting anchors after peak picking and annotation.",
                            "Automatic alignment RT correction failed: Fewer than 3 anchors were found in at least "
                            "50% of the non-blank samples."]
                           + [f"Moved intermediate {index}." for index in range(40)])
            job["error"] = "MS-DIAL Console exited with code -1."
        job.update(status="completed" if exit_code == 0 else "failed", exit_code=exit_code,
                   stop_reason="cancelled" if outcome == "cancelled" else None)

    def cancel(self, job_id: str, reason: str) -> dict[str, Any]:
        self.cancels.append((job_id, reason))
        if job_id in self.jobs:
            self.jobs[job_id]["cancel"] = True
        return {"ok": True, "cancel_requested": True}

    def preflight(self, *, manifest_path: str, extractor_path: str, authorization_path: str,
                  console_path: str = "") -> dict[str, Any]:
        self.calls.append(("preflight", {"manifest_path": manifest_path, "extractor_path": extractor_path,
                                         "authorization_path": authorization_path, "console_path": console_path}))
        if self.world.extractor_refused:
            return {"ok": False, "reason": "raw_metadata_extractor_refused", "codes": ["extractor_not_pinned"],
                    "detail": "raw_metadata_extractor_refused [extractor_not_pinned]: not a pinned build."}
        unit = self._unit_of_manifest(manifest_path)
        script = self.world.scripts.setdefault(unit, UnitScript())
        if script.held:
            # disposition_hold: nothing is read, and the unit keeps whatever disposition it carries.
            return {"completed": False, "extractor_found": True, "preflight_held": {"reason": script.held, "detail": "held"}}
        disposition = script.disposition
        multi = {}
        if disposition == "aif_multi_ce":
            # Interactive 0.5.34: decided for console_path, else the saved setting, and probed for #825.
            console = (console_path if self.world.preflight_takes_console else "") or self.world.saved_console
            ready = bool(console) and console in self.world.consoles_825
            probe = {"capability": "multi_energy_aif_representative_collision_energy", "available": ready,
                     "console_path": console, "console_source": "argument" if console_path else "setting",
                     "console_assembly": "MSDIALCUI.exe", "assembly_sha256": SHA["console"] if ready else "",
                     "probe": "multi_energy_aif_marker" if ready else ("marker_absent" if console else "no_console_configured")}
            multi = {"multi_energy_aif_console": probe}
            if ready:
                multi["aif_multi_ce_run"] = {"collision_energies": [10.0, 20.0], "rule": "multi_ce_aif_with_console_825"}
            disposition = "run" if ready else "aif_hold"
        hold = disposition == "aif_hold"
        if hold:
            disposition = "skip"
        if disposition != "none":
            record = {
                "schema": policy.DISPOSITION_SCHEMA, "disposition": disposition,
                "reasons": [] if disposition in ("run", "split") else
                ["aif_multi_ce_awaiting_console"] if hold else [f"test_{disposition}"],
                "warnings": [], "excluded_inputs": [], "split_key": {"acquisition": True} if disposition == "split" else None,
                "decided_at": self._stamp(),
                "extractor": {"sha256": self.world.extractor_sha, "inventory_sha256": SHA["extractor"],
                              "provenance_status": "verified", "pinned": True},
                "applied": script.applied,
                **({"hold": True} if hold else {}),
                **multi,
            }
            if disposition == "malformed":
                record = {"schema": "other", "disposition": "maybe"}
            self._update(manifest_path, lambda manifest: manifest.update(campaign_disposition=record, status="preflight_passed"))
        return {"completed": True, "extractor_found": True, "status": "preflight_passed"}

    def classify(self, *, manifest_path: str, authorization_path: str, console_path: str = "") -> dict[str, Any]:
        """classify_preflight: the recorded preflight decided again under the approval, and applied."""
        self.calls.append(("classify", {"manifest_path": manifest_path, "authorization_path": authorization_path,
                                        "console_path": console_path}))
        unit = self._unit_of_manifest(manifest_path)
        script = self.world.scripts.setdefault(unit, UnitScript())
        if script.classify_applies:
            self._update(manifest_path, lambda manifest: manifest["campaign_disposition"].update(applied=True))
        return {"ok": True, "applied": script.classify_applies, "held": None, "disposition": script.disposition}

    def split(self, *, manifest_path: str, authorization_path: str) -> dict[str, Any]:
        self.calls.append(("split", {"manifest_path": manifest_path}))
        parent = self.store.read(manifest_path) or {}
        unit = parent["project"]["analysis_unit_id"]
        script = self.world.scripts[unit]
        workspace = Path(parent["workspace"])
        parts = []
        already = parent.get("status") == "split_by_acquisition"
        for mode in script.split_modes:
            part = f"{unit}-{mode}"
            part_workspace = workspace.parent / part
            part_manifest = part_workspace / "provenance" / "run-manifest.json"
            if not part_manifest.is_file():
                self.store.write(part_manifest, {
                    "status": "split_from_parent", "project": {"analysis_unit_id": part}, "workspace": str(part_workspace),
                    "raw_directory": parent["raw_directory"], "output_directory": str(part_workspace / "output"),
                    "split_from": {"analysis_unit_id": unit, "manifest_path": manifest_path},
                })
            self.world.scripts.setdefault(part, UnitScript())
            parts.append({"analysis_unit_id": part, "workspace": str(part_workspace), "manifest_path": str(part_manifest),
                          "acquisition_mode": mode})
            self.split_parts.setdefault(manifest_path, [])
            if str(part_manifest) not in self.split_parts[manifest_path]:
                self.split_parts[manifest_path].append(str(part_manifest))
        self._update(manifest_path, lambda manifest: manifest.update(status="split_by_acquisition"))
        return {"written": not already, "already_split": already, "parts": parts}

    def prepare_metadata(self, *, manifest_path: str, authorization_path: str) -> dict[str, Any]:
        self.calls.append(("prepare_metadata", {"manifest_path": manifest_path}))
        manifest = self.store.read(manifest_path) or {}
        output = Path(manifest["output_directory"])
        output.mkdir(parents=True, exist_ok=True)
        csv = output / "analysis_files.csv"
        csv.write_text("file_path,acquisition_type\nS1.mzML,DDA\n", encoding="ascii")
        seed = {"parameter_strategy": "auto_peak_range", "project_type": "lcms", "ion_mode": "Positive",
                "output_root": str(output), "workflow_overrides": {"repository_run_manifest": manifest_path}}
        script = self.world.scripts.get(self._unit_of_manifest(manifest_path)) or UnitScript()
        preview: dict[str, Any] = {"answer_seed": seed}
        if not script.no_order_record:
            header = script.order_source == "raw_header_acquisition_start_time"
            preview["analytical_order"] = {"derived_from": script.order_source if header else None,
                                           "order_source": script.order_source, "files_recorded": 1}
        return {"prepared": True, "input_path": str(csv), "preview": preview}

    def _console(self, kind: str, *, input_path: str, answers: dict[str, Any], authorization_path: str,
                 timeout_seconds: float, idle_timeout_seconds: float) -> dict[str, Any]:
        manifest_path = answers["workflow_overrides"]["repository_run_manifest"]
        unit = self._unit_of_manifest(manifest_path)
        self.calls.append((kind, {"answers": answers, "timeout_seconds": timeout_seconds,
                                  "idle_timeout_seconds": idle_timeout_seconds, "unit": unit}))
        self.settle()
        for job in self.jobs.values():
            if job.get("unit") == unit and job["kind"] != "download" and job["status"] in ("queued", "running"):
                return {"ok": False, "reason": "unit_busy", "live_job_id": job["id"]}
        for job_id, orphan in self.orphans.items():
            # Interactive's single-flight reads the manifest's run attempts too, and names the orphan's job.
            if orphan["unit"] == unit:
                return {"ok": False, "reason": "unit_busy", "live_job_id": job_id}
        running = [job["id"] for job in self.jobs.values() if job["kind"] != "download" and job["status"] in ("queued", "running")]
        if running or self.orphans:
            self.overlapping_starts.append((unit, running + list(self.orphans)))
        script = self.world.scripts.setdefault(unit, UnitScript())
        outcome = self._next(unit, kind, script.diagnostics if kind == "diagnostic" else script.runs)
        job_id = self._job_id("dg" if kind == "diagnostic" else "rn")
        entry = "peak_count_diagnostic" if kind == "diagnostic" else "agent_run"
        attempt_kind = "tuning" if kind == "diagnostic" else "run"

        def record(manifest: dict[str, Any]) -> None:
            manifest.setdefault("campaign_authorizations", []).append(
                {"boundary": 4, "entry_point": entry, "job_id": job_id, "validated_at": self._stamp()}
            )
            manifest.setdefault("run_attempts", []).append(
                {"kind": attempt_kind, "job_id": job_id, "started_at": self._stamp(), "ended_at": None,
                 "backend": {"pid": 1}}
            )

        self._update(manifest_path, record)
        final = {"lose_reply": "ok", "busy_reply": "ok"}.get(outcome, outcome)
        self.jobs[job_id] = {"id": job_id, "kind": kind, "status": "running", "done_at": self._due(self.world.scripts[unit].ticks),
                             "outcome": final, "unit": unit, "manifest_path": manifest_path}
        self.console_starts.append((unit, kind))
        if outcome == "lose_reply":
            return {"ok": False, "reason": "os_error", "detail": "timed out"}
        if outcome == "busy_reply":
            return {"ok": False, "reason": "unit_busy", "live_job_id": job_id}
        return {"started": True, "job_id": job_id}

    def start_diagnostic(self, **arguments: Any) -> dict[str, Any]:
        return self._console("diagnostic", **arguments)

    def start_run(self, **arguments: Any) -> dict[str, Any]:
        return self._console("run", **arguments)

    def estimate(self, *, job_id: str, manifest_path: str, minimum: int, maximum: int, step: int) -> dict[str, Any]:
        self.calls.append(("estimate", {"job_id": job_id, "step": step}))
        job = self.jobs.get(job_id)
        if job is None or job["status"] != "completed":
            return {"ready": False}
        family = self.world.instrument_family
        if self.legacy_estimate:
            chosen = step or 100
            estimate = {"minimum_peak_height": 12 * chosen, "diagnostic_peak_count": 8000, "threshold_step": chosen}
        else:
            named = family.casefold()
            chosen = 1000 if "fourier" in named or "ft-icr" in named or "fticr" in named else 100
            requested = step or None
            estimate = {
                "minimum_peak_height": 12 * chosen, "diagnostic_peak_count": 8000, "threshold_step": chosen,
                "coarse_threshold_step": chosen, "fine_threshold_step": chosen // 10, "step_fallback": False,
                "fallback_reason": None, "instrument_family": family, "requested_threshold_step": requested,
                "requested_step_disposition": (None if requested is None else "family_step" if requested == chosen
                                               else "recorded_only"),
            }
        if self.step_fallback:
            estimate.update(minimum_peak_height=12 * (chosen // 10), threshold_step=chosen // 10,
                            coarse_threshold_step=chosen, step_fallback=True, fallback_reason="no_coarse_step_in_range")
        estimate.update(self.estimate_patch)
        return {"ready": True, "representative": {"instrument_family": family,
                                                  "instrument_family_source": self.world.instrument_family_source,
                                                  "file_name": "QC_05.mzML", "selection_reason": "QC-nearest-run-midpoint"},
                "estimate": estimate}

    def prepare_guided(self, *, input_path: str, answers: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("prepare_guided", {"answers": answers}))
        manifest_path = (answers.get("workflow_overrides") or {}).get("repository_run_manifest") or ""
        script = self.world.scripts.get(self._unit_of_manifest(manifest_path)) or UnitScript()
        validation = [{"level": "warning", "message": message} for message in script.plan_warnings]
        return {"plan": {"validation": validation, "ready_to_prepare": True}, "preparation": {}, "messages": []}

    def qa(self, *, manifest_path: str) -> dict[str, Any]:
        return {"ok": True}

    def publication(self, *, manifest_path: str, run_qa: bool) -> dict[str, Any]:
        self.calls.append(("publication", {"run_qa": run_qa}))
        return {"ok": True}

    def data_handoff(self, *, job_id: str) -> dict[str, Any]:
        return {"ok": True}

    def cleanup(self, *, manifest_path: str, authorization_path: str) -> dict[str, Any]:
        self.calls.append(("cleanup", {"manifest_path": manifest_path}))
        unit = self._unit_of_manifest(manifest_path)
        if self.world.scripts.get(unit, UnitScript()).cleanup == "blocked":
            return {"ok": True, "deleted": False, "blockers": ["finalisation_held"]}
        if self.world.scripts.get(unit, UnitScript()).cleanup == "unsupported":
            return {"ok": False, "reason": "unsupported", "detail": "msdial_cleanup_repository_raw takes no campaign_authorization_path"}
        manifest = self.store.read(manifest_path) or {}
        shutil.rmtree(manifest["raw_directory"], ignore_errors=True)
        self._update(manifest_path, lambda current: current.update(status="raw_cleaned"))
        return {"ok": True, "deleted": True}

    def discard_blockers(self, manifest: dict[str, Any]) -> list[str]:
        """What discard_download_lease refuses on, as InteractivePort.discard_blockers reads it."""
        codes = []
        status = manifest.get("status")
        if status in ("mztab_validated", "completed", "raw_cleaned"):
            codes.append("validated_status")
        if status == "downloading" and self.lease_state(manifest) != "gone":
            codes.append("lease_live")
        output = Path(str(manifest.get("output_directory") or ""))
        if str(manifest.get("output_directory") or "") and output.is_dir() and any(
            path.is_file() and path.suffix.casefold() == ".mztab" for path in output.rglob("*")
        ):
            codes.append("mztab_output_exists")
        if any("raw_deletion" in (hold.get("blocks") or []) for hold in manifest.get("finalisation_holds") or []):
            codes.append("finalisation_held")
        if any(self._console_alive(item) for item in manifest.get("run_attempts") or []):
            codes.append("console_live")
        return codes

    def _console_alive(self, attempt: dict[str, Any]) -> bool:
        job = self.jobs.get(attempt.get("job_id"))
        return attempt.get("job_id") in self.orphans or (
            not attempt.get("ended_at") and job is not None and job["status"] in ("queued", "running"))

    @staticmethod
    def held_by_disposition(manifest: dict[str, Any]) -> bool:
        """Interactive f225e9b's unreleased_disposition_hold: an applied skip with hold true, unless an operator's
        skip lifted it (disposition_hold_released_by operator_skip). A discard that did not record the release
        leaves it held, discarded or not (review r9-64)."""
        record = manifest.get("campaign_disposition") or {}
        return (manifest.get("disposition_hold_released_by") != "operator_skip" and record.get("applied") is True
                and record.get("disposition") == "skip" and record.get("hold") is True)

    def _held_parts(self, parent_path: str) -> list[str]:
        return [path for path in self.split_parts.get(parent_path, [])
                if self.held_by_disposition(self.store.read(path) or {})]

    def _unsupported_release(self, release: bool) -> dict[str, Any] | None:
        if release and not self.release_hold_supported:
            return {"ok": False, "reason": "unsupported", "detail": "takes no release_disposition_hold"}
        return None

    def discard(self, *, manifest_path: str, authorization_path: str, unit_id: str, parent_unit_id: str = "",
                release_disposition_hold: bool = False) -> dict[str, Any]:
        """As Interactive discards under the agreed contract of 2026-10-07: a unit or split part its campaign
        disposition holds is never discarded without release_disposition_hold, approval or not; with it the
        discard proceeds and records disposition_hold_released_by operator_skip. A split part's discard deletes
        nothing (its raw data are its parent's) and answers part_ended; a split parent's is its release."""
        self.calls.append(("discard", {"manifest_path": manifest_path, "unit_id": unit_id,
                                       "release_disposition_hold": release_disposition_hold}))
        unsupported = self._unsupported_release(release_disposition_hold)
        if unsupported:
            return unsupported
        manifest = self.store.read(manifest_path) or {}
        if manifest_path in self.split_parts:
            result = self._release_parent(manifest_path, release_disposition_hold)
            if result.get("deleted"):
                self._update(manifest_path, lambda current: current.update(status="discarded"))
            return result
        blockers = self.discard_blockers(manifest)
        held = self.held_by_disposition(manifest)
        if held and not release_disposition_hold:
            blockers.append("disposition_held")
        if blockers:
            return {"ok": True, "deleted": False, "blockers": blockers, "detail": "Interactive would refuse: " + ", ".join(blockers)}

        def ended(current: dict[str, Any]) -> None:
            current["status"] = "discarded"
            if held:
                current["disposition_hold_released_by"] = "operator_skip"

        if manifest.get("split_from"):
            self._update(manifest_path, ended)
            return {"ok": True, "deleted": False, "part_ended": True, "raw_release_deferred_to": manifest["split_from"]["manifest_path"]}
        shutil.rmtree(manifest["raw_directory"], ignore_errors=True)
        self._update(manifest_path, ended)
        return {"ok": True, "deleted": True}

    def release_split_parent(self, *, manifest_path: str, authorization_path: str,
                             release_disposition_hold: bool = False) -> dict[str, Any]:
        self.calls.append(("release_split_parent", {"manifest_path": manifest_path,
                                                    "release_disposition_hold": release_disposition_hold}))
        if not self.split_release_supported:
            return {"ok": False, "reason": "unsupported", "detail": "no split-parent release"}
        unsupported = self._unsupported_release(release_disposition_hold)
        if unsupported:
            return unsupported
        return self._release_parent(manifest_path, release_disposition_hold)

    def _release_parent(self, manifest_path: str, release: bool) -> dict[str, Any]:
        """A held split part keeps its parent's raw data until it is released or run. As Interactive f225e9b's
        cleanup_split_parent and _part_end do, a release with release_disposition_hold lifts the hold of EVERY
        part still held (hold_released), recording disposition_hold_released_by operator_skip on each, whether
        or not an operator skipped that part; without it, any part still held refuses the release."""
        held = self._held_parts(manifest_path)
        if held and not release:
            return {"ok": True, "deleted": False, "blockers": ["disposition_held"],
                    "detail": f"{len(held)} part(s): its campaign disposition holds it"}
        for path in held:
            self._update(path, lambda current: current.update(status="discarded", disposition_hold_released_by="operator_skip"))
        manifest = self.store.read(manifest_path) or {}
        shutil.rmtree(manifest["raw_directory"], ignore_errors=True)
        return {"ok": True, "deleted": True}

    def live_attempt(self, manifest_path: str) -> dict[str, Any] | None:
        self.settle()
        for item in reversed((self.store.read(manifest_path) or {}).get("run_attempts") or []):
            if self._console_alive(item):
                return item
        return None

    def lease_state(self, manifest: dict[str, Any]) -> str:
        job = self.jobs.get((manifest.get("lease_owner") or {}).get("job_id"))
        return "alive" if job is not None and job["status"] in ("queued", "running") else "gone"

    def backend_alive(self, attempt: dict[str, Any]) -> bool:
        """An orphan's backend is the one that restarted, unless the test says it still runs."""
        return self.orphan_backend_alive if attempt.get("job_id") in self.orphans else True

    def kill_orphan(self, attempt: dict[str, Any]) -> bool:
        self.calls.append(("kill_orphan", {"job_id": attempt.get("job_id")}))
        if attempt.get("job_id") in self.orphans and not self.orphan_unkillable:
            del self.orphans[attempt["job_id"]]
            self.kills.append(attempt["job_id"])
            return True
        return False


class FakeCatalog:
    def __init__(self, world: "World") -> None:
        self.world = world
        self.saved: list[dict[str, Any]] = []
        self.runs: dict[str, dict[str, Any]] = {}
        self.drift: set[str] = set()

    def class_decision(self, unit_id: str, purpose: str) -> dict[str, Any]:
        digest = self.world.class_digest(unit_id) + ("-drifted" if unit_id in self.drift else "")
        return {"kind": "abstention", "proposal_id": digest, "selected_fields": [], "assignment_count": 3}

    def save_class(self, *, unit_id: str, purpose: str, kind: str, ratification: dict[str, Any]) -> dict[str, Any]:
        self.saved.append({"unit_id": unit_id, "kind": kind, "ratification": ratification})
        return {"ok": True, "proposal_id": ratification["proposal_id"]}

    def handoff(self, *, unit_id: str, class_proposal_id: str) -> dict[str, Any]:
        path = self.world.root / "catalog-data" / "handoffs" / f"{unit_id}.json"
        _write(path, {"analysis_unit_id": unit_id, "class_proposal_id": class_proposal_id})
        return {"ok": True, "handoff_path": str(path), "blocking_reasons": []}

    def copy_handoff(self, response: dict[str, Any], destination: Path) -> dict[str, Any]:
        return ports.copy_handoff(response, destination)

    def record_run(self, **values: Any) -> dict[str, Any]:
        self.runs[values["run_id"]] = values
        return {"ok": True, "recorded": True}


class FakeGate:
    """The gate, as GatePort reads its --json report. `fails` names the checks a point FAILs, by point or by
    (unit, point) (by default
    SUM-1 at an exit 2 of pre_cleanup and final, and none before production); `unevaluated` names, the same
    way, the checks it leaves not evaluable where they are required; `run_policy` is each check's run_policy
    when the gate states one, and while it is empty the report states none. `no_report`, by point or by
    (unit, point), ends a run as GatePort reads a gate that gave no usable report: "timeout", "error" (a
    crash, an exit code the gate does not define), "exit_3", "unparsable", or "raise" (the port raises)."""

    def __init__(self) -> None:
        self.exits = {"before_production": 0, "pre_cleanup": 4, "final": 4}
        self.fails: dict[Any, list[str]] = {}
        self.unevaluated: dict[Any, list[str]] = {}
        self.run_policy: dict[str, str] = {}
        self.no_report: dict[Any, str] = {}
        self.runs: list[tuple[str, str]] = []
        self.detail = ""

    def run(self, workspace: str, point: str, report_path: Path) -> dict[str, Any]:
        unit = Path(workspace).name
        self.runs.append((unit, point))
        verdict = {"stage": ports.GATE_STAGES[point], "strict": True, "gate_commit": "fake", "report_parsed": False}
        ending = self.no_report.get((unit, point), self.no_report.get(point, ""))
        if ending == "raise":
            raise OSError("the gate's interpreter could not be started")
        if ending == "timeout":
            return {**verdict, "outcome": "timeout", "detail": "The gate did not finish within 1200 s."}
        if ending in ("error", "exit_3", "unparsable"):
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_bytes(b"" if ending == "exit_3" else b"Traceback (most recent call last):\n")
            exit_code = {"error": None, "exit_3": 3, "unparsable": 2}[ending]
            return {**verdict, "outcome": "error" if exit_code is None else "ran", "exit_code": exit_code,
                    "report_path": str(report_path), "detail": f"fake gate: {ending}"}
        exit_code = self.exits[point]
        default = ["SUM-1"] if exit_code == 2 and point != "before_production" else []
        fails = self.fails.get((unit, point), self.fails.get(point, default))
        unevaluated = self.unevaluated.get((unit, point), self.unevaluated.get(point, []))

        def stated(check: str) -> dict[str, str]:
            return {"run_policy": self.run_policy[check]} if check in self.run_policy else {}

        report = {"checks": [{"check_id": check, "status": "fail", **stated(check)} for check in fails]
                  + [{"check_id": check, "status": "not_evaluable", "required": True, **stated(check)} for check in unevaluated]}
        if self.run_policy:
            report["checks"] += [{"check_id": check, "status": "pass", "run_policy": rule}
                                 for check, rule in self.run_policy.items() if check not in fails and check not in unevaluated]
        report["strict_failures"] = list(unevaluated) + (["READ-1"] if exit_code == 4 else [])
        report_path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps({"point": point, "exit": exit_code, **report}).encode()
        report_path.write_bytes(data)
        blocking, source = policy.run_blocking_failures(report)
        return {**verdict, "outcome": "ran", "report_parsed": True,
                "exit_code": exit_code, "fail_ids": sorted(fails), "blocking_fail_ids": blocking, "run_policy_source": source,
                "blocking_unevaluated_ids": policy.run_blocking_unevaluated(report),
                "run_policy_mismatches": policy.run_policy_mismatches(report),
                "strict_hold_ids": report["strict_failures"], "stage_reached": "B10",
                "report_path": str(report_path), "report_sha256": hashlib.sha256(data).hexdigest(),
                **({"detail": self.detail} if self.detail else {})}


class FakePins:
    def __init__(self, recorded: dict[str, Any]) -> None:
        self.values = json.loads(json.dumps(recorded))

    def current(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.values))


class World:
    """One campaign on a temporary directory, with its fakes and its ledger."""

    def __init__(self, root: Path, unit_ids: list[str], *, policy_values: dict[str, Any] | None = None,
                 retention: str = "delete_after_validated_output", covers: tuple[str, ...] = ("1", "3", "4", "5", "split"),
                 known_bytes: int = 10 * 1000**3, size_known: bool = True, legacy_policy: bool = False) -> None:
        self.root = root
        self.workspace_root = root / "analysis"
        self.directory = self.workspace_root / "_campaigns" / "test-campaign"
        self.directory.mkdir(parents=True)
        self.clock = FakeClock()
        self.disk = FakeDisk()
        self.scripts: dict[str, UnitScript] = {}
        # While set, every download that would have finished fails with HTTP 503: the repository is down. The
        # repositories named in outage_repositories are down while the others are up.
        self.outage = False
        self.outage_repositories: set[str] = set()
        # How a download that fails on the network ends: the job's error (server.py: str(error)) and the
        # download_failure the lease writes into the unit manifest, the only place its type is kept.
        self.network_error: dict[str, Any] = {
            "job": "HTTP 503", "failure": {"reason": "HTTP Error 503: Service Unavailable", "error_type": "HTTPError"},
        }
        # While set, Interactive refuses every campaign preflight: the extractor is not a verified, pinned build.
        self.extractor_refused = False
        # The Consoles with MsdialWorkbench #825 (Interactive 0.5.34's probe finds its markers), and the Console
        # Interactive's saved setting names, which a preflight given no console_path decides for.
        self.consoles_825: set[str] = set()
        self.saved_console = ""
        # While false, a preflight decides for the saved Console whatever console_path it is sent, as Interactive
        # did for a runner that sent none.
        self.preflight_takes_console = True
        self.extractor_sha = SHA["extractor"]
        self.instrument_family = "QTOF"
        self.instrument_family_source = "mzml_instrument_configuration"
        self.private_directory = root / "private libraries" / "vault"
        self.private_directory.mkdir(parents=True)
        self.libraries = {}
        for name in (POSITIVE_MSP, NEGATIVE_MSP, LBM):
            path = self.private_directory / name
            path.write_text("NAME: synthetic\n", encoding="utf-8")
            self.libraries[name] = str(path)
        manifest_path = self.directory / "campaign-manifest.json"
        manifest_path.write_bytes(b"{}")
        self.digest = "sha256:" + hashlib.sha256(b"{}").hexdigest()
        authorization = self.directory / "campaign-authorization.json"
        authorization.write_text(json.dumps({"schema": "msdial-campaign-authorization.v1", "approval_id": "approval-1"}), encoding="utf-8")
        values = {"poll_seconds": 30.0, **(policy_values or {})}
        self.pins = {
            "console": {"path": "C:/fake/MSDIALCUI.exe", "exists": True, "binary_sha256": SHA["console"],
                        "assembly_sha256": SHA["console"], "inventory_sha256": SHA["console"]},
            "extractor": {"path": "C:/fake/RawMetadataConsoleApp.exe", "exists": True, "binary_sha256": SHA["extractor"],
                          "inventory_sha256": SHA["extractor"]},
            "libraries": [{"name": POSITIVE_MSP, "sha256": SHA["positive"], "bytes": 16},
                          {"name": NEGATIVE_MSP, "sha256": SHA["negative"], "bytes": 16},
                          {"name": LBM, "sha256": SHA["lbm"], "bytes": 16}],
            "interactive": {"version": "0.5.16", "commit": "a" * 40, "dirty": False},
            "catalog": {"version": "0.6.1", "commit": "b" * 40, "dirty": False},
            "gate": {"commit": "c" * 40, "dirty": False},
        }
        self.campaign = {
            "campaign_id": "test-campaign", "pool": "declared", "manifest_path": str(manifest_path),
            "manifest_digest": self.digest, "analysis_purpose": "annotation of every experimental spectrum",
            "workspace_root": str(self.workspace_root), "raw_retention_policy": retention,
            "catalog_database": str(root / "catalog.sqlite"), "authorization_path": str(authorization),
            "authorization_sha256": SHA["auth"], "policy": {
                key: value for key, value in policy.CampaignPolicy.from_dict(values).as_dict().items()
                # legacy_policy: a policy recorded before the automatic RT correction fields (2026-10-07).
                if not (legacy_policy and key.startswith("automatic_rt_correction"))
            },
            "profile": {
                "schema": "msdial-campaign-profile.v1",
                "answers": {"library_strategy": "existing", "use_retention_time_for_annotation": False},
                "by_ion_mode": {
                    "Positive": {"libraries": {"msp_paths": [f"library:{POSITIVE_MSP}"], "lbm_path": f"library:{LBM}"}},
                    "Negative": {"libraries": {"msp_paths": [f"library:{NEGATIVE_MSP}"], "lbm_path": f"library:{LBM}"}},
                },
            },
            "pins": self.pins,
        }
        self.approval = {"approval_id": "approval-1", "manifest_digest": self.digest, "approved_by": "Test Person",
                         "approved_at": "2026-10-01T00:00:00+00:00", "statement": "Approved for the test.", "covers": list(covers)}
        self.units = [
            {"unit_key": unit, "catalog_unit_id": unit, "repository": "metabolights", "accession": f"MTBLS{index + 9000}",
             "order_index": index, "download_group_id": f"group-{index}", "approved_class_digest": self.class_digest(unit),
             "class_kind": "abstention", "known_bytes": known_bytes, "size_known": int(size_known), "has_archive": 0,
             "instrument": "Agilent 6545 Q-TOF", "ion_mode": "Positive"}
            for index, unit in enumerate(unit_ids)
        ]
        self.groups = [{"group_id": f"group-{index}", "unit_count": 1, "known_bytes": known_bytes, "size_known": size_known}
                       for index in range(len(unit_ids))]
        self.ledger_path = self.directory / "ledger.sqlite"
        with ledger_module.Ledger(self.ledger_path, durable=False) as book:
            book.create(self.campaign, self.approval, self.units, self.groups, "2026-10-01T00:00:00+00:00")
        self.interactive = FakeInteractive(self)
        self.catalog = FakeCatalog(self)
        self.gate = FakeGate()
        self.pin_source = FakePins(self.pins)

    @staticmethod
    def class_digest(unit_id: str) -> str:
        return hashlib.sha256(("class:" + unit_id).encode()).hexdigest()[:20]

    def ports(self) -> machine.Ports:
        return machine.Ports(interactive=self.interactive, catalog=self.catalog, gate=self.gate, disk=self.disk,
                             clock=self.clock, pins=self.pin_source)

    def open(self, commit_hook=None) -> ledger_module.Ledger:
        return ledger_module.Ledger(self.ledger_path, commit_hook=commit_hook, durable=False)

    def runner(self, book: ledger_module.Ledger) -> machine.Runner:
        return machine.Runner(book, self.ports(), resources={"libraries": self.libraries})

    def run(self, *, max_iterations: int = 2000, commit_hook=None) -> dict[str, Any]:
        with self.open(commit_hook) as book:
            return self.runner(book).run(until_idle=True, max_iterations=max_iterations)
