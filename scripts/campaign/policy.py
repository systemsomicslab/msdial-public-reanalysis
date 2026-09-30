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
# Fourier-transform analysers, for the diagnostic's threshold step (1000 rather than 100). Read from the
# catalog's instrument text, which is the submitter's own words: "Thermo Scientific Exactive" and "Exactive
# Plus" name no Q, "IQ-X tribrid" no Orbitrap, and "Bruker APEX-Qe 9.4T" is an FT-ICR.
_FOURIER_INSTRUMENT = re.compile(
    r"orbitrap|exactive|exploris|fusion|lumos|eclipse|astral|tribrid|ltq[\s-]?ft|ft[\s-]?icr|fticr|"
    r"solarix|apex|fourier",
    re.IGNORECASE,
)
# The before-production checks whose FAIL stops a unit's MS-DIAL run (the user's rule of 2026-10-01): the
# ones that break results. A FAIL of any other check (CLS-1/2/3, ORD-1, PKH-1 and the rest) is recorded and
# the unit runs. The gate says so itself in each check's run_policy once it carries one; this list is what
# the runner reads when it does not.
BLOCKS_RUN = "blocks_run"
RECORD_ONLY = "record_only"
BLOCKS_RUN_CHECKS = ("ELIG-1", "ACQ-1", "SUM-1", "CNT-1", "INP-1")


class DispositionError(ValueError):
    """A campaign_disposition that does not have the shared contract's shape.

    Interactive writes the record and this runner reads it, in two repositories merged independently. A
    record the runner cannot read is a contract mismatch between the two, which is a campaign fault: it
    would recur for every unit, so the campaign pauses instead of failing each unit and deleting its raw
    data.
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
    # Downloads of this many different units failing in a row on the network (a 5xx, a timeout, a reset,
    # a stall) are a repository outage, not unit failures: the campaign pauses as a fault, and from the
    # attempt that trips it on nothing counts against a unit. 0 turns the breaker off.
    outage_units: int = 3
    prefetch: int = 0
    poll_seconds: float = 30.0
    busy_retry_seconds: float = 120.0
    # A campaign fault (a backend that will not answer, a contract Interactive broke) is looked at again
    # after this long, and the step retried; it pauses again if the fault is still there.
    fault_recheck_seconds: float = 3600.0
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


# ---- results -----------------------------------------------------------------------------------------

# How a tool result is read. Every Interactive tool the runner calls answers {"ok": false, "reason": ...}
# for a failure it caught (mcp_server._structured_validation_errors); the ports wrap anything that
# escapes that as reason "exception".
OK, FAILED, BUSY, FAULT, REFUSED, CONTRACT = "ok", "failed", "busy", "fault", "refused", "contract"
# Refusals that are the campaign's, not the unit's, and that time does not mend: a tool or parameter this
# Interactive does not have, a reply of another shape, and (0.5.17) an extractor that is not a verified,
# pinned build. Each would recur for every unit, so the campaign pauses for an operator instead of failing
# units one by one and deleting their raw data.
CONTRACT_REASONS = frozenset({"unsupported", "malformed", "raw_metadata_extractor_refused"})


def classify_result(result: Any) -> str:
    """ok, failed (counts against the unit), busy (wait, counts nothing), fault (pause the campaign and look
    again later), contract (pause the campaign for an operator) or refused (the campaign approval does not
    cover it)."""
    if not isinstance(result, Mapping):
        return FAILED
    if result.get("ok") is not False:
        return OK
    reason = str(result.get("reason") or "")
    if reason == "campaign_authorization_refused":
        return REFUSED
    if reason in {"unit_busy", "manifest_busy"}:
        return BUSY
    if reason == "backend_unavailable":
        return FAULT
    if reason in CONTRACT_REASONS:
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
    r"Temporary failure in name resolution|EOF occurred in violation of protocol",
    re.IGNORECASE,
)


def network_failure(detail: Mapping[str, Any], outcome: str = "failed") -> bool:
    """Whether a download failed on the network rather than on the unit's own objects: a server error or
    a rate limit, a timeout, a reset, a name that did not resolve, or bytes that stopped arriving."""
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

def threshold_step(instrument: str, instrument_family: str = "", thermo_raw_inputs: int = 0) -> int:
    """1000 for Fourier-transform data, 100 otherwise (the project contract's diagnostic rule).

    Interactive labels every mzML QTOF (workflow.py), so the catalog's instrument text is read too: an
    Orbitrap unit published as mzML is Fourier-transform data.
    """
    family = str(instrument_family or "").casefold()
    if "fourier" in family or "ft-icr" in family or "fticr" in family:
        return 1000
    if thermo_raw_inputs > 0:
        return 1000
    if _FOURIER_INSTRUMENT.search(str(instrument or "")):
        return 1000
    return 100


# ---- the disk --------------------------------------------------------------------------------------

def observed_factor(observations: Iterable[float], policy: DiskPolicy) -> float | None:
    values = sorted(float(value) for value in observations if value and value > 0)
    if len(values) < policy.observed_minimum_units:
        return None
    index = min(len(values) - 1, max(0, math.ceil(policy.observed_quantile * len(values)) - 1))
    return values[index]


def disk_need(
    known_bytes: int,
    size_known: bool,
    has_archive: bool,
    policy: DiskPolicy,
    observations: Iterable[float] = (),
) -> int:
    factor = policy.archive_factor if has_archive else policy.file_factor
    measured = observed_factor(observations, policy)
    if measured is not None:
        factor = max(factor, measured)
    need = int(math.ceil(max(0, int(known_bytes or 0)) * factor))
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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def disk_verdict(need: int, free: int, total: int, policy: DiskPolicy) -> DiskVerdict:
    reserve = disk_reserve(total, policy)
    return DiskVerdict(
        admit=free - reserve >= need,
        need=need,
        free=free,
        reserve=reserve,
        # Physics, not a size policy: even an empty volume could not hold it.
        never_fits=need > max(0, total - reserve),
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
DISCARD_BLOCKERS = {
    "validated_status": True,
    "lease_live": False,
    "mztab_output_exists": True,
    "raw_outside_workspace": True,
    "finalisation_held": False,
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


def run_blocking_failures(report: Mapping[str, Any]) -> tuple[list[str], str]:
    """The FAILed checks of a gate --json report that stop a unit's run, and where that rule came from.

    Each check's run_policy ("blocks_run" or "record_only"), when the gate states one, or a top-level
    run_policy naming them; otherwise BLOCKS_RUN_CHECKS ("runner_default"). Statuses are the gate's own
    lowercase words ("fail"), compared without case. Only a FAIL blocks: a check left not evaluable or a
    WARN is recorded, and the unit runs.
    """
    checks = [item for item in report.get("checks") or [] if isinstance(item, Mapping)]
    declared = report.get("run_policy")
    top: dict[str, str] = {}
    if isinstance(declared, Mapping):
        for key, value in declared.items():
            if key in (BLOCKS_RUN, RECORD_ONLY) and isinstance(value, (list, tuple)):
                top.update({str(check): key for check in value})
            elif isinstance(value, str):
                top[str(key)] = value
    stated = any("run_policy" in item for item in checks) or bool(top)
    blocking: set[str] = set()
    for item in checks:
        if str(item.get("status") or "").casefold() != "fail":
            continue
        check = str(item.get("check_id") or "")
        rule = item.get("run_policy") if "run_policy" in item else top.get(check)
        if (rule == BLOCKS_RUN) if stated else (check in BLOCKS_RUN_CHECKS):
            blocking.add(check)
    return sorted(blocking), ("gate" if stated else "runner_default")


# ---- pins ------------------------------------------------------------------------------------------

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
