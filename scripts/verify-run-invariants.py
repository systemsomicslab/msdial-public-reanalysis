"""Verify the invariants an unattended repository reanalysis must not violate.

An unattended loop cannot rely on a person noticing that something went wrong. Every stage of the
pipeline reports success on its own terms, and each of the failures this script looks for produced a
complete, self-consistent, publishable result set that was simply not the study anybody approved.

The checks are cross-artifact on purpose. Each fact worth trusting is recorded more than once by
different code paths -- the repository manifest, the analysis CSV, the MS-DIAL run manifest, the
exported files, the QA matrix, the mzTab-M -- so an invariant that reads two of them at once cannot
be satisfied by one component lying consistently to itself.

Three rules run through the whole file.

A check that cannot be evaluated reports NOT_EVALUABLE with the reason. It never reports PASS. The
difference matters more than the verdict: "the artifact is absent" and "the artifact is correct" are
the two states an unattended loop must never confuse.

A check reports FAIL only for a fact it established. Where a defect is known but its consequence is
not visible in the artifacts, the check says so rather than inferring.

Nothing here writes, moves or deletes anything. It is safe to run at any point in a run.

Usage:
    python scripts/verify-run-invariants.py <unit-workspace> [--stage STAGE] [--json]

    <unit-workspace>  the directory holding provenance/ and output/ for ONE analysis unit
    --stage           before-production | after-run | before-publish | all   (default: all)
    --json            emit the full report as JSON on stdout

Exit codes: 0 all evaluated checks passed, 2 at least one failed, 3 the workspace is unusable,
4 (--strict only) a check could not be evaluated because an artifact the stage owed is absent.

--strict exists because `ok` means "no check FAILed", and a workspace where nothing has happened
produces no FAILs at all. Run against a directory holding an empty provenance/ and an empty
output/, this file reported 15 not_evaluable, ok=True and exit 0, while the batch skill tells an
agent that exit 0 "means every evaluated check passed". A unit nobody ran and a unit that ran
correctly gave the same answer. Every unattended run must pass --strict.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

PASS = "pass"
FAIL = "fail"
WARN = "warn"
NOT_EVALUABLE = "not_evaluable"

STAGES = ("before-production", "after-run", "before-publish")

# A user-profile path inside an artifact built to be shared. Deliberately narrow: it
# matches what this machine actually leaks (a Windows profile path) rather than trying to
# recognise private data in general, which no pattern can do.
PRIVATE_PATH_PATTERN = re.compile(
    r"[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}[^\\/\s\"\',;]+"
    r"|file:/{2,3}[A-Za-z]:/+Users/+[^\s\"\',;]+",
    re.IGNORECASE,
)


@dataclass
class Check:
    check_id: str
    stage: str
    title: str
    status: str
    detail: str
    evidence: dict = field(default_factory=dict)
    # Whether this stage was responsible for producing what the check reads. False marks the
    # artifacts whose absence is a legitimate state of a correct run -- a unit whose acquisition
    # mode was known without a header preflight, a unit whose raw tree has already been released.
    # Everything else that cannot be evaluated is an artifact the stage should have produced, and
    # under --strict its absence refuses the unit instead of being counted as "nothing to see".
    required: bool = True

    @property
    def strict_failure(self) -> bool:
        return self.status == NOT_EVALUABLE and self.required

    def as_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "stage": self.stage,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "required": self.required,
            "evidence": self.evidence,
        }


class Report:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.checks: list[Check] = []

    # Positional-only, so an evidence key may be named "status" or "detail" without colliding with
    # the parameters. Evidence keys come from the artifacts and are not ours to rename.
    def add(self, check_id: str, stage: str, title: str, status: str, detail: str, /,
            required: bool = True, **evidence) -> Check:
        check = Check(check_id, stage, title, status, detail, evidence, required)
        self.checks.append(check)
        return check

    def counts(self) -> dict[str, int]:
        return dict(Counter(check.status for check in self.checks))

    @property
    def ok(self) -> bool:
        return not any(check.status == FAIL for check in self.checks)

    @property
    def strict_failures(self) -> list[Check]:
        """Checks that could not be evaluated on an artifact this stage owed.

        WHY THIS EXISTS. `ok` is "no check FAILed", and a workspace where nothing has happened
        produces no FAILs at all: run against an empty directory holding an empty provenance/ and
        an empty output/, this file returned 15 not_evaluable, ok=True and exit 0, while the batch
        skill tells an agent that exit 0 "means every evaluated check passed". A unit nobody ran
        and a unit that ran correctly were the same answer, which is the difference an unattended
        loop exists to notice.
        """
        return [check for check in self.checks if check.strict_failure]

    def as_dict(self) -> dict:
        return {
            "workspace": str(self.workspace),
            "ok": self.ok,
            "counts": self.counts(),
            "strict_failures": [check.check_id for check in self.strict_failures],
            "progress": getattr(self, "progress", None),
            "checks_by_stage": _checks_by_stage(self),
            "checks": [check.as_dict() for check in self.checks],
        }


def _read_json(path: Path) -> tuple[dict | None, str]:
    """Return the parsed object, or None with the reason it could not be read."""
    if not path.exists():
        return None, f"{path.name} is absent"
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except (OSError, ValueError) as exc:
        return None, f"{path.name} could not be read: {exc}"


def _read_csv_rows(path: Path) -> tuple[list[dict] | None, str]:
    if not path.exists():
        return None, f"{path.name} is absent"
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle)), ""
    except (OSError, ValueError) as exc:
        return None, f"{path.name} could not be read: {exc}"


# --------------------------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------------------------

def check_unit_identity(report: Report, provenance: dict | None, reason: str) -> None:
    """The workspace directory must be the analysis unit the manifest names.

    Two generations of workspace layout exist side by side on disk: an accession-scoped one that
    predates analysis-unit isolation and carries no analysis_unit_id, and a unit-scoped one that
    does. Both declare the same manifest schema, so the schema string cannot separate them. A loop
    that resolves results by accession will find whichever it meets first, and the sample names
    inside are identical, so the substitution is invisible.
    """
    stage = "before-production"
    if provenance is None:
        report.add("ID-1", stage, "Workspace is the analysis unit its manifest names",
                   NOT_EVALUABLE, reason)
        return
    project = provenance.get("project") or {}
    unit_id = str(project.get("analysis_unit_id") or "")
    directory = report.workspace.name
    if not unit_id:
        report.add(
            "ID-1", stage, "Workspace is the analysis unit its manifest names", FAIL,
            "The manifest declares no analysis_unit_id, so these artifacts belong to an accession "
            "rather than to one analysis unit. A unit-scoped result cannot be derived from them, "
            "and a technical signature cannot be attributed to them.",
            directory=directory, accession=project.get("accession"),
            manifest_schema=provenance.get("schema"),
        )
        return
    if unit_id != directory:
        report.add(
            "ID-1", stage, "Workspace is the analysis unit its manifest names", FAIL,
            "The directory name and the manifest's analysis_unit_id disagree, so it is not "
            "established which unit these artifacts describe.",
            directory=directory, analysis_unit_id=unit_id,
        )
        return
    report.add("ID-1", stage, "Workspace is the analysis unit its manifest names", PASS,
               f"analysis_unit_id {unit_id} matches the workspace directory.",
               analysis_unit_id=unit_id, accession=project.get("accession"))


# --------------------------------------------------------------------------------------------
# eligibility
# --------------------------------------------------------------------------------------------

def check_execution_allowed(report: Report, provenance: dict | None, reason: str) -> None:
    """The manifest's own eligibility verdict must permit execution.

    The server writes execution_allowed and then consults it nowhere, so the verdict currently
    gates nothing on its own. Reading it here is what turns it back into a gate.
    """
    stage = "before-production"
    if provenance is None:
        report.add("ELIG-1", stage, "Manifest permits execution", NOT_EVALUABLE, reason)
        return
    allowed = provenance.get("execution_allowed")
    if allowed is True:
        report.add("ELIG-1", stage, "Manifest permits execution", PASS,
                   "execution_allowed is true.", status=provenance.get("status"))
        return
    if provenance.get("status") == "split_by_acquisition":
        parts = [str(item.get("analysis_unit_id") or "") for item in provenance.get("split_into") or []
                 if isinstance(item, dict)]
        report.add(
            "ELIG-1", stage, "Manifest permits execution", FAIL,
            "This unit was split by acquisition mode and is the raw owner of its parts, not a run. "
            f"Gate the parts instead: {', '.join(parts) or 'none recorded'}.",
            execution_allowed=allowed, status=provenance.get("status"), parts=parts,
        )
        return
    report.add(
        "ELIG-1", stage, "Manifest permits execution", FAIL,
        "execution_allowed is not true. The unit was judged ineligible or unresolved, and no stage "
        "of the pipeline reads this field, so nothing else will stop the run.",
        execution_allowed=allowed, status=provenance.get("status"),
    )


def check_preflight_claim(report: Report, provenance: dict | None, reason: str) -> None:
    """Whether a header-confirmed acquisition claim may be made at all.

    This is not a pass/fail on the run: a unit whose eligibility rests on repository metadata is a
    correct degradation. What it forbids is asserting anywhere downstream that polarity or
    acquisition mode were confirmed from the raw headers, when the reader that would confirm them
    never ran.
    """
    stage = "before-production"
    if provenance is None:
        report.add("PRE-1", stage, "Header-confirmed acquisition claim is permitted",
                   NOT_EVALUABLE, reason)
        return
    preflight = provenance.get("raw_metadata_preflight")
    if not isinstance(preflight, dict) or not preflight:
        # Not required: a unit whose acquisition mode was already unambiguous in the repository
        # metadata never needed a header read, and refusing it under --strict would refuse a
        # correct run for not doing something it did not have to do.
        report.add("PRE-1", stage, "Header-confirmed acquisition claim is permitted", NOT_EVALUABLE,
                   "No raw-header preflight is recorded in the manifest.", required=False)
        return
    summary = preflight.get("summary")
    exit_code = preflight.get("exit_code")
    if summary and exit_code == 0:
        report.add("PRE-1", stage, "Header-confirmed acquisition claim is permitted", PASS,
                   "Raw headers were read; polarity and acquisition mode may be reported as "
                   "header-confirmed.", exit_code=exit_code)
        return
    report.add(
        "PRE-1", stage, "Header-confirmed acquisition claim is permitted", WARN,
        "Raw headers were not read, so eligibility rests on repository metadata alone. That is a "
        "valid degradation, but no artifact may describe polarity or acquisition mode as confirmed "
        "from the data, and a unit that reached here through allow_preflight has had its technical "
        "blockers removed without a header ever being read.",
        exit_code=exit_code, has_summary=bool(summary),
    )


def _raw_owner_manifest(provenance: dict | None) -> tuple[dict | None, str]:
    """The manifest of the unit that downloaded this unit's raw data.

    That is the unit itself, except for a part split from another unit: a part downloaded nothing,
    and its inputs were verified, or not, when its parent was downloaded.
    """
    if not isinstance(provenance, dict):
        return None, ""
    split_from = provenance.get("split_from")
    if isinstance(split_from, dict) and split_from.get("manifest_path"):
        return _read_json(Path(str(split_from["manifest_path"])))
    return provenance, ""


def check_checksum_coverage(report: Report, provenance: dict | None, reason: str) -> None:
    """SUM-1. Every admitted input had its declared checksum verified.

    The record's own "required" is true when at least one file carried a checksum, so
    {"required": true, "verified": 1, "skipped": 29} reads to a boolean scan exactly like full
    coverage. It is reported here as any_checksum_verified, which is what it means.

    A REPOSITORY THAT DECLARES NO CHECKSUM AT ALL is a WARN, not a FAIL. MetaboLights publishes
    none: all eleven MTBLS2207 files came with an empty declared checksum. As a FAIL this refused
    every MetaboLights unit for a property of the repository rather than of the run. The user
    decided on 2026-09-25 that such a unit proceeds on the sha256 recorded at download, with the
    warning saying so, and that no artifact may then call its inputs checksum-verified. A partial
    declaration is still a FAIL: some files could be compared and the rest were not.

    A split part carries its raw owner's verdict. Read on the part alone, the record is absent
    and a FAIL on the parent would become a mere absence on the part.
    """
    stage = "before-production"
    title = "Every input's checksum was verified"
    if provenance is None:
        report.add("SUM-1", stage, title, NOT_EVALUABLE, reason)
        return
    owner, owner_reason = _raw_owner_manifest(provenance)
    inherited_from = ""
    if owner is not provenance:
        if owner is None:
            report.add("SUM-1", stage, title, NOT_EVALUABLE,
                       "This unit was split from another, whose manifest could not be read: "
                       f"{owner_reason}")
            return
        inherited_from = str((provenance.get("split_from") or {}).get("analysis_unit_id") or "")
    validation = owner.get("allowlist_checksum_validation")
    candidates = owner.get("input_candidates")
    if not isinstance(validation, dict) or not isinstance(candidates, list):
        report.add("SUM-1", stage, title, NOT_EVALUABLE,
                   "The manifest records no checksum validation block or no input candidates.",
                   inherited_from=inherited_from)
        return
    verified = validation.get("verified")
    skipped = validation.get("skipped")
    expected = len(candidates)
    evidence = {
        "verified": verified, "skipped": skipped, "inputs": expected,
        "any_checksum_verified": validation.get("required"), "inherited_from": inherited_from,
    }
    if verified == expected and skipped == 0:
        report.add("SUM-1", stage, title, PASS,
                   f"All {expected} inputs verified, none skipped.", **evidence)
        return
    files = [item for item in (owner.get("project") or {}).get("files") or [] if isinstance(item, dict)]
    declared = [item for item in files if str(item.get("checksum") or "").strip()]
    downloads = [item for item in owner.get("downloads") or [] if isinstance(item, dict)]
    hashed = [item for item in downloads if str(item.get("sha256") or "").strip()]
    if not verified and files and not declared and downloads and len(hashed) == len(downloads):
        report.add(
            "SUM-1", stage, title, WARN,
            "The repository declared no checksum for any file, so nothing could be compared with a "
            f"source value. Integrity rests on the sha256 recorded at download for all {len(hashed)} "
            "downloaded files. No artifact may describe these inputs as checksum-verified.",
            declared_checksums=0, downloads_with_sha256=len(hashed), **evidence,
        )
        return
    report.add(
        "SUM-1", stage, title, FAIL,
        "Checksum coverage is partial. The integrity claim in the published audit trail would "
        "overstate what was actually checked.",
        declared_checksums=len(declared), downloads_with_sha256=len(hashed), **evidence,
    )


def check_split_part_partitions_its_parent(report: Report, provenance: dict | None, reason: str) -> None:
    """SPL-1. A split part is one of a set of parts that hold exactly the parent's inputs, once each.

    Each part can pass every other check on its own. What none of them can see is a file that went
    into two parts or into none; the parent's split_into and each part's input_candidates are the
    two records that must agree.
    """
    stage = "before-production"
    title = "A split part partitions its parent's inputs"
    split_from = (provenance or {}).get("split_from")
    if not isinstance(split_from, dict):
        report.add("SPL-1", stage, title, NOT_EVALUABLE,
                   reason or "This unit was not split from another.", required=False)
        return
    parent, parent_reason = _read_json(Path(str(split_from.get("manifest_path") or "")))
    if parent is None:
        report.add("SPL-1", stage, title, NOT_EVALUABLE, parent_reason)
        return
    own_id = str((provenance.get("project") or {}).get("analysis_unit_id") or "")
    parts = [item for item in parent.get("split_into") or [] if isinstance(item, dict)]
    own = next((item for item in parts if item.get("analysis_unit_id") == own_id), None)
    parent_inputs = sorted(str(item) for item in parent.get("input_candidates") or [])
    claimed = sorted(str(path) for item in parts for path in item.get("input_candidates") or [])
    problems = []
    if parent.get("status") != "split_by_acquisition" or parent.get("execution_allowed") is not False:
        problems.append("the parent is not recorded as split and held from execution")
    if own is None:
        problems.append(f"the parent's split_into does not name {own_id or 'this part'}")
    elif sorted(str(item) for item in own.get("input_candidates") or []) != sorted(
        str(item) for item in provenance.get("input_candidates") or []
    ):
        problems.append("this part's input_candidates differ from the parent's record of it")
    if claimed != parent_inputs:
        problems.append(f"the parts hold {len(claimed)} inputs, {len(set(claimed))} distinct, "
                        f"against the parent's {len(parent_inputs)}")
    if problems:
        report.add("SPL-1", stage, title, FAIL, "; ".join(problems) + ".",
                   parent=split_from.get("analysis_unit_id"), part=own_id)
        return
    report.add("SPL-1", stage, title, PASS,
               f"{own_id} is one of {len(parts)} parts holding the parent's {len(parent_inputs)} "
               "inputs exactly once.", parent=split_from.get("analysis_unit_id"), parts=len(parts))


# --------------------------------------------------------------------------------------------
# the sample-count invariant
# --------------------------------------------------------------------------------------------

def _mdpeak_count(output: Path) -> int:
    return len(list(output.glob("*.mdpeak")))


def _production_started(output: Path) -> bool:
    """Whether a production run has left anything in this output directory.

    The Console writes method.keys.json when it reads the method file and a .mdpeak per file it
    processed; a finished run adds an mzTab-M and the publication report. With none of them, no
    production run has started here, and a check owed by a later stage has nothing to judge: that
    is a stage not reached, not two records disagreeing. It stays required, so --strict still
    refuses the unit. Any one of them present means something ran, and the checks judge it.
    """
    return (
        (output / "method.keys.json").is_file()
        or _mdpeak_count(output) > 0
        or any(output.glob("*.mzTab"))
        or (output / "MS_DIAL_publication_report.json").is_file()
    )


NOT_STARTED = "No production run has started in this workspace: no .mdpeak and no method.keys.json."


def _mztab_run_count(mztab: Path) -> tuple[int | None, str]:
    """Count ms_run entries declared in the metadata section."""
    try:
        seen = set()
        with mztab.open(encoding="ascii", errors="replace") as handle:
            for line in handle:
                if not line.startswith("MTD\t"):
                    continue
                match = re.match(r"MTD\tms_run\[(\d+)\]-location\t", line)
                if match:
                    seen.add(int(match.group(1)))
        return (len(seen) or None), "" if seen else "no ms_run location entries were found"
    except OSError as exc:
        return None, f"{mztab.name} could not be read: {exc}"


def check_sample_count_invariant(
    report: Report,
    provenance: dict | None,
    provenance_reason: str,
    csv_rows: list[dict] | None,
    csv_reason: str,
    run_manifest: dict | None,
    output: Path,
    stage: str,
) -> int | None:
    """The approved sample count must survive every stage, and be re-read from disk each time.

    This is the one assertion that covers the largest number of independent failures: the tuning
    diagnostic rewriting the production CSV to a single row, a CSV row silently dropped because its
    file was missing or locked, and MS-DIAL exiting 0 having skipped files it could not read. Each
    of those produces a complete result set describing a study nobody approved, and none of them
    raises anything anywhere.

    The counts are read from the artifacts rather than from a tool response on purpose. The guided
    planner response that would report the same number is large enough to be truncated in transport,
    and its blockers field is serialised after the payload, so its absence and its emptiness look
    alike.
    """
    counts: dict[str, int] = {}
    missing: list[str] = []

    if provenance is not None and isinstance(provenance.get("input_candidates"), list):
        counts["repository_manifest.input_candidates"] = len(provenance["input_candidates"])
    else:
        missing.append(provenance_reason or "the repository manifest has no input_candidates")

    if csv_rows is not None:
        counts["analysis_files.csv rows"] = len(csv_rows)
    else:
        missing.append(csv_reason)

    if run_manifest is not None and isinstance(run_manifest.get("source_files"), list):
        counts["run_manifest.source_files"] = len(run_manifest["source_files"])

    if stage in ("after-run", "before-publish"):
        if not _production_started(output):
            report.add("CNT-1", stage, "The approved sample count survived every stage",
                       NOT_EVALUABLE, NOT_STARTED, counts=counts)
            return None
        counts[".mdpeak files produced"] = _mdpeak_count(output)
        if run_manifest is not None and isinstance(run_manifest.get("expected_analysis_exports"), list):
            counts["run_manifest.expected_analysis_exports"] = len(run_manifest["expected_analysis_exports"])

    if len(counts) < 2:
        report.add("CNT-1", stage, "The approved sample count survived every stage", NOT_EVALUABLE,
                   "; ".join(missing) or "fewer than two independent counts are available",
                   counts=counts)
        return None

    distinct = set(counts.values())
    if len(distinct) == 1:
        value = distinct.pop()
        report.add("CNT-1", stage, "The approved sample count survived every stage", PASS,
                   f"All {len(counts)} independent records agree on {value} samples.", counts=counts)
        return value

    report.add(
        "CNT-1", stage, "The approved sample count survived every stage", FAIL,
        "Independent records of the same study disagree on how many samples it contains. Whichever "
        "is right, at least one retained artifact describes a study that was not run.",
        counts=counts,
    )
    return None


def check_expected_exports_present(
    report: Report, run_manifest: dict | None, output: Path, stage: str
) -> None:
    """Every file the run said it would produce must exist.

    The production job marks itself completed on the Console exit code alone; expected_analysis_exports
    is computed and then read by nothing.
    """
    if stage == "before-production":
        return
    if run_manifest is None or not isinstance(run_manifest.get("expected_analysis_exports"), list):
        report.add("EXP-1", stage, "Every expected export exists", NOT_EVALUABLE,
                   "The run manifest records no expected_analysis_exports.")
        return
    if not _production_started(output):
        report.add("EXP-1", stage, "Every expected export exists", NOT_EVALUABLE, NOT_STARTED,
                   expected=len(run_manifest["expected_analysis_exports"]))
        return
    expected = [Path(item) for item in run_manifest["expected_analysis_exports"]]
    absent = [str(path) for path in expected if not path.exists()]
    if not absent:
        report.add("EXP-1", stage, "Every expected export exists", PASS,
                   f"All {len(expected)} expected exports are present.", expected=len(expected))
        return
    report.add(
        "EXP-1", stage, "Every expected export exists", FAIL,
        "MS-DIAL reported success without producing every export the run planned. A file it could "
        "not read is skipped silently, and the exit code does not reflect it.",
        expected=len(expected), absent_count=len(absent), absent=absent[:10],
    )


# --------------------------------------------------------------------------------------------
# class and run order
# --------------------------------------------------------------------------------------------

_QC_TOKENS = ("qc",)
_BLANK_TOKENS = ("blank",)


def check_class_distribution(report: Report, csv_rows: list[dict] | None, reason: str, stage: str) -> None:
    """Report the executed grouping, and flag names a substring matcher would reclassify.

    Sample category is re-derived downstream by substring match over file_type + class_id, so a
    biological class whose name contains "qc" or "blank" is removed from the comparison and
    simultaneously used as the QC-precision basis. The study that motivated this check contains a
    wine strain named QA23 with samples QA1..QA3; it survives that matcher, but only just.
    """
    if stage != "before-production":
        return
    if csv_rows is None:
        report.add("CLS-1", stage, "Executed grouping is stated and unambiguous", NOT_EVALUABLE, reason)
        return
    if not csv_rows:
        report.add("CLS-1", stage, "Executed grouping is stated and unambiguous", FAIL,
                   "The analysis CSV has no data rows.")
        return
    classes = Counter(str(row.get("class_id", "")).strip() for row in csv_rows)
    file_types = Counter(str(row.get("file_type", "")).strip() for row in csv_rows)
    blank_named = sorted(
        name for name in classes
        if any(token in name.lower() for token in _QC_TOKENS + _BLANK_TOKENS)
    )
    empty = classes.get("", 0)
    if empty:
        report.add(
            "CLS-1", stage, "Executed grouping is stated and unambiguous", FAIL,
            f"{empty} rows carry no class_id. An empty class cell is silently read as 'Sample' "
            "downstream, which removes those samples from the biological comparison without saying so.",
            classes=dict(classes), file_types=dict(file_types),
        )
        return
    if blank_named:
        report.add(
            "CLS-1", stage, "Executed grouping is stated and unambiguous", WARN,
            "A class name contains 'qc' or 'blank' as a substring. Sample category is re-derived by "
            "substring match downstream, so this class may be pulled out of the biological "
            "comparison and used as the QC or blank basis instead. Confirm the intent before running.",
            classes=dict(classes), file_types=dict(file_types), matched_names=blank_named,
        )
        return
    report.add("CLS-1", stage, "Executed grouping is stated and unambiguous", PASS,
               f"{len(classes)} classes over {len(csv_rows)} samples.",
               classes=dict(classes), file_types=dict(file_types))


def check_analytical_order_is_real(report: Report, csv_rows: list[dict] | None, reason: str, stage: str) -> None:
    """Decide whether the recorded injection order is a measurement or a row number.

    When a repository records no injection sequence, the order is synthesized from CSV row order,
    which is grouped by class. A run-order drift statistic computed against it is perfectly
    confounded with the biological factor, and a near-zero correlation is an artifact of an order
    that does not exist rather than evidence of analytical stability.
    """
    if stage != "before-production":
        return
    if csv_rows is None:
        report.add("ORD-1", stage, "Recorded analytical order is a measurement", NOT_EVALUABLE, reason)
        return
    raw = [str(row.get("analytical_order", "")).strip() for row in csv_rows]
    if not all(value.isdigit() for value in raw) or not raw:
        report.add("ORD-1", stage, "Recorded analytical order is a measurement", NOT_EVALUABLE,
                   "analytical_order is absent or not numeric on every row.")
        return
    order = [int(value) for value in raw]
    sequential = order == list(range(1, len(order) + 1))

    classes = [str(row.get("class_id", "")).strip() for row in csv_rows]
    blocks = 0
    previous = object()
    for name in classes:
        if name != previous:
            blocks += 1
            previous = name
    contiguous = blocks == len(set(classes))

    if sequential and contiguous and len(set(classes)) > 1:
        report.add(
            "ORD-1", stage, "Recorded analytical order is a measurement", WARN,
            "analytical_order is exactly the row number and every class occupies one contiguous "
            "block of it, so the order was synthesized from file order rather than recorded by the "
            "repository. It is perfectly confounded with class. This does not stop the run, but no "
            "run-order drift metric computed from it may be reported, and the mzTab-M "
            "injection-sequence label must not be presented as a real acquisition order. ORD-2 "
            "enforces that at publication.",
            classes=len(set(classes)), samples=len(order), contiguous_blocks=blocks,
            synthesized=True,
        )
        return
    if sequential:
        report.add(
            "ORD-1", stage, "Recorded analytical order is a measurement", WARN,
            "analytical_order is exactly the row number. It may still be the true injection order, "
            "but nothing here establishes that it is.",
            samples=len(order),
        )
        return
    report.add("ORD-1", stage, "Recorded analytical order is a measurement", PASS,
               "analytical_order is not a restatement of row order.", samples=len(order))


def _order_is_synthesized(csv_rows: list[dict] | None) -> bool:
    if not csv_rows:
        return False
    raw = [str(row.get("analytical_order", "")).strip() for row in csv_rows]
    if not all(value.isdigit() for value in raw):
        return False
    if [int(value) for value in raw] != list(range(1, len(raw) + 1)):
        return False
    classes = [str(row.get("class_id", "")).strip() for row in csv_rows]
    blocks = 0
    previous = object()
    for name in classes:
        if name != previous:
            blocks += 1
            previous = name
    return blocks == len(set(classes)) and len(set(classes)) > 1


def check_no_metric_rests_on_a_synthetic_order(
    report: Report, output: Path, csv_rows: list[dict] | None, stage: str
) -> None:
    """A drift statistic must not be reported against an order that was never recorded.

    When the order is the row number and each class is one contiguous block, a near-zero
    correlation is an artifact of the ordering, not evidence of analytical stability. The failure
    mode this catches is specific and has been observed: the single QA criterion a QC-free study
    could evaluate was this one, so the assessment reported "1 of 1 passed" on the strength of it.
    """
    if stage != "before-publish":
        return
    if csv_rows is None:
        # Without the analysis CSV the order cannot be characterised at all. Reading that absence
        # as "the order is real" would be the same mistake this check exists to catch.
        report.add("ORD-2", stage, "No reported metric rests on a synthesized run order",
                   NOT_EVALUABLE,
                   "The analysis CSV is absent, so whether the run order was recorded or "
                   "synthesized cannot be established.")
        return
    if not _order_is_synthesized(csv_rows):
        report.add("ORD-2", stage, "No reported metric rests on a synthesized run order", PASS,
                   "The run order is not a restatement of file order, so a drift metric computed "
                   "from it is meaningful.")
        return
    report_json, reason = _read_json(output / "MS_DIAL_publication_report.json")
    if report_json is None:
        report.add("ORD-2", stage, "No reported metric rests on a synthesized run order",
                   NOT_EVALUABLE, reason)
        return
    assessment = report_json.get("qa_assessment") or {}
    checks = assessment.get("checks") if isinstance(assessment, dict) else None
    asserted = [
        item for item in (checks or [])
        if isinstance(item, dict)
        and "run_order" in str(item.get("metric", ""))
        and item.get("value") is not None
        and item.get("status") not in (None, "not_assessed")
    ]
    if not asserted:
        report.add("ORD-2", stage, "No reported metric rests on a synthesized run order", PASS,
                   "The run order is synthesized, and no run-order criterion is asserted.")
        return
    report.add(
        "ORD-2", stage, "No reported metric rests on a synthesized run order", FAIL,
        "The run order was synthesized from file order and is perfectly confounded with class, yet "
        f"{len(asserted)} run-order criterion is reported with a value and a verdict. For a study "
        "whose other criteria are not assessable this becomes the entire QA claim, so the "
        "assessment reads as a pass on the one statistic that means nothing here.",
        asserted=[
            {"metric": item.get("metric"), "value": item.get("value"), "status": item.get("status")}
            for item in asserted
        ],
        evaluated=assessment.get("evaluated"), passed=assessment.get("passed"),
    )


# --------------------------------------------------------------------------------------------
# mzTab-M structure
# --------------------------------------------------------------------------------------------

def check_mztab_structure(report: Report, output: Path, stage: str) -> None:
    """Every data row must have exactly as many fields as its section header declares.

    A whole-section off-by-one is not a cosmetic warning: the interchange format is what the
    campaign redistributes, and a strict third-party validator may reject what the built-in
    structural check reports as a warning.
    """
    if stage == "before-production":
        return
    files = sorted(output.glob("*.mzTab"))
    if not files:
        report.add("TAB-1", stage, "mzTab-M rows match their section headers", NOT_EVALUABLE,
                   "No .mzTab file is present in the output directory.")
        return
    header_prefix = {"SMH": "SML", "SFH": "SMF", "SEH": "SME"}
    for mztab in files:
        widths: dict[str, int] = {}
        mismatched: Counter = Counter()
        totals: Counter = Counter()
        try:
            with mztab.open(encoding="ascii", errors="replace") as handle:
                for line in handle:
                    fields = line.rstrip("\n").rstrip("\r").split("\t")
                    prefix = fields[0]
                    if prefix in header_prefix:
                        widths[header_prefix[prefix]] = len(fields)
                    elif prefix in widths:
                        totals[prefix] += 1
                        if len(fields) != widths[prefix]:
                            mismatched[prefix] += 1
        except OSError as exc:
            report.add("TAB-1", stage, "mzTab-M rows match their section headers", NOT_EVALUABLE,
                       f"{mztab.name} could not be read: {exc}")
            continue
        if not totals:
            report.add("TAB-1", stage, "mzTab-M rows match their section headers", NOT_EVALUABLE,
                       f"{mztab.name} carries no data rows in the summary, feature or evidence sections.",
                       file=mztab.name)
            continue
        if not mismatched:
            report.add("TAB-1", stage, "mzTab-M rows match their section headers", PASS,
                       f"{mztab.name}: every row matches its header width.",
                       file=mztab.name, rows=dict(totals), widths=widths)
            continue
        whole_section = [
            section for section, count in mismatched.items() if count == totals[section]
        ]
        detail = (
            f"{mztab.name}: rows disagree with their section header width. "
            + ("Every row of " + ", ".join(sorted(whole_section)) + " is affected, which is a "
               "systematic exporter defect rather than a damaged file. " if whole_section else "")
            + "The file must not be redistributed in this state."
        )
        report.add("TAB-1", stage, "mzTab-M rows match their section headers", FAIL, detail,
                   file=mztab.name, mismatched=dict(mismatched), rows=dict(totals), widths=widths)


def check_mztab_run_count(report: Report, output: Path, expected: int | None, stage: str) -> None:
    if stage == "before-production":
        return
    files = sorted(output.glob("*.mzTab"))
    if not files:
        report.add("TAB-2", stage, "mzTab-M declares one ms_run per approved sample", NOT_EVALUABLE,
                   "No .mzTab file is present in the output directory.")
        return
    if expected is None:
        report.add("TAB-2", stage, "mzTab-M declares one ms_run per approved sample", NOT_EVALUABLE,
                   "The approved sample count could not be established, so there is nothing to "
                   "compare the ms_run count against.")
        return
    for mztab in files:
        count, reason = _mztab_run_count(mztab)
        if count is None:
            report.add("TAB-2", stage, "mzTab-M declares one ms_run per approved sample",
                       NOT_EVALUABLE, f"{mztab.name}: {reason}", file=mztab.name)
            continue
        status = PASS if count == expected else FAIL
        detail = (
            f"{mztab.name} declares {count} ms_run entries against {expected} approved samples."
            if status == PASS else
            f"{mztab.name} declares {count} ms_run entries but {expected} samples were approved. "
            "The published matrix does not describe the study that was approved."
        )
        report.add("TAB-2", stage, "mzTab-M declares one ms_run per approved sample", status,
                   detail, file=mztab.name, ms_runs=count, approved=expected)


# --------------------------------------------------------------------------------------------
# publication contradictions
# --------------------------------------------------------------------------------------------

def check_library_provenance_contradiction(
    report: Report, output: Path, stage: str
) -> None:
    """A provenance warning must not name a library whose provenance is recorded.

    The reporter matches recorded provenance on a key the writer does not emit, so the lookup always
    misses and the warning fires unconditionally. The consequence for an unattended loop is the
    reverse of the obvious one: because the reporter never actually matches anything, the ABSENCE of
    a warning is not evidence of provenance either, and neither state can be trusted on its own.
    """
    if stage != "before-publish":
        return
    settings, settings_reason = _read_json(output / "workflow-settings.json")
    report_json, report_reason = _read_json(output / "MS_DIAL_publication_report.json")
    if settings is None or report_json is None:
        report.add("LIB-1", stage, "Library provenance warnings agree with recorded provenance",
                   NOT_EVALUABLE, "; ".join(filter(None, [settings_reason, report_reason])))
        return
    recorded = settings.get("library_provenance")
    if not isinstance(recorded, list):
        report.add("LIB-1", stage, "Library provenance warnings agree with recorded provenance",
                   NOT_EVALUABLE, "workflow-settings.json records no library_provenance list.")
        return

    def identified(item: dict) -> bool:
        return any(str(item.get(key, "")).strip() for key in ("doi", "record_url", "source", "version"))

    identifiers = {
        Path(str(item.get("path", ""))).name: identified(item)
        for item in recorded if isinstance(item, dict)
    }
    warnings = report_json.get("library_provenance_warnings")
    if not isinstance(warnings, list):
        report.add("LIB-1", stage, "Library provenance warnings agree with recorded provenance",
                   NOT_EVALUABLE, "The publication report carries no library_provenance_warnings list.")
        return
    contradicted = sorted(
        name for name, has_id in identifiers.items()
        if has_id and any(name and name in str(text) for text in warnings)
    )
    if contradicted:
        report.add(
            "LIB-1", stage, "Library provenance warnings agree with recorded provenance", FAIL,
            "The publication report states that no persistent identifier was recorded for a library "
            "whose identifier the run settings do record. The generated audit trail contradicts the "
            "run's own provenance, and correcting the library metadata would not silence it.",
            contradicted=contradicted, warnings=[str(text)[:160] for text in warnings][:5],
        )
        return
    report.add("LIB-1", stage, "Library provenance warnings agree with recorded provenance", PASS,
               "No provenance warning names a library whose identifier is recorded.",
               libraries=sorted(identifiers), warnings=len(warnings))


def check_qa_prose_matches_assessment(report: Report, output: Path, stage: str) -> None:
    """The generated prose must not recite a QA battery that was not performed."""
    if stage != "before-publish":
        return
    report_json, reason = _read_json(output / "MS_DIAL_publication_report.json")
    if report_json is None:
        report.add("QA-1", stage, "QA prose matches the assessment it summarizes", NOT_EVALUABLE, reason)
        return
    assessment = report_json.get("qa_assessment")
    if not isinstance(assessment, dict) or not isinstance(assessment.get("checks"), list):
        report.add("QA-1", stage, "QA prose matches the assessment it summarizes", NOT_EVALUABLE,
                   "The publication report carries no qa_assessment.checks list.")
        return
    checks = assessment["checks"]
    statuses = Counter(str(item.get("status", "")) for item in checks if isinstance(item, dict))
    evaluated = assessment.get("evaluated")
    not_assessed = statuses.get("not_assessed", 0)
    if not_assessed == 0:
        report.add("QA-1", stage, "QA prose matches the assessment it summarizes", PASS,
                   f"All {len(checks)} prespecified criteria were assessable.",
                   statuses=dict(statuses), evaluated=evaluated)
        return
    report.add(
        "QA-1", stage, "QA prose matches the assessment it summarizes", WARN,
        f"{not_assessed} of {len(checks)} prespecified QA criteria were not assessable, but the "
        f"summary reports {evaluated} evaluated. The Methods prose renders that as a complete "
        "battery. Before publishing, state which criteria were evaluated and why the rest were not.",
        statuses=dict(statuses), evaluated=evaluated, total=len(checks),
        not_assessed_names=[
            str(item.get("name") or item.get("id") or "?")
            for item in checks
            if isinstance(item, dict) and item.get("status") == "not_assessed"
        ],
    )


# --------------------------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------------------------

def _tree_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _raw_directory(provenance: dict | None, workspace: Path) -> Path:
    """The raw tree this unit reads. A split part owns none and reads its parent's.

    The part's manifest names the parent's tree in raw_directory and the owner in raw_owned_by.
    Looking only under the part's own workspace reports its raw data as released while it is on
    disk.
    """
    if isinstance(provenance, dict) and provenance.get("raw_owned_by") and provenance.get("raw_directory"):
        return Path(str(provenance["raw_directory"]))
    return workspace / "raw"


def check_storage_shape(report: Report, workspace: Path, stage: str,
                        provenance: dict | None = None) -> None:
    """Report what the unit actually occupies, against what a transfer figure would suggest."""
    if stage != "before-publish":
        return
    if isinstance(provenance, dict) and provenance.get("raw_owned_by"):
        # A split part owns no raw tree. Its storage is counted once, at the owner: counting it in
        # every part would double the unit, and calling it released would be false.
        report.add("DSK-1", stage, "Retained storage is accounted for", NOT_EVALUABLE,
                   f"The raw tree is owned by {provenance['raw_owned_by']} and is accounted there.",
                   required=False, raw_owned_by=str(provenance["raw_owned_by"]))
        return
    downloads = workspace / "raw" / "downloads"
    data = workspace / "raw" / "data"
    if not downloads.exists() and not data.exists():
        # Not required: a released raw tree is the intended end state under the campaign's
        # delete-after-validated-output policy, so its absence is a result, not a gap.
        report.add("DSK-1", stage, "Retained storage is accounted for", NOT_EVALUABLE,
                   "No raw directory is present; the raw tree may already have been released.",
                   required=False)
        return
    archive = _tree_bytes(downloads) if downloads.exists() else 0
    extracted = _tree_bytes(data) if data.exists() else 0
    total = archive + extracted
    if archive and extracted:
        report.add(
            "DSK-1", stage, "Retained storage is accounted for", WARN,
            "The downloaded archive and its extraction are both retained, so the unit occupies "
            f"{total / 1e9:.2f} GB for {max(archive, extracted) / 1e9:.2f} GB of unique data. A "
            "size approval quoted against the transfer figure understated actual disk use.",
            archive_bytes=archive, extracted_bytes=extracted, total_bytes=total,
        )
        return
    report.add("DSK-1", stage, "Retained storage is accounted for", PASS,
               f"The unit occupies {total / 1e9:.2f} GB.",
               archive_bytes=archive, extracted_bytes=extracted, total_bytes=total)


# --------------------------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------------------------

# ---------------------------------------------------------------------------------------------
# Checks added 2026-09-20, after an independent review measured that every record the pipeline had
# gained since this file was written was read by nothing. Each one compares two facts written by
# code paths that do not know about each other; a check that reads one artifact and trusts it is
# worthless here, because the failure this programme keeps finding is a component staying
# consistent with itself while disagreeing with everything else.
# ---------------------------------------------------------------------------------------------


def _class_proposal(provenance: dict | None) -> dict | None:
    if not isinstance(provenance, dict):
        return None
    proposal = (provenance.get("project") or {}).get("class_proposal")
    return proposal if isinstance(proposal, dict) and proposal else None


def _files_of_samples(provenance: dict | None, samples: set[str], csv_names: set[str]) -> dict[str, list[str]]:
    """The analysis-CSV rows each approved sample is, by name or through its recorded raw file.

    A sample whose id is itself a CSV file_name maps to that row. Otherwise every raw_file the
    unit's sample metadata records for it is reduced to the name the CSV uses (the file name
    without its extension) and kept if the CSV has it. A sample that maps to nothing is absent.
    """
    recorded: dict[str, list[str]] = {}
    for row in ((provenance or {}).get("project") or {}).get("sample_metadata") or []:
        if not isinstance(row, dict):
            continue
        sample = str(row.get("sample_id") or "")
        raw = str(row.get("raw_file") or "").replace("\\", "/").rsplit("/", 1)[-1]
        if sample and raw:
            recorded.setdefault(sample, []).append(raw)
    result: dict[str, list[str]] = {}
    for sample in samples:
        if sample in csv_names:
            result[sample] = [sample]
            continue
        names = []
        for raw in recorded.get(sample, []):
            for candidate in (raw, raw.rsplit(".", 1)[0] if "." in raw else raw):
                if candidate in csv_names and candidate not in names:
                    names.append(candidate)
                    break
        result[sample] = names
    return result


def check_executed_class_matches_approved(
    report: Report, provenance: dict | None, reason: str,
    csv_rows: list[dict] | None, csv_reason: str,
) -> None:
    """CLS-2. The grouping MS-DIAL ran with is the grouping that was approved.

    Two writers. The Catalog proposes and stores the assignments, which travel into the unit's
    provenance manifest through a handoff; the Interactive preparer independently projects the
    reviewed sample metadata into the analysis CSV the Console actually reads. Nothing has ever
    compared them, and an approved proposal being received and then ignored -- the grouping
    re-derived from an argument instead -- is a defect this project has already had once.

    CLS-1 asks whether the executed grouping is stated and unambiguous. It is satisfied by a
    perfectly clean grouping of the wrong thing.
    """
    stage = "before-production"
    proposal = _class_proposal(provenance)
    if proposal is None:
        report.add("CLS-2", stage, "Executed Class is the Class that was approved", NOT_EVALUABLE,
                   reason or "The manifest carries no Class proposal.", required=False)
        return
    if csv_rows is None:
        report.add("CLS-2", stage, "Executed Class is the Class that was approved", NOT_EVALUABLE,
                   csv_reason)
        return
    approved = {
        str(item.get("sample_id", "")): str(item.get("class_label", ""))
        for item in proposal.get("assignments") or []
        if isinstance(item, dict)
    }
    if not approved:
        report.add("CLS-2", stage, "Executed Class is the Class that was approved", NOT_EVALUABLE,
                   "The Class proposal carries no assignments.")
        return
    executed = {str(row.get("file_name", "")): str(row.get("class_id", "")) for row in csv_rows}
    # A Class is assigned to a SAMPLE and the CSV has a row per FILE. Where a repository names its
    # samples after their files the two keys coincide, which is all this check used to handle.
    # MetaboLights does not: MTBLS2207's "DDA E. coli" is the file M3T-Std_Ecoli_neg_DDA_1mz, and
    # joining on equal strings called all six approved samples absent and all six rows unapproved
    # while every one carried its approved Class. The repository's own sample-to-file record
    # (sample_metadata raw_file) is the link; it is neither of the two writers being compared.
    files_of_sample = _files_of_samples(provenance, set(approved), set(executed))
    mapped: set[str] = set()
    missing = []
    differing = []
    for sample, label in sorted(approved.items()):
        files = files_of_sample.get(sample) or []
        if not files:
            missing.append(sample)
            continue
        for name in files:
            mapped.add(name)
            if executed[name] != label:
                differing.append(
                    f"{sample}{'' if name == sample else f' ({name})'}: approved {label!r}, "
                    f"executed {executed[name]!r}"
                )
    extra = sorted(set(executed) - mapped)
    joined_by_file = sum(1 for sample, files in files_of_sample.items() if files and files != [sample])
    if not missing and not extra and not differing:
        report.add("CLS-2", stage, "Executed Class is the Class that was approved", PASS,
                   f"All {len(approved)} approved assignments appear in the analysis CSV with the "
                   "same Class.", assignments=len(approved), joined_through_raw_file=joined_by_file)
        return
    report.add(
        "CLS-2", stage, "Executed Class is the Class that was approved", FAIL,
        "The grouping the Console will read is not the grouping that was approved. "
        f"{len(differing)} sample(s) carry a different Class, {len(missing)} approved sample(s) "
        f"are absent from the CSV, {len(extra)} CSV row(s) were never approved.",
        differing=differing[:10], missing=missing[:10], unapproved=extra[:10],
    )


def check_class_proposal_was_accepted(report: Report, provenance: dict | None, reason: str) -> None:
    """CLS-3. A grouping was ratified, not merely suggested.

    The contract requires an explicit user confirmation before a Class proposal is saved. The
    confirmation is given in a conversation; what a later audit can read is the proposal's own
    status. A proposal still reading "proposed" beside an executed, published run says the
    ratification happened somewhere no artifact records -- which, for a machine-authored grouping,
    is the whole of the safety argument.
    """
    stage = "before-production"
    proposal = _class_proposal(provenance)
    if proposal is None:
        report.add("CLS-3", stage, "The executed grouping was ratified", NOT_EVALUABLE,
                   reason or "The manifest carries no Class proposal.", required=False)
        return
    status = str(proposal.get("status") or "").strip().casefold()
    model = str(proposal.get("model") or "")
    warnings = [str(item) for item in proposal.get("warnings") or []]
    if status in {"accepted", "confirmed", "approved"}:
        report.add("CLS-3", stage, "The executed grouping was ratified", PASS,
                   f"Class proposal status is {status!r}.", status=status, model=model,
                   warnings=warnings[:4])
        return
    report.add(
        "CLS-3", stage, "The executed grouping was ratified", FAIL,
        f"Class proposal status is {status or 'absent'!r}, not an accepted one. The grouping was "
        f"authored by {model or 'an unrecorded author'} and no artifact records that anyone "
        "ratified it.",
        status=status, model=model, warnings=warnings[:4],
    )


def check_unit_reached_a_terminal_state(
    report: Report, provenance: dict | None, reason: str, output: Path | None = None
) -> None:
    """FIN-1. The unit finished, rather than stopping somewhere that looks finished.

    finalize_download_lease is what validates the mzTab-M, records the retained-artifact inventory
    and stamps finalized_at. A workspace can hold a complete mzTab-M, a publication bundle and
    supplementary tables while its manifest still reads the status it had before the run, because
    nothing compares the two. The publishable output is not evidence that the unit was finalised;
    the finalisation record is.
    """
    stage = "before-publish"
    if provenance is None:
        report.add("FIN-1", stage, "The unit reached a recorded terminal state", NOT_EVALUABLE,
                   reason)
        return
    status = str(provenance.get("status") or "")
    finalized = provenance.get("finalized_at")
    validation = provenance.get("mztab_validation")
    if status == "mztab_validated" and finalized and isinstance(validation, dict):
        report.add("FIN-1", stage, "The unit reached a recorded terminal state", PASS,
                   f"status={status!r}, finalized at {finalized}.", status=status,
                   finalized_at=str(finalized))
        return
    if status == "run_failed":
        report.add("FIN-1", stage, "The unit reached a recorded terminal state", FAIL,
                   "The unit recorded a failed run. Nothing here should be published.",
                   status=status, run_failures=len(provenance.get("run_failures") or []))
        return
    if output is not None and not _production_started(output):
        report.add("FIN-1", stage, "The unit reached a recorded terminal state", NOT_EVALUABLE,
                   NOT_STARTED, status=status)
        return
    report.add(
        "FIN-1", stage, "The unit reached a recorded terminal state", FAIL,
        f"status={status or 'absent'!r}, finalized_at={finalized!r}, mztab_validation "
        f"{'present' if isinstance(validation, dict) else 'absent'}. The unit was never finalised, "
        "whatever the output directory contains.",
        status=status, finalized_at=str(finalized), has_validation=isinstance(validation, dict),
    )


def _method_threshold(output: Path) -> tuple[str | None, str]:
    method = output / "method.txt"
    if not method.is_file():
        return None, f"{method.name} is absent"
    try:
        for line in method.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip().casefold() == "minimum peak height":
                return value.strip(), ""
    except OSError as error:
        return None, str(error)
    return None, "method.txt states no Minimum peak height"


def check_threshold_was_measured_on_this_unit(
    report: Report, provenance: dict | None, reason: str, output: Path
) -> None:
    """PKH-1. The peak-detection threshold is one this unit's own diagnostic produced.

    The contract mandates a zero-threshold diagnostic before every production repository run and
    requires its method, representative sample, count, step and accepted threshold in provenance.
    The diagnostic writes into the unit manifest; the threshold the Console will read is written
    independently by the preparer into method.txt. Comparing them is the only way to distinguish a
    threshold measured on this unit from one typed in, inherited from the previous unit, or left
    at a default.

    Compared as numbers, so 500 and 500.0 are the same threshold. Whether the Console can PARSE
    that literal is MTH-1's question, not this one.
    """
    stage = "before-production"
    if provenance is None:
        report.add("PKH-1", stage, "The threshold was measured on this unit", NOT_EVALUABLE, reason)
        return
    diagnostics = provenance.get("peak_height_diagnostics")
    written, written_reason = _method_threshold(output)
    if not isinstance(diagnostics, list) or not diagnostics:
        report.add(
            "PKH-1", stage, "The threshold was measured on this unit", FAIL,
            "No peak-count diagnostic is recorded for this unit. The contract requires one before "
            f"every production run, and method.txt asks for {written or 'an unstated threshold'}.",
            method_threshold=written,
        )
        return
    if written is None:
        report.add("PKH-1", stage, "The threshold was measured on this unit", NOT_EVALUABLE,
                   written_reason)
        return
    measured = []
    for item in diagnostics:
        if isinstance(item, dict) and item.get("minimum_peak_height") is not None:
            try:
                measured.append(float(item["minimum_peak_height"]))
            except (TypeError, ValueError):
                continue
    try:
        executed = float(written)
    except ValueError:
        report.add("PKH-1", stage, "The threshold was measured on this unit", FAIL,
                   f"method.txt states a Minimum peak height of {written!r}, which is not a number.",
                   method_threshold=written, measured=measured)
        return
    if any(abs(executed - value) < 1e-9 for value in measured):
        latest = diagnostics[-1] if isinstance(diagnostics[-1], dict) else {}
        report.add(
            "PKH-1", stage, "The threshold was measured on this unit", PASS,
            f"method.txt asks for {written}, which this unit's diagnostic measured "
            f"({latest.get('method', 'method unrecorded')}, "
            f"{latest.get('diagnostic_peak_count', '?')} peaks at zero threshold, step "
            f"{latest.get('threshold_step', '?')}).",
            method_threshold=written, measured=measured,
            representative=(latest.get("representative") or {}).get("file_name", ""),
        )
        return
    report.add(
        "PKH-1", stage, "The threshold was measured on this unit", FAIL,
        f"method.txt asks for a Minimum peak height of {written}, and no diagnostic on this unit "
        f"produced it. Measured here: {', '.join(str(value) for value in measured) or 'nothing'}.",
        method_threshold=written, measured=measured,
    )


def check_method_file_reached_the_console(report: Report, output: Path, stage: str) -> None:
    """MTH-1. Every parameter in the method file was one the Console could use.

    Two writers again: the preparer writes method.txt, and the Console writes method.keys.json
    beside it saying which keys it applied, which it did not recognise, and whose values it could
    not read. The last of those is the dangerous one -- the key is spelled correctly, so nobody
    reading the method file would suspect it -- and it is how every threshold this campaign chose
    was discarded before the fix of 2026-09-20.

    An unrecognised key is a WARN, not a FAIL. The shipped lipidomics template contains 27 of them;
    failing on those would refuse every method file in existence, including MS-DIAL's own.
    """
    if stage == "before-production":
        return
    record = output / "method.keys.json"
    if not record.is_file():
        report.add("MTH-1", stage, "Every method-file parameter reached the Console", NOT_EVALUABLE,
                   "method.keys.json is absent: no production run has started here, or the Console "
                   "that ran predates the key record. Which parameters took effect cannot be read "
                   "from this workspace.")
        return
    parsed, reason = _read_json(record)
    if parsed is None:
        report.add("MTH-1", stage, "Every method-file parameter reached the Console", NOT_EVALUABLE,
                   reason)
        return
    unusable = [str(item) for item in parsed.get("unusable") or []]
    unrecognised = [str(item) for item in parsed.get("unrecognised") or []]
    applied = [str(item) for item in parsed.get("applied") or []]
    if unusable:
        report.add(
            "MTH-1", stage, "Every method-file parameter reached the Console", FAIL,
            f"{len(unusable)} parameter(s) named a value the Console could not read. Each kept its "
            "built-in default while the retained method file says otherwise.",
            unusable=unusable[:10], unrecognised_count=len(unrecognised),
            applied_count=len(applied),
        )
        return
    if unrecognised:
        report.add(
            "MTH-1", stage, "Every method-file parameter reached the Console", WARN,
            f"{len(applied)} parameter(s) applied; {len(unrecognised)} key(s) no reader claimed "
            "and which therefore had no effect.",
            unrecognised=unrecognised[:10], applied_count=len(applied),
        )
        return
    report.add("MTH-1", stage, "Every method-file parameter reached the Console", PASS,
               f"All {len(applied)} parameter(s) in the method file were applied.",
               applied_count=len(applied))


def check_retention_policy_was_acted_on(
    report: Report, provenance: dict | None, reason: str, workspace: Path
) -> None:
    """RET-1. The retention decision and the disk agree.

    The policy is chosen once, at download, by the person who approved the download, and written
    into the unit's manifest. Whether the raw tree is still there is a fact about the filesystem.
    Nothing compared them, so a unit could carry a complete audit record asserting a retention
    decision it never carried out, in either direction.
    """
    stage = "before-publish"
    if provenance is None:
        report.add("RET-1", stage, "The retention decision matches the disk", NOT_EVALUABLE, reason)
        return
    policy = provenance.get("raw_retention_policy")
    raw = _raw_directory(provenance, workspace)
    present = raw.is_dir() and any(raw.iterdir())
    if policy is None:
        report.add(
            "RET-1", stage, "The retention decision matches the disk", FAIL,
            "The manifest records no raw_retention_policy. The deletion preview reads this field, "
            "so a person confirming an irreversible deletion would be shown a blank where the "
            "intent should be.", raw_present=present,
        )
        return
    policy = str(policy)
    status = str(provenance.get("status") or "")
    if policy == "keep":
        verdict = PASS if present else WARN
        report.add("RET-1", stage, "The retention decision matches the disk", verdict,
                   f"Policy is {policy!r} and the raw tree is {'present' if present else 'gone'}.",
                   policy=policy, raw_present=present)
        return
    if policy == "delete_after_validated_output":
        if present and status == "mztab_validated":
            report.add("RET-1", stage, "The retention decision matches the disk", WARN,
                       "Policy is delete_after_validated_output, the output is validated, and the "
                       "raw tree is still present. Deletion needs its own confirmation and has "
                       "not been given one.", policy=policy, raw_present=present, status=status)
            return
        report.add("RET-1", stage, "The retention decision matches the disk", PASS,
                   f"Policy is {policy!r}; raw tree {'present' if present else 'released'}, "
                   f"status {status!r}.", policy=policy, raw_present=present, status=status)
        return
    report.add("RET-1", stage, "The retention decision matches the disk", FAIL,
               f"The manifest records a retention policy of {policy!r}, which is neither 'keep' "
               "nor 'delete_after_validated_output'. It cannot have been acted on.",
               policy=policy, raw_present=present)


def check_binary_identity_is_recorded(
    report: Report, run_manifest: dict | None, output: Path, stage: str
) -> None:
    """BIN-1. The software the mzTab-M attributes is the software the run recorded.

    The mzTab's software line is written by the C# binary from its own assembly attributes. The
    run manifest's version is written by the Python layer from a --version subprocess against
    whatever path console_path named at the time, and the publication report re-probes it again
    later. A rebuild or a repointed console between one unit and the next splits a batch across
    two binaries with nothing in any artifact to say so.
    """
    if stage == "before-production":
        return
    mztab_files = sorted(output.glob("*.mzTab"))
    if not isinstance(run_manifest, dict) or not mztab_files:
        report.add("BIN-1", stage, "Recorded and attributed software agree", NOT_EVALUABLE,
                   "The run manifest or the mzTab-M is absent.")
        return
    recorded = str(run_manifest.get("msdial_console_version") or "").strip()
    attributed = ""
    try:
        for line in mztab_files[0].read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0] == "MTD" and parts[1] == "software[1]":
                attributed = parts[2].strip()
                break
    except OSError as error:
        report.add("BIN-1", stage, "Recorded and attributed software agree", NOT_EVALUABLE,
                   str(error))
        return
    if not recorded or not attributed:
        report.add("BIN-1", stage, "Recorded and attributed software agree", NOT_EVALUABLE,
                   f"recorded={recorded!r}, attributed={attributed!r}.")
        return
    if recorded in attributed:
        report.add("BIN-1", stage, "Recorded and attributed software agree", PASS,
                   f"The manifest records {recorded} and the mzTab-M attributes {attributed}.",
                   recorded=recorded, attributed=attributed)
        return
    report.add(
        "BIN-1", stage, "Recorded and attributed software agree", FAIL,
        f"The manifest records MS-DIAL {recorded}; the mzTab-M attributes {attributed}. One of "
        "them is not the binary that produced these results.",
        recorded=recorded, attributed=attributed,
    )


def check_no_private_path_in_a_shared_artifact(report: Report, output: Path, stage: str) -> None:
    """SEC-1. Nothing meant for sharing carries a path from this machine.

    The contract forbids private library files and their paths from reaching bundles, logs,
    repositories or shared reports, and requires a library to be identified by name and checksum
    instead. The publication bundle is the artifact built to leave the machine, and a check that
    reads only its member NAMES passes it while its members carry the path.

    A PASS here means no known pattern matched. It is not a statement that the bundle contains no
    private data, and it must never be read as one.
    """
    if stage != "before-publish":
        return
    candidates = [
        output / "MS_DIAL_publication_reporting_bundle.zip",
        output / "MS_DIAL_publication_report.json",
        output / "Supplementary_Table_MS_DIAL.tsv",
        output / "MS_DIAL_Materials_and_Methods.txt",
    ]
    present = [path for path in candidates if path.is_file()]
    if not present:
        report.add("SEC-1", stage, "No private path in a shared artifact", NOT_EVALUABLE,
                   "No publication artifact has been generated.", required=False)
        return
    hits: list[str] = []
    for path in present:
        for member, payload in _readable_members(path):
            for match in PRIVATE_PATH_PATTERN.findall(payload):
                hits.append(f"{path.name}:{member}: {match}")
    if hits:
        report.add(
            "SEC-1", stage, "No private path in a shared artifact", FAIL,
            f"{len(hits)} occurrence(s) of a user-profile path inside an artifact built to be "
            "shared. Identify the library by name and checksum instead.",
            occurrences=sorted(set(hits))[:10], scanned=[path.name for path in present],
        )
        return
    report.add("SEC-1", stage, "No private path in a shared artifact", PASS,
               f"No user-profile path matched in {len(present)} shared artifact(s). This says no "
               "known pattern matched, not that no private data is present.",
               scanned=[path.name for path in present])


def _readable_members(path: Path) -> list[tuple[str, str]]:
    """Every text member of an artifact, so a zip is scanned by content and not by filename."""
    try:
        if path.suffix.casefold() == ".zip":
            members = []
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    try:
                        members.append((name, archive.read(name).decode("utf-8", "replace")))
                    except (OSError, ValueError):
                        continue
            return members
        return [(path.name, path.read_text(encoding="utf-8", errors="replace"))]
    except (OSError, ValueError, zipfile.BadZipFile):
        return []


def verify(workspace: Path, stage: str) -> Report:
    report = Report(workspace)
    provenance_path = workspace / "provenance" / "run-manifest.json"
    output = workspace / "output"
    provenance, provenance_reason = _read_json(provenance_path)
    csv_rows, csv_reason = _read_csv_rows(output / "analysis_files.csv")
    run_manifest, _ = _read_json(output / "run-manifest.json")

    # Every check belongs to exactly one stage: the earliest point at which it can be evaluated and
    # at which acting on it is still cheap. Running a single stage runs that stage's checks only,
    # and the caller is responsible for having run the earlier ones.
    stages = STAGES if stage == "all" else (stage,)
    approved: int | None = None

    if "before-production" in stages:
        check_unit_identity(report, provenance, provenance_reason)
        check_split_part_partitions_its_parent(report, provenance, provenance_reason)
        check_execution_allowed(report, provenance, provenance_reason)
        check_preflight_claim(report, provenance, provenance_reason)
        check_checksum_coverage(report, provenance, provenance_reason)
        check_class_distribution(report, csv_rows, csv_reason, "before-production")
        check_executed_class_matches_approved(report, provenance, provenance_reason, csv_rows, csv_reason)
        check_class_proposal_was_accepted(report, provenance, provenance_reason)
        check_threshold_was_measured_on_this_unit(report, provenance, provenance_reason, output)
        check_analytical_order_is_real(report, csv_rows, csv_reason, "before-production")
        approved = check_sample_count_invariant(
            report, provenance, provenance_reason, csv_rows, csv_reason,
            run_manifest, output, "before-production",
        )

    if "after-run" in stages:
        # Re-read from disk rather than reusing the earlier count: the point of running it twice is
        # that the artifacts may have changed between the two stages, which is the defect class this
        # whole file exists for.
        found = check_sample_count_invariant(
            report, provenance, provenance_reason, csv_rows, csv_reason,
            run_manifest, output, "after-run",
        )
        approved = found if found is not None else approved
        check_expected_exports_present(report, run_manifest, output, "after-run")
        check_mztab_structure(report, output, "after-run")
        check_mztab_run_count(report, output, approved, "after-run")
        check_method_file_reached_the_console(report, output, "after-run")
        check_binary_identity_is_recorded(report, run_manifest, output, "after-run")

    if "before-publish" in stages:
        check_no_metric_rests_on_a_synthetic_order(report, output, csv_rows, "before-publish")
        check_library_provenance_contradiction(report, output, "before-publish")
        check_qa_prose_matches_assessment(report, output, "before-publish")
        check_storage_shape(report, workspace, "before-publish", provenance)
        check_unit_reached_a_terminal_state(report, provenance, provenance_reason, output)
        check_retention_policy_was_acted_on(report, provenance, provenance_reason, workspace)
        check_no_private_path_in_a_shared_artifact(report, output, "before-publish")
    report.progress = completion_progress(workspace, provenance, output)
    return report


# ---------------------------------------------------------------------------------------------
# progress: how far a run got on real data, kept apart from whether it was right
# ---------------------------------------------------------------------------------------------
#
# The ten-unit trial is judged twice, and the two verdicts are never combined (user decision,
# 2026-09-25). Whether the pipeline is ready is Part A in the trial manifest. How far each run got
# is Part B, and it is derived here from the artifacts on disk -- never from a manifest's account of
# itself, because that account is the record that could say "done" for a unit whose gates and
# records exist and which never ran.
#
# A stage is reached when its artifacts exist, whether or not they are right. Correctness is the
# checks' business, and a stage walk that stopped at the first FAIL would report a downloaded,
# preflighted and diagnosed unit as "B0 none" on account of one refused check, which is the very
# conflation this separation exists to prevent. The checks that judge each stage are listed beside
# it, and reported by stage in the JSON.
COMPLETION_STAGES = (
    ("B1", "downloaded", "every input candidate is on disk and the raw owner's manifest lists its downloads",
     ("SUM-1",)),
    ("B2", "preflight_passed", "the manifest permits execution", ("ID-1", "SPL-1", "ELIG-1", "PRE-1")),
    ("B3", "class_settled", "the Class proposal was accepted", ("CLS-3",)),
    ("B4", "diagnostic_done", "a peak-height diagnostic is recorded and its directory exists", ("PKH-1",)),
    ("B5", "production_prepared", "output holds analysis_files.csv, method.txt and run-manifest.json",
     ("CLS-1", "CLS-2", "ORD-1", "CNT-1@before-production")),
    ("B6", "production_run_done", "the Console wrote method.keys.json and at least one .mdpeak",
     ("EXP-1", "CNT-1@after-run", "MTH-1")),
    ("B7", "mztab_validated", "the unit was finalised with an mzTab-M validation record and an mzTab-M exists",
     ("TAB-1", "TAB-2", "BIN-1", "FIN-1")),
    ("B8", "qa_produced", "a QA matrix (*.qa.tsv) exists", ("QA-1", "ORD-2")),
    ("B9", "publication_artifacts",
     "the publication report, Materials and Methods and supplementary table exist", ("LIB-1", "SEC-1")),
    ("B10", "retention_recorded", "the retention policy and the retained-artifact inventory are recorded",
     ("RET-1", "DSK-1")),
)


def completion_progress(workspace: Path, provenance: dict | None, output: Path) -> dict:
    """The furthest stage whose artifacts all exist, walking B1..B10 in order.

    Stages present beyond the first gap are listed as out of order: a publication bundle beside an
    unfinalised manifest is exactly the state FIN-1 exists for, and hiding it behind the gap would
    lose it.
    """
    record = provenance if isinstance(provenance, dict) else {}
    owner, _ = _raw_owner_manifest(record) if record else (None, "")
    candidates = [Path(str(item)) for item in record.get("input_candidates") or []]
    diagnostics = [item for item in record.get("peak_height_diagnostics") or [] if isinstance(item, dict)]
    proposal = _class_proposal(record) or {}
    facts = {
        "B1": bool(owner and owner.get("downloads")) and bool(candidates)
        and all(path.exists() for path in candidates),
        "B2": record.get("execution_allowed") is True,
        "B3": str(proposal.get("status") or "") == "accepted",
        "B4": bool(diagnostics)
        and Path(str(diagnostics[-1].get("diagnostic_run_directory") or "")).is_dir(),
        "B5": all((output / name).is_file() for name in ("analysis_files.csv", "method.txt", "run-manifest.json")),
        "B6": (output / "method.keys.json").is_file() and _mdpeak_count(output) > 0,
        "B7": bool(record.get("finalized_at")) and isinstance(record.get("mztab_validation"), dict)
        and any(output.glob("*.mzTab")),
        "B8": any(output.glob("*.qa.tsv")),
        "B9": all((output / name).is_file() for name in (
            "MS_DIAL_publication_report.json", "MS_DIAL_Materials_and_Methods.txt",
            "Supplementary_Table_MS_DIAL.tsv")),
        "B10": bool(record.get("raw_retention_policy"))
        and isinstance(record.get("retained_artifact_inventory"), list),
    }
    reached = "B0 none"
    next_stage = None
    for code, name, meaning, _checks in COMPLETION_STAGES:
        if not facts[code]:
            next_stage = (code, name, meaning)
            break
        reached = f"{code} {name}"
    gap = next_stage[0] if next_stage else None
    later = []
    if gap:
        seen_gap = False
        for code, name, _meaning, _checks in COMPLETION_STAGES:
            seen_gap = seen_gap or code == gap
            if seen_gap and code != gap and facts[code]:
                later.append(f"{code} {name}")
    return {
        "stage_reached": reached,
        "next_stage": f"{next_stage[0]} {next_stage[1]}" if next_stage else "",
        "next_stage_needs": next_stage[2] if next_stage else "",
        "present_out_of_order": later,
        "stages": facts,
    }


def _checks_by_stage(report: "Report") -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for code, _name, _meaning, keys in COMPLETION_STAGES:
        entries: dict[str, str] = {}
        for key in keys:
            check_id, _, stage = key.partition("@")
            for check in report.checks:
                if check.check_id == check_id and (not stage or check.stage == stage):
                    entries[key] = check.status
        if entries:
            result[code] = entries
    return result


def render(report: Report) -> str:
    lines = [f"workspace: {report.workspace}"]
    symbol = {PASS: "PASS", FAIL: "FAIL", WARN: "WARN", NOT_EVALUABLE: "----"}
    for check in report.checks:
        lines.append(f"[{symbol[check.status]}] {check.check_id} {check.title}")
        lines.append(f"        {check.detail}")
    counts = report.counts()
    lines.append("")
    lines.append("  ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    strict = report.strict_failures
    if strict:
        lines.append(
            "UNEVALUABLE ON ARTIFACTS THIS STAGE OWED: "
            + ", ".join(check.check_id for check in strict)
        )
    progress = getattr(report, "progress", None)
    if progress:
        line = f"PROGRESS (from artifacts, not a verdict): {progress['stage_reached']}"
        if progress["next_stage"]:
            line += f"; next {progress['next_stage']} needs {progress['next_stage_needs']}"
        if progress["present_out_of_order"]:
            line += f"; present out of order: {', '.join(progress['present_out_of_order'])}"
        lines.append(line)
    lines.append("VERDICT: " + ("ok" if report.ok else "REFUSE"))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workspace", help="the analysis-unit workspace directory")
    parser.add_argument("--stage", choices=(*STAGES, "all"), default="all")
    parser.add_argument("--json", action="store_true", help="emit the full report as JSON")
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "refuse (exit 4) when a check could not be evaluated because an artifact this stage "
            "was responsible for producing is absent. Without it, a workspace where nothing "
            "happened exits 0."
        ),
    )
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser()
    if not workspace.is_dir():
        print(f"not a directory: {workspace}", file=sys.stderr)
        return 3

    report = verify(workspace, args.stage)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(render(report))
    if not report.ok:
        return 2
    # A FAIL outranks a strict refusal, because a fact established beats a fact missing.
    if args.strict and report.strict_failures:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
