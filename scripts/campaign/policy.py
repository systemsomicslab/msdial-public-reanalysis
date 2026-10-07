"""The campaign's rules, as pure functions the state machine applies and the tests pin.

THE RULES ARE THE USER'S. Hiroshi Tsugawa decided them on 2026-09-30 for the unattended
full-repository reanalysis, and this module is their one written form:

- A unit failure never stops the campaign. A failed unit is retried twice; after its third failure it
  is failed and its raw data are deleted.
- Raw data are deleted once every MS-DIAL output is present and the mzTab-M validates, whatever the gate
  verdict; the verdict is recorded beside the deletion. The raw data of skipped and excluded units are
  deleted too.
- One Console at a time. Nothing is fetched ahead of the unit in hand unless prefetch says so (0 by
  default).
- Stall detection only: the Console watch that Interactive 0.5.14 added (idle_timeout_seconds) and a
  cancel for a download whose bytes stop arriving. There is no outer limit on the raw-metadata
  extractor; Interactive's own per-chunk limits are authoritative.
- No per-unit size limit. A disk that is short pauses the campaign; it never ends a unit.

And the gate rule, decided on 2026-10-01 and completed on 2026-10-02:

- Before production, a FAIL stops a unit's MS-DIAL run for ELIG-1, ACQ-1, SUM-1, CNT-1, INP-1, ID-1,
  PRE-2 and CONV-1 (BLOCKS_RUN_CHECKS), and so does one of them left not evaluable where it is required
  (strict_failures). The unit then counts as failed. Any other FAIL is recorded and the unit runs.
- A before-production gate that gives no report the runner can use (gate_report_problem) holds the unit:
  it does not run, its raw data are kept, nothing is counted against it, and the other units go on. A
  recheck runs the gate again. After the run a missing report is only recorded.
- "Stop" is per unit: a gate verdict, a failure or a hold stops that unit's analysis, never the runner. A
  pin change, a short disk and a repository outage pause the whole campaign, and each lifts by itself.

And two defaults the runner proposed and the user did not object to on 2026-10-03 ("案AでOK"):

- An Interactive backend that does not answer is the fourth pause of the whole campaign (CAMPAIGN_PAUSES),
  and lifts by itself at the fault recheck. Not answering is a refused connection (backend_unavailable),
  and a connection that times out, is reset or breaks off mid-reply (BACKEND_SILENT_ERRORS): neither is
  the unit's failure.
- A reply or a record of Interactive's that the runner cannot read for one unit (CONTRACT), a reply that
  does not parse among them (UNREADABLE_REPLY_ERRORS), holds that unit, as a missing gate report does, and
  pauses nothing; a recheck makes the step again.

And the AIF rule of 2026-10-07: a multi-collision-energy AIF unit is HELD until a patched Console exists, not
run, its raw data kept, and not counted as a failure. Interactive 0.5.31 says so in the disposition itself:
disposition "skip", hold true and the reason aif_multi_ce_awaiting_console (HOLD_FOR_CONSOLE). The runner
holds such a unit (disposition_held) instead of skipping it, and the hold lifts only at an operator's
explicit recheck-held (Disposition.held), or at the operator's skip, whose discard passes Interactive
release_disposition_hold (DISPOSITION_HELD_BLOCKER).

WHAT THIS MODULE NEVER DECIDES. Whether a unit may run. Interactive's classify_preflight reads the raw
headers and writes that decision into the unit manifest as campaign_disposition (schema
msdial-campaign-disposition.v1); read_disposition only reads and checks it. A second mapping of
preflight outcomes to dispositions here would be the "consistent with itself, disagreeing with
everything else" defect this programme keeps finding (review contradiction 11).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

DISPOSITION_SCHEMA = "msdial-campaign-disposition.v1"
DISPOSITIONS = ("run", "skip", "exclude", "split")
# The hold Interactive 0.5.31 writes into a disposition (user decision, 2026-10-07): a multi-collision-energy AIF
# unit waits for a Console that deconvolutes each energy (MsdialWorkbench #825), and is neither run nor skipped.
# Interactive records it as disposition "skip", hold true, with this among its reasons.
HOLD_FOR_CONSOLE = "aif_multi_ce_awaiting_console"
# The Console's AcquisitionType enum is {DDA, SWATH, AIF, None}, and an unparsable value silently becomes
# DDA (review correction 3), so these are the only values a per-file record may carry.
CONSOLE_ACQUISITION_TYPES = ("DDA", "SWATH", "AIF")
# The two report terms (review contradiction 18). A unit's outputs are produced when its mzTab-M
# validated; it is completed only when the gate --strict exited 0, and READ-1 holds exit 4 until a
# person's reading is recorded, which no runner ever does.
OUTPUTS_PRODUCED = "outputs produced"
COMPLETED = "completed"
# Manifest statuses that Interactive's run job leaves once the Console finished, every expected export
# exists and the mzTab-M validated (repository_reanalysis.CLEANUP_READY_STATUSES).
VALIDATED_STATUSES = frozenset({"mztab_validated", "completed", "cleanup_pending_confirmation"})
RAW_CLEANED_STATUS = "raw_cleaned"
_SHA256 = re.compile(r"[0-9a-f]{64}")
# Fourier-transform analysers in the catalog's instrument text, which is the submitter's own words: "Thermo
# Scientific Exactive" and "Exactive Plus" name no Q, "IQ-X tribrid" no Orbitrap, and "Bruker APEX-Qe 9.4T" is
# an FT-ICR. The step asked of an Interactive before 0.5.28 (threshold_step); since 0.5.28 the family
# Interactive reads from the file decides the step, and this text only leaves a note where it disagrees.
# The tokens are Interactive 0.5.28's own (workflow.instrument_family_from_text: _ORBITRAP_INSTRUMENT,
# _FT_ICR_INSTRUMENT, _FOURIER_GENERIC, msdial-interactive-app#61), with its word boundaries, so an HPLC column
# beside the instrument ("Zorbax Eclipse Plus C18", "Synergi Fusion-RP") names no Fourier-transform analyser.
# The gate reads the same (verify-run-invariants.py FOURIER_INSTRUMENT; tests hold the two equal).
_FOURIER_INSTRUMENT = re.compile(
    r"orbitrap|exactive|exploris|astral|\blumos\b|\bascend\b|tribrid|\bid-x\b|"
    r"\bfusion\b(?![\s-]*rp)|(?<!zorbax )\beclipse\b(?![\s-]*(?:plus|xdb|c18|c8))"
    r"|ft[\s-]?icr|fticr|cyclotron|solarix|scimax|mrms\b|\bapex(?![a-z])|\bltq[\s-]?ft(?![a-z])"
    r"|\bftms\b|fourier",
    re.IGNORECASE,
)
# The before-production checks whose FAIL stops a unit's MS-DIAL run: the ones that break results, which the
# user's rule of 2026-10-01 named (the first five) and the user's placing of 2026-10-02 completed (ID-1,
# PRE-2 and CONV-1). One of them left not evaluable where it is required stops the run too (2026-10-02,
# run_blocking_unevaluated). A FAIL of any other check (CLS-1/2/3, ORD-1, PKH-1, SPL-1) is recorded and the
# unit runs. A gate whose checks carry a run_policy can name more blocking checks; it cannot take one of
# these off the list (run_blocking_failures). The gate's RUN_POLICY states the same eight
# (tests/test_verify_run_policy.py holds the two lists equal).
BLOCKS_RUN = "blocks_run"
RECORD_ONLY = "record_only"
BLOCKS_RUN_CHECKS = ("ELIG-1", "ACQ-1", "SUM-1", "CNT-1", "INP-1", "ID-1", "PRE-2", "CONV-1")


class DispositionError(ValueError):
    """A campaign_disposition that does not have the shared contract's shape.

    Interactive writes the record and this runner reads it, in two repositories merged independently. A
    record the runner cannot read is a contract mismatch between the two, never the unit's failure, which
    would delete its raw data for the runner's fault. "Stop" is per unit (2026-10-02), so the unit is held
    (contract_held), uncounted and with its raw data kept, until a recheck finds a record it can read, and
    the other units go on.
    """


@dataclass(frozen=True)
class Disposition:
    disposition: str
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    excluded_inputs: tuple[dict[str, str], ...]
    split_key: dict[str, Any] | None
    decided_at: str
    extractor: dict[str, Any]
    # Interactive 0.5.17: true only when the unit was a campaign unit as it was decided, so the disposition
    # set execution_allowed, the status and each input's acquisition type. Outside a campaign it is advice,
    # and the runner acts on no disposition that is not applied.
    applied: bool = False
    # Interactive 0.5.31: a skip that is a hold (the AIF rule of 2026-10-07): the unit waits, its raw data kept.
    hold: bool = False

    @property
    def held(self) -> bool:
        """Whether the disposition holds the unit rather than ending it: a skip with hold true. A hold keeps the
        raw data and counts nothing; only an operator's recheck-held asks Interactive again."""
        return self.hold and self.disposition == "skip"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": DISPOSITION_SCHEMA,
            "disposition": self.disposition,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "excluded_inputs": [dict(item) for item in self.excluded_inputs],
            "split_key": self.split_key,
            "decided_at": self.decided_at,
            "extractor": dict(self.extractor),
            "applied": self.applied,
            "hold": self.hold,
        }


def read_disposition(manifest: Mapping[str, Any]) -> Disposition | None:
    """The unit manifest's campaign_disposition, None when Interactive wrote none, or DispositionError.

    A record without "applied" (written before Interactive 0.5.17) reads as not applied."""
    record = manifest.get("campaign_disposition") if isinstance(manifest, Mapping) else None
    if record is None:
        return None
    if not isinstance(record, Mapping):
        raise DispositionError("campaign_disposition is not an object.")
    problems: list[str] = []
    if record.get("schema") != DISPOSITION_SCHEMA:
        problems.append(f"schema is {record.get('schema')!r}, not {DISPOSITION_SCHEMA!r}")
    disposition = str(record.get("disposition") or "")
    if disposition not in DISPOSITIONS:
        problems.append(f"disposition is {disposition!r}, not one of {list(DISPOSITIONS)}")

    def codes(key: str) -> tuple[str, ...]:
        value = record.get(key)
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            problems.append(f"{key} is not a list of codes")
            return ()
        return tuple(value)

    reasons = codes("reasons")
    warnings = codes("warnings")
    excluded: list[dict[str, str]] = []
    for item in record.get("excluded_inputs") or []:
        if not isinstance(item, Mapping) or not str(item.get("path") or "").strip():
            problems.append("an excluded_inputs entry names no path")
            continue
        excluded.append({"path": str(item["path"]), "reason": str(item.get("reason") or "")})
    split_key = record.get("split_key")
    if split_key is not None and not isinstance(split_key, Mapping):
        problems.append("split_key is neither an object nor null")
    if disposition in {"skip", "exclude"} and not reasons:
        problems.append(f"a {disposition} disposition names no reason")
    extractor = record.get("extractor")
    if not isinstance(extractor, Mapping) or not _SHA256.fullmatch(str(extractor.get("sha256") or "")):
        problems.append("extractor names no 64-digit sha256")
        extractor = {}
    applied = record.get("applied", False)
    if not isinstance(applied, bool):
        problems.append("applied is neither true nor false")
    # A disposition before Interactive 0.5.31 says nothing of a hold. A hold is a skip that waits (2026-10-07);
    # on any other disposition it is a record of another shape, which holds the unit as a contract result.
    hold = record.get("hold", False)
    if not isinstance(hold, bool):
        problems.append("hold is neither true nor false")
    elif hold and disposition != "skip":
        problems.append(f"hold is true on a {disposition!r} disposition, where only a skip is held")
    if problems:
        raise DispositionError("campaign_disposition: " + "; ".join(problems) + ".")
    return Disposition(
        disposition=disposition,
        reasons=reasons,
        warnings=warnings,
        excluded_inputs=tuple(excluded),
        split_key=dict(split_key) if isinstance(split_key, Mapping) else None,
        decided_at=str(record.get("decided_at") or ""),
        extractor=dict(extractor),
        applied=applied is True,
        hold=hold is True,
    )


@dataclass(frozen=True)
class DiskPolicy:
    """How much free space a unit needs before its download starts, and what a short disk does.

    MEASURED, NOT GUESSED (review contradiction 20). MTBLS2207's raw tree held 345 MB of MS-DIAL
    intermediates (.dcl, .pai2, .arf, .arf2, .aef) beside 615 MB of mzML, 56 % on top of the raw bytes,
    before any retry. file_factor is the raw bytes, those 56 % and about 10 % of outputs. An archive is
    kept beside its extraction until the unit's cleanup, so archive_factor adds the archive once more.
    Both factors rise to what completed units actually used (observed_factor) once enough are measured.

    UNKNOWN SIZES ARE A POLICY. The Catalog lists 4,205 declared-pool container zips at size 0; for those
    download_plan reports size_known false and the known part as a lower bound. Such a unit needs its
    known bytes times the factor plus unknown_size_reserve_bytes, and while it downloads the free space
    is watched: below download_floor_bytes the download is cancelled (its partial file is kept for a
    resume) and the campaign pauses until space returns.

    THE FLOOR ACTS FIRST. maximum_gb, the limit Interactive's lease enforces while it streams, is what the
    volume could hold above its reserve (download_bound_gb), not what is free now. A limit taken from the
    free space would end a large download of unknown size as a failed unit - and its retry at once, the
    partial file then taking up the space - where the user's rule is a pause.
    """

    file_factor: float = 1.66
    archive_factor: float = 2.66
    reserve_bytes: int = 500 * 1000**3
    reserve_fraction: float = 0.05
    unknown_size_reserve_bytes: int = 200 * 1000**3
    download_floor_bytes: int = 100 * 1000**3
    observed_minimum_units: int = 5
    observed_quantile: float = 0.9


@dataclass(frozen=True)
class CampaignPolicy:
    # The first attempt and two retries: "a failed unit is retried twice, then its raw data are deleted".
    max_attempts: int = 3
    retry_delays_seconds: tuple[int, ...] = (600, 3600)
    # A stop the unit did not cause (a reboot, a backend restart) is retried without counting against it,
    # this many times; past that it counts, so a unit that always kills its backend still ends.
    max_interruptions: int = 5
    # Downloads from one repository failing in a row on the network (a 5xx, a timeout, a reset, a stall) for
    # this many different download groups are a repository outage, not unit failures: the campaign pauses
    # as a fault, the unit that tripped it is not counted (it uses an interruption), and at the recheck a
    # unit of another download group is tried first. Groups, not units: the units of one group share
    # objects, so their failures are one object's. 0 turns the breaker off. (The name is the field's
    # since the first manifest.)
    outage_units: int = 3
    prefetch: int = 0
    poll_seconds: float = 30.0
    busy_retry_seconds: float = 120.0
    # A campaign fault (a backend that will not answer, a repository outage) is looked at again after this
    # long, and the step retried; it pauses again if the fault is still there.
    fault_recheck_seconds: float = 3600.0
    # Raw data held against the rules (a deletion Interactive refused) are looked at again when the runner
    # starts and after this long, and deleted once Interactive's deletion accepts them. A unit held because
    # the before-production gate gave no usable report (gate_held), or because Interactive's reply or record
    # for it could not be read (contract_held), has its step made again when the runner starts and this long
    # after each try.
    held_recheck_seconds: float = 6 * 3600.0
    # Stall detection, never an outer limit (review contradiction 10). 0 means no limit.
    console_idle_timeout_seconds: float = 6 * 3600.0
    console_timeout_seconds: float = 0.0
    diagnostic_idle_timeout_seconds: float = 3 * 3600.0
    diagnostic_timeout_seconds: float = 0.0
    download_stall_seconds: float = 1800.0
    gate_timeout_seconds: float = 1200.0
    gate_points: tuple[str, ...] = ("before_production", "pre_cleanup", "final")
    peak_count_min: int = 3000
    peak_count_max: int = 6000
    # Automatic alignment RT correction (decided 2026-10-07): MsdialWorkbench #826's local outlier test, with at
    # most 12 anchors and the Console's own local window. The runner pins both answers over the profile, as it
    # pins the peak-count targets, so a profile cannot leave them out and run at Interactive's default of 6
    # anchors, or uncorrected. A policy recorded before these fields (a manifest approved before 2026-10-07)
    # does not name them, and its runs are not pinned: automatic_rt_correction_pinned reads the record.
    automatic_rt_correction: bool = True
    automatic_rt_correction_maximum_anchors: int = 12
    # A production run whose Console could not select anchors (AutomaticAlignmentRetentionTimeCorrection
    # .Build: too few candidates, or too few anchors covering enough samples) ends at exit -1 with no output,
    # and would end so again on every retry. With the fallback, the unit's next attempts run without the
    # correction, and its record and status say so; without it, the unit is retried and ends as any failure.
    automatic_rt_correction_fallback: bool = True
    runner_lock_stale_seconds: float = 600.0
    heartbeat_seconds: float = 30.0
    disk: DiskPolicy = field(default_factory=DiskPolicy)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["retry_delays_seconds"] = list(self.retry_delays_seconds)
        value["gate_points"] = list(self.gate_points)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "CampaignPolicy":
        data = dict(value or {})
        disk = DiskPolicy(**{
            item.name: data.get("disk", {}).get(item.name, getattr(DiskPolicy(), item.name))
            for item in fields(DiskPolicy)
        }) if isinstance(data.get("disk"), Mapping) else DiskPolicy()
        known = {item.name for item in fields(cls)} - {"disk"}
        unknown = sorted(set(data) - known - {"disk"})
        if unknown:
            raise ValueError(f"Unknown campaign policy keys: {', '.join(unknown)}.")
        kwargs = {key: data[key] for key in known if key in data}
        for key in ("retry_delays_seconds", "gate_points"):
            if key in kwargs:
                kwargs[key] = tuple(kwargs[key])
        policy = replace(cls(disk=disk), **kwargs)
        policy.validate()
        return policy

    def validate(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts is at least 1.")
        if len(self.retry_delays_seconds) < self.max_attempts - 1:
            raise ValueError("retry_delays_seconds names a delay for every retry.")
        if self.prefetch < 0:
            raise ValueError("prefetch is 0 or more.")
        if self.outage_units == 1 or self.outage_units < 0:
            raise ValueError("outage_units is 0 (no breaker) or at least 2: one unit's failures are its own.")
        unknown = set(self.gate_points) - {"before_production", "pre_cleanup", "final"}
        if unknown:
            raise ValueError(f"Unknown gate points: {', '.join(sorted(unknown))}.")
        if "pre_cleanup" not in self.gate_points:
            # The verdict recorded with every raw deletion is this one.
            raise ValueError("gate_points must include pre_cleanup.")
        anchors = self.automatic_rt_correction_maximum_anchors
        if isinstance(anchors, bool) or not isinstance(anchors, int) or anchors < AUTOMATIC_RT_MINIMUM_ANCHORS:
            raise ValueError(f"automatic_rt_correction_maximum_anchors is a whole number of at least {AUTOMATIC_RT_MINIMUM_ANCHORS}.")
        for name in ("automatic_rt_correction", "automatic_rt_correction_fallback"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} is true or false.")
        if "before_production" not in self.gate_points:
            # A unit runs only on a before-production report (the user's rule of 2026-10-02): without the
            # point, every unit would be held for want of one.
            raise ValueError("gate_points must include before_production.")


# ---- results -----------------------------------------------------------------------------------------

# How a tool result is read. Every Interactive tool the runner calls answers {"ok": false, "reason": ...}
# for a failure it caught (mcp_server._structured_validation_errors); the ports wrap anything that
# escapes that as reason "exception".
OK, FAILED, BUSY, FAULT, REFUSED, CONTRACT = "ok", "failed", "busy", "fault", "refused", "contract"
# Refusals that are no fault of the unit's: a tool or parameter this Interactive does not have, a reply of
# another shape, and (0.5.17) an extractor that is not a verified, pinned build. None is counted against the
# unit or deletes its raw data. "Stop" is per unit (2026-10-02), so the unit is held (contract_held) and the
# other units go on, and a deletion refused so leaves that unit's raw data held.
CONTRACT_REASONS = frozenset({"unsupported", "malformed", "raw_metadata_extractor_refused"})
# A backend that does not answer (2026-10-03): the exception a call to it ended on, as Interactive's wrapper
# reports one past its own mapping (reason os_error) or the port reports one that escaped it (exception). A
# timeout, a reset or a reply broken off is the backend's silence, never the unit's failure.
BACKEND_SILENT_ERRORS = frozenset({
    "TimeoutError", "timeout", "ConnectionResetError", "ConnectionAbortedError", "ConnectionRefusedError",
    "BrokenPipeError", "RemoteDisconnected", "IncompleteRead",
})
# A reply, or a record of Interactive's, that does not parse (2026-10-03: an unreadable reply holds the unit).
UNREADABLE_REPLY_ERRORS = frozenset({"JSONDecodeError", "UnicodeDecodeError"})
# The four pauses of the whole campaign: what every unit would meet alike. Each lifts by itself once its
# cause has gone (2026-10-02 for the first three, 2026-10-03 for the backend).
CAMPAIGN_PAUSES = {
    "pin": "a pin change",
    "disk": "a short disk",
    "outage": "a repository outage",
    "backend": "an Interactive backend that does not answer",
}
_PAUSE_LIFTS = {
    "pin": "by itself, once the pinned identities match the approved manifest again",
    "disk": "by itself, once no unit waits for space",
    "outage": "by itself, at the fault recheck (fault_recheck_seconds), when a download is tried again",
    "backend": "by itself, at the fault recheck (fault_recheck_seconds), once the backend answers",
    "fault": "by itself, at the fault recheck (fault_recheck_seconds), once the backend answers",
    "operator": "at an operator's resume",
    "contract": "at an operator's resume",
}
_PAUSE_NAMES = {
    **CAMPAIGN_PAUSES,
    "operator": "an operator's pause",
    "contract": "a contract pause a runner before 2026-10-02 left in the ledger",
    "fault": "a backend that did not answer or a repository outage, as a runner before 2026-10-03 recorded it",
}


def pause_name(kind: str | None) -> str | None:
    """What a pause kind is, in the words of the status export."""
    return None if kind is None else _PAUSE_NAMES.get(str(kind), str(kind))


def pause_lifts(kind: str | None) -> str | None:
    """How a pause of this kind lifts."""
    return None if kind is None else _PAUSE_LIFTS.get(str(kind), "at an operator's resume")


def classify_result(result: Any) -> str:
    """ok, failed (counts against the unit), busy (wait, counts nothing), fault (pause the campaign and look
    again later), contract (hold the unit, counting nothing, for a recheck) or refused (the campaign approval
    does not cover it)."""
    if not isinstance(result, Mapping):
        return FAILED
    if result.get("ok") is not False:
        return OK
    reason = str(result.get("reason") or "")
    error_type = str(result.get("error_type") or "")
    if reason == "campaign_authorization_refused":
        return REFUSED
    if reason in {"unit_busy", "manifest_busy"}:
        return BUSY
    if reason == "backend_unavailable" or (reason in {"os_error", "exception"} and error_type in BACKEND_SILENT_ERRORS):
        return FAULT
    if reason in CONTRACT_REASONS or (reason in {"validation_error", "exception"} and error_type in UNREADABLE_REPLY_ERRORS):
        return CONTRACT
    return FAILED


@dataclass(frozen=True)
class RetryDecision:
    state: str  # waiting_retry or failed
    failures: int
    next_attempt_at: str | None


def after_failure(failures_before: int, now: datetime, policy: CampaignPolicy) -> RetryDecision:
    """A unit's next state once another attempt has failed."""
    failures = failures_before + 1
    if failures >= policy.max_attempts:
        return RetryDecision("failed", failures, None)
    delay = policy.retry_delays_seconds[min(failures - 1, len(policy.retry_delays_seconds) - 1)]
    return RetryDecision("waiting_retry", failures, iso(now + timedelta(seconds=float(delay))))


def interruption_counts(interruptions_before: int, policy: CampaignPolicy) -> bool:
    """Whether an interruption (a stop the unit did not cause) now counts as a failure."""
    return interruptions_before + 1 > policy.max_interruptions


def _failure_texts(detail: Mapping[str, Any]) -> tuple[str, set[str]]:
    """The error text and exception type names a download failure's record carries, wherever they sit:
    the job's error, the manifest's download_failure, a tool's detail. Only the error the lease ended on:
    an earlier attempt that stalled and was retried says nothing about why the last one failed."""
    texts: list[str] = []
    types: set[str] = set()
    failure = detail.get("failure")
    for record in (detail, failure if isinstance(failure, Mapping) else {}):
        for key in ("error", "reason", "detail"):
            if record.get(key):
                texts.append(str(record[key]))
        if record.get("error_type"):
            types.add(str(record["error_type"]))
    return " | ".join(texts), types


# Interactive's lease refusing to go past maximum_gb (repository_reanalysis: the Content-Length check, the
# streaming check, and the lease's own check of the required bytes).
_LEASE_LIMIT = (
    re.compile(r"Remote object is (\d+) bytes; limit is \d+ bytes"),
    re.compile(r"Download exceeded the (\d+)-byte safety limit"),
    re.compile(r"Required repository bundle is (\d+) bytes; the (?:download lease|approved) limit is \d+ bytes"),
)


def lease_size_limit(detail: Mapping[str, Any]) -> int | None:
    """The bytes a download needed, when it ended because the lease reached maximum_gb, else None.

    The user set no per-unit size limit, so maximum_gb is only the disk bound and such an end is a short
    disk, never the unit's failure. The figure is the size Interactive names, or one byte past the limit.
    """
    text, _types = _failure_texts(detail)
    for index, pattern in enumerate(_LEASE_LIMIT):
        match = pattern.search(text)
        if match:
            return int(match.group(1)) + (1 if index == 1 else 0)
    return None


# Interactive's extraction guard (archives.ExtractionLimits, 20 GB held in reserve) ending a lease because the
# volume ran short while an archive expanded: ArchiveError insufficient_disk_space, recorded in the manifest's
# download_failure.archive_failure, and in these words in the job's error (the listing's check, the stream's
# check, and 7-Zip stopped by its watchdog).
_EXTRACTION_EXPANDS = re.compile(r"expands to (\d+) bytes; \d+ are free and \d+ are held in reserve")
_EXTRACTION_SHORT = re.compile(r"Free space fell below the \d+-byte reserve while|\binsufficient_disk_space\b")


def extraction_disk_short(detail: Mapping[str, Any]) -> int | None:
    """The bytes an archive was to expand to, when Interactive's extraction guard ended the lease because the
    volume ran short (0 when it did not say), else None.

    A short disk, never the unit's failure: the fetch had completed, so the free-space floor no longer
    watched the lease, and Interactive's own reserve is what stopped it.
    """
    failure = detail.get("failure")
    archive = failure.get("archive_failure") if isinstance(failure, Mapping) else None
    text, _types = _failure_texts(detail)
    named = isinstance(archive, Mapping) and archive.get("reason") == "insufficient_disk_space"
    if not named and not (_EXTRACTION_EXPANDS.search(text) or _EXTRACTION_SHORT.search(text)):
        return None
    declared = (archive.get("detail") or {}).get("declared_bytes") if named and isinstance(archive.get("detail"), Mapping) else None
    if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0:
        return declared
    match = _EXTRACTION_EXPANDS.search(text)
    return int(match.group(1)) if match else 0


_NETWORK_TYPES = frozenset({
    "URLError", "TimeoutError", "timeout", "ConnectionError", "ConnectionResetError", "ConnectionAbortedError",
    "ConnectionRefusedError", "RemoteDisconnected", "IncompleteRead", "gaierror", "SSLError", "SSLEOFError",
    # Interactive 0.5.18: a read that got no byte for its idle timeout, a lost connection or a short body,
    # each retried from its .part three times before the lease gives up (repository_reanalysis).
    "DownloadStalled", "DownloadConnectionLost", "DownloadIncomplete", "RetryableDownloadError",
})
_NETWORK_TEXT = re.compile(
    r"\bHTTP(?: Error)? (?:5\d\d|429)\b|urlopen error|timed out|Connection (?:reset|aborted|refused)|"
    r"forcibly closed|Remote end closed|IncompleteRead|getaddrinfo failed|Name or service not known|"
    r"Temporary failure in name resolution|EOF occurred in violation of protocol|"
    # Interactive 0.5.18's own words for DownloadStalled, DownloadConnectionLost and DownloadIncomplete: the
    # job's error is str(error), and only the manifest's download_failure carries the type.
    r"No bytes arrived from the server for|The connection was lost during the transfer|"
    r"Download ended at \d+ of \d+ declared bytes",
    re.IGNORECASE,
)


def network_failure(detail: Mapping[str, Any], outcome: str = "failed") -> bool:
    """Whether a download failed on the network rather than on the unit's own objects: a server error or
    a rate limit, a timeout, a reset, a name that did not resolve, or bytes that stopped arriving.

    `detail` is the job's record with the unit manifest's download_failure under "failure" where the lease
    wrote one, as the machine passes it: the type and retryable live only there."""
    if outcome == "stalled":
        return True
    failure = detail.get("failure")
    if isinstance(failure, Mapping) and failure.get("retryable") is True:
        return True
    text, types = _failure_texts(detail)
    return bool(types & _NETWORK_TYPES) or bool(_NETWORK_TEXT.search(text))


# ---- stalls -----------------------------------------------------------------------------------------

def download_stalled(
    last_progress_at: datetime | None, now: datetime, stall_seconds: float, fetch_completed: bool
) -> bool:
    """Whether a download's bytes have stopped arriving for longer than the stall window.

    Only while it is fetching. Once the lease records its fetch stage completed it is verifying,
    extracting or attributing, which moves no bytes and can take hours for a 928 GB study archive; a
    cancel then would also not be honoured (Interactive checks its cancel flag only while bytes arrive).
    """
    if fetch_completed or stall_seconds <= 0 or last_progress_at is None:
        return False
    return (now - last_progress_at).total_seconds() > stall_seconds


def fetch_completed(manifest: Mapping[str, Any] | None) -> bool:
    for entry in (manifest or {}).get("lease_stages") or []:
        if isinstance(entry, Mapping) and entry.get("stage") == "fetch" and entry.get("status") == "completed":
            return True
    return False


# ---- the diagnostic ---------------------------------------------------------------------------------

def is_fourier_transform_family(instrument_family: str) -> bool:
    """True for the labels Interactive gives Orbitrap and FT-ICR data ("Fourier-transform MS", "FT-ICR"), as
    Interactive 0.5.28's agent_workflow.is_fourier_transform_family reads them."""
    family = str(instrument_family or "").casefold()
    return "fourier" in family or "ft-icr" in family or "fticr" in family


def family_step(instrument_family: str) -> int:
    """The instrument family's step, as Interactive 0.5.28 gives it (agent_workflow.family_threshold_step,
    msdial-interactive-app#61): 1,000 for a Fourier-transform family, 100 for every other (QTOF-type, GC-MS,
    Unknown, none recorded). Interactive always searches this step first, whatever step it is asked for."""
    return 1000 if is_fourier_transform_family(instrument_family) else 100


def threshold_step(instrument: str, instrument_family: str = "", thermo_raw_inputs: int = 0) -> int:
    """1000 for Fourier-transform data, 100 otherwise, from the Catalog's instrument text as well as the family.

    Asked for only of an Interactive before 0.5.28, which searched the step it was asked for and labelled every
    mzML QTOF, so the Catalog's text had to name an Orbitrap published as mzML. Interactive 0.5.28 decides the
    step itself from the file (family_step), and a requested step is only recorded there, never searched: the
    runner asks it for none (machine.Runner._estimate), and compares this step with Interactive's only to
    leave a note.
    """
    if is_fourier_transform_family(instrument_family):
        return 1000
    if thermo_raw_inputs > 0:
        return 1000
    if _FOURIER_INSTRUMENT.search(str(instrument or "")):
        return 1000
    return 100


def estimate_step(estimate: Mapping[str, Any], family: int) -> dict[str, Any] | str:
    """The step an estimate used, read against the family step it should have searched first (the user's step
    rule of 2026-10-06): {"threshold_step", "coarse_threshold_step", "step_fallback", "fallback_reason"}, or
    what is wrong with it.

    family is the family step of the instrument family the estimate itself records (family_step) for
    Interactive 0.5.28, which records threshold_step (the step used), coarse_threshold_step (the family step it
    searched first), step_fallback and fallback_reason; for an older one, which records threshold_step alone,
    it is the step the runner asked for, with no fallback. The step used is the family step, or with a
    fallback a tenth of it, never finer.
    """
    def number(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return int(result) if result == int(result) else None

    used = number(estimate.get("threshold_step"))
    coarse = number(estimate.get("coarse_threshold_step", estimate.get("threshold_step")))
    fallback = estimate.get("step_fallback", False)
    if used is None or coarse is None:
        return "the estimate records no threshold step"
    if fallback not in (True, False):
        return f"the estimate's step_fallback is {fallback!r}, neither true nor false"
    if coarse != family:
        return f"the estimate searched first at step {coarse}, where its family step is {family}"
    expected = family // 10 if fallback else family
    if used != expected:
        return (f"the estimate used step {used}, where {'a fallback from' if fallback else 'no fallback from'} the "
                f"family step {family} gives {expected}")
    reason = estimate.get("fallback_reason")
    return {"threshold_step": used, "coarse_threshold_step": coarse, "step_fallback": bool(fallback),
            "fallback_reason": str(reason) if reason else None}


# ---- the disk --------------------------------------------------------------------------------------

def observed_factor(observations: Iterable[float], policy: DiskPolicy) -> float | None:
    values = sorted(float(value) for value in observations if value and value > 0)
    if len(values) < policy.observed_minimum_units:
        return None
    index = min(len(values) - 1, max(0, math.ceil(policy.observed_quantile * len(values)) - 1))
    return values[index]


def disk_factor(has_archive: bool, policy: DiskPolicy, observations: Iterable[float] = ()) -> float:
    factor = policy.archive_factor if has_archive else policy.file_factor
    measured = observed_factor(observations, policy)
    return factor if measured is None else max(factor, measured)


def known_bytes_for_need(need: int, has_archive: bool, policy: DiskPolicy, observations: Iterable[float] = ()) -> int:
    """The smallest size whose need (disk_need, the size known) is at least `need`."""
    return int(math.ceil(max(0, int(need)) / disk_factor(has_archive, policy, observations)))


def disk_need(
    known_bytes: int,
    size_known: bool,
    has_archive: bool,
    policy: DiskPolicy,
    observations: Iterable[float] = (),
    held_bytes: int = 0,
) -> int:
    """What the unit takes on the volume in all: its raw bytes times the factor, plus the reserve for a size
    not known. `held_bytes` is what it already holds there (a partial download kept for a resume): a size
    known only as a lower bound is at least that."""
    factor = disk_factor(has_archive, policy, observations)
    known = max(0, int(known_bytes or 0))
    if not size_known:
        known = max(known, int(held_bytes or 0))
    need = int(math.ceil(known * factor))
    if not size_known:
        need += int(policy.unknown_size_reserve_bytes)
    return need


def disk_reserve(total_bytes: int, policy: DiskPolicy) -> int:
    return max(int(policy.reserve_bytes), int(policy.reserve_fraction * max(0, int(total_bytes))))


@dataclass(frozen=True)
class DiskVerdict:
    admit: bool
    need: int
    free: int
    reserve: int
    never_fits: bool
    # What the unit already holds on the volume, which the free space already lacks.
    held: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def disk_verdict(need: int, free: int, total: int, policy: DiskPolicy, held: int = 0) -> DiskVerdict:
    """Whether a unit that takes `need` bytes in all, `held` of them already on the volume, fits now.

    Its own partial download is credited: the free space already lacks those bytes, and asking for the
    whole need on top of them deferred the retry of a unit that fitted, and every pending unit behind it,
    for good. Whether it could ever fit is its whole need against the volume."""
    reserve = disk_reserve(total, policy)
    held = max(0, int(held or 0))
    return DiskVerdict(
        admit=free - reserve >= max(0, need - held),
        need=need,
        free=free,
        reserve=reserve,
        # Physics, not a size policy: even an empty volume could not hold it.
        never_fits=need > max(0, total - reserve),
        held=held,
    )


def download_bound_gb(total: int, policy: DiskPolicy) -> float:
    """maximum_gb for a download: the disk bound, since the user set no per-unit size limit.

    Interactive requires maximum_gb above 0, refuses a unit whose required bytes exceed it, and stops a
    lease that streams past it. The bound is what the whole volume could hold above its reserve, so only
    a unit that could never fit meets it; a disk that is short now is the free-space floor's to pause
    (DiskPolicy). Never below 1 GB, so the call itself is well formed.
    """
    return max(1.0, (int(total) - disk_reserve(total, policy)) / 1000**3)


# ---- outputs and raw data --------------------------------------------------------------------------

# Why discard_download_lease would refuse, as InteractivePort.discard reads it before it records a crossing.
# The permanent ones do not change by waiting: a validated run takes the normal cleanup instead, and a run
# that left an mzTab-M it could not validate keeps its raw data until Interactive has a discard for it.
# console_live is the port's own: a Console that may still read the raw tree (one a backend restart left
# running) ends, or the runner stops it, so waiting mends it. disposition_held (Interactive 0.5.31): the unit's
# campaign disposition holds it, and Interactive never discards a held unit or a held split part unless the
# call passes release_disposition_hold, which only an operator's skip of the held unit does; waiting does not
# mend it, and the machine keeps such raw data (the hold's own decision) rather than holding them for a recheck.
DISPOSITION_HELD_BLOCKER = "disposition_held"
DISCARD_BLOCKERS = {
    "console_live": False,
    "validated_status": True,
    "lease_live": False,
    "mztab_output_exists": True,
    "raw_outside_workspace": True,
    "finalisation_held": False,
    DISPOSITION_HELD_BLOCKER: True,
}


def discard_blocked_for_good(blockers: Iterable[str]) -> bool:
    return any(DISCARD_BLOCKERS.get(str(code), False) for code in blockers)


def outputs_produced(manifest: Mapping[str, Any] | None) -> bool:
    """Every MS-DIAL output present and the mzTab-M validated, as Interactive's run job records it.

    The job refuses to finish when an expected export is missing (server._verify_expected_exports) and
    validates the mzTab-M before it finalises the lease, which is what sets cleanup_allowed.
    """
    manifest = manifest or {}
    return str(manifest.get("status") or "") in VALIDATED_STATUSES and manifest.get("cleanup_allowed") is True


def report_terms(outputs: bool, final_gate_exit: int | None) -> list[str]:
    terms = []
    if outputs:
        terms.append(OUTPUTS_PRODUCED)
    if outputs and final_gate_exit == 0:
        terms.append(COMPLETED)
    return terms


def gate_verdict_token(exit_code: int | None) -> str:
    """The gate's exit code as the lowercase token the Catalog's analysis_run row takes."""
    return {0: "pass", 2: "fail", 3: "unusable", 4: "held"}.get(exit_code, "not_run" if exit_code is None else "other")


# A gate check id, such as ELIG-1 or CONV-1.
_CHECK_ID = re.compile(r"[A-Z][A-Z0-9]*-\d+[A-Za-z]?")
# The gate's stage whose FAILs can stop a run (verify-run-invariants.py STAGES).
BEFORE_PRODUCTION_STAGE = "before-production"


def _later_stage(item: Mapping[str, Any]) -> bool:
    """Whether a check of a gate report is of a stage after production, with no run left to stop. The gate
    gives such a check run_policy null, and a --stage all report (pre_cleanup, final) holds many. A check
    that names no stage is read as a before-production one."""
    stage = item.get("stage")
    return isinstance(stage, str) and bool(stage) and stage != BEFORE_PRODUCTION_STAGE


def _run_policy_statements(report: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """What a gate report states each check's run_policy to be, and what of it could not be read.

    A check's own run_policy, or a top-level run_policy that maps a check id to its rule or a rule to the
    check ids it covers (one id or a list of them). A check's own statement wins over the top level's.

    A check of a later stage states nothing. The gate gives it null, and read as a rule this reader did
    not know, null made every later-stage FAIL of a --stage all report one that stops the run, and every
    later-stage check a mismatch. A before-production check given null is one the gate's table left out:
    the user's list decides it, and the gap is said. The gate's run_blocked_by must be a list of ids."""
    stated: dict[str, Any] = {}
    problems: list[str] = []
    declared = report.get("run_policy")
    if isinstance(declared, Mapping):
        for key, value in declared.items():
            if key in (BLOCKS_RUN, RECORD_ONLY):
                names = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else None
                if names is None or not all(isinstance(name, str) for name in names):
                    problems.append(f"run_policy.{key} is {value!r}, neither a check id nor a list of them")
                    continue
                stated.update({name: key for name in names})
            elif isinstance(key, str) and _CHECK_ID.fullmatch(key):
                stated[key] = value
            else:
                problems.append(f"run_policy names {key!r}, neither a rule nor a check id")
    elif declared is not None:
        problems.append(f"run_policy is {type(declared).__name__}, not an object")
    for item in report.get("checks") or []:
        if not isinstance(item, Mapping) or "run_policy" not in item or _later_stage(item):
            continue
        check = str(item.get("check_id") or "")
        if item["run_policy"] is None:
            problems.append(f"{check}: the gate states no run_policy for this before-production check; the user's list decides")
            continue
        stated[check] = item["run_policy"]
    if _blocked_by(report) is None:
        problems.append(f"run_blocked_by is {report.get('run_blocked_by')!r}, not a list of check ids")
    return stated, problems


def _blocked_by(report: Mapping[str, Any]) -> list[str] | None:
    """The gate's own run_blocked_by (one check id or a list of them), [] when it states none, None when it
    is of another shape."""
    named = report.get("run_blocked_by")
    if named is None:
        return []
    names = [named] if isinstance(named, str) else named if isinstance(named, list) else None
    if names is None or not all(isinstance(name, str) for name in names):
        return None
    return [name for name in names if name]


def _blocking_rule(check: str, stated: Mapping[str, Any]) -> bool:
    """Whether a before-production check is one whose FAIL stops the run: one of the user's, or one the
    gate states a rule for that is not record_only (a rule in a word this reader does not know included)."""
    return check in BLOCKS_RUN_CHECKS or (check in stated and stated.get(check) != RECORD_ONLY)


def _before_production(report: Mapping[str, Any], status: str) -> list[Mapping[str, Any]]:
    """The report's before-production checks of this status, compared without case."""
    return [item for item in report.get("checks") or []
            if isinstance(item, Mapping) and not _later_stage(item)
            and str(item.get("status") or "").casefold() == status]


def run_blocking_failures(report: Mapping[str, Any]) -> tuple[list[str], str]:
    """The FAILed checks of a gate --json report that stop a unit's run, and where that rule came from.

    The user's rule always holds: a FAIL of a check in BLOCKS_RUN_CHECKS blocks, whatever the gate says.
    The gate's run_policy can add to it - a check it states blocks_run, or whose rule it states in a word
    this reader does not know, blocks too - and never take from it: a record_only it states for one of the
    user's checks is a mismatch (run_policy_mismatches), and the check blocks. Reading it the other way
    round failed open: once any check stated a policy, the fixed list was dropped for every check, and a
    misspelt rule blocked nothing. The gate's own run_blocked_by adds to it as well, less the checks it
    names that the report shows not evaluable, which run_blocking_unevaluated returns. The source is "gate"
    when the report stated a run_policy, read with the fixed list, and "runner_default" when it stated none.
    Statuses are the gate's own lowercase words ("fail"), compared without case. Only a FAIL of a
    before-production check is returned here: a WARN is recorded, and the unit runs, and a check of a
    later stage has no run left to stop.
    """
    stated, problems = _run_policy_statements(report)
    blocking = {str(item.get("check_id") or "") for item in _before_production(report, "fail")}
    blocking = {check for check in blocking if _blocking_rule(check, stated)}
    unevaluated = {str(item.get("check_id") or "") for item in _before_production(report, "not_evaluable")}
    blocking.update(name for name in _blocked_by(report) or [] if name not in unevaluated)
    stating = stated or problems or report.get("run_blocked_by") is not None
    return sorted(blocking), ("gate" if stating else "runner_default")


def run_blocking_unevaluated(report: Mapping[str, Any]) -> list[str]:
    """The checks of a gate --json report that stop a unit's run although they did not FAIL: before-production
    checks whose FAIL would stop it (_blocking_rule), left not evaluable where they are required.

    The user's decision of 2026-10-02: such a check stops the run exactly as its FAIL does, for without what
    the stage owed it the check that would show the results broken has not been made. A required one is what
    --strict counts and the report lists in strict_failures; the check's own "required" says so, and where a
    check does not state it, strict_failures decides. A check not evaluable where it is not required never
    stops the run (INP-1 for a unit that declares no analysis inputs is normal), nor does a record_only one.
    A check the gate's run_blocked_by names and the report shows not evaluable is returned too."""
    stated, _problems = _run_policy_statements(report)
    strict = report.get("strict_failures")
    strict = {name for name in strict if isinstance(name, str)} if isinstance(strict, list) else set()
    found: set[str] = set()
    unevaluated: set[str] = set()
    for item in _before_production(report, "not_evaluable"):
        check = str(item.get("check_id") or "")
        unevaluated.add(check)
        required = item.get("required")
        owed = required is True or (not isinstance(required, bool) and check in strict)
        if owed and _blocking_rule(check, stated):
            found.add(check)
    found.update(name for name in _blocked_by(report) or [] if name in unevaluated)
    return sorted(found)


# Why a gate verdict carries no report the runner can read (gate_report_problem).
GATE_NOT_RUN = "not_run"            # the runner could not start it: no workspace, or an exception
GATE_TIMEOUT = "timeout"            # it gave no answer within gate_timeout_seconds
GATE_ERROR = "error"                # it could not be started, or crashed: an exit code outside {0, 2, 3, 4}
GATE_UNUSABLE = "unusable_workspace"  # exit 3: the workspace is unusable, and the gate wrote no report
GATE_UNPARSABLE = "unparsable"      # its output is not a gate report


def gate_report_problem(verdict: Mapping[str, Any] | None) -> str | None:
    """Why a gate verdict gives no usable report, or None where it gives one.

    THE USER'S RULE (2026-10-02). Before production, a gate that gives no usable report holds the unit: it
    does not run, its raw data are kept, nothing is counted against it, and the other units go on. Without a
    report the runner cannot tell whether a check that stops the run FAILed, so it cannot apply the rule; nor
    is the gate's silence the unit's failure, so the unit is not retried towards the deletion of its raw
    data. After the run a missing report is only recorded. A usable report is one that ran, exited 0, 2 or
    4, and parsed as a gate report (GatePort sets report_parsed); anything else is one of the reasons
    above. None, the verdict of a gate that was never started, is GATE_NOT_RUN."""
    if verdict is None:
        return GATE_NOT_RUN
    outcome = verdict.get("outcome")
    if outcome == "timeout":
        return GATE_TIMEOUT
    if outcome != "ran":
        return GATE_NOT_RUN if verdict.get("not_started") else GATE_ERROR
    exit_code = verdict.get("exit_code")
    if exit_code == 3:
        return GATE_UNUSABLE
    if isinstance(exit_code, bool) or exit_code not in (0, 2, 4):
        return GATE_ERROR
    if verdict.get("report_parsed") is not True:
        return GATE_UNPARSABLE
    return None


def run_policy_mismatches(report: Mapping[str, Any]) -> list[str]:
    """Where a gate report's run_policy disagrees with the user's rule or cannot be read. Each is read the
    way that blocks (run_blocking_failures); the runner records them, so the contract can be mended."""
    stated, problems = _run_policy_statements(report)
    mismatches = list(problems)
    for check, rule in sorted(stated.items()):
        if not isinstance(rule, str) or rule not in (BLOCKS_RUN, RECORD_ONLY):
            mismatches.append(f"{check}: run_policy {rule!r} is neither {BLOCKS_RUN!r} nor {RECORD_ONLY!r}; read as {BLOCKS_RUN}")
        elif rule == RECORD_ONLY and check in BLOCKS_RUN_CHECKS:
            mismatches.append(f"{check}: the gate states {RECORD_ONLY}, and the user's rule stops the run on its FAIL")
    return mismatches


# ---- pins ------------------------------------------------------------------------------------------

# Which automatic alignment RT correction the pinned Console implements (ports.PinReader.console records it
# as automatic_rt_correction). The campaign runs MsdialWorkbench #826's local outlier test (decided
# 2026-10-07): a Console of #810 alone runs the run-wide test instead, silently, since Interactive does not
# write the key #826 added; a Console of neither is refused by Interactive at every unit's run start, after
# its download.
AUTOMATIC_RT_LOCAL_SUPPORT = "local_support"
AUTOMATIC_RT_RUN_WIDE = "run_wide"
AUTOMATIC_RT_NONE = "none"
# The Console's least anchor count (Interactive's automatic_rt_correction_minimum_anchors default), below which
# CampaignPolicy refuses a maximum; and the local window the campaign runs with, #826's default, which the runner
# does not send (Interactive writes the window only when the answers set it) and a profile may state only as is.
AUTOMATIC_RT_MINIMUM_ANCHORS = 3
AUTOMATIC_RT_LOCAL_SUPPORT_RT_WINDOW = 1.5
# The line the Console writes when it could not select anchors (LcmsProcess: "Automatic alignment RT correction
# failed: <reason>", then exit -1); Interactive keeps the Console's output in the job's log.
AUTOMATIC_RT_FAILED_LINE = "Automatic alignment RT correction failed"


def automatic_rt_correction_pinned(recorded: Mapping[str, Any] | None) -> bool:
    """Whether a campaign's recorded policy pins automatic RT correction on: only a policy that names the field.
    One recorded before 2026-10-07 does not, and its units run with what their profile says, as they did."""
    return isinstance(recorded, Mapping) and recorded.get("automatic_rt_correction") is True


# What identifies each pinned thing. Paths are recorded but not compared: the Console and the extractor
# may be reached by another spelling, and a library is named by file name only.
PIN_IDENTITY = {
    "console": ("binary_sha256", "assembly_sha256", "inventory_sha256"),
    # provenance_status and pinned: the build record still verifies, and its commits are still one of
    # Interactive's PINNED_BUILDS (0.5.17). Interactive refuses a campaign preflight otherwise.
    "extractor": ("binary_sha256", "inventory_sha256", "provenance_status", "pinned"),
    "interactive": ("version", "commit", "dirty"),
    "catalog": ("version", "commit", "dirty"),
    "gate": ("commit", "dirty"),
}
CODE_PINS = ("interactive", "catalog", "gate")


def pin_problems(pins: Mapping[str, Any]) -> list[str]:
    """Why these pins cannot be approved. A checkout with uncommitted changes, or whose state could not be
    read, is not identified by its commit: the code that ran could change under the same pin, unseen."""
    problems = []
    for name in CODE_PINS:
        pin = pins.get(name)
        if not isinstance(pin, Mapping):
            continue
        if pin.get("dirty") is True:
            problems.append(f"the {name} checkout has uncommitted changes; commit them and plan again")
        elif pin.get("dirty") is None or not str(pin.get("commit") or "").strip():
            problems.append(f"the {name} checkout's state could not be read ({pin.get('error') or 'no commit'})")
    return problems


def pin_differences(recorded: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Every pinned identity that differs, as '<pin>.<field>'. A field the plan recorded as absent is not
    compared; one it recorded and that is now missing is a difference."""
    changes: list[str] = []
    for name, keys in PIN_IDENTITY.items():
        before = recorded.get(name) or {}
        after = current.get(name) or {}
        for key in keys:
            if key not in before or before.get(key) in (None, ""):
                continue
            if after.get(key) != before.get(key):
                changes.append(f"{name}.{key}")
    libraries_before = {item["name"]: item for item in recorded.get("libraries") or []}
    libraries_after = {item["name"]: item for item in current.get("libraries") or []}
    for name, item in sorted(libraries_before.items()):
        other = libraries_after.get(name)
        if other is None:
            changes.append(f"libraries.{name}.missing")
        elif other.get("sha256") != item.get("sha256"):
            changes.append(f"libraries.{name}.sha256")
    return changes


# ---- privacy ---------------------------------------------------------------------------------------

def redactor(locations: Mapping[str, str]):
    """A function that replaces every private location in a JSON-shaped value with <library:NAME>.

    `locations` maps a library's name to its local path. Its directory is withheld too, in backslash and
    forward-slash spellings, case-insensitively, because the ledger and the logs are meant to be read by
    the later verification work and a private library's location must never reach them.
    """
    needles: list[tuple[re.Pattern[str], str]] = []
    for name, location in sorted(locations.items(), key=lambda item: -len(str(item[1]))):
        text = str(location or "").strip()
        if not text:
            continue
        forms = {text, text.replace("\\", "/"), text.replace("/", "\\"), text.replace("\\", "\\\\")}
        for form in sorted(forms, key=len, reverse=True):
            needles.append((re.compile(re.escape(form), re.IGNORECASE), f"<library:{name}>"))
        parent = re.split(r"[\\/]", text)
        if len(parent) > 1:
            directory = text[: len(text) - len(parent[-1])].rstrip("\\/")
            if directory and len(directory) > 3:
                for form in {directory, directory.replace("\\", "/"), directory.replace("\\", "\\\\")}:
                    needles.append((re.compile(re.escape(form), re.IGNORECASE), "<library-directory>"))

    def apply(value: Any) -> Any:
        if isinstance(value, str):
            for pattern, label in needles:
                value = pattern.sub(label, value)
            return value
        if isinstance(value, Mapping):
            return {apply(str(key)): apply(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [apply(item) for item in value]
        return value

    return apply


# ---- time ------------------------------------------------------------------------------------------

def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        value = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def canonical_json(value: Any) -> bytes:
    """The one serialisation a digest is taken over: sorted keys, no spaces, UTF-8."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
