"""The per-unit state machine: what each unit does next, how a crash is resumed, what is reported.

ONE UNIT IN HAND. The runner advances the units in the approved order, with at most 1 + prefetch of them
between their Class decision and their end (prefetch is 0 by default), and never more than one MS-DIAL
Console at a time: the ledger's console_slot is a singleton, and Interactive refuses a second Console on
one unit. A unit that waits - for a retry, for disk, for its turn as a split part - leaves the hand, so
the next unit starts: a unit failure never stops the campaign. A unit waiting for disk holds back only
new work behind it; a retry or a split part, which already has its raw data and is what frees space,
still enters the hand.

EVERY STEP IS A COMMITTED TRANSITION. A step opens an attempt (committed), makes its one call, and then
commits the unit's next state, the attempt's end and whatever else records why, in one transaction.
Nothing between two commits is left undone by a crash except that one call, and `reconcile` finds out
whether it happened: a download is found by the lease its manifest records, a Console start by the run
attempt and the boundary-4 crossing Interactive writes into the unit manifest before the job exists. So
a crash at any committed transition resumes without a second download or a second Console
(tests/test_campaign_resume.py stops the runner at every commit and checks it).

WHAT THIS MODULE NEVER DECIDES. Whether a unit may run: that is Interactive's campaign_disposition,
read after the raw-header preflight (policy.read_disposition), and acted on only once Interactive
applied it under the approval (0.5.17). The Class digest, the libraries, the Console and the extractor
were fixed by the approved manifest; a change pauses the campaign.

THE RULES are the user's, as policy.py writes them down: a failed unit is retried twice and then its raw
data are deleted; raw data are deleted once the outputs are produced and the mzTab-M validates, with the
gate verdict recorded beside the deletion, whatever it is; skipped and excluded units' raw data are
deleted too; a short disk pauses the campaign; a before-production FAIL stops the unit's run only for a
check that breaks results (blocks_run), and the unit then counts as failed, while any other FAIL is
recorded and the unit runs. Every production attempt is recorded in the Catalog as it ends. Raw data
the rules delete and Interactive would not are "held", counted apart (summary), never reported as kept
or deleted. The runner never records a person's reading (boundary 6): READ-1 holding the gate at exit 4
is a state it stores, and such a unit is reported as "outputs produced", never "completed".
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import ledger as ledger_module
from . import policy

UNIT_RECORD_SCHEMA = "msdial-campaign-unit-record.v1"
STATUS_SCHEMA = "msdial-campaign-status.v1"
IN_FLIGHT = ("downloading", "diagnosing", "running")
IDLE = ("pending",) + ledger_module.WAITING_STATES
# Pauses under which nothing new starts; jobs already running are still watched to their end.
BLOCKING_PAUSES = ("operator", "contract", "pin", "fault")
# A unit's end once its outputs exist, and the discard that ends one without them. A step here that raises
# is tried again later without counting against the unit, still ending as it was going to: nothing after
# the outputs exist turns a unit into a failed one, or rewrites what became of its raw data.
END_STATES = ("run_done", "published", "gated", "releasing", "discarding")
JOB_COLUMN = {"downloading": "download_job_id", "diagnosing": "diagnostic_job_id", "running": "run_job_id"}
# Interactive's entry points that write a boundary-4 crossing, with the job id, before the job exists.
CONSOLE_ENTRY_POINT = {"tuning": "peak_count_diagnostic", "run": "agent_run"}
# Lease statuses that are not a finished download.
UNFINISHED_LEASE = frozenset({"downloading", "download_failed", "discarded"})
# A download job waiting for another unit's lease to fetch an object they share (plan item 15's store): it
# moves no bytes of its own meanwhile, so it is neither stalled nor ended.
WAITING_FOR_SHARED = "waiting_for_shared_download"
TSV_COLUMNS = (
    "unit_key", "catalog_unit_id", "repository", "accession", "role", "parent_unit_key", "state",
    "terminal_reason", "report_terms", "outputs_produced", "gate_exit_pre", "gate_exit_final",
    "raw_disposition", "failures", "interruptions", "run_job_id", "minimum_peak_height", "threshold_step",
    "workspace",
)


@dataclass
class Ports:
    """What the machine acts through. tests/ pass fakes with the same methods; scripts/campaign-runner.py
    passes ports.InteractivePort, CatalogPort, GatePort, LocalDisk, SystemClock, PinReader and
    BackendSupervisor."""

    interactive: Any
    catalog: Any
    gate: Any
    disk: Any
    clock: Any
    pins: Any
    backend: Any = None


def _loads(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _merge(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """base updated by extra, nested objects merged key by key."""
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_library_references(value: Any, libraries: Mapping[str, str]) -> Any:
    """Replace every "library:<file name>" in a profile with where that library is on this machine.

    The approved profile names libraries by file name only; the location comes from the git-ignored
    resource map at run time and goes nowhere but the call to Interactive.
    """
    if isinstance(value, str) and value.startswith("library:"):
        name = value[len("library:"):]
        if name not in libraries:
            raise KeyError(f"The profile names library {name}, which the resource map does not locate.")
        return str(libraries[name])
    if isinstance(value, Mapping):
        return {key: resolve_library_references(item, libraries) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_library_references(item, libraries) for item in value]
    return value


def started_console_job(
    manifest: Mapping[str, Any] | None, kind: str, since: datetime, known: Iterable[str] = ()
) -> str:
    """The job a Console start made for this unit at or after `since`, from the unit manifest, or ''.

    Interactive writes the boundary-4 crossing with the job id into the manifest before the job exists,
    and opens a run attempt before the Console starts; either is proof that the start call happened,
    whatever became of its reply. A job the ledger already knows for the unit is an earlier start's.
    """
    manifest = manifest or {}
    known = set(known)
    for item in reversed(list(manifest.get("run_attempts") or [])):
        started = policy.parse_iso(item.get("started_at")) if isinstance(item, Mapping) else None
        if started and started >= since and item.get("kind") == kind and item.get("job_id") and item["job_id"] not in known:
            return str(item["job_id"])
    for item in reversed(list(manifest.get("campaign_authorizations") or [])):
        if not isinstance(item, Mapping):
            continue
        validated = policy.parse_iso(item.get("validated_at"))
        if (
            validated and validated >= since and str(item.get("boundary")) == "4"
            and item.get("entry_point") == CONSOLE_ENTRY_POINT[kind] and item.get("job_id")
            and item["job_id"] not in known
        ):
            return str(item["job_id"])
    return ""


def started_download_job(manifest: Mapping[str, Any] | None, since: datetime, known: Iterable[str] = ()) -> str:
    """The download job whose lease this unit's manifest records as begun at or after `since`, or ''."""
    manifest = manifest or {}
    started = policy.parse_iso(manifest.get("download_started_at"))
    job = str((manifest.get("lease_owner") or {}).get("job_id") or "")
    return job if job and started and started >= since and job not in set(known) else ""


def _group(unit: Mapping[str, Any]) -> str:
    """The unit's download group: the units that share download objects."""
    return str(unit.get("download_group_id") or unit["unit_key"])


def _downloads_next(unit: Mapping[str, Any]) -> bool:
    """Whether the unit's next call to a repository is its download: it holds no raw data yet."""
    state = unit["resume_state"] if unit["state"] in ledger_module.WAITING_STATES else unit["state"]
    return state in ("pending", "class_settled", "handoff_ready")


def thermo_raw_inputs(manifest: Mapping[str, Any] | None) -> int:
    """Thermo .raw inputs are files; a Waters .raw is a folder."""
    count = 0
    for item in (manifest or {}).get("input_candidates") or []:
        text = str(item)
        if text.casefold().endswith(".raw") and os.path.isfile(text):
            count += 1
    return count


class Runner:
    """Drives one campaign's units from the ledger. Rebuilt from the ledger alone after a crash."""

    # Consecutive polls of one job that raise before the job is given up (cancelled, and the unit's
    # attempt counted as failed); at the 30 s poll that is five minutes of a job nobody can read.
    POLL_ERROR_LIMIT = 10

    def __init__(
        self,
        ledger: ledger_module.Ledger,
        ports: Ports,
        *,
        resources: Mapping[str, Any] | None = None,
        stop: Callable[[], bool] | None = None,
    ) -> None:
        self.ledger = ledger
        self.ports = ports
        self.campaign = ledger.campaign()
        self.approval = ledger.approval()
        self.policy = policy.CampaignPolicy.from_dict(self.campaign["policy"])
        self.pins = self.campaign["pins"]
        self.profile = self.campaign["profile"]
        self.libraries = dict((resources or {}).get("libraries") or {})
        self.redact = policy.redactor(self.libraries)
        self.directory = Path(self.campaign["manifest_path"]).parent
        self.stop = stop or (lambda: False)
        self._progress: dict[str, tuple[int, datetime]] = {}
        self._poll_errors: dict[str, int] = {}
        # Jobs this process has sent a cancel for. A cancel the ledger records and this process has not sent
        # (a crash between the two) is sent at the job's next poll.
        self._cancels_sent: set[str] = set()
        # Orphaned Consoles this process has tried to stop, and when: one that would not stop is tried again
        # only after busy_retry_seconds (a kill can wait ten seconds for the process), and recorded once.
        self._orphan_kills: dict[str, datetime] = {}
        self._orphans_recorded: set[tuple[str, bool]] = set()

    # ---- time and small helpers ------------------------------------------------------------------

    def now(self) -> datetime:
        return self.ports.clock.now()

    def stamp(self) -> str:
        return policy.iso(self.now())

    def _paused(self) -> str | None:
        runner = self.ledger.runner()
        return runner["pause_kind"] if runner["paused"] else None

    def _pause(self, kind: str, reason: str) -> None:
        self.ledger.pause(kind, str(self.redact(reason)), self.stamp())

    def _event(self, kind: str, detail: Mapping[str, Any], unit_key: str | None = None) -> None:
        self.ledger.event(kind, self.stamp(), self.redact(dict(detail)), unit_key=unit_key)

    def _workspace(self, unit: Mapping[str, Any]) -> Path:
        if unit.get("workspace"):
            return Path(unit["workspace"])
        root = Path(self.campaign["workspace_root"])
        return root / unit["repository"] / unit["accession"] / unit["catalog_unit_id"]

    def _manifest(self, unit: Mapping[str, Any]) -> dict[str, Any] | None:
        return self.ports.interactive.read_manifest(unit.get("manifest_path") or "")

    def _unit_directory(self, unit: Mapping[str, Any]) -> Path:
        return self.directory / "units" / unit["unit_key"]

    def _authorization(self, unit: Mapping[str, Any]) -> str:
        return str(unit.get("authorization_copy") or self.campaign["authorization_path"])

    def _known_jobs(self, unit_key: str) -> set[str]:
        jobs = {row["job_id"] for row in self.ledger.attempts(unit_key) if row["job_id"]}
        jobs |= {row["job_id"] for row in self.ledger.console_runs(unit_key) if row["job_id"]}
        return jobs

    def _live(self, boundary: str) -> bool:
        return self.ledger.live_approval(boundary) is not None

    def _require(self, boundary: str) -> None:
        """Before a call that crosses a boundary, not only at the transition that records it: a revoked
        approval must stop the call itself, whatever the copy Interactive is given still says."""
        if not self._live(boundary):
            raise ledger_module.ApprovalMissing(f"No live approval covers boundary {boundary}.")

    def _move(self, unit: Mapping[str, Any], to_state: str, **kwargs: Any) -> dict[str, Any]:
        """A transition from the state this unit was read in, with everything it records redacted: its
        detail, the attempt it opens or closes (a job's error can quote a file) and the gate verdict (whose
        detail is the gate's own stderr tail)."""
        for key in ("detail", "terminal_detail", "raw_detail"):
            if kwargs.get(key) is not None:
                kwargs[key] = self.redact(kwargs[key])
        if kwargs.get("new_attempt") is not None:
            kwargs["new_attempt"] = self.redact(dict(kwargs["new_attempt"]))
        if kwargs.get("close_attempt") is not None:
            attempt_id, outcome, counted, detail = kwargs["close_attempt"]
            kwargs["close_attempt"] = (attempt_id, outcome, counted, self.redact(detail) if detail is not None else None)
        if kwargs.get("gate") is not None:
            point, verdict = kwargs["gate"]
            kwargs["gate"] = (point, self.redact(dict(verdict)))
        return self.ledger.transition(unit["unit_key"], to_state, self.stamp(), expect_from=unit["state"], **kwargs)

    # ---- the loop ----------------------------------------------------------------------------------

    def run(
        self, *, until_idle: bool = False, max_units: int | None = None, max_iterations: int | None = None
    ) -> dict[str, Any]:
        self.reconcile()
        terminal_before = len(self.ledger.units(ledger_module.TERMINAL_STATES))
        iterations = 0
        while not self.stop():
            progressed = self.iterate()
            iterations += 1
            if until_idle and self.idle():
                break
            if max_units is not None and (
                len(self.ledger.units(ledger_module.TERMINAL_STATES)) - terminal_before >= max_units
            ):
                break
            if max_iterations is not None and iterations >= max_iterations:
                break
            if not progressed:
                self.ports.clock.sleep(self._sleep_seconds())
        return summary(self.ledger)

    def idle(self) -> bool:
        """Every unit has ended, or waits on a disk that could never hold it, or on space that nothing left
        in the campaign gives back: only units deferred for disk and the pending units held back behind
        them remain, and none of them can be taken now. The disk pause says why; run --until-idle returns
        (releasing the Catalog lock), and the campaign goes on once someone frees space and runs it again."""
        for unit in self.ledger.units():
            if unit["state"] in ledger_module.TERMINAL_STATES or unit["state"] in ("deferred_disk", "pending"):
                continue
            return False
        return self._next_candidate() is None

    def _sleep_seconds(self) -> float:
        wait = float(self.policy.poll_seconds)
        now = self.now()
        for unit in self.ledger.units(("waiting_retry",)):
            due = policy.parse_iso(unit["next_attempt_at"])
            # A retry already due waits for room in the hand, which the next poll looks for anyway.
            if due is not None and due > now:
                wait = min(wait, max(1.0, (due - now).total_seconds()))
        return wait

    def iterate(self) -> bool:
        progressed = self._requests()
        pause = self._paused()
        if pause == "pin" and not self._pin_differences():
            self.ledger.resume(self.stamp(), kinds=["pin"], detail={"pins": "match the approved manifest again"})
            pause = None
        elif pause == "fault":
            paused_at = policy.parse_iso(self.ledger.runner().get("paused_at"))
            if paused_at is None or (self.now() - paused_at).total_seconds() >= self.policy.fault_recheck_seconds:
                if self._backend_ok():
                    self.ledger.resume(self.stamp(), kinds=["fault"], detail={"recheck": "retrying the step"})
                    pause = None
        elif pause == "disk" and not self._waiting_for_disk():
            self.ledger.resume(self.stamp(), kinds=["disk"], detail={"disk": "no unit waits for space any more"})
        # Jobs in flight are always watched: their end is recorded even while the campaign is paused.
        for unit in self.ledger.units(IN_FLIGHT):
            progressed |= self._guard(unit)
        if self._paused() in BLOCKING_PAUSES:
            return progressed
        for unit in self.ledger.units(("split_parent",)):
            progressed |= self._guard(unit)
        for unit in self._in_hand():
            if self._paused() in BLOCKING_PAUSES:
                return progressed
            current = self.ledger.unit(unit["unit_key"])
            if current["state"] not in IN_FLIGHT and current["state"] not in ledger_module.TERMINAL_STATES:
                progressed |= self._guard(current)
        while len(self._in_hand()) < 1 + self.policy.prefetch and self._paused() not in BLOCKING_PAUSES:
            candidate = self._next_candidate()
            if candidate is None:
                break
            if self._pin_check_failed():
                break
            before = candidate["state"]
            progressed |= self._guard(candidate)
            if self.ledger.unit(candidate["unit_key"])["state"] == before:
                break
        return progressed

    def _in_hand(self) -> list[dict[str, Any]]:
        return [
            unit for unit in self.ledger.units()
            if unit["state"] not in ledger_module.TERMINAL_STATES and unit["state"] not in IDLE
            and unit["state"] != "split_parent"
        ]

    def _next_candidate(self) -> dict[str, Any] | None:
        """The next unit to take into the hand, in the approved order.

        A unit deferred for disk, while it does not fit, holds back the pending units behind it: a short
        disk pauses new work, as the user's rule says. It does not hold back a retry or a split part: those
        already have their raw data on the volume, and their end is what gives the space back. A unit no
        volume could hold holds nothing back.

        While a streak of network failures is long enough to trip the outage breaker, a unit whose next
        step is a download from one of the streak's groups comes last: a unit of another group is the probe
        that tells an outage, which fails it too, from those groups' own objects failing, which it does
        not. Among them the one tried longest ago goes first, so an outage that lasts uses every unit's
        interruptions in turn rather than one unit's.
        """
        now = self.now()
        held_back = False
        suspected = self._suspected_outage()
        last: dict[str, Any] | None = None
        for unit in self.ledger.units(IDLE):
            state = unit["state"]
            candidate = False
            if state == "pending":
                candidate = not held_back
            elif state == "queued":
                candidate = True
            elif state == "waiting_retry":
                due = policy.parse_iso(unit["next_attempt_at"])
                candidate = due is None or due <= now
            elif state == "deferred_disk":
                verdict = self._disk_verdict(unit)
                if verdict.never_fits:
                    continue
                candidate = verdict.admit
                held_back = held_back or not verdict.admit
            if not candidate:
                continue
            if suspected and _downloads_next(unit) and (unit["repository"], _group(unit)) in suspected:
                if last is None or str(unit["next_attempt_at"] or "") < str(last["next_attempt_at"] or ""):
                    last = unit
                continue
            return unit
        return last

    def _waiting_for_disk(self) -> bool:
        """Whether a unit still waits for space: deferred for it, or about to be looked at (handoff_ready),
        whose admission lifts the disk pause with its own record."""
        if self.ledger.units(("handoff_ready",)):
            return True
        return any(not self._disk_verdict(unit).never_fits for unit in self.ledger.units(("deferred_disk",)))

    def _guard(self, unit: dict[str, Any]) -> bool:
        """One step of one unit. A failure of the unit is recorded and the loop goes on."""
        try:
            return bool(self._step(unit))
        except ledger_module.ApprovalMissing as error:
            current = self.ledger.unit(unit["unit_key"])
            for attempt in self.ledger.open_attempts(unit["unit_key"]):
                self.ledger.close_attempt(attempt["attempt_id"], "refused", self.stamp(), detail={"approval": str(error)})
            if current["state"] in ledger_module.TERMINAL_STATES:
                return False
            self.ledger.transition(
                unit["unit_key"], "stopped_no_approval", self.stamp(), expect_from=current["state"],
                terminal_reason="approval_missing", terminal_detail=str(error),
                end_console_run=self._open_console_end(current, "stopped"),
            )
            return True
        except (ledger_module.LedgerError, sqlite3.Error):
            raise
        except Exception as error:  # noqa: BLE001 - a defect in one step fails that unit, not the campaign
            current = self.ledger.unit(unit["unit_key"])
            if current["state"] in ledger_module.TERMINAL_STATES:
                return False
            detail = {"state": current["state"], "error_type": type(error).__name__, "detail": str(error)}
            if current["state"] in IN_FLIGHT:
                # The job may still be running: its Console keeps the slot until its end is seen. Only a
                # poll that keeps failing gives the job up, cancelled first.
                errors = self._poll_errors.get(unit["unit_key"], 0) + 1
                self._poll_errors[unit["unit_key"]] = errors
                if errors == 1:
                    self._event("poll_error", detail, unit["unit_key"])
                if errors < self.POLL_ERROR_LIMIT:
                    return False
                self._poll_errors.pop(unit["unit_key"], None)
                self._cancel(current, current[JOB_COLUMN[current["state"]]], "poll_error")
                self._production_ended(current, "failed")
                restart = {"downloading": "handoff_ready", "diagnosing": "metadata_prepared", "running": "prepared"}
                self._fail(current, step=f"{current['state']}_poll", result={"ok": False, "reason": "exception", **detail},
                           retry_state=restart[current["state"]], console_run=self._open_console_end(current, "failed"))
                return True
            if current["state"] in END_STATES:
                for attempt in self.ledger.open_attempts(unit["unit_key"]):
                    self.ledger.close_attempt(attempt["attempt_id"], "failed", self.stamp(), detail=self.redact(detail))
                self._step_retry(current, step=f"{current['state']}_step", detail=detail)
                return True
            if current["state"] not in ledger_module.RESUMABLE_STATES:
                # A split parent, or a unit waiting its turn: looked at again next time, nothing to undo.
                self._event("step_error", detail, unit["unit_key"])
                return False
            opened = self.ledger.open_attempts(unit["unit_key"])
            for attempt in opened[:-1]:
                self.ledger.close_attempt(attempt["attempt_id"], "failed", self.stamp())
            self._fail(
                current, step=opened[-1]["step"] if opened else f"{current['state']}_step",
                attempt_id=opened[-1]["attempt_id"] if opened else None,
                result={"ok": False, "reason": "exception", "error_type": type(error).__name__, "detail": str(error)},
                retry_state=current["state"], console_run=self._open_console_end(current, "failed"),
            )
            return True

    def _step(self, unit: dict[str, Any]) -> bool:
        handler = getattr(self, "_state_" + unit["state"], None)
        if handler is None:
            return False
        result = handler(unit)
        self._poll_errors.pop(unit["unit_key"], None)
        return result

    # ---- failure, interruption, retry ---------------------------------------------------------------

    def _fail(
        self,
        unit: dict[str, Any],
        *,
        step: str,
        result: Any,
        retry_state: str,
        attempt_id: int | None = None,
        outcome: str = "failed",
        console_run: tuple[int, str] | None = None,
        job_id: str | None = None,
        gate: tuple[str, dict[str, Any]] | None = None,
    ) -> None:
        kind = policy.classify_result(result) if isinstance(result, Mapping) else policy.FAILED
        detail = self.redact(dict(result) if isinstance(result, Mapping) else {"result": repr(result)})
        if job_id:
            detail["job_id"] = job_id
        if unit.get("outputs_produced") and kind in (policy.FAILED, policy.BUSY):
            # A unit whose outputs exist is never failed (END_STATES): its step is tried again.
            self._step_retry(unit, step=step, detail=detail, attempt_id=attempt_id)
            return

        def attempt(result_outcome: str, counted: bool) -> dict[str, Any]:
            if attempt_id is not None:
                return {"close_attempt": (attempt_id, result_outcome, counted, detail)}
            return {"new_attempt": {"step": step, "outcome": result_outcome, "counted": counted, "detail": detail}}

        if kind == policy.REFUSED:
            self._move(
                unit, "stopped_no_approval", terminal_reason="campaign_authorization_refused",
                terminal_detail=json.dumps(detail, ensure_ascii=False, sort_keys=True), detail={"step": step},
                end_console_run=console_run, gate=gate, **attempt("refused", False),
            )
            return
        if kind == policy.BUSY:
            due = self.now() + timedelta(seconds=float(self.policy.busy_retry_seconds))
            self._move(
                unit, "waiting_retry", resume_state=retry_state, next_attempt_at=policy.iso(due),
                detail={"step": step, "busy": True}, end_console_run=console_run, gate=gate, **attempt("busy", False),
            )
            return
        if kind in (policy.FAULT, policy.CONTRACT):
            # The campaign's, not the unit's: nothing counts, the unit stays where it is, and the step is made
            # again when the pause lifts - by itself at the fault recheck, by an operator for a contract.
            if attempt_id is not None:
                self.ledger.close_attempt(attempt_id, "fault", self.stamp(), detail=detail)
            if console_run is not None:
                self.ledger.end_console_run(console_run[0], "not_started", self.stamp())
            self._pause(kind, f"{step} for unit {unit['unit_key']}: {detail.get('reason')}: {detail.get('detail') or ''}".rstrip(": "))
            return
        decision = policy.after_failure(int(unit["failures"]), self.now(), self.policy)
        if decision.state == "waiting_retry":
            self._move(
                unit, "waiting_retry", resume_state=retry_state, next_attempt_at=decision.next_attempt_at,
                failures=decision.failures, detail={"step": step, "failures": decision.failures},
                end_console_run=console_run, gate=gate, **attempt(outcome, True),
            )
            return
        # The first attempt and both retries failed: the unit ends as failed, and its raw data go.
        self._move(
            unit, "discarding", pending_terminal="failed", failures=decision.failures,
            terminal_detail=json.dumps({"reason": f"{step}_failed", "detail": detail}, ensure_ascii=False, sort_keys=True),
            detail={"step": step, "failures": decision.failures, "retries_exhausted": True},
            end_console_run=console_run, gate=gate, **attempt(outcome, True),
        )

    def _step_retry(
        self, unit: dict[str, Any], *, step: str, detail: Mapping[str, Any], attempt_id: int | None = None
    ) -> None:
        """A step of a unit's end (END_STATES) that went wrong: looked at again after the retry delays, then
        every longest delay, never counted and never ending the unit otherwise than it was going to end.

        An error there is the campaign's (a full disk, a file Windows holds, a gate report that could not
        be written), not the unit's: counted, it used to end a skipped unit as failed and a unit whose
        outputs were produced and whose raw data were released as failed with its raw data "kept".
        """
        state = unit["state"]
        tried = self.ledger.count_attempts(unit["unit_key"], step, ("failed",))
        delays = self.policy.retry_delays_seconds or (self.policy.fault_recheck_seconds,)
        due = self.now() + timedelta(seconds=float(delays[min(tried, len(delays) - 1)]))
        record = self.redact(dict(detail))
        extra: dict[str, Any] = (
            {"close_attempt": (attempt_id, "failed", False, record)} if attempt_id is not None
            else {"new_attempt": {"step": step, "outcome": "failed", "counted": False, "detail": record}}
        )
        keep = (
            {"pending_terminal": unit.get("pending_terminal"), "terminal_detail": unit.get("terminal_detail")}
            if state == "discarding" else {}
        )
        self._move(
            unit, "waiting_retry", resume_state=state, next_attempt_at=policy.iso(due),
            detail={"step": step, "step_error": True, "tried": tried + 1}, **keep, **extra,
        )

    def _interrupted(
        self,
        unit: dict[str, Any],
        *,
        step: str,
        retry_state: str,
        detail: Mapping[str, Any],
        attempt_id: int | None = None,
        console_run: tuple[int, str] | None = None,
    ) -> None:
        """A stop the unit did not cause (a reboot, a backend restart). Retried without counting, up to
        max_interruptions; past that it counts as a failure, so a unit that always kills its backend ends."""
        if policy.interruption_counts(int(unit["interruptions"]), self.policy):
            self._fail(
                unit, step=step, result={"ok": False, "reason": "interrupted", **dict(detail)},
                retry_state=retry_state, attempt_id=attempt_id, outcome="interrupted", console_run=console_run,
            )
            return
        record = self.redact(dict(detail))
        extra: dict[str, Any] = (
            {"close_attempt": (attempt_id, "interrupted", False, record)} if attempt_id is not None
            else {"new_attempt": {"step": step, "outcome": "interrupted", "counted": False, "detail": record}}
        )
        self._move(
            unit, retry_state, interruptions=int(unit["interruptions"]) + 1, detail={"step": step, "interrupted": True},
            end_console_run=console_run, **extra,
        )

    def _open_console_end(self, unit: Mapping[str, Any], outcome: str) -> tuple[int, str] | None:
        for kind in ("diagnostic", "production"):
            run = self.ledger.open_console_run_of(unit["unit_key"], kind)
            if run is not None:
                return (run["console_run_id"], outcome)
        return None

    # ---- resume -------------------------------------------------------------------------------------

    def reconcile(self) -> None:
        """Close what a stopped runner left open, adopting a start that happened after all."""
        for unit in self.ledger.units():
            if unit["state"] in ledger_module.TERMINAL_STATES:
                for attempt in self.ledger.open_attempts(unit["unit_key"]):
                    self.ledger.close_attempt(attempt["attempt_id"], "interrupted", self.stamp())
                continue
            for attempt in self.ledger.open_attempts(unit["unit_key"]):
                self._reconcile_attempt(self.ledger.unit(unit["unit_key"]), attempt)
        slot = self.ledger.slot()
        if slot is not None:
            owner = self.ledger.unit(slot["unit_key"])
            live_start = any(
                attempt["step"] in ("diagnostic_start", "run_start")
                for attempt in self.ledger.open_attempts(owner["unit_key"])
            )
            owns = owner["state"] in ("diagnosing", "running") and slot.get("job_id")
            if not owns and not live_start:
                self.ledger.end_console_run(slot["console_run_id"], "abandoned", self.stamp())
                self._event("console_slot_released", {"console_run_id": slot["console_run_id"]}, owner["unit_key"])

    def _reconcile_attempt(self, unit: dict[str, Any], attempt: Mapping[str, Any]) -> None:
        since = policy.parse_iso(attempt["started_at"]) or self.now()
        step = attempt["step"]
        if step == "download_start" and unit["state"] == "handoff_ready":
            job = started_download_job(self._manifest(unit), since, self._known_jobs(unit["unit_key"]))
            if job:
                self._move(
                    unit, "downloading", boundary="1", download_job_id=job,
                    close_attempt=(attempt["attempt_id"], "ok", False, {"job_id": job, "adopted": True}),
                    detail={"adopted_after_restart": True},
                )
                return
        if step in ("diagnostic_start", "run_start"):
            kind, target, column, console_kind = (
                ("tuning", "diagnosing", "diagnostic_job_id", "diagnostic") if step == "diagnostic_start"
                else ("run", "running", "run_job_id", "production")
            )
            wanted = "metadata_prepared" if step == "diagnostic_start" else "prepared"
            run = self.ledger.open_console_run_of(unit["unit_key"], console_kind)
            if unit["state"] == wanted:
                job = started_console_job(self._manifest(unit), kind, since, self._known_jobs(unit["unit_key"]))
                if job:
                    self._move(
                        unit, target, boundary="4", **{column: job},
                        close_attempt=(attempt["attempt_id"], "ok", False, {"job_id": job, "adopted": True}),
                        console_job=(run["console_run_id"], job) if run else None,
                        detail={"adopted_after_restart": True},
                    )
                    return
            self.ledger.close_attempt(attempt["attempt_id"], "interrupted", self.stamp())
            if run is not None and not run.get("job_id"):
                self.ledger.end_console_run(run["console_run_id"], "not_started", self.stamp())
            return
        self.ledger.close_attempt(attempt["attempt_id"], "interrupted", self.stamp())

    # ---- operator requests ------------------------------------------------------------------------

    def _requests(self) -> bool:
        handled = False
        for request in self.ledger.pending_requests():
            unit = self.ledger.unit(request["unit_key"])
            state = unit["state"]
            if request["action"] == "skip":
                if state in ledger_module.TERMINAL_STATES or state == "discarding":
                    detail = f"not skipped: the unit is already {state}"
                elif state in END_STATES or (state == "waiting_retry" and unit["resume_state"] in END_STATES):
                    # Already ending - failed, skipped or excluded, or with its outputs produced: what it ends as
                    # is not relabelled.
                    detail = f"not skipped: the unit is ending ({unit['pending_terminal'] or unit['resume_state'] or state})"
                elif state in IN_FLIGHT:
                    self._cancel(unit, unit[JOB_COLUMN[state]], "skip")
                    detail = "cancel requested; the unit is skipped when its job ends"
                elif state == "split_parent":
                    detail = "not skipped: skip the split parts one by one"
                else:
                    self._move(
                        unit, "discarding", pending_terminal="skipped",
                        terminal_detail=json.dumps({"reason": "operator_skip", "detail": request["reason"]}),
                        detail={"request_id": request["request_id"]},
                        end_console_run=self._open_console_end(unit, "skipped"),
                    )
                    detail = "skipped"
            else:
                if state == "waiting_retry":
                    self._move(unit, "waiting_retry", resume_state=unit["resume_state"], next_attempt_at=self.stamp(),
                               pending_terminal=unit.get("pending_terminal"), terminal_detail=unit.get("terminal_detail"),
                               detail={"request_id": request["request_id"]})
                    detail = "retry brought forward"
                elif state == "deferred_disk":
                    self._move(unit, unit["resume_state"] or "handoff_ready", detail={"request_id": request["request_id"]})
                    detail = "disk deferral lifted"
                elif state in ("failed", "excluded", "skipped", "stopped_no_approval", "stopped_policy_drift") and unit["role"] == "run":
                    self._move(
                        unit, "pending", failures=0, interruptions=0, outputs_produced=0,
                        detail={"request_id": request["request_id"], "retry_of": state},
                    )
                    detail = "the unit starts again from its Class decision"
                else:
                    detail = f"not retried: nothing to retry in state {state}"
            self.ledger.handle_request(request["request_id"], detail, self.stamp())
            handled = True
        return handled

    # ---- pins, backend, disk --------------------------------------------------------------------------

    def _pin_differences(self) -> list[str]:
        try:
            current = self.ports.pins.current()
        except Exception as error:  # noqa: BLE001 - an unreadable pin is a changed pin
            return [f"unreadable: {type(error).__name__}: {error}"]
        return policy.pin_differences(self.pins, current)

    def _pin_check_failed(self) -> bool:
        differences = self._pin_differences()
        if not differences:
            return False
        reason = "Pinned identities changed since the approval: " + ", ".join(differences)
        if self._paused() != "pin":
            self._event("pin_changed", {"differences": differences})
        self._pause("pin", reason)
        return True

    def _backend_ok(self) -> bool:
        if self.ports.backend is None:
            return True
        result = self.ports.backend.ensure()
        if not result.get("ok"):
            self._event("backend_unavailable", {"detail": result.get("detail")})
        elif result.get("started"):
            self._event("backend_started", {"pid": result.get("pid")})
        return bool(result.get("ok"))

    def _observations(self) -> list[float]:
        values = []
        for unit in self.ledger.units(("done",)):
            if unit["peak_workspace_bytes"] and unit["downloaded_bytes"]:
                values.append(unit["peak_workspace_bytes"] / unit["downloaded_bytes"])
        return values

    def _disk_verdict(self, unit: Mapping[str, Any]) -> policy.DiskVerdict:
        held = self._held_bytes(unit)
        need = policy.disk_need(
            int(unit["known_bytes"] or 0), bool(unit["size_known"]), bool(unit["has_archive"]),
            self.policy.disk, self._observations(), held_bytes=held,
        )
        free, total = self.ports.disk.usage(self.campaign["workspace_root"])
        return policy.disk_verdict(need, free, total, self.policy.disk, held=held)

    def _held_bytes(self, unit: Mapping[str, Any]) -> int:
        """What the unit already has on the volume before its download: its own raw tree, the partial files a
        failed or cancelled lease keeps for the resume included. A split part's raw data are its parent's."""
        if unit.get("role") == "split_part" or not unit.get("workspace"):
            return 0
        raw = Path(str(unit["workspace"])) / "raw"
        return int(self.ports.disk.tree_bytes(str(raw))) if raw.is_dir() else 0

    # ---- the steps, one per state -----------------------------------------------------------------

    def _state_pending(self, unit: dict[str, Any]) -> bool:
        """Settle the unit's Class as the approved manifest pinned it (boundary 3)."""
        purpose = self.campaign["analysis_purpose"]
        attempt = self.ledger.open_attempt(unit["unit_key"], "class", self.stamp(), tool="msdial_catalog_save_class_proposal")
        decision = self.ports.catalog.class_decision(unit["catalog_unit_id"], purpose)
        if decision.get("proposal_id") != unit["approved_class_digest"]:
            # A grouping nobody approved: nothing is saved under the approval.
            self._move(
                unit, "stopped_policy_drift", terminal_reason="class_digest_changed",
                terminal_detail=json.dumps({"approved": unit["approved_class_digest"], "now": decision.get("proposal_id")}),
                close_attempt=(attempt, "refused", False, {"approved": unit["approved_class_digest"], "now": decision.get("proposal_id")}),
            )
            return True
        self._require("3")
        ratification = {
            "approval_id": self.approval["approval_id"],
            "manifest_digest": self.campaign["manifest_digest"],
            "authorization_sha256": self.campaign["authorization_sha256"],
            "campaign_id": self.campaign["campaign_id"],
            "proposal_id": unit["approved_class_digest"],
        }
        result = self.ports.catalog.save_class(
            unit_id=unit["catalog_unit_id"], purpose=purpose, kind=decision["kind"], ratification=ratification
        )
        if result.get("ok") is False:
            self._fail(unit, step="class", result=result, retry_state="pending", attempt_id=attempt)
            return True
        self._move(
            unit, "class_settled", boundary="3", class_proposal_id=result["proposal_id"], class_kind=decision["kind"],
            close_attempt=(attempt, "ok", False, {"proposal_id": result["proposal_id"], "kind": decision["kind"]}),
        )
        return True

    def _state_class_settled(self, unit: dict[str, Any]) -> bool:
        """Take the Catalog's handoff and keep the campaign's own copy of it."""
        attempt = self.ledger.open_attempt(unit["unit_key"], "handoff", self.stamp(), tool="msdial_catalog_reanalysis_handoff")
        result = self.ports.catalog.handoff(unit_id=unit["catalog_unit_id"], class_proposal_id=unit["class_proposal_id"])
        if result.get("ok") is False:
            self._fail(unit, step="handoff", result=result, retry_state="class_settled", attempt_id=attempt)
            return True
        record = self.ports.catalog.copy_handoff(result, self.directory / "handoffs" / unit["unit_key"])
        workspace = self._workspace(unit)
        copy = workspace / "provenance" / "campaign-authorization.json"
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.campaign["authorization_path"], copy)
        self._move(
            unit, "handoff_ready", handoff_path=record["copy_path"], workspace=str(workspace),
            manifest_path=str(workspace / "provenance" / "run-manifest.json"), authorization_copy=str(copy),
            close_attempt=(attempt, "ok", False, {"copy": record, "catalog_blocking_reasons": list(result.get("blocking_reasons") or [])}),
        )
        return True

    def _state_handoff_ready(self, unit: dict[str, Any]) -> bool:
        """The disk guard, then the download (boundary 1)."""
        verdict = self._disk_verdict(unit)
        values = {"volume": self.campaign["workspace_root"], "free": verdict.free, "reserve": verdict.reserve,
                  "need": verdict.need, "unit_key": unit["unit_key"]}
        if verdict.never_fits:
            # No per-unit size limit: the unit is not ended, it waits, and the campaign goes on.
            self.ledger.disk_event("never_fits", self.stamp(), **values)
            self._move(unit, "deferred_disk", resume_state="handoff_ready", detail={"disk": verdict.as_dict()})
            return True
        if not verdict.admit:
            # Out of the hand, so a retry that already holds raw data can run and give the space back.
            return self._defer_for_disk(
                unit, reason=f"Unit {unit['unit_key']} needs {max(0, verdict.need - verdict.held)} bytes above the reserve "
                             f"({verdict.need} in all, {verdict.held} already on the volume); {verdict.free} are free."
            )
        if self._paused() == "disk":
            self.ledger.resume(self.stamp(), kinds=["disk"], detail={"unit_key": unit["unit_key"]})
            self.ledger.disk_event("resume", self.stamp(), **values)
        self.ledger.disk_event("admit", self.stamp(), **values)
        _free, total = self.ports.disk.usage(self.campaign["workspace_root"])
        bound = policy.download_bound_gb(total, self.policy.disk)
        arguments = {
            "repository": unit["repository"], "accession": unit["accession"],
            "workspace_root": self.campaign["workspace_root"], "maximum_gb": bound,
            "raw_retention_policy": self.campaign["raw_retention_policy"], "handoff_path": unit["handoff_path"],
            "analysis_purpose": self.campaign["analysis_purpose"], "authorization_path": self._authorization(unit),
        }
        self._require("1")
        attempt = self.ledger.open_attempt(
            unit["unit_key"], "download_start", self.stamp(), tool="msdial_download_repository_raw",
            arguments=self.redact(arguments),
        )
        started_at = policy.parse_iso(self.ledger.attempts(unit["unit_key"])[-1]["started_at"]) or self.now()
        result = self.ports.interactive.download(**arguments)
        if result.get("ok") is not False and result.get("job_id"):
            self._move(
                unit, "downloading", boundary="1", download_job_id=result["job_id"],
                close_attempt=(attempt, "ok", False, {"job_id": result["job_id"]}),
            )
            return True
        job = started_download_job(self._manifest(unit), started_at, self._known_jobs(unit["unit_key"]))
        if job:
            self._move(
                unit, "downloading", boundary="1", download_job_id=job,
                close_attempt=(attempt, "ok", False, {"job_id": job, "adopted": True, "reply": self.redact(result)}),
            )
            return True
        if result.get("reason") == "blocked":
            blockers = list(result.get("blocking_reasons") or [])
            if "size_limit:exceeded" in blockers:
                # Interactive's own figure (a shared bundle's declared bytes, say) is above what the volume
                # could hold above its reserve: the unit needs more than the plan knew. It waits for a disk
                # that holds it, and Interactive is not asked again meanwhile.
                required = int(result.get("required_download_bytes") or 0)
                return self._defer_for_disk(
                    unit, known_bytes=max(int(unit["known_bytes"] or 0), required, int(bound * 1000**3) + 1),
                    reason=f"Unit {unit['unit_key']}: Interactive needs {required} bytes, above the {bound:.1f} GB the volume holds.",
                    close_attempt=(attempt, "blocked", False, {"blocking_reasons": blockers, "required_download_bytes": required}),
                )
            # Interactive refused the unit before any byte: its decision, recorded and not retried.
            self._move(
                unit, "discarding", pending_terminal="excluded",
                terminal_detail=json.dumps({"reason": "download_blocked", "blocking_reasons": blockers}),
                close_attempt=(attempt, "blocked", False, {"blocking_reasons": blockers}),
            )
            return True
        return self._download_failed(unit, result=result, attempt_id=attempt)

    def _defer_for_disk(
        self, unit: dict[str, Any], *, reason: str, known_bytes: int | None = None, **record: Any
    ) -> bool:
        """Take a unit the disk cannot hold now out of the hand, to be admitted again once it fits.

        A unit that kept the hand while it waited could wait for ever: the retry of a unit whose raw data
        take the space could then never run and give it back. `record` is the attempt that ended here.
        """
        fields = {} if known_bytes is None else {"known_bytes": int(known_bytes)}
        verdict = self._disk_verdict({**unit, **fields})
        self._move(unit, "deferred_disk", resume_state="handoff_ready", detail={"disk": verdict.as_dict()}, **fields, **record)
        values = {"volume": self.campaign["workspace_root"], "free": verdict.free, "reserve": verdict.reserve,
                  "need": verdict.need, "unit_key": unit["unit_key"]}
        if verdict.never_fits:
            self.ledger.disk_event("never_fits", self.stamp(), **values)
        elif not verdict.admit:
            if self._paused() != "disk":
                self.ledger.disk_event("pause", self.stamp(), **values)
            self._pause("disk", reason)
        return True

    def _download_failed(
        self, unit: dict[str, Any], *, result: dict[str, Any], outcome: str = "failed",
        attempt_id: int | None = None, job_id: str | None = None,
    ) -> bool:
        """A download that failed: the unit's failure, unless it completes a repository outage.

        The unit that trips the breaker leaves the hand uncounted, using an interruption, until the fault
        recheck, which tries a unit of another download group first (_next_candidate). So a group whose own
        objects keep failing cannot hold the campaign: the probe's success ends the streak, and the group's
        next failures count. Past its interruptions a unit's failure counts here too, so even a unit that
        meets an outage at every recheck ends.
        """
        if policy.classify_result(result) == policy.FAILED and policy.network_failure(result, outcome):
            result = {**result, "network": True}
            groups = self._outage_streaks().get(unit["repository"], set()) | {_group(unit)}
            if (
                self.policy.outage_units and len(groups) >= self.policy.outage_units
                and not policy.interruption_counts(int(unit["interruptions"]), self.policy)
            ):
                detail = self.redact({**result, "outage_groups": sorted(groups), **({"job_id": job_id} if job_id else {})})
                due = self.now() + timedelta(seconds=float(self.policy.fault_recheck_seconds))
                self._move(
                    unit, "waiting_retry", resume_state="handoff_ready", next_attempt_at=policy.iso(due),
                    interruptions=int(unit["interruptions"]) + 1, detail={"outage": True},
                    **({"close_attempt": (attempt_id, "fault", False, detail)} if attempt_id is not None
                       else {"new_attempt": {"step": "download", "outcome": "fault", "counted": False, "detail": detail}}),
                )
                self._pause(
                    "fault",
                    f"Downloads from {unit['repository']} failed on the network for {len(groups)} different download "
                    f"groups in a row (last unit {unit['unit_key']}): a repository outage, not unit failures. At the "
                    "fault recheck a unit of another download group is tried first; this one waits, uncounted.",
                )
                return True
        self._fail(unit, step="download", result=result, retry_state="handoff_ready", attempt_id=attempt_id,
                   outcome=outcome, job_id=job_id)
        return True

    def _outage_streaks(self) -> dict[str, set[str]]:
        """For each repository, the download groups whose downloads failed on the network since its last
        download that finished. A failure of any other kind ends that repository's run: bytes arrived, so
        its server worked. Another repository's downloads say nothing about this one's server.

        A group that failed on the network before that download too, and has not downloaded since, is left
        out: its failure outlived the server's working, so it is the group's own, and it does not make the
        next run look like an outage (the probe's verdict, _next_candidate, stands)."""
        units = {unit["unit_key"]: unit for unit in self.ledger.units()}
        streaks: dict[str, set[str]] = {}
        persistent: dict[str, set[str]] = {}
        recovered: dict[str, set[str]] = {}
        ended: set[str] = set()
        for row in self.ledger.recent_attempts(("download", "download_start")):
            unit = units.get(row["unit_key"])
            if unit is None:
                continue
            repository, group, outcome = unit["repository"], _group(unit), row["outcome"]
            if outcome == "ok":
                if row["step"] == "download":
                    ended.add(repository)
                    recovered.setdefault(repository, set()).add(group)
                continue
            if outcome not in ("failed", "stalled", "fault"):
                continue
            network = bool(_loads(row["detail_json"]).get("network"))
            if repository not in ended:
                if network:
                    streaks.setdefault(repository, set()).add(group)
                else:
                    ended.add(repository)
            elif network and group not in recovered.get(repository, set()):
                persistent.setdefault(repository, set()).add(group)
        return {repository: groups - persistent.get(repository, set()) for repository, groups in streaks.items()}

    def _suspected_outage(self) -> set[tuple[str, str]]:
        """(repository, download group) of every streak long enough to trip the breaker."""
        if not self.policy.outage_units:
            return set()
        return {
            (repository, group) for repository, groups in self._outage_streaks().items()
            if len(groups) >= self.policy.outage_units for group in groups
        }

    def _cancel(self, unit: Mapping[str, Any], job_id: str | None, reason: str) -> None:
        """Stop a job, with why recorded first: a resumed runner reads the reason from the record, and a
        cancel recorded and never sent (a crash between the two) is sent at the job's next poll
        (_resend_cancel). msdial_cancel_job answers a finished job without acting, so sending twice is safe."""
        if not job_id:
            return
        recorded = any(
            _loads(event["detail_json"]).get("job_id") == job_id and _loads(event["detail_json"]).get("reason") == reason
            for event in self.ledger.events("cancel_requested")
        )
        if not recorded:
            self._event("cancel_requested", {"job_id": job_id, "reason": reason}, unit["unit_key"])
        self._send_cancel(job_id, reason)

    def _send_cancel(self, job_id: str, reason: str) -> None:
        if job_id in self._cancels_sent:
            return
        result = self.ports.interactive.cancel(job_id, reason)
        if isinstance(result, Mapping) and result.get("ok") is not False:
            self._cancels_sent.add(job_id)

    def _resend_cancel(self, job_id: str | None) -> None:
        if job_id and job_id not in self._cancels_sent:
            reason = self._cancel_reason(job_id)
            if reason:
                self._send_cancel(job_id, reason)

    def _cancel_reason(self, job_id: str) -> str:
        reason = ""
        for event in self.ledger.events("cancel_requested"):
            detail = _loads(event["detail_json"])
            if detail.get("job_id") == job_id:
                reason = str(detail.get("reason") or "")
        return reason

    def _state_downloading(self, unit: dict[str, Any]) -> bool:
        job_id = unit["download_job_id"]
        job = self.ports.interactive.job(job_id)
        if job.get("ok") is False:
            if policy.classify_result(job) == policy.FAULT:
                self._pause("fault", f"The campaign backend did not answer for download job {job_id}.")
                return False
            return self._download_lost(unit)
        status = str(job.get("status") or "")
        if status == WAITING_FOR_SHARED:
            self._resend_cancel(job_id)
            self._progress.pop(job_id, None)
            return False
        if status in ("queued", "running"):
            self._resend_cancel(job_id)
            self._watch_download(unit, job)
            return False
        if status == "completed":
            manifest_path = str((job.get("result") or {}).get("manifest_path") or unit["manifest_path"])
            return self._downloaded(unit, manifest_path)
        reason = self._cancel_reason(job_id)
        detail = {"job_id": job_id, "status": status, "error": job.get("error"), "stop_reason": job.get("stop_reason")}
        failure = self._lease_failure(unit, job_id)
        if failure:
            detail["failure"] = failure
        return self._download_ended(unit, status, reason, detail)

    def _lease_failure(self, unit: Mapping[str, Any], job_id: str) -> dict[str, Any]:
        """The download_failure this job's lease wrote into the unit manifest, or {}.

        The job's own record carries only str(error): what the failure was (error_type, retryable, the
        attempts, an archive's reason code) is written to the manifest alone, and the job's text alone
        reads a stalled or cut-short transfer, or a volume that ran short while an archive expanded, as the
        unit's own failure. A record of another job's lease is not this one's."""
        manifest = self._manifest(unit) or {}
        if manifest.get("status") != "download_failed" or (manifest.get("lease_owner") or {}).get("job_id") != job_id:
            return {}
        failure = manifest.get("download_failure")
        return dict(failure) if isinstance(failure, Mapping) else {}

    def _downloaded(self, unit: dict[str, Any], manifest_path: str) -> bool:
        manifest = self.ports.interactive.read_manifest(manifest_path) or {}
        raw = str(manifest.get("raw_directory") or "")
        size = self.ports.disk.tree_bytes(raw) if raw else 0
        self._progress.pop(unit["download_job_id"] or "", None)
        # A download that finished, which ends its repository's run of network failures (_outage_streaks).
        finished = {"step": "download", "outcome": "ok", "counted": False,
                    "detail": {"job_id": unit["download_job_id"], "bytes": size}}
        if self._cancel_reason(unit["download_job_id"] or "") == "skip":
            self._move(
                unit, "discarding", pending_terminal="skipped", manifest_path=manifest_path, downloaded_bytes=size,
                terminal_detail=json.dumps({"reason": "operator_skip"}), raw_disposition="present", new_attempt=finished,
            )
            return True
        self._move(unit, "downloaded", manifest_path=manifest_path, downloaded_bytes=size, raw_disposition="present",
                   new_attempt=finished)
        return True

    def _download_ended(self, unit: dict[str, Any], status: str, reason: str, detail: dict[str, Any]) -> bool:
        if reason == "disk":
            # Cancelled because the disk ran short: nothing the unit did. It resumes from its .part file. A size
            # known only as a lower bound is at least what arrived, kept even should the partial file go.
            held = self._held_bytes(unit)
            return self._defer_for_disk(
                unit, reason=f"Download of unit {unit['unit_key']} was stopped because free space ran below the floor.",
                known_bytes=max(int(unit["known_bytes"] or 0), held) if not unit["size_known"] else None,
                new_attempt={"step": "download", "outcome": "cancelled", "counted": False, "detail": {**detail, "held_bytes": held}},
            )
        if reason == "skip":
            self._move(
                unit, "discarding", pending_terminal="skipped", terminal_detail=json.dumps({"reason": "operator_skip"}),
                new_attempt={"step": "download", "outcome": "cancelled", "counted": False, "detail": detail},
            )
            return True
        if status == "interrupted":
            self._interrupted(unit, step="download", retry_state="handoff_ready", detail=detail)
            return True
        needed = policy.lease_size_limit(detail)
        if needed is not None:
            # The lease reached maximum_gb, the volume above its reserve: a disk that cannot hold the unit,
            # never its failure. Its partial files stay for a resume.
            return self._defer_for_disk(
                unit, known_bytes=max(int(unit["known_bytes"] or 0), needed),
                reason=f"Unit {unit['unit_key']}'s download reached the disk bound; it needs at least {needed} bytes.",
                new_attempt={"step": "download", "outcome": "blocked", "counted": False, "detail": detail},
            )
        expands = policy.extraction_disk_short(detail)
        if expands is not None:
            # Interactive's extraction guard stopped because the volume ran short as an archive expanded: a
            # short disk, never the unit's failure. The unit needs at least the archive's expansion, and more
            # than it holds plus what was free then, so it is not admitted again into the same shortage.
            held = self._held_bytes(unit)
            free, _total = self.ports.disk.usage(self.campaign["workspace_root"])
            floor = policy.known_bytes_for_need(held + free + 1, bool(unit["has_archive"]), self.policy.disk, self._observations())
            return self._defer_for_disk(
                unit, known_bytes=max(int(unit["known_bytes"] or 0), expands, floor),
                reason=f"Unit {unit['unit_key']}'s archive did not fit as it expanded ({expands or 'unstated'} bytes); "
                       f"{free} bytes were free.",
                new_attempt={"step": "download", "outcome": "blocked", "counted": False, "detail": detail},
            )
        outcome = "stalled" if reason == "stalled" else "failed"
        return self._download_failed(unit, result={"ok": False, "reason": outcome, **detail}, outcome=outcome,
                                     job_id=unit["download_job_id"])

    def _download_lost(self, unit: dict[str, Any]) -> bool:
        """The backend no longer knows the job: the unit manifest says what became of the lease."""
        manifest = self._manifest(unit)
        detail = {"job_id": unit["download_job_id"], "job": "not in the backend's registry"}
        if manifest is None:
            self._interrupted(unit, step="download", retry_state="handoff_ready", detail=detail)
            return True
        status = str(manifest.get("status") or "")
        if status == "downloading":
            if self.ports.interactive.lease_state(manifest) == "gone":
                self._interrupted(unit, step="download", retry_state="handoff_ready", detail={**detail, "lease": "gone"})
                return True
            return False
        if status == "download_failed":
            failure = manifest.get("download_failure") or {}
            reason = self._cancel_reason(unit["download_job_id"] or "")
            return self._download_ended(unit, "failed", reason, {**detail, "failure": failure})
        if status in UNFINISHED_LEASE:
            self._interrupted(unit, step="download", retry_state="handoff_ready", detail={**detail, "status": status})
            return True
        return self._downloaded(unit, str(unit["manifest_path"]))

    def _watch_download(self, unit: dict[str, Any], job: Mapping[str, Any]) -> None:
        """Stall detection, and the free-space floor for a unit whose size was not known."""
        job_id = unit["download_job_id"]
        now = self.now()
        received = int(job.get("received") or 0)
        previous = self._progress.get(job_id)
        if previous is None or previous[0] != received:
            self._progress[job_id] = (received, now)
        last = self._progress[job_id][1]
        manifest = self._manifest(unit)
        beat = policy.parse_iso((manifest or {}).get("download_progress_at"))
        if beat is not None and beat > last:
            last = beat
        fetched = policy.fetch_completed(manifest)
        if policy.download_stalled(last, now, self.policy.download_stall_seconds, fetched):
            self._cancel(unit, job_id, "stalled")
            return
        if not fetched:
            free, total = self.ports.disk.usage(self.campaign["workspace_root"])
            if free < self.policy.disk.download_floor_bytes:
                self.ledger.disk_event(
                    "cancel_download", self.stamp(), volume=self.campaign["workspace_root"], free=free, total=total,
                    reserve=self.policy.disk.download_floor_bytes, unit_key=unit["unit_key"],
                )
                self._cancel(unit, job_id, "disk")

    def _state_downloaded(self, unit: dict[str, Any]) -> bool:
        """The raw-header preflight; then Interactive's campaign_disposition says what the unit does."""
        extractor = str((self.pins.get("extractor") or {}).get("path") or "")
        attempt = self.ledger.open_attempt(unit["unit_key"], "preflight", self.stamp(), tool="msdial_repository_raw_metadata_preflight")
        result = self.ports.interactive.preflight(
            manifest_path=unit["manifest_path"], extractor_path=extractor, authorization_path=self._authorization(unit)
        )
        if result.get("ok") is False:
            # An extractor Interactive refuses as unverified or unpinned (0.5.17) is a contract pause, not a
            # failure of this unit: _fail reads it so.
            self._fail(unit, step="preflight", result=result, retry_state="downloaded", attempt_id=attempt)
            return True
        if result.get("extractor_found") is False:
            self.ledger.close_attempt(attempt, "fault", self.stamp(), detail={"extractor_found": False})
            self._pause("contract", "The pinned raw-metadata extractor was not found by Interactive.")
            return False
        # A broken contract pauses as "contract", which only an operator lifts: a pause that lifted itself
        # would run the preflight again, hours for a large unit, only to find the same record.
        held = dict(result.get("preflight_held") or {})
        try:
            disposition = policy.read_disposition(self._manifest(unit) or {})
            if disposition is not None and not disposition.applied:
                # Recorded as advice (the unit was no campaign unit as it was decided): classify_preflight
                # decides it again under the approval and applies it, without reading a header again.
                classified = self.ports.interactive.classify(
                    manifest_path=unit["manifest_path"], authorization_path=self._authorization(unit)
                )
                if classified.get("ok") is False:
                    self._fail(unit, step="classify", result=classified, retry_state="downloaded", attempt_id=attempt)
                    return True
                held = held or dict(classified.get("held") or {})
                disposition = policy.read_disposition(self._manifest(unit) or {})
        except policy.DispositionError as error:
            self.ledger.close_attempt(attempt, "fault", self.stamp(), detail={"contract": str(error)})
            self._pause("contract", f"Unit {unit['unit_key']}: {error}")
            return False
        if (disposition is None or not disposition.applied) and held:
            return self._preflight_held(unit, held, attempt)
        if disposition is None:
            self.ledger.close_attempt(attempt, "fault", self.stamp(), detail={"contract": "no campaign_disposition"})
            self._pause(
                "contract",
                "Interactive wrote no campaign_disposition after the preflight; this runner reads that decision "
                "and makes none of its own (classify_preflight, plan item 11).",
            )
            return False
        if not disposition.applied:
            self.ledger.close_attempt(attempt, "fault", self.stamp(), detail={"contract": "disposition not applied"})
            self._pause(
                "contract",
                f"Unit {unit['unit_key']}'s campaign_disposition is not applied, even by classify_preflight under the "
                "approval; the runner acts only on an applied disposition.",
            )
            return False
        pinned = str((self.pins.get("extractor") or {}).get("binary_sha256") or "")
        if (pinned and disposition.extractor.get("sha256") != pinned) or disposition.extractor.get("pinned") is False:
            # The pinned binary is where it was (the pin check found no difference before the unit started),
            # so Interactive ran another one, or reused verdicts another one made, or no longer counts the
            # build among its PINNED_BUILDS.
            self.ledger.close_attempt(attempt, "fault", self.stamp(), detail={"extractor": disposition.extractor})
            self._event("contract_broken", {"extractor_sha256": disposition.extractor.get("sha256"), "pinned_sha256": pinned},
                        unit["unit_key"])
            self._pause(
                "contract",
                f"Unit {unit['unit_key']} was classified by extractor {str(disposition.extractor.get('sha256'))[:12]}, "
                f"not the pinned {pinned[:12]}. Resume once that is understood; the preflight then runs again.",
            )
            return False
        record = disposition.as_dict()
        close = (attempt, "ok", False, {"disposition": disposition.disposition, "reasons": list(disposition.reasons),
                                        "warnings": list(disposition.warnings)})
        if disposition.disposition == "run":
            self._move(unit, "preflighted", disposition_json=json.dumps(record, sort_keys=True), close_attempt=close)
        elif disposition.disposition == "split":
            self._move(unit, "splitting", disposition_json=json.dumps(record, sort_keys=True), close_attempt=close)
        else:
            terminal = "skipped" if disposition.disposition == "skip" else "excluded"
            self._move(
                unit, "discarding", pending_terminal=terminal, disposition_json=json.dumps(record, sort_keys=True),
                terminal_detail=json.dumps({"reason": "preflight_" + disposition.disposition, "codes": list(disposition.reasons)}),
                close_attempt=close,
            )
        return True

    def _preflight_held(self, unit: dict[str, Any], held: Mapping[str, Any], attempt: int) -> bool:
        """Interactive read nothing and decided nothing: disposition_hold holds the unit (0.5.17), and it
        carries no applied disposition to act on. A run of its own that may still be going is waited for,
        uncounted; a unit split, finished or never preflighted cannot be run from here, which is the unit's
        failure - retried, and then its raw data go."""
        reason = str(held.get("reason") or "")
        result = {"ok": False, "reason": "unit_busy" if reason == "run_in_progress" else "preflight_held",
                  "held": dict(held)}
        self._fail(unit, step="preflight", result=result, retry_state="downloaded", attempt_id=attempt)
        return True

    def _state_splitting(self, unit: dict[str, Any]) -> bool:
        """Split a unit whose headers disagree, under the approval's "split"; the parts wait their turn."""
        self._require("split")
        attempt = self.ledger.open_attempt(unit["unit_key"], "split", self.stamp(), tool="msdial_split_repository_unit")
        result = self.ports.interactive.split(manifest_path=unit["manifest_path"], authorization_path=self._authorization(unit))
        parts = list(result.get("parts") or [])
        if result.get("ok") is False or not parts or not (result.get("written") or result.get("already_split")):
            if result.get("ok") is not False:
                result = {"ok": False, "reason": "split_not_written", "blockers": result.get("blockers") or []}
            self._fail(unit, step="split", result=result, retry_state="splitting", attempt_id=attempt)
            return True
        new_units = []
        for part in parts:
            workspace = Path(str(part["workspace"]))
            copy = workspace / "provenance" / "campaign-authorization.json"
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.campaign["authorization_path"], copy)
            new_units.append({
                "unit_key": str(part["analysis_unit_id"]), "state": "queued", "resume_state": "downloaded",
                "catalog_unit_id": unit["catalog_unit_id"], "repository": unit["repository"],
                "accession": unit["accession"], "order_index": unit["order_index"], "role": "split_part",
                "parent_unit_key": unit["unit_key"], "download_group_id": unit["download_group_id"],
                "class_proposal_id": unit["class_proposal_id"], "class_kind": unit["class_kind"],
                "instrument": unit["instrument"], "ion_mode": unit["ion_mode"], "workspace": str(workspace),
                "manifest_path": str(part["manifest_path"]), "handoff_path": unit["handoff_path"],
                "authorization_copy": str(copy), "raw_disposition": "deferred_to_parent",
            })
        self._move(
            unit, "split_parent", boundary="split", role="split_parent", add_units=new_units,
            close_attempt=(attempt, "ok", False, {"parts": [item["unit_key"] for item in new_units]}),
        )
        return True

    def _state_split_parent(self, unit: dict[str, Any]) -> bool:
        """The single trigger for a split parent's raw data: once every part has ended."""
        parts = [item for item in self.ledger.units() if item["parent_unit_key"] == unit["unit_key"]]
        if not parts or any(item["state"] not in ledger_module.TERMINAL_STATES for item in parts):
            return False
        produced = any(item["outputs_produced"] for item in parts)
        raw, detail, boundary = "kept", "", None
        if self.campaign["raw_retention_policy"] != "delete_after_validated_output":
            detail = "the campaign keeps raw data"
        elif not self._live("5"):
            detail = "no live approval covers boundary 5"
        elif produced:
            result = self.ports.interactive.release_split_parent(
                manifest_path=unit["manifest_path"], authorization_path=self._authorization(unit)
            )
            if result.get("ok") is not False and result.get("deleted"):
                raw, boundary, detail = "released", "5", "released after its parts' validated outputs"
            else:
                raw = "kept" if policy.classify_result(result) == policy.REFUSED else "held"
                detail = str(result.get("detail") or result.get("reason") or "not released")
                self._event("split_parent_release_held", {"detail": detail}, unit["unit_key"])
        else:
            result = self.ports.interactive.discard(
                manifest_path=unit["manifest_path"], authorization_path=self._authorization(unit),
                unit_id=unit["unit_key"],
            )
            if result.get("ok") is not False and result.get("deleted"):
                raw, boundary, detail = "discarded", "5", "no part produced validated outputs"
            else:
                raw = "kept" if policy.classify_result(result) == policy.REFUSED else "held"
                detail = str(result.get("detail") or result.get("reason") or "not discarded")
        reason = "parts_ended"
        self._write_record(unit, "split_done", reason, raw, detail)
        self._move(
            unit, "split_done", terminal_reason=reason, raw_disposition=raw, raw_detail=self.redact(detail),
            boundary=boundary, detail={"parts": [item["unit_key"] for item in parts]},
        )
        return True

    def _state_preflighted(self, unit: dict[str, Any]) -> bool:
        """The analysis CSV and the reviewed metadata, with the approved Class applied (boundary 3)."""
        self._require("3")
        attempt = self.ledger.open_attempt(unit["unit_key"], "prepare_metadata", self.stamp(), tool="msdial_prepare_repository_reanalysis")
        result = self.ports.interactive.prepare_metadata(manifest_path=unit["manifest_path"], authorization_path=self._authorization(unit))
        if result.get("ok") is False or not result.get("prepared"):
            if result.get("ok") is not False:
                result = {"ok": False, "reason": "not_prepared", "detail": result.get("message")}
            self._fail(unit, step="prepare_metadata", result=result, retry_state="preflighted", attempt_id=attempt)
            return True
        seed = dict((result.get("preview") or {}).get("answer_seed") or {})
        directory = self._unit_directory(unit)
        directory.mkdir(parents=True, exist_ok=True)
        _write_json(directory / "answer-seed.json", seed)
        self._move(
            unit, "metadata_prepared", boundary="3", input_path=str(result["input_path"]),
            close_attempt=(attempt, "ok", False, {"input_path": result["input_path"]}),
        )
        return True

    def answers(self, unit: Mapping[str, Any], minimum_peak_height: float | None = None) -> dict[str, Any]:
        """Interactive's answer seed, then the approved profile, then what the campaign pinned."""
        seed = _loads((self._unit_directory(unit) / "answer-seed.json").read_text(encoding="utf-8"))
        answers = _merge(seed, dict(self.profile.get("answers") or {}))
        ion_mode = str(seed.get("ion_mode") or unit.get("ion_mode") or "")
        answers = _merge(answers, dict((self.profile.get("by_ion_mode") or {}).get(ion_mode) or {}))
        # What Interactive seeded about the unit itself is not the profile's to change.
        answers["workflow_overrides"] = _merge(
            dict(answers.get("workflow_overrides") or {}), dict(seed.get("workflow_overrides") or {})
        )
        answers = resolve_library_references(answers, self.libraries)
        answers["console_path"] = str((self.pins.get("console") or {}).get("path") or "")
        answers["smoothing_method"] = "TimeBasedLinearWeightedMovingAverage"
        answers["target_peak_count_min"] = int(self.policy.peak_count_min)
        answers["target_peak_count_max"] = int(self.policy.peak_count_max)
        if minimum_peak_height is not None:
            answers["minimum_peak_height"] = float(minimum_peak_height)
        return answers

    def _console_start(self, unit: dict[str, Any], kind: str) -> bool:
        """One Console start, recorded before the call (the Console run and its attempt) and adopted
        from the unit manifest when the reply is lost."""
        diagnostic = kind == "tuning"
        slot = self.ledger.slot()
        if slot is not None and slot["unit_key"] != unit["unit_key"]:
            return False
        if self._pin_check_failed():
            return False
        self._require("4")
        console_kind, step, target, column = (
            ("diagnostic", "diagnostic_start", "diagnosing", "diagnostic_job_id") if diagnostic
            else ("production", "run_start", "running", "run_job_id")
        )
        timeout = self.policy.diagnostic_timeout_seconds if diagnostic else self.policy.console_timeout_seconds
        idle = self.policy.diagnostic_idle_timeout_seconds if diagnostic else self.policy.console_idle_timeout_seconds
        answers = self.answers(unit, None if diagnostic else unit["minimum_peak_height"])
        console_sha = str((self.pins.get("console") or {}).get("binary_sha256") or "0" * 64)
        run = self.ledger.open_console_run_of(unit["unit_key"], console_kind)
        run_id = run["console_run_id"] if run else self.ledger.open_console_run(
            unit["unit_key"], console_kind, console_sha, timeout, idle, self.stamp()
        )
        if self._orphan_console(unit) is not None:
            # A Console of this unit still runs that no job of the backend owns. The unit waits for it in the
            # hand, holding the Console slot, so no other Console starts beside it either.
            return False
        attempt = self.ledger.open_attempt(
            unit["unit_key"], step, self.stamp(), tool="msdial_start_peak_count_diagnostic" if diagnostic else "msdial_start_guided_analysis",
            arguments=self.redact({"input_path": unit["input_path"], "answers": answers}),
        )
        since = policy.parse_iso(self.ledger.attempts(unit["unit_key"])[-1]["started_at"]) or self.now()
        call = self.ports.interactive.start_diagnostic if diagnostic else self.ports.interactive.start_run
        result = call(
            input_path=unit["input_path"], answers=answers, authorization_path=self._authorization(unit),
            timeout_seconds=timeout, idle_timeout_seconds=idle,
        )
        job = str(result.get("job_id") or "") if result.get("ok") is not False and result.get("started", True) else ""
        if not job and result.get("reason") == "unit_busy":
            busy = str(result.get("live_job_id") or "")
            # Adopted only while the backend runs it. A job it reports ended or interrupted is an orphan's,
            # named from the manifest's run attempt: adopted, it read as interrupted again and again until
            # the unit failed and its raw data were discarded under that Console. The busy unit waits
            # instead, and its next start looks for the orphan first.
            if busy and self._job_live(busy):
                job = busy
        if not job:
            job = started_console_job(self._manifest(unit), kind, since, self._known_jobs(unit["unit_key"]))
        if job:
            self._move(
                unit, target, boundary="4", **{column: job}, console_job=(run_id, job),
                close_attempt=(attempt, "ok", False, {"job_id": job}),
            )
            return True
        if result.get("ok") is not False:
            result = {"ok": False, "reason": "not_started", "detail": result.get("message") or result.get("error")}
        self._fail(
            unit, step=step.replace("_start", ""), result=result,
            retry_state="metadata_prepared" if diagnostic else "prepared",
            attempt_id=attempt, console_run=(run_id, "not_started"),
        )
        return True

    def _state_metadata_prepared(self, unit: dict[str, Any]) -> bool:
        return self._console_start(unit, "tuning")

    def _console_ended(
        self, unit: dict[str, Any], job: Mapping[str, Any], *, step: str, retry_state: str
    ) -> bool:
        """A Console job that ended other than completed: a timeout, a cancel, a crash, a failure.

        An interrupted job is the backend's restart, not its Console's end: the Console may run on, and
        nothing is retried, skipped or discarded until it has been waited for or stopped.
        """
        if job.get("status") == "interrupted" and self._orphan_console(unit) is not None:
            return False
        run = self._open_console_end(unit, "failed")
        exit_code = job.get("exit_code")
        detail = {"job_id": job.get("id") or job.get("job_id"), "status": job.get("status"), "exit_code": exit_code,
                  "error": job.get("error"), "stop_reason": job.get("stop_reason")}
        job_id = unit[JOB_COLUMN[unit["state"]]]
        if self._cancel_reason(job_id) == "skip":
            self._production_ended(unit, "cancelled")
            self._move(
                unit, "discarding", pending_terminal="skipped", terminal_detail=json.dumps({"reason": "operator_skip"}),
                end_console_run=(run[0], "cancelled") if run else None,
                new_attempt={"step": step, "outcome": "cancelled", "counted": False, "detail": detail},
            )
            return True
        if job.get("status") == "interrupted":
            self._production_ended(unit, "interrupted")
            self._interrupted(unit, step=step, retry_state=retry_state, detail=detail,
                              console_run=(run[0], "interrupted") if run else None)
            return True
        outcome = "timeout" if exit_code == -3 else "cancelled" if exit_code == -4 else "failed"
        self._production_ended(unit, {"timeout": "timed_out"}.get(outcome, outcome))
        self._fail(unit, step=step, result={"ok": False, "reason": outcome, **detail}, retry_state=retry_state,
                   outcome=outcome, console_run=(run[0], outcome) if run else None, job_id=job_id)
        return True

    def _console_lost(self, unit: dict[str, Any], *, step: str, retry_state: str) -> bool:
        """The backend no longer knows the job: an orphaned Console is waited for or stopped first."""
        if self._orphan_console(unit) is not None:
            return False
        job_id = unit[JOB_COLUMN[unit["state"]]]
        run = self._open_console_end(unit, "interrupted")
        self._production_ended(unit, "interrupted")
        self._interrupted(unit, step=step, retry_state=retry_state, detail={"job_id": job_id, "job": "lost"}, console_run=run)
        return True

    def _job_live(self, job_id: str) -> bool:
        """Whether the backend runs this job now."""
        job = self.ports.interactive.job(job_id)
        return job.get("ok") is not False and str(job.get("status") or "") in ("queued", "running")

    def _orphan_console(self, unit: Mapping[str, Any]) -> dict[str, Any] | None:
        """The run attempt of a Console of this unit that may still be running although no job of the
        backend runs it, or None.

        On Windows a Console Interactive started is not stopped with its backend (no job object), so a
        backend restart leaves it reading the unit's raw data: the restarted backend reports its job
        interrupted, or no longer knows it, and its single-flight refuses another Console on the unit. One
        whose backend is gone is stopped here (kill_orphan); one whose backend still runs, or cannot be
        read, is waited for, and so is one that would not stop. A job of this backend that is still running
        is no orphan: a start adopts it from Interactive's unit_busy reply.
        """
        interactive = self.ports.interactive
        attempt = interactive.live_attempt(unit.get("manifest_path") or "")
        if attempt is None:
            return None
        job_id = str(attempt.get("job_id") or "")
        if job_id and self._job_live(job_id):
            return None
        if interactive.backend_alive(attempt) is not False:
            return attempt
        tried = self._orphan_kills.get(job_id)
        if tried is not None and (self.now() - tried).total_seconds() < self.policy.busy_retry_seconds:
            return attempt
        self._orphan_kills[job_id] = self.now()
        killed = bool(interactive.kill_orphan(attempt))
        if (job_id, killed) not in self._orphans_recorded:
            self._orphans_recorded.add((job_id, killed))
            self._event("orphan_killed", {"job_id": job_id, "console_pid": attempt.get("console_pid"), "killed": killed},
                        unit["unit_key"])
        return interactive.live_attempt(unit.get("manifest_path") or "")

    def _state_diagnosing(self, unit: dict[str, Any]) -> bool:
        job_id = unit["diagnostic_job_id"]
        job = self.ports.interactive.job(job_id)
        if job.get("ok") is False:
            if policy.classify_result(job) == policy.FAULT:
                self._pause("fault", f"The campaign backend did not answer for diagnostic job {job_id}.")
                return False
            estimate = self._estimate(unit)
            if estimate is not None:
                return self._diagnosed(unit, estimate)
            return self._console_lost(unit, step="diagnostic", retry_state="metadata_prepared")
        status = str(job.get("status") or "")
        if status in ("queued", "running"):
            self._resend_cancel(job_id)
            return False
        if status != "completed":
            return self._console_ended(unit, job, step="diagnostic", retry_state="metadata_prepared")
        estimate = self._estimate(unit)
        if estimate is None:
            run = self._open_console_end(unit, "completed")
            self._fail(unit, step="estimate", result={"ok": False, "reason": "estimate_unavailable"},
                       retry_state="metadata_prepared", console_run=run)
            return True
        return self._diagnosed(unit, estimate)

    def _estimate(self, unit: Mapping[str, Any]) -> dict[str, Any] | None:
        """The stepped threshold from the diagnostic, with the campaign's step rule applied."""
        arguments = {"job_id": unit["diagnostic_job_id"], "manifest_path": unit["manifest_path"],
                     "minimum": self.policy.peak_count_min, "maximum": self.policy.peak_count_max}
        first = self.ports.interactive.estimate(**arguments, step=0)
        if first.get("ok") is False or not first.get("ready"):
            return None
        representative = dict(first.get("representative") or {})
        step = policy.threshold_step(
            unit["instrument"], str(representative.get("instrument_family") or ""),
            thermo_raw_inputs(self._manifest(unit)),
        )
        estimate = dict(first.get("estimate") or {})
        if int(estimate.get("threshold_step") or 0) != step:
            second = self.ports.interactive.estimate(**arguments, step=step)
            if second.get("ok") is False or not second.get("ready"):
                return None
            estimate = dict(second.get("estimate") or {})
        return {"estimate": estimate, "representative": representative, "threshold_step": step}

    def _diagnosed(self, unit: dict[str, Any], estimate: Mapping[str, Any]) -> bool:
        values = estimate["estimate"]
        representative = estimate["representative"]
        self._move(
            unit, "diagnosed", minimum_peak_height=float(values.get("minimum_peak_height") or 0),
            diagnostic_peak_count=int(values.get("diagnostic_peak_count") or 0),
            threshold_step=int(estimate["threshold_step"]),
            representative_file=str(representative.get("file_name") or representative.get("file_path") or ""),
            order_source=str(representative.get("selection_reason") or ""),
            end_console_run=self._open_console_end(unit, "completed"),
            detail={"estimate": dict(values), "representative_reason": representative.get("selection_reason")},
        )
        return True

    def _state_diagnosed(self, unit: dict[str, Any]) -> bool:
        """The production plan, written without running; then the before-production gate.

        A FAIL there stops the run only for a check that breaks results (the user's rule of 2026-10-01):
        policy.BLOCKS_RUN_CHECKS, and any check the gate's report states blocks_run. Such a unit counts as
        failed, so it is retried twice and then its raw data go. Any other FAIL, a WARN, a check left not
        evaluable and a gate that could not run are recorded, and the unit runs.
        """
        answers = self.answers(unit, unit["minimum_peak_height"])
        attempt = self.ledger.open_attempt(unit["unit_key"], "prepare_run", self.stamp(), tool="msdial_prepare_guided_analysis")
        result = self.ports.interactive.prepare_guided(input_path=unit["input_path"], answers=answers)
        if result.get("ok") is False:
            self._fail(unit, step="prepare_run", result=result, retry_state="diagnosed", attempt_id=attempt)
            return True
        verdict = self._gate(unit, "before_production")
        mismatches = list((verdict or {}).get("run_policy_mismatches") or [])
        if mismatches:
            # The gate's run_policy and this reader disagree: read the way that blocks, and said so.
            self._event("run_policy_mismatch", {"point": "before_production", "mismatches": mismatches}, unit["unit_key"])
        blocking = list((verdict or {}).get("blocking_fail_ids") or [])
        if blocking:
            self._fail(
                unit, step="before_production_gate", attempt_id=attempt, retry_state="diagnosed",
                result={"ok": False, "reason": "gate_blocks_run", "blocking_fail_ids": blocking,
                        "run_policy_source": verdict.get("run_policy_source")},
                gate=("before_production", verdict),
            )
            return True
        self._move(
            unit, "prepared", close_attempt=(attempt, "ok", False, {}),
            gate=("before_production", verdict) if verdict else None,
        )
        return True

    def _state_prepared(self, unit: dict[str, Any]) -> bool:
        return self._console_start(unit, "run")

    def _state_running(self, unit: dict[str, Any]) -> bool:
        job_id = unit["run_job_id"]
        job = self.ports.interactive.job(job_id)
        if job.get("ok") is False:
            if policy.classify_result(job) == policy.FAULT:
                self._pause("fault", f"The campaign backend did not answer for run job {job_id}.")
                return False
            manifest = self._manifest(unit) or {}
            if policy.outputs_produced(manifest) and (manifest.get("finalized_run") or {}).get("job_id") == job_id:
                return self._run_done(unit)
            return self._console_lost(unit, step="run", retry_state="prepared")
        status = str(job.get("status") or "")
        if status in ("queued", "running"):
            self._resend_cancel(job_id)
            return False
        if status != "completed":
            return self._console_ended(unit, job, step="run", retry_state="prepared")
        if not policy.outputs_produced(self._manifest(unit)):
            run = self._open_console_end(unit, "completed")
            self._production_ended(unit, "outputs_not_validated")
            self._fail(unit, step="run", result={"ok": False, "reason": "outputs_not_validated", "job_id": job_id},
                       retry_state="prepared", console_run=run, job_id=job_id)
            return True
        return self._run_done(unit)

    def _run_done(self, unit: dict[str, Any]) -> bool:
        self._production_ended(unit, "outputs_produced")
        size = self.ports.disk.tree_bytes(str(self._workspace(unit)))
        self._move(
            unit, "run_done", outputs_produced=1, peak_workspace_bytes=size,
            end_console_run=self._open_console_end(unit, "completed"),
        )
        return True

    def _state_run_done(self, unit: dict[str, Any]) -> bool:
        """QA, publication and the data-mining handoff. None of them fails the unit; each is recorded."""
        qa = self.ports.interactive.qa(manifest_path=unit["manifest_path"])
        qa_ok = qa.get("ok") is not False
        publication = self.ports.interactive.publication(manifest_path=unit["manifest_path"], run_qa=qa_ok)
        if publication.get("ok") is False and qa_ok:
            publication = self.ports.interactive.publication(manifest_path=unit["manifest_path"], run_qa=False)
        handoff = self.ports.interactive.data_handoff(job_id=unit["run_job_id"])
        outcomes = {name: ("ok" if value.get("ok") is not False else str(value.get("reason") or "failed"))
                    for name, value in (("qa", qa), ("publication", publication), ("handoff", handoff))}
        self._move(
            unit, "published", detail=outcomes,
            new_attempt={"step": "publish", "outcome": "ok" if set(outcomes.values()) == {"ok"} else "failed",
                         "counted": False, "detail": outcomes},
        )
        return True

    def _gate(self, unit: Mapping[str, Any], point: str) -> dict[str, Any] | None:
        if point not in self.policy.gate_points:
            return None
        workspace = self._workspace(unit)
        if not workspace.is_dir():
            return None
        number = len(self.ledger.gate_verdicts(unit["unit_key"])) + 1
        report = self.directory / "gate" / unit["unit_key"] / f"{number:02d}-{point}.json"
        return dict(self.ports.gate.run(str(workspace), point, report))

    def _state_published(self, unit: dict[str, Any]) -> bool:
        verdict = self._gate(unit, "pre_cleanup")
        exit_code = verdict.get("exit_code") if verdict else None
        self._move(unit, "gated", gate=("pre_cleanup", verdict) if verdict else None, gate_exit_pre=exit_code)
        return True

    def _state_gated(self, unit: dict[str, Any]) -> bool:
        """Delete the raw data now that the outputs are produced, whatever the gate said (boundary 5)."""
        gate_note = f"gate pre_cleanup exit {unit['gate_exit_pre']}"
        raw, detail, boundary = "kept", "", None
        if self.campaign["raw_retention_policy"] != "delete_after_validated_output":
            detail = "the campaign keeps raw data"
        elif unit["role"] == "split_part":
            raw, detail = "deferred_to_parent", "released with the split parent once every part has ended"
        elif not self._live("5"):
            detail = "no live approval covers boundary 5"
        else:
            manifest = self._manifest(unit) or {}
            if manifest.get("status") == policy.RAW_CLEANED_STATUS:
                raw, boundary, detail = "released", "5", f"deleted after validated outputs; {gate_note}"
            else:
                result = self.ports.interactive.cleanup(manifest_path=unit["manifest_path"], authorization_path=self._authorization(unit))
                if result.get("ok") is not False and result.get("deleted"):
                    raw, boundary, detail = "released", "5", f"deleted after validated outputs; {gate_note}"
                elif policy.classify_result(result) == policy.REFUSED:
                    detail = f"deletion refused: {result.get('codes') or result.get('detail')}"
                elif policy.classify_result(result) == policy.CONTRACT:
                    self._pause("contract", f"The raw cleanup of unit {unit['unit_key']}: {result.get('reason')}: {result.get('detail')}")
                    return False
                else:
                    tried = self.ledger.count_attempts(unit["unit_key"], "release", ("failed",))
                    record = {"step": "release", "outcome": "failed", "counted": False,
                              "detail": self.redact({"blockers": result.get("blockers"), "detail": result.get("detail")})}
                    if tried + 1 < self.policy.max_attempts:
                        delay = self.policy.retry_delays_seconds[min(tried, len(self.policy.retry_delays_seconds) - 1)]
                        self._move(unit, "waiting_retry", resume_state="gated", new_attempt=record,
                                   next_attempt_at=policy.iso(self.now() + timedelta(seconds=float(delay))))
                        return True
                    self.ledger.close_attempt(self.ledger.open_attempt(unit["unit_key"], "release", self.stamp()),
                                              "failed", self.stamp(), detail=record["detail"])
                    raw = "held"
                    detail = f"not deleted: {result.get('blockers') or result.get('detail') or result.get('reason')}"
        self._move(unit, "releasing", raw_disposition=raw, raw_detail=self.redact(detail), boundary=boundary)
        return True

    def _state_releasing(self, unit: dict[str, Any]) -> bool:
        """The final gate, the Catalog's run record and the unit's campaign record; then done."""
        verdict = self._gate(unit, "final")
        exit_code = verdict.get("exit_code") if verdict else None
        terms = policy.report_terms(bool(unit["outputs_produced"]), exit_code)
        reason = "completed" if policy.COMPLETED in terms else "outputs_produced"
        self._record_run(unit, "completed" if reason == "completed" else "outputs_produced", exit_code)
        self._write_record(unit, "done", reason, unit["raw_disposition"], unit["raw_detail"] or "", final_exit=exit_code)
        self._move(unit, "done", terminal_reason=reason, gate_exit_final=exit_code,
                   gate=("final", verdict) if verdict else None)
        return True

    def _state_discarding(self, unit: dict[str, Any]) -> bool:
        """End a unit that produced no validated output, deleting its raw data under boundary 5."""
        pending = unit["pending_terminal"]
        info = _loads(unit["terminal_detail"])
        reason = str(info.get("reason") or pending)
        raw, detail, boundary = self._discard_raw(unit)
        if raw == "pause":
            return False
        if raw == "wait":
            tried = self.ledger.count_attempts(unit["unit_key"], "discard", ("failed",))
            record = {"step": "discard", "outcome": "failed", "counted": False, "detail": {"detail": detail}}
            if tried + 1 < self.policy.max_attempts:
                delay = self.policy.retry_delays_seconds[min(tried, len(self.policy.retry_delays_seconds) - 1)]
                self._move(unit, "waiting_retry", resume_state="discarding", pending_terminal=pending,
                           terminal_detail=unit["terminal_detail"], new_attempt=record,
                           next_attempt_at=policy.iso(self.now() + timedelta(seconds=float(delay))))
                return True
            raw = "held"
        # The gate reads a unit manifest; a unit that never downloaded has none to read.
        verdict = self._gate(unit, "final") if unit["manifest_path"] and Path(unit["manifest_path"]).is_file() else None
        exit_code = verdict.get("exit_code") if verdict else None
        self._record_run(unit, pending, exit_code)
        self._write_record(unit, pending, reason, raw, detail, final_exit=exit_code)
        self._move(
            unit, pending, terminal_reason=reason, terminal_detail=json.dumps(info, ensure_ascii=False, sort_keys=True),
            raw_disposition=raw, raw_detail=self.redact(detail), boundary=boundary, gate_exit_final=exit_code,
            gate=("final", verdict) if verdict else None,
        )
        return True

    def _discard_raw(self, unit: Mapping[str, Any]) -> tuple[str, str, str | None]:
        """(raw disposition, detail, boundary crossed) for a unit ending without validated output, ("wait", ...)
        to try again, or ("pause", ...) when the campaign paused on a contract. Each deletion is the one
        Interactive performs for the unit's state: its cleanup for outputs that validated, else its discard
        (InteractivePort.discard: the approval-taking one once plan item 14 lands, the fallback until then)."""
        manifest = self._manifest(unit) if unit["manifest_path"] else None
        if manifest is None:
            return "none", "no raw data were downloaded", None
        if unit["role"] == "split_part":
            return "deferred_to_parent", "deleted with the split parent once every part has ended", None
        if self.campaign["raw_retention_policy"] != "delete_after_validated_output":
            return "kept", "the campaign keeps raw data", None
        status = manifest.get("status")
        if status == "discarded":
            return "discarded", "deleted", "5"
        if status == policy.RAW_CLEANED_STATUS:
            return "released", "deleted by the cleanup of its validated outputs", "5"
        if not self._live("5"):
            return "kept", "no live approval covers boundary 5", None
        if policy.outputs_produced(manifest):
            # The run validated although its job ended otherwise: Interactive deletes such raw data only by
            # the normal cleanup, and discards only what produced no validated output.
            result = self.ports.interactive.cleanup(manifest_path=unit["manifest_path"], authorization_path=self._authorization(unit))
            if result.get("ok") is not False and result.get("deleted"):
                return "released", f"deleted: the unit {unit['pending_terminal']}, its outputs validated", "5"
            if policy.classify_result(result) == policy.REFUSED:
                return "kept", f"deletion refused: {result.get('codes') or result.get('detail')}", None
            if policy.classify_result(result) == policy.CONTRACT:
                return self._contract_pause(unit, "raw cleanup", result)
            return "wait", str(result.get("blockers") or result.get("detail") or result.get("reason") or "not deleted"), None
        result = self.ports.interactive.discard(
            manifest_path=unit["manifest_path"], authorization_path=self._authorization(unit),
            unit_id=unit["unit_key"], parent_unit_id=unit["parent_unit_key"] or "",
        )
        if result.get("ok") is not False and result.get("deleted"):
            return "discarded", f"deleted: the unit {unit['pending_terminal']}", "5"
        if policy.classify_result(result) == policy.REFUSED:
            return "kept", f"deletion refused: {result.get('codes') or result.get('detail')}", None
        if policy.classify_result(result) == policy.CONTRACT:
            return self._contract_pause(unit, "raw discard", result)
        blockers = [str(code) for code in result.get("blockers") or []]
        detail = str(result.get("detail") or result.get("reason") or "not deleted")
        if policy.discard_blocked_for_good(blockers):
            # A failed run that left an mzTab-M: Interactive discards no such unit's raw data, and asking
            # again changes nothing. Nothing here works around that by touching the unit's output: the raw
            # data are held, counted apart, until Interactive's own discard deletes them (plan item 14).
            return "held", f"not deleted ({', '.join(blockers)}): {detail}", None
        return "wait", detail, None

    def _contract_pause(self, unit: Mapping[str, Any], step: str, result: Mapping[str, Any]) -> tuple[str, str, None]:
        """A deletion this Interactive cannot make as called (a tool or parameter it lacks): the campaign's
        contract, not the unit's end. The unit stays where it is until an operator resumes."""
        self._pause("contract", f"The {step} of unit {unit['unit_key']}: {result.get('reason')}: {result.get('detail')}")
        return "pause", str(result.get("detail") or ""), None

    def _state_waiting_retry(self, unit: dict[str, Any]) -> bool:
        due = policy.parse_iso(unit["next_attempt_at"])
        if due is not None and due > self.now():
            return False
        self._move(unit, unit["resume_state"], pending_terminal=unit["pending_terminal"] if unit["resume_state"] == "discarding" else None,
                   terminal_detail=unit["terminal_detail"] if unit["resume_state"] == "discarding" else None,
                   detail={"woke": True})
        return True

    def _state_queued(self, unit: dict[str, Any]) -> bool:
        self._move(unit, unit["resume_state"], detail={"turn": True})
        return True

    def _state_deferred_disk(self, unit: dict[str, Any]) -> bool:
        verdict = self._disk_verdict(unit)
        if verdict.never_fits or not verdict.admit:
            return False
        self._move(unit, unit["resume_state"] or "handoff_ready", detail={"disk": verdict.as_dict()})
        return True

    # ---- records --------------------------------------------------------------------------------------

    def _record_run(
        self, unit: Mapping[str, Any], status: str, exit_code: int | None, *, job_id: str | None = None,
        final: bool = True,
    ) -> None:
        """The Catalog's analysis_run row for one production run of a unit, keyed <unit>:<job>. Never fatal.

        Every production attempt is recorded as it ends (final false: its own end, no gate verdict), failed
        ones included; the unit's end then completes the record of its last run with how the unit ended
        and the final gate's verdict. The Catalog keeps what a later call leaves empty.
        """
        job = job_id or unit.get("run_job_id")
        if not job:
            return
        workspace = self._workspace(unit)
        manifest = self._manifest(unit) or {}
        mztab_path, mztab_sha256 = "", ""
        validated_job = str((manifest.get("finalized_run") or {}).get("job_id") or job)
        for item in (manifest.get("mztab_validation") or {}).get("files") or [] if validated_job == job else []:
            path = Path(str(item.get("file") or ""))
            try:
                mztab_path = path.resolve().relative_to(workspace.resolve()).as_posix()
                mztab_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            except (OSError, ValueError):
                mztab_path, mztab_sha256 = "", ""
            break
        values: dict[str, Any] = {
            "run_id": f"{unit['unit_key']}:{job}", "unit_id": unit["catalog_unit_id"], "status": status.replace(" ", "_"),
            "class_proposal_id": unit.get("class_proposal_id") or "",
            "interactive_version": str((self.pins.get("interactive") or {}).get("version") or ""),
            "msdial_version": str((self.pins.get("console") or {}).get("version") or ""),
            "mztab_path": mztab_path, "mztab_sha256": mztab_sha256,
            "provenance": {
                "campaign_id": self.campaign["campaign_id"], "approval_id": self.approval["approval_id"],
                "manifest_digest": self.campaign["manifest_digest"],
                "console_sha256": str((self.pins.get("console") or {}).get("binary_sha256") or ""),
                "extractor_sha256": str((self.pins.get("extractor") or {}).get("binary_sha256") or ""),
                "libraries": [{"name": item["name"], "sha256": item["sha256"]} for item in self.pins.get("libraries") or []],
                "minimum_peak_height": unit.get("minimum_peak_height"), "threshold_step": unit.get("threshold_step"),
                "attempt_status": status.replace(" ", "_") if not final else None,
            },
        }
        if final:
            values.update(gate_verdict=policy.gate_verdict_token(exit_code), gate_exit_code=exit_code)
            values["provenance"].pop("attempt_status")
        result = self.ports.catalog.record_run(**self.redact(values))
        if result.get("ok") is False:
            self._event("catalog_run_not_recorded", {"run_id": values["run_id"], "detail": result.get("detail")}, unit["unit_key"])

    def _production_ended(self, unit: Mapping[str, Any], status: str) -> None:
        """Record the production attempt of a unit in 'running' as it ends, before the transition that says so."""
        if unit.get("state") == "running":
            self._record_run(unit, status, None, job_id=unit.get("run_job_id"), final=False)

    def _write_record(
        self, unit: Mapping[str, Any], state: str, reason: str, raw: str, raw_detail: str, *, final_exit: int | None = None
    ) -> None:
        """campaign-record.json beside the unit's artifacts: which approval covered what, and how it ended."""
        workspace = self._workspace(unit)
        if not workspace.is_dir():
            return
        crossings = [
            {"boundary": item["boundary"], "at": item["at"], "to_state": item["to_state"], "approval_id": item["approval_id"]}
            for item in self.ledger.transitions(unit["unit_key"]) if item["boundary"]
        ]
        verdicts = [
            {"point": item["point"], "outcome": item["outcome"], "exit_code": item["exit_code"],
             "stage_reached": item["stage_reached"], "fail_ids": json.loads(item["fail_ids_json"]),
             "blocking_fail_ids": json.loads(item["blocking_fail_ids_json"]), "run_policy_source": item["run_policy_source"],
             "strict_hold_ids": json.loads(item["strict_hold_ids_json"])}
            for item in self.ledger.gate_verdicts(unit["unit_key"])
        ]
        outputs = bool(unit.get("outputs_produced"))
        record = {
            "schema": UNIT_RECORD_SCHEMA,
            "campaign_id": self.campaign["campaign_id"],
            "approval_id": self.approval["approval_id"],
            "manifest_digest": self.campaign["manifest_digest"],
            "authorization_sha256": self.campaign["authorization_sha256"],
            "unit_key": unit["unit_key"],
            "catalog_unit_id": unit["catalog_unit_id"],
            "role": unit["role"],
            "parent_unit_key": unit.get("parent_unit_key"),
            "state": state,
            "terminal_reason": reason,
            "report_terms": policy.report_terms(outputs, final_exit),
            "outputs_produced": outputs,
            "completed": policy.COMPLETED in policy.report_terms(outputs, final_exit),
            "boundary_crossings": crossings,
            "campaign_disposition": _loads(unit.get("disposition_json")) or None,
            "gate_verdicts": verdicts,
            "raw_disposition": raw,
            "raw_detail": raw_detail,
            "failures": unit.get("failures"),
            "interruptions": unit.get("interruptions"),
            "jobs": {"download": unit.get("download_job_id"), "diagnostic": unit.get("diagnostic_job_id"),
                     "run": unit.get("run_job_id")},
            "minimum_peak_height": unit.get("minimum_peak_height"),
            "threshold_step": unit.get("threshold_step"),
            "diagnostic_peak_count": unit.get("diagnostic_peak_count"),
            "pins": {
                "console_sha256": (self.pins.get("console") or {}).get("binary_sha256"),
                "extractor_sha256": (self.pins.get("extractor") or {}).get("binary_sha256"),
                "libraries": [{"name": item["name"], "sha256": item["sha256"]} for item in self.pins.get("libraries") or []],
            },
            "written_at": self.stamp(),
        }
        _write_json(workspace / "campaign-record.json", self.redact(record))


def _write_json(path: Path, value: Any) -> None:
    from .ports import write_json_atomic

    write_json_atomic(path, value)


# ---- status, for the operator and for the later verification work ----------------------------------

def unit_status(unit: Mapping[str, Any]) -> dict[str, Any]:
    terms = policy.report_terms(bool(unit["outputs_produced"]), unit["gate_exit_final"])
    return {
        **{key: unit.get(key) for key in TSV_COLUMNS if key != "report_terms"},
        "report_terms": terms,
        "outputs_produced": bool(unit["outputs_produced"]),
        "completed": policy.COMPLETED in terms,
        "disposition": _loads(unit.get("disposition_json")) or None,
        "terminal_detail": unit.get("terminal_detail"),
        "raw_detail": unit.get("raw_detail"),
        "next_attempt_at": unit.get("next_attempt_at"),
    }


def summary(ledger: ledger_module.Ledger) -> dict[str, Any]:
    """Counts the operator reads. 'outputs produced' and 'completed' are kept apart (contradiction 18)."""
    units = ledger.units()
    states: dict[str, int] = {}
    raw: dict[str, int] = {}
    for unit in units:
        states[unit["state"]] = states.get(unit["state"], 0) + 1
        raw[unit["raw_disposition"]] = raw.get(unit["raw_disposition"], 0) + 1
    runner = ledger.runner()
    held = [unit for unit in units if unit["raw_disposition"] == "held"]
    return {
        "units": len(units),
        "states": dict(sorted(states.items())),
        policy.OUTPUTS_PRODUCED: sum(1 for unit in units if unit["outputs_produced"]),
        policy.COMPLETED: sum(1 for unit in units if unit["outputs_produced"] and unit["gate_exit_final"] == 0),
        "raw_disposition": dict(sorted(raw.items())),
        # Still on the volume, against the rules: the disk guard sees them as used space, and so should the
        # person reading this.
        "raw_held": {"units": len(held), "downloaded_bytes": sum(int(unit["downloaded_bytes"] or 0) for unit in held),
                     "unit_keys": [unit["unit_key"] for unit in held]},
        "paused": {"kind": runner["pause_kind"], "reason": runner["pause_reason"], "at": runner["paused_at"]}
        if runner["paused"] else None,
        "terms": {
            policy.OUTPUTS_PRODUCED: "every MS-DIAL output present and the mzTab-M validated",
            policy.COMPLETED: "outputs produced, and the final gate --strict exited 0",
        },
    }


def export_status(ledger: ledger_module.Ledger) -> tuple[dict[str, Any], str]:
    """The per-unit status as JSON and as TSV, for the later verification work."""
    campaign = ledger.campaign()
    rows = [unit_status(unit) for unit in ledger.units()]
    document = {
        "schema": STATUS_SCHEMA,
        "campaign_id": campaign["campaign_id"],
        "manifest_digest": campaign["manifest_digest"],
        "summary": summary(ledger),
        "units": rows,
    }
    lines = ["\t".join(TSV_COLUMNS)]
    for row in rows:
        values = []
        for column in TSV_COLUMNS:
            value = row.get(column)
            if isinstance(value, list):
                value = ";".join(value)
            elif isinstance(value, bool):
                value = int(value)
            values.append("" if value is None else str(value).replace("\t", " ").replace("\n", " "))
        lines.append("\t".join(values))
    return document, "\n".join(lines) + "\n"
