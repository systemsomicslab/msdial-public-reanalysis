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

    <unit-workspace>  the directory holding provenance/ and output/ for ONE analysis unit; a run's outputs
                      are read in the folder the manifest's output_directory names (output-run-<n> for a
                      new run of a finished unit), which must lie inside this directory
    --stage           before-production | after-run | before-publish | all   (default: all)
    --json            emit the full report as JSON on stdout

Exit codes: 0 no evaluated check failed, 2 at least one failed, 3 the workspace is unusable (not a
directory, or a run's output_directory outside it),
4 (--strict only) a check could not be evaluated because an artifact the stage owed is absent.
A WARN exits 0 and is for a person to read before publishing: QA-1's, for one, quotes every QA
sentence that is not Interactive's own statement for the assessment. Those sentences, and the
checksum phrasings SUM-2 sets aside, are also held by READ-1, which --strict refuses until a
person's reading of them is recorded with scripts/record-reading.py.

--strict exists because `ok` means "no check FAILed", and a workspace where nothing has happened
produces no FAILs at all. Run against a directory holding an empty provenance/ and an empty
output/, this file reported 15 not_evaluable, ok=True and exit 0, while the batch skill tells an
agent that exit 0 "means every evaluated check passed". A unit nobody ran and a unit that ran
correctly gave the same answer. Every unattended run must pass --strict.

Every before-production check carries a run_policy in --json: "blocks_run" where its FAIL stops a
campaign unit's MS-DIAL run, "record_only" where the FAIL is recorded and the unit runs (the user's
rule of 2026-10-01: only a check whose failure breaks the MS-DIAL results blocks the run; the user
placed every check on 2026-10-02). A blocks_run check left not evaluable on an artifact the stage owed
(strict_failures) stops the run as its FAIL does, and run_blocked_by names both. RUN_POLICY holds the
table, and each check's docstring says why it is classed as it is. A check of a later stage has no run
left to stop, and states no run_policy.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import html
import io
import json
import math
import os
import re
import sys
import unicodedata
import urllib.parse
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

PASS = "pass"
FAIL = "fail"
WARN = "warn"
NOT_EVALUABLE = "not_evaluable"

STAGES = ("before-production", "after-run", "before-publish")

# What a before-production FAIL does to a campaign unit's MS-DIAL run: the user's rule of 2026-10-01, and
# the user's placing of every check on 2026-10-02. A FAIL stops the run for the checks that break the
# results: ELIG-1, ACQ-1, SUM-1, CNT-1 and INP-1, which the rule named, and ID-1, PRE-2 and CONV-1, which
# the user placed with them. The unit then counts as failed, so it is retried twice and its raw data are
# deleted. Such a check left not evaluable on an artifact the stage owed stops the run as its FAIL does
# (Check.blocks_run); one not evaluable where the stage owed nothing never does. Any other FAIL is
# recorded and the unit runs: CLS-1, CLS-2, CLS-3, ORD-1 and PKH-1, which the rule named, and SPL-1,
# which the user placed with them; PRE-1, which never FAILs, records too, and so does PAIR-1 (2026-10-06),
# which lists every inferred name pairing and never FAILs. Each check's docstring gives
# its reason under "RUN POLICY". The campaign runner reads the class from each check's run_policy in
# --json, and holds the eight blocking ones itself whatever the report states.
BLOCKS_RUN = "blocks_run"
RECORD_ONLY = "record_only"
RUN_POLICY = {
    "ID-1": BLOCKS_RUN,
    "SPL-1": RECORD_ONLY,
    "ELIG-1": BLOCKS_RUN,
    "PRE-1": RECORD_ONLY,
    "PRE-2": BLOCKS_RUN,
    "ACQ-1": BLOCKS_RUN,
    "SUM-1": BLOCKS_RUN,
    "CONV-1": BLOCKS_RUN,
    "CLS-1": RECORD_ONLY,
    "CLS-2": RECORD_ONLY,
    "CLS-3": RECORD_ONLY,
    "PKH-1": RECORD_ONLY,
    "ORD-1": RECORD_ONLY,
    "INP-1": BLOCKS_RUN,
    "CNT-1": BLOCKS_RUN,
    "PAIR-1": RECORD_ONLY,
}

# A user-profile path inside an artifact built to be shared. Deliberately narrow: it
# matches what this machine actually leaks (a Windows profile path) rather than trying to
# recognise private data in general, which no pattern can do. SEC-1 refuses it wherever it is found.
PRIVATE_PATH_PATTERN = re.compile(
    r"[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}[^\\/\s\"\',;]+"
    r"|file:/{2,3}[A-Za-z]:/+Users/+[^\s\"\',;]+",
    re.IGNORECASE,
)
# Every other absolute path from this machine, in the forms the writers produce: raw, JSON-escaped,
# forward-slashed and percent-encoded. The production library is kept outside any user profile, so
# the pattern above never saw its location. These recognise locations, not private data. A SMILES
# string never starts with a bond and never puts a bond after "X:", so its "\" and "/" read as
# neither; the mzTab-M's structure columns are not read at all.
ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?:(?<![A-Za-z0-9])|(?<=%2F)|(?<=%5C)|(?<=%20))"
    r"[A-Za-z](?::|%3A)(?:\\\\|\\|/|%5C|%2F)(?=[^\s\"'<>|]*[A-Za-z0-9_])[^\s\"'<>|]*",
    re.IGNORECASE,
)
UNC_PATH_PATTERN = re.compile(
    r"(?:^|(?<=[\s\"'<>|;,=]))(?:\\\\\\\\|\\\\|//|%5C%5C)"
    r"[A-Za-z0-9][A-Za-z0-9._$-]{0,62}(?:\\\\|\\|/|%5C)[^\\/\s\"'<>|]+[^\s\"'<>|]*"
    r"|smb://[^\s\"'<>|]+",
    re.IGNORECASE | re.MULTILINE,
)
FILE_URI_PATTERN = re.compile(
    r"file(?::|%3A)(?:/|%2F){2,}(?=[^\s\"'<>|]*[A-Za-z0-9])[^\s\"'<>|]*",
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

    @property
    def run_policy(self) -> str | None:
        """What this check's FAIL does to the MS-DIAL run (RUN_POLICY); None after production, with no
        run left to stop. CNT-1, which runs at each stage, stops the run only from before-production."""
        return RUN_POLICY.get(self.check_id) if self.stage == "before-production" else None

    @property
    def blocks_run(self) -> bool:
        """Whether this check stops a campaign unit's run: a blocks_run check that FAILed, or that was left
        not evaluable on an artifact this stage owed (the user's decision of 2026-10-02: without what the
        stage owed, the check that would show the results broken has not been made). One not evaluable
        where the stage owed nothing, such as INP-1 for a unit that declares no analysis inputs, does not."""
        return self.run_policy == BLOCKS_RUN and (self.status == FAIL or self.strict_failure)

    def as_dict(self) -> dict:
        # A check after production states no run_policy at all. A reader that takes a stated policy it
        # does not know as blocking (the campaign runner's run_blocking_failures) would read a null as a
        # check that stops the run, and the after-run CNT-1, keyed like the before-production one, would
        # state over it.
        policy = {"run_policy": self.run_policy} if self.run_policy is not None else {}
        return {
            "check_id": self.check_id,
            "stage": self.stage,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "required": self.required,
            **policy,
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

    @property
    def run_blocked_by(self) -> list[str]:
        """The before-production checks whose run_policy is blocks_run and that stop the run, each once:
        each one that FAILed, and each one left not evaluable on an artifact this stage owed, which
        strict_failures names too (Check.blocks_run, the user's decision of 2026-10-02). A blocks_run check
        not evaluable where the stage owed nothing is not among them, and neither is a record_only FAIL."""
        return list(dict.fromkeys(check.check_id for check in self.checks if check.blocks_run))

    def as_dict(self) -> dict:
        return {
            "workspace": str(self.workspace),
            "ok": self.ok,
            "counts": self.counts(),
            "strict_failures": [check.check_id for check in self.strict_failures],
            "run_blocked_by": self.run_blocked_by,
            "progress": getattr(self, "progress", None),
            "checks_by_stage": _checks_by_stage(self),
            "checks": [check.as_dict() for check in self.checks],
        }


def _read_json(path: Path) -> tuple[dict | None, str]:
    """Return the parsed object, or None with the reason it could not be read."""
    if not path.exists():
        return None, f"{path.name} is absent"
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        # RecursionError: a document nested deeper than the parser's stack, which is not a record.
        return None, f"{path.name} could not be read: {type(exc).__name__}: {exc}"
    if not isinstance(parsed, dict):
        # Every record this gate reads is an object. A list or a bare value would otherwise reach a
        # check as if it were one and end the gate with a traceback, which is not a verdict.
        return None, f"{path.name} is not a JSON object"
    return parsed, ""


def _read_csv_rows(path: Path) -> tuple[list[dict] | None, str]:
    if not path.exists():
        return None, f"{path.name} is absent"
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle)), ""
    except (OSError, ValueError, csv.Error) as exc:  # csv.Error: a field past csv.field_size_limit
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

    RUN POLICY: blocks_run, as the user placed it (2026-10-02). Every other check reads this manifest
    as the unit's, so where it names another unit, or only an accession, the checks that stop the run
    hold the CSV to that other record's inputs and samples, and what MS-DIAL computes here is recorded
    in the campaign record and the Catalog's run under a unit it may not describe. Results that cannot
    be attributed to the unit the directory names are not that unit's results.
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

    RUN POLICY: blocks_run, as the user named it (2026-10-01). A unit judged ineligible, unresolved or
    split is not a run whose results mean anything.
    """
    stage = "before-production"
    if provenance is None:
        report.add("ELIG-1", stage, "Manifest permits execution", NOT_EVALUABLE, reason)
        return
    allowed = provenance.get("execution_allowed")
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

    RUN POLICY: record_only, as the user placed it (2026-10-02). It governs what may be claimed about
    the headers, never what the Console computes, and it never FAILs.
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


# Interactive's raw_metadata_extractor.PINNED_BUILDS, the approved (msrawdataworkbench, MsdialWorkbench)
# commit pairs of the raw-metadata extractor, newest first: a "built" pair is one a campaign may run, a
# "planned" one was approved and never built. Mirrored rather than imported, because the gate judges what
# Interactive recorded without running Interactive's code, and in one place only;
# tests/test_verify_extractor_and_release.py holds it equal to the table at EXTRACTOR_PINS_MIRRORED_FROM.
EXTRACTOR_PINS_MIRRORED_FROM = "c9ef2f6"
EXTRACTOR_RAW_TREE = "msrawdataworkbench"
EXTRACTOR_COMMON_TREE = "MsdialWorkbench"
EXTRACTOR_PIN_BUILT = "built"
EXTRACTOR_PINNED_BUILDS = (
    {"msrawdataworkbench": "5f604462d7bd61141bf764ca04ce52bf3f6452c9",
     "MsdialWorkbench": "f0583493a44e73723f53ae312e33955f62052dd7", "state": "built"},
    {"msrawdataworkbench": "a12293c612a4e29b23d1d584f1c19556d76863f6",
     "MsdialWorkbench": "f0583493a44e73723f53ae312e33955f62052dd7", "state": "built"},
    {"msrawdataworkbench": "592b6dbce72177fa14d3e7cd407557b1c64a3046",
     "MsdialWorkbench": "f0583493a44e73723f53ae312e33955f62052dd7", "state": "built"},
    {"msrawdataworkbench": "b34c857a5328e8f08c1918b3d890e7dae50b7d6d",
     "MsdialWorkbench": "c471463a576626650e0886e26bd064cca53a7ae3", "state": "planned"},
)
# What inspect_raw_metadata_extractor calls a build whose record no longer describes it: the record
# names other files than the ones that ran, or source trees with uncommitted changes.
EXTRACTOR_STALE = "stale_mismatch"
EXTRACTOR_DIRTY = "dirty_source"
EXTRACTOR_VERIFIED = "verified"
PRE2_TITLE = "The raw headers were read by a verified, pinned extractor"


def _extractor_pin(raw_commit: object, common_commit: object) -> dict | None:
    raw, common = str(raw_commit or "").strip().casefold(), str(common_commit or "").strip().casefold()
    return next((dict(entry) for entry in EXTRACTOR_PINNED_BUILDS
                 if entry[EXTRACTOR_RAW_TREE] == raw and entry[EXTRACTOR_COMMON_TREE] == common), None)


def _campaign_crossings(provenance: dict | None) -> list[dict]:
    """The campaign approvals the unit crossed a boundary under: its own, or else its raw owner's."""
    record = provenance if isinstance(provenance, dict) else {}
    own = record.get("campaign_authorizations")
    own = [item for item in own if isinstance(item, dict)] if isinstance(own, list) else []
    if own or not isinstance(record.get("split_from"), dict):
        return own
    owner, _ = _raw_owner_manifest(record)
    crossings = (owner or {}).get("campaign_authorizations")
    return [item for item in crossings if isinstance(item, dict)] if isinstance(crossings, list) else []


def _boundaries(crossings: list[dict]) -> str:
    """"boundary 4", or "boundaries 3, 4": the boundaries these crossings were made at."""
    named = list(dict.fromkeys(str(item.get("boundary")) for item in crossings))
    return ("boundaries " if len(named) > 1 else "boundary ") + ", ".join(named)


def _preflight_campaign(provenance: dict | None) -> dict:
    """Whether a campaign acts on the raw-header preflight's verdicts, and on what record that rests.

    Interactive states it itself, in campaign_disposition.applied, which it sets as it decides the
    disposition: true when a campaign was in force (an approval passed, or one already recorded for the
    unit or its split parent), false otherwise. It is also true for a read made before the campaign whose
    disposition classify_preflight applied once the unit became a campaign unit: the campaign acts on
    that read all the same. A unit preflighted outside a campaign and adopted by one later carries
    applied false, and the crossings it gained afterwards say nothing about the read. Only a unit with
    no disposition - a split parent, which gets none - is judged from its crossings, and then from those
    validated at or before the preflight started; one whose order against the preflight is not recorded
    counts, because nothing shows that the read came first.
    """
    record = provenance if isinstance(provenance, dict) else {}
    preflight = record.get("raw_metadata_preflight") if isinstance(record.get("raw_metadata_preflight"), dict) else {}
    crossings = _campaign_crossings(record)
    approvals = list(dict.fromkeys(str(item.get("approval_id") or "") for item in crossings))
    disposition = record.get("campaign_disposition")
    if isinstance(disposition, dict) and isinstance(disposition.get("applied"), bool):
        if disposition["applied"]:
            campaign = disposition.get("campaign") if isinstance(disposition.get("campaign"), dict) else {}
            approval = str(campaign.get("approval_id") or (approvals[0] if approvals else "")) or "unnamed"
            return {"under": True, "basis": "disposition_applied", "later": [],
                    "said": f"Campaign approval {approval} applied the disposition decided from these reads"}
        return {"under": False, "basis": "disposition_advice", "later": crossings,
                "said": "Interactive recorded the disposition decided from these reads as advice (applied false), "
                        "so no campaign was in force when it was decided"}
    started = _instant(preflight.get("started_at")) if preflight.get("started_at") else None
    before, unordered, later = [], [], []
    for item in crossings:
        validated = _instant(item.get("validated_at")) if item.get("validated_at") else None
        if started is None or validated is None:
            unordered.append(item)
        elif validated <= started:
            before.append(item)
        else:
            later.append(item)
    if before:
        return {"under": True, "basis": "crossing_before_preflight", "later": later,
                "said": f"Campaign approval {before[0].get('approval_id') or 'unnamed'} was recorded for the unit "
                        f"({_boundaries(before)}) before this preflight started"}
    if unordered:
        return {"under": True, "basis": "crossing_unordered", "later": later,
                "said": f"Campaign approval {unordered[0].get('approval_id') or 'unnamed'} is recorded for the unit "
                        f"({_boundaries(unordered)}), and nothing records that this preflight came before it"}
    return {"under": False, "basis": "crossing_after_preflight" if later else "no_campaign", "later": later,
            "said": "No campaign approval was recorded for the unit when this preflight started"}


def _recorded_extractor(provenance: dict | None) -> dict:
    preflight = (provenance or {}).get("raw_metadata_preflight") if isinstance(provenance, dict) else None
    extractor = preflight.get("extractor") if isinstance(preflight, dict) else None
    return extractor if isinstance(extractor, dict) else {}


def check_extractor_identity(report: Report, provenance: dict | None, reason: str) -> None:
    """PRE-2. The build that read the raw headers is one whose source is known.

    The extractor decides a unit's acquisition mode, polarity and separation, and with them whether it
    runs, splits or is skipped, and a campaign deletes the raw data afterwards. Until Interactive 0.5.17 a
    preflight recorded the extractor by path, size and modification time, and the binary in use had been
    built from a working checkout with uncommitted changes: its verdicts named no code. Since then each
    preflight records the extractor's sha256, its build record's verdict (provenance_status) and the pair
    of commits the record names, and a campaign runs only a verified build of a pinned pair.

    No sha256 is a legacy record: WARN. A record Interactive itself called stale or dirty names code that
    did not run: FAIL. A verified build of a pair EXTRACTOR_PINNED_BUILDS lists as built: PASS. Anything
    else - no build record, or a pair not pinned - is a WARN when the read was made outside a campaign and
    a FAIL when a campaign acts on it, because Interactive refuses such an extractor to a campaign
    preflight, so a campaign acting on one's verdicts is outside the rule. Whether a campaign acts on the
    read is what the unit's disposition recorded when it was decided (_preflight_campaign), not whatever
    crossings the unit carries now: a unit adopted by a campaign after its preflight did not have it read
    under one. A pair Interactive recorded as pinned and this mirror does not list is a WARN: the mirror
    may be behind Interactive, which is not a fact about the unit.

    RUN POLICY: blocks_run, as the user placed it (2026-10-02). Its FAIL reaches the results without
    showing that it did: ACQ-1 holds every row to the header verdict, and a FAIL here says that the code
    that gave the verdict did not run as recorded, or that a campaign acted on a read the pinned
    extractor did not make, so the acquisition types and the polarity the results rest on rest on a
    verdict no other check can question. A file deconvoluted as the wrong type completes, validates and
    is wrong, as ACQ-1 says of its own FAIL.
    """
    stage = "before-production"
    if provenance is None:
        report.add("PRE-2", stage, PRE2_TITLE, NOT_EVALUABLE, reason)
        return
    preflight = provenance.get("raw_metadata_preflight")
    if not isinstance(preflight, dict) or not preflight:
        # Not required, as for PRE-1: a unit whose acquisition mode the repository already settled never
        # needed a header read, so no extractor decided anything here.
        report.add("PRE-2", stage, PRE2_TITLE, NOT_EVALUABLE,
                   "No raw-header preflight is recorded in the manifest, so no extractor ran.", required=False)
        return
    extractor = _recorded_extractor(provenance)
    sha256 = str(extractor.get("sha256") or "").strip().casefold()
    status = str(extractor.get("provenance_status") or "").strip()
    raw_commit = str(extractor.get("msrawdataworkbench_commit") or "").strip()
    common_commit = str(extractor.get("msdialworkbench_commit") or "").strip()
    pin = _extractor_pin(raw_commit, common_commit)
    campaign = _preflight_campaign(provenance)
    # Checksums and commits only: the path is this machine's, and the gate's report may travel.
    evidence = {
        "sha256": sha256, "inventory_sha256": str(extractor.get("inventory_sha256") or ""),
        "provenance_status": status, "msrawdataworkbench_commit": raw_commit,
        "msdialworkbench_commit": common_commit, "recorded_pinned": extractor.get("pinned"),
        "recorded_pin_state": str(extractor.get("pin_state") or ""),
        "gate_pin_state": pin["state"] if pin else "", "pins_mirrored_from": EXTRACTOR_PINS_MIRRORED_FROM,
        "under_campaign": campaign["under"], "campaign_basis": campaign["basis"],
        "boundaries_crossed_after_preflight": [str(item.get("boundary")) for item in campaign["later"]],
    }
    if not sha256:
        report.add(
            "PRE-2", stage, PRE2_TITLE, WARN,
            ("The preflight records its extractor by path, size and modification time only, as Interactive did "
             "before 0.5.17" if extractor else "The preflight records no extractor at all")
            + ", so which build read these headers, and from which source, is not established. Its verdicts are "
            "not tied to code.", **evidence)
        return
    if not _SHA256_TEXT.fullmatch(sha256):
        report.add("PRE-2", stage, PRE2_TITLE, WARN,
                   f"The preflight records an extractor checksum that is not a sha256 ({sha256[:24]!r}), so the build "
                   "that read these headers is not established.", **evidence)
        return
    pair = (f"msrawdataworkbench {raw_commit[:9]} with MsdialWorkbench {common_commit[:9]}"
            if raw_commit and common_commit else "no recorded commits")
    if status == EXTRACTOR_STALE:
        report.add(
            "PRE-2", stage, PRE2_TITLE, FAIL,
            f"The extractor that read these headers (sha256 {sha256[:12]}) was stale when it ran: its build record "
            f"describes other files than the ones on disk, so {pair} is not the code that decided this unit's "
            "acquisition mode.", **evidence)
        return
    if status == EXTRACTOR_DIRTY:
        report.add(
            "PRE-2", stage, PRE2_TITLE, FAIL,
            f"The extractor that read these headers (sha256 {sha256[:12]}) was built from source trees with "
            f"uncommitted changes, so {pair} does not describe the code that decided this unit's acquisition "
            "mode.", **evidence)
        return
    notes: list[str] = []
    if isinstance(provenance.get("split_from"), dict):
        owner, _ = _raw_owner_manifest(provenance)
        parent_sha = str(_recorded_extractor(owner).get("sha256") or "").strip().casefold()
        evidence["parent_sha256"] = parent_sha
        if parent_sha and parent_sha != sha256:
            notes.append(f"This part's headers were read by extractor {sha256[:12]}, and the split was decided from "
                         f"its parent's read by {parent_sha[:12]}: the split and the part's verdicts come from two "
                         "different builds.")
    disposition = provenance.get("campaign_disposition")
    decided = disposition.get("extractor") if isinstance(disposition, dict) else None
    decided_by = str((decided or {}).get("sha256") or "").strip().casefold() if isinstance(decided, dict) else ""
    if decided_by and decided_by != sha256:
        evidence["disposition_sha256"] = decided_by
        notes.append(f"The campaign disposition names extractor {decided_by[:12]}, not the {sha256[:12]} the "
                     "preflight records.")
    tail = (" " + " ".join(notes)) if notes else ""
    built = bool(pin and pin["state"] == EXTRACTOR_PIN_BUILT)
    if status == EXTRACTOR_VERIFIED and built:
        current = EXTRACTOR_PINNED_BUILDS[0]
        report.add(
            "PRE-2", stage, PRE2_TITLE, WARN if notes else PASS,
            f"The headers were read by extractor {sha256[:12]}, a verified build of {pair}, a pinned pair"
            + ("" if pin == current else f" though not the current pin ({current[EXTRACTOR_RAW_TREE][:9]})")
            + "." + tail, **evidence)
        return
    if status == EXTRACTOR_VERIFIED and extractor.get("pinned") is True:
        report.add(
            "PRE-2", stage, PRE2_TITLE, WARN,
            f"Interactive recorded extractor {sha256[:12]} as a verified build of the pinned pair {pair}, and the "
            f"gate's mirror of PINNED_BUILDS (Interactive {EXTRACTOR_PINS_MIRRORED_FROM}) does not list that pair as "
            "built: the mirror is behind Interactive, or the record is wrong." + tail, **evidence)
        return
    if status == EXTRACTOR_VERIFIED:
        why = f"a verified build of {pair}, a pair " + ("that was planned and never built" if pin else "no pin names")
    else:
        why = (f"identified by its checksum alone: its build record was {status or 'not recorded'}, so no source "
               "revision names it")
    if campaign["under"]:
        report.add(
            "PRE-2", stage, PRE2_TITLE, FAIL,
            f"The headers were read by extractor {sha256[:12]}, {why}. {campaign['said']}. A campaign acts only on "
            "the reads of a verified build of a pinned pair, and Interactive refuses any other extractor to a "
            "campaign preflight, so these verdicts reached the campaign outside the rule." + tail, **evidence)
        return
    adopted = ""
    if campaign["later"]:
        adopted = (f" {campaign['said']}; the unit crossed {_boundaries(campaign['later'])} under campaign "
                   f"approval {campaign['later'][0].get('approval_id') or 'unnamed'} after it, and a crossing made "
                   "after the read does not make it a campaign's read.")
    report.add(
        "PRE-2", stage, PRE2_TITLE, WARN,
        f"The headers were read by extractor {sha256[:12]}, {why}. Outside a campaign that is allowed, but its "
        "verdicts are not tied to reviewed code." + adopted + tail, **evidence)


def _raw_owner_manifest(provenance: dict | None) -> tuple[dict | None, str]:
    """The manifest of the unit that downloaded this unit's raw data.

    That is the unit itself, except for a part split from another unit: a part downloaded nothing,
    and its inputs were verified, or not, when its parent was downloaded. A part whose split record
    names no parent manifest has an unknown owner; it never falls back to its own record, which
    holds no download to judge.
    """
    if not isinstance(provenance, dict):
        return None, ""
    split_from = provenance.get("split_from")
    if isinstance(split_from, dict):
        if not str(split_from.get("manifest_path") or "").strip():
            return None, "split_from names no manifest_path"
        return _read_json(Path(str(split_from["manifest_path"])))
    return provenance, ""


def _same_path(left: object, right: object) -> bool:
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


def _path_key(value: object) -> str:
    return os.path.normcase(os.path.normpath(str(value)))


# Repositories recorded as publishing no checksum for any file. For these, and only these, a unit
# whose record holds no checksum is not a declaration lost on the way in. MetaboLights builds every
# file entry without one; Metabolomics Workbench, MetaboBank and MB-POST publish them.
REPOSITORIES_WITHOUT_CHECKSUMS = frozenset({"metabolights"})


# What an archive or a packed file is named with, as Interactive's archives.py opens them, longest
# first so ".tar.gz" wins over ".gz" where a suffix is stripped.
ARCHIVE_SUFFIXES = tuple(sorted(
    (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tbz", ".tar.xz", ".txz", ".zip", ".tar", ".7z", ".rar", ".gz",
     ".bz2", ".xz", ".lzma"), key=len, reverse=True))
# How a basis names the algorithm of a published checksum, as Interactive's statements do.
ALGORITHM_NAMES = {"md5": "MD5", "sha1": "SHA-1", "sha256": "SHA-256"}


def _declared_name(value: object) -> str:
    """A file-list name as the Interactive resolves it: separators unified, a leading FILES/ dropped."""
    name = str(value or "").replace("\\", "/").lstrip("/")
    if name.casefold().startswith("files/"):
        name = name[6:]
    return name.casefold()


def _relative_name(path_text: object, root_text: object) -> str:
    """A path's name relative to the unit's input directory, in the form _declared_name produces."""
    path = Path(os.path.normpath(str(path_text)))
    try:
        return path.relative_to(Path(os.path.normpath(str(root_text)))).as_posix().casefold()
    except ValueError:
        return path.name.casefold()


def _name_forms(relative: str) -> tuple[str, ...]:
    """The names a declared file may carry for this input, as the Interactive resolves them.

    The Interactive's validator and allow-list compare a declared name with the input's path relative
    to the data directory, or with that path less its first component (the folder an archive unpacks
    into, such as MB-POST_files_MPST000007.0). Nothing deeper: a declared a.lcd does not vouch for
    neg/a.lcd, which is a different file the validator never checked.
    """
    parts = relative.split("/")
    return (relative,) if len(parts) < 2 else (relative, "/".join(parts[1:]))


def _names_cover(relative: str, declared: str) -> bool:
    return declared in _name_forms(relative)


# ---- The input lineage ----------------------------------------------------------------------------
# Interactive writes one row per analysis input into the manifest's input_lineage table: what the
# input is and where its bytes came from. SUM-1 used to answer that question from its own reading of
# downloads, extracted_files and the allow-list, and could not follow an input through an archive or
# a conversion at all: no clause let a verified Workbench archive vouch for what came out of it, so
# every one of the 661 declared-pool Workbench units was refused. A manifest that carries the table is
# judged by it; one written before it existed keeps the reading below it, unchanged.
INPUT_LINEAGE_SCHEMA = "msdial-input-lineage.v1"
# What archives.py writes to provenance/archive-members-<sha12>.tsv for the whole lineage of one
# archive, nested archives included, each path relative to the outermost archive's destination.
ARCHIVE_LISTING_COLUMNS = ("path", "type", "size", "crc32", "modified", "depth", "archive", "member_name",
                           "disposition")
# How strongly each basis vouches for an input, weakest first. A unit rests on its weakest input.
CHECKSUM_BASES = ("download_sha256", "archive_verified", "verified")
# An input extracted from an archive whose published MD5 matched at download, and named in the
# archive's recorded listing, was never compared with a checksum of its own. Whether that earns a PASS
# is the user's decision, not yet taken (2026-09-30); until then it is a WARN, which exits 0 like a
# PASS, so the Workbench units proceed either way. Changing this constant is that decision.
ARCHIVE_VERIFIED_STATUS = WARN
ARCHIVE_WORDING_TEXT = "extracted from an archive whose published MD5 matched"


def _archive_wording(algorithms: list[str]) -> str:
    """The permitted wording, naming the algorithm compared as Interactive's statement does (SHA-256)."""
    if algorithms in ([], ["MD5"]):
        return ARCHIVE_WORDING_TEXT
    return f"extracted from an archive whose published {' or '.join(algorithms)} matched"


@dataclass
class _Cover:
    """What vouches for one input's bytes: a basis from CHECKSUM_BASES, or the reason nothing does."""

    basis: str = ""
    reason: str = ""
    detail: str = ""
    # The file's own sha256 and size where a record gives them, for a conversion to be compared with,
    # and the declared checksum that was compared with it, with its algorithm.
    sha256: str = ""
    size: int | None = None
    declared: str = ""
    algorithm: str = ""
    archive: str = ""
    # The algorithm of the published checksum that archive was compared with.
    archive_algorithm: str = ""
    crc_verified: object = None
    converted: bool = False


def _uncovered(reason: str, detail: str) -> _Cover:
    return _Cover(reason=reason, detail=detail)


def _claimed_checksum(checksums: dict) -> str:
    """The declared checksum a lineage row says was verified, or "" where it says none was."""
    if checksums.get("declared_verified") is True:
        return str(checksums.get("declared") or "").strip().casefold()
    return ""


def _weakest(covers: list[_Cover]) -> _Cover:
    return min(covers, key=lambda cover: CHECKSUM_BASES.index(cover.basis))


@dataclass
class _ArchiveListing:
    files: dict = field(default_factory=dict)
    directories: set = field(default_factory=set)
    problem: str = ""
    # Each extracted file's path as the listing spells it, by its casefolded key in files.
    names: dict = field(default_factory=dict)


class _InputLineage:
    """The manifest's input_lineage table, resolved input by input to what vouches for its bytes.

    Each row is read by its kind, and every basis is checked against the primary record it names,
    never against the row alone. A row's word that its declared checksum was verified is one such
    basis, and holds only where a record bears it out: the download of the input itself, which
    compared that very value; or the allow-list validator, whose count accounts for every checksum the
    unit declares, and which compared this value because the unit declares it for a file of this
    input's name. A member's claim to its archive's own checksum is refused: that checksum vouches for
    the archive, and through it for the member only by the archive basis below.

    - file: a repository object downloaded as itself, at the row's own path. Its own declared checksum,
      verified at download or by the allow-list validator ("verified"); or, for a repository that
      publishes none and a unit that declares none, the sha256 recorded at download
      ("download_sha256").
    - extracted_member and archived_container: out of an archive. Its own declared checksum where one
      was compared ("verified"). Otherwise the archive's: the download record must hold the archive's
      sha256, its published checksum must have matched ("archive_verified"; for a repository
      publishing none, "download_sha256"), an archive_extractions record for exactly those bytes must
      list the input in its member listing, whose sha256 is recorded, and must have rejected no member.
      Where the download itself vouches for nothing, an archive expanded inside it that holds the input
      and was compared with its own published checksum before it expanded does (_verified_inner).
    - vendor_folder: a .d or .raw directory assembled from objects downloaded one by one, each of
      which must be covered as a file is.
    - converted: written by the mzXML conversion. Covered iff a completed conversion record in
      input_conversions (msdial-mzxml-conversion.v1 records, as a list or as an object's "records")
      names it as its output, with the output's sha256; the row's
      source.conversion.source_sha256 is the sha256 that record read; and the mzXML it read is itself
      covered, through the table's own row for that mzXML, or source.conversion.source_row when the
      table has none (a carried row must describe that mzXML, and be the table's row where there is
      one), otherwise through the download or the archive listing that holds it. The record's hashes
      must agree with what the cover knows of those bytes: the sha256 of a download, the declared
      checksum a validator compared where the record carries that algorithm, and, for an archive
      member, which is not hashed, the listed size. The mzXML's basis is the converted input's. Whether
      the mzML is still the bytes its record gives, and passed its validation, is CONV-1's.

    An input with no row, or with two different rows, is covered by nothing.
    """

    def __init__(self, owner: dict) -> None:
        self.owner = owner
        self.problem = ""
        lineage = owner.get("input_lineage")
        rows = lineage.get("rows") if isinstance(lineage, dict) else None
        if not isinstance(lineage, dict) or lineage.get("schema") != INPUT_LINEAGE_SCHEMA or not isinstance(rows, list):
            self.problem = (f"The manifest's input_lineage is not an {INPUT_LINEAGE_SCHEMA} table with rows, so "
                            "nothing says where any input came from.")
            rows = []
        self.rows: dict[str, list[dict]] = {}
        for row in rows:
            if isinstance(row, dict) and str(row.get("path") or "").strip():
                self.rows.setdefault(_path_key(row["path"]), []).append(row)
        self.downloads = [item for item in owner.get("downloads") or [] if isinstance(item, dict)]
        self.downloads_by_path = {_path_key(item.get("path")): item for item in self.downloads
                                  if str(item.get("path") or "").strip()}
        self.downloads_by_sha256 = {str(item.get("sha256")).casefold(): item for item in self.downloads
                                    if str(item.get("sha256") or "").strip()}
        project = owner.get("project") or {}
        self.repository = str(project.get("repository") or "").strip().casefold()
        files = [item for item in project.get("files") or [] if isinstance(item, dict)]
        declared = [item for item in files if str(item.get("checksum") or "").strip()]
        self.unit_declares_checksums = bool(declared) or any(
            str(item.get("declared_checksum") or "").strip() for item in self.downloads)
        # Each checksum the unit declares, by the name of the file it declares it for.
        self.declared_files = [(_declared_name(item.get("name")), str(item.get("checksum")).strip().casefold())
                               for item in declared]
        validation = owner.get("allowlist_checksum_validation")
        verified = validation.get("verified") if isinstance(validation, dict) else None
        at_download = validation.get("archives_verified_at_download") if isinstance(validation, dict) else None
        # An archive's declared checksum is compared with its download and counted apart, where a lease
        # records that count; it is never more than the declared names that are archives.
        archives = sum(1 for name, _ in self.declared_files if name.endswith(ARCHIVE_SUFFIXES))
        at_download = min(at_download, archives) if isinstance(at_download, int) and at_download > 0 else 0
        # The validator raises on a mismatch or on a name it cannot resolve, so a count that accounts for
        # every declared checksum means each one was compared with the file it names. Anything less, and
        # nothing says which of them were compared.
        self.validator_accounts = (bool(declared) and isinstance(verified, int)
                                   and verified + at_download == len(declared))
        self.declared_names = {name for name, _ in self.declared_files} if self.validator_accounts else set()
        self.input_directory = str(owner.get("input_directory") or "")
        raw_extracted = owner.get("extracted_files")
        self.extracted = {_path_key(item) for item in raw_extracted} if isinstance(raw_extracted, list) else set()
        raw_records = owner.get("archive_extractions")
        # None: the lease predates the extraction record, and extracted a MetaboLights archive as the
        # legacy reading below assumes. A present block is judged by what it records.
        self.extraction_records = (
            None if raw_records is None
            else [item for item in raw_records if isinstance(item, dict)] if isinstance(raw_records, list) else []
        )
        self.extractions = {str(item.get("archive_sha256")).casefold(): item
                            for item in self.extraction_records or [] if str(item.get("archive_sha256") or "").strip()}
        raw_conversions = owner.get("input_conversions")
        if isinstance(raw_conversions, dict):
            raw_conversions = raw_conversions.get("records")
        self.conversions = {}
        for record in raw_conversions if isinstance(raw_conversions, list) else []:
            output = record.get("output") if isinstance(record, dict) else None
            if isinstance(output, dict) and str(output.get("path") or "").strip():
                self.conversions.setdefault(_path_key(output["path"]), []).append(record)
        self._listings: dict[str, _ArchiveListing] = {}
        self._under: dict[str, list[dict]] | None = None

    def resolve_input(self, path: str) -> _Cover:
        rows = self.rows.get(_path_key(path)) or []
        if not rows:
            return _uncovered("no_lineage_row", "the lineage table has no row for it")
        if any(row != rows[0] for row in rows[1:]):
            return _uncovered("ambiguous_lineage_row", f"the lineage table has {len(rows)} different rows for it")
        return self.resolve_row(rows[0], 0)

    def resolve_row(self, row: dict, depth: int) -> _Cover:
        kind = str(row.get("kind") or "")
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        checksums = row.get("checksums") if isinstance(row.get("checksums"), dict) else {}
        path = str(row.get("path") or "")
        if kind == "file":
            return self._file(path, source, checksums)
        if kind == "extracted_member":
            archive = source.get("archive")
            if _claimed_checksum(checksums):
                return self._claimed_member(path, archive if isinstance(archive, dict) else {}, checksums)
            if not isinstance(archive, dict):
                return _uncovered("no_archive_source", "its row names no archive it came out of")
            return self._from_archive(path, archive, str(source.get("member") or ""), directory=False)
        if kind == "archived_container":
            archives = [item for item in source.get("archives") or [] if isinstance(item, dict)] \
                if isinstance(source.get("archives"), list) else []
            if not archives:
                return _uncovered("no_archive_source", "its row names no archive it came out of")
            covers = [self._from_archive(path, archive, "", directory=True) for archive in archives]
            refused = next((cover for cover in covers if not cover.basis), None)
            return refused or _weakest(covers)
        if kind == "vendor_folder":
            return self._vendor_folder(path, checksums)
        if kind == "converted":
            return self._converted(path, source, checksums, depth)
        return _uncovered("unknown_lineage_kind", f"its lineage kind {kind or 'none'!r} is not one the gate reads")

    def _declaration(self, download: dict, *, archive: bool) -> _Cover:
        """What a downloaded object's own declaration earns: verified, unverified, none published, or lost."""
        what = "archive it came out of" if archive else "file"
        if str(download.get("declared_checksum") or "").strip():
            if download.get("declared_checksum_verified") is True:
                return _Cover("archive_verified" if archive else "verified")
            if archive:
                return _uncovered("archive_md5_unverified",
                                  f"the {what} declares a checksum that was not verified at download")
            return _uncovered("declared_checksum_unverified", "its declared checksum was not verified")
        if self.repository in REPOSITORIES_WITHOUT_CHECKSUMS:
            if self.unit_declares_checksums:
                return _uncovered("partial_declaration",
                                  f"the {what} declares no checksum while other files of this unit do")
            return _Cover("download_sha256")
        return _uncovered("declaration_lost",
                          f"the {what} declares no checksum, and {self.repository or 'its repository'} is not "
                          "recorded as publishing none: the declaration was lost on the way in")

    def _download_sha256(self, download: dict) -> str:
        return str(download.get("sha256") or "").strip().casefold()

    def _file(self, path: str, source: dict, checksums: dict) -> _Cover:
        # Interactive writes a file row only for an input that is itself a download, at the same path.
        # A row naming another download as its own is a lineage that is wrong, and borrows that
        # object's checksums for bytes it never vouched for.
        named = str(source.get("download_path") or "").strip()
        if named and not _same_path(named, path):
            return _uncovered("download_path_mismatch",
                              f"its row names the download of {Path(named).name} as its own")
        download = self.downloads_by_path.get(_path_key(path))
        if download is None:
            reused = source.get("origin") == "not_downloaded_by_this_lease"
            return _uncovered("not_downloaded", "it is not a recorded download"
                              + (" (the lease did not download it)" if reused else ""))
        sha256 = self._download_sha256(download)
        if not sha256:
            return _uncovered("no_download_sha256", "no sha256 was recorded when it was downloaded")
        recorded = str(checksums.get("sha256") or "").strip().casefold()
        if recorded and recorded != sha256:
            return _uncovered("sha256_disagrees", "its lineage row and its download record give different sha256")
        claimed = _claimed_checksum(checksums)
        if claimed:
            cover = self._confirmed_claim(path, claimed, checksums, download=download)
            if not cover.basis:
                return cover
        else:
            cover = self._declaration(download, archive=False)
        cover.sha256 = sha256
        size = download.get("size_bytes")
        cover.size = size if isinstance(size, int) else None
        return cover

    def _confirmed_claim(self, path: str, claimed: str, checksums: dict, *, download: dict | None = None) -> _Cover:
        """A row's word that its declared checksum was verified, held against the record that did it.

        The download of the input itself compared its declared MD5, and records whether it matched.
        The allow-list validator compared every checksum the unit declares with the file of that name,
        and records only how many; so its word counts for a checksum the unit declares for a file of
        this input's name, and only while that count accounts for all of them.
        """
        if (download is not None and download.get("declared_checksum_verified") is True
                and str(download.get("declared_checksum") or "").strip().casefold() == claimed):
            # The download loop compares a declared value with the MD5 it computed, and nothing else.
            return _Cover("verified", declared=claimed, algorithm="md5")
        relative = _relative_name(path, self.input_directory) if self.input_directory else Path(path).name.casefold()
        if not any(checksum == claimed and _names_cover(relative, name) for name, checksum in self.declared_files):
            return _uncovered("declared_checksum_unknown", "the checksum its row says was verified is not one the "
                              "repository declared for a file of its name")
        if not self.validator_accounts:
            return _uncovered("declared_checksum_unaccounted",
                              "its row says its declared checksum was verified, but the allow-list validator's "
                              "count does not account for every checksum the unit declares")
        return _Cover("verified", declared=claimed,
                      algorithm=str(checksums.get("declared_algorithm") or "").strip().casefold())

    def _claimed_member(self, path: str, archive: dict, checksums: dict) -> _Cover:
        """A member of an archive whose row says its own declared checksum was verified."""
        claimed = _claimed_checksum(checksums)
        # The archive's own checksums: what was computed as it arrived, and a declared value only where
        # it was verified as the archive's. MB-POST's tar carries the first member's declared checksum,
        # never compared with the tar, and that member's claim to it is its own.
        own = set()
        download = self._archive_download(archive)
        for record in (archive, download or {}):
            own.update(str(record.get(key) or "").strip().casefold() for key in ("md5", "sha256"))
            if record.get("declared_checksum_verified") is True:
                own.add(str(record.get("declared_checksum") or "").strip().casefold())
        own.discard("")
        if claimed in own:
            return _uncovered("archive_checksum_as_own", "its row gives the checksum of the archive it came out of "
                              "as its own")
        cover = self._confirmed_claim(path, claimed, checksums)
        if cover.basis:
            cover.sha256 = str(checksums.get("sha256") or "").strip().casefold()
        return cover

    def _archive_download(self, archive: dict) -> dict | None:
        """The download record of the archive a row names, by its sha256, else by its download path."""
        claimed = str(archive.get("sha256") or "").strip().casefold()
        download = self.downloads_by_sha256.get(claimed) if claimed else None
        if download is None and str(archive.get("download_path") or "").strip():
            download = self.downloads_by_path.get(_path_key(archive["download_path"]))
        return download

    def _downloads_under(self, folder: str) -> list[dict]:
        """The downloads inside a folder, from one index over every download's parents.

        A Waters unit holds hundreds of .raw folders and tens of thousands of member objects; searching
        the downloads once per folder would grow with their product.
        """
        if self._under is None:
            self._under = {}
            stop = _path_key(self.input_directory) if self.input_directory else ""
            for key, item in self.downloads_by_path.items():
                parent = os.path.dirname(key)
                while parent and len(parent) > len(stop) and parent != os.path.dirname(parent):
                    self._under.setdefault(parent, []).append(item)
                    parent = os.path.dirname(parent)
        return self._under.get(_path_key(folder), [])

    def _vendor_folder(self, path: str, checksums: dict) -> _Cover:
        members = self._downloads_under(path)
        if not members:
            return _uncovered("not_downloaded", "no downloaded object lies under it")
        count = checksums.get("member_objects")
        if isinstance(count, int) and count != len(members):
            return _uncovered("member_count_disagrees",
                              f"its row counts {count} member object(s) and the downloads {len(members)}")
        covers = []
        for member in members:
            name = Path(str(member.get("path"))).name
            if not self._download_sha256(member):
                return _uncovered("no_download_sha256", f"its member {name} has no sha256 recorded at download")
            relative = _relative_name(member.get("path"), self.input_directory) if self.input_directory else ""
            if relative and any(form in self.declared_names for form in _name_forms(relative)):
                covers.append(_Cover("verified"))
                continue
            cover = self._declaration(member, archive=False)
            if not cover.basis:
                return _uncovered(cover.reason, f"its member {name}: {cover.detail}")
            covers.append(cover)
        return _weakest(covers)

    def _member_names(self, path: str, record: dict, member: str) -> list[str]:
        """The names an input may carry in an archive's listing, which is relative to the destination."""
        key = _path_key(path)
        destination = str(record.get("destination") or "")
        if destination and key.startswith(_path_key(destination) + os.sep):
            return [key[len(_path_key(destination)) + 1:].replace(os.sep, "/").casefold()]
        names = [member.replace("\\", "/").strip("/").casefold()] if member.strip() else []
        root = _path_key(self.input_directory) if self.input_directory else ""
        if root and key.startswith(root + os.sep):
            names.append(key[len(root) + 1:].replace(os.sep, "/").casefold())
        return list(dict.fromkeys(names))

    def _from_archive(self, path: str, archive: dict, member: str, *, directory: bool) -> _Cover:
        claimed = str(archive.get("sha256") or "").strip().casefold()
        download = self._archive_download(archive)
        if download is None:
            return _uncovered("archive_not_downloaded", "the archive it came out of is not a recorded download")
        sha256 = self._download_sha256(download)
        name = Path(str(download.get("path") or "")).name or "its archive"
        if not sha256:
            return _uncovered("no_download_sha256", f"no sha256 was recorded when {name} was downloaded")
        if claimed and claimed != sha256:
            return _uncovered("sha256_disagrees", f"its lineage row and the download record of {name} give "
                              "different sha256")
        cover = self._declaration(download, archive=True)
        record = self.extractions.get(sha256)
        if not cover.basis and record is not None:
            cover = self._verified_inner(path, record, member) or cover
        if not cover.basis:
            return cover
        if not cover.archive:
            cover.archive = sha256
            cover.archive_algorithm = (str(download.get("declared_checksum_algorithm") or "").strip().casefold()
                                       or ("md5" if cover.basis == "archive_verified" else ""))
            cover.crc_verified = record.get("crc_verified") if record is not None else None
        if record is None:
            if (cover.basis == "download_sha256" and self.extraction_records is None
                    and (_path_key(path) in self.extracted or (self.input_directory and _path_key(path).startswith(
                        _path_key(self.input_directory) + os.sep)))):
                # Extracted before the record existed: the archive's hash covers what came out of it, as
                # the legacy reading has it.
                return _Cover("download_sha256", archive=sha256)
            return _uncovered("no_extraction_record",
                              f"no archive_extractions record lists what came out of {name}")
        rejected = _rejected_member_count(record)
        if rejected:
            return _uncovered("members_rejected", f"{rejected} member(s) of {name} were rejected at extraction, "
                              "so what came out of it is not the archive that was published")
        listing = self._listing(record)
        if listing.problem:
            return _uncovered("listing_unreadable", listing.problem)
        names = self._member_names(path, record, member)
        found = [item for item in names if (item in listing.directories if directory else item in listing.files)]
        if not found:
            return _uncovered("missing_from_listing", f"it is not in the recorded member listing of {name}")
        size = None if directory else listing.files[found[0]]
        return _Cover(cover.basis, size=size, archive=cover.archive, archive_algorithm=cover.archive_algorithm,
                      crc_verified=cover.crc_verified)

    def _verified_inner(self, path: str, record: dict, member: str) -> _Cover | None:
        """The innermost archive expanded inside a download that holds the input and matched its own checksum.

        MB-POST ships one project tar of per-sample zips and publishes an MD5 for each zip, none for the
        tar. The lease compares each zip with its MD5 before it expands, and records that on the zip's
        nested extraction record; the tar's download carries the first zip's value, never compared with
        the tar, so the tar vouches for nothing and the zip that holds the input does. Held against the
        records that did it: the digest the extraction computed for the zip's bytes is the published
        value, the unit declares that value for a file of the zip's name, and the allow-list validator's
        count accounts for every checksum the unit declares. The member listing is still the download's,
        which lists the whole lineage, nested archives included.
        """
        names = self._member_names(path, record, member)
        found: tuple[int, dict, str] | None = None
        pending = [(item, 1) for item in record.get("nested") or [] if isinstance(item, dict)]
        while pending:
            item, depth = pending.pop()
            if depth < 8:
                pending.extend((child, depth + 1) for child in item.get("nested") or [] if isinstance(child, dict))
            root = str(item.get("destination_relative") or "").replace("\\", "/").strip("/").casefold()
            if not root or not any(name == root or name.startswith(root + "/") for name in names):
                continue
            algorithm = str(item.get("declared_checksum_algorithm") or "").strip().casefold()
            declared = str(item.get("declared_checksum") or "").strip().casefold()
            computed = (str(item.get(f"archive_{algorithm}") or "").strip().casefold()
                        if algorithm in ALGORITHM_NAMES else "")
            if (item.get("declared_checksum_verified") is not True or not declared or computed != declared
                    or not self.validator_accounts
                    or (_declared_name(item.get("declared_name")), declared) not in self.declared_files):
                continue
            if found is None or depth > found[0]:
                found = (depth, item, algorithm)
        if found is None:
            return None
        _depth, item, algorithm = found
        return _Cover("archive_verified", archive=str(item.get("archive_sha256") or "").strip().casefold()
                      or f"{record.get('archive_sha256')}:{item.get('archive_path')}",
                      archive_algorithm=algorithm, crc_verified=item.get("crc_verified"))

    def _listing(self, record: dict) -> _ArchiveListing:
        members = record.get("members_tsv")
        name = str(record.get("archive_name") or "the archive")
        if not isinstance(members, dict) or not str(members.get("path") or "").strip():
            return _ArchiveListing(problem=f"the extraction record of {name} keeps no member listing")
        recorded = Path(str(members["path"]))
        candidates = [recorded]
        if str(self.owner.get("workspace") or "").strip():
            # Read where the unit keeps it, if its workspace moved since the listing was written.
            candidates.append(Path(str(self.owner["workspace"])) / "provenance" / recorded.name)
        path = next((item for item in candidates if item.is_file()), None)
        if path is None:
            return _ArchiveListing(problem=f"the member listing of {name} ({recorded.name}) is absent")
        cache_key = _path_key(path)
        if cache_key in self._listings:
            return self._listings[cache_key]
        listing = self._listings[cache_key] = _read_archive_listing(path, str(members.get("sha256") or ""), name)
        return listing

    def _converted(self, path: str, source: dict, checksums: dict, depth: int) -> _Cover:
        if depth:
            return _uncovered("conversion_chain", "it is a conversion of a conversion, which the gate does not follow")
        records = self.conversions.get(_path_key(path)) or []
        if not records:
            return _uncovered("no_conversion_record", "no conversion record names it as its output")
        if len(records) > 1:
            return _uncovered("ambiguous_conversion_record",
                              f"{len(records)} conversion records name it as their output")
        record = records[0]
        if record.get("status") != "converted":
            return _uncovered("conversion_not_completed",
                              f"its conversion record's status is {record.get('status')!r}, not 'converted'")
        output = record.get("output") if isinstance(record.get("output"), dict) else {}
        output_sha256 = str(output.get("sha256") or "").strip().casefold()
        if not output_sha256:
            return _uncovered("no_output_sha256", "its conversion record keeps no sha256 of the mzML it wrote")
        recorded = str(checksums.get("sha256") or "").strip().casefold()
        if recorded and recorded != output_sha256:
            return _uncovered("sha256_disagrees", "its lineage row and its conversion record give different sha256")
        read = record.get("source") if isinstance(record.get("source"), dict) else {}
        source_sha256 = str(read.get("sha256") or "").strip().casefold()
        if not source_sha256:
            return _uncovered("no_source_sha256", "its conversion record keeps no sha256 of the mzXML it read")
        reference = source.get("conversion") if isinstance(source.get("conversion"), dict) else {}
        if str(reference.get("source_sha256") or "").strip().casefold() != source_sha256:
            return _uncovered("source_sha256_mismatch", "its row's source_sha256 is not the sha256 of the mzXML its "
                              "conversion record read")
        source_path = str(read.get("path") or reference.get("source_path") or "")
        if reference.get("source_path") and read.get("path") and not _same_path(reference["source_path"], read["path"]):
            return _uncovered("source_path_mismatch", "its row and its conversion record name different mzXML files")
        source_name = Path(source_path).name or "its source"
        # The mzXML the record read is the one to be covered, whatever row vouches for it: a carried row
        # for another file would let that file's cover stand for bytes nobody checked.
        table = (self.rows.get(_path_key(source_path)) or []) if source_path.strip() else []
        if any(row != table[0] for row in table[1:]):
            return _uncovered("ambiguous_lineage_row",
                              f"the lineage table has {len(table)} different rows for its source {source_name}")
        carried = reference.get("source_row") if isinstance(reference.get("source_row"), dict) else None
        if carried is not None and not (source_path.strip() and _same_path(carried.get("path") or "", source_path)):
            described = Path(str(carried.get("path") or "")).name or "no file"
            return _uncovered("source_row_mismatch", f"the source row it carries describes {described}, not the "
                              f"{source_name} its conversion record read")
        if carried is not None and table and carried != table[0]:
            return _uncovered("source_row_mismatch", f"the source row it carries is not the lineage table's row for "
                              f"{source_name}")
        source_row = table[0] if table else carried if carried is not None else self._derived_row(source_path)
        if source_row is None:
            return _uncovered("source_not_traced", f"{source_name} is neither a recorded download nor listed in an "
                              "archive extraction")
        cover = self.resolve_row(source_row, depth + 1)
        if not cover.basis:
            return _uncovered(cover.reason, f"its source {source_name} is not covered: {cover.detail}")
        if cover.sha256 and cover.sha256 != source_sha256:
            return _uncovered("source_sha256_mismatch", f"the conversion read other bytes than the {source_name} "
                              "that was downloaded")
        # A member verified by the allow-list validator is not hashed in the lineage; its declared
        # checksum is what was compared, and the record keeps the sha256 and sha1 of what it read.
        read_digest = (str(read.get(cover.algorithm) or "").strip().casefold()
                       if cover.algorithm in ("md5", "sha1", "sha256") else "")
        if cover.declared and read_digest and read_digest != cover.declared:
            return _uncovered("source_checksum_mismatch", f"the conversion read other bytes than the {source_name} "
                              f"whose declared {cover.algorithm} was verified")
        read_bytes = read.get("bytes")
        if cover.size is not None and isinstance(read_bytes, int) and cover.size != read_bytes:
            return _uncovered("source_size_mismatch", f"the conversion read {read_bytes} bytes and the record that "
                              f"covers {source_name} gives {cover.size}")
        return _Cover(cover.basis, sha256=output_sha256, archive=cover.archive,
                      archive_algorithm=cover.archive_algorithm, crc_verified=cover.crc_verified, converted=True)

    def _derived_row(self, path: str) -> dict | None:
        """A lineage row for a conversion's source, from the download or the archive listing that holds it."""
        if not path.strip():
            return None
        key = _path_key(path)
        if key in self.downloads_by_path:
            return {"kind": "file", "path": path, "source": {"download_path": self.downloads_by_path[key].get("path")}}
        for record in self.extraction_records or []:
            listing = self._listing(record)
            if not listing.problem and any(name in listing.files for name in self._member_names(path, record, "")):
                return {"kind": "extracted_member", "path": path,
                        "source": {"archive": {"sha256": record.get("archive_sha256")}}}
        return None


def _rejected_member_count(record: dict, depth: int = 0) -> int:
    rejected = record.get("rejected_members")
    count = len(rejected) if isinstance(rejected, list) else 0
    nested = record.get("nested")
    if depth < 8 and isinstance(nested, list):
        count += sum(_rejected_member_count(item, depth + 1) for item in nested if isinstance(item, dict))
    return count


def _read_archive_listing(path: Path, recorded_sha256: str, name: str) -> _ArchiveListing:
    """The files and folders an archive's recorded listing says were extracted."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        return _ArchiveListing(problem=f"the member listing of {name} could not be read: {exc}")
    if not recorded_sha256.strip():
        return _ArchiveListing(problem=f"the extraction record of {name} keeps no sha256 of its member listing")
    if hashlib.sha256(data).hexdigest() != recorded_sha256.strip().casefold():
        return _ArchiveListing(problem=f"the member listing of {name} has changed since its sha256 was recorded")
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return _ArchiveListing(problem=f"the member listing of {name} is not UTF-8")
    header = lines[0].split("\t") if lines else []
    if not set(ARCHIVE_LISTING_COLUMNS) <= set(header):
        return _ArchiveListing(problem=f"the member listing of {name} does not have the columns archives.py writes")
    listing = _ArchiveListing()
    for number, line in enumerate(lines[1:], start=2):
        if not line:
            continue
        cells = line.split("\t")
        if len(cells) != len(header):
            return _ArchiveListing(problem=f"line {number} of the member listing of {name} has {len(cells)} fields, "
                                   f"not {len(header)}")
        entry = dict(zip(header, cells))
        # Only what was extracted: dropped operating-system metadata and nested archives that were
        # expanded in place are not on disk as themselves.
        if entry["disposition"] != "extracted":
            continue
        member = entry["path"].replace("\\", "/").strip("/").casefold()
        parts = member.split("/")
        listing.directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
        if entry["type"] == "dir":
            listing.directories.add(member)
        else:
            listing.files[member] = int(entry["size"]) if entry["size"].isdigit() else None
            listing.names[member] = entry["path"].replace("\\", "/").strip("/")
    return listing


def _lineage_checksum_basis(owner: dict, evidence: dict, candidates: list[str]) -> tuple[str, str, dict]:
    """SUM-1 for a manifest that carries input_lineage: every analysed input resolved through its row."""
    lineage = _InputLineage(owner)
    evidence["lineage_schema"] = INPUT_LINEAGE_SCHEMA
    evidence["basis"] = "insufficient"
    if lineage.problem:
        return "insufficient", lineage.problem, evidence
    if not candidates:
        return "insufficient", "No input candidate is recorded, so the lineage vouches for nothing analysed.", evidence
    covers = [(item, lineage.resolve_input(item)) for item in candidates]
    uncovered = [(item, cover) for item, cover in covers if not cover.basis]
    covered = [cover for _, cover in covers if cover.basis]
    archives = {cover.archive: cover for cover in covered if cover.archive and cover.basis == "archive_verified"}
    evidence.update({
        "lineage_rows": sum(len(rows) for rows in lineage.rows.values()),
        "inputs_by_basis": dict(Counter(cover.basis for cover in covered)),
        "inputs_uncovered": len(uncovered),
        "uncovered_reasons": dict(Counter(cover.reason for _, cover in uncovered)),
        "uncovered": [{"input": Path(item).name, "reason": cover.reason, "detail": cover.detail}
                      for item, cover in uncovered[:10]],
        "converted_inputs": sum(1 for cover in covered if cover.converted),
        "archives_verified": len(archives),
        "archives_without_member_crc": sum(1 for cover in archives.values() if cover.crc_verified is not True),
    })
    if uncovered:
        shown = "; ".join(f"{Path(item).name}: {cover.detail}" for item, cover in uncovered[:3])
        return ("insufficient", f"{len(uncovered)} of {len(candidates)} input(s) are covered by nothing their lineage "
                f"records: {shown}.", evidence)
    basis = evidence["basis"] = _weakest(covered).basis
    counts = evidence["inputs_by_basis"]
    converted = (f" {evidence['converted_inputs']} of the inputs are mzML converted from an mzXML that is covered "
                 "in this way itself." if evidence["converted_inputs"] else "")
    if basis == "verified":
        return ("verified", f"Every one of the {len(candidates)} inputs had its own declared checksum verified, "
                "traced through the input lineage." + converted, evidence)
    if basis == "archive_verified":
        without_crc = evidence["archives_without_member_crc"]
        own = counts.get("verified", 0)
        # Named as Interactive's statement names it: "published SHA-256" where that is what was compared.
        algorithms = sorted({ALGORITHM_NAMES.get(cover.archive_algorithm, "") for cover in archives.values()} - {""})
        wording = _archive_wording(algorithms)
        evidence["archive_algorithms"] = algorithms
        return (
            "archive_verified",
            f"{counts.get('archive_verified', 0)} of the {len(candidates)} inputs were {wording}, and each is "
            f"named in the recorded member listing of the download it came out of"
            + (f" ({without_crc} of the {len(archives)} archive(s) carry no member CRC the extractor could check)"
               if without_crc else "")
            + (f"; the other {own} had their own declared checksum verified" if own else "")
            + f".{converted} The archived inputs' own checksums were not compared, so no artifact may call them "
            f"checksum-verified; the permitted wording is '{wording}'. Whether this basis passes is "
            "the user's decision, not yet taken.",
            evidence,
        )
    return (
        "download_sha256",
        f"{lineage.repository} publishes no checksum for any file, so nothing could be compared with a source "
        f"value. Integrity rests on the sha256 recorded at download, which covers all {len(candidates)} inputs "
        f"through their lineage.{converted} No artifact may describe these inputs as checksum-verified.",
        evidence,
    )


def _checksum_basis(owner: dict, not_analysed: "dict[str, tuple[str, str]] | None" = None) -> tuple[str, str, dict]:
    """How this unit's inputs are known to be intact: (kind, detail, evidence).

    kind is "verified" (every declared file was checked against its published checksum, and every
    input is one of them, lies in a declared vendor directory, or came out of a declared archive),
    "archive_verified" (some input rests on the published MD5 of the archive it was extracted from,
    and on that archive's recorded listing, and the rest are verified; only a manifest carrying
    input_lineage can say so), "download_sha256" (the repository publishes none, and every input rests
    on a sha256 recorded at download), or "insufficient" (anything else).

    A manifest carrying input_lineage is resolved through it, input by input (_InputLineage), over the
    input candidates less ``not_analysed`` (_not_analysed: excluded by the campaign disposition, and
    opened by no CSV row), which are reported in the evidence and vouch for nothing. One written before
    the table existed is read as it always was, below, exclusions and all: it predates campaign
    dispositions (Interactive 0.5.9 against 0.5.17), and its validation counts every declared file
    without naming one, so which of them an excluded input's were cannot be told.
    """
    validation = owner.get("allowlist_checksum_validation")
    files = [item for item in (owner.get("project") or {}).get("files") or [] if isinstance(item, dict)]
    downloads = [item for item in owner.get("downloads") or [] if isinstance(item, dict)]
    raw_candidates = owner.get("input_candidates")
    candidates = [str(item) for item in raw_candidates] if isinstance(raw_candidates, list) else []
    not_analysed = (not_analysed or {}) if owner.get("input_lineage") is not None else {}
    excluded = [not_analysed[_path_key(item)] for item in candidates if _path_key(item) in not_analysed]
    candidates = [item for item in candidates if _path_key(item) not in not_analysed]
    raw_extracted = owner.get("extracted_files")
    extracted = {_path_key(item) for item in raw_extracted} if isinstance(raw_extracted, list) else set()
    input_directory = str(owner.get("input_directory") or "")
    verified = validation.get("verified") if isinstance(validation, dict) else None
    skipped = validation.get("skipped") if isinstance(validation, dict) else None
    declared = [item for item in files if str(item.get("checksum") or "").strip()]
    declared_names = [_declared_name(item.get("name")) for item in declared]
    declared_at_download = [item for item in downloads if str(item.get("declared_checksum") or "").strip()]
    hashed_downloads = [item for item in downloads if str(item.get("sha256") or "").strip()]
    hashed = {_path_key(item.get("path")) for item in hashed_downloads}
    hashed_archive = any(str(item.get("path") or "").casefold().endswith(ARCHIVE_SUFFIXES)
                         for item in hashed_downloads)
    all_downloads_hashed = bool(downloads) and len(hashed_downloads) == len(downloads)
    repository = str((owner.get("project") or {}).get("repository") or "").strip().casefold()
    counts_known = isinstance(verified, int) and isinstance(skipped, int)

    def under_input_directory(item: str) -> bool:
        if not input_directory:
            return False
        root = _path_key(input_directory)
        return _path_key(item).startswith(root + os.sep)

    def declared_cover(item: str) -> bool:
        relative = _relative_name(item, input_directory) if input_directory else Path(item).name.casefold()
        if any(_names_cover(relative, name) for name in declared_names):
            return True
        # A vendor directory (.d, .raw) is one input whose files are declared one by one, under it.
        # No clause lets a declared archive vouch for whatever lies under the data directory: the
        # validator checks an archive only where it sits unextracted, and nothing then came out of it.
        prefixes = tuple(form + "/" for form in _name_forms(relative))
        return any(name.startswith(prefixes) for name in declared_names)

    def download_cover(item: str) -> bool:
        if _path_key(item) in hashed:
            return True
        if not all_downloads_hashed:
            return False
        # Extracted from a hashed archive: the archive's hash covers what came out of it.
        return _path_key(item) in extracted or (hashed_archive and under_input_directory(item))

    evidence = {
        "verified": verified, "skipped": skipped, "inputs": len(candidates), "files": len(files),
        "any_checksum_verified": validation.get("required") if isinstance(validation, dict) else None,
        "declared_checksums": len(declared), "downloads_with_sha256": len(hashed_downloads),
        "repository": repository,
    }
    if excluded:
        evidence["inputs_excluded"] = len(excluded)
        evidence["excluded"] = [{"input": Path(path.rstrip("\\/")).name, "reason": reason}
                                for path, reason in excluded[:10]]
        if not candidates:
            return ("insufficient", f"All {len(excluded)} input candidate(s) were excluded by the campaign "
                    "disposition, so no input is analysed for a checksum to vouch for.", evidence)
    if owner.get("input_lineage") is not None:
        return _lineage_checksum_basis(owner, evidence, candidates)
    if counts_known and files and skipped == 0 and verified == len(files):
        # The validator raises on a mismatch or on a file it cannot resolve, so a count equal to the
        # declared files means every one was checked. Sidecars such as .wiff.scan are declared and
        # verified but are not inputs, so the files are not compared with the inputs by count; each
        # input is instead traced to a verified declaration.
        uncovered = [item for item in candidates if not declared_cover(item)]
        evidence["inputs_without_verified_checksum"] = len(uncovered)
        if not candidates:
            return ("insufficient", "No input candidate is recorded, so nothing ties the verified files "
                    "to what is analysed.", evidence)
        if uncovered:
            return ("insufficient", f"Every declared file was verified, but {len(uncovered)} input(s) "
                    "carry no declared checksum: they were admitted without being checked.", evidence)
        return ("verified", f"All {verified} declared files verified, none skipped, covering all "
                f"{len(candidates)} inputs.", evidence)
    uncovered = [item for item in candidates if not download_cover(item)]
    evidence["inputs_without_download_sha256"] = len(uncovered)
    if (
        repository in REPOSITORIES_WITHOUT_CHECKSUMS
        and counts_known and verified == 0 and files and skipped == len(files)
        and not declared and not declared_at_download
        and candidates and not uncovered
    ):
        return (
            "download_sha256",
            f"{repository} publishes no checksum for any file, so nothing could be compared with a "
            f"source value. Integrity rests on the sha256 recorded at download, which covers all "
            f"{len(candidates)} inputs. No artifact may describe these inputs as checksum-verified.",
            evidence,
        )
    if counts_known and not declared and not declared_at_download and repository not in REPOSITORIES_WITHOUT_CHECKSUMS:
        detail = (
            f"No checksum is recorded for any file of this unit, and {repository or 'its repository'} "
            "is not recorded as a repository that publishes none: the declaration was lost on the way "
            "in, and nothing was checked."
        )
    elif uncovered and not declared:
        detail = (
            f"{len(uncovered)} input(s) are not traced to a hashed download."
            if hashed_downloads else
            f"{len(uncovered)} input(s) have neither a verified checksum nor a sha256 recorded at "
            "download, so their integrity rests on nothing."
        )
    elif not candidates:
        detail = "No input candidate is recorded."
    elif not counts_known:
        detail = "The checksum validation record does not say how many files it verified and skipped."
    else:
        detail = (
            "Checksum coverage is partial. The integrity claim in the published audit trail would "
            "overstate what was actually checked."
        )
    return "insufficient", detail, evidence


def check_checksum_coverage(report: Report, provenance: dict | None, reason: str,
                           csv_rows: list[dict] | None = None) -> None:
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

    AN ARCHIVE'S PUBLISHED MD5 is the only checksum a Metabolomics Workbench archive unit has. Where
    the manifest carries input_lineage, an input extracted from an archive whose MD5 matched at
    download, and named in that archive's recorded member listing, rests on basis archive_verified:
    ARCHIVE_VERIFIED_STATUS, a WARN until the user decides it passes. An unverified archive MD5, an
    input missing from the listing, and a member rejected at extraction are FAILs.

    AN INPUT A BINDING CAMPAIGN DISPOSITION EXCLUDED is never analysed: Interactive leaves it among the
    input candidates, where the disposition found it, and gives it no CSV row. Its checksum is not
    required, and it is reported as excluded with the disposition's reason (_not_analysed). Where a CSV
    row opens it all the same, through its Console alias or not, it is analysed, and covered or refused
    like any other input; INP-1 refuses the row. So is every excluded input where a row opens something
    the gate cannot tie to a candidate, which may be any of them under another name, and where the
    analysis CSV is absent or cannot be read, which leaves no row to tie at all.

    RUN POLICY: blocks_run, as the user named it (2026-10-01). Bytes nothing vouches for give results
    that may not describe the published data.
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
                       f"This unit was split from another, whose manifest cannot be used: {owner_reason}")
            return
        inherited_from = str((provenance.get("split_from") or {}).get("analysis_unit_id") or "")
    if not isinstance(owner.get("allowlist_checksum_validation"), dict) or not isinstance(
        owner.get("input_candidates"), list
    ):
        report.add("SUM-1", stage, title, NOT_EVALUABLE,
                   "The manifest records no checksum validation block or no input candidates.",
                   inherited_from=inherited_from)
        return
    not_analysed = _not_analysed(provenance, owner, csv_rows)
    kind, detail, evidence = _checksum_basis(owner, not_analysed)
    if evidence.get("inputs_excluded") and evidence.get("inputs"):
        detail += _excluded_sentence(not_analysed)
    status = {"verified": PASS, "archive_verified": ARCHIVE_VERIFIED_STATUS, "download_sha256": WARN}.get(kind, FAIL)
    report.add("SUM-1", stage, title, status, detail, inherited_from=inherited_from, **evidence)


# --------------------------------------------------------------------------------------------
# conversion
# --------------------------------------------------------------------------------------------
#
# MS-DIAL opens neither mzXML nor mzData: MsdialCore lists no such format, and RawDataHandler has no
# reader for either. Interactive's mzxml_conversion.py (0.5.11) writes a sample whose only readable
# encoding is mzXML as plain mzML under <raw>\converted, re-reads what it wrote with a second reader
# and compares every spectrum with the mzXML, and keeps one record per file in the raw owner's
# input_conversions (msdial-mzxml-conversion.v1 records, as a list or as an object's "records"). SUM-1
# follows a converted input back through its record to the mzXML it read; CONV-1 holds the
# conversion itself to its record.

CONVERSION_RECORD_SCHEMA = "msdial-mzxml-conversion.v1"
CONVERSION_VALIDATION_SCHEMA = "msdial-mzxml-conversion-validation.v1"
# What MS-DIAL cannot open, as Interactive names it: mzXML, which the converter writes as mzML, and
# mzData, which nothing converts.
UNREADABLE_ENCODINGS = (".mzxml", ".mzdata", ".mzdata.xml")
# Where Interactive writes each conversion: beside the raw tree's data, and released with it.
CONVERTED_DIRECTORY = "converted"
CONV1_TITLE = "Every converted input is a validated conversion kept in the raw tree"
_SHA256_TEXT = re.compile(r"[0-9a-f]{64}")


def _count(value: object) -> int | None:
    """A count as a record writes one: a whole number, never a bool."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _unreadable_encoding(path: object) -> bool:
    return str(path or "").rstrip("\\/").casefold().endswith(UNREADABLE_ENCODINGS)


def _under(path: object, root: object) -> bool:
    return bool(str(root or "").strip()) and _path_key(path).startswith(_path_key(root) + os.sep)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _conversion_records(owner: dict) -> tuple[list | None, str]:
    """The raw owner's conversion records, None where it keeps none, and what is wrong with the block."""
    block = owner.get("input_conversions")
    if block is None:
        return None, ""
    records = block.get("records") if isinstance(block, dict) else block
    if not isinstance(records, list):
        return [], "the manifest's input_conversions is neither a list of conversion records nor an object holding one"
    return records, ""


def _declared_ion_mode(owner: dict) -> tuple[str, str, str]:
    """The ion mode the raw owner declared: its polarity as the converter's imputation names it ("" where
    it names neither), the declared value ("" where none survives), and where it was read, or why none is.

    The converter imputes the unit's declared ion mode, so that is what an imputation is held to, never
    project.ion_mode as it stands: the raw-header preflight rewrites that from the headers, and a
    converted input's headers carry the polarity its conversion imputed. The Catalog handoff's
    technical settings, which nothing rewrites, keep the declaration; project.ion_mode stands for it
    only where no preflight is recorded. The raw owner converted every part's files, so a split part's
    own ion mode is no declaration either.
    """
    project = owner.get("project") if isinstance(owner.get("project"), dict) else {}
    metadata = project.get("repository_metadata")
    handoff = metadata.get("catalog_handoff") if isinstance(metadata, dict) else None
    settings = handoff.get("technical_settings") if isinstance(handoff, dict) else None
    preflight = owner.get("raw_metadata_preflight")
    if isinstance(settings, dict) and str(settings.get("ion_mode") or "").strip():
        mode, source = str(settings["ion_mode"]).strip(), "the Catalog handoff's technical settings"
    elif isinstance(preflight, dict) and preflight:
        return "", "", ("no Catalog handoff gives one, and the raw-header preflight rewrote the project record's "
                        "from the headers, which carry what was imputed")
    elif str(project.get("ion_mode") or "").strip():
        mode, source = (str(project["ion_mode"]).strip(),
                        "the project record, which no raw-header preflight has rewritten")
    else:
        return "", "", "neither a Catalog handoff nor the project record gives one"
    folded = mode.casefold()
    return ("positive" if folded.startswith("pos") else "negative" if folded.startswith("neg") else ""), mode, source


def _conversion_problems(record: dict, raw_roots: list[str], moved: "tuple[str, Path] | None", raw_present: bool,
                         declared: tuple[str, str, str], declarer: str, tally: Counter) -> list[str]:
    """What stands against one completed conversion record, adding what it did to the tally.

    ``raw_roots`` are the raw tree as the raw owner's manifest records it and where it is now; ``moved``
    maps the first onto the second where the workspace moved since the record was written. ``declared``
    is the raw owner's ion mode as _declared_ion_mode reads it, and ``declarer`` names that unit.
    """
    if record.get("schema") != CONVERSION_RECORD_SCHEMA:
        return [f"its record is not an {CONVERSION_RECORD_SCHEMA} record, the one the gate reads"]
    problems = []
    output = record["output"]
    path = str(output["path"])
    if not any(_under(path, root) for root in raw_roots):
        problems.append("it lies outside the raw tree, so it is not released with the raw data")
    sha256 = str(output.get("sha256") or "").strip().casefold()
    if not _SHA256_TEXT.fullmatch(sha256):
        problems.append("its record keeps no sha256 of the mzML it wrote")
    else:
        places = [Path(path)]
        if moved is not None and _under(path, moved[0]):
            places.append(moved[1] / os.path.relpath(os.path.normpath(path), os.path.normpath(moved[0])))
        located = next((item for item in places if item.is_file()), None)
        if located is None:
            if raw_present:
                problems.append("it is absent while the raw tree is present, so the mzML MS-DIAL reads cannot be "
                                "re-hashed")
            else:
                # Released with the raw tree: the sha256 recorded when it was written is what stands.
                tally["released"] += 1
        else:
            try:
                size = located.stat().st_size
                recorded = _count(output.get("bytes"))
                if recorded is not None and size != recorded:
                    problems.append(f"it holds {size} bytes where its record gives {recorded}")
                elif _file_sha256(located) != sha256:
                    problems.append("its bytes are no longer those whose sha256 its record gives")
                else:
                    tally["rehashed"] += 1
            except OSError as exc:
                problems.append(f"it could not be re-hashed: {exc}")
    converter = record.get("converter") if isinstance(record.get("converter"), dict) else {}
    missing = [key for key in ("name", "version") if not str(converter.get(key) or "").strip()]
    if not _SHA256_TEXT.fullmatch(str(converter.get("module_sha256") or "").strip().casefold()):
        missing.append("module_sha256")
    if missing:
        problems.append(f"its record does not identify the converter that wrote it (no {', '.join(missing)})")
    counts = record.get("counts") if isinstance(record.get("counts"), dict) else {}
    spectra = _count(counts.get("spectra"))
    validation = record.get("validation")
    compared = _count(validation.get("spectra_compared")) if isinstance(validation, dict) else None
    if not isinstance(validation, dict):
        problems.append("its record keeps no validation of the mzML against the mzXML")
    elif validation.get("schema") != CONVERSION_VALIDATION_SCHEMA:
        problems.append(f"its validation is not an {CONVERSION_VALIDATION_SCHEMA} record")
    elif validation.get("status") != "passed" or _count(validation.get("problem_count")) != 0:
        problems.append(f"its validation against the mzXML did not pass (status {validation.get('status')!r}, "
                        f"{validation.get('problem_count')!r} problem(s))")
    elif spectra is None or compared != spectra:
        problems.append(f"its validation compared {compared if compared is not None else 'an unrecorded number of'} "
                        f"spectra, and its record counts {spectra if spectra is not None else 'none'} written")
    else:
        tally["spectra"] += spectra
    # RawDataHandler reads polarity only as a spectrum cvParam, and MS-DIAL skips a spectrum whose polarity
    # is not the method's ion mode: a scan the mzXML left without one is lost unless one was imputed.
    after, before = counts.get("polarity"), counts.get("polarity_recorded")
    inferences = record.get("inferences") if isinstance(record.get("inferences"), list) else []
    imputations = [item for item in inferences if isinstance(item, dict) and item.get("kind") == "polarity_imputation"]
    imputed = sum(_count(item.get("spectra")) or 0 for item in imputations)
    if not isinstance(after, dict):
        problems.append("its record keeps no polarity counts, so nothing says every spectrum carries one")
    else:
        unrecorded = _count(after.get("unrecorded")) or 0
        absent = sum(_count(before.get(key)) or 0 for key in ("absent", "any")) if isinstance(before, dict) else None
        if unrecorded:
            problems.append(f"{unrecorded} of its spectra carry no polarity and none was imputed, and MS-DIAL skips a "
                            "spectrum whose polarity is not the method's ion mode")
        elif absent is not None and absent != imputed:
            problems.append(f"{absent} of its mzXML scans record no polarity, and its record imputes one to {imputed}")
    # The converter imputes only what it is asked to, and its caller asks for the unit's declared ion mode.
    polarity, mode, source = declared
    options = record.get("options") if isinstance(record.get("options"), dict) else {}
    asked = str(options.get("impute_polarity") or "").strip().casefold()
    for item in imputations:
        value = str(item.get("value") or "").strip().casefold()
        if value != asked:
            problems.append(f"it imputes {value or 'no'} polarity where its record's options asked for "
                            + (f"{asked} polarity" if asked else "no imputation"))
        if not polarity:
            problems.append(f"it imputes {value or 'no'} polarity where {declarer} declares no single ion mode ("
                            + (f"{mode}, from {source})" if mode else f"{source})"))
        elif value != polarity:
            problems.append(f"it imputes {value or 'no'} polarity where {declarer}'s declared ion mode is {polarity} "
                            f"(from {source})")
    tally["imputed"] += imputed
    return problems


def check_converted_inputs_are_their_conversions(
    report: Report, provenance: dict | None, reason: str, csv_rows: list[dict] | None, csv_reason: str,
) -> None:
    """CONV-1. Each converted input is its converter's recorded, validated output, and no input is a file
    MS-DIAL cannot open.

    SUM-1 asks whether the mzXML a converted input was read from is covered. Nothing asked whether the
    mzML is the conversion its record describes. So, for every completed conversion record (a split
    part's parent converted every part's files, and a part answers for its own inputs):

    - its output lies under the raw tree, which is released with the raw data and holds none of what
      is kept;
    - its sha256 is re-hashed while the file is on disk. One released with the raw tree keeps the sha256
      recorded when it was written; one absent while the raw tree is present is a FAIL;
    - it names its converter: name, version and the sha256 of the converter's module;
    - its validation against the mzXML passed, with no problem, over every spectrum it wrote;
    - no spectrum is left without a polarity unless one was imputed, and an imputation is what the
      record's options asked for and the ion mode its raw owner declared: in the Catalog handoff, or in
      the project record where no raw-header preflight has rewritten it from the headers the imputation
      wrote. Where no single ion mode is declared, an imputation is a FAIL.

    And for the unit: no input candidate and no analysis-CSV row is an mzXML or mzData file, and every
    input in the raw tree's converted directory is the output of a record. A record whose conversion
    did not complete is a FAIL where its output is an input, and a WARN where it is none, since a sample
    whose only encoding that mzXML was is then analysed by nothing. A candidate a binding campaign
    disposition excluded stays where it was found and is never opened, so it is not held against the unit.

    THE ONE ENCODING RULE (user decision, 2026-10-09; Interactive 0.5.36). Each sample's choice is held to the
    conversions (_encoding_conversion_problems): an mzXML a choice uses has a completed conversion record, and an
    mzXML a choice leaves unused as conversion_failed has one that did not complete, so the rule took the next
    file of that sample (clause 3). A record that says otherwise is a FAIL. Whether what runs is what the rule
    names is INP-1's.

    PASS where nothing was converted and nothing MS-DIAL cannot open is an input: every unit prepared
    before the converter existed.

    RUN POLICY: blocks_run, as the user placed it (2026-10-02). Its refusals reach the results: an mzML
    that is not its recorded, validated conversion, or whose spectra were given a polarity the unit did
    not declare, describes the published data no better than SUM-1's unverified bytes; an input MS-DIAL
    cannot open is skipped without a word (EXP-1 and CNT-1 find it only after the run); and spectra left
    without a polarity are skipped as not the method's ion mode. In a campaign the conversion imputes
    the polarity only where the unit's Catalog handoff declares exactly one ion mode (the user's decision
    of the same day), so an mzXML without one in any other unit FAILs here and the unit does not run. One
    refusal, an output outside the raw tree, is about its release only, and stops the run with the rest.
    """
    stage = "before-production"
    if provenance is None:
        report.add("CONV-1", stage, CONV1_TITLE, NOT_EVALUABLE, reason)
        return
    split = isinstance(provenance.get("split_from"), dict)
    owner, owner_reason = _raw_owner_manifest(provenance)
    if owner is None:
        report.add("CONV-1", stage, CONV1_TITLE, NOT_EVALUABLE,
                   f"This unit was split from another, whose manifest cannot be used: {owner_reason}")
        return
    records, shape = _conversion_records(owner)
    problems = [shape] if shape else []
    raw_path, _raw_owner, _raw_unknown = _raw_directory(provenance, report.workspace)
    recorded_raw = str(owner.get("raw_directory") or "").strip()
    raw_roots = [root for root in dict.fromkeys((recorded_raw, str(raw_path or ""))) if root]
    moved = ((recorded_raw, raw_path) if recorded_raw and raw_path is not None and not _same_path(recorded_raw, raw_path)
             else None)
    raw_present = raw_path is not None and raw_path.exists()
    raw_candidates = provenance.get("input_candidates")
    candidates = [str(item) for item in raw_candidates] if isinstance(raw_candidates, list) else None
    aliases = _input_keys_by_console_path(provenance)
    # Every input, by key, with the name it is shown by: the candidates, and what each CSV row opens.
    inputs = {_path_key(item): Path(item.rstrip("\\/")).name for item in candidates or []}
    for row in csv_rows or []:
        key = _input_key(row, aliases)
        if key:
            inputs.setdefault(key, Path(str(row.get("file_path") or "").rstrip("\\/")).name)

    by_output: dict[str, list[dict]] = {}
    for number, record in enumerate(records or [], start=1):
        output = record.get("output") if isinstance(record, dict) else None
        if not isinstance(output, dict) or not str(output.get("path") or "").strip():
            problems.append(f"conversion record {number} names no output")
            continue
        by_output.setdefault(_path_key(output["path"]), []).append(record)
    if by_output and not raw_roots:
        problems.append("the raw owner's manifest names no raw_directory, so where each output lies cannot be told")
    judged = {key: items for key, items in by_output.items() if not split or key in inputs}
    # The raw owner converted every part's files, with the ion mode it declared.
    declared = _declared_ion_mode(owner)
    declarer = "the parent unit" if split else "the unit"
    tally: Counter = Counter()
    inferences: Counter = Counter()
    deviations: Counter = Counter()
    failed: list[str] = []
    for key, items in judged.items():
        name = Path(str(items[0]["output"]["path"])).name
        if len(items) > 1:
            problems.append(f"{name}: {len(items)} conversion records name it as their output")
            continue
        record = items[0]
        if record.get("status") != "converted":
            error = str(record.get("error") or "no error recorded")
            if key in inputs:
                problems.append(f"{name}: it is an input, and its conversion record's status is "
                                f"{record.get('status')!r}, not 'converted' ({error})")
            else:
                failed.append(f"{name}: {error}")
            continue
        tally["converted"] += 1
        tally["inputs"] += int(key in inputs)
        problems.extend(f"{name}: {item}" for item in
                        _conversion_problems(record, raw_roots, moved, raw_present, declared, declarer, tally))
        for field_name, counter in (("inferences", inferences), ("deviations", deviations)):
            listed = record.get(field_name)
            if isinstance(listed, list):
                counter.update(str(item.get("kind") or "unnamed") for item in listed if isinstance(item, dict))

    # A candidate a binding campaign disposition excluded stays where it was found, and MS-DIAL never opens it.
    held = {_path_key(item) for item in _excluded_candidates(provenance, candidates or [])}
    unreadable = [Path(item.rstrip("\\/")).name for item in candidates or []
                  if _unreadable_encoding(item) and _path_key(item) not in held]
    set_aside = sum(1 for item in candidates or [] if _unreadable_encoding(item) and _path_key(item) in held)
    if unreadable:
        problems.append(f"{len(unreadable)} input candidate(s) are mzXML or mzData, which MS-DIAL cannot open "
                        f"({', '.join(unreadable[:5])})")
    listed_rows = [str(row.get("file_name") or "") or Path(str(row.get("file_path") or "")).name
                   for row in csv_rows or []
                   if _unreadable_encoding(row.get("file_path")) or _unreadable_encoding(_input_key(row, aliases))]
    if listed_rows:
        problems.append(f"{len(listed_rows)} analysis-CSV row(s) open an mzXML or mzData file, which MS-DIAL cannot "
                        f"open ({', '.join(listed_rows[:5])})")
    converted_roots = [os.path.join(root, CONVERTED_DIRECTORY) for root in raw_roots]
    unrecorded = [name for key, name in inputs.items()
                  if key not in by_output and any(_under(key, root) for root in converted_roots)]
    if unrecorded:
        problems.append(f"{len(unrecorded)} input(s) in the raw tree's {CONVERTED_DIRECTORY} directory are the output "
                        f"of no conversion record ({', '.join(unrecorded[:5])})")
    # The one encoding rule of 2026-10-09 (clauses 1 and 3): an mzXML a sample's choice uses is a completed conversion,
    # and one it leaves unused as conversion_failed is one whose conversion did not complete.
    encoding_problems, passed_over = _encoding_conversion_problems(provenance, records)
    problems.extend(encoding_problems)

    evidence = {
        "records": len(records or []), "judged": len(judged), "converted": tally["converted"], "failed": len(failed),
        "rehashed": tally["rehashed"], "released_with_raw_tree": tally["released"],
        "spectra_validated": tally["spectra"], "imputed_polarity_spectra": tally["imputed"],
        "declared_ion_mode": declared[1] or None,
        "inferences": dict(inferences), "deviations": dict(deviations),
        "converted_inputs": tally["inputs"], "unreadable_candidates_excluded": set_aside,
        "input_candidates": len(candidates) if candidates is not None else None,
        "analysis_csv_rows": len(csv_rows) if csv_rows is not None else None,
        **({"conversion_failed_next_encoding_used": passed_over[:10]} if passed_over else {}),
    }
    if problems:
        report.add("CONV-1", stage, CONV1_TITLE, FAIL,
                   f"{len(problems)} problem(s) with what was converted or with what MS-DIAL is given to open: "
                   + "; ".join(problems[:3]) + ". An mzML that is not its recorded, validated conversion, or a "
                   "file MS-DIAL cannot read, runs to a result that does not describe the published data.",
                   problems=problems[:10], failures=failed[:10], **evidence)
        return
    if candidates is None:
        report.add("CONV-1", stage, CONV1_TITLE, NOT_EVALUABLE,
                   "The manifest records no input candidates, so nothing says whether MS-DIAL is given a file it "
                   "cannot open.", **evidence)
        return
    if csv_rows is None:
        report.add("CONV-1", stage, CONV1_TITLE, NOT_EVALUABLE, csv_reason, **evidence)
        return
    unit = "this part's" if split else "the unit's"
    if tally["converted"]:
        kept = "; ".join(part for part in (
            f"{tally['rehashed']} re-hashed to the sha256 recorded" if tally["rehashed"] else "",
            f"{tally['released']} released with the raw tree, whose recorded sha256 stands" if tally["released"] else "",
        ) if part)
        named = [f"{count} {kind}" for kind, count in sorted((inferences + deviations).items())]
        detail = (f"{tally['inputs']} of {unit} {len(candidates)} input candidate(s) are mzML converted from mzXML"
                  + (f", beside {tally['converted'] - tally['inputs']} other completed conversion(s)"
                     if tally["converted"] > tally["inputs"] else "")
                  + f": each conversion lies in the raw tree ({kept}), names its converter, and passed its validation "
                  f"against the mzXML over all {tally['spectra']} spectra it wrote, none left without a polarity"
                  + (f" ({tally['imputed']} by imputing {declared[0]} polarity, {declarer}'s declared ion mode "
                     f"from {declared[2]})" if tally["imputed"] else "")
                  + (f", recording {', '.join(named)}" if named else "")
                  + ". No input MS-DIAL opens and no CSV row is an mzXML or mzData file.")
    else:
        detail = (f"No input of {'this part' if split else 'the unit'} was converted, and none of its "
                  f"{len(candidates) - set_aside} input candidate(s) MS-DIAL opens and {len(csv_rows)} analysis-CSV "
                  "row(s) is an mzXML or mzData file.")
    if set_aside:
        detail += (f" The campaign disposition excluded {set_aside} mzXML or mzData candidate(s), which stay where "
                   "they were found and are never opened.")
    if failed:
        lost = (f" Under the one encoding rule of 2026-10-09 the next file of {len(passed_over)} such sample(s) runs "
                f"instead ({'; '.join(passed_over[:5])})." if passed_over else "")
        report.add("CONV-1", stage, CONV1_TITLE, WARN,
                   f"{len(failed)} conversion(s) did not complete, and none of their outputs is an input: "
                   + "; ".join(failed[:3]) + ". A sample whose only encoding was one of these mzXML is analysed by "
                   "nothing." + lost + " " + detail, failures=failed[:10], **evidence)
        return
    report.add("CONV-1", stage, CONV1_TITLE, PASS, detail, **evidence)


def check_split_part_partitions_its_parent(report: Report, provenance: dict | None, reason: str) -> None:
    """SPL-1. A split part is one of a set of parts that hold exactly the parent's inputs, once each.

    Each part can pass every other check on its own. What none of them can see is a file that went
    into two parts or into none; the parent's split_into and each part's input_candidates are the
    two records that must agree. An input the parent's binding campaign disposition excluded stays
    among the parent's candidates and goes into no part.

    RUN POLICY: record_only, as the user placed it (2026-10-02). Its FAIL does not break this part's
    results: they come from the part's own rows, whose types ACQ-1 holds to their headers, and whose
    inputs INP-1, SUM-1 and CNT-1 hold to the part's candidates, all of which do stop the run. What a
    FAIL here adds is that the parts
    together do not hold the parent's inputs once each, or that the part's records name another raw
    tree than its parent's: a file analysed by two parts or by none, which is the campaign's coverage,
    or records RET-1 and DSK-1 would follow to another unit's disk.
    """
    stage = "before-production"
    title = "A split part partitions its parent's inputs"
    split_from = (provenance or {}).get("split_from")
    if not isinstance(split_from, dict):
        report.add("SPL-1", stage, title, NOT_EVALUABLE,
                   reason or "This unit was not split from another.", required=False)
        return
    parent, parent_reason = _raw_owner_manifest(provenance)
    if parent is None:
        report.add("SPL-1", stage, title, NOT_EVALUABLE, parent_reason)
        return
    own_id = str((provenance.get("project") or {}).get("analysis_unit_id") or "")
    parts = [item for item in parent.get("split_into") or [] if isinstance(item, dict)]
    own = next((item for item in parts if item.get("analysis_unit_id") == own_id), None)
    excluded = {_path_key(item) for item in _excluded_inputs(parent)}
    candidates = [_path_key(item) for item in parent.get("input_candidates") or []]
    parent_inputs = sorted(item for item in candidates if item not in excluded)
    left_out = len(candidates) - len(parent_inputs)
    claimed = sorted(_path_key(path) for item in parts for path in item.get("input_candidates") or [])
    own_inputs = sorted(_path_key(item) for item in provenance.get("input_candidates") or [])
    problems = []
    if parent.get("status") != "split_by_acquisition" or parent.get("execution_allowed") is not False:
        problems.append("the parent is not recorded as split and held from execution")
    if own is None:
        problems.append(f"the parent's split_into does not name {own_id or 'this part'}")
    elif sorted(_path_key(item) for item in own.get("input_candidates") or []) != own_inputs:
        problems.append("this part's input_candidates differ from the parent's record of it")
    held = {_path_key(path): Path(str(path).rstrip("\\/")).name for item in parts
            for path in item.get("input_candidates") or [] if _path_key(path) in excluded}
    if held:
        problems.append(f"the parts hold {len(held)} input(s) the parent's campaign disposition excluded "
                        f"({', '.join(list(held.values())[:5])})")
    if claimed != parent_inputs:
        problems.append(f"the parts hold {len(claimed)} inputs, {len(set(claimed))} distinct, "
                        f"against the parent's {len(parent_inputs)}"
                        + (f" left when its campaign disposition excluded {left_out}" if left_out else ""))
    # The part reads its raw tree through raw_owned_by and raw_directory, which RET-1 and DSK-1
    # follow. They must name the same parent as split_from, or those checks judge another unit's disk.
    if not _same_path(provenance.get("raw_owned_by") or "", split_from.get("manifest_path") or ""):
        problems.append("raw_owned_by does not name the parent that split_from names")
    parent_raw = parent.get("raw_directory")
    if not parent_raw or not _same_path(provenance.get("raw_directory") or "", parent_raw):
        problems.append("raw_directory is not the parent's raw directory")
    elif any(not _path_key(item).startswith(_path_key(parent_raw) + os.sep) for item in own_inputs):
        problems.append("some of this part's inputs lie outside the parent's raw directory")
    if problems:
        report.add("SPL-1", stage, title, FAIL, "; ".join(problems) + ".",
                   parent=split_from.get("analysis_unit_id"), part=own_id)
        return
    report.add("SPL-1", stage, title, PASS,
               f"{own_id} is one of {len(parts)} parts holding the parent's {len(parent_inputs)} "
               "inputs exactly once"
               + (f", the {left_out} its campaign disposition excluded in none" if left_out else "") + ".",
               parent=split_from.get("analysis_unit_id"), parts=len(parts),
               **({"excluded_inputs": left_out} if left_out else {}))


# --------------------------------------------------------------------------------------------
# the sample-count invariant
# --------------------------------------------------------------------------------------------

def _mdpeak_count(output: Path) -> int:
    return len(list(output.glob("*.mdpeak")))


# The exports the Console writes one per input file. Anything else in expected_analysis_exports is
# written once per run, such as the automatic RT correction audit TSVs (MsdialWorkbench #810).
PER_FILE_EXPORT_SUFFIXES = (".mdpeak", ".mdscan")


# Statuses the Interactive writes only after a production run was attempted.
ATTEMPTED_STATUSES = frozenset({
    "run_failed", "validation_failed", "mztab_validated", "completed",
    "cleanup_pending_confirmation", "raw_cleaned",
})
VALIDATED_STATUSES = frozenset({"mztab_validated", "completed", "cleanup_pending_confirmation", "raw_cleaned"})


def _unvalidated(record: dict, output: Path) -> str:
    """Why the unit's records and output do not show a validated mzTab-M, or "" when they do.

    B7's fact, in order: a validated terminal status, a finalisation, a validation record with no
    failure, and an mzTab-M in output.
    """
    status = str(record.get("status") or "")
    validation = record.get("mztab_validation")
    summary = validation.get("summary") if isinstance(validation, dict) else None
    if status not in VALIDATED_STATUSES:
        return f"its status {status or 'unrecorded'!r} is not a validated one"
    if not record.get("finalized_at"):
        return "its run was never finalised"
    if not isinstance(validation, dict):
        return "no mzTab-M validation is recorded"
    if isinstance(summary, dict) and summary.get("failed"):
        return f"its mzTab-M validation failed {summary.get('failed')} file(s)"
    if not any(output.glob("*.mzTab")):
        return "no mzTab-M is in its output"
    return ""


def _validated_nothing(validation: object) -> str:
    """Why a validation record that counts no failure still validated no mzTab-M, or "" when it did one.

    Interactive (mztab_validation.validate_mztab_files) counts the files it checked in summary.file_count,
    and finalisation calls a run validated when there was at least one and none failed; with no file the
    summary says warning, and a status of failed is a failure whatever the counts say. A record without
    file_count - none Interactive wrote - is taken at its passed count or its status.
    """
    record = validation if isinstance(validation, dict) else {}
    summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
    if "failed" in (str(record.get("status") or ""), str(summary.get("status") or "")):
        return "its mzTab-M validation's status is failed"
    checked = summary.get("file_count")
    if isinstance(checked, int) and not isinstance(checked, bool):
        return "" if checked > 0 else "its mzTab-M validation checked no file (file_count 0)"
    if isinstance(record.get("files"), list):
        return "" if record["files"] else "its mzTab-M validation lists no file it checked"
    passed = summary.get("passed")
    if isinstance(passed, int) and not isinstance(passed, bool) and passed > 0:
        return ""
    if "passed" in (str(record.get("status") or ""), str(summary.get("status") or "")):
        return ""
    return "its mzTab-M validation records no file it checked"


def _production_started(output: Path, provenance: dict | None = None) -> bool:
    """Whether a production run was attempted in this workspace.

    The Console writes method.keys.json when it reads the method file and a .mdpeak per file it
    processed; a finished run adds an mzTab-M and the publication report. The manifest records an
    attempt too: a failure record, a finalisation, or a status only a run can produce. A Console
    that crashed before writing anything leaves the output empty and the failure in the manifest,
    and that is a failed run, not one that never started.

    With none of these, a check owed by a later stage has nothing to judge: that is a stage not
    reached, not two records disagreeing. It stays required, so --strict still refuses the unit.
    """
    if (
        (output / "method.keys.json").is_file()
        or _mdpeak_count(output) > 0
        or any(output.glob("*.mzTab"))
        or (output / "MS_DIAL_publication_report.json").is_file()
    ):
        return True
    record = provenance if isinstance(provenance, dict) else {}
    return bool(
        _run_failures(record)
        or record.get("finalized_at")
        or str(record.get("status") or "") in ATTEMPTED_STATUSES
    )


def _run_failures(provenance: dict | None) -> list[dict]:
    record = provenance if isinstance(provenance, dict) else {}
    failures = record.get("run_failures")
    return [item for item in failures if isinstance(item, dict)] if isinstance(failures, list) else []


def _recorded_failure(provenance: dict | None) -> str:
    """The manifest's account of the latest run as failed, or "" when the latest run did not fail.

    run_failures is a history the Interactive appends to and never clears, so a unit that failed once
    and then ran again is not failed: only a run_failed status says the latest attempt failed.
    """
    record = provenance if isinstance(provenance, dict) else {}
    failures = _run_failures(record)
    if str(record.get("status") or "") != "run_failed" and not _failure_is_latest(record, failures):
        return ""
    code = (failures[-1] if failures else {}).get("exit_code")
    return (f"The run was recorded as failed ({len(failures)} failure(s) recorded"
            + (f", last exit code {code}" if code is not None else "") + ")")


def _instant(value: object):
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _failure_is_latest(record: dict, failures: list[dict]) -> bool:
    """Whether the last recorded failure came after the last finalisation (or there was none).

    A preflight rewrites status, so a unit that failed and was preflighted again carries
    preflight_passed over a failure nothing has since superseded. Timestamps carry offsets and are
    compared as instants, not as strings.
    """
    if not failures:
        return False
    failed_at = _instant(failures[-1].get("recorded_at"))
    finalized_at = _instant(record.get("finalized_at")) if record.get("finalized_at") else None
    if finalized_at is None:
        return True
    return failed_at is not None and failed_at > finalized_at


def _earlier_failures(provenance: dict | None) -> str:
    count = len(_run_failures(provenance))
    return f" {count} earlier failed attempt(s) are recorded." if count and not _recorded_failure(provenance) else ""


NOT_STARTED = (
    "No production-run artifact is present (no method.keys.json, .mdpeak, mzTab-M or publication "
    "report) and the manifest records no attempt: no run has started here, or one failed before "
    "writing anything and was not recorded."
)


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

    RUN POLICY: blocks_run before production, as the user named it (2026-10-01): a run that would
    analyse another number of samples than were found describes a study nobody approved. Its later
    instances have no run left to stop.
    """
    counts: dict[str, int] = {}
    missing: list[str] = []
    run_level: list[str] = []
    held: list[str] = []

    if provenance is not None and isinstance(provenance.get("input_candidates"), list):
        # Less the candidates a campaign disposition excluded, which no stage after it runs (INP-1).
        held = _excluded_candidates(provenance, provenance["input_candidates"])
        counts["repository_manifest.input_candidates"] = len(provenance["input_candidates"]) - len(held)
    else:
        missing.append(provenance_reason or "the repository manifest has no input_candidates")

    if csv_rows is not None:
        counts["analysis_files.csv rows"] = len(csv_rows)
    else:
        missing.append(csv_reason)

    if run_manifest is not None and isinstance(run_manifest.get("source_files"), list):
        counts["run_manifest.source_files"] = len(run_manifest["source_files"])

    if stage in ("after-run", "before-publish"):
        if not _production_started(output, provenance):
            report.add("CNT-1", stage, "The approved sample count survived every stage",
                       NOT_EVALUABLE, NOT_STARTED, counts=counts)
            return None
        counts[".mdpeak files produced"] = _mdpeak_count(output)
        if run_manifest is not None and isinstance(run_manifest.get("expected_analysis_exports"), list):
            # A per-file export is a sample count; a run-level one is not. Interactive lists the
            # automatic RT correction audit TSVs among the expected exports, and counting them as
            # samples refused every run that used it. EXP-1 still requires every one of them.
            exports = [str(item) for item in run_manifest["expected_analysis_exports"]]
            per_file = [item for item in exports if item.casefold().endswith(PER_FILE_EXPORT_SUFFIXES)]
            run_level = [Path(item).name for item in exports if item not in per_file]
            counts["run_manifest.expected_analysis_exports"] = len(per_file)

    excluded = {"excluded_input_candidates": [Path(item.rstrip("\\/")).name for item in held[:10]]} if held else {}
    less = f", the input candidates less the {len(held)} the campaign disposition excluded," if held else ""
    if len(counts) < 2:
        report.add("CNT-1", stage, "The approved sample count survived every stage", NOT_EVALUABLE,
                   "; ".join(missing) or "fewer than two independent counts are available",
                   counts=counts, **excluded)
        return None

    distinct = set(counts.values())
    if len(distinct) == 1:
        value = distinct.pop()
        report.add("CNT-1", stage, "The approved sample count survived every stage", PASS,
                   f"All {len(counts)} independent records{less} agree on {value} samples.", counts=counts,
                   run_level_exports=run_level, **excluded)
        return value

    report.add(
        "CNT-1", stage, "The approved sample count survived every stage", FAIL,
        "Independent records of the same study disagree on how many samples it contains. Whichever "
        "is right, at least one retained artifact describes a study that was not run."
        + (f" The input candidates are counted less the {len(held)} the campaign disposition excluded."
           if held else ""),
        counts=counts, run_level_exports=run_level, **excluded,
    )
    return None


INP1_TITLE = "Each declared analysis input is one input candidate and one CSV row"


def _declared_inputs(project: dict) -> tuple[list | None, str]:
    """The analysis inputs a unit's project declares, or None; and what contradicts the declaration.

    The Catalog's handoff carries the list with its count and a flag saying whether the listing named
    the inputs at all. A declaration with a count or the flag and no list is a list lost on the way in:
    the handoff's own response omits it, and only its file holds it.
    """
    inputs = project.get("analysis_inputs")
    count = project.get("analysis_input_count")
    count = count if isinstance(count, int) and not isinstance(count, bool) else None
    if not isinstance(inputs, list) or not inputs:
        if project.get("analysis_inputs_declared") is True or (count or 0) > 0:
            return [], (f"the manifest declares {count if count else 'its'} analysis input(s) and lists none, "
                        "so the list was lost on the way in")
        return None, ""
    if count is not None and count != len(inputs):
        return inputs, f"the manifest counts {count} analysis input(s) and lists {len(inputs)}"
    return inputs, ""


def _binding_disposition(manifest: dict | None) -> dict:
    """A unit's campaign disposition where it binds the run, else {}.

    Interactive's classify_preflight records a disposition for every preflighted unit and applies it
    only to a campaign unit, saying which (applied); its execution gate ignores one that was not
    applied, and so does this. A disposition that does not say, as the shared contract's v1 shape
    does not, binds.
    """
    disposition = (manifest or {}).get("campaign_disposition")
    return disposition if isinstance(disposition, dict) and disposition.get("applied") is not False else {}


def _excluded_inputs(manifest: dict | None) -> list[str]:
    """The inputs a unit's binding campaign disposition excluded, by path, each once."""
    excluded = _binding_disposition(manifest).get("excluded_inputs")
    paths = {_path_key(item["path"]): str(item["path"]) for item in excluded
             if isinstance(item, dict) and str(item.get("path") or "").strip()} if isinstance(excluded, list) else {}
    return list(paths.values())


def _exclusion_reasons(*manifests: dict | None) -> dict[str, tuple[str, str]]:
    """The inputs these manifests' binding campaign dispositions excluded, by key: (path, reason)."""
    reasons: dict[str, tuple[str, str]] = {}
    for manifest in manifests:
        excluded = _binding_disposition(manifest).get("excluded_inputs")
        for item in excluded if isinstance(excluded, list) else []:
            if isinstance(item, dict) and str(item.get("path") or "").strip():
                reasons.setdefault(_path_key(item["path"]), (str(item["path"]), str(item.get("reason") or "")))
    return reasons


def _lease_excluded(manifest: dict | None) -> dict[str, tuple[str, str]]:
    """The inputs a unit's lease excluded itself, by key: (path, reason).

    Interactive 0.5.18 keeps an mzML whose arrays RawDataHandler cannot decode (unsupported_mzml_encoding)
    out of the input candidates and lists it in excluded_input_candidates and among input_lineage's
    excluded rows; the rest of the unit runs without it. It was declared and downloaded, and is no
    candidate and no CSV row. Both records are the lease's, and each names the same inputs.
    """
    manifest = manifest or {}
    recorded = manifest.get("excluded_input_candidates")
    sources = [recorded if isinstance(recorded, list) else [], _lineage_rows(manifest, "excluded")]
    excluded: dict[str, tuple[str, str]] = {}
    for source in sources:
        for item in source:
            if isinstance(item, dict) and str(item.get("path") or "").strip():
                exclusion = item.get("exclusion") if isinstance(item.get("exclusion"), dict) else {}
                reason = str(item.get("reason") or exclusion.get("reason") or "")
                excluded.setdefault(_path_key(item["path"]), (str(item["path"]), reason))
    return excluded


def _not_analysed(provenance: dict, owner: dict, csv_rows: list[dict] | None) -> dict[str, tuple[str, str]]:
    """The raw owner's input candidates a binding campaign disposition excluded and no CSV row opens.

    By key, with the path and the disposition's reason. The dispositions are the raw owner's and, for a
    split part, the part's own, as _excluded_candidates reads them. Interactive leaves such an input
    among the candidates and gives it no row; a row that opens it all the same, through its Console
    alias or not, makes it analysed, and it is then held to everything any input is.

    Which input a row opens is read by _inputs_opened, and that no row opens an excluded input is said
    only where each row is known to open one of the unit's candidates. A row compared by its spelling
    alone said nothing of the rest: one that opened the excluded input through a link the lineage does
    not record, or through a \\\\?\\ prefix, left it unheld, and SUM-1 passed an input nothing vouches
    for. A row the gate cannot tie to a candidate may open any of them, so then no excluded input is
    taken as unopened, and each is held to its checksum.

    Nor is it said without the CSV. An analysis CSV that is absent, or that the gate cannot read (csv_rows
    None), has no row to tie to anything, and the Console may still read a file this reader refused, so
    every excluded input is held as if a row opened it. Reading the missing rows as none excused them all:
    SUM-1 passed an excluded input nothing vouches for while INP-1 and CNT-1, which read the same CSV,
    were not evaluable, and no check that stops the run said a word.
    """
    if csv_rows is None:
        return {}
    reasons = _exclusion_reasons(owner, provenance)
    if not reasons:
        return {}
    candidates = owner.get("input_candidates") if isinstance(owner.get("input_candidates"), list) else []
    own = provenance.get("input_candidates") if isinstance(provenance.get("input_candidates"), list) else []
    opened = set(_inputs_opened(csv_rows, _input_keys_by_console_path(provenance), [*candidates, *own]))
    if "" in opened:
        return {}
    return {_path_key(item): reasons[_path_key(item)] for item in candidates
            if _path_key(item) in reasons and _path_key(item) not in opened}


def _inputs_opened(csv_rows: list[dict], aliases: dict[str, str], inputs: list) -> list[str]:
    """The key of the input each CSV row opens, of these inputs, row by row; "" for a row that opens none.

    A row opens the input its path names, or the one its Console alias stands for (_input_key). Compared
    as spelt, a row opening an input under a name the lineage does not record, a hard link or a \\\\?\\
    prefix, opens nothing known, so while both are on disk a row that is an input's file record
    (_record_of) opens that input whatever it is called. A row that is none of them, or one the gate can
    no longer compare once the files are gone, is "".
    """
    keys = {_path_key(item) for item in inputs}
    records: "dict[tuple[int, int], str] | None" = None
    opened = []
    for row in csv_rows:
        key = _input_key(row, aliases)
        if key in keys:
            opened.append(key)
            continue
        if records is None:
            # Only for a row its spelling does not tie to an input, so a unit whose rows all do stats nothing.
            records = {}
            for item in inputs:
                record = _record_of(item)
                if record is not None:
                    records.setdefault(record, _path_key(item))
        record = _record_of(row.get("file_path")) if key else None
        opened.append(records.get(record, "") if record is not None else "")
    return opened


def _record_of(path: object) -> "tuple[int, int] | None":
    """The file record a path names (_file_record), or None for one that is not on disk or cannot be read."""
    try:
        return _file_record(os.stat(str(path)))
    except (OSError, ValueError):
        return None


def _excluded_sentence(not_analysed: dict[str, tuple[str, str]]) -> str:
    """SUM-1's account of the inputs it did not hold to a checksum, with the disposition's reasons."""
    names = [Path(path.rstrip("\\/")).name + " (" + (reason or "no reason recorded") + ")"
             for path, reason in list(not_analysed.values())[:5]]
    return (f" {len(not_analysed)} input candidate(s) the campaign disposition excluded are never analysed, so "
            f"no checksum was required of them: {', '.join(names)}.")


def _excluded_candidates(provenance: dict, candidates: list) -> list[str]:
    """The input candidates a campaign disposition excluded: the unit's own, and a split part's parent's.

    Interactive leaves an excluded input among the input candidates, where the disposition found it,
    and keeps it out of the analysis CSV and out of every split part. So the CSV holds the candidates
    less these; an excluded input that is no candidate takes nothing from them.
    """
    manifests = [provenance]
    if isinstance(provenance.get("split_from"), dict):
        parent, _ = _raw_owner_manifest(provenance)
        manifests.append(parent)
    keys = {_path_key(item) for manifest in manifests for item in _excluded_inputs(manifest)}
    return [str(item) for item in candidates if _path_key(item) in keys]


# UNATTRIBUTED MEMBERS (user decision, 2026-10-07; Interactive 0.5.31). A unit whose archive is unit-scoped
# (every download object its scope lists is claimed by this unit alone, or the Catalog scoped the download to
# the unit's own files, kind "unit_files") is analysed "even by force": the archive members no sample row pairs
# with run as inputs of their own, attributed to no sample, and always on record. ST001264 has 31 members and 31
# sample rows, and only the three BioRec rows pair; the 28 "..._Youn_saN.raw" members are included so.
# Interactive records each as an input_lineage row whose name_pairing is {"paired_by": "unattributed_member",
# "member_name": <name>}, with the member's stem as sample_id and sample_row null; its analysis-CSV row has the
# unit's abstention Class where the Class decision is an abstention, else UNATTRIBUTED_CLASS, as a Sample; the
# manifest's unattributed_members gives {"count", "members", "paths", "rule"} (members the basenames, as each
# name_pairing.member_name is; paths the '/'-separated paths under the unit's raw data root, its parent's for a
# split part: the agreed contract of 2026-10-07); and the manifest's and the disposition's
# warnings carry UNATTRIBUTED_WARNING. INP-1, CLS-1, CLS-2, CLS-3 and PAIR-1 read them as explained inputs:
# never a FAIL for being there, always a WARN that lists them, and never an approved sample.
#
# WHAT THE LEASE LEAVES OUT, AND WHAT IT CONVERTS (user decision, 2026-10-08, second round, answer 3; Interactive
# 0.5.36, msdial-interactive-app#69). In a campaign an unpaired mzXML is converted like any other mzXML input (the
# rule of 2026-09-30): the mzML written from it is the unattributed input, its lineage row a conversion
# (source.conversion.source_path the mzXML) whose name_pairing.member_name is the mzXML's basename, and
# unattributed_members.converted lists the mzXML by its path under the data root. What the lease leaves out of the
# unit, and no sample's choice of encoding accounts for, is in unattributed_members.left_out {member_name, path,
# reason}, its path under the data root: opposite-polarity names (polarity_token_contradicts_ion_mode), a shared
# archive's members of no admitted file's stem (2026-10-08, second round, answer 1), in a declared unit each member
# no declaration names (not_named_by_the_catalog_declaration, second round, answer 3), and an unattributed member of
# a stem two sample rows share (stem_of_several_sample_rows). INP-1 holds the run to that record
# (_unattributed_choice_problems, blocks_run): no member the record leaves out reaches the run, and a converted
# unattributed input is the conversion of the mzXML it names. PAIR-1 holds the record itself (record_only):
# converted lists every converted member, and each left-out entry gives a reason. Which one of a sample's
# encodings runs is the one encoding rule's (below), never left_out's.
UNATTRIBUTED_PAIRING = "unattributed_member"
UNATTRIBUTED_WARNING = "unattributed_members_included"
UNATTRIBUTED_RULE = "unit_scoped_archive_2026_10_07"
UNATTRIBUTED_CLASS = "Unattributed"
UNATTRIBUTED_RECORD = "unattributed_members"
UNIT_SCOPED_DOWNLOAD_KIND = "unit_files"
UNATTRIBUTED_LEFT_OUT = "left_out"
UNATTRIBUTED_CONVERTED = "converted"
UNSUPPORTED_MZML_ENCODING = "unsupported_mzml_encoding"
# The left_out reasons that are no reason a file could not be read (Interactive's _unattributed_members,
# _members_no_declaration_names and _encoding_groups): each removes a file from the unit only where what it says holds
# of that file (_left_out_removal_problem).
POLARITY_TOKEN_CONTRADICTS_ION_MODE = "polarity_token_contradicts_ion_mode"
NOT_NAMED_BY_DECLARATION = "not_named_by_the_catalog_declaration"
STEM_OF_SEVERAL_SAMPLE_ROWS = "stem_of_several_sample_rows"
#
# ONE ENCODING PER SAMPLE (the user's one rule of 2026-10-09, "A: この一つのルールで統一"; Interactive 0.5.36,
# msdial-interactive-app#69, msdial_app.encoding_rule). It supersedes every earlier case-by-case answer about a
# sample whose data arrive more than once (the readable twin of an undecodable mzML of 2026-10-08, the encoding
# order between unpaired members, a copy nearest the data root), and the gate's checks of them are gone with them.
# When one sample's data arrive in several encodings (S1.raw, S1.mzML, S1.mzXML; copies in other folders included):
#   1. among the READABLE ones exactly one is used: the highest in the order vendor format (a folder or container)
#      -> mzML -> mzXML (converted to mzML in a campaign; outside a campaign an mzXML is no input);
#   2. a tie (the same rank) goes to the first by path name in lexicographic order: the path relative to the data
#      root, '/'-separated, compared without case;
#   3. where the chosen one cannot be read or decoded, or its conversion fails, the next in order is taken;
#   4. the file used is that sample's own input, paired to its sample row and in its Class, whatever encoding the
#      sample row names;
#   5. every file not used is recorded with its reason, naming the file that was used.
# A re-encoding MS-DIAL opens that no instrument writes (.cdf, .abf, .ibf) is in none of the rule's ranks; Interactive
# ranks it after every vendor format and before mzML, where it stood before the rule (outside the rule's words, the
# existing behaviour kept), and so does the gate (ENCODING_RE_ENCODING_RANK).
#
# Interactive records it twice. manifest.encoding_choices lists each sample the rule chose for among more than one
# candidate, {rule ENCODING_RULE, used, unused: [{path, reason}]}, paths relative to the data root; used is null
# where no candidate could be read (each then keeps the exclusion the lease records for it). The lineage row of each
# input used carries the same choice as encoding_choice, with stands_for (the absolute path of the file its sample
# row pairs with) where the row names another file of the sample; a split part carries its own samples' choices. An
# unused file is no input and no excluded candidate: its sample's choice is its only record.
#
# The gate recomputes the rule from that record (_encoding_rule_problems): it orders a choice's candidates by the
# rule (rank, then path without case) and holds that every candidate before the one used carries a reason it could
# not be read (ENCODING_UNREADABLE_REASONS, each on the encoding it applies to: ENCODING_REASON_RANKS), every one
# after it lower_in_encoding_order or tie_lexicographic as its rank against the used one gives (or its own reason it
# could not be read, as Interactive keeps where only a header stood in the way), and, where none was used, every
# candidate a reason it could not be read. INP-1 (blocks_run) holds the run to it (_encoding_run_problems): the
# record follows the rule, the file used is what runs for its sample (a converted mzML through the mzXML it was
# converted from), runs as the sample its rows name or a pairing rule paired with its files, never unattributed, and
# no unused file reaches the run, a split part's raw owner's choices included; and a run that departs from the rule
# without the record (two encodings of one sample that both run, an input beside a lease-excluded encoding of its own
# sample that no choice records, or a listed archive member of a running input's sample that no record names) is
# refused as well. CONV-1 holds each choice to the conversions: an mzXML used is a completed
# conversion, and one unused as conversion_failed is one whose conversion did not complete. PAIR-1 (record_only)
# lists every choice and holds the record itself (_encoding_record_problems).
ENCODING_RULE = "one_encoding_per_sample_2026_10_09"
ENCODING_CHOICES = "encoding_choices"
ENCODING_CHOICE = "encoding_choice"
LOWER_IN_ENCODING_ORDER = "lower_in_encoding_order"
TIE_LEXICOGRAPHIC = "tie_lexicographic"
ENCODING_ORDER_REASONS = frozenset({LOWER_IN_ENCODING_ORDER, TIE_LEXICOGRAPHIC})
CONVERSION_FAILED = "conversion_failed"
REQUIRES_CONVERSION = "requires_conversion"
RAW_HEADER_REASONS = frozenset({"raw_header_unreadable", "raw_header_unsupported_format"})
# Why a candidate could not be read (clause 3), as encoding_rule names it: an mzML RawDataHandler cannot decode, an
# mzXML whose conversion failed or whose scans contradict the declared polarity, an mzXML outside a campaign, a
# listed vendor folder that did not arrive whole, and a vendor header the raw-metadata extractor could not read.
ENCODING_UNREADABLE_REASONS = frozenset({
    "undecodable", CONVERSION_FAILED, "polarity_contradicts_declaration", REQUIRES_CONVERSION,
    "incomplete_container", *RAW_HEADER_REASONS})
# The preflight's warning where a vendor file the rule used was excluded for a header it could not read, and the
# sample's next encoding was not taken (the lease read no header for the rule).
ENCODING_FALLBACK_NOT_TAKEN = "encoding_fallback_not_taken"
# The rule's ranks (encoding_rule.encoding_rank), with the suffixes Interactive's encoding_preference reads.
ENCODING_VENDOR_SUFFIXES = (".raw", ".d", ".wiff", ".wiff2", ".lcd", ".qgd", ".abf", ".ibf", ".cdf", ".lrp")
ENCODING_RE_ENCODING_SUFFIXES = (".cdf", ".abf", ".ibf")
ENCODING_OPEN_SUFFIXES = (".mzml", ".imzml")
ENCODING_UNREADABLE_SUFFIXES = (".mzxml", ".mzdata", ".mgf", ".ibd", ".dat", ".scan")
ENCODING_SIDECAR_SUFFIXES = (".wiff2.scan", ".wiff.scan", ".timeseries.data")
ENCODING_VENDOR_RANK, ENCODING_RE_ENCODING_RANK, ENCODING_MZML_RANK, ENCODING_MZXML_RANK = 0, 1, 2, 3
ENCODING_UNRANKED = 9
# The encodings each reason a candidate could not be read applies to, as Interactive's lease gives them
# (repository_reanalysis._choose_encodings' readability): undecodable to an mzML RawDataHandler scanned;
# conversion_failed, polarity_contradicts_declaration and requires_conversion to an mzXML; incomplete_container and a
# raw-header reason to a vendor file or a re-encoding (a file neither an mzML nor an mzXML). A reason given to a file
# of another encoding passes a readable file over, which clause 3 does not allow.
ENCODING_REASON_RANKS = {
    "undecodable": frozenset({ENCODING_MZML_RANK}),
    CONVERSION_FAILED: frozenset({ENCODING_MZXML_RANK}),
    "polarity_contradicts_declaration": frozenset({ENCODING_MZXML_RANK}),
    REQUIRES_CONVERSION: frozenset({ENCODING_MZXML_RANK}),
    "incomplete_container": frozenset({ENCODING_VENDOR_RANK, ENCODING_RE_ENCODING_RANK}),
    **{reason: frozenset({ENCODING_VENDOR_RANK, ENCODING_RE_ENCODING_RANK}) for reason in RAW_HEADER_REASONS},
}
ENCODING_RANK_NAMES = {ENCODING_VENDOR_RANK: "a vendor format", ENCODING_RE_ENCODING_RANK: "a re-encoding",
                       ENCODING_MZML_RANK: "an mzML", ENCODING_MZXML_RANK: "an mzXML"}
# The pos/neg tokens a path states a polarity by (repository_reanalysis.POLARITY_NAME_TOKENS), and those beside
# which a token in the file name names a sample rather than a polarity (POLARITY_EXEMPTING_TOKENS).
POLARITY_NAME_TOKENS = {"pos": "Positive", "positive": "Positive", "neg": "Negative", "negative": "Negative"}
POLARITY_EXEMPTING_TOKENS = frozenset({"control", "ctrl", "blank", "qc"})


def _is_unattributed(row: object) -> bool:
    """Whether a lineage row is an archive member the lease included unattributed (name_pairing.paired_by)."""
    pairing = row.get("name_pairing") if isinstance(row, dict) else None
    return isinstance(pairing, dict) and str(pairing.get("paired_by") or "") == UNATTRIBUTED_PAIRING


def _basename(name: str) -> str:
    """The last part of a member name, or of a '/'- or backslash-separated path under a data root."""
    return name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _member_name(row: dict) -> str:
    """The member's basename: name_pairing.member_name, as Interactive writes it for every rule, else its path's."""
    pairing = row.get("name_pairing") if isinstance(row.get("name_pairing"), dict) else {}
    return _basename(str(pairing.get("member_name") or "") or str(row["path"]))


def _download_scope(manifest: "dict | None") -> dict:
    project = (manifest or {}).get("project")
    scope = project.get("download_scope") if isinstance(project, dict) else None
    return scope if isinstance(scope, dict) else {}


def _unit_scoped(manifest: "dict | None") -> "tuple[bool | None, str]":
    """Whether the raw owner's download is unit-scoped, as the rule of 2026-10-07 reads the Catalog's scope.
    Shared first, in the order Interactive's unit_scoped_download reads it: any bundle URL or object claimed by
    more than one unit (shared_unit_count above 1, or bundle_shared_unit_count above 1) is a shared archive,
    whatever the scope's kind (False). Then its own: kind "unit_files", or every count the scope records (on its
    bundle_urls, its objects and bundle_shared_unit_count) is 1 (True). None where no scope is recorded, or one
    that records no count for some of its objects and is not unit_files."""
    scope = _download_scope(manifest)
    if not scope:
        return None, "the manifest records no download_scope"

    def count(value: object) -> "int | None":
        if isinstance(value, bool):
            return None
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    listed = [count(item.get("shared_unit_count")) for key in ("bundle_urls", "objects")
              for item in scope.get(key) or [] if isinstance(item, dict)]
    bundle = count(scope.get("bundle_shared_unit_count"))
    numbers = [number for number in [*listed, bundle] if number is not None]
    if any(number > 1 for number in numbers):
        return False, (f"the download is shared with other units (shared_unit_count up to {max(numbers)}), so a "
                       "member no sample row pairs with may be another unit's file")
    if str(scope.get("kind") or "") == UNIT_SCOPED_DOWNLOAD_KIND:
        return True, "the Catalog scoped the download to the unit's own files (unit_files)"
    if numbers and None not in listed:
        return True, "every download object is claimed by this unit alone (shared_unit_count 1)"
    return None, "the download_scope records no shared_unit_count for its objects"


@dataclass
class _Unattributed:
    """The archive members a unit's lease included unattributed (_unattributed_members)."""
    rows: list = field(default_factory=list)       # their lineage rows, inputs and lease-excluded alike
    keys: set = field(default_factory=set)         # their paths, keyed as _path_key keys them
    members: list = field(default_factory=list)    # their member names, in lineage order
    record: "dict | None" = None                   # manifest.unattributed_members, the unit's or its raw owner's
    record_rows: int = 0                           # the unattributed lineage rows of the manifest that holds it
    class_label: str = UNATTRIBUTED_CLASS          # the Class their CSV rows carry
    abstention: bool = False
    left_out: list = field(default_factory=list)   # unattributed_members.left_out, the unit's and its raw owner's
    converted: list = field(default_factory=list)  # the mzXML members converted to their unattributed input

    def __bool__(self) -> bool:
        return bool(self.rows)

    @property
    def recorded_count(self) -> "int | None":
        count = (self.record or {}).get("count")
        return count if isinstance(count, int) and not isinstance(count, bool) else None

    def sentence(self) -> str:
        names = ", ".join(self.members[:5]) + (f", and {len(self.members) - 5} more" if len(self.members) > 5 else "")
        count = self.recorded_count
        text = (f"{len(self.members)} input(s) are archive members no sample row pairs with, included unattributed "
                f"under the rule {UNATTRIBUTED_RULE} (manifest.unattributed_members.count "
                f"{count if count is not None else 'not recorded'}) in Class {self.class_label!r}: {names}")
        if self.converted:
            text += (f"; {len(self.converted)} of them converted from mzXML, as any mzXML input of a campaign is "
                     f"({', '.join(self.converted[:5])})")
        if self.left_out:
            reasons = Counter(str(item.get("reason") or "no reason recorded") for item in self.left_out)
            text += (f"; {len(self.left_out)} other member(s) left out, on record ("
                     + ", ".join(f"{reason} {number}" for reason, number in sorted(reasons.items())) + ")")
        return text

    def evidence(self) -> dict:
        paths = (self.record or {}).get("paths")
        return {"warning": UNATTRIBUTED_WARNING, UNATTRIBUTED_RECORD: {
            "count": self.recorded_count, "lineage_rows": len(self.members), "members": self.members[:10],
            **({"paths": [str(item) for item in paths[:10]]} if isinstance(paths, list) else {}),
            **({UNATTRIBUTED_CONVERTED: self.converted[:10]} if self.converted else {}),
            **({UNATTRIBUTED_LEFT_OUT: _left_out_summary(self.left_out)} if self.left_out else {}),
            "rule": (self.record or {}).get("rule"), "class": self.class_label}}


def _unattributed_members(provenance: "dict | None") -> _Unattributed:
    """The archive members this unit's lease included unattributed: its own lineage rows (_own_lineage_rows, a
    split part's share of its raw owner's) whose name_pairing says so, and the record that counts them, the
    unit's own manifest.unattributed_members or else its raw owner's. The Class their rows carry is the Class of
    the unit's ratified abstention where its Class decision is one, else UNATTRIBUTED_CLASS."""
    result = _Unattributed()
    if not isinstance(provenance, dict):
        return result
    seen: set[str] = set()
    for part in ("rows", "excluded"):
        for row in _own_lineage_rows(provenance, part):
            key = _path_key(row["path"])
            if key not in seen and _is_unattributed(row):
                seen.add(key)
                result.rows.append(row)
                result.keys.add(key)
                result.members.append(_member_name(row))
    holder = provenance
    if not isinstance(provenance.get(UNATTRIBUTED_RECORD), dict) and isinstance(provenance.get("split_from"), dict):
        owner, _ = _raw_owner_manifest(provenance)
        if isinstance(owner, dict) and isinstance(owner.get(UNATTRIBUTED_RECORD), dict):
            holder = owner
    record = holder.get(UNATTRIBUTED_RECORD)
    result.record = record if isinstance(record, dict) else None
    result.left_out = _record_left_out(provenance)
    result.converted = [_member_name(row) for row in result.rows if _conversion_source(row)]
    rows = _own_lineage_rows(provenance, "rows") + _own_lineage_rows(provenance, "excluded") \
        if holder is provenance else _lineage_rows(holder, "rows") + _lineage_rows(holder, "excluded")
    result.record_rows = len({_path_key(row["path"]) for row in rows if _is_unattributed(row)})
    proposal = _class_proposal(provenance)
    contrast = (proposal or {}).get("contrast_definition")
    if isinstance(contrast, dict) and contrast.get("kind") == "abstention" and str(contrast.get("class_label") or ""):
        result.abstention = True
        result.class_label = str(contrast["class_label"])
    return result


def _data_root(provenance: dict) -> str:
    """The unit's raw data root, as the manifest records it (input_directory): a split part's repeats its parent's,
    and a part that does not is read through its raw owner. The root Interactive names members and inputs under
    (unattributed_members.paths and left_out, aif_collision_energies_by_input)."""
    root = str(provenance.get("input_directory") or "").strip()
    if not root and isinstance(provenance.get("split_from"), dict):
        owner, _ = _raw_owner_manifest(provenance)
        root = str((owner or {}).get("input_directory") or "").strip()
    return root


def _conversion_source(row: dict) -> str:
    """The mzXML a converted input's lineage row was read from (source.conversion.source_path), or ""."""
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    conversion = source.get("conversion") if isinstance(source.get("conversion"), dict) else {}
    return str(conversion.get("source_path") or "").strip()


def _record_left_out(provenance: dict) -> list[dict]:
    """unattributed_members.left_out, of the unit's own record and of its raw owner's, once per path and reason:
    each entry with its path under the data root ('/'-separated) and its reason, as Interactive writes them."""
    manifests = [provenance]
    if isinstance(provenance.get("split_from"), dict):
        owner, _ = _raw_owner_manifest(provenance)
        if isinstance(owner, dict):
            manifests.append(owner)
    entries: list[dict] = []
    seen: set = set()
    for manifest in manifests:
        record = manifest.get(UNATTRIBUTED_RECORD)
        listed = record.get(UNATTRIBUTED_LEFT_OUT) if isinstance(record, dict) else None
        for item in listed if isinstance(listed, list) else []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or item.get("member_name") or "").replace("\\", "/").strip().strip("/")
            mark = (path.casefold(), str(item.get("reason") or ""))
            if path and mark not in seen:
                seen.add(mark)
                entries.append({**item, "path": path})
    return entries


def _left_out_summary(left_out: list[dict]) -> dict:
    """The evidence's account of the members the lease left out: how many for each reason, and the first ten."""
    return {"count": len(left_out),
            "reasons": dict(sorted(Counter(str(item.get("reason") or "") for item in left_out).items())),
            "members": [{key: item[key] for key in ("path", "reason") if key in item} for item in left_out[:10]]}


def _reaching_rows(provenance: dict, csv_rows: "list[dict] | None") -> "tuple[list[dict], set[str]]":
    """The unit's own input lineage rows that reach the run (a split part's share of its raw owner's), those the
    analysis CSV opens where there is one, each once; and the keys of what reaches it, the CSV's included."""
    rows = _own_lineage_rows(provenance, "rows")
    opened: "set[str] | None" = None
    if csv_rows is not None:
        aliases = _input_keys_by_console_path(provenance)
        opened = {_input_key(row, aliases) for row in csv_rows}
        rows = [row for row in rows if _path_key(row["path"]) in opened]
    seen: set[str] = set()
    unique = []
    for row in rows:
        key = _path_key(row["path"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique, seen | (opened or set())


def _unattributed_choice_problems(provenance: dict, csv_rows: "list[dict] | None",
                                  unattributed: _Unattributed) -> "tuple[list[str], dict]":
    """Why the inputs that reach the run are not what Interactive's record of an archive's unpaired members lets
    reach it (the second-round answer 3 of 2026-10-08), and the evidence; ([], {}) where the unit has neither
    unattributed members nor a record of members left out.

    The inputs are the unit's own input lineage rows that reach the run (_reaching_rows), each named by the member it
    is under the data root (_data_root): a converted input by the mzXML its conversion read. Two refusals:

    - a member unattributed_members.left_out leaves out reaches the run: whatever the reason (an opposite-polarity
      name, a shared archive's member, a member no declaration names, an mzXML not converted), the record says it is
      no input of the unit;
    - a converted unattributed input is not the conversion of the mzXML its name_pairing names (member_name is the
      basename of its conversion's source_path, an mzXML).

    Which one of a sample's encodings runs is the one encoding rule's, held by _encoding_run_problems."""
    left_out = unattributed.left_out
    if not unattributed and not left_out:
        return [], {}
    root = _data_root(provenance)
    rows, _reaching = _reaching_rows(provenance, csv_rows)
    inputs = [(row, _aif_input_key(_conversion_source(row) or row["path"], root), _conversion_source(row))
              for row in rows]
    problems: list[str] = []
    out_of = {}
    for item in left_out:
        out_of.setdefault(item["path"].casefold(), item)
    reached = [f"{member} ({out_of[member.casefold()].get('reason') or 'no reason recorded'})"
               for _row, member, _source in inputs if member.casefold() in out_of]
    if reached:
        problems.append(f"{len(reached)} input(s) are archive members that {UNATTRIBUTED_RECORD}.{UNATTRIBUTED_LEFT_OUT} "
                        f"leaves out, and they reach the run all the same: {'; '.join(reached[:5])}")
    misnamed: list[str] = []
    for row, _member, source in inputs:
        if not source or not _is_unattributed(row):
            continue
        named, read = _member_name(row), _basename(source)
        if not read.casefold().endswith(".mzxml"):
            misnamed.append(f"{Path(str(row['path'])).name} is converted from {read}, which is no mzXML")
        elif named.casefold() != read.casefold():
            misnamed.append(f"{Path(str(row['path'])).name} is converted from {read}, and its name_pairing names "
                            f"{named}")
    if misnamed:
        problems.append(f"{len(misnamed)} converted unattributed input(s) are not the conversion of the member they "
                        f"name: {'; '.join(misnamed[:5])}")
    evidence = {"inputs_read": len(inputs),
                "converted_unattributed_inputs": sum(1 for row, _m, source in inputs if source and _is_unattributed(row))}
    if left_out:
        evidence[UNATTRIBUTED_LEFT_OUT] = _left_out_summary(left_out)
    return problems, evidence


def _unattributed_csv_names(provenance: "dict | None", csv_rows: "list[dict] | None",
                            unattributed: "_Unattributed | None" = None) -> list[str]:
    """The file_name of each analysis-CSV row that opens an unattributed member, through its Console alias where
    it has one (_input_key), in CSV order."""
    unattributed = unattributed if unattributed is not None else _unattributed_members(provenance)
    if not unattributed or not csv_rows:
        return []
    aliases = _input_keys_by_console_path(provenance)
    return [str(row.get("file_name") or "") for row in csv_rows if _input_key(row, aliases) in unattributed.keys]


def _left_out_record_problems(record: "dict | None") -> list[str]:
    """Why an unattributed_members record's left_out does not say why it leaves out each member, or []: each entry
    gives a reason. Read wherever the record has a left_out: beside unattributed members, and in a record that takes
    none (a shared archive, a declared unit, a stem two sample rows share)."""
    if not isinstance(record, dict):
        return []
    problems: list[str] = []
    listed_out = record.get(UNATTRIBUTED_LEFT_OUT)
    if listed_out is not None and not isinstance(listed_out, list):
        problems.append(f"{UNATTRIBUTED_RECORD}.{UNATTRIBUTED_LEFT_OUT} is no list")
    unexplained: list[str] = []
    for item in listed_out if isinstance(listed_out, list) else []:
        entry = item if isinstance(item, dict) else {}
        name = str(entry.get("path") or entry.get("member_name") or "").strip() or "an entry naming no member"
        if not str(entry.get("reason") or "").strip():
            unexplained.append(f"{name} (no reason)")
    if unexplained:
        problems.append(f"{UNATTRIBUTED_RECORD}.{UNATTRIBUTED_LEFT_OUT} does not say why it leaves out "
                        f"{'; '.join(unexplained[:5])}")
    return problems


def _unattributed_record_problems(provenance: dict, unattributed: _Unattributed) -> list[str]:
    """What keeps the unattributed members off the record the rule of 2026-10-07 requires, or []."""
    problems = []
    record = unattributed.record
    if record is None:
        problems.append(f"the manifest records no {UNATTRIBUTED_RECORD}")
    else:
        if unattributed.recorded_count != unattributed.record_rows:
            problems.append(f"{UNATTRIBUTED_RECORD}.count is {unattributed.recorded_count!r}, and the lineage holds "
                            f"{unattributed.record_rows} unattributed member(s)")
        if str(record.get("rule") or "") != UNATTRIBUTED_RULE:
            problems.append(f"{UNATTRIBUTED_RECORD}.rule is {str(record.get('rule') or '') or 'none'!r}, not "
                            f"{UNATTRIBUTED_RULE}")
        listed = record.get("members")
        if isinstance(listed, list):
            # Basenames on both sides (the agreed contract of 2026-10-07): members lists them, as each lineage
            # row's name_pairing.member_name does; the paths under the data root are unattributed_members.paths.
            recorded = {_basename(str(item)) for item in listed}
            missing = [name for name in unattributed.members if _basename(name) not in recorded]
            if missing:
                problems.append(f"{UNATTRIBUTED_RECORD}.members does not list {', '.join(missing[:5])}")
        else:
            problems.append(f"{UNATTRIBUTED_RECORD}.members is no list")
        # The mzXML members converted to their unattributed input (2026-10-08, second round, answer 3): listed in
        # converted by their path under the data root, compared here by basename, as members is.
        converted_rows = [row for row in unattributed.rows if _conversion_source(row)]
        listed_converted = record.get(UNATTRIBUTED_CONVERTED)
        if listed_converted is not None and not isinstance(listed_converted, list):
            problems.append(f"{UNATTRIBUTED_RECORD}.{UNATTRIBUTED_CONVERTED} is no list")
        else:
            said = {_basename(str(item)).casefold() for item in listed_converted or []}
            read = {_basename(_conversion_source(row)).casefold() for row in converted_rows}
            unlisted = [_basename(_conversion_source(row)) for row in converted_rows
                        if _basename(_conversion_source(row)).casefold() not in said]
            if unlisted:
                problems.append(f"{UNATTRIBUTED_RECORD}.{UNATTRIBUTED_CONVERTED} does not list "
                                f"{', '.join(unlisted[:5])}, converted to an unattributed input")
            # Only a record of the unit's own lists nothing beyond its own rows: a split part that carries none reads
            # its raw owner's, which lists every part's.
            own_record = record is provenance.get(UNATTRIBUTED_RECORD)
            unread = [str(item) for item in listed_converted or []
                      if own_record and _basename(str(item)).casefold() not in read]
            if unread:
                problems.append(f"{UNATTRIBUTED_RECORD}.{UNATTRIBUTED_CONVERTED} lists {', '.join(unread[:5])}, which "
                                "no converted unattributed input in the lineage was read from")
        problems.extend(_left_out_record_problems(record))
    owner, _ = _raw_owner_manifest(provenance)
    warned = [manifest for manifest in (provenance, owner) if isinstance(manifest, dict)
              and UNATTRIBUTED_WARNING in (manifest.get("warnings") or [])]
    if not warned:
        problems.append(f"the manifest's warnings do not carry {UNATTRIBUTED_WARNING}")
    dispositions = _binding_dispositions(provenance)
    if dispositions and not any(UNATTRIBUTED_WARNING in (item.get("warnings") or []) for item in dispositions):
        problems.append(f"the campaign disposition's warnings do not carry {UNATTRIBUTED_WARNING}")
    attributed = [_member_name(row) for row in unattributed.rows if row.get("sample_row") is not None]
    if attributed:
        problems.append(f"{len(attributed)} unattributed member(s) carry a sample_row ({', '.join(attributed[:3])})")
    scoped, why = _unit_scoped(owner if isinstance(owner, dict) else provenance)
    if scoped is not True:
        problems.append(f"the rule includes unattributed members only from a unit-scoped archive, and {why}")
    return problems


def _slashed(value: object) -> str:
    """A path as the record names one: '/'-separated, without a leading or trailing separator."""
    return str(value or "").replace("\\", "/").strip().strip("/")


def _encoding_name(path: object) -> str:
    """A path's last part as the rule reads it, without case: a packed name (x.d.zip, x.mzML.gz) as the file it
    unpacks to (encoding_preference.unpacked)."""
    name = _basename(_slashed(path)).casefold()
    packed = next((suffix for suffix in ARCHIVE_SUFFIXES if name.endswith(suffix) and len(name) > len(suffix)), "")
    if packed:
        inner = name[: -len(packed)]
        known = next((suffix for suffix in (*ENCODING_SIDECAR_SUFFIXES, *ENCODING_VENDOR_SUFFIXES,
                                            *ENCODING_OPEN_SUFFIXES, *ENCODING_UNREADABLE_SUFFIXES)
                      if inner.endswith(suffix)), "")
        if known and len(inner) > len(known):
            return inner
    return name


def _encoding_rank(path: object) -> "int | None":
    """The rule's rank of a path's encoding (encoding_rule.encoding_rank): 0 a vendor format (a folder or a
    container), 1 a re-encoding MS-DIAL opens that no instrument writes (.cdf, .abf, .ibf), 2 mzML, 3 mzXML; None
    for anything else (mzData, mgf, a SCIEX sidecar)."""
    name = _encoding_name(path)
    if name.endswith(".mzxml"):
        return ENCODING_MZXML_RANK
    if name.endswith(ENCODING_UNREADABLE_SUFFIXES):
        return None
    if name.endswith(ENCODING_OPEN_SUFFIXES):
        return ENCODING_MZML_RANK
    if name.endswith(ENCODING_VENDOR_SUFFIXES):
        return ENCODING_RE_ENCODING_RANK if name.endswith(ENCODING_RE_ENCODING_SUFFIXES) else ENCODING_VENDOR_RANK
    return None


def _encoding_order_key(path: object) -> tuple:
    """Where a candidate stands in the rule's order (encoding_rule.order_key): by rank (clause 1), then by its path
    relative to the data root, '/'-separated, without case (clause 2)."""
    text = _slashed(path)
    rank = _encoding_rank(text)
    return (ENCODING_UNRANKED if rank is None else rank, text.casefold(), text)


def _encoding_stem(path: object) -> str:
    """The sample a file is an encoding of, by name (encoding_preference.stem): its last part, unpacked, less its
    container suffix, without case. Folders do not part one stem's files (the rule's 'copies in other folders')."""
    name = _encoding_name(path)
    _, dot, extension = name.rpartition(".")
    suffix = f".{extension}" if dot else ""
    if suffix in (*ENCODING_VENDOR_SUFFIXES, *ENCODING_OPEN_SUFFIXES, *ENCODING_UNREADABLE_SUFFIXES) \
            and len(name) > len(suffix):
        return name[: -len(suffix)]
    return name


def _path_polarities(path: object) -> frozenset:
    """The polarities a path's folders and name state by a pos/neg token of their own, as Interactive's
    _polarity_token_reading reads them: a token in the file name beside a control, ctrl, blank or QC token names a
    sample, not a polarity. Two paths of one stem that state different polarities are two acquisitions."""
    parts = [part for part in _slashed(path).split("/") if part.strip()]
    stated: set[str] = set()
    for position, part in enumerate(parts):
        tokens = [token for token in re.split(r"[_\-. ]+", part.casefold()) if token]
        for index, token in enumerate(tokens):
            if token not in POLARITY_NAME_TOKENS:
                continue
            beside = tokens[max(index - 1, 0):index] + tokens[index + 1:index + 2]
            if position == len(parts) - 1 and any(word in POLARITY_EXEMPTING_TOKENS for word in beside):
                continue
            stated.add(POLARITY_NAME_TOKENS[token])
    return frozenset(stated)


def _one_sample_polarities(stated: "Iterable[frozenset]", unit_polarity: str) -> dict:
    """How the polarities the paths of one stem state make samples of them, as Interactive's _one_sample_polarities
    reads them: {a path's polarities: its sample's}. The paths of one stem that state one polarity, and those that
    state none, are one sample's where that polarity is the unit's own, or the unit gives none (POS/S1.raw and S1.raw
    in a positive unit). Where they state two, or one that is not the unit's, each set is a sample of its own (an
    empty mapping)."""
    found = {frozenset(item) for item in stated if item}
    if len(found) != 1:
        return {}
    (only,) = found
    if unit_polarity and only != frozenset({unit_polarity}):
        return {}
    return {frozenset(): only, only: only}


def _unit_polarity(provenance: dict) -> str:
    """The unit's polarity as the lease grouped by it ("Positive", "Negative" or ""): the raw owner's declared ion mode
    (_declared_ion_mode), else the project record's ion_mode, the unit's own and then its raw owner's."""
    owner, _ = _raw_owner_manifest(provenance)
    owner = owner if isinstance(owner, dict) else provenance
    declared = _declared_ion_mode(owner)[0]
    if declared:
        return declared.capitalize()
    for manifest in (provenance, owner):
        project = manifest.get("project") if isinstance(manifest.get("project"), dict) else {}
        mode = str(project.get("ion_mode") or "").strip().casefold()
        if mode.startswith("pos"):
            return "Positive"
        if mode.startswith("neg"):
            return "Negative"
    return ""


def _sample_keys(paths: "Iterable[str]", unit_polarity: str) -> dict:
    """Each path's sample by name, as the lease groups one stem's files: {path: (stem, the sample's polarities)}, the
    polarities a path states merged with none as _one_sample_polarities merges them, stem by stem."""
    paths = list(dict.fromkeys(paths))
    by_stem: dict[str, list[str]] = {}
    for path in paths:
        by_stem.setdefault(_encoding_stem(path), []).append(path)
    keys: dict = {}
    for stem, members in by_stem.items():
        stated = {path: _path_polarities(path) for path in members}
        merged = _one_sample_polarities(stated.values(), unit_polarity)
        for path in members:
            keys[path] = (stem, merged.get(stated[path], stated[path]))
    return keys


@dataclass
class _EncodingChoice:
    """One sample's choice under the one encoding rule, as a record gives it: the file used ('' where none could be
    read) and every other candidate with its reason, paths relative to the data root; where the record holds it;
    and what is wrong with its shape."""
    used: str = ""
    unused: list = field(default_factory=list)     # [(path, reason)]
    where: str = ""
    problems: list = field(default_factory=list)

    @property
    def candidates(self) -> list[str]:
        return [*([self.used] if self.used else []), *(path for path, _reason in self.unused)]

    @property
    def identity(self) -> tuple:
        return self.used.casefold(), frozenset((path.casefold(), reason) for path, reason in self.unused)

    @property
    def label(self) -> str:
        return self.used or (self.unused[0][0] if self.unused else "an empty choice")

    def described(self) -> str:
        unused = ", ".join(f"{path} {reason or 'with no reason'}" for path, reason in self.unused[:4])
        more = f", and {len(self.unused) - 4} more" if len(self.unused) > 4 else ""
        return f"{self.used or 'no file'} used" + (f" ({unused}{more})" if unused else "")


def _read_encoding_choice(value: object, where: str) -> _EncodingChoice:
    """A recorded choice ({rule, used, unused: [{path, reason}]}) read into an _EncodingChoice."""
    entry = _EncodingChoice(where=where)
    if not isinstance(value, dict):
        entry.problems.append("is no object")
        return entry
    if value.get("rule") != ENCODING_RULE:
        entry.problems.append(f"names the rule {value.get('rule')!r}, not {ENCODING_RULE}")
    used = value.get("used")
    if used is not None and (not isinstance(used, str) or not used.strip()):
        entry.problems.append(f"names {used!r} as the file used")
    entry.used = _slashed(used) if isinstance(used, str) else ""
    unused = value.get("unused")
    if not isinstance(unused, list):
        entry.problems.append("lists no unused files (unused is no list)")
        return entry
    for item in unused:
        path = _slashed(item.get("path")) if isinstance(item, dict) else ""
        if not path:
            entry.problems.append("lists an unused file that names no path")
            continue
        entry.unused.append((path, str(item.get("reason") or "").strip()))
    return entry


@dataclass
class _EncodingRecord:
    """A unit's record of the one encoding rule: manifest.encoding_choices (listed), and each of its own lineage rows'
    encoding_choice (carried, with the row), under the data root (root)."""
    root: str = ""
    listed: list = field(default_factory=list)
    carried: list = field(default_factory=list)    # [(lineage row, _EncodingChoice)]
    shape: list = field(default_factory=list)

    @property
    def choices(self) -> list:
        """Every choice the record holds, each once: the listed first, then those only a lineage row carries."""
        seen: set = set()
        result = []
        for entry in [*self.listed, *(entry for _row, entry in self.carried)]:
            if entry.identity not in seen:
                seen.add(entry.identity)
                result.append(entry)
        return result

    @property
    def contested(self) -> list:
        """The choices made among more than one candidate: the samples whose data arrived in several encodings."""
        return [entry for entry in self.choices if len(entry.candidates) > 1]


def _row_member(row: dict, root: str) -> str:
    """The file a lineage row's input is under the data root, as the rule names it: a converted input by the mzXML
    its conversion read (source.conversion.source_path), any other by its own path."""
    return _aif_input_key(_conversion_source(row) or row["path"], root)


def _row_name(row: dict) -> str:
    return Path(str(row["path"]).rstrip("\\/")).name


def _encoding_record(provenance: dict) -> _EncodingRecord:
    """The unit's record of the one encoding rule: its own manifest.encoding_choices (a split part's are its own
    samples'), and the encoding_choice of each of its own lineage rows (_own_lineage_rows: a split part's share of
    its raw owner's), inputs and lease-excluded alike."""
    record = _EncodingRecord(root=_data_root(provenance))
    listed = provenance.get(ENCODING_CHOICES)
    if listed is not None and not isinstance(listed, list):
        record.shape.append(f"manifest.{ENCODING_CHOICES} is no list")
    for number, item in enumerate(listed if isinstance(listed, list) else [], start=1):
        record.listed.append(_read_encoding_choice(item, f"{ENCODING_CHOICES}[{number}]"))
    seen: set[str] = set()
    for part in ("rows", "excluded"):
        for row in _own_lineage_rows(provenance, part):
            key = _path_key(row["path"])
            if key in seen or row.get(ENCODING_CHOICE) is None:
                continue
            seen.add(key)
            record.carried.append((row, _read_encoding_choice(row[ENCODING_CHOICE],
                                                              f"the {ENCODING_CHOICE} of {_row_name(row)}")))
    return record


def _encoding_rule_problems(entry: _EncodingChoice, campaign: bool) -> list[str]:
    """Where one recorded choice departs from the rule, recomputed from its candidates and the reasons it gives, or
    []. The candidates are ordered by the rule (_encoding_order_key: rank, then path without case); then:

    - each candidate is one the rule ranks, named once, and each unused one gives a reason the rule gives
      (ENCODING_ORDER_REASONS, ENCODING_UNREADABLE_REASONS; clause 5); in a campaign an mzXML is converted, so
      requires_conversion is none there (clause 1);
    - a reason it could not be read is one that applies to the candidate's encoding (ENCODING_REASON_RANKS):
      undecodable an mzML's, conversion_failed, requires_conversion and polarity_contradicts_declaration an mzXML's,
      incomplete_container and a raw-header reason a vendor file's or a re-encoding's (clause 3);
    - every candidate before the one used carries a reason it could not be read (clause 3): one passed over for the
      order alone was readable and higher, and the rule names it;
    - every candidate after it is tie_lexicographic where its rank is the used one's and lower_in_encoding_order
      where it is lower (clauses 1, 2 and 5), or carries a reason of its own it could not be read (Interactive keeps
      that where only a header stood in the way);
    - where none is used, every candidate carries a reason it could not be read."""
    problems = list(entry.problems)
    seen: set[str] = set()
    for path in entry.candidates:
        if path.casefold() in seen:
            problems.append(f"names {path} more than once")
        seen.add(path.casefold())
        if _encoding_rank(path) is None:
            problems.append(f"names {path}, which is no encoding the rule ranks (a vendor format, mzML or mzXML)")
    for path, reason in entry.unused:
        if not reason:
            problems.append(f"gives no reason for leaving {path} unused (clause 5)")
        elif reason not in ENCODING_ORDER_REASONS | ENCODING_UNREADABLE_REASONS:
            problems.append(f"leaves {path} unused as {reason!r}, which is no reason the rule gives")
        elif reason == REQUIRES_CONVERSION and campaign:
            problems.append(f"leaves {path} unused as {REQUIRES_CONVERSION}, and in a campaign an mzXML is converted "
                            "to mzML (clause 1)")
        elif reason in ENCODING_REASON_RANKS and _encoding_rank(path) is not None \
                and _encoding_rank(path) not in ENCODING_REASON_RANKS[reason]:
            owners = " or ".join(ENCODING_RANK_NAMES[rank] for rank in sorted(ENCODING_REASON_RANKS[reason]))
            problems.append(f"leaves {path} unused as {reason}, a reason only {owners} is left unused for, and "
                            f"{path} is {ENCODING_RANK_NAMES[_encoding_rank(path)]}: a file is passed over as "
                            "unreadable only for a reason that applies to its encoding (clause 3)")
    if not entry.used:
        ordered = [f"{path} ({reason})" for path, reason in entry.unused if reason in ENCODING_ORDER_REASONS]
        if ordered:
            problems.append("uses no file and leaves " + ", ".join(ordered[:5]) + " unused for the rule's order, which "
                            "passes a readable file over only beside the one it uses (clauses 1 and 3)")
        return problems
    used_key = _encoding_order_key(entry.used)
    for path, reason in entry.unused:
        if reason not in ENCODING_ORDER_REASONS:
            continue
        key = _encoding_order_key(path)
        if key < used_key:
            problems.append(f"uses {entry.used} and leaves {path}, before it in the rule's order, unused as {reason}: "
                            "a file before the one used is passed over only where it cannot be read (clause 3)")
            continue
        expected = TIE_LEXICOGRAPHIC if key[0] == used_key[0] else LOWER_IN_ENCODING_ORDER
        if reason != expected:
            problems.append(f"leaves {path} unused as {reason}, where its rank against {entry.used} makes it "
                            f"{expected}")
    return problems


def _encoding_record_problems(provenance: dict, record: "_EncodingRecord | None" = None) -> list[str]:
    """What keeps the unit's record of the one encoding rule from saying, by the rule, which file each sample used,
    or []:

    - every choice, listed or carried, follows the rule (_encoding_rule_problems);
    - a lineage row's encoding_choice uses that row's own file (a converted input's mzXML), and one made among more
      than one candidate is listed in manifest.encoding_choices; its stands_for, where given, is one of its
      sample's candidates;
    - every listed choice whose file used is an input of the lineage is carried by that input's row, the same."""
    record = record if record is not None else _encoding_record(provenance)
    campaign = bool(_binding_dispositions(provenance))
    problems = list(record.shape)
    for entry in record.choices:
        problems.extend(f"the choice of {entry.label} ({entry.where}) {problem}"
                        for problem in _encoding_rule_problems(entry, campaign))
    listed = {entry.identity for entry in record.listed}
    carried: dict[str, _EncodingChoice] = {}
    for row, entry in record.carried:
        name = _row_name(row)
        member = _row_member(row, record.root)
        carried[member.casefold()] = entry
        if not entry.used:
            problems.append(f"{name}'s {ENCODING_CHOICE} uses no file, and {name} is an input of the lineage")
        elif entry.used.casefold() != member.casefold():
            problems.append(f"{name}'s {ENCODING_CHOICE} uses {entry.used}, and {name} is {member}")
        if entry.unused and entry.identity not in listed:
            problems.append(f"{name}'s {ENCODING_CHOICE} ({entry.described()}) is not in manifest.{ENCODING_CHOICES}")
        choice = row.get(ENCODING_CHOICE) if isinstance(row.get(ENCODING_CHOICE), dict) else {}
        if "stands_for" in choice:
            stand = str(choice.get("stands_for") or "").strip()
            relative = _aif_input_key(stand, record.root) if stand else ""
            if relative.casefold() not in {path.casefold() for path in entry.candidates}:
                problems.append(f"{name}'s {ENCODING_CHOICE} stands for {stand or 'nothing'}, which is none of its "
                                "sample's candidates")
    members = {_row_member(row, record.root).casefold()
               for part in ("rows", "excluded") for row in _own_lineage_rows(provenance, part)}
    for entry in record.listed:
        if not entry.used or entry.used.casefold() not in members:
            continue
        own = carried.get(entry.used.casefold())
        if own is None:
            problems.append(f"manifest.{ENCODING_CHOICES} uses {entry.used} for its sample, and its lineage row carries "
                            f"no {ENCODING_CHOICE}")
        elif own.identity != entry.identity:
            problems.append(f"manifest.{ENCODING_CHOICES} ({entry.described()}) and the lineage row of {entry.used} "
                            f"({own.described()}) record different choices for its sample")
    return list(dict.fromkeys(problems))


def _sample_row_names(provenance: dict) -> list[tuple[int, str, str]]:
    """The unit's sample rows (its own project's, else its raw owner's) as (index, the raw_file it names,
    '/'-separated without case, its sample_id)."""
    for manifest in _lineage_manifests(provenance):
        project = manifest.get("project") if isinstance(manifest.get("project"), dict) else {}
        rows = [(index, _slashed(item.get("raw_file")).casefold(), str(item.get("sample_id") or "").strip())
                for index, item in enumerate(project.get("sample_metadata") or []) if isinstance(item, dict)]
        rows = [row for row in rows if row[1]]
        if rows:
            return rows
    return []


def _rows_naming(member: str, rows: list[tuple[int, str, str]], *, by_stem: bool = False) -> set[int]:
    """The sample rows that name a file under the data root by their own raw_file: by its path where a raw_file
    gives folders, else by its name (a packed name as what it unpacks to, an extensionless one as a stem); and, with
    ``by_stem``, where none does, the one row whose named file has its stem (S7.raw for a row naming S7.mzML)."""
    relative = _slashed(member).casefold()
    by_path = {index for index, raw, _sample in rows
               if "/" in raw and (relative == raw or relative.endswith("/" + raw) or raw.endswith("/" + relative))}
    if by_path:
        return by_path
    name, stem = _encoding_name(relative), _encoding_stem(relative)
    named = {index for index, raw, _sample in rows
             if _encoding_name(raw) == name or ("." not in _basename(raw) and _basename(raw) == stem)}
    if named or not by_stem:
        return named
    stemmed = {index for index, raw, _sample in rows if _encoding_stem(raw) == stem}
    return stemmed if len(stemmed) == 1 else set()


def _tied_rows(member: str, rows: list[tuple[int, str, str]],
               paired: "dict[str, set[str]] | None" = None) -> set[int]:
    """The sample rows a file under the data root is paired to: those that name it (_rows_naming, exact), and those
    that name a raw file a pairing rule paired it with (``paired``, _paired_raw_files: a prefixed or
    leading-identifier member paired to the row naming S7.raw); where none is, the one row whose named file has its
    stem (S7.mzML for the row naming S7.raw, in any encoding: clause 4), as Interactive's _encoding_groups reads a
    candidate's sample row (sample_row_of). One row's files are one sample's whatever their stems, so S7.mzML and
    021518_387057_CSHp_S7.raw, paired by a prefix to that row, are one sample's."""
    tied = set(_rows_naming(member, rows))
    paired = paired or {}
    for raw_file in paired.get(_slashed(member).casefold(), set()) | paired.get(_basename(member).casefold(), set()):
        tied |= _rows_naming(raw_file, rows)
    return tied or _rows_naming(member, rows, by_stem=True)


def _two_samples(first: str, second: str, rows: list[tuple[int, str, str]],
                 paired: "dict[str, set[str]] | None" = None) -> bool:
    """Whether two files of one sample by name or by row are two samples' all the same: two different sample rows
    are theirs (_tied_rows: rows naming S1.raw and S1.mzML each keep their own; MTBKS64's two rows of S01 naming
    raw/batch1/QC.RAW and raw/batch2/QC.RAW), or the same two or more rows are both's and no one row is theirs.
    Outside the rule's words, the existing behaviour is kept for both."""
    named_first, named_second = _tied_rows(first, rows, paired), _tied_rows(second, rows, paired)
    return bool(named_first and named_second and named_first.isdisjoint(named_second)) \
        or (named_first == named_second and len(named_first) >= 2)


def _sample_groups(paths: "Iterable[str]", unit_polarity: str, rows: list[tuple[int, str, str]],
                   paired: "dict[str, set[str]] | None" = None) -> dict:
    """Each path's sample as the rule reads 'the same sample', {path: a key one sample's files share}: the files of
    one stem whose polarities make one sample (_sample_keys), joined with every file paired to the same sample row
    (_tied_rows: exact, prefixed or leading-identifier pairing, else the one row whose named file has its stem)
    under the same polarity, a path stating none taken as stating the unit's. So 021518_387057_CSHp_BioRec1.raw,
    paired by prefix to the row naming BioRec1.raw, 000/BioRec1.raw, which that row names, and BioRec1.mzML, of the
    row's own stem, are one sample's files; POS/S1.raw and NEG/S1.raw stay two samples'."""
    keys = _sample_keys(paths, unit_polarity)
    parent: dict = {key: key for key in keys.values()}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    first_of: dict[tuple, tuple] = {}
    for path, key in keys.items():
        polarity = key[1] or (frozenset({unit_polarity}) if unit_polarity else frozenset())
        for index in _tied_rows(path, rows, paired):
            other = first_of.setdefault((index, polarity), key)
            parent[find(key)] = find(other)
    return {path: find(key) for path, key in keys.items()}


def _owner_encoding_choices(provenance: dict) -> list:
    """A split part's raw owner's choices of the one encoding rule (every sample's, the part's and its siblings'),
    or [] for a unit that is its own raw owner. A part carries only the choices its own rows carry, so a file its
    owner's choice left unused is no choice of the part's own record, and is read against the owner's."""
    if not isinstance(provenance.get("split_from"), dict):
        return []
    owner, _ = _raw_owner_manifest(provenance)
    if not isinstance(owner, dict) or owner is provenance:
        return []
    return _encoding_record(owner).choices


def _encoding_exclusions(provenance: dict) -> dict[str, tuple[str, str]]:
    """What the lease and the binding campaign dispositions excluded, of the unit and its raw owner, by key."""
    manifests = _lineage_manifests(provenance)
    exclusions: dict[str, tuple[str, str]] = {}
    for manifest in manifests:
        for key, value in _lease_excluded(manifest).items():
            exclusions.setdefault(key, value)
    for key, value in _exclusion_reasons(*manifests).items():
        exclusions.setdefault(key, value)
    return exclusions


def _encoding_used_exclusion(entry: _EncodingChoice, provenance: dict, root: str,
                             exclusions: dict[str, tuple[str, str]]) -> "tuple[str, str] | None":
    """The exclusion of the file a choice used, (path, reason), or None where nothing excludes it."""
    known = {_row_member(row, root).casefold(): row
             for part in ("rows", "excluded") for row in _own_lineage_rows(provenance, part)}
    listed = known.get(entry.used.casefold())
    key = _path_key(listed["path"]) if listed is not None else (
        _path_key(os.path.join(root, entry.used.replace("/", os.sep))) if root else "")
    return exclusions.get(key) if key else None


def _paired_raw_files(provenance: dict, root: str) -> dict[str, set[str]]:
    """The raw-file names a pairing rule paired each file with, by the file under the data root and by its name
    (casefolded): each lineage row's name_pairing (declared_raw_file, by the row's own file and by the member_name it
    names; an unattributed member's is none), and each manifest.inferred_name_pairings entry (by its member_name and
    by the encoding_used, the file the one encoding rule ran for that member's sample). The unit's own and its raw
    owner's."""
    paired: dict[str, set[str]] = {}

    def add(form: object, name: str) -> None:
        text = _slashed(form).casefold()
        if text and name:
            paired.setdefault(text, set()).add(name)

    for manifest in _lineage_manifests(provenance):
        for part in ("rows", "excluded"):
            for row in _lineage_rows(manifest, part):
                pairing = row.get("name_pairing")
                if not isinstance(pairing, dict) or _is_unattributed(row):
                    continue
                name = str(pairing.get("declared_raw_file") or "").strip()
                add(_row_member(row, root), name)
                add(pairing.get("member_name"), name)
        for item in manifest.get("inferred_name_pairings") or []:
            if isinstance(item, dict):
                name = str(item.get("declared_raw_file") or "").strip()
                add(item.get("member_name"), name)
                add(item.get("encoding_used"), name)
    return paired


def _encoding_run_problems(provenance: dict, csv_rows: "list[dict] | None") -> "tuple[list[str], dict]":
    """Why what reaches the run departs from the one encoding rule of 2026-10-09, or lacks its record, and the
    evidence; ([], {}) where the unit records no choice and nothing departs.

    What reaches the run is the unit's own input lineage rows the analysis CSV opens (_reaching_rows), each the file
    it is under the data root (_row_member: a converted input as its mzXML). Refused:

    - a record that does not follow the rule or does not say what ran (_encoding_record_problems);
    - a file a choice leaves unused that reaches the run (clause 1: exactly one file of a sample), the choices of a
      split part's raw owner included (_owner_encoding_choices);
    - a choice whose file used reaches no run, unless it was excluded on record; and one excluded for a raw header
      the preflight could not read while the sample's next encoding was left for the order only (clause 3:
      ENCODING_FALLBACK_NOT_TAKEN, which a disposition's warning of that name says as well);
    - a file used that runs unattributed or as another sample than the sample rows give that name its sample's
      files, or that a pairing rule paired one of them with (_paired_raw_files: name_pairing, inferred_name_pairings),
      or with a stands_for and no sample (clause 4);
    - two files of one sample that both reach the run (_sample_groups: one stem and the same sample's polarities,
      or one sample row by any pairing under one polarity, and no two sample rows of their own: _two_samples); an
      input beside a lease-excluded encoding of its own sample that no choice records; and a file of a sample the
      unit records (a running input's, a choice's, an excluded one's)
      that the raw owner's record shows delivered, by an archive listing or a download that is no archive, and that
      no choice, input, exclusion, left_out record or declaration accounts for (_unaccounted_listed_members): the
      run departs from the rule, or the rule's record of it is missing (clauses 2, 3 and 5);
    - a file unattributed_members.left_out leaves out that the rule would use before a file of its sample that runs
      (before it in the rule's order: a higher encoding, or the first of a tie), for a reason that does not remove
      it under the rule (_left_out_rule_problems; clauses 1 to 3)."""
    record = _encoding_record(provenance)
    root = record.root
    rows, _reaching = _reaching_rows(provenance, csv_rows)
    problems = _encoding_record_problems(provenance, record)
    reaching = {_row_member(row, root).casefold(): row for row in rows}
    exclusions = _encoding_exclusions(provenance)
    samples = _input_samples(provenance, ("rows", "excluded"))
    names = _sample_row_names(provenance)
    paired = _paired_raw_files(provenance, root)
    choices = record.choices
    owner_choices = _owner_encoding_choices(provenance)
    identities = {entry.identity for entry in choices}
    every_choice = [*choices, *(entry for entry in owner_choices if entry.identity not in identities)]

    reached = [f"{path} ({reason or 'no reason'}; {entry.used or 'no file'} used)"
               for entry in every_choice for path, reason in entry.unused if path.casefold() in reaching]
    if reached:
        problems.append(f"{len(reached)} file(s) the one encoding rule left unused reach the run all the same: "
                        + "; ".join(dict.fromkeys(reached[:5])) + " (clause 1: exactly one file of a sample is used)")
    fallback = False
    for entry in choices:
        if not entry.used:
            continue
        row = reaching.get(entry.used.casefold())
        if row is None:
            excluded = _encoding_used_exclusion(entry, provenance, root, exclusions)
            later = [path for path, reason in entry.unused if reason in ENCODING_ORDER_REASONS]
            if excluded is not None and excluded[1] in RAW_HEADER_REASONS and later:
                fallback = True
                problems.append(f"{entry.used}, the file the rule used for its sample, is excluded for a raw header the "
                                f"preflight could not read ({excluded[1]}), and the sample's next encoding, "
                                f"{later[0]}, was not taken ({ENCODING_FALLBACK_NOT_TAKEN}): where the chosen one "
                                "cannot be read, the next in order is taken (clause 3)")
            elif excluded is None:
                problems.append(f"the choice of {entry.label} ({entry.where}) uses {entry.used}, and no input that "
                                "reaches the run is it, and nothing excludes it: its sample runs on nothing")
            continue
        choice = row.get(ENCODING_CHOICE) if isinstance(row.get(ENCODING_CHOICE), dict) else {}
        stand = str(choice.get("stands_for") or "").strip()
        named = set()
        for path in [*entry.candidates, *([_aif_input_key(stand, root)] if stand else [])]:
            named |= _rows_naming(path, names, by_stem=True)
            for raw_file in paired.get(_slashed(path).casefold(), set()) | paired.get(_basename(path).casefold(), set()):
                named |= _rows_naming(raw_file, names, by_stem=True)
        named_samples = {sample for index, _raw, sample in names if index in named and sample}
        sample = str(row.get("sample_id") or "").strip() or samples.get(_path_key(row["path"]), "")
        if (named_samples or stand) and _is_unattributed(row):
            problems.append(f"{entry.used} runs as an unattributed member, and it is the file the rule used for a "
                            "sample a sample row names: the file used is that sample's own input, paired to its row "
                            "and in its Class (clause 4)")
        elif named_samples and sample not in named_samples:
            problems.append(f"{entry.used} runs as {f'sample {sample}' if sample else 'no sample'}, the file the rule "
                            f"used for the sample of {', '.join(sorted(named_samples))}, whose rows name its "
                            "sample's files or were paired with one of them (clause 4)")
        elif stand and not sample:
            problems.append(f"{entry.used} stands for {_aif_input_key(stand, root)}, the file its sample row pairs "
                            "with, and runs as no sample (clause 4)")
    warned = [disposition for disposition in _binding_dispositions(provenance)
              if ENCODING_FALLBACK_NOT_TAKEN in (disposition.get("warnings") or [])]
    if warned and not fallback:
        problems.append(f"the campaign disposition warns {ENCODING_FALLBACK_NOT_TAKEN}: a file the rule used was "
                        "excluded for its raw header, and its sample's next encoding was not taken (clause 3)")
    unit_polarity = _unit_polarity(provenance)
    problems.extend(_unrecorded_encoding_problems(provenance, rows, record, names, every_choice, unit_polarity,
                                                  paired))
    problems.extend(_unaccounted_listed_members(provenance, rows, record, every_choice, unit_polarity, names, paired))
    problems.extend(_left_out_rule_problems(provenance, rows, record, unit_polarity, names, paired, exclusions))
    return list(dict.fromkeys(problems)), _encoding_evidence(record)


def _unrecorded_encoding_problems(provenance: dict, rows: list[dict], record: _EncodingRecord,
                                  names: list[tuple[int, str, str]], choices: "list | None" = None,
                                  unit_polarity: str = "", paired: "dict[str, set[str]] | None" = None) -> list[str]:
    """A run that departs from the one encoding rule where its record says nothing: two files of one sample that
    both reach the run, and an input beside a lease-excluded encoding of its own sample that no choice holds with it
    (``choices``: the unit's, and a split part's raw owner's). One sample's files are those of one stem
    (_encoding_stem, whatever folders they lie in) whose polarities make one sample (_sample_keys: a path stating
    none is one sample's with one stating the unit's own), and those paired to one sample row (_sample_groups: an
    exact name, a prefixed or a leading-identifier pairing, ``paired``), that are not two sample rows'
    (_two_samples)."""
    root = record.root
    together = [{path.casefold() for path in entry.candidates}
                for entry in (record.choices if choices is None else choices)]
    lease: dict[str, tuple[str, str]] = {}
    for manifest in _lineage_manifests(provenance):
        for key, value in _lease_excluded(manifest).items():
            lease.setdefault(key, value)
    running: list[str] = []
    seen: set[str] = set()
    for row in rows:
        member = _row_member(row, root)
        if _encoding_rank(member) is None or member.casefold() in seen:
            continue
        seen.add(member.casefold())
        running.append(member)
    excluded = [(_aif_input_key(path, root), reason) for path, reason in lease.values()]
    excluded = [(member, reason) for member, reason in excluded if _encoding_rank(member) is not None]
    keys = _sample_groups([*running, *(member for member, _reason in excluded)], unit_polarity, names, paired)
    groups: dict[tuple, list[str]] = {}
    for member in running:
        groups.setdefault(keys[member], []).append(member)
    problems: list[str] = []
    twice: list[str] = []
    for members in groups.values():
        both = sorted({item for index, first in enumerate(members) for second in members[index + 1:]
                       if not _two_samples(first, second, names, paired) for item in (first, second)},
                      key=_encoding_order_key)
        if both:
            twice.append(" and ".join(both))
    if twice:
        problems.append(f"{len(twice)} sample(s) reach the run in more than one file, where the one encoding rule uses "
                        "exactly one file of a sample (clauses 1 and 2): " + "; ".join(twice[:5]))
    beside: list[str] = []
    for member, reason in excluded:
        for other in groups.get(keys[member], []):
            if other.casefold() == member.casefold() \
                    or any(member.casefold() in group and other.casefold() in group for group in together) \
                    or _two_samples(member, other, names, paired):
                continue
            beside.append(f"{member} ({reason or 'no reason recorded'}) beside {other}")
    if beside:
        problems.append(f"the lease excluded {len(beside)} file(s) of a sample another of whose files runs, and no "
                        f"choice of the one encoding rule records them ({ENCODING_CHOICE}: every file not used is "
                        "recorded with its reason, naming the file used; clause 5): " + "; ".join(beside[:5]))
    return problems


def _conversions_by_source(records: "list | None", root: str) -> dict[str, dict]:
    """The conversion records by their source under the data root (source.path, or source.relative_path), without
    case."""
    by_source: dict[str, dict] = {}
    for item in records or []:
        source = item.get("source") if isinstance(item, dict) and isinstance(item.get("source"), dict) else {}
        for form in (_aif_input_key(source.get("path"), root) if str(source.get("path") or "").strip() else "",
                     _slashed(source.get("relative_path"))):
            if form:
                by_source.setdefault(form.casefold(), item)
    return by_source


def _left_out_removal_problem(path: str, reason: str, provenance: dict, root: str, unit_polarity: str,
                              names: list[tuple[int, str, str]], paired: "dict[str, set[str]] | None",
                              exclusions: dict[str, tuple[str, str]]) -> str:
    """Why a left_out reason does not remove a file under the one encoding rule, or "" where it does:

    - a reason it could not be read (ENCODING_UNREADABLE_REASONS) applies to its encoding (ENCODING_REASON_RANKS),
      requires_conversion only outside a campaign, and has its evidence on record: requires_conversion the encoding
      itself; any other an exclusion of the file on record for that reason (undecodable as unsupported_mzml_encoding
      too), or, for conversion_failed and polarity_contradicts_declaration, a conversion record of it that did not
      complete (clause 3);
    - polarity_token_contradicts_ion_mode, a path that states a polarity by a token of its own other than the unit's;
    - not_named_by_the_catalog_declaration, a unit that declares its inputs and does not name the file (R2-3);
    - stem_of_several_sample_rows, a file of a stem the named files of two or more sample rows have, which no row
      names or a pairing rule paired (two rows' files are two samples': no one sample's encoding);
    - any other reason (a shared archive's, a download scope's, none) removes no file of a sample from the rule."""
    if reason in ENCODING_UNREADABLE_REASONS:
        rank = _encoding_rank(path)
        if rank not in ENCODING_REASON_RANKS.get(reason, frozenset()):
            return f"{reason} is no reason {ENCODING_RANK_NAMES.get(rank, 'such a file')} cannot be read"
        if reason == REQUIRES_CONVERSION:
            return ("in a campaign an mzXML is converted to mzML (clause 1)" if _binding_dispositions(provenance)
                    else "")
        excluded = exclusions.get(_path_key(os.path.join(root, path.replace("/", os.sep)))) if root else None
        accepted = {reason, UNSUPPORTED_MZML_ENCODING} if reason == "undecodable" else {reason}
        if excluded is not None and excluded[1] in accepted:
            return ""
        if reason in (CONVERSION_FAILED, "polarity_contradicts_declaration"):
            owner, _ = _raw_owner_manifest(provenance)
            records, _problem = _conversion_records(owner if isinstance(owner, dict) else provenance)
            conversion = _conversions_by_source(records, root).get(path.casefold())
            if conversion is not None and conversion.get("status") != "converted":
                return ""
            return ("no exclusion of it for that reason, and no conversion record of it that did not complete, is on "
                    "record")
        return "no exclusion of it for that reason is on record"
    if reason == POLARITY_TOKEN_CONTRADICTS_ION_MODE:
        stated = _path_polarities(path)
        if unit_polarity and stated - {unit_polarity}:
            return ""
        return (f"its path states {', '.join(sorted(stated)) or 'no polarity'}, and the unit's polarity is "
                f"{unit_polarity or 'not recorded'}")
    if reason == NOT_NAMED_BY_DECLARATION:
        owner, _ = _raw_owner_manifest(provenance)
        undeclared = _undeclared(owner if isinstance(owner, dict) else provenance, provenance)
        if undeclared is None:
            return "the unit declares no analysis inputs"
        return "" if undeclared(path) else "the declaration names it"
    if reason == STEM_OF_SEVERAL_SAMPLE_ROWS:
        stem = _encoding_stem(path)
        rows_of_stem = {index for index, raw, _sample in names if _encoding_stem(raw) == stem}
        if len(rows_of_stem) >= 2 and not _tied_rows(path, names, paired):
            return ""
        return "no two sample rows share its stem, or a sample row names it or was paired with it"
    return "it is no reason the rule passes a file of a sample over for"


def _left_out_rule_problems(provenance: dict, rows: list[dict], record: _EncodingRecord, unit_polarity: str,
                            names: list[tuple[int, str, str]], paired: "dict[str, set[str]] | None",
                            exclusions: dict[str, tuple[str, str]]) -> list[str]:
    """The files unattributed_members.left_out leaves out (the unit's and its raw owner's) that the one encoding rule
    would use before a file of their sample that reaches the run (_sample_groups, and no two sample rows of their
    own: _two_samples; before it in the rule's order: a higher encoding, or the first of a tie), and whose reason
    does not remove them under the rule (_left_out_removal_problem). A left_out entry accounts for a file, but only
    a reason that applies to it takes the rule's choice from it: otherwise a lower-ranked file, or a later tie, runs
    where the rule names the file left out (clauses 1 to 3)."""
    root = record.root
    left_out = [(item["path"], str(item.get("reason") or "").strip()) for item in _record_left_out(provenance)]
    left_out = [(path, reason) for path, reason in left_out if _encoding_rank(path) is not None]
    if not left_out:
        return []
    running = list(dict.fromkeys(member for member in (_row_member(row, root) for row in rows)
                                 if _encoding_rank(member) is not None))
    running_keys = {member.casefold() for member in running}
    keys = _sample_groups([*running, *(path for path, _reason in left_out)], unit_polarity, names, paired)
    found: list[str] = []
    for path, reason in left_out:
        if path.casefold() in running_keys:
            continue
        after = [other for other in running if keys[other] == keys[path]
                 and _encoding_order_key(path) < _encoding_order_key(other)
                 and not _two_samples(path, other, names, paired)]
        if not after:
            continue
        why = _left_out_removal_problem(path, reason, provenance, root, unit_polarity, names, paired, exclusions)
        if why:
            found.append(f"{path} ({reason or 'no reason'}: {why}) while {after[0]} runs")
    if not found:
        return []
    return [f"{UNATTRIBUTED_RECORD}.{UNATTRIBUTED_LEFT_OUT} leaves out {len(found)} file(s) the one encoding rule "
            "uses before the file of their sample that runs, for a reason that does not remove them under the rule "
            "(a reason it could not be read, on record and of its encoding; a polarity its path states; a declaration "
            "that does not name it; a stem two sample rows share; clauses 1 to 3): " + "; ".join(found[:5])]


def _listed_inputs(owner: dict, root: str) -> dict[str, bool]:
    """Every input container the raw owner's record shows delivered under the data root, as the rule names it
    (relative, '/'-separated), with whether an archive listing shows it ({path: archived}), as _delivery_of reads the
    delivery: each container an archive member listing
    (archive_extractions[].members_tsv, its sha256 checked) shows extracted, at the listing's path under the
    extraction's destination (or as listed where none is recorded), and each container a download that is no archive
    is or is inside (_download_container: a Waters .raw folder fetched file by file is one input, and a .wiff.scan
    companion is none). A container the rule ranks no encoding of, one outside the data root, and a listing that
    cannot be read give nothing."""
    found: dict[str, tuple[str, bool]] = {}

    def add(relative: str, archived: bool = False) -> None:
        if relative and not relative.startswith("../") and relative != ".." and not os.path.isabs(relative) \
                and _encoding_rank(relative) is not None:
            known, before = found.get(relative.casefold(), (relative, False))
            found[relative.casefold()] = (known, before or archived)

    for item in owner.get("archive_extractions") or []:
        if not isinstance(item, dict):
            continue
        listing = _member_listing(owner, item)
        if listing.problem:
            continue
        destination = str(item.get("destination") or "").strip()
        for member in sorted(listing.files):
            container = _member_container(listing.names.get(member, member))
            if not container:
                continue
            add(_aif_input_key(os.path.join(destination, container.replace("/", os.sep)), root)
                if destination and root else container, archived=True)
    for item in owner.get("downloads") or []:
        if not isinstance(item, dict) or not str(item.get("path") or "").strip() or _is_archive_download(item):
            continue
        path = str(item["path"])
        if root and os.path.isabs(path) and os.path.isabs(root):
            relative = _aif_input_key(path, root)
            add(_member_container(relative) if not relative.startswith("../") and not os.path.isabs(relative)
                else "")
        else:
            add(_download_container(path, str(owner.get("raw_directory") or "")))
    return dict(found.values())


def _undeclared(owner: dict, provenance: dict) -> "Callable[[str], bool] | None":
    """Where the unit declares its analysis inputs (the raw owner's declaration, else its own), whether a path under
    the data root is one the declaration does not name, under any of the forms Interactive's allow-list compares
    (_allowlist_forms): the declaration is the record that keeps such a file out of the run (R2-3). None where the
    unit declares none."""
    for manifest in (owner, provenance):
        project = manifest.get("project") if isinstance(manifest, dict) else None
        declared, _contradiction = _declared_inputs(project if isinstance(project, dict) else {})
        if declared:
            names = {_declared_input_key(item.get("path") if isinstance(item, dict) else item) for item in declared}
            names.discard("")
            return lambda relative: not any(form in names for form in _allowlist_forms(_declared_input_key(relative)))
    return None


def _unaccounted_listed_members(provenance: dict, rows: list[dict], record: _EncodingRecord,
                                choices: list, unit_polarity: str, names: "list[tuple[int, str, str]] | None" = None,
                                paired: "dict[str, set[str]] | None" = None) -> list[str]:
    """The files the raw owner's record shows delivered (_listed_inputs: an archive listing, or a download that is no
    archive) that are of a sample the unit records (_sample_groups: one stem's, or paired to one sample row by an
    exact name or a prefixed or leading-identifier pairing) and that nothing accounts for: no choice of the one
    encoding rule names them, and they are no input candidate, no lineage row (an input or an excluded one, the raw
    owner's included), no excluded candidate, no unattributed or left_out member, and, in a declared unit, no
    download that is no archive and that the declaration does not name (the declaration keeps it out, and Interactive
    writes no other record of it). An archive member no declaration names stays out on record: Interactive lists it
    in unattributed_members.left_out (not_named_by_the_catalog_declaration, R2-3), and without that record it is
    unaccounted for here. A sample the unit records is that
    of an input that reaches the run, of a candidate of any choice (one whose file used was excluded, or that used
    none, included), and of any lineage row or excluded candidate. Every file of a sample not used is recorded with
    its reason (clause 5), and a copy no record names may be the one the rule names (clauses 2 and 3)."""
    root = record.root
    owner, _ = _raw_owner_manifest(provenance)
    owner = owner if isinstance(owner, dict) else provenance
    if not root:
        return []
    listed = _listed_inputs(owner, root)
    if not listed:
        return []
    accounted: set[str] = set()
    known: list[str] = []

    def key(path: object) -> str:
        text = str(path or "").strip()
        return _slashed(_aif_input_key(text, root)) if text else ""

    def account(path: object, *, of_a_sample: bool = True) -> None:
        relative = key(path)
        if relative:
            accounted.add(relative.casefold())
            if of_a_sample and _encoding_rank(relative) is not None:
                known.append(relative)

    running = list(dict.fromkeys(_row_member(row, root) for row in rows if _encoding_rank(_row_member(row, root))
                                 is not None))
    known.extend(running)
    for entry in [*record.choices, *choices]:
        for path in entry.candidates:
            account(path)
    for manifest in [*_lineage_manifests(provenance), owner]:
        for path in manifest.get("input_candidates") or []:
            account(path)
        for item in manifest.get("excluded_input_candidates") or []:
            if isinstance(item, dict):
                account(item.get("path"))
        for part in ("rows", "excluded"):
            for row in _lineage_rows(manifest, part):
                account(row["path"])
                account(_row_member(row, root))
        free = manifest.get(UNATTRIBUTED_RECORD)
        for path in (free.get("paths") or []) if isinstance(free, dict) else []:
            account(path, of_a_sample=False)
    for item in _record_left_out(provenance):
        account(item["path"], of_a_sample=False)
    undeclared = _undeclared(owner, provenance)
    keys = _sample_groups([*known, *listed], unit_polarity, names or [], paired)
    by_sample: dict[tuple, str] = {}
    for member in known:
        by_sample.setdefault(keys[member], member)
    unaccounted = [f"{path} beside {by_sample[keys[path]]}" for path in sorted(listed, key=_encoding_order_key)
                   if path.casefold() not in accounted and keys[path] in by_sample
                   and not (undeclared is not None and not listed[path] and undeclared(path))]
    if not unaccounted:
        return []
    return [f"the raw owner's record shows {len(unaccounted)} file(s) delivered (an archive listing, or a download "
            "that is no archive) of a sample the unit records, and no choice of the one encoding rule, input, "
            "exclusion, left_out record or declaration names them (every file not used is recorded with its reason, "
            "and a copy no record names may be the one the rule uses; clauses 2, 3 and 5): "
            + "; ".join(unaccounted[:5])]


def _encoding_run_sentence(provenance: dict, csv_rows: "list[dict] | None") -> str:
    """The sentence that names each sample whose data arrived in several encodings, the file the rule used, and
    whether that file runs: a file used that was excluded on record (or a sample none of whose files could be read)
    is named as a sample that runs on no file, never as one whose file runs. "" where no sample arrived so."""
    record = _encoding_record(provenance)
    sentence = _encoding_sentence(record)
    if not sentence:
        return ""
    root = record.root
    rows, _reaching = _reaching_rows(provenance, csv_rows)
    exclusions = _encoding_exclusions(provenance)
    reaching = {_row_member(row, root).casefold() for row in rows if _path_key(row["path"]) not in exclusions}
    dropped: list[str] = []
    for entry in record.contested:
        if not entry.used:
            dropped.append(f"{entry.label} (none of its files could be read)")
        elif entry.used.casefold() not in reaching:
            excluded = _encoding_used_exclusion(entry, provenance, root, exclusions)
            dropped.append(f"{entry.used} (excluded, {excluded[1] or 'no reason recorded'})" if excluded is not None
                           else f"{entry.used} (no input that reaches the run)")
    if not dropped:
        return f"{sentence}; the file used runs for each, as its sample, and none other does"
    ran = len(record.contested) - len(dropped)
    return (f"{sentence}. {len(dropped)} of these sample(s) run on no file: the file used is excluded or none could "
            "be read, and no other file of the sample runs in its place: " + "; ".join(dropped[:5])
            + (f". The file used runs for each of the other {ran}, as its sample, and none other does" if ran else ""))


def _encoding_evidence(record: _EncodingRecord) -> dict:
    """The evidence's account of the one encoding rule's choices among more than one candidate: how many, the files
    left unused by reason, and the first ten."""
    contested = record.contested
    if not contested:
        return {}
    reasons = Counter(reason or "" for entry in contested for _path, reason in entry.unused)
    return {"rule": ENCODING_RULE, ENCODING_CHOICES: len(contested),
            "unused_encodings": sum(len(entry.unused) for entry in contested),
            "reasons": dict(sorted(reasons.items())),
            "choices": [{"used": entry.used or None,
                         "unused": [{"path": path, "reason": reason} for path, reason in entry.unused]}
                        for entry in contested[:10]]}


def _encoding_sentence(record: _EncodingRecord) -> str:
    """The sentence that names each sample whose data arrived in several encodings and the file the rule used."""
    contested = record.contested
    if not contested:
        return ""
    more = f"; and {len(contested) - 5} more" if len(contested) > 5 else ""
    return (f"{len(contested)} sample(s) arrived in more than one encoding, and the one encoding rule of 2026-10-09 "
            f"({ENCODING_RULE}) used one file of each, recording every other with its reason: "
            + "; ".join(entry.described() for entry in contested[:5]) + more)


def _encoding_unused_paths(provenance: dict) -> list[str]:
    """Every file a choice of the one encoding rule left unused beside the one it used, as an absolute path under the
    data root: no input and no excluded candidate, each is accounted for by its sample's choice alone."""
    record = _encoding_record(provenance)
    if not record.root:
        return []
    return list(dict.fromkeys(os.path.join(record.root, path.replace("/", os.sep))
                              for entry in record.choices if entry.used for path, _reason in entry.unused))


def _encoding_conversion_problems(provenance: dict, records: "list | None") -> "tuple[list[str], list[str]]":
    """Where the one encoding rule's record disagrees with the conversions, and the mzXML whose failed conversion the
    rule passed over for the next file of their sample. A choice that uses an mzXML uses a completed conversion of
    it (status converted), and one that leaves an mzXML unused as conversion_failed names one whose conversion
    record did not complete (clause 3). A conversion record is read by its source (source.path under the data root,
    or source.relative_path)."""
    record = _encoding_record(provenance)
    root = record.root
    by_source = _conversions_by_source(records, root)
    problems: list[str] = []
    passed_over: list[str] = []
    for entry in record.choices:
        if entry.used and _encoding_rank(entry.used) == ENCODING_MZXML_RANK:
            conversion = by_source.get(entry.used.casefold())
            if conversion is None:
                problems.append(f"the one encoding rule used {entry.used}, an mzXML, for its sample, and no conversion "
                                "record read it")
            elif conversion.get("status") != "converted":
                problems.append(f"the one encoding rule used {entry.used}, an mzXML, for its sample, and its "
                                f"conversion's status is {conversion.get('status')!r}, not 'converted': where a "
                                "conversion fails the next in order is taken (clause 3)")
        for path, reason in entry.unused:
            if reason != CONVERSION_FAILED:
                continue
            conversion = by_source.get(path.casefold())
            if conversion is None:
                problems.append(f"{path} is left unused as {CONVERSION_FAILED}, and no conversion record read it")
            elif conversion.get("status") == "converted":
                problems.append(f"{path} is left unused as {CONVERSION_FAILED}, and its conversion record says it "
                                "was converted")
            elif entry.used:
                passed_over.append(f"{path} for {entry.used}")
    return list(dict.fromkeys(problems)), passed_over


def check_analysis_inputs_are_the_inputs(
    report: Report, provenance: dict | None, reason: str, csv_rows: list[dict] | None, csv_reason: str,
) -> None:
    """INP-1. What the Catalog declared MS-DIAL opens is what the lease found and what the CSV lists.

    Three writers. The Catalog projects a unit's file listing onto what MS-DIAL opens, one analysis
    input per file, vendor folder or packed container (project.analysis_inputs); Interactive finds its
    input candidates on disk after the download; the preparer writes one CSV row per candidate. A
    Waters .raw folder read as its member files is thirty-odd inputs where one was declared, and the
    stale sidecars of one inflated unit held 477 rows for 12 samples. CNT-1 compares the candidates
    with the rows; nothing compared either with what the Catalog declared.

    Required only where the manifest declares analysis inputs. A unit whose data sit inside an
    accession archive, or that publishes only converted files, finds its inputs after the download,
    and a manifest written before the Catalog declared them has nothing to compare.

    An input a binding campaign disposition excluded (excluded_inputs, with its reason) was declared
    and is never a CSV row. Interactive leaves it among the input candidates, where the disposition
    found it, so the declaration is the candidates and the rows are the candidates less the excluded;
    an excluded input that is no candidate is counted beside the candidates instead. A disposition
    Interactive recorded without applying it (applied: false, a unit outside a campaign) excludes
    nothing, as its execution gate reads it.

    An input the lease excluded itself (_lease_excluded: an mzML RawDataHandler cannot decode, Interactive
    0.5.18) was declared too, and is neither a candidate nor a row, so it is counted beside the candidates
    as well. Without it a correct unit with one undecodable mzML FAILed here, and INP-1 stops the run: the
    unit counted as failed and lost its raw data, where the rule is that the file is excluded and the rest
    of the unit runs.

    EACH ROW IS AN INPUT, not only a count of them. A row that opens an input the disposition or the lease
    excluded, under any name, in place of another sample's row leaves every count equal, so each row
    must open a candidate that runs (_inputs_opened: by its path, by a Console alias the lineage
    records, or while both are on disk by file record), and no candidate may be opened twice. Rows
    compared by count alone let an excluded input that nothing vouches for run through a hard link
    while SUM-1 was told it was never opened.

    A split part's project is its parent's with, where the split carries them, only the part's own
    samples' inputs: the parent's declaration is compared with the parent's candidates, a declaration
    of the part's own with the part's candidates, and the part's candidates with its rows. SPL-1 holds
    that the parts partition the parent.

    UNATTRIBUTED MEMBERS (user decision, 2026-10-07; Interactive 0.5.31). Archive members of a unit-scoped
    archive that no sample row pairs with are input candidates and CSV rows of their own, included unattributed
    (_unattributed_members). They are explained inputs, not a contradiction of the declaration: the declared
    inputs are then at least the candidates the lease attributed, and the unattributed members stand beside
    them, since which declared input each one is, if any, is exactly what nobody could say. Every row must still
    open a candidate, once. The check is then a WARN that lists them with manifest.unattributed_members.count,
    never a PASS. They FAIL it only where the download is recorded as shared with other units, whose files they
    may be: the rule includes them from a unit-scoped archive only. That guard is read first, before INP-1 finds
    nothing declared to compare: Interactive includes unattributed members only where the Catalog declared no
    analysis inputs, so a check that returned NOT_EVALUABLE first never blocked a real run on it.

    WHAT THE LEASE LEFT OUT, OR CONVERTED (user decision, 2026-10-08, second round, answer 3; Interactive 0.5.36).
    An unpaired mzXML converted to mzML is an unattributed input like any other, where its lineage row is the
    conversion of the mzXML its name_pairing names; what the lease leaves out of the unit is in
    unattributed_members.left_out. INP-1 FAILs, with or without a declaration, where the run holds a member that
    record leaves out, or a converted unattributed input that is not the conversion of the member it names
    (_unattributed_choice_problems). Read, like the shared-archive guard, before the declaration is found missing.

    ONE ENCODING PER SAMPLE (the user's one rule of 2026-10-09; Interactive 0.5.36). Where a sample's data arrive in
    several encodings, the rule uses exactly one: the highest readable in the order vendor format -> mzML -> mzXML,
    a tie to the first path without case, the next where the chosen one cannot be read or converted, that sample's
    own input whatever encoding its row names, and every other on record with its reason. INP-1 recomputes the rule
    from the record (manifest.encoding_choices and each lineage row's encoding_choice) and FAILs, with or without a
    declaration, a run that departs from it or lacks its record (_encoding_run_problems): a choice the rule does not
    make, a file left unused that runs (a split part's raw owner's choices included), a file used that runs as no
    sample or another one than its rows or a pairing rule's inference give, a vendor file excluded for its header
    with the next encoding not taken, two files of one sample that both run, an input beside a lease-excluded
    encoding of its sample that no choice records, or a file of a sample the unit records that an archive listing or
    a download that is no archive shows delivered and that no record or declaration names. A sample whose file used was excluded is named as running on no file. A declared input the rule left unused is no
    candidate and no excluded input: it is counted beside the candidates, accounted for by its sample's choice.

    RUN POLICY: blocks_run, as the user named it (2026-10-01). A folder read as its member files, or
    an input the run never opens, gives results for files that are not the unit's.
    """
    stage = "before-production"
    if provenance is None:
        report.add("INP-1", stage, INP1_TITLE, NOT_EVALUABLE, reason, required=False)
        return
    split = isinstance(provenance.get("split_from"), dict)
    owner, owner_reason = _raw_owner_manifest(provenance)

    def declaration(manifest: dict | None) -> tuple[list | None, str]:
        project = (manifest or {}).get("project")
        return _declared_inputs(project if isinstance(project, dict) else {})

    own_declared, own_contradiction = declaration(provenance)
    declared, contradiction = declaration(owner) if split and owner is not None else (own_declared, own_contradiction)
    if declared is None and own_declared is None:
        # Interactive includes unattributed members only where the Catalog declared no analysis inputs, so the
        # shared-archive guard is read here, before the declaration is found missing: members of an archive
        # other units share may be their files, and the unit does not run on them.
        unattributed = _unattributed_members(provenance)
        if unattributed:
            scoped, why = _unit_scoped(owner if isinstance(owner, dict) else provenance)
            if scoped is False:
                report.add("INP-1", stage, INP1_TITLE, FAIL,
                           "What MS-DIAL will open is not what the rule of 2026-10-07 lets it open: "
                           f"{len(unattributed.members)} input candidate(s) are archive members no sample row "
                           f"pairs with, included unattributed, and {why}. The rule includes them from a "
                           "unit-scoped archive only. " + unattributed.sentence() + ".",
                           **unattributed.evidence())
                return
        # What the lease left out or converted among the unpaired members is held here too (2026-10-08, second
        # round, answer 3), and so is the one encoding rule (2026-10-09): the declaration is missing, the record is not.
        choices, choice_evidence = _unattributed_choice_problems(provenance, csv_rows, unattributed)
        encoding_problems, encoding_evidence = _encoding_run_problems(provenance, csv_rows)
        if choices or encoding_problems:
            report.add("INP-1", stage, INP1_TITLE, FAIL,
                       "What MS-DIAL will open is not what the lease's record lets it open: "
                       + "; ".join([*choices, *encoding_problems]) + ". A member the record leaves out, a file the one "
                       "encoding rule did not use, or a sample's file run outside its sample, gives results for a file "
                       "that is not the unit's input, a sample measured twice, or a sample's data outside its Class.",
                       **({"unpaired_members": choice_evidence} if choice_evidence else {}),
                       **({"one_encoding_rule": encoding_evidence} if encoding_evidence else {}),
                       **(unattributed.evidence() if unattributed else {}))
            return
        held = ""
        if choice_evidence:
            held = (f" The {choice_evidence['inputs_read']} input(s) that reach the run are what the lease's record of "
                    "the archive's unpaired members lets reach it: none is a member it leaves out, and each converted "
                    "unattributed input is the conversion of the mzXML it names.")
        sentence = _encoding_run_sentence(provenance, csv_rows)
        if sentence:
            held += f" {sentence}."
        report.add("INP-1", stage, INP1_TITLE, NOT_EVALUABLE,
                   "The manifest declares no analysis inputs: the unit finds its inputs after the download, or "
                   "was prepared before the Catalog declared them." + held, required=False,
                   **({"unpaired_members": choice_evidence} if choice_evidence else {}),
                   **({"one_encoding_rule": encoding_evidence} if encoding_evidence else {}))
        return
    if owner is None:
        report.add("INP-1", stage, INP1_TITLE, NOT_EVALUABLE,
                   f"This unit was split from another, whose manifest cannot be used: {owner_reason}")
        return
    owner_candidates, own_candidates = owner.get("input_candidates"), provenance.get("input_candidates")
    if not isinstance(owner_candidates, list) or not isinstance(own_candidates, list):
        report.add("INP-1", stage, INP1_TITLE, NOT_EVALUABLE,
                   "The manifest declares analysis inputs and records no input candidates to compare them with.")
        return
    if csv_rows is None:
        report.add("INP-1", stage, INP1_TITLE, NOT_EVALUABLE, csv_reason)
        return

    def beside(excluded: list[str], candidates: list) -> list[str]:
        """The excluded inputs that are not among the candidates."""
        keys = {_path_key(item) for item in candidates}
        return [item for item in excluded if _path_key(item) not in keys]

    owner_excluded = _excluded_inputs(owner)
    own_excluded = _excluded_inputs(provenance) if split else owner_excluded
    excluded_keys = {_path_key(item) for item in owner_excluded + own_excluded}
    outside = beside(owner_excluded, owner_candidates)
    # What the lease excluded itself is no candidate either, and is counted once beside them.
    owner_lease = _lease_excluded(owner)
    lease_out = beside([path for path, _reason in owner_lease.values()], owner_candidates + outside)
    # What the one encoding rule left unused (2026-10-09) is no candidate and no excluded input: its sample's choice
    # accounts for it, and it is counted once beside them.
    owner_unused = beside(_encoding_unused_paths(owner), owner_candidates + outside + lease_out)
    # A part that carries a declaration of its own samples' inputs: a list other than its parent's.
    own_list = split and own_declared is not None and own_declared != declared
    own_outside = beside(own_excluded, own_candidates) if own_list else []
    own_lease_out = beside([path for path, _reason in _lease_excluded(provenance).values()],
                           own_candidates + own_outside) if own_list else []
    own_unused = beside(_encoding_unused_paths(provenance), own_candidates + own_outside + own_lease_out) \
        if own_list else []
    # The candidates the CSV leaves out: the disposition found them there and excluded them.
    held = _excluded_candidates(provenance, own_candidates)
    counts = {"analysis_inputs": len(declared or []), "input_candidates": len(own_candidates),
              "analysis_files.csv rows": len(csv_rows)}
    if split:
        counts["parent input_candidates"] = len(owner_candidates)
    if own_list:
        counts["part analysis_inputs"] = len(own_declared)
    if excluded_keys:
        counts["excluded_inputs"] = len(excluded_keys)
    if held:
        counts["excluded input_candidates"] = len(held)
    if lease_out:
        counts["lease excluded_input_candidates"] = len(lease_out)
    if owner_unused:
        counts["unused encodings"] = len(owner_unused)
    # The archive members the lease included unattributed (2026-10-07): candidates no declared input is known to be.
    unattributed = _unattributed_members(provenance)
    owner_unattributed = {_path_key(row["path"]) for row in _lineage_rows(owner) if _is_unattributed(row)}
    owner_free = sum(1 for item in owner_candidates if _path_key(item) in owner_unattributed)
    own_free = sum(1 for item in own_candidates if _path_key(item) in unattributed.keys | owner_unattributed)
    if unattributed:
        counts["unattributed input_candidates"] = own_free

    def disagrees(count: int, held: int, free: int) -> bool:
        """Whether a declaration of `count` inputs disagrees with `held` candidates and excluded inputs, `free` of
        them unattributed members: equal where there are none, else at least the attributed ones."""
        return count != held if not free else count < held - free

    # A part's own list is its parent's cut to its samples, so a count it carries is the parent's.
    problems = [contradiction] if contradiction else []
    held_by_owner = len(owner_candidates) + len(outside) + len(lease_out) + len(owner_unused)
    if declared is not None and disagrees(len(declared), held_by_owner, owner_free):
        problems.append(
            f"the Catalog declared {len(declared)} analysis input(s) and the lease found {len(owner_candidates)} "
            "input candidate(s)" + (" in the parent" if split else "")
            + (f", {owner_free} of them unattributed members" if owner_free else "")
            + (f", with {len(outside)} more excluded" if outside else "")
            + (f", and excluded {len(lease_out)} itself" if lease_out else "")
            + (f", and left {len(owner_unused)} unused under the one encoding rule" if owner_unused else ""))
    if own_list and disagrees(len(own_declared), len(own_candidates) + len(own_outside) + len(own_lease_out)
                              + len(own_unused), own_free):
        problems.append(
            f"the part declares {len(own_declared)} analysis input(s) of its own samples and holds "
            f"{len(own_candidates)} input candidate(s)"
            + (f", {own_free} of them unattributed members" if own_free else "")
            + (f", with {len(own_outside)} more excluded" if own_outside else "")
            + (f", and its lease excluded {len(own_lease_out)} itself" if own_lease_out else "")
            + (f", and left {len(own_unused)} unused under the one encoding rule" if own_unused else ""))
    if unattributed:
        scoped, why = _unit_scoped(owner)
        if scoped is False:
            problems.append(f"{len(unattributed.members)} input candidate(s) are archive members no sample row pairs "
                            f"with, included unattributed, and {why}")
    choices, _choice_evidence = _unattributed_choice_problems(provenance, csv_rows, unattributed)
    problems.extend(choices)
    # The one encoding rule of 2026-10-09, recomputed from its record, beside a declaration as without one.
    encoding_problems, encoding_evidence = _encoding_run_problems(provenance, csv_rows)
    problems.extend(encoding_problems)
    if len(own_candidates) - len(held) != len(csv_rows):
        problems.append(f"the analysis CSV has {len(csv_rows)} row(s) for {len(own_candidates)} input candidate(s)"
                        + (f", {len(held)} of them excluded by the campaign disposition" if held else ""))
    # Which input each row opens, not only how many rows there are: a row that opens an excluded input,
    # or another candidate's twice, in place of one candidate's leaves every count above as it was.
    own_lease = _lease_excluded(provenance) if split else owner_lease
    lease_keys = set(owner_lease) | set(own_lease)
    known = [*own_candidates, *owner_excluded, *own_excluded,
             *(path for path, _reason in [*owner_lease.values(), *own_lease.values()])]
    opened = _inputs_opened(csv_rows, _input_keys_by_console_path(provenance), known)
    names = [str(row.get("file_name") or "") or Path(str(row.get("file_path") or "")).name for row in csv_rows]
    own_keys = {_path_key(item) for item in own_candidates}
    listed_excluded = [name for name, key in zip(names, opened) if key in excluded_keys]
    listed_lease = [name for name, key in zip(names, opened) if key in lease_keys and key not in excluded_keys]
    unknown = [name for name, key in zip(names, opened) if not key]
    rows_of: dict[str, list[str]] = {}
    for name, key in zip(names, opened):
        if key in own_keys and key not in excluded_keys | lease_keys:
            rows_of.setdefault(key, []).append(name)
    twice = [" and ".join(group) for group in rows_of.values() if len(group) > 1]
    if listed_excluded:
        problems.append(f"{len(listed_excluded)} CSV row(s) name an input the campaign disposition excluded "
                        f"({', '.join(listed_excluded[:5])})")
    if listed_lease:
        problems.append(f"{len(listed_lease)} CSV row(s) open an input the lease excluded "
                        f"({', '.join(listed_lease[:5])})")
    if unknown:
        problems.append(f"{len(unknown)} CSV row(s) open no input candidate of this unit, by their path, by a "
                        f"Console alias the lineage records or by file record ({', '.join(unknown[:5])})")
    if twice:
        problems.append(f"{len(twice)} input candidate(s) are opened by more than one CSV row ({'; '.join(twice[:5])})")
    excluded_names = [Path(item.rstrip("\\/")).name for item in dict.fromkeys(owner_excluded + own_excluded)][:10]
    lease = {"lease_excluded": [{"input": Path(item.rstrip("\\/")).name, "reason": owner_lease[_path_key(item)][1]}
                                for item in lease_out[:10]]} if lease_out else {}
    unattributed_evidence = unattributed.evidence() if unattributed else {}
    if encoding_evidence:
        lease["one_encoding_rule"] = encoding_evidence
    if problems:
        report.add("INP-1", stage, INP1_TITLE, FAIL,
                   "What MS-DIAL will open is not what the Catalog declared it opens: " + "; ".join(problems)
                   + ". A vendor folder read as its member files, or a sample dropped on the way, looks like this.",
                   counts=counts, excluded=excluded_names, **lease, **unattributed_evidence)
        return
    less = f", less the {len(held)} the campaign disposition excluded," if held else ""
    reasons = sorted({owner_lease[_path_key(item)][1] or "no reason recorded" for item in lease_out})
    by_lease = (f" and {len(lease_out)} input(s) the lease excluded itself ({', '.join(reasons)})"
                if lease_out else "")
    if owner_unused:
        by_lease += (f" and {len(owner_unused)} input(s) the one encoding rule left unused for another file of their "
                     "sample")
    if split:
        detail = ((f"The parent declared {len(declared)} analysis input(s), which are its {len(owner_candidates)} "
                   "input candidates" + (f" and {len(outside)} excluded input(s) that are none" if outside else "")
                   + by_lease + "; " if declared is not None else "")
                  + (f"this part declares {len(own_declared)} of its own samples'; " if own_list else "")
                  + f"this part's {len(own_candidates)} input candidates{less} are its {len(csv_rows)} CSV rows.")
    elif held or outside or lease_out or owner_unused:
        detail = (f"The {len(declared)} declared analysis input(s) are the {len(own_candidates)} input candidates"
                  + (f" and {len(outside)} excluded input(s) that are none" if outside else "")
                  + by_lease + f"; the candidates{less} are the {len(csv_rows)} CSV rows.")
    elif unattributed:
        detail = (f"The {len(declared)} declared analysis input(s) account for the {len(own_candidates) - own_free} "
                  f"input candidate(s) the lease attributed, and the {len(own_candidates)} candidates are the "
                  f"{len(csv_rows)} CSV rows.")
    else:
        detail = (f"The {len(declared)} declared analysis input(s) are the {len(own_candidates)} input candidates "
                  f"and the {len(csv_rows)} CSV rows.")
    sentence = _encoding_run_sentence(provenance, csv_rows)
    if sentence:
        detail += f" {sentence}."
    if unattributed:
        detail += (" " + unattributed.sentence() + ". Which declared input each one is, if any, is not recorded; "
                   "read them before the result is used.")
        report.add("INP-1", stage, INP1_TITLE, WARN, detail, counts=counts, excluded=excluded_names, **lease,
                   **unattributed_evidence)
        return
    report.add("INP-1", stage, INP1_TITLE, PASS, detail, counts=counts, excluded=excluded_names, **lease)


# The rules by which Interactive's lease pairs a declared raw-file name with an archive member that does
# not carry it exactly (lineage row name_pairing.paired_by). Both are inferences, and the user decided on
# 2026-10-06 that every one MUST always be left on record: a member that carries the declared name behind
# a prefix (021518_387057_CSHp_BioRec1.raw for BioRec1.raw), and a member whose leading identifier token
# matches the declared name's uniquely on both sides (VV_13 in VV_13_HEpG2_C1_exp344_pos.raw for
# VV_13_HEpG2_C1_pos.raw). Interactive warns of them as INFERRED_PAIRING_WARNING.
INFERRED_PAIRING_RULES = {
    "prefixed_member_name": "the declared name behind a prefix",
    "leading_identifier_token": "the same leading identifier token",
}
INFERRED_PAIRING_WARNING = "input_names_paired_by_inference"
PAIR1_TITLE = "Every input paired with a declared name by inference is on record"


def check_inferred_name_pairings_are_listed(report: Report, provenance: dict | None, reason: str) -> None:
    """PAIR-1. Every input the lease paired with a declared raw file by inference, not by its exact name,
    is listed in the gate report.

    The lease admits an archive member as a declared raw file when its name is the declared name, and,
    since Interactive 0.5.25, when it carries that name behind a prefix or shares its leading identifier
    token, one to one (repository_reanalysis._member_name_pairings, msdial-interactive-app#58). The lineage
    row of such an input records how (name_pairing: paired_by, declared_raw_file, member_name, and key, the
    shared identifier, for a pairing by leading identifier token). An exact name needs no
    record; an inferred one is a judgement about which file is which, which the user decided must always
    be on record (2026-10-06). This check lists each one, with the declared and the member name, as a
    WARN under INFERRED_PAIRING_WARNING, so the pairing is visible in every gate report of the unit.

    The lineage rows read are the unit's own (_own_lineage_rows): its manifest's, and for a split part those
    of its raw owner's rows whose input is one of the part's own input candidates, never a sibling part's
    (Interactive's split gives each part the parent's rows of its own inputs; the parent's excluded rows go
    to no part and stay the parent's record). Both the inputs and those the lease excluded are read; an
    input is listed once. A name_pairing whose paired_by is no
    rule this gate knows is listed apart and WARNs too: a pairing nobody here can describe is still a
    pairing. A unit whose lineage records no name_pairing PASSes; one with no input lineage at all, an
    older manifest or a unit not yet leased, is not evaluable and owed nothing.

    It judges nothing about the pairing itself. Whether the member is the declared file is the reader's
    question: INP-1, CLS-2 and CNT-1 judge what the run holds.

    UNATTRIBUTED MEMBERS (user decision, 2026-10-07). A row whose name_pairing.paired_by is
    "unattributed_member" is an archive member no sample row pairs with, included as an input of its own; the
    user decided it is always on record. Each is listed apart (unattributed_members, with
    manifest.unattributed_members.count) as a WARN under the warning code unattributed_members_included. The
    record itself is held to the rule (_unattributed_record_problems): manifest.unattributed_members counts and
    lists every such row under the rule unit_scoped_archive_2026_10_07, the manifest's and the disposition's
    warnings carry the code, no such row carries a sample row, and the download is unit-scoped. A record that
    falls short of that is a FAIL here.

    ONE ENCODING PER SAMPLE (the user's one rule of 2026-10-09; Interactive 0.5.36). Each sample whose data
    arrived in several encodings is listed with the file the rule used and every other with its reason
    (one_encoding_rule), and the check WARNs. Its record is held here (_encoding_record_problems): every choice
    follows the rule as recomputed from its candidates and reasons, a lineage row's encoding_choice uses that row's
    own file and is listed in manifest.encoding_choices, its stands_for is one of its sample's candidates, and every
    listed choice whose file is an input is carried by that input's row. A record short of that is a FAIL. Whether
    the run holds what the record lets it hold is INP-1's (_encoding_run_problems). A pairing a rule made for one
    file of a sample, where the rule used another (name_pairing.member_name is not the input's own name), is listed
    with the input that runs.

    RUN POLICY: record_only, as the task of 2026-10-06 placed it. A pairing is a fact to be read, and
    listing it changes nothing in what MS-DIAL computes.
    """
    stage = "before-production"
    if provenance is None:
        report.add("PAIR-1", stage, PAIR1_TITLE, NOT_EVALUABLE, reason)
        return
    rows: list[tuple[dict, str]] = []
    seen: set[str] = set()
    for part in ("rows", "excluded"):
        for row in _own_lineage_rows(provenance, part):
            key = _path_key(row["path"])
            if key not in seen:
                seen.add(key)
                rows.append((row, part))
    if not rows:
        report.add("PAIR-1", stage, PAIR1_TITLE, NOT_EVALUABLE,
                   "The manifest records no input lineage, so no pairing of a declared name with an input is "
                   "recorded to list.", required=False)
        return
    inferred: list[dict] = []
    unknown: list[dict] = []
    unattributed = _unattributed_members(provenance)
    for row, part in rows:
        pairing = row.get("name_pairing")
        if pairing is None or _is_unattributed(row):
            continue
        pairing = pairing if isinstance(pairing, dict) else {}
        entry = {
            "paired_by": str(pairing.get("paired_by") or ""),
            "declared_raw_file": str(pairing.get("declared_raw_file") or ""),
            "member_name": str(pairing.get("member_name") or "") or Path(str(row["path"]).rstrip("\\/")).name,
            "sample_id": str(row.get("sample_id") or ""),
        }
        if pairing.get("key") is not None:
            entry["key"] = pairing.get("key")
        if part == "excluded":
            entry["excluded_by_the_lease"] = True
        if entry["member_name"].casefold() != _row_name(row).casefold() and not _conversion_source(row):
            # The file the one encoding rule used for a sample whose row a rule paired with another of its files
            # (2026-10-09, clause 4): the pairing as it was made, and the input that runs.
            entry["input"] = _row_name(row)
        (inferred if entry["paired_by"] in INFERRED_PAIRING_RULES else unknown).append(entry)
    record_problems = _unattributed_record_problems(provenance, unattributed) if unattributed else []
    encoding_record = _encoding_record(provenance)
    encoding_problems = _encoding_record_problems(provenance, encoding_record)
    # A record that takes no unattributed member (a shared archive, a declared unit, a stem two sample rows share)
    # still says why it leaves out each member it lists.
    left_out_problems = [] if unattributed else _left_out_record_problems(unattributed.record)
    free_evidence = unattributed.evidence() if unattributed else {}
    free_sentence = (" " + unattributed.sentence() + "; which declared file each one is, if any, is not recorded."
                     if unattributed else "")
    encoding_sentence = _encoding_sentence(encoding_record)
    if encoding_sentence:
        free_sentence += f" {encoding_sentence}."
        free_evidence = {**free_evidence, "one_encoding_rule": _encoding_evidence(encoding_record)}
    if record_problems or encoding_problems or left_out_problems:
        said = []
        if record_problems:
            said.append(f"{len(unattributed.members)} input(s) in the lineage are archive members included "
                        "unattributed, and the record the rule of 2026-10-07 requires of them falls short: "
                        + "; ".join(record_problems))
        if left_out_problems:
            said.append("the record of the archive members the lease left out falls short: "
                        + "; ".join(left_out_problems))
        if encoding_problems:
            said.append("the record of the one encoding rule of 2026-10-09 does not say, by the rule, which file each "
                        "sample used: " + "; ".join(encoding_problems))
        report.add("PAIR-1", stage, PAIR1_TITLE, FAIL, ". ".join(said) + "." + free_sentence,
                   inputs=len(rows), paired_by_inference=len(inferred) + len(unknown),
                   record_problems=[*record_problems, *left_out_problems, *encoding_problems],
                   pairings=inferred, **({"pairings_by_unknown_rule": unknown} if unknown else {}), **free_evidence)
        return
    if not inferred and not unknown and not unattributed and not encoding_sentence:
        report.add("PAIR-1", stage, PAIR1_TITLE, PASS,
                   f"None of the {len(rows)} input(s) in the lineage was paired with a declared raw file by "
                   "inference.", inputs=len(rows))
        return
    if not inferred and not unknown:
        report.add("PAIR-1", stage, PAIR1_TITLE, WARN,
                   f"None of the {len(rows)} input(s) in the lineage was paired with a declared raw file by "
                   "inference." + free_sentence,
                   inputs=len(rows), paired_by_inference=0, by_rule={}, pairings=[], **free_evidence)
        return

    def described(entry: dict) -> str:
        key = f", key {entry['key']}" if "key" in entry else ""
        return (f"{entry['member_name']} as {entry['declared_raw_file'] or 'an unrecorded declared file'} "
                f"({entry['paired_by'] or 'no rule recorded'}{key})")

    by_rule = dict(sorted(Counter(entry["paired_by"] for entry in inferred + unknown).items()))
    listed = inferred + unknown
    detail = (f"{len(listed)} of the {len(rows)} input(s) in the lineage were paired with a declared raw file by "
              "inference, not by an exact name (" + ", ".join(f"{rule or 'unrecorded'} {count}"
                                                              for rule, count in by_rule.items()) + "): "
              + "; ".join(described(entry) for entry in listed[:10])
              + (f"; and {len(listed) - 10} more, all in the evidence" if len(listed) > 10 else "") + ".")
    if unknown:
        detail += (f" {len(unknown)} of them name a rule this gate does not know "
                   f"({', '.join(sorted({entry['paired_by'] or 'none' for entry in unknown}))}).")
    detail += " Each is the lease's inference of which file is which; read them before the result is used."
    detail += free_sentence
    if unattributed:
        free_evidence["warnings"] = [INFERRED_PAIRING_WARNING, UNATTRIBUTED_WARNING]
        free_evidence.pop("warning")
    report.add("PAIR-1", stage, PAIR1_TITLE, WARN, detail,
               warning=INFERRED_PAIRING_WARNING, inputs=len(rows), paired_by_inference=len(listed),
               by_rule=by_rule, pairings=inferred, **({"pairings_by_unknown_rule": unknown} if unknown else {}),
               **free_evidence)


def _absent_exports(run_manifest: dict | None) -> "tuple[int, list[str]] | None":
    """(planned, absent): how many exports the run manifest planned, and those of them not on disk.

    EXP-1's fact, which RET-1 shares; None when the manifest records no expected_analysis_exports.
    """
    if run_manifest is None or not isinstance(run_manifest.get("expected_analysis_exports"), list):
        return None
    expected = [Path(item) for item in run_manifest["expected_analysis_exports"]]
    return len(expected), [str(path) for path in expected if not path.exists()]


def check_expected_exports_present(
    report: Report, run_manifest: dict | None, output: Path, stage: str,
    provenance: dict | None = None,
) -> None:
    """Every file the run said it would produce must exist.

    The production job marks itself completed on the Console exit code alone; expected_analysis_exports
    is computed and then read by nothing.
    """
    if stage == "before-production":
        return
    exports = _absent_exports(run_manifest)
    if exports is None:
        report.add("EXP-1", stage, "Every expected export exists", NOT_EVALUABLE,
                   "The run manifest records no expected_analysis_exports.")
        return
    planned, absent = exports
    if not _production_started(output, provenance):
        report.add("EXP-1", stage, "Every expected export exists", NOT_EVALUABLE, NOT_STARTED,
                   expected=planned)
        return
    if not absent:
        report.add("EXP-1", stage, "Every expected export exists", PASS,
                   f"All {planned} expected exports are present.", expected=planned)
        return
    failure = _recorded_failure(provenance)
    report.add(
        "EXP-1", stage, "Every expected export exists", FAIL,
        (f"{failure} after producing {planned - len(absent)} of {planned} planned exports."
         if failure else
         "MS-DIAL reported success without producing every export the run planned. A file it could "
         "not read is skipped silently, and the exit code does not reflect it."
         + _earlier_failures(provenance)),
        expected=planned, absent_count=len(absent), absent=absent[:10],
    )


# --------------------------------------------------------------------------------------------
# the acquisition type the Console reads
# --------------------------------------------------------------------------------------------
#
# The Console deconvolutes each file by the acquisition_type its CSV row gives, and reads that value
# with Enum.TryParse over {DDA, SWATH, AIF, None}, ignoring case: anything it cannot parse, "DIA"
# among them, silently becomes DDA (MsdialCoreTestApp AnalysisFilesParser). A DIA file deconvoluted
# as DDA, or an MSe file as DDA, completes, validates and is wrong. Interactive maps each file's
# header verdict onto a Console type, console_acquisition_type in the per-file preflight record (DIA
# to SWATH where isolation windows are recorded or inferred, to AIF where MS2 is all-ion), and the CSV
# builder writes it. The extractor's own record, raw-metadata-preflight.json, is a third writer.

CONSOLE_ACQUISITION_TYPES = ("DDA", "SWATH", "AIF")
ACQ1_TITLE = "Each file runs as the acquisition type its header gives"
AIF1_TITLE = "Every AIF file has collision-energy targets"
# What the Console prints for each AIF collision-energy target at or below 0, which it skips
# (MsdialLcMsApi SpectrumDeconvolutionProcess). It names no file, and an empty target list prints nothing.
AIF_CE_MESSAGE = "No correct CE information in AIF-MSDEC"
CONSOLE_LOG_LIMIT = 256 * 1024 * 1024


def _per_file_records(provenance: dict) -> dict[str, dict]:
    """Each inspected input's per-file raw-header record, by path.

    From the unit's own preflight, then the verdicts a split part carried from its parent, then the
    parent's own preflight, as Interactive reads the acquisition start times.
    """
    def per_file(manifest: dict) -> object:
        preflight = manifest.get("raw_metadata_preflight")
        summary = preflight.get("summary") if isinstance(preflight, dict) else None
        return summary.get("per_file") if isinstance(summary, dict) else None

    sources = [per_file(provenance), provenance.get("header_verdicts_from_parent")]
    if isinstance(provenance.get("split_from"), dict):
        parent, _ = _raw_owner_manifest(provenance)
        if parent is not None:
            sources.append(per_file(parent))
    records: dict[str, dict] = {}
    for source in sources:
        for item in source if isinstance(source, list) else []:
            if isinstance(item, dict) and str(item.get("file") or "").strip():
                records.setdefault(_path_key(item["file"]), item)
    return records


def _lineage_manifests(provenance: dict) -> list[dict]:
    """The manifests that hold a unit's input lineage: its own and, for a split part, its raw owner's."""
    manifests = [provenance]
    if isinstance(provenance.get("split_from"), dict):
        parent, _ = _raw_owner_manifest(provenance)
        if parent is not None:
            manifests.append(parent)
    return manifests


def _lineage_rows(manifest: dict | None, part: str = "rows") -> list[dict]:
    """A manifest's input_lineage rows that name a path: its inputs ("rows"), or what the lease excluded."""
    lineage = (manifest or {}).get("input_lineage")
    rows = lineage.get(part) if isinstance(lineage, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and str(row.get("path") or "").strip()]


def _own_lineage_rows(provenance: dict, part: str = "rows") -> list[dict]:
    """The lineage rows of this unit's own inputs: all of its manifest's, and, for a split part, those of its
    raw owner's rows whose input is one of the part's input candidates. A sibling part's inputs, and the
    parent's rows of inputs no part runs, are not the part's."""
    rows = list(_lineage_rows(provenance, part))
    if isinstance(provenance.get("split_from"), dict):
        parent, _ = _raw_owner_manifest(provenance)
        if parent is not None and parent is not provenance:
            own = {_path_key(item) for item in provenance.get("input_candidates") or [] if str(item or "").strip()}
            rows += [row for row in _lineage_rows(parent, part) if _path_key(row["path"]) in own]
    return rows


def _input_keys_by_console_path(provenance: dict) -> dict[str, str]:
    """The input each Console path stands for, where the CSV names an alias of it rather than the input.

    A path or name the Console's CSV parser cannot read back (a comma, a quote, a character outside
    ASCII) is given to it through an alias in the raw tree, and the input's lineage row records the path
    the CSV gives it (console_path, console_alias). The raw-header records name the input itself.
    The CSV's writer wrote that record, so while both are on disk the alias must be the input (a
    junction or a hard link to it); one that is another file stands for nothing.
    """
    keys: dict[str, str] = {}
    for manifest in _lineage_manifests(provenance):
        for row in _lineage_rows(manifest):
            alias = row.get("console_alias") if isinstance(row.get("console_alias"), dict) else {}
            for console in (row.get("console_path"), alias.get("path")):
                if not str(console or "").strip() or _same_path(console, row["path"]):
                    continue
                if Path(str(console)).exists() and Path(str(row["path"])).exists():
                    try:
                        if not os.path.samefile(str(console), str(row["path"])):
                            continue
                    except OSError:
                        continue
                keys.setdefault(_path_key(console), _path_key(row["path"]))
    return keys


def _input_key(row: dict, aliases: dict[str, str]) -> str:
    """The key of the input a CSV row opens, through its Console alias where it has one; "" for no path."""
    path = str(row.get("file_path") or "")
    return aliases.get(_path_key(path), _path_key(path)) if path.strip() else ""


def _extractor_records(provenance: dict, workspace: Path) -> dict[str, dict]:
    """What the raw-metadata extractor itself wrote for each input, by path.

    The preflight names its output file; a workspace that moved keeps it under provenance/. A split
    part reads its own, then its parent's.
    """
    manifests = [(provenance, workspace)]
    if isinstance(provenance.get("split_from"), dict):
        parent, _ = _raw_owner_manifest(provenance)
        if parent is not None:
            manifests.append((parent, Path(str(parent.get("workspace") or ""))))
    records: dict[str, dict] = {}
    for manifest, root in manifests:
        preflight = manifest.get("raw_metadata_preflight")
        named = str(preflight.get("output") or "").strip() if isinstance(preflight, dict) else ""
        if not named:
            continue
        candidates = [Path(named)] + ([root / "provenance" / Path(named).name] if str(root) not in ("", ".") else [])
        path = next((item for item in candidates if item.is_file()), None)
        if path is None:
            continue
        try:
            parsed = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, RecursionError):
            continue
        for item in [parsed] if isinstance(parsed, dict) else parsed if isinstance(parsed, list) else []:
            source = item.get("source") if isinstance(item, dict) else None
            if isinstance(source, dict) and str(source.get("filePath") or "").strip():
                records.setdefault(_path_key(source["filePath"]), item)
    return records


def _extractor_acquisition(record: dict | None) -> dict:
    acquisition = (record or {}).get("acquisition")
    return acquisition if isinstance(acquisition, dict) else {}


def _extractor_method(record: dict | None) -> str:
    method = _extractor_acquisition(record).get("method")
    return str((method.get("value") if isinstance(method, dict) else method) or "")


def _isolation_windows(record: dict | None) -> int | None:
    """How many isolation targets the extractor recorded, or None where it wrote no record."""
    if record is None:
        return None
    targets = _extractor_acquisition(record).get("isolationWindowTargets")
    return len(targets) if isinstance(targets, list) else None


def _console_reads(value: str) -> str:
    """The AcquisitionType the pinned Console makes of a CSV value: Enum.TryParse ignoring case, else DDA."""
    text = value.strip()
    named = {name.casefold(): name for name in (*CONSOLE_ACQUISITION_TYPES, "None")}
    if text.casefold() in named:
        return named[text.casefold()]
    if re.fullmatch(r"[+-]?\d{1,9}", text):
        return {0: "DDA", 1: "SWATH", 2: "AIF", 3: "None"}.get(int(text), f"the undefined value {int(text)}")
    return "DDA"


def _header_contradiction(method: str, windows: int | None, value: str) -> str:
    """How a CSV acquisition type contradicts the extractor's own verdict, or "".

    Two or more isolation targets are windows (SWATH); Waters MSe records one and is all-ion.
    """
    if method in ("DDA", "AIF") and value != method:
        return f"its header says {method}, and the Console will deconvolute it as {value}"
    if method == "DIA" and value == "DDA":
        return "its header says DIA, and the Console will deconvolute it as DDA"
    if method == "DIA" and value == "AIF" and (windows or 0) >= 2:
        return (f"its header says DIA with {windows} isolation targets recorded, and the Console will deconvolute it "
                "as AIF, which ignores them")
    return ""


# RULE B2, HEADER FIRST (user decision, 2026-10-06; Interactive 0.5.29, msdial-interactive-app#62). A file
# whose header was read and has MS2 runs as its header gives, over the repository's declaration (the Catalog's
# keyword inference) and at any extractor confidence; the per-file record keeps what the header alone gives
# as header_console_acquisition_type, which no disposition rewrites. Before it, a disposition kept the
# declaration over a header below 0.8 confidence; that exemption is gone, here as in Interactive.
#
# Header verdicts that give a Console type: the extractor's DIA is SWATH or AIF by its isolation.
HEADER_CONSOLE_METHODS = ("DDA", "DIA", "AIF", "SWATH")
# Units declared in the DIA family, whose MS1-only files rule B2 never folds into a DDA run (Interactive's
# _DECLARED_DIA_FAMILY): they may be all-ion data exported as MS1 scans.
DECLARED_DIA_FAMILY = ("DIA", "AIF", "SWATH")
# SANCTIONED MAPPINGS: a row that runs as another Console type than its header gives, which the user has
# allowed, by (header type, row type), with the name of the binding disposition's field that must record the
# mapping. A mapping is accepted only where the disposition records it and the file's own record says that rule
# decided it (_sanctioned_mapping).
#
# AIF RUN AS SWATH (user decision, 2026-10-07; Interactive 0.5.31). An AIF unit whose included files carry one
# distinct MS2 collision energy (Waters LockSpray excluded) runs as SWATH-type in the Console: ST004304 gives
# identical results either way, and MTBKS281 matches its 30 eV collection. Interactive records it as
# campaign_disposition.aif_run_as_swath = {"collision_energies": [<one energy>], "rule": AIF_AS_SWATH_RULE}, and
# each such file's per-file record keeps header_console_acquisition_type "AIF" with console_acquisition_type
# "SWATH" and console_acquisition_basis AIF_AS_SWATH_BASIS. A multi-energy AIF unit is held until a patched
# Console exists (campaign_disposition hold, aif_multi_ce_awaiting_console), never run as SWATH: a record of more
# than one energy sanctions nothing.
AIF_AS_SWATH_FIELD = "aif_run_as_swath"
AIF_AS_SWATH_RULE = "single_ce_aif_as_swath_2026_10_07"
AIF_AS_SWATH_BASIS = "aif_single_ce_as_swath"
#
# MULTI-ENERGY AIF WITH #825 (Interactive 0.5.34, msdial-interactive-app#67). The patched Console is MsdialWorkbench
# #825: it deconvolutes a multi-energy AIF file once per energy and represents each peak by the energy of its MS/MS
# reference-spectrum match, else the energy with the most product ions. Where the Console Interactive decided for
# has it, a unit whose AIF inputs all record the same MS2 collision energies, more than one, runs as AIF (header
# type AIF, row AIF, so no mapping is involved): the disposition records aif_multi_ce_run = {"collision_energies",
# "rule": AIF_MULTI_CE_RULE} beside its probe of that Console (multi_energy_aif_console: capability, available,
# probe, assembly_sha256), and each input's record carries console_acquisition_basis AIF_MULTI_CE_BASIS and its own
# ms2_collision_energies. Without #825 the unit is held as before. ACQ-1 reads those records: a multi-energy AIF
# row passes only where they say so and the Console the run manifest names is the one the probe found #825 in (or,
# where it is another, the gate finds #825's markers in that Console's assembly itself).
#
# ENERGY SETS THAT DIFFER BETWEEN INPUTS (user decision, 2026-10-08, answer 6 "run as is"; Interactive 0.5.36,
# msdial-interactive-app#69). #825 chooses a representative energy among one file's energies, never across files,
# so inputs whose energy sets differ (one energy each but not the same one, different sets, or a multi-energy file
# beside a single-energy one) are each processed with their own per-file representative energy. Interactive
# 0.5.34-0.5.35 held such a unit (aif_collision_energies_differ_between_inputs); 0.5.36 runs it as AIF under the
# same rule and records it: aif_multi_ce_run.energy_sets_differ true, aif_multi_ce_run.collision_energy_sets (each
# distinct set with its file count), the disposition's aif_collision_energies_by_input (each input's own set, keyed
# by its path relative to the data root: AIF_CE_BY_INPUT_KEY) and the warning aif_energy_sets_differ_between_inputs.
# ACQ-1 then holds each row to its own recorded set instead of the unit's union, and reports that the sets differ as
# a WARN: recorded, never a reason to stop the run (AIF_CE_SETS_DIFFER_POLICY).
#
# HOW THE SETS NAME AN INPUT (user decision, 2026-10-08, second round, answer 2; Interactive 0.5.36,
# raw_metadata_preflight.aif_input_key). By the input's path relative to the manifest's input_directory (for a split
# part, the parent's, which the part's manifest repeats), '/'-separated, case as given, computed by os.path.relpath
# on the recorded paths without resolving links; with no input_directory, the path as the per-file record gives it.
# Keys are compared without case. Two inputs of one basename in two folders (POS/QC_01.mzML, NEG/QC_01.mzML) are two
# keys, each with its own set, and ACQ-1 holds each row to its own: a basename lookup would hold both to one set,
# or FAIL every input in a subfolder. An input directly in the data root has its basename as its key. The
# disposition says so in aif_collision_energies_by_input_key; one that names another scheme is not read.
AIF_MULTI_CE_FIELD = "aif_multi_ce_run"
AIF_CE_SETS_DIFFER_FIELD = "energy_sets_differ"
AIF_CE_SETS_FIELD = "collision_energy_sets"
AIF_CE_BY_INPUT_FIELD = "aif_collision_energies_by_input"
AIF_CE_BY_INPUT_KEY_FIELD = "aif_collision_energies_by_input_key"
AIF_CE_BY_INPUT_KEY = "path_relative_to_input_directory"
AIF_CE_SETS_DIFFER_WARNING = "aif_energy_sets_differ_between_inputs"
AIF_CE_SETS_DIFFER_DECISION = "run_as_is_2026_10_08"
AIF_CE_SETS_DIFFER_POLICY = RECORD_ONLY
AIF_MULTI_CE_RULE = "multi_ce_aif_with_console_825"
AIF_MULTI_CE_BASIS = "aif_multi_ce_console_825"
MULTI_ENERGY_AIF_PROBE_FIELD = "multi_energy_aif_console"
# Mirrored from Interactive's workflow.MULTI_ENERGY_AIF_CAPABILITY and MULTI_ENERGY_AIF_CONSOLE_MARKERS: two messages
# of #825's RepresentativeDeconvolutionReader, .NET literals in the Console assembly (UTF-16LE). Both are required:
# a local AIF patch build from before #825's representative-energy rule carries the second only.
MULTI_ENERGY_AIF_CAPABILITY = "multi_energy_aif_representative_collision_energy"
MULTI_ENERGY_AIF_CONSOLE_MARKERS = ("The collision-energy files of ", " nor a collision-energy file exists.")
# Interactive compares collision energies to 0.1 eV (raw_metadata_preflight.COLLISION_ENERGY_DECIMALS).
COLLISION_ENERGY_DECIMALS = 1
SANCTIONED_ACQUISITION_MAPPINGS: dict[tuple[str, str], str] = {("AIF", "SWATH"): AIF_AS_SWATH_FIELD}
ACQ1_SOURCES = {
    "header": "from headers", "declaration": "from the repository declaration",
    "folded_ms1_only": "MS1-only folded into DDA", "unresolved": "with no Console type resolved",
    "unsettled": "DIA with its scheme unsettled", "other": "from another source", "no_record": "with no header record",
    "sanctioned": f"AIF run as SWATH under {AIF_AS_SWATH_RULE}",
}


def _is_gcms(provenance: dict | None) -> bool:
    """A unit whose project says it was separated by gas chromatography."""
    project = (provenance or {}).get("project")
    separation = str(project.get("separation") or "") if isinstance(project, dict) else ""
    return separation.strip().casefold().startswith("gc")


def _binding_dispositions(provenance: dict) -> list[dict]:
    """The binding campaign dispositions that decided a unit's files: its own, and a split part's parent's."""
    manifests = [provenance]
    if isinstance(provenance.get("split_from"), dict):
        manifests.append(_raw_owner_manifest(provenance)[0])
    return [item for item in (_binding_disposition(manifest) for manifest in manifests) if item]


def _declared_vs_header(dispositions: list[dict]) -> dict[str, dict]:
    """What a campaign disposition recorded where a header disagreed with the declaration, by file."""
    entries: dict[str, dict] = {}
    for disposition in dispositions:
        items = disposition.get("declared_vs_header")
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and str(item.get("file") or "").strip():
                entries.setdefault(_path_key(item["file"]), item)
    return entries


def _header_confidence(record: dict | None, entry: dict | None) -> float | None:
    for value in ((record or {}).get("confidence"), (entry or {}).get("confidence")):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
    return None


def _record_header_console(record: dict) -> "str | None":
    """The Console type a file's header alone gives, as Interactive's entry_header_console reads it: the
    record's header_console_acquisition_type where it has the field (0.5.29 on), else DDA, SWATH or AIF as its
    acquisition_mode says; a legacy DIA recorded no isolation, and gives none."""
    if "header_console_acquisition_type" in record:
        value = record.get("header_console_acquisition_type")
        return str(value) if value else None
    mode = str(record.get("acquisition_mode") or "").strip()
    return mode if mode in CONSOLE_ACQUISITION_TYPES else None


def _aif_as_swath_energies(entry: object) -> "tuple[list[float] | None, str]":
    """The collision energies an aif_run_as_swath record gives, rounded to two places as the Console rounds them,
    each once; or None, and why the record sanctions nothing."""
    if not isinstance(entry, dict):
        return None, f"the campaign disposition records no {AIF_AS_SWATH_FIELD}"
    if str(entry.get("rule") or "") != AIF_AS_SWATH_RULE:
        return None, (f"its {AIF_AS_SWATH_FIELD} names the rule {str(entry.get('rule') or '') or 'none'!r}, not "
                      f"{AIF_AS_SWATH_RULE}")
    values = entry.get("collision_energies")
    if not isinstance(values, list) or not all(
            isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) for item in values):
        return None, f"its {AIF_AS_SWATH_FIELD} records no list of collision energies"
    energies = sorted({round(float(item), 2) for item in values})
    if len(energies) != 1:
        return None, (f"its {AIF_AS_SWATH_FIELD} records {len(energies)} collision energies, and only a single-energy "
                      f"AIF unit runs as SWATH (a multi-energy one runs as AIF under {AIF_MULTI_CE_RULE} on a Console "
                      "with MsdialWorkbench#825, and is held otherwise)")
    return energies, ""


def _energy_set(values: object) -> "tuple[float, ...] | None":
    """The distinct collision energies a recorded list gives, to 0.1 eV as Interactive compares them; None where
    nothing is recorded (no list)."""
    if not isinstance(values, list):
        return None
    return tuple(sorted({round(float(item), COLLISION_ENERGY_DECIMALS) for item in values
                         if isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item)}))


def _energies_text(energies: "tuple[float, ...] | list[float] | None") -> str:
    return ", ".join(f"{value:g}" for value in energies) + " eV" if energies else "no energy"


def _aif_multi_ce_record(dispositions: list[dict]) -> "tuple[dict | None, tuple[float, ...], str]":
    """(the binding disposition whose aif_multi_ce_run names AIF_MULTI_CE_RULE and two or more energies, those
    energies, ""); or (None, (), why no binding disposition sanctions a multi-energy AIF run)."""
    reasons = []
    for disposition in dispositions:
        entry = disposition.get(AIF_MULTI_CE_FIELD)
        if not isinstance(entry, dict):
            held = ", ".join(str(item) for item in disposition.get("reasons") or [])
            reasons.append(f"the campaign disposition records no {AIF_MULTI_CE_FIELD}"
                           + (f" (it holds the unit: {held})" if disposition.get("hold") is True and held else ""))
            continue
        if str(entry.get("rule") or "") != AIF_MULTI_CE_RULE:
            reasons.append(f"its {AIF_MULTI_CE_FIELD} names the rule {str(entry.get('rule') or '') or 'none'!r}, not "
                           f"{AIF_MULTI_CE_RULE}")
            continue
        energies = _energy_set(entry.get("collision_energies")) or ()
        if len(energies) < 2:
            reasons.append(f"its {AIF_MULTI_CE_FIELD} records {len(energies)} collision energies, not two or more")
            continue
        return disposition, energies, ""
    return None, (), "; ".join(dict.fromkeys(reasons)) or f"no binding campaign disposition records {AIF_MULTI_CE_FIELD}"


def _probe_ready(probe: object) -> bool:
    """Whether a multi_energy_aif_console record shows #825 in the Console it read, as Interactive's
    raw_metadata_preflight.multi_energy_aif_ready reads it."""
    return (isinstance(probe, dict) and probe.get("capability") == MULTI_ENERGY_AIF_CAPABILITY
            and probe.get("available") is True)


def _assembly_markers(path: Path) -> "tuple[bool | None, str]":
    """Whether a Console assembly carries every #825 marker (True, False), or None and why it was not read."""
    try:
        binary = path.read_bytes()
    except OSError as error:
        return None, f"its assembly could not be read ({type(error).__name__})"
    present = [marker.encode("utf-16-le") in binary or marker.encode("utf-8") in binary
               for marker in MULTI_ENERGY_AIF_CONSOLE_MARKERS]
    if all(present):
        return True, ""
    return False, "its assembly carries only some of #825's markers" if any(present) else "its assembly carries none of #825's markers"


def _multi_energy_aif_console(probe: object, run_manifest: "dict | None") -> "tuple[bool, str, dict]":
    """Whether the Console the run manifest names has MsdialWorkbench#825: (shown, how or why not, evidence).

    Shown where the disposition's probe found #825 in an assembly whose sha256 is the one the run manifest records
    for the Console that runs; or, where the probe read another Console or none, where the gate finds every #825
    marker in the assembly the run manifest names, whose sha256 is still the one recorded."""
    console = (run_manifest or {}).get("console") if isinstance(run_manifest, dict) else None
    console = console if isinstance(console, dict) else {}
    recorded = str(console.get("assembly_sha256") or console.get("binary_sha256") or "").strip().casefold()
    probed = str(probe.get("assembly_sha256") or "").strip().casefold() if isinstance(probe, dict) else ""
    evidence = {"run_console_assembly_sha256": recorded or None,
                "probe": probe.get("probe") if isinstance(probe, dict) else None,
                "probe_available": _probe_ready(probe), "probe_assembly_sha256": probed or None}
    if not isinstance(run_manifest, dict):
        return False, "no run manifest records the Console that runs it", evidence
    if not recorded:
        return False, "the run manifest records no Console assembly sha256", evidence
    if _probe_ready(probe) and probed == recorded:
        evidence["shown_by"] = "disposition_probe"
        return True, "the disposition's probe found #825 in the assembly the run manifest records", evidence
    named = str(console.get("assembly_path") or console.get("path") or "").strip()
    assembly = Path(named) if named else None
    probed_text = ("the disposition's probe read another Console" if _probe_ready(probe) else
                   f"the disposition's probe found no #825 ({evidence['probe'] or 'not probed'})")
    if assembly is None or not assembly.is_file():
        return False, probed_text + ", and the assembly the run manifest names is not on disk to read", evidence
    try:
        actual = _file_sha256(assembly)
    except OSError as error:
        return False, f"{probed_text}, and the assembly the run manifest names could not be read ({type(error).__name__})", evidence
    if actual != recorded:
        return False, f"{probed_text}, and the assembly the run manifest names is no longer the one it recorded", evidence
    found, why = _assembly_markers(assembly)
    if found:
        evidence["shown_by"] = "gate_read_the_assembly"
        return True, "the gate found #825's markers in the assembly the run manifest records", evidence
    return False, f"{probed_text}, and the Console the run manifest names has no MsdialWorkbench#825 ({why})", evidence


def _aif_input_key(path: object, input_directory: object) -> str:
    """An input's key in aif_collision_energies_by_input, as Interactive's raw_metadata_preflight.aif_input_key gives
    it (AIF_CE_BY_INPUT_KEY): its path relative to the data root, '/'-separated and case as given, where both are
    absolute; otherwise the path as given, '/'-separated. Never resolved: a store lease links its members in."""
    text = str(path or "")
    root = str(input_directory or "").strip()
    if root and os.path.isabs(text) and os.path.isabs(root):
        try:
            return os.path.relpath(os.path.normpath(text), os.path.normpath(root)).replace("\\", "/")
        except ValueError:
            # Another drive: no relative path exists.
            pass
    return text.replace("\\", "/")


def _multi_energy_aif_failures(
    rows: "list[tuple[str, tuple[float, ...] | None, str, str]]", dispositions: list[dict],
    run_manifest: "dict | None",
) -> "tuple[list[str], dict]":
    """Why the rows that run as multi-energy AIF may not, and the evidence. Each row is (name, its record's
    ms2_collision_energies, its record's console_acquisition_basis, its key in aif_collision_energies_by_input:
    _aif_input_key of its record's file).

    Under a binding disposition its aif_multi_ce_run decides: without one naming AIF_MULTI_CE_RULE and two or more
    energies nothing runs as multi-energy AIF, and each row's basis must be AIF_MULTI_CE_BASIS. Each row must record
    its energies, and they must be the recorded ones: the unit's, or, where aif_multi_ce_run records
    energy_sets_differ true (the run-as-is decision of 2026-10-08), the set the disposition's
    aif_collision_energies_by_input records under its key (compared without case), which must be among the unit's
    energies; a disposition whose aif_collision_energies_by_input_key names a scheme other than AIF_CE_BY_INPUT_KEY
    is not read, and each such row FAILs. Without a
    binding disposition (no campaign) no such decision is recorded, and the rows' energies must agree. Either way
    the Console the run manifest names must be shown to have #825 (_multi_energy_aif_console). Where the decision
    is recorded, evidence["energy_sets_differ"] is true and evidence["collision_energy_sets"] gives each set the
    rows record and how many rows record it."""
    failures: list[str] = []
    record, energies, why = _aif_multi_ce_record(dispositions)
    evidence: dict = {"rows": len(rows), "rule": AIF_MULTI_CE_RULE}
    first = rows[0][0]
    if dispositions and record is None:
        failures.append(
            f"{len(rows)} row(s) run as AIF with more than one MS2 collision energy (the first {first}), and {why}: a "
            f"multi-energy AIF unit runs as AIF only under {AIF_MULTI_CE_RULE}, on a Console with MsdialWorkbench#825, "
            "and is held otherwise (aif_multi_ce_awaiting_console)")
        return failures, evidence
    if record is not None:
        run = record.get(AIF_MULTI_CE_FIELD)
        evidence[AIF_MULTI_CE_FIELD] = run
        differ = isinstance(run, dict) and run.get(AIF_CE_SETS_DIFFER_FIELD) is True
        by_input = record.get(AIF_CE_BY_INPUT_FIELD) if differ else None
        # Keyed by each input's path under the data root (AIF_CE_BY_INPUT_KEY), compared without case.
        by_input = ({str(key).replace("\\", "/").casefold(): value for key, value in by_input.items()}
                    if isinstance(by_input, dict) else {})
        scheme = str(record.get(AIF_CE_BY_INPUT_KEY_FIELD) or "") if differ else ""
        if differ:
            evidence[AIF_CE_BY_INPUT_KEY_FIELD] = scheme or None
        for name, own, basis, file_name in rows:
            if basis != AIF_MULTI_CE_BASIS:
                failures.append(f"{name}: runs as multi-energy AIF, and its record's console_acquisition_basis is "
                                f"{basis or 'none'!r}, not {AIF_MULTI_CE_BASIS}")
            elif not own:
                # Interactive holds a unit with an input that records no energy (aif_collision_energy_unrecorded),
                # whether the inputs' sets differ or not.
                failures.append(f"{name}: records no ms2_collision_energies, and {AIF_MULTI_CE_RULE} runs a unit only "
                                "where every input records its MS2 collision energies (Interactive holds one that does "
                                "not: aif_collision_energy_unrecorded)")
            elif differ and scheme and scheme != AIF_CE_BY_INPUT_KEY:
                failures.append(f"{name}: the campaign disposition's {AIF_CE_BY_INPUT_FIELD} is keyed by {scheme!r} "
                                f"({AIF_CE_BY_INPUT_KEY_FIELD}), and this gate reads it keyed by "
                                f"{AIF_CE_BY_INPUT_KEY} only, so no set it records can be told to be this input's")
            elif differ:
                recorded = _energy_set(by_input.get(file_name.casefold()))
                if not recorded:
                    failures.append(f"{name}: records {_energies_text(own)}, and the campaign disposition's "
                                    f"{AIF_CE_BY_INPUT_FIELD} records no set for {file_name} (its path relative to "
                                    f"the data root, {AIF_CE_BY_INPUT_KEY}), though its {AIF_MULTI_CE_FIELD} says the "
                                    "inputs' sets differ")
                elif own != recorded:
                    failures.append(f"{name}: records {_energies_text(own)}, not the {_energies_text(recorded)} the "
                                    f"campaign disposition's {AIF_CE_BY_INPUT_FIELD} records for {file_name}")
                elif not set(own) <= set(energies):
                    failures.append(f"{name}: records {_energies_text(own)}, which are not all among the unit's "
                                    f"{_energies_text(energies)} that its {AIF_MULTI_CE_FIELD} records")
            elif own != energies:
                failures.append(f"{name}: records {_energies_text(own)}, not the unit's {_energies_text(energies)}, "
                                f"and its {AIF_MULTI_CE_FIELD} does not record that the inputs' sets differ "
                                f"({AIF_CE_SETS_DIFFER_FIELD}, the run-as-is decision of 2026-10-08): #825 chooses among "
                                "one file's energies, never across files, so only that decision runs inputs whose "
                                "sets differ")
        if differ:
            counted = Counter(own for _name, own, _basis, _file in rows if own)
            warned = record.get("warnings")
            evidence.update({
                AIF_CE_SETS_DIFFER_FIELD: True, "decision": AIF_CE_SETS_DIFFER_DECISION,
                "energy_sets_differ_run_policy": AIF_CE_SETS_DIFFER_POLICY,
                AIF_CE_SETS_FIELD: [{"collision_energies": list(values), "rows": count}
                                    for values, count in sorted(counted.items())],
                "recorded_" + AIF_CE_SETS_FIELD: run.get(AIF_CE_SETS_FIELD),
                "disposition_warning": AIF_CE_SETS_DIFFER_WARNING in (warned if isinstance(warned, list) else []),
            })
    else:
        sets = {own for _name, own, _basis, _file in rows}
        energies = next(iter(sets)) if len(sets) == 1 and None not in sets else ()
        if len(sets) > 1:
            failures.append(f"{len(rows)} row(s) run as multi-energy AIF, and their inputs record different MS2 "
                            "collision energies (" + "; ".join(sorted(_energies_text(item) for item in sets if item))
                            + "): #825 chooses a representative energy among one file's energies, never across files")
    evidence["collision_energies"] = list(energies)
    probe = record.get(MULTI_ENERGY_AIF_PROBE_FIELD) if record is not None else None
    shown, how, console = _multi_energy_aif_console(probe, run_manifest)
    evidence.update(console=console, console_shown=shown, console_detail=how)
    if not shown:
        failures.append(f"{len(rows)} row(s) run as multi-energy AIF ({_energies_text(energies)}; the first {first}), "
                        f"which only an MS-DIAL Console with MsdialWorkbench#825 processes, and {how}; decide the unit "
                        "again with the Console that will run it")
    return failures, evidence


def _sanctioned_mapping(header: str, value: str, key: str, dispositions: list[dict],
                        record: "dict | None" = None) -> "tuple[str, str]":
    """(rule, "") where SANCTIONED_ACQUISITION_MAPPINGS allows this file's header type to run as the row's type and
    the binding disposition records the mapping; else ("", why it is not sanctioned, "" where no rule applies).

    AIF as SWATH (2026-10-07): a binding disposition's aif_run_as_swath names AIF_AS_SWATH_RULE and exactly one
    collision energy, and the file's per-file record says that rule decided its type (console_acquisition_basis
    AIF_AS_SWATH_BASIS). The disposition decides for the unit, so the file is not named in it."""
    field_name = SANCTIONED_ACQUISITION_MAPPINGS.get((header, value))
    if not field_name:
        return "", ""
    if field_name != AIF_AS_SWATH_FIELD:
        return "", f"no rule of this gate reads {field_name}"
    reasons = []
    for disposition in dispositions:
        energies, why = _aif_as_swath_energies(disposition.get(field_name))
        if energies is None:
            reasons.append(why)
            continue
        basis = str((record or {}).get("console_acquisition_basis") or "")
        if basis != AIF_AS_SWATH_BASIS:
            reasons.append(f"its record's console_acquisition_basis is {basis or 'none'!r}, not {AIF_AS_SWATH_BASIS}")
            continue
        return AIF_AS_SWATH_RULE, ""
    return "", "; ".join(dict.fromkeys(reasons)) or f"no binding campaign disposition records {field_name}"


def _declared_dia_family(dispositions: list[dict]) -> "tuple[str, str] | None":
    """(declared mode, its source) where a binding disposition records a unit declared DIA, AIF or SWATH."""
    for disposition in dispositions:
        declared = disposition.get("declared") if isinstance(disposition.get("declared"), dict) else {}
        mode = str(declared.get("acquisition_mode") or "").strip()
        if mode.upper() in DECLARED_DIA_FAMILY:
            return mode, str(disposition.get("declared_acquisition_source") or "")
    return None


def check_acquisition_type_is_the_headers(
    report: Report, provenance: dict | None, reason: str, csv_rows: list[dict] | None, csv_reason: str,
    run_manifest: "dict | None" = None,
) -> None:
    """ACQ-1. Every CSV row's acquisition_type is a Console type, and the one its file's header gives.

    The domain first, wherever the CSV exists: the pinned Console turns any value it cannot parse into
    DDA without a word, so the column takes DDA, SWATH or AIF, and a GC-MS unit's None besides. That
    needs no header.

    Then each row against console_acquisition_type where the per-file record carries it, and always
    against the extractor's own verdict, written by another program than the one that mapped it: a DDA
    or AIF header must run as itself, and a DIA header never as DDA, nor as AIF where windows are
    recorded. A record written before console_acquisition_type existed says DIA without saying which;
    its extractor record's windows settle SWATH, and without them the row is a WARN.

    RULE B2, HEADER FIRST (user decision, 2026-10-06; Interactive 0.5.29). A row FAILs where its type is not
    the one its file's header alone gives (header_console_acquisition_type: DDA, SWATH or AIF), at any
    confidence and whatever the record's console_acquisition_basis, unless SANCTIONED_ACQUISITION_MAPPINGS
    allows that mapping and the binding disposition records it (_sanctioned_mapping). One mapping is sanctioned
    (user decision, 2026-10-07): an AIF header runs as SWATH where campaign_disposition.aif_run_as_swath names the
    rule single_ce_aif_as_swath_2026_10_07 and exactly one collision energy, and the file's record says that rule
    decided it; the row is then a WARN naming the rule, never a PASS, and a record of more than one energy (a
    multi-energy AIF unit) sanctions nothing. It FAILs too where the type is not the one the record decided
    (console_acquisition_type).

    MULTI-ENERGY AIF WITH #825 (Interactive 0.5.34). A row that runs as AIF is a multi-energy AIF row where its
    record gives more than one MS2 collision energy (ms2_collision_energies, to 0.1 eV), where its record's basis is
    aif_multi_ce_console_825, or where a binding disposition records aif_multi_ce_run. It runs as its header gives,
    so it PASSes only where (_multi_energy_aif_failures): a binding disposition records aif_multi_ce_run under the
    rule multi_ce_aif_with_console_825 with two or more energies, and each such row's record carries that basis and
    records its energies, which are those energies, or, where aif_multi_ce_run records energy_sets_differ true (the
    user's run-as-is decision of 2026-10-08, Interactive 0.5.36), the set the disposition's
    aif_collision_energies_by_input records for its input, keyed by the input's path relative to the manifest's
    input_directory and compared without case (AIF_CE_BY_INPUT_KEY: the second-round answer 2 of 2026-10-08, so two
    inputs of one basename in two folders are each held to their own set), among the unit's energies; and the
    Console the run manifest (output/run-manifest.json) records is shown to have MsdialWorkbench#825: the
    disposition's multi_energy_aif_console probe found it in the assembly whose sha256 the run manifest records,
    or the gate finds both of #825's markers in that assembly itself. A multi-energy AIF row on a Console without
    #825 FAILs, and so does one whose disposition holds the unit or records no such run. Without a binding
    disposition (no campaign) the rows' energies must agree and the Console must be shown the same way. Where the
    inputs' sets differ and nothing is refused, ACQ-1 is a WARN that names each set and the decision: that the sets
    differ is recorded (record_only) and never stops the run, while every refusal above still does. A
    single-energy AIF unit is still expected as SWATH under single_ce_aif_as_swath_2026_10_07, whatever the
    Console. A record written before header_console_acquisition_type existed gives DDA,
    SWATH or AIF as its acquisition_mode says, and the extractor's own verdict is read beside it, as before;
    the exemption that let a disposition keep the declaration over a header below 0.8 confidence is gone.
    A WARN only names what a row's type rests on where it is not a header's: the repository's declaration
    where the header gave no Console type (an unreadable header, a DIA header whose isolation left SWATH
    and AIF open), and an MS1-only file folded into a DDA run. That fold FAILs where the binding disposition
    records the unit as declared DIA, AIF or SWATH, where rule B2 excludes such files
    (ms1_only_in_declared_dia_unit); only a disposition written before 0.5.29 holds one. A row a binding
    campaign disposition gave no Console type stays a FAIL: a unit whose acquisition is still unknown does
    not run (user decision, 2026-09-30).

    The header comparison is not required without a preflight: a unit whose acquisition mode was known
    from the repository never needed a header read (PRE-1). Nor is it made for a GC-MS unit, outside
    this campaign's LC-MS/MS scope, whose EI spectra are deconvoluted as MS1 whatever the column says.

    RUN POLICY: blocks_run, as the user named it (2026-10-01). A file deconvoluted as another
    acquisition type completes, validates and is wrong.
    """
    stage = "before-production"
    gcms = _is_gcms(provenance)
    domain = CONSOLE_ACQUISITION_TYPES + (("None",) if gcms else ())
    domain_text = f"{', '.join(domain[:-1])} or {domain[-1]}"
    rows = csv_rows or []
    failures: list[str] = []
    for row in rows:
        value = str(row.get("acquisition_type") or "")
        if value not in domain:
            name = str(row.get("file_name") or "") or Path(str(row.get("file_path") or "")).name or "a row with no file"
            failures.append(f"{name}: acquisition_type {value!r} is not {domain_text}, and the Console reads it as "
                            f"{_console_reads(value)}")
    unparsed = len(failures)
    types = dict(Counter(str(row.get("acquisition_type") or "") for row in rows))
    type_text = ", ".join(f"{key} {count}" for key, count in sorted(types.items()))

    # The rows that run as multi-energy AIF (name, recorded energies, basis), and why they may not.
    multi_rows: "list[tuple[str, tuple[float, ...] | None, str, str]]" = []
    multi_failures: list[str] = []

    def refuse(**evidence) -> None:
        typed = len(failures) - unparsed - len(multi_failures)
        parts = ([f"{unparsed} carry an acquisition_type other than {domain_text}"] if unparsed else []) + (
            [f"{typed} would be deconvoluted as an acquisition type their header does not give"]
            if typed > 0 else []) + (
            [f"{len(multi_rows)} run as multi-energy AIF where the records or the Console do not allow it"]
            if multi_failures else [])
        report.add("ACQ-1", stage, ACQ1_TITLE, FAIL,
                   f"Of the {len(rows)} row(s), " + " and ".join(parts) + ", which completes, validates and is wrong: "
                   + "; ".join(failures[:3]) + ".",
                   failures=failures[:10], rows=len(rows), types=types, **evidence)

    records = _per_file_records(provenance) if provenance is not None else {}
    if provenance is None or not records or gcms:
        if failures:
            refuse()
            return
        parsed = f"every row's acquisition_type is one the Console parses ({type_text})" if rows else ""
        if provenance is None:
            why = (parsed[:1].upper() + parsed[1:] + "; " if parsed else "") + reason
        elif gcms:
            why = ("This is a GC-MS unit, whose spectra MS-DIAL deconvolutes as MS1 whatever the column says, so no "
                   "header comparison is made" + (f"; {parsed}." if parsed else "."))
        elif not parsed:
            why = "No per-file raw-header record is recorded, so no header verdict stands to compare the CSV with."
        else:
            why = (parsed[:1].upper() + parsed[1:] + "; no per-file raw-header record is recorded, so no header "
                   "verdict stands to compare them with.")
        report.add("ACQ-1", stage, ACQ1_TITLE, NOT_EVALUABLE, why, required=False,
                   **({"rows": len(rows), "types": types} if csv_rows is not None else {}))
        return
    if csv_rows is None:
        report.add("ACQ-1", stage, ACQ1_TITLE, NOT_EVALUABLE, csv_reason)
        return
    extracted = _extractor_records(provenance, report.workspace)
    aliases = _input_keys_by_console_path(provenance)
    dispositions = _binding_dispositions(provenance)
    decisions = _declared_vs_header(dispositions)
    declared_dia = _declared_dia_family(dispositions)
    multi_ce_recorded = any(isinstance(item.get(AIF_MULTI_CE_FIELD), dict) for item in dispositions)
    # The data root aif_collision_energies_by_input's keys are relative to (_data_root).
    input_directory = _data_root(provenance)
    warnings: list[str] = []
    basis: Counter = Counter()
    sources: Counter = Counter()
    sanctioned = 0
    sanctioned_rows: list[str] = []
    for row in csv_rows:
        path = str(row.get("file_path") or "")
        name = str(row.get("file_name") or "") or Path(path).name or "a row with no file"
        value = str(row.get("acquisition_type") or "")
        if value not in CONSOLE_ACQUISITION_TYPES:
            continue
        key = _input_key(row, aliases)
        record = records.get(key) if key else None
        header = extracted.get(key) if key else None
        method = str((record or {}).get("acquisition_mode") or "") or _extractor_method(header)
        windows = _isolation_windows(header)
        if record is None:
            basis["no_record"] += 1
            sources["no_record"] += 1
            contradiction = _header_contradiction(method, windows, value)
            (failures if contradiction else warnings).append(
                f"{name}: " + (contradiction or f"no raw-header record names it, so no header verdict stands "
                               f"behind its {value}"))
            continue
        if "console_acquisition_type" in record:
            basis["console_acquisition_type"] += 1
            console = record.get("console_acquisition_type")
            decided = str(record.get("console_acquisition_basis") or "")
            header_console = _record_header_console(record)
            if console is not None and console not in CONSOLE_ACQUISITION_TYPES:
                failures.append(f"{name}: its record's console_acquisition_type {console!r} is no Console type, so "
                                "its header verdict was never resolved to DDA, SWATH or AIF")
                continue
            mapped = ""
            if header_console in CONSOLE_ACQUISITION_TYPES and header_console != value:
                mapped, why = _sanctioned_mapping(header_console, value, key, dispositions, record)
                if not mapped:
                    failures.append(f"{name}: its header gives {header_console}, and the Console will deconvolute it "
                                    f"as {value}" + ("" if decided in ("", "header") else
                                                     f" (decided on the basis {decided!r})")
                                    + (f"; no sanctioned mapping covers it: {why}" if why else ""))
                    continue
            if console is not None and console != value:
                failures.append(f"{name}: its record decided {console}, and the Console will deconvolute it as {value}")
                continue
            if value == "AIF":
                energies = _energy_set(record.get("ms2_collision_energies"))
                basis_text = str(record.get("console_acquisition_basis") or "")
                if (energies and len(energies) > 1) or basis_text == AIF_MULTI_CE_BASIS or multi_ce_recorded:
                    # The key aif_collision_energies_by_input names this input by, as Interactive keys it: the
                    # record's file relative to the data root (AIF_CE_BY_INPUT_KEY), never its basename.
                    input_key = _aif_input_key(str(record.get("file") or "") or path, input_directory)
                    multi_rows.append((name, energies, basis_text, input_key or name))
            if mapped:
                sanctioned += 1
                sources["sanctioned"] += 1
                sanctioned_rows.append(f"{name}: its header gives {header_console}, and it runs as {value} under {mapped}")
                continue
            contradiction = _header_contradiction(method, windows, value) if header_console is None else ""
            if contradiction:
                failures.append(f"{name}: {contradiction}")
                continue
            confidence = _header_confidence(record, decisions.get(key))
            if console is None:
                sources["unresolved"] += 1
                if dispositions:
                    failures.append(f"{name}: the campaign disposition gave it no Console acquisition type, and a unit "
                                    "whose acquisition is still unknown does not run (user decision, 2026-09-30)")
                else:
                    warnings.append(f"{name}: its header gives no Console acquisition type, so its {value} rests on "
                                    "something other than the raw headers")
            elif decided == "folded_ms1_only":
                sources["folded_ms1_only"] += 1
                if declared_dia is not None:
                    mode, source = declared_dia
                    failures.append(f"{name}: its header gives {method or 'no acquisition mode'} with no MS2, and the "
                                    f"campaign disposition folded it into the DDA run of a unit declared {mode}, where "
                                    "rule B2 (2026-10-06) excludes it as ms1_only_in_declared_dia_unit"
                                    + (f" (declared by {source})" if source else
                                       "; the disposition predates Interactive 0.5.29"))
                else:
                    warnings.append(f"{name}: its header gives {method or 'no acquisition mode'} with no MS2 to "
                                    "deconvolute, and the campaign disposition folded it into the DDA run")
            elif decided == "declaration" and header_console is None:
                sources["declaration"] += 1
                warnings.append(f"{name}: its {value} is the repository's declaration, which the campaign disposition "
                                f"took where its header gives {method or 'no acquisition mode'}"
                                + ("" if confidence is None else f" at confidence {confidence:.2f}")
                                + " and no Console acquisition type")
            elif decided == "declaration":
                sources["declaration"] += 1
            else:
                sources["header"] += 1
            continue
        basis["acquisition_mode"] += 1
        contradiction = _header_contradiction(method, windows, value)
        if contradiction:
            failures.append(f"{name}: {contradiction}")
        elif method == "DIA" and (windows or 0) < 2:
            sources["unsettled"] += 1
            warnings.append(f"{name}: its header says DIA, its record predates console_acquisition_type, and "
                            + ("the extractor wrote no record of it" if windows is None else
                               f"the extractor recorded {windows} isolation target(s)")
                            + f", so whether {value} is right is not settled")
        elif method not in ("DDA", "AIF", "DIA"):
            sources["other"] += 1
            warnings.append(f"{name}: its header gives {method or 'no acquisition mode'}, which is no Console "
                            f"acquisition type, so its {value} rests on something other than the raw headers")
        else:
            sources["header"] += 1
    evidence = {"basis": dict(basis), "sources": dict(sources), "extractor_records": len(extracted)}
    overrides = [entry for entry in decisions.values() if str(entry.get("basis") or "") == "header"]
    if overrides:
        # acquisition_header_overrides_declaration: each declaration a header decided over, with its source.
        evidence["header_overrides_declaration"] = len(overrides)
        evidence["declaration_sources"] = dict(Counter(str(entry.get("declaration_source") or "unrecorded")
                                                       for entry in overrides))
    multi_text = ""
    differing = ""
    if multi_rows:
        refused, evidence["multi_energy_aif"] = _multi_energy_aif_failures(multi_rows, dispositions, run_manifest)
        multi_failures.extend(refused)
        failures.extend(refused)
        multi = evidence["multi_energy_aif"]
        sha = str((multi.get("console") or {}).get("run_console_assembly_sha256") or "")
        multi_text = (f" {len(multi_rows)} of them run as multi-energy AIF ({_energies_text(multi.get('collision_energies'))}) "
                      f"under {AIF_MULTI_CE_RULE}: {multi.get('console_detail')}"
                      + (f" ({sha[:12]})" if sha else "") + ", and that Console represents each peak by the energy of "
                      "its MS/MS reference-spectrum match, else the energy with the most product ions.")
        sets = multi.get(AIF_CE_SETS_FIELD) or []
        if multi.get(AIF_CE_SETS_DIFFER_FIELD) is True and not refused:
            differing = (
                f"The {len(multi_rows)} multi-energy AIF row(s) record {len(sets)} different set(s) of MS2 collision "
                "energies (" + "; ".join(f"{_energies_text(item['collision_energies'])} in {item['rows']} row(s)"
                                          for item in sets)
                + "): #825 chooses among one file's energies only, so each file is processed with its own "
                "representative collision energy, and representative energies can differ between files. Each row "
                f"records the set the campaign disposition's {AIF_CE_BY_INPUT_FIELD} records for its input, by its "
                "path relative to the data root. The unit "
                f"runs as it is, on record (user decision, 2026-10-08; {AIF_CE_SETS_DIFFER_WARNING}); that the sets "
                "differ is recorded here and stops no run.")
    if sanctioned:
        evidence["sanctioned_mappings"] = sanctioned
        evidence["sanctioned_rule"] = AIF_AS_SWATH_RULE
        evidence[AIF_AS_SWATH_FIELD] = next((item.get(AIF_AS_SWATH_FIELD) for item in dispositions
                                             if _aif_as_swath_energies(item.get(AIF_AS_SWATH_FIELD))[0] is not None), None)
    if failures:
        refuse(warnings=warnings[:10], **({"sanctioned": sanctioned_rows[:10]} if sanctioned_rows else {}), **evidence)
        return
    if warnings or sanctioned_rows or differing:
        sentences = [differing] if differing else []
        if sanctioned_rows:
            energies, _why = _aif_as_swath_energies(evidence.get(AIF_AS_SWATH_FIELD))
            energy = f"{energies[0]:g} eV" if energies else "one collision energy"
            sentences.append(
                f"{len(sanctioned_rows)} of the {len(csv_rows)} row(s) run as SWATH where their header gives AIF, under "
                f"the rule {AIF_AS_SWATH_RULE} (the user's interim rule of 2026-10-07: a unit whose included files carry "
                f"a single MS2 collision energy, here {energy}, runs as SWATH-type in the Console, as the campaign "
                "disposition records it): " + "; ".join(sanctioned_rows[:3]) + ".")
        if warnings:
            other = {key: count for key, count in sources.items() if key != "sanctioned"}
            sentences.append(
                f"No row runs against its header verdict, but {len(warnings)} of the {len(csv_rows)} rest "
                "on something else for their acquisition type ("
                + ", ".join(f"{count} {ACQ1_SOURCES[key]}" for key, count in other.items()) + "): "
                + "; ".join(warnings[:3]) + ".")
        report.add("ACQ-1", stage, ACQ1_TITLE, WARN, " ".join(sentences) + multi_text,
                   warnings=warnings[:10], rows=len(csv_rows), types=types,
                   **({"sanctioned": sanctioned_rows[:10]} if sanctioned_rows else {}), **evidence)
        return
    report.add("ACQ-1", stage, ACQ1_TITLE, PASS,
               f"All {len(csv_rows)} row(s) run as the acquisition type their raw header gives ({type_text})."
               + multi_text,
               rows=len(csv_rows), types=types, **evidence)


def _ce_targets(record: dict | None, header: dict | None) -> list[float] | None:
    """The collision energies recorded for a file, or None where nothing records them.

    From the per-file record where Interactive carries them, else from the extractor's own record, which
    keeps the energies above 0 among the MS2 headers it sampled.
    """
    for values in ((record or {}).get("collision_energies"), _extractor_acquisition(header).get("collisionEnergies")
                   if header is not None else None):
        if isinstance(values, list):
            numbers = [float(item) for item in values
                       if isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item)]
            if numbers or not values:
                return numbers
    return None


def _console_log_hits(provenance: dict | None, output: Path) -> tuple[int, list[str]]:
    """How often the Console's output this unit keeps says it skipped an AIF target, and what was read.

    Interactive keeps the last lines of a failed run's log in the manifest (run_failures, and
    run_attempts where they carry one); a successful run's log stays with the job, so only a log kept
    in the output (*.log) is read beside them.
    """
    hits, sources = 0, []
    record = provenance if isinstance(provenance, dict) else {}
    for key in ("run_failures", "run_attempts"):
        items = record.get(key)
        for index, item in enumerate(items if isinstance(items, list) else []):
            tail = item.get("log_tail") if isinstance(item, dict) else None
            if isinstance(tail, list):
                sources.append(f"{key}[{index}]")
                hits += sum(1 for line in tail if AIF_CE_MESSAGE in str(line))
    for path in sorted(output.glob("*.log")):
        try:
            if path.stat().st_size > CONSOLE_LOG_LIMIT:
                sources.append(f"{path.name} (not read: larger than {CONSOLE_LOG_LIMIT // (1024 * 1024)} MB)")
                continue
            with path.open(encoding="utf-8", errors="replace") as handle:
                hits += sum(1 for line in handle if AIF_CE_MESSAGE in line)
            sources.append(path.name)
        except OSError as error:
            sources.append(f"{path.name} (not read: {error})")
    return hits, sources


def check_aif_files_have_collision_energies(
    report: Report, provenance: dict | None, csv_rows: list[dict] | None, csv_reason: str, output: Path,
    stage: str,
) -> None:
    """AIF-1. Every file the Console runs as AIF has a collision-energy target it can use.

    The Console deconvolutes an AIF file once per collision-energy target and skips every target at or
    below 0, printing only that it did (AIF_CE_MESSAGE, with no file named). A file whose target list is
    empty gets no MS2 deconvolution at all, and nothing says so. The targets are read from the per-file
    raw-header record; the extractor samples spectrum headers and also reads an energy under activation,
    where the Console reads spectrum-level energies only, so a target recorded here is the extractor's
    evidence and not the Console's list. A Console with MsdialWorkbench#825 stops instead on an AIF file whose MS2
    scans carry no collision energy, and Interactive 0.5.34 holds such a unit before it runs
    (aif_collision_energy_unrecorded), with or without #825.

    Never a FAIL, and never required: the user decided on 2026-09-30 that an AIF file with an empty
    target list is recorded with a warning and still runs. The files are named; the Console's own line is
    attributed to the files that can print it.
    """
    if stage == "before-production":
        return
    if csv_rows is None:
        report.add("AIF-1", stage, AIF1_TITLE, NOT_EVALUABLE, csv_reason, required=False)
        return
    aif = [row for row in csv_rows if _console_reads(str(row.get("acquisition_type") or "")) == "AIF"]
    if not aif:
        report.add("AIF-1", stage, AIF1_TITLE, NOT_EVALUABLE, "No file is run as AIF.", required=False)
        return
    records = _per_file_records(provenance) if isinstance(provenance, dict) else {}
    extracted = _extractor_records(provenance, report.workspace) if isinstance(provenance, dict) else {}
    aliases = _input_keys_by_console_path(provenance) if isinstance(provenance, dict) else {}
    none_usable, some_skipped, unknown, usable = [], [], [], []
    for row in aif:
        path = str(row.get("file_path") or "")
        name = str(row.get("file_name") or "") or Path(path).name
        key = _input_key(row, aliases)
        targets = _ce_targets(records.get(key), extracted.get(key)) if key else None
        count = (records.get(key) or {}).get("collision_energy_count") if key else None
        if targets is None and isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            # Interactive's per-file record counts the extractor's energies, which are those above 0.
            (usable if count else none_usable).append(name)
            continue
        if targets is None:
            unknown.append(name)
            continue
        # Rounded to two places before the comparison, as the Console does.
        kept = [value for value in targets if round(value, 2) > 0]
        (none_usable if not kept else some_skipped if len(kept) < len(targets) else usable).append(name)
    hits, sources = _console_log_hits(provenance, output)
    evidence = {"aif_files": len(aif), "without_usable_target": none_usable[:10], "with_a_skipped_target": some_skipped[:10],
                "targets_unrecorded": unknown[:10], "console_log_lines": hits, "console_logs_read": sources[:10]}
    if none_usable or hits:
        parts = []
        if none_usable:
            parts.append(f"{len(none_usable)} of the {len(aif)} AIF file(s) have no collision-energy target above 0 in "
                         f"their raw-header record ({', '.join(none_usable[:5])}): the Console gets no MS2 deconvolution "
                         "for such a file, and says nothing")
        if hits:
            # The line names no file. Only a file whose recorded targets include one at or below 0 prints it,
            # or one whose targets are not recorded.
            named = ([str(aif[0].get("file_name") or "")] if len(aif) == 1 else some_skipped or unknown)
            parts.append(f"the Console's log says '{AIF_CE_MESSAGE}' {hits} time(s), so it skipped a target at or below "
                         "0 for " + (", ".join(named[:5]) if named else
                                     "one of the AIF files, and neither the log nor the records say which"))
        sentence = "; ".join(parts)
        report.add("AIF-1", stage, AIF1_TITLE, WARN,
                   sentence[:1].upper() + sentence[1:] + ". Recorded, not refused (user decision, 2026-09-30).",
                   **evidence)
        return
    if unknown:
        report.add("AIF-1", stage, AIF1_TITLE, NOT_EVALUABLE,
                   f"The collision-energy targets of {len(unknown)} of the {len(aif)} AIF file(s) are recorded nowhere "
                   f"({', '.join(unknown[:5])}), and no Console log kept here says one was skipped.",
                   required=False, **evidence)
        return
    report.add("AIF-1", stage, AIF1_TITLE, PASS,
               f"Each of the {len(aif)} AIF file(s) has a collision-energy target above 0 in its raw-header record"
               + (f", and no Console log read here ({', '.join(sources[:3])}) says one was skipped." if sources else
                  "; no Console log is kept in this workspace, so the Console's own reading is not seen."),
               **evidence)


# --------------------------------------------------------------------------------------------
# class and run order
# --------------------------------------------------------------------------------------------

_QC_TOKENS = ("qc",)
_BLANK_TOKENS = ("blank",)


def check_class_distribution(report: Report, csv_rows: list[dict] | None, reason: str, stage: str,
                             provenance: "dict | None" = None) -> None:
    """Report the executed grouping, and flag names a substring matcher would reclassify.

    Sample category is re-derived downstream by substring match over file_type + class_id, so a
    biological class whose name contains "qc" or "blank" is removed from the comparison and
    simultaneously used as the QC-precision basis. The study that motivated this check contains a
    wine strain named QA23 with samples QA1..QA3; it survives that matcher, but only just.

    Rows of archive members the lease included unattributed (user decision, 2026-10-07; _unattributed_members)
    carry the unit's abstention Class or "Unattributed": a stated grouping, and a WARN that lists them with
    manifest.unattributed_members.count, since no sample row says what they are.

    RUN POLICY: record_only, as the user named it (2026-10-01). The grouping decides the comparison
    made from the results, not the spectra each file yields.
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
    unattributed = _unattributed_members(provenance)
    free = _unattributed_csv_names(provenance, csv_rows, unattributed)
    free_evidence = {}
    free_sentence = ""
    if free:
        carried = Counter(str(row.get("class_id", "")).strip() for row in csv_rows
                          if str(row.get("file_name") or "") in set(free))
        free_evidence = {**unattributed.evidence(), "unattributed_rows": len(free),
                         "unattributed_classes": dict(carried)}
        free_sentence = (f" {len(free)} of the {len(csv_rows)} rows are archive members no sample row pairs with, "
                         f"included unattributed under the rule {UNATTRIBUTED_RULE} "
                         f"(manifest.unattributed_members.count {unattributed.recorded_count}), in Class "
                         + ", ".join(f"{label!r} ({count})" for label, count in sorted(carried.items()))
                         + ": " + ", ".join(free[:5]) + (", ..." if len(free) > 5 else "")
                         + ". No sample row says what they are.")
    if blank_named:
        report.add(
            "CLS-1", stage, "Executed grouping is stated and unambiguous", WARN,
            "A class name contains 'qc' or 'blank' as a substring. Sample category is re-derived by "
            "substring match downstream, so this class may be pulled out of the biological "
            "comparison and used as the QC or blank basis instead. Confirm the intent before running."
            + free_sentence,
            classes=dict(classes), file_types=dict(file_types), matched_names=blank_named, **free_evidence,
        )
        return
    if free:
        report.add("CLS-1", stage, "Executed grouping is stated and unambiguous", WARN,
                   f"{len(classes)} classes over {len(csv_rows)} samples." + free_sentence,
                   classes=dict(classes), file_types=dict(file_types), **free_evidence)
        return
    report.add("CLS-1", stage, "Executed grouping is stated and unambiguous", PASS,
               f"{len(classes)} classes over {len(csv_rows)} samples.",
               classes=dict(classes), file_types=dict(file_types))


HEADER_ORDER_SOURCE = "raw_header_acquisition_start_time"
# Interactive 0.5.4 records what a repository unit's order was taken from when the headers did not
# give it; a run-order criterion is not to be assessed against the file listing or a sequence number
# read out of the file names (decided 2026-09-29).
UNRECORDED_ORDER_SOURCES = {"listing": "the file listing", "embedded": "a sequence number read out of the file names"}
# The repository's own sample table records an injection order. (A header order is judged by
# _header_order_agreement, which also checks that the recorded times order the files.)
DECLARED_ORDER_SOURCE = "repository_sample_table"


def _parse_header_time(value) -> "datetime | None":
    from datetime import datetime

    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = re.sub(r"(T\d{2}:\d{2}:\d{2})\.(\d+)",
                  lambda match: match.group(1) + "." + (match.group(2) + "000000")[:6], text, count=1)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _opened_name(row: dict, aliases: dict[str, str]) -> str:
    """The file name of the input a CSV row opens, without case: its own path's, or, where the row names
    a Console alias, that of the input the alias stands for (_input_keys_by_console_path)."""
    path = str(row.get("file_path", ""))
    target = aliases.get(_path_key(path)) if path.strip() else None
    return Path((target or path).replace("\\", "/")).name.casefold()


def _order_stems(provenance: dict | None, csv_rows: list[dict]) -> dict[str, str]:
    """The CSV file_name an analytical-order record may name a row by other than the row's own.

    Interactive's record names each file as the CSV does, by its file_name, since it builds the CSV from
    the input lineage (repository_analysis_rows.order_rows): an input read through a Console alias is
    named in the CSV by neither its own stem nor its path. A record naming an aliased row's input by
    the input's own name is read as that row, where no row carries the name as its file_name and only
    one row opens an input of that stem; two inputs sharing a stem are told apart by nothing but the
    CSV's names. Empty for a unit with no alias, as every unit prepared before the lineage-built CSV.
    """
    if not isinstance(provenance, dict) or not csv_rows:
        return {}
    aliases = _input_keys_by_console_path(provenance)
    if not aliases:
        return {}
    names = {str(row.get("file_name", "")).strip().casefold() for row in csv_rows}
    stems: dict[str, list[str]] = {}
    for row in csv_rows:
        stem = Path(_opened_name(row, aliases)).stem
        if stem:
            stems.setdefault(stem, []).append(str(row.get("file_name", "")).strip().casefold())
    return {stem: rows[0] for stem, rows in stems.items() if len(rows) == 1 and stem not in names}


def _recorded_order_source(provenance: dict | None, csv_rows: list[dict] | None) -> "str | None":
    """What the unit manifest records the CSV's analytical order as taken from, or None.

    The rule Interactive 0.5.4 withholds the run-order criterion by: the CSV's files are among the
    unit's inputs (by name) and keep the relative order the record gives them, a file dropped since
    or the ranks renumbered included. A reordered CSV carries an order nobody recorded, and a record
    with tied or unreadable ranks orders nothing. A header record from before order_source reads as
    the header source; any other record without it as unknown.

    A row naming a Console alias is the input the alias stands for (_opened_name), as Interactive's
    recorded_order_source reads it, and a record may name it by that input's own name (_order_stems).
    """
    record = (provenance or {}).get("analytical_order")
    if not isinstance(record, dict) or not csv_rows:
        return None
    source = record.get("order_source") or (
        HEADER_ORDER_SOURCE if record.get("derived_from") == HEADER_ORDER_SOURCE else None)
    if not isinstance(source, str) or not source:
        return None
    inputs = {Path(str(path).replace("\\", "/")).name.casefold()
              for path in (provenance or {}).get("input_candidates") or [] if str(path).strip()}
    aliases = _input_keys_by_console_path(provenance) if isinstance(provenance, dict) else {}
    if not inputs or any(_opened_name(row, aliases) not in inputs for row in csv_rows):
        return None
    translate = _order_stems(provenance, csv_rows)
    files = record.get("files") if isinstance(record.get("files"), list) else []
    recorded: dict = {}
    for item in files:
        if not isinstance(item, dict):
            return None
        stem = Path(str(item.get("file", ""))).stem.casefold()
        stem = translate.get(stem, stem)
        rank = _as_rank(item.get("analytical_order"))
        if not stem or stem in recorded or rank is None or rank in recorded.values():
            return None
        recorded[stem] = rank
    carried: list = []
    seen: set = set()
    for row in csv_rows:
        stem = str(row.get("file_name", "")).strip().casefold()
        rank = _as_rank(row.get("analytical_order"))
        if not stem or stem in seen or stem not in recorded or rank is None:
            return None
        seen.add(stem)
        carried.append((recorded[stem], rank))
    carried.sort()
    if not all(later[1] > earlier[1] for earlier, later in zip(carried, carried[1:])):
        return None
    return source


def _as_rank(value) -> "int | None":
    text = str(value if value is not None else "").strip()
    return int(text) if text.isdecimal() else None


def _header_order_agreement(provenance: dict | None, csv_rows: list[dict] | None) -> tuple[bool, list[str]] | None:
    """Whether the CSV carries the order the unit manifest says the raw headers gave.

    None when the manifest records no header-derived order, or when the headers order nothing
    the listing did not decide: every file at one time, or files of different Classes at one
    time. Then the row-order heuristic judges it as before. Otherwise (agrees, mismatches):
    the CSV must carry each recorded file exactly once at its recorded rank, and the ranks must
    follow from the recorded times, any order being allowed within a group of equal times. A
    record of the wrong shape, or one whose times cannot be compared, is a mismatch, never a
    traceback, and is found before any tie lets the heuristic take over.
    """
    record = (provenance or {}).get("analytical_order")
    if record is None:
        return None
    if not isinstance(record, dict):
        return (False, ["analytical_order in the unit manifest is not an object"])
    if record.get("derived_from") != HEADER_ORDER_SOURCE or not csv_rows:
        return None
    files = record.get("files") if isinstance(record.get("files"), list) else []
    entries = [item for item in files if isinstance(item, dict)]
    mismatches = [] if len(entries) == len(files) else ["a recorded file entry is not an object"]

    # Identity first: every recorded file once in the CSV, every CSV row recorded once. A recorded name
    # is the CSV's for its row, or an aliased row's input's own (_order_stems).
    translate = _order_stems(provenance, csv_rows)

    def stem_of(item: dict) -> str:
        stem = Path(str(item.get("file", ""))).stem.casefold()
        return translate.get(stem, stem)

    recorded = Counter(stem_of(item) for item in entries)
    in_csv = Counter(str(row.get("file_name", "")).strip().casefold() for row in csv_rows)
    for stem in sorted(set(recorded) | set(in_csv)):
        if recorded[stem] != 1 or in_csv[stem] != 1:
            mismatches.append(f"{stem}: {recorded[stem]} recorded, {in_csv[stem]} in the CSV")

    timed = []
    for item in entries:
        stem = stem_of(item)
        when = _parse_header_time(item.get("acquisition_start_time"))
        if when is None:
            mismatches.append(f"{item.get('file')}: no readable recorded time")
            continue
        timed.append((when, stem, _as_rank(item.get("analytical_order"))))
    if len({when.tzinfo is None for when, _, _ in timed}) > 1:
        mismatches.append("recorded times mix values with and without a UTC offset, so they cannot be ranked")
    if mismatches:
        return (False, mismatches)

    groups: dict = {}
    for when, stem, rank in timed:
        groups.setdefault(when, []).append((stem, rank))
    class_of = {str(row.get("file_name", "")).strip().casefold(): str(row.get("class_id", "")).strip()
                for row in csv_rows}
    if len(timed) > 1 and len(groups) == 1:
        return None
    if any(len(members) > 1 and len({class_of.get(stem, "") for stem, _ in members}) > 1
           for members in groups.values()):
        return None

    position = 1
    for when in sorted(groups):
        members = groups[when]
        expected = set(range(position, position + len(members)))
        if {rank for _, rank in members} != expected:
            mismatches.append(
                f"{', '.join(stem for stem, _ in members)}: recorded as "
                f"{sorted(str(rank) for _, rank in members)}, but their time ranks them {sorted(expected)}"
            )
        position += len(members)
    ranks = {stem: rank for _, stem, rank in timed}
    for row in csv_rows:
        name = str(row.get("file_name", "")).strip().casefold()
        if name in ranks and _as_rank(row.get("analytical_order")) != ranks[name]:
            mismatches.append(f"{row.get('file_name')}: CSV {row.get('analytical_order')}, recorded {ranks[name]}")
    return (not mismatches, mismatches)


def check_analytical_order_is_real(
    report: Report, csv_rows: list[dict] | None, reason: str, stage: str,
    provenance: dict | None = None,
) -> None:
    """Decide whether the recorded injection order is a measurement or a row number.

    When a repository records no injection sequence, the order is synthesized from CSV row order,
    which is grouped by class. A run-order drift statistic computed against it is perfectly
    confounded with the biological factor, and a near-zero correlation is an artifact of an order
    that does not exist rather than evidence of analytical stability.

    An order Interactive ranked from the raw headers' acquisition start times is a measurement,
    and the unit manifest says so with every file's time. It passes when the CSV carries exactly
    that order and fails when it does not, whatever the numbers look like: a header order can
    happen to equal the row order, and a row order can happen not to.

    The record names each file as the CSV does (file_name) since Interactive builds the CSV from the
    input lineage; one that names an aliased row's input by its own name is read as that row
    (_order_stems), as _recorded_order_source reads it.

    RUN POLICY: record_only, as the user named it (2026-10-01). The order decides what a run-order
    metric may claim, which ORD-2 holds at publication, not what each file yields.
    """
    if stage != "before-production":
        return
    if csv_rows is None:
        report.add("ORD-1", stage, "Recorded analytical order is a measurement", NOT_EVALUABLE, reason)
        return
    raw = [str(row.get("analytical_order", "")).strip() for row in csv_rows]
    if not all(value.isdecimal() for value in raw) or not raw:
        report.add("ORD-1", stage, "Recorded analytical order is a measurement", NOT_EVALUABLE,
                   "analytical_order is absent or not numeric on every row.")
        return
    agreement = _header_order_agreement(provenance, csv_rows)
    if agreement is not None:
        agrees, mismatches = agreement
        if agrees:
            report.add("ORD-1", stage, "Recorded analytical order is a measurement", PASS,
                       f"analytical_order is the acquisition order the raw headers record for all "
                       f"{len(csv_rows)} files.", samples=len(csv_rows), source=HEADER_ORDER_SOURCE)
        else:
            report.add("ORD-1", stage, "Recorded analytical order is a measurement", FAIL,
                       "The unit manifest records an order ranked from the raw headers, but "
                       "analysis_files.csv carries a different one.", mismatches=mismatches[:10])
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
        single = len(set(classes)) == 1
        report.add(
            "ORD-1", stage, "Recorded analytical order is a measurement", WARN,
            "analytical_order is exactly the row number. It may still be the true injection order, "
            "but nothing here establishes that it is."
            + (" With every sample in one Class no Class pattern in it can say either way, so ORD-2 "
               "warns on any run-order criterion reported against it." if single else ""),
            samples=len(order), single_class=single,
        )
        return
    report.add("ORD-1", stage, "Recorded analytical order is a measurement", PASS,
               "analytical_order is not a restatement of row order.", samples=len(order))


def _order_kind(csv_rows: list[dict] | None, provenance: dict | None = None) -> str:
    """What the CSV's analytical_order is, as far as the artifacts can say.

    "header": the acquisition order the raw headers record. "not_row_number": anything but the
    sequence 1..N. Otherwise it is the row number, and "synthesized" when each of two or more
    Classes is one contiguous block of it (perfectly confounded with Class), "interleaved" when the
    Classes alternate as a randomised injection sequence does, and "single_class" when every sample
    is in one Class, where no Class pattern can say whether the row number is the injection order.
    """
    if not csv_rows:
        return "not_row_number"
    agreement = _header_order_agreement(provenance, csv_rows)
    if agreement is not None and agreement[0]:
        return "header"
    raw = [str(row.get("analytical_order", "")).strip() for row in csv_rows]
    if not all(value.isdecimal() for value in raw):
        return "not_row_number"
    if [int(value) for value in raw] != list(range(1, len(raw) + 1)):
        return "not_row_number"
    classes = [str(row.get("class_id", "")).strip() for row in csv_rows]
    if len(set(classes)) == 1:
        return "single_class"
    blocks = 0
    previous = object()
    for name in classes:
        if name != previous:
            blocks += 1
            previous = name
    return "synthesized" if blocks == len(set(classes)) else "interleaved"


def _order_is_synthesized(csv_rows: list[dict] | None, provenance: dict | None = None) -> bool:
    return _order_kind(csv_rows, provenance) == "synthesized"


def check_no_metric_rests_on_a_synthetic_order(
    report: Report, output: Path, csv_rows: list[dict] | None, stage: str,
    provenance: dict | None = None,
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
    kind = _order_kind(csv_rows, provenance)
    recorded = _recorded_order_source(provenance, csv_rows)
    report_json, reason = _read_json(output / "MS_DIAL_publication_report.json")
    assessment = (report_json or {}).get("qa_assessment") or {}
    checks = assessment.get("checks") if isinstance(assessment, dict) else None
    # A report whose checks are not a list is QA-1's to refuse; ORD-2 reads no criterion from it.
    readable = isinstance(checks, list)
    run_order = [item for item in (checks if readable else []) if isinstance(item, dict)
                 and "run_order" in str(item.get("metric", ""))]
    asserted = [item for item in run_order
                if item.get("value") is not None and item.get("status") not in (None, "not_assessed")]
    said_unrecorded = [item for item in run_order if item.get("status") == "not_assessed"
                       and _qa_reason_kind(str(item.get("reason") or "")) == ("unrecorded order", None)]
    if said_unrecorded and recorded is not None and recorded not in UNRECORDED_ORDER_SOURCES:
        report.add(
            "ORD-2", stage, "No reported metric rests on a synthesized run order", FAIL,
            "The report withholds the run-order criterion because the injection order was not recorded, but "
            f"the unit manifest records the order the analysis CSV carries as taken from {recorded}.",
            order=kind, recorded_source=recorded)
        return
    if recorded in UNRECORDED_ORDER_SOURCES:
        # Whatever pattern the Classes make in it, an order read from the names is no injection order
        # (decided 2026-09-29).
        if report_json is None or not readable:
            report.add("ORD-2", stage, "No reported metric rests on a synthesized run order", NOT_EVALUABLE,
                       reason if report_json is None else "The publication report's qa_assessment carries no list "
                       "of checks, so no run-order criterion can be read from it.", order=kind, recorded_source=recorded)
        elif asserted:
            report.add(
                "ORD-2", stage, "No reported metric rests on a synthesized run order", FAIL,
                f"The unit manifest records the order the analysis CSV carries as {UNRECORDED_ORDER_SOURCES[recorded]}, "
                f"yet {len(asserted)} run-order criterion is reported with a value and a verdict. That is no "
                "injection order, so a drift computed against it describes the file names.",
                order=kind, recorded_source=recorded,
                asserted=[{"metric": item.get("metric"), "value": item.get("value"), "status": item.get("status")}
                          for item in asserted])
        else:
            report.add("ORD-2", stage, "No reported metric rests on a synthesized run order", PASS,
                       f"The unit manifest records the order as {UNRECORDED_ORDER_SOURCES[recorded]}, and no "
                       "run-order criterion is asserted.", order=kind, recorded_source=recorded)
        return
    if recorded == DECLARED_ORDER_SOURCE:
        # The order was recorded, whatever the Classes do in it: that is not a synthesized order.
        report.add("ORD-2", stage, "No reported metric rests on a synthesized run order", PASS,
                   "The unit manifest records the run order as the injection order the repository's sample table "
                   "declares, so a drift metric computed from it is meaningful.", order=kind, recorded_source=recorded)
        return
    if kind in ("header", "not_row_number", "interleaved"):
        report.add("ORD-2", stage, "No reported metric rests on a synthesized run order", PASS, {
            "header": "The run order is the acquisition order the raw headers record, so a drift "
                      "metric computed from it is meaningful.",
            "not_row_number": "The run order is not the row number, so it was not synthesized from "
                              "file order.",
            "interleaved": "The run order is the row number, but the Classes alternate in it as a "
                           "randomised injection sequence does, so it is not confounded with Class.",
        }[kind], order=kind)
        return
    if report_json is None or not readable:
        report.add("ORD-2", stage, "No reported metric rests on a synthesized run order",
                   NOT_EVALUABLE, reason if report_json is None else "The publication report's qa_assessment "
                   "carries no list of checks, so no run-order criterion can be read from it.")
        return
    if not asserted:
        report.add("ORD-2", stage, "No reported metric rests on a synthesized run order", PASS,
                   "The run order is synthesized, and no run-order criterion is asserted." if kind == "synthesized"
                   else "The run order is the row number, and no run-order criterion is asserted.", order=kind)
        return
    if kind == "single_class":
        # One Class cannot be confounded with itself, so this is not ORD-2's refusal; but nothing
        # establishes the row number as the injection order, and every sample of an abstention sits here.
        report.add(
            "ORD-2", stage, "No reported metric rests on a synthesized run order", WARN,
            "The run order is the row number, which nothing here establishes as the injection order, "
            f"and with every sample in one Class no Class pattern can say either way. {len(asserted)} "
            "run-order criterion is reported with a value and a verdict against it: it is a statement "
            "about file order until the injection order is shown.",
            order=kind,
            asserted=[
                {"metric": item.get("metric"), "value": item.get("value"), "status": item.get("status")}
                for item in asserted
            ],
        )
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


# QA-1 reads what a reader of the paper reads. The assessment in the publication report is the
# record; the Methods, the QA results and the Supplementary Table repeat it. Until Interactive 0.5.1
# a run with no QC and no Blank was written up as "assessed ... using ... QC precision and detection
# rate, blank separation and carryover, PCA topology" and "1 of 1 evaluable prespecified QA criteria
# were met": the six criteria that were not assessed recited as used, and left out of the count.
#
# What can be established is what Interactive's own sentences say, because their wording is fixed,
# and what the table and the record say, because they are structured. Those are compared, and a
# contradiction is a FAIL. Interactive's QA text is nothing but those sentences, so anything else in
# it was written by someone else: English the gate cannot parse reliably, and for a person to read
# (WARN). A PASS therefore means a text carries Interactive's statement for this assessment, it and
# the table agree with the record, and nothing else was written about QA. Before the Methods' QA
# section, which describes processing, QA-1 can only look for QA vocabulary.
QA_PROSE_FILES = ("MS_DIAL_Materials_and_Methods.txt", "MS_DIAL_QA_Results.txt")
QA_METHODS_FILE = QA_PROSE_FILES[0]
QA_TABLE_FILE = "Supplementary_Table_MS_DIAL.tsv"
QA_WORKBOOK_FILE = "Supplementary_Table_MS_DIAL.xlsx"
QA_REPORT_FILE = "MS_DIAL_publication_report.json"
QA_BUNDLE_FILE = "MS_DIAL_publication_reporting_bundle.zip"
QA_TITLE = "QA prose matches the assessment it summarizes"
QA_TEXT_LIMIT = 1024 * 1024          # one prose text; Interactive's are a few KB
QA_TABLE_LIMIT = 8 * 1024 * 1024     # the table; Interactive's grows with the files, ~72 KB for five
QA_BUNDLE_LIMIT = 12 * 1024 * 1024   # all the bundle's QA members together
QA_LABEL_LIMIT = 300
QA_STATUSES = ("pass", "fail", "not_assessed")
QA_QC_METRICS = {"median_qc_rsd_percent", "qc_features_rsd_le_30_percent", "median_qc_detection_rate",
                 "qc_pca_relative_dispersion"}
QA_BLANK_METRICS = {"sample_blank_ratio_ge_3", "median_blank_carryover_ratio"}
QA_SECTION_HEADING = re.compile(r"^[ \t]*Quality assurance[ \t]*$", re.MULTILINE)


def _qa_template(pattern: str) -> "re.Pattern":
    # Interactive's wording, its case included: "the other 2 (...)" in a person's sentence is not it.
    return re.compile(pattern)


# A template sentence starts a sentence: "Of 3 criteria for blanks, the other 2 ..." is not one.
_QA_START = r"(?:^|(?<=[.!?]\s)|(?<=\|\s)|(?<=Quality\sassurance\s))"
# The sentences Interactive 0.5.1 and later write (materials_methods._qa_criteria_sentence,
# _qa_methods_sentence and _results_text), exactly, with any whitespace between their words.
QA_T_OF = _qa_template(_QA_START + r"Of\s+(\d{1,9})\s+prespecified\s+QA\s+criteria\s*,\s+(\d{1,9})\s+could\s+be\s+"
                       r"evaluated\s*\(([^()]{0,2000})\)\s*,\s+and\s+(\d{1,9})\s+of\s+them\s+(?:was|were)\s+met\s*\.")
QA_T_NONE = _qa_template(_QA_START + r"None\s+of\s+the\s+(\d{1,9})\s+prespecified\s+QA\s+criteria\s+could\s+be\s+"
                         r"evaluated\s*\.")
QA_T_OTHER = _qa_template(_QA_START + r"The\s+other\s+(\d{1,9})\s*\(([^()]{0,2000})\)\s*could\s+not\s+be\s+assessed"
                          r"(?:\s+because\s+the\s+run\s+had\s+(?:(\d{1,9})\s+QC\s+injection\(s\)\s*,\s+where\s+at\s+least\s+"
                          r"three\s+are\s+needed(\s+and\s+no\s+Blank\s+files)?|(no\s+Blank\s+files)))?\s*\.")
# From 0.5.3 each criterion that could not be assessed is given the reason it fell to, from a fixed set
# (msdial_app.quality_assurance.not_assessed_reasons): "could not be assessed because <reason>" when
# they share one, else "could not be assessed: A and B because <reason>; C because <reason>".
QA_REASONS = (
    ("qc count", r"the\s+run\s+had\s+(\d{1,9})\s+QC\s+injection\(s\)\s*,\s+and\s+at\s+least\s+three\s+are\s+needed"),
    ("injection count", r"the\s+run\s+had\s+(\d{1,9})\s+injection\(s\)\s*,\s+and\s+at\s+least\s+three\s+are\s+needed"),
    ("no blank", r"the\s+run\s+had\s+no\s+Blank\s+files"),
    ("no features", r"the\s+alignment\s+has\s+no\s+features"),
    ("qc detections", r"no\s+feature\s+was\s+detected\s+in\s+two\s+or\s+more\s+QC\s+injections"),
    ("qc dispersion", r"the\s+QC\s+dispersion\s+could\s+not\s+be\s+computed\s+from\s+the\s+PCA"),
    ("blank detections", r"no\s+feature\s+was\s+detected\s+in\s+both\s+a\s+Blank\s+file\s+and\s+a\s+study\s+sample"),
    ("blank order", r"no\s+Blank\s+file\s+followed\s+an\s+injection\s+with\s+detected\s+features\s+in\s+its\s+batch"),
    ("flat", r"run\s+order\s+or\s+median\s+intensity\s+did\s+not\s+vary\s+across\s+injections"),
    # Interactive 0.5.4: the unit manifest records the analytical order as the file listing.
    ("unrecorded order", r"the\s+injection\s+order\s+was\s+not\s+recorded\s+for\s+every\s+file"),
    ("no value", r"the\s+QA\s+matrix\s+gives\s+no\s+value\s+for\s+it"),
    ("no matrix", r"no\s+LC-MS\s+QA\s+matrix\s+was\s+supplied"),
)
# The criteria a reason can be the reason for; one not listed can be the reason for any.
QA_REASON_METRICS = {
    "qc count": QA_QC_METRICS,
    "qc detections": {"median_qc_rsd_percent", "qc_features_rsd_le_30_percent"},
    "qc dispersion": {"qc_pca_relative_dispersion"},
    "no blank": QA_BLANK_METRICS,
    "blank detections": {"sample_blank_ratio_ge_3"},
    "blank order": {"median_blank_carryover_ratio"},
    "injection count": {"run_order_intensity_correlation"},
    "flat": {"run_order_intensity_correlation"},
    "unrecorded order": {"run_order_intensity_correlation"},
}
_QA_REASON = "(?:" + "|".join(pattern.replace(r"(\d{1,9})", r"\d{1,9}") for _, pattern in QA_REASONS) + ")"
QA_T_OTHER_REASONS = _qa_template(
    _QA_START + r"The\s+other\s+(\d{1,9})\s*\(([^()]{0,2000})\)\s*could\s+not\s+be\s+assessed(?:\s+because\s+("
    + _QA_REASON + r")|\s*:\s+((?:[^;:.()]{1,2000}?\s+because\s+" + _QA_REASON + r"\s*;\s+){0,20}[^;:.()]{1,2000}?\s+"
    r"because\s+" + _QA_REASON + r"))\s*\.")
QA_T_REVIEW = _qa_template(_QA_START + r"Criteria\s+requiring\s+review\s+were\s*:\s*([^.]{0,1000}?)\s*\.(?=\s|$)")
QA_T_FILES = _qa_template(_QA_START + r"Analytical\s+quality\s+was\s+assessed\s+from\s+(\d{1,9})\s+files?\s*\(\s*(\d{1,9})"
                          r"\s+study\s+samples?\s*,\s+(\d{1,9})\s+pooled\s+QC\s+samples?\s*,\s+and\s+(\d{1,9})\s+blanks?"
                          r"\s*\)\s*\.")
QA_T_INJECTIONS = _qa_template(_QA_START + r"Quality\s+assessment\s+included\s+(\d{1,9})\s+injections\s+and\s+(\d{1,9})"
                               r"\s+aligned\s+features\s*\.")
QA_T_NO_MATRIX = _qa_template(_QA_START + r"(?:No\s+LC-MS\s+quality-assurance\s+matrix\s+was\s+supplied\s+when\s+this\s+"
                              r"report\s+was\s+generated\s*;\s+QA\s+claims\s+should\s+be\s+added\s+after\s+assessment"
                              r"|Quality-assurance\s+results\s+were\s+not\s+generated\s+because\s+no\s+LC-MS\s+QA\s+matrix"
                              r"\s+was\s+supplied)\s*\.")
QA_T_TABLE_NOTE = _qa_template(_QA_START + r"Individual\s+criteria\s+and\s+outcomes\s+are\s+reported\s+in\s+Supplementary"
                               r"\s+Table\s+S1\s*\.")
# The value sentences _results_text prints for a value that is not None: metric, sentence, scale.
QA_T_VALUES = (
    ("median_qc_rsd_percent", _qa_template(_QA_START + r"The\s+median\s+feature\s+RSD\s+among\s+QC\s+injections\s+was\s+"
                                           r"(\S{1,40}?)%\s*\."), 1.0),
    ("median_qc_detection_rate", _qa_template(_QA_START + r"The\s+median\s+QC\s+detection\s+rate\s+was\s+(\S{1,40}?)%"
                                              r"\s*\."), 100.0),
    ("qc_pca_relative_dispersion", _qa_template(_QA_START + r"QC\s+relative\s+dispersion\s+in\s+the\s+first\s+two\s+PCA"
                                                r"\s+dimensions\s+was\s+(\S{1,40})\s+compared\s+with\s+all\s+displayed\s+"
                                                r"samples\s*\."), 1.0),
)
# The sentences Interactive wrote before 0.5.1 (git 442d1af~1), exactly.
QA_L_BATTERY = _qa_template(r"\busing\s+feature-intensity\s+distributions\s*,\s+QC\s+precision\s+and\s+detection\s+rate\s*,"
                            r"\s+blank\s+separation\s+and\s+carryover\s*,\s+PCA\s+topology\s*,\s+analytical-order\s+drift\s*,"
                            r"\s+MS/MS\s+acquisition\s*,\s+raw\s+signal-to-noise\s+ratios\s*,\s+and\s+internal-standard\s+"
                            r"mass\s+and\s+retention-time\s+errors\s+where\s+available\s*\.")
QA_L_FILES = _qa_template(_QA_START + r"Analytical\s+quality\s+was\s+assessed\s+from\s+(\d{1,9})\s+files?\s*\(\s*(\d{1,9})"
                          r"\s+study\s+samples?\s*,\s+(\d{1,9})\s+pooled\s+QC\s+samples?\s*,\s+and\s+(\d{1,9})\s+blanks?"
                          r"\s*\)\s+(?=using\s+feature-intensity)")
QA_L_COUNT = _qa_template(_QA_START + r"(?:Overall\s*,\s+)?(\d{1,9})\s+of\s+(\d{1,9})\s+(?:prespecified\s*,\s+evaluable\s+"
                          r"QA\s+criteria\s+were\s+met\s*;\s+individual\s+criteria\s+and\s+outcomes\s+are\s+reported\s+in\s+"
                          r"Supplementary\s+Table\s+S1|evaluable\s+prespecified\s+QA\s+criteria\s+were\s+met)\s*\.")
QA_L_NONE = _qa_template(_QA_START + r"Prespecified\s+QA\s+criteria\s+could\s+not\s+be\s+evaluated\s+from\s+the\s+"
                         r"available\s+sample\s+types\s*\.")
QA_TEMPLATES = (QA_T_OF, QA_T_NONE, QA_T_OTHER_REASONS, QA_T_OTHER, QA_T_REVIEW, QA_T_FILES, QA_T_INJECTIONS, QA_T_NO_MATRIX,
                QA_T_TABLE_NOTE, QA_L_BATTERY, QA_L_FILES, QA_L_COUNT, QA_L_NONE) + tuple(item[1] for item in QA_T_VALUES)

# Before the Methods' QA section: what names QA, or one of the criteria. Interactive's processing
# and annotation paragraphs use none of it; raw-file and library names, which may, are set aside first.
QA_VOCABULARY = re.compile(
    r"\bQA\b|\bQCs?\b|quality|\bprespecified\b|\bcriteri(?:a|on)\b|\bacceptance\b|\bRSDs?\b|\bCVs?\b"
    r"|coefficients?\s+of\s+variation|\bprecision\b|reproducib|repeatab|\bdrift|contaminat|carry[- ]?over"
    r"|blank\s+separation|sample\s*(?:/|-?\s*to\s*-?)\s*blank|\bPCA\b|dispersion|topology",
    re.IGNORECASE)
QA_FILE_NAMES = re.compile(
    r"\S+\.(?:mzML|mzXML|mzData|raw|RAW|wiff2?|lcd|abf|ibf|mgf|msp|lbm2?|d)\b"
    # The automatic RT-correction paragraph names its reference file, often a pooled QC ("QC-01").
    r"|\breference\s+file\s+.{1,300}?(?=,\s+which\s+defines|\s+only\s+where|;\s+per-file)")
# A whole sentence that says one thing of one criterion: the only English QA-1 reads as a claim.
QA_SAID = (
    ("met", r"(?:was|were|is|has\s+been)\s+(?:met|passed|satisfied)"),
    ("not met", r"(?:(?:was|were|is|has\s+been)\s+not\s+(?:met|passed|satisfied)|failed)"),
    ("not assessed", r"(?:could\s+not\s+be|cannot\s+be|was\s+not|were\s+not)\s+(?:assessed|evaluated)"),
    ("value", r"was\s+([-+\u2212]?\d[\d,]{0,20}(?:\.\d{1,20})?(?:[eE][-+]?\d{1,3})?\s*%?)"),
)


def _qa_space(text: str) -> str:
    # A hard wrap after a hyphen ("run-" at a line end) joins without the space the newline becomes.
    text = text.replace("\u00ad", "").replace("\u2010", "-").replace("\u2011", "-").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", re.sub(r"(?<=\w)-[ \t]*\r?\n\s*(?=\w)", "-", text)).strip()


def _qa_criteria_count(count: int) -> str:
    return f"{count} {'criterion' if count == 1 else 'criteria'}"


def _qa_short(text, limit: int = 160) -> str:
    text = _qa_space(str(text))
    return text if len(text) <= limit else text[:limit] + "..."


def _qa_int(value) -> "int | None":
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number != int(number) or abs(number) > 1e12:
        return None
    return int(number)


def _qa_float(value) -> "float | None":
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _qa_number(text: str) -> "tuple[float, float, bool] | None":
    """(number, the tolerance its printed digits allow, whether it carried a %), or None if unreadable.

    "1,234" could be a thousand or a decimal comma, so it is unreadable; "0,812" and "1.234,5" are not.
    """
    cleaned = _qa_space(str(text)).replace("\u2212", "-").replace(" ", "")
    percent = cleaned.endswith("%")
    cleaned = cleaned.rstrip("%")
    if re.fullmatch(r"[-+]?[1-9]\d{0,2},\d{3}", cleaned):
        return None
    if re.fullmatch(r"[-+]?\d{1,30},\d{1,30}", cleaned):
        cleaned = cleaned.replace(",", ".")          # a decimal comma
    elif re.fullmatch(r"[-+]?\d{1,3}(?:,\d{3}){2,10}(?:\.\d{0,30})?|[-+]?\d{1,3},\d{3}\.\d{0,30}", cleaned):
        cleaned = cleaned.replace(",", "")           # thousands
    match = re.fullmatch(r"([-+]?(?:\d{1,30}(?:\.(\d{0,30}))?|\.(\d{1,30})))(?:[eE]([-+]?\d{1,3}))?", cleaned)
    if not match:
        return None
    exponent = int(match.group(4) or 0)
    if abs(exponent) > 300:
        return None
    try:
        number = float(cleaned)
        decimals = len(match.group(2) or match.group(3) or "")
        tolerance = 0.5 * 10.0 ** (exponent - decimals) + 1e-12 * abs(number)
    except (OverflowError, ValueError):
        return None
    if not (math.isfinite(number) and math.isfinite(tolerance)):
        return None
    return number, tolerance, percent


def _qa_agrees(printed: str, recorded, *, scale: float = 1.0, unit: str = "", absolute: bool = False) -> "bool | None":
    """Whether a printed number is the recorded one as far as its digits say; None if unreadable.

    A fraction may be printed as a percent; an absolute criterion's value may be printed unsigned.
    """
    parsed, value = _qa_number(printed), _qa_float(recorded)
    if parsed is None or value is None:
        return None
    number, tolerance, percent = parsed
    if percent and scale == 1.0 and unit and unit != "%":
        number, tolerance = number / 100.0, tolerance / 100.0
    target = value * scale
    if absolute:
        number, target = abs(number), abs(target)
    return abs(number - target) <= tolerance + 1e-12


def _qa_label_pattern(label: str) -> "re.Pattern | None":
    words = _qa_space(label).split()
    if not words:
        return None
    return re.compile(r"(?<![\w/])" + r"[\s-]+".join(re.escape(word) for word in words) + r"(?!\w)", re.IGNORECASE)


def _qa_fold(text: str) -> str:
    return re.sub(r"[\s-]+", " ", text.casefold()).strip()


def _qa_items(listing: str) -> list[str]:
    """The names in a criteria list: separated by ';' as Interactive writes, or ',' and 'and'."""
    text = _qa_space(listing).replace("\u2264", "<=").replace("\u2265", ">=")
    text = re.sub(r"\s*(<=|>=)\s*", r" \1", text)
    parts = re.split(r"\s*;\s*", text) if ";" in text else re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", text)
    return [re.sub(r"^(?:the|a|an)\s+", "", part.strip(), flags=re.IGNORECASE).casefold() for part in parts if part.strip()]


def _qa_listing(listing: str, expected: list[dict], criteria: list[dict]) -> "tuple[str, str]":
    """('agrees' | 'contradicts' | 'unread', what differs), for a list against the criteria it should name."""
    names = _qa_items(listing)
    if len(names) == 1 and names[0] in ("none", "no criteria"):
        names = []
    known = {}
    for item in criteria:
        for name in item["names"]:
            known[re.sub(r"\s*(<=|>=)\s*", r" \1", name)] = item
    matched, unread = [], []
    for name in names:
        item = known.get(name)
        (matched.append(item) if item is not None else unread.append(name))
    wanted = {id(item) for item in expected}
    got = [id(item) for item in matched]
    wrong = [item["label"] for item in matched if id(item) not in wanted]
    absent = [item["label"] for item in expected if id(item) not in got]
    if wrong or (absent and not unread):
        return "contradicts", "; ".join(filter(None, [
            f"names {', '.join(wrong)}, which it should not" if wrong else "",
            f"leaves out {', '.join(absent)}" if absent else ""]))
    if unread:
        return "unread", f"names {', '.join(unread)}, which QA-1 cannot match to a criterion"
    if len(got) != len(set(got)):
        return "unread", "names a criterion twice"
    return "agrees", ""


def _qa_sentences(text: str) -> list[str]:
    parts = re.split(r"\s*\|\s*|(?<=[.!?])\s+", text)
    return [part.strip() for part in parts if re.search(r"[^\s|]", part)]


def _qa_duplicate_keys(path: Path) -> list[str]:
    """Keys that appear twice in one object of a JSON file, which a reader resolves by keeping the last."""
    repeated: list[str] = []

    def hook(pairs):
        seen = set()
        for key, _ in pairs:
            if key in seen:
                repeated.append(key)
            seen.add(key)
        return dict(pairs)

    try:
        json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=hook)
    except (OSError, ValueError, RecursionError):
        pass
    return repeated


class _QaRecord:
    """The assessment and the QA matrix summary behind it, and whether QA-1 could read them."""

    def __init__(self, report_json: dict) -> None:
        self.notes: list[str] = []
        self.unreadable: list[str] = []
        self.contradictions: list[str] = []
        assessment = report_json.get("qa_assessment")
        checks = assessment.get("checks") if isinstance(assessment, dict) else None
        self.assessment = assessment if isinstance(assessment, dict) else {}
        self.criteria: list[dict] = []
        for index, item in enumerate(checks if isinstance(checks, list) else []):
            if not isinstance(item, dict):
                self.unreadable.append(f"entry {index + 1} is not an object")
                continue
            metric = item.get("metric") if isinstance(item.get("metric"), str) else ""
            label = item.get("label") if isinstance(item.get("label"), str) else ""
            status = item.get("status") if isinstance(item.get("status"), str) else None
            metric, label = metric.strip(), _qa_space(label)
            if not label or len(label) > QA_LABEL_LIMIT:
                self.unreadable.append(f"entry {index + 1} ({metric[:40] or 'no metric'}) has no usable label")
                continue
            if ";" in label:
                self.unreadable.append(f"{label!r} has a ';', which separates the names in a list")
            if status not in QA_STATUSES:
                self.unreadable.append(f"{label!r} has status {status!r}, not one of {', '.join(QA_STATUSES)}")
                continue
            words = r"[\s-]+".join(re.escape(word) for word in label.split())
            operator = item.get("operator") if isinstance(item.get("operator"), str) else ""
            if item.get("reason") is not None and not isinstance(item.get("reason"), str):
                self.unreadable.append(f"{label!r} has a reason that is not text")
                continue
            reason = _qa_space(item["reason"]) if isinstance(item.get("reason"), str) else ""
            if reason and status != "not_assessed":
                self.contradictions.append(f"{label!r} is {status!r}, but the assessment gives a reason it could not "
                                           f"be assessed: {_qa_short(reason, 80)!r}")
            self.criteria.append({
                "metric": metric, "label": label, "status": status, "value": item.get("value"), "reason": reason,
                "operator": operator, "threshold": item.get("threshold"),
                "absolute": operator.replace(" ", "").startswith("abs"),
                "unit": item.get("unit") if isinstance(item.get("unit"), str) else "",
                "names": {name.casefold() for name in (label, metric) if name},
                "fold": _qa_fold(label),
                "pattern": _qa_label_pattern(label),
                "claims": [(kind, re.compile(r"(?:the\s+)?" + words + r"\s+" + said + r"\s*[.!]?", re.IGNORECASE))
                           for kind, said in QA_SAID],
            })
        labels = Counter(item["label"].casefold() for item in self.criteria)
        for label, count in labels.items():
            if count > 1:
                self.unreadable.append(f"{count} criteria share the label {label!r}")
        self.assessed = [item for item in self.criteria if item["status"] != "not_assessed"]
        self.missing = [item for item in self.criteria if item["status"] == "not_assessed"]
        self.failed = [item for item in self.criteria if item["status"] == "fail"]
        self.passed = sum(1 for item in self.criteria if item["status"] == "pass")
        for field in ("evaluated", "passed"):
            if field in self.assessment and _qa_int(self.assessment.get(field)) is None:
                self.notes.append(f"qa_assessment.{field} is {_qa_short(self.assessment.get(field), 40)!r}, not a count, "
                                  "and was not compared")
        evaluated, passed = _qa_int(self.assessment.get("evaluated")), _qa_int(self.assessment.get("passed"))
        if (evaluated is not None and evaluated != len(self.assessed)) or (passed is not None and passed != self.passed):
            self.contradictions.append(f"the assessment records {evaluated} evaluated and {passed} passed, but its "
                                       f"checks give {len(self.assessed)} and {self.passed}")
        status = self.assessment.get("status")
        derived = "not_assessed" if not self.assessed else ("pass" if self.passed == len(self.assessed) else "review")
        if isinstance(status, str) and status.strip() and status.strip() != derived:
            self.contradictions.append(f"the assessment's status is {status.strip()!r}, but its checks give {derived!r}")

        qa_report = report_json.get("qa_report")
        summary = qa_report.get("summary") if isinstance(qa_report, dict) else None
        self.summary = summary if isinstance(summary, dict) else {}
        self.present = bool(self.summary)
        counts = self.summary.get("category_counts")
        if counts is not None and not isinstance(counts, dict):
            self.notes.append("qa_report.summary.category_counts is not an object and was not read")
        self.categories = counts if isinstance(counts, dict) else {}
        self.files = self._count(self.summary, "sample_count", "qa_report.summary.sample_count")
        self.features = self._count(self.summary, "alignment_spot_count", "qa_report.summary.alignment_spot_count")
        self.sample = self._count(self.categories, "Sample", "category_counts.Sample")
        self.qc = self._count(self.categories, "QC", "category_counts.QC")
        self.blank = self._count(self.categories, "Blank", "category_counts.Blank")
        if self.assessed and not self.present:
            self.notes.append(f"the report carries no QA matrix summary, although {len(self.assessed)} criteria were "
                              "evaluated; the counts the texts give were not compared")
        self.has_reasons = any(item["reason"] for item in self.criteria)
        for item in self.missing:
            if item["reason"]:
                for level, message in _qa_reason_problems(item, item["reason"], self, "the assessment"):
                    (self.contradictions if level == "fail" else self.notes).append(message)
            elif self.has_reasons:
                self.notes.append(f"the assessment gives no reason {item['label']!r} could not be assessed, though it "
                                  "gives one for the others")
        # From 0.5.3 a QC-based criterion is assessed only from three or more QC injections.
        if self.qc is not None and self.qc < 3:
            for item in self.assessed:
                if item["metric"] in QA_QC_METRICS:
                    message = (f"{item['label']!r} is {item['status']!r} from {self.qc} QC injection(s); a QC-based "
                               "criterion needs at least three")
                    (self.contradictions if self.has_reasons else self.notes).append(
                        message if self.has_reasons else message + " (a record from before Interactive 0.5.3)")
        summary_reasons = self.summary.get("not_assessed_reasons")
        if summary_reasons is not None and not isinstance(summary_reasons, dict):
            self.notes.append("qa_report.summary.not_assessed_reasons is not an object and was not read")
        elif isinstance(summary_reasons, dict):
            for item in self.criteria:
                stated = summary_reasons.get(item["metric"])
                if stated is None:
                    continue
                stated = _qa_space(str(stated))
                if item["status"] != "not_assessed":
                    self.contradictions.append(f"the QA matrix summary gives a reason {item['label']!r} could not be "
                                               f"assessed, but it is {item['status']!r}")
                elif item["reason"] and stated.casefold() != item["reason"].casefold():
                    self.contradictions.append(f"the QA matrix summary gives {_qa_short(stated, 80)!r} as why "
                                               f"{item['label']!r} could not be assessed; its check gives {item['reason']!r}")
        # Interactive judges each criterion on the summary's value of the same name.
        for item in self.criteria:
            if not self.present or item["metric"] not in self.summary:
                continue
            summary_value = self.summary.get(item["metric"])
            if summary_value is None and item["status"] != "not_assessed":
                self.contradictions.append(f"{item['label']!r} is {item['status']!r}, but the QA matrix summary "
                                           "records no value for it")
            elif summary_value is not None and _qa_float(summary_value) is not None:
                recorded = _qa_float(item["value"])
                if item["status"] == "not_assessed" or recorded is None:
                    self.contradictions.append(f"{item['label']!r} is {item['status']!r} with no value, but the QA "
                                               f"matrix summary records {_qa_short(summary_value, 40)}")
                elif abs(recorded - float(summary_value)) > 1e-9 * max(1.0, abs(recorded)):
                    self.contradictions.append(f"{item['label']!r} records {recorded}, but the QA matrix summary "
                                               f"records {_qa_short(summary_value, 40)}")

    def _count(self, mapping: dict, key: str, name: str) -> "int | None":
        if key not in mapping:
            return None
        value = _qa_int(mapping.get(key))
        if value is None or value < 0:
            self.notes.append(f"{name} is {_qa_short(mapping.get(key), 40)!r}, not a count, and was not compared")
            return None
        return value


def _qa_claim(sentence: str, criteria: list[dict]) -> str:
    """A whole sentence that says one thing of one criterion against its status, or ''."""
    folded = _qa_fold(sentence)
    for item in criteria:
        if not (folded.startswith(item["fold"]) or folded.startswith("the " + item["fold"])):
            continue
        for kind, pattern in item["claims"]:
            match = pattern.fullmatch(sentence)
            if not match:
                continue
            status = item["status"]
            if kind == "met" and status != "pass":
                return f"says {item['label']!r} was met; the assessment {'failed it' if status == 'fail' else 'did not assess it'}"
            if kind == "not met" and status != "fail":
                return f"says {item['label']!r} was not met; the assessment {'passed it' if status == 'pass' else 'did not assess it'}"
            if kind == "not assessed" and status != "not_assessed":
                return f"says {item['label']!r} could not be assessed; the assessment {'passed' if status == 'pass' else 'failed'} it"
            if kind == "value":
                if status == "not_assessed":
                    return f"gives a value for {item['label']!r}, which was not assessed"
                if _qa_agrees(match.group(1), item["value"], unit=item["unit"], absolute=item["absolute"]) is False:
                    return f"gives {_qa_space(match.group(1))} for {item['label']!r}; the assessment records {item['value']}"
    return ""


def _qa_reason_kind(text: str) -> "tuple[str, int | None] | None":
    for kind, pattern in QA_REASONS:
        match = re.fullmatch(pattern, _qa_space(text).rstrip(".").strip())
        if match:
            return kind, int(match.group(1)) if match.groups() else None
    return None


def _qa_decided_kind(item: dict, record: "_QaRecord") -> "str | None":
    """The reason the counts alone decide for a criterion, as Interactive decides it, or None."""
    if record.features == 0:
        return "no features"
    if item["metric"] in QA_QC_METRICS and record.qc is not None and record.qc < 3:
        return "qc count"
    if item["metric"] in QA_BLANK_METRICS and record.blank == 0:
        return "no blank"
    if item["metric"] == "run_order_intensity_correlation" and record.files is not None and record.files < 3:
        return "injection count"
    return None


def _qa_reason_problems(item: dict, reason: str, record: "_QaRecord", who: str) -> "list[tuple[str, str]]":
    """(level, message) for a reason given for one criterion: one QA-1 knows, that can be the reason for
    that criterion, and that the counts bear out."""
    label = item["label"]
    kind_and_number = _qa_reason_kind(reason)
    if kind_and_number is None:
        return [("warn", f"{who} gives {_qa_short(reason, 80)!r} as why {label!r} could not be assessed, a reason "
                         "QA-1 does not know")]
    kind, number = kind_and_number
    problems: list[tuple[str, str]] = []
    allowed = QA_REASON_METRICS.get(kind)
    if allowed is not None and item["metric"] not in allowed:
        problems.append(("fail", f"{who} gives {_qa_short(reason, 80)!r} as why {label!r} could not be assessed, which "
                                 "is no reason for that criterion"))

    def against(recorded: "int | None", what: str, wrong: bool, message: str) -> None:
        if recorded is None:
            problems.append(("warn", f"{who} gives {what} as a reason, which the report has no figure to compare with"))
        elif wrong:
            problems.append(("fail", f"{who} {message}"))

    if kind == "qc count":
        against(record.qc, f"{number} QC injection(s)", record.qc is not None and (record.qc != number or number >= 3),
                f"gives {number} QC injection(s) as the reason; the QA matrix records {record.qc}"
                + (", and at least three are enough" if number is not None and number >= 3 else ""))
    elif kind == "injection count":
        against(record.files, f"{number} injection(s)", record.files is not None and (record.files != number or number >= 3),
                f"gives {number} injection(s) as the reason; the QA matrix records {record.files}")
    elif kind == "no blank":
        against(record.blank, "'no Blank files'", bool(record.blank),
                f"gives 'no Blank files' as the reason; the QA matrix records {record.blank}")
    elif kind == "no features":
        against(record.features, "'no features'", bool(record.features),
                f"gives 'the alignment has no features' as the reason; the QA matrix records {record.features}")
    elif kind == "no matrix" and record.present:
        problems.append(("fail", f"{who} gives 'no LC-MS QA matrix was supplied' as a reason; the report carries its "
                                 "summary"))
    decided = _qa_decided_kind(item, record)
    if decided is not None and decided != kind and not any(level == "fail" for level, _ in problems):
        problems.append(("warn", f"{who} gives {_qa_short(reason, 80)!r} as why {label!r} could not be assessed, but the "
                                 f"counts decide another reason ({decided})"))
    return problems


def _qa_judge_reasons(source: str, single: "str | None", groups: "str | None",
                      record: "_QaRecord") -> "tuple[list[tuple[str, str]], list[str]]":
    """What the reasons in a 0.5.3 sentence say, against the record and the counts.

    Each criterion that could not be assessed must be given one reason: the one the record gives it
    where the record carries reasons (the record's own reasons are checked where it is read), and
    otherwise one that can be the reason for that criterion and that the counts bear out. Returns
    (level, message) problems and the reasons given, for the PASS detail.
    """
    problems: list[tuple[str, str]] = []
    missing = record.missing
    if single is not None:
        parts = [(missing, single)]
    else:
        parts = []
        known = {re.sub(r"\s*(<=|>=)\s*", r" \1", name): item for item in record.criteria for name in item["names"]}
        for match in re.finditer(r"\s*(.+?)\s+because\s+(" + _QA_REASON + r")\s*(?:;|$)", groups or ""):
            members, unread = [], []
            for name in _qa_items(match.group(1)):
                (members.append(known[name]) if name in known else unread.append(name))
            if unread:
                problems.append(("warn", f"{source}: gives a reason for {', '.join(unread)}, which QA-1 cannot match to "
                                         "a criterion"))
            parts.append((members, match.group(2)))
    given: list[str] = []
    seen: dict[int, int] = {}
    for members, reason in parts:
        given.append("because " + _qa_space(reason))
        for item in members:
            seen[id(item)] = seen.get(id(item), 0) + 1
            if item["status"] != "not_assessed":
                problems.append(("fail", f"{source}: gives a reason {item['label']!r} could not be assessed; the "
                                         f"assessment {'passed' if item['status'] == 'pass' else 'failed'} it"))
            elif item["reason"]:
                if _qa_space(reason).casefold() != item["reason"].casefold():
                    problems.append(("fail", f"{source}: gives {_qa_short(reason, 80)!r} as why {item['label']!r} could "
                                             f"not be assessed; the assessment records {item['reason']!r}"))
            else:
                problems.extend(_qa_reason_problems(item, reason, record, f"{source}:"))
    if single is None:
        for item in missing:
            if seen.get(id(item), 0) == 0:
                problems.append(("warn", f"{source}: does not say why {item['label']!r} could not be assessed"))
            elif seen[id(item)] > 1:
                problems.append(("warn", f"{source}: gives {item['label']!r} more than one reason"))
    unique = list(dict.fromkeys(problems))
    return unique, sorted(set(given), key=given.index)


def _qa_judge_text(source: str, text: str, record: _QaRecord, *, methods: bool) -> dict:
    """What one text says of the assessment."""
    fails: list[str] = []
    warns: list[str] = []
    # What a person must read (READ-1): the sentences that are not Interactive's, and those of its
    # sentences with words in them QA-1 cannot attribute to Interactive.
    to_read: list[dict] = []

    def quote(sentence: str) -> None:
        to_read.append({"source": source, "sentence": _qa_space(sentence)})
    criteria, assessed, missing, failed = record.criteria, record.assessed, record.missing, record.failed
    total, passed = len(criteria), record.passed
    heading = QA_SECTION_HEADING.search(text) if methods else None
    flat = _qa_space(text[heading.end():] if heading else text)
    before = _qa_space(text[:heading.start()]) if heading else ""
    if methods and not heading:
        first = min((match.start() for pattern in QA_TEMPLATES for match in [pattern.search(flat)] if match),
                    default=None)
        before, flat = (flat[:first], flat[first:]) if first is not None else (flat, "")
        before = re.sub(r"\bQuality\s+assurance\s*$", "", before).strip()
        if flat:
            warns.append(f"{source}: has no 'Quality assurance' heading; its QA text was taken to start at "
                         f"{_qa_short(flat, 60)!r}")

    def uncompared(what: str) -> None:
        warns.append(f"{source}: gives {what}, which the report has no figure to compare with")

    spans: list[tuple[int, int]] = []
    forms: list[str] = []
    contradicted = False
    reasons: list[str] = []
    for match in QA_T_OF.finditer(flat):
        spans.append(match.span())
        stated = (int(match.group(1)), int(match.group(2)), int(match.group(4)))
        if stated != (total, len(assessed), passed):
            contradicted = True
            fails.append(f"{source}: says {stated[1]} of {stated[0]} prespecified criteria could be evaluated and "
                         f"{stated[2]} met; the assessment has {len(assessed)} of {total} and {passed} met")
        verdict, detail = _qa_listing(match.group(3), assessed, criteria)
        if verdict == "contradicts":
            contradicted = True
            fails.append(f"{source}: lists {_qa_short(match.group(3))!r} as evaluated, which {detail}")
        elif verdict == "unread":
            contradicted = True
            warns.append(f"{source}: its list of evaluated criteria {detail}")
            quote(match.group(0))
        forms.append("statement")
    for match in QA_T_NONE.finditer(flat):
        spans.append(match.span())
        if int(match.group(1)) != total or assessed:
            contradicted = True
            fails.append(f"{source}: says none of {match.group(1)} prespecified criteria could be evaluated; the "
                         f"assessment evaluated {len(assessed)} of {total}")
        forms.append("statement")
    # "could not be assessed because the run had no Blank files" is both 0.5.2's reason for the whole
    # run and 0.5.3's for each criterion. A record from before 0.5.3 carries no reasons, and its text is
    # read as 0.5.2 wrote it.
    record_reasons = any(item["reason"] for item in criteria)
    reasoned = [match for match in QA_T_OTHER_REASONS.finditer(flat)
                if record_reasons or not (match.group(3) and (_qa_reason_kind(match.group(3)) or ("",))[0] == "no blank")]
    for match in reasoned:
        spans.append(match.span())
        if int(match.group(1)) != len(missing):
            contradicted = True
            fails.append(f"{source}: says the other {match.group(1)} could not be assessed; the assessment could "
                         f"not assess {len(missing)}")
        verdict, detail = _qa_listing(match.group(2), missing, criteria)
        if verdict == "contradicts":
            contradicted = True
            fails.append(f"{source}: lists {_qa_short(match.group(2))!r} as not assessed, which {detail}")
        elif verdict == "unread":
            contradicted = True
            warns.append(f"{source}: its list of criteria not assessed {detail}")
        problems, given = _qa_judge_reasons(source, match.group(3), match.group(4), record)
        for level, message in problems:
            (fails if level == "fail" else warns).append(message)
            if level == "fail" or message.endswith("QA-1 cannot match to a criterion") or " does not say why " in message:
                contradicted = True
        if any(level == "warn" and "no figure to compare with" not in message for level, message in problems):
            quote(match.group(0))
        reasons.extend(given)
    taken = {match.span() for match in reasoned}
    others = [match for match in QA_T_OTHER.finditer(flat) if match.span() not in taken]
    others_all = reasoned + others
    for match in others:
        spans.append(match.span())
        if record_reasons:
            # 0.5.2's one reason for all, where the record gives each criterion its own.
            contradicted = True
            stated = {kind for kind, present in (("qc count", match.group(3)), ("no blank", match.group(4) or match.group(5)))
                      if present}
            warns.append(f"{source}: gives Interactive 0.5.2's reason for all where the assessment gives each criterion "
                         "its own")
            for item in missing:
                recorded = _qa_reason_kind(item["reason"]) if item["reason"] else None
                if recorded is not None and recorded[0] not in stated:
                    fails.append(f"{source}: gives {', '.join(sorted(stated)) or 'no reason'} as why the criteria could "
                                 f"not be assessed; the assessment records {item['reason']!r} for {item['label']!r}")
        if int(match.group(1)) != len(missing):
            contradicted = True
            fails.append(f"{source}: says the other {match.group(1)} could not be assessed; the assessment could "
                         f"not assess {len(missing)}")
        verdict, detail = _qa_listing(match.group(2), missing, criteria)
        if verdict == "contradicts":
            contradicted = True
            fails.append(f"{source}: lists {_qa_short(match.group(2))!r} as not assessed, which {detail}")
        elif verdict == "unread":
            contradicted = True
            warns.append(f"{source}: its list of criteria not assessed {detail}")
        stated_qc, blank_after_qc, blank_alone = match.group(3), match.group(4), match.group(5)
        says_no_blank = bool(blank_after_qc or blank_alone)
        if stated_qc is not None:
            if record.qc is None:
                uncompared(f"{stated_qc} QC injection(s) as the reason")
            elif int(stated_qc) != record.qc:
                fails.append(f"{source}: gives {stated_qc} QC injection(s) as the reason; the QA matrix records {record.qc}")
            elif record.qc >= 3:
                fails.append(f"{source}: gives {stated_qc} QC injection(s), 'where at least three are needed', as the reason")
        if says_no_blank:
            if record.blank is None:
                uncompared("'no Blank files' as the reason")
            elif record.blank:
                fails.append(f"{source}: gives 'no Blank files' as the reason; the QA matrix records {record.blank}")
        if stated_qc is None and not says_no_blank:
            warns.append(f"{source}: does not say why {_qa_criteria_count(len(missing))} could not be assessed")
        else:
            reasons.append(_qa_space(match.group(0)[match.group(0).find("because"):].rstrip(".")))
        if stated_qc is not None and not any(item["metric"] in QA_QC_METRICS for item in missing):
            warns.append(f"{source}: gives the QC count as the reason, but no QC-based criterion is among those not assessed")
        if says_no_blank and not any(item["metric"] in QA_BLANK_METRICS for item in missing):
            warns.append(f"{source}: gives the missing blanks as the reason, but no blank-based criterion is among "
                         "those not assessed")
        if record.qc is not None and record.qc < 3 and stated_qc is None and any(
                item["metric"] in QA_QC_METRICS for item in missing):
            warns.append(f"{source}: does not give the QC count as a reason, although the run had {record.qc} QC")
        if record.blank == 0 and not says_no_blank and any(item["metric"] in QA_BLANK_METRICS for item in missing):
            warns.append(f"{source}: does not give the missing blanks as a reason, although the run had no Blank files")
    for match in QA_T_REVIEW.finditer(flat):
        spans.append(match.span())
        verdict, detail = _qa_listing(match.group(1), failed, criteria)
        if verdict == "contradicts":
            fails.append(f"{source}: lists {_qa_short(match.group(1))!r} as requiring review, which {detail}")
        elif verdict == "unread":
            warns.append(f"{source}: its list of criteria requiring review {detail}")
            quote(match.group(0))
    for pattern in (QA_T_FILES, QA_L_FILES):
        for match in pattern.finditer(flat):
            spans.append(match.span())
            stated = [int(group) for group in match.groups()]
            recorded = [record.files, record.sample, record.qc, record.blank]
            if any(want is not None and got != want for got, want in zip(stated, recorded)):
                fails.append(f"{source}: says {stated[0]} files ({stated[1]} study samples, {stated[2]} QC, {stated[3]} "
                             f"blanks); the QA matrix records {recorded[0]} ({recorded[1]}, {recorded[2]}, {recorded[3]})")
            elif any(want is None for want in recorded):
                uncompared(f"{stated[0]} files ({stated[1]} study samples, {stated[2]} QC, {stated[3]} blanks)")
    for match in QA_T_INJECTIONS.finditer(flat):
        spans.append(match.span())
        stated = [int(group) for group in match.groups()]
        recorded = [record.files, record.features]
        if any(want is not None and got != want for got, want in zip(stated, recorded)):
            fails.append(f"{source}: says {stated[0]} injections and {stated[1]} aligned features; the QA matrix "
                         f"records {recorded[0]} and {recorded[1]}")
        elif any(want is None for want in recorded):
            uncompared(f"{stated[0]} injections and {stated[1]} aligned features")
    for match in QA_T_NO_MATRIX.finditer(flat):
        spans.append(match.span())
        if record.present:
            fails.append(f"{source}: says no QA matrix was supplied; the report carries its summary")
        elif assessed:
            fails.append(f"{source}: says no QA matrix was supplied; the assessment evaluated {len(assessed)} criteria")
        forms.append("no matrix")
    for match in QA_T_TABLE_NOTE.finditer(flat):
        spans.append(match.span())
    by_metric = {item["metric"]: item for item in criteria}
    for metric, pattern, scale in QA_T_VALUES:
        for match in pattern.finditer(flat):
            spans.append(match.span())
            item = by_metric.get(metric)
            if item is None:
                warns.append(f"{source}: gives a value for {metric}, which the assessment does not list")
                quote(match.group(0))
                continue
            agrees = _qa_agrees(match.group(1), item["value"], scale=scale)
            if item["status"] == "not_assessed":
                if _qa_number(match.group(1)) is None:
                    warns.append(f"{source}: prints {_qa_short(match.group(1), 40)!r} as the value of {item['label']!r}, "
                                 "which was not assessed")
                    quote(match.group(0))
                else:
                    fails.append(f"{source}: gives {match.group(1)} as the value of {item['label']!r}, which was not assessed")
            elif agrees is False:
                fails.append(f"{source}: gives {match.group(1)} as the value of {item['label']!r}; the assessment "
                             f"records {item['value']}")
            elif agrees is None:
                warns.append(f"{source}: gives {_qa_short(match.group(1), 40)!r} as the value of {item['label']!r}, "
                             "which QA-1 cannot read as a number")
                quote(match.group(0))

    # The sentences Interactive wrote before 0.5.1.
    if (QA_L_BATTERY.search(flat) or QA_L_BATTERY.search(before)) and missing:
        fails.append(f"{source}: says quality was assessed using the whole battery, but {len(missing)} of the "
                     f"criteria it names {'was' if len(missing) == 1 else 'were'} not assessed "
                     f"({'; '.join(item['label'] for item in missing)})")
    for match in QA_L_BATTERY.finditer(flat):
        spans.append(match.span())
    legacy_counts = list(QA_L_COUNT.finditer(flat))
    for match in legacy_counts:
        spans.append(match.span())
        if (int(match.group(1)), int(match.group(2))) != (passed, len(assessed)):
            contradicted = True
            fails.append(f"{source}: says {match.group(1)} of {match.group(2)} evaluable criteria were met; the "
                         f"assessment has {passed} of {len(assessed)}")
        forms.append("evaluable count")
    for match in QA_L_NONE.finditer(flat):
        spans.append(match.span())
        if assessed:
            contradicted = True
            fails.append(f"{source}: says no prespecified criterion could be evaluated; the assessment evaluated "
                         f"{len(assessed)}")
        forms.append("none from the sample types")

    # What is left of the QA text once Interactive's sentences are taken out.
    kept, position = [], 0
    for low, high in sorted(spans):
        if low > position:
            kept.append(flat[position:low])
        position = max(position, high)
    kept.append(flat[position:])
    residue = _qa_sentences(" | ".join(kept))
    for sentence in residue:
        claim = _qa_claim(sentence, criteria)
        if claim:
            fails.append(f"{source}: {claim}: {_qa_short(sentence)!r}")
    to_read.extend({"source": source, "sentence": sentence} for sentence in residue)
    if residue:
        warns.append(f"{source}: {len(residue)} sentence(s) in its QA text are not Interactive's; read them: "
                     + " | ".join(repr(_qa_short(sentence, 120)) for sentence in residue[:3]))
    if legacy_counts and missing and not others_all:
        # The older count leaves out the criteria it could not assess. Alone it says so by omission;
        # beside a person's words, those words may say it.
        message = (f"{source}: counts only the {len(assessed)} evaluable of {_qa_criteria_count(total)} and does not "
                   f"say that {len(missing)} could not be assessed")
        if residue:
            warns.append(message + ", unless the other sentences do")
        else:
            contradicted = True
            fails.append(message)
    # Before the QA section: anything about QA in it.
    if before:
        scanned = QA_FILE_NAMES.sub(" ", before)
        for sentence in _qa_sentences(scanned):
            claim = _qa_claim(sentence, criteria)
            if claim:
                fails.append(f"{source}: {claim}: {_qa_short(sentence)!r}")
            named = any(item["pattern"] and item["pattern"].search(sentence) for item in criteria)
            if named or QA_VOCABULARY.search(sentence):
                to_read.append({"source": source, "sentence": sentence})
                warns.append(f"{source}: speaks of QA before its QA section; read it: {_qa_short(sentence)!r}")

    covers = not contradicted and (
        ("statement" in forms and (not missing or bool(others_all)))
        or ("no matrix" in forms and not assessed and not record.present)
        or ("evaluable count" in forms and not missing)
        or ("none from the sample types" in forms and not assessed))
    if "statement" in forms and missing and not others_all:
        warns.append(f"{source}: gives Interactive's count but not its sentence naming the {len(missing)} criteria "
                     "that could not be assessed")
    return {"source": source, "covers": covers, "forms": forms, "reasons": reasons, "fails": fails, "to_read": to_read,
            "warns": warns, "speaks": bool(forms or residue), "empty": not (flat or before),
            "methods": methods}


QA_STATUS_WORDS = {"pass": "pass", "passed": "pass", "met": "pass", "fail": "fail", "failed": "fail",
                   "not_met": "fail", "not_assessed": "not_assessed", "not_assessable": "not_assessed",
                   "not_evaluated": "not_assessed", "na": "not_assessed", "n/a": "not_assessed"}
QA_NO_VALUE = {"", "not recorded", "na", "n/a", "none", "null", "-", "not assessed", "not available"}


def _qa_table(source: str, table: str, record: _QaRecord) -> dict:
    """The Supplementary Table's QA rows against the record.

    A row QA-1 cannot read or place is a WARN, and its cells are a sentence to read (READ-1): what
    the gate did not read, a person must.
    """
    fails: list[str] = []
    warns: list[str] = []
    to_read: list[dict] = []
    criteria = record.criteria

    def unread(message: str, *cells) -> None:
        warns.append(message)
        to_read.append({"source": source, "sentence": " | ".join(_qa_space(str(cell)) for cell in cells)})

    try:
        parsed = list(csv.DictReader(io.StringIO(table, newline=""), delimiter="\t"))
    except csv.Error as exc:
        return {"source": source, "rows": 0, "fails": [], "warns": [f"{source}: could not be parsed ({exc})"],
                "to_read": [], "unread": [f"{source} could not be parsed ({exc})"]}
    names = {name for item in criteria for name in item["names"]}
    rows, near, elsewhere = [], [], []
    for row in parsed:
        section = _qa_space(str(row.get("Section") or "")).casefold()
        if section == "quality assurance":
            rows.append(row)
            continue
        cells = (row.get("Section"), row.get("Record"), row.get("Parameter"), row.get("Value"))
        if re.search(r"quality|\bqa\b", section):
            near.append((section, cells))
        elif _qa_space(str(row.get("Record") or "")).casefold() in names:
            elsewhere.append((section, cells))
    for section, cells in near:
        unread(f"{source}: has rows under {_qa_short(section, 60)!r}, which QA-1 does not read as the QA section", *cells)
    for section, cells in elsewhere:
        unread(f"{source}: has rows for a QA criterion under {_qa_short(section, 60)!r}", *cells)
    if not rows:
        return {"source": source, "rows": 0, "fails": fails, "warns": warns, "to_read": to_read, "unread": []}
    by_record: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        by_record.setdefault(_qa_space(str(row.get("Record") or "")).casefold(), []).append(
            (_qa_space(str(row.get("Parameter") or "")).casefold(), str(row.get("Value") or "").strip()))

    def status_of(text: str) -> "str | None":
        return QA_STATUS_WORDS.get(re.sub(r"[\s-]+", "_", text.strip().casefold()))

    known = set()
    for item in criteria:
        records = sorted(name for name in item["names"] if name in by_record)
        if not records:
            warns.append(f"{source}: has no rows for {item['label']!r}")
            continue
        known.update(records)
        entries = [entry for name in records for entry in by_record[name]]
        if not any(parameter == "assessment" for parameter, _ in entries):
            warns.append(f"{source}: has no Assessment row for {item['label']!r}")
        for parameter, value in entries:
            if parameter == "assessment":
                stated = status_of(value)
                if stated is None:
                    unread(f"{source}: assesses {item['label']!r} as {_qa_short(value, 40)!r}, which QA-1 cannot read",
                           item["label"], parameter, value)
                elif stated != item["status"]:
                    fails.append(f"{source}: assesses {item['label']!r} as {_qa_short(value, 40)!r}; the assessment says "
                                 f"{item['status']!r}")
            elif parameter == "observed":
                if item["status"] == "not_assessed":
                    if _qa_number(value) is not None:
                        fails.append(f"{source}: gives {_qa_short(value, 40)} as observed for {item['label']!r}, which "
                                     "was not assessed")
                    elif value.strip().casefold() not in QA_NO_VALUE:
                        unread(f"{source}: gives {_qa_short(value, 40)!r} as observed for {item['label']!r}, which was "
                               "not assessed", item["label"], parameter, value)
                    continue
                agrees = _qa_agrees(value, item["value"], unit=item["unit"], absolute=item["absolute"])
                if agrees is False:
                    fails.append(f"{source}: gives {_qa_short(value, 40)} as observed for {item['label']!r}; the "
                                 f"assessment records {item['value']}")
                elif agrees is None and _qa_float(item["value"]) is not None:
                    unread(f"{source}: gives {_qa_short(value, 40)!r} as observed for {item['label']!r}, which QA-1 "
                           "cannot read as a number", item["label"], parameter, value)
            elif parameter == "not assessed because":
                stated = _qa_space(value).rstrip(".").strip()
                if item["status"] != "not_assessed":
                    fails.append(f"{source}: gives {_qa_short(value, 60)!r} as why {item['label']!r} could not be "
                                 f"assessed; the assessment {'passed' if item['status'] == 'pass' else 'failed'} it")
                elif not item["reason"]:
                    unread(f"{source}: gives {_qa_short(value, 60)!r} as why {item['label']!r} could not be assessed, a "
                           "reason the assessment does not record", item["label"], parameter, value)
                    for level, message in _qa_reason_problems(item, stated, record, f"{source}:"):
                        (fails if level == "fail" else warns).append(message)
                elif stated.casefold() != item["reason"].casefold():
                    if _qa_reason_kind(stated) is None:
                        unread(f"{source}: gives {_qa_short(value, 60)!r} as why {item['label']!r} could not be "
                               "assessed, which QA-1 cannot read as one of Interactive's reasons",
                               item["label"], parameter, value)
                    else:
                        fails.append(f"{source}: gives {_qa_short(value, 60)!r} as why {item['label']!r} could not be "
                                     f"assessed; the assessment records {item['reason']!r}")
            elif parameter == "criterion":
                if not item["operator"] or _qa_float(item["threshold"]) is None:
                    continue
                match = re.fullmatch(r"(abs\s*<=|<=|>=|<|>)\s*(.+)",
                                     _qa_space(value).replace("\u2264", "<=").replace("\u2265", ">="))
                threshold = _qa_agrees(match.group(2), item["threshold"], unit=item["unit"]) if match else None
                if threshold is False:
                    fails.append(f"{source}: gives {_qa_short(value, 40)!r} as the criterion for {item['label']!r}; "
                                 f"the assessment applied {item['operator']} {item['threshold']}")
                elif threshold is None:
                    unread(f"{source}: gives {_qa_short(value, 40)!r} as the criterion for {item['label']!r}, which "
                           "QA-1 cannot read", item["label"], parameter, value)
                elif re.sub(r"\s+", "", match.group(1)) != re.sub(r"\s+", "", item["operator"]):
                    unread(f"{source}: gives {_qa_short(value, 40)!r} as the criterion for {item['label']!r}; the "
                           f"assessment applied {item['operator']} {item['threshold']}", item["label"], parameter, value)
            else:
                unread(f"{source}: gives {_qa_short(parameter, 40)!r} for {item['label']!r}, a row QA-1 does not read",
                       item["label"], parameter, value)
    for record_name, entries in by_record.items():
        if record_name in known or record_name == "observed metric":
            continue
        for parameter, value in entries:
            if parameter in ("assessment", "observed", "criterion", "not assessed because"):
                unread(f"{source}: gives an assessment, observed value or criterion for {_qa_short(record_name, 60)!r}, "
                       "which the assessment does not list", record_name, parameter, value)
    # "Observed metric" rows repeat the QA matrix summary the criteria were judged on.
    summary = {str(name).casefold(): value for name, value in record.summary.items()}
    observed_rows = by_record.get("observed metric", [])
    if observed_rows and not record.present:
        warns.append(f"{source}: gives {len(observed_rows)} observed metric(s), but the report carries no QA matrix summary")
    for parameter, value in observed_rows if record.present else []:
        if parameter not in summary:
            unread(f"{source}: gives {_qa_short(parameter, 40)!r} as an observed metric, which the QA matrix summary "
                   "does not carry", "Observed metric", parameter, value)
            continue
        recorded = summary[parameter]
        if isinstance(recorded, (dict, list)):
            try:
                differs = json.loads(value) != recorded
            except (ValueError, RecursionError):
                unread(f"{source}: gives {_qa_short(value, 40)!r} as {parameter}, which QA-1 cannot read",
                       "Observed metric", parameter, value)
                continue
            if differs:
                fails.append(f"{source}: gives {_qa_short(value, 80)} as {parameter}; the QA matrix records another value")
            continue
        if recorded is None or _qa_float(recorded) is None:
            if _qa_number(value) is not None:
                fails.append(f"{source}: gives {_qa_short(value, 40)} as {parameter}; the QA matrix records none")
            elif value.strip().casefold() not in QA_NO_VALUE and recorded is None:
                unread(f"{source}: gives {_qa_short(value, 40)!r} as {parameter}, which the QA matrix records as none",
                       "Observed metric", parameter, value)
            continue
        agrees = _qa_agrees(value, recorded)
        if agrees is False:
            fails.append(f"{source}: gives {_qa_short(value, 40)} as {parameter}; the QA matrix records {recorded}")
        elif agrees is None:
            unread(f"{source}: gives {_qa_short(value, 40)!r} as {parameter}, which QA-1 cannot read as a number",
                   "Observed metric", parameter, value)
    return {"source": source, "rows": len(rows), "fails": fails, "warns": warns, "to_read": to_read, "unread": []}


def _qa_decode(data: bytes) -> "tuple[str | None, str]":
    for bom, encoding in ((b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
                          (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            break
    else:
        encoding = "utf-8-sig"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        return None, "is not UTF-8 text"
    if "\x00" in text:
        return None, "holds NUL characters, so it is not UTF-8 text"
    return text.replace("\r\n", "\n").replace("\r", "\n"), ""


def _qa_read(path: Path, limit: int) -> "tuple[str | None, str]":
    try:
        if not path.is_file():
            return None, "is not a file"
        if path.stat().st_size > limit:
            return None, f"is larger than {limit // (1024 * 1024)} MB"
        return _qa_decode(path.read_bytes())
    except OSError as exc:
        return None, f"could not be read ({exc})"


def _qa_bundle(path: Path, wanted: tuple[str, ...]) -> "tuple[list[tuple[str, str, str]], list[str]]":
    """(name, member, text) for each wanted member of the bundle, and what could not be read."""
    members: list[tuple[str, str, str]] = []
    notes: list[str] = []
    names = {name.casefold(): name for name in wanted}
    stems = {name.rsplit(".", 1)[0].casefold(): name for name in wanted}
    budget = QA_BUNDLE_LIMIT
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                base = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
                name = names.get(base.casefold())
                if info.is_dir():
                    continue
                if name is None:
                    if base.rsplit(".", 1)[0].casefold() in stems and not base.casefold().endswith(".xlsx"):
                        notes.append(f"{path.name}:{info.filename} looks like a QA text under another name and was not read")
                    continue
                limit = QA_TABLE_LIMIT if name in (QA_TABLE_FILE, QA_REPORT_FILE) else QA_TEXT_LIMIT
                if info.file_size > limit or info.file_size > budget:
                    notes.append(f"{path.name}:{info.filename} is too large to read ({info.file_size} bytes)")
                    continue
                try:
                    data = archive.read(info)
                except Exception as exc:  # an encrypted member, an unknown compression, a corrupt stream
                    notes.append(f"{path.name}:{info.filename} could not be read ({type(exc).__name__})")
                    continue
                budget -= len(data)
                text, problem = _qa_decode(data)
                if text is None:
                    notes.append(f"{path.name}:{info.filename} {problem} and was not read")
                    continue
                members.append((name, info.filename, text))
    except Exception as exc:  # not a zip, or one the archive reader cannot open
        notes.append(f"{path.name} could not be opened ({type(exc).__name__})")
    return members, notes


def _qa_same_record(first: dict, second: dict) -> bool:
    try:
        def view(record: dict) -> str:
            report = record.get("qa_report")
            return json.dumps([record.get("qa_assessment"), report.get("summary") if isinstance(report, dict) else None],
                              sort_keys=True)
        return view(first) == view(second)
    except (ValueError, TypeError, RecursionError):
        return False


def _qa_table_text(text: str) -> str:
    return text.lstrip("\ufeff").replace("\r\n", "\n").rstrip("\n")


def check_qa_prose_matches_assessment(report: Report, output: Path, stage: str) -> None:
    """QA-1. The published QA text and table say what the assessment says, and no more.

    Read: the Materials and Methods, the QA results, the Supplementary Table, and their copies in
    the reporting bundle. FAIL for what is established: one of Interactive's own sentences (0.5.1 and
    later, or the wording before 0.5.1) whose counts, lists, values or reason contradict the
    assessment or its QA matrix summary; that older wording's recital of criteria that were not
    assessed, and its count of the evaluable ones alone; a whole sentence saying one criterion was
    met, not met, not assessed or had a value, against its status; a table row that contradicts the
    record; an assessment that contradicts its own checks or its QA matrix summary. WARN for what a
    person must read: any other sentence in a QA text, QA vocabulary before the Methods' QA section, a
    text about QA without Interactive's statement, a missing reason, a count with nothing to compare it
    with, a table row QA-1 cannot place, a bundle copy or record that differs, a file QA-1 cannot read.
    NOT_EVALUABLE when the assessment or every text is unreadable.
    """
    if stage != "before-publish":
        return
    report_json, reason = _read_json(output / QA_REPORT_FILE)
    if report_json is None:
        report.add("QA-1", stage, QA_TITLE, NOT_EVALUABLE, reason)
        return
    assessment = report_json.get("qa_assessment")
    if not isinstance(assessment, dict) or not isinstance(assessment.get("checks"), list):
        report.add("QA-1", stage, QA_TITLE, NOT_EVALUABLE,
                   "The publication report carries no qa_assessment.checks list.")
        return
    record = _QaRecord(report_json)
    repeated = _qa_duplicate_keys(output / QA_REPORT_FILE)
    if repeated:
        record.unreadable.append(f"the report repeats the key(s) {', '.join(sorted(set(repeated))[:5])}, so which "
                                 "value it means cannot be told")
    if record.unreadable or not record.criteria:
        report.add("QA-1", stage, QA_TITLE, NOT_EVALUABLE,
                   "The assessment cannot be read, so nothing can be compared with it: "
                   + ("; ".join(_qa_short(item, 120) for item in record.unreadable[:5]) or "it lists no criteria."),
                   unreadable=[_qa_short(item, 200) for item in record.unreadable[:20]])
        return
    assessed, missing, passed = record.assessed, record.missing, record.passed
    notes = list(record.notes)

    # Every copy: the files, and the bundle's members that differ from them, each distinct text once.
    sources: list[tuple[str, str, str]] = []
    local: dict[str, str] = {}
    unread_texts: list[str] = []
    for name in QA_PROSE_FILES + (QA_TABLE_FILE,):
        if not (output / name).exists():
            continue
        text, problem = _qa_read(output / name, QA_TABLE_LIMIT if name == QA_TABLE_FILE else QA_TEXT_LIMIT)
        if text is None:
            notes.append(f"{name} {problem} and was not read")
            unread_texts.append(f"{name} {problem}")
            continue
        local[name] = text

        sources.append((name, name, text))

    def key_of(name: str, text: str) -> tuple[str, str]:
        return (name, _qa_table_text(text) if name == QA_TABLE_FILE else _qa_space(text))

    if (output / QA_BUNDLE_FILE).exists():
        members, problems = _qa_bundle(output / QA_BUNDLE_FILE, QA_PROSE_FILES + (QA_TABLE_FILE, QA_REPORT_FILE))
        notes += problems
        unread_texts += problems
        seen = {key_of(name, text) for name, text in local.items()}
        for name, member, text in members:
            where = f"{QA_BUNDLE_FILE}:{member}"
            if name == QA_REPORT_FILE:
                try:
                    shared = json.loads(text)
                except (ValueError, RecursionError):
                    notes.append(f"{where} is not JSON")
                    unread_texts.append(f"{where} is not JSON")
                    continue
                if not isinstance(shared, dict) or not _qa_same_record(shared, report_json):
                    notes.append(f"{where} carries a different assessment or QA summary from the report beside it")
                continue
            key = key_of(name, text)
            if key in seen:
                continue
            seen.add(key)
            if name in local:
                notes.append(f"{where} differs from the {name} beside it")
            sources.append((name, where, text))

    judged, tables = [], []
    for name, where, text in sources:
        if name == QA_TABLE_FILE:
            tables.append(_qa_table(where, text, record))
        else:
            judged.append(_qa_judge_text(where, text, record, methods=name == QA_METHODS_FILE))
    record_fails = list(record.contradictions)
    fails = record_fails + [message for item in judged + tables for message in item["fails"]]
    warns = notes + [message for item in judged + tables for message in item["warns"]]
    readable = [item for item in judged if not item["empty"]]
    covering = [item for item in readable if item["covers"]]
    if readable and not covering:
        warns.append("no text carries the statement Interactive writes for this assessment"
                     + (f", so none says which {len(missing)} criteria could not be assessed" if missing else ""))
    for item in readable:
        if item["speaks"] and not item["covers"] and covering and not item["fails"]:
            warns.append(f"{item['source']}: speaks of QA without the statement Interactive writes for it")
    cited = any(QA_T_TABLE_NOTE.search(_qa_space(text)) for name, _, text in sources if name != QA_TABLE_FILE)
    if tables and not any(item["rows"] for item in tables) and (assessed or cited):
        warns.append(f"{QA_TABLE_FILE} carries no Quality assurance rows, though "
                     + ("a text says the criteria are reported there" if cited else f"{len(assessed)} were evaluated"))
    workbook = (output / QA_WORKBOOK_FILE).exists()
    to_read = [entry for item in judged + tables for entry in item["to_read"]]
    unread_texts += [message for item in tables for message in item.get("unread", [])]

    evidence = {
        **_to_read_evidence("QA-1", to_read),
        "unread_texts": unread_texts,
        "total": len(record.criteria), "evaluated": len(assessed), "passed": passed,
        "evaluated_names": [item["label"] for item in assessed],
        "not_assessed_names": [item["label"] for item in missing],
        "read": [where for _, where, _ in sources],
        "carrying_the_statement": [item["source"] for item in covering],
        "fails": [_qa_short(message, 400) for message in fails[:20]], "fail_count": len(fails),
        "warns": [_qa_short(message, 400) for message in warns[:20]], "warn_count": len(warns),
        "not_read": [QA_WORKBOOK_FILE] if workbook else [],
    }
    if fails:
        prose = len(fails) - len(record_fails)
        headline = ("The published QA contradicts the assessment" if prose else "The assessment contradicts itself")
        report.add("QA-1", stage, QA_TITLE, FAIL,
                   f"{headline} ({len(assessed)} of {len(record.criteria)} criteria evaluated, {passed} met): "
                   + " | ".join(_qa_short(message, 300) for message in fails[:3]), **evidence)
        return
    if not readable:
        report.add("QA-1", stage, QA_TITLE, NOT_EVALUABLE,
                   "No Materials and Methods or QA results text could be read, so no QA prose can be compared with "
                   "the assessment." + (" " + "; ".join(_qa_short(note, 160) for note in notes[:3]) if notes else ""),
                   **evidence)
        return
    if warns:
        report.add("QA-1", stage, QA_TITLE, WARN,
                   f"Nothing established contradicts the assessment ({len(assessed)} of {len(record.criteria)} "
                   f"evaluated, {passed} met), but a person should read: "
                   + " | ".join(_qa_short(message, 300) for message in warns[:3]), **evidence)
        return
    forms = {form for item in covering for form in item["forms"]}
    reasons = sorted({reason for item in covering for reason in item["reasons"]})
    if "no matrix" in forms:
        what = f"say that no QA matrix was supplied, so none of the {len(record.criteria)} criteria was evaluated"
    elif "none from the sample types" in forms and "statement" not in forms:
        what = "say that no prespecified criterion could be evaluated from the available sample types"
    elif missing:
        what = (f"state that {len(assessed)} of {len(record.criteria)} prespecified criteria could be evaluated and "
                f"{passed} met, and name the {len(missing)} that could not be assessed"
                + (f" ({'; '.join(reasons)})" if reasons else ""))
    else:
        what = f"state that all {len(record.criteria)} prespecified criteria could be evaluated and {passed} met"
    table_note = (" The Supplementary Table's QA rows agree." if any(item["rows"] for item in tables)
                  else " The Supplementary Table carries no QA rows." if tables else " No Supplementary Table was read.")
    methods_note = (" and no QA vocabulary before the Methods' QA section"
                    if any(item["methods"] for item in readable) else "")
    report.add("QA-1", stage, QA_TITLE, PASS,
               f"{', '.join(item['source'] for item in covering)} carry the statement Interactive writes for this "
               f"assessment: they {what}.{table_note} No other sentence was found in the QA texts{methods_note}."
               + (f" {QA_WORKBOOK_FILE} was not read." if workbook else ""), **evidence)


# --------------------------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------------------------

# Interactive's accession-scoped download store (download_store.py): <repository>\<accession>\_dl beside the
# unit workspaces, holding each object once under o\<id>\obj and its extraction under o\<id>\t. A unit reads
# NTFS hardlinks to them from its own raw tree, so a unit's name for a file and the store's are one file record.
STORE_DIRECTORY = "_dl"
STORE_OBJECT_PARTS = ("obj", "t")


def _file_record(details: os.stat_result) -> "tuple[int, int] | None":
    """The file record a name points to, or None where the filesystem numbers none (st_ino 0)."""
    return (details.st_dev, details.st_ino) if details.st_ino else None


def _stat_quietly(path: Path) -> "os.stat_result | None":
    """A name's stat, or None for a name that went between the listing and the stat.

    A campaign keeps writing while the gate reads: the store's gc unlinks object trees, a raw deletion
    removes a unit's, and on Windows a name being deleted refuses access until its last handle closes.
    A file that is gone is not counted; that is not a reason for the gate to end without a verdict.
    """
    try:
        return path.stat()
    except (FileNotFoundError, PermissionError):
        return None


class _Occupancy:
    """Bytes under a unit's raw trees, each file record counted once however many names it has.

    A hardlink is a second name for one record, so summing the sizes of the names counts a linked file
    twice within a unit and counts the store's bytes as the unit's own. Records with more than one name
    are kept aside, so that those the accession store also holds can be reported as linked from it.
    """

    def __init__(self) -> None:
        self.seen: set = set()
        self.shared: dict = {}  # file record -> size, for records with other names
        self.logical_bytes = 0
        self.names = 0

    def add(self, root: Path) -> int:
        unique = 0
        if not root.exists():
            return 0
        for item in root.rglob("*"):
            details = _stat_quietly(item) if item.is_file() else None
            if details is None:
                continue
            self.names += 1
            self.logical_bytes += details.st_size
            record = _file_record(details)
            if record is not None and record in self.seen:
                continue
            if record is not None:
                self.seen.add(record)
                if details.st_nlink > 1:
                    self.shared[record] = details.st_size
            unique += details.st_size
        return unique

    def linked_from(self, store: Path) -> "tuple[int, int]":
        """(bytes, files) of the multiply-named records that the store holds too."""
        if not self.shared or not store.is_dir():
            return 0, 0
        held: set = set()
        for directory in sorted((store / "o").glob("*")):
            for part in STORE_OBJECT_PARTS:
                root = directory / part
                if not root.is_dir():
                    continue
                for item in root.rglob("*"):
                    details = _stat_quietly(item) if item.is_file() else None
                    record = _file_record(details) if details is not None else None
                    if record is not None and record in self.shared:
                        held.add(record)
        return sum(self.shared[record] for record in held), len(held)


def _raw_directory(provenance: dict | None, workspace: Path) -> tuple[Path | None, dict | None, str]:
    """The raw tree this unit reads, the manifest that owns it, and why either is unknown.

    A split part owns none and reads its parent's, named in the parent's own manifest. Looking only
    under the part's workspace reports its raw data as released while it is on disk, so a part whose
    owner cannot be read has no known tree at all rather than its own empty one. SPL-1 checks that
    the part's raw_owned_by and raw_directory agree with its split_from.
    """
    if isinstance(provenance, dict) and isinstance(provenance.get("split_from"), dict):
        owner, reason = _raw_owner_manifest(provenance)
        if owner is None:
            return None, None, reason or "the raw owner's manifest cannot be read"
        raw = owner.get("raw_directory")
        if not raw:
            return None, owner, "the raw owner's manifest names no raw_directory"
        return Path(str(raw)), owner, ""
    return workspace / "raw", provenance, ""


def check_storage_shape(report: Report, workspace: Path, stage: str,
                        provenance: dict | None = None) -> None:
    """Report what the unit actually occupies, against what a transfer figure would suggest."""
    if stage != "before-publish":
        return
    if isinstance(provenance, dict) and isinstance(provenance.get("split_from"), dict):
        # A split part owns no raw tree. Its storage is counted once, at the owner: counting it in
        # every part would double the unit, and calling it released would be false.
        report.add("DSK-1", stage, "Retained storage is accounted for", NOT_EVALUABLE,
                   f"The raw tree is owned by {provenance.get('raw_owned_by') or 'its parent'} and "
                   "is accounted there: once all its runs are done, read DSK-1 from the raw owner's "
                   "before-publish report, whose exit code is not a verdict on the owner.",
                   required=False, raw_owned_by=str(provenance.get("raw_owned_by") or ""))
        return
    downloads = workspace / "raw" / "downloads"
    data = workspace / "raw" / "data"
    # The mzML written from mzXML (CONV-1) lies beside the data and is released with it.
    converted = workspace / "raw" / CONVERTED_DIRECTORY
    if not downloads.exists() and not data.exists() and not converted.exists():
        # Not required: a released raw tree is the intended end state under the campaign's
        # delete-after-validated-output policy, so its absence is a result, not a gap.
        report.add("DSK-1", stage, "Retained storage is accounted for", NOT_EVALUABLE,
                   "No raw directory is present; the raw tree may already have been released.",
                   required=False)
        return
    occupancy = _Occupancy()
    # In this order, so a record named under two of them is counted where it was first found.
    archive = occupancy.add(downloads)
    extracted = occupancy.add(data)
    conversions = occupancy.add(converted)
    total = archive + extracted + conversions
    store = workspace.parent / STORE_DIRECTORY
    linked, linked_files = occupancy.linked_from(store)
    evidence = {
        "archive_bytes": archive, "extracted_bytes": extracted, "converted_bytes": conversions, "total_bytes": total,
        "logical_bytes": occupancy.logical_bytes, "file_names": occupancy.names,
        "hardlinked_records": len(occupancy.shared), "linked_from_store_bytes": linked,
        "linked_from_store_files": linked_files, "unit_only_bytes": total - linked,
    }
    recorded = provenance.get("raw_storage") if isinstance(provenance, dict) else None
    if isinstance(recorded, dict):
        evidence["recorded_raw_storage"] = {key: recorded.get(key) for key in (
            "materialization", "logical_bytes", "bytes_linked_from_store", "bytes_copied") if key in recorded}
    shared = (f"; {linked / 1e9:.2f} GB of it ({linked_files} file(s)) is linked from the accession store, "
              "shared with the store's other consumers and freed only when the store collects it"
              if linked else "")
    if archive and extracted:
        report.add(
            "DSK-1", stage, "Retained storage is accounted for", WARN,
            "The downloaded archive and its extraction are both retained, so the unit occupies "
            f"{total / 1e9:.2f} GB for {max(archive, extracted) / 1e9:.2f} GB of unique data"
            + (f" and {conversions / 1e9:.2f} GB of mzML converted from mzXML" if conversions else "") + shared + ". A "
            "size approval quoted against the transfer figure understated actual disk use.",
            **evidence,
        )
        return
    report.add("DSK-1", stage, "Retained storage is accounted for", PASS,
               f"The unit occupies {total / 1e9:.2f} GB"
               + (f", {conversions / 1e9:.2f} GB of it mzML converted from mzXML" if conversions else "") + shared + ".",
               **evidence)


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


RATIFIED_STATUSES = frozenset({"accepted", "confirmed", "approved"})


def _ratified(proposal: dict | None) -> bool:
    return str((proposal or {}).get("status") or "").strip().casefold() in RATIFIED_STATUSES


# The reasons the Catalog's declared-factor selection gives for abstaining (msdial_repository_catalog
# class_proposal.ABSTENTION_REASONS).
ABSTENTION_REASONS = ("no_declared_factor", "no_usable_declared_factor")


def _class_proposal(provenance: dict | None) -> dict | None:
    if not isinstance(provenance, dict):
        return None
    proposal = (provenance.get("project") or {}).get("class_proposal")
    return proposal if isinstance(proposal, dict) and proposal else None


# The containers MS-DIAL opens, as Interactive's archives.py names them beside its archive suffixes
# (ARCHIVE_SUFFIXES). Its container_alias is the one alias rule: X.d.zip stands for the folder X.d,
# which the CSV calls X.
CONTAINER_SUFFIXES = (".raw", ".d", ".wiff", ".wiff2", ".mzml", ".mzxml", ".lcd", ".cdf", ".qgd", ".abf")


def _strip_suffix(name: str, suffixes: tuple[str, ...]) -> str:
    lower = name.casefold()
    return next((name[:-len(suffix)] for suffix in suffixes
                 if lower.endswith(suffix) and len(lower) > len(suffix)), name)


def _csv_name_forms(raw_file: str) -> list[str]:
    """The names an analysis-CSV row may carry for a recorded raw_file, most specific first.

    A folder is recorded with a trailing "/" (raw/x.raw/), and a container that was published packed
    with its archive suffix (x.d.zip), while the CSV names both by the container's stem (x). So the
    trailing "/" goes, then an archive suffix, then a container suffix; the file name less its last
    extension, as this check always tried, stays last.
    """
    name = raw_file.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    unpacked = _strip_suffix(name, ARCHIVE_SUFFIXES)
    forms = [name, unpacked, _strip_suffix(unpacked, CONTAINER_SUFFIXES),
             unpacked.rsplit(".", 1)[0], name.rsplit(".", 1)[0]]
    return [form for form in dict.fromkeys(forms) if form]


def _files_of_samples(provenance: dict | None, samples: set[str], csv_names: set[str]) -> dict[str, list[str]]:
    """The analysis-CSV rows each approved sample is, by name or through its recorded raw file.

    A sample whose id is itself a CSV file_name maps to that row. Otherwise every raw_file the
    unit's sample metadata records for it is reduced to the name the CSV uses (_csv_name_forms: the
    container's stem for a folder or an archived container, else the file name without its
    extension) and kept if the CSV has it. A sample that maps to nothing is absent.
    """
    recorded: dict[str, list[str]] = {}
    for row in ((provenance or {}).get("project") or {}).get("sample_metadata") or []:
        if not isinstance(row, dict):
            continue
        sample = str(row.get("sample_id") or "")
        raw = str(row.get("raw_file") or "")
        if sample and _csv_name_forms(raw):
            recorded.setdefault(sample, []).append(raw)
    result: dict[str, list[str]] = {}
    for sample in samples:
        if sample in csv_names:
            result[sample] = [sample]
            continue
        names = []
        for raw in recorded.get(sample, []):
            for candidate in _csv_name_forms(raw):
                if candidate in csv_names and candidate not in names:
                    names.append(candidate)
                    break
        result[sample] = names
    return result


# The kinds of declared analysis input MS-DIAL opens, as Interactive's declared_analysis_inputs keeps them.
# A declared directory is a sample but never an input, and an input that must be converted first (mzXML)
# is attributed through its conversion's record, not through the declaration.
DECLARED_INPUT_KINDS = frozenset({"file", "vendor_folder", "archived_container"})


def _declared_input_key(value: object) -> str:
    """A declared path as Interactive keys it (_safe_relative_name): "/"-separated and casefolded, less a
    leading "/" and FILES/ and any empty or "." component; "" for none, or for one that climbs out."""
    parts = [part for part in _declared_name(value).split("/") if part not in ("", ".")]
    return "" if not parts or ".." in parts else "/".join(parts)


def _allowlist_forms(relative: str) -> list[str]:
    """Interactive's _allowlist_forms, most specific first: the path relative to the data root, less a
    leading FILES/, and less its first component (the folder an archive unpacks into), again less a
    FILES/ under it. Nothing deeper."""
    forms = {relative}
    if relative.startswith("files/"):
        forms.add(relative[6:])
    parts = relative.split("/")
    if len(parts) > 1:
        rest = "/".join(parts[1:])
        forms.add(rest)
        if rest.startswith("files/"):
            forms.add(rest[6:])
    return sorted(forms, key=len, reverse=True)


def _declared_samples(manifest: dict) -> tuple[dict[str, str], dict[str, str]]:
    """The sample the Catalog declared each analysis input for (project.analysis_inputs), keyed as
    Interactive keys it (_declared_input_key); and an archived container's, keyed by its archive."""
    project = manifest.get("project") if isinstance(manifest.get("project"), dict) else {}
    inputs = project.get("analysis_inputs")
    by_path: dict[str, str] = {}
    by_archive: dict[str, str] = {}
    for entry in inputs if isinstance(inputs, list) else []:
        if (not isinstance(entry, dict) or entry.get("requires_conversion")
                or str(entry.get("kind") or "") not in DECLARED_INPUT_KINDS):
            continue
        path = _declared_input_key(entry.get("path"))
        sample = str(entry.get("sample_id") or "").strip()
        if path:
            by_path.setdefault(path, sample)
        archive = _declared_input_key(entry.get("archive")) if entry.get("kind") == "archived_container" else ""
        if archive and sample:
            by_archive.setdefault(archive, sample)
    return by_path, by_archive


def _declared_sample_of(manifest: dict, row: dict, declared: tuple[dict[str, str], dict[str, str]]) -> str:
    """The sample the Catalog declared a lineage row's input for, read as Interactive's CSV builder reads it.

    Interactive's build_input_lineage fills a row's sample_id only where exactly one of the unit's samples
    names the input by its base name, so neg/x.mzML and pos/x.mzML, which share x.mzml, both carry "". Its
    analysis-CSV builder takes the sample from the Catalog's declared analysis input first
    (project.analysis_inputs[].sample_id, _declared_samples), matched by the input's path relative to the
    data root, the most specific form first (match_declared_inputs, _allowlist_forms), and so writes each
    row with its own sample's Class. So is the sample read here: by that path, else through the one
    declared input, or the archive of the one archived container, that the row's declared_names name. The
    declaration is the Catalog's, the writer of the assignments, never the CSV writer's. Where the lineage
    row does name a sample, that is still the one, as this check always read it (_input_samples). A
    container its archive unpacked under another name than the one declared is matched by neither, and is
    left to the name.
    """
    by_path, by_archive = declared
    if not by_path:
        return ""
    root_text = str(manifest.get("input_directory") or "").strip()
    root = _path_key(root_text).rstrip("\\/") if root_text else ""
    path = _path_key(row["path"])
    if root and path.startswith(root + os.sep):
        relative = path[len(root) + 1:].replace(os.sep, "/").casefold()
        form = next((form for form in _allowlist_forms(relative) if form in by_path), None)
        if form is not None:
            return by_path[form]
    names = row.get("declared_names") if isinstance(row.get("declared_names"), list) else []
    named = {by_path.get(name) or by_archive.get(name, "") for name in map(_declared_input_key, names) if name}
    named.discard("")
    return next(iter(named)) if len(named) == 1 else ""


def _input_samples(provenance: dict, parts: tuple[str, ...] = ("rows",)) -> dict[str, str]:
    """The sample each input of the unit's lineage is, by key: the one its lineage row names (sample_id),
    else the one the Catalog declared the input for (_declared_sample_of); "" where neither names one.

    The lineage rows are the unit's own and, for a split part, its raw owner's (_lineage_manifests), each
    read against its own manifest's declaration and data root; an input's first row to name a sample
    gives it. ``parts`` are the lineage's inputs ("rows") and what the lease excluded ("excluded").
    """
    samples: dict[str, str] = {}
    for manifest in _lineage_manifests(provenance):
        declared: "tuple[dict[str, str], dict[str, str]] | None" = None
        for part in parts:
            for row in _lineage_rows(manifest, part):
                key = _path_key(row["path"])
                if samples.get(key):
                    continue
                sample = str(row.get("sample_id") or "").strip()
                if not sample:
                    declared = declared if declared is not None else _declared_samples(manifest)
                    sample = _declared_sample_of(manifest, row, declared)
                samples[key] = sample
    return samples


def _samples_by_csv_name(provenance: dict | None, csv_rows: list[dict]) -> dict[str, str]:
    """The sample each analysis-CSV row is, by the row's file_name, where the input lineage says so.

    A row is the input it opens: its own path, or the input its Console alias stands for
    (_input_keys_by_console_path reads console_path and console_alias). That input's lineage row names
    the sample the lease attributed it to (sample_id), and where it names none the input is the sample
    the Catalog declared it for (_input_samples); and once Interactive built the CSV from the lineage,
    the row records the name the CSV gives it (file_name): the input's stem, its alias, or the stem made
    unique by a digest. A lineage row recording another file_name than the row's describes another CSV,
    and says nothing of this one. Rows the lineage says nothing of are left out.
    """
    if not isinstance(provenance, dict):
        return {}
    by_path: dict[str, dict] = {}
    for manifest in _lineage_manifests(provenance):
        for row in _lineage_rows(manifest):
            by_path.setdefault(_path_key(row["path"]), row)
    if not by_path:
        return {}
    aliases = _input_keys_by_console_path(provenance)
    samples = _input_samples(provenance)
    result: dict[str, str] = {}
    for row in csv_rows:
        name = str(row.get("file_name", ""))
        key = _input_key(row, aliases)
        lineage = by_path.get(key) or {}
        sample = samples.get(key, "") if lineage else ""
        recorded = str(lineage.get("file_name") or "")
        if sample and (not recorded or recorded == name):
            result[name] = sample
    return result


def _container_stem(name: str) -> str:
    """A path as the CSV names the input it is, compared without case: the last component less a
    trailing "/" and an archive suffix, then less its container suffix, else less its last extension."""
    base = str(name).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    unpacked = _strip_suffix(base, ARCHIVE_SUFFIXES)
    stripped = _strip_suffix(unpacked, CONTAINER_SUFFIXES)
    return (stripped if stripped != unpacked else unpacked.rsplit(".", 1)[0]).casefold()


def _excluded_samples(provenance: dict | None, samples: set[str]) -> dict[str, list[str]]:
    """Of these approved samples, those whose input was excluded from the run, with the inputs' names.

    Excluded by a binding campaign disposition (the unit's, and a split part's parent's), or by the lease
    itself (excluded_input_candidates: an mzML RawDataHandler cannot decode). Either is another writer
    than the CSV's. The sample an excluded input is the input of is what the lease's lineage row names
    for it, or where that names none the sample the Catalog declared the input for (_input_samples);
    where neither does, the CSV record (analysis_csv.excluded_inputs), in which Interactive names each
    such input with its sample; and else a sample whose id, or recorded raw_file, is the input by name.
    The CSV record is the CSV writer's own, so it never outranks the lease's or the Catalog's: a CSV that
    dropped one sample's row and named that sample beside another's excluded input would otherwise
    excuse both. An entry of the CSV record that neither exclusion bears out excludes nothing, and nor
    does any exclusion excuse a sample the lineage or the declaration gives an input that runs (a
    candidate nobody excluded): that sample's row is missing, whatever else of it was excluded.
    """
    if not isinstance(provenance, dict) or not samples:
        return {}
    manifests = _lineage_manifests(provenance)
    excluded = {key: path for key, (path, _reason) in _exclusion_reasons(*manifests).items()}
    for manifest in manifests:
        for item in manifest.get("excluded_input_candidates") or []:
            if isinstance(item, dict) and str(item.get("path") or "").strip():
                excluded.setdefault(_path_key(item["path"]), str(item["path"]))
    if not excluded:
        return {}
    candidates = provenance.get("input_candidates") if isinstance(provenance.get("input_candidates"), list) else []
    runnable = {_path_key(item) for item in candidates if str(item).strip()} - set(excluded)
    attributed = _input_samples(provenance, ("rows", "excluded"))
    running = {sample for key, sample in attributed.items() if key in runnable}
    named: dict[str, str] = {key: sample for key, sample in attributed.items() if key in excluded and sample}
    record = provenance.get("analysis_csv")
    listed = record.get("excluded_inputs") if isinstance(record, dict) else None
    for item in listed if isinstance(listed, list) else []:
        if isinstance(item, dict):
            key = _path_key(item.get("path") or "")
            sample = str(item.get("sample_id") or "").strip()
            if key in excluded and sample:
                named.setdefault(key, sample)
    by_id = {sample.strip(): sample for sample in samples}
    raw_files: dict[str, set[str]] = {}
    for row in ((provenance.get("project") or {}).get("sample_metadata") or []):
        if not isinstance(row, dict):
            continue
        sample, raw = str(row.get("sample_id") or "").strip(), str(row.get("raw_file") or "").strip()
        if sample and raw:
            raw_files.setdefault(_container_stem(raw), set()).add(sample)
    result: dict[str, list[str]] = {}
    for key, path in excluded.items():
        stem = _container_stem(path)
        owners = {named[key]} if key in named else (
            {sample for sample in by_id if sample.casefold() == stem} | raw_files.get(stem, set()))
        for sample in owners - running:
            if sample in by_id:
                result.setdefault(by_id[sample], []).append(Path(path.rstrip("\\/")).name)
    return result


def _base_name(value: object) -> str:
    """A path's last component, compared without case: "/" or "\\" separated, less a trailing separator."""
    return str(value or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].casefold()


def _unreached_samples(provenance: dict | None, samples: set[str]) -> dict[str, list[str]]:
    """Of these approved samples, those that never reached the lease's inputs, with the raw files their rows
    record.

    A sample is unreached when the input lineage, the lease's record of what it admitted, says nothing of
    it, and nothing the lease admitted carries its name. That is: no lineage row (an input, or one the lease
    excluded, of the unit or of a split part's raw owner) names it, by its sample_id, by the sample of the
    Catalog's declared input (_input_samples), by the sample row the CSV was written from (sample_row), by
    a declared name it is listed under (declared_names) or by the declared raw file an inferred pairing
    gave it (name_pairing); and no input candidate, lineage path or excluded candidate has the sample's
    id or a raw file of its rows as its name (_container_stem). Such a sample is not a row the CSV writer
    dropped, which CLS-2 calls missing: the lineage shows it never reached the run.

    Whether it was never delivered, or delivered and left unpaired, is not the lineage's to say: the
    lineage records what the lease admitted, not what the download brought. _delivery_of reads that from
    the archive member listings and the downloads.

    Read from the lineage only, never from the CSV record (analysis_csv.samples_without_input), which is
    the CSV writer's own account. Without a lineage nothing is known unreached, and a sample with no
    row stays missing as before.
    """
    if not isinstance(provenance, dict) or not samples:
        return {}
    manifests = _lineage_manifests(provenance)
    lineage = [row for manifest in manifests for part in ("rows", "excluded") for row in _lineage_rows(manifest, part)]
    if not lineage:
        return {}
    raw_files: dict[str, list[str]] = {}
    by_name: dict[str, set[str]] = {}
    for manifest in manifests:
        for row in ((manifest.get("project") or {}).get("sample_metadata") or []):
            if not isinstance(row, dict):
                continue
            sample, raw = str(row.get("sample_id") or "").strip(), str(row.get("raw_file") or "").strip()
            if sample and raw:
                if raw not in raw_files.setdefault(sample, []):
                    raw_files[sample].append(raw)
                for form in (_base_name(raw), _container_stem(raw)):
                    by_name.setdefault(form, set()).add(sample)
    named = {sample for sample in _input_samples(provenance, ("rows", "excluded")).values() if sample}
    for row in lineage:
        sample_row = row.get("sample_row")
        if isinstance(sample_row, dict) and str(sample_row.get("sample_id") or "").strip():
            named.add(str(sample_row["sample_id"]).strip())
        pairing = row.get("name_pairing")
        declared = [*(row.get("declared_names") if isinstance(row.get("declared_names"), list) else []),
                    *([pairing.get("declared_raw_file")] if isinstance(pairing, dict) else [])]
        for name in declared:
            if str(name or "").strip():
                named |= by_name.get(_base_name(name), set()) | by_name.get(_container_stem(str(name)), set())
    seen: list[object] = list(provenance.get("input_candidates") or [])
    for manifest in manifests:
        seen += [item.get("path") for item in manifest.get("excluded_input_candidates") or [] if isinstance(item, dict)]
    seen += [row["path"] for row in lineage]
    stems = {_container_stem(str(path)) for path in seen if str(path or "").strip()}
    stems |= {_base_name(path) for path in seen if str(path or "").strip()}
    result: dict[str, list[str]] = {}
    for sample in samples:
        stripped = sample.strip()
        if not stripped or stripped in named:
            continue
        forms = {stripped.casefold(), _container_stem(stripped)}
        forms |= {form for raw in raw_files.get(stripped, []) for form in (_base_name(raw), _container_stem(raw))}
        if not forms & stems:
            result[sample] = raw_files.get(stripped, [])
    return result


def _loose_name(value: object) -> str:
    """A name compared loosely: its container stem (_container_stem) with everything but letters and digits
    dropped, so QC-D5-C and the archive member QC-D5-C-.mzML are one name."""
    return "".join(character for character in _container_stem(str(value or "")) if character.isalnum())


def _member_container(member: str) -> str:
    """The container an archive member is, or is inside, as the listing names it ("/"-separated): the first
    path component, from the top, that carries a container suffix (x.d of x.d/AcqData/..., x.raw of a
    Waters folder), or ""; a member in no container (a log, a tag file, a README) is no input."""
    parts = [part for part in member.split("/") if part]
    for index, part in enumerate(parts):
        if _strip_suffix(part, CONTAINER_SUFFIXES) != part:
            return "/".join(parts[:index + 1])
    return ""


def _member_listing(owner: dict, record: dict) -> _ArchiveListing:
    """An extraction record's member listing, as _InputLineage._listing finds and checks it."""
    members = record.get("members_tsv")
    name = str(record.get("archive_name") or "the archive")
    if not isinstance(members, dict) or not str(members.get("path") or "").strip():
        return _ArchiveListing(problem=f"the extraction record of {name} keeps no member listing")
    recorded = Path(str(members["path"]))
    candidates = [recorded]
    if str(owner.get("workspace") or "").strip():
        candidates.append(Path(str(owner["workspace"])) / "provenance" / recorded.name)
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        return _ArchiveListing(problem=f"the member listing of {name} ({recorded.name}) is absent")
    return _read_archive_listing(path, str(members.get("sha256") or ""), name)


def _download_container(path: str, raw_directory: str = "") -> str:
    """The input container a download that is no archive is, or is inside, read as _member_container reads an
    archive member: the path below the raw directory ("/"-separated; the whole path where it is not under
    it), and in it the first component, from the top, that carries a container suffix. A vendor folder
    fetched file by file (MetaboBank's Waters x.raw/_FUNC001.DAT, _extern.inf, SystemSettings.xml ...) is
    one input, x.raw; a file that is an input (x.mzML, x.wiff) is itself; and a companion file beside its
    input (x.wiff.scan) or a file in no container (a README) is no input, "", as in an archive listing."""
    text = str(path).replace("\\", "/").rstrip("/")
    root = str(raw_directory or "").replace("\\", "/").rstrip("/")
    if root and text.casefold().startswith(root.casefold() + "/"):
        text = text[len(root) + 1:]
    return _member_container(text)


def _is_archive_download(item: dict) -> bool:
    archive = item.get("archive")
    if isinstance(archive, dict) and str(archive.get("format") or "").strip():
        return True
    return str(item.get("path") or "").casefold().endswith(ARCHIVE_SUFFIXES)


@dataclass
class _Delivery:
    """What the download delivered against what the lease paired (_delivery_of)."""
    unpaired: dict = field(default_factory=dict)      # sample -> the unpaired delivered files named after it
    undelivered: dict = field(default_factory=dict)   # sample -> the raw files its rows record
    unestablished: dict = field(default_factory=dict)  # sample -> the raw files its rows record
    unpaired_files: list = field(default_factory=list)
    delivered: int = 0
    shared: bool = False
    problem: str = ""


def _delivery_of(provenance: dict | None, unreached: dict[str, list[str]]) -> _Delivery:
    """Of the approved samples that never reached the lease's inputs (_unreached_samples), those whose files the
    download delivered but the lease did not pair, and those it never delivered.

    WHAT WAS DELIVERED is the raw owner's record of its download, never the CSV: every input container an
    archive member listing (archive_extractions[].members_tsv, its sha256 checked) marks extracted, and every
    input container a download that is no archive is or is inside (_download_container: a Waters .raw folder
    fetched file by file is one input, and a .wiff.scan companion is none). WHAT WAS PAIRED is every input the lease admitted or excluded: the input
    candidates, the lineage rows of the raw owner and of the unit (a split part's sibling's included: paired,
    to the sibling), and the excluded candidates. A delivered container none of those is, compared by stem
    (_container_stem), is an unpaired delivered file.

    A sample is DELIVERED BUT UNPAIRED, a mapping failure, where an unpaired delivered file carries its name
    loosely (_loose_name: ST004304's QC-D5-C and the member QC-D5-C-.mzML); and where unpaired delivered
    files that carry no unreached sample's name remain, every unreached sample left is too, since one of
    them may be any of those files (ST001264's Sample1..28 and its members Youn_sa1..28.raw), unless the
    download is shared with other units (download_scope shared_unit_count above 1), whose files those may
    be. A sample is NEVER DELIVERED only where the inventory leaves no such doubt. Where the inventory
    cannot be read (no download recorded, an archive with no readable member listing), neither is
    established, and the samples are left unestablished: CLS-2 counts them missing.
    """
    result = _Delivery()
    if not unreached:
        return result
    owner, why = _raw_owner_manifest(provenance)
    if owner is None:
        result.problem = why or "the unit's raw owner is not recorded"
        result.unestablished = dict(unreached)
        return result
    downloads = [item for item in owner.get("downloads") or [] if isinstance(item, dict) and str(item.get("path") or "").strip()]
    if not downloads:
        result.problem = "the raw owner's manifest records no download"
        result.unestablished = dict(unreached)
        return result
    delivered: dict[str, str] = {}
    for item in downloads:
        if not _is_archive_download(item):
            container = _download_container(str(item["path"]), str(owner.get("raw_directory") or ""))
            if container:
                delivered.setdefault(_container_stem(container), container.rsplit("/", 1)[-1])
    if any(_is_archive_download(item) for item in downloads):
        records = [item for item in owner.get("archive_extractions") or [] if isinstance(item, dict)]
        if not records:
            result.problem = "an archive was downloaded and the manifest records no extraction of it"
            result.unestablished = dict(unreached)
            return result
        for record in records:
            listing = _member_listing(owner, record)
            if listing.problem:
                result.problem = listing.problem
                result.unestablished = dict(unreached)
                return result
            # The files first, whose names the listing spells; a directory only as its key.
            for member in [*sorted(listing.files), *sorted(listing.directories)]:
                container = _member_container(listing.names.get(member, member))
                if container:
                    delivered.setdefault(_container_stem(container), container.rsplit("/", 1)[-1])
    paired: list[object] = [*((provenance or {}).get("input_candidates") or []), *(owner.get("input_candidates") or [])]
    for manifest in [owner, *([provenance] if provenance is not owner else [])]:
        paired += [item.get("path") for item in manifest.get("excluded_input_candidates") or [] if isinstance(item, dict)]
        paired += [row["path"] for part in ("rows", "excluded") for row in _lineage_rows(manifest, part)]
    paired_stems = {_container_stem(str(path)) for path in paired if str(path or "").strip()}
    unpaired = {stem: name for stem, name in delivered.items() if stem not in paired_stems}
    result.delivered = len(delivered)
    result.unpaired_files = sorted(unpaired.values())
    scope = ((owner.get("project") or {}).get("download_scope") if isinstance(owner.get("project"), dict) else None) or {}
    counts = [scope.get("bundle_shared_unit_count")] + [item.get("shared_unit_count") for item in scope.get("objects") or []
                                                       if isinstance(item, dict)]
    result.shared = any(isinstance(count, int) and not isinstance(count, bool) and count > 1 for count in counts)
    loose = {}
    for stem, name in unpaired.items():
        loose.setdefault(_loose_name(name), []).append(name)
    claimed: set[str] = set()
    for sample, raws in sorted(unreached.items()):
        forms = {_loose_name(sample)} | {_loose_name(raw) for raw in raws}
        forms.discard("")
        names = sorted({name for form in forms for name in loose.get(form, [])})
        if names:
            result.unpaired[sample] = names
            claimed.update(names)
    leftover = [name for name in result.unpaired_files if name not in claimed]
    for sample, raws in sorted(unreached.items()):
        if sample in result.unpaired:
            continue
        if leftover and not result.shared:
            result.unpaired[sample] = []
        else:
            result.undelivered[sample] = raws
    return result


def _class_token(value: object) -> str:
    """A Class label as Interactive projects it into the analysis CSV (repository_metadata.class_token,
    which apply_class_proposal applies to every approved label): NFKC, runs of white space and "_" as
    "-", anything but a letter, a digit or ".+-" as "-", and the "-" runs joined and trimmed."""
    text = unicodedata.normalize("NFKC", str(value if value is not None else "").strip())
    text = re.sub(r"[\s_]+", "-", text.strip())
    text = "".join(character if character.isalnum() or character in ".+-" else "-" for character in text)
    return re.sub(r"-+", "-", text).strip("-")


def _console_reads_back(value: object) -> bool:
    """Whether the Console's analysis-CSV parser reads this value back as written (Interactive's
    workflow.console_safe_text): printable ASCII without a comma or a quote."""
    text = str(value)
    return all(" " <= character <= "~" for character in text) and "," not in text and '"' not in text


# The name Interactive gives a Class label whose ASCII fold is empty or another label's (Class1, Class2, ...).
_NUMBERED_CLASS = re.compile(r"Class[1-9][0-9]*")


def _class_id_aliases(provenance: dict | None) -> tuple[dict[str, str], dict[str, str]]:
    """The ASCII Class each projected label the Console could not read back was written as, and the
    entries of the record that are no such fold.

    Interactive's lineage-built CSV (repository_analysis_rows._class_aliases) leaves a label the Console
    reads back as it is, folds any other to ASCII (class_token of its NFKD fold), or numbers it (Class1,
    Class2, ...) where the fold is empty or meets another label, keeping the grouping, and records the
    map in analysis_csv.class_id_aliases of the CSV it wrote (status "written"); a failed record wrote no
    CSV and maps nothing. The record is the CSV writer's own, so an entry is taken only as that fold: one
    that renames a label the Console reads back, or gives a label another meaningful name, would let a
    CSV that swapped "Control" and "Treated" record the swap and pass. Such entries are returned apart.
    """
    record = provenance.get("analysis_csv") if isinstance(provenance, dict) else None
    aliases = record.get("class_id_aliases") if isinstance(record, dict) and record.get("status") == "written" else None
    if not isinstance(aliases, dict):
        return {}, {}
    folds: dict[str, str] = {}
    refused: dict[str, str] = {}
    for label, alias in aliases.items():
        label = str(label)
        if not isinstance(alias, str) or not alias:
            continue
        ascii_fold = _class_token(unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii"))
        if (not _console_reads_back(label) and _console_reads_back(alias)
                and (alias == ascii_fold or _NUMBERED_CLASS.fullmatch(alias))):
            folds[label] = alias
        else:
            refused[label] = alias
    return folds, refused


def _executed_forms(label: str, aliases: dict[str, str]) -> set[str]:
    """The Class labels an approved label may run as: itself, as projected ("Sample" where the projection
    leaves nothing, as apply_class_proposal writes it), and as folded for the Console."""
    token = _class_token(label) or "Sample"
    forms = {label, token} | {aliases[form] for form in (label, token) if form in aliases}
    return {form for form in forms if form}


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

    A ROW IS JOINED TO ITS SAMPLE through the input lineage first (_samples_by_csv_name): the input the
    row opens, through its Console alias, and the sample its lineage row names, or where it names none
    (two inputs sharing a base name) the sample the Catalog declared the input for. Since Interactive builds
    the CSV from the lineage, a row's file_name may be an ASCII alias or a stem made unique by a digest
    (file_name_not_unique), which no sample's name or raw file is. A row the lineage says nothing of is
    joined by name, as before (_files_of_samples). An approved sample with no row is absent unless its
    input was excluded from the run, by a binding campaign disposition or by the lease, and then it is
    reported as excluded (_excluded_samples). Whose input an excluded one was is the lease's lineage to
    say before the CSV record, which is the CSV writer's own, and a sample the lineage gives an input
    that runs is absent however much else of it was excluded. Where every approved sample was excluded
    the Console reads no grouping, and that is a FAIL, not a comparison of nothing.

    AN APPROVED SAMPLE THAT NEVER REACHED THE LEASE'S INPUTS (_unreached_samples: no input lineage row names
    it and no input the lease admitted carries its name) is told apart by what the download delivered
    (_delivery_of), read from the archive member listings and the downloads in provenance, never from the CSV:
    - DELIVERED BUT UNPAIRED: a delivered file the lease did not pair carries its name, or unpaired
      delivered files remain that may be its (ST004304's QC-D5-C beside the member QC-D5-C-.mzML; ST001264's
      Sample1..28 beside the members Youn_sa1..28.raw). That is a mapping failure: listed under its own
      evidence key (delivered_unpaired_samples, with the unpaired files under unpaired_delivered_files) and
      a FAIL, since the run leaves out data it holds;
    - NEVER DELIVERED: the delivery holds nothing it could be. Listed under undelivered_samples, and a WARN
      naming how many approved samples the run covers, where a missing sample FAILs;
    - neither established, where the delivery cannot be read: counted missing, as before.
    It is never read from the CSV record's samples_without_input, the CSV writer's own account. A run of
    none of the approved samples is still a FAIL.

    A LABEL IS COMPARED AS INTERACTIVE WRITES IT: projected (_class_token, "Wild type" as "Wild-type",
    and "Sample" where the projection leaves nothing), and, where the Console could not read the
    projection back, folded to the ASCII Class the CSV record names (analysis_csv.class_id_aliases),
    an entry taken only where it is Interactive's fold of the label (_class_id_aliases). Each approved
    Class must still run as one executed Class and no executed Class carry two: a projection or a fold
    that merges or splits Classes is a grouping nobody approved, which the label comparison alone would
    not see.

    UNATTRIBUTED MEMBERS (user decision, 2026-10-07; _unattributed_members). A row that opens an archive member
    the lease included unattributed is no approved sample: it is neither an unapproved row nor joined to one by
    its name, and its Class must be the unit's abstention Class where the Class decision is an abstention, else
    "Unattributed" (a different one is a Class mismatch). An approved sample that never reached the lease's
    inputs in such a unit is neither missing nor undelivered nor a pairing failure: its file may be any of the
    unattributed members, and which one nobody recorded. It is listed apart (samples_without_attributed_input),
    and the check WARNs, listing the members with manifest.unattributed_members.count. Delivered files that are
    neither paired nor unattributed still FAIL as before.

    RUN POLICY: record_only, as the user named it (2026-10-01), as for CLS-1.
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
    unattributed = _unattributed_members(provenance)
    free = set(_unattributed_csv_names(provenance, csv_rows, unattributed))
    # A Class is assigned to a SAMPLE and the CSV has a row per FILE. Where a repository names its
    # samples after their files the two keys coincide, which is all this check used to handle.
    # MetaboLights does not: MTBLS2207's "DDA E. coli" is the file M3T-Std_Ecoli_neg_DDA_1mz, and
    # joining on equal strings called all six approved samples absent and all six rows unapproved
    # while every one carried its approved Class. The repository's own sample-to-file record
    # (sample_metadata raw_file) is the link; it is neither of the two writers being compared. The
    # input lineage, written by the lease, is the link for a CSV built from it.
    through_lineage = {name: sample for name, sample in _samples_by_csv_name(provenance, csv_rows).items()
                       if name not in free}
    by_id = {sample.strip(): sample for sample in approved}
    files_of_sample: dict[str, list[str]] = {}
    for name, sample in through_lineage.items():
        if sample in by_id:
            files_of_sample.setdefault(by_id[sample], []).append(name)
    by_name = _files_of_samples(provenance, set(approved) - set(files_of_sample),
                                set(executed) - set(through_lineage) - free)
    files_of_sample.update(by_name)
    unmatched = {sample for sample in approved if not files_of_sample.get(sample)}
    excluded = _excluded_samples(provenance, unmatched)
    delivery = _delivery_of(provenance, _unreached_samples(provenance, unmatched - set(excluded)))
    undelivered, unpaired = delivery.undelivered, delivery.unpaired
    # Beside unattributed members, a sample nothing delivered could be is one whose file may be any of them.
    without_attribution = dict(undelivered) if unattributed else {}
    if without_attribution:
        undelivered = {}
    aliases, refused = _class_id_aliases(provenance)
    mapped: set[str] = set()
    missing = []
    differing = []
    ran_as: dict[str, set[str]] = {}
    carried: dict[str, set[str]] = {}
    folded: dict[str, str] = {}
    for sample, label in sorted(approved.items()):
        files = files_of_sample.get(sample) or []
        if not files:
            if (sample not in excluded and sample not in undelivered and sample not in unpaired
                    and sample not in without_attribution):
                missing.append(sample)
            continue
        forms = _executed_forms(label, aliases)
        for name in files:
            mapped.add(name)
            if executed[name] not in forms:
                differing.append(
                    f"{sample}{'' if name == sample else f' ({name})'}: approved {label!r}, "
                    f"executed {executed[name]!r}"
                )
                continue
            ran_as.setdefault(label, set()).add(executed[name])
            carried.setdefault(executed[name], set()).add(label)
            if executed[name] != label:
                folded[label] = executed[name]
    extra = sorted(set(executed) - mapped - free)
    # An unattributed member's row runs in the abstention Class, or in "Unattributed": any other is a mismatch.
    free_forms = _executed_forms(unattributed.class_label, aliases) if free else set()
    for name in sorted(free):
        if executed.get(name) not in free_forms:
            differing.append(f"{name} (an unattributed archive member): executed {executed.get(name)!r}, where an "
                             f"unattributed member runs in {unattributed.class_label!r}")
    regrouped = ([f"approved {label!r} runs as {sorted(labels)}" for label, labels in sorted(ran_as.items())
                  if len(labels) > 1]
                 + [f"{label!r} carries approved {sorted(labels)}" for label, labels in sorted(carried.items())
                    if len(labels) > 1])
    joined_by_file = sum(1 for sample, files in by_name.items() if files and files != [sample])
    joined_by_lineage = len(set(files_of_sample) - set(by_name))
    evidence = {"assignments": len(approved), "joined_through_raw_file": joined_by_file}
    if joined_by_lineage:
        evidence["joined_through_lineage"] = joined_by_lineage
    if excluded:
        evidence["excluded_samples"] = {sample: names for sample, names in sorted(excluded.items())[:10]}
    if undelivered:
        # Kept apart from "missing": the lineage shows these never reached the run, which is not the CSV
        # writer dropping a row it had.
        evidence["undelivered_samples"] = {sample: raws for sample, raws in sorted(undelivered.items())[:10]}
        evidence["undelivered_count"] = len(undelivered)
    if unpaired:
        # A mapping failure: the download delivered files the lease did not pair with these samples.
        evidence["delivered_unpaired_samples"] = {sample: names for sample, names in sorted(unpaired.items())[:10]}
        evidence["delivered_unpaired_count"] = len(unpaired)
    if undelivered or unpaired:
        evidence["unpaired_delivered_files"] = delivery.unpaired_files[:10]
        evidence["unpaired_delivered_file_count"] = len(delivery.unpaired_files)
        evidence["delivered_files"] = delivery.delivered
        if delivery.shared:
            evidence["download_shared_with_other_units"] = True
    if delivery.unestablished:
        evidence["delivery_not_established"] = delivery.problem
    if free or without_attribution:
        evidence.update(unattributed.evidence())
        evidence["unattributed_rows"] = len(free)
    if without_attribution:
        evidence["samples_without_attributed_input"] = {sample: raws for sample, raws in
                                                        sorted(without_attribution.items())[:10]}
        evidence["samples_without_attributed_input_count"] = len(without_attribution)
    if folded:
        evidence["written_as"] = dict(sorted(folded.items())[:10])
    if refused:
        evidence["class_id_aliases_not_a_fold"] = dict(sorted(refused.items())[:10])
    analysed = len(approved) - len(excluded) - len(undelivered) - len(unpaired) - len(without_attribution)
    free_sentence = ""
    if free or without_attribution:
        free_sentence = unattributed.sentence() + ", none of them an approved sample"
        if without_attribution:
            free_sentence += (f"; {len(without_attribution)} approved sample(s) have no attributed input, and their "
                              "files may be any of those members (" + ", ".join(sorted(without_attribution)[:5])
                              + (", ..." if len(without_attribution) > 5 else "") + ")")
    unpaired_sentence = (
        f"{len(unpaired)} approved sample(s) have no input although the download delivered files the lease did not "
        "pair (" + ", ".join(f"{sample}" + (f" as {', '.join(names[:2])}" if names else "")
                             for sample, names in sorted(unpaired.items())[:5])
        + (", ..." if len(unpaired) > 5 else "") + f"; {len(delivery.unpaired_files)} unpaired delivered file(s): "
        + ", ".join(delivery.unpaired_files[:5]) + (", ..." if len(delivery.unpaired_files) > 5 else "")
        + "), a name-pairing failure, not a missing download") if unpaired else ""
    if not missing and not extra and not differing and not regrouped and analysed == 0 and not free:
        reasons = []
        if excluded:
            reasons.append(f"the input of {len(excluded)} was excluded by the campaign disposition or the lease ("
                           + ", ".join(sorted(excluded)[:5]) + ")")
        if undelivered:
            reasons.append(f"{len(undelivered)} were never delivered: no input lineage row names them and nothing the "
                           "download delivered could be theirs (" + ", ".join(sorted(undelivered)[:5]) + ")")
        if unpaired:
            reasons.append(unpaired_sentence)
        report.add(
            "CLS-2", stage, "Executed Class is the Class that was approved", FAIL,
            f"None of the {len(approved)} approved sample(s) is analysed: " + "; ".join(reasons)
            + ", so the Console reads no grouping at all.", **evidence)
        return
    if not missing and not extra and not differing and not regrouped:
        if not excluded and not folded and not undelivered and not unpaired and not free_sentence:
            detail = f"All {len(approved)} approved assignments appear in the analysis CSV with the same Class."
        else:
            detail = (f"All {analysed} approved assignments of the samples analysed appear in "
                      "the analysis CSV with the same Class")
            if folded:
                detail += (f", {len(folded)} Class label(s) written as the Console reads them ("
                           + ", ".join(f"{label!r} as {alias!r}" for label, alias in sorted(folded.items())[:3]) + ")")
            if excluded:
                detail += (f"; {len(excluded)} approved sample(s) are not analysed, their input excluded by the "
                           "campaign disposition or the lease (" + ", ".join(sorted(excluded)[:5]) + ")")
            if undelivered:
                detail += (f"; {len(undelivered)} approved sample(s) were never delivered: no input lineage row "
                           "names them and nothing the download delivered could be theirs ("
                           + ", ".join(sorted(undelivered)[:5]) + (", ..." if len(undelivered) > 5 else "") + ")")
            if unpaired:
                detail += "; " + unpaired_sentence
            if free_sentence:
                detail += "; " + free_sentence
            if undelivered or unpaired or without_attribution:
                detail += f", so the run covers {analysed} of the {len(approved)} approved samples by attribution"
            detail += "."
        report.add("CLS-2", stage, "Executed Class is the Class that was approved",
                   FAIL if unpaired else WARN if undelivered or free_sentence else PASS, detail, **evidence)
        return
    report.add(
        "CLS-2", stage, "Executed Class is the Class that was approved", FAIL,
        "The grouping the Console will read is not the grouping that was approved. "
        f"{len(differing)} sample(s) carry a different Class, {len(missing)} approved sample(s) "
        f"are absent from the CSV, {len(extra)} CSV row(s) were never approved"
        + (f", and {len(regrouped)} Class(es) were merged or split on the way" if regrouped else "") + "."
        + (f" {len(undelivered)} further approved sample(s) were never delivered (undelivered_samples)."
           if undelivered else "")
        + (f" {len(unpaired)} further approved sample(s) were delivered and left unpaired "
           "(delivered_unpaired_samples)." if unpaired else "")
        + (f" {free_sentence[:1].upper() + free_sentence[1:]}." if free_sentence else ""),
        differing=differing[:10], missing=missing[:10], unapproved=extra[:10],
        **({"regrouped": regrouped[:10]} if regrouped else {}), **evidence,
    )


def check_class_proposal_was_accepted(report: Report, provenance: dict | None, reason: str) -> None:
    """CLS-3. A grouping was ratified, not merely suggested.

    The contract requires an explicit user confirmation before a Class proposal is saved. The
    confirmation is given in a conversation; what a later audit can read is the proposal's own
    status. A proposal still reading "proposed" beside an executed, published run says the
    ratification happened somewhere no artifact records -- which, for a machine-authored grouping,
    is the whole of the safety argument.

    Archive members the lease included unattributed (user decision, 2026-10-07; _unattributed_members) run in
    the abstention Class or in "Unattributed", which no assignment of the ratified record gives them: a ratified
    decision is then a WARN that lists them with manifest.unattributed_members.count, never a PASS
    (_unattributed_beside_the_decision).

    RUN POLICY: record_only, as the user named it (2026-10-01), as for CLS-1.
    """
    before = len(report.checks)
    try:
        stage = "before-production"
        proposal = _class_proposal(provenance)
        if proposal is None:
            report.add("CLS-3", stage, "The executed grouping was ratified", NOT_EVALUABLE,
                       reason or "The manifest carries no Class proposal.", required=False)
            return
        status = str(proposal.get("status") or "").strip().casefold()
        model = str(proposal.get("model") or "")
        warnings = [str(item) for item in proposal.get("warnings") or []]
        contrast = proposal.get("contrast_definition") if isinstance(proposal.get("contrast_definition"), dict) else {}
        # A Class record saved for another unit settles nothing here, however it was ratified. A split
        # part carries its parent's record, so the parent's id is this unit's too.
        record_unit = str(proposal.get("unit_id") or "")
        own_unit = str(((provenance or {}).get("project") or {}).get("analysis_unit_id") or "")
        split_from = (provenance or {}).get("split_from")
        parent_unit = str(split_from.get("analysis_unit_id") or "") if isinstance(split_from, dict) else ""
        if record_unit and own_unit and record_unit not in {own_unit, parent_unit}:
            report.add("CLS-3", stage, "The executed grouping was ratified", FAIL,
                       f"The Class record was saved for unit {record_unit}, not for this unit ({own_unit}"
                       + (f", split from {parent_unit}" if parent_unit else "") + "): it settles nothing here.",
                       status=status, record_unit=record_unit, analysis_unit_id=own_unit, parent_unit=parent_unit)
            return
        if contrast.get("kind") == "abstention":
            # Where the Catalog abstains, the decision is that no Class is defined: saved, and ratified,
            # like a proposal (decided 2026-09-28), as every sample in one Class with no field selected.
            assignments = [item for item in proposal.get("assignments") or [] if isinstance(item, dict)]
            labels = sorted({str(item.get("class_label") or "") for item in assignments})
            fields = list(proposal.get("selected_fields") or [])
            problems = []
            if fields:
                problems.append(f"it selects {fields}")
            if len(labels) != 1:
                problems.append(f"it gives {len(labels)} Classes, not one" if assignments else "it assigns no sample")
            elif labels[0] != str(contrast.get("class_label") or ""):
                problems.append(f"its one Class {labels[0]!r} is not the Class it names "
                                f"({str(contrast.get('class_label') or '') or 'none'!r})")
            if problems:
                report.add("CLS-3", stage, "The executed grouping was ratified", FAIL,
                           "The record says no Class was defined, but " + " and ".join(problems)
                           + ": an abstention that groups the samples is a grouping nobody proposed.",
                           status=status, abstention=True, labels=labels[:5], selected_fields=fields)
                return
            reason = str(contrast.get("reason") or "")
            if status in RATIFIED_STATUSES and reason not in ABSTENTION_REASONS:
                # Ratified, but not for a reason the Catalog's selection gives: the decision stands and
                # the record does not say why no declared factor could be used.
                report.add("CLS-3", stage, "The executed grouping was ratified", WARN,
                           f"The Class decision is a ratified abstention, but its reason "
                           f"({reason or 'none recorded'!r}) is not one the Catalog's selection gives "
                           f"({', '.join(ABSTENTION_REASONS)}). Every sample is in Class {labels[0]!r}.",
                           status=status, abstention=True, reason=reason, model=model, warnings=warnings[:4])
                return
            if status in RATIFIED_STATUSES:
                report.add("CLS-3", stage, "The executed grouping was ratified", PASS,
                           f"The Class decision is a ratified abstention ({reason}): "
                           f"no contrast, every sample in Class {labels[0]!r}. Status is {status!r}.",
                           status=status, abstention=True, reason=reason, model=model,
                           warnings=warnings[:4])
                return
        if status in RATIFIED_STATUSES:
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
    finally:
        _unattributed_beside_the_decision(report, before, provenance)


def _unattributed_beside_the_decision(report: Report, before: int, provenance: "dict | None") -> None:
    """Make the PASS CLS-3 just added a WARN where unattributed archive members run beside the ratified decision."""
    check = report.checks[-1] if len(report.checks) > before else None
    unattributed = _unattributed_members(provenance)
    if check is not None and check.status == PASS and unattributed and _class_proposal(provenance) is not None:
        check.status = WARN
        check.detail += " " + unattributed.sentence() + ", which no assignment of the ratified record covers."
        check.evidence.update(unattributed.evidence())


CHECKSUM_CLAIM = re.compile(
    r"checksums?[- ]?(?:were\s+|was\s+|are\s+|is\s+|have\s+been\s+|has\s+been\s+)?"
    r"(?:verified|validated|confirmed|matched|checked)"
    r"|(?:verified|validated|confirmed|checked)\s+(?:by|against|with|using)\s+[^.;]{0,60}?checksums?"
    r"|verified\s+(?:the\s+|their\s+|its\s+|all\s+)?(?:md5\s+|sha-?256\s+)?checksums?"
    r"|checksum\s+(?:verification|validation|comparison|check)"
    r"|integrity[- ](?:was\s+|were\s+|is\s+|has\s+been\s+)?(?:verified|confirmed|validated|checked)"
    r"|integrity\s+(?:was|were|is|has\s+been)\s+(?:verified|confirmed|validated|checked)"
    r"|(?:md5|sha-?256|sha-?1)[- ](?:verified|checked|validated)",
    re.IGNORECASE,
)
# A negation governs a phrase only within its own clause, and "no file failed verification" asserts
# that every file passed: a negation with a failure word is the claim, not its denial.
CLAIM_NEGATION = re.compile(r"\b(?:not|no|never|cannot|without|none|neither|nor)\b|could\s+not|n['\u2019]t", re.IGNORECASE)
# A phrase denied after it: "checksum verification was not performed", "checksum validation: not done".
NEGATED_AFTER = re.compile(
    r"^\s*(?::|(?:was|were|is|are|has\s+been|have\s+been))\s*(?:not|never)\s+"
    r"(?:performed|possible|done|carried\s+out|attempted|applicable|available)\b",
    re.IGNORECASE,
)
CLAUSE_BOUNDARY = re.compile(r"[,:]|\b(?:and|but|while|whereas|although|though|however|yet)\b", re.IGNORECASE)
FAILURE_WORD = re.compile(r"\bfail(?:ed|s|ure|ures)?\b", re.IGNORECASE)
# A sentence about the spectral library alone is the library identification the contract asks for.
LIBRARY_SUBJECT = re.compile(r"\b(?:msp|lbm2?|librar(?:y|ies)|zenodo)\b", re.IGNORECASE)
INPUT_SUBJECT = re.compile(
    r"\b(?:raw|input|inputs|data\s+files?|mzml|spectra\s+files?)\b"
    r"|\b(?:all|every|each)\s+(?:\w+\s+){0,2}(?:files?|downloads?|checksums?)\b",
    re.IGNORECASE,
)
# A sentence ends at . ; ! ? or a newline, but not at the point of a decimal such as 5.5.
SENTENCE = re.compile(r"(?:[^.;!?\n]|(?<=\d)\.(?=\d))+")
# The one way an artifact may describe inputs whose basis is archive_verified (ARCHIVE_WORDING_TEXT),
# with the checksum noun, a plural and the algorithm Interactive names (MD5, SHA-256 or SHA-1) allowed. It says what was compared, the archive, and not that the
# inputs themselves were. Where no archive's published MD5 was compared it is itself an unearned claim.
ARCHIVE_WORDING = re.compile(
    r"extracted\s+from\s+(?:(?:an|the)\s+)?archives?\s+whose\s+published\s+(?:md5|sha-?256|sha-?1)s?"
    r"(?:\s+checksums?)?\s+matched",
    re.IGNORECASE,
)


def _claim_is_negated(sentence: str, start: int, end: int | None = None) -> bool:
    if end is not None and NEGATED_AFTER.match(sentence[end:]):
        return True
    before = sentence[:start]
    boundaries = list(CLAUSE_BOUNDARY.finditer(before))
    clause = before[boundaries[-1].end():] if boundaries else before
    return bool(CLAIM_NEGATION.search(clause)) and not FAILURE_WORD.search(clause)
PUBLICATION_ARTIFACTS = (
    "MS_DIAL_publication_report.json", "MS_DIAL_Materials_and_Methods.txt", "MS_DIAL_QA_Results.txt",
    "Supplementary_Table_MS_DIAL.tsv", "MS_DIAL_publication_reporting_bundle.zip",
)


def check_no_unearned_checksum_claim(report: Report, provenance: dict | None, output: Path,
                                     csv_rows: list[dict] | None = None) -> None:
    """SUM-2. No published artifact calls inputs checksum-verified that were not.

    The second clause of the user's decision of 2026-09-25: a unit whose inputs rest on the sha256
    recorded at download proceeds past SUM-1 with a WARN, and no artifact may then describe its
    inputs as checksum-verified. SUM-1 can only say so; this is where the claim would be made.

    The same holds for inputs whose basis is archive_verified: only the archive they came out of was
    compared with a published MD5. For them, and only for them, the artifact may say they were
    "extracted from an archive whose published MD5 matched" (ARCHIVE_WORDING), which is neither counted
    nor set aside. Said of inputs that rest on a download sha256, where the repository published no
    checksum at all, the same wording is a claim.
    """
    stage = "before-publish"
    title = "No artifact calls unverified inputs checksum-verified"
    owner, owner_reason = _raw_owner_manifest(provenance)
    if owner is None:
        report.add("SUM-2", stage, title, NOT_EVALUABLE, owner_reason or "The manifest is absent.")
        return
    # The inputs SUM-1 judged: what an artifact says of the inputs is said of those analysed.
    kind, _detail, basis_evidence = _checksum_basis(owner, _not_analysed(provenance, owner, csv_rows))
    if kind not in ("download_sha256", "archive_verified"):
        report.add("SUM-2", stage, title, NOT_EVALUABLE,
                   "The inputs were checksum-verified, or SUM-1 refused them; there is no unearned "
                   "claim to look for.", required=False, basis=kind)
        return
    present = [output / name for name in PUBLICATION_ARTIFACTS if (output / name).is_file()]
    if not present:
        report.add("SUM-2", stage, title, NOT_EVALUABLE, "No publication artifact is present.")
        return
    archive = kind == "archive_verified"
    algorithms = basis_evidence.get("archive_algorithms") or []
    allowed = _archive_wording(algorithms)
    compared = " or ".join(algorithms) or "MD5"
    claims = []
    skipped = []
    permitted: list[str] = []
    to_read: list[dict] = []
    unread: list[str] = []
    for path in present:
        for member, text in _readable_members(path, unread):
            for sentence in SENTENCE.findall(text):
                spans = [wording.span() for wording in ARCHIVE_WORDING.finditer(sentence)]
                for _span in spans:
                    where = f"{path.name}:{member}: {sentence.strip()[:160]!r}"
                    if archive:
                        permitted.append(where)
                    else:
                        claims.append(f"{where} (no archive's published MD5 was compared for these inputs)")
                for match in CHECKSUM_CLAIM.finditer(sentence):
                    if any(start <= match.start() and match.end() <= end for start, end in spans):
                        continue  # the archive wording, already judged whole
                    where = f"{path.name}:{member}: {sentence.strip()[:160]!r}"
                    if _claim_is_negated(sentence, match.start(), match.end()):
                        skipped.append(f"negated: {where}")
                    elif LIBRARY_SUBJECT.search(sentence) and not INPUT_SUBJECT.search(sentence):
                        skipped.append(f"about the library: {where}")
                    else:
                        claims.append(where)
                        continue
                    to_read.append({"source": f"{path.name}:{member}", "sentence": sentence.strip()})
    read_evidence = {**_to_read_evidence("SUM-2", to_read), "unread": unread[:20]}
    if archive:
        read_evidence.update(basis=kind, permitted_wording=permitted[:20], permitted_wording_count=len(permitted))
    if claims:
        report.add("SUM-2", stage, title, FAIL,
                   "A published artifact calls these inputs checksum-verified, but only the archive they were "
                   f"extracted from was compared with its published {compared}, and their own checksums were not. "
                   f"The permitted wording is '{allowed}'." if archive else
                   "A published artifact calls these inputs checksum-verified, but the repository "
                   "published no checksum and none was compared.",
                   claims=claims[:10], claim_count=len(claims),
                   not_counted=skipped[:50], not_counted_count=len(skipped), **read_evidence)
        return
    if skipped:
        # A phrasing matched and was set aside by a rule, not by a reader. That is for a person to
        # read, and the report says so instead of passing it.
        report.add("SUM-2", stage, title, WARN,
                   f"{len(skipped)} checksum-verification phrasing(s) matched and were not counted as "
                   "claims, as negations or statements about the library; read them: "
                   + " | ".join(skipped[:3]),
                   artifacts=len(present), not_counted=skipped[:50], not_counted_count=len(skipped),
                   **read_evidence)
        return
    if unread:
        report.add("SUM-2", stage, title, WARN,
                   f"No known checksum-verification phrasing matched, but {len(unread)} artifact member(s) could "
                   "not be read, so what they say is unknown: " + "; ".join(unread[:5]),
                   artifacts=len(present), **read_evidence)
        return
    report.add("SUM-2", stage, title, PASS,
               f"No known checksum-verification phrasing matched in {len(present)} publication "
               "artifact(s). This is not a statement that no such claim is made."
               + (f" {len(permitted)} sentence(s) use the permitted wording '{allowed}'."
                  if permitted else ""),
               artifacts=len(present), **read_evidence)


# ---- READ-1 -------------------------------------------------------------------------------------
# Decided by the user on 2026-09-28: a WARN a person must read holds a run from counting as completed
# until that person's reading is recorded. Those WARNs are SUM-2's matching sentences set aside by a
# rule and QA-1's sentences that are not Interactive's own; each check hands over the sentences, and
# a digest of them, as to_read. The reading is recorded with scripts/record-reading.py in
# provenance/readings.json, against that digest, so a changed text is read again. WARNs that report
# a known condition (SUM-1, MTH-1 and QA-1's other notes) need no reading.
READINGS_FILE = "readings.json"
READINGS_SCHEMA = "msdial-public-reanalysis-readings.v1"
READ_CHECKS = ("QA-1", "SUM-2")
READ_CONCLUSIONS = ("accepted", "rejected")


def _reading_digest(check_id: str, to_read: list[dict]) -> str:
    items = sorted([str(item.get("source", "")), re.sub(r"\s+", " ", str(item.get("sentence", ""))).strip()]
                   for item in to_read)
    payload = json.dumps([check_id, items], ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _to_read_evidence(check_id: str, to_read: list[dict]) -> dict:
    return {"to_read": to_read, "to_read_count": len(to_read),
            "to_read_digest": _reading_digest(check_id, to_read) if to_read else ""}


def _read_readings(workspace: Path) -> "tuple[list[dict], list[str]]":
    """The recorded readings, and every reason some of them cannot be trusted.

    An entry is trusted only as the recorder writes it: a known check, a sha256 digest, a
    conclusion of accepted or rejected, a reader's name and a time. A repeated key anywhere, or an
    entry of another shape, makes the file untrusted for the checks it concerns.
    """
    path = workspace / "provenance" / READINGS_FILE
    if not path.exists():
        return [], []
    record, reason = _read_json(path)
    if record is None:
        return [], [reason]
    if _qa_duplicate_keys(path):
        return [], [f"{READINGS_FILE} repeats a key, so which value it means cannot be told"]
    readings = record.get("readings")
    if record.get("schema") != READINGS_SCHEMA or not isinstance(readings, list):
        return [], [f"{READINGS_FILE} is not a {READINGS_SCHEMA} record"]
    kept, problems = [], []
    for index, item in enumerate(readings, start=1):
        problem = _reading_problem(item)
        if problem:
            problems.append(f"entry {index} of {READINGS_FILE} {problem}")
        else:
            kept.append(item)
    return kept, problems


def _reading_problem(item) -> str:
    if not isinstance(item, dict):
        return "is not an object"
    if item.get("check_id") not in READ_CHECKS:
        return f"names no check QA-1 or SUM-2 ({item.get('check_id')!r})"
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item.get("digest") or "")):
        return "has no sha256 digest"
    if item.get("conclusion") not in READ_CONCLUSIONS:
        return f"concludes {item.get('conclusion')!r}, not accepted or rejected"
    if not isinstance(item.get("read_by"), str) or not item["read_by"].strip():
        return "names no reader"
    try:
        datetime.fromisoformat(str(item.get("read_at") or ""))
    except ValueError:
        return "has no time it was read"
    return ""


def check_readings_recorded(report: Report, workspace: Path, stage: str) -> None:
    """READ-1. Every sentence a person must read has been read, and the reading is recorded.

    PASS when nothing is to be read, or when each such check's current sentences carry a reading
    whose latest conclusion is 'accepted'. FAIL when the latest reading of them concluded 'rejected':
    a person established that the text is wrong, until it is corrected or a later reading of the
    same sentences accepts it. NOT_EVALUABLE, required, so that --strict holds the run, when
    sentences are to be read and no trusted reading of exactly them is recorded, or when QA-1 or
    SUM-2 could not read a text; NOT_EVALUABLE, not required, when QA-1 could not be evaluated.
    """
    title = "Every sentence a person must read has been read"
    checks = {check.check_id: check for check in report.checks if check.check_id in READ_CHECKS}
    pending = {check_id: check for check_id, check in checks.items() if check.evidence.get("to_read_count")}
    unreadable = {check_id: list(check.evidence.get("unread_texts") or check.evidence.get("unread") or [])
                  for check_id, check in checks.items()}
    unreadable = {check_id: items for check_id, items in unreadable.items() if items}
    # SUM-2 not evaluable has nothing to set aside; QA-1 not evaluable read no text, so what is to be
    # read is unknown.
    unjudged = ["QA-1"] if "QA-1" in checks and checks["QA-1"].status == NOT_EVALUABLE else []
    readings, problems = _read_readings(workspace)
    missing, rejected, accepted, stale = [], [], [], []
    for check_id, check in sorted(pending.items()):
        digest = check.evidence["to_read_digest"]
        count = check.evidence["to_read_count"]
        matching = [item for item in readings if item["check_id"] == check_id and item["digest"] == digest]
        if any(item["check_id"] == check_id and item["digest"] != digest for item in readings):
            stale.append(check_id)
        if not matching:
            missing.append(f"{check_id} ({count} sentence(s), digest {digest[:19]}...)")
            continue
        latest = matching[-1]
        entry = f"{check_id}: {count} sentence(s) read by {latest['read_by']} on {latest['read_at']}"
        if latest["conclusion"] == "rejected":
            rejected.append(entry + (f", who found them wrong: {_qa_short(latest.get('note') or '', 160)}"
                                     if latest.get("note") else ", who found them wrong"))
        else:
            accepted.append(entry)
    evidence = {"pending": sorted(pending), "readings_file": str(Path("provenance") / READINGS_FILE),
                "accepted": accepted, "rejected": rejected, "missing": missing, "unreadable": unreadable,
                "unjudged": unjudged, "problems": problems[:20],
                "stale": [f"{check_id}: a reading is recorded for other sentences" for check_id in stale]}
    if rejected:
        report.add("READ-1", stage, title, FAIL,
                   "A person read the sentences and found them wrong, so the text must be corrected before "
                   "publication: " + " | ".join(rejected), **evidence)
        return
    if unreadable:
        report.add("READ-1", stage, title, NOT_EVALUABLE,
                   "A text could not be read, by the gate or so by anyone reading what it quotes: "
                   + "; ".join(f"{check_id}: {', '.join(_qa_short(item, 100) for item in items[:3])}"
                               for check_id, items in sorted(unreadable.items()))
                   + ". Make it readable, or remove it, and run the gate again.", **evidence)
        return
    if missing or (pending and problems):
        report.add("READ-1", stage, title, NOT_EVALUABLE,
                   ("Sentences are for a person to read, and no reading of exactly them is recorded: "
                    + "; ".join(missing) + ". " if missing else "")
                   + ("The readings file cannot be trusted: " + "; ".join(problems[:3]) + ". " if problems else "")
                   + "Show the sentences to the person, and once they have said what they found, record it with "
                   "scripts/record-reading.py.", **evidence)
        return
    if unjudged:
        report.add("READ-1", stage, title, NOT_EVALUABLE,
                   "QA-1 could not be evaluated, so what is to be read is unknown."
                   + (" Read and accepted: " + " | ".join(accepted) if accepted else ""), required=False, **evidence)
        return
    if accepted:
        report.add("READ-1", stage, title, PASS, "Read and accepted: " + " | ".join(accepted), **evidence)
        return
    judged = ", ".join(f"{check_id} ({checks[check_id].status})" for check_id in sorted(checks)) or "neither check ran"
    report.add("READ-1", stage, title, PASS,
               f"No sentence is for a person to read: {judged} left none.", **evidence)


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
    if status in VALIDATED_STATUSES and finalized and isinstance(validation, dict):
        report.add("FIN-1", stage, "The unit reached a recorded terminal state", PASS,
                   f"status={status!r}, finalized at {finalized}.", status=status,
                   finalized_at=str(finalized))
        return
    if status == "run_failed":
        report.add("FIN-1", stage, "The unit reached a recorded terminal state", FAIL,
                   "The unit recorded a failed run. Nothing here should be published.",
                   status=status, run_failures=len(provenance.get("run_failures") or []))
        return
    if status == "validation_failed":
        report.add("FIN-1", stage, "The unit reached a recorded terminal state", FAIL,
                   "The unit was finalised and its mzTab-M failed validation. Nothing here should be "
                   "published.", status=status, finalized_at=str(finalized))
        return
    if output is not None and not _production_started(output, provenance):
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


# What Interactive's run finalisation (0.5.15, run_finalisation.py) records a step it could not do as:
# one entry of the unit manifest's finalisation_holds, naming what the step's absence makes unsafe.
FINALISATION_HOLDS = "finalisation_holds"
BLOCKS_SHARING = "sharing"
BLOCKS_RAW_DELETION = "raw_deletion"
FIN2_TITLE = "No finalisation hold stands"


def check_no_finalisation_hold_stands(report: Report, provenance: dict | None, reason: str) -> None:
    """FIN-2. Nothing a run's finalisation left undone stands against sharing or raw deletion.

    After the Console returns, Interactive rewrites the mzTab-M so it names no location of this
    machine, moves a campaign unit's MS-DIAL containers out of the raw tree, and deletes the loaded-
    library copy (*_Loaded.msp2.dbs). A step another process's lock defeats is kept as a hold until a
    retry succeeds. A hold that blocks sharing (an mzTab-M still naming a location, a library copy still
    in the output, a finalisation that stopped) means a shared artifact may still carry a private
    location: a FAIL, here at publication. One that blocks only raw deletion (containers still beside
    the inputs) is a WARN; the raw cleanup refuses it on its own.

    A manifest without the record was written before the finalisation existed, or by a run that has not
    finalised: not evaluable, and not required. SEC-1 reads the shared artifacts themselves.
    """
    stage = "before-publish"
    if provenance is None:
        report.add("FIN-2", stage, FIN2_TITLE, NOT_EVALUABLE, reason, required=False)
        return
    holds = provenance.get(FINALISATION_HOLDS)
    if holds is None:
        report.add("FIN-2", stage, FIN2_TITLE, NOT_EVALUABLE,
                   f"The manifest records no {FINALISATION_HOLDS}: it predates Interactive's run finalisation "
                   "(0.5.15), or no production run has finalised here.", required=False)
        return
    resolved = provenance.get("finalisation_hold_resolutions")
    evidence = {"holds": len(holds) if isinstance(holds, list) else None,
                "resolved_on_retry": len(resolved) if isinstance(resolved, list) else 0}
    if not isinstance(holds, list):
        report.add("FIN-2", stage, FIN2_TITLE, NOT_EVALUABLE,
                   f"{FINALISATION_HOLDS} is not a list, so it is not established that nothing blocks sharing.",
                   **evidence)
        return

    def described(item: dict) -> str:
        return (f"{item.get('step') or 'an unnamed step'} of run {item.get('job_id') or 'unrecorded'} "
                f"({item.get('reason') or 'no reason recorded'})")

    readable = [item for item in holds if isinstance(item, dict) and isinstance(item.get("blocks"), list)]
    unreadable = len(holds) - len(readable)
    sharing = [item for item in readable if BLOCKS_SHARING in item["blocks"]]
    raw = [item for item in readable if item not in sharing and BLOCKS_RAW_DELETION in item["blocks"]]
    other = [item for item in readable if item not in sharing and item not in raw]
    evidence.update({"sharing": [described(item) for item in sharing[:10]],
                     "raw_deletion": [described(item) for item in raw[:10]],
                     "other": [described(item) for item in other[:10]], "unreadable": unreadable})
    if sharing:
        report.add("FIN-2", stage, FIN2_TITLE, FAIL,
                   f"{len(sharing)} finalisation hold(s) block sharing: {'; '.join(evidence['sharing'][:3])}. A shared "
                   "artifact of this run may still carry a location of this machine or a copy of a private library; "
                   "nothing here may be shared until a retry clears the hold.", **evidence)
        return
    if unreadable:
        report.add("FIN-2", stage, FIN2_TITLE, NOT_EVALUABLE,
                   f"{unreadable} of the {len(holds)} finalisation hold(s) do not say what they block, so it is not "
                   "established that nothing blocks sharing.", **evidence)
        return
    if raw or other:
        report.add("FIN-2", stage, FIN2_TITLE, WARN,
                   (f"{len(raw)} finalisation hold(s) block raw deletion: {'; '.join(evidence['raw_deletion'][:3])}. "
                    "Deleting the raw tree would delete MS-DIAL's containers with it; the cleanup refuses until a "
                    "retry moves them." if raw else "")
                   + (" " if raw and other else "")
                   + (f"{len(other)} hold(s) block something this gate does not know: "
                      f"{'; '.join(evidence['other'][:3])}." if other else ""), **evidence)
        return
    report.add("FIN-2", stage, FIN2_TITLE, PASS,
               "No finalisation hold stands"
               + (f"; {evidence['resolved_on_retry']} earlier hold(s) were resolved on retry." if evidence["resolved_on_retry"]
                  else "."), **evidence)


def _console_field(line: str) -> "tuple[str, str] | None":
    """(key, value) as the Console's readFieldValues reads a method-file line, or None.

    Nothing for a blank or '#' line or one without a separator; otherwise the key before the first
    ':' or '=', trimmed and case-folded, and the value after it, trimmed, with one pair of enclosing
    quotes removed.
    """
    if len(line) < 2 or line.lstrip().startswith("#"):
        return None
    separators = [index for index in (line.find(":"), line.find("=")) if index >= 0]
    if not separators:
        return None
    at = min(separators)
    value = line[at + 1:].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return line[:at].strip().casefold(), value


def _console_count(text: str) -> "int | None":
    """The whole number the Console's Count arm accepts, or None: an integer, or a real that is one."""
    try:
        return int(text)
    except ValueError:
        pass
    try:
        real = float(text)
    except ValueError:
        return None
    return int(round(real)) if abs(real - round(real)) < 1e-9 else None


def _method_threshold(output: Path) -> "tuple[str | None, str, list[str]]":
    """The Minimum peak height the Console applies from method.txt, why none, and every value stated.

    As the Console reads it: every non-blank value of the key is stated, and the last one its Count
    arm can read is applied (MsdialWorkbench ConfigParser; every reader is last-wins since #817).
    When none can be read the last stated value is returned, for PKH-1 to name.
    """
    method = output / "method.txt"
    if not method.is_file():
        return None, f"{method.name} is absent", []
    try:
        text = method.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as error:
        return None, str(error), []
    stated = []
    for line in text.splitlines():
        field = _console_field(line)
        if field and field[0] == "minimum peak height" and field[1]:
            stated.append(field[1])
    if not stated:
        return None, "method.txt states no Minimum peak height", []
    usable = [value for value in stated if _console_count(value) is not None]
    return (usable or stated)[-1], "", stated


# The step rule for the peak-height threshold, as the user decided it on 2026-10-06: search in the
# instrument-family step (100 for QTOF-type data, 1,000 for Fourier-transform data). Only when the
# zero-threshold count is above the target's upper bound and no threshold at that step lands in the
# target range, search again at a step ten times finer (10, or 100), and never finer than that. The
# fallback is recorded, and so is a fine step that still misses the range. Interactive's estimate
# records it (0.5.28): threshold_step is the step the threshold was taken at, coarse_threshold_step the
# instrument-family step, step_fallback whether the finer step was used and fallback_reason why, and
# within_target_range whether the estimated count is in the target. A diagnostic recorded before then
# carries threshold_step and within_target_range alone, and is read as it is.
FINE_STEP_DIVISOR = 10
# The absolute floor of the step (the user's rule: never finer than 10 for QTOF-type data, 100 for Fourier-
# transform data), and the instrument-family steps it is a tenth of. Judged against the unit's instrument,
# never against the family step the diagnostic records: an estimate asked for at step 10 records 10 as its
# family step and falls back to 1, at the noise floor.
FAMILY_STEPS = {"qtof": 100, "fourier": 1000}
STEP_FLOORS = {"qtof": 10, "fourier": 100}
# Fourier-transform analysers in the Catalog's instrument text, read with the instrument tokens of Interactive
# 0.5.28's own classifier (workflow.instrument_family_from_text: _ORBITRAP_INSTRUMENT, _FT_ICR_INSTRUMENT and
# _FOURIER_GENERIC, msdial-interactive-app#61), so the gate calls a declared instrument Fourier-transform
# exactly where Interactive does. The word boundaries keep HPLC column names out: "Zorbax Eclipse Plus C18"
# and "Synergi Fusion-RP" are no Orbitrap Eclipse or Fusion, and a bare "LTQ" or "Velos" is an ion trap. The
# campaign runner reads the same tokens (scripts/campaign/policy.py _FOURIER_INSTRUMENT; tests hold the
# two equal). Read only where the diagnostic records no family from the file itself (_step_family): before
# 0.5.28 Interactive labelled every mzML QTOF, and since then it reads the mzML header and lets a declared
# instrument decide only over a format default.
_ORBITRAP_INSTRUMENT = (
    r"orbitrap|exactive|exploris|astral|\blumos\b|\bascend\b|tribrid|\bid-x\b|"
    r"\bfusion\b(?![\s-]*rp)|(?<!zorbax )\beclipse\b(?![\s-]*(?:plus|xdb|c18|c8))"
)
_FT_ICR_INSTRUMENT = r"ft[\s-]?icr|fticr|cyclotron|solarix|scimax|mrms\b|\bapex(?![a-z])|\bltq[\s-]?ft(?![a-z])"
_FOURIER_GENERIC = r"\bftms\b|fourier"
FOURIER_INSTRUMENT = re.compile(
    "|".join((_ORBITRAP_INSTRUMENT, _FT_ICR_INSTRUMENT, _FOURIER_GENERIC)), re.IGNORECASE)
# Where Interactive (0.5.28, workflow.detect_raw_format) took a file's instrument family from the file itself:
# its vendor format, or its mzML header. A family from these is evidence about the file, and a repository's
# declared instrument does not overrule it; "format_default" (an mzML whose header names no instrument, a
# Bruker or unrecognised .d) is no such evidence.
FILE_FAMILY_SOURCES = ("vendor_format", "mzml_instrument_configuration")
PKH1_TITLE = "The threshold was measured on this unit"
STEP_RULE_FIELDS = ("coarse_threshold_step", "step_fallback", "fallback_reason")
# What a production run actually kept (the user's decision of 2026-10-06), as Interactive records it after
# each repository run: production_peak_counts, one record per run, appended (msdial-production-peak-counts.v1,
# Interactive 0.5.28, msdial-interactive-app#61). Read where present, and only recorded: the estimate is read
# off one file's zero-threshold diagnostic, and no rule yet says what a production count must be.
PRODUCTION_PEAK_COUNTS = "production_peak_counts"
PRODUCTION_PEAK_COUNT_FIELDS = (
    "job_id", "run_complete", "minimum_peak_height", "diagnostic_matches_applied_threshold", "estimated_peak_count",
    "representative_file_name", "representative_peak_count", "representative_to_estimate_ratio",
    "representative_within_target_range", "file_count", "files_counted", "peak_count_min", "peak_count_median",
    "peak_count_max", "peak_count_total",
)


def _production_peak_counts(provenance: dict, threshold: "float | None") -> "dict | None":
    """The latest production_peak_counts record of a run at this threshold, else the latest, with its fields
    as recorded (PRODUCTION_PEAK_COUNT_FIELDS) and whether its threshold is the one method.txt asks for."""
    records = [item for item in provenance.get(PRODUCTION_PEAK_COUNTS) or [] if isinstance(item, dict)]
    if not records:
        return None
    matching = [item for item in records if threshold is not None
                and _as_number(item.get("minimum_peak_height")) == threshold]
    record = (matching or records)[-1]
    summary = {name: record.get(name) for name in PRODUCTION_PEAK_COUNT_FIELDS if name in record}
    summary["records"] = len(records)
    summary["matches_method_threshold"] = bool(matching)
    return summary


def _diagnostic_field(item: dict, name: str) -> object:
    """A field of a recorded diagnostic: as its estimate recorded it (every field the estimator produced,
    unedited), else as the record copied it beside the estimate."""
    estimate = item.get("estimate") if isinstance(item.get("estimate"), dict) else {}
    value = estimate.get(name)
    return value if value is not None else item.get(name)


def _as_number(value: object) -> "float | None":
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _figure(value: "float | None") -> str:
    if value is None:
        return "?"
    return f"{int(value):,}" if value == int(value) else f"{value:,}"


def _is_fourier_family(named: object) -> bool:
    family = str(named or "").casefold()
    return "fourier" in family or "ft-icr" in family or "fticr" in family


def _recorded_family(item: dict) -> tuple[str, str]:
    """(family, source): the instrument family a diagnostic records, its representative's first, and what
    Interactive took it from ("" for either where nothing is recorded, as before 0.5.28 for the source)."""
    representative = item.get("representative") if isinstance(item.get("representative"), dict) else {}
    family = next((str(named).strip() for named in (representative.get("instrument_family"),
                                                    _diagnostic_field(item, "instrument_family"))
                   if str(named or "").strip()), "")
    source = str(representative.get("instrument_family_source")
                 or _diagnostic_field(item, "instrument_family_source") or "").strip()
    return family, source


def _step_family(item: dict, provenance: dict | None) -> tuple[str, str]:
    """("fourier" or "qtof", why): the unit's instrument family for the step floor, read as Interactive reads
    it (agent_workflow.representative_instrument_family, 0.5.28): the file first, the Catalog only where the
    file says nothing.

    1. Fourier-transform where the family the diagnostic recorded is (its representative's, or the record's).
    2. The family the diagnostic recorded, where Interactive recorded it from the file itself
       (instrument_family_source vendor_format or mzml_instrument_configuration: a SCIEX .wiff, a Waters
       .raw folder, an mzML header naming a TOF). The Catalog's instrument text does not overrule it, as it
       does not in Interactive: a multi-platform study's "Q Exactive; TripleTOF 6600" makes no .wiff FT data.
    3. Else (no family recorded; a format default, an mzML whose header names no instrument; or a record
       from before 0.5.28, which recorded no source and labelled every mzML QTOF), Fourier-transform where
       the Catalog's instrument text names a Fourier-transform analyser, or where the diagnostic searched at
       the Fourier-transform family step of 1,000; else QTOF-type."""
    representative = item.get("representative") if isinstance(item.get("representative"), dict) else {}
    recorded = [named for named in (representative.get("instrument_family"), _diagnostic_field(item, "instrument_family"))
                if str(named or "").strip()]
    source = str(representative.get("instrument_family_source")
                 or _diagnostic_field(item, "instrument_family_source") or "").strip()
    for named in recorded:
        family = str(named).casefold()
        if "fourier" in family or "ft-icr" in family or "fticr" in family:
            return "fourier", f"the diagnostic's instrument family is {named}" + (f", from {source}" if source else "")
    project = (provenance or {}).get("project") if isinstance((provenance or {}).get("project"), dict) else {}
    handoff = ((project.get("repository_metadata") or {}).get("catalog_handoff") or {}) if isinstance(
        project.get("repository_metadata"), dict) else {}
    settings = handoff.get("technical_settings") if isinstance(handoff, dict) else None
    instrument = str((settings or {}).get("instrument") or "") if isinstance(settings, dict) else ""
    if recorded and source in FILE_FAMILY_SOURCES and str(recorded[0]).strip().casefold() != "unknown":
        why = f"the diagnostic's instrument family is {recorded[0]}, from {source}"
        if FOURIER_INSTRUMENT.search(instrument):
            why += f", which the unit's declared instrument ({instrument}) does not overrule"
        return "qtof", why
    if FOURIER_INSTRUMENT.search(instrument):
        return "fourier", f"the unit's instrument is {instrument}"
    coarse = _as_number(_diagnostic_field(item, "coarse_threshold_step"))
    step = _as_number(_diagnostic_field(item, "threshold_step"))
    if (coarse if coarse is not None else step) == FAMILY_STEPS["fourier"]:
        return "fourier", "the diagnostic searched at the Fourier-transform step of 1,000"
    return "qtof", "no Fourier-transform analyser is recorded"


def _step_rule(item: dict, provenance: dict | None = None) -> dict:
    """What a diagnostic records of the step rule (2026-10-06), and what it breaks or leaves to be read.

    Returns the recorded fields ("evidence"), the words that describe them ("said"), the departures
    from the rule ("broken": a fallback to another step than the finer one, a fallback where the count
    needed no threshold, a step other than the family's with no fallback recorded) and what a person
    should read ("notes": a fallback that records no reason, an estimate outside the target range where
    the zero-threshold count was above it). A diagnostic recorded before the rule carries none of its
    fields, and only its step and within_target_range are read.

    THE FLOOR is absolute (_step_family): a step finer than 10 for QTOF-type data or 100 for
    Fourier-transform data breaks the rule, whatever family step the diagnostic records; and a recorded
    family step other than 100 or 1,000 is no family step.
    """
    step = _as_number(_diagnostic_field(item, "threshold_step"))
    coarse = _as_number(_diagnostic_field(item, "coarse_threshold_step"))
    fallback = _diagnostic_field(item, "step_fallback")
    reason = str(_diagnostic_field(item, "fallback_reason") or "").strip()
    within = _diagnostic_field(item, "within_target_range")
    count = _as_number(_diagnostic_field(item, "diagnostic_peak_count"))
    estimated = _as_number(_diagnostic_field(item, "estimated_peak_count"))
    lower = _as_number(_diagnostic_field(item, "target_peak_count_min"))
    upper = _as_number(_diagnostic_field(item, "target_peak_count_max"))
    recorded = any(_diagnostic_field(item, name) is not None for name in STEP_RULE_FIELDS)
    target = f"{_figure(lower)}-{_figure(upper)}" if lower is not None and upper is not None else "the target range"
    evidence: dict = {"step_rule_recorded": recorded, "threshold_step": _diagnostic_field(item, "threshold_step")}
    if recorded:
        evidence.update({"coarse_threshold_step": _diagnostic_field(item, "coarse_threshold_step"),
                         "step_fallback": fallback, "fallback_reason": reason})
    if within is not None:
        evidence["within_target_range"] = within
    for name in ("selection_rule", "fine_threshold_step", "instrument_family_source"):
        if _diagnostic_field(item, name) is not None:
            evidence[name] = _diagnostic_field(item, name)
    if estimated is not None:
        evidence["estimated_peak_count"] = _diagnostic_field(item, "estimated_peak_count")
    broken: list[str] = []
    notes: list[str] = []
    family, family_why = _step_family(item, provenance)
    floor = STEP_FLOORS[family]
    evidence.update({"instrument_family_for_step": family, "step_floor": floor})
    family_name = "Fourier-transform" if family == "fourier" else "QTOF-type"
    if step is not None and step < floor - 1e-9:
        broken.append(f"the threshold was taken at step {_figure(step)}, finer than the floor of {floor} for "
                      f"{family_name} data ({family_why}), which no recorded family step lowers")
    if recorded and coarse is not None and coarse not in FAMILY_STEPS.values():
        broken.append(f"the instrument-family step recorded is {_figure(coarse)}, which is neither 100 (QTOF-type) "
                      "nor 1,000 (Fourier-transform)")
    elif recorded and coarse is not None and coarse != FAMILY_STEPS[family]:
        note = (f"the diagnostic searched first at step {_figure(coarse)}, where {family_name} data "
                f"({family_why}) take {FAMILY_STEPS[family]:,}")
        own, source = _recorded_family(item)
        own_step = FAMILY_STEPS["fourier" if _is_fourier_family(own) else "qtof"]
        if own and source and coarse != own_step:
            # Interactive 0.5.28 (msdial-interactive-app#61) always searches first the step of the family it
            # records, and only records a requested step; the campaign runner asks for none. A record whose
            # search began elsewhere came from a build that searched a requested step, not from the rule.
            note += (f"; the diagnostic records the family {own}, from {source}, whose step is {own_step:,}, and "
                     "Interactive 0.5.28 always searches that step first and never a requested one, so this "
                     "search was not made by its rule")
        notes.append(note)
    if fallback is True:
        fine = coarse / FINE_STEP_DIVISOR if coarse else None
        said = (f"step {_figure(step)}, falling back from the instrument-family step {_figure(coarse)}"
                + (f" ({reason})" if reason else ""))
        if not coarse:
            broken.append("the diagnostic records a step fallback but not the instrument-family step it fell "
                          "back from")
        elif step is None or abs(step - fine) > 1e-9:
            broken.append(f"the fallback took step {_figure(step)}, where the rule's finer step for the "
                          f"instrument-family step {_figure(coarse)} is {_figure(fine)} and nothing finer")
        if count is not None and upper is not None and count <= upper:
            broken.append(f"the fallback was taken although the zero-threshold count ({_figure(count)}) is not "
                          f"above {_figure(upper)}, where the rule keeps the threshold at 0")
        if not reason:
            notes.append("the step fallback records no reason")
    else:
        said = f"step {_figure(step)}"
        if recorded and coarse is not None and step is not None and abs(step - coarse) > 1e-9:
            broken.append(f"the threshold was taken at step {_figure(step)}, not the instrument-family step "
                          f"{_figure(coarse)}, and no step fallback is recorded")
    if within is False and (count is None or upper is None or count > upper):
        if fallback is True:
            notes.append(f"even the finer step {_figure(step)} leaves the estimate ({_figure(estimated)} peaks) "
                         f"outside {target}, and the threshold nearest the range was taken")
        elif recorded:
            notes.append(f"the estimate ({_figure(estimated)} peaks) is outside {target} at step {_figure(step)}, "
                         "and no step fallback is recorded")
        else:
            notes.append(f"the estimate ({_figure(estimated)} peaks) is outside {target} at step {_figure(step)}; "
                         "the diagnostic predates the step fallback of 2026-10-06 and records no step rule")
    elif within is False:
        said += (f"; the {_figure(count)} peaks at zero threshold are below {target}, which the contract leaves "
                 "at threshold 0")
    return {"evidence": evidence, "said": said, "broken": broken, "notes": notes}


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

    THE STEP RULE (the user's decision of 2026-10-06, _step_rule) is read from the diagnostic that
    produced the threshold, the latest where several did. A fallback to the finer step that lands in
    the target range is the rule working, and passes with its reason stated. A fallback whose finer
    step still misses the range, an estimate outside the range at the family step, or a fallback that
    records no reason is a WARN, so a person reads it. A step the rule does not allow (a fallback to
    anything but the family step divided by ten, a fallback where the zero-threshold count needed no
    threshold, or another step than the family's with no fallback recorded) is a FAIL. A diagnostic
    recorded before the rule records none of its fields; its step and within_target_range are read as
    they are. The floor is absolute (_step_family): never finer than 10 for QTOF-type data or 100 for
    Fourier-transform data, whatever family step the diagnostic itself records, and a recorded family step
    is 100 or 1,000.

    WHAT THE PRODUCTION RUN KEPT (production_peak_counts, Interactive 0.5.28) is reported where recorded,
    against the estimate, and judged by nothing: the user asked that it be recorded (2026-10-06).

    RUN POLICY: record_only, as the user named it (2026-10-01). A threshold not measured here keeps
    more or fewer peaks; what it keeps is still this unit's data.
    """
    stage = "before-production"
    if provenance is None:
        report.add("PKH-1", stage, PKH1_TITLE, NOT_EVALUABLE, reason)
        return
    diagnostics = provenance.get("peak_height_diagnostics")
    written, written_reason, stated = _method_threshold(output)
    if not isinstance(diagnostics, list) or not diagnostics:
        report.add(
            "PKH-1", stage, PKH1_TITLE, FAIL,
            "No peak-count diagnostic is recorded for this unit. The contract requires one before "
            f"every production run, and method.txt asks for {written or 'an unstated threshold'}.",
            method_threshold=written,
        )
        return
    if written is None:
        report.add("PKH-1", stage, PKH1_TITLE, NOT_EVALUABLE, written_reason)
        return
    measured = []
    measured_by: list[tuple[float, dict]] = []
    for item in diagnostics:
        if isinstance(item, dict) and item.get("minimum_peak_height") is not None:
            try:
                value = float(item["minimum_peak_height"])
            except (TypeError, ValueError):
                continue
            measured.append(value)
            measured_by.append((value, item))
    try:
        executed = float(written)
    except ValueError:
        report.add("PKH-1", stage, PKH1_TITLE, FAIL,
                   f"method.txt states a Minimum peak height of {written!r}, which is not a number.",
                   method_threshold=written, measured=measured)
        return
    producing = [item for value, item in measured_by if abs(executed - value) < 1e-9]
    if producing:
        # The diagnostic that produced the threshold, the latest where several did: re-running it with
        # another step or representative is normal, and the step rule is read from the one that ran.
        latest = producing[-1]
        rule = _step_rule(latest, provenance)
        # Other stated values do not run, but a method file that asks for two thresholds was edited
        # by something that did not know which one the Console applies.
        differing = sorted({value for value in stated if value != written})
        status = FAIL if rule["broken"] else WARN if differing or rule["notes"] else PASS
        detail = (
            f"method.txt asks for {written}, which this unit's diagnostic measured "
            f"({latest.get('method', 'method unrecorded')}, "
            f"{latest.get('diagnostic_peak_count', '?')} peaks at zero threshold, {rule['said']})."
            + (f" It also states {', '.join(differing)}, which the Console does not apply: it applies the last "
               "value it can read." if differing else "")
        )
        if rule["broken"]:
            detail += " The step is not one the rule of 2026-10-06 allows: " + "; ".join(rule["broken"]) + "."
        if rule["notes"]:
            detail += " To be read: " + "; ".join(rule["notes"]) + "."
        production = _production_peak_counts(provenance, executed)
        if production is not None:
            kept = production.get("representative_peak_count")
            detail += (f" The production run{'' if production['matches_method_threshold'] else ' recorded at another threshold'} "
                       f"kept {kept if kept is not None else 'an unrecorded number of'} peaks in the representative "
                       f"file against the estimate of {production.get('estimated_peak_count', '?')}, and "
                       f"{production.get('peak_count_min', '?')}-{production.get('peak_count_max', '?')} across "
                       f"{production.get('files_counted', '?')} file(s) (recorded, not judged).")
        report.add(
            "PKH-1", stage, PKH1_TITLE, status, detail,
            **({"production_peak_counts": production} if production is not None else {}),
            method_threshold=written, measured=measured, stated=stated,
            representative=(latest.get("representative") or {}).get("file_name", ""),
            **rule["evidence"],
            **({"step_rule_broken": rule["broken"]} if rule["broken"] else {}),
            **({"step_rule_notes": rule["notes"]} if rule["notes"] else {}),
        )
        return
    report.add(
        "PKH-1", stage, PKH1_TITLE, FAIL,
        f"method.txt asks for a Minimum peak height of {written}, and no diagnostic on this unit "
        f"produced it. Measured here: {', '.join(str(value) for value in measured) or 'nothing'}."
        + (f" (It states {len(stated)} values; the Console applies the last it can read.)" if len(stated) > 1 else ""),
        method_threshold=written, measured=measured, stated=stated,
    )


def check_method_file_reached_the_console(report: Report, output: Path, stage: str,
                                          provenance: dict | None = None) -> None:
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
        failure = _recorded_failure(provenance)
        report.add("MTH-1", stage, "Every method-file parameter reached the Console", NOT_EVALUABLE,
                   (f"method.keys.json is absent. {failure} before the Console wrote it."
                    if failure else
                    "method.keys.json is absent: no production run has started here, or the Console "
                    "that ran predates the key record. Which parameters took effect cannot be read "
                    "from this workspace."))
        return
    parsed, reason = _read_json(record)
    if parsed is None:
        report.add("MTH-1", stage, "Every method-file parameter reached the Console", NOT_EVALUABLE,
                   reason)
        return
    # The record names the method file it read by checksum; a record of another method file says
    # nothing about this one.
    schema = parsed.get("schema")
    recorded_sha = str(parsed.get("method_file_sha256") or "").strip().casefold()
    method_path = output / "method.txt"
    if schema is not None and schema != "msdial-method-file-keys.v1":
        report.add("MTH-1", stage, "Every method-file parameter reached the Console", NOT_EVALUABLE,
                   f"method.keys.json has schema {schema!r}, not msdial-method-file-keys.v1.")
        return
    if recorded_sha and method_path.is_file():
        actual_sha = hashlib.sha256(method_path.read_bytes()).hexdigest()
        if actual_sha != recorded_sha:
            report.add("MTH-1", stage, "Every method-file parameter reached the Console", NOT_EVALUABLE,
                       "method.keys.json records another method file (sha256 "
                       f"{recorded_sha[:12]}...) than output/method.txt ({actual_sha[:12]}...), so it does not "
                       "say which of this file's parameters took effect.",
                       recorded_sha256=recorded_sha, method_sha256=actual_sha)
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


# What the user decided about raw data in a campaign (2026-09-30): they are deleted once every MS-DIAL
# output is present and the mzTab-M validates, whatever the gate's verdict; a unit that failed is retried
# twice and then deleted; a skipped or excluded unit is deleted too. Interactive deletes a unit's raw tree
# on one of two authorities only - a person's confirmed=true, or a campaign approval covering boundary 5,
# whose crossing it writes into campaign_authorizations before anything is deleted - and records the
# deletion as the status raw_cleaned (after a validated run) or discarded (without one). A split part owns
# no tree: its parent's is released once every part has ended, recorded as the parent's raw_release.
DELETE_RETENTION = "delete_after_validated_output"
RAW_DELETION_BOUNDARY = "5"
# The parent's record of that release (msdial-split-parent-raw-release.v1): its state, the parts it was
# decided for, and the authority it was made under.
SPLIT_RELEASE = "raw_release"
SPLIT_RELEASE_DELETED = "deleted"
# The campaign runner's own record of how a unit ended, beside its provenance and output.
CAMPAIGN_RECORD_FILE = "campaign-record.json"
CAMPAIGN_RECORD_SCHEMA = "msdial-campaign-unit-record.v1"
CAMPAIGN_ENDS_WITHOUT_OUTPUT = ("failed", "skipped", "excluded")
# The store's claim states that keep an object (download_store.LIVE_CLAIM_STATES), and how it names a
# unit's claim file (download_store._unit_file_name): readable for a Catalog-style id, hashed otherwise.
STORE_LIVE_CLAIM_STATES = ("pending", "materialized")
_STORE_READABLE_UNIT = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")
_STORE_RESERVED_NAMES = frozenset({"con", "prn", "aux", "nul"} | {f"com{index}" for index in range(1, 10)}
                                  | {f"lpt{index}" for index in range(1, 10)})
RET1_TITLE = "The retention decision matches the disk"


def _store_claim_file_name(unit_id: str) -> str:
    if (_STORE_READABLE_UNIT.fullmatch(unit_id) and not unit_id.endswith(".")
            and unit_id.split(".", 1)[0] not in _STORE_RESERVED_NAMES):
        return f"{unit_id}.json"
    return f"_u_{hashlib.sha256(unit_id.encode('utf-8')).hexdigest()[:24]}.json"


def _store_claims(store: Path, unit_id: str) -> "list[dict] | None":
    """The unit's claims in the accession store, one that cannot be read as {"state": "unreadable"};
    None without a store."""
    claims = store / "claims"
    if not unit_id or not claims.is_dir():
        return None
    name = _store_claim_file_name(unit_id)
    found: list[dict] = []
    for directory in sorted(claims.iterdir()):
        path = directory / name
        if not path.is_file():
            continue
        record, _ = _read_json(path)
        if record is None or str(record.get("unit_id") or "") != unit_id:
            found.append({"state": "unreadable"})
            continue
        found.append(record)
    return found


def _claim_opened_after(claim: dict, deleted_at: object) -> bool:
    """Whether a store claim was opened after the deletion recorded at `deleted_at`: a new consumer's.

    download_store.claim stamps claimed_at whenever it opens a claim, a released one it reopens included,
    and keeps the claim's earlier life in history; a record without claimed_at is dated by the last
    release it reopened. A deletion with no recorded time orders nothing.
    """
    deleted = _instant(deleted_at) if deleted_at else None
    if deleted is None:
        return False
    opened = _instant(claim.get("claimed_at")) if claim.get("claimed_at") else None
    if opened is None:
        released = [_instant(item.get("released_at")) for item in claim.get("history") or []
                    if isinstance(item, dict) and item.get("released_at")]
        opened = max((item for item in released if item is not None), default=None)
    return opened is not None and opened > deleted


def _deletion_crossings(*records: "dict | None") -> list[dict]:
    """The boundary-5 crossings the unit, or the raw owner of its tree, recorded under a campaign approval."""
    found: list[dict] = []
    for record in records:
        crossings = (record or {}).get("campaign_authorizations") if isinstance(record, dict) else None
        for item in crossings if isinstance(crossings, list) else []:
            if isinstance(item, dict) and str(item.get("boundary")).strip() == RAW_DELETION_BOUNDARY and item not in found:
                found.append(item)
    return found


def _campaign_boundaries(*records: "dict | None") -> list[str]:
    """Every boundary the unit, or the raw owner of its tree, recorded crossing under a campaign approval."""
    found = {str(item.get("boundary")).strip() for record in records if isinstance(record, dict)
             for item in (record.get("campaign_authorizations")
                          if isinstance(record.get("campaign_authorizations"), list) else [])
             if isinstance(item, dict)}
    return sorted(found)


def _recorded_deletion(provenance: dict, owner: dict) -> "tuple[str, str, str]":
    """(kind, where, at) of the deletion of the tree the unit reads, as its records state it, or ("", "", "").

    `at` is the time the record gives, "" when it gives none.
    """
    for record, whose in ((provenance, "the unit's"), (owner, "the raw owner's")):
        if whose == "the raw owner's" and record is provenance:
            break
        status = str(record.get("status") or "")
        if status in ("raw_cleaned", "discarded"):
            at = str(record.get(f"{status}_at") or "")
            return status, f"{whose} status {status} ({at or 'no time recorded'})", at
    release = owner.get(SPLIT_RELEASE)
    if isinstance(release, dict) and str(release.get("state") or "") == SPLIT_RELEASE_DELETED:
        at = str(release.get("deleted_at") or "")
        return "split_release", f"the split parent's raw_release ({at or 'no time recorded'})", at
    return "", "", ""


def _release_parts(release: object) -> "list[str] | None":
    if not isinstance(release, dict) or not isinstance(release.get("parts"), list):
        return None
    return [str(item.get("analysis_unit_id") or "") for item in release["parts"] if isinstance(item, dict)]


# Where a run's outputs are. Interactive 0.5.29 (PR #62) prepares a new production run of a finished unit in a new
# folder, <workspace>\output-run-<n>, named by the manifest's output_directory, and moves the finished run's records,
# its output_directory among them, into an entry of superseded_runs; the old folder is left as it was. A manifest
# written before then may name no output_directory, and its run's folder is <workspace>\output.
DEFAULT_OUTPUT_DIRECTORY = "output"


class OutputOutsideWorkspace(ValueError):
    """A run's output_directory lies outside the unit workspace the gate judges: the gate reads nothing there.

    main() exits 3, the workspace is unusable, as it does for a workspace that is not a directory.
    """


def _parts_inside(path: Path, root: Path) -> "tuple[str, ...] | None":
    """The parts of `path` below `root` when `path` lies strictly inside it, compared as Windows compares paths
    (case and separators aside); None otherwise, the root itself included."""
    def plain(item: Path) -> str:
        return os.path.normpath(os.path.abspath(str(item)))

    target, base = plain(path), plain(root)
    try:
        relative = os.path.relpath(os.path.normcase(target), os.path.normcase(base))
    except ValueError:  # another drive
        return None
    if relative in (os.curdir, os.pardir) or relative.startswith(os.pardir + os.sep) or os.path.isabs(relative):
        return None
    return Path(target).parts[len(Path(base).parts):]


def _run_output(record: dict, workspace: Path, recorded_workspace: str = "") -> Path:
    """The folder holding one run's outputs, inside `workspace`: the record's output_directory, else <workspace>\\output.

    output_directory is an absolute path Interactive wrote. It is taken when it lies inside the workspace judged,
    also once each is resolved (a short 8.3 name, a junction), or inside the workspace the manifest recorded, which
    a copied or moved workspace no longer is: its place relative to that workspace is then read in this one. A
    relative output_directory is read against the workspace. Anything else - another unit's folder, a folder
    outside the analysis tree, the workspace itself - raises OutputOutsideWorkspace, and nothing is read from it.
    """
    declared = str(record.get("output_directory") or "").strip() if isinstance(record, dict) else ""
    if not declared:
        return workspace / DEFAULT_OUTPUT_DIRECTORY
    path = Path(declared)
    if not path.is_absolute():
        path = workspace / path
    candidates = [(path, workspace)]
    try:
        candidates.append((path.resolve(), workspace.resolve()))
    except (OSError, RuntimeError):
        pass
    if recorded_workspace.strip():
        candidates.append((path, Path(recorded_workspace.strip())))
    for target, root in candidates:
        parts = _parts_inside(target, root)
        if parts:
            return workspace.joinpath(*parts)
    raise OutputOutsideWorkspace(
        f"The manifest names output_directory {declared!r}, which is not inside the unit workspace {str(workspace)!r}"
        + (f" or the workspace it recorded ({recorded_workspace.strip()!r})" if recorded_workspace.strip() else "")
        + ". The gate reads a run's outputs only from within the workspace it judges.")


def _superseded_runs(provenance: dict | None) -> list[dict]:
    """The unit's earlier production runs, oldest first, as Interactive keeps them (superseded_runs)."""
    runs = (provenance or {}).get("superseded_runs") if isinstance(provenance, dict) else None
    return [item for item in runs if isinstance(item, dict)] if isinstance(runs, list) else []


def _unit_runs(provenance: dict | None, workspace: Path) -> "list[tuple[str, dict, Path]]":
    """Every production run of the unit, newest first: (which, its record, its output folder).

    The current run is the manifest's top level; the superseded runs follow, the latest first. Raises
    OutputOutsideWorkspace when any of them names a folder outside the workspace.
    """
    record = provenance if isinstance(provenance, dict) else {}
    recorded = str(record.get("workspace") or "")
    runs = [("its current run", record, _run_output(record, workspace, recorded))]
    superseded = _superseded_runs(record)
    for index in reversed(range(len(superseded))):
        runs.append((f"its superseded run {index} (superseded_runs[{index}])", superseded[index],
                     _run_output(superseded[index], workspace, recorded)))
    return runs


def _mztab_not_validated(provenance: dict, output: Path) -> str:
    """Why the unit's mzTab-M does not count as validated for deleting its raw data, or "" when it does.

    B7's fact, from a validation that checked at least one file: a record of no failure among no files is
    not an mzTab-M that validates.
    """
    return _unvalidated(provenance, output) or _validated_nothing(provenance.get("mztab_validation"))


def _outputs_incomplete(output: Path) -> "tuple[str, list[str]]":
    """Why the unit's MS-DIAL outputs are not all present, with the exports absent; ("", []) when they are.

    EXP-1's fact, every export the run manifest planned on disk, and B6's, an .mdpeak in output. MS-DIAL
    skips a file it cannot read without saying so, and finalisation reads only the mzTab-M, so a validated
    mzTab-M does not by itself say the outputs are complete.
    """
    run_manifest, unreadable = _read_json(output / "run-manifest.json")
    exports = _absent_exports(run_manifest)
    if exports is None:
        recorded = ("records no expected_analysis_exports" if run_manifest is not None
                    else unreadable.split(" ", 1)[-1])
        return f"its output's run manifest {recorded}, so whether every output is present is not established", []
    planned, absent = exports
    if absent:
        names = ", ".join(Path(item).name for item in absent[:5]) + (", ..." if len(absent) > 5 else "")
        return f"{len(absent)} of the {planned} exports its run planned are absent ({names})", absent
    if not _mdpeak_count(output):
        return "no .mdpeak is in its output", []
    return "", []


# A superseded run Interactive calls validated by its records (repository_reanalysis.superseded_validated_run,
# PR #62 at 52b470b): its status cleanup-ready, or its cleanup allowed.
CLEANUP_READY_STATUSES = frozenset({"mztab_validated", "completed", "cleanup_pending_confirmation"})
# _deletion_justification's kind for a unit whose raw data are held for a new run that has not validated.
HELD_FOR_NEW_RUN = "held_for_new_run"


def _held_superseded_run(provenance: dict, workspace: Path) -> "dict | None":
    """The newest superseded run that validated, for a unit whose current run has not; None when there is none.

    Interactive PR #62 (52b470b): raw-data deletion is judged by the unit's current run, and while a new run
    prepared after a validated run has not validated (prepared, running or failed) the raw data are held for
    it - cleanup and discard both refuse. A superseded run counts as validated by its records as Interactive
    reads them (a cleanup-ready status, or cleanup_allowed true) or by the gate's own fact, a validated mzTab-M
    and every output in the folder that run's output_directory names. The caller has established that the
    current run did not validate.
    """
    for which, record, output in _unit_runs(provenance, workspace)[1:]:
        recorded = str(record.get("status") or "") in CLEANUP_READY_STATUSES or record.get("cleanup_allowed") is True
        on_disk = not (_mztab_not_validated(record, output) or _outputs_incomplete(output)[0])
        if recorded or on_disk:
            return {"run": which, "status": str(record.get("status") or ""), "output": output.name,
                    "finalized_at": record.get("finalized_at"), "superseded_at": record.get("superseded_at"),
                    "validated_by": "its records and its output" if recorded and on_disk
                    else "its records" if recorded else "its output"}
    return None


def _deletion_justification(provenance: dict, workspace: Path) -> "tuple[str, str]":
    """What the campaign's deletion rule lets this unit's raw data go for: (kind, detail), or ("", "").

    Validated outputs, which are a validated mzTab-M and every MS-DIAL output present (the user's rule;
    _mztab_not_validated and _outputs_incomplete), a recorded failure (after its retries: how many is the
    runner's decision, and a failure record is what is required of it), or a skip or exclusion the
    campaign disposition decided. A unit whose outputs are incomplete has in effect failed, so it is
    judged by the rest.
    The gate's own verdicts do not enter: the user decided that deletion follows the outputs, whatever
    the gate says of them.
    A deletion is judged by the unit's current run only, read in the folder its output_directory names
    (Interactive PR #62 at 52b470b). A superseded run's validated outputs never justify one: while a new run
    prepared after a validated run has not validated - prepared, running or failed - the raw data are held for
    it, and no cleanup or discard may delete them, approved or not, split part or not. That case is
    HELD_FOR_NEW_RUN, ahead of a failure, a disposition or a runner's end, and RET-1 refuses the deletion.
    """
    status = str(provenance.get("status") or "")
    output = _unit_runs(provenance, workspace)[0][2]
    unvalidated = _mztab_not_validated(provenance, output) or _outputs_incomplete(output)[0]
    if not unvalidated:
        return "validated", ("its run was finalised, its mzTab-M validated and is in its output, and every export "
                             "its run planned is there"
                             + ("" if output.name == DEFAULT_OUTPUT_DIRECTORY else f" ({output.name})"))
    held = _held_superseded_run(provenance, workspace)
    if held is not None:
        return HELD_FOR_NEW_RUN, (
            f"{held['run']} validated (status {held['status'] or 'unrecorded'!r}, by {held['validated_by']}, in "
            f"{held['output']}), and the new run prepared after it, its current run in {output.name}, has not "
            f"({unvalidated}): its raw data are held for that new run, and no cleanup or discard may delete them "
            "until it validates")
    failures = _run_failures(provenance)
    if failures:
        last = failures[-1]
        return "failed", (f"{len(failures)} run failure(s) are recorded, the last "
                          f"{str(last.get('reason') or 'with no reason')[:120]!r}")
    if status == "download_failed" or provenance.get("download_failed_at") or isinstance(provenance.get("download_failure"), dict):
        return "failed", "its download is recorded as failed"
    if isinstance(provenance.get("stale_lease_discarded"), dict):
        return "failed", "its lease's process stopped before it recorded its inputs or its failure"
    disposition = provenance.get("campaign_disposition")
    if isinstance(disposition, dict) and disposition.get("applied") is True \
            and disposition.get("disposition") in ("skip", "exclude"):
        kind = "skipped" if disposition["disposition"] == "skip" else "excluded"
        reasons = [str(item) for item in disposition.get("reasons") or []][:3]
        return kind, f"its campaign disposition {kind} it ({', '.join(reasons) or 'no reason recorded'})"
    record, _ = _read_json(workspace / CAMPAIGN_RECORD_FILE)
    if record is not None and record.get("schema") == CAMPAIGN_RECORD_SCHEMA \
            and str(record.get("state") or "") in CAMPAIGN_ENDS_WITHOUT_OUTPUT:
        return str(record["state"]), (f"the campaign runner ended it as {record['state']} "
                                      f"({record.get('terminal_reason') or 'no reason recorded'})")
    return "", ""


def check_retention_policy_was_acted_on(
    report: Report, provenance: dict | None, reason: str, workspace: Path
) -> None:
    """RET-1. The retention decision and the disk agree, and a deletion had the authority it needed.

    The policy is chosen once, at download, by the person who approved the download, and written
    into the unit's manifest. Whether the raw tree is still there is a fact about the filesystem.
    Nothing compared them, so a unit could carry a complete audit record asserting a retention
    decision it never carried out, in either direction.

    A deletion recorded while the tree is still on disk is refused, and so is a tree gone that the
    unit's records show raw data in, with neither a recorded deletion nor a campaign approval covering
    boundary 5 to account for it. A deletion under a campaign approval is correct once the outputs are
    validated, or for a unit that failed, was skipped or was excluded, and refused for any other unit:
    those are the only cases the user's rule deletes. Validated is the user's "every MS-DIAL output
    present and the mzTab-M validated": the fact B7 reaches on, an mzTab-M in output as well as the
    manifest's record, from a validation that checked at least one file, with every export EXP-1 holds
    the run to and B6's .mdpeak on disk. RET-1 never calls a deletion justified by outputs the progress
    walk or EXP-1 says are not there, and a cleanup after validated output (raw_cleaned) that lacks them
    is refused. Validated is the current run's, in the folder its output_directory names: a deletion while
    a new run prepared after a validated run has not validated is refused whatever authorized it, naming
    the superseded run the raw data were held for (Interactive PR #62). A split part whose parent's release does not list it is a WARN: its tree went without its
    own state being part of the decision. So is a deletion with no recorded authority in a unit that
    carries campaign crossings: the gate cannot tell a person's confirmation from a campaign component
    that deleted without recording its approval.
    """
    stage = "before-publish"
    if provenance is None:
        report.add("RET-1", stage, RET1_TITLE, NOT_EVALUABLE, reason)
        return
    raw, owner, unknown = _raw_directory(provenance, workspace)
    if raw is None or owner is None:
        report.add("RET-1", stage, RET1_TITLE, NOT_EVALUABLE,
                   f"This unit reads its raw tree from another unit, and {unknown}.")
        return
    # The owner's policy decides the owner's tree; a part's copy of it was taken at split time.
    policy = owner.get("raw_retention_policy")
    present = raw.is_dir() and any(raw.iterdir())
    if policy is None:
        report.add(
            "RET-1", stage, RET1_TITLE, FAIL,
            "The manifest records no raw_retention_policy. The deletion preview reads this field, "
            "so a person confirming an irreversible deletion would be shown a blank where the "
            "intent should be.", raw_present=present,
        )
        return
    policy = str(policy)
    status = str(provenance.get("status") or "")
    # The current run's folder; verify() has refused a workspace whose runs name one outside it.
    output = _unit_runs(provenance, workspace)[0][2]
    if policy not in ("keep", DELETE_RETENTION):
        report.add("RET-1", stage, RET1_TITLE, FAIL,
                   f"The manifest records a retention policy of {policy!r}, which is neither 'keep' "
                   "nor 'delete_after_validated_output'. It cannot have been acted on.",
                   policy=policy, raw_present=present)
        return

    part = isinstance(provenance.get("split_from"), dict)
    unit_id = str((provenance.get("project") or {}).get("analysis_unit_id") or "")
    owner_id = str((owner.get("project") or {}).get("analysis_unit_id") or "") if part else unit_id
    deletion, deleted_where, deleted_at = _recorded_deletion(provenance, owner)
    crossings = _deletion_crossings(provenance, owner if part else None)
    release = owner.get(SPLIT_RELEASE)
    store_claims = _store_claims(workspace.parent / STORE_DIRECTORY, owner_id)
    evidence = {
        "policy": policy, "raw_present": present, "status": status, "recorded_deletion": deletion,
        "deletion_approvals": sorted({str(item.get("approval_id") or "") for item in crossings}),
        "store_claims": None if store_claims is None else dict(Counter(
            str(item.get("state") or "unrecorded") for item in store_claims)),
    }
    notes: list[str] = []
    # A release that names its parts and leaves this one out went without this part's state.
    listed = _release_parts(release)
    if listed is not None:
        expected = [unit_id] if part else [
            str(item.get("analysis_unit_id") or "") for item in provenance.get("split_into") or []
            if isinstance(item, dict)]
        missing = [item for item in expected if item and item not in listed]
        evidence["release_parts"] = listed
        if missing:
            notes.append(f"The split parent's raw_release ({release.get('state') or 'no state'}) does not list "
                         f"{', '.join(missing)}, so the release was decided without that part's state.")

    def verdict(status_value: str, detail: str) -> None:
        report.add("RET-1", stage, RET1_TITLE, WARN if status_value == PASS and notes else status_value,
                   detail + ("" if not notes else " " + " ".join(notes)), **evidence)

    if present and deletion:
        verdict(FAIL, f"The raw tree is still on disk, and {deleted_where} records it as deleted. The record "
                      "says a deletion happened that the filesystem contradicts, so whatever relies on it - the "
                      "store's collection, a disk budget, the unit's own end state - is wrong.")
        return
    if present:
        if crossings:
            verdict(WARN, f"A deletion was authorized under campaign approval {evidence['deletion_approvals'][0]} "
                          "(boundary 5), and the raw tree is still on disk: the deletion did not complete.")
            return
        if isinstance(release, dict) and str(release.get("state") or "") != SPLIT_RELEASE_DELETED:
            verdict(WARN, f"The split parent's raw_release is {release.get('state') or 'without a state'!r} and "
                          "the raw tree is still on disk: the release has not completed.")
            return
        if policy == DELETE_RETENTION and status in VALIDATED_STATUSES - {"raw_cleaned"}:
            incomplete = _outputs_incomplete(output)[0]
            if incomplete and not _mztab_not_validated(provenance, output):
                verdict(WARN, f"Policy is delete_after_validated_output, the mzTab-M is validated, and {incomplete}: "
                              "the raw tree is still present, and the deletion is not yet due. A unit whose outputs "
                              "are incomplete has in effect failed, and its raw data go after its retries.")
                return
            verdict(WARN, "Policy is delete_after_validated_output, the output is validated, and the raw tree is "
                          "still present. The deletion is due; it needs a person's confirmation or a campaign "
                          "approval covering boundary 5, and neither has been acted on yet.")
            return
        verdict(PASS, "Policy is 'keep' and the raw tree is present." if policy == "keep"
                else f"Policy is {policy!r}; raw tree present, status {status!r}.")
        return

    # The tree is gone.
    if not deletion:
        if crossings:
            verdict(WARN, f"The raw tree is gone under campaign approval {evidence['deletion_approvals'][0]} "
                          "(boundary 5), and no deletion is recorded: the deletion's completion was not written.")
            return
        downloaded = bool(owner.get("downloads")) or bool(owner.get("input_candidates")) \
            or bool(provenance.get("input_candidates"))
        if downloaded:
            verdict(FAIL, "The raw tree is gone, the unit's records show raw data were downloaded into it, and "
                          "neither a recorded deletion (raw_cleaned, discarded or a split parent's release) nor a "
                          "campaign approval covering boundary 5 accounts for it: the raw data were deleted without "
                          "an authorization or a confirmation.")
            return
        if policy == "keep":
            verdict(WARN, f"Policy is {policy!r} and the raw tree is gone.")
            return
        verdict(WARN, "Policy is delete_after_validated_output and the raw tree is gone, but no confirmed cleanup "
                      f"is recorded (status {str(owner.get('status') or '') or status!r}).")
        return

    keeping = [str(item.get("approval_id") or "") for item in crossings
               if str(item.get("raw_retention_policy") or "") != DELETE_RETENTION]
    if keeping:
        verdict(FAIL, f"The raw tree was deleted ({deleted_where}) under campaign approval {keeping[0]}, which "
                      "keeps raw data: no approval that keeps raw data covers a deletion.")
        return
    boundaries = _campaign_boundaries(provenance, owner if part else None)
    if crossings:
        authority = f"campaign approval {evidence['deletion_approvals'][0]} (boundary 5)"
    elif deletion == "split_release" and release.get("authorized_by"):
        authority = f"the release's recorded authority ({str(release.get('authorized_by'))[:120]})"
    elif boundaries:
        # A campaign unit whose deletion carries no crossing: a person's confirmed=true records no crossing
        # either, so the records cannot say which it was.
        authority = "a confirmed=true call whose authority is not recorded"
        notes.append(f"The unit carries campaign crossings for boundar{'y' if len(boundaries) == 1 else 'ies'} "
                     f"{', '.join(boundaries)} and none for boundary 5, so its deletion was not recorded under the "
                     "campaign's approval: a person's confirmation is assumed, and a campaign component that deleted "
                     "without recording its approval would look the same.")
    else:
        # Interactive writes raw_cleaned and discarded only on confirmed=true or a boundary-5 crossing, and
        # outside a campaign confirmed=true is a person's; who gave it is not recorded.
        authority = "confirmed=true, a person's confirmation outside a campaign (who gave it is not recorded)"
    # Why the outputs do not count as validated: the mzTab-M, or else the outputs beside it.
    unvalidated, incomplete, absent = "", "", []
    if deletion == "split_release" and not part:
        parts = _release_parts(release) or []
        kind, why = ("released", f"its release lists {len(parts)} part(s)") if parts else ("", "")
    else:
        kind, why = _deletion_justification(provenance, workspace)
        if kind != "validated":
            unvalidated = _mztab_not_validated(provenance, output)
            incomplete, absent = ("", []) if unvalidated else _outputs_incomplete(output)
    evidence.update(authority=authority, justification=kind)
    if kind == HELD_FOR_NEW_RUN:
        held = _held_superseded_run(provenance, workspace) or {}
        evidence.update(unvalidated_because=unvalidated or incomplete, held_superseded_run=held.get("run"),
                        held_superseded_output=held.get("output"), held_superseded_status=held.get("status"))
        verdict(FAIL, f"The raw tree was deleted ({deleted_where}) on {authority}, and {why}. A raw deletion is "
                      "judged by the unit's current run (Interactive PR #62): a superseded run's validated outputs "
                      "justify no deletion, and the new run was left without the inputs it was prepared to read.")
        return
    if deletion == "raw_cleaned" and kind != "validated":
        evidence["unvalidated_because"] = unvalidated or incomplete
        if absent:
            evidence.update(absent_export_count=len(absent), absent_exports=absent[:10])
        verdict(FAIL, f"The raw tree was deleted as a cleanup after validated output ({deleted_where}), and "
                      + (f"{unvalidated}: the cleanup rests on a validated mzTab-M that neither the unit's records "
                         "nor its output show." if unvalidated else
                         f"{incomplete}: the cleanup rests on outputs that are not all there. A unit whose outputs "
                         "are incomplete has in effect failed, and the campaign deletes a failed unit's raw data "
                         "only after its retries."))
        return
    if not kind:
        neither = ("neither validated outputs" + (f" ({unvalidated or incomplete})" if unvalidated or incomplete else "")
                   + " nor a failure, a skip or an exclusion")
        if crossings:
            verdict(WARN, f"The raw tree was deleted ({deleted_where}) under {authority}, and the unit records "
                          f"{neither}. The campaign deletes raw data for none but those, so either the unit's failure "
                          "record was never written or the deletion broke the rule.")
            return
        verdict(WARN, f"The raw tree was deleted ({deleted_where}) on {authority}, and the unit records {neither}, "
                      "so why is not on record"
                      + (f" beyond {str(provenance.get('discard_reason'))[:160]!r}" if provenance.get("discard_reason") else "")
                      + ".")
        return
    if policy == "keep":
        verdict(WARN, f"Policy is 'keep' and the raw tree was deleted ({deleted_where}) on {authority}: the "
                      "deletion was authorized, and it is not what the retention decision recorded.")
        return
    # A claim opened after the deletion is a new consumer's, such as a re-run's pre-claim, which reopens the
    # released claim; only one opened before it is left over from the tree that went.
    live = [item for item in store_claims or [] if item.get("state") in STORE_LIVE_CLAIM_STATES]
    reopened = sum(1 for item in live if _claim_opened_after(item, deleted_at))
    if reopened:
        evidence["store_claims_opened_after_deletion"] = reopened
    if len(live) > reopened:
        notes.append(f"{len(live) - reopened} of the unit's claims in the accession store are still live, so the store "
                     "keeps the objects this tree linked to until they are released.")
    verdict(PASS, f"The raw tree was deleted ({deleted_where}) under {authority}: {why}.")


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


# ---- SEC-1 --------------------------------------------------------------------------------------
# Three classes of artifact, from the privacy contract. SHARED ones are built to leave the machine:
# the publication report, bundle, tables and texts, the workflow bundle, and anything under the
# unit's share\. The Console's mzTab-M is what the campaign redistributes, and the Console writes this
# machine's raw and library locations into it as file URIs; nothing in it can declare a sharing
# policy, so its paths are graded as a legacy artifact's are. datamining-handoff.json is a LOCAL
# handoff to the next agent: it carries this machine's paths on purpose, and is read for a private
# library only.
#
# Refused wherever it is read: a user-profile path; a private library's location or directory, in
# any encoding, or its file name after a separator (a bare file name is the identifier the contract
# asks for; after a separator it is a path); a member whose bytes are a recorded library's; a library
# file, or MS-DIAL's copy of one (*_Loaded.msp2.dbs), inside any bundle; and a local-only file inside
# a shared bundle. Any other drive-letter, UNC or file-URI path is refused in an artifact that declares
# Interactive's shared-path policy (msdial-interactive.shared-paths.v1, or any value: a declaration
# binds) and warned about in one that does not, which is every artifact written before the policy
# existed, MTBLS2207's among them. A file or member named as a zip or a workbook that does not open as
# one is unread, never scanned as text: empty, headerless or an error page saved under the name, what it
# was meant to hold is not there to read.
SHARED_PATHS_MEMBER = "SHARED-PATHS.json"
SEC1_TITLE = "No private path in a shared artifact"
SEC1_REPORT = "MS_DIAL_publication_report.json"
SEC1_PUBLICATION_BUNDLE = "MS_DIAL_publication_reporting_bundle.zip"
SEC1_PUBLICATION_FILES = (
    SEC1_PUBLICATION_BUNDLE, SEC1_REPORT, "Supplementary_Table_MS_DIAL.tsv",
    "Supplementary_Table_MS_DIAL.xlsx", "MS_DIAL_Materials_and_Methods.txt", "MS_DIAL_QA_Results.txt",
)
SEC1_WORKFLOW_BUNDLE = "msdial-workflow-bundle.zip"
SEC1_HANDOFF = "datamining-handoff.json"
LIBRARY_MEMBER = re.compile(r"\.(?:msp|msp2|dbs|lbm|lbm2)$", re.IGNORECASE)
LOCAL_ONLY_MEMBER = re.compile(
    r"\.(?:mdproject|mddata|dcl|pai2)$|^(?:datamining-handoff|guided-answers)\.json$", re.IGNORECASE)
PRIVATE_LICENCE = re.compile(r"private|institutional|proprietary|in-house", re.IGNORECASE)
ZIP_MAGIC = b"PK\x03\x04"
SEC1_CONTAINER_NAME = re.compile(r"\.(?:zip|xlsx)$", re.IGNORECASE)  # must open as a zip, whatever it holds
SEC1_DEPTH = 2                          # a bundle, and the workbook inside it
SEC1_NESTED_LIMIT = 256 * 1024 * 1024   # a nested container is read into memory to be opened
SEC1_LINE_LIMIT = 1024 * 1024           # a longer line is read in pieces
SEC1_CHUNK = 4 * 1024 * 1024            # text is matched this much at a time
SEC1_STRUCTURE_COLUMNS = frozenset({"smiles", "inchi"})
SEC1_SEPARATORS = re.compile(r"[\\/]+")
SEC1_JSON_ESCAPE = re.compile(r"\\u([0-9A-Fa-f]{4})")


@dataclass(frozen=True)
class _Sec1Scope:
    """How one artifact is read.

    paths: the path patterns apply. declared: the artifact declares the shared-path policy, so any
    absolute path in it is refused. content: its text is read at all, rather than only its member
    names. members: a local-only member is refused, which holds only inside a shared container.
    """
    role: str
    paths: bool = True
    declared: bool = False
    content: bool = True
    members: bool = True


class _Sec1Findings:
    def __init__(self) -> None:
        self.fails: dict[tuple[str, str], list] = {}
        self.warns: dict[tuple[str, str], list] = {}
        self.unread: list[str] = []

    @staticmethod
    def _add(table: dict, where: str, kind: str, example: str) -> None:
        entry = table.setdefault((where, kind), [0, example])
        entry[0] += 1
        entry[1] = entry[1] or example

    def fail(self, where: str, kind: str, example: str = "") -> None:
        self._add(self.fails, where, kind, example)

    def warn(self, where: str, kind: str, example: str = "") -> None:
        self._add(self.warns, where, kind, example)

    @staticmethod
    def listed(table: dict) -> list[str]:
        return [f"{where}: {kind}" + (f" x{count}" if count > 1 else "") + (f" (e.g. {example})" if example else "")
                for (where, kind), (count, example) in table.items()]

    @staticmethod
    def total(table: dict) -> int:
        return sum(count for count, _example in table.values())

    @staticmethod
    def by_artifact(table: dict) -> list[str]:
        """How many findings each artifact holds, its members counted with it."""
        counts: Counter = Counter()
        for (where, _kind), (count, _example) in table.items():
            counts[where.split(":", 1)[0]] += count
        return [f"{artifact} {count}" for artifact, count in counts.items()]


def _sec1_unescape(match: "re.Match") -> str:
    code = int(match.group(1), 16)
    # json.dumps escapes what lies beyond ASCII. An escaped ASCII character is not something it writes,
    # and decoding one could turn a backslash and the name after it into something else.
    return chr(code) if code >= 0x80 else match.group(0)


def _sec1_fold(text: str) -> str:
    """Text as SEC-1 compares it with a library location: percent-, JSON- and XML-escapes decoded, one
    case, and every run of separators a single '/'. The location is folded the same way, so whichever
    encoding a writer chose (backslashes, doubled backslashes, forward slashes, file://, %20) meets it.
    """
    if "%" in text:
        text = urllib.parse.unquote(text, errors="replace")
    if "\\u" in text:
        text = SEC1_JSON_ESCAPE.sub(_sec1_unescape, text)
    if "&" in text:
        text = html.unescape(text)
    return SEC1_SEPARATORS.sub("/", text.casefold())


def _sec1_libraries(output: Path) -> "tuple[list[dict], list[str]]":
    """Every library this unit's local records name, and why a record could not be read.

    A library is public only when a DOI or an https source is recorded for it and no record gives it
    a private, institutional or proprietary licence or a private distribution. Anything else is
    private, and so is a library the settings load that no provenance record names: the default
    licence for a user's own library is 'institutional/private', and a record that says nothing is
    no evidence that the library may be shared.
    """
    merged: dict[str, dict] = {}
    problems: list[str] = []
    readable = 0

    def add(path: object, entry: "dict | None" = None) -> None:
        text = str(path or "").strip()
        if not text:
            return
        key = _sec1_fold(text).rstrip("/")
        library = merged.setdefault(key, {
            "key": key, "path": text, "name": PureWindowsPath(text).name, "identified": False,
            "restricted": False, "sha256": set(), "sizes": set(), "size_unknown": False,
        })
        if not entry:
            return
        source = str(entry.get("source") or entry.get("record_url") or "").strip()
        if str(entry.get("doi") or "").strip() or source.casefold().startswith("https://"):
            library["identified"] = True
        licence = str(entry.get("license") or entry.get("licence") or "")
        if PRIVATE_LICENCE.search(licence) or str(entry.get("distribution") or "").strip().casefold() == "private":
            library["restricted"] = True
        digest = str(entry.get("sha256") or "").strip().casefold()
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            library["sha256"].add(digest)
            size = entry.get("size", entry.get("size_bytes"))
            if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                library["sizes"].add(size)
            else:
                library["size_unknown"] = True

    for name, key in (("workflow-settings.json", "library_provenance"), ("run-manifest.json", "libraries")):
        path = output / name
        if not path.exists():
            continue
        record, reason = _read_json(path)
        if record is None:
            problems.append(reason)
            continue
        readable += 1
        entries = record.get(key)
        if entries is not None and not isinstance(entries, list):
            problems.append(f"{name} records {key} as something other than a list")
            entries = None
        for entry in entries or []:
            if isinstance(entry, dict):
                add(entry.get("path") or entry.get("local_path") or entry.get("filename"), entry)
        if name == "workflow-settings.json":
            # The libraries the Console was told to load, whether or not a provenance record names them.
            for setting in ("msp_path", "text_db_path", "lbm_path"):
                add(record.get(setting))
            for rows, column in (("msp_annotators", "msp_file_path"), ("text_annotators", "text_db_file_path")):
                for row in record.get(rows) if isinstance(record.get(rows), list) else []:
                    if isinstance(row, dict):
                        add(row.get(column))
            if isinstance(record.get("lbm_annotator"), dict):
                add(record["lbm_annotator"].get("lbm_file_path"))
    if not readable and not problems:
        problems.append("neither workflow-settings.json nor run-manifest.json is in the output directory")
    libraries = list(merged.values())
    for library in libraries:
        library["private"] = library["restricted"] or not library["identified"]
    return libraries, problems


class _Sec1Needles:
    """What a private library looks like in an artifact, built from the unit's own records."""

    def __init__(self, libraries: list[dict], workspace: Path) -> None:
        self.locations: list[tuple[str, str]] = []
        self.directories: list[tuple["re.Pattern", str]] = []
        self.names: list[tuple[str, str]] = []
        self.sha256: dict[str, str] = {}
        self.sizes: set[int] = set()
        self.hash_every_size = False
        self.directories_not_used = 0
        home = _sec1_fold(os.path.abspath(workspace)).rstrip("/")
        public = [library["key"] for library in libraries if not library["private"]]
        for library in libraries:
            for digest in library["sha256"]:
                self.sha256[digest] = library["name"]
            self.sizes |= library["sizes"]
            self.hash_every_size |= library["size_unknown"]
            if not library["private"]:
                continue
            if library["name"]:
                self.names.append(("/" + _sec1_fold(library["name"]), library["name"]))
            where = PureWindowsPath(library["path"])
            if not where.anchor:
                continue  # a relative record: the file-name rule is what applies
            self.locations.append((library["key"], library["name"]))
            parent = where.parent
            directory = _sec1_fold(str(parent)).rstrip("/")
            # A drive or share root names no directory. A directory that holds the workspace, lies inside
            # it or holds a public library would refuse every path to those, which say nothing about where
            # the private library is kept; the library's own location and name still apply.
            if (len(parent.parts) < 2 or directory == home or home.startswith(directory + "/")
                    or directory.startswith(home + "/") or any(key.startswith(directory + "/") for key in public)):
                self.directories_not_used += 1
                continue
            # The directory itself, or a path under it; not a sibling that merely begins with its name.
            self.directories.append((re.compile(
                re.escape(directory) + r"(?=/|[\s\"'<>|,;)\]]|\.(?:\s|$)|$)", re.MULTILINE), library["name"]))

    @property
    def active(self) -> bool:
        return bool(self.locations or self.directories or self.names)

    def hashes(self, size: int) -> bool:
        return bool(self.sha256) and (self.hash_every_size or size in self.sizes)


def _sec1_paths(text: str) -> "list[tuple[str, str]]":
    """(kind, match) for each path in the text, each location counted once: a file URI holds a
    drive-letter path, and a user-profile path is one."""
    starts: list[int] = []
    ends: list[int] = []
    found: list[tuple[str, str]] = []
    # Each pattern but the file URI's tries a lookbehind at every position, which on a large mzTab-M
    # costs seconds per hundred MB. A text holding none of the separators a match must contain is skipped.
    for kind, pattern, marks in (
        ("user-profile path", PRIVATE_PATH_PATTERN, (":\\", ":/")),
        ("file URI", FILE_URI_PATTERN, ()),
        ("UNC path", UNC_PATH_PATTERN, ("\\\\", "//", "%5C", "%5c")),
        ("drive-letter path", ABSOLUTE_PATH_PATTERN, (":\\", ":/", "%3A", "%3a")),
    ):
        if marks and not any(mark in text for mark in marks):
            continue
        for match in pattern.finditer(text):
            start, end = match.span()
            index = bisect.bisect_right(starts, start)
            if (index and ends[index - 1] > start) or (index < len(starts) and starts[index] < end):
                continue
            starts.insert(index, start)
            ends.insert(index, end)
            found.append((kind, match.group(0)))
    return found


def _sec1_shape(kind: str, match: str) -> str:
    """Enough to find a path again without saying where it leads: the kind of anchor, and the file name
    when the path ends in one. A bare file name is an identifier the contract allows; a directory is
    not, and nor is a drive letter, since the report itself may be kept and shared. A user-profile
    path is not shown at all, since it names the account."""
    if kind == "user-profile path":
        return ""
    # A path in a CSV row runs on into the next field; the Console's parser allows no comma in one.
    path = re.split(r"[,;]", match, maxsplit=1)[0].rstrip("\\/.:)]}")
    parts = [part for part in re.split(r"(?:\\|/|%5C|%2F)+", path, flags=re.IGNORECASE) if part]
    last = parts[-1] if len(parts) > 1 else ""
    if len(last) > 80 or not re.fullmatch(r"[^.]+(?:\.[A-Za-z0-9]{1,8})+", last):
        return ""
    head, separator = {"file URI": ("file://", "/"), "UNC path": ("\\\\", "\\")}.get(kind, ("", "\\"))
    return head + "..." + separator + last


def _sec1_structures_blanked(line: str, columns: "dict[str, list[int]]") -> str:
    """An mzTab-M line with its SMILES and InChI fields emptied, so no stereo bond is read as a path."""
    body = line.rstrip("\r\n")
    fields = body.split("\t")
    section = {"SMH": "SML", "SEH": "SME"}.get(fields[0])
    if section:
        columns[section] = [index for index, name in enumerate(fields)
                            if name.strip().casefold() in SEC1_STRUCTURE_COLUMNS]
        return line
    blank = columns.get(fields[0])
    if not blank:
        return line
    for index in blank:
        if index < len(fields):
            fields[index] = ""
    return "\t".join(fields) + line[len(body):]


def _sec1_match(text: str, where: str, scope: _Sec1Scope, needles: _Sec1Needles,
                findings: _Sec1Findings) -> None:
    if scope.paths:
        for kind, match in _sec1_paths(text):
            if kind == "user-profile path" or scope.declared:
                findings.fail(where, kind, _sec1_shape(kind, match))
            else:
                findings.warn(where, kind, _sec1_shape(kind, match))
    if not needles.active:
        return
    folded = _sec1_fold(text)
    named: set[str] = set()
    for location, name in needles.locations:
        if location in folded:
            findings.fail(where, f"the location of private library {name}")
            named.add(name)
    for pattern, name in needles.directories:
        if name not in named and pattern.search(folded):
            findings.fail(where, f"the directory of private library {name}")
            named.add(name)
    for fragment, name in needles.names:
        if name not in named and fragment in folded:
            findings.fail(where, f"private library {name} named inside a path")


class _Sec1Reader(io.RawIOBase):
    """A member's bytes, hashed as they are read, so each member is read once for every rule."""

    def __init__(self, raw, digest) -> None:
        super().__init__()
        self._raw = raw
        self._digest = digest

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self._raw.read(len(buffer))
        if self._digest is not None:
            self._digest.update(data)
        buffer[:len(data)] = data
        return len(data)


def _sec1_leaf(handle, where: str, size: int, scope: _Sec1Scope, needles: _Sec1Needles,
               findings: _Sec1Findings) -> None:
    digest = hashlib.sha256() if needles.hashes(size) else None
    if scope.content and not LIBRARY_MEMBER.search(where):
        stream = io.TextIOWrapper(io.BufferedReader(_Sec1Reader(handle, digest)), encoding="utf-8-sig",
                                  errors="replace", newline="")
        structures = where.casefold().endswith(".mztab")
        columns: dict[str, list[int]] = {}
        lines: list[str] = []
        length = 0
        while True:
            line = stream.readline(SEC1_LINE_LIMIT)
            if not line:
                break
            if structures:
                line = _sec1_structures_blanked(line, columns)
            lines.append(line)
            length += len(line)
            if length >= SEC1_CHUNK:
                _sec1_match("".join(lines), where, scope, needles, findings)
                lines, length = [], 0
        if lines:
            _sec1_match("".join(lines), where, scope, needles, findings)
    elif digest is not None:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if digest is not None and digest.hexdigest() in needles.sha256:
        findings.fail(where, f"the content of library {needles.sha256[digest.hexdigest()]}")


def _sec1_not_a_zip(header: bytes) -> str:
    """Why a file named as a zip was not opened as one. Streamed as text instead, it would pass with
    nothing it was meant to hold read."""
    return ("empty" if not header else "no zip header") + ", although its name makes it a zip archive"


def _sec1_container(archive: zipfile.ZipFile, where: str, depth: int, scope: _Sec1Scope,
                    needles: _Sec1Needles, findings: _Sec1Findings) -> None:
    for info in archive.infolist():
        if info.is_dir():
            continue
        member = f"{where}:{info.filename}"
        base = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
        if LIBRARY_MEMBER.search(base):
            findings.fail(member, "a library file, or MS-DIAL's copy of one, as a member")
        elif scope.members and LOCAL_ONLY_MEMBER.search(base):
            findings.fail(member, "a local-only file as a member")
        if not scope.content and not needles.hashes(info.file_size):
            continue
        try:
            nested = False
            if scope.content:
                with archive.open(info) as handle:
                    header = handle.read(4)
                nested = header == ZIP_MAGIC
                if not nested and SEC1_CONTAINER_NAME.search(base):
                    findings.unread.append(f"{member} ({_sec1_not_a_zip(header)})")
                    continue
            if not nested:
                with archive.open(info) as handle:
                    _sec1_leaf(handle, member, info.file_size, scope, needles, findings)
                continue
            if depth >= SEC1_DEPTH or info.file_size > SEC1_NESTED_LIMIT:
                findings.unread.append(f"{member} (a container nested too deep, or too large, to open)")
                continue
            with zipfile.ZipFile(io.BytesIO(archive.read(info))) as inner:
                _sec1_container(inner, member, depth + 1, scope, needles, findings)
        except Exception as exc:  # an encrypted member, an unknown compression, a corrupt stream
            findings.unread.append(f"{member} ({type(exc).__name__})")


def _sec1_artifact(path: Path, where: str, scope: _Sec1Scope, needles: _Sec1Needles,
                   findings: _Sec1Findings) -> None:
    try:
        with path.open("rb") as handle:
            header = handle.read(4)
        container = header == ZIP_MAGIC
        if not container and SEC1_CONTAINER_NAME.search(path.name):
            findings.unread.append(f"{where} ({_sec1_not_a_zip(header)})")
            return
        if container:
            with zipfile.ZipFile(path) as archive:
                _sec1_container(archive, where, 1, scope, needles, findings)
            return
        size = path.stat().st_size
        if not scope.content and not needles.hashes(size):
            return
        with path.open("rb") as handle:
            _sec1_leaf(handle, where, size, scope, needles, findings)
    except Exception as exc:  # a zip header on something that is not one, or an unreadable file
        findings.unread.append(f"{where} ({type(exc).__name__})")


def _sec1_policy(value: object) -> str:
    """The policy an artifact declares. Any value at all is a declaration, and binds the artifact."""
    if value is None or value is False:
        return ""
    return str(value).strip()


def _sec1_member_record(path: Path, member_name: str) -> "tuple[bool, dict | None]":
    """Whether a bundle holds a member of this name, and the JSON object in it when it can be read."""
    found = False
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.filename.replace("\\", "/").rsplit("/", 1)[-1].casefold() != member_name.casefold():
                    continue
                found = True
                if info.file_size > QA_TABLE_LIMIT:
                    return found, None
                parsed = json.loads(archive.read(info).decode("utf-8-sig"))
                return found, parsed if isinstance(parsed, dict) else None
    except Exception:  # an unreadable bundle, or member, is reported as unread by the scan itself
        pass
    return found, None


def check_no_private_path_in_a_shared_artifact(report: Report, output: Path, stage: str) -> None:
    """SEC-1. Nothing meant for sharing carries a path from this machine, or a private library.

    The contract forbids private library files and their paths from reaching bundles, logs,
    repositories or shared reports, and requires a library to be identified by name and checksum
    instead. The publication bundle is the artifact built to leave the machine, and a check that
    reads only its member NAMES passes it while its members carry the path.

    The first widening of this check followed the production library out of the user profile. It
    matched only a profile path in four publication files, and passed both MTBLS2207 parts while
    their report, table and workflow bundle carried tens of absolute paths, the library locations
    among them, and their mzTab-M named each library as a file URI: a library kept on a data drive
    or a share was invisible to it. So it now reads the workbook inside the bundle, the workflow
    bundle, the mzTab-M and the data-mining handoff as well, knows each private library by the unit's
    own records, and refuses a library's bytes or MS-DIAL's copy of it in any bundle.

    Nothing this check reports quotes a location: a finding names the artifact, what was found, the
    library by its file name, and at most the file name a path ends in. The report is itself a record
    that may be kept and shared.

    A PASS here means no known pattern matched. It is not a statement that the bundle contains no
    private data, and it must never be read as one.
    """
    if stage != "before-publish":
        return
    workspace = output.parent
    libraries, library_problems = _sec1_libraries(output)
    needles = _Sec1Needles(libraries, workspace)
    findings = _Sec1Findings()
    notes: list[str] = []
    report_policy = ""
    if (output / SEC1_REPORT).exists():
        record, reason = _read_json(output / SEC1_REPORT)
        if record is None:
            notes.append(f"{reason}, so whether the publication artifacts declare a shared-path policy is unknown")
        else:
            report_policy = _sec1_policy(record.get("shared_path_policy"))

    # The tables and texts follow the report written with them; a bundle also follows its own copy.
    artifacts: list[tuple[Path, str, _Sec1Scope]] = []
    declared: dict[str, str] = {}
    for name in SEC1_PUBLICATION_FILES:
        path = output / name
        if not path.is_file():
            continue
        policy = report_policy
        if name == SEC1_PUBLICATION_BUNDLE and not policy:
            _found, inner = _sec1_member_record(path, SEC1_REPORT)
            policy = _sec1_policy((inner or {}).get("shared_path_policy"))
        artifacts.append((path, name, _Sec1Scope("shared", declared=bool(policy))))
        if policy:
            declared[name] = policy
    workflow = output / SEC1_WORKFLOW_BUNDLE
    if workflow.is_file():
        # The workflow bundle is written before the run, so it declares for itself, by carrying the member.
        found, record = _sec1_member_record(workflow, SHARED_PATHS_MEMBER)
        policy = (_sec1_policy((record or {}).get("policy")) or SHARED_PATHS_MEMBER) if found else ""
        artifacts.append((workflow, workflow.name, _Sec1Scope("shared", declared=bool(policy))))
        if policy:
            declared[workflow.name] = policy
    for path in sorted(output.glob("*.mzTab")):
        artifacts.append((path, path.name, _Sec1Scope("console")))
    if (output / SEC1_HANDOFF).is_file():
        artifacts.append((output / SEC1_HANDOFF, SEC1_HANDOFF, _Sec1Scope("handoff", paths=False, members=False)))
    share = workspace / "share"
    if share.is_dir():
        # Placed there to be shared, so the directory is the declaration.
        for path in sorted(item for item in share.rglob("*") if item.is_file()):
            where = "share/" + path.relative_to(share).as_posix()
            artifacts.append((path, where, _Sec1Scope("shared", declared=True)))
            declared[where] = "share"
            if LIBRARY_MEMBER.search(path.name):
                findings.fail(where, "a library file, or MS-DIAL's copy of one, placed to be shared")
            elif LOCAL_ONLY_MEMBER.search(path.name):
                findings.fail(where, "a local-only file placed to be shared")
    read = len(artifacts)
    # Every other bundle is local, and is read for one thing: a library file inside it.
    for path in sorted(output.glob("*.zip")):
        if path.name.casefold() not in (SEC1_PUBLICATION_BUNDLE.casefold(), SEC1_WORKFLOW_BUNDLE.casefold()):
            artifacts.append((path, path.name, _Sec1Scope("local bundle", paths=False, content=False, members=False)))
    for path, where, scope in artifacts:
        _sec1_artifact(path, where, scope, needles, findings)

    if library_problems:
        notes.append("the private-library rules were not applied, because " + "; ".join(library_problems))
    fails = _Sec1Findings.listed(findings.fails)
    warns = _Sec1Findings.listed(findings.warns)
    evidence = dict(
        scanned=[f"{where} ({scope.role})" for _path, where, scope in artifacts][:40],
        declared=declared,
        libraries=[{"name": library["name"], "distribution": "private" if library["private"] else "public"}
                   for library in libraries],
        private_library_rules="not applied" if library_problems else "applied",
        directory_rules_not_used=needles.directories_not_used,
        unread=findings.unread[:20],
    )
    if fails:
        report.add(
            "SEC-1", stage, SEC1_TITLE, FAIL,
            f"{_Sec1Findings.total(findings.fails)} finding(s) that no artifact built to be shared may carry: "
            + "; ".join(fails[:3]) + ". Identify a library by name and checksum, never by location; keep "
            "library files and MS-DIAL's copies of them out of every bundle; and an artifact that declares "
            "the shared-path policy carries no absolute path at all.",
            fails=fails[:30], fail_count=len(fails), warns=warns[:30], **evidence,
        )
        return
    if not read:
        report.add("SEC-1", stage, SEC1_TITLE, NOT_EVALUABLE, "No artifact built to be shared is present.",
                   required=False, **evidence)
        return
    reasons: list[str] = []
    if warns:
        reasons.append(
            f"{_Sec1Findings.total(findings.warns)} absolute path(s) (drive-letter, UNC or file URI) are in "
            "artifacts that declare no shared-path policy, so were written before Interactive declared one, "
            "or by the Console (" + ", ".join(_Sec1Findings.by_artifact(findings.warns)) + "). Regenerate or "
            "redact them before they are shared")
    if findings.unread:
        reasons.append(f"{len(findings.unread)} artifact member(s) could not be read, so what they hold is "
                       "unknown: " + "; ".join(findings.unread[:5]))
    reasons.extend(notes)
    if reasons:
        report.add("SEC-1", stage, SEC1_TITLE, WARN,
                   "No user-profile path, private-library location or library content matched, but "
                   + "; and ".join(reasons) + ".",
                   warns=warns[:30], warn_count=len(warns), **evidence)
        return
    report.add("SEC-1", stage, SEC1_TITLE, PASS,
               f"No known path pattern, private-library location or library content matched in {read} "
               "artifact(s). This says no known pattern matched, not that no private data is present.",
               **evidence)


def _readable_members(path: Path, unread: list[str] | None = None) -> list[tuple[str, str]]:
    """Every text member of an artifact, so a zip is scanned by content and not by filename.

    What cannot be read is named in `unread`, so that a check scanning these does not pass content
    it never saw.
    """
    unread = unread if unread is not None else []
    try:
        if path.suffix.casefold() == ".zip":
            members = []
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    try:
                        members.append((name, archive.read(name).decode("utf-8", "replace")))
                    except Exception as exc:  # an encrypted member, an unknown compression, a corrupt stream
                        unread.append(f"{path.name}:{name} ({type(exc).__name__})")
            return members
        return [(path.name, path.read_text(encoding="utf-8", errors="replace"))]
    except Exception as exc:  # not a zip, or unreadable
        unread.append(f"{path.name} ({type(exc).__name__})")
        return []


def verify(workspace: Path, stage: str) -> Report:
    """Run `stage`'s checks (or every stage's) on one unit workspace.

    The run's outputs are read in the folder the manifest's output_directory names, <workspace>\\output where
    it names none (_run_output). Raises OutputOutsideWorkspace, before any check runs, when that folder or a
    superseded run's lies outside the workspace.
    """
    report = Report(workspace)
    provenance_path = workspace / "provenance" / "run-manifest.json"
    provenance, provenance_reason = _read_json(provenance_path)
    output = _unit_runs(provenance, workspace)[0][2]
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
        check_extractor_identity(report, provenance, provenance_reason)
        check_acquisition_type_is_the_headers(report, provenance, provenance_reason, csv_rows, csv_reason, run_manifest)
        check_checksum_coverage(report, provenance, provenance_reason, csv_rows)
        check_converted_inputs_are_their_conversions(report, provenance, provenance_reason, csv_rows, csv_reason)
        check_class_distribution(report, csv_rows, csv_reason, "before-production", provenance)
        check_executed_class_matches_approved(report, provenance, provenance_reason, csv_rows, csv_reason)
        check_class_proposal_was_accepted(report, provenance, provenance_reason)
        check_threshold_was_measured_on_this_unit(report, provenance, provenance_reason, output)
        check_analytical_order_is_real(report, csv_rows, csv_reason, "before-production", provenance)
        check_analysis_inputs_are_the_inputs(report, provenance, provenance_reason, csv_rows, csv_reason)
        check_inferred_name_pairings_are_listed(report, provenance, provenance_reason)
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
        check_expected_exports_present(report, run_manifest, output, "after-run", provenance)
        check_mztab_structure(report, output, "after-run")
        check_mztab_run_count(report, output, approved, "after-run")
        check_method_file_reached_the_console(report, output, "after-run", provenance)
        check_aif_files_have_collision_energies(report, provenance, csv_rows, csv_reason, output, "after-run")
        check_binary_identity_is_recorded(report, run_manifest, output, "after-run")

    if "before-publish" in stages:
        check_no_metric_rests_on_a_synthetic_order(report, output, csv_rows, "before-publish", provenance)
        check_library_provenance_contradiction(report, output, "before-publish")
        check_qa_prose_matches_assessment(report, output, "before-publish")
        check_storage_shape(report, workspace, "before-publish", provenance)
        check_unit_reached_a_terminal_state(report, provenance, provenance_reason, output)
        check_no_finalisation_hold_stands(report, provenance, provenance_reason)
        check_no_unearned_checksum_claim(report, provenance, output, csv_rows)
        check_retention_policy_was_acted_on(report, provenance, provenance_reason, workspace)
        check_no_private_path_in_a_shared_artifact(report, output, "before-publish")
        check_readings_recorded(report, workspace, "before-publish")
    report.progress = completion_progress(workspace, provenance, output)
    return report


# ---------------------------------------------------------------------------------------------
# progress: how far a run got on real data, kept apart from whether it was right
# ---------------------------------------------------------------------------------------------
#
# The ten-unit trial is judged twice, and the two verdicts are never combined (user decision,
# 2026-09-25). Whether the pipeline is ready is Part A in the trial manifest. How far each run got is
# Part B, and it is derived here from the artifacts on disk -- never from a stage written into a
# manifest by hand, because that is the record that could say "done" for a unit whose gates and
# records exist and which never ran. Some stages do read fields the provenance manifest records,
# and each is paired with an artifact where one exists: B1 downloads, input_candidates and the
# unit's or its owner's status; B2 execution_allowed; B3 the Class proposal's status; B4 peak_height_diagnostics
# with its directory on disk; B7 status, finalized_at and mztab_validation with an mzTab-M on disk;
# B10 raw_retention_policy and retained_artifact_inventory.
#
# A stage is reached when its artifacts exist, whether or not they are right. Correctness is the
# checks' business, and a stage walk that stopped at the first FAIL would report a downloaded,
# preflighted and diagnosed unit as "B0 none" on account of one refused check, which is the very
# conflation this separation exists to prevent. The checks that judge each stage are listed beside
# it, and reported by stage in the JSON.
COMPLETION_STAGES = (
    ("B1", "downloaded",
     "the raw owner's manifest lists its downloads, and every input is on disk or the raw tree was released "
     "by the confirmed cleanup (status raw_cleaned)",
     ("SUM-1", "CONV-1", "PAIR-1")),
    ("B2", "preflight_passed", "the manifest permits execution", ("ID-1", "SPL-1", "ELIG-1", "PRE-1", "PRE-2")),
    ("B3", "class_settled",
     "a ratified Class proposal, or where the Catalog abstains a ratified abstention (accepted, confirmed or approved)",
     ("CLS-3",)),
    ("B4", "diagnostic_done", "a recorded peak-height diagnostic whose absolute directory exists", ("PKH-1",)),
    ("B5", "production_prepared", "output holds analysis_files.csv, method.txt and run-manifest.json",
     ("CLS-1", "CLS-2", "ORD-1", "CNT-1@before-production", "INP-1", "ACQ-1")),
    ("B6", "production_run_done", "at least one .mdpeak in output",
     ("EXP-1", "CNT-1@after-run", "MTH-1", "AIF-1", "FIN-2")),
    ("B7", "mztab_validated",
     "a validated terminal status, a validation record with no failure, and an mzTab-M in output",
     ("TAB-1", "TAB-2", "BIN-1", "FIN-1")),
    ("B8", "qa_produced", "a QA matrix (*.qa.tsv) exists", ("QA-1", "ORD-2")),
    ("B9", "publication_artifacts",
     "the publication report, Materials and Methods and supplementary table exist",
     ("LIB-1", "SEC-1", "SUM-2", "READ-1")),
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
    diagnostic_directory = str((diagnostics[-1] if diagnostics else {}).get("diagnostic_run_directory") or "")
    raw_path, raw_owner, _unknown = _raw_directory(record, workspace)
    status = str(record.get("status") or "")
    # Released only by the confirmed cleanup, the one deletion path, which records raw_cleaned.
    raw_released = raw_path is not None and not raw_path.exists() and (
        status == "raw_cleaned" or str((raw_owner or {}).get("status") or "") == "raw_cleaned"
    )
    facts = {
        # Downloaded, and either still on disk or released by the confirmed cleanup: a confirmed
        # deletion is the campaign's intended end state, not a lost download.
        "B1": bool(owner and owner.get("downloads")) and bool(candidates)
        and (all(path.exists() for path in candidates) or raw_released),
        "B2": record.get("execution_allowed") is True,
        "B3": _ratified(_class_proposal(record)),
        "B4": bool(diagnostic_directory) and Path(diagnostic_directory).is_absolute()
        and Path(diagnostic_directory).is_dir(),
        "B5": all((output / name).is_file() for name in ("analysis_files.csv", "method.txt", "run-manifest.json")),
        # The .mdpeak files are the run's own output; whether the Console also wrote its key record
        # is MTH-1's to judge.
        "B6": _mdpeak_count(output) > 0,
        "B7": not _unvalidated(record, output),
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


def _severity(check: "Check") -> int:
    if check.status == FAIL:
        return 4
    if check.status == NOT_EVALUABLE and check.required:
        return 3
    if check.status == WARN:
        return 2
    if check.status == NOT_EVALUABLE:
        return 1
    return 0


def _checks_by_stage(report: "Report") -> dict[str, dict[str, str]]:
    """Each stage's checks and their verdict. A check that ran more than once -- TAB-1 and TAB-2 run
    once per mzTab-M -- is reported by its worst instance, so a FAIL is never shown as a pass."""
    result: dict[str, dict[str, str]] = {}
    for code, _name, _meaning, keys in COMPLETION_STAGES:
        entries: dict[str, str] = {}
        for key in keys:
            check_id, _, stage = key.partition("@")
            matching = [check for check in report.checks
                        if check.check_id == check_id and (not stage or check.stage == stage)]
            if matching:
                entries[key] = max(matching, key=_severity).status
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
    blocked = report.run_blocked_by
    if blocked:
        lines.append("CHECKS THAT STOP A CAMPAIGN RUN (run_policy blocks_run, FAILed or unevaluable where owed): "
                     + ", ".join(blocked))
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

    try:
        report = verify(workspace, args.stage)
    except OutputOutsideWorkspace as error:
        print(f"unusable workspace: {error}", file=sys.stderr)
        return 3
    # Details quote the artifacts, and a console code page such as cp932 has no "\u2264" in it: the
    # print would end the gate with no report at all. JSON escapes what it cannot print; text
    # replaces it.
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=True, indent=2))
    else:
        try:
            sys.stdout.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
        print(render(report))
    if not report.ok:
        return 2
    # A FAIL outranks a strict refusal, because a fact established beats a fact missing.
    if args.strict and report.strict_failures:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
