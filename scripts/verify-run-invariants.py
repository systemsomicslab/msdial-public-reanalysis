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

Exit codes: 0 all evaluated checks passed, 2 at least one failed, 3 the workspace is unusable.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

PASS = "pass"
FAIL = "fail"
WARN = "warn"
NOT_EVALUABLE = "not_evaluable"

STAGES = ("before-production", "after-run", "before-publish")


@dataclass
class Check:
    check_id: str
    stage: str
    title: str
    status: str
    detail: str
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "stage": self.stage,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "evidence": self.evidence,
        }


class Report:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.checks: list[Check] = []

    # Positional-only, so an evidence key may be named "status" or "detail" without colliding with
    # the parameters. Evidence keys come from the artifacts and are not ours to rename.
    def add(self, check_id: str, stage: str, title: str, status: str, detail: str, /, **evidence) -> Check:
        check = Check(check_id, stage, title, status, detail, evidence)
        self.checks.append(check)
        return check

    def counts(self) -> dict[str, int]:
        return dict(Counter(check.status for check in self.checks))

    @property
    def ok(self) -> bool:
        return not any(check.status == FAIL for check in self.checks)

    def as_dict(self) -> dict:
        return {
            "workspace": str(self.workspace),
            "ok": self.ok,
            "counts": self.counts(),
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
        report.add("PRE-1", stage, "Header-confirmed acquisition claim is permitted", NOT_EVALUABLE,
                   "No raw-header preflight is recorded in the manifest.")
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


def check_checksum_coverage(report: Report, provenance: dict | None, reason: str) -> None:
    """Every admitted input must have had its declared checksum verified.

    required is reported true when at least one file carried a checksum, so {"required": true,
    "verified": 1, "skipped": 29} reads to a boolean scan exactly like full coverage.
    """
    stage = "before-production"
    if provenance is None:
        report.add("SUM-1", stage, "Every input's checksum was verified", NOT_EVALUABLE, reason)
        return
    validation = provenance.get("allowlist_checksum_validation")
    candidates = provenance.get("input_candidates")
    if not isinstance(validation, dict) or not isinstance(candidates, list):
        report.add("SUM-1", stage, "Every input's checksum was verified", NOT_EVALUABLE,
                   "The manifest records no checksum validation block or no input candidates.")
        return
    verified = validation.get("verified")
    skipped = validation.get("skipped")
    expected = len(candidates)
    if verified == expected and skipped == 0:
        report.add("SUM-1", stage, "Every input's checksum was verified", PASS,
                   f"All {expected} inputs verified, none skipped.",
                   verified=verified, skipped=skipped, inputs=expected)
        return
    report.add(
        "SUM-1", stage, "Every input's checksum was verified", FAIL,
        "Checksum coverage is partial. The integrity claim in the published audit trail would "
        "overstate what was actually checked.",
        verified=verified, skipped=skipped, inputs=expected, required=validation.get("required"),
    )


# --------------------------------------------------------------------------------------------
# the sample-count invariant
# --------------------------------------------------------------------------------------------

def _mdpeak_count(output: Path) -> int:
    return len(list(output.glob("*.mdpeak")))


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


def check_storage_shape(report: Report, workspace: Path, stage: str) -> None:
    """Report what the unit actually occupies, against what a transfer figure would suggest."""
    if stage != "before-publish":
        return
    downloads = workspace / "raw" / "downloads"
    data = workspace / "raw" / "data"
    if not downloads.exists() and not data.exists():
        report.add("DSK-1", stage, "Retained storage is accounted for", NOT_EVALUABLE,
                   "No raw directory is present; the raw tree may already have been released.")
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
        check_execution_allowed(report, provenance, provenance_reason)
        check_preflight_claim(report, provenance, provenance_reason)
        check_checksum_coverage(report, provenance, provenance_reason)
        check_class_distribution(report, csv_rows, csv_reason, "before-production")
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

    if "before-publish" in stages:
        check_no_metric_rests_on_a_synthetic_order(report, output, csv_rows, "before-publish")
        check_library_provenance_contradiction(report, output, "before-publish")
        check_qa_prose_matches_assessment(report, output, "before-publish")
        check_storage_shape(report, workspace, "before-publish")
    return report


def render(report: Report) -> str:
    lines = [f"workspace: {report.workspace}"]
    symbol = {PASS: "PASS", FAIL: "FAIL", WARN: "WARN", NOT_EVALUABLE: "----"}
    for check in report.checks:
        lines.append(f"[{symbol[check.status]}] {check.check_id} {check.title}")
        lines.append(f"        {check.detail}")
    counts = report.counts()
    lines.append("")
    lines.append("  ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    lines.append("VERDICT: " + ("ok" if report.ok else "REFUSE"))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workspace", help="the analysis-unit workspace directory")
    parser.add_argument("--stage", choices=(*STAGES, "all"), default="all")
    parser.add_argument("--json", action="store_true", help="emit the full report as JSON")
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
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
